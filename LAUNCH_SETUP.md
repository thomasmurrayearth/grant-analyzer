# Launch setup — the bits only Thomas can do

Everything in work packages WP-1 and WP-2 of the launch plan is built and
deployed. Three things need a human with passwords. None of them break the app
if you don't do them — the app keeps working, you just don't get the data.

---

## 1. Run the database migration (5 minutes) — ✅ **DONE 13 July 2026**

Confirmed working via `/admin/stats`: funnel, usage, and cost data are flowing.
Nothing further needed here. The instructions below are retained only in case the
schema ever needs re-applying — it is safe to re-run.

The feedback, funnel, and cost tracking write to three Supabase tables and a few
extra columns.

1. Go to your Supabase project → **SQL Editor** → **New query**.
2. Open `supabase_schema.sql` from this repo, copy the whole thing, paste it in.
3. Click **Run**.

It is safe to run more than once and it never deletes anything.

**Until you do this:** analyses still run and still get logged, but feedback,
page-view/click tracking, waitlist emails, and cost-per-run are silently
dropped, and the funnel numbers on the stats page stay at zero.

**How to check it worked:** visit
`https://<your-app>/admin/stats?token=<your ADMIN_TOKEN>` after someone has run
an analysis. The "Funnel" and "Economics & feedback" sections should show
numbers rather than dashes.

---

## 2. Optional environment variables (Railway → Variables)

All have sensible defaults; set them only if you want to change behaviour.

| Variable | Default | What it does |
|---|---|---|
| `MAX_RUNS_PER_IP_PER_DAY` | `5` | How many analyses one person can run per day. Raise it on launch day if you want more headroom. |
| `MAX_CONCURRENT_JOBS` | `4` | How many analyses run at once. Anyone arriving above this is offered the waitlist instead. **This is your spend cap** — each analysis costs roughly US$0.57 in API calls (observed mean and p90 as at 20 July 2026; small sample, verify across ≥10 runs). |
| `CONSULTING_URL` | `https://thomasmurray.earth/startup-journey.html` | Where "Get help with your application" points, in the app, the email, and the XLSX. |
| `CONSULTING_EMAIL` | `thomasmurraynz@gmail.com` | The "email me directly" address. |
| `APP_URL` | the Railway URL | Used for the "view results" link in emails. **Change this when the domain lands.** |
| `SELF_BENCHMARK_ENABLED` | `1` | The app measures its own output quality on the 1st and 15th, **but only if no real analysis completed in the previous fortnight.** Set to `0` to stop that entirely. |
| `SELF_BENCHMARK_REPEATS` | `2` | Runs per benchmark company. Two is the minimum that makes consistency measurable — with one run there is nothing to compare against. |
| `SELF_BENCHMARK_MAX_RUNS` | `4` | Hard ceiling on benchmark runs per cycle. At roughly US$0.57 a run this caps a quiet fortnight at about US$2.30. |
| `SELF_BENCHMARK_MIN_REAL_RUNS` | `1` | How many completed real analyses in a fortnight suppress the benchmark. One is deliberate: any live evidence beats a synthetic run. |

---

## 3. Add the ADMIN_TOKEN secret to GitHub (2 minutes) — **DO THIS ONCE**

Without it the daily quality snapshot cannot run, and the fortnightly review
goes blind again.

1. GitHub → your `grant-analyzer` repo → **Settings** → **Secrets and
   variables** → **Actions**.
2. **New repository secret**.
3. Name: `ADMIN_TOKEN`. Value: exactly the same string as `ADMIN_TOKEN` in
   Railway → Variables.
4. **Add secret**.

**How to check it worked:** repo → **Actions** tab → **Quality snapshot** →
**Run workflow**. It should go green in under a minute and add a file at
`reports/snapshots/quality-<today>.json`. If it goes red, open the run — the
error line says whether the secret is missing or the app refused the token.

**If you ever change `ADMIN_TOKEN`,** change it in both places. Changing it in
Railway alone makes every snapshot fail with a 403 — which the workflow will
email you about, so you will not lose a fortnight to it.

**What this replaces:** the review used to fetch the admin endpoint live, at
review time, which meant a token had to reach the reviewer somehow and the app
had to be reachable at that exact moment. Now GitHub holds the secret, takes a
snapshot every morning at 07:00 UTC, and commits it to the repo. The review
reads a file. See `reports/snapshots/README.md`.

---

## 4. When you buy the domain (C1 — still open)

Buy it, attach it in Railway, then:

1. Set `APP_URL` in Railway to the new address.
2. In `frontend/index.html`, update the four absolute URLs in the `<head>`
   (`canonical`, `og:url`, `og:image`, `twitter:image`). They are all together
   in one block with a comment marking them.
3. Bump `CACHE_NAME` in `frontend/sw.js` (v4 → v5) so installed apps refresh.

Ask Claude to do steps 2 and 3 — they're one-line changes and easy to get wrong
by hand.

---

## What you can look at right now

- **The sample report** — `https://<your-app>/?demo=1`. A real analysis of a
  fictional climate hardware startup (Kelvara Systems), frozen as the demo. It's
  linked from the landing page under "What you'll get". If you don't like it as a
  showcase, say so and it can be re-run.
- **The stats dashboard** — `https://<your-app>/admin/stats?token=<ADMIN_TOKEN>`.
  Now shows the funnel (views → starts → completions → emails → downloads → CTA
  clicks), cost per run, feedback scores, and flags **GATE TRIGGERED** when the
  pricing gates in §6 of the launch plan fire.

---

## The fortnightly quality review

On the 1st and 15th at 9am, Claude reviews the app's actual output — not just
how many people used it — and brings you findings and proposals. You decide what
gets built; nothing user-facing changes without your say-so.

**One thing is required from you: §3 above, the GitHub secret.** After that it
runs on its own. Three things are worth knowing:

- Supabase pauses free projects that go unused for a stretch, and a paused
  project looks identical to a deleted one from outside — that is what happened
  on 30 July 2026. Resuming it from the Supabase dashboard restores everything
  and keeps the same URL and key, so nothing needs changing in Railway. The app
  now pings the database periodically to stop it happening again, but that only
  works while the app is deployed and running. If measurement ever looks dead,
  check `https://<your-app>/health?deep=1` first: it says outright whether the
  pipeline or the database is the problem.

- It reads yesterday's committed snapshot in `reports/snapshots/`, not the live
  app. The snapshot comes from a private endpoint,
  `https://<your-app>/admin/quality.json?token=<ADMIN_TOKEN>`, which returns the
  analyses people received with quality scores attached. You can still open that
  yourself; it's dense JSON rather than a dashboard. The review reads files so
  that it cannot be blinded by the app being briefly unreachable, which is what
  happened on 1 August 2026.
- If a fortnight goes by with nobody completing an analysis, the app runs two
  test companies twice each so the review still has something to measure. That
  costs about US$2.30 and only happens on a silent fortnight. If real people
  used it, it spends nothing.

To run a review off-cycle, ask Claude to "run the grant analyser improvement
cycle". Reports land in `reports/health-YYYY-MM-DD.md`, and the trend across
cycles accumulates in `reports/quality-history.json`.
