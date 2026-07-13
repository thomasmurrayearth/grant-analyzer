# Launch setup — the bits only Thomas can do

Everything in work packages WP-1 and WP-2 of the launch plan is built and
deployed. Three things need a human with passwords. None of them break the app
if you don't do them — the app keeps working, you just don't get the data.

---

## 1. Run the database migration (5 minutes) — **do this first**

The new feedback, funnel, and cost tracking write to three new Supabase tables
and a few new columns. They don't exist yet.

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
| `MAX_CONCURRENT_JOBS` | `4` | How many analyses run at once. Anyone arriving above this is offered the waitlist instead. **This is your spend cap** — each analysis costs roughly US$1–2 in API calls. |
| `CONSULTING_URL` | `https://thomasmurray.earth/startup-journey.html` | Where "Get help with your application" points, in the app, the email, and the XLSX. |
| `CONSULTING_EMAIL` | `thomasmurraynz@gmail.com` | The "email me directly" address. |
| `APP_URL` | the Railway URL | Used for the "view results" link in emails. **Change this when the domain lands.** |

---

## 3. When you buy the domain (C1 — still open)

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
