"""What ``SubagentManager``'s run loop does with the provider stream.

This file pins the PostToolUse hook and parallel tool-call attribution, the usage row and
credit accounting of a turn that completes, fails, is cancelled or retries, and how a
child's MCP permission events are answered: identity-verified grants and the escalation
bound. The family's other files are described in ``test_subagent.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_helpers import (
    _mock_ctx_builder_auto_spawn,
    _mock_sessions,
)

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


class TestSubagentPostToolUseHook:
    """PostToolUse hook fires on EVENT_TOOL_RESULT in subagent loop.

    Without this branch, hooks registered for subagent-spawned tool calls
    received PreToolUse but never PostToolUse — losing tool_response payloads.
    """

    @pytest.mark.asyncio
    async def test_post_tool_use_fires_on_tool_result(self) -> None:
        """PostToolUse hook is called with tool_name + tool_response after EVENT_TOOL_RESULT."""
        from kiro_crew.providers.base import (
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Running: ReadFile",
                tool_call_id="t-1",
                tool_input='{"path": "/tmp/x"}',
                tool_kind="builtin",
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="t-1",
                tool_output="hello world",
            )

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.auto_approve_subagent_tools = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        manager.hook_store = MagicMock()
        manager.hook_store.fire = AsyncMock()

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="t01",
            task="test",
            parent_session_key="slack:C:T",
        )
        manager._log_spawned(info)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run_inner(info, "subagent:t01")

        # Hook fired twice total — once for PreToolUse (via fire_tool_hooks),
        # once for PostToolUse. Verify the PostToolUse call carried the cached
        # tool_name (with "Running: " stripped) and the tool_response payload.
        post_calls = [
            c
            for c in manager.hook_store.fire.await_args_list
            if c.args and c.args[0] == "PostToolUse"
        ]
        assert len(post_calls) == 1, (
            f"Expected exactly one PostToolUse hook call, got "
            f"{[c.args for c in manager.hook_store.fire.await_args_list]}"
        )
        kwargs = post_calls[0].kwargs
        assert kwargs["tool_name"] == "ReadFile", (
            f"PostToolUse should receive cached tool_name with 'Running: ' "
            f"stripped, got {kwargs['tool_name']!r}"
        )
        assert kwargs["tool_response"] == {"output": "hello world"}
        # The child's own key, for per-session attribution, beside the
        # parent's key that marks the event as a subagent's.
        assert kwargs["session_key"] == "subagent:t01"
        assert kwargs["parent_session_key"] == "slack:C:T"

    @pytest.mark.asyncio
    async def test_post_tool_use_truncates_long_output(self) -> None:
        """PostToolUse output is capped at 2000 chars to bound hook payload size."""
        from kiro_crew.providers.base import (
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        long_output = "x" * 5000

        async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="ReadFile",
                tool_call_id="t-2",
                tool_input="",
                tool_kind="builtin",
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="t-2",
                tool_output=long_output,
            )

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.auto_approve_subagent_tools = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        manager.hook_store = MagicMock()
        manager.hook_store.fire = AsyncMock()

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="t02",
            task="test",
            parent_session_key="slack:C:T",
        )
        manager._log_spawned(info)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run_inner(info, "subagent:t02")

        post_calls = [
            c
            for c in manager.hook_store.fire.await_args_list
            if c.args and c.args[0] == "PostToolUse"
        ]
        assert len(post_calls) == 1
        output = post_calls[0].kwargs["tool_response"]["output"]
        assert len(output) == 2000
        assert output == "x" * 2000

    @pytest.mark.asyncio
    async def test_post_tool_use_no_hook_store_is_noop(self) -> None:
        """When hook_store is None, EVENT_TOOL_RESULT does not raise."""
        from kiro_crew.providers.base import (
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="X", tool_call_id="t-3")
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t-3", tool_output="ok")

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.auto_approve_subagent_tools = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        # Default hook_store is None — explicitly verify no raise.
        assert manager.hook_store is None

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="t03",
            task="test",
            parent_session_key="slack:C:T",
        )
        manager._log_spawned(info)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run_inner(info, "subagent:t03")

        # The stream ran through the tool result and the run ended clean.
        assert info.done is True
        assert not info.error

    @pytest.mark.asyncio
    async def test_post_tool_use_hook_exception_is_swallowed(self) -> None:
        """Hook errors are caught and don't break the subagent stream."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
            yield LLMEvent(kind=EVENT_TOOL_CALL, title="X", tool_call_id="t-4")
            yield LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t-4", tool_output="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.auto_approve_subagent_tools = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        manager.hook_store = MagicMock()
        manager.hook_store.fire = AsyncMock(side_effect=RuntimeError("boom"))

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="t04",
            task="test",
            parent_session_key="slack:C:T",
        )
        manager._log_spawned(info)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run_inner(info, "subagent:t04")

        post_calls = [
            c
            for c in manager.hook_store.fire.await_args_list
            if c.args and c.args[0] == "PostToolUse"
        ]
        assert [c.kwargs["tool_name"] for c in post_calls] == ["X"]
        assert info.done is True
        assert not info.error


