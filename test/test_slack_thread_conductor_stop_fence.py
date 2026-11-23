"""A Conductor thread's Stop also fences dispatch from the stopped turn (RFC part D).

A code channel's Stop refuses the stopped turn's further ``session_create`` and
``send`` through its channel-wide stop fence. A thread's turns never record their
start in that fence, so a thread's Stop narrows the conductor's running turn and
drops its recorded Slack authors instead: the exemption's author clause then
refuses dispatch from the unwinding turn (no worker born after Stop, no stopped
worker started again by a ``send``), until the owner writes again. Driven through
the real exemption (``session_control.create_session``) with code channels off.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import test_slack_thread_conductor_identity as identity
from test_session_control_owner_dm import _state

from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce
from kiro_crew.slack.code_channels import routing, stop_helpers

# The identity suite's autouse setup (code channels off, thread runtime built), reused here.
_enabled = identity._enabled
OWNER, _create, _owner_turn, _refused, _thread = (
    identity.OWNER,
    identity._create,
    identity._owner_turn,
    identity._refused,
    identity._thread,
)


def _stop(state: Any, binding: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run the thread's Stop (``routing._stop_bound``) with a fake Slack client."""
    posts: list[str] = []

    async def post_message(channel: str, text: str, thread_ts: Any = None) -> str:
        posts.append(text)
        return "1.0"

    monkeypatch.setattr(routing, "_dashboard_state", lambda: state)
    orch = SimpleNamespace(slack=SimpleNamespace(post_message=post_message))
    asyncio.run(routing._stop_bound(orch, binding, OWNER, binding.channel_id, source="test.stop"))
    return posts


def test_the_stopped_turn_dispatches_nothing_more(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    slot, store = _thread(state, tmp_path)
    _owner_turn(slot)
    assert _create(state, slot)["target"]  # dispatch works before the Stop

    binding = store.thread_bindings()[0]
    _stop(state, binding, monkeypatch)

    # The same turn, still unwinding: refused by the exemption's author clause.
    exc = _refused(state, slot)
    assert "not on record" in exc.message


def test_the_owners_next_message_dispatches_again(tmp_path, monkeypatch) -> None:
    state = _state(tmp_path, monkeypatch)
    slot, store = _thread(state, tmp_path)
    _owner_turn(slot)
    _stop(state, store.thread_bindings()[0], monkeypatch)
    cce.note_channel_sender(slot, OWNER)  # the owner writes in the thread again
    assert _create(state, slot)["target"]


def test_the_fence_ends_with_the_turn() -> None:
    """Only the running turn is narrowed: the turn runner clears the flag at the
    turn's end and the next turn's start, so a new dashboard turn is not narrowed."""
    slot = SimpleNamespace(key="chat-x", _turn_channel_narrowed=False)
    stop_helpers.fence_thread_dispatch(slot)
    assert slot._turn_channel_narrowed is True
    assert cce._sender_refusal(slot)  # narrowed, no author on record
