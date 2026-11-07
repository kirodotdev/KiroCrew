"""A host without git gets one plain refusal from a registry install or update.

Every registry row is fetched with git. Without the preflight, a git-less host
fails deep in the checkout (``git init failed`` on a fresh clone, an
``unreadable_clone_origin`` refusal over a healthy checkout on an update). These
tests take git off ``PATH`` for real rather than patching the lookup, so they
exercise the same ``shutil.which`` answer the gateway gets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps import registry
from kiro_crew.apps.manager import enable_app, get_app

# The wire contract, spelled out rather than imported: a client keys off the code,
# and the install banner shows the sentence.
GIT_UNAVAILABLE_CODE = "git_unavailable"
GIT_UNAVAILABLE_ERROR = (
    "Installing or updating an App Store app needs git, which is not installed "
    "on this machine. Install git and retry."
)


def _drop_git_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``PATH`` at an empty directory, so ``git`` resolves nowhere."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def test_refusal_names_the_app_and_says_what_to_do(tmp_path, monkeypatch):
    from kiro_crew.apps.registry_git_preflight import git_unavailable_refusal

    _drop_git_from_path(tmp_path, monkeypatch)

    assert git_unavailable_refusal("demo") == {
        "ok": False,
        "name": "demo",
        "code": GIT_UNAVAILABLE_CODE,
        "error": GIT_UNAVAILABLE_ERROR,
    }


@pytest.mark.asyncio
async def test_install_refuses_before_trust_manifest_or_clone(tmp_path, monkeypatch):
    """A reinstall after uninstall is a fresh registry install: it must name the
    missing tool, not report ``git init failed`` from inside the clone."""
    monkeypatch.setattr(
        registry,
        "_resolve_install_entry",
        lambda name: ({"name": name, "gitUrl": "https://example.test/owner/demo"}, ""),
    )

    def _never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a git-less host must be refused before this step")

    async def _never_async(*args: Any, **kwargs: Any) -> Any:
        _never()

    monkeypatch.setattr(registry, "repository_bound_grant_denied", _never)
    monkeypatch.setattr(registry, "_fetch_app_manifest", _never_async)
    monkeypatch.setattr(registry, "_clone_build_app", _never_async)
    _drop_git_from_path(tmp_path, monkeypatch)

    result = await registry.install_from_registry("demo")

    assert result == {
        "ok": False,
        "name": "demo",
        "code": GIT_UNAVAILABLE_CODE,
        "error": GIT_UNAVAILABLE_ERROR,
    }


@pytest.mark.asyncio
async def test_registry_update_refuses_before_stopping_the_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update route asks before the install: the app stays enabled, registered
    and running, and the user reads the missing tool."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    calls: list[str] = []

    async def _must_not_install(name: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a git-less host must be refused before the install")

    monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
    monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
    monkeypatch.setattr(routes_mod, "install_from_registry", _must_not_install)
    monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
    monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))
    _drop_git_from_path(tmp_path, monkeypatch)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json={})
        assert resp.status == 400
        body = await resp.json()

    assert body["code"] == GIT_UNAVAILABLE_CODE
    assert body["error"] == GIT_UNAVAILABLE_ERROR
    assert calls == [], calls
    assert (get_app(APP) or {}).get("enabled") is True


@pytest.mark.asyncio
async def test_local_source_update_does_not_need_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight belongs to the registry branch only: an app updated from a
    local folder copies files and never spawns git."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    source = tmp_path / "source" / APP
    _drop_git_from_path(tmp_path, monkeypatch)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/update", json={"source": str(source)})
        body = await resp.json()

    assert resp.status == 200, body
    assert body.get("code") != GIT_UNAVAILABLE_CODE
