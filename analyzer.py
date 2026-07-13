"""
Three-phase grant analysis pipeline.

Phase 1 — Company Analysis
  1a. Fetch startup website content.
  1b. Claude runs up to 5 web searches for external company info
      (news, funding history, pilots, LinkedIn, Crunchbase, etc.)
  1c. Claude synthesises website + research into a structured company profile.

Phase 2 — Grant Discovery (wide funnel, thematic fit first)
  2a. Claude generates theme-driven search queries; mandatory geography/sector
      queries are prepended in code.
  2b. Python executes all queries itself (no tool-use loop — no runaway risk).
  2c. A single streaming call compiles a wide longlist (target 30-45) scored
      ONLY on thematic fit. Eligibility concerns are recorded as flags, not
      used to exclude — Phase 3 and the code-level gates do the filtering.

Phase 3 — Deep Research & Scoring
  3a. Take the top 20 candidates by initial thematic fit from the longlist.
  3b. Ask Claude to generate 2 targeted research queries per grant:
        - one for official details (eligibility, funding, deadline)
        - one for independent commentary / applicant experiences
  3c. Execute all queries (~40 searches, fully controlled — no runaway risk).
  3d. A final scoring call (no tools) gives Claude all research evidence
      and asks it to apply the full rubric. This separates research from
      scoring cleanly and guarantees a complete JSON output.
"""

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, AsyncGenerator, Callable

import anthropic
import httpx

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Model + token/cost accounting
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"

# USD per million tokens for MODEL. Cache writes bill at 1.25x input, cache
# reads at 0.1x input. Update these together with MODEL.
PRICE_PER_MTOK = {
    "input":       3.00,
    "output":     15.00,
    "cache_write":  3.75,
    "cache_read":   0.30,
}

# Per-analysis usage accumulator. A ContextVar so two analyses running
# concurrently each accumulate into their own dict: asyncio.create_task copies
# the current context, so whatever the task sets stays inside that task.
_usage_ctx: ContextVar[dict | None] = ContextVar("analyzer_usage", default=None)


def new_usage() -> dict:
    return {
        "api_calls":         0,
        "input_tokens":      0,
        "output_tokens":     0,
        "cache_write_tokens": 0,
        "cache_read_tokens": 0,
    }


def start_usage_tracking(existing: dict | None = None) -> dict:
    """Begin accumulating token usage for the current task. Returns the
    accumulator; the caller holds the same dict the pipeline writes into.
    Pass the accumulator from an earlier phase of the same analysis to keep
    counting into it, so cost-per-run covers the whole pipeline."""
    acc = existing if existing is not None else new_usage()
    _usage_ctx.set(acc)
    return acc


