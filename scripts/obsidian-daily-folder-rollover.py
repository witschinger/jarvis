#!/usr/bin/env python3
"""
Obsidian Daily Notes Folder Rollover

Hält den `folder`-Pfad in `<vault>/.obsidian/daily-notes.json` automatisch
synchron mit dem aktuellen Monat. Geplant via launchd
(com.user.jarvis.obsidian-rollover), läuft beim Login + täglich.

Was es macht (idempotent):
  1. Bestimmt heutiges Jahr + Monat
  2. Stellt sicher, dass `Journal/<YEAR>/` existiert. Falls neu (z.B. 1. Januar):
     legt Jahres-MOC mit Mermaid + 12 Monats-Ordner + 12 Monats-MOCs an.
  3. Liest `daily-notes.json`, vergleicht mit erwarteter `Journal/<YEAR>/<MM-Monat>`.
     Falls abweichend → überschreibt mit korrektem Pfad.
  4. Best-effort: triggert `app:reload` via Local-REST-API damit Obsidian die Config
     ohne Neustart nimmt.
  5. Loggt jeden Lauf nach ~/Library/Logs/jarvis-daily-rollover.log

Re-run-Verhalten: alles no-op solange Monat unverändert + Strukturen vorhanden.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

VAULT = Path("/Users/stephan.merk/obsidian/jarvis obsidian")
REST_KEY_FILE = Path("/Users/stephan.merk/Python_Code/jarvis/.local_rest_api_key")
REST_BASE = "http://127.0.0.1:27123"

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def log(msg: str) -> None:
    ts = date.today().isoformat()
    print(f"[{ts}] {msg}", flush=True)


def year_moc_body(year: int) -> str:
    mermaid = "graph TD\n    Y[\"📅 " + str(year) + " Jahresübersicht\"]\n"
    for i, name in enumerate(MONTHS_DE, 1):
        mermaid += f"    Y --> M{i:02d}[\"{i:02d} {name}\"]\n"

    wikilinks = "\n".join(
        f"- [[Journal/{year}/{i:02d}-{name}/{year}-{i:02d} {name}|{i:02d} {name}]]"
        for i, name in enumerate(MONTHS_DE, 1)
    )

    return f"""---
tags:
  - journal
  - moc
  - year
  - "year/{year}"
type: year-moc
year: {year}
created: {date.today().isoformat()}
---

# 📅 {year} — Jahresübersicht

> [!info] Jahres-MOC
> Sammelpunkt für alle Tagebücher, Monatsreviews und Jahresziele in {year}. Tagesnotizen liegen unter `Journal/{year}/<MM-Monat>/`.

## 🎯 Jahresziele

- *(noch nicht definiert)*

## 🌳 Struktur-Diagramm

```mermaid
{mermaid}```

## 🗓 Monatsübersicht

{wikilinks}

## 📊 Auto-Rollup (Dataview)

```dataview
TABLE
  length(rows) AS "Notizen"
FROM "Journal/{year}"
WHERE file.name != "{year} — Übersicht"
GROUP BY substring(file.path, length("Journal/{year}/") + 1, 2) AS Monat
SORT Monat ASC
```

## 🔗 Verwandt

- [[Willkommen]]
- [[Claude Code — Skills Inventar]]
"""


def month_moc_body(year: int, idx: int) -> str:
    name = MONTHS_DE[idx - 1]
    body = f"""---
tags:
  - journal
  - moc
  - month
  - "month/{year}-{idx:02d}"
type: month-moc
year: {year}
month: {idx:02d}
month_name: {name}
created: {date.today().isoformat()}
---

# 📆 {name} {year}

> [!info] Monats-MOC
> Sammelpunkt für alle Tagebuch-Einträge im {name} {year}.

## 🎯 Monatsfokus

- *(noch nicht definiert)*

## 📝 Tagebuch-Einträge

```dataview
LIST
FROM "Journal/{year}/{idx:02d}-{name}"
WHERE file.name != "{year}-{idx:02d} {name}"
SORT file.name ASC
```

## 📊 Monatsreview

- **Highlights:** *(Ende des Monats füllen)*
- **Lessons:** *(was hat funktioniert)*
- **Übertrag in nächsten Monat:** *(was hochgenommen wird)*

## 🔗 Navigation

- [[{year} — Übersicht|⬆ {year} Jahresübersicht]]
"""
    if idx > 1:
        pi = idx - 1
        body += f"- [[Journal/{year}/{pi:02d}-{MONTHS_DE[pi-1]}/{year}-{pi:02d} {MONTHS_DE[pi-1]}|⬅ {MONTHS_DE[pi-1]}]]\n"
    if idx < 12:
        ni = idx + 1
        body += f"- [[Journal/{year}/{ni:02d}-{MONTHS_DE[ni-1]}/{year}-{ni:02d} {MONTHS_DE[ni-1]}|{MONTHS_DE[ni-1]} ➡]]\n"
    return body


def ensure_year_structure(year: int) -> bool:
    """Create Journal/<year>/ + 12 month folders + MOCs if missing. Returns True if anything was created."""
    year_dir = VAULT / "Journal" / str(year)
    created_anything = False

    if not year_dir.exists():
        year_dir.mkdir(parents=True)
        log(f"created year folder: {year_dir}")
        created_anything = True

    moc_path = year_dir / f"{year} — Übersicht.md"
    if not moc_path.exists():
        moc_path.write_text(year_moc_body(year), encoding="utf-8")
        log(f"wrote year MOC: {moc_path.name}")
        created_anything = True

    for i, name in enumerate(MONTHS_DE, 1):
        m_dir = year_dir / f"{i:02d}-{name}"
        m_dir.mkdir(parents=True, exist_ok=True)
        m_moc = m_dir / f"{year}-{i:02d} {name}.md"
        if not m_moc.exists():
            m_moc.write_text(month_moc_body(year, i), encoding="utf-8")
            log(f"wrote month MOC: {m_moc.relative_to(VAULT)}")
            created_anything = True

    return created_anything


def update_daily_notes_config(year: int, month_idx: int) -> bool:
    """Update vault/.obsidian/daily-notes.json so daily-notes-plugin uses the current month. Returns True if changed."""
    expected = f"Journal/{year}/{month_idx:02d}-{MONTHS_DE[month_idx-1]}"
    cfg_path = VAULT / ".obsidian" / "daily-notes.json"

    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}

    cfg.setdefault("format", "YYYY-MM-DD")
    cfg.setdefault("template", "")
    current = cfg.get("folder", "")

    if current == expected:
        return False

    cfg["folder"] = expected
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    log(f"daily-notes.json folder changed: '{current}' -> '{expected}'")
    return True


def trigger_obsidian_reload() -> None:
    """Best-effort POST to the Local REST API plugin to reload Obsidian. Silently skip if not reachable."""
    if not REST_KEY_FILE.exists():
        return
    try:
        key = REST_KEY_FILE.read_text().strip()
        if not key:
            return
        req = urllib.request.Request(
            f"{REST_BASE}/commands/app:reload/",
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status == 204:
                log("Obsidian reload triggered")
    except Exception as e:
        log(f"reload skipped (Obsidian/REST not reachable): {type(e).__name__}")


def main() -> int:
    today = date.today()
    year = today.year
    month = today.month

    structure_changed = ensure_year_structure(year)
    config_changed = update_daily_notes_config(year, month)

    if structure_changed or config_changed:
        trigger_obsidian_reload()
        log(f"done — current folder: Journal/{year}/{month:02d}-{MONTHS_DE[month-1]}")
    else:
        log(f"no-op — already on Journal/{year}/{month:02d}-{MONTHS_DE[month-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
