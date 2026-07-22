#!/bin/bash
# KiroCrew Persistent Sessions Setup
#
# Installs kirocrew gateway as a systemd user service.
# Requires the systemd user manager to be running (see README.md Phase 1).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USERNAME=$(whoami)
HOSTNAME=$(hostname)

echo "👻 KiroCrew Persistent Sessions Setup"
echo ""

# ── Check: systemd user manager running? ──
if ! systemctl --user status >/dev/null 2>&1; then
    echo "❌ Systemd user manager is not running."
    echo "   Complete Phase 1 in README.md first (requires sudo, one-time)."
    exit 1
fi
echo "✅ Systemd user manager running"

# ── Check: kirocrew gateway already running in tmux? ──
if pgrep -f "kiro_crew gateway\|kirocrew gateway" | grep -v $$ >/dev/null 2>&1; then
    echo ""
    echo "⚠️  kirocrew gateway is already running (tmux or manual)."
    echo "   Kill it first: tmux kill-session -t kirocrew"
    echo "   Then re-run this script."
    exit 1
fi

# ── Install user service ──
echo "→ Installing kirocrew user service..."
USER_UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_UNIT_DIR"
NODE_VERSION=$(node --version 2>/dev/null || basename "$(ls -d "$HOME"/.nvm/versions/node/v* 2>/dev/null | tail -1)")

# Resolve kirocrew binary from current shell PATH
KIROCREW_BIN="$(command -v kirocrew 2>/dev/null)" || { echo "❌ kirocrew not found in PATH"; exit 1; }
echo "  Binary: $KIROCREW_BIN"

sed -e "s/%u/$USERNAME/g" \
    -e "s|KIROCREW_BIN|$KIROCREW_BIN|g" \
    -e "s/NVM_NODE_VERSION/$NODE_VERSION/g" \
    "$SCRIPT_DIR/kirocrew.service" > "$USER_UNIT_DIR/kirocrew.service"

systemctl --user daemon-reload
systemctl --user enable kirocrew
systemctl --user start kirocrew

echo ""
systemctl --user status kirocrew --no-pager || true

# ── Mac instructions ──
echo ""
echo "━━━ Mac Setup (run on your laptop) ━━━"
echo ""
echo "scp $USERNAME@$HOSTNAME:$SCRIPT_DIR/com.kirocrew.tunnel.plist ~/Library/LaunchAgents/"
echo "sed -i '' 's|ALIAS@DEV_DESKTOP_HOSTNAME|$USERNAME@$HOSTNAME|g' ~/Library/LaunchAgents/com.kirocrew.tunnel.plist"
echo "launchctl load ~/Library/LaunchAgents/com.kirocrew.tunnel.plist"
echo ""
echo "Done! Dashboard: http://localhost:5476"
