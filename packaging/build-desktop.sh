#!/usr/bin/env bash
# Build the standalone KiroCrew desktop app end-to-end.
#
# Pipeline (uses the python-build-standalone approach):
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
# ARCHITECTURE: on macOS this builds ONE universal .app/DMG by default: the
# Electron shell is lipo-merged (arm64 + x86_64) by electron-builder, and the
# backend — which cannot be lipo-merged (a whole PBS tree, not one binary) —
# ships as TWO complete trees (backend-dist/kirocrew-backend-arm64/ and
# .../kirocrew-backend-x64/), selected at launch by find-bin.js via
# process.arch. The x86_64 backend is built under Rosetta 2, so the universal
# build needs an Apple-Silicon host. Linux always builds host-arch only
# (AppImage). UNIVERSAL=0 forces a host-arch-only macOS build (faster local
# iteration, or the only option on an Intel Mac).
#
# Usage:
#   bash packaging/build-desktop.sh            # macOS: universal DMG · Linux: host arch
#   UNIVERSAL=0 bash packaging/...             # macOS: host-arch-only DMG
#   SKIP_FRONTEND=1 bash packaging/...         # reuse an already-staged dist
#   SKIP_ELECTRON=1 bash packaging/...         # stop after the backend binary
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
HOST_ARCH="$(uname -m)"

# Universal is the macOS default; Linux has no universal concept (AppImage is
# per-arch). UNIVERSAL=0 opts a macOS build out.
if [ "$OS" = "darwin" ]; then
  UNIVERSAL="${UNIVERSAL:-1}"
else
  UNIVERSAL="${UNIVERSAL:-0}"
fi

if [ "$UNIVERSAL" = "1" ]; then
  if [ "$OS" != "darwin" ]; then
    echo "ERROR: UNIVERSAL=1 is a macOS-only mode (universal .app = lipo-merged" >&2
    echo "       Mach-O shell + dual macOS backends). Build Linux per-arch instead." >&2
    exit 1
  fi
  if [ "$HOST_ARCH" != "arm64" ]; then
    echo "ERROR: the universal build requires an Apple-Silicon host — the arm64" >&2
    echo "       backend cannot be built on Intel (no x86_64->arm64 Rosetta)." >&2
    echo "       On this machine run a host-arch-only build instead:" >&2
    echo "       UNIVERSAL=0 make desktop" >&2
    exit 1
  fi
  if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "ERROR: Rosetta 2 is required to build the x86_64 backend. Install it with:" >&2
    echo "       softwareupdate --install-rosetta --agree-to-license" >&2
    echo "       (or build host-arch only: UNIVERSAL=0 make desktop)" >&2
    exit 1
  fi
  printf '\n\033[1;33m▶ Building UNIVERSAL macOS app: arm64 + x86_64.\033[0m\n'
else
  printf '\n\033[1;33m▶ Building for host arch only: %s/%s.\033[0m\n' \
    "$(uname -s)" "$HOST_ARCH"
fi

ELECTRON_DIR="$ROOT/website/electron"

# Version from the package.
KC_VERSION="$(grep -m1 '__version__' "$ROOT/src/kiro_crew/__init__.py" \
  | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')"
if [ -z "$KC_VERSION" ]; then
  echo "ERROR: could not parse __version__ from src/kiro_crew/__init__.py" >&2
  exit 1
fi

# Channel identity from the version stamp. Nightly ships as a SEPARATE
# side-by-side app (its own bundle id, name, icon) so it can be installed
# next to the production app. Insider/stable share the production identity —
# they are ONE app on two update lanes (the in-app channel switcher moves
# between them), so they keep the package.json defaults. Derivation mirrors
# auto-update.js channelForVersion: only a "-nightly." stamp changes
# identity; unstamped dev builds and insider/stable stamps build "KiroCrew".
case "$KC_VERSION" in
  *-nightly.*) PRODUCT_NAME="KiroCrew Nightly" ;;
  *)           PRODUCT_NAME="KiroCrew" ;;
esac

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

# --- 2. uv (provisions the PBS interpreters) ---------------------------------
log "Ensuring uv is available…"
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

