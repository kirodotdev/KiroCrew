"""An app or plugin manifest saved as UTF-8 with a byte-order mark parses.

Windows editors (Notepad's "UTF-8 with BOM", PowerShell 5.1's ``-Encoding UTF8``)
write a leading BOM and git keeps it, so ``app.json`` and a plugin manifest are
read through :func:`kiro_crew.user_json.loads_user_json`. One leading mark is
dropped; invalid JSON is still refused exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from apps_registry_coverage_helpers import _fake_sandbox, _FakeProc
from plugin_import_helpers import _package

from kiro_crew.apps import plugin_import, registry
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
