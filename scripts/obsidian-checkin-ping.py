#!/usr/bin/env python3
"""
Obsidian Tracker-Check-In Ping

Wird 4× am Tag (10:00 · 13:00 · 16:00 · 19:00) von launchd getriggert.
Schickt eine macOS-Notification mit der zur Uhrzeit passenden Frage-Sektion,
damit der User die heutige Daily-Note an der richtigen Stelle befüllen kann.

Die Frage-Sektionen selbst stehen schon im Daily-Skeleton (Bootstrap-Script).
Dieses Skript dispatched nur die Erinnerung — kein File-Write, kein Append.

Slot-Erkennung erfolgt automatisch aus der aktuellen Stunde:
  10:00–12:59  → Slot 1 (Morgen)
  13:00–15:59  → Slot 2 (Mittag)
  16:00–18:59  → Slot 3 (Nachmittag)
  19:00–21:00  → Slot 4 (Abend)

CLI: --slot 1|2|3|4 zum manuellen Test überschreibt die Auto-Erkennung.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime

SLOTS = {
    1: {
        "title": "🔔 Morgen-Check",
        "message": "Schlaf · Stimmung · Energie · Top-3 Tagesziele",
        "sound": "Glass",
    },
    2: {
        "title": "🔔 Mittag-Check",
        "message": "Stimmung mittags · Energie · Vormittags-Highlights",
        "sound": "Glass",
    },
    3: {
        "title": "🔔 Nachmittag-Check",
        "message": "Energie · Auf Kurs mit Zielen? · Was bremst?",
        "sound": "Glass",
    },
    4: {
        "title": "🔔 Abend-Check",
        "message": "Stimmung abends · Erkenntnisse · Glückspunkt · Morgen",
        "sound": "Glass",
    },
}


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def detect_slot_from_clock() -> int:
    h = datetime.now().hour
    if h < 13:
        return 1
    if h < 16:
        return 2
    if h < 19:
        return 3
    return 4


def send_notification(slot: int) -> None:
    info = SLOTS[slot]
    # Escape double quotes for AppleScript
    title = info["title"].replace('"', '\\"')
    message = info["message"].replace('"', '\\"')
    script = (
        f'display notification "{message}" '
        f'with title "{title}" '
        f'subtitle "Daily-Note in Obsidian befüllen" '
        f'sound name "{info["sound"]}"'
    )
    res = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True)
    if res.returncode != 0:
        log(f"notification failed: {res.stderr.strip()}")
    else:
        log(f"slot {slot} {info['title']} sent")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--slot", type=int, choices=[1, 2, 3, 4], help="Slot manuell setzen (sonst auto aus Uhrzeit)")
    args = p.parse_args()

    slot = args.slot or detect_slot_from_clock()
    send_notification(slot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
