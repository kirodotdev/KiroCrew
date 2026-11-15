"""The verifier corpus's 175 benign daily-use snippets (``BENIGN``).

See :mod:`redaction_verifier_corpus`. The two certificates are public test
certificates; every other value is fake and built at import.
"""

from __future__ import annotations

import base64

from redaction_verifier_gen import (
    UPD,
    b62,
    b64s,
    b64u,
    hexs,
    key_id,
    pick,
    png_data_uri,
    uuid,
)

CERT_EC = """-----BEGIN CERTIFICATE-----
MIIBzjCCAXWgAwIBAgIUYFi5h6UWy8skhYdPdCH5uMyoxkwwCgYIKoZIzj0EAwIw
PTEkMCIGA1UEAwwbYnVpbGQuY29ycC5leGFtcGxlLmludGVybmFsMRUwEwYDVQQK
DAxFeGFtcGxlIENvcnAwHhcNMjYxMTE1MDc1ODA4WhcNMjcxMTE1MDc1ODA4WjA9
MSQwIgYDVQQDDBtidWlsZC5jb3JwLmV4YW1wbGUuaW50ZXJuYWwxFTATBgNVBAoM
DEV4YW1wbGUgQ29ycDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABM7nEhhwUZfo
JfS3gEBvIuJ/JxQNx8tvxAhEdH22VL/r/I5tSfQds/+ppGZlQTRPEtx4V9jfCDXd
Dz3f8b4+CkKjUzBRMB0GA1UdDgQWBBSO5KGAS+fFq/QX+xibIkK+FyTfNzAfBgNV
HSMEGDAWgBSO5KGAS+fFq/QX+xibIkK+FyTfNzAPBgNVHRMBAf8EBTADAQH/MAoG
CCqGSM49BAMCA0cAMEQCIFfvbbGl12itwpDMcLkmxD12uay+z7FSwfkuOTT+sZUr
AiAj0HLCxmtLLqC86JwVw3gSy9jv2/4iAZ+nSY+fRFGNbA==
-----END CERTIFICATE-----"""
CERT_RSA = """-----BEGIN CERTIFICATE-----
MIIDvTCCAqWgAwIBAgIUEDupdoBxQSrcv0UuXOyoZuUmHWAwDQYJKoZIhvcNAQEL
BQAwbjELMAkGA1UEBhMCVVMxEzARBgNVBAgMCldhc2hpbmd0b24xEDAOBgNVBAcM
B1NlYXR0bGUxFTATBgNVBAoMDEV4YW1wbGUgQ29ycDEhMB8GA1UEAwwYYXBpLmlu
dGVybmFsLmV4YW1wbGUuY29tMB4XDTI2MTExNTA3NTgxNVoXDTI5MDIxNzA3NTgx
NVowbjELMAkGA1UEBhMCVVMxEzARBgNVBAgMCldhc2hpbmd0b24xEDAOBgNVBAcM
B1NlYXR0bGUxFTATBgNVBAoMDEV4YW1wbGUgQ29ycDEhMB8GA1UEAwwYYXBpLmlu
dGVybmFsLmV4YW1wbGUuY29tMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKC
AQEA0dGJv4A0M1YSAlWgfq4hbpcSO2unAvwBqUblPR4dsYjZFpWaWx2GMnDktlJL
wCz8ae8H/ORBj6BxoSap+xEeGH2BOLqLNpZur8BdrcnZPXiglaQLHs4ueXTdBLeA
aF5oGL0bZXZtqPj7Hr3tD8ff0ogWftKG7QkQ1sjO17gnToHOgmL3L1HSz1WZMNxI
SyEwmlru7vmHEwGTQMNbQFLyTUXt2XITi/msLJYcpGBvyJFQ4OyPak/pFjAC9McB
uKRPN3ZhXP8G14e8Nkzxbj+4uzPceO37X3O9ZJYcVaC+bLHmblo1xWaUT8la2w/Y
ozMJ/0FJNz6YqHgZeTzLOJjxLwIDAQABo1MwUTAdBgNVHQ4EFgQUFVnmbd37j+4h
Qi45Tasd8HJ2Gj0wHwYDVR0jBBgwFoAUFVnmbd37j+4hQi45Tasd8HJ2Gj0wDwYD
VR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAVB+eEebncuF1igmDYjzU
16/5T1sqiiE2/ZDK4Ae0Ne9iYTnW1xN0l32t2DA6pwrPnwxrnpIGGHGC+lmZX50N
vup1ByF4Fm9q6y0ksNOlhONqU/veujs43Er2MruRnD0Zd58K42OzjviognB1l5pF
VRDs4Gp4HoR4PJCsM/iYQy8bLXdHhchvvoXBF1GIFMMw25UI2qTCQFdIy5RZbEOP
Z8OX93P/hLeW3cI338GoB45pNXINeu9uSSTyns1hKJ7JA90lGx15Im1//CqQxzoa
EGwRYD1HAR6zPBpzlqcwvhSE4n2ozEcv9O2qA+mrmf0e/Npjjh3eyY8uyyF6hDm+
6Q==
-----END CERTIFICATE-----"""

ACCT = "111122223333"
ACCT2 = "444455556666"
KID = key_id("AKIA")
KID2 = key_id("AKIA")
ASIA = key_id("ASIA")
AROA = "AROA" + pick(UPD, 17)
AIDA = "AIDA" + pick(UPD, 17)
SHA = [hexs(40) for _ in range(12)]
SHORT = [s[:7] for s in SHA]
D64 = [hexs(64) for _ in range(8)]
REQ = [uuid() for _ in range(12)]
XAMZ2 = b64s(76)
XAMZREQ = pick("0123456789ABCDEF", 16)
PNG1 = png_data_uri(24, 24)
PNG2 = png_data_uri(8, 8)
SVG_URI = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><circle cx="8" cy="8" r="7" fill="#2f81f7"/></svg>'
    ).decode()
)
SSH_ED = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF7Hnf54uuDajSUBeegEFjjxDJKxrNeLLOj/TWaVwypu alice@dev-box"
)
SSH_RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC63XGwMk+LMwJPwXuQkZyHhb9lNBFZeJDG333MvMu3GhmziB2zk6SL3PVZ0j16tRfEnj0kkuZM32GFjK49c+0xEcMnf3jnsdMDBrhLu0XCiRXCw0XJmbAEhZirJKtISgy9jYl5qa0Av5oYErp3esn/vnM+kFAX2q5iPQsQ+iWl9cu/NmPeqs4YpPzRUB+upbJ87AZUvAA1PfEiV4cE3mLi5Q0S0hzFnnKxxaJQIZN7YCqdl5Vz2LA1mAGV7Sq6toSq7yrajISazuqLdFdi9PRk0U2/ZO2O8T1DgdTtwHX92lUa0zhIbL6jOpaE4TpV8109QgtEmHCq/9cxZ/dmi7St deploy@ci"
FP_ED = "SHA256:n28Rc3Qb7WdxtKW+RL6gz6hBv6tazm/3C3guBhRRmno"
FP_RSA = "SHA256:aNwqvtt2cox6uM9v9f9rkcivtC8orsXYyS+9zzCOJgE"
# Published documentation JWTs (jwt.io / RFC 7519): HS256 signatures recomputed below.
JWT_JWTIO = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
JWT_RFC = "eyJ0eXAiOiJKV1QiLA0KICJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
JWT_OLD_JWTIO = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiYWRtaW4iOnRydWV9.TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"
# The AWS documentation's example key pairs, joined at import so the source holds
# no key-shaped literal.
DOC_KID = "AKIA" + "IOSFODNN7EXAMPLE"
DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY"
DOC_KID2 = "AKIA" + "I44QH8DHBEXAMPLE"
DOC_SECRET2 = "je7MtGbClwBF/2Zp9Utk/" + "h3yCo8nvbEXAMPLEKEY"
# A Slack permalink's message segment: ``p`` and the thread timestamp's digits.
SLACK_P = "p" + "17315712001"

B = []


def add(cat, text):
    B.append((f"B{len(B)+1:03d}", cat, text))


