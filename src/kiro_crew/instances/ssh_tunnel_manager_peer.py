"""Requests to a connected crew's gateway over its forward, for
``kiro_crew.instances.ssh_tunnel_manager``.

Resolving the forward a request rides and refusing one that moved under it, proving
the far end is the crew the credential was minted for, exchanging the link token for
a session cookie, and the carriers built on that: the token liveness probe, the
streaming proxy, the session-bundle transfer, capability and version reads, and
federated session search, with their bounded reply readers and typed errors.

Each ``SshTunnelManager`` member here is a module-level function whose first parameter
is ``self``; the class in :mod:`kiro_crew.instances.ssh_tunnel_manager` binds it by
name, so the instance state, every patch of a method and every ``__new__`` build stay
on the one class.

Instance fields written: ``_peer_sessions``.

Instance fields read: ``_chained_far_end``, ``_tokens``, ``_tunnel_epoch``,
``_tunnels``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from kiro_crew.instances.constants import CAPABILITY_REPLY_MAX_BYTES as _CAPABILITY_REPLY_MAX_BYTES
from kiro_crew.instances.constants import (
    DEFAULT_CAPABILITY_PROXY_TIMEOUT_SECS as _CAPABILITY_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS as _MODELS_CAPABILITY_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_PROXY_CONNECT_TIMEOUT_SECS as _PROXY_CONNECT_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS as _PROXY_READ_IDLE_TIMEOUT,
)
from kiro_crew.instances.constants import DEFAULT_SEARCH_PROXY_TIMEOUT_SECS as _SEARCH_PROXY_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS as _TRANSFER_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_TOKEN_PROBE_TIMEOUT_SECS as _TOKEN_PROBE_TIMEOUT
from kiro_crew.instances.constants import SEARCH_REPLY_MAX_BYTES as _SEARCH_REPLY_MAX_BYTES
from kiro_crew.instances.constants import SESSION_IMPORT_MEMORY_WAIT_SECS as _IMPORT_MEMORY_WAIT
from kiro_crew.instances.constants import (
    SESSION_TRANSFER_REPLY_MAX_BYTES as _TRANSFER_REPLY_MAX_BYTES,
)
from kiro_crew.instances.ssh_tunnel_manager_child import TunnelState
from kiro_crew.instances.ssh_tunnel_manager_child_text import _LOOPBACK

if TYPE_CHECKING:
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


#: Refusal codes an OLDER importer answers when a bundle is past the size
#: ceilings it enforces. ``send_session_bundle`` resends such a bundle once
#: without Layer B, which that peer accepts when Layer B was what put it over;
#: a transcript still past its ceilings is refused again, and that answer is
#: returned as it stands.
_PEER_SIZE_REFUSALS = frozenset({"transfer_layer_b_too_large", "transfer_bundle_too_large"})
#: Read size for streaming a serialised bundle up the tunnel.
_UPLOAD_CHUNK_BYTES = 256 * 1024


async def _read_transfer_reply(resp: Any) -> Any:
    """A peer's reply to a session transfer, decoded, or ``{}``.

    Read under :data:`_TRANSFER_REPLY_MAX_BYTES` before anything is decoded: the
    upload has no total timeout, so a peer that keeps sending would otherwise
    have the gateway buffer its reply without end. A reply past the cap, or not
    JSON, reads as ``{}``; the status still decides the outcome.
    """
    chunks: list[bytes] = []
    received = 0
    async for chunk in resp.content.iter_chunked(65536):
        received += len(chunk)
        if received > _TRANSFER_REPLY_MAX_BYTES:
            return {}
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except Exception:
        return {}


async def _upload_chunks(fh: Any, stall: asyncio.Timeout) -> Any:
    """Yield *fh* in chunks for an upload, with a no-progress deadline on *stall*.

    The request has no total timeout, because a bundle has no size ceiling, so a
    peer that stops reading would otherwise hold the upload forever. aiohttp
    pulls the next chunk only once the previous one is written, so each pull is
    progress: every pull moves the deadline ``_TRANSFER_TIMEOUT`` ahead, and a
    peer that stalls lets it lapse, which raises ``TimeoutError`` out of the
    request. When the file is exhausted the deadline is cleared, and waiting for
    the peer's reply falls to the session's per-read timeout.
    """
    loop = asyncio.get_running_loop()
    while True:
        stall.reschedule(loop.time() + _TRANSFER_TIMEOUT)
        chunk = await asyncio.to_thread(fh.read, _UPLOAD_CHUNK_BYTES)
        if not chunk:
            stall.reschedule(None)
            return
        yield chunk


#: The closed set of peer endpoints :meth:`SshTunnelManager.peer_capability` may
#: read. Every one is a GET that reports what the peer gateway CAN do — its
#: version, its agent roster, its model list, its effort levels, its workspaces —
#: and none of them mutates anything. Keeping the set here (rather than letting
#: the caller name a path) is what makes the method a carrier instead of a second
#: proxy: a tainted string cannot reach a peer route that was never listed, and
#: the ``api/agents`` mutating verbs stay unreachable even though the roster read
#: lives under the same prefix. Adding a row is a security decision — it grants
#: the local gateway a new read against every connected peer.
#:
#: ``/api/status`` is read only by ``api_instances_approval_state``, for exactly
#: one field, the peer's live YOLO flag, so a crew window's approval-mode picker
#: shows the mode the peer is really in. That handler forwards nothing else.
_PEER_CAPABILITY_PATHS: frozenset[str] = frozenset(
    {
        "/api/version",
        "/api/agents",
        "/api/models",
        "/api/effort-levels",
        "/api/workspaces",
        "/api/status",
    }
)


async def _read_capability_body(resp: Any) -> tuple[bool, Any]:
    """Read and decode a peer capability reply body under the size cap.

    Returns ``(True, payload)`` when the body is a JSON dict or list, else
    ``(False, {"error", "code"})`` with ``capability_malformed_reply``.
    """
    chunks: list[bytes] = []
    received = 0
    async for chunk in resp.content.iter_chunked(65536):
        received += len(chunk)
        if received > _CAPABILITY_REPLY_MAX_BYTES:
            return False, {
                "error": "peer capability reply exceeds the size cap",
                "code": "capability_malformed_reply",
            }
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except Exception:
        return False, {
            "error": "peer returned a malformed capability reply",
            "code": "capability_malformed_reply",
        }
    if not isinstance(payload, (dict, list)):
        return False, {
            "error": "peer returned a malformed capability reply",
            "code": "capability_malformed_reply",
        }
    return True, payload


class ProxyRequestError(Exception):
    """Typed failure from :meth:`SshTunnelManager.proxy_request`.

    Carries a machine-readable ``code`` (mirrors the ``code`` convention of the
    federated-search errors) and a suggested ``http_status`` so the route
    handler can translate a failure without string-matching the message.
    """

    def __init__(self, code: str, message: str, *, http_status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


# Named by the path callers import it from: a traceback and an error chain render a
# class as ``module.qualname``, so this module's name stays out of that text.
ProxyRequestError.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


class _PeerUnavailable(Exception):
    """A request to a connected peer could not be attempted at all.

    Raised by :meth:`SshTunnelManager._peer_target` and
    :meth:`SshTunnelManager._peer_cookie_header` so the three public callers can
    share how a peer target is *resolved* without sharing their error contracts:
    each maps ``kind`` onto its own machine-readable code. Those codes are
    deliberately NOT derived from ``kind`` here — the ``proxy_``/``transfer_``/
    ``search_`` families belong to three separate route contracts pinned by
    ``test_error_code_contract.py``, and a reader grepping for one of them must
    land on the site that returns it.

    ``kind`` is ``"not_connected"``, ``"no_credential"`` or
    ``"exchange_failed"``; ``message`` is the caller-facing text, identical
    across the three families. The last two share one code per family: both
    mean the manager holds no session it can present to the peer.
    """

    _MESSAGES = {
        "not_connected": "instance is not connected",
        "no_credential": "no live credential for this instance; reconnect it",
        "exchange_failed": "could not exchange the credential with the peer",
    }

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.message = self._MESSAGES[kind]


_PeerUnavailable.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


async def _chained_far_end_holds(self: SshTunnelManager, instance_id: str) -> bool:
    """Whether *instance_id*'s forward still reaches the crew it was built for.

    ``True`` for every forward with no recorded far end: a top-level forward
    rides no hop, and a chained crew that reported no id at build time has
    nothing to compare. For a chained forward with a recorded id, the
    current answer at its local port must equal it exactly, and anything
    else sends no credential. Only a DIFFERENT id retires the forward
    (intent kept, so an ordinary reconnect can rebuild it on a hop that is
    really ours). No answer -- a probe timeout on a slow link, a forward
    that is down -- withholds this one send and leaves the forward to the
    next attempt and to self-heal.

    The probe itself carries no credential, so asking it of a squatter
    discloses nothing. The id is not a secret, though: a local process that
    read it while the crew answered on the hop can replay it after the port
    frees, so this check stops a listener that did not, not one that did.
    """
    expected = self._chained_far_end.get(instance_id, "")
    if not expected:
        return True
    stamp = self._peer_forward_stamp(instance_id)
    port = stamp[0]
    reported = await self._peer_gateway_id(port) if port > 0 else ""
    if port > 0 and self._peer_forward_stamp(instance_id) != stamp:
        # The forward this probe read changed while it ran: it exited, or
        # another took its place. The answer describes neither, and retiring
        # here could tear down a replacement, so the credential is withheld.
        return False
    if reported == expected and self._chained_far_end.get(instance_id) == expected:
        return True
    if not reported:
        logger.info("Holding back a send for %s: far end did not answer", instance_id)
        return False
    reason = (
        "the crew behind this forward no longer answers for the gateway it was "
        "built against, so its credential is withheld. Connect again."
    )
    logger.warning("Holding back a send for %s: far end changed", instance_id)
    self._schedule_chained_retirement(instance_id, reason)
    return False


def _instance_on_port(self: SshTunnelManager, local_port: int) -> str:
    """The id of the live forward listening on *local_port*, or ``""``."""
    for instance_id, tunnel in self._tunnels.items():
        if int(getattr(tunnel.status, "local_port", 0) or 0) == int(local_port):
            return instance_id
    return ""


async def token_validates(self: SshTunnelManager, local_port: int, token: str) -> bool:
    """Probe whether *token* still authenticates against the live tunnel.

    A cheap loopback ``GET http://127.0.0.1:<local_port>/api/status?token=…``
    through the already-open SSH forward — **no SSH spawn**. Lets the API
    layer validate a *stored* token before handing it to the browser on
    (re)connect: a token can go stale while the tunnel stays CONNECTED (a
    failed self-heal re-mint, or a remote ``kirocrew restart`` that
    invalidates tokens), and an iframe loaded with a stale token gets a
    server-rendered 403 page — the SPA never boots, so the reactive
    ``mc-auth-expired`` recovery can't fire. This closes that initial-load
    gap by catching the bad token *before* the iframe loads.

    Returns ``True`` only on a positive ``2xx`` that confirms the token is
    accepted. Returns ``False`` on 401/403, a missing token, an unknown
    port, **and** on any timeout / connection error — an unconfirmed token
    is never treated as valid (authorization must be positively confirmed,
    deny-by-default). The caller recovers by forcing a fresh mint
    (``refresh_token``); a genuinely unreachable link will fail that mint too
    and the caller surfaces a clean error rather than serving a token it
    could not confirm. The token is sent only over loopback→SSH
    (encrypted)→remote loopback and is never logged.
    """
    if not token or local_port <= 0:
        return False
    # The probe sends the token itself, so the far end is checked first.
    owner = self._instance_on_port(local_port)
    if owner and not await self._chained_far_end_holds(owner):
        return False
    url = f"http://{_LOOPBACK}:{int(local_port)}/api/status"
    timeout = aiohttp.ClientTimeout(total=_TOKEN_PROBE_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"token": token}) as resp:
                # Positive confirmation only: 2xx == token accepted.
                return 200 <= resp.status < 300
    except Exception as e:  # timeout, connection refused, etc.
        # Deny-by-default: we could not positively confirm the token.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- the format names a token, its arguments never carry one
        logger.info(
            "Token liveness probe on port %s inconclusive (%s); treating as invalid",
            local_port,
            type(e).__name__,  # never the token
        )
        return False


def _peer_target(self: SshTunnelManager, instance_id: str, path: str) -> tuple[str, str]:
    """Resolve ``(url, cookie_name)`` for one request to a CONNECTED peer.

    Owns the three rules that every peer request shares and that all of them
    depend on for correctness, so they are stated once instead of per caller:

    * the request is only ever attempted against a ``CONNECTED`` tunnel —
      none of these methods opens one, so a disconnected peer is a refusal,
      not a reconnect;
    * the target is always the loopback end of the already-open forward,
      never a peer-supplied host;
    * the cookie name is **port-scoped**. The dashboard keys its cookie on
      the port the CLIENT connected to (``token_auth._cookie_port_from_host``),
      not on the peer's own listen port, so two remotes both serving 7777
      through different forwards do not collide on one cookie. A bare
      ``mc_token`` is never read and would 403 every call.

    Raises :class:`_PeerUnavailable` instead of returning an error, because
    the callers' failure shapes differ (an exception for ``proxy_request``,
    an ``(ok, payload)`` tuple for the other two).
    """
    st = self.status(instance_id)
    if st is None or st.state is not TunnelState.CONNECTED:
        raise _PeerUnavailable("not_connected")
    local_port = st.local_port
    if local_port <= 0:
        raise _PeerUnavailable("no_credential")
    url = f"http://{_LOOPBACK}:{int(local_port)}/{path.lstrip('/')}"
    return url, f"mc_token_{int(local_port)}"


def _peer_forward_stamp(self: SshTunnelManager, instance_id: str) -> tuple[int, int]:
    """The ``(port, generation)`` a peer target was resolved against.

    Captured next to :meth:`_peer_target`, which bakes the port into both the
    url and the cookie name, so the two readings describe one moment.
    """
    st = self.status(instance_id)
    connected = st is not None and st.state is TunnelState.CONNECTED
    port = int(getattr(st, "local_port", 0) or 0) if connected else 0
    return port, self._tunnel_epoch.get(instance_id, 0)


def _require_peer_forward(self: SshTunnelManager, instance_id: str, stamp: tuple[int, int]) -> None:
    """Refuse unless the forward *stamp* names is still the live one.

    Every peer request resolves its url and cookie name from a port and then
    awaits before spending the credential on them. A teardown in that window
    frees the port WITHOUT zeroing it on the status object the tunnel handed
    out, and the allocator gives that exact port to the next connect first,
    so the pre-await url can name another crew's forward by the time the
    request goes out. The generation is compared too, because a peer
    disconnected and reconnected inside the window can land on the same port
    and satisfy the port alone -- the same reading
    :meth:`_remint_parent_under_lock` relies on, and for the same reason: the
    mint budget is tens of seconds and the request path does not hold the
    manager lock.

    Raises :class:`_PeerUnavailable` ``("not_connected")``, so each caller's
    existing mapping for a peer that cannot be reached applies unchanged.
    """
    if self._peer_forward_stamp(instance_id) != stamp:
        raise _PeerUnavailable("not_connected")


async def _peer_headers_for(
    self: SshTunnelManager, instance_id: str, url: str, cookie_name: str, stamp: tuple[int, int]
) -> dict[str, str]:
    """One attempt's credential header, for a target resolved from *stamp*.

    The pair of checks is here rather than at the five call sites so it has one
    definition and cannot be half-applied. Both are needed, because TWO
    credentials can go out per attempt: the link exchange inside
    :meth:`_peer_cookie_header` presents the minted link to *url* itself, and
    the caller then sends the session cookie to that same *url*. A retry
    reaches this method after the previous attempt's request and its re-mint,
    which is the long await the forward can be torn down inside -- so the
    check before the exchange is not redundant with the caller's capture.
    """
    self._require_peer_forward(instance_id, stamp)
    headers = await self._peer_cookie_header(instance_id, url, cookie_name)
    self._require_peer_forward(instance_id, stamp)
    return headers


async def _exchange_link(self: SshTunnelManager, url: str, link: str, cookie_name: str) -> str:
    """Trade a one-time *link* for the peer's session cookie.

    The peer refuses a link presented as a cookie, so the manager does what a
    browser does: one ``GET /api/status?token=`` through the tunnel, keeping
    the ``Set-Cookie`` value. Redirects are not followed and nothing is
    logged.

    Only a peer that answered 401 or 403 (the peer's ``_deny`` answers 403
    to a bad link) returns ``""``: that is the one failure a re-mint fixes,
    so the caller sends no cookie and lets the peer's 401/403 drive it.
    Every other outcome raises :class:`_PeerUnavailable`
    ``("exchange_failed")``: a peer that could not be reached, one that
    accepted the link (2xx) without setting a session cookie, and one that
    answered any other status (a 3xx, another 4xx, a 5xx). A fresh link
    would meet the same wall, and spending the single re-mint on it would
    report a peer error as "peer rejected the credential".
    """
    base = url.split("/", 3)
    status_url = "/".join(base[:3]) + "/api/status"
    # A peer call, not the deny-by-default liveness probe: it waits the
    # proxy connect budget, so a slow but reachable peer is not reported
    # as "exchange_failed" (503 proxy_no_credential).
    timeout = aiohttp.ClientTimeout(total=_PROXY_CONNECT_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                status_url, params={"token": link}, allow_redirects=False
            ) as resp:
                status = resp.status
                morsel = resp.cookies.get(cookie_name)
    except Exception as e:  # timeout, connection refused, etc.
        logger.info("Peer link exchange failed (%s)", type(e).__name__)
        raise _PeerUnavailable("exchange_failed") from None
    if status in (401, 403):
        return ""
    if not 200 <= status < 300 or morsel is None:
        raise _PeerUnavailable("exchange_failed")
    return morsel.value


async def _peer_cookie_header(
    self: SshTunnelManager, instance_id: str, url: str, cookie_name: str
) -> dict[str, str]:
    """Build the ``Cookie`` header carrying this peer's session credential.

    Must be re-read per attempt, not hoisted out of a retry loop: a re-mint
    replaces the credential mid-call and the retry exists to use the fresh
    one. The minted token is a one-time link, so it is exchanged for the
    peer's session cookie first and the result cached against that link.
    An exchange the peer refused sends no cookie; the peer's 401/403 then
    drives the caller's single re-mint. An exchange that could not complete
    raises :class:`_PeerUnavailable` (see :meth:`_exchange_link`).

    **The session never leaves this object.** It travels as a cookie
    rather than a query parameter so it cannot land in the peer's HTTP access
    log, and it is never logged here. The minted link itself reaches the peer
    once, as ``GET /api/status?token=``, the same exchange a browser makes.
    """
    link = self._tokens.get(instance_id, "")
    if not link:
        raise _PeerUnavailable("no_credential")
    # Both the link and the cached session are credentials for the crew this
    # forward was built against; neither goes out unless that crew answers.
    if not await self._chained_far_end_holds(instance_id):
        raise _PeerUnavailable("not_connected")
    cached = self._peer_sessions.get(instance_id)
    if cached is not None and cached[0] == link:
        return {"Cookie": f"{cookie_name}={cached[1]}"}
    session_value = await self._exchange_link(url, link, cookie_name)
    if not session_value:
        return {}
    # A re-mint that landed during the exchange owns the cache: storing this
    # pair would displace the session of the link that replaced it.
    if self._tokens.get(instance_id) == link:
        self._peer_sessions[instance_id] = (link, session_value)
    return {"Cookie": f"{cookie_name}={session_value}"}


async def _prime_peer_session(self: SshTunnelManager, instance_id: str) -> None:
    """Exchange a just-stored link for the peer's session while it is fresh.

    A minted link's click window is minutes, while the proactive refresh
    re-mints hours apart. Exchanging only on the first peer request would
    leave a link that has aged past that window by then, so every site that
    stores a link calls this right away. A failure here changes nothing:
    the request path still exchanges lazily and re-mints on a refusal.
    """
    try:
        url, cookie_name = self._peer_target(instance_id, "api/status")
        # The far-end check inside the header build awaits a probe, so the
        # forward is revalidated around it like every other carrier.
        stamp = self._peer_forward_stamp(instance_id)
        await self._peer_headers_for(instance_id, url, cookie_name, stamp)
    except _PeerUnavailable:
        return


@contextlib.asynccontextmanager
async def proxy_request(
    self: SshTunnelManager,
    instance_id: str,
    method: str,
    path: str,
    *,
    params: "dict[str, str] | None" = None,
    data: bytes | None = None,
    content_type: str = "",
):
    """Open *path* on a connected peer's gateway; yield the live response.

    The generic carrier for remote-crew chat (design: remote-crew-chat).
    Runs entirely over the already-open forward — **no SSH spawn** — and
    follows :meth:`search_sessions_remote`'s credential rules: the token
    never leaves this object, it travels as the port-scoped cookie so it
    cannot land in the peer's access log, and a 401/403 gets exactly one
    transparent re-mint retry.

    Yields the **un-buffered** ``aiohttp.ClientResponse`` so the caller can
    pump a streaming body (a proxied chat turn streams SSE for minutes);
    the response and its session are closed when the context exits. The
    timeout is connect+read-idle rather than total for the same reason: a
    total cap would sever a long turn mid-stream. It is fixed here rather
    than offered as a parameter — the policy is a property of what this
    method is for, not a per-call choice.

    Failures raise :class:`ProxyRequestError` with a machine-readable
    ``code`` and a suggested ``http_status``, so the route handler can
    translate without string-matching.
    """

    def _unavailable(exc: _PeerUnavailable) -> ProxyRequestError:
        if exc.kind == "not_connected":
            return ProxyRequestError("proxy_peer_not_connected", exc.message, http_status=503)
        return ProxyRequestError("proxy_no_credential", exc.message, http_status=503)

    try:
        url, cookie_name = self._peer_target(instance_id, path)
    except _PeerUnavailable as e:
        raise _unavailable(e) from None
    # Captured WITH the url, not per attempt: the url is resolved once here and
    # reused by every retry, so the forward a later attempt must still be
    # talking to is the one THIS reading names. See :meth:`_require_peer_forward`.
    stamp = self._peer_forward_stamp(instance_id)
    tmo = aiohttp.ClientTimeout(
        total=None,
        sock_connect=_PROXY_CONNECT_TIMEOUT,
        sock_read=_PROXY_READ_IDLE_TIMEOUT,
    )
    reminted = False
    while True:
        try:
            headers = await self._peer_headers_for(instance_id, url, cookie_name, stamp)
        except _PeerUnavailable as e:
            raise _unavailable(e) from None
        if content_type:
            headers["Content-Type"] = content_type
        session = aiohttp.ClientSession(timeout=tmo)
        try:
            resp = await session.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                # The tunnel endpoint is the ONLY legitimate target. A
                # compromised peer answering 30x would otherwise make
                # aiohttp fetch an attacker-chosen URL FROM THE HUB (SSRF
                # into its loopback control planes).
                allow_redirects=False,
            )
        except Exception as e:  # timeout, connection refused, etc.
            await session.close()
            logger.info(
                "proxy_request to %s failed before a response (%s)",
                instance_id,
                type(e).__name__,  # never the token or the body
            )
            raise ProxyRequestError(
                "proxy_peer_unreachable",
                f"peer did not answer ({type(e).__name__})",
                http_status=502,
            ) from None
        if resp.status in (401, 403):
            resp.release()
            await session.close()
            # One re-mint, then it is a credential failure — never streamed
            # to the caller as a bare peer 401, which would read as "the
            # chat endpoint said no" instead of "the tunnel credential is
            # not working" and lose the coded error the UI keys off.
            if not reminted and await self.refresh_token(instance_id):
                reminted = True
                continue  # retry once with the fresh credential
            raise ProxyRequestError(
                "proxy_unauthorized", "peer rejected the credential", http_status=502
            )
        try:
            yield resp
        finally:
            resp.release()
            await session.close()
        return


async def send_session_bundle(
    self: SshTunnelManager,
    instance_id: str,
    bundle: dict,
    *,
    serialise: Callable[[dict], Path],
    recheck: Callable[[], dict | None] | None = None,
) -> tuple[bool, dict]:
    """POST a session-transfer *bundle* to a connected instance's importer.

    Each attempt writes the bundle to a file through *serialise* and uploads
    that file, so a large session is never encoded in memory; the file is
    removed after the attempt. The caller supplies the serialiser because
    the bundle's own encoding (a Layer B log carried as a file) belongs to
    the dashboard.

    *recheck*, when given, runs off the loop after each serialisation and
    immediately before the request. It returns ``None`` to proceed or a
    refusal payload (``error`` + ``code``) that is returned unsent: a large
    session serialises for long enough that the caller's own checks on the
    source (its privacy line) can go stale in between.

    Returns ``(ok, payload)``: on success *payload* is the peer's JSON reply
    (carrying the new session key); on failure it carries ``error`` and a
    machine-readable ``code`` so the caller can tell a stale token from an
    unreachable peer from a bundle the peer refused from a peer too old to
    have an importer at all.

    Runs entirely over the already-open forward — **no SSH spawn**, same as
    :meth:`token_validates`.

    **The token never leaves this object** — see
    :meth:`_peer_cookie_header`, which owns that rule for every peer request.
    """
    try:
        url, cookie_name = self._peer_target(instance_id, "/api/chat/slots/import")
    except _PeerUnavailable as e:
        return False, {
            "error": e.message,
            "code": (
                "transfer_peer_not_connected"
                if e.kind == "not_connected"
                else "transfer_no_credential"
            ),
        }
    stamp = self._peer_forward_stamp(instance_id)
    # Per-connect and per-read, never total: a bundle has no size ceiling, so a
    # total budget would fail every transfer that simply takes long to
    # upload. The read budget outlasts the importer's own wait for memory.
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=_TRANSFER_TIMEOUT,
        sock_read=_IMPORT_MEMORY_WAIT + _TRANSFER_TIMEOUT,
    )
    # Two INDEPENDENT one-shot retries, tracked by flag rather than by loop
    # index so neither consumes the other's budget:
    #  * ``reminted`` -- a retained credential can go stale while the tunnel
    #    stays CONNECTED (the condition ``token_validates`` exists for: a
    #    failed self-heal re-mint, or a remote restart that invalidates
    #    credentials). One fresh mint turns that into a transparent success.
    #  * ``downgraded`` -- an older peer refuses bundle_version 2; resend the
    #    transcript-only v1 shape it has always accepted.
    #  * ``trimmed`` -- an older peer enforces a size ceiling this side does
    #    not, and refuses a bundle whose Layer B is past it; resend without
    #    Layer B, the transcript-only copy that peer accepts.
    # Bounded at 4 attempts so at most one of each can fire plus the original.
    reminted = False
    downgraded = False
    trimmed = False
    for _attempt in range(4):
        try:
            headers = await self._peer_headers_for(instance_id, url, cookie_name, stamp)
        except _PeerUnavailable as e:
            return False, {"error": e.message, "code": "transfer_no_credential"}
        body_path: Path | None = None
        body_file: Any = None
        # No deadline until the upload starts; see ``_upload_chunks``.
        stall = asyncio.timeout(None)
        try:
            body_path = await asyncio.to_thread(serialise, bundle)
            body_file = await asyncio.to_thread(open, body_path, "rb")
            # Serialising a large session is the longest await before the
            # request, so the forward is re-checked after it: a tunnel
            # replaced meanwhile can hand this URL's port to another peer,
            # which must not receive this session or its credential.
            self._require_peer_forward(instance_id, stamp)
            if recheck is not None:
                refusal = await asyncio.to_thread(recheck)
                if refusal is not None:
                    return False, refusal
            async with stall, aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    data=_upload_chunks(body_file, stall),
                    headers={**headers, "Content-Type": "application/json"},
                ) as resp:
                    payload = await _read_transfer_reply(resp)
                    if 200 <= resp.status < 300:
                        return True, payload if isinstance(payload, dict) else {}
                    if resp.status in (401, 403):
                        if not reminted and await self.refresh_token(instance_id):
                            reminted = True
                            continue  # retry once with the fresh credential
                        return False, {
                            "error": "peer rejected the credential",
                            "code": "transfer_unauthorized",
                        }
                    if resp.status in (404, 405):
                        # A peer with no importer route cannot receive a
                        # session at all, and says so in two different ways
                        # depending on its routing table: 404 when nothing
                        # matches, 405 when the path falls through to
                        # ``/api/chat/slots/{slot}`` (registered GET/DELETE
                        # only) and aiohttp reports the method instead.
                        # Neither is a status the importer itself ever
                        # returns, so both mean the same actionable thing —
                        # surface that rather than a bare status code the
                        # user cannot act on.
                        return False, {
                            "error": (
                                "instance is running an older Kiro Crew that cannot "
                                "receive sessions — update it, then reconnect"
                            ),
                            "code": "transfer_peer_too_old",
                        }
                    # Forward the peer's own code when it sent one: a version
                    # mismatch or an oversized bundle is actionable, and
                    # rewriting it here would erase that.
                    code = payload.get("code") if isinstance(payload, dict) else None
                    # An OLDER peer refuses bundle_version 2 outright, even
                    # though its Layer B is purely additive. Downgrade once
                    # and resend the transcript-only v1 shape that peer has
                    # always handled: without this, gaining Layer B would
                    # REMOVE the ability to send to a peer that has not been
                    # upgraded yet.
                    #
                    # Gated on the VERSION, not on Layer B presence: a
                    # context-free session ships a v2 bundle with NO
                    # ``layer_b`` key at all, and a presence check would skip
                    # the downgrade for exactly those transfers and fail them
                    # against a v1 peer. Dropping ``layer_b`` below stays
                    # unconditional because it is simply absent in that case.
                    if (
                        code == "transfer_version_unsupported"
                        and not downgraded
                        and bundle.get("bundle_version") == 2
                    ):
                        downgraded = True
                        bundle = {k: v for k, v in bundle.items() if k != "layer_b"}
                        bundle["bundle_version"] = 1
                        logger.info(
                            "Session transfer to %s: peer refused v2; "
                            "retrying transcript-only at v1",
                            instance_id,
                        )
                        continue
                    # An OLDER peer enforces size ceilings this side does
                    # not. Layer B is the part that grows past them, and that
                    # peer accepts the session without it, so resend that
                    # copy rather than fail the transfer. The peer then
                    # answers ``prefix`` and the row reads "Sent (transcript
                    # only)", which is true.
                    if code in _PEER_SIZE_REFUSALS and not trimmed and "layer_b" in bundle:
                        trimmed = True
                        bundle = {k: v for k, v in bundle.items() if k != "layer_b"}
                        logger.info(
                            "Session transfer to %s: peer refused the size (%s); "
                            "retrying without Layer B",
                            instance_id,
                            code,
                        )
                        continue
                    return False, {
                        "error": (
                            payload.get("error")
                            if isinstance(payload, dict) and payload.get("error")
                            else f"peer refused the transfer (HTTP {resp.status})"
                        ),
                        "code": code or "transfer_peer_refused",
                    }
        except _PeerUnavailable as e:
            # The forward changed under the serialisation; nothing was sent.
            return False, {"error": e.message, "code": "transfer_peer_not_connected"}
        except Exception as e:
            logger.info(
                "Session transfer to %s failed (%s)",
                instance_id,
                type(e).__name__,  # never the credential, never the bundle
            )
            return False, {
                "error": f"could not reach the instance ({type(e).__name__})",
                "code": "transfer_unreachable",
            }
        finally:
            # Each attempt serialises its own body, and a retry changes the
            # bundle, so the file is this attempt's alone to remove. A
            # failure here is logged, never raised: it would replace the
            # attempt's own answer, and after a committed import that turns
            # success into an error whose retry duplicates the session.
            try:
                if body_file is not None:
                    await asyncio.to_thread(body_file.close)
                if body_path is not None:
                    await asyncio.to_thread(body_path.unlink, missing_ok=True)
            except OSError as e:
                logger.warning(
                    "Session transfer to %s: could not remove the staged body (%s)",
                    instance_id,
                    type(e).__name__,
                )
    # Both attempts came back unauthorized.
    return False, {
        "error": "peer rejected the credential",
        "code": "transfer_unauthorized",
    }


async def peer_capability(self: SshTunnelManager, instance_id: str, path: str) -> tuple[bool, Any]:
    """GET one of a connected peer's read-only capability endpoints.

    This is deliberately a NARROW CARRIER, not a general proxy. The generic
    ``/api/instances/{id}/proxy/*`` route forwards a caller-supplied path and
    is therefore fenced to the ``api/chat`` / ``api/stream`` prefixes; the
    paths a local session needs in order to render a peer-bound header
    (version, agent roster, model list, effort levels, workspaces, status) sit
    outside those prefixes. Widening the prefix list would have granted the
    whole ``api/agents`` surface — including its mutating ``PUT`` — so the
    capability read gets its own carrier whose target is chosen from a fixed
    set here rather than by the caller.

    Returns ``(ok, payload)``. On success *payload* is the peer's decoded
    JSON, which may be a dict (version, workspaces) or a list (agents,
    models, effort levels) — both shapes are real and returned as-is. On
    failure *payload* is ``{"error", "code"}`` so a caller can tell a stale
    credential from a peer too old to answer.
    """
    if path not in _PEER_CAPABILITY_PATHS:
        # A programming error, not a runtime condition: the path set is
        # closed and every caller passes a literal from it.
        raise ValueError(f"not a peer capability path: {path!r}")
    try:
        url, cookie_name = self._peer_target(instance_id, path)
    except _PeerUnavailable as e:
        return False, {
            "error": e.message,
            "code": (
                "capability_peer_not_connected"
                if e.kind == "not_connected"
                else "capability_no_credential"
            ),
        }
    stamp = self._peer_forward_stamp(instance_id)
    # /api/models is the one read whose cold path runs bounded work on the
    # peer (up to 5s sandbox-backend detection + up to 10s `kiro-cli chat
    # --list-models` + up to 3s entitlement revalidation, ~18s worst case),
    # so it gets its own budget; the cheap reads keep the short one.
    # See both constants for sizing.
    total = _MODELS_CAPABILITY_PROXY_TIMEOUT if path == "/api/models" else _CAPABILITY_PROXY_TIMEOUT
    timeout = aiohttp.ClientTimeout(total=total)
    reminted = False
    for _attempt in range(2):
        try:
            headers = await self._peer_headers_for(instance_id, url, cookie_name, stamp)
        except _PeerUnavailable as e:
            return False, {"error": e.message, "code": "capability_no_credential"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers=headers,
                    # Same SSRF reasoning as the search carrier: the tunnel
                    # endpoint is the only legitimate target, so a peer
                    # answering 30x must not redirect the hub anywhere.
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (401, 403):
                        if not reminted and await self.refresh_token(instance_id):
                            reminted = True
                            continue  # retry once with the fresh credential
                        return False, {
                            "error": "peer rejected the credential",
                            "code": "capability_unauthorized",
                        }
                    if resp.status in (404, 405):
                        # The peer predates this endpoint. Reported as its own
                        # code because it is the actionable case (update the
                        # peer), not a transport fault to retry.
                        return False, {
                            "error": f"peer does not serve {path}",
                            "code": "capability_peer_too_old",
                        }
                    if resp.status == 503 and path == "/api/models":
                        # A healthy peer answers its models read with a
                        # deliberate 503 while it revalidates the list. The
                        # models read is the only path that answers this
                        # deliberate 503, so only it gets the transient code
                        # the caller re-polls through; any other 503 is a
                        # refusal.
                        body_ok, body = await _read_capability_body(resp)
                        if (
                            body_ok
                            and isinstance(body, dict)
                            and body.get("code") == "model_list_revalidating"
                        ):
                            return False, {
                                "error": "peer is revalidating its model list",
                                "code": "capability_peer_revalidating",
                            }
                    if not 200 <= resp.status < 300:
                        return False, {
                            "error": f"peer refused the read (HTTP {resp.status})",
                            "code": "capability_peer_refused",
                        }
                    return await _read_capability_body(resp)
        except Exception as e:
            logger.info(
                "Peer capability read %s from %s failed (%s)",
                path,
                instance_id,
                type(e).__name__,  # never the credential
            )
            return False, {
                "error": f"could not reach the instance ({type(e).__name__})",
                "code": "capability_unreachable",
            }
    # Both attempts came back unauthorized.
    return False, {
        "error": "peer rejected the credential",
        "code": "capability_unauthorized",
    }


async def peer_version(self: SshTunnelManager, instance_id: str) -> tuple[bool, str]:
    """The peer gateway's ``kiro_crew.__version__``, or ``(False, code)``.

    Used by the version-equality gate that fences remote execution. A peer
    without ``/api/version`` answers 404 and comes back as
    ``capability_peer_too_old`` — which the gate must treat exactly like a
    mismatch, since an unknown version cannot be proven equal.
    """
    ok, payload = await self.peer_capability(instance_id, "/api/version")
    if not ok:
        code = payload.get("code", "capability_unreachable") if isinstance(payload, dict) else ""
        return False, str(code)
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version:
        return False, "capability_malformed_reply"
    return True, version


async def search_sessions_remote(
    self: SshTunnelManager, instance_id: str, query: str, limit: int
) -> tuple[bool, dict]:
    """GET a connected peer's ``/api/sessions/search`` over its tunnel.

    Returns ``(ok, payload)``: on success *payload* is the peer's JSON reply
    (``{"sessions": [...]}``); on failure it carries ``error`` and a
    machine-readable ``code`` so the aggregator can tell a stale credential
    from an unreachable peer.

    Runs entirely over the already-open forward — **no SSH spawn** — and
    follows the shared credential rules in :meth:`_peer_cookie_header`: the
    token never leaves this object and travels as the port-scoped cookie. A
    401/403 gets exactly one transparent re-mint retry — a retained
    credential can go stale while the tunnel stays CONNECTED.
    """
    try:
        url, cookie_name = self._peer_target(instance_id, "/api/sessions/search")
    except _PeerUnavailable as e:
        return False, {
            "error": e.message,
            "code": (
                "search_peer_not_connected" if e.kind == "not_connected" else "search_no_credential"
            ),
        }
    stamp = self._peer_forward_stamp(instance_id)
    timeout = aiohttp.ClientTimeout(total=_SEARCH_PROXY_TIMEOUT)
    reminted = False
    for _attempt in range(2):
        try:
            headers = await self._peer_headers_for(instance_id, url, cookie_name, stamp)
        except _PeerUnavailable as e:
            return False, {"error": e.message, "code": "search_no_credential"}
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    params={"q": query, "limit": str(int(limit))},
                    headers=headers,
                    # The tunnel endpoint is the ONLY legitimate target. A
                    # compromised peer answering 30x would otherwise make
                    # aiohttp fetch an attacker-chosen URL FROM THE HUB
                    # (SSRF into its loopback control planes).
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (401, 403):
                        if not reminted and await self.refresh_token(instance_id):
                            reminted = True
                            continue  # retry once with the fresh credential
                        return False, {
                            "error": "peer rejected the credential",
                            "code": "search_unauthorized",
                        }
                    if not 200 <= resp.status < 300:
                        return False, {
                            "error": f"peer refused the search (HTTP {resp.status})",
                            "code": "search_peer_refused",
                        }
                    # Byte-cap BEFORE decoding: resp.json() buffers the whole
                    # body first, so a hostile/broken peer streaming an
                    # unbounded reply could exhaust hub memory before any
                    # per-field clamp runs. StreamReader.read(n) returns as
                    # soon as ANY buffered data exists, so a single call can
                    # yield a prefix of a multi-chunk reply — accumulate to
                    # EOF, refusing the moment the cap is crossed. An honest
                    # reply (<=200 clamped rows) sits far below the cap.
                    chunks: list[bytes] = []
                    received = 0
                    oversized = False
                    async for chunk in resp.content.iter_chunked(65536):
                        received += len(chunk)
                        if received > _SEARCH_REPLY_MAX_BYTES:
                            oversized = True
                            break
                        chunks.append(chunk)
                    if oversized:
                        return False, {
                            "error": "peer search reply exceeds the size cap",
                            "code": "search_malformed_reply",
                        }
                    try:
                        payload = json.loads(b"".join(chunks))
                    except Exception:
                        payload = None
                    if not isinstance(payload, dict):
                        return False, {
                            "error": "peer returned a malformed search reply",
                            "code": "search_malformed_reply",
                        }
                    return True, payload
        except Exception as e:
            logger.info(
                "Federated session search to %s failed (%s)",
                instance_id,
                type(e).__name__,  # never the credential, never the query
            )
            return False, {
                "error": f"could not reach the instance ({type(e).__name__})",
                "code": "search_unreachable",
            }
    # Both attempts came back unauthorized.
    return False, {
        "error": "peer rejected the credential",
        "code": "search_unauthorized",
    }
