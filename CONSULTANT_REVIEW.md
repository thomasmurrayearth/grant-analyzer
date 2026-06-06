# Grant Opportunity Analyser — Project Review for Consultant

**Prepared:** May 2026  
**Status:** Working prototype with persistent output quality issues  
**Purpose:** Independent review to identify root causes and a more efficient path to production-quality results

---

## 1. What the Tool Does

The Grant Opportunity Analyser is a web application that helps climate-tech and deeptech startups identify relevant grant funding opportunities. A user provides their company's website URL and optionally uploads documents or pastes text. The tool then:

1. **Researches the company** — scrapes the website, runs up to 5 external web searches, and uses Claude to synthesise a structured company profile (sector, TRL, geography, key themes, etc.)
2. **Shows a review gate** — presents the profile to the user, who can edit it before the search begins. TRL is editable here because it directly influences which grants are filtered in or out.
3. **Discovers grant opportunities** — runs 20 targeted DuckDuckGo web searches and uses Claude to compile a longlist of 10–25 candidates
4. **Deep-researches the shortlist** — takes the top 15 candidates by initial thematic fit, runs 3 targeted searches per grant (official details, applicant commentary, and specifically the application URL), producing ~45 searches of evidence
5. **Scores and routes** — a single long Claude call (2–4 minutes) applies a detailed rubric to classify each grant, assign it to main recommendations or a strategic watchlist, and score it on thematic fit, strategic value, and ease of application
6. **Validates specificity** — a post-scoring Claude call confirms that each main recommendation has a real, named application process (as opposed to being a generic funder page or programme umbrella)
7. **Verifies links** — parallel HTTP HEAD checks on all URLs in the output
8. **Returns results** — a prioritised shortlist with tier labels (Must Pursue / Big Bet / Quick Win / etc.), strategic watchlist, and strategic narrative, plus an XLSX export

The tool is built in Python (FastAPI backend, vanilla HTML/JS frontend), uses the Anthropic Claude `claude-sonnet-4-6` API, and requires no API keys beyond Anthropic's.

---

## 2. Architecture Overview

```
Browser (index.html)
    │
    │  POST /analyse          → SSE stream of progress + profile_ready event
    │  POST /analyse/continue → SSE stream of progress + complete event
    │  POST /extract          → file text extraction (PDF, DOCX, TXT)
    │  POST /download         → XLSX download
    ▼
FastAPI (main.py)
    │
    ├── scraper.py          fetch_company_context()
    │     ├── httpx direct URL fetch
    │     └── DuckDuckGo fallback if site is JS-rendered
    │
    └── analyzer.py         run_phase1() / run_phase23()
          ├── Phase 1:      _analyse_company()      (tool-use loop, ≤5 searches)
          ├── Phase 2:      _discover_opportunities()
          │     ├── _generate_discovery_queries()   (no-tool call)
          │     ├── _execute_discovery_queries()    (20 DuckDuckGo searches)
          │     └── _analyse_discovery_results()    (streaming, no tools)
          └── Phase 3:
                ├── _generate_research_queries()    (no-tool call, 3 queries/grant)
                ├── _execute_research_queries()     (~45 DuckDuckGo searches)
                ├── _score_with_evidence()          (streaming, no tools, 2–4 min)
                ├── _validate_application_specificity() (batch Claude call)
                └── _verify_application_links()    (parallel HTTP checks)
```

**Key design decisions:**
- **Separated research from reasoning.** All web searches are executed by Python (DuckDuckGo via `ddgs`), and the results are passed to Claude as context. Claude does not use tool calls during the scoring phase — this prevents runaway searching and guarantees output.
- **Two-phase with user review gate.** The company profile is presented to the user between Phase 1 and Phase 2+3, allowing corrections before expensive searches begin.
- **Streaming for long calls.** The scoring call (Phase 3c) can take 2–4 minutes generating 8,000+ tokens. Streaming avoids HTTP timeout issues.
- **Retry logic.** An `_with_retry` async utility wraps network-intensive steps (query generation, scoring, specificity validation) with exponential backoff (10s, 20s) and user-visible progress messages.
- **asyncio.Queue pipeline.** Results are passed between pipeline stages and the SSE response via an async queue, enabling real-time progress updates to the browser.

