"""
Supabase analytics logging.  All functions are best-effort — they never
raise, so a database outage cannot break the main analysis pipeline.

Required environment variables (set in Railway / .env):
  SUPABASE_URL   — your project URL, e.g. https://xxxx.supabase.co
  SUPABASE_KEY   — your project service-role key

The full schema, including the funnel-analytics tables added in July 2026,
lives in `supabase_schema.sql` — run it once in the Supabase SQL editor.
Writes that use a column the database does not have yet are retried without
the new fields, so an un-migrated database degrades to the old behaviour
rather than losing the row entirely.
"""

import logging
import os

logger = logging.getLogger(__name__)

_supabase_client = None
_client_initialised = False

# Fields added after the original `analyses` table shipped. If an insert or
# update fails, we retry without these so a database that has not run the
# latest migration still records the row.
_ANALYSES_NEW_FIELDS = (
    "newsletter_opt_in", "cost_usd", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_write_tokens", "api_calls",
)


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


def _write_analyses(payload: dict, job_id: str | None, what: str) -> None:
    """Insert (job_id None) or update a row in `analyses`, retrying once
    without the newer columns if the database does not have them yet."""
    c = _client()
    if not c:
        return

    def _go(data: dict) -> None:
        if job_id is None:
            c.table("analyses").insert(data).execute()
        else:
            c.table("analyses").update(data).eq("job_id", job_id).execute()

    try:
        _go(payload)
    except Exception as exc:
        legacy = {k: v for k, v in payload.items() if k not in _ANALYSES_NEW_FIELDS}
        if legacy == payload:
            logger.warning("DB %s failed: %s", what, exc)
            return
        logger.warning("DB %s failed (%s) — retrying without new columns", what, exc)
        try:
            _go(legacy)
        except Exception as exc2:
            logger.warning("DB %s retry failed: %s", what, exc2)


def log_started(
    job_id: str,
    company_url: str,
    input_text: str,
    geographies: str,
    consortium: bool,
    accelerators: bool,
    prizes: bool,
    ip: str | None,
    email: str | None = None,
    newsletter_opt_in: bool = False,
) -> None:
    _write_analyses({
        "job_id":            job_id,
        "company_url":       company_url or None,
        "input_snippet":     (input_text or "")[:500] or None,
        "geographies":       geographies or None,
        "consortium":        consortium,
        "accelerators":      accelerators,
        "prizes":            prizes,
        "ip_address":        ip,
        "user_email":        email or None,
        "newsletter_opt_in": bool(newsletter_opt_in),
        "status":            "started",
    }, None, "log_started")


def log_profile_ready(job_id: str, company_name: str, profile: dict) -> None:
    _write_analyses({
        "company_name": company_name,
        "profile_json": profile,
        "status":       "profile_ready",
    }, job_id, "log_profile_ready")


