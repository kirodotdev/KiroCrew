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
from kiro_crew.subagent_persistence import create_agent_folder, write_run_agent

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
    provider.context_window_tokens = lambda: 100000
    provider.context_used_tokens = lambda: 0
    provider.session_id = "sid-123"
    provider.cwd = ""

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, resumed))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(
        side_effect=lambda key: ("template", sessions.get_agent(key))
    )
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
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "origrun1", memory_mode="persistent")
        await asyncio.to_thread(sp.write_run_agent, "origrun1", "")
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
        create_agent_folder("retryrun")
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
        create_agent_folder("skiprun")
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

        create_agent_folder("kept-run")
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
        await asyncio.to_thread(create_agent_folder, "origrun2")
        await asyncio.to_thread(write_run_agent, "origrun2", "")
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
        assert not info.error, info.error
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
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "origrun1", memory_mode="persistent")
        await asyncio.to_thread(sp.write_run_agent, "origrun1", "")
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
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "origrun9", memory_mode="persistent")
        await asyncio.to_thread(sp.write_run_agent, "origrun9", "")
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

        ts = json.loads((sp._agent_dir(info.id) / "tombstone.json").read_text())
        assert ts["session_id"] == "sid-resume-fresh"
        assert ts["provider"] == "acp"
        # The prompt must never have been sent on the fresh session.
        provider.stream.assert_not_called()


# ── steer_run ──


