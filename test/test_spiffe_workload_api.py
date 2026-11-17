"""The SPIFFE Workload API client: protobuf, gRPC framing, claims and the call.

A fake SPIFFE Workload API answers ``FetchJWTSVID`` over HTTP/2 on a real Unix
socket, so the client's framing, metadata header and trailer handling run for real.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
from typing import Any, Optional

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import spiffe_workload_api as wapi

AUDIENCE = "https://runtime.example.test"
SUBJECT = "spiffe://example.test/kiro-crew/platform"
NOW = 1_900_000_000.0

unix_only = pytest.mark.skipif(sys.platform == "win32", reason="unix sockets only")


def _b64(obj: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def make_token(
    *,
    sub: str = SUBJECT,
    aud: Any = AUDIENCE,
    iat: float = NOW - 60,
    exp: Any = NOW + 1800,
    iss: str = "https://issuer.example.test",
) -> str:
    header = _b64({"alg": "RS256", "kid": "k1", "typ": "JWT"})
    payload = _b64({"sub": sub, "aud": aud, "iat": iat, "exp": exp, "iss": iss})
    return f"{header}.{payload}.c2lnbmF0dXJl"


def _response(svids: list[tuple[str, str]]) -> bytes:
    out = b""
    for spiffe_id, token in svids:
        inner = wapi._string_field(1, spiffe_id) + wapi._string_field(2, token)
        out += wapi._varint((1 << 3) | 2) + wapi._varint(len(inner)) + inner
    return out


# ── pure functions ──


def test_request_encoding_round_trips_through_the_field_reader() -> None:
    raw = wapi.encode_jwt_svid_request(AUDIENCE, SUBJECT)
    fields = [(n, w, v.decode()) for n, w, v in wapi._fields(raw)]
    assert fields == [(1, 2, AUDIENCE), (2, 2, SUBJECT)]
    assert [n for n, _w, _v in wapi._fields(wapi.encode_jwt_svid_request(AUDIENCE))] == [1]


def test_response_decoding_reads_every_svid_and_skips_unknown_fields() -> None:
    token = make_token()
    raw = _response([(SUBJECT, token)]) + wapi._varint((7 << 3) | 0) + wapi._varint(5)
    assert wapi.decode_jwt_svid_response(raw) == [(SUBJECT, token)]


def test_fixed_width_fields_are_skipped() -> None:
    token = make_token()
    raw = (
        wapi._varint((8 << 3) | 1)
        + b"\x00" * 8
        + wapi._varint((9 << 3) | 5)
        + b"\x00" * 4
        + _response([(SUBJECT, token)])
    )
    assert wapi.decode_jwt_svid_response(raw) == [(SUBJECT, token)]


def test_truncated_protobuf_is_refused() -> None:
    raw = _response([(SUBJECT, make_token())])
    with pytest.raises(wapi.WorkloadApiError):
        wapi.decode_jwt_svid_response(raw[:-3])


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (wapi._varint((1 << 3) | 3), "protobuf"),
        (b"\xff" * 11, "protobuf"),
        (wapi._varint((1 << 3) | 1) + b"\x00", "protobuf"),
        (
            wapi._varint((1 << 3) | 2)
            + wapi._varint(3)
            + wapi._varint((2 << 3) | 2)
            + wapi._varint(1)
            + b"\xff",
            "UTF-8",
        ),
    ],
)
def test_malformed_protobuf_is_refused_by_category(raw: bytes, match: str) -> None:
    with pytest.raises(wapi.WorkloadApiError, match=match):
        wapi.decode_jwt_svid_response(raw)


def test_grpc_frames_round_trip_and_compressed_frames_are_refused() -> None:
    assert wapi.grpc_unframe(wapi.grpc_frame(b"abc")) == b"abc"
    with pytest.raises(wapi.WorkloadApiError, match="compressed"):
        wapi.grpc_unframe(b"\x01" + (3).to_bytes(4, "big") + b"abc")
    with pytest.raises(wapi.WorkloadApiError, match="truncated"):
        wapi.grpc_unframe(b"\x00" + (9).to_bytes(4, "big") + b"abc")
    with pytest.raises(wapi.WorkloadApiError, match="no message"):
        wapi.grpc_unframe(b"\x00\x00")


@pytest.mark.parametrize(
    ("endpoint", "path"),
    [
        ("unix:///run/spire/agent.sock", "/run/spire/agent.sock"),
        ("unix:/run/spire/agent.sock", "/run/spire/agent.sock"),
        ("/run/spire/agent.sock", "/run/spire/agent.sock"),
    ],
)
def test_socket_path_accepts_unix_forms(endpoint: str, path: str) -> None:
    assert wapi.socket_path(endpoint) == path


@pytest.mark.parametrize("endpoint", ["", "tcp://127.0.0.1:8081", "unix://relative.sock"])
def test_socket_path_refuses_anything_but_an_absolute_unix_path(endpoint: str) -> None:
    with pytest.raises(wapi.WorkloadApiError):
        wapi.socket_path(endpoint)


# ── claims ──


def test_check_svid_reads_the_claims() -> None:
    svid = wapi.check_svid(make_token(), audience=AUDIENCE, now=NOW)
    assert (svid.spiffe_id, svid.audience, svid.lifetime) == (SUBJECT, (AUDIENCE,), 1860.0)
    assert make_token() not in repr(svid)


@pytest.mark.parametrize(
    ("token", "kwargs", "match"),
    [
        (make_token(aud="https://other.test"), {}, "audience"),
        (make_token(aud=["https://other.test", AUDIENCE]), {}, None),
        (make_token(sub="not-spiffe"), {}, "SPIFFE ID"),
        (make_token(), {"expected_spiffe_id": "spiffe://example.test/x"}, "configured"),
        (make_token(), {"returned_spiffe_id": "spiffe://example.test/x"}, "agent named"),
        (make_token(exp=NOW + 5), {}, "expire"),
        ("a.b", {}, "compact JWT"),
        ("x y.z.w", {}, "ASCII token"),
    ],
)
def test_check_svid_refusals(token: str, kwargs: dict, match: Optional[str]) -> None:
    if match is None:
        assert wapi.check_svid(token, audience=AUDIENCE, now=NOW, **kwargs).spiffe_id == SUBJECT
        return
    with pytest.raises(wapi.WorkloadApiError, match=match) as info:
        wapi.check_svid(token, audience=AUDIENCE, now=NOW, **kwargs)
    assert token not in str(info.value)


@pytest.mark.parametrize("aud", [5, {AUDIENCE: 1}, None])
def test_an_audience_that_is_not_a_string_or_list_names_no_audience(aud: Any) -> None:
    with pytest.raises(wapi.WorkloadApiError, match="audience"):
        wapi.check_svid(make_token(aud=aud), audience=AUDIENCE, now=NOW)


@pytest.mark.parametrize("exp", [float("nan"), float("inf"), 1e12, 10**400, "soon", True, None])
def test_an_exp_that_is_not_a_usable_time_is_refused(exp: Any) -> None:
    # 1e12 is finite but past what datetime holds: refused here, as a WorkloadApiError,
    # so the KAS callback answers "workload identity unavailable" instead of crashing.
    with pytest.raises(wapi.WorkloadApiError, match="exp claim"):
        wapi.check_svid(make_token(exp=exp), audience=AUDIENCE, now=NOW)


@pytest.mark.parametrize("exp", [0, -1, -1e12])
def test_an_exp_at_or_before_the_epoch_is_refused_as_expired(exp: float) -> None:
    with pytest.raises(wapi.WorkloadApiError, match="expire"):
        wapi.check_svid(make_token(exp=exp), audience=AUDIENCE, now=NOW)


def test_an_iat_too_large_for_a_float_reads_as_unknown() -> None:
    svid = wapi.check_svid(make_token(iat=10**400), audience=AUDIENCE, now=NOW)
    assert svid.issued_at is None


# ── a fake Workload API on a real socket ──


class FakeWorkloadApi:
    """Answers ``FetchJWTSVID`` over h2; records what each call carried."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.server: Any = None
        self.svids: list[tuple[str, str]] = []
        self.grpc_status = "0"
        self.grpc_message = ""
        self.http_status = "200"
        self.body: Optional[bytes] = None
        self.trailers_only = False
        self.stall = False
        self.calls: list[dict] = []
        self._pending: dict[int, tuple[bytes, list[tuple[str, str]]]] = {}

    async def __aenter__(self) -> "FakeWorkloadApi":
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import h2.config
        import h2.connection
        import h2.events
        import h2.exceptions

        conn = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
        )
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        headers: dict = {}
        body = bytearray()
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                for event in conn.receive_data(chunk):
                    if isinstance(event, h2.events.RequestReceived):
                        headers = dict(event.headers)
                    elif isinstance(event, h2.events.DataReceived):
                        body.extend(event.data)
                        conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    elif isinstance(event, h2.events.StreamEnded) and not self.stall:
                        self._answer(conn, event.stream_id, headers, bytes(body))
                self._flush(conn)
                writer.write(conn.data_to_send())
                await writer.drain()
        except (ConnectionError, h2.exceptions.ProtocolError):
            return
        finally:
            writer.close()

    def _answer(self, conn: Any, stream_id: int, headers: dict, body: bytes) -> None:
        request = {n: v.decode() for n, _w, v in wapi._fields(wapi.grpc_unframe(body))}
        self.calls.append({"headers": headers, "request": request})
        status = [(":status", self.http_status), ("content-type", "application/grpc")]
        trailers = [("grpc-status", self.grpc_status)]
        if self.grpc_message:
            trailers.append(("grpc-message", self.grpc_message))
        if self.trailers_only:
            conn.send_headers(stream_id, status + trailers, end_stream=True)
            return
        conn.send_headers(stream_id, status)
        data = self.body if self.body is not None else wapi.grpc_frame(_response(self.svids))
        self._pending[stream_id] = (data, trailers)

    def _flush(self, conn: Any) -> None:
        """Send each answer as far as flow control allows, then its trailers."""
        for stream_id, (data, trailers) in list(self._pending.items()):
            while data:
                room = min(conn.local_flow_control_window(stream_id), conn.max_outbound_frame_size)
                if room <= 0:
                    break
                conn.send_data(stream_id, data[:room])
                data = data[room:]
            if data:
                self._pending[stream_id] = (data, trailers)
            else:
                del self._pending[stream_id]
                conn.send_headers(stream_id, trailers, end_stream=True)


