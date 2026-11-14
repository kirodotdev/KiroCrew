"""The span tree of one ACP turn, opened where every model turn already passes.

Every surface reaches the model through one of two stream functions:
``AcpSessionProvider.stream`` (kiro, Kiro Agent) and ``AcpClient.stream_events``
(Claude and the direct clients). Both pass their event stream through
:func:`traced`, which hands it back untouched when tracing is off and otherwise
builds::

    invoke_agent <agent>          the turn; model and spend
      user.prompt                 marker: the prompt was sent
      chat [<model>]              the turn's model work, one aggregate span
      kirocrew.step ...           one per derived model call, never with spend
      execute_tool <tool> ...     one per tool call (see below)
      kirocrew.compaction ...     a compaction the backend ran inside the turn
      turn.complete               marker: the stop reason arrived

Approvals, steers and queue wait are steps a Crew call site reports
(``tracing/steps.py``); they hang under the same turn span.

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
from dataclasses import dataclass, field, replace
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
#: Steps inside the turn that are Crew's own, not GenAI operations.
SPAN_STEP = "kirocrew.step"
SPAN_COMPACTION = "kirocrew.compaction"

#: The ACP event kinds a turn's spans read, spelled out like the stop reasons
#: above; ``test_tracing_steps`` pins them to ``acp.types``.
EVENT_COMPLETE = "complete"
EVENT_TOOL_CALL = "tool_call"
EVENT_TEXT_CHUNK = "text_chunk"
EVENT_COMPACTION_STATUS = "compaction_status"
#: The compaction statuses that end one (``kirocrew.compaction.outcome``).
COMPACTION_RESULTS = frozenset({"completed", "failed"})

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
    #: The turn span's name in place of ``invoke_agent <agent>`` (a workflow call).
    name: str = ""
    #: Attributes on the turn span alone (a queued turn's wait), not on its children.
    span_attrs: dict[str, Any] = field(default_factory=dict)


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
    name: str = "",
    span_attrs: dict[str, Any] | None = None,
) -> contextvars.Token | None:
    """Make *turn_id* the id of the turns this task streams next.

    With no *join* the turn id is also the trace id. With a *join* (a
    ``runs.RunRef``) the turns sit in that run's trace under its span and
    carry its root's ``session.id`` (RFC R4, R6). *links* go on the turn span;
    *attrs* (correlation ids) on every span of the turn, *span_attrs* on the turn
    span only. *rollup_key* names the
    span whose roll-up the turn's spend joins, when that is not the span it hangs
    under (a sub-agent run moved onto a continuation trace). *name* replaces the
    turn span's ``invoke_agent <agent>`` name.

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
            name,
            dict(span_attrs or {}),
        )
    )


def adopt_root() -> None:
    """Make the turn root this task inherited its own, so its turns are the root's.

    A task copies its creator's context but streams as a stranger to the root,
    which records the task that bound it. Work the binding task hands to a new
    task and only awaits (an admitted cron dispatch) calls this first; the
    binding task streams nothing of its own after that.
    """
    root = _ROOT.get()
    task = _current_task()
    if root is not None and task is not None:
        root.task = task


