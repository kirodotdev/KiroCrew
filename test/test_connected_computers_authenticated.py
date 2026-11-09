"""Connector proof-of-possession and connection integrity, with deterministic peers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import test_connected_computers_confinement as handshake_helpers
from test_connected_computers_confinement import (
    GATEWAY,
    _connector,
    _enroll,
    _grant,
    _hello,
)
from test_connected_computers_local import FakeWs

from kiro_crew.connected_computers import credentials, protocol
from kiro_crew.connected_computers.authenticated import SessionFrames
from kiro_crew.connected_computers.broker import ConnectionBroker
from kiro_crew.connected_computers.ws_route import CONNECT_PATH
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

registry = handshake_helpers.registry
gateway_client = handshake_helpers.gateway_client

KEY = "ab" * 32
BOUND = (GATEWAY, "laptop", "0" * 64, "1" * 64)
CREDIT = {"op": "credit", "ch": 1, "bytes": 1}
READY = {
    "op": "ready",
    "v": protocol.PROTOCOL_VERSION,
    "root": "/work",
    "mcp": [],
    "platform": "linux",
    "confinement": "test",
}


def test_captured_auth_is_not_a_bearer_and_cannot_be_replayed(registry):
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    auth = credentials.connector_auth(token, BOUND)
    assert token not in json.dumps(auth)
    assert token.split(".")[1] not in json.dumps(auth)
    assert credentials.verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry) == (
        record,
        token,
    )
    with pytest.raises(credentials.CredentialRejected):
        credentials.verify(auth["payload"], gateway_id=GATEWAY, registry=registry)
    for index in range(4):
        changed = list(BOUND)
        changed[index] = "2" * 64 if index >= 2 else "another-identity"
        with pytest.raises(credentials.CredentialRejected, match="invalid_proof"):
            credentials.verify_auth(
                auth["payload"], auth["proof"], tuple(changed), registry=registry
            )


@pytest.mark.parametrize("action", ["revoke", "remove", "reenroll"])
def test_auth_preserves_enrollment_revocation(registry, action):
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    auth = credentials.connector_auth(token, BOUND)
    if action == "reenroll":
        # A fresh consent moves the generation (a renewal does not).
        registry.enroll(
            base=record.device_id,
            fingerprint=record.fingerprint,
            label="Laptop",
            owner=record.owner,
        )
    else:
        getattr(registry, action)(record.device_id)
    with pytest.raises(credentials.CredentialRejected):
        credentials.verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry)


def test_auth_preserves_expiry(registry, monkeypatch, manual_clock):
    from kiro_crew.dashboard import token_auth_tokens

    manual_clock.install(monkeypatch, token_auth_tokens)
    manual_clock.install(monkeypatch, credentials)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    auth = credentials.connector_auth(token, BOUND)
    manual_clock.advance(credentials.CREDENTIAL_TTL_SECS + 1)
    with pytest.raises(credentials.CredentialRejected, match="expired"):
        credentials.verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry)


@pytest.mark.parametrize("claim", ["owner", "fingerprint", "gateway", "audience", "cgen", "status"])
def test_auth_preserves_all_credential_checks(registry, monkeypatch, claim):
    from kiro_crew.dashboard import token_auth_tokens

    record = _enroll(registry)
    if claim == "audience":
        token = token_auth_tokens.generate_token(record.owner, session=True)
    else:
        token, _ = credentials.mint(record, "other-gateway" if claim == "gateway" else GATEWAY)
    auth = credentials.connector_auth(token, BOUND)
    if claim == "owner":
        monkeypatch.setattr(registry, "get", lambda _: replace(record, owner="another-owner"))
    elif claim == "fingerprint":
        monkeypatch.setattr(registry, "get", lambda _: replace(record, fingerprint="f" * 64))
    elif claim == "cgen":
        monkeypatch.setattr(registry, "get", lambda _: replace(record, cgen=record.cgen + 1))
    elif claim == "status":
        monkeypatch.setattr(registry, "get", lambda _: replace(record, status="revoked"))
    with pytest.raises(credentials.CredentialRejected):
        credentials.verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry)


@pytest.mark.parametrize("gateway", [True, False])
def test_every_frame_is_directional_and_sequence_replay_ends_session(gateway):
    sender = SessionFrames(KEY, BOUND, gateway=gateway)
    receiver = SessionFrames(KEY, BOUND, gateway=not gateway)
    text = sender.encode(CREDIT)
    assert receiver.decode(text, allowed=protocol.FROM_DEVICE) == CREDIT
    with pytest.raises(protocol.ProtocolError):
        receiver.decode(text, allowed=protocol.FROM_DEVICE)
    with pytest.raises(protocol.ProtocolError):
        receiver.decode(sender.encode(CREDIT), allowed=protocol.FROM_DEVICE)
    with pytest.raises(protocol.ProtocolError):
        SessionFrames(KEY, BOUND, gateway=gateway).decode(text, allowed=protocol.FROM_DEVICE)


@pytest.mark.parametrize("field", ["frame", "seq", "mac", "connection"])
def test_session_rejects_tampering_and_other_connections(field):
    sender = SessionFrames(KEY, BOUND, gateway=True)
    receiver = SessionFrames(KEY, BOUND, gateway=False)
    envelope = json.loads(sender.encode(CREDIT))
    if field == "frame":
        envelope["frame"]["bytes"] = 2
    elif field == "seq":
        envelope["seq"] = 1
    elif field == "mac":
        envelope["mac"] = "0" * 64
    else:
        receiver = SessionFrames(KEY, (*BOUND[:3], "2" * 64), gateway=False)
    with pytest.raises(protocol.ProtocolError):
        receiver.decode(json.dumps(envelope), allowed=protocol.FROM_GATEWAY)


def test_session_keeps_data_and_frame_limits():
    sender = SessionFrames(KEY, BOUND, gateway=True)
    receiver = SessionFrames(KEY, BOUND, gateway=False)
    frame = protocol.data_frames(1, b"x" * protocol.MAX_DATA_BYTES)[0]
    text = sender.encode(frame)
    assert len(text.encode()) <= protocol.MAX_FRAME_BYTES
    assert (
        protocol.data_of(receiver.decode(text, allowed=protocol.FROM_GATEWAY))
        == b"x" * protocol.MAX_DATA_BYTES
    )
    with pytest.raises(protocol.ProtocolError):
        sender.encode({"op": "stop", "reason": "x" * protocol.MAX_FRAME_BYTES})
    with pytest.raises(protocol.ProtocolError):
        receiver.decode("x" * (protocol.MAX_FRAME_BYTES + 1), allowed=protocol.FROM_GATEWAY)


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["injection", "tamper", "replay"])
async def test_connector_closes_on_invalid_frames_before_dispatch(attack):
    grant = _grant()
    ws = FakeWs([_hello(grant)], protect=False)
    cached = []

    def frame(_headers):
        assert ws.frames is not None
        if attack == "injection":
            return protocol.encode({"op": "open", "ch": 1, "kind": "workspace"})
        if cached:
            return cached[0]
        text = ws.frames.encode(CREDIT)
        if attack == "tamper":
            envelope = json.loads(text)
            envelope["frame"]["bytes"] = 2
            text = json.dumps(envelope)
        cached.append(text)
        return text

    ws._incoming.extend([frame, frame] if attack == "replay" else [frame])
    connector, _ = _connector(grant, ws)
    connector._handle = AsyncMock()
    result = await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert "protocol error" in result
    assert ws.closed
    assert connector._handle.await_count == (1 if attack == "replay" else 0)
    assert grant.credential not in "".join(ws.wire_sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["injection", "tamper", "replay", "ready"])
async def test_gateway_closes_on_invalid_frames(gateway_client, registry, monkeypatch, attack):
    from kiro_crew.connected_computers import ws_route

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(
        CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: record.device_id, protocol.NONCE_HEADER: BOUND[2]},
    ) as ws:
        hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
        bound = (GATEWAY, record.device_id, BOUND[2], hello["nonce"])
        frames = SessionFrames(
            credentials.peer_key_from_secret(record, GATEWAY), bound, gateway=False
        )
        await ws.send_str(frames.encode(credentials.connector_auth(token, bound)))
        if attack == "ready":
            await ws.send_str(protocol.encode(READY))
        else:
            await ws.send_str(frames.encode(READY))
            text = frames.encode(CREDIT)
            if attack == "injection":
                text = protocol.encode(CREDIT)
            elif attack == "tamper":
                envelope = json.loads(text)
                envelope["frame"]["bytes"] = 2
                text = json.dumps(envelope)
            elif attack == "replay":
                await ws.send_str(text)
            await ws.send_str(text)
        closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
        if attack != "ready":
            # Admitted after a valid ready: the attack frame then ends it.
            assert frames.decode(closing.data, allowed=protocol.BEFORE_ADMISSION) == {
                "op": "admitted"
            }
            closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
        assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert broker.live(record.device_id) is None


@pytest.mark.asyncio
async def test_gateway_rejects_captured_auth_on_a_fresh_socket(gateway_client, registry):
    """A replayed auth fails the new socket's MAC: closed, and never refused (no grant end)."""
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    key = credentials.peer_key_from_secret(record, GATEWAY)
    captured = None
    for nonce in ("0" * 64, "2" * 64):
        async with gateway_client.ws_connect(
            CONNECT_PATH,
            headers={protocol.DEVICE_HEADER: record.device_id, protocol.NONCE_HEADER: nonce},
        ) as ws:
            hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
            bound = (GATEWAY, record.device_id, nonce, hello["nonce"])
            if captured is None:
                frames = SessionFrames(key, bound, gateway=False)
                captured = frames.encode(credentials.connector_auth(token, bound))
            else:
                await ws.send_str(captured)
                closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
                assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
                assert ws.close_code == protocol.CLOSE_HANDSHAKE_FAILED


