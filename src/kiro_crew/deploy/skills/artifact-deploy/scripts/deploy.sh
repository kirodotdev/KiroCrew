#!/usr/bin/env bash
# KiroCrew One-Click Deploy - deploy a pre-built static site under a slug.
#
# Usage:
#   deploy.sh <app_dir> [--slug NAME] [--ttl HOURS] [--profile P] [--region R]
#
# Model: ensures the shared per-account base stack exists (S3 + CloudFront),
# then uploads <app_dir> to s3://<bucket>/<slug>/, writes a TTL manifest, and
# invalidates. First run creates the stack (~5-15 min); after that it's seconds.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_common.sh"
TEMPLATE="$SCRIPT_DIR/../templates/base-stack.yaml"

APP_DIR=""
SLUG=""
TTL_HOURS="72"
PROFILE=""
REGION="${AWS_REGION:-us-west-2}"
ALLOW_FINDINGS=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)    SLUG="$2"; shift 2;;
    --ttl)     TTL_HOURS="$2"; shift 2;;
    --profile) PROFILE="$2"; shift 2;;
    --region)  REGION="$2"; shift 2;;
    --allow-findings) ALLOW_FINDINGS=true; shift;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*)        echo "unknown flag: $1" >&2; exit 2;;
    *)         APP_DIR="$1"; shift;;
  esac
done

[[ -n "$APP_DIR" && -d "$APP_DIR" ]] || {
  echo "usage: deploy.sh <app_dir> [--slug NAME] [--ttl HOURS] [--profile P] [--region R]" >&2
  exit 2
}
[[ -f "$APP_DIR/index.html" ]] || {
  echo "error: $APP_DIR has no index.html at its root (MVP serves a pre-built static site)" >&2
  exit 2
}

# ── TTL validation: must be an integer in [0, 8760] ──
if ! [[ "$TTL_HOURS" =~ ^[0-9]+$ ]]; then
  echo "error: --ttl must be a non-negative integer (got '$TTL_HOURS')" >&2
  exit 1
fi
if (( TTL_HOURS > 8760 )); then
  echo "error: --ttl must be <= 8760 (1 year); got $TTL_HOURS" >&2
  exit 1
fi

# ── Immutable snapshot FIRST, then scan + upload THAT snapshot (R10 F1) ──
# Scanning the live directory and uploading it later leaves a TOCTOU window
# where files added after the scan are uploaded unscanned. Snapshot first;
# every scan below and the upload operate on the same immutable copy.
# R12 F1: the source root itself must not be (inside) a sensitive path —
# checked BEFORE the copy so ~/.aws never lands in the snapshot.
validate_source_dir "$APP_DIR"
# R22 F1: reject hardlinked files in the SOURCE tree before copying — cp -a
# copies a hardlink's content into the snapshot, losing the sensitive original
# path identity (the shell twin of _stage_tree_safe's st_nlink > 1 gate).
# The check must run on the source: copies in the snapshot have nlink=1.
SRC_HARDLINKS="$(find "$APP_DIR" -type f -links +1 2>/dev/null || true)"
if [[ -n "$SRC_HARDLINKS" ]]; then
  echo "error: hardlinked files detected in source tree (not allowed):" >&2
  echo "$SRC_HARDLINKS" | sed 's/^/  /' >&2
  exit 1
fi
SNAPSHOT_DIR="$(mktemp -d)"
trap 'rm -rf "$SNAPSHOT_DIR"' EXIT
cp -a "$APP_DIR/." "$SNAPSHOT_DIR/"
# R18 F2: the scanners skip .git paths by design, so anything inside
# .git (remote URLs with embedded tokens, packed credentials) would
# ride UNSCANNED into the public upload. Remove it from the snapshot.
find "$SNAPSHOT_DIR" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
# Reject symlinks in the snapshot — a link could point outside the scanned tree.
SNAP_SYMLINKS="$(find "$SNAPSHOT_DIR" -type l 2>/dev/null || true)"
if [[ -n "$SNAP_SYMLINKS" ]]; then
  echo "error: symlinks detected in deployment snapshot (not allowed):" >&2
  echo "$SNAP_SYMLINKS" | sed 's/^/  /' >&2
  exit 1
fi

# ── Pre-upload credential / sensitive-file scan (fail-closed) ──
# HARD REFUSAL (--allow-findings does NOT override): credential dirs, private
# keys, .env files. These are never acceptable in a public upload.
CRED_DIRS=$(find "$SNAPSHOT_DIR" -type d \( -name '.aws' -o -name '.ssh' \) ! -path '*/.git/*' 2>/dev/null || true)
if [[ -n "$CRED_DIRS" ]]; then
  echo "error: credential directories detected in deployment directory:" >&2
  echo "$CRED_DIRS" | sed 's/^/  /' >&2
  echo "  Publishing would expose credentials/keys world-readable." >&2
  echo "  Remove them before deploying. This finding cannot be overridden." >&2
  exit 1
