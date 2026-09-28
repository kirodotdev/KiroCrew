"""Regression tests for Round-25 findings (KiroCrew PR #6).

F1: destroy confirm binds to the previewed resource ids (BE verifies
    expected_bucket / expected_distribution_id; FE runs the two-call flow).
F2: the fullstack IAM setup UI surfaces the mandatory boundary policy.
F3: the permissions boundary covers the reaper role's full operational
    surface (boundary intersects with the role policy).
"""
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


# --- F1: destroy two-call binding ---


def test_f1_backend_verifies_expected_resource_ids():
    src = (_ROOT / "src/kiro_crew/deploy/handlers.py").read_text(encoding="utf-8")
    assert "expected_distribution_id" in src
    assert "resource ids changed since preview" in src
    # The verification happens before engine.destroy.
    seg = src.split('exp_dist = str(params.get("expected_distribution_id"', 1)[1]
    assert seg.index("engine.find_site_by_tag") < seg.index("engine.destroy")


def test_f1_frontend_two_call_destroy():
    src = (_ROOT / "website/src/pages/ArtifactDeployPage.tsx").read_text(encoding="utf-8")
    # Preview call without confirm, then confirmed call with bindings.
    assert "expected_bucket" in src
    assert "expected_distribution_id" in src
    # The single-shot pattern (confirm: true using stale list data as the
    # only call) is gone: the confirmed call carries the previewed ids.
    m = re.search(r"jsend<any>\('/destroy', \{ site_id: s\.site_id, confirm: true, profile: s\.profile \|\| '' \}\)", src)
    assert m is None


# --- F2: boundary surfaced in the setup UI ---


def test_f2_ui_surfaces_boundary_policy():
    src = (_ROOT / "website/src/pages/ArtifactDeployPage.tsx").read_text(encoding="utf-8")
    assert "boundary_policy" in src
    # The dashboard is translated, so the visible label lives in the i18n catalog
    # rather than as a literal in the TSX. Assert the component renders the
    # catalog key AND that the key resolves to the expected English text —
    # together those still prove the copy affordance is surfaced, without
    # re-breaking the moment a string moves into (or within) the catalog.
    assert "pages.artifactDeployPage.copy_boundary_policy" in src
    catalog = json.loads(
        (_ROOT / "website/src/i18n/locales/en.json").read_text(encoding="utf-8")
    )
    label = catalog["pages"]["artifactDeployPage"]["copy_boundary_policy"]
    assert label == "Copy boundary policy"


# --- F3: boundary covers the reaper role's grants ---


def test_f3_boundary_covers_reaper_role_actions():
    """Every action reaper.yaml grants must be allowed by the boundary —
    a boundary intersects with the role policy, so a gap silently breaks
    the reaper."""
    from kiro_crew.deploy import iam
    boundary = json.dumps(iam.boundary_policy_document())
    yaml_src = (
        _ROOT / "src/kiro_crew/deploy/skills/artifact-deploy/templates/"
        "reaper.yaml"
    ).read_text(encoding="utf-8")
    # Strip comment lines (action names in comments are not grants).
    yaml_src = "\n".join(
        ln for ln in yaml_src.splitlines() if not ln.strip().startswith("#"))
    granted = set(re.findall(
        r"\b((?:s3|cloudfront|cloudformation|lambda|dynamodb|iam|apigateway|sts):[A-Za-z]+)\b",
        yaml_src))
    # sts:AssumeRole is the trust policy, not an execution permission.
    granted.discard("sts:AssumeRole")
    # lambda:InvokeFunction is the AWS::Lambda::Permission granting
    # EventBridge invoke rights — a resource-based policy on the function,
    # not a permission of the role, so the boundary does not apply.
    granted.discard("lambda:InvokeFunction")
    missing = sorted(a for a in granted if a not in boundary)
    assert not missing, f"reaper actions not covered by boundary: {missing}"
