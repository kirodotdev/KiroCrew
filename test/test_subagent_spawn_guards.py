"""What ``SubagentManager.spawn`` checks and resolves, and the text a run's end carries.

This file pins the host-memory check and the deferral it causes, the empty-task refusal,
the parent-trust approval skip, agent inheritance, the effective turn limit and the
memory mode a spawn snapshots, plus the timeout context a timed-out run reports and the
``completion_keep`` truncation, its wiring and its config validation. The family's other
files are described in ``test_subagent.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_helpers import (
    _mock_ctx_builder,
    _mock_ctx_builder_auto_spawn,
    _mock_sessions,
)

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


class TestTimeoutContext:
    """Tests for _timeout_context() helper."""

    def test_basic_with_elapsed(self) -> None:
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        # flake-ok: pre-existing 60 s offset from the real clock moved verbatim; the whole-second elapsed holds unless a second passes between the two clock reads
        info = SubagentInfo(id="t1", task="test", turns=5, max_turns=30, started=time.time() - 60)
        ctx = _timeout_context(info)
        assert "turn 5/30" in ctx
        assert "elapsed: 60s" in ctx

    def test_no_elapsed(self) -> None:
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(id="t1", task="test", turns=5, max_turns=30, started=time.time())
        ctx = _timeout_context(info, include_elapsed=False)
        assert "turn 5/30" in ctx
        assert "elapsed" not in ctx

    def test_last_tool_included(self) -> None:
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(
            id="t1", task="test", turns=3, max_turns=30, started=time.time() - 10, last_tool="shell"
        )
        ctx = _timeout_context(info)
        assert "last tool: shell" in ctx

    def test_no_last_tool(self) -> None:
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(id="t1", task="test", turns=3, max_turns=30, started=time.time() - 10)
        ctx = _timeout_context(info)
        assert "last tool" not in ctx

    def test_stored_elapsed_preferred(self) -> None:
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(
            id="t1", task="test", turns=1, max_turns=30, started=time.time() - 999, elapsed=42.0
        )
        ctx = _timeout_context(info)
        assert "elapsed: 42s" in ctx

    def test_redaction_called(self) -> None:
        from unittest.mock import patch as _patch

        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(
            id="t1",
            task="test",
            turns=1,
            max_turns=30,
            started=time.time(),
            last_tool="some_tool",
        )
        with _patch("kiro_crew.subagent._redact", return_value="[REDACTED]") as mock_redact:
            ctx = _timeout_context(info, include_elapsed=False)
        mock_redact.assert_called_once_with("some_tool")
        assert "last tool: [REDACTED]" in ctx

    def test_unset_cap_omitted_not_slash_zero(self) -> None:
        """max_turns=0 (no per-spawn cap) must not render a misleading 'turn N/0'."""
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(id="t1", task="test", turns=41, max_turns=0, started=time.time() - 10)
        ctx = _timeout_context(info)
        assert "turn 41" in ctx
        assert "turn 41/" not in ctx

    def test_resolved_turn_limit_used_over_raw_zero(self) -> None:
        """The caller-resolved effective cap is shown when the raw override is unset."""
        from kiro_crew.subagent import SubagentInfo, _timeout_context

        info = SubagentInfo(id="t1", task="test", turns=41, max_turns=0, started=time.time() - 10)
        ctx = _timeout_context(info, turn_limit=100)
        assert "turn 41/100" in ctx


class TestEffectiveTurnLimit:
    """Resolution chain: per-spawn max_turns → manager default → hardcoded."""

    def _manager(self, default_turn_limit: int) -> "SubagentManager":
        return SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=None,
            default_turn_limit=default_turn_limit,
        )

    def test_unconfigured_manager_has_finite_long_task_budget(self) -> None:
        from kiro_crew.subagent import SubagentInfo

        manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=None)
        assert manager._effective_turn_limit(SubagentInfo(id="long", task="test")) == 1000
        assert manager._default_timeout == 10800

    @pytest.mark.parametrize(
        ("max_turns", "expected"),
        [
            pytest.param(30, 30, id="test_per_spawn_override_wins"),
            pytest.param(0, 50, id="test_falls_back_to_manager_default"),
        ],
    )
    def test_turn_limit_resolution(self, max_turns: int, expected: int) -> None:
        from kiro_crew.subagent import SubagentInfo

        manager = self._manager(default_turn_limit=50)
        info = SubagentInfo(id="t1", task="test", max_turns=max_turns)
        assert manager._effective_turn_limit(info) == expected

    def test_falls_back_to_hardcoded_limit(self) -> None:
        from kiro_crew.subagent import _TURN_LIMIT, SubagentInfo

        manager = self._manager(default_turn_limit=0)
        info = SubagentInfo(id="t1", task="test", max_turns=0)
        assert manager._effective_turn_limit(info) == _TURN_LIMIT


class TestAgentInheritance:
    """Agent name is inherited from parent session when not explicitly specified."""

    @pytest.mark.asyncio
    async def test_inherits_agent_from_parent(self) -> None:
        from typing import Any

        from kiro_crew.subagent import SubagentInfo

        sessions = _mock_sessions()
        sessions.get_agent = MagicMock(return_value="parent-agent")
        sessions.get_agent_selection = MagicMock(return_value=("template", "parent-agent"))

        events: list[tuple[str, dict]] = []

        async def capture(name: str, _info: Any, data: dict) -> None:
            events.append((name, data))

        mgr = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder_auto_spawn(),
            on_event=capture,
        )
        info = SubagentInfo(
            execution_context=execution_for_store("", template_id="parent-agent"),
            id="sub-1",
            task="do stuff",
            parent_session_key="parent-key",
            agent="",
        )
        mgr._log_spawned(info)
        await mgr._run(info)

        # get_or_create should receive the inherited agent
        sessions.get_or_create.assert_called_once()
        call_kwargs = sessions.get_or_create.call_args[1]
        assert call_kwargs["agent"] == "parent-agent"

        # spawn event should report the inherited agent
        spawn_events = [(n, d) for n, d in events if n == "subagent_spawn"]
        assert spawn_events
        assert spawn_events[0][1]["agent"] == "parent-agent"


class TestParentTrustedSpawnApproval:
    """When parent session has approval_policy='auto', spawns skip approval."""

    @pytest.mark.asyncio
    async def test_parent_trusted_skips_approval(self) -> None:
        """Spawn auto-approved when parent session approval_policy is 'auto'."""
        sessions = _mock_sessions()
        sessions.get_approval_policy = MagicMock(return_value="auto")
        approval_callback = AsyncMock(return_value=True)

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_spawn_approval=approval_callback,
        )

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("trusted task", parent_session_key="dashboard:chat-1")
            assert info is not None
            # flake-ok: pre-existing await of a task this test drives to completion moved verbatim; tracked for a de-flake pass
            await manager._tasks[info.id]

        assert info.done is True
        assert info.error == ""
        approval_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parent_not_trusted_requires_approval(self) -> None:
        """Spawn requires approval when parent session approval_policy is not 'auto'."""
        sessions = _mock_sessions()
        sessions.get_approval_policy = MagicMock(return_value="")
        approval_callback = AsyncMock(return_value=True)

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_spawn_approval=approval_callback,
        )

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("untrusted task", parent_session_key="dashboard:chat-1")
            assert info is not None
            # flake-ok: pre-existing await of a task this test drives to completion moved verbatim; tracked for a de-flake pass
            await manager._tasks[info.id]

        assert info.done is True
        approval_callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parent_trusted_without_session_key_requires_approval(self) -> None:
        """Spawn requires approval when no parent_session_key is provided."""
        sessions = _mock_sessions()
        sessions.get_approval_policy = MagicMock(return_value="auto")
        approval_callback = AsyncMock(return_value=True)

        manager = SubagentManager(
            sessions=sessions,
            ctx_builder=_mock_ctx_builder(),
            on_spawn_approval=approval_callback,
        )

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("no parent task")
            assert info is not None
            # flake-ok: pre-existing await of a task this test drives to completion moved verbatim; tracked for a de-flake pass
            await manager._tasks[info.id]

        assert info.done is True
        approval_callback.assert_awaited_once()


class TestCheckMemoryAvailable:
    """Tests for the memory guard function."""

    @pytest.mark.parametrize(
        ("meminfo", "expected_ok", "expected_gb"),
        [
            pytest.param(
                "MemTotal:       32768000 kB\nMemAvailable:    8388608 kB\n",
                True,
                8.0,
                id="test_sufficient_memory",
            ),
            pytest.param(
                "MemAvailable:    3145728 kB\n", False, 3.0, id="test_insufficient_memory"
            ),
        ],
    )
    def test_threshold_against_mem_available(self, tmp_path, meminfo, expected_ok, expected_gb):
        """Returns (MemAvailable >= threshold, MemAvailable in GB)."""
        from kiro_crew.subagent import check_memory_available

        f = tmp_path / "meminfo"
        f.write_text(meminfo)
        ok, avail = check_memory_available(min_gb=4.0, path=str(f))
        assert ok is expected_ok
        assert avail == expected_gb

    def test_file_not_found_fails_open(self):
        """Returns (True, -1.0) when /proc/meminfo is unreadable — fails open."""
        from kiro_crew.subagent import check_memory_available

        ok, avail = check_memory_available(path="/nonexistent/path/meminfo")
        assert ok is True
        assert avail == -1.0

    def test_custom_threshold(self, tmp_path):
        """Respects custom min_gb parameter."""
        from kiro_crew.subagent import check_memory_available

        f = tmp_path / "meminfo"
        f.write_text("MemAvailable:    5242880 kB\n")
        ok, _ = check_memory_available(min_gb=6.0, path=str(f))
        assert ok is False
        ok, _ = check_memory_available(min_gb=4.0, path=str(f))
        assert ok is True

    def test_does_not_route_through_path_gate(self, tmp_path):
        """The constant kernel path bypasses the agent-path gate.

        A gate refusal under load must not silently disable spawn
        back-pressure, so the read is a plain ``open``. A function-local
        re-route through either gate helper makes this test fail loudly.
        """
        from unittest.mock import patch

        from kiro_crew.subagent import check_memory_available

        f = tmp_path / "meminfo"
        f.write_text("MemAvailable:    8388608 kB\n")
        with (
            patch(
                "kiro_crew.hooks.safe_read_file",
                side_effect=AssertionError("gate must not be consulted"),
            ),
            patch(
                "kiro_crew.hooks.is_sensitive_path",
                side_effect=AssertionError("gate must not be consulted"),
            ),
        ):
            ok, avail = check_memory_available(min_gb=4.0, path=str(f))
        assert ok is True
        assert avail == 8.0

    def test_permission_error_fails_open(self, tmp_path):
        """A real EACCES from open() is an OSError and fails open (True, -1.0)."""
        from unittest.mock import patch

        from kiro_crew.subagent import check_memory_available

        with patch("builtins.open", side_effect=PermissionError("denied")):
            ok, avail = check_memory_available(path=str(tmp_path / "meminfo"))
        assert ok is True
        assert avail == -1.0

    def test_malformed_meminfo_indexerror(self, tmp_path):
        """Handles malformed MemAvailable line without value — fails open."""
        from kiro_crew.subagent import check_memory_available

        f = tmp_path / "meminfo"
        f.write_text("MemAvailable:\n")
        ok, avail = check_memory_available(path=str(f))
        assert ok is True
        assert avail == -1.0


class TestSpawnMemoryGuard:
    """spawn() under low memory: deferred into the durable queue, or refused
    when no store backs the deferral."""

    def _mgr(self):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentManager

        return SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=MagicMock(),
            on_done=MagicMock(),
            max_concurrent=3,
        )

    def test_spawn_deferred_low_memory(self):
        """With the task store open, the row stays queued with a retry time."""
        from unittest.mock import MagicMock, patch

        mgr = self._mgr()
        assert mgr._taskq is not None

        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(False, 2.5)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        assert info is not None
        assert info.done is False and info.queued is True
        row = mgr._taskq.get(info.id)
        assert row is not None and row.state == "queued" and row.next_run_at is not None
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "deferred_low_memory"
        assert call_kwargs["metadata"]["available_gb"] == 2.5

    def test_spawn_queued_low_memory_without_a_store(self):
        """Without a store, spawn() still queues: the in-memory window holds it."""
        from unittest.mock import MagicMock, patch

        mgr = self._mgr()
        mgr._taskq = None

        with (
            patch("kiro_crew.subagent.check_memory_available", return_value=(False, 2.5)),
            patch("kiro_crew.subagent.KiroCrewConfig") as mock_cfg,
            patch("kiro_crew.subagent.sel") as mock_sel,
        ):
            mock_cfg.load.return_value.agent.spawn_min_memory_gb = 4.0
            mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
            mock_sel.return_value.log_tool_invocation = MagicMock()

            info = mgr.spawn(task="test task", parent_session_key="sess-1")

        assert info is not None
        assert info.done is False and info.queued is True and info.error == ""
        assert info.queued_reason == "low_memory"
        assert "2.5 GB available" in info.queued_reason_detail
        assert "need 5.0 GB" in info.queued_reason_detail  # the 4.0 floor plus this start
        assert "1.00 GB for this start" in info.queued_reason_detail
        assert [p["_preassigned_id"] for p in mgr._queue] == [info.id]
        assert info.id not in mgr._agents and mgr._running_count == 0
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "deferred_low_memory"


class TestSpawnEmptyTaskGuard:
    """spawn() refuses empty/whitespace tasks at the choke point.

    The HTTP handler (api_spawn) and MCP tool schemas validate too, but
    direct Python callers (gateway internals, apps, task runner) reach
    spawn() unvalidated — an empty task produces a useless subagent and
    a blank Activity card in the dashboard.
    """

    def _mgr(self):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentManager

        return SubagentManager(
            sessions=MagicMock(),
            ctx_builder=MagicMock(),
            on_done=MagicMock(),
            max_concurrent=3,
        )

    @pytest.mark.parametrize(
        "task",
        [
            pytest.param("", id="test_spawn_rejected_empty_task"),
            pytest.param("   \n\t ", id="test_spawn_rejected_whitespace_task"),
        ],
    )
    def test_spawn_rejected_blank_task(self, task):
        """A blank task returns a done SubagentInfo with an error + SEL audit."""
        from unittest.mock import MagicMock, patch

        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            info = mgr.spawn(task=task, parent_session_key="sess-1")

        assert info is not None
        assert info.done is True
        assert "non-empty" in info.error
        call_kwargs = mock_sel.return_value.log_tool_invocation.call_args[1]
        assert call_kwargs["outcome"] == "rejected_empty_task"

    def test_spawn_rejection_survives_sel_failure(self):
        """The rejection SubagentInfo is returned even when SEL audit raises."""
        from unittest.mock import patch

        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel", side_effect=RuntimeError("sel down")):
            info = mgr.spawn(task="", parent_session_key="sess-1")

        assert info is not None
        assert info.done is True
        assert "non-empty" in info.error

    def test_spawn_rejected_none_task(self):
        """A None task is rejected gracefully — the guard runs before redaction,
        which would otherwise raise on a non-string task."""
        from unittest.mock import MagicMock, patch

        mgr = self._mgr()
        with patch("kiro_crew.subagent.sel") as mock_sel:
            mock_sel.return_value.log_tool_invocation = MagicMock()
            info = mgr.spawn(task=None, parent_session_key="sess-1")  # type: ignore[arg-type]

        assert info is not None
        assert info.done is True
        assert "non-empty" in info.error


class TestCompletionKeepHelper:
    """Tests for the apply_completion_keep helper in context_management."""

    def test_short_text_unchanged(self) -> None:
        """Texts at or below max_chars are returned unchanged."""
        from kiro_crew.context_management import apply_completion_keep

        assert apply_completion_keep("hello", "head", 100) == "hello"
        assert apply_completion_keep("hello", "tail", 100) == "hello"
        assert apply_completion_keep("hello", "both", 100) == "hello"

    def test_max_chars_zero_or_negative_disables_truncation(self) -> None:
        """max_chars <= 0 returns the input verbatim (truncation disabled)."""
        from kiro_crew.context_management import apply_completion_keep

        text = "a" * 10000
        assert apply_completion_keep(text, "head", 0) == text
        assert apply_completion_keep(text, "tail", 0) == text
        assert apply_completion_keep(text, "both", -1) == text

    def test_head_keeps_first_n_chars(self) -> None:
        """Mode 'head' keeps the first max_chars characters (default behavior)."""
        from kiro_crew.context_management import apply_completion_keep

        text = "ABCDEFGHIJ" * 1000  # 10000 chars
        result = apply_completion_keep(text, "head", 50)
        assert len(result) == 50
        assert result == text[:50]

    def test_tail_keeps_last_n_chars(self) -> None:
        """Mode 'tail' keeps the last max_chars characters."""
        from kiro_crew.context_management import apply_completion_keep

        text = "ABCDEFGHIJ" * 1000
        result = apply_completion_keep(text, "tail", 50)
        assert len(result) == 50
        assert result == text[-50:]

    def test_both_keeps_head_marker_and_tail(self) -> None:
        """Mode 'both' keeps head, then a marker, then tail."""
        from kiro_crew.context_management import apply_completion_keep

        # Use a text where head and tail are distinguishable.
        text = "H" * 5000 + "T" * 5000  # 10000 chars
        result = apply_completion_keep(text, "both", 200)
        assert len(result) <= 200
        assert "[...middle elided...]" in result
        assert result.startswith("H")
        assert result.endswith("T")

    def test_both_with_tiny_budget_falls_back_to_head(self) -> None:
        """When max_chars cannot fit the marker plus content, 'both' falls back to head."""
        from kiro_crew.context_management import apply_completion_keep

        text = "ABCDEFGHIJ" * 100
        # Marker is ~25 chars; budget of 10 cannot fit head + marker + tail.
        result = apply_completion_keep(text, "both", 10)
        assert result == text[:10]


class TestSubagentManagerCompletionKeepWiring:
    """Tests that SubagentManager honors the completion_keep constructor params."""

    @pytest.mark.asyncio
    async def test_default_completion_keep_is_head(self) -> None:
        """Without explicit kwargs, manager defaults to head + module default chars."""
        from kiro_crew.context_management import COMPLETION_KEEP_DEFAULT_CHARS

        manager = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=_mock_ctx_builder(),
        )
        assert manager._completion_keep == "head"
        assert manager._completion_keep_chars == COMPLETION_KEEP_DEFAULT_CHARS

    @pytest.mark.asyncio
    async def test_custom_completion_keep_stored(self) -> None:
        """Explicit completion_keep / completion_keep_chars round-trip onto attrs."""
        manager = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=_mock_ctx_builder(),
            completion_keep="tail",
            completion_keep_chars=5000,
        )
        assert manager._completion_keep == "tail"
        assert manager._completion_keep_chars == 5000


class TestCompletionKeepLoader:
    """The config loader must reject unknown completion_keep values at load time."""

    def test_load_unknown_completion_keep_raises(self) -> None:
        from kiro_crew.config.loader import _validated_completion_keep

        with pytest.raises(ValueError, match="completion_keep"):
            _validated_completion_keep("tial")

    def test_load_valid_completion_keep_accepts(self) -> None:
        from kiro_crew.config.loader import _validated_completion_keep

        for v in ("head", "tail", "both"):
            assert _validated_completion_keep(v) == v


class TestSpawnMemoryModeSnapshot:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_queue_keeps_admission_mode_after_parent_replacement(self, mode):
        from kiro_crew.subagent_persistence import read_run_memory_mode

        parent_modes = {"dashboard:parent": mode}
        resolver = MagicMock(side_effect=lambda key: parent_modes[key])
        manager = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=None,
            max_concurrent=1,
            is_yolo=lambda: True,
            memory_mode_for_session=resolver,
        )
        manager._spawn_stagger_secs = 0
        manager._running_count = 1
        manager._run = AsyncMock()
        info = manager.spawn("queued task", parent_session_key="dashboard:parent")
        assert info is not None and info.queued
        assert info.memory_mode == mode
        assert manager._queue[0]["_memory_mode"] == mode
        parent_modes["dashboard:parent"] = "persistent"
        manager._running_count = 0
        manager._drain_queue()
        await await_until(asyncio.ensure_future(asyncio.gather(*manager._tasks.values())))
        running = manager.get(info.id)
        assert running is not None and running.memory_mode == mode
        assert read_run_memory_mode(info.id) == mode
        resolver.assert_called_once_with("dashboard:parent")

    @pytest.mark.asyncio
    async def test_unknown_mode_refuses_before_queue_or_execution(self):
        manager = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=None,
            memory_mode_for_session=lambda key: "unknown",
        )
        info = manager.spawn("task", parent_session_key="dashboard:parent")
        assert info is not None and info.done
        assert info.error.startswith("memory_unavailable:")
        assert not manager._queue and not manager._tasks

    @pytest.mark.asyncio
    async def test_global_binding_write_failure_blocks_provider(self):
        from kiro_crew.subagent import SubagentInfo

        sessions = _mock_sessions()
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        info = SubagentInfo(id="mode-write-failure", task="task", memory_mode="temporary")
        with patch("kiro_crew.subagent.create_agent_folder", side_effect=OSError("write failed")):
            manager._log_spawned(info)
        assert info.error.startswith("memory_unavailable:")
        with pytest.raises(RuntimeError, match="memory_unavailable"):
            await manager._run_inner(info, f"subagent:{info.id}")
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    async def test_approval_wait_keeps_mode_after_parent_closes(self, mode):
        from kiro_crew.subagent_persistence import read_run_memory_mode

        modes = {"dashboard:parent": mode}
        entered = asyncio.Event()
        approved = asyncio.get_running_loop().create_future()

        async def approve(request_id, description, parent):
            entered.set()
            # flake-ok: pre-existing approval future the test resolves moved verbatim; tracked for a de-flake pass
            return await approved

        manager = SubagentManager(
            sessions=_mock_sessions(),
            ctx_builder=None,
            on_spawn_approval=approve,
            memory_mode_for_session=lambda key: modes[key],
        )
        manager._run = AsyncMock()
        info = manager.spawn("approved task", parent_session_key="dashboard:parent")
        assert info is not None
        try:
            await await_until(entered)
            modes.clear()
            approved.set_result(True)
            await await_until(asyncio.ensure_future(asyncio.gather(*manager._tasks.values())))
            assert info.memory_mode == mode
            assert read_run_memory_mode(info.id) == mode
        finally:
            if not approved.done():
                approved.set_result(False)
            await await_until(
                asyncio.ensure_future(
                    asyncio.gather(*manager._tasks.values(), return_exceptions=True)
                )
            )
