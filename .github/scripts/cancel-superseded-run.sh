#!/usr/bin/env bash
# Cancel this CI run when its Fast Gate run was superseded on a push to main.
# Invoked from the `cancel-superseded-run` job in .github/workflows/ci.yml with
#
#   GH_TOKEN REPO SHA EVENT BRANCH HEAD_REPO RUN_ID
#
# in the environment. That job is the only one in ci.yml holding
# `actions: write`, and only a push to main reaches it.
#
# ## What it does
#
# It runs await-fast-gate.sh as a probe (SUPERSEDED_FILE set), so whether the
# gate run for this commit was evicted before it started is decided by the
# barrier's own code, from the same reads. When the probe records that proof,
# this script cancels RUN_ID, this CI run. Await Fast Gate is still running at
# that point, waiting for exactly this, so the run concludes `cancelled`, the
# outcome a CI run evicted from its own group gets, and Coverage Gate
# (`!cancelled()`) is skipped.
#
# Every other probe outcome (the gate passed, failed, started and was
# cancelled, never appeared, or could not be read, or the proof did not hold)
# cancels nothing: this job exits 0 and Await Fast Gate reports the verdict, so
# this job can never stand in for the barrier.
#
# ## Why it cannot turn a red green
#
# Its one write is `POST /actions/runs/$RUN_ID/cancel`, and a cancelled run is
# not a green one. The matrix waits on Await Fast Gate, never on this job, and
# the barrier cannot pass on a gate run that was cancelled. A job that already
# failed in this run keeps its failed check after the run is cancelled. A cancel
# that is refused, or that has not landed after LAND_BUDGET seconds, exits 1,
# and Await Fast Gate fails closed after its own wait.
#
# ## Test seams
#
# AWAIT_FG_NOW and AWAIT_FG_SLEEP work as in await-fast-gate.sh, and reach the
# probe unchanged, so test_await_fast_gate_poll.py drives both scripts in zero
# wall-clock time.

set -euo pipefail

# How long the cancel may take to land. The job's timeout-minutes covers the
# probe's TOTAL_BUDGET plus LAND_BUDGET.
LAND_BUDGET=90

now() { ${AWAIT_FG_NOW:-date +%s}; }
nap() { ${AWAIT_FG_SLEEP:-sleep} "$1"; }

if [ "${EVENT:-}" != "push" ] || [ "${BRANCH:-}" != "main" ] || [ -z "${RUN_ID:-}" ]; then
  echo "::error::This job cancels only a push run on main and needs RUN_ID" \
       "(EVENT=${EVENT:-unset} BRANCH=${BRANCH:-unset}); nothing was cancelled."
  exit 1
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/cancel-superseded.XXXXXX")"
cleanup() {
  rm -f "$work/proof" "$work/probe.log" "$work/err"
  rmdir "$work" 2>/dev/null || true
}
trap cleanup EXIT

# The probe's own annotations belong to Await Fast Gate, which prints them for
# the same reads. Its output is captured and shown indented, so a line starting
# with `::` is not raised a second time from this job.
probe_rc=0
SUPERSEDED_FILE="$work/proof" bash "$(dirname "$0")/await-fast-gate.sh" \
  >"$work/probe.log" 2>&1 || probe_rc=$?
sed 's/^/  probe| /' "$work/probe.log"

if [ ! -s "$work/proof" ]; then
  echo "The Fast Gate run for this commit was not superseded (probe exit $probe_rc)," \
       "so there is nothing to cancel. Await Fast Gate reports the verdict."
  exit 0
fi

proof="$(head -c 600 "$work/proof" | tr '\n' ' ')"
echo "::warning::${proof}Nothing judged this commit, so this job cancels the CI run" \
     "instead of letting it report a failure. For a verdict on this commit, re-run" \
     "that Fast Gate run, then this run."
if ! gh api --method POST "repos/$REPO/actions/runs/$RUN_ID/cancel" >/dev/null 2>"$work/err"; then
  echo "::error::Could not cancel this run ($(head -c 300 "$work/err" 2>/dev/null | tr '\n' ' '))," \
       "so Await Fast Gate fails closed instead."
  exit 1
fi
asked="$(now)"
while [ $(( $(now) - asked )) -lt "$LAND_BUDGET" ]; do
  nap 10
done
echo "::error::This run was asked to cancel but is still running after ${LAND_BUDGET}s;" \
     "Await Fast Gate fails closed."
exit 1
