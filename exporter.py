"""
Generate the grant-analysis XLSX report.

Three tabs:
  1. "Grant Opportunities"   — one row per grant, both the scored shortlist
     (result["opportunities"]) and the strategic watchlist
     (result["strategic_watchlist"]), so the download mirrors everything the
     web page shows. Columns A–V mirror the exemplar exactly; W (Applicant
     Route) and X (Status) are appended. Watchlist rows were never put
     through the full 3-axis scoring rubric, so Strategic Value and Ease of
     Execution default to 0 and Thematic Fit falls back to the analyzer's
     discovery-stage rating (see analyzer.py's _backfill_initial_thematic_fit)
     — only a grant with no rating at all renders "unknown" and drops the
     Priority Score formula.
  2. "Strategic Recommendations" — the narrative analysis, plus the offer of
     help writing the applications. The workbook is the artefact users forward
     to co-founders and boards, so it has to carry the offer with it.
  3. "Definitions acronyms"  — acronyms/abbreviations used in tab 1.
"""

import io
import os
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Shared styles (exemplar palette)
# ---------------------------------------------------------------------------

_HEADER_FILL  = PatternFill("solid", fgColor="073763")   # navy
_HEADER_FONT  = Font(bold=True, color="FFFFFF", size=11)
_HEADER_ALIGN = Alignment(wrap_text=True, vertical="top")
_DATA_ALIGN   = Alignment(wrap_text=True, vertical="top")
_LINK_FONT    = Font(color="1155CC", underline="single")

_SCALE_GREEN  = "57BB8A"   # colour-scale top for Priority Score / Funding
_SCALE_YELLOW = "FFD966"   # colour-scale top for the 1-5 sub-scores
_EXPIRED_RED  = "EA9999"   # past-deadline highlight


def _style_header_row(ws) -> None:
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN


# ---------------------------------------------------------------------------
# Value coercion helpers
# ---------------------------------------------------------------------------

_FUNDING_RE = re.compile(
    r"(?:€|\bEUR\s?)\s*([\d][\d.,]*)\s*(million|billion|bn|mn|m\b|k\b)?",
    re.IGNORECASE,
)

_MULTIPLIERS = {
    "k": 1_000, "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000,
    "bn": 1_000_000_000, "billion": 1_000_000_000,
}


def _max_funding_eur(opp: dict) -> int | None:
    """Numeric euro value for column J, or None when no estimate exists."""
    val = opp.get("max_funding_eur")
    if isinstance(val, (int, float)) and val > 0:
        return round(val)
    if isinstance(val, str):
        try:
            return round(float(val.replace(",", "").replace("€", "")))
        except ValueError:
            pass
    # Fallback for results produced before max_funding_eur existed: parse
    # euro-denominated amounts out of the free-text field. Other currencies
    # are left blank rather than converted with a made-up exchange rate,
    # and equity components ("€2.5m grant + €15m equity") don't count as
    # grant funding.
    text = str(opp.get("max_funding") or "")
    amounts = []
    for m in _FUNDING_RE.finditer(text):
        if re.match(r"\s*(equity|investment)", text[m.end():], re.IGNORECASE):
            continue
        try:
            value = float(m.group(1).replace(",", "").rstrip("."))
        except ValueError:
            continue
        amounts.append(value * _MULTIPLIERS.get((m.group(2) or "").lower().strip(), 1))
    return round(max(amounts)) if amounts else None


_ORDINAL_RE = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

_DATE_FORMATS = (
    "%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y",
)


def _as_excel_date(text) -> datetime | None:
    """
    Return a real datetime when the whole cell value is a specific
    day-level date, so the past-deadline conditional rule can fire.
    Descriptive values ("rolling", "July 2026 (window Feb–Jul)") stay text.
    """
    s = _ORDINAL_RE.sub(r"\1", re.sub(r"\s+", " ", str(text or "").strip()))
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Strategic watchlist → Grant Opportunities row shape
#
# Watchlist items (result["strategic_watchlist"]) carry a smaller, differently
# named field set than scored opportunities (see analyzer.py). This maps the
# fields that have a genuine analogue onto the opportunity keys _tab_
# opportunities() already reads via .get(), so a watchlist row can sit in the
# same table. Anything with no analogue is left unset — .get() then returns
# None and the cell renders as "unknown", same as a scored opportunity with a
# missing field.
# ---------------------------------------------------------------------------


