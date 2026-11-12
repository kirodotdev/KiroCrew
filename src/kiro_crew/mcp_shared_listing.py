"""What a stdio MCP server says before it can run a tool: ``initialize`` and ``tools/list``.

The one owner of both answers. :func:`initialize_result` builds the first and
:func:`listable_tools` the second, and nothing else builds either: the dispatch loop
answers from them, and so does :func:`serve_before_handlers`, which answers the same two
requests before the server has imported the code that runs its tools.

That early answer is why this module, and everything it imports, stays import-cheap. An
agent engine starts every one of a session's MCP servers when the session starts and holds
the first prompt until each has listed its tools, so what a server loads before it lists
is paid on every session start, once per server. A server declares itself as a
:class:`StdioServer` in a module that imports no tool handler; the handlers load after the
list is out, and a ``tools/call`` that arrives meanwhile waits in the pipe for them.

Split out of ``kiro_crew.mcp_shared``, which stays the patch target: every name here is
reached as ``kiro_crew.mcp_shared.<name>``, and a patch through that facade reaches this
module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
"""

from __future__ import annotations

import collections
import logging
import queue
import sys
from typing import Any, Callable, NamedTuple

from kiro_crew import platform_compat
from kiro_crew.mcp_caller import (
    INFLIGHT_CAPABILITY_KEY,
    CallerContext,
    caller_identity_capability,
)
from kiro_crew.mcp_shared_policy import (
    ToolPolicy,
    _ambient_audit_session,
    _resolve_tool_policy,
    serves_managed_tools,
)
from kiro_crew.mcp_shared_wire import (
    _frame_within,
    _NoFrame,
    _Skipped,
    _start_stdin_pump,
    respond,
    snapshot_stdout_fd,
)
from kiro_crew.sel import sel
from kiro_crew.validation_parts.field_engine import ValidationError
from kiro_crew.validation_parts.wire_envelopes import validate_jsonrpc_request

#: The family logs as ``kiro_crew.mcp_shared``, the name callers and log filters use.
logger = logging.getLogger("kiro_crew.mcp_shared")

#: The MCP revision every Kiro Crew stdio server answers ``initialize`` with.
PROTOCOL_VERSION = "2024-11-05"

#: Longest :func:`serve_before_handlers` waits for the client's next frame. A client
#: sends ``initialize`` as it starts the server and lists the tools as soon as that is
#: answered, so the wait normally ends at once. It bounds the case where the client does
#: not: the handlers then start loading no later than this after the last frame, so a
#: first ``tools/call`` never waits on a list the client was not going to ask for.
EARLY_FRAME_WAIT_SECS = 1.0


class StdioServer(NamedTuple):
    """One stdio MCP server, declared where importing it loads no tool handler.

    The server's ``run`` function passes these same values to ``run_mcp_stdio_loop``
    (:meth:`loop_options` for the keyword half), so what the early answers say and what
    the loop says later come from one declaration.
    """

    name: str
    version: str
    #: Builds the full tool list. Called for every ``tools/list``; never cached.
    list_tools: Callable[[], list[dict[str, Any]]]
    advertise_caller_identity: bool = False
    error_prefix_is_error: bool = False
    reports_inflight: bool = False
    instructions: str = ""
    #: A step this server owes before it reads its first request, or ``None``. Run
    #: wherever that first read happens; it must be safe to run more than once.
    before_first_request: Callable[[], None] | None = None

    def loop_options(self) -> dict[str, Any]:
        """The keyword arguments ``run_mcp_stdio_loop`` takes for this server.

        Only the ones this server sets: the loop's defaults are this class's defaults,
        so an option left out reads the same on both sides.
        """
        options: dict[str, Any] = {
            "advertise_caller_identity": self.advertise_caller_identity,
            "error_prefix_is_error": self.error_prefix_is_error,
            "reports_inflight": self.reports_inflight,
            "instructions": self.instructions,
        }
        return {name: value for name, value in options.items() if value}


