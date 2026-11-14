"""Session lifecycle spans: one short trace per live session (tracing RFC §5.8, phase 6).

A live session is one generation of a Crew session key in the session registry,
from ``metrics/sessions.record_session_started`` to its end record. Its lifecycle
events are short spans in a trace of their own::

    kirocrew.session.open           the root, zero length: a session joined the registry
      kirocrew.session.compaction   an automatic compaction between turns, its real length
      kirocrew.session.switch       an agent, conductor, model, workspace or effort switch
      kirocrew.session.adopt        another session took it over (the session tree edge)
      kirocrew.session.close        it left the registry; ``kirocrew.session.reset`` for a reset

Every span carries the session's ``session.id`` and ``kirocrew.session_key_hash``,
the same values its turns carry, so a backend's session view shows the lifecycle
trace beside the turn traces, and an ACP recycle (a new ACP session id under the
same key) keeps one ``session.id``. A worker, a fork or a channel-linked slot's
open span carries ``kirocrew.parent_session_key_hash`` and
``kirocrew.session.relation``, and links to the parent's open span when the
parent's session is live in this process.

**Ids.** The generation is the start time in microseconds, so a session resumed
after a restart (the same ACP session id) gets a new trace, not a reused one::

    trace   H(trace.session, session_key, generation)
    span    H(session.<event>, session_key, generation, n)

``n`` is 0 for open, close and reset (one per generation) and counts the repeatable
events (compaction, switch, adopt) from 1 in this process. Children parent to the
open span by id, as run anchors do (``runs.py``). shortcut: a session that was
already live when tracing was switched on has no open span, so its children name a
parent nobody wrote and its generation is unknown (``""``) until its next open.

Off (``provider.peek()`` is ``None``): every entry point returns at its first line.
No title, prompt or message text: names are fixed, values are closed vocabularies,
hashed keys and the backend's own ACP session id.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from kiro_crew.tracing import provider, runs

logger = logging.getLogger(__name__)

KIND = "session"
SPAN_PREFIX = "kirocrew.session."

EVENT_OPEN = "open"
EVENT_CLOSE = "close"
EVENT_RESET = "reset"
EVENT_COMPACTION = "compaction"
EVENT_SWITCH = "switch"
EVENT_ADOPT = "adopt"

#: ``kirocrew.session.relation``: why the open span names a parent.
RELATION_WORKER = "worker"
RELATION_FORK = "fork"
RELATION_LINKED = "linked"

#: ``kirocrew.session.switch_kind``: the four switch handlers' own kinds, plus the
#: Crew Mode migration's agent switch, which moves a root onto a new conductor.
SWITCH_KINDS = frozenset({"agent", "conductor", "model", "workspace", "reasoning_effort"})

#: What ``CompactionCoordinator._compact_session`` answers; anything else is ``other``.
COMPACTION_OUTCOMES = frozenset(
    {"ok", "reset", "failed", "busy", "absent", "unmanaged", "recycled", "cancelled", "unconfirmed"}
)
_COMPACTION_ERRORS = frozenset({"failed", "cancelled"})

#: The end reason the close span is named ``kirocrew.session.reset`` for.
_RESET_REASON = "reset"

_MAX_TRACKED = 4096


@dataclass
class _Generation:
    generation: str
    acp_session_id: str = ""
    counts: dict[str, int] = field(default_factory=dict)


_lock = threading.Lock()
#: The newest generation of each session key, kept after its close so a switch
#: recorded just after its reset still lands in that session's trace.
_generations: "OrderedDict[str, _Generation]" = OrderedDict()
#: ``session key -> (parent key, relation)`` noted by the dashboard before a claim.
_lineage: "OrderedDict[str, tuple[str, str]]" = OrderedDict()


def _bounded_put(table: OrderedDict, key: str, value: Any) -> None:
    table[key] = value
    table.move_to_end(key)
    while len(table) > _MAX_TRACKED:
        table.popitem(last=False)


def generation_of(started_at: float) -> str:
    """The generation id of a session that joined the registry at *started_at*."""
    return str(int(started_at * 1_000_000))


def _ids(state: provider.TraceState, key: str, gen: str, event: str, n: int) -> tuple[str, str]:
    from kiro_crew.tracing import ids

    ns = state.namespace
    return ids.trace_id(ns, KIND, key, gen), ids.span_id(ns, f"{KIND}.{event}", key, gen, n)


def _open_link(state: provider.TraceState, key: str) -> Any:
    """A link to *key*'s live open span, or ``None`` when this process has none."""
    with _lock:
        live = _generations.get(key)
    if live is None:
        return None
    return runs.link(*_ids(state, key, live.generation, EVENT_OPEN, 0))