def _record_usage(usage: Any) -> None:
    """Add one API response's token usage to the current accumulator."""
    acc = _usage_ctx.get()
    if acc is None or usage is None:
        return
    acc["api_calls"]          += 1
    acc["input_tokens"]       += getattr(usage, "input_tokens", 0) or 0
    acc["output_tokens"]      += getattr(usage, "output_tokens", 0) or 0
    acc["cache_write_tokens"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
    acc["cache_read_tokens"]  += getattr(usage, "cache_read_input_tokens", 0) or 0


def usage_cost_usd(usage: dict | None) -> float:
    """Dollar cost of one analysis from its accumulated token usage."""
    if not usage:
        return 0.0
    return round(sum(
        usage.get(f"{k}_tokens", 0) / 1_000_000 * price
        for k, price in PRICE_PER_MTOK.items()
    ), 4)


# ---------------------------------------------------------------------------
# Retry utility
# ---------------------------------------------------------------------------

async def _with_retry(
    factory: Callable[[], Any],
    max_attempts: int = 3,
    on_retry: Callable[[int, Exception, int], None] | None = None,
) -> Any:
    """
    Call factory() to get a fresh coroutine each attempt.

    factory must be a zero-argument callable that creates a new coroutine
    every time it is called — e.g. ``lambda: some_async_fn(arg1, arg2)``.
    Using a lambda instead of passing the coroutine directly is important:
    a coroutine object can only be awaited once.

    On failure, waits ``delay`` seconds and retries up to max_attempts times.
    on_retry(attempt_number, exc, wait_seconds) is called before each retry
    so callers can surface progress messages.
    """
    delays = [10, 20]   # seconds before attempt 2, then attempt 3
    # 4xx client errors (bad request, auth, permission, not found) will fail
    # identically on every attempt — retrying just wastes 30s. Fail fast.
    _non_retryable = (
        anthropic.BadRequestError,
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
    )
    for attempt in range(max_attempts):
        try:
            return await factory()
        except _non_retryable:
            raise
        except Exception as exc:
            # Some call sites wrap API errors in RuntimeError — unwrap the cause
            if isinstance(exc.__cause__, _non_retryable):
                raise
            if attempt < max_attempts - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                if on_retry:
                    on_retry(attempt + 1, exc, delay)
                await asyncio.sleep(delay)
            else:
                raise

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------
MAX_COMPANY_SEARCHES  = 5   # external background searches on the company
MAX_DISCOVERY_SEARCHES = 30  # grant landscape searches (wide-funnel discovery)
DISCOVERY_RESULTS_PER_SEARCH = 6   # DuckDuckGo results kept per discovery query
DISCOVERY_SNIPPET_CHARS = 2500     # per-search evidence budget in the longlist
                                   # call. At ~500 chars per formatted result,
                                   # 2500 keeps ~5 of 6 results; the old value
                                   # of 500 silently discarded all but one.
LONGLIST_MAX_TOKENS   = 12000  # output budget for the longlist call (streams,
                               # so no timeout risk). 6000 capped the longlist
                               # at ~25 items; 12000 supports the 30-45 target.
SHORTLIST_SIZE        = 10   # grants to deep-research in Phase 3
                             # Historically capped at 8 because the scoring call
                             # was limited to 8192 output tokens and silently
                             # truncated beyond ~8 items. The scoring call now
                             # streams with SCORING_MAX_TOKENS headroom (20K is
                             # ~2x the ~10K a 10-item response uses); each extra
                             # item costs 3 Phase-3 searches — raise
                             # deliberately, not casually.
SCORING_MAX_TOKENS    = 20000  # output budget for the final scoring call.
                               # claude-sonnet-4-6 supports up to 128K output
                               # tokens with streaming; 20K eliminates silent
                               # truncation as a failure mode.
QUERIES_PER_GRANT     = 3    # research queries per shortlisted grant

# Watchlist admission policy: a slot is earned ONLY by
#   (a) "partner_route"   — a programme requiring a specific class of eligible
#       lead applicant (licensed operator, NHS trust, social landlord, local
#       authority, ...) that the startup could join as technology partner, or
#   (b) "between_rounds"  — a well-established recurring programme with strong
#       thematic fit that is currently between public application rounds.
# Strong-fit items whose application route cannot be confirmed are DROPPED,
# not watchlisted — an unverifiable entry is not actionable.
STRONG_FIT_MIN        = 4    # minimum thematic fit for watchlist admission
WATCHLIST_CAP         = 10   # maximum watchlist entries shown to the user
_MAX_TRIAGE_SEARCHES  = 12   # bound on extra searches for watchlist triage

# ---------------------------------------------------------------------------
# Mandatory discovery query helpers
# ---------------------------------------------------------------------------

def _year_qualifier() -> str:
    """Current + next year, e.g. "2026 2027" — keeps search queries fresh
    without hardcoded years going stale."""
    from datetime import date
    y = date.today().year
    return f"{y} {y + 1}"


def _mandatory_queries(profile: dict) -> list[str]:
    """
    Return a small set of mandatory discovery search queries for high-value
    recurring programmes that must always be searched, regardless of what the
    AI-generated queries cover.

    Based on the company's geography and sector — not hardcoded grant names.
    These queries are prepended to the AI-generated list so they are always
    executed first, ensuring key programmes are never missed simply because
    the discovery prompt didn't think to search for them.
    """
    queries: list[str] = []

    hq     = (profile.get("hq") or "").lower()
    geos   = " ".join(g.lower() for g in (profile.get("operational_geographies") or []))
    themes = " ".join(t.lower() for t in (profile.get("key_themes") or []))
    cls    = (profile.get("classification") or "").lower()
    # Free-text fields are scanned too — the model sometimes omits Wales from
    # hq/geos but mentions it in website_summary or external_findings. But
    # free text can also NEGATE a geography ("No evidence of UK presence was
    # found"), so drop any sentence containing a negation marker before
    # scanning, and use word-boundary matching (bare "uk" also appears inside
    # German words like "Produkte").
    freetext = (
        (profile.get("website_summary") or "") + " " +
        (profile.get("external_findings") or "") + " " +
        (profile.get("value_proposition") or "")
    ).lower()
    freetext_affirmative = " ".join(
        s for s in re.split(r"[.;\n]", freetext)
        if not re.search(r"\bno\b|\bnot\b|\bnone\b|\bwithout\b", s)
    )

    _UK_PAT = re.compile(
        r"\buk\b|united kingdom|\bengland\b|\bwales\b|\bscotland\b|"
        r"northern ireland|\blondon\b|\bmanchester\b|\bbirmingham\b|"
        r"\bcardiff\b|\bedinburgh\b"
    )
    _WALES_PAT = re.compile(
        r"\bwales\b|\bcymru\b|\bcardiff\b|\bswansea\b|\bnewport\b|"
        r"\bwrexham\b|\bllandow\b|\bcowbridge\b|\bwelsh\b"
    )

    # Structured fields are authoritative; affirmative free text broadens
    # coverage (e.g. hq says only "United Kingdom" but the summary names
    # Llandow, Wales) or fills in when structured fields are empty.
    structured = f"{hq} {geos}".replace("unknown", " ").strip()
    is_uk = bool(_UK_PAT.search(structured)) or bool(_UK_PAT.search(freetext_affirmative))
    is_wales = bool(_WALES_PAT.search(structured)) or (
        is_uk and bool(_WALES_PAT.search(freetext_affirmative))
    )
    all_text = themes + " " + cls
    is_energy = any(k in all_text for k in (
        "energy", "heat", "power", "grid", "net zero", "clean energy",
        "climate", "renewabl", "decarboni", "emissions",
    ))
    is_deeptech = any(k in all_text for k in (
        "deeptech", "deep tech", "hardware", "climate tech", "cleantech",
        "advanced manufacturing", "compute", "semiconductor",
    ))

    years = _year_qualifier()
    if is_uk:
        queries.append(
            f"Innovate UK open grant competitions SME innovation apply {years}"
        )
        queries.append(
            f"Innovate UK Knowledge Transfer Partnership KTP open call UK SME university {years}"
        )
    if is_wales:
        queries.append(
            f"Welsh Government SMART FIS innovation grant SME apply Wales {years}"
        )
        queries.append(
            f"Business Wales Smart innovation support grant apply {years}"
        )
    if is_energy and is_uk:
        # Ofgem is the GB energy regulator — its programmes are only
        # relevant to companies operating in the UK
        queries.append(
            f"Ofgem Strategic Innovation Fund SIF open call apply {years}"
        )
    if is_deeptech or is_energy:
        queries.append(
            f"EIC Accelerator open call apply EU climate deep tech SME {years}"
        )

    return queries



# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_COMPANY_SYSTEM = """You are an expert grants strategist building a detailed company profile.

You have been given information about a startup from one or more sources (website, web search results, or text provided directly). Before writing the profile, use the web_search tool to find additional external information — aim for {max_searches} searches covering:
1. Recent news, press coverage, or blog posts about the company
2. Funding rounds, investor announcements, or crowdfunding campaigns
3. Technical details about the product not already captured
4. Any known pilots, partnerships, or customer case studies
5. The company on LinkedIn, Crunchbase, or similar platforms

After completing your searches, return ONLY a valid JSON object — no markdown:
{{
  "name": "company name",
  "classification": "primary sector e.g. Climate Tech / Agtech / Deeptech / Biotech / AI / Fintech / Healthcare / Manufacturing / SaaS",
  "value_proposition": "one sentence — what problem they solve and for whom",
  "technology": "core technology or operational differentiation",
  "primary_outcome": "primary measurable impact e.g. emissions reduction / yield increase / cost saving",
  "secondary_benefits": ["benefit1", "benefit2"],
  "hq": "city, country",
  "operational_geographies": ["region or country"],
  "customer_geographies": ["region or country"],
  "trl": "estimated TRL 1-9 with brief rationale",
  "stage": "Pre-seed / Seed / Series A / Growth / unknown",
  "funding_characteristics": ["SME", "deeptech", "AI-enabled", "university spinout", "etc"],
  "key_themes": ["theme1", "theme2"],
  "website_summary": "2-3 sentence plain-language summary of what the company does",
  "external_findings": "2-3 sentences summarising the most important things found in external research"
}}

Be precise. Mark anything genuinely unknown as "unknown"."""


_DISCOVERY_QUERY_SYSTEM = """You are a grants research analyst. Generate exactly {n} targeted web search queries to find grant and funding opportunities for the startup described.

IMPORTANT: Do NOT generate queries for the following programmes — they are already being searched separately and you should use your query budget for OTHER opportunities:
{exclude_programmes}

## QUERY STRATEGY — derive queries from the company's THEMES, not from a fixed category checklist

The goal is a wide, thematically targeted net. Build most queries by combining
the company's key themes, primary outcome, and technology with the geographies
where it operates or sells:

  1. THEME x GEOGRAPHY queries (roughly half the budget): for each key theme and
     the primary outcome, search for grants in the company's HQ country and each
     operational/customer geography. Vary the vocabulary — use the funder's
     language (e.g. "decarbonisation", "net zero", "industrial efficiency"),
     not just the company's own words.
  2. National innovation / R&D agency programmes in the HQ country.
  3. EU / regional programmes (Horizon Europe, EIC, EIT) — only if the company
     operates in or sells to eligible geographies.
  4. Sector-specific government programmes and regulated-sector innovation
     funds relevant to the company's themes.
  5. Philanthropic and multilateral innovation funds for the company's primary
     outcome.
  6. Challenge prizes / open innovation competitions — ONLY if the user is open
     to prizes; otherwise spend these queries on categories 1-5.
  7. Accelerators / incubators with grant or equity-free funding — ONLY if the
     user is open to accelerators; otherwise spend these queries on categories 1-5.

Each query must be specific — include sector or theme, geography, the year
qualifier "{years}", and terms like "open" or "apply" or "grant" where useful.
No two queries should be near-duplicates of each other.

Return ONLY a JSON array of query strings — no markdown:
["query 1", "query 2", ...]"""


_DISCOVERY_ANALYSIS_SYSTEM = """You are a grants research analyst. You have been given web search results. Analyse them and compile a WIDE longlist of grant and funding opportunities for the startup described.

## YOUR ONE ORGANISING CRITERION: THEMATIC FIT

Your job at this stage is a single judgement per programme: is this startup a
good THEMATIC fit for what the fund exists to support? You are NOT filtering
for eligibility, application route, or current status — later pipeline stages
research and verify all of that with far better evidence than you have here.
A relevant programme excluded now is unrecoverable; an included marginal one
gets filtered later. Cast the net wide.

### THEMATIC FIT (1-5) — score every candidate
Compare the funder's PRIMARY objective, intended beneficiary, and intended
applicant type against the company's CORE product and PRIMARY outcome:
5 = Exact match — the company is a natural poster-child for what this fund exists to support
4 = Strong alignment — core product clearly serves the funder's primary objective with only minor reframing
3 = Partial alignment — real overlap, but requires reframing or leans on secondary co-benefits
2 = Weak — only a secondary co-benefit or broad sector keyword connects them
1 = Poor / forced relevance

Do NOT conflate co-benefits with primary alignment. Shared vocabulary (both
mention "energy" or "climate") is not alignment — check whether the funder's
intended beneficiary and applicant type actually match this company.

## MINIMAL JUNK FILTER — the ONLY grounds for exclusion

EXCLUDE an item only when the search result shows:
- No specific, NAMED programme at all (a bare funder homepage, portfolio page, or general funding pledge like "Organisation X has £50m for clean tech")
- A strategy document, policy announcement, or roadmap with no programme attached
- A news article ONLY about funding already awarded to other recipients, with no named programme new entrants could apply to
- A fund that only makes equity investments, with no grant or prize component

Everything else that is a named programme with thematic fit >= 2 goes on the
longlist. In particular, DO INCLUDE (with the appropriate status and flags):
- Programmes currently between rounds or recently closed — status "Recurring"
- Well-established institutional funds (national innovation agencies, EU programmes, devolved government grants, regulated-sector innovation funds, NHS/AHSN programmes) even if no active round is visible — status "Recurring"
- Programmes that require a licensed or regulated entity as lead applicant (energy network innovation funds, NHS innovation funds, social housing decarbonisation schemes, local authority programmes) where the startup could participate as a named technology partner — set "likely_partner_route": true
- Programmes whose geographic eligibility might not match the company — set "possible_geography_mismatch": true and explain in notes; do NOT exclude

## ELIGIBILITY FLAGS — record concerns, do not act on them

"possible_geography_mismatch": true when the fund's required deployment or
impact geography (e.g. ODA-eligible developing countries, a specific country or
devolved nation) may not match where this company operates. Later stages verify.

"likely_partner_route": true when the fund appears to require a specific class
of lead applicant (licensed network operator, NHS trust, registered social
landlord, local authority, university, other public body) so the startup could
likely only participate as a project partner.

## PROGRAMME FAMILIES vs. SPECIFIC COMPETITIONS

Many large funders (e.g. Innovate UK, Horizon Europe) run multiple specific competitions under a broad programme umbrella. Do NOT name the programme umbrella as a single entry — name only the specific competition.

- WRONG: "Innovate UK Smart Grants" (a programme family with many sub-competitions)
- RIGHT: "Innovate UK Smart Grants — [specific current or recent round name]"
- RIGHT: "Horizon Europe EIC Accelerator Open {years}"

If you can only identify the programme family but not a specific competition within it, include it with a clear note in the `notes` field that this is a programme umbrella and the specific competition must be confirmed, and set `status` to "Recurring".

List each distinct programme ONCE — if several search results describe the same programme, merge them into one entry using the most specific name found.

## OUTPUT
Return ONLY a valid JSON array — no markdown:
[
  {{
    "name": "specific competition or programme name — do NOT use a generic umbrella name if a specific competition is known",
    "managing_body": "organisation",
    "geography": "geography where the company would need to deploy or operate to qualify",
    "funder_target_geography": "geography where the funded technology must have impact (e.g. 'UK domestic', 'ODA-eligible developing countries', 'EU member states')",
    "status": "Open | Recurring | Closed but likely reopening",
    "application_link": "URL or unknown",
    "funding_type": "Grant | Prize | Blended | Concessional | Accelerator",
    "funding_estimate": "amount / range / unknown",
    "initial_thematic_fit": 4,
    "initial_thematic_fit_reason": "one sentence grounded in the funder's primary objective vs the company's core product",
    "possible_geography_mismatch": false,
    "likely_partner_route": false,
    "notes": "key eligibility caveats, programme family note if applicable, or specific competition name if different from entry name"
  }}
]

Aim for 30-45 candidates. Sort by initial_thematic_fit descending. Be inclusive — the deep-research and scoring stages apply the full rubric with real evidence."""


_QUERY_GENERATION_SYSTEM = """You are a grants research analyst. For each grant in the list provided, generate exactly 3 targeted web search queries:

1. "query_official" — to find the grant's official details: eligibility criteria, funding amount, application deadline, and process requirements.
2. "query_commentary" — to find independent commentary: blog posts, forum discussions, news articles, or testimonials from previous applicants about the experience, burden, success rates, or pitfalls.
3. "query_apply" — to find the specific URL where a startup can apply. Search for the application portal, online form, call-for-proposals page, competition entry page, or submission system. Use terms like "apply", "application form", "submit", "portal", "open call", "call for proposals". The goal is to find the direct link a startup would use to actually begin an application.

Return ONLY a valid JSON array — no markdown:
[
  {{
    "name": "exact grant name from the input list",
    "query_official": "targeted search query for official details",
    "query_commentary": "targeted search query for applicant experiences and independent reviews",
    "query_apply": "targeted search query to find the specific application portal, form, or call page URL"
  }}
]"""


_SCORING_SYSTEM = """You are a world-class grants strategist. Work through each shortlisted grant opportunity in three steps: classify it, route it, then score it.

CRITICAL OUTPUT CONSTRAINT: Your written analysis for Steps 1–2 combined must stay under 150 words per grant. The final JSON block is the primary deliverable — never sacrifice JSON completeness for written analysis. If you run out of reasoning budget, write shorter summaries but always output complete JSON for every grant.

## STEP 1 — CLASSIFY EACH OPPORTUNITY

Assign ALL of the following fields to every item, grounded strictly in the research evidence:

**opportunity_type** — choose exactly one:
direct_grant | challenge_prize | accelerator_with_cash | accelerator_in_kind_only | incubator_grant | blended_finance | equity_or_investment | procurement | partner_led_grant | relationship_led_funder | monitor_only | generic_portal | policy_programme | past_award_or_news | closed_one_off | exclude

Use closed_one_off for: programmes that the evidence confirms have been discontinued, permanently closed, or were time-limited one-off initiatives that will not recur (e.g. COVID-era emergency funds, programmes explicitly described as ended or cancelled). These will be excluded entirely from the output.

**application_route** — choose exactly one:
direct_startup_application | startup_as_partner | consortium_required | incubator_application | challenge_or_prize_application | nomination_required | invitation_or_relationship_led | government_or_accredited_entity_required | prior_award_required | unclear_application_route | no_application_route_found

**has_application_process** — true | false
Set TRUE only if the research evidence confirms that a real public application process exists or has previously existed: an application form, portal, competition entry, or call for proposals that a startup can access. A funder's general website, an overview of their funding priorities, a news article about past awards, or a description of their investment thesis does NOT qualify. If the evidence only shows a homepage or general description — set FALSE.

**application_timing** — choose exactly one:
open_now            = there is a current open call or rolling applications accepting submissions right now
known_future_window = not open now, but a specific upcoming window is announced or follows a confirmed regular schedule (e.g. opens every January)
recurring_uncertain = has had open calls in the past and is likely to recur, but no confirmed next date is known from the evidence
timing_unknown      = no evidence that a public application window has ever existed or will exist

**link_type** — based on query_apply results and other evidence, classify the best URL available:
application_portal  = direct link to an application form, submission portal, or competition entry page
programme_page      = official programme or call overview page (right funder page, but not the form itself)
funder_homepage     = only a general funder website was found (not specific to this programme or call)
unknown             = no reliable URL found at all

**trl_match** — true | false | unknown
Compare the company's TRL (given in the company profile) against this grant's stated TRL requirements from the research evidence.
Set FALSE only when there is an explicit, clear mismatch: e.g. the grant states "TRL 1–4 only" and the company is at TRL 6+, or the grant is for late-stage commercialisation only and the company is at TRL 2. Set TRUE if the TRL range is unstated, broad, or genuinely compatible. Set UNKNOWN if TRL requirements are not mentioned and cannot be inferred.

**geography_match** — true | false | unknown
Does the funder's required deployment geography match where this company actually operates and creates impact?
Set FALSE when the grant explicitly requires technology deployment or beneficiary impact in a geography this company does not serve — e.g. the grant funds projects in ODA-eligible developing countries (sub-Saharan Africa, South Asia, Indo-Pacific, Latin America) but the company deploys only in the UK or EU. Set TRUE when geographies are compatible or the grant has no specific deployment geography requirement. Set UNKNOWN when geographic requirements are unclear.

**applicant_type_match** — choose exactly one:
direct      = the startup can apply as lead applicant and receive funding directly (the normal case for most innovation grants)
partner     = the startup can participate in a funded project but CANNOT be the lead applicant or direct funding recipient — the grant requires the applicant of record to be a specific organisation type this startup is not (e.g. an Ofgem-licensed distribution network operator, an NHS trust, a registered social landlord, a housing association, a local authority, a public sector body). The startup can only be a named project partner under such an entity.
ineligible  = the startup is COMPLETELY AND CATEGORICALLY barred from any involvement whatsoever — not even as a sub-contractor, technology supplier, or named project partner. Use this ONLY for hard eligibility barriers such as: wrong country (grant is for French companies only and startup is UK-based), wrong company type with no partner route (grant is solely for public bodies and no technology partner role exists), or explicit sector exclusion. DO NOT use "ineligible" simply because the startup cannot be the lead applicant — that is "partner".

CRITICAL RULE: If there is ANY conceivable way the startup could participate in the funded project — even as a named technology partner in someone else's application — use "partner", NOT "ineligible". "Ineligible" should be rare. When in doubt between partner and ineligible, always choose "partner".

KEY DISTINCTION: The test for "partner" vs "direct" is whether the startup can receive the grant money directly as lead applicant. If the funder's eligibility criteria require the applicant to hold a specific licence, accreditation, or legal status that this startup does not have — set "partner" even if the programme description mentions "working with innovative companies" or "technology partners". A company that can only participate as a sub-contractor or named partner in someone else's application is "partner", not "direct".

---

## STEP 2 — ROUTE EACH OPPORTUNITY

### MANDATORY PRE-ROUTING OVERRIDE — apply BEFORE any other Step 2 rule:
Before routing any opportunity, ask: does this programme require a specific licensed or regulated entity as lead applicant (e.g. a licensed network operator, NHS trust, registered social landlord, housing association, local authority, or other public body) — AND could the startup still participate as a named technology partner, sub-contractor, or project member?

If YES → set applicant_type_match="partner" and route to STRATEGIC WATCHLIST. Do NOT use "ineligible". Do NOT exclude entirely.

This applies to the entire class of regulated-entity-led innovation programmes, including but not limited to:
- Energy network innovation funds (e.g. Ofgem SIF, NIC, NIA): require a licensed DNO or GDN as lead applicant; technology companies routinely participate as named project partners.
- NHS and health innovation funds: require an NHS trust or ICS as lead; technology companies are common project partners.
- Social housing decarbonisation schemes: require a registered social landlord or local authority as lead; technology suppliers participate as delivery partners.
- Local authority innovation programmes: require a council as lead applicant; technology companies can be named sub-contractors.

The test is simple: can the startup be involved in the funded project in any capacity, even without leading it? If yes — use "partner", not "ineligible".

### EXCLUDE ENTIRELY (omit from both output arrays) if ANY of these apply:
- trl_match is false — confirmed TRL mismatch: the grant explicitly requires a TRL the company does not meet (e.g. TRL 1–4 only for a company with live commercial deployments, or commercialisation-stage only for a pre-prototype company). This is a hard factual exclusion.
- geography_match is false — the grant explicitly requires technology deployment or beneficiary impact in a geography the company does not operate in (e.g. ODA-eligible developing countries only, or a specific country/region the company has no presence in). This is a hard factual exclusion.
- opportunity_type is any of: generic_portal | policy_programme | past_award_or_news | closed_one_off | exclude | equity_or_investment | procurement

NOTE: "has_application_process is false" and "applicant_type_match is ineligible" are NO LONGER grounds for exclusion. These are uncertain judgements — route those items to the STRATEGIC WATCHLIST instead so the user can review them.

### Route to STRATEGIC WATCHLIST (do not apply scoring rubric) if ANY of these apply:
- has_application_process is false or unclear — set why_watchlist to: "No confirmed application process found in research. Verify directly with the funder whether a public application route exists."
- applicant_type_match is "ineligible" — set why_watchlist to: "Scored as potentially ineligible for this startup. Review eligibility criteria directly — this classification may be incorrect and the startup may have a participation route not captured in the research."
- applicant_type_match is "partner" — set why_watchlist to: "Startup cannot be lead applicant or direct funding recipient for this programme. Participation as a named project partner under an eligible lead organisation may be possible — worth pursuing if a suitable lead partner can be identified."
- application_route is any of: invitation_or_relationship_led | nomination_required | government_or_accredited_entity_required | prior_award_required | no_application_route_found | unclear_application_route
- opportunity_type is any of: relationship_led_funder | monitor_only | accelerator_in_kind_only
- User preferences: if not open to consortium → also route consortium_required here; if not open to accelerators → route accelerator_with_cash and incubator_grant here; if not open to prizes → route challenge_prize here

### Everything else → MAIN RECOMMENDATIONS (proceed to Step 3)

---

## STEP 3 — SCORE MAIN RECOMMENDATIONS ONLY

### Thematic Fit (1-5) — CONSERVATIVE

5 = startup's core product directly serves the funder's primary objective, target beneficiary, deployment geography, AND deployment context — a natural poster-child requiring no reframing
4 = strong fit with only minor adaptation required
3 = partial fit: requires reframing the pitch or relies significantly on secondary co-benefits rather than the core product
2 = weak fit: broad sector overlap but poor specific match
1 = no meaningful alignment

ADVERSARIAL CHECK — before awarding 4 or 5, answer all eight questions. If any reveals a material weakness, downgrade by at least 1 point:
1. Is the match based mainly on broad sector keywords rather than the startup's core product and primary outcome? Shared vocabulary (e.g. both involve "heat" or "energy") is not alignment — check whether the funder's intended beneficiary, deployment model, and technology type actually match this startup's.
2. Does the funder's REQUIRED deployment geography match where this company actually deploys and creates impact — not merely where it is incorporated? A grant requiring impact in ODA-eligible developing countries is NOT a match for a company deploying only in the UK or EU. A grant for a specific devolved nation or region is not a match for a company with no presence there.
3. Is the outcome the funder primarily cares about the startup's primary outcome, or only a secondary co-benefit?
4. Is the application route realistic for this startup right now?
5. Would the startup need to substantially pivot or reframe its story to fit this grant?
6. Would a sceptical grant assessor immediately recognise this as a strong fit?
7. Does the funding programme's intended applicant type and business model match this startup's? A grant designed for centralised infrastructure operators (e.g. heat network operators, grid companies, utilities) does NOT fit a startup selling distributed consumer hardware, even if both operate in the same sector. A grant for academic research institutions does not fit a commercial SME. Check the intended applicant, not just the technology theme.
8. Is this a specific named competition that the startup can apply to now or at a known future date — or is it a programme umbrella with no confirmed open window? If it is a programme family with no specific current competition identified, do not award more than 3 for thematic fit regardless of sector alignment.

KNOWN HARD ELIGIBILITY GATES — apply these regardless of whether the research evidence explicitly states them:
- EIC Pathfinder (and similar "breakthrough science" / "visionary research" calls): Designed for TRL 1–4. If the company has live commercial deployments, paying customers, or is at TRL 5 or above, set trl_match=false and EXCLUDE ENTIRELY per Step 2 routing rules. This is non-negotiable.
- Innovate UK Energy Catalyst: Exclusively funds technology deployment in ODA-eligible developing countries (sub-Saharan Africa, South Asia, Indo-Pacific, Latin America). If the company deploys technology in UK, EU, or USA, set geography_match=false and EXCLUDE ENTIRELY.
- Regulated-entity-led innovation programmes (Ofgem SIF, NHS funds, social housing schemes, local authority programmes, etc.): If a programme requires a licensed network operator, NHS trust, registered social landlord, or local authority as lead applicant, technology companies typically CAN participate as named project partners. Set applicant_type_match="partner" (never "ineligible") and route to STRATEGIC WATCHLIST per the Step 2 mandatory override. Confirm here that Step 2's routing was applied correctly.

### Strategic Value (1-5)
5 = transformational — unlocks a major new market, geography, or institutional credibility
4 = high — significant investor signal, ecosystem positioning, or follow-on leverage
3 = moderate — useful credibility or network access
2 = limited
1 = minimal

### Ease of Application (1-5)
Use research evidence — especially applicant testimony and independent commentary:
5 = very easy — simple online form, solo application, low administrative burden
4 = easy — straightforward short proposal
3 = moderate — full technical proposal, standard reporting
2 = difficult — consortium or co-funding required, or known high burden
1 = very difficult — complex multi-partner, political, or high-failure-rate process

### PRIORITY SCORE
priority_score = (0.45 × thematic_fit) + (0.35 × strategic_value) + (0.20 × ease)

### PRIORITY TIER RULES — apply timing rules strictly:
Must Pursue:               priority_score >= 3.5 AND thematic_fit >= 4 AND route is direct/clear AND application_timing is open_now or known_future_window
Big Bet:                   thematic_fit >= 4 AND ease <= 2, OR strategic_value = 5 with difficult application; application_timing must NOT be timing_unknown
Quick Win:                 ease >= 4 AND priority_score >= 3.0 AND thematic_fit >= 3 AND application_timing is open_now
Prepare for Next Window:   priority_score >= 2.5 AND thematic_fit >= 3 AND application_timing is recurring_uncertain AND route is direct or clear
Strategic Positioning:     strategic_value >= 4 AND thematic_fit <= 3; any confirmed timing
Low Priority:              priority_score < 2.5 OR thematic_fit <= 2 OR application_timing is timing_unknown

HARD RULES:
- Relationship-led, unclear, or indirect routes can never be Must Pursue.
- timing_unknown items can only be Low Priority or Strategic Positioning.
- A priority_score above 4.0 should be genuinely rare.

---

## OUTPUT
Return ONLY a valid JSON object — no markdown fences.

IMPORTANT: generate the fields in exactly this order — executive_summary and strategic_recommendations FIRST, then the opportunity lists. This ensures the strategic sections are always present even if the output is long.

{{
  "executive_summary": {{
    "company_overview": "2-3 sentences on what the company is",
    "strongest_themes": "which funding themes align best and why",
    "strongest_geographies": "where the best funding landscape exists",
    "key_constraints": "main barriers to grant success"
  }},
  "strategic_recommendations": {{
    "best_fit_strategy": "overall recommended approach",
    "best_geographies": "top 1-2 geographies for grant funding",
    "strongest_pathways": "which specific routes to prioritise first",
    "key_partnerships": "institutional partnerships that would unlock funding",
    "capability_gaps": "what the company needs to build or demonstrate",
    "key_risks": "main risks to grant success"
  }},
  "opportunities": [
    {{
      "name": "full grant / programme name",
      "managing_body": "organisation",
      "geography": "relevant geography",
      "opportunity_type": "direct_grant",
      "application_route": "direct_startup_application",
      "has_application_process": true,
      "trl_match": true,
      "geography_match": true,
      "applicant_type_match": "direct | partner | ineligible",
      "application_timing": "open_now | known_future_window | recurring_uncertain | timing_unknown",
      "status": "Open | Recurring | Closed but likely reopening | Monitor",
      "application_link": "Populate using query_apply results first. Use the most specific URL found: direct application portal preferred, then programme overview page, then funder homepage as last resort. Never invent a URL. Use 'unknown' only if no URL was found at all.",
      "link_type": "application_portal | programme_page | funder_homepage | unknown",
      "funding_type": "Grant | Prize | Blended | Concessional | Accelerator",
      "max_funding": "amount / range / unknown",
      "max_funding_eur": 250000,
      "funding_confidence": "High | Medium | Low",
      "thematic_fit_score": 4,
      "thematic_fit_explanation": "1-2 sentences grounded in the research evidence",
      "strategic_value_score": 3,
      "strategic_value_explanation": "1-2 sentences",
      "ease_score": 3,
      "ease_explanation": "1-2 sentences citing process requirements or applicant testimony",
      "priority_score": 3.55,
      "priority_tier": "Must Pursue | Big Bet | Quick Win | Prepare for Next Window | Strategic Positioning | Low Priority",
      "opens_date": "date applications open, e.g. '12 March 2026' or 'February 2026' — 'unknown' if not stated in evidence",
      "deadline": "date applications close — prefer a full date like '12 March 2026' when the evidence gives one; otherwise 'rolling' / 'unknown'",
      "recurrence": "Annual | Biannual | Rolling | One-off | Unknown",
      "project_size_duration": "typical award size and project duration, e.g. '£100k–£500k, 6–18 months' — use 'unknown' if not found in evidence",
      "trl_requirement": "TRL range required by the funder, e.g. 'TRL 4–7', 'TRL 6+', 'Any' — use 'unknown' if not stated",
      "consortium_rules": "solo or consortium requirement, e.g. 'Solo application allowed', 'Consortium of 3+ required', 'Lead must be licensed entity' — use 'unknown' if not stated",
      "past_similar_projects": "1 sentence naming 1-2 past funded projects or awardees similar to this startup, if found in evidence — use 'unknown' if none found",
      "notes": "important eligibility caveats or conditions",
      "reason_for_inclusion": "one sentence — what specifically about this company's product, technology, and market makes it a genuine match for this grant",
      "reason_for_caution": "one sentence — the single most important risk, limitation, or condition the user should know before investing time in this application"
    }}
  ],
  "strategic_watchlist": [
    {{
      "name": "full name",
      "managing_body": "organisation",
      "geography": "relevant geography",
      "opportunity_type": "relationship_led_funder",
      "application_route": "invitation_or_relationship_led",
      "application_timing": "recurring_uncertain",
      "status": "Open | Recurring | Unknown",
      "application_link": "Most specific official URL found, or 'unknown'",
      "link_type": "programme_page | funder_homepage | unknown",
      "funding_type": "Grant | Prize | Blended | Concessional | Accelerator | Unknown",
      "max_funding": "amount or unknown",
      "thematic_relevance": "1-2 sentences on why this is strategically relevant",
      "why_watchlist": "1 sentence on why it is not a direct recommendation right now",
      "what_would_unlock": "1 sentence on what condition would make it actionable"
    }}
  ]
}}

Sort opportunities by priority_score descending. Sort watchlist by thematic relevance descending.
max_funding_eur must be a plain integer: your best estimate of the maximum funding available
to a single applicant, converted to euros (approximate conversion is fine). Use null if unknown.
Keep all explanation fields to 1-2 sentences maximum to stay within output limits."""


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web for information. Use specific, targeted queries "
            "for best results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"}
            },
            "required": ["query"],
        },
    }
]

# ---------------------------------------------------------------------------
# Web search (DuckDuckGo — no API key required)
# ---------------------------------------------------------------------------


def _sync_search(query: str, max_results: int = 4) -> str:
    import time
    from ddgs import DDGS

    results = []
    for attempt in range(5):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            break
        except Exception as exc:
            if attempt < 4:
                time.sleep(3 * (attempt + 1))   # 3s, 6s, 9s, 12s
            else:
                return f"Search unavailable after 5 attempts ({exc}). Skipping."

    if not results:
        return "No results found."

    parts = []
    for r in results:
        summary = (r.get("body") or "")[:400]   # truncate to keep context lean
        parts.append(
            f"Title: {r.get('title', 'N/A')}\n"
            f"URL: {r.get('href', 'N/A')}\n"
            f"Summary: {summary}"
        )
    return "\n\n---\n\n".join(parts)


async def _web_search(query: str, max_results: int = 4) -> str:
    await asyncio.sleep(0.5)
    try:
        return await asyncio.to_thread(_sync_search, query, max_results)
    except Exception as exc:
        import traceback
        traceback.print_exc()   # full traceback visible in uvicorn terminal
        return f"Search failed: {exc}. Skipping."


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict:
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON object found (first 300 chars): {text[:300]}")


def _extract_json_object_tolerant(text: str) -> dict:
    """
    Extract a JSON object, with recovery for truncated output (max_tokens).
    Tries clean parse first; if that fails, extracts each top-level field
    individually so we keep whatever Claude finished writing.
    """
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    # ── Attempt 1: clean parse ───────────────────────────────────────────
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # ── Attempt 2: field-by-field recovery ──────────────────────────────
    result: dict = {}

    # executive_summary — small object, usually complete
    m = re.search(r'"executive_summary"\s*:\s*(\{.*?\})', text, re.DOTALL)
    if m:
        try:
            result["executive_summary"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            result["executive_summary"] = {}

    # opportunities — use the tolerant array extractor
    m = re.search(r'"opportunities"\s*:\s*(\[.*)', text, re.DOTALL)
    if m:
        try:
            result["opportunities"] = _extract_json_array_tolerant(m.group(1))
        except (ValueError, json.JSONDecodeError):
            result["opportunities"] = []

    # strategic_watchlist — use the tolerant array extractor
    m = re.search(r'"strategic_watchlist"\s*:\s*(\[.*)', text, re.DOTALL)
    if m:
        try:
            result["strategic_watchlist"] = _extract_json_array_tolerant(m.group(1))
        except (ValueError, json.JSONDecodeError):
            result["strategic_watchlist"] = []

    # strategic_recommendations — small object, try to grab it
    m = re.search(r'"strategic_recommendations"\s*:\s*(\{.*?\})', text, re.DOTALL)
    if m:
        try:
            result["strategic_recommendations"] = json.loads(m.group(1))
        except json.JSONDecodeError:
            result["strategic_recommendations"] = {}

    if result:
        return result

    raise ValueError(
        f"Could not parse or recover JSON object. First 300 chars: {text[:300]}"
    )


def _extract_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"No JSON array found (first 300 chars): {text[:300]}")


def _extract_json_array_tolerant(text: str) -> list:
    """
    Extract a JSON array from text, with robust recovery for truncated
    or partially malformed output.

    Strategy:
    1. Try a clean parse of the full array — works in the normal case.
    2. If that fails, scan character-by-character tracking brace depth
       to pull out each top-level {...} block individually, parse each
       one, and collect all that succeed. This recovers whatever Claude
       managed to write before hitting a token limit or making a small
       JSON mistake.
    """
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)

    # ── Attempt 1: clean parse ───────────────────────────────────────────
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # ── Attempt 2: extract individual objects ────────────────────────────
    start = text.find("[")
    if start == -1:
        raise ValueError(f"No JSON array start found. First 300 chars: {text[:300]}")

    content = text[start + 1:]   # everything after the opening [
    objects: list = []
    depth = 0
    obj_start: int | None = None

    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                candidate = content[obj_start : i + 1]
                try:
                    obj = json.loads(candidate)
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass  # skip malformed individual entries
                obj_start = None

    if objects:
        return objects

    raise ValueError(
        f"Could not recover any valid objects from JSON array. "
        f"First 300 chars: {text[:300]}"
    )


# ---------------------------------------------------------------------------
# Shared tool-use loop with hard search cap + forced-output final call
# ---------------------------------------------------------------------------


async def _run_tool_loop(
    system: str,
    messages: list,
    max_searches: int,
    max_output_tokens: int,
    on_search: Callable[[str, int], None] | None = None,
    extract: str = "object",  # "object" or "array"
) -> dict | list:
    """
    Run a Claude tool-use loop up to max_searches searches.

    When the search budget is exhausted we omit the `tools` parameter
    entirely on the next call — Claude cannot produce tool_use blocks
    without it, so it is forced to write its text output instead.

    We never append two consecutive user-role messages, which the
    Anthropic API rejects.

    Uses `async with` so the httpx connection pool is always closed on exit.
    """
    search_count = 0

    async with anthropic.AsyncAnthropic() as client:
        while True:
            # Build API call — only offer tools while budget remains
            call_kwargs: dict = {
                "model": "claude-sonnet-4-6",
                "max_tokens": max_output_tokens,
                # temperature=0 minimises run-to-run sampling variance for
                # extraction/classification work (not full determinism, but
                # meaningfully more stable than the default of 1.0).
                "temperature": 0,
                "system": system,
                "messages": messages,
            }
            if search_count < max_searches:
                call_kwargs["tools"] = _TOOLS

            try:
                response = await client.messages.create(**call_kwargs)
                _record_usage(response.usage)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                raise RuntimeError(f"Anthropic API call failed: {exc}") from exc

            # ── Claude produced text output ──────────────────────────────────
            if response.stop_reason in ("end_turn", "stop_sequence", "max_tokens"):
                text_blocks = [b for b in response.content if hasattr(b, "text")]
                if not text_blocks:
                    raise ValueError("Model returned no text.")
                text = text_blocks[-1].text
                if extract == "array":
                    return _extract_json_array_tolerant(text)
                return _extract_json_object(text)

            # ── Claude wants to search ───────────────────────────────────────
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    search_count += 1
                    query = block.input.get("query", "")
                    if on_search:
                        on_search(query, search_count)
                    result = await _web_search(query)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

                # Merge stop notice into the tool-result user message so we
                # never produce two consecutive user-role messages
                if search_count >= max_searches:
                    tool_results.append({
                        "type": "text",
                        "text": (
                            f"Search budget reached ({max_searches} searches used). "
                            "Do not search again. Write your JSON output now."
                        ),
                    })

                messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": tool_results},
                ]
                continue

            raise ValueError(f"Unexpected stop_reason: {response.stop_reason}")


# ---------------------------------------------------------------------------
# Phase 1: Company analysis (website + external research)
# ---------------------------------------------------------------------------


async def _analyse_company(
    company_context: str,
    source_note: str,
    url: str | None,
    on_search: Callable[[str, int], None] | None = None,
) -> dict:
    system = _COMPANY_SYSTEM.replace("{max_searches}", str(MAX_COMPANY_SEARCHES))
    url_line = f"Company website URL: {url}\n\n" if url else ""
    messages = [{
        "role": "user",
        "content": (
            url_line
            + f"{source_note}\n\n"
            + f"COMPANY INFORMATION:\n{company_context}"
        ),
    }]
    return await _run_tool_loop(
        system=system,
        messages=messages,
        max_searches=MAX_COMPANY_SEARCHES,
        max_output_tokens=4096,
        on_search=on_search,
        extract="object",
    )


# ---------------------------------------------------------------------------
# Phase 2: Grant discovery — three clean steps, no tool-use loop
# ---------------------------------------------------------------------------


async def _generate_discovery_queries(
    company_profile: dict,
    preferences: dict,
    n: int | None = None,
    exclude_programmes: list[str] | None = None,
) -> list[str]:
    """Step 2a — ask Claude to generate search queries (no searching yet).

    n: number of queries to generate (defaults to MAX_DISCOVERY_SEARCHES)
    exclude_programmes: programmes already being searched via mandatory queries —
        tell Claude to skip these so it uses its budget on other opportunities.
    """
    count = n if n is not None else MAX_DISCOVERY_SEARCHES
    excludes_note = (
        "\n".join(f"  - {p}" for p in exclude_programmes)
        if exclude_programmes else "(none)"
    )
    system = (
        _DISCOVERY_QUERY_SYSTEM
        .replace("{n}", str(count))
        .replace("{exclude_programmes}", excludes_note)
        .replace("{years}", _year_qualifier())
    )
    async with anthropic.AsyncAnthropic() as client:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=2000,
            temperature=0,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"COMPANY PROFILE:\n{json.dumps(company_profile, indent=2)}\n\n"
                    "USER PREFERENCES:\n"
                    f"- Open to consortium applications: {preferences.get('consortium', True)}\n"
                    f"- Open to accelerators/incubators: {preferences.get('accelerators', True)}\n"
                    f"- Open to challenge prizes: {preferences.get('prizes', True)}\n"
                    f"- Priority geographies: {preferences.get('geographies') or 'not specified'}\n\n"
                    f"Generate {count} search queries."
                ),
            }],
        )
        _record_usage(response.usage)
    try:
        return _extract_json_array(response.content[0].text)
    except Exception:
        return _extract_json_array_tolerant(response.content[0].text)


async def _execute_discovery_queries(
    queries: list[str],
    on_search: Callable[[str, int], None] | None = None,
) -> dict[str, str]:
    """Step 2b — execute all queries ourselves and return results dict."""
    results: dict[str, str] = {}
    for i, query in enumerate(queries[:MAX_DISCOVERY_SEARCHES], 1):
        if on_search:
            on_search(query, i)
        results[query] = await _web_search(
            query, max_results=DISCOVERY_RESULTS_PER_SEARCH
        )
    return results


async def _analyse_discovery_results(
    company_profile: dict,
    search_results: dict[str, str],
    preferences: dict,
) -> list:
    """Step 2c — analyse all results in one API call, return longlist.

    Uses streaming so a large longlist (LONGLIST_MAX_TOKENS) never hits a
    fixed read-timeout.
    """
    # Keep DISCOVERY_SNIPPET_CHARS per search — enough for ~5 formatted
    # results. Total input at 30 searches is ~75K chars (~19K tokens).
    results_text = ""
    for i, (query, result) in enumerate(search_results.items(), 1):
        results_text += (
            f"\n\n=== Search {i}: {query} ===\n{result[:DISCOVERY_SNIPPET_CHARS]}"
        )

    async with anthropic.AsyncAnthropic() as client:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=LONGLIST_MAX_TOKENS,
            temperature=0,
            system=_DISCOVERY_ANALYSIS_SYSTEM.replace("{years}", _year_qualifier()),
            messages=[{
                "role": "user",
                "content": (
                    f"COMPANY PROFILE:\n{json.dumps(company_profile, indent=2)}\n\n"
                    "USER PREFERENCES:\n"
                    f"- Open to consortium: {preferences.get('consortium', True)}\n"
                    f"- Open to accelerators: {preferences.get('accelerators', True)}\n"
                    f"- Open to prizes: {preferences.get('prizes', True)}\n"
                    f"- Priority geographies: {preferences.get('geographies') or 'not specified'}\n\n"
                    f"SEARCH RESULTS ({len(search_results)} searches):{results_text}\n\n"
                    "Compile the grant opportunity longlist as a JSON array."
                ),
            }],
        ) as stream:
            text = await stream.get_final_text()
            _record_usage((await stream.get_final_message()).usage)
    return _extract_json_array_tolerant(text)


def _dedupe_longlist(longlist: list) -> list:
    """
    Collapse near-duplicate longlist entries (same programme surfaced by
    several queries) using the fuzzy name matcher. Keeps the entry with the
    higher initial_thematic_fit — ties keep the earlier (usually more
    specific) entry — so a duplicate never occupies a second shortlist slot.
    """
    kept: list = []
    for item in longlist:
        name = item.get("name") or ""
        if not name:
            continue
        dup_idx = next(
            (i for i, k in enumerate(kept) if _names_similar(name, k.get("name") or "")),
            None,
        )
        if dup_idx is None:
            kept.append(item)
        elif (item.get("initial_thematic_fit") or 0) > (kept[dup_idx].get("initial_thematic_fit") or 0):
            kept[dup_idx] = item
    return kept


async def _discover_opportunities(
    company_profile: dict,
    preferences: dict,
    on_search: Callable[[str, int], None] | None = None,
) -> list:
    """Orchestrate the three discovery steps.

    Mandatory queries always run — they target high-value recurring programmes
    (Innovate UK, KTP, Welsh Government SMART FIS, Ofgem SIF, EIC Accelerator)
    that must never be missed due to AI query generation blind spots.

    The AI is told how many queries to generate (MAX_DISCOVERY_SEARCHES minus
    the mandatory count) so the total stays within budget, and is explicitly
    told which programmes are already covered so it spends its quota on other
    opportunities rather than duplicating the mandatory searches.
    """
    mandatory = _mandatory_queries(company_profile)

    # Tell the AI what is already being searched and how many queries to fill
    ai_count    = max(10, MAX_DISCOVERY_SEARCHES - len(mandatory))
    ai_queries  = await _with_retry(
        lambda: _generate_discovery_queries(
            company_profile, preferences,
            n=ai_count,
            exclude_programmes=mandatory,
        ),
    )

    # Mandatory queries first — they are always executed, guaranteed
    queries = mandatory + ai_queries

    search_results = await _execute_discovery_queries(queries, on_search=on_search)
    # _analyse_discovery_results is a long streaming call — retry on network error
    longlist = await _with_retry(
        lambda: _analyse_discovery_results(company_profile, search_results, preferences),
    )
    return _dedupe_longlist(longlist)


# ---------------------------------------------------------------------------
# Phase 3a: Generate research queries for shortlisted grants
# ---------------------------------------------------------------------------


async def _generate_research_queries(shortlist: list) -> list:
    """Ask Claude to produce 3 search queries per grant (no tool use)."""
    async with anthropic.AsyncAnthropic() as client:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=6000,
            temperature=0,
            system=_QUERY_GENERATION_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    "Generate 3 research queries for each of these grant opportunities:\n\n"
                    + json.dumps(
                        [{"name": g["name"], "managing_body": g.get("managing_body", ""),
                          "geography": g.get("geography", "")} for g in shortlist],
                        indent=2,
                    )
                ),
            }],
        )
        _record_usage(response.usage)
    return _extract_json_array(response.content[0].text)


# ---------------------------------------------------------------------------
# Phase 3b: Execute research queries and collect evidence
# ---------------------------------------------------------------------------


async def _execute_research_queries(
    queries: list,
    on_search: Callable[[str, int], None] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Execute all research queries.

    Returns a 2-tuple:
      evidence       — dict keyed by grant name → concatenated results from all 3 queries
      apply_evidence — dict keyed by grant name → results from query_apply only
                       (used by specificity validation to confirm a real application
                        process exists before promoting a grant to main recommendations)
    """
    evidence_parts: dict[str, list[str]] = {}
    apply_evidence: dict[str, str] = {}
    search_count = 0

    for item in queries:
        name = item.get("name", "unknown")
        evidence_parts.setdefault(name, [])

        for q_key in ("query_official", "query_commentary", "query_apply"):
            query = item.get(q_key, "")
            if not query:
                continue
            search_count += 1
            if on_search:
                on_search(query, search_count)
            result = await _web_search(query)
            evidence_parts[name].append(f"[{q_key}: {query}]\n{result}")
            if q_key == "query_apply":
                apply_evidence[name] = f"[Query: {query}]\n{result}"

    combined = {name: "\n\n".join(parts) for name, parts in evidence_parts.items()}
    return combined, apply_evidence


# ---------------------------------------------------------------------------
# Phase 3c: Score using gathered evidence (no tools)
# ---------------------------------------------------------------------------


async def _score_with_evidence(
    company_profile: dict,
    shortlist: list,
    evidence: dict,
    preferences: dict,
    on_progress: Callable[[int], None] | None = None,
) -> dict:
    """
    Single scoring call — no tools. Claude applies rubric using evidence.

    Uses streaming so the call never times out on long generation.
    Generating scored JSON for 15 grants takes 2-4 minutes at normal
    Sonnet speeds; a non-streaming call with a fixed timeout would fire
    before the response finishes.  With streaming, per-chunk read
    timeouts (a few seconds each) apply instead of a single total
    deadline, so the call completes regardless of generation length.

    on_progress(chars_so_far) is called every ~600 chars so the caller
    can emit heartbeat messages to the UI during the long generation.
    """
    # Truncate evidence per grant to keep input manageable.
    # Full evidence (~3 300 chars/grant × 15 = ~50 000 chars) is unnecessary;
    # 800 chars/grant captures the key eligibility facts.
    MAX_EVIDENCE_CHARS = 800

    annotated = []
    for grant in shortlist:
        name = grant.get("name", "")
        raw_evidence = evidence.get(name, "No evidence gathered.")
        entry = dict(grant)
        entry["research_evidence"] = (
            raw_evidence[:MAX_EVIDENCE_CHARS] + "…"
            if len(raw_evidence) > MAX_EVIDENCE_CHARS
            else raw_evidence
        )
        annotated.append(entry)

    user_message = (
        "Score these grant opportunities using the research evidence provided.\n\n"
        f"COMPANY PROFILE:\n{json.dumps(company_profile, indent=2)}\n\n"
        "USER PREFERENCES:\n"
        f"- Open to consortium applications: {preferences.get('consortium', True)}\n"
        f"- Open to accelerators/incubators: {preferences.get('accelerators', True)}\n"
        f"- Open to challenge prizes: {preferences.get('prizes', True)}\n"
        f"- Priority geographies: {preferences.get('geographies') or 'not specified'}\n\n"
        f"SHORTLISTED GRANTS WITH RESEARCH EVIDENCE "
        f"({len(annotated)} opportunities):\n"
        + json.dumps(annotated, indent=2)
    )

    try:
        async with anthropic.AsyncAnthropic() as client:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=SCORING_MAX_TOKENS,
                temperature=0,
                system=_SCORING_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            ) as stream:
                chunks: list[str] = []
                chars_generated = 0
                last_reported = 0
                async for chunk in stream.text_stream:
                    chunks.append(chunk)
                    chars_generated += len(chunk)
                    # Fire a heartbeat roughly every 600 chars (~150 tokens)
                    if on_progress and chars_generated - last_reported >= 600:
                        on_progress(chars_generated)
                        last_reported = chars_generated
                text = "".join(chunks)
                _record_usage((await stream.get_final_message()).usage)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"Scoring API call failed: {exc}") from exc

    return _extract_json_object_tolerant(text)


