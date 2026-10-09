"""``reset_conversation`` on turns a person did not type.

A session driven by other sessions never has a human turn: its wakes arrive by
``session_send`` from the session that dispatched it. That producer may now reset
the session's OWN conversation. Every other non-human producer, a cron delivery
included, is still refused, and a peer send cannot reset a pinned session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.session_control import SEND_ORIGIN_META_KEY
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY
from kiro_crew.dashboard.state import SlotOrigin


class _FakeSlot:
    def __init__(self, key: str = "chat-1-1", *, pinned: bool = False):
        self.key = key
        self.pinned = pinned
        self.linked_session_key = ""
        self._pending_discard_conversation_key = None


async def _reset(slot: _FakeSlot, session_key: str = "", **producer: bool) -> str:
    return await apply_session_directive(
        None,
        slot,
        session_key or f"dashboard:{slot.key}",
        "reset_conversation",
        {},
        **producer,
    )


# ── the applier's gate ──


@pytest.mark.asyncio
async def test_a_peer_send_turn_may_reset_its_own_conversation():
    slot = _FakeSlot()
    result = await _reset(slot, producer_is_peer_send=True)
    assert "queued" in result
    assert slot._pending_discard_conversation_key == "dashboard:chat-1-1"


@pytest.mark.asyncio
async def test_a_peer_send_turn_cannot_reset_a_pinned_session():
    slot = _FakeSlot(pinned=True)
    result = await _reset(slot, producer_is_peer_send=True)
    assert result.startswith("Error:")
    assert "pinned" in result
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_peer_send_turn_cannot_reset_a_member_dm_thread():
    """A ``member-<slug>`` DM thread is the person's conversation with the member,
    so it refuses a peer like a pinned session even with the pin flag unset."""
    slot = _FakeSlot("member-researcher")
    result = await _reset(slot, producer_is_peer_send=True)
    assert result.startswith("Error:")
    assert "pinned" in result
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_cron_delivery_alone_is_refused_even_unpinned():
    """The ``cron`` turn actor rides on a body-supplied ``caller_session``, so a
    turn with no peer-send mark is refused however it was routed."""
    slot = _FakeSlot()
    result = await _reset(slot)
    assert result.startswith("Error:")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_cron_slot_is_refused_whatever_the_producer():
    slot = _FakeSlot("cron-job1")
    result = await _reset(slot, producer_is_peer_send=True)
    assert result.startswith("Error:")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_headless_session_key_is_refused_for_a_peer_send():
    """A sub-agent or task-runner key has no user surface, so the turn is not the
    session's own even when a peer started it."""
    slot = _FakeSlot()
    result = await _reset(slot, "subagent:abc", producer_is_peer_send=True)
    assert result.startswith("Error:")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_user_facing_turn_on_a_headless_key_is_still_refused():
    """The user-surface check holds for every producer: a human-flagged turn on a
    sub-agent key is refused the same as before this change."""
    slot = _FakeSlot()
    result = await _reset(slot, "subagent:abc", producer_is_user_facing=True)
    assert result.startswith("Error:")
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_a_loop_wake_alone_is_still_refused():
    slot = _FakeSlot()
    result = await _reset(slot, producer_is_self_wake=True)
    # A self-wake is decided by the conductor round-reset gate, which refuses a
    # slot that is not a conductor's; the peer admission adds nothing to it.
    assert "NOT reset" in result
    assert slot._pending_discard_conversation_key is None


@pytest.mark.asyncio
async def test_set_project_does_not_inherit_the_peer_admission():
    slot = _FakeSlot()
    result = await apply_session_directive(
        None,
        slot,
        "dashboard:chat-1-1",
        "set_project",
        {"project": "/tmp"},
        producer_is_peer_send=True,
    )
    assert result.startswith("Error:")


