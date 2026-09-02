#!/usr/bin/env bash
# KiroCrew One-Click Deploy - deploy a whole app (frontend + optional backend)
# in ONE command. Vercel-like convention:
#   <app_dir>/public/   -> static frontend (index.html at its root)  [fallback: <app_dir> itself]
#   <app_dir>/api/      -> backend handler (index.py: def handler(event, context))
#
# Usage:
#   deploy-app.sh <app_dir> --slug NAME [--table] [--runtime R] [--handler H] \
#                 [--wait] [--profile P] [--region R]
#
# If an api/ dir is present it is deployed behind CloudFront at /<slug>/api/*.
# Add --table for a stateful app (provisions a DynamoDB table, scoped IAM).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP=""; SLUG=""; TABLE=""; WAITF=""; RUNTIME=""; HANDLER=""; PROFILE=""; REGION=""
while [[ $# -gt 0 ]]; do case "$1" in
  --slug)    SLUG="$2"; shift 2;;
  --table)   TABLE="--table"; shift;;
  --wait)    WAITF="--wait"; shift;;
  --runtime) RUNTIME="$2"; shift 2;;
  --handler) HANDLER="$2"; shift 2;;
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  -*) echo "unknown flag: $1" >&2; exit 2;;
  *) APP="$1"; shift;;
esac; done
[[ -n "$APP" && -d "$APP" ]] || { echo "usage: deploy-app.sh <app_dir> --slug NAME [--table] [...]" >&2; exit 2; }
[[ -n "$SLUG" ]] || { echo "error: --slug is required" >&2; exit 2; }
source "$DIR/_common.sh"
validate_slug "$SLUG" || exit 1

STATIC="$APP/public"; [[ -d "$STATIC" ]] || STATIC="$APP"
API="$APP/api"

common=()
[[ -n "$PROFILE" ]] && common+=(--profile "$PROFILE")
[[ -n "$REGION" ]] && common+=(--region "$REGION")

echo "== deploy-app '$SLUG' =="
echo "   static:  $STATIC"
[[ -d "$API" ]] && echo "   backend: $API $([[ -n "$TABLE" ]] && echo '(stateful)')"

"$DIR/deploy.sh" "$STATIC" --slug "$SLUG" ${common[@]+"${common[@]}"}

if [[ -d "$API" ]]; then
  bflags=(--slug "$SLUG")
  [[ -n "$TABLE" ]]   && bflags+=("$TABLE")
  [[ -n "$RUNTIME" ]] && bflags+=(--runtime "$RUNTIME")
  [[ -n "$HANDLER" ]] && bflags+=(--handler "$HANDLER")
  [[ -n "$WAITF" ]]   && bflags+=("$WAITF")
  "$DIR/deploy-backend.sh" "$API" "${bflags[@]}" ${common[@]+"${common[@]}"}
else
  echo "(no api/ dir — static-only)"
fi

echo ""
echo "✅ app '$SLUG' deployed."
