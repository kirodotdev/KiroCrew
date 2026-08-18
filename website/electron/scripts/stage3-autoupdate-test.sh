#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# End-to-end macOS auto-update test via electron-updater (NO Apple notarization).
#
# Builds v1.0.0 (the "installed" app) and v1.0.1 (the "update"), signed with a
# deep-signs both, serves the 1.0.1 zip from a local yml feed, and prints how to
# run + what to observe. Proves the full loop: check -> CONSENT -> download ->
# graceful gateway stop -> bundle swap -> relaunch as 1.0.1.
#
# CAVEATS (read before running):
#  - Same-machine only. A throwaway SELF-SIGNED identity (not ad-hoc) satisfies Squirrel.Mac's
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

# --- Signing identity -------------------------------------------------------
#
# WHY NOT AD-HOC: Squirrel.Mac verifies that the DOWNLOADED bundle satisfies the
# RUNNING app's designated code requirement (SQRLCodeSignature). An ad-hoc
# signature has no signing identity, so its designated requirement degrades to
# the bundle's own cdhash -- which is by definition different for a different
# build. The update can therefore never satisfy it, and Squirrel refuses the
# install with:
#
#   Code signature at URL .../KiroCrew.app/ did not pass validation:
#   code failed to satisfy specified code requirement(s)
#   (domain: SQRLCodeSignatureErrorDomain, code: -1)
#
# Observed live in ota-test.yml run 30490303783. The previous note here claimed
# ad-hoc signatures satisfy Squirrel; that was never true and made this harness
# unable to prove the one thing it exists to prove.
#
# THE FIX: mint a throwaway self-signed CODE-SIGNING certificate in a temporary
# keychain and sign BOTH bundles with it. Same identity + same bundle ID => the
# update satisfies the running app's requirement, and the swap mechanism is
# exercised for real. Still no secrets and no Developer ID: this validates the
# SWAP, not Gatekeeper acceptance or notarization (those live in the signing
# lane). The keychain is deleted on exit.
OTA_KEYCHAIN="${TMPDIR:-/tmp}/ota-signing.keychain-db"
OTA_KEYCHAIN_PASS="ota-test"
OTA_IDENTITY="KiroCrew OTA Test Signing"

cleanup_keychain() {
  security delete-keychain "$OTA_KEYCHAIN" 2>/dev/null || true
  rm -f "${TMPDIR:-/tmp}/ota-signing".{key,crt,p12,cnf} 2>/dev/null || true
}
# Armed early so an abort during the build still tears the identity down.
# Superseded by cleanup_all once the feed server starts (see below).
trap cleanup_keychain EXIT

setup_signing_identity() {
  local d="${TMPDIR:-/tmp}"
  echo ">>> minting a throwaway self-signed code-signing identity"
  cleanup_keychain

  # extendedKeyUsage=codeSigning is REQUIRED: without it, codesign rejects the
  # identity ("no identity found" / unsupported for signing).
  cat > "$d/ota-signing.cnf" <<'CNF'
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = KiroCrew OTA Test Signing
[v3]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF

  openssl req -x509 -newkey rsa:2048 -nodes -days 3 -sha256 \
    -keyout "$d/ota-signing.key" -out "$d/ota-signing.crt" \
    -config "$d/ota-signing.cnf" >/dev/null 2>&1
  # LEGACY PKCS#12 ALGORITHMS ARE MANDATORY. OpenSSL 3 defaults to AES-256-CBC
  # with a SHA-256 MAC, which macOS's Security framework cannot read: the import
  # dies with "SecKeychainItemImport: MAC verification failed during PKCS12
  # import (wrong password?)" -- a misleading message, since the password is
  # fine. Observed in ota-test.yml run 30490965078, where the runner's PATH
  # openssl is 3.x. Pinning SHA-1/3DES is understood by both OpenSSL 3 and the
  # LibreSSL that ships as /usr/bin/openssl. Weak crypto is irrelevant here: the
  # key is generated, used, and deleted inside one CI job.
  openssl pkcs12 -export -inkey "$d/ota-signing.key" -in "$d/ota-signing.crt" \
    -name "$OTA_IDENTITY" -out "$d/ota-signing.p12" -passout pass:"$OTA_KEYCHAIN_PASS" \
    -macalg sha1 -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES >/dev/null 2>&1 \
    || openssl pkcs12 -export -legacy -inkey "$d/ota-signing.key" -in "$d/ota-signing.crt" \
         -name "$OTA_IDENTITY" -out "$d/ota-signing.p12" -passout pass:"$OTA_KEYCHAIN_PASS" >/dev/null 2>&1

  # Fail LOUDLY here rather than letting codesign fail later with an unrelated
  # "no identity found": this step is the one that silently broke.
  [ -s "$d/ota-signing.p12" ] || { echo "ERROR: could not export a PKCS#12 for the signing identity"; return 1; }

  security create-keychain -p "$OTA_KEYCHAIN_PASS" "$OTA_KEYCHAIN"
  security unlock-keychain -p "$OTA_KEYCHAIN_PASS" "$OTA_KEYCHAIN"
  # Keep the login keychain in the search list or unrelated tooling breaks.
  security list-keychains -d user -s "$OTA_KEYCHAIN" $(security list-keychains -d user | tr -d '"')
  # -A so codesign can use the key without an interactive authorization prompt;
  # safe because the key is throwaway and the keychain is deleted on exit.
  security import "$d/ota-signing.p12" -k "$OTA_KEYCHAIN" -P "$OTA_KEYCHAIN_PASS" \
    -T /usr/bin/codesign -A >/dev/null \
    || { echo "ERROR: security import failed (openssl $(openssl version 2>/dev/null))"; return 1; }
  # Required on modern macOS or codesign still prompts and then fails headless.
  security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "$OTA_KEYCHAIN_PASS" "$OTA_KEYCHAIN" >/dev/null 2>&1

  # DELIBERATELY NO `security add-trusted-cert`. An earlier revision installed
  # this cert as a machine-wide code-signing trust anchor in
  # /Library/Keychains/System.keychain. That is a durable, per-machine
  # IRREVERSIBLE security regression for anyone running this script locally: the
  # macOS trust store is separate from the keychain, so deleting the keychain
  # does NOT revoke the trust entry, and the throwaway private key sits at a
  # predictable path. A trusted-for-code-signing root plus a readable key is a
  # code-signing oracle on a developer's machine.
  #
  # It is also unnecessary. Squirrel.Mac's check is a REQUIREMENT match, not a
  # trust evaluation: the designated requirement is
  # `identifier "..." and certificate leaf = H"<hash>"`, a hash comparison.
  # Verified locally -- with an UNTRUSTED self-signed identity, `codesign
  # --sign` succeeds, `codesign --verify --deep --strict` passes, and
  # cross-verifying one bundle against the other's designated requirement
  # passes; re-signing either bundle ad-hoc makes that same check fail. So
  # sharing one identity is what matters, and `security find-identity -v`
  # reporting "0 valid identities" (it filters on trust) is expected and
  # harmless here.

  # No -v: it filters to TRUSTED identities and this cert is deliberately
  # untrusted, so -v would print "0 valid identities" and hide the real one.
  security find-identity -p codesigning "$OTA_KEYCHAIN" | sed 's/^/    /'
}

