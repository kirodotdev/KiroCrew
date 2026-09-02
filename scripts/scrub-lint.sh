#!/usr/bin/env bash
# scrub-lint.sh — CI gate that fails on Amazon-internal markers in the public tree.
# Run from the repo root: ./scripts/scrub-lint.sh
# Self-test mode:          ./scripts/scrub-lint.sh --test
# Exit 0 = clean, exit 1 = internal content detected.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$REPO_ROOT"

ALLOWLIST="scripts/scrub-allowlist.txt"
FAILURES=0

# --no-history skips the git-history scan (check 4). The working tree can be
# clean long before history is (history rewrite is a separate, sign-off-gated
# task), so CI runs the working-tree + credential + alias checks as a blocking
# gate with --no-history, while a full local run still audits history.
SKIP_HISTORY=0
for arg in "$@"; do
  [[ "$arg" == "--no-history" ]] && SKIP_HISTORY=1
done

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# Self-test mode: plant a marker, assert failure, remove it, assert pass
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--test" ]]; then
  dim "Running self-test..."
  MARKER_FILE="src/__scrub_test_marker.py"
  TEST_MARKER_FILE="test/__scrub_test_marker.py"
  ALLOWED_MARKER_FILE="test/__scrub_test_marker_allowed.py"

  # The probe paths are fixed (the allowlist anchors to one of them), so refuse
  # to run if any already exists: planting would truncate a file this process
  # does not own — leftover residue from an aborted run, or a concurrent
  # self-test's live probe. -L catches what -e misses: a DANGLING symlink at a
  # probe path would be followed by the redirection, creating its target
  # outside the checkout while cleanup removed only the link.
  for f in "$MARKER_FILE" "$TEST_MARKER_FILE" "$ALLOWED_MARKER_FILE"; do
    if [[ -e "$f" || -L "$f" ]]; then
      red "SELF-TEST ABORT: $f already exists — refusing to overwrite it."
      red "  Remove it (or wait for a concurrent self-test to finish) and re-run."
      exit 1
    fi
  done
  # Remove the probes on EVERY exit, interrupts included: residue under test/
  # is collected by pytest, and residue at the allowlisted path is the one file
  # the gate is blind to — the only residue that could be committed CI-green.
  trap 'rm -f "$MARKER_FILE" "$TEST_MARKER_FILE" "$ALLOWED_MARKER_FILE"' EXIT

  # Plant markers that should each trigger the scan. One probe per pattern
  # FAMILY, so a typo that silently breaks a family is caught here rather than
  # discovered when an internal marker ships green.
  probe_fail=0
  PROBES=(
    'internal-domain|# test marker: code.amazon.com/packages/FakePackage'
    'aim-invocation|# test marker: aim mcp install some-server'
    'aim-dep-contract|TYPE = "aim.mcp"'
    'aim-home-tree|P = "~/.aim/skills"'
    'arcc-tool|# test marker: search_arcc(query="x")'
    'arcc-server|# test marker: arcc-governance mcp server'
  )
  for probe in "${PROBES[@]}"; do
    probe_label="${probe%%|*}"
    probe_line="${probe#*|}"
    printf '%s\n' "$probe_line" > "$MARKER_FILE"
    # --no-history: the probes only exercise the working-tree scan, and the
    # history pass is slow enough that running it per-probe dominates runtime.
    # </dev/null so the child cannot consume this shell's stdin.
    if "$SELF" --no-history >/dev/null 2>&1 </dev/null; then
      red "SELF-TEST FAIL: planted $probe_label marker was not detected"
      probe_fail=1
    else
      green "  ✓ Planted $probe_label marker correctly detected"
    fi
    rm -f "$MARKER_FILE"
  done
  # The narrow ``test/`` passes need probes planted UNDER test/: a marker there
  # exercises the test-tree pass wiring itself, which a probe under src/
  # (already caught by check 1's broad pattern) cannot.
  TEST_PROBES=(
    'review-url|# test marker: see code.amazon.com/reviews/example'
    'review-id|# test marker: review CR-1200456'
  )
  for probe in "${TEST_PROBES[@]}"; do
    probe_label="${probe%%|*}"
    probe_line="${probe#*|}"
    printf '%s\n' "$probe_line" > "$TEST_MARKER_FILE"
    if "$SELF" --no-history >/dev/null 2>&1 </dev/null; then
      red "SELF-TEST FAIL: planted $probe_label marker under test/ was not detected"
      probe_fail=1
    else
      green "  ✓ Planted $probe_label marker under test/ correctly detected"
    fi
    rm -f "$TEST_MARKER_FILE"
  done
  # An allowlisted entry must still pass: the allowlist carries a line-anchored
  # entry for THIS marker file and id, so the same review-id shape planted there
  # is filtered out. Judge by whether the OUTPUT names the marker file, not by
  # the child's whole-run exit code — an unrelated failure elsewhere in the tree
  # (alias file present locally, an identity or credential hit) would otherwise
  # be misreported as this probe's failure. Same style as the clean-tree block
  # below, and for the same reason.
  printf '%s\n' '# test marker: review CR-9999999' > "$ALLOWED_MARKER_FILE"
  allowed_output=$("$SELF" --no-history 2>&1 </dev/null || true)
  if echo "$allowed_output" | grep -q "__scrub_test_marker_allowed"; then
    red "SELF-TEST FAIL: allowlisted review-id marker under test/ was reported"
    probe_fail=1
  else
    green "  ✓ Allowlisted review-id marker under test/ correctly ignored"
  fi
  rm -f "$ALLOWED_MARKER_FILE"
  if [[ $probe_fail -ne 0 ]]; then
    exit 1
  fi

  # Markers removed — scan should pass (ignoring git history which always fails pre-rewrite)
  rm -f "$MARKER_FILE"
  # Run checks 1+2 only (history will fail until rewrite)
  output=$("$SELF" 2>&1 || true)
  if echo "$output" | grep -q "Internal markers found"; then
    red "SELF-TEST FAIL: clean tree still has unexpected markers"
    exit 1
  fi
  if echo "$output" | grep -q "Credential patterns found"; then
    red "SELF-TEST FAIL: clean tree still has unexpected credentials"
    exit 1
  fi
  green "  ✓ Clean tree passes checks 1+2"
  green "Self-test passed ✓"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. Working-tree scan: internal domains, hostnames, account IDs, ticket IDs
