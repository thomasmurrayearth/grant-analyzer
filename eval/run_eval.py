#!/usr/bin/env python3
"""
Grant Analyser Evaluation Harness

Runs the full pipeline against known test companies and scores the output
against ground truth to measure precision and recall.  Make one prompt
change at a time, then run this to confirm the delta before shipping.

Usage
-----
Run all test cases (full pipeline, ~10 min each):
    python eval/run_eval.py

Run a single test case by id:
    python eval/run_eval.py --case thermify

Score a previously saved result without re-running the pipeline:
    python eval/run_eval.py --score eval/results/thermify_20250101_120000.json

Results are saved automatically to eval/results/ after each run.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# On Windows the default console encoding (cp1252) can't handle all Unicode.
# Set PYTHONIOENCODING before any output so print() works everywhere.
os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")

# Make the parent package importable when run from repo root or eval/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so ANTHROPIC_API_KEY is available.
# Use os.environ[k] = v (not setdefault) so the file always wins over an
# empty environment variable that a subprocess might inherit.
_env = ROOT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")   # strip surrounding quotes
            if _k and _v:
                os.environ[_k] = _v

from analyzer import run_phase1, run_phase23  # noqa: E402


# ---------------------------------------------------------------------------
# Ground-truth test cases
# ---------------------------------------------------------------------------
# Each case defines:
#   must_appear_in_main              grants that MUST be main recommendations
#   must_appear_in_watchlist_or_main grants that should appear SOMEWHERE
#   must_not_appear_anywhere         grants that must be excluded entirely
#
# Name matching is case-insensitive substring:
#   "Energy Catalyst" matches "Innovate UK Energy Catalyst Round 12"
#   "Ofgem" matches "Ofgem Strategic Innovation Fund 2025"
#
# Keep must_appear_in_main sparse — only add entries you are certain about.
# Wrong ground truth is worse than no ground truth.
# ---------------------------------------------------------------------------

TEST_CASES = [
    {
        "id":           "thermify",
        "company_url":  "https://thermify.cloud/",
        "company_name": "Thermify",
        "notes": (
            "Wales-registered: 39a Vale Business Park, Llandow, Cowbridge, Wales CF71 7PF. "
            "Distributed edge-compute-as-heating hardware (HeatHub: 500 Raspberry Pi CMs in oil "
            "bath replacing gas boilers). TRL 7-8 (live installs, GBP 2.5m contracted revenue, "
            "aiming for 40k units/year). "
            "Confirmed Ofgem SIF recipient — via SHIELD project led by UK Power Networks "
            "(DNO lead applicant, Thermify as technology partner). "
            "Facility at Sony Pencoed site, South Wales. "
            "Active partnership: Swansea University SPECIFIC IKC. "
            "Coverage: The Register, Data Centre Dynamics, Solar Power Portal. "
            "SIF NOTE: Thermify cannot be SIF lead applicant (requires energy licence holder). "
            "KTP NOTE: Thermify + Swansea University partnership already in place — KTP formalisation viable."
        ),
        "preferences": {
            "geographies":  "",
            "consortium":   True,
            "accelerators": True,
            "prizes":       True,
        },

        # Grants that MUST appear as main recommendations (direct applicant, high confidence)
        "must_appear_in_main": [
            "Innovate UK",     # Some current Innovate UK competition — direct SME applicant
            "SMART",           # Welsh Government SMART FIS — Wales-registered, direct applicant
        ],

        # Grants that should appear SOMEWHERE (main OR watchlist — either is acceptable)
        "must_appear_in_watchlist_or_main": [
            "Ofgem",           # Ofgem SIF — confirmed prior recipient (as partner); must surface somewhere
            "EIC Accelerator", # UK companies eligible for up to EUR 2.5m grant; multiple 2026 deadlines
            "KTP",             # Knowledge Transfer Partnership — Swansea University partner already in place
        ],

        # Grants that must NOT appear anywhere (confirmed ineligible or wrong applicant geography)
        "must_not_appear_anywhere": [
            "Energy Catalyst",  # ODA-only — funds projects in developing countries only; UK company ineligible
            "EIC Pathfinder",   # TRL 1-4 only; Thermify is TRL 7-8 — explicit confirmed mismatch
        ],
    },

    # -------------------------------------------------------------------------
    # Add more test cases here as you validate other companies.
    # Suggested next additions:
    #   - A pre-seed UK deeptech company (tests early-TRL grant discovery)
    #   - A non-UK EU company (tests Horizon / EIC landscape without UK funds)
    # -------------------------------------------------------------------------
]


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def run_pipeline(case: dict) -> dict:
    """Run Phase 1 + Phase 2+3 for a single test case. Returns the result dict."""
    url         = case["company_url"]
    preferences = case.get("preferences", {})

    print(f"\n{'=' * 64}")
    print(f"  Running: {case['company_name']}  ({url})")
    if case.get("notes"):
        print(f"  Notes:   {case['notes'][:120]}...")
    print(f"{'=' * 64}")

    # ── Phase 1 ──────────────────────────────────────────────────────────────
    profile = None
    print("\n[Phase 1] Company research…")
    async for event in run_phase1(url=url, extra_text=None, preferences=preferences):
        if event["type"] == "progress":
            print(f"  {event['message']}")
        elif event["type"] == "profile_ready":
            profile = event["profile"]
            print(
                f"  [OK] Profile: {profile.get('name')} | "
                f"TRL: {profile.get('trl')} | "
                f"HQ: {profile.get('hq')}"
            )
        elif event["type"] == "error":
            raise RuntimeError(f"Phase 1 error: {event['message']}")

    if not profile:
        raise RuntimeError("Phase 1 did not return a profile.")

    # ── Phase 2 + 3 ──────────────────────────────────────────────────────────
    result = None
    print("\n[Phase 2+3] Grant discovery and scoring…")
    async for event in run_phase23(profile, preferences):
        if event["type"] == "progress":
            print(f"  {event['message']}")
        elif event["type"] == "complete":
            result = event["result"]
            n_opps  = len(result.get("opportunities", []))
            n_watch = len(result.get("strategic_watchlist", []))
            print(f"  [OK] Complete: {n_opps} main recommendations, {n_watch} watchlist items")
        elif event["type"] == "error":
            raise RuntimeError(f"Phase 2+3 error: {event['message']}")

    if not result:
        raise RuntimeError("Phase 2+3 did not return a result.")

    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _matches(grant_name: str, pattern: str, managing_body: str = "") -> bool:
    """
    Case-insensitive substring match against grant name AND managing body.

    Matching on managing_body ensures that patterns like "Innovate UK" correctly
    match grants like "Knowledge Transfer Partnership" (managed by Innovate UK)
    that don't include the organisation name in the grant title itself.
    """
    combined = (grant_name + " " + managing_body).lower()
    return pattern.lower() in combined


def score_result(result: dict, case: dict) -> dict:
    """
    Score a pipeline result against the ground-truth rules for a test case.

    Returns a findings dict containing per-rule pass/fail and aggregate counts.
    """
    opportunities = result.get("opportunities", [])
    watchlist     = result.get("strategic_watchlist", [])

    # Each entry is a (name, managing_body) tuple for richer pattern matching
    opp_pairs   = [(o.get("name", ""), o.get("managing_body", "")) for o in opportunities]
    watch_pairs = [(w.get("name", ""), w.get("managing_body", "")) for w in watchlist]
    all_pairs   = opp_pairs + watch_pairs

    # Backwards-compatible name-only lists for reporting
    opp_names   = [n for n, _ in opp_pairs]
    watch_names = [n for n, _ in watch_pairs]
    all_names   = opp_names + watch_names

    findings: dict = {
        "case_id":   case["id"],
        "company":   case["company_name"],
        "n_main":    len(opportunities),
        "n_watch":   len(watchlist),
        "checks":    [],
        "pass":      0,
        "fail":      0,
        "fp_count":  0,   # false positives (must_not_appear items that appeared)
        "fn_count":  0,   # false negatives (must_appear items that didn't appear)
    }

    # must_appear_in_main — should be in main recommendations
    for pattern in case.get("must_appear_in_main", []):
        match = next(
            ((n, mb) for n, mb in opp_pairs if _matches(n, pattern, mb)),
            None,
        )
        matched_name = match[0] if match else None
        passed = matched_name is not None
        findings["checks"].append({
            "rule":    "must_appear_in_main",
            "pattern": pattern,
            "result":  "PASS" if passed else "FAIL",
            "matched": matched_name,
        })
        findings["pass" if passed else "fail"] += 1
        if not passed:
            findings["fn_count"] += 1

    # must_appear_in_watchlist_or_main — should appear anywhere
    for pattern in case.get("must_appear_in_watchlist_or_main", []):
        match = next(
            ((n, mb) for n, mb in all_pairs if _matches(n, pattern, mb)),
            None,
        )
        matched_name = match[0] if match else None
        passed = matched_name is not None
        findings["checks"].append({
            "rule":    "must_appear_in_watchlist_or_main",
            "pattern": pattern,
            "result":  "PASS" if passed else "FAIL",
            "matched": matched_name,
        })
        findings["pass" if passed else "fail"] += 1
        if not passed:
            findings["fn_count"] += 1

    # must_not_appear_anywhere — should be excluded entirely
    for pattern in case.get("must_not_appear_anywhere", []):
        match = next(
            ((n, mb) for n, mb in all_pairs if _matches(n, pattern, mb)),
            None,
        )
        matched_name = match[0] if match else None
        passed = matched_name is None   # PASS means it was correctly excluded
        findings["checks"].append({
            "rule":    "must_not_appear_anywhere",
            "pattern": pattern,
            "result":  "PASS" if passed else "FAIL",
            "matched": matched_name,
        })
        findings["pass" if passed else "fail"] += 1
        if not passed:
            findings["fp_count"] += 1

    total = findings["pass"] + findings["fail"]
    findings["score"]   = f"{findings['pass']}/{total}"
    findings["pct"]     = round(100 * findings["pass"] / total, 1) if total else 0
    return findings


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(findings: dict, result: dict) -> None:
    passed = findings["pass"]
    total  = findings["pass"] + findings["fail"]
    pct    = findings["pct"]

    print(f"\n{'=' * 64}")
    print(f"  SCORE: {findings['company']}  ->  {passed}/{total} ({pct}%)")
    print(f"  Main recommendations: {findings['n_main']}  |  Watchlist: {findings['n_watch']}")
    if findings["fp_count"]:
        print(f"  [WARN]  {findings['fp_count']} false positive(s) -- grants that should have been excluded")
    if findings["fn_count"]:
        print(f"  [WARN]  {findings['fn_count']} false negative(s) -- grants that should have appeared")
    print(f"{'=' * 64}")

    print("\n  Ground-truth checks:")
    for check in findings["checks"]:
        icon  = "[PASS]" if check["result"] == "PASS" else "[FAIL]"
        label = check["rule"].replace("_", " ")
        note  = f"  -> matched: \"{check['matched']}\"" if check["matched"] else ""
        print(f"    {icon}  [{label}]  '{check['pattern']}'{note}")

    print(f"\n  Main recommendations ({findings['n_main']}):")
    for opp in result.get("opportunities", []):
        tier   = opp.get("priority_tier", "?")
        score  = opp.get("priority_score", "?")
        fit    = opp.get("thematic_fit_score", "?")
        timing = opp.get("application_timing", "?")
        atype  = opp.get("applicant_type_match", "?")
        print(
            f"    • [{tier}]  {opp.get('name', '?')}"
            f"  (fit={fit}, score={score}, timing={timing}, applicant={atype})"
        )

    print(f"\n  Strategic watchlist ({findings['n_watch']}):")
    for item in result.get("strategic_watchlist", []):
        why = (item.get("why_watchlist") or "")[:80]
        print(f"    • {item.get('name', '?')}  — {why}")


# ---------------------------------------------------------------------------
# Save / load helpers
# ---------------------------------------------------------------------------

def save_result(case_id: str, result: dict, findings: dict) -> Path:
    results_dir = ROOT / "eval" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = results_dir / f"{case_id}_{ts}.json"
    path.write_text(json.dumps({"result": result, "findings": findings}, indent=2))
    print(f"\n  Saved -> {path.relative_to(ROOT)}")
    return path


def load_result(path_str: str) -> tuple[dict, dict]:
    data = json.loads(Path(path_str).read_text())
    return data["result"], data.get("findings", {})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    if args.score:
        # Score-only mode: load a previously saved result file, re-score it
        result, _ = load_result(args.score)
        stem    = Path(args.score).stem          # e.g. "thermify_20250101_120000"
        case_id = stem.split("_")[0]
        case    = next((c for c in TEST_CASES if c["id"] == case_id), None)
        if not case:
            ids = [c["id"] for c in TEST_CASES]
            print(f"No test case found with id '{case_id}'. Available: {ids}")
            print("Update TEST_CASES in eval/run_eval.py to add new test companies.")
            return
        findings = score_result(result, case)
        print_report(findings, result)
        return

    # Run mode — execute the pipeline for each case
    cases = TEST_CASES
    if args.case:
        cases = [c for c in TEST_CASES if c["id"] == args.case]
        if not cases:
            ids = [c["id"] for c in TEST_CASES]
            print(f"No test case with id '{args.case}'. Available: {ids}")
            return

    for case in cases:
        try:
            result   = await run_pipeline(case)
            findings = score_result(result, case)
            save_result(case["id"], result, findings)
            print_report(findings, result)
        except Exception as exc:
            print(f"\n  ERROR running {case['company_name']}: {exc}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Grant Analyser evaluation harness — run pipeline and score against ground truth"
    )
    parser.add_argument(
        "--case",
        help="Run only this test case (by id, e.g. --case thermify)",
    )
    parser.add_argument(
        "--score",
        metavar="RESULT_FILE",
        help="Score a previously saved result JSON instead of running the pipeline",
    )
    asyncio.run(main(parser.parse_args()))
