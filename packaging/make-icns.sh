#!/usr/bin/env bash
# Regenerate the macOS .icns bundles from the 1024x1024 PNG sources.
#
# Why this exists: electron-builder's own PNG->icns converter writes the legacy
# `icp4` / `icp5` slots (16pt / 32pt @1x) with PNG payloads. macOS decodes those
# two slots as raw ARGB, so every small-icon consumer (Spotlight rows, list
# views, app pickers such as Logi Options+) renders the compressed bytes as
# colored static instead of the icon. Apple's `iconutil` emits `ic04` / `ic05`
# for those sizes, which macOS decodes correctly — so we ship a pre-built,
# iconutil-generated .icns and point `mac.icon` at it.
#
# Run this after changing icon.png or icon-nightly.png, then commit the .icns.
# macOS only (iconutil ships with Xcode command line tools).
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "make-icns.sh: macOS only (needs iconutil/sips) — skipping." >&2
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ELECTRON_DIR="$ROOT/website/electron"

# Apple's required representations: <px> <iconset basename>
SIZES=(
  "16 icon_16x16"
  "32 icon_16x16@2x"
  "32 icon_32x32"
  "64 icon_32x32@2x"
  "128 icon_128x128"
  "256 icon_128x128@2x"
  "256 icon_256x256"
  "512 icon_256x256@2x"
  "512 icon_512x512"
  "1024 icon_512x512@2x"
)

for source_png in icon.png icon-nightly.png; do
  src="$ELECTRON_DIR/$source_png"
  [ -f "$src" ] || { echo "make-icns.sh: missing source PNG $src" >&2; exit 1; }
  out="$ELECTRON_DIR/${source_png%.png}.icns"
  staging="$(mktemp -d)/${source_png%.png}.iconset"
  mkdir -p "$staging"
  for spec in "${SIZES[@]}"; do
    set -- $spec
    sips -z "$1" "$1" "$src" --out "$staging/$2.png" >/dev/null
  done
  iconutil -c icns "$staging" -o "$out"
  rm -rf "$(dirname "$staging")"
  echo "  wrote $out"
done
