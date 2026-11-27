"""A pod's bundled KAS engine borrows the host's Kiro Crew sign-in.

The ``pod_host_sign_in`` feature preview: off by default, and always off in an
LTS build (``edition_locks.preview_on``). It covers the bundle route only.

* **Pod side** (this module). A pod's gateway is told the host's dashboard unix
  socket in :data:`SOCKET_ENV`. When its KAS engine runs on the bundle route and
  asks for a token while the pod's own vault holds no sign-in, the gateway asks
  the host over that socket (:func:`fetch_answer`) and nowhere else: there is no
  TCP path, and the listener must be this same user. It also asks whether a
  token it was served has been refused (:func:`ask_refused`).
* **Host side** (``dashboard.handlers.pod_host_sign_in``). The host answers only
  on its own dashboard socket, and only when the connecting process is the named
  pod's running gateway itself: its kernel-reported pid is the pod unit's main
  process, it runs as this user, and it shares the host gateway's namespaces, so
  a sandboxed shell inside the pod is refused. Each request re-checks that, the
  owner's grant to an enabled pod app and the preview. The answer comes from the
  host's own Crew vault only (never a workload identity, kiro-cli's sign-in or an
  API key) and carries :data:`ANSWER_KEYS` alone: the access token, its expiry and
  the account metadata sent with it, never a refresh token or client secret.
  Renewal stays on the host, under its vault's refresh lock.

No secret is written into the pod: the pod is recognised by its process, so
``pod down`` (which stops the unit) ends its access with nothing to revoke.

Not covered: the relay route. A pod whose KAS runs through kiro-cli keeps signing
in from the identity store ``pod.runtime_home._stage_runtime_auth_store`` copies
in, exactly as before, whether or not this preview is on.

Stdlib only at import: the pod side is read on the KAS spawn path, which must not
load ``cryptography`` (see :mod:`kiro_crew.acp.kas_host_auth`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The ``dashboard.feature_previews`` name, and ``mc-preview-pod-host-sign-in`` in
#: ``website/src/utils/previewFlags.ts``.
PREVIEW_NAME = "pod_host_sign_in"

#: The host routes a pod's gateway asks. POST only, on the host's dashboard socket.
TOKEN_ROUTE = "/api/pods/host-sign-in/token"
REFUSED_ROUTE = "/api/pods/host-sign-in/refused"

#: The routes' namespace for :func:`kiro_crew.apps.core_route_grants.core_route_grant`.
GRANT_NAMESPACE = "/api/pods/host-sign-in"

#: The pod gateway's environment variable naming the host's dashboard socket.
SOCKET_ENV = "KIROCREW_POD_HOST_SIGN_IN_SOCKET"

#: The only keys an answer may carry, on both sides of the socket: the access token,
#: its expiry, and the account metadata the engine sends with it (KAS refuses a
#: request with no ``profileArn``). No refresh token, client secret or other field
#: is among them, so no change to the answer's builder widens what leaves.
ANSWER_KEYS = ("accessToken", "expiresAt", "profileArn", "provider", "authMethod")

#: The keys an answer must carry to be used.
REQUIRED_KEYS = ("accessToken", "expiresAt")

#: The pod-side message when the host holds no Crew sign-in. The engine shows it
#: in its sign-in prompt, so it names where to sign in.
HOST_NOT_SIGNED_IN = "the host is not signed in to Kiro Crew; run `kirocrew kas login` on the host"

#: Seconds the pod waits for the host, which may refresh first under its own
#: retry window (``kas_host_auth.REFRESH_RETRY_WINDOW_SECS``).
REQUEST_TIMEOUT_SECS = 20.0

_ANSWER_MAX_BYTES = 64 * 1024
_SOCKET_NAME_RE = re.compile(r"dashboard-(\d{1,5})\.sock\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class HostSignInError(Exception):
    """The host did not answer with a token. ``str(exc)`` is token-free."""


def preview_on(cfg: Any = None) -> bool:
    """Whether the preview is on in *cfg* (``None``: the live or on-disk config).

    Read through ``edition_locks``, so an LTS build reads it off. Never raises.
    """
    from kiro_crew.edition_locks import preview_on as _preview_on

    return _preview_on(PREVIEW_NAME, cfg)


def live_preview_on() -> bool:
    """:func:`preview_on` from the live snapshot only: off until one is loaded.

    For the request path, which runs on the event loop and never reads the file.
    """
    try:
        from kiro_crew.config import live

        snap = live.snapshot()
    except Exception:  # noqa: BLE001 -- unread means off
        return False
    return snap is not None and preview_on(snap)


def filter_answer(answer: Mapping[str, Any]) -> dict[str, str]:
    """*answer* cut to :data:`ANSWER_KEYS`, non-empty strings only."""
    return {k: v for k in ANSWER_KEYS if isinstance(v := answer.get(k), str) and v}


def is_digest(value: Any) -> bool:
    """Whether *value* is a ``kas_host_auth.token_digest`` (lower-case SHA-256 hex)."""
    return isinstance(value, str) and _DIGEST_RE.match(value) is not None


# --------------------------------------------------------------------------- #
# Pod side.
# --------------------------------------------------------------------------- #


def _socket() -> tuple[str, int] | None:
    """The host socket this pod gateway was given, and its port, or ``None``."""
    if os.environ.get("KIROCREW_POD") != "1":
        return None
    path = (os.environ.get(SOCKET_ENV) or "").strip()
    # A dashboard socket's own name only: the port is read from it for the Host
    # header, and anything else is not the host's dashboard socket.
    match = _SOCKET_NAME_RE.search(path) if os.path.isabs(path) else None
    if match is None:
        return None
    return path, int(match.group(1))


def host_socket_for_pod(live_port: int) -> str | None:
    """The host gateway's dashboard socket to give a pod, or ``None``. Host side.

    ``None`` while this host's preview is off, in a process that is itself a
    pod (a pod lends nothing on), while no enabled app holds the routes' grant,
    or when no host dashboard socket exists. The
    configured live port is tried first, then the ports of this data home's
    gateway run markers. Blocking (config and file reads). Never raises.

    shortcut: read once when the pod's environment is built, so a host gateway
    that moves to a new port is reached after the pod restarts; upgrade by having
    the pod re-read the host's run markers on a failed send.
    """
    try:
        if os.environ.get("KIROCREW_POD") == "1" or not preview_on():
            return None
        from kiro_crew.apps.core_route_grants import core_route_grant

        # No app holds the routes' grant: the host would refuse every request, so
        # the pod is not moved onto the bundle route and signs in by itself.
        if not core_route_grant(TOKEN_ROUTE, GRANT_NAMESPACE).holder:
            return None
        from kiro_crew.dashboard.urls import dashboard_socket_path
        from kiro_crew.instances import run_marker

        for port in (live_port, *(p for p in run_marker.marker_ports() if p != live_port)):
            path = dashboard_socket_path(port)
            if path.is_socket():
                return str(path)
    except Exception:  # noqa: BLE001 -- the pod then signs in by itself
        logger.debug("pod host sign-in: no host socket for the pod", exc_info=True)
    return None


def apply_to_pod_env(env: dict[str, str], live_port: int) -> None:
    """Set or drop :data:`SOCKET_ENV` in a pod's environment. Host side.

    Always drops an inherited value first, so a pod is given the socket only
    by this host, and only while :func:`host_socket_for_pod` names one.
    """
    env.pop(SOCKET_ENV, None)
    socket_path = host_socket_for_pod(live_port)
    if socket_path:
        env[SOCKET_ENV] = socket_path


def active() -> bool:
    """Whether this process is a pod's gateway that was given the host's socket.

    A presence check, like ``workload_identity.active``: a host that does not
    answer still counts, so a bundle spawn stays Crew-owned and its first token
    request names the cause. Never raises.
    """
    try:
        return _socket() is not None
    except Exception:  # noqa: BLE001
        return False


def catalog_identity() -> str:
    """The borrowed sign-in's model-catalog key, or ``""`` when not active.

    shortcut: the host socket is the key, so a host account switch keeps the
    cached catalog until the pod restarts; upgrade by having the host answer
    its vault fingerprint with the token.
    """
    target = _socket() if active() else None
    if target is None:
        return ""
    return "pod-host-sign-in:" + hashlib.sha256(os.fsencode(target[0])).hexdigest()


def ready() -> bool:
    """Whether the host lends a token now. Blocking IO. Never raises."""
    try:
        fetch_answer()
    except Exception:  # noqa: BLE001 -- an answer that does not come is not ready
        return False
    return True


def _pod_name() -> str:
    """This pod's name: its data home is ``<pod root>/<name>``."""
    from kiro_crew.config.paths import data_home

    return data_home().name


