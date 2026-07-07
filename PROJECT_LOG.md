# Project Log — Grant Opportunity Analyser

A plain-English record of how this app has evolved, where it stands today,
and what is known to be outstanding. Newest entries first within each
section.

> **⚠ Keep this file up to date.** Every time a change is committed to this
> repository, add a dated entry to the Timeline below (what changed and
> why, in one to three sentences) and revise the "Current state" and "Open
> items" sections if they are affected. This applies to humans and AI
> assistants alike — a change is not finished until this log reflects it.

---

## Where we are now (last updated 2026-07-07)

**Status: live in production.** The app runs at
https://grant-analyzer-production.up.railway.app/ on Railway,
auto-deployed from the `main` branch of this GitHub repository. It is a
working product past the prototype stage: the May 2026 output-quality
concerns (see `CONSULTANT_REVIEW.md`) have been substantially addressed
through the June–July quality overhauls, verified with the eval harness
in `eval/`.

**What the app does:** a user describes their company (URL, pasted text,
or uploaded document) → the app researches the company, lets the user
review/correct the profile, searches the live funding landscape
(30 discovery searches), deep-researches a shortlist of 10, scores each
grant against a rubric, and returns a ranked list plus a strategic
watchlist and an XLSX export.

**Key architecture:** FastAPI backend (`main.py`) with background jobs
polled by the frontend; the analysis pipeline lives in `analyzer.py`
(Claude + DuckDuckGo searches); XLSX export in `exporter.py`; single-page
frontend in `frontend/index.html` (also installable as a PWA on Android,
iOS, and desktop); job logging to Supabase (`db.py`); email + web-push
notifications when results are ready.

## Timeline

**2026-07-07 — Project log introduced**
Created this file as the ongoing record of the app's evolution, plus
`CLAUDE.md` (instructions for AI assistants) and a README pointer, all
requiring a dated log entry with every committed change.

**2026-07-07 — Frontend redesign (`0d7a8a9`)**
Full visual overhaul away from the generic template look: earth palette
taken from thomasmurray.earth, Archivo/Instrument Sans/Spline Sans Mono
type system, new logo (rising green bars + bronze point), and a numbered
stage timeline with a live "what am I searching right now" ticker
replacing the terminal-style log (full log still available behind a
disclosure). The form now hides while an analysis runs. New PWA icon set
(incl. Android maskable and iOS touch icon) and service-worker cache v3
so installed apps pick up the redesign. Terms/Privacy pages restyled to
match.

**2026-07-07 — Search limits hidden from users (`6bd9ccf`)**
Progress messages no longer state how many searches will run or number
each search ("up to 30 searches", "Search 4/30"). They now just say what
is being searched. The underlying limits are unchanged.

**2026-07-07 — Analysis quality: partner routes, dedup, acronyms (`46cc0b9`)**
Programmes that require an eligible lead applicant are tagged
"partner_route" and routed to the watchlist instead of being wrongly
excluded; the longlist is deduplicated; searches carry a year qualifier;
an acronym/abbreviation definitions tab was added to the XLSX export,
which was also broadly reworked (real Excel dates, restyled sheets,
max-funding column).

**2026-07-05 — Consistency and link-quality overhaul (`87d5d3f`)**
Addressed the run-to-run variance and broken/vague-link problems flagged
in the consultant review: duplicate detection, hard eligibility gates,
application-link resolution and verification, and eval tooling to measure
variance across repeated runs (results in `eval/results/`).

**2026-06-09 — Resilience and notifications (`ab46428`…`e1b83dc`)**
Analyses became background jobs that survive a locked phone screen or
closed tab: the frontend polls job status and restores in-progress runs
on reload; users can leave an email or accept push notifications to be
told when results are ready. A 10-second auto-continue timer was added to
the profile review gate. Service-worker cache bumped (v2) and a fix so
the progress UI is never blocked by the push-permission dialog.

**2026-06-06 — Initial release (`bf0a507`)**
First working version of the Grant Opportunity Analyser: three-phase
pipeline (company research → grant discovery → deep research and
scoring), review gate for the company profile, scored and tiered results
table, strategic watchlist, XLSX export.

**May 2026 — Consultant review**
`CONSULTANT_REVIEW.md` captured the prototype's state and its persistent
output-quality issues (inconsistent results between runs, weak
application links, umbrella programmes mistaken for open calls). Note:
its pipeline numbers (20 discovery searches, shortlist of 15) are
superseded — the app now runs 30 discovery searches and deep-researches a
shortlist of 10.

## Open items and known limitations

- **Watchlist table density** — the seven-column strategic watchlist gets
  very long with many items; could benefit from a condensed or
  card-based layout.
- **Progress messages that still reveal internal caps** — "Shortlisting
  top 10 for deep research…" and "Applying full scoring rubric to all 10
  shortlisted grants…" still expose the shortlist size (search-count
  limits themselves are now hidden). Left as-is by decision on
  2026-07-07; revisit if desired.
- **Google Fonts loaded at runtime** — the redesign loads three font
  families from fonts.googleapis.com; self-hosting them would remove the
  external dependency.
- **Home-screen icon refresh** — Android may keep showing the old app
  icon until the PWA is reinstalled; nothing further to do in code.
- **In-memory job store** — jobs live in server memory; a Railway restart
  loses in-flight analyses (completed results are logged to Supabase).

## Standing decisions

- **Generalise, don't special-case:** prompt and safety-net fixes must
  describe *classes* of programmes/behaviour, never a specific company or
  programme seen in testing (e.g. never hardcode "Thermify" or one
  funder's name into logic).
- **Deploy = push to `main`:** Railway auto-deploys; roll back via the
  Railway dashboard (Deployments → redeploy an older entry). Tag
  `old-version` (`e1b83dc`) marks the pre-July version.
- **Verify before deploy:** exercise changes against the running app
  (unit tests in `tests/`, eval runs in `eval/` for output-quality
  changes, headless-browser screenshots for UI changes).