sign_app() {  # $1 = path to .app — deep sign with the shared identity + verify
  echo ">>> deep-signing $(basename "$1") as \"$OTA_IDENTITY\""
  codesign --remove-signature "$1" 2>/dev/null || true
  # NO --options runtime here. Hardened runtime enables LIBRARY VALIDATION,
  # which requires the main binary and every loaded library to share a Team ID.
  # A self-signed cert has no Team ID, so a hardened app + this Electron
  # Framework is rejected by dyld with "different Team IDs" and the app dies on
  # launch with Abort trap: 6. Real CI builds sign with a Developer ID (one
  # team, runtime hardening on); this path is local/CI test-only and the bundle
  # swap under test does not depend on hardened runtime.
  codesign --deep --force --keychain "$OTA_KEYCHAIN" --sign "$OTA_IDENTITY" "$1"
  codesign --verify --deep --strict "$1" && echo "    signature verified"
  # Prove BOTH bundles will share a requirement -- the property the swap needs.
  codesign -d -r- "$1" 2>&1 | grep -E "^designated" | sed 's/^/    /' || true
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
    # updaterCacheDirName is DERIVED, exactly as app-builder-lib derives it
    # (appInfo.updaterCacheDirName = sanitizedName.toLowerCase() + "-updater"):
    # a hardcoded copy silently stops matching the real cache dir after a
    # package rename, and this stub's whole purpose is to mirror what the real
    # build emits.
    local cache_dir_name
    cache_dir_name="$("$NODE" -p "require('./package.json').name.toLowerCase() + '-updater'")"
    cat > "$app/Contents/Resources/app-update.yml" <<YML
provider: generic
url: https://updates.crew.kiro.dev/feed/stable/
updaterCacheDirName: ${cache_dir_name}
YML
    echo "    wrote app-update.yml (dir target does not emit it)" >&2
  fi
  echo "$app"
}

# 1. UPDATE artifact (1.0.1): build -> sign -> ditto-zip into OUT/update
UPD_APP="$(build_dir "$UPDATE_VER")"
[ -n "$UPD_APP" ] || { echo "ERROR: no .app produced for update build"; exit 1; }
setup_signing_identity
sign_app "$UPD_APP"
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
sign_app "$INST_APP"
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
# ONE combined EXIT trap. Bash traps REPLACE rather than stack, so declaring a
# second `trap ... EXIT` here silently disarmed cleanup_keychain (declared
# above) on every default run -- leaving the throwaway keychain and its private
# key on disk. Both teardowns must live in the same handler.
cleanup_all() {
  [ -n "${FEED_PID:-}" ] && /bin/kill "$FEED_PID" 2>/dev/null || true
  cleanup_keychain
}
trap cleanup_all EXIT
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
