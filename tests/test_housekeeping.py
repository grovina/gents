#!/usr/bin/env python3
"""Tests for scripts/housekeeping.py. Run: python3 tests/test_housekeeping.py"""
import importlib.util
import os
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


if __name__ == "__main__":
    unittest.main()
