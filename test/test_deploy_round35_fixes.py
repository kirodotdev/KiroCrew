"""R35 regression tests (round-35 Codex findings on baaaaec).

F1: fullstack apigateway mutations (PUT/PATCH/DELETE) must be gated on the
    kirocrew:managed resource tag — the deploy profile must not be able to
    mutate/delete unrelated APIs in the account.
F2: the "file-too-large" staging rejection (added in R33) must be in the
    _do_deploy structured-409 allowlist, not escape as a 500.
"""
from pathlib import Path

import yaml

from kiro_crew.deploy import iam as iam_mod

REPO = Path(__file__).resolve().parents[1]
HANDLERS = (REPO / "src" / "kiro_crew" / "deploy" / "handlers.py").read_text(encoding="utf-8")
TEMPLATES = REPO / "src" / "kiro_crew" / "deploy" / "skills" / "artifact-deploy" / "templates"


def _statements(doc):
    return {st.get("Sid", ""): st for st in doc["Statement"]}


class TestF1ApiGwTagCondition:
    def test_mutations_are_tag_conditioned(self):
        doc = iam_mod.policy_document(tier="fullstack")
        sts = _statements(doc)
        assert "ApiGatewayFullstack" not in sts, "combined statement must be split"
        mut = sts["ApiGatewayFullstackMutateManagedOnly"]
        # R39: POST joins the gated set — POST under /apis/* creates routes/
        # integrations beneath ANY existing API, so it is a mutation.
        assert set(mut["Action"]) == {
            "apigateway:POST", "apigateway:PUT", "apigateway:PATCH",
            "apigateway:DELETE",
        }
        cond = mut["Condition"]["StringEquals"]
        assert cond["aws:ResourceTag/kirocrew:managed"] == "true"

    def test_read_has_no_mutations_and_post_only_on_collection(self):
        doc = iam_mod.policy_document(tier="fullstack")
        sts = _statements(doc)
        rd = sts["ApiGatewayFullstackRead"]
        assert rd["Action"] == ["apigateway:GET"]
        root = sts["ApiGatewayFullstackCreateRoot"]
        assert root["Action"] == ["apigateway:POST"]
        assert root["Resource"] == "arn:aws:apigateway:*::/apis", (
            "unconditioned POST must be limited to the /apis collection"
        )

    def test_templates_tag_the_api(self):
        # the tag condition is only sound if every template tags its Api
        class _CfnLoader(yaml.SafeLoader):
            pass

        _CfnLoader.add_multi_constructor(
            "!", lambda loader, suffix, node: None
        )
        for name in ("app-apigw.yaml", "app-apigw-ddb.yaml"):
            raw = (TEMPLATES / name).read_text(encoding="utf-8")
            doc = yaml.load(raw, Loader=_CfnLoader)  # noqa: S506 — tag-blind CFN parse
            api = next(
                r for r in doc["Resources"].values()
                if r.get("Type") == "AWS::ApiGatewayV2::Api"
            )
            tags = api["Properties"].get("Tags") or {}
            assert tags.get("kirocrew:managed") == "true", (
                f"{name}: HttpApi must carry kirocrew:managed=true or the "
                f"R35 mutation condition locks the deploy profile out"
            )


class TestF2FileTooLargeInRejects:
    def test_rejects_allowlist_includes_file_too_large(self):
        idx = HANDLERS.index("_rejects = (")
        block = HANDLERS[idx:idx + 300]
        assert '"file-too-large"' in block, (
            "file-too-large must map to the structured 409, not a raw 500"
        )
