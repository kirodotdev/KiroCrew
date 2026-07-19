#!/usr/bin/env bash
# pod-e2e.sh <worktree-name> [--keep] [--no-stop] [--api-only] [--fe-only] [--video]
#
# Run the full e2e flow for ONE worktree against an ISOLATED pod instance,
# never touching the live gateway:
#
#   kirocrew pod up --json  →  health poll  →  auth check  →  API tests  →  Playwright  →  pod down
#
# Everything runs on the pod's own port + its own KIROCREW_HOME. The live
# gateway is never bounced. Teardown is zero-residue unless
# --keep / --no-stop is passed.
#
# Exit code = number of failed phases (0 = all green). Structured summary + an
# artifact dir path are printed at the end so a subagent can parse them.
set -uo pipefail

# ---------------------------------------------------------------- args ----
NAME="" ; KEEP=0 ; NO_STOP=0 ; RUN_API=1 ; RUN_FE=1 ; VIDEO=0
for a in "$@"; do
  case "$a" in
    --keep)     KEEP=1 ;;
    --no-stop)  NO_STOP=1 ;;
    --api-only) RUN_FE=0 ;;
    --fe-only)  RUN_API=0 ;;
    --video)    VIDEO=1 ;;
    -*)         echo "unknown flag: $a" >&2; exit 64 ;;
    *)          NAME="$a" ;;
  esac
