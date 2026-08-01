import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from main import app


class GrantAnalyserAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_root_serves_frontend(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Grant Analyser", response.text)

    def test_manifest_is_available(self):
        response = self.client.get("/manifest.json")
        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest["name"], "Grant Opportunity Analyser")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertIn("icons", manifest)

    def test_service_worker_is_available(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("self.addEventListener", response.text)

    def test_health_endpoint(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("status", response.json())


class ConversionEssentialsTest(unittest.TestCase):
    """WP-1: consulting CTAs, feedback capture, funnel events, rate limiting."""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def tearDown(self):
        main._jobs.clear()
        main._ip_runs_today.clear()

    # ── Landing page ────────────────────────────────────────────────────────
    def test_landing_page_has_social_preview_tags(self):
        html = self.client.get("/").text
        self.assertIn('property="og:image"', html)
        self.assertIn('name="twitter:card"', html)
        self.assertIn('rel="canonical"', html)

    def test_landing_page_offers_the_sample_report(self):
        html = self.client.get("/").text
        self.assertIn("?demo=1", html)
        self.assertIn("What you'll get", html)

    def test_landing_page_asks_for_newsletter_consent(self):
        html = self.client.get("/").text
        self.assertIn("chk-newsletter", html)
        self.assertIn("unsubscribe anytime", html)

    def test_results_page_carries_the_consulting_cta(self):
        html = self.client.get("/").text
        self.assertIn("thomasmurray.earth", html)
        self.assertIn("Get help with your application", html)

    def test_og_image_is_served(self):
        response = self.client.get("/icons/og-image.png")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")

    # ── Feedback (C5) ───────────────────────────────────────────────────────
    def test_feedback_is_stored(self):
        with patch("main.db.log_feedback") as log:
            response = self.client.post("/feedback", json={
                "job_id": "job-1", "rating": 4,
                "comment": "Missed a UK programme", "may_contact": True,
            })
        self.assertEqual(response.status_code, 200)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][1], 4)

    def test_feedback_rejects_out_of_range_rating(self):
        with patch("main.db.log_feedback") as log:
            response = self.client.post("/feedback", json={"job_id": "j", "rating": 9})
        self.assertEqual(response.status_code, 400)
        log.assert_not_called()

    # ── Funnel events (C6) ──────────────────────────────────────────────────
    def test_event_is_recorded(self):
        with patch("main.db.log_event") as log:
            response = self.client.post("/event", json={"event": "landing_view"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(log.call_args[0][0], "landing_view")

    def test_event_requires_a_name(self):
        with patch("main.db.log_event") as log:
            response = self.client.post("/event", json={"event": "  "})
        self.assertEqual(response.status_code, 400)
        log.assert_not_called()

    # ── Waitlist / capacity fallback (C10) ──────────────────────────────────
    def test_waitlist_accepts_an_email(self):
        with patch("main.db.log_waitlist") as log:
            response = self.client.post(
                "/waitlist", json={"email": "founder@example.com", "reason": "at_capacity"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(log.call_args[0][0], "founder@example.com")

    def test_waitlist_rejects_a_non_email(self):
        with patch("main.db.log_waitlist") as log:
            response = self.client.post("/waitlist", json={"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        log.assert_not_called()

    # ── Rate limiting (C10) ─────────────────────────────────────────────────
    def test_analyse_is_refused_when_at_capacity(self):
        for i in range(main.MAX_CONCURRENT_JOBS):
            main._jobs[f"running-{i}"] = {"status": "phase23_running"}

        with patch("main.db.log_event") as log_event, \
             patch("main.db.log_started") as log_started:
            response = self.client.post("/analyse", json={"text": "a climate startup"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["reason"], "at_capacity")
        # No analysis was started — the point of the limit is to not spend money.
        log_started.assert_not_called()
        self.assertEqual(log_event.call_args[0][0], "at_capacity")

    def test_analyse_is_refused_over_the_daily_cap(self):
        with patch("main.db.count_recent_runs_for_ip",
                   return_value=main.MAX_RUNS_PER_IP_PER_DAY), \
             patch("main.db.log_event") as log_event, \
             patch("main.db.log_started") as log_started:
            response = self.client.post("/analyse", json={"text": "a climate startup"})

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["reason"], "daily_limit")
        log_started.assert_not_called()
        self.assertEqual(log_event.call_args[0][0], "rate_limited")

    def test_daily_cap_does_not_lock_users_out_when_the_database_is_down(self):
        # count_recent_runs_for_ip returns None when Supabase is unreachable.
        # That must not read as "over the limit".
        with patch("main.db.count_recent_runs_for_ip", return_value=None):
            self.assertEqual(main._ip_runs("1.2.3.4"), 0)


class CostAccountingTest(unittest.TestCase):
    """C6: per-analysis API cost, without which the pricing gates can't fire."""

    def test_cost_is_computed_from_token_usage(self):
        import analyzer
        usage = {
            "api_calls": 3,
            "input_tokens": 1_000_000,      # $3.00
            "output_tokens": 100_000,       # $1.50
            "cache_read_tokens": 1_000_000,  # $0.30
            "cache_write_tokens": 0,
        }
        self.assertAlmostEqual(analyzer.usage_cost_usd(usage), 4.80, places=2)

    def test_cost_of_no_usage_is_zero(self):
        import analyzer
        self.assertEqual(analyzer.usage_cost_usd(None), 0.0)

    def test_usage_accumulates_across_calls(self):
        import analyzer

        class FakeUsage:
            input_tokens = 100
            output_tokens = 50
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 10

        acc = analyzer.start_usage_tracking()
        analyzer._record_usage(FakeUsage())
        analyzer._record_usage(FakeUsage())
        self.assertEqual(acc["api_calls"], 2)
        self.assertEqual(acc["input_tokens"], 200)
        self.assertEqual(acc["output_tokens"], 100)
        self.assertEqual(acc["cache_read_tokens"], 20)

        # Phase 2/3 continues counting into the same accumulator.
        resumed = analyzer.start_usage_tracking(acc)
        analyzer._record_usage(FakeUsage())
        self.assertIs(resumed, acc)
        self.assertEqual(acc["api_calls"], 3)


class ServerSideAutoContinueTest(unittest.IsolatedAsyncioTestCase):
    """
    Phase 1 must hand off to Phase 2+3 on a SERVER-side timer, so a run finishes
    even if the user has closed their browser. An open browser can still pre-empt
    (Find grants) or pause (editing) the timer.
    """

    PREFS = {"geographies": None, "consortium": True,
             "accelerators": True, "prizes": True}

    def setUp(self):
        # Fake, instant phases so no real API calls happen.
        async def fake_phase1(url=None, extra_text=None, preferences=None):
            yield {"type": "profile_ready",
                   "profile": {"name": "TestCo"}, "source_note": ""}

        async def fake_phase23(profile, preferences):
            yield {"type": "complete",
                   "result": {"opportunities": [{"n": 1}, {"n": 2}],
                              "company_profile": {"name": "TestCo"}}}

        self._saved = (main.run_phase1, main.run_phase23,
                       main.AUTO_CONTINUE_DELAY_SECONDS)
        main.run_phase1 = fake_phase1
        main.run_phase23 = fake_phase23
        main.AUTO_CONTINUE_DELAY_SECONDS = 0  # fire almost immediately in tests

        self._patches = [
            patch("main.analyzer.start_usage_tracking", lambda *a, **k: {}),
            patch("main.analyzer.usage_cost_usd", lambda *a, **k: 0.0),
            patch("main.db.log_profile_ready", lambda *a, **k: None),
            patch("main.db.log_completed", lambda *a, **k: None),
            patch("main.db.log_failed", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        (main.run_phase1, main.run_phase23,
         main.AUTO_CONTINUE_DELAY_SECONDS) = self._saved
        for store in (main._jobs, main._results, main._phase23_started,
                      main._auto_paused, main._auto_continue_tasks):
            store.clear()

    def _fresh_job(self, job_id):
        main._jobs[job_id] = {
            "status": "started", "progress": [], "stage": 1, "profile": None,
            "source_note": "", "result": None, "error": None, "prefs": self.PREFS,
        }

    async def _wait_status(self, job_id, target, timeout=3.0):
        import asyncio
        for _ in range(int(timeout / 0.02)):
            if main._jobs[job_id]["status"] == target:
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_auto_continues_without_a_browser_call(self):
        self._fresh_job("j1")
        await main._phase1_task("j1", "https://x", None, self.PREFS)
        self.assertEqual(main._jobs["j1"]["status"], "profile_ready")
        # No /analyse/continue call — the server timer must drive it.
        self.assertTrue(await self._wait_status("j1", "completed"),
                        "server did not auto-continue Phase 2+3")

    async def test_pause_holds_the_timer(self):
        import asyncio
        self._fresh_job("j2")
        await main._phase1_task("j2", "https://x", None, self.PREFS)
        await main.analyse_pause(main.PauseRequest(job_id="j2"))
        await asyncio.sleep(0.1)  # well past the (0s) delay
        self.assertEqual(main._jobs["j2"]["status"], "profile_ready",
                         "paused job auto-continued anyway")

    async def test_manual_continue_starts_phase23_once(self):
        self._fresh_job("j3")
        await main._phase1_task("j3", "https://x", None, self.PREFS)
        await main.analyse_pause(main.PauseRequest(job_id="j3"))  # stop the timer
        await main.analyse_continue(
            main.ContinueRequest(job_id="j3", profile={"name": "TestCo"}))
        # A racing second click must be a harmless no-op, not a second run.
        await main.analyse_continue(
            main.ContinueRequest(job_id="j3", profile={"name": "TestCo"}))
        self.assertTrue(await self._wait_status("j3", "completed"))
        self.assertIn("j3", main._phase23_started)


class NoResultsEmailTest(unittest.TestCase):
    """
    The app must never promise to email anyone their results.

    Removed 1 August 2026: no mail provider was ever configured, so the
    landing page's "Email me my results when they're ready — you can safely
    lock your screen" was a promise the app could not keep. Anyone who left an
    address and locked their phone got nothing, and the failure was invisible
    because the send path returned silently when the provider was unset.

    These tests exist so the promise cannot come back without someone
    deliberately deleting them.
    """

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.html = cls.client.get("/").text

    def tearDown(self):
        main._jobs.clear()
        main._ip_runs_today.clear()

    def test_no_mail_sending_module_is_wired_into_the_app(self):
        self.assertFalse(
            hasattr(main, "email_sender"),
            "main imports a mail sender again — if a provider is now "
            "configured, update these tests deliberately rather than by "
            "accident.",
        )

    def test_landing_page_does_not_promise_to_email_results(self):
        lowered = self.html.lower()
        for phrase in (
            "email me my results",
            "send you the results",
            "email you your results",
            "we'll email you when your analysis",
        ):
            self.assertNotIn(
                phrase, lowered,
                f"landing page promises a results email: {phrase!r}",
            )

    def test_landing_page_still_explains_how_results_arrive(self):
        # Removing the promise must not leave the user wondering what happens
        # if they lock their screen — push is the path that actually works.
        lowered = self.html.lower()
        self.assertIn("appear on this page", lowered)
        self.assertIn("notification", lowered)

    def test_digest_consent_is_still_offered(self):
        # The address is still collected for the deadline digest, which is a
        # §1 lead-magnet function and is not a delivery promise.
        self.assertIn("chk-newsletter", self.html)
        self.assertIn("unsubscribe anytime", self.html)

    def test_address_is_stored_only_with_digest_consent(self):
        with patch("main.db.log_started") as log_started, \
             patch("main.db.count_recent_runs_for_ip", return_value=0), \
             patch("main.analyzer.start_usage_tracking", lambda *a, **k: {}):
            self.client.post("/analyse", json={
                "text": "a climate startup",
                "email": "founder@example.com",
                "newsletter": True,
            })
        self.assertEqual(log_started.call_args[0][8], "founder@example.com")

    def test_address_is_discarded_without_digest_consent(self):
        # Before this change the field doubled as "email me my results", so an
        # address typed without ticking the box still had a purpose. It no
        # longer does, so it must not be kept.
        with patch("main.db.log_started") as log_started, \
             patch("main.db.count_recent_runs_for_ip", return_value=0), \
             patch("main.analyzer.start_usage_tracking", lambda *a, **k: {}):
            self.client.post("/analyse", json={
                "text": "a climate startup",
                "email": "founder@example.com",
                "newsletter": False,
            })
        self.assertIsNone(log_started.call_args[0][8])

    def test_continue_still_accepts_a_stale_email_field(self):
        # Installed PWAs cache the frontend. A client holding the old page will
        # keep posting `email` to /analyse/continue; that must not 422.
        req = main.ContinueRequest(
            job_id="stale", profile={"name": "TestCo"},
            email="founder@example.com",
        )
        self.assertEqual(req.email, "founder@example.com")

    def test_capacity_copy_promises_a_human_not_an_automated_send(self):
        for message in (main.AT_CAPACITY_MESSAGE, main.DAILY_LIMIT_MESSAGE):
            lowered = message.lower()
            self.assertNotIn("send you the results", lowered)
            self.assertIn("thomas", lowered)
