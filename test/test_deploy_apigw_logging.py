"""Regression: API Gateway access logging (CWE-778) — template <-> IAM coverage.

The app-apigw*.yaml stages create a CloudWatch LogGroup + AccessLogSettings.
deploy-backend.sh runs `cloudformation deploy` with the deploy principal's own
creds, so the fullstack policy must grant scoped log-group lifecycle perms or
the stack rolls back; and the reaper cascade must be able to delete them.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.deploy.iam import boundary_policy_document, policy_document

_TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/deploy/skills/artifact-deploy/templates"
)


def _actions(doc: dict) -> set[str]:
    acts: set[str] = set()
    for st in doc["Statement"]:
        a = st.get("Action", [])
        acts.update([a] if isinstance(a, str) else a)
    return acts


def test_apigw_templates_have_access_logging():
    for name in ("app-apigw.yaml", "app-apigw-ddb.yaml"):
        text = (_TEMPLATES / name).read_text()
        assert "AccessLogSettings:" in text, name
        assert "AWS::Logs::LogGroup" in text, name


def test_fullstack_policy_grants_scoped_log_group_perms():
    doc = policy_document(tier="fullstack")
    acts = _actions(doc)
    for need in ("logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy"):
        assert need in acts, f"fullstack policy missing {need}"
    logs_st = [
        s for s in doc["Statement"]
        if "logs:CreateLogGroup" in (
            s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        )
    ]
    assert logs_st, "no logs statement in fullstack policy"
    res = logs_st[0]["Resource"]
    res = res if isinstance(res, list) else [res]
    assert all("/kirocrew-deploy-app/" in r for r in res), res
    assert all(r != "*" for r in res), res
    # logs:DescribeLogGroups is not resource-scopable — must be Resource "*".
    desc = [s for s in doc["Statement"] if s.get("Action") == ["logs:DescribeLogGroups"]]
    assert desc and desc[0]["Resource"] == "*", "DescribeLogGroups must be Resource '*'"


def test_static_policy_has_no_log_perms():
    assert "logs:CreateLogGroup" not in _actions(policy_document(tier="static"))


def test_reaper_boundary_can_delete_log_groups():
    assert "logs:DeleteLogGroup" in _actions(boundary_policy_document())
