#!/usr/bin/env bash
# KiroCrew One-Click Deploy - install the in-account scheduled reaper ONCE per
# account. Provisions an EventBridge-triggered Lambda that deletes expired
# non-persistent deploys. Reliable: runs in-account on a timer, no local creds.
#
# Usage:
#   install-reaper.sh [--rate 'rate(1 hour)'] [--profile P] [--region R]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/_common.sh"
TEMPLATE="$DIR/../templates/reaper.yaml"
CODE_DIR="$DIR/reaper_lambda"

RATE="rate(1 hour)"
while [[ $# -gt 0 ]]; do case "$1" in
  --rate)    RATE="$2"; shift 2;;
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done

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
  echo "  All deploy roles require this boundary. Create it first:" >&2
  echo "  Use the dashboard /deploy console Setup page to generate the boundary document," >&2
  echo "  then run:" >&2
  echo "    aws iam create-policy --policy-name kirocrew-deploy-app-boundary \\" >&2
  echo "      --policy-document file://boundary-policy.json \\" >&2
  echo "      ${PROFILE:+--profile $PROFILE }--region $REGION" >&2
  exit 1
fi

# ── F2 (R29): Dead stack detection — ROLLBACK_COMPLETE blocks deploy ──
_STACK_STATUS="$(aws_cli cloudformation describe-stacks --stack-name kirocrew-deploy-reaper \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "DOES_NOT_EXIST")"
if [[ "$_STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  echo "error: stack kirocrew-deploy-reaper is in ROLLBACK_COMPLETE state." >&2
  echo "  This dead stack blocks new deployments. Delete it first:" >&2
  echo "    aws cloudformation delete-stack --stack-name kirocrew-deploy-reaper \\" >&2
  echo "      ${PROFILE:+--profile $PROFILE }--region $REGION" >&2
  exit 1
fi

# package + upload the reaper Lambda code to the shared bucket
# mktemp -d (not -u): -u yields a predictable un-created path — a symlink
# planted there lets shutil.make_archive overwrite an arbitrary file (TOCTOU).
zipdir="$(mktemp -d)"
trap 'rm -rf "$zipdir"' EXIT
zipbase="$zipdir/code"
python3 -c 'import shutil,sys; shutil.make_archive(sys.argv[1], "zip", sys.argv[2])' "$zipbase" "$CODE_DIR"
zip="${zipbase}.zip"
key="_reaper/reaper-$(date -u +%Y%m%d%H%M%S).zip"
echo ">> uploading reaper code -> s3://$BUCKET/$key ..."
aws_cli s3 cp "$zip" "s3://$BUCKET/$key" --only-show-errors
rm -f "$zip"

echo ">> deploying reaper stack (schedule: $RATE) ..."
aws_cli cloudformation deploy \
  --stack-name kirocrew-deploy-reaper \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides Bucket="$BUCKET" DistributionId="$DIST_ID" CodeBucket="$BUCKET" CodeKey="$key" ScheduleRate="$RATE"

echo ""
echo "✅ reaper installed: Lambda kirocrew-deploy-reaper runs $RATE in-account."
echo "   Test one run:  aws lambda invoke --function-name kirocrew-deploy-reaper /tmp/reap.json --profile <p> --region <r> && cat /tmp/reap.json"
