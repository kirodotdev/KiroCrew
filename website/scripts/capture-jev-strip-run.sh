#!/bin/bash
# One-shot: bring up the isolated capture dev server, photograph the routing
# receipt (collapsed + expanded), assert both, then tear the server down.
#
#   bash scripts/capture-jev-strip-run.sh
#
# Frames land in ../temp-screenshots/decision-strip-model-caption/ (gitignored,
# which is what the PR body attaches).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

export PATH="$HOME/.local/share/mise/installs/node/24.18.0/bin:$PATH"

PORT=6845
LOG=/tmp/vite-jev-strip.log

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

node scripts/capture-decision-strip-model-caption.mjs \
  "http://127.0.0.1:$PORT" ../temp-screenshots/decision-strip-model-caption
STATUS=$?

exit "$STATUS"
