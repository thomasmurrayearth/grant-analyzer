"""
Tests for the snapshot read path.

This module is the thing standing between the improvement cycle and a repeat
of its two failures, so the cases below are mostly about *not measuring*:
stale files, missing days, failed fetches, corrupt JSON. Getting the happy path
right is easy; the value is entirely in refusing to report a number that isn't
current.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshots


def write_snapshot(directory, day, ok=True, quality=None, http_status="200"):
    payload = {
        "schema": 1,
        "date": day,
        "fetched_at": f"{day}T07:00:00Z",
        "source": "/admin/quality.json?days=14",
        "http_status": http_status,
        "ok": ok,
        "health": {"status": "ok", "analytics_database": "ok"},
        "quality": quality if ok else None,
    }
    path = os.path.join(directory, f"quality-{day}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


SAMPLE = {
    "real_runs": {
        "summary": {
            "completed": 3,
            "structural_precision": 0.82,
            "main_count_mean": 4.0,
            "broken_link_rate": 0.05,
        },
        "convergence": {"exact": 0.31},
    },
    "feedback": {"mean": 4.2, "count": 5},
    "blind_spots": ["no repeat runs of the same company"],
}


class SnapshotLoadingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_directory_is_not_an_error(self):
        self.assertEqual(snapshots.load_all("/nonexistent/path"), [])
        self.assertIsNone(snapshots.latest("/nonexistent/path"))

    def test_snapshots_load_oldest_first(self):
        write_snapshot(self.dir, "2026-08-03", quality=SAMPLE)
        write_snapshot(self.dir, "2026-08-01", quality=SAMPLE)
        write_snapshot(self.dir, "2026-08-02", quality=SAMPLE)
        dates = [s["date"] for s in snapshots.load_all(self.dir)]
        self.assertEqual(dates, ["2026-08-01", "2026-08-02", "2026-08-03"])

    def test_latest_json_duplicate_is_not_double_counted(self):
        write_snapshot(self.dir, "2026-08-01", quality=SAMPLE)
        with open(os.path.join(self.dir, "latest.json"), "w", encoding="utf-8") as fh:
            json.dump({"date": "2026-08-01", "ok": True, "quality": SAMPLE}, fh)
        self.assertEqual(len(snapshots.load_all(self.dir)), 1)

    def test_failed_snapshots_are_kept_but_not_usable(self):
        write_snapshot(self.dir, "2026-08-01", ok=False, http_status="503")
        self.assertEqual(len(snapshots.load_all(self.dir)), 1)
        self.assertEqual(snapshots.usable(snapshots.load_all(self.dir)), [])
        self.assertIsNone(snapshots.latest(self.dir))

    def test_corrupt_file_is_reported_not_raised(self):
        with open(os.path.join(self.dir, "quality-2026-08-01.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{not json")
        loaded = snapshots.load_all(self.dir)
        self.assertEqual(len(loaded), 1)
        self.assertTrue(loaded[0]["unreadable"])
        self.assertFalse(loaded[0]["ok"])

    def test_latest_skips_a_failed_newer_snapshot(self):
        write_snapshot(self.dir, "2026-08-01", quality=SAMPLE)
        write_snapshot(self.dir, "2026-08-02", ok=False, http_status="503")
        newest = snapshots.latest(self.dir)
        self.assertEqual(newest["date"], "2026-08-01")


class StalenessTest(unittest.TestCase):
    """The failure mode that matters most: quoting an old number as current."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_staleness_is_none_when_nothing_was_ever_measured(self):
        self.assertIsNone(snapshots.staleness_days(self.dir, date(2026, 8, 5)))
        self.assertTrue(snapshots.is_stale(self.dir, date(2026, 8, 5)))

    def test_fresh_snapshot_is_not_stale(self):
        write_snapshot(self.dir, "2026-08-05", quality=SAMPLE)
        self.assertEqual(snapshots.staleness_days(self.dir, date(2026, 8, 5)), 0)
        self.assertFalse(snapshots.is_stale(self.dir, date(2026, 8, 5)))

    def test_a_delayed_cron_is_tolerated(self):
        # GitHub's scheduler slips under load; one late day is not an outage.
        write_snapshot(self.dir, "2026-08-04", quality=SAMPLE)
        self.assertFalse(snapshots.is_stale(self.dir, date(2026, 8, 5)))

    def test_a_stopped_workflow_is_caught(self):
        write_snapshot(self.dir, "2026-07-20", quality=SAMPLE)
        self.assertEqual(snapshots.staleness_days(self.dir, date(2026, 8, 5)), 16)
        self.assertTrue(snapshots.is_stale(self.dir, date(2026, 8, 5)))

    def test_stale_is_measured_from_the_newest_usable_not_newest_file(self):
        # Two weeks of committed failures must not read as "measured today".
        write_snapshot(self.dir, "2026-07-20", quality=SAMPLE)
        for day in range(21, 32):
            write_snapshot(self.dir, f"2026-07-{day}", ok=False, http_status="503")
        self.assertEqual(snapshots.staleness_days(self.dir, date(2026, 8, 1)), 12)
        self.assertTrue(snapshots.is_stale(self.dir, date(2026, 8, 1)))


class GapDetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_days_are_listed(self):
        write_snapshot(self.dir, "2026-08-05", quality=SAMPLE)
        write_snapshot(self.dir, "2026-08-03", quality=SAMPLE)
        missing = snapshots.missing_dates(self.dir, days=4, today=date(2026, 8, 5))
        self.assertEqual(missing, ["2026-08-02", "2026-08-04"])

    def test_failed_and_missing_are_distinguished(self):
        # Different causes, different owners: missing points at GitHub Actions,
        # failed points at Railway or Supabase.
        write_snapshot(self.dir, "2026-08-05", ok=False, http_status="503")
        health = snapshots.health_report(self.dir, days=2, today=date(2026, 8, 5))
        self.assertEqual([f["date"] for f in health["failed_in_window"]],
                         ["2026-08-05"])
        self.assertEqual(health["missing_in_window"], ["2026-08-04"])


class HealthReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_never_measured_is_blind(self):
        health = snapshots.health_report(self.dir, today=date(2026, 8, 5))
        self.assertTrue(health["blind"])
        self.assertEqual(health["verdict"], "no snapshots have ever been taken")
        self.assertEqual(health["total_snapshots"], 0)

    def test_only_failures_is_blind(self):
        write_snapshot(self.dir, "2026-08-05", ok=False, http_status="503")
        health = snapshots.health_report(self.dir, today=date(2026, 8, 5))
        self.assertTrue(health["blind"])
        self.assertIn("none carries a measurement", health["verdict"])

    def test_clean_run_is_not_blind(self):
        for day in range(1, 6):
            write_snapshot(self.dir, f"2026-08-0{day}", quality=SAMPLE)
        health = snapshots.health_report(self.dir, days=5, today=date(2026, 8, 5))
        self.assertFalse(health["blind"])
        self.assertEqual(health["verdict"], "measuring cleanly")
        self.assertEqual(health["usable_snapshots"], 5)

    def test_gaps_are_reported_without_declaring_blindness(self):
        write_snapshot(self.dir, "2026-08-05", quality=SAMPLE)
        write_snapshot(self.dir, "2026-08-03", quality=SAMPLE)
        health = snapshots.health_report(self.dir, days=4, today=date(2026, 8, 5))
        self.assertFalse(health["blind"])
        self.assertEqual(health["verdict"], "measuring, with gaps")


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _series(self, values):
        for day, value in values:
            quality = json.loads(json.dumps(SAMPLE))
            quality["real_runs"]["summary"]["structural_precision"] = value
            write_snapshot(self.dir, day, quality=quality)

    def test_series_follows_a_dotted_path(self):
        self._series([("2026-08-01", 0.70), ("2026-08-02", 0.80)])
        self.assertEqual(
            snapshots.series("real_runs.summary.structural_precision", self.dir),
            [("2026-08-01", 0.70), ("2026-08-02", 0.80)],
        )

    def test_unknown_path_yields_nothing_rather_than_raising(self):
        self._series([("2026-08-01", 0.70)])
        self.assertEqual(snapshots.series("no.such.measure", self.dir), [])

    def test_failed_snapshots_are_excluded_from_a_trend(self):
        self._series([("2026-08-01", 0.70)])
        write_snapshot(self.dir, "2026-08-02", ok=False, http_status="503")
        self.assertEqual(
            len(snapshots.series("real_runs.summary.structural_precision", self.dir)), 1)

    def test_movement_reports_direction_and_delta(self):
        self._series([("2026-08-01", 0.70), ("2026-08-02", 0.75), ("2026-08-03", 0.90)])
        move = snapshots.movement("real_runs.summary.structural_precision", self.dir)
        self.assertEqual(move["direction"], "up")
        self.assertAlmostEqual(move["delta"], 0.20, places=6)
        self.assertEqual(move["points"], 3)
        self.assertEqual(move["first_date"], "2026-08-01")
        self.assertEqual(move["last_date"], "2026-08-03")

    def test_movement_needs_two_points(self):
        self._series([("2026-08-01", 0.70)])
        self.assertIsNone(
            snapshots.movement("real_runs.summary.structural_precision", self.dir))

    def test_a_missing_measure_is_omitted_not_zero_filled(self):
        # "did not exist yet" and "was zero" are different facts. Conflating
        # them would invent a regression that never happened.
        write_snapshot(self.dir, "2026-08-01", quality={"real_runs": {"summary": {}}})
        self._series([("2026-08-02", 0.80)])
        points = snapshots.series("real_runs.summary.structural_precision", self.dir)
        self.assertEqual(points, [("2026-08-02", 0.80)])


if __name__ == "__main__":
    unittest.main()
