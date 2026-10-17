# Configuring & operating a box

A repo opts into gents features through its own versioned `gent.json` plus two
optional files in `.gent/`. This page covers the per-box knobs; for credentials
see [Secrets](secrets.md), for login and memory see [Auth](auth.md).

## Attach feels native — the box runs the *real* claude CLI

There is no thin client for Claude Code: when you `gent attach`, the genuine
`claude` CLI is running in the box and you're driving it over `docker exec` +
tmux. Fidelity is capped by the weakest link in the chain, so two one-time
choices make it feel like a local `claude`:

- **Box side (done):** `/etc/tmux.conf` strips tmux's UI (no status bar,
  truecolor, OSC52 copy-out, extended keys, mouse) so the multiplexer is
  invisible. Baked into the base image.
- **Host side (your terminal):** Terminal.app is the bottleneck — 256-color
  only, no OSC52, weak key encoding. Use a modern GPU terminal: **Ghostty**
  (recommended — native, zero-fuss), or kitty / WezTerm / iTerm2. All give
  truecolor + clipboard-out + Shift+Enter.

The one thing no terminal fixes: pasting an image/file *into* the box. Dragging
a file inserts its **host** path, which the container can't see (the sandbox
wall). Copy *out* works; paste *in* is a remote limitation — drop the file into
the repo dir (it's mounted) and reference it by path.

## Sidecar services — a box can run a daemon next to claude

Some repos need a long-running process *alongside* the agent — a poller, an
event bridge — that claude only watches (via `tail -F` / `Monitor`), never
spawns. Declare them in the repo's `gent.json`:

```json
"services": [
  { "name": "poller", "cmd": "set -a; . ./.env; set +a; python3 poller.py" }
]
```

gents owns their lifecycle: each runs in its own auto-restarting tmux window
inside the box (so a crash self-heals, like the claude window). The command runs
from the repo's mount path, so the daemon executes the repo's **current source**
— edit the code and restart the window (or `gent down/up`) to reload, no image
rebuild. A repo's own first-run `.gent/setup.sh` (best-effort, runs before the
services) is the place to install their deps.

Typical shape: a repo whose agent coordinates an external service (a chat bot, a
message queue, a home-automation hub). The always-on backend lives on the host
or in the cloud; the box is the *agent* — the claude session plus a thin daemon
that feeds it. Anything LAN- or device-bound stays on the host, reachable from
the box at `host.docker.internal`.

## Host actions — the one thing a box can ask the host to do

A box has no docker socket, deliberately: it runs `claude
--dangerously-skip-permissions`, and the socket is root on the host. But a box
often cannot restart **the process holding its own code**. An api container that
bind-mounts the repo read-only keeps its modules in memory, so an edit is dormant
— and, unlike a daemon that logs `STALE:`, it says nothing while it serves last
hour's code. Before this existed, closing that gap meant asking a human or
another claude session in prose. A capability exercised by asking a peer in
English is not a capability; it is an unwritten dependency, discovered late.

Grant one in `fleet.json`, per repo:

```json
"host_actions": {
  "actions": {
    "restart-api": { "run": ["docker", "compose", "restart", "myapp-api"], "min_interval_s": 60 },
    "restart-scale": {
      "run": ["docker", "compose", "restart", "scale"],
      "min_interval_s": 300, "group": "ble-radio", "group_cooldown_s": 90
    }
  }
}
```

**Names, not commands.** The box writes a request naming an *action*; the argv
lives in `fleet.json`, on the host, which no box can read or write. A box cannot
compose a command, pass an argument, or name a service you did not list. No shell
is involved anywhere (`run` is an argv list, never a string). The worst a confused
box can do is ask for one of its own listed actions at the wrong moment — which is
what the two guards are for. A bare argv list is shorthand for an action needing
neither.

**The transport is a directory**, because the box already has one. Declaring
`host_actions` is the whole opt-in: gent provisions
`state_root/host-actions/<box>/spool` and binds it read-write at
`/gent/host-actions` (also `$GENT_HOST_ACTIONS`). No port, no token, no daemon in
the box. Boxes that declare nothing get no mount and no env var.

