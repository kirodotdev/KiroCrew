"""``POST /api/mcp/try-tool``: list or run one tool of a configured MCP server, once.

Reached only through the route ``dashboard.routes.agent_config`` registers, which
imports this module once ``mcp.tool_tester`` is on; owner-only. Each request runs
the server through the discovery probe (:func:`kiro_crew.mcp_discovery.probe_server`):
the same sandboxed spawn on a private loop, the same handshake, the same teardown of
its whole process tree, with one ``tools/call`` added after the listing when a tool
is named. A muted or remote server is refused before anything starts. A tool not
declared read-only (the shareability rule: positive ``readOnlyHint`` only) needs
``confirm: true``; a switched-off tool, or one the governance ceiling or the
dashboard's profile denies, is refused. Every run and refusal is written to the
security event log, and a run whose record cannot be written is refused. The result
is redacted as a transcript is, then cut to :data:`_MAX_RESULT_CHARS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger("kiro_crew.dashboard.handlers.mcp")

_CALL_TIMEOUT_SECS = 30.0
#: How long past the run's limit the backstop waits, so the probe's own teardown ends
#: first: its stderr read (about 1.9 s), its exit wait and the wait after the kill
#: (``2 * _PROBE_TEARDOWN_WAIT_SECS``) and the Windows tree kill (taskkill's 5 s).
_TEARDOWN_MARGIN_SECS = 20.0
_MAX_RESULT_CHARS = 64 * 1024
_MAX_TOOLS = 200
_MAX_LIST_CHARS = 256 * 1024
_MAX_TEXT_CHARS = 1000
#: The identity a tester run sends; a probe under its own identity is not cached.
_CLIENT_INFO = {"name": "kirocrew-try-tool", "version": "1.0.0"}
#: The session key a run's governance question is asked under. It classifies as the
#: dashboard surface (``sel._infer_source``), so the profile bound to that surface binds.
_SESSION_KEY = "dashboard:mcp-try-tool"
#: Refusals that come before the server starts: (status, code, message).
_REFUSED = {
    "unknown_server": (404, "unknown server"),
    "server_disabled": (409, "server switched off"),
    "not_local": (409, "only a local server can be tried here"),
}


def _redacted(text: str) -> str:
    from kiro_crew.platform.context import redact_via_context

    return redact_via_context(text)


def _redacted_schema(schema: object) -> dict | None:
    """*schema* redacted like the rest of a listing, or ``None`` when it is discarded.

    A schema that is not an object, is over the size cap or stops being JSON once
    redacted is discarded, and the panel then asks for the arguments as raw JSON; ``{}``
    stays the schema of a tool that takes none.
    """
    if not isinstance(schema, dict):
        return None
    text = json.dumps(schema)
    if len(text) > _MAX_RESULT_CHARS:
        return None
    try:
        parsed = json.loads(_redacted(text))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_rows(
    tools: list[Any], server: str, want: str = ""
) -> tuple[list[dict[str, Any]], int, bool, int, bool]:
    """The listing rows, the tools the limits left out, whether *want* is one of them, the
    tools withheld for their names, and whether *want* is one of those.

    A tool past :data:`_MAX_TOOLS`, past the :data:`_MAX_LIST_CHARS` budget or with a name
    too long to show is counted, never dropped without a trace. A name the redactor would
    change is neither listed nor run: it is counted apart, so a secret in it is not shown.
    """
    from kiro_crew.cron_script import _switched_off_tools, _tool_is_switched_off
    from kiro_crew.mcp_gateway.shareability import _all_tools_read_only

    off = _switched_off_tools(server)
    rows: list[dict[str, Any]] = []
    used, left_out, wanted_left_out, withheld, wanted_withheld = 0, 0, False, 0, False
    for tool in tools:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name:
            continue
        if len(name) <= _MAX_TEXT_CHARS and _redacted(name) != name:
            withheld += 1
            wanted_withheld = wanted_withheld or name == want
            continue
        if len(rows) < _MAX_TOOLS and used <= _MAX_LIST_CHARS and len(name) <= _MAX_TEXT_CHARS:
            text, ann = tool.get("description"), tool.get("annotations")
            schema = _redacted_schema(tool.get("inputSchema"))
            used += len(name) + len(json.dumps(schema))
            if used <= _MAX_LIST_CHARS:
                rows.append(
                    {
                        "name": name,
                        "description": _redacted(text if isinstance(text, str) else "")[
                            :_MAX_TEXT_CHARS
                        ],
                        "inputSchema": schema,
                        "needsConfirm": not (isinstance(ann, dict) and _all_tools_read_only([ann])),
                        "switchedOff": _tool_is_switched_off(name, off),
                    }
                )
                continue
        left_out += 1
        wanted_left_out = wanted_left_out or name == want
    return rows, left_out, wanted_left_out, withheld, wanted_withheld


def _local_row(name: str) -> tuple[Any, str]:
    """The listed row for server *name*, or why it may not start. Blocking reads."""
    from kiro_crew.mcp_cleanup import mcp_server_muted_in_any_store
    from kiro_crew.mcp_discovery import list_servers

    row = next((s for s in list_servers() if s.name == name), None)
    if row is None:
        return None, "unknown_server"
    # The row's flag covers only the crew scope; a mute in any scope is consent withheld.
    if row.disabled or mcp_server_muted_in_any_store(name):
        return None, "server_disabled"
    if row.is_remote or not row.command:
        return None, "not_local"
    return row, ""


def _governance_refusal(server: str, tool: str, arguments: dict) -> tuple[int, str, str] | None:
    """The refusal when the ceiling or the dashboard's profile forbids ``@server/tool``.

    The same ``mcp``-scope question the tool-call gate and an app's tool call ask
    (``gate_decision`` with the trusted reference), on the ceiling boot installed in
    this process and the profile bound to the dashboard surface, so an
    administrator's deny binds this run too. An evaluation that fails refuses.
    Blocking reads; called on the probe's private loop.
    """
    try:
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import gate_decision
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        ceiling = current_context().governance
        profile = resolve_active_scope(_SESSION_KEY)
        if ceiling is None and profile is None:
            return None
        decision = gate_decision(
            ceiling,
            profile,
            "",
            mcp_ref=f"@{server}/{tool}",
            own_scopes=("mcp",),
            raw_params=arguments,
        )
    except Exception:
        logger.warning("mcp try-tool refused: governance could not be evaluated", exc_info=True)
        return 503, "governance_unavailable", "the governance policy could not be checked"
    if decision.permitted:
        return None
    return 403, "governance_denied", _redacted(f"blocked by governance policy: {decision.reason}")


class _Run:
    """One request's choice of call, made from the listing, and its audit records."""

    def __init__(self, server: str, tool: str, arguments: dict, confirm: bool) -> None:
        self.server, self.tool, self.arguments, self.confirm = server, tool, arguments, confirm
        self.rows: list[dict[str, Any]] = []
        self.left_out = 0
        self.withheld = 0
        self.refusal: tuple[int, str, str] | None = None
        self.called = False

    def audit(self, outcome: str, error: str = "", *, critical: bool = False) -> None:
        """One ``tool_invocation`` record, from the dashboard caller. Blocking write.

        The record names the call as the caller typed it, redacted like the listing,
        so a refused name that carries a credential-shaped value never reaches the log.
        """
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key="dashboard",
            source="dashboard",
            tool_name=_redacted(f"{self.server}/{self.tool}"),
            tool_kind="dashboard_mcp_tool",
            outcome=outcome,
            error=error,
            metadata={"confirm": self.confirm},
            critical=critical,
        )

    def refuse(self, status: int, code: str, message: str) -> None:
        """Refuse the run; its record is best effort, since nothing runs either way."""
        self.refusal = status, code, message
        try:
            self.audit("denied", code)
        except Exception:
            logger.warning("mcp try-tool: refusal audit unavailable (%s)", code, exc_info=True)

    def choose(self, tools: list[Any]) -> dict[str, Any] | None:
        """The ``tools/call`` params for the probe, or None; on the probe's private loop."""
        self.rows, self.left_out, listed_over_limit, self.withheld, name_withheld = _tool_rows(
            tools, self.server, self.tool
        )
        if not self.tool:
            return None
        row = next((r for r in self.rows if r["name"] == self.tool), None)
        if row is None and name_withheld:
            self.refuse(403, "tool_name_withheld", "tool name withheld")
        elif row is None and listed_over_limit:
            self.refuse(404, "tool_not_listed", "not listed: over the limit")
        elif row is None:
            self.refuse(404, "unknown_tool", "unknown tool")
        elif row["switchedOff"]:
            self.refuse(403, "tool_switched_off", "tool switched off")
        elif (denied := _governance_refusal(self.server, self.tool, self.arguments)) is not None:
            self.refuse(*denied)
        elif row["needsConfirm"] and not self.confirm:
            self.refuse(409, "confirm_required", "confirm required")
        if self.refusal is not None:
            return None
        try:
            # Written ahead and synchronously: a call that cannot be recorded never runs.
            self.audit("invoked", critical=True)
        except Exception:
            logger.error("mcp try-tool refused: security log unavailable", exc_info=True)
            self.refusal = 503, "audit_unavailable", "the security log cannot be written"
            return None
        self.called = True
        return {"name": self.tool, "arguments": self.arguments}

    async def finish(self, outcome: str, error: str = "") -> None:
        """The outcome record of a call that was sent; the call cannot be undone now."""
        if not self.called:
            return
        try:
            await asyncio.to_thread(self.audit, outcome, error, critical=True)
        except Exception:
            logger.warning("mcp try-tool: outcome audit unavailable", exc_info=True)


