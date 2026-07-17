"""Tests for deploy_web.engine recall / destroy / list_sites (mocked aws CLI)."""
from __future__ import annotations

import json

import pytest

from kiro_crew.apps.builtins.deploy_web import engine

_SITE_TAGS = [
    {"ResourceARN": "arn:aws:s3:::kirocrew-web-aaaa1111",
     "Tags": [{"Key": "kirocrew:managed", "Value": "true"}, {"Key": "kirocrew:site", "Value": "cr-dash"}]},
    {"ResourceARN": "arn:aws:cloudfront::123456789012:distribution/DISTAAA",
     "Tags": [{"Key": "kirocrew:managed", "Value": "true"}, {"Key": "kirocrew:site", "Value": "cr-dash"}]},
]


def _tag_resp(items):
    return 0, json.dumps({"ResourceTagMappingList": items}), ""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(engine, "_sleep", lambda *_a, **_k: None)


def test_recall_empties_bucket_and_invalidates(monkeypatch):
    calls = []

    def run(args, profile, timeout=30):  # noqa: ANN001
        calls.append(list(args))
        if args[0] == "resourcegroupstaggingapi":
            return _tag_resp(_SITE_TAGS)
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    result = engine.recall("cr-dash", profile="p")
    assert result["recalled"] is True
    assert result["bucket"] == "kirocrew-web-aaaa1111"
    acts = [f"{c[0]} {c[1]}" for c in calls if len(c) > 1]
    assert "s3 rm" in acts
    assert "cloudfront create-invalidation" in acts
    # Recall must NOT touch the distribution lifecycle.
    assert "cloudfront update-distribution" not in acts
    assert "cloudfront delete-distribution" not in acts


def test_recall_missing_site_raises(monkeypatch):
    monkeypatch.setattr(engine, "run_aws",
                        lambda a, p, timeout=30: _tag_resp([]) if a[0] == "resourcegroupstaggingapi" else (0, "{}", ""))
    with pytest.raises(engine.AWSError):
        engine.recall("nope", profile="p")


def test_destroy_disable_wait_delete_order(monkeypatch):
    calls = []
    poll = {"n": 0}

    def run(args, profile, timeout=30):  # noqa: ANN001
        calls.append(list(args))
        if args[0] == "resourcegroupstaggingapi":
            return _tag_resp(_SITE_TAGS)
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            return 0, json.dumps({"ETag": "ETAG1", "DistributionConfig": {"Enabled": True}}), ""
        if args[:2] == ["cloudfront", "get-distribution"]:
            poll["n"] += 1
            status = "InProgress" if poll["n"] < 2 else "Deployed"
            return 0, json.dumps({"Distribution": {"Status": status, "DomainName": "d.cloudfront.net"}}), ""
        if args[:2] == ["cloudfront", "list-origin-access-controls"]:
            return 0, json.dumps({"OriginAccessControlList": {"Items": []}}), ""
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    result = engine.destroy("cr-dash", profile="p")
    assert result["destroyed"] is True
    acts = [f"{c[0]} {c[1]}" for c in calls if len(c) > 1]
    # disable (update) → wait (get-distribution) → delete-distribution → delete bucket, in order.
    assert acts.index("cloudfront update-distribution") < acts.index("cloudfront delete-distribution")
    assert acts.index("cloudfront delete-distribution") < acts.index("s3api delete-bucket")
    assert "cloudfront get-distribution" in acts  # polled for Deployed
    assert "s3 rm" in acts  # bucket emptied before delete
    assert acts.index("s3 rm") < acts.index("s3api delete-bucket")


def test_destroy_skips_disable_when_already_disabled(monkeypatch):
    calls = []

    def run(args, profile, timeout=30):  # noqa: ANN001
        calls.append(list(args))
        if args[0] == "resourcegroupstaggingapi":
            return _tag_resp(_SITE_TAGS)
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            return 0, json.dumps({"ETag": "E", "DistributionConfig": {"Enabled": False}}), ""
        if args[:2] == ["cloudfront", "get-distribution"]:
            return 0, json.dumps({"Distribution": {"Status": "Deployed", "DomainName": "d"}}), ""
        if args[:2] == ["cloudfront", "list-origin-access-controls"]:
            return 0, json.dumps({"OriginAccessControlList": {"Items": []}}), ""
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    engine.destroy("cr-dash", profile="p")
    acts = [f"{c[0]} {c[1]}" for c in calls if len(c) > 1]
    assert "cloudfront update-distribution" not in acts  # already disabled
    assert "cloudfront delete-distribution" in acts


def test_destroy_deletes_matching_oac(monkeypatch):
    deleted = {}

    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return _tag_resp(_SITE_TAGS)
        if args[:2] == ["cloudfront", "get-distribution-config"]:
            return 0, json.dumps({"ETag": "E", "DistributionConfig": {"Enabled": False}}), ""
        if args[:2] == ["cloudfront", "get-distribution"]:
            return 0, json.dumps({"Distribution": {"Status": "Deployed", "DomainName": "d"}}), ""
        if args[:2] == ["cloudfront", "list-origin-access-controls"]:
            return 0, json.dumps({"OriginAccessControlList": {"Items": [
                {"Id": "OACX", "Name": "deploy-web-cr-dash-abc123"},
                {"Id": "OTHER", "Name": "someone-elses-oac"},
            ]}}), ""
        if args[:2] == ["cloudfront", "get-origin-access-control"]:
            return 0, json.dumps({"ETag": "OACETAG"}), ""
        if args[:2] == ["cloudfront", "delete-origin-access-control"]:
            deleted["id"] = args[args.index("--id") + 1]
            return 0, "", ""
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    engine.destroy("cr-dash", profile="p")
    assert deleted.get("id") == "OACX"  # only the site's own OAC, not OTHER


def test_list_sites_groups_by_tag_with_live_status(monkeypatch):
    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[0] == "resourcegroupstaggingapi":
            return _tag_resp(_SITE_TAGS + [
                {"ResourceARN": "arn:aws:s3:::kirocrew-web-bbbb2222",
                 "Tags": [{"Key": "kirocrew:managed", "Value": "true"},
                          {"Key": "kirocrew:site", "Value": "blog"}]},
            ])
        if args[:2] == ["cloudfront", "get-distribution"]:
            return 0, json.dumps({"Distribution": {
                "Status": "Deployed", "DomainName": "d111.cloudfront.net"}}), ""
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    sites = engine.list_sites(profile="p")
    by_id = {s["site_id"]: s for s in sites}
    assert set(by_id) == {"cr-dash", "blog"}
    assert by_id["cr-dash"]["bucket"] == "kirocrew-web-aaaa1111"
    assert by_id["cr-dash"]["distribution_id"] == "DISTAAA"
    assert by_id["cr-dash"]["status"] == "Deployed"
    assert by_id["cr-dash"]["url"] == "https://d111.cloudfront.net/"
    # site with only a bucket (no distribution yet) still listed, blank status
    assert by_id["blog"]["distribution_id"] == ""
    assert by_id["blog"]["url"] == ""
