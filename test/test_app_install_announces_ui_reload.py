"""A route that replaces an app's files tells open dashboards with ``app_reload``.

A dashboard keeps an app's UI module for the life of the page, so a tab that has
the app open, or opens it again later, runs the old UI until something tells it
the files changed. The gateway sends the ``app_reload`` frame the dev-mode watcher
sends, and AppHost answers it with a page reload.

Every route that replaces an app's files on success sends it once: the update
route (``kirocrew app update`` and the Sync button) from a local source and from
the registry, the local install, and both registry install routes. A failed
update or install replaced nothing and sends nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    APP,
    _install,
    _make_app,
    _make_app_source,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import AppResult, enable_app, get_app, update_app


def _recording_app() -> tuple[web.Application, list[tuple[str, dict[str, Any]]]]:
    """An owner-authenticated routes app whose dashboard broadcasts are recorded."""
    app = _make_app(dashboard_user="owner")
    frames: list[tuple[str, dict[str, Any]]] = []
    app["state"].broadcast_ws = lambda msg_type, data: frames.append((msg_type, data))
    return app, frames


def _reloads(frames: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data["app"] for msg_type, data in frames if msg_type == "app_reload"]


def _bump_source(src: Path, version: str) -> None:
    manifest_path = src / "app.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)


async def _post(app: web.Application, path: str, body: dict[str, Any]) -> int:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, json=body)
        await resp.read()
        return resp.status


@pytest.mark.asyncio
async def test_a_local_update_announces_the_new_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    _bump_source(tmp_path / "source" / APP, "1.0.1")
    _no_backend(monkeypatch)
    app, frames = _recording_app()

    assert await _post(app, f"/api/apps/{APP}/update", {}) == 200

    assert get_app(APP)["version"] == "1.0.1"
    assert _reloads(frames) == [APP]


@pytest.mark.asyncio
async def test_a_failed_update_announces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    monkeypatch.setattr(
        routes_mod,
        "update_app",
        lambda source, expected_name=None: AppResult(ok=False, name=APP, error="copy failed"),
    )
    _no_backend(monkeypatch)
    app, frames = _recording_app()

    assert await _post(app, f"/api/apps/{APP}/update", {}) == 400

    assert _reloads(frames) == []


@pytest.mark.asyncio
async def test_a_local_install_announces_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install after an uninstall replaces a bundle an open tab may still hold."""
    _setup_env(tmp_path, monkeypatch)
    src = _make_app_source(tmp_path)
    _no_backend(monkeypatch)
    app, frames = _recording_app()

    assert await _post(app, "/api/apps/install", {"source": str(src)}) == 201

    assert get_app(APP) is not None
    assert _reloads(frames) == [APP]


def _stub_registry_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the registry clone: swap the installed tree with the real ``update_app``."""
    clone = _make_app_source(tmp_path / "clone")
    _bump_source(clone, "2.0.0")

    async def _reinstall(name: str, **kwargs: Any) -> dict[str, Any]:
        result = update_app(clone, expected_name=name)
        assert result.ok, result.error
        return {"ok": True, "name": name}

    monkeypatch.setattr(routes_mod, "install_from_registry", _reinstall)
    monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
    monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param(f"/api/apps/{APP}/update", {}, id="update"),
        pytest.param("/api/apps/registry/install", {"name": APP}, id="install"),
        pytest.param("/api/apps/registry/install-stream", {"name": APP}, id="install-stream"),
    ],
)
async def test_a_registry_update_announces_the_new_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any]
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    _stub_registry_clone(tmp_path, monkeypatch)
    _no_backend(monkeypatch)
    app, frames = _recording_app()

    assert await _post(app, path, body) in (200, 201)

    assert get_app(APP)["version"] == "2.0.0"
    assert _reloads(frames) == [APP]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/apps/registry/install", "/api/apps/registry/install-stream"]
)
async def test_a_failed_registry_install_announces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _setup_env(tmp_path, monkeypatch)

    async def _clone_fails(name: str, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "name": name, "error": "clone failed"}

    monkeypatch.setattr(routes_mod, "install_from_registry", _clone_fails)
    app, frames = _recording_app()

    await _post(app, path, {"name": APP})

    assert get_app(APP) is None
    assert _reloads(frames) == []
