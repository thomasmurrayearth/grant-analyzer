"""
Fetch and clean startup website content for analysis.

fetch_company_context() is the public entry point.  It assembles the
richest possible description of a company from any combination of:
  • a direct URL fetch
  • an automatic web-search fallback when the URL fails
  • text pasted or typed by the user
  • text extracted from an uploaded file (passed in as extra_text)

If the URL cannot be fetched (JavaScript-rendered site, bot protection,
etc.) the function silently falls back to 3 DuckDuckGo searches for the
company name extracted from the URL, so the analysis can still proceed.
"""

import asyncio
import re
import time
from typing import Callable

import httpx
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_REMOVE_TAGS = [
    "script", "style", "nav", "footer", "header", "aside",
    "noscript", "iframe", "svg", "form",
]


# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------

def _clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_REMOVE_TAGS):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [line.strip() for line in main.get_text(separator="\n").splitlines()]
    lines = [ln for ln in lines if len(ln) > 20]
    return "\n".join(lines)[:12000]


# ---------------------------------------------------------------------------
# Direct URL fetch
# ---------------------------------------------------------------------------

async def _fetch_url(url: str) -> str:
    """
    Fetch and clean a URL.  Returns cleaned text, or empty string on any
    failure (network error, HTTP error, JS-rendered blank page, etc.).
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=30
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content = _clean_html(response.text)
            return content if len(content) >= 100 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Company-name extraction from URL
# ---------------------------------------------------------------------------

def _company_hint_from_url(url: str) -> str:
    """
    Extract a likely company name from a URL.

    Examples:
      https://www.mittilabs.earth/  →  mittilabs
      https://thermify.cloud/       →  thermify
      https://www.openai.com/       →  openai
    """
    hint = re.sub(r"^https?://", "", url)
    hint = re.sub(r"^www\.", "", hint)
    hint = hint.split("/")[0]           # drop path
    parts = hint.split(".")
    # Take the second-to-last segment (before the TLD)
    return parts[-2] if len(parts) >= 2 else hint


# ---------------------------------------------------------------------------
# Web-search fallback
# ---------------------------------------------------------------------------

def _sync_search(query: str) -> str:
    """Single DuckDuckGo search.  Returns empty string on any failure."""
    try:
        from ddgs import DDGS
        for attempt in range(3):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=3))
                break
            except Exception:
                if attempt < 2:
                    time.sleep(2)
                else:
                    return ""
        parts = []
        for r in results:
            body = (r.get("body") or "")[:400]
            parts.append(
                f"Title: {r.get('title', '')}\n"
                f"URL:   {r.get('href', '')}\n"
                f"{body}"
            )
        return "\n\n".join(parts)
    except Exception:
        return ""


async def _fallback_search_context(
    hint: str,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """
    Run 3 searches for a company and return concatenated results.
    Called when the direct URL fetch returns too little content.
    """
    queries = [
        f'"{hint}" company technology product',
        f'"{hint}" startup about mission',
        f'"{hint}" site:linkedin.com OR site:crunchbase.com',
    ]
    parts = []
    for q in queries:
        if on_status:
            on_status(f'Searching for company info: "{q}"')
        result = await asyncio.to_thread(_sync_search, q)
        if result:
            parts.append(f"[Search: {q}]\n{result}")
        await asyncio.sleep(0.5)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def fetch_company_context(
    url: str | None,
    extra_text: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """
    Assemble the richest possible context about a company.

    Parameters
    ----------
    url        : company website URL (optional)
    extra_text : text pasted by the user or extracted from an uploaded
                 file (optional)
    on_status  : callback(message) for progress updates; called with
                 short human-readable strings as each step happens

    Returns
    -------
    (context_text, source_note)
    context_text  – combined text to pass into Phase 1
    source_note   – human-readable description of what was gathered
                    so the Phase 1 prompt can tell Claude the provenance
    """
    url_content   = ""
    source_parts  = []

    # ── URL path ────────────────────────────────────────────────────────────
    if url and url.strip():
        url = url.strip()
        if on_status:
            on_status("Fetching startup website…")
        url_content = await _fetch_url(url)

        if url_content:
            source_parts.append(f"Website content fetched directly from {url}.")
        else:
            # Fallback: extract a company name hint and search for it
            hint = _company_hint_from_url(url)
            if on_status:
                on_status(
                    f"Website couldn't be fetched directly "
                    f"(likely JavaScript-rendered or bot-protected). "
                    f"Searching the web for '{hint}'…"
                )
            url_content = await _fallback_search_context(hint, on_status=on_status)
            if url_content:
                source_parts.append(
                    f"Note: {url} could not be fetched directly. "
                    f"The following context was gathered via web search for '{hint}'."
                )
            else:
                source_parts.append(
                    f"Note: {url} could not be fetched and web search returned no results."
                )

    # ── User-supplied text ───────────────────────────────────────────────────
    parts: list[str] = []
    if url_content:
        parts.append(url_content)
    if extra_text and extra_text.strip():
        parts.append(
            "--- Additional context provided by user ---\n"
            + extra_text.strip()[:12000]
        )
        source_parts.append("Additional context was provided directly by the user.")

    if not parts:
        raise ValueError(
            "No company information available. "
            "Please provide a URL, paste some text, or upload a document."
        )

    if on_status:
        on_status("Analysing company information…")

    context     = "\n\n".join(parts)
    source_note = " ".join(source_parts)
    return context, source_note
