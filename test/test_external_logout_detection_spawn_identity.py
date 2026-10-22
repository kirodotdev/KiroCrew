"""External logout detection tests: spawn-time identity.

Per-child spawn stamps, stamp-mismatch flagging, parking and the drain reap.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from external_logout_detection_helpers import _private_sel_root_per_test  # noqa: F401
from external_logout_detection_helpers import _expire_identity_cache, _FakeProvider, _write_store
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import kiro_prerequisite as kp


class TestSpawnIdentityStamp:
    """Each kiro-backed child records the account it spawned under.

    The gateway-wide baseline and the interim latch share one blind spot: an
    A->B->A round trip that completes with NO fresh read in between leaves
    both comparing A to A while a child that spawned during the interim still
    holds B's credential. The stamp closes it per child: the spawn records the
    account the store held at that moment, and the turn gate's
    ``flag_identity_stamp_mismatches`` retires any session whose record
    PROVABLY differs from the live read.

    "Provably" is the loop guard, mirrored from the latch: a component
    participates only when nonempty on BOTH sides, because reads lose
    components under failure and never gain or alter them -- so a transient
    read failure (at spawn or at the gate) can never flag a healthy child,
    and an unstamped child keeps exactly the pre-stamping protections.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }

    def test_mismatch_component_rules(self) -> None:
        """The pure comparator: only a both-sides-nonempty difference flags."""

        mismatch = kp.identity_stamp_mismatch
        # Unknown on either side is never a mismatch.
        assert mismatch("", "") is False
        assert mismatch("", "cli-a") is False
        assert mismatch("cli-b", "") is False
        # Equality is never a mismatch.
        assert mismatch("cli-a", "cli-a") is False
        assert mismatch("cli-a+crew:v1", "cli-a+crew:v1") is False
        # A CLI component that differs on both sides flags.
        assert mismatch("cli-b", "cli-a") is True
        assert mismatch("cli-b+crew:v1", "cli-a+crew:v1") is True
        # A vault component that differs on both sides flags.
        assert mismatch("cli-a+crew:v2", "cli-a+crew:v1") is True
        assert mismatch("+crew:v2", "+crew:v1") is True
        # Component LOSS is indistinguishable from a read blip: never flags.
        assert mismatch("cli-a+crew:v1", "cli-a") is False
        assert mismatch("cli-a", "cli-a+crew:v1") is False
        assert mismatch("+crew:v1", "cli-a") is False

    @pytest.mark.asyncio
    async def test_a_child_from_an_unobserved_round_trip_is_flagged(self, tmp_path: Path) -> None:
        """The end-to-end scenario the stamp exists for.

        Seed A -> a child spawns while the store holds B (its spawn records
        B) -> the store returns to A before ANY other read. The baseline
        comparison reports unchanged -- and the child is still flagged for
        recycle, from its own record.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        # The interim window: the store switches to B and a child spawns.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        provider = _FakeProvider("")
        pre_spawn = await kp.pre_spawn_identity(service.read_spawn_identity)
        await kp.stamp_spawn_identity(service.read_spawn_identity, provider, pre_spawn=pre_spawn)
        interim_stamp = getattr(provider, "spawn_identity", "")
        assert interim_stamp != "", "the spawn must have recorded the interim account"

        smap = SessionManager(KiroCrewConfig())
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["victim"] = sess

        # The round trip completes; the live read now matches the baseline.
        _write_store(db)
        _expire_identity_cache(service)
        live = await service.current_identity_fingerprint(allow_cached=False)
        assert live != interim_stamp

        flagged = await smap.flag_identity_stamp_mismatches(live)
        assert flagged == ["victim"]
        assert sess.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_an_unstamped_child_is_never_flagged(self, tmp_path: Path) -> None:
        """No stamp means no verdict: the pre-stamping protections apply."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        sess = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        smap._sessions["plain"] = sess

        assert await smap.flag_identity_stamp_mismatches("live-fp") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_matching_stamp_never_flags_and_an_empty_live_never_flags(
        self, tmp_path: Path
    ) -> None:
        """The healthy-host invariant and the gate-side blip guard.

        A stamped child whose account still matches must never be recycled --
        flagging here every turn would be the respawn churn the seeded
        baseline removed -- and an unreadable live read proves nothing.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("")
        provider.spawn_identity = "same-fp"  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["healthy"] = sess

        for _ in range(3):
            assert await smap.flag_identity_stamp_mismatches("same-fp") == []
        assert await smap.flag_identity_stamp_mismatches("") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_non_kiro_child_is_never_flagged(self, tmp_path: Path) -> None:
        """The store predicate still gates: a claude child never compares."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("claude")
        provider.spawn_identity = "cli-b"  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["claude"] = sess

        assert await smap.flag_identity_stamp_mismatches("cli-a") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_shared_runtime_stamp_is_read_through_the_fallback(
        self, tmp_path: Path
    ) -> None:
        """A demuxed session's provider inherits its runtime's spawn record."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("")
        provider._runtime = SimpleNamespace(spawn_identity="cli-b")  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["demuxed"] = sess

        assert await smap.flag_identity_stamp_mismatches("cli-a") == ["demuxed"]
        assert sess.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_the_first_stamp_wins(self) -> None:
        """A warm-pool provider keeps its fill-time record across a claim.

        Re-stamping at claim time would relabel the child with whatever the
        store holds THEN -- exactly the wrong account for a provider that
        spawned during an interim window.
        """

        provider = _FakeProvider("")

        async def _fill_read() -> str:
            return "fill-time-fp"

        async def _claim_read() -> str:
            return "claim-time-fp"

        await kp.stamp_spawn_identity(_fill_read, provider, pre_spawn="fill-time-fp")
        await kp.stamp_spawn_identity(_claim_read, provider, pre_spawn="claim-time-fp")
        assert provider.spawn_identity == "fill-time-fp"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stamping_refuses_failures_and_empty_reads(self) -> None:
        """A failed or empty spawn read leaves the child unstamped, never raises.

        Absent and unknown must stay the same observable: recording "" would
        be a claim, and the flag machinery skips unstamped children entirely.
        """

        provider = _FakeProvider("")

        await kp.stamp_spawn_identity(None, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

        async def _empty() -> str:
            return ""

        await kp.stamp_spawn_identity(_empty, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

        async def _boom() -> str:
            raise RuntimeError("store exploded")

        await kp.stamp_spawn_identity(_boom, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_a_switch_across_the_spawn_window_refuses_the_stamp(self) -> None:
        """Disagreeing bracket reads mean the child's account is unprovable.

        The child reads its credential between the pre-spawn and post-spawn
        reads. When they disagree, the store switched inside the window --
        stamping the post value would label a possibly-B child as A, which is
        exactly the mislabelling the reviewer's F2 names. Refusal keeps the
        child on the pre-stamping protections, and the differing read has
        already fed the interim latch through the ordinary read path.
        """

        provider = _FakeProvider("")

        async def _post_read() -> str:
            return "cli-a"

        await kp.stamp_spawn_identity(_post_read, provider, pre_spawn="cli-b")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_a_missing_pre_read_refuses_the_stamp(self) -> None:
        """No pre-spawn read, no agreement, no stamp.

        A post-only read is the single-sample race F2 names; a site that
        skips the pre-read must degrade to the fail-safe unstamped state,
        never to a guess.
        """

        provider = _FakeProvider("")

        async def _post_read() -> str:
            return "cli-a"

        await kp.stamp_spawn_identity(_post_read, provider, pre_spawn="")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_agreeing_bracket_reads_stamp_the_child(self) -> None:
        """The healthy path: both reads name the same account and it sticks."""

        provider = _FakeProvider("")

        async def _read() -> str:
            return "cli-a"

        pre = await kp.pre_spawn_identity(_read)
        await kp.stamp_spawn_identity(_read, provider, pre_spawn=pre)
        assert provider.spawn_identity == "cli-a"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_failed_pre_read_returns_empty_and_never_raises(self) -> None:
        """``pre_spawn_identity`` is best-effort: failures become the absent value."""

        async def _boom() -> str:
            raise RuntimeError("store exploded")

        assert await kp.pre_spawn_identity(None) == ""
        assert await kp.pre_spawn_identity(_boom) == ""

    @pytest.mark.asyncio
    async def test_a_spawn_read_also_feeds_the_interim_latch(self, tmp_path: Path) -> None:
        """Defense in depth: the spawn's own read is an interim observer.

        Stamping flows through the ordinary read path, so a spawn that lands
        during the interim window arms the latch exactly as a status poll
        would -- the round trip is then caught globally as well as per child.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await kp.stamp_spawn_identity(service.read_spawn_identity, _FakeProvider(""))

        _write_store(db)  # the round trip completes
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "the spawn-time observation must force a sweep"

    def test_both_startup_sites_wire_the_spawn_reader(self) -> None:
        """Every seeded startup site must also wire the stamp reader.

        A site that seeds but does not wire silently reverts that entrypoint
        to baseline-only protection. Counted like the seed-call scan: the
        wiring is spelled identically at both sites.
        """

        dashboard = Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard"
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in (
                dashboard / "server.py",
                dashboard / "server_runtime" / "api_entrypoint.py",
            )
        )
        seeds = source.count("seed_sessions_baseline()")
        wirings = source.count("spawn_identity_reader = ")
        assert seeds >= 2, "expected both startup sites to seed"
        assert wirings >= seeds, (
            f"{seeds} seed site(s) but only {wirings} spawn_identity_reader "
            "wiring(s): an unwired site leaves every child unstamped there"
        )

    def test_every_spawn_site_stamps(self) -> None:
        """Each provider start path must record the spawn account.

        Counted per file: a start site that skips stamping reverts its spawns
        to baseline-only protection without any test failing at runtime,
        because unstamped children are (by design) silently skipped.
        """

        src = Path(__file__).parents[1] / "src" / "kiro_crew"
        expected = {
            # shared run runtime + direct companion runtime
            "session_allocation_parts/companions.py": 2,
            # cold start
            "session_allocation_parts/get_or_create.py": 1,
            # bg create + recycle replacement + bg replacement runtime
            "session_background.py": 3,
            "session_pool.py": 1,  # warm pool fill
        }
        pre_reads = {
            "session_allocation_parts/companions.py": 2,
            "session_allocation_parts/get_or_create.py": 1,
            # the bg replacement runtime re-brackets its fallback spawn, so it
            # carries one more pre-read than stamp calls
            "session_background.py": 4,
            "session_pool.py": 1,
        }
        for name, count in expected.items():
            source = (src / name).read_text(encoding="utf-8")
            found = source.count("await stamp_spawn_identity(")
            assert found >= count, (
                f"{name}: expected at least {count} stamp_spawn_identity "
                f"call(s) after provider starts, found {found}"
            )
            brackets = source.count("await pre_spawn_identity(")
            assert brackets >= pre_reads[name], (
                f"{name}: expected at least {pre_reads[name]} pre_spawn_identity bracket "
                f"read(s) before provider starts, found {brackets} -- a site "
                "without one leaves its children unstamped (fail-safe, but "
                "silently reduced coverage)"
            )

    @staticmethod
    def _fake_runtime(stamp: str, *, busy: bool = False, kiro: bool = True) -> "SimpleNamespace":
        """A registry double declaring the store capability like AcpRuntime."""

        killed: list[str] = []

        async def _kill(*, expected: bool = False, reason: str = "") -> None:
            killed.append(reason)

        ns = SimpleNamespace(
            uses_kiro_identity_store=kiro,
            spawn_identity=stamp,
            has_active_or_initializing_sessions=lambda: busy,
            kill=_kill,
            killed=killed,
            pid=_UNALLOCATABLE_PID,
        )
        ns.is_alive = lambda: not killed
        return ns

    @pytest.mark.asyncio
    async def test_a_mismatched_bg_runtime_is_parked_at_the_gate(self) -> None:
        """F1/F3: the background runtime's stamp is compared, and the slot is
        freed WITHOUT a same-pass kill.

        It spawned while the store held B; the store now names A. It never
        appears in ``_sessions``, so only the registry pass can reach it --
        and even when its busy probe answers idle, the kill is deferred to
        the drain reap: the probe races a ``get_bg_session`` claim that was
        handed the runtime but has not yet opened its init scope, so killing
        on the pass that proved the mismatch would abort that claim.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        runtime = self._fake_runtime("cli-b")
        smap._bg_runtime = runtime  # type: ignore[assignment]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert flagged == ["background-runtime"]
        assert runtime.killed == [], "the mismatch pass must never kill same-pass"
        assert smap._bg_runtime is None
        assert runtime in smap._draining_bg_runtimes

    @pytest.mark.asyncio
    async def test_a_mismatched_subagent_runtime_is_parked_at_the_gate(self) -> None:
        """F1/F3: a wrong-account companion runtime is displaced, never
        killed on the pass that proved the mismatch.

        Every session demuxed onto it would inherit the stale credential, so
        the registry itself must be swept -- but an idle-looking runtime may
        be one a concurrent claim was just handed (``create_session`` opens
        its init scope only at entry), so the kill belongs to a later drain
        reap, after the park grace.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        runtime = self._fake_runtime("cli-b")
        smap._subagent_runtimes["parent"] = runtime  # type: ignore[index]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert flagged == ["subagent-runtime:parent"]
        assert runtime.killed == [], "the mismatch pass must never kill same-pass"
        assert "parent" not in smap._subagent_runtimes
        assert runtime in smap._draining_subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_busy_mismatched_runtime_is_displaced_never_killed(self) -> None:
        """A busy wrong-account runtime leaves the claimable slots but keeps running.

        Killing live work is the defect this whole change removes -- but
        leaving the runtime registered kept it CLAIMABLE: the registry and the
        ``_bg`` slot are exactly what new acquisitions are handed, so a busy
        B-stamped runtime would serve fresh sessions B's credentials for its
        whole drain. Displacement takes both properties at once: the runtime
        is popped from the claimable slot (a replacement spawns under the
        live account) and parked, unkilled, until its work drains.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        bg = self._fake_runtime("cli-b", busy=True)
        sub = self._fake_runtime("cli-b", busy=True)
        healthy = self._fake_runtime("cli-a", busy=True)
        smap._bg_runtime = bg  # type: ignore[assignment]
        smap._subagent_runtimes["parent"] = sub  # type: ignore[index]
        smap._subagent_runtimes["healthy"] = healthy  # type: ignore[index]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert sorted(flagged) == ["background-runtime", "subagent-runtime:parent"]
        # Never killed mid-turn: the work is preserved.
        assert bg.killed == [] and sub.killed == []
        # Unclaimable: both slots are free for a live-account respawn.
        assert smap._bg_runtime is None
        assert "parent" not in smap._subagent_runtimes
        # Parked to drain instead.
        assert sub in smap._draining_subagent_runtimes
        assert bg in smap._draining_bg_runtimes
        # A busy runtime with a MATCHING stamp is untouched.
        assert smap._subagent_runtimes["healthy"] is healthy
        assert healthy.killed == []

    @pytest.mark.asyncio
    async def test_a_parked_displaced_runtime_is_reaped_once_it_drains(self) -> None:
        """The park is a drain, not a leak: idle parked runtimes are killed.

        While busy the parked runtime survives every pass (probes fail toward
        busy). Once idle it must ALSO outlive the park grace before the reap
        may end it -- the busy probe can read a just-claimed runtime as idle
        for the sub-second stretch before the claim opens its init scope --
        and only a pass that finds it idle past the grace kills it and drops
        it from the drain list.
        """

        import time as _time

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.kiro_prerequisite import IDENTITY_PARK_GRACE_SECS
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        bg = self._fake_runtime("cli-b", busy=True)
        sub = self._fake_runtime("cli-b", busy=True)
        smap._bg_runtime = bg  # type: ignore[assignment]
        smap._subagent_runtimes["parent"] = sub  # type: ignore[index]

        await smap.flag_identity_stamp_mismatches("cli-a")
        # Still busy: parked runtimes survive the next pass unkilled.
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert bg.killed == [] and sub.killed == []

        sub.has_active_or_initializing_sessions = lambda: False
        bg.has_active_or_initializing_sessions = lambda: False
        # Idle but freshly parked: the grace refuses the kill.
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert bg.killed == [] and sub.killed == []
        assert sub in smap._draining_subagent_runtimes
        assert bg in smap._draining_bg_runtimes

        # Age the park marks past the grace: the next pass reaps both.
        aged = _time.monotonic() - IDENTITY_PARK_GRACE_SECS - 1.0
        sub._identity_parked_at = aged
        bg._identity_parked_at = aged
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert sub.killed == ["drained identity displacement teardown"]
        assert bg.killed == ["drained displacement teardown"]
        assert smap._draining_subagent_runtimes == []
        assert smap._draining_bg_runtimes == []

    @pytest.mark.asyncio
    async def test_matching_unstamped_or_foreign_runtimes_are_never_retired(self) -> None:
        """The healthy-host invariant extends to the registries.

        A matching stamp, no stamp at all (fail-safe skip), and a non-kiro
        runtime must all survive every pass -- retiring any of them per turn
        would be the respawn churn this PR exists to remove.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        healthy = self._fake_runtime("cli-a")
        unstamped = self._fake_runtime("")
        foreign = self._fake_runtime("cli-b", kiro=False)
        smap._bg_runtime = healthy  # type: ignore[assignment]
        smap._subagent_runtimes["plain"] = unstamped  # type: ignore[index]
        smap._subagent_runtimes["claude"] = foreign  # type: ignore[index]

        for _ in range(3):
            assert await smap.flag_identity_stamp_mismatches("cli-a") == []
        assert healthy.killed == [] and unstamped.killed == [] and foreign.killed == []
        assert smap._bg_runtime is healthy

    @pytest.mark.asyncio
    async def test_a_direct_companion_runtime_is_stamped_at_spawn(self) -> None:
        """F1: ``get_subagent_runtime``'s raw runtime spawn records the account.

        Without the stamp, the registry pass compares an empty string and
        silently skips this runtime for its whole lifetime -- every subagent
        session demuxed onto it would inherit a credential nothing can audit.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())

        async def reader() -> str:
            return "cli-a"

        smap.spawn_identity_reader = reader  # type: ignore[attr-defined]

        class _FakeRuntime:
            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            runtime = await smap.get_subagent_runtime("parent")

        assert getattr(runtime, "spawn_identity", "") == "cli-a"
        assert smap._subagent_runtimes["parent"] is runtime

    @pytest.mark.asyncio
    async def test_ensure_background_holds_the_permit_through_registration(self) -> None:
        """F2: the sweep's permit barrier covers registration, not just start.

        The identity sweep drains every cold-start permit as its quiescence
        barrier. If ``_ensure_background`` released its permit before the
        provider was registered, a concurrent sweep could reconcile a store
        change while the (possibly wrong-account) provider was invisible to
        it, and the provider would register afterward -- past the barrier.
        Registration must therefore happen while the permit is still held.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import BACKGROUND_KEY, SessionManager

        class _Prov:
            cwd = "."

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

        smap = SessionManager(
            KiroCrewConfig(), provider_factory=lambda key, agent=None, cwd=None: _Prov()
        )
        # One permit total, so ``locked()`` is True exactly while it is held.
        from kiro_crew.start_priority import PrioritySemaphore

        smap._start_sem = PrioritySemaphore(1)

        held_at_registration: list[bool] = []
        original = smap._advance_session_generation

        def probe(key: str) -> None:
            held_at_registration.append(smap._start_sem.locked())
            original(key)

        smap._advance_session_generation = probe  # type: ignore[method-assign]

        await smap._ensure_background()

        assert BACKGROUND_KEY in smap._sessions
        assert held_at_registration, "registration never happened"
        assert held_at_registration[0] is True, (
            "the cold-start permit was released before the background provider "
            "was registered -- the identity sweep's barrier cannot cover the "
            "started-but-unregistered window"
        )
        await smap.close_all()

    @pytest.mark.asyncio
    async def test_a_cancelled_stamp_read_tears_down_the_companion_runtime(self) -> None:
        """A cancellation during the stamp read must not leak the spawned runtime.

        The stamp read is a real suspension point between a successful spawn
        and registration. Pre-guard, a cancellation landing there escaped with
        the runtime alive but absent from ``_subagent_runtimes`` -- unmanaged
        until the orphan sweep's grace window expired. The guard kills it on
        the way out and re-raises.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())

        calls: list[int] = []

        async def cancelling_reader() -> str:
            # The first read is the pre-spawn bracket (before spawn -- a
            # cancellation there never leaks anything); the second is the
            # post-start stamp read, the window under test.
            calls.append(1)
            if len(calls) >= 2:
                raise asyncio.CancelledError()
            return "cli-a"

        smap.spawn_identity_reader = cancelling_reader  # type: ignore[attr-defined]

        killed: list[str] = []

        class _FakeRuntime:
            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

            async def kill(self, expected: bool = False, reason: str = "") -> None:
                killed.append(reason)

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            with pytest.raises(asyncio.CancelledError):
                await smap.get_subagent_runtime("parent")

        assert killed, (
            "the spawned runtime was not torn down when the stamp read was "
            "cancelled -- it leaks unmanaged until the orphan sweep's grace "
            "window expires"
        )
        assert "parent" not in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_cancelled_stamp_read_kills_the_background_provider(self) -> None:
        """The background spawn's stamp window is covered by a kill guard too."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import BACKGROUND_KEY, SessionManager

        class _Prov:
            cwd = "."

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

        smap = SessionManager(
            KiroCrewConfig(), provider_factory=lambda key, agent=None, cwd=None: _Prov()
        )

        calls: list[int] = []

        async def cancelling_reader() -> str:
            # First read = pre-spawn bracket (a cancel there escapes before
            # start and leaks nothing); second = the post-start stamp read.
            calls.append(1)
            if len(calls) >= 2:
                raise asyncio.CancelledError()
            return "cli-a"

        smap.spawn_identity_reader = cancelling_reader  # type: ignore[attr-defined]

        hard_killed: list[object] = []
        smap._dispatch_hard_kill = hard_killed.append  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await smap._ensure_background()

        assert hard_killed, (
            "the started background provider was not killed when the stamp "
            "read was cancelled before registration"
        )
        assert BACKGROUND_KEY not in smap._sessions
        await smap.close_all()

    def test_every_stamp_await_is_guarded_against_cancellation(self) -> None:
        """Source scan: no stamp call may sit unguarded between start and registry.

        ``stamp_spawn_identity`` awaits an uncached store read (a multi-second
        suspension point) after the child's process already started but before
        it is registered anywhere a sweep can see. Every call site must
        therefore either sit inside a ``try`` whose handler tears the child
        down (kill/discard + raise) or be covered by an enclosing ``finally``
        that discards the child (the warm-pool fill pattern). This scan pins
        the guard textually: each ``await stamp_spawn_identity(`` must be
        preceded within a few lines by a ``try:`` opener that pairs with a
        teardown handler, so a refactor that hoists a stamp back out of its
        guard fails here loudly.
        """

        import re
        from pathlib import Path

        import kiro_crew

        src_root = Path(kiro_crew.__file__).parent
        guarded = 0
        for rel in (
            "session_allocation.py",
            "session_allocation_parts/companions.py",
            "session_allocation_parts/get_or_create.py",
            "session_background.py",
            "session_pool.py",
        ):
            text = (src_root / rel).read_text(encoding="utf-8")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "await stamp_spawn_identity(" not in line:
                    continue
                guarded += 1
                window_before = "\n".join(lines[max(0, i - 6) : i + 1])
                window_after = "\n".join(lines[i : i + 40])
                has_try_guard = re.search(r"^\s*try:\s*$", window_before, re.M) and re.search(
                    r"except BaseException:", window_after
                )
                has_finally_discard = re.search(
                    r"finally:\s*\n\s*if provider is not None:", window_after
                )
                assert has_try_guard or has_finally_discard, (
                    f"{rel}:{i + 1}: `await stamp_spawn_identity(` is not covered by a "
                    "cancellation teardown guard (try/except BaseException that kills the "
                    "child, or an enclosing finally that discards it)"
                )
        assert guarded >= 6, f"expected at least 6 stamp sites, scanned {guarded}"

    @pytest.mark.asyncio
    async def test_the_stamp_read_runs_under_the_pid_shield(self) -> None:
        """F2: the child's PID is shielded from the orphan sweep during the stamp.

        The stamp read is a bounded suspension point between the child's
        process start and its registration anywhere the sweep's active-PID
        union can see (``_starting_pids``, the session map, a runtime
        registry, the warm pool). On hosts whose PID probe has no age grace a
        sweep tick landing in that await would kill the healthy child -- so
        the shield must be up BEFORE the read suspends, and released once
        registration is visible.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        observed: list[set[int]] = []

        async def reader() -> str:
            observed.append(set(smap._starting_pids))
            return "cli-a"

        smap.spawn_identity_reader = reader  # type: ignore[attr-defined]

        class _FakeRuntime:
            pid = _UNALLOCATABLE_PID

            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            runtime = await smap.get_subagent_runtime("parent")

        assert len(observed) == 2, "expected a pre-spawn and a stamp read"
        assert _UNALLOCATABLE_PID not in observed[0], "no shield before the process exists"
        assert _UNALLOCATABLE_PID in observed[1], (
            "the stamp read must run under the start-to-registration PID "
            "shield, or an orphan-sweep tick landing in the await kills the "
            "healthy child"
        )
        assert (
            _UNALLOCATABLE_PID not in smap._starting_pids
        ), "the shield must be released once the registry owns the runtime"
        assert smap._subagent_runtimes["parent"] is runtime

    def test_every_stamp_await_is_shielded_from_the_orphan_sweep(self) -> None:
        """Source scan: each stamp await sits under the PID shield.

        Every ``await stamp_spawn_identity(`` must be preceded, within its
        site's shield block, by a ``_starting_pids.add(`` -- the facade's
        start-to-registration guard the orphan sweep unions into its active
        set. A site that stamps unshielded re-opens the Windows kill window
        the shield exists for, so a refactor that drops one fails here
        loudly.
        """

        from pathlib import Path

        import kiro_crew

        src_root = Path(kiro_crew.__file__).parent
        scanned = 0
        for rel in (
            "session_allocation.py",
            "session_allocation_parts/companions.py",
            "session_allocation_parts/get_or_create.py",
            "session_background.py",
            "session_pool.py",
        ):
            lines = (src_root / rel).read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if "await stamp_spawn_identity(" not in line:
                    continue
                scanned += 1
                window_before = "\n".join(lines[max(0, i - 25) : i + 1])
                assert "_starting_pids.add(" in window_before, (
                    f"{rel}:{i + 1}: `await stamp_spawn_identity(` is not preceded by a "
                    "`_starting_pids.add(` shield -- the read suspends after start() "
                    "published the PID but before registration is visible to the "
                    "orphan sweep"
                )
        assert scanned >= 6, f"expected at least 6 stamp sites, scanned {scanned}"


class TestFlaggedSessionResumePointer:
    """A stamp-flagged session must lose its durable resume pointer.

    The eviction that recycles a flagged session replaces the process, but
    ``get_or_create``'s re-entry reads ``resume_sid`` from the session map --
    and ``close_all`` skips retire-flagged sessions by design, so a stale
    pointer would survive a gateway restart too. Either way the replacement
    child, authenticated under the CURRENT account, would ``session/load``
    the flagged account's conversation, whose signed thinking blocks its
    provider rejects wholesale.
    """

    @pytest.mark.asyncio
    async def test_a_flagged_session_loses_its_resume_pointer(self, tmp_path: Path) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        _write_store(
            db,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        _expire_identity_cache(service)
        victim_provider = _FakeProvider("")
        pre_spawn = await kp.pre_spawn_identity(service.read_spawn_identity)
        await kp.stamp_spawn_identity(
            service.read_spawn_identity, victim_provider, pre_spawn=pre_spawn
        )
        assert getattr(victim_provider, "spawn_identity", "") != ""

        _write_store(db)
        _expire_identity_cache(service)
        live = await service.current_identity_fingerprint(allow_cached=False)

        smap = SessionManager(KiroCrewConfig())
        smap._sessions["victim"] = _Session(provider=victim_provider)  # type: ignore[arg-type]
        keeper_provider = _FakeProvider("")
        keeper_provider.spawn_identity = live  # matches the live account
        smap._sessions["keeper"] = _Session(provider=keeper_provider)  # type: ignore[arg-type]

        cleared: list[str] = []
        smap._session_map.clear_sid = cleared.append  # type: ignore[method-assign]

        flagged = await smap.flag_identity_stamp_mismatches(live)
        assert flagged == ["victim"]
        assert cleared == ["victim"], (
            "the flagged session's resume pointer must be dropped at flag "
            "time, mirroring the sweep's clear_sid over invalidated keys"
        )


class TestDrainReapConcurrentPark:
    """A runtime parked WHILE the drain reap kills another must survive.

    The reap awaits each kill, and a concurrent turn gate can displace a new
    runtime onto the drain list during that suspension. The list is the only
    reference the parked runtime has left -- it is already popped from
    ``_subagent_runtimes`` -- so dropping it orphans a live child: invisible
    to ``close_all`` and to the PID shield alike.
    """

    @staticmethod
    def _runtime(*, busy: bool, parked_at: float | None = None) -> "SimpleNamespace":
        killed: list[str] = []

        async def _kill(*, expected: bool = False, reason: str = "") -> None:
            killed.append(reason)

        ns = SimpleNamespace(
            uses_kiro_identity_store=True,
            spawn_identity="cli-b",
            has_active_or_initializing_sessions=lambda: busy,
            kill=_kill,
            killed=killed,
            pid=_UNALLOCATABLE_PID,
        )
        ns.is_alive = lambda: not killed
        if parked_at is not None:
            ns._identity_parked_at = parked_at
        return ns

    @pytest.mark.asyncio
    async def test_a_runtime_parked_during_the_reap_kill_survives(self) -> None:
        import time as _time

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.kiro_prerequisite import IDENTITY_PARK_GRACE_SECS
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        aged = _time.monotonic() - IDENTITY_PARK_GRACE_SECS - 1.0
        drained = self._runtime(busy=False, parked_at=aged)
        late_park = self._runtime(busy=True, parked_at=_time.monotonic())

        original_kill = drained.kill

        async def _kill_and_park(*, expected: bool = False, reason: str = "") -> None:
            # While this kill's await is suspended, a CONCURRENT reap pass
            # finishes: its own rebuild drops the entry this pass is killing
            # and carries the runtime a turn gate just displaced. Any rebuild
            # this pass then writes from its pre-await traversal erases that
            # park; targeted removal of what this pass killed cannot.
            smap._draining_subagent_runtimes[:] = [late_park]
            await original_kill(expected=expected, reason=reason)

        drained.kill = _kill_and_park
        smap._draining_subagent_runtimes.append(drained)

        await smap.flag_identity_stamp_mismatches("cli-a")

        assert drained.killed, "the drained runtime past its grace is reaped"
        assert late_park in smap._draining_subagent_runtimes, (
            "a runtime parked during the reap's kill await must stay on the "
            "drain list -- it is that runtime's only remaining reference"
        )
