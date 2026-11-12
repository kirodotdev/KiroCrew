"""The span tree of one ACP turn, opened where every model turn already passes.

Every surface reaches the model through one of two stream functions:
``AcpSessionProvider.stream`` (kiro, Kiro Agent) and ``AcpClient.stream_events``
(Claude and the direct clients). Both pass their event stream through
:func:`traced`, which hands it back untouched when tracing is off and otherwise
builds::

    invoke_agent <agent>          the turn; model and spend
      user.prompt                 marker: the prompt was sent
      chat [<model>]              the turn's model work, one aggregate span
      execute_tool <tool> ...     one per tool call (see below)
      turn.complete               marker: the stop reason arrived

**Trace id.** A dashboard turn binds its own turn id (:func:`bind_turn_root`)
before it streams, and that id is the trace id. The binding is honoured only
in the task that made it, so a background task spawned from inside the turn
(which inherits the context) roots its own trace instead of joining this one.
A turn nothing bound mints a random id: every model turn is traced either way.

**Tool spans** come from ``metrics/tool_calls.py``'s start and finish hooks,
which both frame parsers already call on the runtime's reader task. A turn
registers under its ACP session id, and the hook finds it there, so two
sessions that reuse one ``toolCallId`` never meet. A start for a call that
already ended is a ``session/load`` replay and is dropped.

Best-effort throughout: a failure here is logged and the turn goes on.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.tracing import provider, runs

logger = logging.getLogger(__name__)

#: Stop reasons of a turn the backend did not finish: a cancel, and the ones
#: the ACP layer synthesizes when it gives up on a turn (stale probe, turn
#: ceiling, and the whole ``error:`` family). Their spend may be partial: the
#: last metering frame can be cut with the turn. A backend-reported end
#: (``end_turn``, ``refusal``, ``max_tokens``) carries final usage. Spelled out
#: rather than imported, because ``acp.types`` would load the ACP package into
#: a module the metrics hooks import; ``test_tracing_turns`` pins them to it.
STOP_REASONS_UNFINISHED = frozenset({"cancelled", "stale_recover", "timeout"})
STOP_REASON_ERROR_PREFIX = "error:"


def _metering_finished(event: Any) -> bool:
    """Whether a terminal event's usage is the turn's final, whole spend."""
    if getattr(event, "synthetic_completion", False):
        return False
    reason = str(getattr(event, "stop_reason", "") or "")
    return reason not in STOP_REASONS_UNFINISHED and not reason.startswith(STOP_REASON_ERROR_PREFIX)


#: Span names (§3g of the tracing RFC).
OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"
SPAN_USER_PROMPT = "user.prompt"
SPAN_TURN_COMPLETE = "turn.complete"

#: The tool name every MCP-served call shows: MCP server and tool names are
#: user-defined and can reveal what someone works on, so they never ride a span.
MCP_TOOL_NAME = "mcp"

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
# Bounds, like ``tool_calls._MAX_OPEN_CALLS``: a registry that reaches one is
# holding frames whose end never came, and is dropped rather than grown.
_MAX_OPEN = 4096
_MAX_ENDED = 4096
#: Longest ACP session id or tool call id a registry keeps: the same bound
#: ``correlation`` puts on an id attribute.
_MAX_KEY_LEN = 256


@dataclass
class _TurnRoot:
    turn_id: str
    task: object
    attempts: int = 0
    #: The run this turn joins (``tracing/runs.py``); ``None`` roots the turn itself.
    join: Any = None
    #: Links on the turn span: the causes of a turn that roots itself.
    links: tuple[Any, ...] = ()
    #: Correlation attributes every span of the turn carries.
    attrs: dict[str, Any] = field(default_factory=dict)
    #: Called with the :class:`TurnTrace` once its spans have ended.
    on_end: Callable[[Any], None] | None = None
    #: The span whose roll-up this turn's spend joins; default: the span it hangs under.
    rollup_key: str = ""


_ROOT: contextvars.ContextVar[_TurnRoot | None] = contextvars.ContextVar(
    "kirocrew_trace_turn_root", default=None
)


def _current_task() -> object:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def bind_turn_root(
    turn_id: str,
    *,
    join: Any = None,
    links: tuple[Any, ...] = (),
    attrs: dict[str, Any] | None = None,
    on_end: Callable[[Any], None] | None = None,
    rollup_key: str = "",
) -> contextvars.Token | None:
    """Make *turn_id* the id of the turns this task streams next.

    With no *join* the turn id is also the trace id. With a *join* (a
    ``runs.RunRef``) the turns sit in that run's trace under its span and
    carry its root's ``session.id`` (RFC R4, R6). *links* go on the turn span;
    *attrs* (correlation ids) on every span of the turn. *rollup_key* names the
    span whose roll-up the turn's spend joins, when that is not the span it hangs
    under (a sub-agent run moved onto a continuation trace).

    Bound whatever the switch says (one context-variable write), so the first
    turn of a process, which streams while the switch is still being read,
    keeps its own id.
    """
    if not turn_id:
        return None
    return _ROOT.set(
        _TurnRoot(
            turn_id,
            _current_task(),
            0,
            join,
            tuple(links),
            dict(attrs or {}),
            on_end,
            rollup_key,
        )
    )


def release_turn_root(token: contextvars.Token | None) -> None:
    """Undo :func:`bind_turn_root`, restoring an outer turn's binding."""
    if token is None:
        return
    try:
        _ROOT.reset(token)
    except ValueError:  # reset from another context: nothing to restore
        pass


