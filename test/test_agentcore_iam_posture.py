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
_WORKLOAD_RESOURCES = [_WORKLOAD_DIR, _WORKLOAD_ID]

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


_WORKLOAD_TAGGED = (
    "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew-*"
)


def test_workload_token_grants_omit_identity_wildcard() -> None:
    """A GRANT never carries the tagged wildcard: two tagged identities plus it
    would let one crew mint a sibling bearer. The launcher's token verbs are
    absent altogether, so the launcher document carries no token grant either."""
    for posture in ("workload", "login"):
        doc = iam.agentcore_instance_policy_document(posture)
        for st in doc["Statement"]:
            if st["Effect"] == "Allow" and any("WorkloadAccessToken" in a for a in _actions(st)):
                assert _WORKLOAD_TAGGED not in _resources(st)
    launcher = json.dumps(iam.policy_document())
    assert "GetWorkloadAccessToken" not in launcher


def test_ceiling_and_control_plane_admit_the_identity_each_launch_creates() -> None:
    """``agentcore_workload_name`` yields ``kirocrew-<tag>``; a ceiling or a
    control-plane statement that admitted only ``kirocrew`` would deny both the
    CloudFormation create and every token vend of a provisioned crew."""
    tagged = iam.agentcore_workload_name("kc-8ad35d", "workload")
    assert tagged == "kirocrew-kc-8ad35d"
    ceiling = _statement_by_sid(
        iam.agentcore_boundary_policy_document("123456789012"), "AgentCoreUnionCeiling"
    )
    assert _WORKLOAD_TAGGED in _resources(ceiling)
    assert _WORKLOAD_ID in _resources(ceiling)
    control = _statement_by_sid(iam.policy_document(), "AgentCoreWorkloadIdentityControlPlane")
    assert _WORKLOAD_TAGGED in _resources(control)


def test_launcher_policy_can_create_agentcore_identity() -> None:
    st = _statement_by_sid(iam.policy_document(), "AgentCoreWorkloadIdentityControlPlane")
    assert st["Effect"] == "Allow"
    assert "bedrock-agentcore:CreateWorkloadIdentity" in _actions(st)
    assert "bedrock-agentcore:DeleteWorkloadIdentity" in _actions(st)
    assert "InvokeGateway" not in "".join(_actions(st))
    assert "GetWorkloadAccessToken" not in "".join(_actions(st))
    assert _resources(st) == [*_WORKLOAD_RESOURCES, _WORKLOAD_TAGGED]


def test_launcher_cannot_mutate_pass_or_retag_an_agentcore_bounded_role() -> None:
    """The successor ceiling admits every ``kirocrew-*`` identity, so the one path a
    leaked launcher credential has to a sibling token is rewriting an EXISTING
    successor-bounded role's grant and passing it to a fresh instance. The launcher
    document denies exactly that, keyed on the boundary-class tag the template
    applies at CreateRole -- and denies (un)tagging so the class cannot be flipped."""
    st = _statement_by_sid(iam.policy_document(), "DenyLauncherOnAgentCoreBoundedRoles")
    assert st["Effect"] == "Deny"
    for action in (
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
    ):
        assert action in _actions(st)
    assert st["Resource"] == f"arn:aws:iam::*:role/{iam.ROLE_NAME_PREFIX}*"
    assert st["Condition"] == {
        "StringEquals": {f"aws:ResourceTag/{iam.BOUNDARY_TAG_KEY}": iam.BOUNDARY_TAG_AGENTCORE}
    }


def test_template_tags_instance_role_with_its_boundary_class() -> None:
    template = _load_cfn_template()
    tags = {
        t["Key"]: t["Value"] for t in template["Resources"]["InstanceRole"]["Properties"]["Tags"]
    }
    assert tags["kirocrew:managed"] == "true"
    # ``!If [IsAgentCoreBoundary, agentcore, base]`` collapses to its payload
    # list. The class is read off the BOUNDARY the role is created under, not
    # off AgentCorePosture: a successor-bounded role launched with posture
    # `none` must still be tagged `agentcore`, or the launcher deny keyed on
    # that tag would not cover it.
    assert tags[iam.BOUNDARY_TAG_KEY] == [
        "IsAgentCoreBoundary",
        iam.BOUNDARY_TAG_AGENTCORE,
        iam.BOUNDARY_TAG_BASE,
    ]
    cond = template["Conditions"]["IsAgentCoreBoundary"]
    # ``!Equals [!Ref PermissionsBoundaryArn, !Sub "...-agentcore"]``
    assert cond == [
        "PermissionsBoundaryArn",
        f"arn:aws:iam::${{AWS::AccountId}}:policy/{iam.AGENTCORE_BOUNDARY_NAME}",
    ]


