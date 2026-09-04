"""The API-Gateway-over-Function-URL rationale is stated security-first.

Issue #3475: the artifact-deploy docs argued the M2 backend shape backwards --
as if API Gateway were chosen *so that* a corporate account's automated
guardrails would not fire. The actual reason is the security property itself: a
Function URL needs a ``Principal:"*"`` resource policy, so the Lambda becomes
world-accessible, while an API Gateway HTTP API keeps the function's invoke
permission scoped to the API Gateway service principal. Guardrail behaviour is a
downstream consequence of that property, not the goal.

The prose surfaces are ratcheted as prose: they fail if any of them reintroduces
the guardrail-as-goal framing, or drops the property that makes the shape
correct on its own merits. The templates are asserted structurally instead --
every ``AWS::Lambda::Permission`` must name the API Gateway service principal by
equality, which is what the prose is claiming and is stronger than finding the
string somewhere in the file.
"""

import re
from pathlib import Path

import yaml
from yaml_helpers import load_with

_DEPLOY = Path(__file__).parent.parent / "src" / "kiro_crew" / "deploy"
_SKILL = _DEPLOY / "skills" / "artifact-deploy"

SKILL_MD = _SKILL / "SKILL.md"
APIGW_TEMPLATE = _SKILL / "templates" / "app-apigw.yaml"
APIGW_DDB_TEMPLATE = _SKILL / "templates" / "app-apigw-ddb.yaml"
DEPLOY_BACKEND_SH = _SKILL / "scripts" / "deploy-backend.sh"
ATTACH_BACKEND_PY = _SKILL / "scripts" / "attach_backend.py"
DEPLOY_WEB_DOC = (
    Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "deploy-web.md"
)

#: Every surface that explains why the backend sits behind API Gateway. A new
#: one must be added here, so the framing is pinned everywhere it is stated.
RATIONALE_SURFACES = (
    SKILL_MD,
    APIGW_TEMPLATE,
    APIGW_DDB_TEMPLATE,
    DEPLOY_BACKEND_SH,
    ATTACH_BACKEND_PY,
    DEPLOY_WEB_DOC,
)

#: Phrasings that make "a guardrail does not fire" the purpose of the choice.
#: "Guardrail-safe" is included because as a standalone label it names the
#: account-policy outcome as the property being claimed.
GUARDRAIL_AS_GOAL = (
    re.compile(r"guardrail[- ]safe", re.IGNORECASE),
    re.compile(r"auto[- ]mitigat", re.IGNORECASE),
    re.compile(r"(?:so|thus)\b[^.]{0,80}\bdetector\b[^.]{0,40}\bfires?\b", re.I),
    re.compile(r"\bno\b[^.]{0,40}\b(?:mitigation|detector)\b[^.]{0,20}\bfires?\b", re.I),
)

#: The service principal an API-Gateway-fronted Lambda scopes its invoke
#: permission to.
APIGW_SERVICE_PRINCIPAL = "apigateway.amazonaws.com"

#: Prose must name the principal as *what the policy is scoped to*, not merely
#: mention it. Matched as a phrase with escaped dots rather than by substring
#: containment: a bare hostname in an ``in`` test is both the weaker assertion
#: and, to CodeQL, indistinguishable from URL sanitization
#: (py/incomplete-url-substring-sanitization).
SCOPED_TO_APIGATEWAY = re.compile(
    r"scoped\s+to\s+`?apigateway\.amazonaws\.com`?", re.IGNORECASE
)

#: A Function URL's world-accessible resource policy, in either spelling the
#: prose may use.
PRINCIPAL_STAR = re.compile(r"""Principal:\s*["']?\*["']?""")


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that ignores CFN intrinsic tags (!Ref/!Sub/!If/!GetAtt...)."""


def _construct_intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_TagTolerantLoader.add_multi_constructor(None, _construct_intrinsic)


def _lambda_permissions(path: Path) -> dict[str, dict]:
    """Every ``AWS::Lambda::Permission`` in a template, by logical id."""
    doc = load_with(_TagTolerantLoader, path.read_text(encoding="utf-8"))
    return {
        name: res.get("Properties", {})
        for name, res in doc.get("Resources", {}).items()
        if res.get("Type") == "AWS::Lambda::Permission"
    }


class TestRationaleIsNotGuardrailEvasion:
    """No surface presents guardrail behaviour as the reason for the design."""

    def test_no_surface_frames_guardrails_as_the_goal(self):
        offenders = []
        for path in RATIONALE_SURFACES:
            text = path.read_text(encoding="utf-8")
            for pattern in GUARDRAIL_AS_GOAL:
                match = pattern.search(text)
                if match:
                    offenders.append(f"{path.name}: {match.group(0)!r}")
        assert not offenders, (
            "guardrail-as-goal framing found (issue #3475): "
            + "; ".join(offenders)
            + " -- state the security property (a Function URL needs "
            'Principal:"*"; API Gateway keeps the Lambda policy scoped) and let '
            "guardrail compatibility be the consequence."
        )


class TestProseStatesTheSecurityProperty:
    """The docs that argue the choice name the property that justifies it."""

    def test_skill_md_names_principal_star_and_the_scoped_policy(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        assert PRINCIPAL_STAR.search(text), (
            "SKILL.md must say a Function URL requires a world-accessible "
            'Principal:"*" resource policy'
        )
        assert SCOPED_TO_APIGATEWAY.search(text), (
            "SKILL.md must say the Lambda's policy is scoped TO the API Gateway "
            "service principal"
        )
        assert "app-lambda.yaml" in text, (
            "SKILL.md must keep documenting the Function URL variant as the "
            "lighter option for unrestricted accounts"
        )

    def test_deploy_web_doc_names_principal_star_and_the_scoped_policy(self):
        text = DEPLOY_WEB_DOC.read_text(encoding="utf-8")
        assert PRINCIPAL_STAR.search(text)
        assert SCOPED_TO_APIGATEWAY.search(text)


class TestTemplatesScopeTheInvokePermission:
    """The claim the prose makes is true of the templates it describes."""

    def test_every_lambda_permission_names_the_apigateway_principal(self):
        for path in (APIGW_TEMPLATE, APIGW_DDB_TEMPLATE):
            permissions = _lambda_permissions(path)
            assert permissions, f"{path.name} declares no AWS::Lambda::Permission"
            for name, props in permissions.items():
                assert props.get("Principal") == APIGW_SERVICE_PRINCIPAL, (
                    f"{path.name}:{name} must scope invoke to "
                    f"{APIGW_SERVICE_PRINCIPAL}, got {props.get('Principal')!r} "
                    "-- any other principal (notably '*') makes the function "
                    "world-accessible, which is the shape these docs rule out"
                )
                assert props.get("Action") == "lambda:InvokeFunction", (
                    f"{path.name}:{name} must grant only lambda:InvokeFunction"
                )

    def test_templates_still_say_the_lambda_is_not_world_accessible(self):
        for path in (APIGW_TEMPLATE, APIGW_DDB_TEMPLATE):
            text = path.read_text(encoding="utf-8")
            assert "world-accessible" in text, (
                f"{path.name} must still say the Lambda is not world-accessible"
            )
