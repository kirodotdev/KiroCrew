"""The tailnet login allowlist fails closed when the daemon proof cannot be made.

``tailscale serve`` reaches the gateway over loopback with one
``X-Forwarded-For``. Where the kernel cannot prove the loopback client is the
daemon (macOS, Windows, a same-user ``tailscaled`` on Linux, a proof that
failed), the forwarded name is never attributed. These tests pin that the
allowlist still refuses every login it does not name, on the real auth
middleware over a real socket. The daemon's ``whois`` is mocked at
``tailnet._run_json_detail``.
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import tailnet, tailnet_proxy_proof
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import people
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY

TRUST = TailnetTrust(
    trust_identity=True,
    allowed_logins=("alice@example.com", "bob@example.com", "tagged-devices"),
)
ADDRS = {
    "100.64.0.11": ("alice@example.com", "alice-laptop.tail.ts.net."),
    "100.64.0.13": ("mallory@example.com", "mallory-laptop.tail.ts.net."),
}
ALICE, MALLORY, NOBODY = "100.64.0.11", "100.64.0.13", "100.64.0.99"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    import kiro_crew.dashboard.revocation_gen as rg
    import kiro_crew.dashboard.token_auth as ta

    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    monkeypatch.setattr(tailnet, "IS_POSIX", True)
    monkeypatch.setattr(tailnet_proxy_proof, "_daemon_uid_cache", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    tailnet._whois_cache.clear()
    people.reset_for_tests()

    def whois(argv: list[str]) -> tuple[Any, bool]:
        hit = ADDRS.get(argv[-1])
        if hit is None:
            return None, False
        return {"Node": {"Name": hit[1]}, "UserProfile": {"LoginName": hit[0]}}, False

    monkeypatch.setattr(tailnet, "_run_json_detail", MagicMock(side_effect=whois))
    yield
    ta._state.clear_all()
    tailnet._whois_cache.clear()
    people.reset_for_tests()


def _proof_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", lambda *_a: False)


def _proof_error(monkeypatch) -> None:
    def boom(*_a: object) -> bool:
        raise RuntimeError("proof could not run")

    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", boom)


def _proof_ok(monkeypatch) -> None:
    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", lambda *_a: True)


async def _whoami(request: web.Request) -> web.Response:
    actor = request.get(ACTOR_REQUEST_KEY)
    if actor is None:
        return web.json_response({"login": None, "unknown": None})
    p = actor.principal
    return web.json_response({"login": p.display.login, "unknown": p.is_unknown})


async def _ask(headers: list[tuple[str, str]]) -> tuple[int, Any]:
    app = web.Application(middlewares=[token_auth_middleware(tailnet_trust=TRUST)])
    app.router.add_get("/api/whoami", _whoami)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    try:
        token = generate_token("tsuser", ttl_seconds=300)
        resp = await client.get("/api/whoami", params={"token": token}, headers=headers)
        body = await resp.json() if resp.status == 200 else await resp.text()
        return resp.status, body
    finally:
        await client.close()


_UNPROVEN = pytest.mark.parametrize(
    "no_proof", [_proof_unavailable, _proof_error], ids=["proof-unavailable", "proof-error"]
)


@pytest.mark.asyncio
@_UNPROVEN
async def test_a_login_off_the_allowlist_is_refused_without_the_proof(monkeypatch, no_proof):
    no_proof(monkeypatch)
    status, body = await _ask([("X-Forwarded-For", MALLORY)])
    assert status == 403, body
    assert "tailnet login not allowed" in body


@pytest.mark.asyncio
@_UNPROVEN
async def test_an_address_the_daemon_cannot_name_is_refused(monkeypatch, no_proof):
    no_proof(monkeypatch)
    status, body = await _ask([("X-Forwarded-For", NOBODY)])
    assert status == 403, body
    assert "tailnet identity unverified" in body


@pytest.mark.asyncio
@_UNPROVEN
async def test_a_chain_cannot_smuggle_a_login_past_the_allowlist(monkeypatch, no_proof):
    no_proof(monkeypatch)
    for headers in (
        [("X-Forwarded-For", f"{ALICE}, {MALLORY}")],
        [("X-Forwarded-For", ALICE), ("X-Forwarded-For", MALLORY)],
    ):
        status, body = await _ask(headers)
        assert status == 403, (headers, body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof", [_proof_unavailable, _proof_error, _proof_ok], ids=["unavailable", "error", "proven"]
)
async def test_a_login_header_that_disagrees_with_the_daemon_is_refused(monkeypatch, proof):
    proof(monkeypatch)
    status, body = await _ask(
        [("X-Forwarded-For", MALLORY), ("Tailscale-User-Login", "alice@example.com")]
    )
    assert status == 403, body


@pytest.mark.asyncio
@_UNPROVEN
async def test_an_allowed_login_without_the_proof_is_admitted_unnamed(monkeypatch, no_proof):
    no_proof(monkeypatch)
    status, body = await _ask(
        [("X-Forwarded-For", ALICE), ("Tailscale-User-Login", "alice@example.com")]
    )
    assert status == 200
    assert body == {"login": "", "unknown": True}


@pytest.mark.asyncio
async def test_a_proven_allowed_login_is_named(monkeypatch):
    _proof_ok(monkeypatch)
    status, body = await _ask([("X-Forwarded-For", ALICE)])
    assert status == 200
    assert body["login"] == "alice@example.com" and body["unknown"] is False


@pytest.mark.asyncio
@_UNPROVEN
async def test_the_local_operator_is_never_refused(monkeypatch, no_proof):
    no_proof(monkeypatch)
    status, body = await _ask([])
    assert status == 200
    assert body["unknown"] is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc proof")
@pytest.mark.asyncio
async def test_a_same_user_daemon_does_not_open_the_allowlist(monkeypatch):
    """The real proof refuses a daemon running as the gateway's user (proof mismatch)."""
    monkeypatch.setattr(tailnet_proxy_proof, "_daemon_uids", lambda: frozenset({os.geteuid()}))
    status, body = await _ask([("X-Forwarded-For", MALLORY)])
    assert status == 403, body
