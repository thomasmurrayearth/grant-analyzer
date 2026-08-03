"""
Tests for the post-scoring specificity validation.

This step decides whether a scored opportunity survives as a recommendation.
It was the largest gate-side loss in the pipeline until 3 August 2026: on the
two real runs of 31 July it took five main recommendations to two, and four to
two, because every "likely" verdict was demoted off the main list.

The distinction these tests defend:

    "likely"      = the process is known, this round is not confirmed open.
                    Ordinary for a recurring programme between calls. Stays,
                    labelled honestly.
    "unconfirmed" = no evidence any public process exists. Still dropped.

Collapsing those two back together would silently reinstate the bug, so the
cases below pin both sides of the line.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer


class FakeMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]
        self.usage = None


class FakeClient:
    """Stands in for anthropic.AsyncAnthropic as an async context manager."""

    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def messages(self):
        payload = self._payload

        class _Messages:
            async def create(self, **kwargs):
                return FakeMessage(payload)

        return _Messages()


def run(opportunities, watchlist, validations, evidence=None):
    import json

    payload = json.dumps(validations)
    with patch.object(analyzer.anthropic, "AsyncAnthropic",
                      lambda *a, **k: FakeClient(payload)), \
         patch.object(analyzer, "_record_usage", lambda *a, **k: None):
        return asyncio.run(analyzer._validate_application_specificity(
            opportunities, watchlist, evidence or {},
        ))


def opp(name, tier="Quick Win", fit=5, timing="open_now", link="https://f.example/p"):
    return {
        "name": name,
        "managing_body": "Funder",
        "priority_tier": tier,
        "thematic_fit_score": fit,
        "application_timing": timing,
        "application_link": link,
        "link_type": "programme_page",
    }


class LikelyStaysInTheMainListTest(unittest.TestCase):

    def test_a_likely_programme_is_no_longer_demoted(self):
        # The regression that produced one-recommendation shortlists.
        main, watch = run(
            [opp("Innovate UK Smart Grants")], [],
            [{"name": "Innovate UK Smart Grants", "status": "likely",
              "reason": "Runs twice yearly; next round not yet announced."}],
        )
        self.assertEqual([o["name"] for o in main], ["Innovate UK Smart Grants"])
        self.assertEqual(watch, [])

    def test_timing_is_forced_to_recurring_uncertain(self):
        # Keeping the item must not import a false "apply now" framing.
        main, _ = run(
            [opp("KTP", timing="open_now")], [],
            [{"name": "KTP", "status": "likely"}],
        )
        self.assertEqual(main[0]["application_timing"], "recurring_uncertain")

    def test_an_urgent_tier_is_demoted_to_next_window(self):
        for tier in ("Must Pursue", "Quick Win"):
            main, _ = run([opp("P", tier=tier)], [],
                          [{"name": "P", "status": "likely"}])
            self.assertEqual(main[0]["priority_tier"], "Prepare for Next Window",
                             f"tier {tier!r} was left claiming urgency")

    def test_a_non_urgent_tier_is_left_alone(self):
        main, _ = run([opp("P", tier="Big Bet")], [],
                      [{"name": "P", "status": "likely"}])
        self.assertEqual(main[0]["priority_tier"], "Big Bet")

    def test_the_reader_is_told_no_round_is_open(self):
        main, _ = run(
            [opp("P")], [],
            [{"name": "P", "status": "likely", "reason": "Round 4 closed in May."}],
        )
        caveat = main[0]["timing_caveat"]
        self.assertIn("No application round is confirmed open", caveat)
        self.assertIn("Round 4 closed in May.", caveat)

    def test_a_caveat_is_present_even_without_a_stated_reason(self):
        main, _ = run([opp("P")], [], [{"name": "P", "status": "likely"}])
        self.assertTrue(main[0]["timing_caveat"].strip())

    def test_a_better_url_is_still_adopted(self):
        main, _ = run(
            [dict(opp("P"), link_type="funder_homepage")], [],
            [{"name": "P", "status": "likely",
              "best_url": "https://funder.example/programme/apply"}],
        )
        self.assertEqual(main[0]["application_link"],
                         "https://funder.example/programme/apply")
        self.assertEqual(main[0]["link_type"], "programme_page")

    def test_low_thematic_fit_no_longer_causes_a_silent_drop(self):
        # Previously a "likely" item below STRONG_FIT_MIN was discarded with
        # no trace in either array.
        main, watch = run([opp("P", fit=1)], [], [{"name": "P", "status": "likely"}])
        self.assertEqual(len(main), 1)
        self.assertEqual(watch, [])


class UnconfirmedIsStillDroppedTest(unittest.TestCase):
    """The safeguard. Loosening this too would trade a gap for a falsehood."""

    def test_unconfirmed_earns_neither_list(self):
        main, watch = run([opp("Ghost Fund")], [],
                          [{"name": "Ghost Fund", "status": "unconfirmed"}])
        self.assertEqual(main, [])
        self.assertEqual(watch, [])

    def test_a_legacy_boolean_false_is_treated_as_unconfirmed(self):
        main, _ = run([opp("P")], [], [{"name": "P", "confirmed": False}])
        self.assertEqual(main, [])

    def test_confirmed_passes_through_untouched(self):
        main, _ = run([opp("P", tier="Must Pursue")], [],
                      [{"name": "P", "status": "confirmed"}])
        self.assertEqual(main[0]["priority_tier"], "Must Pursue")
        self.assertEqual(main[0]["application_timing"], "open_now")
        self.assertNotIn("timing_caveat", main[0])


class FailureModesTest(unittest.TestCase):

    def test_an_unvalidated_item_is_kept_not_dropped(self):
        # A missing verdict is the validator's failure, not the grant's.
        main, _ = run([opp("P")], [], [])
        self.assertEqual(len(main), 1)

    def test_an_existing_watchlist_is_passed_through_unchanged(self):
        existing = [{"name": "Pre-existing"}]
        _, watch = run([opp("P")], existing, [{"name": "P", "status": "likely"}])
        self.assertEqual([w["name"] for w in watch], ["Pre-existing"])

    def test_no_opportunities_is_a_no_op(self):
        main, watch = run([], [{"name": "W"}], [])
        self.assertEqual(main, [])
        self.assertEqual([w["name"] for w in watch], ["W"])

    def test_the_original_opportunity_is_not_mutated(self):
        original = opp("P", tier="Quick Win")
        main, _ = run([original], [], [{"name": "P", "status": "likely"}])
        self.assertEqual(original["priority_tier"], "Quick Win")
        self.assertEqual(original["application_timing"], "open_now")
        self.assertEqual(main[0]["priority_tier"], "Prepare for Next Window")


class PromptContractTest(unittest.TestCase):
    """
    The other half of the fix is in the scoring prompt: the model was routing
    half the shortlist to the watchlist on uncertainty, and sometimes dropping
    items from both arrays entirely.
    """

    @staticmethod
    def _source():
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "analyzer.py",
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_prompt_demands_every_shortlisted_item_be_placed(self):
        source = self._source()
        self.assertIn("exactly one of\nthe two output arrays", source)
        self.assertIn("never neither", source)

    def test_the_prompt_separates_barriers_from_doubt(self):
        source = self._source()
        self.assertIn("The watchlist is for barriers, not for doubt", source)
        self.assertIn("Route on evidence\nof an actual barrier", source)

    def test_the_watchlist_route_requires_positive_evidence(self):
        # "false or unclear" was the wording that sent half the shortlist to
        # the watchlist on absence of evidence.
        source = self._source()
        self.assertNotIn("has_application_process is false or unclear", source)


if __name__ == "__main__":
    unittest.main()