class TestContinuationAgentInheritance:

    @pytest.mark.asyncio
    async def test_unknown_initial_template_refuses_allocation(self) -> None:
        sessions = _mock_sessions()
        sessions.get_agent.return_value = None
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("initial task", parent_session_key="dashboard:owner")
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert "effective agent template is invalid" in info.error
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live", "restart"])
    @pytest.mark.parametrize("original_agent", ["worker", ""], ids=["named", "default"])
    async def test_chained_override_preserves_original_template(
        self, restart: bool, original_agent: str
    ) -> None:
        import kiro_crew.subagent_persistence as sp

        sessions = _mock_sessions()
        sessions.get_agent.return_value = original_agent
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
        ):
            original = manager.spawn(
                "original task", parent_session_key="dashboard:owner", keep=True
            )
            assert original is not None and not original.error
            await asyncio.wait_for(manager._tasks[original.id], timeout=10)
            assert not original.error
            target = original
            for override in ("other-worker", "", ""):
                if restart:
                    sessions = _mock_sessions(resumed=True)
                    manager = _manager(sessions)
                    manager._spawn_stagger_secs = 0
                else:
                    provider = sessions.get_or_create.return_value[0]
                    sessions.get_or_create.return_value = (provider, True, True)
                sessions.get_agent.return_value = "conductor"
                followup = manager.continue_conversation(
                    target.id, "next turn", parent_session_key="dashboard:owner", agent=override
                )
                assert followup is not None and not followup.error
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                assert not followup.error
                call = sessions.get_or_create.call_args
                assert call.args[0] == f"subagent:{target.id}"
                assert call.kwargs["agent"] == (override or original_agent or None)
                state = await asyncio.to_thread(sp.read_state, followup.id)
                assert state is not None and state["agent"] == (override or original_agent)
                # Writable diagnostics cannot replace either protected identity.
                await asyncio.to_thread(sp.update_state, followup.id, agent="forged-worker")
                target = followup
        assert (await asyncio.to_thread(sp.read_run_agent_selection, original.id))[
            1
        ] == original_agent
        assert (await asyncio.to_thread(sp.read_run_agent_selection, target.id))[
            1
        ] == original_agent

    @pytest.mark.asyncio
    @pytest.mark.parametrize("authority", ["missing", "corrupt", "unreadable"])
    async def test_override_cannot_establish_missing_lineage(self, authority: str) -> None:
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "original", agent="forged-worker")
        path = sp._run_agent_identity_path("original")
        if authority == "corrupt":
            await asyncio.to_thread(path.write_text, "{", encoding="utf-8")
        elif authority == "unreadable":
            await asyncio.to_thread(path.mkdir)
        sessions = _mock_sessions(resumed=True)
        sessions.get_agent.return_value = "conductor"
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
        ):
            target = "original"
            for override in ("other-worker", "other-worker", ""):
                followup = manager.continue_conversation(
                    target, "next turn", parent_session_key="dashboard:owner", agent=override
                )
                assert followup is not None and not followup.error
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                if override:
                    assert not followup.error
                    assert sessions.get_or_create.call_args.kwargs["agent"] == override
                else:
                    assert "protected agent template unavailable" in followup.error
                    assert sessions.get_or_create.await_count == 2
                target = followup.id
                # Restart removes live registry hints between every turn.
                manager = _manager(sessions)
                manager._spawn_stagger_secs = 0
        with pytest.raises(ValueError, match="protected agent template unavailable"):
            (await asyncio.to_thread(sp.read_run_agent_selection, target))[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", ["removed-template", "spawn-policy"])
    async def test_chained_override_does_not_bypass_original_template_refusal(
        self, refusal: str
    ) -> None:
        from types import SimpleNamespace

        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(
            sp.create_agent_folder, "original", agent="worker", app="example-app"
        )
        await asyncio.to_thread(sp.write_run_agent, "original", "worker")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0

        def validate(name: str, cwd: str) -> tuple[str, str, str]:
            if name == "worker" and refusal == "removed-template":
                return "", "agent 'worker' not found", "agent_not_found"
            return name, "", ""

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch("kiro_crew.subagent._validate_agent", side_effect=validate),
            patch(
                "kiro_crew.subagent.list_agents",
                return_value=[
                    SimpleNamespace(name=name, filename=f"example-app--{name}.json")
                    for name in ("worker", "other-worker")
                ],
            ),
            patch(
                "kiro_crew.subagent._vet_spawn_governance",
                side_effect=lambda parent, agent, app="": (
                    "worker denied" if agent == "worker" and refusal == "spawn-policy" else None
                ),
            ) as governance,
        ):
            override = manager.continue_conversation(
                "original",
                "temporary task",
                parent_session_key="dashboard:owner",
                agent="other-worker",
            )
            assert override is not None and not override.error
            await asyncio.wait_for(manager._tasks[override.id], timeout=10)
            assert not override.error
            assert sessions.get_or_create.call_args.kwargs["agent"] == "other-worker"
            assert (await asyncio.to_thread(sp.read_run_agent_selection, override.id))[
                1
            ] == "worker"
            manager = _manager(sessions)
            followup = manager.continue_conversation(
                override.id, "next task", parent_session_key="dashboard:owner"
            )
            assert followup is not None and not followup.error
            await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
        assert sessions.get_or_create.await_count == 1
        if refusal == "removed-template":
            assert followup.error_code == "agent_not_found"
        else:
            assert "worker denied" in followup.error
            governance.assert_any_call("dashboard:owner", "worker", app="example-app")
        assert followup.app == "example-app"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live", "restart"])
    async def test_private_override_chain_retains_memory_authority(
        self, restart: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.history import ConversationLog
        from kiro_crew.member_memory_auth import (
            bind_private_session_store,
            private_memory_store_for_session,
        )
        from kiro_crew.memory_stores import provision_member_memory

        # No provider process is launched; only the OS capability probe is
        # modeled. Store provisioning, delegation and protected bindings are real.
        monkeypatch.setattr(
            "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kw: True
        )

        def provision() -> tuple[str, str]:
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="worker")
            cfg.agents["other-worker"] = KiroCrewAgentConfig(kiro_agent="other-worker")
            store = provision_member_memory(cfg, "worker")
            peer = provision_member_memory(cfg, "other-worker")
            cfg.save()
            return store, peer

        store, peer = await asyncio.to_thread(provision)
        history = ConversationLog()
        parent = "dashboard:worker"
        await asyncio.to_thread(bind_private_session_store, parent, store)
        await asyncio.to_thread(history.update_metadata, parent, {"memory_store": store})
        sessions = _mock_sessions()
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0
        manager._ctx_builder.conversation_log = history
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
        ):
            original = manager.spawn(
                "private task",
                parent_session_key=parent,
                agent="worker",
                memory_store=store,
                keep=True,
            )
            assert original is not None and not original.error
            await asyncio.wait_for(manager._tasks[original.id], timeout=10)
            assert not original.error
            target = original
            for override in ("other-worker", ""):
                if restart:
                    sessions = _mock_sessions(resumed=True)
                    manager = _manager(sessions)
                    manager._spawn_stagger_secs = 0
                    manager._ctx_builder.conversation_log = history
                else:
                    provider = sessions.get_or_create.return_value[0]
                    sessions.get_or_create.return_value = (provider, True, True)
                sessions.get_agent.return_value = "conductor"
                followup = manager.continue_conversation(
                    target.id, "private follow-up", parent_session_key=parent, agent=override
                )
                assert followup is not None and not followup.error
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                assert not followup.error
                assert sessions.get_or_create.call_args.kwargs["agent"] == (override or "worker")
                assert followup.memory_store == store
                assert await asyncio.to_thread(sp.read_run_memory_store, followup.id) == store
                assert (
                    await asyncio.to_thread(
                        private_memory_store_for_session, f"subagent:{target.id}"
                    )
                    == store
                )
                await asyncio.to_thread(
                    sp.update_state, followup.id, agent="other-worker", memory_store=peer
                )
                target = followup
            # A temporary template also cannot authorize entry into its
            # namesake member's store on behalf of the private caller.
            await asyncio.to_thread(
                sp.create_agent_folder, "peer", agent="other-worker", memory_store=peer
            )
            await asyncio.to_thread(sp.write_run_agent, "peer", "other-worker")
            allocated = sessions.get_or_create.await_count
            refused = manager.continue_conversation(
                "peer", "cross-member follow-up", parent_session_key=parent, agent="worker"
            )
            assert refused is not None
            if not refused.done:
                await asyncio.wait_for(manager._tasks[refused.id], timeout=10)
            assert "must retain that member's private memory" in refused.error
            assert sessions.get_or_create.await_count == allocated
        assert (await asyncio.to_thread(sp.read_run_agent_selection, target.id))[1] == "worker"
        assert await asyncio.to_thread(sp.read_run_memory_store, target.id) == store

    @pytest.mark.asyncio
    @pytest.mark.parametrize("continuation", [False, True])
    async def test_inherited_template_obeys_spawn_scope(self, continuation: bool) -> None:
        import kiro_crew.subagent_persistence as sp

        sessions = _mock_sessions(resumed=continuation)
        sessions.get_agent.return_value = "worker"
        manager = _manager(sessions)
        if continuation:
            await asyncio.to_thread(sp.create_agent_folder, "original", agent="worker")
            await asyncio.to_thread(sp.write_run_agent, "original", "worker")
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
            patch(
                "kiro_crew.subagent._vet_spawn_governance",
                side_effect=lambda parent, agent, app="": (
                    "worker denied" if agent == "worker" else None
                ),
            ) as governance,
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            if continuation:
                info = manager.continue_conversation(
                    "original", "follow-up", parent_session_key="dashboard:owner"
                )
            else:
                info = manager.spawn("task", parent_session_key="dashboard:owner", keep=True)
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert "worker denied" in info.error
        governance.assert_any_call("dashboard:owner", "worker", app="")
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [True, False])
    @pytest.mark.parametrize("override", ["", "other-worker"])
    async def test_resume_retains_worker_template(self, live: bool, override: str) -> None:
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "original", agent="worker")
        await asyncio.to_thread(sp.write_run_agent, "original", "worker")
        sessions = _mock_sessions(resumed=True)
        sessions.get_agent.return_value = "conductor"
        manager = _manager(sessions)
        if live:
            manager._agents["original"] = SubagentInfo(
                id="original", task="first task", agent="worker", done=True
            )
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            info = manager.continue_conversation(
                "original", "follow-up", parent_session_key="dashboard:owner", agent=override
            )
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert not info.error
        call = sessions.get_or_create.call_args
        assert call.args[0] == "subagent:original"
        assert call.kwargs["agent"] == (override or "worker")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state_agent", ["worker", "conductor", "", None])
    async def test_implicit_spawn_records_template_for_restart(
        self, state_agent: str | None
    ) -> None:
        import kiro_crew.subagent_persistence as sp

        sessions = _mock_sessions()
        sessions.get_agent.return_value = "worker"
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("first task", parent_session_key="dashboard:worker", keep=True)
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert not info.error
        state = await asyncio.to_thread(sp.read_state, info.id)
        assert state is not None and state["agent"] == "worker"
        # Workers can edit this diagnostic file. Restart must preserve the
        # gateway's effective template even when that field is changed.
        await asyncio.to_thread(sp.update_state, info.id, agent=state_agent)

        restored_sessions = _mock_sessions(resumed=True)
        restored_sessions.get_agent.return_value = "conductor"
        restored = _manager(restored_sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
            patch.object(restored, "_promote_conversation", return_value=object()),
        ):
            followup = restored.continue_conversation(
                info.id, "follow-up after restart", parent_session_key="dashboard:owner"
            )
            assert followup is not None and not followup.error
            await asyncio.wait_for(restored._tasks[followup.id], timeout=10)
        assert not followup.error
        assert restored_sessions.get_or_create.call_args.kwargs["agent"] == "worker"

    @pytest.mark.asyncio
    async def test_recorded_default_does_not_inherit_new_parent(self) -> None:
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "original", agent="")
        await asyncio.to_thread(sp.write_run_agent, "original", "")
        sessions = _mock_sessions(resumed=True)
        sessions.get_agent.return_value = "conductor"
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            info = manager.continue_conversation(
                "original", "follow-up", parent_session_key="dashboard:owner"
            )
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert not info.error
        assert sessions.get_or_create.call_args.kwargs["agent"] is None

    @pytest.mark.asyncio
    async def test_unavailable_recorded_template_refuses_allocation(self) -> None:
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "original", agent="removed-worker")
        await asyncio.to_thread(sp.write_run_agent, "original", "removed-worker")
        sessions = _mock_sessions(resumed=True)
        sessions.get_agent.return_value = "conductor"
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent",
                return_value=("", "agent 'removed-worker' not found", "agent_not_found"),
            ),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            info = manager.continue_conversation(
                "original", "follow-up", parent_session_key="dashboard:owner"
            )
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert "removed-worker" in info.error
        assert info.error_code == "agent_not_found"
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    async def test_legacy_template_requires_explicit_override(self, override: str) -> None:
        import kiro_crew.subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "original", agent="conductor")
        sessions = _mock_sessions(resumed=True)
        sessions.get_agent.return_value = "conductor"
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
            patch.object(manager, "_promote_conversation", return_value=object()),
        ):
            info = manager.continue_conversation(
                "original", "follow-up", parent_session_key="dashboard:owner", agent=override
            )
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        if override:
            assert not info.error
            assert sessions.get_or_create.call_args.kwargs["agent"] == "worker"
        else:
            assert "protected agent template unavailable" in info.error
            sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("lineage", ["initial", "known", "unknown"])
    async def test_template_publication_failure_refuses_allocation(self, lineage: str) -> None:
        import kiro_crew.subagent_persistence as sp

        if lineage != "initial":
            await asyncio.to_thread(sp.create_agent_folder, "original", agent="worker")
            if lineage == "known":
                await asyncio.to_thread(sp.write_run_agent, "original", "worker")
        sessions = _mock_sessions()
        manager = _manager(sessions)
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
            patch.object(
                type(manager._run_events),
                "_write_run_agent",
                side_effect=OSError("protected template unavailable"),
            ),
        ):
            if lineage == "initial":
                info = manager.spawn("first task", parent_session_key="dashboard:worker")
            else:
                info = manager.continue_conversation(
                    "original",
                    "next task",
                    parent_session_key="dashboard:worker",
                    agent="other-worker",
                )
            assert info is not None and not info.error
            await asyncio.wait_for(manager._tasks[info.id], timeout=10)
        assert "protected template unavailable" in info.error
        sessions.get_or_create.assert_not_awaited()


