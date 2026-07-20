"""Regression tests for Round-21 findings (KiroCrew PR #6).

F1: the app-arch (CloudFormation stack) reap path verifies the stack's
    kirocrew:site identity tag before deletion, matching the R19 engine-arch
    gate; deploy-backend.sh tags stacks at creation.
F2: webapp_metadata.public_url validation rejects Basic-auth userinfo
    (https://user:pass@host) at the backend; safeHttpUrl mirrors it in the FE.
"""
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1] / (
    "src/kiro_crew/deploy/skills/artifact-deploy"
)


# --- F1: stack identity gate ---


def test_f1_deploy_backend_tags_stack_at_creation():
    src = (_SKILL / "scripts/deploy-backend.sh").read_text()
    assert '--tags "kirocrew:site=$SLUG"' in src


def test_f1_lambda_gates_every_delete_stack():
    src = (_SKILL / "scripts/reaper_lambda/index.py").read_text()
    assert "_stack_site_tag_ok" in src
    # The Phase A main path reads stack tags from describe_stacks and refuses
    # on mismatch before delete_stack.
    assert "app_reap_stack_tag_mismatch" in src
    # _del_stack (dry-run/manual path) is gated.
    fn = src.split("def _del_stack", 1)[1].split("\ndef ", 1)[0]
    assert "_stack_site_tag_ok" in fn
    # Phase B DELETE_FAILED retry is gated.
    phase_b = src.split('elif status == "DELETE_FAILED"', 1)[1][:600]
    assert "_stack_site_tag_ok" in phase_b


def test_f1_shell_reaper_gates_stack_deletion():
    src = (_SKILL / "scripts/reaper.sh").read_text()
    # Both Phase A and the Phase B DELETE_FAILED retry check the tag.
    assert src.count("kirocrew:site") >= 2
    assert "tag mismatch" in src


# --- F2: userinfo rejection ---


def _validate(url: str):
    from kiro_crew.validation import _validate_webapp_metadata_shape
    _validate_webapp_metadata_shape({"deploy_target": {"public_url": url}})


def test_f2_backend_rejects_userinfo_url():
    from kiro_crew.validation import ValidationError
    with pytest.raises(ValidationError):
        _validate("https://user:secret@attacker.example/path")
    with pytest.raises(ValidationError):
        _validate("https://user@attacker.example/")


def test_f2_backend_accepts_plain_https():
    _validate("https://d123.cloudfront.net/site/")
    _validate("")  # draft state


def test_f2_frontend_safe_http_url_rejects_userinfo():
    src = (
        Path(__file__).resolve().parents[1] / "website/src/lib/safeUrl.ts"
    ).read_text()
    assert "u.username || u.password" in src
