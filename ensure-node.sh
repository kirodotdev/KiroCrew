#!/bin/bash
# Ensure Node.js >= 18 is available. Installs via mise (preferred) or nvm.
# Called by: setup.sh, kirocrew update, kirocrew gateway
# Platforms: macOS, Linux

MIN_VERSION=18
TARGET_VERSION=20

_node_major() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f1
}

_needs_install() {
    if ! command -v node &>/dev/null; then return 0; fi
    local cur
    cur=$(_node_major)
    [ -z "$cur" ] || [ "$cur" -lt "$MIN_VERSION" ]
}

_get_platform() {
    if [[ "$(uname)" == "Darwin" ]]; then echo "mac"
    else echo "linux"; fi
}

_source_mise() {
    if [ -f "$HOME/.local/bin/mise" ]; then
        eval "$("$HOME/.local/bin/mise" activate bash 2>/dev/null)" 2>/dev/null || true
    elif command -v mise &>/dev/null; then
        eval "$(mise activate bash 2>/dev/null)" 2>/dev/null || true
    fi
}

_source_nvm() {
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        . "$NVM_DIR/nvm.sh"
    fi
}

# Source existing managers first — node may already be installed but not in PATH
_source_mise
_source_nvm

if ! _needs_install; then
    echo "  ✅ node v$(_node_major) ($(which node))"
    exit 0
fi

PLATFORM=$(_get_platform)
echo "  → Node.js missing or < $MIN_VERSION, installing on $PLATFORM…"

_ensure_mise() {
    _source_mise
    if ! command -v mise &>/dev/null; then
        echo "  → Installing mise…"
        curl -fsSL https://mise.run | sh
        _source_mise
    fi
}

case $PLATFORM in
    mac)
        _ensure_mise
        mise use -g "node@$TARGET_VERSION" 2>/dev/null
        ;;
    *)
        # Generic Linux — try mise, fall back to nvm
        _ensure_mise
        if command -v mise &>/dev/null; then
            mise use -g "node@$TARGET_VERSION" 2>/dev/null
        else
            echo "  → Falling back to nvm…"
            if [ ! -d "$HOME/.nvm" ]; then
                curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash 2>/dev/null
            fi
            _source_nvm
            nvm install "$TARGET_VERSION" 2>/dev/null
            nvm alias default "$TARGET_VERSION" 2>/dev/null
        fi
        ;;
esac

# Re-source to pick up newly installed node
_source_mise
_source_nvm

if command -v node &>/dev/null && [ "$(_node_major)" -ge "$MIN_VERSION" ]; then
    echo "  ✅ node $(node -v) installed ($(which node))"
else
    echo "  ⚠️  Node install failed — frontend will use legacy fallback"
    echo "     Install manually: curl https://mise.run | sh && mise use -g node@$TARGET_VERSION"
    exit 1
fi
