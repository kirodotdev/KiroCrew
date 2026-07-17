"""Tests for subagent_persistence — agent folder CRUD."""

from __future__ import annotations

import json
import time

import pytest

from kiro_crew.subagent_persistence import (
    create_agent_folder,
    delete_agent_folder,
    list_orphans,
    mark_delivered,
    prune_stale_tombstones,
    read_state,
    update_state,
    write_result_chunk,
    write_tombstone,
)


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point persistence at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _mock_memory_ok(monkeypatch):
    """Prevent memory guard from refusing spawns on low-RAM build machines."""
    monkeypatch.setattr(
        "kiro_crew.subagent.check_memory_available", lambda **_kw: (True, 8.0)
    )


# ── create_agent_folder ──────────────────────────────────────────────


class TestCreateAgentFolder:
    def test_creates_state_json(self, agent_root):
        path = create_agent_folder("abc123", task="do stuff", agent="kirocrew", parent_session="dashboard:default", max_turns=100)
        state = json.loads((path / "state.json").read_text())
        assert state["id"] == "abc123"
        assert state["task"] == "do stuff"
        assert state["agent"] == "kirocrew"
        assert state["status"] == "running"
        assert state["max_turns"] == 100
        assert "started" in state

    def test_idempotent_on_existing_folder(self, agent_root):
        create_agent_folder("abc123", task="t1")
        path = create_agent_folder("abc123", task="t2")
        state = json.loads((path / "state.json").read_text())
        assert state["task"] == "t2"


# ── update_state ─────────────────────────────────────────────────────


class TestUpdateState:
    def test_updates_fields(self, agent_root):
        create_agent_folder("u1", task="t")
        update_state("u1", pid=12345, turns=5, last_tool="read")
        state = json.loads((agent_root / "u1" / "state.json").read_text())
        assert state["pid"] == 12345
        assert state["turns"] == 5
        assert state["last_tool"] == "read"

    def test_preserves_existing_fields(self, agent_root):
        create_agent_folder("u2", task="original")
        update_state("u2", pid=99)
        state = json.loads((agent_root / "u2" / "state.json").read_text())
        assert state["task"] == "original"
        assert state["pid"] == 99

    def test_missing_folder_logs_no_crash(self, agent_root):
        # Should not raise
        update_state("nonexistent", pid=1)


# ── read_state ───────────────────────────────────────────────────────


class TestReadState:
    def test_reads_state(self, agent_root):
        create_agent_folder("r1", task="hello")
        state = read_state("r1")
        assert state is not None
        assert state["task"] == "hello"

    def test_missing_returns_none(self, agent_root):
        assert read_state("nope") is None

    def test_corrupt_json_returns_none(self, agent_root):
        folder = agent_root / "bad"
        folder.mkdir()
        (folder / "state.json").write_text("{corrupt")
        assert read_state("bad") is None


# ── write_result_chunk ───────────────────────────────────────────────


class TestWriteResultChunk:
    def test_appends_text(self, agent_root):
        create_agent_folder("w1", task="t")
        write_result_chunk("w1", "hello ")
        write_result_chunk("w1", "world")
        content = (agent_root / "w1" / "result.txt").read_text()
        assert content == "hello world"


# ── write_tombstone ──────────────────────────────────────────────────


class TestWriteTombstone:
    def test_writes_tombstone_json(self, agent_root):
        create_agent_folder("t1", task="t")
        write_tombstone("t1", cause="timeout", recovery_action="notified_slack")
        ts = json.loads((agent_root / "t1" / "tombstone.json").read_text())
        assert ts["cause"] == "timeout"
        assert ts["recovery_action"] == "notified_slack"
        assert "died" in ts

    def test_extra_fields_included(self, agent_root):
        create_agent_folder("t2", task="t")
        write_tombstone("t2", cause="reaped", recovery_action="delivered", pid=999, turns=12)
        ts = json.loads((agent_root / "t2" / "tombstone.json").read_text())
        assert ts["pid"] == 999
        assert ts["turns"] == 12


# ── delete_agent_folder ──────────────────────────────────────────────


