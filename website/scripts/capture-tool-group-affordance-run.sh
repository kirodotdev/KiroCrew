#!/bin/bash
# One-shot: bring up the isolated capture dev server, photograph both
# collapsed-tool-group affordances (#9699) collapsed and expanded in dark and
# light, assert they share one class list, then tear the server down.
#
#   bash scripts/capture-tool-group-affordance-run.sh
#
# Frames land in ../temp-screenshots/tool-group-affordance/ (gitignored, which is
# what the PR body attaches).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

export PATH="$HOME/.local/share/mise/installs/node/24.18.0/bin:$PATH"

PORT=6851
LOG=/tmp/vite-tool-group-affordance.log

npx vite --host 127.0.0.1 --port "$PORT" --strictPort > "$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

for _ in $(seq 1 40); do
  if grep -q "ready in" "$LOG" 2>/dev/null; then break; fi
  sleep 1
done
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "dev server died; log follows" >&2
  tail -20 "$LOG" >&2
  exit 1
fi

node scripts/capture-tool-group-affordance.mjs "http://127.0.0.1:$PORT" ../temp-screenshots/tool-group-affordance
STATUS=$?

exit "$STATUS"
