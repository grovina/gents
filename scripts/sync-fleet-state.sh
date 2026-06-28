#!/usr/bin/env bash
# sync-fleet-state.sh — stage / refresh the fleet's precious non-git state onto a
# relocation target host. Idempotent: rsync ships only deltas, so run it now to
# pre-stage and again at cutover to catch the latest live state.
#
#   ./scripts/sync-fleet-state.sh [user@host]      # default: grovina@grovitec.local
#
# For every box in fleet.json (+ the gents control repo) it copies:
#   - the gent state root (~/.gent/state: secrets + app/<box>, minus reproducible cache/)
#   - each repo working tree  (git + gitignored state; minus node_modules/.venv/build dirs
#                              and homer's homeassistant/ data, which is a Track-B/HA concern)
#   - each repo's Claude memory + transcripts, RENAMED to the target's abs-path key
#
# It does NOT copy: reproducible build dirs, Docker named volumes, or HA data.
# It does NOT start anything — pure file staging.
set -euo pipefail

TARGET="${1:-grovina@grovitec.local}"
REMOTE_USER="${TARGET%@*}"
SSH="ssh -o BatchMode=yes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET="$HERE/fleet.json"

# Claude keys per-repo memory by the repo's ABSOLUTE path (slashes -> dashes).
# Moving Mac (/Users/you) -> Linux (/home/you) changes the key, so we rename on copy.
LKEY="$(printf '%s' "$HOME/Projects" | sed 's#/#-#g')"
RKEY="$(printf '%s' "/home/$REMOTE_USER/Projects" | sed 's#/#-#g')"

# Box repos straight from fleet.json, plus the control repo itself. (Paths have no spaces.)
REPOS=( $(python3 -c "import json;d=json.load(open('$FLEET'));r=d['repos'];print(chr(10).join(v['path'] for v in (r.values() if isinstance(r,dict) else r)))") "grovina/gents" )

EXC=(--exclude='node_modules/' --exclude='.venv/' --exclude='venv/' --exclude='target/'
     --exclude='.next/' --exclude='dist/' --exclude='build/' --exclude='__pycache__/'
     --exclude='*.pyc' --exclude='.turbo/' --exclude='.cache/' --exclude='homeassistant/'
     --exclude='.esphome/' --exclude='.mww-work/'  # esphome build cache + wake-word training scratch (homer, ~14G reproducible)
     --exclude='engine/data/')                     # filmograma: ANCINE/Drive download CSVs (~4.7G, "not part of the deployable artifact")

echo "==> target $TARGET   (memory key ${LKEY}-*  ->  ${RKEY}-*)"
$SSH "$TARGET" 'mkdir -p ~/.gent/state ~/.claude/projects ~/Projects'

echo "==> gent state root (secrets + app/<box>, minus reproducible cache)"
rsync -az --exclude 'secrets/cache/' -e "$SSH" "$HOME/.gent/state/" "$TARGET:.gent/state/"

for p in "${REPOS[@]}"; do
  src="$HOME/Projects/$p"
  [ -d "$src" ] || { echo "  -- $p (absent, skip)"; continue; }
  enc="$(printf '%s' "$p" | sed 's#/#-#g')"
  $SSH "$TARGET" "mkdir -p ~/Projects/${p%/*}"
  rsync -az "${EXC[@]}" -e "$SSH" "$src/" "$TARGET:Projects/$p/"
  mdir="$HOME/.claude/projects/${LKEY}-${enc}"
  [ -d "$mdir" ] && rsync -az -e "$SSH" "$mdir/" "$TARGET:.claude/projects/${RKEY}-${enc}/"
  echo "  ok $p"
done

cat <<'EOF'

==> staged. For the FINAL cutover, in THIS order:
    1. `gent fleet down` on the Mac FIRST     (freeze live state — avoids torn DBs / split-brain)
    2. re-run this script                     (fast delta; ~/.gent/state now carries ALL app state — no volume export)
    3. move the Zigbee dongle + bring up Home Assistant on the target (Track B)
    4. `gent fleet up` on the target
EOF
