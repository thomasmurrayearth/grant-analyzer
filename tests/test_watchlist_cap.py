"""
Tests for the watchlist cap.

`WATCHLIST_CAP` was declared and never read for months, which is why real runs
shipped watchlists of 24–27 rows beside three or four recommendations. These
tests exist so it stays enforced, and so the two things that make the cap safe
rather than merely tidy — ranking by relevance, and being deterministic — are
not quietly lost in a later refactor.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyzer


def item(name, fit=None, link=None):
    out = {"name": name}
    if fit is not None:
        out["thematic_fit"] = fit
    if link is not None:
        out["application_link"] = link
    return out


class WatchlistCapTest(unittest.TestCase):

    def test_the_cap_is_twenty(self):
        # Thomas's decision, 1 August 2026: 10 discarded too much while the
        # main shortlist is still short. If this changes, it should be a
        # decision, not a drift.
        self.assertEqual(analyzer.WATCHLIST_CAP, 20)

    def test_a_short_watchlist_loses_nothing(self):
        watchlist = [item(f"P{i}", fit=3) for i in range(5)]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist)
        self.assertEqual(len(capped), 5)
        self.assertEqual(trimmed, 0)

    def test_a_short_watchlist_is_still_ranked(self):
        # Dropping a tail is only defensible if the list is ordered by
        # relevance, so the ordering must not depend on whether a trim
        # happened to be needed.
        watchlist = [item("low", fit=1), item("high", fit=5), item("mid", fit=3)]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist)
        self.assertEqual([i["name"] for i in capped], ["high", "mid", "low"])
        self.assertEqual(trimmed, 0)

    def test_a_watchlist_exactly_at_the_cap_is_untouched(self):
        watchlist = [item(f"P{i}", fit=3) for i in range(20)]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist)
        self.assertEqual(len(capped), 20)
        self.assertEqual(trimmed, 0)

    def test_an_over_long_watchlist_is_trimmed_and_the_loss_is_reported(self):
        # The real observed case: 27 rows beside 3 recommendations.
        watchlist = [item(f"P{i}", fit=3) for i in range(27)]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist)
        self.assertEqual(len(capped), 20)
        self.assertEqual(trimmed, 7)

    def test_the_most_relevant_survive(self):
        watchlist = [item("low", fit=1), item("high", fit=5), item("mid", fit=3)]
        capped, _ = analyzer._apply_watchlist_cap(watchlist, cap=2)
        self.assertEqual([i["name"] for i in capped], ["high", "mid"])

    def test_a_clickable_link_breaks_a_tie(self):
        # Between two equally relevant entries, the one a reader can act on
        # is worth more than the one they'd have to go and find.
        watchlist = [
            item("no-link", fit=4),
            item("linked", fit=4, link="https://funder.example/apply"),
        ]
        capped, _ = analyzer._apply_watchlist_cap(watchlist, cap=1)
        self.assertEqual(capped[0]["name"], "linked")

    def test_a_non_http_link_does_not_count_as_clickable(self):
        watchlist = [
            item("placeholder", fit=4, link="unknown"),
            item("real", fit=4, link="https://funder.example/apply"),
        ]
        capped, _ = analyzer._apply_watchlist_cap(watchlist, cap=1)
        self.assertEqual(capped[0]["name"], "real")

    def test_items_with_no_fit_rank_last_but_are_not_dropped_early(self):
        watchlist = [item("unscored"), item("scored", fit=2)]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist, cap=2)
        self.assertEqual([i["name"] for i in capped], ["scored", "unscored"])
        self.assertEqual(trimmed, 0)

    def test_the_result_is_deterministic_for_a_given_input(self):
        watchlist = [item("Beta", fit=3), item("Alpha", fit=3), item("Gamma", fit=3)]
        first, _ = analyzer._apply_watchlist_cap(list(watchlist), cap=2)
        second, _ = analyzer._apply_watchlist_cap(list(watchlist), cap=2)
        self.assertEqual([i["name"] for i in first], [i["name"] for i in second])

    def test_ties_keep_upstream_order_rather_than_going_alphabetical(self):
        # Real runs produce watchlists with no fit on any entry. An
        # alphabetical tiebreak then degenerates into discarding the tail of
        # the alphabet — losing "Innovate UK Smart Grants" to an also-ran on
        # its initial letter. Upstream order is the model's own judgement.
        watchlist = [item("Zeta"), item("Alpha"), item("Innovate UK Smart Grants")]
        capped, trimmed = analyzer._apply_watchlist_cap(watchlist, cap=2)
        self.assertEqual([i["name"] for i in capped], ["Zeta", "Alpha"])
        self.assertEqual(trimmed, 1)

    def test_initial_thematic_fit_is_used_when_the_backfill_missed(self):
        # The backfill matches by name against the longlist, so it misses any
        # watchlist entry the scoring model invented. Those items still carry
        # a discovery-stage rating.
        watchlist = [
            {"name": "backfilled", "thematic_fit": 2},
            {"name": "model-invented", "initial_thematic_fit": 5},
        ]
        capped, _ = analyzer._apply_watchlist_cap(watchlist, cap=1)
        self.assertEqual(capped[0]["name"], "model-invented")

    def test_an_empty_watchlist_is_safe(self):
        capped, trimmed = analyzer._apply_watchlist_cap([])
        self.assertEqual(capped, [])
        self.assertEqual(trimmed, 0)

    def test_a_zero_or_negative_cap_disables_trimming(self):
        # A misconfigured constant should degrade to "no cap", never to
        # "no output".
        watchlist = [item(f"P{i}", fit=3) for i in range(5)]
        for cap in (0, -1):
            capped, trimmed = analyzer._apply_watchlist_cap(watchlist, cap=cap)
            self.assertEqual(len(capped), 5)
            self.assertEqual(trimmed, 0)

    def test_original_items_are_not_mutated(self):
        watchlist = [item("keep", fit=5), item("drop", fit=1)]
        analyzer._apply_watchlist_cap(watchlist, cap=1)
        self.assertEqual(len(watchlist), 2)
        self.assertEqual(watchlist[0]["name"], "keep")


if __name__ == "__main__":
    unittest.main()
