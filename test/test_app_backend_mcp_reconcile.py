"""The health-gated MCP registration of an app backend and its reconcile.

Registration and scrub follow the record they belong to, a reconcile that did
not land is retried, teardown takes the reconcile lock, and the scrub also
refreshes the materialized agents.
Fakes come from ``app_backend_helpers.py``.
"""

from __future__ import annotations

import pytest
from app_backend_helpers import _FakeHealthResp, _FakeProc, _probe_outcome

from kiro_crew.apps.backend import AppProcess


def _reconcile_lock_held() -> bool:
    """Whether the health reconcile lock is held right now.

    Probed from a SEPARATE thread on purpose: the lock is an RLock, so a same-thread
    acquire would succeed by re-entrancy and report False no matter who holds it.
    """
    import threading

    import kiro_crew.apps.backend as bmod

    result: list[bool] = []

    def _probe() -> None:
        got = bmod._health_reconcile_lock.acquire(blocking=False)
        result.append(not got)
        if got:
            bmod._health_reconcile_lock.release()

    t = threading.Thread(target=_probe)
    t.start()
    # flake-ok: joins a probe thread that only makes one non-blocking acquire; moved verbatim, tracked for a de-flake pass
    t.join()
    return result[0]


class TestHealthGatedMcpRegistration:
    """Health-gated MCP registration (review + review-bot race finding).

    The health-check loop must register an app's MCP servers ONLY when the backend is
    still tracked and healthy, and scrub them when it never becomes healthy — never write
    a dead-URL entry (the kiro-cli outage shape)."""

    def _fast_health(self, bmod, monkeypatch):
        # Make the loop iterate instantly.
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 3)

    def test_registers_when_healthy_and_still_tracked(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: calls.append((name, port, healthy)),
        )

        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())

        with bmod._lock:
            bmod._processes["hg-app"] = AppProcess(app_name="hg-app", port=9150, healthy=False)
        try:
            bmod._health_check_loop(bmod._processes["hg-app"], "/health")
            assert calls == [("hg-app", 9150, True)]  # registered exactly once, healthy
            assert bmod._processes["hg-app"].healthy is True
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_does_not_register_if_stopped_mid_healthcheck(self, monkeypatch):
        # review-bot race finding: app removed from _processes between the poll and the lock →
        # must NOT register MCP for a now-dead backend.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: calls.append((name, port, healthy)),
        )

        # urlopen "succeeds" but the app is NOT in _processes (stopped mid-check).
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())
        with bmod._lock:
            bmod._processes.clear()  # ensure absent

        bmod._health_check_loop(AppProcess(app_name="gone-app", port=9151), "/health")
        assert calls == []  # never registered — no dead-URL entry written

    def test_scrubs_when_never_healthy(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        self._fast_health(bmod, monkeypatch)
        calls = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: calls.append((name, port, healthy)),
        )

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(bmod, "loopback_urlopen", _boom)

        with bmod._lock:
            bmod._processes["sick-app"] = AppProcess(app_name="sick-app", port=9152, healthy=False)
        try:
            bmod._health_check_loop(bmod._processes["sick-app"], "/health")
            # Never healthy → scrub (healthy=False), never register.
            assert calls == [("sick-app", 9152, False)]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestMcpReconcileHonoursRecordIdentity:
    """A stale watcher must not move the SUCCESSOR's MCP entry (review finding).

    The MCP writers key on the app NAME, not on the record: `_deregister_mcp_servers`
    removes every `<app>:` entry. So the identity guard has to stay effective through
    the reconcile, not merely alongside the flag write — otherwise a demotion decided
    about a retired record scrubs the live successor's servers, and a stale promotion
    republishes the predecessor's dead port.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        calls: list[tuple[str, int, bool]] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (calls.append((name, port, healthy)), True)[1],
        )
        with bmod._lock:
            bmod._processes.clear()
        try:
            yield bmod, calls
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_retired_record_cannot_scrub_the_successors_servers(self, wired):
        bmod, calls = wired
        old = AppProcess(app_name="app", port=9180, healthy=True)
        new = AppProcess(app_name="app", port=9181, healthy=True)
        with bmod._lock:
            bmod._processes["app"] = new  # a stop/start already replaced `old`

        bmod._demote(old, reason="its own process exited")

        assert calls == []  # the successor's `app:` entries are untouched
        assert new.healthy is True
        assert bmod.get_app_backend_port("app") == 9181

    def test_a_retired_record_cannot_republish_its_dead_port(self, wired):
        bmod, calls = wired
        old = AppProcess(app_name="app", port=9182, healthy=False)
        new = AppProcess(app_name="app", port=9183, healthy=True)
        with bmod._lock:
            bmod._processes["app"] = new

        bmod._promote(old)

        assert calls == []  # never re-registers 9182, which nothing serves any more

    def test_the_reconcile_runs_under_the_serialization_lock(self, wired):
        # The ordering guarantee depends on the reconcile happening INSIDE
        # _health_reconcile_lock, not merely after the identity check. Observe it from
        # the reconcile itself, which is the only place the property is visible.
        bmod, calls = wired
        ap = AppProcess(app_name="app", port=9184, healthy=False)
        with bmod._lock:
            bmod._processes["app"] = ap
        held: list[bool] = []

        def _observe(name, port, *, healthy):
            held.append(_reconcile_lock_held())
            return True

        monkey = pytest.MonkeyPatch()
        monkey.setattr(bmod, "_gate_mcp_registration", _observe)
        monkey.setattr(bmod, "_app_enabled_state", lambda name: True)
        try:
            bmod._promote(ap)
        finally:
            monkey.undo()

        assert held == [True]

    def test_startup_registration_takes_the_same_lock_as_the_watch(self, monkeypatch):
        # Serializing only the watch would not be enough: the successor's own startup
        # registration has to queue behind a retiring watcher's reconcile, or the two
        # can still interleave and leave the dead port as the last write.
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_HEALTH_CHECK_RETRIES", 2)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *a, **k: _FakeHealthResp())
        held: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (held.append(_reconcile_lock_held()), True)[1],
        )
        ap = AppProcess(app_name="boot", port=9185, healthy=False)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["boot"] = ap
        try:
            assert bmod._health_check_loop(ap, "/health") is ap
            assert held == [True]
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestFailedMcpReconcileIsRetried:
    """A reconcile that did not land must be retried (review finding).

    The health FLAG moves whether or not mcp.json could be written. Gating the
    reconcile on the health *transition* alone therefore stranded a failed write until
    the next transition — which, for a backend that then stays put, never comes: a dead
    URL kiro-cli dials on every session, or a live backend with no MCP entry at all.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(
            app_name="w", port=9200, pid=1, proc=_FakeProc(), healthy=True, mcp_healthy=True
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["w"] = ap
        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                with bmod._lock:
                    bmod._processes.pop("w", None)
                return _probe_outcome(False)
            return _probe_outcome(probes.pop(0))

        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_failed_scrub_is_reattempted_on_the_next_sweep(self, monkeypatch, watched):
        bmod, ap, probes = watched
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) > 1  # the first write fails, the retry lands

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        # Demote on sweep 3, then two more sweeps with the verdict unchanged.
        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES + 2))
        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False, False]  # retried once, then stopped once it landed
        assert ap.mcp_healthy is False  # record only advances on a write that landed

    def test_a_landed_reconcile_is_not_rewritten_every_sweep(self, monkeypatch, watched):
        bmod, ap, probes = watched
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        probes.extend([False] * (bmod._HEALTH_WATCH_FAILURES + 3))
        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # one scrub; the steady state writes nothing further

    def test_an_exited_process_scrubs_an_entry_the_flag_had_already_left_behind(
        self, monkeypatch, watched
    ):
        # healthy=False but mcp_healthy=True is exactly the state a failed scrub leaves.
        # The terminal exit path has to consult the entry, not just the flag, or the
        # dead URL survives the watch's own shutdown.
        bmod, ap, probes = watched
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )
        ap.healthy = False
        ap.mcp_healthy = True
        ap.proc = _FakeProc(returncode=1)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]
        assert ap.mcp_healthy is False


