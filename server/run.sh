#!/usr/bin/env bash
# Start the TeXDeck API from server/ (default port 8000, hot reload)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
UVICORN="${ROOT}/.venv/bin/uvicorn"
if [[ ! -x "$UVICORN" ]]; then
  UVICORN="uvicorn"
fi
exec "$UVICORN" app.main:app --reload --host 0.0.0.0 --port 8000
