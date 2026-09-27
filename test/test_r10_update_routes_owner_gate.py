"""Owner gate on the gateway-restart and update-policy routes.

``POST /api/restart``, ``POST /api/update/auto`` and ``POST /api/update/channel``
each restart the gateway or change what it installs next, so each is refused
for every caller but the dashboard owner -- the same gate ``POST /api/update``
carries.

Every row drives the REAL handler through aiohttp with a middleware that plants
the caller's claims, and stubs only the side effect, recording whether it ran.
The owner row is the positive control: the same stubs, the owner's claims, and
the side effect DOES run -- so a refusal row cannot pass because the stubs broke
the handler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import updates

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"
NON_OWNER = "U0NONOWNER"

# caller name -> (request claims, expected status)
_CALLERS: dict[str, tuple[dict[str, str], int]] = {
    # An allow-listed channel user: a dashboard-user token that is not the owner.
    "non_owner": ({"app": "", "user": NON_OWNER}, 403),
    # An App Kit app token, even one carrying the owner's subject.
    "app_token": ({"app": "some-app", "user": OWNER}, 403),
    "owner": ({"app": "", "user": OWNER}, 200),
}


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        owner_id=OWNER,
        _gateway_restart_task=None,
        _background_tasks=set(),
        push_update_progress=lambda *a, **k: None,
    )


def _stub_restart(monkeypatch: pytest.MonkeyPatch, reached: list[str]) -> None:
    async def _fake_restart(state: object, **_kw: object) -> None:
        reached.append("restart")

    monkeypatch.setattr(updates, "_restart_gateway", _fake_restart)


def _stub_auto(monkeypatch: pytest.MonkeyPatch, reached: list[str]) -> None:
    async def _fake_run_config_write(fn: object, path: object, *, mutate: Callable) -> None:
        mutate({})
        reached.append("auto_update")

    monkeypatch.setattr(updates, "run_config_write", _fake_run_config_write)
    monkeypatch.setattr(updates, "config_path", lambda: "/nonexistent/config.json")


def _stub_channel(monkeypatch: pytest.MonkeyPatch, reached: list[str]) -> None:
    def _fake_set_channel(ch: str) -> str:
        reached.append("channel")
        return ch

    async def _noop_check() -> None:
        return None

    monkeypatch.setattr(updates, "resolve_provider", lambda: None)
    monkeypatch.setattr(
        updates,
        "detect_install_layout",
        lambda: SimpleNamespace(is_git=False, is_externally_managed=False, guidance=""),
    )
    monkeypatch.setattr(updates, "set_release_channel", _fake_set_channel)
    monkeypatch.setattr(updates, "_do_update_check", _noop_check)
    monkeypatch.setattr(updates, "_invalidate_update_check", lambda ch: None)
    monkeypatch.setattr(
        updates, "KiroCrewConfig", SimpleNamespace(load=lambda: SimpleNamespace(auto_update=True))
    )


# route name -> (path, handler name, JSON body, side-effect stub)
_ROUTES: dict[str, tuple[str, str, dict[str, object] | None, Callable]] = {
    "restart": ("/api/restart", "api_gateway_restart", None, _stub_restart),
    "auto": ("/api/update/auto", "api_update_auto", {"enabled": False}, _stub_auto),
    "channel": ("/api/update/channel", "api_update_channel", {"channel": "nightly"}, _stub_channel),
}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_route_is_owner_only(
    route: str, caller: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, handler_name, body, stub = _ROUTES[route]
    claims, expected = _CALLERS[caller]
    reached: list[str] = []
    stub(monkeypatch, reached)

    @web.middleware
    async def _claims(request: web.Request, handler: Callable) -> web.StreamResponse:
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = _state()
    app.router.add_post(path, getattr(updates, handler_name))
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, json=body)
        text = await resp.text()
        await asyncio.sleep(0.5)  # the restart task waits 0.25s before it runs

    assert resp.status == expected, f"{caller} on {path}: {resp.status} {text}"
    if expected == 200:
        assert reached, f"owner control: the {route} side effect never ran"
    else:
        assert reached == [], f"{caller} reached the {route} side effect: {reached}"
