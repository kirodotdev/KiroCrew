"""The Kiro relay host-leg wire format, version 1: the numbers, and every frame's shape.

This module owns every limit, timeout and protocol string of the relay protocol the
host leg speaks. The values come from the protocol v1 contract and its JSON fixtures;
``test/fixtures/relay_protocol_v1/`` carries the fixtures the client is tested against,
and ``test/test_relay_client_protocol.py`` fails when a frame here drifts from them.

Every frame is one JSON object in one WebSocket text frame. :func:`parse_inbound`
validates a frame the relay sent and returns it as a plain ``dict``; a frame that
breaks the contract raises :class:`FrameError`, which carries the ``error`` code the
client answers with and, when the frame named a valid stream, that stream's id. The
``build_*`` helpers make the frames the client sends.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Optional, Tuple, TypeGuard
from urllib.parse import urlsplit

#: The protocol version this client speaks and offers in ``hello``.
PROTOCOL_VERSION = 1
#: The one mode version 1 serves. ``acp`` is reserved for a later mode.
MODE_HTTP = "http"
#: Features the client offers. ``flow-credit`` and ``ws-flow`` are the relay's
#: credit for responses and its pause for WebSockets. ``request-credit`` is the
#: client's own credit for request bodies (contract section 5.4): once the relay
#: agrees it, the relay sends each request body only as far as the client has
#: granted. ``opaque`` is reserved and never offered in version 1.
FEATURE_FLOW_CREDIT = "flow-credit"
FEATURE_WS_FLOW = "ws-flow"
FEATURE_REQUEST_CREDIT = "request-credit"
OFFERED_FEATURES: Tuple[str, ...] = (
    FEATURE_FLOW_CREDIT,
    FEATURE_WS_FLOW,
    FEATURE_REQUEST_CREDIT,
)

# ── timing (contract section 11.2) ──
#: The client pings this often.
PING_INTERVAL_SECONDS = 25.0
#: The client replaces the socket after this long with nothing received.
SILENCE_REPLACE_SECONDS = 60.0
#: Reconnect backoff steps; each wait is a random time between 0 and the step.
BACKOFF_STEPS_SECONDS: Tuple[float, ...] = (2.0, 5.0, 15.0, 30.0, 60.0)
#: The backoff starts again once a connection reaches ready and stays up this long.
BACKOFF_RESET_AFTER_SECONDS = 60.0
#: How long the client waits for the Crew's web server to send a response head.
#: The wait restarts on each request-body chunk, so a long upload is not cut.
CREW_FIRST_BYTE_SECONDS = 50.0
#: A proxied WebSocket the relay paused resumes on its own after this long.
WS_PAUSE_WATCHDOG_SECONDS = 30.0
#: How long a close handshake on the host socket may take before it is abandoned.
CLOSE_HANDSHAKE_SECONDS = 5.0
#: The reconnect contract gives 5 seconds for drain and 30 for max-age.
MOVE_WINDOW_MAX_SECONDS = 30
#: After a move, the old connection stays open until no request has arrived on it
#: for this long with none in flight: a request the relay wrote there just before
#: it stopped routing to it can still be on its way. Never past the move window.
#: Provisional, so it is the setting ``tunnel.relay_move_grace_seconds``.
MOVE_QUIET_GRACE_SECONDS = 1.0

# ── sizes (contract section 3, fixture limits/sizes.json) ──
MAX_TEXT_FRAME_BYTES = 65536
BODY_CHUNK_RAW_BYTES = 40960
WS_PIECE_BYTES = 56000
WS_MESSAGE_MAX_BYTES = 10 * 1024 * 1024
WS_REASSEMBLY_PER_CONNECTION_BYTES = 50 * 1024 * 1024
WS_REASSEMBLY_CONCURRENT = 10
#: Retained fragments per message, including empty pieces. This also admits the
#: worst JSON-escaped 10 MiB message split by split_ws_data.
WS_REASSEMBLY_MAX_PIECES = 4096
#: Messages waiting for the Crew, including the send currently in flight.
WS_QUEUE_MAX_MESSAGES = 1024
#: Recent backoff waits retained for diagnostics, not the client's whole lifetime.
BACKOFF_HISTORY_MAX_ENTRIES = 128
WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS = 10.0
SET_COOKIES_MAX = 32
SET_COOKIE_MAX_CHARS = 4096
WS_UPGRADE_HEADERS_MAX = 64
HEADER_NAME_MAX_CHARS = 256
HEADER_VALUE_MAX_CHARS = 8192
WS_CLOSE_REASON_BYTES = 123
NONCE_MAX_CHARS = 64
ERROR_MESSAGE_MAX_CHARS = 256
#: A credit grant adds 1 byte to 16 MiB.
MAX_CREDIT_BYTES = 16 * 1024 * 1024
#: Request-body bytes one stream may hold while the Crew's web server reads them.
#: Without ``request-credit`` the relay sends a body without waiting, so this is
#: the bound on what a slow reader makes the client buffer; past it the stream
#: ends with an error. With it, a stream never holds more than its credit.
REQUEST_BUFFER_MAX_BYTES = 8 * 1024 * 1024
#: The request-body credit each stream starts with once ``request-credit`` is
#: agreed (contract section 5.4, ``requestCreditInitialBytes``). Both sides know
#: it, so no frame carries it. The client only gives back what has left this
#: process for the Crew's web server, so a stream never holds more than this of
#: its body: 256 streams' bodies need at most 32 MiB of the connection's buffer
#: budget, which viewer WebSocket messages share.
REQUEST_CREDIT_INITIAL_BYTES = 128 * 1024
#: Taken bytes the client gathers before it sends one ``flow-credit`` for them.
#: At most the starting credit, so a relay that has spent all of it always gets a
#: grant once the Crew's server takes what is queued.
REQUEST_CREDIT_GRANT_BYTES = 32 * 1024
#: Bytes every stream on one connection may hold together, request bodies and
#: viewer WebSocket messages alike, so many slow streams cannot add up past it.
CONNECTION_BUFFER_MAX_BYTES = 64 * 1024 * 1024
#: Streams one host connection holds at once. The relay owns per-endpoint caps;
#: this one bounds what the client retains whatever the relay sends.
MAX_STREAMS_PER_CONNECTION = 256

# ── reserved paths and identifiers (contract sections 3 and 9) ──
RESERVED_PATH_PREFIX = "/_kiro/relay/"
#: The Kiro user ID grammar: ASCII, safe as a header value, at most 256 characters.
USER_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+=/-]{0,255}\Z", re.ASCII)
#: An endpoint ID is one DNS label.
ENDPOINT_ID_RE = re.compile(r"\A[a-z0-9]{20,63}\Z", re.ASCII)
#: A Crew URL's host: lower-case DNS labels, at most 253 characters.
CREW_HOST_RE = re.compile(
    r"\A(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z", re.ASCII
)

# ── error codes (contract section 4.6) ──
ERR_INVALID_MESSAGE = "invalid_message"
ERR_UNSUPPORTED_FRAME = "unsupported_frame"
ERR_MESSAGE_TOO_LARGE = "message_too_large"
ERR_CREW_NOT_ANSWERING = "crew_not_answering"
ERR_CREW_UNREACHABLE = "crew_unreachable"

# ── close codes (contract section 6.1, fixture close-codes/host-leg.json) ──
CLOSE_TOKEN_REFUSED = 4401
CLOSE_TOKEN_EXPIRED = 4403
CLOSE_NO_HELLO = 4408
CLOSE_REPLACED = 4409
CLOSE_RATE_LIMITED = 4429
CLOSE_DRAIN = 1012
CLOSE_TRY_AGAIN_LATER = 1013
#: The Crew's own close when Remote Control is turned off.
CLOSE_REMOTE_CONTROL_OFF = 4410
#: The Crew's close of the old connection once a move has finished.
CLOSE_MOVED = 1000
#: The Crew's close when the gateway stops without turning Remote Control off.
CLOSE_GOING_AWAY = 1001
#: The relay client's code when the Crew's web server refuses a WebSocket upgrade.
WS_CLOSE_CREW_REFUSED = 4502
#: The block of viewer-leg codes the relay keeps for itself.
RELAY_RESERVED_CLOSE_CODES = range(4400, 4500)

#: Frames the relay sends that the client handles.
_INBOUND_TYPES = frozenset(
    {
        "ready",
        "ping",
        "pong",
        "reconnect",
        "error",
        "request",
        "request-chunk",
        "request-end",
        "request-cancel",
        "ws-upgrade",
        "ws-frame",
        "ws-close",
        "flow-credit",
        "ws-flow",
    }
)

_RECONNECT_REASONS = frozenset({"drain", "max-age"})


class FrameError(Exception):
    """A frame from the relay that breaks the contract.

    ``code`` is the ``error`` code the client answers with. ``stream_id`` is the
    stream the frame named, when it named a valid one, so the answer ends that
    stream only. ``answer`` is ``False`` for the one case the contract leaves
    unanswered: an ``error`` frame with no code is dropped and logged.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stream_id: Optional[int] = None,
        answer: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stream_id = stream_id
        self.answer = answer
        #: The ``type`` of the frame that failed, once known.
        self.frame_type: Optional[str] = None


