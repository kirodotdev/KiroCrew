#!/usr/bin/env bash
# Shared helpers for KiroCrew One-Click Deploy scripts.
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"
# Provides: STACK_NAME, PROFILE, REGION, aws_cli(), read_base_outputs(), validate_slug(),
#           scan_source_dir()
STACK_NAME="kirocrew-deploy-base"
PROFILE="${PROFILE:-}"
REGION="${REGION:-${AWS_REGION:-us-west-2}}"

# Validate a slug: lowercase alphanumeric + hyphens, 1-63 chars, starts with
# alphanumeric, no reserved prefixes (_ or . leading, _backends, _reaper).
# Usage: validate_slug "$SLUG" || exit 1
validate_slug() {
  local slug="$1"
  if [[ -z "$slug" ]]; then
    echo "error: slug cannot be empty" >&2; return 1
  fi
  if ! [[ "$slug" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
    echo "error: slug must match ^[a-z0-9][a-z0-9-]{0,62}$ (lowercase, digits, hyphens; 1-63 chars; starts with alphanumeric)" >&2
    echo "  got: '$slug'" >&2; return 1
  fi
  if [[ "$slug" == _* || "$slug" == .* || "$slug" == "_backends" || "$slug" == "_reaper" ]]; then
    echo "error: slug cannot start with '_' or '.' or be a reserved prefix (_backends, _reaper)" >&2
    echo "  got: '$slug'" >&2; return 1
  fi
  return 0
}

# aws wrapper that injects --profile/--region (read at call time).
aws_cli() {
  if [[ -n "$PROFILE" ]]; then
    command aws --profile "$PROFILE" --region "$REGION" "$@"
  else
    command aws --region "$REGION" "$@"
  fi
}

# Sets globals BUCKET / DIST_ID / DOMAIN from the base stack outputs.
# Returns non-zero if the base stack does not exist yet.
read_base_outputs() {
  local json
  json="$(aws_cli cloudformation describe-stacks --stack-name "$STACK_NAME" \
            --query 'Stacks[0].Outputs' --output json 2>/dev/null)" || return 1
  [[ -n "$json" && "$json" != "null" ]] || return 1
  read -r BUCKET DIST_ID DOMAIN < <(
    printf '%s' "$json" | python3 -c 'import sys,json; o={x["OutputKey"]:x["OutputValue"] for x in json.load(sys.stdin)}; print(o["BucketName"], o["DistributionId"], o["DistributionDomainName"])'
  )
}

# scan_source_dir <dir>
# Fail-closed pre-package/upload scan. Uses kiro_crew.security (canonical) with
# grep fallback. Exits 1 on any finding. Single source of truth for both
# deploy.sh and deploy-backend.sh.

# ── R12 F1: validate the SOURCE ROOT itself before any copy ──
# scan_source_dir checks contents; this checks the root — pointing the deploy
# at ~/.aws (or a dir inside a sensitive root) must fail BEFORE cp copies it
# into a tmp snapshot where the path identity is lost.
validate_source_dir() {
  local src_dir="$1"
  local resolved
  resolved="$(readlink -f "$src_dir" 2>/dev/null || echo "$src_dir")"
  # Canonical check (preferred): kiro_crew.security.is_sensitive_path.
  if python3 -c "import kiro_crew.security" 2>/dev/null; then
    if ! python3 - "$resolved" <<'VSD_PYEOF'
import sys
from kiro_crew.security import is_sensitive_path
p = sys.argv[1]
if is_sensitive_path(p):
    print(f"error: source directory is a sensitive path — refusing: {p}", file=sys.stderr)
    sys.exit(1)
VSD_PYEOF
    then
      exit 1
    fi
  else
    # Builtin fallback (fail-closed): reject well-known sensitive roots.
    local sens
    for sens in "$HOME/.aws" "$HOME/.ssh" "$HOME/.kube" "$HOME/.gnupg" \
                "$HOME/.config/gcloud" "/etc" "$HOME/.kirocrew/keys" "$HOME/.kiro"; do
      if [[ "$resolved" == "$sens" || "$resolved" == "$sens"/* ]]; then
        echo "error: source directory is inside a sensitive root — refusing: $resolved" >&2
        exit 1
      fi
    done
  fi
}
scan_source_dir() {
  local src_dir="$1"
  # Refuse credential directories at any depth.
  local CRED_DIRS
  CRED_DIRS=$(find "$src_dir" -type d \( -name '.aws' -o -name '.ssh' \) ! -path '*/.git/*' 2>/dev/null || true)
  if [[ -n "$CRED_DIRS" ]]; then
    echo "error: credential directories inside source — refusing to package:" >&2
    echo "$CRED_DIRS" | sed 's/^/  /' >&2
    exit 1
  fi
  # Refuse sensitive files at any depth.
  local SENS_FILES
  SENS_FILES=$(find "$src_dir" -type f \( -name '.env' -o -name '*.env' -o -name '.env.*' -o -name 'id_rsa*' -o -name '*.pem' \) ! -path '*/.git/*' 2>/dev/null || true)
  if [[ -n "$SENS_FILES" ]]; then
    echo "error: sensitive files inside source — refusing to package:" >&2
    echo "$SENS_FILES" | sed 's/^/  /' >&2
    exit 1
  fi
  # Refuse symlinked sources.
  local SYMLINKS
  SYMLINKS=$(find "$src_dir" -type l ! -path '*/.git/*' 2>/dev/null || true)
  if [[ -n "$SYMLINKS" ]]; then
    echo "error: symlinks inside source (archives follow them) — refusing to package:" >&2
    echo "$SYMLINKS" | sed 's/^/  /' >&2
    exit 1
  fi
  # Canonical credential scan via Python (fail-closed: scanner error = abort).
  local _SCAN_RC=0
  local CRED_HITS
  if python3 -c "import kiro_crew.security" 2>/dev/null; then
    CRED_HITS=$(python3 - "$src_dir" <<'PYEOF'
import sys, os
from pathlib import Path
from kiro_crew.security import get_credential_patterns, is_sensitive_path

target = Path(sys.argv[1])
patterns = [p for p in get_credential_patterns()]
hits = []
for root, dirs, files in os.walk(target):
    dirs[:] = [d for d in dirs if d != '.git']
    for dname in list(dirs):
        dpath = Path(root) / dname
        try:
            resolved = str(dpath.resolve())
        except OSError:
            resolved = str(dpath)
        if is_sensitive_path(resolved):
            hits.append(str(dpath))
            dirs.remove(dname)
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
      echo "error: credential patterns detected in source:" >&2
      echo "$CRED_HITS" | sed 's/^/  /' >&2
      echo "  Remove credentials before deploying. This finding cannot be overridden." >&2
      exit 1
    elif [[ $_SCAN_RC -ne 0 ]]; then
      echo "error: credential scanner failed (exit $_SCAN_RC) — aborting (fail-closed)." >&2
      exit 1
    fi
  else
    # Fallback: python3/kiro_crew unavailable — use grep (failure = abort).
    local SENSITIVE_NAMES SENSITIVE_DIRS_EXTRA
    SENSITIVE_NAMES=$(find "$src_dir" -type f \( \
      -name '.npmrc' -o -name '.netrc' -o -name '.pypirc' -o -name '.git-credentials' \
      \) ! -path '*/.git/*' 2>/dev/null || true)
    SENSITIVE_DIRS_EXTRA=$(find "$src_dir" -type d \( \
      -name '.kube' -o -name '.docker' -o -name '.gnupg' -o -name '.gpg' \
      \) ! -path '*/.git/*' 2>/dev/null || true)
    if [[ -n "$SENSITIVE_NAMES" || -n "$SENSITIVE_DIRS_EXTRA" ]]; then
      echo "error: sensitive credential paths detected in source:" >&2
      { echo "$SENSITIVE_NAMES"; echo "$SENSITIVE_DIRS_EXTRA"; } | grep -v '^$' | sed 's/^/  /' >&2
      exit 1
    fi
    CRED_HITS=$(grep -RIl --exclude-dir='.git' \
      -E 'AKIA[0-9A-Z]{16}|-----BEGIN .* PRIVATE KEY|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}' \
      "$src_dir" 2>/dev/null) || _SCAN_RC=$?
    if [[ $_SCAN_RC -eq 0 && -n "$CRED_HITS" ]]; then
      echo "error: credential patterns detected in source:" >&2
      echo "$CRED_HITS" | sed 's/^/  /' >&2
      exit 1
    elif [[ $_SCAN_RC -ne 0 && $_SCAN_RC -ne 1 ]]; then
      echo "error: credential scanner (grep fallback) failed (exit $_SCAN_RC) — aborting." >&2
      exit 1
    fi
  fi
}
