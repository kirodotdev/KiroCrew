"""Connected computers: the connector socket's admission, handshake and recheck.

Drives ``GET /api/connected-computers/connect`` end to end on a loopback test
server (port 0) with a real device registry in a temporary vault and real
connector credentials, then covers the final admission (``_admit``) and the
open socket's recheck (``_watch``) directly with pure-Python fakes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import live
from kiro_crew.connected_computers import broker as broker_mod
from kiro_crew.connected_computers import flag, protocol, remote_routes, vault_doc, ws_route
from kiro_crew.connected_computers.authenticated import Liveness, SessionFrames
from kiro_crew.connected_computers.broker import ConnectionBroker, DeviceConnection
from kiro_crew.connected_computers.connector_token import (
    CredentialRejected,
    connector_auth,
    mint,
    peer_key_from_secret,
    proof_matches,
)
from kiro_crew.connected_computers.devices import (
    STATUS_ACTIVE,
    STATUS_REVOKED,
    DeviceRecord,
    DeviceRegistry,
)
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import async_wait_until, await_until, finished

pytestmark = pytest.mark.asyncio

GATEWAY = "gw-test-0001"
CLIENT_NONCE = "1" * 64
READY: dict[str, Any] = {
    "op": "ready",
    "v": protocol.PROTOCOL_VERSION,
    "root": "/work",
    "mcp": [],
    "platform": "linux",
    "confinement": "none",
}
_CLOSING = frozenset({WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})


def _fp(seed: int) -> str:
    return install_fingerprint(bytes([seed]) * 32)


@pytest.fixture(autouse=True)
def _isolated_token_secret(tmp_path, _floor_monkeypatch):
    # The credential and the peer key derive from the token secret under the data home.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "data"))


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.events.append((kwargs["outcome"], kwargs["error"]))


class _Broker(ConnectionBroker):
    """The real broker, recording why each connection was detached."""

    def __init__(self) -> None:
        super().__init__()
        self.detached: list[str] = []

    def detach(self, conn: DeviceConnection, why: str) -> None:
        self.detached.append(why)
        super().detach(conn, why)


class _BrokenVault:
    def get_with_revision(self, name: str) -> Any:
        raise OSError("the vault cannot be read")


@pytest.fixture
def gw(tmp_path, monkeypatch):
    previews: dict[str, Any] = {flag.PREVIEW_NAME: True}
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    vault = SecretVault(tmp_path / "vault-home")
    monkeypatch.setattr(vault_doc, "open_vault", lambda: vault)
    monkeypatch.setattr("kiro_crew.gateway_identity.gateway_id", lambda create=True: GATEWAY)
    audit = _Audit()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    broker = _Broker()
    monkeypatch.setattr(broker_mod, "_BROKER", broker)
    return SimpleNamespace(
        previews=previews, registry=DeviceRegistry(vault), broker=broker, audit=audit
    )


def _enroll(registry: DeviceRegistry, seed: int = 1) -> tuple[DeviceRecord, str]:
    rec = registry.enroll(base="laptop", fingerprint=_fp(seed), label="Laptop", owner="me")
    token, _expires = mint(rec, GATEWAY)
    return rec, token


async def enrolled(gw: Any, seed: int = 1) -> tuple[DeviceRecord, str]:
    return await asyncio.to_thread(_enroll, gw.registry, seed)


def _app() -> web.Application:
    app = web.Application()
    remote_routes.register_all(app)
    return app


def _headers(device_id: str, *, nonce: str = CLIENT_NONCE, generation: str | None = None):
    headers = {protocol.DEVICE_HEADER: device_id, protocol.NONCE_HEADER: nonce}
    if generation is not None:
        headers[protocol.GENERATION_HEADER] = generation
    return headers


async def _get(headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(ws_route.CONNECT_PATH, headers=headers)
        return resp.status, await resp.json()


async def refused(headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    return await finished(_get(headers), "the refused upgrade")


async def _get_text(headers: dict[str, str]) -> tuple[int, str]:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(ws_route.CONNECT_PATH, headers=headers)
        return resp.status, await resp.text()


async def _next(ws: Any) -> Any:
    return await finished(ws.receive(), "the next frame from the gateway")


async def _closed(ws: Any) -> int | None:
    msg = await _next(ws)
    assert msg.type in _CLOSING, msg
    return ws.close_code


async def _hello(client: TestClient, proved: DeviceRecord, *, generation: str | None = None):
    """Upgrade, read ``hello`` and check its proof under ``proved``'s peer key."""
    ws = await finished(
        client.ws_connect(
            ws_route.CONNECT_PATH, headers=_headers(proved.device_id, generation=generation)
        ),
        "the upgrade",
    )
    hello = protocol.decode((await _next(ws)).data, allowed=frozenset({"hello"}))
    assert hello["gateway"] == GATEWAY and hello["device"] == proved.device_id
    key = await asyncio.to_thread(peer_key_from_secret, proved, GATEWAY)
    bound = (GATEWAY, proved.device_id, CLIENT_NONCE, hello["nonce"])
    assert proof_matches(key, bound, hello["proof"])
    return ws, bound, SessionFrames(key, bound, gateway=False)


