"""
Supabase analytics logging.  All functions are best-effort — they never
raise, so a database outage cannot break the main analysis pipeline.

Required environment variables (set in Railway / .env):
  SUPABASE_URL   — your project URL, e.g. https://xxxx.supabase.co
  SUPABASE_KEY   — your project anon/service-role key

Supabase table DDL (run once in the Supabase SQL editor):

  CREATE TABLE analyses (
    id               BIGSERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    job_id           TEXT NOT NULL,
    company_url      TEXT,
    input_snippet    TEXT,        -- first 500 chars of pasted text, if any
    company_name     TEXT,
    geographies      TEXT,
    consortium       BOOLEAN,
    accelerators     BOOLEAN,
    prizes           BOOLEAN,
    status           TEXT DEFAULT 'started',
    grants_found     INTEGER,
    profile_json     JSONB,
    results_json     JSONB,
    error_message    TEXT,
    ip_address       TEXT
  );
"""

import logging
import os

logger = logging.getLogger(__name__)

_supabase_client = None
_client_initialised = False


def _client():
    global _supabase_client, _client_initialised
    if _client_initialised:
        return _supabase_client
    _client_initialised = True
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
    except Exception as exc:
        logger.warning("Could not initialise Supabase client: %s", exc)
    return _supabase_client


def log_started(
    job_id: str,
    company_url: str,
    input_text: str,
    geographies: str,
    consortium: bool,
    accelerators: bool,
    prizes: bool,
    ip: str | None,
) -> None:
    c = _client()
    if not c:
        return
    try:
        snippet = (input_text or "")[:500] or None
        c.table("analyses").insert({
            "job_id":       job_id,
            "company_url":  company_url or None,
            "input_snippet": snippet,
            "geographies":  geographies or None,
            "consortium":   consortium,
            "accelerators": accelerators,
            "prizes":       prizes,
            "ip_address":   ip,
            "status":       "started",
        }).execute()
    except Exception as exc:
        logger.warning("DB log_started failed: %s", exc)


def log_profile_ready(job_id: str, company_name: str, profile: dict) -> None:
    c = _client()
    if not c:
        return
    try:
        c.table("analyses").update({
            "company_name": company_name,
            "profile_json": profile,
            "status":       "profile_ready",
        }).eq("job_id", job_id).execute()
    except Exception as exc:
        logger.warning("DB log_profile_ready failed: %s", exc)


def log_completed(job_id: str, grants_found: int, results: dict) -> None:
    c = _client()
    if not c:
        return
    try:
        from datetime import datetime, timezone
        c.table("analyses").update({
            "status":        "completed",
            "grants_found":  grants_found,
            "results_json":  results,
            "completed_at":  datetime.now(timezone.utc).isoformat(),
        }).eq("job_id", job_id).execute()
    except Exception as exc:
        logger.warning("DB log_completed failed: %s", exc)


def log_failed(job_id: str, error: str) -> None:
    c = _client()
    if not c:
        return
    try:
        c.table("analyses").update({
            "status":        "failed",
            "error_message": (error or "")[:1000],
        }).eq("job_id", job_id).execute()
    except Exception as exc:
        logger.warning("DB log_failed failed: %s", exc)
