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
# The barrier only reads: every `gh api` call below is a GET, and the job's
# token is `actions: read`. The one write that a superseded push needs,
# cancelling this CI run, belongs to the `cancel-superseded-run` job, which runs
# only on a push to main and runs this same file as a probe (see
# cancel-superseded-run.sh and SUPERSEDED_FILE below).
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
#
# ## The probe switch
#
# SUPERSEDED_FILE is unset in the barrier job. cancel-superseded-run.sh sets it
# to a path when it runs this file as a probe: a proven supersession is then
# written to that file and the probe exits 1 at once instead of waiting. Every
# other outcome is the barrier's own, so the probe and the barrier reach their
# verdicts by the same code. The switch can only end the script with a non-zero
# exit; it never reaches the `success)` arm.

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

# How long the barrier waits, once it has proven its gate run superseded, for
# the cancel-superseded-run job to cancel this run before it fails closed; and
# how far past a gate run's cancellation the run that evicted it may report its
# start. The job's timeout-minutes covers TOTAL_BUDGET plus CANCEL_WAIT.
CANCEL_WAIT=150
SUPERSEDE_SLACK=60
SUPERSEDED_FILE="${SUPERSEDED_FILE:-}"

# A cancelled gate on a push to main that was SUPERSEDED, not judged.
#
# On a push, fast-gate.yml and ci.yml each keep one running and one pending run
# per ref, and a run entering the group evicts the pending one as `cancelled`
# before any of its jobs start. The two groups drain independently, so this CI
# run can be running while its Fast Gate run still waits; the next push to main,
# or a re-run of an older Fast Gate run, then evicts it. Nothing judged this
# commit, so the outcome that fits is the one a CI run evicted from its own
# group gets: `cancelled`. The cancel-superseded-run job makes the same proof
# and cancels the run; this job waits for that while it is still running, so it
# ends `cancelled` rather than `failure`. The heavy matrix is never released on
# this path.
#
# Supersession is proven from two reads, never assumed:
#   1. the cancelled gate run's attempt has no jobs, so it never started; and
#   2. another Fast Gate push run on this branch started between the gate run's
#      creation and its cancellation (plus SUPERSEDE_SLACK seconds); the one
#      that started closest to the cancellation is named as the run whose entry
#      into the group evicted it.
# Proven, EVICTOR names that run; a read that fails, or a proof that does not
# hold, leaves EVICTOR empty and the caller fails closed as for any other
# cancelled gate. Only a push reaches this: a pull request's CI run is cancelled
# together with its gate run, and a merge group's ref is unique to the group.
EVICTOR=""
prove_superseded() {
  local gate="$1" gid attempt created cancelled_at jobs njobs others
  EVICTOR=""
  gid="$(printf '%s' "$gate" | jq -r '.id')"
  attempt="$(printf '%s' "$gate" | jq -r '.run_attempt // 1')"
  created="$(printf '%s' "$gate" | jq -r '.created_at // ""')"
  cancelled_at="$(printf '%s' "$gate" | jq -r '.updated_at // ""')"
  if [ -z "$created" ] || [ -z "$cancelled_at" ]; then
    echo "::warning::Cannot tell whether the cancelled Fast Gate run was superseded" \
         "(missing run timestamps)."
    return 0
  fi

  if ! jobs="$(gh api --method GET \
    "repos/$REPO/actions/runs/$gid/attempts/$attempt/jobs" -f per_page=1 2>/dev/null)"; then
    echo "::warning::Could not read the jobs of the cancelled Fast Gate run $gid," \
         "so it is not treated as superseded."
    return 0
  fi
  njobs="$(printf '%s' "$jobs" | jq -r '.total_count // "unknown"' 2>/dev/null || echo unknown)"
  if [ "$njobs" != "0" ]; then
    # It started (or its jobs could not be counted): a gate that ran on this
    # commit and was then cancelled is a verdict, and it fails closed.
    return 0
  fi

  if ! others="$(gh api --method GET \
    "repos/$REPO/actions/workflows/fast-gate.yml/runs" \
    -f "event=push" -f "branch=$BRANCH" -f per_page=100 2>/dev/null)"; then
    echo "::warning::Could not list Fast Gate runs on $BRANCH, so the cancelled" \
         "run $gid is not treated as superseded."
    return 0
  fi
  EVICTOR="$(printf '%s' "$others" \
    | jq -r --arg branch "$BRANCH" --arg repo "$HEAD_REPO" --argjson gid "$gid" \
        --arg from "$created" --arg to "$cancelled_at" --argjson slack "$SUPERSEDE_SLACK" \
        '(.workflow_runs // [])
         | map(select(.id != $gid
                      and .event == "push"
                      and .head_branch == $branch
                      and .head_repository.full_name == $repo
                      and (.run_started_at // "") != ""))
         | map(select((.run_started_at | fromdateiso8601) as $t
                      | $t >= ($from | fromdateiso8601)
                        and $t <= (($to | fromdateiso8601) + $slack)))
         | if length == 0 then ""
           else (min_by((.run_started_at | fromdateiso8601) - ($to | fromdateiso8601)
                        | if . < 0 then -. else . end)
                 | "\(.html_url) (\(.head_sha[0:10]))") end' \
        2>/dev/null || echo "")"
}

# A proven supersession. As the probe, record it and stop: the cancel job acts
# on the file. As the barrier, wait while the cancel job cancels this run, and
# fail closed if that does not happen within CANCEL_WAIT.
on_superseded() {
  local gurl="$1" proof waited_from
  proof="Fast Gate run $gurl never started: it was evicted from its concurrency group when $EVICTOR entered it."
  if [ -n "$SUPERSEDED_FILE" ]; then
    printf '%s\n' "$proof" > "$SUPERSEDED_FILE"
    exit 1
  fi
  echo "::warning::$proof Nothing judged this commit, so the Cancel Superseded Run" \
       "job cancels this CI run, and this job waits for that instead of reporting" \
       "a failure. For a verdict on this commit, re-run that Fast Gate run, then this run."
  waited_from="$(now)"
  while [ $(( $(now) - waited_from )) -lt "$CANCEL_WAIT" ]; do
    nap 10
  done
  echo "::error::This run was not cancelled within ${CANCEL_WAIT}s of proving its" \
       "Fast Gate run superseded (see the Cancel Superseded Run job), so it fails" \
       "closed and the matrix stays unreleased."
  exit 1
}

started="$(now)"
seen=false

while :; do
  elapsed=$(( $(now) - started ))

  # A read that ERRORS is not a run that is absent. The listing call's stderr
  # is kept, so a rate limit or 5xx is named in the log, and a failed read
  # never spends the APPEAR budget: it only proves the barrier could not look.
  # The TOTAL budget still bounds the wait, so an API that stays down fails
  # closed -- with a message that says the code was never judged.
  read_err=""
  if ! runs="$(gh api --method GET \
    "repos/$REPO/actions/workflows/fast-gate.yml/runs" \
    -f "head_sha=$SHA" -f "event=$EVENT" -f "branch=$BRANCH" \
    -f per_page=100 2>"${TMPDIR:-/tmp}/await-fg-err.$$")"; then
    read_err="$(head -c 300 "${TMPDIR:-/tmp}/await-fg-err.$$" 2>/dev/null | tr '\n' ' ')"
    runs=""
  fi
  rm -f "${TMPDIR:-/tmp}/await-fg-err.$$"

  if [ -n "$read_err" ] || [ -z "$runs" ]; then
    echo "::warning::Fast Gate listing read failed after ${elapsed}s: ${read_err:-empty response}"
    if [ "$elapsed" -ge "$TOTAL_BUDGET" ]; then
      echo "::error::Fast Gate listing unreadable after ${elapsed}s (${read_err:-empty response})." \
           "The gates were never judged, so the matrix is not cleared to run;" \
           "this is not a verdict on the code -- re-run this job."
      exit 1
    fi
    nap 10
    continue
  fi

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
    # A push's gate run evicted before it started is superseded, not decided:
    # proven, this job waits to be cancelled (or, as the probe, records the
    # proof) and never returns; otherwise the case below fails closed on
    # `cancelled` like any other non-success.
    if [ "$conclusion" = "cancelled" ] && [ "$EVENT" = "push" ]; then
      prove_superseded "$latest"
      if [ -n "$EVICTOR" ]; then
        on_superseded "$url"
      fi
    fi
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
         "Failing closed rather than starting the matrix unverified." \
         "This is not a verdict on the code: re-run this job once that run completes."
    exit 1
  fi

  nap 10
done
