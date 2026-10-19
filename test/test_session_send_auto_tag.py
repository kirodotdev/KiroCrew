"""`session_send` tags the session it starts, as a typed first message does.

The composer starts auto-tagging on every send (`chat_api.chat_send`). A session
that another session opens and drives with `session_send` never passes through
the composer, so before this its sidebar row carried no project tag while a
typed-in sibling in the same project did.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(_floor_monkeypatch):
    _floor_monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


def _key(slot) -> str:
    return slot_history_key(slot)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def test_send_that_starts_a_turn_tags_the_session_with_its_project(tmp_path, monkeypatch):
    """The real `maybe_auto_tag` runs, so the assertion is on the tag itself.

    Mutation guard: removing the run-arm `_start_auto_tag` call leaves no tag.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = state.get_or_create_slot("chat-2")
    target.project = str(tmp_path / "workspace-vinod")

    async def _fake_run_chat(_state, _slot, _prompt):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

    async def _drive():
        out = await sc.send_to_target(
            state, caller_session_key=_key(caller), target="chat-2", message="investigate"
        )
        await asyncio.gather(*list(state._background_tasks))
        return out

    out = asyncio.run(_drive())

    assert out["started"] is True
    names = {t["name"] for t in state._tags if t["id"] in target.tags}
    assert names == {"workspace-vinod"}
    assert target._auto_tagged is True


def test_send_that_queues_does_not_start_auto_tagging(tmp_path, monkeypatch):
    """A queued send leaves tagging alone: the running turn was started elsewhere.

    The queue arm has not written the user row yet, so the topic pass would read
    the wrong first message. The turn already running owns that decision.

    Mutation guard: tagging on every delivery, not only the run arm, calls it here.
    """
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    target = _busy(state.get_or_create_slot("chat-2"))
    target.project = str(tmp_path / "workspace-vinod")
    tagger = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_auto_tag.maybe_auto_tag", tagger)

    out = asyncio.run(
        sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="later")
    )

    assert out["started"] is False
    tagger.assert_not_called()