def test_agentcore_workload_name_is_per_tag() -> None:
    assert iam.agentcore_workload_name("kc-abc123", "workload") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "login") == "kirocrew-kc-abc123"
    assert iam.agentcore_workload_name("kc-abc123", "none") == ""
    assert iam.normalize_agentcore_posture("") == "none"
    assert iam.normalize_agentcore_posture("WORKLOAD") == "workload"


def test_launcher_create_role_omits_successor_boundary() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamCreateRoleWithBoundary")
    cond = st["Condition"]["ArnLike"]["iam:PermissionsBoundary"]
    values = [cond] if isinstance(cond, str) else list(cond)
    assert values == [f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}"]
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" not in values


def test_agentcore_deploy_create_role_uses_successor_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentCore-posture CreateRole passes the admin-pre-created successor."""
    import kiro_crew.cloud.source as source_mod
    from kiro_crew.cloud import aws, ec2, sizes

    seen: dict[str, int] = {"require": 0, "ensure": 0}

    def _require(profile: str, region: str) -> str:
        seen["require"] += 1
        return f"arn:aws:iam::1:policy/{iam.AGENTCORE_BOUNDARY_NAME}"

    def _ensure(profile: str, region: str, *, name: str | None = None) -> str:
        seen["ensure"] += 1
        return f"arn:aws:iam::1:policy/{iam.BOUNDARY_NAME}"

    monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
    monkeypatch.setattr(source_mod, "require_agentcore_boundary", _require)
    monkeypatch.setattr(source_mod, "ensure_instance_boundary", _ensure)
    monkeypatch.setattr(ec2, "discover_network", lambda *a, **k: ("vpc-1", "subnet-1", "igw"))
    monkeypatch.setattr(aws, "run_aws", lambda *a, **k: (0, "ok", ""))
    monkeypatch.setattr(
        ec2,
        "describe",
        lambda *a, **k: {"instance_id": "i-1", "stack_status": "CREATE_COMPLETE"},
    )
    result = ec2.deploy(
        tag="t1",
        tier=sizes.default_tier(),
        profile="dev",
        region="us-east-1",
        ship_source=False,
        agentcore_posture="workload",
    )
    assert seen["require"] == 1
    assert seen["ensure"] == 0
    assert f"arn:aws:iam::1:policy/{iam.AGENTCORE_BOUNDARY_NAME}" in " ".join(result.argv)


def test_agentcore_deploy_fails_closed_when_successor_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing successor must abort before CloudFormation deploy."""
    import kiro_crew.cloud.source as source_mod
    from kiro_crew.cloud import aws, ec2, sizes

    def _require(profile: str, region: str) -> str:
        raise aws.AWSError(
            "successor missing",
            action="iam:GetPolicy",
        )

    monkeypatch.setattr(ec2, "find_stack", lambda *a, **k: None)
    monkeypatch.setattr(source_mod, "require_agentcore_boundary", _require)
    monkeypatch.setattr(
        source_mod,
        "ensure_instance_boundary",
        lambda *a, **k: pytest.fail("must not CreatePolicy the original on AgentCore launch"),
    )
    ran: list[str] = []
    monkeypatch.setattr(aws, "run_aws", lambda *a, **k: ran.append("deploy") or (0, "ok", ""))
    with pytest.raises(aws.AWSError, match="successor missing"):
        ec2.deploy(
            tag="t1",
            tier=sizes.default_tier(),
            profile="dev",
            region="us-east-1",
            ship_source=False,
            agentcore_posture="login",
        )
    assert ran == []


def test_launcher_create_once_omits_successor_name() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamInstanceBoundaryCreateOnce")
    assert set(st["Action"]) == {
        "iam:CreatePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
    }
    resources = _resources(st)
    assert resources == [f"arn:aws:iam::*:policy/{iam.BOUNDARY_NAME}"]
    assert f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}" not in resources
    assert not any(r.endswith("*") for r in resources)


