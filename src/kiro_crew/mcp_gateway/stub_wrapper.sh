#!/bin/bash
# mc-mcp-stub-wrapper.sh
#
# Shim invoked by kiro-cli in place of calling the KiroCrew MCP stub
# directly.
#
# Kiro-cli strips most env vars when spawning MCP subprocesses. This
# wrapper walks up the ancestor process chain via /proc/<pid>/environ to
# recover session-scoped env vars (currently KIROCREW_CHANNEL_ID) and
# prepends them as explicit flags before exec'ing the Python stub module.
#
# Without this wrapper the stub registers with channel_id=null, collapsing
# all sessions to one PoolKey and losing caller.channel_id in _meta
# injection.
#
# Generated and placed by kiro_crew.mcp_gateway.rewriter. Usage from
# rewritten agent JSON:
#   {"command": "<this-script>", "args": ["--server", "...", ...]}
# All args pass through to the Python stub unchanged, except we also
# prepend a recovered --channel-id when the env walk finds one.
#
# The Python interpreter used to run the stub can be overridden via
# MC_MCP_PY_BIN (falls back to python3 on PATH).

set -euo pipefail

# ── Resolve the python interpreter used to run the stub module ─────────────
PY_BIN="${MC_MCP_PY_BIN:-python3}"

# ── Ancestor env walk ──────────────────────────────────────────────────────
# Walk PPID chain up to 20 levels. For each ancestor, read /proc/<pid>/environ
# and extract the requested var. First non-empty value wins.
_walk_env() {
    local var="$1"
    local pid=$PPID
    local depth=0
    local val=""
    while [[ -n "$pid" && "$pid" != "0" && "$pid" != "1" && "$depth" -lt 20 ]]; do
        if [[ -r "/proc/$pid/environ" ]]; then
            val=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | \
                  awk -F= -v v="$var" '$1 == v { print substr($0, index($0,"=")+1); exit }') || val=""
            if [[ -n "$val" ]]; then
                printf '%s' "$val"
                return 0
            fi
        fi
        # /proc/<pid>/stat: the comm field (field 2) is wrapped in parens and
        # may itself contain spaces AND ')'. Strip through the LAST ')' so the
        # remainder is "<state> <ppid> ..." and take ppid — a naive first-')'
        # strip picks the wrong field for a name like "(foo) bar". The trailing
        # "|| pid=" keeps a transient read failure from aborting under set -e
        # + pipefail.
        if [[ -r "/proc/$pid/stat" ]]; then
            pid=$(sed 's/.*)//' "/proc/$pid/stat" 2>/dev/null | awk '{ print $2 }') || pid=""
        else
            break
        fi
        depth=$((depth + 1))
    done
    return 0
}

channel_id=$(_walk_env KIROCREW_CHANNEL_ID)

# ── Invocation log ─────────────────────────────────────────────────────────
# Single-line JSON to $KIROCREW_HOME/logs/stub_wrapper.jsonl capturing the
# PPID, the server name from argv, and the channel_id we recovered. Best-
# effort; failures to log are silently swallowed so MCP traffic stays up.
_LOG_DIR="${KIROCREW_HOME:-$HOME/.kirocrew}/logs"
_LOG_FILE="$_LOG_DIR/stub_wrapper.jsonl"
_server=""
for ((i=1; i<=$#; i++)); do
    arg="${!i}"
    next_i=$((i + 1))
    if [[ "$arg" == "--server" && $next_i -le $# ]]; then
        _server="${!next_i}"
        break
    fi
done
# Escape a string for safe JSON embedding (backslash, double-quote, control chars).
_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    # Replace literal newlines/tabs with JSON escape sequences.
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}
mkdir -p "$_LOG_DIR" 2>/dev/null && {
    printf '{"ts":"%s","ppid":%s,"server":"%s","recovered_channel_id":"%s"}\n' \
        "$(date -u +%FT%TZ)" \
        "$PPID" \
        "$(_json_escape "$_server")" \
        "$(_json_escape "$channel_id")" \
        >> "$_LOG_FILE" 2>/dev/null || true
}

# ── Build final argv and exec the Python stub module ───────────────────────
EXTRA=()
[[ -n "$channel_id" ]] && EXTRA+=("--channel-id" "$channel_id")

# Expand EXTRA with the ``${arr[@]+"${arr[@]}"}`` idiom: a bare
# "${EXTRA[@]}" on an EMPTY array under ``set -u`` raises "unbound
# variable" on bash < 4.4 (e.g. AL2's bash 4.2), which aborts the wrapper
# before exec — so the MCP server never starts and the client records zero
# tools. EXTRA is empty whenever no channel_id was recovered (every
# dashboard session, which has no KIROCREW_CHANNEL_ID).
exec "$PY_BIN" -m kiro_crew.mcp_gateway.stub ${EXTRA[@]+"${EXTRA[@]}"} "$@"
