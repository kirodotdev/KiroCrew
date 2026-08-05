#!/usr/bin/env bash
# Ad-hoc re-sign native extension libraries in a Python environment's
# site-packages so macOS code-signing validation does not SIGKILL the
# interpreter when it loads them.
#
# Why this exists: pip wheels ship `.so`/`.dylib` files with a lightweight
# "linker-signed" ad-hoc signature (codesign flags 0x20002). On recent macOS
# (Apple Silicon, with the hardware-backed Code Signing Monitor) that flavor is
# validated flakily — when a code page fails the per-page hash check on
# demand-paging, the kernel kills the process with
# `EXC_BAD_ACCESS (SIGKILL (Code Signature Invalid))` /
# `CODESIGNING, Code 2, Invalid Page`. This bites numpy especially (numpy.random
# loads ~9 compiled extensions), breaking `make test` and the PyInstaller freeze
# in `make desktop` / `make backend-bin`.
#
# Re-signing with `codesign --force --sign -` replaces the linker-signed
# signature with a normal ad-hoc one (flags 0x2) carrying complete, valid
# per-page hashes, which the kernel validates deterministically.
#
# Usage: resign-macos-libs.sh [PYTHON]
#   PYTHON  interpreter whose site-packages to re-sign (default: python3)
#
# No-op on non-macOS. Safe to run repeatedly (idempotent).
set -euo pipefail

# macOS-only; silently succeed elsewhere so the build flow stays portable.
if [ "$(uname -s)" != "Darwin" ]; then
  exit 0
fi

if ! command -v codesign >/dev/null 2>&1; then
  echo "▶ resign-macos-libs: codesign not found — skipping." >&2
  exit 0
fi

PY="${1:-python3}"
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  echo "▶ resign-macos-libs: interpreter '$PY' not found — skipping." >&2
  exit 0
fi

# Resolve the interpreter's site-packages (works for venvs and system installs).
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
if [ -z "$SITE" ] || [ ! -d "$SITE" ]; then
  echo "▶ resign-macos-libs: could not resolve site-packages for '$PY' — skipping." >&2
  exit 0
fi

printf '\n\033[1;36m▶ Re-signing native libs in %s …\033[0m\n' "$SITE"
count=0
while IFS= read -r lib; do
  codesign --force --sign - "$lib" >/dev/null 2>&1 || {
    echo "  ! failed to re-sign: $lib" >&2
    continue
  }
  count=$((count + 1))
done < <(find "$SITE" -type f \( -name "*.so" -o -name "*.dylib" \))

echo "  re-signed $count native libraries (ad-hoc)."
