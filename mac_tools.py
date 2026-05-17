"""
Jarvis — Mac System Bridge via AppleScript (osascript).

Narrowly scoped on purpose: a handful of named verbs (reminder/note/open) so
the LLM cannot construct arbitrary AppleScript at runtime. Each function
escapes its arguments before embedding them in the script literal.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from typing import Optional


def _quote(value: str) -> str:
    """Escape a string for safe embedding inside a literal AppleScript double-quoted string."""
    # Newlines -> AppleScript line continuation via concatenation
    s = value.replace("\\", "\\\\").replace("\"", "\\\"")
    # AppleScript string literals don't allow real newlines; replace with explicit return
    s = s.replace("\r\n", "\n").replace("\n", "\" & return & \"")
    return f'"{s}"'


async def _run_osascript(script: str, timeout: float = 8.0) -> tuple[int, str, str]:
    """Run an AppleScript snippet, return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"osascript timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip(), err.decode("utf-8", errors="replace").strip()


# --- Reminders -----------------------------------------------------------------

async def create_reminder(title: str, body: Optional[str] = None, list_name: Optional[str] = None) -> dict:
    """Create a reminder in the macOS Reminders app. Returns the reminder's id."""
    if not title.strip():
        raise ValueError("Reminder title leer.")

    list_clause = f'list {_quote(list_name)}' if list_name else 'default list'
    props = [f"name:{_quote(title)}"]
    if body:
        props.append(f"body:{_quote(body)}")
    props_clause = ", ".join(props)

    script = f"""
tell application "Reminders"
    set newRem to make new reminder at {list_clause} with properties {{{props_clause}}}
    return id of newRem
end tell
""".strip()
    rc, out, err = await _run_osascript(script)
    if rc != 0:
        raise RuntimeError(f"Reminders failed: {err or out}")
    return {"id": out, "title": title}


# --- Notes ---------------------------------------------------------------------

async def create_note(body: str, title: Optional[str] = None, folder: Optional[str] = None) -> dict:
    """
    Create a note in macOS Notes. If `title` is omitted, the first line of the
    body becomes the title (Apple Notes convention).
    """
    if not body.strip():
        raise ValueError("Note body leer.")

    if title is None:
        title = body.strip().splitlines()[0][:80]

    # AppleScript Notes uses HTML body — wrap plain text minimally
    safe_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    html_body = f"<h1>{title.replace('<', '&lt;')}</h1>{safe_body}"

    if folder:
        body_decl = (
            f'tell application "Notes"\n'
            f'    set newNote to make new note at folder {_quote(folder)} '
            f'with properties {{name:{_quote(title)}, body:{_quote(html_body)}}}\n'
            f'    return id of newNote\n'
            f'end tell'
        )
    else:
        # No folder clause — Notes places the new note in the default account's default folder.
        body_decl = (
            f'tell application "Notes"\n'
            f'    set newNote to make new note '
            f'with properties {{name:{_quote(title)}, body:{_quote(html_body)}}}\n'
            f'    return id of newNote\n'
            f'end tell'
        )
    script = body_decl
    rc, out, err = await _run_osascript(script)
    if rc != 0:
        raise RuntimeError(f"Notes failed: {err or out}")
    return {"id": out, "title": title}


# --- Open app or URL -----------------------------------------------------------

# Explicit allowlist of bundle IDs / app names users typically want to launch
# from voice. Anything not on this list is rejected to keep the surface narrow.
APP_ALLOWLIST = {
    "finder", "safari", "google chrome", "chromium", "firefox",
    "notes", "reminders", "calendar", "mail", "messages",
    "spotify", "music", "obsidian", "slack", "zoom", "microsoft teams",
    "visual studio code", "code", "terminal", "iterm", "1password",
    "system settings", "system preferences",
    "asana",
}


async def open_app_or_url(target: str) -> dict:
    """Open a URL or whitelisted macOS app via the `open` command."""
    t = (target or "").strip()
    if not t:
        raise ValueError("Open target leer.")

    if "://" in t:
        # URL — allow any scheme `open` can handle (http, https, mailto, custom app schemes)
        proc = await asyncio.create_subprocess_exec("/usr/bin/open", t,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"open URL failed: {err.decode(errors='replace')}")
        return {"opened": t, "kind": "url"}

    if t.lower() not in APP_ALLOWLIST:
        raise PermissionError(
            f"App '{t}' nicht auf der Whitelist. Erweitere APP_ALLOWLIST in mac_tools.py."
        )

    proc = await asyncio.create_subprocess_exec("/usr/bin/open", "-a", t,
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"open -a failed: {err.decode(errors='replace')}")
    return {"opened": t, "kind": "app"}


# --- Paste into app -----------------------------------------------------------

async def paste_into_app(app_name: str, text: str) -> dict:
    """
    Activate `app_name` (must be in APP_ALLOWLIST) and inject `text` at the
    current cursor position via clipboard + Cmd+V. Uses paste rather than
    keystroke to survive arbitrary unicode/punctuation in the text.
    """
    if not text.strip():
        raise ValueError("Paste text leer.")
    if app_name.lower() not in APP_ALLOWLIST:
        raise PermissionError(
            f"App '{app_name}' nicht auf der Whitelist. Erweitere APP_ALLOWLIST in mac_tools.py."
        )

    pb = await asyncio.create_subprocess_exec(
        "/usr/bin/pbcopy",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, pb_err = await pb.communicate(text.encode("utf-8"))
    if pb.returncode != 0:
        raise RuntimeError(f"pbcopy failed: {pb_err.decode(errors='replace')}")

    script = (
        f'tell application {_quote(app_name)} to activate\n'
        f'delay 0.25\n'
        f'tell application "System Events" to keystroke "v" using command down'
    )
    rc, out, err = await _run_osascript(script)
    if rc != 0:
        raise RuntimeError(f"paste failed: {err or out}")
    return {"pasted_into": app_name, "chars": len(text)}