@pytest.mark.asyncio
async def test_gateway_answers_a_valid_handshake_with_authenticated_admission(
    gateway_client, registry, monkeypatch
):
    from kiro_crew.connected_computers import ws_route

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(
        CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: record.device_id, protocol.NONCE_HEADER: BOUND[2]},
    ) as ws:
        hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
        bound = (GATEWAY, record.device_id, BOUND[2], hello["nonce"])
        frames = SessionFrames(
            credentials.peer_key_from_secret(record, GATEWAY), bound, gateway=False
        )
        await ws.send_str(frames.encode(credentials.connector_auth(token, bound)))
        await ws.send_str(frames.encode(READY))
        first = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert json.loads(first)["seq"] == 0
        assert frames.decode(first, allowed=protocol.BEFORE_ADMISSION) == {"op": "admitted"}
        assert broker.live(record.device_id) is not None


@pytest.mark.asyncio
async def test_real_connector_and_gateway_exchange_authenticated_refusal_and_stop(
    gateway_client, registry, monkeypatch
):
    from kiro_crew.connected_computers import flag, ws_route
    from kiro_crew.connected_computers.broker import ChannelInterrupted
    from kiro_crew.connected_computers.connector import Connector
    from kiro_crew.testing.wait import async_wait_until

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    monkeypatch.setattr(flag, "local_enabled", lambda cfg=None: False)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    grant = _grant(
        gateway_id=GATEWAY,
        device_id=record.device_id,
        credential=token,
        peer_key=credentials.peer_key_from_secret(record, GATEWAY),
    )
    connector = Connector(
        grant,
        url=str(gateway_client.make_url(CONNECT_PATH).with_scheme("ws")),
        plan=type("Plan", (), {"name": "test"})(),
        grant_still_active=lambda: False,
    )
    task = asyncio.create_task(connector.run())
    try:
        await async_wait_until(lambda: broker.live(record.device_id) is not None)
        conn = broker.live(record.device_id)
        assert conn is not None and conn.root == grant.root
        with pytest.raises(ChannelInterrupted, match="turned off"):
            await asyncio.wait_for(conn.open("workspace", "test-session"), FALLBACK_TIMEOUT_SECS)
        await asyncio.wait_for(
            broker.disconnect(record.device_id, "test complete"), FALLBACK_TIMEOUT_SECS
        )
        assert "test complete" in await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
        assert connector.ended.is_set()
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)


