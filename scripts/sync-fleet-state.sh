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
#                              and homer's 688M HA recorder DB — its config + .storage +
#                              zigbee.db DO travel, so HA comes up paired on the target)
#   - each repo's Claude memory + transcripts, RENAMED to the target's abs-path key
#
# It does NOT copy: reproducible build dirs, Docker named volumes, or HA data.
# It does NOT start anything — pure file staging.
set -euo pipefail

TARGET="${1:-grovina@grovitec.local}"
SSH="ssh -o BatchMode=yes"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLEET="$HERE/fleet.json"

# Per-repo claude memory needs NOTHING special here any more. It lives in the repo
# (<repo>/.claude/memory) and rides the tree rsync below like any other file. This used
# to be the ugliest part of the move: memory was keyed by the repo's ABSOLUTE path under
# ~/.claude/projects, so Mac (/Users/you) -> Linux (/home/you) changed the key and the
# copy had to rewrite it on the way over. Now the path is not part of the address.
#
# Transcripts are deliberately NOT copied: they're raw session logs, they're keyed per
# host, and re-ups start fresh sessions anyway. Memory is the distilled part, and that's
# what travels.

# Box repos straight from fleet.json, plus the control repo itself. (Paths have no spaces.)
REPOS=( $(python3 -c "import json;d=json.load(open('$FLEET'));r=d['repos'];print(chr(10).join(v['path'] for v in (r.values() if isinstance(r,dict) else r)))") "grovina/gents" )

EXC=(--exclude='node_modules/' --exclude='.venv/' --exclude='venv/' --exclude='target/'
     --exclude='.next/' --exclude='dist/' --exclude='build/' --exclude='__pycache__/'
     --exclude='*.pyc' --exclude='.turbo/' --exclude='.cache/'
     --exclude='home-assistant_v2.db*' --exclude='home-assistant.log*'  # homer: 688M HA recorder DB + logs (history; .storage/zigbee.db DO travel)
     --exclude='.esphome/' --exclude='.mww-work/'  # esphome build cache + wake-word training scratch (homer, ~14G reproducible)
     --exclude='engine/data/')                     # filmograma: ANCINE/Drive download CSVs (~4.7G, "not part of the deployable artifact")

echo "==> target $TARGET"
$SSH "$TARGET" 'mkdir -p ~/.gent/state ~/Projects'

echo "==> gent state root (secrets + app/<box>, minus reproducible cache)"
rsync -az --exclude 'secrets/cache/' -e "$SSH" "$HOME/.gent/state/" "$TARGET:.gent/state/"

for p in "${REPOS[@]}"; do
  src="$HOME/Projects/$p"
  [ -d "$src" ] || { echo "  -- $p (absent, skip)"; continue; }
  $SSH "$TARGET" "mkdir -p ~/Projects/${p%/*}"
  rsync -az "${EXC[@]}" -e "$SSH" "$src/" "$TARGET:Projects/$p/"   # incl. .claude/memory
  echo "  ok $p"
done

cat <<'EOF'

==> staged. For the FINAL cutover, in THIS order:
    1. `gent fleet down --stack` on the Mac  (FIRST — stops boxes + homer's HA so the agents stop
                                              WRITING. Syncing a live fleet copies torn state.)
    2. re-run this script                    (fast delta; ~/.gent/state + repos carry everything —
                                              HA config + .storage, and each repo's .claude/memory)
    3. on the target: `gent fleet up`        (boxes + homer's HA host_stack come up together; HA
                                              reconnects to the LAN Zigbee coordinator at
                                              192.168.1.60 — network coordinator, no dongle to move)
    4. OTA the two ESP satellites            (docker exec esphome esphome run /config/paper.yaml
                                              and /config/atom-echos3r.yaml — they address the host
                                              by NAME now, so this is the one thing that still has to
                                              be pushed to them. Do the Echo first as a canary.)

    NB: the target does NOT need any particular IP. The satellites resolve the host by name
    via the router's DNS, so a new lease — or a whole new machine — costs nothing.
EOF