# ── the drain marks a queued peer delivery ──


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    """SEL writes from the gate's audit land in the test's own tmp dir."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _spawn_closing(_state, _slot, turn, *_a, **_k) -> MagicMock:
    close = getattr(turn, "close", None)
    if callable(close):
        close()
    return MagicMock()


async def _drained_kwargs(state, slot) -> dict:
    run = MagicMock()
    with (
        patch.object(chat_runner, "spawn_guarded_turn", side_effect=_spawn_closing),
        patch.object(chat_runner, "_run_chat", new=run),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert run.call_count == 1
    return run.call_args.kwargs


_STAMP = {SEND_ORIGIN_META_KEY: {"slot": "chat-9-9", "tab": "tab-9"}}


@pytest.mark.asyncio
async def test_a_queued_peer_send_drains_as_a_peer_origin_turn(state):
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append("[sent by session chat-9-9 via session_send]\n\nwake", meta=dict(_STAMP))
    kwargs = await _drained_kwargs(state, slot)
    assert kwargs.get("_directive_peer_origin") is True


@pytest.mark.asyncio
async def test_a_restored_peer_send_is_not_a_peer_origin_turn(state):
    """The stamp rides in ``meta``, which a restart reads back off an editable
    file, so a restored entry must not be admitted on it."""
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append("wake", meta=dict(_STAMP))
    slot._queue[0][RESTORED_QUEUE_KEY] = True
    kwargs = await _drained_kwargs(state, slot)
    assert not kwargs.get("_directive_peer_origin")


@pytest.mark.asyncio
async def test_a_plain_queued_message_is_not_a_peer_origin_turn(state):
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append("typed text")
    kwargs = await _drained_kwargs(state, slot)
    assert not kwargs.get("_directive_peer_origin")


@pytest.mark.asyncio
async def test_a_queued_message_of_other_provenance_never_joins_a_peer_send_turn(state):
    """A queued message of other provenance never rides in a peer-send turn: the
    peer entry drains as its own turn with the mark, and the plain entry behind it
    drains as the next turn without it."""
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append("[sent by session chat-9-9 via session_send]\n\nwake", meta=dict(_STAMP))
    slot.queue_append("cron result text")
    first = await _drained_kwargs(state, slot)
    assert first.get("_directive_peer_origin") is True
    assert [q["content"] for q in slot._queue] == ["cron result text"]
    second = await _drained_kwargs(state, slot)
    assert not second.get("_directive_peer_origin")


@pytest.mark.asyncio
async def test_a_peer_send_queued_behind_other_text_does_not_lend_it_the_mark(state):
    """Reverse order: whatever the drain consumes first, a turn holding the plain
    entry is not a peer-origin turn."""
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append("cron result text")
    slot.queue_append("[sent by session chat-9-9 via session_send]\n\nwake", meta=dict(_STAMP))
    first = await _drained_kwargs(state, slot)
    assert not first.get("_directive_peer_origin")


# ── a steer of another provenance withdraws the peer admission ──


async def _peer_turn_emits_reset_after_a_steer(tmp_path, monkeypatch, *, steer: str):
    """Drive a real peer-send turn that takes a steer, then emits ``reset_conversation``.

    The harness is ``test_discord_resumed_busy``'s: the real ``_run_chat`` loop
    over a fake ACP client, with ``apply_session_directive_outcome`` replaced by
    a spy. Between the directive tool's call and its result the turn takes a
    steer: the composer's (*steer* ``"composer"``), a Discord human's through
    the hand-off (``"channel"``), or another peer's ``session_send`` (``"peer"``,
    the counterfactual). Returns the spy and the slot.
    """
    from test_acp_tool_identity import _stub_state
    from test_discord_resumed_busy import _busy, _steerable

    from kiro_crew import session_directive
    from kiro_crew.acp.types import (
        EVENT_COMPLETE,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        AcpEvent,
    )
    from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
    from kiro_crew.dashboard.session_directive_apply import DirectiveOutcome

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    marker = session_directive.encode("reset_conversation", {}, "resetting")
    outcomes: list = []

    async def _stream(_msg):
        if outcomes:
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield AcpEvent(kind=EVENT_COMPLETE)
            return
        yield AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-reset",
            title="Resetting",
            tool_name="reset_conversation",
            mcp_server_name="kirocrew-core",
        )
        _busy(slot)
        try:
            outcomes.append(
                await steer_into_running_turn(
                    state,
                    slot,
                    "start over",
                    user_origin=steer == "composer",
                    channel_origin=steer == "channel",
                    peer_send=steer == "peer",
                )
            )
        finally:
            slot.task = None
        yield AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-reset", tool_output=marker, tool_final=True
        )
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    inner = _steerable()
    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = inner
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    spy = AsyncMock(return_value=DirectiveOutcome("[applied]"))
    monkeypatch.setattr(chat_runner, "apply_session_directive_outcome", spy)

    await chat_runner._run_chat(state, slot, "go", _directive_peer_origin=True)
    task = getattr(slot, "task", None)
    if task is not None:
        await task
    assert outcomes == ["steered"], outcomes
    return spy, slot


@pytest.mark.asyncio
@pytest.mark.parametrize("steer", ["composer", "channel"])
async def test_a_steer_of_other_provenance_withdraws_the_peer_admission(
    tmp_path, monkeypatch, steer
):
    """A cron, channel or composer text steered into a peer-send turn could ask for
    the reset, so the directive it may have shaped is not filed as a peer's."""
    spy, slot = await _peer_turn_emits_reset_after_a_steer(tmp_path, monkeypatch, steer=steer)

    spy.assert_called_once()
    assert spy.call_args.args[3] == "reset_conversation"
    assert spy.call_args.kwargs["producer_is_peer_send"] is False
    assert slot._turn_peer_send_voided is False, "the withdrawal ends with the turn"


@pytest.mark.asyncio
async def test_another_peers_steer_keeps_the_peer_admission(tmp_path, monkeypatch):
    """Counterfactual: a second ``session_send`` steer keeps every input a peer's."""
    spy, slot = await _peer_turn_emits_reset_after_a_steer(tmp_path, monkeypatch, steer="peer")

    spy.assert_called_once()
    assert spy.call_args.kwargs["producer_is_peer_send"] is True
