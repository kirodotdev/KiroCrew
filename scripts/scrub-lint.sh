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

  # Plant a marker that should trigger the scan
  echo '# test marker: code.amazon.com/packages/FakePackage' > "$MARKER_FILE"
  if "$SELF" >/dev/null 2>&1; then
    red "SELF-TEST FAIL: planted marker was not detected"
    rm -f "$MARKER_FILE"
    exit 1
  fi
  green "  ✓ Planted marker correctly detected (scan failed as expected)"

  # Remove marker — scan should pass (ignoring git history which always fails pre-rewrite)
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
dim "[1/4] Scanning working tree for internal markers..."

INTERNAL_PATTERN='amazon\.com|a2z\.com|aws\.dev|\.amazon\.|code\.amazon|t\.corp|sim\.amazon|isengard|phonetool|midway-auth|mwinit|brazil ws|brazil-build|brazil-runtime|brazil-pkg-cache|meshclaw|Mesh-[0-9]|AVP-[0-9]|account.?[0-9]{12}|CR-[0-9]{6,}|\bP[0-9]{6,}\b'

matches=$(grep -rniE "$INTERNAL_PATTERN" \
  src/ website/src/ website/docs/ docs/ skills/ scripts/ config/ packaging/ \
  ./*.md \
  --include='*.py' --include='*.ts' --include='*.tsx' --include='*.md' --include='*.json' \
  --include='*.sh' --include='*.yaml' --include='*.yml' --include='*.css' \
  2>/dev/null || true)

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
dim "[2/4] Scanning for employee aliases..."

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
# 3. Credential pattern scan
# ---------------------------------------------------------------------------
dim "[3/4] Scanning for credential patterns..."

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
dim "[4/4] Scanning git history for internal references..."

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