@pytest.fixture
def continuation_runtime(tmp_path, monkeypatch):
    """Real allocation and protected state; only the external native provider is doubled."""
    import json
    import uuid
    from types import SimpleNamespace

    from kiro_crew import agent, agent_state, session
    from kiro_crew.agent_capabilities import CapabilityService, prepare_member_capabilities
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import (
        bind_private_session_store,
        private_memory_store_for_session,
    )
    from kiro_crew.memory_stores import provision_member_memory
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent, LLMProvider

    home, specs, project, native = (
        tmp_path / name for name in ("home", "agents", "project", "native")
    )
    for path in (home, specs, project, native):
        path.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr("kiro_crew.agent_discovery._KIRO_AGENTS_DIR", specs)
    # Model resolution and the native provider must read the same agent specs.
    monkeypatch.setattr("kiro_crew.config.loader.kiro_agents_dir", lambda: specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", native)
    # Keep real V2 provisioning, ownership, delegation and binding checks.
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kw: True
    )
    for name in ("worker", "member-parent"):
        (specs / f"{name}.json").write_text(
            json.dumps({"name": name, "prompt": name, "tools": [], "includeMcpJson": False}),
            encoding="utf-8",
        )
    cfg = KiroCrewConfig.load()
    cfg.workspaces[cfg.default_workspace] = WorkspaceConfig(dir=str(project))
    cfg.session.pool_size = 0
    cfg.agent.subagent_cwd_allowed_roots = [str(project)]
    cfg.agents["owner"] = KiroCrewAgentConfig(kiro_agent="worker")
    store = provision_member_memory(cfg, "owner")
    cfg.save()
    history = ConversationLog()
    parent = "dashboard:owner"
    bind_private_session_store(parent, store)
    history.update_metadata(parent, {"memory_store": store})
    made = []
    managers = []

    class NativeProvider(LLMProvider):
        def __init__(self, key, template, cwd, crew_agent, private):
            self.key, self.template, self._cwd = key, template, cwd
            self.crew_agent = crew_agent
            self._private_memory = private
            self.active = ""
            self.incarnation = ""
            self.messages = []
            self.resume_sid = ""
            self.client = SimpleNamespace(
                _session_id="",
                _pid=None,
                resumed=False,
                set_resume_session_id=self.set_resume_session_id,
            )

        def set_resume_session_id(self, sid):
            self.resume_sid = sid

        async def prepare_private_memory(self):
            pass

        async def start(self):
            def load():
                spec = json.loads((specs / f"{self.template}.json").read_text(encoding="utf-8"))
                if self.resume_sid:
                    assert (native / f"{self.resume_sid}.json").is_file()
                sid = self.resume_sid or uuid.uuid4().hex
                (native / f"{sid}.json").write_text("{}", encoding="utf-8")
                (native / f"{sid}.jsonl").write_text(
                    '{"role":"user","content":"original context"}\n', encoding="utf-8"
                )
                return spec["name"], sid

            self.active, self.client._session_id = await asyncio.to_thread(load)
            self.client.resumed = bool(self.resume_sid)
            self.incarnation = uuid.uuid4().hex

        async def shutdown(self):
            self.incarnation = ""

        async def stream(self, message):
            owner = next(
                manager for manager in reversed(managers) if manager.get_provider(self.key) is self
            )
            self.capability_stamp = owner._sessions[self.key].loaded_capabilities
            self.messages.append(message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="done")
            yield LLMEvent(kind=EVENT_COMPLETE)

        async def approve_tool(self, request_id, *, always=False):
            pass

        async def reject_tool(self, request_id):
            pass

        def context_usage_pct(self):
            return 0

        def is_process_alive(self):
            return bool(self.incarnation)

        def is_alive(self):
            return self.is_process_alive()

        @property
        def cwd(self):
            return self._cwd

        @property
        def session_id(self):
            return self.client._session_id

        @property
        def process_instance(self):
            return self.incarnation

        @property
        def member_capabilities_supported(self):
            return True

        @property
        def loaded_capability_template(self):
            return self.active

    # Expose native session/load through the manager's external-provider seam.
    is_acp_provider = session._is_acp_provider
    monkeypatch.setattr(
        session,
        "_is_acp_provider",
        lambda provider: isinstance(provider, NativeProvider) or is_acp_provider(provider),
    )

    def factory(key, agent=None, cwd=None, crew_agent=None, **kwargs):
        provider = NativeProvider(
            key, agent, cwd, crew_agent, bool(private_memory_store_for_session(key))
        )
        provider.model_override = kwargs.get("model_override")
        made.append(provider)
        return provider

    def new_manager():
        sessions = session.SessionManager(KiroCrewConfig.load(), provider_factory=factory)
        managers.append(sessions)
        manager = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
        manager._ctx_builder.conversation_log = history
        manager._spawn_stagger_secs = 0
        return sessions, manager

    def enroll_collision():
        cfg = KiroCrewConfig.load()
        cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="member-parent")
        peer_store = provision_member_memory(cfg, "worker")
        cfg.save()
        service = CapabilityService()
        request = {"revision": service.get("worker")["revision"], "enroll": True}
        preview = service.preview("worker", request)
        service.put("worker", {**request, "preview_token": preview["preview_token"]})
        prepared = prepare_member_capabilities("worker", project)
        assert prepared["template"] != "worker"
        return prepared["template"], peer_store

    def update_member():
        service = CapabilityService()
        request = {
            "revision": service.get("worker")["revision"],
            "operations": [
                {"section": "prompt", "id": "prompt", "action": "set", "value": "updated member"}
            ],
        }
        preview = service.preview("worker", request)
        service.put("worker", {**request, "preview_token": preview["preview_token"]})
        return prepare_member_capabilities("worker", project)["template"]

    return SimpleNamespace(
        new_manager=new_manager,
        enroll_collision=enroll_collision,
        made=made,
        parent=parent,
        store=store,
        project=str(project),
        history=history,
        update_member=update_member,
        specs=specs,
    )


