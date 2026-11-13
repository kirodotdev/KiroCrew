"""The setup status's ``kiro_sign_in_needed`` field: first run's Kiro sign-in hold.

True exactly when the next KAS session takes the bundle route (the shared
``kas_bundle_route()``) and no usable Kiro sign-in is stored
(``KasToken.is_usable``). Answered by every owner route the dashboard caches as
its status, always false for a non-owner, and false when no read can produce an
answer at all; a vault that is present but unreadable answers, as no usable
sign-in. Remembered while the vault's write stamp and the config signature are
both unchanged, so the forever poll behind the field costs one ``stat`` of each.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kiro_prerequisite_helpers import _no_audit

from kiro_crew.agent_sdk import kas_bundle
from kiro_crew.auth import bridge
from kiro_crew.config import load_memo
from kiro_crew.dashboard.handlers import kiro_prerequisite as handler
from kiro_crew.dashboard.handlers.kiro_prerequisite import (
    api_kiro_prerequisite_repair_specs,
    api_kiro_prerequisite_status,
    api_kiro_prerequisite_update_cli,
)
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the handler's memo and pin the config half of its key.

    The memo is module state, so without this one test's answer would serve the
    next one's poll. Pinning :func:`config_signature` keeps the vault's write
    stamp the only key a test moves, and keeps the key off the host's config files.
    """
    monkeypatch.setattr(handler, "_sign_in_needed_memo", None)
    monkeypatch.setattr(load_memo, "config_signature", lambda: ("pinned",))


def _app(
    service: KiroPrerequisiteService,
    *,
    user: str = "test-user",
    owner_id: str = "test-user",
) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = ""
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


class _Vault:
    """The vault the handler reads: a write stamp, a stored token, a resolve count."""

    def __init__(self, token: Any, stamp: str) -> None:
        self.token = token
        self.stamp = stamp
        self.resolves = 0

    def write_stamp(self) -> str:
        return self.stamp

    def resolve(self) -> Any:
        self.resolves += 1
        return self.token


def _stored(monkeypatch: pytest.MonkeyPatch, token: Any, stamp: str = "vault-1") -> _Vault:
    store = _Vault(token, stamp)
    monkeypatch.setattr(bridge, "default_token_store", lambda: store)
    return store


def _token(*, usable: bool) -> Any:
    return SimpleNamespace(is_usable=lambda: usable)


async def _status(app: web.Application) -> dict[str, Any]:
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/api/kiro-prerequisite")
        assert response.status == 200
        body: dict[str, Any] = await response.json()
        return body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bundle_route", "token", "expected"),
    [
        (True, None, True),
        # Stored but unusable (expired, no refresh token) is not a sign-in.
        (True, _token(usable=False), True),
        (True, _token(usable=True), False),
        # The relay route (kiro-cli) and every other harness keep their first run.
        (False, None, False),
    ],
)
async def test_owner_status_is_bundle_route_and_no_usable_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_route: bool,
    token: Any,
    expected: bool,
) -> None:
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", lambda: bundle_route)
    _stored(monkeypatch, token)
    body = await _status(_app(_service(tmp_path, monkeypatch)))
    assert body["setup_allowed"] is True
    assert body.get("kiro_sign_in_needed") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/kiro-prerequisite/repair-specs", "/api/kiro-prerequisite/update-cli"]
)
@pytest.mark.parametrize("signed_in", [True, False])
async def test_owner_remedy_answers_carry_the_field_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, signed_in: bool
) -> None:
    # The dashboard caches these answers as the setup status, so leaving the
    # field out would drop the hold mid-load.
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", lambda: True)
    _stored(monkeypatch, _token(usable=True) if signed_in else None)
    async with TestClient(TestServer(_app(_service(tmp_path, monkeypatch)))) as client:
        response = await client.post(path)
        assert response.status == 200
        body = await response.json()
    assert body["setup_allowed"] is True
    assert body.get("kiro_sign_in_needed") is (not signed_in)


@pytest.mark.asyncio
async def test_non_owner_is_never_held_behind_the_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", lambda: True)
    _stored(monkeypatch, None)
    app = _app(
        _service(tmp_path, monkeypatch), user="allowed-slack-user", owner_id="configured-owner"
    )
    body = await _status(app)
    assert body["setup_allowed"] is False
    assert body.get("kiro_sign_in_needed") is False


def _raise(*_: Any) -> Any:
    raise OSError("vault directory refused")


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["route", "store"])
async def test_a_read_error_answers_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    monkeypatch.setattr(
        kas_bundle, "kas_bundle_route", _raise if broken == "route" else (lambda: True)
    )
    if broken == "store":
        monkeypatch.setattr(bridge, "default_token_store", _raise)
    else:
        _stored(monkeypatch, None)
    body = await _status(_app(_service(tmp_path, monkeypatch)))
    assert body.get("kiro_sign_in_needed") is False


def _counting_route(calls: list[str]) -> Any:
    """The bundle route, recording each resolution in *calls*."""

    def route() -> bool:
        calls.append("resolved")
        return True

    return route


async def _poll(client: TestClient) -> Any:
    """One owner status GET, as the dashboard's background poll makes it."""
    response = await client.get("/api/kiro-prerequisite")
    assert response.status == 200
    body: dict[str, Any] = await response.json()
    return body.get("kiro_sign_in_needed")


async def _two_owner_polls(app: web.Application) -> list[Any]:
    async with TestClient(TestServer(app)) as client:
        return [await _poll(client), await _poll(client)]


@pytest.mark.asyncio
async def test_a_second_poll_with_the_same_stamps_resolves_no_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner dashboard polls this route every 30 s forever, and resolving the
    # route warns about a lapsed vault and audits a credential read each time.
    calls: list[str] = []
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", _counting_route(calls))
    vault = _stored(monkeypatch, None)
    assert await _two_owner_polls(_app(_service(tmp_path, monkeypatch))) == [True, True]
    assert calls == ["resolved"]
    assert vault.resolves == 1


@pytest.mark.asyncio
async def test_a_vault_write_recomputes_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", _counting_route(calls))
    vault = _stored(monkeypatch, None, stamp="vault-signed-out")
    app = _app(_service(tmp_path, monkeypatch))
    async with TestClient(TestServer(app)) as client:
        assert await _poll(client) is True
        # A sign-in: the vault is rewritten, which is a new write stamp.
        vault.token = _token(usable=True)
        vault.stamp = "vault-signed-in"
        assert await _poll(client) is False
    # Both polls read the vault, which a memo hit would not have; only the
    # signed-out one went on to the route.
    assert vault.resolves == 2
    assert calls == ["resolved"]


@pytest.mark.asyncio
async def test_a_usable_sign_in_resolves_no_route_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stored usable sign-in settles the field whichever route the next session
    # takes, so the steady state of a signed-in gateway reads the vault and stops.
    calls: list[str] = []
    monkeypatch.setattr(kas_bundle, "kas_bundle_route", _counting_route(calls))
    _stored(monkeypatch, _token(usable=True))
    assert await _two_owner_polls(_app(_service(tmp_path, monkeypatch))) == [False, False]
    assert calls == []
