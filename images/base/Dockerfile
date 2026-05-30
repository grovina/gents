# gents base image — a generous, self-contained agent sandbox.
# This is a FOUNDATION, not a per-repo image. Repo-specific extras the agent
# installs itself inside its own writable container (that's the contract:
# generous inside). Resist the urge to fork this per repo.
FROM python:3.13-bookworm

# Node 22 (Claude Code runtime) + the common toolbelt.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git openssh-client tmux ripgrep jq less build-essential sudo ffmpeg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
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
# auto-update it. + python package managers.
ENV NPM_CONFIG_PREFIX=/opt/npm-global
ENV PATH=/opt/npm-global/bin:$PATH
RUN npm install -g @anthropic-ai/claude-code \
 && pip install --no-cache-dir pdm uv

# Privacy: error reporting (Sentry crash reports) off. Telemetry stays ON —
# Claude Code gates Remote Control eligibility behind its feature-flag (Statsig)
# check, which rides the telemetry channel; DISABLE_TELEMETRY=1 would silently
# kill Remote Control (the /remote-control command never registers). Auto-update
# stays ON (newest is best).
# UTF-8 locale so claude's TUI glyphs render (default debian locale is POSIX/C).
ENV DISABLE_ERROR_REPORTING=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NPM_CONFIG_CACHE=/cache/npm \
    PIP_CACHE_DIR=/cache/pip \
    UV_CACHE_DIR=/cache/uv

# Strip tmux's UI fleet-wide so attaching to a box feels like a local terminal,
# not a multiplexer (truecolor, OSC52 copy-out, extended keys all pass through).
COPY tmux.conf /etc/tmux.conf
COPY bootstrap /usr/local/bin/bootstrap
# Non-root by default (keeps bind-mount file ownership sane, shrinks the
# default blast radius) but with passwordless sudo so the agent can install
# anything it needs inside — "generous inside the box".
RUN chmod +x /usr/local/bin/bootstrap \
 && useradd -m -s /bin/bash -u 1000 gent \
 && echo 'gent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/gent \
 && chown -R gent:gent /opt/npm-global \
 && ln -sf /opt/npm-global/bin/claude /usr/local/bin/claude \
 && echo 'export PATH=/opt/npm-global/bin:$PATH' > /etc/profile.d/gents-path.sh

USER gent
WORKDIR /repo
ENTRYPOINT ["/usr/local/bin/bootstrap"]
