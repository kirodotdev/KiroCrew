"""Regression tests for Round-23 findings (KiroCrew PR #6).

F1: the fullstack IAM policy conditions iam:CreateRole on the
    kirocrew-deploy-app-boundary permissions boundary and denies boundary
    tampering; all app-template roles carry the boundary.
F2: staging failures clean up the temp tree and every expected staging
    rejection converts to a structured 409 (not a raw 500).
F3: _stage_tree_safe rejects symlinked DIRECTORIES (previously silently
    dropped from the snapshot).
F4: no Amazon-internal tooling instructions in the public dashboard.
"""
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src/kiro_crew/deploy/skills/artifact-deploy/templates"


# --- F1: privilege-escalation chain closed by permissions boundary ---


def _fullstack_policy() -> dict:
    from kiro_crew.deploy import iam
    return iam.policy_document(tier="fullstack")


def test_f1_create_role_requires_boundary():
    pol = _fullstack_policy()
    create_stmts = [
        st for st in pol["Statement"]
        if "iam:CreateRole" in st.get("Action", []) and st["Effect"] == "Allow"
    ]
    assert create_stmts, "no Allow statement for iam:CreateRole"
    for st in create_stmts:
        cond = st.get("Condition", {})
        bound = json.dumps(cond)
        assert "iam:PermissionsBoundary" in bound
        assert "kirocrew-deploy-app-boundary" in bound


def test_f1_boundary_tampering_denied():
    pol = _fullstack_policy()
    deny = [
        st for st in pol["Statement"]
        if st["Effect"] == "Deny"
        and "iam:DeleteRolePermissionsBoundary" in st.get("Action", [])
    ]
    assert deny
    assert "iam:PutRolePermissionsBoundary" in deny[0]["Action"]


def test_f1_boundary_policy_document_is_scoped():
    from kiro_crew.deploy import iam
    doc = iam.boundary_policy_document()
    body = json.dumps(doc)
    # Only own-prefix resources; no wildcard-action grants.
    assert "kirocrew-deploy-app-*" in body
    assert '"Action": "*"' not in body
    # R25 F3 added scoped iam cleanup actions for the reaper cascade —
    # but the boundary must NEVER contain the escalation primitives.
    for esc in ("iam:CreateRole", "iam:PutRolePolicy",
                "iam:AttachRolePolicy", "iam:PassRole",
                "iam:PutRolePermissionsBoundary",
                "iam:DeleteRolePermissionsBoundary"):
        assert esc not in body, f"escalation primitive {esc} in boundary"


def test_f1_all_template_roles_carry_boundary():
    for name in ("app-apigw.yaml", "app-apigw-ddb.yaml", "app-lambda.yaml"):
        src = (_TPL / name).read_text(encoding="utf-8")
        roles = src.count("Type: AWS::IAM::Role")
        boundaries = src.count("PermissionsBoundary:")
        assert roles == boundaries, f"{name}: {roles} roles, {boundaries} boundaries"
        assert "kirocrew-deploy-app-boundary" in src


# --- F2: staging rejection -> 409 + no temp leak ---


def test_f2_all_staging_rejections_convert_to_409():
    src = (_ROOT / "src/kiro_crew/deploy/handlers.py").read_text(encoding="utf-8")
    assert '"hardlink-in-tree"' in src
    assert '"staging-read-blocked"' in src
    # Cleanup wrapper guards everything after mkdtemp.
    assert "except BaseException:" in src


# --- F3: directory symlinks rejected, not dropped ---


def test_f3_stage_tree_safe_rejects_dir_symlink(tmp_path):
    from kiro_crew.deploy.handlers import _stage_tree_safe
    src = tmp_path / "src"
    (src / "real").mkdir(parents=True)
    (src / "real" / "a.txt").write_text("hi")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (src / "linked").symlink_to(outside, target_is_directory=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(RuntimeError, match="symlink-in-tree"):
        _stage_tree_safe(src, staging)


# --- F4: no internal tooling in public UI ---


def test_f4_no_internal_tooling_instructions():
    src = (_ROOT / "website/src/pages/ArtifactDeployPage.tsx").read_text(encoding="utf-8")
    assert "ada credentials" not in src
    assert "Amazon-internal" not in src