# ---------------------------------------------------------------------------
# Link verification
# ---------------------------------------------------------------------------

_LINK_CHECK_TIMEOUT  = 10   # seconds per request
_LINK_CHECK_HEADERS  = {"User-Agent": "Mozilla/5.0 (compatible; GrantAnalyser/1.0)"}


async def _check_one_link(url: str) -> str:
    """
    Return "verified", "broken", or "unverified" for a single URL.

    Strategy:
      1. Reject immediately if url is empty / "unknown" / not http(s).
      2. Try HEAD (fast, no body transfer).
      3. If the server returns 405 Method Not Allowed, fall back to GET
         with a Range header so we only pull the first byte.
      4. Any network error → "broken".
    """
    if not url or url.lower() in ("unknown", "n/a", "") or not url.startswith("http"):
        return "unverified"
    try:
        async with httpx.AsyncClient(
            headers=_LINK_CHECK_HEADERS,
            follow_redirects=True,
            timeout=_LINK_CHECK_TIMEOUT,
        ) as client:
            try:
                r = await client.head(url)
                if r.status_code == 405:
                    raise ValueError("HEAD not allowed")
                return "verified" if r.status_code < 400 else "broken"
            except ValueError:
                # Fall back to a byte-range GET so we don't download full pages
                r = await client.get(
                    url, headers={**_LINK_CHECK_HEADERS, "Range": "bytes=0-0"}
                )
                return "verified" if r.status_code < 400 else "broken"
    except Exception:
        return "broken"


