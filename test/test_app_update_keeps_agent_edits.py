"""An app update keeps the user's own edits in the app's materialized agent configs.

Both update branches of ``POST /api/apps/{name}/update`` deregister the app before they
re-register it, and deregistering deletes ``<agents dir>/<app>--<agent>.json``. The
registration that follows can only carry a user's edits (``model``, ``description``,
``welcomeMessage``...) across the refresh if it is handed what was on disk BEFORE that
delete, so the route snapshots the files first. A framework-owned key still refreshes
from the new template, and the failed-update restore keeps the edits too.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, enable_app, install_app, update_app
from kiro_crew.apps.routes import register_app


def _source(root: Path, version: str, template: dict[str, Any]) -> Path:
    src = root / f"source-{version}" / APP
    (src / "agents").mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Coverage Test App",
        "description": "App for update tests",
        "author": "tester",
        "agents": ["agents/helper.json"],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    (src / "agents" / "helper.json").write_text(json.dumps(template), encoding="utf-8")
    return src


_V1 = {"name": "helper", "model": "auto", "description": "v1 text", "tools": ["read"]}
_V2 = {"name": "helper", "model": "auto", "description": "v2 text", "tools": ["read", "write"]}
_USER_EDITS = {"model": "my-own-model", "description": "mine", "welcomeMessage": "hi there"}


def _agent_file(tmp_path: Path) -> Path:
    return tmp_path / "kiro-agents" / f"{APP}--helper.json"


def _installed_with_edits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install v1, enable and register it, then hand-edit the materialized config."""
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
    assert install_app(_source(tmp_path, "1.0.0", _V1)).ok
    assert enable_app(APP).ok
    register_app(APP)
    path = _agent_file(tmp_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(_USER_EDITS)
    config["tools"] = ["user-picked"]  # framework-owned: the update must refresh it
    path.write_text(json.dumps(config), encoding="utf-8")
    return _source(tmp_path, "1.1.0", _V2)


def _assert_edits_kept(tmp_path: Path, *, tools: list[str]) -> None:
    after = json.loads(_agent_file(tmp_path).read_text(encoding="utf-8"))
    for key, value in _USER_EDITS.items():
        assert after.get(key) == value, (key, after)
    assert after["tools"] == tools


class TestUpdateKeepsAgentEdits:
    @pytest.mark.asyncio
    async def test_a_local_update_keeps_the_users_agent_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        v2 = _installed_with_edits(tmp_path, monkeypatch)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={"source": str(v2)})
            assert resp.status == 200, await resp.text()

        _assert_edits_kept(tmp_path, tools=_V2["tools"])

    @pytest.mark.asyncio
    async def test_a_registry_update_keeps_the_users_agent_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        v2 = _installed_with_edits(tmp_path, monkeypatch)

        async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
            result = await asyncio.to_thread(update_app, v2, expected_name=name)
            return {"ok": result.ok, "name": name, "error": result.error}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200, await resp.text()

        _assert_edits_kept(tmp_path, tools=_V2["tools"])

    @pytest.mark.asyncio
    async def test_a_failed_registry_update_restores_the_users_agent_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed_with_edits(tmp_path, monkeypatch)

        async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "name": name, "error": "clone failed"}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _failed_install)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400

        _assert_edits_kept(tmp_path, tools=_V1["tools"])

    @pytest.mark.asyncio
    async def test_a_failed_local_update_restores_the_users_agent_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed_with_edits(tmp_path, monkeypatch)
        missing = tmp_path / "no-such-source"

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={"source": str(missing)})
            assert resp.status == 400

        _assert_edits_kept(tmp_path, tools=_V1["tools"])

    @pytest.mark.asyncio
    async def test_an_agent_the_new_version_drops_does_not_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed_with_edits(tmp_path, monkeypatch)
        v2 = _source(tmp_path, "2.0.0", {**_V2, "name": "renamed"})

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={"source": str(v2)})
            assert resp.status == 200, await resp.text()

        assert not _agent_file(tmp_path).exists()
        renamed = json.loads(
            (tmp_path / "kiro-agents" / f"{APP}--renamed.json").read_text(encoding="utf-8")
        )
        # The snapshot is keyed by the materialized file, so another agent's edits
        # are never grafted onto the new one.
        assert renamed["model"] == "auto"
        assert renamed["description"] == "v2 text"


class TestSnapshotAppAgentConfigs:
    """``snapshot_app_agent_configs`` is imported per test so that, with the fix
    reverted, the route tests above still collect and show the edits being lost."""

    def test_reads_only_this_apps_regular_agent_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.bridges import snapshot_app_agent_configs

        _setup_env(tmp_path, monkeypatch)
        agents = tmp_path / "kiro-agents"
        (agents / f"{APP}--a.json").write_text('{"model": "m"}', encoding="utf-8")
        (agents / f"{APP}--broken.json").write_text("{", encoding="utf-8")
        (agents / f"{APP}--list.json").write_text("[]", encoding="utf-8")
        (agents / f"{APP}x--b.json").write_text('{"model": "other"}', encoding="utf-8")
        (agents / "other-app--c.json").write_text('{"model": "other"}', encoding="utf-8")
        outside = tmp_path / "outside.json"
        outside.write_text('{"model": "linked"}', encoding="utf-8")
        try:
            os.symlink(outside, agents / f"{APP}--linked.json")
        except (OSError, NotImplementedError):
            pass  # no symlink privilege (Windows): the regular-file cases still run

        assert snapshot_app_agent_configs(APP) == {f"{APP}--a.json": {"model": "m"}}

    def test_a_missing_agents_dir_is_an_empty_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.bridges import snapshot_app_agent_configs

        _setup_env(tmp_path, monkeypatch)
        (tmp_path / "kiro-agents").rmdir()
        assert snapshot_app_agent_configs(APP) == {}
