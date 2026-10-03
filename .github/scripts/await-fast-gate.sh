#!/usr/bin/env bash
# The Fast Gate barrier's poll: read the Fast Gate run for THIS commit and fail
# closed until it concludes success, so ci.yml's heavy matrix never starts on a
# gate that was not confirmed. Invoked from the `await-fast-gate` job in
# .github/workflows/ci.yml with the identity of the commit under test in the
# environment:
#
#   GH_TOKEN REPO SHA EVENT BRANCH HEAD_REPO
#
# and nothing else. The job's step is one line -- `bash
# .github/scripts/await-fast-gate.sh` -- so this file IS the barrier's logic.
#
# ## Why a script and not an inline `run:` body
#
# The barrier is not enforced by GitHub: every property that makes its read
# trustworthy (identify the run by its full identity triple, fail closed in all
# three unreadable directions) is asserted by test_fast_gate_barrier.py. Keeping
# the body in a file lets that test run the WHOLE poll against a stubbed `gh` and
# a fake clock -- every classification case end to end, not just the jq selector
# and conclusion `case` as isolated fragments. The workflow gains nothing from
# carrying the shell inline, and the test gains coverage of the loop's control
# flow rather than its two islands alone.
#
# ## Why `.github/scripts/` and not the repo-root `scripts/`
#
# Root `scripts/` holds product and dev tooling a contributor runs by hand. This
# is workflow-only glue: it reads the commit identity ci.yml resolves from
# `github.event.*` and has exactly one caller. It lives next to the workflow that
# is its only caller, alongside the other extracted workflow scripts.
#
# ## Test seams (production defaults are exact)
#
# Four values are read through `${VAR:-default}` so the test can drive the loop
# without real waits, and every default reproduces the inline body byte for byte:
#
#   APPEAR_BUDGET   180   seconds a missing run is tolerated as the start-up race
#   TOTAL_BUDGET    720   seconds the whole poll may run before failing closed
#   AWAIT_FG_NOW    date +%s   the clock; overridden with a script that prints a
#                              fake, advancing epoch so the budgets are reached in
#                              zero wall-clock time
#   AWAIT_FG_SLEEP  sleep      the wait between polls; overridden with `:` (a
#                              no-op) so the test spins the loop instantly
#
# On the real runner none of the four is set, so the poll is identical to the
# body it replaced. `gh` and `jq` are found on PATH exactly as before, which is
# also how the test substitutes a stub `gh`.

set -euo pipefail

# Both workflows start from the same webhook, so Fast Gate's run may not
# be visible for the first few seconds. APPEAR_BUDGET covers that race;
# after it, a missing run is a real misconfiguration and fails.
APPEAR_BUDGET="${APPEAR_BUDGET:-180}"
TOTAL_BUDGET="${TOTAL_BUDGET:-720}"

# The clock and the wait, read through a seam that defaults to the real tools.
# `now` is a function so the test can point it at a fake-clock command; without
# an override it is `date +%s`, unchanged.
now() { ${AWAIT_FG_NOW:-date +%s}; }
nap() { ${AWAIT_FG_SLEEP:-sleep} "$1"; }

started="$(now)"
seen=false

while :; do
  elapsed=$(( $(now) - started ))

  runs="$(gh api --method GET \
    "repos/$REPO/actions/workflows/fast-gate.yml/runs" \
    -f "head_sha=$SHA" -f "event=$EVENT" -f "branch=$BRANCH" \
    -f per_page=100 2>/dev/null || true)"

  # `branch=` is passed to the API as a narrowing hint, but the match is
  # re-asserted here: a filter the server silently ignores would hand
  # back another PR's run, and selecting max_by(.id) out of an unfiltered
  # list would pick the NEWEST such run -- the most likely one to be a
  # different PR's. Filter first, collapse second.
  latest="$(printf '%s' "$runs" \
    | jq -c --arg branch "$BRANCH" --arg repo "$HEAD_REPO" \
        '[(.workflow_runs // [])[]
          | select(.head_branch == $branch)
          | select(.head_repository.full_name == $repo)]
         | if length == 0 then null else max_by(.id) end' \
        2>/dev/null || echo null)"

  if [ "$latest" = "null" ] || [ -z "$latest" ]; then
    if [ "$elapsed" -ge "$APPEAR_BUDGET" ]; then
      echo "::error::No Fast Gate run found for $SHA on $HEAD_REPO@$BRANCH" \
           "($EVENT) after ${elapsed}s. The gates cannot be confirmed, so the" \
           "matrix is not cleared to run."
      exit 1
    fi
    nap 10
    continue
  fi

  status="$(printf '%s' "$latest" | jq -r '.status')"
  conclusion="$(printf '%s' "$latest" | jq -r '.conclusion // ""')"
  url="$(printf '%s' "$latest" | jq -r '.html_url')"

  if [ "$seen" = false ]; then
    echo "Fast Gate run: $url"
    seen=true
  fi

  if [ "$status" = "completed" ]; then
    case "$conclusion" in
      success)
        echo "Fast Gate passed. Releasing the heavy jobs."
        exit 0
        ;;
      action_required)
        # A fork run still awaiting maintainer approval is reported as
        # completed/action_required. That is pending, not decided, so keep
        # polling under TOTAL_BUDGET -- otherwise which of the two pending
        # runs a maintainer approves first decides whether the matrix runs.
        status="awaiting maintainer approval"
        ;;
      *)
        echo "::error::Fast Gate concluded '$conclusion' ($url)." \
             "Fix the gate it reports; the heavy jobs are skipped on purpose."
        exit 1
        ;;
    esac
  fi

  if [ "$elapsed" -ge "$TOTAL_BUDGET" ]; then
    echo "::error::Fast Gate still '$status' after ${elapsed}s ($url)." \
         "Failing closed rather than starting the matrix unverified."
    exit 1
  fi

  nap 10
done
