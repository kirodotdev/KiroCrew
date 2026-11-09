"""Remote gateway: ``GET /api/connected-computers/connect``, the connector's socket.

Self-authenticating (``token_auth_paths.CONNECTOR_CONNECT_PATH`` keeps the
dashboard gate off it, GET only), so everything that admits a connector is here
and runs BEFORE the upgrade:

1. the ``connected_computers`` preview is on (else 403, nothing upgraded);
2. no ``Origin`` header: a browser page never opens a connector socket;
3. no ``Authorization`` header (400 ``credential_too_early``): bearer
   credentials never travel on the socket;
4. a well-formed device id and client nonce, and a device record here (else
   401, nothing upgraded). The optional :data:`.protocol.GENERATION_HEADER`
   names the generation of the credential the computer holds; it must not be
   newer than the record's. Without it the record must be active.

Then, on the socket: ``hello`` with this gateway's nonce and its proof under the
peer key of that generation (:func:`.credentials.gateway_proof`), so a computer
that was disconnected or removed still hears why; the connector's ``auth``
proves possession without sending the bearer. The locally reconstructed
credential must verify in full for the SAME device and enrollment. Every frame
after ``hello``, ``auth`` and ``ready`` included, uses a directional session
MAC and consecutive sequence number. After ``ready`` the record is read again
inside the device's admission fence (:meth:`.broker.ConnectionBroker.admission`),
which Disconnect and Remove also hold, so one made while the handshake was
pending refuses it, and one made later finds and ends the attached connection.
The gateway answers with an authenticated
``admitted`` once the computer is attached, or an authenticated ``refusal``
carrying the :class:`.credentials.CredentialRejected` code before it closes;
the close code (4401 / 4503 / 4400) is only a diagnostic, since a relay can
forge it. A frame that fails its MAC is never answered with a refusal. No
dashboard cookie or token is consulted.

The peer address is never evidence: the SSH local forward delivers the connector
from a loopback peer, exactly as it would deliver anything else.

While the socket is open the device record is re-checked every
:data:`RECHECK_SECS` (:func:`.credentials.verify_standing`; expiry does not end
an admitted socket), and the switch re-read, so a revocation, a removal or the
switch turning off ends the socket -- and with it every channel, as interrupted
-- without waiting for the computer. A credential refused then is sent as an
authenticated ``refusal``; the switch turning off is an ordinary ``stop``.

Once admitted, both sides need authenticated progress
(:class:`.authenticated.Liveness`): this gateway pings every
:data:`.protocol.HEARTBEAT_SECS`, and a computer that sent no authenticated
frame for :data:`.protocol.LIVENESS_DEADLINE_SECS` is detached as disconnected,
its channels interrupted. A relay keeping only the WebSocket's own pings and
pongs alive cannot hold the connection open. That is never a refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
import secrets
from dataclasses import replace
from typing import Any, Awaitable, Callable, Final, Optional

from aiohttp import WSMsgType, web

from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.authenticated import Liveness, SessionFrames
from kiro_crew.connected_computers.broker import (
    ConnectionBroker,
    DeviceConnection,
    get_broker,
)
from kiro_crew.connected_computers.credentials import (
    CredentialRejected,
    gateway_proof,
    peer_key_from_secret,
    verify_auth,
    verify_standing,
)
from kiro_crew.connected_computers.devices import DeviceRecord, DeviceRegistry
from kiro_crew.connected_computers.flag import remote_enabled, remote_refusal
from kiro_crew.connected_computers.protocol import REMOTE_OFF_STATUS

logger = logging.getLogger(__name__)

CONNECT_PATH: Final[str] = "/api/connected-computers/connect"
#: How often an open socket re-checks its credential and the switch.
RECHECK_SECS: Final[float] = 15.0
#: How long the connector has to send ``ready`` after ``hello``.
READY_TIMEOUT_SECS: Final[float] = 15.0
#: Longest the ``admitted`` send may take while the admission fence is held.
ADMIT_SEND_SECS: Final[float] = 5.0


def _audit(request: web.Request, outcome: str, detail: str, device: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=device or "connector",
            operation="connected_computer_connect",
            outcome=outcome,
            source="connected_computers",
            resources=CONNECT_PATH,
            error=detail if outcome != "ok" else "",
        )
    except Exception:  # noqa: BLE001 -- audit must not decide the connection
        logger.debug("connected computers: audit failed", exc_info=True)


def _refuse(request: web.Request, status: int, code: str, message: str) -> web.Response:
    _audit(request, "denied", code)
    return web.json_response({"error": message, "code": code}, status=status)


def _registry() -> DeviceRegistry:
    from kiro_crew.connected_computers.vault_doc import open_vault

    return DeviceRegistry(open_vault())


def _gateway_id() -> str:
    from kiro_crew.gateway_identity import gateway_id

    return gateway_id(create=False)


def _new_liveness() -> Liveness:
    """One connection's liveness on the real clock (tests replace this binding)."""
    return Liveness()