fi
# Check for sensitive files at ANY depth (nested .env, private keys, certs).
SENSITIVE_HITS=$(find "$SNAPSHOT_DIR" -type f \( \
  -name '.env' -o -name '*.env' -o -name '.env.*' \
  -o -name 'id_rsa*' -o -name '*.pem' \
  \) ! -path '*/.git/*' 2>/dev/null || true)
if [[ -n "$SENSITIVE_HITS" ]]; then
  echo "error: sensitive files detected in deployment directory:" >&2
  echo "$SENSITIVE_HITS" | sed 's/^/  /' >&2
  echo "  Publishing would make these files world-readable." >&2
  echo "  Remove them before deploying. This finding cannot be overridden." >&2
  exit 1
fi
# Canonical credential scan via Python (fail-closed: scanner error = abort).
# Falls back to a builtin grep set ONLY when python3/kiro_crew is unavailable;
# the fallback's failure is also fatal (no `|| true`).
_SCAN_RC=0
if python3 -c "import kiro_crew.security" 2>/dev/null; then
  CRED_HITS=$(python3 - "$SNAPSHOT_DIR" <<'PYEOF'
import sys, os, re
from pathlib import Path
from kiro_crew.security import get_credential_patterns, is_sensitive_path

target = Path(sys.argv[1])
patterns = [p for p in get_credential_patterns()]
hits = []
for root, dirs, files in os.walk(target):
    dirs[:] = [d for d in dirs if d != '.git']
    # Check sensitive paths for EVERY entry (dirs + files, resolved)
    for dname in list(dirs):
        dpath = Path(root) / dname
        try:
            resolved = str(dpath.resolve())
        except OSError:
            resolved = str(dpath)
        if is_sensitive_path(resolved):
            hits.append(str(dpath))
            dirs.remove(dname)  # don't descend
    for fname in files:
        fpath = Path(root) / fname
        try:
            resolved = str(fpath.resolve())
        except OSError:
            resolved = str(fpath)
        if is_sensitive_path(resolved):
            hits.append(str(fpath))
            continue
        try:
            data = fpath.read_text(errors='ignore')
        except (OSError, UnicodeDecodeError):
            continue
        for pat in patterns:
            if pat.search(data):
                hits.append(str(fpath))
                break
if hits:
    for h in hits:
        print(h)
    sys.exit(2)
sys.exit(0)
PYEOF
  ) || _SCAN_RC=$?
  if [[ $_SCAN_RC -eq 2 ]]; then
    echo "error: credential patterns detected in deployment directory:" >&2
    echo "$CRED_HITS" | sed 's/^/  /' >&2
    echo "  Publishing would make these files world-readable." >&2
    echo "  Remove credentials before deploying. This finding cannot be overridden." >&2
    exit 1
  elif [[ $_SCAN_RC -ne 0 ]]; then
    echo "error: credential scanner failed (exit $_SCAN_RC) — aborting (fail-closed)." >&2
    exit 1
  fi
else
  # Fallback: python3/kiro_crew unavailable — use grep (failure = abort).
  # Extended name blocklist covers .npmrc/.netrc/.pypirc/.git-credentials/.kube/.docker/.gnupg
  SENSITIVE_NAMES=$(find "$SNAPSHOT_DIR" -type f \( \
    -name '.npmrc' -o -name '.netrc' -o -name '.pypirc' -o -name '.git-credentials' \
    \) ! -path '*/.git/*' 2>/dev/null || true)
  SENSITIVE_DIRS_EXTRA=$(find "$SNAPSHOT_DIR" -type d \( \
    -name '.kube' -o -name '.docker' -o -name '.gnupg' -o -name '.gpg' \
    \) ! -path '*/.git/*' 2>/dev/null || true)
  if [[ -n "$SENSITIVE_NAMES" || -n "$SENSITIVE_DIRS_EXTRA" ]]; then
    echo "error: sensitive credential paths detected in deployment directory:" >&2
    { echo "$SENSITIVE_NAMES"; echo "$SENSITIVE_DIRS_EXTRA"; } | grep -v '^$' | sed 's/^/  /' >&2
    echo "  Publishing would expose credentials/keys world-readable." >&2
    echo "  Remove them before deploying. This finding cannot be overridden." >&2
    exit 1
  fi
  CRED_HITS=$(grep -RIl --exclude-dir='.git' \
    -E 'AKIA[0-9A-Z]{16}|-----BEGIN .* PRIVATE KEY|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}' \
    "$SNAPSHOT_DIR" 2>/dev/null) || _SCAN_RC=$?
  if [[ $_SCAN_RC -eq 0 && -n "$CRED_HITS" ]]; then
    echo "error: credential patterns detected in deployment directory:" >&2
    echo "$CRED_HITS" | sed 's/^/  /' >&2
    echo "  Publishing would make these files world-readable." >&2
    echo "  Remove credentials before deploying. This finding cannot be overridden." >&2
    exit 1
  elif [[ $_SCAN_RC -ne 0 && $_SCAN_RC -ne 1 ]]; then
    # grep returns 1 = no match (clean), 2+ = error — abort on error.
    echo "error: credential scanner (grep fallback) failed (exit $_SCAN_RC) — aborting." >&2
    exit 1
  fi
