"""
FastAPI backend for the Grant Opportunity Analyser.
"""

import asyncio
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            if _v:
                os.environ[_k.strip()] = _v

import db
import email_sender
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analyzer
from analyzer import run_phase1, run_phase23
from exporter import generate_xlsx

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Grant Opportunity Analyser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job state  {job_id: {...}}
_jobs: dict[str, dict] = {}

# Completed results cache for XLSX download  {job_id: result_dict}
_results: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Server-side auto-continue state
#
# When Phase 1 finishes, the server (not the browser) schedules Phase 2+3 to
# start after a short grace period. This makes the hand-off survive the user
# closing their browser tab. These live OUTSIDE the _jobs dict because that
# dict is serialised to JSON by /status, and asyncio.Task objects are not
# JSON-serialisable.
# ---------------------------------------------------------------------------

# {job_id: asyncio.Task} — the pending "start Phase 2+3 soon" timer.
_auto_continue_tasks: dict[str, "asyncio.Task"] = {}

# job_ids whose Phase 2+3 has already begun, so it can never start twice
# (server auto-continue vs. the user clicking "Find grants").
_phase23_started: set[str] = set()

# job_ids where an open browser asked us to hold off (user is editing the
# profile). The server timer will not fire for these.
_auto_paused: set[str] = set()


# ---------------------------------------------------------------------------
# Abuse control
#
# Every analysis costs real API money, so a traffic spike (a launch-day post,
# a scraper) has to be bounded. Two limits, both overridable from the
# environment so they can be relaxed on launch day without a redeploy:
#   * a per-IP daily cap on new analyses;
#   * a cap on analyses running at the same time.
# A user who hits either is offered the waitlist instead of an error, which
# turns overload into list growth.
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


MAX_RUNS_PER_IP_PER_DAY = _int_env("MAX_RUNS_PER_IP_PER_DAY", 5)
MAX_CONCURRENT_JOBS     = _int_env("MAX_CONCURRENT_JOBS", 4)

# Seconds to wait after Phase 1 finishes before the server auto-starts Phase
# 2+3. Long enough for an open browser to show the profile and let the user
# start editing (which pauses this timer); short enough that a closed-browser
# run isn't left hanging. Overridable from the environment without a redeploy.
AUTO_CONTINUE_DELAY_SECONDS = _int_env("AUTO_CONTINUE_DELAY_SECONDS", 15)

AT_CAPACITY_MESSAGE = (
    "We're at capacity right now — analyses are queued behind other users. "
    "Leave your email and we'll run yours and send you the results."
)
DAILY_LIMIT_MESSAGE = (
    f"You've reached the limit of {MAX_RUNS_PER_IP_PER_DAY} analyses per day. "
    "Leave your email if you need more and we'll sort it out."
)

# {(ip, YYYY-MM-DD): count} — reset naturally as the date key changes.
_ip_runs_today: dict[tuple[str, str], int] = {}


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _running_jobs() -> int:
    return sum(
        1 for j in _jobs.values()
        if j.get("status") in ("started", "phase1_running", "phase23_running")
    )


def _ip_runs(ip: str | None) -> int:
    """Analyses started by this IP today. Counts the in-memory tally and, when
    the database is reachable, the persisted count — so a Railway restart does
    not hand everyone a fresh quota."""
    if not ip:
        return 0
    in_memory = _ip_runs_today.get((ip, _today()), 0)
    persisted = db.count_recent_runs_for_ip(ip, hours=24)
    return max(in_memory, persisted or 0)


def _note_ip_run(ip: str | None) -> None:
    if ip:
        key = (ip, _today())
        _ip_runs_today[key] = _ip_runs_today.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AnalyseRequest(BaseModel):
    url:          str        = ""
    text:         str        = ""
    geographies:  str        = ""
    consortium:   bool       = True
    accelerators: bool       = True
    prizes:       bool       = True
    email:        str        = ""
    newsletter:   bool       = False   # explicit consent to the deadline digest
    subscription: dict | None = None   # browser push subscription object


class FeedbackRequest(BaseModel):
    job_id:      str
    rating:      int
    comment:     str  = ""
    may_contact: bool = False
    email:       str  = ""


