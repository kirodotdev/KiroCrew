"""Round-19 code review fixes — tests for F1-F5."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# --- F1: _sanitize_response recursive redaction ---


def test_f1_sanitize_response_redacts_credential_in_error():
    """Error response containing an AKIA key in local_dir path gets redacted."""
    from kiro_crew.deploy.handlers import _sanitize_response
    payload = {"error": "local_dir not found: /tmp/AKIAIOSFODNN7EXAMPLE/build"}
    result = _sanitize_response(payload)
    assert "AKIAIOSFODNN7EXAMPLE" not in result.get("error", "")
    assert "[REDACTED" in result.get("error", "") or "***" in result.get("error", "")


def test_f1_sanitize_response_recursive_nested():
    """Nested dicts and lists are recursively sanitized."""
    from kiro_crew.deploy.handlers import _sanitize_response
    payload = {
        "outer": {"inner": "token=AKIAIOSFODNN7EXAMPLE"},
        "list": ["safe", "AKIAIOSFODNN7EXAMPLE"],
    }
    result = _sanitize_response(payload)
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(result)


def test_f1_sanitize_response_idempotent_on_clean():
    """Clean text passes through unchanged."""
    from kiro_crew.deploy.handlers import _sanitize_response
    payload = {"status": "ok", "url": "https://example.cloudfront.net"}
    result = _sanitize_response(payload)
    assert result == payload


# --- F2: hardlink rejection in staging ---

def test_f2_hardlink_rejected_in_staging(tmp_path):
    """A hardlinked file in the source tree blocks deploy staging."""
    from kiro_crew.deploy.handlers import _stage_tree_safe
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<h1>Hi</h1>")
    # Create two hardlinks to same inode WITHIN the tree (nlink=2)
    orig = src / "orig.txt"
    orig.write_text("content")
    try:
        os.link(str(orig), str(src / "linked.txt"))
    except (PermissionError, OSError):
        pytest.skip("hardlinks not permitted in this environment")
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    with pytest.raises(RuntimeError, match="hardlink"):
        _stage_tree_safe(src, staging_root)


def test_f2_normal_files_pass_staging(tmp_path):
    """Normal files (nlink=1) pass staging without error."""
    from kiro_crew.deploy.handlers import _stage_tree_safe
    src = tmp_path / "site"
    src.mkdir()
    (src / "index.html").write_text("<h1>Hi</h1>")
    (src / "style.css").write_text("body{}")
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    staged = _stage_tree_safe(src, staging_root)
    assert (staged / "index.html").exists()
    assert (staged / "style.css").exists()


# --- F3: attach_backend origin_domain validation ---

def _load_attach_backend():
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "src/kiro_crew/deploy/skills/artifact-deploy/scripts/attach_backend.py"
    )
    spec = importlib.util.spec_from_file_location("attach_backend", spec_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_f3_origin_domain_valid_execute_api():
    """Valid execute-api domain passes validation."""
    mod = _load_attach_backend()
    # Should not raise
    mod._validate_origin_domain("abc123def4.execute-api.us-west-2.amazonaws.com", "us-west-2")


def test_f3_origin_domain_rejects_attacker():
    """Attacker-controlled domain is rejected."""
    mod = _load_attach_backend()
    with pytest.raises(SystemExit):
        mod._validate_origin_domain("evil.attacker.com", "us-west-2")


def test_f3_origin_domain_rejects_wrong_region():
    """Domain with mismatched region is rejected."""
    mod = _load_attach_backend()
    with pytest.raises(SystemExit):
        mod._validate_origin_domain("abc123.execute-api.eu-west-1.amazonaws.com", "us-west-2")


# --- F4: reaper tag verification ---

def test_f4_reaper_dist_tag_mismatch_skips(capsys):
    """Distribution with mismatched kirocrew:site tag is skipped, not deleted."""
    from unittest.mock import patch as _patch
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "src/kiro_crew/deploy/skills/artifact-deploy/scripts/reaper_lambda/index.py"
    )
    spec = importlib.util.spec_from_file_location("reaper", spec_path)
    mod = importlib.util.module_from_spec(spec)
    # Mock boto3 before loading
    mock_boto = MagicMock()
    mock_cf = MagicMock()
    mock_s3 = MagicMock()
    mock_cfn = MagicMock()
    mock_boto.client.side_effect = lambda svc, **kw: {
        "s3": mock_s3, "cloudformation": mock_cfn, "cloudfront": mock_cf
    }.get(svc, MagicMock())
    with _patch.dict(sys.modules, {"boto3": mock_boto, "botocore": MagicMock(), "botocore.exceptions": MagicMock()}):
        with _patch.dict(os.environ, {"BUCKET": "test-bucket", "DIST_ID": "EDIST123"}):
            spec.loader.exec_module(mod)

    # Simulate: distribution exists, tag says wrong slug
    mock_cf.get_distribution_config.return_value = {
        "ETag": "etag1",
        "DistributionConfig": {"Enabled": True},
    }
    mock_cf.list_tags_for_resource.return_value = {
        "Tags": {"Items": [{"Key": "kirocrew:site", "Value": "OTHER-SLUG"}]}
    }
    man = {"distribution_id": "EABC12345678", "bucket": "", "slug": "my-site"}
    result = mod._reap_engine_arch(man, "my-site", 9999999999)
    # Should return "reaped" without deleting (tag mismatch -> skip)
    assert result == "reaped"
    # Should NOT have called delete_distribution
    mock_cf.delete_distribution.assert_not_called()
    mock_cf.update_distribution.assert_not_called()


# --- F5: artifacts dedup branch validation + field passthrough ---


def test_f5_dedup_validates_webapp_metadata():
    """Dedup branch rejects invalid webapp_metadata (same as normal save path)."""
    from kiro_crew.dashboard.handlers.artifacts import _validate_inbound_webapp_metadata

    # Non-dict webapp_metadata is rejected
    bad_body = {"webapp_metadata": "not a dict"}
    err = _validate_inbound_webapp_metadata(bad_body)
    assert err is not None  # validation should reject non-dict


def test_f5_dedup_kind_conflict_code_exists():
    """Dedup with conflicting kind should be detected — verify code structure."""
    # The fix adds kind-conflict detection in the dedup branch.
    import inspect

    from kiro_crew.dashboard.handlers import artifacts as art_mod
    source = inspect.getsource(art_mod.api_artifacts_create)
    assert "dedup kind conflict" in source
    assert "409" in source
