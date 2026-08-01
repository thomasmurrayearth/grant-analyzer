"""
Reading the daily quality snapshots.

`.github/workflows/quality-snapshot.yml` commits one file per day into
`reports/snapshots/`. This module turns that pile of files into the few things
the fortnightly improvement cycle actually needs to ask:

    What is the most recent usable measurement, and how old is it?
    Which days are missing or failed, and when did that start?
    How has a given measure moved over time?

Everything here is pure and offline — no network, no key, no database. That is
the point: the review cycle failed twice because it depended on reaching the
running app, so the read path it uses now must be a local file read that cannot
fail for environmental reasons.

Design notes
------------
* A snapshot with `ok: false` is kept, not discarded. An outage is evidence,
  and knowing the exact day the database stopped answering is more useful than
  a gap. `usable()` filters; `load_all()` does not.
* `staleness_days` is deliberately prominent. The single worst failure mode of
  this whole mechanism is a cycle that reads a stale snapshot and reports its
  figures as current. Callers should check it before quoting any number.
* Nothing here interprets quality. `quality.py` defines the measures; this
  module only locates them in time.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Iterable

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "reports", "snapshots")

# A snapshot older than this makes a cycle blind rather than merely late. Two
# days covers a delayed GitHub cron (schedules can slip under load) without
# tolerating a workflow that has actually stopped.
STALE_AFTER_DAYS = 2


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def load_all(directory: str | None = None) -> list[dict]:
    """
    Every snapshot on disk, oldest first, `latest.json` excluded.

    `latest.json` is a duplicate of the newest dated file, kept for humans
    opening the folder. Including it would double-count the newest day.
    """
    directory = directory or SNAPSHOT_DIR
    if not os.path.isdir(directory):
        return []

    out: list[dict] = []
    for name in sorted(os.listdir(directory)):
        if not name.startswith("quality-") or not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                snap = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # A corrupt file is itself a finding, and must not stop the cycle.
            out.append({
                "date": name[len("quality-"):-len(".json")],
                "ok": False,
                "http_status": "unreadable",
                "quality": None,
                "unreadable": True,
            })
            continue
        snap.setdefault("date", name[len("quality-"):-len(".json")])
        out.append(snap)

    out.sort(key=lambda s: s.get("date") or "")
    return out


def usable(snapshots: Iterable[dict]) -> list[dict]:
    """Only the snapshots that actually carry a measurement."""
    return [s for s in snapshots
            if s.get("ok") is True and s.get("quality") is not None]


def latest(directory: str | None = None) -> dict | None:
    """The most recent usable snapshot, or None if there has never been one."""
    ok = usable(load_all(directory))
    return ok[-1] if ok else None


# ---------------------------------------------------------------------------
# Freshness and gaps — "can this cycle trust what it is about to read?"
# ---------------------------------------------------------------------------

def staleness_days(directory: str | None = None,
                   today: date | None = None) -> int | None:
    """
    Days since the most recent *usable* snapshot. None if there are none.

    Check this before quoting any figure. A cycle that reports a stale
    snapshot's numbers as this fortnight's performance is worse than a cycle
    that reports nothing.
    """
    newest = latest(directory)
    if not newest:
        return None
    when = _parse_date(newest.get("date", ""))
    if not when:
        return None
    return ((today or date.today()) - when).days


def is_stale(directory: str | None = None,
             today: date | None = None,
             limit: int = STALE_AFTER_DAYS) -> bool:
    age = staleness_days(directory, today)
    return age is None or age > limit


def missing_dates(directory: str | None = None,
                  days: int = 14,
                  today: date | None = None) -> list[str]:
    """
    Dates in the last `days` with no snapshot file at all.

    Distinct from a failed snapshot: a missing date means the workflow did not
    run, which points at GitHub Actions; a failed one means it ran and the app
    did not answer, which points at Railway or Supabase.
    """
    today = today or date.today()
    have = {s.get("date") for s in load_all(directory)}
    out = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        if day not in have:
            out.append(day)
    return sorted(out)


def failed_snapshots(directory: str | None = None,
                     days: int = 14,
                     today: date | None = None) -> list[dict]:
    """Snapshots in the window that ran but recorded a failure."""
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    return [s for s in load_all(directory)
            if (s.get("date") or "") >= cutoff and not s.get("ok")]


def health_report(directory: str | None = None,
                  days: int = 14,
                  today: date | None = None) -> dict:
    """
    One call answering "is the measurement pipeline itself working?".

    Intended to be the first thing a review cycle runs, before it looks at a
    single quality figure.
    """
    today = today or date.today()
    snaps = load_all(directory)
    ok = usable(snaps)
    failures = failed_snapshots(directory, days, today)
    missing = missing_dates(directory, days, today)
    age = staleness_days(directory, today)

    if not snaps:
        verdict = "no snapshots have ever been taken"
    elif not ok:
        verdict = "snapshots exist but none carries a measurement"
    elif age is not None and age > STALE_AFTER_DAYS:
        verdict = f"newest usable snapshot is {age} days old"
    elif failures or missing:
        verdict = "measuring, with gaps"
    else:
        verdict = "measuring cleanly"

    return {
        "verdict": verdict,
        "blind": not ok or is_stale(directory, today),
        "total_snapshots": len(snaps),
        "usable_snapshots": len(ok),
        "staleness_days": age,
        "latest_date": ok[-1].get("date") if ok else None,
        "failed_in_window": [
            {"date": f.get("date"), "http_status": f.get("http_status")}
            for f in failures
        ],
        "missing_in_window": missing,
    }


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

def _dig(obj: Any, path: str) -> Any:
    """Fetch a dotted path, returning None rather than raising."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def series(path: str, directory: str | None = None) -> list[tuple[str, Any]]:
    """
    (date, value) for a dotted path inside each usable snapshot's `quality`.

    e.g. series("real_runs.summary.structural_precision")

    Days where the value is absent are omitted rather than zero-filled: a
    measure that did not exist yet and a measure that was zero are different
    facts, and conflating them would corrupt the trend.
    """
    out = []
    for snap in usable(load_all(directory)):
        value = _dig(snap.get("quality") or {}, path)
        if value is not None:
            out.append((snap.get("date"), value))
    return out


def movement(path: str, directory: str | None = None) -> dict | None:
    """
    First and last value of a series, and the direction of travel.

    Deliberately does not label a direction "good" or "bad" — whether a rise
    in a number is an improvement depends on the measure, and that judgement
    belongs to the review against launch plan §9a, not to a helper.
    """
    points = series(path, directory)
    numeric = [(d, v) for d, v in points if isinstance(v, (int, float))]
    if len(numeric) < 2:
        return None
    (first_date, first), (last_date, last) = numeric[0], numeric[-1]
    return {
        "measure": path,
        "first": first, "first_date": first_date,
        "last": last, "last_date": last_date,
        "delta": round(last - first, 6),
        "direction": "up" if last > first else ("down" if last < first else "flat"),
        "points": len(numeric),
    }
