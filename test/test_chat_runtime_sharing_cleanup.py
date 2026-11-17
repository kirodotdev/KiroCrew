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
    FakeRuntime,
    KillableRuntime,
    a_key,
    shared_provider,
    spawner,
)

import kiro_crew.runtime_ownership as ro
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.runtime_ownership import RuntimeOwnership, authorize_runtime_kill
from kiro_crew.testing.wait import async_wait_until, await_until

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


class TestSharedChatTurnsAreNotSerialized:
    """A shared chat never waits for another chat's turn.

    Chat sharing is KAS-only, and a KAS engine that may share runs turns of
    different sessions in parallel (``KAS_CHAT_SHARING_MIN_ENGINE``), so Crew holds
    no per-process turn lock. Re-adding one would queue shared chats behind each
    other, the behaviour the preview promises not to have.
    """

    def test_no_per_process_chat_turn_lock_exists(self):
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.session_provider import AcpSessionProvider
        from kiro_crew.providers.acp import AcpProvider

        assert not hasattr(AcpRuntime, "chat_turn_gate")
        assert not hasattr(AcpSessionProvider, "_chat_turn_gate")
        assert not hasattr(AcpSessionProvider, "hold_chat_turn_gate")
        assert not hasattr(AcpProvider, "hold_chat_turn_gate")
        assert not hasattr(ro, "chat_turn_gate_hold")


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
        outer = src.index("except (asyncio.CancelledError, Exception)", start)
        arm = src[outer : src.index("raise", outer)]
        assert "await _end_started_provider(owner, provider)" in arm
        # The failed-join retry arm ends the provider the same way.
        assert "_dispatch_hard_kill" not in src[start : src.index("raise", outer)]

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


