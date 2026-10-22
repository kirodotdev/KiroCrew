"""The gateway records a crewmate's goal and its "waiting on you" list into its crew log.

A crewmate's Dashboard page is a page it writes itself, and every value on it is read
from a crew-log fold so the page moves on its own as facts land. Two of the facts a
page about a crewmate's work needs were not in the log: its goal record (the goals,
each milestone's target and latest reading, whether it is met) and what it waits on
the person for. This module appends both, as snapshots, at the point each changes:

* ``dashboard/goal_recorded`` after every write of the goal record
  (:func:`kiro_crew.member_goals.add_change_listener`), folded as ``goal``;
* ``dashboard/waiting_changed`` whenever the list
  :func:`kiro_crew.dashboard.member_waiting.waiting_items` reads off the thread differs
  from the one last recorded, checked after each growth of the crewmate's own DM log
  (a reply, an [OPTIONS] chip, a card raised or pressed, a turn ending all append
  there), folded as ``waiting``.

Each snapshot is appended only when it differs from the last one this process
recorded for that thread, so the append that a check itself causes does not repeat
it. A snapshot counts as recorded once its append commits (the writer's
``on_settled``); one the writer rejects is appended again on the next check. A restart forgets the last digest and records one fresh snapshot per thread on
the next change, which a fold that keeps the newest one whole absorbs.

Runs on the serving loop; the growth signal arrives on the emitter's writer thread
and is handed over with ``call_soon_threadsafe``. The list reads the change-card
store, so each settle first warms it off the loop (``CardStore.warm``) and, when that
fails, reads the list without the store rather than touch the file on the loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import threading
import weakref
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: How long a burst of log growth is coalesced before the list is read again.
_SETTLE_SECONDS = 0.4

_state_ref: "weakref.ReferenceType[Any] | None" = None
_registered = False
_lock = threading.Lock()
#: Per thread slot key, the digest of the last snapshot of each kind recorded.
_last: dict[tuple[str, str], str] = {}
#: Per thread slot key, ``(seq, digest)`` of the newest snapshot handed to the writer
#: and not yet settled. ``seq`` orders appends, so an older append's settle never
#: speaks for a newer one with the same digest.
_inflight: dict[tuple[str, str], tuple[int, str]] = {}
#: Per thread slot key, the ``seq`` of the append that carries the held goal snapshot.
_held_seq: dict[str, int] = {}
_seq = itertools.count(1)
_pending: set[str] = set()
_timer: "asyncio.TimerHandle | None" = None
#: Per thread slot key, the newest goal snapshot not yet committed: written while
#: the thread had no session to log into, or rejected by the writer. Appended on the
#: thread's next growth, and dropped only once an append of it commits.
_held_goals: dict[str, dict[str, Any]] = {}
#: Per thread slot key, the session it last logged into, so a growth that lands
#: after the turn's teardown cleared the live client still finds its thread.
_sessions: dict[str, str] = {}


def _digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _iso(at: Any) -> str:
    if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
        try:
            return datetime.fromtimestamp(float(at), tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def goal_snapshot(rec: dict[str, Any]) -> dict[str, Any]:
    """The ``dashboard/goal_recorded`` payload for goal record *rec*.

    Every string is redacted the way the waiting list is (exfiltration URLs, then
    credentials): a milestone reading is free text, and the snapshot is appended to
    the crew log and broadcast as a fold. The rows stop at the fold's own bounds,
    the counts cover the whole record, and every cell is shortened until the entry
    fits one crew-log line.
    """
    from kiro_crew.crew_log.entry_types import CREWMATE_ROW_LIMIT, CREWMATE_TEXT_LIMIT
    from kiro_crew.dashboard.state import _redact

    def clean(value: Any) -> str:
        return _redact(str(value or ""))

    key_results = [kr for kr in rec.get("key_results") or [] if isinstance(kr, dict)]
    goals = [g for g in rec.get("goals") or [] if isinstance(g, dict)]
    limit = CREWMATE_TEXT_LIMIT
    while True:
        snap = _goal_rows(goals, key_results, clean, limit, CREWMATE_ROW_LIMIT)
        # The counts cover the whole record; the rows are what the fold keeps anyway.
        snap.update(goals_total=len(goals), milestones_total=len(key_results))
        if limit <= 16 or _fits(snap):
            return snap
        # Wide characters escape to up to twelve bytes each on the log line: shorten
        # every cell until the whole entry fits rather than have the writer refuse it.
        limit //= 2


def _fits(data: dict[str, Any]) -> bool:
    from kiro_crew.crew_log import emit_text
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY

    return emit_text._entry_line_fits("dashboard/goal_recorded", data, src=_SRC_GATEWAY)


def _goal_rows(
    goals: list[dict[str, Any]],
    key_results: list[dict[str, Any]],
    clean: Any,
    limit: int,
    rows: int,
) -> dict[str, Any]:
    def cut(value: Any) -> str:
        return str(clean(value))[:limit]

    milestones = []
    for kr in key_results[:rows]:
        readings = kr.get("readings") or []
        latest = max(readings, key=lambda r: r.get("at", 0)) if readings else {}
        milestones.append(
            {
                "text": cut(kr.get("text")),
                "target": cut(kr.get("target")),
                "current": cut(kr.get("current")),
                "at": _iso(latest.get("at")),
            }
        )
    return {"goals": [{"text": cut(g.get("text"))} for g in goals[:rows]], "milestones": milestones}


def _session_of(slot: Any) -> str:
    from kiro_crew.crew_log import emit

    return emit.session_id_of(getattr(slot, "_acp_client", None))


def _sid(slot: Any) -> str:
    """*slot*'s live session, or the one it last logged into once teardown cleared it."""
    sid = _session_of(slot)
    if sid:
        _sessions[slot.key] = sid
        return sid
    return _sessions.get(slot.key, "")


