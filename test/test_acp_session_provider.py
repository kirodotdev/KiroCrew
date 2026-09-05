"""Unit tests for AcpSessionProvider — the AcpSessionHandle → LLMProvider adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.acp.runtime import AcpRuntimeDead
from kiro_crew.acp.session_handle import WatchdogSettings
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import AcpEvent, AcpPromptStats
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK


def _make_handle(
    session_id: str = "test-session-1",
    is_turn_active: bool = False,
    context_pct: float = 42.0,
    context_used: int = 5000,
    context_window: int = 200000,
) -> MagicMock:
    """Create a mock AcpSessionHandle with configurable defaults."""
    handle = MagicMock()
    handle.session_id = session_id
    handle.is_turn_active = is_turn_active
    handle.last_prompt_stats = AcpPromptStats(
        context_pct=context_pct,
        context_used_tokens=context_used,
        context_window_tokens=context_window,
    )
    handle.destroy = AsyncMock()
    handle.approve_tool = AsyncMock()
    handle.reject_tool = AsyncMock()
    handle.cancel = AsyncMock()
    handle.wait_turn_done = AsyncMock(return_value=True)
    return handle


def _make_runtime(alive: bool = True, acp_backend: str = "") -> MagicMock:
    """Create a mock AcpRuntime.

    ``acp_backend`` mirrors the real constructor's default (kiro), because the
    provider's ``backend`` delegates to it rather than returning a constant.
    """
    runtime = MagicMock()
    runtime.is_alive.return_value = alive
    runtime._process = MagicMock(returncode=None if alive else 1)
    runtime._last_activity = 0.0
    runtime.acp_backend = acp_backend
    return runtime


class TestAcpSessionProviderBasic:
    """Basic property and lifecycle tests."""

    def test_session_id(self):
        handle = _make_handle(session_id="abc-123")
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.session_id == "abc-123"

    def test_context_usage_pct(self):
        handle = _make_handle(context_pct=55.5)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.context_usage_pct() == 55.5

    def test_context_usage_unknown_is_false_while_pct_is_measured(self):
        handle = _make_handle(context_pct=55.5)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.context_usage_unknown() is False

    def test_context_usage_unknown_after_in_place_compaction(self):
        """A compaction zeroes the percentage; the adapter must pass "unknown"
        through so a threshold consumer does not read the session as brand new."""
        handle = _make_handle(context_pct=91.0)
        handle.last_prompt_stats.reset_after_compaction()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.context_usage_pct() == 0.0
        assert provider.context_usage_unknown() is True

    def test_context_window_tokens(self):
        handle = _make_handle(context_window=128000)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.context_window_tokens() == 128000

    def test_context_used_tokens(self):
        handle = _make_handle(context_used=7500)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.context_used_tokens() == 7500

    def test_is_alive_delegates_to_runtime(self):
        handle = _make_handle()
        runtime = _make_runtime(alive=True)
        provider = AcpSessionProvider(handle, runtime)
        assert provider.is_alive() is True

        runtime.is_alive.return_value = False
        assert provider.is_alive() is False

    def test_is_process_alive(self):
        handle = _make_handle()
        runtime = _make_runtime(alive=True)
        provider = AcpSessionProvider(handle, runtime)
        assert provider.is_process_alive() is True

    def test_exit_code_running(self):
        handle = _make_handle()
        runtime = _make_runtime(alive=True)
        provider = AcpSessionProvider(handle, runtime)
        assert provider.exit_code is None

    def test_exit_code_dead(self):
        handle = _make_handle()
        runtime = _make_runtime(alive=False)
        runtime._process.returncode = 137
        provider = AcpSessionProvider(handle, runtime)
        assert provider.exit_code == 137


class TestAcpSessionProviderLifecycle:
    """Start/shutdown lifecycle tests."""

    @pytest.mark.asyncio
    async def test_start_is_noop(self):
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        # Should not raise
        await provider.start()

    @pytest.mark.asyncio
    async def test_shutdown_destroys_handle(self):
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        await provider.shutdown()
        handle.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_handles_destroy_error(self):
        handle = _make_handle()
        handle.destroy = AsyncMock(side_effect=Exception("pipe broken"))
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        # Should not raise
        await provider.shutdown()
        handle.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_idle_subagent_does_not_cancel(self):
        # No in-flight turn → nothing to cancel; just destroy the handle.
        handle = _make_handle(is_turn_active=False)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=False)

        await provider.shutdown()
        handle.cancel.assert_not_awaited()
        handle.destroy.assert_awaited_once()
        runtime.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_active_subagent_cancels_then_destroys(self):
        # Reaping a session-sharing subagent mid-turn must CANCEL the turn (so
        # the abandoned prompt stops burning credits / wedging the shared
        # runtime) but must NOT kill the runtime (co-tenants keep running).
        handle = _make_handle(is_turn_active=True)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=False)

        await provider.shutdown()
        handle.cancel.assert_awaited_once()
        handle.destroy.assert_awaited_once()
        runtime.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_active_subagent_destroys_even_if_cancel_fails(self):
        # A failed/hung cancel must not block handle teardown.
        handle = _make_handle(is_turn_active=True)
        handle.cancel = AsyncMock(side_effect=Exception("runtime unresponsive"))
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=False)

        await provider.shutdown()
        handle.destroy.assert_awaited_once()
        runtime.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_owns_runtime_kills_runtime(self):
        # Parent session owns the runtime → kill the whole process, no per-session
        # cancel/destroy dance.
        handle = _make_handle(is_turn_active=True)
        runtime = _make_runtime()
        runtime.kill = AsyncMock()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=True)

        await provider.shutdown()
        runtime.kill.assert_awaited_once()
        handle.destroy.assert_not_awaited()
        handle.cancel.assert_not_awaited()


class TestAcpSessionProviderStream:
    """Streaming (prompt) tests."""

    @pytest.mark.asyncio
    async def test_stream_yields_events(self):
        handle = _make_handle()
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Hello "),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="world"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]

        async def mock_prompt(msg):
            for e in events:
                yield e

        handle.prompt = mock_prompt
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        collected = []
        async for event in provider.stream("test message"):
            collected.append(event)

        assert len(collected) == 3
        assert collected[0].kind == EVENT_TEXT_CHUNK
        assert collected[0].text == "Hello "
        assert collected[2].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_stream_command_routes_through_handle_stream_command(self):
        """Slash commands go through the handle's NATIVE commands/execute path,
        never through prompt() — a prompt round-trip would hand the command to
        the model, which summarizes kiro-cli's output instead of returning it
        (issue #4972)."""
        handle = _make_handle()
        events = [
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="13 tools"),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=""),
        ]
        seen_commands: list[str] = []

        async def mock_stream_command(command):
            seen_commands.append(command)
            for e in events:
                yield e

        async def mock_prompt(msg):  # pragma: no cover — must never run
            raise AssertionError("stream_command must not route through prompt()")
            yield  # make it a generator

        handle.stream_command = mock_stream_command
        handle.prompt = mock_prompt
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        collected = [e async for e in provider.stream_command("/tools")]

        assert seen_commands == ["/tools"]
        assert [e.kind for e in collected] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert collected[0].text == "13 tools"

    @pytest.mark.asyncio
    async def test_stream_command_translates_runtime_dead(self):
        """Runtime death mid-command stays inside the AcpError hierarchy
        (AcpProcessDied), mirroring stream()'s translation."""

        async def dead_stream_command(command):
            raise AcpRuntimeDead("runtime died")
            yield  # make it a generator

        handle = _make_handle()
        handle.stream_command = dead_stream_command
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: False
        provider = AcpSessionProvider(handle, runtime)

        with pytest.raises(AcpProcessDied):
            async for _ in provider.stream_command("/tools"):
                pass


