"""``kirocrew memory forget``: delete one semantic key through the running gateway.

The delete is the dashboard's own route, ``DELETE /api/memory/semantic/{key}``,
so it passes the same owner gate (``_memory_write_gate`` ->
``require_owner_dashboard_request``) a dashboard delete passes. The CLI holds no
authority of its own: it mints a short-lived dashboard credential through
``/api/token/local`` and the gateway decides.

Both requests travel only the dashboard's owner-only Unix socket, the same
transport ``app_lifecycle_client`` uses, so the minted credential cannot reach a
different process listening on the TCP port. Each connection is also checked,
before any byte is sent, to be answered by the gateway process recorded for that
port (:func:`_gateway_peer_verifier`). There is no TCP fallback and no in-process
delete: with no verified gateway answering, nothing is deleted.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from kiro_crew.app_lifecycle_client import _socket_unavailable
from kiro_crew.config.loader import read_local_secret
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.loopback_http import unix_socket_urlopen
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.terminal_safe import safe_terminal_line

_TIMEOUT_SECS = 30

#: The dashboard's own static session key, which the browser dashboard sends too.
#: ``_memory_write_gate`` checks it only after the owner gate has already passed.
_DASHBOARD_SESSION_KEY = "dashboard:ui"

#: What the CLI tells the operator when the route refuses for one of its own codes.
_REFUSAL_TEXT = {
    "value_changed": "the value changed after it was shown",
    "superseded_by_absent": "the --superseded-by key is not a live key",
    "superseded_by_self": "--superseded-by names the key being forgotten",
    "owner_only": "the gateway accepts this only from the owner",
}


class ForgetRefused(ValueError):
    """The gateway (or the lack of one) refused the forget; nothing was deleted."""

    def __init__(self, message: str) -> None:
        super().__init__(safe_terminal_line(message))


class AlreadyAbsent(ForgetRefused):
    """The gateway reports the key is not live."""


def _error_code(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """``(code, error)`` from a gateway error body, empty strings when absent."""
    try:
        body = json.loads(exc.read())
    except Exception:  # noqa: BLE001 -- peer bytes must not crash the CLI
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    code, error = body.get("code"), body.get("error")
    return (code if isinstance(code, str) else ""), (error if isinstance(error, str) else "")


def _gateway_peer_verifier(port: int):
    """A connect-time check that the socket is answered by this port's gateway.

    The socket file sits in a directory its owner can rewrite, so a same-UID
    process could rebind it and receive the local secret and the credential. The
    check is the recorded-identity check the unix arm of
    ``_owner_verified_secret_urlopen`` also applies
    (``cli_server_peer._connected_unix_peer_is_recorded_gateway``): the kernel
    names the listener's pid on the connected socket, and it must equal the pid
    the gateway recorded for *port*, with the start identity recorded beside it,
    or nothing is sent. A record that is missing, or whose start token does not
    match the live pid, refuses before connecting, so the operator hears why
    rather than a generic socket refusal.
    """
    from kiro_crew.cli_server_peer import (
        _audit_peer_allowed,
        _audit_peer_denied,
        _connected_unix_peer_is_recorded_gateway,
        _recorded_gateway_pid,
    )

    if _recorded_gateway_pid(port) is None:
        raise ForgetRefused(
            "the running gateway's pid record could not be read or does not match "
            "the running gateway, so the process answering its socket cannot be "
            "verified. If this is your own gateway, restart it to re-publish its "
            "identity record. Nothing was forgotten."
        )

    def _verify(sock: socket.socket) -> None:
        # Both arms of this permission decision are audited, in the same SEL
        # shape the CLI's other connection-bound proofs emit.
        if not _connected_unix_peer_is_recorded_gateway(sock, port):
            _audit_peer_denied(
                "memory_forget", port, "connected socket peer is not this install's gateway"
            )
            raise ForgetRefused(
                "the dashboard socket is not answered by the recorded gateway; "
                "nothing was forgotten."
            )
        _audit_peer_allowed("memory_forget", port)

    return _verify


def forget_via_gateway(key: str, *, expect_value_json: str, superseded_by: str | None) -> None:
    """Tombstone *key* through the gateway's owner-gated delete route.

    Returns only when the gateway answered ``ok``. Raises :class:`AlreadyAbsent` on
    404 and :class:`ForgetRefused` on every other refusal, including no gateway.
    """
    port, _evidence_backed = resolve_client_port_ex(None)
    # The socket carries the request, so the secret is the port-keyed one; see
    # app_lifecycle_client._post_lifecycle_action for why no dial_host is passed.
    secret = read_local_secret(port)
    if not secret:
        raise ForgetRefused(
            "no running gateway for this home; start it with `kirocrew gateway`. "
            "Nothing was forgotten."
        )
    socket_path = dashboard_socket_path(port)
    verify_peer = _gateway_peer_verifier(port)
    base = "http://127.0.0.1:%d" % port
    mint = urllib.request.Request(
        f"{base}/api/token/local?ttl=2m", headers={"X-Local-Secret": secret}
    )
    try:
        with unix_socket_urlopen(
            mint, timeout=_TIMEOUT_SECS, socket_path=socket_path, verify_peer=verify_peer
        ) as resp:
            minted = json.loads(resp.read())
    except ForgetRefused:
        raise
    except urllib.error.HTTPError as exc:
        _code, error = _error_code(exc)
        raise ForgetRefused(
            f"the gateway refused a local owner credential ({exc.code}): "
            f"{error or exc.reason}. Nothing was forgotten."
        ) from exc
    except (http.client.IncompleteRead, ValueError) as exc:
        raise ForgetRefused("the gateway returned a malformed credential response") from exc
    except OSError as exc:
        if _socket_unavailable(exc):
            raise ForgetRefused(
                "the gateway's dashboard socket is not reachable from here; delete the "
                "key from the dashboard Memory page instead. Nothing was forgotten."
            ) from exc
        raise ForgetRefused(f"could not reach the gateway: {exc}") from exc
    credential = minted.get("token") if isinstance(minted, dict) else None
    if not isinstance(credential, str) or not credential:
        raise ForgetRefused("the gateway returned an empty local credential")

    body: dict[str, str] = {"expect_value_json": expect_value_json}
    if superseded_by is not None:
        body["superseded_by"] = superseded_by
    route = "/api/memory/semantic/" + urllib.parse.quote(key, safe="")
    request = urllib.request.Request(
        f"{base}{route}?token={urllib.parse.quote(credential, safe='')}",
        method="DELETE",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Session-Key": _DASHBOARD_SESSION_KEY,
        },
    )
    try:
        with unix_socket_urlopen(
            request, timeout=_TIMEOUT_SECS, socket_path=socket_path, verify_peer=verify_peer
        ) as resp:
            result = json.loads(resp.read())
    except ForgetRefused:
        raise
    except urllib.error.HTTPError as exc:
        code, error = _error_code(exc)
        if exc.code == 404:
            raise AlreadyAbsent("already absent") from exc
        detail = _REFUSAL_TEXT.get(code) or error or str(exc.reason)
        raise ForgetRefused(f"{detail}; nothing was forgotten.") from exc
    except (http.client.IncompleteRead, ValueError) as exc:
        raise ForgetRefused(
            "the gateway returned a malformed response; check `kirocrew memory list`"
        ) from exc
    except OSError as exc:
        raise ForgetRefused(
            f"the gateway stopped answering ({exc}); check `kirocrew memory list`"
        ) from exc
    if not (isinstance(result, dict) and result.get("ok") is True):
        raise ForgetRefused("the gateway did not confirm the delete; check `kirocrew memory list`")
