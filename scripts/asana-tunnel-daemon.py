#!/usr/bin/env python3
"""
Asana tunnel daemon.

Long-running process that:
  1. Opens an SSH tunnel to a.pinggy.io exposing localhost:8340 publicly.
  2. Captures the *.run.pinggy-free.link URL from SSH stdout.
  3. Deletes any previously registered Asana webhook (state in
     .asana_webhook_state.json), then re-registers a new webhook against the
     fresh public URL.
  4. Holds the SSH session open until either the tunnel dies, ~55 minutes
     elapse (anonymous Pinggy expires at 60 min), or the daemon is signalled.
  5. Cleans up: deletes the webhook, kills SSH, then loops.

Designed to run under launchd as com.user.jarvis.asana, with KeepAlive.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

import asana_tools  # noqa: E402

STATE_FILE = os.path.join(ROOT_DIR, ".asana_webhook_state.json")
PORT = 8340
PINGGY_HOST = "a.pinggy.io"
PINGGY_PORT = 443
RENEW_BEFORE_EXPIRY_S = 5 * 60      # rotate ~5 min before the 60-min Pinggy cap
URL_PATTERN = re.compile(r"https://[a-zA-Z0-9.-]+\.run\.pinggy-free\.link")
JARVIS_WAIT_S = 60                  # how long to wait for the local Jarvis server before retrying

DEFAULT_FILTERS = [
    {"resource_type": "task", "action": "added"},
    {"resource_type": "task", "action": "changed"},
    {"resource_type": "task", "action": "deleted"},
    {"resource_type": "task", "action": "removed"},
    {"resource_type": "task", "action": "undeleted"},
    {"resource_type": "story", "action": "added"},
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[asana-daemon {ts}] {msg}", flush=True)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"warning: could not read state file: {e}")
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"warning: could not write state file: {e}")


def jarvis_running() -> bool:
    """Cheap port probe — TCP connect to localhost:8340."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2):
            return True
    except OSError:
        return False


async def delete_webhook_safely(gid: str) -> None:
    try:
        await asana_tools.delete_webhook(gid)
        log(f"deleted webhook {gid}")
    except Exception as e:
        log(f"could not delete webhook {gid}: {e}")


async def cleanup_stale_webhook() -> None:
    state = load_state()
    gid = state.get("webhook_gid")
    if gid:
        await delete_webhook_safely(gid)
    save_state({})


async def open_tunnel_and_register() -> tuple[asyncio.subprocess.Process, str]:
    """Spawn ssh, parse the public URL, register a fresh webhook. Returns (proc, gid)."""
    log("starting Pinggy SSH tunnel...")
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-p", str(PINGGY_PORT),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes",
        f"-R0:localhost:{PORT}",
        "-T",
        f"nokey@{PINGGY_HOST}",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    public_url: str | None = None
    try:
        for _ in range(60):
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
            except asyncio.TimeoutError:
                line = b""
            if not line:
                if proc.returncode is not None:
                    raise RuntimeError(f"ssh exited early (rc={proc.returncode})")
                continue
            line_s = line.decode(errors="ignore").strip()
            if line_s:
                log(f"  ssh: {line_s}")
            m = URL_PATTERN.search(line_s)
            if m:
                public_url = m.group(0)
                break
    except Exception:
        proc.terminate()
        raise

    if not public_url:
        proc.terminate()
        raise RuntimeError("Could not capture Pinggy URL from SSH output within timeout")

    log(f"public URL: {public_url}")

    user_task_list_gid = await asana_tools.get_my_user_task_list_gid()
    target = public_url.rstrip("/") + "/asana/webhook"
    try:
        result = await asana_tools.create_webhook(user_task_list_gid, target, filters=DEFAULT_FILTERS)
    except Exception:
        proc.terminate()
        raise

    webhook_gid = result.get("gid", "")
    log(f"registered webhook gid={webhook_gid} resource={user_task_list_gid}")

    save_state({
        "webhook_gid": webhook_gid,
        "public_url": public_url,
        "target": target,
        "registered_at": datetime.now(timezone.utc).isoformat(),
    })
    return proc, webhook_gid


async def cycle(stop: asyncio.Event) -> None:
    """One full tunnel lifecycle. Returns after teardown."""
    while not jarvis_running() and not stop.is_set():
        log(f"localhost:{PORT} not responding — waiting {JARVIS_WAIT_S}s for Jarvis server")
        try:
            await asyncio.wait_for(stop.wait(), timeout=JARVIS_WAIT_S)
        except asyncio.TimeoutError:
            pass
    if stop.is_set():
        return

    await cleanup_stale_webhook()
    try:
        proc, gid = await open_tunnel_and_register()
    except Exception as e:
        log(f"tunnel/registration failed: {e}")
        return

    try:
        ssh_task = asyncio.create_task(proc.wait(), name="ssh")
        renew_task = asyncio.create_task(
            asyncio.sleep(60 * 60 - RENEW_BEFORE_EXPIRY_S),
            name="renew_timer",
        )
        stop_task = asyncio.create_task(stop.wait(), name="stop")

        # Drain ssh stdout so the buffer doesn't fill up and block the process
        async def _drain() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                s = line.decode(errors="ignore").rstrip()
                if s:
                    log(f"  ssh: {s}")

        drain_task = asyncio.create_task(_drain(), name="drain")

        done, pending = await asyncio.wait(
            [ssh_task, renew_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        drain_task.cancel()

        finisher = next(iter(done))
        log(f"cycle ended via: {finisher.get_name()}")
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await delete_webhook_safely(gid)
        save_state({})


async def main() -> int:
    if not asana_tools.is_configured():
        print("Asana PAT fehlt in config.json — daemon exits.", file=sys.stderr)
        return 2

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop() -> None:
        if not stop.is_set():
            log("stop signal received; will tear down current cycle")
            stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: request_stop())

    log("daemon starting")
    while not stop.is_set():
        try:
            await cycle(stop)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"unexpected cycle error: {e}")

        if stop.is_set():
            break

        # Brief pause before next cycle so we don't spin on a hard error
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass

    log("daemon shutting down; final cleanup")
    await cleanup_stale_webhook()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
