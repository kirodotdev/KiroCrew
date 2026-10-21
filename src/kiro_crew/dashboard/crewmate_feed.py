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
it. A restart forgets the last digest and records one fresh snapshot per thread on
the next change, which a fold that keeps the newest one whole absorbs.

Runs on the serving loop; the growth signal arrives on the emitter's writer thread
and is handed over with ``call_soon_threadsafe``. The list reads the change-card
store, so each settle first warms it off the loop (``CardStore.warm``) and, when that
fails, reads the list without the store rather than touch the file on the loop.
"""

from __future__ import annotations

import asyncio
import hashlib
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
_pending: set[str] = set()
_timer: "asyncio.TimerHandle | None" = None
#: Per thread slot key, the newest goal record written while the thread had no live
#: session to log it into; recorded on the thread's next growth, when it has one.
_held_goals: dict[str, dict[str, Any]] = {}


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
    """The ``dashboard/goal_recorded`` payload for goal record *rec*."""
    from kiro_crew import member_goals

    milestones = []
    for kr in rec.get("key_results") or []:
        readings = kr.get("readings") or []
        latest = max(readings, key=lambda r: r.get("at", 0)) if readings else {}
        milestones.append(
            {
                "text": str(kr.get("text") or ""),
                "target": str(kr.get("target") or ""),
                "current": str(kr.get("current") or ""),
                "met": member_goals.key_result_met(kr),
                "at": _iso(latest.get("at")),
            }
        )
    return {
        "goals": [{"text": str(g.get("text") or "")} for g in rec.get("goals") or []],
        "milestones": milestones,
    }


def _session_of(slot: Any) -> str:
    from kiro_crew.crew_log import emit

    return emit.session_id_of(getattr(slot, "_acp_client", None))


def _crewmate_slots(state: Any) -> list[Any]:
    from kiro_crew import members as members_mod

    slots = getattr(state, "_slots", None) or {}
    return [s for s in list(slots.values()) if getattr(s, "mode", "") == members_mod.DM_SLOT_MODE]


def _record(slot_key: str, kind: str, session_id: str, data: dict[str, Any]) -> bool:
    """Append *data* as *kind* unless it equals the last one recorded for *slot_key*."""
    from kiro_crew.crew_log import emit

    digest = _digest(data)
    if not session_id or _last.get((slot_key, kind)) == digest:
        return False
    if kind == "goal":
        emit.on_goal_recorded(session_id, data)
    else:
        emit.on_waiting_changed(session_id, data)
    _last[(slot_key, kind)] = digest
    return True


def record_waiting(state: Any, slot: Any, *, cards: bool = True) -> bool:
    """Record *slot*'s waiting list when it changed. True when an entry was appended.

    *cards* False leaves the change-card store unread (its warm-up failed), so the
    list holds the thread's own questions only.
    """
    from kiro_crew.dashboard import member_waiting

    items = member_waiting.waiting_items(state, slot.key, cards=cards)
    return _record(slot.key, "waiting", _session_of(slot), {"items": items})


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
    """Record goal record *rec* on *slot* when it changed. True when appended."""
    return _record(slot.key, "goal", _session_of(slot), goal_snapshot(rec))


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
    if _timer is None:
        _timer = asyncio.get_running_loop().call_later(_SETTLE_SECONDS, _settle)


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
            if _session_of(slot) not in grown:
                continue
            record_waiting(state, slot, cards=cards)
            held = _held_goals.pop(slot.key, None)
            if held is not None:
                record_goal(slot, held)
            elif (slot.key, "goal") not in _last:
                # A goal written before the thread had a session (the create flow sets
                # it first) was not recorded then: record it on the thread's first growth.
                task = asyncio.get_running_loop().create_task(_record_current_goal(slot))
                _keep.add(task)
                task.add_done_callback(_keep.discard)
        except Exception:
            logger.debug("crewmate feed: waiting list for %s not recorded", slot.key, exc_info=True)


#: Strong references to in-flight goal reads, so the loop cannot collect one.
_keep: set["asyncio.Task[None]"] = set()


async def _record_current_goal(slot: Any) -> None:
    """Read *slot*'s crewmate's goal record off the loop and record it."""
    from kiro_crew import member_goals
    from kiro_crew import members as members_mod

    try:
        member = slot.agent
        slug = await asyncio.to_thread(members_mod.member_slug, member)
        rec = await asyncio.to_thread(member_goals.read_goals, slug, member)
        if member_goals.has_goal(rec):
            record_goal(slot, rec)
    except Exception:
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
    for slot in _crewmate_slots(state):
        if getattr(slot, "agent", "") == member:
            try:
                if _session_of(slot):
                    _held_goals.pop(slot.key, None)
                    record_goal(slot, rec)
                else:
                    # An idle thread has no live client, so no session to log into
                    # (a milestones press between turns, a goal set after a restart).
                    # Held, newest wins, until the thread's next growth names one.
                    _held_goals[slot.key] = rec
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
