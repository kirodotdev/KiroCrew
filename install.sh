#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# KiroCrew Installer
# One-command setup for macOS and Linux. Public build — no Brazil workspace,
# no internal tooling required. Uses python3/pip (backend) + npm/vite (frontend).
#
# Usage (from a local clone):
#   git clone https://github.com/kirodotdev/KiroCrew.git
#   cd kirocrew
#   bash install.sh
#
# Options:
#   --voice   Install optional voice extras (pip install -e .[voice])
#   --mise    Use mise (https://mise.jdx.dev) for Python and Node.js
#             instead of system package managers. Does not modify your
#             global mise config.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# Isolate the managed venv from any inherited PYTHONPATH/PYTHONHOME. If the
# caller's environment points these at foreign site-packages (e.g. another
# app's interpreter on a different Python version), pip treats those packages
# as already satisfied and silently skips installing our dependencies into the
# venv -- producing a broken install (ImportError: No module named 'aiohttp').
unset PYTHONPATH PYTHONHOME

# ── Parse arguments ──
USE_MISE=0
WITH_VOICE=0
for _arg in "$@"; do
    case "$_arg" in
        --mise)  USE_MISE=1 ;;
        --voice) WITH_VOICE=1 ;;
        *) echo "Error: unknown argument '$_arg'" >&2; exit 1 ;;
    esac
done

# ── Constants ──
# Repo root = directory containing this script (run from a local clone).
KIROCREW_APP_DIR="$(cd "$(dirname "$0")" && pwd)"
# Data home: honor KIROCREW_HOME, else the current default ~/.kiro/crew (NOT the
# pre-move ~/.kirocrew, which is not the data home).
KIROCREW_DATA_DIR="${KIROCREW_HOME:-$HOME/.kiro/crew}"
NODE_VERSION="24"
# Minimum Node major the frontend build actually supports. Defined here
# (not just at the post-install check) because DETECTION consults it: a
# pre-existing but too-old node must not short-circuit the install ladder.
NODE_MIN_MAJOR=22
PYTHON_VERSION="3.12"
KIROCREW_PORT="${KIROCREW_PORT:-5476}"
ACP_NPM_PKG="@agentclientprotocol/claude-agent-acp"

# ── Colors & Formatting ──
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    BOLD=$(tput bold)
    DIM=$(tput dim)
    RESET=$(tput sgr0)
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    BLUE=$(tput setaf 4)
    MAGENTA=$(tput setaf 5)
    CYAN=$(tput setaf 6)
else
    BOLD="" DIM="" RESET=""
    RED="" GREEN="" YELLOW="" BLUE="" MAGENTA="" CYAN=""
fi

# ── UI Helpers ──
_STEP=0
_TOTAL_STEPS=5

