"""External logout detection tests: the session baseline, the interim latch and the turn
gate that decide when a turn sweeps.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
from external_logout_detection_helpers import _private_sel_root_per_test  # noqa: F401
from external_logout_detection_helpers import _expire_identity_cache, _FakeProvider, _write_store

from kiro_crew import kiro_prerequisite as kp
from kiro_crew.testing.wait import finished


class TestIdentityChangePredicate:
    @pytest.mark.asyncio
    async def test_no_change_before_the_first_probe(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.identity_changed_since_probe() is False

    @pytest.mark.asyncio
    async def test_change_detected_against_the_recorded_identity(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        # Stand in for a completed probe: the latch was written while the store
        # named this account.
        service._stamp_probe(await service.current_identity_fingerprint())
        assert await service.identity_changed_since_probe() is False
        _write_store(db, start_url="https://personal.awsapps.com/start")
        _expire_identity_cache(service)
        assert await service.identity_changed_since_probe() is True

    @pytest.mark.asyncio
    async def test_a_logout_before_the_first_turn_is_still_detected(self, tmp_path: Path) -> None:
        """A child can exist before any turn (eager spawn / warm pool)."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_an_unset_baseline_reports_changed(self, tmp_path: Path) -> None:
        """ "We do not know" must not resolve to "the children match".

        Readiness is probed a few seconds AFTER boot while a session can be
        spawned eagerly before it. A logout landing in that gap would otherwise be
        adopted as the starting point, and the pre-logout child would keep
        answering as the previous account with nothing left to detect it.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # No probe has run and nothing has been reconciled.
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_logout_before_the_delayed_probe_is_detected(self, tmp_path: Path) -> None:
        """The eager-spawn-before-probe window GPT identified.

        Child spawns under account A, the terminal logs out, and only THEN does
        the delayed boot probe run. Seeding the baseline from that probe would
        record the post-logout identity and strand the child.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # Logout happens BEFORE the probe.
        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()

        # The delayed probe now runs and sees the signed-out store.
        service._stamp_probe(await service.current_identity_fingerprint())

        # The pre-logout child must still be swept.
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True

    @pytest.mark.asyncio
    async def test_probes_never_move_the_session_baseline(self, tmp_path: Path) -> None:
        """Only a completed sweep advances it; a probe never does."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        assert service._session_identity is None

        _write_store(db, start_url="https://personal.awsapps.com/start")
        service._stamp_probe(await service.current_identity_fingerprint())
        assert service._session_identity is None

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        service.note_sessions_reconciled(live)
        changed_after, _ = await service.identity_changed_since_sessions()
        assert changed_after is False

    @pytest.mark.asyncio
    async def test_assume_ready_never_reports_a_change(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(
            home=tmp_path, environ={}, platform_name="linux", assume_ready=True
        )
        service._stamp_probe("something")
        assert await service.identity_changed_since_probe() is False


class TestBaselinesAreIndependent:
    """The status consumer must not be able to consume the retirement signal."""

    @pytest.mark.asyncio
    async def test_a_status_reprobe_does_not_hide_the_change_from_retirement(
        self, tmp_path: Path
    ) -> None:
        """The defect this pair of baselines exists to prevent.

        With ONE shared baseline: logout -> a status poll re-probes and stamps the
        new identity -> the next turn sees no change -> the stale child is never
        retired. The dashboard polls every few seconds and turns are minutes
        apart, so the poll essentially always wins that race.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        # Reconcile the retirement baseline explicitly -- an unset one now reports
        # changed, so the sweep has to have happened before this scenario starts.
        _, live0 = await service.identity_changed_since_sessions()
        service.note_sessions_reconciled(live0)
        assert await service.identity_changed_since_probe() is False
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

        # The account changes.
        _write_store(db, start_url="https://personal.awsapps.com/start")
        _expire_identity_cache(service)

        # The status consumer observes it FIRST and advances its own baseline,
        # exactly as an ordinary poll's re-probe does.
        assert await service.identity_changed_since_probe() is True
        service._stamp_probe(await service.current_identity_fingerprint())
        assert await service.identity_changed_since_probe() is False

        # Retirement must STILL see the change.
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_the_session_baseline_advances_only_when_told(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        await service.identity_changed_since_sessions()  # adopt

        _write_store(db, start_url="https://personal.awsapps.com/start")
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        # Re-asking without reconciling keeps reporting the change, so a failed
        # retirement is retried rather than silently recorded as handled.
        changed_again, _ = await service.identity_changed_since_sessions()
        assert changed_again is True

        service.note_sessions_reconciled(live)
        changed_after, _ = await service.identity_changed_since_sessions()
        assert changed_after is False

    @pytest.mark.asyncio
    async def test_an_unreconciled_baseline_never_reads_as_matching(self, tmp_path: Path) -> None:
        """Repeated asks keep reporting changed until a sweep reconciles.

        Replaces an earlier "first call adopts" behaviour, which was the hole GPT
        found: adopting on first read trusts children that may predate the read.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        first, live = await service.identity_changed_since_sessions()
        second, _ = await service.identity_changed_since_sessions()
        assert first is True
        assert second is True

        service.note_sessions_reconciled(live)
        after, _ = await service.identity_changed_since_sessions()
        assert after is False


class TestLatchNarrowingPolicy:
    """Narrowing readiness is right for a sign-out and wrong for a switch."""

    class _State:
        def __init__(self, service: object, sessions: object) -> None:
            self.kiro_prerequisite_service = service
            self.sessions = sessions

    class _Sessions:
        def __init__(self, complete: bool = True) -> None:
            self._complete = complete
            self.calls = 0

        async def retire_kiro_identity_sessions(
            self, fingerprint: str = "", *, spare_children: bool = False
        ):
            self.calls += 1
            return ([], self._complete)

    @pytest.mark.asyncio
    async def test_a_switch_to_a_valid_account_does_not_narrow_readiness(
        self, tmp_path: Path
    ) -> None:
        """The stuck-readiness sequence.

        A status poll observes the switch FIRST and stamps the new identity. If the
        turn path then narrowed unconditionally, readiness would go false while the
        fingerprints now MATCH -- so no ordinary poll re-probes and the card sits
        at "not signed in" until someone presses Check again.
        """

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        # Switch to another VALID account, then let a poll observe it first.
        _write_store(db, start_url="https://personal.awsapps.com/start")
        service._stamp_probe(await service.current_identity_fingerprint())
        service._status = type(service._status)(  # type: ignore[misc]
            **{**vars(service._status), "authenticated": True, "ready": True}
        )

        state = self._State(service, self._Sessions())
        await chat_runner._retire_sessions_on_identity_change(state)

        assert service._status.ready is True, "readiness was narrowed on a valid switch"

    @pytest.mark.asyncio
    async def test_an_actual_sign_out_does_narrow_readiness(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        service._status = type(service._status)(  # type: ignore[misc]
            **{**vars(service._status), "authenticated": True, "ready": True}
        )

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        state = self._State(service, self._Sessions())
        await chat_runner._retire_sessions_on_identity_change(state)

        assert service._status.ready is False
        assert service._status.authenticated is False

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_leaves_the_change_pending(self, tmp_path: Path) -> None:
        """A skipped holder must not be recorded as reconciled."""

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1

        # Still pending, so the next turn tries again.
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_warns_and_names_what_it_waits_on(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO sits below the default level, so the holdout was invisible."""

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        sessions.identity_sweep_waiting_on = ("channel:c1:m1 (channel member)",)  # type: ignore[attr-defined]
        state = self._State(service, sessions)

        with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.chat_runner"):
            await chat_runner._retire_sessions_on_identity_change(state)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, caplog.text
        assert "incomplete" in warnings[0].getMessage()
        assert "channel:c1:m1 (channel member)" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_complete_sweep_reconciles_once(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_store_is_never_reconciled(self, tmp_path: Path) -> None:
        """ "Cannot tell" must not become the accepted steady state.

        Reconciling an empty fingerprint would make every LATER account switch
        compare equal to "" and go undetected, while children keep running. Staying
        unreconciled re-sweeps each turn, bounding how long a child can outlive the
        account it loaded to one turn.
        """

        from kiro_crew.dashboard import chat_runner

        # No store on disk at all: the fingerprint is absent.
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)

        # Every turn re-sweeps rather than accepting the unreadable state.
        assert sessions.calls == 3
        assert service._session_identity is None


class TestReturnToBaselineAfterIncompleteSweep:
    """A switch BACK to the reconciled account must still retire interim holders.

    The baseline advances only on a COMPLETE sweep. So after an A->B sweep left a
    busy session behind, returning to A compares equal to the baseline and the
    identity check would see no change -- while successors that registered under B
    keep answering on B's credential, with no auth failure to report it.
    """

    class _State:
        def __init__(self, service: object, sessions: object) -> None:
            self.kiro_prerequisite_service = service
            self.sessions = sessions

    class _Sessions:
        """Mirrors the lifecycle service's pending-fingerprint bookkeeping."""

        def __init__(self, complete: bool = True) -> None:
            self._complete = complete
            self.calls = 0
            self.swept_with: list[str] = []
            self.spare_children_calls: list[bool] = []
            self.pending_identity_sweep_fingerprint = ""

        async def retire_kiro_identity_sessions(
            self, fingerprint: str = "", *, spare_children: bool = False
        ):
            self.calls += 1
            self.swept_with.append(fingerprint)
            self.spare_children_calls.append(spare_children)
            # Kept while a sweep stays incomplete; cleared the moment one completes.
            self.pending_identity_sweep_fingerprint = "" if self._complete else fingerprint
            return ([], self._complete)

    @pytest.mark.asyncio
    async def test_returning_to_the_baseline_account_still_sweeps(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        # Children already match A.
        service.note_sessions_reconciled(fp_a)

        # A -> B, and the sweep cannot finish (a busy session survives it).
        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        state = self._State(service, sessions)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1
        # The per-turn identity-change path opts into the live-children deferral
        # (sign-out keeps the default and cancels).
        assert sessions.spare_children_calls == [True]
        assert service._session_identity == fp_a
        pending_b = sessions.pending_identity_sweep_fingerprint
        assert pending_b and pending_b != fp_a

        # Back to A. The baseline now MATCHES the live account, but TWO
        # independent triggers still reach the B successors: the gate's own
        # fresh read observed B and latched (the interim-identity latch), and
        # the incomplete sweep recorded B as the pending fingerprint. Either
        # alone forces the retry; both are pinned here.
        _write_store(db)
        assert (await service.identity_changed_since_sessions())[
            0
        ] is True, "the observed interim account must latch and report changed"

        # Isolate the pending-fingerprint retry: with the latch cleared (as if
        # the observation had never happened), the identity predicate reports
        # no change -- and the B holders must STILL be swept via the pending
        # fingerprint alone.
        service._interim_identity_observed = False
        assert (await service.identity_changed_since_sessions())[0] is False

        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2, "the B holders were left serving under B"
        # The retry is captured afresh under the account now in use, not B's.
        assert sessions.swept_with[-1] != pending_b

    @pytest.mark.asyncio
    async def test_an_external_logout_cancels_children_on_the_per_turn_path(
        self, tmp_path: Path
    ) -> None:
        """An external ``kiro-cli logout`` is the fail-safe case, not a defer.

        The per-turn sweep is the only path that catches an external logout (it
        never goes through the sign-out handler). There the live fingerprint is
        empty, the account is gone, and an idle parent's children MUST be
        cancelled rather than deferred -- so the per-turn caller passes
        ``spare_children=bool(live)``, which is False here. Only a switch
        between two real accounts (non-empty live) defers.
        """

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        service.note_sessions_reconciled(fp_a)

        # External logout: the store is wiped, so the live read is empty.
        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True and live == ""

        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)
        await chat_runner._retire_sessions_on_identity_change(state)

        assert sessions.calls == 1
        assert sessions.swept_with == [""]
        # Empty live => the deferral is NOT requested => children are cancelled.
        assert sessions.spare_children_calls == [False]

    def test_identity_component_dropped_detects_a_partial_logout(self, tmp_path: Path) -> None:
        """A credential source present in the baseline but EMPTY in live dropped.

        An external ``kiro-cli logout`` wipes the store component while the Crew
        vault stays populated, so the combined live fingerprint is still nonempty
        but its store half is gone. That is a credential loss, not a switch.
        """
        sep, vault = kp._API_KEY_FINGERPRINT_SEP, kp._CREW_VAULT_FINGERPRINT_SEP
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # Baseline: store s1 + vault v1. Live: store gone, vault still v1.
        service._session_identity = f"s1{vault}v1"
        assert service.identity_component_dropped(f"{vault}v1") is True
        # The vault itself dropping (store kept) is also a drop.
        assert service.identity_component_dropped("s1") is True
        # The API-key component dropping is a drop too.
        service._session_identity = f"s1{sep}k1"
        assert service.identity_component_dropped("s1") is True

    def test_identity_component_added_is_not_a_drop(self, tmp_path: Path) -> None:
        """A component that APPEARS or merely CHANGES is a switch, not a loss, so
        the children are still spared.
        """
        vault = kp._CREW_VAULT_FINGERPRINT_SEP
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # A vault component appears on top of the same store: nothing dropped.
        service._session_identity = "s1"
        assert service.identity_component_dropped(f"s1{vault}v1") is False
        # A plain account switch (store changes, nothing lost): not a drop.
        service._session_identity = "s1"
        assert service.identity_component_dropped("s2") is False
        # Both present, both changed (a two-account switch): not a drop.
        service._session_identity = f"s1{vault}v1"
        assert service.identity_component_dropped(f"s2{vault}v2") is False

    def test_identity_component_dropped_is_false_with_no_baseline(self, tmp_path: Path) -> None:
        """An unset baseline answers False: nothing was there to drop, and that
        case already sweeps once on its own.
        """
        vault = kp._CREW_VAULT_FINGERPRINT_SEP
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert service._session_identity is None
        assert service.identity_component_dropped("") is False
        assert service.identity_component_dropped(f"s1{vault}v1") is False

    @pytest.mark.asyncio
    async def test_a_partial_logout_cancels_children_like_main(self, tmp_path: Path) -> None:
        """The gate's fail-safe: a dropped credential source forces the cancel path.

        When a source present in the baseline is gone from live (a partial
        external logout), the per-turn caller passes ``spare_children=False`` even
        though ``live`` is nonempty, so an idle parent with live children is
        retired and its children cancelled, exactly as the sign-out path does.
        """
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        # Baseline carries BOTH the store and a vault component.
        vault = kp._CREW_VAULT_FINGERPRINT_SEP
        service.note_sessions_reconciled(f"{fp_a}{vault}v1")

        # The store is unchanged, so the live read is still fp_a -- but the vault
        # component that was in the baseline is gone, so a source dropped.
        _expire_identity_cache(service)
        _, live = await service.identity_changed_since_sessions()
        assert live == fp_a  # nonempty: bool(live) alone would wrongly spare
        assert service.identity_component_dropped(live) is True

        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)
        await chat_runner._retire_sessions_on_identity_change(state)

        assert sessions.calls == 1
        # A dropped source forces the cancel path even though live is nonempty.
        assert sessions.spare_children_calls == [False]

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_retries_even_for_the_live_account(
        self, tmp_path: Path
    ) -> None:
        """Completion is the stop condition, not a fingerprint match.

        This trigger sweeps with the LIVE fingerprint while the baseline equals it,
        so the pending fingerprint an incomplete one records is the live account's.
        Stopping on that equality would abandon the retry with holders still to
        retire; only a sweep that COMPLETES clears the pending fingerprint.
        """

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        service.note_sessions_reconciled(fp_a)

        # An outstanding sweep FOR the live account, still incomplete.
        sessions = self._Sessions(complete=False)
        sessions.pending_identity_sweep_fingerprint = fp_a
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2, "an incomplete sweep stopped retrying"

        # A sweep that completes clears the pending fingerprint, which stops it.
        sessions._complete = True
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 3
        assert sessions.pending_identity_sweep_fingerprint == ""
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 3, "it kept sweeping after completion"


