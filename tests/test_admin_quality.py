"""
Tests for `/admin/quality.json` — the endpoint the fortnightly review reads.

The review cycle is only as honest as this payload. Three properties are worth
pinning hard, and each has a corresponding way the mechanism could quietly rot:

* **It is private.** It carries whole analyses, so an unguarded route would be
  a data leak rather than an inconvenience.
* **Benchmark runs never masquerade as demand.** The app runs benchmarks only
  when a fortnight was quiet; if those runs landed in the usage figures, the
  quietest fortnights would report the healthiest numbers.
* **What could not be seen is stated.** A cycle that silently drops the things
  it failed to measure reads as healthier than it is, which is worse than no
  cycle at all.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import benchmark  # noqa: E402
import main  # noqa: E402
from main import app  # noqa: E402

TOKEN = "test-admin-token"


def analysis_row(job_id, url="https://alpha.example", cost=0.5, status="completed"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "created_at": now,
        "completed_at": now if status == "completed" else None,
        "company_name": "Alpha",
        "company_url": url,
        "geographies": "",
        "status": status,
        "grants_found": 2,
        "error_message": None,
        "user_email": None,
        "cost_usd": cost,
        "newsletter_opt_in": False,
    }


def result_row(job_id, url="https://alpha.example", names=("Beacon Grants",), cost=0.5):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "created_at": now,
        "completed_at": now,
        "company_name": "Alpha",
        "company_url": url,
        "geographies": "",
        "grants_found": len(names),
        "cost_usd": cost,
        "results_json": {
            "opportunities": [
                {
                    "name": n,
                    "managing_body": "Vantor Agency",
                    "priority_tier": "Quick Win",
                    "priority_score": 3.5,
                    "application_link": "https://vantor.example/apply",
                    "link_type": "application_portal",
                    "link_status": "verified",
                    "applicant_type_match": "direct",
                    "has_application_process": True,
                    "trl_match": True,
                    "geography_match": True,
                    "application_timing": "open_now",
                }
                for n in names
            ],
            "strategic_watchlist": [],
        },
    }


class AdminQualityAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_invisible_when_no_admin_token_is_configured(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
            self.assertEqual(self.client.get("/admin/quality.json").status_code, 404)

    def test_rejects_a_wrong_token(self):
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}):
            response = self.client.get("/admin/quality.json?token=nope")
        self.assertEqual(response.status_code, 403)

    def test_reports_an_unreachable_database_rather_than_an_empty_report(self):
        # Silently returning zeroes here would let a cycle conclude "nothing
        # happened" when the truth is "we could not look".
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}), \
             patch.object(main.db, "get_recent_analyses", return_value=None):
            response = self.client.get(f"/admin/quality.json?token={TOKEN}")
        self.assertEqual(response.status_code, 503)


class AdminQualityPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def _get(self, analyses, results, events=None, feedback=None, **params):
        events = events or []

        def named(event, days, limit=2000):
            return [e for e in events if e["event"] == event]

        query = "&".join(f"{k}={v}" for k, v in params.items())
        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}), \
             patch.object(main.db, "get_recent_analyses", return_value=analyses), \
             patch.object(main.db, "get_recent_results", return_value=results), \
             patch.object(main.db, "get_events_named", side_effect=named), \
             patch.object(main.db, "get_recent_feedback", return_value=feedback or []):
            response = self.client.get(
                f"/admin/quality.json?token={TOKEN}" + (f"&{query}" if query else "")
            )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_real_run_is_scored_and_its_recommendations_are_readable(self):
        payload = self._get([analysis_row("r1")], [result_row("r1")])
        self.assertEqual(payload["usage"]["completed"], 1)
        self.assertEqual(payload["real_runs"]["count"], 1)
        run = payload["real_runs"]["runs"][0]
        self.assertEqual(run["main"][0]["name"], "Beacon Grants")
        self.assertEqual(run["assessment"]["defensibility"]["structural_precision"], 1.0)

    def test_benchmark_runs_are_kept_out_of_usage_and_economics(self):
        events = [{"event": benchmark.RUN_EVENT, "job_id": "b1", "detail": "thermify"}]
        payload = self._get(
            [analysis_row("b1", cost=0.6)], [result_row("b1")], events=events,
        )
        self.assertEqual(payload["usage"]["completed"], 0)
        self.assertEqual(payload["real_runs"]["count"], 0)
        self.assertEqual(payload["benchmark"]["count"], 1)
        self.assertEqual(payload["economics"]["total_cost_usd"], 0)
        self.assertEqual(payload["economics"]["benchmark_cost_usd"], 0.6)

    def test_a_benchmark_run_is_scored_against_ground_truth(self):
        events = [{"event": benchmark.RUN_EVENT, "job_id": "b1", "detail": "thermify"}]
        payload = self._get(
            [analysis_row("b1")],
            [result_row("b1", names=("Innovate UK Smart Grants", "SMART FIS"))],
            events=events,
        )
        run = payload["benchmark"]["runs"][0]
        self.assertEqual(run["case_id"], "thermify")
        self.assertIn("recall", run["ground_truth"])
        self.assertIsNotNone(run["ground_truth"]["recall"])

    def test_repeat_runs_of_one_company_produce_a_convergence_reading(self):
        payload = self._get(
            [analysis_row("r1"), analysis_row("r2")],
            [result_row("r1", names=("Beacon Grants",)),
             result_row("r2", names=("Upland Restoration",))],
        )
        groups = payload["real_runs"]["convergence"]
        self.assertEqual(len(groups), 1)
        reading = list(groups.values())[0]
        self.assertEqual(reading["runs"], 2)
        self.assertEqual(reading["main"]["exact"]["mean_pairwise_jaccard"], 0.0)

    def test_a_single_run_of_each_company_is_flagged_as_a_blind_spot(self):
        payload = self._get([analysis_row("r1")], [result_row("r1")])
        self.assertTrue(any("convergence" in b.lower() for b in payload["blind_spots"]))

    def test_nothing_measured_is_said_out_loud(self):
        payload = self._get([], [])
        self.assertTrue(any("nothing to measure" in b.lower() for b in payload["blind_spots"]))

    def test_unreadable_payloads_are_reported_rather_than_ignored(self):
        payload = self._get([analysis_row("r1")], None)
        self.assertTrue(any("payload" in b.lower() for b in payload["blind_spots"]))

    def test_the_benchmark_policy_is_published_with_the_data(self):
        # The review must be able to tell "suppressed by design" from "never
        # fired" without reading the source.
        policy = self._get([], [])["benchmark"]["policy"]
        self.assertEqual(policy["cycle_days"], list(benchmark.CYCLE_DAYS))
        self.assertEqual(policy["lookback_days"], benchmark.LOOKBACK_DAYS)
        self.assertIn("real", policy["rule"].lower())

    def test_cycle_decisions_are_exposed_in_order(self):
        events = [
            {"event": benchmark.CYCLE_EVENT, "job_id": None,
             "detail": "2026-07-15 skipped: suppressed", "created_at": "2026-07-15T00:00:00Z"},
            {"event": benchmark.CYCLE_EVENT, "job_id": None,
             "detail": "2026-08-01 done: 4/4", "created_at": "2026-08-01T00:00:00Z"},
        ]
        payload = self._get([], [], events=events)
        details = [c["detail"] for c in payload["benchmark"]["cycles"]]
        self.assertEqual(details, ["2026-07-15 skipped: suppressed", "2026-08-01 done: 4/4"])

    def test_the_shortlist_promise_comes_from_the_app_not_a_constant_here(self):
        # Otherwise the shortfall measure would drift the moment the pipeline
        # changed its target, and nobody would notice.
        payload = self._get([analysis_row("r1")], [result_row("r1")])
        self.assertEqual(payload["app"]["shortlist_size"], main.analyzer.SHORTLIST_SIZE)
        self.assertEqual(
            payload["real_runs"]["runs"][0]["assessment"]["shape"]["promised_main"],
            main.analyzer.SHORTLIST_SIZE,
        )

    def test_feedback_comments_are_carried_through_for_reading(self):
        feedback = [{"rating": 2, "comment": "links were dead", "job_id": "r1",
                     "created_at": "2026-08-01T00:00:00Z"}]
        payload = self._get([analysis_row("r1")], [result_row("r1")], feedback=feedback)
        self.assertEqual(payload["feedback"]["n"], 1)
        self.assertEqual(payload["feedback"]["comments"][0]["comment"], "links were dead")


class AdminStatsSeparationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_the_dashboard_separates_benchmark_runs_from_real_ones(self):
        events = [{"event": benchmark.RUN_EVENT, "job_id": "b1", "detail": "thermify"}]

        def named(event, days, limit=2000):
            return [e for e in events if e["event"] == event]

        with patch.dict(os.environ, {"ADMIN_TOKEN": TOKEN}), \
             patch.object(main.db, "get_recent_analyses",
                          return_value=[analysis_row("b1", cost=0.6), analysis_row("r1")]), \
             patch.object(main.db, "get_events_named", side_effect=named), \
             patch.object(main.db, "get_recent_events", return_value=[]), \
             patch.object(main.db, "get_recent_feedback", return_value=[]):
            html = self.client.get(f"/admin/stats?token={TOKEN}").text
        self.assertIn("self-benchmark", html)
        self.assertIn("$0.6", html)


if __name__ == "__main__":
    unittest.main()