def initialize_result(
    server_name: str,
    server_version: str,
    *,
    advertise_caller_identity: bool = False,
    reports_inflight: bool = False,
    instructions: str = "",
) -> dict[str, Any]:
    """The ``initialize`` result of a Kiro Crew stdio server."""
    capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
    experimental: dict[str, Any] = {}
    if advertise_caller_identity:
        # Pooled-operation opt-in for IDENTITY, not for pooling: gatewayd injects the
        # per-call ``_meta.kirocrew.caller`` block only into a backend that advertised
        # this capability, and nothing declines to POOL one that did not (see
        # ``rewriter.UNPOOLABLE_SERVERS``, which lists only credential vendors and
        # documents exactly that). So NOT advertising does not buy a per-session spawn:
        # it buys a shared process whose dispatch-loop caller slot never receives
        # gateway-authored metadata, which is how a session-scoped tool silently
        # degrades to unattached behaviour.
        experimental.update(caller_identity_capability())
    if reports_inflight:
        # Only a server that opted in advertises: a gateway that believes a backend
        # reports applies the short recycle ceiling to its calls.
        experimental[INFLIGHT_CAPABILITY_KEY] = {}
    if experimental:
        capabilities["experimental"] = experimental
    result: dict[str, Any] = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": capabilities,
        "serverInfo": {"name": server_name, "version": server_version},
    }
    if instructions:
        result["instructions"] = instructions
    return result


def caller_tool_policy(caller: CallerContext | None, *, applies_policy: bool) -> ToolPolicy:
    """One request's exclusion set AND whether the policy was readable.

    The enforcement seam: a ``tools/call`` needs the second half to tell "the operator
    excluded nothing" apart from "we could not read what the operator excluded".

    The caller block's token rides along explicitly. This runs on the read loop before
    the worker installs the caller ContextVar, so the resolver cannot find the token
    there; a pooled backend has it nowhere else.

    *applies_policy* is False for a server that is not Kiro Crew's own
    (:func:`kiro_crew.mcp_shared_policy.serves_managed_tools`): it has no managed tools
    for the policy to name, so it is answered "nothing excluded" without a read.
    """
    if not applies_policy:
        return ToolPolicy(frozenset(), "")
    return _resolve_tool_policy(
        caller.session_key if caller else "",
        caller_token=caller.session_token if caller else "",
    )


def listable_tools(
    server_name: str,
    list_tools_fn: Callable[[], list[dict[str, Any]]],
    caller: CallerContext | None,
    *,
    applies_policy: bool,
) -> list[dict[str, Any]]:
    """The tool list for a ``tools/list``, minus this session's exclusions.

    An unresolved policy lists EVERYTHING on purpose. kiro-cli calls ``tools/list`` once
    per session and caches the answer, so hiding tools on a transient policy failure
    would hide them for the session's whole life. Listing is not the enforcement point;
    ``tools/call`` is, and it refuses while the policy is unresolved. The audit event
    records that a listing went out unfiltered so the widened window is attributable.
    """
    policy = caller_tool_policy(caller, applies_policy=applies_policy)
    tools = list_tools_fn()
    if policy.unresolved:
        try:
            sel().log_api_access(
                caller=(caller.session_key if caller is not None else _ambient_audit_session()),
                operation="tool_policy.unfiltered_listing",
                outcome="unresolved",
                source="mcp",
                resources=f"reason={policy.unresolved}",
            )
        except Exception as sel_exc:
            logger.warning("SEL audit failed for unfiltered listing: %s", sel_exc)
        return tools
    if policy.excluded:
        tools = [t for t in tools if t.get("name") not in policy.excluded]
    included = policy.include_for(server_name)
    if included is not None:
        tools = [t for t in tools if t.get("name") in included]
    return tools


