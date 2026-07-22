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
#   4. Polls until signing completes (CDSigner signs only; notarization is a
#      separate post-signing step via notarytool)
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

# Strip pre-existing ad-hoc signatures from nested Mach-Os (macOS only --
# codesign is unavailable elsewhere). electron-builder and the Python
# runtime ship arm64 binaries with mandatory linker ad-hoc signatures;
# stripping them first lets CDSigner apply clean Developer ID signatures
# to every explicitly-listed embedded binary.
if [ "$(uname -s)" = "Darwin" ]; then
  STRIPPED=0
  while IFS= read -r MACHO; do
    codesign --remove-signature "$MACHO" 2>/dev/null && STRIPPED=$((STRIPPED + 1)) || true
  done < <(python3 "$SCRIPT_DIR/generate-manifest.py" --list-machos "$PACKAGE_DIR/${APP_NAME}.app")
  log "Stripped ad-hoc signatures from ${STRIPPED} nested Mach-O binaries"
fi

# Copy entitlements
cp "$SCRIPT_DIR/Entitlements.entitlements" "$PACKAGE_DIR/SIGNING_METADATA/Entitlements.entitlements"

# Create tar.gz. On macOS, suppress AppleDouble (._*) entries and
# xattr/ACL/flag metadata -- bsdtar embeds them by default and CDSigner's
# artifact security scan rejects archives containing them. GNU tar on the
# Linux CI runners never emits this metadata (flags kept Darwin-only since
# GNU tar does not know --no-mac-metadata).
TAR_PATH="$WORK_DIR/${APP_NAME}.tar.gz"
TAR_FLAGS=()
if [ "$(uname -s)" = "Darwin" ]; then
  TAR_FLAGS=(--no-xattrs --no-mac-metadata --no-acls --no-fflags)
fi
( cd "$PACKAGE_DIR" && COPYFILE_DISABLE=1 tar "${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"}" -czf "$TAR_PATH" "${APP_NAME}.app" SIGNING_METADATA/ )

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

# Build the manifest with full nested Mach-O coverage. Notarization requires
# every nested binary (embedded Python backend, Squirrel ShipIt) to be
# Developer-ID signed; generate-manifest.py enumerates them from the actual
# .app so the list never goes stale as backend dependencies change.
MANIFEST=$(SIGNER_ACCESS_ROLE_ARN="${AWS_SIGNER_ROLE_ARN}" \
  SIGNING_BUCKET="${AWS_SIGNING_BUCKET}" \
  INPUT_KEY="${INPUT_KEY}" \
  OUTPUT_KEY="${OUTPUT_KEY}" \
  python3 "$SCRIPT_DIR/generate-manifest.py" \
    "$SCRIPT_DIR/manifest-template.json" \
    "$PACKAGE_DIR/${APP_NAME}.app") || {
  echo "ERROR: manifest generation failed" >&2
  exit 4
}

# CD Signer ad-hoc signing API v2: POST /v2/sign-tasks. awscurl SigV4-signs
# from the AWS credential chain (env vars, incl. AWS_SESSION_TOKEN) -- no
# credentials on the command line. The full response body is surfaced on
# failure so auth/manifest/Bindle errors stay diagnosable.
if ! command -v awscurl >/dev/null 2>&1; then
  echo "ERROR: awscurl not found (required for CDSigner SigV4 signing)" >&2
  exit 1
fi

RESPONSE=$(awscurl --service signer-builder-tools --region us-west-2 \
  -X POST -H "Content-Type: application/json" -d "$MANIFEST" \
  "${CDSIGNER_API_ENDPOINT}/v2/sign-tasks" 2>&1) || {
  echo "ERROR: CDSigner sign-task submission failed" >&2
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

MAX_WAIT=900  # 15 minutes
POLL_INTERVAL=30
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
      log "Signing completed! (${ELAPSED}s)"
      SIGNED_OK=1
      break
      ;;
    failure)
      echo "ERROR: Signing failed" >&2
      echo "$STATUS_RESPONSE" >&2
      exit 4
      ;;
    created|processing|inProgress)
      printf "  [%ds] status: %s\n" "$ELAPSED" "$STATUS"
      ;;
    *)
      printf "  [%ds] status: %s\n" "$ELAPSED" "${STATUS:-<none>}"
      ;;
  esac
done

# Gate on the explicit success flag, not elapsed time: success arriving
# exactly on the final poll tick must not be misread as a timeout.
if [ "$SIGNED_OK" -ne 1 ]; then
  echo "ERROR: Signing timed out after ${MAX_WAIT}s (sign task: ${SIGN_TASK_ID})" >&2
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
