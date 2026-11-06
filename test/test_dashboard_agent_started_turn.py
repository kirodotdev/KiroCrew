"""A turn the agent starts on its own between a chat's prompts, on the dashboard.

The transport half (the idle reader, ``stream_unsolicited``) is
``test_acp_client_agent_started_turns.py``. This file covers what the dashboard
does with it: the provider's watch, the slot claim in ``chat_turn/agent_started.py``
and the ``_run_chat(_agent_started=True)`` turn. Design record:
``docs/request-for-change/rfc-agent-started-turns.md``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_regenerate_cov80_helpers import _client as _regen_client
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from dashboard_approval_helpers import (
    _answer_approval,
    _async_iter,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _permission_event,
)

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied
from kiro_crew.acp.types import STOP_REASON_STALE_RECOVER
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_turn import turn_ledgers
from kiro_crew.dashboard.chat_utils import TRANSIENT_GIVE_UP_TEXT, TRANSIENT_RETRY_KIND
from kiro_crew.hooks import ToolHookResult
from kiro_crew.providers import acp_turns
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.recovery.ladder import InfraError
from kiro_crew.shared_crew import turn_gate
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

# A credential-shaped string the agent's own reply carries; it must be stored redacted.
_SECRET = "AKIA" + "Z" * 16
_ORIGIN = {"kind": "agent"}


def _text(text: str) -> LLMEvent:
    return LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)


def _agent_turn_client(state, client: AsyncMock, events: list[LLMEvent]) -> None:
    """Make *client* a session holding one agent-started turn made of *events*."""
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    client.agent_turn_pending = MagicMock(return_value=True)
    client.stream_unsolicited = MagicMock(side_effect=lambda *a, **kw: _async_iter(events))
    client.stream = MagicMock(side_effect=AssertionError("an agent-started turn sends no prompt"))


async def _run_agent_turn(state, slot) -> None:
    await _run_chat(
        state,
        slot,
        "",
        _agent_started=True,
        _turn_actor=chat_runner.AGENT_STARTED_ACTOR,
        _synthetic_payload=True,
    )
    if slot.task:
        await slot.task


def _rows(slot, role: str) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == role]


# ── the turn itself ──


@pytest.mark.asyncio
async def test_the_turn_shows_its_permission_card_and_its_reply_on_its_own_row(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder(ToolHookResult.allow()))
    slot = _make_slot()
    _agent_turn_client(
        state,
        client,
        [
            _text(f"The background build finished. Key {_SECRET}."),
            _permission_event(title="fs_write", tool_kind="edit"),
            _complete_event(),
        ],
    )
    approver = asyncio.get_running_loop().create_task(_answer_approval(slot, "req-1", "approved"))

    with _patch_stats(), patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
        await _run_agent_turn(state, slot)
    await _drain(approver)

    client.stream_unsolicited.assert_called_once()
    client.approve_tool.assert_called_once()
    assert client.approve_tool.call_args.args[0] == "req-1"
    decisions = [
        c.kwargs
        for c in mock_sel.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("request_id") == "req-1"
    ]
    assert len(decisions) == 1
    # No user row: nobody sent this turn a message.
    assert _rows(slot, "user") == []
    [reply] = _rows(slot, "assistant")
    assert "The background build finished." in reply["content"]
    assert _SECRET not in reply["content"]
    assert reply["meta"]["origin"] == _ORIGIN
    # The permission card rendered as on a user turn.
    assert any(m.get("role") == "permission" for m in slot.messages)


@pytest.mark.asyncio
async def test_the_next_user_bubble_holds_only_its_own_reply(tmp_path):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [_text("it finished, output: done"), _complete_event()])
    with _patch_stats():
        await _run_agent_turn(state, slot)

    client.stream = MagicMock(
        side_effect=lambda *a, **kw: _async_iter([_text("All quiet."), _complete_event()])
    )
    with _patch_stats():
        await _run_chat(state, slot, "status?")
        if slot.task:
            await slot.task

    agent_reply, user_reply = _rows(slot, "assistant")
    assert agent_reply["content"] == "it finished, output: done"
    assert agent_reply["meta"]["origin"] == _ORIGIN
    assert user_reply["content"] == "All quiet."
    assert "origin" not in (user_reply.get("meta") or {})


@pytest.mark.asyncio
async def test_a_turn_with_no_reply_queues_no_prompt_and_counts_as_no_loop_cycle(tmp_path):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [_complete_event()])

    with _patch_stats(), patch.object(chat_runner, "_notify_autonudge_turn_end") as notify:
        await _run_agent_turn(state, slot)

    # The empty-response ladder would replay or continue a prompt; this turn had none.
    assert slot._queue == []
    client.stream.assert_not_called()
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_pressed_while_the_turn_is_prepared_cancels_the_agents_cycle(tmp_path):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [_text("The background build"), _complete_event()])
    client.cancel = AsyncMock(return_value="acked")
    prepare = chat_runner._turn_dispatch_prep

    async def _stop_meanwhile(turn) -> None:
        await prepare(turn)
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

    with _patch_stats(), patch.object(chat_runner, "_turn_dispatch_prep", new=_stop_meanwhile):
        await _run_agent_turn(state, slot)

    # That Stop found no open turn; the cycle runs natively all the same, so the
    # turn opens its stream and sends the cancel once, at the first event.
    client.stream_unsolicited.assert_called_once()
    client.cancel.assert_awaited_once()
    client.stream.assert_not_called()
    # Opened only to cancel, so Resume all does not read it as a turn that ran.
    assert slot._turns_started == 0


@pytest.mark.asyncio
async def test_after_the_owed_cancel_a_permission_ask_opens_no_card(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder(ToolHookResult.allow()))
    slot = _make_slot()
    _agent_turn_client(
        state,
        client,
        [_permission_event(title="fs_write", tool_kind="edit"), _complete_event()],
    )
    client.cancel = AsyncMock(return_value="acked")
    prepare = chat_runner._turn_dispatch_prep

    async def _stop_meanwhile(turn) -> None:
        await prepare(turn)
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1

    with _patch_stats(), patch.object(chat_runner, "_turn_dispatch_prep", new=_stop_meanwhile):
        await await_until(
            asyncio.ensure_future(_run_agent_turn(state, slot)),
            what="the cancelled agent-started turn's end",
        )

    # The cancel already answered that ask as cancelled, so no card waits on a
    # person and the turn reads on to its end.
    client.cancel.assert_awaited_once()
    client.approve_tool.assert_not_called()
    assert not any(m.get("role") == "permission" for m in slot.messages)


def _dies(exc: Exception, *, after_text: bool = True):
    async def _events(*_a, **_kw):
        if after_text:
            yield _text("The background build")
        raise exc
        yield  # an async generator, even when it raises first

    return _events


@pytest.mark.parametrize(
    ("stream", "budget", "given_up"),
    [
        (
            _dies(AcpProcessDied("Process exited during prompt")),
            None,
            gn.GATEWAY_CONNECTION_LOST_GIVE_UP_TEXT,
        ),
        (
            lambda *_a, **_kw: _async_iter(
                [
                    _text("The background build"),
                    LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_STALE_RECOVER),
                ]
            ),
            "_stale_recovery_retries",
            gn.GATEWAY_TURN_STALLED_GIVE_UP_TEXT,
        ),
        (
            _dies(AcpError("upstream 503", transient=True), after_text=False),
            "_transient_5xx_retries",
            None,
        ),
    ],
    ids=["process-died", "stale-recover", "transient-error"],
)
@pytest.mark.asyncio
async def test_a_failing_turn_gives_up_without_announcing_a_retry(
    tmp_path, stream, budget, given_up
):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [])
    client.stream_unsolicited = MagicMock(side_effect=stream)

    with _patch_stats():
        await _run_agent_turn(state, slot)

    # Every retry arm continues or replays a prompt; this turn had none, so it
    # takes the nested-turn give-up: no retry row, no queued retry, no budget spent.
    assert slot._queue == []
    client.stream.assert_not_called()
    assert not any((m.get("meta") or {}).get("kind") == TRANSIENT_RETRY_KIND for m in slot.messages)
    if budget is not None:
        assert getattr(slot, budget) == 0
    shown = [m for m in slot.messages if m.get("role") != "done"]
    assert shown[-1]["role"] == "error"
    if given_up is not None:
        assert shown[-1]["content"] == given_up


@pytest.mark.asyncio
async def test_a_declined_recovery_nobody_announced_posts_no_give_up_row(tmp_path):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [_text("Done."), _complete_event()])

    def _declining_arm(turn) -> None:
        # A Stop hook's block reason asks for a continuation and posts no row.
        assert turn_ledgers._queue_recovery(turn, 0, "keep going", kind="hook") == ""

    with (
        _patch_stats(),
        patch.object(chat_runner, "_turn_stop_hook", new=AsyncMock(side_effect=_declining_arm)),
    ):
        await _run_agent_turn(state, slot)

    assert slot._queue == []
    assert not any(m.get("content") == TRANSIENT_GIVE_UP_TEXT for m in slot.messages)


@pytest.mark.parametrize(
    ("promise_retries", "infra_error"),
    [
        (0, None),
        (1, None),
        (0, InfraError(error_class="capacity", retry_after_secs=0.0)),
    ],
    ids=["promise-only", "promise-budget-spent", "capacity-refused-tool"],
)
@pytest.mark.asyncio
async def test_a_promise_only_reply_lands_as_it_stands(tmp_path, promise_retries, infra_error):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    slot._promise_only_retries = promise_retries
    _agent_turn_client(state, client, [_text("I'll check the logs now."), _complete_event()])
    client.last_infra_error = infra_error

    with _patch_stats():
        await _run_agent_turn(state, slot)

    # The promise-only guard and the capacity retry continue a prompt; this turn
    # had none, so it posts no retry or give-up row and spends none of the
    # promise-only budget (a landed turn hands it back).
    assert slot._queue == []
    client.stream.assert_not_called()
    assert slot._promise_only_retries == 0
    assert not any(
        m.get("role") in ("notice", "error") for m in slot.messages if m.get("role") != "done"
    ), slot.messages
    [reply] = _rows(slot, "assistant")
    assert reply["content"] == "I'll check the logs now."


@pytest.mark.asyncio
async def test_the_reply_reaches_the_linked_slack_thread_without_an_echo(tmp_path):
    state, client = _make_state(tmp_path)
    state.slack_client = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=("111.222", "C123"))
    slot = _make_slot()
    _agent_turn_client(state, client, [_text("it finished, output: done"), _complete_event()])

    with _patch_stats():
        await _run_agent_turn(state, slot)

    state.sessions.get_slack_link.assert_called()
    posted = [c.args for c in state.slack_client.post_message.await_args_list]
    assert posted and all(args[0] == "C123" for args in posted)
    assert not any(str(args[1]).startswith(">") for args in posted)


@pytest.mark.asyncio
async def test_a_turn_a_prompt_already_read_ends_before_it_opens(tmp_path):
    state, client = _make_state(tmp_path)
    slot = _make_slot()
    _agent_turn_client(state, client, [_text("never read"), _complete_event()])
    client.agent_turn_pending = MagicMock(return_value=False)

    with _patch_stats():
        await _run_agent_turn(state, slot)

    client.stream_unsolicited.assert_not_called()
    assert _rows(slot, "assistant") == []


# ── claiming the slot ──


def _registered(state, slot, provider) -> None:
    state._slots[slot.key] = slot
    state.sessions.get_provider = MagicMock(return_value=provider)
    provider.agent_turn_pending = MagicMock(return_value=True)


def _capture_spawns(started: list) -> MagicMock:
    def _spawn(_state, _slot, coro):
        started.append(coro)
        coro.close()
        return asyncio.get_running_loop().create_future()

    return MagicMock(side_effect=_spawn)


@pytest.mark.asyncio
async def test_the_turn_is_admitted_on_its_own_and_a_send_meanwhile_cannot_cancel_it(tmp_path):
    state, _client = _make_state(tmp_path)
    slot = _make_slot()
    provider = MagicMock()
    _registered(state, slot, provider)
    admitted: list[str] = []

    def _spawn(_state, _slot, _turn):
        admitted.append(turn_gate._ADMITTED_WORK.get())
        return asyncio.get_running_loop().create_future()

    run_chat = MagicMock(return_value=None)
    # The idle reader runs in the finished turn's context, admitted work and all.
    with turn_gate.mark_admitted("work-of-the-previous-turn"):
        with (
            patch.object(chat_runner, "spawn_guarded_turn", MagicMock(side_effect=_spawn)),
            patch.object(chat_runner, "_run_chat", run_chat),
        ):
            chat_runner._on_agent_turn(state, slot, provider)

    assert admitted == [""]
    assert run_chat.call_args.kwargs["_responsive_protected"] is True


@pytest.mark.asyncio
async def test_an_idle_slot_is_claimed_before_the_callback_returns(tmp_path):
    state, _client = _make_state(tmp_path)
    slot = _make_slot()
    provider = MagicMock()
    _registered(state, slot, provider)
    started: list = []

    with patch.object(chat_runner, "spawn_guarded_turn", _capture_spawns(started)):
        chat_runner._on_agent_turn(state, slot, provider)

    assert len(started) == 1
    # A message sent now finds the slot running and queues behind the turn.
    assert slot.running is True
    state.push_slots_update.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_took_it", [False, True])
async def test_a_slot_still_ending_its_turn_starts_the_agent_turn_when_that_task_ends(
    tmp_path, prompt_took_it: bool
):
    state, _client = _make_state(tmp_path)
    slot = _make_slot()
    provider = MagicMock()
    _registered(state, slot, provider)
    ending = asyncio.get_running_loop().create_future()
    slot.task = ending
    started: list = []

    with patch.object(chat_runner, "spawn_guarded_turn", _capture_spawns(started)):
        chat_runner._on_agent_turn(state, slot, provider)
        assert started == []
        provider.agent_turn_pending.return_value = not prompt_took_it
        ending.set_result(None)
        # The retry re-reads whether the turn is still pending, on the loop's next pass.
        await async_wait_until(lambda: provider.agent_turn_pending.call_count == 2)

    assert len(started) == (0 if prompt_took_it else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["slot_replaced", "session_replaced", "nothing_pending"])
async def test_nothing_starts_when_the_turn_no_longer_belongs_to_the_slot(tmp_path, case: str):
    state, _client = _make_state(tmp_path)
    slot = _make_slot()
    provider = MagicMock()
    _registered(state, slot, provider)
    if case == "slot_replaced":
        state._slots[slot.key] = _make_slot(slot.key)
    elif case == "session_replaced":
        state.sessions.get_provider.return_value = MagicMock()
    else:
        provider.agent_turn_pending.return_value = False
    started: list = []

    with patch.object(chat_runner, "spawn_guarded_turn", _capture_spawns(started)):
        chat_runner._on_agent_turn(state, slot, provider)

    assert started == []
    assert slot.task is None


@pytest.mark.parametrize(("switch", "registers"), [(True, True), (False, False), (None, False)])
def test_the_kill_switch_decides_whether_the_watch_is_registered(switch, registers: bool):
    client = MagicMock(spec=AcpProvider)
    cfg = (
        None
        if switch is None
        else SimpleNamespace(agent=SimpleNamespace(agent_started_turns=switch))
    )

    chat_runner._watch_agent_turns(MagicMock(), _make_slot(), client, cfg)

    [call] = client.watch_agent_turns.call_args_list
    assert callable(call.args[0]) is registers
    if not registers:
        assert call.args[0] is None


def test_a_session_another_provider_serves_registers_no_watch():
    client = MagicMock()
    cfg = SimpleNamespace(agent=SimpleNamespace(agent_started_turns=True))

    chat_runner._watch_agent_turns(MagicMock(), _make_slot(), client, cfg)

    assert client.watch_agent_turns.call_args_list == []


# ── Regenerate never re-runs a prompt over a reply the agent started ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        # The agent's reply is the newest one.
        [("user", "Q", None), ("assistant", "A", None), ("assistant", "B", {"origin": _ORIGIN})],
        # A prompted reply follows it: the prompt queued behind the agent's turn.
        [("user", "Q", None), ("assistant", "B", {"origin": _ORIGIN}), ("assistant", "A", None)],
    ],
)
async def test_regenerate_refuses_a_span_holding_a_reply_the_agent_started(state, rows) -> None:
    slot = state.get_or_create_slot("s1")
    for role, text, meta in rows:
        slot.append(role, text, meta=meta)
    slot.drain()
    before = [dict(m) for m in slot.messages]
    run_chat = AsyncMock()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run_chat):
        async with _regen_client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400
            assert (await resp.json())["code"] == "agent_started_reply"

    run_chat.assert_not_called()
    assert slot.messages == before
    assert not slot._pending_variants


# ── the provider seam, on a real client ──


def _provider(backend: str) -> tuple[SimpleNamespace, AcpClient, asyncio.StreamReader]:
    client = AcpClient(acp_backend=backend)
    if backend == ACP_BACKEND_CLAUDE:
        # The claude adapter's spawn step does this (ClaudeLaunch.resolve_spawn).
        client._install_idle_reader()
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    client._process = proc
    provider = SimpleNamespace(_client=client, _to_llm_event=acp_turns._to_llm_event)
    return provider, client, reader


def _frame(update: dict) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": update},
    }
    return (json.dumps(body) + "\n").encode()


@pytest.mark.asyncio
async def test_a_claude_session_hands_the_turn_and_its_permission_request_to_the_watch():
    provider, client, reader = _provider(ACP_BACKEND_CLAUDE)
    calls: list[str] = []
    assert acp_turns.watch_agent_turns(provider, lambda: calls.append("turn")) is True

    client._maybe_start_idle_reader()
    reader.feed_data(
        _frame(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}
        )
    )
    permission = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {"toolCallId": "t1", "title": "fs_write"},
            "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}],
        },
    }
    reader.feed_data((json.dumps(permission) + "\n").encode())
    reader.feed_data(
        _frame(
            {
                "sessionUpdate": "usage_update",
                "used": 10,
                "size": 100,
                "_meta": {"_claude/origin": {"kind": "task-notification"}},
            }
        )
    )
    await async_wait_until(lambda: calls == ["turn"])
    assert acp_turns.agent_turn_pending(provider) is True

    async def _read() -> list[str]:
        return [
            e.kind async for e in acp_turns.stream_unsolicited(provider, timeout=default_timeout())
        ]

    with patch.object(acp_turns, "record_live_children_after_turn", AsyncMock()):
        kinds = await await_until(
            asyncio.ensure_future(_read()), what="the agent-started turn's events"
        )

    assert EVENT_TEXT_CHUNK in kinds
    assert EVENT_PERMISSION_REQUEST in kinds
    assert kinds[-1] == EVENT_COMPLETE
    assert acp_turns.agent_turn_pending(provider) is False

    assert acp_turns.watch_agent_turns(provider, None) is True
    assert client.on_agent_turn is None
    await client._stop_idle_reader()


@pytest.mark.asyncio
async def test_a_kiro_session_registers_no_watch_and_never_reads_between_turns():
    provider, client, _reader = _provider(ACP_BACKEND_KIRO)

    assert acp_turns.watch_agent_turns(provider, lambda: None) is False

    assert client.on_agent_turn is None
    assert acp_turns.agent_turn_pending(provider) is False
    client._maybe_start_idle_reader()
    assert client._idle_reader is None


@pytest.mark.asyncio
async def test_a_session_that_is_not_a_direct_client_registers_no_watch():
    provider = SimpleNamespace(_client=MagicMock(), _to_llm_event=acp_turns._to_llm_event)

    assert acp_turns.watch_agent_turns(provider, lambda: None) is False
    assert acp_turns.agent_turn_pending(provider) is False
    assert [e async for e in acp_turns.stream_unsolicited(provider)] == []
