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
`gent host` gives that host session the same two safety nets:

```
gent host up        # tmux + self-healing supervisor + keepalive, all armed
gent host down      # stop it (kill tmux + reap any orphaned claude)
gent host status    # session state + token expiry + keepalive job
gent host attach    # join the session
```

- **`up`** runs `claude` in a tmux session under a supervisor that relaunches it
  on exit and resumes the newest transcript, so the phone reconnects the *same*
  session. Run it from the dir whose conversation you want to resume. It arms the
  launchd keepalive automatically (idempotent; re-run after a gent update to
  re-arm). `gent host install-keepalive` arms it by hand.
- **keepalive** is the host-side twin of `gent fleet refresh-auth`: when the
  token nears expiry it **pokes** an idle session with a one-line, ignore-me
  prompt to force an in-place re-mint (skipped if the session is busy — it's
  re-minting itself); only once the token has actually lapsed (e.g. after the
  laptop slept) does it **bounce** claude so the supervisor relaunches it and
  reconnects Remote Control.

Set the session name with `"host": {"name": "…"}` in `fleet.json`. Needs tmux on
the host (`brew install tmux`); on macOS the token is read from the login
Keychain, elsewhere from `~/.claude/.credentials.json`.

## Per-repo memory & history — one brain per repo, shared with your host

A box mounts its repo at the **real host absolute path** (e.g.
`/Users/you/Projects/acme/api`), not a fixed `/repo`, and works from there. That
makes the box a path-faithful subset of your machine: Claude keys per-project
state (memory, transcripts) by the working directory, so the box and your host
sessions for that repo compute the **same** key. gent then binds your host's
`~/.claude/projects/<that-key>/` straight into the box, so the two share **one**
per-repo memory and one transcript history — what the autonomous box learns,
your host sessions see, and vice versa.

This is also what keeps memory **un-scrambled**. The shared `claude-home` login
is one dir across the whole fleet; if every box worked from the same `/repo`
path they'd all collapse into a single `projects/-repo/` namespace and stomp on
each other's memory. The real-path mount gives every repo a distinct key, and
the host bind keeps each repo's brain in exactly one place. `claude-home` still
supplies the fleet login/creds/settings; only each repo's own project subtree is
host-backed.