# ---- git log / show / diff -------------------------------------------------
add(
    "git",
    f"""commit {SHA[0]}
Author: Ada Lovelace <ada@example.com>
Date:   Thu Nov 13 18:42:07 2026 -0800

    fix(slack): keep thread_ts when a reply is retried

    The retry path rebuilt the payload without thread_ts, so the
    retried reply landed in the channel root. Refs #37390.
""",
)
add(
    "git",
    f"""{SHORT[0]} (HEAD -> fix/thread-ts, origin/fix/thread-ts) fix(slack): keep thread_ts when a reply is retried
{SHORT[1]} (origin/main, origin/HEAD, main) Merge pull request #37388 from kirodotdev/chore/bump-aiohttp
{SHORT[2]} chore(deps): bump aiohttp from 3.14.4 to 3.14.5
{SHORT[3]} docs: explain the cron catch-up window
{SHORT[4]} test: pin the retry jitter with a frozen clock
""",
)
add(
    "git",
    f"""diff --git a/src/kiro_crew/slack/retry.py b/src/kiro_crew/slack/retry.py
index {SHA[5][:7]}..{SHA[6][:7]} 100644
--- a/src/kiro_crew/slack/retry.py
+++ b/src/kiro_crew/slack/retry.py
@@ -88,7 +88,8 @@ async def post_with_retry(client, payload, *, attempts=3):
     for attempt in range(attempts):
         try:
-            return await client.chat_postMessage(**payload)
+            kept = {{**payload, "thread_ts": payload.get("thread_ts")}}
+            return await client.chat_postMessage(**kept)
         except SlackApiError as exc:
             if exc.response.status_code != 429:
                 raise
""",
)
add(
    "git",
    f"""commit {SHA[7]}
Author: deploy-bot <deploy-bot@example.com>
Date:   Wed Nov 12 09:15:44 2026 +0000

    chore(iam): rotate the ci-deployer access key

    Old key {KID} is deactivated; new key {KID2} is in Secrets Manager
    under ci/deployer. Ticket: https://tickets.example.com/T{pick('0123456789', 9)}
""",
)
add(
    "git",
    f"""$ git show --stat HEAD~2
commit {SHA[2]}
Merge: {SHORT[8]} {SHORT[9]}
Author: dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>
Date:   Tue Nov 11 06:02:11 2026 +0000

    chore(deps): bump aiohttp from 3.14.4 to 3.14.5

 pyproject.toml | 2 +-
 uv.lock        | 8 ++++----
 2 files changed, 5 insertions(+), 5 deletions(-)
""",
)
add(
    "git",
    f"""$ git blame -L 40,44 src/kiro_crew/config/loader.py
{SHA[3][:8]} (Grace Hopper 2026-10-02 11:20:31 -0700 40)     password_file = home / ".pgpass"
{SHA[4][:8]} (Ada Lovelace 2026-11-01 08:03:12 -0700 41)     if not password_file.exists():
{SHA[4][:8]} (Ada Lovelace 2026-11-01 08:03:12 -0700 42)         return None
{SHA[3][:8]} (Grace Hopper 2026-10-02 11:20:31 -0700 43)     return password_file.read_text().strip()
""",
)
add(
    "git",
    f"""$ git rebase -i origin/main
Successfully rebased and updated refs/heads/fix/thread-ts.
$ git push --force-with-lease
Enumerating objects: 9, done.
Writing objects: 100% (5/5), 712 bytes | 712.00 KiB/s, done.
To github.com:kirodotdev/KiroCrew.git
 + {SHORT[5]}...{SHORT[0]} fix/thread-ts -> fix/thread-ts (forced update)
""",
)
add(
    "git",
    """$ git remote -v
origin\tgit@github.com:kirodotdev/KiroCrew.git (fetch)
origin\tgit@github.com:kirodotdev/KiroCrew.git (push)
upstream\thttps://github.com/kirodotdev/KiroCrew.git (fetch)
mirror\tssh://git.example.com/kirocrew/mirror.git (fetch)
""",
)
add(
    "git",
    """diff --git a/config/settings.example.yaml b/config/settings.example.yaml
--- a/config/settings.example.yaml
+++ b/config/settings.example.yaml
@@ -3,6 +3,8 @@ database:
   host: localhost
   port: 5432
-  password: changeme
+  # read from the environment; never commit a real value
+  password: ${DB_PASSWORD}
""",
)
add(
    "git",
    f"""$ git log --format='%H %s' -3 -- docs/
{SHA[9]} docs: document the redaction switch
{SHA[10]} docs: fix the api_key example in the quickstart
{SHA[11]} docs(security): describe the token rotation runbook
""",
)

# ---- gh pr view / gh run view / gh api --------------------------------------
add(
    "gh",
    """title:\tfix(slack): keep thread_ts when a reply is retried
state:\tOPEN
author:\toctocat
labels:\tbug, slack
assignees:\toctocat
reviewers:\thubot (Requested), monalisa (Approved)
projects:
milestone:\t0.14
number:\t37392
url:\thttps://github.com/kirodotdev/KiroCrew/pull/37392
additions:\t12
deletions:\t3
auto-merge:\tdisabled
--
**Goal:** a retried Slack reply stays in its thread.

- [x] unit test for the retry payload
- [ ] verified on a real workspace
""",
)
add(
    "gh",
    f"""✓ fix/thread-ts CI · {pick('0123456789', 11)}
Triggered via pull_request about 4 minutes ago

JOBS
✓ lint in 1m12s (ID {pick('0123456789', 11)})
✓ test (ubuntu-latest, 3.12) in 9m03s (ID {pick('0123456789', 11)})
X test (windows-latest, 3.12) in 11m40s (ID {pick('0123456789', 11)})
  ✓ Set up job
  X Run pytest
ANNOTATIONS
X Process completed with exit code 1.
test (windows-latest, 3.12): .github#1

To see what failed, try: gh run view {pick('0123456789', 11)} --log-failed
View this run on GitHub: https://github.com/kirodotdev/KiroCrew/actions/runs/{pick('0123456789', 11)}
""",
)
add(
    "gh",
    f"""{{
  "url": "https://api.github.com/repos/kirodotdev/KiroCrew/pulls/37392",
  "id": 2140381776,
  "node_id": "PR_kwDOK{b62(9)}",
  "html_url": "https://github.com/kirodotdev/KiroCrew/pull/37392",
  "diff_url": "https://github.com/kirodotdev/KiroCrew/pull/37392.diff",
  "state": "open",
  "user": {{"login": "octocat", "id": 81234567, "avatar_url": "https://avatars.githubusercontent.com/u/81234567?v=4", "gravatar_id": ""}},
  "head": {{"ref": "fix/thread-ts", "sha": "{SHA[0]}"}},
  "base": {{"ref": "main", "sha": "{SHA[1]}"}},
  "merge_commit_sha": "{SHA[8]}"
}}""",
)
add(
    "gh",
    """$ gh issue list --label bug --limit 3
#37401  Slack thread reply lands in channel root after 429      bug, slack   about 1 hour ago
#37377  Files view shows [REDACTED] for an AWS key id            bug, ui      about 2 days ago
#37350  password field in the onboarding form is not masked      bug, ui      about 5 days ago
""",
)
add(
    "gh",
    f"""$ gh api repos/kirodotdev/KiroCrew/commits/{SHA[0]}/check-runs --jq '.check_runs[] | [.name, .conclusion] | @tsv'
lint\tsuccess
test (ubuntu-latest, 3.12)\tsuccess
test (windows-latest, 3.12)\tfailure
internal-content-scan\tsuccess
CodeQL\tsuccess
""",
)
add(
    "gh",
    f"""$ gh pr checks 37392
All checks were successful
0 cancelled, 0 failing, 14 successful, 0 skipped, and 0 pending checks

✓  CodeQL                    2m11s  https://github.com/kirodotdev/KiroCrew/runs/{pick('0123456789', 11)}
✓  PR Hygiene                   8s  https://github.com/kirodotdev/KiroCrew/actions/runs/{pick('0123456789', 11)}/job/{pick('0123456789', 11)}
""",
)

