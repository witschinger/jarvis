#!/usr/bin/env python3
"""
Obsidian Daily Note Bootstrap

Legt täglich um 05:30 (via launchd) das Skeleton der heutigen Daily-Note an.
Pfad: `Journal/<YYYY>/<MM-Monat>/<YYYY-MM-DD>.md`

Verhalten:
  * Idempotent: existiert die Datei bereits, no-op (kein Überschreiben deiner Einträge).
  * Skeleton enthält leere Sektionen für strukturiertes Journaling (Tagesziele,
    Ablauf, Ideen, Highlights, Hindernisse, Erkenntnisse, Asana, Yazio, Abschluss).
  * Yazio-Marker (`<!-- yazio-start ... -->`) im Skeleton → yazio-daily-sync füllt
    den Block um 20:00.
  * Wetter wird beim Bootstrap aus wttr.in geholt + ins Frontmatter geschrieben.
  * Navigation-Wikilinks (Vortag, Folgetag, Monats-MOC) am Ende.

Args:
  --date YYYY-MM-DD   Datum, default: heute
  --force             überschreibt bestehende Datei (Vorsicht)
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

VAULT = Path("/Users/stephan.merk/obsidian/jarvis obsidian")
WEATHER_CITY = "Neumarkt"

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_weather() -> str:
    """Best-effort Wetter-Snapshot beim Bootstrap-Zeitpunkt."""
    try:
        req = urllib.request.Request(
            f"https://wttr.in/{WEATHER_CITY}?format=j1",
            headers={"User-Agent": "curl"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        c = data["current_condition"][0]
        return f"{c['temp_C']}°C, {c['weatherDesc'][0]['value']}"
    except Exception as e:
        log(f"weather fetch failed: {type(e).__name__}: {e}")
        return ""


def link(target: date) -> str:
    """Wikilink-Pfad zu einer Daily-Note."""
    return f"Journal/{target.year}/{target.month:02d}-{MONTHS_DE[target.month-1]}/{target.isoformat()}"


def month_moc_link(target: date) -> str:
    name = MONTHS_DE[target.month - 1]
    return f"Journal/{target.year}/{target.month:02d}-{name}/{target.year}-{target.month:02d} {name}"


def build_skeleton(target: date) -> str:
    weekday = WEEKDAYS_DE[target.weekday()]
    month = MONTHS_DE[target.month - 1]
    weather = fetch_weather()
    prev_d = target - timedelta(days=1)
    next_d = target + timedelta(days=1)
    month_name = MONTHS_DE[target.month - 1]

    return f"""---
date: {target.isoformat()}
type: daily
tags:
  - journal
  - daily
weekday: {weekday}
month: {month}
weather: {weather}
mood:
energy:
sleep_hours:
sleep_quality:
---

# {weekday}, {target.day}. {month} {target.year}

> [!tip] Skeleton automatisch angelegt — Füll-Bereiche im Laufe des Tages befüllen. Der Yazio-Block aktualisiert sich um 20:00 Uhr selbst.

## 🎯 Tagesziele

-
-
-

## 🔔 Tracker-Pings

> [!info] Jarvis fragt 4× am Tag (10:00 · 13:00 · 16:00 · 19:00) — du beantwortest direkt hier in der Daily

### 10:00 — Morgen-Check
- 🛏 Schlaf: _h_ / Qualität _/5_
- 🌅 Stimmung jetzt: _/5_
- ⚡ Energie: _/5_
- 🎯 Top-3 Tagesziele oben befüllt?
- 💭 Was beschäftigt dich heute zuerst?

### 13:00 — Mittag-Check
- ☀ Stimmung mittags: _/5_
- ⚡ Energie: _/5_
- 🔥 Was lief am Vormittag besonders gut?
- 🚧 Was bremst dich gerade?
- 🍴 Mittagessen geplant / gegessen?

### 16:00 — Nachmittag-Check
- ⚡ Energie nachmittags: _/5_
- 🎯 Auf Kurs mit den Tageszielen?
- 💡 Wertvollste Erkenntnis bisher?
- ☕ Pause oder Re-Fokus nötig?
- 📞 Wichtigste Kommunikation heute?

### 19:00 — Abend-Check
- 🌙 Stimmung abends: _/5_
- 📚 Was hast du heute gelernt?
- ✅ Welche Asana-Tasks hast du erledigt?
- 🎯 Top-Prio morgen früh?
- 🍀 Was war heute der Glückspunkt?

## ⏱ Ablauf

- **HH:MM** —

## 💡 Ideen & Austausch

-

## 🔥 Highlights

-

## 🚧 Hindernisse / Frust

-

## 📚 Erkenntnisse

-

## 💑 Partnerschaft & Privat

> Beobachtungen aus dem Alltag — Momente, Konflikte, kleine Aufmerksamkeiten, Themen die mir aufgefallen sind und die ich für mich oder fürs Gespräch festhalten will.

-

## 📞 Kundengespräche

| Wer | Thema | Folge-Action |
|---|---|---|
|  |  |  |

## 💼 Projekt-Updates

-

## ✅ Erledigte Asana-Tasks

_Tasks notieren oder per Wikilink referenzieren._

<!-- yazio-start {target.isoformat()} -->
## 🍴 Yazio — Tagesbilanz

_Wird automatisch um 20:00 Uhr aus Yazio synchronisiert._

<!-- yazio-end -->

## 🌙 Tagesabschluss

- **1-Satz-Tagessumme:**
- **🍀 Glückspunkt des Tages:**
- **Was hat heute gut funktioniert?**
- **Was würde ich morgen anders machen?**
- **Was kommt morgen?**

---

[[{month_moc_link(target)}|⬆ {month_name} {target.year}]] · [[{link(prev_d)}|⬅ {prev_d.isoformat()}]] · [[{link(next_d)}|{next_d.isoformat()} ➡]]
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="Zieldatum YYYY-MM-DD (default: heute)")
    p.add_argument("--force", action="store_true", help="Vorhandene Datei überschreiben")
    args = p.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    month_name = MONTHS_DE[target.month - 1]
    file_path = VAULT / "Journal" / str(target.year) / f"{target.month:02d}-{month_name}" / f"{target.isoformat()}.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if file_path.exists() and not args.force:
        log(f"no-op — existiert bereits: {file_path.relative_to(VAULT)}")
        return 0

    file_path.write_text(build_skeleton(target), encoding="utf-8")
    log(f"created: {file_path.relative_to(VAULT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