def test_unsigned_payload_cannot_change_credential_claims(registry):
    from kiro_crew.dashboard.token_auth_tokens import _b64url_decode, _b64url_encode

    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    auth = credentials.connector_auth(token, BOUND)
    payload = json.loads(_b64url_decode(auth["payload"]))
    payload["session_exp"] += credentials.CREDENTIAL_TTL_SECS
    changed = _b64url_encode(json.dumps(payload).encode())
    with pytest.raises(credentials.CredentialRejected, match="invalid_proof"):
        credentials.verify_auth(changed, auth["proof"], BOUND, registry=registry)


@pytest.mark.parametrize("sequence", [True, -1, 1, 1 << 64, "0"])
def test_invalid_sequence_types_and_bounds_are_refused(sequence):
    sender = SessionFrames(KEY, BOUND, gateway=True)
    receiver = SessionFrames(KEY, BOUND, gateway=False)
    envelope = json.loads(sender.encode(CREDIT))
    envelope["seq"] = sequence
    with pytest.raises(protocol.ProtocolError):
        receiver.decode(json.dumps(envelope), allowed=protocol.FROM_GATEWAY)


@pytest.mark.parametrize("nonce", ["0" * 64 + "\n", "0" * 63, "G" * 64, True])
def test_nonce_and_mac_shape_is_exact(nonce):
    assert not protocol.valid_nonce(nonce)


