"""Conductor rounds (conductor mode or a Bot) as traces, and a span per ledger write (RFC §5.5).

With ``telemetry.traces_conductor_mode = "run"`` (the default) one round of a conductor's
board is one trace::

    conductor_mode round N         anchor: the round's root, zero length
      invoke_agent <conductor>     each conductor turn of the round
        kirocrew.work.<action>     each ledger write the turn makes
      work_item it_xxxxxxxx        anchor per item open in the round
        invoke_agent <worker>      each turn of the item's worker
          kirocrew.work.report     the worker's report
        work_item outcome <state>  at the close: verdict, fails, roll-up
      conductor_mode outcome <status> when the board moves on: ``closed`` with no item
                                   open, ``advanced`` with items carried over

A round is keyed ``(conductor, goal key, round)``: the goal key is the header's
``goal_id``, or ``<generation>.<goal_items_base>`` on a header from before it. Each
round's anchor links to the round before it. An anchor is emitted only by the ledger
write that brings its key into being: the board's first write, a goal write that
changes the goal or the round (which also re-emits the items still open under the new
round and ends the old one), and an item's create. So a restart re-derives every id and
re-emits nothing, and a turn finds its anchor by reading the ledger (:func:`binding`).
A round past its span budget or six days old (``round_started_at`` on the header) moves
to a ``(cont N)`` trace (``runs.segment``), and each item's anchor is emitted again under
it, once per process, on first use. A round's roll-up is keyed by its first anchor and an
item's by the item itself, so an outcome sums the whole run, rounds and rollovers
included.

Outcome markers wait for the acting turn to end (``turns.after_turn``), so the spend of
the turn that closes an item or moves the round on is in their roll-up.

shortcuts: anchors are not emitted for a board that tracing was off for when its round
began; its turns then parent to an anchor nobody wrote, until the next round. A worker
turn still streaming when its item closes adds nothing to that item's roll-up. A goal's
last round gets no round outcome, and keeps its roll-up entry, until a goal write moves
the board on: the ledger has no "goal finished" action to end it on. An item
that never closes (a purged board) keeps its roll-up entry until the shared cap. An item
open across a restart, or created while tracing was off, has no roll-up, so its outcome
carries no totals. Continuations are counted per process: a restart on a round already
past its age budget emits its ``(cont 2)`` anchor (same ids) again; persist the live
continuation on the header if duplicate anchors matter.

With ``"turn"`` there are no anchors: every turn roots itself, and the write spans
sit in their acting turn.

A write span sits in the turn streaming on the acting unit when that turn is in the
write's round trace. Otherwise it goes under its item's or round's anchor, linked to the
acting turn when there is one (the turn that opens a round was bound before it, and a
person's steer or a remote report has none). With no round it is in the acting turn,
else in a one-span trace of its own. Its attributes are ids, closed-vocabulary values and counts only: never the
goal, a title, acceptance, decision, summary or artifact text.

**Patrol turns.** A turn the conductor's work-ledger watch delivers runs inside
:func:`patrol_turn`. It takes one link per report span (and per
``kirocrew.work.report_undelivered`` event) written on its board since the last such
turn (:func:`answered_links`), so a wake that answers a batch of reports names each one.
The conductor's own ``work_ledger_read`` in a turn puts the board's health on that turn's
span as counts (:func:`note_board_health`). shortcut: a wake steered into a turn already
running adds no links, since a span's links are fixed at its start; the reports wait for
the next patrol turn.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from kiro_crew.tracing import provider, runs, turns

logger = logging.getLogger(__name__)

#: Item anchors already emitted in a continuation trace by this process (bounded).
_cont_items: "OrderedDict[str, None]" = OrderedDict()
_cont_lock = threading.Lock()
_MAX_CONT_ITEMS = 4096

#: Report spans a board's next patrol turn answers, by conductor ledger key (bounded).
_answerable: "OrderedDict[str, list[tuple[str, str]]]" = OrderedDict()
_MAX_BOARDS = 4096
#: Links one patrol turn takes at most: the newest reports win.
MAX_ANSWERED_LINKS = 64


class _Patrol:
    """The mark of a work-ledger watch's turn: a one-shot holder the first turn to bind
    claims. It survives a hop to a new task (fair admission runs the turn in one), and a
    queued successor, started after the watch's turn bound, finds it already taken."""

    __slots__ = ("taken",)

    def __init__(self) -> None:
        self.taken = False


