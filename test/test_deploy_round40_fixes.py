"""R40 regression (round-40 Codex findings on 142a3b2).

F1: an EMPTY profile name must never reach ``aws configure set`` —
    validate_field skips the pattern check on "" (and _PROFILE_SPEC is not
    required), and engine._aws() OMITS --profile for an empty name, so
    ``POST {"name":"","create":true}`` would silently rewrite the user's
    DEFAULT AWS profile. Both layers must reject empty explicitly.

F2: the ``.kirocrew-deploy.json`` control manifest lives beside published
    site content in a CloudFront origin bucket — without an explicit Deny it
    is world-readable through the distribution (leaks local username +
    bucket/distribution/OAC ids). Both bucket policies (engine per-site +
    base-stack shared) must deny CloudFront the manifest and quarantine keys.
"""
import json
from pathlib import Path
from unittest import mock

from kiro_crew.deploy import engine, profiles
from kiro_crew.validation import validate_field

REPO = Path(__file__).resolve().parents[1]
HANDLERS = (REPO / "src" / "kiro_crew" / "deploy" / "handlers.py").read_text(encoding="utf-8")
BASE_STACK = (
    REPO / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy"
    / "templates" / "base-stack.yaml"
).read_text(encoding="utf-8")


class TestF1EmptyProfileName:
    def test_validate_field_gap_this_guards(self):
        # Documents WHY the explicit checks exist: "" passes _PROFILE_SPEC
        # because the pattern check is skipped on empty and required is not
        # set. If this ever starts raising, the explicit guards are welcome
        # redundancy — but the guards must never be removed on that basis.
        from kiro_crew.deploy.handlers import _PROFILE_SPEC
        assert validate_field("", _PROFILE_SPEC) == ""

    def test_handler_rejects_empty_name_before_side_effects(self):
        # The explicit empty-name rejection must sit between validation and
        # the first side effect (registry load / create_aws_profile).
        start = HANDLERS.index("async def _handle_profiles_post")
        body = HANDLERS[start:start + 3000]
        reject_idx = body.index("name must not be empty")
        registry_idx = body.index("load_registry")
        create_idx = body.index("create_aws_profile")
        assert reject_idx < registry_idx, "empty-name check must precede registry access"
        assert reject_idx < create_idx, "empty-name check must precede aws configure set"

    def test_create_aws_profile_rejects_empty_name(self):
        # Defense in depth: even called directly, an empty name must never
        # reach _configure_set (which would write the DEFAULT profile).
        with mock.patch.object(profiles, "_configure_set") as cs:
            err = profiles.create_aws_profile("", "us-west-2")
        assert err is not None and "empty" in err
        cs.assert_not_called()

    def test_aws_argv_omits_profile_flag_on_empty(self):
        # Documents the downstream hazard: empty profile => no --profile flag
        # => the AWS CLI operates on the DEFAULT profile.
        argv = engine._aws(["configure", "set", "region", "us-west-2"], "")
        assert "--profile" not in argv


class TestF2ManifestNotPublic:
    def _policy_statements(self):
        with mock.patch.object(engine, "_checked") as checked:
            engine.put_oac_bucket_policy(
                "kirocrew-web-example", "arn:aws:cloudfront::123:distribution/EX1", "p")
        argv = checked.call_args[0][0]
        policy = json.loads(argv[argv.index("--policy") + 1])
        return policy["Statement"]

    def test_engine_policy_denies_cloudfront_manifest_reads(self):
        stmts = self._policy_statements()
        denies = [s for s in stmts if s["Effect"] == "Deny"]
        assert denies, "per-site bucket policy must contain an explicit Deny"
        deny = denies[0]
        assert deny["Principal"] == {"Service": "cloudfront.amazonaws.com"}
        res = deny["Resource"]
        assert any(r.endswith("/*/.kirocrew-deploy.json") for r in res), (
            "Deny must cover the per-slug control manifest key")
        assert any("/_quarantine/" in r for r in res), (
            "Deny must cover the quarantine prefix")

    def test_engine_policy_keeps_oac_allow(self):
        # The Deny must be additive — the OAC Allow (pinned to the
        # distribution ARN) still serves the site content.
        stmts = self._policy_statements()
        allows = [s for s in stmts if s["Effect"] == "Allow"]
        assert len(allows) == 1
        assert allows[0]["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("EX1")

    def test_base_stack_policy_denies_cloudfront_manifest_reads(self):
        # Shell/shared-bucket twin: base-stack.yaml must carry the same Deny.
        assert "DenyCloudFrontControlManifests" in BASE_STACK
        idx = BASE_STACK.index("DenyCloudFrontControlManifests")
        block = BASE_STACK[idx:idx + 600]
        assert "Effect: Deny" in block
        assert "/*/.kirocrew-deploy.json" in block
        assert "/_quarantine/*" in block
