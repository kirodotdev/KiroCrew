"""Tests for deploy_web IAM policy generator + read-only reachability + skill wiring."""
from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.deploy import engine
from kiro_crew.deploy import iam as iam_mod

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
    # Must cover BOTH the CFN template prefix (kirocrew-deploy-*) and the
    # engine.py HTTP-publish prefix (kirocrew-web-*).
    assert f"arn:aws:s3:::{iam_mod.S3_PREFIX}" in s3["Resource"]
    assert iam_mod.S3_PREFIX_WEB in s3["Resource"]
    cf = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontManageTagged")
    assert cf["Condition"]["StringEquals"]["aws:ResourceTag/kirocrew:managed"] == "true"  # tag scope


def test_policy_prefix_matches_template_bucket():
    """Regression: S3_PREFIX must match the bucket name pattern in the template."""
    import yaml

    # CloudFormation templates use intrinsic functions (!Sub, !Ref, !GetAtt)
    # that yaml.safe_load can't handle — add constructors for the ones we need.
    class _CfnLoader(yaml.SafeLoader):
        pass
    for tag in ("!Sub", "!Ref", "!GetAtt", "!If", "!Select", "!Join"):
        _CfnLoader.add_constructor(tag, lambda loader, node: {"Fn::Sub": loader.construct_scalar(node)}
                                   if node.tag == "!Sub" else loader.construct_scalar(node))
    # Re-register !Sub properly — it's the one we care about.
    _CfnLoader.add_constructor("!Sub", lambda loader, node: {"Fn::Sub": loader.construct_scalar(node)})

    template_path = _PKG / "skills" / "artifact-deploy" / "templates" / "base-stack.yaml"
    with open(template_path) as f:
        tmpl = yaml.load(f, Loader=_CfnLoader)  # noqa: S506
    bucket_name = tmpl["Resources"]["OriginBucket"]["Properties"]["BucketName"]
    # Template uses !Sub 'kirocrew-deploy-${AWS::AccountId}-${AWS::Region}'
    # The IAM prefix must be 'kirocrew-deploy-*' to cover all account/region combos.
    sub_str = bucket_name["Fn::Sub"] if isinstance(bucket_name, dict) else bucket_name
    prefix_stem = sub_str.split("-${")[0]
    expected_prefix = f"{prefix_stem}-*"
    assert iam_mod.S3_PREFIX == expected_prefix


def test_policy_covers_engine_bucket_prefix():
    """Regression (R8→R9): engine.py creates kirocrew-web-* buckets; IAM must cover them."""
    doc = iam_mod.policy_document()
    s3_bucket = next(s for s in doc["Statement"] if s["Sid"] == "S3BucketLevel")
    s3_object = next(s for s in doc["Statement"] if s["Sid"] == "S3ObjectLevel")
    web_arn = f"arn:aws:s3:::{engine.BUCKET_PREFIX}*"
    web_obj_arn = f"arn:aws:s3:::{engine.BUCKET_PREFIX}*/*"
    assert web_arn in s3_bucket["Resource"], f"S3BucketLevel missing {web_arn}"
    assert web_obj_arn in s3_object["Resource"], f"S3ObjectLevel missing {web_obj_arn}"


def test_policy_covers_cloudfront_function_and_headers():
    """Ensure IAM policy includes CloudFront Function + ResponseHeadersPolicy actions."""
    doc = iam_mod.policy_document()
    create_stmt = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontCreateList")
    actions = create_stmt["Action"]
    assert "cloudfront:CreateFunction" in actions
    assert "cloudfront:PublishFunction" in actions
    assert "cloudfront:CreateResponseHeadersPolicy" in actions
    manage_stmt = next(s for s in doc["Statement"] if s["Sid"] == "CloudFrontManageTagged")
    managed_actions = manage_stmt["Action"]
    assert "cloudfront:UpdateFunction" in managed_actions
    assert "cloudfront:DeleteResponseHeadersPolicy" in managed_actions


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


def test_policy_fullstack_tier_includes_lambda_and_dynamodb():
    doc = iam_mod.policy_document(tier="fullstack")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "LambdaFullstack" in sids
    assert "ApiGatewayFullstackRead" in sids
    assert "DynamoDBFullstack" in sids
    # R23 F1: the old IAMPassRoleFullstack statement was split — CreateRole
    # now lives in its own boundary-conditioned statement.
    assert "IAMCreateRoleWithBoundaryOnly" in sids
    assert "IAMRoleLifecycleFullstack" in sids
    assert "IAMDenyBoundaryTampering" in sids
    # Resources are scoped to kirocrew-deploy-app-*
    lambda_stmt = next(s for s in doc["Statement"] if s["Sid"] == "LambdaFullstack")
    assert "kirocrew-deploy-app-*" in lambda_stmt["Resource"]


def test_policy_static_tier_has_no_fullstack_sids():
    doc = iam_mod.policy_document(tier="static")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "LambdaFullstack" not in sids
    assert "ApiGatewayFullstackRead" not in sids


