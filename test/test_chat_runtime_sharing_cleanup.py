"""Chat runtime sharing: cleanup, the turn gate and the start-path placement.

Split out of ``test_chat_runtime_sharing.py`` for the file line cap; the shared
fakes (``FakeRuntime``, ``spawner``, ``a_key`` and the shutdown fakes) stay there
and are imported from it. What is tested HERE: a failed or reset start never
kills a co-tenant's process, chat turns on a shared process take the gate one at
a time, and the placement on the start path.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from chat_runtime_sharing_helpers import _PlacementProvider, confirming, make_placement
from test_chat_runtime_sharing import (
    FakeHandle,
    FakeRuntime,
    KillableRuntime,
    a_key,
    shared_provider,
    spawner,
)

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.runtime_ownership import RuntimeOwnership, authorize_runtime_kill
from kiro_crew.testing.wait import async_wait_until

# ── What a joining session must not do ──


class TestAFailedStartReleasesBeforeItKills:
    """Where a cleanup owes a release, given that the lease precedes the spawn.

    An eligible chat-share start takes its lease before the spawn, so the window
    between ``start()`` returning and registration holds one. A cancellation
    landing in that window -- the spawn-identity stamp awaits a reader, a real
    suspension point -- reaches a cleanup arm that kills. A hard-kill site earlier
    than the start holds no lease and needs none of this.
    """

    def test_the_gate_refuses_a_kill_while_the_lease_is_outstanding(self):
        """Why killing without releasing does not merely leak the process.

        The gate refuses, so the signal never goes out, and the reconciler's
        unowned sweep skips a leased pid -- nothing reclaims it later.
        """
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9400)
            live = ro.RUNTIME_OWNERSHIP
            acq = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))

            assert (
                authorize_runtime_kill(rt, reason="failed start", caller="test") is False
            ), "a kill with the lease still held must be refused"

            assert asyncio.run(live.release(acq.lease)) is rt, "the last release strands it"
            assert (
                authorize_runtime_kill(rt, reason="failed start", caller="test") is True
            ), "released first, the same kill is authorized"
        finally:
            ro._reset_for_tests()

    def test_the_post_start_cleanup_releases_before_it_kills(self):
        """Structural, and deliberately ORDER-sensitive.

        Driving the real arm needs a live registry, a real spawn and an identity
        reader that raises mid-await. What a later edit breaks is the order and the
        shielding, both of which read off the source.
        """
        import re
        from pathlib import Path

        import kiro_crew.session_allocation_parts.get_or_create as alloc_mod

        source = Path(alloc_mod.__file__).read_text(encoding="utf-8")
        # Anchored on the comment that names THIS arm: the stamp is the one
        # suspension point past a chat-share acquisition.
        anchor = "suspension point between start() and PID registration"
        assert source.count(anchor) == 1, "the post-start stamp arm's anchor moved"
        start = source.index(anchor)
        end = re.search(r"^[ \t]*raise[ \t]*$", source[start:], re.MULTILINE)
        assert end is not None, "the post-start stamp arm no longer re-raises"
        region = source[start : start + end.start()]
        assert "await _end_started_provider(owner, provider)" in region, (
            "the post-start cleanup no longer ends a shared provider through its "
            "own shutdown, so a failed shared start leaves its session resident"
        )
        # And the helper: shutdown first, shielded through a cancellation, the
        # hard kill only when the shutdown itself raised.
        helper = source[source.index("async def _end_started_provider") :]
        helper = helper[: helper.index("\ndef ")]
        assert re.search(r"await asyncio\.wait\(\{ending\}\)", helper)
        assert helper.index("provider.shutdown()") < helper.rindex("_dispatch_hard_kill")
        assert re.search(r"except Exception:\s*\n\s*owner\._dispatch_hard_kill", helper)


# ── The reset path must not reap a co-tenant's process ──


class TestResetLeavesACoTenantsProcessAlone:
    """A shared process outliving one session's shutdown is not a survivor.

    ``session_lifecycle`` SIGKILLs a pid that is still alive after shutdown and
    sweeps its escaped children. Under a cap that pid can be a live process other
    sessions are mid-turn on, and the sweep would take the MCP servers they are
    using with it.
    """

    def test_a_lease_with_no_registered_session_still_holds_the_pid(self):
        """A joiner holds its lease before it is a registered session.

        ``provider.start`` takes the lease and only RETURNS afterwards, so a
        co-tenant that is still starting is invisible to the live session table
        for the whole of a multi-second cold start. The guard has to see it
        anyway, or it force-kills a shared process out from under a session that
        is mid-start -- which is why it asks the registry, where a starting
        session is already recorded, rather than the table, where it is not.
        """
        from kiro_crew.session_lifecycle_parts.teardown import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48271)
            acq = asyncio.run(
                ro.RUNTIME_OWNERSHIP.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=25)
            )
            assert _pid_is_still_held(48271) is True
            assert _pid_is_still_held(48272) is False

            # Once the last lease goes the pid stops being held, so a process
            # that really did outlive its only session is still reapable.
            asyncio.run(ro.RUNTIME_OWNERSHIP.release(acq.lease))
            assert _pid_is_still_held(48271) is False
        finally:
            ro._reset_for_tests()

    def test_a_dead_runtimes_leases_do_not_shield_its_pid(self):
        """The opposite hole: a crashed process must still be reaped and swept."""
        from kiro_crew.session_lifecycle_parts.teardown import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48273)
            asyncio.run(
                ro.RUNTIME_OWNERSHIP.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=25)
            )
            rt.die()
            assert (
                _pid_is_still_held(48273) is False
            ), "a dead runtime's stale leases would strand its escaped children"
        finally:
            ro._reset_for_tests()

    def test_a_subagent_mid_turn_on_the_pid_also_holds_it(self):
        """A tenancy is a holder too: the gate refuses a kill for one.

        A subagent sharing the process holds no lease -- a turn tenancy neither
        consumes the cap nor moves a session -- so a guard that counted leases
        alone would reap a process a subagent is streaming on.
        """
        from kiro_crew.session_lifecycle_parts.teardown import _pid_is_still_held

        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48274)
            handle = ro.claim_runtime_tenancy(rt, holder="subagent:test")
            assert handle is not None
            assert _pid_is_still_held(48274) is True

            ro.release_runtime_tenancy(handle)
            assert _pid_is_still_held(48274) is False
        finally:
            ro._reset_for_tests()

    def test_an_unreadable_registry_leaves_the_pid_reapable(self, caplog):
        """A bookkeeping failure must not turn into a permanent leak."""
        from kiro_crew.session_lifecycle_parts.teardown import _pid_is_still_held

        def explode(_pid):
            raise RuntimeError("registry unreadable")

        with caplog.at_level(logging.DEBUG):
            assert _pid_is_still_held(5153, holders=explode) is False

    def test_the_guard_is_wired_into_the_reset_path(self):
        """The helper being right is not the same as the reset path calling it.

        Structural, and named as such: driving a whole reset needs the lifecycle
        service's entire dependency set, while what a later edit breaks is the
        call and the two things it gates -- the force-kill and the child sweep.
        """
        import re
        from pathlib import Path

        import kiro_crew.session_lifecycle_parts.teardown as lifecycle_mod

        source = Path(lifecycle_mod.__file__).read_text(encoding="utf-8")
        # Asked only for a session on a SHARED chat runtime, so every other reset
        # keeps its force-kill and sweep exactly as before.
        assert (
            "shared_with_others = is_shared_chat_runtime(session.provider) and _pid_is_still_held("
            in source
        )
        assert re.search(r"if shared_with_others:", source), "the force-kill is not gated"
        assert re.search(
            r"if child_pids and not shared_with_others:", source
        ), "the child sweep is not gated, so it would take a co-tenant's MCP servers"

    def test_a_tenant_only_survivor_is_reaped_when_its_last_tenancy_releases(self):
        """A process kept alive ONLY by a tenancy is handed back on release.

        The reset path leaves a still-held pid alone (``_pid_is_still_held``), so
        a pid held only by a subagent TENANCY -- no lease, because the session
        being reset was its only lease and that lease is gone -- has no lease
        release to come. Unless the reset records that a kill was OWED, the
        tenant's last release has nothing to hand back and the process is left
        resident once the subagent's turn ends. This pins both halves: without
        the recorded debt the release hands nothing back (the gap), and with it
        -- the ``refuse_for_tenants`` call the reset makes on the skip -- the
        last release returns the orphaned runtime for teardown.
        """
        ro._reset_for_tests()
        try:
            rt = FakeRuntime(pid=48280)
            handle = ro.claim_runtime_tenancy(rt, holder="subagent:reset-survivor")
            assert handle is not None
            # No lease holds it: only the tenancy. The pid is still held, so the
            # reset leaves it alone.
            from kiro_crew.session_lifecycle_parts.teardown import _pid_is_still_held

            assert _pid_is_still_held(48280) is True
            assert ro.outstanding_leases(rt) == 0

            # The gap: a plain release with no owed debt recorded hands nothing
            # back, so nobody reaps the orphan.
            unmarked = FakeRuntime(pid=48281)
            unmarked_handle = ro.claim_runtime_tenancy(unmarked, holder="subagent:unmarked")
            assert ro.release_runtime_tenancy(unmarked_handle) is None

            # The fix: the reset records the refusal (``refuse_for_tenants`` on the
            # bare pid, exactly what the skip branch now does), which marks the live
            # tenancy owed. The last release then hands the orphaned runtime back.
            tenants, holders = ro.RUNTIME_TENANCY.refuse_for_tenants(48280)
            assert tenants == 1
            assert holders == ["subagent:reset-survivor"]
            handed_back = ro.release_runtime_tenancy(handle)
            assert handed_back is rt, "the owed debt must hand the orphan back for teardown"
            assert _pid_is_still_held(48280) is False
        finally:
            ro._reset_for_tests()

    def test_the_reset_path_records_the_reap_debt_on_the_skip(self):
        """Structural: the skip branch records the kill-owed debt.

        The behavioural test above proves ``refuse_for_tenants`` hands the orphan
        back; this pins that the reset path actually makes that call on the skip,
        so a later edit that drops it re-opens the leak. Named structural for the
        same reason as the guard-wiring test: driving a whole reset needs the
        lifecycle service's entire dependency set.
        """
        from pathlib import Path

        import kiro_crew.session_lifecycle_parts.teardown as lifecycle_mod

        source = Path(lifecycle_mod.__file__).read_text(encoding="utf-8")
        skip = source.index("if shared_with_others:")
        window = source[skip : source.index("elif platform_compat.pid_exists(pid):", skip)]
        assert (
            "RUNTIME_TENANCY.refuse_for_tenants(pid)" in window
        ), "the skip must record the reap debt so the last tenant's release reaps the orphan"


class _GateRuntime(FakeRuntime):
    """A runtime whose ``chat_turn_gate`` is the real one-lock serializer.

    Records how many gate holders are inside at once so a test can prove the
    lock actually serializes rather than merely being awaited.
    """

    def __init__(self, pid: int = 7000) -> None:
        super().__init__(pid=pid)
        self._chat_turn_lock = asyncio.Lock()
        self.max_concurrent = 0
        self._inside = 0

    def chat_turn_gate(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def _gate():
            async with self._chat_turn_lock:
                self._inside += 1
                self.max_concurrent = max(self.max_concurrent, self._inside)
                try:
                    yield
                finally:
                    self._inside -= 1

        return _gate()


def _provider_for_gate(runtime, *, shared_runtime: bool):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider(
        FakeHandle(),  # type: ignore[arg-type]
        runtime,
        owns_runtime=True,
        runtime_lease="lease" if shared_runtime else None,
        shared_runtime=shared_runtime,
        session_key="k",
    )


class TestChatTurnsAreSerializedOnASharedProcess:
    """An ownerless control frame is unambiguous only while ONE chat turn runs.

    The reader loop routes a no-sessionId compaction / clear / agent-switch to
    the sessions with an active turn; with two chat turns overlapping it would
    fan to both and a peer's control event corrupts a co-tenant. So a shared
    chat session holds the per-process turn gate for its whole turn, and a
    sub-agent (shared_runtime=False) is kept OUT of it — contending for the lock
    the principal holds would deadlock the principal→sub-agent await.
    """

    @pytest.mark.asyncio
    async def test_two_shared_chat_turns_do_not_overlap(self):
        rt = _GateRuntime()
        a = _provider_for_gate(rt, shared_runtime=True)
        b = _provider_for_gate(rt, shared_runtime=True)

        order: list[str] = []
        b_may_finish = asyncio.Event()

        async def run_a():
            async with a._chat_turn_gate():
                order.append("a-in")
                # Hold the gate until the test releases it, so b can only be
                # waiting — never inside — while a holds it.
                await b_may_finish.wait()
                order.append("a-out")

        async def run_b():
            # Let a acquire first.
            await asyncio.sleep(0)
            async with b._chat_turn_gate():
                order.append("b-in")

        ta = asyncio.create_task(run_a())
        tb = asyncio.create_task(run_b())
        # Wait on the STATE, not a fixed sleep: a is inside the gate (order shows
        # "a-in") and b is parked on the lock (the lock is held and b has not
        # entered). Both are loop-confined asyncio state, so evaluate on-loop.
        await async_wait_until(
            lambda: order == ["a-in"] and rt._chat_turn_lock.locked(),
            describe=lambda: f"order={order} locked={rt._chat_turn_lock.locked()}",
        )
        # a is inside and holding; b is blocked on the lock, not inside.
        assert order == ["a-in"], order
        assert rt.max_concurrent == 1
        b_may_finish.set()
        try:
            # 30s so a slow runner never decides this; a real deadlock still
            # fails within it. The ordering assertion below is the actual signal.
            await asyncio.wait_for(asyncio.gather(ta, tb), timeout=30)
        except (asyncio.TimeoutError, TimeoutError):
            for t in (ta, tb):
                t.cancel()
            await asyncio.gather(ta, tb, return_exceptions=True)
            raise AssertionError("gate turns did not drain within 30s")
        assert order == ["a-in", "a-out", "b-in"], order
        assert rt.max_concurrent == 1, "two shared chat turns were inside the gate at once"

    @pytest.mark.asyncio
    async def test_a_subagent_turn_does_not_contend_for_the_gate(self):
        """A non-shared (sub-agent) session never touches the lock.

        If it did, a sub-agent whose principal already holds the gate could
        never enter — the deadlock the exemption exists to prevent. Here the
        principal holds the gate and the sub-agent's gate still enters freely.
        """
        rt = _GateRuntime()
        principal = _provider_for_gate(rt, shared_runtime=True)
        subagent = _provider_for_gate(rt, shared_runtime=False)

        async with principal._chat_turn_gate():
            assert rt._chat_turn_lock.locked(), "principal did not take the shared lock"
            # The sub-agent's gate must not block on the principal's held lock.
            # Bound the acquisition with a local deadline: if the exemption
            # regresses, the sub-agent would wait on the principal's held lock
            # forever, so without this the Windows pytest-timeout kills the whole
            # worker instead of producing a bounded failure here.
            entered = False

            async def _enter_subagent_gate() -> None:
                nonlocal entered
                async with subagent._chat_turn_gate():
                    entered = True

            try:
                # 30s so a slow runner never decides this; the principal holds
                # the gate, so if the sub-agent contended it would never enter and
                # a real deadlock still fails within the floor.
                await asyncio.wait_for(_enter_subagent_gate(), timeout=30)
            except (asyncio.TimeoutError, TimeoutError):
                raise AssertionError("a sub-agent turn was blocked by the principal's turn gate")
            assert entered, "a sub-agent turn was blocked by the principal's turn gate"
        assert not rt._chat_turn_lock.locked()


class TestAgentSwitchFrameOnASharedRuntime:
    """A fanned agent-switch frame moves the slot's agent only off a shared runtime.

    A shared runtime fans a switch frame to every co-tenant runner, marked
    ``runtime_global`` with no issuer, so a peer's switch must not move this
    slot's agent. With the preview off nothing is shared and the same frame is
    recorded as it always was. Driven through the real ``_run_chat``.
    """

    @staticmethod
    async def _switch_turn(tmp_path, monkeypatch, *, shared: bool) -> str:
        from unittest.mock import AsyncMock

        from test_todo_pill_resync import _runner_state, _runner_turn

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.providers.base import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        # The switch's durable write and spec-hook reload are other suites' subject.
        monkeypatch.setattr(chat_runner, "record_provider_agent_switch", lambda *a, **k: None)
        monkeypatch.setattr(chat_runner, "refuse_stale_switch", AsyncMock())
        monkeypatch.setattr(
            chat_runner, "_prepare_spec_hooks", AsyncMock(return_value=([], False, None))
        )
        state, _sent = _runner_state(tmp_path, monkeypatch, is_new=False, resumed=True)
        slot = state.get_or_create_slot("switch-chat")
        slot.agent = ""
        provider = (await state.sessions.get_or_create())[0]
        provider._shared_runtime = shared

        streamed: list[bool] = []

        async def stream(message, *args, **kwargs):
            streamed.append(True)
            yield LLMEvent(kind=EVENT_AGENT_SWITCHED, text="other-agent", runtime_global=True)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(provider, False, True))
        await _runner_turn(state, slot, "carry on")
        assert streamed, "the turn never reached the provider stream"
        return slot.agent

    @pytest.mark.asyncio
    async def test_a_peers_switch_does_not_move_a_shared_slots_agent(self, tmp_path, monkeypatch):
        agent = await self._switch_turn(tmp_path, monkeypatch, shared=True)
        assert agent == ""

    @pytest.mark.asyncio
    async def test_the_same_frame_is_recorded_when_nothing_is_shared(self, tmp_path, monkeypatch):
        agent = await self._switch_turn(tmp_path, monkeypatch, shared=False)
        assert agent == "other-agent"


# ── A failed start on a shared process must not try to kill a co-tenant's process ──


class TestFailedAllocationCleanupNeverKillsACoTenantsProcess:
    """A start that fails AFTER taking its lease on a SHARED chat runtime must
    EVICT its own session and kill the process ONLY when it was the last holder.

    The allocation cleanup runs the provider's own shutdown, which destroys this
    session's handle -- so its native session, queue and MCP children leave the
    shared process -- releases the lease, and kills the runtime only when this was
    its last holder. Releasing the lease alone would leave the abandoned session
    resident on a runtime a co-tenant keeps alive. A sole owner's shutdown kills
    its runtime, as the unshared path always did.
    """

    def test_a_failed_joiner_is_evicted_without_killing_the_co_tenants_process(self):
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9400)
            live = ro.RUNTIME_OWNERSHIP
            # Two sessions share one process; the first one's start then fails.
            failing = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))
            other = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", failing.lease)
            # The failed-start cleanup runs the provider's own shutdown.
            asyncio.run(provider.shutdown())

            assert handle.destroyed == 1, (
                "the failed shared start must evict its session (destroy the "
                "handle) so its native session and queue leave the process"
            )
            assert rt.kills == [], "the failed start killed a co-tenant's process"
            assert rt.is_alive(), "the co-tenant's process must still be running"
            assert provider._runtime_lease is None, "the failed start must drop its own lease"
            # The surviving co-tenant still holds it; ITS release ends the process.
            assert asyncio.run(live.release(other.lease)) is rt
        finally:
            ro._reset_for_tests()

    def test_a_failed_sole_owner_start_still_kills_its_runtime(self):
        ro._reset_for_tests()
        try:
            rt = KillableRuntime(pid=9401)
            live = ro.RUNTIME_OWNERSHIP
            only = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawner(rt), cap=10))

            provider, handle = shared_provider(rt, "dashboard:chat-1-1", only.lease)
            asyncio.run(provider.shutdown())

            assert handle.destroyed == 1
            assert len(rt.kills) == 1, "the sole holder's failed start must end the process"
            assert not rt.is_alive()
        finally:
            ro._reset_for_tests()

    def test_the_allocation_cleanup_ends_a_shared_provider_through_its_shutdown(self):
        """``_end_started_provider``: shutdown for a shared provider, hard kill otherwise.

        The hard kill stays the fallback for a shutdown that fails, and is the
        only arm for an unshared provider -- the preview-off path, unchanged.
        """
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts.get_or_create import _end_started_provider

        killed: list[object] = []
        owner = SimpleNamespace(_dispatch_hard_kill=killed.append)

        class Shared:
            _shared_runtime = True

            def __init__(self, fail: bool) -> None:
                self.fail = fail
                self.shutdowns = 0

            async def shutdown(self) -> None:
                self.shutdowns += 1
                if self.fail:
                    raise RuntimeError("shutdown failed")

        clean, broken = Shared(False), Shared(True)
        unshared = SimpleNamespace(_shared_runtime=False)
        for provider in (clean, broken, unshared):
            asyncio.run(_end_started_provider(owner, provider))
        assert clean.shutdowns == 1 and broken.shutdowns == 1
        assert killed == [broken, unshared]


class TestReviewFindingsOnTheSharedPath:
    """Structural pins for four cleanup and ordering rules on the shared path.

    Each test names the one edit that would re-open its finding. Driving them for
    real needs the allocator, the provider start path, the turn runner and the
    compaction coordinator with live sessions; what a later edit breaks is a call
    or its order, which reads off the source.
    """

    def test_a_failed_start_ends_a_shared_provider_through_its_shutdown(self):
        # Reverting to ``owner._dispatch_hard_kill(provider)`` in the start()
        # except arm leaks the lease: the kill gate refuses while it is held.
        import inspect

        import kiro_crew.session_allocation_parts.get_or_create as alloc_mod

        src = inspect.getsource(alloc_mod)
        start = src.index("await provider.start()")
        arm = src[start : src.index("raise", start)]
        assert "except (asyncio.CancelledError, Exception)" in arm
        assert "await _end_started_provider(owner, provider)" in arm
        assert "_dispatch_hard_kill" not in arm

    def test_the_start_path_passes_this_sessions_crew_identity(self):
        # Dropping ``crew_agent=self._crew_agent`` from either call makes a joiner
        # run under the founder's crew (the process default).
        import inspect

        import kiro_crew.providers.acp_runtime_start as start_mod

        src = inspect.getsource(start_mod)
        create = src.index("handle = await runtime.create_session(")
        assert "crew_agent=self._crew_agent," in src[create : src.index(")", create + 40) + 400]
        load = src.index("handle = await self._load_session_with_retry(")
        assert "crew_agent=self._crew_agent," in src[load : load + 700]
        retry = inspect.getsource(start_mod._load_session_with_retry)
        assert "crew_agent=crew_agent," in retry

    def test_every_session_start_on_a_shared_process_checks_the_ceiling(self):
        # A joiner never spawns, so the spawn's over-ceiling refusal does not run
        # for it. The session start does: dropping either call lets a chat join a
        # kiro-cli process and run a spec the current ceiling would refuse.
        import inspect

        from kiro_crew.acp import runtime_session_create, runtime_session_load

        for fn, verb in (
            (runtime_session_create.create_session, "METHOD_SESSION_NEW"),
            (runtime_session_load.load_session, "METHOD_SESSION_LOAD"),
        ):
            src = inspect.getsource(fn)
            check = src.index("await self._refuse_over_ceiling_start(agent or self._agent)")
            assert check < src.index(verb), f"{fn.__name__} checks the ceiling after {verb}"

    def test_compaction_takes_the_gate_outside_its_budget_and_answers_busy(self):
        # Moving the hold back inside ``wait_for(...)`` lets a co-tenant's long
        # turn spend the budget and recycle this session as a failed compaction.
        import inspect

        from kiro_crew.session_compaction import CompactionCoordinator

        src = inspect.getsource(CompactionCoordinator._compact_in_place)
        hold = src.index("await gate.enter_async_context(chat_turn_gate_hold(session.provider))")
        assert src.rindex("async with asyncio.timeout(timeout):", 0, hold) < hold
        busy = src.index("except TimeoutError:", hold)
        assert src.index('return "busy"', busy) < src.index("await asyncio.wait_for(", hold)
        assert hold < src.index("_run_compaction(), timeout=")
        # The receipt budget is measured from the gate, not from before the wait:
        # dropping the reset lets a long co-tenant turn shrink it to the floor.
        reset = src.index("started = time.monotonic()", hold)
        assert reset < src.index("await asyncio.wait_for(", hold)

    def test_a_founding_spawn_launches_with_the_keyed_agent_env(self):
        # Without the hand-off the founder re-reads agent.env at spawn, so a save
        # landing between the read and the spawn gives the process values its
        # key does not name, and a later joiner on that key inherits them.
        import inspect

        import kiro_crew.acp.runtime_spawn_launch as launch_mod
        import kiro_crew.providers.acp_runtime_start as start_mod

        p = make_placement(agent_env={"HTTPS_PROXY": "http://a:1"})
        assert p.spawn_agent_env() == {"HTTPS_PROXY": "http://a:1"}
        assert dict(p.key.agent_env) == {"HTTPS_PROXY": "http://a:1"}
        start_src = inspect.getsource(start_mod)
        assert "agent_env=share.spawn_agent_env()," in start_src
        launch_src = inspect.getsource(launch_mod)
        assert 'self._spawn_agent_env = getattr(self, "_agent_env_override", None)' in launch_src
        assert 'override = getattr(self, "_agent_env_override", None)' in launch_src

    def test_the_runtime_keeps_the_frozen_agent_env(self):
        from kiro_crew.acp.runtime import AcpRuntime

        frozen = AcpRuntime(work_dir="/w", agent_env={"A": "1"})
        assert frozen._agent_env_override == {"A": "1"}
        assert AcpRuntime(work_dir="/w", kas_telemetry=False)._kas_telemetry_override is False
        assert AcpRuntime(work_dir="/w")._agent_env_override is None


class TestAnOversizedSharedProcessDrains:
    """The RSS ceiling reclaims a shared process by closing it to joins.

    Recycling one co-tenant releases one lease and frees nothing, and the
    recycled chat would rejoin the same process with the same key. Closed, every
    later start founds a new process and this one dies with its last holder.
    """

    def test_a_closed_runtime_takes_no_new_lease(self):
        ro._reset_for_tests()
        try:
            old, new = KillableRuntime(pid=9500), KillableRuntime(pid=9501)
            live = ro.RUNTIME_OWNERSHIP
            spawn = spawner(old, new)
            first = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=10))
            assert live.close_to_joins(9500) == 1
            again = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            assert again.runtime is new and again.joined is False
            # The closed process keeps its holders and dies with the last one.
            assert asyncio.run(live.release(first.lease)) is None
        finally:
            ro._reset_for_tests()

    def test_the_ceiling_closes_only_a_shared_runtime_over_it(self, monkeypatch):
        from types import SimpleNamespace

        import kiro_crew.session_cleanup_spare as spare

        cleanup = SimpleNamespace(state=SimpleNamespace(rss_max_mb=100))
        closed: list[int] = []
        leases = {1: 1, 2: 3}
        monkeypatch.setattr(ro.RUNTIME_OWNERSHIP, "leases_on_pid", lambda pid: leases[pid])
        monkeypatch.setattr(ro.RUNTIME_OWNERSHIP, "close_to_joins", closed.append)

        assert spare.rss_limit_for(cleanup, 2, 250) == 300  # shared, under: open
        assert spare.rss_limit_for(cleanup, 1, 150) == 100  # unshared, over: no drain
        assert spare.rss_limit_for(cleanup, 2, 350) == 300  # shared, over: closed
        assert closed == [2]

    def test_the_busy_pass_scales_the_ceiling_like_the_idle_pass(self, monkeypatch):
        # At 1.5x the one-session ceiling, a runtime shared by two chats is within
        # its budget; an unshared one is over it and has its leftovers looked at.
        import logging as _logging
        from types import SimpleNamespace

        import kiro_crew.session_cleanup_leftovers as leftovers_mod

        monkeypatch.setattr(ro.RUNTIME_OWNERSHIP, "leases_on_pid", {1: 1, 2: 2}.get)
        monkeypatch.setattr(ro.RUNTIME_OWNERSHIP, "close_to_joins", lambda pid: None)
        monkeypatch.setattr(leftovers_mod, "end_mid_turn_enabled", lambda cfg: False)
        monkeypatch.setattr(leftovers_mod, "can_end_leftovers", lambda: False)
        looked_at: list[int] = []

        async def snapshot(cleanup, pid, loop):
            looked_at.append(pid)

        monkeypatch.setattr(leftovers_mod, "snapshot", snapshot)
        cleanup = SimpleNamespace(
            state=SimpleNamespace(rss_max_mb=100, leftover_noticed={}),
            _owner=SimpleNamespace(_cfg=None),
            _deps=SimpleNamespace(logger=_logging.getLogger("test")),
        )
        busy = [("dashboard:chat-1-1", 1, None), ("dashboard:chat-2-1", 2, None)]

        async def scenario():
            loop = asyncio.get_running_loop()
            await leftovers_mod.report_busy_sessions(
                cleanup, busy, lambda pid: 0, {1: 150, 2: 150}, loop
            )

        asyncio.run(scenario())
        assert looked_at == [1]


# ── The placement on the start path (providers.chat_share) ──


class _PlacedRuntime(FakeRuntime):
    """A runtime the placement may evict a session from and kill."""

    def __init__(self, pid: int = 4242, backend: str = ACP_BACKEND_KIRO) -> None:
        super().__init__(pid)
        self.acp_backend = backend
        self.kills: list[str] = []
        self.terminated: list[str] = []
        self.evicted: list[str] = []

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)
        self._alive = False

    async def terminate_session(self, session_id: str) -> None:
        self.terminated.append(session_id)

    async def evict_session(self, session_id: str) -> None:
        self.evicted.append(session_id)


@pytest.fixture
def fresh_table(monkeypatch):
    import kiro_crew.providers.chat_share as share_mod

    table = RuntimeOwnership()
    monkeypatch.setattr(share_mod, "RUNTIME_OWNERSHIP", table)
    return table


class TestChatSharePlacement:
    """``ChatSharePlacement`` driven against a real lease table and fake runtimes."""

    def test_two_starts_with_equal_inputs_share_one_process(self, fresh_table):
        rt = _PlacedRuntime()
        spawn = spawner(rt)

        async def scenario():
            # Two chats: equal PROCESS-level inputs, their own session keys.
            a = make_placement(session_key="dashboard:chat-1-1790000000")
            b = make_placement(session_key="dashboard:chat-2-1790000000")
            a._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
            b._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
            ra = await a.place(spawn)
            rb = await b.place(spawn)
            return a, b, ra, rb

        a, b, ra, rb = asyncio.run(scenario())
        assert ra is rb is rt
        assert spawn.calls == [rt]
        assert (a.joined, b.joined) == (False, True)
        assert b.leases_on_runtime == 2

    def test_two_starts_of_ONE_chat_never_share_a_process(self, fresh_table):
        """The same equal inputs, but both starts are the same chat.

        The allocator starts one chat twice on purpose (a person's foreground
        claim does not join a background start), and both starts resume the same
        persisted session id. Sharing a process would put two ``session/load``
        calls on one id: they share the replay counter and the session queue, and
        the load that reads the counter second sees no replayed turn, so it
        evicts the id and takes the winning start's live session with it.
        """
        first, second = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(first, second)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            background = make_placement()
            foreground = make_placement()
            assert background.session_key == foreground.session_key
            background._read_confirmation = foreground._read_confirmation = ok
            return await background.place(spawn), await foreground.place(spawn), foreground

        placed_background, placed_foreground, foreground = asyncio.run(scenario())
        assert placed_background is first
        assert placed_foreground is second, "the second start of one chat joined its own process"
        assert foreground.joined is False
        assert fresh_table.leases_on_runtime(first) == 1

    def test_different_model_agent_or_cwd_never_share(self, fresh_table):
        r1, r2, r3, r4 = (_PlacedRuntime(pid=p) for p in (1, 2, 3, 4))
        spawn = spawner(r1, r2, r3, r4)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            out = []
            for over in (
                {},
                {"configured_model": "claude-opus"},
                {"agent": "other"},
                {"work_dir": "/elsewhere"},
            ):
                p = make_placement(**over)
                p._read_confirmation = ok
                out.append(await p.place(spawn))
            return out

        assert asyncio.run(scenario()) == [r1, r2, r3, r4]

    def test_a_changed_agent_env_never_joins_the_old_process(self, fresh_table):
        # A saved agent.env change: the next chat must found a process that has
        # the new values, not join one that took the old ones at its spawn.
        old, new = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(old, new)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            out = []
            for env in ({"HTTPS_PROXY": "http://old:8080"}, {"HTTPS_PROXY": "http://new:8080"}):
                p = make_placement(agent_env=env)
                p._read_confirmation = ok
                out.append(await p.place(spawn))
            return out

        assert asyncio.run(scenario()) == [old, new]

    def test_a_different_telemetry_opt_out_never_joins_the_old_process(self, fresh_table):
        # KAS takes the telemetry opt-out once, at initialize: a chat whose folder
        # opts out must found its own process, not join one started with telemetry on.
        on, off = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(on, off)

        async def scenario():
            out = []
            # Three chats: the same opt-out joins, a different one founds its own.
            for n, enabled in enumerate((True, True, False)):
                p = make_placement(kas_telemetry=enabled, session_key=f"dashboard:chat-{n}-1")
                p._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, enabled))
                assert p.spawn_kas_telemetry() is enabled
                out.append(await p.place(spawn))
            return out

        assert asyncio.run(scenario()) == [on, on, off]

    def test_a_spec_that_moves_during_placement_is_retried_on_the_fresh_key(self, fresh_table):
        stale, fresh = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(stale, fresh)

        async def scenario():
            p = make_placement()
            p._read_confirmation = confirming(
                ("gen-2", "acct-A", "auto", False, True), ("gen-2", "acct-A", "auto", False, True)
            )
            return p, await p.place(spawn)

        p, placed = asyncio.run(scenario())
        assert placed is fresh
        # The founder of the stale key held its only lease: released and killed.
        assert stale.kills == ["chat runtime sharing: placement moved before confirm"]
        assert p.key.spec_generation == "gen-2"
        assert fresh_table.leases_on_runtime(stale) == 0

    @pytest.mark.parametrize(
        ("moved", "field", "expected"),
        [
            (("gen-1", "acct-A", "strict", False, True), "sandbox_mode", "strict"),
            (("gen-1", "acct-A", "auto", True, True), "forward_ssh_auth_sock", True),
            (("gen-1", "acct-A", "auto", False, False), "kas_telemetry", False),
        ],
    )
    def test_a_keyed_input_that_moves_during_placement_is_retried(
        self, fresh_table, moved, field, expected
    ):
        # The sandbox tier, the forwarding consent or the telemetry opt-out changed
        # between the key read and the acquisition: the stale lease is dropped and
        # the session places again on a key holding the new value.
        stale, fresh = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(stale, fresh)

        async def scenario():
            p = make_placement()
            p._read_confirmation = confirming(moved, moved)
            return p, await p.place(spawn)

        p, placed = asyncio.run(scenario())
        assert placed is fresh
        assert stale.kills == ["chat runtime sharing: placement moved before confirm"]
        assert getattr(p.key, field) == expected

    def test_an_input_still_moving_founds_an_unjoinable_process(self, fresh_table):
        runtimes = [_PlacedRuntime(pid=p) for p in range(1, 5)]
        spawn = spawner(*runtimes)
        moving = iter(["gen-2", "gen-3", "gen-4", "gen-5"])

        async def read():
            return next(moving), "acct-A", "auto", True, True

        async def scenario():
            p = make_placement(forward_ssh=True)
            p._read_confirmation = read
            return p, await p.place(spawn)

        p, placed = asyncio.run(scenario())
        assert placed is runtimes[3]
        assert p.key.spec_generation.startswith("unsettled-")
        assert p.joinable is False
        # Nothing can match the key, so the spawn reads the consent fresh.
        assert p.spawn_forward_ssh() is None

    @pytest.mark.parametrize("prefix", ["unobservable-", "unresolved-"])
    def test_an_unreadable_spec_founds_once_without_retrying(self, fresh_table, prefix):
        # Every read of an unreadable spec is a fresh unique token, so a
        # re-compare would never match: the first founding is kept, with no
        # spawn-and-kill rounds.
        runtimes = [_PlacedRuntime(pid=p) for p in range(1, 5)]
        spawn = spawner(*runtimes)
        reads = iter(f"{prefix}{n}" for n in range(2, 6))

        async def read():
            return next(reads), "acct-A", "auto", False, True

        async def scenario():
            p = make_placement(spec_generation=f"{prefix}1")
            p._read_confirmation = read
            return await p.place(spawn)

        assert asyncio.run(scenario()) is runtimes[0]
        assert spawn.calls == [runtimes[0]]
        assert runtimes[0].kills == []

    def test_an_empty_era_re_read_fails_closed_to_a_retry(self, fresh_table):
        first, second = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(first, second)

        async def scenario():
            p = make_placement(era="acct-A")
            p._read_confirmation = confirming(
                ("gen-1", "", "auto", False, True), ("gen-1", "", "auto", False, True)
            )
            return p, await p.place(spawn)

        p, placed = asyncio.run(scenario())
        assert placed is second
        assert first.kills
        assert p.key.spawn_identity.startswith("unverified-")

    def test_a_joiner_refused_by_fork_governance_leaves_and_raises(self, fresh_table, monkeypatch):
        import kiro_crew.agent as agent_mod
        from kiro_crew.acp.runtime import AcpRuntimeError

        rt = _PlacedRuntime()
        spawn = spawner(rt)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        def refuse(agent, work_dir):
            raise agent_mod.ForkGovernanceUnresolved("fork refresh failed")

        async def scenario():
            founder = make_placement()
            founder._read_confirmation = ok
            await founder.place(spawn)
            monkeypatch.setattr(agent_mod, "require_fork_governance", refuse)
            joiner = make_placement(session_key="dashboard:chat-2-1790000000")
            joiner._read_confirmation = ok
            with pytest.raises(AcpRuntimeError, match="fork refresh failed"):
                await joiner.place(spawn)
            return founder, joiner

        founder, joiner = asyncio.run(scenario())
        assert joiner.lease is None
        assert fresh_table.leases_on_runtime(rt) == 1
        assert rt.kills == [], "the joiner's refusal must not end the founder's process"

    def test_a_cancel_during_the_confirmation_returns_the_lease(self, fresh_table):
        rt = _PlacedRuntime()
        spawn = spawner(rt)

        async def scenario():
            p = make_placement()
            gate = asyncio.Event()

            async def read():
                await gate.wait()

            p._read_confirmation = read
            task = asyncio.ensure_future(p.place(spawn))
            await async_wait_until(lambda: fresh_table.leases_on_runtime(rt) == 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        assert fresh_table.leases_on_runtime(rt) == 0
        assert rt.kills == ["chat runtime sharing: placement cancelled before confirm"]

    def test_a_dead_runtime_is_replaced_through_the_same_placement(self, fresh_table):
        dead, fresh = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(dead, fresh)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            p = make_placement()
            p._read_confirmation = ok
            await p.place(spawn)
            dead.die()
            return p, await p.replace_dead(spawn)

        p, placed = asyncio.run(scenario())
        assert placed is fresh
        assert fresh_table.leases_on_runtime(dead) == 0
        assert fresh_table.leases_on_runtime(fresh) == 1

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_failed_joiner_is_evicted_without_ending_the_process(self, fresh_table, backend):
        rt = _PlacedRuntime(backend=backend)
        spawn = spawner(rt)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            founder = make_placement()
            joiner = make_placement(session_key="dashboard:chat-2-1790000000")
            founder._read_confirmation = joiner._read_confirmation = ok
            await founder.place(spawn)
            await joiner.place(spawn)
            await joiner.abandon(rt, "sess-joiner")

        asyncio.run(scenario())
        # KAS keeps the record (session/close); kiro-cli terminates the session.
        if backend == ACP_BACKEND_KAS:
            assert (rt.evicted, rt.terminated) == (["sess-joiner"], [])
        else:
            assert (rt.evicted, rt.terminated) == ([], ["sess-joiner"])
        assert rt.kills == []
        assert fresh_table.leases_on_runtime(rt) == 1

    def test_a_failed_sole_holder_kills_its_runtime(self, fresh_table):
        rt = _PlacedRuntime()
        spawn = spawner(rt)

        async def scenario():
            p = make_placement()
            p._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
            await p.place(spawn)
            await p.abandon(rt, None)

        asyncio.run(scenario())
        assert rt.kills == ["failed session setup cleanup (last holder)"]

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_a_cancel_during_the_eviction_still_returns_the_lease(self, fresh_table, backend):
        # A cancel landing while the eviction is pending must not skip the release:
        # the lease would stay held, the kill gate would refuse every kill of the
        # pid, and the process would outlive its last chat.
        rt = _PlacedRuntime(backend=backend)
        spawn = spawner(rt)
        pending = asyncio.Event()

        async def evict_until_cancelled(session_id: str) -> None:
            pending.set()
            await asyncio.Event().wait()

        rt.terminate_session = rt.evict_session = evict_until_cancelled  # type: ignore[method-assign]

        async def scenario():
            p = make_placement(provider=_PlacementProvider(backend=backend))
            p._read_confirmation = confirming(("gen-1", "acct-A", "auto", False, True))
            await p.place(spawn)
            task = asyncio.ensure_future(p.abandon(rt, "sess-1"))
            await pending.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return p

        p = asyncio.run(scenario())
        assert p.lease is None
        assert fresh_table.leases_on_runtime(rt) == 0
        assert rt.kills == ["failed session setup cleanup (last holder)"]


class TestResolveChatShare:
    """The preview gate on the start path: off is one config read and ``None``."""

    def _cfg(self, previews):
        from types import SimpleNamespace

        return SimpleNamespace(
            dashboard=SimpleNamespace(feature_previews=previews),
            agent=SimpleNamespace(chat_runtime_sharing_max_sessions=4),
        )

    def _resolve(self, monkeypatch, previews, provider, **kw):
        import kiro_crew.providers.chat_share as share_mod
        from kiro_crew.config import live

        cfg = self._cfg(previews)
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        reads: list[str] = []

        def spec_generation(work_dir, agent):
            reads.append("spec")
            return "gen-1"

        monkeypatch.setattr(share_mod, "agent_spec_generation", spec_generation)
        monkeypatch.setattr(share_mod, "kas_chat_sharing_ready", lambda: kw.get("kas_ready", False))
        monkeypatch.setattr(
            share_mod, "agent_spawns_native_subagents", lambda w, a: kw.get("native", False)
        )
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "effective_sandbox_mode", lambda mode: mode)
        monkeypatch.setattr(sandbox_mod, "_forward_ssh_auth_sock", lambda mode, hidden: False)
        args = dict(
            work_dir="/w",
            agent="kirocrew",
            sandbox_mode="auto",
            extra_env={},
            mcp_gateway_overlay=None,
            mcp_gateway_socket=None,
            configured_model="",
            launch_agent="",
            remote_workspace=kw.get("remote", False),
        )
        return asyncio.run(share_mod.resolve_chat_share(provider, **args)), reads

    def test_preview_off_places_nothing_and_reads_nothing(self, monkeypatch):
        placement, reads = self._resolve(monkeypatch, {}, _PlacementProvider())
        assert placement is None
        assert reads == []

    def test_preview_on_places_an_eligible_chat_with_the_configured_cap(self, monkeypatch):
        placement, reads = self._resolve(
            monkeypatch, {"chat_runtime_sharing": True}, _PlacementProvider()
        )
        assert placement is not None
        assert placement.cap == 4
        assert reads == ["spec"]

    def test_an_ineligible_start_is_logged_by_reason_and_reads_nothing(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch,
                {"chat_runtime_sharing": True},
                _PlacementProvider(backend=ACP_BACKEND_KAS),
            )
        assert placement is None
        assert reads == []
        assert "reason=kas_engine_not_shareable" in caplog.text

    def test_a_ready_kas_engine_shares(self, monkeypatch):
        placement, _ = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            kas_ready=True,
        )
        assert placement is not None

    def test_a_native_subagent_agent_keeps_its_own_process(self, monkeypatch, caplog):
        # Its subagents' approvals route only while one session is registered, so
        # beside a co-tenant they would be auto-rejected: it must never join.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch, {"chat_runtime_sharing": True}, _PlacementProvider(), native=True
            )
        assert placement is None and reads == []
        assert "reason=native_subagents" in caplog.text

    @pytest.mark.parametrize(
        ("spec", "grants"),
        [
            ({"tools": ["fs_read", "execute_bash"]}, False),
            ({"tools": ["@builtin"]}, True),
            ({"tools": "*"}, True),
            ({"tools": ["read", "use_subagent"]}, True),
            ({"tools": ["subagent/helper"]}, True),
            ({"tools": ["*"], "excludedTools": ["subagent"]}, False),
            ({}, True),
            (None, True),
        ],
    )
    def test_which_specs_can_start_native_subagents(self, spec, grants):
        from kiro_crew.acp.chat_runtime_sharing import spec_grants_native_subagents

        assert spec_grants_native_subagents(spec) is grants

    def test_an_unreadable_agent_spec_fails_closed(self, tmp_path):
        from kiro_crew.acp.chat_runtime_sharing import agent_spawns_native_subagents

        assert agent_spawns_native_subagents(tmp_path, "not a valid name/../x") is True

    def test_a_session_environment_keeps_its_own_process(self, monkeypatch):
        placement, reads = self._resolve(
            monkeypatch, {"chat_runtime_sharing": True}, _PlacementProvider(), remote=True
        )
        assert placement is None and reads == []
