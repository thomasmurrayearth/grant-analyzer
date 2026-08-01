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

## Where we are now (last updated 2026-08-01)

**The improvement loop can finally see.** As of 1 August 2026 a GitHub
Actions job snapshots `/admin/quality.json` every morning at 07:00 UTC and
commits it to `reports/snapshots/`. The fortnightly review reads those files
rather than calling the live app, which is what made cycles 1 and 2 blind.
One action is outstanding for Thomas: add `ADMIN_TOKEN` as a GitHub
repository secret (LAUNCH_SETUP.md §3). Until that is done the workflow runs
and fails, loudly, every morning. Also on 1 August the results email was
removed — it had never sent a single message, because no mail provider was
ever configured.


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

**The Supabase migration has been run — 13 July 2026.** `supabase_schema.sql`
was applied in the Supabase SQL editor and measurement is confirmed live:
`/admin/stats` shows real funnel, usage, and cost data (a test run logged at
$0.57). Feedback, funnel events, waitlist emails, and cost-per-run are all
being recorded, so the weekly report, the pricing gates in §6 of the launch
plan, and the WP-1 acceptance criterion ("all events visible in Supabase")
are all unblocked. Everything else in WP-1 is done except the custom domain
(C1), which is deliberately deferred until the soft launch proves traction.

> **Corrected 30 July 2026.** This entry previously stated the migration was
> unrun and blocking. That was wrong: it was applied on 13 July, as recorded
> in the launch plan's WP status table and corroborated by the $0.57 cost
> figure in `reports/digest-2026-07-20.md`, which only exists in a migrated
> schema. The stale claim misled subsequent sessions.

**Next work packages** (per the plan's sequencing): WP-3 (explainer video
script + assets) can start now; then the Phase 1 soft launch, then WP-4
(launch asset pack).

**The app now measures its own output quality (30 July 2026).** Usage was
already instrumented; output was not. `/admin/quality.json` returns every
completed analysis in a window already scored by `quality.py`, and a
fortnightly review cycle (the `grant-analyzer-health-check` skill, scheduled
for the 1st and 15th) reads it, trends it in `reports/quality-history.json`,
and brings Thomas proposals. When a fortnight passes with no completed user
run — and only then — the app runs its two ground-truth companies twice each
so the cycle is never blind. Baseline as at cycle 0: recall 0.92, exclusion
accuracy 0.90, structural precision 0.76, shortlist 4–5 against a promised
10, convergence 0.21. Those numbers are the standard the next cycle has to
beat.

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

**2026-08-01 — The review reads a committed file instead of calling the live app**
Commit subject: *Snapshot quality daily from CI; drop the results email*.
Two improvement cycles in a row produced no measurement at all. Cycle 1 was
blind because the free-tier database had paused for inactivity; cycle 2 was
blind because the review session could not reach the app — its fetch tool
only accepts URLs it has been handed, and the admin URL carries a token that
must never be pasted into a prompt. Different causes, one shared root: the
reviewer depended on making a live authenticated network call at the moment
of review. After two cycles the loop had never once verified that a change it
recommended actually worked. The fix is the ordinary one that scheduled
reporting has always used — a CI job holds the secret and commits the data.
`.github/workflows/quality-snapshot.yml` runs daily at 07:00 UTC, reads
`ADMIN_TOKEN` from GitHub Actions secrets, fetches `/admin/quality.json` and
`/health?deep=1`, and commits `reports/snapshots/quality-YYYY-MM-DD.json`. The
review now reads a file: no network, no token, no secret in any prompt. Three
properties earn their keep. A failed fetch is *still committed*, with its HTTP
status and the unauthenticated health payload, so an outage becomes data and
the day it started is visible rather than a gap. The job commits every day,
and a commit is the only thing GitHub counts as activity against its 60-day
auto-disable rule for scheduled workflows — so the mechanism cannot be
switched off by the quiet fortnights it exists to cover, which is exactly the
trap cycle 1 fell into. And the run exits non-zero on a bad snapshot, so
GitHub emails the owner: the alarm that was missing both times measurement
died silently. `snapshots.py` is the offline read library (`health_report`,
`staleness_days`, `series`, `movement`) and `eval/read_snapshots.py` the CLI,
which exits non-zero when the newest usable snapshot is missing or more than
two days old — so a blind cycle is caught in the first thirty seconds instead
of after an hour. Staleness is treated as the primary hazard throughout:
reporting an old snapshot as current is worse than reporting nothing, because
it manufactures confidence that isn't there. Requires a one-time step from
Thomas: add `ADMIN_TOKEN` as a GitHub repository secret (LAUNCH_SETUP.md §3).
31 new tests; 218 green.