def test_bearer_auth_is_refused_and_dashboard_revocation_state_is_not_read(registry, monkeypatch):
    from kiro_crew.dashboard import token_auth

    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(
            protocol.encode({"op": "auth", "credential": token}), allowed=protocol.AUTH_ONLY
        )
    auth = credentials.connector_auth(token, BOUND)
    # Revocation is the device record's: the dashboard's counter does not decide.
    monkeypatch.setattr(token_auth, "current_revocation_gen_or_none", lambda: None)
    assert credentials.verify_auth(auth["payload"], auth["proof"], BOUND, registry=registry) == (
        record,
        token,
    )


# -- authenticated liveness: a relay that drops the tail cannot hold the connection ------


class _FakeTime:
    """An injected clock whose sleepers wake only when the test advances it.

    Several tasks may sleep at once (the watchdog, the pinger, a send bound);
    each wakes when the clock reaches its own time, in the order it slept.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now, seconds, future))
        await future

    def parked(self, seconds: float) -> bool:
        """A task sleeps ``seconds`` from now (the pinger, between heartbeats)."""
        return any(
            not future.done() and start == self.now and wait == seconds
            for start, wait, future in self._sleepers
        )

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for start, wait, future in self._sleepers:
            if start + wait <= self.now and not future.done():
                future.set_result(None)
        self._sleepers = [entry for entry in self._sleepers if not entry[2].done()]


async def _next_heartbeat(fake: _FakeTime) -> None:
    """Advance one interval once the pinger is parked for it."""
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(lambda: fake.parked(protocol.HEARTBEAT_SECS))
    fake.advance(protocol.HEARTBEAT_SECS)


class _SilentAfterWs(FakeWs):
    """Delivers its frames, then nothing: the transport stays up, the tail is dropped."""

    async def receive(self, timeout=None):
        if not self._incoming:
            # Cancelled by the connector's liveness; the test bounds the whole run.
            await asyncio.Event().wait()
        return await super().receive(timeout)


def test_liveness_counts_only_authenticated_progress():
    from kiro_crew.connected_computers.authenticated import Liveness

    fake = _FakeTime()
    liveness = Liveness(clock=fake.clock, sleep=fake.sleep)
    assert (liveness.interval, liveness.deadline, liveness.send_timeout) == (
        protocol.HEARTBEAT_SECS,
        protocol.LIVENESS_DEADLINE_SECS,
        protocol.LIVENESS_DEADLINE_SECS,
    )
    fake.now = protocol.LIVENESS_DEADLINE_SECS - 1
    assert not liveness.stale()
    liveness.progress()
    fake.now += protocol.LIVENESS_DEADLINE_SECS - 1
    assert not liveness.stale(), "progress restarts the deadline"
    fake.now += 1
    assert liveness.stale()


@pytest.mark.asyncio
async def test_the_gateways_authenticated_pings_keep_the_connector_alive():
    """Each authenticated ping is progress: four intervals with pings never drop it."""
    from kiro_crew.connected_computers.authenticated import Liveness
    from kiro_crew.testing.wait import async_wait_until

    fake = _FakeTime()
    liveness = Liveness(clock=fake.clock, sleep=fake.sleep)
    sent: list[float] = []

    async def ping() -> None:
        sent.append(fake.now)
        liveness.progress()  # the peer's answer arrived

    task = asyncio.create_task(liveness.run(ping))
    try:
        for _ in range(4):
            await _next_heartbeat(fake)
        await async_wait_until(lambda: len(sent) == 4)
        assert not liveness.lost.is_set()
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
    assert sent == [15.0, 30.0, 45.0, 60.0]


@pytest.mark.asyncio
async def test_the_watchdog_fires_while_a_heartbeat_send_is_blocked():
    """A relay stops reading: the send holds the send lock forever; the deadline still ends it."""
    from kiro_crew.connected_computers.authenticated import Liveness
    from kiro_crew.testing.wait import await_until

    fake = _FakeTime()
    liveness = Liveness(
        clock=fake.clock, sleep=fake.sleep, send_timeout=10_000.0
    )  # flake-ok: fake clock, never slept
    send_lock = asyncio.Lock()
    started = asyncio.Event()
    abandoned = asyncio.Event()

    async def stalled_ping() -> None:
        async with send_lock:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                abandoned.set()
                raise

    task = asyncio.create_task(liveness.run(stalled_ping))
    try:
        await _next_heartbeat(fake)
        await await_until(started, what="the heartbeat send to stall")
        assert send_lock.locked()
        fake.advance(protocol.LIVENESS_DEADLINE_SECS - protocol.HEARTBEAT_SECS)
        await await_until(liveness.lost, what="the watchdog to fire")
        await await_until(task, what="the heartbeat to end")
        # Abandoned: cancelled, never awaited by the watchdog or the pinger.
        await await_until(abandoned, what="the stalled send to be cancelled")
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
    assert fake.now == protocol.LIVENESS_DEADLINE_SECS, "measured from the last progress"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["overrun", "error"])
async def test_a_heartbeat_send_that_overruns_or_fails_drops_the_connection(failure):
    from kiro_crew.connected_computers.authenticated import Liveness
    from kiro_crew.testing.wait import async_wait_until, await_until

    fake = _FakeTime()
    liveness = Liveness(
        clock=fake.clock, sleep=fake.sleep, send_timeout=5.0
    )  # flake-ok: fake clock, never slept
    started = asyncio.Event()

    async def ping() -> None:
        started.set()
        if failure == "error":
            raise ConnectionResetError("the transport is gone")
        await asyncio.Event().wait()

    task = asyncio.create_task(liveness.run(ping))
    try:
        await _next_heartbeat(fake)
        await await_until(started, what="the heartbeat send")
        if failure == "overrun":
            await async_wait_until(lambda: fake.parked(5.0))
            fake.advance(5.0)
        await await_until(liveness.lost, what="the bounded send to drop the connection")
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
    assert fake.now == (20.0 if failure == "overrun" else 15.0), "before the 45 s deadline"


def _silent_connector(grant, ws, fake):
    from kiro_crew.connected_computers.connector import Connector

    return Connector(
        grant,
        url="ws://127.0.0.1:1/x",
        plan=handshake_helpers._NoFencePlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: handshake_helpers.FakeSession(ws),
        clock=fake.clock,
        sleep=fake.sleep,
    )


class _Child:
    """A running tool server stand-in: records its stop."""

    def __init__(self, stopped: list[int]) -> None:
        self._stopped = stopped

    async def stop(self) -> None:
        self._stopped.append(1)


@pytest.mark.asyncio
async def test_connector_drops_a_gateway_whose_authenticated_tail_stopped():
    """The relay keeps the socket (and its WebSocket pongs) alive but drops every frame."""
    from kiro_crew.testing.wait import async_wait_until

    grant = _grant()
    ws = _SilentAfterWs([_hello(grant)])
    fake = _FakeTime()
    connector = _silent_connector(grant, ws, fake)
    stopped: list[int] = []
    task = asyncio.create_task(connector.run())
    try:
        await async_wait_until(lambda: connector.admitted)
        connector._channels[1] = _Child(stopped)  # a running tool server
        assert [frame["op"] for frame in ws.sent] == ["auth", "ready"], "no ping before admission"
        for _ in range(3):
            await _next_heartbeat(fake)
        result = await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
    assert "nothing authenticated" in result, "interrupted, never an AuthenticatedRefusal"
    assert [frame["op"] for frame in ws.sent] == ["auth", "ready", "ping", "ping"]
    assert stopped == [1], "its processes stopped as on any disconnect"
    assert not connector.protocol_failed


@pytest.mark.asyncio
async def test_lost_liveness_cancels_a_blocked_handler_and_stops_running_channels():
    """An ``open`` stalls (holding the send lock); the deadline still ends the run."""
    from kiro_crew.testing.wait import async_wait_until, await_until

    grant = _grant()
    ws = _SilentAfterWs([_hello(grant), json.dumps({"op": "open", "ch": 2, "kind": "workspace"})])
    fake = _FakeTime()
    connector = _silent_connector(grant, ws, fake)
    stopped: list[int] = []
    started = asyncio.Event()
    handler: list[str] = []

    async def stalled_handle(frame) -> None:
        async with connector._send_lock:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                handler.append("cancelled")
                raise

    connector._handle = stalled_handle
    task = asyncio.create_task(connector.run())
    try:
        await async_wait_until(lambda: connector.admitted)
        connector._channels[1] = _Child(stopped)  # already running before the stall
        await await_until(started, what="the open to stall")
        fake.advance(protocol.LIVENESS_DEADLINE_SECS)
        result = await asyncio.wait_for(task, FALLBACK_TIMEOUT_SECS)
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), FALLBACK_TIMEOUT_SECS)
    assert "nothing authenticated" in result
    assert handler == ["cancelled"], "the stalled handler was cancelled"
    assert stopped == [1], "the running channel stopped without waiting for the handler"
    assert connector._channels == {} and connector._closing


@pytest.mark.asyncio
async def test_an_open_prepared_after_liveness_was_lost_never_spawns(monkeypatch):
    """The last re-check: a wrap that finishes after the deadline starts nothing."""
    from kiro_crew.connected_computers import connector as connector_mod
    from kiro_crew.connected_computers.authenticated import Liveness
    from kiro_crew.connected_computers.confinement import Confined

    grant = _grant()
    connector = _silent_connector(grant, FakeWs([]), _FakeTime())
    connector._liveness = Liveness()
    sent: list[dict] = []

    async def send(frame) -> None:
        sent.append(frame)

    async def wrap_then_lose(plan, argv, **kw):
        connector._liveness.lost.set()  # the deadline passed during the wrap
        return Confined(argv, {}, kw["root"])

    async def never_spawn(_confined):
        raise AssertionError("spawned after liveness was lost")

    monkeypatch.setattr(connector, "_send", send)
    monkeypatch.setattr(connector_mod, "wrap_off_loop", wrap_then_lose)
    monkeypatch.setattr(connector_mod, "_spawn", never_spawn)
    monkeypatch.setattr("kiro_crew.connected_computers.flag.local_enabled", lambda cfg=None: True)
    await connector._open(1, "workspace")
    assert connector._channels == {} and sent == []


@pytest.mark.asyncio
async def test_gateway_drops_a_computer_whose_authenticated_tail_stopped(
    gateway_client, registry, monkeypatch
):
    """The computer's frames stop arriving after ready; the WebSocket itself stays open."""
    from kiro_crew.connected_computers import ws_route
    from kiro_crew.connected_computers.authenticated import Liveness

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    fake = _FakeTime()
    monkeypatch.setattr(
        ws_route, "_new_liveness", lambda: Liveness(clock=fake.clock, sleep=fake.sleep)
    )
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(
        CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: record.device_id, protocol.NONCE_HEADER: BOUND[2]},
    ) as ws:
        hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
        bound = (GATEWAY, record.device_id, BOUND[2], hello["nonce"])
        frames = SessionFrames(
            credentials.peer_key_from_secret(record, GATEWAY), bound, gateway=False
        )
        await ws.send_str(frames.encode(credentials.connector_auth(token, bound)))
        await ws.send_str(frames.encode(READY))
        admitted = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(admitted, allowed=protocol.BEFORE_ADMISSION) == {"op": "admitted"}
        assert broker.live(record.device_id) is not None
        pings = []
        for _ in range(2):
            await _next_heartbeat(fake)
            text = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
            pings.append(frames.decode(text, allowed=protocol.AFTER_ADMISSION))
        assert pings == [{"op": "ping"}, {"op": "ping"}], "authenticated, sequenced heartbeats"
        await _next_heartbeat(fake)
        closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
        assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.close_code not in (
            protocol.CLOSE_CREDENTIAL_REJECTED,
            protocol.CLOSE_STATE_UNREADABLE,
        ), "a missed heartbeat is never a refusal"
    assert broker.live(record.device_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "remove"])
