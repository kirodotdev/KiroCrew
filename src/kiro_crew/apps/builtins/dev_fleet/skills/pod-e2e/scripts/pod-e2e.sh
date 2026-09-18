#!/usr/bin/env bash
# pod-e2e.sh <worktree-name> [--handle-json <path>] [--keep] [--no-stop] [--api-only] [--video] [--no-suppress-first-run]
#
# Run the full e2e flow for ONE worktree against an ISOLATED pod instance,
# never touching the live gateway:
#
#   kirocrew pod up --json  →  health poll  →  auth check  →  Playwright  →  pod down
#
# --handle-json <path> runs the same flow against a pod SOMEBODY ELSE started.
# The file holds that pod's handle -- name, base_url, token, port, and the health
# `pod status` reports -- so this script calls no pod verb at all: no status, no
# up, no token, no url, no logs, no down. Every pod verb talks to the systemd
# user bus, and a session behind an outer sandbox with its own user namespace
# cannot reach it, which is what the pod_up / pod_status MCP tools are for. Write
# their output to a file, pass it here, and the phases that need no bus -- the
# production-port refusal, the tokenized auth check, Playwright, the artifacts --
# run unchanged.
#
# Two things move to the caller in that mode, and the summary says so rather than
# printing a probe that never ran. The health verdict is read from the handle
# instead of polled here, so a caller that supplies a stale one is testing a pod
# that has since died. And the pod was booted by whatever build the gateway runs,
# NOT by the worktree's own CLI this script otherwise pins -- so for a diff that
# changes pod lifecycle code itself, the CLI path above is the one that tests it.
#
# It does NOT run the worktree's test suite: scoped, change-relevant tests are
# the dev agent's job in its own worktree, and CI runs the full suite on the
# merge ref. This harness proves the pod boots, auths and renders.
#
# Everything runs on the pod's own port + its own KIROCREW_HOME. The live
# gateway is never bounced. Teardown deletes the pod's HOME and verifies it is
# gone (a survivor is reported, not called zero residue) unless
# --keep / --no-stop is passed.
#
# Exit code = number of failed phases (0 = all green). Structured summary + an
# artifact dir path are printed at the end so a subagent can parse them.
#
# Env knobs:
#   POD_E2E_PW_TIMEOUT        hard cap (s) on the whole Playwright phase (default 600)
#   POD_E2E_TEARDOWN_TIMEOUT  hard cap (s) per browser-teardown step (default 30)
set -uo pipefail

# ---------------------------------------------------------------- args ----
NAME="" ; KEEP=0 ; NO_STOP=0 ; RUN_FE=1 ; VIDEO=0
NO_SUPPRESS_FIRST_RUN=0
HANDLE_JSON=""
# Shifts per argument rather than iterating "$@", because --handle-json takes a
# value and the loop must be able to consume the next word.
while [ $# -gt 0 ]; do
  a="$1"
  case "$a" in
    --keep)     KEEP=1 ;;
    --no-stop)  NO_STOP=1 ;;
    --api-only) RUN_FE=0 ;;
    # Accepted no-op: with no test-suite phase to skip, "frontend only" is
    # what every run already does. Kept so older invocations and stale agent
    # prompts do not die on exit 64.
    --fe-only)  : ;;
    --video)    VIDEO=1 ;;
    # Documented in SKILL.md and accepted by pod-playwright.py; without this
    # arm the catch-all below rejects the documented spelling with exit 64.
    --no-suppress-first-run) NO_SUPPRESS_FIRST_RUN=1 ;;
    # Both spellings, because a flag this file documents and the catch-all
    # rejects is a failure mode it has already been patched for twice.
    --handle-json)
      [ $# -ge 2 ] || { echo "--handle-json needs a path" >&2; exit 64; }
      [ -n "$2" ] || { echo "--handle-json needs a non-empty path" >&2; exit 64; }
      HANDLE_JSON="$2" ; shift ;;
    --handle-json=*)
      HANDLE_JSON="${a#*=}"
      [ -n "$HANDLE_JSON" ] || { echo "--handle-json needs a non-empty path" >&2; exit 64; } ;;
    -*)         echo "unknown flag: $a" >&2; exit 64 ;;
    *)          NAME="$a" ;;
  esac
  shift