class TestContinuationTemplateNamespace:
    def test_parent_selection_snapshot_keeps_allocation_namespace(self):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        cfg = KiroCrewConfig()
        sessions = SessionManager(cfg)
        assert sessions.get_agent_selection("absent") == ("template", "")
        session = _Session(provider=MagicMock(), agent="worker")
        sessions._sessions["parent"] = session
        template = sessions.get_agent_selection("parent")
        cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="different-template")
        assert sessions.get_agent_selection("parent") == template == ("template", "worker")
        session.capability_member = "member-alias"
        member = sessions.get_agent_selection("parent")
        session.agent = "a-new-generation"
        assert member == sessions.get_agent_selection("parent") == ("member", "member-alias")
        assert template == ("template", "worker")

    @pytest.mark.parametrize("agent,member", [(None, ""), ("worker", None), ("worker", [])])
    def test_present_parent_with_unavailable_selection_refuses(self, agent, member):
        from types import SimpleNamespace

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        sessions = SessionManager(KiroCrewConfig())
        sessions._sessions["parent"] = SimpleNamespace(agent=agent, capability_member=member)
        with pytest.raises(ValueError, match="parent agent selection unavailable"):
            sessions.get_agent_selection("parent")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live-manager", "restart"])
    async def test_retained_template_survives_same_named_member_enrollment(
        self, continuation_runtime, restart
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.member_memory_auth import private_memory_store_for_session

        world = continuation_runtime
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                original = manager.spawn(
                    "original task",
                    parent_session_key=world.parent,
                    agent="worker",
                    memory_store=world.store,
                    keep=True,
                    cwd=world.project,
                )
                assert original is not None and not original.error
                await asyncio.wait_for(manager._tasks[original.id], timeout=10)
                assert not original.error
                key = f"subagent:{original.id}"
                first = world.made[-1]
                assert first.template == "worker" and first.messages
                sid = first.session_id
                assert sessions.resumable_sid(key) == sid
                assert (await asyncio.to_thread(sp.read_run_agent_selection, original.id))[
                    1
                ] == "worker"
                generation, peer_store = await asyncio.to_thread(world.enroll_collision)
                assert peer_store != world.store
                if restart:
                    await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)
                    sessions, manager = world.new_manager()
                    assert not manager._agents
                followup = manager.continue_conversation(
                    original.id,
                    "continue the task",
                    parent_session_key=world.parent,
                    cwd=world.project,
                )
                assert followup is not None and not followup.error
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                assert not followup.error
                resumed = world.made[-1]
                assert resumed is not first and resumed.messages
                assert resumed.key == key
                assert resumed.resume_sid == sid == resumed.session_id
                assert followup.conversation_key == key
                assert sessions.resumable_sid(key) == sid
                assert (await asyncio.to_thread(sp.read_run_agent_selection, followup.id))[
                    1
                ] == "worker"
                assert await asyncio.to_thread(sp.read_run_memory_store, followup.id) == world.store
                assert await asyncio.to_thread(private_memory_store_for_session, key) == world.store
                assert resumed.template == "worker", (
                    f"retained template was replaced by member generation {generation}: "
                    f"{resumed.template}; crew_agent={resumed.crew_agent!r}"
                )
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fallback", [False, True], ids=["dedicated", "shared-fallback"])
    async def test_explicit_template_is_not_a_same_named_enrolled_member(
        self, continuation_runtime, monkeypatch, fallback
    ):
        world = continuation_runtime
        generation, _ = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        shared = AsyncMock(side_effect=RuntimeError("parent runtime unavailable"))
        if fallback:
            # Model an eligible global parent's native runtime failing to create
            # a shared handle. Dedicated fallback still uses the real allocator.
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "use the explicit template",
                    agent="worker",
                    cwd=world.project,
                    keep=not fallback,
                )
                assert child is not None and not child.error
                await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert not child.error
            if fallback:
                shared.assert_awaited_once()
            provider = world.made[-1]
            assert provider.key == f"subagent:{child.id}"
            assert provider.messages and not provider._private_memory
            assert provider.template == "worker", (
                f"explicit template was replaced by member generation {generation}: "
                f"{provider.template}; crew_agent={provider.crew_agent!r}"
            )
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    @pytest.mark.parametrize("queued", [False, True])
    async def test_named_crew_keeps_member_identity(self, continuation_runtime, override, queued):
        from kiro_crew import subagent_persistence as sp

        world = continuation_runtime
        generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                if queued:
                    manager._running_count = manager.max_concurrent
                child = manager.spawn(
                    "named member task",
                    crew="worker",
                    agent=override,
                    memory_store=store,
                    keep=True,
                    cwd=world.project,
                )
                assert child is not None and not child.error
                if queued:
                    assert child.queued
                    assert manager._queue[0]["crew"] == "worker"
                    manager._running_count = 0
                    manager._drain_queue()
                    for _ in range(100):
                        if child.id in manager._tasks:
                            break
                        await asyncio.sleep(0.01)
                    child = manager._agents[child.id]
                await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert not child.error
                provider = world.made[-1]
                assert provider.template == (override or generation)
                assert provider.crew_agent == ("" if override else "worker")
                assert await asyncio.to_thread(sp.read_run_agent_selection, child.id) == (
                    "member",
                    "worker",
                )
                await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)
                sessions, manager = world.new_manager()
                followup = manager.continue_conversation(
                    child.id,
                    "continue member task",
                    cwd=world.project,
                )
                assert followup is not None and not followup.error
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                assert not followup.error
                assert world.made[-1].crew_agent == "worker"
                assert world.made[-1].template == generation
                assert await asyncio.to_thread(sp.read_run_agent_selection, followup.id) == (
                    "member",
                    "worker",
                )
        finally:
            manager._running_count = 0
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    async def test_http_named_crew_reaches_member_allocation(self, continuation_runtime, override):
        import json

        from test_handlers_messaging_coverage import _Req, _state

        from kiro_crew import subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        world = continuation_runtime
        generation, store = await asyncio.to_thread(world.enroll_collision)

        def enable_delegation():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"].triggers = "work"
            cfg.save()

        await asyncio.to_thread(enable_delegation)
        sessions, manager = world.new_manager()
        state = _state(subagents=manager, sessions=sessions, conversation_log=world.history)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                response = await api_spawn(
                    _Req(
                        state,
                        {
                            "task": "named member task",
                            "crew": "worker",
                            "agent": override,
                            "cwd": world.project,
                            "keep": True,
                        },
                    )
                )
                assert response.status == 200, response.text
                child = manager._agents[json.loads(response.text)["id"]]
                await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert not child.error
                assert child.memory_store == store
                assert world.made[-1].template == (override or generation)
                assert world.made[-1].crew_agent == ("" if override else "worker")
                assert await asyncio.to_thread(sp.read_run_agent_selection, child.id) == (
                    "member",
                    "worker",
                )
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    @pytest.mark.parametrize("fault", ["removed", "rebound", "governance"])
    async def test_queued_named_crew_revalidates_identity(
        self, continuation_runtime, monkeypatch, override, fault
    ):
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        _generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                manager._running_count = manager.max_concurrent
                queued = manager.spawn(
                    "queued member task",
                    crew="worker",
                    agent=override,
                    memory_store=store,
                    cwd=world.project,
                )
                assert queued is not None and queued.queued and not queued.error
                params = manager._queue.pop(0)
                if fault == "governance":
                    monkeypatch.setattr(
                        "kiro_crew.subagent._vet_spawn_governance",
                        lambda parent, agent, **kw: "member denied" if agent == "worker" else "",
                    )
                else:

                    def change_member():
                        cfg = KiroCrewConfig.load()
                        if fault == "removed":
                            del cfg.agents["worker"]
                        else:
                            cfg.agents["worker"].memory_store = world.store
                        cfg.save()

                    await asyncio.to_thread(change_member)
                manager._running_count = 0
                child = manager.spawn(**params, _from_queue=True)
                assert child is not None
                if child.id in manager._tasks:
                    await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert child.error
                assert not world.made, "a refused identity must not allocate any provider"
        finally:
            manager._running_count = 0
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    async def test_initial_implicit_parent_keeps_crew_alias_resolution(self, continuation_runtime):
        world = continuation_runtime
        generation, peer_store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        from kiro_crew.member_memory_auth import bind_private_session_store

        parent = "slack:C123:123.456"
        await asyncio.to_thread(bind_private_session_store, parent, peer_store)
        await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": peer_store})
        try:
            await asyncio.wait_for(
                sessions.get_or_create(parent, agent="worker", cwd=world.project), timeout=10
            )
            sessions.release(parent)
            assert sessions.get_agent(parent) == "worker"
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "inherit the parent",
                    parent_session_key=parent,
                    memory_store=peer_store,
                    keep=True,
                    cwd=world.project,
                )
                assert child is not None and not child.error
                await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert not child.error
            assert world.made[-1].template == generation
            assert world.made[-1].crew_agent == "worker"
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fallback", [False, True], ids=["dedicated", "shared-fallback"])
    @pytest.mark.parametrize("template_model", ["", "template-model"])
    @pytest.mark.parametrize("override", [None, "caller-model"])
    async def test_literal_template_does_not_borrow_member_model(
        self, continuation_runtime, monkeypatch, fallback, template_model, override
    ):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        await asyncio.to_thread(world.enroll_collision)

        def configure_models():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"].model = "member-model"
            cfg.agent.model = "global-model"
            cfg.save()
            path = world.specs / "worker.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            spec["model"] = template_model
            path.write_text(json.dumps(spec), encoding="utf-8")
            return cfg

        cfg = await asyncio.to_thread(configure_models)
        sessions, manager = world.new_manager()
        shared = AsyncMock(side_effect=RuntimeError("parent runtime unavailable"))
        if fallback:
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "use the selected template and its model",
                    agent="worker",
                    model=override or "",
                    cwd=world.project,
                    keep=not fallback,
                )
                assert child is not None and not child.error
                await asyncio.wait_for(manager._tasks[child.id], timeout=10)
                assert not child.error
            if fallback and not override:
                shared.assert_awaited_once()
            else:
                shared.assert_not_awaited()
            provider = world.made[-1]
            assert provider.template == "worker" and provider.crew_agent == ""
            assert provider.messages
            expected_override = override or (None if template_model else "global-model")
            assert provider.model_override == expected_override
            # Exercise the same remaining model tier used by the real factory.
            resolved = await asyncio.to_thread(
                cfg.acp_effective_model, provider.template, provider.model_override
            )
            assert resolved == (override or template_model or "global-model")
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live-manager", "restart"])
    async def test_member_continuation_adopts_generation_after_template_override(
        self, continuation_runtime, monkeypatch, restart
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.member_memory_auth import (
            bind_private_session_store,
            private_memory_store_for_session,
        )
        from kiro_crew.subagent import _vet_spawn_governance

        world = continuation_runtime
        governance = MagicMock(wraps=_vet_spawn_governance)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", governance)
        generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        parent = "slack:C123:123.456"
        await asyncio.to_thread(bind_private_session_store, parent, store)
        await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": store})
        try:
            await asyncio.wait_for(
                sessions.get_or_create(parent, agent="worker", cwd=world.project), timeout=10
            )
            sessions.release(parent)
            assert sessions.get_agent_selection(parent) == ("member", "worker")
            # A member must reach dedicated preparation even if a sharing
            # eligibility result is stale after capability enrollment.
            shared = AsyncMock(side_effect=AssertionError("member bypassed preparation"))
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                original = manager.spawn(
                    "member task", parent_session_key=parent, memory_store=store, cwd=world.project
                )
                assert original is not None and not original.error
                await asyncio.wait_for(manager._tasks[original.id], timeout=10)
                assert not original.error
                shared.assert_not_awaited()
                first = world.made[-1]
                sid = first.session_id
                assert first.template == generation and first.crew_agent == "worker"
                assert first.capability_stamp.member == "worker"
                assert first.capability_stamp.template == generation
                governance.assert_any_call(parent, "worker", app="")
                assert await asyncio.to_thread(sp.read_run_agent_selection, original.id) == (
                    "member",
                    "worker",
                )
                updated = await asyncio.to_thread(world.update_member)
                assert updated != generation
                target = original
                for override in ("worker", ""):
                    if restart:
                        await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)
                        sessions, manager = world.new_manager()
                    key = f"subagent:{target.id}"
                    followup = manager.continue_conversation(
                        target.id,
                        "member follow-up",
                        parent_session_key=parent,
                        agent=override,
                        cwd=world.project,
                    )
                    assert followup is not None and not followup.error
                    await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                    assert not followup.error
                    provider = world.made[-1]
                    assert provider.key == followup.conversation_key == key
                    assert provider.resume_sid == provider.session_id == sid
                    assert provider.template == ("worker" if override else updated)
                    assert provider.crew_agent == ("" if override else "worker")
                    assert provider.messages
                    if override:
                        assert provider.capability_stamp is None
                    else:
                        assert provider.capability_stamp.member == "worker"
                        assert provider.capability_stamp.template == updated
                        assert provider.capability_stamp.session_id == sid
                    # Policy names the selected identity, never its generated
                    # runtime artifact. The explicit override also names worker.
                    scoped_targets = [
                        call.args[1] for call in governance.call_args_list if call.args[1]
                    ]
                    assert scoped_targets and set(scoped_targets) == {"worker"}
                    assert await asyncio.to_thread(sp.read_run_agent_selection, followup.id) == (
                        "member",
                        "worker",
                    )
                    assert await asyncio.to_thread(private_memory_store_for_session, key) == store
                    assert await asyncio.to_thread(sp.read_run_memory_store, followup.id) == store
                    target = followup
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault", ["removed", "rebound", "governance"])
    async def test_retained_member_cannot_fall_back_or_change_authority(
        self, continuation_runtime, monkeypatch, fault
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        _, store = await asyncio.to_thread(world.enroll_collision)
        await asyncio.to_thread(sp.create_agent_folder, "member-run", memory_store=store)
        await asyncio.to_thread(sp.write_run_agent, "member-run", "worker", kind="member")
        authority_path = sp._run_memory_identity_path("member-run")
        authority = await asyncio.to_thread(authority_path.read_bytes)
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._ctx_builder.conversation_log = world.history
        if fault == "governance":
            monkeypatch.setattr(
                "kiro_crew.subagent._vet_spawn_governance",
                lambda parent, agent, app="": "member denied" if agent == "worker" else None,
            )
        else:

            def change_member():
                cfg = KiroCrewConfig.load()
                if fault == "removed":
                    del cfg.agents["worker"]
                else:
                    cfg.agents["worker"].memory_store = world.store
                cfg.save()

            await asyncio.to_thread(change_member)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            followup = manager.continue_conversation("member-run", "follow-up", cwd=world.project)
            assert followup is not None
            if not followup.done:
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
        assert (
            "member denied" if fault == "governance" else "memory_unavailable:"
        ) in followup.error
        sessions.get_or_create.assert_not_awaited()
        assert await asyncio.to_thread(authority_path.read_bytes) == authority

    @pytest.mark.asyncio
    async def test_missing_member_never_becomes_same_named_template(self, continuation_runtime):
        from kiro_crew import subagent_persistence as sp

        world = continuation_runtime
        # worker.json exists, but worker is not a configured member.
        await asyncio.to_thread(sp.create_agent_folder, "member-run")
        await asyncio.to_thread(sp.write_run_agent, "member-run", "worker", kind="member")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._ctx_builder.conversation_log = world.history
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            followup = manager.continue_conversation("member-run", "follow-up", cwd=world.project)
            assert followup is not None and not followup.error
            await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
        assert "selected member is unavailable" in followup.error
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", [None, "caller-model"])
    async def test_unenrolled_member_keeps_its_model_pin(self, continuation_runtime, override):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

        world = continuation_runtime

        def configure_member():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"] = KiroCrewAgentConfig(
                kiro_agent="member-parent", model="member-model"
            )
            cfg.agent.model = "global-model"
            cfg.save()

        await asyncio.to_thread(configure_member)
        sessions, _ = world.new_manager()
        try:
            provider, _, _ = await asyncio.wait_for(
                sessions.get_or_create(
                    "member-session",
                    agent="member-parent",
                    crew_agent="worker",
                    cwd=world.project,
                    model=override,
                ),
                timeout=10,
            )
            assert provider.template == "member-parent"
            assert provider.model_override == (override or "member-model")
            assert sessions.get_agent_selection("member-session") == ("member", "worker")
            sessions.release("member-session")
        finally:
            await asyncio.wait_for(sessions.close_all(drain_timeout=0), timeout=10)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy", ["worker", ""])
    async def test_legacy_namespace_never_becomes_new_template_authority(self, legacy):
        import json

        from kiro_crew import subagent_persistence as sp

        await asyncio.to_thread(sp.create_agent_folder, "legacy")
        await asyncio.to_thread(
            sp._run_agent_identity_path("legacy").write_text,
            json.dumps({"version": 1, "agent": legacy}),
            encoding="utf-8",
        )
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
        ):
            target = "legacy"
            for override in ("", "other-worker", ""):
                followup = manager.continue_conversation(target, "next turn", agent=override)
                assert followup is not None and not followup.error
                allocated = sessions.get_or_create.await_count
                await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
                if legacy and not override:
                    assert "protected agent template unavailable" in followup.error
                    assert sessions.get_or_create.await_count == allocated
                else:
                    assert not followup.error
                    assert sessions.get_or_create.call_args.kwargs["crew_agent"] == ""
                    assert sessions.get_or_create.call_args.kwargs["agent"] == (override or None)
                    if legacy:
                        with pytest.raises(
                            ValueError, match="protected agent template unavailable"
                        ):
                            await asyncio.to_thread(sp.read_run_agent_selection, followup.id)
                    else:
                        assert await asyncio.to_thread(
                            sp.read_run_agent_selection, followup.id
                        ) == ("template", "")
                    target = followup.id
                manager = _manager(sessions)
                manager._spawn_stagger_secs = 0


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
        assert read_run_app(conv_id) == read_run_app(info.id) == ""
        assert read_run_agent_selection(info.id) == ("template", "")
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
        assert read_run_agent_selection(resumed.id) == ("template", "")
        assert read_run_app(resumed.id) == ""

    @pytest.mark.asyncio
    async def test_missing_original_policy_never_allocates_provider(self):
        from kiro_crew.subagent_persistence import _run_memory_identity_path, create_agent_folder

        await asyncio.to_thread(
            create_agent_folder, "missing-resume-policy", memory_mode="incognito"
        )
        await asyncio.to_thread(write_run_agent, "missing-resume-policy", "")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("moved_pool", [False, True], ids=["same-default", "changed-default"])
