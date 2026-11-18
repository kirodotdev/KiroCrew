"""The connector wire: JSON text frames over one WebSocket, multiplexed by channel.

Both machines import this module, so the frame shapes cannot drift. The local
machine opened the socket; after the handshake, each tool server the session
uses is one *channel*.

Handshake (R6): the upgrade request carries no credential, only
:data:`DEVICE_HEADER`, a fresh client nonce in :data:`NONCE_HEADER` and the
credential's generation in :data:`GENERATION_HEADER` (which enrollment's peer
key the gateway must prove; it grants nothing). The
gateway's ``hello`` adds its own nonce and a ``proof``
(:func:`.connector_token.gateway_proof` under the per-enrollment peer key the
connector got over SSH). Only when the proof checks does the connector send
``auth`` with its unsigned credential payload and proof of possession, then
``ready``. Every frame after ``hello`` is wrapped by
:class:`.authenticated.SessionFrames` as ``{seq, frame, mac}``, including auth,
ready, credit and stop. Counters start at zero independently in each direction.
The gateway's first authenticated frame is ``admitted`` (it verified the
credential and attached the computer) or ``refusal`` (the reason it will not).

WebSocket close codes and reasons are NOT authenticated: a port relay can forge
them. The connector never derives anything from them; only an authenticated
``refusal`` naming an explicit end (:data:`TERMINAL_REFUSALS`) ends a grant.

gateway -> local::

    {"op": "hello", "v": 1, "gateway": <gateway id>, "device": <device id>,
     "nonce": <64 hex>, "proof": <64 hex>}
    {"op": "admitted"}                         # auth and ready verified, attached
    {"op": "refusal", "code": <refusal code>}  # why this computer is refused, then close
    {"op": "open", "ch": <n>, "kind": "workspace" | "mcp:<name>"}
    {"op": "data", "ch": <n>, "b64": <base64, at most MAX_DATA_BYTES raw>}
    {"op": "eof", "ch": <n>}                   # the bridge closed its input
    {"op": "close", "ch": <n>}                 # stop the channel's process tree
    {"op": "credit", "ch": <n>, "bytes": <n>}  # the gateway consumed this much
    {"op": "stop", "reason": <text>}           # revoked / feature off: end everything
    {"op": "ping"}                             # authenticated liveness, after admission

local -> gateway::

    {"op": "auth", "payload": <unsigned payload>, "proof": <64 hex>}
    {"op": "ready", "v": 1, "root": <granted root>, "mcp": [<exposed names>],
     "platform": <os>, "confinement": <plan name>}
    {"op": "opened", "ch": <n>}
    {"op": "refused", "ch": <n>, "reason": <text>}
    {"op": "data", ...} / {"op": "eof", ...} / {"op": "credit", ...}
    {"op": "closed", "ch": <n>, "reason": <text>}
    {"op": "ping"}                             # authenticated liveness, after admission

Authenticated liveness: a relay cannot forge or replay a frame, but it can drop
the tail of the traffic while keeping WebSocket pings and pongs flowing. So
each side sends a MACed, sequenced ``ping`` every :data:`HEARTBEAT_SECS` once
admitted, and ends the connection when no authenticated frame of any op arrived
for :data:`LIVENESS_DEADLINE_SECS` (:class:`.authenticated.Liveness`). That end
is an interrupted connection, never a refusal: it never ends a grant.
Transport pings and pongs are never authenticated progress.

The gateway never names a path, an argv or an environment: ``kind`` selects
either the workspace tool server, confined to the root the LOCAL user granted,
or one local MCP server the local user exposed by name. Unknown ops and fields
are refused, so a newer peer fails loudly instead of being half understood.

Flow control is a credit window per channel and direction (:class:`SendWindow`,
:class:`RecvBuffer`): a sender never has more than :data:`WINDOW_BYTES` unconsumed
at its peer, so a slow reader holds its producer instead of growing a queue.
There is no resend and no resume: a channel lives on exactly one connection, and
when that connection ends the channel ends as interrupted (B08).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any, Final

PROTOCOL_VERSION: Final[int] = 1
#: Largest text frame either side accepts.
MAX_FRAME_BYTES: Final[int] = 64 * 1024
#: Largest raw payload of one data frame (base64 grows it by a third).
MAX_DATA_BYTES: Final[int] = 32 * 1024
#: Unconsumed bytes a sender may have outstanding per channel.
WINDOW_BYTES: Final[int] = 1024 * 1024
#: Most local MCP servers one grant may expose.
MAX_EXPOSED_MCP: Final[int] = 16
#: Open channels ONE chat may hold on a computer: every exposed MCP server plus
#: that chat's workspace channel.
MAX_CHANNELS: Final[int] = MAX_EXPOSED_MCP + 1
#: Open channels one connection holds across all its chats: room for four chats
#: at their full :data:`MAX_CHANNELS`. A resource bound, not a product limit:
#: each channel may buffer up to :data:`WINDOW_BYTES` unread on the gateway, so
#: this caps one computer at 68 MiB of unread tool output.
MAX_CONNECTION_CHANNELS: Final[int] = 4 * MAX_CHANNELS
#: Highest channel number (channels are numbered by the gateway).
MAX_CHANNEL_ID: Final[int] = 65535
#: Longest reason text carried in a frame.
MAX_REASON_CHARS: Final[int] = 300
#: How often each side sends an authenticated ``ping`` once admitted.
HEARTBEAT_SECS: Final[float] = 15.0
#: Longest time without any authenticated frame before the connection is dropped
#: as interrupted (three missed heartbeats).
LIVENESS_DEADLINE_SECS: Final[float] = 45.0

KIND_WORKSPACE: Final[str] = "workspace"
KIND_MCP_PREFIX: Final[str] = "mcp:"
_ALNUM: Final[frozenset[str]] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
#: Longest exposed local MCP server name.
MCP_NAME_MAX: Final[int] = 64
#: The characters an MCP server name may hold after its first, which is a letter or digit.
MCP_NAME_CHARS: Final[frozenset[str]] = _ALNUM | {"_", ".", "-"}
#: Longest refusal code.
REFUSAL_CODE_MAX: Final[int] = 64
REFUSAL_CODE_CHARS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz_")
#: A nonce, proof or MAC: this many lower-case hex characters (32 bytes).
NONCE_CHARS: Final[int] = 64
_HEX_LOWER: Final[frozenset[str]] = frozenset("0123456789abcdef")


def valid_mcp_name(value: Any) -> bool:
    """An exposed local MCP server's name, as the local user's config calls it.

    1-64 of ``A-Z``, ``a-z``, ``0-9``, ``_``, ``.`` and ``-``, starting with a
    letter or digit.
    """
    return (
        isinstance(value, str)
        and 0 < len(value) <= MCP_NAME_MAX
        and value[0] in _ALNUM
        and all(ch in MCP_NAME_CHARS for ch in value)
    )


FROM_GATEWAY: Final[frozenset[str]] = frozenset(
    {"hello", "admitted", "refusal", "open", "data", "eof", "close", "credit", "stop", "ping"}
)
#: What the connector accepts from the gateway before ``admitted``.
BEFORE_ADMISSION: Final[frozenset[str]] = frozenset({"admitted", "refusal"})
#: What the connector accepts from the gateway after ``admitted``.
AFTER_ADMISSION: Final[frozenset[str]] = FROM_GATEWAY - {"hello", "admitted"}
FROM_DEVICE: Final[frozenset[str]] = frozenset(
    {"ready", "opened", "refused", "data", "eof", "closed", "credit", "ping"}
)
#: The only frame the connector may send between ``hello`` and ``ready``.
AUTH_ONLY: Final[frozenset[str]] = frozenset({"auth"})

#: Upgrade-request headers: which computer is dialling, and its fresh nonce.
DEVICE_HEADER: Final[str] = "X-Connector-Device"
NONCE_HEADER: Final[str] = "X-Connector-Nonce"
#: The ``cgen`` of the credential the connector holds (decimal). Optional: without
#: it the gateway proves the record's current generation and admits only an
#: active record.
GENERATION_HEADER: Final[str] = "X-Connector-Generation"
#: The upgrade's answer while the remote gateway has the preview on but cannot
#: serve connected computers (Session Environments unreachable): "not now", so
#: the local grant waits and retries. With the preview off the path is not served
#: at all, and the dashboard gate refuses the connector (403) as it refuses any
#: unserved path; an unauthenticated status never ends a grant either way.
REMOTE_OFF_STATUS: Final[int] = 503
#: WebSocket close codes the gateway ends a handshake with. Diagnostic only: a
#: relay can forge them, so the connector never acts on them.
CLOSE_HANDSHAKE_FAILED: Final[int] = 4400
CLOSE_CREDENTIAL_REJECTED: Final[int] = 4401
CLOSE_STATE_UNREADABLE: Final[int] = 4503
#: The authenticated refusal of a credential that ran out: renew it, then redial.
EXPIRED_REFUSAL: Final[str] = "expired"
#: Authenticated refusal codes that end a grant until it is shared again: the
#: owner disconnected (``revoked``) or removed the computer, a newer consent
#: replaced this enrollment, or the record names another computer or owner.
#: Every other code is retried with back-off.
TERMINAL_REFUSALS: Final[frozenset[str]] = frozenset(
    {"revoked", "removed", "superseded", "unknown_device", "wrong_device", "wrong_owner"}
)
#: Longest unsigned payload an ``auth`` frame carries; the complete credential
#: must also fit ``connector_token.MAX_CREDENTIAL_CHARS`` when reconstructed locally.
MAX_CREDENTIAL_CHARS: Final[int] = 4096


def valid_nonce(value: Any) -> bool:
    """A handshake nonce or proof: 64 lowercase hex characters."""
    return (
        isinstance(value, str)
        and len(value) == NONCE_CHARS
        and all(ch in _HEX_LOWER for ch in value)
    )


def valid_refusal_code(value: Any) -> bool:
    """1-64 of ``a-z`` and ``_``."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= REFUSAL_CODE_MAX
        and all(ch in REFUSAL_CODE_CHARS for ch in value)
    )