class TestAcpSessionProviderToolApproval:
    """Tool approval/rejection tests."""

    @pytest.mark.asyncio
    async def test_approve_tool_once(self):
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        await provider.approve_tool("req-42")
        handle.approve_tool.assert_awaited_once_with("req-42", option_id="allow_once")

    @pytest.mark.asyncio
    async def test_approve_tool_always(self):
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        await provider.approve_tool("req-99", always=True)
        handle.approve_tool.assert_awaited_once_with("req-99", option_id="allow_always")

    @pytest.mark.asyncio
    async def test_reject_tool(self):
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        await provider.reject_tool("req-7")
        handle.reject_tool.assert_awaited_once_with("req-7")


class TestAcpSessionProviderCancel:
    """Cancel operation tests."""

    @pytest.mark.asyncio
    async def test_cancel_no_turn(self):
        handle = _make_handle(is_turn_active=False)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel()
        assert result == "no_turn"
        handle.cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_active_turn(self):
        handle = _make_handle(is_turn_active=True)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel()
        assert result == "acked"
        handle.cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_with_timeout_acked(self):
        handle = _make_handle(is_turn_active=True)
        handle.wait_turn_done = AsyncMock(return_value=True)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel(wait_ack_timeout=5.0)
        assert result == "acked"
        handle.wait_turn_done.assert_awaited_once_with(timeout=5.0)

    @pytest.mark.asyncio
    async def test_cancel_with_timeout_expired(self):
        handle = _make_handle(is_turn_active=True)
        handle.wait_turn_done = AsyncMock(return_value=False)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel(wait_ack_timeout=1.0)
        assert result == "timeout"

    @pytest.mark.asyncio
    async def test_cancel_runtime_dead(self):
        handle = _make_handle(is_turn_active=True)
        handle.cancel = AsyncMock(side_effect=AcpRuntimeDead("dead"))
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel()
        assert result == "error"

    @pytest.mark.asyncio
    async def test_cancel_unexpected_error(self):
        handle = _make_handle(is_turn_active=True)
        handle.cancel = AsyncMock(side_effect=RuntimeError("oops"))
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        result = await provider.cancel()
        assert result == "error"