_PATROL: contextvars.ContextVar[_Patrol | None] = contextvars.ContextVar(
    "kirocrew_trace_patrol_turn", default=None
)

KIND_ROUND = "conductor_mode"
KIND_ITEM = "work_item"
#: Where a write span's name starts: ``kirocrew.work.<action>``.
SPAN_PREFIX = "kirocrew.work."

#: How a round ends: its last open item closed, or the round moved on with items open.
STATUS_CLOSED = "closed"
STATUS_ADVANCED = "advanced"

#: The actions whose write can bring a round into being (the board's bootstrap, a goal).
_OPENING = frozenset({"goal", "create"})
_OPEN = "open"


class _NotRead:
    """The header before a write was not read (tracing off, or not an opening action)."""


NOT_READ: Any = _NotRead()


@dataclass(frozen=True)
class Round:
    """One round of a board, as its ledger header names it."""

    conductor: str
    goal_key: str
    goal_id: str
    number: int
    generation: str
    #: When the round began (epoch seconds; 0 when the header predates the stamp),
    #: for the age budget of R5.
    started: float = 0.0

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.conductor, self.goal_key, str(self.number))


def round_of(conductor: str, header: Any) -> Round | None:
    """The round *header* (a ``ConductorRecord``) is in, or ``None`` with no header."""
    if header is None or isinstance(header, _NotRead) or not conductor:
        return None
    generation = str(getattr(header, "generation", "") or "")
    goal_id = str(getattr(header, "goal_id", "") or "")
    base = getattr(header, "goal_items_base", 0)
    return Round(
        conductor,
        goal_id or f"{generation}.{base}",
        goal_id,
        int(getattr(header, "round", 0) or 0),
        generation,
        _epoch(getattr(header, "round_started_at", "")),
    )


def _epoch(stamp: Any) -> float:
    """An ISO stamp as epoch seconds; 0 for a missing or unreadable one."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(stamp)).timestamp() if stamp else 0.0
    except ValueError:
        return 0.0


def _root_session(conductor: str) -> str:
    """The conductor's Crew session key from its ledger key (the dashboard prefix put back)."""
    return conductor if ":" in conductor else f"dashboard:{conductor}"


def _round_ref(state: provider.TraceState, rnd: Round) -> runs.RunRef:
    """The round's place in its trace: the ids ``runs.anchor`` gave it, re-derived."""
    from kiro_crew.tracing import ids

    ns = state.namespace
    return runs.RunRef(
        KIND_ROUND,
        rnd.key,
        ids.trace_id(ns, KIND_ROUND, *rnd.key, 1),
        ids.span_id(ns, KIND_ROUND, *rnd.key, 1),
        _root_session(rnd.conductor),
        rnd.started,
    )


def _item_rollup(state: provider.TraceState, rnd: Round, item_id: str) -> str:
    """The key an item's roll-up is kept under: one per item for its whole life, so an
    item open across a round change or a rollover sums every turn it took."""
    from kiro_crew.tracing import ids

    return ids.span_id(state.namespace, "work_item.rollup", rnd.conductor, rnd.generation, item_id)


def _item_ref(
    state: provider.TraceState, rnd: Round, item_id: str, live: runs.RunRef | None = None
) -> runs.RunRef:
    """An item's anchor in *rnd*'s trace, or in the round's continuation *live*. Keyed by
    the round too: an item open across a round change has an anchor in each round."""
    from kiro_crew.tracing import ids

    round_ref = live or _round_ref(state, rnd)
    key = (item_id, rnd.generation, rnd.goal_key, str(rnd.number))
    return runs.RunRef(
        KIND_ITEM,
        key,
        round_ref.trace_id,
        ids.span_id(state.namespace, KIND_ITEM, *key, round_ref.cont),
        _root_session(rnd.conductor),
        round_ref.started,
        cont=round_ref.cont,
    )


def _live_round(state: provider.TraceState, rnd: Round) -> runs.RunRef:
    """Where the round's next span goes: its anchor, or its live continuation (R5)."""
    return runs.segment(state, _round_ref(state, rnd), f"conductor_mode round {rnd.number}")


