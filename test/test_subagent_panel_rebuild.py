"""Tests for the subagents panel durable rebuild from persistence (#759).

These drive the REAL ws.py helper, ``_collect_persisted_replay_frames``, and its
off-loop wrapper ``_load_persisted_replay_frames`` - not a re-implementation of
their logic - so a regression in the handler fallback fails the test.

Coverage targets (review findings on commit 511cca10):
- I1: the durable rebuild in ws.py is exercised end to end (reverting the
  helper changes the frames these tests assert on).
- I2: the blocking disk scan runs OFF the event loop (mirrors test_ws_offload).
- Finding 1 / M3: an agent still marked running with no tombstone whose process
  is dead is rebuilt as a terminal done card, never dropped and never left as a
  permanently stuck running snapshot.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import subagent_persistence as sp
from kiro_crew.dashboard import ws


@pytest.fixture()
def subagents_dir(tmp_path: Path):
    """Temp subagents directory the persistence layer (and ws helper) resolve to."""
    d = tmp_path / "subagents"
    d.mkdir()
    with patch.object(sp, "_SUBAGENTS_DIR", d):
        yield d


def _frame_by_id(frames: list[dict], agent_id: str) -> dict | None:
    for f in frames:
        if f.get("data", {}).get("id") == agent_id:
            return f
    return None


class TestCollectPersistedReplayFrames:
    """Drive the real ws.py durable-rebuild helper."""

    def test_running_agent_with_live_pid_yields_snapshot(self, subagents_dir: Path):
        sp.create_agent_folder(
            "abc12345",
            task="search docs for API changes\nsecond line ignored by header",
            agent="gpu-coder",
            parent_session="dashboard:slot1",
        )
        sp.update_state("abc12345", last_tool="read_file", turns=5, pid=4242, status="running")

        with patch.object(ws, "pid_exists", return_value=True):
            frames = ws._collect_persisted_replay_frames(set(), time.time())

        f = _frame_by_id(frames, "abc12345")
        assert f is not None
        assert f["type"] == "subagent_snapshot"
        assert f["data"]["slot"] == "slot1"
        assert f["data"]["task"].startswith("search docs for API changes")
        assert f["data"]["agent"] == "gpu-coder"
        assert f["data"]["last_tool"] == "read_file"
        assert f["data"]["tool_count"] == 5

    def test_delivered_tombstone_yields_completed_done(self, subagents_dir: Path):
        sp.create_agent_folder(
            "def67890",
            task="review the PR",
            agent="reviewer",
            parent_session="dashboard:my-slot",
        )
        sp.write_tombstone("def67890", cause="delivered", recovery_action="delivered")

        frames = ws._collect_persisted_replay_frames(set(), time.time())

        f = _frame_by_id(frames, "def67890")
        assert f is not None
        assert f["type"] == "subagent_done"
        assert f["data"]["outcome"] == "completed"
        assert f["data"]["error"] is None
        assert f["data"]["slot"] == "my-slot"

    def test_failure_tombstone_yields_failed_done(self, subagents_dir: Path):
        sp.create_agent_folder(
            "fai10001",
            task="do the thing",
            agent="worker",
            parent_session="dashboard:s",
        )
        sp.write_tombstone("fai10001", cause="timeout", recovery_action="killed")

        frames = ws._collect_persisted_replay_frames(set(), time.time())

        f = _frame_by_id(frames, "fai10001")
        assert f is not None
        assert f["type"] == "subagent_done"
        assert f["data"]["outcome"] == "failed"
        assert f["data"]["error"] == "timeout"

    def test_running_agent_dead_pid_no_tombstone_rebuilt_as_done(self, subagents_dir: Path):
        """FINDING 1 / M3: a crashed agent with no tombstone must still rebuild.

        It must NOT be dropped, and it must NOT replay as a permanently stuck
        running snapshot - it becomes a terminal (failed) done card.
        """
        sp.create_agent_folder(
            "dead0001",
            task="crashed before finishing",
            agent="worker",
            parent_session="dashboard:s",
        )
        sp.update_state("dead0001", pid=999999, status="running")  # no tombstone written

        with patch.object(ws, "pid_exists", return_value=False):
            frames = ws._collect_persisted_replay_frames(set(), time.time())

        f = _frame_by_id(frames, "dead0001")
        assert f is not None, "crashed agent without a tombstone must still be rebuilt"
        assert f["type"] == "subagent_done", "a dead process must not replay as a running card"
        assert f["data"]["outcome"] == "failed"
        assert not any(
            fr["type"] == "subagent_snapshot" and fr["data"]["id"] == "dead0001" for fr in frames
        )

    def test_running_agent_never_had_pid_rebuilt_as_done(self, subagents_dir: Path):
        """An agent whose pid was never recorded (None) is treated as not alive."""
        sp.create_agent_folder(
            "nopid001",
            task="never recorded a pid",
            agent="worker",
            parent_session="dashboard:s",
        )  # status running, pid=None, no tombstone

        frames = ws._collect_persisted_replay_frames(set(), time.time())

        f = _frame_by_id(frames, "nopid001")
        assert f is not None
        assert f["type"] == "subagent_done"
        assert f["data"]["outcome"] == "failed"

    def test_non_dashboard_parent_excluded(self, subagents_dir: Path):
        sp.create_agent_folder(
            "aaa11111",
            task="slack task",
            agent="kirocrew",
            parent_session="slack:thread123",
        )
        frames = ws._collect_persisted_replay_frames(set(), time.time())
        assert _frame_by_id(frames, "aaa11111") is None

    def test_seen_ids_are_skipped(self, subagents_dir: Path):
        sp.create_agent_folder(
            "known123",
            task="known task",
            agent="kirocrew",
            parent_session="dashboard:s",
        )
        frames = ws._collect_persisted_replay_frames({"known123"}, time.time())
        assert _frame_by_id(frames, "known123") is None

    def test_ttl_excludes_old_agents(self, subagents_dir: Path):
        sp.create_agent_folder(
            "old12345",
            task="ancient task",
            agent="kirocrew",
            parent_session="dashboard:x",
        )
        # Backdate beyond the TTL window.
        sp.update_state(
            "old12345", started=time.time() - (ws.NATIVE_SUBAGENT_TERMINAL_TTL_SECS + 100)
        )

        frames = ws._collect_persisted_replay_frames(set(), time.time())
        assert _frame_by_id(frames, "old12345") is None

    def test_replay_bounded_by_keep_limit(self, subagents_dir: Path):
        keep = ws.NATIVE_SUBAGENT_TERMINAL_KEEP
        for i in range(keep + 5):
            sp.create_agent_folder(
                f"agent{i:03d}",
                task=f"task {i}",
                agent="kirocrew",
                parent_session="dashboard:s",
            )
        frames = ws._collect_persisted_replay_frames(set(), time.time())
        assert len(frames) == keep


class TestReplayRunsOffEventLoop:
    """I2: the blocking disk scan must not run on the event loop thread."""

    @pytest.mark.asyncio
    async def test_collector_runs_off_loop_thread(self, monkeypatch):
        loop_thread = threading.get_ident()
        seen: dict[str, int] = {}

        def fake_collect(seen_ids, now):
            seen["thread"] = threading.get_ident()
            return [{"type": "subagent_snapshot", "data": {"id": "x"}}]

        monkeypatch.setattr(ws, "_collect_persisted_replay_frames", fake_collect)

        frames = await ws._load_persisted_replay_frames(set())

        assert frames == [{"type": "subagent_snapshot", "data": {"id": "x"}}]
        assert seen["thread"] != loop_thread, "persistence scan must run off the event loop"

    @pytest.mark.asyncio
    async def test_scan_does_not_block_the_loop(self, monkeypatch):
        ticks = 0

        async def _ticker():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.01)
                ticks += 1

        def slow_collect(seen_ids, now):
            time.sleep(0.1)
            return []

        monkeypatch.setattr(ws, "_collect_persisted_replay_frames", slow_collect)

        ticker = asyncio.create_task(_ticker())
        frames = await ws._load_persisted_replay_frames(set())
        await ticker

        assert frames == []
        assert ticks >= 5, f"event loop appears stalled during blocking scan (ticks={ticks})"