class TestAcpSessionProviderErrorPropagation:
    """Tests for error propagation through the adapter."""

    @pytest.mark.asyncio
    async def test_stream_propagates_acp_process_died(self):
        """When runtime dies mid-prompt, AcpProcessDied propagates to caller."""
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()

        async def dying_prompt(msg):
            yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="partial ")
            raise AcpProcessDied("Runtime process died during prompt")

        handle.prompt = dying_prompt
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)

        collected = []
        with pytest.raises(AcpProcessDied, match="Runtime process died"):
            async for event in provider.stream("test"):
                collected.append(event)

        # Should have received the partial chunk before dying
        assert len(collected) == 1
        assert collected[0].text == "partial "

    @pytest.mark.asyncio
    async def test_stream_propagates_runtime_dead(self):
        """When the handle raises AcpRuntimeDead, stream() TRANSLATES it to
        AcpProcessDied (parity with AcpClient) so chat_runner's handlers catch
        it -- AcpRuntimeDead (an AcpRuntimeError, NOT an AcpError) would
        otherwise escape uncaught. Auth-expiry -> AcpAuthRequired is covered by
        TestAcpSessionProviderRound4Parity."""
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()

        async def dead_prompt(msg):
            raise AcpRuntimeDead("runtime is dead")
            yield  # noqa: unreachable — makes this an async generator

        handle.prompt = dead_prompt
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: False  # not an auth failure
        provider = AcpSessionProvider(handle, runtime)

        with pytest.raises(AcpProcessDied):
            async for _ in provider.stream("test"):
                pass

    def test_touch_activity_updates_runtime(self):
        """touch_activity refreshes the runtime's _last_activity timestamp."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime._last_activity = 0.0
        provider = AcpSessionProvider(handle, runtime)

        provider.touch_activity()
        assert runtime._last_activity > 0.0


class TestAcpSessionProviderClientCompat:
    """Tests for the AcpClient-compatible API surface."""

    def test_backend_is_empty_for_kiro(self):
        """backend reports empty string for a kiro runtime (not claude)."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.backend == ""

    def test_backend_reports_the_runtimes_backend(self):
        """Delegated, not constant.

        This provider replaces the placeholder AcpClient on
        ``AcpProvider._client`` once startup finishes, so it is the only place
        a started provider's backend can still be read. Returning kiro
        unconditionally would persist every KAS session under the kiro label.
        """
        handle = _make_handle()
        runtime = _make_runtime(acp_backend="kas")
        provider = AcpSessionProvider(handle, runtime)
        assert provider.backend == "kas"

    def test_cwd_reports_the_sessions_bound_dir_not_the_shared_runtimes(self):
        """A shared runtime carries sessions opened against different projects.

        The task runtime is started once in workspace A; a task session then opens against
        B. Answering with the runtime's directory reports a workspace this session never
        bound, so reuse validation reads it as moved and evicts a live session -- losing
        its conversation for failing to be somewhere it never was.
        """
        handle = _make_handle()
        handle._bound_cwd = "/workspaces/b"
        runtime = _make_runtime()
        runtime._work_dir = Path("/workspaces/a")
        provider = AcpSessionProvider(handle, runtime)
        assert provider.cwd == "/workspaces/b", (
            "the session bound to B must report B; reporting the runtime's A evicts it "
            f"on every project-scoped claim; got {provider.cwd!r}"
        )

    def test_cwd_falls_back_to_the_runtime_when_no_bound_dir_was_recorded(self):
        """A handle predating the record is the single-session case, where they agree."""
        handle = _make_handle()
        handle._bound_cwd = ""
        runtime = _make_runtime()
        runtime._work_dir = Path("/workspaces/a")
        provider = AcpSessionProvider(handle, runtime)
        assert provider.cwd == str(Path("/workspaces/a"))

    def test_has_active_turn(self):
        """has_active_turn is a METHOD (parity with AcpClient) delegating to
        handle.is_turn_active. Callers invoke it with () -- a @property here
        caused 'TypeError: bool object is not callable' on the kiro path."""
        handle = _make_handle(is_turn_active=True)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert callable(provider.has_active_turn)
        assert provider.has_active_turn() is True

        handle.is_turn_active = False
        assert provider.has_active_turn() is False

    @pytest.mark.asyncio
    async def test_ensure_ready_alive(self):
        """ensure_ready succeeds when runtime is alive."""
        handle = _make_handle()
        runtime = _make_runtime(alive=True)
        provider = AcpSessionProvider(handle, runtime)
        await provider.ensure_ready()  # Should not raise

    @pytest.mark.asyncio
    async def test_ensure_ready_dead_raises(self):
        """ensure_ready raises within the AcpError hierarchy (AcpProcessDied) when
        the runtime is dead -- R6: NOT the raw AcpRuntimeError, so callers that
        catch AcpError (chat_runner) see it instead of hitting `except Exception`."""
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()
        runtime = _make_runtime(alive=False)
        runtime.saw_not_logged_in = lambda: False
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpProcessDied):
            await provider.ensure_ready()

    def test_is_responsive(self):
        """is_responsive delegates to handle.is_responsive."""
        handle = _make_handle()
        handle.is_responsive = lambda t=600.0: True
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.is_responsive() is True

    @pytest.mark.asyncio
    async def test_send_command(self):
        """send_command delegates to handle.send_command."""
        handle = _make_handle()
        handle.send_command = AsyncMock(return_value="done")
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        result = await provider.send_command("/compact")
        handle.send_command.assert_awaited_once_with("/compact", None)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_send_command_with_args(self):
        """send_command passes args to handle."""
        handle = _make_handle()
        handle.send_command = AsyncMock(return_value="ok")
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        result = await provider.send_command("/effort", {"level": "high"})
        handle.send_command.assert_awaited_once_with("/effort", {"level": "high"})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_set_config_option(self):
        """set_config_option delegates to handle."""
        handle = _make_handle()
        handle.set_config_option = AsyncMock()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        await provider.set_config_option("effort", "high")
        handle.set_config_option.assert_awaited_once_with("effort", "high")

    @pytest.mark.asyncio
    async def test_wait_for_compaction(self):
        """wait_for_compaction delegates to handle."""
        handle = _make_handle()
        handle.wait_for_compaction = AsyncMock(
            return_value={"type": "completed", "summary": "reduced to 50%"}
        )
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        result = await provider.wait_for_compaction()
        assert result == {"type": "completed", "summary": "reduced to 50%"}

    def test_model_property(self):
        """_model property reads from handle.model."""
        handle = _make_handle()
        handle.model = "opus-4"
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider._model == "opus-4"

    def test_model_setter(self):
        """_model setter writes to handle._model."""
        handle = _make_handle()
        handle._model = ""
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        provider._model = "sonnet-4"
        assert handle._model == "sonnet-4"

    def test_work_dir(self):
        """_work_dir reads from runtime._work_dir."""
        handle = _make_handle()
        runtime = _make_runtime()
        from pathlib import Path

        runtime._work_dir = Path("/home/user/workspace")
        provider = AcpSessionProvider(handle, runtime)
        assert provider._work_dir == Path("/home/user/workspace")

    def test_permission_mode_always_empty(self):
        """_permission_mode is always empty string for kiro."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider._permission_mode == ""
        # Setter is a no-op
        provider._permission_mode = "auto"
        assert provider._permission_mode == ""

    def test_supports_permission_mode_always_false(self):
        """supports_permission_mode always returns False for kiro."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.supports_permission_mode("auto") is False

    def test_acp_config_options(self):
        """acp_config_options returns handle.config_options."""
        handle = _make_handle()
        handle.config_options = [{"id": "effort", "options": []}]
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.acp_config_options == [{"id": "effort", "options": []}]

    def test_available_models(self):
        """available_models returns handle.available_models."""
        handle = _make_handle()
        handle.available_models = [{"id": "opus-4", "name": "Claude Opus 4"}]
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.available_models() == [{"id": "opus-4", "name": "Claude Opus 4"}]

    def test_get_valid_effort_levels(self):
        """get_valid_effort_levels delegates to handle."""
        handle = _make_handle()
        handle.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.get_valid_effort_levels() == ["low", "high"]

    def test_supports_config_option(self):
        """supports_config_option delegates to handle."""
        handle = _make_handle()
        handle.supports_config_option = MagicMock(return_value=True)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert provider.supports_config_option("effort") is True
        handle.supports_config_option.assert_called_once_with("effort")

    def test_pid_property(self):
        """_pid returns runtime.pid."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime.pid = 54321
        provider = AcpSessionProvider(handle, runtime)
        assert provider._pid == 54321


class TestAcpSessionProviderOwnsRuntime:
    """Tests for owns_runtime=True behavior (parent session path)."""

    @pytest.mark.asyncio
    async def test_shutdown_kills_runtime_when_owns(self):
        """When owns_runtime=True, shutdown kills the entire runtime."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime.kill = AsyncMock()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=True)

        await provider.shutdown()
        runtime.kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutdown_only_destroys_handle_when_not_owns(self):
        """When owns_runtime=False (default), shutdown only destroys the handle."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime.kill = AsyncMock()
        provider = AcpSessionProvider(handle, runtime, owns_runtime=False)

        await provider.shutdown()
        handle.destroy.assert_awaited_once()
        runtime.kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shutdown_handles_kill_failure(self):
        """shutdown doesn't raise when runtime.kill() fails."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime.kill = AsyncMock(side_effect=OSError("already dead"))
        provider = AcpSessionProvider(handle, runtime, owns_runtime=True)

        # Should not raise
        await provider.shutdown()


