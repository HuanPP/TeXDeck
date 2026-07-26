#!/usr/bin/env bash
# Start the TeXDeck API from server/ (default port 8000, hot reload)
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
UVICORN="${ROOT}/.venv/bin/uvicorn"
if [[ ! -x "$UVICORN" ]]; then
  UVICORN="uvicorn"
fi
# Localhost by default: the API has no auth. Set TEXDECK_BIND_HOST=0.0.0.0 to expose deliberately.
exec "$UVICORN" app.main:app --reload --host "${TEXDECK_BIND_HOST:-127.0.0.1}" --port "${TEXDECK_PORT:-8000}"