# ---- CI logs with request ids ----------------------------------------------
add(
    "ci",
    f"""2026-11-14T07:12:01.1234567Z ##[group]Run actions/checkout@{SHA[6]}
2026-11-14T07:12:01.1234890Z with:
2026-11-14T07:12:01.1235012Z   repository: kirodotdev/KiroCrew
2026-11-14T07:12:01.1235104Z   token: ***
2026-11-14T07:12:01.1235201Z   fetch-depth: 1
2026-11-14T07:12:01.1235299Z ##[endgroup]
2026-11-14T07:12:02.4410021Z Syncing repository: kirodotdev/KiroCrew
2026-11-14T07:12:03.0021987Z [command]/usr/bin/git -c protocol.version=2 fetch --no-tags --prune --no-recurse-submodules --depth=1 origin +{SHA[0]}:refs/remotes/pull/37392/merge
""",
)
add(
    "ci",
    f"""2026-11-14T07:13:10.0000000Z ##[group]Run aws-actions/configure-aws-credentials@{SHA[7]}
2026-11-14T07:13:10.0000100Z with:
2026-11-14T07:13:10.0000200Z   role-to-assume: arn:aws:iam::{ACCT}:role/github-actions-deployer
2026-11-14T07:13:10.0000300Z   aws-region: us-west-2
2026-11-14T07:13:10.0000400Z   role-session-name: gha-{pick('0123456789', 10)}
2026-11-14T07:13:11.5121000Z Assuming role with OIDC
2026-11-14T07:13:12.0983000Z Authenticated as assumedRoleId {AROA}:gha-{pick('0123456789', 10)}
""",
)
add(
    "ci",
    f"""[Container] 2026/11/14 07:20:44.781221 Running command aws s3 cp dist/ s3://kc-artifacts-{ACCT}-us-west-2/builds/{SHORT[0]}/ --recursive
upload: dist/kirocrew-0.14.0-py3-none-any.whl to s3://kc-artifacts-{ACCT}-us-west-2/builds/{SHORT[0]}/kirocrew-0.14.0-py3-none-any.whl
[Container] 2026/11/14 07:20:47.002918 Phase complete: BUILD State: SUCCEEDED
[Container] 2026/11/14 07:20:47.003001 Phase context status code:  Message:
CODEBUILD_BUILD_ID=kc-release:{REQ[0]}
CODEBUILD_RESOLVED_SOURCE_VERSION={SHA[0]}
""",
)
add(
    "ci",
    """npm ERR! code E404
npm ERR! 404 Not Found - GET https://registry.npmjs.org/@kiro%2fcrew-ui - Not found
npm ERR! 404
npm ERR! 404  '@kiro/crew-ui@^0.14.0' is not in this registry.
npm ERR! A complete log of this run can be found in: /home/runner/.npm/_logs/2026-11-14T07_22_10_333Z-debug-0.log
""",
)
add(
    "ci",
    """=========================== short test summary info ============================
FAILED test/test_slack_retry.py::test_retry_keeps_thread_ts - AssertionError: assert None == '1731571200.123456'
FAILED test/test_slack_retry.py::test_429_backoff[jitter] - Failed: Timeout >30.0s
ERROR test/test_credential_redaction_switch.py::TestGovernedHost::test_only_the_policy_opt_in - fixture '_quiet_audit' not found
=========== 2 failed, 1311 passed, 4 skipped, 1 error in 312.44s (0:05:12) ===========
""",
)
add(
    "ci",
    f"""Error: An error occurred (ThrottlingException) when calling the DescribeStacks operation (reached max retries: 4): Rate exceeded
RequestId: {REQ[1]}
x-github-request-id: {pick('0123456789ABCDEF', 4)}:{pick('0123456789ABCDEF', 4)}:{pick('0123456789ABCDEF', 7)}:{pick('0123456789ABCDEF', 7)}:{pick('0123456789ABCDEF', 8)}
##[error]Process completed with exit code 255.
""",
)
add(
    "ci",
    f"""BUILD SUCCEEDED
  Package: kirocrew-1.0
  Channel: kirocrew/development
  Build id: {REQ[2]}
  Artifacts: https://build.example.internal/artifacts/{REQ[2]}/kirocrew-1.0.tar.gz
  Duration: 00:04:51
""",
)
add(
    "ci",
    """Run pip install -r requirements.txt --require-hashes
Collecting aiohttp==3.14.5 (from -r requirements.txt (line 1))
  Downloading aiohttp-3.14.5-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl.metadata (7.7 kB)
Successfully installed aiohttp-3.14.5 aiosignal-1.3.2 attrs-25.3.0 frozenlist-1.7.0 multidict-6.6.3 yarl-1.20.1
""",
)

# ---- AWS CLI JSON -----------------------------------------------------------
add(
    "aws",
    f"""{{
    "UserId": "{AROA}:alice",
    "Account": "{ACCT}",
    "Arn": "arn:aws:sts::{ACCT}:assumed-role/AWSReservedSSO_AdministratorAccess_{hexs(16)}/alice"
}}""",
)
add(
    "aws",
    f"""{{
    "UserId": "{AIDA}",
    "Account": "{ACCT2}",
    "Arn": "arn:aws:iam::{ACCT2}:user/ci-deployer"
}}""",
)
add(
    "aws",
    f"""{{
    "AccessKeyMetadata": [
        {{
            "UserName": "ci-deployer",
            "AccessKeyId": "{KID}",
            "Status": "Inactive",
            "CreateDate": "2025-11-12T09:15:44+00:00"
        }},
        {{
            "UserName": "ci-deployer",
            "AccessKeyId": "{KID2}",
            "Status": "Active",
            "CreateDate": "2026-11-12T09:15:44+00:00"
        }}
    ]
}}""",
)
add(
    "aws",
    """{
    "UserName": "ci-deployer",
    "AccessKeyLastUsed": {
        "LastUsedDate": "2026-11-13T22:10:00+00:00",
        "ServiceName": "s3",
        "Region": "us-west-2"
    }
}""",
)
add(
    "aws",
    f"""{{
    "Reservations": [{{
        "Instances": [{{
            "InstanceId": "i-0{hexs(16)}",
            "ImageId": "ami-0{hexs(16)}",
            "InstanceType": "m7i.2xlarge",
            "PrivateIpAddress": "10.42.3.17",
            "IamInstanceProfile": {{"Arn": "arn:aws:iam::{ACCT}:instance-profile/dev-box-role", "Id": "AIPA{pick(UPD, 17)}"}},
            "SecurityGroups": [{{"GroupName": "dev-box-sg", "GroupId": "sg-0{hexs(16)}"}}],
            "Tags": [{{"Key": "Name", "Value": "dev-box"}}, {{"Key": "aws:cloudformation:stack-id", "Value": "arn:aws:cloudformation:us-west-2:{ACCT}:stack/dev-box/{REQ[3]}"}}]
        }}],
        "OwnerId": "{ACCT}",
        "ReservationId": "r-0{hexs(16)}"
    }}]
}}""",
)
add(
    "aws",
    f"""{{
    "Configuration": {{
        "FunctionName": "kc-webhook-relay",
        "FunctionArn": "arn:aws:lambda:us-west-2:{ACCT}:function:kc-webhook-relay",
        "Runtime": "python3.12",
        "Role": "arn:aws:iam::{ACCT}:role/service-role/kc-webhook-relay-role-{b62(8).lower()}",
        "CodeSha256": "{base64.b64encode(bytes.fromhex(D64[0])).decode()}",
        "Version": "$LATEST",
        "Environment": {{"Variables": {{"LOG_LEVEL": "INFO", "SECRET_ARN": "arn:aws:secretsmanager:us-west-2:{ACCT}:secret:prod/webhook/signing-key-{b62(6)}"}}}},
        "KMSKeyArn": "arn:aws:kms:us-west-2:{ACCT}:key/{REQ[4]}",
        "RevisionId": "{REQ[5]}"
    }},
    "Code": {{"RepositoryType": "S3", "Location": "https://awslambda-us-west-2-tasks.s3.us-west-2.amazonaws.com/snapshots/{ACCT}/kc-webhook-relay-{REQ[6]}"}}
}}""",
)
add(
    "aws",
    f"""{{
    "ARN": "arn:aws:secretsmanager:us-west-2:{ACCT}:secret:prod/db/password-{b62(6)}",
    "Name": "prod/db/password",
    "Description": "RDS primary password for kc-prod",
    "KmsKeyId": "arn:aws:kms:us-west-2:{ACCT}:key/{REQ[4]}",
    "RotationEnabled": true,
    "RotationLambdaARN": "arn:aws:lambda:us-west-2:{ACCT}:function:SecretsManagerRDSPostgreSQLRotationSingleUser",
    "LastRotatedDate": "2026-11-01T00:00:12.345000+00:00",
    "VersionIdsToStages": {{"{REQ[7]}": ["AWSCURRENT"], "{REQ[8]}": ["AWSPREVIOUS"]}}
}}""",
)
add(
    "aws",
    f"""{{
    "Parameter": {{
        "Name": "/kc/prod/api_key",
        "Type": "SecureString",
        "Value": "AQICAHh{b64s(180)}==",
        "Version": 7,
        "ARN": "arn:aws:ssm:us-west-2:{ACCT}:parameter/kc/prod/api_key",
        "DataType": "text"
    }}
}}""",
)
add(
    "aws",
    f"""{{
    "AcceptRanges": "bytes",
    "LastModified": "2026-11-14T06:58:31+00:00",
    "ContentLength": 48213,
    "ETag": "\\"{hexs(32)}\\"",
    "VersionId": "{b64s(32)}",
    "ContentType": "application/zip",
    "ServerSideEncryption": "aws:kms",
    "SSEKMSKeyId": "arn:aws:kms:us-west-2:{ACCT}:key/{REQ[4]}",
    "Metadata": {{"git-sha": "{SHA[0]}"}}
}}""",
)
add(
    "aws",
    f"""      Name                    Value             Type    Location
      ----                    -----             ----    --------
   profile                      dev           manual    --profile
access_key     ****************{KID[-4:]}              sso
secret_key     ****************{b62(4)}              sso
    region                us-west-2      config-file    ~/.aws/config
""",
)
add(
    "aws",
    f"""$ aws sso login --profile dev
Attempting to automatically open the SSO authorization page in your default browser.
If the browser does not open or you wish to use a different device to authorize this request, open the following URL:

https://device.sso.us-east-1.amazonaws.com/

Then enter the code:

{pick(UPD.replace('2', '').replace('3', ''), 4)}-{pick(UPD.replace('2', '').replace('3', ''), 4)}
Successfully logged into Start URL: https://d-9067{pick('0123456789', 6)}.awsapps.com/start
""",
)
add(
    "aws",
    f"""{{
    "eventVersion": "1.09",
    "userIdentity": {{
        "type": "AssumedRole",
        "principalId": "{AROA}:alice",
        "arn": "arn:aws:sts::{ACCT}:assumed-role/Admin/alice",
        "accountId": "{ACCT}",
        "accessKeyId": "{ASIA}",
        "sessionContext": {{"sessionIssuer": {{"type": "Role", "principalId": "{AROA}", "arn": "arn:aws:iam::{ACCT}:role/Admin", "accountId": "{ACCT}", "userName": "Admin"}}, "attributes": {{"creationDate": "2026-11-14T06:00:00Z", "mfaAuthenticated": "false"}}}}
    }},
    "eventTime": "2026-11-14T06:31:05Z",
    "eventSource": "s3.amazonaws.com",
    "eventName": "PutBucketPolicy",
    "awsRegion": "us-west-2",
    "sourceIPAddress": "203.0.113.42",
    "requestID": "{XAMZREQ}",
    "eventID": "{REQ[9]}",
    "readOnly": false,
    "recipientAccountId": "{ACCT}"
}}""",
)
add(
    "aws",
    f"""$ aws iam get-role --role-name kc-deployer --query Role.AssumeRolePolicyDocument
{{
    "Version": "2012-10-17",
    "Statement": [{{
        "Effect": "Allow",
        "Principal": {{"Federated": "arn:aws:iam::{ACCT}:oidc-provider/token.actions.githubusercontent.com"}},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {{"StringEquals": {{"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"}}, "StringLike": {{"token.actions.githubusercontent.com:sub": "repo:kirodotdev/KiroCrew:ref:refs/heads/main"}}}}
    }}]
}}""",
)
add(
    "aws",
    f"""$ echo $AWS_ACCESS_KEY_ID $AWS_REGION $AWS_PROFILE
{ASIA} us-west-2 dev
""",
)
add(
    "aws",
    f"""# from the AWS CLI user guide, "Configuration basics"
$ aws configure
AWS Access Key ID [None]: {DOC_KID}
AWS Secret Access Key [None]: {DOC_SECRET}
Default region name [None]: us-west-2
Default output format [None]: json
""",
)
add(
    "aws",
    f"""[default]
aws_access_key_id = {DOC_KID}
aws_secret_access_key = {DOC_SECRET}

[user1]
aws_access_key_id = {DOC_KID2}
aws_secret_access_key = {DOC_SECRET2}
""",
)