def _chain_attr(source: object, names: tuple[str, ...]) -> str:
    """First non-empty string attribute among *names* down the wrapper chain."""
    from kiro_crew.wrapper_chain import wrapper_chain

    try:
        for name in names:
            for node in wrapper_chain(source):
                value = getattr(node, name, "")
                if isinstance(value, str) and value:
                    return value
    except Exception:
        logger.debug("trace attribute read failed", exc_info=True)
    return ""


def _usage_attrs(usage: Any, model: str, resolved: str, complete: bool) -> dict[str, Any]:
    """Model and spend, set on both ``invoke_agent`` and ``chat`` (RFC §7.2)."""
    attrs: dict[str, Any] = {
        "gen_ai.request.model": model,
        "gen_ai.response.model": resolved,
        "kirocrew.usage.complete": complete,
    }
    if usage is None:
        return attrs
    for key, field_name in (
        ("gen_ai.usage.input_tokens", "input_tokens"),
        ("gen_ai.usage.output_tokens", "output_tokens"),
        ("kirocrew.usage.credits", "credits"),
        ("kirocrew.usage.cost_usd", "cost_usd"),
        ("kirocrew.usage.model_calls", "num_turns"),
    ):
        value = getattr(usage, field_name, 0)
        if isinstance(value, (int, float)) and value > 0:
            attrs[key] = value
    return attrs


def _langfuse_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """The three Langfuse keys, on the ``chat`` span only (RFC decision c).

    Credits and tokens are never mixed in one ``usage_details``: Langfuse sums
    the buckets into ``total``. No harness reports both today; tokens win.
    """
    import json

    out: dict[str, Any] = {"langfuse.observation.type": "generation"}
    tokens = {
        k: attrs[f"gen_ai.usage.{k}_tokens"]
        for k in ("input", "output")
        if f"gen_ai.usage.{k}_tokens" in attrs
    }
    if tokens:
        out["langfuse.observation.usage_details"] = json.dumps(tokens, sort_keys=True)
    elif "kirocrew.usage.credits" in attrs:
        out["langfuse.observation.usage_details"] = json.dumps(
            {"credits": attrs["kirocrew.usage.credits"]}
        )
    if "kirocrew.usage.cost_usd" in attrs:
        out["langfuse.observation.cost_details"] = json.dumps(
            {"total": attrs["kirocrew.usage.cost_usd"]}
        )
    return out


