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
        self.assertIn("dropped self_sent", err)           # every drop is logged

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
