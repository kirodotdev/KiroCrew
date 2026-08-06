#!/bin/bash
# Ensure Node.js is available for the build. Installs via mise (preferred),
# nvm, or the nodejs "unofficial-builds" glibc-217 tarball on old-glibc hosts.
# Called by: setup.sh, kirocrew update, kirocrew gateway, Makefile (frontend)
# Platforms: macOS, Amazon Linux 2 (glibc 2.26), Amazon Linux 2023, Linux
#
# On success this records the resolved node bin directory in
# "<data-home>/node-bin-dir" so non-interactive callers (e.g. make) can put
# the right node on PATH without re-running a version manager. The data home is
# "$KIROCREW_HOME" when set, else "$HOME/.kiro/crew" (the current default) —
# NOT the pre-move "$HOME/.kirocrew", which the one-time data-home migration
# deletes, so writing there would resurrect the legacy dir on every build.

# The frontend build (vite 8 / rolldown) requires a Node in the bundler's own
# declared support range: `vite@8.2.0` and `rolldown@1.2.3` both set
# engines.node "^20.19.0 || >=22.12.0". Too-old Node fails in more than one
# way; the first symptom on Node 16 is rolldown importing `styleText` from
# `node:util` (added in 20.12.0), which aborts the build with
#   "The requested module 'node:util' does not provide an export named 'styleText'".
# See _version_meets_floor for the exact per-line cutoffs.
MIN_VERSION="20.19"   # human-readable primary floor, for messages
TARGET_VERSION=20

# Amazon Linux 2 ships glibc 2.26, but the OFFICIAL Node >= 18 binaries are
# linked against glibc >= 2.28 and fail to load ("GLIBC_2.28 not found"). The
# official Node 16 build (glibc-2.17 baseline) runs on AL2 but is below the
# >= 20.19 floor the frontend now needs. The nodejs "unofficial-builds" project
# publishes a `glibc-217` variant compiled against glibc 2.17 that satisfies
# both constraints on AL2 — x64 only (upstream ships no arm64 glibc-217 build).
GLIBC217_VERSION=20.20.2

_node_major() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f1
}

_node_minor() {
    node -v 2>/dev/null | sed 's/v//' | cut -d. -f2
}

# node is "usable" only if it is on PATH AND actually executes. A binary built
# for a newer glibc is present on PATH but exits non-zero with a loader error,
# so a plain "command -v node" is not enough.
_node_usable() {
    command -v node >/dev/null 2>&1 && node -v >/dev/null 2>&1
}

# The floor is the frontend bundler's OWN declared support range, not just the
# first symptom: `vite@8.2.0` and `rolldown@1.2.3` (website/package-lock.json)
# both declare engines.node "^20.19.0 || >=22.12.0". So 20.19+ is supported on
# the 20.x line, the whole 21.x line is not supported at all, and 22 needs
# 22.12+. A missing `util.styleText` (added in 20.12.0) is only the first way a
# too-old Node fails; gating on that export alone would still admit versions the
# bundler rejects. Re-check this range when vite/rolldown are upgraded.
_version_meets_floor() {   # $1=major $2=minor
    local maj="$1" min="$2"
    [ -n "$maj" ] && [ -n "$min" ] || return 1
    if [ "$maj" -eq 20 ]; then [ "$min" -ge 19 ]; return; fi
    if [ "$maj" -eq 22 ]; then [ "$min" -ge 12 ]; return; fi
    if [ "$maj" -ge 23 ]; then return 0; fi
    return 1   # 21.x is unsupported; so is anything below 20.19
}

# True (0) iff the node FIRST ON PATH runs and meets the floor. This is the
# predicate for "is node good enough?" — used to decide install, to pick the
# old-glibc fallback, and for final validation, so a runnable-but-too-old node
# (Node 16/20.11/21.6 already on PATH) never masquerades as sufficient.
_node_version_ok() {
    _node_usable || return 1
    _version_meets_floor "$(_node_major)" "$(_node_minor)"
}