def next_turn() -> None:
    """Start a new turn under the root bound in this task: same run, a fresh turn id.

    For an engine whose one root streams several distinct turns in one task (a
    cron run's agent sequence): without it each later turn would count as the
    next attempt of the first. No-op when no root is bound in this task. The
    root's own release still restores what was bound before it.
    """
    root = _ROOT.get()
    if root is not None and root.task is _current_task():
        _ROOT.set(replace(root, turn_id=uuid.uuid4().hex, attempts=0))


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
        #: The turn's spend attributes once it ends, for an ``on_end`` that sums them.
        self.spend: dict[str, Any] = {}
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
            (root.name if root is not None and root.name else "")
            or f"{OP_INVOKE_AGENT} {self.agent}".strip(),
            ids.KIND_TURN,
            parent,
            {
                **agent,
                **(root.span_attrs if root is not None else {}),
                "gen_ai.operation.name": OP_INVOKE_AGENT,
            },
            links=root.links if root is not None else (),
        )
        self.span_id = format(self.turn_span.get_span_context().span_id, "016x")
        self._marker(SPAN_USER_PROMPT, ids.KIND_USER_PROMPT, {})
        self.chat_span = self._start(
            OP_CHAT, ids.KIND_CHAT, self.turn_span, {"gen_ai.operation.name": OP_CHAT}
        )
        #: Spans numbered within the turn, by kind: derived steps, compactions, steers.
        self._ordinals: dict[str, int] = {}
        self._in_tools = False
        self._compaction: Any = None
        self._step = self._next_step()
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

    def numbered(self, name: str, kind: str, attrs: dict[str, Any]) -> tuple[Any, int]:
        """Start the turn's next span of *kind* under the turn span, and its number.

        The id is ``H(kind, turn_id, attempt, n)``, *n* counting that kind in this
        turn from 1. Spend is never put on one: it is the turn's (``chat``).
        """
        from kiro_crew.tracing import correlation, ids

        with _lock:
            n = self._ordinals[kind] = self._ordinals.get(kind, 0) + 1
        span = provider.start_span(
            self._state,
            name,
            trace_id=self.trace_id,
            span_id=ids.span_id(self._state.namespace, kind, self.turn_id, self.attempt, n),
            parent=self.turn_span,
            attributes={**self.common, **correlation.clean(attrs)},
        )
        return span, n

    def _next_step(self) -> Any:
        """Open the next derived model-call step (``kirocrew.step``)."""
        from kiro_crew.tracing import ids

        span, n = self.numbered(SPAN_STEP, ids.KIND_STEP, {"kirocrew.step.derived": True})
        span.set_attribute("kirocrew.step.index", n)
        return span

    def observe(self, event: Any) -> None:
        """Note one stream event: the terminal one, and the step boundaries in between.

        A step is derived where the dashboard's crew log derives one
        (``chat_turn/stream_chunks.py``): text after a tool group is the next model
        call, the only per-call boundary the stream exposes.
        """
        kind = getattr(event, "kind", "")
        if kind == EVENT_COMPLETE:
            self._complete_event = event
            return
        try:
            if kind == EVENT_TOOL_CALL:
                self._in_tools = True
            elif kind == EVENT_TEXT_CHUNK and self._in_tools:
                self._in_tools = False
                self._step.end()
                self._step = self._next_step()
            elif kind == EVENT_COMPACTION_STATUS and not getattr(event, "runtime_global", False):
                self._compaction_status(str(getattr(event, "text", "") or ""))
        except Exception:
            logger.debug("turn step trace failed", exc_info=True)

    def _compaction_status(self, status: str) -> None:
        """``kirocrew.compaction``: from ``started`` to its result, inside this turn.

        A result with no start seen (a backend that reports only the end) is a
        zero-length span at the result.
        """
        from kiro_crew.tracing import ids

        if status == "started" and self._compaction is None:
            self._compaction = self.numbered(SPAN_COMPACTION, ids.KIND_COMPACTION, {})[0]
        elif status in COMPACTION_RESULTS:
            span = self._compaction or self.numbered(SPAN_COMPACTION, ids.KIND_COMPACTION, {})[0]
            self._compaction = None
            span.set_attribute("kirocrew.compaction.outcome", status)
            if status == "failed":
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR))
            span.end()

    def finish(self, exc: BaseException | None) -> None:
        """End every span of the turn. *exc* is what ended the stream, if anything."""
        if self._ended:
            return
        self._ended = True
        try:
            self._finish(exc)
        except Exception:
            logger.debug("turn trace finish failed", exc_info=True)
        _run_after(self)

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
        self.spend = spend
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
        # The last model call has no tool group after it to close it; the turn's end does.
        self._step.end()
        if self._compaction is not None:  # its result never came
            self._compaction.end()
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
#: ``(scope, tool_call_id) -> (turn, span, role)``; ``span`` is ``None`` while built.
_open_tools: dict[tuple[str, str], tuple[TurnTrace, Any, str]] = {}
_ended_tools: OrderedDict[tuple[str, str], None] = OrderedDict()
#: Roll-ups of the child turns under each open spawn tool span, by its span id (R3).
_rollups: dict[str, runs.Rollup] = {}

#: The Crew tools whose open call another span needs to find, by role: a
#: spawn's children parent to it, a send's receiver and a workflow run link to
#: it. Matched on the tool name the harness reports, whatever prefix it adds
#: (``mcp_kirocrew_core_``, ``@server/``); the span itself still shows ``mcp``.
ROLE_SPAWN = "spawn"
ROLE_SEND = "send"
ROLE_WORKFLOW = "workflow"
_ROLE_TOOLS = (
    ("spawn_sub_agents", ROLE_SPAWN),
    ("spawn_run", ROLE_SPAWN),
    ("spawn_continue", ROLE_SPAWN),
    ("session_send", ROLE_SEND),
    ("session_broadcast", ROLE_SEND),
    ("workflow_run", ROLE_WORKFLOW),
)
#: What may sit just before a role tool's name when a harness prefixes it.
_PREFIX_ENDS = "_/:@."


