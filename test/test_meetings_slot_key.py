"""Meetings slot keys stay inside the agent-turn SDK's key alphabet.

An agent id hand-edited into ``config.json`` can fall outside the alphabet the
App Kit's ``AgentTurnSDK.check_key`` accepts. Such an agent must still be
dispatched, and ``MeetingSession.abandoned`` must still answer with a bool.
"""

from __future__ import annotations

import pytest
from meetings_helpers import (  # noqa: F401
    FakeSessionManager,
    reset_module_state_fixture,
    root_fixture,
)

from kiro_crew.apps.builtins.meetings.backend.domain import session as sess


def test_an_id_inside_the_alphabet_keeps_its_plain_key():
    assert sess.slot_key("note-taker", "standup") == "meetings-note-taker-standup"


def test_an_id_outside_the_alphabet_gets_a_valid_stable_distinct_key():
    key = sess.slot_key("notes taker", "standup")
    assert sess._KEY_CHECK.check_key(key) == key
    assert key == sess.slot_key("notes taker", "standup")
    assert key != sess.slot_key("notes-taker", "standup")
    assert key != sess.slot_key("notes\ttaker", "standup")


@pytest.mark.asyncio
async def test_a_hand_edited_agent_id_is_still_dispatched():
    manager = FakeSessionManager()
    manager.live_keys = set()
    session = sess.MeetingSession(
        meeting_id="m1", sessions=manager, config={"meeting_agents": [{"id": "notes taker"}]}
    )
    assert session.abandoned is True
    session.broadcast("the deployment is done")
    await session.flush_all()
    assert sess.slot_key("notes taker", "m1") in [call[0] for call in manager.calls]
    assert session.agents["notes taker"].paused is False
