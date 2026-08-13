#!/usr/bin/env bash
# KiroCrew One-Click Deploy - cost ESTIMATE from usage metrics (NOT the AWS bill;
# billing lags ~24h). Per-slug S3 storage is exact; CloudFront is account-wide
# for the shared distribution (per-slug egress needs access logs - see NOTE).
# Usage: cost.sh [slug] [--hours N] [--profile P] [--region R]
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

SLUG=""; HOURS=24
while [[ $# -gt 0 ]]; do case "$1" in
  --hours)   HOURS="$2"; shift 2;;
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  -*) echo "unknown flag: $1" >&2; exit 2;;
  *) SLUG="$1"; shift;;
esac; done

[[ -z "$SLUG" ]] || validate_slug "$SLUG" || exit 1
read_base_outputs || { echo "base stack not found." >&2; exit 1; }

# --- TTL-window projection params ---
# A shared link lives for its TTL (default 72h) then the reaper deletes it, so we
# project TOTAL spend over that window (pay-per-request => scales with views, not
# time), NOT a calendar month. Persistent deploys are projected monthly instead.
PROJ_HOURS=72; PROJ_MODE="window"; SLUG_PERSIST="false"; SLUG_BYTES=0
if [[ -n "$SLUG" ]]; then
  _man="$(aws_cli s3 cp "s3://$BUCKET/$SLUG/.kirocrew-deploy.json" - 2>/dev/null || true)"
  if [[ -n "$_man" ]]; then
    read -r _th _pers < <(printf '%s' "$_man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print((m.get("ttl_hours") or "72"),str(m.get("persistent")).lower())')
    SLUG_PERSIST="$_pers"
    if [[ "$_pers" == "true" ]]; then PROJ_MODE="month"; else PROJ_HOURS="${_th:-72}"; fi
    # Manifest values are remote data — never trust into interpolation.
    [[ "$PROJ_HOURS" =~ ^[0-9]+$ ]] && (( PROJ_HOURS >= 1 && PROJ_HOURS <= 8760 )) || PROJ_HOURS=72
  fi
fi

# rough public us-west-2 unit prices (USD)
P_S3_GB_MONTH=0.023
P_CF_PER_10K_REQ=0.0075
P_CF_GB_OUT=0.085

# Validate --hours: bounded integer, passed via argv (never interpolated into source).
if ! [[ "$HOURS" =~ ^[0-9]+$ ]] || (( HOURS < 1 || HOURS > 8760 )); then
  echo "error: --hours must be an integer between 1 and 8760 (got: $HOURS)" >&2
  exit 1
fi
start="$(python3 -c "import sys; from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)-timedelta(hours=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$HOURS")"
end="$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")"

echo "KiroCrew deploy cost - ESTIMATE from usage (NOT the AWS bill; billing lags ~24h)"
echo "window: last ${HOURS}h   origin region: $REGION"
echo ""

s3_bytes() { aws_cli s3 ls "s3://$BUCKET/$1" --recursive --summarize 2>/dev/null | awk '/Total Size/{print $3}'; }

if [[ -n "$SLUG" ]]; then
  b="$(s3_bytes "$SLUG/")"; b="${b:-0}"; SLUG_BYTES="$b"
  echo "slug: $SLUG"
  if [[ "$SLUG_PERSIST" == "true" ]]; then
    cost="$(python3 -c "import sys; print(f'{float(sys.argv[1])/1073741824*float(sys.argv[2]):.6f}')" "$b" "$P_S3_GB_MONTH")"
    echo "  S3 storage: $b bytes  ≈ \$$cost / month (persistent, per-slug exact)"
  else
    cost="$(python3 -c "import sys; b,rate,h=float(sys.argv[1]),float(sys.argv[2]),float(sys.argv[3]); print(f'{b/1073741824*rate*h/730:.6f}')" "$b" "$P_S3_GB_MONTH" "$PROJ_HOURS")"
    echo "  S3 storage: $b bytes  ≈ \$$cost over its ${PROJ_HOURS}h TTL (prorated, per-slug exact)"
  fi
  if aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" >/dev/null 2>&1; then
    lfn="$(aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$SLUG" --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' --output text 2>/dev/null)"
    if [[ -n "$lfn" && "$lfn" != "None" ]]; then
      inv="$(aws_cli cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Invocations --dimensions Name=FunctionName,Value="$lfn" --start-time "$start" --end-time "$end" --period 86400 --statistics Sum --query 'Datapoints[].Sum' --output text 2>/dev/null)"
      [[ -z "$inv" || "$inv" == "None" ]] && inv=0
      echo "  backend Lambda ($lfn): ${inv%.*} invocations in last ${HOURS}h"
    fi
  fi
else
  echo "per-slug S3 storage (exact):"
  aws_cli s3 ls "s3://$BUCKET/" 2>/dev/null | awk '/ PRE /{print $2}' | while read -r p; do
    [[ -n "$p" && "${p%/}" != _* ]] || continue
    b="$(s3_bytes "$p")"; b="${b:-0}"
    printf '  %-30s %s bytes\n' "${p%/}" "$b"
  done