# ---- CloudWatch log lines ---------------------------------------------------
add(
    "cw",
    f"""START RequestId: {REQ[10]} Version: $LATEST
[INFO]\t2026-11-14T07:01:02.345Z\t{REQ[10]}\trelay: posting to channel C04{pick(UPD, 8)} thread 1731571200.123456
END RequestId: {REQ[10]}
REPORT RequestId: {REQ[10]}\tDuration: 812.44 ms\tBilled Duration: 813 ms\tMemory Size: 512 MB\tMax Memory Used: 141 MB\tInit Duration: 402.10 ms
XRAY TraceId: 1-{hexs(8)}-{hexs(24)}\tSegmentId: {hexs(16)}\tSampled: true
""",
)
add(
    "cw",
    f"""{{"level":"ERROR","ts":"2026-11-14T07:02:11.900Z","requestId":"{REQ[11]}","traceId":"Root=1-{hexs(8)}-{hexs(24)};Parent={hexs(16)};Sampled=1","msg":"upstream returned 502","upstream":"https://api.internal.example.com/v2/orders?page=3&per_page=50","latency_ms":3012}}""",
)
add(
    "cw",
    f"""{{"requestId":"{REQ[0]}","ip":"203.0.113.42","caller":"-","user":"-","requestTime":"14/Nov/2026:07:03:12 +0000","httpMethod":"POST","resourcePath":"/v1/webhooks/slack","status":"200","protocol":"HTTP/1.1","responseLength":"2","integrationLatency":"88"}}""",
)
add(
    "cw",
    f"""2 {ACCT} eni-0{hexs(16)} 10.42.3.17 10.42.8.201 443 49152 6 10 8400 1731571200 1731571260 ACCEPT OK
2 {ACCT} eni-0{hexs(16)} 198.51.100.7 10.42.3.17 22 51022 6 3 180 1731571200 1731571260 REJECT OK
""",
)
add(
    "cw",
    f"""2026-11-14 07:04:55,120 INFO  [main] c.e.kc.OrderService - Processed order {uuid()} for customer cust_{b62(14)} in 41 ms (requestId={REQ[1]})
2026-11-14 07:04:55,388 WARN  [pool-3-thread-7] c.e.kc.PaymentClient - Retrying charge ch_{b62(24)} after 503 (attempt 2/3)
""",
)
add(
    "cw",
    """fields @timestamp, @message, @logStream
| filter @message like /ERROR/ and @logStream like /kc-webhook-relay/
| stats count(*) as errors by bin(5m)
| sort errors desc
| limit 20
""",
)
add(
    "cw",
    f"""[ecs] 2026-11-14T07:06:00Z task arn:aws:ecs:us-west-2:{ACCT}:task/kc-prod/{hexs(32)} container kc-api image {ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api@sha256:{D64[1]} stopped: Essential container in task exited (exit code 137)""",
)

# ---- docker -----------------------------------------------------------------
add(
    "docker",
    f"""#1 [internal] load build definition from Dockerfile
#1 transferring dockerfile: 1.21kB done
#5 [2/6] RUN pip install --no-cache-dir -r requirements.txt
#5 CACHED
#9 exporting to image
#9 exporting layers 0.4s done
#9 writing image sha256:{D64[2]} done
#9 naming to {ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api:{SHORT[0]} done
#9 DONE 0.6s
""",
)
add(
    "docker",
    f"""$ docker pull public.ecr.aws/docker/library/python:3.12-slim
3.12-slim: Pulling from docker/library/python
{hexs(12)}: Pull complete
{hexs(12)}: Pull complete
Digest: sha256:{D64[3]}
Status: Downloaded newer image for public.ecr.aws/docker/library/python:3.12-slim
public.ecr.aws/docker/library/python:3.12-slim
""",
)
add(
    "docker",
    f"""REPOSITORY                                               TAG        DIGEST                                                                    IMAGE ID       CREATED        SIZE
{ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api            {SHORT[0]}    sha256:{D64[4]}   {hexs(12)}   2 hours ago    412MB
python                                                   3.12-slim  sha256:{D64[3]}   {hexs(12)}   9 days ago     124MB
""",
)
add(
    "docker",
    f"""$ aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin {ACCT}.dkr.ecr.us-west-2.amazonaws.com
Login Succeeded
$ docker push {ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api:{SHORT[0]}
{SHORT[0]}: digest: sha256:{D64[4]} size: 2203
""",
)
add(
    "docker",
    f"""FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:{D64[3]}
ARG PIP_INDEX_URL
ENV PYTHONDONTWRITEBYTECODE=1
RUN --mount=type=secret,id=pip_token pip install -r requirements.txt
COPY --chown=app:app src/ /app/src/
""",
)

