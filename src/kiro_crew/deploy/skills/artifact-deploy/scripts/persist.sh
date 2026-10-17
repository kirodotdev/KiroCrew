#!/usr/bin/env bash
# KiroCrew One-Click Deploy - promote a deploy to persistent (clears its TTL so
# the reaper skips it).
# Usage: persist.sh <slug> [--profile P] [--region R]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

SLUG=""
while [[ $# -gt 0 ]]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  -*) echo "unknown flag: $1" >&2; exit 2;;
  *) SLUG="$1"; shift;;
esac; done
[[ -n "$SLUG" ]] || { echo "usage: persist.sh <slug> [--profile P] [--region R]" >&2; exit 2; }
validate_slug "$SLUG" || exit 1

read_base_outputs || { echo "base stack not found." >&2; exit 1; }

key="s3://$BUCKET/$SLUG/.kirocrew-deploy.json"
cur="$(aws_cli s3 cp "$key" - 2>/dev/null || true)"
[[ -n "$cur" ]] || { echo "no manifest for slug '$SLUG' (is it deployed?)" >&2; exit 1; }

new="$(printf '%s' "$cur" | python3 -c 'import sys,json; m=json.load(sys.stdin); m["persistent"]=True; m["expires_at"]=None; m["ttl_hours"]="0"; print(json.dumps(m))')"
tmp="$(mktemp)"; printf '%s' "$new" > "$tmp"
aws_cli s3 cp "$tmp" "$key" --content-type application/json --only-show-errors
rm -f "$tmp"
echo "✅ '$SLUG' is now persistent (TTL cleared; reaper will skip it)."