class EventRequest(BaseModel):
    event:  str
    job_id: str = ""
    detail: str = ""


class WaitlistRequest(BaseModel):
    email:  str
    reason: str = ""


class ContinueRequest(BaseModel):
    job_id:       str
    profile:      dict
    geographies:  str  = ""
    consortium:   bool = True
    accelerators: bool = True
    prizes:       bool = True
    email:        str  = ""


class PauseRequest(BaseModel):
    job_id: str


class DownloadRequest(BaseModel):
    job_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prefs(req: AnalyseRequest | ContinueRequest) -> dict:
    return {
        "geographies":  req.geographies or None,
        "consortium":   req.consortium,
        "accelerators": req.accelerators,
        "prizes":       req.prizes,
    }


def _get_ip(request: Request) -> str | None:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _phase1_task(
    job_id: str,
    url: str | None,
    text: str | None,
    preferences: dict,
    email: str | None,
) -> None:
    job = _jobs[job_id]
    job["status"] = "phase1_running"
    job["usage"]  = analyzer.start_usage_tracking()
    try:
        async for event in run_phase1(url=url, extra_text=text, preferences=preferences):
            t = event.get("type")
            if t == "progress":
                job["progress"].append(event.get("message", ""))
                if event.get("stage"):
                    job["stage"] = event["stage"]
            elif t == "profile_ready":
                job["status"]      = "profile_ready"
                job["profile"]     = event["profile"]
                job["source_note"] = event.get("source_note", "")
                await asyncio.to_thread(
                    db.log_profile_ready,
                    job_id,
                    event["profile"].get("name", ""),
                    event["profile"],
                )
            elif t == "error":
                job["status"] = "failed"
                job["error"]  = event.get("message", "Unknown error")
                await asyncio.to_thread(db.log_failed, job_id, job["error"])
    except Exception as exc:
        job["status"] = "failed"
        job["error"]  = str(exc)
        await asyncio.to_thread(db.log_failed, job_id, str(exc))
        return

    # Phase 1 finished cleanly. If a profile is ready, hand off to Phase 2+3 on
    # a server-side timer so the analysis proceeds even if the user has closed
    # their browser. An open browser can pre-empt this (edit + "Find grants")
    # or pause it while editing (see /analyse/pause).
    if job.get("status") == "profile_ready":
        _schedule_auto_continue(job_id, preferences, email)


async def _phase23_task(
    job_id: str,
    profile: dict,
    preferences: dict,
    email: str | None,
) -> None:
    job = _jobs.setdefault(job_id, {
        "status":      "phase23_running",
        "progress":    [],
        "stage":       2,
        "profile":     profile,
        "source_note": "",
        "result":      None,
        "error":       None,
        "prefs":       preferences,
    })
    job["status"]   = "phase23_running"
    job["progress"] = []   # fresh log for phases 2+3
    job["stage"]    = 2
    # Keep counting tokens into the same accumulator phase 1 used, so the
    # logged cost is the cost of the whole analysis.
    job["usage"]    = analyzer.start_usage_tracking(job.get("usage"))
    try:
        async for event in run_phase23(profile, preferences):
            t = event.get("type")
            if t == "progress":
                job["progress"].append(event.get("message", ""))
                if event.get("stage"):
                    job["stage"] = event["stage"]
            elif t == "complete":
                result = event["result"]
                _results[job_id] = result
                job["status"] = "completed"
                job["result"] = result
                grants_found  = len(result.get("opportunities", []))
                usage         = job.get("usage")
                cost          = analyzer.usage_cost_usd(usage)
                job["cost_usd"] = cost
                await asyncio.to_thread(
                    db.log_completed, job_id, grants_found, result, usage, cost,
                )
                company = result.get("company_profile", {}).get("name", "your company")
                noun    = "opportunity" if grants_found == 1 else "opportunities"
                if email:
                    await asyncio.to_thread(
                        email_sender.send_results_email,
                        email, company, grants_found, result, job_id,
                    )
                subscription = _jobs.get(job_id, {}).get("subscription")
                if subscription:
                    await asyncio.to_thread(
                        _send_push,
                        subscription,
                        f"Grant analysis ready — {company}",
                        f"Found {grants_found} grant {noun}. Tap to view your results.",
                    )
            elif t == "error":
                job["status"] = "failed"
                job["error"]  = event.get("message", "")
                await asyncio.to_thread(db.log_failed, job_id, job["error"])
    except Exception as exc:
        job["status"] = "failed"
        job["error"]  = str(exc)
        await asyncio.to_thread(db.log_failed, job_id, str(exc))


