"""Per-call silence bounds: which bound a dispatched tool call gets, and when it ran out.

From the client side a call the harness never answers looks like a call that is only
slow: an MCP ``tools/call`` has no start acknowledgement, and progress is optional. So
the session handle keeps a SILENCE timer per call. It starts at dispatch, only a frame
attributable to that call restarts it, and when it runs out the call is cancelled and
the model is told it timed out, so it can retry (``kiro_crew.timed_out_call``). The model's own
next step retires the timers of the calls before it, which have returned to it. The liveness oracle and
``watchdog.tool_stall_hard_cap_secs`` stay behind it as the backstop, and a shell call
and a built-in its harness never terminates keep the oracle's rules alone.

Classes, and the ``watchdog.*`` bound each one reads:

* ``builtin``: a harness built-in that is not a shell (a file edit, a read, a search,
  a fetch), ``builtin_tool_silence_secs`` (5 min);
* ``mcp_network``: an MCP tool on a server the session declared with a ``url``,
  ``mcp_network_silence_secs`` (5 min);
* ``mcp_stdio``: every other MCP tool, Kiro Crew's own servers included,
  ``mcp_stdio_silence_secs`` (30 min);
* ``subagent``: a sub-agent call (the harness's own sub-agent tool, or Kiro Crew's
  ``spawn_run``, ``spawn_continue`` and ``spawn_sub_agents``), whose sub-agent's own
  frames count as its activity, ``subagent_silence_secs`` (10 min). It is not judged
  while a native sub-agent of the session has any call open, and the harness's own
  sub-agent call loses its timer once its sub-agent's frames cannot reach the
  session (``HARNESS_CHILD_SESSION_STREAMS``).

A call that no positive identity places in a class gets no silence timer: a shell
call, a call on a harness outside ``ACP_BACKENDS_TOOL_SILENCE_TIMER``, a
built-in that names neither its tool nor a file-plane kind, and a built-in its
harness never terminates (``HARNESS_UNTERMINATED_TOOLS``).

A timer also runs only while the session can prove it would see its call's
activity, and fails open otherwise: where that activity may be out of sight, the
call is not judged by its silence and the oracle and the hard cap judge it
(``session_handle_tool_silence``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kiro_crew.acp.liveness import _MCP_SEPARATOR_RE, ToolCallState
from kiro_crew.acp.types import ACP_BACKEND_KIRO, ACP_BACKENDS_TOOL_SILENCE_TIMER
from kiro_crew.session_directive import CORE_MCP_SERVER
from kiro_crew.timed_out_call import EVIDENCE_SILENCE_TIMER

SILENCE_CLASS_NONE = ""
SILENCE_CLASS_BUILTIN = "builtin"
SILENCE_CLASS_MCP_NETWORK = "mcp_network"
SILENCE_CLASS_MCP_STDIO = "mcp_stdio"
SILENCE_CLASS_SUBAGENT = "subagent"

#: Class -> the ``WatchdogSettings`` field that holds its bound.
SILENCE_BOUND_KEYS: dict[str, str] = {
    SILENCE_CLASS_BUILTIN: "builtin_tool_silence_secs",
    SILENCE_CLASS_MCP_NETWORK: "mcp_network_silence_secs",
    SILENCE_CLASS_MCP_STDIO: "mcp_stdio_silence_secs",
    SILENCE_CLASS_SUBAGENT: "subagent_silence_secs",
}

#: The harness's own sub-agent tool: kiro-cli stamps ``subagent`` and spells it
#: ``use_subagent`` / ``agent_crew`` in a spec, KAS registers it as
#: ``invoke_sub_agent`` (``platform.tool_names``).
HARNESS_SUBAGENT_TOOLS: frozenset[str] = frozenset(
    {"use_subagent", "subagent", "agent_crew", "invoke_sub_agent"}
)
#: Harness -> the built-ins it dispatches and never terminates, by the tool name
#: the harness stamps on the ``tool_call`` frame. kiro-cli 3.1.0 sends
#: ``introspect``'s ``tool_call`` (``_meta.kiro.toolName``) and never a
#: ``tool_call_update`` for it (``test/fixtures/acp_frames/kiro/tool-classes-a-live.jsonl``),
#: and the model's next step can be another call with no text between, or a sibling
#: in the same batch, so no frame ever marks the call as done. These get no
#: silence class; the oracle and the hard cap judge them as every call was judged
#: before the timer.
HARNESS_UNTERMINATED_TOOLS: dict[str, frozenset[str]] = {
    ACP_BACKEND_KIRO: frozenset({"introspect"}),
}
#: Harnesses whose native sub-agent streams under its own ``sessionId``. The
#: runtime hands those frames to the parent only through the ownerless roster
#: route, which needs the parent to be the one session registered on the
#: runtime (``AcpRuntime.routes_roster_children_to``); on a runtime another
#: session shares they are dropped. KAS is not one: its sub-agent's frames ride
#: the parent's own session, named by ``agentSubtaskId``.
HARNESS_CHILD_SESSION_STREAMS: frozenset[str] = frozenset({ACP_BACKEND_KIRO})
#: Kiro Crew's own sub-agent tools, counted only when ``kirocrew-core`` serves them.
CORE_SUBAGENT_TOOLS: frozenset[str] = frozenset({"spawn_run", "spawn_continue", "spawn_sub_agents"})
#: The ``sessionUpdate`` kinds that are a call's own activity when they name it:
#: its dispatch, its refinements and output, and kiro-cli's preview of its input
#: (``tool_call_chunk``, which kiro-cli sends under the extension spelling for a
#: session's own call too). Any other kind that names a call is one the timer
#: cannot read, and the call loses its timer.
CALL_ACTIVITY_UPDATES: frozenset[str] = frozenset(
    {"tool_call", "tool_call_update", "tool_call_chunk"}
)
#: ACP tool kinds a non-shell built-in carries when it names no tool (KAS stamps
#: only the kind on its built-ins): the file plane and a fetch. ``other`` and
#: ``think`` name nothing, so a call carrying only them gets no timer.
BUILTIN_SILENCE_KINDS: frozenset[str] = frozenset(
    {"read", "edit", "delete", "move", "search", "fetch"}
)


def _bare_tool_name(name: str) -> str:
    """*name* without a server qualifier (``@server/tool``, ``server___tool``)."""
    return _MCP_SEPARATOR_RE.split(name.rsplit("/", 1)[-1])[-1]


def named_tool_call_id(params: Any) -> str:
    """The ``toolCallId`` a frame's *params* name, or "".

    Where the harnesses put it: flat on the params (``elicitation/create``,
    ``_kiro/mcp/elicitation``, ``_kiro/userInput``), in a ``toolCall`` object
    (``session/request_permission``), or in an ``update`` payload.
    """
    if not isinstance(params, dict):
        return ""
    for holder in (params, params.get("toolCall"), params.get("update")):
        if isinstance(holder, dict):
            call_id = holder.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                return call_id
    return ""


def is_unterminated_builtin(backend: Any, *, server: str, tool_name: str) -> bool:
    """Whether *tool_name* names a built-in *backend* dispatches and never terminates.

    Read off the harness's own stamp on a frame that names no MCP server
    (``HARNESS_UNTERMINATED_TOOLS``), never off the title: an MCP tool that only
    shares the name is an ordinary call.
    """
    if server or not tool_name:
        return False
    return tool_name in HARNESS_UNTERMINATED_TOOLS.get(backend, frozenset())


def classify_tool_silence(
    *,
    backend: Any,
    is_shell: bool,
    server: str,
    tool_name: str,
    kind: str,
    network_servers: frozenset[str] = frozenset(),
) -> str:
    """The silence class of one dispatched call (a ``SILENCE_CLASS_*`` value).

    *server* is the MCP server the harness's own frame named (empty for a built-in),
    *tool_name* the tool it named, *kind* the frame's ACP ``kind``, and
    *network_servers* the servers this session declared with a ``url``. Every input
    is something the harness wrote, never the model's title.
    """
    if is_shell or backend not in ACP_BACKENDS_TOOL_SILENCE_TIMER:
        return SILENCE_CLASS_NONE
    bare = _bare_tool_name(tool_name) if tool_name else ""
    if server:
        if server == CORE_MCP_SERVER and bare in CORE_SUBAGENT_TOOLS:
            return SILENCE_CLASS_SUBAGENT
        if server in network_servers:
            return SILENCE_CLASS_MCP_NETWORK
        return SILENCE_CLASS_MCP_STDIO
    if is_unterminated_builtin(backend, server=server, tool_name=tool_name):
        return SILENCE_CLASS_NONE
    if bare in HARNESS_SUBAGENT_TOOLS:
        return SILENCE_CLASS_SUBAGENT
    if bare or kind in BUILTIN_SILENCE_KINDS:
        return SILENCE_CLASS_BUILTIN
    return SILENCE_CLASS_NONE


def silence_bound(settings: Any, silence_class: str) -> float:
    """Seconds of silence *silence_class* is allowed under *settings*, 0 for no timer.

    A class whose setting is 0 (or below) is off. Every bound is capped at
    ``tool_stall_hard_cap_secs``, the backstop no window outlasts.
    """
    key = SILENCE_BOUND_KEYS.get(silence_class)
    if key is None:
        return 0.0
    bound = float(getattr(settings, key, 0.0) or 0.0)
    if bound <= 0:
        return 0.0
    cap = float(getattr(settings, "tool_stall_hard_cap_secs", 0.0) or 0.0)
    return min(bound, cap) if cap > 0 else bound


@dataclass(frozen=True)
class SilentCall:
    """One in-flight call whose silence ran past its bound."""

    call_id: str
    state: ToolCallState
    silence: float
    bound: float

    def evidence(self) -> str:
        """The stall evidence the terminal event and the metric carry."""
        return (
            f"{EVIDENCE_SILENCE_TIMER}={self.state.silence_class}: no frame for this call "
            f"in {self.silence:.0f}s (bound {self.bound:.0f}s)"
        )


def call_silence(state: ToolCallState, now: float, parked_total: float) -> float:
    """Seconds *state* has gone without an attributable frame, consumer parks excluded.

    *parked_total* is the turn's banked consumer park now. A park after the call's
    last activity (a person reading an approval card, an IM send) is not the call's
    silence, so it is subtracted, as every other idle clock on the handle does.
    """
    since = max(state.last_activity_ts, state.dispatch_ts)
    return max(0.0, (now - since) - max(0.0, parked_total - state.activity_parked))


def stamp_activity(state: ToolCallState, at: float, parked_total: float) -> bool:
    """Record a frame attributable to *state* at *at*; True when it moved the clock.

    Only a later stamp moves it, and the park baseline moves with it, so a park the
    timer has already subtracted is never counted again.
    """
    if at <= max(state.last_activity_ts, state.dispatch_ts):
        return False
    state.last_activity_ts = at
    state.activity_parked = parked_total
    return True