class TestAcpSessionProviderRound4Parity:
    """Round-4 AcpClient call-surface parity fixes.

    Every member below is invoked on ``AcpProvider._client`` / ``provider.client``
    which, on the kiro shared-runtime path, IS an ``AcpSessionProvider``. A
    missing member or mismatched call-convention/return-type surfaces as a
    runtime TypeError/AttributeError only on that path (the recurring bug class:
    cancel_session, has_active_turn, wait_turn_done).
    """

    def test_rekey_stores_keys_and_refreshes_activity(self):
        """#2 -- session.py warm-pool claim calls provider.client.rekey(...);
        mirror AcpClient.rekey: store correlation keys + touch runtime activity."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime._last_activity = 0.0
        provider = AcpSessionProvider(handle, runtime)
        provider.rekey("dashboard:slot9", "chan-7")
        assert provider._session_key == "dashboard:slot9"
        assert provider._channel_id == "chan-7"
        assert runtime._last_activity > 0.0

    def test_rekey_rebinds_watchdog_to_claiming_crew(self):
        """The claiming session's canonical crew identity travels with the
        claim: rekey rebinds the live handle's watchdog snapshot AND updates
        the runtime default so later sessions (new_conversation) inherit the
        claimed crew, not the pool's spawn state."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        wd = WatchdogSettings(tool_stall_suspect_secs=123.0)
        provider.rekey("dashboard:slot9", "chan-7", crew_agent="pr-reviewer", watchdog=wd)
        handle.rebind_watchdog.assert_called_once_with("pr-reviewer", settings=wd)
        assert runtime._crew_agent == "pr-reviewer"
        # An identity-less claim still rebinds (to the globals): a recycled
        # runtime must not carry a previous crew's windows. Without a
        # pre-resolved snapshot the rebind loads synchronously (settings=None).
        provider.rekey("dashboard:slot3", None)
        handle.rebind_watchdog.assert_called_with("", settings=None)
        assert runtime._crew_agent == ""

    def test_rekey_resets_context_state(self):
        """#2932 -- the handoff must drop the previous session's context state
        (mirror of AcpClient.rekey): _make_handle seeds pct=42/5000/200000, so
        a leak here would hand those numbers to the claiming session and let
        check_context_usage compact its empty conversation."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert handle.last_prompt_stats.context_pct == 42.0  # seeded stale state
        provider.rekey("dashboard:slot9", "chan-7")
        stats = handle.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0
        assert stats.context_tokens_from_usage is False
        assert stats.context_pct_unknown is False

    def test_agent_reads_from_runtime(self):
        """#5 -- session.py session-info introspection reads
        provider.client._agent; mirror AcpClient._agent via the runtime."""
        handle = _make_handle()
        runtime = _make_runtime()
        runtime._agent = "kirocrew-lite"
        provider = AcpSessionProvider(handle, runtime)
        assert provider._agent == "kirocrew-lite"

    @pytest.mark.asyncio
    async def test_stream_events_translates_runtime_dead(self):
        """#3 -- stream_events delegates to stream() so AcpRuntimeDead is
        translated to AcpProcessDied (chat_runner-catchable), not left to escape."""
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()

        async def _boom(_msg):
            raise AcpRuntimeDead("dead")
            yield  # pragma: no cover -- unreachable, makes this an async gen

        handle.prompt = _boom
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: False
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpProcessDied):
            async for _ in provider.stream_events("hi"):
                pass

    @pytest.mark.asyncio
    async def test_stream_events_translates_auth_required(self):
        """#3 -- stream_events -> AcpAuthRequired when runtime saw 'not logged in'."""
        from kiro_crew.acp.client import AcpAuthRequired

        handle = _make_handle()

        async def _boom(_msg):
            raise AcpRuntimeDead("dead")
            yield  # pragma: no cover

        handle.prompt = _boom
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: True
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpAuthRequired):
            async for _ in provider.stream_events("hi"):
                pass

    @pytest.mark.asyncio
    async def test_wait_turn_done_returns_stop_reason_str(self):
        """Bug B -- provider.wait_turn_done returns the stop_reason STR so
        AcpProvider.cancel's `reason in (...)` check works (not a bool)."""
        handle = _make_handle()
        handle.wait_turn_done = AsyncMock(return_value=True)
        handle._last_stop_reason = "end_turn"
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        reason = await provider.wait_turn_done(timeout=1.0)
        assert isinstance(reason, str)
        assert reason == "end_turn"

    @pytest.mark.asyncio
    async def test_wait_turn_done_raises_timeout(self):
        """Bug B -- provider.wait_turn_done raises asyncio.TimeoutError when the
        turn does not finish (parity with AcpClient), rather than returning False."""
        import asyncio

        handle = _make_handle()
        handle.wait_turn_done = AsyncMock(return_value=False)
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(asyncio.TimeoutError):
            await provider.wait_turn_done(timeout=0.05)

    @pytest.mark.asyncio
    async def test_wait_turn_done_defaults_to_end_turn_when_empty(self):
        """A done turn with an EMPTY _last_stop_reason (synthetic-terminal paths:
        tool-interrupted / unresponsive-cancel / stale) must NOT return "" — that
        makes AcpProvider.cancel misread it as a timeout and HARD-KILL the shared
        runtime (killing co-tenants). It must fall back to a benign END_TURN."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN

        handle = _make_handle()
        handle.wait_turn_done = AsyncMock(return_value=True)
        handle._last_stop_reason = ""  # synthetic terminal, no stopReason set
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        reason = await provider.wait_turn_done(timeout=1.0)
        assert reason == STOP_REASON_END_TURN
        assert reason  # never empty

    @pytest.mark.asyncio
    async def test_cancel_session_accepts_grace_secs(self):
        """Round-3 -- cancel_session must accept grace_secs (AcpProvider.cancel
        calls it with grace_secs=...) and forward to handle.cancel."""
        handle = _make_handle()
        handle.cancel = AsyncMock()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        await provider.cancel_session(grace_secs=3.0)
        handle.cancel.assert_awaited_once_with(grace_secs=3.0)

    @pytest.mark.asyncio
    async def test_cancel_session_swallows_runtime_dead(self):
        """R5 -- cancel_session MUST NOT raise (parity with AcpClient's swallow-all):
        if handle.cancel() raises AcpRuntimeDead (runtime died mid-turn), it is
        swallowed so it can't escape AcpProvider.cancel()'s `except AcpError`
        handler and 500 the stop handler."""
        handle = _make_handle()
        handle.cancel = AsyncMock(side_effect=AcpRuntimeDead("runtime is dead"))
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        assert await provider.cancel_session(grace_secs=1.0) is None  # no raise
        handle.cancel.assert_awaited_once_with(grace_secs=1.0)


class TestAcpSessionProviderRuntimeDeadTranslation:
    """R6 fault-injection: the ENTIRE AcpSessionProvider surface must translate
    AcpRuntimeDead (an AcpRuntimeError, OUTSIDE the AcpError hierarchy) into
    AcpProcessDied / AcpAuthRequired, so a runtime.send_* failure never escapes
    to a caller that only catches AcpError (chat_runner) and lands on its
    generic `except Exception` (raw error card, no retry/reset)."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: p.approve_tool("req-1"),
            lambda p: p.reject_tool("req-1"),
            lambda p: p.send_command("/compact"),
            lambda p: p.set_config_option("effort", "high"),
            lambda p: p.compact(),
            lambda p: p.set_model("m"),
            lambda p: p.set_mode("kirocrew"),
        ],
        ids=[
            "approve_tool",
            "reject_tool",
            "send_command",
            "set_config_option",
            "compact",
            "set_model",
            "set_mode",
        ],
    )
    @pytest.mark.asyncio
    async def test_runtime_dead_translates_to_process_died(self, call):
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()
        for m in (
            "approve_tool",
            "reject_tool",
            "send_command",
            "set_config_option",
            "compact",
            "set_model",
            "set_mode",
        ):
            setattr(handle, m, AsyncMock(side_effect=AcpRuntimeDead("dead")))
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: False
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpProcessDied):
            await call(provider)

    @pytest.mark.asyncio
    async def test_runtime_dead_when_not_logged_in_is_auth_required(self):
        """AcpRuntimeDead + saw_not_logged_in -> AcpAuthRequired (login prompt),
        mirroring stream()'s auth-aware translation."""
        from kiro_crew.acp.client import AcpAuthRequired

        handle = _make_handle()
        handle.approve_tool = AsyncMock(side_effect=AcpRuntimeDead("dead"))
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: True
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpAuthRequired):
            await provider.approve_tool("req-1")

    @pytest.mark.asyncio
    async def test_ensure_ready_dead_not_logged_in_is_auth_required(self):
        from kiro_crew.acp.client import AcpAuthRequired

        handle = _make_handle()
        runtime = _make_runtime(alive=False)
        runtime.saw_not_logged_in = lambda: True
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpAuthRequired):
            await provider.ensure_ready()


