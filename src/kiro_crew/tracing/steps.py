"""Steps inside a turn that a Crew call site reports (tracing RFC §5.3).

The turn's own stream gives the derived model-call steps and in-turn compaction
(``turns.TurnTrace.observe``). The rest happen in Crew, so the code that already
handles each one calls in here:

- **Approvals.** :func:`approval_opened` where a prompt is put to someone and
  :func:`approval_closed` where its answer lands: one ``kirocrew.approval`` span
  under the asking turn, with ``kirocrew.approval.wait_ms``,
  ``kirocrew.approval.decided_by`` (``person``, ``policy`` when the host decided
  without an answer, ``timeout``) and ``kirocrew.approval.outcome``. A spawn
  approval sits under the parent's spawn tool span instead. A tool the harness
  auto-approves never reaches Crew, so it has no approval span.
- **Steers.** :func:`steered`: a zero-length ``kirocrew.steer`` in the turn it went
  into, carrying ``kirocrew.steer.mode`` and never the text.
- **Sends and queue wait.** :func:`sending` around a ``session_send`` or broadcast
  delivery names the sender's tool span (else its turn span); :func:`note_queued`
  times a queue entry from its append, and :func:`drained` hands the turn that
  drains entries their wait and senders. A top-level turn's binding takes them once
  (:func:`take_queued`): one link per sender and ``kirocrew.queue.wait_ms`` (the
  oldest entry's) on its turn span.

Best-effort and bounded like the turn registries. Off, each call returns at the
switch read; :func:`drained` and :func:`sending` still set their hand-over, so a
turn never inherits one an earlier turn left untaken.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

SPAN_APPROVAL = "kirocrew.approval"
SPAN_STEER = "kirocrew.steer"

DECIDED_PERSON = "person"
DECIDED_POLICY = "policy"
DECIDED_TIMEOUT = "timeout"
STEER_INTERRUPT = "interrupt"
STEER_FOLLOW_UP = "follow_up"
_STEER_MODES = frozenset({STEER_INTERRUPT, STEER_FOLLOW_UP})

#: The queued-entry ``meta`` key naming the span that sent it, ``<trace id>:<span id>``.
META_SENDER = "trace_sender"
#: Spelled out rather than imported from ``constants`` (pinned by ``test_tracing_steps``).
_CAUSE_TIMEOUT = "approval_timeout"
_MAX = 4096
_MAX_LINKS = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass
class Approval:
    """One open approval span: release it with :func:`approval_closed`."""

    span: Any
    started: float


def decided_by(*, host: bool = False, cause: str = "") -> str:
    """Who decided: the window ran out, the host decided alone, or a person answered."""
    if cause == _CAUSE_TIMEOUT:
        return DECIDED_TIMEOUT
    return DECIDED_POLICY if host or cause else DECIDED_PERSON


@dataclass(frozen=True)
class Asked:
    """When a prompt became answerable: (monotonic, epoch ns), from :func:`asked_now`."""

    monotonic: float
    epoch_ns: int


def asked_now() -> Asked:
    """The moment a prompt can first be answered, for an :func:`approval_opened` made later.

    For a call site that publishes the prompt and then awaits something
    cancellable before it can open and close the span as a pair.
    """
    return Asked(time.monotonic(), time.time_ns())


def approval_opened(
    approval_id: str,
    *,
    session_key: str = "",
    tool_call_id: str = "",
    subagent_id: str = "",
    spawn: bool = False,
    since: Asked | None = None,
) -> Approval | None:
    """Open the span of a prompt put to someone now, or at *since*. Never raises.

    It hangs under the turn the root bound in this task streams, else the one
    open for *session_key*; under sub-agent run *subagent_id*'s open turn; or,
    for its *spawn* approval, under the spawn tool span of the parent turn that
    asked. ``None`` when tracing is off or that turn is not traced.
    """
    state = provider.peek()
    if state is None or not approval_id:
        return None
    try:
        from kiro_crew.tracing import correlation, ids

        if spawn:
            from kiro_crew.tracing import subagents

            origin = subagents._origin_for(subagent_id)
            if origin is None:
                return None
            trace_id, scope = origin.trace_id, subagent_id
            parent = runs.parent_of(origin.trace_id, origin.spawn_span_id)
            common: dict[str, Any] = {
                "kirocrew.subagent_id": subagent_id,
                **correlation.session_attrs(
                    state.hash_key, origin.parent_session_key, origin.root_session_key
                ),
            }
        else:
            turn = (
                turns.open_turn_of_subagent(subagent_id)
                if subagent_id
                else turns.current_turn(session_key)
            )
            if turn is None:
                return None
            # The turn's tracer, which a settings write cannot swap under it.
            state, trace_id, scope = turn._state, turn.trace_id, turn.acp_session_id
            parent, common = turn.turn_span, turn.common
        asked = since or asked_now()
        span = provider.start_span(
            state,
            SPAN_APPROVAL,
            trace_id=trace_id,
            span_id=ids.span_id(
                state.namespace, ids.KIND_APPROVAL, scope, tool_call_id or approval_id
            ),
            parent=parent,
            attributes={**common, **correlation.clean({"kirocrew.tool_call_id": tool_call_id})},
            start_time=asked.epoch_ns,
        )
        return Approval(span, asked.monotonic)
    except Exception:
        logger.debug("approval trace open failed", exc_info=True)
        return None


def approval_closed(approval: Approval | None, *, approved: bool, by: str) -> None:
    """End *approval*'s span with its wait, who decided (*by*) and the outcome."""
    if approval is None:
        return
    try:
        from kiro_crew.tracing import correlation

        approval.span.set_attributes(
            correlation.clean(
                {
                    "kirocrew.approval.wait_ms": int((time.monotonic() - approval.started) * 1000),
                    "kirocrew.approval.decided_by": by,
                    "kirocrew.approval.outcome": "approved" if approved else "rejected",
                }
            )
        )
        approval.span.end(end_time=time.time_ns())  # the same clock as its start
    except Exception:
        logger.debug("approval trace close failed", exc_info=True)


