"""
Tests for `benchmark.py` — the policy that decides whether the app spends money
measuring itself.

Every decision path is exercised without running the pipeline, because the
whole point of separating policy from execution is that the expensive part
never has to run to prove the cheap part is right. Two failure directions
matter and they are not symmetric:

* running when it shouldn't costs real money on every cycle, forever;
* not running when it should leaves the improvement loop blind, which is worse,
  because a blind loop still produces confident-looking reports.

So the tests below pin both directions explicitly rather than trusting one
happy path.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import benchmark  # noqa: E402


def at(day, hour=1, month=8, year=2026):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def completed(job_id="real-1", when=None, status="completed"):
    when = when or datetime.now(timezone.utc)
    return {
        "job_id": job_id,
        "status": status,
        "created_at": when.isoformat(),
        "completed_at": when.isoformat(),
    }


def run_event(job_id, case_id="thermify"):
    return {"event": benchmark.RUN_EVENT, "job_id": job_id, "detail": case_id}


class CycleWindowTest(unittest.TestCase):
    def test_cycle_days_open_a_window(self):
        self.assertEqual(benchmark.cycle_key(at(1)), "2026-08-01")
        self.assertEqual(benchmark.cycle_key(at(15)), "2026-08-15")

    def test_other_days_are_outside_the_window(self):
        self.assertEqual(benchmark.cycle_key(at(2)), "")
        self.assertEqual(benchmark.cycle_key(at(14)), "")

    def test_the_window_closes_before_the_review_runs(self):
        # A full cycle takes roughly forty minutes and the review reads the
        # results at 09:00 local. Starting late would hand the review a cycle
        # still in progress.
        self.assertEqual(benchmark.cycle_key(at(1, hour=5)), "2026-08-01")
        self.assertEqual(benchmark.cycle_key(at(1, hour=6)), "")
        self.assertEqual(benchmark.cycle_key(at(1, hour=23)), "")


class RealActivityTest(unittest.TestCase):
    def test_a_completed_user_run_counts(self):
        self.assertEqual(
            benchmark.count_real_completions([completed()], []), 1,
        )

    def test_benchmark_runs_do_not_count_as_real_activity(self):
        # Otherwise one benchmark cycle would suppress the next one forever.
        rows = [completed("bench-1")]
        self.assertEqual(
            benchmark.count_real_completions(rows, [run_event("bench-1")]), 0,
        )

    def test_starts_that_never_completed_do_not_count(self):
        # A fortnight of abandoned runs leaves nothing to read, so it still
        # needs a benchmark — counting starts would suppress it exactly when
        # it is most needed.
        rows = [completed("x", status="started"), completed("y", status="profile_ready")]
        self.assertEqual(benchmark.count_real_completions(rows, []), 0)

    def test_runs_older_than_the_lookback_do_not_count(self):
        now = datetime.now(timezone.utc)
        old = completed("old", when=now - timedelta(days=20))
        self.assertEqual(benchmark.count_real_completions([old], [], now=now), 0)

    def test_an_unreadable_timestamp_is_treated_as_old(self):
        # Failing this direction costs a few dollars; failing the other would
        # silently hide real activity.
        row = {"job_id": "x", "status": "completed", "created_at": "not a date"}
        self.assertEqual(benchmark.count_real_completions([row], []), 0)


class DecisionTest(unittest.TestCase):
    def test_runs_when_a_fortnight_had_no_completed_runs(self):
        run, key, reason = benchmark.decide(at(1), [], [])
        self.assertTrue(run)
        self.assertEqual(key, "2026-08-01")
        self.assertIn("no real completed runs", reason)

    def test_suppressed_by_a_single_real_run(self):
        run, _, reason = benchmark.decide(at(1), [completed()], [])
        self.assertFalse(run)
        self.assertIn("suppressed", reason)

    def test_does_nothing_outside_a_cycle_window(self):
        run, key, _ = benchmark.decide(at(7), [], [])
        self.assertFalse(run)
        self.assertEqual(key, "")

    def test_does_not_repeat_a_cycle_it_already_handled(self):
        events = [{"event": benchmark.CYCLE_EVENT, "detail": "2026-08-01 running: x"}]
        run, _, reason = benchmark.decide(at(1), [], events)
        self.assertFalse(run)
        self.assertIn("already handled", reason)

    def test_a_skipped_cycle_also_counts_as_handled(self):
        events = [{"event": benchmark.CYCLE_EVENT, "detail": "2026-08-01 skipped: y"}]
        run, _, _ = benchmark.decide(at(1), [], events)
        self.assertFalse(run)

    def test_a_previous_cycle_does_not_block_the_next_one(self):
        events = [{"event": benchmark.CYCLE_EVENT, "detail": "2026-07-15 done: 4/4"}]
        run, key, _ = benchmark.decide(at(1), [], events)
        self.assertTrue(run)
        self.assertEqual(key, "2026-08-01")

    def test_an_unreachable_database_never_triggers_spending(self):
        # `None` means "couldn't look", not "nothing happened". Spending on a
        # guess would be the wrong way to resolve that ambiguity.
        run, _, reason = benchmark.decide(at(1), None, [])
        self.assertFalse(run)
        self.assertIn("database unavailable", reason)

    def test_the_kill_switch_stops_it(self):
        with patch.dict(os.environ, {"SELF_BENCHMARK_ENABLED": "0"}):
            run, _, reason = benchmark.decide(at(1), [], [])
        self.assertFalse(run)
        self.assertIn("disabled", reason)

    def test_the_reason_is_always_reported_even_when_not_running(self):
        # A fortnight with no benchmark data must not be ambiguous between
        # "suppressed as designed" and "the scheduler never fired".
        for analyses in ([], [completed()], None):
            _, _, reason = benchmark.decide(at(1), analyses, [])
            self.assertTrue(reason)


class PlanTest(unittest.TestCase):
    def test_every_case_is_run_the_configured_number_of_times(self):
        plan = benchmark.planned_runs()
        self.assertEqual(len(plan), min(
            benchmark.repeats() * len(benchmark.BENCHMARK_CASE_IDS),
            benchmark.max_runs(),
        ))

    def test_repeats_exist_so_convergence_is_measurable(self):
        # One run per company would make Q5 permanently unmeasurable.
        self.assertGreaterEqual(benchmark.repeats(), 2)
        for case_id in benchmark.BENCHMARK_CASE_IDS:
            self.assertGreaterEqual(benchmark.planned_runs().count(case_id), 2)

    def test_the_spend_cap_is_absolute(self):
        with patch.dict(os.environ, {"SELF_BENCHMARK_MAX_RUNS": "3"}):
            self.assertEqual(len(benchmark.planned_runs()), 3)

    def test_a_growing_case_list_cannot_quietly_raise_the_bill(self):
        with patch.dict(os.environ, {"SELF_BENCHMARK_REPEATS": "50"}):
            self.assertLessEqual(len(benchmark.planned_runs()), benchmark.max_runs())


# ---------------------------------------------------------------------------
# Execution, with the pipeline and the database faked out
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self.events, self.started, self.completed, self.failed, self.profiles = [], [], [], [], []

    def log_event(self, event, job_id=None, ip=None, detail=None):
        self.events.append({"event": event, "job_id": job_id, "detail": detail})

    def log_started(self, job_id, *a, **k):
        self.started.append(job_id)

    def log_profile_ready(self, job_id, name, profile):
        self.profiles.append((job_id, name))

    def log_completed(self, job_id, grants, results, usage=None, cost=None):
        self.completed.append({"job_id": job_id, "grants": grants, "cost": cost})

    def log_failed(self, job_id, error):
        self.failed.append({"job_id": job_id, "error": error})


class FakeAnalyzer:
    @staticmethod
    def start_usage_tracking(existing):
        return {"input_tokens": 0}

    @staticmethod
    def usage_cost_usd(usage):
        return 0.57


async def fake_phase1(url=None, extra_text=None, preferences=None):
    yield {"type": "progress", "message": "working"}
    yield {"type": "profile_ready", "profile": {"name": "Test Co"}}


async def fake_phase23(profile, preferences=None):
    yield {"type": "complete", "result": {"opportunities": [{"name": "A"}]}}


async def failing_phase1(url=None, extra_text=None, preferences=None):
    yield {"type": "error", "message": "network down"}


class ExecutionTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.db = FakeDB()

    def tearDown(self):
        asyncio.get_event_loop().close()

    def test_a_benchmark_run_is_tagged_so_it_can_never_look_like_demand(self):
        case = benchmark.get_case(benchmark.BENCHMARK_CASE_IDS[0])
        out = self._run(benchmark.run_case(
            case, run_phase1=fake_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
        ))
        self.assertTrue(out["ok"])
        tagged = [e for e in self.db.events if e["event"] == benchmark.RUN_EVENT]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["job_id"], out["job_id"])
        self.assertEqual(benchmark.benchmark_job_ids(self.db.events), {out["job_id"]})

    def test_a_benchmark_run_is_stored_like_any_other_analysis(self):
        case = benchmark.get_case(benchmark.BENCHMARK_CASE_IDS[0])
        out = self._run(benchmark.run_case(
            case, run_phase1=fake_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
        ))
        self.assertIn(out["job_id"], self.db.started)
        self.assertEqual(self.db.completed[0]["job_id"], out["job_id"])
        self.assertEqual(self.db.completed[0]["cost"], 0.57)

    def test_a_failed_run_is_recorded_rather_than_swallowed(self):
        case = benchmark.get_case(benchmark.BENCHMARK_CASE_IDS[0])
        out = self._run(benchmark.run_case(
            case, run_phase1=failing_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
        ))
        self.assertFalse(out["ok"])
        self.assertEqual(len(self.db.failed), 1)

    def test_the_cycle_is_marked_before_any_spending_starts(self):
        # A crash part-way through must not cause the whole cycle — and its
        # bill — to be repeated on restart.
        self._run(benchmark.run_cycle(
            "2026-08-01", "no real runs",
            run_phase1=fake_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
        ))
        first = self.db.events[0]
        self.assertEqual(first["event"], benchmark.CYCLE_EVENT)
        self.assertIn("running", first["detail"])
        self.assertTrue(benchmark.already_handled(self.db.events, "2026-08-01"))

    def test_a_cycle_runs_every_planned_case_and_reports_the_spend(self):
        out = self._run(benchmark.run_cycle(
            "2026-08-01", "no real runs",
            run_phase1=fake_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
        ))
        self.assertEqual(len(out["runs"]), len(benchmark.planned_runs()))
        self.assertEqual(out["spend_usd"], round(0.57 * len(out["runs"]), 2))

    def test_a_cycle_waits_for_user_runs_to_finish(self):
        waited = []

        async def wait():
            waited.append(True)

        self._run(benchmark.run_cycle(
            "2026-08-01", "no real runs",
            run_phase1=fake_phase1, run_phase23=fake_phase23,
            analyzer_module=FakeAnalyzer, db_module=self.db,
            wait_until_idle=wait,
        ))
        self.assertEqual(len(waited), len(benchmark.planned_runs()))

    def test_the_scheduler_records_a_skip_so_the_review_can_see_the_decision(self):
        class DB(FakeDB):
            def get_recent_analyses(self, days):
                return [completed()]

            def get_events_named(self, event, days, limit=2000):
                return []

        db = DB()
        with patch("benchmark.cycle_key", return_value="2026-08-01"):
            self._run(benchmark.scheduler_loop(
                run_phase1=fake_phase1, run_phase23=fake_phase23,
                analyzer_module=FakeAnalyzer, db_module=db,
                poll_seconds=0, iterations=1,
            ))
        skips = [e for e in db.events if "skipped" in (e["detail"] or "")]
        self.assertEqual(len(skips), 1)
        self.assertIn("suppressed", skips[0]["detail"])

    def test_a_scheduler_error_never_takes_the_app_down(self):
        class Broken(FakeDB):
            def get_recent_analyses(self, days):
                raise RuntimeError("database on fire")

            def get_events_named(self, event, days, limit=2000):
                return []

        with patch("benchmark.cycle_key", return_value="2026-08-01"):
            self._run(benchmark.scheduler_loop(
                run_phase1=fake_phase1, run_phase23=fake_phase23,
                analyzer_module=FakeAnalyzer, db_module=Broken(),
                poll_seconds=0, iterations=1,
            ))  # must not raise

    def test_the_scheduler_does_nothing_outside_a_window(self):
        class DB(FakeDB):
            def get_recent_analyses(self, days):
                raise AssertionError("should not have looked")

            def get_events_named(self, event, days, limit=2000):
                raise AssertionError("should not have looked")

        with patch("benchmark.cycle_key", return_value=""):
            self._run(benchmark.scheduler_loop(
                run_phase1=fake_phase1, run_phase23=fake_phase23,
                analyzer_module=FakeAnalyzer, db_module=DB(),
                poll_seconds=0, iterations=1,
            ))


class GroundTruthWiringTest(unittest.TestCase):
    def test_every_benchmark_case_resolves_to_a_real_case(self):
        for case_id in benchmark.BENCHMARK_CASE_IDS:
            self.assertIsNotNone(benchmark.get_case(case_id))

    def test_cases_carry_the_rules_scoring_depends_on(self):
        for case in benchmark.TEST_CASES:
            with self.subTest(case=case["id"]):
                rules = sum(len(case.get(k, [])) for k in (
                    "must_appear_in_main", "must_appear_in_watchlist_or_main",
                    "must_not_appear_in_main", "must_not_appear_anywhere",
                ))
                self.assertGreater(rules, 0, "a case with no rules measures nothing")

    def test_a_case_can_be_recognised_from_a_stored_analysis_url(self):
        import cases as case_module
        with_url = [c for c in benchmark.TEST_CASES if c.get("company_url")]
        self.assertTrue(with_url)
        for case in with_url:
            self.assertEqual(
                case_module.case_for_url(case["company_url"].rstrip("/") + "/")["id"],
                case["id"],
            )


if __name__ == "__main__":
    unittest.main()
