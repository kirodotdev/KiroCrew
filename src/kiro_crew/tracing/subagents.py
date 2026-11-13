"""Sub-agent turns in their parent's trace, and the completion turn that joins it (RFC §5.4).

Three moments, each a call from the code that already handles it:

1. **Admission** (:func:`pin_origin`, from the spawn gate). The spawn arrives as a
   tool call inside the parent's turn, so the parent's open turn and its open
   spawn tool span are known here and nowhere later. Their ids are kept under
   the child's agent id.
2. **The child's run** (:func:`bind_child`, around the run). Each run is a new
   turn id under the pinned spawn span, in the parent's trace and wearing the
   parent trace's ``session.id``. A blocking child ends inside its spawn span;
   an async one runs on after that span has ended, which OTel allows.
   ``spawn_continue`` and a retry sit under the origin of the run they continue.
3. **The completion turn** (:func:`note_delivery` where a result is queued for
   the parent, :func:`delivered` where the queue drains it,
   :func:`completion_binding` where the turn binds). One finished run as the
   only cause joins that run's trace under the parent turn's span (R4); a digest
   of several roots itself with one link per run.

In memory only, bounded: a restart loses pins, and an unpinned child roots its
own trace. shortcut: origins do not survive a restart; persist them on the
durable spawn row if restart-spanning runs need to nest.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

KIND = "subagent"
_MAX = 4096
_MAX_CAUSES = 64


@dataclass(frozen=True)
class Origin:
    """Where a child's turns go: the parent turn's trace and spans, by hex id."""

    trace_id: str
    turn_span_id: str
    spawn_span_id: str
    root_session_key: str
    parent_session_key: str
    started: float

    def run(self, span_id: str) -> runs.RunRef:
        """The run root for a span of this origin: children under the spawn span."""
        return runs.RunRef(
            KIND,
            (self.trace_id, self.spawn_span_id),
            self.trace_id,
            span_id,
            self.root_session_key,
            self.started,
        )


_lock = threading.Lock()
_origins: "OrderedDict[str, Origin]" = OrderedDict()
#: Each child's last ended turn ``(trace_id, span_id)``: what a digest links to.
_last_turns: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
#: Queued completion entry id -> the agent ids whose results it delivers.
_deliveries: "OrderedDict[str, tuple[str, ...]]" = OrderedDict()
#: Turn id -> the agent ids that turn delivered, for a retry of it (:func:`requeue`).
_taken: "OrderedDict[str, tuple[str, ...]]" = OrderedDict()


def _put(table: OrderedDict, key: str, value: Any) -> None:
    with _lock:
        table[key] = value
        table.move_to_end(key)
        while len(table) > _MAX:
            table.popitem(last=False)


def pin_origin(agent_id: str, sessions: Any, parent_session_key: str) -> None:
    """Remember where *agent_id*'s turns go, read from the parent's open turn.

    Call from inside the spawn tool call, on its first admission pass. A parent
    with no traced turn open (tracing off, a cron or script caller) pins nothing.
    Never raises.
    """
    if not agent_id or not parent_session_key or provider.peek() is None:
        return
    try:
        from kiro_crew.crew_log.resolve import unit_for_session_key

        turn = turns.open_turn(unit_for_session_key(sessions, parent_session_key))
        if turn is None:
            return
        origin = Origin(
            turn.trace_id,
            turn.span_id,
            turns.spawn_span_of(turn),
            turn.root_session_key,
            turn.session_key or parent_session_key,
            turn.started,
        )
        with _lock:
            if agent_id in _origins:  # a re-entry keeps the pin it was accepted with
                return
        _put(_origins, agent_id, origin)
    except Exception:
        logger.debug("subagent trace origin pin failed", exc_info=True)


def _origin_for(agent_id: str, conversation_key: str = "") -> Origin | None:
    """*agent_id*'s origin; a continuation takes the origin of the run it continues."""
    with _lock:
        first = conversation_key.removeprefix("subagent:")
        return (_origins.get(first) if first else None) or _origins.get(agent_id)


