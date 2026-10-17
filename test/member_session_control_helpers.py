"""The member DM slot and create-budget reset the member session-control tests share.

``test_member_session_control.py`` and ``test_member_session_control_memory_delegation.py``
both drive ``create_session`` from a crew member's own DM slot: ``_MEMBER`` is that slot's
key, :func:`_member_tab` mints it the way the member-thread endpoint does, and the
``_fresh_create_budget`` fixture resets the process-wide per-caller create-rate window
around a test. The fixture is declared with ``name=`` and imported under its function name,
so a test that requests it by parameter does not shadow the import.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.members import DM_SLOT_KEY_PREFIX

_MEMBER = DM_SLOT_KEY_PREFIX + "radar"


@pytest.fixture(name="_fresh_create_budget")
def _fresh_create_budget_fixture():
    """The per-caller create-rate window is process-wide module state."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _member_tab(state):
    """A crew member's own DM slot, as the member-thread endpoint mints it.

    ``mode="member"`` is the one path the slot registry admits a ``member-``
    key through; the agent is left to inherit so a name that does not resolve
    in the test config cannot pre-empt the gates under test.
    """
    return state.get_or_create_slot(_MEMBER, mode="member")
