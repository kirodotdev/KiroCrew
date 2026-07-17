#!/usr/bin/env bash
# Build the standalone KiroCrew desktop app end-to-end.
#
# Pipeline (mirrors MeshClaw's python-build-standalone approach):
#   1. Build the React dashboard (npm)         -> website/dist
#   2. Provision a python-build-standalone (PBS) interpreter via uv
#   3. pip-install kiro_crew + deps INTO the bundled interpreter
#   4. Stage the dashboard into the package's static dir
#   5. Prune caches/tests/unused stdlib to shrink the bundle
#   6. Package the desktop app with electron-builder -> DMG (mac) / AppImage (linux)
#
# The result is a double-clickable app that embeds the whole Python backend +
# dashboard — no system Python, pip, npm, or node required by the end user.
#
# This REPLACES the old PyInstaller approach. PBS interpreters are self-contained
# and use @executable_path-relative dylib references, so the bundle is genuinely
# portable across machines without needing the exact same system Python version.
#
# ARCHITECTURE: builds for the HOST OS and HOST CPU ARCH only (not a universal
# binary). To ship macOS arm64 + x86_64 and Linux x86_64 + aarch64, run once
# per architecture.
#
# Usage:
#   bash packaging/build-desktop.sh            # build for the host OS + arch
#   SKIP_FRONTEND=1 bash packaging/...         # reuse an already-staged dist
#   SKIP_ELECTRON=1 bash packaging/...         # stop after the backend binary
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

printf '\n\033[1;33m▶ Building for host arch only: %s/%s.\033[0m\n' \
  "$(uname -s)" "$(uname -m)"

ELECTRON_DIR="$ROOT/website/electron"

# Version from the package.
KC_VERSION="$(grep -m1 '__version__' "$ROOT/src/kiro_crew/__init__.py" \
  | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')"
if [ -z "$KC_VERSION" ]; then
  echo "ERROR: could not parse __version__ from src/kiro_crew/__init__.py" >&2
  exit 1
fi

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

# --- 1. Frontend ------------------------------------------------------------
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  log "Building dashboard (npm)…"
  ( cd "$ROOT/website"
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
    npm run build )
else
  log "SKIP_FRONTEND=1 — reusing existing website/dist"
fi

if [ ! -f "$ROOT/website/dist/index.html" ]; then
  echo "❌ Dashboard dist missing at website/dist — cannot bundle." >&2
  exit 1
fi

# --- 2. Provision python-build-standalone interpreter via uv ----------------
log "Provisioning python-build-standalone interpreter (uv)…"
command -v uv >/dev/null 2>&1 || {
  echo "uv not found — installing pinned version from https://docs.astral.sh/uv/" >&2
  # Pin to a known-good version to avoid silent supply-chain changes.
  # Bump this explicitly when upgrading uv.
  UV_VERSION="0.10.11"
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || {
    echo "ERROR: uv not found on PATH after install. Check ~/.local/bin or install manually." >&2
    exit 1
  }
}

# Pin to CPython 3.12 (latest stable, matches CI python-version).
UV_PY_VERSION="cpython-3.12"
uv python install "$UV_PY_VERSION" >/dev/null 2>&1 || true

# Resolve the architecture suffix for the PBS directory name.
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
[ "$ARCH" = "arm64" ] && ARCH="aarch64"
[ "$ARCH" = "x86_64" ] && ARCH="x86_64"

if [ "$OS" = "darwin" ]; then
  PBS_PATTERN="cpython-3.12*-macos-${ARCH}-none"
else
  PBS_PATTERN="cpython-3.12*-linux-${ARCH}-gnu"
fi

PBS_DIR="$(find "$(uv python dir)" -maxdepth 1 -type d -name "$PBS_PATTERN" 2>/dev/null | sort -V | tail -1)"
if [ -z "$PBS_DIR" ] || [ ! -x "$PBS_DIR/bin/python3.12" ]; then
  echo "ERROR: no managed python-build-standalone 3.12 for ${OS}-${ARCH} under $(uv python dir)" >&2
  echo "       Run: uv python install $UV_PY_VERSION" >&2
  exit 1
fi
echo "    PBS interpreter: $PBS_DIR"

# --- 3. Install kiro_crew into the bundled interpreter ----------------------
log "Installing kiro_crew into the bundled interpreter…"
BACKEND_OUT="$ELECTRON_DIR/backend-dist/kirocrew-backend"
rm -rf "$ELECTRON_DIR/backend-dist"
mkdir -p "$ELECTRON_DIR/backend-dist"
cp -R "$PBS_DIR" "$BACKEND_OUT"

# PBS ships uv's PEP 668 EXTERNALLY-MANAGED marker; drop it so pip can install
# into our private copy (this is our bundle, not a system interpreter).
find "$BACKEND_OUT" -name "EXTERNALLY-MANAGED" -delete 2>/dev/null || true