def test_launcher_reads_successor_boundary_without_create() -> None:
    st = _statement_by_sid(iam.policy_document(), "IamAgentCoreBoundaryRead")
    assert set(st["Action"]) == {"iam:GetPolicy", "iam:GetPolicyVersion"}
    assert "iam:CreatePolicy" not in st["Action"]
    assert _resources(st) == [f"arn:aws:iam::*:policy/{iam.AGENTCORE_BOUNDARY_NAME}"]


def test_workload_instance_document_denies_for_jwt() -> None:
    doc = iam.agentcore_instance_policy_document("workload")
    identity = _statement_by_sid(doc, "AgentCoreIdentity")
    assert identity["Effect"] == "Allow"
    assert _actions(identity) == {
        "bedrock-agentcore:GetWorkloadAccessToken",
    }
    assert _resources(identity) == _WORKLOAD_RESOURCES

    assert all(s["Sid"] != "AgentCoreGateway" for s in doc["Statement"])
    for st in doc["Statement"]:
        if st["Effect"] == "Allow":
            assert "InvokeGateway" not in _actions(st)

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
    # One document serves both postures: there is no posture knob to vary.
    doc = iam.agentcore_boundary_policy_document("123456789012")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "SsmCore" in sids
    assert "SourceBucketRead" in sids
    dumped = json.dumps(doc)
    for action in (
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:ListGatewayTargets",
    ):
        assert action in dumped
    assert "InvokeGateway" not in dumped
    assert "GetWorkloadAccessTokenForUserId" not in dumped
    assert "SynchronizeGatewayTargets" not in dumped
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
    assert "bedrock-agentcore:GetGatewayTarget" in text
    assert "SynchronizeGatewayTargets" not in text
    assert "gateway/*" in text
    assert "Action: [bedrock-agentcore:InvokeGateway]" not in text


