#!/usr/bin/env bash
# KiroCrew One-Click Deploy - per-app backend (M2.2). API Gateway HTTP API ->
# Lambda, fronted by the shared CloudFront distribution at /<slug>/api/*.
# Palisade-safe: the Lambda is invoked only by API Gateway (no world-accessible
# Function URL). Pass --table to also provision a DynamoDB table (stateful apps).
#
# Usage:
#   deploy-backend.sh <handler_dir> --slug NAME [--table] [--runtime python3.12] \
#                     [--handler index.handler] [--wait] [--profile P] [--region R]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/_common.sh"
ATTACH="$DIR/attach_backend.py"

SRC=""; SLUG=""; RUNTIME="python3.12"; HANDLER="index.handler"; WAIT=0; TABLE=0
while [[ $# -gt 0 ]]; do case "$1" in
  --slug)    SLUG="$2"; shift 2;;
  --runtime) RUNTIME="$2"; shift 2;;
  --handler) HANDLER="$2"; shift 2;;
  --table)   TABLE=1; shift;;
  --wait)    WAIT=1; shift;;
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  -*) echo "unknown flag: $1" >&2; exit 2;;
  *) SRC="$1"; shift;;
esac; done
[[ -n "$SRC" && -d "$SRC" ]] || { echo "usage: deploy-backend.sh <handler_dir> --slug NAME [--table] [...]" >&2; exit 2; }
[[ -n "$SLUG" ]] || { echo "error: --slug is required" >&2; exit 2; }
validate_slug "$SLUG" || exit 1

if [[ "$TABLE" == "1" ]]; then
  TEMPLATE="$DIR/../templates/app-apigw-ddb.yaml"   # stateful: adds a DynamoDB table + scoped role
else
  TEMPLATE="$DIR/../templates/app-apigw.yaml"
fi

read_base_outputs || { echo "base stack not found - run deploy.sh once first." >&2; exit 1; }

# ── F2 (R29): Boundary preflight — fail early if IAM boundary policy doesn't exist ──
ACCT_ID="$(aws_cli sts get-caller-identity --query 'Account' --output text 2>/dev/null)" || {
  echo "error: unable to determine AWS account ID (sts get-caller-identity failed)." >&2
  echo "  Check your --profile / credentials." >&2
  exit 1
}
BOUNDARY_ARN="arn:aws:iam::${ACCT_ID}:policy/kirocrew-deploy-app-boundary"
if ! aws_cli iam get-policy --policy-arn "$BOUNDARY_ARN" >/dev/null 2>&1; then
  echo "error: required permissions boundary policy not found:" >&2
  echo "  $BOUNDARY_ARN" >&2
  echo "" >&2
  echo "  All Lambda execution roles require this boundary. Create it first:" >&2
  echo "  Use the dashboard /deploy console Setup page to generate the boundary document," >&2
  echo "  then run:" >&2
  echo "    aws iam create-policy --policy-name kirocrew-deploy-app-boundary \\" >&2
  echo "      --policy-document file://boundary-policy.json \\" >&2
  echo "      ${PROFILE:+--profile $PROFILE }--region $REGION" >&2
  exit 1
fi

