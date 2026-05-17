"""
Jarvis V2 — Voice AI Server
FastAPI backend: receives speech text, thinks with Claude Haiku,
speaks with ElevenLabs, controls browser with Playwright.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time

import anthropic
import httpx
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

ANTHROPIC_API_KEY = config["anthropic_api_key"]
ELEVENLABS_API_KEY = config["elevenlabs_api_key"]
ELEVENLABS_VOICE_ID = config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD")
USER_NAME = config.get("user_name", "Stephan")
USER_ADDRESS = config.get("user_address", "Sir")
USER_ROLE = config.get("user_role", "Tech-Profi")
CITY = config.get("city", "Neumarkt")
TASKS_FILE = config.get("obsidian_inbox_path", "")

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
http = httpx.AsyncClient(timeout=30)

app = FastAPI()

import asana_tools
import browser_tools
import mac_tools
import obsidian_rest
import obsidian_tools
import screen_capture
import worldmonitor_tools

ASANA_WEBHOOK_SECRET_PATH = config.get(
    "asana_webhook_secret_file",
    os.path.join(os.path.dirname(__file__), ".asana_webhook_secret"),
)

# Connected frontend WebSockets — used to broadcast Asana webhook events
CONNECTED_CLIENTS: set[WebSocket] = set()


def get_weather_sync():
    """Fetch raw weather data at startup."""
    import urllib.request
    try:
        req = urllib.request.Request(f"https://wttr.in/{CITY}?format=j1", headers={"User-Agent": "curl"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        c = data["current_condition"][0]
        return {
            "temp": c["temp_C"],
            "feels_like": c["FeelsLikeC"],
            "description": c["weatherDesc"][0]["value"],
            "humidity": c["humidity"],
            "wind_kmh": c["windspeedKmph"],
        }
    except:
        return None


def get_obsidian_tasks_sync() -> list[str]:
    """Read open tasks from the Obsidian inbox (sync)."""
    if not TASKS_FILE:
        return []
    try:
        tasks_path = os.path.join(TASKS_FILE, "Tasks.md")
        with open(tasks_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [l.strip().replace("- [ ]", "").strip() for l in lines if l.strip().startswith("- [ ]")]
    except:
        return []


async def get_asana_tasks_async(limit: int = 10) -> list[str]:
    """Fetch open tasks from Asana, return as flat list of names. Empty if not configured."""
    if not asana_tools.is_configured():
        return []
    try:
        tasks = await asana_tools.list_my_tasks(limit=limit)
        return [t["name"] for t in tasks if t.get("name")]
    except Exception as e:
        print(f"[jarvis] Asana fetch failed: {e}", flush=True)
        return []


async def refresh_data():
    """Refresh weather + merged task list (Obsidian + Asana)."""
    global WEATHER_INFO, TASKS_INFO
    WEATHER_INFO = get_weather_sync()
    obsidian = get_obsidian_tasks_sync()
    asana = await get_asana_tasks_async(limit=10)
    TASKS_INFO = obsidian + asana
    print(f"[jarvis] Wetter: {WEATHER_INFO}", flush=True)
    print(f"[jarvis] Tasks: {len(TASKS_INFO)} geladen (Obsidian: {len(obsidian)}, Asana: {len(asana)})", flush=True)


WEATHER_INFO = ""
TASKS_INFO: list[str] = []


@app.on_event("startup")
async def _on_startup():
    await refresh_data()

# Action parsing
ACTION_PATTERN = re.compile(r'\[ACTION:(\w+)\]\s*(.*?)$', re.DOTALL | re.MULTILINE)

# Speech trigger to bypass the LLM and paste the rest of the utterance directly
# into VS Code. Match: "in Visual Studio Code [eintragen|einfügen|einsetzen|
# schreiben] <text>" — case-insensitive, tolerant of small whitespace/comma
# variations from the Web Speech transcript.
VSCODE_PASTE_TRIGGER = re.compile(
    r"^\s*in\s+(?:visual\s+studio\s+code|vs\s*code)\s*"
    r"(?:eintragen|einf(?:ue|ü)gen|einsetzen|schreiben)?[\s,:.\-]*"
    r"(.+)$",
    re.IGNORECASE | re.DOTALL,
)

conversations: dict[str, list] = {}

def build_system_prompt():
    weather_block = ""
    if WEATHER_INFO:
        w = WEATHER_INFO
        weather_block = f"\nWetter {CITY}: {w['temp']}°C, gefuehlt {w['feels_like']}°C, {w['description']}"

    task_block = ""
    if TASKS_INFO:
        task_block = f"\nOffene Aufgaben ({len(TASKS_INFO)}): " + ", ".join(TASKS_INFO[:5])

    worldmonitor_block = ""
    if worldmonitor_tools.is_configured():
        worldmonitor_block = (
            "\n[ACTION:WM_BRIEF] LAENDERCODE - KI-generiertes Strategie-Briefing zu einem Land von WorldMonitor. "
            "Erwartet ISO 3166-1 alpha-2 (z.B. DE, US, IR, UA). Nutze, wenn der Dienstherr eine Lagebeurteilung, "
            "geopolitische Einschaetzung oder einen Laenderreport will. Wandle Landnamen selbst in den 2-Buchstaben-Code um. "
            "Schreibe einen kurzen Satz davor wie \"Einen Moment, ich rufe das Laenderbriefing ab.\"\n"
            "[ACTION:WM_SCORE] LAENDERCODE - Resilience-Score (0-100) eines Landes von WorldMonitor plus Trend. "
            "Nutze fuer \"Wie stabil/resilient ist X?\". Auch ISO alpha-2. "
            "Schreibe einen kurzen Satz davor wie \"Ich pruefe den aktuellen Resilience-Score.\""
        )

    mac_block = (
        "\n[ACTION:MAC_REMIND] erinnerungstext - Lege eine Erinnerung in der Mac-App Reminders an. Schreibe kurz \"Ich erinnere Sie daran.\""
        "\n[ACTION:MAC_NOTE] notizinhalt - Lege eine neue Notiz in der Mac-App Notes an. Erste Zeile wird zum Titel. Schreibe kurz \"Notiz angelegt.\""
        "\n[ACTION:MAC_OPEN] app_oder_url - Oeffnet eine erlaubte Mac-App oder eine URL. Erlaubte Apps: Finder, Safari, Google Chrome, Notes, Reminders, Calendar, Mail, Messages, Spotify, Music, Obsidian, Slack, Zoom, Microsoft Teams, Visual Studio Code, Terminal, iTerm, 1Password, System Settings, Asana. Schreibe kurz \"Geoeffnet.\""
    )

    obsidian_block = ""
    if obsidian_tools.is_configured():
        obsidian_block = (
            "\n[ACTION:OBSIDIAN_SEARCH] suchbegriff - Volltext-Suche im Obsidian-Vault (Obsidians eigener Tokenizer, falls REST-API laeuft, sonst Dateisystem-grep). Liefert Treffer mit Datei und Kontext."
            "\n[ACTION:OBSIDIAN_DAILY] notizinhalt - Haengt eine Zeile an die heutige Daily Note an (nutzt Obsidian-Periodic-Notes, falls REST-API laeuft). Schreibe kurz \"Ist im Daily eingetragen.\""
            "\n[ACTION:OBSIDIAN_OPEN] notiz_name - Oeffnet eine vorhandene Notiz in der Obsidian-App. Schreibe kurz \"Oeffne in Obsidian.\""
            "\n[ACTION:OBSIDIAN_NEW] titel || inhalt - Legt eine neue Notiz an UND oeffnet sie in Obsidian (URL-Scheme, max ~2 KB Inhalt). Separator ist || zwischen Titel und Inhalt."
            "\n[ACTION:OBSIDIAN_FOCUS] suchbegriff - Oeffnet den Suche-Tab in der Obsidian-App und fuellt ihn vor. Nutze, wenn der Dienstherr in der App selbst suchen will."
            "\n[ACTION:OBSIDIAN_CMD] command_beschreibung - Fuehrt einen Obsidian-Command (Command-Palette-Eintrag) per Fuzzy-Suche aus. Beispiele: \"Daily note öffnen\", \"Linked mentions umschalten\", \"Tag pane öffnen\". Schreibe kurz \"Ausgefuehrt.\""
        )

    return f"""Du bist Jarvis, der KI-Assistent von Tony Stark aus Iron Man. Dein Dienstherr ist {USER_NAME}, ein {USER_ROLE}. Du sprichst ausschliesslich Deutsch. {USER_NAME} moechte mit "{USER_ADDRESS}" angesprochen und gesiezt werden. Nutze "Sie" als Pronomen — FALSCH: "{USER_ADDRESS} planen", RICHTIG: "Sie planen, {USER_ADDRESS}". Dein Ton ist trocken, sarkastisch und britisch-hoeflich - wie ein Butler der alles gesehen hat und trotzdem loyal bleibt. Du machst subtile, trockene Bemerkungen, bist aber niemals respektlos. Wenn {USER_ADDRESS} eine offensichtliche Frage stellt, darfst du mit elegantem Sarkasmus antworten. Du bist hochintelligent, effizient und immer einen Schritt voraus. Halte deine Antworten kurz - maximal 3 Saetze. Du kommentierst fragwuerdige Entscheidungen hoeflich aber spitz.

