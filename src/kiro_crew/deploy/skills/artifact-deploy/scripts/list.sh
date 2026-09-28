#!/usr/bin/env bash
# KiroCrew One-Click Deploy - list all deploys (from per-slug manifests).
# Usage: list.sh [--profile P] [--region R]
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

while [[ $# -gt 0 ]]; do case "$1" in
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done

read_base_outputs || { echo "base stack not found - nothing deployed yet."; exit 0; }

now="$(date -u +%s)"
printf '%-30s %-9s %-22s %s\n' "SLUG" "STATE" "EXPIRES(UTC)" "LINK"
printf '%-30s %-9s %-22s %s\n' "------------------------------" "---------" "----------------------" "----"

aws_cli s3 ls "s3://$BUCKET/" 2>/dev/null | awk '/ PRE /{print $2}' | while read -r pfx; do
  slug="${pfx%/}"
  [[ -n "$slug" && "$slug" != _* ]] || continue
  man="$(aws_cli s3 cp "s3://$BUCKET/$slug/.kirocrew-deploy.json" - 2>/dev/null)"
  if [[ -z "$man" ]]; then
    printf '%-30s %-9s %-22s %s\n' "$slug" "?" "-" "https://$DOMAIN/$slug/"
    continue
  fi
  line="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print((str(m.get("expires_at") or ""))+"|"+str(m.get("persistent")).lower())')"
  IFS='|' read -r EXP PERS <<< "$line"
  if [[ "$PERS" == "true" ]]; then
    state="persist"
  elif [[ -n "$EXP" ]]; then
    exp_epoch="$(python3 -c "import sys; from datetime import datetime,timezone; print(int(datetime.strptime(sys.argv[1],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()))" "$EXP" 2>/dev/null || echo 0)"
    if (( exp_epoch > 0 && exp_epoch < now )); then state="EXPIRED"; else state="live"; fi
  else
    state="live"
  fi
  printf '%-30s %-9s %-22s %s\n' "$slug" "$state" "${EXP:--}" "https://$DOMAIN/$slug/"
done