def _closer_for(ws: web.WebSocketResponse) -> Callable[[], Awaitable[None]]:
    """Adapt ``ws.close`` (which returns a bool) to the broker's closer shape."""

    async def close() -> None:
        await ws.close()

    return close


async def _refuse_authenticated(
    ws: web.WebSocketResponse, frames: SessionFrames, code: str, close_code: int
) -> None:
    """Tell the computer why in an authenticated frame, then close (code is diagnostic)."""
    with contextlib.suppress(Exception):
        await ws.send_str(frames.encode({"op": "refusal", "code": code}))
    await ws.close(code=close_code, message=code.encode())


def _rejection_close_code(exc: CredentialRejected) -> int:
    """4401 refuses the credential; an unreadable revocation state is only "not now"."""
    if exc.code == "state_unreadable":
        return protocol.CLOSE_STATE_UNREADABLE
    return protocol.CLOSE_CREDENTIAL_REJECTED


def valid_root(root: str) -> bool:
    """A root the gateway can show and match: absolute, normalized POSIX."""
    return (
        isinstance(root, str)
        and root.startswith("/")
        and root != "/"
        and posixpath.normpath(root) == root
        and all(ch.isprintable() for ch in root)
    )


async def _receive(
    ws: web.WebSocketResponse, allowed: frozenset[str], frames: SessionFrames | None = None
) -> dict[str, Any]:
    msg = await ws.receive(timeout=READY_TIMEOUT_SECS)
    if msg.type != WSMsgType.TEXT:
        raise protocol.ProtocolError("expected a text frame")
    return (frames.decode if frames is not None else protocol.decode)(msg.data, allowed=allowed)


async def _authenticate(
    ws: web.WebSocketResponse,
    claimed: DeviceRecord,
    bound: tuple[str, str, str, str],
    registry: DeviceRegistry,
    frames: SessionFrames,
) -> tuple[DeviceRecord, str]:
    """Require possession of the credential for this exact handshake and enrollment.

    ``claimed`` carries the generation the hello was proved under. The full
    verification refuses a revoked, removed or replaced record by name.
    """
    auth = await _receive(ws, protocol.AUTH_ONLY, frames)
    record, token = await asyncio.to_thread(
        verify_auth, auth["payload"], auth["proof"], bound, registry=registry
    )
    if record.device_id != claimed.device_id or record.cgen != claimed.cgen:
        raise CredentialRejected("wrong_device", "this credential belongs to another enrollment")
    return record, token


def _proved_generation(record: DeviceRecord, header: str) -> Optional[DeviceRecord]:
    """``record`` at the generation the computer named, or None when it cannot be proved.

    No header: the current generation of an active record (an older client).
    A header: any generation up to the record's, active or not, so the
    computer of a revoked or removed record gets the authenticated refusal.
    """
    if not header:
        return record if record.active else None
    if not header.isdigit() or len(header) > 18:
        return None
    generation = int(header)
    if not 1 <= generation <= record.cgen:
        return None
    return replace(record, cgen=generation)