WICHTIG: Schreibe NIEMALS Regieanweisungen, Emotionen oder Tags in eckigen Klammern wie [sarcastic] [formal] [amused] [dry] oder aehnliches. Dein Sarkasmus muss REIN durch die Wortwahl kommen. Alles was du schreibst wird laut vorgelesen.

Du hast die volle Kontrolle ueber den Browser von {USER_NAME}. Du kannst im Internet suchen, Webseiten oeffnen und den Bildschirm sehen. Wenn {USER_ADDRESS} dich bittet etwas nachzuschauen, zu recherchieren, zu googeln, eine Seite zu oeffnen, oder irgendetwas im Internet zu tun — nutze IMMER eine Aktion. Frag nicht ob du es tun sollst, tu es einfach.

AKTIONEN - Schreibe die passende Aktion ans ENDE deiner Antwort. Der Text VOR der Aktion wird vorgelesen, die Aktion selbst wird still ausgefuehrt.
[ACTION:SEARCH] suchbegriff - Internet durchsuchen und Ergebnisse zusammenfassen
[ACTION:OPEN] url - URL im Browser oeffnen
[ACTION:SCREEN] - Bildschirm ansehen und beschreiben. WICHTIG: Bei SCREEN schreibe NUR die Aktion, KEINEN Text davor. Also NUR "[ACTION:SCREEN]" und sonst nichts.
[ACTION:NEWS] - Aktuelle Weltnachrichten abrufen. Nutze diese Aktion wenn nach News, Nachrichten, was in der Welt passiert, aktuelle Lage oder Weltgeschehen gefragt wird. Schreibe einen kurzen Satz davor wie "Ich schaue nach den aktuellen Nachrichten."
[ACTION:ASANA_LIST] - Offene Standardaufgaben aus dem Asana-Projekt "Jarvis Inbox" abrufen (assignee=me, nur unerledigt). Nutze diese Aktion immer, wenn {USER_ADDRESS} nach Aufgaben, Tasks, ToDos, "was zu erledigen ist" oder dem Stand der Inbox fragt.
[ACTION:ASANA_CREATE] aufgabentitel - Neue Asana-Aufgabe anlegen. Nur ausfuehren, wenn {USER_ADDRESS} ausdruecklich um eine neue Aufgabe bittet. Schreibe einen Bestaetigungssatz davor wie "Ich lege das fuer Sie an."
[ACTION:ASANA_DONE] aufgabentitel - Eine Asana-Aufgabe als erledigt markieren (Fuzzy-Suche per Name). Schreibe einen kurzen Satz davor wie "Ich hake das fuer Sie ab."
{worldmonitor_block}{mac_block}{obsidian_block}