class TestSubagentParallelToolAttribution:
    """The run loop keeps a slow parallel call judged after a fast one returns."""

    @pytest.mark.asyncio
    async def test_a_wait_stays_judged_after_a_parallel_write_returns(self) -> None:
        """Wire-level shape of the reported stall: a write and a ``wait`` in one
        turn, the write's completed result first. The wait must remain the
        reaper's in-flight call, and a call the previous turn left open must be
        gone by the time the new prompt streams."""
        from kiro_crew.providers.base import (
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="t05",
            task="test",
            parent_session_key="slack:C:T",
        )
        info._active_tool_calls["stale-1"] = MagicMock()
        seen: dict[str, object] = {}

        async def _stream(*_a, **_kw):  # type: ignore[no-untyped-def]
            seen["stale_at_prompt"] = "stale-1" in info._active_tool_calls
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Waiting for CI",
                tool_call_id="wait-1",
                tool_input='{"seconds": 1800, "reason": "ci"}',
                tool_name="wait",
                mcp_server_name="kirocrew-core",
                mcp_identity_trusted=True,
            )
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Editing PLAN-a.md",
                tool_call_id="write-1",
                tool_input='{"path": "PLAN-a.md"}',
                tool_kind="edit",
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="write-1",
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            )
            seen["judged"] = info._inflight_tool

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.auto_approve_subagent_tools = True
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        manager.hook_store = MagicMock()
        manager.hook_store.fire = AsyncMock()
        manager._log_spawned(info)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            await manager._run_inner(info, "subagent:t05")

        assert seen["stale_at_prompt"] is False
        judged = seen["judged"]
        assert judged is not None, "the write's result emptied the in-flight slot"
        assert judged.is_trusted_wait()


