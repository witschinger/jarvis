#!/usr/bin/env python3
"""
Sync Claude Code memory entries into the Obsidian vault.

Source: ~/.claude/projects/-Users-stephan-merk-Python-Code-jarvis/memory/*.md
Target: <vault>/Claude Code Memory/Knowledge/<filename>.md

For each Claude memory file:
  1. Parse the `name:` field from frontmatter.
  2. Look for an existing Obsidian file whose `aliases:` block lists that name.
  3. If found -> leave it alone (manual enhancements persist).
  4. If not -> create a stub Obsidian file with proper frontmatter and the
     Claude memory body inlined.

Designed to be triggered by launchd `WatchPaths` on the source directory.
Idempotent, side-effect-free aside from new file creation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

REPO = Path("/Users/stephan.merk/Python_Code/jarvis")
CLAUDE_MEMORY_DIR = Path.home() / ".claude/projects/-Users-stephan-merk-Python-Code-jarvis/memory"


def vault_path() -> Path:
    with (REPO / "config.json").open("r") as f:
        cfg = json.load(f)
    return Path(cfg["obsidian_vault_path"])


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser sufficient for our flat-ish schema."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}, text
    body = text[m.end():]
    fm: dict = {}
    current_key = None
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # nested key: "  type: feedback"
        if raw.startswith("  ") and ":" in raw:
            k, _, v = raw.strip().partition(":")
            if current_key == "metadata":
                fm.setdefault("metadata", {})[k.strip()] = v.strip()
            continue
        # top-level key: value OR key:
        k, _, v = raw.partition(":")
        k = k.strip()
        v = v.strip()
        if v == "":
            current_key = k
            fm[k] = {} if k == "metadata" else None
        else:
            current_key = k
            fm[k] = v
    return fm, body


def claude_name_from_file(path: Path) -> str | None:
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    name = fm.get("name")
    return name if isinstance(name, str) else None


def collect_existing_aliases(knowledge_dir: Path) -> set[str]:
    aliases: set[str] = set()
    if not knowledge_dir.exists():
        return aliases
    for md in knowledge_dir.glob("*.md"):
        text = md.read_text(encoding="utf-8")
        # find aliases block in frontmatter
        m = re.search(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        if not m:
            continue
        fm_text = m.group(1)
        # bullet-style: "aliases:\n  - foo\n  - bar"
        a_match = re.search(r"^aliases:\s*\n((?:\s*-\s.+\n?)+)", fm_text, re.MULTILINE)
        if a_match:
            for line in a_match.group(1).splitlines():
                v = line.strip().lstrip("-").strip()
                if v:
                    aliases.add(v)
        # inline-style: "aliases: [foo, bar]"
        inline = re.search(r"^aliases:\s*\[(.+?)\]\s*$", fm_text, re.MULTILINE)
        if inline:
            for v in inline.group(1).split(","):
                v = v.strip().strip('"\'')
                if v:
                    aliases.add(v)
    return aliases


def make_obsidian_stub(claude_text: str, claude_name: str) -> str:
    fm, body = parse_frontmatter(claude_text)
    description = fm.get("description", "")
    mem_type = (fm.get("metadata") or {}).get("type", "memory") if isinstance(fm.get("metadata"), dict) else "memory"
    date = time.strftime("%Y-%m-%d")

    title = claude_name.replace("-", " ").replace("_", " ").title()
    tags = ["claude-code", mem_type]

    obsidian_fm = (
        f"---\n"
        f"title: {title}\n"
        f"aliases:\n"
        f"  - {claude_name}\n"
        f"tags:\n"
        + "".join(f"  - {t}\n" for t in tags)
        + f"type: {mem_type}\n"
        f"synced_from: ~/.claude/.../memory/{claude_name}.md\n"
        f"created: {date}\n"
        f"---\n\n"
    )
    obsidian_body = (
        f"# {title}\n\n"
        f"> [!info] Auto-synced stub\n"
        f"> Aus Claude-Code-Memory `{claude_name}` synct. {description}\n"
        f"> ==Bei Bedarf manuell mit Obsidian-Formatierung anreichern (Callouts, Wikilinks, Tables) — der Sync überschreibt nicht.==\n\n"
        f"## Ursprünglicher Memory-Inhalt\n\n"
        f"{body.strip()}\n"
    )
    return obsidian_fm + obsidian_body


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name).strip("_")


def main() -> int:
    if not CLAUDE_MEMORY_DIR.exists():
        print(f"[sync-memory] no memory dir at {CLAUDE_MEMORY_DIR}", flush=True)
        return 0

    vault = vault_path()
    knowledge_dir = vault / "Claude Code Memory" / "Knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    existing_aliases = collect_existing_aliases(knowledge_dir)
    created = 0
    skipped = 0

    for src in sorted(CLAUDE_MEMORY_DIR.glob("*.md")):
        if src.name == "MEMORY.md":
            continue
        try:
            claude_name = claude_name_from_file(src)
        except Exception as e:
            print(f"[sync-memory] parse-error {src.name}: {e}", flush=True)
            continue
        if not claude_name:
            print(f"[sync-memory] skip {src.name} (no name in frontmatter)", flush=True)
            continue
        if claude_name in existing_aliases:
            skipped += 1
            continue
        target = knowledge_dir / f"{sanitize_filename(claude_name)}.md"
        if target.exists():
            skipped += 1
            continue
        stub = make_obsidian_stub(src.read_text(encoding="utf-8"), claude_name)
        target.write_text(stub, encoding="utf-8")
        print(f"[sync-memory] created {target.name} <- {src.name}", flush=True)
        created += 1

    print(f"[sync-memory] done: {created} created, {skipped} already linked, ts={int(time.time())}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
