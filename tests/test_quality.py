"""
Tests for `quality.py` — the measurement library the review cycle trusts.

These matter more than most tests in the repo. Every other test protects a
feature; these protect the instrument. If scoring drifts silently, the
fortnightly cycle keeps producing confident numbers that mean something
different from what they meant last time, and the trend — the whole point of
the exercise — becomes fiction.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import quality  # noqa: E402


def opp(name, body="", **kwargs):
    """A minimal recommendation. Defaults are deliberately *clean* so each test
    introduces exactly one defect and the assertion can only be about that."""
    item = {
        "name": name,
        "managing_body": body,
        "priority_tier": kwargs.pop("tier", "Quick Win"),
        "priority_score": kwargs.pop("score", 3.5),
        "application_link": kwargs.pop("link", "https://example.org/apply"),
        "link_type": kwargs.pop("link_type", "application_portal"),
        "link_status": kwargs.pop("link_status", "verified"),
        "applicant_type_match": kwargs.pop("applicant_type_match", "direct"),
        "has_application_process": kwargs.pop("has_application_process", True),
        "trl_match": kwargs.pop("trl_match", True),
        "geography_match": kwargs.pop("geography_match", True),
        "application_timing": kwargs.pop("timing", "open_now"),
    }
    item.update(kwargs)
    return item


def result(main=(), watch=()):
    return {"opportunities": list(main), "strategic_watchlist": list(watch)}


class NormalisationTest(unittest.TestCase):
    def test_strips_years_punctuation_and_scaffolding_words(self):
        self.assertEqual(
            quality.normalise_name("Acme Innovation Fund (2026 Round 2) — open call"),
            "acme innovation",
        )

    def test_two_wordings_of_one_programme_normalise_together(self):
        a = quality.normalise_name("Northlight Accelerator Open 2025/2026")
        b = quality.normalise_name("Northlight Accelerator — 2026 cut-offs")
        self.assertEqual(a, b)

    def test_empty_input_is_safe(self):
        self.assertEqual(quality.normalise_name(""), "")
        self.assertEqual(quality.normalise_name(None), "")


class FamilyKeyTest(unittest.TestCase):
    def test_funder_words_in_a_title_do_not_create_a_second_identity(self):
        self.assertEqual(
            quality.family_key("Vantor Agency Beacon Grants", "Vantor Agency"),
            quality.family_key("Beacon Grants", "Vantor Agency"),
        )

    def test_an_abbreviation_that_expands_in_the_title_becomes_the_identity(self):
        self.assertEqual(
            quality.family_key("Coastal Resilience Partnership (CRP) Round 4", "Vantor"),
            "crp",
        )

    def test_an_umbrella_abbreviation_does_not_merge_distinct_programmes(self):
        # "VRC" spells nothing in either title, so it is the umbrella's name,
        # not the programme's. Merging on it would inflate agreement — exactly
        # the reassurance this measure must refuse to give.
        first = quality.family_key("VRC Accelerator Open 2026", "European Commission")
        second = quality.family_key("VRC Pathfinder 2026", "European Commission")
        self.assertNotEqual(first, second)

    def test_generic_abbreviations_are_ignored(self):
        # SME abbreviates nothing in the title and identifies a company size.
        self.assertNotEqual(
            quality.family_key("SME Growth Support Fund", "Vantor"), "sme",
        )

    def test_a_title_that_only_repeats_the_funder_falls_back_to_the_funder(self):
        self.assertEqual(
            quality.family_key("Vantor Agency", "Vantor Agency"), "vantor",
        )


class FamilyResolverTest(unittest.TestCase):
    def test_learns_an_abbreviation_from_one_run_and_applies_it_to_another(self):
        items = [
            opp("Coastal Resilience Partnership (CRP): Round 2", "Vantor"),
            opp("Coastal Resilience Partnership", "Vantor"),
        ]
        resolver = quality.FamilyResolver(items)
        self.assertEqual(resolver.key(items[0]), resolver.key(items[1]))
        self.assertEqual(resolver.key(items[1]), "crp")

    def test_refuses_to_learn_an_ambiguous_pairing(self):
        # One description seen with two abbreviations is a coincidence, not an
        # alias. Guessing would merge two different programmes.
        items = [
            opp("Coastal Resilience Partnership (CRP)", "Vantor"),
            opp("Coastal Resilience Fund (CRF)", "Vantor"),
            opp("Coastal Resilience", "Vantor"),
        ]
        resolver = quality.FamilyResolver(items)
        self.assertIn("coastal resilience", resolver.ambiguous)
        self.assertEqual(resolver.key(items[2]), "coastal resilience")

    def test_unrelated_programmes_keep_separate_keys(self):
        items = [
            opp("Coastal Resilience Partnership (CRP)", "Vantor"),
            opp("Upland Restoration Fund", "Vantor"),
        ]
        resolver = quality.FamilyResolver(items)
        self.assertNotEqual(resolver.key(items[0]), resolver.key(items[1]))


class GroundTruthTest(unittest.TestCase):
    case = {
        "id": "demo",
        "must_appear_in_main": ["Beacon"],
        "must_appear_in_watchlist_or_main": ["Harbour"],
        "must_not_appear_in_main": ["Deepwater"],
        "must_not_appear_anywhere": ["Orbital"],
    }

    def test_a_perfect_run_scores_one_on_both_measures(self):
        scored = quality.score_against_ground_truth(
            result(main=[opp("Beacon Grants")], watch=[opp("Harbour Fund")]), self.case,
        )
        self.assertEqual(scored["recall"], 1.0)
        self.assertEqual(scored["exclusion_accuracy"], 1.0)
        self.assertEqual(scored["must_not_violations"], 0)

    def test_a_missing_programme_is_a_recall_failure_and_is_named(self):
        scored = quality.score_against_ground_truth(
            result(main=[opp("Beacon Grants")]), self.case,
        )
        self.assertEqual(scored["recall"], 0.5)
        self.assertEqual(scored["misses"], ["Harbour"])

    def test_recommending_an_ineligible_programme_is_recorded_as_a_violation(self):
        scored = quality.score_against_ground_truth(
            result(main=[opp("Beacon Grants"), opp("Deepwater Challenge")]), self.case,
        )
        self.assertEqual(scored["must_not_violations"], 1)
        self.assertEqual(scored["violations"][0]["rule"], "must_not_appear_in_main")

    def test_a_watchlist_entry_satisfies_a_must_not_appear_in_main_rule(self):
        # Surfacing a programme the company cannot lead, as a partner route, is
        # the behaviour the watchlist exists for — not a defect.
        scored = quality.score_against_ground_truth(
            result(main=[opp("Beacon Grants")],
                   watch=[opp("Harbour Fund"), opp("Deepwater Challenge")]),
            self.case,
        )
        self.assertEqual(scored["must_not_violations"], 0)

    def test_a_confirmed_ineligible_programme_anywhere_is_a_violation(self):
        scored = quality.score_against_ground_truth(
            result(main=[opp("Beacon Grants")],
                   watch=[opp("Harbour Fund"), opp("Orbital Prize")]),
            self.case,
        )
        self.assertEqual(scored["must_not_violations"], 1)

    def test_matching_considers_the_managing_body(self):
        # A pattern naming a funder must catch a programme of theirs whose
        # title never repeats the funder's name.
        scored = quality.score_against_ground_truth(
            result(main=[opp("Knowledge Transfer Partnership", "Beacon Agency")]),
            {"id": "demo", "must_appear_in_main": ["Beacon"]},
        )
        self.assertEqual(scored["recall"], 1.0)

    def test_a_case_with_no_rules_reports_none_rather_than_a_flattering_zero(self):
        scored = quality.score_against_ground_truth(result(), {"id": "empty"})
        self.assertIsNone(scored["recall"])
        self.assertIsNone(scored["exclusion_accuracy"])


class DefensibilityTest(unittest.TestCase):
    def test_a_clean_run_is_fully_defensible(self):
        d = quality.defensibility(result(main=[opp("A"), opp("B")]))
        self.assertEqual(d["structural_precision"], 1.0)
        self.assertEqual(d["failures"], {})

    def test_each_defect_class_is_detected_and_named(self):
        cases = {
            "broken_link": opp("A", link_status="broken"),
            "no_usable_link": opp("A", link=""),
            "homepage_only_link": opp("A", link_type="funder_homepage"),
            "trl_mismatch": opp("A", trl_match=False),
            "geography_mismatch": opp("A", geography_match=False),
            "no_application_process": opp("A", has_application_process=False),
            "deadline_passed": opp("A", timing="passed"),
            "not_directly_applicable": opp("A", applicant_type_match="partner_only"),
        }
        for expected, item in cases.items():
            with self.subTest(defect=expected):
                d = quality.defensibility(result(main=[item]))
                self.assertIn(expected, d["failures"])
                self.assertEqual(d["structural_precision"], 0.0)

    def test_one_item_with_several_defects_counts_once_against_precision(self):
        d = quality.defensibility(result(main=[
            opp("A", link_status="broken", trl_match=False), opp("B"),
        ]))
        self.assertEqual(d["structural_precision"], 0.5)
        self.assertEqual(len(d["offenders"]), 1)

    def test_unknown_applicant_type_is_not_treated_as_a_defect(self):
        # Absence of evidence isn't evidence of a defect; flagging it would
        # make the measure punish honesty about uncertainty.
        d = quality.defensibility(result(main=[opp("A", applicant_type_match="unknown")]))
        self.assertEqual(d["structural_precision"], 1.0)

    def test_watchlist_items_are_not_judged_as_recommendations(self):
        d = quality.defensibility(
            result(main=[opp("A")], watch=[opp("B", applicant_type_match="partner_only")]),
        )
        self.assertEqual(d["structural_precision"], 1.0)

    def test_an_empty_run_reports_none_not_a_perfect_score(self):
        self.assertIsNone(quality.defensibility(result())["structural_precision"])


class TierSpreadTest(unittest.TestCase):
    def test_one_tier_for_everything_is_full_concentration(self):
        spread = quality.tier_spread(result(main=[
            opp("A", tier="Must Pursue", score=4.8),
            opp("B", tier="Must Pursue", score=4.7),
        ]))
        self.assertEqual(spread["concentration"], 1.0)
        self.assertEqual(spread["distinct_tiers"], 1)

    def test_a_spread_of_tiers_lowers_concentration(self):
        spread = quality.tier_spread(result(main=[
            opp("A", tier="Must Pursue", score=4.8),
            opp("B", tier="Quick Win", score=3.2),
            opp("C", tier="Next Window", score=2.1),
        ]))
        self.assertAlmostEqual(spread["concentration"], 0.333, places=2)
        self.assertEqual(spread["distinct_tiers"], 3)
        self.assertAlmostEqual(spread["score_spread"], 2.7, places=2)


class LinkQualityTest(unittest.TestCase):
    def test_counts_specific_links_and_broken_ones(self):
        stats = quality.link_quality(result(
            main=[opp("A"), opp("B", link_type="funder_homepage")],
            watch=[opp("C", link_status="broken")],
        ))
        self.assertEqual(stats["total"], 3)
        self.assertAlmostEqual(stats["specific_link_rate"], 0.667, places=2)
        self.assertAlmostEqual(stats["broken_rate"], 0.333, places=2)

    def test_missing_urls_are_counted(self):
        stats = quality.link_quality(result(main=[opp("A", link="")]))
        self.assertEqual(stats["no_url"], 1)


class ShapeTest(unittest.TestCase):
    def test_reports_the_shortfall_against_what_was_promised(self):
        s = quality.shape(result(main=[opp("A")] * 4, watch=[opp("W")] * 21),
                          promised_main=10)
        self.assertEqual(s["shortfall"], 6)
        self.assertEqual(s["watchlist_ratio"], 5.25)

    def test_no_shortfall_when_the_promise_is_met(self):
        self.assertEqual(
            quality.shape(result(main=[opp("A")] * 10), promised_main=10)["shortfall"], 0,
        )


class ConvergenceTest(unittest.TestCase):
    def test_a_single_run_is_reported_as_incomparable_not_as_perfect(self):
        c = quality.convergence([result(main=[opp("A")])])
        self.assertFalse(c["comparable"])

    def test_identical_runs_converge_completely(self):
        run = result(main=[opp("Beacon Grants", "Vantor"), opp("Harbour Fund", "Vantor")])
        c = quality.convergence([run, run])
        self.assertEqual(c["exact"]["mean_pairwise_jaccard"], 1.0)
        self.assertEqual(c["family"]["core_share"], 1.0)

    def test_disjoint_runs_converge_not_at_all(self):
        c = quality.convergence([
            result(main=[opp("Beacon Grants", "Vantor")]),
            result(main=[opp("Upland Restoration", "Meridian")]),
        ])
        self.assertEqual(c["exact"]["mean_pairwise_jaccard"], 0.0)
        self.assertEqual(c["family"]["in_every_run"], 0)

    def test_the_same_programme_titled_differently_meets_at_family_level(self):
        c = quality.convergence([
            result(main=[opp("Coastal Resilience Partnership (CRP): 2026 Round 2", "Vantor")]),
            result(main=[opp("Coastal Resilience Partnership for Estuaries", "Vantor Agency")]),
        ])
        self.assertEqual(c["exact"]["mean_pairwise_jaccard"], 0.0)
        self.assertEqual(c["family"]["mean_pairwise_jaccard"], 1.0)
        self.assertGreater(c["identity_gap"], 0)

    def test_the_watchlist_can_be_included_in_scope(self):
        runs = [
            result(main=[opp("A", "V")], watch=[opp("B", "V")]),
            result(main=[opp("B", "V")], watch=[opp("A", "V")]),
        ]
        self.assertEqual(quality.convergence(runs, scope="main")["exact"]["mean_pairwise_jaccard"], 0.0)
        self.assertEqual(quality.convergence(runs, scope="all")["exact"]["mean_pairwise_jaccard"], 1.0)


class GroupingTest(unittest.TestCase):
    def test_only_companies_seen_more_than_once_are_returned(self):
        groups = quality.group_repeat_runs([
            {"company_url": "https://a.example/"},
            {"company_url": "https://a.example"},
            {"company_url": "https://b.example"},
        ])
        self.assertEqual(list(groups), ["https://a.example"])
        self.assertEqual(len(groups["https://a.example"]), 2)

    def test_runs_without_a_url_are_ignored_rather_than_grouped_together(self):
        # Text-only analyses share an empty URL; grouping them would compare
        # different companies and report the churn as poor convergence.
        self.assertEqual(quality.group_repeat_runs([{"company_url": ""}] * 3), {})


class SummariseTest(unittest.TestCase):
    def test_reports_sample_size_alongside_every_figure(self):
        runs = [quality.assess_run(result(main=[opp("A")])) for _ in range(3)]
        summary = quality.summarise(runs)
        self.assertEqual(summary["runs"], 3)
        self.assertEqual(summary["structural_precision"], 1.0)

    def test_no_runs_summarises_to_nothing_rather_than_zero(self):
        self.assertEqual(quality.summarise([]), {"runs": 0})

    def test_defect_totals_are_pooled_across_runs(self):
        runs = [
            quality.assess_run(result(main=[opp("A", link_status="broken")])),
            quality.assess_run(result(main=[opp("B", link_status="broken")])),
        ]
        self.assertEqual(quality.summarise(runs)["defect_totals"]["broken_link"], 2)


class UnwrapTest(unittest.TestCase):
    def test_accepts_a_stored_eval_file_as_well_as_a_raw_analysis(self):
        raw = result(main=[opp("A")])
        wrapped = {"result": raw, "findings": {}}
        self.assertEqual(quality.items_of(wrapped)[0], quality.items_of(raw)[0])

    def test_junk_input_does_not_raise(self):
        self.assertEqual(quality.items_of(None), ([], []))
        self.assertEqual(quality.items_of({"opportunities": "not a list"}), ([], []))


if __name__ == "__main__":
    unittest.main()
