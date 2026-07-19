#!/bin/bash
# Ensure Node.js is available for the build. Installs via mise (preferred) or nvm.
# Called by: setup.sh, kirocrew update, kirocrew gateway, Makefile (frontend)
# Platforms: macOS, Amazon Linux 2 (glibc 2.26), Amazon Linux 2023, Linux
#
# On success this records the resolved node bin directory in
# "$HOME/.kirocrew/node-bin-dir" so non-interactive callers (e.g. make) can put
# the right node on PATH without re-running a version manager.

MIN_VERSION=18
TARGET_VERSION=20

# Amazon Linux 2 ships glibc 2.26, but official Node >= 18 binaries are linked
# against glibc >= 2.28 and fail to load ("GLIBC_2.28 not found"). Official
# Node 16 was built on a glibc-2.17 baseline, so it runs on AL2 for both x86_64
# and aarch64 and is enough for the frontend build (tsc + vite). Test-only
# tooling that wants >= 18 runs in CI, not on the AL2 build host. This mirrors
# the sibling deployment's approach.
AL2_NODE_VERSION=16

_node_major() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f1
}

# node is "usable" only if it is on PATH AND actually executes. A binary built
# for a newer glibc is present on PATH but exits non-zero with a loader error,
# so a plain "command -v node" is not enough.
_node_usable() {
    command -v node >/dev/null 2>&1 && node -v >/dev/null 2>&1
}

_needs_install() {
    if ! _node_usable; then return 0; fi
    local cur
    cur=$(_node_major)
    [ -z "$cur" ] || [ "$cur" -lt "$MIN_VERSION" ]
}

_get_platform() {
    if [[ "$(uname)" == "Darwin" ]]; then echo "mac"
    # AL2023 must be checked before AL2: "Amazon Linux release 2" is a
    # substring of "Amazon Linux release 2023".
    elif grep -q 'Amazon Linux release 2023' /etc/system-release 2>/dev/null; then echo "al2023"
    elif grep -q 'Amazon Linux release 2' /etc/system-release 2>/dev/null; then echo "al2"
    else echo "linux"; fi
}

_source_mise() {
    local mise_bin=""
    if [ -x "$HOME/.local/bin/mise" ]; then
        mise_bin="$HOME/.local/bin/mise"
    elif command -v mise &>/dev/null; then
        mise_bin="mise"
    fi
    [ -z "$mise_bin" ] && return 0
    eval "$("$mise_bin" activate bash 2>/dev/null)" 2>/dev/null || true
    # `mise activate` sets up a prompt hook and does not reliably inject tools
    # onto PATH in a non-interactive script (e.g. when invoked from make), so
    # also add the resolved node bin dir directly.
    local nb
    nb="$("$mise_bin" which node 2>/dev/null)"
    [ -n "$nb" ] && export PATH="$(dirname "$nb"):$PATH"
}

_source_nvm() {
    export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        . "$NVM_DIR/nvm.sh"
    fi
}

# Record where node ended up so make / other callers can find it.
_record_node_bin() {
    if command -v node >/dev/null 2>&1; then
        mkdir -p "$HOME/.kirocrew"
        dirname "$(command -v node)" > "$HOME/.kirocrew/node-bin-dir" 2>/dev/null || true
    fi
}

PLATFORM=$(_get_platform)

# On Amazon Linux 2, target Node 16 (see AL2_NODE_VERSION note above).
if [ "$PLATFORM" = "al2" ]; then
    MIN_VERSION="$AL2_NODE_VERSION"
    TARGET_VERSION="$AL2_NODE_VERSION"
fi

# Source existing managers first — node may already be installed but not in PATH
_source_mise
_source_nvm

if ! _needs_install; then
    echo "  ✅ node v$(_node_major) ($(which node))"
    _record_node_bin
    exit 0
fi

echo "  → Node.js missing or < $MIN_VERSION, installing node@$TARGET_VERSION on $PLATFORM…"

_ensure_mise() {
    _source_mise
    if ! command -v mise &>/dev/null; then
        echo "  → Installing mise…"
        curl -fsSL https://mise.run | sh
        _source_mise
    fi
}

case $PLATFORM in
    mac|al2|al2023)
        # macOS + AL2023 use the default target (20); AL2 uses 16 (set above).
        # All are official mise builds that run on the host's glibc.
        _ensure_mise
        mise use -g "node@$TARGET_VERSION" 2>/dev/null
        ;;
    *)
        # Generic Linux — try mise, fall back to nvm.
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
        # If the stock binary can't load (old glibc on a non-AL2-labelled host),
        # fall back to Node 16, whose official build runs on glibc 2.17+.
        _source_mise
        _source_nvm
        if ! _node_usable; then
            echo "  → node@$TARGET_VERSION won't run here (likely old glibc); falling back to node@$AL2_NODE_VERSION…"
            MIN_VERSION="$AL2_NODE_VERSION"
            TARGET_VERSION="$AL2_NODE_VERSION"
            if command -v mise &>/dev/null; then
                mise use -g "node@$TARGET_VERSION" 2>/dev/null
            else
                _source_nvm
                nvm install "$TARGET_VERSION" 2>/dev/null
                nvm alias default "$TARGET_VERSION" 2>/dev/null
            fi
        fi
        ;;
esac

# Re-source to pick up newly installed node
_source_mise
_source_nvm

if _node_usable && [ "$(_node_major)" -ge "$MIN_VERSION" ]; then
    echo "  ✅ node $(node -v) installed ($(which node))"
    _record_node_bin
else
    echo "  ⚠️  Node install failed — frontend will use legacy fallback"
    echo "     Install manually: curl https://mise.run | sh && mise use -g node@$TARGET_VERSION"
    exit 1
fi