done
[ -n "$NAME" ] || { echo "usage: pod-e2e.sh <worktree-name> [--handle-json <path>] [--keep] [--no-stop] [--api-only] [--video] [--no-suppress-first-run]" >&2; exit 64; }
# Pod names are [a-zA-Z0-9._-] without leading dots — reject anything that
# could traverse paths (slashes, '..') before NAME is used in any path.
case "$NAME" in
  */*|.*|*..*) echo "FATAL: invalid worktree name: '$NAME'" >&2; exit 64 ;;
esac
if ! printf '%s' "$NAME" | grep -Eq '^[a-zA-Z0-9][a-zA-Z0-9._-]*$'; then
  echo "FATAL: invalid worktree name: '$NAME'" >&2; exit 64
fi

# A bad handle must fail HERE. Left to be discovered where the payload is read,
# it surfaces as "could not determine base_url", which reads like the pod failed
# to boot and sends the reader looking at a pod that is running fine.
#
# Validate and canonicalize together: every later consumer reads this emitted
# object, never the raw handle. That keeps the value checked for safety identical
# to the value used in comparisons, curl configuration and Playwright arguments.
DEFAULT_LIVE_PORT=5476
# Resolve this variable with the PRODUCER's expression, in the producer's language,
# rather than a second implementation here. `_env_int` in pod/config.py accepts
# `val.strip().lstrip("-").isdigit()` and returns `int(val.strip())`, and
# `str.isdigit()` spans every Unicode decimal digit -- so a shell test over `0-9`
# resolves a DIFFERENT live plane than the gateway for a fullwidth or Arabic-Indic
# setting, leaving the real one out of the refused set. Three review rounds each
# found another spelling that diverged (leading zeros, surrounding whitespace,
# non-ASCII digits); sharing the expression retires the class, not one spelling.
# python3 is already a hard dependency of this harness: every `--json` read below
# goes through it.
if ! CONFIGURED_LIVE_PORT=$(KIROCREW_POD_E2E_LIVE_PORT_DEFAULT="$DEFAULT_LIVE_PORT" python3 -c '
import os, sys

default = int(os.environ["KIROCREW_POD_E2E_LIVE_PORT_DEFAULT"])
raw = os.environ.get("KIROCREW_POD_LIVE_PORT")
if raw is None:
    print(default)
    raise SystemExit(0)
port = None
if raw.strip().lstrip("-").isdigit():
    try:
        port = int(raw.strip())
    except ValueError:
        # `isdigit()` is true for characters `int()` refuses, such as a superscript.
        # The gateway raises on those, so no plane is listening there.
        port = None
if port is None or not 1 <= port <= 65535:
    sys.stderr.write(
        "pod-e2e: ignoring KIROCREW_POD_LIVE_PORT=%r "
        "(want a decimal port in 1..65535); using %d\n" % (raw, default)
    )
    port = default
print(port)
'); then
  echo "FATAL: could not resolve the live-plane port (is python3 on PATH?)" >&2
  exit 70
fi
HANDLE_REFUSED_PORTS=("$CONFIGURED_LIVE_PORT" "$DEFAULT_LIVE_PORT" 7777)
CANONICAL_HANDLE_JSON=""
if [ -n "$HANDLE_JSON" ]; then
  [ -f "$HANDLE_JSON" ] || { echo "FATAL: --handle-json file not found: $HANDLE_JSON" >&2; exit 64; }
  if ! CANONICAL_HANDLE_JSON=$(python3 -c '
import ipaddress, json, re, sys, unicodedata, urllib.parse
expected_name = sys.argv[2]
configured_live_port = int(sys.argv[3])
refused_live_ports = {int(value) for value in sys.argv[3:]}
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    raise SystemExit("not readable JSON: %s" % exc)
if not isinstance(d, dict):
    raise SystemExit("not a JSON object")
required_fields = {"name", "base_url", "token", "port", "health"}
if not {"base_url", "token"} <= d.keys() or not d.get("base_url") or not d.get("token"):
    raise SystemExit("needs a non-empty base_url and token")
if not required_fields <= d.keys():
    if "port" not in d:
        raise SystemExit("port must be a canonical decimal integer in 1..65535")
    if "name" not in d:
        raise SystemExit("name must be a non-empty string")
    raise SystemExit("health is required")
if not isinstance(d["name"], str) or not d["name"]:
    raise SystemExit("name must be a non-empty string")
name = d["name"]
if name != expected_name:
    raise SystemExit("handle name %r does not match requested pod %r" % (name, expected_name))
base_url = d["base_url"]
token = d["token"]
if not isinstance(base_url, str) or not isinstance(token, str):
    raise SystemExit("base_url and token must be strings")
def has_control_or_space(value):
    return any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)
if has_control_or_space(base_url):
    raise SystemExit("base_url contains whitespace or a control character")
if has_control_or_space(token):
    raise SystemExit("token contains whitespace or a control character")
if re.fullmatch(r"[-A-Za-z0-9._~+/=]+", token) is None:
    raise SystemExit("token contains characters outside the generated token format")
port_text = str(d.get("port"))
if isinstance(d.get("port"), bool) or re.fullmatch(r"[1-9][0-9]*", port_text) is None:
    raise SystemExit("port must be a canonical decimal integer in 1..65535")
claimed = int(port_text)
if claimed > 65535:
    raise SystemExit("port must be a canonical decimal integer in 1..65535")
try:
    url = urllib.parse.urlparse(base_url)
    url_port = url.port
except ValueError as exc:
    raise SystemExit("unparseable base_url: %s" % exc)
if url.scheme != "http":
    raise SystemExit("base_url must be http, not %r" % url.scheme)
host = url.hostname or ""
try:
    loopback = ipaddress.ip_address(host).is_loopback
except ValueError:
    loopback = host == "localhost"
if not loopback:
    raise SystemExit("base_url host %r is not loopback; a pod is never remote" % host)
if url_port is None:
    raise SystemExit("base_url needs an explicit port")
if url_port == configured_live_port:
    raise SystemExit("base_url port %d is the configured live plane" % url_port)
if url_port in refused_live_ports:
    raise SystemExit("base_url port %d is reserved for a production gateway" % url_port)
if claimed != url_port:
    raise SystemExit("port %d does not match base_url port %d" % (claimed, url_port))
canonical_host = "[%s]" % host if ":" in host else host
print(json.dumps({
    "name": name,
    "base_url": "http://%s:%d" % (canonical_host, claimed),
    "token": token,
    "port": claimed,
    "health": d["health"],
}, separators=(",", ":")))
' "$HANDLE_JSON" "$NAME" "${HANDLE_REFUSED_PORTS[@]}"); then
    echo "FATAL: unusable --handle-json: $HANDLE_JSON" >&2
    echo '  Expected the object pod_up returns: {"name": ..., "base_url": "http://127.0.0.1:<port>", "token": ..., "port": <port>, "health": ...}' >&2
    exit 64
  fi
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
# In handle mode no pod verb runs, so an absent CLI is not a blocker: the whole
# point of that mode is a host where the CLI's own path does not work.
if [ -z "$HANDLE_JSON" ]; then
  [ -n "$KIROCREW_CLI" ] || { echo "FATAL: kirocrew CLI with pod subcommand not found on PATH" >&2; exit 65; }
fi

# Resolve the checkout path for $NAME via `git worktree list --porcelain`.
# We search from either KIROCREW_POD_REPO or the script's own directory.
#
# This MUST mirror pod/runtime.py resolve_checkout(), or the harness can test a
# different checkout than `kirocrew pod up` booted — a silently wrong QA verdict.
# That function builds one keyspace with dict.setdefault (first occurrence wins)
# over each worktree's basename, absolute path and FULL branch name (minus
# refs/heads/), then does exactly two lookups:
#     wts.get(name) or wts.get(f"feat/{name}")
# Both are EXACT. Matching a branch *leaf* instead would pick `fix/foo` for
# NAME=foo when the CLI picks `feat/foo`.
_resolve_checkout() {
  local name="$1"
  local repo_hint="${KIROCREW_POD_REPO:-$HERE}"
  local wt_path=""
  # Stage 1 = wts.get(name): first worktree, in porcelain order, whose directory
  # basename or whose exact branch equals $name. Basename is checked as the
  # `worktree` line is read, mirroring setdefault's within-record ordering.
  wt_path=$(git -C "$repo_hint" worktree list --porcelain 2>/dev/null | awk -v n="$name" '
    /^worktree / {
      path = substr($0, 10)
      bname = path
      sub(/.*\//, "", bname)
      if (bname == n) { print path; exit }
      next
    }
    /^branch / {
      ref = $2
      sub(/^refs\/heads\//, "", ref)
      if (ref == n) { print path; exit }
    }
  ')
  # Stage 2 = wts.get("feat/" + name): exact `feat/<name>` branch only.
  if [ -z "$wt_path" ]; then
    wt_path=$(git -C "$repo_hint" worktree list --porcelain 2>/dev/null | awk -v n="$name" '
      /^worktree / { path = substr($0, 10) }
      /^branch / {
        ref = $2
        sub(/^refs\/heads\//, "", ref)
        if (ref == "feat/" n) { print path; exit }
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

# Prefer the WORKTREE'S OWN CLI for the pod verbs, now that the checkout is known.
# The PATH-resolved binary above is whatever build happens to be installed on the
# host, so on a machine whose install predates the branch under test, `pod up` /
# `pod down` exercise the INSTALLED code and the verdict describes the wrong build.
# That is not hypothetical: it is how a teardown fix was verified green while the
# harness's own `down` — running the older CLI — left the pod HOME on disk.
#
# The venv is built FIRST, because selecting on "is it already executable" silently
# fell back to the installed CLI for the common case of a checkout that has a built
# dist but no venv yet: `pod up` would create the venv while every lifecycle command
# kept using the stale build. Provision it with the installed CLI (that is what it
# is for), then REQUIRE the worktree's own binary — running the wrong build is a
# false verdict, so it is a hard failure, not a fallback.
_wt_kc="$CHECKOUT/.venv/bin/kirocrew"
if [ -n "$HANDLE_JSON" ]; then
  # Handle mode runs no pod verb, so there is no lifecycle build to pin, and
  # requiring the worktree binary would fail a run that never calls it. The pod
  # was booted elsewhere; the header says what that costs the verdict.
  KIROCREW_CLI=""
  echo "kirocrew CLI: unused (pod handle supplied)"
else
  if [ ! -x "$_wt_kc" ]; then
    echo "provisioning the worktree venv so the suite runs its own build..."
    "$KIROCREW_CLI" pod provision "$NAME" --venv-only || true
  fi
  if [ ! -x "$_wt_kc" ] || ! "$_wt_kc" pod --help >/dev/null 2>&1; then
    echo "FATAL: no usable CLI in the worktree venv at $_wt_kc" >&2
    echo "  The suite must run the branch under test, not the host's installed build." >&2
    echo "  Build it: kirocrew pod provision $NAME --venv-only" >&2
    exit 67
  fi
  KIROCREW_CLI="$_wt_kc"
  echo "kirocrew CLI: $KIROCREW_CLI"
fi

# Playwright runner (sibling script)
PW_PY="${KIROCREW_PW_PY:-}"
PW_RUNNER="$HERE/pod-playwright.py"

# Artifact dir for this run (logs + results).
# NAME was validated above (pod-name charset, no slashes or dots) so it
# cannot traverse outside .e2e-artifacts; belt-and-braces verify anyway.
#
# The verification must resolve BOTH sides the same way. It previously resolved
# the candidate with `readlink -f` but compared it against a pattern built from
# the UNRESOLVED $HOME, so on any host where ~ is a symlink (the standard Amazon
# dev-desktop layout, /home/<u> -> /local/home/<u>) the resolved candidate began
# /local/home/... while the pattern began /home/... — every path "escaped" and
# the suite aborted with exit 65 before running anything.
#
# `readlink -f` is also a GNU extension: BSD/macOS readlink has no -f before
# Ventura, so it silently fell back to the unresolved path there. _realpath_dir
# resolves physically with cd -P/pwd -P (POSIX) and tolerates a not-yet-created
# leaf by resolving the deepest existing ancestor.
_realpath_dir() {
  local p="$1" tail="" seg out=""
  while [ ! -d "$p" ] && [ "$p" != "/" ] && [ -n "$p" ]; do
    tail="$(basename -- "$p")${tail:+/$tail}"
    p="$(dirname -- "$p")"
  done
  if [ -d "$p" ]; then
    p="$(cd -P -- "$p" 2>/dev/null && pwd -P)" || return 1
  fi
  out="${p%/}"
  # Lexically normalise the not-yet-created tail. `readlink -f` collapses `..`;
  # a bare cd/pwd loop does not, and re-appending the tail verbatim would let
  # `<base>/../../x` keep the base as a literal prefix and satisfy a containment
  # check it should fail. Collapse here so the guard stays at least as strict as
  # the GNU implementation it replaces.
  local IFS=/
  for seg in $tail; do
    case "$seg" in
      '' | .) ;;
      ..) out="${out%/*}" ;;
      *) out="$out/$seg" ;;
    esac
  done
  printf '%s\n' "${out:-/}"
}

E2E_ARTIFACT_BASE="$(_realpath_dir "$HOME/.kirocrew-pods/.e2e-artifacts")" \
  || E2E_ARTIFACT_BASE="$HOME/.kirocrew-pods/.e2e-artifacts"
ARTIFACT_DIR="$E2E_ARTIFACT_BASE/$NAME"
case "$(_realpath_dir "$ARTIFACT_DIR")" in
  "$E2E_ARTIFACT_BASE"/*) : ;;
  *) echo "FATAL: artifact dir escapes .e2e-artifacts: $ARTIFACT_DIR" >&2; exit 65 ;;
esac
mkdir -p "$ARTIFACT_DIR"

# Truncate the verdict on EVERY invocation, not just when the driver is
# launched. The artifact dir is keyed per worktree and persists, so a run
# that skips the FE phase (--api-only, unhealthy pod, no KIROCREW_PW_PY) or
# a driver that dies before it can reset the file itself would otherwise
# leave a PREVIOUS run's rows to be read as this run's verdict.
: > "$ARTIFACT_DIR/verdict.jsonl"

# ---------------------------------------------------------------- state ---
ALREADY_UP=0   # if pod was already running, don't stop it
FAILURES=0
WARNINGS=0
declare -a RESULTS=()

# Initialize MANIFEST early (before the FE phase references it).
# Re-discovered below once CHECKOUT is fully resolved.
MANIFEST=""
for _m in "$CHECKOUT/.pod-test.sh" "$CHECKOUT/src/kiro_crew/.pod-test.sh"; do
  [ -f "$_m" ] && MANIFEST="$_m" && break
done

# ---------------------------------------------------------------- cleanup -
_pod_down_best_effort() {
  # The HANDLE_JSON arm closes the window before the up section sets ALREADY_UP:
  # a crash in between must still never stop a pod this run did not start.
  if [ -z "$HANDLE_JSON" ] && [ "$ALREADY_UP" -eq 0 ] && [ "$KEEP" -eq 0 ] && [ "$NO_STOP" -eq 0 ] && [ -n "$NAME" ]; then
    "$KIROCREW_CLI" pod down "$NAME" >/dev/null 2>&1 || true
  fi
}
trap '_pod_down_best_effort' EXIT

log() { printf '\033[36m[pod-e2e]\033[0m %s\n' "$*"; }
pass() { RESULTS+=("  ✅ $1"); }
fail() { RESULTS+=("  ❌ $1"); FAILURES=$((FAILURES + 1)); }
# A warning is neither a pass nor a fail — it is counted separately so it
# never inflates the passed count in the summary.
warn() { RESULTS+=("  ⚠️  $1"); WARNINGS=$((WARNINGS + 1)); }

# ---------------------------------------------------------------- up ------
if [ -n "$HANDLE_JSON" ]; then
  log "pod '$NAME' handle supplied — no pod verb runs, pod stays up on exit"
  POD_JSON="$CANONICAL_HANDLE_JSON"
  # The caller owns this pod, so it is not ours to stop. ALREADY_UP is the same
  # switch that spares a pod this run found already running.
  ALREADY_UP=1
else
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
    # The health-wait budget of the `pod up` below is tunable from THIS shell:
    # KIROCREW_POD_HEALTH_SECS is inherited by the spawned command (default 90s;
    # see `kirocrew pod up --help`). Raise it on a loaded host where a healthy
    # gateway boots slowly and pod-up.log ends mid-boot.
    POD_JSON=$("$KIROCREW_CLI" pod up "$NAME" --json 2>"$ARTIFACT_DIR/pod-up.log")
    if [ $? -ne 0 ]; then
      fail "up — pod failed to start (see $ARTIFACT_DIR/pod-up.log)"
      echo ""; echo "=== POD-E2E SUMMARY ==="; printf '%s\n' "${RESULTS[@]}"
      echo "result:       0 passed, $FAILURES failed"
      echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit "$FAILURES"
    fi
  fi
fi

BASE_URL=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('base_url',''))" 2>/dev/null)
TOKEN=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)
PORT=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('port',''))" 2>/dev/null)
# Handle mode only: the required health code the caller's own pod_status read reported.
HANDLE_HEALTH=$(echo "$POD_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('health',''))" 2>/dev/null)

if [ -z "$BASE_URL" ] || [ -z "$TOKEN" ]; then
  # Only the CLI path has verbs to fall back on. A handle was already checked for
  # both fields, so in handle mode the two assertions below are the whole answer.
  if [ -z "$HANDLE_JSON" ]; then
    # Try fetching from token verb
    TOKEN=$("$KIROCREW_CLI" pod token "$NAME" 2>/dev/null | tail -1)
    BASE_URL=$("$KIROCREW_CLI" pod url "$NAME" 2>/dev/null | tail -1)
  fi
fi

[ -n "$BASE_URL" ] || { fail "up — could not determine base_url"; }
[ -n "$TOKEN" ] || { fail "up — could not determine token"; }

# Safety: refuse if port resolves to the production port
PORT_IS_LIVE_PLANE=0
for _live_plane_port in "${HANDLE_REFUSED_PORTS[@]}"; do
  if [ "$PORT" = "$_live_plane_port" ]; then
    PORT_IS_LIVE_PLANE=1
    break
  fi
done
if [ "$PORT_IS_LIVE_PLANE" -eq 1 ]; then
  fail "SAFETY — pod resolved to production port $PORT, aborting"
  echo ""; echo "=== POD-E2E SUMMARY ==="; printf '%s\n' "${RESULTS[@]}"
  echo "ARTIFACT_DIR=$ARTIFACT_DIR"; exit 1
fi

# ---------------------------------------------------------------- health --
# Identity-gated, deliberately NOT a bare probe of base_url. A pod's port is
# derived from its name across 199 slots and can be pinned by hand, so it is
# routinely held by another pod or by the live gateway -- and every Kiro Crew
# gateway answers /api/health with the same body, so a 200 from that port proves
# only that SOMETHING is there. `pod status --json` reports the pod's OWN health:
# the HTTP code when the process a 127.0.0.1 connect reaches is this pod's own
# gateway, 0 when nothing answers, and -2 when the responder is provably somebody
# else's. Curling base_url here would accept a stranger's 200 and hand every
# later phase -- auth, Playwright, the artifacts -- a pod this run
# never booted.
if [ -n "$HANDLE_JSON" ]; then
  log "reading health from the supplied handle for $NAME ($BASE_URL) ..."
else
  log "waiting for health on $NAME ($BASE_URL) ..."
fi
HEALTHY=0
FOREIGN=0
# Overridable so a slow host can wait longer, and so the phase is drivable in a
# test without sitting out the real deadline. Validated as 1-6 plain decimal
# digits BEFORE any arithmetic, and read with an explicit 10# base, because a
# plain typo in this env var otherwise breaks the run three different ways:
#   abc    -> inside $(( )) bash reads it as a variable NAME; under `set -u`
#             that is "abc: unbound variable" and the run dies (exit 127).
#   08     -> a leading zero means octal, and 08 is not valid octal:
#             "value too great for base", the run dies (exit 1).
#   1e24   -> no error at all: the deadline lands centuries out and the poll
#             never gives up. For an unattended harness a silent hang is worse
#             than a crash, which is what the 6-digit cap (~11 days) closes.
# All three die or hang BEFORE any verdict or the summary is printed, so the
# value is normalized here rather than trusted at the point of use.
HEALTH_TIMEOUT="${POD_E2E_HEALTH_TIMEOUT:-60}"
case "$HEALTH_TIMEOUT" in
  ''|*[!0-9]*) HEALTH_TIMEOUT="" ;;
esac
if [ -z "$HEALTH_TIMEOUT" ] || [ "${#HEALTH_TIMEOUT}" -gt 6 ]; then
  echo "pod-e2e: ignoring POD_E2E_HEALTH_TIMEOUT='${POD_E2E_HEALTH_TIMEOUT:-}' (want 1-6 decimal digits of seconds); using 60" >&2
  HEALTH_TIMEOUT=60
fi
HEALTH_DEADLINE=$(( $(date +%s) + 10#$HEALTH_TIMEOUT ))
# Probe FIRST, test the deadline after -- a do-while, not a while. `date +%s` has
# whole-second resolution, so a pre-test loop reads a clock that may already have
# ticked past the deadline computed one fork earlier, and skips its body. The body
# is the only place HEALTHY is set, so that zero-probe run reports "never became
# healthy" about a pod nobody ever asked. The window is one fork wide, which is
# invisible at the 60s default and near-certain to bite at the 1s a test uses.
while :; do
  # One place decides what a healthy pod looks like. Handle mode substitutes the
  # code the caller already read for the one this loop would poll, so the arms
  # below -- including the -2 "somebody else holds the port" arm -- judge both
  # modes identically instead of growing a second verdict.
  if [ -n "$HANDLE_JSON" ]; then
    CODE="$HANDLE_HEALTH"
  else
    CODE=$("$KIROCREW_CLI" pod status "$NAME" --json 2>/dev/null \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("health",0))' 2>/dev/null \
      || echo 0)
  fi
  case "$CODE" in
    200|401|403) HEALTHY=1; break ;;
    # Keep polling: a predecessor may still be releasing the port. Remembered so
    # the timeout names the conflict instead of blaming the worktree build.
    -2)          FOREIGN=1 ;;
  esac
  # Nothing to wait for in handle mode: the code is a fact the caller read once,
  # not a state that changes while this loop sleeps.
  [ -z "$HANDLE_JSON" ] || break
  # Deadline reached: stop without burning a final pointless sleep.
  [ "$(date +%s)" -lt "$HEALTH_DEADLINE" ] || break
  sleep 1
done
if [ "$HEALTHY" -eq 0 ]; then
  if [ -n "$HANDLE_JSON" ]; then
    # No pod verb here, so no journal tail: name the code that was supplied and
    # where a fresh one comes from, rather than a boot-fail.log never written.
    if [ "$FOREIGN" -eq 1 ]; then
      fail "health — the handle says :$PORT is held by another process, not this pod's gateway; pin a free PORT= for $NAME"
    else
      fail "health — the handle carries health=${CODE:-<absent>}, not a healthy code; re-read it with pod_status (no bus here to poll)"
    fi
  else
    "$KIROCREW_CLI" pod logs "$NAME" -n 50 > "$ARTIFACT_DIR/boot-fail.log" 2>&1 || true
    if [ "$FOREIGN" -eq 1 ]; then
      fail "health — :$PORT is held by another process, not this pod's gateway; pin a free PORT= for $NAME (see boot-fail.log)"
    else
      fail "health — pod never became healthy (${HEALTH_TIMEOUT}s timeout, see boot-fail.log)"
    fi
  fi
elif [ -n "$HANDLE_JSON" ]; then
  # The polled path records no health row, so a green summary means "probed and
  # healthy". Handle mode must not borrow that meaning silently: this run did not
  # confirm the pod, it was told, and a stale handle points at a dead pod.
  pass "health — supplied by the caller (health=$CODE), not polled here"
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

# There is deliberately NO test-suite phase here, and adding one is a
# regression. A `python -m pytest -q` from the checkout root is the wrong tool
# in the wrong place: ~62k tests that need no pod at all, that CI runs on the
# merge ref anyway, and whose fan-out on a shared dev box costs more than the
# browser check this harness exists for. Scoped, change-relevant tests belong
# to the dev agent in its own worktree (see the kirocrew-worktree-dev skill);
# pod-e2e proves the pod BOOTS, AUTHS and RENDERS.

# ---------------------------------------------------------------- FE ------
if [ "$RUN_FE" -eq 1 ] && [ "$HEALTHY" -eq 1 ]; then
  if [ -z "$PW_PY" ] || [ ! -x "$PW_PY" ]; then
    # The frontend phase was REQUESTED (no --api-only) and cannot run, so it
    # produced zero screenshots. Warning here made the run print a green
    # summary with no evidence — which is how "capture is in flight" becomes a
    # believable but false statement. Fail instead. Pin playwright==1.61.0: it
    # pins chromium-1228, which the Node Playwright MCP server has already
    # downloaded into ~/.cache/ms-playwright, so any other version triggers a
    # fresh ~170MB browser download.
    log "FAIL: Playwright python not found (set KIROCREW_PW_PY)"
    log "  python3 -m venv <path> && <path>/bin/pip install playwright==1.61.0"
    log "  export KIROCREW_PW_PY=<path>/bin/python"
    log "  (or re-run with --api-only to skip the frontend phase deliberately)"
    fail "playwright — no usable KIROCREW_PW_PY, so zero screenshots were captured (see fix above)"
  elif [ ! -f "$PW_RUNNER" ]; then
    # Same false-green defect: the phase was requested, the driver is missing,
    # no screenshots exist. A broken install must not report success.
    log "FAIL: pod-playwright.py not found at $PW_RUNNER"
    fail "playwright — driver missing at $PW_RUNNER, so zero screenshots were captured"
  else
    log "running Playwright FE check ..."
    # Token goes via env, not argv — process arguments are world-readable
    # on Linux (/proc/<pid>/cmdline) while environment is uid-restricted.
    PW_ARGS=("$PW_RUNNER" --base-url "$BASE_URL" --artifact-dir "$ARTIFACT_DIR" --checkout "$CHECKOUT")
    PW_ARGS+=(--teardown-timeout "${POD_E2E_TEARDOWN_TIMEOUT:-30}")
    [ "$VIDEO" -eq 1 ] && PW_ARGS+=(--video)
    [ "$NO_SUPPRESS_FIRST_RUN" -eq 1 ] && PW_ARGS+=(--no-suppress-first-run)
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
    # Every other phase here is bounded (the `up` health wait defaults to 90s,
    # tunable via KIROCREW_POD_HEALTH_SECS; this harness's own health poll caps
    # at 60s); this one
    # used to be unbounded and could stall forever in browser teardown, burning
    # a whole agent budget after the verdict was already decided. `python -u`
    # keeps playwright.log flushed so a stall is still diagnosable.
    PW_TIMEOUT="${POD_E2E_PW_TIMEOUT:-600}"
    PW_CMD=("$PW_PY" -u "${PW_ARGS[@]}")
    if command -v timeout >/dev/null 2>&1; then
      PW_CMD=(timeout --kill-after=30s "${PW_TIMEOUT}s" "${PW_CMD[@]}")
    else
      log "WARN: coreutils 'timeout' not found — Playwright phase runs unbounded"
    fi
    KIROCREW_POD_TOKEN="$TOKEN" "${PW_CMD[@]}" > "$ARTIFACT_DIR/playwright.log" 2>&1
    PW_RC=$?
    if [ "$PW_RC" -eq 0 ]; then
      pass "playwright — headless chromium loaded dashboard, SPA rendered"
    elif [ "$PW_RC" -eq 124 ] || [ "$PW_RC" -eq 137 ]; then
      # 124 = timeout expired, 137 = SIGKILL from --kill-after.
      fail "playwright — TIMED OUT after ${PW_TIMEOUT}s (partial artifacts kept: see playwright.log, verdict.jsonl, screenshots)"
    else
      fail "playwright — exit $PW_RC (see playwright.log + screenshots)"
    fi
    # A bounded-teardown bail keeps the verdict (so the phase can still pass),
    # but the operator should know the recording may be truncated.
    if grep -q '"phase": "teardown".*"status": "fail"' "$ARTIFACT_DIR/verdict.jsonl" 2>/dev/null; then
      warn "playwright teardown — abandoned on timeout; recording may be truncated (assertions above still valid)"
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
PASSED=$(( ${#RESULTS[@]} - FAILURES - WARNINGS ))
SUMMARY="result:       $PASSED passed, $FAILURES failed"
[ "$WARNINGS" -gt 0 ] && SUMMARY="$SUMMARY, $WARNINGS warning(s)"
echo "$SUMMARY"
echo "ARTIFACT_DIR=$ARTIFACT_DIR"
exit "$FAILURES"