def _is_int(value: Any) -> TypeGuard[int]:
    # bool is an int subclass, and JSON true is never a valid number here.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(frame: Dict[str, Any], key: str, *, minimum: int = 0) -> int:
    value = frame.get(key)
    if not _is_int(value) or value < minimum:
        raise FrameError(ERR_INVALID_MESSAGE, f"{frame.get('type')}.{key} must be an integer")
    return value


def _require_str(frame: Dict[str, Any], key: str, sid: Optional[int]) -> str:
    value = frame.get(key)
    if not isinstance(value, str):
        raise FrameError(
            ERR_INVALID_MESSAGE, f"{frame.get('type')}.{key} must be a string", stream_id=sid
        )
    return value


def _optional_bool(frame: Dict[str, Any], key: str, sid: Optional[int]) -> None:
    if key in frame and not isinstance(frame[key], bool):
        raise FrameError(
            ERR_INVALID_MESSAGE, f"{frame.get('type')}.{key} must be a boolean", stream_id=sid
        )


def _stream_id(frame: Dict[str, Any]) -> int:
    return _require_int(frame, "id", minimum=0)


def _check_user(frame: Dict[str, Any], sid: int) -> None:
    user = frame.get("user")
    if not isinstance(user, str) or not USER_ID_RE.match(user):
        raise FrameError(
            ERR_INVALID_MESSAGE, "the caller's user ID is missing or malformed", stream_id=sid
        )