class TurnTrace:
    """One ACP turn's spans. Built by :func:`start_turn`; never raises."""

    def __init__(self, state: provider.TraceState, source: object) -> None:
        from kiro_crew.tracing import correlation, ids

        self._state = state
        self._source = source
        self._complete_event: Any = None
        self._ended = False
        self._stats_before = _prompt_stats(source)
        root = _ROOT.get()
        if root is not None and root.task is _current_task():
            root.attempts += 1
            self.turn_id, self.attempt = root.turn_id, root.attempts
        else:
            root = None
            self.turn_id, self.attempt = uuid.uuid4().hex, 1
        self._root = root
        join = root.join if root is not None else None
        self.acp_session_id = _chain_attr(source, ("session_id", "_session_id"))
        self.session_key = _chain_attr(source, ("_session_key",))
        if join is not None:
            # R4/R6: in the run's trace, under its span, wearing its root's session.
            self.trace_id, self.parent_span_id = join.trace_id, join.span_id
            self.root_session_key = join.root_session_key or self.session_key
            self.started = join.started
            parent = runs.parent_of(join.trace_id, join.span_id)
        else:
            self.trace_id = ids.turn_trace_id(state.namespace, self.turn_id)
            self.parent_span_id, self.root_session_key = "", self.session_key
            self.started, parent = time.time(), None
        self.common = correlation.clean(
            {
                **(root.attrs if root is not None else {}),
                "kirocrew.turn_id": self.turn_id,
                "kirocrew.acp_session_id": self.acp_session_id,
                "kirocrew.attempt": self.attempt,
                **correlation.session_attrs(
                    state.hash_key, self.session_key, self.root_session_key
                ),
            }
        )
        agent = correlation.clean(
            {"gen_ai.agent.name": _chain_attr(source, ("active_agent", "_agent"))}
        )
        self.agent = str(agent.get("gen_ai.agent.name", ""))
        self.turn_span = self._start(
            f"{OP_INVOKE_AGENT} {self.agent}".strip(),
            ids.KIND_TURN,
            parent,
            {**agent, "gen_ai.operation.name": OP_INVOKE_AGENT},
            links=root.links if root is not None else (),
        )
        self.span_id = format(self.turn_span.get_span_context().span_id, "016x")
        self._marker(SPAN_USER_PROMPT, ids.KIND_USER_PROMPT, {})
        self.chat_span = self._start(
            OP_CHAT, ids.KIND_CHAT, self.turn_span, {"gen_ai.operation.name": OP_CHAT}
        )
        _register(self)

    def _start(
        self, name: str, kind: str, parent: Any, attrs: dict[str, Any], links: tuple = ()
    ) -> Any:
        from kiro_crew.tracing import correlation, ids

        return provider.start_span(
            self._state,
            name,
            trace_id=self.trace_id,
            span_id=ids.span_id(self._state.namespace, kind, self.turn_id, self.attempt),
            parent=parent,
            attributes={**self.common, **correlation.clean(attrs)},
            links=links,
        )

    def _marker(self, name: str, kind: str, attrs: dict[str, Any]) -> None:
        self._start(name, kind, self.turn_span, attrs).end()

    def observe(self, event: Any) -> None:
        """Note one stream event; only the terminal one carries anything."""
        if getattr(event, "kind", "") == "complete":
            self._complete_event = event

    def finish(self, exc: BaseException | None) -> None:
        """End every span of the turn. *exc* is what ended the stream, if anything."""
        if self._ended:
            return
        self._ended = True
        try:
            self._finish(exc)
        except Exception:
            logger.debug("turn trace finish failed", exc_info=True)

    def _finish(self, exc: BaseException | None) -> None:
        from opentelemetry.trace import Status, StatusCode

        from kiro_crew.tracing import correlation, ids

        _unregister(self)
        event = self._complete_event
        complete = event is not None
        usage = getattr(event, "usage", None) if complete else self._partial_usage()
        model = _chain_attr(self._source, ("_model",))
        resolved = _chain_attr(self._source, ("_resolved_model_id",))
        # A cancelled or abandoned turn still ends in EVENT_COMPLETE, but its
        # spend may be partial (see STOP_REASONS_UNFINISHED).
        usage_complete = complete and _metering_finished(event)
        spend = correlation.clean(
            _usage_attrs(
                usage, model, "" if resolved.lower() == "auto" else resolved, usage_complete
            )
        )
        if complete:
            stop = correlation.clean({"kirocrew.stop_reason": getattr(event, "stop_reason", "")})
            self._marker(SPAN_TURN_COMPLETE, ids.KIND_TURN_COMPLETE, stop)
        chat_model = spend.get("gen_ai.response.model") or spend.get("gen_ai.request.model")
        if chat_model and str(chat_model).lower() != "auto":
            self.chat_span.update_name(f"{OP_CHAT} {chat_model}")
        self.chat_span.set_attributes(
            {**spend, "kirocrew.chat.aggregate": True, **correlation.clean(_langfuse_attrs(spend))}
        )
        self.turn_span.set_attributes(spend)
        if exc is not None and not isinstance(exc, (GeneratorExit, asyncio.CancelledError)):
            # The class name only: an exception message can carry content.
            error = {"error.type": type(exc).__name__}
            for span in (self.chat_span, self.turn_span):
                span.set_attributes(error)
                span.set_status(Status(StatusCode.ERROR))
        _end_open_tools(self)
        self.chat_span.end()
        self.turn_span.end()
        # A child placed in a continuation segment still adds to its spawn span.
        rollup_key = (self._root.rollup_key if self._root is not None else "") or (
            self.parent_span_id
        )
        if rollup_key:
            _add_to_rollup(rollup_key, spend)
        if self._root is not None and self._root.on_end is not None:
            self._root.on_end(self)

    def _partial_usage(self) -> Any:
        """What a turn that never completed had billed so far, if readable.

        Only from the stats object this attempt installed: each prompt replaces
        ``last_prompt_stats`` with a fresh one, so the object seen at the start
        still being there means the turn ended before its prompt went out, and
        reading it would bill the previous turn's spend a second time.
        """
        stats = _prompt_stats(self._source)
        if stats is None or stats is self._stats_before:
            return None
        try:
            return stats.to_turn_usage()
        except Exception:
            logger.debug("partial turn usage read failed", exc_info=True)
            return None

    async def wrap(self, events: AsyncGenerator[Any, None]) -> AsyncIterator[Any]:
        """Yield *events* unchanged, ending the spans at the terminal event.

        They end BEFORE that event is handed on: the consumer's own settling
        (billing, transcript writes) happens after it and is not model time.
        A stream that ends any other way ends them in ``finally``.
        """
        error: BaseException | None = None
        try:
            async with aclosing(events) as inner:
                async for event in inner:
                    self.observe(event)
                    if self._complete_event is event:
                        self.finish(None)
                    yield event
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.finish(error)


