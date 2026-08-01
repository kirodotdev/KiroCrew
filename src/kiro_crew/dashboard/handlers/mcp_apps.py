"""MCP Apps handlers — the UI→gateway tool-callback relay (SEP-1865).

``POST /api/mcp-apps/call`` lets an embedded MCP App iframe invoke one of its
server's *app-visible* tools. The dashboard is a thin relay: it validates the
request shape, performs a cheap local capability pre-check (the spool record
must exist), and forwards a one-shot ``app-call`` control frame over the
gateway's uid-gated unix socket. ALL authorization decisions — spool-token
re-verification, backend routing, ``_meta.ui.visibility`` enforcement, SEL
auditing — happen gateway-side in :mod:`kiro_crew.mcp_gateway.app_call`, so a
bug here cannot grant an app a tool the gateway would refuse.

Body: ``{"spool_id": str, "tool": str, "arguments": {..}}``.
Replies: 200 ``{"result": ...}`` / ``{"error": ...}`` (backend JSON-RPC error),
403 for gateway policy rejections, 404 for unknown/expired spool ids,
503 when gatewayd is unreachable, 504 on timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from kiro_crew import security
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.handlers._shared import _is_restricted_session, _read_session_key
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.mcp_apps_render import load_spool
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.rewriter import default_socket_path
from kiro_crew.sel import SecurityEventLog

logger = logging.getLogger(__name__)


def _redact_result(obj):
    """Recursively credential/exfil-URL redact string leaves of a gateway
    result before it is returned to the server-authored iframe (which can reach
    its declared CSP origins). Same discipline as the render-side redaction."""
    if isinstance(obj, str):
        return security.redact(obj)
    if isinstance(obj, list):
        return [_redact_result(x) for x in obj]
    if isinstance(obj, dict):
        # Redact string KEYS too (a credential can appear as a key).
        return {
            (security.redact(k) if isinstance(k, str) else k): _redact_result(v)
            for k, v in obj.items()
        }
    return obj


#: Overall budget for the gateway round-trip. Must exceed the gateway's own
#: combined inner budgets (10s tools/list + 20s tools/call) with real margin
#: for scheduling/socket/validation/governance overhead — an outer budget
#: equal to the inner sum turns legitimate near-limit calls into 504s.
_GATEWAY_TIMEOUT_SECS = 45.0

#: Cap for one reply line from the gateway. Aligned with the gateway's own
#: configurable frame limit (64 MiB) plus wrapper margin — a smaller relay cap
#: turned a completed call carrying large structured/non-text content into a
#: false 503 even though the gateway would have delivered it.
_MAX_REPLY_BYTES = 64 * 1024 * 1024 + 64 * 1024


def _socket_path() -> str:
    """The gateway socket, honoring the ``mcp_gateway.socket_path`` config
    override (empty -> the standard ``$KIROCREW_HOME`` location)."""
    try:
        # circular import: config.loader pulls dashboard modules; keep lazy.
        from kiro_crew.config.loader import KiroCrewConfig

        configured = (KiroCrewConfig.load().mcp_gateway.socket_path or "").strip()
        if configured:
            return configured
    except Exception:  # pragma: no cover — config load must not break the relay
        logger.debug("config load failed; using default gateway socket", exc_info=True)
    return str(default_socket_path())


async def _gateway_app_call(frame: dict) -> dict:
    """Send one ``app-call`` frame to gatewayd; return the reply frame."""
    # limit: asyncio's default StreamReader limit is 64 KiB — readline() on a
    # legitimate reply above that raises instead of relaying. Size the buffer
    # to our own reply cap (+1 so a cap-sized line still parses).
    reader, writer = await transport.connect(
        _socket_path(), limit=_MAX_REPLY_BYTES + 1
    )
    try:
        writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        if not line or len(line) > _MAX_REPLY_BYTES:
            raise ConnectionError("gateway closed without a reply")
        return json.loads(line.decode("utf-8"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def api_mcp_apps_call(request: web.Request) -> web.Response:
    """POST /api/mcp-apps/call — relay an app's tools/call to the gateway."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "request body must be a JSON object"}, status=400)

    spool_id = body.get("spool_id")
    tool = body.get("tool")
    arguments = body.get("arguments", {})
    # #418 callback capability: delivered to the iframe ONLY over the owner-WS
    # render frame; relayed back here in the (owner-authenticated, non-model-
    # visible) POST body and forwarded to the gateway, which authorizes on it.
    callback_secret = body.get("callback_secret")
    if not isinstance(spool_id, str) or not spool_id:
        return web.json_response({"error": "missing spool_id"}, status=400)
    if not isinstance(tool, str) or not tool:
        return web.json_response({"error": "missing tool"}, status=400)
    if not isinstance(arguments, dict):
        return web.json_response({"error": "arguments must be an object"}, status=400)

    # AUTHENTICATED owner gate (server-side, load-bearing): the caller's
    # identity comes from the token-auth middleware (`request["user"]` /
    # `request["app"]`), not from any client-set header. App tokens and
    # non-owner subjects are refused — an embedded app's capability must only
    # ever be exercised by the owner's own dashboard.
    if not is_owner_dashboard_request(request):
        try:
            SecurityEventLog().log_api_access(
                caller=str(request.get("user") or "unknown"),
                operation="mcp-apps.call", outcome="denied",
                source="dashboard", resources="non_owner_block",
            )
        except Exception:  # pragma: no cover
            logger.debug("SEL audit for non-owner mcp-apps call failed", exc_info=True)
        return web.json_response({"error": "owner authorization required"}, status=403)

    # Ephemeral gate: incognito/guest sessions must not be able to drive
    # tool execution through a (leaked or shoulder-surfed) spool id — the
    # same server-side gate memory operations use.
    if _is_restricted_session(request.app["state"], request):
        try:
            SecurityEventLog().log_api_access(
                caller=_read_session_key(request) or "unknown",
                operation="mcp-apps.call", outcome="denied",
                source="dashboard", resources="restricted_session_block",
            )
        except Exception:  # pragma: no cover — audit must not break the deny
            logger.debug("SEL audit for restricted mcp-apps call failed", exc_info=True)
        return web.json_response({"error": "not available in this session"}, status=403)

    # Capability pre-check + session-ownership binding. The record names the
    # session that produced the app; a caller relaying on behalf of a
    # DIFFERENT session (leaked spool id pasted elsewhere) is refused. The
    # X-Session-Key header is the same honest-client channel the ephemeral
    # gate reads; the gateway independently re-verifies the capability token
    # itself. Records without a session binding fall through (the gateway's
    # ladder still applies). to_thread: records can be multi-MB and this runs
    # on the dashboard event loop.
    record = await asyncio.to_thread(load_spool, spool_id)
    if record is None:
        # Audit the capability denial like every other denied path (owner,
        # restricted, session-mismatch) — an unknown/expired spool id reaching
        # the relay is a denied capability presentation worth the SEL trail.
        try:
            SecurityEventLog().log_api_access(
                caller=_read_session_key(request) or "unknown",
                operation="mcp-apps.call", outcome="denied",
                source="dashboard",
                resources=f"unknown_or_expired_spool spool_id={spool_id}",
            )
        except Exception:  # pragma: no cover — audit must not break the deny
            logger.debug("SEL audit for unknown mcp-apps spool failed", exc_info=True)
        return web.json_response({"error": "unknown or expired app"}, status=404)
    record_session = str(record.get("session_key") or "")
    caller_session = _read_session_key(request)
    # The record stores the CANONICAL producing key (``dashboard:<slot>``) while
    # the honest client sends the bare slot key; canonicalize before comparing
    # or a legitimate owner call is always refused as a mismatch.
    caller_canonical = _history_key_for(caller_session) if caller_session else ""
    if record_session and record_session not in (caller_session, caller_canonical):
        try:
            SecurityEventLog().log_api_access(
                caller=caller_session or "unknown",
                operation="mcp-apps.call", outcome="denied",
                source="dashboard",
                resources=f"spool_session_mismatch spool_id={spool_id}",
            )
        except Exception:  # pragma: no cover
            logger.debug("SEL audit for mcp-apps session mismatch failed", exc_info=True)
        return web.json_response({"error": "app belongs to another session"}, status=403)

    frame = {
        "type": "app-call", "spool_id": spool_id, "callback_secret": callback_secret,
        "tool": tool, "arguments": arguments,
    }
    try:
        reply = await asyncio.wait_for(_gateway_app_call(frame), timeout=_GATEWAY_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        return web.json_response({"error": "gateway timed out"}, status=504)
    except (ConnectionError, OSError, ValueError) as exc:
        logger.warning("mcp-apps call relay failed: %s", exc)
        return web.json_response({"error": "MCP gateway not available"}, status=503)

    kind = reply.get("type") if isinstance(reply, dict) else None
    if kind == "app-result":
        # Offload redaction (recurses over payloads up to the frame cap).
        redacted = await asyncio.to_thread(_redact_result, reply.get("result"))
        return web.json_response({"result": redacted})
    if kind == "app-error":
        # Backend JSON-RPC error the app understands — redact it too: an error
        # message/data can carry credential-bearing backend output before it
        # reaches the network-capable iframe.
        redacted_err = await asyncio.to_thread(_redact_result, reply.get("error"))
        return web.json_response({"error": redacted_err})
    if kind == "app-call-rejected":
        reason = str(reply.get("reason", "rejected"))
        status = 404 if "unknown or expired" in reason else 403
        return web.json_response({"error": reason}, status=status)
    logger.warning("mcp-apps call: unexpected gateway reply type %r", kind)
    return web.json_response({"error": "unexpected gateway reply"}, status=502)