class TestBootSeededSessionBaseline:
    """Seeding at boot replaces the once-per-lifetime unset-baseline sweep.

    The unset baseline deliberately reads as changed (an unknown baseline must
    not resolve to "the children match"), which was designed to cost one sweep
    per gateway lifetime. On a live gateway that sweep's completion
    precondition -- nothing busy, nothing mid-start, no runtime surviving -- is
    routinely unsatisfiable: the sweep retires idle sessions, dashboard slots
    eagerly respawn them, the in-flight starts keep the NEXT sweep incomplete,
    and the baseline never advances. Every turn then re-triggers retirement,
    recycling healthy just-spawned children forever.

    Seeding the baseline at startup, BEFORE anything can spawn a kiro-backed
    child, removes the boot sweep without weakening the guarantee: every child
    necessarily postdates the seed read, so the account it authenticated under
    is the seeded one or a later one -- and a later one compares unequal, which
    is exactly the change the sweep exists to catch.
    """

    @pytest.mark.asyncio
    async def test_a_seeded_baseline_reports_unchanged(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is True

        changed, live = await service.identity_changed_since_sessions()
        assert changed is False
        assert live != ""

    @pytest.mark.asyncio
    async def test_an_unreadable_store_is_never_seeded(self, tmp_path: Path) -> None:
        """No store means no seed: the fail-safe unset-baseline sweep survives.

        Seeding "" would make "cannot tell" the accepted steady state -- every
        later account switch would compare equal to "" and go undetected. The
        unset baseline instead re-sweeps each turn, bounding how long a child
        can outlive the account it loaded.
        """

        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is False

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_a_hung_store_read_refuses_the_seed_within_the_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stalled store must not stall gateway boot.

        The seed's await sits on the startup path ahead of the listeners
        binding, so a hung store (locked SQLite, stalled network filesystem)
        must cost boot at most the deadline -- and the refusal must fall back
        to the existing fail-safe unset-baseline sweep, never to a guessed
        baseline.
        """

        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        async def _hang(*, allow_cached: bool = True) -> str:
            # A hung store: parked until the seed deadline cancels it.
            await asyncio.Event().wait()
            return "never-reached"

        monkeypatch.setattr(service, "current_identity_fingerprint", _hang)
        monkeypatch.setattr(kp, "_SEED_BASELINE_TIMEOUT_SECS", 0.05)

        assert await finished(service.seed_sessions_baseline(), "the bounded seed") is False

    @pytest.mark.asyncio
    async def test_a_switch_after_seeding_is_still_detected(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        _write_store(
            db,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_sign_out_after_seeding_is_still_detected(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_seeding_never_overwrites_a_recorded_baseline(self, tmp_path: Path) -> None:
        """A pending change must not be masked by a late seed call."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service.note_sessions_reconciled("fingerprint-of-a-previous-account")

        assert await service.seed_sessions_baseline() is False

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True

    @pytest.mark.asyncio
    async def test_a_second_seed_is_a_noop(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is True
        assert await service.seed_sessions_baseline() is False

    @pytest.mark.asyncio
    async def test_assume_ready_seeding_is_a_noop(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(
            home=tmp_path, environ={}, platform_name="linux", assume_ready=True
        )

        assert await service.seed_sessions_baseline() is False

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

    @pytest.mark.asyncio
    async def test_a_seeded_first_turn_never_marks_a_busy_session(self, tmp_path: Path) -> None:
        """The end-to-end repro of the retire/respawn loop, fixed at its root.

        Pre-fix: turn 1 reads the unset baseline as changed, the sweep marks the
        busy session ``retire_on_identity_change`` (guaranteeing a recycle at its
        next turn) and reports incomplete, so the baseline never advances and
        every later turn repeats it. Post-fix: the seeded baseline compares
        equal, no sweep fires, the busy session is untouched.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        smap = SessionManager(KiroCrewConfig())
        busy = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        busy.semaphore._value = 0  # type: ignore[attr-defined]
        smap._sessions["busy"] = busy

        # The turn path's decision, exactly as _retire_sessions_on_identity_change
        # takes it: no change and no pending sweep means no retirement call.
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False
        assert getattr(smap, "pending_identity_sweep_fingerprint", "") == ""
        assert busy.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_an_unseeded_busy_gateway_re_sweeps_every_turn(self, tmp_path: Path) -> None:
        """Characterizes the defect the seed removes (and the fail-safe kept).

        With no seed and a perpetually-busy session, the sweep can never
        complete, the baseline never advances, and every turn re-triggers
        retirement. This remains the DESIRED behaviour for the one case the
        seed refuses: a store that cannot be fingerprinted.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        smap = SessionManager(KiroCrewConfig())
        busy = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        busy.semaphore._value = 0  # type: ignore[attr-defined]
        smap._sessions["busy"] = busy

        for _turn in range(3):
            changed, live = await service.identity_changed_since_sessions()
            assert changed is True
            retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=live)
            assert retired == []
            assert complete is False
            # The incomplete sweep never advances the baseline (mirrors the
            # caller's `if complete and live` gate), so the loop repeats.

        assert busy.retire_on_identity_change is True

    def test_both_startup_sites_seed_before_serving(self) -> None:
        """Every place that constructs the service must seed it immediately.

        A construction site without a seed re-introduces the boot sweep for
        that entrypoint. Counted rather than AST-walked: the construction is
        spelled identically at both sites.
        """

        dashboard = Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard"
        source = "".join(
            path.read_text(encoding="utf-8")
            for path in (
                dashboard / "server.py",
                dashboard / "server_runtime" / "api_entrypoint.py",
            )
        )
        constructions = source.count("KiroPrerequisiteService,")
        seeds = source.count("seed_sessions_baseline()")
        assert constructions >= 2, "expected both startup sites to construct the service"
        assert seeds >= constructions, (
            f"{constructions} construction site(s) but only {seeds} seed call(s): "
            "a site that skips seed_sessions_baseline re-introduces the boot sweep loop"
        )


class TestInterimIdentityLatch:
    """An observed A->B->A round trip must still trigger the retirement sweep.

    The baseline comparison alone is blind to a round trip: seed A -> switch
    to B (a child spawns under B on a non-sweep path) -> switch back to A
    compares equal to the baseline, and the B-authenticated child serves the
    next turn. The latch records any fresh read that observed a DIFFERENT
    signed-in account and reports changed until a sweep completes, whatever
    the store says by then.

    The latch's own hazard is the mirror image: a sticky flag armed by a
    TRANSIENT read failure would force retire-until-complete sweeps on a
    healthy host -- re-creating the perpetual recycle loop the seeded
    baseline exists to remove. Half these tests therefore pin the refusals:
    component LOSS (an unreadable CLI store, a vanished vault component)
    never latches; only a component that appears or changes does, because
    reads lose components under failure, they do not gain them.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }

    async def _seeded_service(self, tmp_path: Path) -> "kp.KiroPrerequisiteService":
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True
        return service

    @pytest.mark.asyncio
    async def test_a_round_trip_observed_by_a_poll_still_sweeps(self, tmp_path: Path) -> None:
        """Switch A->B, a poll observes B, switch back to A: changed reports True."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        # Any fresh read is an observer -- here, the status surface's poll.
        observed = await service.current_identity_fingerprint(allow_cached=False)
        assert observed != ""

        _write_store(db)  # back to the seeded account
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True, "the observed interim account must force a sweep"
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_complete_sweep_clears_the_latch_and_turns_go_quiet(
        self, tmp_path: Path
    ) -> None:
        """Reconciliation resolves the observation; later turns must NOT re-sweep.

        The loop guard: if the latch survived reconciliation, every turn on a
        healthy host would retire healthy children forever -- the exact
        pathology the seeded baseline removes.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        _write_store(db)
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        service.note_sessions_reconciled(live)

        for _ in range(3):
            _expire_identity_cache(service)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False, "a reconciled latch must not keep forcing sweeps"

    @pytest.mark.asyncio
    async def test_an_unreadable_cli_store_blip_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient read failure is not an interim account.

        An unreadable store reads as absent; latching on it would make one
        blip sticky until a fully-quiescent sweep, which a live gateway may
        never produce. The blip itself still reports changed (non-sticky,
        existing behaviour) for as long as it persists -- but once the store
        reads fine again, turns must be quiet.
        """

        service = await self._seeded_service(tmp_path)

        real_fingerprint = kp.identity_fingerprint

        def _blip(path: object, **_kwargs: object) -> str:
            raise OSError("database is locked")

        monkeypatch.setattr(kp, "identity_fingerprint", _blip)
        _expire_identity_cache(service)
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: absent differs from the baseline
        assert live == ""

        monkeypatch.setattr(kp, "identity_fingerprint", real_fingerprint)
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered blip must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_a_vanished_vault_component_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vault component lost while the CLI account matches: refused.

        An unreadable vault reads as empty, indistinguishable from a vault
        sign-out, so a sticky latch on component loss would arm on a blip.
        """

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = await self._seeded_service(tmp_path)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: the combined digest differs

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered vault must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_a_vault_identity_round_trip_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault component that APPEARS is a real interim identity, not a blip.

        Reads lose components under failure; they do not gain them. A Crew
        sign-in that lands and is reverted between two turns leaves children
        holding its credential, exactly like a CLI round trip.
        """

        service = await self._seeded_service(tmp_path)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-INTERIM")
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "an observed interim vault identity must force a sweep"

    @pytest.mark.asyncio
    async def test_a_vault_only_round_trip_latches_with_no_cli_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault-only gateway must still catch a vault A->B->A round trip.

        With no CLI store at all, the CLI component is permanently absent --
        but a vault component that CHANGES to a different nonempty value
        cannot be a read blip (reads lose components under failure; they do
        not gain or alter them). Refusing to latch whenever the CLI is absent
        would make the entire latch inert on vault-only hosts, leaving
        B-authenticated children alive after the round trip.
        """

        # No _write_store: the CLI store never exists on this host.
        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-B")
        _expire_identity_cache(service)
        # A status poll observes the interim vault account.
        observed = await service.current_identity_fingerprint(allow_cached=False)
        assert observed != ""

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "a vault-only interim account must force a sweep"

    @pytest.mark.asyncio
    async def test_a_vault_only_blip_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vault-only host, vault read blips to empty: refused, no sticky latch.

        The loop guard for the vault-only arm: an empty vault under an absent
        CLI is indistinguishable from a transient vault read failure, so it
        must not arm the sticky flag -- only a nonempty, DIFFERENT vault does.
        Once the vault reads fine again, turns must be quiet.
        """

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: the combined digest differs

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered vault-only blip must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_an_unobserved_round_trip_remains_undetected(self, tmp_path: Path) -> None:
        """Pins the SERVICE-level boundary: the baseline stays blind by design.

        A round trip with NO fresh read during the interim window is invisible
        to a gateway-wide observer by construction, and this test pins that
        the baseline comparison honestly reports unchanged. The CHILD is no
        longer exposed by it: its spawn recorded the interim account
        (``read_spawn_identity`` -> ``stamp_spawn_identity``), and the turn
        gate's ``flag_identity_stamp_mismatches`` retires it from that record
        -- see ``TestSpawnIdentityStamp``. What remains open is a full round
        trip completing strictly between the two bracket reads of a single
        spawn, documented on ``stamp_spawn_identity``.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _write_store(db)  # round trip completes with no read in between
        _expire_identity_cache(service)

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

    @pytest.mark.asyncio
    async def test_a_stable_identity_never_latches_across_repeated_reads(
        self, tmp_path: Path
    ) -> None:
        """The healthy-host invariant: no observation, no latch, no sweeps."""

        service = await self._seeded_service(tmp_path)

        for _ in range(5):
            _expire_identity_cache(service)
            await service.current_identity_fingerprint(allow_cached=False)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False


class TestGenerationScopedLatchClear:
    """Reconciliation must not erase an observation NEWER than its own sweep.

    The sweep's initiating read covers every observation up to that moment.
    But the sweep releases its permits before the gate reconciles, so a
    status poll can observe ANOTHER account (a store flap) while the sweep
    runs -- and a child spawned in that window whose bracket reads disagree
    is unstamped, invisible to the stamp gate. An unconditional latch clear
    at reconcile would erase the only record that account existed; the
    generation scope keeps the latch armed so the next turn sweeps again.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }
    _THIRD = {
        "start_url": "https://third.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:3333:profile/THIRD",
    }

    async def _seeded_service(self, tmp_path: Path) -> "kp.KiroPrerequisiteService":
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True
        return service

    @pytest.mark.asyncio
    async def test_an_observation_during_the_sweep_survives_reconcile(self, tmp_path: Path) -> None:
        """The finding's scenario: A->B sweep, C observed mid-sweep, store back to B.

        The reconcile that completes the A->B sweep must NOT clear the latch
        the C observation armed: the sweep compared children against B, never
        against C, and clearing would leave a C-authenticated child serving
        the next B turn with nothing left to catch it.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        # The gate's initiating read observes the switch to B.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True and live != ""
        # What chat_runner captures right after the read.
        observed_gen = service.identity_observation_generation

        # While the sweep runs: a poll observes a flap to a THIRD account.
        _write_store(db, **self._THIRD)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)

        # The store returns to B before the sweep reconciles.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        service.note_sessions_reconciled(live, observations_before=observed_gen)

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, (
            "the C observation postdates the sweep's read; reconciling the "
            "A->B sweep must keep the latch armed so the next turn sweeps"
        )

    @pytest.mark.asyncio
    async def test_reconcile_with_no_newer_observation_clears_the_latch(
        self, tmp_path: Path
    ) -> None:
        """The loop guard: the ordinary reconcile still quiets later turns.

        When nothing was observed after the gate's read, the sweep covered
        every observation and the latch MUST clear -- a latch that survived
        this path would force retire-until-complete sweeps on a healthy host,
        the exact pathology the seeded baseline removes.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        _write_store(db)  # back to the seeded account
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        observed_gen = service.identity_observation_generation
        service.note_sessions_reconciled(live, observations_before=observed_gen)

        for _ in range(3):
            _expire_identity_cache(service)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False, "a covered observation must not keep forcing sweeps"

    @pytest.mark.asyncio
    async def test_a_repeat_observation_while_armed_still_bumps_the_generation(
        self, tmp_path: Path
    ) -> None:
        """An armed latch must not swallow later observations' recency.

        If arming short-circuited while already armed, an account observed
        DURING a sweep would carry the pre-sweep generation and the reconcile
        would clear it as covered -- the same erasure through a side door.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        gen_after_first = service.identity_observation_generation
        assert gen_after_first > 0

        _write_store(db, **self._THIRD)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        assert service.identity_observation_generation > gen_after_first, (
            "an observation landing while the latch is armed must still "
            "postdate a sweep that read the store before it"
        )

    def test_the_turn_gate_passes_the_captured_generation(self) -> None:
        """Source scan: chat_runner captures the generation and hands it back.

        ``observations_before=None`` clears unconditionally (the legacy test
        shape), so the production gate forgetting to pass its capture would
        silently reopen the erasure. Pin both halves of the handshake.
        """

        from pathlib import Path as _Path

        import kiro_crew.dashboard.chat_runner as chat_runner_module

        runner = _Path(chat_runner_module.__file__)
        text = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [*sorted(runner.with_name("chat_turn").glob("*.py")), runner]
            if turn_file.name != "__init__.py"
        )
        assert 'getattr(service, "identity_observation_generation", None)' in text, (
            "the turn gate must capture the observation generation right "
            "after its identity_changed_since_sessions read"
        )
        assert "note_sessions_reconciled(live, observations_before=observed_gen)" in text, (
            "the turn gate must hand its captured generation back at "
            "reconcile, or observations landing during the sweep are erased"
        )
