"""``POST /agent-tools``: the ``ops_mission_control_api`` agent tool, served by the app.

``app.json`` declares the tool in its ``agentTools`` block (App Kit seam level 2). The
gateway lists it in its ``kirocrew-core`` MCP server, checks a call against the declared
schema, and posts ``{"tool": <name>, "arguments": {...}}`` to this route through the
route registry (``backend/agent_tool_hook.py`` registers it there). Everything the
declaration cannot say is decided here, before any other handler runs:

- **The allowlist.** Only an exact ``(method, path)`` of :data:`ALLOWED_CALLS` is
  dispatched, the surface the SOPs use. The human-decision and configuration routes
  (``/incident/proposal/decide``, ``/incident/propose``, ``/proposals``,
  ``/providers*``, ``/settings``, ``/webhook``, bare ``/incident``) are deliberately
  not on it: an agent that needs one of those is off-SOP.
- **The cross-field rules.** A query only on GET, and only value-position characters
  (:data:`QUERY_RE`), so a query can never rewrite the path it is appended to; a body
  only on POST, at most :data:`MAX_BODY_CHARS`, and only a JSON object.
- **The inbound scrub.** The decoded body goes through ``routes._scrub_inbound`` (the
  hidden-character sanitizer, then the credential pass on every string) before it
  reaches a handler; ``routes`` is the surface's registered redaction sink.
- **The channel-agent hold.** :data:`CHANNEL_HELD_OPERATIONS` are the calls that start
  work outliving the caller's turn (``POST /rotation/arm`` arms this app's crons, which
  then fire unattended). A call whose forwarded session key is a channel agent's
  (``channel:...``) is refused; every other call of a channel agent, the reads above
  all, stays reachable.

The call is then answered by the same handler the app's own route serves, behind the same
enable gate, handed an :class:`AgentCall`: the forwarded request as that one call reads it
(its method, path and query, the gateway's application, and the decoded body
``_shared._json_body`` reads). aiohttp cannot clone a request whose body was read, and the
gateway read this one to check it. So every route's validation, audit and response shape
is the one a direct call gets. The answer is returned unchanged; the gateway redacts it
and cuts it at the declared ``maxResultChars``.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Awaitable, Callable

from aiohttp import web
from yarl import URL

from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    APP_NAME,
    AuditWriter,
)

#: The tool's name, as ``app.json`` ``agentTools.tools[0].name`` declares it.
TOOL_NAME = "ops_mission_control_api"

#: The whole agent surface. Widening it is an authorization change: it must come with
#: the SOP that needs it, and ``tests/test_agent_api_tool.py`` pins it.
ALLOWED_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/state"),
        ("GET", "/signals"),
        ("GET", "/incidents"),
        ("GET", "/handover"),
        ("GET", "/rotation"),
        ("GET", "/ledger"),
        ("GET", "/ledger/contradictions"),
        ("POST", "/dispatch"),
        ("POST", "/incident/transition"),
        ("POST", "/incident/claim"),
        ("POST", "/incident/action"),
        ("POST", "/rotation/arm"),
        ("POST", "/ledger"),
        ("POST", "/ledger/hygiene"),
    }
)

#: The operations a channel agent may not call through the tool.
CHANNEL_HELD_OPERATIONS: frozenset[tuple[str, str]] = frozenset({("POST", "/rotation/arm")})

#: Value-position characters only: no ``/``, ``?`` or ``#``. ``%`` admits encoded values.
QUERY_RE = re.compile(r"^[A-Za-z0-9_.=&%+,:-]*$")
MAX_QUERY_CHARS = 512
#: Ledger entries are the largest legitimate payload; 32 KiB is about 4x the biggest seen.
MAX_BODY_CHARS = 32_768

_ARGUMENTS = frozenset({"method", "path", "query", "body_json"})

#: The header the gateway's MCP client sends with the caller's session key.
SESSION_KEY_HEADER = "X-Session-Key"
_CHANNEL_SESSION_PREFIX = "channel:"

#: ``routes._served_call``: the gated handler the route of one allowed call serves.
ServedCall = Callable[[str, str], Callable[[Any], Awaitable[web.StreamResponse]]]


class AgentCall:
    """One allowed call as its route's handler reads it, over the forwarded request.

    The handlers on :data:`ALLOWED_CALLS` read ``method``, ``path``, ``query``, ``app``
    (the gateway's state) and their JSON body through ``_shared._json_body``; none has a
    path parameter. ``tests/test_agent_api_tool.py`` drives every allowed call through it.
    """

    def __init__(self, forwarded: web.Request, method: str, url: URL, body: dict[str, Any]):
        self._forwarded = forwarded
        self._body = body
        self.method = method
        self.rel_url = url
        self.path = url.path
        self.query = url.query
        self.match_info: dict[str, str] = {}

    @property
    def app(self) -> web.Application:
        return self._forwarded.app

    @property
    def headers(self) -> Any:
        return self._forwarded.headers

    async def json(self) -> Any:
        """The decoded, scrubbed body; a GET has none, as an empty request body has none."""
        if self.method != "POST":
            raise ValueError("GET calls take no body")
        return copy.deepcopy(self._body)


def _refuse(status: int, code: str, error: str) -> web.Response:
    return web.json_response({"ok": False, "error": error, "code": code}, status=status)


def _arguments(body: Any) -> dict[str, str] | web.Response:
    """The call's arguments, or the refusal of a body that is not one call of this tool."""
    if not isinstance(body, dict) or set(body) != {"tool", "arguments"}:
        return _refuse(400, "invalid_json", "body must be {tool, arguments}")
    if body["tool"] != TOOL_NAME:
        return _refuse(404, "unknown_tool", f"{APP_NAME} serves no tool {body['tool']!r}")
    args = body["arguments"]
    if not isinstance(args, dict):
        return _refuse(400, "invalid_arguments", "arguments must be an object")
    unknown = sorted(str(k) for k in set(args) - _ARGUMENTS)
    if unknown:
        return _refuse(400, "invalid_arguments", f"unknown arguments {unknown}")
    if not all(isinstance(v, str) for v in args.values()):
        return _refuse(400, "invalid_arguments", "every argument is a string")
    return args


def _call_refusal(method: str, path: str, query: str, body_json: str) -> str:
    """Why this call is outside the agent surface, or ``""``."""
    if (method, path) not in ALLOWED_CALLS:
        return f"{method} {path} is not part of the {APP_NAME} agent surface"
    if query and method != "GET":
        return "query is only accepted on GET calls"
    if len(query) > MAX_QUERY_CHARS or not QUERY_RE.fullmatch(query):
        return "query holds characters outside a query string's values"
    if body_json and method == "GET":
        return "GET calls take no body"
    if len(body_json) > MAX_BODY_CHARS:
        return f"body_json is longer than {MAX_BODY_CHARS} characters"
    return ""


def _held_for_channel(request: web.Request, method: str, path: str) -> bool:
    if (method, path) not in CHANNEL_HELD_OPERATIONS:
        return False
    return request.headers.get(SESSION_KEY_HEADER, "").startswith(_CHANNEL_SESSION_PREFIX)


async def _handle_agent_tool(
    request: web.Request,
    *,
    _audit: AuditWriter,
    _scrub_inbound: Callable[[Any], Any],
    _served_call: ServedCall,
) -> web.StreamResponse:
    """POST /agent-tools: see the module docstring."""
    try:
        raw: Any = await request.json()
    except (LookupError, RecursionError, ValueError):
        raw = None
    args = _arguments(raw)
    if isinstance(args, web.Response):
        return args
    method = args.get("method", "")
    path = args.get("path", "")
    query = args.get("query", "")
    body_json = args.get("body_json", "")
    refusal = _call_refusal(method, path, query, body_json)
    if refusal:
        return _refuse(400, "invalid_arguments", refusal)
    operation = f"{method} {path}"
    if _held_for_channel(request, method, path):
        _audit("agent_tool", operation, "denied", error="channel agent")
        return _refuse(
            403,
            "channel_agent_refused",
            f"{operation} on {TOOL_NAME} is not available to channel agents -- a channel "
            "agent may not start work that outlives its own turn. Do the work in the turn "
            "and report it as a channel post.",
        )
    body: dict[str, Any] = {}
    if body_json:
        try:
            parsed = json.loads(body_json)
        except ValueError:
            return _refuse(400, "invalid_arguments", "body_json is not valid JSON")
        if not isinstance(parsed, dict):
            return _refuse(400, "invalid_arguments", "body_json must encode a JSON object")
        body = _scrub_inbound(parsed)
    target = URL(f"/api/apps/{APP_NAME}{path}" + (f"?{query}" if query else ""), encoded=True)
    call: Any = AgentCall(request, method, target, body)
    return await _served_call(method, path)(call)