async def _verify_application_links(
    opportunities: list,
    watchlist: list,
) -> tuple[list, list]:
    """
    Parallel HTTP checks for every application_link in both lists.
    Adds link_status: "verified" | "broken" | "unverified" to each item.
    All requests run concurrently; total wall-clock time ≈ worst single check.
    """
    all_items = opportunities + watchlist
    urls      = [item.get("application_link", "") for item in all_items]

    statuses = await asyncio.gather(
        *[_check_one_link(url) for url in urls],
        return_exceptions=True,
    )

    for item, status in zip(all_items, statuses):
        item["link_status"] = "broken" if isinstance(status, Exception) else status

    return opportunities, watchlist


# ---------------------------------------------------------------------------
# Watchlist link resolution — find URLs for watchlist items that have none
# ---------------------------------------------------------------------------

_MAX_WATCHLIST_LINK_SEARCHES = 12   # bound the extra search cost per run

_WATCHLIST_LINK_SYSTEM = """You are a grants research analyst. For each grant programme provided, you have a current_link (may be "unknown") and web search results from a query designed to find the programme's official page or application route.

For each programme, pick the single best URL and classify it:
- Prefer: a direct application portal, form, or competition entry page → link_type "application_portal"
- Then: the official programme or call overview page → link_type "programme_page"
- Then: the funder's homepage → link_type "funder_homepage"
- If current_link already points to the correct official programme page and nothing more specific appears in the search results, return current_link with the appropriate link_type
- Do NOT invent or modify URLs — only return current_link or a URL that appears verbatim in the search results
- Ignore URLs for clearly DIFFERENT programmes, third-party blog posts, and news aggregators when an official source is available
- If neither current_link nor the search results contain a relevant official URL, return "unknown" with link_type "unknown"

Return ONLY a valid JSON array — no markdown fences:
[
  {{
    "name": "exact programme name as given",
    "best_url": "URL or unknown",
    "link_type": "application_portal | programme_page | funder_homepage | unknown"
  }}
]"""


