"""The TCP-only gateway peer challenge, against real loopback sockets.

When the gateway has no dashboard socket (TCP-only), the Code Review Sage sender
proves its peer with a challenge-response before it writes the internal secret
(``sage_lib.loopback.challenge_verified_urlopen``; the message layout is
``kiro_crew.loopback_peer_challenge``, the gateway's answer is ``/api/health``).

Every case here dials a real listener on 127.0.0.1. The genuine gateway is the
REAL ``api_health`` handler served by aiohttp. Each impostor records every byte it
was sent, so "nothing is sent" is asserted on what the impostor actually saw.
"""

from __future__ import annotations

import socket
import threading
import urllib.request
from unittest.mock import MagicMock

import pytest
from sage_tcp_gateway_helpers import RawListener, RealTcpGateway, challenge_nonce, http_response

from kiro_crew import cli_server
from kiro_crew import loopback_peer_challenge as challenge
from kiro_crew.apps.builtins.code_review_sage.sage_lib import loopback, review_driver
from kiro_crew.cli_server_peer import _GatewayPeerRefused
from kiro_crew.dashboard.handlers import core as core_mod

_SECRET = "0123456789abcdef0123456789abcdef"


class _SelSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kwargs) -> None:
        self.calls.append(kwargs)

    def outcomes(self) -> list[str]:
        return [c["outcome"] for c in self.calls]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    # A data home of this test's own, so no dashboard socket can exist for any
    # port dialled here and the sender takes the TCP-only arm.
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    spy = _SelSpy()
    _floor_monkeypatch.setattr(cli_server, "sel", lambda: spy)
    return spy


def _send(port: int, secret: str = _SECRET, *, timeout: float = 5):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/spawn", headers={"X-Internal-Secret": secret}
    )
    return loopback.challenge_verified_urlopen(req, port, timeout, operation="sage_api")


@pytest.fixture
def gateway():
    gw = RealTcpGateway(_SECRET).start()
    try:
        yield gw
    finally:
        gw.stop()


def _listener(respond):
    return RawListener(respond).start()


# ── A genuine TCP-only gateway passes ────────────────────────────────────────


def test_a_genuine_tcp_only_gateway_passes_and_receives_the_request(gateway, _isolated):
    with _send(gateway.port) as resp:
        assert resp.status == 200
        assert resp.read() == b'{"spawned": true}'
    assert gateway.received_secrets == [_SECRET]
    assert _isolated.outcomes() == ["allowed"]


def test_the_request_rides_the_connection_the_proof_was_made_on(gateway, monkeypatch):
    """One TCP connection carries both the challenge and the request."""
    connects: list[socket.socket] = []
    real = socket.create_connection

    def counting(*args, **kwargs):
        sock = real(*args, **kwargs)
        connects.append(sock)
        return sock

    monkeypatch.setattr(socket, "create_connection", counting)
    with _send(gateway.port) as resp:
        assert resp.status == 200
    assert len(connects) == 1
    assert gateway.received_secrets == [_SECRET]


def test_sage_api_request_reaches_a_tcp_only_gateway(gateway, monkeypatch):
    """End to end through the Sage driver: a TCP-only gateway is reached."""
    base = f"http://127.0.0.1:{gateway.port}"
    monkeypatch.setattr(review_driver, "_gateway_base", lambda: base)
    monkeypatch.setattr(review_driver, "_local_secret", lambda port: _SECRET)
    assert review_driver._api_request("GET", "/api/spawn") == {"spawned": True}
    assert review_driver._probe(base, _SECRET) is True
    assert gateway.received_secrets == [_SECRET, _SECRET]


# ── Impostors are refused and receive nothing ────────────────────────────────


def _assert_refused_and_nothing_sent(port: int, listener: RawListener, spy: _SelSpy) -> None:
    with pytest.raises(_GatewayPeerRefused):
        _send(port)
    listener.stop()
    assert _SECRET.encode() not in listener.received
    assert b"x-internal-secret" not in listener.received.lower()
    assert spy.outcomes() == ["denied"]


def test_an_impostor_that_answers_without_a_proof_is_refused(_isolated):
    def respond(lst, conn):
        lst.recv_head(conn)
        conn.sendall(http_response({"Content-Type": "application/json"}, b'{"ok": true}'))
        lst.drain(conn)

    lst = _listener(respond)
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)


