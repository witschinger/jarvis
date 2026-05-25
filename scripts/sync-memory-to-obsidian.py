#!/usr/bin/env python3
"""
Sync Claude Code memory entries into the Obsidian vault.

Source: ~/.claude/projects/<repo-slug>/memory/*.md
Target: <vault>/Claude Code Memory/Knowledge/<filename>.md

The sync owns only files marked with sync_managed: true. Existing curated
Obsidian files are detected by alias and never overwritten unless explicitly
requested for a managed stub with --force-update.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DELAY_SECONDS = 60
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
LOGGER = logging.getLogger("sync-memory")


class SyncConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMemory:
    path: Path
    name: str
    text: str
    frontmatter: dict
    body: str


@dataclass(frozen=True)
class VaultMemory:
    path: Path
    frontmatter: dict
    body: str
    aliases: tuple[str, ...]
    managed: bool


def repo_memory_slug(repo: Path = REPO) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(repo.resolve())).rstrip("-")


def claude_memory_dir() -> Path:
    configured = os.environ.get("CLAUDE_PROJECT_MEMORY")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "projects" / repo_memory_slug() / "memory"


def vault_path(repo: Path = REPO) -> Path:
    config_path = repo / "config.json"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError as e:
        raise SyncConfigError(f"missing config file: {config_path}") from e
    except json.JSONDecodeError as e:
        raise SyncConfigError(f"invalid JSON in {config_path}: {e}") from e
    except OSError as e:
        raise SyncConfigError(f"cannot read {config_path}: {e}") from e

    raw = cfg.get("obsidian_vault_path")
    if not isinstance(raw, str) or not raw.strip():
        raise SyncConfigError("config.json must contain a non-empty obsidian_vault_path")

    vault = Path(raw).expanduser()
    if not vault.exists() or not vault.is_dir():
        raise SyncConfigError(f"obsidian_vault_path is not a directory: {vault}")
    return vault


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        parsed = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"invalid frontmatter: {e}") from e
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")
    return parsed, text[m.end() :]


def render_frontmatter(frontmatter: dict, body: str) -> str:
    dumped = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    ).strip()
    return f"---\n{dumped}\n---\n\n{body.lstrip()}"


def aliases_from_frontmatter(frontmatter: dict) -> tuple[str, ...]:
    raw = frontmatter.get("aliases")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(v for v in raw if isinstance(v, str) and v)
    return ()


def is_managed_stub(frontmatter: dict, body: str) -> bool:
    if frontmatter.get("sync_managed") is True:
        return True
    synced_from = frontmatter.get("synced_from")
    return isinstance(synced_from, str) and ".claude" in synced_from and "Auto-synced stub" in body


def read_source_memory(path: Path) -> SourceMemory | None:
    try:
        text = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
    except Exception as e:
        LOGGER.warning("parse-error source=%s error=%s", path.name, e)
        return None

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        LOGGER.warning("skip source=%s reason=no-name-in-frontmatter", path.name)
        return None
    return SourceMemory(path=path, name=name.strip(), text=text, frontmatter=fm, body=body)


def collect_sources(memory_dir: Path, min_source_age: int) -> list[SourceMemory]:
    sources: list[SourceMemory] = []
    now = time.time()
    for src in sorted(memory_dir.glob("*.md")):
        if src.name == "MEMORY.md":
            continue
        try:
            age = now - src.stat().st_mtime
        except OSError as e:
            LOGGER.warning("skip source=%s reason=stat-failed error=%s", src.name, e)
            continue
        if age < min_source_age:
            LOGGER.info("skip source=%s reason=too-new age=%.1fs min_age=%ss", src.name, age, min_source_age)
            continue
        memory = read_source_memory(src)
        if memory:
            sources.append(memory)
    return sources


def collect_active_source_names(memory_dir: Path) -> set[str]:
    names: set[str] = set()
    for src in sorted(memory_dir.glob("*.md")):
        if src.name == "MEMORY.md":
            continue
        memory = read_source_memory(src)
        if memory:
            names.add(memory.name)
    return names


def collect_vault_memories(search_root: Path) -> list[VaultMemory]:
    memories: list[VaultMemory] = []
    if not search_root.exists():
        return memories

    for md in sorted(search_root.rglob("*.md")):
        try:
            text = md.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
        except Exception as e:
            LOGGER.warning("skip vault_file=%s reason=read-or-parse-failed error=%s", md, e)
            continue
        aliases = aliases_from_frontmatter(fm)
        memories.append(
            VaultMemory(
                path=md,
                frontmatter=fm,
                body=body,
                aliases=aliases,
                managed=is_managed_stub(fm, body),
            )
        )
    return memories


def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")
    sanitized = unicodedata.normalize("NFC", sanitized)
    if not sanitized:
        raise ValueError(f"name cannot be converted to a safe filename: {name!r}")
    return sanitized


def normalized_filename_key(path: Path) -> str:
    return unicodedata.normalize("NFC", path.name).casefold()


def memory_type(frontmatter: dict) -> str:
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("type"), str):
        return metadata["type"]
    return "memory"


def make_obsidian_stub(source: SourceMemory) -> str:
    mem_type = memory_type(source.frontmatter)
    date = time.strftime("%Y-%m-%d")
    title = source.name.replace("-", " ").replace("_", " ").title()
    description = source.frontmatter.get("description", "")
    if not isinstance(description, str):
        description = ""

    obsidian_fm = {
        "title": title,
        "aliases": [source.name],
        "tags": ["claude-code", mem_type],
        "type": mem_type,
        "sync_managed": True,
        "orphaned": False,
        "synced_source_file": str(source.path),
        "synced_source_name": source.name,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created": date,
    }
    body = (
        f"# {title}\n\n"
        f"> [!info] Auto-synced stub\n"
        f"> Aus Claude-Code-Memory `{source.name}` synct. {description}\n"
        f"> ==Bei Bedarf manuell mit Obsidian-Formatierung anreichern (Callouts, Wikilinks, Tables) — der Sync überschreibt nicht.==\n\n"
        f"## Ursprünglicher Memory-Inhalt\n\n"
        f"{source.body.strip()}\n"
    )
    return render_frontmatter(obsidian_fm, body)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


@contextlib.contextmanager
def sync_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RuntimeError("another sync-memory process is already running") from e
        yield


def mark_orphans(vault_memories: list[VaultMemory], active_names: set[str], dry_run: bool) -> int:
    marked = 0
    for memory in vault_memories:
        if not memory.managed:
            continue
        if memory.frontmatter.get("orphaned") is True:
            continue
        source_name = memory.frontmatter.get("synced_source_name")
        aliases = set(memory.aliases)
        still_active = (isinstance(source_name, str) and source_name in active_names) or bool(aliases & active_names)
        if still_active:
            continue
        updated = dict(memory.frontmatter)
        updated["orphaned"] = True
        updated["orphaned_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        LOGGER.warning("orphaned vault_file=%s", memory.path)
        if not dry_run:
            atomic_write(memory.path, render_frontmatter(updated, memory.body))
        marked += 1
    return marked


def sync_sources(
    sources: list[SourceMemory],
    vault_memories: list[VaultMemory],
    knowledge_dir: Path,
    force_update: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    alias_to_memory: dict[str, VaultMemory] = {}
    for memory in vault_memories:
        for alias in memory.aliases:
            if alias in alias_to_memory and alias_to_memory[alias].path != memory.path:
                LOGGER.warning("duplicate alias=%s files=%s,%s", alias, alias_to_memory[alias].path, memory.path)
            alias_to_memory.setdefault(alias, memory)

    existing_targets = {normalized_filename_key(path): path for path in knowledge_dir.glob("*.md")}
    planned_targets: dict[str, SourceMemory] = {}
    created = 0
    updated = 0
    skipped = 0

    for source in sources:
        try:
            target = knowledge_dir / f"{sanitize_filename(source.name)}.md"
        except ValueError as e:
            LOGGER.warning("skip source=%s reason=%s", source.path.name, e)
            skipped += 1
            continue

        target_key = normalized_filename_key(target)
        if target_key in planned_targets and planned_targets[target_key].name != source.name:
            LOGGER.warning(
                "skip source=%s reason=filename-collision other_source=%s target=%s",
                source.path.name,
                planned_targets[target_key].path.name,
                target.name,
            )
            skipped += 1
            continue
        planned_targets[target_key] = source

        existing_by_alias = alias_to_memory.get(source.name)
        if existing_by_alias:
            if force_update and existing_by_alias.managed:
                LOGGER.info("update target=%s source=%s", existing_by_alias.path.name, source.path.name)
                if not dry_run:
                    atomic_write(existing_by_alias.path, make_obsidian_stub(source))
                updated += 1
            elif existing_by_alias.managed and existing_by_alias.frontmatter.get("orphaned") is True:
                LOGGER.info("restore target=%s source=%s", existing_by_alias.path.name, source.path.name)
                if not dry_run:
                    restored = dict(existing_by_alias.frontmatter)
                    restored["orphaned"] = False
                    restored.pop("orphaned_at", None)
                    restored["synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    atomic_write(existing_by_alias.path, render_frontmatter(restored, existing_by_alias.body))
                updated += 1
            else:
                skipped += 1
            continue

        existing_target = existing_targets.get(target_key)
        if existing_target:
            LOGGER.warning(
                "skip source=%s reason=target-exists-without-alias target=%s alias=%s",
                source.path.name,
                existing_target,
                source.name,
            )
            skipped += 1
            continue

        LOGGER.info("create target=%s source=%s", target.name, source.path.name)
        if not dry_run:
            atomic_write(target, make_obsidian_stub(source))
        created += 1

    return created, updated, skipped


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [sync-memory] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-seconds", type=int, default=int(os.environ.get("JARVIS_MEMORY_SYNC_DELAY", DEFAULT_DELAY_SECONDS)))
    parser.add_argument("--min-source-age", type=int, default=0)
    parser.add_argument("--force-update", action="store_true", help="overwrite existing managed stubs from current source")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    if args.delay_seconds > 0:
        LOGGER.info("debounce delay=%ss", args.delay_seconds)
        time.sleep(args.delay_seconds)

    memory_dir = claude_memory_dir()
    if not memory_dir.exists():
        LOGGER.info("no memory dir at %s", memory_dir)
        return 0

    try:
        vault = vault_path()
    except SyncConfigError as e:
        LOGGER.error("%s", e)
        return 2

    knowledge_dir = vault / "Claude Code Memory" / "Knowledge"
    search_root = knowledge_dir.parent if knowledge_dir.exists() else knowledge_dir
    lock_path = REPO / ".memory-sync.lock"

    try:
        with sync_lock(lock_path):
            if not args.dry_run:
                knowledge_dir.mkdir(parents=True, exist_ok=True)
            active_source_names = collect_active_source_names(memory_dir)
            sources = collect_sources(memory_dir, args.min_source_age)
            vault_memories = collect_vault_memories(search_root)
            orphaned = mark_orphans(vault_memories, active_source_names, args.dry_run)
            created, updated, skipped = sync_sources(
                sources=sources,
                vault_memories=vault_memories,
                knowledge_dir=knowledge_dir,
                force_update=args.force_update,
                dry_run=args.dry_run,
            )
    except RuntimeError as e:
        LOGGER.warning("%s", e)
        return 0

    LOGGER.info("done created=%s updated=%s orphaned=%s skipped=%s ts=%s", created, updated, orphaned, skipped, int(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
