"""Housekeeping logic for `gent fleet housekeeping` — the pure part, so it is testable.

The host runs a timer; each run measures a handful of things (host disk, every box's
disk footprint, backup freshness, stopped boxes, failed host units), turns each into a
level — ok / warn / hard — and `reconcile` decides what is worth SAYING. The rule is
"say it when it changes": a warning that repeats every half hour is a warning nobody
reads. A `hard` level is the exception, it repeats until fixed.

Alerts go out over the same Telegram path as auth-watch.sh ($STATE/secrets/alert.env),
which is independent of the boxes on purpose: the thing being reported is often the
boxes (or the disk they share) being unwell.
"""
import os
import re
import urllib.parse
import urllib.request

OK, WARN, HARD = "ok", "warn", "hard"
_RANK = {OK: 0, WARN: 1, HARD: 2}
HARD_REPEAT_S = 6 * 3600

_UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def parse_size(text):
    """Docker's `SIZE` column ("84.8GB (virtual 89.7GB)", "381MB", "0B") -> bytes of the
    writable layer, i.e. the first figure. None if it is not a size at all."""
    m = re.match(r"\s*([0-9.]+)\s*([kKMGT]?B)", text or "")
    if not m:
        return None
    unit = m.group(2).upper()
    return int(float(m.group(1)) * _UNITS[unit])


def level_for(value, warn, hard):
    """ok / warn / hard for `value` against ascending thresholds (None = no threshold)."""
    if hard is not None and value >= hard:
        return HARD
    if warn is not None and value >= warn:
        return WARN
    return OK


def disk_usage(path):
    """(pct_used, free_bytes, total_bytes) the way a USER hits the wall: against what a
    non-root process may use, not the raw size. Counting the filesystem's reserved 5%
    makes a disk that is full to every ordinary writer read as 95% — it did, on
    2026-11-19, and a guard built on that number stayed silent through the outage."""
    st = os.statvfs(path)
    total = (st.f_blocks - (st.f_bfree - st.f_bavail)) * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return (100.0 * (total - free) / total if total else 0.0), free, total


def gb(n):
    return f"{n / 1e9:.1f} GB" if n < 1e11 else f"{n / 1e9:.0f} GB"


def reconcile(prev, checks, now, repeat_s=HARD_REPEAT_S):
    """Decide what to say. `checks`: [{key, level, msg, detail?}] where `detail` may be a
    callable, evaluated only for a check that actually fires (it can be expensive — a du).
    Returns (alerts, new_state). A key absent from `checks` keeps its old state: a check
    that could not run is not evidence of recovery."""
    state = {k: dict(v) for k, v in (prev or {}).items()}
    alerts = []
    for c in checks:
        key, lvl = c["key"], c["level"]
        old = state.get(key, {"level": OK, "alerted_at": 0})
        changed = lvl != old["level"]
        due = lvl == HARD and not changed and now - old.get("alerted_at", 0) >= repeat_s
        if changed or due:
            if lvl == OK:
                alerts.append(f"✓ recovered: {c['msg']}")
            else:
                icon = "🔴" if lvl == HARD else "⚠️"
                text = f"{icon} {c['msg']}"
                det = c.get("detail")
                det = det() if callable(det) else det
                if det:
                    text += "\n" + det
                alerts.append(text)
            state[key] = {"level": lvl, "alerted_at": now if lvl != OK else 0}
        else:
            state[key] = {"level": lvl, "alerted_at": old.get("alerted_at", 0)}
    return alerts, state


def send_telegram(alert_env, text):
    """Best effort; returns an error string or None. Never raises: a failed alert must
    not stop the housekeeping run that found the problem."""
    try:
        env = {}
        for line in open(alert_env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
        data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text[:4000]}).encode()
        url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()
        return None
    except Exception as e:                                   # noqa: BLE001 — see docstring
        return f"{type(e).__name__}: {e}"


CACHE_TRIM_EVERY_S = 7 * 86400
CACHE_MAX_AGE_S = 30 * 86400


def trim_stale(root, max_age_s, now):
    """Delete files under `root` not read for `max_age_s`, then empty directories.

    For the shared package caches (npm, pip), which nothing else ever expires: a file
    left unread for a month is a package no box has asked for in that time, and a cache
    miss only costs a re-download. Reads atime, falling back to mtime when atime is
    older (a mount with noatime). Returns (files_removed, bytes_freed)."""
    n = freed = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                st = os.lstat(fp)
                if now - max(st.st_atime, st.st_mtime) > max_age_s:
                    os.unlink(fp)
                    n += 1
                    freed += st.st_size
            except OSError:
                continue
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if dirpath != root:
            try:
                os.rmdir(dirpath)       # only succeeds when empty
            except OSError:
                pass
    return n, freed