fi

echo ""
echo "shared CloudFront ($DIST_ID) - ACCOUNT-WIDE totals (cannot split per-slug w/o access logs):"
cf() {
  if [[ -n "$PROFILE" ]]; then command aws --profile "$PROFILE" --region us-east-1 cloudwatch get-metric-statistics "$@";
  else command aws --region us-east-1 cloudwatch get-metric-statistics "$@"; fi
}
req="$(cf --namespace AWS/CloudFront --metric-name Requests --dimensions Name=DistributionId,Value="$DIST_ID" Name=Region,Value=Global --start-time "$start" --end-time "$end" --period 86400 --statistics Sum --query 'Datapoints[].Sum' --output text 2>/dev/null)"
[[ -z "$req" || "$req" == "None" ]] && req=0
bo="$(cf --namespace AWS/CloudFront --metric-name BytesDownloaded --dimensions Name=DistributionId,Value="$DIST_ID" Name=Region,Value=Global --start-time "$start" --end-time "$end" --period 86400 --statistics Sum --query 'Datapoints[].Sum' --output text 2>/dev/null)"
[[ -z "$bo" || "$bo" == "None" ]] && bo=0
cfcost="$(python3 -c "import sys; r=float(sys.argv[1] or 0); b=float(sys.argv[2] or 0); p1=float(sys.argv[3]); p2=float(sys.argv[4]); print(f'{r/10000*p1 + b/1073741824*p2:.6f}')" "$req" "$bo" "$P_CF_PER_10K_REQ" "$P_CF_GB_OUT")"
printf '  requests: %s   bytes out: %s   ≈ \$%s (last %sh)\n' "${req%.*}" "${bo%.*}" "$cfcost" "$HOURS"
echo ""
if [[ "$PROJ_MODE" == "month" ]]; then
  echo "projected MONTHLY cost (this deploy is PERSISTENT; each page view ~= 3 API calls, ~20KB/page):"
  python3 - <<'PY'
CF_REQ = 0.0075 / 10000      # CloudFront per HTTPS request
CF_GB  = 0.085               # CloudFront per GB egress
APIGW  = 1.00 / 1_000_000    # API Gateway HTTP API per request
L_REQ  = 0.20 / 1_000_000    # Lambda per request
L_GBS  = 0.0000166667        # Lambda per GB-second
for v in (1_000, 100_000, 1_000_000):
    api = v * 3
    cf  = (v + api) * CF_REQ + (v * 20_000 / 1_073_741_824) * CF_GB
    agw = api * APIGW
    lam = api * L_REQ + api * 0.05 * 0.25 * L_GBS   # 50ms @ 256MB
    print(f"  {v:>9,} views/mo  ~= ${cf+agw+lam:6.2f}/mo   (CloudFront ${cf:.2f} + APIGW ${agw:.2f} + Lambda ${lam:.2f})")
PY
else
  echo "projected TOTAL spend over this share's ${PROJ_HOURS}h TTL window (then the reaper deletes it):"
  echo "(pay-per-request => scales with TOTAL views during the window, not with time; each view ~= 3 API calls, ~20KB)"
  python3 - "$PROJ_HOURS" "$SLUG_BYTES" <<'PY'
import sys
H = float(sys.argv[1]); B = float(sys.argv[2])
CF_REQ = 0.0075 / 10000      # CloudFront per HTTPS request
CF_GB  = 0.085               # CloudFront per GB egress
APIGW  = 1.00 / 1_000_000    # API Gateway HTTP API per request
L_REQ  = 0.20 / 1_000_000    # Lambda per request
L_GBS  = 0.0000166667        # Lambda per GB-second
S3     = 0.023               # S3 per GB-month
storage = B / 1_073_741_824 * S3 * (H / 730.0)   # prorated to the window
for v in (100, 1_000, 10_000):
    api = v * 3
    cf  = (v + api) * CF_REQ + (v * 20_000 / 1_073_741_824) * CF_GB
    agw = api * APIGW
    lam = api * L_REQ + api * 0.05 * 0.25 * L_GBS   # 50ms @ 256MB
    total = cf + agw + lam + storage
    print(f"  {v:>7,} total views  ~= ${total:7.4f}   (CloudFront ${cf:.4f} + APIGW ${agw:.4f} + Lambda ${lam:.4f} + S3 ${storage:.4f})")
print(f"  {'idle':>7} (0 views, whole window)  ~= ${storage:7.4f}   (just prorated S3 storage; DynamoDB on-demand adds pennies if stateful)")
PY
fi
echo ""
echo "NOTE: live numbers above are a usage ESTIMATE, not the AWS bill. Per-slug request/egress"
echo "      attribution needs CloudFront access logs (M4.1); S3 storage is per-slug exact."
