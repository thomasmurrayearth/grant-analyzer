"""The XLSX is the artefact users forward to co-founders and boards, so it has
to carry the analysis *and* the offer of help with it."""

import io
import unittest

from openpyxl import load_workbook

from exporter import generate_xlsx

RESULT = {
    "company_profile": {"name": "Example Thermal"},
    "executive_summary": {
        "company_overview": "A climate hardware company.",
        "key_constraints": "Pre-revenue, small team.",
    },
    "strategic_recommendations": {
        "best_fit_strategy": "Lead with national innovation grants.",
        "key_risks": "Consortium-only calls need a partner.",
    },
    "opportunities": [{
        "name": "Example Innovation Fund",
        "managing_body": "Example Agency",
        "geography": "EU",
        "thematic_fit_score": 5,
        "strategic_value_score": 4,
        "ease_score": 3,
        "priority_score": 4.5,
        "priority_tier": "Must Pursue",
        "application_link": "https://example.org/apply",
        "deadline": "2026-09-30",
    }],
    "acronym_definitions": [{"acronym": "TRL", "definition": "Technology Readiness Level"}],
}


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.wb = load_workbook(io.BytesIO(generate_xlsx(RESULT)))

    def test_workbook_has_the_three_tabs(self):
        self.assertEqual(
            self.wb.sheetnames,
            ["Grant Opportunities", "Strategic Recommendations", "Definitions acronyms"],
        )

    def test_recommendations_tab_carries_the_analysis(self):
        text = self._sheet_text("Strategic Recommendations")
        self.assertIn("Lead with national innovation grants.", text)
        self.assertIn("A climate hardware company.", text)

    def test_recommendations_tab_carries_the_consulting_offer(self):
        text = self._sheet_text("Strategic Recommendations")
        self.assertIn("Thomas Murray", text)
        self.assertIn("thomasmurray.earth", text)

    def test_opportunities_tab_still_scores_grants(self):
        ws = self.wb["Grant Opportunities"]
        self.assertEqual(ws["A2"].value, "Example Innovation Fund")
        self.assertEqual(ws["B2"].value, "=(2*D2)+F2+H2")

    def _sheet_text(self, name: str) -> str:
        return "\n".join(
            str(cell.value)
            for row in self.wb[name].iter_rows()
            for cell in row
            if cell.value is not None
        )


if __name__ == "__main__":
    unittest.main()