banner() {
    echo ""
    echo "${MAGENTA}${BOLD}"
    cat << 'BANNER'
   _  ___            ___
  | |/ (_)_ _ ___   / __|_ _ _____ __ __
  | ' <| | '_/ _ \ | (__| '_/ -_) V  V /
  |_|\_\_|_| \___/  \___|_| \___|\_/\_/
BANNER
    echo "${RESET}"
    echo "  ${DIM}Your personal AI agent${RESET}"
    echo "  ${DIM}────────────────────────────────────────${RESET}"
    echo ""
}

step() {
    _STEP=$((_STEP + 1))
    echo ""
    echo "  ${BLUE}${BOLD}[$_STEP/$_TOTAL_STEPS]${RESET} ${BOLD}$1${RESET}"
    echo "  ${DIM}$(printf '%.0s─' $(seq 1 50))${RESET}"
}

info()    { echo "  ${DIM}→${RESET} $1"; }
ok()      { echo "  ${GREEN}✓${RESET} $1"; }
warn()    { echo "  ${YELLOW}⚠${RESET} $1"; }
fail()    { echo "  ${RED}✗${RESET} $1"; }
detail()  { echo "    ${DIM}$1${RESET}"; }

die() {
    echo ""
    fail "$1"
    echo ""
    echo "  ${DIM}Need help? See the README or open an issue on GitHub.${RESET}"
    echo ""
    exit 1
}

spinner() {
    local pid=$1 msg=$2
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${DIM}%s${RESET} %s" "${frames:i%${#frames}:1}" "$msg"
        i=$((i + 1))
        sleep 0.1
    done
    printf "\r"
}

has() { command -v "$1" >/dev/null 2>&1; }

# True only when a usable node is on PATH: present AND at or above the build's
# supported floor. `has node` alone was not enough -- Amazon Linux 2023 ships
# node 18, so the "already installed" branch matched, every install branch
# (including nvm) was skipped, and the frontend build then ran under an
# unsupported node and produced no usable dist. A broken binary reports v0 and
# fails the comparison, which is the intended answer.
node_supported() {
    has node || return 1
    _n="$( { node --version 2>/dev/null || echo v0; } | sed 's/^v//' | cut -d. -f1)"
    [ -n "$_n" ] && [ "$_n" -ge "$NODE_MIN_MAJOR" ] 2>/dev/null
}

# ── Pre-flight ──

banner

echo "  ${DIM}Repo directory:${RESET}     $KIROCREW_APP_DIR"
echo "  ${DIM}Data directory:${RESET}     $KIROCREW_DATA_DIR"
echo "  ${DIM}Platform:${RESET}           $(uname -s) $(uname -m)"
echo ""

if [ ! -f "$KIROCREW_APP_DIR/pyproject.toml" ]; then
    die "Run this from inside a KiroCrew clone (pyproject.toml not found in $KIROCREW_APP_DIR).
     git clone https://github.com/kirodotdev/KiroCrew.git && cd kirocrew && bash install.sh"
fi

# ══════════════════════════════════════════════════════════════════════
# Step 1: Dependencies
# ══════════════════════════════════════════════════════════════════════
step "Dependencies"

# ── mise runtimes (opt-in via --mise) ──
if [ "$USE_MISE" -eq 1 ]; then
    if ! has mise; then
        info "Installing mise…"
        curl -fsSL https://mise.run | sh || true
        export PATH="$HOME/.local/bin:$PATH"
        has mise || die "mise installation failed. Install manually: https://mise.jdx.dev/installing-mise.html"
    fi
    ok "mise $("$HOME/.local/bin/mise" version 2>/dev/null || mise version)"

    _mise_log=$(mktemp)

    info "Installing Python $PYTHON_VERSION via mise…"
    mise install "python@$PYTHON_VERSION" -y 2>"$_mise_log" \
        || { cat "$_mise_log" >&2; die "Failed to install Python $PYTHON_VERSION via mise"; }
    _py_prefix="$(mise where "python@$PYTHON_VERSION")" \
        || die "mise installed Python but 'mise where' failed — check mise status"
    [ -n "$_py_prefix" ] || die "mise where returned an empty path for Python"
    [ -x "$_py_prefix/bin/python3" ] \
        || die "Python binary not found at $_py_prefix/bin/python3"
    _py="$_py_prefix/bin/python3"
    _py_ver=$("$_py" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
    ok "Python $_py_ver ($_py) [mise]"

    info "Installing Node.js $NODE_VERSION via mise…"
    mise install "node@$NODE_VERSION" -y 2>"$_mise_log" \
        || { cat "$_mise_log" >&2; die "Failed to install Node.js $NODE_VERSION via mise"; }
    _node_prefix="$(mise where "node@$NODE_VERSION")" \
        || die "mise installed Node.js but 'mise where' failed — check mise status"
    [ -n "$_node_prefix" ] || die "mise where returned an empty path for Node.js"
    [ -x "$_node_prefix/bin/node" ] \
        || die "Node binary not found at $_node_prefix/bin/node"
    export PATH="$_node_prefix/bin:$_py_prefix/bin:$PATH"
    ok "Node.js $(node --version) [mise]"

    rm -f "$_mise_log"
fi

# ── Python ──
if [ "$USE_MISE" -eq 0 ]; then
info "Checking Python…"
_py=""
_find_python() {
    for _candidate in python3.12 python3.11 python3.10 python3 python; do
        if has "$_candidate"; then
            _ok=$("$_candidate" -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>/dev/null || echo "0")
            if [ "$_ok" = "1" ]; then
                _ver=$("$_candidate" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')" 2>/dev/null)
                _py="$_candidate"
                ok "Python $_ver ($( which "$_candidate" ))"
                return 0
            fi
        fi
    done
    return 1
}

if ! _find_python; then
    # Try to install automatically
    if [ "$(uname)" = "Darwin" ]; then
        info "Installing Python via Xcode Command Line Tools…"
        xcode-select --install 2>/dev/null || true
        sleep 2
        if ! _find_python; then
            warn "Xcode CLT install may be in progress (check the popup)"
            detail "After it finishes, re-run this installer"
            die "Python 3.10+ required. Waiting for Xcode CLT to finish installing."
        fi
    elif has apt-get; then
        info "Installing Python 3 via apt…"
        sudo apt-get update -qq >/dev/null 2>&1
        sudo apt-get install -y python3 python3-pip python3-venv >/dev/null 2>&1
        _find_python || die "Python install failed. Run: sudo apt-get install -y python3 python3-venv"
    elif has dnf; then
        info "Installing Python 3 via dnf…"
        sudo dnf install -y python3 python3-pip >/dev/null 2>&1
        _find_python || die "Python install failed. Run: sudo dnf install -y python3"
    elif has yum; then
        info "Installing Python 3 via yum…"
        sudo yum install -y python3 python3-pip >/dev/null 2>&1
        _find_python || die "Python install failed. Run: sudo yum install -y python3"
    elif has brew; then
        info "Installing Python 3 via Homebrew…"
        brew install python@3.12 >/dev/null 2>&1 || true
        _find_python || die "Python install failed. Run: brew install python@3.12"
    else
        die "Python 3.10+ required but not found and no package manager detected.
     Install Python 3.10+ manually (https://www.python.org/downloads/) and re-run."
    fi
fi
fi # USE_MISE -eq 0 (Python)

# ── Git ──
if has git; then
    ok "git ($( git --version | head -1 ))"
else
    die "git not found. Install it first."
fi

# ── Node.js ──
if [ "$USE_MISE" -eq 0 ]; then
info "Checking Node.js…"
if node_supported; then
    _node_ver=$(node --version 2>/dev/null || echo "v0")
    ok "Node.js $_node_ver ($( which node ))"
elif has node; then
    # Present but below the floor: say so, then fall through to the install
    # ladder below rather than building against it.
    info "Node.js $(node --version 2>/dev/null || echo v0) is below the supported floor (>= $NODE_MIN_MAJOR) — installing a supported Node…"
    if has apt-get; then
        sudo apt-get install -y nodejs npm >/dev/null 2>&1 || true
    elif has dnf; then
        sudo dnf install -y nodejs >/dev/null 2>&1 || true
    elif has brew; then
        brew install node >/dev/null 2>&1 || true
    fi
    if ! node_supported; then
        # Distro package still too old (the common case on AL2023/Debian):
        # nvm is the only channel that reliably provides a current Node.
        info "Installing Node.js $NODE_VERSION via nvm…"
        if [ ! -d "$HOME/.nvm" ]; then
            curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash 2>/dev/null
        fi
        export NVM_DIR="$HOME/.nvm"
        # shellcheck disable=SC1091
        [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
        nvm install "$NODE_VERSION" >/dev/null 2>&1
        nvm alias default "$NODE_VERSION" >/dev/null 2>&1
    fi
    node_supported \
        && ok "Node.js $(node --version) now active" \
        || warn "Node.js is still below v$NODE_MIN_MAJOR — the frontend build will fail"
elif has apt-get; then
    info "Installing nodejs via apt…"
    sudo apt-get install -y nodejs npm >/dev/null 2>&1
    has node && ok "Node.js $(node --version) installed via apt" || warn "Node.js install failed — run: sudo apt-get install -y nodejs npm"
elif has dnf; then
    info "Installing nodejs via dnf…"
    sudo dnf install -y nodejs >/dev/null 2>&1
    has node && ok "Node.js $(node --version) installed via dnf" || warn "Node.js install failed — run: sudo dnf install -y nodejs"
elif has brew; then
    info "Installing Node.js via Homebrew…"
    brew install node >/dev/null 2>&1
    has node && ok "Node.js $(node --version) installed via brew" || warn "Node.js install failed — run: brew install node"
else
    info "Installing Node.js via nvm…"
    if [ ! -d "$HOME/.nvm" ]; then
        curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash 2>/dev/null
    fi
    export NVM_DIR="$HOME/.nvm"
    # shellcheck disable=SC1091
    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
    nvm install "$NODE_VERSION" >/dev/null 2>&1
    nvm alias default "$NODE_VERSION" >/dev/null 2>&1
    if has node; then
        ok "Node.js $(node --version) installed via nvm"
    else
        warn "Node.js install failed — frontend build will be skipped"
        detail "Install manually: nvm install $NODE_VERSION"
    fi
fi
fi # USE_MISE -eq 0 (Node.js)

# The apt/dnf/brew branches above accept whatever Node the distro ships, which
# can be older than the supported floor (Debian/Ubuntu in particular). Catch
# that here rather than as a confusing frontend-build failure later.
if has node; then
    # `|| echo v0` keeps a broken node binary (loader error) from killing the
    # installer under `set -e`; v0 then trips the floor warning below.
    _node_major="$( { node --version 2>/dev/null || echo v0; } | sed 's/^v//' | cut -d. -f1)"
    if [ -n "$_node_major" ] && [ "$_node_major" -lt "$NODE_MIN_MAJOR" ] 2>/dev/null; then
        warn "Node.js v$_node_major is below the supported floor (>= $NODE_MIN_MAJOR) — the frontend build will fail"
        detail "Install Node.js $NODE_VERSION (LTS): https://nodejs.org or 'nvm install $NODE_VERSION'"
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# Step 2: Agent Backend (claude-agent-acp)
# ══════════════════════════════════════════════════════════════════════
step "Agent Backend"

# The default agent backend is the public ACP adapter, run via Node.
# kiro-cli is optional and not installed here.
if has claude-agent-acp; then
    ok "claude-agent-acp ($( which claude-agent-acp ))"
elif has npm; then
    info "Installing $ACP_NPM_PKG via npm…"
    if npm install -g "$ACP_NPM_PKG" >/dev/null 2>&1; then
        ok "claude-agent-acp installed"
    else
        warn "Global npm install failed — install it manually before first run:"
        detail "npm i -g $ACP_NPM_PKG"
    fi
else
    warn "npm not available — install the agent backend later:"
    detail "npm i -g $ACP_NPM_PKG"
fi
detail "kiro-cli is an optional alternative backend (https://kiro.dev/docs/cli/installation)"

# ══════════════════════════════════════════════════════════════════════
# Step 3: Build
# ══════════════════════════════════════════════════════════════════════
step "Build"

cd "$KIROCREW_APP_DIR"

# ── Frontend (npm + vite) ──
# Vite emits to website/dist; we stage it into src/kiro_crew/static/dist
# where setup.py copies it into the package at install time.
if has node && [ -d "$KIROCREW_APP_DIR/website" ]; then
    info "Building frontend (website/)…"
    _fe_log="$(mktemp)"
    (
        cd "$KIROCREW_APP_DIR/website" &&
        if [ -f package-lock.json ]; then
            npm ci --no-audit --no-fund --loglevel=error 2>"$_fe_log"
        else
            npm install --no-audit --no-fund --loglevel=error 2>"$_fe_log"
        fi &&
        # Raise V8's heap ceiling for the bundle build. The default (~2 GB on
        # 64-bit) is not enough for this app's 6k+ module graph, and the OOM
        # surfaces as a build that dies without a clear cause -- leaving no
        # dist/ and a "Dashboard HTML not found" page at first launch.
        NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=8192}" npm run build 2>>"$_fe_log"
    ) &
    spinner $! "Installing npm packages & building React app…"
    _fe_ok=0
    if wait $!; then
        _dist_src="$KIROCREW_APP_DIR/website/dist"
        _dist_dst="$KIROCREW_APP_DIR/src/kiro_crew/static/dist"
        if [ -d "$_dist_src" ]; then
            rm -rf "$_dist_dst"
            mkdir -p "$(dirname "$_dist_dst")"
            cp -R "$_dist_src" "$_dist_dst"
            ok "Frontend built and staged → src/kiro_crew/static/dist"
            _fe_ok=1
        fi
    fi
    if [ "$_fe_ok" != "1" ]; then
        # The build did not produce a usable bundle. For a local CLI install this is
        # non-fatal — the dashboard falls back to a legacy page and the CLI still
        # works. For a cloud crew the dashboard IS the product, so
        # KIROCREW_REQUIRE_FRONTEND=1 makes it FATAL: dump the build log and exit
        # non-zero. That lets the cloud bootstrap RETRY the whole install on the warm
        # box (first-boot contention — the common cause — self-heals), and if it still
        # fails the real npm/vite error reaches the failure reason instead of being
        # swallowed behind a "legacy fallback" warning.
        if [ -s "$_fe_log" ]; then
            echo "----- frontend build log (tail) -----"
            tail -n 20 "$_fe_log"
            echo "-------------------------------------"
        fi
        rm -f "$_fe_log"
        if [ "${KIROCREW_REQUIRE_FRONTEND:-0}" = "1" ]; then
            die "Frontend build failed and KIROCREW_REQUIRE_FRONTEND=1 — no static/dist produced (see the build log above)"
        fi
        warn "Frontend build failed — dashboard will use legacy fallback"
    else
        rm -f "$_fe_log"
    fi
else
    warn "Skipping frontend build (Node.js or website/ not available)"
    detail "Install Node.js 22+ (24 LTS recommended) for the full React dashboard experience"
fi

# ── Python virtual environment & package ──
info "Creating virtual environment…"
_venv="$KIROCREW_APP_DIR/.venv"
if [ -d "$_venv" ] && [ -x "$_venv/bin/python" ]; then
    ok "Existing venv found"
else
    "$_py" -m venv "$_venv" || die "Failed to create venv. You may need: $_py -m pip install virtualenv"
    ok "venv created at $_venv"
fi

info "Installing Python package…"

_pip_target="."
if [ "$WITH_VOICE" -eq 1 ]; then
    _pip_target=".[voice]"
    detail "Including voice extras (.[voice])"
fi

_pip_log="$(mktemp)"
(
    "$_venv/bin/pip" install --upgrade pip setuptools wheel 2>&1 | tail -5 > "$_pip_log"
    cd "$KIROCREW_APP_DIR"
    # Frontend already built and staged above; skip rebuild in setup.py.
    KIROCREW_SKIP_FRONTEND=1 "$_venv/bin/pip" install -e "$_pip_target" 2>&1 | tail -20 >> "$_pip_log"
) &
spinner $! "Installing kirocrew and dependencies…"
if wait $!; then
    if "$_venv/bin/python" -c "import aiohttp" 2>/dev/null; then
        ok "Python package installed (isolated venv)"
    else
        die "Package installed but dependencies missing (aiohttp not importable).
     Try manually: $_venv/bin/pip install -e $KIROCREW_APP_DIR"
    fi
else
    if [ -s "$_pip_log" ]; then
        echo ""
        tail -10 "$_pip_log" | while IFS= read -r _line; do detail "$_line"; done
        echo ""
    fi
    die "pip install failed. Check: $_venv/bin/pip --version"
fi
rm -f "$_pip_log"

# Record install method so `kirocrew update` uses the right rebuild strategy
echo "pip" > "$KIROCREW_APP_DIR/.install-method"
ok "Install method recorded (.install-method=pip)"

# Symlink the kirocrew entry point to ~/.local/bin for PATH access
mkdir -p "$HOME/.local/bin"
ln -sf "$_venv/bin/kirocrew" "$HOME/.local/bin/kirocrew"
ok "Symlinked kirocrew → ~/.local/bin/kirocrew"

# ── Desktop App (macOS only) ──
if [ "$(uname)" = "Darwin" ] && has node && [ -d "$KIROCREW_APP_DIR/electron" ]; then
    printf "\n  Install KiroCrew desktop app to ~/Applications? [Y/n] "
    read -r _install_app < /dev/tty
    case "${_install_app:-Y}" in
        [Yy]*)
            info "Building desktop app…"
            (
                cd "$KIROCREW_APP_DIR/electron"
                npm install --no-audit --no-fund --loglevel=error 2>/dev/null
                npx electron-builder --mac --dir 2>/dev/null
            ) &
            spinner $! "Building Electron app…"
            if wait $!; then
                _app_src="$KIROCREW_APP_DIR/electron/dist/mac-arm64/KiroCrew.app"
                [ ! -d "$_app_src" ] && _app_src="$KIROCREW_APP_DIR/electron/dist/mac/KiroCrew.app"
                if [ -d "$_app_src" ]; then
                    mkdir -p "$HOME/Applications"
                    rm -rf "$HOME/Applications/KiroCrew.app" 2>/dev/null
                    cp -R "$_app_src" "$HOME/Applications/KiroCrew.app"
                    ok "KiroCrew.app installed to ~/Applications"
                    detail "Launch via Spotlight (⌘+Space → KiroCrew) or Finder → ~/Applications"
                else
                    warn "Electron build succeeded but .app not found"
                fi
            else
                warn "Desktop app build failed — you can still use the web dashboard"
            fi
            ;;
        *)
            info "Skipping desktop app"
            detail "Install later: cd $KIROCREW_APP_DIR/electron && npm install && npm run dist"
            ;;
    esac
fi

# ══════════════════════════════════════════════════════════════════════
# Step 4: PATH & Shell Config
# ══════════════════════════════════════════════════════════════════════
step "PATH Configuration"

# The kirocrew entry point is symlinked to ~/.local/bin/kirocrew.
export PATH="$HOME/.local/bin:$KIROCREW_APP_DIR/bin:$PATH"

# Persist to shell rc files
_path_line="export PATH=\"\$HOME/.local/bin:\$PATH\""
_marker="# KiroCrew"

_add_to_rc() {
    local rc="$1"
    [ ! -f "$rc" ] && return
    if grep -qF "$_marker" "$rc" 2>/dev/null; then
        local _tmp
        _tmp="$(mktemp)"
        awk "/$_marker/{skip=2} skip>0{skip--; next} 1" "$rc" > "$_tmp" && mv "$_tmp" "$rc"
    fi
    echo "" >> "$rc"
    echo "$_marker" >> "$rc"
    echo "$_path_line" >> "$rc"
    ok "Added to $(basename "$rc")"
}

# Also persist KIROCREW_PROJECT_DIR so kirocrew works from any directory
_proj_line="export KIROCREW_PROJECT_DIR=\"$KIROCREW_APP_DIR\""

_add_proj_to_rc() {
    local rc="$1"
    [ ! -f "$rc" ] && return
    if grep -qF "KIROCREW_PROJECT_DIR" "$rc" 2>/dev/null; then
        local _tmp
        _tmp="$(mktemp)"
        grep -v "KIROCREW_PROJECT_DIR" "$rc" > "$_tmp" && mv "$_tmp" "$rc"
    fi
    echo "$_proj_line" >> "$rc"
}

export KIROCREW_PROJECT_DIR="$KIROCREW_APP_DIR"

for _rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$_rc" ]; then
        _add_to_rc "$_rc"
        _add_proj_to_rc "$_rc"
    fi
done

# ── Fish shell ──
_fish_config="$HOME/.config/fish/config.fish"
if [ -f "$_fish_config" ] || [ "$(basename "${SHELL:-}")" = "fish" ]; then
    mkdir -p "$HOME/.config/fish"
    if grep -qF "$_marker" "$_fish_config" 2>/dev/null; then
        _tmp="$(mktemp)"
        awk -v marker="$_marker" 'BEGIN{s=0} $0==marker{s=1;next} s&&/^(fish_add_path|set -gx KIROCREW)/{next} {s=0;print}' "$_fish_config" > "$_tmp" && mv "$_tmp" "$_fish_config"
    fi
    {
        echo "$_marker"
        echo "fish_add_path -g ~/.local/bin"
        echo "set -gx KIROCREW_PROJECT_DIR $KIROCREW_APP_DIR"
    } >> "$_fish_config"
    ok "Added to config.fish"
fi

# Save project dir for kirocrew to find
mkdir -p "$KIROCREW_DATA_DIR"
echo "$KIROCREW_APP_DIR" > "$KIROCREW_DATA_DIR/project_dir"

# Verify kirocrew is accessible
if has kirocrew; then
    ok "kirocrew command available"
elif [ -x "$HOME/.local/bin/kirocrew" ]; then
    ok "kirocrew symlinked to ~/.local/bin/kirocrew"
    detail "You may need to restart your shell for it to be in PATH"
else
    warn "kirocrew not in PATH — restart your shell or run:"
    detail "source ~/.$(basename "$SHELL")rc"
fi

# ══════════════════════════════════════════════════════════════════════
# Step 5: Agent Config
# ══════════════════════════════════════════════════════════════════════
step "Agent Config"

# Install agent config via kirocrew setup
if has kirocrew; then
    _kirocrew=kirocrew
elif [ -x "$HOME/.local/bin/kirocrew" ]; then
    _kirocrew="$HOME/.local/bin/kirocrew"
else
    _kirocrew=""
fi
if [ -n "$_kirocrew" ]; then
    info "Installing agent config…"
    KIROCREW_PROJECT_DIR="$KIROCREW_APP_DIR" "$_kirocrew" setup --agent-only \
        && ok "Agent config installed" \
        || warn "kirocrew setup --agent-only failed (run manually after install)"
fi

ok "Run ${CYAN}kirocrew setup${RESET} to configure agent, workspace, and integrations"

# ── Embeddings (optional) ──
info "Optional: local vector memory via Ollama"
detail "Install ollama (https://ollama.com), then: ollama pull qwen3-embedding:0.6b"


# ══════════════════════════════════════════════════════════════════════
# Done!
# ══════════════════════════════════════════════════════════════════════

echo ""
echo ""
echo "  ${GREEN}${BOLD}👻 KiroCrew installed successfully!${RESET}"
echo ""
echo "  ${DIM}────────────────────────────────────────${RESET}"
echo ""
echo "  ${BOLD}Next steps:${RESET}"
echo ""
echo "    ${CYAN}1.${RESET} Reload your shell:"
case "$(basename "${SHELL:-}")" in
    fish) echo "       ${GREEN}source ~/.config/fish/config.fish${RESET}" ;;
    bash|zsh) echo "       ${GREEN}source ~/.$(basename "${SHELL}")rc${RESET}" ;;
    *) echo "       ${GREEN}Restart your terminal${RESET}" ;;
esac
echo ""
echo "    ${CYAN}2.${RESET} Run the setup wizard:"
echo "       ${GREEN}kirocrew setup${RESET}"
echo ""
echo "    ${CYAN}3.${RESET} Start the dashboard:"
echo "       ${GREEN}kirocrew gateway${RESET}"
echo ""
echo "    ${CYAN}4.${RESET} Open ${CYAN}http://localhost:${KIROCREW_PORT}${RESET} in your browser"
echo ""
# SSH tunnel tip for remote Linux users
if [ "$(uname)" != "Darwin" ]; then
    _hostname=$(hostname -f 2>/dev/null || hostname)
    echo "  ${DIM}────────────────────────────────────────${RESET}"
    echo ""
    echo "  ${BOLD}Remote access:${RESET}"
    echo "    Run this on your ${CYAN}local machine${RESET} to forward the dashboard:"
    echo ""
    echo "    ${GREEN}ssh -N -L ${KIROCREW_PORT}:localhost:${KIROCREW_PORT} $_hostname${RESET}"
    echo ""
    echo "    Then open ${CYAN}http://localhost:${KIROCREW_PORT}${RESET} in your local browser."
    echo ""
fi
echo "  ${DIM}────────────────────────────────────────${RESET}"
echo ""
echo "  ${DIM}Update anytime:${RESET}  ${GREEN}git pull && bash install.sh${RESET}"
echo ""
