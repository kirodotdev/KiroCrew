"""An install publishes nothing for a disabled record; enable does.

``register_app`` never consults ``enabled``, so the install routes decide from the
live record whether an app's agents, skills, MCP servers and crons are published
and its backend started (``routes_lifecycle._app_may_run_after_install``).
``install_app`` writes a new record disabled, so a fresh install through the
registry route, its SSE stream or the local-path route must leave the agents
directory empty and start no backend, while ``GET /api/apps`` reports the app
disabled. The guarded enable transition then registers the resources.
Owning spec: app-kit-platform §7.3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _make_app, _make_app_source, _setup_env

import kiro_crew.apps.bridges as bridges_mod
import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import enable_app, install_app


def _source_with_agent(tmp_path: Path) -> Path:
    src = _make_app_source(
        tmp_path,
        agents=["agents/helper.json"],
        skills=["skills/helper-skill"],
    )
    (src / "agents").mkdir()
    (src / "agents" / "helper.json").write_text('{"name": "helper"}', encoding="utf-8")
    (src / "skills" / "helper-skill").mkdir(parents=True)
    (src / "skills" / "helper-skill" / "SKILL.md").write_text("# Helper", encoding="utf-8")
    return src


def _published_agents() -> list[str]:
    """Agent spec files in the agents directory (its lock file is not a spec)."""
    return sorted(p.name for p in Path(bridges_mod.KIRO_AGENTS_DIR).glob("*.json"))


def _fake_registry_install(src: Path) -> Any:
    """``install_from_registry`` reduced to its managed-install write."""

    async def _install(name: str, log_lines: Any = None, **kwargs: Any) -> dict[str, Any]:
        result = install_app(src)
        assert result.ok, result.error
        if log_lines is not None:
            log_lines.append("installed")
        return {"ok": True, "name": result.name}

    return _install


def _done_payload(text: str) -> dict[str, Any]:
    frames = [f for f in text.split("\n\n") if f.startswith("event: done")]
    assert frames, text
    data = [line[len("data: ") :] for line in frames[-1].split("\n") if line.startswith("data: ")]
    return json.loads("\n".join(data))


@pytest.fixture()
def started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _setup_env(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append(n))
    return calls


class TestFreshInstallPublishesNothing:
    @pytest.mark.asyncio
    async def test_registry_install_of_a_disabled_record_publishes_no_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[str]
    ) -> None:
        monkeypatch.setattr(
            routes_mod,
            "install_from_registry",
            _fake_registry_install(_source_with_agent(tmp_path)),
        )
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 201
            body = await resp.json()
            listed = await (await client.get("/api/apps")).json()

        row = next(a for a in listed if a["name"] == APP)
        assert row["enabled"] is False
        assert body["registration"]["agents"] == []
        assert body["registration"]["skills"] == []
        assert _published_agents() == []
        assert started == []

    @pytest.mark.asyncio
    async def test_streamed_registry_install_publishes_no_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[str]
    ) -> None:
        monkeypatch.setattr(
            routes_mod,
            "install_from_registry",
            _fake_registry_install(_source_with_agent(tmp_path)),
        )
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            done = _done_payload(await resp.text())

        assert done["ok"] is True
        assert done["registration"]["agents"] == []
        assert _published_agents() == []
        assert started == []

    @pytest.mark.asyncio
    async def test_local_path_install_publishes_no_agents(
        self, tmp_path: Path, started: list[str]
    ) -> None:
        src = _source_with_agent(tmp_path)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 201
            body = await resp.json()

        assert routes_mod.get_app(APP)["enabled"] is False
        assert body["registration"]["agents"] == []
        assert _published_agents() == []
        assert started == []

    @pytest.mark.asyncio
    async def test_enable_after_the_install_publishes_the_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[str]
    ) -> None:
        """The detail page's install-then-Enable flow still lands every resource."""
        monkeypatch.setattr(
            routes_mod,
            "install_from_registry",
            _fake_registry_install(_source_with_agent(tmp_path)),
        )
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            assert (
                await client.post("/api/apps/registry/install", json={"name": APP})
            ).status == 201
            assert _published_agents() == []
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200, await resp.text()
            body = await resp.json()

        assert routes_mod.get_app(APP)["enabled"] is True
        assert body["registration"]["agents"]
        assert _published_agents() != []
        assert started == [APP]


class TestEnabledRecordStillRegisters:
    @pytest.mark.asyncio
    async def test_reinstall_over_an_enabled_record_registers_and_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: list[str]
    ) -> None:
        src = _source_with_agent(tmp_path)
        assert install_app(src).ok
        assert enable_app(APP).ok

        async def _keep(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "name": name}

        registered: list[str] = []
        monkeypatch.setattr(routes_mod, "install_from_registry", _keep)
        monkeypatch.setattr(
            routes_mod,
            "register_app",
            lambda n: registered.append(n) or bridges_mod.RegistrationResult(),
        )
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 201

        assert registered == [APP]
        assert started == [APP]


class TestInstallGateReads:
    @pytest.mark.asyncio
    async def test_a_disabled_record_may_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        assert install_app(_source_with_agent(tmp_path)).ok

        assert await routes_mod._app_may_run_after_install(APP) is False
        assert await routes_mod._app_may_enable_after_install(APP) is True

    @pytest.mark.asyncio
    async def test_an_enabled_record_may_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        assert install_app(_source_with_agent(tmp_path)).ok
        assert enable_app(APP).ok

        assert await routes_mod._app_may_run_after_install(APP) is True

    @pytest.mark.asyncio
    async def test_a_consent_pending_record_may_neither_run_nor_be_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        src = _make_app_source(tmp_path, permissions={"sessionApproval": True})
        assert install_app(src).ok

        assert await routes_mod._app_may_run_after_install(APP) is False
        assert await routes_mod._app_may_enable_after_install(APP) is False

    @pytest.mark.asyncio
    async def test_a_missing_record_may_neither_run_nor_be_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        assert await routes_mod._app_may_run_after_install(APP) is False
        assert await routes_mod._app_may_enable_after_install(APP) is False
