# gents 🧐

The keymaster for a fleet of sandboxed Claude agents — one long-lived,
`--dangerously-skip-permissions` session per repo, each in its own Docker
container.

You run **this** repo by hand: it's the one node that can grant another box
CPU, memory, or secrets, so it is never itself a YOLO agent. Everything else
runs unattended inside a box.

> **Scope.** A single-operator tool for running Claude Code in Docker on macOS.
> All boxes share one Claude login; the host is assumed to be your Mac (Docker
> Desktop). Network egress is **not** yet sandboxed — see [Not yet](#not-yet).

## The contract

> The sandbox must give you **more** liberty than plain `claude`, not less.

- **Generous inside the box.** The agent installs packages, roams the repo,
  uses its scoped credentials, and hits the network — all with **zero
  permission prompts**, unattended. You never click "allow."
- **Hard wall at the edge.** It cannot see other repos, other secrets, your
  personal gcloud account, or the host filesystem. Blast radius = one repo +
  the slice of credentials it was granted.
- **Fast path to widen the inside.** Need another secret, domain, or more
  memory? One line in `fleet.json` — never a code change.

If a box is so tight the agent keeps hitting walls it genuinely needs, that's a
bug against the contract — widen it, don't babysit it.

## Two manifests, split by who owns the fact

| File | Lives in | Owns | Example |
|---|---|---|---|
| `gent.json` | the **target repo** | its *requirements* — which secrets/egress/tools it needs | "needs `gcp` creds, `api.example.com`" |
| `fleet.json` | **here** | the *machine's grants* — cpu/mem, where each secret resolves, approval | "myapp → 2 cpus, 3g, allow: true" |

The repo declares what it needs (versioned with its code, reviewed in its own
PR). This machine decides what it actually gets — a repo can never grant itself
more than the host approves. Same split as GCP: the app declares the roles it
needs; the platform sets quota and binds the service account.

## Quick start

```bash
ln -s "$PWD/gent" ~/bin/gent       # once, from the repo root — put gent on PATH
cp fleet.example.json fleet.json   # then edit: add your repos + their grants
gent fleet build                   # build the base image
```

`fleet.json` is gitignored (your machine's private grants); the checked-in
`fleet.example.json` is the template.

Bring a repo's box up and live in it:

```bash
cd ~/Projects/myorg/myapp
gent up && gent attach
```

You log in **once**: inside any box run `/login`, and every box shares it — see
[Auth & memory](docs/auth.md).

## Commands

Bare `gent` prints usage, like bare `git`. The repo defaults to the cwd's repo
when the name is omitted.

```bash
gent up     myapp   # create + start a repo's agent (persistent, detached)
gent attach myapp   # drop into its live claude session (tmux)
gent shell  myapp   # a separate shell in the box (or `-- cmd` for a one-off)
gent peek   myapp   # snapshot the session without attaching
gent send   myapp "do X"   # prompt the box's session from the host (or pipe on stdin)
gent logs   myapp   # container/bootstrap logs
gent down   myapp   # stop + remove (home volume kept; --wipe to clear it)
```

Fleet/host admin lives under `gent fleet` (run rarely), with `gent ls` as the
daily glance:

```bash
gent ls             # fleet overview (= `gent fleet ls`) — boxes + the host session
gent fleet up       # bring up every approved box not already running
gent fleet down     # stop + remove all boxes (home volumes kept)
gent fleet doctor   # check docker, base image, shared login, secret dirs
gent fleet build    # build the base image
gent fleet auth     # shared-login status
```

`fleet up` skips `allow: false` repos and already-running boxes, and never
aborts the batch on one repo's failure — it's mainly for a fresh checkout or
after a `fleet down` (`--restart unless-stopped` already revives boxes when
Docker starts). `fleet down` keeps every home volume on purpose; wipe
deliberately, per box, with `gent down <repo> --wipe`.

`gent ls` lists this machine's own supervised claude ([`gent host`](docs/auth.md))
as a last row, and `gent attach host` joins it — the host is a member of the
fleet you can glance at and drop into like any box.

## Configuring a repo — `gent.json`

A repo opts into gents features through its own versioned `gent.json`:

| key | effect |
|---|---|
| `needs` | the secrets/egress/tools the repo requires — see [Secrets](docs/secrets.md) |
| `services` | long-running daemons run next to claude (a poller, an event bridge) |
| `derived` | build outputs the box builds on its own volume (`node_modules`, `target/`) |
| `events` | the event bus: one Monitor over one stream, plus cron slots — e.g. `{"cron": "cron.json"}` |
| `telegram` / `monitor` | what the watchdog keeps armed, for a box with its own watch tooling |
| `clear` | periodic context-refresh nudge, e.g. `{"every": "6h"}` |

> Point `events.cron` at a file **in the repo** and commit it — a relative path is
> repo-anchored. At its default the schedule lives in the box's state dir, so a
> reprovisioned box comes back with no slots and says nothing.

`.gent/setup.sh` (first-run install hook) and `.gent/hello.md` (a standing brief
replayed into the pane on startup and after a clear) round it out. Full
reference: [docs/configuration.md](docs/configuration.md).

## How it works

- **Attach is native.** There's no thin client — the real `claude` CLI runs in
  the box and you drive it over `docker exec` + tmux. Use a modern GPU terminal
  (Ghostty, kitty, WezTerm, iTerm2) for truecolor + clipboard + Shift+Enter.
  [More →](docs/configuration.md)
- **One login, one brain per repo.** All boxes share a single Claude config dir,
  so you `/login` once. Each repo's memory and transcripts are bound from your
  host's `~/.claude` at the repo's real path, so a box and your host sessions
  for that repo share one per-repo brain. [More →](docs/auth.md)
- **Scoped secrets.** A shared catalog of credentials; each box sees only the
  entries its `fleet.json` grant references — scoped GCP SAs, per-repo deploy
  keys, read-only `gh` tokens, per-team Vercel tokens. [More →](docs/secrets.md)

## Not yet

- **Egress allowlist enforcement — not implemented.** `gent.json:needs.egress`
  is *documentation only* today: every box has **open outbound network**. The
  hard wall at the edge holds for the filesystem, secrets, and other repos — but
  **not** for network egress yet. Treat outbound as unrestricted until a
  firewalling pass lands.
- **Host-job bridge** — a DB-queue + typed-worker for native host compute, built
  when a repo first needs local native execution.
