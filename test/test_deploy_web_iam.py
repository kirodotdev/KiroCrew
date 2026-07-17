"""Tests for deploy_web IAM policy generator + read-only reachability + skill wiring."""
from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.apps.builtins.deploy_web import engine
from kiro_crew.apps.builtins.deploy_web import iam as iam_mod

_PKG = Path(iam_mod.__file__).parent


def test_policy_is_valid_json_with_expected_sids():
    doc = json.loads(iam_mod.policy_json())
    assert doc["Version"] == "2012-10-17"
    sids = {s["Sid"] for s in doc["Statement"]}
    assert {"S3BucketLevel", "S3ObjectLevel", "CloudFrontCreateList",
            "CloudFrontManageTagged", "DiscoveryAndIdentity"} <= sids


def test_policy_scoping_levers_present():
    doc = json.loads(iam_mod.policy_json())
    s3 = next(s for s in doc["Statement"] if s["Sid"] == "S3BucketLevel")
    assert s3["Resource"] == "arn:aws:s3:::kirocrew-web-*"  # name-prefix scope
    cf = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontManageTagged")
    assert cf["Condition"]["StringEquals"]["aws:ResourceTag/kirocrew:managed"] == "true"  # tag scope


def test_policy_no_iam_or_billing_actions():
    """§6.1 / Q6: never any IAM-write or billing actions in the generated policy."""
    text = iam_mod.policy_json(include_custom_domain=True)
    for forbidden in ("iam:", "ce:", "cloudwatch:", "organizations:"):
        assert forbidden not in text, forbidden


def test_custom_domain_addendum_optional():
    base = json.loads(iam_mod.policy_json())
    full = json.loads(iam_mod.policy_json(include_custom_domain=True))
    base_sids = {s["Sid"] for s in base["Statement"]}
    full_sids = {s["Sid"] for s in full["Statement"]}
    assert "AcmForCloudFront" not in base_sids
    assert {"AcmForCloudFront", "Route53Alias"} <= full_sids


def test_reachability_ok(monkeypatch):
    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[:2] == ["sts", "get-caller-identity"]:
            return 0, json.dumps({"Account": "123456789012"}), ""
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    r = iam_mod.reachability_check("p")
    assert r["reachable"] is True
    assert r["account"] == "123456789012"
    assert r["s3_reachable"] is True and r["cloudfront_reachable"] is True
    assert "not fully verified" in r["note"]


def test_reachability_bad_profile(monkeypatch):
    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[:2] == ["sts", "get-caller-identity"]:
            return 255, "", "Unable to locate credentials / token expired"
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    r = iam_mod.reachability_check("p")
    assert r["reachable"] is False
    assert "sso login" in r["note"]


def test_reachability_partial(monkeypatch):
    # sts ok but cloudfront denied -> reachable True, cloudfront_reachable False
    def run(args, profile, timeout=30):  # noqa: ANN001
        if args[:2] == ["sts", "get-caller-identity"]:
            return 0, json.dumps({"Account": "1"}), ""
        if args[:2] == ["cloudfront", "list-distributions"]:
            return 254, "", "AccessDenied"
        return 0, "{}", ""

    monkeypatch.setattr(engine, "run_aws", run)
    r = iam_mod.reachability_check("p")
    assert r["reachable"] is True
    assert r["cloudfront_reachable"] is False


def test_skill_file_ships_with_app():
    skill = _PKG / "skills" / "deploy-web" / "SKILL.md"
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert "deploy-web" in body
    # Guardrails present in the skill.
    low = body.lower()
    assert "create/attach/modify iam" in low
    assert "world-readable" in low


def test_manifest_declares_skill():
    manifest = json.loads((_PKG / "app.json").read_text(encoding="utf-8"))
    assert "skills/deploy-web" in manifest.get("skills", [])


def test_manifest_opt_in_and_aws_dep():
    manifest = json.loads((_PKG / "app.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "deploy-web"
    assert manifest.get("defaultEnabled") is False  # opt-in / disabled by default
    assert "aws" in manifest.get("dependencies", {}).get("commands", [])
