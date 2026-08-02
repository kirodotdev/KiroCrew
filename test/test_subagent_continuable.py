"""Tests for continuable subagent conversations (spawn_run keep=True).

Covers the hibernate-first lifecycle slice:

- SessionManager continuable-key override: is_stateless bypass, sid
  persistence eligibility, release(cleanup=True) skipping file deletion,
  forget_conversation.
- SubagentManager: keep/conversation_key threading through spawn, forced
  dedicated arm (no session sharing), teardown keeping session files,
  continue_conversation typed errors (busy / gone), steer_run typed errors
  and provider dispatch, release_conversation, and the reaper TTL sweep.
- Persistence guards: orphan reconcile and tombstone prune keep session
  files for keep=True runs.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# Subagent-registry isolation is provided globally by the autouse
# ``_isolate_subagents_dir`` fixture in ``conftest.py``.


def _mock_sessions(resumed: bool = False) -> MagicMock:
    """Mock SessionManager with async methods + continuable API.

    *resumed* is the third element of get_or_create's return — continuation
    tests set True to satisfy the fail-closed resume guard.
    """
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, resumed))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.mark_continuable = MagicMock()
    sessions.unmark_continuable = MagicMock()
    sessions.resumable_sid = MagicMock(return_value="sid-123")
    sessions.forget_conversation = MagicMock(return_value="sid-123")
    sessions.conversation_provider = MagicMock(return_value="acp")
    sessions.get_provider = MagicMock(return_value=None)
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _manager(sessions: MagicMock | None = None) -> SubagentManager:
    return SubagentManager(
        sessions=sessions or _mock_sessions(),
        ctx_builder=_mock_ctx_builder(),
    )


# ── SessionManager continuable override (real SessionManager, no processes) ──


class TestSessionManagerContinuable:
    def _sessions(self):  # type: ignore[no-untyped-def]
        from kiro_crew.session import SessionManager

        with patch.object(SessionManager, "__init__", lambda self: None):
            mgr = SessionManager()  # type: ignore[call-arg]
        mgr._continuable_keys = set()
        mgr._session_map = MagicMock()
        mgr._sessions = {}
        mgr._fold_key = lambda k: k  # type: ignore[assignment]
        return mgr

    def test_mark_unmark_is_continuable(self) -> None:
        mgr = self._sessions()
        mgr.mark_continuable("subagent:abc")
        assert mgr.is_continuable("subagent:abc")
        mgr.unmark_continuable("subagent:abc")
        assert not mgr.is_continuable("subagent:abc")

    def test_release_cleanup_skipped_for_continuable(self) -> None:
        mgr = self._sessions()
        session = MagicMock()
        session.provider.session_id = "sid-1"
        mgr._sessions["subagent:abc"] = session
        mgr.mark_continuable("subagent:abc")
        with patch("kiro_crew.session.asyncio.ensure_future") as ensure:
            mgr.release("subagent:abc", cleanup=True)
        ensure.assert_not_called()
        session.semaphore.release.assert_called_once()

    def test_release_cleanup_runs_for_plain_subagent(self) -> None:
        mgr = self._sessions()
        session = MagicMock()
        session.provider.session_id = "sid-1"
        mgr._sessions["subagent:abc"] = session
        with patch(
            "kiro_crew.session.asyncio.ensure_future",
            side_effect=lambda coro: coro.close(),
        ) as ensure:
            mgr.release("subagent:abc", cleanup=True)
        ensure.assert_called_once()

    def test_forget_conversation_returns_sid_and_unmarks(self) -> None:
        mgr = self._sessions()
        mgr.mark_continuable("subagent:abc")
        mgr._session_map.get = MagicMock(return_value="sid-9")
        sid = mgr.forget_conversation("subagent:abc")
        assert sid == "sid-9"
        mgr._session_map.delete.assert_called_once_with("subagent:abc")
        assert not mgr.is_continuable("subagent:abc")


# ── keep/conversation_key threading through spawn ──


class TestKeepThreading:
    @pytest.mark.asyncio
    async def test_keep_marks_continuable_and_skips_sharing(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("task", keep=True)
            assert info is not None and not info.error
            assert info.keep is True
            await manager._tasks[info.id]
        sessions.mark_continuable.assert_called_once_with(f"subagent:{info.id}")
        conv_key = f"subagent:{info.id}"
        assert conv_key in manager._conversations
        # Teardown must NOT delete session files for keep runs.
        sessions.release.assert_called_with(conv_key, cleanup=False)

    @pytest.mark.asyncio
    async def test_plain_spawn_also_retains_files(self) -> None:
        """Retain-by-default: even non-keep runs keep session files."""
        sessions = _mock_sessions()
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("task")
            assert info is not None and not info.error
            await manager._tasks[info.id]
        sessions.mark_continuable.assert_not_called()
        sessions.release.assert_called_with(f"subagent:{info.id}", cleanup=False)

    @pytest.mark.asyncio
    async def test_conversation_key_overrides_session_key(self) -> None:
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("follow-up", keep=True, conversation_key="subagent:origrun1")
            assert info is not None and not info.error
            await manager._tasks[info.id]
        # get_or_create must be called with the ORIGINAL conversation key.
        called_key = sessions.get_or_create.call_args[0][0]
        assert called_key == "subagent:origrun1"


# ── continue_conversation ──


class TestContinueConversation:
    def test_busy_conversation_refused(self) -> None:
        manager = _manager()
        live = SubagentInfo(id="orig1234", task="t")
        manager._agents["orig1234"] = live  # not done → busy
        with patch("kiro_crew.subagent.sel"):
            info = manager.continue_conversation("orig1234", "more work")
        assert info is not None and info.done
        assert info.error.startswith("conversation_busy")

    def test_gone_conversation_refused(self) -> None:
        sessions = _mock_sessions()
        sessions.resumable_sid = MagicMock(return_value=None)
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.read_state", return_value=None),
        ):
            info = manager.continue_conversation("deadbeef", "more work")
        assert info is not None and info.done
        assert info.error.startswith("conversation_gone")

    @pytest.mark.asyncio
    async def test_continue_seeds_from_state_json(self) -> None:
        """Retain-by-default: a run with no map entry seeds from state.json."""
        sessions = _mock_sessions(resumed=True)
        # First check: no mapping. After seeding: mapping present.
        sessions.resumable_sid = MagicMock(side_effect=[None, "sid-from-state"])
        manager = _manager(sessions)
        state = {"session_id": "sid-from-state", "provider": "acp", "cwd": "/tmp/x"}
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.read_state", return_value=state),
            patch("kiro_crew.subagent.update_state") as upd,
        ):
            info = manager.continue_conversation("origrun2", "follow-up")
            assert info is not None and not info.error, info.error
            await manager._tasks[info.id]
        sessions.seed_conversation.assert_called_once_with(
            "subagent:origrun2", "sid-from-state", provider="acp", cwd="/tmp/x"
        )
        # Promotion: original run's state marked keep so the pruner retains it.
        upd.assert_any_call("origrun2", keep=True)

    def test_continue_seed_with_missing_files_is_gone(self) -> None:
        """Seeded sid whose files are gone (map self-prunes) → conversation_gone."""
        sessions = _mock_sessions()
        sessions.resumable_sid = MagicMock(return_value=None)  # both checks fail
        manager = _manager(sessions)
        state = {"session_id": "sid-stale", "provider": "acp", "cwd": ""}
        with (
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent.read_state", return_value=state),
        ):
            info = manager.continue_conversation("stalerun", "follow-up")
        assert info is not None and info.done
        assert info.error.startswith("conversation_gone")

    @pytest.mark.asyncio
    async def test_continue_dispatches_new_run_on_same_key(self) -> None:
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.continue_conversation("origrun1", "follow-up work")
            assert info is not None and not info.error, info.error
            assert info.id != "origrun1"  # new run id
            assert info.conversation_key == "subagent:origrun1"
            await manager._tasks[info.id]
        assert not info.error, info.error
        sessions.mark_continuable.assert_called_with("subagent:origrun1")
        assert sessions.get_or_create.call_args[0][0] == "subagent:origrun1"

    @pytest.mark.asyncio
    async def test_continuation_fails_closed_when_not_resumed(self) -> None:
        """session/load falling back to a fresh session must NOT execute the
        follow-up context-free — the run fails with a typed resume_failed."""
        sessions = _mock_sessions(resumed=False)
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.continue_conversation("origrun9", "follow-up work")
            assert info is not None and not info.error, info.error
            await manager._tasks[info.id]
        assert info.done
        assert "resume_failed" in info.error
        # The prompt must never have been sent on the fresh session.
        provider = sessions.get_or_create.return_value[0]
        provider.stream.assert_not_called()


# ── steer_run ──


class TestSteerRun:
    @pytest.mark.asyncio
    async def test_unknown_id(self) -> None:
        manager = _manager()
        ok, detail = await manager.steer_run("nope", "hi")
        assert not ok and detail == "not_found"

    @pytest.mark.asyncio
    async def test_finished_run_refused(self) -> None:
        manager = _manager()
        manager._agents["a1"] = SubagentInfo(id="a1", task="t", done=True)
        ok, detail = await manager.steer_run("a1", "hi")
        assert not ok and detail.startswith("not_running")

    @pytest.mark.asyncio
    async def test_steer_dedicated_provider(self) -> None:
        sessions = _mock_sessions()
        provider = AsyncMock()
        provider.steer = AsyncMock(return_value=True)
        sessions.get_provider = MagicMock(return_value=provider)
        manager = _manager(sessions)
        manager._agents["a1"] = SubagentInfo(id="a1", task="t")
        with patch("kiro_crew.subagent.sel"):
            ok, detail = await manager.steer_run("a1", "course correct")
        assert ok and detail == "ok"
        provider.steer.assert_awaited_once_with("course correct")

    @pytest.mark.asyncio
    async def test_steer_shared_provider(self) -> None:
        manager = _manager()
        shared = AsyncMock()
        shared.steer = AsyncMock(return_value=True)
        info = SubagentInfo(id="a1", task="t")
        info._session_sharing = True
        info._shared_provider = shared
        manager._agents["a1"] = info
        with patch("kiro_crew.subagent.sel"):
            ok, _ = await manager.steer_run("a1", "adjust")
        assert ok
        shared.steer.assert_awaited_once_with("adjust")

    @pytest.mark.asyncio
    async def test_no_session_reachable(self) -> None:
        sessions = _mock_sessions()
        sessions.get_provider = MagicMock(return_value=None)
        manager = _manager(sessions)
        manager._agents["a1"] = SubagentInfo(id="a1", task="t")
        ok, detail = await manager.steer_run("a1", "hi")
        assert not ok and detail == "no_session"


# ── release_conversation + TTL sweep ──


class TestReleaseAndSweep:
    def test_release_busy_refused(self) -> None:
        manager = _manager()
        manager._agents["c1"] = SubagentInfo(id="c1", task="t")
        ok, detail = manager.release_conversation("c1")
        assert not ok and detail.startswith("conversation_busy")

    def test_queued_continuation_blocks_release_and_continue(self) -> None:
        """GPT review (PR #1023): a continuation waiting in the spawn queue
        must count as busy — otherwise spawn_release deletes the session
        files the queued run needs (it would die with resume_failed), and a
        second continue could race the same conversation."""
        manager = _manager()
        manager._queue.append(
            {
                "task": "queued follow-up",
                "conversation_key": "subagent:qc1",
                "_preassigned_id": "newrun99",
            }
        )
        ok, detail = manager.release_conversation("qc1")
        assert not ok and detail.startswith("conversation_busy")
        with patch("kiro_crew.subagent.sel"):
            info = manager.continue_conversation("qc1", "another follow-up")
        assert info is not None and info.done
        assert info.error.startswith("conversation_busy")

    def test_queued_plain_run_blocks_release_of_its_own_conversation(self) -> None:
        """A queued plain run (no conversation_key) occupies its own
        preassigned id's conversation."""
        manager = _manager()
        manager._queue.append(
            {"task": "queued plain", "conversation_key": "", "_preassigned_id": "qp1"}
        )
        ok, detail = manager.release_conversation("qp1")
        assert not ok and detail.startswith("conversation_busy")

    def test_release_deletes_files_and_registry(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        manager._conversations["subagent:c1"] = time.time()
        with patch("kiro_crew.subagent._cleanup_session_files_sync") as cleanup:
            ok, detail = manager.release_conversation("c1")
        assert ok and detail == "released"
        cleanup.assert_called_once_with("sid-123", "acp")
        assert "subagent:c1" not in manager._conversations
        sessions.forget_conversation.assert_called_once_with("subagent:c1")

    def test_release_gone_when_no_sid(self) -> None:
        sessions = _mock_sessions()
        sessions.forget_conversation = MagicMock(return_value=None)
        manager = _manager(sessions)
        ok, detail = manager.release_conversation("c1")
        assert not ok and detail.startswith("conversation_gone")

    def test_sweep_expires_only_idle_past_ttl(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        now = time.time()
        manager._conversations["subagent:old1"] = now - 7 * 3600  # expired
        manager._conversations["subagent:new1"] = now - 60  # fresh
        with patch("kiro_crew.subagent._cleanup_session_files_sync"):
            manager._sweep_conversations(now)
        assert "subagent:old1" not in manager._conversations
        assert "subagent:new1" in manager._conversations

    def test_sweep_refreshes_busy_conversation(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        now = time.time()
        manager._conversations["subagent:busy1"] = now - 7 * 3600
        live = SubagentInfo(id="busy1", task="t")  # not done
        manager._agents["busy1"] = live
        manager._sweep_conversations(now)
        assert manager._conversations["subagent:busy1"] == now  # refreshed


# ── persistence guards ──


class TestKeepTranscript:
    """AcpSessionHandle.destroy() honors keep_transcript (shared arm)."""

    def _handle(self):  # type: ignore[no-untyped-def]
        from kiro_crew.acp.session_handle import AcpSessionHandle

        with patch.object(AcpSessionHandle, "__init__", lambda self: None):
            h = AcpSessionHandle()  # type: ignore[call-arg]
        h._session_id = "sid-h"
        h.keep_transcript = False
        h._runtime = MagicMock()
        h._runtime.terminate_session = AsyncMock()
        return h

    @pytest.mark.asyncio
    async def test_destroy_deletes_transcript_by_default(self) -> None:
        h = self._handle()
        with patch.object(h, "_cleanup_transcript", MagicMock()) as cleanup:
            await h.destroy()
        cleanup.assert_called_once()
        h._runtime.terminate_session.assert_awaited_once_with("sid-h")

    @pytest.mark.asyncio
    async def test_destroy_keeps_transcript_when_flagged(self) -> None:
        h = self._handle()
        h.keep_transcript = True
        with patch.object(h, "_cleanup_transcript", MagicMock()) as cleanup:
            await h.destroy()
        cleanup.assert_not_called()
        # terminate_session still runs — RSS reclaim is unconditional.
        h._runtime.terminate_session.assert_awaited_once_with("sid-h")

    @pytest.mark.asyncio
    async def test_shared_arm_teardown_sets_keep_transcript(self) -> None:
        """SubagentManager teardown flags the shared provider before shutdown."""
        manager = _manager()
        info = SubagentInfo(id="sh1", task="t")
        info._session_sharing = True
        shared = MagicMock()
        shared.set_keep_transcript = MagicMock()
        shared.shutdown = AsyncMock()
        info._shared_provider = shared
        await manager._teardown_run_session(info, "subagent:sh1")
        shared.set_keep_transcript.assert_called_once_with(True)
        shared.shutdown.assert_awaited_once()


class TestPersistenceGuards:
    def test_tombstone_prune_keeps_files_for_keep_runs(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "keeprun1"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(agent_id, session_id="sid-k", provider="acp", keep=True)
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        # Force the tombstone past the cutoff and strip its own session_id so
        # the pruner falls back to state.json (where the keep flag lives).
        d = sp._agent_dir(agent_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 0
        ts.pop("session_id", None)
        ts_path.write_text(json.dumps(ts))
        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)
        assert pruned >= 1
        cleanup.assert_not_called()

    def test_tombstone_prune_cleans_files_for_plain_runs(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "plainrun"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(agent_id, session_id="sid-p", provider="acp")
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        d = sp._agent_dir(agent_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 0
        ts.pop("session_id", None)
        ts_path.write_text(json.dumps(ts))
        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)
        assert pruned >= 1
        cleanup.assert_called_once()


# ── conversation TTL registry rebuild on restart ──


class TestConversationTTLRegistryRebuild:
    """Tests for rebuilding the conversation TTL registry on gateway restart.

    The registry is in-memory only, so a restart loses it. Promoted
    conversations (keep=True) would leak past TTL without rebuild.
    """

    def _write_state_with_timestamp(self, agent_id: str, keep: bool, updated_at: float) -> None:
        """Write state.json with explicit timestamp (update_state overwrites it)."""
        import json

        import kiro_crew.subagent_persistence as sp

        sp.create_agent_folder(agent_id, task="t")
        d = sp._agent_dir(agent_id)
        state = json.loads((d / "state.json").read_text())
        state["keep"] = keep
        state["updated_at"] = updated_at
        (d / "state.json").write_text(json.dumps(state))

    @pytest.mark.asyncio
    async def test_rebuild_restores_fresh_conversations(self) -> None:
        """Conversations still within TTL are restored with original timestamp."""
        sessions = _mock_sessions()
        manager = _manager(sessions)
        now = time.time()
        fresh_ts = now - 3600  # 1h ago - still within 6h TTL

        self._write_state_with_timestamp("fresh1", keep=True, updated_at=fresh_ts)

        await manager._rebuild_conversation_registry()

        # Verify it was restored with original timestamp (not extended)
        assert "subagent:fresh1" in manager._conversations
        assert manager._conversations["subagent:fresh1"] == fresh_ts
        sessions.mark_continuable.assert_called_once_with("subagent:fresh1")

    @pytest.mark.asyncio
    async def test_rebuild_expires_stale_conversations(self) -> None:
        """Conversations past TTL are swept immediately, not given fresh TTL."""
        sessions = _mock_sessions()
        manager = _manager(sessions)
        now = time.time()
        stale_ts = now - 8 * 3600  # 8h ago - past 6h TTL

        self._write_state_with_timestamp("stale1", keep=True, updated_at=stale_ts)

        with patch.object(
            manager, "release_conversation", return_value=(True, "released")
        ) as release:
            await manager._rebuild_conversation_registry()

        # Verify it was released, not restored
        release.assert_called_once_with("stale1")
        assert "subagent:stale1" not in manager._conversations

    @pytest.mark.asyncio
    async def test_rebuild_handles_mixed_fresh_and_stale(self) -> None:
        """A mix of fresh and stale entries are handled correctly."""
        sessions = _mock_sessions()
        manager = _manager(sessions)
        now = time.time()

        # One fresh (2h old), one stale (10h old)
        self._write_state_with_timestamp("mix_fresh", keep=True, updated_at=now - 2 * 3600)
        self._write_state_with_timestamp("mix_stale", keep=True, updated_at=now - 10 * 3600)

        with patch.object(
            manager, "release_conversation", return_value=(True, "released")
        ) as release:
            await manager._rebuild_conversation_registry()

        # Fresh restored, stale released
        assert "subagent:mix_fresh" in manager._conversations
        release.assert_called_once_with("mix_stale")

    @pytest.mark.asyncio
    async def test_rebuild_ignores_non_keep_folders(self) -> None:
        """Folders without keep=True are not restored."""
        sessions = _mock_sessions()
        manager = _manager(sessions)

        self._write_state_with_timestamp("plain1", keep=False, updated_at=time.time())

        await manager._rebuild_conversation_registry()

        assert "subagent:plain1" not in manager._conversations
        sessions.mark_continuable.assert_not_called()

    @pytest.mark.asyncio
    async def test_rebuild_handles_empty_subagents_dir(self) -> None:
        """Empty or missing subagents directory is handled gracefully."""
        sessions = _mock_sessions()
        manager = _manager(sessions)

        # Should not raise, just return
        await manager._rebuild_conversation_registry()

        assert len(manager._conversations) == 0


class TestListContinuableConversations:
    """Tests for list_continuable_conversations in subagent_persistence."""

    def _write_state_with_timestamp(self, agent_id: str, keep: bool, updated_at: float) -> None:
        """Write state.json with explicit timestamp."""
        import json

        import kiro_crew.subagent_persistence as sp

        sp.create_agent_folder(agent_id, task="t")
        d = sp._agent_dir(agent_id)
        state = json.loads((d / "state.json").read_text())
        state["keep"] = keep
        state["updated_at"] = updated_at
        (d / "state.json").write_text(json.dumps(state))

    def test_returns_keep_true_entries(self) -> None:
        import kiro_crew.subagent_persistence as sp

        self._write_state_with_timestamp("keep1", keep=True, updated_at=1000.0)

        entries = sp.list_continuable_conversations()
        assert ("keep1", 1000.0) in entries

    def test_ignores_keep_false_entries(self) -> None:
        import kiro_crew.subagent_persistence as sp

        self._write_state_with_timestamp("nokeep1", keep=False, updated_at=1000.0)

        entries = sp.list_continuable_conversations()
        ids = [e[0] for e in entries]
        assert "nokeep1" not in ids

    def test_falls_back_to_mtime_when_updated_at_missing(self) -> None:
        import json

        import kiro_crew.subagent_persistence as sp

        sp.create_agent_folder("nomtime", task="t")
        d = sp._agent_dir("nomtime")
        state = json.loads((d / "state.json").read_text())
        state["keep"] = True
        state.pop("updated_at", None)
        (d / "state.json").write_text(json.dumps(state))

        entries = sp.list_continuable_conversations()
        entry = next((e for e in entries if e[0] == "nomtime"), None)
        assert entry is not None
        # Should have a timestamp (from mtime fallback)
        assert entry[1] > 0