async def _admitted(client: TestClient, rec: DeviceRecord, token: str):
    ws, bound, peer = await _hello(client, rec)
    await ws.send_str(peer.encode(connector_auth(token, bound)))
    await ws.send_str(peer.encode(dict(READY)))
    answer = peer.decode((await _next(ws)).data, allowed=protocol.BEFORE_ADMISSION)
    assert answer == {"op": "admitted"}
    return ws, peer


async def _refusal(ws: Any, peer: SessionFrames, allowed=protocol.BEFORE_ADMISSION) -> str:
    frame = peer.decode((await _next(ws)).data, allowed=allowed)
    assert frame["op"] == "refusal", frame
    return frame["code"]


async def detached(gw: Any) -> list[str]:
    await async_wait_until(lambda: gw.broker.detached, describe=lambda: gw.broker.detached)
    return gw.broker.detached


# -- before the upgrade ----------------------------------------------------------


class TestRefusedBeforeTheUpgrade:
    async def test_preview_off_answers_like_an_unserved_path(self, gw):
        # With the preview off the route answers the router's own 404, as main
        # does for a path it does not serve: no refusal body, no audit event.
        gw.previews.clear()
        status, _text = await finished(_get_text(_headers("laptop")), "the refused upgrade")
        assert status == 404
        assert gw.audit.events == []

    async def test_developer_gate_off_names_it(self, gw, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.session_environment.session_environments_enabled", lambda: False
        )
        status, body = await refused(_headers("laptop"))
        assert status == protocol.REMOTE_OFF_STATUS
        assert body["error"] == flag.DEVELOPER_GATE_REASON

    async def test_a_browser_origin_is_refused(self, gw):
        headers = {**_headers("laptop"), "Origin": "https://example.invalid"}
        status, body = await refused(headers)
        assert (status, body["code"]) == (403, "browser_origin")

    async def test_a_bearer_on_the_upgrade_is_refused(self, gw):
        headers = {**_headers("laptop"), "Authorization": "Bearer x"}
        status, body = await refused(headers)
        assert (status, body["code"]) == (400, "credential_too_early")

    @pytest.mark.parametrize(
        "headers",
        [
            {protocol.NONCE_HEADER: CLIENT_NONCE},
            _headers("d" * 129),
            _headers("laptop", nonce="short"),
            _headers("laptop", nonce="A" * 64),
        ],
        ids=["no-device", "long-device", "short-nonce", "upper-nonce"],
    )
    async def test_a_missing_or_malformed_handshake_is_401(self, gw, headers):
        status, body = await refused(headers)
        assert (status, body["code"]) == (401, "handshake_missing")

    async def test_a_gateway_without_an_id_refuses(self, gw, monkeypatch):
        monkeypatch.setattr("kiro_crew.gateway_identity.gateway_id", lambda create=True: "")
        status, body = await refused(_headers("laptop"))
        assert (status, body["code"]) == (503, "gateway_unknown")

    async def test_unreadable_device_state_refuses(self, gw, monkeypatch):
        monkeypatch.setattr(vault_doc, "open_vault", lambda: _BrokenVault())
        status, body = await refused(_headers("laptop"))
        assert (status, body["code"]) == (503, "state_unreadable")

    async def test_an_unknown_device_is_401(self, gw):
        await enrolled(gw)
        status, body = await refused(_headers("ghost"))
        assert (status, body["code"]) == (401, "unknown_device")

    @pytest.mark.parametrize("generation", ["abc", "0", "2", "1" * 19])
    async def test_a_generation_that_cannot_be_proved_is_401(self, gw, generation):
        rec, _token = await enrolled(gw)
        status, body = await refused(_headers(rec.device_id, generation=generation))
        assert (status, body["code"]) == (401, "unknown_device")

    async def test_a_revoked_record_without_a_generation_is_401(self, gw):
        rec, _token = await enrolled(gw)
        await asyncio.to_thread(gw.registry.revoke, rec.device_id)
        status, body = await refused(_headers(rec.device_id))
        assert (status, body["code"]) == (401, "unknown_device")

    async def test_a_failing_audit_never_decides_the_answer(self, gw, monkeypatch):
        def boom() -> Any:
            raise RuntimeError("audit log unavailable")

        monkeypatch.setattr("kiro_crew.sel.sel", boom)
        headers = {**_headers("laptop"), "Origin": "https://example.invalid"}
        status, _body = await refused(headers)
        assert status == 403