async def test_default_cwd_continuation_does_not_request_override(tmp_path, moved_pool):
    from kiro_crew.config.loader import KiroCrewConfig

    project = tmp_path / "original-project"
    project.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()

    def disable_overrides():
        cfg = KiroCrewConfig.load()
        cfg.agent.subagent_cwd_allowed_roots = []
        cfg.save()

    await asyncio.to_thread(disable_overrides)
    sessions = _mock_sessions()
    sessions._pool_cwd = str(project)
    provider = sessions.get_or_create.return_value[0]
    provider.cwd = str(project)
    manager = _manager(sessions)
    manager._spawn_stagger_secs = 0
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        original = manager.spawn("first task", keep=True)
        assert original is not None and not original.error
        await asyncio.wait_for(manager._tasks[original.id], 10)
        assert not original.error
        sessions.get_or_create.return_value = (provider, True, True)
        if moved_pool:
            sessions._pool_cwd = str(other)
        recorded = await asyncio.to_thread(manager.recorded_cwd, original.id)
        followup = manager.continue_conversation(original.id, "next task", cwd=recorded)
        assert followup is not None
        if moved_pool:
            assert recorded == str(project)
            assert "cwd override is disabled" in followup.error
            assert sessions.get_or_create.await_count == 1
        else:
            assert recorded == "" and not followup.error
            await asyncio.wait_for(manager._tasks[followup.id], 10)
            assert not followup.error
            assert sessions.get_or_create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_metadata", [True, False], ids=["verified", "mismatch"])