def _begin_phase23(
    job_id: str,
    profile: dict,
    preferences: dict,
    email: str | None,
) -> bool:
    """
    Start Phase 2+3 for a job exactly once. Returns False if it was already
    started (e.g. the server auto-continued and then the user also clicked
    "Find grants"). Cancels any pending auto-continue timer for the job.
    """
    if job_id in _phase23_started:
        return False
    _phase23_started.add(job_id)
    pending = _auto_continue_tasks.pop(job_id, None)
    if pending and not pending.done():
        pending.cancel()
    asyncio.create_task(_phase23_task(job_id, profile, preferences, email))
    return True


def _schedule_auto_continue(
    job_id: str,
    preferences: dict,
    email: str | None,
) -> None:
    """
    After Phase 1, wait AUTO_CONTINUE_DELAY_SECONDS and then start Phase 2+3
    using the profile the server already has — unless the user has paused it
    (editing) or already started it themselves. This is what makes the
    hand-off independent of the browser staying open.
    """
    async def _runner() -> None:
        try:
            await asyncio.sleep(AUTO_CONTINUE_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        # We're firing now — drop our own handle so _begin_phase23 doesn't try
        # to cancel the task from inside itself.
        _auto_continue_tasks.pop(job_id, None)
        if job_id in _auto_paused:
            return
        job = _jobs.get(job_id)
        if not job or job.get("status") != "profile_ready":
            return
        _begin_phase23(job_id, job.get("profile") or {}, preferences, email)

    _auto_continue_tasks[job_id] = asyncio.create_task(_runner())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _send_push(subscription: dict, title: str, body: str) -> None:
    """Fire a Web Push notification. Fails silently."""
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not private_key or not subscription:
        return
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=private_key,
            vapid_claims={"sub": "mailto:thomasmurraynz@gmail.com"},
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Push notification failed: %s", exc)


@app.get("/vapid-public-key")
def vapid_public_key() -> dict:
    return {"key": os.environ.get("VAPID_PUBLIC_KEY", "")}


@app.get("/health")
def health() -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "warning", "message": "ANTHROPIC_API_KEY not set"}
    return {"status": "ok"}


@app.post("/analyse")
async def analyse(req: AnalyseRequest, request: Request) -> dict:
    """
    Phase 1 — start company research as a background job.
    Returns {"job_id": "<uuid>"} immediately.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")
    if not req.url.strip() and not req.text.strip():
        raise HTTPException(
            status_code=400,
            detail="Please provide a website URL, paste some company text, or upload a document.",
        )

    ip    = _get_ip(request)
    email = req.email.strip() or None

    # Abuse / cost control. 429 carries a machine-readable reason so the
    # frontend can offer the waitlist rather than showing a dead end.
    if _running_jobs() >= MAX_CONCURRENT_JOBS:
        await asyncio.to_thread(db.log_event, "at_capacity", None, ip, "concurrency")
        raise HTTPException(
            status_code=429,
            detail={"reason": "at_capacity", "message": AT_CAPACITY_MESSAGE},
        )
    if await asyncio.to_thread(_ip_runs, ip) >= MAX_RUNS_PER_IP_PER_DAY:
        await asyncio.to_thread(db.log_event, "rate_limited", None, ip, "daily_cap")
        raise HTTPException(
            status_code=429,
            detail={"reason": "daily_limit", "message": DAILY_LIMIT_MESSAGE},
        )

    job_id = str(uuid.uuid4())

    _jobs[job_id] = {
        "status":       "started",
        "progress":     [],
        "stage":        1,
        "profile":      None,
        "source_note":  "",
        "result":       None,
        "error":        None,
        "subscription": req.subscription,
        "prefs": {
            "geographies":  req.geographies,
            "consortium":   req.consortium,
            "accelerators": req.accelerators,
            "prizes":       req.prizes,
        },
    }

    _note_ip_run(ip)

    await asyncio.to_thread(
        db.log_started,
        job_id, req.url.strip(), req.text.strip(),
        req.geographies, req.consortium, req.accelerators, req.prizes,
        ip, email, req.newsletter,
    )

    asyncio.create_task(_phase1_task(
        job_id,
        req.url.strip() or None,
        req.text.strip() or None,
        _prefs(req),
        email,
    ))

    return {"job_id": job_id}


@app.post("/analyse/continue")
async def analyse_continue(req: ContinueRequest) -> dict:
    """
    Phases 2+3 — start grant discovery as a background job.

    Called when the user clicks "Find grants" (optionally after editing the
    profile). Uses the same once-only starter as the server's auto-continue,
    so whichever fires first wins and the other becomes a no-op. Returns
    {"job_id": "<uuid>"} immediately.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")

    job_id = req.job_id
    email  = req.email.strip() or (_jobs.get(job_id, {}).get("email")) or None

    # If the server already auto-continued a moment ago, this is a harmless
    # no-op and we just report the running job.
    _begin_phase23(job_id, req.profile, _prefs(req), email)

    return {"job_id": job_id}


