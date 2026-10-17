#!/usr/bin/env bash
# KiroCrew One-Click Deploy - TTL reaper. Deletes non-persistent deploys whose
# expires_at is in the past (S3 prefix + invalidation, and any backend stack).
# Safe to run on a schedule (KiroCrew cron / in-account EventBridge).
# Usage: reaper.sh [--dry-run] [--profile P] [--region R]
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"

DRY=0
while [[ $# -gt 0 ]]; do case "$1" in
  --dry-run) DRY=1; shift;;
  --profile) PROFILE="$2"; shift 2;;
  --region)  REGION="$2"; shift 2;;
  -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done

read_base_outputs || { echo "base stack not found - nothing to reap."; exit 0; }

# F7a (R9): Validate manifest-sourced resource IDs before destructive calls.
_valid_dist_id() { [[ "$1" =~ ^[A-Z0-9]+$ ]]; }
_valid_stack_name() { [[ "$1" =~ ^[a-zA-Z][a-zA-Z0-9-]*$ ]]; }
_valid_bucket_name() { [[ "$1" =~ ^[a-z0-9.\-]{3,63}$ ]]; }

now="$(date -u +%s)"
aws_cli s3 ls "s3://$BUCKET/" 2>/dev/null | awk '/ PRE /{print $2}' | while read -r pfx; do
  slug="${pfx%/}"
  [[ -n "$slug" && "$slug" != _* ]] || continue
  man="$(aws_cli s3 cp "s3://$BUCKET/$slug/.kirocrew-deploy.json" - 2>/dev/null)"
  [[ -n "$man" ]] || continue
  line="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print((str(m.get("expires_at") or ""))+"|"+str(m.get("persistent")).lower())')"
  IFS='|' read -r EXP PERS <<< "$line"
  [[ "$PERS" == "true" ]] && continue
  [[ -n "$EXP" ]] || continue
  exp_epoch="$(python3 -c "import sys; from datetime import datetime,timezone; print(int(datetime.strptime(sys.argv[1],'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()))" "$EXP" 2>/dev/null || echo 0)"
  (( exp_epoch > 0 && exp_epoch < now )) || continue

  if [[ "$DRY" == "1" ]]; then
    echo "[dry-run] would reap: $slug (expired $EXP)"
    continue
  fi

  # Check for engine-arch deployment (per-site bucket + own distribution).
  _arch="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(m.get("arch",""))' 2>/dev/null)"
  if [[ "$_arch" == "engine" ]]; then
    _site_bucket="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(m.get("bucket",""))' 2>/dev/null)"
    _site_dist="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(m.get("distribution_id",""))' 2>/dev/null)"
    echo ">> reaping engine-arch $slug (expired $EXP) ..."

    # F7a: validate manifest-sourced resource IDs
    if [[ -n "$_site_dist" ]] && ! _valid_dist_id "$_site_dist"; then
      echo "   error: invalid distribution id '$_site_dist' for $slug — skipping" >&2
      continue
    fi
    if [[ -n "$_site_bucket" ]] && ! _valid_bucket_name "$_site_bucket"; then
      echo "   error: invalid bucket name '$_site_bucket' for $slug — skipping" >&2
      continue
    fi

    # Check for reaping marker (distribution disable in progress).
    _reaping="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(str(m.get("reaping","")).lower())' 2>/dev/null)"

    if [[ -n "$_site_dist" ]]; then
      # Distinguish "distribution truly gone" from transient failures
      # (throttling, network, IAM): only NoSuchDistribution may fall through
      # to bucket/OAC deletion. Any other error -> retry next sweep, never
      # delete infra based on an inconclusive lookup.
      _gdc_err="$(mktemp)"
      if _enabled="$(aws_cli cloudfront get-distribution-config --id "$_site_dist" --query 'DistributionConfig.Enabled' --output text 2>"$_gdc_err")"; then
        rm -f "$_gdc_err"
      elif grep -q 'NoSuchDistribution' "$_gdc_err"; then
        rm -f "$_gdc_err"
        _enabled="GONE"
      else
        rm -f "$_gdc_err"
        echo "   transient error querying distribution for $slug -- will retry next run"
        continue
      fi
      if [[ "$_enabled" == "GONE" ]]; then
        : # Distribution already gone, proceed to bucket.
      else
        # R32 F1 (Lambda R19-F4 twin): the manifest's distribution_id is
        # attacker-writable — verify the distribution's kirocrew:site tag
        # equals THIS slug before ANY destructive op (disable or delete).
        # Missing tag / lookup failure = identity failure -> fail closed.
        _acct="${_REAPER_ACCT:=$(aws_cli sts get-caller-identity --query Account --output text 2>/dev/null || echo "")}"
        if [[ -z "$_acct" ]]; then
          echo "   error: cannot resolve account id for tag verification of $slug -- will retry next run" >&2
          continue
        fi
        _dist_arn="arn:aws:cloudfront::${_acct}:distribution/${_site_dist}"
        _dist_site_tag="$(aws_cli cloudfront list-tags-for-resource --resource "$_dist_arn" \
          --query "Tags.Items[?Key=='kirocrew:site'].Value | [0]" --output text 2>/dev/null || echo "TAG_LOOKUP_FAILED")"
        if [[ "$_dist_site_tag" != "$slug" ]]; then
          echo "   error: distribution $_site_dist kirocrew:site tag ('$_dist_site_tag') != slug '$slug' -- identity mismatch, quarantining manifest, skipping" >&2
          continue
        fi
      fi
      if [[ "$_enabled" == "GONE" ]]; then
        : # already handled above
      elif [[ "$_enabled" == "True" ]]; then
        # Phase 1: disable distribution.
        _etag="$(aws_cli cloudfront get-distribution-config --id "$_site_dist" --query 'ETag' --output text)"
        _cfg_file="$(mktemp)"; aws_cli cloudfront get-distribution-config --id "$_site_dist" --query 'DistributionConfig' > "$_cfg_file"
        python3 -c "import json,sys; c=json.load(open(sys.argv[1])); c['Enabled']=False; json.dump(c,open(sys.argv[1],'w'))" "$_cfg_file"
        aws_cli cloudfront update-distribution --id "$_site_dist" --if-match "$_etag" --distribution-config "file://$_cfg_file" >/dev/null 2>&1 || true
        rm -f "$_cfg_file"
        # Mark as reaping for next sweep.
        _reaping_manifest="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);m["reaping"]=True;print(json.dumps(m))')"
        _tmp="$(mktemp)"; printf '%s' "$_reaping_manifest" > "$_tmp"
        aws_cli s3 cp "$_tmp" "s3://$BUCKET/$slug/.kirocrew-deploy.json" --content-type application/json --only-show-errors
        rm -f "$_tmp"
        echo "   distribution disabled, will delete on next pass"
        continue
      else
        # Phase 2: distribution disabled — delete it.
        _etag="$(aws_cli cloudfront get-distribution-config --id "$_site_dist" --query 'ETag' --output text 2>/dev/null || echo "")"
        if [[ -n "$_etag" ]]; then
          if ! aws_cli cloudfront delete-distribution --id "$_site_dist" --if-match "$_etag" 2>/dev/null; then
            echo "   distribution $slug still propagating -- will retry next run"
            continue
          fi
        fi
      fi
    fi

    # R16 F5: Track deletion success — only remove manifest after ALL resources
    # are verified gone. If any deletion fails, keep/write a reaping marker so
    # the next sweep retries (mirroring Lambda reaper semantics).
    _engine_all_clean=true

    # Empty and delete per-site bucket.
    if [[ -n "$_site_bucket" ]]; then
      # R27 F1 (twin): verify the bucket's kirocrew:site tag matches the slug
      # BEFORE any destructive op — missing tags = identity failure (fail
      # closed), same as the Lambda reaper. engine.py tags buckets at creation.
      _btag="$(aws_cli s3api get-bucket-tagging --bucket "$_site_bucket" \
        --query 'TagSet[?Key==`kirocrew:site`].Value | [0]' --output text 2>/dev/null || echo '')"
      if [[ "$_btag" != "$slug" ]]; then
        if aws_cli s3api head-bucket --bucket "$_site_bucket" 2>/dev/null; then
          echo "   bucket $_site_bucket identity tag mismatch (kirocrew:site='$_btag') -- skipping"
          _engine_all_clean=false
        fi
      else
      aws_cli s3 rm "s3://$_site_bucket/" --recursive --only-show-errors 2>/dev/null || true
      if ! aws_cli s3api delete-bucket --bucket "$_site_bucket" 2>/dev/null; then
        # Check if bucket is truly gone (NoSuchBucket) or deletion failed.
        if aws_cli s3api head-bucket --bucket "$_site_bucket" 2>/dev/null; then
          _engine_all_clean=false
          echo "   bucket $_site_bucket still exists -- will retry"
        fi
      fi
      fi
    fi

    # F7: Delete the OAC to prevent quota exhaustion.
    _oac_id="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(m.get("oac_id",""))' 2>/dev/null)"
    if [[ -z "$_oac_id" ]]; then
      # Fallback: resolve by name prefix for older manifests without oac_id.
      _oac_prefix="deploy-web-${slug}"
      _oac_prefix="${_oac_prefix:0:57}"
      _oac_id="$(aws_cli cloudfront list-origin-access-controls --max-items 100 --output json 2>/dev/null \
        | python3 -c "
import sys, json, re
data = json.load(sys.stdin)
prefix = sys.argv[1]
# R16 F1: exact-match pattern mirroring reaper_lambda/index.py -- bare startswith
# cross-matches overlapping slugs (site 'app' deletes 'app-v2' OAC).
# Names are deploy-web-<site>[:57] + '-' + 6 hex (engine.py).
oac_re = re.compile(re.escape(prefix) + r'-[0-9a-f]{6}$')
for item in data.get('OriginAccessControlList', {}).get('Items', []):
    if oac_re.fullmatch(item.get('Name', '')):
        print(item['Id']); break
" "$_oac_prefix" 2>/dev/null || echo "")"
    fi
    if [[ -n "$_oac_id" ]]; then
      # F3 (R8): IfMatch ETag is REQUIRED by the CloudFront API.
      # R18 F3: manifest oac_id is attacker-writable -- only delete an OAC
      # whose Name matches THIS deployment's exact pattern.
      _oac_name="$(aws_cli cloudfront get-origin-access-control --id "$_oac_id" --query 'OriginAccessControl.OriginAccessControlConfig.Name' --output text 2>/dev/null || echo "")"
      if ! printf '%s' "$_oac_name" | python3 -c "
import re, sys
name = sys.stdin.read().strip()
prefix = ('deploy-web-' + sys.argv[1])[:57]
sys.exit(0 if re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{6}', name) else 1)
" "$slug"; then
        echo "   OAC $_oac_id name mismatch for $slug -- skipping delete"
        _oac_id=""
      fi
      _oac_etag="$(aws_cli cloudfront get-origin-access-control --id "$_oac_id" --query 'ETag' --output text 2>/dev/null || echo "")"
      if [[ -n "$_oac_etag" ]]; then
        if ! aws_cli cloudfront delete-origin-access-control --id "$_oac_id" --if-match "$_oac_etag" 2>/dev/null; then
          # Check if already gone (NoSuchOriginAccessControl) vs real failure
          if aws_cli cloudfront get-origin-access-control --id "$_oac_id" >/dev/null 2>&1; then
            _engine_all_clean=false
            echo "   OAC $_oac_id still exists -- will retry"
          fi
        fi
      fi
    fi

    # R16 F5: Only remove manifest when ALL deletions verified clean.
    if [[ "$_engine_all_clean" == "true" ]]; then
      aws_cli s3 rm "s3://$BUCKET/$slug/.kirocrew-deploy.json" --only-show-errors 2>/dev/null || true
      echo "   reaped (engine-arch): $slug"
    else
      echo "   engine-arch $slug: partial deletion -- manifest retained for retry"
    fi
    continue
  fi

  # Check for reaping marker = Phase B (prior run initiated stack deletion).
  _reaping="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);print(str(m.get("reaping","")).lower())')"
  if [[ "$_reaping" == "true" ]]; then
    # Phase B: stack previously initiated for deletion, check status.
    stack_name="kirocrew-deploy-app-$slug"
    # Only a positive "does not exist" may collapse to GONE — a transient
    # describe failure must retry, not delete S3 content under a live stack.
    _ds_err="$(mktemp)"
    if stack_status="$(aws_cli cloudformation describe-stacks --stack-name "$stack_name" \
      --query 'Stacks[0].StackStatus' --output text 2>"$_ds_err")"; then
      rm -f "$_ds_err"
    elif grep -q 'does not exist' "$_ds_err"; then
      rm -f "$_ds_err"
      stack_status="GONE"
    else
      rm -f "$_ds_err"
      echo "   transient error querying stack $stack_name -- will retry next run"
      continue
    fi
    if [[ "$stack_status" == "GONE" || "$stack_status" == "DELETE_COMPLETE" ]]; then
      # Stack gone - complete the reap.
      aws_cli s3 rm "s3://$BUCKET/_backends/$slug/" --recursive --only-show-errors 2>/dev/null || true
      aws_cli s3 rm "s3://$BUCKET/$slug/" --recursive --only-show-errors
      aws_cli cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/$slug/*" >/dev/null
      echo "   reaped (phase B complete): $slug"
    elif [[ "$stack_status" == "DELETE_FAILED" ]]; then
      # R21 F1: the reaping marker is manifest-writable -- re-verify the
      # stack identity tag before retrying deletion (phase A gate mirror).
      _stack_tag="$(aws_cli cloudformation describe-stacks --stack-name "$stack_name" \
        --query 'Stacks[0].Tags[?Key==`kirocrew:site`].Value | [0]' --output text 2>/dev/null || echo '')"
      if [[ "$_stack_tag" != "$slug" ]]; then
        echo "   stack $stack_name tag mismatch (kirocrew:site='$_stack_tag') -- skipping retry"
        continue
      fi
      echo "   stack $stack_name DELETE_FAILED -- retrying delete"
      aws_cli cloudformation delete-stack --stack-name "$stack_name" || true
    else
      echo "   stack $stack_name still $stack_status -- will retry next run"
    fi
    continue
  fi

  # Phase A: initiate reap.
  echo ">> reaping $slug (expired $EXP) ..."
  # Detach backend from CloudFront FIRST — if the stack is deleted before
  # detach, the origin remains dangling until the next distribution deploy.
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  python3 "$SCRIPT_DIR/detach_backend.py" --profile "$PROFILE" --region "$REGION" --dist-id "$DIST_ID" --slug "$slug" || true

  # Check if this deployment has a backend stack.
  if aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$slug" >/dev/null 2>&1; then
    # R21 F1: verify the stack's kirocrew:site tag matches the slug being
    # reaped BEFORE deletion (same identity gate as the engine-arch path).
    # deploy-backend.sh tags stacks at creation; missing/mismatched tag means
    # the stack is not provably this deployment's -- skip destructive ops.
    _stack_tag="$(aws_cli cloudformation describe-stacks --stack-name "kirocrew-deploy-app-$slug" \
      --query 'Stacks[0].Tags[?Key==`kirocrew:site`].Value | [0]' --output text 2>/dev/null || echo '')"
    if [[ "$_stack_tag" != "$slug" ]]; then
      echo "   stack kirocrew-deploy-app-$slug tag mismatch (kirocrew:site='$_stack_tag') -- skipping"
      continue
    fi
    # Two-phase: initiate stack deletion and write reaping marker.
    if ! aws_cli cloudformation delete-stack --stack-name "kirocrew-deploy-app-$slug"; then
      echo "   stack delete failed for $slug -- will retry next run"
      continue
    fi
    echo "   backend stack kirocrew-deploy-app-$slug scheduled for deletion"
    # Rewrite manifest with reaping marker — S3 deletion deferred to phase B.
    _reaping_manifest="$(printf '%s' "$man" | python3 -c 'import sys,json;m=json.load(sys.stdin);m["reaping"]=True;print(json.dumps(m))')"
    _tmp="$(mktemp)"; printf '%s' "$_reaping_manifest" > "$_tmp"
    aws_cli s3 cp "$_tmp" "s3://$BUCKET/$slug/.kirocrew-deploy.json" --content-type application/json --only-show-errors
    rm -f "$_tmp"
    echo "   manifest marked as reaping -- S3 cleanup on next pass"
  else
    # Static-only: single-phase reap (no stack to wait for).
    aws_cli s3 rm "s3://$BUCKET/$slug/" --recursive --only-show-errors
    aws_cli s3 rm "s3://$BUCKET/_backends/$slug/" --recursive --only-show-errors 2>/dev/null || true
    aws_cli cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/$slug/*" >/dev/null
    echo "   reaped: $slug"
  fi
done
echo "reaper done."