def _crewmate_slots(state: Any) -> list[Any]:
    from kiro_crew import members as members_mod

    slots = getattr(state, "_slots", None) or {}
    return [s for s in list(slots.values()) if getattr(s, "mode", "") == members_mod.DM_SLOT_MODE]


def _record(
    slot_key: str, kind: str, session_id: str, data: dict[str, Any], *, hold: bool = False
) -> bool:
    """Append *data* as *kind* unless it equals the newest one recorded for *slot_key*.

    The newest is the one being written when there is one, else the last committed,
    so a goal changed and changed back while the first append is still queued is
    appended again. A goal snapshot stays held (:data:`_held_goals`) until an append
    of it commits, so one the writer rejects is appended again on the next growth.
    """
    from kiro_crew.crew_log import emit

    key = (slot_key, kind)
    digest = _digest(data)
    with _lock:
        writing = _inflight.get(key)
        if hold:
            # Replacing the held goal and its generation in one step: no older
            # append's settle can own (and so clear) the snapshot held from here.
            _held_goals[slot_key] = data
            _held_seq.pop(slot_key, None)
            if writing is not None and writing[1] == digest:
                # The append already queued carries this very snapshot.
                _held_seq[slot_key] = writing[0]
        newest = writing[1] if writing is not None else _last.get(key)
        if not session_id or digest == newest:
            if kind == "goal" and key not in _inflight and digest == _last.get(key):
                # Already committed: nothing is left to retry.
                _held_goals.pop(slot_key, None)
            return False
        seq = next(_seq)
        _inflight[key] = (seq, digest)
        if kind == "goal" and _held_goals.get(slot_key) is data:
            # This append carries the held snapshot.
            _held_seq[slot_key] = seq

    def settled(ok: bool) -> None:
        # The writer thread, in append order: the digest counts as recorded only
        # once the append committed, and only this append's own settle clears what
        # it carries.
        with _lock:
            if _inflight.get(key, (0, ""))[0] == seq:
                del _inflight[key]
            if ok:
                _last[key] = digest
            if kind == "goal" and _held_seq.get(slot_key) == seq:
                del _held_seq[slot_key]
                if ok:
                    _held_goals.pop(slot_key, None)

    if kind == "goal":
        emit.on_goal_recorded(session_id, data, on_settled=settled)
    else:
        emit.on_waiting_changed(session_id, data, on_settled=settled)
    return True


def _recorded(slot_key: str, kind: str) -> bool:
    """Whether a *kind* snapshot for *slot_key* has committed or is being appended."""
    with _lock:
        return (slot_key, kind) in _last or (slot_key, kind) in _inflight


def record_waiting(state: Any, slot: Any, *, cards: bool = True) -> bool:
    """Record *slot*'s waiting list when it changed. True when an entry was appended.

    *cards* False leaves the change-card store unread (its warm-up failed), so the
    list holds the thread's own questions only.
    """
    from kiro_crew.dashboard import member_waiting

    items, total = member_waiting.waiting_list(state, slot.key, cards=cards)
    return _record(slot.key, "waiting", _sid(slot), {"items": items, "total": total})


