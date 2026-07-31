# Why the app promises ten and delivers three

**Diagnosis requested 31 July 2026 · Proposal 3 from improvement cycle 1**

The landing page and the progress messages promise ten scored opportunities.
Real users get three to five. This is what happens in between, and what I
recommend doing about it.

---

## The short version

Ten is not a target the pipeline aims at. It is the size of the batch handed to
the scoring model — and after that, **four separate stages remove items and
nothing ever puts any back.** The number of recommendations a user sees is
whatever happens to survive.

Every one of those stages is doing something worth doing. None of them is a
bug. The defect is architectural: the promise is set before the filtering, and
no stage is responsible for the promise being kept.

---

## Where the ten go

```
30 discovery searches
        ↓
longlist            30–45 candidates
        ↓  ranked by thematic fit, top 10 taken   ← SHORTLIST_SIZE = 10
shortlist           10 candidates, deep-researched (3 queries each)
        ↓  scoring model applies the full rubric
scored              ~10 items
        ↓  ① eligibility and routing enforcement
        ↓  ② specificity validation
        ↓  ③ admission gates  (new, shipped today)
        ↓  ④ link quality gate (tier demotion only — removes nothing)
main list           3–5 items          ← what the user is handed
watchlist           24–27 items
```

**① Eligibility and routing** (`_enforce_routing_rules`) excludes confirmed TRL
and geography mismatches outright, and moves anything the company cannot lead
to the watchlist as a partner route. Correct behaviour, and unavoidable.

**② Specificity validation** (`_validate_application_specificity`) demotes any
programme whose application process cannot be confirmed from search evidence.
Also correct — an opportunity you cannot apply to is not a recommendation.

**③ Admission gates** (`_apply_recommendation_gates`, added today) demote items
with no confirmed process, a passed deadline, a link broken on two checks, or a
link that isn't the funder's own. Replaying these over the seventeen stored runs
demotes **15% of recommendations** — nine for broken links, two for
non-authoritative sources. Real defects, but they make a thin list thinner.

**④ Link quality gate** only moves items between tiers. It is not part of the
volume problem.

The watchlist grows at every step, which is why it reaches 24–27. Note that
`WATCHLIST_CAP = 10` is declared in `analyzer.py` and **never used** — dead
code, and the reason the watchlist has no ceiling at all.

---

## What the evidence supports, and what it doesn't

| Claim | Confidence | Basis |
|---|---|---|
| Loss happens after scoring, not during discovery | High | Longlists run 30–45; the shortlist is a hard slice of 10 |
| Attrition is spread across stages ①–③ | High | Each removes items unconditionally; none can add |
| Today's new gates cost ~15% of survivors | High | Replayed over 17 stored runs |
| **Which stage loses the most** | **Unknown** | Only the first and last counts were ever recorded |

That last row is the honest gap. I've added stage-by-stage counters
(`pipeline_funnel` on every result, surfaced in `/admin/quality.json`), so the
next cycle can name the stage instead of reasoning about it. Any fix chosen
before those numbers exist is a fix chosen partly on inference.

---

## Options

### A. Over-shortlist — send more into scoring so ten survive

Raise `SHORTLIST_SIZE` so that after roughly 50–60% attrition, eight to ten
remain. That means shortlisting 18–20.

- **Fixes** the cause. The promise becomes something the pipeline aims at.
- **Costs** three research queries per extra item, plus scoring tokens. On
  observed economics (~US$0.57/run) this lands around **US$0.90–1.10 per run**,
  and adds two to four minutes to a ten-minute analysis.
- **Risk:** the extra candidates are, by construction, the ones that ranked
  11th–20th on thematic fit. Some will be weaker. Volume bought by lowering the
  bar is not a win, which is why the verification measure below holds
  structural precision fixed.

### B. Backfill — top up from the longlist after filtering

Let the gates run, then, if fewer than the target survive, promote the
next-best longlist candidates and deep-research only those.

- **Fixes** the cause, and spends nothing on runs that don't need it.
- **Costs** less than A on average, more in the worst case, and adds a second
  research round — a more invasive change to the pipeline's shape.
- **Risk:** two-pass logic is harder to reason about and to test.

### C. Change the promise to match reality

Say "up to ten" or "a shortlist of the opportunities that survive our checks",
and lead with the filtering as a feature.

- **Costs** nothing, ships today, and is arguably more honest positioning:
  *we checked, and these four are the ones worth your time.*
- **Does not fix** the underlying question of whether four is the right number
  for a founder to work with.
- **Risk:** if the true answer is that discovery is too narrow, this closes the
  conversation on a symptom.

### D. Do nothing until the funnel counters report

One fortnight of data names the guilty stage. If ② is losing six items per run,
the fix is the specificity threshold, not the shortlist size — and options A
and B would both be spending money to paper over it.

---

## Recommendation

**D now, then A or B once the data says which stage is losing them** — with C
as a free interim.

The reasoning: A and B both cost real money per run, permanently, and both
assume the loss is spread evenly across stages. If it turns out one stage is
responsible for most of it, tuning that stage is cheaper and better than
inflating the input to compensate. The counters ship today and the next cycle
is on 15 August, so the wait is two weeks.

Meanwhile C costs nothing and removes the immediate credibility problem, which
is not that four is too few but that the app said ten and gave four. A founder
who is told "we checked twenty and four survived" reads competence. One who is
promised ten and given four reads a broken tool.

**Verification, whichever route is taken:** median main-list count ≥ 8, with
structural precision *not falling* below its current level. Volume bought by
lowering the bar fails this test by design.

---

## One thing worth fixing regardless

`WATCHLIST_CAP = 10` exists and does nothing. Watchlists of 24–27 rows are the
result. Either enforce it or delete it — a constant that documents a policy the
code doesn't implement is worse than neither, because the next person to read
it will believe it.