class TestAcpSessionProviderContractParity:
    """Contract-parity deep-dive fixes: base-AcpRuntimeError translation, steer
    guarding, approve_tool option_id."""

    @pytest.mark.asyncio
    async def test_stream_base_runtime_error_translates_to_acp_error(self):
        """The base AcpRuntimeError ('turn already active' guard) is OUTSIDE the
        AcpError hierarchy; stream() must translate it to AcpError so callers
        catch it instead of hitting `except Exception`."""
        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.session_handle import AcpRuntimeError

        handle = _make_handle()

        async def boom(msg):
            raise AcpRuntimeError("A turn is already active")
            yield  # pragma: no cover

        handle.prompt = boom
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpError):
            async for _ in provider.stream("x"):
                pass

    @pytest.mark.asyncio
    async def test_steer_translates_runtime_dead(self):
        """steer() must translate AcpRuntimeDead (completes the exception-contract
        invariant across the whole provider surface)."""
        from kiro_crew.acp.client import AcpProcessDied

        handle = _make_handle()
        handle.steer = AsyncMock(side_effect=AcpRuntimeDead("dead"))
        runtime = _make_runtime()
        runtime.saw_not_logged_in = lambda: False
        provider = AcpSessionProvider(handle, runtime)
        with pytest.raises(AcpProcessDied):
            await provider.steer("go")

    @pytest.mark.asyncio
    async def test_approve_tool_explicit_option_id(self):
        """approve_tool honors an explicit option_id (signature parity)."""
        handle = _make_handle()
        runtime = _make_runtime()
        provider = AcpSessionProvider(handle, runtime)
        await provider.approve_tool("req", option_id="allow_always")
        handle.approve_tool.assert_awaited_once_with("req", option_id="allow_always")


