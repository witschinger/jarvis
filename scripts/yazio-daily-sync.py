#!/usr/bin/env python3
"""
Yazio → Obsidian Daily Sync

Holt täglich um 20:00 (via launchd) die Yazio-Daten des **Vortages** und trägt
sie in die zugehörige Daily-Note im Vault ein. Pfad:
`Journal/<YYYY>/<MM-Monat>/<YYYY-MM-DD>.md`

Eigenschaften:
  * Idempotent: ein klar abgegrenzter HTML-Kommentar-Block markiert den
    Yazio-Bereich (`<!-- yazio-start ... -->` bis `<!-- yazio-end -->`).
    Mehrfach-Runs überschreiben nur diesen Block.
  * Datei wird neu angelegt falls nicht vorhanden (inkl. Frontmatter + Heading).
  * Manuelle Inhalte außerhalb des Markers bleiben unangetastet.
  * Argument `--date YYYY-MM-DD` für Ad-hoc-Resync eines beliebigen Datums.

Auth: liest YAZIO_USERNAME/YAZIO_PASSWORD aus `~/.claude.json` →
`mcpServers.yazio.env` (dort ohnehin gespeichert für den Claude-Code MCP).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

VAULT = Path("/Users/stephan.merk/obsidian/jarvis obsidian")
CLAUDE_CONFIG = Path("/Users/stephan.merk/.claude.json")
NODE_BIN = "/opt/homebrew/opt/node@22/bin"
NPX = f"{NODE_BIN}/npx"

MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]
WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_yazio_creds() -> tuple[str, str]:
    cfg = json.loads(CLAUDE_CONFIG.read_text(encoding="utf-8"))
    env = cfg.get("mcpServers", {}).get("yazio", {}).get("env", {})
    user = env.get("YAZIO_USERNAME")
    pw = env.get("YAZIO_PASSWORD")
    if not user or not pw:
        raise RuntimeError("YAZIO_USERNAME/PASSWORD nicht in ~/.claude.json gefunden.")
    return user, pw


def _run_mcp(requests: list[dict], timeout: int = 25) -> dict[int, dict]:
    """Spawn yazio-mcp, send the given JSON-RPC requests over stdin, return responses keyed by id."""
    user, pw = load_yazio_creds()
    env = {
        **os.environ,
        "YAZIO_USERNAME": user,
        "YAZIO_PASSWORD": pw,
        "PATH": f"{NODE_BIN}:/usr/bin:/bin:" + os.environ.get("PATH", ""),
    }
    proc = subprocess.Popen(
        [NPX, "-y", "yazio-mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True,
    )
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    try:
        out, _ = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise

    responses: dict[int, dict] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and "id" in r and isinstance(r["id"], int):
            responses[r["id"]] = r
    return responses


def _extract_json_from_tool_text(resp: dict) -> dict | None:
    """Yazio MCP wraps JSON in human-readable text. Extract the JSON object."""
    if not resp or "result" not in resp:
        return None
    content = resp["result"].get("content") or []
    if not content:
        return None
    text = content[0].get("text", "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def fetch_yazio_day(target: date) -> dict:
    """Fetch daily_summary + consumed_items + water + exercises + weight for a date."""
    date_str = target.isoformat()
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sync", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_user_daily_summary", "arguments": {"date": date_str}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_user_consumed_items", "arguments": {"date": date_str}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "get_user_water_intake", "arguments": {"date": date_str}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "get_user_exercises", "arguments": {"date": date_str}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "get_user_weight", "arguments": {}}},
    ]
    rs = _run_mcp(reqs, timeout=30)
    return {
        "daily_summary": _extract_json_from_tool_text(rs.get(2)),
        "consumed_items": _extract_json_from_tool_text(rs.get(3)),
        "water_intake": _extract_json_from_tool_text(rs.get(4)),
        "exercises": _extract_json_from_tool_text(rs.get(5)),
        "weight": _extract_json_from_tool_text(rs.get(6)),
    }


def fetch_product_names(product_ids: list[str]) -> dict[str, str]:
    if not product_ids:
        return {}
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "sync", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for i, pid in enumerate(product_ids, start=10):
        reqs.append({
            "jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": "get_product", "arguments": {"id": pid}},
        })
    rs = _run_mcp(reqs, timeout=30)
    names: dict[str, str] = {}
    for i, pid in enumerate(product_ids, start=10):
        data = _extract_json_from_tool_text(rs.get(i))
        if data:
            names[pid] = data.get("name", "?").strip()
    return names


def build_section(target: date, data: dict, names: dict[str, str]) -> str:
    summary = data.get("daily_summary") or {}
    items_data = data.get("consumed_items") or {}
    products = items_data.get("products") or []

    meals = summary.get("meals", {})
    goals = summary.get("goals", {})

    def meal_kcal(slot):
        return (meals.get(slot, {}).get("nutrients", {}) or {}).get("energy.energy", 0) or 0

    total_kcal = sum(meal_kcal(s) for s in ["breakfast", "lunch", "dinner", "snack"])
    total_protein = sum((meals.get(s, {}).get("nutrients", {}) or {}).get("nutrient.protein", 0) or 0
                        for s in ["breakfast", "lunch", "dinner", "snack"])
    total_fat = sum((meals.get(s, {}).get("nutrients", {}) or {}).get("nutrient.fat", 0) or 0
                    for s in ["breakfast", "lunch", "dinner", "snack"])
    total_carb = sum((meals.get(s, {}).get("nutrients", {}) or {}).get("nutrient.carb", 0) or 0
                     for s in ["breakfast", "lunch", "dinner", "snack"])

    water = summary.get("water_intake", 0) or 0
    water_goal = goals.get("water", 0) or 0
    steps = summary.get("steps", 0) or 0
    step_goal = goals.get("activity.step", 0) or 0
    energy_goal = goals.get("energy.energy", 0) or 0
    user_block = summary.get("user", {}) or {}
    weight = user_block.get("current_weight", 0) or 0
    weight_goal = goals.get("bodyvalue.weight", 0) or 0

    def fmt(v, suffix="", digits=0):
        try:
            return f"{float(v):,.{digits}f}".replace(",", ".") + suffix
        except (TypeError, ValueError):
            return "—"

    lines: list[str] = []
    lines.append("## 🍴 Yazio — Tagesbilanz")
    lines.append("")
    lines.append("### Bilanz")
    lines.append("")
    lines.append("| | Aktuell | Ziel |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Kalorien | {fmt(total_kcal, ' kcal')} | {fmt(energy_goal, ' kcal')} |")
    lines.append(f"| Protein | {fmt(total_protein, ' g', 1)} | {fmt(goals.get('nutrient.protein', 0), ' g', 1)} |")
    lines.append(f"| Fett | {fmt(total_fat, ' g', 1)} | {fmt(goals.get('nutrient.fat', 0), ' g', 1)} |")
    lines.append(f"| Carbs | {fmt(total_carb, ' g', 1)} | {fmt(goals.get('nutrient.carb', 0), ' g', 1)} |")
    lines.append(f"| Wasser | {fmt(water, ' ml')} | {fmt(water_goal, ' ml')} |")
    lines.append(f"| Schritte | {fmt(steps)} | {fmt(step_goal)} |")
    lines.append(f"| Gewicht | {fmt(weight, ' kg', 1)} | {fmt(weight_goal, ' kg')} |")
    lines.append("")

    if products:
        lines.append("### Mahlzeiten")
        by_slot: dict[str, list[dict]] = {}
        for it in products:
            by_slot.setdefault(it.get("daytime", "snack"), []).append(it)

        slot_labels = {
            "breakfast": "🍳 Frühstück",
            "lunch": "🥗 Mittag",
            "dinner": "🍗 Abendessen",
            "snack": "🍫 Snack",
        }
        for slot in ["breakfast", "lunch", "dinner", "snack"]:
            entries = by_slot.get(slot, [])
            if not entries:
                continue
            kcal = meal_kcal(slot)
            lines.append("")
            lines.append(f"#### {slot_labels[slot]} — {fmt(kcal, ' kcal')}")
            for it in sorted(entries, key=lambda x: x.get("date", "")):
                pid = it.get("product_id", "")
                name = names.get(pid, "(unbekannt)")
                amount = it.get("amount", 0)
                serving = it.get("serving")
                qty = it.get("serving_quantity")
                if serving and qty:
                    lines.append(f"- {fmt(amount, 'g', 1)} {name} ({fmt(qty, ' ' + serving, 1)})")
                else:
                    lines.append(f"- {fmt(amount, 'g', 1)} {name}")
    else:
        lines.append("### Mahlzeiten")
        lines.append("")
        lines.append("_Keine Einträge für diesen Tag._")

    lines.append("")
    lines.append(f"<sub>Auto-synchronisiert: {datetime.now().strftime('%Y-%m-%d %H:%M')} via yazio-mcp</sub>")
    return "\n".join(lines)


YAZIO_BLOCK_RE = re.compile(r"<!-- yazio-start[^>]*-->.*?<!-- yazio-end -->", re.DOTALL)


def upsert_in_file(file_path: Path, body: str, target: date) -> str:
    start = f"<!-- yazio-start {target.isoformat()} -->"
    end = "<!-- yazio-end -->"
    section = f"{start}\n{body}\n{end}"

    if not file_path.exists():
        weekday = WEEKDAYS_DE[target.weekday()]
        month_name = MONTHS_DE[target.month - 1]
        fm = (
            "---\n"
            f"date: {target.isoformat()}\n"
            "type: daily\n"
            "tags:\n"
            "  - journal\n"
            "  - daily\n"
            "  - yazio\n"
            "---\n\n"
            f"# {weekday}, {target.day}. {month_name} {target.year}\n\n"
            f"{section}\n"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(fm, encoding="utf-8")
        return "created"

    content = file_path.read_text(encoding="utf-8")
    if YAZIO_BLOCK_RE.search(content):
        new_content = YAZIO_BLOCK_RE.sub(section, content)
        file_path.write_text(new_content, encoding="utf-8")
        return "updated"
    if not content.endswith("\n"):
        content += "\n"
    content += "\n" + section + "\n"
    file_path.write_text(content, encoding="utf-8")
    return "appended"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="Target date YYYY-MM-DD; default: yesterday")
    args = p.parse_args()

    target = date.fromisoformat(args.date) if args.date else (date.today() - timedelta(days=1))
    log(f"Yazio sync target: {target.isoformat()}")

    try:
        data = fetch_yazio_day(target)
    except Exception as e:
        log(f"FETCH FAILED: {type(e).__name__}: {e}")
        return 2

    items_data = data.get("consumed_items") or {}
    products = items_data.get("products") or []
    pids = list({p.get("product_id") for p in products if p.get("product_id")})
    log(f"products to resolve: {len(pids)}")

    try:
        names = fetch_product_names(pids) if pids else {}
    except Exception as e:
        log(f"PRODUCT NAME FETCH FAILED: {type(e).__name__}: {e}")
        names = {}

    body = build_section(target, data, names)

    month_name = MONTHS_DE[target.month - 1]
    file_path = VAULT / "Journal" / str(target.year) / f"{target.month:02d}-{month_name}" / f"{target.isoformat()}.md"
    status = upsert_in_file(file_path, body, target)
    log(f"-> {file_path.relative_to(VAULT)}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
