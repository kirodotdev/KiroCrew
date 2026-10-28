"""A sub-agent's turn fires the Stop hooks when it ends, and honours a block decision.

The dashboard fires ``Stop`` (Hooks-page hooks and, on a backend that cannot run
them, the agent spec's own) at every turn end. A child run fires the same event
when its stream ends, so a Stop gate cannot be stepped around by delegating the
work to a sub-agent: a hook's ``{"decision": "block", "reason": ...}`` sends the
reason to the child as its next prompt, under ``agent.max_stop_hook_nudges``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.dashboard.state_parts.injected_turns import HOOK_CONTINUATION_RECOVERY_PREFIX
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hook_runtime.script_hooks import ScriptHookResult

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _block(reason: str) -> list[ScriptHookResult]:
    return [
        ScriptHookResult(
            hook_id="gate",
            hook_name="gate",
            event="Stop",
            stdout=json.dumps({"decision": "block", "reason": reason}),
            exit_code=0,
        )
    ]


async def _run_child(replies: list[str], stop_results: list[list[ScriptHookResult]], **cfg):
    """Run one child whose every prompt answers with the next reply, and return
    (the prompts it was sent, the Stop fire calls, the finished info)."""
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    prompts: list[str] = []
    # How many earlier prompts' streams had run their cleanup when each prompt
    # started: a provider stream's ``finally`` clears the turn's active mark.
    closed_at_start: list[int] = []
    closed = [0]

    async def _stream(msg, *_a, **_kw):  # type: ignore[no-untyped-def]
        closed_at_start.append(closed[0])
        prompts.append(msg)
        try:
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=replies[len(prompts) - 1])
            if not cfg.get("no_complete"):
                stop_reason = (
                    "error: tool stall"
                    if cfg.get("recover_first") and len(prompts) == 1
                    else "end_turn"
                )
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason)
        finally:
            closed[0] += 1

    sessions = _mock_sessions()
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=_stream)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

    ctx = _mock_ctx_builder_auto_spawn()
    ctx.hooks.auto_approve_subagent_tools = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    if cfg.get("recover_first"):
        manager._yield_for_stop_recovery = AsyncMock(
            return_value=cfg.get("recovery_nudge", "continue the interrupted work")
        )
    pending = list(stop_results)

    async def _fire(event, *_a, **_kw):  # type: ignore[no-untyped-def]
        if event == "Stop" and pending:
            if cfg.get("stop_during_hook"):
                info.user_stopped = True
            return pending.pop(0)
        return []

    manager.hook_store = MagicMock()
    manager.hook_store.fire = AsyncMock(side_effect=_fire)

    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="s01",
        task="do the work",
        parent_session_key="dashboard:parent",
    )
    manager._log_spawned(info)
    _cfg = SimpleNamespace(agent=SimpleNamespace(max_stop_hook_nudges=cfg.get("cap", 100)))
    rows: list = cfg.get("rows", [])

    async def _row(_key, _model, ev, *_a, **_kw):  # type: ignore[no-untyped-def]
        rows.append(ev)

    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.config.KiroCrewConfig.load", return_value=_cfg),
        patch(
            "kiro_crew.dashboard.handlers.usage.persist_token_record_async",
            new=AsyncMock(side_effect=_row),
        ),
    ):
        await manager._run_inner(info, "subagent:s01")
        await asyncio.gather(*list(manager._report_tasks), return_exceptions=True)
    stops = [c for c in manager.hook_store.fire.await_args_list if c.args[0] == "Stop"]
    # Every earlier prompt's stream had closed before the next prompt began.
    assert closed_at_start == list(range(len(prompts)))
    return prompts, stops, info


@pytest.mark.asyncio
async def test_a_child_turn_fires_stop_with_its_final_text() -> None:
    prompts, stops, info = await _run_child(["all done"], [])

    assert len(prompts) == 1
    assert len(stops) == 1, "the child's turn end must fire Stop exactly once"
    call = stops[0]
    assert call.args[1] == "all done"
    assert call.kwargs["session_key"] == "subagent:s01"
    assert call.kwargs["subagent_id"] == "s01"
    assert call.kwargs["parent_session_key"] == "dashboard:parent"
    assert call.kwargs["hook_continuation_count"] == 0
    assert "extra_hooks" in call.kwargs
    assert info.result == "all done"


@pytest.mark.asyncio
async def test_a_stop_block_decision_continues_the_child_with_its_reason() -> None:
    prompts, stops, info = await _run_child(
        ["first try", "tests pass now"], [_block("run the tests first")]
    )

    assert len(prompts) == 2
    assert prompts[1] == f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nrun the tests first"
    assert [c.kwargs["hook_continuation_count"] for c in stops] == [0, 1]
    # The second Stop sees only the continuation's own answer.
    assert stops[1].args[1] == "tests pass now"
    # The parent gets both answers, a blank line apart, never glued together.
    assert info.result == "first try\n\ntests pass now"
    with open(info.result_path, encoding="utf-8") as fh:
        assert fh.read() == "first try\n\ntests pass now"


@pytest.mark.asyncio
async def test_a_recovery_continuation_stop_sees_only_its_own_answer() -> None:
    prompts, stops, info = await _run_child(
        ["work before the stall", "DONE"], [], recover_first=True
    )

    assert len(prompts) == 2
    assert [call.args[1] for call in stops] == ["work before the stall", "DONE"]
    assert info.result == "work before the stallDONE"


@pytest.mark.asyncio
async def test_the_nudge_cap_ends_an_always_block_loop() -> None:
    prompts, stops, _info = await _run_child(
        ["a", "b", "c"], [_block("again"), _block("again"), _block("again")], cap=1
    )

    assert len(prompts) == 2, "cap=1 allows one continuation, then the run ends"
    assert len(stops) == 2


@pytest.mark.asyncio
async def test_a_stop_pressed_while_the_hook_runs_wins_over_its_block() -> None:
    prompts, stops, _info = await _run_child(
        ["a", "b"], [_block("keep going")], stop_during_hook=True
    )

    assert len(prompts) == 1, "a user stop must never be overridden by a Stop hook"
    assert len(stops) == 1


@pytest.mark.asyncio
async def test_each_prompt_a_stop_hook_continues_gets_its_own_usage_row() -> None:
    rows: list = []
    prompts, _stops, _info = await _run_child(
        ["first try", "done"], [_block("run the tests first")], rows=rows
    )

    assert len(prompts) == 2
    # One row per answered prompt, so the continued prompt's spend is recorded.
    assert len(rows) == 2
    assert all(getattr(ev, "kind", None) == EVENT_COMPLETE for ev in rows)
    assert rows[0] is not rows[1]


@pytest.mark.asyncio
async def test_a_continued_prompt_with_no_completion_event_still_gets_a_usage_row() -> None:
    # A provider may end a stream with no completion event; that prompt is
    # still settled and recorded before the continuation resets the baseline.
    rows: list = []
    prompts, stops, _info = await _run_child(
        ["first try", "done"], [_block("again")], rows=rows, no_complete=True
    )

    assert len(prompts) == 2
    assert len(stops) == 2
    assert len(rows) == 2
