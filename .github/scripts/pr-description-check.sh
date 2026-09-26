#!/usr/bin/env bash
# Checks a PR description against the sections the PR template requires.
#
# One script, two callers, so fork and same-repo PRs follow the same rules:
# `fork-pr-description.yml` publishes the verdict as a check-run a fork author
# sees before any workflow is approved, and `code-review.yml`'s PR Hygiene job
# fails on it, which blocks PR Readiness for every PR.
#
# Inputs (environment): PR_BODY (untrusted, read as data only), PR_DRAFT
# ("true"/"false"), GITHUB_REPOSITORY, GITHUB_OUTPUT. Writes `conclusion`
# (success | neutral | failure), `title` and a multiline `summary` to
# $GITHUB_OUTPUT. The PR body is never interpolated into code: it is written to
# a file and read by awk.
#
# The rules:
#   - every heading in REQUIRED_SECTIONS is present, as a real ATX heading
#     outside fenced code, matched as a case-insensitive prefix;
#   - `## Problem / Motivation` carries a `**Goal:** <text>` line: the PR's
#     frozen, one-sentence goal.
#
# LIMIT, stated plainly: this verifies that the parts are present, not that
# they are well written. Judging the prose is the AI reviewers' job.
set -uo pipefail

# The maintainer auto-approval cron keeps its own copy of this list outside
# the repository; the two must match or a fork's runs are approved while this
# check is red. Matched as a case-insensitive prefix, so a heading may carry a suffix (the template's own "## What changed
# (motivation -> approach -> change)" matches "## What changed").
REQUIRED_SECTIONS='## Problem / Motivation
## Why it matters
## Not a goal
## What changed
## Tests'

# Strip CR at the input. GitHub returns a web-authored PR body with CRLF line
# endings, so a closing fence arrives as "```\r" -- and a fence closer may
# carry nothing but whitespace after the marker, so the stray CR would stop it
# closing, swallow every heading below the code block, and mark a
# template-compliant PR non-compliant. Fixing it here rather than in the
# scanner keeps the CommonMark rules below reading like the spec, and covers
# the heading text too.
body_file="$(mktemp)"
printf '%s' "${PR_BODY:-}" | tr -d '\r' > "$body_file"

# Collect the body's real ATX heading lines, lowercased, EXCLUDING anything
# inside a fenced code block. A line `goal-line` is printed when a filled
# `**Goal:**` line sits under `## Problem / Motivation`; it can never collide
# with a heading, which always starts with `#`.
#
# Why a scanner and not a grep. Matching the raw body passes a PR that merely
# mentions "## Tests" mid-sentence, or that pastes the template into a fence
# to ask a question about it -- both report full compliance for a description
# that has no sections at all. And matching whole lines is equally wrong in
# the other direction: the template's own heading is "## What changed
# (motivation -> approach -> change)", so the match must stay a PREFIX of a
# heading line.
#
# Why CommonMark's real rules rather than a toggle. A naive "any fence line
# flips a boolean" scanner is wrong for nested examples: a ````-fenced block
# that CONTAINS ``` lines flips the boolean off mid-block and starts accepting
# the enclosed text as headings. The invariant that makes that whole family
# unreachable is the spec's own: a fence closes only on the SAME character, at
# a length >= the opener, with nothing but whitespace after it; and an ATX
# heading takes at most 3 leading spaces (4+ is indented code). The Goal line
# follows the same indent rule, which keeps the template's own guidance (inside
# an indented HTML comment) from counting as a filled Goal. Implemented with
# substr/while rather than regex interval expressions, because `awk` is mawk
# on the Ubuntu runners and its interval support is not something to bet a
# gate on.
headings="$(awk '
  {
    s = $0
    n = 0
    while (n < 3 && substr(s, 1, 1) == " ") { s = substr(s, 2); n++ }

    mch = ""
    if (substr(s, 1, 3) == "```") mch = "`"
    else if (substr(s, 1, 3) == "~~~") mch = "~"
    mlen = 0
    if (mch != "") {
      while (substr(s, mlen + 1, 1) == mch) mlen++
      rest = substr(s, mlen + 1)
    }

    if (open == "") {
      # An opening fence may carry an info string; a closing one may not.
      if (mch != "") { open = mch; olen = mlen; next }
    } else {
      if (mch == open && mlen >= olen && rest ~ /^[ \t]*$/) {
        open = ""; olen = 0
      }
      next
    }

    hn = 0
    while (hn < 7 && substr(s, hn + 1, 1) == "#") hn++
    if (hn >= 1 && hn <= 6) {
      c = substr(s, hn + 1, 1)
      if (c == " " || c == "\t") {
        h = tolower(s)
        print h
        # A subheading stays inside its section; a peer heading ends it.
        if (hn <= 2) in_problem = (index(h, "## problem / motivation") == 1)
      }
      next
    }

    if (in_problem && index(tolower(s), "**goal:**") == 1) {
      if (substr(s, 10) ~ /[^ \t]/) print "goal-line"
    }
  }
