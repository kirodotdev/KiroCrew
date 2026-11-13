"""An update of an enabled app runs its ``setup.onEnable`` again, in the new tree.

An update replaces the installed folder with a fresh copy that never carries
``node_modules`` or ``.venv``, and keeps the app enabled, so whatever ``onEnable``
installed there is gone unless the script runs again. Every route that updates an
installed app (both branches of ``POST /api/apps/{name}/update``, an adopted record
among them, and a registry install over an installed app) re-runs the NEW version's
script once the app is re-registered and its backend started. A failing script
never turns the app off: the update answers success with an ``onEnableFailed``
reason, the app stays enabled with what the update registered, the user gets a bell
note, and the next update runs the script again. An update of a disabled app runs no
script (app-kit-platform, "An update of an enabled app runs `setup.onEnable` again").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
import kiro_crew.dashboard.handlers.template_server_request as tsr
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    INSTALLED_META_FILENAME,
    app_dir,
    apps_dir,
    get_app,
    install_app,
    update_app,
)

#: What the app's ``onEnable`` installs into the installed folder.
_DEP = Path("runtime") / "node_modules" / "demo-dep" / "package.json"

#: A backend-less app's HTTP server: registered as declared, with no backend to start.
_ONE_SERVER = {"srv": {"url": "http://127.0.0.1:7778/mcp"}}

#: (how the route updates, its path, the status of an applied update)
_ROUTES = [
    pytest.param("local", f"/api/apps/{APP}/update", 200, id="update-local"),
    pytest.param("registry", f"/api/apps/{APP}/update", 200, id="update-registry"),
    pytest.param("adopted", f"/api/apps/{APP}/update", 200, id="update-adopted"),
    pytest.param("registry", "/api/apps/registry/install", 201, id="registry-install"),
    pytest.param(
        "registry", "/api/apps/registry/install-stream", 200, id="registry-install-stream"
    ),
]


def _source(root: Path, version: str, **extra: Any) -> Path:
    src = root / f"source-{version}" / APP
    src.mkdir(parents=True)
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Coverage Test App",
        "description": "App for update tests",
        "author": "tester",
        "setup": {"onEnable": f"bash install-deps-{version}.sh", "onEnableTimeout": 45},
        **extra,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


class _Rig:
    """Records the backend calls and stands in for the ``onEnable`` script.

    The script stand-in writes :data:`_DEP` into the installed folder, as
    ``npm ci`` would, unless ``fail`` is set.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.fail = False
        monkeypatch.setattr(routes_mod, "start_app_backend", self._start)
        monkeypatch.setattr(routes_mod, "stop_app_backend", self._stop)
        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", self._script)

    def _start(self, name: str) -> None:
        self.calls.append("start")

    def _stop(self, name: str) -> None:
        self.calls.append("stop")

    async def _script(
        self, name: str, script: str, *, timeout: int = 30, action: str = "", **_: Any
    ) -> dict[str, Any]:
        self.calls.append(f"{action} {script} timeout={timeout}")
        if self.fail:
            return {"output": "npm ERR! network unreachable", "failed": True}
        dep = apps_dir() / name / _DEP
        dep.parent.mkdir(parents=True, exist_ok=True)
        dep.write_text("{}", encoding="utf-8")
        return {"output": "added 1 package", "failed": False}


def _adopt_record() -> None:
    """Give the installed record the shape a built-in adoption leaves behind.

    ``update_app`` with adoption provenance writes ``source: "registry:<name>"`` and
    ``origin: "registry"`` with the pinned repository and commit, so Apps > Update
    sends no source and the route takes its registry branch from the record.
    """
    meta_path = app_dir(APP) / INSTALLED_META_FILENAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        source=f"registry:{APP}",
        origin="registry",
        sourceUrl=f"https://github.com/KiroCrewCaptain/kirocrew-app-{APP}",
        sourceCommit="a" * 40,
    )
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _route_updates_to(
    monkeypatch: pytest.MonkeyPatch, rig: _Rig, how: str, v2: Path
) -> dict[str, Any]:
    """Point the update at *v2* and return the request body the route takes."""
    if how == "local":
        return {"source": str(v2)}

    async def _install_v2(name: str, **kwargs: Any) -> dict[str, Any]:
        rig.calls.append("registry clone")
        await kwargs["before_replace"]()
        result = await asyncio.to_thread(update_app, v2, expected_name=name)
        return {"ok": result.ok, "name": name, "error": result.error}

    monkeypatch.setattr(routes_mod, "git_unavailable_refusal", lambda n: None)
    monkeypatch.setattr(routes_mod, "install_from_registry", _install_v2)
    if how == "adopted":
        # The record names the source: the real `registry:` parse picks the branch.
        return {}
    monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
    monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
    return {"name": APP}


async def _post(client: TestClient, path: str, body: dict[str, Any]) -> tuple[int, dict]:
    """POST *body*; the result, read from the ``done`` event on the stream route."""
    resp = await client.post(path, json=body)
    text = await resp.text()
    if not path.endswith("install-stream"):
        return resp.status, json.loads(text)
    frame = text.rsplit("event: done\n", 1)[1].split("\n\n", 1)[0]
    return resp.status, json.loads("\n".join(line[6:] for line in frame.splitlines()))