class TestASharedCodexProcessClosesOnADeparture:
    """codex leaves a departed session's MCP fleet running (``session/close`` never
    ends it), so a shared codex process takes no new chat once one leaves and dies
    with its last chat: at most cap-1 leftover fleets per process."""

    def test_a_chat_after_a_departure_founds_a_new_process(self):
        ro._reset_for_tests()
        try:
            old, new = KillableRuntime(pid=9600), KillableRuntime(pid=9601)
            old.chat_closes_on_departure = True
            live = ro.RUNTIME_OWNERSHIP
            spawn = spawner(old, new)
            first = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            second = asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=10))
            third = asyncio.run(live.acquire(a_key(), "dashboard:chat-3-1", spawn, cap=10))
            assert second.joined and third.joined
            assert asyncio.run(live.release(first.lease)) is None
            later = asyncio.run(live.acquire(a_key(), "dashboard:chat-4-1", spawn, cap=10))
            assert later.runtime is new and later.joined is False
            # The closed process keeps its holders and dies with the last one.
            assert asyncio.run(live.release(second.lease)) is None
            assert asyncio.run(live.release(third.lease)) is old
        finally:
            ro._reset_for_tests()

    def test_a_runtime_without_the_mark_stays_open(self):
        ro._reset_for_tests()
        try:
            old, new = KillableRuntime(pid=9700), KillableRuntime(pid=9701)
            live = ro.RUNTIME_OWNERSHIP
            spawn = spawner(old, new)
            first = asyncio.run(live.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
            asyncio.run(live.acquire(a_key(), "dashboard:chat-2-1", spawn, cap=10))
            asyncio.run(live.release(first.lease))
            later = asyncio.run(live.acquire(a_key(), "dashboard:chat-3-1", spawn, cap=10))
            assert later.runtime is old and later.joined is True
        finally:
            ro._reset_for_tests()

    @pytest.mark.parametrize(
        ("backend", "marked"), [(ACP_BACKEND_CODEX, True), (ACP_BACKEND_KAS, False)]
    )
    def test_only_a_codex_placement_marks_its_runtime(self, backend, marked):
        from types import SimpleNamespace

        from kiro_crew.acp.runtime import AcpRuntime

        p = make_placement(provider=_PlacementProvider(backend=backend))
        runtime = AcpRuntime(work_dir="/w")
        assert runtime.chat_closes_on_departure is False
        p._accept(SimpleNamespace(lease="l", joined=False, leases_on_runtime=1, runtime=runtime))
        assert runtime.chat_closes_on_departure is marked


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
    # The post-acquisition admission re-read (a KAS key carries no spec): no
    # native subagents, as the fake agents here grant none.
    monkeypatch.setattr(share_mod, "agent_spawns_native_subagents", lambda w, a, b="": False)
    monkeypatch.setattr(share_mod, "agent_pins_tool_surface", lambda w, a, t: False)
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

    def test_a_chat_restarted_onto_its_shared_tree_rejoins_that_process(self, fresh_table):
        """The first message overtakes a chat's pre-warm, and the chat restarts.

        Chat B's eager spawn joins A's process, then the first message takes the
        start over: the pre-warm is dropped and B restarts carrying the tree its
        session was recorded in, which is A's process's own tree. A founder keys no
        tree, so keying B's path would found a second process on the same tree.
        The gate below holds B's pre-warm mid-handshake, as the race does.
        """
        from pathlib import Path

        tree = Path("/home/u/.kirocrew/scratch/tree-a")
        shared, stray = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        shared.work_scratch_dir = tree
        spawn = spawner(shared, stray)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            a = make_placement(session_key="dashboard:chat-a-1")
            a._read_confirmation = ok
            assert await a.place(spawn) is shared

            reached, release = asyncio.Event(), asyncio.Event()

            async def held_confirmation():
                reached.set()
                await release.wait()
                return await ok()

            prewarm = make_placement(session_key="dashboard:chat-b-1")
            prewarm._read_confirmation = held_confirmation
            task = asyncio.ensure_future(prewarm.place(spawn))
            # Bounded, and a pre-warm that fails before the gate surfaces here.
            await async_wait_until(
                lambda: reached.is_set() or task.done(), describe=lambda: "pre-warm handshake"
            )
            if task.done():
                task.result()
            assert fresh_table.leases_on_runtime(shared) == 2
            # The first message takes over: the pre-warm is dropped mid-handshake.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await await_until(task, what="the dropped pre-warm")
            assert fresh_table.leases_on_runtime(shared) == 1

            restarted = _PlacementProvider()
            restarted._shared_scratch = tree
            b = make_placement(provider=restarted, session_key="dashboard:chat-b-1")
            b._read_confirmation = ok
            return b, await b.place(spawn)

        b, placed = asyncio.run(scenario())
        assert placed is shared, "the restarted chat founded a second process on its own tree"
        assert spawn.calls == [shared]
        assert b.joined is True

    def test_two_restarts_of_one_tree_with_different_recorded_trees_share(self, fresh_table):
        """The gateway restarts on a tree whose lead and worker ran on two processes.

        Each recorded its own scratch directory. The directory is not a sharing
        input, so the worker's key equals the lead's and it joins the lead's process
        instead of founding a second one on every restart.
        """
        from pathlib import Path

        lead_rt, stray = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        lead_rt.work_scratch_dir = Path("/home/u/.kirocrew/scratch/tree-lead")
        spawn = spawner(lead_rt, stray)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        def restarted(name, tree):
            provider = _PlacementProvider()
            provider._shared_scratch = Path(tree)
            p = make_placement(provider=provider, session_key=f"dashboard:chat-{name}-1")
            p._read_confirmation = ok
            return p

        async def scenario():
            lead = restarted("lead", "/home/u/.kirocrew/scratch/tree-lead")
            worker = restarted("worker", "/home/u/.kirocrew/scratch/tree-worker")
            assert worker.key == lead.key
            return worker, await lead.place(spawn), await worker.place(spawn)

        worker, placed_lead, placed_worker = asyncio.run(scenario())
        assert (placed_lead, placed_worker) == (lead_rt, lead_rt), "the worker founded its own"
        assert spawn.calls == [lead_rt]
        assert worker.joined is True

    def test_a_fresh_chat_joins_the_process_a_restarted_founder_mounted_its_tree_on(
        self, fresh_table
    ):
        """The gateway restarts and the lead's resume is the tree's first start.

        It keys the tree it was recorded in and founds a process that mounts that
        tree as its own. A worker the lead opens afterwards is fresh (no tree): it
        must join that process, as it would one a fresh lead had founded.
        """
        from pathlib import Path

        tree = Path("/home/u/.kirocrew/scratch/tree-lead")
        lead_rt, stray = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        lead_rt.work_scratch_dir = tree
        spawn = spawner(lead_rt, stray)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        async def scenario():
            restarted = _PlacementProvider()
            restarted._shared_scratch = tree
            lead = make_placement(provider=restarted, session_key="dashboard:chat-lead-1")
            lead._read_confirmation = ok
            worker = make_placement(session_key="dashboard:chat-worker-1")
            worker._read_confirmation = ok
            return worker, await lead.place(spawn), await worker.place(spawn)

        worker, placed_lead, placed_worker = asyncio.run(scenario())
        assert (placed_lead, placed_worker) == (lead_rt, lead_rt), "the worker founded its own"
        assert spawn.calls == [lead_rt]
        assert worker.joined is True

    def test_two_restarts_of_one_tree_at_once_share_the_process_the_first_founds(self, fresh_table):
        """Resume All after a restart: two chats of tree A start together.

        The second waits on the first one's launch. The first publishes under the
        founder's empty tree key, so a waiter re-picking by its own tree key would
        miss it and found a second process on the same tree.
        """
        from pathlib import Path

        tree = Path("/home/u/.kirocrew/scratch/tree-a")
        first, stray = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        first.work_scratch_dir = tree
        launched, release = asyncio.Event(), asyncio.Event()
        calls: list[object] = []
        pending = [first, stray]

        async def spawn():
            rt = pending.pop(0)
            calls.append(rt)
            launched.set()
            await release.wait()
            return rt

        ok = confirming(("gen-1", "acct-A", "auto", False, True))

        def restarted(name):
            provider = _PlacementProvider()
            provider._shared_scratch = tree
            p = make_placement(provider=provider, session_key=f"dashboard:chat-{name}-1")
            p._read_confirmation = ok
            return p

        async def scenario():
            a, b = restarted("a"), restarted("b")
            ta = asyncio.ensure_future(a.place(spawn))
            await async_wait_until(launched.is_set, describe=lambda: "the first launch")
            tb = asyncio.ensure_future(b.place(spawn))
            await async_wait_until(
                lambda: any("dashboard:chat-b-1" in f.sessions for f in fresh_table._foundings),
                describe=lambda: "the second start waiting on the launch",
            )
            release.set()
            placed_a = await await_until(ta, what="the first restart's placement")
            placed_b = await await_until(tb, what="the waiting restart's placement")
            return placed_a, placed_b, b

        placed_a, placed_b, b = asyncio.run(scenario())
        assert (placed_a, placed_b) == (first, first), "the waiter founded a second process"
        assert calls == [first]
        assert b.joined is True

    def test_a_founded_start_logs_which_key_fields_kept_it_off(self, fresh_table, caplog):
        # Names and emptiness only: the work directory is the one field that differs.
        own, fresh = _PlacedRuntime(pid=1), _PlacedRuntime(pid=2)
        spawn = spawner(own, fresh)
        ok = confirming(("gen-1", "acct-A", "auto", False, True))
        tree = "/home/u/oss/other-worktree"

        async def scenario():
            b = make_placement(session_key="dashboard:chat-b-1", work_dir=tree)
            b._read_confirmation = ok
            c = make_placement(session_key="dashboard:chat-c-1")
            c._read_confirmation = ok
            await b.place(spawn)
            await c.place(spawn)

        with caplog.at_level(logging.INFO, logger="kiro_crew.providers.chat_share"):
            asyncio.run(scenario())
        lines = [r.getMessage() for r in caplog.records if "founded:" in r.getMessage()]
        assert lines[0].startswith("chat-runtime-sharing founded: no live process in this tree")
        assert "differs from candidate 0 in work_dir(ours=set,theirs=set)" in lines[1]
        assert tree not in "\n".join(lines)

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

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX])
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
        # KAS keeps the record (session/close) instead of its deleting teardown;
        # kiro-cli terminates the session, and codex's own teardown verb is the
        # record-keeping session/close (CodexHarness.teardown).
        if backend == ACP_BACKEND_KAS:
            assert (rt.evicted, rt.terminated) == (["sess-joiner"], [])
        else:
            assert (rt.evicted, rt.terminated) == ([], ["sess-joiner"])
        if backend == ACP_BACKEND_CODEX:
            from kiro_crew.acp.harness.codex import CodexHarness
            from kiro_crew.acp.types import METHOD_SESSION_CLOSE

            assert CodexHarness().teardown.method == METHOD_SESSION_CLOSE
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

    def test_a_cancel_during_the_native_subagent_recheck_returns_the_lease(self, fresh_table):
        # The KAS recheck runs after the acquisition; a start cancelled while it
        # is pending must release the lease and kill the process it founded.
        rt = _PlacedRuntime(backend=ACP_BACKEND_KAS)
        spawn = spawner(rt)
        pending = asyncio.Event()

        async def recheck_until_cancelled() -> bool:
            pending.set()
            await asyncio.Event().wait()
            return False

        async def scenario():
            p = make_placement(provider=_PlacementProvider(backend=ACP_BACKEND_KAS))
            p._read_confirmation = confirming(
                (p._spec_generation, "acct-A", "auto", False, p._kas_telemetry)
            )
            p._spec_needs_own_process_now = recheck_until_cancelled
            task = asyncio.ensure_future(p.place(spawn))
            await asyncio.wait_for(pending.wait(), 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return p

        p = asyncio.run(scenario())
        assert p.lease is None
        assert fresh_table.leases_on_runtime(rt) == 0
        assert rt.kills, "the founded process outlived its cancelled start"


class TestResolveChatShare:
    """The preview gate on the start path: off is one config read and ``None``."""

    def _cfg(self, previews, harnesses=None):
        from types import SimpleNamespace

        agent = SimpleNamespace(chat_runtime_sharing_max_sessions=4)
        if harnesses is not None:
            agent.chat_runtime_sharing_harnesses = harnesses
        return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews), agent=agent)

    def _resolve(self, monkeypatch, previews, provider, **kw):
        import kiro_crew.providers.chat_share as share_mod
        from kiro_crew.config import live

        cfg = self._cfg(previews, kw.get("harnesses"))
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        reads: list[str] = []

        def spec_generation(work_dir, agent):
            reads.append("spec")
            return "gen-1"

        self.kas_probes = 0

        def kas_ready(pin=""):
            self.kas_probes += 1
            return kw.get("kas_ready", False)

        monkeypatch.setattr(share_mod, "agent_spec_generation", spec_generation)
        monkeypatch.setattr(share_mod, "kas_chat_sharing_ready", kas_ready)
        monkeypatch.setattr(
            share_mod, "agent_spawns_native_subagents", lambda w, a, b="": kw.get("native", False)
        )
        monkeypatch.setattr(
            share_mod, "agent_pins_tool_surface", lambda w, a, t: kw.get("pinned", False)
        )
        import kiro_crew.sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "effective_sandbox_mode", lambda mode: mode)
        monkeypatch.setattr(sandbox_mod, "_forward_ssh_auth_sock", lambda mode, hidden: False)
        args = dict(
            work_dir="/w",
            agent="kirocrew",
            sandbox_mode="auto",
            extra_env=kw.get("extra_env", {}),
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

    def test_preview_off_leaves_a_resumed_chats_recorded_tree_bound(self, monkeypatch):
        # Off, nothing is placed, so the chat's own spawn still mounts its recorded tree.
        from pathlib import Path

        provider = _PlacementProvider()
        tree = Path("/home/u/.kirocrew/scratch/tree-own")
        provider._shared_scratch = tree
        placement, reads = self._resolve(monkeypatch, {}, provider)
        assert placement is None
        assert reads == []
        assert provider._shared_scratch == tree

    def test_preview_on_places_an_eligible_chat_with_the_configured_cap(self, monkeypatch):
        placement, reads = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            kas_ready=True,
        )
        assert placement is not None
        assert placement.cap == 4
        # KAS takes the spec per session, so a KAS placement neither keys nor reads it.
        assert reads == []
        assert placement.key.spec_generation == ""

    def test_a_kiro_cli_chat_keeps_its_own_process_with_the_preview_on(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch,
                {"chat_runtime_sharing": True},
                _PlacementProvider(backend=ACP_BACKEND_KIRO),
                kas_ready=True,
            )
        assert placement is None
        assert reads == []
        assert "reason=backend_not_chat_shareable" in caplog.text

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

    def test_kas_switched_off_keeps_its_own_process_and_reads_no_engine(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch,
                {"chat_runtime_sharing": True},
                _PlacementProvider(backend=ACP_BACKEND_KAS),
                kas_ready=True,
                harnesses={"kas": False},
            )
        assert placement is None and reads == []
        assert self.kas_probes == 0
        assert "reason=harness_switched_off backend=kas" in caplog.text

    def test_kas_switched_on_explicitly_shares(self, monkeypatch):
        placement, _ = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            kas_ready=True,
            harnesses={"kas": True},
        )
        assert placement is not None
        assert self.kas_probes == 1

    def test_the_kas_switch_does_nothing_with_the_preview_off(self, monkeypatch):
        placement, reads = self._resolve(
            monkeypatch,
            {},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            kas_ready=True,
            harnesses={"kas": True},
        )
        assert placement is None and reads == []
        assert self.kas_probes == 0

    def test_a_kas_refusal_is_counted_under_its_harness(self, monkeypatch):
        import kiro_crew.providers.chat_share as share_mod

        counted: list[tuple] = []
        monkeypatch.setattr(
            share_mod, "record_refused", lambda reason, **kw: counted.append((reason, kw))
        )
        self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            harnesses={"kas": False},
        )
        assert counted == [("harness_switched_off", {"backend": ACP_BACKEND_KAS})]

    def test_codex_is_off_by_default_under_the_preview(self, monkeypatch, caplog):
        """codex is new to chat sharing: with the preview on and no switch stored,
        a codex chat keeps its own process."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch,
                {"chat_runtime_sharing": True},
                _PlacementProvider(backend=ACP_BACKEND_CODEX),
            )
        assert placement is None and reads == []
        assert "reason=harness_switched_off backend=codex" in caplog.text

    def test_a_codex_chat_switched_on_shares_without_the_kas_engine_read(self, monkeypatch):
        """codex has no engine gate: with its switch on it shares, and the KAS
        engine is never probed. Its era is the codex credential, not Kiro's."""
        import kiro_crew.providers.chat_share as share_mod

        monkeypatch.setattr(share_mod, "codex_spawn_identity", lambda env=None: "codex:fixed")
        placement, reads = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_CODEX),
            harnesses={"codex": True},
        )
        assert placement is not None
        assert reads == ["spec"]
        assert self.kas_probes == 0
        assert placement._raw_era == "codex:fixed"

    def test_a_codex_chat_keys_on_the_home_its_own_spawn_resolves(self, monkeypatch):
        """The era is read under the start's ``extra_env``, the overlay the codex
        child is spawned with, so a chat whose overlay moves CODEX_HOME keys on that
        home rather than on the gateway's."""
        import kiro_crew.providers.chat_share as share_mod

        seen = []
        monkeypatch.setattr(
            share_mod, "codex_spawn_identity", lambda env=None: seen.append(env) or "codex:x"
        )
        overlay = {"CODEX_HOME": "/srv/codex-b"}
        placement, _ = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_CODEX),
            harnesses={"codex": True},
            extra_env=overlay,
        )
        assert placement is not None
        assert seen == [overlay]

    def test_a_native_subagent_agent_keeps_its_own_process(self, monkeypatch, caplog):
        # Its subagents' approvals route only while one session is registered, so
        # beside a co-tenant they would be auto-rejected: it must never join.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
            placement, reads = self._resolve(
                monkeypatch,
                {"chat_runtime_sharing": True},
                _PlacementProvider(backend=ACP_BACKEND_KAS),
                kas_ready=True,
                native=True,
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
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_KAS),
            kas_ready=True,
            remote=True,
        )
        assert placement is None and reads == []

    @pytest.mark.parametrize("agent_turns", [True, False])
    def test_a_claude_chat_shares_whatever_agent_started_turns_says(self, monkeypatch, agent_turns):
        """The shared session handle reads a turn claude starts on its own
        (``session_handle_agent_turns``), so the setting places no claude chat."""
        import kiro_crew.providers.chat_share as share_mod

        real_cfg = self._cfg

        def cfg(previews, harnesses=None):
            c = real_cfg(previews, harnesses)
            c.agent.agent_started_turns = agent_turns
            return c

        monkeypatch.setattr(self, "_cfg", cfg)
        monkeypatch.setattr(share_mod, "_self_signed_era", lambda backend, env=None: "claude:x")
        placement, _ = self._resolve(
            monkeypatch,
            {"chat_runtime_sharing": True},
            _PlacementProvider(backend=ACP_BACKEND_CLAUDE),
            harnesses={"claude": True},
        )
        assert placement is not None

    @pytest.mark.parametrize(
        "mode, env, shares",
        [("auto", "", False), (None, "auto", False), (None, "", True), ("plan", "", True)],
    )
    def test_a_claude_chat_in_auto_permission_mode_keeps_its_own_process(
        self, monkeypatch, caplog, mode, env, shares
    ):
        """Only the per-chat client audits the calls Claude's classifier approves
        unasked, so an ``auto`` chat (its own ask, or the operator's lever) never takes
        the shared process; asking whether says nothing about another mode."""
        import kiro_crew.providers.chat_share as share_mod
        from kiro_crew.agent_sdk.backends_models import CC_PERMISSION_MODE_ENV

        real_cfg = self._cfg

        def cfg(previews, harnesses=None):
            c = real_cfg(previews, harnesses)
            c.agent.agent_started_turns = False
            return c

        monkeypatch.setattr(self, "_cfg", cfg)
        monkeypatch.delenv(CC_PERMISSION_MODE_ENV, raising=False)
        if env:
            monkeypatch.setenv(CC_PERMISSION_MODE_ENV, env)
        monkeypatch.setattr(share_mod, "_self_signed_era", lambda backend, env=None: "claude:x")
        provider = _PlacementProvider(backend=ACP_BACKEND_CLAUDE)
        provider._client._permission_mode = mode
        with caplog.at_level("WARNING"):
            placement, _ = self._resolve(
                monkeypatch, {"chat_runtime_sharing": True}, provider, harnesses={"claude": True}
            )
        assert (placement is not None) is shares
        assert "is not recognised" not in caplog.text


