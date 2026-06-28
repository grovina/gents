# gents 🧐

The keymaster for a fleet of sandboxed Claude agents — one long-lived,
`--dangerously-skip-permissions` session per repo, each in its own container.

You run **this** repo by hand. It is the one node that can grant another box
CPU, memory, or secrets, so it is never itself a YOLO agent. Everything else
runs unattended inside a box.

> **Scope.** A single-operator tool for running Claude Code in Docker on macOS.
> All boxes share one Claude login; the host is assumed to be your Mac (Docker
> Desktop). Network egress is **not** yet sandboxed — see *Not yet*.

## The contract

> The sandbox must give you **more** liberty than plain `claude`, not less.

- **Generous inside the box.** The agent installs packages, roams the repo,
  uses its scoped credentials, hits the network — all with **zero permission
  prompts**, unattended. You never click "allow."
- **Hard wall at the edge.** It cannot see other repos, other secrets, your
  personal gcloud account, or the host filesystem/shell. Blast radius = one
  repo + the slice of credentials it was granted.
- **Fast path to widen the inside.** Need another secret, domain, or more
  memory? One line in `fleet.json`. Never a code change.

If the box is ever so tight the agent keeps hitting walls it genuinely needs,
that's a bug against the contract — widen it, don't babysit it.

## Two manifests, split by who owns the fact

| File | Lives in | Owns | Example |
|---|---|---|---|
| `gent.json` | the **target repo** | its *requirements* — which secrets/egress/tools it needs | "needs `gcp` creds, `api.example.com`" |
| `fleet.json` | **here** | the *machine's grants* — cpu/mem, where each named secret resolves, approval | "myapp → 2 cpus, 3g, allow: true" |

The repo declares what it needs (versioned with its code, reviewed in its own
PR — no magic). This machine decides what it actually gets. A repo can never
grant itself more than the host approves. Same split as GCP: the app declares
the roles it needs; the platform sets quota and binds the service account.

## Usage

Nothing is implicit: bare **`gent`** just prints usage, like bare `git`. Every
effect is named. Put it on your PATH once:

```bash
ln -s "$PWD/gent" ~/bin/gent       # once, from the repo root — put it on PATH
cp fleet.example.json fleet.json   # then edit: add your repos + their grants
```

`fleet.json` is gitignored (it's your machine's private grants); the checked-in
`fleet.example.json` is the template.

Per-repo box commands (the repo defaults to the cwd's repo when omitted):

```bash
gent up     myapp      # create + start a repo's agent (persistent, detached)
gent attach myapp      # drop into its live claude session (tmux)
gent shell  myapp      # a separate shell in the box (or `-- cmd` for a one-off)
gent peek   myapp      # snapshot the session without attaching
gent logs   myapp      # container/bootstrap logs
gent down   myapp      # stop + remove (home volume kept; --wipe to clear it)
```

From inside a configured repo, drop the name:

```bash
cd ~/Projects/myorg/myapp
gent up && gent attach  # bring it up, then live in myapp's box
```

Fleet/host admin lives under `gent fleet` (run rarely), with `gent ls` as a
shortcut for the daily glance:

```bash
gent ls                 # fleet overview (= `gent fleet ls`)
gent fleet up           # bring up every approved box not already running
gent fleet down         # stop + remove all boxes (home volumes kept)
gent fleet doctor       # check docker, base image, shared login, secret dirs
gent fleet build        # build the base image
gent fleet auth         # shared-login status
```

`fleet up` skips `allow: false` repos and already-running boxes, and never
aborts the batch on one repo's failure. It's mainly for a fresh checkout or
after a `fleet down` — `--restart unless-stopped` already revives boxes when
the Docker daemon starts. `fleet down` keeps every home volume on purpose;
wipe deliberately, per box, with `gent down <repo> --wipe`.

## Attach feels native — the box runs the *real* claude CLI

There is no thin-client for Claude Code: when you `gent attach`, the genuine
`claude` CLI is running in the box and you're driving it over `docker exec` +
tmux. Fidelity is capped by the **weakest link in the chain**, so two one-time
choices make it feel like a local `claude`:

- **Box side (done):** `/etc/tmux.conf` strips tmux's UI (no status bar,
  truecolor, OSC52 copy-out, extended keys, mouse) so the multiplexer is
  invisible. Baked into the base image.