#: A path or query the client forwards: printable ASCII with no space, so it can
#: never break the request line it is written into.
_URL_PART_RE = re.compile(r"\A[\x21-\x7e]*\Z", re.ASCII)
#: An HTTP header name (RFC 9110 token).
_HEADER_NAME_RE = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z", re.ASCII)
#: Characters a header value may never carry: they would split the header block.
_HEADER_VALUE_FORBIDDEN = frozenset("\r\n\x00")
#: A request's declared body length: decimal digits only, nothing around them.
_CONTENT_LENGTH_RE = re.compile(r"\A[0-9]{1,19}\Z", re.ASCII)


def declared_content_length(headers: Dict[str, str]) -> Optional[int]:
    """The body length a checked ``request`` head declares, or ``None`` for none."""
    for name, value in headers.items():
        if name.lower() == "content-length":
            return int(value)
    return None


def _check_body_framing(headers: Dict[str, str], sid: int) -> None:
    """Refuse a head whose body length could be read two ways.

    The relay client enforces the declared length on the body it hands over, so
    the Crew's web server always reads exactly the bytes the head declared, and
    no body byte can be read as the start of another request.
    """
    lengths = [value for name, value in headers.items() if name.lower() == "content-length"]
    if not lengths:
        return
    if len(lengths) > 1 or not _CONTENT_LENGTH_RE.match(lengths[0]):
        raise FrameError(
            ERR_INVALID_MESSAGE, "content-length is repeated or not a number", stream_id=sid
        )
    if any(name.lower() == "transfer-encoding" for name in headers):
        raise FrameError(
            ERR_INVALID_MESSAGE,
            "a request cannot declare both content-length and transfer-encoding",
            stream_id=sid,
        )