async def _resolve_watchlist_links(
    watchlist: list,
    on_search: Callable[[str, int], None] | None = None,
) -> list:
    """
    Resolve and classify application links for under-linked watchlist items.

    Watchlist items produced by the rescue safety nets come straight from the
    discovery longlist and were never deep-researched: in practice they carry
    a URL scraped from a search snippet with link_type "unknown" (never
    classified or checked for specificity), or no URL at all. This runs ONE
    web search per such item (bounded by _MAX_WATCHLIST_LINK_SEARCHES) and a
    single batch Claude call to pick and classify the best official URL —
    keeping the existing URL when it is already the right official page.
    Items with an already-classified link are left untouched.
    """
    missing = [
        item for item in watchlist
        if item.get("link_type", "unknown") == "unknown"
        or not (item.get("application_link") or "").startswith("http")
    ][:_MAX_WATCHLIST_LINK_SEARCHES]
    if not missing:
        return watchlist

    batch_items = []
    for i, item in enumerate(missing, 1):
        name = item.get("name", "")
        body = item.get("managing_body", "")
        query = f"{name} {body} grant apply application".strip()
        if on_search:
            on_search(query, i)
        results = await _web_search(query)
        batch_items.append({
            "name": name,
            "current_link": item.get("application_link", "unknown"),
            "search_results": results[:1500],
        })

    try:
        async with anthropic.AsyncAnthropic() as client:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=2000,
                temperature=0,
                system=_WATCHLIST_LINK_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": (
                        "Pick the best official URL for each programme:\n\n"
                        + json.dumps(batch_items, indent=2)
                    ),
                }],
            )
            _record_usage(response.usage)
        try:
            resolutions = _extract_json_array(response.content[0].text)
        except Exception:
            resolutions = _extract_json_array_tolerant(response.content[0].text)
    except Exception:
        # Link resolution is best-effort — never fail the run over it
        return watchlist

    resolution_map = {
        r["name"]: r for r in resolutions
        if isinstance(r, dict) and r.get("name")
    }
    for item in missing:
        res = resolution_map.get(item.get("name", ""))
        if not res:
            continue
        url = res.get("best_url", "")
        if url and url.startswith("http"):
            item["application_link"] = url
            lt = res.get("link_type", "programme_page")
            item["link_type"] = lt if lt in (
                "application_portal", "programme_page", "funder_homepage"
            ) else "programme_page"

    return watchlist


# ---------------------------------------------------------------------------
# Link quality gate — a top-tier recommendation must have a real, working,
# reasonably specific link
# ---------------------------------------------------------------------------

_LINK_GATED_TIERS = {"Must Pursue", "Quick Win"}
_LINK_GATE_DEMOTED_TIER = "Prepare for Next Window"


def _apply_link_quality_gate(opportunities: list) -> list:
    """
    Post-verification safety net: an item in a top action tier ("Must Pursue"
    / "Quick Win") is an instruction to go apply NOW — a broken link, a bare
    funder homepage, or no link at all undermines that instruction. Such
    items are demoted one tier and annotated so the user knows why.

    Also downgrades link_type on any item whose URL failed the HTTP check,
    so exports don't present a dead link as a confirmed portal.
    """
    for opp in opportunities:
        status = opp.get("link_status", "unverified")
        ltype  = opp.get("link_type", "unknown")

        if status == "broken":
            opp["link_type"] = "unknown"
            note = (
                "Application link failed an automated availability check — "
                "verify the URL before relying on it."
            )
            existing = opp.get("notes") or ""
            if note not in existing:
                opp["notes"] = (existing + "  " + note).strip() if existing else note

        weak_link = (
            status == "broken"
            or ltype in ("funder_homepage", "unknown")
            or not (opp.get("application_link") or "").startswith("http")
        )
        if weak_link and opp.get("priority_tier") in _LINK_GATED_TIERS:
            original = opp["priority_tier"]
            opp["priority_tier"] = _LINK_GATE_DEMOTED_TIER
            note = (
                f"Demoted from '{original}': no specific, working application "
                "link was confirmed. Locate the official application page "
                "before investing application effort."
            )
            existing = opp.get("notes") or ""
            if note not in existing:
                opp["notes"] = (existing + "  " + note).strip() if existing else note

    return opportunities


