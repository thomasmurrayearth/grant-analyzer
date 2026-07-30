"""
Offline unit tests for analyzer.py's pure logic: mandatory-query geography
gating, fuzzy name deduplication, the rescue safety net, and the link
quality gate. None of these touch the network or the Anthropic API.
"""

import unittest

from analyzer import (
    _apply_link_quality_gate,
    _backfill_initial_thematic_fit,
    _enforce_routing_rules,
    _mandatory_queries,
    _names_similar,
    _rescue_missing_partner_items,
    _violates_known_hard_gates,
)


WELSH_PROFILE = {
    "hq": "United Kingdom",  # model sometimes omits Wales from structured fields
    "operational_geographies": ["United Kingdom"],
    "key_themes": ["energy", "decarbonisation"],
    "classification": "Climate Tech / Deeptech hardware",
    "website_summary": "Distributed compute-as-heating hardware company based in Llandow, Wales.",
    "external_findings": "",
    "value_proposition": "Replaces gas boilers with edge-compute heat.",
}

GERMAN_PROFILE = {
    "hq": "Munich, Germany",
    "operational_geographies": ["Germany", "Austria"],
    "key_themes": ["industrial decarbonisation", "process heat"],
    "classification": "Deeptech / Climate Tech hardware",
    "website_summary": "High-temperature industrial heat pumps for food and chemical plants.",
    "external_findings": "Two pilot installations near Augsburg.",
    "value_proposition": "Replaces gas-fired process heat with electric heat pumps.",
}


class MandatoryQueriesTest(unittest.TestCase):
    def test_welsh_company_fires_uk_and_wales_queries(self):
        queries = " | ".join(_mandatory_queries(WELSH_PROFILE)).lower()
        self.assertIn("innovate uk", queries)
        self.assertIn("smart fis", queries)
        self.assertIn("ofgem", queries)

    def test_german_company_fires_no_uk_or_wales_queries(self):
        queries = " | ".join(_mandatory_queries(GERMAN_PROFILE)).lower()
        self.assertNotIn("innovate uk", queries)
        self.assertNotIn("smart fis", queries)
        self.assertNotIn("business wales", queries)

    def test_german_deeptech_still_gets_eic_query(self):
        queries = " | ".join(_mandatory_queries(GERMAN_PROFILE)).lower()
        self.assertIn("eic accelerator", queries)

    def test_negated_uk_mention_does_not_trigger_uk_queries(self):
        # Real failure from the 2026-07-04 germandeeptech eval run: the
        # model's external_findings NEGATED a UK presence, and the substring
        # scan matched "UK" inside the negation.
        profile = dict(GERMAN_PROFILE)
        profile["external_findings"] = (
            "No independent press coverage was found. No evidence of UK "
            "presence, UK customers, or UK-registered entities was found."
        )
        queries = " | ".join(_mandatory_queries(profile)).lower()
        self.assertNotIn("innovate uk", queries)
        self.assertNotIn("ofgem", queries)

    def test_uk_substring_inside_german_words_does_not_trigger(self):
        profile = dict(GERMAN_PROFILE)
        profile["website_summary"] = (
            "Produkte fuer die Zukunft: Industriestruktur und Produktion."
        )
        queries = " | ".join(_mandatory_queries(profile)).lower()
        self.assertNotIn("innovate uk", queries)

    def test_affirmative_freetext_wales_still_detected(self):
        # The original fix #4 case: hq says only "United Kingdom" but the
        # website summary affirms a Welsh location.
        queries = " | ".join(_mandatory_queries(WELSH_PROFILE)).lower()
        self.assertIn("smart fis", queries)


class NamesSimilarTest(unittest.TestCase):
    def test_ktp_variants_are_duplicates(self):
        self.assertTrue(_names_similar(
            "Innovate UK Knowledge Transfer Partnership (KTP) — 2025–2026 Round 4",
            "Knowledge Transfer Partnerships (KTP) — Accelerated KTP 6 (AKT 6)",
        ))

    def test_ofgem_sif_acronym_matches_full_name(self):
        self.assertTrue(_names_similar(
            "Ofgem Strategic Innovation Fund (SIF)",
            "Ofgem SIF",
        ))

    def test_same_funder_different_programmes_are_not_duplicates(self):
        self.assertFalse(_names_similar(
            "Innovate UK Smart Grants",
            "Innovate UK Energy Catalyst",
        ))

    def test_eic_accelerator_vs_pathfinder_not_duplicates(self):
        self.assertFalse(_names_similar(
            "EIC Accelerator Open 2025",
            "EIC Pathfinder",
        ))

    def test_welsh_smart_vs_innovate_uk_smart_not_duplicates(self):
        self.assertFalse(_names_similar(
            "Welsh Government SMART FIS",
            "Innovate UK Smart Grants — Round 12",
        ))

    def test_different_funders_sharing_acronym_not_duplicates(self):
        # Both abbreviate to "SIF" but are unrelated programmes
        self.assertFalse(_names_similar(
            "Ofgem Strategic Innovation Fund (SIF) — next open round",
            "Innovate UK Sustainable Innovation Fund (SIF) — SME R&D recovery",
        ))

    def test_generic_category_vocabulary_not_duplicates(self):
        self.assertFalse(_names_similar(
            "Third Derivative (D3) Climate Tech Accelerator",
            "Greentown Labs Climate Tech Accelerator",
        ))
        self.assertFalse(_names_similar(
            "EIT Climate-KIC Accelerator — Climate Tech Cohort",
            "Greentown Labs Climate Tech Accelerator",
        ))

    def test_same_funder_sif_rounds_are_duplicates(self):
        self.assertTrue(_names_similar(
            "Ofgem Strategic Innovation Fund (SIF) Round 5 — Discovery C5",
            "Ofgem Strategic Innovation Fund (SIF) — larger development rounds",
        ))


