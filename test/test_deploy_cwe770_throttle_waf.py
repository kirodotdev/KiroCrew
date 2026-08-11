"""CWE-770: One-Click Deploy IaC must bound request throughput.

Part A — the per-app API Gateway HTTP API $default stage (both the stateless
app-apigw.yaml and the stateful app-apigw-ddb.yaml templates) must carry
DefaultRouteSettings with ThrottlingRateLimit + ThrottlingBurstLimit, wired to
parameters with sane defaults, so a per-app deploy cannot be driven into
unbounded Lambda invocations / cost. AutoDeploy behavior is preserved.

Part B — the shared CloudFront distribution (base-stack.yaml) must be able to
attach a WAFv2 WebACL for rate limiting. Because base-stack deploys in the
profile's region (default us-west-2), an inline CLOUDFRONT-scoped WebACL would
fail to create (CloudFront WebACLs must live in us-east-1), so the safe wiring
is an opt-in WafWebAclArn parameter gated by a Condition onto WebACLId.
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TPL_DIR = (
    REPO / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "templates"
)
APIGW = (TPL_DIR / "app-apigw.yaml").read_text(encoding="utf-8")
APIGW_DDB = (TPL_DIR / "app-apigw-ddb.yaml").read_text(encoding="utf-8")
BASE_STACK = (TPL_DIR / "base-stack.yaml").read_text(encoding="utf-8")


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that ignores CFN intrinsic tags (!Ref/!Sub/!If/!GetAtt...)."""


def _construct_intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


# Register as the catch-all multi-constructor (tag_prefix=None) so EVERY CFN
# intrinsic tag is tolerated -- including the Conditions-block short tags
# !Not/!Equals/!And as well as !Ref/!Sub/!If/!GetAtt. A "!" prefix alone does
# not reliably match every local tag across PyYAML versions; None is the
# documented universal fallback.
_TagTolerantLoader.add_multi_constructor(None, _construct_intrinsic)


def _load(text):
    return yaml.load(text, Loader=_TagTolerantLoader)  # noqa: S506 — tag-blind CFN parse


class TestPartAApiGwThrottling:
    def _assert_stage_throttled(self, text):
        doc = _load(text)
        params = doc["Parameters"]
        assert "ApiThrottlingRateLimit" in params
        assert "ApiThrottlingBurstLimit" in params
        assert params["ApiThrottlingRateLimit"]["Type"] == "Number"
        assert params["ApiThrottlingBurstLimit"]["Type"] == "Number"
        # Sane defaults for a per-app deploy.
        assert params["ApiThrottlingRateLimit"]["Default"] == 50
        assert params["ApiThrottlingBurstLimit"]["Default"] == 100

        stage = doc["Resources"]["Stage"]["Properties"]
        # AutoDeploy behavior preserved.
        assert stage["AutoDeploy"] is True
        drs = stage["DefaultRouteSettings"]
        # Wired to the parameters (intrinsic !Ref -> scalar name via loader).
        assert drs["ThrottlingRateLimit"] == "ApiThrottlingRateLimit"
        assert drs["ThrottlingBurstLimit"] == "ApiThrottlingBurstLimit"

    def test_apigw_stage_has_throttling(self):
        self._assert_stage_throttled(APIGW)

    def test_apigw_ddb_stage_has_throttling(self):
        self._assert_stage_throttled(APIGW_DDB)


class TestPartBCloudFrontWaf:
    def test_base_stack_exposes_waf_param_and_condition(self):
        doc = _load(BASE_STACK)
        assert "WafWebAclArn" in doc["Parameters"]
        assert doc["Parameters"]["WafWebAclArn"]["Default"] == ""
        assert "HasWafWebAcl" in doc["Conditions"]

    def test_distribution_wires_webaclid_conditionally(self):
        doc = _load(BASE_STACK)
        dist_cfg = doc["Resources"]["Distribution"]["Properties"]["DistributionConfig"]
        # !If [HasWafWebAcl, !Ref WafWebAclArn, !Ref 'AWS::NoValue'] -> the loader
        # flattens to the sequence of the (already scalar-ized) branches.
        web_acl = dist_cfg["WebACLId"]
        assert web_acl == ["HasWafWebAcl", "WafWebAclArn", "AWS::NoValue"]

    def test_region_constraint_documented(self):
        # The us-east-1 CLOUDFRONT-scope constraint must be spelled out so a
        # future editor does not "helpfully" inline a broken WebACL.
        assert "us-east-1" in BASE_STACK
        assert "CLOUDFRONT" in BASE_STACK