def _check_header(name: str, value: str, sid: int) -> None:
    if (
        len(name) > HEADER_NAME_MAX_CHARS
        or len(value) > HEADER_VALUE_MAX_CHARS
        or not _HEADER_NAME_RE.match(name)
        or any(ch in _HEADER_VALUE_FORBIDDEN for ch in value)
    ):
        raise FrameError(ERR_INVALID_MESSAGE, "a header name or value is malformed", stream_id=sid)


def _check_path(frame: Dict[str, Any], sid: int) -> None:
    path = _require_str(frame, "path", sid)
    query = _require_str(frame, "query", sid)
    if not path.startswith("/"):
        raise FrameError(ERR_INVALID_MESSAGE, "path must start with /", stream_id=sid)
    if not _URL_PART_RE.match(path) or not _URL_PART_RE.match(query):
        raise FrameError(
            ERR_INVALID_MESSAGE, "path and query must be printable ASCII", stream_id=sid
        )
    if path.startswith(RESERVED_PATH_PREFIX) or path == RESERVED_PATH_PREFIX.rstrip("/"):
        raise FrameError(
            ERR_INVALID_MESSAGE, "the relay's reserved path is never forwarded", stream_id=sid
        )


def _check_request(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    method = _require_str(frame, "method", sid)
    if not _HEADER_NAME_RE.match(method) or method.upper() != method:
        raise FrameError(ERR_INVALID_MESSAGE, "method must be an upper-case token", stream_id=sid)
    _check_path(frame, sid)
    headers = frame.get("headers")
    if not isinstance(headers, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
    ):
        raise FrameError(ERR_INVALID_MESSAGE, "headers must map names to strings", stream_id=sid)
    for name, value in headers.items():
        _check_header(name, value, sid)
    _check_body_framing(headers, sid)
    if frame.get("streaming") is not True:
        raise FrameError(ERR_INVALID_MESSAGE, "requests always stream in version 1", stream_id=sid)
    _check_user(frame, sid)
    if "traceparent" in frame and not isinstance(frame["traceparent"], str):
        raise FrameError(ERR_INVALID_MESSAGE, "traceparent must be a string", stream_id=sid)
    if "initialCredit" in frame:
        credit = frame["initialCredit"]
        if not _is_int(credit) or not 1 <= credit <= MAX_CREDIT_BYTES:
            raise FrameError(ERR_INVALID_MESSAGE, "initialCredit is out of range", stream_id=sid)


def _check_ws_upgrade(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    _check_path(frame, sid)
    if "protocols" in frame and not isinstance(frame["protocols"], str):
        raise FrameError(ERR_INVALID_MESSAGE, "protocols must be a string", stream_id=sid)
    headers = frame.get("headers", [])
    if not isinstance(headers, list) or len(headers) > WS_UPGRADE_HEADERS_MAX:
        raise FrameError(ERR_INVALID_MESSAGE, "ws-upgrade headers are malformed", stream_id=sid)
    for pair in headers:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(p, str) for p in pair)
        ):
            raise FrameError(
                ERR_INVALID_MESSAGE, "ws-upgrade header pair is malformed", stream_id=sid
            )
        _check_header(pair[0], pair[1], sid)
    _check_user(frame, sid)