async def test_a_disconnected_computer_hears_an_authenticated_refusal(
    gateway_client, registry, action
):
    """The generation header lets the gateway prove the OLD peer key, so it can say why."""
    from kiro_crew.connected_computers.connector import AuthenticatedRefusal, Connector

    record = _enroll(registry)
    token, expires = credentials.mint(record, GATEWAY)
    grant = _grant(
        gateway_id=GATEWAY,
        device_id=record.device_id,
        credential=token,
        credential_expires=expires,
        peer_key=credentials.peer_key_from_secret(record, GATEWAY),
    )
    getattr(registry, action)(record.device_id)
    connector = Connector(
        grant,
        url=str(gateway_client.make_url(CONNECT_PATH)),
        plan=handshake_helpers._NoFencePlan(),
        grant_still_active=lambda: True,
        session_factory=lambda: handshake_helpers._BorrowedSession(gateway_client.session),
    )
    with pytest.raises(AuthenticatedRefusal) as refused:
        await asyncio.wait_for(connector.run(), FALLBACK_TIMEOUT_SECS)
    assert refused.value.code == ("revoked" if action == "revoke" else "removed")
    assert refused.value.terminal and connector.admitted is False


@pytest.mark.asyncio
async def test_a_generation_newer_than_the_record_is_refused_before_upgrade(
    gateway_client, registry
):
    record = _enroll(registry)
    response = await gateway_client.get(
        CONNECT_PATH,
        headers={
            protocol.DEVICE_HEADER: record.device_id,
            protocol.NONCE_HEADER: BOUND[2],
            protocol.GENERATION_HEADER: str(record.cgen + 1),
        },
    )
    assert response.status == 401


