"""The loopback openers the Sage driver sends its internal secret with.

``loopback_urlopen`` is the proxy- and redirect-disabled opener, split out of
``review_driver`` unchanged; ``review_driver`` re-exports it so callers keep using
``review_driver.loopback_urlopen``.

``challenge_verified_urlopen`` is the send for a TCP-only gateway (no dashboard
socket). It proves the peer of the connected socket with the gateway's
challenge-response (:mod:`kiro_crew.loopback_peer_challenge`) before any
credential byte is written, then sends the request on that same connection. The
proof needs only the internal secret this backend already reads and the
connection itself, so it holds inside the agent sandbox, where the gateway's
``/proc/<pid>`` entries cannot be read.
"""

from __future__ import annotations

import socket
import time
import urllib.request

# Every loopback call the driver makes carries X-Internal-Secret, and urlopen
# honours HTTP_PROXY for loopback addresses, so a proxied environment would send
# the secret to the proxy in cleartext. The fallback must therefore stay
# proxy-disabled too -- degrading to a bare urlopen would leave the standalone
# path (`python3 sage_lib/review_driver.py`) carrying the leak this closes.
#
# It must ALSO refuse redirects, for the same reason the real helper does.
# "These endpoints never return a redirect" is wrong for `_probe`, whose entire
# job is to dial candidate ports that may have something other than our gateway
# listening. A 302 from one of those would replay the secret to whatever host
# Location names.
try:
    from kiro_crew.loopback_http import loopback_urlopen  # type: ignore
except ImportError:  # pragma: no cover - standalone fallback

    class _FallbackNoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def loopback_urlopen(req: urllib.request.Request | str, timeout: float):  # type: ignore
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _FallbackNoRedirect()
        ).open(req, timeout=timeout)


#: Upper bound on the challenge exchange, whatever the caller's timeout: a
#: genuine gateway answers ``/api/health`` in milliseconds.
CHALLENGE_TIMEOUT_SECONDS = 5.0

#: A genuine ``/api/health`` answer is a few hundred bytes. Anything past these
#: bounds is not the gateway and is refused rather than buffered.
_MAX_HEAD_BYTES = 16 * 1024
_MAX_BODY_BYTES = 16 * 1024


class ChallengeRefused(Exception):
    """The peer did not answer the challenge with this gateway's proof."""


def _is_ascii_number(text: str, max_digits: int) -> bool:
    """Whether *text* is 1..*max_digits* ASCII digits.

    ``str.isdigit`` alone also accepts digits ``int`` cannot parse (``"²"``), so
    the ASCII test comes first and the length bound keeps ``int`` cheap.
    """
    return 0 < len(text) <= max_digits and text.isascii() and text.isdigit()