#: Fields each op carries (besides ``op``), with their types.
_FIELDS: Final[dict[str, dict[str, type | tuple[type, ...]]]] = {
    "hello": {"v": int, "gateway": str, "device": str, "nonce": str, "proof": str},
    "auth": {"payload": str, "proof": str},
    "ready": {"v": int, "root": str, "mcp": list, "platform": str, "confinement": str},
    "open": {"ch": int, "kind": str},
    "opened": {"ch": int},
    "refused": {"ch": int, "reason": str},
    "data": {"ch": int, "b64": str},
    "eof": {"ch": int},
    "close": {"ch": int},
    "closed": {"ch": int, "reason": str},
    "credit": {"ch": int, "bytes": int},
    "stop": {"reason": str},
    "admitted": {},
    "refusal": {"code": str},
    "ping": {},
}


class ProtocolError(ValueError):
    """A frame that breaks the protocol; the connection that sent it is dropped."""


def encode(frame: dict[str, Any]) -> str:
    text = json.dumps(frame, separators=(",", ":"), ensure_ascii=True)
    if len(text) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    return text


def decode(text: Any, *, allowed: frozenset[str]) -> dict[str, Any]:
    """Parse and validate one frame whose op must be in ``allowed``."""
    if not isinstance(text, str) or len(text) > MAX_FRAME_BYTES:
        raise ProtocolError("frame is not text or too large")
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("frame is not JSON") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("frame is not an object")
    op = frame.get("op")
    if not isinstance(op, str) or op not in allowed:
        raise ProtocolError("unexpected op")
    fields = _FIELDS[op]
    if set(frame) != {"op", *fields}:
        raise ProtocolError(f"{op}: unexpected or missing fields")
    for name, kind in fields.items():
        value = frame[name]
        # bool is an int subclass; it is never a channel number or a count.
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ProtocolError(f"{op}.{name} has the wrong type")
    if "ch" in fields and not 1 <= frame["ch"] <= MAX_CHANNEL_ID:
        raise ProtocolError("channel number out of range")
    if op == "credit" and not 0 < frame["bytes"] <= WINDOW_BYTES:
        raise ProtocolError("credit out of range")
    if op in ("hello", "ready") and frame["v"] != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if op == "hello" and not (valid_nonce(frame["nonce"]) and valid_nonce(frame["proof"])):
        raise ProtocolError("hello.nonce or hello.proof is malformed")
    if op == "auth" and not (
        0 < len(frame["payload"]) <= MAX_CREDENTIAL_CHARS and valid_nonce(frame["proof"])
    ):
        raise ProtocolError("auth.payload or auth.proof is malformed")
    if op == "refusal" and not valid_refusal_code(frame["code"]):
        raise ProtocolError("refusal.code is malformed")
    if op == "open":
        kind_of(frame["kind"])
    if op == "ready":
        names = frame["mcp"]
        if len(names) > MAX_EXPOSED_MCP or not all(valid_mcp_name(n) for n in names):
            raise ProtocolError("ready.mcp is invalid")
    for name in ("reason", "root", "platform", "confinement"):
        if name in frame and len(frame[name]) > 4096:
            raise ProtocolError(f"{op}.{name} is too long")
    return frame


