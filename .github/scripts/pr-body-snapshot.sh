#!/usr/bin/env bash
# Read a PR's title and description ONCE per job, and hand every consumer in
# that job the same bytes.
#
# Sourced (not executed) by pr-attachment-evidence.sh and
# pr-description-capture.sh, which are the two readers of that mutable text.
#
# WHY ONE READ AND NOT TWO. Those two scripts each used to call the API
# themselves, in adjacent steps of the same job -- design-review.yml collects
# evidence then captures prose, ux-review.yml likewise, and their fork
# counterparts the same. A description edited between the two reads pairs the
# OLD attachments with the NEW prose, and the digest the lane publishes is then
# taken over that pair: a composite revision that never existed. A reader
# recomputing it off the live description gets a match on bytes no single
# revision ever held, which is a false confirmation of exactly the guarantee
# the stamp exists to give. Nothing re-triggers the lane on a description edit
# either (the lanes fire on `opened, synchronize, reopened`), so no later run
# corrects it.
#
# The window was ordinary, not adversarial: pushing a commit starts the run,
# and pasting a screenshot or rewording the body in the next minute lands
# inside it.
#
# THE SNAPSHOT IS KEYED TO THE JOB, not to a variable a caller has to remember
# to pass. $RUNNER_TEMP is per-job by definition, so deriving the snapshot's
# home from it makes "one read per job" the default: a lane added later that
# sources either reader joins the same snapshot without wiring, and there is no
# env var whose omission silently restores the two-read defect.
#
# Inputs, all environment variables:
#   REPO, PR, GH_TOKEN   which PR to read, via `gh api`
#   PR_SNAPSHOT_DIR      OPTIONAL. Where the snapshot lives. Defaults to
#                        $RUNNER_TEMP/pr-body-snapshot inside a job; with
#                        neither set, a fresh scratch dir, so an off-runner
#                        by-hand run reads once and caches nothing.
#
# Outputs, set in the caller's shell:
#   KC_PR_SNAPSHOT_OK     1 when title and body are on disk, empty on failure.
#                         The CALLER decides what a failure means and prints its
#                         own error: one script fails the evidence collection,
#                         the other fails the capture, and those two messages
#                         say different true things.
#   KC_PR_TITLE_FILE      file holding the title
#   KC_PR_BODY_FILE       file holding the description, "" when the PR has none
#   KC_PR_SNAPSHOT_READS  how many API reads this job has spent, so a test can
#                         prove the second consumer spent none
#
# Deliberately sets no shell options: it is sourced into two callers that run
# under different ones (`set -euo pipefail` and `set -uo pipefail`), and
# changing the caller's mode from here would alter how ITS later failures
# behave.

_kc_snap_dir="${PR_SNAPSHOT_DIR:-}"
if [ -z "$_kc_snap_dir" ]; then
  if [ -n "${RUNNER_TEMP:-}" ]; then
    _kc_snap_dir="$RUNNER_TEMP/pr-body-snapshot"
  else
    _kc_snap_dir="$(mktemp -d)"
  fi
fi
mkdir -p "$_kc_snap_dir" 2>/dev/null || :
KC_PR_TITLE_FILE="$_kc_snap_dir/title"
KC_PR_BODY_FILE="$_kc_snap_dir/body"
_kc_snap_key="$_kc_snap_dir/key"
KC_PR_SNAPSHOT_OK=""
KC_PR_SNAPSHOT_READS="${KC_PR_SNAPSHOT_READS:-0}"

# Reuse only a snapshot of THIS pull request. A job serves one PR, so this
# cannot differ in practice -- it is written down because a snapshot that
# silently answered for another PR would be the same corruption this file
# exists to remove, one level up, and the check costs three lines.
_kc_snap_want="$REPO#$PR"
if [ -r "$_kc_snap_key" ] && [ -r "$KC_PR_TITLE_FILE" ] && [ -r "$KC_PR_BODY_FILE" ] \
  && [ "$(cat "$_kc_snap_key")" = "$_kc_snap_want" ]; then
  KC_PR_SNAPSHOT_OK=1
  echo "Reusing this job's PR title/description snapshot; no second API read."
else
  # `jq` reads the two fields out of ONE response. Extracting them from a
  # single fetched object is the established shape in this repository
  # (ai-review-human-override.yml reads head.sha and head.repo.full_name off one
  # `gh api`), and it is what makes both fields the same revision by
  # construction rather than by timing.
  if ! command -v jq >/dev/null 2>&1; then
    echo "::error::jq is not available, so this PR's title and description cannot be read as one revision. Failing closed rather than falling back to two independent reads, which is the defect this snapshot removes."
  else
    # One transient API failure (a 5xx, a rate limit) must not end the lane, so
    # the read gets three attempts and then fails closed. Each attempt is a
    # WHOLE fetch: a retry re-reads both fields together, so a description
    # edited between attempt 1 and attempt 2 still yields one consistent pair.
    _kc_snap_json=""
    for _kc_snap_try in 1 2 3; do
      KC_PR_SNAPSHOT_READS=$((KC_PR_SNAPSHOT_READS + 1))
      if _kc_snap_json="$(gh api "repos/$REPO/pulls/$PR")"; then
        KC_PR_SNAPSHOT_OK=1
        break
      fi
      echo "Reading the PR title and description failed on attempt $_kc_snap_try."
      if [ "$_kc_snap_try" -lt 3 ]; then
        sleep "$_kc_snap_try"
      fi
    done
    if [ -n "$KC_PR_SNAPSHOT_OK" ]; then
      # Write both files, then the key -- the key is what marks the snapshot
      # usable, so a crash midway leaves it absent and the next consumer reads
      # afresh rather than pairing one new field with one missing one.
      #
      # `jq -r` ends each file with a newline, which the consumers drop with
      # `$(cat ...)` exactly as command substitution dropped the trailing
      # newline of the old `--jq` output. The bytes the consumers compose are
      # therefore unchanged, so a digest published before this change still
      # recomputes to the same value.
      if printf '%s' "$_kc_snap_json" | jq -r '.title' > "$KC_PR_TITLE_FILE" \
        && printf '%s' "$_kc_snap_json" | jq -r '.body // ""' > "$KC_PR_BODY_FILE"; then
        printf '%s\n' "$_kc_snap_want" > "$_kc_snap_key"
        echo "Read this PR's title and description once ($(wc -c < "$KC_PR_BODY_FILE") body bytes); every consumer in this job reads that snapshot."
      else
        KC_PR_SNAPSHOT_OK=""
        rm -f "$_kc_snap_key"
        echo "::error::Could not split this PR's title and description out of the API response, so the two consumers would have to read it again separately. Failing closed."
      fi
    fi
    unset _kc_snap_json _kc_snap_try
  fi
fi
unset _kc_snap_dir _kc_snap_key _kc_snap_want