async def test_channel_spawn_carries_verified_member_store(continuation_runtime, valid_metadata):
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.memory_stores import UnknownMemoryStore
    from kiro_crew.messaging.commands import _spawn_off_loop

    world = continuation_runtime
    generation, store = await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    parent = "slack:C123:456.789"
    await asyncio.to_thread(bind_private_session_store, parent, store)
    await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": store})
    try:
        await asyncio.wait_for(
            sessions.get_or_create(parent, agent="worker", cwd=world.project), 10
        )
        sessions.release(parent)
        assert sessions.get_agent_selection(parent) == ("member", "worker")
        if not valid_metadata:
            await asyncio.to_thread(
                world.history.update_metadata, parent, {"memory_store": "default"}
            )
            with pytest.raises(UnknownMemoryStore):
                await _spawn_off_loop(manager, "delegated task", parent)
            assert not manager._agents
            return
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = await _spawn_off_loop(manager, "delegated task", parent)
            assert child is not None and not child.error
            await asyncio.wait_for(manager._tasks[child.id], 10)
        assert not child.error
        assert child.memory_store == store
        assert world.made[-1].template == generation
        assert world.made[-1].crew_agent == "worker"
        assert world.made[-1]._private_memory
    finally:
        await asyncio.wait_for(sessions.close_all(drain_timeout=0), 10)