---

## 3. Current Status: What Is and Isn't Working

### Working reliably
- Company research (Phase 1) — consistently produces accurate, detailed profiles
- Pipeline architecture — SSE streaming, review gate, retry logic, XLSX export all function correctly
- Link verification — HTTP checks work correctly
- Tier classification logic — the priority tier rules are coherent and correctly implemented
- Strategic watchlist separation — grants with indirect/unclear application routes are correctly routed away from main recommendations
- Hard exclusions — grants with confirmed TRL mismatch or geographic mismatch are reliably excluded

### Not working reliably
- **Output quality is insufficient for practical use.** The most recent tested output (Thermify v2) returned only 2 main recommendations, both Low Priority poor fits, with the 3 genuinely good opportunities (Ofgem SIF, EIC Accelerator, Innovate UK Smart Grants) incorrectly placed in the strategic watchlist or missing entirely.
- **The tool is in a calibration cycle.** Each round of fixes that eliminates one class of false positive also eliminates true positives. Making filters stricter to remove Energy Catalyst (ODA-only fund, not relevant to a UK company) also caused Ofgem SIF (which Thermify has already received funding from) to be incorrectly excluded.
- **Link quality remains uncertain.** Even when a grant is correctly identified, the application link is often a programme overview page or funder homepage rather than a direct application portal.

---

## 4. Quality Problems — Root Cause Analysis

### 4.1 The core tension: conservative filtering vs. recall

The tool has two competing objectives that are currently in tension:

- **Precision**: Don't show grants that aren't genuinely relevant. This led to geographic exclusions, TRL exclusions, business model exclusions, and specificity validation.
- **Recall**: Don't miss the grants that the company should actually apply for.

Every fix added to improve precision has the potential to reduce recall. The specificity validation step — added to filter out generic search page links — was calibrated around "is there an active open call right now?" This correctly blocked inapplicable grants but also incorrectly blocked well-established recurring programmes (Ofgem SIF, Innovate UK Smart Grants, EIC Accelerator) when they were between rounds.

The fix applied (recalibrating around "has this programme ever run a public application process?") addresses the proximate cause but introduces a new risk: if the criterion is too permissive, it may allow back in some of the generic funder pages we were trying to exclude.