class RescueDedupTest(unittest.TestCase):
    def _longlist_item(self, name, fit=4):
        return {
            "name": name,
            "managing_body": "Innovate UK",
            "initial_thematic_fit": fit,
            "notes": "",
        }

    def test_near_duplicate_longlist_items_rescued_once(self):
        longlist = [
            self._longlist_item("Knowledge Transfer Partnership (KTP) Round 4"),
            self._longlist_item("Knowledge Transfer Partnerships (KTP) — Accelerated KTP 6"),
        ]
        watchlist = _rescue_missing_partner_items(
            shortlist=[], opportunities=[], watchlist=[], longlist=longlist,
        )
        ktp_entries = [w for w in watchlist if "ktp" in w["name"].lower()]
        self.assertEqual(len(ktp_entries), 1)

    def test_item_already_in_watchlist_not_rescued_again(self):
        existing = {"name": "Knowledge Transfer Partnerships (KTP)"}
        longlist = [self._longlist_item("Innovate UK KTP — Round 4")]
        watchlist = _rescue_missing_partner_items(
            shortlist=[], opportunities=[], watchlist=[existing], longlist=longlist,
        )
        ktp_entries = [w for w in watchlist if "ktp" in w["name"].lower()]
        self.assertEqual(len(ktp_entries), 1)

    def test_hard_excluded_names_never_rescued(self):
        longlist = [
            self._longlist_item("EIC Pathfinder Open", fit=4),
            self._longlist_item("Innovate UK Energy Catalyst Round 12", fit=4),
        ]
        watchlist = _rescue_missing_partner_items(
            shortlist=[], opportunities=[], watchlist=[], longlist=longlist,
        )
        self.assertEqual(watchlist, [])

    def test_low_fit_items_not_rescued(self):
        longlist = [self._longlist_item("Some Marginal Fund", fit=2)]
        watchlist = _rescue_missing_partner_items(
            shortlist=[], opportunities=[], watchlist=[], longlist=longlist,
        )
        self.assertEqual(watchlist, [])


class BackfillInitialThematicFitTest(unittest.TestCase):
    """
    Watchlist items never go through the full scoring rubric, so most of them
    have no numeric thematic_fit at all — only the exporter-visible
    "unknown". This backfill copies the discovery-stage rating across by
    name so the XLSX can show a real number instead. See exporter.py's
    Priority Score formula, which needs a numeric thematic fit to compute.
    """

    def test_unscored_watchlist_item_gets_longlist_rating(self):
        longlist = [{"name": "Regional Growth Fund", "initial_thematic_fit": 4}]
        watchlist = [{"name": "Regional Growth Fund"}]
        result = _backfill_initial_thematic_fit(watchlist, longlist=longlist, shortlist=[])
        self.assertEqual(result[0]["thematic_fit"], 4)

    def test_falls_back_to_shortlist_when_not_in_longlist(self):
        # A shortlisted-but-dropped item may have been de-duplicated out of
        # the longlist entirely — the shortlist is the fallback pool.
        shortlist = [{"name": "Regional Growth Fund", "initial_thematic_fit": 5}]
        watchlist = [{"name": "Regional Growth Fund"}]
        result = _backfill_initial_thematic_fit(watchlist, longlist=[], shortlist=shortlist)
        self.assertEqual(result[0]["thematic_fit"], 5)

    def test_existing_numeric_thematic_fit_is_not_overwritten(self):
        # e.g. a "between rounds" demotion that already carries its real
        # scored thematic_fit_score forward as "thematic_fit".
        longlist = [{"name": "Regional Growth Fund", "initial_thematic_fit": 1}]
        watchlist = [{"name": "Regional Growth Fund", "thematic_fit": 4}]
        result = _backfill_initial_thematic_fit(watchlist, longlist=longlist, shortlist=[])
        self.assertEqual(result[0]["thematic_fit"], 4)

    def test_no_match_leaves_thematic_fit_unset(self):
        watchlist = [{"name": "Untraceable Programme"}]
        result = _backfill_initial_thematic_fit(watchlist, longlist=[], shortlist=[])
        self.assertNotIn("thematic_fit", result[0])

    def test_fuzzy_name_match(self):
        longlist = [{
            "name": "Knowledge Transfer Partnership (KTP) Round 4",
            "initial_thematic_fit": 3,
        }]
        watchlist = [{"name": "Knowledge Transfer Partnerships (KTP) — Accelerated KTP 6"}]
        result = _backfill_initial_thematic_fit(watchlist, longlist=longlist, shortlist=[])
        self.assertEqual(result[0]["thematic_fit"], 3)


