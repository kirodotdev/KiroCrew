"""A Dashboard card's turn never inherits an auto-approval.

The words of a card come from a page the crewmate wrote, and crewmate threads default
to ``trust`` (every tool call auto-approved). So ``_run_chat`` is told when a turn is a
card's (``_card_origin``) and then skips every rung that approves without asking:
the hook auto-approve list, trusted patterns, trust-reads, session trust, a scoped
grant and YOLO. The call reaches the approval card instead. A hook DENY still denies.

The harness is the one ``test_chat_runner_coverage_approval.py`` uses.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_runner_coverage_helpers import (
    _complete,
    _drive,
    _permission,
    _runner_state,
    _set_stream,
    _settle,
    _slot,
)

from kiro_crew.dashboard import card_send, chat_runner
from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult

_LS = json.dumps({"command": "ls -la"})
_CARD_TEXT = "[Dashboard card] Approve the plan"
# The real drain, kept before any test stubs it out.
_REAL_DRAIN = chat_runner._start_next_queued_turn


@pytest.fixture(autouse=True)
def _vouch_for_the_program():
    """The name-grant check resolves the real PATH; these tests measure the rung."""
    with (
        patch.object(chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)),
        patch.object(chat_runner, "_name_grant_refusal_for", new=AsyncMock(return_value=None)),
    ):
        yield


def _trusting_slot(mode: str):
    slot = _slot()
    if mode == "trust":
        slot._trust = True
    elif mode == "trust_reads":
        slot._trust_reads = True
    elif mode == "trusted_pattern":
        slot._trusted_patterns = {"ls*"}
    return slot


async def _run(state, client, slot, *, card: bool) -> MagicMock:
    """One turn with one permission request; returns the SEL double."""
    _set_stream(client, [_permission(tool_input=_LS), _complete()])
    audit = MagicMock()
    with (
        patch.object(chat_runner, "sel", return_value=audit),
        patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
    ):
        if card:
            slot._empty_response_retries = 2
            await chat_runner._run_chat(state, slot, _CARD_TEXT, _card_origin=True)
            await _settle(slot)
        else:
            await _drive(state, slot)
    return audit


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trust", "trust_reads", "trusted_pattern"])
async def test_a_typed_turn_in_a_trusting_thread_auto_approves(tmp_path, mode):
    """The control: without it the card tests below would pass for any reason."""
    state, client = _runner_state(tmp_path)
    slot = _trusting_slot(mode)

    await _run(state, client, slot, card=False)

    client.approve_tool.assert_awaited_once_with("req-cov-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trust", "trust_reads", "trusted_pattern"])
async def test_a_card_turn_in_a_trusting_thread_is_not_auto_approved(tmp_path, mode):
    state, client = _runner_state(tmp_path)
    slot = _trusting_slot(mode)

    await _run(state, client, slot, card=True)

    client.approve_tool.assert_not_awaited()
    # It reached the approval card, where a person decides.
    cards = [m for m in slot.messages if m.get("role") == "permission"]
    assert len(cards) == 1


@pytest.mark.asyncio
async def test_a_card_turn_is_not_auto_approved_under_yolo(tmp_path):
    state, client = _runner_state(tmp_path)
    state.is_yolo_active = MagicMock(return_value=True)
    slot = _slot()

    await _run(state, client, slot, card=True)

    client.approve_tool.assert_not_awaited()
    assert [m for m in slot.messages if m.get("role") == "permission"]


@pytest.mark.asyncio
async def test_a_card_turn_is_not_auto_approved_by_a_scoped_grant(tmp_path):
    state, client = _runner_state(tmp_path)
    slot = _slot()
    slot._trust_scope = "scope-1"

    with patch.object(chat_runner, "safety_override") as override:
        override.return_value.is_scope_active.return_value = True
        await _run(state, client, slot, card=True)

    client.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_card_turn_is_not_auto_approved_by_the_hook_auto_approve_list(tmp_path):
    builder = MagicMock()
    builder.hooks.on_tool_call.return_value = ToolHookResult(action=TOOL_AUTO_APPROVE)
    builder.build_message.return_value = ("hello", None)
    state, client = _runner_state(tmp_path, context_builder=builder)

    # The control, typed: the hook list alone approves.
    typed_slot = _slot()
    await _run(state, client, typed_slot, card=False)
    client.approve_tool.assert_awaited_once_with("req-cov-1")

    client.approve_tool.reset_mock()
    slot = _slot()
    audit = await _run(state, client, slot, card=True)

    client.approve_tool.assert_not_awaited()
    assert [m for m in slot.messages if m.get("role") == "permission"]
    declined = [
        c.kwargs
        for c in audit.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "auto_approve_declined"
    ]
    assert [d["metadata"] for d in declined] == [
        {"reason": "card_origin_turn", "tier": "hook_auto_approve"}
    ]


@pytest.mark.asyncio
async def test_the_withheld_trust_is_written_to_the_audit_log(tmp_path):
    state, client = _runner_state(tmp_path)
    slot = _trusting_slot("trust")

    audit = await _run(state, client, slot, card=True)

    declined = [
        c.kwargs
        for c in audit.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "auto_approve_declined"
    ]
    assert [d["metadata"] for d in declined] == [{"reason": "card_origin_turn", "tier": "trust"}]
    assert not [
        c
        for c in audit.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == "auto_approved"
    ]


@pytest.mark.asyncio
async def test_the_session_is_marked_as_running_a_card_turn_for_its_whole_length(tmp_path):
    """The spawn gate reads this mark, not the session's approval policy (other writers
    rewrite that)."""
    state, client = _runner_state(tmp_path)
    slot = _trusting_slot("trust")
    key = chat_runner.effective_session_key(slot)
    seen: list[bool] = []

    async def _stream(*_a, **_kw):
        seen.append(card_send.card_turn_running(key))
        yield _complete()

    client.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    slot._empty_response_retries = 2
    assert card_send.card_turn_running(key) is False

    await chat_runner._run_chat(state, slot, _CARD_TEXT, _card_origin=True)
    await _settle(slot)

    assert seen == [True]
    assert card_send.card_turn_running(key) is False


@pytest.mark.asyncio
async def test_a_typed_turn_is_not_marked(tmp_path):
    state, client = _runner_state(tmp_path)
    slot = _slot()
    key = chat_runner.effective_session_key(slot)
    seen: list[bool] = []

    async def _stream(*_a, **_kw):
        seen.append(card_send.card_turn_running(key))
        yield _complete()

    client.stream = MagicMock(side_effect=lambda *a, **kw: _stream())

    await _drive(state, slot)

    assert seen == [False]


async def _requeued_entry(tmp_path, monkeypatch, *, card: bool):
    """Run one turn that dies before output and return ``(state, slot, requeued entry)``."""
    from chat_test_helpers import _make_state, pin_kiro_cli_harness

    pin_kiro_cli_harness()
    from kiro_crew.acp.client import AcpError

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    slot = state.get_or_create_slot("s-card" if card else "s-typed")
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _die(_msg):
        raise AcpError("ACP process exited (code=-15)")
        yield  # an async generator

    client.stream = _die
    client.stream_command = _die
    client.shutdown = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.reset = AsyncMock()

    async def _no_drain(_state, _slot, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _no_drain)
    kwargs = {"_card_origin": True} if card else {}
    await chat_runner._run_chat(state, slot, "[Dashboard card] go", **kwargs)
    (entry,) = [q for q in slot._queue if q.get("content") == "[Dashboard card] go"]
    return state, slot, entry


@pytest.mark.asyncio
async def test_a_recovery_retry_of_a_card_turn_stays_untrusted(tmp_path, monkeypatch):
    """A card turn that dies before output is requeued; the retry must not lose the mark.

    The requeued entry carries ``cardOrigin``, and the drain hands the retry to
    ``_run_chat`` with ``_card_origin`` set.
    """
    from kiro_crew.dashboard.card_send import CARD_ORIGIN_META_KEY

    state, slot, entry = await _requeued_entry(tmp_path, monkeypatch, card=True)
    assert entry["meta"].get(CARD_ORIGIN_META_KEY) is True

    captured: dict[str, object] = {}

    def _stub_run_chat(_state, _slot, _prompt, **kwargs):
        captured.update(kwargs)

        async def _done():
            return None

        return _done()

    def _fake_spawn(_state, _slot, coro):
        coro.close()
        task = MagicMock()
        task.done.return_value = True
        return task

    # Undo the no-drain stub the helper left, so the real drain runs.
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", _REAL_DRAIN)
    monkeypatch.setattr(chat_runner, "_run_chat", _stub_run_chat)
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", _fake_spawn)
    slot.task = None
    assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert captured.get("_card_origin") is True


@pytest.mark.asyncio
async def test_a_recovery_retry_of_a_typed_turn_carries_no_mark(tmp_path, monkeypatch):
    """The control: an ordinary retry is unchanged."""
    from kiro_crew.dashboard.card_send import CARD_ORIGIN_META_KEY

    _, _, entry = await _requeued_entry(tmp_path, monkeypatch, card=False)
    assert CARD_ORIGIN_META_KEY not in entry["meta"]


@pytest.mark.asyncio
async def test_the_mark_is_released_even_when_the_exit_tail_is_cancelled(tmp_path):
    """A cancel the tail re-raises (``run_to_completion`` defers it, then raises it) must
    not leave the session marked, or every later spawn on the thread would be refused."""
    import asyncio

    state, _ = _runner_state(tmp_path)
    slot = _slot()
    key = chat_runner.effective_session_key(slot)

    async def _turn(_state, _slot, _turn_exit, *_args, **_kwargs):
        assert card_send.card_turn_running(key) is True

    async def _cancelled_tail(*_args, **_kwargs):
        raise asyncio.CancelledError

    guarded = chat_runner._hands_off_queue_on_exit(_turn)
    with patch.object(chat_runner, "_end_turn_tail", _cancelled_tail):
        with pytest.raises(asyncio.CancelledError):
            await guarded(state, slot, _card_origin=True)

    assert card_send.card_turn_running(key) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("card_row", [True, False])
async def test_regenerating_a_card_reply_reruns_it_as_a_card_turn(tmp_path, card_row):
    """Regenerate re-sends the stored user row. A card's row stays a card's turn."""
    from chat_regenerate_cov80_helpers import _client
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    meta = {card_send.CARD_ORIGIN_META_KEY: True} if card_row else None
    slot.append("user", _CARD_TEXT, "msg msg-u", meta=meta)
    slot.append("assistant", "the reply")
    slot.drain()
    seen: dict = {}

    async def _capture(_state, _slot, _message, **kwargs) -> None:
        seen.update(kwargs)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 200
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    assert seen.get("_card_origin", False) is card_row
    assert seen["_directive_user_origin"] is (not card_row)
