#!/usr/bin/env bash
# sync-fleet-state.sh — stage / refresh the fleet's precious non-git state onto a
# relocation target host. Idempotent: rsync ships only deltas, so run it now to
# pre-stage and again at cutover to catch the latest live state.
#
#   ./scripts/sync-fleet-state.sh [user@host]      # default: grovina@grovina-mini
#
# THE TARGET'S OWN fleet.json IS THE AUTHORITY for what this copies. Not this
# machine's. The target runs a SUBSET of what the Mac runs, and the difference is
# the whole point: a repo the target doesn't declare is not cloned there, not
# credentialed there, and never runs there. So this reads the target's manifest,
# and copies only:
#   - the repos it declares (+ the gents control repo, which IS gent)
#   - only the catalog entries those repos are granted (SA keys, env fragments,
#     deploy keys) — the target never receives credentials for repos it doesn't run
#   - the shared fleet login, gitconfig and per-box app state
#
# Repo trees carry git + gitignored state, including each repo's .claude/memory —
# memory lives in the repo now, so it just rides along. Nothing to rename, nothing
# to reconstruct. (This used to be the ugly part: memory was keyed by the repo's
# ABSOLUTE path under ~/.claude/projects, so Mac /Users/... -> Linux /home/... had
# to be rewritten on the way over.) Transcripts are deliberately NOT copied: raw
# session logs, keyed per host, and re-ups start fresh sessions anyway.
#
# It does NOT copy: reproducible build dirs, Docker named volumes, HA's recorder DB.
# It does NOT start anything — pure file staging.
set -euo pipefail

TARGET="${1:-grovina@grovina-mini}"
SSH="ssh -o BatchMode=yes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="$HOME/.gent/state"

# --- read the TARGET's manifest --------------------------------------------------
TF="$($SSH "$TARGET" 'cat ~/Projects/grovina/gents/fleet.json' 2>/dev/null || true)"
if [ -z "$TF" ]; then
  echo "!! $TARGET has no ~/Projects/grovina/gents/fleet.json." >&2
  echo "   That file declares what the target runs, and this script refuses to guess." >&2
  echo "   Author it there first (a subset of this machine's fleet.json), then re-run." >&2
  exit 1
fi

eval "$(printf '%s' "$TF" | python3 -c '
import json, shlex, sys
f = json.load(sys.stdin)
repos = f["repos"]
names = list(repos)
paths = [c["path"] for c in repos.values()]
# Exactly the catalog entries the target’s own boxes are granted — nothing more.
grants = sorted({v for c in repos.values() for v in (c.get("mounts") or {}).values()}
                | {e for c in repos.values() for e in (c.get("env_files") or [])})
print("NAMES=(%s)"  % " ".join(shlex.quote(x) for x in names))
print("PATHS=(%s)"  % " ".join(shlex.quote(x) for x in paths))
print("GRANTS=(%s)" % " ".join(shlex.quote(x) for x in grants))
')"

echo "==> target $TARGET"
echo "    repos:  ${NAMES[*]}"
echo "    grants: ${GRANTS[*]}"
$SSH "$TARGET" 'mkdir -p ~/.gent/state ~/Projects'

# --- gent state: ONLY what the target's boxes are granted -------------------------
# Built as an explicit file list rather than exclude-rules, so the default is DENY:
# a credential reaches the target only by being named here. Skips anything absent so
# this works with the stock macOS rsync (no --ignore-missing-args).
LIST="$(mktemp)"; trap 'rm -f "$LIST"' EXIT
{
  echo "claude-home/"        # the fleet's ONE shared claude login (all boxes share it)
  echo "claude/"
  echo "gitconfig"           # so in-box commits are attributed to you
  echo "alert.env"           # auth-watch's telegram alert channel (host-level, not a box grant)
  for g in "${GRANTS[@]}"; do echo "$g"; done
  for n in "${NAMES[@]}"; do echo "ssh/${n}-deploy"; echo "ssh/${n}-deploy.pub"; done
} | while read -r rel; do [ -e "$STATE/secrets/${rel%/}" ] && echo "secrets/$rel"; done > "$LIST"
# per-box app state (state_mounts pulled out of docker volumes), for granted boxes only
for n in "${NAMES[@]}"; do [ -d "$STATE/app/$n" ] && echo "app/$n/" >> "$LIST"; done

echo "==> gent state root (deny-by-default; secrets/cache is reproducible and skipped)"
rsync -az --files-from="$LIST" -e "$SSH" "$STATE/" "$TARGET:.gent/state/"
sed 's/^/    /' "$LIST"

# --- repo trees ------------------------------------------------------------------
EXC=(--exclude='node_modules/' --exclude='.venv/' --exclude='venv/' --exclude='target/'
     --exclude='.next/' --exclude='dist/' --exclude='build/' --exclude='__pycache__/'
     --exclude='*.pyc' --exclude='.turbo/' --exclude='.cache/'
     --exclude='home-assistant_v2.db*' --exclude='home-assistant.log*'  # homer: 688M HA recorder DB + logs (history; .storage/zigbee.db DO travel)
     --exclude='.esphome/' --exclude='.mww-work/'  # esphome build cache + wake-word training scratch (homer, ~14G reproducible)
     --exclude='engine/data/')                     # filmograma: ANCINE/Drive download CSVs (~4.7G, "not part of the deployable artifact")

echo "==> repo trees (incl. each repo's .claude/memory)"
for p in "${PATHS[@]}" "grovina/gents"; do
  src="$HOME/Projects/$p"
  [ -d "$src" ] || { echo "  -- $p (absent here, skip)"; continue; }
  # NEVER clobber the target's fleet.json — it is the target's own manifest, and it is
  # deliberately a different (smaller) file than this machine's.
  # NB `${extra[@]+"${extra[@]}"}`, not `"${extra[@]}"`: macOS ships bash 3.2, where
  # expanding an EMPTY array under `set -u` is an "unbound variable" error.
  extra=(); [ "$p" = "grovina/gents" ] && extra=(--exclude='/fleet.json')
  $SSH "$TARGET" "mkdir -p ~/Projects/${p%/*}"
  rsync -az "${EXC[@]}" ${extra[@]+"${extra[@]}"} -e "$SSH" "$src/" "$TARGET:Projects/$p/"
  echo "  ok $p"
done

cat <<'EOF'

==> staged. For the FINAL cutover, in THIS order:
    1. `gent fleet down --stack` on the Mac  (FIRST — stops the boxes + homer's HA so the agents
                                              stop WRITING. Syncing a live fleet copies torn state.)
    2. re-run this script                    (fast delta: repos incl. .claude/memory, HA config +
                                              .storage, the shared login, per-box app state)
    3. on the target: `gent fleet up`        (boxes + homer's HA host_stack come up together; HA
                                              reconnects to the LAN Zigbee coordinator at
                                              192.168.1.60 — a network coordinator, no dongle to move)
    4. OTA the two ESP satellites            (docker exec esphome esphome run /config/atom-echos3r.yaml
                                              then /config/paper.yaml — they address the host by NAME
                                              now, so this is the one thing still pushed to them.
                                              Echo first, as the canary.)

    NB: the target needs no particular IP. The satellites resolve the host by name via the
    router's DNS, so a new lease — or a whole new machine — costs nothing.
EOF
