"""The person closing a conductor closes the open sessions it still manages.

``api_chat_slot_delete`` reads :func:`_conductor_line` while the conductor is still
live, then, after its close commits, :func:`_close_managed` closes the sessions that
are both still managed by a closed manager and still in the conductor's tree.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import DashboardState, close_slot, logger


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

    members, live = line
    await asyncio.to_thread(session_managers.load)
    managers = {root_key}
    tried: set[str] = set()
    closed: list[str] = []
    failed: list[str] = []
    while True:
        layer = [
            s
            for s in list(state._slots.values())
            if s.key not in tried
            and (s.key in members or s.key not in live)
            and session_managers.holder(s) in managers
        ]
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
