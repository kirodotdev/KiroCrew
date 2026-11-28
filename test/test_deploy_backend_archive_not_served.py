"""Backend code archives stay off the shared distribution.

deploy-backend.sh uploads the Lambda code zip to ``_backends/<slug>/`` in the
base stack's bucket, the same bucket the shared CloudFront distribution serves
under ``/<slug>/``. The base-stack bucket policy denies CloudFront that prefix,
and deploy-backend.sh uploads only when the live bucket policy carries that deny.

The script tests run the real deploy-backend.sh with a recording ``aws`` stub on
PATH, so the gate is checked on the path an operator runs.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from yaml_helpers import load_with

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

SKILL = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
)
TEMPLATE = SKILL / "templates" / "base-stack.yaml"
DEPLOY_BACKEND = SKILL / "scripts" / "deploy-backend.sh"
DEPLOY_SITE = SKILL / "scripts" / "deploy.sh"

BUCKET = "kirocrew-deploy-123456789012-us-west-2"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET}"

pytestmark = pytest.mark.skipif(os.name == "nt", reason="runs the POSIX deploy script")


def _rendered_bucket_policy() -> dict:
    """The base-stack OriginBucket policy with its intrinsics resolved for BUCKET."""
    values = {
        "${OriginBucket.Arn}": BUCKET_ARN,
        "${AWS::AccountId}": "123456789012",
        "${Distribution.Id}": "EDIST",
    }

    class Loader(yaml.SafeLoader):
        pass

    def _sub(loader, node):
        text = loader.construct_scalar(node)
        for key, val in values.items():
            text = text.replace(key, val)
        return text

    def _getatt(loader, node):
        return BUCKET_ARN if loader.construct_scalar(node) == "OriginBucket.Arn" else "x"

    def _other(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    Loader.add_constructor("!Sub", _sub)
    Loader.add_constructor("!GetAtt", _getatt)
    Loader.add_multi_constructor("!", _other)
    doc = load_with(Loader, TEMPLATE.read_text(encoding="utf-8"))
    return doc["Resources"]["BucketPolicy"]["Properties"]["PolicyDocument"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _cloudfront_can_read(policy: dict, key: str) -> bool:
    arn = f"{BUCKET_ARN}/{key}"

    def matches(st):
        principal = st.get("Principal")
        svc = principal.get("Service") if isinstance(principal, dict) else principal
        return (
            any(p == "cloudfront.amazonaws.com" or p == "*" for p in _as_list(svc))
            and {"s3:GetObject", "s3:*"} & set(_as_list(st["Action"]))
            and any(fnmatch.fnmatchcase(arn, r) for r in _as_list(st["Resource"]))
            and not st.get("Condition", {}).get("Bool")  # the TLS-only deny
        )

    stmts = policy["Statement"]
    if any(st["Effect"] == "Deny" and matches(st) for st in stmts):
        return False
    return any(st["Effect"] == "Allow" and matches(st) for st in stmts)


def test_base_stack_policy_serves_sites_but_not_code_archives():
    policy = _rendered_bucket_policy()
    assert _cloudfront_can_read(policy, "my-app/index.html")
    assert _cloudfront_can_read(policy, "my-app/assets/app.js")
    assert not _cloudfront_can_read(policy, "_backends/my-app/20261126120000.zip")
    assert not _cloudfront_can_read(policy, "_reaper/reaper-20261126120000.zip")
    assert not _cloudfront_can_read(policy, "my-app/.kirocrew-deploy.json")


_AWS_STUB = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
while args and args[0] in ("--region", "--profile"):
    args = args[2:]
with open(os.environ["AWS_STUB_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
cmd = args[:2]
if cmd == ["cloudformation", "describe-stacks"] and "kirocrew-deploy-base" in args:
    print(json.dumps([
        {"OutputKey": "BucketName", "OutputValue": os.environ["AWS_STUB_BUCKET"]},
        {"OutputKey": "DistributionId", "OutputValue": "EDIST"},
        {"OutputKey": "DistributionDomainName", "OutputValue": "d111.cloudfront.net"},
    ]))
elif cmd == ["sts", "get-caller-identity"]:
    print("123456789012")
elif cmd == ["iam", "get-policy"]:
    print("{}")
elif cmd == ["s3api", "get-bucket-policy"]:
    path = os.environ.get("AWS_STUB_POLICY", "")
    if not path:
        sys.stderr.write("NoSuchBucketPolicy\n")
        sys.exit(254)
    print(open(path).read())
elif cmd == ["s3", "cp"] or cmd == ["s3", "sync"]:
    pass
elif cmd == ["cloudformation", "deploy"] and os.environ.get("AWS_STUB_STACK_DEPLOY_OK"):
    pass
elif cmd == ["cloudfront", "create-invalidation"]:
    print("IARCHIVES" if "--query" in args else "{}")
elif cmd == ["cloudfront", "wait"]:
    sys.exit(255 if os.environ.get("AWS_STUB_WAIT_FAILS") else 0)
else:
    # deploy-backend.sh stops here at its stack deploy: those tests cover the
    # upload step only.
    sys.exit(3)
"""