def _attrs(state: provider.TraceState, key: str, acp: str, extra: dict[str, Any]) -> dict:
    from kiro_crew.tracing import correlation

    return correlation.clean(
        {
            **extra,
            "kirocrew.acp_session_id": acp,
            **correlation.session_attrs(state.hash_key, key, key),
        }
    )


def _start(
    state: provider.TraceState,
    key: str,
    event: str,
    attrs: dict[str, Any],
    *,
    name: str = "",
    links: tuple[Any, ...] = (),
    generation: "_Generation | None" = None,
) -> Any:
    """Start one lifecycle span of *key*'s *generation* (default: its newest); the caller ends it."""
    with _lock:
        live = generation if generation is not None else _generations.get(key)
        gen = live.generation if live is not None else ""
        acp = live.acp_session_id if live is not None else ""
        n = 0
        if event not in (EVENT_OPEN, EVENT_CLOSE, EVENT_RESET):
            if live is None:
                live = _Generation("")
                _bounded_put(_generations, key, live)
            n = live.counts[event] = live.counts.get(event, 0) + 1
    trace_id, span_id = _ids(state, key, gen, event, n)
    parent = None
    if event != EVENT_OPEN:
        parent = runs.parent_of(trace_id, _ids(state, key, gen, EVENT_OPEN, 0)[1])
    return provider.start_span(
        state,
        SPAN_PREFIX + (name or event),
        trace_id=trace_id,
        span_id=span_id,
        parent=parent,
        attributes=_attrs(state, key, acp, attrs),
        links=links,
    )


def note_lineage(
    session_key: str, *, creator: str = "", forked_from: str = "", slot_key: str = ""
) -> None:
    """Remember the parent *session_key*'s next open span names. Never raises.

    The dashboard calls this before it claims a session: a worker's creator wins,
    then a fork's source, then the dashboard slot a channel session is linked to
    (*slot_key* when it is not *session_key*).
    """
    if provider.peek() is None or not session_key:
        return
    if creator:
        parent, relation = creator, RELATION_WORKER
    elif forked_from:
        parent, relation = forked_from, RELATION_FORK
    elif slot_key and slot_key != session_key:
        parent, relation = slot_key, RELATION_LINKED
    else:
        return
    with _lock:
        _bounded_put(_lineage, session_key, (parent, relation))


def opened(session_key: str, source: object, started_at: float, *, harness: str = "") -> None:
    """``kirocrew.session.open``: *session_key* joined the registry at *started_at*."""
    state = provider.peek()
    if state is None or not session_key:
        return
    try:
        from kiro_crew.tracing import correlation, turns
        from kiro_crew.validation import bounded_session_id

        # Backend-authored, and kept until the entry is evicted: bounded first.
        acp = bounded_session_id(turns._chain_attr(source, ("session_id", "_session_id"))) or ""
        with _lock:
            _bounded_put(_generations, session_key, _Generation(generation_of(started_at), acp))
            parent, relation = _lineage.get(session_key, ("", ""))
        extra: dict[str, Any] = {"kirocrew.session.harness": harness}
        links: tuple[Any, ...] = ()
        if parent:
            extra["kirocrew.session.relation"] = relation
            if state.hash_key is not None:
                extra["kirocrew.parent_session_key_hash"] = correlation.keyed_hash(
                    state.hash_key, parent
                )
            link = _open_link(state, parent)
            links = (link,) if link is not None else ()
        _start(state, session_key, EVENT_OPEN, extra, links=links).end()
    except Exception:
        logger.debug("session open span failed", exc_info=True)


