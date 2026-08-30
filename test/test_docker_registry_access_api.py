"""Dedicated owner-only Docker registry credential grant API."""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner


def _app() -> tuple[web.Application, AsyncMock]:
    from kiro_crew.dashboard.handlers.docker_registry_access import (
        api_docker_registry_access_get,
        api_docker_registry_access_put,
    )

    sessions = SimpleNamespace(refresh_defaults=AsyncMock())
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="", sessions=sessions)
    app.router.add_get("/api/security/docker-registry-access", api_docker_registry_access_get)
    app.router.add_put("/api/security/docker-registry-access", api_docker_registry_access_put)
    return as_owner(app), sessions.refresh_defaults


def _patch_state(path, *, platform="linux") -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.docker_registry_access.docker_registry_access_state_path",
            return_value=path,
        )
    )
    stack.enter_context(
        patch("kiro_crew.config.loader.docker_registry_access_state_path", return_value=path)
    )
    stack.enter_context(
        patch("kiro_crew.dashboard.handlers.docker_registry_access.sys.platform", platform)
    )
    stack.enter_context(
        patch(
            "kiro_crew.dashboard.handlers.docker_registry_access._audit",
            new_callable=AsyncMock,
        )
    )
    stack.enter_context(
        patch("kiro_crew.dashboard.handlers.docker_registry_access.time.time", return_value=1_000.0)
    )
    stack.enter_context(patch("kiro_crew.config.loader.time.time", return_value=1_000.0))
    return stack


@pytest.mark.asyncio
async def test_owner_grant_uses_keystone_and_refreshes_future_sessions(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access", json={"enabled": True}
            )
            assert response.status == 200
            assert await response.json() == {
                "enabled": True,
                "supported": True,
            }

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "enabled": True,
        "expires_at": 22_600.0,
    }
    if os.name != "nt":
        assert state_path.stat().st_mode & 0o777 == 0o600
    refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_owner_can_read_the_effective_grant(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    state_path.write_text('{"enabled": true, "permanent": true}', encoding="utf-8")
    app, refresh_defaults = _app()
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/security/docker-registry-access")
            response_body = await response.json()

    assert response.status == 200
    assert response_body == {"enabled": True, "supported": True}
    refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_keystone_write_fails_closed(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with (
        _patch_state(state_path),
        patch(
            "kiro_crew.dashboard.handlers.docker_registry_access.atomic_write",
            side_effect=OSError("disk unavailable"),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access", json={"enabled": True}
            )
            response_body = await response.json()

    assert response.status == 500
    assert response_body["code"] == "write_failed"
    assert not state_path.exists()
    refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_grant_survives_refresh_failure(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    refresh_defaults.side_effect = RuntimeError("pool unavailable")
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access", json={"enabled": True}
            )
            response_body = await response.json()

    assert response.status == 200
    assert response_body["enabled"] is True
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "enabled": True,
        "expires_at": 22_600.0,
    }
    refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_config_cannot_mint_the_grant(tmp_path) -> None:
    from kiro_crew.dashboard.handlers.core import api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app = as_owner(app)
    with patch("kiro_crew.config.loader.config_path", return_value=tmp_path / "config.json"):
        async with TestClient(TestServer(app)) as client:
            response = await client.patch(
                "/api/config/kirocrew",
                json={"path": "agent.sandbox_expose_docker_config", "value": True},
            )
    assert response.status == 400


@pytest.mark.asyncio
async def test_non_owner_cannot_read_or_write_the_grant(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            headers = {"X-Test-User": "allowed-channel-user"}
            get_response = await client.get("/api/security/docker-registry-access", headers=headers)
            put_response = await client.put(
                "/api/security/docker-registry-access",
                json={"enabled": True},
                headers=headers,
            )

    assert get_response.status == 403
    assert put_response.status == 403
    assert not state_path.exists()
    refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_can_choose_a_persistent_grant(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": True},
            )

    assert response.status == 200
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "enabled": True,
        "permanent": True,
    }
    refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_linux_stored_grant_can_be_revoked(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    state_path.write_text('{"enabled": true, "permanent": true}', encoding="utf-8")
    app, refresh_defaults = _app()
    with _patch_state(state_path, platform="darwin"):
        async with TestClient(TestServer(app)) as client:
            revoked = await client.put(
                "/api/security/docker-registry-access", json={"enabled": False}
            )
            revoked_body = await revoked.json()

    assert revoked.status == 200
    assert revoked_body["enabled"] is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"enabled": False}
    refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_audit_offloads_sel_work() -> None:
    from kiro_crew.dashboard.handlers.docker_registry_access import _audit

    audit_sync = Mock()
    with patch("kiro_crew.dashboard.handlers.docker_registry_access._audit_sync", audit_sync):
        await _audit({"user": "owner"}, outcome="ok", resources="enabled=true")  # type: ignore[arg-type]

    audit_sync.assert_called_once_with(
        caller="owner", outcome="ok", resources="enabled=true", error=""
    )


@pytest.mark.asyncio
async def test_non_linux_enable_is_refused(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path, platform="darwin"):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access", json={"enabled": True}
            )
            response_body = await response.json()

    assert response.status == 409
    assert response_body["code"] == "platform_unsupported"
    assert not state_path.exists()
    refresh_defaults.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"enabled": "true"},
        {"enabled": True, "x": 1},
        {"enabled": False, "permanent": True},
        {"enabled": True, "permanent": "yes"},
    ],
)
async def test_invalid_bodies_fail_closed(tmp_path, body) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path):
        async with TestClient(TestServer(app)) as client:
            kwargs = {"data": b"not-json"} if body is None else {"json": body}
            response = await client.put("/api/security/docker-registry-access", **kwargs)

    assert response.status == 400
    assert not state_path.exists()
    refresh_defaults.assert_not_awaited()