def test_an_impostor_that_guesses_a_proof_is_refused(_isolated):
    def respond(lst, conn):
        lst.recv_head(conn)
        conn.sendall(http_response({challenge.PROOF_HEADER: "ab" * 32}))
        lst.drain(conn)

    lst = _listener(respond)
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)


def test_an_impostor_that_relays_the_challenge_to_the_real_gateway_is_refused(gateway, _isolated):
    """The relay gets a genuine proof, but for ITS connection, not the sender's."""
    relayed: list[bytes] = []

    def respond(lst, conn):
        head = lst.recv_head(conn)
        with socket.create_connection(("127.0.0.1", gateway.port), timeout=5) as up:
            up.sendall(head)
            answer = b""
            while b"\r\n\r\n" not in answer:
                answer += up.recv(4096)
            length = int(
                [
                    ln.split(b":", 1)[1]
                    for ln in answer.split(b"\r\n")
                    if ln.lower().startswith(b"content-length:")
                ][0]
            )
            while len(answer.partition(b"\r\n\r\n")[2]) < length:
                answer += up.recv(4096)
        relayed.append(answer)
        conn.sendall(answer)
        lst.drain(conn)

    lst = _listener(respond)
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)
    # The relay really obtained the gateway's proof; it just does not verify here.
    assert relayed and challenge.PROOF_HEADER.lower().encode() in relayed[0].lower()
    assert gateway.received_secrets == []


def test_a_proof_captured_from_an_earlier_answer_is_not_accepted(gateway, _isolated):
    """A replayed answer carries another nonce and another connection."""
    with socket.create_connection(("127.0.0.1", gateway.port), timeout=5) as probe:
        nonce = challenge.new_nonce()
        probe.sendall(
            f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"{challenge.CHALLENGE_HEADER}: {nonce}\r\nConnection: close\r\n\r\n".encode()
        )
        captured = b""
        while chunk := probe.recv(4096):
            captured += chunk
    proof = [
        ln.split(b":", 1)[1].strip().decode()
        for ln in captured.split(b"\r\n")
        if ln.lower().startswith(challenge.PROOF_HEADER.lower().encode())
    ][0]

    def respond(lst, conn):
        lst.recv_head(conn)
        conn.sendall(http_response({challenge.PROOF_HEADER: proof}))
        lst.drain(conn)

    lst = _listener(respond)
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)


# ── Nearby inputs: restart, slow responder, malformed replies ───────────────


def test_a_port_reused_after_a_gateway_restart(_isolated):
    """A restarted gateway holds a new secret; only that secret proves it.

    The sender reads the per-port secret on every call, so after a restart it
    holds the new one and passes. A sender still holding the old generation's
    secret is refused and the new gateway receives nothing.
    """
    old = RealTcpGateway("1" * 32).start()
    port = old.port
    old.stop()
    new = RealTcpGateway("2" * 32, port=port).start()
    try:
        with pytest.raises(_GatewayPeerRefused):
            _send(port, "1" * 32)
        assert new.received_secrets == []
        with _send(port, "2" * 32) as resp:
            assert resp.status == 200
        assert new.received_secrets == ["2" * 32]
    finally:
        new.stop()


def test_a_port_taken_by_an_impostor_after_the_gateway_exits(_isolated):
    old = RealTcpGateway(_SECRET).start()
    port = old.port
    old.stop()

    def respond(lst, conn):
        lst.recv_head(conn)
        conn.sendall(http_response({}))
        lst.drain(conn)

    lst = RawListener(respond, port=port).start()
    _assert_refused_and_nothing_sent(port, lst, _isolated)


def test_a_slow_responder_is_refused_within_the_challenge_bound(_isolated, monkeypatch):
    monkeypatch.setattr(loopback, "CHALLENGE_TIMEOUT_SECONDS", 0.3)

    release = threading.Event()

    def respond(lst, conn):
        lst.recv_head(conn)
        # Holds the answer until the sender has given up, so it never arrives
        # within the bound whatever the host's speed.
        release.wait(30)
        lst.drain(conn, seconds=0.5)

    lst = _listener(respond)
    try:
        with pytest.raises(_GatewayPeerRefused, match="in time"):
            _send(lst.port, timeout=30)
    finally:
        release.set()
    lst.stop()
    assert _SECRET.encode() not in lst.received


