#!/usr/bin/env bash
# setup.sh — one-time setup for feature-demo-recording.
# Creates a Python venv with Playwright, reuses the browsers from ~/.cache/ms-playwright,
# and locates the ffmpeg that Playwright bundles. Idempotent: safe to re-run.
#
# Writes two sidecar files into the venv dir:
#   PY_PATH      — absolute path to the venv python (use this to run record.py)
#   FFMPEG_PATH  — absolute path to a working ffmpeg (use this to transcode webm->mp4)
set -euo pipefail

VENV="${DEMO_VENV:-$HOME/.kirocrew/workspace/.demo-recording-venv}"
PY="$VENV/bin/python"
REF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== feature-demo-recording setup =="
echo "venv: $VENV"

# 1) venv (prefer uv if present — much faster; fall back to stdlib venv)
if [ ! -x "$PY" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi

# 2) Python packages: playwright (record) + pillow/numpy/imageio (auto-zoom post-processing)
PKGS="playwright pillow numpy imageio imageio-ffmpeg"
missing=""
for mod in playwright PIL numpy imageio imageio_ffmpeg; do
  "$PY" -c "import $mod" 2>/dev/null || missing="yes"
done
if [ -n "$missing" ]; then
  echo "installing python deps ($PKGS)..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" $PKGS >/dev/null
  else
    "$PY" -m pip install -q --upgrade pip >/dev/null
    "$PY" -m pip install -q $PKGS >/dev/null
  fi
fi
echo "playwright: $("$PY" -m playwright --version 2>/dev/null || echo installed)"
echo "pillow/numpy/imageio: $("$PY" -c 'import PIL,numpy,imageio;print(PIL.__version__, numpy.__version__, imageio.__version__)' 2>/dev/null || echo MISSING)"

# 3) Chromium — `playwright install chromium` is idempotent and revision-aware;
#    it only downloads if the installed revision doesn't match the package version.
echo "ensuring chromium matches installed playwright version..."
"$PY" -m playwright install chromium

# 4) ffmpeg — we need an h264/mp4-capable build. The ffmpeg Playwright BUNDLES is a stripped
#    webm/vp8-only build (no libx264, no mp4 muxer) — it CANNOT transcode the webm to mp4.
#    imageio-ffmpeg ships a full static build with libx264; prefer it. Fall back to a system
#    ffmpeg that has libx264; only as a last resort use Playwright's (webm-only) binary.
if ! "$PY" -c "import imageio_ffmpeg" 2>/dev/null; then
  echo "installing imageio-ffmpeg (full h264 ffmpeg) into venv..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" imageio-ffmpeg >/dev/null
  else
    "$PY" -m pip install -q imageio-ffmpeg >/dev/null
  fi
fi
# has_x264: SIGPIPE-safe check (capture output fully; never pipe ffmpeg into `grep -q`, which
# closes the pipe early -> ffmpeg exits 141 -> flaky false negative under `set -o pipefail`).
has_x264() { [ -x "$1" ] && case "$("$1" -hide_banner -encoders 2>/dev/null)" in *libx264*) return 0;; *) return 1;; esac; }

FF="$("$PY" -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || true)"
if ! has_x264 "$FF"; then
  if command -v ffmpeg >/dev/null 2>&1 && has_x264 "$(command -v ffmpeg)"; then
    FF="$(command -v ffmpeg)"
  fi
fi
if [ -z "$FF" ]; then
  echo "WARN: no h264-capable ffmpeg found. Try: \"$PY\" -m pip install imageio-ffmpeg"
else
  has_x264 "$FF" && X="yes" || X="NO"
  echo "ffmpeg: $FF (libx264: $X)"
fi

# 5) write sidecars
printf '%s' "$PY" > "$VENV/PY_PATH"
[ -n "$FF" ] && printf '%s' "$FF" > "$VENV/FFMPEG_PATH"

echo
echo "DONE."
echo "  python : $(cat "$VENV/PY_PATH")"
[ -f "$VENV/FFMPEG_PATH" ] && echo "  ffmpeg : $(cat "$VENV/FFMPEG_PATH")"
echo
echo "Next:"
echo "  1) copy record_template.py to <workdir>, fill in SCENES"
echo "  2) RECORD : KC_URL=\"\$(cat <workdir>/.tokenurl)\" \"\$(cat $VENV/PY_PATH)\" record.py"
echo "  3) POLISH : bash $REF_DIR/render.sh <workdir> <workdir>/demo.mp4 --dead-air-speed 6"
echo "  4) SHIP   : file_send <workdir>/demo.mp4 to Slack"
