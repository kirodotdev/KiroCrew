"""An app update keeps the user's own edits in the app's materialized agent configs.

Every update (both branches of the update route, a registry install over an installed
app) keeps ``<agents dir>/<app>--<agent>.json`` through its take-down and the
registration rewrites each file in place, so one it cannot rewrite keeps its last good
copy. The user's edits (``model``, ``description``, ``welcomeMessage``...) survive, a
framework-owned key still refreshes from the new template, and the failed-update
restore keeps the edits too.
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
    @pytest.mark.parametrize(
        ("path", "body", "status"),
        [
            (f"/api/apps/{APP}/update", {}, 200),
            ("/api/apps/registry/install", {"name": APP}, 201),
            ("/api/apps/registry/install-stream", {"name": APP}, 200),
        ],
    )
    async def test_a_registry_update_keeps_the_users_agent_edits(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        body: dict[str, Any],
        status: int,
    ) -> None:
        v2 = _installed_with_edits(tmp_path, monkeypatch)

        async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
            await kwargs["before_replace"]()
            result = await asyncio.to_thread(update_app, v2, expected_name=name)
            return {"ok": result.ok, "name": name, "error": result.error}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(path, json=body)
            text = await resp.text()  # the stream route installs while it streams
            assert resp.status == status, text

        _assert_edits_kept(tmp_path, tools=_V2["tools"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cause", ["declined", "disk full"])
    @pytest.mark.parametrize(
        ("path", "body", "status"),
        [
            (f"/api/apps/{APP}/update", None, 200),
            (f"/api/apps/{APP}/update", {}, 200),
            ("/api/apps/registry/install", {"name": APP}, 201),
            ("/api/apps/registry/install-stream", {"name": APP}, 200),
        ],
        ids=["local", "update", "install", "install-stream"],
    )
    async def test_an_update_keeps_an_agent_file_it_cannot_rewrite(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        body: dict[str, Any] | None,
        status: int,
        cause: str,
    ) -> None:
        """A data home that does not own a shared agents directory declines the
        rewrite, and a full disk fails it: the user's file must still be there.
        A ``None`` body is the update route's local-source branch."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.apps import bridges

        v2 = _installed_with_edits(tmp_path, monkeypatch)
        before = _agent_file(tmp_path).read_bytes()

        async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
            await kwargs["before_replace"]()
            result = await asyncio.to_thread(update_app, v2, expected_name=name)
            return {"ok": result.ok, "name": name, "error": result.error}

        def _no_space(*_a: Any, **_k: Any) -> None:
            raise OSError(28, "No space left on device")

        if body is None:
            body = {"source": str(v2)}
        else:
            monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
            monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
            monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)
        if cause == "declined":
            monkeypatch.setattr(agent_mod, "_declined_foreign_spec_write", lambda p: True)
        else:
            monkeypatch.setattr(bridges, "atomic_write", _no_space)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(path, json=body)
            text = await resp.text()
            assert resp.status == status, text

        assert _agent_file(tmp_path).read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "body", "ok", "status"),
        [
            (f"/api/apps/{APP}/update", None, True, 200),
            (f"/api/apps/{APP}/update", {}, True, 200),
            (f"/api/apps/{APP}/update", {}, False, 400),
            ("/api/apps/registry/install", {"name": APP}, True, 201),
            ("/api/apps/registry/install", {"name": APP}, False, 400),
            ("/api/apps/registry/install-stream", {"name": APP}, True, 200),
        ],
    )
    async def test_an_update_that_needs_consent_leaves_no_agent_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        body: dict[str, Any] | None,
        ok: bool,
        status: int,
    ) -> None:
        """A new ``sessionApproval`` leaves the app disabled, so the agent files the
        take-down kept must go, on success and on a failure after the copy alike.
        A ``None`` body is the update route's local-source branch."""
        v2 = _installed_with_edits(tmp_path, monkeypatch)
        manifest_path = v2 / APP_MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["permissions"] = {"sessionApproval": True}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
            await kwargs["before_replace"]()
            result = await asyncio.to_thread(update_app, v2, expected_name=name)
            assert result.ok, result.error
            return {"ok": ok, "name": name, "error": "" if ok else "provenance write failed"}

        if body is None:
            body = {"source": str(v2)}
        else:
            monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
            monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
            monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(path, json=body)
            text = await resp.text()
            assert resp.status == status, text

        assert not _agent_file(tmp_path).exists()

    @pytest.mark.asyncio
    async def test_a_failed_registry_update_restores_the_users_agent_edits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed_with_edits(tmp_path, monkeypatch)

        async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
            await kwargs["before_replace"]()
            return {"ok": False, "name": name, "error": "copy failed"}

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
    @pytest.mark.parametrize("registry", [False, True])
    async def test_an_agent_the_new_version_drops_does_not_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: bool
    ) -> None:
        _installed_with_edits(tmp_path, monkeypatch)
        v2 = _source(tmp_path, "2.0.0", {**_V2, "name": "renamed"})
        body: dict[str, Any] = {"source": str(v2)}
        if registry:
            # The registry take-down keeps the agent files, so the dropped one has to
            # be pruned after the install.

            async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
                await kwargs["before_replace"]()
                result = await asyncio.to_thread(update_app, v2, expected_name=name)
                return {"ok": result.ok, "name": name, "error": result.error}

            monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
            monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
            monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)
            body = {}

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json=body)
            assert resp.status == 200, await resp.text()

        assert not _agent_file(tmp_path).exists()
        renamed = json.loads(
            (tmp_path / "kiro-agents" / f"{APP}--renamed.json").read_text(encoding="utf-8")
        )
        # Edits are read from the agent's own materialized file, so another agent's
        # edits are never grafted onto the new one.
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
