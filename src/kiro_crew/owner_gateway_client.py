"""The dashboard owner's client for the gateway's unix socket, used by ``kirocrew spawn``.

The CLI acts as the dashboard owner at a terminal. It mints a short-lived owner
token from ``/api/token/local`` with the local secret and presents it on each
request, exactly as the owner's browser presents its session.

Two properties hold for every request, the mint included:

* It travels the dashboard unix socket ONLY (``unix_socket_urlopen`` has no TCP
  handler), because the secret and the token are credentials and a loopback port
  can be answered by any local process once the gateway lets go of it.
* Before a byte is written, the connected peer must be the gateway process the
  data home recorded for the port (``run/gateway-<port>.pid`` and its start
  identity). The socket path sits in a directory its owner can rewrite, so a
  listener that took the path is refused here and never receives a credential.

``kirocrew app`` (``app_lifecycle_client``) mints the same way without the peer
check; folding it onto this module is a separate change.
"""

from __future__ import annotations

import http.client
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from kiro_crew.app_lifecycle_client import _deadline_expired, _socket_unavailable
from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import unix_socket_urlopen
from kiro_crew.mcp_gateway.socketsec import get_peer_pid
from kiro_crew.terminal_safe import safe_terminal_line

#: How long a mint that met ``invalid_secret`` waits before its one retry. A
#: starting gateway binds its socket before it writes its credentials, so for a
#: moment the file on disk still holds the previous run's secret.
_MINT_SECRET_RETRY_SECS = 1.0

#: What the CLI tells the user for each ``/api/token/local`` refusal ``code``.
_MINT_403_REMEDY = {
    "invalid_secret": "The local secret did not match; the gateway may be restarting, so retry.",
    "member_owner_token_refused": (
        "kirocrew spawn acts as the dashboard owner, so it must run on the gateway's "
        "host as the same user, outside an agent sandbox; a gateway running as PID 1 "
        "needs an init such as tini (--init)."
    ),
    # The socket's peer is not the gateway's own user (``sudo``, say).
    "loopback_only": (
        "kirocrew spawn must run as the user the gateway runs as, not through sudo "
        "or another account."
    ),
}
#: A 403 with no code this CLI knows, or a 404: a gateway older than its CLI.
_MINT_OLD_GATEWAY = "This gateway does not mint over its dashboard socket; restart or upgrade it."


class OwnerGatewayError(RuntimeError):
    """A request the gateway refused, or one that could not complete.

    ``kind`` is what the caller decides on:

    * ``"refused"``: the gateway answered with an HTTP error, and ``status`` and
      ``code`` say which; or this user may not open the socket (``status`` 0),
      so nothing was sent.
    * ``"no_socket"``: nothing listens at the socket path (not running,
      restarting, or a gateway that serves no socket), so nothing was sent.
    * ``"outcome_unknown"``: the request was sent and then our deadline passed, the
      connection dropped or the answer was cut off, so the gateway may have acted
      on it.
    * ``"transport"``: the connection failed before the request was sent, or the
      owner-token mint failed, so this request was not sent.
    * ``"malformed"``: the gateway answered with something that is not JSON.
    * ``"not_the_gateway"``: the socket's listener is not the recorded gateway, so
      nothing was sent.

    ``retry_after`` is a refusal's ``Retry-After`` in seconds, 0 when it sent none.

    The message is reduced to one printable line: the CLI prints it, and part of
    it can come from the gateway.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int = 0,
        code: str = "",
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(safe_terminal_line(message))
        self.kind = kind
        self.status = status
        self.code = code
        self.retry_after = retry_after


def gateway_peer_verifier(port: int) -> Callable[[socket.socket], None]:
    """A connect-time check that the socket's peer is the gateway serving *port*.

    The kernel names the listener's pid (``SO_PEERCRED`` / ``LOCAL_PEERPID``); it
    must equal the pid the gateway recorded under ``run/`` (written ``0600`` in the
    ``0700`` run directory, which sandboxed agents cannot write), and that pid must
    still be the process that recorded it. Deny by default: an unreadable peer or
    record refuses too.
    """

    def _verify(sock: socket.socket) -> None:
        peer = get_peer_pid(sock)
        record = run_marker.read_pid_record(port)
        if (
            peer is not None
            and record is not None
            and record[0] == peer
            and record[1]
            and record[1] == run_marker.pid_start_token(peer)
        ):
            return
        who = "an unidentifiable process" if peer is None else f"pid {peer}"
        expected = "no gateway is recorded" if record is None else f"the gateway is pid {record[0]}"
        raise OwnerGatewayError(
            f"refusing to send the owner credential: {dashboard_socket_path(port)} is "
            f"answered by {who}, but for port {port} {expected}. Restart the gateway "
            "if this persists.",
            kind="not_the_gateway",
        )

    return _verify


def _no_socket(exc: OSError) -> bool:
    """Whether nothing could listen at the socket, so nothing was sent.

    ``_socket_unavailable``'s answer, plus a socket path too long for
    ``sun_path``: a gateway whose data home is that deep could not bind it either.
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return _socket_unavailable(exc) or (
        isinstance(reason, OSError) and "AF_UNIX path too long" in str(reason)
    )


