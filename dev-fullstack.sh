#!/bin/bash
# Start the full KiroCrew dev stack in ONE terminal:
#   1. Backend gateway from live source (dev-backend.sh, port 6777, .kirocrew-dev home)
#   2. Vite dev server (hot reload) proxying to it
#   3. Mint a dashboard token and print the ready-to-open Vite URL
#
# Usage: ./dev-fullstack.sh
#   Ctrl+C stops backend + Vite together. Logs stream into this terminal.
#   Backend code changes: Ctrl+C and re-run. Frontend changes: hot-reload on save.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GATEWAY_PORT="${KIROCREW_PORT:-6777}"

# Resolve the runtime Python ONCE, with the same precedence dev-backend.sh
# uses (RUNTIME_PYTHON override, else the repo venv), and reuse it for the
# token mint below — hardcoding .venv/bin/python here while dev-backend.sh
# honors RUNTIME_PYTHON would launch the stack fine but silently fail to
# mint the promised auth URL.
RUNTIME_PYTHON="${RUNTIME_PYTHON:-}"
if [ -z "$RUNTIME_PYTHON" ] || [ ! -x "$RUNTIME_PYTHON" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        RUNTIME_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    fi
fi
if [ ! -x "$RUNTIME_PYTHON" ]; then
    echo "[dev] ERROR: cannot find the KiroCrew venv Python at $SCRIPT_DIR/.venv/bin/python."
    echo "[dev] Run 'bash minimal_install.sh' once to set up the venv, then try again."
    exit 1
fi

# Fail fast if something already listens on the gateway port. Otherwise the
# spawned gateway dies on bind while the readiness probe below is satisfied
# by the OLD process — the script reports success, Vite proxies to an
# unrelated (or stale) service, and teardown kills nothing that matters.
if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$GATEWAY_PORT/" 2>/dev/null; then
    echo "[dev] ERROR: something is already listening on :$GATEWAY_PORT."
    echo "[dev] Stop it first (kirocrew stop --port $GATEWAY_PORT) or set KIROCREW_PORT to a free port."
    exit 1
fi
# One EFFECTIVE data home for the whole stack. dev-backend.sh honors a
# caller-provided KIROCREW_HOME, so the token mint below must use the same
# home the backend booted with — hardcoding .kirocrew-dev for one of them
# yields a token signed with a different secret (auth silently fails).
# Same default + relative-path handling as dev-backend.sh.
KIROCREW_HOME="${KIROCREW_HOME:-.kirocrew-dev}"
case "$KIROCREW_HOME" in
    /*) ;;
    *) KIROCREW_HOME="$SCRIPT_DIR/$KIROCREW_HOME" ;;
esac
export KIROCREW_HOME
# Logs live under the dev data home (repo-local, gitignored — same place
# dev-backend.sh keeps its state) rather than the shared world-writable /tmp:
# no cross-user collisions on predictable names, no symlink-planting surface,
# and everything dev-stack-related stays in one directory.
LOG_DIR="$KIROCREW_HOME/logs"
mkdir -p "$LOG_DIR"
BACKEND_LOG="$LOG_DIR/gateway.log"
VITE_LOG="$LOG_DIR/vite.log"

# Teardown: terminate exactly the processes we started, by PID, including
# their descendants (npm spawns node; dev-backend execs python). The previous
# `kill -- -$$` assumed this script is its own process-group leader — true
# from an interactive shell, NOT when invoked from another script or an IDE —
# and silently leaked the stack when that assumption failed. macOS has no
# setsid to force group leadership, so explicit PID-tree kills it is.
PIDS=()
kill_tree() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null); do
        kill_tree "$child"
    done
    kill -TERM "$1" 2>/dev/null || true
}
cleanup() {
    trap - INT TERM EXIT
    local pid
    for pid in "${PIDS[@]}"; do
        kill_tree "$pid"
    done
    wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[dev] starting backend (port $GATEWAY_PORT, home .kirocrew-dev) -> $BACKEND_LOG"
# --no-open: the gateway would otherwise auto-open its OWN url (:6777) — the
# bundled-snapshot surface. The live surface is the Vite url printed below.
#
# NOTE: the gateway's CSRF origin allowlist trusts Vite's DEFAULT port 3000
# only. If something squats on 3000, Vite falls back to 3001+ and every
# mutating request (chat send, etc.) fails with "CSRF check failed" while
# reads and the WS still work. If that happens, either free port 3000 or
# export KIROCREW_ALLOWED_LOOPBACK_PORTS=3000,3001,3002 before running this.
./dev-backend.sh --no-open > "$BACKEND_LOG" 2>&1 &
PIDS+=($!)

# Wait for the gateway to accept connections (up to 90s: first boot loads MCP servers).
for i in $(seq 1 90); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$GATEWAY_PORT/" 2>/dev/null; then
        break
    fi
    if [ "$i" -eq 90 ]; then
        echo "[dev] ERROR: gateway did not come up on :$GATEWAY_PORT — see $BACKEND_LOG"
        exit 1
    fi
    sleep 1
done
echo "[dev] backend up on :$GATEWAY_PORT"

echo "[dev] starting Vite -> $VITE_LOG"
( cd website && KIROCREW_PORT="$GATEWAY_PORT" exec npm run dev > "$VITE_LOG" 2>&1 ) &
PIDS+=($!)

# Vite picks 3000, or the next free port — read the real one from its banner.
VITE_PORT=""
for i in $(seq 1 30); do
    VITE_PORT="$(grep -oE 'Local:.*localhost:[0-9]+' "$VITE_LOG" 2>/dev/null | grep -oE '[0-9]+$' | head -1)"
    [ -n "$VITE_PORT" ] && break
    sleep 1
done
if [ -z "$VITE_PORT" ]; then
    echo "[dev] ERROR: Vite did not report a port — see $VITE_LOG"
    exit 1
fi
echo "[dev] Vite up on :$VITE_PORT"

# Mint a dashboard token against the DEV home and rewrite the URL to the Vite
# port (the Vite proxy forwards the token to the backend and sets the cookie).
# `kirocrew token` can print a SECOND, externally-advertised URL when
# dashboard.url is configured — always take the localhost one.
TOKEN_URL="$(PYTHONPATH="$SCRIPT_DIR/src" \
    "$RUNTIME_PYTHON" -m kiro_crew token --port "$GATEWAY_PORT" 2>/dev/null \
    | grep -oE 'http://localhost:[0-9]+[^ ]*' | head -1)"
if [ -n "$TOKEN_URL" ]; then
    OPEN_URL="$(printf '%s' "$TOKEN_URL" | sed "s/:$GATEWAY_PORT/:$VITE_PORT/")"
    echo ""
    echo "[dev] ============================================================"
    echo "[dev] open:  $OPEN_URL"
    echo "[dev] ============================================================"
    echo ""
    # Open the LIVE (Vite) surface in the browser — macOS `open`, Linux xdg-open.
    # Best-effort: on a headless box the launcher exits nonzero, and under
    # set -e that would tear down the whole stack we just brought up.
    if command -v open >/dev/null 2>&1; then open "$OPEN_URL" || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$OPEN_URL" || true
    fi
else
    echo "[dev] WARNING: could not mint a token automatically. Run:"
    echo "[dev]   KIROCREW_HOME=$KIROCREW_HOME PYTHONPATH=src .venv/bin/python -m kiro_crew token --port $GATEWAY_PORT"
    echo "[dev] then open the URL with :$GATEWAY_PORT swapped to :$VITE_PORT"
fi

echo "[dev] streaming logs (Ctrl+C stops everything)"
tail -f "$BACKEND_LOG" "$VITE_LOG"