@app.post("/analyse/pause")
async def analyse_pause(req: PauseRequest) -> dict:
    """
    Ask the server to hold off on auto-continuing Phase 2+3 for this job.

    An open browser calls this when the user starts editing the reviewed
    profile, so the grace-period timer doesn't fire mid-edit. Phase 2+3 then
    starts only when the user clicks "Find grants". If Phase 2+3 has already
    begun this is a harmless no-op.
    """
    job_id = req.job_id
    _auto_paused.add(job_id)
    pending = _auto_continue_tasks.pop(job_id, None)
    if pending and not pending.done():
        pending.cancel()
    return {"paused": True, "job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str) -> dict:
    """
    Poll this endpoint to get the current state of a job.
    Falls back to Supabase for completed jobs after a server restart.
    """
    job = _jobs.get(job_id)
    if job:
        return job

    # Server restarted — check Supabase for completed result
    recovered = await asyncio.to_thread(db.get_completed_result, job_id)
    if recovered:
        _jobs[job_id]    = recovered
        _results[job_id] = recovered.get("result") or {}
        return recovered

    raise HTTPException(status_code=404, detail="Job not found")


@app.post("/extract")
async def extract_file(file: UploadFile) -> dict:
    """
    Extract plain text from an uploaded PDF, DOCX, or TXT file.
    Returns {"text": "...", "filename": "..."}.
    """
    filename = file.filename or ""
    content  = await file.read()
    ext      = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            text   = "\n".join(page.extract_text() or "" for page in reader.pages)

        elif ext in ("docx", "doc"):
            from docx import Document
            doc  = Document(io.BytesIO(content))
            text = "\n".join(para.text for para in doc.paragraphs)

        elif ext == "txt":
            text = content.decode("utf-8", errors="replace")

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type '.{ext}'. Please upload a PDF, DOCX, or TXT file.",
            )

        text = text.strip()[:15000]
        if not text:
            raise HTTPException(
                status_code=400,
                detail="No readable text could be extracted from the file.",
            )
        return {"text": text, "filename": filename}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not read file: {exc}")


# ---------------------------------------------------------------------------
# Funnel: feedback, events, waitlist
# ---------------------------------------------------------------------------

@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request) -> dict:
    """"How useful was this shortlist?" — the currency of the free phase."""
    rating = req.rating
    if not isinstance(rating, int) or not 1 <= rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")
    await asyncio.to_thread(
        db.log_feedback,
        req.job_id, rating, req.comment.strip(),
        req.may_contact, req.email.strip() or None,
    )
    return {"ok": True}


@app.post("/event")
async def event(req: EventRequest, request: Request) -> dict:
    """Record one funnel event (landing view, CTA click, share, download)."""
    if not req.event.strip():
        raise HTTPException(status_code=400, detail="Event name required.")
    await asyncio.to_thread(
        db.log_event,
        req.event.strip(), req.job_id.strip() or None,
        _get_ip(request), req.detail.strip(),
    )
    return {"ok": True}


