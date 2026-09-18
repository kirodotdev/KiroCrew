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
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": False, "acknowledged": True},
            )
            assert response.status == 200
            assert await response.json() == {
                "enabled": True,
                "supported": True,
                "permanent": False,
                "expires_at": 22_600.0,
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
    assert response_body == {
        "enabled": True,
        "supported": True,
        "permanent": True,
        "expires_at": None,
    }
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
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": False, "acknowledged": True},
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
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": False, "acknowledged": True},
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
                json={"enabled": True, "permanent": False, "acknowledged": True},
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
                json={"enabled": True, "permanent": True, "acknowledged": True},
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
        caller="owner",
        outcome="ok",
        resources="enabled=true",
        error="",
        critical=False,
        operation="docker_registry_access.write",
    )


@pytest.mark.asyncio
async def test_owner_read_logs_its_permission_decision(tmp_path) -> None:
    from kiro_crew.dashboard.handlers.docker_registry_access import _audit

    app, _ = _app()
    log = Mock()
    with (
        _patch_state(tmp_path / "docker_registry_access.json"),
        patch("kiro_crew.dashboard.handlers.docker_registry_access._audit", _audit),
        patch("kiro_crew.sel.sel", return_value=log),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/security/docker-registry-access")
            assert response.status == 200

    log.log_api_access.assert_called_once_with(
        caller="local-app",
        operation="docker_registry_access.read",
        outcome="approved",
        source="dashboard",
        resources="grant_state",
        error="",
        critical=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_audit_outage_blocks_grants_but_allows_revocation(tmp_path, enabled) -> None:
    from kiro_crew.dashboard.handlers.docker_registry_access import _audit

    state_path = tmp_path / "docker_registry_access.json"
    original = '{"enabled": true, "expires_at": 1001}'
    state_path.write_text(original, encoding="utf-8")
    app, refresh_defaults = _app()
    with (
        _patch_state(state_path),
        patch("kiro_crew.dashboard.handlers.docker_registry_access._audit", _audit),
        patch("kiro_crew.sel.sel", side_effect=OSError("audit unavailable")),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access",
                json={"enabled": enabled, "permanent": False, "acknowledged": enabled},
            )
            response_body = await response.json()
    if enabled:
        assert response.status == 503
        assert response_body["code"] == "audit_failed"
        assert state_path.read_text(encoding="utf-8") == original
        refresh_defaults.assert_not_awaited()
    else:
        assert response.status == 200
        assert json.loads(state_path.read_text(encoding="utf-8")) == {"enabled": False}
        refresh_defaults.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_audit_is_critical_and_precedes_keystone_write(tmp_path) -> None:
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.dashboard.handlers.docker_registry_access import _audit

    state_path = tmp_path / "docker_registry_access.json"
    app, _ = _app()
    events = []
    log = Mock()
    log.log_api_access.side_effect = lambda **kwargs: events.append(kwargs)

    def save(*args, **kwargs):
        assert events[0]["critical"] is True
        assert events[0]["outcome"] == "approved"
        return atomic_write(*args, **kwargs)

    with (
        _patch_state(state_path),
        patch("kiro_crew.dashboard.handlers.docker_registry_access._audit", _audit),
        patch("kiro_crew.sel.sel", return_value=log),
        patch("kiro_crew.dashboard.handlers.docker_registry_access.atomic_write", side_effect=save),
    ):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": False, "acknowledged": True},
            )
    assert response.status == 200
    assert events[0]["resources"] == "enabled=true permanent=false"


@pytest.mark.asyncio
async def test_non_linux_enable_is_refused(tmp_path) -> None:
    state_path = tmp_path / "docker_registry_access.json"
    app, refresh_defaults = _app()
    with _patch_state(state_path, platform="darwin"):
        async with TestClient(TestServer(app)) as client:
            response = await client.put(
                "/api/security/docker-registry-access",
                json={"enabled": True, "permanent": False, "acknowledged": True},
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
        {"enabled": True},
        {"enabled": True, "acknowledged": True},
        {"enabled": True, "permanent": False},
        {"enabled": True, "permanent": True, "acknowledged": False},
        {"enabled": True, "permanent": False, "acknowledged": "true"},
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
