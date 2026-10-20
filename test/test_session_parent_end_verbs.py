"""A parent's end cancels its sub-agent runs: the ending verbs.

Among them: ``remove``, ``destroy`` and ``discard_conversation``, the owned continuation, the
``remove_if_unclaimed`` reclaim, the identity sweep and sign-out, ``close_all`` (which leaves the
cancel to the sub-agent manager), the bounded teardown wait, a failing cancel that never blocks the
parent end, a failing provider shutdown that still ends the children, and a failing childless
retire shutdown that warns and leaves the key. The teardown rules beyond the verbs are in
``TestParentEndCancelsItsChildrenTeardownRules`` (test_session_parent_end_teardown.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    _TestParentEndCancelsItsChildrenHarness,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until


class TestParentEndCancelsItsChildren(_TestParentEndCancelsItsChildrenHarness):
    """A parent that ends takes its sub-agent runs with it, on every backend.

    Releasing the companion runtime already ends the children of a harness that
    multiplexes them onto one process — killing that process is what ends them,
    so it is a side effect rather than a decision. A harness that runs one
    process per child has no entry in ``_subagent_runtimes``, so the release
    reaches nothing and its children outlive the conversation that asked for
    them, each holding an agent process and that process's MCP fleet until its
    own run timeout expires. The lifecycle asks the manager to cancel at every
    site that releases the runtime, which makes the two harness shapes agree
    without either being named.
    """

    @pytest.mark.asyncio
    async def test_an_end_cancels_children_without_a_companion_runtime(self, cfg):
        """The cancel sits OUTSIDE the ``_subagent_runtimes`` membership guard.

        That guard is the kiro-shaped condition: a per-process harness never
        appears in it, and its children are exactly the ones that would
        otherwise survive.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        assert "dashboard:chat-9" not in mgr._subagent_runtimes
        await mgr.destroy("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_parent_end_cancels_owned_followup_without_recreating_session(self, cfg):
        """A queued continuation cannot outlive the parent that accepted it."""
        from kiro_crew.subagent import SubagentInfo
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        parent = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create(parent)

        watcher_started = asyncio.Event()
        release_watcher = asyncio.Event()
        dispatch = AsyncMock()

        async def _dispatch_followup():
            watcher_started.set()
            # flake-ok: parks the continuation watcher until remove cancels it; the finally sets the event and cancels the task so it cannot outlive the test; moved verbatim
            await release_watcher.wait()
            await dispatch()
            await mgr.get_or_create(parent)

        info = SubagentInfo(
            id="child-with-followup",
            task="original task",
            agent="default",
            parent_session_key=parent,
        )
        info.done = True
        info._reported_to_parent = True
        info.pending_followups = ["continue after the parent ends"]
        info._followup_watcher = True
        watcher = asyncio.create_task(_dispatch_followup())
        cancel_reasons: list[str] = []
        audited: list[tuple[str, str]] = []

        class _Children:
            def __init__(self):
                self._agents = {info.id: info}
                self._queue = []
                self._undurable_in_dispatch = {}
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {info.id: watcher}
                self._followup_watcher_parents = {info.id: parent}
                self._followup_watcher_infos = {info.id: info}

            def _audit_followup(self, owned, outcome):
                audited.append((owned.id, outcome))

            def _cancel_task_intentionally(self, task, owned=None, *, reason):
                cancel_reasons.append(reason)
                task.cancel()

        children = _Children()
        coordinator = CancellationCoordinator(children)  # type: ignore[arg-type]

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key):
                return coordinator.snapshot_teardown_children_impl(parent_session_key)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key="",
                verb="",
            ):
                return await coordinator.cancel_for_teardown_impl(
                    agent_ids,
                    parent_session_key=parent_session_key,
                    verb=verb,
                )

        mgr.set_child_teardown_handler(_Handler())
        # flake-ok: unbounded wait on watcher_started which the already-scheduled watcher task sets as its first statement so the next loop pass ends it; moved verbatim
        await watcher_started.wait()
        try:
            await mgr.remove(parent)
            release_watcher.set()
            # flake-ok: unbounded gather of the watcher task after remove cancels it and the release event is set; it has nothing left to block on; moved verbatim
            await asyncio.gather(watcher, return_exceptions=True)

            assert cancel_reasons == ["parent teardown cancelled owned follow-up"]
            assert info.pending_followups == []
            assert audited == [(info.id, "followup_suppressed")]
            assert children._followup_watchers == {}
            assert children._followup_watcher_parents == {}
            assert children._followup_watcher_infos == {}
            dispatch.assert_not_awaited()
            assert not mgr.has_session(
                parent
            ), "the queued follow-up rebuilt the conversation after parent teardown"
        finally:
            release_watcher.set()
            if not watcher.done():
                watcher.cancel()
            # flake-ok: the finally sets the release event and cancels the watcher if still running before this gather; it settles on its next step; moved verbatim
            await asyncio.gather(watcher, return_exceptions=True)
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_cancels_children(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_destroy_cancels_children(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.destroy("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_discard_conversation_cancels_children(self, cfg):
        """A fresh conversation under the same slot ends the old one's children."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.discard_conversation("dashboard:chat-9") is True

        assert seen == [("run-1",)]
        await mgr.close_all()

    @staticmethod
    async def _armed_unclaimed(mgr, key: str) -> None:
        """Open *key* as a removable speculative session.

        Removable only while its first turn is armed and unclaimed: the arm is a
        non-sentinel ``first_turn``, and unclaimed means nothing holds the
        session's semaphore.
        """
        await mgr.get_or_create(key)
        mgr.release(key)
        mgr._sessions[key].first_turn = object()

    @pytest.mark.asyncio
    async def test_remove_if_unclaimed_keeps_a_parent_with_a_live_child(self, cfg):
        """An idle prefetch parent whose child still runs is not reclaimed.

        The free semaphore proves only that the parent's own turn ended; the
        child runs on the parent's runtime, so a reclaim here cancels it.
        """
        key = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await self._armed_unclaimed(mgr, key)

        assert await mgr.remove_if_unclaimed(key) is False

        assert mgr.has_session(key)
        assert seen == []
        assert cb.fence_released == [key]  # type: ignore[attr-defined]
        await mgr.close_all()

    @pytest.mark.parametrize(
        "owes",
        [
            "pending_work",
            "unfinished_reports",
            "store_children",
            "fenced_children",
            "store_read_raises",
        ],
    )
    @pytest.mark.asyncio
    async def test_remove_if_unclaimed_keeps_a_parent_that_owes_work(self, cfg, owes):
        """Work the live-child probe cannot see still keeps the parent.

        ``pending_work``: a finished child whose report is not registered yet.
        ``unfinished_reports``: a registered report still delivering after its
        run's task ended (a reaped run's report waits across the reap's awaits).
        ``store_children``: a row only the task store holds. ``fenced_children``:
        a child admitted while the read ran. ``store_read_raises``: an unreadable
        store is read as attached, never as childless.
        """
        key = "dashboard:chat-9"
        kwargs: dict = {
            "pending_work": {key: True},
            "unfinished_reports": {key: True},
            "store_children": {key: True},
            "fenced_children": {key: True},
            "store_read_raises": True,
        }
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder({key: ()}, **{owes: kwargs[owes]})
        mgr.set_child_teardown_handler(cb)
        await self._armed_unclaimed(mgr, key)

        assert await mgr.remove_if_unclaimed(key) is False

        assert mgr.has_session(key)
        assert seen == []
        assert cb.fence_released == [key]  # type: ignore[attr-defined]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_if_unclaimed_keeps_a_parent_that_gains_work_during_the_read(self, cfg):
        """Work that appears while the off-loop read is suspended keeps the parent."""
        key = "dashboard:chat-9"
        owed: dict[str, bool] = {}
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder(
            {key: ()},
            pending_work=owed,
            on_store_probe=lambda k: owed.__setitem__(k, True),
        )
        mgr.set_child_teardown_handler(cb)
        await self._armed_unclaimed(mgr, key)

        assert await mgr.remove_if_unclaimed(key) is False

        assert mgr.has_session(key)
        assert seen == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_if_unclaimed_reclaims_a_parent_that_owes_nothing(self, cfg):
        key = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder({key: ()})
        mgr.set_child_teardown_handler(cb)
        await self._armed_unclaimed(mgr, key)

        assert await mgr.remove_if_unclaimed(key) is True

        assert not mgr.has_session(key)
        # The promoted fence is the committed one: only the selection half ran,
        # and no tentative fence is left to release.
        assert cb.fence_opened == [key]  # type: ignore[attr-defined]
        assert cb.fence_released == []  # type: ignore[attr-defined]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_spares_an_idle_parent_with_children(self, cfg):
        """An idle parent waiting on ``spawn_run`` children is NOT retired on the
        per-turn identity-change sweep (``spare_children=True``).

        It is idle by the semaphore test, so retiring it would cancel its children
        ("provider shutdown") on every turn any chat takes while the sweep stays
        incomplete. The sweep instead defers it the way a mid-turn session is
        deferred: kept in the map, flagged ``retire_on_identity_change``, and the
        sweep reports incomplete.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # The default recorder reports ("run-1",) for any key: this parent has a
        # live child.
        snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []
        assert seen == []  # the child was never cancelled
        # The spare decision uses the side-effect-free probe, not the teardown
        # snapshot: the snapshot arms the delivery gate and tears down follow-ups,
        # which would drop the very completions the sparing preserves.
        assert "dashboard:chat-9" in cb.probed
        assert snapshotted == []
        assert complete is False  # the sweep stays incomplete, as for a busy holder
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_spares_a_parent_with_only_a_store_child(self, cfg):
        """A parent whose only child is durable-only (held by the task store, no
        in-memory run) is deferred too, not retired.

        The in-memory probe answers "no child", but the retirement teardown would
        still cancel the store row. The sweep reads the store side off the loop
        (``has_pending_store_children``) before taking its locks and defers on a
        hit, so the decision matches what the teardown cancels.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # No in-memory child, but the store holds a waiting child for this key.
        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            store_children={"dashboard:chat-9": True},
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []
        assert seen == []  # the store child was never cancelled
        assert "dashboard:chat-9" in cb.store_probed  # the off-loop store read ran
        assert snapshotted == []
        assert complete is False
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_defers_when_the_store_read_fails(self, cfg):
        """An unreadable store defers the parent rather than retiring it.

        If the store read fails, the parent might own an accepted durable child;
        retiring it would cancel that child. The sweep treats a failed read as
        "unknown" and defers, the same fail-safe the sign-out and empty-fingerprint
        paths take.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # No in-memory child, and the store read raises.
        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            store_read_raises=True,
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []  # not retired on an unreadable store
        assert seen == []  # nothing cancelled
        assert "dashboard:chat-9" in cb.store_probed
        assert complete is False
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        await mgr.close_all()

    @staticmethod
    async def _identity_sweep_spares(mgr, cb, key: str, snapshotted, seen) -> None:
        """Run the spare-children sweep over the idle *key* and assert it deferred.

        A deferred parent keeps its session and its children's completion path:
        no teardown snapshot (the step that gates a child's report away from the
        parent), no cancel, the fence released, and the sweep incomplete and
        waiting on the parent's sub-agents.
        """
        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []
        assert snapshotted == []
        assert seen == []
        assert cb.fence_released == [key]  # type: ignore[attr-defined]
        assert complete is False
        assert f"{key} (subagents)" in mgr.identity_sweep_waiting_on
        assert mgr.has_session(key)
        assert mgr._sessions[key].retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_identity_retire_spares_a_parent_whose_child_report_is_delivering(self, cfg):
        """A finished child whose run task is gone but whose terminal report is
        still delivering keeps its parent through the spare-children sweep.

        The child is neither live nor queued, so the sparable-child read sees
        nothing; only the manager's unfinished-report reading does. Retiring the
        parent here would take the teardown snapshot that drops the report before
        it reaches the parent.
        """
        key = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder({key: ()}, unfinished_reports={key: True})
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create(key)
        mgr.release(key)

        await self._identity_sweep_spares(mgr, cb, key, snapshotted, seen)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_spares_a_parent_with_an_unregistered_finished_child(self, cfg):
        """A finished child whose report is not registered yet keeps its parent.

        The span before the report lands in the manager's report registry is the
        pending-work signal's (``has_pending_work_for_async``); the sweep asks it
        off the loop alongside the sparable-child read.
        """
        key = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder({key: ()}, pending_work={key: True})
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create(key)
        mgr.release(key)

        await self._identity_sweep_spares(mgr, cb, key, snapshotted, seen)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_spares_a_parent_whose_report_starts_during_the_read(self, cfg):
        """A report that begins delivering while the off-loop read is suspended is
        caught by the commit recheck under the registry lock."""
        key = "dashboard:chat-9"
        reporting: dict[str, bool] = {}
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder(
            {key: ()},
            unfinished_reports=reporting,
            on_store_probe=lambda k: reporting.__setitem__(k, True),
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create(key)
        mgr.release(key)

        await self._identity_sweep_spares(mgr, cb, key, snapshotted, seen)
        assert key in cb.store_probed  # type: ignore[attr-defined]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_spares_a_parent_whose_pending_work_starts_during_the_read(
        self, cfg
    ):
        """In-memory pending work that appears during the off-loop read keeps the
        parent at the commit recheck, the same as for the unclaimed removal."""
        key = "dashboard:chat-9"
        owed: dict[str, bool] = {}
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder(
            {key: ()},
            pending_work=owed,
            on_store_probe=lambda k: owed.__setitem__(k, True),
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create(key)
        mgr.release(key)

        await self._identity_sweep_spares(mgr, cb, key, snapshotted, seen)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_retires_a_deferred_parent_once_its_owed_work_ends(self, cfg):
        """The deferral lasts only as long as the work the parent is owed.

        Pending work (a live follow-up watcher, say) keeps the parent through one
        sweep. Once that work ends, the next sweep retires the parent, so an
        old-identity parent outlives its owed work by at most one sweep.
        """
        key = "dashboard:chat-9"
        owed = {key: True}
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder({key: ()}, pending_work=owed)
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create(key)
        mgr.release(key)

        await self._identity_sweep_spares(mgr, cb, key, snapshotted, seen)

        owed[key] = False
        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == [key]
        assert complete is True
        assert not mgr.has_session(key)
        assert mgr.identity_sweep_waiting_on == ()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_defers_on_a_child_admitted_during_the_teardown(self, cfg):
        """A child admitted through /api/spawn while the retire decision runs is
        seen via the teardown fence and the parent is deferred, not retired.

        The decision reads the store off the loop at the async teardown. The old
        preflight read could go stale: a /api/spawn admission landing after a
        negative read still cancelled the accepted child. Now the fence that
        admission records into (``note_teardown_store_accept``) is opened under the
        registry lock BEFORE the read, so an admission concurrent with the decision
        is in ``has_fenced_children`` and defers the parent -- no in-memory and no
        store row needed.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # No in-memory child, no pre-existing store row, but a child WAS admitted
        # into the fence since it opened (the concurrent /api/spawn).
        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            fenced_children={"dashboard:chat-9": True},
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []
        assert seen == []  # the admitted child was never cancelled
        assert "dashboard:chat-9" in cb.fence_opened  # fence opened under the lock
        assert snapshotted == []  # no cancel snapshot taken for a spared parent
        assert complete is False
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_defers_on_a_pre_existing_durable_child(self, cfg):
        """A durable-only child accepted BEFORE the fence opened is seen by the
        off-loop store read and the parent is deferred.

        The fence only records admissions since it opened; a child admitted
        earlier is only in the store. The async decision reads the store off the
        loop (``has_pending_store_children``) for exactly this case.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # No in-memory child, nothing in the fence, but a pre-existing store row.
        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            store_children={"dashboard:chat-9": True},
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []
        assert seen == []  # the durable child was never cancelled
        assert "dashboard:chat-9" in cb.store_probed  # the off-loop read ran
        assert snapshotted == []
        assert complete is False
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_defers_a_turn_that_starts_during_the_read(self, cfg):
        """A turn that takes the parent's semaphore during the off-loop read is
        deferred, not shut down mid-turn.

        Selection and the pop were one lock hold before the teardown redesign; the
        off-loop read reopened that gap. A turn from a path that does not go through
        the dashboard gate (a channel or cron) can take the semaphore in it, so the commit
        block re-checks ``semaphore.locked()`` under the re-taken lock and defers
        rather than ``provider.shutdown()`` a live turn. A deferred parent also
        drops its old-account sid, so its cold-started successor does not reload the
        previous account's conversation.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        # No child anywhere, so the parent would be retired -- except a turn takes
        # its semaphore during the off-loop read (simulated inside the probe).
        def _start_a_turn_during_read(key: str) -> None:
            if key == "dashboard:chat-9":
                # A turn claims the session: take its semaphore, as a non-dashboard
                # path would without clearing retire_on_identity_change.
                mgr._sessions["dashboard:chat-9"].semaphore._value = 0

        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            on_store_probe=_start_a_turn_during_read,
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")  # idle at selection time

        sid_cleared: list[str] = []
        orig_clear = mgr._session_map.clear_sid
        with (
            patch(
                "kiro_crew.session._provider_uses_kiro_identity_store",
                return_value=True,
            ),
            patch.object(
                mgr._session_map,
                "clear_sid",
                side_effect=lambda k: (sid_cleared.append(k), orig_clear(k))[1],
            ),
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []  # not retired: a turn was running
        assert seen == []  # nothing cancelled -- no shutdown of a live turn
        assert snapshotted == []
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        assert "dashboard:chat-9" in sid_cleared  # old-account sid dropped
        assert complete is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_defers_a_child_admitted_during_the_read(self, cfg):
        """A child admitted during the off-loop store read is seen by the commit
        block's re-check and the parent is deferred, not retired.

        ``_parent_has_sparable_child`` can read childless and then an ``/api/spawn``
        admission for the parent lands during the await, recording the child in the
        fence. Committing would snapshot and cancel that newly accepted child, so
        the commit block re-reads the in-memory and fence child checks under its
        lock and defers when a child appeared.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        # No child at the sparable read, but one is admitted into the fence during
        # the store probe (the concurrent /api/spawn). ``fenced`` is the dict the
        # recorder's ``has_fenced_children`` reads, so flipping it mid-read is seen
        # by the commit block's re-check.
        fenced: dict[str, bool] = {}

        def _admit_during_read(key: str) -> None:
            if key == "dashboard:chat-9":
                fenced["dashboard:chat-9"] = True

        snapshotted, seen, cb = self._recorder(
            {"dashboard:chat-9": ()},
            fenced_children=fenced,
            on_store_probe=_admit_during_read,
        )
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(spare_children=True)

        assert retired == []  # not retired: a child arrived during the read
        assert seen == []  # the newly admitted child was never cancelled
        assert snapshotted == []  # no cancel snapshot taken
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        assert complete is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_cancels_when_a_spawn_stamp_source_dropped(self, cfg):
        """A parent is spared only while every source it spawned under is still live.

        The retire baseline advances only on a COMPLETE sweep, so a credential
        source added and then removed inside an incomplete one is invisible to a
        baseline comparison: it compares two states that never held the source.
        The parent's OWN spawn stamp did hold it -- it is the identity its children
        loaded -- so the spare is gated on the stamp directly. Here the parent
        spawned under store + vault, then the store source is gone from live (an
        external logout with the vault still populated): a component of its spawn
        identity dropped, so the parent is retired and its child cancelled, not
        spared, exactly as the sign-out path does.
        """
        import kiro_crew.kiro_prerequisite as kp

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # The default recorder reports a live child for any key.
        snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        # The parent authenticated under store S AND the Crew vault V.
        spawn_stamp = kp._combine_identity_fingerprints("S", "V")
        mgr._sessions["dashboard:chat-9"].provider.spawn_identity = spawn_stamp
        # Live now holds only the vault: the store source dropped (external logout).
        live_vault_only = kp._combine_identity_fingerprints("", "V")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(
                fingerprint=live_vault_only, spare_children=True
            )

        # A dropped spawn-stamp source forces the cancel path even though the
        # parent has a live child and spare_children is True.
        assert retired == ["dashboard:chat-9"]
        assert seen == [("run-1",)]  # the child WAS cancelled
        assert snapshotted == ["dashboard:chat-9"]  # teardown snapshot taken
        assert complete is True
        assert "dashboard:chat-9" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_still_spares_when_the_spawn_stamp_is_whole(self, cfg):
        """The converse: when every spawn-stamp source is still present in live, a
        parent with live children is still spared (a plain two-account switch that
        adds nothing and drops nothing).
        """
        import kiro_crew.kiro_prerequisite as kp

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        # Spawned under store S + vault V; live still carries both (plus nothing
        # dropped). The provider is NOT spawned_under live (S2 != S), so it reaches
        # the spare decision rather than being spared as the live account.
        mgr._sessions["dashboard:chat-9"].provider.spawn_identity = (
            kp._combine_identity_fingerprints("S", "V")
        )
        live_switched = kp._combine_identity_fingerprints("S2", "V")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions(
                fingerprint=live_switched, spare_children=True
            )

        # Nothing dropped from the spawn stamp (store changed, vault kept), so the
        # parent with a live child is spared.
        assert retired == []
        assert seen == []
        assert "dashboard:chat-9" in mgr._sessions
        assert mgr._sessions["dashboard:chat-9"].retire_on_identity_change is True
        assert complete is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_signout_retires_an_idle_parent_with_live_children(self, cfg):
        """Sign-out (the default ``spare_children=False``) retires an idle parent
        with live children and cancels them.

        ``_retire_runtimes_after_sign_out`` calls the sweep with no fingerprint and
        no ``spare_children``: the account is gone, so a parent and its children
        MUST be retired, or the children keep running on the signed-out account's
        in-memory credential. Only the per-turn identity-change sweep opts into the
        deferral; sign-out keeps the cancel path.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # The default recorder reports ("run-1",) for any key: this parent has a
        # live child the sign-out must cancel.
        snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions()

        assert retired == ["dashboard:chat-9"]  # retired, not spared
        assert seen == [("run-1",)]  # the child WAS cancelled
        assert snapshotted == ["dashboard:chat-9"]  # teardown snapshot taken
        assert complete is True
        assert "dashboard:chat-9" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_still_retires_an_idle_childless_parent(self, cfg):
        """A parent with no live children is still retired by the sweep."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # No child for this key, so nothing keeps the sweep from retiring it.
        _snapshotted, seen, cb = self._recorder({"dashboard:chat-9": ()})
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, _complete = await mgr.retire_kiro_identity_sessions()

        assert retired == ["dashboard:chat-9"]
        assert seen == [()]  # cancel invoked for the retired key, but with no run id
        assert "dashboard:chat-9" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_cancel_mid_teardown_still_drains_committed(self, cfg):
        """A cancel of the sweep after one retirement commits must still tear that
        committed parent down -- shutdown, child cancel, runtime release.

        Two idle childless candidates: the first (A) commits, the second (B) is
        still inside its off-loop store probe when the initiating chat closes and
        cancels the sweep. Before the fix A's teardown ran in a post-barrier loop
        OUTSIDE any finally, so B's cancellation skipped it and A's provider was
        never shut down -- its children ran on under the logged-out credential.
        Now every committed retirement is torn down in an always-run finally with
        shielded awaits, so A is fully retired even though the sweep is cancelled.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key_a, key_b = "dashboard:chat-a", "dashboard:chat-b"
        # Both childless in memory and in the fence, so both COMMIT at the teardown.
        snapshotted, seen, cb = self._recorder({key_a: ("run-a",), key_b: ()})

        entered_b = asyncio.Event()
        proceed = asyncio.Event()

        async def _blocking_store_probe(parent_session_key):
            # Block only B's store probe; A reads childless and commits first.
            if parent_session_key == key_b:
                entered_b.set()
                # flake-ok: parks the second key's store probe; the test cancels the sweep and sets the event once await_until sees it parked; moved verbatim
                await proceed.wait()
            return False

        cb.has_pending_store_children = _blocking_store_probe
        # A has no in-memory/fenced child either, so it commits; its snapshot names
        # an orphan run so we can see the child cancel ran.
        cb.has_live_or_queued_children = lambda k: False
        cb.snapshot_teardown_children_only = lambda k: ("run-a",) if k == key_a else ()
        mgr.set_child_teardown_handler(cb)
        for k in (key_a, key_b):
            await mgr.get_or_create(k)
            mgr.release(k)
        shut_a = mgr._sessions[key_a].provider.shutdown

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            task = asyncio.ensure_future(mgr.retire_kiro_identity_sessions(spare_children=True))
            # A has committed; the loop is now blocked inside B's store probe.
            await await_until(entered_b, what="the sweep blocked in B's store probe")
            task.cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: unbounded await of the just-cancelled sweep task with its probe released; the drain runs on mock providers; moved verbatim; tracked for a de-flake pass
                await task

        # Despite the cancel landing while B was probing: A was fully torn down.
        assert key_a not in mgr._sessions
        shut_a.assert_awaited()  # A's provider was shut down in the finally
        assert ("run-a",) in seen  # A's children were drained
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_cancel_mid_teardown_still_drains_stamp_dropped(self, cfg):
        """A cancel of the sweep must also tear down a STAMP-DROPPED parent.

        A parent whose spawn stamp lost a component (a partial logout) is retired
        synchronously under the lock into ``doomed`` -- the credential-loss case.
        Before this fix ``doomed`` was drained in a loop AFTER the try/finally, so a
        cancel during a spare candidate's store probe skipped it and left its
        children running on the logged-out credential. Both ``doomed`` and the
        committed spares are now drained in the same always-run shielded finally.
        """
        import kiro_crew.kiro_prerequisite as kp

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key_a, key_b = "dashboard:chat-a", "dashboard:chat-b"
        # A has a live child (its stamp dropping forces a synchronous retire into
        # doomed regardless), B is a childless spare candidate whose probe blocks.
        snapshotted, seen, cb = self._recorder({key_a: ("run-a",), key_b: ()})

        entered_b = asyncio.Event()
        proceed = asyncio.Event()

        async def _blocking_store_probe(parent_session_key):
            if parent_session_key == key_b:
                entered_b.set()
                # flake-ok: parks the spare candidate's store probe; the test cancels the sweep and sets the event once await_until sees it parked; moved verbatim
                await proceed.wait()
            return False

        cb.has_pending_store_children = _blocking_store_probe
        # B is childless so it reaches the committed spare path and its probe runs.
        cb.has_live_or_queued_children = lambda k: k == key_a
        mgr.set_child_teardown_handler(cb)
        for k in (key_a, key_b):
            await mgr.get_or_create(k)
            mgr.release(k)
        # A's spawn stamp carries a component the live fingerprint drops -> A is
        # retired synchronously into doomed (the fail-safe credential-loss path).
        mgr._sessions[key_a].provider.spawn_identity = kp._combine_identity_fingerprints("S", "V")
        live_dropped = kp._combine_identity_fingerprints("S", "")  # vault dropped
        shut_a = mgr._sessions[key_a].provider.shutdown

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            task = asyncio.ensure_future(
                mgr.retire_kiro_identity_sessions(fingerprint=live_dropped, spare_children=True)
            )
            await await_until(entered_b, what="the sweep blocked in B's store probe")
            task.cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                # flake-ok: unbounded await of the just-cancelled sweep task with its probe released; the drain runs on mock providers; moved verbatim; tracked for a de-flake pass
                await task

        # The stamp-dropped parent was fully torn down despite the cancel.
        assert key_a not in mgr._sessions
        shut_a.assert_awaited()
        assert ("run-a",) in seen  # A's children drained in the shielded finally
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_cancel_during_first_drain_still_drains_second(self, cfg):
        """A cancel landing on the FIRST entry's drain must still drain the rest.

        Two idle childless candidates (A, B) both commit at the teardown and land
        in ``committed_teardown``. The drain loop processes A first; the sweep is
        cancelled while A is shutting down (``shutdown`` raises ``CancelledError``,
        as it would when the chat closes mid-drain). Before the fix the loop's
        ``except asyncio.CancelledError: raise`` re-raised on A and left the loop,
        so B -- already unregistered and recorded ended -- never got ``shutdown``,
        ``_cancel_parent_children`` or ``release_subagent_runtime``, and its
        children kept running on the logged-out credential. Now the loop records
        the first ``CancelledError``, drains every remaining entry shielded, and
        re-raises after the loop: B is fully torn down AND ``CancelledError`` still
        propagates to the caller.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key_a, key_b = "dashboard:chat-a", "dashboard:chat-b"
        # Both childless in memory and in the fence, so both COMMIT at the teardown
        # and both are drained in the method-level finally.
        snapshotted, seen, cb = self._recorder({key_a: ("run-a",), key_b: ("run-b",)})
        cb.has_live_or_queued_children = lambda k: False
        cb.has_pending_store_children = AsyncMock(return_value=False)
        cb.snapshot_teardown_children_only = lambda k: {
            key_a: ("run-a",),
            key_b: ("run-b",),
        }.get(k, ())
        mgr.set_child_teardown_handler(cb)
        for k in (key_a, key_b):
            await mgr.get_or_create(k)
            mgr.release(k)

        # A is drained first; its shutdown raises CancelledError, modelling the
        # cancel landing on the first entry. B's provider shuts down normally.
        shut_a = mgr._sessions[key_a].provider.shutdown
        shut_b = mgr._sessions[key_b].provider.shutdown
        shut_a.side_effect = asyncio.CancelledError

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            with pytest.raises(asyncio.CancelledError):
                await mgr.retire_kiro_identity_sessions(spare_children=True)

        # Both parents were unregistered, and -- the fix -- B was fully drained
        # despite the cancel firing on A: its provider was shut down and its
        # children cancelled, rather than left running on the stale credential.
        assert key_a not in mgr._sessions
        assert key_b not in mgr._sessions
        shut_a.assert_awaited()
        shut_b.assert_awaited()
        assert ("run-b",) in seen  # B's children drained after the cancel on A
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_leaves_cancellation_to_cancel_all(self, cfg):
        """Gateway shutdown is deliberately not a per-key cancel.

        ``SubagentManager.cancel_all`` runs there instead: it also drains
        follow-up watchers and announces undelivered messages, which a per-key
        cancel does not do.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.close_all()

        assert seen == []

    @pytest.mark.asyncio
    async def test_a_failing_cancel_never_blocks_the_parent_end(self, cfg):
        """Best-effort, matching the recycle callback beside it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        class _Wedged:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                raise RuntimeError("subagent manager is wedged")

        mgr.set_child_teardown_handler(_Wedged())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_teardown_does_not_report_the_run_into_the_retired_parent(self, cfg):
        """The parent-end path routes through the verb that suppresses delivery.

        ``_on_done`` resolves the parent key through the session registry and
        injects, creating a session when none is live — so reporting a
        teardown-cancelled run rebuilds the conversation the teardown just took
        down and seeds it with that run's terminal text. The suppression rides on
        ``cancel_for_teardown``, so what this pins is that the lifecycle calls that
        verb and not the Stop-all one.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        teardown_calls: list[tuple[str, ...]] = []
        stop_all_calls: list[str] = []

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-7",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                teardown_calls.append(tuple(agent_ids))
                return len(tuple(agent_ids))

            async def cancel_for_parent(self, parent_session_key: str) -> tuple[int, int]:
                stop_all_calls.append(parent_session_key)
                return (0, 0)

        mgr.set_child_teardown_handler(_Handler())
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert teardown_calls == [("run-7",)]
        assert stop_all_calls == [], (
            "a parent end used the Stop-all verb, whose terminal report injects "
            "into the parent it just retired"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_removing_an_already_reset_parent_still_ends_its_children(self, cfg):
        """A reset pops the session; the tab close that follows is still a parent end.

        The two verbs make this sequence ordinary now that a reset keeps the
        conversation: reset recycles the process, then the user closes the tab and
        ``remove`` arrives with ``session is None``. Guarding the cancel on a live
        provider skipped exactly that case and left the children running.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True
        assert cancelled == [], "the reset itself ended the children"

        await mgr.remove("dashboard:chat-9")

        assert cancelled == [
            ("run-1",)
        ], "removing a parent whose session was already popped left its children running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_wait_expires_without_cancelling_the_reap(self, cfg):
        """The timeout bounds the parent's WAIT, not the child reap.

        A bare ``wait_for`` cancels what it waits on, and this coroutine kills child
        processes: a long child reset would have its reap cancelled after the marks were
        written and before the kills landed, leaving a write-capable child executing
        against a conversation that has ended.
        """
        import asyncio as _asyncio

        finished = _asyncio.Event()
        observed: list[str] = []

        class _SlowHandler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                try:
                    # flake-ok: slow reap double sleeps 0.25 s so the patched 0.01 s parent wait expires first; the test awaits the reap's finished event; moved verbatim
                    # nosemgrep: kirocrew.test-sleep-as-barrier -- the slow reap double that must outlast the patched 0.01 s parent wait; the test then awaits its finished event; moved verbatim
                    await _asyncio.sleep(0.25)
                    observed.append("completed")
                except _asyncio.CancelledError:
                    observed.append("cancelled")
                    raise
                finally:
                    finished.set()
                return 1

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_child_teardown_handler(_SlowHandler())
        await mgr.get_or_create("dashboard:chat-9")

        # flake-ok: shrinks the parent's shielded child-cancel wait to 0.01 s so it expires before the 0.25 s reap double; expiry never cancels the reap; moved verbatim
        with patch("kiro_crew.session_lifecycle._CHILD_CANCEL_TIMEOUT_SECS", 0.01):
            await mgr.remove("dashboard:chat-9")

        await await_until(finished, what="the slow child reap settling")
        assert observed == ["completed"], (
            "the parent's wait expiring cancelled the reap itself, so a child could "
            f"outlive the teardown mid-kill: {observed}"
        )
        await mgr.close_all()

    @pytest.mark.parametrize(
        "verb",
        [
            "reset",
            "remove",
            "destroy",
            "discard_conversation",
            "remove_if_unclaimed",
            "retire_kiro_identity_sessions",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_failing_provider_shutdown_still_ends_the_children(self, cfg, verb):
        """A shutdown that raises must not carry the exception past the cancel.

        At every one of these sites the parent has already been retired from the session
        map by the time the provider is asked to stop, so an exception leaving the method
        early leaves children running with no parent to report to and the runtime they
        share still held. It is the one outcome these verbs exist to prevent, reached by
        the one path nobody exercises -- which is why it is parametrized over every site
        rather than demonstrated once.

        ``reset`` re-raises after the cancel and the rest propagate; none may lose the
        children. ``retire_kiro_identity_sessions`` runs on its default
        ``spare_children=False`` (the sign-out / external-logout path): the deferral is
        gated behind ``spare_children``, so this path retires an idle parent even with
        live children and must still cancel them when the shutdown raises.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        session = mgr._sessions["dashboard:chat-9"]

        async def _boom():
            raise RuntimeError("provider shutdown failed")

        session.provider.shutdown = _boom  # type: ignore[method-assign]

        # Each verb needs the precondition that makes it act at all, or the test proves
        # only that a method returned early. These are the same setups the per-verb
        # cancel tests above use.
        if verb == "remove_if_unclaimed":
            # Removable only while the first turn is armed and unclaimed: a non-sentinel
            # ``first_turn``, and nothing holding the semaphore.
            mgr.release("dashboard:chat-9")
            mgr._sessions["dashboard:chat-9"].first_turn = object()
            # The reclaim keeps a parent whose child is live, so the probe answers
            # "nothing owed" while the snapshot still names the run to cancel.
            handler.has_live_or_queued_children = lambda _key: False  # type: ignore[method-assign]

        if verb == "retire_kiro_identity_sessions":
            # The sweep acts only on an idle (released) session whose provider uses the
            # kiro identity store, and on its default path (spare_children=False) it
            # retires the parent even though it has a live child -- so the failing
            # shutdown must still cancel that child.
            mgr.release("dashboard:chat-9")

        if verb == "reset":
            await_call = mgr.reset("dashboard:chat-9", ends_conversation=True)
        elif verb == "retire_kiro_identity_sessions":
            await_call = None
        else:
            await_call = getattr(mgr, verb)("dashboard:chat-9")

        if verb == "retire_kiro_identity_sessions":
            with patch(
                "kiro_crew.session._provider_uses_kiro_identity_store",
                return_value=True,
            ):
                with contextlib.suppress(RuntimeError):
                    await mgr.retire_kiro_identity_sessions()
        else:
            with contextlib.suppress(RuntimeError):
                await await_call

        assert cancelled == [("run-1",)], (
            f"{verb}: a failing provider shutdown skipped the child cancel, so the "
            f"children outlived the parent: {cancelled}"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failing_retire_shutdown_warns_and_leaves_the_key(self, cfg):
        """A childless retire whose provider shutdown raises becomes a warning.

        This exercises the retire path with no child present (distinct from the
        parametrized case above, which proves a child IS cancelled on a failing
        shutdown). The contract: a shutdown that raises is turned into a warning and
        the key is left unretired, rather than propagating out of the sweep.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Childless, so the sweep retires it and reaches the shutdown path.
        _snapshotted, cancelled, handler = self._recorder({"dashboard:chat-9": ()})
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        async def _boom():
            raise RuntimeError("provider shutdown failed")

        mgr._sessions["dashboard:chat-9"].provider.shutdown = _boom  # type: ignore[method-assign]

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, complete = await mgr.retire_kiro_identity_sessions()

        # The failing shutdown is swallowed into a warning: the key is not reported
        # retired, and the sweep reports incomplete.
        assert retired == []
        assert complete is False
        assert cancelled == [()]  # cancel still runs in the finally, with no run id
        await mgr.close_all()