class TestTerminalScrubIsRetriedUntilItLands:
    """The exit path may not leave the dead URL behind (review finding).

    This is the one place where giving up is permanent: nothing revisits an exited
    backend, so a scrub that did not land stays unlanded and kiro-cli keeps dialing the
    dead url every session. The retry the watch does elsewhere was useless here, because
    the terminal path returned before it could run.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        ap = AppProcess(
            app_name="x",
            port=9220,
            pid=3,
            proc=_FakeProc(returncode=1),
            healthy=True,
            mcp_healthy=True,
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["x"] = ap
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: _probe_outcome(False))
        try:
            yield bmod, ap
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_failed_terminal_scrub_is_retried(self, monkeypatch, wired):
        bmod, ap = wired
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) >= 3  # the first two writes fail

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False, False, False]  # retried until it landed
        assert ap.mcp_healthy is False

    def test_it_stops_as_soon_as_the_scrub_lands(self, monkeypatch, wired):
        bmod, ap = wired
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # one scrub, then the watch is done

    def test_it_gives_up_when_the_record_is_dropped_mid_retry(self, monkeypatch, wired):
        # stop_app_backend popping the record ends the watch even with the scrub
        # unlanded — that path owns the teardown from there, and a watch that spun on a
        # record nobody tracks would never exit.
        bmod, ap = wired
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            with bmod._lock:
                bmod._processes.pop("x", None)
            return False  # never lands

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)

        bmod._watch_backend_health(ap, "/health")

        assert attempts == [False]  # did not spin

    def test_nothing_registered_means_nothing_to_unwind(self, monkeypatch, wired):
        bmod, ap = wired
        ap.healthy = False
        ap.mcp_healthy = False
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )

        bmod._watch_backend_health(ap, "/health")

        assert attempts == []


class TestTeardownParticipatesInTheReconcileSerialization:
    """stop_app_backend takes the reconcile lock (review finding).

    A watcher that had already passed its identity check could still be inside
    `_gate_mcp_registration` when the caller's `deregister_app` scrubs — its write would
    land after, restoring the dead url the gate exists to keep out of mcp.json.
    """

    def test_stop_holds_the_reconcile_lock_across_the_pop(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        ap = AppProcess(app_name="s", port=9230, healthy=True, mcp_healthy=True)
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["s"] = ap
        held: list[bool] = []
        real_lock = bmod._lock

        class _SpyLock:
            """Records whether the reconcile lock is held whenever `_lock` is entered.

            Observing the nesting directly is the point: the guarantee is that the pop
            happens INSIDE the serialization, and only the lock state at the moment
            `_lock` is taken can show that.
            """

            def __enter__(self):
                held.append(_reconcile_lock_held())
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        monkeypatch.setattr(bmod, "_lock", _SpyLock())
        try:
            bmod.stop_app_backend("s")
        finally:
            monkeypatch.setattr(bmod, "_lock", real_lock)
            with bmod._lock:
                bmod._processes.clear()

        assert held and held[0] is True, held


class TestAdoptedRecoveryRebindsOwnership:
    """An adopted recovery must re-bind its owning PIDs (review finding).

    `adopted_pids` is what `stop_app_backend` signals and what uninstall acts behind. A
    recovery means the EXTERNAL supervisor put something back, possibly a different
    process — so promoting on the adoption-time identities would mark the record
    freshly-valid while naming a process that is gone.
    """

    @pytest.fixture
    def watched(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        monkeypatch.setattr(bmod, "_gate_mcp_registration", lambda name, port, *, healthy: True)
        ap = AppProcess(
            app_name="ad",
            port=9240,
            pid=0,
            proc=None,
            healthy=False,
            mcp_healthy=False,
            adopted_pids=[111],
            adopted_start_times={111: "old"},
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["ad"] = ap
        probes: list[bool] = []

        def _scripted(_port, _path):
            if not probes:
                with bmod._lock:
                    bmod._processes.pop("ad", None)
                return _probe_outcome(False)
            return _probe_outcome(probes.pop(0))

        monkeypatch.setattr(bmod, "_health_probe", _scripted)
        try:
            yield bmod, ap, probes
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_recovery_recaptures_the_owner_set(self, monkeypatch, watched):
        bmod, ap, probes = watched
        monkeypatch.setattr(
            bmod,
            "_capture_adopted_owners",
            lambda name, port, path: ([222], {222: "new"}),
        )
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is True
        assert ap.adopted_pids == [222]  # rebound to the replacement
        assert ap.adopted_start_times == {222: "new"}

    def test_unconfirmable_ownership_refuses_the_promotion(self, monkeypatch, watched):
        # Unhealthy-but-serving is recoverable next sweep; a mis-bound owner set is not,
        # because stop would signal the wrong PIDs while the replacement keeps running.
        bmod, ap, probes = watched
        monkeypatch.setattr(bmod, "_capture_adopted_owners", lambda name, port, path: None)
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert ap.healthy is False  # refused
        assert ap.adopted_pids == [111]  # untouched, not silently half-updated
        assert bmod.get_app_backend_port("ad") is None

    def test_a_spawned_backend_does_not_pay_the_recapture(self, monkeypatch, watched):
        # Only the adopted shape has external ownership to re-bind; a spawned backend
        # holds its own Popen and must not take this path.
        bmod, ap, probes = watched
        called: list[str] = []
        monkeypatch.setattr(
            bmod,
            "_capture_adopted_owners",
            lambda name, port, path: called.append(name) or ([9], {9: "x"}),
        )
        ap.proc = _FakeProc()
        probes.append(True)

        bmod._watch_backend_health(ap, "/health")

        assert called == []
        assert ap.healthy is True


class TestUnknownMcpStateStillGetsScrubbed:
    """`mcp_healthy` is tri-state; only False means confirmed-scrubbed (review finding).

    A startup reconcile that failed leaves `healthy=True, mcp_healthy=None` — the entry
    may well be on disk, and None records only that we never confirmed a write. Treating
    that as "nothing was ever registered" abandoned precisely the entry most in need of
    removal, one sweep after the process exited.
    """

    def test_a_none_mcp_state_keeps_retrying_the_terminal_scrub(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        attempts: list[bool] = []

        def _gate(name, port, *, healthy):
            attempts.append(healthy)
            return len(attempts) >= 3  # the first two scrubs fail

        monkeypatch.setattr(bmod, "_gate_mcp_registration", _gate)
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: _probe_outcome(False))

        # The state a failed startup reconcile leaves: promoted, never confirmed written.
        ap = AppProcess(
            app_name="u",
            port=9250,
            pid=5,
            proc=_FakeProc(returncode=1),
            healthy=True,
            mcp_healthy=None,
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["u"] = ap
        try:
            bmod._watch_backend_health(ap, "/health")
            assert attempts == [False, False, False]  # kept going past sweep 2
            assert ap.mcp_healthy is False
        finally:
            with bmod._lock:
                bmod._processes.clear()

    def test_a_confirmed_scrub_still_ends_the_watch(self, monkeypatch):
        import kiro_crew.apps.backend as bmod

        # These exercise promotion logic, not enablement: their app names are
        # fabricated and so are not in installed.json. The real gate is pinned by
        # TestPromotionRequiresAConfirmedEnabledApp.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)
        monkeypatch.setattr(bmod, "_HEALTH_WATCH_INTERVAL", 0)
        attempts: list[bool] = []
        monkeypatch.setattr(
            bmod,
            "_gate_mcp_registration",
            lambda name, port, *, healthy: (attempts.append(healthy), True)[1],
        )
        monkeypatch.setattr(bmod, "_health_probe", lambda *_a: _probe_outcome(False))

        ap = AppProcess(
            app_name="u",
            port=9251,
            pid=5,
            proc=_FakeProc(returncode=0),
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes.clear()
            bmod._processes["u"] = ap
        try:
            bmod._watch_backend_health(ap, "/health")
            assert attempts == []  # nothing to unwind, and it does not spin
        finally:
            with bmod._lock:
                bmod._processes.clear()


class TestScrubAlsoRefreshesMaterializedAgents:
    """Removing a server from the global map is only half the removal (review finding).

    An app's materialized agent JSONs COPY the server's launch spec, and the agent config
    is what kiro-cli actually loads — so scrubbing `mcp.json` alone leaves the agents
    still naming the dead url. The refresh goes through `bridges.refresh_app_agents`,
    which already carries the guards this path must honour: a `resources="app"` app
    publishes its own agents, and a denied app's are scrubbed rather than rewritten.
    """

    def test_an_unhealthy_reconcile_refreshes_the_apps_agents(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: list[str] = []
        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(
            brmod,
            "refresh_app_agents",
            lambda name, io_failures=None: (seen.append(name), [])[1],
        )

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("a", 9270, healthy=False) is True
        assert seen == ["a"]

    def test_a_healthy_reconcile_does_not_take_the_scrub_path(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: list[str] = []
        monkeypatch.setattr(
            brmod, "reregister_app_mcp_servers", lambda name, live_port=None, io_failures=None: []
        )
        monkeypatch.setattr(
            brmod,
            "refresh_app_agents",
            lambda name, io_failures=None: (seen.append(name), [])[1],
        )

        assert bmod._gate_mcp_registration("a", 9271, healthy=True) is True
        # reregister_app_mcp_servers does its own agent refresh; this must not double it.
        assert seen == []

    def test_an_agent_io_failure_makes_the_reconcile_unlanded(self, monkeypatch):
        # The agent JSON is the file kiro-cli reads, so a scrub whose agent half failed
        # has NOT achieved what the scrub exists for. Reporting it landed would let
        # `mcp_healthy` advance and strand the dead url there permanently.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])

        def _refresh(name, io_failures=None):
            if io_failures is not None:
                io_failures.append("a--agent.json")
            return []

        monkeypatch.setattr(brmod, "refresh_app_agents", _refresh)

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("a", 9272, healthy=False) is False

    def test_nothing_to_refresh_is_not_a_failure(self, monkeypatch):
        # A self-managed app, a denied one, and an app with no declared agents all return
        # an empty list. None is a failed write, and retrying them never converges — only
        # the io_failures collector means retry.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(brmod, "scrub_backend_mcp_url", lambda name, unreconciled=None: [])
        monkeypatch.setattr(brmod, "refresh_app_agents", lambda name, io_failures=None: [])

        # The demotion path's agent refresh is gated on the app still being
        # enabled — it re-materializes files a disable would have removed. The
        # gate itself is pinned by TestDemotionRefreshIsGatedOnEnablement.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda name: True)

        assert bmod._gate_mcp_registration("selfmanaged", 9273, healthy=False) is True


class TestRegistrationReportsAgentIoFailuresToo:
    """The healthy branch owes the same guarantee as the scrub (review finding).

    Both directions write the agent JSONs, and both let `mcp_healthy` advance on success.
    Reporting a failed agent write as landed on RECOVERY strands the app's agent without
    its MCP tools exactly as reporting one on demotion stranded the dead url — the same
    defect, mirrored, and fixing only one half was an oversight rather than a decision.
    """

    def test_a_failed_agent_write_on_recovery_is_unlanded(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        def _reregister(app_name, live_port=None, io_failures=None):
            if io_failures is not None:
                io_failures.append("a--agent.json")
            return ["a:backend"]

        monkeypatch.setattr(brmod, "reregister_app_mcp_servers", _reregister)

        assert bmod._gate_mcp_registration("a", 9300, healthy=True) is False

    def test_a_clean_registration_still_lands(self, monkeypatch):
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        monkeypatch.setattr(
            brmod,
            "reregister_app_mcp_servers",
            lambda app_name, live_port=None, io_failures=None: ["a:backend"],
        )

        assert bmod._gate_mcp_registration("a", 9301, healthy=True) is True

    def test_the_live_port_is_still_threaded_through(self, monkeypatch):
        # The collector must not displace the argument that makes the url reachable.
        import kiro_crew.apps.backend as bmod
        import kiro_crew.apps.bridges as brmod

        seen: dict[str, object] = {}

        def _reregister(app_name, live_port=None, io_failures=None):
            seen["port"] = live_port
            return []

        monkeypatch.setattr(brmod, "reregister_app_mcp_servers", _reregister)

        assert bmod._gate_mcp_registration("a", 9302, healthy=True) is True
        assert seen == {"port": 9302}
