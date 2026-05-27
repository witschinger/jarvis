#!/usr/bin/env python3
"""
Partnerschaft & Privat — Sammlung

Scannt alle Daily-Notes unter `Journal/**` nach der Sektion
`## 💑 Partnerschaft & Privat`, extrahiert die Bullet-Inhalte (ohne
Boilerplate-Quote und ohne leere Platzhalter-Bullets) und baut eine
Master-Notiz `Partnerschaft Sammlung.md` am Vault-Root mit datums-gruppierten
Einträgen und Wikilinks zurück zur Quell-Daily.

Wird täglich via launchd (`com.user.jarvis.partnership-collect`) ausgeführt,
z.B. um 21:00 — nach dem Abend-Check-In und dem Yazio-Sync.

Args:
  --dry-run   nur stdout-Print, keine Datei schreiben
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

VAULT = Path("/Users/stephan.merk/obsidian/jarvis obsidian")
JOURNAL = VAULT / "Journal"
MASTER = VAULT / "Partnerschaft Sammlung.md"

SECTION_HEADING = "## 💑 Partnerschaft & Privat"
NEXT_H2 = re.compile(r"^## ", re.MULTILINE)
DAILY_FILENAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

# Boilerplate-Lines die wir herausfiltern (Hint-Quote + leere Bullets)
BOILERPLATE = (
    "Beobachtungen aus dem Alltag",
    "Momente, Konflikte, kleine Aufmerksamkeiten",
    "Themen die mir aufgefallen sind",
    "festhalten will",
)

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni",
             "Juli", "August", "September", "Oktober", "November", "Dezember"]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def extract_section(content: str) -> list[str]:
    """Find the `## 💑 Partnerschaft & Privat` heading and return its raw body until next `## `."""
    idx = content.find(SECTION_HEADING)
    if idx == -1:
        return []
    after = content[idx + len(SECTION_HEADING):]
    m = NEXT_H2.search(after)
    body = after[: m.start()] if m else after
    return [ln.rstrip() for ln in body.splitlines()]


def is_meaningful_entry(line: str) -> bool:
    """True only for bullets with actual content."""
    s = line.strip()
    if not s:
        return False
    if s.startswith(">"):  # blockquote (hint)
        return False
    if any(b in s for b in BOILERPLATE):
        return False
    if not s.startswith(("-", "*", "+", "•")):
        return False
    rest = s.lstrip("-*+•").strip()
    if not rest:  # leerer Bullet "-"
        return False
    return True


def scan() -> list[tuple[date, Path, list[str]]]:
    """Return [(date, file, [bullet-lines])] for all daily-notes with non-empty Partnerschaft-Section, sorted newest first."""
    results = []
    if not JOURNAL.exists():
        return results
    for md_file in JOURNAL.rglob("*.md"):
        m = DAILY_FILENAME.match(md_file.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        section_lines = extract_section(content)
        bullets = [ln for ln in section_lines if is_meaningful_entry(ln)]
        if bullets:
            results.append((d, md_file, bullets))
    results.sort(key=lambda r: r[0], reverse=True)
    return results


def build_master(entries: list[tuple[date, Path, list[str]]]) -> str:
    total_entries = sum(len(b) for _, _, b in entries)
    total_days = len(entries)
    span = ""
    if entries:
        oldest = entries[-1][0]
        newest = entries[0][0]
        span = f"{oldest.isoformat()} bis {newest.isoformat()}"
    now = datetime.now()

    head = f"""---
tags:
  - moc
  - partnerschaft
  - sammlung
  - privat
type: collection
auto_generated: true
updated: {now.strftime('%Y-%m-%d %H:%M')}
total_entries: {total_entries}
total_days: {total_days}
span: {span}
---

# 💑 Partnerschaft & Privat — Sammlung

> [!info] Automatisch zusammengetragen
> Diese Notiz wird täglich um 21:00 Uhr aus allen `## 💑 Partnerschaft & Privat`-Sektionen deiner Daily-Notes neu gebaut. **Direkte Edits hier verschwinden beim nächsten Lauf** — Einträge nur in den jeweiligen Daily-Notes ändern.

## 📊 Stand

- **Einträge insgesamt:** {total_entries}
- **Tage mit Einträgen:** {total_days}
- **Zeitraum:** {span or '—'}

## 📝 Einträge nach Datum (neueste zuerst)
"""

    parts = [head]
    for d, file, bullets in entries:
        weekday = WEEKDAYS_DE[d.weekday()]
        month = MONTHS_DE[d.month - 1]
        rel = file.relative_to(VAULT).with_suffix("")
        wikilink = f"[[{rel}|Daily]]"
        parts.append(f"\n### {weekday}, {d.day}. {month} {d.year} — {wikilink}\n")
        for b in bullets:
            parts.append(b)
        parts.append("")

    return "\n".join(parts) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="nur ausgeben, nicht schreiben")
    args = p.parse_args()

    entries = scan()
    md = build_master(entries)
    log(f"scanned {sum(1 for _ in JOURNAL.rglob('*.md'))} daily files, found {len(entries)} days with entries")

    if args.dry_run:
        sys.stdout.write(md)
        return 0

    MASTER.write_text(md, encoding="utf-8")
    log(f"wrote {MASTER.relative_to(VAULT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
