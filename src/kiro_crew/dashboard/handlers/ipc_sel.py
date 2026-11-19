"""``POST /api/ipc/sel``: the gateway writes an MCP stdio server's audit records.

Under an enforcing harness the credential mask withholds ``trust/sel_hmac.key`` from
the harness's process tree on purpose, so a Kiro Crew MCP stdio server started there
cannot sign its own SEL records (``sel.sel``). It hands each record to the gateway
over the agent socket instead (``kiro_crew.mcp_sel_forward``), and the gateway signs
and appends it. The precedent is ``kiro_crew.apps.sel_routes`` for app backends.

Who may call: only a request the agent socket admitted with a capability
(``request[GRANT_KEY]``, ``dashboard.agent_ipc``). The capability is bound to one
process, one server and one session, and the gate already held ``X-Session-Key`` to
that session. A strict internal path reached over loopback TCP with the internal
secret is refused here: that caller can read the trust root and writes its own.

What the body can and cannot say:

* The actor is the capability's: ``session_key``/``caller`` is the bound session,
  ``source`` is ``mcp``, and each record is stamped with the process role and the
  bound server name. A body naming ``session_key``, ``caller``, ``agent``,
  ``source``, the role fields, or any field outside the allowlist is a 400.
* Two kinds only, the ones an MCP server writes on its tool-call path:
  ``log_tool_invocation`` and ``log_api_access``. Every other kind is a 400.
* Each refusal after admission is itself written as a decision record.

Responses never echo the body. A refused write is a status code and a fixed string.

The body cap, the rate limiter and the refusal answer are ``apps.sel_routes``'s own;
this route keeps only its caller check, its allowlist parser and its actor stamping.

shortcut: the limiter is per process and per (session, server), in memory; upgrade
to a shared budget if the gateway ever runs more than one process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.sel_routes import _read_capped, _refuse, _take
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

PATH = "/api/ipc/sel"
#: The process role stamped on every record this route writes.
PROCESS_ROLE = "mcp_stdio_server"

#: Longest single string field accepted.
_MAX_FIELD = 500
#: Most entries, and longest key, in a ``metadata`` mapping.
_MAX_METADATA_KEYS = 16
_MAX_METADATA_KEY = 64

_STR = "str"
_BOOL = "bool"
_META = "metadata"
_ID = "id"

#: method -> (required fields, optional fields), each field -> its type.
_ALLOWED: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "log_tool_invocation": (
        {"tool_name": _STR, "outcome": _STR},
        {
            "tool_kind": _STR,
            "request_id": _ID,
            "downstream_service": _STR,
            "resources": _STR,
            "error": _STR,
            "metadata": _META,
            "critical": _BOOL,
        },
    ),
    "log_api_access": (
        {"operation": _STR, "outcome": _STR},
        {"resources": _STR, "error": _STR, "critical": _BOOL},
    ),
}

#: (session, server) -> [tokens, last refill, last denial row]. Event loop only.
_buckets: dict[tuple[str, str], list[float]] = {}


def _valid(kind: str, value: Any) -> bool:
    if kind == _BOOL:
        return isinstance(value, bool)
    if kind == _ID:
        return (isinstance(value, str) and len(value) <= _MAX_FIELD) or (
            isinstance(value, int) and not isinstance(value, bool)
        )
    if kind == _META:
        return (
            isinstance(value, dict)
            and len(value) <= _MAX_METADATA_KEYS
            and all(
                isinstance(k, str)
                and len(k) <= _MAX_METADATA_KEY
                and isinstance(v, (str, int, float, bool))
                and not (isinstance(v, str) and len(v) > _MAX_FIELD)
                for k, v in value.items()
            )
        )
    return isinstance(value, str) and len(value) <= _MAX_FIELD


def _parse(raw: bytes) -> tuple[str, dict[str, Any]] | None:
    """``(method, kwargs)`` when the body is exactly an allowed shape, else None."""
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or set(body) != {"method", "kwargs"}:
        return None
    method, kwargs = body["method"], body["kwargs"]
    if not isinstance(method, str) or method not in _ALLOWED or not isinstance(kwargs, dict):
        return None
    required, optional = _ALLOWED[method]
    if not set(required) <= set(kwargs) or not set(kwargs) <= set(required) | set(optional):
        return None
    types = {**required, **optional}
    if not all(_valid(types[key], value) for key, value in kwargs.items()):
        return None
    return method, kwargs


def _write(session: str, server: str, method: str, kwargs: dict[str, Any]) -> None:
    """Sign and append one record as the capability's actor, stamped with its role."""
    log = sel()
    if method == "log_tool_invocation":
        metadata = dict(kwargs.pop("metadata", None) or {})
        metadata.update(process_role=PROCESS_ROLE, server=server)
        log.log_tool_invocation(session_key=session, source="mcp", metadata=metadata, **kwargs)
        return
    resources = f"process_role={PROCESS_ROLE},server={server};{kwargs.pop('resources', '')}"
    log.log_api_access(caller=session, source="mcp", resources=resources, **kwargs)


def _refusal_row(session: str, server: str, code: str) -> None:
    """The refusal is a decision; the gateway records it with its own writer."""
    try:
        sel().log_api_access(
            caller=session or "agent-ipc",
            operation="ipc_sel",
            outcome="rejected",
            source="agent_ipc",
            resources=f"process_role={PROCESS_ROLE},server={server}",
            error=code,
        )
    except Exception:  # noqa: BLE001 - best effort; the refusal already answered
        logger.debug("SEL refusal row for ipc_sel failed", exc_info=True)


async def handle_ipc_sel(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.agent_ipc import GRANT_KEY

    binding = request.get(GRANT_KEY)
    if binding is None:
        return _refuse("agent socket capability required", "ipc_capability_required", 403)
    # The agent socket gate held this header to the capability's bound session.
    session = request.headers.get("X-Session-Key", "")
    server = str(binding.server)
    allowed, log_denial = _take((session, server), _buckets)
    if not allowed:
        if log_denial:
            await asyncio.to_thread(_refusal_row, session, server, "sel_rate_limited")
        return _refuse("rate limited", "sel_rate_limited", 429)
    raw = await _read_capped(request)
    if raw is None:
        await asyncio.to_thread(_refusal_row, session, server, "sel_body_too_large")
        return _refuse("body too large", "sel_body_too_large", 413)
    parsed = _parse(raw)
    if parsed is None:
        await asyncio.to_thread(_refusal_row, session, server, "sel_invalid_body")
        return _refuse("invalid body", "sel_invalid_body", 400)
    try:
        await asyncio.to_thread(_write, session, server, *parsed)
    except Exception:  # noqa: BLE001 - one fixed answer; the cause stays in the gateway log
        logger.warning("ipc_sel: SEL write failed for %s", server, exc_info=True)
        return _refuse("audit write failed", "sel_write_failed", 500)
    return web.Response(status=204)