async def _install_and_enable(
    tmp_path: Path, client: TestClient, rig: _Rig, how: str, **extra: Any
) -> None:
    assert install_app(_source(tmp_path, "1.0.0", **extra)).ok
    resp = await client.post(f"/api/apps/{APP}/enable")
    assert resp.status == 200, await resp.text()
    assert (apps_dir() / APP / _DEP).is_file()
    if how == "adopted":
        _adopt_record()
    rig.calls.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(("how", "path", "status"), _ROUTES)
async def test_an_update_of_an_enabled_app_reinstalls_what_on_enable_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, path: str, status: int
) -> None:
    _setup_env(tmp_path, monkeypatch)
    rig = _Rig(monkeypatch)
    body = _route_updates_to(monkeypatch, rig, how, _source(tmp_path, "2.0.0"))

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        await _install_and_enable(tmp_path, client, rig, how)
        got_status, data = await _post(client, path, body)

    assert got_status == status, data
    assert data["ok"] is True, data
    assert data.get("onEnable") == {"output": "added 1 package", "failed": False}
    assert "registration" in data
    # The NEW version's script, with its own timeout, after the backend start. An
    # adopted record takes the registry branch from its own `registry:` source.
    cloned = [] if how == "local" else ["registry clone"]
    on_enable = "on_enable bash install-deps-2.0.0.sh timeout=45"
    assert rig.calls == [*cloned, "stop", "start", on_enable]
    assert (apps_dir() / APP / _DEP).is_file()
    record = get_app(APP)
    assert record is not None
    assert (record["version"], record["enabled"]) == ("2.0.0", True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("how", "path", "status"), _ROUTES)
async def test_a_failed_on_enable_after_an_update_keeps_the_app_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, path: str, status: int
) -> None:
    _setup_env(tmp_path, monkeypatch)
    rig = _Rig(monkeypatch)
    body = _route_updates_to(
        monkeypatch, rig, how, _source(tmp_path, "2.0.0", mcpServers=_ONE_SERVER)
    )
    spec = tmp_path / "kiro-agents" / "kirocrew.json"
    notes: list[dict[str, Any]] = []

    async def _reach(request: object) -> int:
        return 0

    monkeypatch.setattr(tsr, "_reach_sessions", _reach)
    app = _make_app(dashboard_user="owner")
    app["state"].notify = lambda kind, title, body, **kw: notes.append(
        {"kind": kind, "title": title, "body": body, **kw}
    )

    async with TestClient(TestServer(app)) as client:
        await _install_and_enable(tmp_path, client, rig, how, mcpServers=_ONE_SERVER)
        rig.fail = True
        got_status, data = await _post(client, path, body)
        rig.fail = False
        retried_status, retried = await _post(client, path, body)

    # The update itself succeeded, and says what the script reported.
    assert got_status == status, data
    assert data["ok"] is True, data
    assert data.get("onEnableFailed") == "npm ERR! network unreachable"
    assert data.get("onEnable") == {"output": "npm ERR! network unreachable", "failed": True}
    assert "registration" in data
    # Nothing took the app down: no backend stop after the start, still enabled on
    # the new version, its MCP server still in the agent spec.
    cloned = [] if how == "local" else ["registry clone"]
    on_enable = "on_enable bash install-deps-2.0.0.sh timeout=45"
    assert rig.calls[: len(cloned) + 3] == [*cloned, "stop", "start", on_enable]
    assert f"{APP}:srv" in json.loads(spec.read_text())["mcpServers"]
    # One bell note on the app-update channel, naming the app and the reason.
    assert len(notes) == 1, notes
    assert notes[0]["channel"] == "system.apps"
    assert notes[0]["title"] == f"{APP} updated, but its setup script failed"
    assert "npm ERR! network unreachable" in notes[0]["body"]
    assert notes[0]["url"] == f"/apps/detail/{APP}"
    # The next update runs the script again and puts the dependency back.
    assert retried_status == status, retried
    assert retried.get("onEnable") == {"output": "added 1 package", "failed": False}
    assert "onEnableFailed" not in retried
    assert (apps_dir() / APP / _DEP).is_file()
    record = get_app(APP)
    assert record is not None
    assert (record["version"], record["enabled"]) == ("2.0.0", True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("how", "path", "status"), _ROUTES)
async def test_an_update_of_a_disabled_app_runs_no_on_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, path: str, status: int
) -> None:
    _setup_env(tmp_path, monkeypatch)
    rig = _Rig(monkeypatch)
    body = _route_updates_to(monkeypatch, rig, how, _source(tmp_path, "2.0.0"))

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        assert install_app(_source(tmp_path, "1.0.0")).ok
        if how == "adopted":
            _adopt_record()
        got_status, data = await _post(client, path, body)

    assert got_status == status, data
    assert data["ok"] is True, data
    assert "onEnable" not in data
    assert not any(call.startswith("on_enable") for call in rig.calls), rig.calls
    record = get_app(APP)
    assert record is not None
    assert (record["version"], record["enabled"]) == ("2.0.0", False)
