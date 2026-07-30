"""
Fallback self-benchmark: the app measures its own output quality, but only
when nothing else has.

Why this exists
---------------
The fortnightly review cycle can only tell whether the app is getting better if
something produced new measurements since the last cycle. Real user runs are by
far the best evidence — they are the actual product, on companies nobody wrote
ground truth for. Benchmark runs are a poor substitute: same two companies every
time, and they cost real API money.

So the rule is: **real runs first, benchmark only as a fallback.** On a cycle
day the app looks back over the fortnight, and if a single genuine analysis
completed it does nothing at all. Only a fortnight with no completed user runs
triggers a benchmark, and then only enough of one to make convergence
measurable — two companies, twice each.

That ordering is a deliberate owner decision, not an implementation detail.
Spending money to benchmark a fortnight that already has live evidence would
buy nothing; spending nothing on a fortnight with no evidence would leave the
improvement loop blind. This module is the seam between the two.

Everything that decides *whether* to run is a pure function, so the policy can
be tested without spending a cent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent / "eval"))
from cases import BENCHMARK_CASE_IDS, TEST_CASES, get_case  # noqa: E402

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

#: Event name marking one analysis as a benchmark rather than a user run.
#: Everything that reports usage filters on this, so benchmark activity can
#: never be mistaken for demand.
RUN_EVENT = "benchmark_run"

#: Event name recording that a cycle was considered, with what outcome. Written
#: whether or not runs happened, so the decision itself is auditable.
CYCLE_EVENT = "benchmark_cycle"

#: Days of the month a cycle is considered. Matches the fortnightly review
#: schedule so a review always has that morning's measurements available.
CYCLE_DAYS = (1, 15)

#: Hours after midnight UTC during which a cycle may start. A full set of runs
#: takes roughly forty minutes, and the review runs at 09:00 local, so starting
#: before 06:00 UTC leaves comfortable headroom even for a late, slow cycle.
CYCLE_WINDOW_END_HOUR = 6

#: How far back to look for genuine user activity.
LOOKBACK_DAYS = 14

#: How often the scheduler wakes to check whether it is in a cycle window.
POLL_SECONDS = 1800


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _flag_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def repeats() -> int:
    """Runs per company. Two is the minimum that makes convergence measurable
    at all: with one run there is nothing to compare it against."""
    return max(1, _int_env("SELF_BENCHMARK_REPEATS", 2))


def max_runs() -> int:
    """Hard ceiling on runs per cycle — the spend cap. Even if the case list
    grows, a cycle can never quietly become expensive."""
    return max(1, _int_env("SELF_BENCHMARK_MAX_RUNS", 4))


def min_real_runs() -> int:
    """Completed user analyses in the fortnight that suppress the benchmark."""
    return max(1, _int_env("SELF_BENCHMARK_MIN_REAL_RUNS", 1))


def enabled() -> bool:
    return _flag_env("SELF_BENCHMARK_ENABLED", True)


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

def cycle_key(now: datetime) -> str:
    """The identifier for the cycle `now` belongs to, or "" if outside one.

    A date string rather than a counter, so the marker written to the database
    is legible to a human reading the events table and stays meaningful if the
    schedule ever changes.
    """
    if now.day not in CYCLE_DAYS:
        return ""
    if now.hour >= CYCLE_WINDOW_END_HOUR:
        return ""
    return now.strftime("%Y-%m-%d")


def benchmark_job_ids(events: Iterable[dict]) -> set[str]:
    """Job ids belonging to benchmark runs, from the event log."""
    return {
        e["job_id"] for e in (events or [])
        if e.get("event") == RUN_EVENT and e.get("job_id")
    }


def count_real_completions(
    analyses: Iterable[dict],
    events: Iterable[dict],
    days: int = LOOKBACK_DAYS,
    now: datetime | None = None,
) -> int:
    """Genuine user analyses that *completed* in the window.

    Completion is the test, not starts. A fortnight of starts that all
    abandoned leaves no output to read, and the point of the fallback is to
    ensure the review has something to measure — so that fortnight should still
    get its benchmark. Counting starts instead would suppress the benchmark
    exactly when it is most needed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    excluded = benchmark_job_ids(events)
    count = 0
    for row in analyses or []:
        if row.get("status") != "completed":
            continue
        if row.get("job_id") in excluded:
            continue
        if _parsed(row.get("completed_at") or row.get("created_at")) < cutoff:
            continue
        count += 1
    return count


