#!/usr/bin/env bash
# Fetch the git objects Code Review's jobs read, and resolve their diff base.
#
# Run by the "Resolve diff base (merge-base)" step of every job in
# code-review.yml, right after `actions/checkout` with `fetch-depth: 2`: the
# merge commit and its two parents (the base tip and the PR head), each with
# its whole tree. A full-history checkout instead downloads every branch with
# every blob ever committed, about 3 GB, and on a slow runner that fetch alone
# outlasts the job's timeout.
#
# This script adds the two things the jobs read beyond those three commits:
#
#   1. The commit history of BASE_TIP and HEAD_SHA, with no trees or blobs
#      (`--filter=tree:0`). `git merge-base` and `git rev-list` read commits
#      only, so they see the same graph a full clone has.
#   2. The tree and blobs of the merge-base. The diffs below read them, and
#      git fetches each missing object on demand while they run.
#
# Every network read happens in this script, which fails the step when a fetch
# fails or any object of the four commits is still missing at the end. The
# later steps read the commit graph and those four commits' files only, so
# they never fetch. A fetch there that failed would read as an empty diff, and
# a rule grep would pass.
#
# Inputs, environment variables: BASE_TIP, HEAD_SHA and GITHUB_OUTPUT, which
# receives `sha=<merge-base>`.
set -euo pipefail

: "${BASE_TIP:?BASE_TIP is required}" "${HEAD_SHA:?HEAD_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

# The repository is public, so every fetch here is anonymous: this script runs
# from the pull request's own commit, and the job hands it no token. A private
# repository refuses the fetch, and the step fails. The empty credential.helper
# turns off any helper the runner configures. The empty
# http.https://github.com/.extraheader drops the header actions/checkout writes
# when it keeps its credential. With prompts off, a remote that asks for a
# credential fails the fetch at once. The on-demand fetches during the diffs
# inherit all three settings.
export GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0='' \
  GIT_CONFIG_KEY_1=http.https://github.com/.extraheader GIT_CONFIG_VALUE_1=''

# Three attempts, as actions/checkout gives its own fetch.
retry() {
  local attempt
  for attempt in 1 2 3; do
    "$@" && return 0
    [ "$attempt" -lt 3 ] && sleep $((attempt * 5))
  done
  return 1
}

# An attempt can complete the history and still fail, and git refuses
# --unshallow on a complete history, so each attempt asks for it only while the
# history is still shallow.
fetch_history() {
  if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
    set -- --unshallow
  else
    set --
  fi
  git fetch --no-tags --no-recurse-submodules --quiet --filter=tree:0 "$@" \
    origin "$BASE_TIP" "$HEAD_SHA"
}

if ! retry fetch_history; then
  echo "::error::Could not fetch the history of $BASE_TIP and $HEAD_SHA"
  exit 1
fi
# A history still shallow here would give a wrong merge-base and commit count.
if [ "$(git rev-parse --is-shallow-repository)" != false ]; then
  echo "::error::The history of $BASE_TIP and $HEAD_SHA is still shallow after the fetch"
  exit 1
fi

# base.sha is the base branch tip when the PR event fired, not the branch-off
# point, so diffing it directly pulls in sibling commits.
if ! merge_base="$(git merge-base "$BASE_TIP" "$HEAD_SHA")"; then
  echo "::error::Could not resolve the merge-base of $BASE_TIP and $HEAD_SHA"
  exit 1
fi

# HEAD is the checked-out merge commit: the working tree, and what Semgrep's
# baseline compares the merge-base with. A diff reads every file that differs
# between its two commits. So after these three, each file of the merge-base,
# the base tip and the head is local: it either matches the merge commit's
# copy or one of the diffs read it.
for tip in HEAD "$HEAD_SHA" "$BASE_TIP"; do
  if ! retry git diff --binary "$merge_base" "$tip" > /dev/null; then
    echo "::error::Could not fetch the files that differ between $merge_base and $tip"
    exit 1
  fi
done

# --missing=print reports an absent object instead of fetching it.
if ! objects="$(git rev-list --objects --no-walk --missing=print \
  "$merge_base" "$BASE_TIP" "$HEAD_SHA" HEAD)"; then
  echo "::error::Could not list the objects of $merge_base, $BASE_TIP, $HEAD_SHA and the merge commit"
  exit 1
fi
missing="$(grep -c '^?' <<<"$objects" || true)"
if [ "$missing" != 0 ]; then
  echo "::error::$missing objects of $merge_base, $BASE_TIP, $HEAD_SHA and the merge commit are not local"
  exit 1
fi

echo "Diff base: $merge_base"
echo "sha=$merge_base" >> "$GITHUB_OUTPUT"