def _check_ws_frame(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    _require_str(frame, "data", sid)
    _optional_bool(frame, "binary", sid)
    if "partial" in frame and frame["partial"] is not True:
        raise FrameError(ERR_INVALID_MESSAGE, "partial is either true or absent", stream_id=sid)


def _check_ws_close(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    if "code" in frame:
        code = frame["code"]
        if not _is_int(code) or not (code == 1000 or 3000 <= code <= 4999):
            raise FrameError(ERR_INVALID_MESSAGE, "ws-close code is out of range", stream_id=sid)
    if "reason" in frame:
        reason = frame["reason"]
        if not isinstance(reason, str) or len(reason.encode("utf-8")) > WS_CLOSE_REASON_BYTES:
            raise FrameError(ERR_INVALID_MESSAGE, "ws-close reason is too long", stream_id=sid)


def _check_flow_credit(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    credit = frame.get("credit")
    if not _is_int(credit) or not 1 <= credit <= MAX_CREDIT_BYTES:
        raise FrameError(ERR_INVALID_MESSAGE, "credit must be 1 byte to 16 MiB", stream_id=sid)


def _check_ws_flow(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    if not isinstance(frame.get("paused"), bool):
        raise FrameError(ERR_INVALID_MESSAGE, "paused must be a boolean", stream_id=sid)


def _check_ready(frame: Dict[str, Any]) -> None:
    _require_int(frame, "protocolVersion", minimum=1)
    _require_int(frame, "minProtocolVersion", minimum=1)
    if frame.get("mode") != MODE_HTTP:
        raise FrameError(ERR_INVALID_MESSAGE, "ready names a mode this client does not speak")
    features = frame.get("features")
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise FrameError(ERR_INVALID_MESSAGE, "ready.features must be a list of strings")
    endpoint = frame.get("endpointId")
    if not isinstance(endpoint, str) or not ENDPOINT_ID_RE.match(endpoint):
        raise FrameError(ERR_INVALID_MESSAGE, "ready.endpointId is missing or malformed")
    if "deprecation" in frame and not isinstance(frame["deprecation"], dict):
        raise FrameError(ERR_INVALID_MESSAGE, "ready.deprecation must be an object")


def _check_ping_pong(frame: Dict[str, Any]) -> None:
    if "nonce" in frame:
        nonce = frame["nonce"]
        if not isinstance(nonce, str) or len(nonce) > NONCE_MAX_CHARS:
            raise FrameError(ERR_INVALID_MESSAGE, "nonce must be a string of at most 64 characters")


def _check_reconnect(frame: Dict[str, Any]) -> None:
    if not isinstance(frame.get("reason"), str):
        raise FrameError(ERR_INVALID_MESSAGE, "reconnect.reason must be a string")
    _require_int(frame, "closeInSeconds", minimum=0)


def _check_error(frame: Dict[str, Any]) -> None:
    if not isinstance(frame.get("code"), str) or not frame["code"]:
        raise FrameError(ERR_INVALID_MESSAGE, "error frame without a code", answer=False)
    if "id" in frame and (not _is_int(frame["id"]) or frame["id"] < 0):
        raise FrameError(ERR_INVALID_MESSAGE, "error.id must be a stream id", answer=False)
    if "retryAfterSeconds" in frame and not _is_int(frame["retryAfterSeconds"]):
        raise FrameError(
            ERR_INVALID_MESSAGE, "error.retryAfterSeconds must be an integer", answer=False
        )


def _check_stream_only(frame: Dict[str, Any]) -> None:
    _stream_id(frame)


def _check_request_chunk(frame: Dict[str, Any]) -> None:
    sid = _stream_id(frame)
    _require_str(frame, "data", sid)
    _optional_bool(frame, "base64", sid)


_CHECKS = {
    "ready": _check_ready,
    "ping": _check_ping_pong,
    "pong": _check_ping_pong,
    "reconnect": _check_reconnect,
    "error": _check_error,
    "request": _check_request,
    "request-chunk": _check_request_chunk,
    "request-end": _check_stream_only,
    "request-cancel": _check_stream_only,
    "ws-upgrade": _check_ws_upgrade,
    "ws-frame": _check_ws_frame,
    "ws-close": _check_ws_close,
    "flow-credit": _check_flow_credit,
    "ws-flow": _check_ws_flow,
}


def parse_inbound(text: str) -> Dict[str, Any]:
    """Validate one frame the relay sent and return it.

    Raises :class:`FrameError` for a frame that is not JSON, too large, of a type
    the client does not take from the relay (``opaque`` included, which version 1
    never agrees), or whose fields break the contract. Unknown fields are ignored,
    as the contract asks of every receiver.
    """
    if len(text.encode("utf-8")) > MAX_TEXT_FRAME_BYTES:
        raise FrameError(ERR_MESSAGE_TOO_LARGE, "frame is over 64 KiB")
    try:
        frame = json.loads(text)
    except (ValueError, RecursionError):
        raise FrameError(ERR_INVALID_MESSAGE, "frame is not JSON") from None
    if not isinstance(frame, dict):
        raise FrameError(ERR_INVALID_MESSAGE, "frame is not a JSON object")
    kind = frame.get("type")
    if not isinstance(kind, str) or kind not in _INBOUND_TYPES:
        sid = frame.get("id") if _is_int(frame.get("id")) and frame["id"] >= 0 else None
        raise FrameError(ERR_UNSUPPORTED_FRAME, f"frame type {kind!r} is not agreed", stream_id=sid)
    try:
        _CHECKS[kind](frame)
    except FrameError as exc:
        exc.frame_type = kind
        raise
    except (TypeError, ValueError, RecursionError):
        sid = frame.get("id") if _is_int(frame.get("id")) and frame["id"] >= 0 else None
        refusal = FrameError(ERR_INVALID_MESSAGE, "frame fields are malformed", stream_id=sid)
        refusal.frame_type = kind
        raise refusal from None
    if kind == "reconnect" and frame["reason"] not in _RECONNECT_REASONS:
        # An unknown reason is handled as max-age, as the contract asks.
        frame = {**frame, "reason": "max-age"}
    return frame


def encode(frame: Dict[str, Any]) -> str:
    """Serialize one outbound frame compactly."""
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def build_hello(*, client_version: str) -> Dict[str, Any]:
    return {
        "type": "hello",
        "protocolVersion": PROTOCOL_VERSION,
        "mode": MODE_HTTP,
        "features": list(OFFERED_FEATURES),
        "client": {"name": "kiro-crew", "version": client_version[:32]},
    }


def build_flow_credit(stream_id: int, credit: int) -> Dict[str, Any]:
    """Request-body credit for one stream; the contract allows 1 byte to 16 MiB."""
    if not 1 <= credit <= MAX_CREDIT_BYTES:
        raise ValueError(f"flow-credit must be 1 byte to 16 MiB, not {credit}")
    return {"type": "flow-credit", "id": stream_id, "credit": credit}


def build_error(code: str, message: str, *, stream_id: Optional[int] = None) -> Dict[str, Any]:
    frame: Dict[str, Any] = {
        "type": "error",
        "code": code,
        "message": message[:ERROR_MESSAGE_MAX_CHARS],
    }
    if stream_id is not None:
        frame["id"] = stream_id
    return frame


def build_ws_close(stream_id: int, code: Optional[int], reason: str = "") -> Dict[str, Any]:
    """A ``ws-close`` whose code and reason are always legal on the wire.

    A code outside 1000 and 3000 to 4999, or inside the relay's own 4400 to 4499
    block, is left out rather than sent, so a Crew-side close can never reach a
    viewer as a relay close. The reason is cut to 123 bytes on a character boundary.
    """
    frame: Dict[str, Any] = {"type": "ws-close", "id": stream_id}
    if code is not None and (code == 1000 or 3000 <= code <= 4999):
        if code not in RELAY_RESERVED_CLOSE_CODES:
            frame["code"] = code
    if reason:
        frame["reason"] = truncate_utf8(reason, WS_CLOSE_REASON_BYTES)
    return frame


def is_loopback_host(host: str) -> bool:
    """Accept localhost or a loopback IP literal, optionally enclosed in brackets.

    Address identity, not spelling, keeps equivalent loopback forms usable without
    trusting host names that only resemble loopback addresses. IPv4-mapped IPv6
    addresses are refused.
    """
    if host.lower() == "localhost":
        return True
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(literal)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return address.is_loopback


def host_url_problem(url: str) -> Optional[str]:
    """Why the client must not dial *url* with a host token, or ``None`` when it may.

    The host token rides in the dial's ``Authorization`` header, so the dial is
    ``wss://`` (TLS) unless it names this machine's loopback, which only a local
    test relay does.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "the relay host URL does not parse"
    if parts.scheme == "wss" and host:
        return None
    if parts.scheme == "ws" and host and is_loopback_host(host):
        return None
    return "the relay host URL must be wss:// (ws:// only for a loopback test relay)"


def crew_url_problem(url: str) -> Optional[str]:
    """Why *url* cannot be the Crew's relay URL, or ``None`` when it can.

    The URL joins the dashboard's allowed origins, so it must be exactly an
    ``https://`` origin: a host and nothing after it.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # raises ValueError for a port that is not a number
    except ValueError:
        return "the Crew URL does not parse"
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not CREW_HOST_RE.match(host)
    ):
        return "the Crew URL must be an https:// origin"
    return None


def crew_origin(url: str) -> str:
    """The origin a browser sends for a Crew URL :func:`crew_url_problem` accepted.

    Rebuilt from the parsed parts, so letter case, a trailing slash or an empty
    ``?`` in the URL never yields an origin that no request carries.
    """
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port not in (None, 443) else ""
    return f"https://{parts.hostname}{port}"


def truncate_utf8(text: str, limit: int) -> str:
    """Cut *text* to at most *limit* UTF-8 bytes without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def response_head_problem(frame: Dict[str, Any]) -> Optional[str]:
    """Why a ``response`` head breaks the contract, or ``None`` when it is legal.

    The client checks its own head before sending it, so a Crew answer the relay
    would refuse ends the stream with an ``error`` at once rather than leaving the
    viewer to wait out a timeout.
    """
    status = frame.get("status")
    if not _is_int(status) or not 100 <= status <= 599:
        return "response.status is out of range"
    cookies: List[str] = frame.get("setCookies") or []
    if len(cookies) > SET_COOKIES_MAX:
        return f"response.setCookies has more than {SET_COOKIES_MAX} entries"
    if any(len(c) > SET_COOKIE_MAX_CHARS for c in cookies):
        return f"a Set-Cookie value is over {SET_COOKIE_MAX_CHARS} characters"
    if len(encode(frame).encode("utf-8")) > MAX_TEXT_FRAME_BYTES:
        return "response head is over 64 KiB"
    return None


def split_ws_data(data: str) -> List[str]:
    """Split one WebSocket message's ``data`` string into pieces that fit a frame.

    Each piece's JSON-escaped UTF-8 length is at most :data:`WS_PIECE_BYTES` less
    the frame's own overhead, so every ``ws-frame`` stays under the text-frame
    limit. Pieces are cut on code points, so the receiver rebuilds the message by
    joining the pieces' ``data`` in order.
    """
    budget = WS_PIECE_BYTES - 128
    if _escaped_len(data) <= budget:
        return [data]
    pieces: List[str] = []
    pending = [data]
    while pending:
        piece = pending.pop()
        if _escaped_len(piece) <= budget:
            pieces.append(piece)
            continue
        if len(piece) > budget:
            # Cut at the byte budget first: one character never escapes to less
            # than one byte, so the head is the cheapest split that can fit.
            pending.append(piece[budget:])
            pending.append(piece[:budget])
        else:
            half = len(piece) // 2
            pending.append(piece[half:])
            pending.append(piece[:half])
    return pieces


def _escaped_len(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2