def log_completed(
    job_id: str,
    grants_found: int,
    results: dict,
    usage: dict | None = None,
    cost_usd: float | None = None,
) -> None:
    from datetime import datetime, timezone
    payload = {
        "status":       "completed",
        "grants_found": grants_found,
        "results_json": results,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if usage:
        payload.update({
            "api_calls":          usage.get("api_calls"),
            "input_tokens":       usage.get("input_tokens"),
            "output_tokens":      usage.get("output_tokens"),
            "cache_read_tokens":  usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
        })
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    _write_analyses(payload, job_id, "log_completed")


def log_failed(job_id: str, error: str) -> None:
    _write_analyses({
        "status":        "failed",
        "error_message": (error or "")[:1000],
    }, job_id, "log_failed")


# ---------------------------------------------------------------------------
# Funnel analytics: events, feedback, waitlist
# ---------------------------------------------------------------------------

def log_event(
    event: str,
    job_id: str | None = None,
    ip: str | None = None,
    detail: str | None = None,
) -> None:
    """Record one funnel event (page view, CTA click, XLSX download, share...).
    Deliberately schema-light: `event` is a free-text name so new funnel steps
    can be measured without a migration."""
    c = _client()
    if not c:
        return
    try:
        c.table("events").insert({
            "event":      (event or "")[:60],
            "job_id":     job_id or None,
            "ip_address": ip,
            "detail":     (detail or "")[:200] or None,
        }).execute()
    except Exception as exc:
        logger.warning("DB log_event failed: %s", exc)


def log_feedback(
    job_id: str,
    rating: int,
    comment: str = "",
    may_contact: bool = False,
    email: str | None = None,
) -> None:
    c = _client()
    if not c:
        return
    try:
        c.table("feedback").insert({
            "job_id":      job_id,
            "rating":      rating,
            "comment":     (comment or "")[:2000] or None,
            "may_contact": bool(may_contact),
            "user_email":  email or None,
        }).execute()
    except Exception as exc:
        logger.warning("DB log_feedback failed: %s", exc)


def log_waitlist(email: str, reason: str = "", ip: str | None = None) -> None:
    """Email left when the app turned a run away (rate limit / at capacity)."""
    c = _client()
    if not c:
        return
    try:
        c.table("waitlist").insert({
            "user_email": email,
            "reason":     (reason or "")[:100] or None,
            "ip_address": ip,
        }).execute()
    except Exception as exc:
        logger.warning("DB log_waitlist failed: %s", exc)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_completed_result(job_id: str) -> dict | None:
    """Return a job-state-shaped dict for a completed analysis, read from
    Supabase.  Used to recover results after a server restart."""
    c = _client()
    if not c:
        return None
    try:
        rows = (
            c.table("analyses")
            .select("status,profile_json,results_json,company_name,grants_found")
            .eq("job_id", job_id)
            .eq("status", "completed")
            .execute()
        )
        if not rows.data:
            return None
        row = rows.data[0]
        return {
            "status":      "completed",
            "progress":    [],
            "stage":       4,
            "profile":     row.get("profile_json"),
            "source_note": "",
            "result":      row.get("results_json"),
            "error":       None,
            "prefs":       {},
        }
    except Exception as exc:
        logger.warning("DB get_completed_result failed: %s", exc)
        return None


def _since(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def get_recent_analyses(days: int = 30, limit: int = 200) -> list[dict] | None:
    """Return recent analyses (newest first) for the admin stats page.
    Returns None when the database is unavailable."""
    c = _client()
    if not c:
        return None
    columns = (
        "job_id,created_at,completed_at,company_name,company_url,geographies,"
        "status,grants_found,error_message,user_email,cost_usd,newsletter_opt_in"
    )
    for cols in (columns, "job_id,created_at,completed_at,company_name,company_url,"
                          "geographies,status,grants_found,error_message,user_email"):
        try:
            rows = (
                c.table("analyses")
                .select(cols)
                .gte("created_at", _since(days))
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return rows.data or []
        except Exception as exc:
            logger.warning("DB get_recent_analyses failed (%s): %s", cols[:20], exc)
    return None


def get_recent_events(days: int = 30) -> list[dict]:
    """Funnel events in the window. Empty list when unavailable — the admin
    page still renders without them."""
    c = _client()
    if not c:
        return []
    try:
        rows = (
            c.table("events")
            .select("event,created_at,job_id,detail")
            .gte("created_at", _since(days))
            .limit(5000)
            .execute()
        )
        return rows.data or []
    except Exception as exc:
        logger.warning("DB get_recent_events failed: %s", exc)
        return []


def get_events_named(event: str, days: int = 30, limit: int = 2000) -> list[dict]:
    """Every event with this name in the window.

    Separate from `get_recent_events` because that call is capped and dominated
    by high-volume funnel events: a rare marker event (say, a record that a
    scheduled job already ran) would be crowded out of it and silently read as
    absent, which for an idempotency check means doing the work twice.
    """
    c = _client()
    if not c:
        return []
    try:
        rows = (
            c.table("events")
            .select("event,created_at,job_id,detail")
            .eq("event", event)
            .gte("created_at", _since(days))
            .limit(limit)
            .execute()
        )
        return rows.data or []
    except Exception as exc:
        logger.warning("DB get_events_named(%s) failed: %s", event, exc)
        return []


def get_recent_results(days: int = 30, limit: int = 60) -> list[dict] | None:
    """Completed analyses in the window, with their full result payloads.

    This is what makes output quality reviewable. Usage data can only ever show
    that runs happened; the launch plan (§9a) is explicit that the two most
    damaging failure modes — output that can't be trusted, and output that
    isn't distinctive — are invisible in usage data and can only be found by
    reading the analyses themselves.

    Deliberately capped and ordered newest-first: result payloads are large, so
    this is for reading a recent sample rather than exporting the archive.
    Returns None when the database is unavailable, so callers can distinguish
    "nothing ran" from "couldn't look".
    """
    c = _client()
    if not c:
        return None
    try:
        rows = (
            c.table("analyses")
            .select("job_id,created_at,completed_at,company_name,company_url,"
                    "geographies,grants_found,cost_usd,results_json")
            .eq("status", "completed")
            .gte("created_at", _since(days))
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return rows.data or []
    except Exception as exc:
        logger.warning("DB get_recent_results failed: %s", exc)
        return None


def get_recent_feedback(days: int = 30) -> list[dict]:
    c = _client()
    if not c:
        return []
    try:
        rows = (
            c.table("feedback")
            .select("created_at,job_id,rating,comment,may_contact,user_email")
            .gte("created_at", _since(days))
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
        return rows.data or []
    except Exception as exc:
        logger.warning("DB get_recent_feedback failed: %s", exc)
        return []


def count_recent_runs_for_ip(ip: str, hours: int = 24) -> int | None:
    """How many analyses this IP has started in the window. None when the
    database is unavailable — callers must not rate-limit on None, or a
    database outage would lock every user out."""
    c = _client()
    if not c or not ip:
        return None
    try:
        rows = (
            c.table("analyses")
            .select("job_id")
            .eq("ip_address", ip)
            .gte("created_at", _since(hours / 24))
            .limit(200)
            .execute()
        )
        return len(rows.data or [])
    except Exception as exc:
        logger.warning("DB count_recent_runs_for_ip failed: %s", exc)
        return None