class EarlyFrames:
    """What :func:`serve_before_handlers` hands the dispatch loop that follows it.

    The loop reads *pending* before it reads stdin, so every frame is handled in the
    order it arrived and none is answered twice: a frame is either answered early and
    dropped, or left here untouched.
    """

    def __init__(self) -> None:
        #: The ``params`` of the ``initialize`` answered early, or ``None`` when none
        #: was. The loop reads the client's capabilities from them, as it would have
        #: on answering.
        self.init_params: dict[str, Any] | None = None
        #: Frames read but not answered, oldest first. ``None`` is the end of the stream.
        self.pending: collections.deque[dict[str, Any] | None] = collections.deque()
        #: The stdin pump thread's queue where the platform reads stdin through one.
        #: The loop must keep reading from it: a second pump would race this one for
        #: the same lines.
        self.pump: queue.Queue[Any] | None = None


class _EarlyState:
    """This process's early answers: served at most once, taken by at most one loop."""

    def __init__(self) -> None:
        self.served = False
        self.frames: EarlyFrames | None = None


_EARLY = _EarlyState()


def serve_before_handlers(server: StdioServer, *, wait_secs: float = EARLY_FRAME_WAIT_SECS) -> None:
    """Answer *server*'s ``initialize`` and ``tools/list`` now, from this process's stdin.

    Called by the server's entry before it imports the tool handlers. It answers the two
    requests a client sends on starting a server and returns: after the list, at the
    first frame it does not serve (left for the loop, unanswered), when the client stays
    silent for *wait_secs*, or at the end of the stream. It runs at most once per process;
    a second call is a no-op, so an entry that retries its imports may call it again.

    The answers are the dispatch loop's own (:func:`initialize_result`,
    :func:`listable_tools`), so a client cannot tell which of the two wrote them.
    """
    if _EARLY.served:
        return
    _EARLY.served = True
    early = EarlyFrames()
    _EARLY.frames = early
    if server.before_first_request is not None:
        server.before_first_request()
    snapshot_stdout_fd()
    stdin = sys.stdin
    try:
        early.pump = None if platform_compat.IS_POSIX else _start_stdin_pump(stdin)
        while True:
            frame = _frame_within(stdin, early.pump, wait_secs)
            if isinstance(frame, _NoFrame):
                return
            if isinstance(frame, _Skipped):
                continue
            if frame is None:
                early.pending.append(None)
                return
            if not _answer_early(server, early, frame):
                return
    except (OSError, ValueError, TypeError):
        # A stdin this cannot wait on (no descriptor, already closed): nothing was
        # answered here that the loop will not handle from the frames left for it.
        logger.debug("%s: no early answers on this stdin", server.name, exc_info=True)


def _answer_early(server: StdioServer, early: EarlyFrames, frame: dict[str, Any]) -> bool:
    """Answer *frame* if it is one of the two early requests; True to keep reading.

    Anything else, a malformed envelope included, is left in ``pending`` for the loop,
    which answers or refuses it exactly as it always has.
    """
    try:
        method, req_id, params = validate_jsonrpc_request(frame)
    except ValidationError:
        early.pending.append(frame)
        return False
    if method == "initialize" and req_id is not None and early.init_params is None:
        respond(
            req_id,
            initialize_result(
                server.name,
                server.version,
                advertise_caller_identity=server.advertise_caller_identity,
                reports_inflight=server.reports_inflight,
                instructions=server.instructions,
            ),
        )
        early.init_params = params
        return True
    if method == "notifications/initialized" and early.init_params is not None:
        return True
    if method == "tools/list" and req_id is not None and early.init_params is not None:
        try:
            tools = listable_tools(
                server.name,
                server.list_tools,
                CallerContext.from_meta(params.get("_meta")),
                applies_policy=serves_managed_tools(server.list_tools),
            )
        except Exception:  # noqa: BLE001 - the loop answers this frame, as it always has
            logger.exception("%s: listing tools early failed; the loop answers", server.name)
            early.pending.append(frame)
            return False
        respond(req_id, {"tools": tools})
        return False
    early.pending.append(frame)
    return False


def take_early_frames() -> EarlyFrames | None:
    """The frames :func:`serve_before_handlers` left for the loop, handed over once."""
    frames = _EARLY.frames
    _EARLY.frames = None
    return frames