async def api_connector_connect(request: web.Request) -> web.StreamResponse:
    # Off the loop: without a live config snapshot the switch is read from disk.
    off = await asyncio.to_thread(remote_refusal)
    if off is not None:
        # 503, not 403: this gateway is off, the computer is not refused. Its
        # local grant waits and retries (controller.REMOTE_OFF_STATUS); a 401/403
        # would end it, and turning the preview back on would then need a re-share.
        return _refuse(request, REMOTE_OFF_STATUS, "connected_computers_disabled", off)
    if request.headers.get("Origin"):
        return _refuse(request, 403, "browser_origin", "a browser cannot open a connector")
    if request.headers.get("Authorization"):
        return _refuse(
            request,
            400,
            "credential_too_early",
            "bearer credentials are not accepted on the connector socket",
        )
    device_id = request.headers.get(protocol.DEVICE_HEADER, "")
    client_nonce = request.headers.get(protocol.NONCE_HEADER, "")
    if not device_id or len(device_id) > 128 or not protocol.valid_nonce(client_nonce):
        return _refuse(request, 401, "handshake_missing", "a connector handshake is required")
    registry = _registry()
    # A cache miss reads the gateway id file: off the loop.
    gateway = await asyncio.to_thread(_gateway_id)
    if not gateway:
        return _refuse(request, 503, "gateway_unknown", "this gateway has no id")
    try:
        claimed = await asyncio.to_thread(registry.get, device_id)
    except Exception:  # noqa: BLE001 -- unreadable state refuses
        logger.warning("connected computers: device lookup failed", exc_info=True)
        return _refuse(request, 503, "state_unreadable", "connector state is unreadable")
    proved = (
        None
        if claimed is None
        else _proved_generation(claimed, request.headers.get(protocol.GENERATION_HEADER, ""))
    )
    if proved is None:
        return _refuse(request, 401, "unknown_device", "this computer is not enrolled here")
    claimed = proved

    ws = web.WebSocketResponse(max_msg_size=protocol.MAX_FRAME_BYTES, heartbeat=30.0)
    await ws.prepare(request)
    gateway_nonce = secrets.token_hex(32)
    key = await asyncio.to_thread(peer_key_from_secret, claimed, gateway)
    proof = gateway_proof(key, gateway, claimed.device_id, client_nonce, gateway_nonce)
    await ws.send_str(
        protocol.encode(
            {
                "op": "hello",
                "v": protocol.PROTOCOL_VERSION,
                "gateway": gateway,
                "device": claimed.device_id,
                "nonce": gateway_nonce,
                "proof": proof,
            }
        )
    )
    bound = (gateway, claimed.device_id, client_nonce, gateway_nonce)
    # Both directions are authenticated from here: auth, ready, and the refusal
    # or admission this gateway answers with. A close code proves nothing.
    frames = SessionFrames(key, bound, gateway=True)
    try:
        record, token = await _authenticate(ws, claimed, bound, registry, frames)
    except CredentialRejected as exc:
        _audit(request, "denied", exc.code, claimed.device_id)
        await _refuse_authenticated(ws, frames, exc.code, _rejection_close_code(exc))
        return ws
    except (asyncio.TimeoutError, protocol.ProtocolError) as exc:
        # A frame that failed its MAC may be a relay's: never answered with a
        # refusal, which would end the grant for something the computer did not send.
        _audit(request, "denied", f"handshake: {exc}", claimed.device_id)
        await ws.close(code=protocol.CLOSE_HANDSHAKE_FAILED, message=b"handshake failed")
        return ws
    except Exception:  # noqa: BLE001 -- unreadable state refuses
        logger.warning("connected computers: credential check failed", exc_info=True)
        await _refuse_authenticated(ws, frames, "state_unreadable", protocol.CLOSE_STATE_UNREADABLE)
        return ws
    try:
        ready = await _receive(ws, frozenset({"ready"}), frames)
        if not valid_root(ready["root"]):
            raise protocol.ProtocolError("ready.root is not an absolute POSIX path")
    except (asyncio.TimeoutError, protocol.ProtocolError) as exc:
        _audit(request, "denied", f"handshake: {exc}", record.device_id)
        await ws.close(code=protocol.CLOSE_HANDSHAKE_FAILED, message=b"handshake failed")
        return ws

    async def send_authenticated(text: str) -> None:
        # The broker holds its send lock across this adapter, including stop.
        frame = protocol.decode(text, allowed=protocol.FROM_GATEWAY - {"hello"})
        await ws.send_str(frames.encode(frame))

    broker = get_broker()
    admission = await _admit(
        broker,
        record.device_id,
        token,
        gateway,
        registry,
        ws,
        frames,
        lambda current: DeviceConnection(
            current,
            send_authenticated,
            root=ready["root"],
            mcp=tuple(ready["mcp"]),
            platform=protocol.reason(ready["platform"]),
            confinement=protocol.reason(ready["confinement"]),
            closer=_closer_for(ws),
        ),
    )
    # Refused or the socket gone: nothing is attached and no channel can open.
    # The refusal and the close run after the fence, so neither holds a Disconnect.
    if isinstance(admission, CredentialRejected):
        _audit(request, "denied", admission.code, record.device_id)
        await _refuse_authenticated(ws, frames, admission.code, _rejection_close_code(admission))
        return ws
    if admission is None:
        with contextlib.suppress(Exception):
            await ws.close()
        return ws
    conn = admission
    _audit(request, "ok", "", record.device_id)
    why = "the computer disconnected"
    watcher = asyncio.ensure_future(_watch(conn, token, gateway, registry, ws))
    liveness = _new_liveness()
    heartbeat = asyncio.ensure_future(liveness.run(lambda: conn.send({"op": "ping"})))
    closing = frozenset({WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})
    try:
        while True:
            msg = await liveness.receive(ws.receive)
            if msg is None:
                why = "the computer sent nothing authenticated in time"
                break
            if msg.type in closing:
                break
            if msg.type != WSMsgType.TEXT:
                why = "the computer sent a frame this gateway does not accept"
                break
            try:
                frame = frames.decode(msg.data, allowed=protocol.FROM_DEVICE)
                # Only a frame that passed its MAC and sequence is progress.
                liveness.progress()
                if frame["op"] != "ping":
                    conn.handle(frame)
            except protocol.ProtocolError as exc:
                why = f"protocol error: {exc}"
                break
            if not conn.alive:
                why = "replaced by a newer connection"
                break
    finally:
        for task in (watcher, heartbeat):
            task.cancel()
        await asyncio.gather(watcher, heartbeat, return_exceptions=True)
        broker.detach(conn, why)
        with contextlib.suppress(Exception):
            await ws.close()
    return ws


