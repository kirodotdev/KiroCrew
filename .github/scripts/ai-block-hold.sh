#!/usr/bin/env bash
# The AI review hold: before ci.yml's heavy jobs start, read the verdict the two
# blocking line reviewers have ALREADY published for this head, and hold the
# heavy jobs when one of them is a BLOCK. Invoked from the `ai-block-hold` job
# in .github/workflows/ci.yml with:
#
#   GH_TOKEN REPO SHA MODE
#
# MODE is the repository variable CI_AI_BLOCK_HOLD: `shadow` reads and reports
# what it would do and always releases; `enforce` holds. The job does not start
# on any other value, and this script releases on any other value too.
#
# ## What counts as a published BLOCK
#
# A lane is blocking only when every check-run of that name on the head that
# was not cancelled or skipped is `completed` with conclusion `failure` AND
# carries the error annotation the lane's own gate step prints on a BLOCK for
# this exact head (BLOCK_PHRASE below, followed by the full head SHA). A
# cancelled or skipped run judged nothing (a head often carries one beside the
# run that did the review), so it is left out. The conclusion alone is not enough: a
# lane also ends `failure` when the review did not complete (no reviewed
# marker, a model or runner error, a spent turn budget), and that is not a
# verdict about the code. An accepted `/ai-review override` re-runs the lane to
# `success`, so the newest check-run stops matching and a re-run of this job
# releases the heavy jobs.
#
# ## It never waits and it fails open
#
# One read per lane, plus one annotations read per failed check-run: at most
# four GETs, no loop and no retry, because the installation token's REST
# budget is shared with pr-readiness.yml. A verdict that is not published yet
# (no check-run, queued, in progress) releases the heavy jobs exactly as
# before this job existed. So does every read that fails or returns something
# this script cannot parse: the hold exists to save runner time, so anything
# short of a BLOCK it positively read must not stop a test run.
#
# ## The exit codes
#
#   0  release: no published BLOCK, MODE is `shadow`, or a read failed
#   1  hold: MODE is `enforce` and a lane published BLOCK for SHA. `hold=true`
#      is written to $GITHUB_OUTPUT first; `lint-gate` skips on that output,
#      never on this job's result, so a job that failed for any other reason
#      (a runner fault, a timeout) still releases the heavy jobs.
#
# `gh` and `jq` are found on PATH, which is also how the test substitutes a
# stub `gh` (test_ai_block_hold.py).

set -uo pipefail

MODE="${MODE:-}"
REPO="${REPO:-}"
SHA="${SHA:-}"

# The check names the same-repository lanes publish (claude-review.yml and
# codex-review.yml `name:`), and the words both gate steps print on a BLOCK.
LANES=("Opus 5.5 Review" "GPT 6.1 Review")
BLOCK_PHRASE="review flagged a blocking issue on"

summary() { printf '%s\n' "$1" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"; }

release() {
  echo "$1"
  summary "### AI Review Hold"
  summary ""
  summary "$1"
  exit 0
}

case "$MODE" in
  shadow | enforce) ;;
  *) release "CI_AI_BLOCK_HOLD is '${MODE}', not 'shadow' or 'enforce': releasing the heavy jobs." ;;
esac
if [ -z "$REPO" ] || ! printf '%s' "$SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "::warning::AI Review Hold: no repository or full head SHA to read verdicts for. Releasing the heavy jobs."
  release "No repository or full head SHA to read verdicts for: releasing the heavy jobs."
fi

# Prints one of: blocking | passing | incomplete | pending | absent | unreadable
lane_state() {
  local lane="$1" page ids states id notes hit
  page="$(gh api -X GET "repos/$REPO/commits/$SHA/check-runs" \
    -f check_name="$lane" -f filter=latest -f per_page=100 2>/dev/null)" || { echo unreadable; return; }
  # Only the rows GitHub Actions wrote under exactly this name, without the
  # runs that were cancelled or skipped. One line per check-run:
  # "<id> <status> <conclusion>".
  states="$(printf '%s' "$page" | jq -r --arg lane "$lane" '
    .check_runs[]
    | select(.name == $lane and .app.slug == "github-actions")
    | select((.status == "completed" and (.conclusion == "cancelled" or .conclusion == "skipped")) | not)
    | "\(.id) \(.status) \(.conclusion // "none")"' 2>/dev/null)" || { echo unreadable; return; }
  if [ -z "$states" ]; then echo absent; return; fi
  if printf '%s\n' "$states" | grep -Evq ' completed [a-z_]+$'; then echo pending; return; fi
  if printf '%s\n' "$states" | grep -Evq ' completed failure$'; then echo passing; return; fi
  ids="$(printf '%s\n' "$states" | cut -d' ' -f1)"
  for id in $ids; do
    printf '%s' "$id" | grep -Eq '^[0-9]+$' || { echo unreadable; return; }
    notes="$(gh api -X GET "repos/$REPO/check-runs/$id/annotations" -f per_page=100 2>/dev/null)" \
      || { echo unreadable; return; }
    hit="$(printf '%s' "$notes" | jq -r --arg want "$BLOCK_PHRASE $SHA" '
      [.[] | select(.annotation_level == "failure" and ((.message // "") | contains($want)))] | length' \
      2>/dev/null)" || { echo unreadable; return; }
    printf '%s' "$hit" | grep -Eq '^[0-9]+$' || { echo unreadable; return; }
    # A failed lane with no BLOCK annotation did not reach a verdict.
    if [ "$hit" = "0" ]; then echo incomplete; return; fi
  done
  echo blocking
}

blocked=""
report=""
for lane in "${LANES[@]}"; do
  state="$(lane_state "$lane")"
  case "$state" in
    blocking) blocked="${blocked:+$blocked, }$lane" ;;
    unreadable) echo "::warning::AI Review Hold: could not read '$lane' for $SHA. Treating it as not blocking." ;;
    absent | pending | passing | incomplete) ;;
    *) state="unreadable" ;;
  esac
  echo "$lane: $state"
  report="${report}| \`$lane\` | $state |"$'\n'
done

table="| Lane | State for \`$SHA\` |"$'\n'"|---|---|"$'\n'"$report"

if [ -z "$blocked" ]; then
  summary "### AI Review Hold"
  summary ""
  summary "No published BLOCK for this head: releasing the heavy jobs."
  summary ""
  summary "$table"
  echo "No published BLOCK for $SHA: releasing the heavy jobs."
  exit 0
fi

way_out="Fix the finding and push. If the lane turns green for this head instead (its re-run passes, or a repository writer records \`/ai-review override\`), re-run this job with 'Re-run failed jobs' and the heavy jobs start."

if [ "$MODE" = "shadow" ]; then
  echo "::notice title=AI Review Hold (shadow)::$blocked published BLOCK for $SHA before the heavy jobs started. Shadow mode: the heavy jobs are released; with CI_AI_BLOCK_HOLD=enforce they would be held."
  summary "### AI Review Hold (shadow)"
  summary ""
  summary "$blocked published BLOCK for this head before the heavy jobs started. Shadow mode releases them; \`CI_AI_BLOCK_HOLD=enforce\` would hold them."
  summary ""
  summary "$table"
  exit 0
fi

echo "hold=true" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "::error title=Heavy jobs held::$blocked published BLOCK for $SHA, so the test shards, e2e and builds did not start. $way_out"
summary "### AI Review Hold: heavy jobs held"
summary ""
summary "$blocked published BLOCK for this head, so the test shards, e2e and builds did not start. This head has no test verdict."
summary ""
summary "$way_out"
summary ""
summary "$table"
exit 1