def _read_challenge_answer(sock: socket.socket, deadline: float) -> tuple[int, dict, int]:
    """Read ONE HTTP response from *sock*: ``(status, headers, body_length)``.

    Consumes exactly that response and nothing after it, so the next request
    on the connection starts clean. Header names are lowercased; a repeated
    header is kept as a list. Raises :class:`ChallengeRefused` for anything that
    is not a single, bounded, ``Content-Length``-framed response that keeps the
    connection open.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > _MAX_HEAD_BYTES:
            raise ChallengeRefused("the challenge answer's header is too large")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChallengeRefused("the peer did not answer the challenge in time")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError) as exc:
            raise ChallengeRefused("the peer did not answer the challenge in time") from exc
        if not chunk:
            raise ChallengeRefused("the peer closed the connection before answering")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > _MAX_HEAD_BYTES:
        raise ChallengeRefused("the challenge answer's header is too large")
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or not _is_ascii_number(parts[1], 3):
        raise ChallengeRefused("the challenge answer is not an HTTP/1.1 response")
    headers: dict[str, object] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            raise ChallengeRefused("the challenge answer has a malformed header")
        key = name.strip().lower()
        value = value.strip()
        if key in headers:
            prior = headers[key]
            headers[key] = (prior if isinstance(prior, list) else [prior]) + [value]
        else:
            headers[key] = value
    if "transfer-encoding" in headers:
        raise ChallengeRefused("the challenge answer is not Content-Length framed")
    length = headers.get("content-length")
    if (
        not isinstance(length, str)
        or not _is_ascii_number(length, 6)
        or int(length) > _MAX_BODY_BYTES
    ):
        raise ChallengeRefused("the challenge answer has no usable Content-Length")
    connection = headers.get("connection")
    if isinstance(connection, list) or (
        isinstance(connection, str) and connection.lower() == "close"
    ):
        raise ChallengeRefused("the peer closes the connection the proof is bound to")
    body_len = int(length)
    while len(rest) < body_len:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChallengeRefused("the peer did not answer the challenge in time")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(min(4096, body_len - len(rest)))
        except (socket.timeout, TimeoutError) as exc:
            raise ChallengeRefused("the peer did not answer the challenge in time") from exc
        if not chunk:
            raise ChallengeRefused("the peer closed the connection mid-answer")
        rest += chunk
    if len(rest) != body_len:
        raise ChallengeRefused("the peer sent bytes past its challenge answer")
    return int(parts[1]), headers, body_len


def prove_tcp_peer(sock: socket.socket, port: int, secret: str, timeout: float) -> None:
    """Prove the peer of the CONNECTED *sock* holds *secret*, or raise.

    Sends ``GET /api/health`` with a fresh nonce and no credential, reads the
    answer, and checks its proof against this side's view of the connection (the
    gateway's end is our peer address, ours is our local address). Raises
    :class:`ChallengeRefused` unless the proof matches exactly. The socket's own
    timeout is restored on return, so the request that follows keeps the
    caller's.
    """
    from kiro_crew.loopback_peer_challenge import (
        CHALLENGE_HEADER,
        PROOF_HEADER,
        new_nonce,
        peer_proof,
        proof_matches,
    )

    if not secret:
        raise ChallengeRefused("no internal secret to prove the gateway with")
    nonce = new_nonce()
    expected = peer_proof(secret, nonce, sock.getpeername(), sock.getsockname())
    if expected is None:
        raise ChallengeRefused("the connection is not a loopback TCP connection")
    request = (
        f"GET /api/health HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{int(port)}\r\n"
        f"Accept: application/json\r\n"
        f"{CHALLENGE_HEADER}: {nonce}\r\n\r\n"
    ).encode("ascii")
    previous = sock.gettimeout()
    deadline = time.monotonic() + min(float(timeout), CHALLENGE_TIMEOUT_SECONDS)
    try:
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        try:
            sock.sendall(request)
        except OSError as exc:
            raise ChallengeRefused(f"the challenge could not be sent: {exc}") from exc
        try:
            status, headers, _ = _read_challenge_answer(sock, deadline)
        except OSError as exc:
            raise ChallengeRefused(f"the challenge answer could not be read: {exc}") from exc
    finally:
        sock.settimeout(previous)
    if status != 200:
        raise ChallengeRefused(f"the peer answered the challenge with HTTP {status}")
    if not proof_matches(expected, headers.get(PROOF_HEADER.lower())):
        raise ChallengeRefused("the peer's answer does not prove it holds this gateway's secret")


def challenge_verified_urlopen(
    req: urllib.request.Request, port: int, timeout: float, *, operation: str
):
    """Send *req* to a TCP-only gateway only after the peer passes the challenge.

    The secret the proof is keyed with is the one *req* carries
    (``X-Internal-Secret``), so the request goes only to the holder of the very
    credential it would reveal. The proof runs on the connected socket before
    the first byte of *req* (:func:`kiro_crew.loopback_http.tcp_verified_urlopen`
    calls the hook between ``connect()`` and the send); a refusal emits a
    ``denied`` SEL event and raises ``_GatewayPeerRefused``, and a pass emits
    ``allowed`` before the request is written, as the socket and ``/proc``
    proofs do.
    """
    from kiro_crew.cli_server_peer import (
        _audit_peer_allowed,
        _audit_peer_denied,
        _GatewayPeerRefused,
    )
    from kiro_crew.loopback_http import tcp_verified_urlopen

    secret = req.get_header("X-internal-secret") or ""

    def _verify(sock: socket.socket) -> None:
        try:
            prove_tcp_peer(sock, port, secret, timeout)
        except Exception as exc:
            # Every failure is an audited refusal, including one the answer's
            # parsing did not anticipate: a bare exception here would refuse the
            # send without the SEL denial.
            reason = str(exc) if isinstance(exc, ChallengeRefused) else type(exc).__name__
            _audit_peer_denied(operation, port, f"gateway challenge failed: {reason}")
            raise _GatewayPeerRefused(
                f"the TCP-only gateway peer could not be proven: {reason}"
            ) from None
        _audit_peer_allowed(operation, port)

    return tcp_verified_urlopen(req, timeout, verify_peer=_verify)


__all__ = [
    "CHALLENGE_TIMEOUT_SECONDS",
    "ChallengeRefused",
    "challenge_verified_urlopen",
    "loopback_urlopen",
    "prove_tcp_peer",
]
