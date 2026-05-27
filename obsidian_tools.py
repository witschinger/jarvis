"""
Jarvis — Obsidian Vault helpers (read + append).

Pure filesystem access — no Obsidian app required to be running. Two main
verbs:
  - vault_search(query)         : ripgrep-style substring search across .md
  - append_to_daily_note(text)  : append a bullet to today's daily note,
                                  creating it if missing

The vault root is `obsidian_vault_path` in config.json. Daily-note folder
defaults to the conventional "Daily Notes" subfolder; override via
`obsidian_daily_folder` in config.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def _load_config() -> dict:
    with open(_CONFIG_PATH, "r") as f:
        return json.load(f)


def _vault_root() -> Path:
    cfg = _load_config()
    raw = (cfg.get("obsidian_vault_path") or "").strip()
    if not raw or raw.startswith("YOUR_"):
        raise RuntimeError(
            "Obsidian vault nicht konfiguriert. Setze 'obsidian_vault_path' in config.json."
        )
    p = Path(os.path.expanduser(raw)).resolve()
    if not p.is_dir():
        raise RuntimeError(f"Obsidian vault Pfad existiert nicht oder ist kein Verzeichnis: {p}")
    return p


def _daily_folder() -> Path:
    cfg = _load_config()
    rel = (cfg.get("obsidian_daily_folder") or "Daily Notes").strip().strip("/")
    folder = _vault_root() / rel
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def is_configured() -> bool:
    try:
        _vault_root()
        return True
    except Exception:
        return False


def vault_search(query: str, limit: int = 20, max_line_len: int = 200) -> list[dict]:
    """
    Case-insensitive substring search across all *.md files under the vault.
    Returns up to `limit` hits with file path (relative), line number, snippet.
    """
    q = (query or "").strip()
    if not q:
        return []
    root = _vault_root()
    pattern = re.compile(re.escape(q), re.IGNORECASE)

    hits: list[dict] = []
    for path in root.rglob("*.md"):
        # Skip hidden dirs (.obsidian, .trash) and the daily folder if you want quieter results
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if pattern.search(line):
                        snippet = line.strip()
                        if len(snippet) > max_line_len:
                            snippet = snippet[:max_line_len] + "…"
                        hits.append({
                            "file": str(path.relative_to(root)),
                            "line": lineno,
                            "snippet": snippet,
                        })
                        if len(hits) >= limit:
                            return hits
        except OSError:
            continue
    return hits


def _today_daily_path() -> Path:
    """Today's daily note, conventional `YYYY-MM-DD.md` under the configured folder."""
    return _daily_folder() / f"{datetime.now().strftime('%Y-%m-%d')}.md"


def _vault_name() -> str:
    """Vault name as Obsidian internally identifies it — the last path component of the vault root."""
    return _vault_root().name


def _run_open(url: str) -> None:
    """macOS `open` wrapper; non-blocking, errors propagate as exceptions."""
    res = subprocess.run(["/usr/bin/open", url], capture_output=True, text=True, timeout=5)
    if res.returncode != 0:
        raise RuntimeError(f"open failed for {url[:80]}…: {res.stderr.strip()}")


def open_note(note_name: str) -> dict:
    """
    Open (or focus) an existing note in the running Obsidian app via the
    obsidian:// URL scheme. `note_name` may be a vault-relative path with or
    without a .md extension.
    """
    if not note_name.strip():
        raise ValueError("Note-Name leer.")
    name = note_name.strip()
    if name.lower().endswith(".md"):
        name = name[:-3]
    url = (
        "obsidian://open?vault=" + urllib.parse.quote(_vault_name())
        + "&file=" + urllib.parse.quote(name)
    )
    _run_open(url)
    return {"opened": name, "vault": _vault_name(), "url": url}


def focus_search(query: str) -> dict:
    """Open the Obsidian search panel pre-filled with `query`."""
    q = query.strip()
    if not q:
        raise ValueError("Suchbegriff leer.")
    url = (
        "obsidian://search?vault=" + urllib.parse.quote(_vault_name())
        + "&query=" + urllib.parse.quote(q)
    )
    _run_open(url)
    return {"query": q, "url": url}


def create_note_via_url(name: str, content: str = "") -> dict:
    """
    Create a new note in Obsidian via the obsidian://new URL scheme. The note
    pops up in the UI immediately. `content` is URL-encoded so its practical
    length cap is ~2 KB; use vault_root file writes for longer content.
    """
    if not name.strip():
        raise ValueError("Notiz-Name leer.")
    params = {
        "vault": _vault_name(),
        "name": name.strip(),
    }
    if content:
        params["content"] = content
    url = "obsidian://new?" + urllib.parse.urlencode(params)
    if len(url) > 4096:
        raise ValueError("Inhalt zu lang fuer URL-Scheme (>4 KB). Nutze append_to_daily_note oder direkten File-Write.")
    _run_open(url)
    return {"name": name.strip(), "url_length": len(url)}


def append_to_daily_note(content: str, prefix_bullet: bool = True) -> dict:
    """Append a line to today's daily note. Creates the file if missing."""
    content = (content or "").strip()
    if not content:
        raise ValueError("Inhalt leer.")
    path = _today_daily_path()
    ts = datetime.now().strftime("%H:%M")
    line = f"- {ts} — {content}" if prefix_bullet else content
    is_new = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# {datetime.now().strftime('%A, %d. %B %Y')}\n\n")
        f.write(line + "\n")
    return {
        "path": str(path),
        "appended": line,
        "created_file": is_new,
    }
