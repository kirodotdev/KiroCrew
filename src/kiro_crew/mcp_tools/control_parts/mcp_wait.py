"""``mcp_server_wait``: one call that returns once a session's MCP server is usable, or cannot be.

A server that is still connecting when a turn needs it is not in the model's tool
list yet. The turn's prompt says so (``context_assembly/mcp_servers.py``) and points here: the
model tells the user the tool is still starting, calls this once, and the call
returns the moment the server has connected, so the turn can load the tool and
finish the task without spending a model round trip per look.

The answer is the calling session's own MCP report, read by the gateway
(``agent_sdk.mcp_problems.mcp_server_state``) on the keepalive route every parked
tool already pings, so the wait also keeps the session from reading as stalled.
Nothing is kept here: each step is one loopback request under the caller's strict
session key.

This module is an owner of :mod:`kiro_crew.mcp_tools.control`, which serves every
name here and stays the import path and the patch seam. It never imports the facade.
"""

from __future__ import annotations

import json
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.agent_sdk.mcp_problems import MCP_SERVER_WAIT_SECS
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import MCP_SERVER_WAIT_SCHEMA, validate_tool_args

#: How often a parked wait asks the gateway for the server's state. It is the
#: longest the call can lag behind the server connecting.
_POLL_SECS = 1.0
#: Bound on one state request: loopback answers in milliseconds, and a stalled
#: gateway must not hold the shared step pool (the next step asks again).
_POLL_TIMEOUT_SECS = 5.0
#: Consecutive unanswered state requests after which the wait stops asking.
_MAX_UNANSWERED = 5

#: What the model is told for each settled state. Each says what is true and
#: what to do next, because the model's next words are to the user.
_SETTLED_TEXT = {
    "connected": (
        "MCP server {server} is connected. Its tools are in your tool list now: "
        "load the one you need, use it, and finish the task."
    ),
    "failed": (
        "MCP server {server} failed to start in this session, so its tools are not "
        "available. Do not wait for it. Tell the user it failed to start (its "
        "command can be checked on the MCP page; a new chat starts it again) and "
        "do what you can of the task without it."
    ),
    "needs_sign_in": (
        "MCP server {server} is waiting for a person to sign in, so its tools are "
        "not available yet. Do not wait for it. Tell the user it needs a sign-in "
        "and do what you can of the task without it."
    ),
    "disabled": (
        "MCP server {server} is switched off, so its tools are not available. Do "
        "not wait for it. Tell the user it is switched off (it can be switched on "
        "from the MCP page) and do what you can of the task without it."
    ),
    "registry_dropped": (
        "MCP server {server} was left out by the organization's MCP registry and "
        "will not start in this session. Tell the user so and do what you can of "
        "the task without it."
    ),
}
#: No report names the server. That is not proof it is absent: some hosts report
#: none of their servers, and a name may be misspelt. So the text sends the model
#: to its tool list and never says the server is missing.
_UNKNOWN_TEXT = (
    "Kiro Crew has no report of an MCP server named {server} in this session{hint}. "
    "That does not mean it is missing: {why}. Check your tool list: if the server's "
    "tools are there, use them. If the name was misspelt, call this again with the "
    "right one. Otherwise tell the user you cannot see that server in this session. "
    "Do not say it does not exist."
)
#: Why no report names the server: the host may report none, or (the second) Kiro
#: Crew kept some of its reports out at its bound and says how many.
_UNKNOWN_NO_REPORTS = "this session's host may not report its servers"
_UNKNOWN_LEFT_OUT = (
    "this session's host reported more servers than Kiro Crew keeps a state for, "
    "{count} of its reports were left out, and this server may be one of them"
)
_TIMED_OUT_TEXT = (
    "MCP server {server} is still not ready after {secs:g} s. Tell the user so "
    "plainly, do what you can of the task without it, and say they can ask again "
    "in a moment. Do not call this again in this turn."
)
_UNREADABLE_TEXT = (
    "Kiro Crew could not read this session's MCP servers{why}. Check your tool "
    "list: if the server's tools are there, use them; if not, tell the user the "
    "server is not ready yet and that they can ask again in a moment."
)


def _quoted(name: str) -> str:
    """*name* as the model reads it back: scrubbed, bounded and quoted."""
    safe, _ = redact_exfiltration_urls(name)
    safe, _ = redact_credentials(safe)
    return json.dumps(safe[:128], ensure_ascii=False)


def _count(value: Any) -> int:
    """A count the gateway sent, or 0 for anything that is not a positive number."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def mcp_server_wait(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    """Wait, without holding the MCP worker thread, until the named server settles."""
    args = validate_tool_args(args, MCP_SERVER_WAIT_SCHEMA)
    server = str(args.get("server") or "").strip()
    # Strict identity or no answer: under the lenient walk a sub-agent resolves to
    # its PARENT's session, and would be told about servers that are not its own.
    key, _ = mcp_core.require_strict_session_key("mcp_server_wait")
    if not key:
        return _UNREADABLE_TEXT.format(why=" (it cannot tell which session is calling)")
    return _McpWaitStep(server=server, session_key=key)


class _McpWaitStep(DeferredTool):
    """The parked half of :func:`mcp_server_wait`: one state request per step."""

    def __init__(self, *, server: str, session_key: str) -> None:
        self.server = server
        self.session_key = session_key
        now = mcp_core.time.monotonic()
        self.deadline = now + MCP_SERVER_WAIT_SECS
        self.next_poll = now
        self.unanswered = 0

    def due_at(self) -> float:
        return min(self.next_poll, self.deadline)

    def step(self) -> str | None:
        try:
            reply = mcp_core._post(
                "/api/session-keepalive",
                {"mcp_server": self.server},
                timeout=_POLL_TIMEOUT_SECS,
                session_key=self.session_key,
            )
        except Exception:  # noqa: BLE001 - one lost request is retried by the next step
            reply = {}
        state = reply.get("mcp_server_state") if isinstance(reply, dict) else None
        if not isinstance(state, str):
            # ``_post`` answers ``{"error": ...}`` for a failed round trip.
            self.unanswered += 1
            if self.unanswered >= _MAX_UNANSWERED:
                return _UNREADABLE_TEXT.format(why="")
        else:
            self.unanswered = 0
            if state in _SETTLED_TEXT:
                return _SETTLED_TEXT[state].format(server=_quoted(self.server))
            if state != "connecting":
                names = reply.get("mcp_server_names")
                known = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
                listed = ", ".join(_quoted(n) for n in known)
                more = _count(reply.get("mcp_server_names_more"))
                listed += f", and {more} more" if known and more else ""
                hint = f" (servers it does report as not usable yet: {listed})" if known else ""
                left_out = _count(reply.get("mcp_reports_left_out"))
                why = _UNKNOWN_LEFT_OUT.format(count=left_out) if left_out else _UNKNOWN_NO_REPORTS
                return _UNKNOWN_TEXT.format(server=_quoted(self.server), hint=hint, why=why)
        now = mcp_core.time.monotonic()
        if now >= self.deadline:
            return _TIMED_OUT_TEXT.format(server=_quoted(self.server), secs=MCP_SERVER_WAIT_SECS)
        self.next_poll = now + _POLL_SECS
        return None