def _verify_host_peer(sock: Any) -> None:
    """Refuse a listener that is not this user: the request goes to no one else."""
    from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self

    if check_peer_is_self(sock) is not PeerCredResult.MATCH:
        # Not an OSError, so urllib does not fold it into a URLError.
        raise HostSignInError("host sign-in socket is not this user's")


def _post(route: str, body: dict[str, Any]) -> tuple[int, Any]:
    """POST *body* to the host over its socket only. Blocking IO."""
    import urllib.error
    import urllib.request

    from kiro_crew.loopback_http import unix_socket_urlopen

    target = _socket()
    if target is None:
        raise HostSignInError("host sign-in unavailable")
    path, port = target
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with unix_socket_urlopen(  # nosemgrep -- unix socket only, no TCP handler
            request,
            timeout=REQUEST_TIMEOUT_SECS,
            socket_path=path,
            verify_peer=_verify_host_peer,
        ) as response:
            status, raw = response.status, response.read(_ANSWER_MAX_BYTES)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(_ANSWER_MAX_BYTES)
    except HostSignInError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the type only: no URL, no body
        logger.info("host sign-in: the host did not answer (%s)", type(exc).__name__)
        raise HostSignInError("host sign-in unavailable") from exc
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return status, None


def fetch_answer() -> dict[str, str]:
    """Ask the host for an access token. Blocking IO -- call off the loop.

    Returns the answer cut to :data:`ANSWER_KEYS`. Raises :class:`HostSignInError`
    with a token-free reason the engine can show.
    """
    status, body = _post(TOKEN_ROUTE, {"pod": _pod_name()})
    if status == 200 and isinstance(body, dict):
        answer = filter_answer(body)
        if all(k in answer for k in REQUIRED_KEYS):
            return answer
        raise HostSignInError("host sign-in answer malformed")
    code = body.get("code") if isinstance(body, dict) else None
    if code == "host_not_signed_in":
        raise HostSignInError(HOST_NOT_SIGNED_IN)
    raise HostSignInError(
        f"host sign-in refused ({code})"
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code)
        else "host sign-in refused"
    )


def ask_refused(served_digest: str | None) -> bool:
    """Whether the host says its sign-in is refused, renewing the served token.

    The pod's half of ``kas_host_auth.sign_in_refused``. True only on the
    host's word; every failure is False, so a start keeps its retryable error.
    Blocking IO. Never raises.
    """
    try:
        body: dict[str, Any] = {"pod": _pod_name()}
        if is_digest(served_digest):
            body["servedDigest"] = served_digest
        status, answer = _post(REFUSED_ROUTE, body)
    except Exception:  # noqa: BLE001
        return False
    return status == 200 and isinstance(answer, dict) and answer.get("refused") is True
