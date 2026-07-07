# Grant Opportunity Analyser

A web app that runs the repeatable grant opportunity analysis workflow for climate and deeptech startups.

Paste a startup website URL → get a prioritised, scored grant opportunity report + downloadable XLSX.

**Project history and current status:** see [PROJECT_LOG.md](PROJECT_LOG.md).
It must be updated with a dated entry every time a change is committed.

## Setup

**Requirements:** Python 3.11+, an Anthropic API key.

```bash
# 1. Set your API key
set ANTHROPIC_API_KEY=sk-ant-...

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the server
uvicorn main:app --reload

# Or on Windows, just double-click start.bat
```

Open **http://localhost:8000** in your browser.

## Launching in Google Chrome

1. Start the app locally:
   ```bash
   pip install -r requirements.txt
   ./start.sh
   ```
   This starts the backend and opens the app directly in Chrome.

2. Install the app in Chrome:
   - Open `http://localhost:8000`
   - Click the install prompt in the address bar, or use Chrome menu > Install app
   - After installation, launch it anytime from Chrome or your desktop/app launcher

3. Optional desktop shortcut:
   ```bash
   ./install-desktop.sh
   ```
   This installs a Linux desktop launcher that starts the app and opens it in Chrome.

## How it works

| Phase | What happens |
|-------|-------------|
| 1 — Company analysis | Fetches the startup website, sends content to Claude to extract a structured company profile (classification, geographies, TRL, themes, etc.) |
| 2 — Grant discovery | Claude executes 8–12 targeted DuckDuckGo web searches across national agencies, EU programmes, multilateral funds, prizes, and accelerators |
| 2 — Scoring | Each opportunity is scored on Thematic Fit (×0.45), Strategic Value (×0.35), and Ease (×0.20) using the rubric from the workflow |
| Output | Results displayed in browser + downloadable XLSX with 4 tabs |

## XLSX tabs

- **Opportunity Database** — all opportunities, colour-coded by priority tier
- **Priority Matrix** — grouped by tier (Must Pursue → Big Bet → Quick Win → Strategic Positioning → Low Priority)
- **Strategic Recommendations** — company profile + executive summary + strategic guidance
- **Rubrics Reference** — scoring definitions for manual review

## Priority tiers

| Tier | Criteria |
|------|----------|
| Must Pursue | Priority score ≥ 3.5 AND Thematic Fit ≥ 4 |
| Big Bet | Thematic Fit ≥ 4 but Ease ≤ 2, or Strategic Value = 5 |
| Quick Win | Ease ≥ 4 AND Priority ≥ 3.0 AND Thematic Fit ≥ 3 |
| Strategic Positioning | Strategic Value ≥ 4 but Thematic Fit ≤ 3 |
| Low Priority | Priority < 2.5 OR Thematic Fit ≤ 2 |

## Files

```
grant-analyzer/
├── main.py          FastAPI backend + SSE streaming
├── analyzer.py      Two-phase Claude workflow with web search tool loop
├── scraper.py       Website fetching and HTML cleaning
├── exporter.py      XLSX generation with openpyxl
├── requirements.txt
├── start.bat        Windows one-click launcher
├── start.sh         Linux Chrome launcher script
├── install-desktop.sh  Linux desktop launcher installer
├── tests/           Automated app tests
└── frontend/
    ├── index.html   Single-page UI
    ├── manifest.json Progressive Web App manifest
    ├── sw.js        Service worker for Chrome install
    └── icons/
        └── icon.svg
```