# ---- lockfiles --------------------------------------------------------------
add(
    "lock",
    """    "node_modules/react": {
      "version": "18.3.1",
      "resolved": "https://registry.npmjs.org/react/-/react-18.3.1.tgz",
      "integrity": "sha512-wS+hAgJShR0KhEvPJArfuPVN1+Hz1t0Y6n5jLrGQbkb4urgPE/0Rve+1kMB1v/oWgHgm4WIcV+i7F2pTVj+2iQ==",
      "dependencies": {
        "loose-envify": "^1.1.0"
      }
    },""",
)
add(
    "lock",
    f"""[[package]]
name = "aiohttp"
version = "3.14.5"
description = "Async http client/server framework (asyncio)"
optional = false
python-versions = ">=3.9"
files = [
    {{file = "aiohttp-3.14.5-cp312-cp312-macosx_11_0_arm64.whl", hash = "sha256:{D64[5]}"}},
    {{file = "aiohttp-3.14.5.tar.gz", hash = "sha256:{D64[6]}"}},
]
""",
)
add(
    "lock",
    f"""react@^18.3.1:
  version "18.3.1"
  resolved "https://registry.yarnpkg.com/react/-/react-18.3.1.tgz#{SHA[3]}"
  integrity sha512-{b64s(86)}==
""",
)
add(
    "lock",
    f"""github.com/aws/aws-sdk-go-v2 v1.32.4 h1:{b64s(43)}=
github.com/aws/aws-sdk-go-v2 v1.32.4/go.mod h1:{b64s(43)}=
golang.org/x/oauth2 v0.24.0 h1:{b64s(43)}=
""",
)
add(
    "lock",
    f"""[[package]]
name = "serde"
version = "1.0.215"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{D64[7]}"
""",
)
add(
    "lock",
    f"""aiohttp==3.14.5 \\
    --hash=sha256:{D64[5]} \\
    --hash=sha256:{D64[6]}
""",
)

# ---- terraform --------------------------------------------------------------
add(
    "tf",
    f"""  # aws_db_instance.kc will be updated in-place
  ~ resource "aws_db_instance" "kc" {{
        id                    = "db-{pick(UPD, 26)}"
      ~ instance_class        = "db.r6g.large" -> "db.r6g.xlarge"
      ~ password              = (sensitive value)
        tags                  = {{
            "Name" = "kc-prod"
        }}
        # (41 unchanged attributes hidden)
    }}

Plan: 0 to add, 1 to change, 0 to destroy.
""",
)
add(
    "tf",
    """  # aws_iam_access_key.ci will be created
  + resource "aws_iam_access_key" "ci" {
      + create_date                    = (known after apply)
      + encrypted_secret               = (known after apply)
      + id                             = (known after apply)
      + secret                         = (sensitive value)
      + ses_smtp_password_v4           = (sensitive value)
      + status                         = "Active"
      + user                           = "ci-deployer"
    }
""",
)
add(
    "tf",
    """resource "aws_db_instance" "kc" {
  identifier        = "kc-prod"
  engine            = "postgres"
  username          = "kc_admin"
  password          = var.db_password
  kms_key_id        = aws_kms_key.db.arn
}

variable "db_password" {
  type      = string
  sensitive = true
}
""",
)
add(
    "tf",
    f"""$ terraform state show aws_security_group.dev_box
# aws_security_group.dev_box:
resource "aws_security_group" "dev_box" {{
    arn         = "arn:aws:ec2:us-west-2:{ACCT}:security-group/sg-0{hexs(16)}"
    id          = "sg-0{hexs(16)}"
    name        = "dev-box-sg"
    owner_id    = "{ACCT}"
    vpc_id      = "vpc-0{hexs(16)}"
}}
""",
)
add(
    "tf",
    f"""Outputs:

api_endpoint = "https://{b62(10).lower()}.execute-api.us-west-2.amazonaws.com/prod"
bucket_arn = "arn:aws:s3:::kc-artifacts-{ACCT}-us-west-2"
db_password = <sensitive>
role_arn = "arn:aws:iam::{ACCT}:role/kc-deployer"
""",
)

# ---- kubectl ----------------------------------------------------------------
add(
    "k8s",
    """NAME                       READY   STATUS             RESTARTS        AGE   IP             NODE
kc-api-6d8f9b7c5d-x2kqp    1/1     Running            0               3h    10.42.12.31    ip-10-42-3-17.us-west-2.compute.internal
kc-api-6d8f9b7c5d-zt9wm    0/1     CrashLoopBackOff   7 (2m ago)      3h    10.42.12.44    ip-10-42-3-18.us-west-2.compute.internal
kc-worker-7f4c8d6b9-lmn4r  1/1     Running            1 (45m ago)     3h    10.42.13.9     ip-10-42-3-19.us-west-2.compute.internal
""",
)
add(
    "k8s",
    f"""Containers:
  kc-api:
    Container ID:   containerd://{D64[0]}
    Image:          {ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api:{SHORT[0]}
    Image ID:       {ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api@sha256:{D64[4]}
    Port:           8080/TCP
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
    Environment:
      DB_HOST:      kc-prod.cluster-{b62(12).lower()}.us-west-2.rds.amazonaws.com
      DB_PASSWORD:  <set to the key 'password' in secret 'kc-db'>  Optional: false
      API_KEY:      <set to the key 'api_key' in secret 'kc-api'>  Optional: false
""",
)
add(
    "k8s",
    """$ kubectl describe secret kc-db
Name:         kc-db
Namespace:    prod
Labels:       app=kc
Annotations:  <none>

Type:  Opaque

Data
====
password:  24 bytes
username:  8 bytes
""",
)
add(
    "k8s",
    """$ kubectl get secrets
NAME                  TYPE                                  DATA   AGE
kc-db                 Opaque                                2      41d
kc-api                Opaque                                1      41d
kc-registry           kubernetes.io/dockerconfigjson        1      41d
default-token-x7k2p   kubernetes.io/service-account-token   3      120d
""",
)
add(
    "k8s",
    f"""apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {base64.b64encode(CERT_EC.encode()).decode()}
    server: https://{hexs(32).upper()}.gr7.us-west-2.eks.amazonaws.com
  name: arn:aws:eks:us-west-2:{ACCT}:cluster/kc-prod
users:
- name: arn:aws:eks:us-west-2:{ACCT}:cluster/kc-prod
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args: ["--region", "us-west-2", "eks", "get-token", "--cluster-name", "kc-prod"]
""",
)
add(
    "k8s",
    f"""Events:
  Type     Reason     Age                 From               Message
  ----     ------     ----                ----               -------
  Normal   Pulled     3m (x8 over 3h)     kubelet            Container image "{ACCT}.dkr.ecr.us-west-2.amazonaws.com/kc-api:{SHORT[0]}" already present on machine
  Warning  BackOff    2m (x41 over 3h)    kubelet            Back-off restarting failed container kc-api in pod kc-api-6d8f9b7c5d-zt9wm_prod({REQ[2]})
""",
)

# ---- stack traces -----------------------------------------------------------
add(
    "trace",
    """Traceback (most recent call last):
  File "/home/dev/work/KiroCrew/src/kiro_crew/slack/retry.py", line 91, in post_with_retry
    return await client.chat_postMessage(**kept)
  File "/home/dev/work/venv/lib/python3.12/site-packages/slack_sdk/web/async_client.py", line 2401, in chat_postMessage
    return await self.api_call("chat.postMessage", json=kwargs)
slack_sdk.errors.SlackApiError: The request to the Slack API failed. (url: https://slack.com/api/chat.postMessage)
The server responded with: {'ok': False, 'error': 'ratelimited', 'response_metadata': {'messages': []}}
""",
)
add(
    "trace",
    f"""com.amazonaws.services.s3.model.AmazonS3Exception: Access Denied (Service: Amazon S3; Status Code: 403; Error Code: AccessDenied; Request ID: {XAMZREQ}; S3 Extended Request ID: {XAMZ2}; Proxy: null)
\tat com.amazonaws.http.AmazonHttpClient$RequestExecutor.handleErrorResponse(AmazonHttpClient.java:1879)
\tat com.amazonaws.services.s3.AmazonS3Client.invoke(AmazonS3Client.java:5445)
\tat com.example.kc.ArtifactStore.fetch(ArtifactStore.java:88)
\tat com.example.kc.ReleaseJob.run(ReleaseJob.java:142)
Caused by: java.net.SocketTimeoutException: Read timed out
\t... 12 more
""",
)
add(
    "trace",
    """TypeError: Cannot read properties of undefined (reading 'thread_ts')
    at postReply (/app/dist/slack/reply.js:42:31)
    at async Promise.all (index 0)
    at async handleEvent (/app/dist/slack/events.js:118:5)
""",
)
add(
    "trace",
    f"""panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x18 pc=0x{hexs(7)}]

goroutine 42 [running]:
github.com/example/kc/internal/relay.(*Client).Post(0xc000{hexs(6)}, {{0x{hexs(7)}, 0xc000{hexs(6)}}})
\t/src/internal/relay/client.go:77 +0x{hexs(3)}
""",
)
add(
    "trace",
    f"""botocore.exceptions.ClientError: An error occurred (ExpiredToken) when calling the GetObject operation: The provided token has expired.
  File "/tmp/tmp{b62(8).lower()}/handler.py", line 14, in lambda_handler
    body = s3.get_object(Bucket="kc-artifacts-{ACCT}-us-west-2", Key=key)["Body"].read()
""",
)
add(
    "trace",
    """thread 'main' panicked at src/config.rs:41:10:
called `Result::unwrap()` on an `Err` value: Os { code: 2, kind: NotFound, message: "No such file or directory" }
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
""",
)