class TestTheCodexEraIsCodexsOwnCredential:
    """A codex process authenticates from ``$CODEX_HOME/auth.json``, never from Kiro's
    sign-in, so its placement keys and re-confirms on that file, not the Kiro era."""

    @staticmethod
    def _write_chatgpt_auth(path, *, account, user, access, refresh, last_refresh):
        import base64
        import json

        claims = {"https://api.openai.com/auth": {"chatgpt_user_id": user}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        path.write_text(
            json.dumps(
                {
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "id_token": f"h.{payload}.s",
                        "access_token": access,
                        "refresh_token": refresh,
                        "account_id": account,
                    },
                    "last_refresh": last_refresh,
                }
            ),
            encoding="utf-8",
        )

    def test_the_era_follows_the_codex_account(self, tmp_path):
        from kiro_crew.providers.chat_share import codex_spawn_identity

        env = {"CODEX_HOME": str(tmp_path)}
        auth = tmp_path / "auth.json"
        missing = codex_spawn_identity(env)
        # No file: the adapter reads a key from the environment, which a running
        # gateway can change, so nothing stable keys the process: no sharing.
        assert missing == ""
        self._write_chatgpt_auth(
            auth, account="acct-A", user="u-1", access="a1", refresh="r1", last_refresh="t1"
        )
        signed_in_a = codex_spawn_identity(env)
        assert signed_in_a.startswith("codex:")
        # Another account, or another person in the same workspace, is a new era.
        self._write_chatgpt_auth(
            auth, account="acct-B", user="u-1", access="a1", refresh="r1", last_refresh="t1"
        )
        assert codex_spawn_identity(env) not in (signed_in_a, missing)
        self._write_chatgpt_auth(
            auth, account="acct-A", user="u-2", access="a1", refresh="r1", last_refresh="t1"
        )
        assert codex_spawn_identity(env) != signed_in_a
        # The era names the account, never a secret from the file.
        assert "acct" not in codex_spawn_identity(env)

    def test_another_codex_home_with_the_same_login_is_another_era(self, tmp_path):
        """The home holds the config the process loads and the store its threads go
        to, so a CODEX_HOME moved to a copy of the same login founds a new process."""
        from kiro_crew.providers.chat_share import codex_spawn_identity

        eras = []
        for name in ("home-a", "home-b"):
            home = tmp_path / name
            home.mkdir()
            self._write_chatgpt_auth(
                home / "auth.json",
                account="acct-A",
                user="u-1",
                access="a1",
                refresh="r1",
                last_refresh="t1",
            )
            eras.append(codex_spawn_identity({"CODEX_HOME": str(home)}))
        assert eras[0].startswith("codex:") and eras[0] != eras[1]

    def test_the_home_is_the_one_codex_home_resolves(self, tmp_path, monkeypatch):
        """``codex_transcripts.codex_home`` is the one reading of the codex home: the
        overlay's CODEX_HOME wins over the gateway's, an overlay without one falls
        back to it, an overlay HOME moves ``~/.codex``, and a relative home (which
        codex resolves from a cwd the gateway cannot name) fails closed."""
        from kiro_crew import platform_compat
        from kiro_crew.providers.chat_share import codex_spawn_identity

        homes = {}
        for name in ("gw", "overlay", "user/.codex"):
            home = tmp_path / name
            home.mkdir(parents=True)
            self._write_chatgpt_auth(
                home / "auth.json",
                account="acct-A",
                user="u-1",
                access="a1",
                refresh="r1",
                last_refresh="t1",
            )
            homes[name] = home
        monkeypatch.setenv("CODEX_HOME", str(homes["gw"]))
        gateway = codex_spawn_identity({})
        assert gateway.startswith("codex:")
        assert codex_spawn_identity(None) == gateway
        overlaid = codex_spawn_identity({"CODEX_HOME": str(homes["overlay"])})
        assert overlaid.startswith("codex:") and overlaid != gateway
        assert codex_spawn_identity({"CODEX_HOME": "relative/home"}) == ""
        if not platform_compat.IS_WINDOWS:
            monkeypatch.delenv("CODEX_HOME")
            moved = codex_spawn_identity({"HOME": str(tmp_path / "user")})
            assert moved.startswith("codex:") and moved not in (gateway, overlaid)

    def test_a_token_refresh_keeps_the_era(self, tmp_path):
        """codex rewrites tokens and last_refresh on every refresh; a later chat must
        still join the process the earlier one founded."""
        from kiro_crew.providers.chat_share import codex_spawn_identity

        env = {"CODEX_HOME": str(tmp_path)}
        auth = tmp_path / "auth.json"
        self._write_chatgpt_auth(
            auth, account="acct-A", user="u-1", access="a1", refresh="r1", last_refresh="t1"
        )
        before = codex_spawn_identity(env)
        self._write_chatgpt_auth(
            auth, account="acct-A", user="u-1", access="a2", refresh="r2", last_refresh="t2"
        )
        assert codex_spawn_identity(env) == before

    def test_an_api_key_sign_in_is_keyed_by_the_key(self, tmp_path):
        from kiro_crew.providers.chat_share import codex_spawn_identity

        env = {"CODEX_HOME": str(tmp_path)}
        auth = tmp_path / "auth.json"
        auth.write_text('{"OPENAI_API_KEY": "sk-one", "tokens": null}', encoding="utf-8")
        one = codex_spawn_identity(env)
        assert one.startswith("codex:") and "sk-one" not in one
        auth.write_text('{"OPENAI_API_KEY": "sk-two", "tokens": null}', encoding="utf-8")
        assert codex_spawn_identity(env) not in ("", one)

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            '{"tokens": {"access_token": "a"}}',
            '{"OPENAI_API_KEY": null, "tokens": null}',
        ],
    )
    def test_a_file_naming_no_account_fails_closed(self, tmp_path, body):
        from kiro_crew.providers.chat_share import codex_spawn_identity

        (tmp_path / "auth.json").write_text(body, encoding="utf-8")
        assert codex_spawn_identity({"CODEX_HOME": str(tmp_path)}) == ""

    def test_an_unreadable_credential_fails_closed(self, tmp_path):
        from kiro_crew.providers.chat_share import codex_spawn_identity

        (tmp_path / "auth.json").mkdir()
        assert codex_spawn_identity({"CODEX_HOME": str(tmp_path)}) == ""

    def test_the_claude_era_follows_the_signed_in_account(self, tmp_path):
        import json

        from kiro_crew.providers.chat_share import _self_signed_era, claude_spawn_identity

        env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
        cfg = tmp_path / ".claude.json"
        missing = claude_spawn_identity(env)
        assert missing and missing == claude_spawn_identity(env)
        cfg.write_text(json.dumps({"numStartups": 1}), encoding="utf-8")
        no_account = claude_spawn_identity(env)
        assert no_account and no_account not in (missing,)

        def signed_in(account: str, **rest: object) -> str:
            data = {"oauthAccount": {"accountUuid": account, "organizationUuid": "org-1"}, **rest}
            cfg.write_text(json.dumps(data), encoding="utf-8")
            return claude_spawn_identity(env)

        a = signed_in("acct-A")
        assert a.startswith("claude:") and a not in (missing, no_account)
        # Claude Code rewrites its config and its credentials file all the time
        # (counters, token refresh, MCP sign-ins); none of that moves the era.
        assert signed_in("acct-A", numStartups=7) == a
        (tmp_path / ".credentials.json").write_text('{"token": "x"}', encoding="utf-8")
        assert claude_spawn_identity(env) == a
        assert signed_in("acct-B") not in (a, missing, no_account)
        assert "acct" not in claude_spawn_identity(env)
        cfg.write_text("{not json", encoding="utf-8")
        assert claude_spawn_identity(env) == ""
        # KAS reads Kiro's sign-in, so it has no self-signed era.
        assert _self_signed_era(ACP_BACKEND_KAS) is None

    def test_the_claude_era_follows_the_credential_the_process_inherits(self, tmp_path):
        """With no Claude Code account, a key or endpoint the gateway's environment
        or the start's ``extra_env`` changes founds a new process."""
        from kiro_crew.providers.chat_share import _self_signed_era, claude_spawn_identity

        env = {"CLAUDE_CONFIG_DIR": str(tmp_path), "ANTHROPIC_API_KEY": "sk-old"}
        old = claude_spawn_identity(env)
        assert old == claude_spawn_identity(dict(env))
        assert "sk-old" not in old
        rotated = claude_spawn_identity({**env, "ANTHROPIC_API_KEY": "sk-new"})
        assert rotated != old
        moved = claude_spawn_identity({**env, "ANTHROPIC_BASE_URL": "https://proxy.example"})
        assert moved not in (old, rotated)
        bedrock = claude_spawn_identity({**env, "CLAUDE_CODE_USE_BEDROCK": "1"})
        assert bedrock not in (old, rotated, moved)
        # A Claude Code token, an AWS identity and a Vertex credential each move it.
        seen = {old, rotated, moved, bedrock}
        for name, value in (
            ("CLAUDE_CODE_OAUTH_TOKEN", "tok-2"),
            ("AWS_PROFILE", "other"),
            ("AWS_ACCESS_KEY_ID", "AKIDOTHER"),
            ("CLOUD_ML_REGION", "europe-west4"),
            ("GOOGLE_APPLICATION_CREDENTIALS", "/creds/other.json"),
        ):
            era = claude_spawn_identity({**env, name: value})
            assert era not in seen, name
            seen.add(era)
        # An unrelated variable is no credential.
        assert claude_spawn_identity({**env, "HOME_THEME": "dark"}) == old
        # The start's extra_env counts, over the gateway's environment.
        assert claude_spawn_identity(env, {"ANTHROPIC_API_KEY": "sk-new"}) == rotated
        assert (
            _self_signed_era(ACP_BACKEND_CLAUDE, {"CLAUDE_CONFIG_DIR": str(tmp_path)}) is not None
        )

    def test_the_goose_era_follows_the_provider_not_its_secrets(self, tmp_path):
        from kiro_crew.providers.chat_share import goose_spawn_identity

        env = {"GOOSE_PATH_ROOT": str(tmp_path)}
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        cfg = config_dir / "config.yaml"
        # No config and no provider in the environment: no account can be named.
        assert goose_spawn_identity(env) == ""

        def provider(name: str, host: str, **rest: object) -> str:
            lines = [f"GOOSE_PROVIDER: {name}", f"OPENAI_HOST: {host}"]
            lines += [f"{key}: {value}" for key, value in rest.items()]
            cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return goose_spawn_identity(env)

        a = provider("openai", "https://a.example")
        assert a.startswith("goose:") and "openai" not in a
        # goose writes the last model it ran and other keys while it runs, and a
        # token refresh rewrites its secrets; none of that moves the era.
        assert provider("openai", "https://a.example", GOOSE_MODEL="m2") == a
        (config_dir / "secrets.yaml").write_text("OPENAI_API_KEY: k2\n", encoding="utf-8")
        assert goose_spawn_identity(env) == a
        # Another endpoint or provider founds a new process.
        assert provider("openai", "https://b.example") not in ("", a)
        assert provider("anthropic", "https://a.example") not in ("", a)
        # The environment names the provider over the file, as goose resolves it.
        provider("openai", "https://a.example")
        assert goose_spawn_identity({**env, "GOOSE_PROVIDER": "anthropic"}) != a
        # An endpoint only the environment names counts too, and its change moves it.
        only_env = {**env, "OLLAMA_HOST": "http://h1"}
        assert goose_spawn_identity(only_env) not in ("", a)
        assert goose_spawn_identity({**env, "OLLAMA_HOST": "http://h2"}) != goose_spawn_identity(
            only_env
        )
        # A key the environment hands the child is in it: a rotated one founds a new
        # process, the same one joins, and the era carries nothing of the key.
        keyed = {**env, "OPENAI_API_KEY": "sk-old"}
        old = goose_spawn_identity(keyed)
        assert old not in ("", a) and "sk-old" not in old
        assert goose_spawn_identity(dict(keyed)) == old
        assert goose_spawn_identity({**env, "OPENAI_API_KEY": "sk-new"}) not in ("", old)
        assert goose_spawn_identity({**env, "AWS_PROFILE": "p2"}) != a
        # Another goose home with the same provider, hosts and keys founds a new
        # process: its config, credential and session stores are another home's.
        other = tmp_path / "other"
        (other / "config").mkdir(parents=True)
        (other / "config" / "config.yaml").write_text(cfg.read_text(encoding="utf-8"))
        assert goose_spawn_identity({"GOOSE_PATH_ROOT": str(other)}) not in ("", a)
        assert goose_spawn_identity({**env, "XDG_DATA_HOME": "/elsewhere"}) not in ("", a)
        assert goose_spawn_identity(dict(env)) == a
        # Unreadable or not a mapping: fail closed.
        cfg.write_text("key: [unclosed\n", encoding="utf-8")
        assert goose_spawn_identity(env) == ""
        cfg.write_text("- a list\n", encoding="utf-8")
        assert goose_spawn_identity(env) == ""

    def test_the_goose_era_reads_the_provider_agent_env_hands_the_child(
        self, tmp_path, monkeypatch
    ):
        """``agent.env`` reaches goose's environment, so a provider named there counts."""
        from kiro_crew.acp import agent_env
        from kiro_crew.providers.chat_share import goose_spawn_identity

        monkeypatch.setenv("GOOSE_PATH_ROOT", str(tmp_path))
        monkeypatch.delenv("GOOSE_PROVIDER", raising=False)
        monkeypatch.setattr(agent_env, "agent_env_overlay", lambda: {})
        assert goose_spawn_identity() == ""
        monkeypatch.setattr(agent_env, "agent_env_overlay", lambda: {"GOOSE_PROVIDER": "openai"})
        assert goose_spawn_identity().startswith("goose:")
        # The start's extra_env counts too, over the gateway's environment.
        assert goose_spawn_identity(env_overrides={"OPENAI_API_KEY": "k"}) not in (
            "",
            goose_spawn_identity(),
        )

    def _real_confirmation(self, monkeypatch, codex_eras):
        import kiro_crew.providers.chat_share as share_mod
        import kiro_crew.sandbox as sandbox_mod

        pending = list(codex_eras)
        monkeypatch.setattr(
            share_mod,
            "codex_spawn_identity",
            lambda env=None: pending.pop(0) if len(pending) > 1 else pending[0],
        )
        monkeypatch.setattr(share_mod, "agent_spec_generation", lambda w, a: "gen-1")
        monkeypatch.setattr(share_mod, "_kas_telemetry_for", lambda *a: True)
        monkeypatch.setattr(sandbox_mod, "effective_sandbox_mode", lambda mode: "auto")
        monkeypatch.setattr(sandbox_mod, "_forward_ssh_auth_sock", lambda mode, hidden: False)

    def test_codex_chats_with_no_kiro_sign_in_share_one_process(self, fresh_table, monkeypatch):
        """The codex-only operator: no Kiro era at all, one codex credential."""
        self._real_confirmation(monkeypatch, ["codex:aaa"])
        rt = _PlacedRuntime(backend=ACP_BACKEND_CODEX)
        spawn = spawner(rt)

        async def scenario():
            starts = [
                make_placement(
                    provider=_PlacementProvider(era="", backend=ACP_BACKEND_CODEX),
                    session_key=f"dashboard:chat-{i}-1790000000",
                    harness_era="codex:aaa",
                )
                for i in (1, 2)
            ]
            return [await p.place(spawn) for p in starts], starts

        placed, starts = asyncio.run(scenario())
        assert placed == [rt, rt]
        assert spawn.calls == [rt]
        assert [p.joined for p in starts] == [False, True]

    def test_a_codex_sign_in_that_moves_during_placement_never_joins(
        self, fresh_table, monkeypatch
    ):
        """The confirm re-reads the CODEX credential: a sign-in to another codex
        account landing in the window founds a new process on the fresh key."""
        rt_a, rt_b = _PlacedRuntime(backend=ACP_BACKEND_CODEX), _PlacedRuntime(
            pid=4343, backend=ACP_BACKEND_CODEX
        )
        spawn = spawner(rt_a, rt_b)

        async def scenario():
            founder = make_placement(
                provider=_PlacementProvider(era="", backend=ACP_BACKEND_CODEX),
                harness_era="codex:aaa",
            )
            joiner = make_placement(
                provider=_PlacementProvider(era="", backend=ACP_BACKEND_CODEX),
                session_key="dashboard:chat-2-1790000000",
                harness_era="codex:aaa",
            )
            self._real_confirmation(monkeypatch, ["codex:aaa"])
            await founder.place(spawn)
            self._real_confirmation(monkeypatch, ["codex:bbb"])
            return await joiner.place(spawn), joiner

        placed, joiner = asyncio.run(scenario())
        assert placed is rt_b
        assert joiner.joined is False