**2026-08-01 — The results email is gone, because it never worked**
Same commit. The landing page told people *"Email me my results when they're
ready. Analysis takes 7–10 minutes — you can safely lock your screen."* The app
sent nothing, to anyone, ever: `RESEND_API_KEY` and `RESEND_FROM_EMAIL` were
never set in Railway, were never listed in LAUNCH_SETUP.md among the variables
to set, and `email_sender.py` returned silently when either was missing — the
same swallow-the-failure pattern that hid the database outage. Anyone who left
an address and locked their phone got nothing back. Removed at Thomas's
instruction rather than fixed: results appear in the page, and web push (which
is configured and does work) is the notification path for a locked screen. The
address field stays, but now only for the grant-deadline digest, and the
address is stored *only* when that consent is given — with the results-delivery
purpose gone, an address typed without ticking the box has no purpose to be
kept for. Capacity and waitlist copy now promise a human getting in touch
rather than an automated send. `email_sender.py` is a tombstone pending
`git rm` (this environment cannot delete files). `tests/test_app.py` gains a
`NoResultsEmailTest` class so the promise cannot return by accident.

**2026-07-30 — The app now measures its own output quality, on a fortnight's cycle**
Commit subject: *Measure output quality: scoring library, quality endpoint, fallback self-benchmark*.
Until now nothing produced evidence about whether the analyses people
actually received were any good. `/admin/stats` answered "did anyone use
it"; the launch plan (§9a) is explicit that the two most damaging failure
modes — output that can't be trusted, and output no better than five
minutes with a chatbot — leave no trace in usage counts. Four pieces close
that gap. `quality.py` defines every measure once (recall and exclusion
accuracy against ground truth; structural precision, tier spread, link
quality and shape for runs with no ground truth; convergence between
repeat runs at both exact-name and programme-family level). `eval/cases.py`
lifts the ground-truth cases out of the eval harness so the app, the
harness and the scorer share one definition of the right answer.
`/admin/quality.json` hands the fortnightly review every completed
analysis in the window, already scored, plus feedback, funnel, economics
and an explicit list of what could not be seen. `benchmark.py` runs the
two ground-truth companies twice each — **but only when a fortnight passed
with no completed real user run**, because live usage is better evidence
and costs nothing; the decision, taken or not, is written to the events
table so a quiet fortnight is never ambiguous between "suppressed by
design" and "the scheduler never fired". Benchmark runs are excluded from
every usage and economics figure. 90 new tests; 153 green in total.

**2026-07-31 — Three approved output-quality gates, and the volume gap diagnosed**
Commit subject: *Gate recommendations on eligibility, working links and funder-owned sources*.
Thomas approved proposals 1, 2 and 5 from improvement cycle 1 and asked for a
diagnosis on 3. All three gates land in one new stage,
`_apply_recommendation_gates`, which runs after link verification and is the
last thing between the scoring output and the user. A main recommendation is
an instruction — *go and apply for this* — and four conditions make that
instruction false: no confirmed application process, a deadline already
passed, a link broken on two checks, or a link that isn't the funder's.
Nothing is deleted; failures move to the watchlist carrying the reason,
because "real but not yet actionable" is what the watchlist is for.

