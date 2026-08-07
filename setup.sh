#!/bin/sh
# KiroCrew first-time setup script (public build)
# Usage: source setup.sh   (bash or zsh), or: bash setup.sh
#
# Sets up KiroCrew using only public tooling:
#   1. Node.js (via ensure-node.sh)
#   2. Optional tools (git-lfs, ffmpeg for voice)
#   3. Agent backend: claude-agent-acp (npm i -g)
#   4. Build frontend (npm/vite) + backend (pip)
#   5. PATH config
#   6. Agent config (kirocrew setup --agent-only)

# Resolve script directory (works in bash and zsh, sourced or executed)
if [ -n "$BASH_SOURCE" ]; then
    _kirocrew_dir="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
elif [ -n "$ZSH_VERSION" ]; then
    _kirocrew_dir="$(cd "$(dirname "${(%):-%x}")" && pwd)"
else
    _kirocrew_dir="$(pwd)"
fi
cd "$_kirocrew_dir" || return 1

ACP_NPM_PKG="@agentclientprotocol/claude-agent-acp"

echo "👻 KiroCrew Setup"
echo ""

# ── Ensure PATH includes common install locations ──
[ -d "$HOME/.local/bin" ] && export PATH="$HOME/.local/bin:$PATH"
if [ -s "$HOME/.nvm/nvm.sh" ]; then
    export NVM_DIR="$HOME/.nvm"
    # shellcheck disable=SC1091
    . "$NVM_DIR/nvm.sh"
fi

# ── Helper ──

_check() {
    command -v "$1" >/dev/null 2>&1
}

# ── 1. Python ──

echo "── Step 1: Python ──"
_py=""
for _candidate in python3.12 python3.11 python3.10 python3; do
    if _check "$_candidate" && "$_candidate" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
        _py="$_candidate"
        break
    fi
done
if [ -z "$_py" ]; then
    echo "  ❌ Python 3.10+ required but not found."
    echo "     Install Python 3.10+ (https://www.python.org/downloads/) and re-run."
    cd - > /dev/null 2>&1
    return 1 2>/dev/null || exit 1
fi
echo "  ✅ $($_py --version 2>&1) ($(which "$_py"))"
echo ""

# ── 2. Node.js + optional tools ──

echo "── Step 2: Dependencies ──"

# Node.js (>= 18 recommended for the website build)
if [ -x "$_kirocrew_dir/ensure-node.sh" ]; then
    bash "$_kirocrew_dir/ensure-node.sh"
    # Re-source managers so newly-installed node lands on PATH
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
    if [ -f "$HOME/.local/bin/mise" ]; then
        eval "$("$HOME/.local/bin/mise" activate bash 2>/dev/null)" 2>/dev/null || true
    elif command -v mise >/dev/null 2>&1; then
        eval "$(mise activate bash 2>/dev/null)" 2>/dev/null || true
    fi
    # On a host where no version manager can serve a supported Node -- Amazon
    # Linux 2, whose glibc 2.26 cannot load the official builds -- ensure-node.sh
    # installs a private toolchain that neither nvm nor mise owns, so the
    # re-sourcing above cannot surface it. It records the directory it settled on;
    # consume that marker the way the Makefile and kiro_crew.env.node_bin_dirs()
    # already do. Without this the checks below miss the node just installed and
    # the frontend build plus the agent-backend install are skipped.
    _nbd="$(cat "${KIROCREW_HOME:-$HOME/.kiro/crew}/node-bin-dir" 2>/dev/null || true)"
    if [ -n "$_nbd" ] && [ -x "$_nbd/node" ]; then
        export PATH="$_nbd:$PATH"
    fi
    unset _nbd
    if _check node; then
        echo "  ✅ node ($(which node))"
    else
        echo "  ⚠️  node not found after ensure-node.sh — run: bash ensure-node.sh"
    fi
elif _check node; then
    echo "  ✅ node ($(which node))"
else
    echo "  ⚠️  node not found — run: bash ensure-node.sh"
fi

# Git LFS: optional, used if any LFS-tracked assets are added
if ! git lfs version >/dev/null 2>&1; then
    if [ "$(uname)" = "Darwin" ] && _check brew; then
        echo "  → Installing git-lfs..."
        brew install git-lfs >/dev/null 2>&1 || true
        if git lfs version >/dev/null 2>&1; then
            git lfs install >/dev/null 2>&1
            echo "  ✅ git-lfs installed and initialized"
        else
            echo "  ⚠️  git-lfs install failed — run manually: brew install git-lfs && git lfs install"
        fi
    else
        echo "  ⚠️  git-lfs not found (optional) — install via your package manager if needed"
    fi
else
    if ! git config --global filter.lfs.smudge >/dev/null 2>&1; then
        git lfs install >/dev/null 2>&1 && echo "  ✅ git-lfs ($(git lfs version 2>/dev/null | head -1))" \
            || echo "  ⚠️  git-lfs filter init failed — run: git lfs install"
    else
        echo "  ✅ git-lfs ($(git lfs version 2>/dev/null | head -1))"
    fi
fi

# ffmpeg: needed for voice input (whisper) and MP3 stitching
if ! _check ffmpeg; then
    if [ "$(uname)" = "Darwin" ] && _check brew; then
        echo "  → Installing ffmpeg..."
        brew install ffmpeg >/dev/null 2>&1 || true
        _check ffmpeg && echo "  ✅ ffmpeg installed" \
            || echo "  ⚠️  ffmpeg install failed — run manually: brew install ffmpeg"
    else
        echo "  ⚠️  ffmpeg not found (optional, for voice) — install via your package manager"
    fi
