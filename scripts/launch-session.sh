#!/bin/bash
# Jarvis — Launch Session (macOS)
# Starts the Jarvis server, plays a Spotify track, opens VS Code + configured apps,
# opens Chrome on the Jarvis UI, and snaps the four key windows into screen quadrants.

set -u

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
CONFIG_PATH="$WORKSPACE_DIR/config.json"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "[jarvis] config.json not found at $CONFIG_PATH" >&2
  exit 1
fi

read_config() {
  /usr/bin/python3 -c "import json,sys; c=json.load(open('$CONFIG_PATH')); v=c.get('$1', ''); print(v if not isinstance(v, list) else '\n'.join(v))"
}

SPOTIFY_URI="$(read_config spotify_track)"
BROWSER_URL="$(read_config browser_url)"
APPS="$(read_config apps)"

# Kill any old server still bound to port 8340
OLD_PID="$(lsof -ti tcp:8340 2>/dev/null || true)"
if [[ -n "$OLD_PID" ]]; then
  echo "[jarvis] Killing previous server (pid $OLD_PID)"
  kill "$OLD_PID" 2>/dev/null || true
  sleep 1
fi

# 1. Start Jarvis server in background; log to ~/Library/Logs/jarvis.log
LOG_FILE="$HOME/Library/Logs/jarvis.log"
mkdir -p "$(dirname "$LOG_FILE")"
echo "[jarvis] Starting server -> $LOG_FILE"
nohup /usr/bin/env python3 "$WORKSPACE_DIR/server.py" >> "$LOG_FILE" 2>&1 &
disown

# 2. Spotify
if [[ -n "$SPOTIFY_URI" && "$SPOTIFY_URI" != "spotify:track:YOUR_TRACK_ID" ]]; then
  open "$SPOTIFY_URI" 2>/dev/null || true
fi

# 3. VS Code at workspace (via `code` CLI if installed, else `open -a`)
if command -v code >/dev/null 2>&1; then
  code "$WORKSPACE_DIR" >/dev/null 2>&1 &
else
  open -a "Visual Studio Code" "$WORKSPACE_DIR" 2>/dev/null || true
fi

# 4. Configured apps — URL schemes via plain `open`, app names via `open -a`
while IFS= read -r app; do
  [[ -z "$app" ]] && continue
  if [[ "$app" == *"://"* ]]; then
    open "$app" 2>/dev/null || true
  else
    open -a "$app" 2>/dev/null || true
  fi
done <<< "$APPS"

# 5. Wait for the server to come up before pointing Chrome at it
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf -o /dev/null "http://localhost:8340/"; then break; fi
  sleep 0.5
done

# 6. Chrome with Jarvis UI + browser_url; allow autoplay so Jarvis can speak immediately
CHROME_FLAGS=(--autoplay-policy=no-user-gesture-required)
if [[ -n "$BROWSER_URL" && "$BROWSER_URL" != "https://your-website.com" ]]; then
  open -na "Google Chrome" --args "${CHROME_FLAGS[@]}" "http://localhost:8340" "$BROWSER_URL" \
    || open "http://localhost:8340"
else
  open -na "Google Chrome" --args "${CHROME_FLAGS[@]}" "http://localhost:8340" \
    || open "http://localhost:8340"
fi

# 7. Snap windows into quadrants. Requires Accessibility permission for the
#    process running this script (System Settings > Privacy & Security > Accessibility).
sleep 3

osascript <<'APPLESCRIPT' 2>/dev/null || echo "[jarvis] Window snap skipped (grant Accessibility permission to enable)"
tell application "Finder"
  set screenBounds to bounds of window of desktop
end tell
set screenW to (item 3 of screenBounds) - (item 1 of screenBounds)
set screenH to (item 4 of screenBounds) - (item 2 of screenBounds)
set halfW to screenW div 2
set halfH to screenH div 2

set targets to {¬
  {"Code", 0, 0, halfW, halfH}, ¬
  {"Obsidian", halfW, 0, halfW, halfH}, ¬
  {"Google Chrome", 0, halfH, halfW, halfH}, ¬
  {"Spotify", halfW, halfH, halfW, halfH}}

repeat with t in targets
  set appName to item 1 of t
  set x to item 2 of t
  set y to item 3 of t
  set w to item 4 of t
  set h to item 5 of t
  try
    tell application "System Events"
      if (exists process appName) then
        tell process appName
          if (count of windows) > 0 then
            set position of window 1 to {x, y}
            set size of window 1 to {w, h}
          end if
        end tell
      end if
    end tell
  end try
end repeat
APPLESCRIPT

echo "[jarvis] Launch complete."