def _prompt_stats(source: object) -> Any:
    """The provider chain's live ``last_prompt_stats``, or ``None``."""
    from kiro_crew.wrapper_chain import wrapper_chain

    try:
        for node in wrapper_chain(source):
            stats = getattr(node, "last_prompt_stats", None)
            if stats is not None and hasattr(stats, "to_turn_usage"):
                return stats
    except Exception:
        logger.debug("prompt stats read failed", exc_info=True)
    return None


def start_turn(source: object) -> TurnTrace | None:
    """Open the spans of the turn *source* is about to stream; ``None`` when off."""
    state = provider.peek()
    if state is None:
        return None
    try:
        return TurnTrace(state, source)
    except Exception:
        logger.debug("turn trace start failed", exc_info=True)
        return None


def traced(source: object, events: Any) -> Any:
    """*events*, traced as one turn of *source*; the same object when tracing is off.

    Never awaits and never reads the switch: a turn adds no suspension point
    before its prompt is written. The switch is read on a worker thread by
    ``provider.watch_config`` at boot and after each config write; a process
    that never registers it (the ``kirocrew chat`` CLI) is not traced.
    """
    turn = start_turn(source)
    return events if turn is None else turn.wrap(events)


# ── tool spans ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_open_turns: dict[str, TurnTrace] = {}
#: ``(scope, tool_call_id) -> (turn, span, is_spawn)``; ``span`` is ``None`` while built.
_open_tools: dict[tuple[str, str], tuple[TurnTrace, Any, bool]] = {}
_ended_tools: OrderedDict[tuple[str, str], None] = OrderedDict()
#: Roll-ups of the child turns under each open spawn tool span, by its span id (R3).
_rollups: dict[str, runs.Rollup] = {}

