# shellcheck shell=sh
# Install one pinned mise release, verified against a recorded sha256.
#
# Shared by ensure-python.sh, ensure-node.sh and `install.sh --mise`. They used
# to pipe https://mise.run into sh, which runs whatever that script and the
# release it picks are on the day. ensure-node.sh is also reached without a
# prompt (gateway start, `kirocrew update`), so this path must never execute
# downloaded code before it is verified.
#
# To bump: pick a release at least 7 days old from
# https://github.com/jdx/mise/releases, copy the six archive lines from its
# SHASUMS256.txt (they must match the per-asset sha256 GitHub shows), update
# MISE_PIN_VERSION, and run test/test_installer_tool_pins.py. The release checklist
# (docs/build/release.md) asks for this review on every release. A stale pin is
# not a failure: the pinned release stays downloadable.

MISE_PIN_VERSION="2026.10.13"
MISE_PIN_SHA256_LINUX_X64="b775d3feee8f0cdb1934c60a1ec26526d6eb7cdeb258b48c64cfadcec07a8fec"
MISE_PIN_SHA256_LINUX_ARM64="ec1630fd441434a1b8c8c0074a53e3e8cc955ad313e9f354c44775eb19103a57"
MISE_PIN_SHA256_LINUX_X64_MUSL="2ece198f8bebffa06e1027ea5dc5fc26d14b2bc03c9c285658317f13da8ce3dd"
MISE_PIN_SHA256_LINUX_ARM64_MUSL="c8b6cce02b08da85b7e6ac67adb1f1813b5ce095ab56533ade6fa083877f209f"
MISE_PIN_SHA256_MACOS_X64="53dd8c3f08a995e88a300e561183bdd294f2aedd7d986dbcaa6c9a649dab6528"
MISE_PIN_SHA256_MACOS_ARM64="b91f1b30bd9e7e6899899f0ff5ac5625a90c86daa652524f7b8922db3287ff60"

# Print "<asset-suffix> <sha256>" for this host, or fail on an unsupported one.
# Same libc choice as the upstream installer: musl only when the host is musl.
_mise_pin_target() {
    _mp_os="$(uname -s)"
    _mp_arch="$(uname -m)"
    case "$_mp_arch" in
        x86_64|amd64) _mp_arch="x64" ;;
        aarch64|arm64) _mp_arch="arm64" ;;
        *) return 1 ;;
    esac
    case "$_mp_os" in
        Darwin)
            [ "$_mp_arch" = "x64" ] && echo "macos-x64 $MISE_PIN_SHA256_MACOS_X64" && return 0
            echo "macos-arm64 $MISE_PIN_SHA256_MACOS_ARM64"
            ;;
        Linux)
            _mp_musl=""
            if command -v ldd >/dev/null 2>&1 && ldd /bin/ls 2>/dev/null | grep -q musl; then
                _mp_musl="-musl"
            fi
            case "$_mp_arch$_mp_musl" in
                x64) echo "linux-x64 $MISE_PIN_SHA256_LINUX_X64" ;;
                arm64) echo "linux-arm64 $MISE_PIN_SHA256_LINUX_ARM64" ;;
                x64-musl) echo "linux-x64-musl $MISE_PIN_SHA256_LINUX_X64_MUSL" ;;
                arm64-musl) echo "linux-arm64-musl $MISE_PIN_SHA256_LINUX_ARM64_MUSL" ;;
            esac
            ;;
        *) return 1 ;;
    esac
}

# Install the pinned mise to ${MISE_INSTALL_PATH:-$HOME/.local/bin/mise}.
# Fail-closed: no checksum tool, a failed download or a mismatch installs
# nothing and returns 1. Nothing downloaded is executed before it is verified.
install_pinned_mise() {
    _mp_target="$(_mise_pin_target)" || {
        echo "  ⚠  No pinned mise build for $(uname -s)/$(uname -m) — install mise manually: https://mise.jdx.dev/installing-mise.html"
        return 1
    }
    _mp_suffix="${_mp_target% *}"
    _mp_want="${_mp_target#* }"
    if command -v sha256sum >/dev/null 2>&1; then _mp_sha="sha256sum"
    elif command -v shasum >/dev/null 2>&1; then _mp_sha="shasum -a 256"
    else
        echo "  ⚠  No sha256sum/shasum available — refusing to install mise unverified"
        return 1
    fi
    _mp_dest="${MISE_INSTALL_PATH:-$HOME/.local/bin/mise}"
    _mp_asset="mise-v${MISE_PIN_VERSION}-${_mp_suffix}.tar.gz"
    _mp_url="https://github.com/jdx/mise/releases/download/v${MISE_PIN_VERSION}/${_mp_asset}"
    _mp_tmp="$(mktemp -d)" || return 1
    if ! curl -fsSL "$_mp_url" -o "$_mp_tmp/mise.tar.gz"; then
        echo "  ⚠  Download failed: $_mp_url"
        rm -rf "$_mp_tmp"; return 1
    fi
    # $_mp_sha is one or two words by construction; split on purpose.
    # shellcheck disable=SC2086
    _mp_got="$($_mp_sha "$_mp_tmp/mise.tar.gz" | awk '{print $1}')"
    if [ "$_mp_got" != "$_mp_want" ]; then
        echo "  ⚠  Checksum mismatch for $_mp_asset (want $_mp_want got $_mp_got) — not installing"
        rm -rf "$_mp_tmp"; return 1
    fi
    if ! (cd "$_mp_tmp" && tar --no-same-owner -xzf mise.tar.gz mise/bin/mise) \
        || [ ! -f "$_mp_tmp/mise/bin/mise" ]; then
        echo "  ⚠  Could not extract mise from $_mp_asset"
        rm -rf "$_mp_tmp"; return 1
    fi
    mkdir -p "$(dirname "$_mp_dest")" \
        && chmod 755 "$_mp_tmp/mise/bin/mise" \
        && mv -f "$_mp_tmp/mise/bin/mise" "$_mp_dest"
    _mp_rc=$?
    rm -rf "$_mp_tmp"
    [ "$_mp_rc" -eq 0 ] && echo "  ✅ mise $MISE_PIN_VERSION installed ($_mp_dest)"
    return "$_mp_rc"
}
