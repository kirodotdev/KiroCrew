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

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

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
    # Read synchronously after every turn; as AsyncMock children they
    # would hand back coroutines nobody awaits.
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0

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
    sessions.is_continuable = MagicMock(return_value=False)
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


def _stop_reason(info: SubagentInfo) -> str:
    """Every marker that says WHY a run stopped, for an assertion message.

    A run cancelled from outside takes the auto-continue branch, which sets
    neither ``error`` nor ``done``: without these markers in the message, "the
    run was cancelled" is indistinguishable from "the run produced the wrong
    answer" -- the reading that let a real loop stall be reported as a
    memory-mode mismatch.
    """
    return (
        f"error={info.error!r} done={info.done} user_stopped={info.user_stopped} "
        f"reaped={info.reaped} cancel_retry_used={info._cancel_retry_used} "
        f"recovering={info._recovering} mode_ready={info._memory_mode_ready} "
        f"mode={info.memory_mode!r} turns={info.turns}"
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
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("origrun1", memory_mode="persistent")
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

    def test_promotion_write_failure_is_retryable(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        with (
            patch(
                "kiro_crew.subagent_persistence.promote_retention", side_effect=OSError("disk busy")
            ),
            patch.object(manager, "spawn") as spawn,
        ):
            info = manager.continue_conversation("retryrun", "follow-up")
        assert info.done
        assert info.error.startswith("conversation_busy")
        assert "subagent:retryrun" not in manager._conversations
        sessions.unmark_continuable.assert_called_once_with("subagent:retryrun")
        spawn.assert_not_called()

    def test_promotion_skipped_state_write_is_retryable(self) -> None:
        sessions = _mock_sessions()
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.update_state", return_value=False),
            patch.object(manager, "spawn") as spawn,
        ):
            info = manager.continue_conversation("skiprun", "follow-up")
        assert info.done
        assert info.error.startswith("conversation_busy")
        assert "subagent:skiprun" not in manager._conversations
        spawn.assert_not_called()

    def test_retryable_promotion_preserves_existing_retention(self) -> None:
        import kiro_crew.subagent_persistence as sp

        sessions = _mock_sessions()
        sessions.is_continuable.return_value = True
        manager = _manager(sessions)
        manager._conversations["subagent:kept-run"] = 123.0
        with (
            patch(
                "kiro_crew.subagent_persistence.promote_retention",
                return_value=sp.RetentionPromotionResult.RETRYABLE,
            ),
            patch.object(manager, "spawn") as spawn,
        ):
            info = manager.continue_conversation("kept-run", "follow-up")
        assert info.done
        assert info.error.startswith("conversation_busy")
        assert manager._conversations["subagent:kept-run"] == 123.0
        sessions.unmark_continuable.assert_not_called()
        spawn.assert_not_called()

    def test_concurrent_promotions_use_direct_return_values(self) -> None:
        import kiro_crew.subagent_persistence as sp

        manager = _manager(_mock_sessions())
        for agent_id in ("retry-thread", "promoted-thread"):
            sp.create_agent_folder(agent_id, task="t")

        results: dict[str, sp.RetentionPromotionResult] = {}
        errors: list[BaseException] = []

        def promote(agent_id: str) -> None:
            try:
                results[agent_id] = manager._promote_conversation(agent_id, f"subagent:{agent_id}")
            except BaseException as exc:
                errors.append(exc)

        def state_writer(agent_id: str, **_fields: object) -> bool:
            return agent_id != "retry-thread"

        threads = [
            threading.Thread(target=promote, args=(agent_id,))
            for agent_id in ("retry-thread", "promoted-thread")
        ]
        with patch("kiro_crew.subagent.update_state", side_effect=state_writer):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert results == {
            "retry-thread": sp.RetentionPromotionResult.RETRYABLE,
            "promoted-thread": sp.RetentionPromotionResult.PROMOTED,
        }

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
            patch.object(manager, "_promote_conversation", return_value=object()) as promote,
        ):
            info = manager.continue_conversation("origrun2", "follow-up")
            assert info is not None and not info.error, info.error
            await manager._tasks[info.id]
        sessions.seed_conversation.assert_called_once_with(
            "subagent:origrun2", "sid-from-state", provider="acp", cwd="/tmp/x"
        )
        promote.assert_called_once_with("origrun2", "subagent:origrun2")

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
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("origrun1", memory_mode="persistent")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
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
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("origrun9", memory_mode="persistent")
        sessions = _mock_sessions(resumed=False)
        provider = sessions.get_or_create.return_value[0]
        provider.session_id = "sid-resume-fresh"
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            info = manager.continue_conversation("origrun9", "follow-up work")
            assert info is not None and not info.error, info.error
            await manager._tasks[info.id]
        assert info.done
        assert "resume_failed" in info.error
        # The fresh session must still be reclaimable even though execution
        # fails before context construction or the state identity write.
        import json

        import kiro_crew.subagent_persistence as sp

        ts = json.loads((sp._agent_dir(info.id) / "tombstone.json").read_text())
        assert ts["session_id"] == "sid-resume-fresh"
        assert ts["provider"] == "acp"
        # The prompt must never have been sent on the fresh session.
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
        """A live run with no reachable session now gets the startup
        grace, then the typed ``session_starting`` refusal (retryable) —
        not the old terminal bare ``no_session``."""
        import kiro_crew.subagent as subagent_mod

        sessions = _mock_sessions()
        sessions.get_provider = MagicMock(return_value=None)
        manager = _manager(sessions)
        manager._agents["a1"] = SubagentInfo(id="a1", task="t")
        with (
            patch.object(subagent_mod, "_STEER_STARTUP_WAIT_SECS", 0.05),
            patch.object(subagent_mod, "_STEER_STARTUP_POLL_SECS", 0.01),
        ):
            ok, detail = await manager.steer_run("a1", "hi")
        assert not ok and detail.startswith("session_starting")


# ── release_conversation + TTL sweep ──