# Resolve a managed PBS interpreter dir: $1 = uv install key, $2 = dir pattern.
# Prints the interpreter dir on stdout; fails loudly if absent.
provision_pbs() {
  local uv_key="$1" pattern="$2" dir
  uv python install "$uv_key" >/dev/null 2>&1 || true
  dir="$(find "$(uv python dir)" -maxdepth 1 -type d -name "$pattern" 2>/dev/null | sort -V | tail -1)"
  if [ -z "$dir" ] || [ ! -x "$dir/bin/python3.12" ]; then
    echo "ERROR: no managed python-build-standalone 3.12 matching ${pattern} under $(uv python dir)" >&2
    echo "       Run: uv python install $uv_key" >&2
    return 1
  fi
  printf '%s\n' "$dir"
}

# Build ONE self-contained backend tree.
#   $1 = PBS interpreter dir   $2 = output dir   $3 = required Mach-O arch tag
#        ("" skips the arch gate — used by the non-universal Linux path)
# Copies the interpreter, pip-installs kiro_crew (full closure), stages the
# dashboard, writes the relocatable launcher, gates self-containment, prunes.
build_backend() {
  local pbs_dir="$1" out="$2" want_arch="$3" sp

  log "Installing kiro_crew into the bundled interpreter ($(basename "$out"))…"
  mkdir -p "$(dirname "$out")"
  cp -R "$pbs_dir" "$out"

  # PBS ships uv's PEP 668 EXTERNALLY-MANAGED marker; drop it so pip can install
  # into our private copy (this is our bundle, not a system interpreter).
  find "$out" -name "EXTERNALLY-MANAGED" -delete 2>/dev/null || true

  # PYTHONNOUSERSITE=1 + empty PYTHONPATH: force the full closure into the bundle.
  # Without this, pip treats deps already present on the build host as "satisfied"
  # and skips them -> the gateway crashes on a clean machine with ModuleNotFoundError.
  # An x86_64 python3.12 binary runs under Rosetta transparently, so the same
  # invocation builds both arches' bundles.
  # --prefer-binary: take an older prebuilt wheel over a newer sdist. Some deps
  # have dropped macOS x86_64 wheels in their newest releases (e.g. cryptography
  # >= 49 is arm64-only), and a source build inside the bundle needs toolchains
  # (Rust targets) the build host may lack — an older universal2/x86_64 wheel is
  # the portable choice. No-op where the newest release has a usable wheel.
  env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
    "$out/bin/python3.12" -m pip install --prefer-binary \
    --no-warn-script-location --disable-pip-version-check "$ROOT"

  # Stage the dashboard dist into the package's static dir.
  sp="$out/lib/python3.12/site-packages"
  log "Staging dashboard dist into kiro_crew/static/dist…"
  mkdir -p "$sp/kiro_crew/static"
  ( cd "$sp/kiro_crew/static" && rm -rf dist && cp -R "$ROOT/website/dist" dist )
  [ -f "$sp/kiro_crew/static/dist/index.html" ] || {
    echo "ERROR: dashboard dist not staged" >&2; exit 1
  }

  # Relocatable launcher script.
  cat > "$out/bin/kirocrew" <<'LAUNCH'
#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/python3.12" -s -m kiro_crew "$@"
LAUNCH
  chmod +x "$out/bin/kirocrew"

  # Arch gate: the bundled interpreter must be the arch this tree claims to be
  # (a mismatch ships an app whose backend crashes at launch on the other arch).
  if [ -n "$want_arch" ]; then
    case "$(file -b "$out/bin/python3.12")" in
      *"$want_arch"*) ;;
      *)
        echo "ERROR: $(basename "$out")/bin/python3.12 is not ${want_arch}:" >&2
        file "$out/bin/python3.12" >&2
        exit 1
        ;;
    esac
  fi

  # Self-containment gate: the full import chain must resolve with no user-site.
  log "Verifying self-containment ($(basename "$out"))…"
  PYTHONNOUSERSITE=1 "$out/bin/python3.12" -m kiro_crew --version >/dev/null \
    || { echo "ERROR: bundled backend is NOT self-contained (missing dep under PYTHONNOUSERSITE=1)" >&2; exit 1; }

  # Prune to shrink the bundle.
  log "Pruning bundle ($(basename "$out"))…"
  ( cd "$out"
    find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    find lib/python3.12/site-packages -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf lib/python3.12/test lib/python3.12/idlelib lib/python3.12/tkinter \
           lib/python3.12/turtledemo lib/python3.12/ensurepip lib/python3.12/lib2to3 2>/dev/null || true )

  echo "    $(basename "$out") size: $(du -sh "$out" 2>/dev/null | cut -f1)"
}

