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

This module holds the SessionManager override, keep threading, continue_conversation
and the continuation's agent inheritance; the other slices live in sibling modules:
``test_subagent_continuable_template`` (the template namespace a continuation runs
under), ``test_subagent_continuable_persistence`` (transcripts, persistence guards,
memory mode), ``test_subagent_continuable_claims`` (restricted state lifetime,
cleanup identity, successor and continuation claims, follow-ups) and
``test_subagent_continuable_runs`` (steer, release and sweep, store calls off the
loop, the run's model and effort). Shared fakes and fixtures are in
``subagent_continuable_helpers``, and the names other suites import from this
module stay importable here.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _stop_reason  # noqa: F401
from subagent_continuable_helpers import _manager, _mock_sessions

from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_persistence import create_agent_folder, write_run_agent
from kiro_crew.testing.wait import await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers
# No test here requests it: test_spawn_reasoning_effort imports it from here.
continuation_runtime = subagent_continuable_helpers.continuation_runtime


# ── SessionManager continuable override (real SessionManager, no processes) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_run_execution_publication_is_off_loop(monkeypatch, continuation):
    from kiro_crew import execution_context, subagent_persistence
    from kiro_crew.subagent import _RunCreditAccounting

    manager = _manager()
    manager.dependency_coordinator_async = AsyncMock(return_value=None)
    captured = execution_context.ExecutionContext(
        None, execution_context.MemoryStoreRef("default"), "template", "kirocrew", "persistent"
    )
    info = SubagentInfo(id="thread-probe", task="synthetic", execution_context=captured)
    if continuation:
        info.conversation_key = "subagent:original"
    key = info.conversation_key or "subagent:thread-probe"
    monkeypatch.setattr(subagent_persistence, "tighten_run_memory_mode", lambda _, mode: mode)
    loop_thread = threading.get_ident()
    operations = []

    def read(session_key, *, required):
        operations.append(("read", threading.get_ident(), session_key))
        assert required
        return captured

    def bind(session_key, execution):
        operations.append(("bind", threading.get_ident(), session_key))
        assert execution == captured

    class Published(Exception):
        pass

    manager._sessions.get_approval_policy.side_effect = Published
    monkeypatch.setattr(execution_context, "read_session_execution", read)
    monkeypatch.setattr(execution_context, "bind_session_execution", bind)
    with pytest.raises(Published):
        await await_until(
            asyncio.create_task(
                manager._run_events._run_inner_impl(info, key, _RunCreditAccounting(info))
            ),
            what="the run reaching its publication",
        )
    assert [operation[0] for operation in operations] == (
        ["read", "bind"] if continuation else ["bind"]
    )
    assert all(
        thread != loop_thread and session_key == key for _, thread, session_key in operations
    )


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
        mgr._safe_cleanup = MagicMock()  # type: ignore[method-assign]
        with patch("kiro_crew.session.asyncio.ensure_future") as ensure:
            mgr.release("subagent:abc", cleanup=True)
        ensure.assert_not_called()
        mgr._safe_cleanup.assert_not_called()
        session.semaphore.release.assert_called_once_with()
        assert session.turn_owner is None

    def test_release_cleanup_runs_for_plain_subagent(self) -> None:
        mgr = self._sessions()
        session = MagicMock()
        session.provider.session_id = "sid-1"
        mgr._sessions["subagent:abc"] = session
        cleanup = object()
        mgr._safe_cleanup = MagicMock(return_value=cleanup)  # type: ignore[method-assign]
        with patch("kiro_crew.session.asyncio.ensure_future") as ensure:
            mgr.release("subagent:abc", cleanup=True)
        mgr._safe_cleanup.assert_called_once_with(session.provider, "sid-1")
        ensure.assert_called_once_with(cleanup)
        session.semaphore.release.assert_called_once_with()

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
            assert info is not None and info.done
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
            await await_until(manager._tasks[original.id], what="the original run finishing")
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
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
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
        import json

        import kiro_crew.subagent_persistence as sp

        directory = await asyncio.to_thread(sp.create_agent_folder, "original", agent="worker")
        path = directory / "state.json"
        if authority == "missing":
            state = json.loads(path.read_text(encoding="utf-8"))
            state["execution_context"] = None
            path.write_text(json.dumps(state), encoding="utf-8")
        elif authority == "corrupt":
            path.write_text("{", encoding="utf-8")
        else:
            path.unlink()
            path.mkdir()
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        with patch("kiro_crew.subagent.sel"):
            followup = manager.continue_conversation("original", "next turn", agent="other-worker")
        assert followup is not None and followup.error
        assert "memory_unavailable" in followup.error
        sessions.get_or_create.assert_not_awaited()

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
            await await_until(manager._tasks[override.id], what="the override run finishing")
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
            await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
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
            await await_until(manager._tasks[original.id], what="the original run finishing")
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
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
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
                await await_until(manager._tasks[refused.id], what="the refused run finishing")
            assert not refused.error
            assert refused.memory_store == peer
            assert sessions.get_or_create.await_count == allocated + 1
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
            await await_until(manager._tasks[info.id], what="the run finishing")
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
            await await_until(manager._tasks[info.id], what="the run finishing")
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
            await await_until(manager._tasks[info.id], what="the run finishing")
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
            await await_until(restored._tasks[followup.id], what="the follow-up run finishing")
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
            await await_until(manager._tasks[info.id], what="the run finishing")
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
            await await_until(manager._tasks[info.id], what="the run finishing")
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
            await await_until(manager._tasks[info.id], what="the run finishing")
        assert not info.error
        assert sessions.get_or_create.call_args.kwargs["agent"] == (override or "conductor")

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
            await await_until(manager._tasks[info.id], what="the run finishing")
        assert "protected template unavailable" in info.error
        sessions.get_or_create.assert_not_awaited()