class TestSubagentUsageRow:
    """A completed subagent turn appends one usage row tagged
    surface='subagent', carrying the resolved agent and context occupancy."""

    @pytest.mark.asyncio
    async def test_subagent_turn_persists_usage_row_with_surface(self) -> None:
        from kiro_crew.acp.types import AcpEvent, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo

        async def _complete_stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=0.5))

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 12.0
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _complete_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="usage01",
            task="do the thing",
            agent="researcher",
            parent_session_key="slack:C123:T456",
        )
        manager._log_spawned(info)

        persist = AsyncMock()
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.dashboard.handlers.usage.persist_token_record_async", persist),
            patch(
                "kiro_crew.dashboard.handlers.usage.read_context_tokens",
                MagicMock(return_value=(999, 200000)),
                create=True,
            ),
        ):
            await manager._run_inner(info, "subagent:usage01")

        persist.assert_awaited_once()
        kwargs = persist.await_args.kwargs
        assert kwargs["surface"] == "subagent"
        assert kwargs["agent"] == "researcher"
        assert kwargs["context_used"] == 999
        assert kwargs["context_window"] == 200000
        assert info.credits == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_failed_attempt_preserves_reported_credits(self) -> None:
        """A failed turn has no completion event, but may already be billed."""
        from kiro_crew.acp.types import AcpPromptStats
        from kiro_crew.subagent import SubagentInfo

        provider = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.last_prompt_stats = AcpPromptStats()

        async def _failed_stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            provider.last_prompt_stats = AcpPromptStats(credits=0.75)
            raise RuntimeError("backend failed")
            yield  # noqa: unreachable — makes this an async generator

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _failed_stream())
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        info = SubagentInfo(
            id="usage03",
            task="fail after billing",
            execution_context=execution_for_store(""),
        )
        manager._log_spawned(info)

        with pytest.raises(RuntimeError, match="backend failed"):
            await manager._run_inner(info, "subagent:usage03")

        assert info.credits == pytest.approx(0.75)

    def test_credit_accounting_uses_shared_billing_walk_and_turn_usage(self) -> None:
        from kiro_crew.acp.types import AcpEvent, AcpPromptStats, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo, _RunCreditAccounting

        runner = MagicMock(spec=[])
        runner.last_prompt_stats = AcpPromptStats()
        provider = MagicMock(spec=[])
        provider._handle = runner
        info = SubagentInfo(id="usage-wrapper", task="account wrapped usage")
        accounting = _RunCreditAccounting(info)

        accounting.begin(provider)
        runner.last_prompt_stats = AcpPromptStats(
            credits=0.75,
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=3,
            cache_write_tokens=4,
            cost_usd=0.01,
        )
        accounting.settle()

        accounting.begin(provider)
        accounting.settle(
            AcpEvent(
                kind=EVENT_COMPLETE,
                usage=TurnUsage(credits=0.5, input_tokens=2, cost_usd=0.02),
            )
        )

        assert info.credits == pytest.approx(1.25)
        assert accounting.total.input_tokens == 12
        assert accounting.total.output_tokens == 20
        assert accounting.total.cache_read_tokens == 3
        assert accounting.total.cache_creation_tokens == 4
        assert accounting.total.cost_usd == pytest.approx(0.03)

    @pytest.mark.parametrize("credits", [True, -1.0, float("nan"), float("inf")])
    def test_credit_accounting_rejects_invalid_provider_credits(self, credits: object) -> None:
        from kiro_crew.acp.types import AcpEvent, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo, _RunCreditAccounting

        info = SubagentInfo(id="usage-invalid", task="reject invalid credits", credits=0.75)
        accounting = _RunCreditAccounting(info)
        accounting.begin(MagicMock())
        accounting.settle(AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=credits)))

        assert info.credits == pytest.approx(0.75)
        assert accounting.total.credits == pytest.approx(0.75)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interruption_site", ["stream", "consumer"])
    async def test_cancel_preserves_active_attempt_credits(self, interruption_site) -> None:
        from kiro_crew.acp.types import AcpEvent, AcpPromptStats
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK
        from kiro_crew.subagent import SubagentInfo

        entered = asyncio.Event()
        blocked = asyncio.Event()
        provider = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.last_prompt_stats = AcpPromptStats(credits=9.0)

        async def stream(*args, **kwargs):
            provider.last_prompt_stats = AcpPromptStats(credits=0.75)
            if interruption_site == "stream":
                entered.set()
                # flake-ok: pre-existing gate the test releases by cancelling the run moved verbatim; tracked for a de-flake pass
                await blocked.wait()
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial")

        async def on_event(kind, info, extra):
            if kind == "subagent_chunk":
                entered.set()
                # flake-ok: pre-existing gate the test releases by cancelling the run moved verbatim; tracked for a de-flake pass
                await blocked.wait()

        stream_iter = stream()
        provider.stream = MagicMock(return_value=stream_iter)
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
        info = SubagentInfo(
            id="usagecancel",
            task="cancel after billing",
            execution_context=execution_for_store(""),
        )
        manager._log_spawned(info)

        with patch.object(manager, "_fire_event", side_effect=on_event):
            task = asyncio.create_task(manager._run_inner(info, "subagent:usagecancel"))
            try:
                await await_until(entered)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: pre-existing await of the task this test cancelled moved verbatim; tracked for a de-flake pass
                    await task
                assert info.credits == pytest.approx(0.75)
            finally:
                task.cancel()
                # flake-ok: pre-existing cleanup await of the cancelled run moved verbatim; tracked for a de-flake pass
                await asyncio.gather(task, return_exceptions=True)
                await stream_iter.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ending", "expected"),
        [("complete", 1.25), ("no_complete", 1.5), ("cancel", 1.5), ("stale", 0.75)],
    )
    async def test_retry_credits_settle_each_attempt_once(self, ending, expected) -> None:
        from kiro_crew.acp.types import AcpEvent, AcpPromptStats, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo

        class TransientError(Exception):
            transient = True

        provider = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.last_prompt_stats = AcpPromptStats(credits=9.0)
        attempts = []

        async def stream():
            if len(attempts) == 1:
                provider.last_prompt_stats = AcpPromptStats(credits=0.75)
                raise TransientError("retry this attempt")
            if ending == "stale":
                raise RuntimeError("failed before the next prompt started")
            provider.last_prompt_stats = AcpPromptStats(credits=0.75)
            if ending == "cancel":
                raise asyncio.CancelledError
            if ending == "complete":
                yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=0.5))

        def start_stream(*args, **kwargs):
            attempt = stream()
            attempts.append(attempt)
            return attempt

        provider.stream = start_stream
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
        info = SubagentInfo(
            id="usageretry",
            task="retry after billing",
            execution_context=execution_for_store(""),
        )
        manager._log_spawned(info)
        with patch("kiro_crew.subagent.transient_retry_delay", return_value=0.0):
            try:
                if ending == "cancel":
                    with pytest.raises(asyncio.CancelledError):
                        await manager._run_inner(info, "subagent:usageretry")
                elif ending == "stale":
                    with pytest.raises(RuntimeError, match="failed before"):
                        await manager._run_inner(info, "subagent:usageretry")
                else:
                    await manager._run_inner(info, "subagent:usageretry")
                assert len(attempts) == 2
                assert info.credits == pytest.approx(expected)
            finally:
                for attempt in attempts:
                    await attempt.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recovery", ["infra", "stop"])
    @pytest.mark.parametrize("ending", ["complete", "refused", "cancel"])
    async def test_recovery_settles_withheld_usage_before_readmission(
        self, recovery, ending
    ) -> None:
        from kiro_crew.acp.types import AcpEvent, AcpPromptStats, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.recovery.ladder import InfraError
        from kiro_crew.subagent import SubagentInfo

        provider = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        # This provider reports usage only on completion; cached stats are stale.
        provider.last_prompt_stats = AcpPromptStats(credits=9.0)
        attempts: list[str] = []

        async def stream(*args, **kwargs):
            attempts.append(args[0])
            first = len(attempts) == 1
            provider.last_infra_error = (
                InfraError("capacity") if first and recovery == "infra" else None
            )
            yield AcpEvent(
                kind=EVENT_COMPLETE,
                stop_reason="error: tool stall" if first and recovery == "stop" else "end_turn",
                usage=TurnUsage(credits=0.75 if first else 0.5),
            )

        provider.stream = stream
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder_auto_spawn())
        info = SubagentInfo(
            id="usagerecovery",
            task="recover billed turn",
            execution_context=execution_for_store(""),
        )
        manager._log_spawned(info)
        credits_at_readmission: list[float] = []

        async def readmit(*args):
            credits_at_readmission.append(info.credits)
            if ending == "cancel":
                raise asyncio.CancelledError
            return None if ending == "refused" else "continue the interrupted work"

        method = "_yield_for_infra_retry" if recovery == "infra" else "_yield_for_stop_recovery"
        with patch.object(manager, method, side_effect=readmit):
            if ending == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await manager._run_inner(info, "subagent:usagerecovery")
            else:
                await manager._run_inner(info, "subagent:usagerecovery")

        assert credits_at_readmission == [0.75]
        assert len(attempts) == (2 if ending == "complete" else 1)
        assert info.credits == pytest.approx(1.25 if ending == "complete" else 0.75)

    @pytest.mark.asyncio
    async def test_turn_limit_preserves_credits_before_tombstoning(self) -> None:
        from kiro_crew.acp.types import AcpEvent, AcpPromptStats
        from kiro_crew.hooks import TOOL_DENY, ToolHookResult
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(
            id="usagelimit",
            task="limited run",
            max_turns=1,
            execution_context=execution_for_store(""),
        )
        provider = MagicMock()
        provider.context_usage_pct = lambda: 0.0
        provider.last_prompt_stats = AcpPromptStats()

        async def stream():
            provider.last_prompt_stats = AcpPromptStats(credits=0.75)
            for request_id in range(info.max_turns + 1):
                yield AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="read")

        stream_iter = stream()
        provider.stream = MagicMock(return_value=stream_iter)
        sessions = _mock_sessions()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        ctx = _mock_ctx_builder_auto_spawn()
        ctx.hooks.on_tool_call.return_value = ToolHookResult(action=TOOL_DENY)
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
        manager._log_spawned(info)
        tombstone_credits = []
        provider.reject_tool = AsyncMock()
        with (
            patch.object(
                manager,
                "_write_tombstone",
                side_effect=lambda run, cause: tombstone_credits.append(run.credits),
            ),
        ):
            try:
                await manager._run_inner(info, "subagent:usagelimit")
                assert info.error == "turn_limit:1"
                assert info.credits == pytest.approx(0.75)
                assert tombstone_credits == [0.75]
            finally:
                await stream_iter.aclose()
                await asyncio.get_running_loop().shutdown_asyncgens()

    @pytest.mark.asyncio
    async def test_shared_runtime_agent_does_not_override_spawn_agent(self) -> None:
        """Session sharing reuses the PARENT's runtime, so the runtime's agent
        must not overwrite the agent this spawn actually asked for."""
        from kiro_crew.acp.types import AcpEvent, TurnUsage
        from kiro_crew.providers.base import EVENT_COMPLETE
        from kiro_crew.subagent import SubagentInfo

        async def _complete_stream(*_a: object, **_k: object):  # type: ignore[no-untyped-def]
            yield AcpEvent(kind=EVENT_COMPLETE, usage=TurnUsage(credits=0.5))

        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **kw: _complete_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto_spawn(),
        )
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="usage02",
            task="do the thing",
            agent="researcher",
            parent_session_key="slack:C123:T456",
        )
        manager._log_spawned(info)

        persist = AsyncMock()
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.dashboard.handlers.usage.persist_token_record_async", persist),
            patch(
                "kiro_crew.dashboard.handlers.usage.read_context_tokens",
                MagicMock(return_value=(1, 2)),
                create=True,
            ),
            patch(
                # The shared parent runtime reports the parent's agent.
                "kiro_crew.dashboard.handlers.usage.read_effective_agent",
                MagicMock(return_value="kirocrew"),
                create=True,
            ),
        ):
            await manager._run_inner(info, "subagent:usage02")

        persist.assert_awaited_once()
        assert persist.await_args.kwargs["agent"] == "researcher"