# -- the handshake on the socket -------------------------------------------------


class TestHandshake:
    async def test_admitted_then_a_second_ready_ends_the_socket(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, peer = await _admitted(client, rec, token)
            conn = gw.broker.live(rec.device_id)
            assert conn is not None and conn.root == "/work" and conn.platform == "linux"
            # A ping is progress only; a late frame for an unknown channel is dropped.
            await ws.send_str(peer.encode({"op": "ping"}))
            await ws.send_str(peer.encode({"op": "credit", "ch": 1, "bytes": 1}))
            await ws.send_str(peer.encode(dict(READY)))
            assert await _closed(ws) is not None
        assert await detached(gw) == ["protocol error: ready sent twice"]
        assert gw.broker.live(rec.device_id) is None
        assert ("ok", "") in gw.audit.events

    async def test_the_computer_closing_detaches_it(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, _peer = await _admitted(client, rec, token)
            await finished(ws.close(), "the client close")
            assert await detached(gw) == ["the computer disconnected"]
        assert gw.broker.live(rec.device_id) is None

    async def test_a_binary_frame_after_admission_ends_the_socket(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, _peer = await _admitted(client, rec, token)
            await ws.send_bytes(b"\x00")
            await _closed(ws)
        assert await detached(gw) == ["the computer sent a frame this gateway does not accept"]

    async def test_an_unauthenticated_frame_after_admission_ends_the_socket(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, _peer = await _admitted(client, rec, token)
            await ws.send_str(protocol.encode({"op": "ping"}))
            await _closed(ws)
        (why,) = await detached(gw)
        assert why.startswith("protocol error:")

    async def test_lost_liveness_detaches_without_a_refusal(self, gw, monkeypatch):
        def lost_liveness() -> Liveness:
            liveness = Liveness()
            liveness.lost.set()
            return liveness

        monkeypatch.setattr(ws_route, "_new_liveness", lost_liveness)
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, _peer = await _admitted(client, rec, token)
            await _closed(ws)
        assert await detached(gw) == ["the computer sent nothing authenticated in time"]

    async def test_a_disconnect_sends_an_authenticated_refusal_and_closes(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, peer = await _admitted(client, rec, token)
            # Run beside the reads: the gateway's close waits for this client's reply.
            ending = asyncio.ensure_future(
                remote_routes.end_device(gw.registry, gw.broker, rec.device_id, remove=False)
            )
            assert await _refusal(ws, peer, protocol.AFTER_ADMISSION) == "revoked"
            await _closed(ws)
            ended = await await_until(ending, what="the disconnect")
            assert ended.status == STATUS_REVOKED
        assert (await detached(gw))[0] == "access revoked"

    async def test_a_revoked_computer_hears_why_under_its_old_generation(self, gw):
        rec, token = await enrolled(gw)
        await asyncio.to_thread(gw.registry.revoke, rec.device_id)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec, generation=str(rec.cgen))
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            assert await _refusal(ws, peer) == "revoked"
            assert await _closed(ws) == protocol.CLOSE_CREDENTIAL_REJECTED
        assert ("denied", "revoked") in gw.audit.events

    async def test_a_newer_credential_under_an_older_generation_is_wrong_device(self, gw):
        old, _old_token = await enrolled(gw)
        newer, token = await enrolled(gw)
        assert newer.cgen == old.cgen + 1
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, old, generation=str(old.cgen))
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            assert await _refusal(ws, peer) == "wrong_device"
            assert await _closed(ws) == protocol.CLOSE_CREDENTIAL_REJECTED

    async def test_a_wrong_proof_of_possession_is_refused(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec)
            auth = {**connector_auth(token, bound), "proof": "0" * 64}
            await ws.send_str(peer.encode(auth))
            assert await _refusal(ws, peer) == "invalid_proof"
            assert await _closed(ws) == protocol.CLOSE_CREDENTIAL_REJECTED

    async def test_an_unauthenticated_auth_frame_is_never_answered(self, gw):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, _peer = await _hello(client, rec)
            await ws.send_str(protocol.encode(connector_auth(token, bound)))
            assert await _closed(ws) == protocol.CLOSE_HANDSHAKE_FAILED
        assert gw.broker.live(rec.device_id) is None

    async def test_a_binary_auth_frame_fails_the_handshake(self, gw):
        rec, _token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, _bound, _peer = await _hello(client, rec)
            await ws.send_bytes(b"auth")
            assert await _closed(ws) == protocol.CLOSE_HANDSHAKE_FAILED

    async def test_an_unreadable_state_during_auth_refuses_as_not_now(self, gw, monkeypatch):
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the vault cannot be read")

        monkeypatch.setattr(ws_route, "verify_auth", broken)
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec)
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            assert await _refusal(ws, peer) == "state_unreadable"
            assert await _closed(ws) == protocol.CLOSE_STATE_UNREADABLE

    @pytest.mark.parametrize("root", ["work", "/", "/a/../b"])
    async def test_a_ready_with_a_bad_root_fails_the_handshake(self, gw, root):
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec)
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            await ws.send_str(peer.encode({**READY, "root": root}))
            assert await _closed(ws) == protocol.CLOSE_HANDSHAKE_FAILED
        assert gw.broker.live(rec.device_id) is None

    async def test_an_unreadable_standing_check_refuses_after_ready(self, gw, monkeypatch):
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the vault cannot be read")

        monkeypatch.setattr(ws_route, "verify_standing", broken)
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec)
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            await ws.send_str(peer.encode(dict(READY)))
            assert await _refusal(ws, peer) == "state_unreadable"
            assert await _closed(ws) == protocol.CLOSE_STATE_UNREADABLE
        assert gw.broker.live(rec.device_id) is None

    async def test_a_socket_gone_during_admission_closes_quietly(self, gw, monkeypatch):
        async def gone(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(ws_route, "_admit", gone)
        rec, token = await enrolled(gw)
        async with TestClient(TestServer(_app())) as client:
            ws, bound, peer = await _hello(client, rec)
            await ws.send_str(peer.encode(connector_auth(token, bound)))
            await ws.send_str(peer.encode(dict(READY)))
            await _closed(ws)
        assert gw.broker.detached == []


# -- pure helpers ----------------------------------------------------------------


def _record(*, cgen: int = 2, status: str = STATUS_ACTIVE, device_id: str = "laptop"):
    return DeviceRecord(
        device_id=device_id,
        label="Laptop",
        owner="me",
        fingerprint="a" * 64,
        cgen=cgen,
        status=status,
        created=1.0,
        updated=1.0,
    )


class TestProvedGeneration:
    def test_no_header_proves_an_active_record_as_is(self):
        rec = _record()
        assert ws_route._proved_generation(rec, "") is rec

    def test_no_header_never_proves_an_inactive_record(self):
        assert ws_route._proved_generation(_record(status=STATUS_REVOKED), "") is None

    @pytest.mark.parametrize("header", ["x1", "-1", "0", "3", "9" * 19])
    def test_an_unprovable_header(self, header):
        assert ws_route._proved_generation(_record(), header) is None

    def test_an_older_generation_of_a_revoked_record_is_proved(self):
        proved = ws_route._proved_generation(_record(status=STATUS_REVOKED), "1")
        assert proved is not None and proved.cgen == 1 and proved.status == STATUS_REVOKED


class TestValidRoot:
    @pytest.mark.parametrize("root", ["/work", "/home/me/project"])
    def test_absolute_normalized_roots(self, root):
        assert ws_route.valid_root(root) is True

    @pytest.mark.parametrize("root", ["", "/", "work", "/a/", "/a//b", "/a/./b", "/a\x00b", 7])
    def test_anything_else(self, root):
        assert ws_route.valid_root(root) is False


@pytest.mark.parametrize(
    "code,close",
    [
        ("state_unreadable", protocol.CLOSE_STATE_UNREADABLE),
        ("revoked", protocol.CLOSE_CREDENTIAL_REJECTED),
        ("expired", protocol.CLOSE_CREDENTIAL_REJECTED),
    ],
)
def test_rejection_close_code(code, close):
    assert ws_route._rejection_close_code(CredentialRejected(code)) == close


# -- the final admission ---------------------------------------------------------


@pytest.fixture
def admit(gw, monkeypatch):
    record = _record(cgen=1)
    monkeypatch.setattr(ws_route, "verify_standing", lambda *args, **kwargs: record)
    bound = (GATEWAY, record.device_id, "b" * 64, "c" * 64)
    frames = SessionFrames("d" * 64, bound, gateway=True)
    socket = Mock(spec=web.WebSocketResponse)
    socket.send_str = AsyncMock()

    async def run() -> Any:
        return await finished(
            ws_route._admit(
                gw.broker,
                record.device_id,
                "test-token",
                GATEWAY,
                Mock(spec=DeviceRegistry),
                socket,
                frames,
                lambda current: DeviceConnection(current, AsyncMock(), root="/work"),
            ),
            "the final admission",
        )

    return SimpleNamespace(record=record, socket=socket, run=run, broker=gw.broker)


class TestFinalAdmission:
    async def test_a_standing_record_for_another_device_is_refused(self, admit, monkeypatch):
        other = _record(cgen=1, device_id="desktop")
        monkeypatch.setattr(ws_route, "verify_standing", lambda *args, **kwargs: other)
        result = await admit.run()
        assert isinstance(result, CredentialRejected) and result.code == "wrong_device"
        admit.socket.send_str.assert_not_awaited()

    async def test_an_unreadable_standing_check_refuses(self, admit, monkeypatch):
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the vault cannot be read")

        monkeypatch.setattr(ws_route, "verify_standing", broken)
        result = await admit.run()
        assert isinstance(result, CredentialRejected) and result.code == "state_unreadable"

    async def test_a_newer_live_connection_is_not_replaced(self, admit):
        newer = DeviceConnection(replace(admit.record, cgen=2), AsyncMock(), root="/w")
        admit.broker.attach(newer)
        result = await admit.run()
        assert isinstance(result, CredentialRejected) and result.code == "superseded"
        assert admit.broker.live(admit.record.device_id) is newer
        admit.broker.detach(newer, "test complete")

    async def test_a_failed_admitted_send_detaches_and_answers_none(self, admit):
        admit.socket.send_str.side_effect = ConnectionResetError("gone")
        assert await admit.run() is None
        assert admit.broker.live(admit.record.device_id) is None
        assert admit.broker.detached == ["the computer disconnected"]

    async def test_a_cancelled_admitted_send_detaches_and_propagates(self, admit):
        admit.socket.send_str.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await admit.run()
        assert admit.broker.live(admit.record.device_id) is None
        assert admit.broker.detached == ["the computer disconnected"]


# -- the open socket's recheck ---------------------------------------------------


@pytest.fixture
def watch(gw, monkeypatch):
    monkeypatch.setattr(ws_route, "RECHECK_SECS", 0)
    monkeypatch.setattr(ws_route, "remote_enabled", lambda: True)
    sent: list[dict[str, Any]] = []
    closed: list[bool] = []

    async def sender(text: str) -> None:
        sent.append(json.loads(text))

    async def closer() -> None:
        closed.append(True)

    conn = DeviceConnection(_record(cgen=1), sender, root="/work", closer=closer)
    gw.broker.attach(conn)

    async def run() -> None:
        await finished(
            ws_route._watch(conn, "test-token", GATEWAY, Mock(spec=DeviceRegistry), None),
            "the recheck",
        )

    return SimpleNamespace(conn=conn, sent=sent, closed=closed, run=run, broker=gw.broker)


class TestRecheck:
    async def test_the_switch_turning_off_stops_the_socket(self, watch, monkeypatch):
        monkeypatch.setattr(ws_route, "remote_enabled", lambda: False)
        await watch.run()
        why = "connected computers was turned off on this gateway"
        assert watch.sent == [{"op": "stop", "reason": why}]
        assert watch.closed == [True]
        assert watch.broker.detached == [why]
        assert watch.broker.live("laptop") is None

    async def test_a_revoked_credential_is_an_authenticated_refusal(self, watch, monkeypatch):
        calls: list[int] = []

        def standing(*_args: Any, **_kwargs: Any) -> DeviceRecord:
            calls.append(1)
            if len(calls) == 1:
                # The first recheck still admits; the second meets the Disconnect.
                return watch.conn.record
            raise CredentialRejected("revoked")

        monkeypatch.setattr(ws_route, "verify_standing", standing)
        await watch.run()
        assert len(calls) == 2
        assert watch.sent == [{"op": "refusal", "code": "revoked"}]
        assert watch.broker.detached == ["access ended: revoked"]

    async def test_unreadable_state_ends_the_socket_with_a_stop(self, watch, monkeypatch):
        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the vault cannot be read")

        monkeypatch.setattr(ws_route, "verify_standing", broken)
        await watch.run()
        assert watch.sent == [{"op": "stop", "reason": "connector state became unreadable"}]

    async def test_an_ended_connection_is_not_rechecked(self, watch, monkeypatch):
        def never(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("an ended connection was rechecked")

        monkeypatch.setattr(ws_route, "verify_standing", never)
        watch.broker.detach(watch.conn, "ended first")
        await watch.run()
        assert watch.sent == [] and watch.closed == []
