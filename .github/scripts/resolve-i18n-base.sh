#!/usr/bin/env bash
# Resolve the commit the i18n diff-scoped gates measure against, and make it
# available locally. Prints the resolved value on stdout (empty when there is
# genuinely nothing to diff); diagnostics go to stderr.
#
#   I18N_BASE_REF="$(.github/scripts/resolve-i18n-base.sh "$I18N_BASE_REF")" || exit 1
#
# ## Why `.github/scripts/` and not the repo-root `scripts/`
#
# Root `scripts/` holds product and dev tooling that a contributor runs by hand
# (`clean.sh`, `check_npm_audit.py`). This one is workflow-only glue:
# it reads `github.event.*` values that exist nowhere else and is invoked through
# `$GITHUB_WORKSPACE` by three ci.yml steps. It lives next to the workflows that are
# its only caller. Note the repo has no shellcheck step, so a `.sh` is unlinted under
# either path — the placement costs nothing in enforcement.
#
# ## Why a script and not three copies of the same `if`
#
# Three steps in ci.yml need this (`frontend-ratchets`, `frontend-tests`, and the render
# gate in `e2e`). It used to be an inline three-liner in each, which was tolerable
# while the only case was "a PR passes origin/<base>, a push passes nothing". It is
# not tolerable now that a push passes a bare SHA, because the ref-vs-SHA handling
# and the zero-SHA guard would be triplicated shell that only one of the three would
# get fixed in.
#
# `brand-lint` is a fourth caller and is not an i18n gate. Nothing here is
# i18n-specific — it resolves "the commit this diff is measured against" for any
# diff-scoped gate — so the name is historical, not a scope.
#
# ## Two callers, two shapes
#
#   - **pull_request** — `origin/<base branch>`. A ref name, so it also needs a local
#     `refs/remotes/...` entry for the gates that resolve it by name.
#   - **push to main** — `github.event.before`, the commit this push replaced. A bare
#     SHA, which resolves on its own once fetched; creating `refs/remotes/<sha>` for
#     it would be a nonsense ref.
#
# Giving a push a base is the point of the change that introduced this file: it is
# what lets every i18n gate enforce a DIFF and report totals, instead of failing on
# a stored total that no single diff can be held responsible for. It is a sampled
# backstop rather than a per-commit one — ci.yml's concurrency group evicts a pending
# main run when a newer one queues, so some commits reach main without their own run,
# and their delta then falls between two `before` boundaries. The PR gate remains the
# primary check; this catches the interaction between two branches that passed
# separately, which is the class no PR-scoped diff can see.
set -euo pipefail

REF="${1:-}"
LABEL="${2:-the diff-scoped i18n gates}"
ZERO_SHA=0000000000000000000000000000000000000000

if [ -z "$REF" ]; then
  echo "::notice::no base commit supplied; $LABEL will report without enforcing" >&2
  exit 0
fi

# A branch-creation push reports an all-zeros predecessor. There is no earlier commit
# to compare with, so this is "nothing to diff", not a broken configuration.
if [ "$REF" = "$ZERO_SHA" ]; then
  echo "::notice::this push has no predecessor commit; $LABEL will report without enforcing" >&2
  exit 0
fi

# A pull_request run checks out GitHub's test merge commit (the PR head merged into
# the base branch), but the base it is handed is `pull_request.base.sha`, which a
# re-run keeps from the original event while the merge commit moves to the current
# base. A depth-1 checkout has no merge-base to correct that, so the gates would diff
# every base-branch change in between and charge it to the PR. The merge commit's
# first parent IS the base it was built on, so measure from that. It applies
# only when HEAD's second parent is the PR head: a checkout of the PR head itself,
# even one whose tip merged the base in, keeps the base it was given.
if [ "${GITHUB_EVENT_NAME:-}" = pull_request ] && [ -n "${GITHUB_EVENT_PATH:-}" ]; then
  PR_HEAD="$(python3 -c 'import json, sys; print(((json.load(open(sys.argv[1], encoding="utf-8")).get("pull_request") or {}).get("head") or {}).get("sha") or "")' "$GITHUB_EVENT_PATH" 2>/dev/null || true)"
  # `cat-file` reads the commit object itself, so it sees both parents even where a
  # shallow checkout grafts them away from `HEAD^1`.
  PARENTS=($(git cat-file -p HEAD | sed -n 's/^parent //p'))
  if [ -n "$PR_HEAD" ] && [ "${#PARENTS[@]}" -eq 2 ] && [ "${PARENTS[1]}" = "$PR_HEAD" ] \
    && [ "${PARENTS[0]}" != "$(git rev-parse --verify --quiet "${REF}^{commit}" 2>/dev/null || true)" ]; then
    echo "::notice::measuring $LABEL from the merge commit's first parent ${PARENTS[0]} instead of $REF" >&2
    REF="${PARENTS[0]}"
  fi
fi

# A shallow checkout lacks the base commit. Fetching it is NOT optional and
# a failure here must fail the step: ci.yml records having watched a sibling gate skip
# itself green on an unreadable base ref and silently stop checking for a whole PR.
# Depth 1 only into a checkout that is already shallow: in a full clone (a local run,
# or a `fetch-depth: 0` job) it would make the clone and all its worktrees shallow.
DEPTH=()
if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
  DEPTH=(--depth=1)
fi
if ! git fetch ${DEPTH[@]+"${DEPTH[@]}"} origin "${REF#origin/}" >&2; then
  echo "::error::could not fetch base commit ${REF#origin/}; $LABEL cannot run" >&2
  exit 1
fi

case "$REF" in
  origin/*)
    if ! git update-ref "refs/remotes/$REF" FETCH_HEAD >&2; then
      echo "::error::could not update $REF; $LABEL cannot run" >&2
      exit 1
    fi
    ;;
esac

printf '%s\n' "$REF"