def bind_child(info: Any) -> contextvars.Token | None:
    """Bind one run of sub-agent *info* as a new turn under its origin. Never raises.

    Release with ``turns.release_turn_root``. With no origin the run's turns root
    their own trace and still carry ``kirocrew.subagent_id``.
    """
    state = provider.peek()
    if state is None:
        return None
    try:
        from kiro_crew.tracing import correlation

        agent_id = str(getattr(info, "id", "") or "")
        origin = _origin_for(agent_id, str(getattr(info, "conversation_key", "") or ""))
        attrs: dict[str, Any] = {"kirocrew.subagent_id": agent_id}
        join, rollup_key = None, ""
        if origin is not None:
            # A continuation runs under the origin of the run it continues, so its
            # completion turn must find that same origin under the new run's id.
            _put(_origins, agent_id, origin)
            join = runs.segment(state, origin.run(origin.spawn_span_id), KIND)
            rollup_key = origin.spawn_span_id
            if state.hash_key is not None:
                attrs["kirocrew.parent_session_key_hash"] = correlation.keyed_hash(
                    state.hash_key, origin.parent_session_key
                )

        def _ended(turn: turns.TurnTrace) -> None:
            _put(_last_turns, agent_id, (turn.trace_id, turn.span_id))

        return turns.bind_turn_root(
            uuid.uuid4().hex, join=join, attrs=attrs, on_end=_ended, rollup_key=rollup_key
        )
    except Exception:
        logger.debug("subagent trace bind failed", exc_info=True)
        return None


def note_delivery(queue_id: str | None, agent_ids: Iterable[str]) -> None:
    """Remember that queued entry *queue_id* delivers these runs' results."""
    if not queue_id or provider.peek() is None:
        return
    ids = tuple(str(a) for a in agent_ids if a)[:_MAX_CAUSES]
    if ids:
        _put(_deliveries, str(queue_id), ids)


class _Causes:
    """A one-shot holder: the first turn to read it takes the causes."""

    def __init__(self, agent_ids: tuple[str, ...]) -> None:
        self.agent_ids = agent_ids


_CAUSES: contextvars.ContextVar[_Causes | None] = contextvars.ContextVar(
    "kirocrew_trace_completion_causes", default=None
)


@contextmanager
def delivered(queue_ids: Iterable[Any]) -> Iterator[None]:
    """Around starting the turn that drains these queue entries: hand it their causes.

    The turn's task copies the context at creation, so the turn reads them with
    :func:`take_causes`. Set even when nothing was found, so a turn started from
    inside an earlier turn's task never inherits that turn's untaken causes.
    """
    found: list[str] = []
    with _lock:
        for queue_id in queue_ids:
            if isinstance(queue_id, str):
                found.extend(_deliveries.pop(queue_id, ()))
    token = _CAUSES.set(_Causes(tuple(dict.fromkeys(found))) if found else None)
    try:
        yield
    finally:
        _CAUSES.reset(token)


def take_causes(turn_id: str = "") -> tuple[str, ...]:
    """The causes handed to this turn, once: a nested turn of the same task gets none.

    With *turn_id* they are also kept under it, so a retry of the turn that
    :func:`requeue` queues delivers the same runs.
    """
    holder = _CAUSES.get()
    if holder is None:
        return ()
    causes, holder.agent_ids = holder.agent_ids, ()
    if causes and turn_id:
        _put(_taken, turn_id, causes)
    return causes


def requeue(turn_id: str, queue_id: str | None) -> None:
    """A retry of turn *turn_id* was queued as *queue_id*: it delivers the same runs."""
    with _lock:
        causes = _taken.get(turn_id) if turn_id else None
    if causes:
        note_delivery(queue_id, causes)


def completion_binding(causes: tuple[str, ...]) -> tuple[runs.RunRef | None, tuple[Any, ...]]:
    """``(join, links)`` for a turn that delivers these runs' results (R4).

    One run with a known origin: join its trace under the parent turn's span (or the
    run's live continuation, once it rolled over). Two
    or more: no join, one link per run to its last turn (or its spawn span when no
    turn of it ended in this process).
    """
    state = provider.peek()
    if state is None or not causes:
        return None, ()
    known = [(aid, origin) for aid in causes if (origin := _origin_for(aid)) is not None]
    if len(causes) == 1:
        if not known:
            return None, ()
        origin = known[0][1]
        return runs.segment(state, origin.run(origin.turn_span_id), KIND), ()
    from kiro_crew.tracing import correlation

    links = []
    for aid, origin in known:
        with _lock:
            target = _last_turns.get(aid) or (origin.trace_id, origin.spawn_span_id)
        links.append(runs.link(*target, correlation.clean({"kirocrew.subagent_id": aid})))
    return None, tuple(links)


def reset_for_testing() -> None:
    """Forget every pin, ended turn and delivery. Test isolation only."""
    with _lock:
        _origins.clear()
        _last_turns.clear()
        _deliveries.clear()
        _taken.clear()