WENN {USER_NAME} "Jarvis activate" sagt:
- Antworte EXAKT mit diesem Satz und nichts weiter: "Hallo, {USER_ADDRESS}, was kann ich für Sie tun?"
- Keine Tageszeit-Variante, kein Wetter, keine Aufgaben, kein Kommentar, keine Kreativitaet.

=== AKTUELLE DATEN ==={weather_block}{task_block}
==="""


def get_system_prompt():
    return build_system_prompt().replace("{time}", time.strftime("%H:%M"))


def extract_action(text: str):
    match = ACTION_PATTERN.search(text)
    if match:
        clean = text[:match.start()].strip()
        return clean, {"type": match.group(1), "payload": match.group(2).strip()}
    return text, None


async def synthesize_speech(text: str) -> bytes:
    if not text.strip():
        return b""

    # Split long text into chunks at sentence boundaries to avoid ElevenLabs cutoff
    chunks = []
    if len(text) > 250:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        current = ""
        for s in sentences:
            if len(current) + len(s) > 250 and current:
                chunks.append(current.strip())
                current = s
            else:
                current = (current + " " + s).strip()
        if current:
            chunks.append(current.strip())
    else:
        chunks = [text]

    audio_parts = []
    for chunk in chunks:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        try:
            resp = await http.post(url, headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }, json={
                "text": chunk,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.85},
            })
            print(f"  TTS chunk status: {resp.status_code}, size: {len(resp.content)}", flush=True)
            if resp.status_code == 200:
                audio_parts.append(resp.content)
            else:
                print(f"  TTS error body: {resp.text[:200]}", flush=True)
        except Exception as e:
            print(f"  TTS EXCEPTION: {e}", flush=True)

    return b"".join(audio_parts)


async def execute_action(action: dict) -> str:
    t = action["type"]
    p = action["payload"]

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\nURL: {result.get('url', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Suche fehlgeschlagen: {result.get('error', '')}"

    elif t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Seite: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"Seite nicht erreichbar: {result.get('error', '')}"

    elif t == "OPEN":
        await browser_tools.open_url(p)
        return f"Geoeffnet: {p}"

    elif t == "SCREEN":
        return await screen_capture.describe_screen(ai)

    elif t == "NEWS":
        result = await browser_tools.fetch_news()
        return result

    elif t == "ASANA_LIST":
        if not asana_tools.is_configured():
            return "Asana ist noch nicht konfiguriert. Persoenlicher Token fehlt in der config."
        try:
            tasks = await asana_tools.list_my_tasks(limit=20)
        except Exception as e:
            return f"Asana-Abruf fehlgeschlagen: {e}"
        if not tasks:
            return "Keine offenen Aufgaben in Asana."
        lines = []
        for task in tasks:
            line = f"- {task['name']}"
            if task.get("due_on"):
                line += f" (faellig: {task['due_on']})"
            projects = task.get("projects") or []
            if projects:
                line += f" [{projects[0].get('name', '')}]"
            lines.append(line)
        # Refresh cached briefing data so the next "activate" matches reality
        global TASKS_INFO
        TASKS_INFO = [t["name"] for t in tasks]
        return f"Offene Asana-Aufgaben ({len(tasks)}):\n" + "\n".join(lines)

    elif t == "ASANA_CREATE":
        if not asana_tools.is_configured():
            return "Asana ist noch nicht konfiguriert."
        if not p:
            return "Kein Aufgabentitel angegeben."
        try:
            task = await asana_tools.create_task(p)
            return f"Aufgabe in Asana angelegt: {task.get('name')} (gid {task.get('gid')})."
        except Exception as e:
            return f"Aufgabe konnte nicht angelegt werden: {e}"

    elif t == "ASANA_DONE":
        if not asana_tools.is_configured():
            return "Asana ist noch nicht konfiguriert."
        if not p:
            return "Kein Aufgabentitel angegeben."
        try:
            found = await asana_tools.find_task_by_name(p)
            if not found:
                return f"Keine Asana-Aufgabe gefunden, die zu '{p}' passt."
            await asana_tools.complete_task(found["gid"])
            return f"Aufgabe abgehakt: {found['name']}."
        except Exception as e:
            return f"Konnte nicht abhaken: {e}"

    elif t == "WM_BRIEF":
        if not worldmonitor_tools.is_configured():
            return "WorldMonitor ist noch nicht konfiguriert. API-Key fehlt in der config."
        if not p:
            return "Kein Laendercode angegeben."
        try:
            data = await worldmonitor_tools.get_country_brief(p.split()[0])
        except Exception as e:
            return f"WorldMonitor-Briefing fehlgeschlagen: {e}"
        return (
            f"Land: {data.get('countryName', '?')} ({data.get('countryCode', '?')})\n"
            f"Modell: {data.get('model', '?')}\n\n"
            f"{data.get('brief', '(kein Briefing geliefert)')}"
        )

    elif t == "WM_SCORE":
        if not worldmonitor_tools.is_configured():
            return "WorldMonitor ist noch nicht konfiguriert."
        if not p:
            return "Kein Laendercode angegeben."
        try:
            data = await worldmonitor_tools.get_resilience_score(p.split()[0])
        except Exception as e:
            return f"WorldMonitor-Score fehlgeschlagen: {e}"
        interval = data.get("scoreInterval") or {}
        domains = data.get("domains") or []
        top_domains = ", ".join(
            f"{d.get('name')}: {d.get('score')}" for d in domains[:6]
        )
        return (
            f"Resilience-Score {data.get('countryCode', '?')}: "
            f"{data.get('overallScore')} ({data.get('level')}), "
            f"Trend: {data.get('trend')}, 30-Tage-Delta: {data.get('change30d')}, "
            f"Konfidenzband: {interval.get('lower')}–{interval.get('upper')}, "
            f"Datenstand: {data.get('dataVersion')}.\n"
            f"Domains: {top_domains}"
        )

    elif t == "MAC_REMIND":
        if not p:
            return "Kein Erinnerungstext angegeben."
        try:
            r = await mac_tools.create_reminder(p)
            return f"Erinnerung angelegt: {r.get('title')}."
        except Exception as e:
            return f"Erinnerung konnte nicht angelegt werden: {e}"

    elif t == "MAC_NOTE":
        if not p:
            return "Kein Notiztext angegeben."
        try:
            r = await mac_tools.create_note(p)
            return f"Notiz angelegt: {r.get('title')}."
        except Exception as e:
            return f"Notiz konnte nicht angelegt werden: {e}"

    elif t == "MAC_OPEN":
        if not p:
            return "Nichts zum Oeffnen angegeben."
        try:
            r = await mac_tools.open_app_or_url(p)
            kind = r.get("kind")
            return f"Geoeffnet ({kind}): {r.get('opened')}."
        except PermissionError as e:
            return str(e)
        except Exception as e:
            return f"Konnte nicht oeffnen: {e}"

    elif t == "OBSIDIAN_SEARCH":
        if not obsidian_tools.is_configured():
            return "Obsidian-Vault ist nicht konfiguriert."
        if not p:
            return "Kein Suchbegriff."
        # Prefer REST search when the plugin is reachable; fall back to filesystem grep.
        if obsidian_rest.is_available():
            try:
                hits = await obsidian_rest.search_simple(p, context_length=120)
            except Exception as e:
                return f"Vault-Suche (REST) fehlgeschlagen: {e}"
            if not hits:
                return f"Keine Treffer fuer '{p}'."
            lines = [f"{len(hits)} Treffer fuer '{p}' (Obsidian-Tokenizer):"]
            for h in hits[:15]:
                fn = h.get("filename") or h.get("path") or "?"
                ms = h.get("matches") or []
                snippet = ""
                if ms:
                    snippet = (ms[0].get("context") or "").strip().replace("\n", " ")[:200]
                lines.append(f"- {fn}  {snippet}")
            return "\n".join(lines)
        try:
            hits = obsidian_tools.vault_search(p, limit=15)
        except Exception as e:
            return f"Vault-Suche fehlgeschlagen: {e}"
        if not hits:
            return f"Keine Treffer fuer '{p}'."
        lines = [f"{len(hits)} Treffer fuer '{p}':"]
        for h in hits:
            lines.append(f"- {h['file']}:{h['line']}  {h['snippet']}")
        return "\n".join(lines)

    elif t == "OBSIDIAN_DAILY":
        if not obsidian_tools.is_configured():
            return "Obsidian-Vault ist nicht konfiguriert."
        if not p:
            return "Kein Inhalt fuer die Daily Note."
        # Prefer Periodic-Notes-aware REST append; fall back to direct filesystem write.
        if obsidian_rest.is_available():
            try:
                ts = time.strftime("%H:%M")
                await obsidian_rest.append_to_periodic("daily", f"- {ts} — {p}\n")
                return f"In Daily eingetragen (via Obsidian): {p[:80]}"
            except Exception as e:
                # Fall through to filesystem path on error
                print(f"  [obsidian] REST daily append failed, falling back: {e}", flush=True)
        try:
            r = obsidian_tools.append_to_daily_note(p)
        except Exception as e:
            return f"Daily-Note-Eintrag fehlgeschlagen: {e}"
        return f"In Daily eingetragen: {r.get('appended')}"

    elif t == "OBSIDIAN_CMD":
        if not obsidian_rest.is_available():
            return "Obsidian-REST-API nicht erreichbar — Plugin nicht aktiviert oder Obsidian zu."
        if not p:
            return "Kein Command angegeben."
        try:
            r = await obsidian_rest.fuzzy_run_command(p)
            return f"Obsidian-Command ausgefuehrt: {r['matched_name']}."
        except Exception as e:
            return f"Command konnte nicht ausgefuehrt werden: {e}"

    elif t == "OBSIDIAN_OPEN":
        if not obsidian_tools.is_configured():
            return "Obsidian-Vault ist nicht konfiguriert."
        if not p:
            return "Kein Notizname angegeben."
        try:
            r = obsidian_tools.open_note(p)
            return f"Geoeffnet in Obsidian: {r['opened']}."
        except Exception as e:
            return f"Konnte Notiz nicht oeffnen: {e}"

    elif t == "OBSIDIAN_NEW":
        if not obsidian_tools.is_configured():
            return "Obsidian-Vault ist nicht konfiguriert."
        if not p:
            return "Kein Titel/Inhalt angegeben."
        # Split on first '||' so the title is everything before, content after
        if "||" in p:
            title, content = p.split("||", 1)
        else:
            title, content = p, ""
        try:
            r = obsidian_tools.create_note_via_url(title.strip(), content.strip())
            return f"Notiz angelegt: {r['name']}."
        except Exception as e:
            return f"Konnte Notiz nicht anlegen: {e}"

    elif t == "OBSIDIAN_FOCUS":
        if not obsidian_tools.is_configured():
            return "Obsidian-Vault ist nicht konfiguriert."
        if not p:
            return "Kein Suchbegriff."
        try:
            obsidian_tools.focus_search(p)
            return f"Suche in Obsidian geoeffnet fuer: {p}."
        except Exception as e:
            return f"Konnte Suche nicht oeffnen: {e}"

    return ""


async def process_message(session_id: str, user_text: str, ws: WebSocket):
    """Process message and send responses via WebSocket."""
    if session_id not in conversations:
        conversations[session_id] = []

    # Voice routing: "in Visual Studio Code ..." pastes the rest of the
    # utterance directly into VS Code, no LLM/TTS round-trip.
    m = VSCODE_PASTE_TRIGGER.match(user_text)
    if m:
        payload = m.group(1).strip().rstrip(".,;")
        print(f"  VSCode-paste: {payload[:120]}", flush=True)
        try:
            await mac_tools.paste_into_app("visual studio code", payload)
            confirm = "Eingefügt."
        except Exception as e:
            print(f"  VSCode-paste error: {e}", flush=True)
            confirm = f"Einfügen fehlgeschlagen, {USER_ADDRESS}."
        audio = await synthesize_speech(confirm)
        await ws.send_json({
            "type": "response",
            "text": confirm,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        })
        return

    # Refresh weather + tasks on activate
    if "activate" in user_text.lower():
        await refresh_data()

    conversations[session_id].append({"role": "user", "content": user_text})
    history = conversations[session_id][-16:]

    # LLM call
    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=get_system_prompt(),
        messages=history,
    )
    reply = response.content[0].text
    print(f"  LLM raw: {reply[:200]}", flush=True)
    spoken_text, action = extract_action(reply)

    # Speak the main response immediately
    if spoken_text:
        audio = await synthesize_speech(spoken_text)
        print(f"  Jarvis: {spoken_text[:80]}", flush=True)
        print(f"  Audio bytes: {len(audio)}", flush=True)
        conversations[session_id].append({"role": "assistant", "content": spoken_text})
        await ws.send_json({
            "type": "response",
            "text": spoken_text,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        })

    # Execute action if any
    if action:
        print(f"  Action: {action['type']} -> {action['payload'][:100]}", flush=True)

        # Quick voice feedback for SCREEN so user knows Jarvis is working
        if action["type"] == "SCREEN":
            hint = "Lassen Sie mich einen Blick auf Ihren Bildschirm werfen."
            hint_audio = await synthesize_speech(hint)
            await ws.send_json({
                "type": "response",
                "text": hint,
                "audio": base64.b64encode(hint_audio).decode("utf-8") if hint_audio else "",
            })

        try:
            action_result = await execute_action(action)
            print(f"  Result: {action_result}", flush=True)
        except Exception as e:
            print(f"  Action error: {e}", flush=True)
            action_result = f"Fehler: {e}"

        if action["type"] == "OPEN":
            # Just opened browser, nothing to summarize
            return

        # SEARCH, BROWSE, SCREEN — summarize results
        if action_result and "fehlgeschlagen" not in action_result:
            summary_resp = await ai.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                system=f"Du bist Jarvis. Fasse die folgenden Informationen KURZ auf Deutsch zusammen, maximal 3 Saetze, im Jarvis-Stil. Sprich den Nutzer als {USER_ADDRESS} an. KEINE Tags in eckigen Klammern. KEINE ACTION-Tags.",
                messages=[{"role": "user", "content": f"Fasse zusammen:\n\n{action_result}"}],
            )
            summary = summary_resp.content[0].text
            summary, _ = extract_action(summary)
        else:
            summary = f"Das hat leider nicht funktioniert, {USER_ADDRESS}."

        audio2 = await synthesize_speech(summary)
        conversations[session_id].append({"role": "assistant", "content": summary})
        await ws.send_json({
            "type": "response",
            "text": summary,
            "audio": base64.b64encode(audio2).decode("utf-8") if audio2 else "",
        })


async def _cancel(task):
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    session_id = str(id(ws))
    current_task: asyncio.Task | None = None
    CONNECTED_CLIENTS.add(ws)
    print(f"[jarvis] Client connected ({len(CONNECTED_CLIENTS)} total)", flush=True)

    try:
        while True:
            data = await ws.receive_json()

            # Explicit barge-in signal from client — abort in-flight turn, keep socket open
            if data.get("type") == "interrupt":
                if current_task and not current_task.done():
                    print(f"  [interrupt] cancelling in-flight turn", flush=True)
                    await _cancel(current_task)
                current_task = None
                continue

            user_text = data.get("text", "").strip()
            if not user_text:
                continue

            # New user turn cancels any still-running previous turn so its
            # remaining audio chunks never reach the client.
            if current_task and not current_task.done():
                print(f"  [new turn] cancelling previous", flush=True)
                await _cancel(current_task)

            print(f"  You:    {user_text}", flush=True)
            current_task = asyncio.create_task(process_message(session_id, user_text, ws))

    except WebSocketDisconnect:
        await _cancel(current_task)
        conversations.pop(session_id, None)
    finally:
        CONNECTED_CLIENTS.discard(ws)


async def broadcast_status(text: str) -> None:
    """Push a status banner to every connected frontend (used by Asana webhooks)."""
    payload = {"type": "status", "text": text}
    dead: list[WebSocket] = []
    for client in list(CONNECTED_CLIENTS):
        try:
            await client.send_json(payload)
        except Exception:
            dead.append(client)
    for d in dead:
        CONNECTED_CLIENTS.discard(d)


# --- Asana webhook receiver ---------------------------------------------------

def _load_webhook_secret() -> str | None:
    try:
        with open(ASANA_WEBHOOK_SECRET_PATH, "r") as f:
            secret = f.read().strip()
        return secret or None
    except FileNotFoundError:
        return None


def _save_webhook_secret(secret: str) -> None:
    with open(ASANA_WEBHOOK_SECRET_PATH, "w") as f:
        f.write(secret)
    try:
        os.chmod(ASANA_WEBHOOK_SECRET_PATH, 0o600)
    except OSError:
        pass


@app.post("/asana/webhook")
async def asana_webhook(
    request: Request,
    x_hook_secret: str | None = Header(default=None),
    x_hook_signature: str | None = Header(default=None),
):
    """
    Asana webhook endpoint.
      - Handshake: first request carries X-Hook-Secret. We persist it and echo it
        back in the response header so Asana confirms the subscription.
      - Events: subsequent requests carry X-Hook-Signature (HMAC-SHA256 of body
        using the persisted secret). We verify, then refresh data and broadcast.
    """
    body = await request.body()

    # Handshake — Asana sends this once after webhook creation
    if x_hook_secret:
        _save_webhook_secret(x_hook_secret)
        print(f"[asana] webhook handshake — secret saved ({len(x_hook_secret)} chars)", flush=True)
        return Response(
            status_code=200,
            headers={"X-Hook-Secret": x_hook_secret},
        )

    # Event delivery — verify signature
    secret = _load_webhook_secret()
    if not secret:
        raise HTTPException(status_code=412, detail="Webhook secret not provisioned")
    if not x_hook_signature:
        raise HTTPException(status_code=401, detail="Missing X-Hook-Signature")

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_hook_signature):
        print(f"[asana] webhook signature mismatch — dropping payload", flush=True)
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {}
    events = payload.get("events", []) or []
    print(f"[asana] webhook event batch: {len(events)} events", flush=True)

    if events:
        # Refresh in-memory task list and notify connected frontends
        await refresh_data()
        summary = _summarize_events(events)
        if summary:
            await broadcast_status(f"Asana: {summary}")
        # Fire-and-forget so the webhook ACK returns within Asana's timeout
        asyncio.create_task(announce_asana_events(events))

    return PlainTextResponse("ok")


def _summarize_events(events: list[dict]) -> str:
    """Compress an event batch into a one-line human summary."""
    actions: dict[str, int] = {}
    for e in events:
        key = f"{e.get('action', '?')} {e.get('resource', {}).get('resource_type', '?')}"
        actions[key] = actions.get(key, 0) + 1
    return ", ".join(f"{n} x {k}" for k, n in actions.items())


# Dedup recently-announced task gids so a redelivered event doesn't re-speak.
# Bounded; oldest entries pruned by insertion order.
_ANNOUNCED_GIDS: list[str] = []
_ANNOUNCED_LIMIT = 200


def _remember_announced(gid: str) -> bool:
    """Return True if gid is new (and record it). False if already announced recently."""
    if gid in _ANNOUNCED_GIDS:
        return False
    _ANNOUNCED_GIDS.append(gid)
    if len(_ANNOUNCED_GIDS) > _ANNOUNCED_LIMIT:
        del _ANNOUNCED_GIDS[: len(_ANNOUNCED_GIDS) - _ANNOUNCED_LIMIT]
    return True


async def announce_asana_events(events: list[dict]) -> None:
    """
    Generate a short German TTS announcement for actionable Asana events and push
    it to every connected frontend so Jarvis says it out loud.

    Currently announces only newly-added tasks. Each task gid is announced at
    most once across redelivered batches. Skipped silently if no clients are
    connected.
    """
    if not CONNECTED_CLIENTS:
        return

    added = [
        e for e in events
        if e.get("action") == "added"
        and (e.get("resource") or {}).get("resource_type") == "task"
    ]
    if not added:
        return

    names: list[str] = []
    for evt in added[:5]:
        gid = (evt.get("resource") or {}).get("gid")
        if not gid or not _remember_announced(gid):
            continue
        try:
            task = await asana_tools.get_task(gid, opt_fields="name,assignee.name")
        except Exception as e:
            print(f"[asana] could not fetch task {gid} for announcement: {e}", flush=True)
            continue
        nm = (task.get("name") or "").strip()
        if nm:
            names.append(nm)

    if not names:
        return

    if len(names) == 1:
        text = f"{USER_ADDRESS}, neue Aufgabe in Asana: {names[0]}."
    else:
        joined = ", ".join(names[:-1]) + f" und {names[-1]}"
        text = f"{USER_ADDRESS}, {len(names)} neue Asana-Aufgaben: {joined}."

    print(f"[asana] announcing: {text}", flush=True)
    audio = await synthesize_speech(text)
    payload = {
        "type": "response",
        "text": text,
        "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
    }
    dead: list[WebSocket] = []
    for client in list(CONNECTED_CLIENTS):
        try:
            await client.send_json(payload)
        except Exception:
            dead.append(client)
    for d in dead:
        CONNECTED_CLIENTS.discard(d)


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend")), name="static")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("=" * 50, flush=True)
    print("  J.A.R.V.I.S. V2 Server", flush=True)
    print(f"  http://localhost:8340", flush=True)
    print("=" * 50, flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8340)