async def _warm_cards(state: Any) -> bool:
    """Load the change-card store off the loop. False when it cannot be read.

    The store reads its file (and the vault key) the first time it is asked, and
    the waiting list is read on the serving loop, so the read is done here, in a
    worker thread, before the list is. A store that fails to warm stays cold, and
    the caller must not ask it on the loop.
    """
    from kiro_crew.dashboard import change_cards

    try:
        await change_cards.card_store_for(state).warm()
    except Exception:
        logger.debug("crewmate feed: change-card store not warmed", exc_info=True)
        return False
    return True


def record_goal(slot: Any, rec: dict[str, Any]) -> bool:
    """Record goal record *rec* on *slot* when it changed. True when appended.

    The snapshot is held until an append of it commits, newest wins.
    """
    return _record_held(slot, goal_snapshot(rec))


def _record_held(slot: Any, snapshot: dict[str, Any]) -> bool:
    return _record(slot.key, "goal", _sid(slot), snapshot, hold=True)


def _state() -> Any:
    return _state_ref() if _state_ref is not None else None


def _on_log_growth(session_id: str) -> None:
    """The emitter's growth signal. Writer thread: hand it to the loop."""
    state = _state()
    loop = getattr(state, "serving_loop", None) if state is not None else None
    if loop is None or not session_id:
        return
    try:
        loop.call_soon_threadsafe(_grew, session_id)
    except RuntimeError:
        logger.debug("crewmate feed: growth for %s after the loop closed", session_id)


def _grew(session_id: str) -> None:
    global _timer
    _pending.add(session_id)
    state = _state()
    if state is not None:
        # Learn which thread this session is now, before a teardown can clear it.
        for slot in _crewmate_slots(state):
            _sid(slot)
    if _timer is None:
        _timer = asyncio.get_running_loop().call_later(_SETTLE_SECONDS, _settle)


def question_retired(slot_key: str) -> None:
    """A question on *slot_key* was retired (answered, dismissed, expired). Any thread.

    Retiring a question writes nothing to the thread's log, so the growth signal never
    fires for it; this hands the slot's waiting list to the same settle pass instead.
    """
    state = _state()
    loop = getattr(state, "serving_loop", None) if state is not None else None
    if loop is None or not slot_key:
        return
    try:
        loop.call_soon_threadsafe(_retired, slot_key)
    except RuntimeError:
        logger.debug("crewmate feed: question retired on %s after the loop closed", slot_key)


def _retired(slot_key: str) -> None:
    global _timer
    state = _state()
    if state is None:
        return
    for slot in _crewmate_slots(state):
        if slot.key == slot_key and _sid(slot):
            _pending.add(_sid(slot))
            if _timer is None:
                _timer = asyncio.get_running_loop().call_later(_SETTLE_SECONDS, _settle)
            return


def _settle() -> None:
    global _timer
    _timer = None
    state = _state()
    grown = set(_pending)
    _pending.clear()
    if state is None or not grown:
        return
    task = asyncio.get_running_loop().create_task(_settle_grown(state, grown))
    _keep.add(task)
    task.add_done_callback(_keep.discard)


async def _settle_grown(state: Any, grown: set[str]) -> None:
    """Record the waiting list of every crewmate thread in *grown*, the store warm first."""
    cards = await _warm_cards(state)
    for slot in _crewmate_slots(state):
        try:
            if _sid(slot) not in grown:
                continue
            _forget_if_new_session(slot)
            if cards:
                # A store that did not warm would leave its pending cards out: keep the
                # last complete snapshot rather than publish a shorter one as current.
                record_waiting(state, slot, cards=cards)
            with _lock:
                held = _held_goals.get(slot.key)
                writing = (slot.key, "goal") in _inflight
            if held is not None:
                if not writing:
                    _record(slot.key, "goal", _sid(slot), held)
            elif _goal_stale(slot) or not _recorded(slot.key, "goal"):
                # A goal written before the thread had a session (the create flow sets
                # it first), or while its thread was closed, was not recorded then:
                # read the record on the thread's first growth.
                with _lock:
                    since = _last.get((slot.key, "goal"))
                task = asyncio.get_running_loop().create_task(
                    _record_current_goal(slot, since=since)
                )
                _keep.add(task)
                task.add_done_callback(_keep.discard)
        except Exception:
            logger.debug("crewmate feed: waiting list for %s not recorded", slot.key, exc_info=True)