def _stub_env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(_AWS_STUB.replace("/usr/bin/env python3", sys.executable, 1), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "AWS_STUB_LOG": str(tmp_path / "aws.log"),
        "AWS_STUB_BUCKET": BUCKET,
        "TMPDIR": str(tmp_path),
    }
    env.pop("AWS_PROFILE", None)
    return env


def _logged_calls(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "aws.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _run_deploy_backend(tmp_path: Path, policy: dict | None) -> list[list[str]]:
    env = _stub_env(tmp_path)
    src = tmp_path / "api"
    src.mkdir()
    (src / "index.py").write_text("def handler(event, context):\n    return {}\n", encoding="utf-8")
    if policy is not None:
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")
        env["AWS_STUB_POLICY"] = str(policy_file)
    subprocess.run(
        [
            shutil.which("bash") or "bash",
            str(DEPLOY_BACKEND),
            str(src),
            "--slug",
            "my-app",
            "--region",
            "us-west-2",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )
    return _logged_calls(tmp_path)


def _uploads(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["s3", "cp"]]


def test_deploy_backend_uploads_when_the_policy_keeps_archives_private(tmp_path):
    calls = _run_deploy_backend(tmp_path, _rendered_bucket_policy())
    ups = _uploads(calls)
    assert len(ups) == 1, calls
    assert ups[0][3].startswith(f"s3://{BUCKET}/_backends/my-app/") and ups[0][3].endswith(".zip")


def test_deploy_backend_refuses_a_bucket_whose_policy_serves_archives(tmp_path):
    older = _rendered_bucket_policy()
    older["Statement"] = [
        s for s in older["Statement"] if s.get("Sid") != "DenyCloudFrontCodeArchives"
    ]
    calls = _run_deploy_backend(tmp_path, older)
    assert ["s3api", "get-bucket-policy"] in [c[:2] for c in calls]
    assert _uploads(calls) == []


def test_deploy_backend_refuses_when_the_policy_cannot_be_read(tmp_path):
    calls = _run_deploy_backend(tmp_path, None)
    assert _uploads(calls) == []


def _run_deploy_site(tmp_path: Path, *, wait_fails: bool = False) -> subprocess.CompletedProcess:
    env = _stub_env(tmp_path)
    env["AWS_STUB_STACK_DEPLOY_OK"] = "1"
    if wait_fails:
        env["AWS_STUB_WAIT_FAILS"] = "1"
    site = tmp_path / "public"
    site.mkdir()
    (site / "index.html").write_text("<p>hello</p>\n", encoding="utf-8")
    return subprocess.run(
        [
            shutil.which("bash") or "bash",
            str(DEPLOY_SITE),
            str(site),
            "--slug",
            "my-app",
            "--ttl",
            "0",
            "--region",
            "us-west-2",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def test_deploy_site_purges_cached_code_archives_after_applying_the_template(tmp_path):
    result = _run_deploy_site(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "deployed: https://d111.cloudfront.net/my-app/" in result.stdout
    calls = _logged_calls(tmp_path)
    heads = [c[:2] for c in calls]
    purge = [
        i
        for i, c in enumerate(calls)
        if c[:2] == ["cloudfront", "create-invalidation"] and "/_backends/*" in c
    ]
    assert len(purge) == 1, calls
    assert "/_reaper/*" in calls[purge[0]]
    assert purge[0] > heads.index(["cloudformation", "deploy"])
    waits = [c for c in calls if c[:2] == ["cloudfront", "wait"]]
    assert waits == [
        [
            "cloudfront",
            "wait",
            "invalidation-completed",
            "--distribution-id",
            "EDIST",
            "--id",
            "IARCHIVES",
        ]
    ]


def test_deploy_site_does_not_report_success_before_the_purge_completes(tmp_path):
    result = _run_deploy_site(tmp_path, wait_fails=True)
    assert result.returncode != 0
    assert "deployed:" not in result.stdout
