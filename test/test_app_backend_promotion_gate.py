"""The enablement gate on an app backend's health promotion and its undo.

A promotion needs a confirmed enabled app and is verified after the write,
an undo is retried until it completes and never touches a successor, and an
unreadable enablement state never deletes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.apps.backend import AppProcess


class TestPromotionRequiresAConfirmedEnabledApp:
    """A disabled app must not be resurrected by a health recovery.

    `kirocrew app disable` runs in its OWN process: it deregisters the app's resources
    and never touches this process's tracking table. The record survives, so a later
    recovery would re-register the MCP servers and agents the operator just removed.
    Demotion is never gated — scrubbing is always safe.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        ap = AppProcess(app_name="app", port=9310, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_disabled_app_is_not_promoted(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert ap.healthy is False
        assert calls == []  # nothing re-registered for an app the operator disabled

    def test_an_enabled_app_is_promoted(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert ap.healthy is True
        assert calls == [("app", 9310, True)]

    def test_demotion_is_never_gated(self, monkeypatch, tracked):
        # Refusing a scrub because enablement cannot be confirmed would strand the dead
        # url — the exact failure this whole gate exists to prevent.
        bmod, ap, calls = tracked
        ap.healthy = True
        ap.mcp_healthy = True
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=False) is True
        assert ap.healthy is False
        assert calls == [("app", 9310, False)]

    # The unreadable-state case is covered by
    # TestEnabledStateDistinguishesUnreadableFromDisabled, which drives REAL files. A
    # stub of `is_app_enabled` cannot test it: the defect was that the reader collapses
    # a read failure into False without raising, so stubbing it to raise would assert a
    # path production never takes.


class TestPromotionIsVerifiedAfterTheWrite:
    """The enabled check cannot be atomic with the write.

    `kirocrew app disable` runs in another process, so there is no lock to share. Ordering
    closes the interleave where the flag is read after the resources come down; this
    covers the other one — the check passes, the disable completes, and the write lands
    afterwards, leaving a disabled app dispatchable.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        ap = AppProcess(app_name="app", port=9320, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_disable_landing_mid_write_is_undone(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        # Enabled at the pre-check, disabled by the time the write has landed.
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))
        undone: list[str] = []
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "deregister_app", lambda n: undone.append(n))

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert undone == ["app"]  # what the promotion registered is removed again
        assert ap.healthy is False
        assert ap.mcp_healthy is False

    def test_a_still_enabled_app_is_left_registered(self, monkeypatch, tracked):
        bmod, ap, calls = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "deregister_app",
            lambda n: pytest.fail("must not undo a promotion that is still valid"),
        )

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert ap.healthy is True
        assert calls == [("app", 9320, True)]

    def test_a_demotion_is_never_verified_or_undone(self, monkeypatch, tracked):
        # Demotion is ungated going in and must stay ungated coming out: scrubbing a
        # disabled app's entry is exactly what should happen.
        bmod, ap, calls = tracked
        ap.healthy = True
        ap.mcp_healthy = True
        monkeypatch.setattr(
            bmod,
            "_app_enabled_state",
            lambda name: pytest.fail("a demotion must not consult enablement"),
        )

        assert bmod._set_backend_health(ap, healthy=False) is True
        assert calls == [("app", 9320, False)]


class TestUndoIsRetriedUntilItCompletes:
    """`deregister_app` reports softly, so a failed undo must not look clean.

    It returns problems in `RegistrationResult.errors` rather than raising, so recording
    the removal without reading that list leaves a disabled app's resources registered
    while the record claims otherwise — and the retry condition then sees agreement and
    never fires.
    """

    @pytest.fixture
    def disabled(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True)
        ap = AppProcess(app_name="app", port=9330, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_soft_failure_leaves_the_record_unreconciled(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "deregister_app",
            lambda n: SimpleNamespace(errors=["could not remove agent: ENOSPC"]),
        )

        assert bmod._undo_promotion_of_disabled_app(ap) is False
        assert ap.healthy is False
        assert ap.mcp_healthy is True, "a removal that failed must not read as done"

    def test_a_clean_removal_is_recorded(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "deregister_app", lambda n: SimpleNamespace(errors=[]))

        assert bmod._undo_promotion_of_disabled_app(ap) is True
        assert ap.mcp_healthy is False

    def test_a_raising_deregister_also_leaves_it_unreconciled(self, monkeypatch, disabled):
        bmod, ap = disabled
        import kiro_crew.apps.bridges as brmod

        def _boom(n):
            raise OSError("agents dir unwritable")

        monkeypatch.setattr(brmod, "deregister_app", _boom)

        assert bmod._undo_promotion_of_disabled_app(ap) is False
        assert ap.mcp_healthy is True

    def test_a_refused_promotion_retries_the_undo(self, monkeypatch, disabled):
        # Nothing else revisits a disabled app and the watch will not promote it, so the
        # refusal path is where an unlanded undo gets another attempt.
        bmod, ap = disabled
        attempts: list[str] = []
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "deregister_app",
            lambda n: (attempts.append(n), SimpleNamespace(errors=["still failing"]))[1],
        )

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert bmod._set_backend_health(ap, healthy=True) is False
        assert attempts == ["app", "app"]  # retried, not recorded and forgotten

    def test_a_completed_undo_is_not_retried(self, monkeypatch, disabled):
        bmod, ap = disabled
        ap.mcp_healthy = False  # already reconciled
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "deregister_app",
            lambda n: pytest.fail("nothing of ours is registered; nothing to undo"),
        )

        assert bmod._set_backend_health(ap, healthy=True) is False


class TestDemotionRefreshIsGatedOnEnablement:
    """The demotion's agent refresh must not restore a disabled app.

    A demotion does two things: it scrubs the MCP entry — always safe, and deliberately
    ungated — and it re-materializes the agent configs. The second is a WRITE, so for an
    app the operator has disabled it puts back the very files a concurrent
    `deregister_app` just removed.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        scrubbed: list[str] = []
        refreshed: list[str] = []
        dropped: list[str] = []
        monkeypatch.setattr(
            brmod,
            "scrub_backend_mcp_url",
            lambda name, unreconciled=None: (scrubbed.append(name), [])[1],
        )
        monkeypatch.setattr(
            brmod,
            "refresh_app_agents",
            lambda name, io_failures=None: (refreshed.append(name), [])[1],
        )
        monkeypatch.setattr(
            bmod,
            "_drop_disabled_app_resources",
            lambda name: (dropped.append(name), True)[1],
        )
        return bmod, scrubbed, refreshed, dropped

    def test_a_disabled_app_is_scrubbed_but_never_refreshed(self, monkeypatch, wired):
        bmod, scrubbed, refreshed, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9340, healthy=False) is True
        assert scrubbed == ["app"]  # the scrub still happens — that is the desired outcome
        assert refreshed == []  # ...but nothing is written back
        assert dropped == ["app"]

    def test_a_disable_landing_mid_refresh_is_dropped(self, monkeypatch, wired):
        # Enabled at the pre-check, disabled by the time the refresh has landed. Neither
        # check can be atomic with the write, so only the pair converges.
        bmod, scrubbed, refreshed, dropped = wired
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._gate_mcp_registration("app", 9341, healthy=False) is True
        assert refreshed == ["app"]  # it did run
        assert dropped == ["app"]  # ...and was undone

    def test_an_enabled_app_is_refreshed_and_not_dropped(self, monkeypatch, wired):
        bmod, scrubbed, refreshed, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("app", 9342, healthy=False) is True
        assert refreshed == ["app"]
        assert dropped == []


class TestDisabledCleanupResultIsTheReconcileResult:
    """A failed disabled-app cleanup is not a completed reconcile."""

    def test_a_failed_cleanup_reports_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(bmod, "_drop_disabled_app_resources", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9350, healthy=False) is False

    def test_a_clean_cleanup_reports_landed(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)
        monkeypatch.setattr(bmod, "_drop_disabled_app_resources", lambda name: True)

        assert bmod._gate_mcp_registration("app", 9351, healthy=False) is True


class TestTheUndoNeverTouchesASuccessor:
    """Identity is checked BEFORE enablement.

    The undo deregisters by app NAME, so running it for a record that is not the
    tracked one deletes the SUCCESSOR's resources — and an unreadable enabled state is
    precisely what would send a retired watcher down that path.
    """

    def test_a_retired_record_with_unreadable_enablement_undoes_nothing(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        old = AppProcess(app_name="app", port=9360, healthy=False, mcp_healthy=True)
        successor = AppProcess(app_name="app", port=9361, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = successor
        monkeypatch.setattr(
            bmod,
            "_app_enabled_state",
            lambda name: pytest.fail("identity must be checked first"),
        )
        monkeypatch.setattr(
            bmod,
            "_undo_promotion_of_disabled_app",
            lambda ap: pytest.fail("must never deregister on behalf of a retired record"),
        )
        try:
            assert bmod._set_backend_health(old, healthy=True) is False
            assert successor.mcp_healthy is True
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestUnreadableManifestKeepsTheScrubUnlanded:
    """Keeping the agents is right, but it leaves them stale.

    `refresh_app_agents` gives up on the same unreadable manifest, so nothing else
    revisits those files. Recording the scrub as done would strand an agent config
    dialing the dead url forever; reporting it unlanded makes the watch retry until the
    manifest is readable and the refresh can correct them.
    """

    def test_an_unreadable_manifest_reports_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        def _scrub(name, unreconciled=None):
            if unreconciled is not None:
                unreconciled.append(f"{name}: manifest unreadable")
            return []

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", _scrub)

        assert bmod._gate_mcp_registration("app", 9370, healthy=False) is False

    def test_a_readable_manifest_reports_landed(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])

        assert bmod._gate_mcp_registration("app", 9371, healthy=False) is True


class TestUnknownEnablementNeverDeletes:
    """Fail-closed is right for ADDING and wrong for DELETING.

    `installed.json` can fail to read transiently. Refusing to register when enablement
    is unknown is safe — the app stays as it is. Deregistering when it is unknown unlinks
    materialized agents and takes the user-owned fields `_preserve_user_agent_edits`
    carries, permanently, over a temporary fault.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        dropped: list[str] = []
        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])
        monkeypatch.setattr(
            bmod,
            "_drop_disabled_app_resources",
            lambda name: (dropped.append(name), True)[1],
        )
        return bmod, dropped

    def test_unknown_enablement_does_not_deregister(self, monkeypatch, wired):
        bmod, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)

        assert bmod._gate_mcp_registration("app", 9380, healthy=False) is False
        assert dropped == [], "an unreadable state must never destroy user-edited agents"

    def test_a_confirmed_disable_does_deregister(self, monkeypatch, wired):
        bmod, dropped = wired
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._gate_mcp_registration("app", 9381, healthy=False) is True
        assert dropped == ["app"]

    def test_unknown_enablement_still_refuses_a_promotion(self, monkeypatch):
        # The other direction is unchanged: not-confirmed means do not add. Asserted
        # through the transition rather than a predicate, because what matters is that
        # the promotion does not happen AND nothing is deleted on the way.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)
        monkeypatch.setattr(
            brmod,
            "deregister_app",
            lambda n: pytest.fail("an unknown state must never delete"),
        )
        ap = AppProcess(app_name="app", port=9382, healthy=False, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=True) is False
            assert ap.healthy is False
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestATransitionAlwaysReconciles:
    """`mcp_healthy` can be stale in the other direction.

    An MCP write that landed followed by an agent write that did not leaves `mcp_healthy`
    unmoved while the entry IS on disk. If the verdict then flips, matching that stale
    value would skip the scrub and leave the dead url registered.
    """

    def test_a_demotion_reconciles_even_when_the_flags_agree(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append(healthy), True)[1],
        )
        # healthy=True with mcp_healthy=False is exactly what a partial reconcile leaves.
        ap = AppProcess(app_name="app", port=9390, healthy=True, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=False) is True
            assert calls == [False], "the transition must scrub, not trust the stale flag"
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_an_unchanged_verdict_still_short_circuits(self, monkeypatch):
        # The fast path has to survive: without it a settled backend would rewrite
        # mcp.json on every sweep.
        import kiro_crew.apps.backend as bmod

        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: pytest.fail("nothing changed; nothing to write"),
        )
        ap = AppProcess(app_name="app", port=9391, healthy=False, mcp_healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            assert bmod._set_backend_health(ap, healthy=False) is True
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestTheUndoPathsAlsoRefuseAnUnknownState:
    """The tri-state rule applies to ALL THREE deletion sites.

    The demotion path was fixed first; the two undo calls inside `_set_backend_health`
    were not, and they reach the same `deregister_app` → `_deregister_agents` → unlink.
    A transient `installed.json` fault must not destroy user-edited agent configs from
    any of them.
    """

    @pytest.fixture
    def tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        deleted: list[str] = []
        monkeypatch.setattr(brmod, "deregister_app", lambda n: deleted.append(n))
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True)
        ap = AppProcess(app_name="app", port=9400, healthy=False, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["app"] = ap
        try:
            yield bmod, ap, deleted
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_the_pre_check_undo_refuses_an_unknown_state(self, monkeypatch, tracked):
        bmod, ap, deleted = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: None)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == [], "unknown must not delete user-edited agents"

    def test_the_pre_check_undo_still_fires_on_a_confirmed_disable(self, monkeypatch, tracked):
        bmod, ap, deleted = tracked
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: False)

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == ["app"]

    def test_the_post_write_verify_refuses_an_unknown_state(self, monkeypatch, tracked):
        # Enabled at the pre-check, unreadable by the verify: the registration stands,
        # because the pre-check confirmed it and deleting is the unrecoverable direction.
        bmod, ap, deleted = tracked
        states = iter([True, None])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._set_backend_health(ap, healthy=True) is True
        assert deleted == []
        assert ap.healthy is True

    def test_the_post_write_verify_still_fires_on_a_confirmed_disable(self, monkeypatch, tracked):
        bmod, ap, deleted = tracked
        states = iter([True, False])
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: next(states))

        assert bmod._set_backend_health(ap, healthy=True) is False
        assert deleted == ["app"]


class TestEnabledStateDistinguishesUnreadableFromDisabled:
    """The tri-state has to be real, not nominal.

    `is_app_enabled` returns False for BOTH a deliberate disable and an unreadable
    metadata file, because `_read_installed` answers None to both and never raises. Built
    on that, the "unknown" branch was unreachable for exactly the transient fault it
    exists to catch — so a momentary read blip during a demotion still deleted a
    still-enabled app's user-edited agents. Driven against REAL files, since the whole
    defect was a collapsed return value that a stubbed reader would hide.
    """

    def _meta(self, monkeypatch, tmp_path):
        import kiro_crew.apps.manager as mgr

        app = tmp_path / "probe"
        app.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mgr, "app_dir", lambda name: app)
        return mgr, app / mgr.INSTALLED_META_FILENAME

    def test_an_unreadable_file_is_unknown_not_disabled(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text("{ not json", encoding="utf-8")

        assert mgr.is_app_enabled("probe") is False  # the collapsed answer
        assert mgr.app_enabled_state("probe") is None  # ...kept apart here

    def test_a_deliberate_disable_is_false(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text(json.dumps({"name": "probe", "enabled": False}), encoding="utf-8")

        assert mgr.app_enabled_state("probe") is False

    def test_an_enabled_app_is_true(self, monkeypatch, tmp_path):
        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text(json.dumps({"name": "probe", "enabled": True}), encoding="utf-8")

        assert mgr.app_enabled_state("probe") is True

    def test_a_missing_file_is_a_definite_false(self, monkeypatch, tmp_path):
        # Not installed is an ANSWER, not a failure to read one — deleting an
        # uninstalled app's leftovers is correct.
        mgr, _meta = self._meta(monkeypatch, tmp_path)

        assert mgr.app_enabled_state("probe") is False

    def test_the_backend_predicate_reports_unknown_for_an_unreadable_file(
        self, monkeypatch, tmp_path
    ):
        # The path that matters: backend must see None, or no deletion is ever refused.
        import kiro_crew.apps.backend as bmod

        mgr, meta = self._meta(monkeypatch, tmp_path)
        meta.write_text("{ not json", encoding="utf-8")

        assert bmod._app_enabled_state("probe") is None