# Resolver-agreement gate: the Electron launcher (find-bin.js) must locate the
# launcher we just wrote. This catches contract drift between this builder's
# output layout and find-bin.js's candidate list — a silent mismatch there
# ships an app that can't spawn its backend (falls through to the bare
# "kirocrew" PATH fallback -> spawn ENOENT).
#   $1 = expected launcher path   $2 = arch argument ("" = default process.arch)
resolver_gate() {
  local expected="$1" arch_arg="$2"
  if command -v node >/dev/null 2>&1; then
    log "Verifying find-bin.js resolves ${expected#"$ELECTRON_DIR/"}…"
    node -e '
      const fs=require("fs"), os=require("os"), path=require("path");
      const { findKirocrewBin } = require(path.join(process.argv[1], "find-bin"));
      // Simulate the packaged app: resourcesPath and __dirname both point at the
      // electron dir where backend-dist currently lives.
      const arch = process.argv[3] || undefined;
      const resolved = arch
        ? findKirocrewBin(fs, os, path, process.argv[1], process.argv[1], arch)
        : findKirocrewBin(fs, os, path, process.argv[1], process.argv[1]);
      const expected = process.argv[2];
      if (resolved !== expected) {
        console.error("ERROR: find-bin.js resolved \x27" + resolved + "\x27, expected the bundled launcher \x27" + expected + "\x27.");
        console.error("       The builder output layout and find-bin.js candidate list have drifted apart.");
        process.exit(1);
      }
      console.log("    find-bin.js -> " + resolved);
    ' "$ELECTRON_DIR" "$expected" "$arch_arg" \
      || { echo "ERROR: find-bin.js cannot locate the bundled backend launcher" >&2; exit 1; }
  else
    echo "    (node not found; skipping find-bin.js resolver-agreement gate)"
  fi
}

# --- 3. Build the backend tree(s) --------------------------------------------
rm -rf "$ELECTRON_DIR/backend-dist"
mkdir -p "$ELECTRON_DIR/backend-dist"

if [ "$UNIVERSAL" = "1" ]; then
  log "Provisioning PBS interpreters (arm64 + x86_64) via uv…"
  PBS_ARM64="$(provision_pbs "cpython-3.12-macos-aarch64-none" "cpython-3.12*-macos-aarch64-none")"
  PBS_X64="$(provision_pbs "cpython-3.12-macos-x86_64-none" "cpython-3.12*-macos-x86_64-none")"
  echo "    arm64 PBS:  $PBS_ARM64"
  echo "    x86_64 PBS: $PBS_X64"

  build_backend "$PBS_ARM64" "$ELECTRON_DIR/backend-dist/kirocrew-backend-arm64" "arm64"
  build_backend "$PBS_X64" "$ELECTRON_DIR/backend-dist/kirocrew-backend-x64" "x86_64"

  resolver_gate "$ELECTRON_DIR/backend-dist/kirocrew-backend-arm64/bin/kirocrew" "arm64"
  resolver_gate "$ELECTRON_DIR/backend-dist/kirocrew-backend-x64/bin/kirocrew" "x64"
else
  log "Provisioning python-build-standalone interpreter (uv)…"
  # Pin to CPython 3.12 (latest stable, matches CI python-version).
  ARCH="$HOST_ARCH"
  [ "$ARCH" = "arm64" ] && ARCH="aarch64"
  if [ "$OS" = "darwin" ]; then
    PBS_PATTERN="cpython-3.12*-macos-${ARCH}-none"
  else
    PBS_PATTERN="cpython-3.12*-linux-${ARCH}-gnu"
  fi
  PBS_DIR="$(provision_pbs "cpython-3.12" "$PBS_PATTERN")"
  echo "    PBS interpreter: $PBS_DIR"

  build_backend "$PBS_DIR" "$ELECTRON_DIR/backend-dist/kirocrew-backend" ""
  resolver_gate "$ELECTRON_DIR/backend-dist/kirocrew-backend/bin/kirocrew" ""
fi

if [ "${SKIP_ELECTRON:-0}" = "1" ]; then
  log "SKIP_ELECTRON=1 — backend(s) ready under $ELECTRON_DIR/backend-dist/"
  exit 0
fi

