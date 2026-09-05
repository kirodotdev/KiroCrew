#!/usr/bin/env bash
# No operator-facing message may hand out a copyable placeholder assignment.
#
# This exists because the same failure happened three times on one variable: an
# example command containing `export KIRO_API_KEY=...` was pasted verbatim, the
# literal placeholder became the stored key, and it surfaced three steps later as
# "your session has expired" -- pointing at sign-in rather than at the value.
# The shape guard catches the bad value; this catches the thing that PRODUCES it.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# The app root holds skills/; the crew root holds the driver.
APP_ROOT="$(cd "$ROOT/.." && pwd)"
DRIVER="$ROOT/scripts/smc-deploy.sh"
fails=0
checked=0

# A placeholder right-hand side: ... or <something>.
PLACEHOLDER_RHS="('|\")?(\.\.\.|…|<[^>]*>)"

# Two shapes are offences, and one lookalike is not.
#
#   export K=<placeholder>      an offence anywhere, comment included: it is a
#                               complete copyable statement, and a commented
#                               example is copied as readily as a live one.
#   K=<placeholder>             an offence only OUTSIDE a comment, where it is
#                               real code rather than prose.
#   # SMC_X_JSON=<path>         NOT an offence: prose describing the FORMAT of a
#                               marker line the tools emit. Nobody pastes it as a
#                               command, and flagging it would train the reader to
#                               ignore this guard.
EXPORT_PLACEHOLDER="export +[A-Z][A-Z0-9_]*=${PLACEHOLDER_RHS}"
BARE_PLACEHOLDER="^[^#]*[^A-Za-z0-9_]?[A-Z][A-Z0-9_]*=${PLACEHOLDER_RHS}"

check_file() { # LABEL FILE
  local label="$1" file="$2"
  checked=$((checked + 1))
  local hits
  hits="$(grep -nE "$EXPORT_PLACEHOLDER" "$file" 2>/dev/null || true)"
  hits="$hits$(grep -nE "$BARE_PLACEHOLDER" "$file" 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    printf 'FAIL  %s hands out a copyable placeholder assignment\n' "$label" >&2
    printf '%s\n' "$hits" | sed 's/^/        /' >&2
    fails=$((fails + 1))
  else
    printf 'ok    %s\n' "$label"
  fi
}

check_file "crew/scripts/smc-deploy.sh" "$DRIVER"
check_file "skills/SKILL.md" "$APP_ROOT/skills/SKILL.md"
# Any OTHER operator-facing script beside the driver: today build_image.sh and
# build_crew_image.sh, which the driver invokes for the base and crew images.
# A loop rather than two named checks, so a script added later is covered
# without anyone having to remember this guard exists.
for f in "$ROOT"/scripts/*.sh; do
  [ -f "$f" ] || continue
  [ "$f" = "$DRIVER" ] && continue
  check_file "crew/scripts/$(basename "$f")" "$f"
done

# The reuse path must exist and be documented, because it is what makes supplying
# the key unnecessary on a resume. Without it the operator is forced to re-paste a
# value they already stored, which is what created the placeholder habit.
checked=$((checked + 1))
if grep -q 'reused from the existing secret' "$DRIVER"; then
  printf 'ok    step 5 reuses an existing secret when the variable is unset\n'
else
  printf 'FAIL  step 5 has no reuse path, so every resume demands the key again\n' >&2
  fails=$((fails + 1))
fi

printf '\n%d checked, %d failed\n' "$checked" "$fails"
[ "$fails" -eq 0 ]
