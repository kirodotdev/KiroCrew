# shellcheck shell=bash
# Merge-base helpers for the same-repo AI review lanes' shallow checkout.
#
# Sourced, not executed, by the "Fetch the merge-base commit(s) (shallow
# history)" step of claude-review.yml, codex-review.yml, design-review.yml,
# ux-review.yml and first-principles-review.yml. Those lanes check out the
# merge ref at depth 2, so a three-dot `X...Y` range has no history to find
# its merge-base in. `merge_base X Y` names it -- from a visible parent of Y
# when X is one, otherwise from GitHub's compare API -- and `need SHA` fetches
# a commit at depth 1. The lanes then diff the two-dot `<merge-base>..Y`,
# which compares two trees, needs no history, and is the diff `X...Y` gives
# on a full clone.
#
# The fork lanes keep their own copy inline: they source scripts from the
# PR's base tree, and a base older than this file has no copy to source.
#
# Inputs, environment variables: GH_TOKEN (fetch auth and the compare read)
# and REPO (owner/name). Both functions fail closed: `need` exits when the
# commit is still missing, `merge_base` exits when no 40-hex SHA comes back.

auth_b64="$(printf 'x-access-token:%s' "$GH_TOKEN" | base64 | tr -d '\n')"
echo "::add-mask::$auth_b64"

need() {
  git cat-file -e "$1^{commit}" 2>/dev/null && return 0
  git -c http.extraheader="AUTHORIZATION: basic $auth_b64" \
    fetch --no-tags --quiet --depth=1 origin "$1" || true
  git cat-file -e "$1^{commit}" 2>/dev/null \
    || { echo "::error::could not fetch commit $1; failing closed."; exit 1; }
}

merge_base() {
  # A visible parent of the tip is its merge-base with it: no API call.
  if grep -qxF "$1" <<<"$(git rev-parse "$2^@" 2>/dev/null || true)"; then
    printf '%s\n' "$1"
    return 0
  fi
  local sha="" attempt
  for attempt in 1 2 3; do
    sha="$(gh api "repos/$REPO/compare/$1...$2?per_page=1" --jq '.merge_base_commit.sha')" && break
    sleep "$attempt"
  done
  if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
    # stderr: callers read stdout through `$(merge_base ...)`, which would
    # swallow the annotation.
    echo "::error::could not read the merge-base of $1 and $2; failing closed." >&2
    exit 1
  fi
  printf '%s\n' "$sha"
}