def closed(session_key: str, end_reason: str, started_at: float) -> None:
    """``kirocrew.session.close`` (or ``.reset``): the generation from *started_at* ended."""
    state = provider.peek()
    if state is None or not session_key:
        return
    try:
        gen = generation_of(started_at)
        with _lock:
            live = _generations.get(session_key)
            if live is None or live.generation != gen:
                # Opened before tracing was on: the start time still names it.
                _bounded_put(_generations, session_key, _Generation(gen))
        event = EVENT_RESET if end_reason == _RESET_REASON else EVENT_CLOSE
        _start(state, session_key, event, {"kirocrew.session.end_reason": end_reason}).end()
    except Exception:
        logger.debug("session close span failed", exc_info=True)


class Compaction:
    """An open :func:`compaction` block. Set :attr:`outcome` to how it ended."""

    outcome = ""


@contextmanager
def compaction(session_key: str) -> Iterator[Compaction]:
    """``kirocrew.session.compaction`` around one automatic compaction of *session_key*.

    For the compaction Crew starts between turns. One a backend runs inside a turn
    is a step of that turn, not a lifecycle event.
    """
    box = Compaction()
    state = provider.peek()
    span: Any = None
    if state is not None and session_key:
        try:
            span = _start(state, session_key, EVENT_COMPACTION, {})
        except Exception:
            logger.debug("session compaction span failed", exc_info=True)
    if span is None:
        yield box
        return
    try:
        yield box
    except BaseException as exc:
        if not box.outcome:
            box.outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        raise
    finally:
        _end_compaction(span, box.outcome)


def _end_compaction(span: Any, outcome: str) -> None:
    try:
        value = outcome if outcome in COMPACTION_OUTCOMES else "other"
        span.set_attribute("kirocrew.session.compaction_outcome", value)
        if value in _COMPACTION_ERRORS:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR))
        span.end()
    except Exception:
        logger.debug("session compaction span end failed", exc_info=True)


def current(session_key: str) -> "_Generation | None":
    """*session_key*'s newest generation, for a caller that records an event on it
    after an await that a successor can register in. ``None`` while tracing is off."""
    if provider.peek() is None or not session_key:
        return None
    with _lock:
        return _generations.get(session_key)


def switched(session_key: str, kind: str, *, generation: "_Generation | None" = None) -> None:
    """``kirocrew.session.switch``: *session_key*'s session moved to a new *kind* value.

    Recorded once the switch has landed on the session: after its reset, or after
    a live model switch kept it. *generation* (from :func:`current`, read before the
    reset) names the session the switch tore down; without it, the newest one.
    """
    state = provider.peek()
    if state is None or not session_key or kind not in SWITCH_KINDS:
        return
    try:
        _start(
            state,
            session_key,
            EVENT_SWITCH,
            {"kirocrew.session.switch_kind": kind},
            generation=generation,
        ).end()
    except Exception:
        logger.debug("session switch span failed", exc_info=True)


def adopted(session_key: str, parent_key: str) -> None:
    """``kirocrew.session.adopt``: *parent_key* now holds *session_key* in the session tree."""
    state = provider.peek()
    if state is None or not session_key or not parent_key:
        return
    try:
        from kiro_crew.tracing import correlation

        extra: dict[str, Any] = {}
        if state.hash_key is not None:
            extra["kirocrew.parent_session_key_hash"] = correlation.keyed_hash(
                state.hash_key, parent_key
            )
        link = _open_link(state, parent_key)
        links = (link,) if link is not None else ()
        _start(state, session_key, EVENT_ADOPT, extra, links=links).end()
    except Exception:
        logger.debug("session adopt span failed", exc_info=True)


def reset_for_testing() -> None:
    """Forget every generation and lineage note. Test isolation only."""
    with _lock:
        _generations.clear()
        _lineage.clear()
