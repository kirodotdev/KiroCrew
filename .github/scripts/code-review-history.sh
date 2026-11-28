#!/usr/bin/env bash
# Fetch the git objects Code Review's jobs read, and resolve their diff base.
#
# Run by the "Resolve diff base (merge-base)" step of every job in
# code-review.yml and pr-hygiene.yml, right after `actions/checkout` with
# `fetch-depth: 2`: the merge commit and its two parents (the base tip and the
# PR head), each with its whole tree. A full-history checkout instead downloads
# every branch with every blob ever committed, about 3 GB, and on a slow runner
# that fetch alone outlasts the job's timeout.
#
# This script adds the two things the jobs read beyond those three commits:
#
#   1. The commit history of BASE_TIP, HEAD_SHA and the merge commit's first
#      parent, with their trees but no blobs (`--filter=blob:none`).
#      `git merge-base` and `git rev-list` read commits only, so they see the
#      same graph a full clone has. The filter is not `tree:0`: on git 2.56,
#      deepening a shallow clone with `tree:0` aborts in index-pack ("BUG:
#      builtin/pack-objects.c: should_include_obj should only be called on
#      existing objects").
#   2. The tree and blobs of the merge-base. The diffs below read them, and
#      git fetches each missing object on demand while they run.
#
# Every network read happens in this script, which fails the step when a fetch
# fails or any object of the four commits is still missing at the end. The
# later steps read the commit graph, the trees of that history (the wheel-copy
# drift guard compares two of them) and those four commits' files only, so
# they never fetch. A fetch there that failed would read as an empty diff, and
# a rule grep would pass.
#
# Inputs, environment variables: BASE_TIP, HEAD_SHA and GITHUB_OUTPUT, which
# receives `sha=<merge-base>`.
set -euo pipefail

: "${BASE_TIP:?BASE_TIP is required}" "${HEAD_SHA:?HEAD_SHA is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

# base.sha is the base branch tip when the pull request was opened or last
# retargeted, not when this event fired: GitHub does not refresh it on
# `synchronize`. Once the branch is rebased past it, the merge-base with it IS
# that stale tip, and the diff from there charges every base commit in between
# to the pull request. The checkout is GitHub's merge commit, whose first parent
# is the base tip it was built on, so the merge-base is taken against that
# parent, as resolve-i18n-base.sh does for the diff-scoped gates. Only when the
# second parent is this PR's head: a checkout of the head itself, or a merge ref
# GitHub has since re-minted for a newer head (a re-run of a superseded event),
# keeps base.sha. `cat-file` reads the commit object, so it sees both parents
# even where the shallow checkout grafted them away.
base_now="$BASE_TIP"
read -r -a parents <<<"$(git cat-file -p HEAD | sed -n 's/^parent //p' | tr '\n' ' ')"
if [ "${#parents[@]}" -eq 2 ] && [ "${parents[1]}" = "$HEAD_SHA" ] \
  && [ "${parents[0]}" != "$BASE_TIP" ]; then
  echo "::notice::Resolving the diff base from the merge commit's first parent ${parents[0]}, not base.sha $BASE_TIP"
  base_now="${parents[0]}"
fi
history="$BASE_TIP and $HEAD_SHA"
if [ "$base_now" != "$BASE_TIP" ]; then
  history="$BASE_TIP, $HEAD_SHA and $base_now"
fi

# The repository is public, so every fetch here is anonymous: this script runs
# from the pull request's own commit, and the job hands it no token. A private
# repository refuses the fetch, and the step fails with an error that says the
# repository must be public or readable without credentials. The empty
# credential.helper turns off any helper the runner configures. The empty
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
  git fetch --no-tags --no-recurse-submodules --quiet --filter=blob:none "$@" \
    origin "$BASE_TIP" "$HEAD_SHA" "$base_now"
}

if ! retry fetch_history; then
  echo "::error::Could not fetch the history of $history. The history is" \
    "fetched without credentials, so the repository must be public or readable without them."
  exit 1
fi
# A history still shallow here would give a wrong merge-base.
if [ "$(git rev-parse --is-shallow-repository)" != false ]; then
  echo "::error::The history of $history is still shallow after the fetch"
  exit 1
fi

# The base tip is not the branch-off point, so diffing it directly pulls in
# sibling commits.
if ! merge_base="$(git merge-base "$base_now" "$HEAD_SHA")"; then
  echo "::error::Could not resolve the merge-base of $base_now and $HEAD_SHA"
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
