#!/bin/bash
# Install the demo app to the data home's apps/ dir so the full pipeline can be tested.
# Run this once, then navigate to /apps/demo-app in the KiroCrew dashboard.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_SOURCE="$SCRIPT_DIR/../website/public/apps/demo-app"
APP_DEST="${KIROCREW_HOME:-$HOME/.kiro/crew}/apps/demo-app"

if [ -d "$APP_DEST" ]; then
    echo "Demo app already installed at $APP_DEST"
    echo "Updating files..."
    rm -rf "$APP_DEST/ui"
fi

mkdir -p "$APP_DEST/ui"
cp "$APP_SOURCE/app.json" "$APP_DEST/"
cp "$APP_SOURCE/ui/index.mjs" "$APP_DEST/ui/"

# Write installed.json metadata
cat > "$APP_DEST/installed.json" << 'EOF'
{
  "name": "demo-app",
  "version": "0.1.0",
  "displayName": "Demo App",
  "enabled": true,
  "installedAt": "2026-04-19T00:00:00Z",
  "source": "built-in"
}
EOF

echo "✓ Demo app installed to $APP_DEST"
echo "  Navigate to /apps/demo-app in the KiroCrew dashboard to test."
echo ""
echo "  The app will:"
echo "  - Load dynamically via import('/apps/demo-app/ui/index.mjs')"
echo "  - Use the host's React via the import map"
echo "  - Call /api/agents via the permission-scoped SDK"
echo "  - Render with the host's theme (CSS custom properties)"
