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
# PBS interpreters are self-contained
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
# Git Bash / MSYS on Windows reports MINGW64_NT-10.0-... / MSYS_NT-...;
# normalize to "windows" so the branches below read naturally.
case "$OS" in
  mingw*|msys*|cygwin*) OS="windows" ;;
esac
HOST_ARCH="$(uname -m)"

# Beacon provenance for the artifact this run produces, derived from the
# electron-builder target rather than the host: mac.target is dmg, linux.target
# is AppImage + deb + rpm (website/electron/package.json). Reading the host OS instead
# would be wrong on Linux, where the same machine also builds wheels.
# Windows ships an NSIS installer, which has no KNOWN_DISTRIBUTIONS value yet;
# "source" is the honest answer until "nsis" is added on both sides.
case "$OS" in
  darwin)  KC_DISTRIBUTION="dmg" ;;
  windows) KC_DISTRIBUTION="source" ;;
  *)       KC_DISTRIBUTION="appimage" ;;
esac

# Linux packages ONE backend tree into several artifact formats, and the beacon
# `dist` value is baked INTO that tree — so each format needs its own stamp or
# one artifact reports itself as the other, which is exactly the mislabel
# scripts/stamp-distribution.sh exists to prevent. Pair each target with its
# dist label here; the packaging step re-stamps between invocations. The
# expensive work (PBS interpreter + pip closure) still happens once.
LINUX_TARGET_DISTS=( "AppImage:appimage" "deb:deb" "rpm:rpm" )

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

is_macos_intel_backend() {
  local want_arch="$1"
  [ "$OS" = "darwin" ] && {
    [ "$want_arch" = "x86_64" ] ||
      { [ -z "$want_arch" ] && [ "$HOST_ARCH" = "x86_64" ]; }
  }
}

# NOTE ON THE APPLE-SILICON DECODER -- do NOT re-introduce a compressed payload.
#
# The arm64 imageio-ffmpeg executable ships as a PLAIN Mach-O under
# Contents/Resources, exactly like its x86_64 sibling, so the app signer signs it
# with Developer ID + hardened runtime + secure timestamp along with every other
# nested binary (packaging/signing/generate-manifest.py enumerates it).
#
# #6746 instead stored it as inert gzip data, to keep the bytes byte-identical to
# the pinned upstream wheel across signing. The Apple notary service DECOMPRESSES
# archive members and scans what is inside them, so that made notarization fail
# closed on the whole release (submission 3dbd3c7d, three `error` issues on
# .../binaries/ffmpeg-macos-aarch64-v7.1.gz/ffmpeg-macos-aarch64-v7.1: not signed
# with a valid Developer ID certificate / no secure timestamp / hardened runtime
# not enabled). The x86_64 copy of the same wheel, shipped raw in the same
# submission, drew no issue at all -- that is the working shape.
#
# The runtime consequence is handled in kiro_crew.transcribe: the packaged decoder
# is accepted either at the pinned upstream digest (local builds and the build gate
# below, which run BEFORE signing) or on a valid Developer ID signature from our
# team (the released app, whose bytes signing necessarily rewrote).

# Prove that the installed native wheel, audio decoder, and their transitive
# libraries can actually run from the pruned bundle. A successful pip resolution
# is not enough: an ABI mismatch or missing executable otherwise reaches every
# user as an unusable Download button. Model weights are deliberately not involved
# in this gate.
#
# THREE outcomes, not two, because the decoder answers two independent questions
# (see transcribe.PackagedDecoderProbe):
#
#   exit 0  everything loaded and ran
#   exit 1  the recogniser is broken, OR no decoder AUTHENTICATED -- a defect in
#           what we are about to publish, so the build stops
#   exit 2  the decoder authenticated but this HOST would not run it
#
# Exit 2 is a warning and not a failure, and that is the whole point of splitting
# them. Authenticity is a property of the artifact and is gated everywhere;
# executability is a property of the build machine. A build image missing an OS
# library the pinned executable load-time imports refuses it before its entry
# point runs -- Windows Server Core, which every CodeBuild Windows image is built
# on, ships no Video for Windows components and so cannot load an ffmpeg that
# imports AVICAP32.dll -- while the identical bytes run correctly on a user's
# machine. Blocking a release on that withholds a correct artifact because the
# machine that assembled it was not the machine that runs it.
#   $1 = bundled interpreter
local_voice_runtime_gate() {
  local python="$1" report status
  log "Verifying bundled local voice runtime…"
  # `if` rather than a bare assignment: under `set -e` a command substitution that
  # exits non-zero aborts the script, which would make exit 2 a hard failure again.
  if report="$(env PYTHONNOUSERSITE=1 PYTHONPATH= "$python" -s -c '
import sys

from kiro_crew.stt.engine import probe
from kiro_crew.transcribe import _packaged_ffmpeg_version_probe

state = probe()
if not state.ok:
    sys.stderr.write(f"{state.code}: {state.detail}\n")
    raise SystemExit(1)

decoder = _packaged_ffmpeg_version_probe()
if decoder.ok:
    raise SystemExit(0)
sys.stderr.write(f"{decoder.code}: {decoder.detail}\n")
raise SystemExit(1 if not decoder.authentic else 2)
' 2>&1)"; then
    status=0
  else
    status=$?
  fi
  if [ -n "$report" ]; then
    printf '    %s\n' "$report"
  fi
  case "$status" in
    0) ;;
    2)
      echo "  ⚠ the bundled decoder authenticated but does not run on THIS host." >&2
      echo "    Its bytes are the pinned payload, so the bundle is shipped as-is." >&2
      ;;
    *)
      echo "ERROR: bundled local voice runtime cannot load" >&2
      exit 1
      ;;
  esac
}

