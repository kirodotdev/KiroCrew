"""The setup status's ``crew_sign_in`` field: the first-run experience's switch.

First run uses Kiro Crew's own sign-in exactly when KAS is the backend and
``agent.kas_bundle`` is switched on; otherwise it stays on kiro-cli's install and
login steps. So the field follows that switch alone for the owner -- never the
automatic route a host takes without a working kiro-cli -- stays false for a
non-owner (who cannot sign the gateway in), and fails closed to false so an
unreadable config never locks the dashboard.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kiro_prerequisite_helpers import _no_audit

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.auth import whoami
from kiro_crew.dashboard.handlers.kiro_prerequisite import (
    api_kiro_prerequisite_repair_specs,
    api_kiro_prerequisite_status,
    api_kiro_prerequisite_update_cli,
)
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


def _app(
    service: KiroPrerequisiteService,
    *,
    app_claim: str,
    user: str = "test-user",
    owner_id: str = "test-user",
) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id=owner_id)
    app["kiro_prerequisite_service"] = service
    app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
    app.router.add_post("/api/kiro-prerequisite/repair-specs", api_kiro_prerequisite_repair_specs)
    app.router.add_post("/api/kiro-prerequisite/update-cli", api_kiro_prerequisite_update_cli)
    return app


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KiroPrerequisiteService:
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": ""},
        home=tmp_path,
        audit_writer=_no_audit,
    )

    async def snapshot(*, force: bool = False, coalesce: bool = False) -> dict[str, Any]:
        del force, coalesce
        return {"platform": "Linux", "installed": False, "ready": False}

    async def remedy(caller: str) -> dict[str, Any]:
        del caller
        return {"platform": "Linux", "installed": True, "ready": False}

    monkeypatch.setattr(service, "snapshot", snapshot)
    monkeypatch.setattr(service, "repair_agent_specs", remedy)
    monkeypatch.setattr(service, "update_cli", remedy)
    return service


def _config(monkeypatch: pytest.MonkeyPatch, *, backend: str, kas_bundle: bool) -> None:
    agent = SimpleNamespace(acp_backend=backend, kas_bundle=kas_bundle)
    monkeypatch.setattr(whoami, "load_sign_in_owner_config", lambda: SimpleNamespace(agent=agent))


async def _status(app: Any) -> dict[str, Any]:
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/kiro-prerequisite")
        assert response.status == 200
        return await response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "kas_bundle", "expected"),
    [
        (ACP_BACKEND_KAS, True, True),
        # Switch off: kiro-cli's experience, even where the host would take the
        # automatic route because no kiro-cli runs there.
        (ACP_BACKEND_KAS, False, False),
        # The switch only means anything for KAS.
        (ACP_BACKEND_KIRO, True, False),
    ],
)
async def test_owner_status_follows_the_kas_engine_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    kas_bundle: bool,
    expected: bool,
) -> None:
    _config(monkeypatch, backend=backend, kas_bundle=kas_bundle)
    # The vault's say is not consulted: only the switch picks the experience.
    monkeypatch.setattr(whoami, "vault_owns_sign_in", lambda: not expected)
    body = await _status(_app(_service(tmp_path, monkeypatch), app_claim=""))
    assert body["crew_sign_in"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/kiro-prerequisite/repair-specs", "/api/kiro-prerequisite/update-cli"]
)
@pytest.mark.parametrize("kas_bundle", [True, False])
async def test_owner_remedy_answers_carry_the_switch_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, kas_bundle: bool
) -> None:
    # The dashboard caches these answers as the setup status, so leaving the
    # field out reads as the switch being off and admits the app unsigned.
    _config(monkeypatch, backend=ACP_BACKEND_KAS, kas_bundle=kas_bundle)
    app = _app(_service(tmp_path, monkeypatch), app_claim="")
    async with TestClient(TestServer(app)) as client:
        response = await client.post(path)
        assert response.status == 200
        body = await response.json()
    assert body["setup_allowed"] is True
    assert body["crew_sign_in"] is kas_bundle


@pytest.mark.asyncio
async def test_non_owner_is_never_held_behind_the_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the owner can sign the gateway in, so a viewer is never told to.
    _config(monkeypatch, backend=ACP_BACKEND_KAS, kas_bundle=True)
    app = _app(
        _service(tmp_path, monkeypatch),
        app_claim="",
        user="allowed-slack-user",
        owner_id="configured-owner",
    )
    body = await _status(app)
    assert body["setup_allowed"] is False
    assert body["crew_sign_in"] is False


@pytest.mark.asyncio
async def test_an_unreadable_config_never_locks_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Any:
        raise whoami.ConfigUnreadable("config.json present but unparseable")

    monkeypatch.setattr(whoami, "load_sign_in_owner_config", boom)
    body = await _status(_app(_service(tmp_path, monkeypatch), app_claim=""))
    assert body["crew_sign_in"] is False