class TestNewConversation:
    """AcpSessionProvider.new_conversation — the cheap warm-reset primitive the
    workflow session pool relies on. Correctness matters more than speed here: a
    buggy reset would leak one workflow step's context into the next. Asserts it
    (1) creates a FRESH session on the SAME runtime and swaps the handle,
    (2) DESTROYS the old session (frees context — the isolation guarantee),
    (3) raises AcpProcessDied on a dead runtime so the pool self-heals, and
    (4) creates-before-destroys so a failed create leaves the old handle usable."""

    def _runtime_with_new_session(self, alive: bool = True):
        runtime = _make_runtime(alive=alive)
        runtime._work_dir = "/tmp/ws"
        runtime._agent = "kirocrew"
        new_handle = _make_handle(session_id="fresh-session-2")
        runtime.create_session = AsyncMock(return_value=new_handle)
        return runtime, new_handle

    @pytest.mark.asyncio
    async def test_creates_fresh_session_on_same_runtime_and_swaps_handle(self):
        old = _make_handle(session_id="old-session-1")
        runtime, new_handle = self._runtime_with_new_session()
        provider = AcpSessionProvider(old, runtime)

        await provider.new_conversation()

        # Fresh session/new on the SAME runtime (cwd+agent from the runtime).
        runtime.create_session.assert_awaited_once_with(cwd="/tmp/ws", agent="kirocrew")
        # Handle swapped to the fresh session → next prompt starts clean.
        assert provider._handle is new_handle
        assert provider.session_id == "fresh-session-2"

    @pytest.mark.asyncio
    async def test_destroys_old_session_to_free_context(self):
        old = _make_handle(session_id="old-session-1")
        runtime, _ = self._runtime_with_new_session()
        provider = AcpSessionProvider(old, runtime)

        await provider.new_conversation()

        # The isolation guarantee: the prior session is torn down on the shared
        # process (frees its transcript/context + MCP children — no cross-task bleed).
        old.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_runtime_dead(self):
        old = _make_handle(session_id="old-session-1")
        runtime, _ = self._runtime_with_new_session(alive=False)
        provider = AcpSessionProvider(old, runtime)

        with pytest.raises(AcpProcessDied):
            await provider.new_conversation()
        # No attempt to create a session on a dead runtime; old handle untouched.
        runtime.create_session.assert_not_awaited()
        assert provider._handle is old

    @pytest.mark.asyncio
    async def test_create_before_destroy_keeps_old_handle_on_failure(self):
        old = _make_handle(session_id="old-session-1")
        runtime, _ = self._runtime_with_new_session()
        runtime.create_session = AsyncMock(side_effect=RuntimeError("session/new boom"))
        provider = AcpSessionProvider(old, runtime)

        with pytest.raises(RuntimeError):
            await provider.new_conversation()
        # Create failed → provider still points at the ORIGINAL live session
        # (never a window referencing a terminated one), and old was NOT destroyed.
        assert provider._handle is old
        old.destroy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_survives_old_destroy_failure(self):
        """A failure destroying the OLD session must not fail the reset — the new
        session is already live and swapped in (best-effort cleanup)."""
        old = _make_handle(session_id="old-session-1")
        old.destroy = AsyncMock(side_effect=RuntimeError("terminate boom"))
        runtime, new_handle = self._runtime_with_new_session()
        provider = AcpSessionProvider(old, runtime)

        await provider.new_conversation()  # must NOT raise
        assert provider._handle is new_handle

    @pytest.mark.asyncio
    async def test_reapplies_configured_model_to_fresh_session(self):
        """A fresh session/new reverts to the agent-default model, so a warm
        worker configured with a NON-default model must have it re-applied on
        every reset — else every reused task silently runs on the wrong model."""
        old = _make_handle(session_id="old-session-1")
        old.model = "claude-opus-4-8"  # the configured non-default model
        runtime, new_handle = self._runtime_with_new_session()
        new_handle.set_model = AsyncMock()
        provider = AcpSessionProvider(old, runtime)

        await provider.new_conversation()

        # The fresh session is re-pinned to the configured model.
        new_handle.set_model.assert_awaited_once_with("claude-opus-4-8")

    @pytest.mark.asyncio
    async def test_default_model_sentinel_not_reapplied(self):
        """The "auto" sentinel means "let kiro pick per agent config" — no
        set_model call, matching the cold-start handshake."""
        old = _make_handle(session_id="old-session-1")
        old.model = "auto"
        runtime, new_handle = self._runtime_with_new_session()
        new_handle.set_model = AsyncMock()
        provider = AcpSessionProvider(old, runtime)

        await provider.new_conversation()

        new_handle.set_model.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_model_reapply_failure_tears_down_and_raises(self):
        """A set_model failure on the fresh session must NOT commit a wrong-model
        handle: the fresh session is torn down and the call raises so the caller
        (WorkerPool) hard-resets. Committing it would silently run every later
        pooled step on the default model."""
        from kiro_crew.acp.client import AcpError

        old = _make_handle(session_id="old-session-1")
        old.model = "claude-opus-4-8"
        runtime, new_handle = self._runtime_with_new_session()
        new_handle.set_model = AsyncMock(side_effect=RuntimeError("set_model boom"))
        new_handle.destroy = AsyncMock()
        provider = AcpSessionProvider(old, runtime)

        with pytest.raises(AcpError):
            await provider.new_conversation()
        # Fresh (wrong-model) session was torn down; the old handle is NOT
        # destroyed and stays the provider's handle for the hard-reset fallback.
        new_handle.destroy.assert_awaited_once()
        assert provider._handle is old