# Re-stamp every staged backend tree with <dist>, for the Linux multi-format
# path (see LINUX_TARGET_DISTS). Rewrites one generated module per tree, so it
# is cheap enough to run between electron-builder invocations. Finding the trees
# by their site-packages layout keeps this working for both the single-tree
# Linux build and macOS's two-arch layout.
restamp_backends() {
  local dist="$1" sp found=0
  while IFS= read -r sp; do
    [ -n "$sp" ] || continue
    bash "$ROOT/scripts/stamp-distribution.sh" "$dist" "$sp" >/dev/null
    found=$((found + 1))
  done < <(find "$ELECTRON_DIR/backend-dist" -type d -path "*/site-packages/kiro_crew" 2>/dev/null)
  if [ "$found" -eq 0 ]; then
    echo "ERROR: no staged backend tree found to stamp as '$dist'" >&2
    exit 1
  fi
  echo "    stamped $found backend tree(s) as dist=$dist"
}

# Recursively remove a directory tree, defeating the macOS .DS_Store/ENOTEMPTY
# race. macOS Desktop Services (Finder/Spotlight) can drop a fresh .DS_Store
# into a subdirectory *between* rm's child-sweep and its final rmdir, so a plain
# `rm -rf` aborts with ENOTEMPTY (the -f flag suppresses ENOENT, not ENOTEMPTY;
# electron-userland/electron-builder#6890). Every destructive rm of a build
# output dir in this script is exposed to that race, so route them all through
# here: sweep any .DS_Store, rm, and if the tree survives (the race re-created a
# file) sweep + retry a bounded number of times. Success is detected by the
# directory being gone — NOT by grepping stderr — so it is locale-independent
# (a non-English macOS emits a translated "Directory not empty" that a string
# match would silently miss). No-op when the path is already absent.
rm_rf_resilient() {
  local target="$1" attempt=1 max_attempts=5
  # A dangling symlink is "not -e" but must still be removed (leaving it makes
  # the following build step operate on a broken link), so treat -e OR -L as
  # present; only a truly absent path is the no-op.
  { [ -e "$target" ] || [ -L "$target" ]; } || return 0
  while : ; do
    find "$target" -name .DS_Store -delete 2>/dev/null || true
    rm -rf "$target" 2>/dev/null || true
    { [ -e "$target" ] || [ -L "$target" ]; } || return 0
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "  ⚠ '$target' survived $max_attempts rm attempts (macOS .DS_Store race?); one final attempt with errors surfaced…" >&2
      # Let a genuine, non-transient failure abort the build under `set -e`
      # instead of looping forever or masking a real permissions problem.
      rm -rf "$target"
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
}

# Invoke electron-builder. Split into its own function purely so a test can
# shadow it with a stub -- CSC_IDENTITY_AUTO_DISCOVERY=false disables macOS
# codesign identity auto-discovery for local/CI builds.
_eb_invoke() {
  CSC_IDENTITY_AUTO_DISCOVERY=false ./node_modules/.bin/electron-builder "$@"
}

