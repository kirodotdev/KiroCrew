#!/usr/bin/env bash
# ======================================================================
#  KiroCrew — cloud launcher bootstrap (macOS / Linux)
# ======================================================================
#  Ensures the client prerequisites — Python, the AWS CLI, and the SSM
#  Session Manager plugin — then hands off to the Python launcher wizard
#  (`kirocrew cloud launch`), which provisions + configures an EC2 instance
#  in YOUR OWN AWS account and opens the dashboard.
#
#  This is the thin cold-start bootstrapper: all interesting logic lives in
#  the Python `kiro_crew.cloud` module (testable, cross-platform). It NEVER
#  stores AWS credentials — the aws CLI resolves them from your profile/SSO.
#
#  Usage (from a clone):   bash cloud-install.sh [--size <tier>] [--non-interactive]
# ======================================================================
set -euo pipefail

# Isolate the managed venv from any inherited PYTHONPATH/PYTHONHOME. If the
# caller's environment points these at foreign site-packages (e.g. another
# app's interpreter on a different Python version), pip treats those packages
# as already satisfied and silently skips installing our dependencies into the
# venv -- producing a broken install (ImportError: No module named 'aiohttp').
unset PYTHONPATH PYTHONHOME

SIZE=""
NONINTERACTIVE=0
# while+shift (not `for arg in "$@"`): the space-separated `--size <tier>`
# form needs to consume the following word, which a for-loop can't do.
while [ $# -gt 0 ]; do
    case "$1" in
        --size) shift; SIZE="${1:-}";;
        --size=*) SIZE="${1#*=}";;
        --non-interactive) NONINTERACTIVE=1;;
        *) ;;
    esac
    shift || true
done

if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
    BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
    GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); CYAN=$(tput setaf 6); MAGENTA=$(tput setaf 5)
else
    BOLD="" DIM="" RESET="" GREEN="" YELLOW="" CYAN="" MAGENTA=""
fi
step()  { echo; echo "  ${CYAN}${BOLD}[$1/4]${RESET} ${BOLD}$2${RESET}"; }
ok()    { echo "  ${GREEN}✓${RESET} $1"; }
warn()  { echo "  ${YELLOW}⚠${RESET} $1"; }
info()  { echo "  ${DIM}→${RESET} $1"; }
has()   { command -v "$1" >/dev/null 2>&1; }

OS="$(uname -s)"
ARCH="$(uname -m)"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Privilege prefix for package installs: empty when already root (a minimal
# CentOS / container image often has no `sudo` binary, and invoking it would
# abort the installer under `set -e` with command-not-found), empty when sudo
# is simply absent, else `sudo`.
SUDO=""
if [ "$(id -u)" != "0" ] && has sudo; then SUDO="sudo"; fi

echo
echo "  ${MAGENTA}${BOLD}KiroCrew — run on your own AWS${RESET}"
echo "  ${DIM}Platform: $OS $ARCH${RESET}"

# --- 1. Python -------------------------------------------------------------
step 1 "Python"
PY=""
for c in python3.12 python3.11 python3.10 python3; do
    if has "$c" && "$c" -c 'import sys; assert sys.version_info>=(3,10)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    if [ "$OS" = "Darwin" ]; then
        info "Installing the Xcode command-line tools (provides python3)…"
        xcode-select --install 2>/dev/null || true
        warn "Finish the Xcode CLT install if prompted, then re-run this script."
    elif has apt-get; then $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-venv python3-pip
    elif has dnf; then $SUDO dnf install -y python3.11 python3.11-pip 2>/dev/null || $SUDO dnf install -y python3 python3-pip
    elif has yum; then $SUDO yum install -y python3 python3-pip
    elif has brew; then brew install python@3.12
    fi
    # Re-probe with the same >=3.10 gate as above -- a bare existence check would
    # wrongly latch onto a distro's default python3 (RHEL/CentOS 7 = 3.6,
    # Amazon Linux 2023 = 3.9), which then fails later at pip/venv.
    for c in python3.12 python3.11 python3.10 python3; do
        if has "$c" && "$c" -c 'import sys; assert sys.version_info>=(3,10)' 2>/dev/null; then PY="$c"; break; fi
    done
fi
[ -n "$PY" ] || { warn "Python 3.10+ required. Install it and re-run."; exit 1; }
ok "$($PY --version 2>&1)"

# --- 2. AWS CLI ------------------------------------------------------------
step 2 "AWS CLI"
if has aws; then
    ok "$(aws --version 2>&1 | head -1)"
else
    info "Installing the AWS CLI v2…"
    tmp="$(mktemp -d)"
    if [ "$OS" = "Darwin" ]; then
        curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "$tmp/awscli.pkg"
        sudo installer -pkg "$tmp/awscli.pkg" -target / && ok "aws CLI installed" || warn "aws CLI install failed — see https://aws.amazon.com/cli/"
    else
        case "$ARCH" in
            aarch64|arm64) url="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip";;
            *) url="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip";;
        esac
        curl -fsSL "$url" -o "$tmp/awscliv2.zip" && (cd "$tmp" && unzip -q awscliv2.zip && sudo ./aws/install --update) \
            && ok "aws CLI installed" || warn "aws CLI install failed — see https://aws.amazon.com/cli/"
    fi
    rm -rf "$tmp"
