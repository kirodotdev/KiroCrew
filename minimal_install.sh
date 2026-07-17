#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# KiroCrew — Minimal OSS Quickstart
#
# The canonical public install. Builds KiroCrew from a local clone using
# only public tooling: python3 + pip (backend) and npm + vite (frontend).
# No Brazil, no Builder Toolbox, no internal registries.
#
# Run from inside a cloned repo:
#   git clone https://github.com/kirodotdev/KiroCrew.git
#   cd kirocrew
#   bash minimal_install.sh
#
# Prerequisites: Python 3.10+, Node.js 18+, npm, git
# Optional:
#   --voice    also install voice extras (pip install -e .[voice])
#   ollama     for local vector memory (see step "Embeddings" below)
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Parse arguments ──
WITH_VOICE=0
for _arg in "$@"; do
    case "$_arg" in
        --voice) WITH_VOICE=1 ;;
        *) echo "ERROR: unknown argument '$_arg'" >&2; exit 1 ;;
    esac
done

# Repo root = directory containing this script.
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

has() { command -v "$1" >/dev/null 2>&1; }
die() { echo "ERROR: $1" >&2; exit 1; }

# ── Preflight checks ──
has git || die "git not found"

_py=""
for c in python3.12 python3.11 python3.10 python3; do
    if has "$c" && "$c" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
        _py="$c"; break
    fi
done
[ -n "$_py" ] || die "Python 3.10+ not found. Install Python 3.10 or newer and re-run."

has node || die "Node.js not found. Install Node.js 18+ (https://nodejs.org) and re-run."
has npm  || die "npm not found. Install Node.js 18+ (https://nodejs.org) and re-run."

echo "🐾 KiroCrew — minimal install"
echo "  Repo:    $REPO_DIR"
echo "  Python:  $("$_py" --version 2>&1)"
echo "  Node:    $(node --version)"
echo ""

# ── 1. Frontend build (npm + vite) ──
# Vite emits to website/dist; setup.py copies src/kiro_crew/static/dist into
# the package at install time, so we stage the build there first.
if [ -d "$REPO_DIR/website" ]; then
    echo "→ Building frontend (website/)…"
    (
        cd "$REPO_DIR/website"
        if [ -f package-lock.json ]; then
            npm ci --no-audit --no-fund --loglevel=error
        else
            npm install --no-audit --no-fund --loglevel=error
        fi
        npm run build
    ) || die "Frontend build failed. Fix the npm/vite error above and re-run."
    # Stage built assets where setup.py expects them.
    _dist_src="$REPO_DIR/website/dist"
    _dist_dst="$REPO_DIR/src/kiro_crew/static/dist"
    if [ -d "$_dist_src" ]; then
        rm -rf "$_dist_dst"
        mkdir -p "$(dirname "$_dist_dst")"
        cp -R "$_dist_src" "$_dist_dst"
        echo "✓ Frontend built and staged → src/kiro_crew/static/dist"
    else
        echo "⚠ website/dist not found after build — dashboard assets may be missing"
    fi
else
    echo "⚠ website/ not found — skipping frontend build"
fi
echo ""

# ── 2. Python venv + package install (pip) ──
_venv="$REPO_DIR/.venv"
if [ ! -d "$_venv" ] || [ ! -x "$_venv/bin/python" ]; then
    echo "→ Creating virtual environment…"
    "$_py" -m venv "$_venv" || die "Failed to create venv. Try: $_py -m pip install --user virtualenv"
fi

echo "→ Installing kirocrew (pip)…"
"$_venv/bin/pip" install --upgrade pip setuptools wheel -q \
    || die "Failed to upgrade pip/setuptools/wheel"

# Frontend is already built and staged above; tell setup.py not to rebuild it.
if [ "$WITH_VOICE" -eq 1 ]; then
    echo "  (including voice extras)"
    KIROCREW_SKIP_FRONTEND=1 "$_venv/bin/pip" install -e "$REPO_DIR"'[voice]' \
        || die "pip install failed"
else
    KIROCREW_SKIP_FRONTEND=1 "$_venv/bin/pip" install -e "$REPO_DIR" \
        || die "pip install failed"
fi

"$_venv/bin/python" -c "import aiohttp" 2>/dev/null \
    || die "Install succeeded but aiohttp not importable — dependencies missing"
echo "✓ Python package installed"
echo ""

# ── 3. Agent backend (claude-agent-acp) ──
# The default agent backend is the public ACP adapter. kiro-cli is optional.
echo "→ Checking agent backend (claude-agent-acp)…"
if has claude-agent-acp; then
    echo "✓ claude-agent-acp already on PATH"
elif has npm; then
    echo "  Installing @agentclientprotocol/claude-agent-acp via npm…"
    npm install -g @agentclientprotocol/claude-agent-acp >/dev/null 2>&1 \
        && echo "✓ claude-agent-acp installed" \
        || echo "⚠ npm i -g @agentclientprotocol/claude-agent-acp failed — install it manually before running"
else
    echo "⚠ npm not found — install the agent backend later:"
    echo "    npm i -g @agentclientprotocol/claude-agent-acp"
fi
echo ""

# ── 4. Symlink CLI (no shell rc modification) ──
mkdir -p "$HOME/.local/bin"
ln -sf "$_venv/bin/kirocrew" "$HOME/.local/bin/kirocrew"
echo "✓ Symlinked kirocrew → ~/.local/bin/kirocrew"
echo ""

# ── Done ──
echo "🐾 KiroCrew installed!"
echo ""
echo "  Next steps:"
echo "    1. Run setup:    $HOME/.local/bin/kirocrew setup"
echo "    2. Start it:     $HOME/.local/bin/kirocrew gateway"
echo "       (or add ~/.local/bin to PATH and just run: kirocrew gateway)"
echo ""
echo "  Optional — local vector memory (embeddings):"
echo "    Install ollama (https://ollama.com), then:"
echo "      ollama pull qwen3-embedding:0.6b"
echo ""
