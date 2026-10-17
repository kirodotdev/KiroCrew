"""Shared builders for the title refresh suite (``test_title_refresh.py`` and its
``test_title_refresh_<topic>.py`` parts): a fake dashboard state, an auto-titled slot
with N user turns, and a patched refresh generator that records its calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard import chat_title
from kiro_crew.dashboard.chat_title import _TITLE_ORIGIN_AUTO
from kiro_crew.dashboard.state import _ChatSlot


def _fake_state(*slots):
    state = MagicMock()
    # conversation_log must be truthy for _persist_title to attempt a write.
    state.conversation_log = MagicMock()
    # The title write looks up ``state._slots`` for the live holder of the key; a
    # bare MagicMock there poses as a slot at every key. The background titler
    # only writes for the slot that still holds its key, so pass that slot here.
    state._slots = {s.key: s for s in slots}
    return state


def _titled_slot(user_turns: int, *, origin: str = _TITLE_ORIGIN_AUTO) -> _ChatSlot:
    slot = _ChatSlot("chat-1-1")
    slot.messages = []
    for i in range(user_turns):
        slot.messages.append({"role": "user", "content": f"user message {i}"})
        slot.messages.append({"role": "assistant", "content": f"assistant reply {i}"})
    slot.title = "Initial auto title"
    slot._titled = True
    slot._title_origin = origin
    return slot


def _patch_generator(monkeypatch, reply: str | Exception):
    """Replace the refresh generator; returns the list of recorded calls."""
    calls: list[str] = []

    async def _fake(_state, _messages, current_title, *, session_key: str = ""):
        calls.append(current_title)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _fake)
    return calls
