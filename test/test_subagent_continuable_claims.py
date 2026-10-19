"""Who owns a continuable run's state and its next turn.

A restricted (incognito or temporary) run's state lives until its report and then
keeps only the original; the shared-bind cleanup identity names the backend that
served the session; a failed run has one successor, a dashboard retry or a
continuation; a continuation start stays claimed by its conversation id without the
opening run's registry record; and a follow-up on a continuation run continues the
root conversation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager, _mock_sessions

from kiro_crew.context_management import evict_completed_agents
from kiro_crew.subagent import SUCCESSOR_UNKNOWN, SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.continuation import ContinuationCoordinator
from kiro_crew.subagent_persistence import create_agent_folder, write_run_agent
from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers


class TestRestrictedRunStateLifetime:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.parametrize("kept,followup", [(False, False), (True, False), (True, True)])
    async def test_terminal_state_waits_for_report_then_only_keeps_original(
        self, mode, kept, followup
    ):
        from kiro_crew import subagent_persistence as persistence

        manager = _manager()
        entered, finish = asyncio.Event(), asyncio.Event()
        original = "retained-original"
        run_id = "follow-up" if followup else original
        key = f"subagent:{original}"
        if kept:
            # flake-ok: live-clock registry stamp; this test never runs the TTL sweep
            manager._conversations[key] = time.time()
        persistence.create_agent_folder(original, task="original private task", memory_mode=mode)
        if followup:
            persistence.create_agent_folder(run_id, task="follow-up private task", memory_mode=mode)
        info = SubagentInfo(
            id=run_id,
            task="private task",
            keep=kept,
            conversation_key=key if followup else "",
            memory_mode=mode,
        )
        manager._agents[run_id] = info
        manager._running_count = 1

        async def inner(info, session_key):
            info.done = True

        async def on_done(info):
            entered.set()
            # flake-ok: the stub parks on an event the test sets, in its finally too
            await finish.wait()
            assert persistence.read_state(run_id)["task"].endswith("private task")

        manager._run_inner = inner
        manager._on_done = on_done
        task = asyncio.create_task(manager._run(info))
        manager._tasks[run_id] = task
        try:
            await await_until(entered, what="the report entering")
            assert persistence.read_state(run_id) is not None
            finish.set()
            await await_until(task, what="the teardown finishing")
            # flake-ok: zero-delay yield after the awaited teardown; moved verbatim
            await asyncio.sleep(0)
            assert (persistence.read_state(run_id) is not None) == (kept and not followup)
            if kept:
                assert persistence.read_state(original) is not None
                with patch("kiro_crew.subagent._cleanup_session_files_sync"):
                    assert manager.release_conversation(original)[0]
                assert persistence.read_state(original) is None
            assert not (persistence._subagents_dir() / run_id).exists()
        finally:
            finish.set()
            # flake-ok: gathers the task this finally just released; moved verbatim
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    @pytest.mark.parametrize("sid", [None, "sid-123"])
    def test_release_removes_evicted_original_without_deleting_persistent_files(self, mode, sid):
        from kiro_crew import subagent_persistence as persistence

        manager = _manager()
        manager._sessions.forget_conversation.return_value = sid
        persistence.create_agent_folder("original", task="private task", memory_mode=mode)
        persistent = persistence.create_agent_folder("durable", task="durable task") / "state.json"
        before = persistent.read_bytes()
        # flake-ok: live-clock registry stamp; this test never runs the TTL sweep
        manager._conversations["subagent:original"] = time.time()
        with patch("kiro_crew.subagent._cleanup_session_files_sync"):
            manager.release_conversation("original")
        assert persistence.read_state("original") is None
        assert persistent.read_bytes() == before

    def test_busy_release_preserves_original_and_queued_child(self):
        from kiro_crew import subagent_persistence as persistence

        manager = _manager()
        persistence.create_agent_folder("original", task="private task", memory_mode="temporary")
        manager._queue.append({"task": "child", "conversation_key": "subagent:original"})
        assert not manager.release_conversation("original")[0]
        assert persistence.read_state("original")["task"] == "private task"

    @pytest.mark.asyncio
    async def test_abandoned_writer_keeps_state_until_it_lands_after_eviction(self):
        from kiro_crew import subagent_persistence as persistence

        manager = _manager()
        info = SubagentInfo(id="late-writer", task="private", memory_mode="temporary")
        persistence.create_agent_folder(info.id, task=info.task, memory_mode=info.memory_mode)
        manager._agents[info.id] = info
        entered, finish = threading.Event(), threading.Event()
        real_update = persistence.update_state

        def update(*args, **kwargs):
            entered.set()
            assert finish.wait(5)
            return real_update(*args, **kwargs)

        with (
            patch("kiro_crew.subagent.update_state", side_effect=update),
            patch("kiro_crew.subagent._STATE_DRAIN_TIMEOUT", 0),
        ):
            writer = asyncio.create_task(manager._write_state_off_loop(info, "late", turns=3))
            try:
                await async_wait_until(entered.is_set)
                writer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    # flake-ok: awaits the writer task the line above cancelled; moved verbatim
                    await writer
                info.done = True
                manager._agents.pop(info.id)
                assert not manager.release_conversation(info.id)[0]
                assert persistence.read_state(info.id)["task"] == "private"
            finally:
                finish.set()
                # flake-ok: gathers the writer this finally just released; moved verbatim
                await asyncio.gather(writer, return_exceptions=True)

            await async_wait_until(
                lambda: info.id not in manager._abandoned_state_writers,
                describe=lambda: f"abandoned writers: {sorted(manager._abandoned_state_writers)}",
            )
        assert persistence.read_state(info.id) is None
        assert not (persistence._subagents_dir() / info.id).exists()

    @pytest.mark.parametrize("tightened", [False, True])
    def test_late_delivery_does_not_persist_released_restricted_payload(self, tightened):
        from kiro_crew import subagent_persistence as persistence

        manager = _manager()
        folder = persistence.create_agent_folder(
            "released", task="original task", memory_mode="persistent" if tightened else "temporary"
        )
        if tightened:
            persistence.tighten_run_memory_mode("released", "temporary")
            persistence.update_state("released", task="restricted follow-up")
        with patch("kiro_crew.subagent._cleanup_session_files_sync"):
            manager.release_conversation("released")
        persistence.mark_delivered("released")
        assert not (folder / "tombstone.json").exists()
        if tightened:
            assert persistence.read_state("released")["task"] == "original task"
            assert persistence.read_run_memory_mode("released") == "temporary"
        else:
            assert persistence.read_state("released") is None


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
        # A real mode, not a MagicMock attribute: the bind assigns
        # ``provider.memory_mode``, whose setter folds it with the handle's own
        # through ``stricter_memory_mode`` and refuses anything outside
        # ``MEMORY_MODES``. Spelled as the handle doubles in
        # ``test_subagent_continuable_persistence.py`` spell it.
        handle.memory_mode = "persistent"
        runtime = MagicMock()
        runtime.pid = None
        runtime.acp_backend = backend
        provider = AcpSessionProvider(handle, runtime, session_key="subagent:bind")

        remembered = AsyncMock()
        with (
            patch.object(RunEventCoordinator, "_remember_identity_off_loop", remembered),
            # ``run_start.py``'s ``*_impl`` bodies resolve their globals through
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


class TestSuccessorClaim:
    """A failed run has one successor: a dashboard retry or a continuation."""

    @staticmethod
    def _failed(manager: SubagentManager, run_id: str = "fail1234") -> SubagentInfo:
        failed = SubagentInfo(id=run_id, task="t", done=True, error="turn_limit:100")
        manager._agents[run_id] = failed
        return failed

    # Every state a failed run's successor claim can be in, and whether a retry
    # and a continuation are each granted from it. The continuation column is
    # the claim alone: a live or queued continuation is refused one step later
    # by the conversation-busy check every continuation already runs.
    @staticmethod
    def _state_none(m: SubagentManager, f: SubagentInfo) -> None:
        return None

    @staticmethod
    def _state_retry_starting(m: SubagentManager, f: SubagentInfo) -> None:
        assert m.claim_retry(f) == ""

    @staticmethod
    def _state_retried(m: SubagentManager, f: SubagentInfo) -> None:
        assert m.claim_retry(f) == ""
        m.settle_retry(f, "retry5678")

    @staticmethod
    def _state_retry_start_failed(m: SubagentManager, f: SubagentInfo) -> None:
        assert m.claim_retry(f) == ""
        m.settle_retry(f, None)

    @staticmethod
    def _state_continuation_starting(m: SubagentManager, f: SubagentInfo) -> None:
        assert m._claim_continuation(f.id, "x", "")[1] is None

    @staticmethod
    def _state_continued(m: SubagentManager, f: SubagentInfo) -> None:
        assert m._claim_continuation(f.id, "x", "")[1] is None
        m._settle_continuation(f.id, f, SubagentInfo(id="cont0001", task="t"))

    @staticmethod
    def _state_continuation_start_failed(m: SubagentManager, f: SubagentInfo) -> None:
        assert m._claim_continuation(f.id, "x", "")[1] is None
        m._settle_continuation(f.id, f, None)

    @staticmethod
    def _state_retry_start_raised(m: SubagentManager, f: SubagentInfo) -> None:
        assert m.claim_retry(f) == ""
        m.settle_retry(f, SUCCESSOR_UNKNOWN)
        m.settle_retry(f, None)

    @staticmethod
    def _state_continuation_start_raised(m: SubagentManager, f: SubagentInfo) -> None:
        assert m._claim_continuation(f.id, "x", "")[1] is None
        m._settle_continuation(f.id, f, None, raised=True)

    @staticmethod
    def _state_continued_then_refused(m: SubagentManager, f: SubagentInfo) -> None:
        TestSuccessorClaim._state_continued(m, f)
        assert m._claim_continuation(f.id, "x", "")[1] is None
        busy = SubagentInfo(id="x", task="t", done=True, error="conversation_busy: busy")
        m._settle_continuation(f.id, f, busy)

    @staticmethod
    def _state_queued_continuation(m: SubagentManager, f: SubagentInfo) -> None:
        m._queue.append({"_preassigned_id": "cont0001", "conversation_key": f"subagent:{f.id}"})

    @staticmethod
    def _state_live_continuation(m: SubagentManager, f: SubagentInfo) -> None:
        m._agents["cont0001"] = SubagentInfo(
            id="cont0001", task="t", conversation_key=f"subagent:{f.id}"
        )

    @staticmethod
    def _state_finished_continuation(m: SubagentManager, f: SubagentInfo) -> None:
        m._agents["cont0001"] = SubagentInfo(
            id="cont0001", task="t", done=True, conversation_key=f"subagent:{f.id}"
        )

    @pytest.mark.parametrize(
        ("state", "retry_granted", "continue_granted"),
        [
            ("none", True, True),
            ("retry_starting", False, False),
            ("retried", False, False),
            ("retry_start_failed", True, True),
            ("retry_start_raised", False, False),
            ("continuation_starting", False, False),
            ("continued", False, True),
            ("continuation_start_failed", True, True),
            ("continuation_start_raised", False, True),
            ("continued_then_refused", False, True),
            ("queued_continuation", False, True),
            ("live_continuation", False, True),
            ("finished_continuation", False, True),
        ],
    )
    def test_every_claim_state_answers_retry_and_continue(
        self, state: str, retry_granted: bool, continue_granted: bool
    ) -> None:
        for op in ("retry", "continue"):
            manager = _manager()
            failed = self._failed(manager)
            getattr(self, f"_state_{state}")(manager, failed)
            if op == "retry":
                assert (manager.claim_retry(failed) == "") is retry_granted, op
            else:
                refusal = manager._claim_continuation(failed.id, "x", "")[1]
                assert (refusal is None) is continue_granted, op
                if refusal is not None:
                    assert refusal.error.startswith("conversation_busy")

    def test_a_continued_run_stays_claimed_after_its_successor_leaves(self) -> None:
        manager = _manager()
        failed = self._failed(manager)
        self._state_continued(manager, failed)
        manager._agents.pop("cont0001", None)
        assert manager.claim_retry(failed) == "cont0001"

    @pytest.mark.asyncio
    async def test_retry_is_refused_while_a_continuation_start_is_in_flight(self) -> None:
        """The continuation claims before its first await, so a retry landing
        while its durable accept is still pending sees the claim."""
        manager = _manager()
        failed = self._failed(manager)
        gate = asyncio.Event()
        seen: list[str] = []

        async def slow_continue(*_a, **_k):
            seen.append(manager.claim_retry(failed))
            # flake-ok: the stub parks on an event the test body sets after its claim check
            await gate.wait()
            return SubagentInfo(id="cont0001", task="t")

        with patch.object(
            ContinuationCoordinator, "continue_conversation_async_impl", new=slow_continue
        ):
            task = asyncio.ensure_future(manager.continue_conversation_async("fail1234", "x"))
            # flake-ok: zero-delay yield lets the continuation claim before the retry check
            await asyncio.sleep(0)
            assert manager.claim_retry(failed) != ""
            gate.set()
            # flake-ok: awaits the continuation the line above released; moved verbatim
            await task
        assert seen and seen[0] != ""
        assert manager.claim_retry(failed) == "cont0001"

    @pytest.mark.asyncio
    async def test_a_continuation_start_that_raises_keeps_the_claim(self) -> None:
        manager = _manager()
        failed = self._failed(manager)

        async def raises(*_a, **_k):
            raise asyncio.CancelledError

        with (
            patch.object(ContinuationCoordinator, "continue_conversation_async_impl", new=raises),
            pytest.raises(asyncio.CancelledError),
        ):
            await manager.continue_conversation_async("fail1234", "x")
        assert manager.claim_retry(failed) == SUCCESSOR_UNKNOWN
        assert manager._claim_continuation("fail1234", "x", "")[1] is None

    @pytest.mark.asyncio
    async def test_second_continuation_is_refused_while_the_first_is_starting(self) -> None:
        manager = _manager()
        self._failed(manager)
        gate = asyncio.Event()
        starts: list[str] = []

        async def slow_continue(*_a, **_k):
            starts.append("x")
            # flake-ok: the stub parks on an event the test body sets once the second call returns
            await gate.wait()
            return SubagentInfo(id="cont0001", task="t")

        with patch.object(
            ContinuationCoordinator, "continue_conversation_async_impl", new=slow_continue
        ):
            first = asyncio.ensure_future(manager.continue_conversation_async("fail1234", "a"))
            # flake-ok: zero-delay yield lets the first continuation claim; moved verbatim
            await asyncio.sleep(0)
            second = await manager.continue_conversation_async("fail1234", "b")
            gate.set()
            # flake-ok: awaits the continuation the line above released; moved verbatim
            await first
        assert second is not None and second.done
        assert second.error.startswith("conversation_busy")
        assert starts == ["x"]


class TestContinuationClaimWithoutRegistryRecord:
    """A continuation start is claimed by its conversation id, which the manager
    holds apart from the opening run's registry record. A conversation whose opening
    run is not in the registry (evicted after it finished, or from before a restart),
    or whose record is evicted while a start is in flight, stays claimed, so two
    concurrent continuations cannot both pass the conversation-busy lookup before
    either run is registered."""

    @staticmethod
    async def _conversation(conv_id: str) -> None:
        await asyncio.to_thread(create_agent_folder, conv_id, task="original")
        await asyncio.to_thread(write_run_agent, conv_id, "")

    @staticmethod
    def _held_manager() -> tuple[SubagentManager, asyncio.Event]:
        sessions = _mock_sessions(resumed=True)
        release = asyncio.Event()
        provider, _, _ = sessions.get_or_create.return_value

        async def _held_get_or_create(*_a, **_k):
            await asyncio.wait_for(release.wait(), timeout=30)
            return provider, True, True

        sessions.get_or_create.side_effect = _held_get_or_create
        return _manager(sessions), release

    @staticmethod
    async def _drain(manager: SubagentManager, infos) -> None:
        for info in infos:
            task = manager._tasks.get(info.id) if info is not None else None
            if task is not None:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)

    @pytest.mark.asyncio
    async def test_two_concurrent_continuations_start_one_run(self) -> None:
        await self._conversation("evicted1")
        manager, release = self._held_manager()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    manager.continue_conversation_async("evicted1", "first follow-up"),
                    manager.continue_conversation_async("evicted1", "second follow-up"),
                ),
                timeout=30,
            )
            started = [r for r in results if r is not None and not r.error]
            refused = [r for r in results if r is not None and r.error]
            assert len(started) == 1, f"started {len(started)} runs on one conversation"
            assert len(refused) == 1 and refused[0].error.startswith("conversation_busy: ")
            assert "is starting" in refused[0].error
        finally:
            release.set()
            await self._drain(manager, results)

    @pytest.mark.asyncio
    async def test_one_continuation_starts_and_releases_the_claim(self) -> None:
        await self._conversation("evicted2")
        manager = _manager(_mock_sessions(resumed=True))
        info = await manager.continue_conversation_async("evicted2", "follow up")
        assert info is not None and not info.error, info and info.error
        await self._drain(manager, [info])

    @pytest.mark.asyncio
    async def test_a_continuation_after_the_first_finishes_starts(self) -> None:
        await self._conversation("evicted3")
        manager = _manager(_mock_sessions(resumed=True))
        first = await manager.continue_conversation_async("evicted3", "first")
        assert first is not None and not first.error
        await self._drain(manager, [first])
        second = await manager.continue_conversation_async("evicted3", "second")
        assert second is not None and not second.error, second and second.error
        await self._drain(manager, [second])

    @pytest.mark.parametrize("failure", [RuntimeError("store down"), asyncio.CancelledError()])
    @pytest.mark.asyncio
    async def test_a_start_that_raises_releases_the_claim(self, failure) -> None:
        await self._conversation("evicted4")
        manager = _manager(_mock_sessions(resumed=True))

        async def raises(*_a, **_k):
            raise failure

        with (
            patch.object(ContinuationCoordinator, "continue_conversation_async_impl", new=raises),
            pytest.raises(type(failure)),
        ):
            await manager.continue_conversation_async("evicted4", "x")
        # Not refused as "is starting": the failed start released its claim.
        retry = await manager.continue_conversation_async("evicted4", "retry")
        assert retry is not None and not retry.error, retry and retry.error
        await self._drain(manager, [retry])

    @pytest.mark.asyncio
    async def test_the_claim_leaves_the_busy_lookup_unchanged(self) -> None:
        # Release and the retention sweep read ``_conversation_busy``; the claim
        # does not change what they see while a start is in flight.
        manager = _manager()
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def slow_continue(*_a, **_k):
            entered.set()
            await asyncio.wait_for(gate.wait(), timeout=30)
            return SubagentInfo(id="cont0002", task="t")

        with patch.object(
            ContinuationCoordinator, "continue_conversation_async_impl", new=slow_continue
        ):
            task = asyncio.ensure_future(manager.continue_conversation_async("evicted5", "x"))
            # The start is in flight, past its claim: the lookup below is read during it.
            await asyncio.wait_for(entered.wait(), timeout=30)
            assert manager._conversation_busy("subagent:evicted5") is None
            gate.set()
            await asyncio.wait_for(task, timeout=30)

    def test_a_registered_run_is_claimed_by_its_conversation_id(self) -> None:
        # A registered opening run is claimed by its conversation id and by its flag,
        # which ``claim_retry`` reads. Evicting the record while the start is in
        # flight leaves the id claim in place, and settling the start ends both.
        manager = _manager()
        original = SubagentInfo(id="registered1", task="t", done=True)
        manager._agents["registered1"] = original
        assert manager._claim_continuation("registered1", "x", "")[1] is None
        assert original._continuation_starting
        assert evict_completed_agents(manager._agents, max_retained=0) == 1
        second = manager._claim_continuation("registered1", "y", "")[1]
        held = second is not None and "is starting" in str(second.error)
        assert held, "a second continuation was granted after the claimed record was evicted"
        manager._settle_continuation("registered1", original, None)
        assert not original._continuation_starting
        assert manager._claim_continuation("registered1", "z", "")[1] is None

    @pytest.mark.asyncio
    async def test_a_record_evicted_during_the_first_start_refuses_the_second(
        self, monkeypatch
    ) -> None:
        # The opening run is registered when the first continuation claims it. While
        # that start waits in ``spawn_async``, before its own run is registered,
        # another run's finalization evicts the finished record.
        await self._conversation("evicted6")
        manager, release = self._held_manager()
        manager._agents["evicted6"] = SubagentInfo(id="evicted6", task="original", done=True)
        entered = asyncio.Event()
        gate = asyncio.Event()
        spawn_async = manager.spawn_async

        async def held_spawn_async(**kwargs):  # type: ignore[no-untyped-def]
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(gate.wait(), timeout=30)
            return await spawn_async(**kwargs)

        monkeypatch.setattr(manager, "spawn_async", held_spawn_async)
        first = asyncio.ensure_future(manager.continue_conversation_async("evicted6", "first"))
        results: list[SubagentInfo | None] = []
        try:
            await asyncio.wait_for(entered.wait(), timeout=30)
            assert evict_completed_agents(manager._agents, max_retained=0) == 1
            second = await asyncio.wait_for(
                manager.continue_conversation_async("evicted6", "second"), timeout=30
            )
            results.append(second)
            assert second is not None, "the second continuation returned no result"
            refused = second.done and bool(second.error)
            assert refused, f"a second run started on one conversation: run {second.id}"
            assert second.error.startswith("conversation_busy: ")
            assert "is starting" in second.error
            gate.set()
            started = await asyncio.wait_for(first, timeout=30)
            results.append(started)
            assert started is not None and not started.error, started and started.error
        finally:
            gate.set()
            release.set()
            ended = await asyncio.wait_for(
                asyncio.gather(first, return_exceptions=True), timeout=30
            )
            await self._drain(
                manager, [*results, *(r for r in ended if isinstance(r, SubagentInfo))]
            )
        # The first start released its claim on settling, with the record gone.
        third = await manager.continue_conversation_async("evicted6", "third")
        assert third is not None and not third.error, third and third.error
        await self._drain(manager, [third])


class TestFollowUpOnAContinuationRun:
    """A follow-up queued on a continuation run continues the ROOT conversation the run
    executed on, so it shares that conversation's busy check and claim."""

    ROOT = "rootconv"

    @staticmethod
    async def _until(predicate) -> bool:  # type: ignore[no-untyped-def]
        """True once ``predicate`` holds; a hang fails by name at the helper's lost-run bound."""
        return bool(await async_wait_until(predicate))

    async def _setup(self, monkeypatch):  # type: ignore[no-untyped-def]
        for run_id in (self.ROOT, "cont1"):
            await asyncio.to_thread(create_agent_folder, run_id, task="t", memory_mode="persistent")
            await asyncio.to_thread(write_run_agent, run_id, "")
        sessions = _mock_sessions(resumed=True)
        release = asyncio.Event()
        provider, _, _ = sessions.get_or_create.return_value

        async def _held(*_a, **_kw):  # type: ignore[no-untyped-def]
            await asyncio.wait_for(release.wait(), timeout=30)
            return provider, True, True

        sessions.get_or_create.side_effect = _held
        manager = _manager(sessions)
        monkeypatch.setattr(type(manager), "_FOLLOWUP_POLL_SECS", 0.01)
        monkeypatch.setattr(type(manager), "_FOLLOWUP_BUSY_RETRIES", 2)
        monkeypatch.setattr(type(manager), "_FOLLOWUP_BUSY_RETRY_SECS", 0.01)
        cont1 = SubagentInfo(
            id="cont1",
            task="first follow-up",
            parent_session_key="dashboard:chat-1",
            conversation_key=f"subagent:{self.ROOT}",
        )
        manager._agents[cont1.id] = cont1
        assert await manager.follow_up_run(cont1.id, "fix what you just did") == (True, "queued")
        dispatches: list[tuple[str, str]] = []
        original = manager.continue_conversation_async

        async def _recording(conv_id, task, **kw):  # type: ignore[no-untyped-def]
            child = await original(conv_id, task, **kw)
            dispatches.append((conv_id, str(getattr(child, "error", "") or "")))
            return child

        monkeypatch.setattr(manager, "continue_conversation_async", _recording)
        teardown = asyncio.get_running_loop().create_future()
        manager._tasks[cont1.id] = teardown
        cont1.done = True
        return manager, release, cont1, dispatches, original, teardown

    @staticmethod
    async def _drain(manager: SubagentManager, release: asyncio.Event) -> None:
        release.set()
        for a in list(manager._agents.values()):
            task = manager._tasks.get(a.id)
            if task is not None:
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=30)

    @pytest.mark.asyncio
    async def test_a_follow_up_is_refused_busy_while_the_root_conversation_runs(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, original, teardown = await self._setup(monkeypatch)
        try:
            # Told cont1 completed, the parent continues the root conversation.
            second = await asyncio.wait_for(original(self.ROOT, "second follow-up"), timeout=30)
            assert second is not None and not second.error, f"second continuation refused: {second}"
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: bool(dispatches) and not cont1._followup_watcher)
            assert dispatches[-1][0] == self.ROOT, dispatches
            assert dispatches[-1][1].startswith("conversation_busy"), (
                f"the follow-up was accepted while run {second.id} is in flight on the root "
                f"conversation: {dispatches}"
            )
        finally:
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_follow_up_on_an_idle_conversation_still_runs(self, monkeypatch) -> None:
        manager, release, cont1, dispatches, _original, teardown = await self._setup(monkeypatch)
        try:
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: bool(dispatches) and not cont1._followup_watcher)
            # Control: with nothing else in flight the follow-up is dispatched and starts.
            assert dispatches[-1][1] == "", dispatches
        finally:
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_retry_of_a_failed_run_whose_follow_up_started_is_refused(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, _original, teardown = await self._setup(monkeypatch)
        try:
            cont1.error = "timeout"
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: bool(dispatches) and not cont1._followup_watcher)
            assert dispatches[-1] == (self.ROOT, ""), dispatches
            owner = manager.claim_retry(cont1)
            assert owner, f"a dashboard retry of {cont1.id} was granted while its follow-up runs"
        finally:
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_follow_up_of_a_failed_run_already_retried_is_refused(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, _original, teardown = await self._setup(monkeypatch)
        try:
            cont1.error = "timeout"
            assert manager.claim_retry(cont1) == ""  # the dashboard retry comes first
            manager.settle_retry(cont1, "retry1")
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: not cont1._followup_watcher)
            started = [conv for conv, err in dispatches if not err]
            assert not started, f"the follow-up started although {cont1.id} was retried: {started}"
        finally:
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_follow_up_keeps_its_claim_when_the_run_record_is_evicted(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, _original, teardown = await self._setup(monkeypatch)
        entered = asyncio.Event()
        gate = asyncio.Event()
        spawn_async = manager.spawn_async

        async def held_spawn_async(**kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            await asyncio.wait_for(gate.wait(), timeout=30)
            return await spawn_async(**kwargs)

        monkeypatch.setattr(manager, "spawn_async", held_spawn_async)
        watcher = None
        try:
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            # The follow-up claimed cont1 and its dispatch on the root waits in
            # spawn_async when another run's finalization evicts cont1's record.
            await asyncio.wait_for(entered.wait(), timeout=30)
            watcher = manager._followup_watchers.get(cont1.id)
            assert evict_completed_agents(manager._agents, max_retained=0) == 1
            refusal = manager._claim_continuation(cont1.id, "x", "")[1]
            held = refusal is not None and "is starting" in str(refusal.error)
            assert held, f"a continuation of {cont1.id} was granted while its follow-up starts"
            gate.set()
            assert await self._until(lambda: bool(dispatches) and not cont1._followup_watcher)
            assert dispatches[-1] == (self.ROOT, ""), dispatches
            # The settled follow-up ended the claim it took on cont1.
            assert manager._claim_continuation(cont1.id, "y", "")[1] is None
        finally:
            gate.set()
            if watcher is not None:
                await asyncio.wait_for(asyncio.gather(watcher, return_exceptions=True), timeout=30)
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_refused_follow_up_leaves_the_holders_claim_on_the_run(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, _original, teardown = await self._setup(monkeypatch)
        try:
            # A continuation of cont1 by its own id holds the claim when the follow-up fires.
            assert manager._claim_continuation(cont1.id, "x", "")[1] is None
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: not cont1._followup_watcher)
            assert not dispatches, dispatches
            refusal = manager._claim_continuation(cont1.id, "y", "")[1]
            held = refusal is not None and "is starting" in str(refusal.error)
            assert held, f"the refused follow-up released the claim held on {cont1.id}"
        finally:
            await self._drain(manager, release)

    @pytest.mark.asyncio
    async def test_a_cancel_during_the_busy_retry_sleep_leaves_the_run_retryable(
        self, monkeypatch
    ) -> None:
        manager, release, cont1, dispatches, original, teardown = await self._setup(monkeypatch)
        # One busy refusal, then a long sleep before the next attempt.
        monkeypatch.setattr(type(manager), "_FOLLOWUP_BUSY_RETRY_SECS", 30.0)
        try:
            # A second continuation of the root stays in flight (its session start is
            # held), so the follow-up's dispatch on the root is refused busy.
            second = await asyncio.wait_for(original(self.ROOT, "second follow-up"), timeout=30)
            assert second is not None and not second.error, second
            cont1.error = "timeout"
            manager._tasks.pop(cont1.id, None)
            teardown.set_result(None)
            assert await self._until(lambda: bool(dispatches)), "the follow-up never dispatched"
            assert dispatches[-1][1].startswith("conversation_busy"), dispatches
            # A parent teardown cancels the watcher in its retry sleep: no start is in flight.
            watcher = manager._followup_watchers.get(cont1.id)
            assert watcher is not None and not watcher.done(), "the watcher is not in its sleep"
            watcher.cancel()
            await asyncio.wait_for(asyncio.gather(watcher, return_exceptions=True), timeout=30)
            assert not [conv for conv, err in dispatches if not err], dispatches
            owner = manager.claim_retry(cont1)
            try:
                assert owner == "", (
                    f"no follow-up started, yet a dashboard retry of {cont1.id} is refused as "
                    f"already picked up by {owner!r}"
                )
            finally:
                manager.settle_retry(cont1, None)
        finally:
            await self._drain(manager, release)