async def _admit(
    broker: ConnectionBroker,
    device_id: str,
    token: str,
    gateway: str,
    registry: DeviceRegistry,
    ws: web.WebSocketResponse,
    frames: SessionFrames,
    make: Callable[[DeviceRecord], DeviceConnection],
) -> "DeviceConnection | CredentialRejected | None":
    """The final admission, inside the device's admission fence.

    The record is read again here, after ``ready``: a Disconnect or Remove made
    since ``auth`` was verified refuses now. One made after this fence is taken
    waits for the attach and the ``admitted`` frame, then ends the connection
    it finds (:func:`.remote_routes.end_device`). Returns the attached
    connection, the refusal to send, or None when the socket is gone.
    """
    async with broker.admission(device_id):
        try:
            current = await asyncio.to_thread(
                verify_standing, token, gateway_id=gateway, registry=registry
            )
        except CredentialRejected as exc:
            return exc
        except Exception:  # noqa: BLE001 -- unreadable state refuses
            logger.warning("connected computers: admission re-check failed", exc_info=True)
            return CredentialRejected("state_unreadable", "connector state is unreadable")
        if current.device_id != device_id:
            return CredentialRejected("wrong_device", "this credential belongs to another device")
        conn = make(current)
        try:
            broker.attach(conn)
        except CredentialRejected as exc:
            return exc
        # Encoded before any await, so it is sequence 0 ahead of anything the
        # broker sends (a Disconnect's refusal included: it waits on this fence);
        # the only evidence the connector takes that it was admitted.
        admitted = frames.encode({"op": "admitted"})
        try:
            # Bounded: a relay that stalls this send never holds a Disconnect.
            await asyncio.wait_for(ws.send_str(admitted), ADMIT_SEND_SECS)
        except asyncio.CancelledError:
            broker.detach(conn, "the computer disconnected")
            raise
        except Exception:  # noqa: BLE001 -- the socket is gone or stalled
            broker.detach(conn, "the computer disconnected")
            return None
    return conn


async def _watch(
    conn: DeviceConnection, token: str, gateway: str, registry: DeviceRegistry, ws: Any
) -> None:
    """End the socket as soon as the credential or the switch stops admitting it."""
    while conn.alive:
        await asyncio.sleep(RECHECK_SECS)
        why = ""
        refusal = ""
        # Off the loop: without a live config snapshot the switch is read from disk.
        if not await asyncio.to_thread(remote_enabled):
            why = "connected computers was turned off on this gateway"
        else:
            try:
                await asyncio.to_thread(
                    verify_standing, token, gateway_id=gateway, registry=registry
                )
            except CredentialRejected as exc:
                why = f"access ended: {exc.code}"
                # Authenticated: revoked or removed ends the computer's grant.
                refusal = exc.code
            except Exception:  # noqa: BLE001 -- unreadable state ends the socket
                why = "connector state became unreadable"
        if why:
            get_broker().detach(conn, why)
            await conn.stop(why, refusal=refusal)
            return


def register_routes(app: web.Application) -> None:
    app.router.add_get(CONNECT_PATH, api_connector_connect)
