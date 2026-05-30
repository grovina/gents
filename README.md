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
(`~/.config/agent-secrets/claude-home`) is bind-mounted as `~/.claude` in every
box — creds, settings, and sessions alike, exactly as your host's many `claude`
sessions share a single `~/.claude`. So you log in **once**: `gent attach` into
any box, run `/login`, and every box is then logged in. Refreshes propagate to
all (it's the same dir) — no per-box login, no daemon, nothing copied at start.

It's a full OAuth login, which is what makes **Remote Control** work in a box
(reachable from the phone app); the inference-only `setup-token` /
`CLAUDE_CODE_OAUTH_TOKEN` cannot. This shared box login is **independent of your
host's own** Claude login (a separate `~/.claude`) — the fleet has its own.

`gent fleet auth` just reports whether that shared login exists yet.

> Caveat: all boxes share one OAuth credential. If the provider rotates refresh
> tokens, one box's refresh could log the others out → just `/login` again in
> any box. Rare, but watch for it.

## Secrets — a shared catalog, not per-project silos

`fleet.json:secrets_root` (default `~/.config/agent-secrets`) is a **catalog**
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
mkdir -p ~/.config/agent-secrets/gcp
gcloud iam service-accounts keys create \
  ~/.config/agent-secrets/gcp/myapp-sa.json \
  --iam-account "${SA}@${PROJECT}.iam.gserviceaccount.com"
chmod 600 ~/.config/agent-secrets/gcp/myapp-sa.json
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
umask 077; echo 'GH_TOKEN=github_pat_…' > ~/.config/agent-secrets/github/grovina.env
```

Then grant it to that owner's boxes via the normal `env_files` carrier — it's a
plain catalog fragment, nothing special:

```jsonc
"axt": { "env_files": ["github/grovina.env"], … }   // a grovina-owned repo
```

`gh` reads `GH_TOKEN` with no `gh auth login`; `gent fleet doctor` flags whether
`github/grovina.env` is present (a box that lists a missing env_file won't come
up). A box under a **different** owner (e.g. `cavyai/radios`) simply lists no
github token — the grovina PAT wouldn't authorize its repo anyway — until you
mint `github/cavyai.env` for it. This keeps the same per-grant isolation as the
deploy keys: a box reaches only the owner it's granted.

Keep these tokens **read-only** — Pull requests + Issues: *read*, Contents: *no
access* — and that's deliberate, not timid. A **write** scope would be a
*second* push path that defeats the whole point of the per-repo deploy key.
Read-only keeps the deploy key the **only** way a box can write. The box already
has its own code on disk (the `/repo` mount) and pushes via the deploy key, so
it needs no Contents permission at all. Set an expiry you'll renew.

**If a box genuinely must post to GitHub** (open a PR, leave a review comment),
give *that one box* a write-scoped token in its own repo's `env_files` (it's
applied last, so it overrides) — the blast radius stays that single repo, the
same per-repo-identity discipline as the deploy keys.

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
runs from `/repo`, so the daemon executes the repo's **current source** — edit
the code and restart the window (or `gent down/up`) to reload, no image rebuild.
A repo's own first-run `.gent/setup.sh` (best-effort, runs before the services)
is the place to install their deps.

Typical shape: a repo whose agent coordinates an external service (a chat bot, a
message queue, a home-automation hub). The always-on backend lives on the host
or in the cloud; the box is the *agent* — the claude session plus a thin daemon
(a poller or event bridge) that feeds it. Anything LAN- or device-bound stays on
the host, reachable from the box at `host.docker.internal`.

## Not yet (deliberately deferred)

- **Egress allowlist enforcement — not implemented.** `gent.json:needs.egress`
  is *documentation only* today: every box has **open outbound network**. The
  "hard wall at the edge" holds for the filesystem, secrets, and other repos —
  but **not** for network egress yet. Treat outbound as unrestricted until a
  firewalling pass lands.
- **Host-job bridge** — a DB-queue + typed-worker for native host compute, built
  when a repo first needs local native execution.
