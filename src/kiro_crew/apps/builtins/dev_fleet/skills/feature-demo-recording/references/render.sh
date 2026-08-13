#!/usr/bin/env bash
# render.sh — one-shot post-production: webm + events.json -> polished auto-zoom mp4.
# Chains camera.py (keyframes) -> postprocess.py (spring zoom/pan + dead-air trim).
#
# Usage:  bash render.sh <workdir> [out.mp4] [--speed 1.0] [--max-zoom 1.6] [--dead-air-speed 6]
# Reads <workdir>/MAIN_WEBM and <workdir>/events.json (both written by the harness).
set -euo pipefail

WORK="${1:?usage: render.sh <workdir> [out.mp4] [extra postprocess flags]}"
WORK="$(cd "$WORK" && pwd)"
OUT="${2:-$WORK/demo.mp4}"; shift || true; shift 2>/dev/null || true
EXTRA=("$@")

VENV="${DEMO_VENV:-$HOME/.kiro/crew/workspace/.demo-recording-venv}"
PY="$(cat "$VENV/PY_PATH")"
REF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WEBM="$(cat "$WORK/MAIN_WEBM")"
echo "render: webm=$WEBM"
"$PY" "$REF/camera.py" --events "$WORK/events.json" --out "$WORK/camera.json" --workdir "$WORK"
"$PY" "$REF/postprocess.py" --webm "$WEBM" --camera "$WORK/camera.json" --out "$OUT" --workdir "$WORK" "${EXTRA[@]}"
echo "DONE -> $OUT"
