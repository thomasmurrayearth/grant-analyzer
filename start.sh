#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

if ! command -v python >/dev/null 2>&1; then
  echo "Python not found. Install Python 3.11+ and try again." >&2
  exit 1
fi

if ! command -v uvicorn >/dev/null 2>&1; then
  echo "uvicorn is not installed. Run 'pip install -r requirements.txt' first." >&2
  exit 1
fi

HOST=127.0.0.1
PORT=8000
URL="http://$HOST:$PORT"

python -m uvicorn main:app --host "$HOST" --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" >/dev/null 2>&1' EXIT

sleep 2

if command -v google-chrome >/dev/null 2>&1; then
  google-chrome --app="$URL" >/dev/null 2>&1 &
elif command -v google-chrome-stable >/dev/null 2>&1; then
  google-chrome-stable --app="$URL" >/dev/null 2>&1 &
elif command -v chromium-browser >/dev/null 2>&1; then
  chromium-browser --app="$URL" >/dev/null 2>&1 &
elif command -v chromium >/dev/null 2>&1; then
  chromium --app="$URL" >/dev/null 2>&1 &
else
  echo "Could not find Chrome or Chromium. Open $URL manually in your browser." >&2
fi

wait "$SERVER_PID"