# ── F2 (R29): Dead stack detection — ROLLBACK_COMPLETE blocks deploy ──
_STACK_STATUS="$(aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")"
if [[ "$_STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  echo "error: stack kirocrew-deploy-app-$SLUG is in ROLLBACK_COMPLETE state." >&2
  echo "  This dead stack blocks new deployments. Delete it first:" >&2
  echo "    aws cloudformation delete-stack --stack-name kirocrew-deploy-app-$SLUG \\" >&2
  echo "      ${PROFILE:+--profile $PROFILE }--region $REGION" >&2
  exit 1
fi

# ── Pre-package sensitive scan (fail-closed, no override: this becomes Lambda code) ──
# F2 (R9): TOCTOU defense — snapshot source before scan+package.
validate_source_dir "$SRC"
# R22 F1: reject hardlinked files in the SOURCE tree before copying (shell
# twin of _stage_tree_safe's st_nlink > 1 gate — see deploy.sh).
_SRC_HARDLINKS="$(find "$SRC" -type f -links +1 2>/dev/null || true)"
if [[ -n "$_SRC_HARDLINKS" ]]; then
  echo "error: hardlinked files detected in source tree (not allowed):" >&2
  echo "$_SRC_HARDLINKS" | sed 's/^/  /' >&2
  exit 1
fi
SNAP_DIR="$(mktemp -d)"
cp -a "$SRC/." "$SNAP_DIR/"
# R18 F2: the scanners skip .git paths by design, so anything inside
# .git (remote URLs with embedded tokens, packed credentials) would
# ride UNSCANNED into the public upload. Remove it from the snapshot.
find "$SNAP_DIR" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
# Reject symlinks in snapshot.
_SNAP_SYMLINKS="$(find "$SNAP_DIR" -type l 2>/dev/null || true)"
if [[ -n "$_SNAP_SYMLINKS" ]]; then
  echo "error: symlinks detected in source snapshot (may escape boundary):" >&2
  echo "$_SNAP_SYMLINKS" | sed 's/^/  /' >&2
  rm -rf "$SNAP_DIR"
  exit 1
fi
scan_source_dir "$SNAP_DIR"

# package via python (no `zip` binary dependency)
# mktemp -d (not -u): -u yields a predictable un-created path — a symlink
# planted there lets shutil.make_archive overwrite an arbitrary file (TOCTOU).
zipdir="$(mktemp -d)"
trap 'rm -rf "$zipdir" "$SNAP_DIR"' EXIT
zipbase="$zipdir/code"
python3 -c 'import shutil,sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2])' "$zipbase" "$SNAP_DIR"
zip="${zipbase}.zip"
key="_backends/${SLUG}/$(date -u +%Y%m%d%H%M%S).zip"
echo ">> uploading code -> s3://$BUCKET/$key ..."
aws_cli s3 cp "$zip" "s3://$BUCKET/$key" --only-show-errors
rm -f "$zip"

echo ">> deploying backend stack kirocrew-deploy-app-$SLUG ($([[ $TABLE == 1 ]] && echo 'API GW + Lambda + DynamoDB' || echo 'API GW + Lambda')) ..."
# Origin-verify: only CloudFront (which injects this header) may call the API GW
# endpoint directly — a random per-deploy secret shared between the CloudFront
# origin custom headers and the stack's request authorizer.
OV_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# Write parameters to a temp file (mode 600) to keep the secret out of process
# argv (ps-visible). aws cloudformation deploy supports file:// for --parameter-overrides.
_PARAMS_DIR="$(mktemp -d)"
# R10 F4: a second `trap ... EXIT` REPLACES the earlier one (bash semantics),
# which would leak $zipdir/$SNAP_DIR. One trap covers all temp dirs.
trap 'rm -rf "$zipdir" "$SNAP_DIR" "$_PARAMS_DIR"' EXIT
_PARAMS_FILE="$_PARAMS_DIR/params.json"
_OV_SECRET_FILE="$_PARAMS_DIR/ov-secret"
printf '%s' "$OV_SECRET" > "$_OV_SECRET_FILE"
chmod 600 "$_OV_SECRET_FILE"
# CLI v2 file:// format for --parameter-overrides: a JSON array of "Key=Value" strings.
# Read the secret from the mode-600 file (never pass in argv — ps-visible).
python3 -c 'import json,sys; secret=open(sys.argv[1]).read().strip(); pairs=sys.argv[2:]; print(json.dumps(pairs+[f"OriginVerifySecret={secret}"]))' \
  "$_OV_SECRET_FILE" "Slug=$SLUG" "CodeBucket=$BUCKET" "CodeKey=$key" "Runtime=$RUNTIME" "Handler=$HANDLER" \
  > "$_PARAMS_FILE"
chmod 600 "$_PARAMS_FILE"
aws_cli cloudformation deploy \
  --stack-name "kirocrew-deploy-app-$SLUG" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "file://$_PARAMS_FILE" \
  --tags "kirocrew:site=$SLUG" "kirocrew:managed=true"

APIDOMAIN="$(aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" \
  --query 'Stacks[0].Outputs[?OutputKey==`ApiDomain`].OutputValue' --output text)"

echo ">> wiring $SLUG/api/* behind CloudFront ($DIST_ID) -> $APIDOMAIN ..."
python3 "$ATTACH" --profile "$PROFILE" --region "$REGION" --dist-id "$DIST_ID" --slug "$SLUG" --origin-domain "$APIDOMAIN" --origin-verify-secret-file "$_OV_SECRET_FILE"

if [[ "$WAIT" == "1" ]]; then
  echo ">> waiting for CloudFront to redeploy (~3-5 min) ..."
  aws_cli cloudfront wait distribution-deployed --id "$DIST_ID"
  echo "   deployed."
fi

echo ""
echo "✅ backend wired: https://$DOMAIN/$SLUG/api/"
echo "   API Gateway origin; Lambda not world-accessible (Palisade-safe).$([[ $TABLE == 1 ]] && echo ' DynamoDB table: kirocrew-deploy-app-'$SLUG || true)"
[[ "$WAIT" == "1" ]] || echo "   (CloudFront propagating ~3-5 min; pass --wait to block)"