class TestSharedBindIdentityLabel:
    """The shared-bind identity write names the backend that served the session.

    ``_bind_shared_handle_impl`` publishes and persists a cleanup identity before any
    cancellable await, so the value it writes is what a run cancelled in that window
    leaves behind. A constant here can be correct for exactly one of the backends this
    path can serve, so it is read from the provider.

    That record is not what a continuation reads: ``_run_inner_impl`` re-captures the
    label from the same provider right after session acquisition, and that value is
    what reaches ``state.json``, so a run that completes was always labelled
    correctly. What the record routes is session-file CLEANUP. With kiro's label on
    another host's session, ``_cleanup_session_files_sync`` takes the kiro branch,
    unlinks a path that was never going to exist and returns success -- where the real
    label returns False, "no cleanup route for this provider", and the prune keeps its
    retry metadata instead of deleting the run folder over a cleanup that did nothing.
    """

    async def _bound(self, backend: str):  # type: ignore[no-untyped-def]
        """Drive the real bind against a provider whose backend is *backend*.

        The provider is a REAL ``AcpSessionProvider``, built the way the bind builds
        one, because ``provider_label`` resolves it by ``isinstance``: a bare
        ``MagicMock`` answers the default label for every backend, which is precisely
        the failure this test exists to catch. The backend is carried by the runtime,
        which is where the provider reads it from in production too.
        """
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.subagent_manager.run import RunEventCoordinator
        from kiro_crew.subagent_persistence import _live_cleanup_identities

        manager = _manager()
        info = SubagentInfo(id=f"bind{len(backend)}", task="t")
        handle = MagicMock()
        handle.session_id = "sid-bound"
        runtime = MagicMock()
        runtime.pid = None
        runtime.acp_backend = backend
        provider = AcpSessionProvider(handle, runtime, session_key="subagent:bind")

        remembered = AsyncMock()
        with (
            patch.object(RunEventCoordinator, "_remember_identity_off_loop", remembered),
            # ``run.py``'s ``*_impl`` bodies resolve their globals through
            # ``kiro_crew.subagent``, so that is where the name has to be replaced.
            patch("kiro_crew.subagent.AcpSessionProvider", lambda *a, **k: provider),
        ):
            await manager._run_events._bind_shared_handle_impl(
                info, "subagent:bind", runtime, handle
            )
        live = [
            record
            for record in (_live_cleanup_identities(info.id) or [])
            if record.get("session_id") == "sid-bound"
        ]
        return info, live, remembered

    @pytest.mark.asyncio
    async def test_a_codex_shared_session_is_labelled_codex(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_CODEX, PROVIDER_LABEL_CODEX

        info, live, remembered = await self._bound(ACP_BACKEND_CODEX)
        assert info._session_provider == PROVIDER_LABEL_CODEX
        assert live and live[0].get("provider") == PROVIDER_LABEL_CODEX, (
            "the live cleanup identity still claims another provider for a codex "
            f"session: {live!r}"
        )
        assert remembered.await_args is not None
        assert remembered.await_args.kwargs["provider"] == PROVIDER_LABEL_CODEX

    @pytest.mark.asyncio
    async def test_a_kiro_shared_session_is_still_labelled_kiro(self) -> None:
        """Not a swap. ``ACP_BACKEND_KIRO`` IS the empty string, so a truthiness test
        on the backend would have sent kiro itself down the wrong branch -- the same
        trap this file's sibling PR fell into once."""
        from kiro_crew.acp.types import ACP_BACKEND_KIRO, PROVIDER_LABEL_DEFAULT

        info, live, _ = await self._bound(ACP_BACKEND_KIRO)
        assert info._session_provider == PROVIDER_LABEL_DEFAULT
        assert live and live[0].get("provider") == PROVIDER_LABEL_DEFAULT