def _genuine_proof_for(conn: socket.socket, head: bytes) -> str:
    """The proof the REAL gateway would give on *conn*: lets a malformed-framing
    case carry a valid proof, so the framing check alone is what refuses it."""
    proof = challenge.peer_proof(
        _SECRET, challenge_nonce(head), conn.getsockname(), conn.getpeername()
    )
    assert proof is not None
    return proof


def _framed(builder):
    def respond(lst, conn):
        head = lst.recv_head(conn)
        conn.sendall(builder(_genuine_proof_for(conn, head)))
        lst.drain(conn)

    return respond


_P = challenge.PROOF_HEADER

_MALFORMED = {
    "not_http": lambda p: b"hello there\r\n\r\n",
    "http_1_0": lambda p: http_response({_P: p}).replace(b"HTTP/1.1", b"HTTP/1.0", 1),
    "status_403": lambda p: http_response({_P: p}, status="403 Forbidden"),
    "connection_close": lambda p: http_response({_P: p, "Connection": "close"}),
    "chunked": lambda p: (
        f"HTTP/1.1 200 OK\r\n{_P}: {p}\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{{}}\r\n0\r\n\r\n"
    ).encode(),
    "no_content_length": lambda p: f"HTTP/1.1 200 OK\r\n{_P}: {p}\r\n\r\n{{}}".encode(),
    "duplicate_proof": lambda p: http_response({_P: p, _P.lower(): p}),
    "trailing_bytes": lambda p: http_response({_P: p}) + b"HTTP/1.1 200 OK\r\n",
    "oversized_head": lambda p: http_response({_P: p, "X-Pad": "a" * 20000}),
    "header_without_colon": lambda p: (
        f"HTTP/1.1 200 OK\r\n{_P}: {p}\r\nbroken\r\nContent-Length: 2\r\n\r\n{{}}"
    ).encode(),
    "closes_without_answer": lambda p: b"",
    # One byte 0xB2 reads as "²" under latin-1: str.isdigit() accepts it, int() does not.
    "non_ascii_content_length": lambda p: (
        f"HTTP/1.1 200 OK\r\n{_P}: {p}\r\nContent-Length: ".encode() + b"\xb2\r\n\r\n{}"
    ),
    "non_ascii_status": lambda p: (
        b"HTTP/1.1 \xb200 OK\r\n" + f"{_P}: {p}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
    ),
    "huge_content_length": lambda p: (
        f"HTTP/1.1 200 OK\r\n{_P}: {p}\r\nContent-Length: {'9' * 40}\r\n\r\n{{}}"
    ).encode(),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED))
def test_a_malformed_answer_is_refused_even_with_a_valid_proof(case, _isolated):
    lst = _listener(_framed(_MALFORMED[case]))
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)


def test_an_unexpected_error_in_the_challenge_is_an_audited_refusal(_isolated, monkeypatch):
    """A failure the answer's parsing did not anticipate still records the denial."""

    def boom(*_a, **_k):
        raise RuntimeError("unanticipated")

    monkeypatch.setattr(loopback, "prove_tcp_peer", boom)

    def respond(lst, conn):
        lst.drain(conn)

    lst = _listener(respond)
    _assert_refused_and_nothing_sent(lst.port, lst, _isolated)


def test_a_well_framed_answer_with_the_valid_proof_lets_the_request_through(_isolated):
    """Control for the malformed cases: the same harness, framed correctly, passes,
    so each refusal above comes from the one defect that case introduces."""

    def respond(lst, conn):
        head = lst.recv_head(conn)
        conn.sendall(http_response({_P: _genuine_proof_for(conn, head)}))
        lst.recv_head(conn)
        conn.sendall(http_response({"Connection": "close"}, b'{"spawned": true}'))

    lst = _listener(respond)
    with _send(lst.port) as resp:
        assert resp.read() == b'{"spawned": true}'
    lst.stop()
    assert b"x-internal-secret: " + _SECRET.encode() in lst.received.lower()


# ── The unix-socket path is unchanged ────────────────────────────────────────


