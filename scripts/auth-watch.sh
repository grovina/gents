#!/usr/bin/env bash
# auth-watch.sh — alert (Telegram) when the fleet's shared Claude login goes bad,
# and again when it recovers. Runs on a timer (launchd on macOS, systemd on Linux),
# INDEPENDENT of the boxes — they're all degraded when auth is down, so the alert
# path must not route through them.
#
# Detects the `shared-token-mass-up-race` failure: access token expired AND/OR the
# single-use refresh token clobbered to empty -> fleet-wide "Please run /login" 401.
#
# Config: $GENT_STATE/secrets/alert.env  with TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
# Rate-limited via a flag file: one alert per incident, one on recovery.
set -euo pipefail

STATE_ROOT="${GENT_STATE:-$HOME/.gent/state}"
CREDS="$STATE_ROOT/secrets/claude-home/.credentials.json"
ALERT_ENV="$STATE_ROOT/secrets/alert.env"
FLAG="$STATE_ROOT/.auth-watch-alerted"
host="$(scutil --get ComputerName 2>/dev/null || hostname)"

status="$(python3 - "$CREDS" <<'PY'
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))["claudeAiOauth"]
    if not d.get("refreshToken"):
        print("BAD refresh token empty (mass-up race) — needs /login"); raise SystemExit
    margin_ms = (time.time() + 300) * 1000          # 5-min margin
    if d.get("expiresAt", 0) < margin_ms:
        print("BAD access token expired and not refreshing"); raise SystemExit
    print("OK")
except FileNotFoundError:
    print("BAD creds file missing")
except SystemExit:
    pass
except Exception as e:
    print(f"BAD unreadable creds: {e}")
PY
)"

send() {  # $1 = message text
  [ -f "$ALERT_ENV" ] || { echo "auth-watch: no $ALERT_ENV — cannot alert"; return; }
  set -a; . "$ALERT_ENV"; set +a
  curl -fsS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$1" >/dev/null || echo "auth-watch: telegram send failed"
}

if [ "${status%% *}" = "BAD" ]; then
  if [ ! -f "$FLAG" ]; then
    send "⚠️ gents auth DOWN on ${host}: ${status#BAD }. Fix: \`gent attach <box>\` → /login"
    touch "$FLAG"
    echo "auth-watch: ALERTED ($status)"
  else
    echo "auth-watch: still bad ($status), already alerted"
  fi
else
  if [ -f "$FLAG" ]; then
    send "✅ gents auth recovered on ${host}."
    rm -f "$FLAG"
    echo "auth-watch: recovered, sent all-clear"
  else
    echo "auth-watch: ok"
  fi
fi