# ---------------------------------------------------------------------------
dim "[1/5] Scanning working tree for internal markers..."

# Internal code-review markers — the review host and the review-id shape — are
# needed in two working-tree scans: as alternatives of check 1's broad pattern
# (as always) and by a narrow ``test/`` pass further down (see the ARCC pass for
# why ``test/`` gets narrow per-class passes instead of the broad pattern).
# Defined once so those two scans cannot drift apart. The git-history scan
# (check 5) keeps its own copies of these literals: HISTORY_PATTERN scans commit
# metadata, is tracked as a separate sign-off-gated task, and is deliberately
# not restructured here.
REVIEW_PATTERN='code\.amazon|CR-[0-9]{6,}'

INTERNAL_PATTERN='amazon\.com|a2z\.com|aws\.dev|\.amazon\.|t\.corp|sim\.amazon|isengard|phonetool|midway-auth|mwinit|brazil ws|brazil-build|brazil-runtime|brazil-pkg-cache|meshclaw|Mesh-[0-9]|AVP-[0-9]|account.?[0-9]{12}|\bP[0-9]{6,}\b'

INTERNAL_PATTERN="$INTERNAL_PATTERN|$REVIEW_PATTERN"

# The internal package manager (AIM) needs a NARROW pattern, not a bare word:
# ``--aim``/``bg-aim``/``text-aim`` is an unrelated CSS color token used ~40
# places in the frontend, and English "aim" appears in prose ("aim an artifact
# at"). So match only the shapes that constitute a real coupling: the invocation
# grammar, the ``~/.aim`` home tree, and the ``aim.<type>``/``aim/<type>``
# dependency-contract strings. A bare ``\baim\b`` would be unusable here.
# The quoted ``".aim"`` alternative catches the path-BUILDING form
# (``Path.home() / ".aim" / ...``), which the ``~/.aim`` literal misses.
#
# NOT yet covered: the ``~/.aim`` skill scan in agent.py carries its own
# TODO(aim-governance follow-up) to route through the McpToolingProvider seam;
# until that lands its call sites are allowlisted by path below.
AIM_PATTERN='\baim (mcp|skills|agents|install|uninstall)\b|\baim\.(mcp|skills|agents)\b|\baim/(mcp|skills|agents)\b|~/\.aim|\bAIM CLI\b|"\.aim"'

