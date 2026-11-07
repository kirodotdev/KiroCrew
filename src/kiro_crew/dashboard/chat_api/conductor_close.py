"""The person closing a conductor closes the open sessions it still manages.

``api_chat_slot_delete`` reads :func:`_conductor_line` while the conductor is still
live, then, after its close commits, :func:`_close_managed` closes the sessions that
are both still managed by a closed manager and still in the conductor's tree.
:func:`_close_preview` answers, before any close, which sessions that would be, so
the dashboard can ask with the right count. Both pick each layer with
:func:`_managed_layer`, so the preview and the close cannot disagree on the rule.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Iterable

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        close_slot,
        deny_app_slot_access,
        logger,
        slot_not_found,
    )


def _conductor_line(
    state: DashboardState, key: str
) -> "tuple[frozenset[str], frozenset[str]] | None":
    """``(members, live)`` when live slot *key* is a conductor, else ``None``.

    A conductor is an Ultra Mode root or an un-nested Bot thread, by the same stamps
    the slot list writes, so a plain chat that opened a session of its own is not one
    and keeps that session on close. *members* are the live keys whose stamped row
    names *key* as its ``crew_root`` or ``mate_root``: a session released from the
    tree or adopted into another one is not. *live* is every live key at this read.
    One stamp pass, no I/O; ``None`` on a fault.
    """
    try:
        from kiro_crew import crew_mode
        from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

        rows = crew_mode.live_stamp_rows(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        )
        row = rows.get(key)
        if row is None:
            return None
        if not row.get(crew_mode.ROOT_FIELD):
            # A Bot thread another root dispatched is that root's member, not a root.
            nested = row.get(crew_mode.MATE_ROOT_FIELD) or row.get(crew_mode.WORKER_FIELD)
            if not crew_mode._row_is_mate_root(row) or nested:
                return None
        members = frozenset(
            k
            for k, r in rows.items()
            if k != key and key in (r.get(crew_mode.WORKER_FIELD), r.get(crew_mode.MATE_ROOT_FIELD))
        )
        return members, frozenset(rows)
    except Exception:
        logger.debug("Could not tell whether %s is a conductor", key, exc_info=True)
        return None


def _managed_layer(
    slots: "Iterable[Any]",
    line: "tuple[frozenset[str], frozenset[str]]",
    managers: "set[str]",
    tried: "set[str]",
    holder: "Callable[[Any], str]",
) -> "list[Any]":
    """The next sessions a conductor close takes: managed by a closed manager, in its tree.

    *line* is :func:`_conductor_line`'s ``(members, live)``. A session is taken when
    it has not been tried yet, it was a tree member at that read or was not open yet,
    and *holder* names one of *managers*. Pure; the one rule both
    :func:`_close_managed` and :func:`_close_preview` apply.
    """
    members, live = line
    return [
        s
        for s in slots
        if s.key not in tried and (s.key in members or s.key not in live) and holder(s) in managers
    ]


def _close_preview(state: DashboardState, key: str, chain_on: bool) -> "list[str]":
    """The open sessions closing live slot *key* now would close with it, in close order.

    The same steps the close runs, with nothing written: the conductor test and
    tree read (:func:`_conductor_line`), the conductor's own hand-back when the
    manager chain switch is on (``hand_back_changes``, the computation
    ``hand_back_on_close`` writes), then :func:`_managed_layer` pass by pass, each
    taken session becoming a manager as a closed one does. Assumes every close
    succeeds. The caller loads the manager records and reads *chain_on* off the
    loop first; this does no I/O. ``[]`` for a slot that is not a conductor.
    """
    from kiro_crew.dashboard import session_managers
    from kiro_crew.dashboard.session_control_parts.session_handoff import hand_back_changes

    line = _conductor_line(state, key)
    root = state._slots.get(key)
    if line is None or root is None:
        return []
    slots = [s for k, s in list(state._slots.items()) if k != key]
    changes = (hand_back_changes(slots, root, session_managers.load()) if chain_on else None) or {}

    def holder_after_hand_back(slot: Any) -> str:
        if slot.key in changes:
            moved = changes[slot.key]
            return moved[0] if moved else str(getattr(slot, "_created_by", "") or "")
        return session_managers.holder(slot)

    managers = {key}
    tried: set[str] = set()
    order: list[str] = []
    while layer := _managed_layer(slots, line, managers, tried, holder_after_hand_back):
        for child in layer:
            tried.add(child.key)
            order.append(child.key)
            managers.add(child.key)
    return order


async def _close_managed(
    state: DashboardState, root_key: str, line: "tuple[frozenset[str], frozenset[str]]"
) -> "tuple[list[str], list[str]]":
    """Close the open sessions a just-closed *root_key* still manages, down the line.

    Runs after *root_key*'s close committed, so its hand-back has already run: a
    session it handed up the manager line, or gave back to another creator, no
    longer names it and stays open. *line* is :func:`_conductor_line`'s read from
    before that close: a session closes only when it was in the conductor's tree
    then, or was not open yet (opened while the line is closing). One that left the
    tree (``session_release``, an adoption elsewhere) keeps its manager record but
    stays open. Each pass reads the live slots again, so a session opened while an
    earlier one was closing is found too. Only a manager that actually closed passes
    its line on. Returns ``(closed, failed)`` keys.
    """
    from kiro_crew.dashboard import session_managers

    await asyncio.to_thread(session_managers.load)
    managers = {root_key}
    tried: set[str] = set()
    closed: list[str] = []
    failed: list[str] = []
    while True:
        layer = _managed_layer(
            list(state._slots.values()), line, managers, tried, session_managers.holder
        )
        if not layer:
            return closed, failed
        for child in layer:
            tried.add(child.key)
            async with session_managers.HANDOFF_LOCK:
                if state.get_slot(child.key) is not child:
                    continue
                if session_managers.holder(child) not in managers:
                    continue
                # A handoff can land after the layer read; keep it out across the
                # close awaits too. hand_back=False avoids taking this lock again.
                try:
                    await close_slot(state, child, child.key, hand_back=False)
                except Exception as exc:
                    logger.warning("Closing worker %s with its manager failed: %s", child.key, exc)
                    failed.append(child.key)
                else:
                    closed.append(child.key)
                    managers.add(child.key)


async def api_chat_slot_close_preview(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/close-preview: how many sessions a close would take.

    A read with no side effect, for the dashboard's close prompt: ``{"workers": N}``,
    the number of open sessions ``DELETE`` on the same slot would close with it now
    (:func:`_close_preview`, the selection that close runs). Same gates as that
    ``DELETE``: the dashboard's request authentication and the slot-ownership
    checkpoint before the handler, the App Kit ownership check here, and the uniform
    404 for a slot that is not live. An app caller is answered 0, because its close
    never takes workers. Only the count is returned.
    """
    from kiro_crew.dashboard import session_managers
    from kiro_crew.dashboard.session_control_parts.caller_access import manager_chain_enabled

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    request_app = request.get("app", "")
    denied = deny_app_slot_access(request_app, slot, name, "slot_close_preview")
    if denied is not None:
        return denied
    if request_app:
        return web.json_response({"workers": 0})
    await asyncio.to_thread(session_managers.load)
    chain_on = await asyncio.to_thread(manager_chain_enabled)
    # A close that lands during those two reads takes the slot out of the table,
    # which the preview answers as 0: nothing is left to close with it.
    return web.json_response({"workers": len(_close_preview(state, name, chain_on))})
