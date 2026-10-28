"""A sub-agent turn that ran tools and closed without text is a partial result.

A successful turn with no text records ``"_No response._"``. When the run made
no tool call that is all there is to say: nothing happened, and the parent can
send the task again. When it did, the tool calls' side effects already landed,
so a bare "_No response._" beside a completed run invites the parent to spawn
it again and run them twice. The run then flags the result partial and appends
the dashboard's own continue-never-resend sentence
(``messaging.empty_turn_copy.EMPTY_TURN_NOTICE_AFTER_WORK``), both in memory and
in ``result.txt``, which ``spawn_status`` reads for a done run. The outcome
stays completed: the turn ended normally.

The run tests drive the real ``SubagentManager`` path with only the ACP session
provider faked (the seam ``test_subagent_stop_reason_consistency`` uses).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
    open_idle_tab,
)
from subagent_scale_helpers import _make_orchestrator, _mock_dashboard_state
from subagent_scale_helpers import _mock_sessions as _scale_sessions
from subagent_stop_reason_helpers import (
    _complete,
    _done_event,
    _manager,
    _mock_sessions,
    _spawn_and_wait,
    _spy_events,
    _text,
)
from test_subagent_leaked_tool_call_note import _permission_request, _tool_call

import kiro_crew.subagent_persistence as sp
from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE_AFTER_WORK
from kiro_crew.subagent import SubagentInfo
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _stream(*items: Any):
    """One stream call: each int is a tool call, ``"perm"`` the run's own
    permission request, any other str a text chunk, then a normal end of turn."""

    def stream_factory(msg: str, *a, **kw):
        async def _gen():
            for item in items:
                if isinstance(item, int):
                    yield _tool_call(item)
                elif item == "perm":
                    yield _permission_request()
                else:
                    yield _text(item)
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    return stream_factory


async def _run(*items: Any) -> tuple[SubagentInfo, dict]:
    mgr = _manager(_mock_sessions(_stream(*items)))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    return info, _done_event(events)


def _result_file(info: SubagentInfo) -> str | None:
    path = sp._agent_dir(info.id) / "result.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


@pytest.mark.asyncio
async def test_an_empty_turn_after_tool_calls_is_partial_and_says_to_continue():
    info, done = await _run(1, 2)
    assert info.tool_count == 2
    assert info.outcome == "completed" and info.error == ""
    assert info.partial is True and done["partial"] is True
    assert info.result == f"_No response._\n\n{EMPTY_TURN_NOTICE_AFTER_WORK}"
    assert _result_file(info) == info.result


@pytest.mark.asyncio
async def test_a_whitespace_only_turn_after_a_tool_call_is_empty_too():
    info, done = await _run(1, "\n  \n")
    assert info.partial is True and done["partial"] is True
    assert info.result == f"_No response._\n\n{EMPTY_TURN_NOTICE_AFTER_WORK}"


@pytest.mark.asyncio
async def test_a_call_surfaced_only_as_a_permission_request_counts_as_work():
    """A backend may surface a gated call ONLY as the run's own permission
    request, with no tool_call frame: that call ran too."""
    info, done = await _run("perm")
    assert info.tool_count == 0 and info.turns == 1
    assert info.partial is True and done["partial"] is True
    assert info.result == f"_No response._\n\n{EMPTY_TURN_NOTICE_AFTER_WORK}"


@pytest.mark.asyncio
async def test_an_options_only_reply_after_a_tool_call_is_empty_too():
    """The [OPTIONS: ...] line is stripped from a sub-agent result, so a reply
    that was only that line leaves no reply."""
    info, done = await _run(1, "[OPTIONS: Yes | No]")
    assert info.partial is True and done["partial"] is True
    assert info.result.endswith(EMPTY_TURN_NOTICE_AFTER_WORK)


@pytest.mark.asyncio
async def test_an_empty_turn_with_no_tool_call_stays_a_plain_no_response():
    info, done = await _run()
    assert info.tool_count == 0
    assert info.outcome == "completed"
    assert info.partial is False and done["partial"] is False
    assert info.result == "_No response._"
    assert EMPTY_TURN_NOTICE_AFTER_WORK not in info.result
    assert _result_file(info) is None


@pytest.mark.asyncio
async def test_a_turn_that_answers_after_tool_calls_is_not_flagged():
    info, done = await _run(1, "NONCE=42\n")
    assert info.partial is False and done["partial"] is False
    assert info.result == "NONCE=42\n"
    assert _result_file(info) == "NONCE=42\n"


@pytest.mark.asyncio
async def test_the_wave_digest_names_an_empty_turn_after_work_as_its_cause():
    """The digest's partial label for a completed member says why it is
    partial: a member that called tools and then closed with no reply is not
    a backend generate failure, and the generate-failure member keeps its label."""
    orch = _make_orchestrator()
    orch.sessions = _scale_sessions()
    orch.ctx_builder = MagicMock()
    orch.ctx_builder.hooks = MagicMock()
    orch.dashboard_state = _mock_dashboard_state()
    slot = MagicMock()
    open_idle_tab(slot)
    slot.mode = "chat"
    slot.running = False
    slot.task = None
    slot._subagent_deliveries_inflight = 0
    orch.dashboard_state.get_slot = MagicMock(return_value=slot)
    with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
        with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
            mgr = MagicMock()
            mgr.start_reaper = MagicMock()
            mgr.wave_has_live_nested_spawns = MagicMock(return_value=False)
            mock_sm.return_value = mgr
            orch._init_subagents()
            orch.subagent_mgr = mgr
            on_done = mock_sm.call_args.kwargs["on_done"]

    def _member(i: int, result: str) -> SubagentInfo:
        info = SubagentInfo(
            id=f"e{i}",
            task=f"wave task {i}",
            parent_session_key="dashboard:main",
            batch_id="emptywave",
            batch_total=2,
        )
        info.done = True
        info.partial = True
        info.result = result
        info.result_path = f"/tmp/e{i}/result.txt"
        info.elapsed = 10.0
        info.credits = 0.25
        return info

    empty = _member(0, f"_No response._\n\n{EMPTY_TURN_NOTICE_AFTER_WORK}")
    kept = _member(1, "_Warning: the backend failed to generate a final response._\n\nstreamed")
    injected: list[str] = []

    async def _fake_run_chat(_state, _slot, text, *, _directive_user_origin, **_kw):
        injected.append(text)

    with (
        patch("kiro_crew.dashboard.chat_runner._run_chat", side_effect=_fake_run_chat),
        patch("kiro_crew.subagent_persistence.mark_delivered"),
    ):
        mgr.batch_members_pending = MagicMock(return_value=True)
        await on_done(empty)
        await asyncio.sleep(0)
        mgr.batch_members_pending = MagicMock(return_value=False)
        await on_done(kept)
        await async_wait_until(
            lambda: len(injected) >= 1, timeout=5.0, describe=lambda: "the wave digest injected"
        )

    lines = "\n".join(injected).splitlines()
    empty_lines = [line for line in lines if f"`{empty.id}`" in line]
    assert any(
        "✅ (partial: ended without a closing reply after tool calls)" in line
        for line in empty_lines
    )
    assert not any("backend failed to generate" in line for line in empty_lines)
    assert any(
        f"`{kept.id}` ✅ (partial: backend failed to generate the final response)" in line
        for line in lines
    )