# ---------------------------------------------------------------------------
# Specificity validation — confirm each main recommendation has a real,
# named application process before showing it to the user
# ---------------------------------------------------------------------------

_SPECIFICITY_VALIDATION_SYSTEM = """You are a grants research analyst. For each grant opportunity provided, you have search results from a query specifically designed to find the direct application URL for that programme.

Your task: assign one of three status values and identify the most specific URL available.

## THE CORE QUESTION
"Has this programme ever run a public application process, and is it likely to do so again?"
This is NOT: "Is there an open round right now?" — that is handled separately by application_timing.

## STATUS VALUES

### "confirmed" — set this if:
- An official application portal, form, or competition entry page exists (even if login/registration is required)
- Evidence clearly shows a live or recently-closed application round with a specific URL
- Evidence mentions "apply now", "call for proposals", "open call", "competition entry", a specific deadline, or a forthcoming round with a specific URL

### "likely" — set this if:
- The programme is a well-known, institutionalised recurring fund that has run public rounds before — even if no active round appears in the search results right now. Examples: Ofgem Strategic Innovation Fund, Innovate UK Smart Grants, Innovate UK competitions, EIC Accelerator, Horizon Europe calls, Welsh Government SMART FIS, Ofgem Network Innovation Competition. These are confirmed by their nature and will open again.
- Evidence shows the programme has previously run open rounds but is currently between rounds with no active call
- The programme has a clear documented history of recurrent open calls with no indication it has been cancelled or discontinued

### "unconfirmed" — set this ONLY if:
- The evidence only shows a general funder homepage with no reference to any application process past or present
- The programme is clearly relationship-led with no public application route ("contact us", "by invitation only")
- The search results refer to a clearly DIFFERENT programme than the one named (e.g. named "Heat Network Transformation Programme" but search snippets only describe the "Heat Network Efficiency Scheme" — different programmes from the same funder are NOT interchangeable)
- A generic competition listing page (path ending in /competition/search) is the only URL found AND no search snippets confirm this specific programme exists
- No search results were returned AND the programme name is obscure or unverifiable

## BEST URL
From the search snippets, extract the single most specific and relevant URL:
- Prefer: a URL with programme-specific path segments (e.g. /competition/1234/overview or /grants/specific-fund-name)
- Accept: official programme overview page (right funder, right programme, just not the form itself)
- If current_link already points to the correct specific programme page, return it unchanged
- Last resort: funder homepage only if it is the sole result AND status is "confirmed" or "likely"
- A generic competition search page (path ending in /competition/search) is NOT specific — return "unknown" instead
- Do NOT return a URL that is not in the search results or current_link

## OUTPUT
Return ONLY a valid JSON array — no markdown fences:
[
  {{
    "name": "exact grant name as given",
    "status": "confirmed",
    "best_url": "most specific URL found, or 'unknown'",
    "reason": "one sentence explaining the decision"
  }}
]"""


async def _validate_application_specificity(
    opportunities: list,
    watchlist: list,
    apply_evidence: dict[str, str],
) -> tuple[list, list]:
    """
    Post-scoring validation step.

    For every main recommendation, uses the query_apply search evidence to
    confirm that a specific, named application process actually exists.

    Outcome per grant (watchlist admission policy — see constants):
      "confirmed"   — stays a main recommendation; application_link may be
                      upgraded to a more specific URL found in the evidence.
      "likely"      — an established recurring programme currently between
                      rounds: moved to the strategic watchlist as a
                      "between_rounds" entry when thematic fit is strong
                      (>= STRONG_FIT_MIN), otherwise dropped.
      "unconfirmed" — no evidence a public application process exists:
                      DROPPED entirely. An unverifiable route is not
                      actionable, so it earns neither list.

    This runs as a single Claude batch call (one API request for all grants),
    not per-grant calls, to keep latency low.
    """
    if not opportunities:
        return opportunities, watchlist

    # Build the batch input — include current link so Claude can compare
    batch_items = []
    for opp in opportunities:
        name = opp.get("name", "")
        evidence = apply_evidence.get(name, "No application search evidence available.")
        batch_items.append({
            "name": name,
            "current_link": opp.get("application_link", "unknown"),
            "link_type": opp.get("link_type", "unknown"),
            "apply_search_evidence": evidence[:1500],
        })

    try:
        async with anthropic.AsyncAnthropic() as client:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=4000,
                temperature=0,
                system=_SPECIFICITY_VALIDATION_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": (
                        "Validate the following grant opportunities using the "
                        "apply_search_evidence for each:\n\n"
                        + json.dumps(batch_items, indent=2)
                    ),
                }],
            )
            _record_usage(response.usage)
        try:
            validations = _extract_json_array(response.content[0].text)
        except Exception:
            validations = _extract_json_array_tolerant(response.content[0].text)
    except Exception:
        # If validation itself fails, return everything unchanged rather than
        # wiping the results list
        return opportunities, watchlist

    validation_map = {v["name"]: v for v in validations if isinstance(v, dict)}

    confirmed_opps: list = []
    demoted: list = []

    for opp in opportunities:
        name = opp.get("name", "")
        val  = validation_map.get(name)

        if val is None:
            # No validation result for this grant — keep it rather than incorrectly demoting
            confirmed_opps.append(opp)
            continue

        # Support both the new string status ("confirmed"/"likely"/"unconfirmed")
        # and any legacy boolean format from older cached results
        raw_status = val.get("status", val.get("confirmed", True))
        if isinstance(raw_status, bool):
            status = "confirmed" if raw_status else "unconfirmed"
        else:
            status = str(raw_status).lower()

        # Resolve the best URL once — used by both confirmed and likely paths
        best_url = val.get("best_url", "")
        has_better_url = (
            best_url
            and best_url not in ("unknown", "n/a", "")
            and best_url.startswith("http")
        )

        if status == "confirmed":
            updated = dict(opp)   # shallow copy — don't mutate the original
            if has_better_url:
                updated["application_link"] = best_url
                # If we upgraded from a funder homepage, mark as at least programme_page
                if updated.get("link_type") == "funder_homepage":
                    updated["link_type"] = "programme_page"
            confirmed_opps.append(updated)

        elif status == "likely":
            # Established recurring programme currently between rounds.
            # Strong fit → strategic watchlist as a "between_rounds" entry;
            # weaker fit → dropped (watchlist slots are reserved for
            # strong-fit items only).
            fit = opp.get("thematic_fit_score")
            if not isinstance(fit, (int, float)) or fit < STRONG_FIT_MIN:
                continue
            demoted.append({
                "name":              opp.get("name", ""),
                "managing_body":     opp.get("managing_body", ""),
                "geography":         opp.get("geography", ""),
                "opportunity_type":  opp.get("opportunity_type", ""),
                "application_route": opp.get("application_route", ""),
                "application_timing": opp.get("application_timing", "recurring_uncertain"),
                "status":            opp.get("status", "Recurring"),
                "application_link":  best_url if has_better_url else opp.get("application_link", "unknown"),
                "link_type":         opp.get("link_type", "unknown"),
                "funding_type":      opp.get("funding_type", ""),
                "max_funding":       opp.get("max_funding", "unknown"),
                "thematic_relevance": opp.get("thematic_fit_explanation", ""),
                "watchlist_class":   "between_rounds",
                "thematic_fit":      fit,
                "why_watchlist": (
                    "Well-established recurring programme with no application "
                    "window currently open. "
                    + (val.get("reason", ""))
                ).strip(),
                "what_would_unlock": (
                    "The next application round opening — monitor the programme "
                    "page and prepare the application in advance."
                ),
            })

        # "unconfirmed" — no evidence any public application process exists.
        # Dropped entirely: not actionable, so it earns neither list.

    # Append between-rounds demotions to the existing watchlist
    combined_watchlist = watchlist + demoted
    return confirmed_opps, combined_watchlist


# ---------------------------------------------------------------------------
# Acronym definitions — for the "Definitions acronyms" tab in the XLSX export
# ---------------------------------------------------------------------------

_ACRONYMS_MAX_TOKENS = 2500
_ACRONYMS_CORPUS_CHARS = 16000


