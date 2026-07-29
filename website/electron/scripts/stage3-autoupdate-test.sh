#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# End-to-end macOS auto-update test via electron-updater (NO Apple notarization).
#
# Builds v1.0.0 (the "installed" app) and v1.0.1 (the "update"), ad-hoc
# deep-signs both, serves the 1.0.1 zip from a local yml feed, and prints how to
# run + what to observe. Proves the full loop: check -> CONSENT -> download ->
# graceful gateway stop -> bundle swap -> relaunch as 1.0.1.
#
# CAVEATS (read before running):
#  - Same-machine only. Ad-hoc signatures (codesign -s -) satisfy Squirrel.Mac's
#    same-identity check for a locally-built, locally-run app. Gatekeeper
#    acceptance on a CLEAN machine still requires real notarization.
#  - CONSENT-FIRST: discovery never downloads (autoDownload=false), so this test
#    REQUIRES two human clicks in Settings > About: "Download", then
#    "Restart & Update". An unattended run will stop at "found" and look stuck —
#    that is the design working, not a hang.
#  - backend-dist: the packaged app resolves its gateway via find-bin.js, which
#    falls back to a PATH `kirocrew`. This script therefore stubs an EMPTY
#    backend-dist to satisfy electron-builder's extraResources (which errors on
#    a missing source dir) instead of building the ~500MB PyInstaller bundle.
#    Have `kirocrew` on PATH (toolbox install) so a gateway can start. We
#    isolate with a temp KIROCREW_HOME + the BETA flavor (port 7788) so this
#    never touches your real :7777 gateway.
#  - The feed is http://127.0.0.1 (loopback is exempt from App Transport
#    Security, and buildFeedBase permits http ONLY on loopback). If the updater
#    refuses the http feed, add to package.json build.mac:
#    "extendInfo": { "NSAppTransportSecurity": { "NSAllowsLocalNetworking": true } }
#
# Env overrides: PORT (8799), OUT (/tmp/kirocrew-ota), NODE (node).
# ============================================================================

HERE="$(cd "$(dirname "$0")" && pwd)"
ELECTRON="$(cd "$HERE/.." && pwd)"
cd "$ELECTRON"

PORT="${PORT:-8799}"
OUT="${OUT:-/tmp/kirocrew-ota}"
NODE="${NODE:-node}"
INSTALLED_VER="1.0.0"
UPDATE_VER="1.0.1"