class TestLivePathModelEntitlement:
    """The guard has to sit on the path real sessions actually take.

    Dashboard sessions do NOT go through AcpClient's handshake — providers.acp
    spawns an AcpRuntime and applies the configured model with
    ``handle.set_model``. A guard in AcpClient alone would be inert here, which
    is exactly how the unusable model kept reaching the wire.
    """

    @staticmethod
    def _provider(advertised):
        from kiro_crew.acp.session_provider import AcpSessionProvider

        handle = _make_handle()
        handle.available_models = [{"modelId": m, "name": m} for m in advertised]
        handle.set_model = AsyncMock()
        # Default: the revalidation probe cannot confirm anything (returns []),
        # so a stale-snapshot refusal stands. Tests exercising the heal path
        # override this with a side_effect that also updates available_models.
        handle.refresh_available_models = AsyncMock(return_value=[])
        return AcpSessionProvider(handle, MagicMock()), handle

    @pytest.mark.asyncio
    async def test_explicit_switch_refused_on_live_path(self):
        from kiro_crew.acp.client import AcpModelUnavailable

        provider, handle = self._provider(["claude-sonnet-4.6", "claude-haiku-4.5"])

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await provider.set_model("claude-opus-4.8")

        handle.set_model.assert_not_awaited()
        msg = str(excinfo.value)
        assert "not available on your account" in msg
        assert "claude-sonnet-4.6" in msg
        # Terminal, so the retry ladder does not reproduce the same rejection.
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_advertised_switch_still_applied_on_live_path(self):
        provider, handle = self._provider(["claude-sonnet-4.6", "claude-opus-4.8"])

        await provider.set_model("claude-opus-4.8")

        handle.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_unknown_advertised_set_still_applied(self):
        """A backend that advertises nothing must not have every switch refused."""
        provider, handle = self._provider([])

        await provider.set_model("claude-opus-4.8")

        handle.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_malformed_advertised_payload_does_not_raise(self):
        """Remote-shaped input degrades to "unknown", never an exception."""
        provider, handle = self._provider([])
        handle.available_models = "not-a-list"

        await provider.set_model("claude-opus-4.8")

        handle.set_model.assert_awaited_once_with("claude-opus-4.8")

    @pytest.mark.asyncio
    async def test_stale_refusal_revalidates_and_allows(self):
        """A would-be refusal on the session-init snapshot is revalidated
        against a fresh probe; when the fresh answer advertises the model, the
        switch proceeds instead of freezing a false 'not entitled' verdict."""
        provider, handle = self._provider(["claude-sonnet-4", "claude-sonnet-4.5"])
        fresh = [
            {"modelId": "auto", "name": "auto", "description": ""},
            {"modelId": "claude-opus-5", "name": "claude-opus-5", "description": ""},
        ]

        async def _refresh():
            handle.available_models = fresh
            return fresh

        handle.refresh_available_models = AsyncMock(side_effect=_refresh)

        await provider.set_model("claude-opus-5")

        handle.refresh_available_models.assert_awaited_once()
        handle.set_model.assert_awaited_once_with("claude-opus-5")

    @pytest.mark.asyncio
    async def test_refusal_stands_when_fresh_probe_still_lacks_model(self):
        """A real downgrade survives revalidation — and the error names the
        FRESH advertised set, not the stale one."""
        from kiro_crew.acp.client import AcpModelUnavailable

        provider, handle = self._provider(["claude-sonnet-4.6"])
        handle.refresh_available_models = AsyncMock(
            return_value=[
                {"modelId": "claude-sonnet-4", "name": "s4", "description": ""},
                {"modelId": "claude-sonnet-4.5", "name": "s45", "description": ""},
            ]
        )

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await provider.set_model("claude-opus-4.8")

        handle.set_model.assert_not_awaited()
        assert "claude-sonnet-4.5" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_refusal_names_stale_set_when_probe_fails(self):
        """An empty probe result is not evidence: the stale verdict stands and
        the error names the only set we actually have."""
        from kiro_crew.acp.client import AcpModelUnavailable

        provider, handle = self._provider(["claude-sonnet-4.6"])

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await provider.set_model("claude-opus-4.8")

        handle.refresh_available_models.assert_awaited_once()
        assert "claude-sonnet-4.6" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_advertised_pick_never_probes(self):
        """The probe runs only on a would-be refusal — an allowed switch costs
        no extra round-trip."""
        provider, handle = self._provider(["claude-opus-4.8"])

        await provider.set_model("claude-opus-4.8")

        handle.refresh_available_models.assert_not_awaited()
        handle.set_model.assert_awaited_once_with("claude-opus-4.8")


class TestAdvertisedModelIds:
    def test_extracts_ids_and_tolerates_junk(self):
        from kiro_crew.acp.client import advertised_model_ids

        assert advertised_model_ids([{"modelId": "a"}, {"value": "b"}]) == ["a", "b"]
        # Non-list, non-dict members, and blank ids are all dropped rather than
        # raising inside session startup.
        assert advertised_model_ids(None) == []
        assert advertised_model_ids("nope") == []
        assert advertised_model_ids([{"modelId": "  "}, "x", 7]) == []