# ---- OAuth sign-in URLs (public params) -------------------------------------
add(
    "oauth",
    f"""Open this link to sign in: https://accounts.google.com/o/oauth2/v2/auth?client_id=812345678901-{b62(32).lower()}.apps.googleusercontent.com&redirect_uri=http%3A%2F%2Flocalhost%3A8085%2Fcallback&response_type=code&scope=openid%20email%20profile&state={b64u(32)}&code_challenge={b64u(43)}&code_challenge_method=S256&nonce={b64u(22)}&access_type=offline&prompt=consent""",
)
add(
    "oauth",
    f"""https://github.com/login/oauth/authorize?client_id=Iv1.{hexs(16)}&redirect_uri=https%3A%2F%2Fcrew.example.com%2Fapi%2Fconnections%2Fgithub%2Fcallback&scope=repo%20read%3Aorg&state={hexs(32)}&allow_signup=false""",
)
add(
    "oauth",
    f"""https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id={REQ[3]}&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A53682%2F&response_mode=query&scope=offline_access%20User.Read%20Mail.Read&state={b64u(24)}&code_challenge={b64u(43)}&code_challenge_method=S256""",
)
add(
    "oauth",
    f"""https://kc-prod.auth.us-west-2.amazoncognito.com/oauth2/authorize?response_type=code&client_id={b62(26).lower()}&redirect_uri=https://crew.example.com/oauth2/idpresponse&state={b64u(40)}&scope=openid+email+profile&identity_provider=ExampleIdP""",
)
add(
    "oauth",
    f"""https://slack.com/oauth/v2/authorize?client_id={pick('0123456789', 13)}.{pick('0123456789', 13)}&scope=chat:write,channels:history,reactions:write&user_scope=&redirect_uri=https%3A%2F%2Fcrew.example.com%2Fslack%2Foauth&state={b64u(40)}""",
)
add(
    "oauth",
    f"""To finish signing in, open https://view.awsapps.com/start/#/device?user_code={pick('BCDFGHJKLMNPQRSTVWXZ', 4)}-{pick('BCDFGHJKLMNPQRSTVWXZ', 4)} and approve the request from "Kiro CLI".""",
)

# ---- links ------------------------------------------------------------------
add(
    "links",
    f"""See https://github.com/kirodotdev/KiroCrew/blob/{SHA[0]}/src/kiro_crew/slack/retry.py#L88-L96 for the retry loop and https://github.com/kirodotdev/KiroCrew/compare/{SHORT[1]}...{SHORT[0]} for the diff.""",
)
add(
    "links",
    f"""Thread: https://example.slack.com/archives/C04{pick(UPD, 8)}/{SLACK_P}{pick('0123456789', 5)}?thread_ts=1731571200.123456&cid=C04{pick(UPD, 8)}""",
)
add(
    "links",
    f"""Tracked in https://example.atlassian.net/browse/KC-1234?focusedCommentId=10234&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel#comment-10234 and the design is at https://docs.example.com/{b62(12)}/Redaction-Switch-Design""",
)
add(
    "links",
    f"""Review: https://review.example.com/changes/{pick('0123456789', 9)}/revisions/2#/details
Source: https://git.example.com/kirocrew/mirror/blob/main/src/kiro_crew/edition.py#L120
Pipeline: https://ci.example.com/pipelines/kirocrew-release""",
)
add(
    "links",
    f"""Wiki: https://wiki.example.com/KiroCrew/Runbooks/RotateDeployKey/ and ticket https://tickets.example.com/T{pick('0123456789', 9)}/communication""",
)
add(
    "links",
    f"""Dashboard: https://us-west-2.console.aws.amazon.com/cloudwatch/home?region=us-west-2#logsV2:log-groups/log-group/$252Faws$252Flambda$252Fkc-webhook-relay/log-events/2026$252F11$252F14$252F$255B$2524LATEST$255D{hexs(32)}""",
)
add(
    "links",
    "Logs Insights: https://us-west-2.console.aws.amazon.com/cloudwatch/home?region=us-west-2#logsV2:logs-insights$3FqueryDetail$3D~(end~0~start~-3600~timeType~'RELATIVE~unit~'seconds~editorString~'fields*20*40timestamp*2c*20*40message*0a*7c*20filter*20*40message*20like*20*2fERROR*2f~source~(~'*2faws*2flambda*2fkc-webhook-relay))",
)
add(
    "links",
    f"""Design doc: https://docs.google.com/document/d/1{b64u(43)}/edit?usp=sharing&ouid={pick('0123456789', 21)}&rtpof=true""",
)
add(
    "links",
    f"""Console sign-in: https://signin.example.com/federate?account={ACCT}&role=ReadOnly&destination=cloudwatch%2Fhome%3Fregion%3Dus-west-2""",
)
add(
    "links",
    f"""Grafana: https://grafana.example.com/d/{b62(9)}/kc-api?orgId=1&from=now-6h&to=now&var-cluster=kc-prod&var-namespace=prod&refresh=30s""",
)
add(
    "links",
    f"""Artifact: https://github.com/kirodotdev/KiroCrew/actions/runs/{pick('0123456789', 11)}/artifacts/{pick('0123456789', 10)} (expires in 90 days)""",
)
add(
    "links",
    "Release notes: https://github.com/kirodotdev/KiroCrew/releases/tag/v0.14.0 and the PyPI page https://pypi.org/project/kirocrew/0.14.0/#files",
)
add(
    "links",
    f"""S3 object: s3://kc-artifacts-{ACCT}-us-west-2/builds/{SHORT[0]}/kirocrew-0.14.0-py3-none-any.whl
Console: https://s3.console.aws.amazon.com/s3/object/kc-artifacts-{ACCT}-us-west-2?region=us-west-2&bucketType=general&prefix=builds/{SHORT[0]}/kirocrew-0.14.0-py3-none-any.whl""",
)
add(
    "links",
    f"""Sentry: https://example.sentry.io/issues/{pick('0123456789', 10)}/?project={pick('0123456789', 7)}&query=is%3Aunresolved&referrer=issue-stream&statsPeriod=14d""",
)

# ---- markdown tables --------------------------------------------------------
add(
    "md",
    """| Variable | Required | Description |
|---|---|---|
| `KIROCREW_HOME` | no | Data home, default `~/.kiro/crew` |
| `API_KEY` | yes | Your provider API key |
| `DB_PASSWORD` | yes | Database password, read at boot |
| `SLACK_BOT_TOKEN` | no | The bot token (`xoxb-...`) |
""",
)
add(
    "md",
    f"""| Account | Id | Role ARN |
|---|---|---|
| dev | {ACCT} | `arn:aws:iam::{ACCT}:role/Admin` |
| prod | {ACCT2} | `arn:aws:iam::{ACCT2}:role/ReadOnly` |
""",
)
add(
    "md",
    f"""| Commit | Author | Message |
|--------|--------|---------|
| [`{SHORT[0]}`](https://github.com/kirodotdev/KiroCrew/commit/{SHA[0]}) | octocat | fix(slack): keep thread_ts |
| [`{SHORT[2]}`](https://github.com/kirodotdev/KiroCrew/commit/{SHA[2]}) | dependabot | bump aiohttp |
""",
)
add(
    "md",
    """| Field | Type | Notes |
|---|---|---|
| `username` | string | 3 to 32 characters |
| `password` | string | at least 12 characters, never logged |
| `token` | string | returned by `POST /v1/sessions`, valid 1 hour |
""",
)
add(
    "md",
    f"""| Key ID | Status | Last used |
|---|---|---|
| {KID} | Inactive | 2026-11-12 |
| {KID2} | Active | 2026-11-13 |
""",
)
add(
    "md",
    """| Metric | p50 | p99 |
|---:|---:|---:|
| redact 100 KB | 4.1 ms | 9.8 ms |
| stream chunk | 0.06 ms | 0.31 ms |
""",
)