class TestIdentityTrustedChildParentPolicyAuto:
    """A low-fidelity child MCP permission event whose canonical identity IS
    verified (remote server streamed no rawInput) honors an
    unconditional ``parent_policy=auto`` grant instead of stalling on the
    interactive downgrade; without the verified identity the same event stays
    fail-closed.
    """

    def _manager_and_stream(self, event):
        from kiro_crew.hooks import ToolHookResult
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = _mock_sessions()
        sessions.get_approval_policy = MagicMock(return_value="auto")
        provider = sessions.get_or_create.return_value[0]

        async def _stream(*_a, **_kw):
            yield event

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        # ALLOW (not auto-approve): the approval must come from the
        # parent_policy grant, not the hook shortcut.
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult.allow())

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=1)
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="idmcp01",
            task="t",
            parent_session_key="dashboard:default",
        )
        manager._log_spawned(info)
        manager._agents["idmcp01"] = info
        return manager, info, provider

    @staticmethod
    def _child_mcp_event(**overrides):
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        base: dict = dict(
            kind=EVENT_PERMISSION_REQUEST,
            title="@example-server/get-item",
            request_id=7001,
            sub_session_id="child-a",
            shell_classified=True,
            is_shell=False,
            mcp_server_name="example-server",
            tool_name="get-item",
            # Identity provenance flag: the real permission builder sets it
            # when the pair above resolves from the origin-scoped caches.
            mcp_identity_trusted=True,
        )
        base.update(overrides)
        return LLMEvent(**base)

    @pytest.mark.asyncio
    async def test_identity_trusted_child_approved_under_parent_policy_auto(self) -> None:
        event = self._child_mcp_event()
        assert event.child_low_fidelity and event.child_mcp_identity_trusted
        manager, info, provider = self._manager_and_stream(event)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
        ):
            await manager._run_inner(info, "subagent:idmcp01")

        provider.approve_tool.assert_awaited_once_with(7001)
        provider.reject_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identity_missing_child_still_fails_closed(self) -> None:
        """Counterfactual: same grant, same event shape, identity caches
        missed — the request must NOT be auto-approved (headless: rejected)."""
        event = self._child_mcp_event(mcp_server_name="", tool_name="")
        assert event.child_low_fidelity and not event.child_mcp_identity_trusted
        manager, info, provider = self._manager_and_stream(event)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
        ):
            await manager._run_inner(info, "subagent:idmcp01")

        provider.approve_tool.assert_not_awaited()
        provider.reject_tool.assert_awaited_once_with(7001)

    @pytest.mark.asyncio
    async def test_identity_trusted_child_without_auto_policy_not_approved(self) -> None:
        """Identity verification is not itself a grant: with no auto policy
        (and no interactive approver) the event stays fail-closed."""
        event = self._child_mcp_event()
        manager, info, provider = self._manager_and_stream(event)
        manager._sessions.get_approval_policy = MagicMock(return_value="")

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
        ):
            await manager._run_inner(info, "subagent:idmcp01")

        provider.approve_tool.assert_not_awaited()
        provider.reject_tool.assert_awaited_once_with(7001)


