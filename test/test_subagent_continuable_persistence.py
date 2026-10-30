"""What a continuable run keeps on disk: transcripts, persistence guards, memory mode.

A shared session handle's teardown honours ``keep_transcript``; tombstone prune
keeps the session files of keep=True runs; and a continuation restores, and only
tightens, the memory mode of the run it continues, draining that mode's publication
even when it is cancelled.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager, _mock_sessions, _stop_reason

from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_persistence import create_agent_folder, write_run_agent
from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers


# ── persistence guards ──


class TestKeepTranscript:
    """AcpSessionHandle.destroy() honors keep_transcript (shared arm)."""

    @pytest.mark.asyncio
    async def test_destroy_deletes_transcript_by_default(self, tmp_path) -> None:
        h, sessions, files = self._handle_with_transcript(tmp_path, sid="sid-h")
        h._runtime.terminate_session = AsyncMock()
        with patch("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions):
            await h.destroy()
        assert [f for f in files if f.exists()] == []
        h._runtime.terminate_session.assert_awaited_once_with("sid-h")

    @pytest.mark.asyncio
    async def test_destroy_keeps_transcript_when_flagged(self, tmp_path) -> None:
        h, sessions, files = self._handle_with_transcript(tmp_path, sid="sid-h")
        h._runtime.terminate_session = AsyncMock()
        h.keep_transcript = True
        with patch("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: sessions):
            await h.destroy()
        assert all(f.exists() for f in files)
        # terminate_session still runs — RSS reclaim is unconditional.
        h._runtime.terminate_session.assert_awaited_once_with("sid-h")

    @pytest.mark.asyncio
    async def test_shared_arm_teardown_sets_keep_transcript(self) -> None:
        """SubagentManager teardown flags the shared provider before shutdown."""

        class _SharedProvider:
            keep = False
            keep_at_shutdown: bool | None = None

            def set_keep_transcript(self, keep: bool) -> None:
                self.keep = keep

            async def shutdown(self) -> None:
                self.keep_at_shutdown = self.keep

        manager = _manager()
        info = SubagentInfo(id="sh1", task="t")
        info._session_sharing = True
        shared = _SharedProvider()
        info._shared_provider = shared
        await manager._teardown_run_session(info, "subagent:sh1")
        assert shared.keep_at_shutdown is True

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
        h.memory_mode = "persistent"
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
        # flake-ok: back-dated a second on the live clock, past the zero TTL the prune gets
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
        # flake-ok: back-dated a second on the live clock, past the zero TTL the prune gets
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
        # flake-ok: back-dated past the max age; real time passing only ages it further
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
        # flake-ok: back-dated a second on the live clock, past the zero TTL the prune gets
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
        # flake-ok: back-dated 12 h, inside the 24 h unreadable-owner grace the prune must keep
        ts["died"] = time.time() - (12 * 3600)
        if not tombstone_has_sid:
            ts.pop("session_id", None)
        ts_path.write_text(json.dumps(ts))

        with patch.object(sp, "_cleanup_session_files_sync") as cleanup:
            assert sp.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
            assert d.exists()
            cleanup.assert_not_called()
            # flake-ok: back-dated 2 days, past the 24 h unreadable-owner grace, so the prune reaps
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
        from kiro_crew.subagent_persistence import (
            read_run_agent_selection,
            read_run_app,
            read_run_memory_mode,
        )

        conv_id = f"mode-{original}-{requested}"
        await asyncio.to_thread(create_agent_folder, conv_id, task="original", memory_mode=original)
        await asyncio.to_thread(write_run_agent, conv_id, "")
        manager = _manager(_mock_sessions(resumed=True))
        manager._memory_mode_for_session = lambda key: requested
        # The ASYNC entry, because this test body is a coroutine: the sync one
        # takes the accept and the claim as BEGIN IMMEDIATE on this loop, and
        # each waits on the lock the store's writer thread holds across a query
        # (`_the_continuation_path_takes_no_store_call_on_the_loop`).
        info = await manager.continue_conversation_async(conv_id, "follow up")
        assert info is not None and not info.error
        assert not info._memory_mode_ready
        await await_until(manager._tasks[info.id], what="the run finishing")
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
        if expected == "persistent":
            assert read_run_memory_mode(info.id) == expected
            assert read_run_app(info.id) == ""
            assert read_run_agent_selection(info.id) == ("template", "")
        else:
            # Restricted continuation bodies and their transient child state
            # are released after terminal writers settle. The live result still
            # carries the captured mode/app, while the original conversation
            # record remains the restart authority.
            with pytest.raises(ValueError, match="run record is unavailable"):
                read_run_memory_mode(info.id)
            assert info.memory_mode == expected
        assert read_run_memory_mode(conv_id) == expected
        assert read_run_app(conv_id) == ""
        assert manager._ctx_builder.build_message.call_args.kwargs["blocks_reads"] == (
            expected == "temporary"
        )

        restarted = _manager(_mock_sessions(resumed=True))
        resumed = await restarted.continue_conversation_async(conv_id, "another turn")
        assert resumed is not None and not resumed.error
        await await_until(restarted._tasks[resumed.id], what="the resumed run finishing")
        assert not resumed.error, resumed.error
        assert not resumed._cancel_retry_used, f"run cancelled: {_stop_reason(resumed)}"
        assert resumed.memory_mode == expected
        if expected == "persistent":
            assert read_run_agent_selection(resumed.id) == ("template", "")
            assert read_run_app(resumed.id) == ""
        else:
            with pytest.raises(ValueError, match="run record is unavailable"):
                read_run_agent_selection(resumed.id)
            assert resumed.agent == ""
            assert resumed.app == ""

    @pytest.mark.asyncio
    async def test_missing_original_policy_never_allocates_provider(self):
        from kiro_crew.subagent_persistence import _agent_dir, create_agent_folder

        await asyncio.to_thread(
            create_agent_folder, "missing-resume-policy", memory_mode="persistent"
        )
        await asyncio.to_thread(write_run_agent, "missing-resume-policy", "")
        record = _agent_dir("missing-resume-policy") / "state.json"
        import json

        payload = json.loads(record.read_text(encoding="utf-8"))
        del payload["execution_context"]["memory_mode"]
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
            await await_until(task, what="the run finishing")
        # A cancelled run carries no error at all, so the membership check below
        # would read "the refusal was worded differently" for a run that never
        # reached the allocation boundary.
        assert not info._cancel_retry_used, f"run cancelled, not refused: {_stop_reason(info)}"
        assert "memory_unavailable" in info.error, _stop_reason(info)
        assert str(record) not in info.error
        assert "caused by" not in info.error
        assert info.execution_context is None
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
        await async_wait_until(entered.is_set)
        task.cancel()
        # flake-ok: zero-delay yield lets the first cancel land while the writer holds
        await asyncio.sleep(0)
        task.cancel()
        # flake-ok: zero-delay yield lets the second cancel land; moved verbatim
        await asyncio.sleep(0)
        assert not task.done(), "cancellation escaped while the protected writer was active"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled resume ending")
        assert persistence.read_run_memory_mode("cancel-mode-original") == "temporary"
        assert persistence.read_run_memory_mode("cancel-mode-current") == "temporary"
        sessions.get_or_create.assert_not_awaited()
    finally:
        release.set()
        await await_until(asyncio.gather(task, return_exceptions=True), what="the resume ending")
