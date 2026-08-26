#!/bin/bash
# Copy the KiroCrew data home into .kirocrew-dev/ for local development.
# Safe to re-run — wipes .kirocrew-dev first so you get a clean snapshot.
#
# Usage: ./dev-seed.sh
set -e

# The data home moved from the top-level ~/.kirocrew to ~/.kiro/crew. Prefer
# the current location; fall back to the legacy one for a box that still uses
# it so this script doesn't silently no-op.
if [ -d "$HOME/.kiro/crew" ]; then
  SRC="$HOME/.kiro/crew"
elif [ -d "$HOME/.kirocrew" ]; then
  SRC="$HOME/.kirocrew"
else
  SRC=""
fi
DST="$(cd "$(dirname "$0")" && pwd)/.kirocrew-dev"

if [ -z "$SRC" ]; then
  echo "No ~/.kiro/crew or ~/.kirocrew found — nothing to seed."
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

echo "Copying $SRC → .kirocrew-dev/ ..."
cp -R "$SRC" "$DST"

echo "Done. Start the gateway with:"
echo "  KIROCREW_HOME=.kirocrew-dev bin/kirocrew gateway"
