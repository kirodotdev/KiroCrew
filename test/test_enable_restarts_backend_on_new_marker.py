"""Enabling an app that gains its owner-enable marker restarts its backend.

A backend spawned while the app holds no owner-enable marker has no
``KIROCREW_GATEWAY_ORIGIN`` (``apps/backend.py``). Enabling the app as the owner creates
the marker, so the enable route stops the running backend and starts a new one, which
gets the origin. An enable that finds the marker already present starts the backend
without stopping it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.host_exec_grants import host_exec_granted, revoke_host_exec


def _spy_lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Record each backend stop/start with whether the marker stood at that moment."""
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        routes_mod, "stop_app_backend", lambda n: calls.append(("stop", host_exec_granted(n)))
    )
    monkeypatch.setattr(
        routes_mod, "start_app_backend", lambda n: calls.append(("start", host_exec_granted(n)))
    )
    return calls


async def _enable(client: TestClient) -> None:
    resp = await client.post(f"/api/apps/{APP}/enable")
    assert resp.status == 200, await resp.text()


@pytest.mark.asyncio
async def test_re_enable_that_mints_the_marker_restarts_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    calls = _spy_lifecycle(monkeypatch)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        await _enable(client)
        # An app enabled before the marker existed: enabled on record, no marker.
        revoke_host_exec(APP)
        assert not host_exec_granted(APP)
        calls.clear()

        await _enable(client)

    # Stopped, then started with the marker already in place, so the new spawn gets
    # the gateway origin the old backend lacked.
    assert calls == [("stop", True), ("start", True)]


@pytest.mark.asyncio
async def test_re_enable_with_the_marker_already_present_does_not_restart_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    calls = _spy_lifecycle(monkeypatch)
    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        await _enable(client)
        assert host_exec_granted(APP)
        calls.clear()

        await _enable(client)

    assert calls == [("start", True)]