# ---- config examples with placeholders --------------------------------------
add(
    "cfg",
    """```yaml
database:
  host: localhost
  port: 5432
  user: app
  password: ${DB_PASSWORD}
redis:
  url: redis://localhost:6379/0
```""",
)
add(
    "cfg",
    """```json
{
  "api_key": "<your-api-key>",
  "endpoint": "https://api.example.com/v1",
  "timeout_seconds": 30
}
```""",
)
add(
    "cfg",
    f"""```bash
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="your-key-here"
export AWS_SECRET_ACCESS_KEY={'x' * 40}
```""",
)
add(
    "cfg",
    """```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
password = os.getenv("DB_PASSWORD", "")
```""",
)
add(
    "cfg",
    """```yaml
- name: Log in to Docker Hub
  uses: docker/login-action@v3
  with:
    username: ${{ vars.DOCKERHUB_USERNAME }}
    password: ${{ secrets.DOCKERHUB_TOKEN }}
```""",
)
add(
    "cfg",
    """spring.datasource.url=jdbc:postgresql://localhost:5432/app
spring.datasource.username=app
spring.datasource.password=${DB_PASSWORD:}
""",
)
add(
    "cfg",
    """```toml
[tool.poetry]
name = "kc-tools"

[[tool.poetry.source]]
name = "internal"
url = "https://pypi.internal.example.com/simple/"
priority = "supplemental"
```""",
)
add(
    "cfg",
    """```env
# .env.example -- copy to .env and fill in
DATABASE_URL=postgres://USER:PASSWORD@localhost:5432/app
SLACK_BOT_TOKEN=xoxb-your-bot-token
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
STRIPE_SECRET_KEY=sk_test_YOUR_KEY_HERE
API_KEY=<redacted>
```""",
)
add(
    "cfg",
    """```js
const res = await fetch(`${BASE_URL}/v1/orders`, {
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
});
```""",
)
add(
    "cfg",
    """```go
req.Header.Set("Authorization", "Bearer "+os.Getenv("API_TOKEN"))
req.Header.Set("X-Api-Key", cfg.APIKey)
```""",
)
add(
    "cfg",
    """```bash
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/kirodotdev/KiroCrew/pulls/37392
curl -u "$JIRA_USER:$JIRA_API_TOKEN" https://example.atlassian.net/rest/api/3/issue/KC-1234
```""",
)
add(
    "cfg",
    """```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: example
      POSTGRES_DB: app
```""",
)
add(
    "cfg",
    """```ini
[client]
user = app
password = {{ vault_db_password }}
host = db.internal.example.com
```""",
)
add(
    "cfg",
    """Set `Authorization: Bearer <token>` on every request, where `<token>` is the value returned by `POST /oauth/token`. Example: `curl -H 'Authorization: Bearer YOUR_ACCESS_TOKEN' https://api.example.com/v1/me`""",
)
add(
    "cfg",
    """```java
String password = System.getenv("DB_PASSWORD");
DataSource ds = DataSourceBuilder.create().url(url).username(user).password(password).build();
```""",
)
add(
    "cfg",
    """```python
def login(username: str, password: str) -> Session:
    user = repo.find(username)
    if user is None or not bcrypt.checkpw(password.encode(), user.password_hash):
        raise AuthError("invalid username or password")
    return Session.issue(user, token=secrets.token_urlsafe(32))
```""",
)
add(
    "cfg",
    """```sql
SELECT id, email FROM users WHERE email = $1 AND password_hash = crypt($2, password_hash);
UPDATE api_keys SET revoked_at = now() WHERE key_id = $1;
```""",
)

# ---- certificates, ssh, gpg -------------------------------------------------
add("cert", CERT_EC)
add(
    "cert",
    "Server certificate (from `openssl s_client -showcerts`):\n"
    + CERT_RSA
    + "\nsubject=C=US, ST=Washington, L=Seattle, O=Example Corp, CN=api.internal.example.com\nVerify return code: 18 (self-signed certificate)",
)
add("ssh", SSH_ED)
add(
    "ssh",
    f"""$ ssh-keygen -lf ~/.ssh/id_ed25519.pub
256 {FP_ED} alice@dev-box (ED25519)
$ ssh-keygen -lf ~/.ssh/deploy_rsa.pub
2048 {FP_RSA} deploy@ci (RSA)""",
)
add(
    "ssh",
    f"""The authenticity of host 'dev-box (10.42.3.17)' can't be established.
ED25519 key fingerprint is {FP_ED}.
This key is not known by any other names.
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
Warning: Permanently added 'dev-box' (ED25519) to the list of known hosts.""",
)
add("ssh", "# ~/.ssh/authorized_keys\n" + SSH_RSA + "\n" + SSH_ED)
add(
    "ssh",
    """github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=""",
)
add(
    "gpg",
    f"""$ gpg --list-keys --fingerprint ada@example.com
pub   ed25519 2025-03-02 [SC] [expires: 2027-03-02]
      {' '.join(pick('0123456789ABCDEF', 4) for _ in range(5))}  {' '.join(pick('0123456789ABCDEF', 4) for _ in range(5))}
uid           [ultimate] Ada Lovelace <ada@example.com>
sub   cv25519 2025-03-02 [E] [expires: 2027-03-02]""",
)
add(
    "gpg",
    "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"
    + "\n".join(b64s(64) for _ in range(6))
    + "\n="
    + b64s(4)
    + "\n-----END PGP PUBLIC KEY BLOCK-----",
)

# ---- data URIs, JWT doc examples, uuids, hashes ----------------------------
add("img", f"""![build status]({PNG1})""")
add(
    "img",
    f"""<img alt="logo" width="16" src="{SVG_URI}"> and the favicon <link rel="icon" href="{PNG2}">""",
)
add(
    "img",
    f"""{{"type": "image", "mimeType": "image/png", "data": "{PNG1.split(',', 1)[1]}"}}""",
)
add(
    "jwt-doc",
    f"""From jwt.io, the default example token:

{JWT_JWTIO}

Decoded payload: {{"sub": "1234567890", "name": "John Doe", "iat": 1516239022}}""",
)
add(
    "jwt-doc",
    f"""RFC 7519, section 3.1, gives this example JWT:

  {JWT_RFC}""",
)
add(
    "jwt-doc",
    f"""Example (the older jwt.io sample, HS256 with the secret `secret`): `Authorization: Bearer {JWT_OLD_JWTIO}`""",
)
add(
    "ids",
    f"""correlation-id: {REQ[4]}
x-request-id: {REQ[5]}
traceparent: 00-{hexs(32)}-{hexs(16)}-01
idempotency-key: {REQ[6]}""",
)
add(
    "ids",
    f"""$ sha256sum dist/*
{D64[5]}  dist/kirocrew-0.14.0-py3-none-any.whl
{D64[6]}  dist/kirocrew-0.14.0.tar.gz
$ md5sum dist/kirocrew-0.14.0.tar.gz
{hexs(32)}  dist/kirocrew-0.14.0.tar.gz
$ git rev-parse HEAD
{SHA[0]}""",
)
add(
    "ids",
    f"""<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" integrity="sha384-{b64s(64)}" crossorigin="anonymous"></script>""",
)
add(
    "ids",
    f"""HTTP/1.1 200 OK
x-amz-id-2: {XAMZ2}
x-amz-request-id: {XAMZREQ}
Date: Fri, 14 Nov 2026 07:30:00 GMT
x-amz-version-id: {b64s(32).replace('+', '.').replace('/', '_')}
ETag: "{hexs(32)}"
x-amz-server-side-encryption: aws:kms
x-amz-server-side-encryption-aws-kms-key-id: arn:aws:kms:us-west-2:{ACCT}:key/{REQ[4]}
Set-Cookie: AWSALB={b64s(120)}; Expires=Fri, 21 Nov 2026 07:30:00 GMT; Path=/
Set-Cookie: theme=dark; Path=/; SameSite=Lax
Content-Type: application/json""",
)
add(
    "ids",
    f"""Stripe publishable key for the test page: pk_test_{b62(99)}
Sentry DSN (public, safe in the frontend bundle): https://{hexs(32)}@o{pick('0123456789', 6)}.ingest.us.sentry.io/{pick('0123456789', 7)}""",
)
add(
    "ids",
    f"""Object versions:
  {b64s(32)}  2026-11-14T06:58:31Z  latest
  {b64s(32)}  2026-11-13T19:02:10Z
Upload id: {b64s(86).replace('+', '.').replace('/', '_')}""",
)

