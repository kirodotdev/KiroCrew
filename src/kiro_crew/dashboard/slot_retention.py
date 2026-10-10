"""Keep the live chat-slot count under ``MAX_LIVE_SLOTS`` without a person's click.

Every create, fork, resume and transfer is refused with ``slot_cap_reached`` once
``live_slot_count()`` reaches ``MAX_LIVE_SLOTS``. Two things here stop the count
from creeping up to that ceiling and staying there:

* the open-tab restore stops building ordinary tabs at :data:`RESTORE_SLOT_BUDGET`
  (newest first), so a restart cannot bring back more tabs than leave room to
  work; a pinned tab, a tab an armed auto-nudge loop drives and a crew worker tab
  bound to an open work item are always built;
* :func:`idle_slot_sweep_loop` archives idle tabs through the tab-close path,
  but only while the count is at or above :data:`SWEEP_HIGH_WATER`.

Either path tells the person through one notification that links to History, so
a tab that left the sidebar is never gone without a word.

The idle selection is :func:`select_idle_slot_keys`, the same one the "Clean up
sessions" button runs, so the button and the sweep cannot disagree on which tab
is idle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, _normalize_slot_key

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

#: How many slots the open-tab restore fills before it stops building ordinary
#: tabs. Below ``MAX_LIVE_SLOTS`` so a fresh boot leaves room for new chats.
RESTORE_SLOT_BUDGET = 400

#: The live-slot count at which the idle sweep starts archiving (90% of the cap).
#: Above :data:`RESTORE_SLOT_BUDGET`, so the first pass after a full restore has
#: headroom and archives nothing until the count has grown past the budget.
SWEEP_HIGH_WATER = MAX_LIVE_SLOTS * 9 // 10

#: Seconds between two idle-sweep passes.
SWEEP_INTERVAL_SECS = 3600.0

#: Where the archive notifications send the person: the chat page with the
#: History pane open.
HISTORY_URL = "/chat?history=1"


def _bound_keys(pairs: Iterable[tuple[Any, Any]]) -> set[str]:
    keys: set[str] = set()
    for active, slot_key in pairs:
        if not active or not isinstance(slot_key, str) or not slot_key:
            continue
        keys.add(slot_key)
        keys.add(_normalize_slot_key(slot_key))
    return keys


def loop_slot_keys(loops: Iterable[Any], *, include_paused: bool = False) -> set[str]:
    """The slot keys an ACTIVE auto-nudge loop is bound to, in both spellings.

    With *include_paused* a paused loop counts too. A channel-born loop is bound
    under its channel session key (``slack:<ts>``) while its tab is named with
    the folded form (``slack_<ts>``), so both are returned or a lookup by tab
    name misses the loop.
    """
    return _bound_keys(
        (include_paused or getattr(lp, "active", False), getattr(lp, "slot_key", ""))
        for lp in loops
    )


def live_loop_slot_keys(*, include_paused: bool = False) -> set[str] | None:
    """Loop keys from the running auto-nudge service; ``None`` if none runs."""
    from kiro_crew.autonudge import get_instance  # circular: autonudge -> dashboard

    svc = get_instance()
    if svc is None:
        return None
    return loop_slot_keys(svc.list_all(), include_paused=include_paused)


def stored_loop_slot_keys() -> set[str] | None:
    """Active loop keys read from the auto-nudge store on disk. BLOCKING.

    For the startup restore, which runs before the auto-nudge service starts.
    Read-only: unlike the service's own loader it never creates the file. A
    missing store means no loops (an empty set); a store that cannot be read or
    parsed returns ``None``, which callers treat as "unknown".
    """
    from kiro_crew.autonudge_service.store import LoopStore
    from kiro_crew.config.paths import data_home

    path = LoopStore(data_home()).path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception:  # noqa: BLE001 - unreadable means unknown, never "no loops"
        logger.warning("Could not read the auto-nudge store at %s", path, exc_info=True)
        return None
    rows = data.get("loops") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return _bound_keys(
        (row.get("active", True), row.get("slot_key")) for row in rows if isinstance(row, dict)
    )


def crew_bound_on_disk(slot_key: str) -> bool:
    """Whether *slot_key* is a crew worker bound to an OPEN work item. BLOCKING.

    The work-ledger wake gate reads a worker whose slot is gone as closed
    (``ledger_wake.worker_closed``), so a worker tab left unbuilt or archived
    would report a live worker as ended. The store is read by the work-ledger
    handler module, its one dashboard seam
    (:func:`~kiro_crew.dashboard.handlers.work_ledger.worker_holds_open_item`).
    """
    from kiro_crew.dashboard.handlers.work_ledger import (  # circular: handlers -> state
        worker_holds_open_item,
    )

    return worker_holds_open_item(slot_key)


def _parse_ts(raw: Any) -> float:
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def slot_last_activity(slot: "_ChatSlot") -> float:
    """The epoch of the slot's newest timestamped message, else its creation; 0 if unknown."""
    for m in reversed(slot.messages or []):
        ts = m.get("ts", "")
        if not ts:
            continue
        parsed = _parse_ts(ts)
        if parsed:
            return parsed
    return _parse_ts(slot.created_at)


