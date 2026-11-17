"""A minimal SPIFFE Workload API client: fetch one JWT-SVID over the agent socket.

The SPIFFE Workload API is gRPC over HTTP/2 on a Unix domain socket that the node's
agent publishes into the pod (``SPIFFE_ENDPOINT_SOCKET``). The Crew needs exactly one
of its calls, ``SpiffeWorkloadAPI/FetchJWTSVID``, so this module speaks that one
unary call directly with ``h2`` (already installed through ``httpx[http2]``) instead
of pulling in a gRPC runtime:

* the request is ``JWTSVIDRequest{audience: [aud], spiffe_id: id}``, protobuf-encoded
  by hand, with the ``workload.spiffe.io: true`` metadata header the API requires;
* the answer is ``JWTSVIDResponse{svids: [JWTSVID{spiffe_id, svid, hint}]}``, and
  the call succeeds only when the trailers (or a trailers-only answer) carry
  ``grpc-status: 0``.

The SVID is checked for shape and claims (subject, audience, a future ``exp``) but its
signature is not verified here: the agent is the issuer's local delegate and every
relying party verifies the token itself. The token is never logged and never part of
a ``repr``; errors name a category, never response bytes.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Optional, Tuple

from kiro_crew.relay_client.token_source import token_claims

#: The one method this client calls.
FETCH_JWT_SVID_PATH = "/SpiffeWorkloadAPI/FetchJWTSVID"
#: Metadata the Workload API requires on every call, so a plain proxy cannot relay one.
SECURITY_HEADER = ("workload.spiffe.io", "true")
#: The largest answer read; one JWT-SVID is a few kilobytes.
ANSWER_MAX_BYTES = 256 * 1024
#: Bound on one whole call, connecting included.
DEFAULT_TIMEOUT_SECONDS = 10.0
#: A token with less than this many seconds left is not handed out.
MIN_REMAINING_SECONDS = 30.0
#: The latest ``exp`` accepted: 9999-12-31T23:59:59Z, the last time ``datetime`` holds.
MAX_EXP_SECONDS = 253402300799.0
_UNIX_PREFIX = "unix://"


class WorkloadApiError(Exception):
    """The Workload API gave no usable JWT-SVID. The message never carries a token."""


@dataclass(frozen=True)
class JwtSvid:
    """One JWT-SVID and the claims the Crew reads from it (unverified)."""

    token: str = field(repr=False)
    spiffe_id: str
    audience: Tuple[str, ...]
    issuer: str
    expires_at: float
    issued_at: Optional[float] = None

    @property
    def lifetime(self) -> Optional[float]:
        if self.issued_at is None:
            return None
        return self.expires_at - self.issued_at

    def remaining(self, now: Optional[float] = None) -> float:
        return self.expires_at - (time.time() if now is None else now)


def socket_path(endpoint: str) -> str:
    """The filesystem path of a ``SPIFFE_ENDPOINT_SOCKET`` value.

    The standard form is ``unix:///path/agent.sock``; a bare absolute path is taken
    as it is. Anything else (``tcp://``, a relative path) is refused: the Crew only
    talks to a local agent over a Unix socket.
    """
    value = (endpoint or "").strip()
    if value.startswith(_UNIX_PREFIX):
        value = value[len(_UNIX_PREFIX) :]
        # ``unix:/path`` and ``unix:///path`` both name ``/path``.
        while value.startswith("//"):
            value = value[1:]
    elif value.startswith("unix:"):
        value = value[len("unix:") :]
    if not value.startswith("/"):
        raise WorkloadApiError("the Workload API endpoint must be a unix:// socket path")
    return value


# ── protobuf, by hand: only strings and length-delimited messages ──


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _string_field(number: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _varint((number << 3) | 2) + _varint(len(raw)) + raw


def encode_jwt_svid_request(audience: str, spiffe_id: str = "") -> bytes:
    """``JWTSVIDRequest``: field 1 ``audience`` (repeated), field 2 ``spiffe_id``."""
    body = _string_field(1, audience)
    if spiffe_id:
        body += _string_field(2, spiffe_id)
    return body


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise WorkloadApiError("the Workload API answer is not valid protobuf")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes) -> Iterator[Tuple[int, int, Any]]:
    """``(number, wire_type, value)`` for each field; bytes for length-delimited ones."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield number, wire, value
        elif wire == 1:
            pos += 8
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise WorkloadApiError("the Workload API answer is not valid protobuf")
            yield number, wire, buf[pos : pos + length]
            pos += length
        elif wire == 5:
            pos += 4
        else:
            raise WorkloadApiError("the Workload API answer is not valid protobuf")
        if pos > len(buf):
            raise WorkloadApiError("the Workload API answer is not valid protobuf")


def decode_jwt_svid_response(buf: bytes) -> List[Tuple[str, str]]:
    """``[(spiffe_id, svid), ...]`` from a ``JWTSVIDResponse``."""
    out: List[Tuple[str, str]] = []
    for number, wire, value in _fields(buf):
        if number != 1 or wire != 2:
            continue
        spiffe_id = svid = ""
        for inner_number, inner_wire, inner in _fields(value):
            if inner_wire != 2:
                continue
            try:
                text = inner.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkloadApiError("the Workload API answer is not valid UTF-8") from None
            if inner_number == 1:
                spiffe_id = text
            elif inner_number == 2:
                svid = text
        if svid:
            out.append((spiffe_id, svid))
    return out


def grpc_frame(message: bytes) -> bytes:
    """One uncompressed gRPC length-prefixed message."""
    return b"\x00" + len(message).to_bytes(4, "big") + message


def grpc_unframe(body: bytes) -> bytes:
    """The single message of a unary answer's body."""
    if len(body) < 5:
        raise WorkloadApiError("the Workload API answer has no message")
    if body[0] != 0:
        raise WorkloadApiError("the Workload API answer is compressed, which is not supported")
    length = int.from_bytes(body[1:5], "big")
    if 5 + length > len(body):
        raise WorkloadApiError("the Workload API answer is truncated")
    return body[5 : 5 + length]


# ── the JWT ──


def _number(value: Any) -> Optional[float]:
    # json.loads reads NaN and Infinity; neither is a usable time.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float is no usable time either
        return None
    return number if math.isfinite(number) else None


def _audiences(aud: Any) -> Tuple[str, ...]:
    """``aud`` is one string or a list of them; any other shape names no audience."""
    if isinstance(aud, str):
        return (aud,)
    if isinstance(aud, list):
        return tuple(a for a in aud if isinstance(a, str))
    return ()


def check_svid(
    token: str,
    *,
    audience: str,
    expected_spiffe_id: str = "",
    returned_spiffe_id: str = "",
    now: Optional[float] = None,
) -> JwtSvid:
    """Read and check the claims of *token*; raise :class:`WorkloadApiError` on a mismatch."""
    if not token.isascii() or any(ch.isspace() for ch in token):
        raise WorkloadApiError("the JWT-SVID is not one ASCII token")
    # Read, not verified: the services the Crew presents it to verify it.
    claims = token_claims(token)
    if not claims:
        raise WorkloadApiError("the JWT-SVID is not a compact JWT with a JSON object payload")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.startswith("spiffe://"):
        raise WorkloadApiError("the JWT-SVID has no SPIFFE ID subject")
    if returned_spiffe_id and returned_spiffe_id != sub:
        raise WorkloadApiError("the JWT-SVID subject differs from the SPIFFE ID the agent named")
    if expected_spiffe_id and expected_spiffe_id != sub:
        raise WorkloadApiError(
            f"the JWT-SVID subject is {sub}, not the configured {expected_spiffe_id}"
        )
    auds = _audiences(claims.get("aud"))
    if audience not in auds:
        raise WorkloadApiError("the JWT-SVID is not audienced to the requested audience")
    exp = _number(claims.get("exp"))
    if exp is None or exp > MAX_EXP_SECONDS:
        # Past the bound no time can be formed from it, so it is no usable expiry.
        raise WorkloadApiError("the JWT-SVID has no finite exp claim")
    svid = JwtSvid(
        token=token,
        spiffe_id=sub,
        audience=auds,
        issuer=str(claims.get("iss") or ""),
        expires_at=exp,
        issued_at=_number(claims.get("iat")),
    )
    if svid.remaining(now) < MIN_REMAINING_SECONDS:
        raise WorkloadApiError("the JWT-SVID is expired or about to expire")
    return svid


# ── the call ──


async def fetch_jwt_svid(
    endpoint: str,
    audience: str,
    *,
    spiffe_id: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> JwtSvid:
    """Fetch one JWT-SVID for *audience* from the Workload API at *endpoint*.

    *spiffe_id*, when set, asks the agent for that identity and refuses any other.
    """
    if not audience:
        raise WorkloadApiError("no JWT-SVID audience is configured")
    path = socket_path(endpoint)
    try:
        status, message, body = await asyncio.wait_for(
            _unary_call(path, encode_jwt_svid_request(audience, spiffe_id)), timeout
        )
    except asyncio.TimeoutError:
        raise WorkloadApiError("the Workload API did not answer in time") from None
    except OSError as exc:
        raise WorkloadApiError(
            f"the Workload API socket could not be reached ({type(exc).__name__})"
        ) from None
    except WorkloadApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - an h2 protocol fault is named, never echoed
        raise WorkloadApiError(f"the Workload API call failed ({type(exc).__name__})") from None
    if status != "0":
        # grpc-message is the agent's own refusal text, never a token.
        detail = (message or "").strip()[:200]
        raise WorkloadApiError(
            f"the Workload API refused the call (grpc-status {status or 'missing'}"
            + (f": {detail}" if detail else "")
            + ")"
        )
    svids = decode_jwt_svid_response(grpc_unframe(body))
    if not svids:
        raise WorkloadApiError("the Workload API answered with no JWT-SVID")
    returned_id, token = svids[0]
    return check_svid(
        token,
        audience=audience,
        expected_spiffe_id=spiffe_id,
        returned_spiffe_id=returned_id,
    )


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


async def _unary_call(path: str, request: bytes) -> Tuple[str, str, bytes]:
    """One gRPC unary call over HTTP/2 prior knowledge: ``(grpc-status, grpc-message, body)``."""
    import h2.config
    import h2.connection
    import h2.events

    reader, writer = await asyncio.open_unix_connection(path)
    try:
        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding="utf-8")
        )
        conn.initiate_connection()
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (":method", "POST"),
                (":scheme", "http"),
                (":path", FETCH_JWT_SVID_PATH),
                (":authority", "localhost"),
                ("content-type", "application/grpc"),
                ("te", "trailers"),
                SECURITY_HEADER,
            ],
        )
        conn.send_data(stream_id, grpc_frame(request), end_stream=True)
        writer.write(conn.data_to_send())
        await writer.drain()

        body = bytearray()
        grpc: dict = {}
        http_status = ""
        ended = False
        while not ended:
            chunk = await reader.read(65536)
            if not chunk:
                break
            for event in conn.receive_data(chunk):
                if isinstance(event, (h2.events.ResponseReceived, h2.events.TrailersReceived)):
                    for raw_name, raw_value in event.headers:
                        name, value = _text(raw_name), _text(raw_value)
                        if name == ":status":
                            http_status = value
                        elif name in ("grpc-status", "grpc-message"):
                            grpc[name] = value
                elif isinstance(event, h2.events.DataReceived):
                    if event.stream_id == stream_id:
                        body.extend(event.data)
                        if len(body) > ANSWER_MAX_BYTES:
                            raise WorkloadApiError("the Workload API answer is too large")
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamReset) and event.stream_id == stream_id:
                    raise WorkloadApiError("the Workload API reset the call")
                elif isinstance(event, h2.events.ConnectionTerminated):
                    ended = True
                elif isinstance(event, h2.events.StreamEnded) and event.stream_id == stream_id:
                    ended = True
            pending = conn.data_to_send()
            if pending:
                writer.write(pending)
                await writer.drain()
        if http_status and http_status != "200":
            raise WorkloadApiError(f"the Workload API answered HTTP {http_status}")
        return grpc.get("grpc-status", ""), grpc.get("grpc-message", ""), bytes(body)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
