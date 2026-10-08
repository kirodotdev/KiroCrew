#!/bin/bash
# ==============================================================================
# dev-desktop.sh
#
# Build latest FE & BE changes, run the full stack (backend gateway serving the
# frontend React SPA), and spawn the native macOS Electron desktop application.
#
# Usage:
#   ./dev-desktop.sh             # Build, start stack, and launch Mac app (Ctrl+C stops all)
#   ./dev-desktop.sh --skip-build # Skip npm build step for faster re-launch
#   ./dev-desktop.sh --detach    # Launch backend and Mac app in background
#   ./dev-desktop.sh --port 6777 # Use custom port (default: 5476)
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

GATEWAY_PORT="${KIROCREW_PORT:-5476}"
KIROCREW_HOME="${KIROCREW_HOME:-$HOME/.kiro/crew}"
SKIP_BUILD=false
DETACH_MODE=false

# Parse command line options
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port|-p)
            GATEWAY_PORT="$2"
            shift 2
            ;;
        --home)
            KIROCREW_HOME="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --detach|-d)
            DETACH_MODE=true
            shift
            ;;
        --help|-h)
            echo "Usage: ./dev-desktop.sh [options]"
            echo ""
            echo "Options:"
            echo "  --port, -p <port>  Port for backend gateway (default: 5476, or KIROCREW_PORT)"
            echo "  --home <dir>       Data home directory (default: ~/.kiro/crew, or KIROCREW_HOME)"
            echo "  --skip-build       Skip frontend build step to quickly relaunch"
            echo "  --detach, -d       Run backend and Electron detached in background"
            echo "  --help, -h         Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Run './dev-desktop.sh --help' for usage."
            exit 1
            ;;
    esac
done