def _parsed(value: Any) -> datetime:
    """Parse a database timestamp, defaulting to "long ago" when unreadable.

    Defaulting old is the safe direction: an unparseable timestamp then fails
    to suppress a benchmark, which costs a few dollars, rather than fails to
    record real activity, which would leave the review blind.
    """
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def already_handled(events: Iterable[dict], key: str) -> bool:
    """Has this cycle already been decided? Survives restarts and redeploys."""
    return any(
        e.get("event") == CYCLE_EVENT and (e.get("detail") or "").startswith(key)
        for e in (events or [])
    )


def decide(
    now: datetime,
    analyses: Sequence[dict] | None,
    events: Sequence[dict] | None,
) -> tuple[bool, str, str]:
    """Should a benchmark cycle run right now?

    Returns `(run, cycle_key, reason)`. The reason is recorded whatever the
    answer, because "we looked and decided not to" is information the review
    cycle needs — without it, a fortnight with no benchmark data is ambiguous
    between "suppressed as designed" and "the scheduler never fired".
    """
    key = cycle_key(now)
    if not key:
        return False, "", "outside cycle window"
    if not enabled():
        return False, key, "disabled by SELF_BENCHMARK_ENABLED"
    if analyses is None:
        return False, key, "database unavailable — cannot tell if there were real runs"
    if already_handled(events or [], key):
        return False, key, "cycle already handled"
    real = count_real_completions(analyses, events or [], now=now)
    if real >= min_real_runs():
        return False, key, f"suppressed — {real} real completed run(s) in the last {LOOKBACK_DAYS} days"
    return True, key, f"no real completed runs in the last {LOOKBACK_DAYS} days"


def planned_runs() -> list[str]:
    """Case ids to run this cycle, in order, respecting the spend cap."""
    plan: list[str] = []
    for _ in range(repeats()):
        for case_id in BENCHMARK_CASE_IDS:
            plan.append(case_id)
    return plan[:max_runs()]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

async def run_case(
    case: dict,
    *,
    run_phase1: Callable[..., Any],
    run_phase23: Callable[..., Any],
    analyzer_module: Any,
    db_module: Any,
) -> dict:
    """Run one benchmark company end to end and store it like any other analysis.

    Stored through the normal `analyses` path on purpose: the review cycle then
    reads benchmark and real runs through exactly the same code, and anything
    that breaks for one breaks visibly for both. The `benchmark_run` event is
    what keeps them apart in reporting.

    Dependencies are injected rather than imported so the policy and the
    plumbing can be tested without importing the pipeline (which needs an API
    key) or the database.
    """
    job_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    url = case.get("company_url")
    prefs = case.get("preferences") or {}

    await asyncio.to_thread(
        db_module.log_event, RUN_EVENT, job_id, None, case["id"],
    )
    await asyncio.to_thread(
        db_module.log_started,
        job_id, url or "", case.get("extra_text") or "",
        prefs.get("geographies", ""), bool(prefs.get("consortium")),
        bool(prefs.get("accelerators")), bool(prefs.get("prizes")),
        None, None, False,
    )

    usage = analyzer_module.start_usage_tracking(None)
    try:
        profile = None
        async for event in run_phase1(
            url=url, extra_text=case.get("extra_text"), preferences=prefs,
        ):
            if event.get("type") == "profile_ready":
                profile = event["profile"]
            elif event.get("type") == "error":
                raise RuntimeError(f"phase 1: {event.get('message')}")
        if not profile:
            raise RuntimeError("phase 1 produced no profile")

        await asyncio.to_thread(
            db_module.log_profile_ready, job_id, profile.get("name") or case["company_name"], profile,
        )

        result = None
        async for event in run_phase23(profile, prefs):
            if event.get("type") == "complete":
                result = event["result"]
            elif event.get("type") == "error":
                raise RuntimeError(f"phase 2+3: {event.get('message')}")
        if not result:
            raise RuntimeError("phase 2+3 produced no result")

        cost = analyzer_module.usage_cost_usd(usage)
        await asyncio.to_thread(
            db_module.log_completed,
            job_id, len(result.get("opportunities") or []), result, usage, cost,
        )
        return {
            "case_id": case["id"],
            "job_id": job_id,
            "ok": True,
            "cost_usd": cost,
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds()),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Benchmark run failed (%s): %s", case["id"], exc)
        await asyncio.to_thread(db_module.log_failed, job_id, f"benchmark: {exc}")
        return {"case_id": case["id"], "job_id": job_id, "ok": False, "error": str(exc)}


