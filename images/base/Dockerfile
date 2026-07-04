# gents base image — a generous, self-contained agent sandbox.
# This is a FOUNDATION, not a per-repo image. Repo-specific extras the agent
# installs itself inside its own writable container (that's the contract:
# generous inside). Resist the urge to fork this per repo.
FROM python:3.13-bookworm

# Node 22 (Claude Code runtime) + the common toolbelt.
# gh (GitHub CLI) is here because the in-box SSH deploy key only covers
# `git push` — it can't drive the GitHub API. Agents routinely need to READ a
# PR's metadata/diff/CI to review it (e.g. "review PR #1, find gaps"); gh + a
# fleet-wide read-only GH_TOKEN (wired in `gent up`) is what makes that work,
# instead of falling back to a bare `git fetch pull/N/head` that sees no
# title, threads, checks, or review state. Installed from GitHub's own apt repo
# (Debian stable ships no/old gh).
#
# gcloud (google-cloud-cli, from Google's own apt repo) is the same story for
# GCP: a GCP-provisioned box mounts a service-
# account key + GOOGLE_APPLICATION_CREDENTIALS, but that only buys ADC for the
# repo's own client libraries — the agent still needs the gcloud CLI to actually
# DO ops (logging read, run deploy, secrets — gsutil + bq ride along in the same
# package). Credentials without a CLI is exactly the gap gh closed. No-GCP boxes
# carry the weight unused — the same trade already accepted for ffmpeg. gnupg is
# pulled in only so apt can dearmor Google's signing key.
#
# vim+unzip fill two small gaps: a box had no editor for `gent shell`, and unzip
# is a frequent stumble.
#
# postgresql-client (psql) is the DB sibling of the gh/gcloud/vercel story: a
# DB-touching box reaches Postgres through a tunnel and the repo tooling shells
# out to `psql` for migrations/grants/queries. No-DB boxes
# carry it unused — the same trade already accepted for gcloud/ffmpeg.
#
# git-lfs is a git extension, so it belongs in the base rather than per-repo: a
# box carrying an LFS-tracked repo (homer) can't clone/checkout its large files
# without it — a bare `git checkout` leaves LFS pointer stubs on disk. Installed
# fleet-wide + `git lfs install --system` below (registers the smudge/clean
# filters in /etc/gitconfig) so it works for every box's gent user. No-LFS boxes
# carry it unused — the same trade already accepted for gcloud/ffmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg git git-lfs openssh-client tmux ripgrep jq less build-essential sudo ffmpeg unzip vim postgresql-client \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs gh google-cloud-cli \
 && git lfs install --system \
 && rm -rf /var/lib/apt/lists/*

# Trust GitHub's SSH host key once, fleet-wide, so git-over-ssh never stops on
# "authenticity of host 'github.com' can't be established" inside a box.
# System-wide (/etc/ssh) so a home-volume mount can't shadow it, and PINNED to
# GitHub's published Ed25519 key — deterministic, not ssh-keyscan trust-on-
# first-use. If GitHub ever rotates it (rare), update this one line:
#   https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
RUN echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' \
      >> /etc/ssh/ssh_known_hosts

# Claude Code into a gent-WRITABLE prefix (/opt, not /home so home-volumes don't
# shadow it, not /usr/lib which is root-only) so the non-root user can
# auto-update it. + python package managers. + `corepack enable` so `pnpm`/`yarn`
# resolve deterministically to the version each repo pins in package.json's
# `packageManager` field (corepack fetches + caches it on first use), instead of
# relying on a stray global install that vanishes when a box is recreated.
#
# vercel is the npm-native sibling of the gh/gcloud "credentials need a CLI"
# story: a Vercel-deploying box carries a VERCEL_TOKEN in env, but a token without
# a CLI does nothing. The repo tooling shells straight out to `vercel` (link, raw
# `vercel api`, env pull/push to prod, deploy hooks), which auto-reads
# VERCEL_TOKEN — no `vercel login`, no mounted credential file (the auth IS just
# the token string). No-Vercel boxes carry it unused, the same trade already
# accepted for gcloud/ffmpeg.
ENV NPM_CONFIG_PREFIX=/opt/npm-global
ENV PATH=/opt/npm-global/bin:$PATH
RUN npm install -g @anthropic-ai/claude-code vercel \
 && pip install --no-cache-dir pdm uv \
 && corepack enable

# Privacy: error reporting (Sentry crash reports) off. Telemetry stays ON —
# Claude Code gates Remote Control eligibility behind its feature-flag check
# (GrowthBook — cached in ~/.claude.json as cachedGrowthBookFeatures), which
# rides the telemetry channel; DISABLE_TELEMETRY=1 would silently kill Remote
# Control (the /remote-control command never registers). Auto-update stays ON
# (newest is best).
# UTF-8 locale so claude's TUI glyphs render (default debian locale is POSIX/C).
# COLORTERM=truecolor is the lever that actually gives the in-box claude 24-bit
# color: under tmux it sees TERM=tmux-256color, and Node's supports-color decides
# truecolor almost entirely from COLORTERM. Without it claude renders in 256-color
# and its subtle theme backgrounds (inline code, dim recap) degrade to harsh
# reverse-video blocks. tmux.conf's `terminal-features RGB` only feeds RGB UPWARD
# to the host terminal — this is the half that reaches claude itself.
# npm_config_store_dir points pnpm at the fleet-shared store (gents mounts the
# gent-pnpm-store volume there) so a package version is downloaded once fleet-wide;
# the explicit path also stops pnpm's "store on another drive" fallback that
# stranded a store inside /repo. COREPACK_ENABLE_DOWNLOAD_PROMPT=0 lets corepack
# fetch a repo's pinned pnpm non-interactively (boxes are unattended).
ENV DISABLE_ERROR_REPORTING=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    COLORTERM=truecolor \
    NPM_CONFIG_CACHE=/cache/npm \
    PIP_CACHE_DIR=/cache/pip \
    UV_CACHE_DIR=/cache/uv \
    npm_config_store_dir=/pnpm-store \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0

# Strip tmux's UI fleet-wide so attaching to a box feels like a local terminal,
# not a multiplexer (truecolor, OSC52 copy-out, extended keys all pass through).
COPY tmux.conf /etc/tmux.conf
COPY bootstrap /usr/local/bin/bootstrap
# gent-hello / gent-clear: the in-box re-greet + context-reset commands. gent-hello
# replays the repo's .gent/hello.md into the claude pane (the bootstrap fires it on
# startup; gent-clear fires it after a /clear). On PATH so both just work from the
# model's Bash tool.
COPY gent-hello /usr/local/bin/gent-hello
COPY gent-clear /usr/local/bin/gent-clear
# gent-events / gent-schedule: the box's unified event bus + durable scheduler.
# events.jsonl is ONE stream (telegram + reminders + …) the agent watches via one
# Monitor; gent-schedule lets the agent leave durable, cross-session reminders that
# fire from a box-supervised daemon (no at/cron/systemd on the box).
COPY gent-events /usr/local/bin/gent-events
COPY gent-schedule /usr/local/bin/gent-schedule
# Non-root by default (keeps bind-mount file ownership sane, shrinks the
# default blast radius) but with passwordless sudo so the agent can install
# anything it needs inside — "generous inside the box".
RUN chmod +x /usr/local/bin/bootstrap /usr/local/bin/gent-hello /usr/local/bin/gent-clear \
 && chmod +x /usr/local/bin/gent-events /usr/local/bin/gent-schedule \
 && useradd -m -s /bin/bash -u 1000 gent \
 && echo 'gent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/gent \
 && chown -R gent:gent /opt/npm-global \
 && ln -sf /opt/npm-global/bin/claude /usr/local/bin/claude \
 && echo 'export PATH=/opt/npm-global/bin:$PATH' > /etc/profile.d/gents-path.sh

USER gent
# Neutral default workdir: gent mounts each repo at its real host absolute path
# and the bootstrap cd's there (GENT_REPO_DIR), so there's no fixed repo path to
# pin here — /home/gent always exists and keeps an interactive `gent shell` sane.
WORKDIR /home/gent
ENTRYPOINT ["/usr/local/bin/bootstrap"]