class TestReleaseAndSweep:
    def test_release_busy_refused(self) -> None:
        manager = _manager()
        manager._agents["c1"] = SubagentInfo(id="c1", task="t")
        ok, detail = manager.release_conversation("c1")
        assert not ok and detail.startswith("conversation_busy")

    def test_queued_continuation_blocks_release_and_continue(self) -> None:
        """A continuation waiting in the spawn queue
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

    def test_sweep_drops_malformed_registry_key(self) -> None:
        manager = _manager()
        now = time.time()
        manager._conversations["malformed"] = now - 7 * 3600
        with patch.object(manager, "release_conversation") as release:
            manager._sweep_conversations(now)
        assert "malformed" not in manager._conversations
        release.assert_not_called()

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

    # ── cancellation ──
    #
    # `AcpRuntime.terminate_session` swallows `Exception` and unregisters the
    # queue in a `finally`, precisely because `asyncio.CancelledError` is a
    # `BaseException` that would otherwise slip past its `except Exception`.
    # `destroy()` awaits it and then unlinks the transcript, so before the fix
    # that same cancellation carried straight out of the await and skipped the
    # unlink -- on gateway shutdown and abandoned turns, which is where most
    # ephemeral sessions are torn down. Nothing else deletes an ephemeral
    # session's transcript, so each skipped unlink leaks a file permanently.
    #
    # These drive the real `_cleanup_transcript` against a real sessions dir
    # rather than asserting on a mock, so they measure the file, not the call.

    def _handle_with_transcript(self, tmp_path, sid="sid-cancel"):  # type: ignore[no-untyped-def]
        from kiro_crew.acp.session_handle import AcpSessionHandle

        with patch.object(AcpSessionHandle, "__init__", lambda self: None):
            h = AcpSessionHandle()  # type: ignore[call-arg]
        h._session_id = sid
        h.keep_transcript = False
        h._runtime = MagicMock()
        sessions = tmp_path / "sessions" / "cli"
        sessions.mkdir(parents=True)
        files = [sessions / f"{sid}.json", sessions / f"{sid}.jsonl"]
        for f in files:
            f.write_text("{}", encoding="utf-8")
        return h, sessions, files

    @pytest.mark.asyncio
    async def test_destroy_deletes_transcript_when_terminate_is_cancelled(self, tmp_path) -> None:
        """A cancelled teardown must still unlink; the cancellation must propagate."""
        h, sessions, files = self._handle_with_transcript(tmp_path)
        h._runtime.terminate_session = AsyncMock(side_effect=asyncio.CancelledError())

        with patch("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions):
            with pytest.raises(asyncio.CancelledError):
                await h.destroy()

        assert [f for f in files if f.exists()] == [], (
            "a cancelled teardown leaked this session's transcript; nothing else " "deletes it"
        )

    @pytest.mark.asyncio
    async def test_destroy_deletes_transcript_when_terminate_raises(self, tmp_path) -> None:
        """Same for an ordinary exception escaping the runtime call."""
        h, sessions, files = self._handle_with_transcript(tmp_path, sid="sid-raise")
        h._runtime.terminate_session = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions):
            with pytest.raises(RuntimeError):
                await h.destroy()

        assert [f for f in files if f.exists()] == []

    @pytest.mark.asyncio
    async def test_cancelled_teardown_still_honours_keep_transcript(self, tmp_path) -> None:
        """The `finally` must not override the subagent resume guard."""
        h, sessions, files = self._handle_with_transcript(tmp_path, sid="sid-keep")
        h.keep_transcript = True
        h._runtime.terminate_session = AsyncMock(side_effect=asyncio.CancelledError())

        with patch("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions):
            with pytest.raises(asyncio.CancelledError):
                await h.destroy()

        assert all(f.exists() for f in files), (
            "keep_transcript=True is the subagent resume material and must "
            "survive a cancelled teardown too"
        )


class TestPersistenceGuards:
    def test_prune_lock_blocks_concurrent_false_to_true_promotion(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        owner_id = "promotion-race"
        continuation_id = "promotion-race-child"
        sp.create_agent_folder(owner_id, task="original")
        sp.update_state(owner_id, session_id="sid-race", provider="acp", keep=False)
        sp.create_agent_folder(continuation_id, task="continuation")
        sp.update_state(
            continuation_id,
            session_id="sid-race",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{owner_id}",
        )
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-race",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{owner_id}",
        )
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        d = sp._agent_dir(continuation_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 1
        ts_path.write_text(json.dumps(ts))

        observed_false = threading.Event()
        allow_claim = threading.Event()
        continuation_done = threading.Event()
        original_decision = sp._should_defer_tombstone_cleanup
        result: list[SubagentInfo] = []
        manager = _manager()

        def hold_after_false(**kwargs):  # type: ignore[no-untyped-def]
            observed_false.set()
            assert allow_claim.wait(timeout=5)
            return original_decision(**kwargs)

        def continue_run() -> None:
            result.append(manager.continue_conversation(owner_id, "follow-up"))
            continuation_done.set()

        with (
            patch.object(sp, "_should_defer_tombstone_cleanup", hold_after_false),
            patch.object(sp, "_cleanup_session_files_sync"),
        ):
            prune_thread = threading.Thread(
                target=sp.prune_stale_tombstones,
                kwargs={"max_age_days": 0, "delivered_ttl_secs": 0},
            )
            continuation_thread: threading.Thread | None = None
            try:
                prune_thread.start()
                assert observed_false.wait(timeout=5)

                continuation_thread = threading.Thread(target=continue_run)
                continuation_thread.start()
                assert continuation_done.wait(timeout=1)
                assert result[0].error.startswith("conversation_busy")
            finally:
                allow_claim.set()
                prune_thread.join(timeout=5)
                if continuation_thread is not None:
                    continuation_thread.join(timeout=5)

        assert not prune_thread.is_alive()
        assert continuation_thread is not None
        assert not continuation_thread.is_alive()
        assert continuation_done.is_set()
        assert len(result) == 1
        assert result[0].done
        manager._sessions.resumable_sid.return_value = None
        retry = manager.continue_conversation(owner_id, "follow-up")
        assert retry.done
        assert retry.error.startswith("conversation_gone")
        assert not d.exists()
        assert not (sp.read_state(owner_id) or {}).get("keep")

    def test_unrelated_retention_lock_does_not_block_promotion(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import kiro_crew.subagent_persistence as sp

        blocked_id = "retention-lock-blocked"
        promoted_id = "retention-lock-independent"
        for agent_id in (blocked_id, promoted_id):
            sp.create_agent_folder(agent_id, task="original")
            sp.update_state(agent_id, session_id=f"sid-{agent_id}", provider="acp", keep=False)

        holder = sp._retention_lock_for_agent(blocked_id)
        holder.lock.acquire()
        try:
            result = asyncio.run(self._promote_on_loop(sp, promoted_id))
        finally:
            holder.lock.release()

        assert result is sp.RetentionPromotionResult.PROMOTED
        assert (sp.read_state(promoted_id) or {}).get("keep") is True

    def test_same_agent_retention_lock_is_retryable(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import kiro_crew.subagent_persistence as sp

        agent_id = "promotion-retention-contention"
        sp.create_agent_folder(agent_id, task="original")
        sp.update_state(agent_id, session_id="sid-contention", provider="acp", keep=False)

        holder = sp._retention_lock_for_agent(agent_id)
        holder.lock.acquire()
        try:
            result = asyncio.run(self._promote_on_loop(sp, agent_id))
        finally:
            holder.lock.release()

        assert result is sp.RetentionPromotionResult.RETRYABLE
        assert not (sp.read_state(agent_id) or {}).get("keep")

    def test_promotion_retries_while_off_loop_writer_lock_is_held(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import kiro_crew.subagent_persistence as sp

        agent_id = "promotion-writer-contention"
        sp.create_agent_folder(agent_id, task="original")
        sp.update_state(agent_id, session_id="sid-writer", provider="acp", keep=False)
        holder = sp._lock_for_agent(agent_id)
        holder.lock.acquire()
        try:
            result = asyncio.run(self._promote_on_loop(sp, agent_id))
        finally:
            holder.lock.release()

        # RETRYABLE alone proves the loop did not queue behind the writer lock:
        # every acquire on that path is non-blocking by construction
        # (``_try_acquire_retention_lock`` then ``_try_acquire_state_lock``), and
        # the branch returns before ``state_writer`` runs, so the promotion does
        # no file I/O at all. A stopwatch around ``asyncio.run`` cannot add signal
        # here -- the lock is held by the MEASURING thread, so a blocking-acquire
        # regression self-deadlocks and hangs to the pytest timeout instead of
        # reaching an elapsed assertion, while the number it would report is pure
        # loop-construction and interpreter cost that coverage and a co-tenant
        # runner inflate. The properly shaped version of that timing property --
        # holder on a separate thread, bound DERIVED from the hold -- already
        # exists as test_a_coroutine_does_not_wait_on_a_held_lock in
        # test_subagent_state_write_serialization.py, whose own comment records a
        # bare 0.5s bound false-redding at 0.515s on a loaded runner.
        assert result is sp.RetentionPromotionResult.RETRYABLE
        assert not (sp.read_state(agent_id) or {}).get("keep")

        result = asyncio.run(self._promote_on_loop(sp, agent_id))
        assert result is sp.RetentionPromotionResult.PROMOTED
        assert (sp.read_state(agent_id) or {}).get("keep") is True

    def test_off_loop_promotion_uses_writer_lock_without_self_deadlock(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import kiro_crew.subagent_persistence as sp

        agent_id = "promotion-off-loop"
        sp.create_agent_folder(agent_id, task="original")
        sp.update_state(agent_id, session_id="sid-off-loop", provider="acp", keep=False)
        results: list[sp.RetentionPromotionResult] = []

        worker = threading.Thread(target=lambda: results.append(sp.promote_retention(agent_id)))
        worker.start()
        worker.join(timeout=2)

        assert not worker.is_alive(), "off-loop promotion self-deadlocked"
        assert results == [sp.RetentionPromotionResult.PROMOTED]
        assert (sp.read_state(agent_id) or {}).get("keep") is True

    @staticmethod
    async def _promote_on_loop(sp, agent_id):  # type: ignore[no-untyped-def]
        return sp.promote_retention(agent_id)

    def test_invalid_owner_id_does_not_abort_prune_sweep(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        malformed_id = "a-malformed-owner"
        valid_id = "z-valid-run"
        sp.create_agent_folder(malformed_id, task="malformed")
        sp.update_state(
            malformed_id,
            session_id="sid-malformed",
            provider="acp",
            keep=True,
            conversation_key="subagent:../invalid",
        )
        sp.create_agent_folder(valid_id, task="valid")
        sp.update_state(valid_id, session_id="sid-valid", provider="acp", keep=False)
        sp.remember_live_cleanup_identity(
            malformed_id,
            session_id="sid-malformed",
            provider="acp",
            keep=True,
            conversation_key="subagent:../invalid",
        )
        sp.remember_live_cleanup_identity(
            valid_id,
            session_id="sid-valid",
            provider="acp",
            keep=False,
        )

        for agent_id in (malformed_id, valid_id):
            sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
            path = sp._agent_dir(agent_id) / "tombstone.json"
            tombstone = json.loads(path.read_text())
            tombstone["died"] = 1
            path.write_text(json.dumps(tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 2

        assert not sp._agent_dir(malformed_id).exists()
        assert not sp._agent_dir(valid_id).exists()
        assert cleanup.call_count == 2

    def test_deep_tombstone_does_not_abort_prune_sweep(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        deep_id = "a-deep-tombstone"
        valid_id = "z-valid-after-deep"
        for agent_id in (deep_id, valid_id):
            sp.create_agent_folder(agent_id, task=agent_id)
            sp.update_state(
                agent_id,
                session_id=f"sid-{agent_id}",
                provider="acp",
                keep=False,
            )
            sp.remember_live_cleanup_identity(
                agent_id,
                session_id=f"sid-{agent_id}",
                provider="acp",
                keep=False,
            )
            sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")

        deep_path = sp._agent_dir(deep_id) / "tombstone.json"
        deep_path.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
        valid_path = sp._agent_dir(valid_id) / "tombstone.json"
        valid_tombstone = json.loads(valid_path.read_text())
        valid_tombstone["died"] = 1
        valid_path.write_text(json.dumps(valid_tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        assert sp._agent_dir(deep_id).exists()
        assert not sp._agent_dir(valid_id).exists()
        cleanup.assert_called_once_with(f"sid-{valid_id}", "acp", cwd="")

    def test_corrupt_protected_record_does_not_abort_prune_sweep(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        corrupt_id = "a-corrupt-protected"
        valid_id = "z-valid-after-protected"
        for agent_id in (corrupt_id, valid_id):
            sp.create_agent_folder(agent_id, task=agent_id)
            sp.update_state(
                agent_id,
                session_id=f"sid-{agent_id}",
                provider="acp",
                keep=False,
            )
            sp.remember_live_cleanup_identity(
                agent_id,
                session_id=f"sid-{agent_id}",
                provider="acp",
                keep=False,
            )
            sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
            tombstone_path = sp._agent_dir(agent_id) / "tombstone.json"
            tombstone = json.loads(tombstone_path.read_text())
            tombstone["died"] = 1
            tombstone_path.write_text(json.dumps(tombstone))

        corrupt_record = sp._cleanup_identities_path(corrupt_id)
        corrupt_record.write_text("{malformed")
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        assert sp._agent_dir(corrupt_id).exists()
        assert corrupt_record.read_text() == "{malformed"
        assert not sp._agent_dir(valid_id).exists()
        cleanup.assert_called_once_with(f"sid-{valid_id}", "acp", cwd="")

    def test_cancel_recovery_reclaims_every_session_generation(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        from unittest.mock import call

        import kiro_crew.subagent_persistence as sp

        agent_id = "cancel-recovery-generations"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(agent_id, session_id="sid-state", provider="acp", keep=False)
        real_atomic_write = sp._atomic_write
        failed_once = False

        def flaky_sidecar_write(path, data):  # type: ignore[no-untyped-def]
            nonlocal failed_once
            if path.name == sp._CLEANUP_IDENTITIES_FILE and not failed_once:
                failed_once = True
                raise OSError("transient sidecar failure")
            return real_atomic_write(path, data)

        with patch.object(sp, "_atomic_write", side_effect=flaky_sidecar_write):
            with pytest.raises(OSError, match="transient sidecar failure"):
                sp.remember_live_cleanup_identity(
                    agent_id, session_id="sid-1", provider="acp", cwd="/first"
                )
            sp.remember_live_cleanup_identity(
                agent_id, session_id="sid-2", provider="acp", cwd="/second"
            )
        sp.write_tombstone(agent_id, cause="cancelled", recovery_action="none")

        d = sp._agent_dir(agent_id)
        ts_path = d / "tombstone.json"
        tombstone = json.loads(ts_path.read_text())
        assert tombstone["session_id"] == "sid-2"
        assert [item["session_id"] for item in tombstone["cleanup_identities"]] == [
            "sid-1",
            "sid-2",
        ]

        # Shutdown may clear an exclusion tombstone to re-admit orphan recovery.
        # The protected generation record survives that and process-memory reset.
        # Replacement tombstone creation stays memory-only; executor-owned prune
        # merges protected generations without trusting the state-only SID.
        sp.clear_tombstone(agent_id)
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()
        assert not ts_path.exists()
        assert sp._cleanup_identities_path(agent_id).exists()
        sp.write_tombstone(agent_id, cause="gateway_restart", recovery_action="notified")
        tombstone = json.loads(ts_path.read_text())
        assert "cleanup_identities" not in tombstone
        assert tombstone["session_id"] == "sid-state"
        tombstone["died"] = 1
        ts_path.write_text(json.dumps(tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        assert cleanup.call_args_list == [
            call("sid-1", "acp", cwd="/first"),
            call("sid-2", "acp", cwd="/second"),
        ]
        assert not d.exists()
        assert not sp._cleanup_identities_path(agent_id).exists()

    def test_agent_writable_identity_files_cannot_delete_unrelated_session(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "untrusted-sidecar"
        own_sid = "sid-owned-by-run"
        victim_sid = "sid-owned-by-another-run"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(
            agent_id,
            session_id=own_sid,
            provider="acp",
            keep=False,
        )
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id=own_sid,
            provider="acp",
            keep=False,
        )
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        agent_dir = sp._agent_dir(agent_id)
        protected_path = sp._cleanup_identities_path(agent_id)
        assert not protected_path.is_relative_to(agent_dir)
        assert "trust" in protected_path.parts
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = 1
        tombstone["session_id"] = victim_sid
        tombstone["cleanup_identities"] = [{"session_id": victim_sid, "provider": "acp"}]
        tombstone_path.write_text(json.dumps(tombstone))

        # These are the identity files a subagent can write. Durable cleanup
        # authority lives under the protected trust root, so neither forged
        # spelling may add the victim SID to the provider-deletion set.
        (agent_dir / sp._CLEANUP_IDENTITIES_FILE).write_text(
            json.dumps({"identities": [{"session_id": victim_sid, "provider": "acp"}]})
        )
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        own_file = sessions_dir / f"{own_sid}.json"
        victim_file = sessions_dir / f"{victim_sid}.json"
        own_file.write_text("own")
        victim_file.write_text("victim")

        with patch.object(sp, "kiro_sessions_dir", return_value=sessions_dir):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        assert not own_file.exists()
        assert victim_file.read_text() == "victim"
        assert not agent_dir.exists()

    def test_failed_provider_cleanup_preserves_folder_and_identity_for_retry(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        import time

        import kiro_crew.subagent_persistence as sp

        agent_id = "unsupported-cleanup-retry"
        sp.create_agent_folder(agent_id, task="t")
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-claude-retry",
            provider="claude_code",
            cwd="/project",
            keep=False,
        )
        sp.update_state(agent_id, keep=False)
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        agent_dir = sp._agent_dir(agent_id)
        protected_path = sp._cleanup_identities_path(agent_id)
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = time.time() - 1
        tombstone_path.write_text(json.dumps(tombstone))
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()

        # Claude Code has no cleanup route yet: keep both retry surfaces.
        assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert agent_dir.exists()
        assert protected_path.exists()

        # Once a provider cleanup implementation succeeds, the same record is
        # enough to complete prune and reap both surfaces.
        with patch.object(sp, "_cleanup_session_files_sync", return_value=True):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert not agent_dir.exists()
        assert not protected_path.exists()

    def test_legacy_sid_without_trusted_generation_preserves_lookup_for_retry(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        import time

        import kiro_crew.subagent_persistence as sp

        agent_id = "legacy-untrusted-sid"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(
            agent_id,
            session_id="sid-legacy",
            provider="acp",
            keep=False,
        )
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        agent_dir = sp._agent_dir(agent_id)
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = time.time() - 1
        tombstone_path.write_text(json.dumps(tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert agent_dir.exists()
        cleanup.assert_not_called()

        # A migration or later gateway-owned publication makes cleanup safe.
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-legacy",
            provider="acp",
            keep=False,
        )
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()
        with patch.object(sp, "_cleanup_session_files_sync", return_value=True):
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert not agent_dir.exists()

    @pytest.mark.parametrize("trusted_generation", [False, True])
    def test_unreclaimable_lookup_has_ninety_day_hard_ceiling(
        self, tmp_path, trusted_generation: bool
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        import time

        import kiro_crew.subagent_persistence as sp

        agent_id = f"hard-ceiling-{trusted_generation}"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(
            agent_id,
            session_id="sid-hard-ceiling",
            provider="claude_code" if trusted_generation else "acp",
            keep=False,
        )
        if trusted_generation:
            sp.remember_live_cleanup_identity(
                agent_id,
                session_id="sid-hard-ceiling",
                provider="claude_code",
                cwd="/project",
                keep=False,
            )
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        agent_dir = sp._agent_dir(agent_id)
        protected_path = sp._cleanup_identities_path(agent_id)
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = time.time() - sp._UNRECLAIMABLE_LOOKUP_MAX_AGE_SECS - 1
        tombstone_path.write_text(json.dumps(tombstone))
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()

        assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert not agent_dir.exists()
        assert not protected_path.exists()

    def test_unreadable_state_does_not_trust_stale_false_generation(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        import time

        import kiro_crew.subagent_persistence as sp

        agent_id = "stale-false-after-promotion"
        sp.create_agent_folder(agent_id, task="t")
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-promoted",
            provider="acp",
            keep=False,
        )
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        # Promotion lands in state, but the acquisition-time generation remains
        # false. Corrupt state must not turn that stale false into delete authority.
        sp.update_state(agent_id, keep=True)
        agent_dir = sp._agent_dir(agent_id)
        (agent_dir / "state.json").write_text("{corrupt")
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = time.time() - 1
        tombstone.pop("cleanup_identities", None)
        tombstone_path.write_text(json.dumps(tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert agent_dir.exists()
        cleanup.assert_not_called()

        # Unknown retention remains bounded rather than immortal.
        tombstone["died"] = 1
        tombstone_path.write_text(json.dumps(tombstone))
        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        cleanup.assert_called_once_with("sid-promoted", "acp", cwd="")

    def test_cleanup_store_restriction_failure_aborts_before_access(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "cleanup-store-lockdown"
        sp.create_agent_folder(agent_id, task="t")
        protected_path = sp._cleanup_identities_path(agent_id)
        protected_path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"identities": [{"session_id": "sid-original"}]})
        protected_path.write_text(original)

        with (
            patch.object(sp.platform_compat, "make_owner_only_dir"),
            patch.object(
                sp.platform_compat,
                "restrict_dir_to_owner",
                side_effect=OSError("DACL refused"),
            ),
        ):
            with pytest.raises(OSError, match="DACL refused"):
                sp._read_cleanup_identities_file(agent_id)
            with pytest.raises(OSError, match="DACL refused"):
                sp.remember_live_cleanup_identity(agent_id, session_id="sid-forged", provider="acp")

        assert protected_path.read_text() == original

    def test_cleanup_store_parse_failure_never_rewrites_history(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import kiro_crew.subagent_persistence as sp

        agent_id = "cleanup-store-corrupt"
        sp.create_agent_folder(agent_id, task="t")
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-original",
            provider="acp",
            keep=False,
        )
        protected_path = sp._cleanup_identities_path(agent_id)
        malformed = "{malformed"
        protected_path.write_text(malformed)

        with pytest.raises(ValueError):
            sp.remember_live_cleanup_identity(
                agent_id,
                session_id="sid-new",
                provider="acp",
                keep=False,
            )

        assert protected_path.read_text() == malformed

    def test_tombstone_sid_cannot_override_latest_protected_retention(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "forged-tombstone-retention"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(agent_id, provider="acp")
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-owned",
            provider="acp",
            keep=True,
        )
        sp.write_tombstone(agent_id, cause="delivered", recovery_action="none")
        agent_dir = sp._agent_dir(agent_id)
        tombstone_path = agent_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = 1
        tombstone["session_id"] = "sid-victim"
        tombstone.pop("cleanup_identities", None)
        tombstone_path.write_text(json.dumps(tombstone))
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert agent_dir.exists()
        cleanup.assert_not_called()

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
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
        assert d.exists()
        cleanup.assert_not_called()

    def test_continuation_prune_honors_original_release(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        original_id = "original-run"
        continuation_id = "continuation-run"
        sp.create_agent_folder(original_id, task="original")
        sp.update_state(original_id, session_id="sid-c", provider="acp", keep=True)
        sp.create_agent_folder(continuation_id, task="continuation")
        sp.update_state(
            continuation_id,
            session_id="sid-c",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-c",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.update_state(original_id, keep=False)
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        d = sp._agent_dir(continuation_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 0
        ts_path.write_text(json.dumps(ts))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)
        assert pruned == 1
        assert not d.exists()
        cleanup.assert_called_once_with("sid-c", "acp", cwd="")

    def test_continuation_partial_state_uses_sidecar_retention_fallback(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        original_id = "partial-state-owner"
        continuation_id = "partial-state-continuation"
        conversation_key = f"subagent:{original_id}"
        sp.create_agent_folder(original_id, task="original")
        sp.update_state(original_id, session_id="sid-partial", provider="acp", keep=True)
        sp.create_agent_folder(continuation_id, task="continuation")
        # Session acquisition publishes cleanup identity and retention together
        # before the later best-effort combined state update. Simulate that update
        # failing by leaving the otherwise readable state without either field.
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-partial",
            provider="acp",
            keep=True,
            conversation_key=conversation_key,
        )
        sp.update_state(continuation_id, provider="acp")
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        sp._LIVE_CLEANUP_IDENTITIES.clear()
        d = sp._agent_dir(continuation_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 0
        # Restart can rewrite a cleared tombstone before state identity lands;
        # durable generation metadata must still carry retention and owner.
        ts.pop("session_id", None)
        ts.pop("cleanup_identities", None)
        ts_path.write_text(json.dumps(ts))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
            assert d.exists()
            cleanup.assert_not_called()

            # Current readable owner state remains authoritative over stale
            # sidecar keep=True once release records keep=False.
            sp.update_state(original_id, keep=False)
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert not d.exists()
        cleanup.assert_called_once_with("sid-partial", "acp", cwd="")

    @pytest.mark.parametrize(
        ("writable_key", "writable_sid"),
        [
            ("", "sid-continuation"),
            ("subagent:forged-retention-owner", "sid-continuation"),
            ("", "sid-forged"),
        ],
    )
    def test_protected_continuation_owner_outranks_writable_state(
        self, tmp_path, writable_key: str, writable_sid: str
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        owner_id = "protected-retention-owner"
        continuation_id = f"protected-owner-{bool(writable_key)}"
        conversation_key = f"subagent:{owner_id}"
        sp.create_agent_folder(owner_id, task="original")
        sp.update_state(owner_id, session_id="sid-owner", provider="acp", keep=True)
        sp.create_agent_folder(continuation_id, task="continuation")
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-continuation",
            provider="acp",
            keep=True,
            conversation_key=conversation_key,
        )
        # Agent-writable state must not erase or redirect the protected owner.
        sp.update_state(
            continuation_id,
            session_id=writable_sid,
            provider="acp",
            keep=False,
            conversation_key=writable_key,
        )
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        continuation_dir = sp._agent_dir(continuation_id)
        tombstone_path = continuation_dir / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = 0
        tombstone_path.write_text(json.dumps(tombstone))
        with sp._CLEANUP_IDENTITY_LOCK:
            sp._LIVE_CLEANUP_IDENTITIES.clear()

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
            assert continuation_dir.exists()
            cleanup.assert_not_called()

            sp.update_state(owner_id, keep=False)
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        assert not continuation_dir.exists()
        cleanup.assert_called_once_with("sid-continuation", "acp", cwd="")

    def test_unreadable_state_and_empty_tombstone_use_sidecar_retention(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        owner_id = "unreadable-sidecar-owner"
        child_id = "unreadable-sidecar-child"
        conversation_key = f"subagent:{owner_id}"
        sp.create_agent_folder(owner_id, task="original")
        sp.update_state(owner_id, session_id="sid-owner", provider="acp", keep=True)
        sp.create_agent_folder(child_id, task="continuation")
        sp.remember_live_cleanup_identity(
            child_id,
            session_id="sid-sidecar-only",
            provider="acp",
            keep=True,
            conversation_key=conversation_key,
        )
        sp.write_tombstone(child_id, cause="delivered", recovery_action="none")
        (sp._agent_dir(child_id) / "state.json").write_text("{corrupt")
        sp._LIVE_CLEANUP_IDENTITIES.clear()
        tombstone_path = sp._agent_dir(child_id) / "tombstone.json"
        tombstone = json.loads(tombstone_path.read_text())
        tombstone["died"] = 0
        tombstone.pop("session_id", None)
        tombstone.pop("cleanup_identities", None)
        tombstone_path.write_text(json.dumps(tombstone))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
            cleanup.assert_not_called()

            sp.update_state(owner_id, keep=False)
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1

        cleanup.assert_called_once_with("sid-sidecar-only", "acp", cwd="")
        assert not sp._agent_dir(child_id).exists()

    def test_continuation_prune_owner_missing_keep_is_nonretained(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        original_id = "owner-missing-keep"
        continuation_id = "continuation-missing-keep"
        sp.create_agent_folder(original_id, task="original")
        sp.update_state(original_id, session_id="sid-m", provider="acp")
        sp.create_agent_folder(continuation_id, task="continuation")
        sp.update_state(
            continuation_id,
            session_id="sid-m",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-m",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.update_state(original_id, provider="acp")
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        d = sp._agent_dir(continuation_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = 0
        ts_path.write_text(json.dumps(ts))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            pruned = sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0)
        assert pruned == 1
        assert not d.exists()
        cleanup.assert_called_once_with("sid-m", "acp", cwd="")

    @pytest.mark.parametrize("tombstone_has_sid", [True, False])
    def test_continuation_prune_unreadable_owner_gets_bounded_grace(
        self, tmp_path, tombstone_has_sid: bool
    ) -> None:  # type: ignore[no-untyped-def]
        import json
        import time

        import kiro_crew.subagent_persistence as sp

        original_id = "owner-corrupt"
        continuation_id = "continuation-corrupt-owner"
        sp.create_agent_folder(original_id, task="original")
        sp.update_state(original_id, session_id="sid-u", provider="acp", keep=True)
        (sp._agent_dir(original_id) / "state.json").write_text("{corrupt")
        sp.create_agent_folder(continuation_id, task="continuation")
        sp.update_state(
            continuation_id,
            session_id="sid-u",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.remember_live_cleanup_identity(
            continuation_id,
            session_id="sid-u",
            provider="acp",
            keep=True,
            conversation_key=f"subagent:{original_id}",
        )
        sp.write_tombstone(continuation_id, cause="delivered", recovery_action="none")
        d = sp._agent_dir(continuation_id)
        ts_path = d / "tombstone.json"
        ts = json.loads(ts_path.read_text())
        ts["died"] = time.time() - (12 * 3600)
        if not tombstone_has_sid:
            ts.pop("session_id", None)
        ts_path.write_text(json.dumps(ts))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
            assert d.exists()
            cleanup.assert_not_called()
            ts["died"] = time.time() - (2 * 86400)
            ts_path.write_text(json.dumps(ts))
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 1
        assert not d.exists()
        cleanup.assert_called_once_with("sid-u", "acp", cwd="")

    def test_tombstone_prune_cleans_files_for_plain_runs(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        import kiro_crew.subagent_persistence as sp

        agent_id = "plainrun"
        sp.create_agent_folder(agent_id, task="t")
        sp.update_state(agent_id, session_id="sid-p", provider="acp", keep=False)
        sp.remember_live_cleanup_identity(
            agent_id,
            session_id="sid-p",
            provider="acp",
            keep=False,
        )
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


class TestContinuationMemoryMode:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("original", ["persistent", "incognito", "temporary"])
    @pytest.mark.parametrize("requested", ["persistent", "incognito", "temporary"])
    async def test_fresh_manager_restores_and_tightens_original_mode(self, original, requested):
        from kiro_crew.messaging.privacy_mode import strictest
        from kiro_crew.subagent_persistence import create_agent_folder, read_run_memory_mode

        conv_id = f"mode-{original}-{requested}"
        create_agent_folder(conv_id, task="original", memory_mode=original)
        manager = _manager(_mock_sessions(resumed=True))
        manager._memory_mode_for_session = lambda key: requested
        # The ASYNC entry, because this test body is a coroutine: the sync one
        # takes the accept and the claim as BEGIN IMMEDIATE on this loop, and
        # each waits on the lock the store's writer thread holds across a query
        # (`_the_continuation_path_takes_no_store_call_on_the_loop`).
        info = await manager.continue_conversation_async(conv_id, "follow up")
        assert info is not None and not info.error
        assert not info._memory_mode_ready
        await asyncio.wait_for(manager._tasks[info.id], timeout=5)
        expected = strictest((original, requested)) or "persistent"
        assert not info.error, info.error
        # A cancelled run publishes no mode, which the mode assertion below
        # would report as a MISMATCH -- the reading that hid a real loop stall
        # on the Windows shard. Name the cancellation and its stop reason first,
        # and keep the two facts (WHICH mode, and whether it was published at
        # all) as separate assertions.
        assert not info._cancel_retry_used, f"run cancelled, not completed: {_stop_reason(info)}"
        assert info._memory_mode_ready, f"mode publication never ran: {_stop_reason(info)}"
        assert info.memory_mode == expected
        assert read_run_memory_mode(conv_id) == expected
        assert read_run_memory_mode(info.id) == expected
        assert manager._ctx_builder.build_message.call_args.kwargs["blocks_reads"] == (
            expected == "temporary"
        )

        restarted = _manager(_mock_sessions(resumed=True))
        resumed = await restarted.continue_conversation_async(conv_id, "another turn")
        assert resumed is not None and not resumed.error
        await asyncio.wait_for(restarted._tasks[resumed.id], timeout=5)
        assert not resumed.error, resumed.error
        assert not resumed._cancel_retry_used, f"run cancelled: {_stop_reason(resumed)}"
        assert resumed.memory_mode == expected

    @pytest.mark.asyncio
    async def test_missing_original_policy_never_allocates_provider(self):
        from kiro_crew.subagent_persistence import _run_memory_identity_path, create_agent_folder

        create_agent_folder("missing-resume-policy", memory_mode="incognito")
        record = _run_memory_identity_path("missing-resume-policy")
        import json

        payload = json.loads(record.read_text(encoding="utf-8"))
        del payload["memory_mode"]
        record.write_text(json.dumps(payload), encoding="utf-8")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        # The ASYNC entry for the same reason as the sibling test above, and it
        # matters here for the same 5s deadline: the sync one's two BEGIN
        # IMMEDIATEs run on THIS loop, so the deadline is spent waiting on the
        # store lock rather than on the refusal being reached.
        info = await manager.continue_conversation_async("missing-resume-policy", "must not run")
        assert info is not None
        # ``.get``, not ``[...]``: this refusal is raised on the run's first
        # steps, and the async entry's own awaits give it enough of the loop to
        # finish -- and be popped from ``_tasks`` by its finally -- before the
        # dispatch returns. A missing entry therefore means the terminal is
        # already recorded on ``info``, which is what the assertions below read.
        task = manager._tasks.get(info.id)
        if task is not None:
            await asyncio.wait_for(task, timeout=5)
        # A cancelled run carries no error at all, so the membership check below
        # would read "the refusal was worded differently" for a run that never
        # reached the allocation boundary.
        assert not info._cancel_retry_used, f"run cancelled, not refused: {_stop_reason(info)}"
        assert "memory_unavailable" in info.error, _stop_reason(info)
        assert str(record) not in info.error
        assert "caused by" not in info.error
        assert not info._memory_mode_ready
        sessions.get_or_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_resume_drains_mode_publication_before_returning(monkeypatch):
    from kiro_crew import subagent_persistence as persistence

    persistence.create_agent_folder("cancel-mode-original", memory_mode="incognito")
    persistence.create_agent_folder("cancel-mode-current", memory_mode="temporary")
    entered, release = threading.Event(), threading.Event()
    real = persistence.tighten_run_memory_mode

    def held(agent_id, mode):
        entered.set()
        assert release.wait(5), "test did not release mode writer"
        return real(agent_id, mode)

    monkeypatch.setattr(persistence, "tighten_run_memory_mode", held)
    sessions = _mock_sessions(resumed=True)
    manager = _manager(sessions)
    info = SubagentInfo(
        id="cancel-mode-current",
        task="test",
        conversation_key="subagent:cancel-mode-original",
        memory_mode="temporary",
    )
    info._memory_mode_ready = False
    task = asyncio.create_task(manager._run_inner(info, info.conversation_key))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation escaped while the protected writer was active"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert persistence.read_run_memory_mode("cancel-mode-original") == "temporary"
        assert persistence.read_run_memory_mode("cancel-mode-current") == "temporary"
        sessions.get_or_create.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


@pytest.mark.asyncio
async def test_the_continuation_path_takes_no_store_call_on_the_loop(monkeypatch) -> None:
    """A continuation dispatched from the gateway loop reaches the task store
    only on its writer thread -- dispatch, the run's mode publication, and the
    terminal settle.

    ``store.loop_thread_calls`` is what the claim rests on, not the guard alone:
    ``OnLoopDBGuard.check`` raises, and most store call sites sit inside an
    ``except Exception`` that swallows the raise, so a violation shows up as a
    number and not as a failure. The guard is armed as well, for the sites that
    do propagate. The counter also covers the SHAPE this pins against: both
    writes the sync entry would take here (``taskq_accept``, ``taskq_claim``)
    wait on ``TaskStore._lock``, and a 1s hold by the writer thread freezes a
    coroutine caller's loop for the whole hold -- measured 0 of ~95 due 10ms
    heartbeat ticks served through the sync entry against 91 through this one.
    """
    from kiro_crew.subagent_manager import admission as admission_mod
    from kiro_crew.subagent_persistence import create_agent_folder
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    create_agent_folder("no-loop-db", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    await manager.wait_taskq_ready()
    manager._spawn_stagger_secs = 0.0
    store = manager._admission.taskq_store()
    assert store is not None, "this pin needs the real durable store"
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    info = await manager.continue_conversation_async("no-loop-db", "follow up")
    assert info is not None and not info.error, getattr(info, "error", None)
    task = manager._tasks.get(info.id)
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=20)
    for _ in range(40):
        await asyncio.sleep(0.01)  # let the posted writes land, still under the guard
    assert store.loop_thread_calls == before  # the reads below are the test's own
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    # The run really ran: a refusal or a cancellation would take no store call
    # either, and would pass an assertion that only counted.
    #
    # ``queued`` is checked FIRST and on its own, because it is the one
    # never-started outcome the two markers below miss: a handle the claim
    # refused carries ``_memory_mode_ready`` at its dataclass DEFAULT of True
    # (only a registered run has it set from the conversation key), so a
    # continuation that never left the queue passes the pair.
    assert not info.queued and info.done, _stop_reason(info)
    assert info._memory_mode_ready and not info._cancel_retry_used, _stop_reason(info)
    assert store.get(info.id) is not None


def test_a_stale_resume_entry_does_not_hold_a_conversation() -> None:
    """A ``_resume_id`` window entry is a RESIDENT run asking for the lane slot
    it yielded, not an unstarted spawn, so it never answers
    ``_conversation_busy`` -- the same separation the pump's grant loop, the
    refill census, the eviction and the child reserve make.

    An entry whose run is still live is answered by the ``_agents`` scan first.
    The case that reaches this branch is a run that ENDED with its request still
    queued: the queued-stop path leaves the entry alone by design (dropping it
    published a "never started" terminal over a live run) and only the bounded
    waiter's give-up arm withdraws one, while the pump returns above its resume
    loop whenever no slot is free. Counted, that entry refuses every
    continuation and every release of the conversation with a
    ``conversation_busy`` naming a run that is already done.
    """
    from kiro_crew.subagent_persistence import create_agent_folder

    create_agent_folder("resume-held", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    manager._agents["resume-held"] = SubagentInfo(
        id="resume-held", task="original", done=True, user_stopped=True
    )
    manager._queue.append(
        {
            "_resume_id": "resume-held",
            "_preassigned_id": "resume-held",
            "parent_session_key": "web-1",
            "batch_id": "",
            "reason": "children finished",
        }
    )
    assert manager._conversation_busy("subagent:resume-held") is None
    ok, detail = manager.release_conversation("resume-held")
    assert "conversation_busy" not in detail, detail
    # An UNSTARTED entry for the same conversation still holds it.
    manager._queue.append({"_preassigned_id": "resume-held"})
    held = manager._conversation_busy("subagent:resume-held")
    assert held is not None and held.queued and held.id == "resume-held"


@pytest.mark.asyncio
async def test_the_followup_watcher_dispatches_no_store_call_on_the_loop(monkeypatch) -> None:
    """The follow-up watcher is the manager's OWN continuation caller
    (``spawn_steer mode="follow_up"``), and it dispatches from a task on the
    gateway loop -- so the entry it picks is what decides whether a queued
    correction costs the loop two ``BEGIN IMMEDIATE`` waits.

    Pinned separately from the dispatch pin because a counter over
    ``continue_conversation_async`` says nothing about which entry
    ``_deliver_followups`` calls: swapping that one line to the sync entry
    leaves the other pin green.
    """
    from kiro_crew.subagent_manager import admission as admission_mod
    from kiro_crew.subagent_persistence import create_agent_folder
    from kiro_crew.taskq import store as store_mod

    monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
    create_agent_folder("followup-conv", task="original", memory_mode="persistent")
    manager = _manager(_mock_sessions(resumed=True))
    await manager.wait_taskq_ready()
    manager._spawn_stagger_secs = 0.0
    store = manager._admission.taskq_store()
    assert store is not None, "this pin needs the real durable store"
    # A finished run whose task is already popped: what the watcher waits for
    # before it dispatches the queue as ONE continuation.
    done = SubagentInfo(
        id="followup-conv",
        task="original",
        conversation_key="subagent:followup-conv",
        done=True,
    )
    done.pending_followups = ["also fix the test"]
    manager._agents["followup-conv"] = done
    before = store.loop_thread_calls
    monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
    await asyncio.wait_for(manager._deliver_followups(done), timeout=20)
    child = next((a for a in manager._agents.values() if a.id != "followup-conv"), None)
    assert child is not None, "the watcher dispatched no continuation"
    task = manager._tasks.get(child.id)
    if task is not None:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=20)
    for _ in range(40):
        await asyncio.sleep(0.01)  # let the posted writes land, still under the guard
    assert store.loop_thread_calls == before  # the reads below are the test's own
    monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
    # The dispatch really happened (a settled queue with no child would pass a
    # count-only assertion), and it STARTED: a claim the store refused comes
    # back queued and takes no on-loop call either.
    assert done.pending_followups == []
    assert not child.queued, _stop_reason(child)
    assert store.get(child.id) is not None