async def run_cycle(
    key: str,
    reason: str,
    *,
    run_phase1: Callable[..., Any],
    run_phase23: Callable[..., Any],
    analyzer_module: Any,
    db_module: Any,
    wait_until_idle: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """Run every planned benchmark case, sequentially, and record the outcome.

    Sequential by design: benchmark runs must never compete with a user's run
    for the concurrency budget, and a user arriving mid-cycle matters more than
    finishing the measurement quickly.

    The cycle marker is written *before* any run starts, so a crash or redeploy
    part-way through cannot cause the whole cycle to be repeated — and its spend
    doubled — on restart.
    """
    await asyncio.to_thread(
        db_module.log_event, CYCLE_EVENT, None, None, f"{key} running: {reason}"[:200],
    )
    outcomes: list[dict] = []
    for case_id in planned_runs():
        case = get_case(case_id)
        if not case:
            continue
        if wait_until_idle:
            await wait_until_idle()
        outcomes.append(await run_case(
            case,
            run_phase1=run_phase1,
            run_phase23=run_phase23,
            analyzer_module=analyzer_module,
            db_module=db_module,
        ))

    ok = sum(1 for o in outcomes if o.get("ok"))
    spend = round(sum(o.get("cost_usd") or 0 for o in outcomes), 2)
    summary = f"{key} done: {ok}/{len(outcomes)} runs, ${spend}"
    await asyncio.to_thread(db_module.log_event, CYCLE_EVENT, None, None, summary[:200])
    logger.info("Benchmark cycle %s", summary)
    return {"cycle": key, "reason": reason, "runs": outcomes, "spend_usd": spend}


async def scheduler_loop(
    *,
    run_phase1: Callable[..., Any],
    run_phase23: Callable[..., Any],
    analyzer_module: Any,
    db_module: Any,
    wait_until_idle: Callable[[], Awaitable[None]] | None = None,
    poll_seconds: int = POLL_SECONDS,
    iterations: int | None = None,
) -> None:
    """Wake periodically, keep the database awake, decide, and act.

    Started once at application start-up, and started **regardless of whether
    the benchmark itself is enabled** — the keepalive below protects all
    measurement, not just benchmarking, so turning the benchmark off must not
    quietly turn that off too.

    A polling loop rather than a cron entry because the app is the only thing
    guaranteed to be running: an external scheduler would add a second system
    to keep alive, and a laptop-based one would simply miss cycles whenever the
    machine was closed.

    `iterations` bounds the loop for tests. In production it is None and the
    loop runs for the life of the process; any exception is logged and the loop
    continues, because a measurement job must never be able to take the app
    down.
    """
    count = 0
    while iterations is None or count < iterations:
        count += 1
        try:
            # Keep the analytics database awake.
            #
            # Managed database free tiers pause a project after a stretch with no
            # requests, and when they do the hostname stops resolving entirely —
            # the app carries on serving analyses while silently recording
            # nothing. The trap is that the trigger is *quietness*, which is
            # exactly the condition the fallback benchmark exists to measure: a
            # silent fortnight pauses the database, the paused database makes the
            # benchmark refuse to spend money, and the cycle goes blind precisely
            # when it was meant to step in.
            #
            # One indexed single-row read per poll is enough activity to prevent
            # that, and costs nothing worth counting.
            await asyncio.to_thread(db_module.health)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Database keepalive failed: %s", exc)

        try:
            if enabled():
                now = datetime.now(timezone.utc)
                if cycle_key(now):
                    analyses = await asyncio.to_thread(db_module.get_recent_analyses, 30)
                    events = await asyncio.to_thread(
                        db_module.get_events_named, RUN_EVENT, 60,
                    ) + await asyncio.to_thread(
                        db_module.get_events_named, CYCLE_EVENT, 60,
                    )
                    run, key, reason = decide(now, analyses, events)
                    if run:
                        await run_cycle(
                            key, reason,
                            run_phase1=run_phase1,
                            run_phase23=run_phase23,
                            analyzer_module=analyzer_module,
                            db_module=db_module,
                            wait_until_idle=wait_until_idle,
                        )
                    elif key:
                        await asyncio.to_thread(
                            db_module.log_event, CYCLE_EVENT, None, None,
                            f"{key} skipped: {reason}"[:200],
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Benchmark scheduler iteration failed: %s", exc)
        if iterations is None or count < iterations:
            await asyncio.sleep(poll_seconds)


__all__ = [
    "RUN_EVENT", "CYCLE_EVENT", "CYCLE_DAYS", "LOOKBACK_DAYS",
    "TEST_CASES", "BENCHMARK_CASE_IDS",
    "cycle_key", "benchmark_job_ids", "count_real_completions",
    "already_handled", "decide", "planned_runs",
    "run_case", "run_cycle", "scheduler_loop",
    "enabled", "repeats", "max_runs", "min_real_runs",
]