- **Host side (your terminal):** **Terminal.app is the bottleneck** — 256-color
  only, no OSC52, weak key encoding. Use a modern GPU terminal: **Ghostty**
  (recommended — native, zero-fuss), or kitty / WezTerm / iTerm2. All give
  truecolor + clipboard-out + Shift+Enter.

The one thing no terminal fixes: pasting an image/file *into* the box. Dragging
a file inserts its **host** path, which the container can't see (the sandbox
wall). Copy *out* works; paste *in* is a remote limitation — drop the file into
the repo dir (it's mounted) and reference it by path.

## Auth for the in-box claude — `/login` once, in any box

All boxes share **one** Claude config dir: `claude-home/` in the catalog
(`~/.gent/state/secrets/claude-home`) is bind-mounted as `~/.claude` in every
box — the login, creds, and settings, exactly as your host's many `claude`
sessions share a single `~/.claude`. So you log in **once**: `gent attach` into
any box, run `/login`, and every box is then logged in. Refreshes propagate to
all (it's the same dir) — no per-box login, no daemon, nothing copied at start.
(Per-repo **memory and transcripts** are the exception — those come from your
host's `~/.claude`, not this shared dir; see below.)

It's a full OAuth login, which is what makes **Remote Control** work in a box
(reachable from the phone app); the inference-only `setup-token` /
`CLAUDE_CODE_OAUTH_TOKEN` cannot. This shared box login is **independent of your
host's own** Claude login (a separate `~/.claude`) — the fleet has its own.

`gent fleet auth` just reports whether that shared login exists yet.

## Your own host session — `gent host` (idle-proof Remote Control)

Boxes stay reachable from the phone while idle for two reasons: a supervisor
relaunches a crashed claude, and an *active* box re-mints its 8h OAuth token as a
side effect of its own API calls, so **Remote Control** never lapses. A claude
you run **on the host** by hand — outside any box — has neither. Left idle past
token expiry, its Remote Control socket drops and doesn't come back until the
session restarts (the phone can't poke a dropped socket).

`gent host` gives that host session the same two safety nets — one command:

```
gent host up        # tmux + self-healing supervisor + keepalive, all armed
gent host down      # stop it (kill tmux + reap any orphaned claude)
gent host status    # session state + token expiry + keepalive job
gent host attach    # join the session
```

- **`up`** runs `claude` in a tmux session under a supervisor that relaunches it
  on exit and resumes the newest transcript, so the phone reconnects the *same*
  session. Run it from the dir whose conversation you want to resume. It also
  arms the launchd keepalive automatically (idempotent; re-run after a gent
  update to re-arm). `gent host install-keepalive` stays for arming it by hand.
- **keepalive** (a child of the supervisor, plus the launchd job `up` installs) is the
  host-side twin of `gent fleet refresh-auth`: when the token nears expiry it
  **pokes** an idle session with a one-line, ignore-me prompt to force an in-place
  re-mint (skipped if the session is busy — it's re-minting itself); only once the
  token has actually lapsed (e.g. after the laptop slept) does it **bounce** claude
  so the supervisor relaunches it and reconnects Remote Control.

Set the session name with `"host": {"name": "…"}` in `fleet.json`. Needs tmux on
the host (`brew install tmux`); on macOS the token is read from the login
Keychain, elsewhere from `~/.claude/.credentials.json`.

## Per-repo memory & history — one brain per repo, shared with your host

A box mounts its repo at the **real host absolute path** (e.g.
`/Users/you/Projects/acme/api`), not a fixed `/repo`, and works from there. That
makes the box a path-faithful subset of your machine: Claude keys per-project
state (memory, transcripts) by the working directory, so the box and your own
host sessions for that repo compute the **same** key. gent then binds your
host's `~/.claude/projects/<that-key>/` straight into the box, so the two share
**one** per-repo memory and one transcript history — what the autonomous box
learns, your host sessions see, and vice versa.

This is also what keeps memory **un-scrambled**. The shared `claude-home` login
is one dir across the whole fleet; if every box worked from the same `/repo`
path they'd all collapse into a single `projects/-repo/` namespace and stomp on
each other's memory. The real-path mount gives every repo a distinct key, and
the host bind keeps each repo's brain in exactly one place. `claude-home` still
supplies the fleet login/creds/settings; only each repo's own project subtree is
host-backed.

> Caveat: all boxes share one OAuth credential. If the provider rotates refresh
> tokens, one box's refresh could log the others out → just `/login` again in
> any box. Rare, but watch for it.

## Secrets — a shared catalog, not per-project silos

`fleet.json:secrets_root` (default `~/.gent/state/secrets`) is a **catalog**
of reusable credentials and config — files *and* `.env` fragments. One
`gcp/myproject-sa.json` can serve every repo in that GCP project; one API-key
fragment can be shared by everything that talks to that service. Organize it by
*resource*, not by consumer.

Isolation happens in the **grant**, not the directory: a box sees only the
catalog entries its `fleet.json` entry references. Three carriers:

| key in fleet.json | effect |
|---|---|
| `mounts` | `{ "/container/path": "catalog/rel" }` → mounted read-only |
| `env_files` | `[ "catalog/rel.env" ]` → `--env-file` (reusable fragments) |
| `env` | `{ "KEY": "value" }` → inline `-e` (project IDs, locations) |

A secret already gitignored inside a repo's own tree (e.g. a repo's own
`var/token`) needs no catalog entry — it rides along with the repo mount.
Prefer **env vars** for keys/IDs and **mounts** for credential files.

### Provisioning a scoped GCP service account (per-repo identity)

Each box gets its *own* SA — never the shared host `~/.config/gcloud` (which
holds your personal + every project's creds at once). Example for a box that
needs a project's Vertex AI + Secret Manager:

```bash
PROJECT=my-project
SA=myapp-gent
gcloud iam service-accounts create "$SA" --project "$PROJECT" \
  --display-name "myapp gents sandbox"
for ROLE in roles/aiplatform.user roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
    --role "$ROLE"
done
mkdir -p ~/.gent/state/secrets/gcp
gcloud iam service-accounts keys create \
  ~/.gent/state/secrets/gcp/myapp-sa.json \
  --iam-account "${SA}@${PROJECT}.iam.gserviceaccount.com"
chmod 600 ~/.gent/state/secrets/gcp/myapp-sa.json
```

### Git push/pull — a per-repo deploy key, not your personal SSH key

Boxes never get your personal `~/.ssh` key (it's the master key to every repo
you can touch — the opposite of "hard wall at the edge"). Instead each box gets
its **own GitHub deploy key, write-scoped to that one repo** — the same
per-repo-identity philosophy as the scoped GCP SAs. `gent up` mints and
registers it idempotently (via `gh`, no GitHub-UI clicking); `gent deploy-key
<repo>` does just that step ahead of time.

- **Outside (catalog):** `ssh/<repo>-deploy` — one private key per repo.
- **Inside the box:** mounted at a fixed, repo-agnostic path; bootstrap installs
  it as `~/.ssh/id_ed25519`, so `git push` just works (host key already trusted
  fleet-wide via `/etc/ssh/ssh_known_hosts`). The box only knows "the key."

A deploy key attaches to exactly one repo, and each box is one repo — a clean
1:1. Registration needs admin on the repo; if you only have write access, the
mint is non-fatal (warns) and you'd fall back to a fine-grained PAT.

### GitHub API — `gh` + a per-owner **read-only** token

The deploy key covers `git push`, but **not** the GitHub API: *reading* a PR's
title, diff, threads, and CI status, or scanning issues. That's `gh`, and `gh`
needs a token over HTTPS — the SSH key can't drive it. Without one a box can
only `git fetch pull/N/head` and read the raw diff, blind to everything else.

A fine-grained PAT is scoped to **one resource owner**, which lines up with the
catalog's organize-by-resource rule. Mint one read-only PAT per owner and drop
it under `github/<owner>.env`:

```bash
umask 077; echo 'GH_TOKEN=github_pat_…' > ~/.gent/state/secrets/github/myorg.env
```

Then grant it to that owner's boxes via the normal `env_files` carrier — it's a
plain catalog fragment, nothing special:

```jsonc
"myapp": { "env_files": ["github/myorg.env"], … }   // a myorg-owned repo
```

`gh` reads `GH_TOKEN` with no `gh auth login`; a box that lists a missing
env_file won't come up (`gent up` fails fast on it). A box under a **different**
owner (e.g. `otherorg/otherapp`) simply lists no github token — the `myorg` PAT
wouldn't authorize its repo anyway — until you mint `github/otherorg.env` for
it. This keeps the same per-grant isolation as the deploy keys: a box reaches
only the owner it's granted.

Keep these tokens **read-only** — Pull requests + Issues: *read*, Contents: *no
access* — and that's deliberate, not timid. A **write** scope would be a
*second* push path that defeats the whole point of the per-repo deploy key.
Read-only keeps the deploy key the **only** way a box can write. The box already
has its own code on disk (the repo bind mount) and pushes via the deploy key, so
it needs no Contents permission at all. Set an expiry you'll renew.

**If a box genuinely must post to GitHub** (open a PR, leave a review comment),
give *that one box* a write-scoped token in its own repo's `env_files` (it's
applied last, so it overrides) — the blast radius stays that single repo, the
same per-repo-identity discipline as the deploy keys.

### Vercel — `vercel` + a per-team token

Same shape as `gh`, one rung less safe. Repos with a Vercel frontend drive it
through the `vercel` CLI (link, raw `vercel api`, `env pull`/`push` to prod,
deploy hooks), and a token without a CLI does nothing — so `vercel` ships in the
base image. Auth is a single **token string**: the CLI auto-reads `VERCEL_TOKEN`
(no `vercel login`, no mounted key file like GCP), so it rides the catalog as a
plain `env_files` fragment, organized by **Vercel team** (not GitHub owner — the
two don't have to match; e.g. two repos under different GitHub owners can both
link projects that live in one personal Vercel team, so they share its token):

```bash
umask 077; echo 'VERCEL_TOKEN=…' > ~/.gent/state/secrets/vercel/myteam.env
```

```jsonc
"myapp": { "env_files": ["vercel/myteam.env"], "env": { "VERCEL_SCOPE": "team_xxxxxxxxxxxxxxxxxxxxxxxx" }, … }
```

`VERCEL_SCOPE` pins the team for account-level `vercel api` calls that aren't
anchored to a linked project (`.vercel/project.json` carries the team for the
rest). Use the team **ID**, not the slug: if your personal-account username
equals the team slug (the same word for both), the username wins and
`--scope <slug>` resolves to your empty personal account ("You cannot set your
Personal Account as the scope"). The `team_…` ID is unambiguous — read it from
any linked project's `.vercel/project.json` (`orgId`) or `vercel teams ls`.

The caveat that makes this *less* safe than the read-only `gh` token: a stock
Vercel token (Hobby/Pro) **can't be scoped to one project or reduced to read** —
project/permission scoping is Enterprise-only. So within its team a token is
**full access**: deploy, rewrite prod env, add/remove domains, delete projects.
You can't reproduce the GCP gradient (a read-only dev SA next to a full-deploy
deployer SA) here. The levers you do have: one token **per
account**, routed to boxes via `env_files` so a leak is revoked in one place;
keep teams small so the blast radius is naturally bounded; and revoke at
`vercel.com/account/tokens` to rotate.

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
inside the box (so a crash self-heals, like the claude window). The command
runs from the repo's mount path, so the daemon executes the repo's **current source** — edit
the code and restart the window (or `gent down/up`) to reload, no image rebuild.
A repo's own first-run `.gent/setup.sh` (best-effort, runs before the services)
is the place to install their deps.

Typical shape: a repo whose agent coordinates an external service (a chat bot, a
message queue, a home-automation hub). The always-on backend lives on the host
or in the cloud; the box is the *agent* — the claude session plus a thin daemon
(a poller or event bridge) that feeds it. Anything LAN- or device-bound stays on
the host, reachable from the box at `host.docker.internal`.

## Derived build outputs — the box builds its own, the host keeps its own

A repo's files are either **source** (what git tracks — OS-neutral text) or
**derived** (built *from* source by a tool: `node_modules`, `.venv`, `target/`,
`dist/`). The repo bind mount shares source live with the host — that's the
point. But a derived artifact is platform-specific: a macOS `node_modules` is
*wrong* on a Linux box, and sharing one dir across both OSes means a `pnpm
install` on either side wipes the other (it has happened). Source is the
contract; derived is each environment's own business.

So a box **builds its own** derived artifacts on a private volume. Declare them
in the repo's `gent.json` as `{ path: build-command }`:

```json
"derived": { "node_modules": "pnpm install --frozen-lockfile" }
```

For each entry gents mounts a per-box named volume at `<repo>/<path>` — which
**masks** the host's copy (your host checkout is never touched and keeps
working) — then `chown`s it and runs the build before claude/services start.
Wiped with the home volume by `gent down <repo> --wipe`. Repos with no `derived`
are unaffected. It's the general primitive; the build command owns all
tool-specific detail (Rust → `{"target": "cargo build"}`, etc.).

For pnpm/node the image enables **corepack** (so `pnpm` resolves to each repo's
pinned `packageManager` version) and points `npm_config_store_dir` at a
**fleet-shared store volume** (`/pnpm-store`): every package version is
downloaded *once across all boxes* and reused. The store and a box's private
`node_modules` are separate mounts, so pnpm *copies* in (a hardlink would cross
a mount → `EXDEV`) — the win is no re-downloads, not on-disk dedup.

> One-time host step: because the host's own `node_modules` was masked (not
> built) by the box, run your normal install once on the host (e.g. `pnpm
> install`) to make the host checkout pristine too. From then on the two never
> collide — the box writes its real deps to its volume; only inert, relative,
> lockfile-pinned workspace symlinks land in the shared tree.

## Periodic context refresh — nudge, the model clears itself

A long-lived box accretes context: hours of turns the agent no longer needs but
keeps paying for. The fix is a periodic `/clear` — but a blind, timed clear can
nuke a box mid-task, before it's written down what mattered. So gents splits it
the way pickle does: a timer only **nudges**, and the **model** decides when it's
actually safe to clear and pulls the trigger itself.

Opt a box in via its `gent.json`:

```json
"clear": { "every": "6h" }
```

On that cadence the box's pane gets a one-line nudge — *"good stopping point?
wrap up, commit, save memory, then run `gent-clear`."* Nothing is cleared by the
timer. The agent, when **it** judges it's ready (work checkpointed, anything
durable committed or saved to memory), runs the in-box command **`gent-clear`**,
which does the mechanical part: send `/clear` into its own pane, then replay the
box's hello (below) to re-orient the fresh session. Mid-task? It ignores the
nudge and gets poked again next interval. `every` takes `s`/`m`/`h`/`d` suffixes
(bare number = seconds); no `clear` key → no nudge, the default.

The split is the point: the supervisor judges *cadence* (cheap, wall-clock), the
model judges *readiness* (what's owed, what's saved) — neither can clear an
unprepared box.

### A custom "hi" per box — `.gent/hello.md`

Drop a `.gent/hello.md` in the repo and gents replays it into the claude pane as
a prompt — the box's standing brief: who it is, what to resume watching, where
its work lives. It fires at two moments:

- **on box startup** (`gent up`) — once claude's TUI has settled, so a fresh box
  is oriented/kicked without you attaching, and
- **after `gent-clear`** — a `/clear` wipes the conversation, including whatever
  the agent was watching (any `Monitor`), so the hello re-orients the empty
  session.

It's versioned with the repo like `.gent/setup.sh`, multi-line is fine (it's
pasted as one prompt). No `.gent/hello.md` → nothing is replayed (the default —
claude still reloads the repo's `CLAUDE.md` either way). The same primitive is
exposed as the in-box command `gent-hello` if you ever want to re-send it by
hand.

## Not yet (deliberately deferred)

- **Egress allowlist enforcement — not implemented.** `gent.json:needs.egress`
  is *documentation only* today: every box has **open outbound network**. The
  "hard wall at the edge" holds for the filesystem, secrets, and other repos —
  but **not** for network egress yet. Treat outbound as unrestricted until a
  firewalling pass lands.
- **Host-job bridge** — a DB-queue + typed-worker for native host compute, built
  when a repo first needs local native execution.