# Absolutize data home
case "$KIROCREW_HOME" in
    /*) ;;
    *) KIROCREW_HOME="$SCRIPT_DIR/$KIROCREW_HOME" ;;
esac
export KIROCREW_HOME
export KIROCREW_PORT="$GATEWAY_PORT"
export PYTHONPATH="$SCRIPT_DIR/src"

echo "============================================================"
echo "🚀 Kiro Crew Desktop Launcher"
echo "   - Repo Root:   $SCRIPT_DIR"
echo "   - Port:        $GATEWAY_PORT"
echo "   - Data Home:   $KIROCREW_HOME"
echo "============================================================"

# ------------------------------------------------------------------------------
# 0. Preflight checks
# ------------------------------------------------------------------------------
# Resolve Python virtual environment
RUNTIME_PYTHON="${RUNTIME_PYTHON:-}"
if [ -z "$RUNTIME_PYTHON" ] || [ ! -x "$RUNTIME_PYTHON" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        RUNTIME_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        RUNTIME_PYTHON="$(command -v python3)"
    fi
fi

if [ ! -x "$RUNTIME_PYTHON" ]; then
    echo "❌ ERROR: Cannot find Python interpreter at $SCRIPT_DIR/.venv/bin/python."
    echo "   Run 'bash setup.sh' or 'bash minimal_install.sh' first."
    exit 1
fi

# Check Node and npm
if ! command -v npm >/dev/null 2>&1; then
    echo "❌ ERROR: npm not found on PATH. Please ensure Node.js (>=20) is installed."
    exit 1
fi

LOG_DIR="$KIROCREW_HOME/logs"
mkdir -p "$LOG_DIR"
BACKEND_LOG="$LOG_DIR/gateway.log"
ELECTRON_LOG="$LOG_DIR/electron.log"

# Process tracking for cleanup
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
    echo ""
    echo "🛑 Stopping services..."
    local pid
    for pid in "${PIDS[@]}"; do
        kill_tree "$pid"
    done
    wait 2>/dev/null || true
    echo "👋 Stopped."
}

if [ "$DETACH_MODE" = false ]; then
    trap cleanup INT TERM EXIT
fi

# ------------------------------------------------------------------------------
# 1. Build latest FE and BE changes
# ------------------------------------------------------------------------------
echo ""
echo "📦 [1/3] Building latest FE and BE changes..."

# BE: Refresh editable install entry points/metadata if uv exists
if [ -x "$SCRIPT_DIR/.venv/bin/uv" ]; then
    "$SCRIPT_DIR/.venv/bin/uv" pip install -e "$SCRIPT_DIR" --no-deps -q 2>/dev/null || true
fi

# FE: Build Vite SPA and stage into src/kiro_crew/static/dist
if [ "$SKIP_BUILD" = true ]; then
    echo "⏩ Skipping frontend build (--skip-build requested)."
else
    if [ ! -d "$SCRIPT_DIR/website/node_modules" ]; then
        echo "   📥 Installing website dependencies..."
        ( cd "$SCRIPT_DIR/website" && npm install --no-audit --no-fund )
    fi

    echo "   🔨 Compiling website frontend (React + Vite)..."
    ( cd "$SCRIPT_DIR/website" && npm run build )

    echo "   🚚 Staging frontend bundle into src/kiro_crew/static/dist..."
    rm -rf "$SCRIPT_DIR/src/kiro_crew/static/dist"
    mkdir -p "$SCRIPT_DIR/src/kiro_crew/static"
    cp -R "$SCRIPT_DIR/website/dist" "$SCRIPT_DIR/src/kiro_crew/static/dist"

    if [ ! -d "$SCRIPT_DIR/website/electron/node_modules" ]; then
        echo "   📥 Installing electron dependencies..."
        ( cd "$SCRIPT_DIR/website/electron" && npm install --no-audit --no-fund )
    fi
fi

# ------------------------------------------------------------------------------
# 2. Run the full stack - Backend & Frontend Gateway
# ------------------------------------------------------------------------------
echo ""
echo "⚙️  [2/3] Starting full stack (Backend API + Frontend SPA)..."

# Free port if already occupied by an old gateway
if lsof -i :"$GATEWAY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "   🔄 Port $GATEWAY_PORT is busy; stopping existing process to apply changes..."
    "$RUNTIME_PYTHON" -m kiro_crew stop --port "$GATEWAY_PORT" 2>/dev/null || true
    for i in $(seq 1 5); do
        if ! lsof -i :"$GATEWAY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done

    # Force kill if still holding port
    EXISTING_PID="$(lsof -ti :"$GATEWAY_PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
    if [ -n "$EXISTING_PID" ]; then
        kill -TERM "$EXISTING_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$EXISTING_PID" 2>/dev/null || true
    fi
fi

echo "   🌐 Launching gateway on port $GATEWAY_PORT (logging to $BACKEND_LOG)..."
"$RUNTIME_PYTHON" -m kiro_crew gateway --port "$GATEWAY_PORT" --no-open > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!
PIDS+=($BACKEND_PID)

# Wait for gateway readiness probe
GATEWAY_READY=false
for i in $(seq 1 60); do
    HTTP_CODE="$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$GATEWAY_PORT/api/status" 2>/dev/null || true)"
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "403" ]; then
        GATEWAY_READY=true
        break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "❌ ERROR: Backend gateway exited unexpectedly. See logs at: $BACKEND_LOG"
        exit 1
    fi
    sleep 1
done

if [ "$GATEWAY_READY" != true ]; then
    echo "❌ ERROR: Gateway failed to respond within 60s. See logs at: $BACKEND_LOG"
    exit 1
fi
echo "   ✅ Gateway ready at http://localhost:$GATEWAY_PORT"

# ------------------------------------------------------------------------------
# 3. Spawn the Mac Application (Electron)
# ------------------------------------------------------------------------------
echo ""
echo "🖥️  [3/3] Spawning Mac Application..."

if [ "$DETACH_MODE" = true ]; then
    (
        cd "$SCRIPT_DIR/website/electron"
        export KIROCREW_PORT="$GATEWAY_PORT"
        export KIROCREW_HOME="$KIROCREW_HOME"
        nohup npx electron . > "$ELECTRON_LOG" 2>&1 &
    )
    echo ""
    echo "============================================================"
    echo "🎉 Full stack and Mac Application running in background!"
    echo "   - Gateway (BE+FE): http://localhost:$GATEWAY_PORT"
    echo "   - Backend PID:     $BACKEND_PID"
    echo "   - Backend Logs:    $BACKEND_LOG"
    echo "   - Electron Logs:   $ELECTRON_LOG"
    echo "   - To stop:         kirocrew stop --port $GATEWAY_PORT"
    echo "============================================================"
    exit 0
else
    (
        cd "$SCRIPT_DIR/website/electron"
        export KIROCREW_PORT="$GATEWAY_PORT"
        export KIROCREW_HOME="$KIROCREW_HOME"
        exec npx electron .
    ) &
    ELECTRON_PID=$!
    PIDS+=($ELECTRON_PID)

    echo ""
    echo "============================================================"
    echo "🎉 Kiro Crew Mac Application is running!"
    echo "   - Gateway (BE+FE): http://localhost:$GATEWAY_PORT"
    echo "   - Backend Logs:    $BACKEND_LOG"
    echo "   - Close Mac app or press Ctrl+C to stop the stack"
    echo "============================================================"

    # Wait for the Electron application to close
    wait "$ELECTRON_PID" 2>/dev/null || true
fi