' "$body_file")"
rm -f "$body_file"

missing=()
while IFS= read -r section; do
  [ -n "$section" ] || continue
  # Prefix-compare with `case`, not a regex: the needle is data, and this way
  # no section name ever needs regex-escaping.
  needle="$(printf '%s' "$section" | tr '[:upper:]' '[:lower:]')"
  found=0
  while IFS= read -r heading; do
    case "$heading" in
      "$needle"*) found=1; break ;;
    esac
  done <<< "$headings"
  if [ "$found" -eq 0 ]; then
    missing+=("$section")
  fi
done <<< "$REQUIRED_SECTIONS"

# The Goal line is reported only when its section exists: a missing
# `## Problem / Motivation` is already named above, and adding it from the
# template brings the Goal line's scaffold with it.
goal_missing=false
case "${missing[*]:-}" in
  *"## Problem / Motivation"*) ;;
  *) grep -qx 'goal-line' <<< "$headings" || goal_missing=true ;;
esac

if [ "${#missing[@]}" -gt 0 ]; then
  conclusion="failure"
  if [ "${#missing[@]}" -eq 1 ]; then
    title="1 required description section is missing"
  else
    title="${#missing[@]} required description sections are missing"
  fi
elif [ "$goal_missing" = "true" ]; then
  conclusion="failure"
  title="The Goal line is missing from Problem / Motivation"
elif [ "${PR_DRAFT:-false}" = "true" ]; then
  # Not a defect -- the author parked it deliberately. `neutral` renders grey
  # rather than red while still carrying the reason.
  conclusion="neutral"
  title="Draft — workflow runs are not auto-approved yet"
else
  conclusion="success"
  title="Description follows the PR template"
fi

{
  echo "conclusion=$conclusion"
  echo "title=$title"
} >> "$GITHUB_OUTPUT"

template_url="https://github.com/${GITHUB_REPOSITORY}/blob/main/.github/PULL_REQUEST_TEMPLATE.md"

# Multiline output via a delimiter that cannot occur in the summary: nothing
# from the body is echoed into it, only section names from the list above.
{
  echo "summary<<__FORK_PR_DESCRIPTION__"
  if [ "${#missing[@]}" -gt 0 ] || [ "$goal_missing" = "true" ]; then
    echo "Your PR description is missing these parts of the"
    echo "[PR template]($template_url):"
    echo
    for section in ${missing[@]+"${missing[@]}"}; do
      echo "- \`$section\`"
    done
    if [ "$goal_missing" = "true" ]; then
      echo "- a \`**Goal:** <one sentence>\` line under \`## Problem / Motivation\`"
    fi
    echo
    echo "Add them and save the description — no push needed. This check"
    echo "re-runs on edit, and automatic approval of fork workflow runs"
    echo "resumes on the next cycle."
  fi
  if [ "${PR_DRAFT:-false}" = "true" ]; then
    echo
    echo "This PR is also a **draft**. Workflow runs are not"
    echo "auto-approved until it is marked ready for review."
  fi
  if [ "$conclusion" != "failure" ] && [ "${PR_DRAFT:-false}" != "true" ]; then
    echo "All required template sections are present and the PR is"
    echo "ready for review."
  fi
  echo "__FORK_PR_DESCRIPTION__"
} >> "$GITHUB_OUTPUT"