# ---- CJK / emoji ------------------------------------------------------------
add(
    "cjk",
    f"""部署完成 ✅ 版本 v0.14.0 已发布到生产环境。请查看 https://github.com/kirodotdev/KiroCrew/pull/37392 的状态，提交 {SHORT[0]}。""",
)
add(
    "cjk",
    """パスワードは安全に保管してください。アクセストークンの有効期限は1時間です。トークンを共有しないでください。""",
)
add(
    "cjk",
    """비밀번호를 재설정했습니다. 새 API 키는 비밀 관리자에 저장되어 있습니다 🔐""",
)
add(
    "cjk",
    f"""🚀 Deployed kc-api {SHORT[0]} to prod 🎉  p99 latency 212ms → 148ms 📉  rollback: `kubectl rollout undo deploy/kc-api` 🙏""",
)
add(
    "cjk",
    """密码：请联系管理员获取（不要发在群里）
令牌：在 设置 → 安全 中重新生成""",
)

# ---- shell sessions ---------------------------------------------------------
add(
    "shell",
    f"""alice@dev-box-{hexs(8)} ~/work/KiroCrew (fix/thread-ts) $ git status -sb
## fix/thread-ts...origin/fix/thread-ts [ahead 1]
 M src/kiro_crew/slack/retry.py
?? test/test_slack_retry.py
alice@dev-box-{hexs(8)} ~/work/KiroCrew (fix/thread-ts) $ python -m pytest -q test/test_slack_retry.py
..                                                                       [100%]
2 passed in 0.41s""",
)
add(
    "shell",
    f"""$ export AWS_PROFILE=dev AWS_REGION=us-west-2
$ aws s3 ls s3://kc-artifacts-{ACCT}-us-west-2/builds/ | tail -3
                           PRE {SHORT[3]}/
                           PRE {SHORT[2]}/
                           PRE {SHORT[0]}/""",
)
add(
    "shell",
    f"""$ ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes ec2-user@10.42.3.17
Last login: Fri Nov 14 06:59:12 2026 from 10.42.0.5
[ec2-user@ip-10-42-3-17 ~]$ sudo journalctl -u kc-api --since "10 min ago" | tail -2
Nov 14 07:31:02 ip-10-42-3-17 kc-api[2211]: INFO request_id={REQ[7]} POST /v1/orders 201 41ms
Nov 14 07:31:05 ip-10-42-3-17 kc-api[2211]: WARN request_id={REQ[8]} token refresh took 1.2s""",
)
add(
    "shell",
    """$ pip install -e '.[dev]'
Obtaining file:///home/dev/work/KiroCrew
  Installing build dependencies ... done
Successfully installed kirocrew-0.14.0
$ npm ci --prefix website
added 1412 packages, and audited 1413 packages in 38s
found 0 vulnerabilities""",
)
add(
    "shell",
    """$ ls -la ~/.aws
total 16
drwx------  3 alice staff   96 Nov 14 06:00 .
-rw-------  1 alice staff  412 Nov 14 06:00 config
-rw-------  1 alice staff  229 Nov  2 10:11 credentials
drwxr-xr-x  4 alice staff  128 Nov 14 06:00 sso""",
)
add(
    "shell",
    """$ kirocrew doctor
✓ Python 3.12.16
✓ kiro-cli 1.21.0
✓ Gateway reachable on http://127.0.0.1:8484
! Slack: bot token present, app-level token missing (Socket Mode disabled)
✓ Credential redaction: on""",
)
add(
    "shell",
    f"""$ curl -sI https://kc-artifacts-{ACCT}-us-west-2.s3.us-west-2.amazonaws.com/builds/{SHORT[0]}/kirocrew-0.14.0.tar.gz | head -3
HTTP/1.1 403 Forbidden
x-amz-request-id: {XAMZREQ}
x-amz-id-2: {XAMZ2}""",
)
add(
    "shell",
    """$ env | grep -E '^(HOME|SHELL|LANG|TERM)='
HOME=/home/dev
SHELL=/bin/zsh
LANG=en_US.UTF-8
TERM=xterm-256color""",
)

# ---- temp paths -------------------------------------------------------------
add(
    "paths",
    "Wrote the report to /var/folders/7x/k2m9f3vd4hqb1l0p8z6n2w5r0000gn/T/tmpa8f3k2lq/report.html and the raw data to /tmp/pytest-of-dev/pytest-42/test_render0/out.json",
)
add(
    "paths",
    r"""Unpacked to C:\Users\dev\AppData\Local\Temp\pip-install-k3j2l1x0\aiohttp_5f0c2a\ (217 files)""",
)
add(
    "paths",
    f"""/home/dev/.kiro/crew/sessions/{REQ[9]}/transcript.jsonl:1288: {{"role": "assistant", "content": "done"}}""",
)

# ---- ordinary prose ---------------------------------------------------------
add(
    "prose",
    """The password reset link expires after 24 hours. If the token has expired, request a new one from the sign-in page.""",
)
add(
    "prose",
    """Rotate the API key every 90 days, and never paste a secret into a ticket. Bearer tokens belong in the Authorization header, not in the URL.""",
)
add(
    "prose",
    """password: see the 1Password vault "Team Infra", item "kc-prod db"; ask in #kc-oncall if you lack access.""",
)
add(
    "prose",
    """Requirements:
- Password: at least 12 characters, one digit
- API key: required for the batch endpoint
- Token: optional, defaults to the session token""",
)
add(
    "prose",
    """We store only a bcrypt hash of the password, and the secret_key used for session signing lives in Secrets Manager. The access_token is never written to disk.""",
)
add(
    "prose",
    """I re-ran the job with the new token and it passed; the old one had been revoked by the 90-day policy. Next step: delete the unused key pair from IAM.""",
)
add(
    "prose",
    """Tip: `git config --global credential.helper osxkeychain` stores your GitHub token in the macOS keychain, so you are not asked for a password on every push.""",
)
add(
    "prose",
    """Review summary: 3 files changed. The `api_key` argument is now keyword-only, `password=` is no longer accepted positionally, and `token` defaults to `None`.""",
)
add(
    "prose",
    """Kiro Crew is a personal AI agent. You can chat with it from the dashboard, Slack or Discord, schedule cron jobs, and it remembers lessons across sessions.""",
)
add(
    "prose",
    """The deploy is blocked on the change freeze until Monday 09:00 PT. After that, promote build 1311 through beta, gamma and prod with a 2-hour bake in each.""",
)

# ---- misc code and output ---------------------------------------------------
add(
    "code",
    """```python
@dataclass
class DbConfig:
    host: str = "localhost"
    port: int = 5432
    user: str = "app"
    password: str = field(default="", repr=False)
    api_key: Optional[str] = None
```""",
)
add(
    "code",
    """```ts
export interface Credentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken?: string;
}
const creds: Credentials = await provider();
```""",
)
add(
    "code",
    """```python
headers = {"Authorization": f"Bearer {token}", "User-Agent": "kc/0.14"}
resp = httpx.get(url, headers=headers, timeout=10)
resp.raise_for_status()
```""",
)
add(
    "code",
    """```bash
#!/usr/bin/env bash
set -euo pipefail
TOKEN="$(aws secretsmanager get-secret-value --secret-id ci/github --query SecretString --output text)"
gh auth login --with-token <<<"$TOKEN"
```""",
)
add(
    "code",
    """```python
assert redact("api_key = os.environ['API_KEY']") == "api_key = os.environ['API_KEY']"
```""",
)
add(
    "code",
    f"""{{"level":"debug","msg":"cache hit","key":"user:{REQ[10]}:prefs","etag":"W/\\"{hexs(16)}\\"","age_s":42}}""",
)
add(
    "code",
    """<form method="post" action="/login">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Sign in</button>
</form>""",
)
add(
    "code",
    f"""INSERT INTO deploys (id, sha, env, started_at) VALUES ('{REQ[11]}', '{SHA[0]}', 'prod', '2026-11-14T07:00:00Z');""",
)
add(
    "code",
    """Base64 of "hello world": aGVsbG8gd29ybGQ= and of the JSON {"a":1}: eyJhIjoxfQ==""",
)
add(
    "code",
    f"""const config = {{ region: "us-west-2", accountId: "{ACCT}", bucket: `kc-artifacts-${{accountId}}-us-west-2`, keyId: "{KID}" }};""",
)


#: ``(id, category, text)``
BENIGN: list[tuple[str, str, str]] = B
