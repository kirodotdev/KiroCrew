"""The gateway half of a Crew-owned Connection: bridge registry, call gate, proxy.

A KAS session reaches a Crew-owned Connection through a stdio bridge
(:mod:`kiro_crew.kas_connection_bridge`) that forwards each MCP message over the
owner-only dashboard Unix socket to :func:`handle`. This module is the gate, not a
pipe (RFC §4.3, the top risk): the agent's own shell runs in the same process
tree as the bridge and can reach the same socket, so the proxy itself

* admits only a registered bridge: the projection minted a per-bridge secret for
  one (session, server) and only its holder is let in (:func:`register_bridge`);
* pins that bridge to the first process that used it; another live pid is refused
  and audited (a pinned pid that has exited may be replaced, for an MCP restart);
* forwards a ``tools/call`` only against an approval Crew's own permission gate
  granted for that session, server, tool and arguments (:func:`note_approval`,
  minted from ``mediated_request_capability.ApprovalMint``), single-use;
* audits every ``tools/call`` (tool name and outcome, never arguments).

The bearer is added here, in the gateway, and is never returned to the bridge.
All state is in this process's memory; a gateway restart ends the KAS sessions
that registered it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The route the bridge posts to (a strict internal path).
PROXY_PATH = "/api/connection-proxy"
BRIDGE_HEADER = "X-Crew-Bridge"
BRIDGE_TOKEN_HEADER = "X-Crew-Bridge-Token"
_MAX_BODY_BYTES = 4 * 1024 * 1024
#: How long an approval waits for its call to reach the proxy.
_APPROVAL_TTL_SECS = 600.0
_MAX_APPROVALS = 512
_MAX_BRIDGES = 4096
_RELAYED_REQUEST_HEADERS = ("Mcp-Session-Id", "MCP-Protocol-Version", "Last-Event-ID")
_RELAYED_RESPONSE_HEADERS = ("Content-Type", "Mcp-Session-Id")
_VENDOR_TIMEOUT_SECS = 600.0
_BRIDGE_ERROR = -32000


@dataclass
class _Bridge:
    session_key: str
    server: str
    slug: str
    mcp_url: str
    token_digest: bytes
    pid: int | None = None
    snapshot: str = ""
    #: The entry's own non-credential headers (a ``User-Agent``), sent upstream.
    extra_headers: dict[str, str] | None = None


@dataclass(frozen=True)
class _Approval:
    session_key: str
    server: str
    tool: str
    args_digest: str
    token: str
    expires_at: float


_lock = threading.Lock()
#: Bounded by :data:`_MAX_BRIDGES`; every other lookup derives from it.
_bridges: dict[str, _Bridge] = {}
_approvals: list[_Approval] = []


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def args_digest(args: Any) -> str | None:
    """A stable digest of one call's arguments, or ``None`` if they do not serialize."""
    try:
        blob = json.dumps(
            args if args is not None else {},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def new_bridge_token() -> str:
    return secrets.token_urlsafe(32)


def register_bridge(
    *,
    version: str,
    token: str,
    session_key: str,
    server: str,
    slug: str,
    mcp_url: str,
    snapshot: str = "",
    headers: Any = None,
) -> bool:
    """Record one projected bridge and the secret it will present (kept as a digest).

    ``False`` when the table is full: a new bridge is refused rather than evicting
    a live chat's. *snapshot* is the launch file name, which the projection's
    holder hands back to :func:`unregister_snapshots` when it shuts down.
    """
    with _lock:
        if len(_bridges) >= _MAX_BRIDGES:
            return False
        pairs = headers.items() if isinstance(headers, dict) else ()
        extra = {k: str(v) for k, v in pairs if isinstance(k, str) and isinstance(v, (str, int))}
        bridge = _Bridge(
            session_key,
            server,
            slug,
            mcp_url,
            _digest(token),
            snapshot=snapshot,
            extra_headers=extra or None,
        )
        _bridges[version] = bridge
        return True


def unregister_snapshots(names: Any) -> None:
    """Forget the bridges whose launch snapshots *names* name (their holder shut down)."""
    wanted = {n for n in names if isinstance(n, str) and n} if names else set()
    if not wanted:
        return
    with _lock:
        for version in [v for v, b in _bridges.items() if b.snapshot in wanted]:
            _bridges.pop(version, None)


def is_connection_server(session_key: str, server: str) -> bool:
    """Whether a registered bridge serves *server* for *session_key*."""
    with _lock:
        return any(b.session_key == session_key and b.server == server for b in _bridges.values())


def drop_slug(slug: str) -> None:
    """Forget every bridge of *slug* (Disconnect); their next call is refused."""
    with _lock:
        for version in [v for v, b in _bridges.items() if b.slug == slug]:
            _bridges.pop(version, None)


# --- approvals --------------------------------------------------------------


def _call_arguments(raw: Any, server: str, tool: str) -> Any:
    """The arguments the bridge will send for this approved call.

    KAS Tool Search invokes a deferred tool as
    ``{"tool_id": "<server>::<tool>", "arguments": {...}}``; the ``tools/call`` the
    bridge forwards carries only the inner ``arguments``. The envelope is unwrapped
    only when its ``tool_id`` names this exact server and tool.
    """
    if not isinstance(raw, dict):
        return raw
    # The same reading the permission gate applies to a deferred call.
    from kiro_crew.agent_sdk.agent_specs import deferred_call_arguments

    inner, readable = deferred_call_arguments(raw, server, tool)
    if not readable or not isinstance(inner, dict):
        # An envelope for some other call: bind its whole shape, which no
        # forwarded ``arguments`` can match.
        return raw
    # KAS transport metadata rides beside the arguments, never inside the
    # ``tools/call`` the bridge forwards.
    return {k: v for k, v in inner.items() if k != "_meta"}


def note_approval(session_key: str, gate_event: Any) -> str:
    """Record that Crew's gate approved this connection tool call; its token or ``""``.

    Only a call whose server identity the transport trusts, on a server this
    session reaches through a Crew-owned bridge, is recorded. Arguments bind the
    approval when the transport vouches for them.
    """
    if gate_event is None or not session_key:
        return ""
    server = getattr(gate_event, "mcp_server_name", None) or ""
    tool = getattr(gate_event, "tool_name", None) or ""
    if not server or not tool or not getattr(gate_event, "mcp_identity_trusted", False):
        return ""
    if not is_connection_server(session_key, server):
        return ""
    raw = _call_arguments(getattr(gate_event, "raw_tool_params", None), server, tool)
    # The approval binds the exact arguments the person saw. When the transport
    # vouches for none (KAS sends no rawInput for an argument-less call), it binds
    # the empty arguments, so it can never authorize a call that carries any.
    trusted = getattr(gate_event, "raw_params_trusted", False) and isinstance(raw, dict)
    digest = args_digest(raw if trusted else {})
    if digest is None:
        return ""
    now = time.monotonic()
    approval = _Approval(
        session_key, server, tool, digest, secrets.token_urlsafe(16), now + _APPROVAL_TTL_SECS
    )
    with _lock:
        _approvals[:] = [a for a in _approvals if a.expires_at > now]
        if len(_approvals) >= _MAX_APPROVALS:
            return ""
        _approvals.append(approval)
    return approval.token


def revoke_approval(token: str) -> None:
    """Drop an approval whose answer never reached the harness."""
    with _lock:
        _approvals[:] = [a for a in _approvals if a.token != token]


def invalidate_session(session_key: str) -> None:
    """A turn cancel: no queued approval of this session survives it."""
    with _lock:
        _approvals[:] = [a for a in _approvals if a.session_key != session_key]


def _take_approval(session_key: str, server: str, tool: str, args: Any) -> _Approval | None:
    """Remove and return one matching approval, or ``None``."""
    digest = args_digest(args)
    now = time.monotonic()
    with _lock:
        for i, a in enumerate(_approvals):
            if (
                a.expires_at > now
                and a.session_key == session_key
                and a.server == server
                and a.tool == tool
                and digest is not None
                and hmac.compare_digest(a.args_digest, digest)
            ):
                return _approvals.pop(i)
    return None


def consume_approval(session_key: str, server: str, tool: str, args: Any) -> bool:
    """Spend one matching approval; ``False`` when Crew's gate approved no such call."""
    return _take_approval(session_key, server, tool, args) is not None


# --- admission --------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    from kiro_crew.platform_compat import pid_exists

    return pid_exists(pid)


def admit_bridge(version: str, token: str, peer_pid: int | None) -> tuple[_Bridge | None, str]:
    """The registered bridge for this request, or ``(None, reason)``."""
    with _lock:
        bridge = _bridges.get(version)
        if (
            bridge is None
            or not token
            or not hmac.compare_digest(bridge.token_digest, _digest(token))
        ):
            return None, "unregistered bridge"
        if peer_pid is None:
            return None, "peer pid unreadable"
        if bridge.pid is None or (bridge.pid != peer_pid and not _pid_alive(bridge.pid)):
            bridge.pid = peer_pid
        elif bridge.pid != peer_pid:
            return None, f"bridge pinned to another live process (peer_pid={peer_pid})"
        return bridge, ""


def _audit(operation: str, outcome: str, caller: str, resources: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # noqa: BLE001 -- auditing never breaks the proxy
        logger.debug("connection proxy audit failed", exc_info=True)


#: The MCP methods forwarded with no approval: session setup and the tool list.
#: ``tools/call`` needs an approval; every other method is refused (deny by default).
_UNGATED_METHODS = frozenset({"initialize", "ping", "tools/list"})


def _method_allowed_ungated(method: str) -> bool:
    return method in _UNGATED_METHODS or method.startswith("notifications/")


def gate_calls(bridge: _Bridge, msg: Any) -> tuple[bool, list[dict[str, Any]]]:
    """``(refused, errors)`` for *msg*, deny by default.

    A response (no ``method``) and :data:`_UNGATED_METHODS` pass. A ``tools/call``
    passes only by spending a matching approval. Any other method (resources,
    prompts, completion…) is refused. Every ``tools/call`` and every refusal is
    audited. *refused* is True when anything was refused; such a message is never
    forwarded, whether or not it carries ids. *errors* answers every request in it
    that has an id (a call without one is a notification and gets no answer).
    """
    items = msg if isinstance(msg, list) else [msg]
    refused: list[dict[str, Any]] = []
    taken: list[_Approval] = []
    for item in items:
        if not isinstance(item, dict) or "method" not in item:
            continue
        method = str(item.get("method") or "")
        if method != "tools/call":
            if not _method_allowed_ungated(method):
                _audit(
                    "connections_proxy_method",
                    "denied",
                    bridge.session_key,
                    f"provider:{bridge.slug} method={method[:64]} reason=not allowed",
                )
                refused.append(item)
            continue
        raw_params = item.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        tool = str(params.get("name") or "")
        approval = _take_approval(bridge.session_key, bridge.server, tool, params.get("arguments"))
        ok = approval is not None
        if approval is not None:
            taken.append(approval)
        _audit(
            "connections_proxy_tool_call",
            "ok" if ok else "denied",
            bridge.session_key,
            f"provider:{bridge.slug} tool={tool[:128]}"
            + ("" if ok else " reason=no matching approval"),
        )
        if not ok:
            refused.append(item)
    if not refused:
        return False, []
    # Nothing in a refused message is forwarded, so no approval it matched is spent.
    with _lock:
        _approvals.extend(taken)
    message = (
        "kirocrew: this Connection request was not approved through Kiro Crew's "
        "permission gate, so it was not sent"
    )
    return True, [
        {"jsonrpc": "2.0", "id": item["id"], "error": {"code": _BRIDGE_ERROR, "message": message}}
        for item in items
        if isinstance(item, dict) and "method" in item and item.get("id") is not None
    ]


# --- the route --------------------------------------------------------------


#: Marks an answer as the proxy's own refusal, not a status relayed from the vendor.
REFUSAL_HEADER = "X-Crew-Proxy-Refusal"


def _refuse(status: int, error: str, code: str) -> Any:
    from aiohttp import web

    return web.json_response(
        {"error": error, "code": code}, status=status, headers={REFUSAL_HEADER: code}
    )


def _refresh_refusal(code: str) -> Any:
    """A failed refresh, worded by what can fix it. *code* is coarse and value-free."""
    if code == "token_unreachable":
        # The stored sign-in may be fine: the provider did not answer.
        return _refuse(
            502,
            "could not refresh the sign-in (the provider was unreachable); try again",
            "refresh_unavailable",
        )
    if code == "client_not_configured":
        return _refuse(
            409,
            "the OAuth app for this Connection is not configured: "
            "set it under Settings → OAuth Apps",
            "client_not_configured",
        )
    # The provider answered and refused: a retry will not help.
    return _refuse(
        502,
        f"the provider rejected the sign-in refresh ({code[:64]}): check the OAuth app "
        "under Settings → OAuth Apps, then reconnect if it persists",
        "refresh_rejected",
    )


async def handle(request: Any) -> Any:
    """``POST|DELETE /api/connection-proxy``: forward one bridge message to the vendor."""
    import aiohttp
    from aiohttp import web

    from kiro_crew.connections import crew_oauth
    from kiro_crew.dashboard.origin import request_is_unix_socket
    from kiro_crew.mcp_gateway.socketsec import get_peer_pid

    if request.get("internal_auth") is not True or not request_is_unix_socket(request):
        _audit("connections_proxy", "denied", "unknown", "not an internal unix-socket caller")
        return _refuse(403, "internal unix-socket caller required", "internal_unix_required")
    try:
        peer_pid = get_peer_pid(request.transport.get_extra_info("socket"))
    except Exception:  # noqa: BLE001
        peer_pid = None
    bridge, reason = admit_bridge(
        request.headers.get(BRIDGE_HEADER, ""),
        request.headers.get(BRIDGE_TOKEN_HEADER, ""),
        peer_pid,
    )
    if bridge is None:
        _audit("connections_proxy", "denied", "unknown", reason)
        return _refuse(
            403,
            "this Connection bridge is not registered for a live session",
            "bridge_unregistered",
        )
    if not await asyncio.to_thread(crew_oauth.manages, bridge.slug):
        return _refuse(
            409, "Crew-owned sign-in is turned off for this Connection", "crew_oauth_off"
        )

    body = b""
    if request.method == "POST":
        if request.content_length is not None and request.content_length > _MAX_BODY_BYTES:
            return _refuse(413, "message too large", "message_too_large")
        # Read to EOF: one read() returns only what has arrived so far.
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                return _refuse(413, "message too large", "message_too_large")
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            msg = json.loads(body)
        except ValueError:
            return _refuse(400, "not JSON", "invalid_json")
        refused, errors = gate_calls(bridge, msg)
        if refused:
            # Never forwarded. A refused notification has no id to answer.
            if not errors:
                return web.Response(status=202)
            return web.json_response(errors if isinstance(msg, list) else errors[0])
        # Forward the message the gate judged, never the raw bytes (duplicate keys).
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")

    # The entry's own headers first, so protocol headers and the bearer win.
    headers = dict(bridge.extra_headers or {})
    headers["Accept"] = "application/json, text/event-stream"
    if body:
        headers["Content-Type"] = "application/json"
    for name in _RELAYED_REQUEST_HEADERS:
        if request.headers.get(name):
            headers[name] = request.headers[name]

    timeout = aiohttp.ClientTimeout(total=None, sock_read=_VENDOR_TIMEOUT_SECS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        upstream = None
        for force in (False, True):
            try:
                token = await crew_oauth.access_token(bridge.slug, force=force)
            except crew_oauth.CrewOAuthError as exc:
                if exc.code == "needs_auth":
                    return _refuse(
                        409, "not connected: reconnect it on the Connections page", "needs_auth"
                    )
                return _refresh_refusal(exc.code)
            try:
                upstream = await session.request(
                    request.method,
                    bridge.mcp_url,
                    data=body or None,
                    headers={**headers, "Authorization": f"Bearer {token}"},
                    allow_redirects=False,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                return _refuse(
                    502,
                    f"the provider could not be reached ({type(exc).__name__})",
                    "provider_unreachable",
                )
            if upstream.status != 401:
                break
            upstream.release()
            upstream = None
        if upstream is None:
            # Never a 401 toward the harness: KAS would start its own sign-in.
            return _refuse(
                409, "the provider refused the stored sign-in: reconnect it", "needs_auth"
            )
        out = web.StreamResponse(status=upstream.status)
        for name in _RELAYED_RESPONSE_HEADERS:
            if upstream.headers.get(name):
                out.headers[name] = upstream.headers[name]
        try:
            await out.prepare(request)
            async for chunk in upstream.content.iter_any():
                await out.write(chunk)
            await out.write_eof()
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
            # Headers are out; the bridge reports the cut-off answer itself.
            logger.debug("connection proxy stream for %s ended early", bridge.slug)
        finally:
            upstream.release()
        return out