@app.post("/waitlist")
async def waitlist(req: WaitlistRequest, request: Request) -> dict:
    """Email left after a run was turned away at capacity."""
    email = req.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    await asyncio.to_thread(db.log_waitlist, email, req.reason.strip(), _get_ip(request))
    return {"ok": True}


@app.post("/download")
async def download(req: DownloadRequest, request: Request) -> Response:
    """Return an XLSX file for a completed analysis."""
    result = _results.get(req.job_id)
    if not result:
        # Try recovering from Supabase (server restart)
        recovered = await asyncio.to_thread(db.get_completed_result, req.job_id)
        if recovered:
            result = recovered.get("result")
            if result:
                _results[req.job_id] = result
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Result not found. The analysis may not have completed yet.",
        )
    await asyncio.to_thread(
        db.log_event, "xlsx_download", req.job_id, _get_ip(request), None,
    )
    xlsx_bytes = generate_xlsx(result)
    company    = result.get("company_profile", {}).get("name", "grant-analysis")
    safe_name  = "".join(c if c.isalnum() or c in "- _" else "_" for c in company)
    filename   = f"{safe_name}-grant-analysis.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Admin analytics
# ---------------------------------------------------------------------------

@app.get("/admin/stats")
async def admin_stats(token: str = "", days: int = 30) -> Response:
    """Private usage dashboard. Requires ADMIN_TOKEN to be set in the
    environment and passed as ?token=...; returns 404 when disabled so the
    endpoint is invisible on deployments without a token."""
    import secrets as _secrets

    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(status_code=404, detail="Not found")
    if not _secrets.compare_digest(token, admin_token):
        raise HTTPException(status_code=403, detail="Invalid token")

    rows = await asyncio.to_thread(db.get_recent_analyses, days)
    if rows is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    events   = await asyncio.to_thread(db.get_recent_events, days)
    feedback = await asyncio.to_thread(db.get_recent_feedback, days)

    total     = len(rows)
    completed = sum(1 for r in rows if r.get("status") == "completed")
    failed    = sum(1 for r in rows if r.get("status") == "failed")
    grants    = [r["grants_found"] for r in rows
                 if r.get("status") == "completed" and r.get("grants_found") is not None]
    avg_grants = round(sum(grants) / len(grants), 1) if grants else 0
    emails     = sum(1 for r in rows if r.get("user_email"))

    # Funnel
    def count(name: str) -> int:
        return sum(1 for e in events if e.get("event") == name)

    views      = count("landing_view")
    downloads  = count("xlsx_download")
    cta_clicks = count("cta_click")
    turned_away = count("at_capacity") + count("rate_limited")

    def pct(numerator: int, denominator: int) -> str:
        return f"{round(100 * numerator / denominator)}%" if denominator else "—"

    # Cost per run — mean and p90, the number the pricing gates key off.
    costs = sorted(float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None)
    total_cost = round(sum(costs), 2)
    mean_cost  = f"${round(sum(costs) / len(costs), 2)}" if costs else "—"
    p90_cost   = f"${round(costs[min(int(len(costs) * 0.9), len(costs) - 1)], 2)}" if costs else "—"

    ratings    = [f["rating"] for f in feedback if isinstance(f.get("rating"), int)]
    avg_rating = f"{round(sum(ratings) / len(ratings), 1)}/5" if ratings else "—"

    def esc(v: Any) -> str:
        import html
        return html.escape(str(v)) if v not in (None, "") else "—"

    # Pricing gates from the launch plan (§6). Flagged explicitly so the
    # weekly report can say "GATE TRIGGERED" without re-deriving the rules.
    gates = []
    if completed >= 100 and len(ratings) >= 15 and ratings and sum(ratings) / len(ratings) >= 3.5:
        gates.append("Gate B: ≥100 completed analyses and ≥15 feedback responses averaging ≥3.5/5")
    if total_cost > 50:
        gates.append(f"Gate B: spend in this window is ${total_cost} (over the ~US$50/month trigger)")
    if total >= 500 and ratings and sum(ratings) / len(ratings) >= 4:
        gates.append("Gate C: ≥500 analyses with sustained ≥4/5 feedback")
    gates_html = (
        "<div class='gate'><b>GATE TRIGGERED</b><ul>"
        + "".join(f"<li>{esc(g)}</li>" for g in gates)
        + "</ul></div>"
    ) if gates else ""

    feedback_html = "".join(
        "<tr>"
        f"<td>{esc((f.get('created_at') or '')[:16].replace('T', ' '))}</td>"
        f"<td>{esc(f.get('rating'))}/5</td>"
        f"<td>{esc(f.get('comment'))}</td>"
        f"<td>{'yes' if f.get('may_contact') else '—'}</td>"
        f"<td>{esc(f.get('user_email'))}</td>"
        "</tr>"
        for f in feedback
    ) or "<tr><td colspan='5'>No feedback yet.</td></tr>"

    table_rows = "".join(
        "<tr>"
        f"<td>{esc((r.get('created_at') or '')[:16].replace('T', ' '))}</td>"
        f"<td>{esc(r.get('company_name'))}</td>"
        f"<td>{esc(r.get('company_url'))}</td>"
        f"<td>{esc(r.get('geographies'))}</td>"
        f"<td>{esc(r.get('status'))}</td>"
        f"<td>{esc(r.get('grants_found'))}</td>"
        f"<td>{('$' + str(r['cost_usd'])) if r.get('cost_usd') is not None else '—'}</td>"
        f"<td>{esc(r.get('user_email'))}</td>"
        f"<td>{esc((r.get('error_message') or '')[:120])}</td>"
        "</tr>"
        for r in rows
    )
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Grant Analyzer — Usage</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#222}}
.tiles{{display:flex;gap:1rem;margin-bottom:1.5rem;flex-wrap:wrap}}
.tile{{border:1px solid #ddd;border-radius:8px;padding:1rem 1.5rem}}
.tile b{{display:block;font-size:1.6rem}}
.tile span{{font-size:.8rem;color:#666}}
.gate{{border:2px solid #9C4A2F;background:#fdf1ed;border-radius:8px;padding:1rem 1.5rem;margin-bottom:1.5rem}}
h2{{margin-top:2rem;font-size:1.1rem}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th,td{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}}
th{{background:#f5f5f5}}
</style></head><body>
<h1>Grant Analyzer — last {days} days</h1>
{gates_html}
<h2>Usage</h2>
<div class="tiles">
<div class="tile"><b>{total}</b><span>analyses started</span></div>
<div class="tile"><b>{completed}</b><span>completed</span></div>
<div class="tile"><b>{failed}</b><span>failed</span></div>
<div class="tile"><b>{avg_grants}</b><span>avg grants found</span></div>
<div class="tile"><b>{turned_away}</b><span>turned away (at capacity)</span></div>
</div>
<h2>Funnel</h2>
<div class="tiles">
<div class="tile"><b>{views}</b><span>landing views</span></div>
<div class="tile"><b>{pct(total, views)}</b><span>view → start</span></div>
<div class="tile"><b>{pct(completed, total)}</b><span>start → completion</span></div>
<div class="tile"><b>{pct(emails, total)}</b><span>left an email</span></div>
<div class="tile"><b>{pct(downloads, completed)}</b><span>downloaded XLSX</span></div>
<div class="tile"><b>{cta_clicks}</b><span>consulting CTA clicks</span></div>
</div>
<h2>Economics &amp; feedback</h2>
<div class="tiles">
<div class="tile"><b>{mean_cost}</b><span>mean API cost / run</span></div>
<div class="tile"><b>{p90_cost}</b><span>p90 cost / run</span></div>
<div class="tile"><b>${total_cost}</b><span>API spend in window</span></div>
<div class="tile"><b>{avg_rating}</b><span>avg feedback ({len(ratings)} responses)</span></div>
</div>
<h2>Feedback</h2>
<table><tr><th>When</th><th>Rating</th><th>Comment</th><th>May contact</th><th>Email</th></tr>
{feedback_html}</table>
<h2>Analyses</h2>
<table><tr><th>Started</th><th>Company</th><th>URL</th><th>Geographies</th>
<th>Status</th><th>Grants</th><th>Cost</th><th>Email</th><th>Error</th></tr>
{table_rows}</table>
</body></html>"""
    return Response(content=page, media_type="text/html")


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="static")
