#!/usr/bin/env python3
"""Tests for `gent-events next` / `pending`. Run: python3 tests/test_gent_events.py

Every test points GENT_STATE_DIR and GENT_EVENTS_PATH at a temp dir, so it never touches a
live box's stream, cursor or pidfile."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import datetime

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "images", "base", "gent-events")


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, GENT_STATE_DIR=self.tmp.name,
                        GENT_EVENTS_PATH=os.path.join(self.tmp.name, "events.jsonl"))
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    @property
    def stream(self):
        return self.env["GENT_EVENTS_PATH"]

    @property
    def cursor(self):
        return os.path.join(self.tmp.name, "events.cursor")

    def run_cmd(self, *args, timeout=10):
        return subprocess.run([sys.executable, BIN, *args], env=self.env, capture_output=True,
                              text=True, timeout=timeout)

    def spawn(self, *args):
        p = subprocess.Popen([sys.executable, BIN, *args], env=self.env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def emit(self, kind, **kv):
        self.run_cmd("emit", kind, *["%s=%s" % i for i in kv.items()])

    def raw(self, text):
        with open(self.stream, "a") as f:
            f.write(text)

    def cur(self):
        with open(self.cursor) as f:
            return int(f.read())

    def arm_at_end(self):
        """First use: `next` with no cursor starts at the end. Seed it with a throwaway run."""
        open(self.stream, "a").close()
        p = self.spawn("next")
        deadline = time.time() + 5
        while not os.path.exists(self.cursor) and time.time() < deadline:
            time.sleep(0.05)
        p.terminate()
        p.wait(5)


class NextTests(Env):
    def test_first_use_starts_at_end_and_says_so(self):
        self.emit("telegram", line="old")
        p = self.spawn("next")
        deadline = time.time() + 5
        while not os.path.exists(self.cursor) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.cur(), os.path.getsize(self.stream))   # history not replayed
        self.emit("telegram", line="new")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "new")
        self.assertIn("no cursor yet", err)

    def test_prints_and_advances_cursor_then_exits(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.assertIsNone(p.poll())          # blocks while nothing arrives
        self.emit("telegram", line="hello")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "hello")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.cur(), os.path.getsize(self.stream))

    def test_batches_a_burst(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("telegram", line="a")
        self.emit("telegram", line="b")
        self.emit("reminder", id="r1", note="n")
        out, _ = p.communicate(timeout=10)
        lines = out.strip().splitlines()
        self.assertEqual(lines[:2], ["a", "b"])
        self.assertIn("Reminder [r1]", lines[2])

    def test_self_events_skipped_and_do_not_wake(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("self_sent", text="echo")
        time.sleep(1.2)
        self.assertIsNone(p.poll())                       # no wake for an echo
        self.assertEqual(self.cur(), os.path.getsize(self.stream))   # but it is consumed
        self.emit("telegram", line="real")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")
        self.assertIn("dropped 1 self event (self_sent x1)", err)   # one summary line, at the wake

    def test_many_self_events_make_one_summary_line(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        for _ in range(30):
            self.emit("self_scheduled_run", text="x")
        self.emit("self_sent", text="echo")
        time.sleep(1.5)
        self.assertIsNone(p.poll())
        self.emit("telegram", line="real")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")
        drops = [l for l in err.splitlines() if "dropped" in l]
        self.assertEqual(len(drops), 1, err)
        self.assertIn("31 self events", drops[0])
        self.assertIn("self_scheduled_run x30", drops[0])

    def test_unparsable_line_fails_open(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.raw("not json at all\n")
        out, _ = p.communicate(timeout=10)
        self.assertIn("not json at all", out)

    def test_partial_line_waits_for_its_newline(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.raw('{"kind":"telegram","li')
        time.sleep(1.2)
        self.assertIsNone(p.poll())
        self.raw('ne":"whole"}\n')
        out, _ = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "whole")

    def test_no_loss_between_two_runs(self):
        self.arm_at_end()
        p1 = self.spawn("next")
        time.sleep(0.5)
        self.emit("telegram", line="one")
        out1, _ = p1.communicate(timeout=10)
        # nothing is waiting between exit and re-arm: these land in the gap
        self.emit("telegram", line="gap1")
        self.emit("telegram", line="gap2")
        r = self.run_cmd("next")
        self.assertEqual(out1.strip(), "one")
        self.assertEqual(r.stdout.strip().splitlines(), ["gap1", "gap2"])   # at once, in order
        self.assertEqual(self.cur(), os.path.getsize(self.stream))

    def test_truncated_stream_restarts_at_zero(self):
        for i in range(5):
            self.emit("telegram", line="old%d" % i)
        with open(self.cursor, "w") as f:
            f.write(str(os.path.getsize(self.stream)))
        open(self.stream, "w").close()                    # rotated / truncated
        self.emit("telegram", line="fresh")               # smaller than the old cursor
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip(), "fresh")
        self.assertIn("shrank", r.stderr)

    def test_two_waiters_newest_wins(self):
        self.arm_at_end()
        old = self.spawn("next")
        time.sleep(0.5)
        new = self.spawn("next")
        time.sleep(0.5)
        self.emit("telegram", line="once")
        out_new, _ = new.communicate(timeout=10)
        out_old, _ = old.communicate(timeout=10)
        self.assertEqual(out_new.strip(), "once")
        self.assertNotIn("once", out_old)
        self.assertIn("superseded", out_old)

    def test_watch_pid_released_on_exit(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("telegram", line="x")
        p.communicate(timeout=10)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "watch.pid")))


class PendingTests(Env):
    def pending(self, *a):
        return self.run_cmd("pending", *a).returncode

    def test_no_cursor_means_nothing_pending(self):
        self.emit("telegram", line="x")
        self.assertEqual(self.pending(), 1)

    def test_pending_does_not_advance_and_ignores_self(self):
        self.arm_at_end()
        self.emit("self_sent", text="echo")
        self.assertEqual(self.pending(), 1)
        self.emit("telegram", line="x")
        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.pending(), 0)               # read-only
        self.assertLess(self.cur(), os.path.getsize(self.stream))

    def test_older_than(self):
        self.arm_at_end()
        self.emit("telegram", line="fresh")
        self.assertEqual(self.pending("--older-than", "120"), 1)
        old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=300)).isoformat()
        self.raw(json.dumps({"kind": "telegram", "line": "old", "ts": old}) + "\n")
        self.assertEqual(self.pending("--older-than", "120"), 1)   # the OLDEST pending is "fresh"
        self.assertEqual(self.pending("--older-than", "0"), 0)


def sh(script):
    """A filter / reconcile command as an argv list (no shell in the tool itself)."""
    return ["sh", "-c", script]


class Cfg(Env):
    def cfg(self, **kw):
        self.env["GENT_EVENTS_CONFIG"] = json.dumps(kw)

    def wait_cursor(self):
        deadline = time.time() + 5
        while not os.path.exists(self.cursor) and time.time() < deadline:
            time.sleep(0.05)

    def state(self, name):
        return os.path.join(self.tmp.name, name)


class ConfigTests(Cfg):
    def test_unparsable_config_is_today_plus_one_line(self):
        self.env["GENT_EVENTS_CONFIG"] = "{nope"
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("self_sent", text="x")
        self.emit("telegram", line="hi")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "hi")
        self.assertEqual(err.count("GENT_EVENTS_CONFIG"), 1, err)

    def test_unset_config_is_silent(self):
        self.arm_at_end()
        self.emit("telegram", line="hi")
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip(), "hi")
        self.assertNotIn("GENT_EVENTS_CONFIG", r.stderr)

    def test_raw_appended_lines_are_read_and_stream_untouched(self):
        self.arm_at_end()
        ino = os.stat(self.stream).st_ino
        p = self.spawn("next")
        time.sleep(0.5)
        self.raw(json.dumps({"kind": "telegram", "line": "raw one"}) + "\n")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "raw one")
        self.assertEqual(os.stat(self.stream).st_ino, ino)
        self.assertEqual(os.path.getsize(self.stream), len(json.dumps({"kind": "telegram", "line": "raw one"})) + 1)

    def test_large_history_without_cursor_starts_at_end(self):
        with open(self.stream, "w") as f:
            for i in range(50000):
                f.write(json.dumps({"kind": "telegram", "line": "old%d" % i}) + "\n")
        p = self.spawn("next")
        self.wait_cursor()
        self.assertEqual(self.cur(), os.path.getsize(self.stream))
        self.emit("telegram", line="new")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "new")


class CursorHomeTests(Cfg):
    def setUp(self):
        super().setUp()
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        self.env["GENT_EVENTS_PATH"] = os.path.join(self.data.name, "own.jsonl")

    def test_cursor_lives_in_state_dir_keyed_by_stream(self):
        open(self.stream, "a").close()
        p = self.spawn("next")
        time.sleep(0.8)
        p.terminate()
        p.wait(5)
        self.assertEqual(os.listdir(self.data.name), ["own.jsonl"])     # nothing beside the stream
        cursors = [f for f in os.listdir(self.tmp.name) if f.endswith(".cursor")]
        self.assertEqual(len(cursors), 1)
        self.assertNotEqual(cursors[0], "events.cursor")                # not the default stream's
        self.assertTrue(cursors[0].startswith("events-"))

    def test_two_streams_never_share_a_cursor(self):
        other = os.path.join(self.data.name, "other.jsonl")
        for st in (self.stream, other):
            self.env["GENT_EVENTS_PATH"] = st
            self.raw("")
            self.run_cmd("emit", "telegram", "line=x")
            p = self.spawn("next")
            time.sleep(0.8)
            p.terminate()
            p.wait(5)
        self.assertEqual(len([f for f in os.listdir(self.tmp.name) if f.endswith(".cursor")]), 2)

    def test_legacy_cursor_beside_custom_stream_is_taken_over_once(self):
        self.run_cmd("emit", "telegram", "line=old")
        size = os.path.getsize(self.stream)
        self.run_cmd("emit", "telegram", "line=missed")
        legacy = os.path.join(self.data.name, "events.cursor")
        with open(legacy, "w") as f:
            f.write(str(size))
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip(), "missed")
        self.assertIn("took over the cursor", r.stderr)
        self.assertFalse(os.path.exists(legacy))
        self.run_cmd("emit", "telegram", "line=again")
        r2 = self.run_cmd("next")
        self.assertEqual(r2.stdout.strip(), "again")
        self.assertNotIn("took over", r2.stderr)

    def test_default_stream_keeps_events_cursor(self):
        self.env["GENT_EVENTS_PATH"] = os.path.join(self.tmp.name, "events.jsonl")
        self.arm_at_end()
        self.assertTrue(os.path.exists(self.cursor))


class DropFilterTests(Cfg):
    def test_dropped_kinds_advance_cursor_and_are_logged(self):
        self.cfg(drop=["noise"])
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("noise", text="x")
        time.sleep(1.2)
        self.assertIsNone(p.poll())
        self.assertEqual(self.cur(), os.path.getsize(self.stream))
        self.emit("telegram", line="real")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")
        self.assertIn("dropped 1 event (noise x1)", err)

    def test_pending_applies_drop_and_filter(self):
        self.cfg(drop=["noise"], filter=sh("grep -v skipme || true"))
        self.arm_at_end()
        self.emit("noise", text="x")
        self.emit("telegram", line="skipme")
        self.assertEqual(self.run_cmd("pending").returncode, 1)
        self.emit("telegram", line="keep")
        self.assertEqual(self.run_cmd("pending").returncode, 0)

    def test_filter_passes_and_rewrites_in_order(self):
        self.cfg(filter=sh("grep -v drop-me | sed 's/\"line\": \"/\"line\": \"[f] /'"))
        self.arm_at_end()
        self.emit("telegram", line="a")
        self.emit("telegram", line="drop-me")
        self.emit("telegram", line="b")
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip().splitlines(), ["[f] a", "[f] b"])

    def test_filter_string_is_shlex_split(self):
        self.cfg(filter="grep -v skipme")
        self.arm_at_end()
        self.emit("telegram", line="skipme")
        self.emit("telegram", line="ok")
        self.assertEqual(self.run_cmd("next").stdout.strip(), "ok")

    def test_filter_that_removes_everything_keeps_waiting(self):
        self.cfg(filter=sh("cat >/dev/null"))
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.5)
        self.emit("telegram", line="eaten")
        time.sleep(1.2)
        self.assertIsNone(p.poll())
        self.assertEqual(self.cur(), os.path.getsize(self.stream))

    def _fail_open(self, filt, **extra):
        self.cfg(filter=filt, **extra)
        self.arm_at_end()
        self.emit("telegram", line="a")
        self.emit("telegram", line="b")
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip().splitlines(), ["a", "b"])
        self.assertIn("filter", r.stderr)
        self.assertEqual(r.stderr.count("every line passes"), 1)

    def test_filter_missing_command_fails_open(self):
        self._fail_open(["/nonexistent/filter-cmd"])

    def test_filter_nonzero_exit_fails_open(self):
        self._fail_open(sh("echo only-this; exit 3"))

    def test_filter_timeout_fails_open(self):
        self._fail_open(sh("sleep 5"), filter_timeout=0.5)


class OrderTests(Cfg):
    def test_lines_seconds_apart_stay_in_file_order(self):
        self.arm_at_end()
        now = datetime.datetime.now(datetime.UTC)
        for i in range(4):
            ts = (now - datetime.timedelta(seconds=10 - 3 * i)).isoformat()
            self.raw(json.dumps({"kind": "telegram", "line": "l%d" % i, "ts": ts}) + "\n")
        r = self.run_cmd("next")
        self.assertEqual(r.stdout.strip().splitlines(), ["l0", "l1", "l2", "l3"])

    def test_order_survives_a_filter(self):
        self.cfg(filter=sh("cat"))
        self.arm_at_end()
        for i in range(5):
            self.emit("telegram", line="n%d" % i)
        self.assertEqual(self.run_cmd("next").stdout.strip().splitlines(), ["n0", "n1", "n2", "n3", "n4"])


class ReconcileTests(Cfg):
    def seed(self):
        """A cursor at the end of the stream, nothing else (reconcile has never run)."""
        open(self.stream, "a").close()
        with open(self.cursor, "w") as f:
            f.write(str(os.path.getsize(self.stream)))

    def rec(self, script, **kw):
        self.cfg(reconcile=sh(script), **kw)

    def test_runs_on_first_arm_and_wakes_with_headed_output(self):
        self.rec("echo missed-one")
        self.seed()
        r = self.run_cmd("next")
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith("RECONCILE"), lines)
        self.assertEqual(lines[1], "missed-one")

    def test_debounce_skips_right_after_an_exit(self):
        self.rec("echo ran", reconcile_after=600)
        self.seed()
        self.run_cmd("next")                                # runs, state ok
        self.emit("telegram", line="e")
        r = self.run_cmd("next")                            # exit was seconds ago: skipped
        self.assertEqual(r.stdout.strip(), "e")
        self.assertNotIn("RECONCILE", r.stdout)

    def test_runs_again_after_reconcile_after(self):
        self.rec("echo again", reconcile_after=0)
        self.seed()
        self.run_cmd("next")
        r = self.run_cmd("next")
        self.assertIn("again", r.stdout)

    def test_empty_output_keeps_waiting(self):
        self.rec("true")
        self.seed()
        p = self.spawn("next")
        time.sleep(1.5)
        self.assertIsNone(p.poll())
        self.emit("telegram", line="real")
        out, _ = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")

    def test_nonzero_exit_one_stderr_line_keeps_waiting(self):
        self.rec("echo partial; exit 4")
        self.seed()
        p = self.spawn("next")
        time.sleep(1.5)
        self.assertIsNone(p.poll())
        self.emit("telegram", line="real")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")
        self.assertEqual(err.count("reconcile failed"), 1, err)

    def test_timeout_one_stderr_line_keeps_waiting(self):
        self.rec("sleep 30", reconcile_timeout=0.5)
        self.seed()
        p = self.spawn("next")
        time.sleep(2)
        self.assertIsNone(p.poll())
        self.emit("telegram", line="real")
        out, err = p.communicate(timeout=10)
        self.assertEqual(out.strip(), "real")
        self.assertEqual(err.count("reconcile timed out"), 1, err)

    def test_event_during_slow_reconcile_wakes_first_and_reconcile_is_owed(self):
        self.rec("sleep 30; echo late")
        self.seed()
        p = self.spawn("next")
        time.sleep(0.7)
        t0 = time.time()
        self.emit("telegram", line="fast")
        out, _ = p.communicate(timeout=10)
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(out.strip(), "fast")
        with open(self.state("events.reconcile")) as f:
            self.assertEqual(f.read(), "owed")
        self.rec("echo second")                              # next arm runs it again
        r = self.run_cmd("next")
        self.assertIn("second", r.stdout)

    def test_event_batch_window_joins_reconcile_wake(self):
        self.rec("sleep 0.6; echo rec-out")
        self.seed()
        p = self.spawn("next")
        time.sleep(0.5)
        out, _ = p.communicate(timeout=10)
        self.assertIn("rec-out", out)

    def test_output_cap_default_4kb(self):
        self.rec("head -c 6000 /dev/zero | tr '\\0' x")
        self.seed()
        r = self.run_cmd("next")
        body = r.stdout.splitlines()
        self.assertEqual(len(body[1]), 4096)
        self.assertIn("more cut (reconcile_max_bytes 4096)", body[-1])
        self.assertIn("1.9 KB", body[-1])

    def test_output_cap_configurable(self):
        self.rec("head -c 1000 /dev/zero | tr '\\0' y", reconcile_max_bytes=100)
        self.seed()
        r = self.run_cmd("next")
        body = r.stdout.splitlines()
        self.assertEqual(len(body[1]), 100)
        self.assertIn("900 B more cut (reconcile_max_bytes 100)", body[-1])


class WireTests(Cfg):
    def wire(self, **env):
        e = dict(self.env, **env)
        return subprocess.run([sys.executable, BIN, "wire"], env=e, capture_output=True,
                              text=True, timeout=20)

    def test_ok_next(self):
        self.arm_at_end()
        p = self.spawn("next")
        time.sleep(0.6)
        r = self.wire()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "OK next pid=%d" % p.pid)

    def test_ok_idle_while_handling(self):
        self.arm_at_end()
        self.run_cmd("emit", "telegram", "line=x")
        self.run_cmd("next")                                 # exits: agent is handling
        r = self.wire()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK idle (handling)"))

    def test_down_when_event_waited_too_long(self):
        self.arm_at_end()
        old = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=300)).isoformat()
        self.raw(json.dumps({"kind": "telegram", "line": "old", "ts": old}) + "\n")
        self.raw(json.dumps({"kind": "telegram", "line": "old2", "ts": old}) + "\n")
        r = self.wire()
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout.strip(), r"^DOWN absent \d+s, 2 waiting$")

    def test_down_when_absent_too_long(self):
        self.arm_at_end()
        r = self.wire(GENT_WD_ABSENT_MAX="0")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout.strip(), r"^DOWN absent \d+s$")

    def test_down_without_cursor_after_six_seconds(self):
        r = self.wire()
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "DOWN absent over 6s"))

    def test_duplicate(self):
        # Real waiters stand each other down (newest wins), so stand-ins that merely carry
        # the `gent-events next|watch` command line model two that both survive.
        self.arm_at_end()
        stub = os.path.join(self.tmp.name, "gent-events")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nsleep 30\n")
        os.chmod(stub, 0o755)
        a = subprocess.Popen([stub, "next"], env=self.env)
        b = subprocess.Popen([stub, "watch"], env=self.env)
        self.procs += [a, b]
        time.sleep(0.5)
        r = self.wire()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout.strip(), "DUPLICATE pids=%d,%d" % tuple(sorted([a.pid, b.pid])))

    def test_live_pattern_for_custom_watch(self):
        self.arm_at_end()
        p = subprocess.Popen(["sleep", "31.7342"])
        self.procs.append(p)
        r = subprocess.run([sys.executable, BIN, "wire", "--live", "sleep 31[.]7342"], env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK live pid=%d" % p.pid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
