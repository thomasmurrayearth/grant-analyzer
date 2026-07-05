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

    {
        # Non-UK profile shape: stress-tests geography gating. The UK/Wales
        # mandatory discovery queries must NOT fire, UK-registration-required
        # programmes must not be recommended as direct applications, and the
        # EU funding landscape (EIC etc.) must surface instead.
        #
        # Fictional company fed via extra_text (no URL) so the test doesn't
        # depend on a live website; Phase 1's external searches will simply
        # find nothing and the profile is built from the text below.
        "id":           "germandeeptech",
        "company_url":  None,
        "company_name": "Kaltefluss GmbH (fictional)",
        "extra_text": (
            "Kaltefluss GmbH is a deep-tech hardware startup registered in Munich, "
            "Germany. It builds high-temperature industrial heat pumps (up to 200C "
            "output) that replace gas-fired process heat in food processing and "
            "chemical plants across Germany and Austria. Technology: proprietary "
            "turbo-compressor with natural refrigerants. TRL 5-6: two pilot "
            "installations running at customer sites near Augsburg, no serial "
            "production yet. Stage: seed, 14 employees, ~EUR 2.1m raised from "
            "German angel investors. No UK presence, no UK customers, no UK "
            "subsidiary. Primary outcome: industrial decarbonisation - each unit "
            "displaces roughly 1,200 tonnes CO2 per year."
        ),
        "notes": "Fictional German industrial heat-pump company, TRL 5-6, seed stage, no UK presence.",
        "preferences": {
            "geographies":  "",
            "consortium":   True,
            "accelerators": True,
            "prizes":       True,
        },

        # No must_appear_in_main entries: ground truth for a fictional company
        # is only reliable for structural checks, not specific competitions.
        "must_appear_in_main": [],

        "must_appear_in_watchlist_or_main": [
            "EIC",             # EIC Accelerator/programmes — core fit for a German deep-tech SME
        ],

        # Programmes requiring UK registration must not be DIRECT recommendations
        # for a company with no UK presence (watchlist partner-route is acceptable)
        "must_not_appear_in_main": [
            "Innovate UK",
        ],

        # Devolved-nation programmes are impossible for a German company
        "must_not_appear_anywhere": [
            "Welsh",           # Welsh Government SMART FIS etc. — Wales presence required
            "Energy Catalyst", # ODA-only geography — wrong for a German/Austrian deployer too
        ],
    },

    # -------------------------------------------------------------------------
    # Add more test cases here as you validate other companies.
    # Suggested next additions:
    #   - A pre-seed UK deeptech company (tests early-TRL grant discovery)
    # -------------------------------------------------------------------------
]


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def run_pipeline(case: dict) -> dict:
    """Run Phase 1 + Phase 2+3 for a single test case. Returns the result dict."""
    url         = case.get("company_url")
    extra_text  = case.get("extra_text")
    preferences = case.get("preferences", {})

    print(f"\n{'=' * 64}")
    print(f"  Running: {case['company_name']}  ({url or 'text-only profile'})")
    if case.get("notes"):
        print(f"  Notes:   {case['notes'][:120]}...")
    print(f"{'=' * 64}")

    # ── Phase 1 ──────────────────────────────────────────────────────────────
    profile = None
    print("\n[Phase 1] Company research…")
    async for event in run_phase1(url=url, extra_text=extra_text, preferences=preferences):
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

    # must_not_appear_in_main — must not be a direct recommendation
    # (appearing in the watchlist, e.g. via a partner route, is acceptable)
    for pattern in case.get("must_not_appear_in_main", []):
        match = next(
            ((n, mb) for n, mb in opp_pairs if _matches(n, pattern, mb)),
            None,
        )
        matched_name = match[0] if match else None
        passed = matched_name is None
        findings["checks"].append({
            "rule":    "must_not_appear_in_main",
            "pattern": pattern,
            "result":  "PASS" if passed else "FAIL",
            "matched": matched_name,
        })
        findings["pass" if passed else "fail"] += 1
        if not passed:
            findings["fp_count"] += 1

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
    findings["link_stats"] = link_stats(result)
    return findings


