#!/usr/bin/env bash
# render-slot.sh: run ONE headless-browser job while holding one of a few shared slots.
#
# Why: one software-rendered headless browser uses about 4 CPU cores. Seven workers
# rendering at once took the 32-core host to a load of 535. This wrapper lets at most
# SLOTS browser jobs run at the same time across ALL workers, and makes the rest wait
# in line instead of each one polling the load and starving.
#
# Use:   render-slot.sh <command> [args...]
#   e.g. render-slot.sh node tools/record.mjs out 0 600
# The command must start at most ONE browser. Run chunks one after another, each
# through this wrapper, or start a few wrapped jobs and let them queue.
#
# It waits for a free slot (no time limit), runs the command, frees the slot when the
# command ends for any reason, and returns the command's exit code.

set -u
SLOTS="${RENDER_SLOTS:-4}"
DIR="${XR_UI:?set XR_UI}/tools/.render-slots"
mkdir -p "$DIR"

if [ "$#" -lt 1 ]; then
  echo "usage: render-slot.sh <command> [args...]" >&2
  exit 2
fi

waited=0
while true; do
  i=1
  while [ "$i" -le "$SLOTS" ]; do
    lock="$DIR/slot-$i.lock"
    exec 9>"$lock"
    if flock -n 9; then
      echo "render-slot: got slot $i of $SLOTS after ${waited}s: $*" >&2
      "$@"
      code=$?
      flock -u 9
      exec 9>&-
      exit "$code"
    fi
    exec 9>&-
    i=$((i + 1))
  done
  sleep 5
  waited=$((waited + 5))
  if [ $((waited % 60)) -eq 0 ]; then
    echo "render-slot: still waiting for a slot (${waited}s)" >&2
  fi
done