def _failure(server: Any) -> tuple[int, str, str]:
    """The answer for a probe that did not end ``ok``; the server's text is redacted."""
    if server.status == "disabled":
        return 409, "server_disabled", _REFUSED["server_disabled"][1]
    if server.status != "error":
        return 409, "not_started", _redacted(server.error) or "this server is not started here"
    # The probe appends the server's last output below its own first line.
    if server.error.split("\n", 1)[0] == "timeout":
        return 504, "timeout", "no answer in time"
    return 502, "server_failed", _redacted(server.error)[:_MAX_TEXT_CHARS]


def _result_text(result: object) -> tuple[str, bool]:
    """The result as shown: dumped, redacted whole, then cut; and whether it was cut.

    The redactor sees the text from its first character, since it knows a key by its
    first line, and only its output is cut. Blocking: run it off the gateway loop.
    """
    text = _redacted(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return text[:_MAX_RESULT_CHARS], len(text) > _MAX_RESULT_CHARS


async def _try(server: str, tool: str, arguments: dict, confirm: bool) -> tuple[int, dict]:
    """Probe *server*, calling *tool* once when named; ``(status, payload)``."""
    from kiro_crew.mcp_discovery import ToolCallStep, probe_server

    row, refused = await asyncio.to_thread(_local_row, server)
    if row is None:
        status, message = _REFUSED[refused]
        return status, {"error": message, "code": refused}
    run = _Run(server, tool, arguments, confirm)
    # The answer is read against the run's own limit, not the shorter handshake timeout.
    step = ToolCallStep(run.choose, timeout=_CALL_TIMEOUT_SECS)
    probed: Any = None
    try:
        # The probe stops at the step's limit itself; this backstop waits past its teardown.
        probed = await asyncio.wait_for(
            probe_server(row, client_info=_CLIENT_INFO, tool_call=step),
            _CALL_TIMEOUT_SECS + _TEARDOWN_MARGIN_SECS,
        )
    except asyncio.TimeoutError:
        # The cancel reached the probe's private task, whose teardown reaps the tree.
        # A tool that answered before the limit ran out in that teardown did answer.
        if step.answer is None:
            await run.finish("timeout", "no answer in time")
            return 504, {"error": "no answer in time", "code": "timeout"}
    if run.refusal is not None:
        status, code, message = run.refusal
        return status, {"error": message, "code": code}
    failed = probed is not None and probed.status != "ok"
    if failed or (tool and step.answer is None):
        status, code, message = _failure(probed)
        await run.finish("timeout" if code == "timeout" else "error", message)
        return status, {"error": message, "code": code}
    if not tool:
        return 200, {
            "ok": True,
            "tools": run.rows,
            "leftOut": run.left_out,
            "withheld": run.withheld,
        }
    answer = step.answer or {}
    result = answer.get("result", answer.get("error"))
    is_error = "error" in answer or (isinstance(result, dict) and result.get("isError") is True)
    await run.finish("error" if is_error else "completed")
    text, truncated = await asyncio.to_thread(_result_text, result)
    return 200, {"ok": True, "isError": is_error, "result": text, "truncated": truncated}


async def api_mcp_try_tool(request: web.Request) -> web.Response:
    """POST /api/mcp/try-tool -- ``{server, tool?, arguments?, confirm?}``."""
    # Body-scope import for the cycle the sibling probe route names.
    from kiro_crew.dashboard.handlers._shared import (
        read_bounded_json,
        require_owner_dashboard_request,
    )

    denied = await require_owner_dashboard_request(request, "mcp_try_tool")
    if denied is not None:
        return denied
    body, error = await read_bounded_json(request)
    if error is not None or body is None:
        return error or web.json_response({"code": "bad_args"}, status=400)
    server, tool = body.get("server"), body.get("tool", "")
    arguments = body.get("arguments") or {}
    if not (isinstance(server, str) and server and isinstance(tool, str)):
        return web.json_response({"error": "name a server", "code": "bad_args"}, status=400)
    if not isinstance(arguments, dict):
        return web.json_response(
            {"error": "arguments must be an object", "code": "bad_args"}, status=400
        )
    status, payload = await _try(server, tool, arguments, body.get("confirm") is True)
    if status == 200:
        return web.json_response(payload)
    return web.json_response({"error": payload["error"], "code": payload["code"]}, status=status)