done
[ -n "$NAME" ] || { echo "usage: pod-e2e.sh <worktree-name> [--keep] [--no-stop] [--api-only] [--fe-only] [--video]" >&2; exit 64; }
# Pod names are [a-zA-Z0-9._-] without leading dots — reject anything that
# could traverse paths (slashes, '..') before NAME is used in any path.
case "$NAME" in
  */*|.*|*..*) echo "FATAL: invalid worktree name: '$NAME'" >&2; exit 64 ;;
esac
if ! printf '%s' "$NAME" | grep -Eq '^[a-zA-Z0-9][a-zA-Z0-9._-]*$'; then
  echo "FATAL: invalid worktree name: '$NAME'" >&2; exit 64
fi

# ---------------------------------------------------------------- paths ---
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the kirocrew CLI
KIROCREW_CLI=""
_kc="$(command -v kirocrew 2>/dev/null || true)"
if [ -n "$_kc" ] && "$_kc" pod --help >/dev/null 2>&1; then
  KIROCREW_CLI="$_kc"
fi
if [ -z "$KIROCREW_CLI" ]; then
  for _cand in "$HOME/.local/bin/kirocrew" "/usr/local/bin/kirocrew"; do
    if [ -x "$_cand" ] && "$_cand" pod --help >/dev/null 2>&1; then
      KIROCREW_CLI="$_cand"; break
    fi
  done
fi
[ -n "$KIROCREW_CLI" ] || { echo "FATAL: kirocrew CLI with pod subcommand not found on PATH" >&2; exit 65; }

# Resolve the checkout path for $NAME via `git worktree list --porcelain`.
# We search from either KIROCREW_POD_REPO or the script's own directory.
_resolve_checkout() {
  local name="$1"
  local repo_hint="${KIROCREW_POD_REPO:-$HERE}"
  local wt_path=""
  wt_path=$(git -C "$repo_hint" worktree list --porcelain 2>/dev/null | awk -v n="$name" '
    /^worktree / { path = substr($0, 10) }
    /^branch /   {
      if (path != "") {
        bname = path
        gsub(/.*\//, "", bname)
        if (bname == n) { print path; exit }
      }
    }
  ')
  # Fallback: match by directory basename (bare worktree, no branch line yet)
  if [ -z "$wt_path" ]; then
    wt_path=$(git -C "$repo_hint" worktree list --porcelain 2>/dev/null | awk -v n="$name" '
      /^worktree / {
        path = substr($0, 10)
        # basename match
        bname = path
        gsub(/.*\//, "", bname)
        if (bname == n) { print path; exit }
      }
    ')
  fi
  # Final fallback: KIROCREW_POD_WORKTREES_ROOT/<name>
  if [ -z "$wt_path" ] && [ -n "${KIROCREW_POD_WORKTREES_ROOT:-}" ] && [ -d "$KIROCREW_POD_WORKTREES_ROOT/$name" ]; then
    wt_path="$KIROCREW_POD_WORKTREES_ROOT/$name"
  fi
  echo "$wt_path"
}

CHECKOUT="$(_resolve_checkout "$NAME")"
if [ -z "$CHECKOUT" ] || [ ! -d "$CHECKOUT" ]; then
  echo "FATAL: could not resolve worktree checkout for '$NAME'" >&2
  echo "  Ensure a git worktree with basename '$NAME' exists, or set KIROCREW_POD_REPO / KIROCREW_POD_WORKTREES_ROOT" >&2
  exit 66
fi

# Playwright runner (sibling script)
PW_PY="${KIROCREW_PW_PY:-}"
PW_RUNNER="$HERE/pod-playwright.py"

# Artifact dir for this run (logs + results).
# NAME was validated above (pod-name charset, no slashes or dots) so it
# cannot traverse outside .e2e-artifacts; belt-and-braces verify anyway.
ARTIFACT_DIR="$HOME/.kirocrew-pods/.e2e-artifacts/$NAME"
case "$(readlink -f -- "$ARTIFACT_DIR" 2>/dev/null || echo "$ARTIFACT_DIR")" in
  "$HOME/.kirocrew-pods/.e2e-artifacts/"*) : ;;
  *) echo "FATAL: artifact dir escapes .e2e-artifacts: $ARTIFACT_DIR" >&2; exit 65 ;;
esac
mkdir -p "$ARTIFACT_DIR"

# ---------------------------------------------------------------- state ---
ALREADY_UP=0   # if pod was already running, don't stop it
FAILURES=0
declare -a RESULTS=()

# Initialize MANIFEST early (before both API and FE phases reference it).
# Re-discovered below once CHECKOUT is fully resolved.
MANIFEST=""
for _m in "$CHECKOUT/.pod-test.sh" "$CHECKOUT/src/kiro_crew/.pod-test.sh"; do
  [ -f "$_m" ] && MANIFEST="$_m" && break
done

# ---------------------------------------------------------------- cleanup -
_pod_down_best_effort() {
  if [ "$ALREADY_UP" -eq 0 ] && [ "$KEEP" -eq 0 ] && [ "$NO_STOP" -eq 0 ] && [ -n "$NAME" ]; then
    "$KIROCREW_CLI" pod down "$NAME" >/dev/null 2>&1 || true
  fi
}
trap '_pod_down_best_effort' EXIT

log() { printf '\033[36m[pod-e2e]\033[0m %s\n' "$*"; }
pass() { RESULTS+=("  ✅ $1"); }
fail() { RESULTS+=("  ❌ $1"); FAILURES=$((FAILURES + 1)); }

# ---------------------------------------------------------------- up ------
log "starting pod '$NAME' ..."
# pod status exits 0 for both up AND down; parse the --json output to check actual state.
_pod_status_json=$("$KIROCREW_CLI" pod status "$NAME" --json 2>/dev/null || echo '{}')
_pod_is_up=$(echo "$_pod_status_json" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('yes' if d.get('status') == 'up' else 'no')
except Exception:
    print('no')
" 2>/dev/null)

if [ "$_pod_is_up" = "yes" ]; then
  log "pod '$NAME' already up — reusing (won't stop on exit)"
  ALREADY_UP=1
  POD_JSON="$_pod_status_json"
else
  POD_JSON=$("$KIROCREW_CLI" pod up "$NAME" --json 2>"$ARTIFACT_DIR/pod-up.log")
  if [ $? -ne 0 ]; then
    fail "up — pod failed to start (see $ARTIFACT_DIR/pod-up.log)"
    echo ""; echo "=== POD-E2E SUMMARY ==="; printf '%s\n' "${RESULTS[@]}"
    echo "result:       0 passed, $FAILURES failed"
    echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit "$FAILURES"
  fi
fi

BASE_URL=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_url',''))" 2>/dev/null)
TOKEN=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
PORT=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('port',''))" 2>/dev/null)

if [ -z "$BASE_URL" ] || [ -z "$TOKEN" ]; then
  # Try fetching from token verb
  TOKEN=$("$KIROCREW_CLI" pod token "$NAME" 2>/dev/null | tail -1)
  BASE_URL=$("$KIROCREW_CLI" pod url "$NAME" 2>/dev/null | tail -1)
fi

[ -n "$BASE_URL" ] || { fail "up — could not determine base_url"; }
[ -n "$TOKEN" ] || { fail "up — could not determine token"; }

# Safety: refuse if port resolves to the production port
if [ "$PORT" = "5476" ] || [ "$PORT" = "7777" ]; then
  fail "SAFETY — pod resolved to production port $PORT, aborting"
  echo ""; echo "=== POD-E2E SUMMARY ==="; printf '%s\n' "${RESULTS[@]}"
  echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit 1
fi

# ---------------------------------------------------------------- health --
log "waiting for health on $BASE_URL ..."
HEALTHY=0
for i in $(seq 1 45); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/health" 2>/dev/null || echo "000")
  if [ "$CODE" = "200" ] || [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
    HEALTHY=1; break
  fi
  sleep 1
done
if [ "$HEALTHY" -eq 0 ]; then
  "$KIROCREW_CLI" pod logs "$NAME" -n 50 > "$ARTIFACT_DIR/boot-fail.log" 2>&1 || true
  fail "health — pod never became healthy (45s timeout, see boot-fail.log)"
fi

# ---------------------------------------------------------------- auth ----
if [ "$HEALTHY" -eq 1 ]; then
  # Tokenized URL goes to curl via stdin config — argv is world-readable
  # on Linux (/proc/<pid>/cmdline) for the duration of the request.
  AUTH_OK=$(printf 'url = "%s/api/sessions?token=%s"\n' "$BASE_URL" "$TOKEN" | curl -s -o /dev/null -w '%{http_code}' --config - 2>/dev/null)
  AUTH_NO=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/sessions" 2>/dev/null)
  if [ "$AUTH_OK" = "200" ] && [ "$AUTH_NO" = "403" ]; then
    pass "auth — GET /api/sessions → 200 with token, 403 without"
  else
    fail "auth — expected 200/403, got $AUTH_OK/$AUTH_NO"
  fi
fi

# ---------------------------------------------------------------- API -----
if [ "$RUN_API" -eq 1 ] && [ "$HEALTHY" -eq 1 ]; then
  log "running API tests (cwd=$CHECKOUT) ..."
  pushd "$CHECKOUT" >/dev/null
  API_PY="$CHECKOUT/.venv/bin/python"
  if [ ! -x "$API_PY" ]; then
    fail "api-tests — worktree venv python not found at $API_PY (run: kirocrew pod provision $NAME)"
  else
    API_TEST_CMD="$API_PY -m pytest -q"
    export POD_BASE_URL="$BASE_URL"
    export POD_TOKEN="$TOKEN"
    if "$API_PY" -m pytest -q > "$ARTIFACT_DIR/api-tests.log" 2>&1; then
      pass "api-tests — $API_TEST_CMD → exit 0"
    else
      fail "api-tests — $API_TEST_CMD → exit $? (see api-tests.log)"
    fi
  fi
  popd >/dev/null
fi

# ---------------------------------------------------------------- FE ------
if [ "$RUN_FE" -eq 1 ] && [ "$HEALTHY" -eq 1 ]; then
  if [ -z "$PW_PY" ] || [ ! -x "$PW_PY" ]; then
    log "SKIP: Playwright python not found (set KIROCREW_PW_PY)"
    RESULTS+=("  ⚠️  playwright — skipped (no KIROCREW_PW_PY)")
  elif [ ! -f "$PW_RUNNER" ]; then
    log "SKIP: pod-playwright.py not found at $PW_RUNNER"
    RESULTS+=("  ⚠️  playwright — skipped (script missing)")
  else
    log "running Playwright FE check ..."
    # Token goes via env, not argv — process arguments are world-readable
    # on Linux (/proc/<pid>/cmdline) while environment is uid-restricted.
    PW_ARGS=("$PW_RUNNER" --base-url "$BASE_URL" --artifact-dir "$ARTIFACT_DIR" --checkout "$CHECKOUT")
    [ "$VIDEO" -eq 1 ] && PW_ARGS+=(--video)
    # Declarative manifest parse: extract ONLY the PLAYWRIGHT_SPEC value.
    # The manifest is branch-controlled — never source/eval it on the host.
    PLAYWRIGHT_SPEC=""
    if [ -n "$MANIFEST" ]; then
      PLAYWRIGHT_SPEC=$(sed -n 's/^PLAYWRIGHT_SPEC=["'"'"']\{0,1\}\([^"'"'"']*\)["'"'"']\{0,1\}$/\1/p' "$MANIFEST" | head -1)
    fi
    # PLAYWRIGHT_SPEC is manifest-relative per the contract — resolve it
    # against the manifest's directory, not this process's CWD.
    if [ -n "${PLAYWRIGHT_SPEC:-}" ]; then
      case "$PLAYWRIGHT_SPEC" in
        /*) : ;;
        *) [ -n "$MANIFEST" ] && PLAYWRIGHT_SPEC="$(dirname "$MANIFEST")/$PLAYWRIGHT_SPEC" ;;
      esac
      PW_ARGS+=(--spec "$PLAYWRIGHT_SPEC")
    fi
    if KIROCREW_POD_TOKEN="$TOKEN" "$PW_PY" "${PW_ARGS[@]}" > "$ARTIFACT_DIR/playwright.log" 2>&1; then
      pass "playwright — headless chromium loaded dashboard, SPA rendered"
    else
      fail "playwright — exit $? (see playwright.log + screenshots)"
    fi
  fi
fi

# ---------------------------------------------------------------- stop ----
# Explicit teardown (the EXIT trap also covers crash paths).
if [ "$ALREADY_UP" -eq 0 ] && [ "$KEEP" -eq 0 ] && [ "$NO_STOP" -eq 0 ]; then
  log "tearing down pod '$NAME' ..."
  "$KIROCREW_CLI" pod down "$NAME" >/dev/null 2>&1 || true
fi
# Disarm the trap — teardown already done.
trap - EXIT

# ---------------------------------------------------------------- summary -
echo ""
echo "=== POD-E2E SUMMARY ==="
printf '%s\n' "${RESULTS[@]}"
PASSED=$(( ${#RESULTS[@]} - FAILURES ))
echo "result:       $PASSED passed, $FAILURES failed"
echo "ARTIFACT_DIR=$ARTIFACT_DIR"
exit "$FAILURES"
