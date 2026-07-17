"""Tests for deploy_web.engine — deterministic deploy flow with mocked aws CLI."""
from __future__ import annotations

import json
import re

import pytest

from kiro_crew.apps.builtins.deploy_web import engine


class FakeAWS:
    """Records aws CLI calls and returns canned responses keyed by a matcher.

    Monkeypatched in for engine.run_aws. Each call records the argv (sans the
    leading 'aws'/profile) so tests can assert ordering and content.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.account = "123456789012"

    def __call__(self, args, profile, timeout=30):  # noqa: ANN001
        self.calls.append(list(args))
        sub = args[0]
        if sub == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if sub == "sts":
            return 0, json.dumps({"Account": self.account}), ""
        if sub == "s3api" and args[1] == "create-bucket":
            return 0, "", ""
        if sub == "cloudfront" and args[1] == "create-origin-access-control":
            return 0, json.dumps({"OriginAccessControl": {"Id": "OAC123"}}), ""
        if sub == "cloudfront" and args[1] == "create-distribution-with-tags":
            return 0, json.dumps({"Distribution": {
                "Id": "DIST123",
                "ARN": "arn:aws:cloudfront::123456789012:distribution/DIST123",
                "DomainName": "d111abc.cloudfront.net",
            }}), ""
        if sub == "cloudfront" and args[1] == "get-distribution":
            return 0, json.dumps({"Distribution": {
                "Status": "Deployed", "DomainName": "d111abc.cloudfront.net"}}), ""
        # s3api put-*, s3 sync, cloudfront create-invalidation, etc.
        return 0, "{}", ""

    def actions(self) -> list[str]:
        """Compact action labels in call order, for ordering assertions."""
        labels = []
        for a in self.calls:
            labels.append(f"{a[0]} {a[1]}" if len(a) > 1 else a[0])
        return labels


@pytest.fixture
def fake(monkeypatch):
    f = FakeAWS()
    monkeypatch.setattr(engine, "run_aws", f)
    return f


def test_random_bucket_name_format():
    name = engine.random_bucket_name()
    assert name.startswith("kirocrew-web-")
    suffix = name[len("kirocrew-web-"):]
    assert re.fullmatch(r"[0-9a-f]{12}", suffix), name
    # No account id, opaque
    assert "123456789012" not in name
    # Distinct each call
    assert engine.random_bucket_name() != name


def test_first_deploy_full_flow_and_ordering(fake):
    result = engine.deploy("cr-dashboard", "/tmp/site", profile="p", region="us-west-2")
    assert result["reused"] is False
    assert result["url"] == "https://d111abc.cloudfront.net/"
    assert result["bucket"].startswith("kirocrew-web-")
    assert result["distribution_id"] == "DIST123"

    acts = fake.actions()
    # Ordering gotcha (§4): create-distribution MUST precede put-bucket-policy.
    assert "cloudfront create-distribution-with-tags" in acts
    assert "s3api put-bucket-policy" in acts
    assert acts.index("cloudfront create-distribution-with-tags") < acts.index("s3api put-bucket-policy")
    # OAC created before distribution; sync + invalidate happen after policy.
    assert acts.index("cloudfront create-origin-access-control") < acts.index("cloudfront create-distribution-with-tags")
    assert acts.index("s3api put-bucket-policy") < acts.index("s3 sync")
    assert acts.index("s3 sync") < acts.index("cloudfront create-invalidation")


def test_bucket_policy_pins_distribution_arn(fake):
    engine.deploy("site-x", "/tmp/site", profile="p")
    policy_call = next(c for c in fake.calls if c[0] == "s3api" and c[1] == "put-bucket-policy")
    policy = json.loads(policy_call[policy_call.index("--policy") + 1])
    cond = policy["Statement"][0]["Condition"]["StringEquals"]
    assert cond["AWS:SourceArn"] == "arn:aws:cloudfront::123456789012:distribution/DIST123"


def test_distribution_tagged_at_creation(fake):
    engine.deploy("tagged-site", "/tmp/site", profile="p")
    call = next(c for c in fake.calls if c[1] == "create-distribution-with-tags")
    payload = json.loads(call[call.index("--distribution-config-with-tags") + 1])
    tags = {t["Key"]: t["Value"] for t in payload["Tags"]["Items"]}
    assert tags[engine.TAG_MANAGED] == "true"
    assert tags[engine.TAG_SITE] == "tagged-site"


def test_redeploy_is_idempotent_reuses_infra(monkeypatch):
    f = FakeAWS()

    def fake_run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": [
                {"ResourceARN": "arn:aws:s3:::kirocrew-web-deadbeef0001"},
                {"ResourceARN": "arn:aws:cloudfront::123456789012:distribution/DISTOLD"},
            ]}), ""
        return f(args, profile, timeout)

    monkeypatch.setattr(engine, "run_aws", fake_run)
    result = engine.deploy("existing", "/tmp/site", profile="p")
    assert result["reused"] is True
    assert result["bucket"] == "kirocrew-web-deadbeef0001"
    assert result["distribution_id"] == "DISTOLD"
    acts = f.actions()
    # Re-deploy must NOT create new infra — only sync + invalidate (+ status read).
    assert "s3api create-bucket" not in acts
    assert "cloudfront create-distribution-with-tags" not in acts
    assert "s3 sync" in acts and "cloudfront create-invalidation" in acts


def test_access_denied_maps_to_statement(monkeypatch):
    def denied(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if args[0] == "s3api" and args[1] == "create-bucket":
            return 255, "", "An error occurred (AccessDenied) ... s3:CreateBucket denied"
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", denied)
    with pytest.raises(engine.AWSError) as ei:
        engine.deploy("denied-site", "/tmp/site", profile="p")
    assert ei.value.missing_statement == "S3BucketLevel"


def test_map_access_denied_direct():
    assert engine.map_access_denied("boom AccessDenied cloudfront:CreateInvalidation") == "CloudFrontManageTagged"
    assert engine.map_access_denied("all good") is None


def test_bucket_name_409_retry(monkeypatch):
    f = FakeAWS()
    seen = {"n": 0}

    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return 0, json.dumps({"ResourceTagMappingList": []}), ""
        if args[0] == "s3api" and args[1] == "create-bucket":
            seen["n"] += 1
            if seen["n"] == 1:
                return 1, "", "An error occurred (BucketAlreadyExists)"
            return 0, "", ""
        return f(args, profile, timeout)

    monkeypatch.setattr(engine, "run_aws", run)
    result = engine.deploy("retry-site", "/tmp/site", profile="p")
    assert result["reused"] is False
    assert seen["n"] == 2  # retried once after the 409


def test_partial_deploy_recovery_reuses_bucket(fake, monkeypatch):
    """Bucket tagged but distribution missing (partial prior deploy): reuse the
    existing bucket instead of allocating a new one that would orphan it."""
    monkeypatch.setattr(
        engine, "find_site_by_tag",
        lambda sid, p, region=engine.DEFAULT_REGION: {
            "bucket": "kirocrew-web-deadbeef0000", "distribution_id": "", "distribution_arn": ""},
    )
    result = engine.deploy("cr-dash", "/tmp/site", profile="p", region="us-west-2")
    # Reused the tagged bucket; created the missing distribution (not a full reuse).
    assert result["bucket"] == "kirocrew-web-deadbeef0000"
    assert result["reused"] is False
    # No new bucket was allocated — the existing one was not orphaned.
    assert "s3api create-bucket" not in fake.actions()
    # Distribution creation + sync still ran.
    assert "cloudfront create-distribution-with-tags" in fake.actions()
