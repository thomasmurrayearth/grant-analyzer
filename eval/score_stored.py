#!/usr/bin/env python3
"""
Score analyses that have already been run — no pipeline, no API key, no network.

Two jobs:

* Establish and re-establish the **baseline** the fortnightly review trends
  against, from whatever runs are already on disk in `eval/results/`.
* Let anyone re-derive a cycle's numbers later. A review that cannot be
  reproduced is an opinion; one that can be re-run from stored inputs is a
  measurement.

Deliberately separate from `run_eval.py`, which executes the pipeline and
therefore needs a key, a network and about ten minutes per company. This reads
files.

Usage
-----
    python eval/score_stored.py                     # every stored run, grouped by case
    python eval/score_stored.py --case thermify     # one company
    python eval/score_stored.py --since 2026-07-01  # runs from a date onwards
    python eval/score_stored.py --json out.json     # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import quality  # noqa: E402
from cases import CASES_BY_ID  # noqa: E402

RESULTS_DIR = ROOT / "eval" / "results"

# eval/results/{case_id}_{YYYYMMDD}_{HHMMSS}.json
_NAME_RE = re.compile(r"^(?P<case>[a-z0-9]+)_(?P<date>\d{8})_(?P<time>\d{6})\.json$")


def stored_runs(case: str | None = None, since: str | None = None) -> list[dict]:
    """Every stored run, newest last, tagged with the case it belongs to."""
    runs: list[dict] = []
    for path in sorted(RESULTS_DIR.glob("*.json")):
        match = _NAME_RE.match(path.name)
        if not match:
            continue
        case_id, date = match["case"], match["date"]
        if case and case_id != case:
            continue
        if since and date < since.replace("-", ""):
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [skip] {path.name}: {exc}")
            continue
        runs.append({
            "file": path.name,
            "case_id": case_id,
            "date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
            "result": quality.unwrap(payload),
        })
    return runs


def score(runs: list[dict]) -> dict:
    """Per-case quality figures, plus the convergence reading across runs.

    Convergence is computed over every stored run of a company. Where those
    runs span different versions of the pipeline the figure mixes them, which
    understates agreement — so it is reported alongside the date range rather
    than on its own. Use `--since` to confine it to one version.
    """
    by_case: dict[str, list[dict]] = {}
    for run in runs:
        by_case.setdefault(run["case_id"], []).append(run)

    out: dict[str, dict] = {}
    for case_id, group in sorted(by_case.items()):
        payloads = [g["result"] for g in group]
        assessments = [quality.assess_run(p) for p in payloads]
        case = CASES_BY_ID.get(case_id)

        ground_truth = None
        if case:
            scored = [quality.score_against_ground_truth(p, case) for p in payloads]
            recalls = [s["recall"] for s in scored if s["recall"] is not None]
            exclusions = [s["exclusion_accuracy"] for s in scored
                          if s["exclusion_accuracy"] is not None]
            ground_truth = {
                "runs_scored": len(scored),
                "recall_mean": round(sum(recalls) / len(recalls), 3) if recalls else None,
                "recall_range": [min(recalls), max(recalls)] if recalls else None,
                "exclusion_accuracy_mean": (round(sum(exclusions) / len(exclusions), 3)
                                            if exclusions else None),
                "runs_with_violations": sum(1 for s in scored if s["must_not_violations"]),
                "most_missed": _most_common([m for s in scored for m in s["misses"]]),
            }

        out[case_id] = {
            "runs": len(group),
            "date_range": [group[0]["date"], group[-1]["date"]],
            "files": [g["file"] for g in group],
            "structural": quality.summarise(assessments),
            "ground_truth": ground_truth,
            "convergence": {
                "main": quality.convergence(payloads, scope="main"),
                "all": quality.convergence(payloads, scope="all"),
            },
        }
    return out


def _most_common(values: list[str]) -> list[list]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return [[k, v] for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


def _print(scores: dict) -> None:
    for case_id, data in scores.items():
        s, g = data["structural"], data["ground_truth"]
        conv = data["convergence"]["main"]
        print(f"\n{'=' * 68}\n  {case_id} — {data['runs']} run(s), "
              f"{data['date_range'][0]} to {data['date_range'][1]}\n{'=' * 68}")
        print(f"  Structural precision   {s['structural_precision']}   "
              f"(share of recommendations with no detectable defect)")
        print(f"  Main recommendations   {s['main_count']}  range {s['main_count_range']}")
        print(f"  Watchlist              {s['watchlist_count']}")
        print(f"  Tier concentration     {s['tier_concentration']}   "
              f"(1.0 = every item in one tier)")
        print(f"  Specific-link rate     {s['specific_link_rate']}")
        print(f"  Broken-link rate       {s['broken_rate']}")
        if s["defect_totals"]:
            print(f"  Defects                {s['defect_totals']}")
        if g:
            print(f"  Recall (ground truth)  {g['recall_mean']}  range {g['recall_range']}")
            print(f"  Exclusion accuracy     {g['exclusion_accuracy_mean']}")
            print(f"  Runs recommending something confirmed ineligible: "
                  f"{g['runs_with_violations']}/{g['runs_scored']}")
            if g["most_missed"]:
                print(f"  Most missed            {g['most_missed']}")
        if conv.get("comparable"):
            print(f"  Convergence (exact)    {conv['exact']['mean_pairwise_jaccard']}")
            print(f"  Convergence (family)   {conv['family']['mean_pairwise_jaccard']}")
            print(f"  In every run           {conv['family']['in_every_run']} of "
                  f"{conv['family']['distinct_across_runs']} distinct programmes")
        else:
            print("  Convergence            not comparable (needs two runs)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="score only this case id")
    parser.add_argument("--since", help="only runs on or after this date (YYYY-MM-DD)")
    parser.add_argument("--json", dest="json_path", help="write machine-readable output here")
    args = parser.parse_args()

    runs = stored_runs(args.case, args.since)
    if not runs:
        print("No stored runs matched. Nothing to score.")
        return
    scores = score(runs)
    _print(scores)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(scores, indent=2))
        print(f"\nWrote {args.json_path}")


if __name__ == "__main__":
    main()
