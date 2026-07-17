#!/usr/bin/env bash
# Sandbox security smoke test — run on dev desktop to verify standard mode
# Usage: ssh dev-dsk-bolichen-2a-88e4ee3f.us-west-2.amazon.com
#        cd ~/workplace/kirocrew && bash test/smoke_sandbox.sh
#
# Prerequisites: kirocrew gateway running, ada profile "smoke" configured
# Setup:  ada profile add --profile smoke --account <account_id> --provider conduit --role IibsAdminAccess-DO-NOT-DELETE

set -uo pipefail

# Resolve PYTHONPATH — use editable install or fall back to src/
export PYTHONPATH="${PYTHONPATH:-src}"

ACCOUNT="${1:-373229397057}"
PROFILE="smoke"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; SKIP=0

pass() { echo -e "  ${GREEN}✓ PASS${NC}: $1"; ((PASS++)); }
fail() { echo -e "  ${RED}✗ FAIL${NC}: $1"; ((FAIL++)); }
skip() { echo -e "  ${YELLOW}⊘ SKIP${NC}: $1"; ((SKIP++)); }

# Run a command inside kirocrew's sandbox (same as kiro-cli subprocess)
sandbox_run() {
    local sandbox_mode="${KIROCREW_SANDBOX_MODE:-auto}"
    python3 -c "
from kiro_crew.sandbox import wrap_argv
import subprocess, sys
argv, cleanup = wrap_argv(sys.argv[1:], '$sandbox_mode')
try:
    r = subprocess.run(argv, capture_output=True, timeout=30, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    sys.exit(r.returncode)
finally:
    if cleanup:
        import os; os.unlink(cleanup)
" "$@"
}

# Run a denied-commands check
is_denied() {
    python3 -c "
import json, re, sys
with open('agents/defaults.json') as f:
    cmds = json.load(f)['toolsSettings']['execute_bash']['deniedCommands']
cmd = ' '.join(sys.argv[1:])
for p in cmds:
    if re.search(p, cmd):
        print(f'DENIED by: {p}')
        sys.exit(0)
print('ALLOWED')
sys.exit(1)
" "$@"
}

# Run redact_credentials check
check_redaction() {
    python3 -c "
from kiro_crew.security import redact_credentials
import sys
text = sys.argv[1]
result, warnings = redact_credentials(text)
if warnings:
    print(f'REDACTED ({len(warnings)} patterns)')
    sys.exit(0)
else:
    print('NOT REDACTED')
    sys.exit(1)
" "$1"
}

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          KiroCrew Sandbox Security Smoke Test               ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Account: $ACCOUNT"
echo "Sandbox: ${KIROCREW_SANDBOX_MODE:-auto} (standard)"
echo ""

# ─── Section 1: Sandbox filesystem isolation ───
echo "━━━ 1. Sandbox Filesystem Isolation ━━━"

# Standard mode: .aws/.ssh/.kube should be VISIBLE
if sandbox_run ls ~/.aws/ >/dev/null 2>&1; then
    pass "~/.aws is accessible (standard mode)"
else
    fail "~/.aws is NOT accessible — standard mode should expose it"
fi

if sandbox_run ls ~/.ssh/ >/dev/null 2>&1; then
    pass "~/.ssh is accessible (standard mode)"
else
    fail "~/.ssh is NOT accessible — standard mode should expose it"
fi

if sandbox_run ls ~/.kube/ 2>/dev/null | head -1 >/dev/null 2>&1; then
    pass "~/.kube is accessible (standard mode)"
else
    if [ -d ~/.kube ]; then
        fail "~/.kube exists but NOT accessible in sandbox"
    else
        skip "~/.kube does not exist on this host"
    fi
fi

# Standard mode: .gnupg/.azure/.docker SHOULD be hidden
for dir in .gnupg .config/gcloud .azure .docker; do
    real_path="$HOME/$dir"
    if [ -d "$real_path" ]; then
        if sandbox_run ls "$real_path" 2>/dev/null | grep -q .; then
            fail "~/$dir is visible in sandbox (should be hidden)"
        else
            pass "~/$dir is hidden in sandbox"
        fi
    else
        skip "~/$dir does not exist on this host"
    fi
done

# ─── Section 2: Env var scrubbing ───
echo ""
echo "━━━ 2. Environment Variable Scrubbing ━━━"

# Set test env vars, verify they're scrubbed inside sandbox
export AWS_SECRET_ACCESS_KEY="test_secret_key_12345"
export AWS_SESSION_TOKEN="test_session_token_12345"

secret_in_sandbox=$(sandbox_run env 2>/dev/null | grep -c "AWS_SECRET\|AWS_SESSION" || true)
if [ "$secret_in_sandbox" -eq 0 ]; then
    pass "AWS_SECRET*/AWS_SESSION* scrubbed from sandbox env"
else
    fail "AWS_SECRET*/AWS_SESSION* leaked into sandbox ($secret_in_sandbox vars found)"
fi

unset AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

ssh_sock_in_sandbox=$(sandbox_run env 2>/dev/null | grep -c "SSH_AUTH_SOCK" || true)
if [ "$ssh_sock_in_sandbox" -eq 0 ]; then
    pass "SSH_AUTH_SOCK scrubbed from sandbox env"
else
    fail "SSH_AUTH_SOCK leaked into sandbox"
fi

# ─── Section 3: Denied commands ───
echo ""
echo "━━━ 3. Denied Commands (should BLOCK) ━━━"

blocked_cmds=(
    "ada credentials print --profile smoke"
    "ada credentials update --account $ACCOUNT --role Admin --other emergency"
    "aws s3 cp ./file s3://bucket/exfil"
    "aws s3 sync ./dir s3://bucket/"
    "aws ec2 terminate-instances --instance-ids i-1234"
    "aws logs delete-log-group --log-group-name test"
    "echo \$AWS_SECRET_ACCESS_KEY"
    "printenv AWS_SECRET_ACCESS_KEY"
    "env | grep AWS_SECRET"
    "cat ~/.aws/credentials"
    "cat ~/.ssh/id_rsa"
    "python3 -c 'import boto3; print(boto3.Session().get_credentials())'"
    "curl http://169.254.169.254/latest/meta-data/"
)

for cmd in "${blocked_cmds[@]}"; do
    if is_denied $cmd >/dev/null 2>&1; then
        pass "BLOCKED: $cmd"
    else
        fail "NOT BLOCKED: $cmd"
    fi
done

# ─── Section 4: Denied commands (should ALLOW) ───
echo ""
echo "━━━ 4. Denied Commands (should ALLOW) ━━━"
echo "  Note: ada commands are blocked by kiro-cli at runtime"

allowed_cmds=(
    "ada credentials update --once --account $ACCOUNT --provider conduit --role IibsAdminAccess-DO-NOT-DELETE"
    "ada credentials update --account $ACCOUNT --provider conduit --role IibsAdminAccess-DO-NOT-DELETE"
    "ada profile add --profile test --account $ACCOUNT --provider conduit --role IibsAdminAccess-DO-NOT-DELETE"
    "ada profile list"
    "aws sts get-caller-identity --profile $PROFILE"
    "aws sts assume-role --role-arn arn:aws:iam::$ACCOUNT:role/Admin"
    "aws ec2 describe-instances --profile $PROFILE"
    "aws logs filter-log-events --profile $PROFILE --log-group-name /aws/lambda/test"
    "aws s3 ls s3://my-bucket --profile $PROFILE"
    "aws s3 cp s3://bucket/file ./local"
    "kubectl get pods"
    "git clone ssh://git.amazon.com:2222/pkg/KiroCrew"
)

for cmd in "${allowed_cmds[@]}"; do
    if is_denied $cmd >/dev/null 2>&1; then
        fail "BLOCKED (should be allowed): $cmd"
    else
        pass "ALLOWED: $cmd"
    fi
done

# ─── Section 5: Credential output redaction ───
echo ""
echo "━━━ 5. Credential Output Redaction ━━━"

# Should be redacted
redact_cases=(
    "AKIAIOSFODNN7EXAMPLE"
    "SecretAccessKey=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG"
    "SessionToken=FwoGZXIvYXdzEBYaDHlongtoken1234567890abc"
    "-----BEGIN RSA PRIVATE KEY-----"
    "xoxb-1234567890-abcdefghijklmnop"
)

for text in "${redact_cases[@]}"; do
    if check_redaction "$text" >/dev/null 2>&1; then
        pass "REDACTED: ${text:0:40}..."
    else
        fail "NOT REDACTED: ${text:0:40}..."
    fi
done

# Should NOT be redacted (normal output)
safe_cases=(
    "Successfully refreshed aws credentials for default"
    '{"Account": "123456789012", "Arn": "arn:aws:iam::123:user/dev"}'
    "Cloning into KiroCrew... remote: Enumerating objects: 1234"
    "NAME       READY   STATUS    RESTARTS   AGE"
)

for text in "${safe_cases[@]}"; do
    if check_redaction "$text" >/dev/null 2>&1; then
        fail "FALSE POSITIVE: ${text:0:50}..."
    else
        pass "NOT REDACTED (correct): ${text:0:50}..."
    fi
done

# ─── Section 6: Base64 encoded credential detection ───
echo ""
echo "━━━ 6. Base64 Encoded Credential Detection ━━━"

b64_secret=$(echo -n "AccessKeyId=AKIAIOSFODNN7EXAMPLE SecretAccessKey=wJalrXUtnFEMI" | base64)
if check_redaction "$b64_secret" >/dev/null 2>&1; then
    pass "Base64-encoded credentials detected and redacted"
else
    fail "Base64-encoded credentials NOT detected"
fi

b64_privkey=$(echo -n "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA" | base64)
if check_redaction "$b64_privkey" >/dev/null 2>&1; then
    pass "Base64-encoded private key detected and redacted"
else
    fail "Base64-encoded private key NOT detected"
fi

b64_benign=$(echo -n "Hello world, this is a normal message" | base64)
if check_redaction "$b64_benign" >/dev/null 2>&1; then
    fail "False positive on benign base64"
else
    pass "Benign base64 NOT redacted (correct)"
fi

# ─── Section 7: Live AWS access (requires ada profile) ───
echo ""
echo "━━━ 7. Live AWS Access (requires ada profile '$PROFILE') ━━━"

if ada profile print --profile "$PROFILE" >/dev/null 2>&1; then
    # Test credential_process works inside sandbox
    caller=$(sandbox_run aws sts get-caller-identity --profile "$PROFILE" --output text 2>/dev/null || true)
    if echo "$caller" | grep -q "$ACCOUNT"; then
        pass "AWS credential_process works inside sandbox (account $ACCOUNT)"
    else
        fail "AWS credential_process failed inside sandbox"
    fi

    # Test read-only S3 access
    if sandbox_run aws s3 ls --profile "$PROFILE" >/dev/null 2>&1; then
        pass "aws s3 ls works inside sandbox"
    else
        skip "aws s3 ls failed (may be permissions, not sandbox)"
    fi
else
    skip "ada profile '$PROFILE' not configured — run: ada profile add --profile $PROFILE --account $ACCOUNT --provider conduit --role IibsAdminAccess-DO-NOT-DELETE"
fi

# ─── Section 8: Live git-over-SSH (requires kinit) ───
echo ""
echo "━━━ 8. Live git-over-SSH ━━━"

if sandbox_run ssh -o BatchMode=yes -o ConnectTimeout=5 -p 2222 git.amazon.com 2>&1 | grep -qi "successfully authenticated\|permission denied\|shell access"; then
    pass "SSH to git.amazon.com reachable from sandbox"
else
    if sandbox_run ssh -o BatchMode=yes -o ConnectTimeout=5 -p 2222 git.amazon.com 2>&1 | grep -qi "bad owner\|no such file"; then
        fail "SSH config ownership bug still present in sandbox"
    else
        skip "SSH to git.amazon.com inconclusive (may need kinit)"
    fi
fi

# ─── Summary ───
echo ""
echo "══════════════════════════════════════════════════════════════"
echo -e "  ${GREEN}PASS: $PASS${NC}  ${RED}FAIL: $FAIL${NC}  ${YELLOW}SKIP: $SKIP${NC}"
echo "══════════════════════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