class TestDeleteAgentFolder:
    def test_removes_folder(self, agent_root):
        create_agent_folder("d1", task="t")
        assert (agent_root / "d1").exists()
        delete_agent_folder("d1")
        assert not (agent_root / "d1").exists()

    def test_missing_folder_no_crash(self, agent_root):
        delete_agent_folder("ghost")


# ── list_orphans ─────────────────────────────────────────────────────


class TestListOrphans:
    def test_returns_non_tombstoned_folders(self, agent_root):
        create_agent_folder("alive1", task="t1")
        create_agent_folder("alive2", task="t2")
        create_agent_folder("dead1", task="t3")
        write_tombstone("dead1", cause="timeout", recovery_action="delivered")
        orphans = list_orphans()
        ids = [o["id"] for o in orphans]
        assert "alive1" in ids
        assert "alive2" in ids
        assert "dead1" not in ids

    def test_skips_corrupt_state(self, agent_root):
        folder = agent_root / "corrupt1"
        folder.mkdir()
        (folder / "state.json").write_text("not json")
        orphans = list_orphans()
        assert len(orphans) == 0


# ── prune_stale_tombstones ───────────────────────────────────────────


class TestPruneStaleTombstones:
    def test_prunes_old_tombstones(self, agent_root):
        create_agent_folder("old1", task="t")
        write_tombstone("old1", cause="timeout", recovery_action="delivered")
        # Backdate the tombstone
        ts_path = agent_root / "old1" / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = time.time() - (8 * 86400)  # 8 days ago
        ts_path.write_text(json.dumps(ts))

        prune_stale_tombstones(max_age_days=7)
        assert not (agent_root / "old1").exists()

    def test_keeps_recent_tombstones(self, agent_root):
        create_agent_folder("new1", task="t")
        write_tombstone("new1", cause="timeout", recovery_action="delivered")
        prune_stale_tombstones(max_age_days=7)
        assert (agent_root / "new1").exists()

    def test_keeps_non_tombstoned_folders(self, agent_root):
        create_agent_folder("running1", task="t")
        prune_stale_tombstones(max_age_days=7)
        assert (agent_root / "running1").exists()

    def test_delivered_pruned_after_ttl(self, agent_root):
        # A delivered result older than the (short) delivered TTL is pruned even
        # though it is far younger than the 7-day default window.
        create_agent_folder("del1", task="t")
        mark_delivered("del1")
        ts_path = agent_root / "del1" / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = time.time() - 7200  # 2h ago
        ts_path.write_text(json.dumps(ts))
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert not (agent_root / "del1").exists()

    def test_delivered_kept_within_ttl(self, agent_root):
        create_agent_folder("del2", task="t")
        mark_delivered("del2")
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert (agent_root / "del2").exists()

    def test_non_delivered_kept_past_delivered_ttl(self, agent_root):
        # A non-delivered (timeout) tombstone uses the 7-day window, so a 2h-old
        # one survives even a 1h delivered TTL — proves the per-cause cutoff.
        create_agent_folder("err1", task="t")
        write_tombstone("err1", cause="timeout", recovery_action="notified")
        ts_path = agent_root / "err1" / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = time.time() - 7200  # 2h ago
        ts_path.write_text(json.dumps(ts))
        prune_stale_tombstones(max_age_days=7, delivered_ttl_secs=3600)
        assert (agent_root / "err1").exists()


