"""AgentCore instance Policy.json postures and successor boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.cloud import iam

_WORKLOAD_DIR = "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default"
_WORKLOAD_ID = (
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew"
)
_WORKLOAD_ID_WILDCARD = (
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew-*"
)
_WORKLOAD_RESOURCES = [_WORKLOAD_DIR, _WORKLOAD_ID, _WORKLOAD_ID_WILDCARD]
_GATEWAY = "arn:aws:bedrock-agentcore:*:*:gateway/kirocrew-*"

# Byte-stable original boundary: SSM-core + source-bucket read, no AgentCore.
_ORIGINAL_BOUNDARY_SIDS = frozenset({"SsmCore", "SourceBucketRead"})


def _statement_by_sid(doc: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in doc["Statement"] if s["Sid"] == sid)


def _actions(st: dict[str, Any]) -> set[str]:
    raw = st["Action"]
    return {raw} if isinstance(raw, str) else set(raw)


def _resources(st: dict[str, Any]) -> list[str]:
    raw = st["Resource"]
    return [raw] if isinstance(raw, str) else list(raw)


def test_launcher_policy_json_has_no_invoke_gateway() -> None:
    text = iam.policy_json()
    assert "InvokeGateway" not in text
    assert "GetWorkloadAccessToken" not in text
    assert "GetGateway" not in text
    assert "ListGatewayTargets" not in text
    assert "SynchronizeGatewayTargets" not in text


def test_launcher_policy_can_create_agentcore_identity() -> None:
    st = _statement_by_sid(iam.policy_document(), "AgentCoreWorkloadIdentityControlPlane")
    assert st["Effect"] == "Allow"
    assert "bedrock-agentcore:CreateWorkloadIdentity" in _actions(st)
    assert "bedrock-agentcore:DeleteWorkloadIdentity" in _actions(st)
    assert "InvokeGateway" not in "".join(_actions(st))
    assert "GetWorkloadAccessToken" not in "".join(_actions(st))
    assert _resources(st) == _WORKLOAD_RESOURCES


def test_agentcore_workload_name_is_per_tag() -> None:
    assert iam.agentcore_workload_name("kc-abc123", "workload") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "login") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "none") == ""
    assert iam.normalize_agentcore_posture("") == "none"
    assert iam.normalize_agentcore_posture("WORKLOAD") == "workload"


def test_launcher_create_role_accepts_either_boundary() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamCreateRoleWithBoundary")
    cond = st["Condition"]["ArnLike"]["iam:PermissionsBoundary"]
    values = [cond] if isinstance(cond, str) else list(cond)
    assert f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}" in values
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" in values


def test_launcher_create_once_covers_successor_name() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamInstanceBoundaryCreateOnce")
    assert set(st["Action"]) == {
        "iam:CreatePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
    }
    resources = _resources(st)
    assert f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}" in resources
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" in resources
    assert not any(r.endswith("*") for r in resources)


def test_workload_instance_document_denies_for_jwt() -> None:
    doc = iam.agentcore_instance_policy_document("workload")
    identity = _statement_by_sid(doc, "AgentCoreIdentity")
    assert identity["Effect"] == "Allow"
    assert _actions(identity) == {
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    }
    assert _resources(identity) == _WORKLOAD_RESOURCES

    gateway = _statement_by_sid(doc, "AgentCoreGateway")
    assert gateway["Effect"] == "Allow"
    assert _actions(gateway) == {"bedrock-agentcore:InvokeGateway"}
    assert _resources(gateway) == [_GATEWAY]

    deny = _statement_by_sid(doc, "DenyJwtPathOnWorkloadPosture")
    assert deny["Effect"] == "Deny"
    assert _actions(deny) == {"bedrock-agentcore:GetWorkloadAccessTokenForJWT"}
    assert _resources(deny) == ["*"]

    inspect = _statement_by_sid(doc, "AgentCoreGatewayInspect")
    assert inspect["Effect"] == "Allow"
    assert _actions(inspect) == {
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:ListGatewayTargets",
        "bedrock-agentcore:GetGatewayTarget",
        "bedrock-agentcore:SynchronizeGatewayTargets",
    }
    assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]

    for st in doc["Statement"]:
        if st["Effect"] == "Allow":
            assert "*" not in _resources(st)


def test_login_instance_document_denies_userid_and_invoke() -> None:
    doc = iam.agentcore_instance_policy_document("login")
    identity = _statement_by_sid(doc, "AgentCoreIdentityForJwt")
    assert identity["Effect"] == "Allow"
    assert _actions(identity) == {"bedrock-agentcore:GetWorkloadAccessTokenForJWT"}
    assert _resources(identity) == _WORKLOAD_RESOURCES

    deny = _statement_by_sid(doc, "DenyUserIdAndIamGateway")
    assert deny["Effect"] == "Deny"
    assert _actions(deny) == {
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:InvokeGateway",
    }
    assert _resources(deny) == ["*"]

    inspect = _statement_by_sid(doc, "AgentCoreGatewayInspect")
    assert inspect["Effect"] == "Allow"
    assert "bedrock-agentcore:GetGateway" in _actions(inspect)
    assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]

    dumped = json.dumps(doc)
    assert "GetWorkloadAccessTokenForUserId" in dumped
    assert '"Effect": "Allow"' not in dumped.split("GetWorkloadAccessTokenForUserId")[0][-80:]
    for st in doc["Statement"]:
        if st["Effect"] == "Allow":
            assert "InvokeGateway" not in _actions(st)
            assert "GetWorkloadAccessTokenForUserId" not in _actions(st)
            assert "*" not in _resources(st)


def test_original_boundary_document_unchanged() -> None:
    doc = iam.boundary_policy_document("123456789012")
    dumped = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    assert "bedrock-agentcore" not in dumped
    assert "InvokeGateway" not in dumped
    assert "GetWorkloadAccessToken" not in dumped
    sids = {s["Sid"] for s in doc["Statement"]}
    assert sids == _ORIGINAL_BOUNDARY_SIDS
    ssm = _statement_by_sid(doc, "SsmCore")
    assert "ssm:UpdateInstanceInformation" in ssm["Action"]
    s3 = _statement_by_sid(doc, "SourceBucketRead")
    assert s3["Action"] == ["s3:GetObject"]
    assert s3["Resource"] == "arn:aws:s3:::kirocrew-src-123456789012-*/*"
    assert json.loads(iam.boundary_policy_json("123456789012")) == doc


def test_successor_boundary_is_union_ceiling() -> None:
    for posture in ("workload", "login"):
        doc = iam.agentcore_boundary_policy_document("123456789012", posture)
        sids = {s["Sid"] for s in doc["Statement"]}
        assert "SsmCore" in sids
        assert "SourceBucketRead" in sids
        dumped = json.dumps(doc)
        for action in (
            "bedrock-agentcore:GetWorkloadAccessToken",
            "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            "bedrock-agentcore:InvokeGateway",
            "bedrock-agentcore:GetGateway",
            "bedrock-agentcore:ListGatewayTargets",
            "bedrock-agentcore:SynchronizeGatewayTargets",
        ):
            assert action in dumped
        inspect = _statement_by_sid(doc, "AgentCoreInspectCeiling")
        assert _resources(inspect) == ["arn:aws:bedrock-agentcore:*:*:gateway/*"]
        s3 = _statement_by_sid(doc, "SourceBucketRead")
        assert s3["Resource"] == "arn:aws:s3:::kirocrew-src-123456789012-*/*"


def test_successor_boundary_name_is_distinct() -> None:
    assert iam.AGENTCORE_BOUNDARY_NAME == "kirocrew-ec2-boundary-agentcore"
    assert iam.BOUNDARY_NAME == "kirocrew-ec2-boundary"
    assert iam.AGENTCORE_BOUNDARY_NAME != iam.BOUNDARY_NAME


def test_template_allowed_pattern_lists_both_boundary_names() -> None:
    from kiro_crew.cloud import ec2

    text = ec2.load_template()
    assert "kirocrew-ec2-boundary" in text
    assert "kirocrew-ec2-boundary-agentcore" in text
    assert "kirocrew-ec2-boundary(-agentcore)?" in text or (
        "kirocrew-ec2-boundary" in text and "agentcore" in text
    )


def test_template_instance_policies_include_inspect() -> None:
    from kiro_crew.cloud import ec2

    text = ec2.load_template()
    assert text.count("AgentCoreGatewayInspect") >= 2
    assert "bedrock-agentcore:GetGateway" in text
    assert "bedrock-agentcore:SynchronizeGatewayTargets" in text
    assert "gateway/*" in text


@pytest.mark.asyncio
async def test_iam_policy_api_returns_labeled_instance_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.cloud import launch_job as lj
    from kiro_crew.dashboard import handlers_cloud as hc

    monkeypatch.setattr(hc.sys, "platform", "linux")
    state = SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_store=lj.LaunchJobStore(root=tmp_path / "launch-jobs"),
    )
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "GET",
        "/api/cloud/iam-policy?instance=1&posture=workload",
        app=app,
    )
    req["user"] = "owner-1"
    req["app"] = ""
    resp = await hc.api_cloud_iam_policy(req)
    assert resp.status == 200
    body = json.loads(resp.body.decode("utf-8"))
    assert "policy" in body
    assert "InvokeGateway" not in body["policy"]
    instance = json.loads(body["instance_policy"])
    assert body["instance_posture"] == "workload"
    assert any("InvokeGateway" in json.dumps(s) for s in instance["Statement"])


def test_cli_iam_policy_instance_flag(capsys: pytest.CaptureFixture[str]) -> None:
    from kiro_crew import cli_cloud

    ns = type("NS", (), {"cloud_action": "iam-policy", "instance": True, "posture": "login"})()
    assert cli_cloud.handle_cloud(ns) == 0
    out = capsys.readouterr().out
    assert "GetWorkloadAccessTokenForJWT" in out
    assert "DenyUserIdAndIamGateway" in out


def test_cli_iam_policy_instance_requires_posture(capsys: pytest.CaptureFixture[str]) -> None:
    """``--instance`` without ``--posture`` must not emit the privileged sibling."""
    from kiro_crew import cli_cloud

    ns = type("NS", (), {"cloud_action": "iam-policy", "instance": True, "posture": None})()
    assert cli_cloud.handle_cloud(ns) != 0
    captured = capsys.readouterr()
    assert "InvokeGateway" not in captured.out
    assert "InvokeGateway" not in captured.err