# True (0) iff the given node BINARY runs and meets the floor. Used to test a
# candidate without disturbing PATH.
_node_bin_ok() {   # $1 = path to a node binary
    local nb="$1" ver
    [ -x "$nb" ] || return 1
    ver="$("$nb" -v 2>/dev/null)" || return 1
    ver="${ver#v}"
    [ -n "$ver" ] || return 1
    _version_meets_floor "${ver%%.*}" "$(printf '%s\n' "${ver#*.}" | cut -d. -f1)"
}

_needs_install() {
    ! _node_version_ok
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

# Every node bin dir this script knows about, most-preferred first. Printed one
# per line; only existing, executable candidates are emitted.
_candidate_node_bins() {
    local d nb mise_bin nvm_versions v
    d="$(_glibc217_node_dir)/bin"
    # Same completeness requirement as _source_glibc217_node: node without npm is
    # a half-written tree, not a usable toolchain.
    [ -x "$d/node" ] && [ -x "$d/npm" ] && echo "$d"

    if [ -x "$HOME/.local/bin/mise" ]; then
        mise_bin="$HOME/.local/bin/mise"
    elif command -v mise >/dev/null 2>&1; then
        mise_bin="mise"
    fi
    if [ -n "$mise_bin" ]; then
        nb="$("$mise_bin" which node 2>/dev/null)"
        [ -n "$nb" ] && [ -x "$nb" ] && dirname "$nb"
    fi

    # nvm keeps every installed version side by side; try newest first. The "v"
    # prefix must be stripped before sorting: with it, `sort -k1,1nr` reads "v20"
    # as 0, every entry ties on the first field, and the order silently falls
    # through to the MINOR number (v18.19.0 ahead of v20.12.0).
    nvm_versions="${NVM_DIR:-$HOME/.nvm}/versions/node"
    if [ -d "$nvm_versions" ]; then
        for v in $(ls -1 "$nvm_versions" 2>/dev/null | sed 's/^v//' \
                   | sort -t. -k1,1nr -k2,2nr -k3,3nr); do
            [ -x "$nvm_versions/v$v/bin/node" ] && echo "$nvm_versions/v$v/bin"
        done
    fi
}

# PATH precedence is SOURCE ORDER, not version: whichever manager is sourced
# last wins, so an nvm default of Node 18 can mask a Node 20 that mise or the
# glibc-217 install just provided, and the run then fails validation with the
# right node installed but not in front. When the node on PATH is below the
# floor, promote the first candidate that meets it instead of trusting order.
_promote_usable_node() {
    _node_version_ok && return 0
    local cand
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        if _node_bin_ok "$cand/node"; then
            export PATH="$cand:$PATH"
            return 0
        fi
    done <<EOF
$(_candidate_node_bins)
EOF
    return 1
}

# Directory holding the unofficial glibc-217 Node install, under the data home.
_glibc217_node_dir() {
    echo "${KIROCREW_HOME:-$HOME/.kiro/crew}/node-glibc217"
}

# Put a previously-installed glibc-217 node on PATH (if present). Called LAST in
# the source order so it takes precedence over any stale mise/nvm node. Requires
# npm alongside node, matching the reuse check in _install_glibc217_node: a tree
# left half-written by an older build has a runnable bin/node but no npm, and
# putting that on PATH would satisfy the version gate while breaking `npm ci`.
_source_glibc217_node() {
    local bin
    bin="$(_glibc217_node_dir)/bin"
    [ -x "$bin/node" ] && [ -x "$bin/npm" ] && export PATH="$bin:$PATH"
}

# Install the nodejs "unofficial-builds" glibc-217 variant (glibc 2.17 baseline)
# so a supported Node runs on old-glibc hosts (AL2 = glibc 2.26). x64 only.
# On success exports PATH and returns 0; otherwise prints guidance and returns 1.
_install_glibc217_node() {
    local ver="$GLIBC217_VERSION"
    local arch
    arch="$(uname -m)"
    if [ "$arch" != "x86_64" ]; then
        echo "  ⚠️  $arch: upstream publishes no glibc-217 Node build."
        echo "     The frontend build needs Node >= $MIN_VERSION (vite 8 / rolldown), and the"
        echo "     official arm64 Node >= 18 needs glibc 2.28 (this host has less)."
        echo "     Build Node from source, or run 'make build' on an x86_64 host / in CI."
        return 1
    fi

    local dir tarball url tmp
    dir="$(_glibc217_node_dir)"
    tarball="node-v${ver}-linux-x64-glibc-217"
    url="https://unofficial-builds.nodejs.org/download/release/v${ver}/${tarball}.tar.gz"

    # Reuse an existing install only when it is COMPLETE (npm present) and meets
    # the floor. A tree left behind by an interrupted extraction can have a
    # runnable bin/node but no npm, and must be replaced rather than trusted.
    if _node_bin_ok "$dir/bin/node" && [ -x "$dir/bin/npm" ]; then
        export PATH="$dir/bin:$PATH"
        return 0
    fi

    echo "  → Installing Node v${ver} (glibc-217, old-glibc compatible) from unofficial-builds…"
    tmp="$(mktemp -d)" || return 1
    if ! curl -fsSL "$url" -o "$tmp/node.tar.gz"; then
        echo "  ⚠️  Download failed: $url"
        rm -rf "$tmp"; return 1
    fi

    # Verify integrity against the published checksum before extracting. Both the
    # manifest and the tarball come from one HTTPS origin, so this is corruption
    # detection rather than an independent trust root — but it is fail-closed: a
    # manifest we cannot fetch, a tool we do not have, or an absent entry all stop
    # the install rather than silently extracting unverified bytes.
    if ! curl -fsSL "https://unofficial-builds.nodejs.org/download/release/v${ver}/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"; then
        echo "  ⚠️  Could not fetch SHASUMS256.txt for v${ver} — refusing to install unverified"
        rm -rf "$tmp"; return 1
    fi
    local want got sha_cmd
    if command -v sha256sum >/dev/null 2>&1; then sha_cmd="sha256sum";
    elif command -v shasum >/dev/null 2>&1; then sha_cmd="shasum -a 256"; fi
    if [ -z "$sha_cmd" ]; then
        echo "  ⚠️  No sha256sum/shasum available — refusing to install unverified"
        rm -rf "$tmp"; return 1
    fi
    want="$(awk -v f="${tarball}.tar.gz" '$2==f {print $1}' "$tmp/SHASUMS256.txt")"
    got="$($sha_cmd "$tmp/node.tar.gz" | awk '{print $1}')"
    # Empty want = the expected filename was absent from the manifest
    # (wrong version / bad download); fail closed rather than skip.
    if [ -z "$want" ] || [ "$want" != "$got" ]; then
        echo "  ⚠️  Checksum verification failed for ${tarball}.tar.gz (want '$want' got '$got')"
        rm -rf "$tmp"; return 1
    fi

    # Extract into a staging dir NEXT TO the destination — same filesystem, so the
    # promote below is an atomic rename — then validate it and swap it in.
    # Unpacking straight into $dir cannot be made safe: if the process is killed
    # mid-tar, no cleanup runs and $dir is left as a partial tree for the next run
    # to find. Staging means an interrupted run leaves $dir untouched.
    local staging="${dir}.incoming.$$"
    mkdir -p "$(dirname "$dir")"
    rm -rf "$staging"
    if ! mkdir -p "$staging"; then
        echo "  ⚠️  Could not create staging dir $staging"
        rm -rf "$tmp"; return 1
    fi
    if ! tar -xzf "$tmp/node.tar.gz" -C "$staging" --strip-components=1; then
        echo "  ⚠️  Extraction failed"
        rm -rf "$staging" "$tmp"; return 1
    fi
    rm -rf "$tmp"

    # Validate the staged tree BEFORE it becomes the install, so $dir only ever
    # holds a complete, runnable, floor-meeting Node.
    if ! _node_bin_ok "$staging/bin/node" || [ ! -x "$staging/bin/npm" ]; then
        echo "  ⚠️  Extracted Node tree is incomplete or does not meet Node >= $MIN_VERSION"
        rm -rf "$staging"; return 1
    fi

    rm -rf "$dir"
    if ! mv "$staging" "$dir"; then
        echo "  ⚠️  Could not promote $staging to $dir"
        rm -rf "$staging"; return 1
    fi

    export PATH="$dir/bin:$PATH"
}

# Record where node ended up so make / other callers can find it. Writes into
# the data home ($KIROCREW_HOME, else ~/.kiro/crew) — never the legacy
# ~/.kirocrew (see header).
_record_node_bin() {
    if command -v node >/dev/null 2>&1; then
        local home="${KIROCREW_HOME:-$HOME/.kiro/crew}"
        mkdir -p "$home"
        dirname "$(command -v node)" > "$home/node-bin-dir" 2>/dev/null || true
    fi
}

PLATFORM=$(_get_platform)

# Source existing managers first — node may already be installed but not in PATH.
# _promote_usable_node then puts a good-enough node in front regardless of which
# manager was sourced last, so an already-present Node 20 is not reinstalled just
# because an older nvm default masks it.
_source_mise
_source_nvm
_source_glibc217_node
_promote_usable_node

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
    al2)
        # AL2 (glibc 2.26): the official Node >= 18 binaries won't load, so use
        # the glibc-217 unofficial build (meets the vite 8 / rolldown range).
        _install_glibc217_node
        ;;
    mac|al2023)
        # macOS + AL2023 (glibc 2.34) run the official mise build for Node 20.
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
        # If the resulting node can't load OR is outside the supported range (an
        # old glibc leaves official Node 20 unloadable, or a pre-existing
        # nvm/mise Node 16/18 is runnable but too old), fall back to the
        # glibc-217 unofficial build (x64), which runs on glibc 2.17+ and is
        # within the range the frontend build requires.
        _source_mise
        _source_nvm
        # Promote before deciding: source order alone can leave an nvm default of
        # Node 18 in front of the Node 20 mise just installed, and testing PATH
        # as-is would download the unofficial build on a host that already has a
        # supported toolchain.
        _promote_usable_node
        if ! _node_version_ok; then
            echo "  → node@$TARGET_VERSION is unusable or < $MIN_VERSION here; trying glibc-217 build…"
            _install_glibc217_node
        fi
        ;;
esac

# Re-source to pick up the newly installed node, then repair PATH precedence:
# sourcing order alone would let an older manager's node sit in front of the one
# just installed (see _promote_usable_node).
_source_mise
_source_nvm
_source_glibc217_node
_promote_usable_node

if _node_version_ok; then
    echo "  ✅ node $(node -v) installed ($(which node))"
    _record_node_bin
else
    echo "  ⚠️  Node install failed — frontend build needs Node >= $MIN_VERSION"
    echo "     macOS / AL2023 / modern Linux: curl https://mise.run | sh && mise use -g node@$TARGET_VERSION"
    echo "     Amazon Linux 2 (x86_64): re-run 'bash ensure-node.sh' (installs the glibc-217 Node build)"
    exit 1
fi