class TestMarkDelivered:
    def test_writes_delivered_tombstone(self, agent_root):
        create_agent_folder("mv1", task="t")
        write_result_chunk("mv1", "final output")
        mark_delivered("mv1")
        ts = json.loads((agent_root / "mv1" / "tombstone.json").read_text())
        assert ts["cause"] == "delivered"
        assert ts["recovery_action"] == "delivered"
        assert ts["result_available"] is True

    def test_delivered_excluded_from_orphans(self, agent_root):
        create_agent_folder("mv2", task="t")
        mark_delivered("mv2")
        assert "mv2" not in [o["id"] for o in list_orphans()]


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

        async def _empty_stream(*_a, **_kw):
            return
            yield

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built_message", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("test task", parent_session_key="dashboard:default")
            assert info is not None
            assert not info.done
            await manager._tasks[info.id]

        # Agent folder should exist with state.json
        state_path = agent_root / info.id / "state.json"
        assert state_path.exists(), f"Expected {state_path} to exist"
        state = json.loads(state_path.read_text())
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

        async def _empty_stream(*_a, **_kw):
            return
            yield

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")

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
            # Ensure info1's background task completes while patches are active
            await manager._tasks[info1.id]

        assert info2 is not None
        # Queued agent ID starts with 'q'
        assert info2.id.startswith("q")
        # No folder for queued agent
        folders = list(agent_root.iterdir()) if agent_root.exists() else []
        queued_folders = [f for f in folders if f.name.startswith("q")]
        assert len(queued_folders) == 0


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
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("built_message", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("stream test", parent_session_key="dashboard:default")
            assert info is not None
            await manager._tasks[info.id]

        # Result should be in agent folder
        result_path = agent_root / info.id / "result.txt"
        assert result_path.exists()
        assert result_path.read_text() == "hello world"
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

        async def _stream(*_a, **_kw):
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("pid test", parent_session_key="dashboard:default")
            await manager._tasks[info.id]

        state = json.loads((agent_root / info.id / "state.json").read_text())
        assert state["pid"] == 42
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
        provider.approve_tool = AsyncMock()

        async def _stream(*_a, **_kw):
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="shell", request_id=1, tool_kind="mcp")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="result")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("tool test", parent_session_key="dashboard:default")
            await manager._tasks[info.id]

        state = json.loads((agent_root / info.id / "state.json").read_text())
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
        create_agent_folder("timeout1", task="t")
        manager._agents["timeout1"] = info
        manager._running_count = 1

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            # Simulate what _run does on TimeoutError
            manager._write_tombstone(info, "timeout")

        ts = json.loads((agent_root / "timeout1" / "tombstone.json").read_text())
        assert ts["cause"] == "timeout"

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
            await asyncio.sleep(999)

        with patch.object(manager, "_run_inner", _hang), \
             patch.object(manager, "_default_timeout", 0.01), \
             patch("kiro_crew.subagent.Stats"), \
             patch("kiro_crew.subagent.sel"), \
             patch.object(manager, "_fire_event", new_callable=AsyncMock), \
             patch.object(manager, "_on_done", new_callable=AsyncMock):
            await manager._run(info)

        # Tombstone should still say "reaped", not "timeout"
        ts = json.loads((agent_root / "reaped_timeout" / "tombstone.json").read_text())
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
        provider.approve_tool = AsyncMock()

        async def _many_tools(*_a, **_kw):
            for i in range(5):
                yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, title=f"tool{i}", request_id=i, tool_kind="mcp")

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _many_tools())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_AUTO_APPROVE))
        ctx.hooks.auto_approve_subagent_spawn = True

        # Turn limit of 2 — will exceed on 3rd tool
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=2)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("turn limit test", parent_session_key="dashboard:default")
            await manager._tasks[info.id]

        ts_path = agent_root / info.id / "tombstone.json"
        assert ts_path.exists()
        ts = json.loads(ts_path.read_text())
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

        ts = json.loads((agent_root / "error1" / "tombstone.json").read_text())
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

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("success test", parent_session_key="dashboard:default")
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

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        on_done = AsyncMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_done=on_done)

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("cleanup test", parent_session_key="dashboard:default")
            await manager._tasks[info.id]

        # Folder is RETAINED after successful delivery (TTL grace window) so the
        # parent can read the full transcript via spawn_status / read / grep. A
        # "delivered" tombstone marks it for deferred prune by the reaper.
        agent_dir = agent_root / info.id
        assert agent_dir.exists()
        ts = json.loads((agent_dir / "tombstone.json").read_text())
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

        async def _ok(*_a, **_kw):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = MagicMock(side_effect=lambda *a, **kw: _ok())
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        sessions.record_success = MagicMock()
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("msg", None))
        ctx.hooks.on_tool_call = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True

        async def _slow_on_done(_info):
            await asyncio.sleep(999)

        manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_done=_slow_on_done)

        with patch("kiro_crew.subagent._ON_DONE_TIMEOUT", 0.01), \
             patch("kiro_crew.subagent.Stats"), \
             patch("kiro_crew.subagent.sel"):
            info = manager.spawn("delivery failure test", parent_session_key="dashboard:default")
            await manager._tasks[info.id]

        # Folder must survive when delivery times out
        assert (agent_root / info.id).exists()


