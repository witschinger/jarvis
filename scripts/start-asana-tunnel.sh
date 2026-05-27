#!/bin/bash
# Jarvis — Asana Webhook Tunnel
# Starts an ephemeral cloudflared tunnel pointing at the local Jarvis server,
# extracts the public https URL, and prints the exact command to register the
# Asana webhook against it.

set -u

PORT="${PORT:-8340}"
LOG_FILE="${TMPDIR:-/tmp}/jarvis-tunnel.log"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
WORKSPACE_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install via: brew install cloudflared" >&2
  exit 1
fi

# Quick sanity: is the local server up?
if ! curl -sf -o /dev/null "http://localhost:$PORT/"; then
  echo "Warning: nothing answering on http://localhost:$PORT/. Start Jarvis first (python3 server.py)." >&2
fi

echo "[tunnel] Starting cloudflared on http://localhost:$PORT (log: $LOG_FILE)"
: > "$LOG_FILE"
cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!

cleanup() {
  echo
  echo "[tunnel] Stopping cloudflared (pid $TUNNEL_PID)"
  kill "$TUNNEL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for the public URL to appear in the log
PUBLIC_URL=""
for i in $(seq 1 60); do
  PUBLIC_URL=$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG_FILE" | head -1 || true)
  if [[ -n "$PUBLIC_URL" ]]; then break; fi
  sleep 0.5
done

if [[ -z "$PUBLIC_URL" ]]; then
  echo "[tunnel] Failed to obtain a public URL after 30s. Tail of $LOG_FILE:" >&2
  tail -20 "$LOG_FILE" >&2
  exit 1
fi

WEBHOOK_URL="$PUBLIC_URL/asana/webhook"
echo
echo "==================================================================="
echo "  Public tunnel:    $PUBLIC_URL"
echo "  Webhook target:   $WEBHOOK_URL"
echo "==================================================================="
echo
echo "Register the Asana webhook in another terminal:"
echo
echo "  python3 $WORKSPACE_DIR/scripts/register-asana-webhook.py \\"
echo "      --target '$WEBHOOK_URL'"
echo
echo "Tunnel runs in foreground. Ctrl+C to stop. Log: $LOG_FILE"
wait "$TUNNEL_PID"
