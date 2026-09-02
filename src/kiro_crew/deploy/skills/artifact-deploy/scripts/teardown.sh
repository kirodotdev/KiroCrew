#!/usr/bin/env bash
# KiroCrew One-Click Deploy - remove one deployed slug (S3 prefix + invalidation).
# The shared base stack (bucket + distribution) is left intact.
#
# Usage:
#   teardown.sh <slug> [--profile P] [--region R]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
SLUG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --region)  REGION="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*)        echo "unknown flag: $1" >&2; exit 2;;
    *)         SLUG="$1"; shift;;
  esac
done

[[ -n "$SLUG" ]] || { echo "usage: teardown.sh <slug> [--profile P] [--region R]" >&2; exit 2; }
validate_slug "$SLUG" || exit 1

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
AWS+=(--region "$REGION")

read -r BUCKET DIST_ID < <(
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs" --output json \
  | python3 -c 'import sys,json; o={x["OutputKey"]:x["OutputValue"] for x in json.load(sys.stdin)}; print(o["BucketName"], o["DistributionId"])'
)

echo ">> detaching backend (if any) ..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/detach_backend.py" --profile "$PROFILE" --region "$REGION" --dist-id "$DIST_ID" --slug "$SLUG" || true

if "${AWS[@]}" cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" >/dev/null 2>&1; then
  # R22 F2: verify the stack's identity tags before deletion (same gate the
  # reaper uses, R21 F1). deploy-backend.sh tags stacks at creation with
  # kirocrew:site=<slug> + kirocrew:managed=true; a stack without matching
  # tags is not provably this deployment's -- abort rather than delete.
  _STACK_TAGS="$("${AWS[@]}" cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" \
    --query 'Stacks[0].Tags' --output json 2>/dev/null || echo '[]')"
  _TAG_OK="$(printf '%s' "$_STACK_TAGS" | python3 -c '
import json, sys
tags = {t.get("Key"): t.get("Value") for t in (json.load(sys.stdin) or [])}
slug = sys.argv[1]
ok = tags.get("kirocrew:site") == slug and tags.get("kirocrew:managed") == "true"
print("ok" if ok else "mismatch")
' "$SLUG")"
  if [[ "$_TAG_OK" != "ok" ]]; then
    echo "error: stack kirocrew-deploy-app-$SLUG identity tags do not match this deployment -- refusing to delete" >&2
    exit 1
  fi
  echo ">> deleting backend stack kirocrew-deploy-app-$SLUG ..."
  "${AWS[@]}" cloudformation delete-stack --stack-name "kirocrew-deploy-app-$SLUG"
fi

echo ">> removing s3://$BUCKET/$SLUG/ ..."
"${AWS[@]}" s3 rm "s3://$BUCKET/$SLUG/" --recursive --only-show-errors

echo ">> removing deployment archives s3://$BUCKET/_backends/$SLUG/ ..."
"${AWS[@]}" s3 rm "s3://$BUCKET/_backends/$SLUG/" --recursive --only-show-errors 2>/dev/null || true

echo ">> invalidating /$SLUG/* ..."
"${AWS[@]}" cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/$SLUG/*" >/dev/null

echo "✅ torn down: $SLUG"