class TestIdentityTrustedChildHookIdentityGrant:
    """A low-fidelity child MCP event with a VERIFIED canonical identity honors
    a hook auto-approve that was decided by that identity
    (``ToolHookResult.identity_grant``) — the user's own narrow grant — with no
    ``parent_policy=auto``. A hook grant that read the title, or the same grant
    on a child whose identity is NOT verified, stays fail-closed.
    """

    def _manager_and_stream(self, event, hook_result):
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = _mock_sessions()
        # No unconditional grant: only the hook can approve.
        sessions.get_approval_policy = MagicMock(return_value="")
        provider = sessions.get_or_create.return_value[0]

        async def _stream(*_a, **_kw):
            yield event

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        provider.approve_tool = AsyncMock()
        provider.reject_tool = AsyncMock()

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=hook_result)

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=1)
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="idhook01",
            task="t",
            parent_session_key="dashboard:default",
        )
        manager._log_spawned(info)
        manager._agents["idhook01"] = info
        return manager, info, provider

    @staticmethod
    def _child_mcp_event(**overrides):
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

        base: dict = dict(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: @memory-mcp/search_memory",
            request_id=7101,
            sub_session_id="child-a",
            is_shell=False,
            mcp_server_name="memory-mcp",
            tool_name="search_memory",
            mcp_identity_trusted=True,
        )
        base.update(overrides)
        return LLMEvent(**base)

    def _patches(self):
        return (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
        )

    @pytest.mark.asyncio
    async def test_identity_grant_approves_identity_trusted_child(self) -> None:
        from kiro_crew.hooks import ToolHookResult

        event = self._child_mcp_event()
        assert event.child_low_fidelity and event.child_mcp_identity_trusted
        manager, info, provider = self._manager_and_stream(
            event, ToolHookResult.auto_approve(identity_grant=True)
        )
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await manager._run_inner(info, "subagent:idhook01")

        provider.approve_tool.assert_awaited_once_with(7101)
        provider.reject_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_title_grant_stays_downgraded_for_child(self) -> None:
        """The same auto-approve WITHOUT identity provenance (a title-keyed
        match) is still fail-closed for the low-fidelity child."""
        from kiro_crew.hooks import ToolHookResult

        event = self._child_mcp_event()
        manager, info, provider = self._manager_and_stream(event, ToolHookResult.auto_approve())
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await manager._run_inner(info, "subagent:idhook01")

        provider.approve_tool.assert_not_awaited()
        provider.reject_tool.assert_awaited_once_with(7101)

    @pytest.mark.asyncio
    async def test_identity_grant_needs_verified_identity(self) -> None:
        """An identity grant cannot carry a child whose own identity did not
        verify (cache miss): nothing ties the grant to the tool that runs."""
        from kiro_crew.hooks import ToolHookResult

        event = self._child_mcp_event(mcp_identity_trusted=False)
        assert event.child_low_fidelity and not event.child_mcp_identity_trusted
        manager, info, provider = self._manager_and_stream(
            event, ToolHookResult.auto_approve(identity_grant=True)
        )
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await manager._run_inner(info, "subagent:idhook01")

        provider.approve_tool.assert_not_awaited()
        provider.reject_tool.assert_awaited_once_with(7101)


