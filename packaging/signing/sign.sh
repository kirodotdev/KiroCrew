#!/usr/bin/env bash
# Sign a KiroCrew .app bundle via CDSigner.
#
# Usage:
#   bash packaging/signing/sign.sh <app-path> <channel> <version>
#
# Example:
#   bash packaging/signing/sign.sh website/electron/dist/mac-arm64/KiroCrew.app nightly 0.2.0-nightly.20260708
#
# Environment variables (required):
#   AWS_SIGNING_BUCKET     — S3 bucket for signing artifacts
#   AWS_SIGNER_ROLE_ARN    — Role ARN CDSigner assumes to read/write S3
#   CDSIGNER_API_ENDPOINT  — CDSigner API Gateway endpoint URL
#
# The script:
#   1. Packages the .app into a tar.gz with entitlements metadata
#   2. Uploads to pre-signed/{channel}/{version}/ in S3
#   3. Submits a signing request to CDSigner API
#   4. Polls until signing + notarization completes
#   5. Downloads the signed artifact to signed/ locally
#   6. Verifies the signature
#
# Exit codes:
#   0 — success, signed artifact at signed/{app-name}.zip
#   1 — usage error or missing env
#   2 — packaging failed
#   3 — upload failed
#   4 — signing request failed
#   5 — signing timed out (>15 min)
#   6 — verification failed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Args ────────────────────────────────────────────────────────────────────
APP_PATH="${1:-}"
CHANNEL="${2:-}"
VERSION="${3:-}"

if [ -z "$APP_PATH" ] || [ -z "$CHANNEL" ] || [ -z "$VERSION" ]; then
  echo "Usage: $0 <app-path> <channel> <version>" >&2
  exit 1
fi

if [ ! -d "$APP_PATH" ]; then
  echo "ERROR: .app not found at $APP_PATH" >&2
  exit 1
fi

# ── Env ─────────────────────────────────────────────────────────────────────
: "${AWS_SIGNING_BUCKET:?Set AWS_SIGNING_BUCKET}"
: "${AWS_SIGNER_ROLE_ARN:?Set AWS_SIGNER_ROLE_ARN}"
: "${CDSIGNER_API_ENDPOINT:?Set CDSIGNER_API_ENDPOINT}"

APP_NAME="$(basename "$APP_PATH" .app)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

INPUT_KEY="pre-signed/${CHANNEL}/${VERSION}/${APP_NAME}.tar.gz"
OUTPUT_KEY="signed/${CHANNEL}/${VERSION}/${APP_NAME}.zip"

log() { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }

# ── 1. Package ──────────────────────────────────────────────────────────────
log "Packaging ${APP_NAME}.app for signing..."

PACKAGE_DIR="$WORK_DIR/package"
mkdir -p "$PACKAGE_DIR/SIGNING_METADATA"

# Copy the .app
cp -R "$APP_PATH" "$PACKAGE_DIR/${APP_NAME}.app"

# Copy entitlements
cp "$SCRIPT_DIR/Entitlements.entitlements" "$PACKAGE_DIR/SIGNING_METADATA/Entitlements.entitlements"

# Create tar.gz
TAR_PATH="$WORK_DIR/${APP_NAME}.tar.gz"
( cd "$PACKAGE_DIR" && tar czf "$TAR_PATH" "${APP_NAME}.app" SIGNING_METADATA/ )

TAR_SIZE=$(du -h "$TAR_PATH" | cut -f1)
log "Package created: ${TAR_SIZE}"

# ── 2. Upload ───────────────────────────────────────────────────────────────
log "Uploading to s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}..."

aws s3 cp "$TAR_PATH" "s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}" --quiet || {
  echo "ERROR: S3 upload failed" >&2
  exit 3
}

# ── 3. Submit signing request ───────────────────────────────────────────────
log "Submitting CDSigner request..."

