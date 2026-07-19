"""Tests for deploy Round 8 fixes: F1 (staleness checks), F2 (redaction), F3 (OAC ETag)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.deploy import engine, handlers, pending

SCRIPTS_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "kiro_crew"
    / "deploy"
    / "skills"
    / "artifact-deploy"
    / "scripts"
)


def _load_script(name: str):
    """Import a script as a module via importlib (standalone-execution path)."""
    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(handlers, "CONFIG_PATH", cfg)
    monkeypatch.setattr(handlers, "DATA_DIR", tmp_path)
    # Isolate pending store
    store_path = tmp_path / "deploy" / "pending-deploys.json"
    monkeypatch.setattr(pending, "_store_path", lambda: store_path)
    return cfg


@pytest.fixture(autouse=True)
def _mock_base_stack(monkeypatch):
    _base_outputs = json.dumps([{"OutputKey": "BucketName", "OutputValue": "kirocrew-base-bucket"}])
    monkeypatch.setattr(engine, "run_aws", lambda *a, **kw: (0, _base_outputs, ""))


def _run(coro):
    return asyncio.run(coro)


class _NonRestrictedState:
    _restricted_keys: set = set()
    _slots: dict = {}


class _FakeReq:
    def __init__(self, *, match_info=None):
        self.headers = {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": _NonRestrictedState()}
        self.match_info = match_info or {}


# ═══════════════════════════════════════════════════════════════════════════════
# F1: staleness checks at confirm time
# ═══════════════════════════════════════════════════════════════════════════════


class TestF1ProfileStaleness:
    """Confirm rejects if the stored profile no longer resolves to the same values."""

    def test_confirm_rejects_changed_profile(self, tmp_path, monkeypatch):
        """If profile resolution changes between preview and confirm, 409."""
        entry = pending.add_pending({
            "site_id": "test-site",
            "local_dir": "",
            "artifact_slug": "test-slug",
            "profile": "my-profile",
            "region": "us-east-1",
            "ttl_hours": 72,
            "scan_summary": "clean",
            "content_digest": "",
        })
        entry_id = entry["id"]

        async def _changed_profile(params):
            return ("my-profile", "eu-west-1")  # region changed

        monkeypatch.setattr(handlers, "_resolve_profile", _changed_profile)

        resp = _run(handlers._handle_pending_confirm(_FakeReq(match_info={"id": entry_id})))
        assert resp.status == 409
        body = json.loads(resp.body)
        assert "profile changed since preview" in body["error"]

        # Entry should be re-added (not lost)
        remaining = pending.list_pending()
        assert any(e["id"] == entry_id for e in remaining)

    def test_confirm_rejects_changed_content(self, tmp_path, monkeypatch):
        """If local_dir content changes between preview and confirm, 409."""
        src_dir = tmp_path / "site"
        src_dir.mkdir()
        (src_dir / "index.html").write_text("<h1>Hello</h1>")

        original_digest = handlers._compute_content_digest(str(src_dir))

        entry = pending.add_pending({
            "site_id": "test-site",
            "local_dir": str(src_dir),
            "artifact_slug": "",
            "profile": "my-profile",
            "region": "us-east-1",
            "ttl_hours": 72,
            "scan_summary": "clean",
            "content_digest": original_digest,
        })
        entry_id = entry["id"]

        # Modify the content after preview
        (src_dir / "index.html").write_text("<h1>Changed!</h1>")

        async def _same_profile(params):
            return ("my-profile", "us-east-1")

        monkeypatch.setattr(handlers, "_resolve_profile", _same_profile)
        # R9: confirm re-confines local_dir against allowed roots before the
        # digest check — tmp_path must be an allowed root for this test to
        # exercise the content-drift rejection (not the confinement rejection).
        monkeypatch.setattr(handlers, "_allowed_local_roots", lambda: [tmp_path.resolve()])

        resp = _run(handlers._handle_pending_confirm(_FakeReq(match_info={"id": entry_id})))
        assert resp.status == 409
        body = json.loads(resp.body)
        assert "content changed since preview" in body["error"]

        remaining = pending.list_pending()
        assert any(e["id"] == entry_id for e in remaining)


class TestF1ContentDigest:
    """_compute_content_digest produces stable, deterministic digests."""

    def test_same_content_same_digest(self, tmp_path):
        d = tmp_path / "site"
        d.mkdir()
        (d / "a.html").write_text("hello")
        (d / "b.css").write_text("body{}")

        d1 = handlers._compute_content_digest(str(d))
        d2 = handlers._compute_content_digest(str(d))
        assert d1 == d2
        assert len(d1) == 64  # sha256 hex

    def test_different_content_different_digest(self, tmp_path):
        d = tmp_path / "site"
        d.mkdir()
        (d / "a.html").write_text("hello")

        d1 = handlers._compute_content_digest(str(d))
        (d / "a.html").write_text("world")
        d2 = handlers._compute_content_digest(str(d))
        assert d1 != d2


# ═══════════════════════════════════════════════════════════════════════════════
# F2: pending list redaction
# ═══════════════════════════════════════════════════════════════════════════════


class TestF2PendingRedaction:
    """GET /api/deploy/pending redacts credential strings in entries."""

    def test_pending_list_redacts_akia_in_scan_summary(self, tmp_path):
        """A fake AKIA key in scan_summary must be redacted in the response."""
        pending.add_pending({
            "site_id": "test-site",
            "local_dir": "/home/user/project",
            "artifact_slug": "",
            "profile": "my-profile",
            "region": "us-east-1",
            "ttl_hours": 72,
            "scan_summary": "Found key: AKIAIOSFODNN7EXAMPLE in config.js",
            "content_digest": "",
        })

        resp = _run(handlers._handle_pending_list(_FakeReq()))
        assert resp.status == 200
        body = json.loads(resp.body)
        entries = body["pending"]
        assert len(entries) == 1
        summary = entries[0]["scan_summary"]
        assert "AKIAIOSFODNN7EXAMPLE" not in summary

    def test_pending_list_redacts_in_site_id(self, tmp_path):
        """Credential patterns embedded in any field are redacted."""
        pending.add_pending({
            "site_id": "site-AKIAIOSFODNN7EXAMPLE",
            "local_dir": "",
            "artifact_slug": "",
            "profile": "",
            "region": "us-east-1",
            "ttl_hours": 72,
            "scan_summary": "clean",
            "content_digest": "",
        })

        resp = _run(handlers._handle_pending_list(_FakeReq()))
        body = json.loads(resp.body)
        site_id_val = body["pending"][0]["site_id"]
        assert "AKIAIOSFODNN7EXAMPLE" not in site_id_val


# ═══════════════════════════════════════════════════════════════════════════════
# F3: OAC delete with IfMatch ETag
# ═══════════════════════════════════════════════════════════════════════════════


class TestF3OACDeleteETag:
    """Reaper Lambda must call get_origin_access_control then pass IfMatch."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import sys

        mock_boto3 = MagicMock()
        mock_botocore = MagicMock()
        mock_botocore_exceptions = MagicMock()

        class MockClientError(Exception):
            def __init__(self, error_response, operation_name):
                self.response = error_response
                self.operation_name = operation_name
                super().__init__(f"{operation_name}: {error_response}")

        mock_botocore_exceptions.ClientError = MockClientError
        mock_botocore.exceptions = mock_botocore_exceptions

        with patch.dict(os.environ, {"BUCKET": "test-bucket", "DIST_ID": "E123TEST"}), \
             patch.dict(sys.modules, {
                 "boto3": mock_boto3,
                 "botocore": mock_botocore,
                 "botocore.exceptions": mock_botocore_exceptions,
             }):
            self.mod = _load_script("reaper_lambda/index.py")
            self.ClientError = MockClientError

    def test_oac_pending_path_uses_etag(self):
        """The oac_pending path fetches ETag before delete."""
        mock_cf = MagicMock()
        # R18 F3: reaper now verifies the OAC Name before deleting a
        # manifest-supplied oac_id -- the mock must return a matching name.
        mock_cf.get_origin_access_control = MagicMock(return_value={
            "ETag": "ETAG1",
            "OriginAccessControl": {"OriginAccessControlConfig": {
                "Name": "deploy-web-test-app-abc123"}},
        })
        mock_cf.get_origin_access_control.return_value = {
            "ETag": "ETAG123",
            # R18 F3: Name must match the deployment pattern or delete is skipped.
            "OriginAccessControl": {"OriginAccessControlConfig": {
                "Name": "deploy-web-test-slug-abc123"}},
        }
        mock_cf.delete_origin_access_control.return_value = {}

        mock_s3 = MagicMock()
        mock_s3.delete_object.return_value = {}

        self.mod.cf = mock_cf
        self.mod.s3 = mock_s3

        result = self.mod._reap_engine_arch(
            {"oac_pending": True, "oac_id": "OAC-ABC", "arch": "engine"},
            "test-slug",
            int(time.time()),
        )

        assert result == "reaped"
        mock_cf.get_origin_access_control.assert_called_once_with(Id="OAC-ABC")
        mock_cf.delete_origin_access_control.assert_called_once_with(
            Id="OAC-ABC", IfMatch="ETAG123"
        )

    def test_oac_not_found_on_get_treated_as_success(self):
        """NoSuchOriginAccessControl on get is treated as already-gone."""
        mock_cf = MagicMock()
        # R18 F3: reaper now verifies the OAC Name before deleting a
        # manifest-supplied oac_id -- the mock must return a matching name.
        mock_cf.get_origin_access_control = MagicMock(return_value={
            "ETag": "ETAG1",
            "OriginAccessControl": {"OriginAccessControlConfig": {
                "Name": "deploy-web-test-app-abc123"}},
        })
        mock_cf.get_origin_access_control.side_effect = self.ClientError(
            {"Error": {"Code": "NoSuchOriginAccessControl", "Message": ""}},
            "GetOriginAccessControl",
        )

        mock_s3 = MagicMock()
        mock_s3.delete_object.return_value = {}

        self.mod.cf = mock_cf
        self.mod.s3 = mock_s3

        result = self.mod._reap_engine_arch(
            {"oac_pending": True, "oac_id": "OAC-GONE", "arch": "engine"},
            "test-slug",
            int(time.time()),
        )

        assert result == "reaped"
        mock_cf.delete_origin_access_control.assert_not_called()

    def test_main_reap_path_uses_etag(self):
        """The main engine reap path (non oac_pending) also uses IfMatch."""
        mock_cf = MagicMock()
        # R18 F3: reaper now verifies the OAC Name before deleting a
        # manifest-supplied oac_id -- the mock must return a matching name.
        mock_cf.get_origin_access_control = MagicMock(return_value={
            "ETag": "ETAG1",
            "OriginAccessControl": {"OriginAccessControlConfig": {
                "Name": "deploy-web-test-app-abc123"}},
        })
        # Distribution lookup fails -> already gone
        mock_cf.get_distribution_config.side_effect = self.ClientError(
            {"Error": {"Code": "NoSuchDistribution", "Message": ""}},
            "GetDistributionConfig",
        )
        # R19 F4: the tag check runs BEFORE destructive ops. For a gone
        # distribution, list_tags_for_resource also raises NoSuchDistribution,
        # which the tag gate treats as non-fatal (falls through to the
        # gone-handling path below).
        mock_cf.list_tags_for_resource.side_effect = self.ClientError(
            {"Error": {"Code": "NoSuchDistribution", "Message": ""}},
            "ListTagsForResource",
        )
        # OAC listing returns a match
        mock_cf.list_origin_access_controls.return_value = {
            "OriginAccessControlList": {"Items": [
                {"Id": "OAC-FOUND", "Name": "deploy-web-test-slug-abc123"}
            ]}
        }
        mock_cf.get_origin_access_control.return_value = {
            "ETag": "ETAG-MAIN",
            # R18 F3: Name must match or delete is skipped.
            "OriginAccessControl": {"OriginAccessControlConfig": {
                "Name": "deploy-web-test-slug-abc123"}},
        }
        mock_cf.delete_origin_access_control.return_value = {}

        mock_s3 = MagicMock()
        mock_s3.delete_object.return_value = {}
        # Empty paginator for bucket cleanup
        paginator = MagicMock()
        paginator.paginate.return_value = []
        mock_s3.get_paginator.return_value = paginator

        self.mod.cf = mock_cf
        self.mod.s3 = mock_s3

        result = self.mod._reap_engine_arch(
            {"arch": "engine", "slug": "test-slug", "distribution_id": "E2TESTDISTX123",
             "bucket": ""},
            "test-slug",
            int(time.time()),
        )

        assert result == "reaped"
        mock_cf.get_origin_access_control.assert_called_once_with(Id="OAC-FOUND")
        mock_cf.delete_origin_access_control.assert_called_once_with(
            Id="OAC-FOUND", IfMatch="ETAG-MAIN"
        )


