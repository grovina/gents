# Secrets & credentials

## A shared catalog, not per-project silos

`fleet.json:secrets_root` (default `~/.gent/state/secrets`) is a **catalog** of
reusable credentials and config — files *and* `.env` fragments. One
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
`var/token`) needs no catalog entry — it rides along with the repo mount. Prefer
**env vars** for keys/IDs and **mounts** for credential files.

## Scoped GCP service account (per-repo identity)

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

## Git push/pull — a per-repo deploy key, not your personal SSH key

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

## GitHub API — `gh` + a per-owner **read-only** token

The deploy key covers `git push`, but **not** the GitHub API: *reading* a PR's
title, diff, threads, and CI status, or scanning issues. That's `gh`, and `gh`
needs a token over HTTPS — the SSH key can't drive it. Without one a box can only
`git fetch pull/N/head` and read the raw diff, blind to everything else.

A fine-grained PAT is scoped to **one resource owner**, which lines up with the
catalog's organize-by-resource rule. Mint one read-only PAT per owner and drop
it under `github/<owner>.env`:

```bash
umask 077; echo 'GH_TOKEN=github_pat_…' > ~/.gent/state/secrets/github/myorg.env
```

Then grant it to that owner's boxes via the normal `env_files` carrier:

```jsonc
"myapp": { "env_files": ["github/myorg.env"], … }   // a myorg-owned repo
```

`gh` reads `GH_TOKEN` with no `gh auth login`; a box that lists a missing
env_file won't come up (`gent up` fails fast on it). A box under a **different**
owner simply lists no github token until you mint one for it — the same
per-grant isolation as the deploy keys.

Keep these tokens **read-only** — Pull requests + Issues: *read*, Contents: *no
access*. A **write** scope would be a *second* push path that defeats the
per-repo deploy key; read-only keeps the deploy key the **only** way a box can
write. Set an expiry you'll renew.

**If a box genuinely must post to GitHub** (open a PR, leave a review comment),
give *that one box* a write-scoped token in its own repo's `env_files` (it's
applied last, so it overrides) — the blast radius stays that single repo.

## Vercel — `vercel` + a per-team token

Same shape as `gh`, one rung less safe. Repos with a Vercel frontend drive it
through the `vercel` CLI (link, raw `vercel api`, `env pull`/`push` to prod,
deploy hooks), and a token without a CLI does nothing — so `vercel` ships in the
base image. Auth is a single **token string**: the CLI auto-reads `VERCEL_TOKEN`
(no `vercel login`, no mounted key file), so it rides the catalog as a plain
`env_files` fragment, organized by **Vercel team** (not GitHub owner — the two
don't have to match):

```bash
umask 077; echo 'VERCEL_TOKEN=…' > ~/.gent/state/secrets/vercel/myteam.env
```

```jsonc
"myapp": { "env_files": ["vercel/myteam.env"], "env": { "VERCEL_SCOPE": "team_xxxxxxxxxxxxxxxxxxxxxxxx" }, … }
```

`VERCEL_SCOPE` pins the team for account-level `vercel api` calls that aren't
anchored to a linked project (`.vercel/project.json` carries the team for the
rest). Use the team **ID**, not the slug: if your personal-account username
equals the team slug, the username wins and `--scope <slug>` resolves to your
empty personal account. Read the `team_…` ID from any linked project's
`.vercel/project.json` (`orgId`) or `vercel teams ls`.

The caveat that makes this *less* safe than the read-only `gh` token: a stock
Vercel token (Hobby/Pro) **can't be scoped to one project or reduced to read** —
project/permission scoping is Enterprise-only. So within its team a token is
**full access**: deploy, rewrite prod env, add/remove domains, delete projects.
The levers you do have: one token **per account**, routed via `env_files` so a
leak is revoked in one place; keep teams small so the blast radius is bounded;
and revoke at `vercel.com/account/tokens` to rotate.