fi

# --- 3. SSM Session Manager plugin ----------------------------------------
step 3 "SSM Session Manager plugin"
if has session-manager-plugin; then
    ok "session-manager-plugin found"
else
    info "Installing the SSM Session Manager plugin…"
    tmp="$(mktemp -d)"
    if [ "$OS" = "Darwin" ]; then
        # Pick the package by CPU arch up front — Intel Macs must not fetch the
        # arm64 build (a valid download of the wrong architecture).
        case "$ARCH" in
            aarch64|arm64) mac_url="https://session-manager-downloads.s3.amazonaws.com/plugin/latest/mac_arm64/session-manager-plugin.pkg";;
            *)             mac_url="https://session-manager-downloads.s3.amazonaws.com/plugin/latest/mac/session-manager-plugin.pkg";;
        esac
        if curl -fsSL "$mac_url" -o "$tmp/ssm.pkg"; then sudo installer -pkg "$tmp/ssm.pkg" -target / 2>/dev/null || true; fi
        if has session-manager-plugin; then ok "session-manager-plugin installed"; else warn "SSM plugin install failed (needed to connect)"; fi
    elif has dnf || has rpm; then
        case "$ARCH" in aarch64|arm64) url="https://s3.amazonaws.com/session-manager-downloads/plugin/latest/linux_arm64/session-manager-plugin.rpm";; *) url="https://s3.amazonaws.com/session-manager-downloads/plugin/latest/linux_64bit/session-manager-plugin.rpm";; esac
        # Explicit if/elif (not a mixed &&/|| chain, which can latch a success
        # message onto a partial failure). Verify the binary landed at the end.
        if curl -fsSL "$url" -o "$tmp/ssm.rpm"; then
            if has dnf; then sudo dnf install -y "$tmp/ssm.rpm" 2>/dev/null || true
            else sudo rpm -i "$tmp/ssm.rpm" 2>/dev/null || true; fi
        fi
        if has session-manager-plugin; then ok "session-manager-plugin installed"; else warn "SSM plugin install failed"; fi
    elif has apt-get || has dpkg; then
        case "$ARCH" in aarch64|arm64) url="https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_arm64/session-manager-plugin.deb";; *) url="https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb";; esac
        if curl -fsSL "$url" -o "$tmp/ssm.deb"; then sudo dpkg -i "$tmp/ssm.deb" 2>/dev/null || true; fi
        if has session-manager-plugin; then ok "session-manager-plugin installed"; else warn "SSM plugin install failed"; fi
    else
        warn "Could not auto-install the SSM plugin — see AWS docs; connect will still print the URL."
    fi
    rm -rf "$tmp"
fi

# --- 4. KiroCrew client + launch ------------------------------------------
step 4 "KiroCrew client"
info "Installing the KiroCrew client (pip, editable)…"
_venv="$REPO_ROOT/.venv"
[ -x "$_venv/bin/python" ] || "$PY" -m venv "$_venv"
"$_venv/bin/pip" install --upgrade pip -q 2>/dev/null || true
KIROCREW_SKIP_FRONTEND=1 "$_venv/bin/pip" install -e "$REPO_ROOT" -q && ok "KiroCrew client installed" || { warn "pip install failed"; exit 1; }
mkdir -p "$HOME/.local/bin"
ln -sf "$_venv/bin/kirocrew" "$HOME/.local/bin/kirocrew"
KC="$_venv/bin/kirocrew"

# Confirm AWS is configured before launching.
if ! aws sts get-caller-identity >/dev/null 2>&1; then
    warn "AWS credentials aren't configured yet."
    info "Run one of:  ${CYAN}aws configure sso${RESET}  (recommended)  or  ${CYAN}aws configure${RESET}"
    info "Then launch:  ${CYAN}kirocrew cloud launch${RESET}"
    exit 0
fi
ok "AWS credentials resolve."

if [ "$NONINTERACTIVE" -eq 1 ]; then
    info "Prerequisites ready. Launch when you're set:  ${CYAN}kirocrew cloud launch${RESET}"
    exit 0
fi

echo
echo "  ${CYAN}Launching KiroCrew on AWS…${RESET}"
if [ -n "$SIZE" ]; then "$KC" cloud launch --size "$SIZE"; else "$KC" cloud launch; fi
