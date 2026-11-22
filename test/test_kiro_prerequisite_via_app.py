"""``GET /api/kiro-prerequisite`` answers a delegated (``via_app``) token like a non-owner.

A delegated token carries the owner's identity, so the owner test alone would hand it the
host platform, binary paths and probe errors. The body it gets must equal the one a
signed-in non-owner dashboard user gets for the same snapshot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.kiro_prerequisite import api_kiro_prerequisite_status
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

HOST_DETAIL = "/opt/host/bin/kiro-cli probe failed: EPERM"


class _Service(KiroPrerequisiteService):
    async def snapshot(self, *, force: bool = False, coalesce: bool = False) -> dict[str, Any]:
        del force, coalesce
        return {
            "platform": "Linux",
            "installed": True,
            "authenticated": True,
            "ready": True,
            "initial_setup_complete": True,
            "repair_required": False,
            "probe_error": HOST_DETAIL,
            "binary_path": "/opt/host/bin/kiro-cli",
            "operation": {"kind": "login", "status": "succeeded", "detail": HOST_DETAIL},
        }


def _app(*, user: str, via_app: str, home: Path) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = ""
        if via_app:
            request["via_app"] = via_app
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(owner_id="the-owner")
    app["kiro_prerequisite_service"] = _Service(
        platform_name="linux",
        environ={"HOME": str(home), "PATH": ""},
        home=home,
        audit_writer=lambda *_a, **_k: None,
    )
    app.router.add_get("/api/kiro-prerequisite", api_kiro_prerequisite_status)
    return app


async def _body(*, user: str, via_app: str, home: Path) -> dict[str, Any]:
    async with TestClient(TestServer(_app(user=user, via_app=via_app, home=home))) as client:
        response = await client.get("/api/kiro-prerequisite?refresh=explicit")
        assert response.status == 200
        return await response.json()


@pytest.mark.asyncio
async def test_a_delegated_token_gets_the_non_owner_body(tmp_path: Path) -> None:
    delegated = await _body(user="the-owner", via_app="some-app", home=tmp_path)
    non_owner = await _body(user="someone-else", via_app="", home=tmp_path)
    owner = await _body(user="the-owner", via_app="", home=tmp_path)
    assert delegated == non_owner
    assert delegated["setup_allowed"] is False
    assert delegated["platform"] == "gateway"
    assert HOST_DETAIL not in str(delegated)
    assert "/opt/host" not in str(delegated)
    # The person keeps the full owner payload (so the comparison above is not vacuous).
    assert owner["setup_allowed"] is True
    assert owner["platform"] == "Linux"