A mount can only be added at `docker run`, so a box that is **already running**
gains the channel only when it is next re-created — and that starts a fresh claude
session, discarding the context of the one that is working. When that price is too
high, name a `spool` relative to the repo root instead
(`"spool": "app/data/host-actions"`, pointed at a gitignored dir): the repo is
already mounted read-write, so the channel works immediately with no re-up and no
new mount. Runner state stays under `state_root` either way.

```
$GENT_HOST_ACTIONS/requests/<id>.json   {"action": "restart-api", "why": "backlog 637 note scrub",
                                         "asked": "2026-09-12T15:01:37.637400+00:00"}
$GENT_HOST_ACTIONS/results/<id>.json    {"state": "done|refused|failed", "rc": 0, "output": "…",
                                         "asked": …, "started": …, "answered": …}
```

Every stamp is **aware UTC ISO-8601**, full precision: the host writes `started`
and `answered`, and echoes your `asked` back unchanged. Write `asked` the same way.
A document that mixes an aware stamp with a naive local one turns a 39-second round
trip into two hours — and truncating one side of that pair to whole seconds makes an
answer arrive *before* its own ask.

`gent fleet timers install` arms the runner (every 30 s, alongside the auth jobs);
`gent fleet host-actions` runs one pass by hand. Runner state lives *outside* the
mounted dir, so the rate limit is not a file the caller can edit.

**Every request is answered.** `done`, `refused` (with the reason) or `failed`
— never silently dropped. A dropped request is indistinguishable, to the caller,
from one still queued, so it waits, gives up, and asks again. That holds through a
**kill**, too: the runner must delete the request before running the command (or a
crash mid-restart would re-run it every 30 s forever), so between the delete and
the answer it keeps a host-side in-flight record. An exception is caught and
answered; a SIGTERM at that moment — a reboot, a `systemctl stop`, an OOM — is not,
and used to lose the ask entirely. The next drain now answers it instead, honestly:
*the command WAS launched, so it may or may not have run.*

**Two guards, both generic:**

- `min_interval_s` — the same action cannot run again within N seconds. A caller
  watching a staleness signal would otherwise restart in a loop forever, because
  the signal does not always clear (a lazily-imported module reads as stale until
  something imports it). The clock starts on *attempt*, not on success.
- `group` + `group_cooldown_s` — actions naming the same group are never run close
  together. Two BLE sidecars sharing one radio, restarted in one command, cost
  fifteen minutes of `starting`: the second one's scanner blocked past its radio
  lock and never returned. A grouped ask that arrives too early is **deferred**
  (left queued, retried next tick), not refused — the ask is fine, its timing is not.
- `closed_windows` (+ optional `tz`) — hours in which the action is **refused**.
  The other two guards ask whether the *machine* is busy; this one is about *people*,
  which nothing inside the box can measure. Restarting a BLE scale drops the weighing
  someone is standing on, and that fact had been living in the `why` line of a
  hand-written deferral — re-typed each time, with a deadline chosen by feel. One such
  deadline was 22:00, which turned out to be the single most common weighing hour on
  record (11 of 32). **A hold that expires into the peak is worse than no hold**: it
  reads as considered and fires at the worst moment.

```json
"restart-scale": {
  "run": ["docker", "compose", "restart", "scale"],
  "group": "ble-radio", "group_cooldown_s": 90, "tz": "Europe/Zurich",
  "closed_windows": [
    {"from": "07:00", "to": "11:00", "why": "14 of 32 readings; the latest morning one is 10:50."},
    {"from": "19:30", "to": "00:00", "why": "14 of 32; 11 at 22:00 alone, latest 23:35."}
  ],
  "closed_basis": "counted from the scale ledger 2026-09-12, n=32 over 15 days."
}
```

  Refused, not deferred — a cooldown is seconds, a window is hours, and a queued
  request would fire unattended long after its asker's session ended, at a moment
  nobody chose. The refusal names the opening time, so the caller can come back
  deliberately. `from`/`to` are `HH:MM` wall clock in `tz` (the host's own clock if
  unset), half-open, and `from > to` crosses midnight. It **fails closed**: a window
  that does not parse refuses the ask rather than reading as no guard.
  ▶ Make each window state its own evidence. `why` and `closed_basis` are echoed in
  the refusal, so the rule can be re-checked by whoever it stops — a bare pair of
  times is a guess with a timestamp, and choosing the times by feel is exactly what
  put a deadline on the peak. Check the boundaries against the record rather than
  rounding: 10:30 and 23:30 look like the obvious edges here and each misses a real
  reading by minutes.

