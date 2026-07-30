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
| 2 — Grant discovery | Claude executes targeted DuckDuckGo web searches across national agencies, EU programmes, multilateral funds, prizes, and accelerators (count set by `MAX_DISCOVERY_SEARCHES` in `analyzer.py` — currently 30) |
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

## Measuring output quality

Usage data can tell you whether people used the app. It cannot tell you whether
what they received was any good — and per §9a of the launch plan, the two most
damaging failure modes (output that can't be trusted, output no better than a
chatbot) leave no trace in usage counts. So quality is measured separately.

| Piece | What it does |
|-------|--------------|
| `quality.py` | Defines every measure once — recall and exclusion accuracy against ground truth, structural precision, tier spread, link quality, shape, and convergence between repeat runs. Pure: no network, no database, no API key. |
| `eval/cases.py` | The ground truth. Shared by the eval harness, the app and the scorer so there is one definition of the right answer. |
| `eval/run_eval.py` | Runs the full pipeline against those cases. Needs a key and about ten minutes per company. |
| `eval/score_stored.py` | Scores runs already on disk. Offline, instant, and how the baseline is re-derived. |
| `/admin/quality.json` | Every completed analysis in a window, already scored, plus feedback, funnel, economics and an explicit list of what could not be seen. Token-gated. |
| `benchmark.py` | Runs the ground-truth companies **only when a fortnight passed with no completed user run.** Live usage is better evidence and costs nothing. |
| `reports/quality-history.json` | The trend, one entry per fortnightly review. |

```bash
python3 eval/score_stored.py                    # baseline from stored runs
python3 eval/score_stored.py --since 2026-07-01 # confined to one pipeline version
```

**Convergence is a diagnostic, never a target.** Output that churns between
runs is evidence that retrieval and scoring are near-random. The fix is better
retrieval — never caching or pinning results, which would re-serve a poor answer
and destroy the live-search differentiator the app is built on.

## Files

```
grant-analyzer/
├── main.py          FastAPI backend + SSE streaming
├── analyzer.py      Two-phase Claude workflow with web search tool loop
├── quality.py       Output-quality measures (pure; no network or key)
├── benchmark.py     Fallback self-benchmark — runs only when a fortnight was quiet
├── scraper.py       Website fetching and HTML cleaning
├── exporter.py      XLSX generation with openpyxl
├── requirements.txt
├── start.bat        Windows one-click launcher
├── start.sh         Linux Chrome launcher script
├── install-desktop.sh  Linux desktop launcher installer
├── eval/            Ground-truth cases, eval harness, offline scorer
├── reports/         Quality trend and fortnightly review reports
├── tests/           Automated app tests
└── frontend/
    ├── index.html   Single-page UI
    ├── manifest.json Progressive Web App manifest
    ├── sw.js        Service worker for Chrome install
    └── icons/
        └── icon.svg
```