def test_with_a_dashboard_socket_the_sender_keeps_the_socket_proof(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(review_driver, "_existing_dashboard_socket", lambda port: "/run/d.sock")
    monkeypatch.setattr(
        review_driver,
        "_owner_verified_secret_urlopen",
        lambda *a, **k: calls.append("socket") or MagicMock(),
    )
    monkeypatch.setattr(
        review_driver,
        "challenge_verified_urlopen",
        lambda *a, **k: calls.append("challenge") or MagicMock(),
    )
    review_driver._verified_urlopen(MagicMock(), 41234, 3, operation="sage_api")
    assert calls == ["socket"]


def test_without_a_dashboard_socket_the_sender_takes_the_challenge(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(review_driver, "_existing_dashboard_socket", lambda port: "")
    monkeypatch.setattr(
        review_driver,
        "_owner_verified_secret_urlopen",
        lambda *a, **k: calls.append("socket") or MagicMock(),
    )
    monkeypatch.setattr(
        review_driver,
        "challenge_verified_urlopen",
        lambda *a, **k: calls.append("challenge") or MagicMock(),
    )
    review_driver._verified_urlopen(MagicMock(), 41234, 3, operation="sage_api")
    assert calls == ["challenge"]


# ── The gateway's side ───────────────────────────────────────────────────────


def _health_request(headers: dict, remote: str = "127.0.0.1", sockname=("127.0.0.1", 5476)):
    req = MagicMock()
    req.remote = remote
    req.headers = headers
    req.app = {"local_secret": _SECRET, "allowed_origins": {"http://127.0.0.1:5476"}}
    req.transport.get_extra_info.side_effect = {
        "sockname": sockname,
        "peername": ("127.0.0.1", 51000),
    }.get
    return req


def test_the_gateway_answers_a_direct_local_tcp_challenge():
    nonce = challenge.new_nonce()
    proof = core_mod._gateway_peer_proof(_health_request({challenge.CHALLENGE_HEADER: nonce}))
    assert proof == challenge.peer_proof(_SECRET, nonce, ("127.0.0.1", 5476), ("127.0.0.1", 51000))


@pytest.mark.parametrize(
    "headers, remote, sockname",
    [
        ({}, "127.0.0.1", ("127.0.0.1", 5476)),  # no challenge
        ({challenge.CHALLENGE_HEADER: "not-a-nonce"}, "127.0.0.1", ("127.0.0.1", 5476)),
        ({challenge.CHALLENGE_HEADER: "A" * 64}, "127.0.0.1", ("127.0.0.1", 5476)),
        ({challenge.CHALLENGE_HEADER: "a" * 64}, "203.0.113.9", ("127.0.0.1", 5476)),
        (
            {challenge.CHALLENGE_HEADER: "a" * 64, "X-Forwarded-For": "203.0.113.9"},
            "127.0.0.1",
            ("127.0.0.1", 5476),
        ),
        ({challenge.CHALLENGE_HEADER: "a" * 64}, "127.0.0.1", "/run/dashboard.sock"),
        # A DNS-rebound page: loopback connection, but a Host this gateway does not serve.
        (
            {challenge.CHALLENGE_HEADER: "a" * 64, "Host": "rebound.example:5476"},
            "127.0.0.1",
            ("127.0.0.1", 5476),
        ),
    ],
    ids=[
        "no-challenge",
        "bad-nonce",
        "uppercase-nonce",
        "remote",
        "forwarded",
        "unix-socket",
        "rebound-host",
    ],
)
def test_the_gateway_gives_no_proof_outside_a_direct_local_tcp_challenge(headers, remote, sockname):
    assert core_mod._gateway_peer_proof(_health_request(headers, remote, sockname)) is None


def test_the_gateway_gives_no_proof_without_a_secret():
    req = _health_request({challenge.CHALLENGE_HEADER: "a" * 64})
    req.app = {"allowed_origins": set()}
    assert core_mod._gateway_peer_proof(req) is None


def test_endpoints_name_an_ipv4_caller_the_same_way_on_a_dual_stack_listener():
    assert challenge.endpoint_text(("::ffff:127.0.0.1", 5476, 0, 0)) == "127.0.0.1 5476"
    assert challenge.endpoint_text(("127.0.0.1", 5476)) == "127.0.0.1 5476"
    assert challenge.endpoint_text(("::1", 5476, 0, 0)) == "::1 5476"
    for bad in ("/run/d.sock", ("127.0.0.1",), ("nope", 1), ("127.0.0.1", 0), ("127.0.0.1", True)):
        assert challenge.endpoint_text(bad) is None
