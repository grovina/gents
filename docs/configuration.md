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

Opt a box in via its `gent.json`:

```json
"clear": { "every": "6h" }
```

On that cadence the box's pane gets a one-line nudge — *"good stopping point?
wrap up, commit, save memory, then run `gent-clear -m "<what's next>"`."*
Nothing is cleared by the timer. The agent, when **it** judges it's ready, runs
the in-box command **`gent-clear`**, which does the mechanical part: send
`/clear` into its own pane, then replay the box's hello to re-orient the fresh
session. Mid-task? It ignores the nudge and gets poked again next interval.
`every` takes `s`/`m`/`h`/`d` suffixes (bare number = seconds); no `clear` key →
no nudge.

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
