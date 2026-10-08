#!/usr/bin/env bash
# Boot the court UI. Idempotent: leave an existing listener alone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export SPORTFLEX_CHROME_NO_SANDBOX=1

if (echo >/dev/tcp/127.0.0.1/8765) >/dev/null 2>&1; then
  echo "sport-flex already listening on port 8765"
  exit 0
fi

exec "$ROOT/.venv/bin/python" -m sportflex ui --host 0.0.0.0 --port 8765 --no-browser
