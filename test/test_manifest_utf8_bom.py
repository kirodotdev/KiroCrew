"""An app or plugin manifest saved as UTF-8 with a byte-order mark parses.

Windows editors (Notepad's "UTF-8 with BOM", PowerShell 5.1's ``-Encoding UTF8``)
write a leading BOM and git keeps it, so ``app.json`` and a plugin manifest are
read through :func:`kiro_crew.user_json.loads_user_json`. One leading mark is
dropped; invalid JSON is still refused exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from apps_registry_coverage_helpers import _fake_sandbox, _FakeProc
from plugin_import_helpers import _package
from registry_store_art_prewarm_helpers import ART, MANIFEST, _entry, _fake_fetch, _pin_registry

from kiro_crew.apps import plugin_import, registry
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.plugin_import import PluginImportError, convert_plugin_package

BOM = b"\xef\xbb\xbf"
APP_MANIFEST = {"name": "bomapp", "version": "1.0.0", "platform": {"os": ["macos", "linux"]}}


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _local_checkout(monkeypatch, tmp_path: Path, body: bytes) -> None:
    src = tmp_path / "app-sources" / "bomapp"
    _write(src / "app.json", body)
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

    async def _true(*a, **k):
        return True

    monkeypatch.setattr(registry, "_clone_origin_matches", _true)
    monkeypatch.setattr(registry, "_clone_branch_matches", _true)


def _throwaway_clone(monkeypatch, tmp_path: Path, body: bytes) -> None:
    import tempfile

    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: str(clone))
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    _write(clone / "app.json", body)
    _fake_sandbox(monkeypatch, [_FakeProc(returncode=0)])


class TestAppManifestBom:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", [BOM, b""], ids=["bom", "no-bom"])
    async def test_local_fast_path_reads_the_manifest(self, monkeypatch, tmp_path, prefix):
        _local_checkout(monkeypatch, tmp_path, prefix + json.dumps(APP_MANIFEST).encode())
        got = await registry._fetch_app_manifest(
            "o/bomapp",
            "main",
            app_name="bomapp",
            git_url="https://github.com/o/bomapp.git",
            allow_remote=False,
        )
        assert got == APP_MANIFEST

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", [BOM, b""], ids=["bom", "no-bom"])
    async def test_throwaway_clone_reads_the_manifest(self, monkeypatch, tmp_path, prefix):
        _throwaway_clone(monkeypatch, tmp_path, prefix + json.dumps(APP_MANIFEST).encode())
        got = await registry._fetch_app_manifest(
            "o/bomapp", "main", git_url="https://github.com/o/bomapp.git"
        )
        assert got == APP_MANIFEST

    @pytest.mark.asyncio
    async def test_invalid_json_after_a_bom_still_reads_as_none(self, monkeypatch, tmp_path):
        _throwaway_clone(monkeypatch, tmp_path, BOM + b"not json")
        got = await registry._fetch_app_manifest(
            "o/bomapp", "main", git_url="https://github.com/o/bomapp.git"
        )
        assert got is None


class TestPluginManifestBom:
    @pytest.mark.parametrize("prefix", [BOM, b""], ids=["bom", "no-bom"])
    def test_read_manifest_name(self, tmp_path, prefix):
        path = tmp_path / "plugin.json"
        _write(path, prefix + b'{"name":"bom-plugin"}')
        assert plugin_import.read_manifest_name(path) == "bom-plugin"

    def test_invalid_json_after_a_bom_is_still_not_json(self, tmp_path):
        path = tmp_path / "plugin.json"
        _write(path, BOM + b"{not json")
        with pytest.raises(PluginImportError) as exc:
            plugin_import.read_manifest_name(path)
        assert exc.value.code == "manifest_not_json"

    @pytest.mark.parametrize("prefix", [BOM, b""], ids=["bom", "no-bom"])
    def test_is_schema_qualified(self, tmp_path, prefix):
        path = tmp_path / "plugin.json"
        doc = {"$schema": plugin_import.SCHEMA_NAMESPACE_PREFIX + "v1", "name": "x"}
        _write(path, prefix + json.dumps(doc).encode())
        assert plugin_import._is_schema_qualified(path) is True

    def test_convert_plugin_package_accepts_a_bom_manifest(self, tmp_path):
        root = _package(tmp_path, name="bom-plugin")
        manifest = root / ".codex-plugin" / "plugin.json"
        manifest.write_bytes(BOM + manifest.read_bytes())
        report = convert_plugin_package(root, tmp_path / "out")
        assert json.loads((tmp_path / "out" / "app.json").read_text("utf-8"))["name"] == (
            "bom-plugin"
        )
        assert report is not None


class TestInstalledManifestBom:
    @pytest.mark.parametrize("prefix", [BOM, b""], ids=["bom", "no-bom"])
    def test_from_json_file(self, tmp_path, prefix):
        path = tmp_path / "app.json"
        _write(path, prefix + json.dumps(APP_MANIFEST).encode())
        assert AppManifest.from_json_file(path).name == "bomapp"

    def test_from_json_file_still_refuses_invalid_json_after_a_bom(self, tmp_path):
        path = tmp_path / "app.json"
        _write(path, BOM + b"{not json")
        with pytest.raises(ValueError):
            AppManifest.from_json_file(path)


class TestInstallIdentityGatesBom:
    """The identity gates read the cloned ``app.json``; a BOM is not a mismatch."""

    @pytest.fixture(autouse=True)
    def _admitted(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)
        monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install", lambda name: None
        )
        monkeypatch.setattr(registry, "sel", lambda: MagicMock())

    @pytest.mark.asyncio
    async def test_pre_build_gate_admits_a_bom_manifest(self, monkeypatch, tmp_path):
        built: list = []

        async def _fake_build(build_dir, app_name, log_lines, **kwargs):
            built.append(app_name)
            return {"ok": True}

        async def _fake_clone(git_url, branch, pkg_dir, log_lines, **kwargs):
            _write(pkg_dir / "app.json", BOM + json.dumps({"name": "bomapp"}).encode())
            return None

        monkeypatch.setattr(registry, "_run_app_build", _fake_build)
        monkeypatch.setattr(registry, "_git_clone_or_pull", _fake_clone)
        monkeypatch.setattr(registry, "app_source_dir", lambda name: tmp_path / name)

        result = await registry._clone_build_app_locked(
            "https://example.invalid/r.git", "bomapp", [], pending_cleanup=[]
        )

        assert built == ["bomapp"], result

    @pytest.mark.asyncio
    async def test_post_build_gate_admits_a_bom_manifest(self, monkeypatch, tmp_path):
        url = "https://github.com/o/bomapp.git"
        entry = {"name": "bomapp", "gitUrl": url, "repo": url, "branch": "main"}
        monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
        manifest = {**APP_MANIFEST, "displayName": "Bom App", "description": "d", "author": "t"}
        monkeypatch.setattr(registry, "_fetch_app_manifest", AsyncMock(return_value=manifest))
        clone = tmp_path / "clone"
        _write(clone / "app.json", BOM + json.dumps(manifest).encode())

        async def _fake_clone_build(git_url, app_name, log_lines, branch="main", **kwargs):
            return {"ok": True, "pkg_dir": clone}

        monkeypatch.setattr(registry, "_clone_build_app", _fake_clone_build)

        result = await registry.install_from_registry("bomapp")

        assert result["ok"] is True, result.get("error")


class TestStoreArtPrewarmBom:
    @pytest.mark.asyncio
    async def test_prewarm_caches_a_bom_manifest(self, monkeypatch):
        reg = _pin_registry(monkeypatch, registry._TRUST_OWNER)
        _fake_fetch(
            monkeypatch, {"app.json": BOM + json.dumps(MANIFEST).encode(), **ART}, manifest=None
        )
        entry = _entry()

        assert await registry._prewarm_owner_tier_store_assets(reg, [entry]) == 1
        assert registry._read_manifest_cache(entry) == MANIFEST
