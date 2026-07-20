# Auth & per-repo memory

## In-box login — `/login` once, in any box

All boxes share **one** Claude config dir: `claude-home/` in the catalog
(`~/.gent/state/secrets/claude-home`) is bind-mounted as `~/.claude` in every
box — the login, creds, and settings, exactly as your host's many `claude`
sessions share a single `~/.claude`. So you log in **once**: `gent attach` into
any box, run `/login`, and every box is then logged in. Refreshes propagate to
all (it's the same dir) — no per-box login, no daemon, nothing copied at start.
(Per-repo memory and transcripts are the exception — see below.)

It's a full OAuth login, which is what makes **Remote Control** work in a box
(reachable from the phone app); the inference-only `setup-token` /
`CLAUDE_CODE_OAUTH_TOKEN` cannot. This shared box login is **independent of your
host's own** Claude login (a separate `~/.claude`) — the fleet has its own.
`gent fleet auth` reports whether it exists yet.

> Caveat: all boxes share one OAuth credential. If the provider rotates refresh
> tokens, one box's refresh could log the others out → just `/login` again in
> any box. Rare, but watch for it.

## Your own host session — `gent host` (idle-proof Remote Control)

Boxes stay reachable from the phone while idle for two reasons: a supervisor
relaunches a crashed claude, and an *active* box re-mints its 8h OAuth token as
a side effect of its own API calls, so **Remote Control** never lapses. A claude
you run **on the host** by hand has neither — left idle past token expiry, its
Remote Control socket drops and doesn't come back until the session restarts.
`gent host` gives that host session the same safety nets — and adds a third so it
survives a reboot:

```
gent host up        # tmux + self-healing supervisor + keepalive + boot autostart, all armed
gent host down      # stop it (kill tmux + reap any orphaned claude)
gent host status    # session state + token expiry + keepalive & boot-autostart jobs
gent host attach    # join the session (`gent attach host` is the same thing)
```

The session also shows as a row in `gent ls`, next to the boxes — it's part of the
fleet, not a thing off to the side.

- **`up`** runs `claude` in a tmux session under a supervisor that relaunches it
  on exit and resumes the newest transcript, so the phone reconnects the *same*
  session. Run it from the dir whose conversation you want to resume. It arms both
  background jobs automatically (idempotent; re-run after a gent update to re-arm),
  and refuses to start if this host has no `claude` — a fleet host doesn't otherwise
  need one, and the supervisor would hide the mistake as a silent relaunch loop.
  `gent host install-keepalive` arms the keepalive by hand.
- **keepalive** is the host-side twin of `gent fleet refresh-auth`: when the
  token nears expiry it **pokes** an idle session with a one-line, ignore-me
  prompt to force an in-place re-mint (skipped if the session is busy — it's
  re-minting itself); only once the token has actually lapsed (e.g. after the
  laptop slept) does it **bounce** claude so the supervisor relaunches it and
  reconnects Remote Control.
- **boot autostart** (`gent-host-boot-<name>`: a `RunAtLoad` launchd job on macOS,
  a systemd `--user` unit on Linux) runs `gent host up` at login/boot — the tmux
  server doesn't survive a reboot,
  and keepalive only pokes an *existing* session, so without this a rebooted host
  has no session until someone runs `up` by hand. It remembers the cwd `up` was
  last started in, and its PATH is widened to find `claude` (often `~/.local/bin`)
  and `tmux`. `gent host down --keepalive` removes it along with the keepalive.
  Both jobs need systemd lingering on Linux (`gent` enables it, or tells you to);
  without it they'd die at logout, which on an unattended box means never firing.

Set the session name with `"host": {"name": "…"}` in `fleet.json`. Needs tmux on
the host (`brew install tmux` / `apt install tmux`); on macOS the token is read
from the login Keychain, elsewhere from `~/.claude/.credentials.json`.

## Per-repo memory & history — one brain per repo, shared with your host

Per-repo memory lives **in the repo**, at `<repo>/.claude/memory` — not under
`~/.claude/projects/<abs-path-key>/`. A box mounts its repo at a stable, host-agnostic
`/repos/<owner>/<repo>` and works from there, and both the box and your own host
sessions in that repo are pointed at the repo's memory dir. They reach it by different
absolute paths, so each needs its own pointer: the host's is written into the repo's
`.claude/settings.local.json` (`autoMemoryDirectory`), while the box **cannot** use that
file — it holds a host path that doesn't exist in the container — so `bootstrap`
re-asserts the container path at the **managed settings** tier, which outranks any
project file. Both name the same directory on disk. One brain per repo: what the
autonomous box learns, your host sessions see, and vice versa.

Putting memory in the repo is what makes it **portable**. It used to be addressed by the
repo's absolute path, which made it a hostage of the machine — moving the fleet meant
rewriting `/Users/...` keys to `/home/...` on the way over. Now the path isn't part of the
address, and memory simply travels with the tree.

The container path still has to be **distinct per repo**: the shared `claude-home` login
is one dir across the whole fleet, and a fixed `/repo` would collapse every box into a
single `projects/-repo/` namespace where they stomp on each other. `/repos/<owner>/<repo>`
is distinct *and* host-independent — which the old real-path mount was not.

Memory is **gitignored**, and `gent fleet doctor` asserts it. It's agent-written,
unreviewed prose recording whatever the agent learned about your machines; one
`git add -A` would publish it irreversibly. Transcripts are *not* shared between host and
box, and are not copied when the fleet moves — they're raw session logs, and re-ups start
fresh sessions anyway.