def _load_cfn_template() -> dict:
    """The EC2 template as a dict; CFN intrinsics collapse to their scalar/seq payload."""
    import yaml

    from kiro_crew.cloud import ec2

    class _Cfn(yaml.SafeLoader):
        pass

    def _construct(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _Cfn.add_multi_constructor("!", _construct)
    from yaml_helpers import load_with

    return load_with(_Cfn, ec2.load_template())


def _normalized_statements(doc: dict) -> list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
    """(Sid, Effect, sorted actions, sorted resources) with account/region wildcarded.

    The template scopes ARNs to ``${AWS::Region}``/``${AWS::AccountId}`` and names
    the per-launch identity by ``!GetAtt``; the Python fragment prints ``*`` for
    both and the bare ``kirocrew`` identity. Both spell the same grant, so the
    comparison folds those two encodings onto each other and nothing else.
    """
    workload_id = "arn:aws:bedrock-agentcore:*:*:workload-identity-directory/default/workload-identity/kirocrew"

    def _res(value: object) -> str:
        text = str(value)
        if text == "CrewWorkloadIdentity.WorkloadIdentityArn":
            return workload_id
        return text.replace("${AWS::Region}", "*").replace("${AWS::AccountId}", "*")

    out = []
    for st in doc["Statement"]:
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        out.append(
            (
                st["Sid"],
                st["Effect"],
                tuple(sorted(actions)),
                tuple(sorted(_res(r) for r in resources)),
            )
        )
    return sorted(out)


@pytest.mark.parametrize(
    ("posture", "resource"),
    [("workload", "AgentCoreWorkloadInstancePolicy"), ("login", "AgentCoreLoginInstancePolicy")],
)
def test_template_inline_posture_policy_matches_python_fragment(
    posture: str, resource: str
) -> None:
    """The two authoritative copies of each posture grant say the same thing.

    A CloudFormation-launched crew gets the template's inline policy; a pasted
    fleet gets ``iam.agentcore_instance_policy_document``. Per posture, every
    statement (Sid, Effect, actions, resources, Deny SIDs) must be equal once
    the template's account/region scoping and ``!GetAtt`` identity are folded
    onto the fragment's ``*`` / ``kirocrew`` spelling, so the two fleets cannot
    silently drift into different security postures.
    """
    template = _load_cfn_template()
    inline = template["Resources"][resource]["Properties"]["PolicyDocument"]
    fragment = iam.agentcore_instance_policy_document(posture)
    assert _normalized_statements(inline) == _normalized_statements(fragment)


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
    for st in instance["Statement"]:
        if st["Effect"] == "Allow":
            assert "InvokeGateway" not in json.dumps(st)


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


def test_iam_boundary_agentcore_selector_passes_successor_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import cli_cloud

    seen: dict[str, Any] = {}

    def _ensure(profile: str, region: str, *, name: str | None = None) -> str:
        seen["name"] = name
        return f"arn:aws:iam::1:policy/{iam.AGENTCORE_BOUNDARY_NAME}"

    monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-east-1"))
    monkeypatch.setattr("kiro_crew.cloud.source.ensure_instance_boundary", _ensure)
    ns = type("NS", (), {"agentcore": True, "profile": "dev", "region": "us-east-1"})()
    assert cli_cloud._cloud_iam_boundary(ns) == 0
    assert seen["name"] == iam.AGENTCORE_BOUNDARY_NAME


def test_iam_boundary_default_creates_original_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import cli_cloud

    seen: dict[str, Any] = {}

    def _ensure(profile: str, region: str, *, name: str | None = None) -> str:
        seen["name"] = name
        return f"arn:aws:iam::1:policy/{iam.BOUNDARY_NAME}"

    monkeypatch.setattr(cli_cloud, "_resolve", lambda _args: ("dev", "us-east-1"))
    monkeypatch.setattr("kiro_crew.cloud.source.ensure_instance_boundary", _ensure)
    ns = type("NS", (), {"agentcore": False, "profile": "dev", "region": "us-east-1"})()
    assert cli_cloud._cloud_iam_boundary(ns) == 0
    assert seen["name"] is None


def _load_cfn_text(text: str) -> dict:
    import yaml

    class _Cfn(yaml.SafeLoader):
        pass

    def _construct(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    _Cfn.add_multi_constructor("!", _construct)
    from yaml_helpers import load_with

    return load_with(_Cfn, text)


def test_default_launch_template_carries_no_agentcore_resource_type() -> None:
    """CloudFormation checks resource TYPES against the region's registry before
    Conditions, so a none launch must deploy a template with no
    ``AWS::BedrockAgentCore::`` type in it at all -- in a region without Bedrock
    AgentCore the full template would fail change-set creation for everyone."""
    from kiro_crew.cloud import ec2

    base = ec2.base_template_text()
    assert "Type: AWS::BedrockAgentCore::" not in base  # prose mentions are not types
    # No dangling reference to the stripped resource (a comment at the top may name it).
    assert "!Ref CrewWorkloadIdentity" not in base
    assert "!GetAtt CrewWorkloadIdentity" not in base
    assert "agentcore-only" not in base
    doc = _load_cfn_text(base)
    # The parameters the launcher always passes stay, as do the conditions and
    # the boundary-class tag they drive; only the posture resources/outputs go.
    for param in ("AgentCorePosture", "AgentCoreWorkloadName", "AgentCoreGatewayUrl"):
        assert param in doc["Parameters"]
    assert "IsAgentCoreBoundary" in doc["Conditions"]
    for gone in ("HasAgentCore", "IsWorkloadPosture", "IsLoginPosture"):
        assert gone not in doc["Conditions"]  # posture-only, would be dead in the base
    assert "InstanceRole" in doc["Resources"] and "Instance" in doc["Resources"]
    for gone in (
        "CrewWorkloadIdentity",
        "AgentCoreWorkloadInstancePolicy",
        "AgentCoreLoginInstancePolicy",
    ):
        assert gone not in doc["Resources"]
    for gone in ("AgentCoreWorkloadIdentityName", "AgentCoreWorkloadIdentityArn"):
        assert gone not in doc["Outputs"]
    # The full template still has all of it -- a posture launch needs the service.
    full = _load_cfn_template()
    assert (
        full["Resources"]["CrewWorkloadIdentity"]["Type"]
        == "AWS::BedrockAgentCore::WorkloadIdentity"
    )
    # Stripping removes exactly the marked blocks: everything else is byte-identical.
    kept = [ln for ln in ec2.load_template().splitlines() if "agentcore-only" not in ln]
    assert all(ln in kept for ln in base.splitlines())


def test_agentcore_only_markers_must_balance() -> None:
    from kiro_crew.cloud import ec2

    with pytest.raises(ValueError, match="unterminated"):
        ec2.base_template_text("Resources:\n  # >>> agentcore-only\n  X: {}\n")
    with pytest.raises(ValueError, match="without a begin"):
        ec2.base_template_text("Resources:\n  # <<< agentcore-only\n")


def test_none_launch_stages_the_base_variant_only_for_the_deploy_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base variant is what the launcher's credentials will execute, so it is
    never left at a path an in-sandbox agent could pre-write: it is staged under
    the agent-hidden ``run/`` dir, in a fresh 0700 subdir, no-follow, and removed
    once the deployment child has exited."""
    import os
    import stat

    from kiro_crew.cloud import ec2

    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    seen: dict[str, object] = {}
    with ec2.staged_template("none") as path:
        seen["path"] = path
        assert path.parent.parent == tmp_path / "run"
        assert path.parent.name.startswith("cfn-stage-")
        if os.name == "posix":
            # Mode bits are a POSIX concept; Windows derives access from the DACL
            # and reports the umask-free defaults here.
            assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert not path.is_symlink()
        text = path.read_text(encoding="utf-8")
        assert "Type: AWS::BedrockAgentCore::" not in text
        assert text == ec2.base_template_text()
    # Gone with the context: nothing outside the child ever sees it.
    assert not Path(str(seen["path"])).exists()
    assert not Path(str(seen["path"])).parent.exists()


def test_posture_launch_stages_a_snapshot_of_the_full_template_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled YAML sits in the package directory, which the agent sandbox
    does not mask and which the ordinary personal install leaves writable to the
    sandbox's own uid -- so a posture deploy must never hand CloudFormation that
    path either. It deploys a snapshot staged exactly like the base variant."""
    from kiro_crew.cloud import ec2

    monkeypatch.setattr("kiro_crew.config.paths.config_dir", lambda: tmp_path)
    for posture in ("workload", "login"):
        with ec2.staged_template(posture) as path:
            assert path != ec2._template_path()
            assert path.parent.parent == tmp_path / "run"
            assert path.parent.name.startswith("cfn-stage-")
            assert path.name == "kirocrew-ec2.yaml"
            assert not path.is_symlink()
            text = path.read_text(encoding="utf-8")
            assert "AWS::BedrockAgentCore::WorkloadIdentity" in text
            assert text == ec2.load_template()
        assert not path.exists()
        assert not path.parent.exists()


def test_deploy_argv_carries_the_staged_file_and_the_dry_run_a_placeholder(
    tmp_path: Path,
) -> None:
    from kiro_crew.cloud import ec2, sizes

    common = dict(
        tag="t1",
        tier=sizes.get_tier("balanced"),
        vpc_id="vpc-1",
        subnet_id="subnet-1",
        permissions_boundary_arn="arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary",
    )
    # The deploy passes the staged path explicitly.
    staged = tmp_path / "kirocrew-ec2-base.yaml"
    argv = ec2.build_deploy_argv(**common, template_file=staged)
    assert argv[argv.index("--template-file") + 1] == str(staged)
    # Without one (the dry run) the none launch shows a placeholder, never a
    # real path -- the base variant exists only inside staged_template.
    argv = ec2.build_deploy_argv(**common)
    shown = argv[argv.index("--template-file") + 1]
    assert shown == "<staged kirocrew-ec2-base.yaml>"
    assert not Path(shown).exists()
    argv = ec2.build_deploy_argv(
        tag="t1",
        tier=sizes.get_tier("balanced"),
        vpc_id="vpc-1",
        subnet_id="subnet-1",
        permissions_boundary_arn="arn:aws:iam::123456789012:policy/kirocrew-ec2-boundary-agentcore",
        agentcore_posture="workload",
        agentcore_workload_name="kirocrew-t1",
    )
    # A posture dry run likewise shows a placeholder, never the bundled path:
    # the deploy snapshots the full template the same way.
    shown = argv[argv.index("--template-file") + 1]
    assert shown == "<staged kirocrew-ec2.yaml>"
    assert not Path(shown).exists()
