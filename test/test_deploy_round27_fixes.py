"""Regression tests for Round-27 findings (KiroCrew PR #6).

F1 (Codex): missing bucket TagSet is an identity FAILURE — the reaper
    quarantines the manifest and touches nothing (no name-prefix pass);
    the shell reaper twin gets the same gate.
F2 (Claude): the permissions boundary's CloudFront cap tag-scopes the
    destructive distribution actions — an unconditioned Resource:"*" would
    re-open the CreateRole+PutRolePolicy+PassRole escalation the boundary
    exists to close.
"""
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / (
    "src/kiro_crew/deploy/skills/artifact-deploy/scripts"
)


# --- F1: NoSuchTagSet fail-closed ---


def test_f1_lambda_untagged_bucket_is_identity_failure():
    src = (_SCRIPTS / "reaper_lambda/index.py").read_text()
    seg = src.split('elif code == "NoSuchTagSet":', 1)[1][:800]
    # Fail closed: quarantine + return, no prefix-based pass.
    assert "_quarantine_manifest(slug)" in seg
    assert 'return "reaped"' in seg
    assert 'startswith("kirocrew-web-")' not in seg


def test_f1_shell_reaper_gates_bucket_deletion_on_tag():
    src = (_SCRIPTS / "reaper.sh").read_text()
    assert "get-bucket-tagging" in src
    # The tag check precedes the bucket empty (s3 rm) and delete.
    assert src.index("get-bucket-tagging") < src.index(
        's3 rm "s3://$_site_bucket/"')


# --- F2: boundary CloudFront cap tag-scoped ---


def test_f2_boundary_destructive_cloudfront_actions_are_tag_scoped():
    from kiro_crew.deploy import iam
    doc = iam.boundary_policy_document()
    for st in doc["Statement"]:
        actions = st.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        destructive = {"cloudfront:DeleteDistribution",
                       "cloudfront:UpdateDistribution"}
        if destructive & set(actions):
            cond = json.dumps(st.get("Condition", {}))
            assert "aws:ResourceTag/kirocrew:managed" in cond, (
                f"statement {st.get('Sid')} grants destructive CloudFront "
                f"actions without the managed-tag condition")


def test_f2_boundary_no_unconditioned_delete_distribution_on_star():
    from kiro_crew.deploy import iam
    doc = iam.boundary_policy_document()
    for st in doc["Statement"]:
        actions = st.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if "cloudfront:DeleteDistribution" in actions:
            assert st.get("Condition"), (
                "DeleteDistribution must never be unconditioned in the boundary")
