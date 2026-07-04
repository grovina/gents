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
wrap up, commit, save memory, then run `gent-clear`."* Nothing is cleared by the
timer. The agent, when **it** judges it's ready, runs the in-box command
**`gent-clear`**, which does the mechanical part: send `/clear` into its own
pane, then replay the box's hello to re-orient the fresh session. Mid-task? It
ignores the nudge and gets poked again next interval. `every` takes
`s`/`m`/`h`/`d` suffixes (bare number = seconds); no `clear` key → no nudge.

## A custom "hi" per box — `.gent/hello.md`

Drop a `.gent/hello.md` in the repo and gents replays it into the claude pane as
a prompt — the box's standing brief: who it is, what to resume watching, where
its work lives. It fires at two moments:

- **on box startup** (`gent up`) — once claude's TUI has settled, so a fresh box
  is oriented without you attaching, and
- **after `gent-clear`** — a `/clear` wipes the conversation, including whatever
  the agent was watching, so the hello re-orients the empty session.

It's versioned with the repo like `.gent/setup.sh`; multi-line is fine (it's
pasted as one prompt). No `.gent/hello.md` → nothing is replayed (claude still
reloads the repo's `CLAUDE.md` either way). The same primitive is exposed as the
in-box command `gent-hello` to re-send it by hand.