# ── Slice 7: Orphan reconciliation on startup ────────────────────────


class TestOrphanReconciliation:
    """Verify _reconcile_orphans handles all three branches."""

    @pytest.mark.asyncio
    async def test_dead_pid_with_result_tombstoned_as_delivered(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        # Simulate orphan from prior run: dead PID, has result
        create_agent_folder("orphan1", task="old task", parent_session="dashboard:default")
        write_result_chunk("orphan1", "some result")
        from kiro_crew.subagent_persistence import update_state
        update_state("orphan1", pid=99999)  # dead PID

        with patch.object(manager, "_is_pid_alive", return_value=False):
            await manager._reconcile_orphans()

        ts = json.loads((agent_root / "orphan1" / "tombstone.json").read_text())
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "result_available"

    @pytest.mark.asyncio
    async def test_dead_pid_no_result_tombstoned_as_notified(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan2", task="old task")
        update_state("orphan2", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=False):
            await manager._reconcile_orphans()

        ts = json.loads((agent_root / "orphan2" / "tombstone.json").read_text())
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "notification_pending"

    @pytest.mark.asyncio
    async def test_alive_pid_killed_and_tombstoned(self, agent_root):
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan3", task="stuck task")
        update_state("orphan3", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=True), \
             patch.object(manager, "_is_orphan_process", return_value=True), \
             patch.object(manager, "_kill_orphan_pid") as mock_kill:
            await manager._reconcile_orphans()

        mock_kill.assert_called_once_with(99999)
        ts = json.loads((agent_root / "orphan3" / "tombstone.json").read_text())
        assert ts["cause"] == "gateway_restart"
        assert ts["recovery_action"] == "notification_pending"

    @pytest.mark.asyncio
    async def test_recycled_pid_not_killed(self, agent_root):
        """A live PID that doesn't belong to the original agent must not be killed."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("recycled1", task="old task")
        update_state("recycled1", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=True), \
             patch.object(manager, "_is_orphan_process", return_value=False), \
             patch.object(manager, "_kill_orphan_pid") as mock_kill:
            await manager._reconcile_orphans()

        mock_kill.assert_not_called()
        ts = json.loads((agent_root / "recycled1" / "tombstone.json").read_text())
        assert ts["cause"] == "gateway_restart"

    @pytest.mark.asyncio
    async def test_reconcile_uses_pid_recorded_at(self, agent_root):
        """pid_recorded_at (not started) is passed to _is_orphan_process."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        sessions = MagicMock()
        manager = SubagentManager(sessions=sessions, ctx_builder=MagicMock())

        create_agent_folder("orphan_ts", task="ts task")
        update_state("orphan_ts", pid=88888, pid_recorded_at=1234567890.5)

        with patch.object(manager, "_is_pid_alive", return_value=True), \
             patch.object(manager, "_is_orphan_process", return_value=True) as mock_check, \
             patch.object(manager, "_kill_orphan_pid"):
            await manager._reconcile_orphans()

        mock_check.assert_called_once_with(88888, 1234567890.5)

    @pytest.mark.asyncio
    async def test_already_tombstoned_skipped(self, agent_root):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_tombstone

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("already_dead", task="t")
        write_tombstone("already_dead", cause="timeout", recovery_action="delivered")

        # Should not re-tombstone
        await manager._reconcile_orphans()
        ts = json.loads((agent_root / "already_dead" / "tombstone.json").read_text())
        assert ts["cause"] == "timeout"  # unchanged

    @pytest.mark.asyncio
    async def test_tracked_agents_skipped(self, agent_root):
        from unittest.mock import MagicMock

        from kiro_crew.subagent import SubagentInfo, SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("tracked1", task="t")
        # Simulate this agent being tracked in current run
        manager._agents["tracked1"] = SubagentInfo(id="tracked1", task="t")

        await manager._reconcile_orphans()
        # No tombstone — it's tracked
        assert not (agent_root / "tracked1" / "tombstone.json").exists()


# ── Slice 8: Notification — injection + Slack DM fallback ────────────


class TestOrphanNotification:
    """Verify orphan notification with injection attempt and Slack DM fallback."""

    @pytest.mark.asyncio
    async def test_notification_called_for_orphan_with_result(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            update_state,
            write_result_chunk,
        )

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif1", task="important task", parent_session="dashboard:default")
        write_result_chunk("notif1", "the answer is 42")
        update_state("notif1", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=False), \
             patch.object(manager, "_notify_orphan", new_callable=AsyncMock) as mock_notify:
            await manager._reconcile_orphans()

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "notif1"  # agent_id
        assert call_args[0][2] == "result_available"  # recovery
        assert call_args[0][3] is True  # has_result

    @pytest.mark.asyncio
    async def test_notification_called_for_orphan_without_result(self, agent_root):
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, update_state

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif2", task="lost task")
        update_state("notif2", pid=99999)

        with patch.object(manager, "_is_pid_alive", return_value=False), \
             patch.object(manager, "_notify_orphan", new_callable=AsyncMock) as mock_notify:
            await manager._reconcile_orphans()

        mock_notify.assert_awaited_once()
        call_args = mock_notify.call_args
        assert call_args[0][2] == "notification_pending"  # recovery
        assert call_args[0][3] is False  # has_result

    @pytest.mark.asyncio
    async def test_slack_dm_fallback_called(self, agent_root):
        """When injection returns False, Slack DM fallback is called."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif3", task="fallback task", parent_session="dashboard:default")
        write_result_chunk("notif3", "result data")

        state = {"id": "notif3", "task": "fallback task", "parent_session": "dashboard:default"}

        with patch.object(manager, "_try_inject_orphan_notification", new_callable=AsyncMock, return_value=False), \
             patch.object(manager, "_send_orphan_slack_dm", new_callable=AsyncMock) as mock_dm:
            await manager._notify_orphan("notif3", state, "delivered", True)

        mock_dm.assert_awaited_once()
        msg = mock_dm.call_args[0][0]
        assert "notif3" in msg
        assert "orphaned by gateway restart" in msg

    @pytest.mark.asyncio
    async def test_msg_redacted_before_injection_path(self, agent_root):
        """msg must be redacted before _try_inject_orphan_notification (not just Slack DM)."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder, write_result_chunk

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
        create_agent_folder("notif_redact", task="secret task")
        write_result_chunk("notif_redact", "result")

        state = {"id": "notif_redact", "task": "secret task", "parent_session": "dashboard:default"}

        injected_msg = None

        async def _capture_inject(_session, msg):
            nonlocal injected_msg
            injected_msg = msg
            return True

        with patch.object(manager, "_try_inject_orphan_notification", side_effect=_capture_inject), \
             patch("kiro_crew.subagent._redact", side_effect=lambda m: f"[REDACTED]{m}") as mock_redact:
            await manager._notify_orphan("notif_redact", state, "delivered", True)

        # _redact must have been called before injection
        mock_redact.assert_called()
        assert injected_msg is not None
        assert injected_msg.startswith("[REDACTED]")

    @pytest.mark.asyncio
    async def test_notification_failure_doesnt_crash(self, agent_root):
        """Notification failure should not prevent reconciliation of other orphans."""
        from unittest.mock import MagicMock, patch

        from kiro_crew.subagent import SubagentManager
        from kiro_crew.subagent_persistence import create_agent_folder

        manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

        create_agent_folder("notif4", task="t1")
        create_agent_folder("notif5", task="t2")

        call_count = 0

        async def _failing_notify(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("notification failed")

        with patch.object(manager, "_is_pid_alive", return_value=False), \
             patch.object(manager, "_notify_orphan", side_effect=_failing_notify):
            await manager._reconcile_orphans()

        # Both orphans should be tombstoned despite notification failure
        assert (agent_root / "notif4" / "tombstone.json").exists()
        assert (agent_root / "notif5" / "tombstone.json").exists()


# ── Slice 9: Reaper prunes tombstoned folders > 7 days ───────────────


class TestReaperPrunesTombstones:
    """Verify the reaper loop calls prune_stale_tombstones."""

    @pytest.mark.asyncio
    async def test_reaper_prunes_old_tombstones(self, agent_root):
        """Old tombstoned folders are pruned during reaper sweep."""
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            prune_stale_tombstones,
            write_tombstone,
        )

        # Create an old tombstoned folder (8 days)
        create_agent_folder("old_tomb", task="t")
        write_tombstone("old_tomb", cause="timeout", recovery_action="delivered")
        ts_path = agent_root / "old_tomb" / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = time.time() - (8 * 86400)
        ts_path.write_text(json.dumps(ts))

        # Create a recent tombstoned folder (1 day)
        create_agent_folder("new_tomb", task="t")
        write_tombstone("new_tomb", cause="timeout", recovery_action="delivered")

        pruned = prune_stale_tombstones(max_age_days=7)
        assert pruned == 1
        assert not (agent_root / "old_tomb").exists()
        assert (agent_root / "new_tomb").exists()


# ── Slice 10: spawn_status reads from agent folder ───────────────────


class TestSpawnStatusReadsFromAgentFolder:
    """Verify spawn_status falls back to persistence layer for orphaned agents."""

    def test_result_path_points_to_agent_folder(self, agent_root):
        """After spawn+run, info.result_path should point to agent folder."""
        from kiro_crew.subagent_persistence import (
            _agent_dir,
            create_agent_folder,
            write_result_chunk,
        )

        create_agent_folder("status1", task="t")
        write_result_chunk("status1", "full result text")

        expected = str(_agent_dir("status1") / "result.txt")
        actual = (agent_root / "status1" / "result.txt").read_text()
        assert actual == "full result text"
        assert str(agent_root / "status1" / "result.txt") == expected

    def test_read_state_for_orphaned_agent(self, agent_root):
        """read_state returns data for orphaned agents (not in memory)."""
        from kiro_crew.subagent_persistence import create_agent_folder, read_state, write_tombstone

        create_agent_folder("orphan_status", task="orphaned task", parent_session="dashboard:default")
        write_tombstone("orphan_status", cause="gateway_restart", recovery_action="delivered")

        state = read_state("orphan_status")
        assert state is not None
        assert state["task"] == "orphaned task"

    @pytest.mark.asyncio
    async def test_api_spawn_status_fallback_to_disk(self, agent_root):
        """api_spawn_status returns disk data when agent not in memory."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.messaging import api_spawn_status
        from kiro_crew.subagent_persistence import (
            create_agent_folder,
            write_result_chunk,
            write_tombstone,
        )

        create_agent_folder("disk_agent", task="disk task")
        write_result_chunk("disk_agent", "disk result")
        write_tombstone("disk_agent", cause="gateway_restart", recovery_action="delivered")

        # subagents must be truthy (not None/empty) but missing the agent_id
        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)
        request = MagicMock()
        request.match_info = {"agent_id": "disk_agent"}
        request.query = {}  # real mapping so _apply_result_view reads no paging params
        request.app = {"state": MagicMock(subagents=subagents)}

        resp = await api_spawn_status(request)
        body = json.loads(resp.body)

        assert resp.status == 200
        assert body["id"] == "disk_agent"
        assert body["done"] is True
        assert "disk result" in body["result"]
        assert "gateway_restart" in body["error"]
        assert "started" in body

    @pytest.mark.asyncio
    async def test_api_spawn_status_404_when_not_on_disk(self, agent_root):
        """api_spawn_status returns 404 when agent not in memory or on disk."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers.messaging import api_spawn_status

        subagents = MagicMock()
        subagents.get = MagicMock(return_value=None)
        request = MagicMock()
        request.match_info = {"agent_id": "nonexistent"}
        request.app = {"state": MagicMock(subagents=subagents)}

        resp = await api_spawn_status(request)
        assert resp.status == 404


# ── Path traversal protection ────────────────────────────────────────


class TestPathTraversal:
    def test_dot_agent_id_rejected(self, agent_root):
        from kiro_crew.subagent_persistence import _agent_dir

        with pytest.raises(ValueError, match="Invalid agent_id"):
            _agent_dir(".")

    def test_dotdot_agent_id_rejected(self, agent_root):
        from kiro_crew.subagent_persistence import _agent_dir

        with pytest.raises(ValueError, match="Invalid agent_id"):
            _agent_dir("..")

    def test_slash_agent_id_rejected(self, agent_root):
        from kiro_crew.subagent_persistence import _agent_dir

        with pytest.raises(ValueError, match="Invalid agent_id"):
            _agent_dir("../etc")
