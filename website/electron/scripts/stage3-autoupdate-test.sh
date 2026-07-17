#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Stage 3 — end-to-end Squirrel.Mac auto-update test (NO Apple notarization).
#
# Builds v1.0.0 (the "installed" app) and v1.0.1 (the "update"), ad-hoc
# deep-signs both, serves the 1.0.1 zip from a local feed, and prints how to
# run + what to observe. Proves the full loop: check -> download -> prompt ->
# graceful gateway stop -> bundle swap -> relaunch as 1.0.1.
#
# CAVEATS (read before running):
#  - Same-machine only. Ad-hoc signatures (codesign -s -) satisfy Squirrel.Mac's
#    same-identity check for a locally-built, locally-run app. Gatekeeper
#    acceptance on a CLEAN machine still requires real notarization (Stage 4).
#  - The app spawns the bundled/PATH gateway on launch and only reaches
#    initAutoUpdate after it connects. Have `kirocrew` installed (toolbox) so a
#    gateway can start. We isolate with a temp KIROCREW_HOME + the BETA flavor
#    (port 7788) so this never touches your real :7777 gateway.
#  - The feed is http://127.0.0.1 (loopback is exempt from App Transport
#    Security). If Squirrel still refuses the http feed, add to package.json
#    build.mac:  "extendInfo": { "NSAppTransportSecurity": { "NSAllowsLocalNetworking": true } }
#
# Env overrides: PORT (8799), OUT (/tmp/kirocrew-stage3), NODE (node).
# ============================================================================

HERE="$(cd "$(dirname "$0")" && pwd)"
ELECTRON="$(cd "$HERE/.." && pwd)"
cd "$ELECTRON"

PORT="${PORT:-8799}"
OUT="${OUT:-/tmp/kirocrew-stage3}"
NODE="${NODE:-node}"
INSTALLED_VER="1.0.0"
UPDATE_VER="1.0.1"

command -v codesign >/dev/null || { echo "ERROR: codesign not found (Xcode CLT)"; exit 1; }
command -v ditto >/dev/null     || { echo "ERROR: ditto not found"; exit 1; }
[ -d node_modules ] || { echo "ERROR: run 'bb install' (or npm ci) in $ELECTRON first"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT/update" "$OUT/installed" "$OUT/home"

adhoc_sign_app() {  # $1 = path to .app — deep ad-hoc sign + verify
  echo ">>> ad-hoc deep-signing $(basename "$1")"
  codesign --remove-signature "$1" 2>/dev/null || true
  codesign --deep --force --options runtime --sign - "$1"
  codesign --verify --deep --strict "$1" && echo "    signature verified"
}

build_dir() {  # $1 = version -> unpacked .app in dist/ ; echoes the .app path
  echo ">>> building $1 (dir target)" >&2
  rm -rf dist
  CSC_IDENTITY_AUTO_DISCOVERY=false npx electron-builder --mac dir \
    -c.extraMetadata.version="$1" >&2
  /usr/bin/find dist -maxdepth 3 -name '*.app' -type d | head -1
}

# 1. UPDATE artifact (1.0.1): build -> sign -> ditto-zip into OUT/update
UPD_APP="$(build_dir "$UPDATE_VER")"
[ -n "$UPD_APP" ] || { echo "ERROR: no .app produced for update build"; exit 1; }
adhoc_sign_app "$UPD_APP"
UPD_ZIP="$OUT/update/Kiro-$UPDATE_VER-mac.zip"
echo ">>> packaging update zip (ditto)"
ditto -c -k --sequesterRsrc --keepParent "$UPD_APP" "$UPD_ZIP"
echo "    update zip: $UPD_ZIP"

# 2. INSTALLED app (1.0.0): build -> sign -> copy into OUT/installed
INST_SRC="$(build_dir "$INSTALLED_VER")"
[ -n "$INST_SRC" ] || { echo "ERROR: no .app produced for installed build"; exit 1; }
adhoc_sign_app "$INST_SRC"
cp -R "$INST_SRC" "$OUT/installed/"
INST_APP="$OUT/installed/$(basename "$INST_SRC")"
INST_BIN="$INST_APP/Contents/MacOS/$(/usr/bin/basename "$INST_APP" .app)"
echo "    installed app: $INST_APP"
rm -rf dist

# 3. local feed (loopback only)
echo ">>> starting local feed on 127.0.0.1:$PORT"
"$NODE" "$HERE/local-feed-server.js" --port "$PORT" --zip "$UPD_ZIP" --version "$UPDATE_VER" &
FEED_PID=$!
trap 'kill $FEED_PID 2>/dev/null || true' EXIT
sleep 1

cat <<MSG

============================ Stage 3 ready ============================
Feed:      http://127.0.0.1:$PORT   (serving $UPDATE_VER)
Installed: $INST_APP   (version $INSTALLED_VER)

Run the installed app pointed at the local feed (new terminal, or here):

  KIROCREW_UPDATE_FEED="http://127.0.0.1:$PORT" \\
  KIROCREW_FLAVOR=beta \\
  KIROCREW_HOME="$OUT/home" \\
  "$INST_BIN"

What to observe (app stderr or Console.app, filter "[update]"):
  [update] feed: http://127.0.0.1:$PORT?platform=...&channel=insider&version=1.0.0
  [update] checking…  ->  update available — downloading…  ->  downloaded 1.0.1 — prompting
  (dialog) "KiroCrew 1.0.1 is ready to install."  -> click "Restart & Update"
  "Stopping gateway gracefully..."  -> bundle swap -> app relaunches as 1.0.1
  Re-open: [update] up to date   (feed returns 204)

The feed server logs each request (200 update / 204 up-to-date / served zip).
Press Ctrl-C to stop the feed server.
======================================================================
MSG
wait $FEED_PID
