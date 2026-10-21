"""A crewmate's own DM thread is never titled by a background path.

The thread (``mode == "member"``) is one long-lived conversation with one
crewmate, and the sidebar names it after the crewmate. The first title, the
refresh milestones and the user's "Regenerate title" must neither run a titling
model call nor write a title for it. A session a crewmate opens for its workers
is an ordinary slot and keeps being titled.
"""

from __future__ import annotations

import pytest
from title_refresh_helpers import _fake_state, _patch_generator, _titled_slot

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_REFRESH_MILESTONES,
    _maybe_auto_title,
    generate_manual_title,
    maybe_refresh_title,
    title_then_refresh,
)
from kiro_crew.dashboard.state import _ChatSlot


def _member_slot(key: str = "member-lead") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.mode = "member"
    slot.agent = "lead"
    slot.messages = [
        {"role": "user", "content": "how is the colour of the Crew page"},
        {"role": "assistant", "content": "It uses the accent token."},
    ]
    return slot


def _patch_title_model(monkeypatch, title: str = "Generated topic") -> list[int]:
    calls: list[int] = []

    async def _fake(_state, _messages, *, session_key: str = ""):
        calls.append(1)
        return title

    monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake)
    return calls


@pytest.mark.asyncio
async def test_first_title_is_not_generated_for_a_member_thread(monkeypatch):
    calls = _patch_title_model(monkeypatch)
    slot = _member_slot()
    await _maybe_auto_title(_fake_state(slot), slot)
    assert calls == []
    assert slot._titled is False
    assert slot.title != "Generated topic"


@pytest.mark.asyncio
async def test_first_title_still_runs_for_an_ordinary_session(monkeypatch):
    """The real path for every other session keeps working."""
    calls = _patch_title_model(monkeypatch)
    slot = _member_slot("chat-9-1")
    slot.mode = "chat"
    slot.agent = ""
    await _maybe_auto_title(_fake_state(slot), slot)
    assert calls == [1]
    assert slot._titled is True
    assert slot.title == "Generated topic"


@pytest.mark.asyncio
async def test_refresh_does_not_rewrite_a_member_thread_title(monkeypatch):
    calls = _patch_generator(monkeypatch, "A different topic")
    slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
    slot.mode = "member"
    slot.agent = "lead"
    await maybe_refresh_title(_fake_state(slot), slot)
    assert calls == []
    assert slot.title == "Initial auto title"


@pytest.mark.asyncio
async def test_refresh_still_runs_for_an_ordinary_session(monkeypatch):
    calls = _patch_generator(monkeypatch, "A different topic")
    slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
    await maybe_refresh_title(_fake_state(slot), slot)
    assert calls == ["Initial auto title"]
    assert slot.title == "A different topic"


@pytest.mark.asyncio
async def test_end_of_turn_chain_leaves_a_member_thread_untitled(monkeypatch):
    title_calls = _patch_title_model(monkeypatch)
    refresh_calls = _patch_generator(monkeypatch, "A different topic")
    slot = _member_slot()
    await title_then_refresh(_fake_state(slot), slot)
    assert title_calls == [] and refresh_calls == []
    assert slot._titled is False


@pytest.mark.asyncio
async def test_regenerate_gives_a_member_thread_no_title(monkeypatch):
    calls = _patch_title_model(monkeypatch)
    slot = _member_slot()
    assert await generate_manual_title(_fake_state(slot), slot) == ""
    assert calls == []
