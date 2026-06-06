@echo off
cd /d "%~dp0"

if not defined ANTHROPIC_API_KEY (
    echo ERROR: ANTHROPIC_API_KEY environment variable is not set.
    echo Set it with:  set ANTHROPIC_API_KEY=sk-ant-...
    pause
    exit /b 1
)

echo Installing dependencies...
pip install -r requirements.txt --quiet

echo.
echo Starting Grant Opportunity Analyser at http://localhost:8000
echo Press Ctrl+C to stop.
echo.

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