# -- final admission is fenced against Disconnect and Remove -----------------------------


async def _open_handshake(ws, record, token):
    """Read hello and send auth; the session frames for the rest of the socket."""
    hello = json.loads(await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS))
    bound = (GATEWAY, record.device_id, BOUND[2], hello["nonce"])
    frames = SessionFrames(credentials.peer_key_from_secret(record, GATEWAY), bound, gateway=False)
    await ws.send_str(frames.encode(credentials.connector_auth(token, bound)))
    return frames


def _connect_headers(record) -> dict:
    return {protocol.DEVICE_HEADER: record.device_id, protocol.NONCE_HEADER: BOUND[2]}


def _quiet_liveness(monkeypatch, ws_route) -> None:
    """No heartbeat or deadline while a test reads frames in a fixed order."""
    from kiro_crew.connected_computers.authenticated import Liveness

    async def parked(_seconds: float) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(ws_route, "_new_liveness", lambda: Liveness(sleep=parked))


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True], ids=["disconnect", "remove"])
async def test_an_end_between_auth_and_ready_refuses_admission(
    gateway_client, registry, monkeypatch, remove
):
    """auth verified -> Disconnect/Remove -> ready: never admitted, no channel opens."""
    from kiro_crew.connected_computers import remote_routes, ws_route
    from kiro_crew.connected_computers.broker import DeviceOffline

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    authenticated = asyncio.Event()
    real_authenticate = ws_route._authenticate

    async def observed(*args, **kwargs):
        result = await real_authenticate(*args, **kwargs)
        authenticated.set()
        return result

    monkeypatch.setattr(ws_route, "_authenticate", observed)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(CONNECT_PATH, headers=_connect_headers(record)) as ws:
        frames = await _open_handshake(ws, record, token)
        await asyncio.wait_for(authenticated.wait(), FALLBACK_TIMEOUT_SECS)
        ended = await asyncio.wait_for(
            remote_routes.end_device(registry, broker, record.device_id, remove=remove),
            FALLBACK_TIMEOUT_SECS,
        )
        assert ended is not None and not ended.active
        await ws.send_str(frames.encode(READY))
        answer = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(answer, allowed=protocol.BEFORE_ADMISSION) == {
            "op": "refusal",
            "code": "removed" if remove else "revoked",
        }
        closing = await ws.receive(timeout=FALLBACK_TIMEOUT_SECS)
        assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.close_code == protocol.CLOSE_CREDENTIAL_REJECTED
    assert broker.live(record.device_id) is None
    with pytest.raises(DeviceOffline):
        await broker.open_channel(record.device_id, "workspace", session_key="s")
    assert broker.admission_waiters(record.device_id) == 0


