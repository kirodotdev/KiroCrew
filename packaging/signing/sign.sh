#!/usr/bin/env bash
# Sign a KiroCrew .app bundle via the enterprise signing service.
#
# Usage:
#   bash packaging/signing/sign.sh <app-path> <channel> <version>
#
# Example:
#   bash packaging/signing/sign.sh website/electron/dist/mac-arm64/KiroCrew.app nightly 0.2.0-nightly.20260708
#
# Environment variables (required):
#   AWS_SIGNING_BUCKET     — S3 bucket for signing artifacts
#   AWS_SIGNER_ROLE_ARN    — Role ARN the signing service assumes to read/write S3
#   CDSIGNER_API_ENDPOINT  — signing service API Gateway endpoint URL
#
# Environment variables (optional):
#   SIGN_KEY_SUFFIX        — appended to the app slug in BOTH bucket keys
#                            (pre-signed/.../<slug><suffix>.tar.gz and
#                            signed/.../<slug><suffix>.zip). The keys are
#                            otherwise derived from the .app name alone, so
#                            two builds of the same app for the same
#                            channel+version -- the universal and a
#                            single-arch DMG signed in one run -- would
#                            overwrite each other's request and read back
#                            whichever finished last. sign-and-notarize.yml
#                            passes "-arm64" / "-x64" on the single-arch
#                            legs and nothing on the universal one, so the
#                            universal keys are byte-identical to before.
#
# The script:
#   1. Packages the .app into a tar.gz with entitlements metadata
#   2. Uploads to pre-signed/{channel}/{version}/ in S3
#   3. Submits a signing request to the signing service API (a request body
#      over the service's size limit is signed in parts first; see
#      sign_in_parts and split-manifest.py)
#   4. Polls until signing completes (the service signs only; notarization is a
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
#   5 — signing timed out (>45 min)
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
# Space-free slug for bucket keys: the nightly bundle is "KiroCrew Nightly.app"
# and these keys flow into the CDSigner request JSON and URL paths.
APP_SLUG="${APP_NAME// /-}${SIGN_KEY_SUFFIX:-}"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

INPUT_KEY="pre-signed/${CHANNEL}/${VERSION}/${APP_SLUG}.tar.gz"
OUTPUT_KEY="signed/${CHANNEL}/${VERSION}/${APP_SLUG}.zip"

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
# stripping them first lets the signing service apply clean Developer ID signatures
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
# xattr/ACL/flag metadata -- bsdtar embeds them by default and the signing
# service's artifact security scan rejects archives containing them. GNU tar on the
# Linux CI runners never emits this metadata (flags kept Darwin-only since
# GNU tar does not know --no-mac-metadata).
TAR_PATH="$WORK_DIR/${APP_NAME}.tar.gz"
TAR_FLAGS=()
if [ "$(uname -s)" = "Darwin" ]; then
  TAR_FLAGS=(--no-xattrs --no-mac-metadata --no-acls --no-fflags)
fi
pack_tar() {
  ( cd "$PACKAGE_DIR" && COPYFILE_DISABLE=1 tar "${TAR_FLAGS[@]+"${TAR_FLAGS[@]}"}" -czf "$1" "${APP_NAME}.app" SIGNING_METADATA/ )
}
pack_tar "$TAR_PATH"

TAR_SIZE=$(du -h "$TAR_PATH" | cut -f1)
log "Package created: ${TAR_SIZE}"

# ── 2. Upload ───────────────────────────────────────────────────────────────
log "Uploading to s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}..."

aws s3 cp "$TAR_PATH" "s3://${AWS_SIGNING_BUCKET}/${INPUT_KEY}" --quiet || {
  echo "ERROR: S3 upload failed" >&2
  exit 3
}

# ── 3. Submit signing request ───────────────────────────────────────────────
log "Submitting signing request..."

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

# Signing service ad-hoc signing API v2: POST /v2/sign-tasks. awscurl SigV4-signs
# from the AWS credential chain (env vars, incl. AWS_SESSION_TOKEN) -- no
# credentials on the command line. submit_sign_task retries a throttled
# submission and surfaces the full response body on any other failure, so
# auth/manifest errors stay diagnosable.
if ! command -v awscurl >/dev/null 2>&1; then
  echo "ERROR: awscurl not found (required for SigV4 signing)" >&2
  exit 1
fi

# shellcheck source=cdsigner-submit.sh
source "$SCRIPT_DIR/cdsigner-submit.sh"

