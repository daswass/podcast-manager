#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d web/dist ]]; then
  npm --prefix web install
  npm --prefix web run build
fi

if [[ ! -x venv/bin/python ]]; then
  echo "Missing venv. Run: python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

./venv/bin/python -m uvicorn manager_server:app --host 127.0.0.1 --port 8765 &
server_pid=$!

cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

ready=0
for _ in $(seq 1 50); do
  if curl -sf -o /dev/null "http://127.0.0.1:8765/api/library"; then
    ready=1
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "Manager server exited before it was ready" >&2
    exit 1
  fi
  sleep 0.2
done

if [[ "$ready" -ne 1 ]]; then
  echo "Manager server did not answer on http://127.0.0.1:8765" >&2
  exit 1
fi

open "http://127.0.0.1:8765"
wait "$server_pid"