def _tool_role(tool_name: str) -> str:
    """The role of the Crew tool *tool_name* names, or ``""``."""
    if not tool_name or len(tool_name) > _MAX_KEY_LEN:
        return ""
    for name, role in _ROLE_TOOLS:
        if tool_name == name:
            return role
        if tool_name.endswith(name) and tool_name[-len(name) - 1] in _PREFIX_ENDS:
            return role
    return ""


def open_turn(acp_session_id: str) -> TurnTrace | None:
    """The turn streaming on ACP session *acp_session_id* right now, if traced."""
    with _lock:
        return _open_turns.get(acp_session_id)


def open_turn_for(source: object) -> TurnTrace | None:
    """The turn streaming right now on *source*'s ACP session (a provider or client)."""
    return open_turn(_chain_attr(source, ("session_id", "_session_id")))


def open_turn_of_session(session_key: str) -> TurnTrace | None:
    """The turn streaming for Crew session *session_key* right now, if traced.

    shortcut: a scan of the open turns (bounded by ``_MAX_OPEN``); key them by
    session as well if a hot path ever needs this.
    """
    if not session_key:
        return None
    with _lock:
        for turn in _open_turns.values():
            if turn.session_key == session_key:
                return turn
    return None


def open_turn_of_subagent(agent_id: str) -> TurnTrace | None:
    """The turn sub-agent run *agent_id* is streaming right now, if traced (a scan, as above)."""
    if not agent_id:
        return None
    with _lock:
        for turn in _open_turns.values():
            if turn.common.get("kirocrew.subagent_id") == agent_id:
                return turn
    return None


def current_turn(session_key: str = "") -> TurnTrace | None:
    """The open turn of the root bound in this task, else the one open for *session_key*.

    A dashboard or channel turn decides its approvals in the task that streams
    it, so its own root finds it whichever session key its provider carries.
    """
    root = _ROOT.get()
    if root is not None and root.task is _current_task():
        with _lock:
            for turn in _open_turns.values():
                if turn._root is root:
                    return turn
    return open_turn_of_session(session_key)


def role_span(turn: TurnTrace, role: str) -> Any:
    """The one tool span of *role* open in *turn*; ``None`` with none or several open."""
    with _lock:
        found = [
            span
            for owner, span, held in _open_tools.values()
            if owner is turn and held == role and span is not None
        ]
    return found[0] if len(found) == 1 else None


def spawn_span_of(turn: TurnTrace) -> str:
    """The span id a sub-agent *turn* admits now parents to (RFC §5.4).

    The one spawn tool call open in the turn; with none, or with several open at
    once (which call admitted the child is not visible), the turn span itself.
    """
    span = role_span(turn, ROLE_SPAWN)
    if span is not None:
        return format(span.get_span_context().span_id, "016x")
    return turn.span_id


def _add_to_rollup(span_id: str, spend: dict[str, Any]) -> None:
    """Add a child turn's spend to its parent span's roll-up, while that span is open."""
    with _lock:
        rollup = _rollups.get(span_id)
        if rollup is not None:
            rollup.add(spend)


def open_rollup(span_id: str) -> None:
    """Start summing the spend of the turns that hang under *span_id* (a run anchor)."""
    with _lock:
        if len(_rollups) < _MAX_OPEN:
            _rollups[span_id] = runs.Rollup()


def close_rollup(span_id: str) -> runs.Rollup | None:
    """Stop summing under *span_id* and return what was summed, if it was open."""
    with _lock:
        return _rollups.pop(span_id, None)


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
        mine = [key for key, (owner, _span, _role) in _open_tools.items() if owner is turn]
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
            role = _tool_role(tool_name)
            _open_tools[key] = (turn, None, role)
        if dropped:
            logger.warning("tool span registry full: ending %d open tool spans", len(dropped))
            for _owner, stale, _role in dropped:
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
                _open_tools[key] = (turn, span, role)
                if role == ROLE_SPAWN and len(_rollups) < _MAX_OPEN:
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


def after_turn(acp_session_id: str, fn: Callable[[], None]) -> bool:
    """Run *fn* once the turn streaming on *acp_session_id* has ended its spans.

    ``False``, and *fn* is not kept, when no traced turn is streaming there. For a
    marker whose roll-up must include that turn's spend (``tracing/work.py``).
    """
    with _lock:
        turn = _open_turns.get(acp_session_id)
        if turn is None:
            return False
        turn.__dict__.setdefault("_after", []).append(fn)
    return True


def _run_after(turn: TurnTrace) -> None:
    # The turn left ``_open_turns`` at the start of its finish, under the same lock,
    # so nothing is added after this pop.
    with _lock:
        pending = turn.__dict__.pop("_after", ())
    for fn in pending:
        try:
            fn()
        except Exception:
            logger.debug("after-turn trace step failed", exc_info=True)