#: The Crew tools whose call admits sub-agents. Matched on the tool name the
#: harness reports, whatever prefix it adds (``mcp_kirocrew_core_``, ``@server/``),
#: and used only to find a child's parent span: the span itself still shows ``mcp``.
_SPAWN_TOOLS = ("spawn_sub_agents", "spawn_run", "spawn_continue")
#: What may sit just before a spawn tool's name when a harness prefixes it.
_PREFIX_ENDS = "_/:@."


def _is_spawn_tool(tool_name: str) -> bool:
    if not tool_name or len(tool_name) > _MAX_KEY_LEN:
        return False
    for name in _SPAWN_TOOLS:
        if tool_name == name:
            return True
        if tool_name.endswith(name) and tool_name[-len(name) - 1] in _PREFIX_ENDS:
            return True
    return False


def open_turn(acp_session_id: str) -> TurnTrace | None:
    """The turn streaming on ACP session *acp_session_id* right now, if traced."""
    with _lock:
        return _open_turns.get(acp_session_id)


def spawn_span_of(turn: TurnTrace) -> str:
    """The span id a sub-agent *turn* admits now parents to (RFC §5.4).

    The one spawn tool call open in the turn; with none, or with several open at
    once (which call admitted the child is not visible), the turn span itself.
    """
    with _lock:
        spawns = [
            span
            for owner, span, is_spawn in _open_tools.values()
            if owner is turn and is_spawn and span is not None
        ]
    if len(spawns) == 1:
        return format(spawns[0].get_span_context().span_id, "016x")
    return turn.span_id


def _add_to_rollup(span_id: str, spend: dict[str, Any]) -> None:
    """Add a child turn's spend to its parent span's roll-up, while that span is open."""
    with _lock:
        rollup = _rollups.get(span_id)
        if rollup is not None:
            rollup.add(spend)


def _register(turn: TurnTrace) -> None:
    if not 0 < len(turn.acp_session_id) <= _MAX_KEY_LEN:
        return
    with _lock:
        if len(_open_turns) >= _MAX_OPEN:
            _open_turns.clear()
        _open_turns[turn.acp_session_id] = turn


def _unregister(turn: TurnTrace) -> None:
    with _lock:
        if _open_turns.get(turn.acp_session_id) is turn:
            del _open_turns[turn.acp_session_id]


def _end_open_tools(turn: TurnTrace) -> None:
    """End the tool spans a turn left open: their terminal frame never came."""
    with _lock:
        mine = [key for key, (owner, _span, _spawn) in _open_tools.items() if owner is turn]
        spans = [_open_tools.pop(key)[1] for key in mine]
    for span in spans:
        if span is not None:  # a start that failed after reserving its slot
            _end_tool_span(span)


def _bounded(scope: str, tool_call_id: str) -> bool:
    """Whether both registry key halves are present and short enough to retain.

    The ids are backend-chosen, so the registries' entry caps alone would not
    bound their memory: a call with an oversized id is simply not traced.
    """
    return (
        isinstance(scope, str)
        and isinstance(tool_call_id, str)
        and 0 < len(scope) <= _MAX_KEY_LEN
        and 0 < len(tool_call_id) <= _MAX_KEY_LEN
    )


def _tool_name(kind: str, mcp_server_name: str | None, tool_name: str) -> str:
    """The name a tool span shows: built-in names only, ``mcp`` for MCP calls.

    A harness that does not mark MCP calls can still hand over an MCP tool's
    name, so a name spelled like one (``mcp_…``, ``@server/tool``, ``a__b``) is
    treated as MCP too. With no usable name the tool's kind stands in.
    """
    if mcp_server_name:
        return MCP_TOOL_NAME
    name = tool_name or ""
    if name.lower().startswith("mcp") or any(sep in name for sep in ("@", "/", "__")):
        return MCP_TOOL_NAME
    return name or kind


