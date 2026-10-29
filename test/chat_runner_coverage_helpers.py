"""Shared builders for the ``kiro_crew.dashboard.chat_runner`` coverage tests.

``test_chat_runner_coverage.py`` and its ``test_chat_runner_coverage_*.py`` siblings
import from here, and ``test_chat_runner_coverage.py`` re-exports the names other
suites import from it. ``_state`` builds a dashboard state over mocked sessions and
``_runner_state`` adds a scripted client for a ``_run_chat`` turn; ``_slot`` builds a
chat slot. ``_complete``, ``_permission`` and the coding-tool builders make the
``LLMEvent``s a stream yields, ``_set_stream`` scripts the stream, and ``_drive`` runs
exactly one turn and ``_settle`` the follow-up it may dispatch. ``_quiet_sel`` stubs
the SEL audit sink, ``_answer_card`` answers a pending approval card,
``_write_recorder`` is a transport writer that runs the caller's write check, and
``_errors`` lists a slot's error rows.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import LLMEvent

# ── Shared helpers ────────────────────────────────────────────────────────


def _write_recorder(order: list[str]):
    """A transport ``_send_response`` that runs the caller's write check first.

    It follows the framing contract: the check runs right before the frame is
    written, and a raise means nothing was written.
    """

    def _write(_request_id, _result, *, before_write=None):
        if before_write is not None:
            before_write()
        order.append("sent")

    return _write


def _slot(key: str = "chat-cov-1") -> _ChatSlot:
    slot = _ChatSlot(key)
    # Titled on purpose: the end-of-turn cycle routes titling through
    # title_then_refresh, and a titled slot makes both halves self-guard
    # (_maybe_auto_title no-ops, maybe_refresh_title returns not-due) without
    # a real LLM call.
    slot._titled = True
    return slot


def _state(tmp_path, **kwargs) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.set_slack_link = MagicMock()
    sessions.get_mirror_link = MagicMock(return_value=None)
    sessions.reset = AsyncMock()
    # Returns whether it tore down; False means skip_if_busy refused.
    sessions.discard_conversation = AsyncMock(return_value=True)
    # Production returns None when no session is live for the key. Left as a bare
    # MagicMock it would answer a truthy provider whose has_active_turn() is also
    # truthy, so every busy-probe would read "turn in flight" on an idle state.
    sessions.get_provider = MagicMock(return_value=None)
    sessions.resumable_sid = MagicMock(return_value=None)
    # Production answers a plain string, "" when the allocation selected nothing.
    # Left as a bare MagicMock it would answer a truthy object, which the runner
    # would store as the slot's requested model and hand to the crew-log emitter.
    sessions.allocation_requested_model = MagicMock(return_value="")
    sessions.remove = AsyncMock()
    sessions.record_failure = AsyncMock()
    sessions.remove_if_unclaimed = AsyncMock(return_value=False)
    sessions.check_context_usage = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.refresh_slot_source_status = MagicMock()
    state.broadcast_context_usage = MagicMock()
    return state


async def _async_iter(items):
    for item in items:
        yield item


def _complete(stop_reason: str = "end_turn", **kwargs) -> LLMEvent:
    return LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason, **kwargs)


def _permission(
    title: str = "Running: ls -la",
    tool_input: str = "",
    tool_kind: str = "execute",
    request_id: str = "req-cov-1",
    *,
    tool_call_id: str | None = None,
    is_shell: bool = True,
    tool_name: str = "",
    mcp_server_name: str = "",
    raw_tool_params: dict | None = None,
    mcp_identity_trusted: bool = False,
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        tool_kind=tool_kind,
        tool_input=tool_input,
        request_id=request_id,
        tool_call_id=tool_call_id,
        is_shell=is_shell,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
        raw_tool_params=raw_tool_params,
        mcp_identity_trusted=mcp_identity_trusted,
    )


def _coding_tool_call(
    tool_call_id: str = "tc-wt-1",
    *,
    tool_kind: str = "edit",
    tool_name: str = "fs_write",
    mcp_server_name: str = "",
) -> LLMEvent:
    """A coding-shaped tool call."""
    return LLMEvent(
        kind=EVENT_TOOL_CALL,
        title="Writing the file",
        tool_call_id=tool_call_id,
        tool_kind=tool_kind,
        tool_name=tool_name,
        mcp_server_name=mcp_server_name,
    )


def _statusless_coding_tool_call(tool_call_id: str = "tc-wt-1") -> LLMEvent:
    """A kiro-cli one-way coding call with no wire status."""
    return _coding_tool_call(tool_call_id)


def _coding_tool_result(tool_call_id: str = "tc-wt-1") -> LLMEvent:
    """The completed (tool_final) result frame for a coding tool call."""
    return LLMEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=tool_call_id,
        tool_output="done",
        tool_final=True,
        tool_kind="edit",
        tool_name="fs_write",
    )


def _runner_state(tmp_path, *, hook_store=None, context_builder=None):
    """Return ``(state, client)`` wired for a scripted ``_run_chat`` turn."""
    state = _state(tmp_path)
    client = AsyncMock()
    # The provider's sync accessors must NOT be AsyncMock: _run_chat calls them
    # inline and would otherwise store un-awaited coroutines in the WS payload.
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_usage_unknown = MagicMock(return_value=False)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.last_prompt_stats = None
    client._client = client
    client.exit_code = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state._hook_store = hook_store or MagicMock(fire=AsyncMock(return_value=[]))
    if context_builder is not None:
        state.context_builder = context_builder
    return state, client


def _set_stream(client, events) -> None:
    """Script ``client.stream`` so only the FIRST turn yields *events*.

    A turn that produced no visible assistant text arms the empty-response
    auto-continue, which re-queues the prompt and runs a second turn. With a
    stream that replays unconditionally, that second turn re-processes the same
    permission event and every ``assert_awaited_once`` on the provider counts
    two calls. Later turns therefore complete immediately.
    """
    calls = {"n": 0}

    def _stream(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _async_iter(events)
        return _async_iter([_complete()])

    client.stream = MagicMock(side_effect=_stream)
    client.stream_command = MagicMock(side_effect=_stream)


@contextmanager
def _quiet_sel():
    """Stub the SEL audit sink so a turn does not write an audit trail."""
    with patch.object(chat_runner, "sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield mock_sel


async def _drive(state, slot, message: str = "hello", *, _turn_actor: str = "") -> None:
    """Run exactly one turn and leave no task behind.

    ``_empty_response_retries`` is pre-spent on purpose. A turn that streams no
    assistant text re-queues itself, and the finally block then dispatches a
    SECOND turn through ``spawn_guarded_turn`` — which doubles every
    ``assert_awaited_once`` on the provider and leaves a task running past the
    test's event loop. Starting at the exhausted count sends the empty response
    to its terminal notice branch instead, so one call is one turn.
    """
    slot._empty_response_retries = 2
    with _quiet_sel():
        await chat_runner._run_chat(state, slot, message, _turn_actor=_turn_actor)
    await _settle(slot)


def _answer_card(slot, outcome: str) -> None:
    """Answer the pending approval card, as a person clicking it would."""
    future = slot._approval_futures.get("req-cov-1")
    if future is not None and not future.done():
        future.set_result(outcome)


async def _settle(slot) -> None:
    """Await (or cancel) any follow-up turn the finally block dispatched."""
    task = slot.task
    if task is None or not hasattr(task, "cancel"):
        return
    if not task.done():
        task.cancel()
    try:
        # flake-ok: pre-existing await of the follow-up turn task once it is cancelled, moved verbatim; tracked for a de-flake pass
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # pragma: no cover — draining, never the assertion
        pass


def _errors(slot) -> list[str]:
    return [m.get("content", "") for m in slot.messages if m.get("role") == "error"]