fi
echo ""

# ── 3. Agent backend (claude-agent-acp) ──

echo "── Step 3: Agent Backend ──"
if _check claude-agent-acp; then
    echo "  ✅ claude-agent-acp ($(which claude-agent-acp))"
elif _check npm; then
    echo "  → Installing $ACP_NPM_PKG via npm..."
    if npm install -g "$ACP_NPM_PKG" >/dev/null 2>&1; then
        echo "  ✅ claude-agent-acp installed"
    else
        echo "  ⚠️  npm i -g $ACP_NPM_PKG failed — install it manually before first run"
    fi
else
    echo "  ⚠️  npm not found — install the agent backend later:"
    echo "       npm i -g $ACP_NPM_PKG"
fi
echo "  (kiro-cli is an optional alternative backend: https://kiro.dev/docs/cli/installation)"
echo ""

# ── 4. Build (npm/vite frontend + pip backend) ──

echo "── Step 4: Build ──"

# Frontend: vite emits to website/dist; stage into src/kiro_crew/static/dist
if _check node && [ -d "$_kirocrew_dir/website" ]; then
    echo "→ Building frontend (website/)..."
    if (cd "$_kirocrew_dir/website" \
            && { [ -f package-lock.json ] && npm ci --no-audit --no-fund --loglevel=error \
                 || npm install --no-audit --no-fund --loglevel=error; } \
            && npm run build); then
        _dist_src="$_kirocrew_dir/website/dist"
        _dist_dst="$_kirocrew_dir/src/kiro_crew/static/dist"
        if [ -d "$_dist_src" ]; then
            rm -rf "$_dist_dst"
            mkdir -p "$(dirname "$_dist_dst")"
            cp -R "$_dist_src" "$_dist_dst"
            echo "  ✅ Frontend built and staged → src/kiro_crew/static/dist"
        else
            echo "  ⚠️  website/dist not found after build — dashboard will use legacy fallback"
        fi
    else
        echo "  ⚠️  Frontend build failed — dashboard will use legacy fallback"
    fi
else
    echo "  ⚠️  Skipping frontend build (node or website/ not available)"
fi

# Backend: venv + pip install -e .
_venv="$_kirocrew_dir/.venv"
if [ ! -d "$_venv" ] || [ ! -x "$_venv/bin/python" ]; then
    echo "→ Creating virtual environment..."
    "$_py" -m venv "$_venv" || {
        echo "  ❌ Failed to create venv"
        cd - > /dev/null 2>&1
        return 1 2>/dev/null || exit 1
    }
fi
echo "→ Installing kirocrew (pip)..."
"$_venv/bin/pip" install --upgrade pip setuptools wheel -q 2>/dev/null || true
if KIROCREW_SKIP_FRONTEND=1 "$_venv/bin/pip" install -e "$_kirocrew_dir" -q; then
    echo "  ✅ Build succeeded"
    # Record install method for tooling that branches on it
    echo "pip" > "$_kirocrew_dir/.install-method"
else
    echo "  ❌ pip install failed"
    cd - > /dev/null 2>&1
    return 1 2>/dev/null || exit 1
fi
# Symlink CLI so `kirocrew` works on PATH
mkdir -p "$HOME/.local/bin"
ln -sf "$_venv/bin/kirocrew" "$HOME/.local/bin/kirocrew"
if _check kirocrew; then
    echo "  ✅ kirocrew command available ($(which kirocrew))"
else
    echo "  ✅ kirocrew symlinked → ~/.local/bin/kirocrew (restart shell or fix PATH in Step 5)"
fi
echo ""

# ── 5. PATH ──

echo "── Step 5: PATH ──"
export PATH="$HOME/.local/bin:$PATH"
echo "→ ~/.local/bin added to PATH"

_path_line="export PATH=\"\$HOME/.local/bin:\$PATH\""

_add_to_rc() {
    _rc="$1"
    if [ ! -f "$_rc" ]; then return; fi
    if grep -qF "$_path_line" "$_rc" 2>/dev/null; then
        echo "  ✅ Already in $_rc"
        return
    fi
    # Remove any old KiroCrew PATH entry and replace with current
    if grep -qF "KiroCrew" "$_rc" 2>/dev/null; then
        sed -i.bak '/# KiroCrew/d;/KiroCrew.*bin/d;/\.local\/bin/d' "$_rc"
        rm -f "${_rc}.bak"
    fi
    printf "→ Add ~/.local/bin to PATH permanently in %s? [Y/n] " "$_rc"
    read _answer
    case "${_answer:-Y}" in
        [Yy]*)
            echo "" >> "$_rc"
            echo "# KiroCrew" >> "$_rc"
            echo "$_path_line" >> "$_rc"
            echo "  ✅ Added to $_rc"
            ;;
    esac
}

_add_to_rc "$HOME/.bashrc"
_add_to_rc "$HOME/.zshrc"
echo ""

# ── 6. Install agent config ──

echo "── Step 6: Agent Config ──"
echo "→ Installing agent config..."
KIROCREW_PROJECT_DIR="$_kirocrew_dir" kirocrew setup --agent-only \
    || echo "  ⚠️  kirocrew setup --agent-only failed (run manually later)"

echo ""
echo "👻 Setup complete!"
echo ""
echo "  kirocrew doctor     # verify everything"
echo "  kirocrew gateway    # start dashboard + gateway"
echo "  kirocrew chat       # interactive chat"
echo ""
echo "  Optional — local vector memory (embeddings):"
echo "    Install ollama (https://ollama.com), then: ollama pull qwen3-embedding:0.6b"

# Cleanup
cd - > /dev/null 2>&1
