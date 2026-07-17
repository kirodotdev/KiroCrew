#!/bin/bash
# Copy ~/.kirocrew into .kirocrew-dev/ for local development.
# Safe to re-run — wipes .kirocrew-dev first so you get a clean snapshot.
#
# Usage: ./dev-seed.sh
set -e

SRC="$HOME/.kirocrew"
DST="$(cd "$(dirname "$0")" && pwd)/.kirocrew-dev"

if [ ! -d "$SRC" ]; then
  echo "No ~/.kirocrew found — nothing to seed."
  exit 0
fi

if [ -d "$DST" ]; then
  # Refuse to rm -rf if .kirocrew-dev is a symlink (could follow to unrelated dir)
  if [ -L "$DST" ]; then
    echo "ERROR: .kirocrew-dev is a symlink — refusing to remove. Delete it manually."
    exit 1
  fi
  echo "Removing existing .kirocrew-dev/ ..."
  rm -rf "$DST"
fi

echo "Copying ~/.kirocrew → .kirocrew-dev/ ..."
cp -R "$SRC" "$DST"

echo "Done. Start the gateway with:"
echo "  KIROCREW_HOME=.kirocrew-dev bin/kirocrew gateway"
