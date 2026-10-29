"""A local token holder cannot name a tailnet person with forwarding headers.

Loopback names the host, not the proxy. These tests drive the real auth
middleware and assert that a forwarded identity is read only when the kernel
proves the loopback client is the Tailscale daemon
(``tailnet_proxy_proof.loopback_client_is_daemon``). The daemon's ``whois`` is
mocked at ``tailnet._run_json_detail``; the socket-owner lookup is real on Linux.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import tailnet, tailnet_proxy_proof
from kiro_crew.dashboard.tailnet import TailnetTrust
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.shared_crew import people
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, Provenance

TRUST = TailnetTrust(
    trust_identity=True,
    allowed_logins=("alice@example.com", "bob@example.com"),
    pin_scope="login",
)
ADDRS = {
    "100.64.0.5": ("alice@example.com", "alice-laptop.tail.ts.net."),
    "100.64.0.7": ("bob@example.com", "bob-laptop.tail.ts.net."),
}
BOB = "100.64.0.7"

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc proof")


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


async def _whoami(request: web.Request) -> web.Response:
    actor = request.get(ACTOR_REQUEST_KEY)
    if actor is None:
        return web.json_response({"login": None, "provenance": None})
    p = actor.principal
    return web.json_response({"login": p.display.login, "provenance": p.provenance.value})


async def _client() -> TestClient:
    app = web.Application(middlewares=[token_auth_middleware(tailnet_trust=TRUST)])
    app.router.add_get("/api/whoami", _whoami)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    return client


async def _ask(client: TestClient, token: str, headers: list[tuple[str, str]]) -> tuple[int, Any]:
    resp = await client.get("/api/whoami", params={"token": token}, headers=headers)
    body = await resp.json() if resp.status == 200 else None
    return resp.status, body


def _token(user: str = "local-app") -> str:
    return generate_token(user, ttl_seconds=300)


# -- Real sockets: the client end is this test process ---------------------- #


@linux_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [("X-Forwarded-For", BOB)],
        [("X-Forwarded-For", BOB), ("Tailscale-User-Login", "bob@example.com")],
        [("X-Forwarded-For", BOB), ("Tailscale-User-Name", "Bob")],
        [("X-Forwarded-For", f"{BOB}, 100.64.0.5")],
        [("X-Forwarded-For", BOB), ("X-Forwarded-For", BOB)],
    ],
    ids=["xff", "xff+login-header", "xff+name-header", "chain", "repeated"],
)
async def test_a_same_user_token_holder_cannot_become_bob(monkeypatch, headers):
    # Even a daemon that ran as this very user is not provable.
    monkeypatch.setattr(tailnet_proxy_proof, "_daemon_uids", lambda: frozenset({os.geteuid()}))
    client = await _client()
    try:
        for token in (_token(), _token("owner")):
            status, body = await _ask(client, token, headers)
            assert body is None or body["login"] != "bob@example.com", (status, body)
    finally:
        await client.close()


@linux_only
@pytest.mark.asyncio
async def test_forged_login_headers_without_xff_never_name_bob(monkeypatch):
    monkeypatch.setattr(tailnet_proxy_proof, "_daemon_uids", lambda: frozenset({os.geteuid()}))
    client = await _client()
    try:
        status, body = await _ask(
            client,
            _token(),
            [("Tailscale-User-Login", "bob@example.com"), ("Tailscale-User-Name", "Bob")],
        )
        assert status == 200
        assert body["login"] != "bob@example.com"
        assert body["provenance"] == Provenance.LOCAL_OPERATOR.value
    finally:
        await client.close()


@linux_only
@pytest.mark.asyncio
async def test_a_proven_daemon_socket_still_resolves(monkeypatch):
    """Positive control: the real ``/proc`` lookup finds this socket's owner."""
    real_uid = os.geteuid()
    monkeypatch.setattr(tailnet_proxy_proof, "_effective_uid", lambda: real_uid + 1)
    monkeypatch.setattr(tailnet_proxy_proof, "_daemon_uids", lambda: frozenset({real_uid}))
    client = await _client()
    try:
        status, body = await _ask(client, _token(), [("X-Forwarded-For", BOB)])
        assert status == 200
        assert body == {"login": "bob@example.com", "provenance": "tailscale_serve"}
    finally:
        await client.close()