command -v codesign >/dev/null || { echo "ERROR: codesign not found (Xcode CLT)"; exit 1; }
command -v ditto >/dev/null     || { echo "ERROR: ditto not found"; exit 1; }
[ -d node_modules ] || { echo "ERROR: run 'bb install' (or npm ci) in $ELECTRON first"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT/update" "$OUT/installed" "$OUT/home"

# electron-builder's extraResources errors if `from: backend-dist` is missing.
# We do NOT want the ~500MB PyInstaller bundle for a feed/swap test, and
# find-bin.js falls back to a PATH `kirocrew`, so an empty dir is sufficient.
# Only created if absent, and never deleted (a real bundle must survive).
if [ ! -d backend-dist ]; then
  echo ">>> stubbing empty backend-dist (gateway will resolve from PATH)"
  mkdir -p backend-dist
  echo "placeholder for local OTA testing; real bundles come from packaging/build-desktop.sh" \
    > backend-dist/.ota-test-placeholder
  command -v kirocrew >/dev/null || echo "    WARNING: no kirocrew on PATH — the app may not reach initAutoUpdate"
fi

adhoc_sign_app() {  # $1 = path to .app — deep ad-hoc sign + verify
  echo ">>> ad-hoc deep-signing $(basename "$1")"
  codesign --remove-signature "$1" 2>/dev/null || true
  # NO --options runtime here. Hardened runtime enables LIBRARY VALIDATION,
  # which requires the main binary and every loaded library to share a Team ID.
  # Ad-hoc signatures have no Team ID, so an ad-hoc app + ad-hoc Electron
  # Framework is rejected by dyld with "different Team IDs" and the app dies on
  # launch with Abort trap: 6. Real CI builds sign with a Developer ID (one
  # team, runtime hardening on); this ad-hoc path is local/CI test-only and the
  # bundle swap under test does not depend on hardened runtime.
  codesign --deep --force --sign - "$1"
  codesign --verify --deep --strict "$1" && echo "    signature verified"
}

build_dir() {  # $1 = version -> unpacked .app in dist/ ; echoes the .app path
  echo ">>> building $1 (dir target)" >&2
  rm -rf dist
  CSC_IDENTITY_AUTO_DISCOVERY=false npx electron-builder --mac dir \
    -c.extraMetadata.version="$1" >&2
  local app
  app="$(/usr/bin/find dist -maxdepth 3 -name '*.app' -type d | head -1)"
  # electron-builder writes app-update.yml ONLY for the dmg/zip targets on
  # darwin (PublishManager.js: it returns early unless targets include one of
  # them). This test uses the `dir` target because it is far faster, so the file
  # must be written by hand -- WITHOUT it, electron-updater's downloadUpdate()
  # rejects ENOENT while reading it, and this test would fail on the very defect
  # it exists to catch. Content mirrors what the real dmg+zip build emits; the
  # url is overridden at runtime by configureFeed()'s setFeedURL anyway.
  if [ -n "$app" ] && [ ! -f "$app/Contents/Resources/app-update.yml" ]; then
    cat > "$app/Contents/Resources/app-update.yml" <<YML
provider: generic
url: https://updates.crew.kiro.dev/feed/stable/
updaterCacheDirName: kirocrew-electron-mac-updater
YML
    echo "    wrote app-update.yml (dir target does not emit it)" >&2
  fi
  echo "$app"
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
# 2. INSTALLED app (1.0.0): build -> ditto into OUT/installed -> sign IN PLACE.
# Copy BEFORE signing and use ditto, not cp -R: cp does not reliably preserve
# the nested code-signature relationships in a .app, which produces a bundle
# that verifies at the source path and fails to load its framework at the
# destination. Signing at the final location avoids the question entirely.
INST_SRC="$(build_dir "$INSTALLED_VER")"
[ -n "$INST_SRC" ] || { echo "ERROR: no .app produced for installed build"; exit 1; }
ditto "$INST_SRC" "$OUT/installed/$(basename "$INST_SRC")"
INST_APP="$OUT/installed/$(basename "$INST_SRC")"
adhoc_sign_app "$INST_APP"
INST_BIN="$INST_APP/Contents/MacOS/$(/usr/bin/basename "$INST_APP" .app)"
echo "    installed app: $INST_APP"
rm -rf dist

# 3. local feed (loopback only).
#
# BUILD_ONLY=1 stops here: CI drives the feed itself, because this script's
# trap kills the feed when the script exits, so a non-blocking run would tear
# down the server the moment it returned. Interactive runs keep the old
# behaviour (start the feed, print instructions, block on it).
if [ "${BUILD_ONLY:-0}" = "1" ]; then
  echo ">>> BUILD_ONLY=1 — artifacts ready, not starting the feed"
  echo "INSTALLED_APP=$INST_APP"
  echo "INSTALLED_BIN=$INST_BIN"
  echo "UPDATE_ZIP=$UPD_ZIP"
  exit 0
fi

echo ">>> starting local feed on 127.0.0.1:$PORT"
"$NODE" "$HERE/local-feed-server.js" --port "$PORT" --zip "$UPD_ZIP" --version "$UPDATE_VER" &
FEED_PID=$!
trap 'kill $FEED_PID 2>/dev/null || true' EXIT
sleep 1

cat <<MSG

============================ OTA test ready ============================
Feed:      http://127.0.0.1:$PORT   (serving $UPDATE_VER as latest-mac.yml)
Installed: $INST_APP   (version $INSTALLED_VER)

Run the installed app pointed at the local feed (new terminal, or here):

  KIROCREW_UPDATE_FEED="http://127.0.0.1:$PORT/feed" \\
  KIROCREW_FLAVOR=beta \\
  KIROCREW_HOME="$OUT/home" \\
  "$INST_BIN" 2>&1 | tee $OUT/app.log

What to observe (filter "[update]"), and WHAT YOU MUST CLICK:

  1. [update] feed: http://127.0.0.1:$PORT/feed/stable/
     [update] checking…
     [update] found $UPDATE_VER (running $INSTALLED_VER) — awaiting user consent
     ^ Discovery STOPS here by design: autoDownload=false. A native
       notification points at Settings > About. This is NOT a hang.

  2. >>> CLICK: open Settings > About, press "Download".
     [update] user consented — downloading $UPDATE_VER
     (download-progress events drive the percentage in the card)
     [update] downloaded $UPDATE_VER — notifying UI

  3. >>> CLICK: press "Restart & Update".
     [update] stopping gateway before install
     [update] gateway down — quitAndInstall
     -> bundle swap -> app relaunches as $UPDATE_VER

  4. Verify the swap actually happened (do NOT accept "no error" as success):
       defaults read "$INST_APP/Contents/Info" CFBundleShortVersionString
       # must print $UPDATE_VER
     Re-open the app: [update] up to date

The feed server logs every request (yml fetch / zip download).
Press Ctrl-C to stop the feed server.
=======================================================================
MSG
wait $FEED_PID
