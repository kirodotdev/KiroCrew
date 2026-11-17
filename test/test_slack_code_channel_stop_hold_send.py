"""A send to a session a code channel's Stop holds is queued, and its sender is told.

Ruling 09:50Z (``stop_hold``): no peer releases a held helper, the channel's own
session included; the sender's ``session_send`` result says in one sentence that
the helper is held by a Stop and starts when a person messages it, so a conductor
does not wait on it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.slack.code_channels import stop_hold


def _hold(slot: Any) -> Any:
    stop_hold.Pressed(next(stop_hold._presses)).hold(slot)
    return slot


def test_the_channels_send_to_its_held_helper_is_queued_and_says_so(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state(tmp_path)
    channel = _slot(state, "chat-1")
    helper = _peer_target(state, "chat-2", channel)
    helper._created_by = channel.key  # its helper: the channel's session created it
    busy = _busy(_peer_target(state, "chat-3", channel))  # queued, but not held
    busy._created_by = channel.key
    _hold(helper)

    async def drive() -> tuple[dict[str, Any], dict[str, Any]]:
        held = await sc.send_to_target(
            state, caller_session_key=_key(channel), target=helper.key, message="carry on"
        )
        busy_result = await sc.send_to_target(
            state, caller_session_key=_key(channel), target=busy.key, message="carry on"
        )
        return held, busy_result

    held, busy_result = asyncio.run(drive())
    assert held == {
        "ok": True,
        "target": "chat-2",
        "started": False,
        "steered": False,
        "held": True,
    }
    assert [e["content"].endswith("carry on") for e in helper._queue] == [True]
    assert stop_hold.holds(helper), "the channel's own send does not release it"
    assert busy_result["started"] is False and "held" not in busy_result


def test_the_session_send_tool_says_the_target_is_held_in_one_sentence() -> None:
    from kiro_crew.mcp_dashboard_session_runs import _run_session_send

    replies = {"ok": True, "target": "chat-2", "started": False, "steered": False, "held": True}
    ctx = SimpleNamespace(client=SimpleNamespace(post=lambda *_a, **_k: replies), caller_key="c")
    text = _run_session_send({"target": "chat-2", "message": "carry on"}, ctx)
    assert stop_hold.QUEUED_TEXT in text and "mid-turn" not in text
