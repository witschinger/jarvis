# CLAUDE.md

**Jarvis** — voice AI assistant. Fork von [witschinger/jarvis](https://github.com/witschinger/jarvis), nach **macOS** portiert und um eine Asana-Integration (REST + Webhooks mit auto-rotierendem Tunnel), Obsidian-Integration (Filesystem + URL-Scheme + Local REST API Plugin), Mac-System-Bridge (Reminders/Notes/Open via AppleScript) und WorldMonitor-Skeleton (Pro-Tier-API, derzeit ohne Key) erweitert.

Setup ist abgeschlossen. Dieses Dokument ist die Referenz für **laufende Arbeit**; Setup-Anweisungen aus `SETUP.md` greifen nur, wenn der Vault neu aufgesetzt wird.

## Quickstart

```bash
cd /Users/stephan.merk/Python_Code/jarvis
python3 server.py     # uvicorn auf 0.0.0.0:8340
# Browser: http://localhost:8340  (Chrome, einmal klicken um Audio zu entsperren)
```

Stoppen: `lsof -ti tcp:8340 | xargs kill`

## Architektur

```
Chrome (Web Speech API) → WebSocket /ws → FastAPI server.py
                                                ↓
                              Claude Haiku (claude-haiku-4-5-20251001)
                                                ↓
              ┌────────────┬───────────────┬────┴────┬────────────┬──────────────┐
              ↓            ↓               ↓         ↓            ↓              ↓
       ElevenLabs    Playwright      Asana REST   Obsidian     Mac AppleScript  WorldMonitor
       TTS (b64)     (search/news    + Webhooks   (filesystem  (Reminders/      (skeleton)
                     /screen)        (HMAC)       + URL-scheme Notes/Open)
                                                  + REST plug)
              ↓
       WebSocket → Browser → Audio + Status-Banner
```

Daneben: launchd-Daemon `com.user.jarvis.asana`, der den Pinggy-Tunnel + das Asana-Webhook alle ~55 Min rotiert.

## Workspace Structure

```
jarvis/
├── server.py                         # FastAPI: Voice-Actions, Webhook-Receiver, Broadcast
├── browser_tools.py                  # Playwright: search_and_read / visit / fetch_news / open_url
├── screen_capture.py                 # PIL ImageGrab + Claude Vision
├── mac_tools.py                      # osascript: Reminders / Notes / Open (Whitelist)
├── obsidian_tools.py                 # Vault FS (search, daily) + URL-Scheme (open/new/focus)
├── obsidian_rest.py                  # Local REST API plugin client (commands, search, periodic)
├── asana_tools.py                    # Asana REST + Webhook management
├── worldmonitor_tools.py             # WorldMonitor REST (Key fehlt → Actions ausgeblendet)
├── config.json                       # gitignored
├── config.example.json
├── requirements.txt
├── .asana_webhook_secret             # HMAC, mode 600, gitignored
├── .asana_webhook_state.json         # Daemon-State, gitignored
├── .local_rest_api_key               # Obsidian Plugin Key, mode 600, gitignored
├── frontend/
│   ├── index.html
│   ├── main.js                       # Speech, WebSocket, Audio + Barge-In (interim results)
│   └── style.css
└── scripts/
    ├── clap-trigger.py               # Doppelklatschen (plattform-agnostisch)
    ├── launch-session.sh             # macOS Apps + Server starten + Fenster snappen
    ├── start-asana-tunnel.sh         # Manueller cloudflared-Wrapper
    ├── register-asana-webhook.py     # Webhook add/list/delete + --list-projects
    ├── asana-tunnel-daemon.py        # 24/7 rotating Pinggy + Webhook-Rebinder
    ├── com.user.jarvis.asana.plist   # launchd Service (Asana-Daemon)
    └── com.user.jarvis.clap.plist    # launchd Service (Clap-Trigger)
```

## Voice-Action-Inventar

Alle Actions sind dynamisch im System-Prompt gegated. Module mit fehlender Config (z.B. WorldMonitor ohne Key, Obsidian ohne Vault-Pfad) verschwinden automatisch. Quelle der Wahrheit ist `build_system_prompt()` in [server.py](server.py).

| Gruppe | Actions | Aktivierung |
|---|---|---|
| **Browser** | `SEARCH`, `OPEN`, `SCREEN`, `NEWS` | immer aktiv |
| **Asana** | `ASANA_LIST`, `ASANA_CREATE`, `ASANA_DONE` | `asana_pat` in config |
| **Mac** | `MAC_REMIND`, `MAC_NOTE`, `MAC_OPEN` | immer aktiv |
| **Obsidian** | `OBSIDIAN_SEARCH`, `OBSIDIAN_DAILY`, `OBSIDIAN_OPEN`, `OBSIDIAN_NEW`, `OBSIDIAN_FOCUS`, `OBSIDIAN_CMD` | `obsidian_vault_path` in config; CMD braucht REST-Plugin reachable |
| **WorldMonitor** | `WM_BRIEF`, `WM_SCORE` | `worldmonitor_api_key` in config (derzeit leer) |

`OBSIDIAN_SEARCH` und `OBSIDIAN_DAILY` haben Failover: REST-Plugin wenn erreichbar, sonst Dateisystem.

## Integrations-spezifische Gotchas

### Mac (mac_tools.py)
- **Notes-AppleScript**: kein `at default folder`-Token. Klausel weglassen → Notes legt in Default-Folder des Default-Accounts an.
- **Permission-Dialoge**: macOS fragt beim **ersten** Reminders/Notes-Aufruf den aufrufenden Prozess (Terminal/VS Code/Python framework). System Settings → Privacy → Automation. Einmal erlauben, danach permanent.
- **APP_ALLOWLIST**: neue Apps in `mac_tools.py:APP_ALLOWLIST` ergänzen.

### Asana (asana_tools.py + scripts/)
- **Token-Format**: PAT = `1/<id>:<hex>`. Service-Token = `2/<id>/<id>:<hex>`. Beide als Bearer akzeptiert.
- **Workspace-scoped Webhooks** lehnen `task`-Filter ab (`invalid_filters_for_larger_scoped_webhooks`). Für Task-Events scope auf `user_task_list` (My Tasks) — `asana_tools.get_my_user_task_list_gid()`.
- **`create_task` ohne assignee** → Task in keinem user_task_list → Webhook feuert nicht. Default ist deshalb `assignee="me"`.
- **Filter-Whitelist** (DEFAULT_FILTERS in register-Skript): task added/changed/deleted/removed/undeleted + story added. Keine Erfindungen.
- **HMAC-Signatur**: SHA-256 vom Raw-Body. Server verifiziert in `asana_webhook()`. Handshake-Phase erkennbar am `X-Hook-Secret`-Header; Event-Phase am `X-Hook-Signature`.

### Asana-Tunnel-Daemon
- **launchd Service**: `com.user.jarvis.asana`. PATH explizit gesetzt in EnvironmentVariables (`/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin`), sonst findet launchd weder ssh noch cloudflared.
- **Pinggy anonymous tier**: 60-Min-Limit pro SSH-Session. Daemon rotiert bei 55 Min.
- **Cloudflared trycloudflare.com** war während dieser Session 500-Outage (Cloudflare-seitig). Pinggy via `ssh -p 443 a.pinggy.io` als Fallback.
- **Pinggy-Subdomain enthält die User-IPv6** (`*-2003-e3-…-299a.run.pinggy-free.link`). Privacy-Gotcha bei Shared-Links.

### Obsidian
- **Local REST API Plugin v3.6.2** installiert. Port **27123 (HTTP insecure, local-only)** — umgeht den Self-signed-Cert-Tanz von 27124. Bindet auf 127.0.0.1.
- **Restricted Mode** umgangen via `.obsidian/app.json` → `{"enableCommunityPlugins": true}`. `community-plugins.json` allein reicht NICHT.
- **API-Key pre-seeded** in `data.json` des Plugins vor First-Run — Plugin nimmt den vorhandenen Wert, kein UI-Copy-Paste nötig.
- **Daily-Notes-Folder-Alignment**: `.obsidian/daily-notes.json` mit `"folder": "Daily Notes"` plus `app:reload` via REST. Ohne reload schreibt das REST-Plugin in Vault-Root.
- **Vault-Pfad mit Leerzeichen** (`jarvis obsidian`). `pathlib.Path()` OK; in Shell-Args quoten.

### WorldMonitor
- API-Key nicht gesetzt → WM_*-Actions sind aus dem System-Prompt rausgegated (`worldmonitor_tools.is_configured()`).
- Free-Tier (0 USD), Pro 39,99/Monat. Free reicht eventuell für `country-brief`, definitiv nicht für `resilience-score`.

### Frontend / Voice
- **Barge-In** ist aktiv: `recognition.interimResults = true`, `currentAudio.pause()` + Queue-Clear bei ≥3 Zeichen Interim-Text nach 800 ms Grace.
- **`droppingAudio`-Flag**: nach Barge-In werden noch eintreffende TTS-Chunks aus dem alten Turn verworfen, bis das nächste finale User-Transkript gesendet wird.
- **Server-Side Cancellation**: jeder neue User-Turn ODER `{type:"interrupt"}` cancelt den laufenden `process_message`-Task. Verhindert, dass alte Audio-Chunks nachkommen.

## Operationale Befehle

| Aktion | Befehl |
|---|---|
| Server starten (foreground) | `python3 server.py` |
| Server-Restart | `lsof -ti tcp:8340 \| xargs kill && python3 server.py` |
| Asana-Daemon Status | `launchctl print gui/$(id -u)/com.user.jarvis.asana \| grep state` |
| Asana-Daemon Stop | `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.user.jarvis.asana.plist` |
| Asana-Daemon Start | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.jarvis.asana.plist` |
| Asana-Daemon manuelle Rotation | `launchctl kickstart -k gui/$(id -u)/com.user.jarvis.asana` |
| Asana-Daemon Logs | `tail -f ~/Library/Logs/jarvis-asana.log` |
| Aktive Asana-Webhooks | `python3 scripts/register-asana-webhook.py --list` |
| Asana-Projekte listen | `python3 scripts/register-asana-webhook.py --list-projects` |
| Obsidian REST testen | `curl -H "Authorization: Bearer $(cat .local_rest_api_key)" http://127.0.0.1:27123/vault/` |

## Neue Integration einbauen — Pattern

1. **Modul** `<service>_tools.py` schreiben. Konventionen: `httpx.AsyncClient`, `_load_config()` aus `config.json`, `is_configured()`-Funktion, einzelne action-friendly Coroutines mit klaren Returntypes.
2. **Config-Felder** in `config.json` UND `config.example.json` ergänzen.
3. In `server.py`:
   - Import oben (alphabetisch zwischen den anderen Tool-Imports).
   - In `build_system_prompt()` einen Block hinzufügen, gegated durch `<service>_tools.is_configured()`.
   - In `execute_action()` Branches für die neuen Action-Types — mit defensiver Fehlerbehandlung, denn Fehler werden vom LLM gesprochen.
4. Optional: gitignore-Eintrag für Secret-Files.
5. Smoke-Test:
   - `python3 -m py_compile server.py <module>.py`
   - Modul-Direktaufruf via `python3 -c "import asyncio, <module>; asyncio.run(<module>.<fn>(...))"`
6. Server-Restart.

## System-Prompt-Architektur

Der Prompt wird in `build_system_prompt()` aus Bausteinen zusammengesetzt:
- `{USER_NAME}` / `{USER_ADDRESS}` / `{USER_ROLE}` / `{CITY}` aus `config.json` — keine Hardcodes mehr.
- `weather_block` aus wttr.in (sync, refresh bei "activate")
- `task_block` aus Obsidian-Inbox + Asana-Tasks (async, refresh bei "activate")
- Action-Blocks per `is_configured()`-Gating

Das stellt sicher, dass das LLM nur Actions vorschlägt, die tatsächlich ausführbar sind.

## Häufige Fehlerbilder

| Symptom | Ursache | Fix |
|---|---|---|
| `[ACTION:MAC_REMIND]` antwortet mit `-1743`-Fehler | Automation-Permission fehlt | System Settings → Privacy → Automation → aufrufenden Prozess erlauben |
| Screen-Capture liefert nur schwarz | Screen-Recording-Permission fehlt | System Settings → Privacy → Screen Recording → python3 |
| `[ACTION:OBSIDIAN_*]` schweigt obwohl Vault da | REST-Plugin nicht erreichbar, Restricted Mode | `.obsidian/app.json` Setting prüfen, ggf. Obsidian neu starten |
| Asana-Webhook liefert keine Events | Tasks haben keinen Assignee → nicht in user_task_list | `create_task(..., assignee="me")` (default) verwenden |
| Pinggy-Tunnel tot nach 60 Min | anonymous tier expiry | Daemon rotiert automatisch; falls Daemon down: `launchctl kickstart -k gui/$(id -u)/com.user.jarvis.asana` |
| Server-Restart liest config nicht neu | `config` ist Modul-level cached | Restart ist nötig; per-call sind nur die Tool-Module (asana_tools etc.), die `_load_config()` fresh lesen |