def kind_of(kind: str) -> tuple[str, str]:
    """``("workspace", "")`` or ``("mcp", <name>)``; raises for anything else."""
    if kind == KIND_WORKSPACE:
        return KIND_WORKSPACE, ""
    if kind.startswith(KIND_MCP_PREFIX) and valid_mcp_name(kind[len(KIND_MCP_PREFIX) :]):
        return "mcp", kind[len(KIND_MCP_PREFIX) :]
    raise ProtocolError("unknown channel kind")


def data_frames(ch: int, data: bytes) -> list[dict[str, Any]]:
    """``data`` as data frames of at most :data:`MAX_DATA_BYTES` each."""
    return [
        {"op": "data", "ch": ch, "b64": base64.b64encode(data[i : i + MAX_DATA_BYTES]).decode()}
        for i in range(0, len(data), MAX_DATA_BYTES)
    ]


def data_of(frame: dict[str, Any]) -> bytes:
    try:
        data = base64.b64decode(frame["b64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtocolError("data is not base64") from exc
    if len(data) > MAX_DATA_BYTES or not data:
        raise ProtocolError("data frame size out of range")
    return data


def reason(text: Any) -> str:
    """A reason safe to put in a frame: printable and bounded."""
    clean = "".join(ch for ch in str(text or "") if ch.isprintable())
    return clean[:MAX_REASON_CHARS]


class WindowClosed(ConnectionError):
    """The channel ended while a sender waited for credit."""


class SendWindow:
    """How many bytes may still be sent before the peer grants more credit."""

    def __init__(self, size: int = WINDOW_BYTES) -> None:
        self._available = size
        self._changed = asyncio.Event()
        self._closed = False

    async def acquire(self, n: int) -> None:
        while self._available < n:
            if self._closed:
                raise WindowClosed("channel ended")
            self._changed.clear()
            await self._changed.wait()
        if self._closed:
            raise WindowClosed("channel ended")
        self._available -= n

    def grant(self, n: int) -> None:
        self._available = min(self._available + n, WINDOW_BYTES)
        self._changed.set()

    def close(self) -> None:
        self._closed = True
        self._changed.set()


class RecvBuffer:
    """Bytes received and not yet consumed, bounded by the window the peer honours.

    A peer that sends past the window broke the protocol: :meth:`feed` raises
    rather than growing the buffer.
    """

    def __init__(self, size: int = WINDOW_BYTES) -> None:
        self._size = size
        self._chunks: list[bytes] = []
        self._held = 0
        self._eof = False
        self._error: BaseException | None = None
        self._changed = asyncio.Event()

    @property
    def held(self) -> int:
        return self._held

    def feed(self, data: bytes) -> None:
        if self._eof or self._error is not None:
            raise ProtocolError("data after the end of the channel")
        if self._held + len(data) > self._size:
            raise ProtocolError("peer sent past its credit window")
        self._chunks.append(data)
        self._held += len(data)
        self._changed.set()

    def feed_eof(self) -> None:
        self._eof = True
        self._changed.set()

    def fail(self, error: BaseException) -> None:
        if self._error is None:
            self._error = error
        self._changed.set()

    async def read(self, n: int) -> bytes:
        """Up to ``n`` bytes; ``b""`` at end of stream; raises the failure if any."""
        while not self._chunks:
            if self._error is not None:
                raise self._error
            if self._eof:
                return b""
            self._changed.clear()
            await self._changed.wait()
        out = bytearray()
        while self._chunks and len(out) < n:
            chunk = self._chunks[0]
            take = chunk[: n - len(out)]
            out += take
            if len(take) == len(chunk):
                self._chunks.pop(0)
            else:
                self._chunks[0] = chunk[len(take) :]
        self._held -= len(out)
        return bytes(out)
