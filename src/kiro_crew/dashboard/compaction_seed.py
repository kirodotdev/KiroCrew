"""The dashboard's answer to whether a rotation compaction can re-seed a session.

The compaction coordinator owns no transcript, so it asks the surface through
``SessionManager.set_compaction_seed_writer`` before it projects a rotation: a
``soft`` successor is re-seeded from the dashboard transcript by its replay, and
nothing else re-seeds it. ``supports`` answers from the slot registry alone, with
no transcript read. Only a tab's own session key answers ``True``, so linked
sessions keep ``native``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.autonudge import is_channel_key

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


def _seed_slot(state: DashboardState, key: str) -> Any | None:
    """The slot whose transcript would re-seed *key*'s successor, or ``None``.

    The channel check comes first: a Slack or Discord conversation keeps its own
    session key even while a dashboard tab mirrors it. After routing, the slot's
    own session key is the positive allowlist: an ordinary dashboard tab owns its
    ``dashboard:<slot>`` key, and a task-review tab owns its minted ``taskrunner:``
    key. Cron, hook, subagent, workflow and other linked sessions belong to their
    producer, so their displayed tab cannot authorize a rotation.
    """
    if is_channel_key(key):
        return None
    # circular import: chat_utils imports dashboard.state at module scope, and
    # state registers this writer, so module-scope imports here would close
    # state -> compaction_seed -> chat_utils -> state.
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key
    from kiro_crew.dashboard.slot_ownership import own_session_key

    slot_key = dashboard_slot_key(key)
    if not slot_key:
        return None
    slot = state.get_slot(slot_key)
    if slot is None or own_session_key(slot) != key:
        return None
    return slot


def supports_seed(state: DashboardState, key: str) -> bool:
    """Whether this dashboard holds a transcript that re-seeds *key* (no transcript read)."""
    return _seed_slot(state, key) is not None


class DashboardSeedWriter:
    """The ``SeedWriter`` the dashboard registers: ``supports_seed`` bound to one state."""

    __slots__ = ("_state",)

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    def supports(self, key: str) -> bool:
        return supports_seed(self._state, key)