INTERNAL_PATTERN="$INTERNAL_PATTERN|$AIM_PATTERN"

# The internal governance service (ARCC) needs word boundaries plus the two
# identifier shapes its tool/server names use: ``search_arcc`` (suffix) and
# ``arcc_governance`` (prefix). A bare substring would match the base64 in npm
# lockfile ``integrity`` hashes (``...JkARCCf7rqK...``, a real hit in
# website/electron/package-lock.json) and English words like "marcciano".
ARCC_PATTERN='\barcc\b|_arcc\b|\barcc_'

INTERNAL_PATTERN="$INTERNAL_PATTERN|$ARCC_PATTERN"

matches=$(grep -rniE "$INTERNAL_PATTERN" \
  src/ website/src/ website/docs/ docs/ skills/ scripts/ config/ packaging/ \
  ./*.md \
  --include='*.py' --include='*.ts' --include='*.tsx' --include='*.md' --include='*.json' \
  --include='*.sh' --include='*.yaml' --include='*.yml' --include='*.css' \
  2>/dev/null || true)

# ARCC also gets a SECOND, narrower pass that additionally covers ``test/``,
# which check 1 never scans. Every arcc reference that shipped publicly — MCP
# fixture names, ``search_arcc`` autoApprove entries, a guidance citation in a
# docstring — lived in the test tree, so leaving it unscanned is what let them
# through. The full INTERNAL_PATTERN cannot be pointed at ``test/``: it hits 291
# lines across 29 files (CR-ids, brazil-build strings and internal hostnames that
# are legitimate parser/deny-rule FIXTURES there), and a gate nobody can keep
# green is how check 2 became vacuous. ARCC has no such legitimate use: the
# service is unreachable from a public install, so any occurrence is a leak.
arcc_test_matches=$(grep -rniE "$ARCC_PATTERN" test/ \
  --include='*.py' --include='*.md' --include='*.json' 2>/dev/null || true)
matches=$(printf '%s\n%s\n' "$matches" "$arcc_test_matches")
matches=$(echo "$matches" | grep -v '^$' || true)

# Internal code-review markers get the same narrow treatment — the third
# instance of the pattern the ARCC comment above describes. Check 1's broad
# pattern already carries the review host and the review-id shape (via
# REVIEW_PATTERN above), but check 1 never scans ``test/``, and the passes that
# do (the ARCC pass above, the identity scan below) match neither alternative —
# so a review URL or review id under ``test/`` was reported by no pass at all.
# A legitimate review-id-shaped FIXTURE under ``test/`` takes a line-anchored
# allowlist entry; an incidental one is better reshaped to a non-matching
# token (e.g. ``TICKET-1234567``).
review_test_matches=$(grep -rniE "$REVIEW_PATTERN" test/ \
  --include='*.py' --include='*.md' --include='*.json' 2>/dev/null || true)
matches=$(printf '%s\n%s\n' "$matches" "$review_test_matches")
# Dedupe: a test/ line can match more than one narrow pass (ARCC + review) and
# would otherwise be reported and counted twice.
matches=$(echo "$matches" | grep -v '^$' | awk '!seen[$0]++' || true)

# Filter out allowlisted paths
if [[ -f "$ALLOWLIST" ]]; then
  while IFS= read -r pattern; do
    [[ -z "$pattern" || "$pattern" == \#* ]] && continue
    matches=$(echo "$matches" | grep -v "$pattern" || true)
  done < "$ALLOWLIST"
fi

if [[ -n "$matches" ]]; then
  red "FAIL: Internal markers found in working tree:"
  echo "$matches" | head -20
  count=$(echo "$matches" | wc -l)
  [[ $count -gt 20 ]] && dim "  ... and $((count - 20)) more"
  FAILURES=$((FAILURES + 1))
else
  green "  ✓ No internal markers (outside allowlist)"
fi

# ---------------------------------------------------------------------------
# 2. Employee alias scan (known patterns in non-test source)
# ---------------------------------------------------------------------------
dim "[2/5] Scanning for employee aliases..."

# Aliases are stored in an external file excluded from the public repo.
# If the file doesn't exist, this check is skipped (fresh clones won't have it).
ALIAS_FILE="scripts/.scrub-aliases.txt"

if [[ -f "$ALIAS_FILE" ]]; then
  aliases_joined=$(grep -v '^#' "$ALIAS_FILE" | grep -v '^$' | paste -sd '|')
  if [[ -z "$aliases_joined" ]]; then
    dim "  ⊘ Alias check skipped (alias file has no entries)"
  else
    ALIAS_PATTERN="\b($aliases_joined)\b"

    alias_matches=$(grep -rniE "$ALIAS_PATTERN" \
      src/ website/src/ \
      --include='*.py' --include='*.ts' --include='*.tsx' \
      2>/dev/null || true)

  # Filter allowlist
  if [[ -f "$ALLOWLIST" ]]; then
    while IFS= read -r pattern; do
      [[ -z "$pattern" || "$pattern" == \#* ]] && continue
      alias_matches=$(echo "$alias_matches" | grep -v "$pattern" || true)
    done < "$ALLOWLIST"
  fi
  alias_matches=$(echo "$alias_matches" | grep -v '^$' || true)

  if [[ -n "$alias_matches" ]]; then
    red "FAIL: Employee aliases found in source:"
    echo "$alias_matches"
    FAILURES=$((FAILURES + 1))
  else
    green "  ✓ No employee aliases in source"
  fi
  fi
else
  dim "  ⊘ Alias check skipped (no $ALIAS_FILE — create it with one alias per line)"
fi

# ---------------------------------------------------------------------------
# 3. Personal identity scan (structural — needs NO name list)
#
# The alias scan above reads scripts/.scrub-aliases.txt, which is deliberately
# kept out of the repo (publishing a list of employee names would defeat the
# purpose). Consequence: on a fresh clone — and in CI — that file is absent, the
# check prints "skipped", and the gate becomes VACUOUS. That is exactly how
# personal aliases have shipped before now.
#
# This check needs no configuration and therefore cannot silently degrade. It
# matches the *shape* of a personal identifier rather than any specific name:
#   * a home-directory path whose user segment is not a known generic
#     placeholder (/local/home/<alias>, /home/<alias>, /Users/<alias>)
#   * an @amazon.com address whose local part is not a known fake persona
# Unlike scan 1 it also covers test/, which scan 1 never looks at — real
# addresses in test fixtures were previously unchecked.
# ---------------------------------------------------------------------------
dim "[3/5] Scanning for personal identity leaks (structural)..."

# User segments that are intentional documentation placeholders / system users.
GENERIC_USER='user|users|otheruser|someuser|u|me|you|your|youruser|dev|developer|example|someone|somebody|alice|bob|carol|dave|eve|jane|john|foo|bar|baz|secret|weird|root|runner|ubuntu|node|builder|linuxbrew|kirocrew|kirocrew-workspace|mcp-gateway|nimbus|src|admin|test|tester|testuser|du|voce|você|tu|vous|usuario|usuário|utente|utilisateur|benutzer|<user>|\$USER|\$\{USER\}|\{user\}|%USERNAME%'

# Fake personas permitted in email fixtures.
GENERIC_EMAIL_LOCAL='alice|bob|carol|dave|eve|user|test|tester|example|someone|noreply|no-reply|opensource-codeofconduct|dev|admin'

IDENTITY_SCAN_DIRS=(src/ website/src/ website/docs/ docs/ skills/ scripts/ config/ packaging/ test/)
IDENTITY_INCLUDES=(--include='*.py' --include='*.ts' --include='*.tsx' --include='*.md'
                   --include='*.json' --include='*.sh' --include='*.yaml' --include='*.yml'
                   --include='*.cfg' --include='*.toml')

# --- personal home-directory paths ---
personal_matches=$(grep -rnoE '/(local/home|home|Users)/[A-Za-z][A-Za-z0-9._-]+' \
  "${IDENTITY_SCAN_DIRS[@]}" ./*.md "${IDENTITY_INCLUDES[@]}" 2>/dev/null || true)

# --- Amazon /workplace/<alias> trees ---
# /workplace holds BOTH employee aliases and Brazil package/workspace names, so
# only an ALIAS-SHAPED segment counts. The charset (lowercase start, then
# lowercase/digit/hyphen) admits real logins like `jo-smith` and `dlloyd2` while
# still excluding the CamelCase and dotted names Brazil packages use, so package
# paths do not false-positive.
workplace_matches=$(grep -rnoE '/workplace/[a-z][a-z0-9-]{1,30}(/|$)' \
  "${IDENTITY_SCAN_DIRS[@]}" ./*.md "${IDENTITY_INCLUDES[@]}" 2>/dev/null || true)
workplace_matches=$(echo "$workplace_matches" | sed 's:/$::')

personal_matches=$(printf '%s\n%s\n' "$personal_matches" "$workplace_matches")
personal_matches=$(echo "$personal_matches" \
  | grep -vEi "/(local/home|home|Users|workplace)/($GENERIC_USER)$" || true)

# --- employee-shaped email addresses ---
email_matches=$(grep -rnoE '[A-Za-z0-9._%+-]+@amazon\.com' \
  "${IDENTITY_SCAN_DIRS[@]}" ./*.md "${IDENTITY_INCLUDES[@]}" 2>/dev/null || true)
email_matches=$(echo "$email_matches" \
  | grep -vEi "[:/]($GENERIC_EMAIL_LOCAL)@amazon\.com$" || true)

# NOT covered, deliberately: a bare alias assigned to an identity-ish field (an
# `author`/`assignee`/`principal_id` string literal). It was tried and measured:
# scanning those fields yields ~60 hits of which ~55 are legitimate personas
# (octocat, agent, reviewer, maintainer, a-contributor, yourname...), because a
# bare token is indistinguishable from any other string without a name list. A
# gate nobody can keep green is how scan 2 became vacuous in the first place, so
# this class stays uncovered rather than pretending to be covered — see scan 2.
identity_matches=$(printf '%s\n%s\n' "$personal_matches" "$email_matches")

# Filter allowlist — but ONLY its `path:pattern` entries. A PATH-ONLY entry was
# written to exempt a file from the marker/alias scans (e.g. an auth stub that
# must mention midway); honouring it here would blanket-exempt that whole file
# from identity checking too — verified: planted identity lines in
# test_deploy_web_profiles.py, sensitiveCommand.test.ts and deploy/scan.py were
# all silently dropped. That is exactly the hazard this allowlist warns about at
# :144-150, so an identity hit needs a narrow `path:pattern` entry to be waived.
if [[ -f "$ALLOWLIST" ]]; then
  while IFS= read -r pattern; do
    [[ -z "$pattern" || "$pattern" == \#* ]] && continue
    [[ "$pattern" != *:* ]] && continue
    identity_matches=$(echo "$identity_matches" | grep -v "$pattern" || true)
  done < "$ALLOWLIST"
fi
identity_matches=$(echo "$identity_matches" | grep -v '^$' || true)

if [[ -n "$identity_matches" ]]; then
  red "FAIL: Personal identifiers found (home paths / employee emails / identity fields):"
  echo "$identity_matches"
  red "  Replace with a generic placeholder (e.g. /home/user, alice@amazon.com),"
  red "  or add a narrow entry to $ALLOWLIST if the reference is intentional."
  FAILURES=$((FAILURES + 1))
else
  green "  ✓ No personal identifiers in source or tests"
fi

# ---------------------------------------------------------------------------
# 4. Credential pattern scan
# ---------------------------------------------------------------------------
dim "[4/5] Scanning for credential patterns..."

CRED_PATTERN='AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{20,}|-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----'

cred_matches=$(grep -rniE "$CRED_PATTERN" . \
  --exclude-dir=.git --exclude-dir=node_modules \
  --exclude-dir=.venv --exclude-dir=venv --exclude-dir=env \
  --exclude-dir=dist --exclude-dir=build --exclude-dir=backend-dist \
  --exclude-dir=site-packages --exclude-dir=__pycache__ \
  --include='*.py' --include='*.ts' --include='*.tsx' --include='*.json' \
  --include='*.md' --include='*.sh' --include='*.cfg' --include='*.toml' --include='*.yaml' --include='*.yml' \
  2>/dev/null || true)

# Filter: allow the well-known test fixture key and the two fixture dirs that
# hold synthetic keys the redaction/leak-scanner tests assert on — the backend
# ``test/`` suite and the frontend ``website/src/test/`` suite. These are
# ANCHORED prefixes: a blanket ``/test/`` substring would silently exempt any
# nested ``*/test/`` under shipped source (e.g. an app's ``test/``) from the
# real-credential scan, weakening the gate below its intended scope.
cred_matches=$(echo "$cred_matches" | grep -v "AKIAIOSFODNN7EXAMPLE" || true)
cred_matches=$(echo "$cred_matches" | grep -v "^\./test/" || true)
cred_matches=$(echo "$cred_matches" | grep -v "^\./website/src/test/" || true)
cred_matches=$(echo "$cred_matches" | grep -v "smoke_gateway\|smoke_sandbox" || true)
# Filter: allow documentation references to patterns (not actual keys)
cred_matches=$(echo "$cred_matches" | grep -v 'AKIA\[0-9A-Z\]\|ASIA\[0-9A-Z\]\|\\$AWS_SECRET\|"aws_secret' || true)
# Remove empty lines
cred_matches=$(echo "$cred_matches" | grep -v '^$' || true)

if [[ -n "$cred_matches" ]]; then
  red "FAIL: Credential patterns found (not in allowlist):"
  echo "$cred_matches"
  FAILURES=$((FAILURES + 1))
else
  green "  ✓ No credential leaks"
fi

# ---------------------------------------------------------------------------
# 4. Git history scan (author emails and subjects)
# ---------------------------------------------------------------------------
dim "[5/5] Scanning git history for internal references..."

if [[ $SKIP_HISTORY -eq 1 ]]; then
  dim "  ⊘ Git-history scan skipped (--no-history) — tracked separately"
else

HISTORY_PATTERN='@amazon\.com|@a2z\.com|midway-auth|mwinit|t\.corp|sim\.amazon|code\.amazon|isengard|phonetool|brazil-build|brazil-runtime|CR-[0-9]{6,}|\bP[0-9]{6,}\b'

history_matches=$(git log --all --pretty='%h %ae %ce %s' 2>/dev/null \
  | grep -iE "$HISTORY_PATTERN" || true)

if [[ -n "$history_matches" ]]; then
  count=$(echo "$history_matches" | wc -l)
  red "FAIL: $count commits with internal references in history:"
  echo "$history_matches" | head -10
  [[ $count -gt 10 ]] && dim "  ... and $((count - 10)) more"
  dim "  → Run the git-history rewrite before public push (tracked separately)"
  FAILURES=$((FAILURES + 1))
else
  green "  ✓ Git history clean"
fi

fi  # end SKIP_HISTORY guard

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
if [[ $FAILURES -eq 0 ]]; then
  green "All checks passed ✓"
  exit 0
else
  red "$FAILURES check(s) failed — resolve before publishing"
  exit 1
fi