Two things are worth recording about how this was built.

**The eligibility gate no longer names funders.** The previous version matched
two programmes by name, which meant it could only catch the two that had
already caused trouble — and it broke the repo's own rule against hardcoding a
funder seen in testing. Reading the stored runs showed the model reliably
*states* the disqualifying fact and then ignores it: it records `trl_match:
true` and explains at length that the company is "significantly beyond the
intended stage". So the gate now reads the concession, and separately checks
declared development-assistance restrictions against the company's actual
geographies. Both describe classes of restriction, so they catch funders
nobody has seen. Tests rewritten around invented programmes to prove it.

**The link-authority classifier was wrong first, and replay caught it.**
Before shipping, it was replayed over all seventeen stored runs. The first
version demoted `gov.uk`, `gov.wales`, `ukri.org` and Innovate UK's own
delivery-partner domains — false demotions of legitimate government
programmes, which would have been worse than the defect being fixed. Three
real bugs: a suffix list that missed a host equal to the suffix, only the
first organisation in a "Delivery Agency / Parent Department" body being
considered, and initialisms that keep a short word whole ("IUK") not being
generated. Fixed and pinned with regression tests. Demotion across stored runs
fell from 32% to 15% — nine broken links and two genuinely non-authoritative
sources, which is the real defect rate.

**On proposal 3 (ten promised, three delivered):** diagnosed in
`reports/diagnosis-shortlist-volume-2026-07-31.md`. Ten is not a target the
pipeline aims at — it is the batch size handed to scoring, after which four
stages remove items and nothing puts any back. The honest gap is that only the
first and last counts were ever recorded, so which stage loses the most is
unknown. Stage-by-stage counters (`pipeline_funnel`, surfaced in
`/admin/quality.json`) now ship with every run, and the recommendation is to
let the 15 August cycle name the guilty stage before spending money inflating
the input. Also noted: `WATCHLIST_CAP = 10` is declared and never used, which
is why watchlists run to 27 rows.

**2026-07-30 — ⚠ Analytics had silently stopped: the database was paused**
Commit subjects: *Surface analytics outages instead of swallowing them* ·
*Keep the analytics database awake*.
Verifying the new quality endpoint against production turned up a live
fault: `/admin/stats` returned **503 Database unavailable** and the
Supabase hostname in `SUPABASE_URL` did not resolve from a browser either.
The cause was **the free-tier project being paused for inactivity** — a
paused project's hostname stops resolving entirely, which reads exactly
like a deleted one. Resumed from the Supabase dashboard on 30 July; all
data, backups and storage were retained and the project reference, URL and
key are unchanged, so no Railway variable and no schema re-run were
needed.

The app itself was never affected: analyses ran and users saw nothing
wrong, because every database write is deliberately swallowed so an
analytics problem can't break a run someone is waiting on. The cost of
that design is exactly what happened — the app looked healthy while
recording nothing, with no signal until someone went looking.

**The trap worth naming:** the pause is triggered by inactivity, and this
app is quietest in precisely the fortnight the fallback self-benchmark
exists to cover. A silent fortnight pauses the database; a paused database
makes the benchmark refuse to spend money (correctly — it can't tell
whether real runs happened); so the improvement loop goes blind at exactly
the moment it was designed to step in. Three changes close that off:

* `benchmark.scheduler_loop` performs one indexed single-row read each
  pass, which is enough activity to prevent the pause and costs nothing
  worth counting. It runs whether or not benchmarking is enabled, because
  it protects all measurement rather than just benchmarking.
* `db.health()` does one cheap read and classifies the result — not
  configured, client unbuildable, credentials rejected, or unreachable —
  and names inactivity-pause as the first thing to check when a hostname
  stops resolving.