# Run electron-builder (EB_ARGS as "$@"), retrying a bounded number of times
# on two transient failure classes -- and still failing the build on anything
# else, or once the retry budget is exhausted:
#
#   - macOS .DS_Store/ENOTEMPTY temp-dir race (electron-userland/electron-builder#6890):
#     the universal build's lipo-merge stage removes dist/mac-universal-<arch>-temp
#     dirs with a recursive fs.rm that has no retries of its own, and Desktop
#     Services (Finder/Spotlight) can drop a fresh .DS_Store into one mid-removal.
#   - transient network/TLS failures in electron-builder's OWN mid-build
#     fetches: the AppImage and NSIS/Squirrel targets pull their own tooling
#     through `got` AFTER the electron zip has already reported
#     progress=100%. A dropped connection ("socket hang up"), a
#     TLS-intercepted response ("self-signed certificate"), or an HTTP-level
#     answer from the artifact CDN that is retryable rather than final
#     ("Response code 504") aborts the whole build there. These are
#     per-execution network events, not build errors -- the same commit
#     passes on a plain re-run, and one matrix leg can fail while its
#     siblings go green in the same attempt (#3088, #6795).
#
# Split out of the packaging step (rather than left inline) so the
# retry/classification logic is testable in isolation, mirroring
# rm_rf_resilient above: a test extracts this function's body, shadows
# _eb_invoke with a stub that fails N times, and asserts the real retry/
# classification logic here recovers (or correctly gives up).
run_electron_builder_with_retry() {
  local attempt=1 max_attempts=3 eb_log eb_transient eb_backoff
  while : ; do
    eb_log="$(mktemp "${TMPDIR:-/tmp}/kc-eb.XXXXXX")"
    if _eb_invoke "$@" 2>&1 | tee "$eb_log"; then
      rm -f "$eb_log"; return 0
    fi
    # Classify the failure before deciding: each transient class needs its
    # own cleanup, and anything unrecognised must still abort on the first
    # failure.
    eb_transient=""
    if grep -q "ENOTEMPTY" "$eb_log"; then
      eb_transient="ds_store"
    # Socket-level errno strings and HTTP-level statuses share ONE class
    # because the class boundary here is cleanup, not protocol layer (see the
    # header comment): both arise in the same `got` fetches, and both recover
    # by simply re-fetching on the next attempt with nothing to sweep first.
    # A 5xx is transient by definition and a 429 is a rate limit that clears;
    # every OTHER 4xx (401/403/404/400) is a configuration or authorisation
    # fault that would fail identically on all three attempts, so it stays
    # unrecognised and still aborts on the first one.
    elif grep -qE "socket hang up|self[- ]signed certificate|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND" "$eb_log" \
      || grep -qE "Response code (429|5[0-9][0-9])" "$eb_log"; then
      eb_transient="network"
    fi
    if [ -n "$eb_transient" ] && [ "$attempt" -lt "$max_attempts" ]; then
      if [ "$eb_transient" = "ds_store" ]; then
        echo "  ⚠ macOS .DS_Store/ENOTEMPTY temp-dir race (attempt $attempt/$max_attempts); sweeping .DS_Store and retrying…" >&2
        find dist -name .DS_Store -delete 2>/dev/null || true
        rm -rf dist/*-temp 2>/dev/null || true
        eb_backoff=2
      else
        echo "  ⚠ transient network/TLS/HTTP failure in an electron-builder fetch (attempt $attempt/$max_attempts); retrying…" >&2
        eb_backoff=$((attempt * 10))
      fi
      rm -f "$eb_log"; attempt=$((attempt + 1)); sleep "$eb_backoff"; continue
    fi
    rm -f "$eb_log"
    echo "❌ electron-builder failed (not a known transient class, or retries exhausted)." >&2
    return 1
  done
}

# --- 1. Frontend ------------------------------------------------------------
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  log "Building dashboard (npm)…"
  # The V8 heap ceiling this build needs is pinned in website/.npmrc
  # (`node-options`), so every caller of `npm run build` gets it — not just this
  # script. See that file for why it cannot be an inline env prefix here.
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
  # POSIX PBS trees carry bin/python3.12; Windows PBS puts python.exe at
  # the tree root. Accept either.
  if [ -z "$dir" ] || { [ ! -x "$dir/bin/python3.12" ] && [ ! -x "$dir/python.exe" ]; }; then
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

  # The voice extras, in TWO steps.
  #
  # They have to be in the bundle at all because `local` is the default
  # speech-to-text provider and nothing in the app can install it later: the
  # pip-invoking endpoint was deliberately removed, so a bundle without them
  # reports the default provider as unavailable with no in-app way to fix it.
  #
  # Two steps because pip resolves an extra ATOMICALLY, and this bundle is
  # UNIVERSAL on macOS where Intel has no published pywhispercpp wheel. A single
  # `[voice]` install there fails as a whole and omits boto3 and amazon-transcribe
  # with it, taking the `transcribe` provider down alongside the `local` one it has
  # nothing to do with. So the cloud half goes in first and unconditionally, then
  # the recogniser is attempted on its own.
  #
  # `--only-binary pywhispercpp` (scoped to that one name, so kirocrew itself still
  # installs from this checkout) prevents a surprise CMake/C++ source build. Missing
  # recogniser wheels fail every supported desktop build: local dictation is the
  # default and must be usable immediately. macOS Intel is the sole legacy exception.
  env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
    "$out/bin/python3.12" -m pip install --prefer-binary \
    --no-warn-script-location --disable-pip-version-check \
    "$ROOT[voice-aws]" "imageio-ffmpeg==0.6.0"
  if ! env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
      "$out/bin/python3.12" -m pip install --prefer-binary \
      --only-binary pywhispercpp \
      --no-warn-script-location --disable-pip-version-check "$ROOT[voice]"; then
    if is_macos_intel_backend "$want_arch"; then
      log "No prebuilt speech recogniser for macOS Intel — leaving that legacy backend unsupported."
    else
      echo "ERROR: no prebuilt speech recogniser for supported target ${OS}/${want_arch:-$HOST_ARCH}" >&2
      echo "       Refusing to ship a desktop app whose default local voice provider is unusable." >&2
      exit 1
    fi
  fi

  # Stage the dashboard dist into the package's static dir.
  sp="$out/lib/python3.12/site-packages"
  log "Staging dashboard dist into kiro_crew/static/dist…"
  mkdir -p "$sp/kiro_crew/static"
  ( cd "$sp/kiro_crew/static" && rm -rf dist && cp -R "$ROOT/website/dist" dist )
  [ -f "$sp/kiro_crew/static/dist/index.html" ] || {
    echo "ERROR: dashboard dist not staged" >&2; exit 1
  }

  # Beacon provenance, stamped into the INSTALLED tree (not $ROOT) so a
  # universal build's two backends are each stamped and no state leaks into the
  # developer's checkout. pip installed from $ROOT, where the module is
  # gitignored and absent, so this is the only place it exists.
  bash "$ROOT/scripts/stamp-distribution.sh" "$KC_DISTRIBUTION" "$sp/kiro_crew"

  # Relocatable launcher script.
  cat > "$out/bin/kirocrew" <<'LAUNCH'
#!/bin/bash
set -euo pipefail
# Resolve symlinks before deriving DIR. When this launcher is reached through a
# symlink (e.g. the ~/.local/bin/kirocrew shim planted on the user's PATH),
# ${BASH_SOURCE[0]} is the symlink path, so a naive dirname points at the
# symlink's directory and execs the wrong (or missing) python3.12. macOS ships
# no `readlink -f`, so walk the symlink chain to the real wrapper location.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [ "${SOURCE:0:1}" != "/" ] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
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

  if ! is_macos_intel_backend "$want_arch"; then
    local_voice_runtime_gate "$out/bin/python3.12"
  fi

  # After pruning, so it validates what actually ships.
  stdlib_probe_gate "$out"

  # Compile the WHOLE shipped tree as checked-hash pycs, then let the runtime
  # forbid writing any.
  #
  # This is what keeps the signed .app's seal intact. codesign seals every file
  # under Contents/, so bytecode written there after signing invalidates the
  # signature and Gatekeeper refuses the app as "damaged" -- which is what
  # managed Macs report, because their policy re-evaluates instead of reusing a
  # cached accept verdict.
  #
  # Deliberately the whole tree, not the traced startup closure the Windows lane
  # ships. The pipeline signs a COMPLETE bundle, so nothing on a user's machine
  # has any business writing into it -- and a module left uncompiled is exactly
  # what creates the reason to. Windows can afford the narrower closure because
  # Authenticode seals no resource tree, so a later write there is harmless; on
  # macOS an uncovered module is a latent signature break, so coverage has to be
  # total rather than measured.
  #
  # The MODE is the other half. The prune above deletes the TIMESTAMP pycs pip
  # left behind, and deleting them is not squeamishness -- a timestamp pyc
  # records the mtime of the source it was built from, `ditto` restamps sources
  # at extraction, so every shipped timestamp pyc is guaranteed to look stale on
  # the user's machine and be rewritten in place. That rewrite IS the corruption.
  # CHECKED_HASH validates against contents, so an extraction-restamped source
  # still matches.
  #
  # `-q -f`: quiet, and force so nothing is skipped on a stale timestamp cache.
  # A failure to compile one module must not fail the build -- compileall exits
  # non-zero on a syntax error in any file it walks, including vendored samples
  # that are not importable on this interpreter -- so the exit code is reported
  # and tolerated while the coverage assertion below is what actually gates.
  log "Precompiling the shipped tree as checked-hash pycs ($(basename "$out"))…"
  env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= \
    "$out/bin/python3.12" -s -m compileall -q -f \
    --invalidation-mode checked-hash "$out/lib" \
    || echo "    compileall reported errors (unimportable vendored samples are expected)"

  # Coverage gate: the point of the exercise is that the runtime never needs to
  # write, so a tree that shipped without caches would silently reintroduce the
  # defect the moment PYTHONDONTWRITEBYTECODE is honoured.
  "$out/bin/python3.12" -s - "$out/lib" <<'COVERAGE'
import os, sys
root = sys.argv[1]
missing = []
for dirpath, dirnames, filenames in os.walk(root):
    if os.path.basename(dirpath) == "__pycache__":
        continue
    cache = os.path.join(dirpath, "__pycache__")
    for name in filenames:
        if not name.endswith(".py"):
            continue
        stem = name[:-3]
        if not any(
            f.startswith(stem + ".") and f.endswith(".pyc")
            for f in (os.listdir(cache) if os.path.isdir(cache) else ())
        ):
            missing.append(os.path.join(dirpath, name))
if missing:
    print(f"    {len(missing)} module(s) shipped without a bytecode cache", file=sys.stderr)
    for path in missing[:10]:
        print(f"      {os.path.relpath(path, root)}", file=sys.stderr)
    # A handful of unimportable vendored samples is tolerable; a wholesale miss
    # means compileall did not run and every launch would want to write.
    if len(missing) > 50:
        print("ERROR: bytecode coverage is too low to forbid runtime writes", file=sys.stderr)
        raise SystemExit(1)
COVERAGE

  echo "    $(basename "$out") size: $(du -sh "$out" 2>/dev/null | cut -f1)"
}

# Build the Windows backend tree. Separate from build_backend because the
# PBS Windows layout differs everywhere the POSIX function assumes bin/ and
# lib/python3.12: python.exe sits at the tree root, site-packages at
# Lib/site-packages, and the launcher is a .cmd shim (find-bin.js probes
# bin/kirocrew.cmd on win32; Electron unwraps the shim and spawns
# python.exe directly -- see main.js).
#   $1 = PBS interpreter dir   $2 = output dir
build_backend_windows() {
  local pbs_dir="$1" out="$2" sp root_uri

  log "Installing kiro_crew into the bundled interpreter ($(basename "$out"))…"
  mkdir -p "$(dirname "$out")"
  cp -R "$pbs_dir" "$out"
  find "$out" -name "EXTERNALLY-MANAGED" -delete 2>/dev/null || true

  env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
    "$out/python.exe" -m pip install --prefer-binary \
    --no-warn-script-location --disable-pip-version-check "$ROOT"

  # Git Bash normally rewrites a plain /d/a/... argument for a native Windows
  # process, which is why the core install above works. Appending `[voice]`
  # defeats that MSYS path conversion and pip receives the literal POSIX path,
  # then rejects it as an invalid requirement. Derive a real file URI from the
  # native interpreter's cwd and use explicit PEP 508 direct references for both
  # extras, so spaces and the drive-letter boundary are unambiguous too.
  root_uri="$(cd "$ROOT" && "$out/python.exe" -c \
    'import sys; from pathlib import Path; sys.stdout.write(Path.cwd().as_uri())')"

  # The voice extras, in two steps so an independent AWS-provider dependency
  # cannot be omitted by recogniser resolution. Windows is a supported local-
  # dictation target, so the recogniser is a hard gate. Model weights remain an
  # explicit one-click download after installation.
  #
  # They have to be in the bundle at all because `local` is the default
  # speech-to-text provider and nothing in the app can install it later: the
  # pip-invoking endpoint was deliberately removed, so a bundle without them
  # reports the default provider as unavailable with no in-app way to fix it.
  #
  env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
    "$out/python.exe" -m pip install --prefer-binary \
    --no-warn-script-location --disable-pip-version-check \
    "kirocrew[voice-aws] @ $root_uri" "imageio-ffmpeg==0.6.0"
  env PYTHONNOUSERSITE=1 PYTHONPATH= KIROCREW_SKIP_FRONTEND=1 \
    "$out/python.exe" -m pip install --prefer-binary \
    --only-binary pywhispercpp \
    --no-warn-script-location --disable-pip-version-check \
    "kirocrew[voice] @ $root_uri"

  sp="$out/Lib/site-packages"
  log "Staging dashboard dist into kiro_crew/static/dist…"
  mkdir -p "$sp/kiro_crew/static"
  ( cd "$sp/kiro_crew/static" && rm -rf dist && cp -R "$ROOT/website/dist" dist )
  [ -f "$sp/kiro_crew/static/dist/index.html" ] || {
    echo "ERROR: dashboard dist not staged" >&2; exit 1
  }

  # Beacon provenance. The NSIS installer has no KNOWN_DISTRIBUTIONS value, so
  # the honest answer is "source", but stamp it explicitly rather than relying on
  # the module's absence: pip installed from $ROOT, and a stale stamp left in a
  # developer's checkout would otherwise be copied in and mislabel the build.
  bash "$ROOT/scripts/stamp-distribution.sh" "$KC_DISTRIBUTION" "$sp/kiro_crew"

  # Relocatable launcher shim: %~dp0 is the .cmd's own directory (bin\),
  # so the interpreter resolves relative to the bundle wherever it lands.
  mkdir -p "$out/bin"
  printf '@echo off\r\n"%%~dp0..\\python.exe" -s -m kiro_crew %%*\r\n' > "$out/bin/kirocrew.cmd"

  log "Verifying self-containment ($(basename "$out"))…"
  PYTHONNOUSERSITE=1 "$out/python.exe" -s -m kiro_crew --version >/dev/null \
    || { echo "ERROR: bundled backend is NOT self-contained (missing dep under PYTHONNOUSERSITE=1)" >&2; exit 1; }

  log "Pruning bundle ($(basename "$out"))…"
  ( cd "$out"
    find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    find Lib/site-packages -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf Lib/test Lib/idlelib Lib/tkinter Lib/turtledemo Lib/ensurepip Lib/lib2to3 \
           include libs tcl \
           Lib/site-packages/kiro_crew/_vendor/llama_cpp_libs/linux_aarch64 \
           Lib/site-packages/kiro_crew/_vendor/llama_cpp_libs/linux_x86_64 \
           Lib/site-packages/kiro_crew/_vendor/llama_cpp_libs/macos_arm64 \
           Lib/site-packages/kiro_crew/_vendor/llama_cpp_libs/macos_x86_64 \
           2>/dev/null || true
    rm -f DLLs/_tkinter.pyd DLLs/tcl*.dll DLLs/tk*.dll 2>/dev/null || true )

  local_voice_runtime_gate "$out/python.exe"

  # After pruning, so it validates what actually ships.
  stdlib_probe_gate "$out"

  # Trace the real gateway import after the final prune and ship checked-hash
  # pycs for exactly that closure. Windows can consume these beside the source
  # without invalidating an Authenticode signature, avoiding the first launch's
  # thousand-file cache write while keeping unrelated modules out of the bundle.
  log "Precompiling Windows gateway startup modules ($(basename "$out"))…"
  env PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH= \
    "$out/python.exe" -s "$ROOT/packaging/precompile_windows.py" \
    --root "$out" --module kiro_crew.cli_server

  echo "    $(basename "$out") size: $(du -sh "$out" 2>/dev/null | cut -f1)"
}

# Stdlib-probe agreement gate: every package bundle-integrity.js probes must be
# present, as an importable package, in the tree we just built. That module
# refuses to spawn a backend whose stdlib looks incomplete, so a name it probes
# that this bundle does not ship (a Python bump turning a package back into a
# module, a rename, or a new prune above) would refuse EVERY launch of a healthy
# app — a permanent failure strictly worse than the transient one it prevents.
# Failing the build here converts that into a build error the developer sees.
#   $1 = built backend tree
stdlib_probe_gate() {
  local out="$1"
  if ! command -v node >/dev/null 2>&1; then
    log "node unavailable — SKIPPING stdlib-probe gate for $(basename "$out")"
    return 0
  fi
  log "Verifying bundle-integrity.js stdlib probes resolve ($(basename "$out"))…"
  node -e '
    const fs=require("fs"), path=require("path");
    const { findMissingBundleParts, REQUIRED_STDLIB_PARTS } =
      require(path.join(process.argv[1], "bundle-integrity"));
    const out = process.argv[2];
    const missing = findMissingBundleParts(fs, path, out);
    if (missing.length) {
      console.error(`ERROR: bundle-integrity.js probes ${REQUIRED_STDLIB_PARTS.length} stdlib `
        + `packages; this bundle is missing: ${missing.join(", ")}`);
      console.error("       The launcher would refuse to start this bundle on every launch.");
      console.error("       Fix the prune step, or update REQUIRED_STDLIB_PARTS in");
      console.error("       website/electron/bundle-integrity.js to match the shipped stdlib.");
      process.exit(1);
    }
  ' "$ELECTRON_DIR" "$out" || exit 1
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
      // Normalize separators: under Git Bash on Windows the expected path
      // arrives with forward slashes while Node resolves backslashes.
      if (path.resolve(resolved) !== path.resolve(expected)) {
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
rm_rf_resilient "$ELECTRON_DIR/backend-dist"
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
  elif [ "$OS" = "windows" ]; then
    PBS_PATTERN="cpython-3.12*-windows-${ARCH}-none"
  else
    PBS_PATTERN="cpython-3.12*-linux-${ARCH}-gnu"
  fi
  PBS_DIR="$(provision_pbs "cpython-3.12" "$PBS_PATTERN")"
  echo "    PBS interpreter: $PBS_DIR"

  if [ "$OS" = "windows" ]; then
    build_backend_windows "$PBS_DIR" "$ELECTRON_DIR/backend-dist/kirocrew-backend"
    resolver_gate "$ELECTRON_DIR/backend-dist/kirocrew-backend/bin/kirocrew.cmd" ""
  else
    build_backend "$PBS_DIR" "$ELECTRON_DIR/backend-dist/kirocrew-backend" ""
    resolver_gate "$ELECTRON_DIR/backend-dist/kirocrew-backend/bin/kirocrew" ""
  fi
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
      "-c.mac.icon=icon-nightly.icns"
      "-c.linux.icon=icon-nightly.png"
      "-c.win.icon=icon-nightly.png"
      # Finder/Dock title (CFBundleDisplayName) mirrors the spaced display
      # name. Never override CFBundleName: Electron derives its helper-app
      # paths from that internal name, so changing it while productName stays
      # space-free makes the packaged app abort with "Unable to find helper
      # app". Nightly re-overrides the static display name to keep its suffix.
      "-c.mac.extendInfo.CFBundleDisplayName=Kiro Crew Nightly"
      # Linux packages key their INSTALL identity off the package name, so
      # nightly needs its own or dpkg/rpm treat a nightly install as an UPGRADE
      # of stable and remove it -- the same hazard as the nsis.guid below, from
      # the Linux side. Three names move together because all three are
      # per-install-unique paths a second channel must not claim:
      #
      #   packageName    -> the dpkg/rpm package identity
      #   executableName -> /usr/bin/<name> and /opt/<Product>/<name>
      #   desktopName    -> /usr/share/applications/<name>, and (via
      #                     linux.syncDesktopName) Electron's app_id and the
      #                     entry's StartupWMClass, which must keep matching
      #
      # productName already differs, so the /opt directory does not collide.
      "-c.deb.packageName=kirocrew-nightly"
      "-c.rpm.packageName=kirocrew-nightly"
      "-c.linux.executableName=kirocrew-desktop-nightly"
      "-c.extraMetadata.desktopName=kirocrew-desktop-nightly.desktop"
      # The npm package `name` is per-channel for the same reason the Linux
      # package name is. It is not build metadata: appInfo derives
      # updaterCacheDirName from it (`sanitizedName.toLowerCase() +
      # "-updater"`), Electron derives the userData directory from it, and NSIS
      # receives it as ${APP_PACKAGE_NAME}. Shared, that makes nightly and
      # stable write ONE %LOCALAPPDATA%\<name>-updater and ONE
      # %APPDATA%\<name> -- so uninstalling either channel would delete the
      # other's pending update download, its differential baseline, and its
      # window state. productName and nsis.guid already separate the install
      # directory and the registry key; this separates the per-user state they
      # do not cover.
      "-c.extraMetadata.name=kirocrew-desktop-nightly"
      # Squirrel.Windows keyed the INSTALL identity off squirrelWindows.name;
      # NSIS keys it off two separate things, and nightly needs both:
      #
      # 1. The uninstall/upgrade registry key is a GUID, defaulting to
      #    UUID v5(appId) (app-builder-lib NsisTarget.js: `options.guid ||
      #    UUID.v5(appInfo.id, ELECTRON_BUILDER_NS_UUID)`). Nightly shares
      #    appId with production (see above), so WITHOUT an explicit guid both
      #    channels claim one registry key and an assisted nightly install
      #    adopts -- then on uninstall removes -- the stable entry. The value
      #    below is exactly what electron-builder would derive from a
      #    hypothetical `com.amazon.kiro.crew.nightly` appId, so it is stable,
      #    reproducible, and collision-free without moving the real appId.
      # 2. The install DIRECTORY comes from productFilename (i.e. the spaced
      #    productName above) only because nsis.oneClick is false --
      #    getWindowsInstallationDirName() falls back to the npm package name
      #    under one-click. That is why the target is an assisted installer.
      #
      # As on mac, this identity persists on user machines from first install:
      # changing it later orphans installed updaters, so it is pinned from the
      # first shipped build.
      "-c.nsis.guid=0f417bf9-2759-51d6-acfb-f864805d1f41"
      # WINDOWS-ONLY appId split. The shared appId above is required on macOS
      # (Squirrel.Mac validates against the host's designated requirement, which
      # pins the bundle id), but on Windows it reaches ${APP_ID}, which the NSIS
      # template uses for two registrations that are global per-id rather than
      # per-install: WinShell::SetLnkAUMI stamps the AppUserModelID onto the
      # desktop and Start Menu shortcuts, and WinShell::UninstAppUserModelId
      # removes that registration outright.
      #
      # The update path is safe on its own: nsis.allowToChangeInstallationDirectory
      # is false, so that define is never emitted, setIsTryToKeepShortcuts always
      # yields "true", and the old uninstaller runs with --keep-shortcuts, which
      # skips the deregistration. A real UNINSTALL does not. Uninstall one
      # channel and WinShell::UninstAppUserModelId runs against the id BOTH
      # channels share, deregistering the AppUserModelID the surviving channel's
      # shortcuts still carry -- its desktop shortcut then resolves to a dead
      # registration and the shell reports that app as relocated or missing even
      # though its .exe is untouched.
      #
      # Scoped to `win` deliberately: appInfo.id prefers the platform-specific
      # value, so a top-level -c.appId would also move the macOS bundle id and
      # strand every installed mac app's updates. This is the same identity
      # main.js already claims at runtime via app.setAppUserModelId, so the
      # packaged shortcuts and the running process finally agree.
      "-c.win.appId=com.amazon.kiro.crew.nightly"
    )
  fi
  # Start from a pristine output dir. A prior interrupted universal build can
  # leave dist/mac-universal-<arch>-temp dirs behind (with a .DS_Store inside);
  # those linger and re-trip the ENOTEMPTY cleanup below on every later run.
  # This pre-clean is itself exposed to the .DS_Store race (Finder re-drops one
  # mid-removal), so it MUST go through the resilient helper — a plain rm here
  # aborts the build before the retry loop below is ever reached.
  # Runs ONCE, before any invocation: the Linux path invokes electron-builder
  # per target, and clearing between them would delete the previous artifact.
  rm_rf_resilient dist

  # Each electron-builder invocation below goes through
  # run_electron_builder_with_retry (defined above, alongside rm_rf_resilient)
  # so the macOS .DS_Store/ENOTEMPTY race AND transient network/TLS failures
  # in electron-builder's own mid-build fetches are both retried, not just
  # the former.
  if [ "$OS" = "darwin" ]; then
    EB_ARGS+=( --mac )
    [ "$UNIVERSAL" = "1" ] && EB_ARGS+=( --universal )
    run_electron_builder_with_retry "${EB_ARGS[@]}"
  elif [ "$OS" = "windows" ]; then
    EB_ARGS+=( --win )
    run_electron_builder_with_retry "${EB_ARGS[@]}"
  else
    # One invocation PER FORMAT, each preceded by its own beacon stamp, so the
    # AppImage and the deb do not both claim the label of whichever was built
    # last. Targets are named explicitly rather than letting package.json's
    # target array drive a single invocation, because a single invocation shares
    # one stamped backend tree between both artifacts.
    for pair in "${LINUX_TARGET_DISTS[@]}"; do
      target="${pair%%:*}"; dist="${pair##*:}"
      log "Packaging Linux ${target} (dist=${dist})…"
      restamp_backends "$dist"
      run_electron_builder_with_retry "${EB_ARGS[@]}" --linux "$target"
    done
  fi
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
ls -1 "$ELECTRON_DIR/dist/"*.{dmg,AppImage,deb,rpm,zip,exe} 2>/dev/null | sed 's/^/   /' || true
echo ""
echo "    The .app embeds the backend, so it runs with no PATH kirocrew needed."
