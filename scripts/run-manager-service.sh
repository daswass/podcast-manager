#!/bin/bash
# Serves the manager page for launchd. Does not open a browser.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_DIR}"
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

exec "${PROJECT_DIR}/venv/bin/python" -m uvicorn manager_server:app --host 127.0.0.1 --port 8765
