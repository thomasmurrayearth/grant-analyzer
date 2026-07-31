"""
Offline unit tests for analyzer.py's pure logic: mandatory-query geography
gating, fuzzy name deduplication, the rescue safety net, and the link
quality gate. None of these touch the network or the Anthropic API.
"""

import unittest
from unittest.mock import AsyncMock, patch

import quality
from analyzer import (
    _apply_recommendation_gates,
    _apply_link_quality_gate,
    _backfill_initial_thematic_fit,
    _enforce_routing_rules,
    _mandatory_queries,
    _names_similar,
    _rescue_missing_partner_items,
    _violates_declared_eligibility,
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


class DeclaredEligibilityTest(unittest.TestCase):
    """The gate reads what an item *declares*, never what it is called.

    These tests deliberately use invented programme names. The previous
    version of this gate matched two funders by name, which meant it could
    only ever catch the two programmes someone had already been burned by —
    and it violated the repo rule against hardcoding a funder seen in testing.
    Every case below would be caught for a funder nobody has heard of.
    """

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

    # ── Development-assistance restriction ──────────────────────────────
    def test_development_only_programme_excluded_for_a_developed_market_company(self):
        opp = {
            "name": "Northlight Clean Power Challenge",
            "notes": "Only ODA-eligible rounds are running; funds deployment "
                     "in developing countries.",
        }
        self.assertIsNotNone(_violates_declared_eligibility(opp, self.UK_TRL7))

    def test_the_same_programme_is_allowed_for_a_company_in_those_markets(self):
        opp = {
            "name": "Northlight Clean Power Challenge",
            "notes": "Only ODA-eligible rounds are running; funds deployment "
                     "in developing countries.",
        }
        self.assertIsNone(_violates_declared_eligibility(opp, self.ODA_TRL3))

    def test_a_passing_mention_without_the_restriction_does_not_gate(self):
        opp = {"name": "Northlight Clean Power Challenge",
               "notes": "Applicants may partner with overseas universities."}
        self.assertIsNone(_violates_declared_eligibility(opp, self.UK_TRL7))

    # ── The model conceding a mismatch it then ignored ──────────────────
    def test_prose_conceding_a_trl_mismatch_overrides_a_structured_pass(self):
        # The observed failure: trl_match says true, the explanation says the
        # company is outside the band, and the item ships anyway.
        opp = {
            "name": "Meridian Frontier Science Call",
            "trl_match": True,
            "thematic_fit_explanation": "The programme funds breakthrough "
                "research at low TRL; the company is significantly beyond the "
                "intended stage.",
        }
        self.assertIsNotNone(_violates_declared_eligibility(opp, self.UK_TRL7))

    def test_an_explicit_trl_mismatch_note_is_caught(self):
        opp = {"name": "Meridian Frontier Science Call", "trl_match": True,
               "notes": "TRL mismatch is the primary reason for low priority."}
        self.assertIsNotNone(_violates_declared_eligibility(opp, self.UK_TRL7))

    def test_a_clean_assessment_passes(self):
        opp = {
            "name": "Meridian Frontier Science Call",
            "trl_match": True,
            "thematic_fit_explanation": "Strong fit: the programme funds "
                "commercial demonstration at exactly this stage.",
        }
        self.assertIsNone(_violates_declared_eligibility(opp, self.UK_TRL7))

    def test_no_profile_means_no_gating(self):
        # Without a company to compare against, a restriction is not a
        # mismatch. Guessing would drop valid opportunities.
        opp = {"name": "Northlight Clean Power Challenge",
               "notes": "ODA-eligible rounds only."}
        self.assertIsNone(_violates_declared_eligibility(opp, None))

    def test_enforce_routing_drops_gated_items_from_both_arrays(self):
        opps = [
            {"name": "Northlight Clean Power Challenge",
             "notes": "ODA-eligible: funds deployment in developing countries.",
             "opportunity_type": "direct_grant", "trl_match": True,
             "geography_match": True, "applicant_type_match": "direct"},
            {"name": "Vantor Regional Innovation Grant",
             "opportunity_type": "direct_grant", "trl_match": True,
             "geography_match": True, "applicant_type_match": "direct"},
        ]
        watch = [{"name": "Meridian Frontier Science Call",
                  "why_watchlist": "The company is significantly beyond the "
                                   "intended stage for this call."}]
        kept, watchlist = _enforce_routing_rules(opps, watch, profile=self.UK_TRL7)
        self.assertEqual([o["name"] for o in kept],
                         ["Vantor Regional Innovation Grant"])
        self.assertEqual(watchlist, [])


if __name__ == "__main__":
    unittest.main()


class LinkAuthorityTest(unittest.TestCase):
    """Whose site does a recommendation actually point at?

    A link that loads is not a link that can be trusted. Every case here uses
    an invented funder, because the rule has to work for programmes nobody has
    seen — and errs toward "not the funder", since a false demotion costs a
    watchlist row while a false pass ships the defect unmeasured.
    """

    def test_an_official_domain_is_authoritative_whatever_it_is_called(self):
        self.assertEqual(
            quality.link_authority("https://apply.northlight.gov.uk/scheme/12",
                                   "Coastal Resilience Partnership", "Vantor Agency"),
            "funder",
        )

    def test_a_domain_sharing_the_funders_name_is_authoritative(self):
        self.assertEqual(
            quality.link_authority("https://vantor.org/grants/crp",
                                   "Coastal Resilience Partnership", "Vantor Agency"),
            "funder",
        )

    def test_a_run_together_domain_still_matches(self):
        # Organisations routinely concatenate their name into a domain.
        self.assertEqual(
            quality.link_authority("https://northlightfoundation.org/",
                                   "Northlight Prize", "The Northlight Foundation"),
            "funder",
        )

    def test_a_social_platform_is_never_the_funder(self):
        for url in ("https://www.facebook.com/someone/posts/123",
                    "https://medium.com/@writer/grants-2026",
                    "https://x.com/someone/status/1"):
            with self.subTest(url=url):
                self.assertEqual(
                    quality.link_authority(url, "Coastal Grant", "Vantor Agency"),
                    "third_party",
                )

    def test_an_unrelated_commercial_site_is_not_the_funder(self):
        # An aggregator or a consultancy's summary page. Loads fine; proves
        # nothing about where the money is.
        self.assertEqual(
            quality.link_authority("https://grantfinderpro.co.uk/listings/8821",
                                   "Coastal Resilience Partnership", "Vantor Agency"),
            "unknown",
        )

    def test_a_shared_sector_word_alone_does_not_make_a_site_authoritative(self):
        # Half this sector has "energy" or "innovation" in its name; matching
        # on those would wave through a retailer's blog on a coincidence.
        self.assertEqual(
            quality.link_authority("https://bigenergyretailer.com/blog/grants-2026",
                                   "Energy Innovation Fund", "Vantor Energy Agency"),
            "unknown",
        )

    def test_a_missing_or_malformed_link_is_not_authoritative(self):
        for url in ("", "unknown", "not a url"):
            with self.subTest(url=url):
                self.assertEqual(quality.link_authority(url, "X", "Y"), "unknown")


class RecommendationGatesTest(unittest.IsolatedAsyncioTestCase):
    """The last check before a user sees anything.

    Every case asserts two things: the item leaves the main list, and it
    arrives on the watchlist carrying a reason. Dropping a real opportunity
    silently would trade one defect for a worse one.
    """

    PROFILE = {"hq": "Cardiff, United Kingdom",
               "operational_geographies": ["United Kingdom"],
               "trl": "TRL 7-8"}

    @staticmethod
    def opp(**kwargs):
        item = {
            "name": "Coastal Resilience Partnership",
            "managing_body": "Vantor Agency",
            "application_link": "https://vantor.org/apply/crp",
            "link_type": "application_portal",
            "link_status": "verified",
            "has_application_process": True,
            "application_timing": "open_now",
            "priority_tier": "Must Pursue",
            "thematic_fit_score": 5,
        }
        item.update(kwargs)
        return item

    async def _run(self, opps, watch=None):
        return await _apply_recommendation_gates(
            opps, watch if watch is not None else [], profile=self.PROFILE,
        )

    async def test_a_clean_recommendation_survives(self):
        kept, watch = await self._run([self.opp()])
        self.assertEqual(len(kept), 1)
        self.assertEqual(watch, [])

    async def test_an_item_with_no_application_process_is_demoted(self):
        kept, watch = await self._run([self.opp(has_application_process=False)])
        self.assertEqual(kept, [])
        self.assertIn("application process", watch[0]["why_watchlist"])

    async def test_a_passed_deadline_is_demoted(self):
        kept, watch = await self._run([self.opp(application_timing="passed")])
        self.assertEqual(kept, [])
        self.assertIn("closed", watch[0]["why_watchlist"])

    async def test_a_declared_eligibility_mismatch_is_demoted(self):
        kept, watch = await self._run([self.opp(
            notes="ODA-eligible only: funds deployment in developing countries.",
        )])
        self.assertEqual(kept, [])
        self.assertIn("development-assistance", watch[0]["why_watchlist"])

    async def test_a_non_funder_link_is_demoted(self):
        kept, watch = await self._run([self.opp(
            application_link="https://www.facebook.com/someone/posts/1",
        )])
        self.assertEqual(kept, [])
        self.assertIn("social media", watch[0]["why_watchlist"])

    async def test_an_aggregator_link_is_demoted(self):
        kept, watch = await self._run([self.opp(
            application_link="https://grantfinderpro.co.uk/listings/8821",
        )])
        self.assertEqual(kept, [])
        self.assertIn("third-party", watch[0]["why_watchlist"])

    async def test_a_broken_link_gets_one_recheck_before_demotion(self):
        # A single timeout is not evidence of a dead page, and demoting a good
        # programme on a blip costs more than the re-check does.
        with patch("analyzer._check_one_link", new=AsyncMock(return_value="verified")):
            kept, watch = await self._run([self.opp(link_status="broken")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["link_status"], "verified")

    async def test_a_link_broken_on_both_checks_is_demoted(self):
        with patch("analyzer._check_one_link", new=AsyncMock(return_value="broken")):
            kept, watch = await self._run([self.opp(link_status="broken")])
        self.assertEqual(kept, [])
        self.assertIn("two checks", watch[0]["why_watchlist"])

    async def test_demoted_items_keep_their_scoring_work(self):
        kept, watch = await self._run([self.opp(
            has_application_process=False, thematic_fit_score=4,
            thematic_fit_explanation="Strong thematic overlap.",
        )])
        self.assertEqual(watch[0]["thematic_fit"], 4)
        self.assertEqual(watch[0]["thematic_relevance"], "Strong thematic overlap.")
        self.assertEqual(watch[0]["watchlist_class"], "demoted_from_main")

    async def test_nothing_is_ever_deleted(self):
        opps = [self.opp(name="A", has_application_process=False),
                self.opp(name="B", application_timing="passed"),
                self.opp(name="C")]
        kept, watch = await self._run(opps, watch=[{"name": "existing"}])
        self.assertEqual([o["name"] for o in kept], ["C"])
        self.assertEqual(len(watch), 3)   # 1 pre-existing + 2 demoted

    async def test_an_empty_list_is_handled(self):
        self.assertEqual(await self._run([]), ([], []))