async def _generate_acronym_definitions(opportunities: list) -> list:
    """
    Extract the acronyms/abbreviations used across the final opportunity
    rows and define each one. Returns [{"acronym", "definition"}, ...]
    sorted alphabetically. Non-fatal: returns [] on any failure — the
    exporter falls back to a static general dictionary.
    """
    if not opportunities:
        return []

    corpus = json.dumps(
        [{k: v for k, v in opp.items() if isinstance(v, str)} for opp in opportunities],
        ensure_ascii=False,
    )[:_ACRONYMS_CORPUS_CHARS]

    prompt = (
        "Below is the JSON content of a grant-opportunities report. List every "
        "acronym or abbreviation that appears in the text — programme names, "
        "funding instruments, agencies, and technical terms. For each, give a "
        "concise one-line definition appropriate to the grant-funding context. "
        "Only include acronyms that actually appear in the text; never invent "
        "entries. Return ONLY a JSON array in this form:\n"
        '[{"acronym": "TRL", "definition": "Technology Readiness Level '
        '(scale from 1–9 measuring maturity of a technology)"}]\n\n'
        f"REPORT CONTENT:\n{corpus}"
    )

    try:
        async with anthropic.AsyncAnthropic() as client:
            response = await client.messages.create(
                model=MODEL,
                max_tokens=_ACRONYMS_MAX_TOKENS,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            _record_usage(response.usage)
        items = _extract_json_array(response.content[0].text)
    except Exception:
        return []

    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        acro = str(item.get("acronym") or "").strip()
        defn = str(item.get("definition") or "").strip()
        if acro and defn and acro.upper() not in seen:
            seen.add(acro.upper())
            out.append({"acronym": acro, "definition": defn})
    out.sort(key=lambda d: d["acronym"].upper())
    return out


# ---------------------------------------------------------------------------
# Hard routing enforcement — post-scoring safety net
# ---------------------------------------------------------------------------

# Opportunity types that should be excluded entirely (not even watchlist)
_EXCLUDE_ENTIRELY_TYPES = {
    "generic_portal", "policy_programme", "past_award_or_news",
    "closed_one_off", "exclude", "equity_or_investment", "procurement",
}


def _violates_known_hard_gates(opp: dict, profile: dict | None) -> str | None:
    """
    Code-level mirror of _SCORING_SYSTEM's "KNOWN HARD ELIGIBILITY GATES".

    The scoring model sometimes overrides these gates — e.g. classifying
    Energy Catalyst geography_match=true by asserting "UK-relevant rounds"
    exist — so the confirmed gates are re-checked here, conditioned on the
    COMPANY profile (not applied blindly): an ODA-only programme is fine for
    a company deploying in developing markets, and a TRL 1-4 programme is
    fine for an early-TRL company.

    Returns a short reason string when the item must be excluded, else None.
    """
    if not profile:
        return None
    name = (opp.get("name") or "").lower()

    # ODA-only programmes: fund deployment in developing countries only
    if "energy catalyst" in name:
        geo = " ".join(
            [profile.get("hq") or ""]
            + (profile.get("operational_geographies") or [])
            + (profile.get("customer_geographies") or [])
        ).lower()
        oda_markers = (
            "africa", "south asia", "southeast asia", "indo-pacific",
            "latin america", "developing",
        )
        if not any(m in geo for m in oda_markers):
            return "ODA-only geography: funds deployment in developing countries"

    # Breakthrough-science calls: TRL 1-4 only
    if "pathfinder" in name:
        m = re.search(r"\d", profile.get("trl") or "")
        if m and int(m.group()) >= 5:
            return "TRL 1-4 only: company is at TRL 5+"

    return None


def _enforce_routing_rules(
    opportunities: list,
    watchlist: list,
    profile: dict | None = None,
) -> tuple[list, list]:
    """
    Enforce the scoring rubric's routing rules in code.

    The scoring model occasionally violates its own Step 2 routing rules.
    This function corrects those mistakes after the fact, guaranteeing:

    EXCLUDE ENTIRELY (hard factual mismatches only):
      - trl_match is False (confirmed TRL mismatch — grant requires a TRL
        the company cannot meet)
      - geography_match is False (grant requires deployment geography the
        company does not operate in)
      - opportunity_type is in the exclude set (wrong category entirely)

    MOVE TO WATCHLIST (tagged "partner_route" — admission class (a)):
      - applicant_type_match is "partner" (can participate but not lead)
      - applicant_type_match is "ineligible" AND the item carries
        regulated-entity-lead signals — the scoring model routinely writes
        "ineligible" for programmes that actually have a partner route
        (energy network funds, NHS funds, social housing schemes, local
        authority programmes), so these are reclassified rather than lost.

    DROP:
      - applicant_type_match is "ineligible" with no partner-route signals.
        Under the watchlist admission policy an item earns a slot only as a
        partner-route or between-rounds entry; a plainly ineligible
        programme is neither.
    """
    kept_opps: list = []
    extra_watch: list = []

    def _is_false(val) -> bool:
        """Return True only for a confirmed False value (bool or string)."""
        if isinstance(val, bool):
            return val is False
        if isinstance(val, str):
            return val.lower() == "false"
        return False

    def _to_watchlist(opp: dict, why: str, what: str) -> dict:
        return {
            "name":              opp.get("name", ""),
            "managing_body":     opp.get("managing_body", ""),
            "geography":         opp.get("geography", ""),
            "opportunity_type":  opp.get("opportunity_type", ""),
            "application_route": opp.get("application_route", ""),
            "application_timing": opp.get("application_timing", "timing_unknown"),
            "status":            opp.get("status", ""),
            "application_link":  opp.get("application_link", "unknown"),
            "link_type":         opp.get("link_type", "unknown"),
            "funding_type":      opp.get("funding_type", ""),
            "max_funding":       opp.get("max_funding", "unknown"),
            "thematic_relevance": opp.get("thematic_fit_explanation", ""),
            "watchlist_class":   "partner_route",
            "thematic_fit":      opp.get("thematic_fit_score"),
            "why_watchlist":     why,
            "what_would_unlock": what,
        }

    _PARTNER_WHY = (
        "Startup cannot be lead applicant or direct funding recipient "
        "for this programme — the grant requires a licensed operator, "
        "public body, or regulated entity as the applicant of record. "
        "Participation as a named project partner under an eligible lead "
        "organisation may be possible and is worth pursuing."
    )
    _PARTNER_WHAT = (
        "Identify a suitable lead partner (e.g. a licensed network operator, "
        "housing association, or NHS trust) willing to be the applicant of "
        "record, with the startup as named technology partner."
    )

    for opp in opportunities:
        opp_type = opp.get("opportunity_type", "")
        trl      = opp.get("trl_match")
        geo      = opp.get("geography_match")
        apt      = opp.get("applicant_type_match", "direct")

        # ── Hard exclude (factual mismatches only) ───────────────────────
        if _is_false(trl):
            continue   # TRL mismatch — confirmed factual, exclude entirely
        if _is_false(geo):
            continue   # Wrong deployment geography — confirmed factual, exclude
        if opp_type in _EXCLUDE_ENTIRELY_TYPES:
            continue   # Wrong category — exclude entirely
        if _violates_known_hard_gates(opp, profile):
            continue   # Confirmed eligibility gate the scoring model overrode

        # ── Route to watchlist as partner_route ──────────────────────────
        if apt == "partner":
            extra_watch.append(_to_watchlist(opp, _PARTNER_WHY, _PARTNER_WHAT))
            continue

        if apt == "ineligible":
            # The model over-uses "ineligible" for regulated-entity-led
            # programmes that actually have a partner route. Reclassify when
            # the signals say so; otherwise drop — a plainly ineligible
            # programme earns no watchlist slot.
            searchable = " ".join([
                opp.get("name", ""), opp.get("managing_body", ""),
                opp.get("notes", "") or "", opp.get("application_route", ""),
            ]).lower()
            if any(sig in searchable for sig in _REGULATED_ENTITY_LEAD_SIGNALS):
                extra_watch.append(_to_watchlist(opp, _PARTNER_WHY, _PARTNER_WHAT))
            continue

        kept_opps.append(opp)

    # The same confirmed gates apply to watchlist entries — a hard-ineligible
    # programme should not be presented for monitoring either.
    combined_watchlist = [
        item for item in watchlist + extra_watch
        if not _violates_known_hard_gates(item, profile)
    ]
    return kept_opps, combined_watchlist


# ---------------------------------------------------------------------------
# Watchlist candidate selection + triage — replaces the old passive rescue
# sweep with an evidence-based admission test (see watchlist policy constants)
# ---------------------------------------------------------------------------

# Text patterns that indicate a programme likely requires a licensed or
# regulated entity as lead applicant — meaning a startup could participate
# only as a named project partner, not as lead.
#
# Intentionally general: covers energy networks, health, social housing, and
# local authority programmes rather than naming specific grants.  Used to
# reclassify "ineligible" scoring calls as partner-route, and as the fallback
# admission signal when the triage call is unavailable.
_REGULATED_ENTITY_LEAD_SIGNALS = [
    # Energy network regulators and programmes
    "ofgem",
    "strategic innovation fund",
    "network innovation",
    "distribution network",
    " dno ",
    " gdn ",
    "transmission operator",
    # Health sector
    " nhs ",
    "health trust",
    "integrated care",
    " ics ",
    "nhs foundation",
    # Social housing
    "registered social landlord",
    "housing association",
    "social housing decarboni",
    "warm homes",
    # Local authority / public sector
    "local authority",
    "local council",
    "city council",
    "county council",
    # Water
    "water company",
    "ofwat",
]


# Tokens that carry no identity when comparing grant names — generic funding
# vocabulary, round/year qualifiers, and connectives. Kept general on purpose.
_NAME_NOISE_TOKENS = {
    "the", "and", "for", "of", "a", "an", "to", "in",
    "grant", "grants", "fund", "funds", "funding", "programme", "program",
    "scheme", "call", "competition", "round", "phase", "wave", "cohort",
    "open", "2024", "2025", "2026", "2027",
    # generic category vocabulary — shared by unrelated programmes
    # ("Greentown Labs Climate Tech Accelerator" vs "Third Derivative (D3)
    # Climate Tech Accelerator" are different programmes)
    "accelerator", "climate", "tech", "technology", "incubator",
}


def _name_tokens(name: str) -> set[str]:
    """Normalise a grant name into a set of identity-bearing tokens."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    out = set()
    for t in tokens:
        if t in _NAME_NOISE_TOKENS or len(t) < 2:
            continue
        # light stemming so "partnership"/"partnerships" match
        out.add(t[:-1] if t.endswith("s") and len(t) > 3 else t)
    return out


# Funder/organisation tokens: shared between DIFFERENT programmes run by the
# same funder (e.g. "Innovate UK Smart Grants" vs "Innovate UK Energy
# Catalyst"), so overlap on these alone must not count as a duplicate.
_ORG_TOKENS = {
    "innovate", "uk", "eu", "european", "commission", "horizon", "europe",
    "government", "welsh", "wale", "scottish", "scotland", "ofgem", "nhs",
    "national", "eic", "eit", "ukri", "esa", "defra", "beis", "desnz",
    "sme",  # generic applicant-type acronym, not a programme identity
}


def _acronym_tokens(name: str) -> set[str]:
    """All-caps acronym tokens (3+ chars) from a raw grant name, lowercased.

    Programme acronyms like KTP or SIF are strong identity signals; funder
    acronyms (EIC, EIT, UKRI, ...) are filtered out via _ORG_TOKENS by the
    caller because different programmes share them.
    """
    return {
        t.lower()
        for t in re.findall(r"\b[A-Z][A-Z0-9]{2,5}\b", name)
    }


def _names_similar(a: str, b: str) -> bool:
    """
    True if two grant names likely refer to the same programme.

    Combines the original substring-containment check with token-overlap
    containment, which catches near-duplicates with different qualifiers —
    e.g. "Innovate UK Knowledge Transfer Partnership (KTP) — Round 4" vs.
    "Knowledge Transfer Partnerships (KTP) — Accelerated KTP 6".
    """
    al, bl = a.lower(), b.lower()
    if len(al) > 4 and len(bl) > 4 and (al in bl or bl in al):
        return True

    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False

    # Names carrying DIFFERENT funder identities are different programmes
    # even when the rest of the name (or an acronym) collides — e.g. "Ofgem
    # Strategic Innovation Fund (SIF)" vs "Innovate UK Sustainable
    # Innovation Fund (SIF)".
    org_a, org_b = ta & _ORG_TOKENS, tb & _ORG_TOKENS
    if org_a and org_b and org_a.isdisjoint(org_b):
        return False

    # Shared programme acronym (KTP, SIF, ...) is a strong identity signal —
    # catches "Knowledge Transfer Partnerships (KTP)" vs "Innovate UK KTP".
    # Funder acronyms (EIC, EIT, ...) are excluded: different programmes
    # legitimately share them (EIC Accelerator vs EIC Pathfinder).
    if (_acronym_tokens(a) & _acronym_tokens(b)) - _ORG_TOKENS:
        return True

    shared = ta & tb
    # Most of the shorter name's identity tokens must appear in the longer
    # name, AND at least one shared token must be programme-specific (not
    # just a common funder name).
    ratio_ok = len(shared) / min(len(ta), len(tb)) >= 0.6
    discriminative = any(t not in _ORG_TOKENS for t in shared)
    return ratio_ok and discriminative


def _rescue_missing_partner_items(
    shortlist: list,
    opportunities: list,
    watchlist: list,
    longlist: list | None = None,
) -> list:
    """
    Safety net: for every item that was shortlisted (and therefore researched)
    but is completely absent from both output arrays, add a minimal watchlist
    entry so it is not silently lost.

    The scoring model sometimes drops shortlisted items entirely rather than
    routing them to either array — particularly when it cannot confidently
    classify a programme.  This function catches those omissions.

    Also sweeps the full discovery longlist so items that never made the
    top-8 shortlist (e.g. partner-route programmes like Ofgem SIF, or
    programmes like KTP that score lower than 8 stronger candidates in a
    given run) are not silently lost either.

    TWO-TIER rescue applied to BOTH shortlist and longlist:

    Tier 1 — regulated-entity-led programmes (partner-route note):
      If the item matches _REGULATED_ENTITY_LEAD_SIGNALS (energy networks,
      NHS, social housing, local authority, water), the why_watchlist note
      explains the partner-route angle specifically.

    Tier 2 — all other items with thematic fit ≥ 3:
      Any other item that was scored as at least partial fit at discovery but
      is absent from output gets a generic "verify eligibility" note.  This
      prevents high-relevance programmes (e.g. KTP) from being silently
      dropped when the scoring model fails to include them.

      Items with initial_thematic_fit < 3 are NOT rescued — a low discovery
      fit score suggests the model may have correctly excluded them.
    """
    # Names already present in either output array. Rescued names are added
    # as we go so two near-duplicate longlist entries can't both be rescued.
    present_names = [
        (o.get("name") or "")
        for o in opportunities + watchlist
        if o.get("name")
    ]

    def _already_present(name: str) -> bool:
        return any(_names_similar(name, present) for present in present_names)

    # Hard-exclusion filters — applied in Tier 2 to avoid rescuing known-bad items
    _HARD_EXCLUDE_NAMES = [
        "pathfinder",        # EIC Pathfinder: TRL 1–4 only — always wrong for commercial startups
        "energy catalyst",   # Innovate UK Energy Catalyst: ODA-only geography
    ]
    _HARD_EXCLUDE_NOTES = [
        "oda", "developing countr", "sub-saharan", "south asia", "indo-pacific",
        "trl 1-4", "trl 1–4", "trl 1 to 4", "trl 1-3", "trl 1–3",
        "breakthrough science", "visionary research",
        "academic only", "universities only", "research institution only",
    ]

    # Sweep shortlist first, then non-shortlisted longlist items.
    # Deduplication via _already_present prevents double-entries.
    shortlist_names_lower = {(o.get("name") or "").lower() for o in shortlist}
    non_shortlisted = [
        item for item in (longlist or [])
        if (item.get("name") or "").lower() not in shortlist_names_lower
    ]
    candidates = list(shortlist) + non_shortlisted

    rescued = []
    for item in candidates:
        name          = item.get("name") or ""
        managing_body = item.get("managing_body") or ""
        notes         = item.get("notes") or ""

        if not name or _already_present(name):
            continue

        searchable = (name + " " + managing_body + " " + notes).lower()
        fit = item.get("initial_thematic_fit", 0)
        was_shortlisted = name.lower() in shortlist_names_lower

        # Tier 1: regulated-entity-led signals → partner-route note
        if any(sig in searchable for sig in _REGULATED_ENTITY_LEAD_SIGNALS):
            if was_shortlisted:
                why = (
                    "This programme was researched and shortlisted but was not included "
                    "in the scored output. It likely requires a licensed or regulated "
                    "entity (e.g. network operator, NHS trust, housing association, or "
                    "local authority) as lead applicant. The startup may be able to "
                    "participate as a named technology partner under an eligible lead. "
                    "Verify current eligibility and identify a suitable lead organisation."
                )
            else:
                why = (
                    "This programme was identified during discovery but not deep-researched. "
                    "It likely requires a licensed or regulated entity (e.g. network operator, "
                    "NHS trust, housing association, or local authority) as lead applicant. "
                    "The startup may be able to participate as a named technology partner. "
                    "Verify current eligibility and identify a suitable lead organisation."
                )
            rescued.append({
                "name":              name,
                "managing_body":     managing_body,
                "geography":         item.get("geography", ""),
                "opportunity_type":  item.get("opportunity_type", ""),
                "application_route": item.get("application_route", ""),
                "application_timing": item.get("application_timing", "timing_unknown"),
                "status":            item.get("status", ""),
                "application_link":  item.get("application_link", "unknown"),
                "link_type":         item.get("link_type", "unknown"),
                "funding_type":      item.get("funding_type", ""),
                "max_funding":       item.get("max_funding", "unknown"),
                "thematic_relevance": item.get("initial_thematic_fit_reason", ""),
                "why_watchlist":     why,
                "what_would_unlock": (
                    "Identify a licensed or regulated organisation (network operator, "
                    "NHS trust, housing association, or local authority) willing to act "
                    "as lead applicant, with the startup named as the technology partner."
                ),
                "_rescued_by_safety_net": True,
            })
            present_names.append(name)
            continue

        # Tier 2: items with thematic fit ≥ 3 — skip known hard mismatches
        if any(sig in name.lower() for sig in _HARD_EXCLUDE_NAMES):
            continue
        if any(sig in (notes + searchable) for sig in _HARD_EXCLUDE_NOTES):
            continue

        if fit >= 3:
            if was_shortlisted:
                why = (
                    "This programme was researched and shortlisted but was not included "
                    "in the final scored output. Verify eligibility directly — it may "
                    "have application requirements or eligibility constraints that the "
                    "scoring model could not confidently resolve."
                )
            else:
                why = (
                    "This programme was identified during discovery but was not selected "
                    "for deep research. It showed partial thematic fit — verify eligibility "
                    "and application process directly."
                )
            rescued.append({
                "name":              name,
                "managing_body":     managing_body,
                "geography":         item.get("geography", ""),
                "opportunity_type":  item.get("opportunity_type", ""),
                "application_route": item.get("application_route", ""),
                "application_timing": item.get("application_timing", "timing_unknown"),
                "status":            item.get("status", ""),
                "application_link":  item.get("application_link", "unknown"),
                "link_type":         item.get("link_type", "unknown"),
                "funding_type":      item.get("funding_type", ""),
                "max_funding":       item.get("max_funding", "unknown"),
                "thematic_relevance": item.get("initial_thematic_fit_reason", ""),
                "why_watchlist":     why,
                "what_would_unlock": (
                    "Review the programme's eligibility criteria and application process "
                    "directly to determine whether the startup can apply."
                ),
                "_rescued_by_safety_net": True,
            })
            present_names.append(name)

    return watchlist + rescued


# ---------------------------------------------------------------------------
# Public entry points — split into two generators for the review gate
# ---------------------------------------------------------------------------


async def run_phase1(
    url: str | None,
    extra_text: str | None,
    preferences: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Phase 1 only: assemble company context, run company research.

    Ends with:
      {"type": "profile_ready", "profile": {...}, "source_note": "..."}

    The caller (main.py) attaches a job_id to this event before forwarding
    it to the browser.
    """
    from scraper import fetch_company_context

    preferences = preferences or {}
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def _pipeline() -> None:
        stage_name = "company context"
        try:
            # ── Fetch / assemble company context ─────────────────────────
            def _on_status(msg: str) -> None:
                queue.put_nowait({"type": "progress", "stage": 1, "message": msg})

            context, source_note = await fetch_company_context(
                url=url or None,
                extra_text=extra_text or None,
                on_status=_on_status,
            )

            # ── Company research (external searches) ──────────────────────
            stage_name = "company research"
            await queue.put({
                "type": "progress", "stage": 1,
                "message": "Researching company externally…",
            })

            def _on_s1(query: str, n: int) -> None:
                queue.put_nowait({"type": "progress", "stage": 1,
                                  "message": f'Searching: "{query}"'})

            profile = await _analyse_company(
                context, source_note, url, on_search=_on_s1
            )

            # Preserve the original URL in the profile so Phase 2+3 can use it
            profile["_source_url"] = url or ""

            await queue.put({
                "type": "profile_ready",
                "profile": profile,
                "source_note": source_note,
            })

        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "error", "message": f"[{stage_name}] {exc}"})
        finally:
            await queue.put(None)

    asyncio.create_task(_pipeline())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield item