def steered(mode: str, *, source: object = None, subagent_id: str = "") -> None:
    """A steer of *mode* went into the turn open on *source*'s ACP session, or run *subagent_id*'s."""
    if mode not in _STEER_MODES or provider.peek() is None:
        return
    try:
        from kiro_crew.tracing import ids

        turn = (
            turns.open_turn_of_subagent(subagent_id) if subagent_id else turns.open_turn_for(source)
        )
        if turn is not None:
            span = turn.numbered(SPAN_STEER, ids.KIND_STEER, {"kirocrew.steer.mode": mode})[0]
            span.end(end_time=span.start_time)  # a point: when it went in
    except Exception:
        logger.debug("steer trace failed", exc_info=True)


# ── sends and queue wait ──────────────────────────────────────────────────

_lock = threading.Lock()
#: Queue entry id -> monotonic time it was appended.
_queued_at: "OrderedDict[str, float]" = OrderedDict()


@dataclass
class _Queued:
    """A one-shot hand-over: the first top-level turn to read it takes it."""

    senders: tuple[str, ...]
    wait_ms: int | None = None


_QUEUED: contextvars.ContextVar[_Queued | None] = contextvars.ContextVar(
    "kirocrew_trace_queued", default=None
)


def note_queued(queue_id: str) -> None:
    """Queue entry *queue_id* was appended now."""
    if not queue_id or provider.peek() is None:
        return
    with _lock:
        _queued_at[queue_id] = time.monotonic()
        while len(_queued_at) > _MAX:
            _queued_at.popitem(last=False)


def _sender_ref(session_key: str) -> str:
    """``<trace>:<span>`` of the send tool span open in *session_key*'s turn, else that turn."""
    turn = turns.open_turn_of_session(session_key)
    if turn is None:
        return ""
    context = (turns.role_span(turn, turns.ROLE_SEND) or turn.turn_span).get_span_context()
    return f"{context.trace_id:032x}:{context.span_id:016x}"


@contextmanager
def sending(session_key: str) -> Iterator[dict[str, str]]:
    """Around one peer delivery from *session_key*: yields the queue ``meta`` naming the sender.

    A turn the delivery starts at once copies this context, so it takes the
    sender from here; a queued one reads it back from its entry (:func:`drained`).
    """
    ref = ""
    if session_key and provider.peek() is not None:
        try:
            ref = _sender_ref(session_key)
        except Exception:
            logger.debug("sender trace read failed", exc_info=True)
    token = _QUEUED.set(_Queued((ref,)) if ref else None)
    try:
        yield {META_SENDER: ref} if ref else {}
    finally:
        _QUEUED.reset(token)


@contextmanager
def drained(entries: Iterable[Any]) -> Iterator[None]:
    """Around starting the turn that drains these queue entries: their wait and senders."""
    now, started, senders = time.monotonic(), None, []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        with _lock:
            at = _queued_at.pop(str(entry.get("id") or ""), None)
        if at is not None and (started is None or at < started):
            started = at
        meta = entry.get("meta")
        ref = meta.get(META_SENDER) if isinstance(meta, dict) else None
        if isinstance(ref, str) and ref:
            senders.append(ref)
    wait_ms = int((now - started) * 1000) if started is not None else None
    holder = (
        _Queued(tuple(dict.fromkeys(senders))[:_MAX_LINKS], wait_ms)
        if senders or wait_ms is not None
        else None
    )
    token = _QUEUED.set(holder)
    try:
        yield
    finally:
        _QUEUED.reset(token)


def _link(ref: str) -> Any:
    """A link to the span *ref* names, or ``None`` when it is not two hex ids."""
    from kiro_crew.tracing import ids

    trace_id, _, span_id = ref.partition(":")
    if not ids.is_hex32(trace_id) or len(span_id) != 16 or not set(span_id) <= _HEX_DIGITS:
        return None
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return runs.link(trace_id, span_id)


def take_queued() -> tuple[tuple[Any, ...], dict[str, Any]]:
    """``(links, turn-span attrs)`` handed to this turn, taken once; empty when off."""
    holder = _QUEUED.get()
    if holder is None or provider.peek() is None:
        return (), {}
    senders, wait_ms = holder.senders, holder.wait_ms
    holder.senders, holder.wait_ms = (), None
    try:
        links = tuple(link for link in map(_link, senders) if link is not None)
    except Exception:
        logger.debug("sender trace link failed", exc_info=True)
        links = ()
    return links, ({"kirocrew.queue.wait_ms": wait_ms} if wait_ms is not None else {})


def reset_for_testing() -> None:
    """Forget every queued entry's time. Test isolation only."""
    with _lock:
        _queued_at.clear()