def tool_started(
    scope: str,
    tool_call_id: str,
    *,
    kind: str,
    mcp_server_name: str | None = None,
    tool_name: str = "",
) -> None:
    """Open an ``execute_tool`` span under the turn open on ACP session *scope*."""
    # The open turn decides, on the tracer it started with: a settings write that
    # re-reads the switch must not cut this turn's tool spans.
    if not _open_turns or not _bounded(scope, tool_call_id):
        return
    try:
        from kiro_crew.tracing import correlation, ids

        key = (scope, tool_call_id)
        with _lock:
            turn = _open_turns.get(scope)
            if turn is None or key in _open_tools or key in _ended_tools:
                return
            dropped = list(_open_tools.values()) if len(_open_tools) >= _MAX_OPEN else []
            if dropped:
                _open_tools.clear()
            is_spawn = _is_spawn_tool(tool_name)
            _open_tools[key] = (turn, None, is_spawn)
        if dropped:
            logger.warning("tool span registry full: ending %d open tool spans", len(dropped))
            for _owner, stale, _spawn in dropped:
                if stale is not None:
                    _end_tool_span(stale)
        name = correlation.clean({"gen_ai.tool.name": _tool_name(kind, mcp_server_name, tool_name)})
        label = name.get("gen_ai.tool.name", kind)
        span = provider.start_span(
            turn._state,
            f"{OP_EXECUTE_TOOL} {label}",
            trace_id=turn.trace_id,
            span_id=ids.span_id(turn._state.namespace, ids.KIND_TOOL, scope, tool_call_id),
            parent=turn.turn_span,
            attributes={
                **turn.common,
                **correlation.clean(
                    {
                        **name,
                        "gen_ai.operation.name": OP_EXECUTE_TOOL,
                        "kirocrew.tool_kind": kind,
                        "kirocrew.tool_call_id": tool_call_id,
                    }
                ),
            },
        )
        with _lock:
            if key in _open_tools:
                _open_tools[key] = (turn, span, is_spawn)
                if is_spawn and len(_rollups) < _MAX_OPEN:
                    _rollups[format(span.get_span_context().span_id, "016x")] = runs.Rollup()
                return
        span.end()  # the turn ended while the span was being built
    except Exception:
        logger.debug("tool span start failed", exc_info=True)


def tool_finished(scope: str, tool_call_id: str, *, status: str | None) -> None:
    """End the tool's span on its terminal status; a non-terminal one is ignored."""
    if status not in _TERMINAL or not _open_tools or not _bounded(scope, tool_call_id):
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        key = (scope, tool_call_id)
        with _lock:
            entry = _open_tools.pop(key, None)
            _ended_tools[key] = None
            while len(_ended_tools) > _MAX_ENDED:
                _ended_tools.popitem(last=False)
        if entry is None or entry[1] is None:
            return
        span = entry[1]
        span.set_attribute("kirocrew.tool.outcome", status)
        if status == "failed":
            span.set_status(Status(StatusCode.ERROR))
        _end_tool_span(span)
    except Exception:
        logger.debug("tool span finish failed", exc_info=True)


def _end_tool_span(span: Any) -> None:
    """End a tool span, first writing the roll-up of the child turns that ended under it.

    A blocking ``spawn_sub_agents`` ends after its children, so its span carries
    their totals (R3); an async ``spawn_run`` ends first and carries none.
    shortcut: direct children only; a grandchild's spend reaches the child's own
    spawn span, not this one. Upgrade when a run kind needs whole-tree totals.
    """
    with _lock:
        rollup = _rollups.pop(format(span.get_span_context().span_id, "016x"), None)
    totals = rollup.attrs() if rollup is not None else {}
    if totals:
        from kiro_crew.tracing import correlation

        span.set_attributes(correlation.clean(totals))
    span.end()


def reset_for_testing() -> None:
    """Forget every open turn and tool. Test isolation only."""
    with _lock:
        _open_turns.clear()
        _open_tools.clear()
        _ended_tools.clear()
        _rollups.clear()