@pytest.mark.asyncio
async def test_an_end_after_attach_drops_the_admitted_connection(
    gateway_client, registry, monkeypatch
):
    from kiro_crew.connected_computers import remote_routes, ws_route

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    _quiet_liveness(monkeypatch, ws_route)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(CONNECT_PATH, headers=_connect_headers(record)) as ws:
        frames = await _open_handshake(ws, record, token)
        await ws.send_str(frames.encode(READY))
        first = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(first, allowed=protocol.BEFORE_ADMISSION) == {"op": "admitted"}
        assert broker.live(record.device_id) is not None
        await asyncio.wait_for(
            remote_routes.end_device(registry, broker, record.device_id, remove=False),
            FALLBACK_TIMEOUT_SECS,
        )
        assert broker.live(record.device_id) is None
        answer = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(answer, allowed=protocol.AFTER_ADMISSION) == {
            "op": "refusal",
            "code": "revoked",
        }


@pytest.mark.asyncio
async def test_a_disconnect_racing_the_admission_recheck_waits_then_ends_it(
    gateway_client, registry, monkeypatch
):
    """A Disconnect made while the re-check holds the fence lands after the attach."""
    import threading

    from kiro_crew.connected_computers import remote_routes, ws_route
    from kiro_crew.testing.wait import async_wait_until

    broker = ConnectionBroker()
    monkeypatch.setattr(ws_route, "get_broker", lambda: broker)
    _quiet_liveness(monkeypatch, ws_route)
    entered = threading.Event()
    go = threading.Event()
    real_standing = ws_route.verify_standing

    def held_standing(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert go.wait(FALLBACK_TIMEOUT_SECS), "the test never released the re-check"
        return real_standing(*args, **kwargs)

    monkeypatch.setattr(ws_route, "verify_standing", held_standing)
    record = _enroll(registry)
    token, _ = credentials.mint(record, GATEWAY)
    async with gateway_client.ws_connect(CONNECT_PATH, headers=_connect_headers(record)) as ws:
        frames = await _open_handshake(ws, record, token)
        await ws.send_str(frames.encode(READY))
        await async_wait_until(entered.is_set)
        ending = asyncio.ensure_future(
            remote_routes.end_device(registry, broker, record.device_id, remove=False)
        )
        try:
            # The re-check holds the fence; the Disconnect queues behind it, unwritten.
            await async_wait_until(lambda: broker.admission_waiters(record.device_id) == 2)
            assert (await asyncio.to_thread(registry.get, record.device_id)).active
        finally:
            go.set()
        first = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(first, allowed=protocol.BEFORE_ADMISSION) == {"op": "admitted"}
        second = await ws.receive_str(timeout=FALLBACK_TIMEOUT_SECS)
        assert frames.decode(second, allowed=protocol.AFTER_ADMISSION) == {
            "op": "refusal",
            "code": "revoked",
        }
        assert (await asyncio.wait_for(ending, FALLBACK_TIMEOUT_SECS)) is not None
    assert broker.live(record.device_id) is None
