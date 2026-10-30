"""External logout detection tests: which sessions an identity change retires.

The provider membership predicate, the retirement sweep's coverage of every holder,
and the warm-pool identity fence.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from external_logout_detection_helpers import _private_sel_root_per_test  # noqa: F401
from external_logout_detection_helpers import (
    _DeadProvider,
    _FakeProvider,
    _FakeRuntime,
)
from source_corpus import parsed_candidates, src_root

from kiro_crew import kiro_prerequisite as kp
from kiro_crew.session_allocation import (
    FOREGROUND_COLD_START_RESERVE as _COLD_START_RESERVE_FOR_TEST,
)
from kiro_crew.session_allocation import MAX_CONCURRENT_COLD_STARTS as _MAX_COLD_STARTS_FOR_TEST
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked


class TestProviderMembership:
    def test_kiro_backend_is_a_member(self) -> None:
        from kiro_crew.session import _provider_uses_kiro_identity_store

        assert _provider_uses_kiro_identity_store(_FakeProvider(""))

    def test_unknown_backend_fails_closed(self) -> None:
        """An object that declares nothing must be left running, not recycled."""

        from kiro_crew.session import _provider_uses_kiro_identity_store

        assert not _provider_uses_kiro_identity_store(object())
        assert not _provider_uses_kiro_identity_store(_FakeProvider("claude"))

    def test_the_capability_is_declared_on_the_provider_abc(self) -> None:
        """harness-parity H14: the session layer reads a declared capability.

        Probing private shapes (``_client``, ``_acp_backend``) would silently
        misclassify an adapted provider. The base declares it with a safe default
        so a harness that never states the claim cannot inherit it.
        """

        from kiro_crew.providers.base import LLMProvider

        assert "uses_kiro_identity_store" in vars(LLMProvider)
        assert LLMProvider.uses_kiro_identity_store.fget(object()) is False  # type: ignore[attr-defined]

    def test_a_non_declaring_provider_is_not_swept(self) -> None:
        from kiro_crew.session import _provider_uses_kiro_identity_store

        class _Bare:
            pass

        assert _provider_uses_kiro_identity_store(_Bare()) is False


class TestRetirementCoverage:
    """Every holder of a kiro child must be reachable by retirement."""

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        # pool_size 0 so construction never pre-spawns; the pool is populated
        # explicitly by the test that cares about it.
        return SessionManager(KiroCrewConfig())

    @staticmethod
    def _stored_sid(smap, key: str) -> str:
        """Read the stored sid WITHOUT ``get``'s transcript-file check.

        ``SessionMap.get`` stats ``<sid>.json`` and prunes an entry whose
        transcript is missing, which would answer "no pointer" for a reason this
        test is not about. The question here is only whether the retirement
        dropped the pointer, so read the entry.
        """

        from kiro_crew.session_map import canonical_key

        entry = smap._session_map._data.get(canonical_key(key)) or {}
        return entry.get("sid", "")

    @staticmethod
    def _session(provider: object, *, busy: bool = False):
        from kiro_crew.session import _Session

        sess = _Session(provider=provider)  # type: ignore[arg-type]
        if busy:
            # Retirement reads semaphore.locked(); drain the permit to say "busy".
            sess.semaphore._value = 0  # type: ignore[attr-defined]
        return sess

    @pytest.mark.asyncio
    async def test_idle_kiro_sessions_are_retired_and_others_left_alone(self) -> None:
        smap = self._manager()
        kiro = _FakeProvider("")
        claude = _FakeProvider("claude")
        smap._sessions["kiro-key"] = self._session(kiro)
        smap._sessions["claude-key"] = self._session(claude)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["kiro-key"]
        assert complete is True
        assert "kiro-key" not in smap._sessions
        assert "claude-key" in smap._sessions
        assert kiro.shutdown_calls == 1
        assert claude.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_a_busy_kiro_session_is_not_retired(self) -> None:
        smap = self._manager()
        busy = _FakeProvider("")
        smap._sessions["busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        # A skipped session means the change is NOT reconciled.
        assert complete is False
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_selection_and_unregistration_are_one_atomic_step(self) -> None:
        """A session busy at DECISION time is never unregistered or shut down.

        The TOCTOU GPT flagged is closed by doing the idle check and the
        unregistration in a single lock hold. The acquire path takes the
        semaphore OUTSIDE the lock and then validates registration INSIDE it, so
        the two orderings are both safe: acquire-first is visible here as
        ``locked()`` and skipped, while pop-first is caught by that validation
        (covered by the next test).
        """

        smap = self._manager()
        idle = _FakeProvider("")
        busy = _FakeProvider("")
        smap._sessions["idle"] = self._session(idle)
        smap._sessions["busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["idle"]
        assert complete is False  # the busy one was left running
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_a_turn_can_never_stream_on_a_retired_provider(self) -> None:
        """The other half of the race: a turn that acquires AFTER the pop.

        It re-validates registration under the same lock, finds the entry gone,
        releases its semaphore and reports invalid -- so the caller cold starts a
        replacement on the current account instead of streaming on the retired
        child. Without that, retirement would be racing every in-flight acquire.
        """

        smap = self._manager()
        provider = _FakeProvider("")
        sess = self._session(provider)
        smap._sessions["key"] = sess

        await smap.retire_kiro_identity_sessions()
        assert "key" not in smap._sessions

        # A turn that had not yet acquired when the sweep ran now tries to.
        still_valid = await smap._reacquire_and_validate("key", sess)

        assert still_valid is False
        # The contract is that an invalid result has ALREADY released the
        # semaphore; a leaked permit would deadlock the key forever.
        assert not sess.semaphore.locked()

    @pytest.mark.asyncio
    async def test_pooled_kiro_providers_are_discarded(self) -> None:
        """A warm provider spawned pre-change would otherwise be handed to a
        brand-new session, running it as the previous account."""

        smap = self._manager()
        pooled_kiro = _FakeProvider("")
        pooled_other = _FakeProvider("claude")
        smap._warm_pool.put_nowait((pooled_kiro, 0.0))
        smap._warm_pool.put_nowait((pooled_other, 0.0))

        await smap.retire_kiro_identity_sessions()

        assert pooled_kiro.shutdown_calls == 1
        assert pooled_other.shutdown_calls == 0
        # The non-kiro entry is put back, not dropped on the floor.
        assert smap._warm_pool.qsize() == 1
        survivor, _ = smap._warm_pool.get_nowait()
        assert survivor is pooled_other

    @pytest.mark.asyncio
    async def test_kiro_subagent_runtimes_are_retired(self) -> None:
        smap = self._manager()

        kiro_runtime = _FakeRuntime("")
        other_runtime = _FakeRuntime("claude")
        smap._subagent_runtimes["parent-kiro"] = kiro_runtime  # type: ignore[assignment]
        smap._subagent_runtimes["parent-other"] = other_runtime  # type: ignore[assignment]

        await smap.retire_kiro_identity_sessions()

        assert kiro_runtime.killed == 1
        assert kiro_runtime.kill_reasons == ["subagent runtime released"]
        assert other_runtime.killed == 0
        assert "parent-other" in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_the_shared_background_runtime_is_retired(self) -> None:
        """One process serves all background work and outlives every session, so
        the session sweep cannot reach it."""

        smap = self._manager()

        bg = _FakeRuntime("")
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 1
        assert smap._bg_runtime is None
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_non_kiro_background_runtime_is_left_alone(self) -> None:
        smap = self._manager()

        bg = _FakeRuntime("claude")
        smap._bg_runtime = bg  # type: ignore[assignment]

        await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg

    @pytest.mark.asyncio
    async def test_an_active_background_runtime_is_spared_and_reported_incomplete(self) -> None:
        """One process serves every background caller, so killing it mid-flight
        drops work belonging to callers unrelated to the account change.

        Same principle as a busy session: spare it, report the sweep incomplete so
        the change stays pending, and retire it once it drains.
        """

        smap = self._manager()
        bg = _FakeRuntime("", active=True)
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is False

    @pytest.mark.asyncio
    async def test_an_active_subagent_runtime_is_spared_and_reported_incomplete(self) -> None:
        smap = self._manager()
        busy_runtime = _FakeRuntime("", active=True)
        smap._subagent_runtimes["parent"] = busy_runtime  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert busy_runtime.killed == 0
        assert "parent" in smap._subagent_runtimes
        assert complete is False

    # -- The live-account spare ---------------------------------------------
    #
    # A holder whose spawn stamp EQUALS the live fingerprint provably
    # authenticated as the live account. Retiring it buys nothing and costs
    # its in-flight children, and counting it against completeness keeps a
    # sweep on a busy host from finishing: every turn re-sweeps and every
    # idle parent's subagents die as "provider shutdown".

    LIVE = "cli-live"

    @pytest.mark.asyncio
    async def test_an_idle_session_stamped_with_the_live_account_is_spared(self) -> None:
        smap = self._manager()
        spared = _FakeProvider("")
        spared.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        unstamped = _FakeProvider("")
        other = _FakeProvider("")
        other.spawn_identity = "cli-other"  # type: ignore[attr-defined]
        smap._sessions["spared"] = self._session(spared)
        smap._sessions["unstamped"] = self._session(unstamped)
        smap._sessions["other"] = self._session(other)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert sorted(retired) == ["other", "unstamped"]
        assert complete is True
        assert "spared" in smap._sessions
        assert spared.shutdown_calls == 0
        assert not smap._sessions["spared"].retire_on_identity_change
        assert unstamped.shutdown_calls == 1
        assert other.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_a_busy_session_stamped_with_the_live_account_does_not_block_completion(
        self,
    ) -> None:
        """This is the livelock: a busy live-account holder kept every sweep
        incomplete, so the baseline never advanced and the next turn swept
        again. It is not holding the previous account, so it is not a reason
        to retry."""

        smap = self._manager()
        busy = _FakeProvider("")
        busy.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._sessions["busy-live"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert "busy-live" in smap._sessions
        assert not smap._sessions["busy-live"].retire_on_identity_change
        assert smap.pending_identity_sweep_fingerprint == ""

    @pytest.mark.asyncio
    async def test_a_demuxed_session_is_spared_through_its_runtime_stamp(self) -> None:
        """A demuxed session's provider carries no stamp of its own; the shared
        runtime it rides does."""

        smap = self._manager()
        provider = _FakeProvider("")
        provider._runtime = SimpleNamespace(spawn_identity=self.LIVE)  # type: ignore[attr-defined]
        smap._sessions["demuxed"] = self._session(provider)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert provider.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_the_spare_needs_the_whole_fingerprint_to_match(self) -> None:
        """Sparing is stricter than the mismatch gate: a stamp that agrees on
        the CLI component but lost or differs on the vault component is not
        proof of the live account, so it is retired like any other."""

        smap = self._manager()
        partial = _FakeProvider("")
        partial.spawn_identity = "cli-live"  # type: ignore[attr-defined]
        smap._sessions["partial"] = self._session(partial)
        live = kp._combine_identity_fingerprints("cli-live", "vault-x")

        retired, _ = await smap.retire_kiro_identity_sessions(fingerprint=live)

        assert retired == ["partial"]
        assert partial.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_store_spares_nothing(self) -> None:
        """An empty live fingerprint means "cannot tell"; a stamped holder is
        then retired exactly as before the spare existed (the sign-out path
        sweeps with no fingerprint on purpose)."""

        smap = self._manager()
        stamped = _FakeProvider("")
        stamped.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._sessions["stamped"] = self._session(stamped)

        retired, _ = await smap.retire_kiro_identity_sessions(fingerprint="")

        assert retired == ["stamped"]
        assert stamped.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_a_live_stamped_subagent_runtime_is_spared_even_when_busy(self) -> None:
        smap = self._manager()
        live_runtime = _FakeRuntime("", active=True)
        live_runtime.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        old_runtime = _FakeRuntime("")
        smap._subagent_runtimes["parent-live"] = live_runtime  # type: ignore[assignment]
        smap._subagent_runtimes["parent-old"] = old_runtime  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert live_runtime.killed == 0
        assert "parent-live" in smap._subagent_runtimes
        assert old_runtime.killed == 1
        assert "parent-old" not in smap._subagent_runtimes
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_release_pinned_to_a_stale_runtime_spares_its_replacement(self) -> None:
        """The sweep decides from a snapshot, then waits for the per-parent
        lock. A respawn holding that lock installs a live-stamped replacement
        under the same key before letting go; the release must not pop it."""

        smap = self._manager()
        stale = _FakeRuntime("")
        replacement = _FakeRuntime("", active=True)
        replacement.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._subagent_runtimes["parent"] = replacement  # type: ignore[assignment]
        smap._subagent_runtime_locks["parent"] = asyncio.Lock()

        released = await smap.release_subagent_runtime("parent", expected=stale)

        assert released is False
        assert replacement.killed == 0
        assert smap._subagent_runtimes["parent"] is replacement
        assert "parent" in smap._subagent_runtime_locks

    @pytest.mark.asyncio
    async def test_a_release_pinned_to_the_registered_runtime_still_kills_it(self) -> None:
        smap = self._manager()
        runtime = _FakeRuntime("")
        smap._subagent_runtimes["parent"] = runtime  # type: ignore[assignment]

        released = await smap.release_subagent_runtime("parent", expected=runtime)

        assert released is True
        assert runtime.killed == 1
        assert "parent" not in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_replacement_installed_mid_sweep_survives_and_the_sweep_completes(
        self,
    ) -> None:
        """Same interleaving through the sweep itself: the idle probe on the
        stale runtime is the suspension the respawn wins, so a swap there
        models a replacement landing between the snapshot and the release."""

        smap = self._manager()
        replacement = _FakeRuntime("", active=True)
        replacement.spawn_identity = self.LIVE  # type: ignore[attr-defined]

        class _StaleRuntime(_FakeRuntime):
            def has_active_or_initializing_sessions(self) -> bool:
                smap._subagent_runtimes["parent"] = replacement  # type: ignore[assignment]
                return False

        stale = _StaleRuntime("")
        smap._subagent_runtimes["parent"] = stale  # type: ignore[assignment]

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert stale.killed == 0
        assert replacement.killed == 0
        assert smap._subagent_runtimes["parent"] is replacement
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_live_stamped_background_runtime_is_spared_even_when_busy(self) -> None:
        smap = self._manager()
        bg = _FakeRuntime("", active=True)
        bg.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is True

    # -- Channel members ----------------------------------------------------
    #
    # A channel member holds its lease for the channel's whole life, so the
    # semaphore test reads it as busy even while it only listens.

    def _channel_member(self, smap, key: str, stamp: str):
        provider = _FakeProvider("")
        if stamp:
            provider.spawn_identity = stamp  # type: ignore[attr-defined]
        smap._sessions[key] = self._session(provider, busy=True)
        assert smap.mark_lifecycle_lease(key), "precondition: the lease was not declared"
        return provider

    @pytest.mark.asyncio
    async def test_a_live_stamped_channel_member_does_not_block_completion(self) -> None:
        smap = self._manager()
        member = self._channel_member(smap, "channel:c1:m1", self.LIVE)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert member.shutdown_calls == 0
        assert not smap._sessions["channel:c1:m1"].retire_on_identity_change
        assert smap.pending_identity_sweep_fingerprint == ""
        assert smap.identity_sweep_waiting_on == ()

    @pytest.mark.asyncio
    async def test_an_old_account_channel_member_is_named_as_the_holdout(self) -> None:
        smap = self._manager()
        member = self._channel_member(smap, "channel:c1:m1", "cli-old")
        busy = _FakeProvider("")
        smap._sessions["chat-busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is False
        assert member.shutdown_calls == 0
        assert smap.identity_sweep_waiting_on == (
            "channel:c1:m1 (channel member)",
            "chat-busy (busy)",
        )

        # Once the holdouts are gone the next sweep completes and clears the list.
        del smap._sessions["channel:c1:m1"]
        del smap._sessions["chat-busy"]
        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)
        assert complete is True
        assert smap.identity_sweep_waiting_on == ()

    @pytest.mark.asyncio
    async def test_a_provider_mid_start_makes_the_sweep_incomplete(self) -> None:
        """A provider between start() and registration is in none of the maps.

        It already holds whatever the store said when it spawned, so reconciling
        while it is in flight would advance the baseline over it and leave it
        reusable under the previous account once it registers.
        """

        smap = self._manager()
        smap._starting_pids.add(4242)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False

    @pytest.mark.asyncio
    async def test_no_in_flight_start_allows_a_complete_sweep(self) -> None:
        smap = self._manager()
        assert not smap._starting_pids

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is True

    @pytest.mark.asyncio
    async def test_an_in_flight_runtime_spawn_makes_the_sweep_incomplete(self) -> None:
        """`get_subagent_runtime` holds the per-parent lock across its spawn.

        A runtime being created right now is in no map at all, while it already
        holds whatever the store said when it started. Reconciling would advance
        the baseline over it and leave later subagents running as the previous
        account.
        """

        smap = self._manager()
        lock = asyncio.Lock()
        await lock.acquire()
        smap._subagent_runtime_locks["parent"] = lock

        _, complete = await smap.retire_kiro_identity_sessions()
        assert complete is False

        lock.release()
        _, complete_after = await smap.retire_kiro_identity_sessions()
        assert complete_after is True

    @pytest.mark.asyncio
    async def test_an_idle_runtime_lock_does_not_block_reconciliation(self) -> None:
        """A lock that merely EXISTS is not a spawn in flight."""

        smap = self._manager()
        smap._subagent_runtime_locks["parent"] = asyncio.Lock()

        _, complete = await smap.retire_kiro_identity_sessions()
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_busy_session_is_marked_so_its_next_turn_cannot_reuse_it(self) -> None:
        """Skipping a busy session protected its turn but not the NEXT one.

        `get_or_create` would simply wait for that turn's semaphore and hand the
        same old-account provider to the following turn. Marking it makes the
        post-semaphore re-validate report invalid, so the caller's existing
        stale-provider path evicts and cold starts -- no blocking, no refusal.
        """

        smap = self._manager()
        busy = _FakeProvider("")
        sess = self._session(busy, busy=True)
        smap._sessions["busy"] = sess

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is False
        assert sess.retire_on_identity_change is True
        assert busy.shutdown_calls == 0, "the in-flight turn must not be killed"

        # The next turn releases and re-validates: the mark makes it invalid.
        sess.semaphore.release()
        still_valid = await smap._reacquire_and_validate("busy", sess)
        assert still_valid is False
        assert not sess.semaphore.locked(), "an invalid result must release the permit"

    @pytest.mark.asyncio
    async def test_an_unmarked_session_still_validates_normally(self) -> None:
        """The mark must not make every re-validate fail."""

        smap = self._manager()
        provider = _FakeProvider("claude")
        sess = self._session(provider)
        smap._sessions["ok"] = sess

        assert sess.retire_on_identity_change is False
        still_valid = await smap._reacquire_and_validate("ok", sess)
        assert still_valid is True
        smap.release("ok")

    @pytest.mark.asyncio
    async def test_a_runtime_surviving_the_sweep_reports_incomplete(self) -> None:
        """Post-condition, not a window enumeration.

        A companion spawn that COMPLETES between the runtime snapshot and the final
        lock check is in neither -- its lock is released and it was not in the
        snapshot. Asserting that no kiro-backed runtime is LEFT catches anything
        installed while we swept, whatever the timing.
        """

        smap = self._manager()
        survivor = _FakeRuntime("")
        smap._subagent_runtimes["parent"] = survivor  # type: ignore[assignment]

        # Simulate a release that does not actually remove it (equivalently: a
        # runtime installed after the snapshot was taken).
        async def _noop_release(key: str) -> None:
            return None

        smap.release_subagent_runtime = _noop_release  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert "parent" in smap._subagent_runtimes
        assert complete is False, "a surviving kiro-backed runtime must not reconcile"

    @pytest.mark.asyncio
    async def test_a_non_kiro_runtime_surviving_is_fine(self) -> None:
        """The post-condition must only count runtimes this sweep owns."""

        smap = self._manager()
        smap._subagent_runtimes["parent"] = _FakeRuntime("claude")  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is True

    @pytest.mark.asyncio
    async def test_an_initializing_session_protects_a_runtime(self) -> None:
        """`create_session` registers its queue OUTSIDE the runtime lock.

        So a session whose `session/new` is in flight is invisible to
        `has_active_sessions()`, and killing the runtime under it surfaces as
        `AcpRuntimeDead` on work the user never connected to an account change.
        The stale-runtime recycle path tolerates that window because a respawn
        loop backstops it; this sweep has no such backstop, so it must not.
        """

        smap = self._manager()
        initializing = _FakeRuntime("", initializing=True)
        smap._subagent_runtimes["parent"] = initializing  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert initializing.killed == 0
        assert "parent" in smap._subagent_runtimes
        assert complete is False

    @pytest.mark.asyncio
    async def test_an_initializing_session_protects_the_background_runtime(self) -> None:
        smap = self._manager()
        bg = _FakeRuntime("", initializing=True)
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is False

    @pytest.mark.asyncio
    async def test_the_runtime_predicate_counts_inits_in_flight(self) -> None:
        """Pins the real AcpRuntime property, not just the test double."""

        from kiro_crew.acp.runtime import AcpRuntime

        runtime = AcpRuntime.__new__(AcpRuntime)
        runtime._session_queues = {}  # type: ignore[attr-defined]
        runtime._session_inits_in_flight = 0  # type: ignore[attr-defined]
        assert runtime.has_active_sessions() is False
        assert runtime.has_active_or_initializing_sessions() is False

        runtime._session_inits_in_flight = 1  # type: ignore[attr-defined]
        # The old predicate still reports idle -- that is the window.
        assert runtime.has_active_sessions() is False
        assert runtime.has_active_or_initializing_sessions() is True

    @pytest.mark.asyncio
    async def test_two_concurrent_sweeps_do_not_deadlock(self) -> None:
        """Two sweeps at once are serialized, and both complete.

        Each sweep takes every cold-start permit through ``PrioritySemaphore.drain``,
        which is not re-entrant: a second drain started while one is pending raises.
        ``_identity_sweep_lock`` is what lets the second sweep wait for the first
        instead. Two concurrent sweeps are the common boot case, not an exotic one:
        with ``_session_identity`` unset, every in-flight turn sees a change at once.
        """
        from kiro_crew.start_priority import StartPriority

        smap = self._manager()
        # Every permit held -- background its full width, a person the reserve -- so
        # both sweeps are waiting before any permit is free.
        holds = [StartPriority.BACKGROUND] * _MAX_COLD_STARTS_FOR_TEST + [
            StartPriority.FOREGROUND
        ] * _COLD_START_RESERVE_FOR_TEST
        for priority in holds:
            await await_until(asyncio.ensure_future(smap._start_sem.acquire(priority)))

        first = asyncio.create_task(smap.retire_kiro_identity_sessions())
        second = asyncio.create_task(smap.retire_kiro_identity_sessions())
        # The first sweep holds the sweep lock and is draining the permits; the
        # second is parked on that lock. Both are observed waiting, not merely
        # not-yet-started.
        await async_wait_until(
            lambda: smap._start_sem._drain_held is not None,
            describe=lambda: f"first.done()={first.done()}",
        )
        await until_parked(smap._identity_sweep_lock)
        assert not first.done() and not second.done()

        for priority in holds:
            smap._start_sem.release(priority)
            await asyncio.sleep(0)

        results = await await_until(asyncio.ensure_future(asyncio.gather(first, second)))
        assert all(complete for _, complete in results)
        # Every permit returned, so later cold starts are unaffected.
        assert smap._start_sem._value == smap._start_sem._limit

    @pytest.mark.asyncio
    async def test_the_barrier_waits_for_an_in_flight_cold_start(self) -> None:
        """The scan must be authoritative, so it WAITS for every permit.

        A partial barrier is not enough: reporting "incomplete" defers the baseline
        but does not stop the current turn, so an eager session spawned under the
        previous account would still win registration and serve it.
        """

        from kiro_crew.start_priority import StartPriority

        smap = self._manager()
        await await_until(asyncio.ensure_future(smap._start_sem.acquire(StartPriority.BACKGROUND)))

        task = asyncio.create_task(smap.retire_kiro_identity_sessions())
        # The sweep has started its drain and is waiting for the held permit.
        await async_wait_until(
            lambda: smap._start_sem._drain_held is not None,
            describe=lambda: f"task.done()={task.done()}",
        )
        assert not task.done(), "the sweep scanned while a cold start was in flight"

        smap._start_sem.release(StartPriority.BACKGROUND)
        retired, complete = await await_until(task)
        assert complete is True
        assert retired == []

    @pytest.mark.asyncio
    async def test_the_barrier_releases_every_permit_it_took(self) -> None:
        """A leaked permit would shrink cold-start concurrency for the process."""

        smap = self._manager()
        before = smap._start_sem._value
        await smap.retire_kiro_identity_sessions()
        assert smap._start_sem._value == before

    @pytest.mark.asyncio
    async def test_permits_are_released_even_when_the_scan_raises(self) -> None:
        """The release must be in a `finally`, or one failure degrades the process."""

        smap = self._manager()
        before = smap._start_sem._value

        class _Boom(Exception):
            pass

        async def _explode() -> bool:
            raise _Boom()

        smap._retire_kiro_warm_pool = _explode  # type: ignore[assignment]
        with pytest.raises(_Boom):
            await smap.retire_kiro_identity_sessions()
        assert smap._start_sem._value == before

    @pytest.mark.asyncio
    async def test_the_pool_drain_holds_the_fill_lock(self) -> None:
        """An in-flight fill must not land a pre-change child behind the sweep.

        Without the lock the sweep reads an empty queue, the outstanding spawn
        completes, and a provider authenticated as the old account is enqueued for
        a later session to claim.
        """

        smap = self._manager()
        observed: list[bool] = []

        original = smap._retire_kiro_warm_pool

        async def watched() -> bool:
            observed.append(smap._pool_fill_lock.locked())
            return await original()

        smap._retire_kiro_warm_pool = watched  # type: ignore[assignment]

        # Hold the fill lock as an in-flight fill would, and confirm the drain
        # cannot proceed until it is released.
        await smap._pool_fill_lock.acquire()
        task = asyncio.create_task(smap.retire_kiro_identity_sessions())
        # The drain is parked on the fill lock the in-flight fill holds.
        await until_parked(smap._pool_fill_lock, describe=lambda: f"task.done()={task.done()}")
        assert not task.done(), "drain proceeded while a fill held the lock"
        smap._pool_fill_lock.release()
        await task

        assert observed, "the drain never ran"

    @pytest.mark.asyncio
    async def test_a_retired_session_loses_its_native_conversation_pointer(self) -> None:
        """The successor must not ``session/load`` the previous account's history.

        An extended-thinking model's stored thinking blocks carry a provider
        signature bound to the conversation they were minted in. Reload one under
        a different account and the provider rejects the whole request with
        "Invalid `signature` in `thinking` block ... bound to a different
        conversation", and it repeats on every later turn because the cold start
        keeps resuming the same sid. Clearing the pointer is what makes the
        replacement child start a NEW native conversation.
        """

        smap = self._manager()
        kiro = _FakeProvider("")
        claude = _FakeProvider("claude")
        smap._sessions["kiro-key"] = self._session(kiro)
        smap._sessions["claude-key"] = self._session(claude)
        smap._session_map.set("kiro-key", "sid-old-account")
        smap._session_map.set("claude-key", "sid-untouched")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["kiro-key"]
        assert complete is True
        assert self._stored_sid(smap, "kiro-key") == ""
        # Only the pointer is dropped: the conversation stays recoverable.
        assert smap._session_map.get_discarded_sid("kiro-key") == "sid-old-account"
        # A provider that does not read the kiro identity store is untouched.
        assert self._stored_sid(smap, "claude-key") == "sid-untouched"

    @pytest.mark.asyncio
    async def test_a_busy_session_loses_its_pointer_during_the_sweep(self) -> None:
        """The running child keeps its in-process sid, but the map drops it now."""

        smap = self._manager()
        busy = _FakeProvider("")
        session = self._session(busy, busy=True)
        smap._sessions["busy"] = session
        smap._session_map.set("busy", "sid-old-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False
        assert session.retire_on_identity_change is True
        assert self._stored_sid(smap, "busy") == ""
        assert smap._session_map.get_discarded_sid("busy") == "sid-old-account"
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_retry_sweep_retires_the_successor_too(self) -> None:
        """A retry retires every Kiro session, successors included.

        Registration order cannot say which account a session authenticated
        under: a cold start that began before the switch registers after it, so
        sparing "whatever registered later" hands the old account a session the
        sweep was asked to retire. Retiring the successor costs it a fresh native
        conversation; its previous pointer stays recoverable.
        """

        smap = self._manager()
        old = _FakeProvider("")
        old_session = self._session(old, busy=True)
        smap._sessions["shared"] = old_session
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-old-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-old-account"

        old_session.semaphore.release()
        await smap._evict_stale_session("shared", old_session)
        successor = self._session(_FakeProvider("", sid="sid-new-account"))
        smap._sessions["shared"] = successor
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-new-account")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-b")

        assert retired == ["shared"]
        assert complete is True
        assert "shared" not in smap._sessions
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-new-account"

    @pytest.mark.asyncio
    async def test_retry_sweep_still_clears_a_marked_busy_holder(self) -> None:
        """A busy holder keeps its mark and loses its pointer on every retry."""

        smap = self._manager()
        old = _FakeProvider("")
        old_session = self._session(old, busy=True)
        smap._sessions["shared"] = old_session
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-old-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False

        # Model the marked generation trying to republish before the retry.
        smap._session_map.set("shared", "sid-old-account")
        _, complete = await smap.retire_kiro_identity_sessions("fp-b")

        assert complete is False
        assert old_session.retire_on_identity_change is True
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-old-account"

    @pytest.mark.asyncio
    async def test_complete_sweep_releases_the_pending_marker(self) -> None:
        """A completed sweep clears the marker, so a later change is not a retry."""

        smap = self._manager()
        first = self._session(_FakeProvider(""))
        smap._sessions["shared"] = first
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-first-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is True

        second = self._session(_FakeProvider(""))
        smap._sessions["shared"] = second
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-second-account")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-c")

        assert retired == ["shared"]
        assert complete is True
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-second-account"

    @pytest.mark.asyncio
    async def test_a_second_switch_during_an_incomplete_sweep_retires_the_successor(
        self,
    ) -> None:
        """A -> B with a busy A-session, then B -> C before it goes idle.

        The consumer baseline is still A, so the sweep re-fires. The successor
        registered under B holds a B-account conversation that account C would
        reject exactly like the original bug, so it is retired rather than spared
        for having registered later.
        """

        smap = self._manager()
        busy_a = self._session(_FakeProvider(""), busy=True)
        smap._sessions["busy"] = busy_a
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-a")
        smap._sessions["shared"] = self._session(_FakeProvider(""))
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-account-a")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert retired == ["shared"]
        assert complete is False

        successor_b = self._session(_FakeProvider("", sid="sid-account-b"))
        smap._sessions["shared"] = successor_b
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-account-b")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-c")

        assert retired == ["shared"]
        assert complete is False
        assert "shared" not in smap._sessions
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-account-b"
        assert busy_a.retire_on_identity_change is True
        assert self._stored_sid(smap, "busy") == ""

    @pytest.mark.asyncio
    async def test_a_sweep_back_to_the_baseline_retires_the_interim_holders(self) -> None:
        """A -> B incomplete, back to A, then a genuine A -> B.

        The return to A is itself swept, because an outstanding change is its own
        trigger and the reconciled baseline says nothing about it. A session
        started under A in between therefore loses its A-account pointer at the
        later genuine A -> B, instead of surviving as something the sweep decided
        was already on the new account.
        """

        smap = self._manager()
        busy_a = self._session(_FakeProvider(""), busy=True)
        smap._sessions["busy"] = busy_a
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-a")

        # A -> B, left incomplete by the busy A-session: fence armed for B.
        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False

        # Back to A. The consumer baseline never advanced, so this fires only
        # because an outstanding sweep is a trigger in its own right.
        await smap.retire_kiro_identity_sessions("fp-a")

        # A session started under A AFTER that return.
        later = self._session(_FakeProvider("", sid="sid-account-a"))
        smap._sessions["later"] = later
        smap._advance_session_generation("later")
        smap._session_map.set("later", "sid-account-a")

        # The genuine A -> B. `later` holds an A conversation and must lose it.
        retired, _ = await smap.retire_kiro_identity_sessions("fp-b")

        assert "later" in retired, "an A-account holder escaped the sweep"
        assert self._stored_sid(smap, "later") == ""
        assert smap._session_map.get_discarded_sid("later") == "sid-account-a"

    @pytest.mark.asyncio
    async def test_an_older_sweep_does_not_retire_a_newer_sweeps_fence(self) -> None:
        """A -> B whose shutdowns straddle a B -> C sweep.

        The shutdown loop runs outside ``identity_sweep_lock``, so a second
        account change can acquire that lock and recapture the fence while the
        first sweep is still awaiting a child's exit. The first sweep's own work
        succeeded while the pending change belongs to C: clearing the fence
        would drop the successor generations C recorded, and reporting complete
        would let the caller reconcile its baseline with C's busy holder still
        serving turns on B's credential -- and with no pending fingerprint left
        for the next turn to retry from.
        """

        released = asyncio.Event()
        entered = asyncio.Event()

        class _SlowProvider(_FakeProvider):
            async def shutdown(self) -> None:
                self.shutdown_calls += 1
                entered.set()
                await released.wait()

        smap = self._manager()
        smap._sessions["doomed"] = self._session(_SlowProvider(""))
        smap._advance_session_generation("doomed")
        smap._session_map.set("doomed", "sid-account-a")

        first = asyncio.create_task(smap.retire_kiro_identity_sessions("fp-b"))
        await await_until(entered)

        # A busy successor registers under B while that shutdown is pending, then
        # the account moves on to C.
        busy_b = self._session(_SlowProvider(""), busy=True)
        smap._sessions["busy"] = busy_b
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-b")

        _, second_complete = await smap.retire_kiro_identity_sessions("fp-c")
        assert second_complete is False
        assert smap.pending_identity_sweep_fingerprint == "fp-c"

        released.set()
        retired, first_complete = await await_until(first)

        assert retired == ["doomed"]
        assert first_complete is False, "an older sweep claimed a newer change was handled"
        assert smap.pending_identity_sweep_fingerprint == "fp-c"
        assert busy_b.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_an_ordinary_stale_eviction_keeps_the_conversation(self) -> None:
        """Only an identity change invalidates the sid.

        A dead or unresponsive provider is replaced under the SAME account, where
        resuming the native conversation is the behaviour that preserves it.
        """

        smap = self._manager()
        dead = _FakeProvider("")
        session = self._session(dead)
        smap._sessions["stale"] = session
        smap._session_map.set("stale", "sid-same-account")

        await smap._evict_stale_session("stale", session)

        assert self._stored_sid(smap, "stale") == "sid-same-account"
        assert smap._session_map.get_discarded_sid("stale") == ""

    @pytest.mark.asyncio
    async def test_a_marked_session_whose_process_died_first_still_loses_its_pointer(
        self,
    ) -> None:
        """A child that dies after the sweep cannot restore its old pointer."""

        smap = self._manager()
        dead = _DeadProvider("")
        session = self._session(dead, busy=True)
        smap._sessions["marked"] = session
        smap._session_map.set("marked", "sid-old-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False
        assert session.retire_on_identity_change is True
        assert self._stored_sid(smap, "marked") == ""

        # No factory: the cold start after the removal raises, which keeps this
        # test on the removal itself rather than on provider construction.
        smap._provider_factory = None
        with pytest.raises(RuntimeError, match="No provider factory"):
            await smap.get_or_create("marked")

        assert "marked" not in smap._sessions
        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"

    @pytest.mark.asyncio
    async def test_an_unmarked_dead_session_keeps_its_conversation(self) -> None:
        """A dead provider outside the changed identity store keeps its pointer."""

        smap = self._manager()
        dead = _DeadProvider("claude")
        smap._sessions["stale"] = self._session(dead)
        smap._session_map.set("stale", "sid-same-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is True
        assert self._stored_sid(smap, "stale") == "sid-same-account"

        smap._provider_factory = None
        with pytest.raises(RuntimeError, match="No provider factory"):
            await smap.get_or_create("stale")

        assert self._stored_sid(smap, "stale") == "sid-same-account"
        assert smap._session_map.get_discarded_sid("stale") == ""

    @pytest.mark.asyncio
    async def test_shutdown_skips_marked_sid_but_persists_unmarked_sid(self, monkeypatch) -> None:
        """Shutdown must not undo the sweep's old-account pointer clear."""

        smap = self._manager()
        marked = _FakeProvider("", sid="sid-old-account")
        marked_session = self._session(marked, busy=True)
        smap._sessions["marked"] = marked_session
        smap._session_map.set("marked", "sid-old-account")

        await smap.retire_kiro_identity_sessions()
        assert marked_session.retire_on_identity_change is True
        assert self._stored_sid(smap, "marked") == ""

        unmarked = _FakeProvider("", sid="sid-current-account")
        smap._sessions["unmarked"] = self._session(unmarked)
        monkeypatch.setattr("kiro_crew.session._load_acp_provider_type", lambda: _FakeProvider)

        await smap.close_all()

        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"
        assert self._stored_sid(smap, "unmarked") == "sid-current-account"

    def test_marked_replay_settlement_consumes_lease_without_republishing_sid(self) -> None:
        """A landed replay cannot restore the identity sweep's discarded sid."""

        from kiro_crew.providers.acp import AcpProvider

        smap = self._manager()
        provider = object.__new__(AcpProvider)
        provider._client = SimpleNamespace(
            _session_id="sid-old-account-replayed",
            _work_dir="/old-workspace",
            backend="kiro",
        )
        session = self._session(provider)
        session.provider_switch_replay = True
        session.retire_on_identity_change = True
        smap._sessions["marked"] = session
        smap._session_map.set("marked", "sid-old-account")
        smap._session_map.clear_sid("marked")

        assert smap.commit_provider_switch_replay_sid("marked") is True
        assert session.provider_switch_replay is False
        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"

    def test_every_sid_writer_is_registration_or_identity_fenced(self) -> None:
        """A future non-registration writer must respect the identity marker."""

        import ast

        source_root = src_root()
        registration_paths = {
            ("session_allocation.py", "seed_conversation"),
            ("session_allocation_parts/get_or_create.py", "_get_or_create_impl"),
        }
        writers: list[str] = []
        offenders: list[str] = []

        def is_session_map_set(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_session_map"
            )

        def has_prior_identity_guard(
            call: ast.Call,
            parents: dict[ast.AST, ast.AST],
        ) -> bool:
            child: ast.AST = call
            while child in parents:
                parent = parents[child]
                for _, value in ast.iter_fields(parent):
                    if not isinstance(value, list) or child not in value:
                        continue
                    for sibling in value[: value.index(child)]:
                        if not isinstance(sibling, ast.If):
                            continue
                        tests_identity_marker = (
                            isinstance(sibling.test, ast.Attribute)
                            and sibling.test.attr == "retire_on_identity_change"
                        )
                        exits_writer_path = bool(sibling.body) and isinstance(
                            sibling.body[-1], (ast.Continue, ast.Return)
                        )
                        if tests_identity_marker and exits_writer_path:
                            return True
                child = parent
            return False

        # A writer spells ``_session_map`` as an attribute, so only files whose text
        # holds it are parsed.
        for path, _text, tree in parsed_candidates(
            require_any=("_session_map",), skip_syntax_errors=False
        ):
            parents = {
                child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
            }
            for call in (node for node in ast.walk(tree) if is_session_map_set(node)):
                assert isinstance(call, ast.Call)
                function: ast.AST = call
                while function in parents and not isinstance(
                    function, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    function = parents[function]
                function_name = getattr(function, "name", "<module>")
                relative_path = path.relative_to(source_root).as_posix()
                location = f"{relative_path}:{call.lineno}:{function_name}"
                writers.append(location)
                if (relative_path, function_name) in registration_paths:
                    continue
                if not has_prior_identity_guard(call, parents):
                    offenders.append(location)

        assert len(writers) >= 7, f"session-map writer scan found only {writers}"
        assert (
            not offenders
        ), "session-map sid writer lacks a retire_on_identity_change fence: " + ", ".join(offenders)


class TestWarmPoolIdentityFence:
    """A pooled process that authenticated as the previous account is unusable.

    Pool claims take no cold-start permit, so the sweep's permit barrier cannot
    hold one back, and the pool teardown cannot run inside that barrier without
    deadlocking on the fill lock. The claim-time age check is the fence.
    """

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        return SessionManager(KiroCrewConfig())

    @staticmethod
    def _record_discards(smap) -> list:
        discarded: list = []

        async def _discard(provider, context: str) -> None:
            discarded.append((provider, context))

        smap._discard_pool_provider = _discard  # type: ignore[method-assign]
        smap._schedule_replenish = lambda: None  # type: ignore[method-assign]
        return discarded

    @pytest.mark.asyncio
    async def test_a_provider_queued_before_the_switch_is_discarded_not_claimed(self) -> None:
        smap = self._manager()
        discarded = self._record_discards(smap)
        stale = _FakeProvider("")
        smap._warm_pool.put_nowait((stale, time.monotonic()))

        # The sweep stamps the pool before any key becomes claimable.
        smap._mark_identity_epoch()

        assert await smap._drain_and_claim(None) is None
        assert [p for p, _ in discarded] == [stale]

    @pytest.mark.asyncio
    async def test_a_pool_start_spanning_the_switch_is_discarded(self) -> None:
        """The enqueue timestamp records when startup began, not when it ended."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        started = asyncio.Event()
        finish_start = asyncio.Event()

        class _StraddlingProvider(_FakeProvider):
            async def start(self) -> None:
                started.set()
                await finish_start.wait()

        provider = _StraddlingProvider("")
        smap._pool._pool_size = 1
        smap._provider_factory = lambda *args, **kwargs: provider

        fill = asyncio.create_task(smap._fill_warm_pool())
        await started.wait()
        smap._mark_identity_epoch()
        finish_start.set()
        await fill

        assert await smap._drain_and_claim(None) is None
        assert [item for item, _ in discarded] == [provider]

    @pytest.mark.asyncio
    async def test_a_provider_spawned_after_the_switch_is_still_claimable(self) -> None:
        """The fence is an age check, not a pool-wide off switch.

        The replenish that follows a sweep spawns under the NEW account, and
        refusing those would leave the pool permanently useless.
        """

        smap = self._manager()
        discarded = self._record_discards(smap)
        smap._mark_identity_epoch()
        fresh = _FakeProvider("")
        # Strictly after the epoch: the stamp is taken before this spawn.
        smap._warm_pool.put_nowait((fresh, smap._pool._pool_identity_epoch + 0.001))

        assert await smap._drain_and_claim(None) is fresh
        assert discarded == []

    @pytest.mark.asyncio
    async def test_a_provider_on_another_identity_store_is_not_fenced(self) -> None:
        """Only providers that read the Kiro identity store are affected."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        other = _FakeProvider("claude")
        assert other.uses_kiro_identity_store is False
        smap._warm_pool.put_nowait((other, time.monotonic()))

        smap._mark_identity_epoch()

        assert await smap._drain_and_claim(None) is other
        assert discarded == []

    @pytest.mark.asyncio
    async def test_an_untouched_pool_claims_normally(self) -> None:
        """With no sweep having run there is no epoch, so nothing is fenced."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        provider = _FakeProvider("")
        smap._warm_pool.put_nowait((provider, time.monotonic()))

        assert smap._pool._pool_identity_epoch == 0.0
        assert await smap._drain_and_claim(None) is provider
        assert discarded == []

    @pytest.mark.asyncio
    async def test_the_sweep_stamps_the_pool_even_when_it_retires_nothing(self) -> None:
        """The stamp is the sweep's first act, not a consequence of a retirement.

        A sweep that finds no session to pop still has a pool full of previous
        account processes, and the claim path is what must learn about it.
        """

        smap = self._manager()
        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is True
        assert smap._pool._pool_identity_epoch > 0.0
