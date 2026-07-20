"""Tests for deploy Round 17 fixes.

F1: teardown manifest expiry is fail-closed -- no fresh-write fallback, and
    the identity check requires BOTH distribution_ids nonempty AND equal.
F2: (asserted in test_deploy_round15_fixes) early live-path digest read gone.
F3: webapp_metadata.lifecycle.status type-checked before enum membership.
F4: confirm binds previewed identity -- expected_profile/expected_region
    drift -> 409 stale_preview.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "kiro_crew"
HANDLERS = (SRC / "deploy" / "handlers.py").read_text()
PUBLISHHUB = (
    SRC.parent.parent / "website" / "src" / "components" / "PublishHub.tsx"
).read_text()


class TestF1FailClosedManifestExpiry:
    def test_no_fresh_write_fallback(self):
        # The old fallback synthesized a fresh manifest when none was readable,
        # letting a forged slug direct the reaper at another deployment.
        assert "fresh-write fallback" not in HANDLERS
        assert "no existing manifest readable (fail closed)" in HANDLERS

    def test_identity_requires_both_nonempty(self):
        assert ("if not meta_dist_id or not manifest_dist_id "
                "or meta_dist_id != manifest_dist_id:") in HANDLERS

    def test_missing_metadata_id_refuses(self):
        """Artifact metadata without distribution_id must NOT expire a manifest."""
        from types import SimpleNamespace

        from kiro_crew.deploy import engine, handlers
        from kiro_crew.deploy import profiles as profiles_mod

        lifecycle = SimpleNamespace(created_at="2026-01-01T00:00:00Z",
                                    expires_at=None, persistent=False, status="live")
        meta = SimpleNamespace(
            deploy_target=SimpleNamespace(profile="p1", region="us-west-2",
                                          distribution_id=""),  # missing
            slug="x", lifecycle=lifecycle,
        )
        art = SimpleNamespace(webapp_metadata=meta, slug="victim", kind="webapp")

        def _fake_run_aws(cmd, profile, timeout):
            import json as _j
            if "describe-stacks" in cmd:
                return (0, _j.dumps(
                    [{"OutputKey": "BucketName", "OutputValue": "b"}]), "")
            if "s3" in cmd and "cp" in cmd and "-" in cmd:
                return (0, _j.dumps({"slug": "victim",
                                     "distribution_id": "EVICTIM"}), "")
            return (0, "", "")

        import unittest.mock as _m
        reg = {"profiles": [{"name": "p1", "region": "us-west-2"}], "default": "p1"}
        with _m.patch.object(engine, "run_aws", _fake_run_aws), \
             _m.patch.object(profiles_mod, "load_registry", lambda: reg):
            import asyncio
            result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
                handlers._expire_manifest_best_effort(art))
        assert result == "unreachable"


class TestF3StatusTypeCheck:
    def test_unhashable_status_is_400_not_500(self):
        from kiro_crew.validation import ValidationError, _validate_webapp_metadata_shape
        with pytest.raises(ValidationError) as ei:
            _validate_webapp_metadata_shape({"lifecycle": {"status": ["live"]}})
        assert "must be a string" in str(ei.value)


class TestF4IdentityBoundConfirm:
    def test_backend_rejects_profile_and_region_drift(self):
        assert 'params.get("expected_profile"' in HANDLERS
        assert 'params.get("expected_region"' in HANDLERS
        assert HANDLERS.count('"code": "stale_preview"') >= 3

    def test_frontend_sends_previewed_identity(self):
        assert "expected_profile" in PUBLISHHUB
        assert "expected_region" in PUBLISHHUB
        assert "setPreviewIdentity" in PUBLISHHUB
