#!/usr/bin/env bash
# sign-dmg.sh -- Developer ID sign a DMG via CDSigner (second signing task).
#
# hdiutil-created DMGs carry an adhoc signature, which the Apple notary
# service Accepts but Gatekeeper rejects ("app is damaged" when dragging
# the app out of a quarantined mount). A Developer ID signature on the DMG
# itself fixes that -- and additionally makes the DMG staple-able, so
# first-install verification works fully offline (stapler fails with
# Error 73 on unsigned DMGs).
#
# Mirrors sign.sh's proven flow (package -> upload -> submit -> poll ->
# download) with a `type: dmg` manifest instead of the app manifest.
# Manifest syntax per StoreGenVector docs/build/signing-runbook.md and
# the CDSigner API guide; flow per AIWQuilloWord code-signing-guide.md.
#
# Usage: sign-dmg.sh <dmg-path> <channel> <version>
#
# Env: AWS_SIGNING_BUCKET, AWS_SIGNER_ROLE_ARN, CDSIGNER_API_ENDPOINT
# (same contract as sign.sh). Requires awscurl on PATH and AWS credentials
# in the environment (OIDC role).
#
# On success the signed DMG REPLACES the file at <dmg-path>.

set -euo pipefail

DMG_PATH="${1:-}"
CHANNEL="${2:-}"
VERSION="${3:-}"
# Default to the ONBOARDED app identifier: the signing service's authz is
# per-identifier -- an unfamiliar identifier (e.g. com.amazon.kiro.crew.dmg) is
# rejected, whereas the onboarded app identifier signs successfully. The
# bundle-id rename is a separate coordinated task (re-onboard the signing
# identity first) -- do NOT change it as part of a string scrub.
DMG_IDENTIFIER="${DMG_IDENTIFIER:-com.amazon.kiro.crew}"

if [ -z "$DMG_PATH" ] || [ -z "$CHANNEL" ] || [ -z "$VERSION" ]; then
  echo "Usage: $0 <dmg-path> <channel> <version>" >&2
  exit 1
fi

if [ ! -f "$DMG_PATH" ]; then
  echo "ERROR: DMG not found at $DMG_PATH" >&2
  exit 1
fi

# ── Env ─────────────────────────────────────────────────────────────────────
: "${AWS_SIGNING_BUCKET:?Set AWS_SIGNING_BUCKET}"
: "${AWS_SIGNER_ROLE_ARN:?Set AWS_SIGNER_ROLE_ARN}"
: "${CDSIGNER_API_ENDPOINT:?Set CDSIGNER_API_ENDPOINT}"

DMG_NAME="$(basename "$DMG_PATH")"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

INPUT_KEY="pre-signed/${CHANNEL}/${VERSION}/${DMG_NAME}.tar.gz"
OUTPUT_KEY="signed/${CHANNEL}/${VERSION}/${DMG_NAME}"

log() { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }

# ── 1. Package ──────────────────────────────────────────────────────────────
# Same metadata-clean tar recipe as sign.sh: on macOS, suppress AppleDouble
# (._*) entries and xattr/ACL/flag metadata -- bsdtar embeds them by default
# and CDSigner's artifact security scan rejects archives containing them.
TAR_PATH="$WORK_DIR/${DMG_NAME}.tar.gz"
TAR_FLAGS=()
if [ "$(uname)" = "Darwin" ]; then
  TAR_FLAGS=(--no-xattrs --no-mac-metadata --no-acls --no-fflags)
fi
( cd "$(dirname "$DMG_PATH")" && COPYFILE_DISABLE=1 tar "${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"}" -czf "$TAR_PATH" "$DMG_NAME" )

# ── 2. Upload ───────────────────────────────────────────────────────────────
log "Uploading to s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}..."
aws s3 cp "$TAR_PATH" "s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}" --quiet || {
  echo "ERROR: upload failed" >&2
  exit 3
}

# ── 3. Submit signing request ───────────────────────────────────────────────
log "Submitting CDSigner DMG sign task..."

# Manifest shape per the OFFICIAL CDSigner API guide ("macOS application
# (.app, .pkg and .dmg) signing"): DMGs sign as type "app" with the DMG as
# the output path -- the live API enumerates valid types and "dmg" is NOT
# one (a "type: dmg" submission is rejected with a 400; proven by nightly
# run 29884088951). The DMG must already be read-only UDZO, which the
# build step guarantees (hdiutil create -format UDZO).
REQUEST=$(python3 - "$DMG_NAME" "$DMG_IDENTIFIER" "$AWS_SIGNER_ROLE_ARN" "$AWS_SIGNING_BUCKET" "$INPUT_KEY" "$OUTPUT_KEY" <<'PYEOF'
import json, sys
name, identifier, role, bucket, in_key, out_key = sys.argv[1:7]
print(json.dumps({
    "manifest": {
        "type": "app",
        "os": "osx",
        "name": name,
        "outputs": [{"label": "macos", "path": name}],
        "app": {
            "identifier": identifier,
            "signing_requirements": {
                "certificate_type": "developerIDAppDistribution",
                "team_id": "94KV3E626L",
            },
        },
    },
    "s3ArtifactLocations": {
        "bucketAccessRole": role,
        "bucket": bucket,
        "inputKey": in_key,
        "outputKey": out_key,
    },
}))
PYEOF
)