# -- Any platform: an unproven loopback client is never attributed ---------- #


@pytest.mark.asyncio
async def test_missing_proof_is_never_attributed(monkeypatch):
    monkeypatch.setattr(tailnet, "loopback_client_is_daemon", lambda *_a: False)
    client = await _client()
    try:
        status, body = await _ask(client, _token(), [("X-Forwarded-For", BOB)])
        assert body is None or body["login"] != "bob@example.com", (status, body)
    finally:
        await client.close()


# -- The proof against a fake /proc ----------------------------------------- #

GW = ("127.0.0.1", 8765)
PEER = ("127.0.0.1", 50123)
# 127.0.0.1 little-endian is 0100007F; ports in hex.
_ROW = "   0: 0100007F:C3CB 0100007F:223D 01 00000000:00000000 00:00000000 00000000  {uid}  0 4242"
_ROW6 = (
    "   0: 0000000000000000FFFF00000100007F:C3CB 0000000000000000FFFF00000100007F:223D "
    "01 00000000:00000000 00:00000000 00000000  {uid}  0 4243"
)
_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid"


def _fake_proc(root: Path, *, tcp: str = "", tcp6: str = "", daemon_uid: int | None = 0) -> None:
    (root / "net").mkdir(parents=True)
    (root / "net" / "tcp").write_text(_HEAD + "\n" + tcp + "\n")
    (root / "net" / "tcp6").write_text(_HEAD + "\n" + tcp6 + "\n")
    if daemon_uid is not None:
        pid = root / "77"
        pid.mkdir()
        (pid / "comm").write_text("tailscaled\n")
        (pid / "status").write_text(f"Name:\ttailscaled\nUid:\t{daemon_uid}\t{daemon_uid}\t0\t0\n")
    other = root / "78"
    other.mkdir()
    (other / "comm").write_text("python3\n")
    (other / "status").write_text("Name:\tpython3\nUid:\t1000\t1000\t1000\t1000\n")


@pytest.fixture()
def fake_proc(tmp_path, monkeypatch):
    monkeypatch.setattr(tailnet_proxy_proof, "_PROC", tmp_path)
    monkeypatch.setattr(tailnet_proxy_proof.sys, "platform", "linux")
    monkeypatch.setattr(tailnet_proxy_proof, "_effective_uid", lambda: 1000)
    return tmp_path


def test_another_users_process_named_tailscaled_is_not_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=1001), daemon_uid=1001)
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False


def test_daemon_owned_socket_is_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=0))
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is True


def test_mapped_ipv6_socket_is_proven(fake_proc):
    _fake_proc(fake_proc, tcp6=_ROW6.format(uid=0))
    assert tailnet_proxy_proof.loopback_client_is_daemon(("::ffff:127.0.0.1", 50123), GW)


def test_gateway_user_socket_is_not_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=1000), daemon_uid=1000)
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False


def test_another_users_socket_is_not_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=1001))
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False


def test_no_running_daemon_is_not_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=0), daemon_uid=None)
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False


def test_no_matching_socket_is_not_proven(fake_proc):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=0))
    assert tailnet_proxy_proof.loopback_client_is_daemon(("127.0.0.1", 50124), GW) is False
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, ("127.0.0.1", 8766)) is False


def test_unreadable_proc_and_bad_input_are_not_proven(fake_proc):
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False
    assert tailnet_proxy_proof.loopback_client_is_daemon(None, GW) is False
    assert tailnet_proxy_proof.loopback_client_is_daemon(("x", "y"), GW) is False


def test_other_platforms_are_not_proven(fake_proc, monkeypatch):
    _fake_proc(fake_proc, tcp=_ROW.format(uid=0))
    monkeypatch.setattr(tailnet_proxy_proof.sys, "platform", "darwin")
    assert tailnet_proxy_proof.loopback_client_is_daemon(PEER, GW) is False
