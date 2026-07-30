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

## Where we are now (last updated 2026-07-30)

**Status: live in production, and now instrumented for launch.** The app
runs at https://grant-analyzer-production.up.railway.app/ on Railway,
auto-deployed from the `main` branch of this GitHub repository. It is a
working product past the prototype stage: the May 2026 output-quality
concerns (see `CONSULTANT_REVIEW.md`) have been substantially addressed
through the June–July quality overhauls, verified with the eval harness
in `eval/`.

As of 13 July 2026 the conversion and measurement layer from the launch
plan (work packages WP-1 and WP-2) is **deployed to production**: the
landing page shows what you get before you commit ten minutes (including
a live sample report at `/?demo=1`), the results page, the XLSX, and the
completion email all carry the consulting offer, users can rate the
shortlist, every funnel step and the API cost of every run are logged,
and per-IP/concurrency limits cap spend during a traffic spike. Verified
live after deploy (commits `d12c5cb`, `961b954`).

**⚠ Blocking the next step — the Supabase migration has not been run.**
`supabase_schema.sql` must be pasted into the Supabase SQL editor and run
(Thomas's action; instructions in `LAUNCH_SETUP.md`). Until then, feedback,
funnel events, waitlist emails, and cost-per-run are written and silently
dropped, so the app looks fine but produces **no measurement data** — and
the weekly report, the pricing gates in §6 of the launch plan, and the
WP-1 acceptance criterion ("all events visible in Supabase") all depend on
it. Everything else in WP-1 is done except the custom domain (C1), which
was always blocked on Thomas buying one.

**Next work packages** (per the plan's sequencing): WP-3 (explainer video
script + assets) can start now; then the Phase 1 soft launch, then WP-4
(launch asset pack).

As of 30 July 2026: the XLSX download now includes the strategic watchlist
alongside the scored shortlist (previously watchlist-only items were
invisible in the download even though they showed on the page), the header
carries a "Back to thomasmurray.earth" link since the app is reached from
that site, and the profile-review auto-continue timer moved from the
browser to the server so a run finishes grant discovery even if the user
closes the tab (the browser can still pre-empt it early or pause it while
editing). Service worker cache bumped to v5.

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

**2026-07-30 — XLSX shows the watchlist too, back-to-site link, server-side auto-continue**
The XLSX download only ever contained the scored shortlist, so anyone
downloading it lost the strategic watchlist items visible on the results
page; the "Grant Opportunities" tab now includes both (watchlist rows
sort below scored ones and show "unknown" instead of a score, since they
were never put through the 3-axis rubric), and the frozen sample workbook
was rebuilt to match. Since the app is reached via a link from
thomasmurray.earth, the header now carries a "← Back to
thomasmurray.earth" link back to it. Also converted the profile-review
auto-continue timer from a client-side countdown to a server-side one
(`/analyse/pause` lets an open browser hold it off while editing), so a
run's grant-discovery phase starts even if the user closes the tab —
closing the gap where a closed-tab run just stalled forever at the review
step. Service worker cache bumped to v5.

**2026-07-13 — Consulting offer reworded (Thomas's copy)**
Thomas rewrote the consulting call-to-action in his own voice ("Want help
winning a grant?… I help climate and deep tech startups win grant funding
and achieve profitability… a productised version of an opportunity
assessment I've done for several startups"). Applied identically in all
three places the offer appears — the results page, the completion email,
and the Strategic Recommendations tab of the XLSX — and the frozen sample
workbook was rebuilt to match.

**2026-07-13 — Sample report + "what you'll get" landing section (WP-2)**
Users were being asked to commit ten minutes blind. The landing page now
shows three cards explaining the output and links to a full sample report
at `/?demo=1` — a real analysis of a fictional climate hardware startup
(Kelvara Systems, seed-stage thermal storage, NZ/UK), frozen as static
JSON plus a downloadable sample XLSX. The demo renders through the live
result renderers, so it cannot drift from the real app.

**2026-07-13 — Conversion essentials: CTAs, feedback, funnel, cost, limits (WP-1)**
The launch plan's conversion layer. The results page, the XLSX (via a new
"Strategic Recommendations" tab), and the completion email now carry the
consulting offer, because the app is the lead magnet for the consulting
work and previously the only trace of Thomas was a `mailto:` link. Added:
a 1–5 feedback widget with optional comment and contact consent; funnel
event logging (landing views, CTA clicks, XLSX downloads, shares); token
and dollar cost logged per analysis, without which the pricing gates in
the plan cannot fire; social/OG meta plus a share image and a "share this
tool" button; an explicit newsletter consent checkbox; and per-IP daily
and concurrency limits that offer a waitlist instead of an error, turning
overload into list growth. The admin stats page now reports the whole
funnel, cost per run (mean and p90), feedback scores, and flags
**GATE TRIGGERED** when a pricing gate is met. The privacy policy was
updated to cover the new data. Requires the Supabase migration in
`supabase_schema.sql`.

**2026-07-12 — Dev container for safe Claude Code execution**
Replaced the minimal `.devcontainer` config with a full setup: Python 3.11
base, Claude Code extension + CLI preinstalled, port 8000 forwarded, API keys
passed from host env vars, and the launch-plan folder (Start-up consultant
project, containing `GRANT_ANALYZER_LAUNCH_PLAN.md`) bind-mounted at
`/workspaces/launch-plan`. Plain-English usage guide in
`.devcontainer/README.md`. Purpose: all launch-plan work packages can be
executed by Claude Code inside an isolated container.

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

- **⚠ Supabase migration not yet run (Thomas's action — blocks all launch
  analytics)** — the feedback, events, and waitlist tables and the cost
  columns exist in `supabase_schema.sql` but must be pasted into the
  Supabase SQL editor and run. Until then those writes fail and are logged
  as warnings; the analysis pipeline is unaffected by design, so the app
  looks healthy while capturing nothing. Check whether it has been done by
  loading `/admin/stats?token=…`: if the Funnel and Economics tiles are all
  dashes/zeros after real traffic, it hasn't. See `LAUNCH_SETUP.md`.
- **Custom domain still outstanding (C1 — Thomas's action)** — when it
  lands, change `APP_URL` in Railway, the four absolute URLs in the
  `<head>` of `frontend/index.html` (canonical, `og:url`, `og:image`,
  `twitter:image` — grouped together with a comment), and bump
  `CACHE_NAME` in `frontend/sw.js` (currently v5).
- **Lighthouse check not run** — the WP-1 acceptance criteria mention it;
  OG/meta tags are in place but no performance audit has been done.
- **Sample report is thin on scored opportunities** — the frozen sample
  run (Kelvara Systems) returned 4 scored grants but 21 watchlist items.
  That is a real, unedited output, not a bug, but it suggests the pipeline
  routes a lot of programmes to the watchlist for multi-geography hardware
  profiles. Worth investigating as an output-quality question, and worth
  re-running the sample (`frontend/sample/report.json` + the matching XLSX)
  if a better showcase is wanted. Thomas has seen it and not yet objected.
- **Watchlist table density** — the seven-column strategic watchlist gets
  very long with many items; could benefit from a condensed or
  card-based layout. Now visible in the sample report, where the
  watchlist runs to 21 rows.
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