def _opportunity_row_from_watchlist(item: dict) -> dict:
    return {
        "name":                      item.get("name", ""),
        "thematic_fit_explanation":  item.get("thematic_relevance", ""),
        "thematic_fit_score":        item.get("thematic_fit"),
        "funding_type":              item.get("funding_type", ""),
        "max_funding":               item.get("max_funding", ""),
        "application_link":          item.get("application_link", ""),
        "managing_body":             item.get("managing_body", ""),
        "reason_for_inclusion":      item.get("what_would_unlock", ""),
        "geography":                 item.get("geography", ""),
        "application_timing":        item.get("application_timing", ""),
        "reason_for_caution":        item.get("why_watchlist", ""),
        "applicant_type_match":      item.get("application_route", ""),
        "status":                    item.get("status", ""),
    }


# ---------------------------------------------------------------------------
# Tab 1 — Grant Opportunities
#
# Column map (A–V mirrors the exemplar; W–X are our additions):
#   A  Grant Name
#   B  Priority Score          ← live Excel formula: =(2*D{r})+F{r}+H{r}
#   C  Thematic Fit (explanation)
#   D  Thematic Fit score (1-5)
#   E  Strategic Value (explanation)
#   F  Strategic Value Score (1–5)
#   G  Ease of Execution (explanation)
#   H  Ease of Execution Score (1–5)
#   I  Funding Level (explanation)
#   J  Maximum Funding Level (Estimated by AI, Euros)   ← numeric, "€"#,##0
#   K  Application Link         ← real hyperlink
#   L  Managing Body
#   M  Grant opens for applications (date)
#   N  Grant closes for applications (date)             ← real date if parseable
#   O  Project Size / Duration
#   P  Past Similar Projects
#   Q  Alignment Conditions
#   R  TRL Requirement
#   S  Consortium Rules
#   T  Geography / Eligibility
#   U  Application Timing
#   V  Compliance / Risks
#   W  Applicant Route
#   X  Status
# ---------------------------------------------------------------------------

_HEADERS = [
    "Grant Name",
    "Priority Score",
    "Thematic Fit (explanation)",
    "Thematic Fit score (1-5)",
    "Strategic Value (explanation)",
    "Strategic Value Score (1–5)",
    "Ease of Execution (explanation)",
    "Ease of Execution Score (1–5)",
    "Funding Level (explanation)",
    "Maximum Funding Level (Estimated by AI, Euros)",
    "Application Link",
    "Managing Body",
    "Grant opens for applications (date)",
    "Grant closes for applications (date)",
    "Project Size / Duration",
    "Past Similar Projects",
    "Alignment Conditions",
    "TRL Requirement",
    "Consortium Rules",
    "Geography / Eligibility",
    "Application Timing",
    "Compliance / Risks",
    "Applicant Route",
    "Status",
]

_COLUMN_WIDTHS = {
    "A": 20.63, "B": 7.88,  "C": 35.25, "D": 9.5,
    "E": 18.13, "F": 13.0,  "G": 17.0,  "H": 10.63,
    "I": 17.88, "J": 14.25, "K": 19.38, "L": 13.5,
    "M": 13.0,  "N": 13.0,  "O": 14.25, "P": 22.5,
    "Q": 35.0,  "R": 18.13, "S": 22.5,  "T": 15.88,
    "U": 18.13, "V": 25.0,  "W": 18.0,  "X": 14.0,
}

_ROUTE_LABELS = {
    "direct":     "Direct applicant",
    "partner":    "Partner route only",
    "ineligible": "Potential ineligible — verify",
}


def _txt(value) -> str:
    """Text cell value — never blank; empty/missing becomes 'unknown'."""
    s = str(value).strip() if value is not None else ""
    return s or "unknown"