**The root issue:** We are calibrating a text classifier (Claude's scoring prompt) using only 2 test cases (Thermify v1 and v2). The signal-to-noise ratio in this debugging process is very low. Each run takes ~10 minutes and costs approximately $1–2 in API calls, making rapid iteration slow.

### 4.2 The specificity validation step is doing too much

The specificity validation step is currently asked to make a binary confirmed/not-confirmed decision that effectively overrides the scoring step. This means:

1. A grant can score 4/5 thematic fit, 4/5 strategic value, be rated "Must Pursue" — and then be silently demoted to the watchlist by specificity validation
2. There is no visibility into why a grant was demoted — the user only sees it in the watchlist
3. The step creates a hidden dependency where the quality of query_apply search results (a DuckDuckGo query that may return poor results for any specific programme) determines whether a genuinely relevant grant makes it to the user at all

### 4.3 Evidence truncation limits scoring quality

In Phase 3c (scoring), each grant's research evidence is truncated to 800 characters before being passed to Claude. The total input is approximately 800 × 15 grants = 12,000 characters of evidence, plus the company profile and 8,192 tokens of output capacity. This truncation means Claude is often scoring grants on a single sentence of evidence — a title and URL snippet — rather than the actual eligibility criteria, funding amounts, and application requirements.

### 4.4 The shortlisting step discards relevant grants

Phase 2 produces a longlist of 10–25 grants scored for initial thematic fit (1–5). Phase 3 only deep-researches the top 15 by initial fit score. If a highly relevant grant receives a low initial fit score (because the discovery search returns poor results, or because the grant name doesn't pattern-match well), it is permanently excluded from the deep research and scoring stages and will never appear in the output.

### 4.5 DuckDuckGo search quality is variable

All web searches use DuckDuckGo via the `ddgs` library with no API key. Search result quality is inconsistent — for some queries it returns highly relevant official programme pages; for others it returns news articles, generic funding portals, or nothing useful. Since the entire pipeline depends on web search quality, poor search results propagate through to poor outputs. There is no fallback for failed or low-quality searches.

---

## 5. The Full System Prompt Stack

The tool uses five distinct Claude calls, each with a dedicated system prompt:

| Step | Prompt | Purpose | Output |
|------|--------|---------|--------|
| Phase 1c | `_COMPANY_SYSTEM` | Synthesise company profile | JSON object (company profile) |
| Phase 2a | `_DISCOVERY_QUERY_SYSTEM` | Generate 20 discovery search queries | JSON array (query strings) |
| Phase 2c | `_DISCOVERY_ANALYSIS_SYSTEM` | Compile grant longlist from search results | JSON array (10–25 grants) |
| Phase 3a | `_QUERY_GENERATION_SYSTEM` | Generate 3 research queries per shortlisted grant | JSON array (query objects) |
| Phase 3c | `_SCORING_SYSTEM` | Apply full rubric, classify, route, score | Large JSON object |
| Post-3c | `_SPECIFICITY_VALIDATION_SYSTEM` | Confirm application process exists | JSON array (confirmed/not per grant) |

The `_SCORING_SYSTEM` prompt is the most complex, structured as three explicit steps:
- **Step 1 — Classify:** assigns `opportunity_type`, `application_route`, `has_application_process`, `application_timing`, `link_type`, `trl_match`, `geography_match`
- **Step 2 — Route:** hard exclusions (has_application_process false, trl_match false, geography_match false, specific excluded opportunity types) and watchlist routing
- **Step 3 — Score:** thematic fit with 8-question adversarial check, strategic value, ease of application, priority score formula, priority tier assignment

The output schema has been reordered so `executive_summary` and `strategic_recommendations` are generated before `opportunities` and `strategic_watchlist` — this prevents token truncation from silently omitting the strategic narrative.

---

## 6. What Has Been Tried and Rejected / Modified

| Problem | Fix Tried | Result |
|---------|-----------|--------|
| Generic search page links (competition/search) | HTTP link verification | Correctly identifies broken links but doesn't fix the root cause (wrong URL selected by Claude) |
| Generic funder homepage links | `link_type` classification + specificity validation | Reduced generic links but caused over-exclusion of valid recurring programmes |
| Energy Catalyst included for UK company | `geography_match` hard exclusion field | Works — Energy Catalyst correctly excluded |
| EIC Pathfinder included despite TRL mismatch | `trl_match` hard exclusion field | Works — TRL-incompatible grants now excluded |
| Heat network programme confusion | Adversarial check question 7 (applicant type) | Partially works — catches centralised vs distributed mismatch |
| Strategic recommendations truncated/missing | Moved schema fields earlier in output order | Works — strategic fields now consistently present |
| Network errors causing full restart | `_with_retry` with exponential backoff | Works — tool now recovers from transient network errors |
| TRL not visible at review gate | Added TRL input to review card | Works — user can now correct TRL before Phase 2 |
| Ofgem SIF, EIC Accelerator, Smart Grants in watchlist | Recalibrated specificity validation | Implemented but not yet tested against real run |

---

## 7. Deferred Items (Consultant Feedback from Earlier Review)

The following items were identified in an earlier consultant review but have not yet been implemented. They are listed here so the consultant is aware of the full picture:

- **Point 4 — Funding amount basis:** Add a `funding_amount_basis` field (per-project vs total-programme) to prevent total budget figures from being presented as per-applicant amounts
- **Point 5 — Reason for inclusion / caution:** Add explicit `reason_for_inclusion` and `reason_for_caution` fields to each opportunity so users understand the reasoning, not just the score
- **Point 6 — Confidence fields:** Add `application_process_confidence` and `funding_amount_confidence` fields so users can assess reliability of the data
- **Point 7 — Expanded user preferences:** Add in-kind support preference, partner-led preference, and minimum funding amount threshold to the initial preferences form

---

## 8. Known Risks

1. **Hallucinated grants.** If web search returns no results for a query, Claude may invent plausible-sounding grant names. The specificity validation step partially addresses this, but cannot fully prevent hallucination when search quality is poor.

2. **Stale data.** DuckDuckGo results reflect the current web index. Grant information may be out of date; deadlines may have passed; programmes may have closed. The tool surfaces what it finds but cannot guarantee currency.

3. **Token limit for scoring.** The scoring call uses `max_tokens=8192`. For a shortlist of 15 grants with detailed evidence, this is occasionally tight. The tolerant JSON parser recovers partial output, but very long responses may be truncated.

4. **DuckDuckGo rate limiting.** Under heavy use or repeated testing, DuckDuckGo may rate-limit requests. The `ddgs` library has built-in retry logic (5 attempts, increasing delays), but sustained rate limiting will degrade search quality silently.

5. **Anthropic API costs.** A full run costs approximately $1–2. At 15 shortlisted grants × 8,192 output tokens + all input context, the scoring call is the most expensive single step.

---

## 9. Recommended Areas for Consultant Focus

Based on the development history, the following are the highest-leverage areas for review:

### 9.1 Calibration methodology (highest priority)

The current approach is to fix issues one at a time based on individual test runs, then re-test. This is slow, expensive, and prone to introducing new problems. A more systematic approach would be:
- Define a ground-truth evaluation set: for 3–5 test companies, manually identify which grants they should and shouldn't receive as recommendations
- Run the tool against each test company and score precision and recall against ground truth
- Make prompt changes and measure the delta before shipping

Without this, the calibration loop will continue indefinitely.

### 9.2 Specificity validation architecture

The current specificity validation step is a binary gate that can silently discard valid grants. Consider replacing it with:
- A `specificity_confidence` score (high/medium/low) rather than a confirmed/not-confirmed binary
- Surface "medium confidence" grants to the user with a flag rather than hiding them in the watchlist
- Show the user why a grant was demoted, so they can make an informed decision

### 9.3 Evidence quality and quantity

800 characters of evidence per grant is too little for confident scoring. Options:
- Increase truncation limit (trade-off: larger input context, higher cost, slower)
- Use Claude's extended thinking mode for the scoring call to improve reasoning quality on limited evidence
- Add a step that specifically extracts eligibility criteria from official URLs before scoring (structured extraction rather than raw snippet passing)

### 9.4 Search quality

DuckDuckGo is free and requires no API key, but its quality is variable. For production use, consider:
- Bing Search API or Google Custom Search API (paid, more reliable)
- Using a specialist grants database or API (e.g. Innovate UK's IFS API, Grants.gov for US grants) as a primary source, with web search as a supplement
- Caching search results to allow re-scoring without re-searching (particularly useful during development/testing)

### 9.5 Deferred feedback items

Points 5 and 6 from the earlier consultant feedback (reason for inclusion/caution, confidence fields) would significantly improve the tool's practical usefulness and should be prioritised in the next development round. They are straightforward prompt additions that do not require architectural changes.

---

## 10. File Reference

| File | Purpose |
|------|---------|
| `analyzer.py` | Core pipeline: all system prompts, Claude API calls, search execution, pipeline orchestration |
| `main.py` | FastAPI routes: /analyse, /analyse/continue, /extract, /download |
| `scraper.py` | Company website fetch + DuckDuckGo fallback |
| `exporter.py` | XLSX export (multi-tab workbook) |
| `frontend/index.html` | Single-page application (vanilla HTML/JS/CSS) |
| `.env` | API key storage (not committed to version control) |
| `requirements.txt` | Python dependencies |

All business logic and prompt engineering lives in `analyzer.py`. The system prompts are module-level string constants at the top of that file. The pipeline orchestration functions (`run_phase1`, `run_phase23`) are at the bottom of the file.

---

## 11. How to Run the Tool

```bash
# Install dependencies
pip install -r requirements.txt

# Set API key
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Start the server
uvicorn main:app --reload

# Open browser
# http://localhost:8000
```

The tool is self-contained and requires no database, no external services beyond Anthropic's API and internet access for web searches.

---

*This document was prepared to support an independent review of the project. All code is in the files listed in Section 10. The most recent round of fixes (specificity validation recalibration, geography match hard exclusion, recurring programme handling) has been implemented in `analyzer.py` but has not yet been tested against a full production run.*
