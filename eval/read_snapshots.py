"""
Read the committed quality snapshots — offline, no key, no network.

The companion to `score_stored.py`. That one re-derives figures from stored
*runs*; this one reads the daily *snapshots* the GitHub Actions workflow
commits into `reports/snapshots/`.

Run this first in any improvement cycle. It answers "can I trust what I am
about to read?" before any quality figure is quoted, which is the check whose
absence made two cycles blind without either of them noticing early.

Usage
-----
    python3 eval/read_snapshots.py                      # pipeline health + headlines
    python3 eval/read_snapshots.py --days 30            # wider window for gaps
    python3 eval/read_snapshots.py --trend real_runs.summary.structural_precision
    python3 eval/read_snapshots.py --json out.json      # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snapshots  # noqa: E402

# The measures a cycle leads with. Paths are resolved leniently, so a snapshot
# that predates a measure simply omits it rather than breaking the report.
HEADLINE_MEASURES = [
    ("Real runs completed",      "real_runs.summary.completed"),
    ("Structural precision",     "real_runs.summary.structural_precision"),
    ("Main-list mean",           "real_runs.summary.main_count_mean"),
    ("Watchlist mean",           "real_runs.summary.watchlist_count_mean"),
    ("Broken-link rate",         "real_runs.summary.broken_link_rate"),
    ("Link authority rate",      "real_runs.summary.link_authority_rate"),
    ("Convergence (exact)",      "real_runs.convergence.exact"),
    ("Feedback mean",            "feedback.mean"),
    ("Feedback responses",       "feedback.count"),
]


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=None, help="snapshot directory")
    parser.add_argument("--days", type=int, default=14,
                        help="window for gap detection (default 14)")
    parser.add_argument("--trend", default=None,
                        help="dotted path to trend, e.g. real_runs.summary.broken_link_rate")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write machine-readable output here")
    args = parser.parse_args()

    health = snapshots.health_report(args.dir, args.days)

    print()
    print("=" * 68)
    print("  Measurement pipeline")
    print("=" * 68)
    print(f"  Verdict            {health['verdict']}")
    print(f"  Snapshots on disk  {health['total_snapshots']} "
          f"({health['usable_snapshots']} usable)")
    print(f"  Newest usable      {_fmt(health['latest_date'])} "
          f"({_fmt(health['staleness_days'])} days old)")

    if health["failed_in_window"]:
        print(f"\n  Failed in the last {args.days} days:")
        for f in health["failed_in_window"]:
            print(f"    {f['date']}  HTTP {f['http_status']}")

    if health["missing_in_window"]:
        missing = health["missing_in_window"]
        shown = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
        print(f"\n  No snapshot at all on {len(missing)} day(s): {shown}")
        print("    (missing = the workflow did not run; failed = the app did not answer)")

    if health["blind"]:
        print("\n  ** This cycle is BLIND. Do not quote figures below as current. **")

    latest = snapshots.latest(args.dir)
    if latest:
        print()
        print("=" * 68)
        print(f"  Headline measures — snapshot of {latest.get('date')}")
        print("=" * 68)
        quality = latest.get("quality") or {}
        for label, path in HEADLINE_MEASURES:
            print(f"  {label:<24} {_fmt(snapshots._dig(quality, path))}")

        blind_spots = quality.get("blind_spots") or []
        if blind_spots:
            print("\n  Blind spots declared by the endpoint:")
            for spot in blind_spots:
                print(f"    - {spot}")

    if args.trend:
        print()
        print("=" * 68)
        print(f"  Trend — {args.trend}")
        print("=" * 68)
        points = snapshots.series(args.trend, args.dir)
        if not points:
            print("  No snapshot carries this measure.")
        else:
            for when, value in points:
                print(f"  {when}   {_fmt(value)}")
            move = snapshots.movement(args.trend, args.dir)
            if move:
                print(f"\n  {move['first_date']} → {move['last_date']}: "
                      f"{_fmt(move['first'])} → {_fmt(move['last'])} "
                      f"({move['direction']}, delta {move['delta']:+})")

    if args.json_path:
        payload = {"health": health}
        if args.trend:
            payload["trend"] = {
                "measure": args.trend,
                "points": snapshots.series(args.trend, args.dir),
                "movement": snapshots.movement(args.trend, args.dir),
            }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nWrote {args.json_path}")

    print()
    # Non-zero exit when blind, so an automated caller notices without parsing.
    return 1 if health["blind"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
