"""
Generate a multi-tab XLSX report from the grant analysis result.
"""

import io
from openpyxl import Workbook
from openpyxl.styles import (
    Alignment, Border, Font, PatternFill, Side
)
from openpyxl.utils import get_column_letter

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_TIER_COLOURS = {
    "Must Pursue":              ("1A7A4A", "FFFFFF"),   # dark green / white
    "Big Bet":                  ("6B21A8", "FFFFFF"),   # purple / white
    "Quick Win":                ("1D4ED8", "FFFFFF"),   # blue / white
    "Prepare for Next Window":  ("0F766E", "FFFFFF"),   # teal / white
    "Strategic Positioning":    ("B45309", "FFFFFF"),   # amber / white
    "Low Priority":             ("6B7280", "FFFFFF"),   # grey / white
}

_HEADER_FILL     = PatternFill("solid", fgColor="073763")   # matches Mitti navy
_HEADER_FONT     = Font(bold=True, color="FFFFFF", size=10)
_ALT_FILL        = PatternFill("solid", fgColor="F0F4F8")
_SECTION_FILL    = PatternFill("solid", fgColor="1E3A5F")

_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _header_row(ws, cols: list[str]) -> None:
    ws.append(cols)
    for cell in ws[ws.max_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = _BORDER
    ws.row_dimensions[ws.max_row].height = 36


def _auto_widths(ws, min_w: int = 12, max_w: int = 50) -> None:
    for col_idx, col_cells in enumerate(ws.columns, 1):
        width = min_w
        for cell in col_cells:
            try:
                cell_len = len(str(cell.value or ""))
                width = min(max(width, cell_len + 2), max_w)
            except Exception:
                pass
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _tier_fill(tier: str) -> PatternFill | None:
    colours = _TIER_COLOURS.get(tier)
    if colours:
        return PatternFill("solid", fgColor=colours[0])
    return None


def _tier_font(tier: str) -> Font:
    colours = _TIER_COLOURS.get(tier)
    if colours:
        return Font(bold=True, color=colours[1], size=10)
    return Font(size=10)


# ---------------------------------------------------------------------------
# Tab 1 — Grant Opportunities  (matches Mitti layout)
#
# Column map (A–V mirrors Mitti exactly; W–Z are our bonus fields):
#   A  Grant Name
#   B  Priority Score          ← live Excel formula: =(2*D{r})+F{r}+H{r}
#   C  Thematic Fit (explanation)
#   D  Thematic Fit Score (1-5)
#   E  Strategic Value (explanation)
#   F  Strategic Value Score (1-5)
#   G  Ease of Execution (explanation)
#   H  Ease of Execution Score (1-5)
#   I  Funding Level (explanation)
#   J  Maximum Funding Level
#   K  Application Link
#   L  Managing Body
#   M  Grant opens for applications
#   N  Grant closes / recurrence
#   O  Project Size / Duration
#   P  Past Similar Projects
#   Q  Alignment Conditions
#   R  TRL Requirement
#   S  Consortium Rules
#   T  Geography / Eligibility
#   U  Application Timing
#   V  Compliance / Risks
#   W  Priority Tier           ← bonus
#   X  Applicant Route         ← bonus
#   Y  Status                  ← bonus
# ---------------------------------------------------------------------------

def _tab_opportunities(wb: Workbook, opportunities: list[dict]) -> None:
    ws = wb.active
    ws.title = "Grant Opportunities"

    headers = [
        "Grant Name",
        "Priority Score",
        "Thematic Fit (explanation)",
        "Thematic Fit Score (1-5)",
        "Strategic Value (explanation)",
        "Strategic Value Score (1-5)",
        "Ease of Execution (explanation)",
        "Ease of Execution Score (1-5)",
        "Funding Level (explanation)",
        "Maximum Funding Level",
        "Application Link",
        "Managing Body",
        "Grant opens for applications",
        "Grant closes / recurrence",
        "Project Size / Duration",
        "Past Similar Projects",
        "Alignment Conditions",
        "TRL Requirement",
        "Consortium Rules",
        "Geography / Eligibility",
        "Application Timing",
        "Compliance / Risks",
        "Priority Tier",
        "Applicant Route",
        "Status",
    ]
    ws.append(headers)
    hdr_row = ws.max_row
    for cell in ws[hdr_row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = _BORDER
    ws.row_dimensions[hdr_row].height = 36
    ws.freeze_panes = "A2"

    for opp in opportunities:
        tier = opp.get("priority_tier", "")
        apt  = (opp.get("applicant_type_match") or "direct").lower()
        apt_label = {
            "direct":     "Direct applicant",
            "partner":    "Partner route only",
            "ineligible": "Potential ineligible — verify",
        }.get(apt, apt.replace("_", " ").title())

        # Funding level explanation: combine type + amount + confidence
        funding_parts = [p for p in [
            opp.get("funding_type", ""),
            opp.get("max_funding", ""),
            ("Confidence: " + opp.get("funding_confidence", "")) if opp.get("funding_confidence") else "",
        ] if p]
        funding_explanation = " | ".join(funding_parts)

        data_row = [
            opp.get("name", ""),
            None,                                          # B — formula written below
            opp.get("thematic_fit_explanation", ""),
            opp.get("thematic_fit_score", ""),
            opp.get("strategic_value_explanation", ""),
            opp.get("strategic_value_score", ""),
            opp.get("ease_explanation", ""),
            opp.get("ease_score", ""),
            funding_explanation,
            opp.get("max_funding", ""),
            opp.get("application_link", ""),
            opp.get("managing_body", ""),
            opp.get("deadline", ""),
            opp.get("recurrence", ""),
            opp.get("project_size_duration", ""),
            opp.get("past_similar_projects", ""),
            opp.get("reason_for_inclusion", ""),
            opp.get("trl_requirement", ""),
            opp.get("consortium_rules", ""),
            opp.get("geography", ""),
            (opp.get("application_timing", "") or "").replace("_", " "),
            opp.get("reason_for_caution", ""),
            tier,
            apt_label,
            opp.get("status", ""),
        ]
        ws.append(data_row)

        row_num = ws.max_row

        # Write Priority Score as a live formula
        ws[f"B{row_num}"] = f"=(2*D{row_num})+F{row_num}+H{row_num}"

        # Style data cells
        for col_idx, cell in enumerate(ws[row_num], 1):
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = _BORDER

            # Colour the Priority Tier cell (col W = 23)
            if col_idx == 23:
                tf = _tier_fill(tier)
                if tf:
                    cell.fill = tf
                    cell.font = _tier_font(tier)
                    continue
            # No alternating fill — plain white like Mitti

    # Column widths matching Mitti (A–V), sensible defaults for W–Y
    widths = {
        "A": 20.63, "B": 7.88,  "C": 35.25, "D": 9.5,
        "E": 18.13, "F": 13.0,  "G": 17.0,  "H": 10.63,
        "I": 17.88, "J": 14.25, "K": 19.38, "L": 13.5,
        "M": 13.0,  "N": 13.0,  "O": 14.25, "P": 22.5,
        "Q": 35.0,  "R": 18.13, "S": 22.5,  "T": 15.88,
        "U": 18.13, "V": 25.0,  "W": 22.0,  "X": 18.0,
        "Y": 14.0,
    }
    for col_letter, width in widths.items():
        ws.column_dimensions[col_letter].width = width


# ---------------------------------------------------------------------------
# Tab 2 — Priority Matrix
# ---------------------------------------------------------------------------

_TIER_ORDER = [
    "Must Pursue",
    "Big Bet",
    "Quick Win",
    "Prepare for Next Window",
    "Strategic Positioning",
    "Low Priority",
]

_TIER_DESCRIPTIONS = {
    "Must Pursue":             "High fit + high strategic value + open or known future window + realistic route",
    "Big Bet":                 "Transformational but difficult — worth investing in if capacity allows",
    "Quick Win":               "Open now, manageable effort, good fit — act immediately",
    "Prepare for Next Window": "Good fit and clear route — no current call but likely to recur",
    "Strategic Positioning":   "Useful for signalling and ecosystem access",
    "Low Priority":            "Weak fit, timing unknown, or unrealistic — monitor only",
}


def _tab_priority(wb: Workbook, opportunities: list[dict]) -> None:
    ws = wb.create_sheet("Priority Matrix")

    by_tier: dict[str, list[dict]] = {t: [] for t in _TIER_ORDER}
    for opp in opportunities:
        tier = opp.get("priority_tier", "Low Priority")
        if tier not in by_tier:
            tier = "Low Priority"
        by_tier[tier].append(opp)

    for tier in _TIER_ORDER:
        opps = sorted(by_tier[tier], key=lambda o: o.get("priority_score", 0), reverse=True)
        if not opps:
            continue

        # Tier heading row
        ws.append([tier, _TIER_DESCRIPTIONS[tier]])
        heading_row = ws.max_row
        tf = _tier_fill(tier)
        for cell in ws[heading_row]:
            if tf:
                cell.fill = tf
                cell.font = _tier_font(tier)
            cell.alignment = Alignment(wrap_text=False, vertical="center")
        ws.row_dimensions[heading_row].height = 22
        ws.merge_cells(f"C{heading_row}:G{heading_row}")

        # Column headers
        _header_row(ws, ["Name", "Managing Body", "Geography", "Priority Score",
                          "Thematic Fit", "Strategic Value", "Ease", "Status", "Max Funding"])

        for opp in opps:
            row = [
                opp.get("name", ""),
                opp.get("managing_body", ""),
                opp.get("geography", ""),
                opp.get("priority_score", ""),
                opp.get("thematic_fit_score", ""),
                opp.get("strategic_value_score", ""),
                opp.get("ease_score", ""),
                opp.get("status", ""),
                opp.get("max_funding", ""),
            ]
            ws.append(row)
            for cell in ws[ws.max_row]:
                cell.border = _BORDER
                cell.alignment = Alignment(vertical="top")
            ws.row_dimensions[ws.max_row].height = 18

        ws.append([])  # spacer

    _auto_widths(ws)


# ---------------------------------------------------------------------------
# Tab 3 — Strategic Recommendations
# ---------------------------------------------------------------------------


def _tab_recommendations(wb: Workbook, result: dict) -> None:
    ws = wb.create_sheet("Strategic Recommendations")
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 80

    profile = result.get("company_profile", {})
    summary = result.get("executive_summary", {})
    recs    = result.get("strategic_recommendations", {})

    sections = [
        ("COMPANY PROFILE", None),
        ("Company", profile.get("name", "")),
        ("Classification", profile.get("classification", "")),
        ("Value Proposition", profile.get("value_proposition", "")),
        ("Technology", profile.get("technology", "")),
        ("HQ", profile.get("hq", "")),
        ("Operational Geographies", ", ".join(profile.get("operational_geographies", []))),
        ("Stage", profile.get("stage", "")),
        ("TRL", profile.get("trl", "")),
        ("", ""),
        ("EXECUTIVE SUMMARY", None),
        ("Company Overview", summary.get("company_overview", "")),
        ("Strongest Themes", summary.get("strongest_themes", "")),
        ("Strongest Geographies", summary.get("strongest_geographies", "")),
        ("Key Constraints", summary.get("key_constraints", "")),
        ("", ""),
        ("STRATEGIC RECOMMENDATIONS", None),
        ("Best Fit Strategy", recs.get("best_fit_strategy", "")),
        ("Best Geographies", recs.get("best_geographies", "")),
        ("Strongest Pathways", recs.get("strongest_pathways", "")),
        ("Key Partnerships", recs.get("key_partnerships", "")),
        ("Capability Gaps", recs.get("capability_gaps", "")),
        ("Key Risks", recs.get("key_risks", "")),
    ]

    section_fill = PatternFill("solid", fgColor="1E3A5F")
    section_font = Font(bold=True, color="FFFFFF", size=11)

    for label, value in sections:
        if value is None:
            ws.append([label, ""])
            row_num = ws.max_row
            ws[f"A{row_num}"].fill = section_fill
            ws[f"A{row_num}"].font = section_font
            ws[f"B{row_num}"].fill = section_fill
            ws.row_dimensions[row_num].height = 24
        else:
            ws.append([label, value])
            row_num = ws.max_row
            ws[f"A{row_num}"].font = Font(bold=True, size=10)
            ws[f"A{row_num}"].alignment = Alignment(vertical="top")
            ws[f"B{row_num}"].alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[row_num].height = max(18, min(len(str(value)) // 3, 80))


# ---------------------------------------------------------------------------
# Tab 4 — Strategic Watchlist
# ---------------------------------------------------------------------------

_WATCHLIST_FILL = PatternFill("solid", fgColor="FFF8E1")   # pale amber rows
_WATCHLIST_ALT  = PatternFill("solid", fgColor="FFFFFF")
_WATCHLIST_HDR  = PatternFill("solid", fgColor="92400E")   # amber-900


def _tab_watchlist(wb: Workbook, watchlist: list[dict]) -> None:
    ws = wb.create_sheet("Strategic Watchlist")

    cols = [
        "Name", "Managing Body", "Geography",
        "Opportunity Type", "Application Route", "Status",
        "Funding Type", "Max Funding",
        "Thematic Relevance", "Why Not Main Recommendation", "What Would Unlock",
        "Application Link",
    ]
    # Custom amber header
    ws.append(cols)
    hdr_fill = PatternFill("solid", fgColor="92400E")
    hdr_font = Font(bold=True, color="FFFFFF", size=10)
    for cell in ws[ws.max_row]:
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = _BORDER
    ws.row_dimensions[ws.max_row].height = 36
    ws.freeze_panes = "A2"

    for i, item in enumerate(watchlist, 1):
        row = [
            item.get("name", ""),
            item.get("managing_body", ""),
            item.get("geography", ""),
            (item.get("opportunity_type", "") or "").replace("_", " "),
            (item.get("application_route", "") or "").replace("_", " "),
            item.get("status", ""),
            item.get("funding_type", ""),
            item.get("max_funding", ""),
            item.get("thematic_relevance", ""),
            item.get("why_watchlist", ""),
            item.get("what_would_unlock", ""),
            item.get("application_link", ""),
        ]
        ws.append(row)
        row_num = ws.max_row
        fill = _WATCHLIST_FILL if i % 2 else _WATCHLIST_ALT
        for cell in ws[row_num]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = _BORDER
            cell.fill = fill
        ws.row_dimensions[row_num].height = 60

    _auto_widths(ws)
    # Widen the three narrative columns
    ws.column_dimensions["I"].width = 45   # Thematic Relevance
    ws.column_dimensions["J"].width = 40   # Why Not Main
    ws.column_dimensions["K"].width = 40   # What Would Unlock


# ---------------------------------------------------------------------------
# Tab 5 — Rubrics Reference
# ---------------------------------------------------------------------------


def _tab_rubrics(wb: Workbook) -> None:
    ws = wb.create_sheet("Rubrics Reference")
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 65

    rubrics = [
        ("THEMATIC FIT", "", "Does the grant target this company's core problem?"),
        ("Score", "Meaning", "Guidance"),
        ("5 — Core alignment", "5", "Company is exactly what this fund exists to support"),
        ("4 — Strong alignment", "4", "Company clearly relevant, strong natural fit"),
        ("3 — Moderate alignment", "3", "Partial relevance; co-benefits present but not primary"),
        ("2 — Weak alignment", "2", "Secondary co-benefit being stretched as primary claim"),
        ("1 — Poor alignment", "1", "Peripheral or forced relevance"),
        ("", "", ""),
        ("STRATEGIC VALUE", "", "Does this fund create leverage beyond its direct funding?"),
        ("Score", "Meaning", "Guidance"),
        ("5 — Transformational", "5", "Unlocks major new market, geography, or institutional credibility"),
        ("4 — High", "4", "Strong investor signal, ecosystem positioning, or follow-on leverage"),
        ("3 — Moderate", "3", "Useful credibility or network access"),
        ("2 — Limited", "2", "Modest benefit beyond direct funding"),
        ("1 — Minimal", "1", "Little strategic value"),
        ("", "", ""),
        ("EASE OF APPLICATION", "", "How difficult is it to prepare and submit a credible bid?"),
        ("Score", "Meaning", "Guidance"),
        ("5 — Very easy", "5", "Simple online form, solo application, low burden"),
        ("4 — Easy", "4", "Standard short proposal"),
        ("3 — Moderate", "3", "Full technical proposal, moderate reporting"),
        ("2 — Difficult", "2", "Consortium or co-funding required, complex process"),
        ("1 — Very difficult", "1", "Multi-partner, political, or highly regulated process"),
        ("", "", ""),
        ("PRIORITY SCORE FORMULA", "", ""),
        ("", "", "Priority Score = (0.45 × Thematic Fit) + (0.35 × Strategic Value) + (0.20 × Ease)"),
        ("", "", ""),
        ("PRIORITY TIERS", "", ""),
        ("Must Pursue", "", "Priority ≥ 3.5 AND Thematic Fit ≥ 4"),
        ("Big Bet", "", "Thematic Fit ≥ 4 but Ease ≤ 2, or Strategic Value = 5 but difficult"),
        ("Quick Win", "", "Ease ≥ 4 AND Priority ≥ 3.0 AND Thematic Fit ≥ 3"),
        ("Strategic Positioning", "", "Strategic Value ≥ 4 but Thematic Fit ≤ 3"),
        ("Low Priority", "", "Priority < 2.5 OR Thematic Fit ≤ 2"),
    ]

    heading_fill = PatternFill("solid", fgColor="1E3A5F")
    heading_font = Font(bold=True, color="FFFFFF", size=10)
    sub_fill = PatternFill("solid", fgColor="E8EEF4")
    sub_font = Font(bold=True, size=10)

    for row_data in rubrics:
        ws.append(list(row_data))
        rn = ws.max_row
        label = str(row_data[0])
        if label in ("THEMATIC FIT", "STRATEGIC VALUE", "EASE OF APPLICATION",
                     "PRIORITY SCORE FORMULA", "PRIORITY TIERS"):
            for cell in ws[rn]:
                cell.fill = heading_fill
                cell.font = heading_font
        elif label == "Score":
            for cell in ws[rn]:
                cell.fill = sub_fill
                cell.font = sub_font
        ws[f"C{rn}"].alignment = Alignment(wrap_text=True)
        ws.row_dimensions[rn].height = 18


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_xlsx(result: dict) -> bytes:
    """Return an XLSX file as bytes from a completed analysis result dict."""
    wb = Workbook()

    opportunities = result.get("opportunities", [])
    # Sort by priority score descending
    opportunities = sorted(opportunities, key=lambda o: o.get("priority_score", 0), reverse=True)

    watchlist = result.get("strategic_watchlist", [])

    _tab_opportunities(wb, opportunities)
    _tab_priority(wb, opportunities)
    if watchlist:
        _tab_watchlist(wb, watchlist)
    _tab_recommendations(wb, result)
    _tab_rubrics(wb)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
