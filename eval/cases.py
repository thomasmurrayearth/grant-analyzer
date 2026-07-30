"""
Ground-truth test cases for the Grant Analyser.

Kept in their own module — with no imports from `analyzer` or any other part
of the app — so that three different consumers can share one definition of
"right answer":

* `eval/run_eval.py`      — the manual harness that runs the full pipeline.
* `benchmark.py`          — the app's fallback self-benchmark (see §"Measurement
                            supply" in PROJECT_LOG).
* `quality.py`            — scoring, which needs the rules but never the pipeline.

Importing this module must stay cheap and side-effect free: the app imports it
at start-up, so it must not need an API key, a network connection, or a browser.
"""

# ---------------------------------------------------------------------------
# Ground-truth test cases
# ---------------------------------------------------------------------------
# Each case defines:
#   must_appear_in_main              grants that MUST be main recommendations
#   must_appear_in_watchlist_or_main grants that should appear SOMEWHERE
#   must_not_appear_anywhere         grants that must be excluded entirely
#
# Name matching is case-insensitive substring:
#   "Energy Catalyst" matches "Innovate UK Energy Catalyst Round 12"
#   "Ofgem" matches "Ofgem Strategic Innovation Fund 2025"
#
# Keep must_appear_in_main sparse — only add entries you are certain about.
# Wrong ground truth is worse than no ground truth.
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id":           "thermify",
        "company_url":  "https://thermify.cloud/",
        "company_name": "Thermify",
        "notes": (
            "Wales-registered: 39a Vale Business Park, Llandow, Cowbridge, Wales CF71 7PF. "
            "Distributed edge-compute-as-heating hardware (HeatHub: 500 Raspberry Pi CMs in oil "
            "bath replacing gas boilers). TRL 7-8 (live installs, GBP 2.5m contracted revenue, "
            "aiming for 40k units/year). "
            "Confirmed Ofgem SIF recipient — via SHIELD project led by UK Power Networks "
            "(DNO lead applicant, Thermify as technology partner). "
            "Facility at Sony Pencoed site, South Wales. "
            "Active partnership: Swansea University SPECIFIC IKC. "
            "Coverage: The Register, Data Centre Dynamics, Solar Power Portal. "
            "SIF NOTE: Thermify cannot be SIF lead applicant (requires energy licence holder). "
            "KTP NOTE: Thermify + Swansea University partnership already in place — KTP formalisation viable."
        ),
        "preferences": {
            "geographies":  "",
            "consortium":   True,
            "accelerators": True,
            "prizes":       True,
        },

        # Grants that MUST appear as main recommendations (direct applicant, high confidence)
        "must_appear_in_main": [
            "Innovate UK",     # Some current Innovate UK competition — direct SME applicant
            "SMART",           # Welsh Government SMART FIS — Wales-registered, direct applicant
        ],

        # Grants that should appear SOMEWHERE (main OR watchlist — either is acceptable)
        "must_appear_in_watchlist_or_main": [
            "Ofgem",           # Ofgem SIF — confirmed prior recipient (as partner); must surface somewhere
            "EIC Accelerator", # UK companies eligible for up to EUR 2.5m grant; multiple 2026 deadlines
            "KTP",             # Knowledge Transfer Partnership — Swansea University partner already in place
        ],

        # Grants that must NOT appear anywhere (confirmed ineligible or wrong applicant geography)
        "must_not_appear_anywhere": [
            "Energy Catalyst",  # ODA-only — funds projects in developing countries only; UK company ineligible
            "EIC Pathfinder",   # TRL 1-4 only; Thermify is TRL 7-8 — explicit confirmed mismatch
        ],
    },

    {
        # Non-UK profile shape: stress-tests geography gating. The UK/Wales
        # mandatory discovery queries must NOT fire, UK-registration-required
        # programmes must not be recommended as direct applications, and the
        # EU funding landscape (EIC etc.) must surface instead.
        #
        # Fictional company fed via extra_text (no URL) so the test doesn't
        # depend on a live website; Phase 1's external searches will simply
        # find nothing and the profile is built from the text below.
        "id":           "germandeeptech",
        "company_url":  None,
        "company_name": "Kaltefluss GmbH (fictional)",
        "extra_text": (
            "Kaltefluss GmbH is a deep-tech hardware startup registered in Munich, "
            "Germany. It builds high-temperature industrial heat pumps (up to 200C "
            "output) that replace gas-fired process heat in food processing and "
            "chemical plants across Germany and Austria. Technology: proprietary "
            "turbo-compressor with natural refrigerants. TRL 5-6: two pilot "
            "installations running at customer sites near Augsburg, no serial "
            "production yet. Stage: seed, 14 employees, ~EUR 2.1m raised from "
            "German angel investors. No UK presence, no UK customers, no UK "
            "subsidiary. Primary outcome: industrial decarbonisation - each unit "
            "displaces roughly 1,200 tonnes CO2 per year."
        ),
        "notes": "Fictional German industrial heat-pump company, TRL 5-6, seed stage, no UK presence.",
        "preferences": {
            "geographies":  "",
            "consortium":   True,
            "accelerators": True,
            "prizes":       True,
        },

        # No must_appear_in_main entries: ground truth for a fictional company
        # is only reliable for structural checks, not specific competitions.
        "must_appear_in_main": [],

        "must_appear_in_watchlist_or_main": [
            "EIC",             # EIC Accelerator/programmes — core fit for a German deep-tech SME
        ],

        # Programmes requiring UK registration must not be DIRECT recommendations
        # for a company with no UK presence (watchlist partner-route is acceptable)
        "must_not_appear_in_main": [
            "Innovate UK",
        ],

        # Devolved-nation programmes are impossible for a German company
        "must_not_appear_anywhere": [
            "Welsh",           # Welsh Government SMART FIS etc. — Wales presence required
            "Energy Catalyst", # ODA-only geography — wrong for a German/Austrian deployer too
        ],
    },

    # -------------------------------------------------------------------------
    # Add more test cases here as you validate other companies.
    # Suggested next additions:
    #   - A pre-seed UK deeptech company (tests early-TRL grant discovery)
    # -------------------------------------------------------------------------
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

CASES_BY_ID: dict[str, dict] = {c["id"]: c for c in TEST_CASES}


def get_case(case_id: str) -> dict | None:
    """Return the ground-truth case with this id, or None."""
    return CASES_BY_ID.get(case_id)


def case_for_url(url: str) -> dict | None:
    """Return the case whose company_url matches this URL, ignoring scheme,
    a leading www., and trailing slashes. Used to recognise a stored analysis
    as a benchmark run of a known company."""
    if not url:
        return None

    def _key(u: str) -> str:
        u = (u or "").strip().lower()
        for prefix in ("https://", "http://"):
            if u.startswith(prefix):
                u = u[len(prefix):]
        if u.startswith("www."):
            u = u[4:]
        return u.rstrip("/")

    target = _key(url)
    if not target:
        return None
    for case in TEST_CASES:
        if case.get("company_url") and _key(case["company_url"]) == target:
            return case
    return None


# Cases the app is allowed to run itself when a fortnight passed with no real
# user runs (see `benchmark.py`). Deliberately the full set — keep it small,
# because every entry costs real API money each time the fallback fires.
BENCHMARK_CASE_IDS: list[str] = [c["id"] for c in TEST_CASES]