class TestChildEscalationLimit:
    """Child-origin permission escalations get their own volume bound, and the
    bail must ANSWER the triggering request before tombstoning — a return
    without a response strands the child's oneshot (under session sharing the
    runtime outlives the subagent, so nothing else tears the connection down).
    """

    @pytest.mark.asyncio
    async def test_escalation_limit_bail_answers_triggering_request(self) -> None:
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
        from kiro_crew.subagent import SubagentInfo, SubagentManager

        sessions = _mock_sessions()
        sessions.get_approval_policy = MagicMock(return_value="")
        provider = sessions.get_or_create.return_value[0]

        async def _stream(*_a, **_kw):
            # Limit floor is 60: the 61st child escalation trips the bail.
            for i in range(61):
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    title=f"child tool {i}",
                    request_id=1000 + i,
                    sub_session_id="child-a",
                )

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        provider.reject_tool = AsyncMock()

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=1)
        info = SubagentInfo(
            execution_context=execution_for_store(""),
            id="esc01",
            task="t",
            parent_session_key="dashboard:default",
        )
        manager._log_spawned(info)
        manager._agents["esc01"] = info

        tombstones: list[str] = []
        manager._write_tombstone = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda _i, cause: tombstones.append(cause)
        )

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.update_state"),
            patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
        ):
            await manager._run_inner(info, "subagent:esc01")

        assert tombstones == ["child_escalation_limit"]
        assert info.error.startswith("child_escalation_limit:")
        # EVERY escalation was answered — including the triggering 61st
        # (low-fidelity children with no approver are rejected; the bail must
        # not strand the one that tripped the limit).
        answered = {c.args[0] for c in provider.reject_tool.await_args_list}
        assert 1000 + 60 in answered, "triggering request was not answered"
        assert len(answered) == 61