async def run_phase23(
    profile: dict,
    preferences: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Phases 2 + 3: grant discovery, deep research, and scoring.

    Takes the (possibly user-edited) company profile from Phase 1 and
    runs the full grant analysis.  Ends with {"type": "complete", ...}.
    """
    preferences = preferences or {}
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def _pipeline() -> None:
        stage_name = "grant discovery"
        try:
            # ── Stage 2: Grant discovery ──────────────────────────────────
            await queue.put({
                "type": "progress", "stage": 2,
                "message": "Searching for grant opportunities…",
            })

            def _on_s2(query: str, n: int) -> None:
                queue.put_nowait({"type": "progress", "stage": 2,
                                  "message": f'Searching: "{query}"'})

            longlist = await _discover_opportunities(profile, preferences, on_search=_on_s2)

            # Rank by thematic fit; break ties by actionability so that with a
            # wide longlist the deep-research slots go to items the startup
            # could act on directly: an open call beats a recurring programme,
            # and a direct-application candidate beats a likely partner-route
            # one (partner-route items reach the watchlist without deep
            # research, via triage).
            def _shortlist_key(item: dict) -> tuple:
                fit = item.get("initial_thematic_fit") or 0
                status = (item.get("status") or "").lower()
                status_rank = 2 if status.startswith("open") else (
                    1 if status.startswith("recurring") else 0
                )
                direct = 0 if item.get("likely_partner_route") else 1
                return (fit, direct, status_rank)

            sorted_list = sorted(longlist, key=_shortlist_key, reverse=True)
            shortlist = sorted_list[:SHORTLIST_SIZE]

            await queue.put({
                "type": "progress", "stage": 2,
                "message": (
                    f"Discovery complete — {len(longlist)} opportunities found. "
                    f"Shortlisting top {len(shortlist)} for deep research…"
                ),
            })

            # ── Stage 3: Deep research + scoring ─────────────────────────

            # Shared retry-progress helper — sends a visible message to the UI
            # before each retry attempt so the user knows the tool is recovering
            # rather than stuck.
            def _on_retry(attempt: int, exc: Exception, delay: int, label: str) -> None:
                queue.put_nowait({
                    "type": "progress", "stage": 3,
                    "message": (
                        f"Network error during {label} — "
                        f"retrying in {delay}s (attempt {attempt + 1} of 3)…"
                    ),
                })

            stage_name = "research query generation"
            await queue.put({"type": "progress", "stage": 3,
                             "message": "Generating research queries for shortlisted grants…"})

            queries = await _with_retry(
                lambda: _generate_research_queries(shortlist),
                on_retry=lambda a, e, d: _on_retry(a, e, d, "query generation"),
            )

            stage_name = "deep grant research"
            await queue.put({"type": "progress", "stage": 3,
                             "message": "Executing targeted research searches…"})

            def _on_s3(query: str, n: int) -> None:
                queue.put_nowait({"type": "progress", "stage": 3,
                                  "message": f'Researching: "{query}"'})

            evidence, apply_evidence = await _execute_research_queries(queries, on_search=_on_s3)

            stage_name = "scoring"
            await queue.put({"type": "progress", "stage": 3,
                             "message": (
                                 f"Applying full scoring rubric to all {len(shortlist)} shortlisted "
                                 "grants… (this step takes 2–3 minutes)"
                             )})

            def _on_scoring_progress(chars: int) -> None:
                queue.put_nowait({"type": "progress", "stage": 3,
                                  "message": f"Scoring in progress… ({chars:,} characters generated so far)"})

            # Scoring is the most network-intensive call (2-4 min streaming
            # response).  Wrap with retry so a brief network hiccup doesn't
            # force the user to restart the whole analysis from scratch.
            result = await _with_retry(
                lambda: _score_with_evidence(
                    profile, shortlist, evidence, preferences,
                    on_progress=_on_scoring_progress,
                ),
                on_retry=lambda a, e, d: _on_retry(a, e, d, "scoring"),
            )
            result["company_profile"] = profile

            # ── Hard routing enforcement ──────────────────────────────────
            # The scoring model sometimes ignores its own routing rules.
            # Enforce them in code as a post-processing safety net.
            result["opportunities"], result["strategic_watchlist"] = (
                _enforce_routing_rules(
                    result.get("opportunities", []),
                    result.get("strategic_watchlist", []),
                    profile=profile,
                )
            )

            # ── Partner-route rescue ──────────────────────────────────────
            # The scoring model sometimes omits partner-route programmes from
            # BOTH output arrays (by incorrectly using applicant_type_match=
            # "ineligible" and then following the EXCLUDE ENTIRELY rule).
            # Recover any shortlisted grant that (a) matches known partner-
            # route signals and (b) is absent from both output arrays.
            result["strategic_watchlist"] = _rescue_missing_partner_items(
                shortlist,
                result.get("opportunities", []),
                result.get("strategic_watchlist", []),
                longlist=longlist,
            )

            # ── Specificity validation ────────────────────────────────────
            # Use query_apply search evidence to confirm each main recommendation
            # actually has a specific, named application process.  Grants that
            # fail are demoted to the strategic watchlist; confirmed grants may
            # have their application_link updated to a more specific URL.
            stage_name = "specificity validation"
            opps  = result.get("opportunities", [])
            watch = result.get("strategic_watchlist", [])
            if opps:
                await queue.put({"type": "progress", "stage": 3,
                                 "message": (
                                     f"Checking {len(opps)} opportunities for confirmed "
                                     "application processes…"
                                 )})
                opps, watch = await _with_retry(
                    lambda: _validate_application_specificity(opps, watch, apply_evidence),
                    on_retry=lambda a, e, d: _on_retry(a, e, d, "specificity validation"),
                )
                result["opportunities"]       = opps
                result["strategic_watchlist"] = watch

            # ── Watchlist link resolution ─────────────────────────────────
            # Rescued watchlist items were never deep-researched — their URL
            # (if any) came from a discovery search snippet and was never
            # classified. Run a bounded search pass to find and classify
            # their official pages so the watchlist is actionable too.
            stage_name = "watchlist link resolution"
            n_missing = sum(
                1 for item in watch
                if item.get("link_type", "unknown") == "unknown"
                or not (item.get("application_link") or "").startswith("http")
            )
            if n_missing:
                await queue.put({"type": "progress", "stage": 3,
                                 "message": (
                                     f"Finding official links for "
                                     f"{min(n_missing, _MAX_WATCHLIST_LINK_SEARCHES)} "
                                     "watchlist items…"
                                 )})
                watch = await _resolve_watchlist_links(watch)
                result["strategic_watchlist"] = watch

            # ── Link verification ─────────────────────────────────────────
            stage_name = "link verification"
            n_checkable = sum(
                1 for item in opps + watch
                if (item.get("application_link") or "").startswith("http")
            )
            if n_checkable:
                await queue.put({"type": "progress", "stage": 3,
                                 "message": f"Verifying {n_checkable} application links…"})
                opps, watch = await _verify_application_links(opps, watch)
                result["opportunities"]      = opps
                result["strategic_watchlist"] = watch

            # ── Link quality gate ─────────────────────────────────────────
            # A "Must Pursue" / "Quick Win" item with a broken or generic
            # link is a contradiction — demote it and explain why.
            stage_name = "link quality gate"
            result["opportunities"] = _apply_link_quality_gate(
                result.get("opportunities", [])
            )

            # ── Acronym definitions ───────────────────────────────────────
            # Compile the acronyms/abbreviations used across the final rows
            # so the XLSX export can render its "Definitions acronyms" tab.
            # Non-fatal: the exporter has a static fallback dictionary.
            stage_name = "acronym definitions"
            await queue.put({"type": "progress", "stage": 3,
                             "message": "Compiling acronym and abbreviation definitions…"})
            result["acronym_definitions"] = await _generate_acronym_definitions(
                result.get("opportunities", [])
            )

            await queue.put({"type": "complete", "result": result})

        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "error", "message": f"[{stage_name}] {exc}"})
        finally:
            await queue.put(None)

    asyncio.create_task(_pipeline())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