Keep the vocabulary tiny. Every action is a line a human wrote, and the point of
the design is that a box's entire reach is readable in ten seconds. If you want to
pass an argument from the box, add a second named action instead.

## Backups — the host backs up every box, boxes do nothing

A fleet-level `backup` block in fleet.json turns it on (see `fleet.example.json`).
From then on `gent fleet timers install` adds an hourly `gent fleet backup`, which
backs up, with [restic](https://restic.net) run from its own pinned image:

- **every approved box**: its whole checkout (git-tracked files too, so unpushed
  commits and stashes come back; deduplication makes that nearly free), its home
  volume, and its `state_root/app/<box>` dir;
- **the host itself** (as box `host`): this checkout, where the gitignored
  `fleet.json` lives, and `state_root`, i.e. the secrets catalog.

Regenerable trees are skipped everywhere (`node_modules`, `.venv`, `__pycache__`,
`.next`, `.gradle`, …; in home volumes also `.cache`, `.local/lib`, toolchains);
the lists are at the top of the backup section in `gent`. A box needs no entry at
all. Its fleet.json `backup` is only for exceptions: `exclude` (checkout-relative
paths), `sqlite` (live SQLite files to snapshot consistently first; a file copied
mid-write can restore corrupt), `skip: true`.

**What a box sees** is one file, `.git/gent-backup-status.json` in its own checkout
(never tracked, so no repo has to ignore it, and present without a re-up):
`ok`, `last_success`, `snapshots`, `error`, `stale_after`. `ok: false`, or a
`last_success` older than `stale_after` seconds, means the box is not being backed
up. Boxes hold no backup credential and cannot reach the repository.

**Keys.** One restic repository per box, each with its own password minted into
`secrets_root/backup/<box>.pass`, so restoring one box never decrypts another.
Every repository also accepts `secrets_root/backup/escrow.pass`.
⚠ **Keep a copy of `escrow.pass` off this machine** (a password manager). The
per-box passwords live on the host; lose the host without the escrow copy and
the backups are unreadable ciphertext. `gent fleet doctor` reminds you.

**Deletion protection is the backend's job**, not the key's. restic deletes its
own lock files on every run and `forget` deletes snapshot files, so an add-only
credential cannot run it. Turn on the bucket's soft-delete (or object versioning)
with a credential the host does NOT hold: a compromised host can then delete, but
not irrecoverably, and the status files show a vanished snapshot within the hour.
Prefer a STANDARD storage class: restic rewrites pack files on prune, and archive
classes bill every object for a minimum duration (a year, for GCS ARCHIVE).

**Schedule.** Every run: `backup` (`--skip-if-unchanged`), then `forget` by the
fleet's `keep` (default 24 hourly, 14 daily, 8 weekly, 12 monthly). Weekly:
`--prune`. Monthly: `check --read-data-subset 2%`.

```bash
gent fleet backup                 # run now, every box (what the timer does)
gent fleet backup run axt host    # only these
gent fleet backup status          # freshness per box; exit 1 if any is stale
gent fleet backup restore axt /tmp/axt-restore [--snapshot ID] [--include /src/repo/var]
```

A restore always goes into a NEW directory (it refuses a non-empty one), never over
a live checkout: `/src/repo` is the checkout, `/src/home` the home volume,
`/src/state` the app state, `/src/sqlite` the consistent SQLite copies. Files are
handed back to your user. Copy over what you need, then `gent up` as usual.

## ⚠ Running a one-off command in the image — always `--entrypoint`

The consequence of the section above: the image's `ENTRYPOINT` is `bootstrap`,
which runs the repo's `.gent/setup.sh` and then **starts every service the repo
declares**. That is what you want from `gent up`. It is emphatically not what you
want when you just need a shell, a test run, or a one-off build in the same
environment — because with the repo mounted, a plain

```bash
docker run --rm -v "$PWD":/repo <image> ...          # ✗ boots a whole box
```

starts a **second copy of every sidecar**, against whatever real state you
mounted: the repo's live data directory, and any `--network` you attached. A
duplicated poller or event bridge writing alongside the real one is the kind of
fault that looks like a haunted daemon for a week.

Two things make it worse than a stray container usually is:

- **A host-side `timeout` kills the `docker run` *client*, not the container**,
  so `--rm` never fires and the services keep running after your command
  "ended". After any run that timed out, check with
  `docker ps --filter ancestor=<image>`.
- The container is unnamed, so it is easy to miss in `docker ps` among the boxes.

**The safe form is one flag:**

```bash
docker run --rm --entrypoint bash -u 1000 \
  -v "$PWD":/repo -w /repo <image> -lc '<command>'
```

`--entrypoint bash` skips `bootstrap` entirely: no `setup.sh`, no services.

ⓘ The trade-off is real and worth knowing before it confuses you: `setup.sh` is
also what installs the repo's own dependencies, so with the entrypoint overridden
they are absent. A tool that worked inside the box fails here with a missing
module. Install what that one command needs and no more.

### Running a counterfactual — "was this failing before my change?"

The reason to reach for the image at all is usually to answer that question. The
approach that does *not* work is a `git worktree` of the old commit: a worktree
holds only tracked files, and anything gitignored — a data directory, a `.env`, a
built payload — is missing, so the command fails for a reason that has nothing to
do with the change you are testing.

What works is the inverse. Mount the **real** tree, then bind-mount a copy of
just the files you want to differ, read-only, over their own paths:

```bash
git show HEAD:path/to/file.ts > /tmp/file.head.ts
docker run --rm --entrypoint bash -u 1000 \
  -v "$PWD":/repo \
  -v /tmp/file.head.ts:/repo/path/to/file.ts:ro \
  -w /repo <image> -lc '<the failing command>'
```

The container sees a complete tree with one file swapped, and the working tree is
never touched. That last part matters whenever something else may be writing the
repo — another session, a running daemon, a scheduled job: it is also how to run
a **sabotage control** (break the thing on purpose, confirm the check goes red)
without a broken file ever existing on disk where something else could commit it.

## The event bus — one Monitor, many sources

A box's async wake-ups — an inbound Telegram message, a due reminder, a cron slot
— all land in **one** append-only stream (`events.jsonl`), and the agent watches
it with **one** Monitor (`gent-events watch`). The sources that feed the stream
are **box-supervised**, so they keep running across `/clear`, compaction, and a
dropped Monitor; only the thin final hop is session-bound, and a watchdog re-arms
it when it drops. Turn the bus on with `"events": true` (or any `.events` object):

```json
"events": { "cron": "cron.json", "path": "var/events.jsonl", "tz": "Europe/Zurich" }
```

- **`cron`** — the [cron file](#cron-slots-put-the-file-in-the-repo) (default:
  the box's state dir).
- **`path`** — the stream (default: the box's state dir).
- **`tz`** — the zone cron slots fire in (default: the box's `$TZ`, then UTC).

A **relative path is resolved against the repo**, an absolute one is taken as-is.
That single fact is the whole game — see below.

A box whose inbound arrives over Telegram gets the bus automatically if it runs a
`tg poll` service. Any other tooling opts in explicitly, so the watchdog knows what
to keep alive and what to tell the agent to run on re-arm:

```json
"telegram": { "watch": "./m telegram watch", "reconcile": "./m telegram pending" }
```

Use `"monitor": { "watch": …, "reconcile": …, "live": … }` for a box that watches
its own raw stream rather than `gent-events watch` (`live` is the pgrep pattern
that proves it's up).

### Cron slots: put the file in the repo

`gent-schedule` fires wall-clock slots from a JSON array — a nudge into the
stream, a command to run, or both:

```json
[{"name": "checklist", "cron": "45 9 * * 2-5", "run": ["tools/checklist", "open"]},
 {"name": "digest",    "cron": "32 8 * * *",   "event": {"topic": "morning_digest"}}]
```

**Point `events.cron` at a file inside the repo and commit it.** Left at its
default the file lives in the box's state dir, which is *not* the repo and is *not
versioned* — so a reprovisioned box comes back with **no slots at all, silently**.
Nothing errors; the nightly job simply never runs again, and you find out on the
evening nothing goes out. A schedule is operational truth: it belongs in git next
to the script it runs. (Only the per-minute de-dup state stays box-local, which is
right — it's ephemeral.) `run` executes in the repo dir, so keep the command
**relative** (`tools/checklist`) — a host-absolute path breaks the moment the fleet
moves to another machine.

## Derived build outputs — the box builds its own, the host keeps its own

A repo's files are either **source** (what git tracks — OS-neutral text) or
**derived** (built *from* source: `node_modules`, `.venv`, `target/`, `dist/`).
The repo bind mount shares source live with the host — that's the point. But a
derived artifact is platform-specific: a macOS `node_modules` is *wrong* on a
Linux box, and sharing one dir across both OSes means a `pnpm install` on either
side wipes the other. Source is the contract; derived is each environment's own
business.

So a box **builds its own** derived artifacts on a private volume. Declare them
in the repo's `gent.json` as `{ path: build-command }`:

```json
"derived": { "node_modules": "pnpm install --frozen-lockfile" }
```

For each entry gents mounts a per-box named volume at `<repo>/<path>` — which
**masks** the host's copy (your host checkout is never touched) — then `chown`s
it and runs the build before claude/services start. Wiped with the home volume
by `gent down <repo> --wipe`. Repos with no `derived` are unaffected. It's the
general primitive; the build command owns all tool-specific detail (Rust →
`{"target": "cargo build"}`, etc.).

For pnpm/node the image enables **corepack** (so `pnpm` resolves to each repo's
pinned `packageManager` version) and points `npm_config_store_dir` at a
**fleet-shared store volume** (`/pnpm-store`): every package version is
downloaded *once across all boxes* and reused. The store and a box's private
`node_modules` are separate mounts, so pnpm *copies* in (a hardlink would cross
a mount → `EXDEV`) — the win is no re-downloads, not on-disk dedup.

> One-time host step: because the host's own `node_modules` was masked (not
> built) by the box, run your normal install once on the host (e.g. `pnpm
> install`) to make the host checkout pristine too. From then on the two never
> collide.

## Periodic context refresh — nudge, the model clears itself

A long-lived box accretes context: hours of turns the agent no longer needs but
keeps paying for. The fix is a periodic `/clear` — but a blind, timed clear can
nuke a box mid-task, before it's written down what mattered. So gents splits it:
a timer only **nudges**, and the **model** decides when it's safe to clear and
pulls the trigger itself.

Opt a box in via its `gent.json` — on a cadence, on **context size**, or both:

```json
"clear": { "every": "6h", "atTokens": 300000 }
```

The box's pane gets a one-line nudge — *"good stopping point? wrap up, commit,
save memory, then run `gent-clear -m "<what's next>"`."* Nothing is cleared by
the nudge. The agent, when **it** judges it's ready, runs the in-box command
**`gent-clear`**, which does the mechanical part: send `/clear` into its own
pane, then replay the box's hello to re-orient the fresh session. Mid-task? It
ignores the nudge and gets poked again later. No `clear` key → no nudge.

- **`every`** — a plain cadence. Takes `s`/`m`/`h`/`d` suffixes (bare number =
  seconds).
- **`atTokens`** — nudge when the session's context passes this many tokens.

**Prefer `atTokens`.** A cadence fires whether or not the session is actually
full, so a box that worked hard for twenty minutes is nudged at the same moment
as one that idled for six hours — which is how a nudge teaches its reader to
dismiss it. The size trigger reads the real number: every assistant turn in the
session transcript records its `usage`, and `input_tokens` +
`cache_read_input_tokens` + `cache_creation_input_tokens` is exactly the context
that turn was sent. Keep `every` alongside it as a floor for a session that
stays small but goes stale.

The two nudges differ in tone on purpose. The cadence nudge says *"mid-task?
ignore this"* — a timer has no evidence the session is actually full. The size
nudge does not, because past the threshold **staying is the more expensive
option**: recall of earlier turns is already degrading, so "later" means finishing
the work in a worse state. It says to land what is in flight — finish it, commit
it, save what's durable — and then clear, without starting anything new first.

The size nudge names the number it saw, so the agent can judge for itself. It
re-arms when a clear actually lands (a `/clear` starts a fresh transcript, and
the reading drops with it — there is no high-water mark to reset), and otherwise
stays quiet until the context has grown by another quarter of the threshold. So
ignoring one nudge is not punished every minute, but a session that keeps
growing is told again. A transcript that cannot be read counts as `0` and can
never trip the threshold — the failure direction is silence, not a false alarm.

### Handing work across the clear

A clear is amnesia: the fresh session has the hello, so it knows *who it is* —
but not what you were in the middle of. Anything that should survive goes in the
**handoff**, an instruction the agent writes to its post-clear self:

```bash
gent-clear -m "PR #42 is green but unmerged — merge it, then resume the backlog"
```

It's delivered with the hello as the fresh session's first prompt (brief above,
handoff below), so the box wakes up oriented **and** pointed. Write it for a
reader who remembers nothing: name the task, the branch, the next step — not
"continue where I left off".

Anything durable still belongs in a commit or memory — the handoff is a pointer
to the work, not the record of it. It's dropped if the clear never lands (the old
context is intact, so there's nothing to hand off).

**`gent-clear` parses like `git commit`.** The handoff arrives only via `-m`;
anything else is a usage error that clears nothing. That's deliberate: the note
used to be a bare positional, so *every* argument was a note — an agent running
`gent-clear --help` to read the manual wiped its own context instead. A command
whose job is destroying context can't have a swallow-anything argument.

⚠ **The note in `-m` is written by your shell before `gent-clear` sees it**, and no
argument parser can undo that. In bash double quotes, `` `backticks` `` and `$(…)`
run as commands and `$VAR` expands — and a handoff is prose about code, which is
mostly backticks. This has already fired: a handoff quoting a command in backticks
ran it, and the run migrated a 208-row data file before the write it attempted was
refused. Note the order — the refusal came *after* the schema change, so "the guard
rejected it" did not mean "nothing happened". For any note containing backticks,
`$` or quotes, use the draft below; a file is never interpreted.

Bare `gent-clear` therefore clears nothing either. Like `git commit` with no
`-m`, it opens a draft — `~/.local/state/gent/handoff.md`, pre-filled with
commented instructions — and stops. Write the handoff into it and run
`gent-clear --file` to spend it; the draft is consumed (moved aside to
`.sent`) so it can never be re-handed to a later session. Leave it untouched and
`--file` still clears, just with nothing handed over — that's the dismissal, and
`gent-clear --none` is the same answer in one step. So "no handoff" stays
reachable, but only as something you said, never as something you fell into.

Whichever clearing form it uses (`-m`, `--file`, `--none`), it must be the
agent's **final action** with no output after: `gent-clear` runs through the Bash
tool, so the pane is busy and the `/clear` keystroke queues behind the current
turn. Staging the draft is the exception — that one is mid-turn by design.

### The host session clears the same way

`gent-clear` lives in the image, so for a long time it reached every box and not
the one session running on the host itself — even though `gent host up` treats
that session as a claude session like any other, in-repo memory and all. It
accreted context forever with nowhere to hand off.

```bash
gent host clear -m "PR #42 is green but unmerged — merge it, then resume"
```

`gent host clear` runs **the same script the boxes run**, pointed at the host's
tmux session via `GENT_TMUX_SESSION` (unset → `gent`, so boxes are unchanged).
The flags are identical: `-m`, `--file`, `--none`, and bare stages a draft. It
delegates rather than reimplements — the queued keystroke, the
prove-it-by-a-new-transcript check and the idle-gated greet are subtle enough
that a second copy would drift from the first the moment either changed.

## What the bill actually is — the compaction ceiling, and eco mode

A long agent session is not expensive because it *writes* a lot. Measured over
three weeks of one fleet's transcripts, **~75% of the spend was cache reads**:
every request re-reads the whole context, and a native-1M model does not
compact until ~967K tokens. The median request carried 250–330K tokens; builder
subagents grew to 500–900K and cost more than the main agent that spawned them.
Output (and so effort) was ~6%. So the lever is *how long a context may grow*,
and second *which model reads it*.

Both are claude settings, and a fleet keeps them in **two files** — the shared
`claude-home` every box mounts, and the host's own `~/.claude` — which drift if
edited by hand. So `fleet.json`'s `cost` block is the authority, and gent is the
only writer of the keys it names:

```json
"cost": {
  "autocompact_window": 400000,
  "subagent_model": "claude-sonnet-5-5",
  "disable_fast_mode": true,
  "eco": { "model": "sonnet", "max_effort": "medium", "autocompact_window": 200000 }
}
```

```bash
gent fleet cost            # apply the standing policy (idempotent; --check only reports)
gent fleet doctor          # warns when it has drifted; `gent up` re-asserts it
```

- **`autocompact_window`** → `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. Measured on CLI
  2.1.296, not documented: it reaches **subagents** too, and compaction fires
  ~**35K below** the number (a 100K window compacted a subagent at ~64K), so
  400000 compacts near 365K.
- **`subagent_model`** → `CLAUDE_CODE_SUBAGENT_MODEL` + `…_FORCE=1`.
- **`disable_fast_mode`** → a saved `fastMode` in a shared file would otherwise
  put every box on the 2×-priced tier.

A key you leave out is never touched, so this does not delete a setting someone
made by hand.

**Keep the clear nudge below the compaction point.** Compaction is the lossy
backstop; the nudge (`clear.atTokens`, above) lets the model hand off in its own
words first. With a 400K window, a 350K nudge leaves the model ~15K tokens of
runway — raise the window, not the nudge, if it lands mid-thought.

### Eco mode — for when the quota is running out

```bash
gent eco on        # cheaper model + effort cap + tighter window, fleet-wide
gent eco off       # put everything back exactly as it was
gent eco status    # what each session is configured for vs. actually running
```

`on` changes, and `off` restores from a record in `<state_root>/eco.json`:

- the shared claude-home **and** the host's `~/.claude`: `model`, `maxEffortLevel`,
  and the window (the profile in `cost.eco`; the model is an *alias* so a
  release does not stale it);
- `model` in `<repo>/.claude/settings.local.json` of every box whose
  **project** settings pin a model — a project pin outranks the shared default,
  which is the usual reason a fleet-wide model change "did nothing" for one
  repo. The local file outranks the pin and is gitignored, so no tracked file
  is touched;
- every **running** session, switched in place with `/model`. A running
  session ignores a settings change to its model (it is fixed at launch, and a
  restart resumes the model the transcript recorded), so the pane is the only
  lever that keeps the conversation. gent answers the CLI's "Switch model? the
  full history gets re-read" menu — which would otherwise block an unattended
  pane — and skips sessions already on the target, since a switch is a
  full-history re-read.

`--dry-run` prints the plan and changes nothing; `--no-live` changes config only.

⚠ `/model` writes `model` and `modelSettings[<id>].effortLevel` into the **shared**
settings ("saved as your default for new sessions"), and the effort it records is
the *switching session's* — from an `xhigh` box that would put the whole fleet on
`xhigh`. So eco snapshots `modelSettings` before switching and puts it back
after. ⚠ And `/model` makes no request of its own: `eco status` reads each
session's real model from its transcript, so a switch shows up there only after
that session's next turn.

## A custom "hi" per box — `.gent/hello.md`

Drop a `.gent/hello.md` in the repo and gents replays it into the claude pane as
a prompt — the box's standing brief: who it is, what to resume watching, where
its work lives. It fires at two moments:

- **on box startup** (`gent up`) — once claude's TUI has settled, so a fresh box
  is oriented without you attaching, and
- **after `gent-clear`** — a `/clear` wipes the conversation, including whatever
  the agent was watching, so the hello re-orients the empty session (alongside
  the clear's optional [handoff note](#handing-work-across-the-clear), if any).

It's versioned with the repo like `.gent/setup.sh`; multi-line is fine (it's
pasted as one prompt). No `.gent/hello.md` → nothing is replayed (claude still
reloads the repo's `CLAUDE.md` either way). The same primitive is exposed as the
in-box command `gent-hello` to re-send it by hand — `gent-hello "<note>"`
appends a note below the brief, exactly as the post-clear greet does.