* `/health?deep=1` reports `degraded` and separates the pipeline from the
  database, while plain `/health` stays cheap and always 200 so a platform
  probe is never coupled to a third-party service. Both admin endpoints
  now carry the diagnosis in their 503 instead of a bare "unavailable".

Nothing logged can leak a connection string or key, and a test pins that.

**2026-07-30 — Baseline: what the numbers actually say today**
Scoring the sixteen stored Thermify runs and one German run through the
new library (`eval/score_stored.py`, offline, no API key) gives the first
honest reading of output quality. Across the five runs since 4 July:
recall against ground truth averages 0.92 but ranges 0.8–1.0; exclusion
accuracy is 0.90, meaning one run in five recommended a programme
confirmed ineligible; structural precision is 0.76, almost entirely from
broken application links; the shortlist holds 4–5 items against a promised
10; and convergence between same-company runs is 0.21 at exact-name level
and 0.25 at family level — the naming-variance gap is small, so the churn
is genuine retrieval churn rather than one programme wearing several
names. Recorded as cycle 0 in `reports/quality-history.json`. **Consistency
is being tracked as a diagnostic, never a target: the fix for churn is
better retrieval, never caching or pinning results.**

**2026-07-30 — Watchlist rows get a real Priority Score in the XLSX**
Thomas noticed that after the watchlist was folded into the XLSX (below),
its rows still showed "unknown" for Thematic Fit, Strategic Value, Ease of
Execution, and Priority Score — because watchlist items never go through
the full 3-axis scoring rubric, so those fields are usually genuinely
absent from the data, not just missing from the export. Two changes: (1)
`analyzer.py` now backfills the discovery-stage `initial_thematic_fit`
rating (computed for every longlist candidate, previously only used
internally to rank the shortlist and then discarded) onto any watchlist
item that lacks a real scored thematic fit, matching by name against the
longlist/shortlist (`_backfill_initial_thematic_fit`); (2) the XLSX export
now defaults Strategic Value and Ease of Execution to 0 (not "unknown")
whenever they're absent, so the Priority Score formula computes a real
number for any row with a thematic fit rating at all — scored or
discovery-stage. Only a grant with no rating whatsoever still shows
"unknown" and drops the formula. Frozen sample workbook rebuilt again to
match (most of its 21 watchlist rows predate this fix and have no
discovery-stage rating recorded, so they still show "unknown" — the 2 that
already carried a rating now get a live score).

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

- **Free-tier database pauses for inactivity** — resolved once on 30 July
  2026 (resumed from the dashboard, no data lost). The app now keeps it
  awake with one read per scheduler pass, but that only works while the
  app is deployed and running; a long Railway outage or a redeploy gap
  could still let it lapse. If `/health?deep=1` ever reports
  `"analytics_database": "unavailable"`, check the Supabase dashboard for
  a paused project before assuming anything worse. Upgrading off the free
  tier would remove the risk entirely and is a cost decision, not a
  technical one.
- **Ground truth covers two companies** — `eval/cases.py` holds Thermify
  (Wales, TRL 7–8, real) and a fictional German heat-pump company. Recall
  and exclusion accuracy are therefore directional rather than
  statistically meaningful, and both cases sit in one sector. Widening the
  bench is the single biggest improvement available to the measurement
  itself; it costs research time to write correct rules, and wrong ground
  truth is worse than none.
- **Real user runs carry no ground truth** — nobody wrote correct answers
  for a stranger's company, so those runs are scored structurally
  (defensible links, applicant-type match, tier spread, shape) and read by
  judgement. Structural precision is a floor on the defect rate, never a
  ceiling on quality: it cannot see a programme that is simply a poor fit.
- **Programme-family matching is heuristic** — `quality.FamilyResolver`
  learns abbreviations from the runs it is given rather than guessing
  them, and refuses ambiguous pairings, but it cannot guarantee two
  wordings of one programme meet. Read exact-level convergence as the
  conservative floor and family-level as the optimistic ceiling.
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