#: Per thread, the session its snapshots were last recorded into.
_recorded_in: dict[str, str] = {}


def _forget_if_new_session(slot: Any) -> None:
    """Drop *slot*'s recorded snapshots when its thread now logs into another session.

    A thread deleted and opened again under the same key, or reset, has a new session
    whose log holds none of the old snapshots, so this thread's next growth records its
    goal and waiting list afresh rather than taking them as already recorded.
    """
    sid = _sid(slot)
    with _lock:
        before = _recorded_in.get(slot.key)
        _recorded_in[slot.key] = sid
        if before is not None and before != sid:
            for kind in ("goal", "waiting"):
                _last.pop((slot.key, kind), None)


#: Strong references to in-flight goal reads, so the loop cannot collect one.
_keep: set["asyncio.Task[None]"] = set()


#: Crewmates whose goal record was written while no thread of theirs was open, so
#: no snapshot was recorded or held: their thread's next growth reads the record.
_stale_goals: set[str] = set()


def _goal_stale(slot: Any) -> bool:
    """Whether *slot*'s crewmate had a goal written while it had no open thread; clears it."""
    member = getattr(slot, "agent", "")
    with _lock:
        if member not in _stale_goals:
            return False
        _stale_goals.discard(member)
        return True


async def _record_current_goal(slot: Any, *, since: "str | None" = None) -> None:
    """Read *slot*'s crewmate's goal record off the loop and record it.

    *since* is the digest committed for the slot when the read began. An empty record
    is recorded too: no digest in this process (a restart cleared it) does not mean
    the log holds no older goal, and a page must not keep a deleted one. A goal write
    that lands while the read is off the loop holds, queues or commits a newer
    snapshot, and the read then stands down.
    """
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod

    try:
        member = slot.agent
        slug = await asyncio.to_thread(members_mod.member_slug, member)
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
        key = (slot.key, "goal")
        with _lock:
            newer = key in _inflight or slot.key in _held_goals or _last.get(key) != since
        if newer:
            # A goal write landed while this read was off the loop; it is newer.
            return
        record_goal(slot, rec)
    except Exception:
        # The record could not be read: keep the thread marked so its next growth
        # tries again rather than trusting the snapshot from before.
        with _lock:
            _stale_goals.add(getattr(slot, "agent", ""))
        logger.debug("crewmate feed: goal for %s not recorded", slot.key, exc_info=True)


def _on_goal_written(slug: str, member: str, rec: dict[str, Any]) -> None:
    """``member_goals`` wrote *member*'s record. Any thread: hand it to the loop."""
    state = _state()
    loop = getattr(state, "serving_loop", None) if state is not None else None
    if loop is None:
        return
    snapshot = json.loads(json.dumps(rec, default=str))
    try:
        loop.call_soon_threadsafe(_goal_written, member, snapshot)
    except RuntimeError:
        logger.debug("crewmate feed: goal write for %s after the loop closed", slug)


def _goal_written(member: str, rec: dict[str, Any]) -> None:
    state = _state()
    if state is None:
        return
    slots = [slot for slot in _crewmate_slots(state) if getattr(slot, "agent", "") == member]
    if not slots:
        # No open thread to hold the snapshot on: the thread that next grows reads the
        # record afresh, so a page does not keep the goal from before it closed.
        with _lock:
            _stale_goals.add(member)
        return
    for slot in slots:
        try:
            # An idle thread after a restart has no session to log into yet (a
            # milestones press between turns): the snapshot stays held, newest
            # wins, until the thread's next growth names one.
            record_goal(slot, rec)
        except Exception:
            logger.debug("crewmate feed: goal for %s not recorded", slot.key, exc_info=True)


def start(state: Any) -> None:
    """Record into *state*'s crewmate logs from now on. Once per process; no-op with the log off."""
    global _state_ref, _registered
    from kiro_crew.constants import crew_log_enabled

    if not crew_log_enabled():
        return
    from kiro_crew import member_goals
    from kiro_crew.crew_log import emit

    with _lock:
        _state_ref = weakref.ref(state)
        if not _registered:
            emit.add_growth_listener(_on_log_growth)
            member_goals.add_change_listener(_on_goal_written)
            _registered = True