class TestR10ArtifactStaleness:
    """R10 F3: artifact_slug deploys get the same digest staleness check."""

    def test_confirm_rejects_changed_artifact(self, monkeypatch):
        """If the artifact is edited between preview and confirm, 409."""
        import types

        from kiro_crew.deploy import handlers, pending

        class _Art:
            kind = "html"
            content = "<h1>v1</h1>"
            name = "site"

        # Digest of the ORIGINAL staged content, stored at preview time.
        _f, staged, _b = handlers._stage_artifact_html(
            _Art.kind, _Art.content, _Art.name)
        original_digest = handlers._compute_content_digest(staged)
        import shutil as _sh
        _sh.rmtree(staged, ignore_errors=True)

        entry = pending.add_pending({
            "site_id": "art-site",
            "artifact_slug": "my-art",
            "local_dir": "",
            "profile": "my-profile",
            "region": "us-east-1",
            "ttl_hours": 72,
            "scan_summary": "clean",
            "content_digest": original_digest,
        })
        entry_id = entry["id"]

        # The artifact was edited after preview.
        class _Changed(_Art):
            content = "<h1>v2 CHANGED</h1>"

        monkeypatch.setattr(handlers, "_HAS_ARTIFACTS", True)
        monkeypatch.setattr(
            handlers, "get_default_store",
            lambda: types.SimpleNamespace(get=lambda slug: _Changed()),
        )

        async def _same_profile(params):
            return ("my-profile", "us-east-1")

        monkeypatch.setattr(handlers, "_resolve_profile", _same_profile)

        resp = _run(handlers._handle_pending_confirm(
            _FakeReq(match_info={"id": entry_id})))
        assert resp.status == 409
        body = json.loads(resp.body)
        assert "content changed since preview" in body["error"]
        # Entry restored for retry after a fresh preview.
        assert any(e["id"] == entry_id for e in pending.list_pending())