# Build manifest from template
MANIFEST=$(cat "$SCRIPT_DIR/manifest-template.json" \
  | sed "s|\${SIGNER_ACCESS_ROLE_ARN}|${AWS_SIGNER_ROLE_ARN}|g" \
  | sed "s|\${SIGNING_BUCKET}|${AWS_SIGNING_BUCKET}|g" \
  | sed "s|\${INPUT_KEY}|${INPUT_KEY}|g" \
  | sed "s|\${OUTPUT_KEY}|${OUTPUT_KEY}|g")

RESPONSE=$(curl -sf -X POST "${CDSIGNER_API_ENDPOINT}/prod/v1/signing_requests" \
  --aws-sigv4 "aws:amz:us-west-2:signer-builder-tools" \
  -H "Content-Type: application/json" \
  -d "$MANIFEST") || {
  echo "ERROR: CDSigner API request failed" >&2
  echo "$RESPONSE" >&2
  exit 4
}

REQUEST_ID=$(echo "$RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['requestId'])" 2>/dev/null) || {
  echo "ERROR: Could not parse requestId from response: $RESPONSE" >&2
  exit 4
}

log "Signing request submitted: ${REQUEST_ID}"

# ── 4. Poll for completion ──────────────────────────────────────────────────
log "Polling for completion (timeout: 15 min)..."

MAX_WAIT=900  # 15 minutes
POLL_INTERVAL=30
ELAPSED=0

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
  sleep "$POLL_INTERVAL"
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  STATUS_RESPONSE=$(curl -sf "${CDSIGNER_API_ENDPOINT}/prod/v1/signing_requests/${REQUEST_ID}" \
    --aws-sigv4 "aws:amz:us-west-2:signer-builder-tools" \
    2>/dev/null) || continue

  STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

  case "$STATUS" in
    COMPLETED|completed)
      log "Signing completed! (${ELAPSED}s)"
      break
      ;;
    FAILED|failed)
      echo "ERROR: Signing failed" >&2
      echo "$STATUS_RESPONSE" >&2
      exit 4
      ;;
    *)
      printf "  [%ds] status: %s\n" "$ELAPSED" "$STATUS"
      ;;
  esac
done

if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
  echo "ERROR: Signing timed out after ${MAX_WAIT}s (request: ${REQUEST_ID})" >&2
  exit 5
fi

# ── 5. Download signed artifact ─────────────────────────────────────────────
log "Downloading signed artifact..."

SIGNED_DIR="signed"
mkdir -p "$SIGNED_DIR"
SIGNED_PATH="${SIGNED_DIR}/${APP_NAME}.zip"

aws s3 cp "s3://${AWS_SIGNING_BUCKET}/${OUTPUT_KEY}" "$SIGNED_PATH" --quiet || {
  echo "ERROR: Failed to download signed artifact" >&2
  exit 3
}

log "Signed artifact: ${SIGNED_PATH} ($(du -h "$SIGNED_PATH" | cut -f1))"

# ── 6. Verify (macOS only) ──────────────────────────────────────────────────
if [ "$(uname -s)" = "Darwin" ]; then
  log "Verifying signature..."
  VERIFY_DIR="$WORK_DIR/verify"
  mkdir -p "$VERIFY_DIR"
  SIGNED_PATH_ABS="$(cd "$(dirname "$SIGNED_PATH")" && pwd)/$(basename "$SIGNED_PATH")"
  ( cd "$VERIFY_DIR" && unzip -q "$SIGNED_PATH_ABS" )

  VERIFY_APP=$(find "$VERIFY_DIR" -name "*.app" -maxdepth 1 | head -1)
  if [ -n "$VERIFY_APP" ]; then
    codesign --verify --deep --strict "$VERIFY_APP" && log "codesign: VALID" || {
      echo "WARNING: codesign verification failed" >&2
      exit 6
    }
    spctl --assess --type execute "$VERIFY_APP" && log "spctl: ACCEPTED" || {
      echo "WARNING: spctl assessment failed (notarization may not be stapled)" >&2
    }
  fi
fi

log "Done. Signed artifact: ${SIGNED_PATH}"
echo "$SIGNED_PATH"