def link_stats(result: dict) -> dict:
    """
    Application-link quality summary across main recommendations + watchlist.

    Tracks the Priority-2 goal ("every grant has a real, working link"):
    counts by link_type, HTTP-verification outcomes, and how many items have
    no usable URL at all.
    """
    stats = {
        "main":  {"application_portal": 0, "programme_page": 0,
                  "funder_homepage": 0, "unknown": 0},
        "watch": {"application_portal": 0, "programme_page": 0,
                  "funder_homepage": 0, "unknown": 0},
        "verified": 0, "broken": 0, "unverified": 0,
        "no_url": 0, "total": 0,
    }
    for bucket, items in (("main", result.get("opportunities", [])),
                          ("watch", result.get("strategic_watchlist", []))):
        for item in items:
            stats["total"] += 1
            lt = item.get("link_type", "unknown")
            stats[bucket][lt if lt in stats[bucket] else "unknown"] += 1
            ls = item.get("link_status", "unverified")
            stats[ls if ls in ("verified", "broken", "unverified") else "unverified"] += 1
            if not (item.get("application_link") or "").startswith("http"):
                stats["no_url"] += 1
    return stats


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

    ls = findings.get("link_stats") or link_stats(result)
    print("\n  Link quality (main | watchlist):")
    for lt in ("application_portal", "programme_page", "funder_homepage", "unknown"):
        print(f"    {lt:<20} {ls['main'][lt]:>3} | {ls['watch'][lt]:>3}")
    print(
        f"    HTTP check: {ls['verified']} verified, {ls['broken']} broken, "
        f"{ls['unverified']} unverified  |  items with no URL: {ls['no_url']}/{ls['total']}"
    )


def print_aggregate(case_id: str, runs: list[dict]) -> None:
    """Cross-run variance report for --repeat: per-check pass rates and link quality."""
    n = len(runs)
    print(f"\n{'#' * 64}")
    print(f"  AGGREGATE: {case_id} — {n} runs")
    scores = [f["score"] for f in runs]
    pcts   = [f["pct"] for f in runs]
    print(f"  Scores: {', '.join(scores)}  (mean {sum(pcts)/n:.1f}%)")
    print(f"{'#' * 64}")

    # Per-check pass rate across runs — shows WHICH checks are unstable
    check_totals: dict[tuple, int] = {}
    for f in runs:
        for c in f["checks"]:
            key = (c["rule"], c["pattern"])
            check_totals.setdefault(key, 0)
            if c["result"] == "PASS":
                check_totals[key] += 1
    print("\n  Per-check pass rate:")
    for (rule, pattern), passes in check_totals.items():
        flag = "" if passes == n else "  <-- UNSTABLE" if passes else "  <-- ALWAYS FAILS"
        print(f"    {passes}/{n}  [{rule.replace('_', ' ')}]  '{pattern}'{flag}")

    # Aggregate link quality
    agg = {"portal": 0, "page": 0, "homepage": 0, "unknown": 0,
           "verified": 0, "broken": 0, "total": 0}
    for f in runs:
        ls = f.get("link_stats")
        if not ls:
            continue
        for bucket in ("main", "watch"):
            agg["portal"]   += ls[bucket]["application_portal"]
            agg["page"]     += ls[bucket]["programme_page"]
            agg["homepage"] += ls[bucket]["funder_homepage"]
            agg["unknown"]  += ls[bucket]["unknown"]
        agg["verified"] += ls["verified"]
        agg["broken"]   += ls["broken"]
        agg["total"]    += ls["total"]
    if agg["total"]:
        print(
            f"\n  Link quality across all runs ({agg['total']} items): "
            f"{agg['portal']} portal, {agg['page']} programme page, "
            f"{agg['homepage']} homepage, {agg['unknown']} unknown; "
            f"{agg['verified']} verified, {agg['broken']} broken"
        )


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

    repeat = max(1, args.repeat)
    for case in cases:
        case_findings: list[dict] = []
        for i in range(repeat):
            if repeat > 1:
                print(f"\n>>> Run {i + 1}/{repeat} for '{case['id']}'")
            try:
                result   = await run_pipeline(case)
                findings = score_result(result, case)
                save_result(case["id"], result, findings)
                print_report(findings, result)
                case_findings.append(findings)
            except Exception as exc:
                print(f"\n  ERROR running {case['company_name']}: {exc}")
                import traceback
                traceback.print_exc()
        if repeat > 1 and case_findings:
            print_aggregate(case["id"], case_findings)


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
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help="Run each case N times and print an aggregate variance report "
             "(per-check pass rates across runs)",
    )
    asyncio.run(main(parser.parse_args()))
