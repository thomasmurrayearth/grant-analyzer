# Daily quality snapshots

One JSON file per day, committed automatically by
[`.github/workflows/quality-snapshot.yml`](../../.github/workflows/quality-snapshot.yml).
Nothing in this folder is written by hand.

## Why this folder exists

The fortnightly improvement cycle needs production telemetry. Twice it got
none:

| Cycle | Date | What went wrong |
|---|---|---|
| 1 | 30 Jul 2026 | The free-tier Supabase project had paused for inactivity. Nothing had been recorded for a fortnight, and the app looked perfectly healthy throughout. |
| 2 | 1 Aug 2026 | The database was fine, but the review session could not reach the app: its fetch tool only accepts URLs it has been handed, and the admin URL carries a token that must never be pasted into a prompt. |

Both times a fortnight of evidence was lost, and neither failure announced
itself. After two cycles the loop had never once verified that a change it
recommended actually worked.

The common cause is that **the reviewer depended on making a live,
authenticated network call at the moment of review.** That is the part that
had to go.

## How it works now

```
07:00 UTC daily
  GitHub Actions  ──reads ADMIN_TOKEN from repository secrets
                  ──GET /admin/quality.json?days=14
                  ──GET /health?deep=1
                  └──commits reports/snapshots/quality-YYYY-MM-DD.json to main

review cycle      ──reads a file. No network. No token. No secret in any prompt.
```

The secret lives in GitHub Actions secrets, where CI can use it and no prompt
can ever see it. This is the ordinary way scheduled reporting is done; nothing
here is novel, which is the point.

## Properties worth knowing

**A failed fetch is still committed.** If the app returns 503, the snapshot
records `"ok": false` and the HTTP status, and `health` still captures whether
the database or the app was the problem. A missing file and a broken database
used to look identical from the outside. Now an outage is data, and you can
see the exact day it began.

**The workflow keeps itself alive.** GitHub disables scheduled workflows after
60 days without repository activity, and only new commits count as activity.
Because this one commits every day, it cannot be switched off by the quiet
fortnights it exists to cover — which is precisely the trap cycle 1 fell into,
where the app being quiet was also what broke the measurement.

**A failure is loud.** The final step exits non-zero when the snapshot records
a bad fetch, so GitHub emails the repository owner. That alarm is what was
missing both times measurement died.

**Git history is the archive.** Every day is a commit, so the trend can be
reconstructed for any past date, and a lost cycle can be recovered later
rather than being gone for good.

## File shape

```jsonc
{
  "schema": 1,
  "date": "2026-08-01",
  "fetched_at": "2026-08-01T07:00:03Z",
  "source": "/admin/quality.json?days=14",
  "http_status": "200",
  "ok": true,               // false => the fetch failed; `quality` is null
  "health": { ... },        // /health?deep=1, unauthenticated, always recorded
  "quality": { ... }        // the full scored payload, or null
}
```

`latest.json` is a copy of the newest dated file, for opening the folder and
seeing the current state without sorting by name. The reader ignores it so the
newest day is not counted twice.

## Reading them

```bash
python3 eval/read_snapshots.py                  # is the pipeline healthy? headline numbers
python3 eval/read_snapshots.py --days 30        # wider window for gaps
python3 eval/read_snapshots.py --trend real_runs.summary.broken_link_rate
```

The command exits non-zero when the newest usable snapshot is missing or more
than two days old, so a blind cycle is detected in the first thirty seconds
rather than after an hour of work.

`snapshots.py` is the library behind it — `health_report()`, `staleness_days()`,
`series()`, `movement()`. It is pure and offline, and covered by
`tests/test_snapshots.py`, which is mostly about the not-measuring cases:
stale files, missing days, failed fetches, corrupt JSON.

## The one rule

**Check `staleness_days` before quoting any figure.** A cycle that reports a
three-week-old snapshot as this fortnight's performance is worse than a cycle
that reports nothing, because it manufactures confidence that isn't there.
`read_snapshots.py` prints a blunt warning and exits non-zero for exactly this
reason.
