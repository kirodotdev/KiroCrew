"""Telegram keeps a conversation on the agent its session key was built under.

``handle_message`` fixes the key before the fair-admission wait and ``_run_turn``
runs after it. A default change plus a ``/new`` in that window repins the route,
so the turn must take the agent read with its key, not read the route again.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _hot_reload_helpers import prime_live
from telegram_helpers import _dispatcher, _dm, _press

from kiro_crew.shared_crew import turn_gate

_NEW = "amzn-builder"
_ROUTE = ("direct", "7")


@pytest.mark.asyncio
async def test_a_new_during_admission_leaves_the_waiting_turn_on_its_keys_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prime_live("agent", default_agent="")
    d, _client, _sessions = _dispatcher({7})
    ran: list[dict[str, Any]] = []

    async def _run_turn(msg: Any, text: str, **kwargs: Any) -> bool:
        ran.append(kwargs)
        return False

    async def _admit_after_a_new(*, session_key, text, automation, run, refused):
        # While this message waits, the owner moves the default and the same chat
        # starts a new conversation, which repins the route to the new default.
        prime_live("agent", default_agent=_NEW)
        d._conv.bump_gen(_ROUTE)
        assert _NEW in d._session_key(_ROUTE)
        return await run()

    d._run_turn = _run_turn  # type: ignore[method-assign]
    monkeypatch.setattr(turn_gate, "run_channel_turn", _admit_after_a_new)

    await d.handle_message(_dm("hi"))

    (turn,) = ran
    assert turn["session_key"].split(":")[1] == "kirocrew"
    assert turn["agent"] == "kirocrew"


def test_the_picker_names_the_agent_the_open_conversation_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the default moves, an open conversation still runs the pinned old
    default: the picker says so and marks no row, since Default is the new one."""
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: [_NEW]))
    assert d._session_key(_ROUTE).split(":")[1] == "kirocrew"
    prime_live("agent", default_agent=_NEW)

    asyncio.run(d.handle_message(_dm("/agent")))

    text, markup = client.sent[-1]
    assert text == "Current agent: Default (kirocrew)\nPick one:"
    assert [row[0]["text"] for row in markup["inline_keyboard"]] == [
        f"Default ({_NEW})",
        _NEW,
    ]


def test_a_default_row_pressed_after_the_default_moved_names_the_live_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picker keeps the Default row's label from when it opened. Pressed after
    the default moved, the reset applies the live default and must say so."""
    prime_live("agent", default_agent="")
    d, client, _sessions = _dispatcher({7})
    monkeypatch.setattr(type(d), "_installed_agent_names", staticmethod(lambda: [_NEW]))
    d._agent_picks.set(_ROUTE, _NEW)
    asyncio.run(d.handle_message(_dm("/agent")))
    prime_live("agent", default_agent="other-agent")

    asyncio.run(d.on_callback(_press("g:0")))

    # The press answers by editing the picker message in place.
    reply = client.edits[-1][1]
    assert "Default (other-agent)" in reply, reply
    assert "kirocrew" not in reply, reply