if ! command -v awscurl >/dev/null 2>&1; then
  echo "ERROR: awscurl not found (required for CDSigner SigV4 signing)" >&2
  exit 4
fi

RESPONSE=$(awscurl --service signer-builder-tools --region us-west-2 \
  -X POST -H "Content-Type: application/json" -d "$REQUEST" \
  "${CDSIGNER_API_ENDPOINT}/v2/sign-tasks" 2>&1) || {
  echo "ERROR: CDSigner DMG sign-task submission failed" >&2
  echo "$RESPONSE" >&2
  exit 4
}

SIGN_TASK_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['signTaskId'])" 2>/dev/null) || {
  echo "ERROR: CDSigner submission returned no signTaskId:" >&2
  echo "$RESPONSE" >&2
  exit 4
}

log "Sign task submitted: ${SIGN_TASK_ID}"

# ── 4. Poll for completion ──────────────────────────────────────────────────
log "Polling for completion (timeout: 15 min)..."

MAX_WAIT="${SIGN_DMG_MAX_WAIT:-900}"
POLL_INTERVAL="${SIGN_DMG_POLL_INTERVAL:-30}"
ELAPSED=0
SIGNED_OK=0

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  STATUS_RESPONSE=$(awscurl --service signer-builder-tools --region us-west-2 \
    -X GET "${CDSIGNER_API_ENDPOINT}/v2/sign-tasks/${SIGN_TASK_ID}" \
    2>/dev/null) || continue

  STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

  case "$STATUS" in
    success)
      log "DMG signing completed! (${ELAPSED}s)"
      SIGNED_OK=1
      break
      ;;
    failure)
      echo "ERROR: DMG signing failed" >&2
      echo "$STATUS_RESPONSE" >&2
      exit 4
      ;;
    *)
      printf "  [%ds] status: %s\n" "$ELAPSED" "${STATUS:-<none>}"
      ;;
  esac
done

# Gate on the explicit success flag, not elapsed time: success arriving
# exactly on the final poll tick must not be misread as a timeout.
if [ "$SIGNED_OK" -ne 1 ]; then
  echo "ERROR: DMG signing timed out after ${MAX_WAIT}s (sign task: ${SIGN_TASK_ID})" >&2
  exit 5
fi

# ── 5. Download signed DMG ──────────────────────────────────────────────────
log "Downloading signed DMG..."

SIGNED_PATH="$WORK_DIR/${DMG_NAME}.signed"
aws s3 cp "s3://${AWS_SIGNING_BUCKET}/${OUTPUT_KEY}" "$SIGNED_PATH" --quiet || {
  echo "ERROR: Failed to download signed DMG" >&2
  exit 3
}

# CDSigner returns the app task's output re-packaged as a zip; the dmg task
# output format is expected to be the DMG itself, but tolerate a zip wrapper
# (first live run confirms which). Fail closed on anything else.
FILE_TYPE=$(file -b "$SIGNED_PATH")
case "$FILE_TYPE" in
  *"Zip archive"*)
    log "Output is a zip wrapper -- extracting the DMG..."
    unzip -q -o "$SIGNED_PATH" -d "$WORK_DIR/unwrapped"
    INNER=$(find "$WORK_DIR/unwrapped" -name "*.dmg" | head -1)
    [ -n "$INNER" ] || { echo "ERROR: no .dmg inside signed output zip" >&2; exit 3; }
    mv "$INNER" "$SIGNED_PATH"
    ;;
  *) : ;;  # assume raw DMG; the codesign gate below fails closed if not
esac

# ── 6. Fail-closed signature gate ───────────────────────────────────────────
# The whole point: the DMG must now carry a VALID Developer ID signature (an
# adhoc or missing signature reproduces the "app is damaged" defect).
# NOTE: Authority lines are only emitted at -dvvv verbosity; plain -dv omits
# them entirely, which would make this gate reject every valid DMG.
if ! codesign --verify --strict "$SIGNED_PATH" 2>&1; then
  echo "ERROR: signed DMG failed codesign verification -- failing closed." >&2
  exit 6
fi
AUTHORITY=$(codesign -dvvv "$SIGNED_PATH" 2>&1 | grep "^Authority=" | head -1 || true)
log "DMG signature: ${AUTHORITY:-<none>}"
if ! echo "$AUTHORITY" | grep -q "Developer ID Application"; then
  echo "ERROR: signed DMG does not carry a Developer ID signature -- failing closed." >&2
  codesign -dvvv "$SIGNED_PATH" 2>&1 | head -8 >&2 || true
  exit 6
fi

mv "$SIGNED_PATH" "$DMG_PATH"
log "Signed DMG in place: ${DMG_PATH} ($(du -h "$DMG_PATH" | cut -f1))"