# PYTHONNOUSERSITE=1 + empty PYTHONPATH: force the full closure into the bundle.
# Without this, pip treats deps already present on the build host as "satisfied"
# and skips them -> the gateway crashes on a clean machine with ModuleNotFoundError.
env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
  "$BACKEND_OUT/bin/python3.12" -m pip install \
  --no-warn-script-location --disable-pip-version-check "$ROOT"

# Stage the dashboard dist into the package's static dir.
SP="$BACKEND_OUT/lib/python3.12/site-packages"
log "Staging dashboard dist into kiro_crew/static/dist…"
mkdir -p "$SP/kiro_crew/static"
( cd "$SP/kiro_crew/static" && rm -rf dist && cp -R "$ROOT/website/dist" dist )
[ -f "$SP/kiro_crew/static/dist/index.html" ] || {
  echo "ERROR: dashboard dist not staged" >&2; exit 1
}

# Relocatable launcher script.
cat > "$BACKEND_OUT/bin/kirocrew" <<'LAUNCH'
#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/python3.12" -s -m kiro_crew "$@"
LAUNCH
chmod +x "$BACKEND_OUT/bin/kirocrew"

# Self-containment gate: the full import chain must resolve with no user-site.
log "Verifying self-containment…"
PYTHONNOUSERSITE=1 "$BACKEND_OUT/bin/python3.12" -m kiro_crew --version >/dev/null \
  || { echo "ERROR: bundled backend is NOT self-contained (missing dep under PYTHONNOUSERSITE=1)" >&2; exit 1; }

# Resolver-agreement gate: the Electron launcher (find-bin.js) must be able to
# locate the launcher we just wrote. This catches contract drift between this
# builder's output layout (BACKEND_OUT/bin/kirocrew) and find-bin.js's candidate
# list — a silent mismatch there ships an app that can't spawn its backend
# (falls through to the bare "kirocrew" PATH fallback -> spawn ENOENT).
if command -v node >/dev/null 2>&1; then
  log "Verifying find-bin.js resolves the bundled launcher…"
  node -e '
    const fs=require("fs"), os=require("os"), path=require("path");
    const { findKirocrewBin } = require(path.join(process.argv[1], "find-bin"));
    // Simulate the packaged app: resourcesPath and __dirname both point at the
    // electron dir where backend-dist currently lives.
    const resolved = findKirocrewBin(fs, os, path, process.argv[1], process.argv[1]);
    const expected = process.argv[2];
    if (resolved !== expected) {
      console.error("ERROR: find-bin.js resolved \x27" + resolved + "\x27, expected the bundled launcher \x27" + expected + "\x27.");
      console.error("       The builder output layout and find-bin.js candidate list have drifted apart.");
      process.exit(1);
    }
    console.log("    find-bin.js -> " + resolved);
  ' "$ELECTRON_DIR" "$BACKEND_OUT/bin/kirocrew" \
    || { echo "ERROR: find-bin.js cannot locate the bundled backend launcher" >&2; exit 1; }
else
  echo "    (node not found; skipping find-bin.js resolver-agreement gate)"
fi

# --- 4. Prune to shrink the bundle ------------------------------------------
log "Pruning bundle…"
( cd "$BACKEND_OUT"
  find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find lib/python3.12/site-packages -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
  rm -rf lib/python3.12/test lib/python3.12/idlelib lib/python3.12/tkinter \
         lib/python3.12/turtledemo lib/python3.12/ensurepip lib/python3.12/lib2to3 2>/dev/null || true )

echo "    backend-dist size: $(du -sh "$BACKEND_OUT" 2>/dev/null | cut -f1)"

if [ "${SKIP_ELECTRON:-0}" = "1" ]; then
  log "SKIP_ELECTRON=1 — backend ready at $BACKEND_OUT"
  exit 0
fi

# --- 5. Package the desktop app with electron-builder -----------------------
log "Packaging desktop app (electron-builder, version: $KC_VERSION)…"
( cd "$ELECTRON_DIR"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi

  EB_ARGS=( "-c.extraMetadata.version=$KC_VERSION" )
  if [ "$OS" = "darwin" ]; then
    EB_ARGS+=( --mac )
  else
    EB_ARGS+=( --linux )
  fi

  CSC_IDENTITY_AUTO_DISCOVERY=false ./node_modules/.bin/electron-builder "${EB_ARGS[@]}"
)

log "Done. Installer(s) are in $ELECTRON_DIR/dist/"
ls -1 "$ELECTRON_DIR/dist/"*.{dmg,AppImage,zip} 2>/dev/null | sed 's/^/   /' || true
echo ""
echo "    The .app embeds the backend, so it runs with no PATH kirocrew needed."