def _live_item(state: provider.TraceState, rnd: Round, item_id: str, root_kind: str) -> runs.RunRef:
    """The item's anchor in the round's live trace, emitted there first when that is a
    continuation this process has not put it in yet."""
    live = _live_round(state, rnd)
    ref = _item_ref(state, rnd, item_id, live)
    if live.cont > 1:
        with _cont_lock:
            fresh = ref.span_id not in _cont_items
            _cont_items[ref.span_id] = None
            while len(_cont_items) > _MAX_CONT_ITEMS:
                _cont_items.popitem(last=False)
        if fresh:
            _emit_anchor(state, ref, live, item_id, _board_attrs(state, rnd, root_kind))
    return ref


def _board_attrs(state: provider.TraceState, rnd: Round, root_kind: str) -> dict[str, Any]:
    from kiro_crew.tracing import correlation

    out: dict[str, Any] = {
        "kirocrew.work.goal_id": rnd.goal_id,
        "kirocrew.work.round": rnd.number,
        "kirocrew.crew.root_kind": root_kind,
    }
    if state.hash_key is not None:
        out["kirocrew.work.conductor_hash"] = correlation.keyed_hash(state.hash_key, rnd.conductor)
    return out


def _item_id(item: Any) -> str:
    """The item's id when it has an id's shape (it names a span), else ``""``.

    *item* is a ``WorkItem`` or the id itself.
    """
    from kiro_crew.tracing import correlation

    value = item if isinstance(item, str) else getattr(item, "item_id", None)
    found = correlation.clean({"kirocrew.work.item_id": value})
    return str(found.get("kirocrew.work.item_id", ""))


def _emit_anchor(
    state: provider.TraceState,
    ref: runs.RunRef,
    round_ref: runs.RunRef,
    item_id: str,
    attrs: dict[str, Any],
) -> None:
    provider.start_span(
        state,
        f"{KIND_ITEM} {item_id}",
        trace_id=ref.trace_id,
        span_id=ref.span_id,
        parent=runs.parent_of(round_ref.trace_id, round_ref.span_id),
        attributes=runs._run_attrs(state, ref, {**attrs, "kirocrew.work.item_id": item_id}),
    ).end()


def _emit_item(state: provider.TraceState, rnd: Round, item_id: str, attrs: dict[str, Any]) -> None:
    """A new item's anchor, in the round's live trace; its roll-up keyed by its first anchor."""
    live = _live_round(state, rnd)
    ref = _item_ref(state, rnd, item_id, live)
    if live.cont > 1:
        with _cont_lock:
            _cont_items[ref.span_id] = None
    _emit_anchor(state, ref, live, item_id, attrs)


def _after_acting_turn(unit: str, fn: Callable[[], None]) -> None:
    """Run *fn* once the turn streaming on *unit* has added its spend; now when none is."""
    if not (unit and turns.after_turn(unit, fn)):
        fn()


def _open_round(
    state: provider.TraceState,
    unit: str,
    rnd: Round,
    before: Round | None,
    root_kind: str,
    open_ids: list,
) -> None:
    """A new round: its anchor (linked to the last), its open items, the last one's end."""
    attrs = _board_attrs(state, rnd, root_kind)
    links: tuple[Any, ...] = ()
    if before is not None:
        prev = _live_round(state, before)
        links = (runs.link(prev.trace_id, prev.span_id),)
    ref = runs.anchor(
        state,
        KIND_ROUND,
        f"conductor_mode round {rnd.number}",
        rnd.key,
        root_session_key=_root_session(rnd.conductor),
        attrs=attrs,
        links=links,
    )
    turns.open_rollup(ref.span_id)
    for item_id in open_ids:
        _emit_item(state, rnd, item_id, attrs)
    if before is None:
        return
    # The round ends once, here: an item closing leaves room for another in the round.
    status = STATUS_ADVANCED if open_ids else STATUS_CLOSED
    old = _board_attrs(state, before, root_kind)

    def end() -> None:
        runs.outcome(
            state,
            _live_round(state, before),
            status,
            outcome_key=str(rnd.number),
            rollup=turns.close_rollup(_round_ref(state, before).span_id),
            attrs=old,
        )

    _after_acting_turn(unit, end)


