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

# Load .env file if present (so ANTHROPIC_API_KEY can be set there)
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

# In-memory store for completed results (keyed by job_id)
_results: dict[str, dict] = {}


def _get_ip(request: Request) -> str | None:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AnalyseRequest(BaseModel):
    url:          str  = ""    # optional — website URL
    text:         str  = ""    # optional — pasted text or extracted file text
    geographies:  str  = ""
    consortium:   bool = True
    accelerators: bool = True
    prizes:       bool = True


class ContinueRequest(BaseModel):
    job_id:       str
    profile:      dict          # company profile, possibly edited by user
    geographies:  str  = ""
    consortium:   bool = True
    accelerators: bool = True
    prizes:       bool = True


class DownloadRequest(BaseModel):
    job_id: str


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _prefs(req: AnalyseRequest | ContinueRequest) -> dict:
    return {
        "geographies":  req.geographies or None,
        "consortium":   req.consortium,
        "accelerators": req.accelerators,
        "prizes":       req.prizes,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"status": "warning", "message": "ANTHROPIC_API_KEY not set"}
    return {"status": "ok"}


@app.post("/analyse")
async def analyse(req: AnalyseRequest, request: Request) -> StreamingResponse:
    """
    Phase 1 — company research.

    Streams SSE progress events.  Final event is one of:
      {"type": "profile_ready", "profile": {...}, "source_note": "...", "job_id": "<uuid>"}
      {"type": "error",         "message": "..."}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")
    if not req.url.strip() and not req.text.strip():
        raise HTTPException(
            status_code=400,
            detail="Please provide a website URL, paste some company text, or upload a document.",
        )

    ip = _get_ip(request)

    async def event_stream():
        job_id = str(uuid.uuid4())
        await asyncio.to_thread(
            db.log_started,
            job_id, req.url.strip(), req.text.strip(),
            req.geographies, req.consortium, req.accelerators, req.prizes, ip,
        )
        async for event in run_phase1(
            url=req.url.strip() or None,
            extra_text=req.text.strip() or None,
            preferences=_prefs(req),
        ):
            if event.get("type") == "profile_ready":
                event["job_id"] = job_id
                await asyncio.to_thread(
                    db.log_profile_ready,
                    job_id,
                    event["profile"].get("name", ""),
                    event["profile"],
                )
            elif event.get("type") == "error":
                await asyncio.to_thread(db.log_failed, job_id, event.get("message", ""))
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/analyse/continue")
async def analyse_continue(req: ContinueRequest) -> StreamingResponse:
    """
    Phases 2 + 3 — grant discovery, deep research, scoring.

    Takes the (possibly user-edited) company profile from Phase 1 and
    streams SSE until the analysis is complete.  Final event:
      {"type": "complete", "result": {...}, "job_id": "<uuid>"}
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not set.")

    async def event_stream():
        job_id = req.job_id
        async for event in run_phase23(req.profile, _prefs(req)):
            if event.get("type") == "complete":
                _results[job_id] = event["result"]
                event["job_id"] = job_id
                grants_found = len(event["result"].get("opportunities", []))
                await asyncio.to_thread(db.log_completed, job_id, grants_found, event["result"])
            elif event.get("type") == "error":
                await asyncio.to_thread(db.log_failed, job_id, event.get("message", ""))
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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

        text = text.strip()[:15000]   # cap length
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
    """Return an XLSX file for a previously completed analysis."""
    result = _results.get(req.job_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Result not found. The analysis may not have completed yet.",
        )
    xlsx_bytes  = generate_xlsx(result)
    company     = result.get("company_profile", {}).get("name", "grant-analysis")
    safe_name   = "".join(c if c.isalnum() or c in "- _" else "_" for c in company)
    filename    = f"{safe_name}-grant-analysis.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

_frontend = Path(__file__).parent / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="static")