def _tab_opportunities(wb: Workbook, opportunities: list[dict]) -> None:
    ws = wb.active
    ws.title = "Grant Opportunities"

    ws.append(_HEADERS)
    _style_header_row(ws)
    ws.freeze_panes = "B2"

    for opp in opportunities:
        apt = (opp.get("applicant_type_match") or "direct").lower()
        apt_label = _ROUTE_LABELS.get(apt, apt.replace("_", " ").title())

        # Funding level explanation: combine type + amount + confidence
        funding_parts = [p for p in [
            opp.get("funding_type", ""),
            opp.get("max_funding", ""),
            ("Confidence: " + opp.get("funding_confidence", "")) if opp.get("funding_confidence") else "",
        ] if p]
        funding_explanation = " | ".join(funding_parts)

        # Application timing: recurrence pattern + current window
        timing_parts = [p for p in [
            opp.get("recurrence", ""),
            (opp.get("application_timing", "") or "").replace("_", " "),
        ] if p and p.lower() != "unknown"]
        timing = "; ".join(timing_parts) or "unknown"

        link = opp.get("application_link", "")
        closes = opp.get("deadline", "")

        # Thematic fit has no default — a grant with no rating at all (scored
        # or the discovery-stage fallback the analyzer backfills onto
        # unscored watchlist items) renders as "unknown" and skips the
        # formula below (a text score would make it render as #VALUE!).
        # Strategic value and ease of execution DO default to 0: they only
        # exist for grants that went through the full scoring rubric, and
        # treating an unscored grant as "0" on those axes (rather than
        # "unknown") lets every row still get a live Priority Score driven
        # by thematic fit.
        thematic_fit = opp.get("thematic_fit_score")
        strategic_value = opp.get("strategic_value_score")
        if not isinstance(strategic_value, (int, float)):
            strategic_value = 0
        ease = opp.get("ease_score")
        if not isinstance(ease, (int, float)):
            ease = 0
        have_scores = isinstance(thematic_fit, (int, float))

        ws.append([
            _txt(opp.get("name")),
            None,                                          # B — written below
            _txt(opp.get("thematic_fit_explanation")),
            thematic_fit if have_scores else "unknown",
            _txt(opp.get("strategic_value_explanation")),
            strategic_value,
            _txt(opp.get("ease_explanation")),
            ease,
            _txt(funding_explanation),
            _max_funding_eur(opp) or "unknown",
            _txt(link),
            _txt(opp.get("managing_body")),
            _txt(opp.get("opens_date")),
            _as_excel_date(closes) or _txt(closes),
            _txt(opp.get("project_size_duration")),
            _txt(opp.get("past_similar_projects")),
            _txt(opp.get("reason_for_inclusion")),
            _txt(opp.get("trl_requirement")),
            _txt(opp.get("consortium_rules")),
            _txt(opp.get("geography")),
            timing,
            _txt(opp.get("reason_for_caution")),
            apt_label,
            _txt(opp.get("status")),
        ])

        row_num = ws.max_row
        if have_scores:
            ws[f"B{row_num}"] = f"=(2*D{row_num})+F{row_num}+H{row_num}"
        else:
            ws[f"B{row_num}"] = "unknown"

        for cell in ws[row_num]:
            cell.alignment = _DATA_ALIGN

        ws[f"J{row_num}"].number_format = '"€"#,##0'
        if isinstance(ws[f"N{row_num}"].value, datetime):
            ws[f"N{row_num}"].number_format = "dd mmmm yyyy"
        if isinstance(link, str) and link.startswith("http"):
            k_cell = ws[f"K{row_num}"]
            k_cell.hyperlink = link
            k_cell.font = _LINK_FONT

    for col_letter, width in _COLUMN_WIDTHS.items():
        ws.column_dimensions[col_letter].width = width

    last = ws.max_row
    if last < 2:
        return

    # Conditional formatting, matching the exemplar:
    #   B (Priority Score) and J (Max Funding)  white → green scales
    #   D / F / H (1-5 sub-scores)              white → yellow scales
    #   N (closing date)                        red when a real date is past
    def _scale(top_colour: str) -> ColorScaleRule:
        return ColorScaleRule(
            start_type="min", start_color="FFFFFF",
            end_type="max", end_color=top_colour,
        )

    ws.conditional_formatting.add(f"B2:B{last}", _scale(_SCALE_GREEN))
    for col in ("D", "F", "H"):
        ws.conditional_formatting.add(f"{col}2:{col}{last}", _scale(_SCALE_YELLOW))
    ws.conditional_formatting.add(f"J2:J{last}", _scale(_SCALE_GREEN))
    ws.conditional_formatting.add(
        f"N2:N{last}",
        FormulaRule(
            formula=[f"AND(ISNUMBER(N2),TRUNC(N2)<TODAY())"],
            fill=PatternFill(
                start_color=_EXPIRED_RED, end_color=_EXPIRED_RED, fill_type="solid"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Tab 2 — Strategic Recommendations (+ the offer of help)
# ---------------------------------------------------------------------------

# Where "get help with your application" points. Overridable so the link can
# follow a custom domain without a code change.
CONSULTING_URL = os.environ.get(
    "CONSULTING_URL", "https://thomasmurray.earth/startup-journey.html"
).strip()
CONSULTING_EMAIL = os.environ.get("CONSULTING_EMAIL", "thomasmurraynz@gmail.com").strip()

_REC_FIELDS = [
    ("best_fit_strategy",  "Best fit strategy"),
    ("best_geographies",   "Best geographies"),
    ("strongest_pathways", "Strongest pathways"),
    ("key_partnerships",   "Key partnerships"),
    ("capability_gaps",    "Capability gaps"),
    ("key_risks",          "Key risks"),
]

_SUMMARY_FIELDS = [
    ("company_overview",      "Company overview"),
    ("strongest_themes",      "Strongest themes"),
    ("strongest_geographies", "Strongest geographies"),
    ("key_constraints",       "Key constraints"),
]


def _tab_recommendations(wb: Workbook, result: dict) -> None:
    ws = wb.create_sheet("Strategic Recommendations")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 110

    ws.append(["Section", "Detail"])
    _style_header_row(ws)

    summary = result.get("executive_summary") or {}
    recs    = result.get("strategic_recommendations") or {}

    def _section(title: str, fields: list[tuple[str, str]], source: dict) -> None:
        wrote_any = False
        for key, label in fields:
            value = str(source.get(key) or "").strip()
            if not value:
                continue
            if not wrote_any:
                ws.append([title, ""])
                ws[f"A{ws.max_row}"].font = Font(bold=True)
                wrote_any = True
            ws.append([label, value])
            ws[f"B{ws.max_row}"].alignment = _DATA_ALIGN
            ws[f"A{ws.max_row}"].alignment = _DATA_ALIGN

    _section("Executive summary", _SUMMARY_FIELDS, summary)
    _section("Strategic recommendations", _REC_FIELDS, recs)

    # The offer. Deliberately last: the reader has just finished the analysis.
    ws.append([])
    ws.append(["Want help winning a grant?", ""])
    ws[f"A{ws.max_row}"].font = Font(bold=True)
    ws.append([
        "About this report",
        "I'm Thomas Murray. I help climate and deep tech startups win grant funding "
        "and achieve profitability. This tool is a productised version of an "
        "opportunity assessment I've done for several startups. If you want help "
        "turning a grant opportunity into a complete application, contact me.",
    ])
    ws[f"B{ws.max_row}"].alignment = _DATA_ALIGN
    ws[f"A{ws.max_row}"].alignment = _DATA_ALIGN

    ws.append(["Work with Thomas", CONSULTING_URL])
    link_cell = ws[f"B{ws.max_row}"]
    link_cell.hyperlink = CONSULTING_URL
    link_cell.font = _LINK_FONT

    ws.append(["Contact", CONSULTING_EMAIL])
    mail_cell = ws[f"B{ws.max_row}"]
    mail_cell.hyperlink = f"mailto:{CONSULTING_EMAIL}"
    mail_cell.font = _LINK_FONT


# ---------------------------------------------------------------------------
# Tab 3 — Definitions acronyms
# ---------------------------------------------------------------------------

# Fallback dictionary of grant-world acronyms, used only when the analysis
# result carries no LLM-generated acronym_definitions (e.g. results saved
# before that pipeline step existed). Deliberately general — spans EU, UK,
# US, and global programme vocabulary rather than any one funder.
_FALLBACK_ACRONYMS = {
    "AI":     "Artificial Intelligence",
    "ARPA-E": "Advanced Research Projects Agency–Energy (US Department of Energy)",
    "CSA":    "Coordination and Support Action (Horizon Europe funding type focused on networking and coordination)",
    "DOE":    "United States Department of Energy",
    "EIC":    "European Innovation Council",
    "EIT":    "European Institute of Innovation and Technology",
    "ERDF":   "European Regional Development Fund",
    "ESG":    "Environmental, Social and Governance",
    "EU":     "European Union",
    "FTE":    "Full-Time Equivalent",
    "IA":     "Innovation Action (Horizon Europe funding instrument typically focused on demonstration and piloting)",
    "IP":     "Intellectual Property",
    "LCA":    "Life Cycle Assessment",
    "LIFE":   "EU LIFE Programme (funding programme for environment, climate, and circular economy)",
    "MRV":    "Measurement, Reporting and Verification",
    "NGO":    "Non-Governmental Organisation",
    "NHS":    "National Health Service (United Kingdom)",
    "ODA":    "Official Development Assistance",
    "POC":    "Proof of Concept",
    "PPP":    "Public-Private Partnership",
    "R&D":    "Research and Development",
    "RIA":    "Research and Innovation Action (Horizon Europe funding instrument typically focused on research)",
    "RTO":    "Research and Technology Organisation",
    "SBIR":   "Small Business Innovation Research (US federal funding programme)",
    "SIF":    "Strategic Innovation Fund (Ofgem programme for energy network innovation)",
    "SME":    "Small and Medium-sized Enterprise",
    "TRL":    "Technology Readiness Level (scale from 1–9 measuring maturity of a technology)",
    "UK":     "United Kingdom",
    "UKRI":   "UK Research and Innovation",
    "US":     "United States",
    "VC":     "Venture Capital",
}


def _fallback_definitions(opportunities: list[dict]) -> list[dict]:
    """Scan the tab-1 text for known acronyms and define the ones present."""
    corpus = " ".join(
        str(v) for opp in opportunities for v in opp.values() if isinstance(v, str)
    )
    found = []
    for acro, definition in _FALLBACK_ACRONYMS.items():
        pattern = r"(?<![A-Za-z0-9])" + re.escape(acro) + r"(?![A-Za-z0-9])"
        if re.search(pattern, corpus):
            found.append({"acronym": acro, "definition": definition})
    return found


def _tab_acronyms(wb: Workbook, result: dict, opportunities: list[dict]) -> None:
    ws = wb.create_sheet("Definitions acronyms")
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 70.75

    ws.append(["Acronym", "Definition"])
    _style_header_row(ws)

    definitions = [
        d for d in (result.get("acronym_definitions") or [])
        if isinstance(d, dict) and d.get("acronym") and d.get("definition")
    ] or _fallback_definitions(opportunities)

    seen: set[str] = set()
    for item in sorted(definitions, key=lambda d: str(d["acronym"]).upper()):
        acro = str(item["acronym"]).strip()
        if acro.upper() in seen:
            continue
        seen.add(acro.upper())
        ws.append([acro, str(item["definition"]).strip()])
        ws[f"B{ws.max_row}"].alignment = _DATA_ALIGN


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_xlsx(result: dict) -> bytes:
    """Return an XLSX file as bytes from a completed analysis result dict."""
    wb = Workbook()

    watchlist_rows = [
        _opportunity_row_from_watchlist(item)
        for item in result.get("strategic_watchlist", [])
    ]
    # Sort by priority_score: scored shortlist rows have a real value, so
    # they sort above watchlist rows (which lack the key and default to 0)
    # without needing a separate tier column.
    opportunities = sorted(
        list(result.get("opportunities", [])) + watchlist_rows,
        key=lambda o: o.get("priority_score", 0) or 0,
        reverse=True,
    )

    _tab_opportunities(wb, opportunities)
    _tab_recommendations(wb, result)
    _tab_acronyms(wb, result, opportunities)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
