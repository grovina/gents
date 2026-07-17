#!/usr/bin/env bash
# auth-watch.sh — alert (Telegram) when the fleet's shared Claude login goes bad,
# and on recovery auto-reconnect each box's Remote Control (bounce claude so its RC
# socket re-establishes). Runs on a timer (launchd on macOS, systemd on Linux),
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
    # We're the LAST line of defense, not the first. An active box re-mints the
    # shared token as a side effect of its own calls, and `gent fleet refresh-auth`
    # bounces a box at actual expiry — either path fires within a couple of 10-min
    # ticks. Only alarm once the token has been expired PAST that window (15 min),
    # so we don't cry wolf during the brief at-expiry gap before a refresh lands.
    deadline_ms = (time.time() - 900) * 1000        # 15 min past expiry
    if d.get("expiresAt", 0) < deadline_ms:
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
    # Auth just recovered (bad -> good). Boxes that ran through the outage have
    # stale Remote Control sockets — claude only re-establishes RC when it
    # RESTARTS. So bounce claude in every running box (staggered; the token is
    # valid now, so this is race-free); each box's supervisor relaunches it and RC
    # reconnects on its own. This is the self-heal — no command to run.
    #
    # Match on -x (process NAME), like `gent fleet refresh-auth` does. The package
    # now ships a compiled bin/claude.exe, but the running process still reports
    # comm=claude and argv[0]=claude, so -x claude is what actually matches. A -f
    # pattern of 'claude-code/bin/claude' matches NOTHING: -f tests the cmdline
    # ("claude --dangerously-skip-permissions -n <box> --resume <id>"), which never
    # contains the install path. That silently made this self-heal a no-op — it
    # bounced 0 boxes and still reported success.
    bounced=0
    for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep '^gent-'); do
      docker exec "$c" pkill -x claude 2>/dev/null && bounced=$((bounced+1))
      sleep 2
    done
    send "✅ gents auth recovered on ${host} — bounced ${bounced} box(es) to reconnect Remote Control."
    rm -f "$FLAG"
    echo "auth-watch: recovered, bounced $bounced boxes to reconnect RC"
  else
    echo "auth-watch: ok"
  fi
fi
