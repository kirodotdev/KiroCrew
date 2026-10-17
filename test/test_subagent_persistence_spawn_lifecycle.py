"""The agent folder a ``SubagentManager`` run writes, from spawn to its end.

``spawn()`` creates the folder, while a rejected spawn leaves none and a queued one
has none while it waits. The result streams into the folder, not the session
workspace, and ``state.json`` records the PID once the session exists and each
turn's count and last tool. A run stopped at its turn limit is tombstoned
``turn_limit``, ``_write_tombstone`` records a timeout's or an error's cause, a run
the reaper already tombstoned keeps that tombstone through its own timeout, and a
success with no delivery callback writes none. A delivered run keeps its folder
under a ``delivered`` tombstone while the parent may still read the transcript, and
a run whose delivery times out keeps it for recovery. The shared ``agent_root``
fixture lives in ``subagent_persistence_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from subagent_persistence_helpers import (  # noqa: F401 -- the fixture is requested by name
    agent_root_fixture,
)

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── Slice 2: spawn() creates agent folder ────────────────────────────


class TestSpawnCreatesFolder:
    """Verify SubagentManager.spawn() creates an agent folder on disk."""

    @pytest.mark.asyncio
    async def test_spawn_creates_agent_folder(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _empty_stream(*_a, **_kw):
            return
            yield

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built_message", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("test task", parent_session_key="dashboard:default")
            assert info is not None
            assert not info.done
            # flake-ok: the spawned run task ends on its own once the stubbed empty stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        # Agent folder should exist with state.json
        state_path = agent_root / info.id / "state.json"
        assert state_path.exists(), f"Expected {state_path} to exist"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["id"] == info.id
        assert state["status"] == "running"
        assert state["parent_session"] == "dashboard:default"

    @pytest.mark.asyncio
    async def test_rejected_spawn_no_folder(self, agent_root):
        """Rejected spawns should NOT leave orphaned folders."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=None)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("rejected task")

        assert info is not None
        assert info.done
        # No folder should exist for rejected agents
        assert not (agent_root / info.id).exists()

    @pytest.mark.asyncio
    async def test_queued_spawn_no_folder(self, agent_root):
        """Queued spawns should NOT get a folder until actually spawned."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _empty_stream(*_a, **_kw):
            return
            yield

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built_message", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, max_concurrent=1)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            # First spawn takes the slot
            info1 = manager.spawn("task1", parent_session_key="dashboard:default")
            # Second spawn gets queued
            info2 = manager.spawn("task2", parent_session_key="dashboard:default")

            # Assert the invariant WHILE info2 is still queued — a queued agent has no
            # folder, because the sandbox is created when the agent actually STARTS, not
            # when it is accepted. This MUST run before draining info1: completing info1's
            # task frees the slot, which promotes info2 and lets it create its folder. The
            # earlier version awaited first and then asserted "no folder", which is a
            # wall-clock race — it passed only when info2's promotion had not finished yet,
            # and failed on a slow (Windows) runner where it had. Asserted against info2's
            # real id, which does not carry a `q<n>` sentinel name to filter on.
            assert info2 is not None
            assert info2.queued is True
            folders = list(agent_root.iterdir()) if agent_root.exists() else []
            assert not any(f.name == info2.id for f in folders)
            assert not (agent_root / info2.id).exists()

            # Now drain BOTH tasks under the active patches: info1 to release the slot, then
            # info2 which its release promotes. Draining both avoids a "task was destroyed
            # but it is pending" warning that pytest reports against a LATER test.
            # flake-ok: info1's run task ends on its own over the stubbed empty stream and frees the slot; the test never has to unblock it; moved verbatim
            await manager._tasks[info1.id]
            for task in list(manager._tasks.values()):
                # flake-ok: the promoted queued run ends on its own over the stubbed empty stream; the test never has to unblock it; moved verbatim
                await task


# ── Slice 3: Result streaming to agent folder ────────────────────────


class TestResultStreamingToAgentFolder:
    """Verify result text is written to agent folder, not session_workspace."""

    @pytest.mark.asyncio
    async def test_result_written_to_agent_folder(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _stream_chunks(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello ")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="world")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream_chunks())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built_message", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("stream test", parent_session_key="dashboard:default")
            assert info is not None
            # flake-ok: the spawned run task ends on its own once the stubbed chunk stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        # Result should be in agent folder
        result_path = agent_root / info.id / "result.txt"
        assert result_path.exists()
        assert result_path.read_text(encoding="utf-8") == "hello world"
        # info.result_path should point to agent folder
        assert info.result_path == str(result_path)


# ── Slice 4: Per-turn state.json updates ─────────────────────────────


class TestPerTurnStateUpdates:
    """Verify PID and turn count are persisted to state.json."""

    @pytest.mark.asyncio
    async def test_pid_recorded_after_session_create(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=42)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        provider.session_id = "session-live"

        async def _stream(*_a, **_kw):
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("pid test", parent_session_key="dashboard:default")
            # flake-ok: the spawned run task ends on its own once the stubbed stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        state = json.loads((agent_root / info.id / "state.json").read_text(encoding="utf-8"))
        assert state["pid"] == 42
        assert state["session_id"] == "session-live"
        assert info._session_id == "session-live"
        assert info._session_provider == state["provider"]
        assert "pid_recorded_at" in state
        assert isinstance(state["pid_recorded_at"], float)

    @pytest.mark.asyncio
    async def test_turns_and_last_tool_updated(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        provider.approve_tool = AsyncMock()

        async def _stream(*_a, **_kw):
            yield LLMEvent(
                kind=EVENT_PERMISSION_REQUEST, title="shell", request_id=1, tool_kind="mcp"
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("tool test", parent_session_key="dashboard:default")
            # flake-ok: the spawned run task ends on its own once the stubbed stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        state = json.loads((agent_root / info.id / "state.json").read_text(encoding="utf-8"))
        assert state["turns"] == 1
        assert state["last_tool"] == "shell"


# ── Slice 5: Tombstone on abnormal exit ──────────────────────────────


class TestTombstoneOnAbnormalExit:
    """Verify tombstone.json is written on timeout, reap, turn_limit, cancel, error."""

    @pytest.mark.asyncio
    async def test_tombstone_on_timeout(self, agent_root):
        """Timeout in _run writes tombstone with cause=timeout."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentInfo, SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        sessions = MagicMock()
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        info = SubagentInfo(id="timeout1", task="t", parent_session_key="dashboard:default")
        info._session_id = "session-live"
        info._session_provider = "claude_code"
        info._session_cwd = "/project"
        info.keep = True
        create_agent_folder("timeout1", task="t")
        from kiro_crew.subagent_persistence import remember_live_cleanup_identity

        remember_live_cleanup_identity(
            "timeout1",
            session_id="session-live",
            provider="claude_code",
            cwd="/project",
        )
        manager._agents["timeout1"] = info
        manager._running_count = 1

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            # Simulate what _run does on TimeoutError
            manager._write_tombstone(info, "timeout")

        ts = json.loads((agent_root / "timeout1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "timeout"
        assert ts["session_id"] == "session-live"
        assert ts["provider"] == "claude_code"
        assert ts["cwd"] == "/project"
        assert "keep" not in ts

    @pytest.mark.asyncio
    async def test_timeout_skipped_when_already_reaped(self, agent_root):
        """TimeoutError path must not overwrite reaped tombstone or double-count stats."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentInfo, SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_tombstone

        sessions = MagicMock()
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        info = SubagentInfo(id="reaped_timeout", task="t", parent_session_key="dashboard:default")
        info.reaped = True
        info.error = "reaped by reaper"
        create_agent_folder("reaped_timeout", task="t")
        write_tombstone("reaped_timeout", cause="reaped", recovery_action="notification_pending")
        manager._agents["reaped_timeout"] = info
        manager._running_count = 1

        async def _hang(*a, **kw):
            # flake-ok: the stubbed _run_inner hangs so the patched 10 ms run timeout fires; the hang is under test, never a barrier
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed _run_inner's own hang, which the patched run timeout cuts; moved verbatim
            await asyncio.sleep(999)

        with (
            patch.object(manager, "_run_inner", _hang),
            patch.object(manager, "_default_timeout", 0.01),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(manager, "_fire_event", new_callable=AsyncMock),
            patch.object(manager, "_on_done", new_callable=AsyncMock),
        ):
            # flake-ok: _run returns on its patched 10 ms timeout over the hanging stub; that timeout path is under test; moved verbatim
            await manager._run(info)

        # Tombstone should still say "reaped", not "timeout"
        ts = json.loads(
            (agent_root / "reaped_timeout" / "tombstone.json").read_text(encoding="utf-8")
        )
        assert ts["cause"] == "reaped"
        # Error should not be overwritten
        assert info.error == "reaped by reaper"

    @pytest.mark.asyncio
    async def test_tombstone_on_turn_limit(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.hooks import TOOL_AUTO_APPROVE, ToolHookResult
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0
        provider.approve_tool = AsyncMock()

        async def _many_tools(*_a, **_kw):
            for i in range(5):
                yield LLMEvent(
                    kind=EVENT_PERMISSION_REQUEST, title=f"tool{i}", request_id=i, tool_kind="mcp"
                )

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _many_tools())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        ctx.hooks.auto_approve_subagent_spawn = True

        # Turn limit of 2 — will exceed on 3rd tool
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=2)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("turn limit test", parent_session_key="dashboard:default")
            # flake-ok: the spawned run task ends on its own at the 2-turn limit of the stubbed tool stream; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        ts_path = agent_root / info.id / "tombstone.json"
        assert ts_path.exists()
        ts = json.loads(ts_path.read_text(encoding="utf-8"))
        assert ts["cause"] == "turn_limit"

    @pytest.mark.asyncio
    async def test_tombstone_on_error(self, agent_root):
        """Error in _run writes tombstone with cause=error."""
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentInfo, SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        info = SubagentInfo(id="error1", task="t", parent_session_key="dashboard:default")
        create_agent_folder("error1", task="t")

        manager._write_tombstone(info, "error")

        ts = json.loads((agent_root / "error1" / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "error"

    @pytest.mark.asyncio
    async def test_no_tombstone_on_success(self, agent_root):
        """Successful completion should NOT write a tombstone."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("success test", parent_session_key="dashboard:default")
            # flake-ok: the spawned run task ends on its own once the stubbed stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        ts_path = agent_root / info.id / "tombstone.json"
        assert not ts_path.exists()


# ── Slice 6: Folder cleanup on normal completion ─────────────────────


class TestFolderCleanupOnSuccess:
    """Verify agent folder is retained (delivered tombstone) after successful
    delivery, so the parent can read the transcript during the TTL grace window."""

    @pytest.mark.asyncio
    async def test_folder_retained_on_success(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        on_done = AsyncMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_done=on_done)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("cleanup test", parent_session_key="dashboard:default")
            # flake-ok: the spawned run task ends on its own once the stubbed stream completes; the test never has to unblock it; moved verbatim
            await manager._tasks[info.id]

        # Folder is RETAINED after successful delivery (TTL grace window) so the
        # parent can read the full transcript via spawn_status / read / grep. A
        # "delivered" tombstone marks it for deferred prune by the reaper.
        agent_dir = agent_root / info.id
        assert agent_dir.exists()
        ts = json.loads((agent_dir / "tombstone.json").read_text(encoding="utf-8"))
        assert ts["cause"] == "delivered"
        on_done.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_folder_kept_on_delivery_failure(self, agent_root):
        """If on_done times out, folder should NOT be deleted (for recovery)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        # Read synchronously after every turn; as AsyncMock children they
        # would hand back coroutines nobody awaits.
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        async def _slow_on_done(_info):
            # flake-ok: the stubbed on_done hangs so the patched 10 ms delivery timeout fires; the hang is under test, never a barrier
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed on_done's own hang, which the patched delivery timeout cuts; moved verbatim
            await asyncio.sleep(999)

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_done=_slow_on_done)

        with (
            patch("kiro_crew.subagent._ON_DONE_TIMEOUT", 0.01),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("delivery failure test", parent_session_key="dashboard:default")
            # flake-ok: the run task returns once the patched 10 ms delivery timeout cuts the hanging on_done; that path is under test; moved verbatim
            await manager._tasks[info.id]

        # Folder must survive when delivery times out
        assert (agent_root / info.id).exists()