# --- 4. Package the desktop app with electron-builder -----------------------
log "Packaging desktop app (electron-builder, version: $KC_VERSION)…"
( cd "$ELECTRON_DIR"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi

  EB_ARGS=( "-c.extraMetadata.version=$KC_VERSION" )
  if [ "$PRODUCT_NAME" = "KiroCrew Nightly" ]; then
    # Same appId (com.amazon.kiro.crew) as production ON PURPOSE:
    # - Finder decides install-replace by FILENAME only, so the distinct
    #   productName alone gives side-by-side installs.
    # - Squirrel.Mac validates updates against the host app's designated
    #   requirement (which pins the bundle id); a distinct nightly id would
    #   strand every existing install at the identity switch.
    # - CDSigner authz is per-identifier; the shared id is already onboarded.
    # Cost accepted: shared TCC/notification identity, and a kirocrew:// URL
    # scheme could not disambiguate the two apps (none is registered today).
    EB_ARGS+=(
      "-c.productName=KiroCrew Nightly"
      "-c.mac.icon=icon-nightly.png"
      "-c.linux.icon=icon-nightly.png"
    )
  fi
  if [ "$OS" = "darwin" ]; then
    EB_ARGS+=( --mac )
    [ "$UNIVERSAL" = "1" ] && EB_ARGS+=( --universal )
  else
    EB_ARGS+=( --linux )
  fi

  # Start from a pristine output dir. A prior interrupted universal build can
  # leave dist/mac-universal-<arch>-temp dirs behind (with a .DS_Store inside);
  # those linger and re-trip the ENOTEMPTY cleanup below on every later run.
  rm -rf dist

  # macOS universal .DS_Store/ENOTEMPTY race:
  # electron-builder's universal step stages each arch into
  # dist/mac-universal-<arch>-temp, lipo-merges them, then removes the temp
  # dirs with a recursive fs.rm. If macOS Desktop Services (Finder/Spotlight)
  # drops a .DS_Store into a temp dir *during* that removal, Node's fs.rm --
  # which performs no retries -- fails the final rmdir with ENOTEMPTY and the
  # whole build aborts (electron-userland/electron-builder#6890). It is a
  # transient race, so retry from a swept, clean dir a bounded number of times.
  attempt=1; max_attempts=3
  while : ; do
    eb_log="$(mktemp "${TMPDIR:-/tmp}/kc-eb.XXXXXX")"
    if CSC_IDENTITY_AUTO_DISCOVERY=false ./node_modules/.bin/electron-builder "${EB_ARGS[@]}" 2>&1 | tee "$eb_log"; then
      rm -f "$eb_log"; break
    fi
    if grep -q "ENOTEMPTY" "$eb_log" && [ "$attempt" -lt "$max_attempts" ]; then
      echo "  ⚠ macOS .DS_Store/ENOTEMPTY temp-dir race (attempt $attempt/$max_attempts); sweeping .DS_Store and retrying…" >&2
      find dist -name .DS_Store -delete 2>/dev/null || true
      rm -rf dist/*-temp 2>/dev/null || true
      rm -f "$eb_log"; attempt=$((attempt + 1)); sleep 2; continue
    fi
    rm -f "$eb_log"
    echo "❌ electron-builder failed (not the .DS_Store race, or retries exhausted)." >&2
    exit 1
  done
)

# Universal post-gate: the staged shell binary must carry BOTH arch slices.
if [ "$UNIVERSAL" = "1" ]; then
  log "Verifying the shell binary is universal (lipo)…"
  APP_BIN="$(find "$ELECTRON_DIR/dist" -maxdepth 5 \
    -path "*/${PRODUCT_NAME}.app/Contents/MacOS/${PRODUCT_NAME}" -print -quit 2>/dev/null)"
  if [ -z "$APP_BIN" ]; then
    echo "ERROR: staged ${PRODUCT_NAME}.app not found under $ELECTRON_DIR/dist" >&2
    exit 1
  fi
  LIPO_ARCHS="$(lipo -archs "$APP_BIN")"
  case "$LIPO_ARCHS" in
    *x86_64*arm64*|*arm64*x86_64*)
      echo "    $APP_BIN: $LIPO_ARCHS" ;;
    *)
      echo "ERROR: shell binary is not universal (lipo -archs: $LIPO_ARCHS)" >&2
      exit 1 ;;
  esac
fi

log "Done. Installer(s) are in $ELECTRON_DIR/dist/"
ls -1 "$ELECTRON_DIR/dist/"*.{dmg,AppImage,zip} 2>/dev/null | sed 's/^/   /' || true
echo ""
echo "    The .app embeds the backend, so it runs with no PATH kirocrew needed."
