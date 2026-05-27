"""
Jarvis — Obsidian Local REST API client.

Talks to the Local REST API plugin (https://github.com/coddingtonbear/obsidian-local-rest-api)
that runs inside the Obsidian app on http://127.0.0.1:27123 (insecure local-only).
The pre-seeded API key lives in .local_rest_api_key beside config.json.

This is the high-fidelity layer: uses Obsidian's own tokenizer, respects plugin
behavior (Periodic Notes, Templater, etc.). The filesystem-only fallback lives
in obsidian_tools.py.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

REST_BASE = "http://127.0.0.1:27123"
_KEY_PATH = os.path.join(os.path.dirname(__file__), ".local_rest_api_key")
_TIMEOUT = 10.0


def _key() -> str:
    try:
        with open(_KEY_PATH) as f:
            k = f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"Local-REST-API-Key Datei fehlt: {_KEY_PATH}")
    if not k:
        raise RuntimeError("Local-REST-API-Key leer.")
    return k


def _headers(extra: Optional[dict] = None) -> dict:
    h = {"Authorization": f"Bearer {_key()}"}
    if extra:
        h.update(extra)
    return h


def is_available() -> bool:
    """Quick synchronous check that the plugin is reachable AND key works."""
    try:
        with httpx.Client(timeout=2) as c:
            r = c.get(REST_BASE + "/vault/", headers=_headers())
            return r.status_code == 200
    except Exception:
        return False


# --- Commands ----------------------------------------------------------------

async def list_commands() -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(REST_BASE + "/commands/", headers=_headers())
        r.raise_for_status()
        return r.json().get("commands", [])


async def run_command(command_id: str) -> dict:
    """Execute an Obsidian command palette command by its id."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(REST_BASE + f"/commands/{command_id}/", headers=_headers())
        if r.status_code >= 400:
            raise RuntimeError(f"run_command {command_id} -> {r.status_code}: {r.text[:200]}")
        return {"command_id": command_id, "http_status": r.status_code}


async def fuzzy_run_command(query: str) -> dict:
    """
    Lists all commands, picks the best name match for `query`, then executes it.
    Match heuristic: substring on lowercased name, then word-by-word AND match,
    prefers shorter (more specific) names.
    """
    cmds = await list_commands()
    if not cmds:
        raise RuntimeError("Keine Commands zurueckgegeben.")
    q = query.strip().lower()
    matches = [c for c in cmds if q in (c.get("name") or "").lower()]
    if not matches:
        words = [w for w in q.split() if w]
        matches = [c for c in cmds
                   if all(w in (c.get("name") or "").lower() for w in words)]
    if not matches:
        raise RuntimeError(f"Kein Obsidian-Command gefunden zu '{query}'.")
    cmd = sorted(matches, key=lambda c: len(c.get("name") or ""))[0]
    res = await run_command(cmd["id"])
    return {"matched_name": cmd["name"], "matched_id": cmd["id"], **res}


# --- Search ------------------------------------------------------------------

async def search_simple(query: str, context_length: int = 100) -> list[dict]:
    """Simple full-text search. Returns list of {filename, matches:[{match,context}]}."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            REST_BASE + "/search/simple/",
            headers=_headers(),
            params={"query": query, "contextLength": context_length},
        )
        r.raise_for_status()
        return r.json()


# --- Vault file ops ----------------------------------------------------------

async def append_to_file(path: str, text: str) -> dict:
    """Append plain text to an existing note (created if missing)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            REST_BASE + f"/vault/{path}",
            headers=_headers({"Content-Type": "text/markdown"}),
            content=text.encode("utf-8"),
        )
        if r.status_code >= 400:
            raise RuntimeError(f"append_to_file {path} -> {r.status_code}: {r.text[:200]}")
        return {"path": path, "http_status": r.status_code, "appended_bytes": len(text)}


async def get_active() -> dict:
    """Return content of the note currently focused in Obsidian, or empty dict if none."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(REST_BASE + "/active/", headers=_headers())
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        # Plugin returns content as text/markdown with metadata in JSON if Accept asks for it
        return {"content": r.text, "headers": dict(r.headers)}


# --- Periodic notes ----------------------------------------------------------

async def append_to_periodic(period: str, text: str) -> dict:
    """
    Append text to a periodic note. `period` is one of 'daily', 'weekly',
    'monthly', 'quarterly', 'yearly'. Honors the Periodic Notes plugin's config
    (template, folder, naming).
    """
    if period not in ("daily", "weekly", "monthly", "quarterly", "yearly"):
        raise ValueError(f"Ungueltiger period: {period}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            REST_BASE + f"/periodic/{period}/",
            headers=_headers({"Content-Type": "text/markdown"}),
            content=text.encode("utf-8"),
        )
        if r.status_code >= 400:
            raise RuntimeError(f"append_to_periodic {period} -> {r.status_code}: {r.text[:200]}")
        return {"period": period, "http_status": r.status_code}