@pytest.fixture
def sock_path() -> Any:
    with tempfile.TemporaryDirectory(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()) as d:
        yield os.path.join(d, "a.sock")


@unix_only
@pytest.mark.asyncio
async def test_fetch_sends_the_audience_and_the_security_header(sock_path: str) -> None:
    token = make_token(iat=0, exp=4_000_000_000)
    async with FakeWorkloadApi(sock_path) as api:
        api.svids = [(SUBJECT, token)]
        svid = await wapi.fetch_jwt_svid("unix://" + sock_path, AUDIENCE, spiffe_id=SUBJECT)
    assert svid.token == token and svid.spiffe_id == SUBJECT
    (call,) = api.calls
    assert call["headers"][":path"] == wapi.FETCH_JWT_SVID_PATH
    assert call["headers"]["workload.spiffe.io"] == "true"
    assert call["headers"]["content-type"] == "application/grpc"
    assert call["request"] == {1: AUDIENCE, 2: SUBJECT}


@unix_only
@pytest.mark.asyncio
async def test_a_grpc_error_is_reported_by_status_without_any_token(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path) as api:
        api.trailers_only = True
        api.grpc_status = "7"
        api.grpc_message = "stand-in refusal"
        with pytest.raises(wapi.WorkloadApiError, match="grpc-status 7: stand-in refusal"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_an_empty_answer_is_refused(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path):
        with pytest.raises(wapi.WorkloadApiError, match="no JWT-SVID"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_a_missing_socket_is_named_by_error_type(sock_path: str) -> None:
    with pytest.raises(wapi.WorkloadApiError, match="could not be reached"):
        await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_a_token_with_a_malformed_audience_is_a_workload_api_error(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path) as api:
        api.svids = [(SUBJECT, make_token(aud=5, iat=0, exp=4_000_000_000))]
        with pytest.raises(wapi.WorkloadApiError, match="audience"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_an_http_error_status_is_refused(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path) as api:
        api.http_status = "503"
        api.trailers_only = True
        with pytest.raises(wapi.WorkloadApiError, match="HTTP 503"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_an_answer_over_the_cap_is_refused(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path) as api:
        api.body = b"\x00" * (wapi.ANSWER_MAX_BYTES + 1)
        with pytest.raises(wapi.WorkloadApiError, match="too large"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE)


@unix_only
@pytest.mark.asyncio
async def test_an_agent_that_never_answers_times_out(sock_path: str) -> None:
    async with FakeWorkloadApi(sock_path) as api:
        api.stall = True
        with pytest.raises(wapi.WorkloadApiError, match="in time"):
            await wapi.fetch_jwt_svid(sock_path, AUDIENCE, timeout=0)
