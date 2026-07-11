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
    subscription: dict | None = None   # browser push subscription object


class ContinueRequest(BaseModel):
    job_id:       str
    profile:      dict
    geographies:  str  = ""
    consortium:   bool = True
    accelerators: bool = True
    prizes:       bool = True
    email:        str  = ""


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
                await asyncio.to_thread(db.log_completed, job_id, grants_found, result)
                company = result.get("company_profile", {}).get("name", "your company")
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
                        f"Found {grants_found} grant {"opportunity" if grants_found == 1 else "opportunities"}. Tap to view your results.",
                    )
            elif t == "error":
                job["status"] = "failed"
                job["error"]  = event.get("message", "")
                await asyncio.to_thread(db.log_failed, job_id, job["error"])
    except Exception as exc:
        job["status"] = "failed"
        job["error"]  = str(exc)
        await asyncio.to_thread(db.log_failed, job_id, str(exc))


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

    await asyncio.to_thread(
        db.log_started,
        job_id, req.url.strip(), req.text.strip(),
        req.geographies, req.consortium, req.accelerators, req.prizes,
        ip, email,
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
    Returns {"job_id": "<uuid>"} immediately.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")

    job_id = req.job_id
    email  = req.email.strip() or (_jobs.get(job_id, {}).get("email")) or None

    asyncio.create_task(_phase23_task(job_id, req.profile, _prefs(req), email))

    return {"job_id": job_id}


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


@app.post("/download")
async def download(req: DownloadRequest) -> Response:
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

    total     = len(rows)
    completed = sum(1 for r in rows if r.get("status") == "completed")
    failed    = sum(1 for r in rows if r.get("status") == "failed")
    grants    = [r["grants_found"] for r in rows
                 if r.get("status") == "completed" and r.get("grants_found") is not None]
    avg_grants = round(sum(grants) / len(grants), 1) if grants else 0

    def esc(v: Any) -> str:
        import html
        return html.escape(str(v)) if v not in (None, "") else "—"

    table_rows = "".join(
        "<tr>"
        f"<td>{esc((r.get('created_at') or '')[:16].replace('T', ' '))}</td>"
        f"<td>{esc(r.get('company_name'))}</td>"
        f"<td>{esc(r.get('company_url'))}</td>"
        f"<td>{esc(r.get('geographies'))}</td>"
        f"<td>{esc(r.get('status'))}</td>"
        f"<td>{esc(r.get('grants_found'))}</td>"
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
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th,td{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}}
th{{background:#f5f5f5}}
</style></head><body>
<h1>Grant Analyzer — last {days} days</h1>
<div class="tiles">
<div class="tile"><b>{total}</b>analyses</div>
<div class="tile"><b>{completed}</b>completed</div>
<div class="tile"><b>{failed}</b>failed</div>
<div class="tile"><b>{avg_grants}</b>avg grants found</div>
</div>
<table><tr><th>Started</th><th>Company</th><th>URL</th><th>Geographies</th>
<th>Status</th><th>Grants</th><th>Email</th><th>Error</th></tr>
{table_rows}</table>
</body></html>"""
    return Response(content=page, media_type="text/html")


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="static")