fi
# OVERRIDABLE findings (--allow-findings): internal hostnames, AWS ARNs,
# account IDs — informational, user may deploy anyway.
INFO_HITS=$(grep -RIl --exclude-dir='.git' \
  -E '\.amazon\.(com|dev)|\.a2z\.com|arn:aws:|[0-9]{12}\.dkr\.ecr\.' \
  "$SNAPSHOT_DIR" 2>/dev/null || true)
if [[ -n "$INFO_HITS" ]]; then
  echo "warning: internal-reference patterns detected in deployment directory:" >&2
  echo "$INFO_HITS" | sed 's/^/  /' >&2
  echo "  These may expose internal hostnames/ARNs in a public site." >&2
  echo "  Pass --allow-findings to override." >&2
  [[ "$ALLOW_FINDINGS" == "true" ]] || exit 1
fi

AWS=(aws)
[[ -n "$PROFILE" ]] && AWS+=(--profile "$PROFILE")
AWS+=(--region "$REGION")

# slug default: sanitized dir name + short random suffix
if [[ -z "$SLUG" ]]; then
  base="$(basename "$APP_DIR" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
  SLUG="${base:-demo}-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
validate_slug "$SLUG" || exit 1

echo ">> ensuring base stack ($STACK_NAME) in $REGION ..."
if ! "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" >/dev/null 2>&1; then
  echo "   base stack not found - creating it now."
  echo "   NOTE: first deploy takes ~5-15 min while CloudFront propagates globally."
  "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file "$TEMPLATE" \
    --no-fail-on-empty-changeset \
    --tags 'kirocrew:managed=true'
fi

read -r BUCKET DIST_ID DOMAIN < <(
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs" --output json \
  | python3 -c 'import sys,json; o={x["OutputKey"]:x["OutputValue"] for x in json.load(sys.stdin)}; print(o["BucketName"], o["DistributionId"], o["DistributionDomainName"])'
)

# ── Reaper stack check BEFORE upload (only when TTL is finite) ──
# Abort BEFORE any s3 cp/sync to avoid publishing content that cannot be
# automatically reaped — the user must install the reaper first.
if [[ "$TTL_HOURS" != "0" ]]; then
  if ! "${AWS[@]}" cloudformation describe-stacks --stack-name "kirocrew-deploy-reaper" >/dev/null 2>&1; then
    echo ""
    echo "ERROR: The reaper stack (kirocrew-deploy-reaper) is NOT deployed."
    echo "   Finite-TTL deploys require the reaper for automated cleanup."
    echo "   Install it first:"
    echo ""
    echo "     $SCRIPT_DIR/install-reaper.sh --profile ${PROFILE:-<your-profile>} --region $REGION"
    echo ""
    exit 1
  fi
fi

echo ">> uploading $APP_DIR -> s3://$BUCKET/$SLUG/ ..."

# F2 (R9): TOCTOU defense — snapshot the directory before scan+upload so
# files added between scan and upload cannot skip scanning.

"${AWS[@]}" s3 sync "$SNAPSHOT_DIR" "s3://$BUCKET/$SLUG/" \
  --delete --no-follow-symlinks --only-show-errors \
  --exclude '.git/*' --exclude '.env' --exclude '.env.*' \
  --exclude '.aws/*' --exclude '.ssh/*'
rm -rf "$SNAPSHOT_DIR"

# TTL manifest = source of truth for the reaper (M3) and list.sh.
if [[ "$TTL_HOURS" == "0" ]]; then
  EXPIRES_RAW="null"; PERSISTENT="true"
else
  EXPIRES_RAW="$(python3 -c "import sys; from datetime import datetime,timedelta,timezone; print((datetime.now(timezone.utc)+timedelta(hours=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$TTL_HOURS")"; PERSISTENT="false"
fi
MANIFEST="$(mktemp)"
# Build via json.dumps (argv) — a raw heredoc would break on quotes/backslashes
# in $USER, producing invalid JSON that silently makes the deploy un-reapable.
python3 -c '
import json, sys
slug, owner, created, expires, ttl = sys.argv[1:6]
print(json.dumps({
    "slug": slug, "owner": owner, "created_at": created,
    "expires_at": None if expires == "null" else expires,
    "persistent": expires == "null", "ttl_hours": ttl,
}))' "$SLUG" "${USER:-unknown}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$EXPIRES_RAW" "$TTL_HOURS" > "$MANIFEST"
"${AWS[@]}" s3 cp "$MANIFEST" "s3://$BUCKET/$SLUG/.kirocrew-deploy.json" \
  --content-type application/json --only-show-errors
rm -f "$MANIFEST"

echo ">> invalidating /$SLUG/* ..."
"${AWS[@]}" cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/$SLUG/*" >/dev/null

echo ""
echo "✅ deployed: https://$DOMAIN/$SLUG/"
if [[ "$PERSISTENT" == "true" ]]; then
  echo "   TTL: persistent"
else
  echo "   TTL: expires in ${TTL_HOURS}h ($EXPIRES_RAW)"
fi