def _refusal(exc: urllib.error.HTTPError) -> OwnerGatewayError:
    """The gateway's own ``error`` and ``code`` for *exc*, or its status line."""
    detail, code = f"{exc.code} {exc.reason}", ""
    try:
        body = json.loads(exc.read())
    except Exception:
        body = None
    if isinstance(body, dict):
        if isinstance(body.get("error"), str) and body["error"]:
            detail = body["error"]
        if isinstance(body.get("code"), str):
            code = body["code"]
    try:
        hint = exc.headers.get("Retry-After") if exc.headers is not None else None
        retry_after = max(0.0, float(hint or 0))
    except (TypeError, ValueError):
        retry_after = 0.0  # an HTTP-date, or garbage: no hint
    return OwnerGatewayError(
        detail, kind="refused", status=exc.code, code=code, retry_after=retry_after
    )


def owner_call(
    port: int,
    path: str,
    *,
    token: str = "",
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> dict:
    """GET *path* (POST when *body* is given) on the gateway's socket; the JSON answer.

    *token* is sent as the ``token`` query parameter, the way the gateway reads an
    owner token on an API route. Raises :class:`OwnerGatewayError` for every
    failure, so a caller can decide on ``kind`` alone.
    """
    url = f"http://127.0.0.1:{port}{path}"
    if token:
        url += ("&" if "?" in path else "?") + "token=" + urllib.parse.quote(token, safe="")
    data = json.dumps(body).encode() if body is not None else None
    sent_headers = dict(headers or {})
    if data is not None:
        sent_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=sent_headers)
    socket_path = dashboard_socket_path(port)
    try:
        with unix_socket_urlopen(
            req, timeout=timeout, socket_path=socket_path, verify_peer=gateway_peer_verifier(port)
        ) as response:
            raw = response.read()
    except OwnerGatewayError:
        raise
    except urllib.error.HTTPError as exc:
        raise _refusal(exc) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if _no_socket(exc):
            raise OwnerGatewayError(
                f"nothing answers the dashboard socket {socket_path}", kind="no_socket"
            ) from exc
        if isinstance(reason, PermissionError):
            raise OwnerGatewayError(
                f"cannot connect to the dashboard socket {socket_path}: permission denied",
                kind="refused",
            ) from exc
        if _deadline_expired(exc):
            raise OwnerGatewayError(
                "the gateway did not answer in time", kind="outcome_unknown"
            ) from exc
        if not isinstance(exc, urllib.error.URLError):
            # urllib wraps what fails while it connects and writes the request;
            # an error raised bare came from reading the answer, so the request
            # was sent (a ``RemoteDisconnected`` or a reset, say).
            raise OwnerGatewayError(
                f"the connection dropped after the request was sent: {reason}",
                kind="outcome_unknown",
            ) from exc
        raise OwnerGatewayError(f"cannot reach the gateway: {reason}", kind="transport") from exc
    except http.client.HTTPException as exc:
        raise OwnerGatewayError(
            f"the gateway's answer was cut off ({type(exc).__name__})", kind="outcome_unknown"
        ) from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OwnerGatewayError(
            "the gateway returned a malformed answer", kind="malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise OwnerGatewayError("the gateway returned a malformed answer", kind="malformed")
    return payload


def mint_owner_token(port: int, *, ttl: str, origin: str = "") -> tuple[str, float]:
    """Mint an owner token over the socket: ``(token, lifetime_secs)``.

    *origin* asks for the signed ``origin`` claim (``/api/token/local?origin=``),
    which also mints the token without a one-time-link nonce: it is only ever
    presented on the API. A 403 names its gate in ``code``, and the error carries
    the remedy that gate needs; an ``invalid_secret`` is retried once. A mint
    that cannot finish is ``transport``, never ``outcome_unknown``: the request
    the token was for has not been sent.
    """
    query = urllib.parse.urlencode({"ttl": ttl, **({"origin": origin} if origin else {})})
    for attempt in range(2):
        # Port-keyed, with no dial host: the request travels the socket, so the
        # secret is not paired to a TCP address (as in ``app_lifecycle_client``).
        secret = read_local_secret(port)
        if not secret:
            raise OwnerGatewayError(
                f"gateway not running (no local secret for port {port})", kind="no_socket"
            )
        try:
            payload = owner_call(
                port, f"/api/token/local?{query}", headers={"X-Local-Secret": secret}
            )
        except OwnerGatewayError as exc:
            if exc.kind == "outcome_unknown":
                raise OwnerGatewayError(
                    "the gateway did not answer the owner-token request, so this "
                    "request was not sent",
                    kind="transport",
                ) from exc
            if exc.status not in (403, 404):
                raise
            if exc.code == "invalid_secret" and attempt == 0:
                time.sleep(_MINT_SECRET_RETRY_SECS)
                continue
            remedy = _MINT_403_REMEDY.get(exc.code, _MINT_OLD_GATEWAY)
            raise OwnerGatewayError(
                f"{str(exc).rstrip('.')}. {remedy}",
                kind="refused",
                status=exc.status,
                code=exc.code,
            ) from exc
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise OwnerGatewayError("the gateway returned no owner token", kind="malformed")
        try:
            lifetime = float(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0.0
        return token, lifetime
    raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises


__all__ = [
    "OwnerGatewayError",
    "gateway_peer_verifier",
    "mint_owner_token",
    "owner_call",
]