def test_policy_fullstack_tier_includes_reaper_permissions():
    """Fix #7: fullstack tier must include scoped reaper Lambda/CFN/IAM/Events."""
    doc = iam_mod.policy_document(tier="fullstack")
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "ReaperLambda" in sids
    assert "ReaperCloudFormation" in sids
    assert "ReaperIAMRole" in sids
    assert "ReaperEvents" in sids
    # All scoped to kirocrew-deploy-reaper*
    reaper_lambda = next(s for s in doc["Statement"] if s["Sid"] == "ReaperLambda")
    assert "kirocrew-deploy-reaper" in reaper_lambda["Resource"]
    reaper_cfn = next(s for s in doc["Statement"] if s["Sid"] == "ReaperCloudFormation")
    assert "kirocrew-deploy-reaper" in reaper_cfn["Resource"]
    reaper_iam = next(s for s in doc["Statement"] if s["Sid"] == "ReaperIAMRole")
    assert "kirocrew-deploy-reaper" in reaper_iam["Resource"]
    reaper_events = next(s for s in doc["Statement"] if s["Sid"] == "ReaperEvents")
    assert "kirocrew-deploy-reaper" in reaper_events["Resource"]


def test_policy_fullstack_iam_attach_constrained_to_template_policies():
    """AttachRolePolicy must be limited to the exact managed policies used by templates."""
    doc = iam_mod.policy_document(tier="fullstack")
    attach_stmt = next(
        (s for s in doc["Statement"] if s.get("Sid") == "IAMAttachRolePolicyFullstack"), None)
    assert attach_stmt is not None, "IAMAttachRolePolicyFullstack statement missing"
    # Must have ArnEquals condition restricting iam:PolicyARN
    cond = attach_stmt.get("Condition", {})
    arn_eq = cond.get("ArnEquals", {})
    allowed = arn_eq.get("iam:PolicyARN", [])
    assert "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" in allowed
    # Must NOT allow AdministratorAccess or PowerUser
    for policy in allowed:
        assert "AdministratorAccess" not in policy
        assert "PowerUser" not in policy


def test_policy_fullstack_passrole_scoped_to_lambda():
    """PassRole must only be passable to lambda.amazonaws.com."""
    doc = iam_mod.policy_document(tier="fullstack")
    pass_stmt = next(
        (s for s in doc["Statement"] if s.get("Sid") == "IAMPassRoleLambdaOnly"), None)
    assert pass_stmt is not None, "IAMPassRoleLambdaOnly statement missing"
    cond = pass_stmt.get("Condition", {})
    assert cond.get("StringEquals", {}).get("iam:PassedToService") == "lambda.amazonaws.com"


def test_policy_custom_domain_no_delete_certificate():
    """acm:DeleteCertificate must not be granted (no template uses it)."""
    doc = iam_mod.policy_document(include_custom_domain=True)
    acm_stmt = next(s for s in doc["Statement"] if s.get("Sid") == "AcmForCloudFront")
    assert "acm:DeleteCertificate" not in acm_stmt["Action"]


def test_unmark_webapp_expired_sets_live():
    """unmark_webapp_expired must set status to 'live', not 'active'."""
    import tempfile
    from pathlib import Path

    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.deploy.webapp_types import WebAppDeployTarget, WebAppLifecycle, WebAppMetadata

    with tempfile.TemporaryDirectory() as tmp:
        store = ArtifactStore(Path(tmp))
        art = store.create(name="test-webapp", content="<h1>hi</h1>", kind="webapp")
        # Manually inject webapp_metadata with expired lifecycle
        art.webapp_metadata = WebAppMetadata(
            deploy_target=WebAppDeployTarget(profile="p", region="us-east-1"),
            lifecycle=WebAppLifecycle(status="expired"),
        )
        store._write_meta(art)  # persist
        result = store.unmark_webapp_expired(art.slug)
        assert result.webapp_metadata.lifecycle.status == "live"


def test_reaper_template_includes_engine_arch_permissions():
    """F1 regression: reaper.yaml must have CloudFrontEngineSites + S3EngineSites for engine-arch."""
    import yaml

    class _CfnLoader(yaml.SafeLoader):
        pass
    for tag in ("!Sub", "!Ref", "!GetAtt", "!If", "!Select", "!Join"):
        _CfnLoader.add_constructor(tag, lambda loader, node: loader.construct_scalar(node))

    template_path = _PKG / "skills" / "artifact-deploy" / "templates" / "reaper.yaml"
    with open(template_path) as f:
        tmpl = yaml.load(f, Loader=_CfnLoader)  # noqa: S506

    role_props = tmpl["Resources"]["ReaperRole"]["Properties"]
    stmts = role_props["Policies"][0]["PolicyDocument"]["Statement"]
    sids = {s["Sid"] for s in stmts}
    assert "CloudFrontEngineSites" in sids, "Missing CloudFrontEngineSites statement"
    assert "S3EngineSites" in sids, "Missing S3EngineSites statement"
    assert "S3EngineSiteObjects" in sids, "Missing S3EngineSiteObjects statement"

    # CloudFrontEngineSites must be scoped by the kirocrew:site tag PRESENCE
    # test (Null: false), NOT kirocrew:managed==true (which the shared base-stack
    # distribution also carries, so it wouldn't scope to per-site only).
    # The value of kirocrew:site is the site_id string — a StringEquals "true"
    # condition would be a dead (never-matching) condition. Presence test ensures
    # only distributions that carry the tag at all are affected.
    cf_engine = next(s for s in stmts if s["Sid"] == "CloudFrontEngineSites")
    assert "cloudfront:DeleteDistribution" in cf_engine["Action"]
    assert cf_engine["Condition"]["Null"]["aws:ResourceTag/kirocrew:site"] == "false"

    # S3EngineSites must be scoped to kirocrew-web-*
    s3_engine = next(s for s in stmts if s["Sid"] == "S3EngineSites")
    assert "arn:aws:s3:::kirocrew-web-*" in s3_engine["Resource"]
