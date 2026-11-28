#!/usr/bin/env python3
"""Tests for scripts/housekeeping.py. Run: python3 tests/test_housekeeping.py"""
import importlib.util
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "housekeeping", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "housekeeping.py"))
hk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hk)


def chk(key, level, msg="m", detail=None):
    return {"key": key, "level": level, "msg": msg, "detail": detail}


class Size(unittest.TestCase):
    def test_docker_size_column(self):
        self.assertEqual(hk.parse_size("84.8GB (virtual 89.7GB)"), 84_800_000_000)
        self.assertEqual(hk.parse_size("381MB (virtual 5.33GB)"), 381_000_000)
        self.assertEqual(hk.parse_size("0B"), 0)
        self.assertEqual(hk.parse_size("10kB"), 10_000)
        self.assertIsNone(hk.parse_size("-1B"))     # docker could not size the layer
        self.assertIsNone(hk.parse_size("--"))
        self.assertIsNone(hk.parse_size(""))


class Levels(unittest.TestCase):
    def test_thresholds_are_inclusive_and_ordered(self):
        self.assertEqual(hk.level_for(79.9, 80, 90), hk.OK)
        self.assertEqual(hk.level_for(80, 80, 90), hk.WARN)
        self.assertEqual(hk.level_for(90, 80, 90), hk.HARD)
        self.assertEqual(hk.level_for(5, None, None), hk.OK)

    def test_usage_counts_the_reserved_blocks_as_unusable(self):
        # 2026-11-19: a disk full to every ordinary writer read as 94.9% against raw size.
        # statvfs: 1000 blocks, 50 reserved for root (bfree - bavail), 0 available to users.
        real = os.statvfs
        class S: f_blocks, f_bfree, f_bavail, f_frsize = 1000, 50, 0, 1
        os.statvfs = lambda p: S
        try:
            pct, free, total = hk.disk_usage("/")
        finally:
            os.statvfs = real
        self.assertEqual(pct, 100.0)
        self.assertEqual(free, 0)
        self.assertEqual(total, 950)


class Reconcile(unittest.TestCase):
    def test_first_ok_is_silent(self):
        alerts, st = hk.reconcile({}, [chk("a", hk.OK)], now=1000)
        self.assertEqual(alerts, [])
        self.assertEqual(st["a"]["level"], hk.OK)

    def test_says_it_once_when_it_changes_not_every_run(self):
        a1, st = hk.reconcile({}, [chk("a", hk.WARN, "disk 82%")], now=1000)
        a2, st = hk.reconcile(st, [chk("a", hk.WARN, "disk 83%")], now=2800)
        self.assertEqual(len(a1), 1)
        self.assertIn("disk 82%", a1[0])
        self.assertEqual(a2, [])

    def test_escalation_and_recovery_each_speak(self):
        _, st = hk.reconcile({}, [chk("a", hk.WARN)], now=1)
        up, st = hk.reconcile(st, [chk("a", hk.HARD, "now hard")], now=2)
        down, st = hk.reconcile(st, [chk("a", hk.OK, "fine")], now=3)
        self.assertEqual(len(up), 1)
        self.assertTrue(up[0].startswith("🔴"))
        self.assertEqual(down, ["✓ recovered: fine"])

    def test_hard_repeats_until_fixed_but_only_every_six_hours(self):
        _, st = hk.reconcile({}, [chk("a", hk.HARD)], now=0)
        quiet, st = hk.reconcile(st, [chk("a", hk.HARD)], now=3600)
        loud, st = hk.reconcile(st, [chk("a", hk.HARD)], now=6 * 3600)
        self.assertEqual(quiet, [])
        self.assertEqual(len(loud), 1)

    def test_a_check_that_did_not_run_keeps_its_state(self):
        _, st = hk.reconcile({}, [chk("a", hk.HARD)], now=0)
        _, st2 = hk.reconcile(st, [], now=100)
        self.assertEqual(st2["a"]["level"], hk.HARD)

    def test_detail_is_only_computed_for_a_check_that_fires(self):
        calls = []
        def detail():
            calls.append(1)
            return "Largest: x"
        _, st = hk.reconcile({}, [chk("a", hk.OK, detail=detail)], now=0)
        self.assertEqual(calls, [])
        alerts, st = hk.reconcile(st, [chk("a", hk.WARN, "w", detail=detail)], now=1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Largest: x", alerts[0])
        hk.reconcile(st, [chk("a", hk.WARN, "w", detail=detail)], now=2)      # unchanged → not computed
        self.assertEqual(len(calls), 1)


class TrimStale(unittest.TestCase):
    def test_removes_only_files_unread_past_the_age(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "old", "deep"))
            os.makedirs(os.path.join(root, "new"))
            old, new = os.path.join(root, "old", "deep", "a"), os.path.join(root, "new", "b")
            for fp, size in ((old, 10), (new, 5)):
                open(fp, "wb").write(b"x" * size)
            os.utime(old, (1000, 1000))
            os.utime(new, (9000, 9000))
            n, freed = hk.trim_stale(root, 5000, now=10000)
            self.assertEqual((n, freed), (1, 10))
            self.assertFalse(os.path.exists(old))
            self.assertFalse(os.path.exists(os.path.join(root, "old")))   # emptied dirs go too
            self.assertTrue(os.path.exists(new))
            self.assertTrue(os.path.isdir(root))

    def test_a_recent_read_or_write_keeps_the_file(self):
        with tempfile.TemporaryDirectory() as root:
            fp = os.path.join(root, "f")
            open(fp, "w").write("x")
            os.utime(fp, (1000, 9500))          # read long ago, written recently
            self.assertEqual(hk.trim_stale(root, 5000, now=10000), (0, 0))
            self.assertTrue(os.path.exists(fp))


class WorktreeVerdict(unittest.TestCase):
    NOW = 100 * 86400

    def w(self, **kw):
        base = dict(merged=True, tracked_dirty=0, locked=False, lock_alive=False, last_touch=self.NOW - 10 * 86400)
        base.update(kw)
        return base

    def test_merged_clean_idle_goes(self):
        self.assertTrue(hk.worktree_verdict(self.w(), self.NOW)[0])

    def test_each_guard_keeps_it(self):
        for kw in (dict(merged=False), dict(tracked_dirty=2), dict(locked=True, lock_alive=True),
                   dict(last_touch=self.NOW - 3600)):
            self.assertFalse(hk.worktree_verdict(self.w(**kw), self.NOW)[0], kw)

    def test_a_lock_from_a_dead_session_does_not_hold_it(self):
        self.assertTrue(hk.worktree_verdict(self.w(locked=True, lock_alive=False), self.NOW)[0])


if __name__ == "__main__":
    unittest.main()