# ── 4. Poll for completion ──────────────────────────────────────────────────
# wait_for_sign_task <sign-task-id>: returns when the task succeeded, exits 4
# when it failed and 5 when it timed out.
wait_for_sign_task() {
  local task_id="$1"
  log "Polling for completion (timeout: 45 min)..."

  # Sized for a GB-scale payload: the mac zip carries the bundled kiro-cli on top
  # of the two backend trees, and the signing service's turnaround grows with the
  # payload. The sign job's timeout-minutes in sign-and-notarize.yml leaves room
  # for the upload ahead of one poll. The parts path polls twice in sequence and
  # can pass that timeout on a slow service (docs/build/signing-runbook.md).
  local MAX_WAIT=2700  # 45 minutes
  local POLL_INTERVAL=30
  local ELAPSED=0
  local SIGNED_OK=0 STATUS_RESPONSE STATUS

  while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    STATUS_RESPONSE=$(awscurl --service signer-builder-tools --region us-west-2 \
      -X GET "${CDSIGNER_API_ENDPOINT}/v2/sign-tasks/${task_id}" \
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
    echo "ERROR: Signing timed out after ${MAX_WAIT}s (sign task: ${task_id})" >&2
    exit 5
  fi
}

# The service refuses a request body over 102400 bytes, and the universal app's
# manifest grew past that (337 nested binaries are 136880 bytes). A body under
# SIGN_BODY_MAX_BYTES is sent as ONE request, exactly as before. A bigger one is
# signed in the order the service documents for nested code: the regular files
# go out in parts (each body under SIGN_PART_MAX_BYTES) against the same
# upload, their signed bytes are copied into the app, and a final request signs
# the bundle entries and the app over that merged tree. Cutting the list and
# merging the outputs alone would leave the app's seal covering only one part.
# shortcut: the final request must still fit one body, and it holds only the
# bundle entries (5 today); split those too if a layout ever lists hundreds.
SIGN_BODY_MAX_BYTES=100000
SIGN_PART_MAX_BYTES=90000

sign_in_parts() {
  local parts_dir="$WORK_DIR/parts" merged_key="pre-signed/${CHANNEL}/${VERSION}/${APP_SLUG}.merged.tar.gz"
  local part_count i task_id task_ids
  task_ids=()
  mkdir -p "$parts_dir"
  printf '%s' "$MANIFEST" > "$WORK_DIR/manifest.json"

  part_count=$(python3 "$SCRIPT_DIR/split-manifest.py" plan \
    "$WORK_DIR/manifest.json" "$PACKAGE_DIR/${APP_NAME}.app" "$parts_dir" \
    --max-bytes "$SIGN_PART_MAX_BYTES" \
    --part-output-prefix "signed/${CHANNEL}/${VERSION}/${APP_SLUG}.part-" \
    --merged-input-key "$merged_key") || {
    echo "ERROR: could not split the signing request" >&2
    exit 4
  }
  if [ "$part_count" -lt 1 ]; then
    echo "ERROR: the signing request is over ${SIGN_BODY_MAX_BYTES} bytes but lists no regular file to split off" >&2
    exit 4
  fi
  log "Request is ${MANIFEST_BYTES} bytes; signing nested binaries in ${part_count} parts, then the app"

  for i in $(seq 1 "$part_count"); do
    task_id=$(submit_sign_task "$(cat "$parts_dir/part-$i.json")") || exit 4
    log "Sign task submitted for part ${i}: ${task_id}"
    task_ids+=("$task_id")
  done

  for i in $(seq 1 "$part_count"); do
    wait_for_sign_task "${task_ids[$((i - 1))]}"
    aws s3 cp "s3://${AWS_SIGNING_BUCKET}/signed/${CHANNEL}/${VERSION}/${APP_SLUG}.part-${i}.zip" \
      "$WORK_DIR/part-$i.zip" --quiet || {
      echo "ERROR: Failed to download signed part ${i}" >&2
      exit 3
    }
    python3 "$SCRIPT_DIR/split-manifest.py" merge \
      "$parts_dir/part-$i.json" "$WORK_DIR/part-$i.zip" "$PACKAGE_DIR/${APP_NAME}.app" || exit 4
    rm -f "$WORK_DIR/part-$i.zip"
  done

  log "Re-packaging with the signed nested binaries..."
  pack_tar "$WORK_DIR/merged.tar.gz"
  aws s3 cp "$WORK_DIR/merged.tar.gz" "s3://${AWS_SIGNING_BUCKET}/${merged_key}" --quiet || {
    echo "ERROR: S3 upload failed" >&2
    exit 3
  }

  task_id=$(submit_sign_task "$(cat "$parts_dir/final.json")") || exit 4
  log "Sign task submitted for the app: ${task_id}"
  wait_for_sign_task "$task_id"
}

MANIFEST_BYTES=$(printf '%s' "$MANIFEST" | wc -c | tr -d ' ')
if [ "$MANIFEST_BYTES" -lt "$SIGN_BODY_MAX_BYTES" ]; then
  SIGN_TASK_ID=$(submit_sign_task "$MANIFEST") || exit 4
  log "Sign task submitted: ${SIGN_TASK_ID}"
  wait_for_sign_task "$SIGN_TASK_ID"
else
  sign_in_parts
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
