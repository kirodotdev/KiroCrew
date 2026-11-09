"""Whether a live dashboard slot or SessionManager runtime still owns a session key.

Asked before a close, a delete, a trash or a resource wake retires what a session
holds. Each answer fails safe: True when the owners cannot be read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.messaging.link import canonical_key

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

# Logs under the sessions facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


def slot_owns_session(state: DashboardState, session_key: str, context: str) -> bool:
    """Fail safe when any current slot owns or cannot resolve *session_key*."""
    canonical_session_key = canonical_key(session_key)
    for current_slot in tuple(state._slots.values()):
        try:
            if canonical_key(effective_session_key(current_slot)) == canonical_session_key:
                return True
        except Exception:
            logger.warning(
                "History delete: current slot session owner unreadable for %s; "
                "preserving destructive cleanup",
                context,
                exc_info=True,
            )
            return True
    return False


def runtime_owns_session(state: DashboardState, session_key: str, context: str) -> bool:
    """Fail safe when a slot or SessionManager runtime still owns the key.

    *context* names what is being removed, for the log. A slot key is recycled,
    so a removal that runs after its owner scan re-checks with this.
    """
    if slot_owns_session(state, session_key, context):
        return True
    canonical_session_key = canonical_key(session_key)
    try:
        manager_keys = state.sessions.session_keys()
        return any(
            canonical_key(manager_key) == canonical_session_key for manager_key in manager_keys
        )
    except Exception:
        logger.warning(
            "History delete: current SessionManager owners unreadable for %s; "
            "preserving cron ownership",
            context,
            exc_info=True,
        )
        return True


def session_owned(state: DashboardState, session_key: str, context: str) -> bool:
    """Whether a slot, a SessionManager runtime or an open slot name still owns the key.

    The union of both owner checks, so a key either one keeps stays kept. Fails
    safe: True when the slot directory cannot be read.
    """
    if runtime_owns_session(state, session_key, context):
        return True
    try:
        return bool(state.slot_exists(session_key.removeprefix("dashboard:")))
    except Exception:
        logger.warning(
            "Session owners: slot directory unreadable for %s; keeping it owned",
            context,
            exc_info=True,
        )
        return True


async def session_closure_recorded(
    state: DashboardState | None, session_key: str, context: str
) -> bool:
    """Positive evidence that dashboard session *session_key* was closed (§4.7).

    Asked before a resource subscription is restored at start and before a
    wake retires one. Only a dashboard key counts, only after the open-tab
    restore has made the live slot set authoritative, and only a readable
    persisted ``closed`` flag: absence is missing evidence, never closure, so a
    tab the restore skipped for its budget keeps its subscriptions. The read
    awaits a disk hop, so an owner that reclaimed the key meanwhile keeps them too.
    """
    from kiro_crew.dashboard.handlers.change_cards import slot_closure_recorded

    if (
        state is None
        or not session_key.startswith("dashboard:")
        or not bool(getattr(state, "open_slots_restored", False))
        or session_owned(state, session_key, context)
    ):
        return False
    if not await slot_closure_recorded(state, session_key.removeprefix("dashboard:")):
        return False
    return not session_owned(state, session_key, context)