#: The longest slot key a foreground report may name (the ``slot_read`` bound).
_MAX_FOREGROUND_KEY = 512


def note_foreground(state: "DashboardState", conn: int, slot_key: object) -> None:
    """Record the slot connection *conn* shows in the foreground; a blur clears it.

    Fed from an owner socket's ``slot_focused`` frame, which the client sends
    with ``null`` when its tab is hidden, and cleared when the socket closes.
    """
    slots = getattr(state, "_foreground_slots", None)
    if not isinstance(slots, dict):
        return
    if isinstance(slot_key, str) and slot_key and len(slot_key) <= _MAX_FOREGROUND_KEY:
        slots[conn] = slot_key
    else:
        slots.pop(conn, None)


def foreground_slot_keys(state: "DashboardState") -> set[str]:
    """The slots some connected dashboard shows in the foreground."""
    slots = getattr(state, "_foreground_slots", None)
    return set(slots.values()) if isinstance(slots, dict) else set()


def has_pending_approval(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """Whether *slot* waits on an approval, its own or a coordinator one routed to it.

    A coordinator approval (a child's gateway approval routed to its parent)
    writes no transcript row and does not make the parent ``running``, so only
    ``pending_coordinator_approvals`` shows it. Closing the slot would reject it.
    A registry that cannot answer counts as pending.
    """
    futures = getattr(slot, "_approval_futures", None) or {}
    if any(not fut.done() for fut in futures.values()):
        return True
    pending_for = getattr(state, "pending_coordinator_approvals", None)
    if not callable(pending_for):
        return False
    try:
        return bool(pending_for(slot.key))
    except Exception:  # noqa: BLE001 - unknown approvals keep the slot
        logger.debug("pending-approval probe failed for %s", slot.key, exc_info=True)
        return True


def subagents_attached_now(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """The in-memory half of the shared attached-sub-agent probe, fail closed.

    Running children and results still being delivered, read without the task
    store so it can run inside a synchronous re-check. The queued half reads the
    store and is asked by :func:`sweep_idle_slots` through
    ``subagents_attached_async`` before that re-check. A probe that raises or
    answers ``None`` counts as attached.
    """
    from kiro_crew.dashboard.chat_utils import (  # circular: chat_utils -> state
        _attached_verdict,
        effective_session_key,
    )

    subs = getattr(state, "subagents", None)
    if subs is None:
        return False
    try:
        running = subs.running_agents_for(effective_session_key(slot))
    except Exception:  # noqa: BLE001 - an unanswerable probe keeps the slot
        logger.debug("sub-agent probe failed for %s", slot.key, exc_info=True)
        return True
    return _attached_verdict(running, 0, slot)


def select_idle_slot_keys(
    state: "DashboardState",
    *,
    looped: set[str],
    cutoff: float,
    request_app: str = "",
    active_slot: str = "",
    skip_running: bool = False,
    skip_app_owned: bool = False,
    skip_busy: bool = False,
) -> tuple[list[str], bool]:
    """Pick the live slots idle since before *cutoff*.

    Skips pinned slots, slots in *looped* (an armed auto-nudge loop is idle by
    nature between cycles, and archiving its tab ends the loop for good), slots of
    another app when *request_app* is set, slots whose activity is unknown, with
    *skip_running* slots with a turn in flight, with *skip_app_owned* every
    app-owned slot, and with *skip_busy* slots a connected dashboard shows in
    the foreground, waiting on an approval or with sub-agent work attached
    (:func:`foreground_slot_keys`, :func:`has_pending_approval`,
    :func:`subagents_attached_now`). *active_slot* is never picked; the second
    return value says whether it would have been.
    """
    stale: list[str] = []
    active_is_stale = False
    for name in list(state._slots):
        slot = state._slots.get(name)
        if slot is None or slot.pinned or name in looped:
            continue
        if request_app and slot._app != request_app:
            continue
        if skip_running and slot.running:
            continue
        if skip_app_owned and slot._app:
            continue
        last_activity = slot_last_activity(slot)
        if not last_activity or last_activity >= cutoff:
            continue
        if skip_busy and (
            name in foreground_slot_keys(state)
            or has_pending_approval(state, slot)
            or subagents_attached_now(state, slot)
        ):
            continue
        if name == active_slot:
            active_is_stale = True
            continue
        stale.append(name)
    return stale, active_is_stale


def _still_idle_check(
    state: "DashboardState", slot: "_ChatSlot", name: str, cutoff: float
) -> Callable[[], None]:
    """The synchronous re-check ``close_slot`` runs right before it pops *slot*.

    It first asks that *name* still holds *slot*: the sweep awaits its probes
    after reading the slot, and a tab closed and reopened under the same key in
    that window is a different session the re-check never judged.
    """

    def _check() -> None:
        from kiro_crew.dashboard.chat_handlers import SlotCloseError  # circular

        looped = live_loop_slot_keys(include_paused=True)
        if (
            state._slots.get(name) is not slot
            or slot.running
            or slot.pinned
            or slot._app
            or looped is None
            or name in looped
            or slot_last_activity(slot) >= cutoff
            or name in foreground_slot_keys(state)
            or has_pending_approval(state, slot)
            or subagents_attached_now(state, slot)
        ):
            raise SlotCloseError(
                "the session became active during the idle sweep", code="slot_not_idle"
            )

    return _check


async def _queued_or_attached(state: "DashboardState", slot: "_ChatSlot") -> bool:
    """The full shared sub-agent probe, queued children included; fail closed."""
    from kiro_crew.dashboard.chat_utils import (  # circular: chat_utils -> state
        effective_session_key,
        subagents_attached_async,
    )

    try:
        return await subagents_attached_async(
            state, slot, effective_session_key(slot), "idle_slot_sweep"
        )
    except Exception:  # noqa: BLE001 - an unanswerable probe keeps the slot
        logger.debug("sub-agent probe failed for %s", slot.key, exc_info=True)
        return True


def notify_left_in_history(state: "DashboardState", count: int, reason: str) -> None:
    """Tell the person *count* tabs left the sidebar for History. Never raises."""
    if count <= 0:
        return
    try:
        state.notify(
            "agent",
            "Sessions moved to History",
            f"{reason} Open History to resume one.",
            meta={"count": count},
            url=HISTORY_URL,
        )
    except Exception:  # noqa: BLE001 - the log line below is the fallback report
        logger.warning("Could not publish the History notice for %d tab(s)", count, exc_info=True)


async def sweep_idle_slots(
    state: "DashboardState", idle_days: int, *, now: float | None = None
) -> list[str]:
    """Archive idle slots while the live count is at or above :data:`SWEEP_HIGH_WATER`.

    Each slot closes through ``close_slot``, the tab-✕ path, so it is saved to
    history as closed and can be resumed later. App-owned slots are never swept:
    that path tells the owning app the person dismissed the tab, which an idle
    tab is not. Neither is a slot with any loop, active or paused, a slot a
    connected dashboard shows in the foreground, a slot waiting on an approval, a slot with sub-agent work attached (running, queued or still
    delivering; a probe that cannot answer keeps the slot), or a crew worker tab
    bound to an open work item. The same re-check runs right before
    ``close_slot`` (whose first step retires the slot's loop) and again right
    before the pop, so a slot that started a turn, got pinned, gained a loop, an
    approval, a child or a viewer, or saw new activity is kept. One notification reports
    what was archived. Returns the archived keys.
    """
    if idle_days <= 0 or state.live_slot_count() < SWEEP_HIGH_WATER:
        return []
    # A PAUSED loop exempts its tab too: ``close_slot`` removes whatever loop the
    # slot holds, and a resume can land while that removal waits for the lock.
    looped = live_loop_slot_keys(include_paused=True)
    if looped is None:
        # Which tabs a loop drives is unknown, so archive nothing this pass rather
        # than end a loop by closing its tab.
        logger.info("Idle slot sweep skipped: the auto-nudge service is not running")
        return []
    cutoff = (time.time() if now is None else now) - idle_days * 86400
    stale, _ = select_idle_slot_keys(
        state,
        looped=looped,
        cutoff=cutoff,
        skip_running=True,
        skip_app_owned=True,
        skip_busy=True,
    )
    from kiro_crew.dashboard import chat_handlers  # circular: chat_handlers -> state

    archived: list[str] = []
    for name in stale:
        slot = state._slots.get(name)
        if slot is None or slot.is_closing:
            continue
        if await asyncio.to_thread(crew_bound_on_disk, name):
            continue
        if await _queued_or_attached(state, slot):
            continue
        still_idle = _still_idle_check(state, slot, name, cutoff)
        try:
            # Synchronous and immediately before the call: no await may separate
            # this check from close_slot retiring the slot's loop.
            still_idle()
            await chat_handlers.close_slot(state, slot, name, pre_pop_check=still_idle)
        except chat_handlers.SlotCloseError as exc:
            logger.info("Idle slot sweep kept %s: %s", name, exc.code)
            continue
        archived.append(name)
    if archived:
        logger.info(
            "Idle slot sweep archived %d session(s) idle over %d day(s)", len(archived), idle_days
        )
        notify_left_in_history(
            state,
            len(archived),
            f"Archived {len(archived)} session(s) idle over {idle_days} day(s) to History "
            f"to stay under the {MAX_LIVE_SLOTS}-session limit.",
        )
    return archived


async def idle_slot_sweep_loop(state: "DashboardState", idle_days: int) -> None:
    """Background task: run :func:`sweep_idle_slots` every :data:`SWEEP_INTERVAL_SECS`."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECS)
        try:
            await sweep_idle_slots(state, idle_days)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad pass must not end the loop
            logger.warning("Idle slot sweep pass failed", exc_info=True)