class LinkQualityGateTest(unittest.TestCase):
    def _opp(self, tier, link_type, link_status, url="https://example.org/apply"):
        return {
            "name": "Test Grant",
            "priority_tier": tier,
            "link_type": link_type,
            "link_status": link_status,
            "application_link": url,
            "notes": "",
        }

    def test_must_pursue_with_broken_link_is_demoted(self):
        opps = [self._opp("Must Pursue", "application_portal", "broken")]
        _apply_link_quality_gate(opps)
        self.assertEqual(opps[0]["priority_tier"], "Prepare for Next Window")
        self.assertEqual(opps[0]["link_type"], "unknown")
        self.assertIn("Demoted", opps[0]["notes"])

    def test_quick_win_with_homepage_link_is_demoted(self):
        opps = [self._opp("Quick Win", "funder_homepage", "verified")]
        _apply_link_quality_gate(opps)
        self.assertEqual(opps[0]["priority_tier"], "Prepare for Next Window")

    def test_must_pursue_with_verified_portal_link_is_kept(self):
        opps = [self._opp("Must Pursue", "application_portal", "verified")]
        _apply_link_quality_gate(opps)
        self.assertEqual(opps[0]["priority_tier"], "Must Pursue")
        self.assertEqual(opps[0]["notes"], "")

    def test_lower_tiers_never_demoted(self):
        opps = [self._opp("Strategic Positioning", "unknown", "broken", url="unknown")]
        _apply_link_quality_gate(opps)
        self.assertEqual(opps[0]["priority_tier"], "Strategic Positioning")


class KnownHardGatesTest(unittest.TestCase):
    UK_TRL7 = {
        "hq": "Cardiff, United Kingdom",
        "operational_geographies": ["United Kingdom"],
        "customer_geographies": ["United Kingdom", "EU"],
        "trl": "TRL 7-8 (live commercial installs)",
    }
    ODA_TRL3 = {
        "hq": "Nairobi, Kenya",
        "operational_geographies": ["Sub-Saharan Africa"],
        "customer_geographies": ["Kenya", "Tanzania"],
        "trl": "TRL 3 (lab prototype)",
    }

    def test_energy_catalyst_excluded_for_developed_market_company(self):
        opp = {"name": "Innovate UK Energy Catalyst — UK-relevant rounds"}
        self.assertIsNotNone(_violates_known_hard_gates(opp, self.UK_TRL7))

    def test_energy_catalyst_allowed_for_oda_market_company(self):
        opp = {"name": "Innovate UK Energy Catalyst Round 12"}
        self.assertIsNone(_violates_known_hard_gates(opp, self.ODA_TRL3))

    def test_pathfinder_excluded_for_trl5_plus_company(self):
        opp = {"name": "EIC Pathfinder Open"}
        self.assertIsNotNone(_violates_known_hard_gates(opp, self.UK_TRL7))

    def test_pathfinder_allowed_for_early_trl_company(self):
        opp = {"name": "EIC Pathfinder Open"}
        self.assertIsNone(_violates_known_hard_gates(opp, self.ODA_TRL3))

    def test_no_profile_means_no_gating(self):
        opp = {"name": "Innovate UK Energy Catalyst"}
        self.assertIsNone(_violates_known_hard_gates(opp, None))

    def test_enforce_routing_drops_gated_items_from_both_arrays(self):
        opps = [
            {"name": "Innovate UK Energy Catalyst — UK-relevant rounds",
             "opportunity_type": "direct_grant", "trl_match": True,
             "geography_match": True, "applicant_type_match": "direct"},
            {"name": "Welsh Government SMART FIS",
             "opportunity_type": "direct_grant", "trl_match": True,
             "geography_match": True, "applicant_type_match": "direct"},
        ]
        watch = [{"name": "EIC Pathfinder Open"}]
        kept, watchlist = _enforce_routing_rules(opps, watch, profile=self.UK_TRL7)
        self.assertEqual([o["name"] for o in kept], ["Welsh Government SMART FIS"])
        self.assertEqual(watchlist, [])


if __name__ == "__main__":
    unittest.main()