def _enum(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else ""


def _write_attrs(action: str, actor: str, item: Any, header: Any) -> dict[str, Any]:
    """A write's attributes: ids, closed-vocabulary values and counts, nothing else."""
    from kiro_crew import work_vocab

    holder = item if item is not None else header
    pr = getattr(item, "pr", None)
    fails = getattr(item, "fails", None)
    return {
        "kirocrew.work.action": action,
        "kirocrew.work.actor": _enum(actor, work_vocab.WORK_ACTORS),
        "kirocrew.work.item_id": _item_id(item),
        "kirocrew.work.round": getattr(holder, "round", None),
        "kirocrew.work.generation": getattr(header, "generation", None),
        "kirocrew.work.goal_id": getattr(header, "goal_id", None),
        "kirocrew.work.state": _enum(getattr(item, "state", None), work_vocab.WORK_ITEM_STATES),
        "kirocrew.work.verdict": _enum(getattr(item, "verdict", None), work_vocab.WORK_VERDICTS),
        "kirocrew.work.fails": fails if isinstance(fails, int) else None,
        "kirocrew.work.status": (
            _enum(getattr(item, "status", None), work_vocab.WORK_WORKER_STATUSES)
            if action == "report"
            else ""
        ),
        "kirocrew.work.reason": (
            _enum(getattr(item, "reason", None), work_vocab.WORK_BLOCKED_REASONS)
            if action == "report"
            else ""
        ),
        "kirocrew.work.pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
    }


def _write_span(
    state: provider.TraceState,
    unit: str,
    conductor: str,
    rnd: Round | None,
    action: str,
    actor: str,
    item: Any,
    header: Any,
    event_id: str,
    root_kind: str,
) -> None:
    """``kirocrew.work.<action>``: in the acting turn, else under its anchor, else alone."""
    from kiro_crew.tracing import correlation, ids

    item_id = _item_id(item)
    if action == "goal":
        parts: tuple[Any, ...] = (
            "work.goal",
            conductor,
            getattr(header, "generation", ""),
            getattr(header, "goal_version", 0),
        )
    else:
        parts = ("work", conductor, item_id, action, event_id)
    attrs = correlation.clean(_write_attrs(action, actor, item, header))
    turn = turns.open_turn(unit) if unit else None
    target = None
    if rnd is not None:
        target = _live_item(state, rnd, item_id, root_kind) if item_id else _live_round(state, rnd)
    common: Mapping[str, Any]
    links: tuple[Any, ...] = ()
    if turn is not None and (target is None or turn.trace_id == target.trace_id):
        trace_id, parent, common = turn.trace_id, turn.turn_span, turn.common
    elif target is not None:
        trace_id, parent = target.trace_id, runs.parent_of(target.trace_id, target.span_id)
        common = runs._run_attrs(state, target, {})
        if turn is not None:
            links = (runs.link(turn.trace_id, turn.span_id),)
    else:
        trace_id, parent = ids.trace_id(state.namespace, *parts), None
        common = (
            correlation.session_attrs(
                state.hash_key, _root_session(conductor), _root_session(conductor)
            )
            if conductor
            else {}
        )
    span_id = ids.span_id(state.namespace, *parts)
    provider.start_span(
        state,
        SPAN_PREFIX + action,
        trace_id=trace_id,
        span_id=span_id,
        parent=parent,
        attributes={**common, **attrs},
        links=links,
    ).end()
    if action in _ANSWERABLE and conductor:
        _note_answerable(conductor, trace_id, span_id)


#: The writes a patrol turn answers with a link.
ACTION_UNDELIVERED = "report_undelivered"
_ANSWERABLE = frozenset({"report", ACTION_UNDELIVERED})


def _note_answerable(conductor: str, trace_id: str, span_id: str) -> None:
    with _cont_lock:
        pending = _answerable.setdefault(conductor, [])
        pending.append((trace_id, span_id))
        del pending[:-MAX_ANSWERED_LINKS]
        _answerable.move_to_end(conductor)
        while len(_answerable) > _MAX_BOARDS:
            _answerable.popitem(last=False)


def _recorded(
    state: provider.TraceState,
    unit: str,
    conductor: str,
    action: str,
    actor: str,
    item: Any,
    header: Any,
    event_id: str,
    before: Any,
    root_kind: str,
) -> None:
    from kiro_crew import work_ledger

    run = provider.conductor_mode_shape() == provider.CONDUCTOR_MODE_RUN
    rnd = round_of(conductor, header)
    if run and rnd is not None:
        prev = round_of(conductor, before)
        item_id = _item_id(item)
        if (
            action in _OPENING
            and not isinstance(before, _NotRead)
            and (prev is None or prev.key != rnd.key)
        ):
            open_ids = [
                found
                for listed in work_ledger.list_work_items(conductor)
                if getattr(listed, "state", "") == _OPEN and (found := _item_id(listed))
            ]
            _open_round(state, unit, rnd, prev, root_kind, open_ids)
        elif action == "create" and item_id:
            _emit_item(state, rnd, item_id, _board_attrs(state, rnd, root_kind))
        if action == "create" and item_id:  # the roll-up it keeps for its whole life
            turns.open_rollup(_item_rollup(state, rnd, item_id))
    _write_span(
        state,
        unit,
        conductor,
        rnd if run else None,
        action,
        actor,
        item,
        header,
        event_id,
        root_kind,
    )
    if run and rnd is not None and action == "close":
        _close_item(state, unit, rnd, item, event_id, root_kind)


def _close_item(
    state: provider.TraceState, unit: str, rnd: Round, item: Any, event_id: str, root_kind: str
) -> None:
    """``work_item outcome <state>``, once the closing turn has added its spend."""
    from kiro_crew import work_vocab

    item_id = _item_id(item)
    status = _enum(getattr(item, "state", None), work_vocab.WORK_ITEM_STATES)
    if not item_id or not status:
        return
    write = _write_attrs("close", "", item, None)
    attrs = {
        **_board_attrs(state, rnd, root_kind),
        **{
            k: write[k]
            for k in ("kirocrew.work.item_id", "kirocrew.work.verdict", "kirocrew.work.fails")
        },
    }

    def end() -> None:
        runs.outcome(
            state,
            _live_item(state, rnd, item_id, root_kind),
            status,
            outcome_key=event_id,
            rollup=turns.close_rollup(_item_rollup(state, rnd, item_id)),
            attrs=attrs,
        )

    _after_acting_turn(unit, end)


async def header_before(conductor: str, action: str) -> Any:
    """The board's header before a write that may open a round; :data:`NOT_READ` otherwise.

    Read only while tracing is on in ``run`` mode, for ``goal`` and ``create``: their
    write may change the round, which the anchors need to know. Never raises.
    """
    if (
        action not in _OPENING
        or provider.peek() is None
        or provider.conductor_mode_shape() != provider.CONDUCTOR_MODE_RUN
    ):
        return NOT_READ
    from kiro_crew import work_ledger

    try:
        return await asyncio.to_thread(work_ledger.read_conductor, conductor)
    except Exception:
        logger.debug("trace header read failed", exc_info=True)
        return NOT_READ


async def recorded(
    unit: str,
    conductor: str,
    action: str,
    *,
    actor: str,
    item: Any = None,
    header: Any = None,
    event_id: Any = None,
    before: Any = NOT_READ,
    root_kind: Callable[[], str] | None = None,
) -> None:
    """Trace one ledger write whose crew-log entry landed. Never raises.

    *unit* is the acting session's unit (``""`` when no turn of this host made the
    write); *item* and *header* are the COMMITTED records; *before* is what
    :func:`header_before` read; *root_kind* answers ``conductor_mode`` or ``mate`` for the
    board, on the loop. Nothing happens while tracing is off.
    """
    from kiro_crew import work_vocab

    state = provider.peek()
    if state is None or action not in work_vocab.WORK_ACTIONS:
        return
    try:
        kind = root_kind() if root_kind is not None else ""
        await asyncio.to_thread(
            _recorded,
            state,
            unit,
            conductor,
            action,
            actor,
            item,
            header,
            str(event_id or ""),
            before,
            kind,
        )
    except Exception:
        logger.debug("work ledger trace failed", exc_info=True)


def binding(
    conductor_slot: str, item: tuple[str, str, int] | None, root_kind: str = ""
) -> tuple[runs.RunRef | None, str]:
    """``(join, rollup key)`` for a turn on a board: its item's anchor, or its round's.

    *item* is the ``(conductor, item id, round)`` a worker is bound to; without it the
    turn is the conductor's own, on *conductor_slot*. ``(None, "")`` in ``turn`` mode,
    off a board, or while tracing is off. Reads disk: run it on a worker thread.
    """
    state = provider.peek()
    if state is None or provider.conductor_mode_shape() != provider.CONDUCTOR_MODE_RUN:
        return None, ""
    from kiro_crew import session_ledger, work_ledger

    try:
        conductor = item[0] if item is not None else session_ledger.ledger_key(conductor_slot)
        rnd = round_of(conductor, work_ledger.read_conductor(conductor))
        if rnd is None:
            return None, ""
        if item is not None:
            item_id = _item_id(item[1])
            if not item_id:
                return None, ""
            live = _live_item(state, rnd, item_id, root_kind)
            return live, _item_rollup(state, rnd, item_id)
        return _live_round(state, rnd), _round_ref(state, rnd).span_id
    except Exception:
        logger.debug("trace board binding failed", exc_info=True)
        return None, ""


def reset_for_testing() -> None:
    """Forget which continuations hold which item anchors, and pending report links.
    Test isolation only."""
    with _cont_lock:
        _cont_items.clear()
        _answerable.clear()


@contextmanager
def patrol_turn(active: bool = True) -> Iterator[None]:
    """Around starting a turn the conductor's work-ledger watch delivered."""
    token = _PATROL.set(_Patrol() if active else None)
    try:
        yield
    finally:
        _PATROL.reset(token)


def in_patrol_turn() -> bool:
    """Whether a turn bound now would be the watch's turn: marked, and not yet taken."""
    marked = _PATROL.get()
    return marked is not None and not marked.taken


def _take_patrol() -> bool:
    """Claim the mark for the turn binding now; only the first turn to bind gets it."""
    marked = _PATROL.get()
    with _cont_lock:
        if marked is None or marked.taken:
            return False
        marked.taken = True
    return True


def end_patrol_turn() -> None:
    """At a turn's queue hand-off: spend a mark the turn never claimed (it ended before
    binding, or bound with tracing off), so the turn it hands off to cannot take it."""
    _take_patrol()


def answered_links(conductor_slot: str) -> tuple[Any, ...]:
    """One link per report written on *conductor_slot*'s board since its last patrol
    turn, for a patrol turn only; taken once."""
    if not conductor_slot or provider.peek() is None or not _take_patrol():
        return ()
    from kiro_crew import session_ledger

    with _cont_lock:
        pending = _answerable.pop(session_ledger.ledger_key(conductor_slot), [])
    return tuple(runs.link(trace_id, span_id) for trace_id, span_id in pending)


def _undelivered(state: provider.TraceState, conductor: str, item_id: str, at: str) -> None:
    from kiro_crew import work_ledger

    header = work_ledger.read_conductor(conductor)
    rnd = round_of(conductor, header)
    run = provider.conductor_mode_shape() == provider.CONDUCTOR_MODE_RUN
    _write_span(
        state,
        "",
        conductor,
        rnd if run else None,
        ACTION_UNDELIVERED,
        "worker",
        item_id,
        header,
        at,
        "",
    )


async def report_undelivered(noted: Mapping[str, Any] | None) -> None:
    """A worker's report never reached its item (``work_ledger.note_undelivered``'s
    record): a ``kirocrew.work.report_undelivered`` span under the item, which the
    conductor's next patrol turn links to. Never raises."""
    state = provider.peek()
    if state is None or not noted:
        return
    conductor = str(noted.get("conductor_slot_key") or "")
    item_id = _item_id(str(noted.get("item_id") or ""))
    if not conductor or not item_id:
        return
    try:
        await asyncio.to_thread(_undelivered, state, conductor, item_id, str(noted.get("at") or ""))
    except Exception:
        logger.debug("undelivered report trace failed", exc_info=True)


def note_board_health(sessions: Any, conductor: str, rows: Sequence[Mapping[str, Any]]) -> None:
    """Put the board's health on the turn reading it: counts of the read's flags.

    *rows* are the read's item rows with their derived flags, the whole board. Never
    raises; nothing happens while tracing is off or with no traced turn reading.
    """
    if provider.peek() is None or not conductor:
        return
    try:
        from kiro_crew.crew_log.resolve import unit_for_session_key
        from kiro_crew.tracing import correlation

        turn = turns.open_turn(unit_for_session_key(sessions, conductor))
        if turn is None:
            return
        counts = {
            "kirocrew.work.open_items": sum(row.get("state") == _OPEN for row in rows),
            "kirocrew.work.stale_items": sum(bool(row.get("stale")) for row in rows),
            "kirocrew.work.orphaned_items": sum(bool(row.get("orphaned")) for row in rows),
            "kirocrew.work.pending_overdue_items": sum(
                bool(row.get("pending_overdue")) for row in rows
            ),
            "kirocrew.work.undelivered_items": sum(
                bool(row.get("report_undelivered")) for row in rows
            ),
        }
        turn.turn_span.set_attributes(correlation.clean(counts))
    except Exception:
        logger.debug("board health trace failed", exc_info=True)
