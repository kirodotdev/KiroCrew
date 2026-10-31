"""Unit coverage for the level-triggered app-backend backstop.

``backend_runtime/backstop.py`` is the coarse safety net that re-seats an enabled app's
backend when the edge-triggered health-watch/restart chain was lost. The
tests pin its two load-bearing properties with no real process, clock, or thread:

* the candidate set is exactly "enabled app, declares a backend entry point, NO tracked
  record" — and nothing else (disabled, no backend, already tracked in ANY form, Dev
  Fleet, or an unreadable enabled state are all excluded);
* a sweep seats each candidate through the internal ``_start_app_backend`` (no lifecycle
  START transition), re-checking record-absence and the START phase (and, after the
  unlocked spawn, re-reading the generation to stop a replacement a teardown raced), vets
  governance/admission fail-closed and audits a denial, and retries a failed revive every
  interval without ever permanently capping it.

Every collaborator is patched through the facade (``bmod.X``), which the composition
contract fans a write out from to the owner's own binding, so the owner under test runs
the double. ``_isolated_module_state`` (autouse) gives each test a clean process table.
"""

from __future__ import annotations

from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import _fake_proc

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess
from kiro_crew.apps.backend_runtime.restart import ActivationVerdict


def _app(
    name: str,
    *,
    enabled: bool = True,
    backend: bool = True,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    if backend:
        manifest["backend"] = {"entryPoint": "server.py"}
    return {"name": name, "enabled": enabled, "origin": "builtin", "manifest": manifest}


@pytest.fixture()
def backstop_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub the collaborators the backstop reaches out through.

    The sweep seats a record through the INTERNAL ``_start_app_backend`` (no lifecycle
    START transition), so that is what the double stands in for. It records the names it
    was asked to start and, by default, returns a fresh live record for each — the
    success path. A test overrides it to model a start that fails (returns None) or
    raises.
    """
    calls: dict[str, list[Any]] = {"started": [], "stopped": []}

    def _start(name: str) -> AppProcess | None:
        calls["started"].append(name)
        ap = AppProcess(app_name=name, port=5000, proc=_fake_proc(), healthy=True)
        with bmod._lock:
            bmod._processes[name] = ap
            bmod._seated_backends.add(name)  # a real seat marks the app seated
        return ap

    monkeypatch.setattr(bmod, "_start_app_backend", _start)
    monkeypatch.setattr(
        bmod, "stop_app_backend", lambda name, **kw: calls["stopped"].append((name, kw))
    )
    # Enablement reads True for everything unless a test says otherwise.
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
    # The common test apps have been seated once (so they are eligible for revival);
    # a test that wants an UNSEATED app clears the set.
    bmod._seated_backends.update({"alpha", "beta"})
    # The shutdown signal is clear by default.
    monkeypatch.setattr(bmod.shutdown_event, "is_set", lambda: False)
    return calls
    # The shutdown signal is clear by default.
    monkeypatch.setattr(bmod.shutdown_event, "is_set", lambda: False)
    return calls


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def test_candidate_is_enabled_with_backend_and_no_record(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one positive case: enabled, declares a backend, nothing tracked."""
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    assert bmod._backstop_candidates() == ["alpha"]


def test_disabled_app_is_not_a_candidate(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha", enabled=False)])
    assert bmod._backstop_candidates() == []


def test_app_without_backend_entrypoint_is_not_a_candidate(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha", backend=False)])
    assert bmod._backstop_candidates() == []


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(AppProcess(app_name="alpha", starting=True), id="starting-placeholder"),
        pytest.param(
            AppProcess(app_name="alpha", port=1, proc=_fake_proc(), healthy=True),
            id="live-record",
        ),
        pytest.param(
            AppProcess(app_name="alpha", adopted_pids=[1234]),
            id="adopted-record",
        ),
        pytest.param(
            AppProcess(app_name="alpha", port=1, proc=_fake_proc(returncode=1)),
            id="dead-but-tracked-awaiting-restart",
        ),
    ],
)
def test_any_tracked_record_excludes_the_app(
    backstop_env: dict[str, list[Any]],
    monkeypatch: pytest.MonkeyPatch,
    existing: AppProcess,
) -> None:
    """A record in ANY form means an existing lifecycle path owns the app — skip it.

    The lost-watch case the backstop exists for is a WHOLLY ABSENT record; a dead record
    still awaiting the restart loop, a STARTING placeholder, a live record and an adopted
    record are each left to their owner so the backstop never double-starts.
    """
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    with bmod._lock:
        bmod._processes["alpha"] = existing
    assert bmod._backstop_candidates() == []


def test_dev_fleet_is_never_a_candidate(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev Fleet needs the gateway's bound port at spawn — only the deferred wave starts it."""
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app(bmod.DEV_FLEET_APP_NAME)])
    assert bmod._backstop_candidates() == []


def test_unreadable_enabled_state_fails_closed(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tri-state None (metadata unreadable) is NOT a licence to start."""
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: None)
    assert bmod._backstop_candidates() == []


def test_an_unseated_app_is_not_a_candidate(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop revives only an app the gateway once SEATED, not one never started.

    An app enabled with a backend and no record, but that the gateway never successfully
    started (e.g. boot refused it on governance/admission/execution, or its first spawn
    has never run), is NOT a lost record — it is boot's/restart's concern. The seated-set
    membership is what keeps the backstop from re-vetting as a poller and from starting an
    app boot deliberately refused.
    """
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("ghost")])
    # "ghost" was never seated (fixture only seeds alpha/beta).
    assert "ghost" not in bmod._seated_backends
    assert bmod._backstop_candidates() == []
    # Once the gateway seats it, a later lost record makes it a candidate.
    with bmod._lock:
        bmod._seated_backends.add("ghost")
    assert bmod._backstop_candidates() == ["ghost"]


def test_stop_app_backend_clears_the_seated_name() -> None:
    """INVARIANT: every keep-down path clears the seated set, via stop_app_backend.

    disable, uninstall, trust revoke, update, gateway shutdown and the health watch's
    ceiling revocation all route through stop_app_backend, so clearing the name there
    covers them all. Exercise the real stop_app_backend (NOT the fixture's stub): seat an
    app, stop it, and confirm the name is gone from _seated_backends — so the backstop
    will not revive an app the gateway took down.
    """
    ap = AppProcess(app_name="alpha", port=5000, proc=_fake_proc(returncode=0), healthy=True)
    with bmod._lock:
        bmod._processes["alpha"] = ap
        bmod._seated_backends.add("alpha")
    bmod.stop_app_backend("alpha")
    assert "alpha" not in bmod._seated_backends
    assert bmod._lifecycle_generation.get("alpha", (0, bmod._LIFECYCLE_START))[1] == (
        bmod._LIFECYCLE_STOP
    )


def test_app_mid_deliberate_stop_is_not_a_candidate(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A popped record left at lifecycle STOP is a teardown in flight, not a lost watch.

    stop_app_backend pops the record and advances the generation to STOP while the app's
    persisted enabled can still read true (disable/revoke/update stop before flipping
    metadata). The backstop must not revive a backend the gateway is tearing down.
    """
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    with bmod._lock:
        bmod._processes.pop("alpha", None)
        bmod._lifecycle_generation["alpha"] = (7, bmod._LIFECYCLE_STOP)
    assert bmod._backstop_candidates() == []
    # A later START transition (e.g. re-enable) makes it a candidate again.
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (8, bmod._LIFECYCLE_START)
    assert bmod._backstop_candidates() == ["alpha"]


# ---------------------------------------------------------------------------
# Sweep behaviour
# ---------------------------------------------------------------------------


def test_sweep_seats_a_missing_record(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha"), _app("beta")])
    failures: dict[str, int] = {}
    seated = bmod._run_backstop_sweep(failures)
    assert sorted(seated) == ["alpha", "beta"]
    assert sorted(backstop_env["started"]) == ["alpha", "beta"]
    assert failures == {}


def test_sweep_is_a_noop_when_every_app_is_tracked(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    with bmod._lock:
        bmod._processes["alpha"] = AppProcess(
            app_name="alpha", port=1, proc=_fake_proc(), healthy=True
        )
    assert bmod._run_backstop_sweep({}) == []
    assert backstop_env["started"] == []


def test_revive_rechecks_stop_before_spawning(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STOP already set when _revive_one_backend runs is caught by the pre-check.

    The candidate list is a snapshot; a disable/revoke that popped the record and set STOP
    before this call must be honoured — the spawn is never reached.
    """

    def _must_not_run(name: str) -> AppProcess | None:  # pragma: no cover - must not run
        backstop_env["started"].append(name)
        return AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)

    monkeypatch.setattr(bmod, "_start_app_backend", _must_not_run)
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (4, bmod._LIFECYCLE_STOP)

    assert bmod._revive_one_backend("alpha") is None
    assert backstop_env["started"] == []  # the spawn was never reached


def test_revive_rechecks_enablement_before_spawning(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata-only disable (no STOP generation bump) is caught by the pre-spawn check.

    A cross-process CLI disable persists disabled metadata WITHOUT touching the lifecycle
    generation, so the STOP-phase check alone would miss it. _revive_one_backend re-reads
    enablement fail-closed right before the spawn, so a disabled app's startup code never
    runs.
    """

    def _must_not_run(name: str) -> AppProcess | None:  # pragma: no cover - must not run
        backstop_env["started"].append(name)
        return AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)

    monkeypatch.setattr(bmod, "_start_app_backend", _must_not_run)
    # Generation phase is START (no stop), but enablement now reads False.
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (3, bmod._LIFECYCLE_START)
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: False)

    assert bmod._revive_one_backend("alpha") is None
    assert backstop_env["started"] == []  # the spawn was never reached


def test_revive_stops_its_replacement_when_a_teardown_races_the_spawn(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STOP landing DURING the spawn is caught by the post-spawn generation re-check.

    The spawn runs outside the lock (it can take ~120 s), so a teardown can land while it
    is in flight. The post-spawn re-check sees the generation advance to STOP and stops
    the exact replacement it just spawned, by identity — the backstop's only stop.
    """
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (3, bmod._LIFECYCLE_START)

    spawned: dict[str, AppProcess] = {}

    def _spawn_then_teardown(name: str) -> AppProcess | None:
        backstop_env["started"].append(name)
        ap = AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)
        spawned[name] = ap
        # Simulate a disable/revoke landing DURING the spawn: generation advances to STOP.
        with bmod._lock:
            bmod._lifecycle_generation[name] = (4, bmod._LIFECYCLE_STOP)
        return ap

    monkeypatch.setattr(bmod, "_start_app_backend", _spawn_then_teardown)

    assert bmod._revive_one_backend("alpha") is None
    assert backstop_env["started"] == ["alpha"]  # the spawn did run
    # ... and its exact replacement was stopped by identity.
    assert backstop_env["stopped"] == [("alpha", {"_expected": spawned["alpha"]})]


def test_revive_keeps_replacement_when_post_spawn_enablement_read_is_unknown(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tri-state None (transient installed.json fault) after the spawn must NOT tear down.

    Stopping on an unknown read would record a STOP that then makes every later sweep skip
    the still-enabled app (the STOP-phase guard), stranding it — the exact failure this
    pass exists to prevent. Only a positively-decisive teardown (raced STOP / shutdown /
    enablement positively False) stops the replacement; an unknown read leaves it running.
    """
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (3, bmod._LIFECYCLE_START)

    spawned: dict[str, AppProcess] = {}

    def _spawn(name: str) -> AppProcess | None:
        backstop_env["started"].append(name)
        ap = AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)
        spawned[name] = ap
        return ap

    monkeypatch.setattr(bmod, "_start_app_backend", _spawn)
    # Enablement was True at the pre-check; it reads unknown (None) in the post-spawn check.
    reads = iter([True, None])
    monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: next(reads, None))

    ap = bmod._revive_one_backend("alpha")
    assert ap is spawned["alpha"]  # the replacement is kept
    assert backstop_env["stopped"] == []  # NOT torn down -> no synthetic STOP recorded


def test_revive_uses_internal_entry_not_public(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revive must go through the internal entry (no START bump), never the public one."""
    public_calls: list[str] = []
    monkeypatch.setattr(bmod, "start_app_backend", lambda name: public_calls.append(name))
    with bmod._lock:
        bmod._lifecycle_generation["alpha"] = (1, bmod._LIFECYCLE_START)
    ap = bmod._revive_one_backend("alpha")
    assert ap is not None
    assert backstop_env["started"] == ["alpha"]  # internal entry used
    assert public_calls == []  # public entry (which would bump lifecycle START) untouched


def test_failed_start_is_retried_every_sweep_never_capped(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that returns None counts as a failure but is NEVER permanently skipped.

    The backstop is a safety net: a transient fault (process-limit exhaustion) must
    self-heal on a later sweep, so a failed revive is retried every interval. The counter
    only throttles the log; it never gates the attempt.
    """
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha")])
    monkeypatch.setattr(
        bmod, "_start_app_backend", lambda name: backstop_env["started"].append(name)
    )
    failures: dict[str, int] = {}
    # Many more sweeps than any old cap: every one still attempts the app.
    for sweep in range(6):
        assert bmod._run_backstop_sweep(failures) == []
        assert failures["alpha"] == sweep + 1
    assert len(backstop_env["started"]) == 6  # attempted on EVERY sweep, never skipped

    # When the app finally starts, its failure counter is cleared.
    monkeypatch.setattr(bmod, "_start_app_backend", lambda name: AppProcess(app_name=name, port=1))
    assert bmod._run_backstop_sweep(failures) == ["alpha"]
    assert "alpha" not in failures


def test_raising_start_is_isolated_and_counted(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One app's start raising must not skip the rest of the sweep, and it is counted."""

    def _start(name: str) -> AppProcess | None:
        backstop_env["started"].append(name)
        if name == "alpha":
            raise RuntimeError("spawn boom")
        ap = AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)
        with bmod._lock:
            bmod._processes[name] = ap
        return ap

    monkeypatch.setattr(bmod, "_start_app_backend", _start)
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha"), _app("beta")])
    failures: dict[str, int] = {}
    seated = bmod._run_backstop_sweep(failures)
    assert seated == ["beta"]
    assert failures == {"alpha": 1}
    assert sorted(backstop_env["started"]) == ["alpha", "beta"]


def test_recovered_app_clears_its_failure_counter(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app that stops being a candidate (recovered/disabled) drops its stale counter."""
    monkeypatch.setattr(bmod, "list_apps", lambda: [])
    failures = {"alpha": 2}
    bmod._run_backstop_sweep(failures)
    assert failures == {}


def test_sweep_stops_on_shutdown(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown mid-sweep breaks out before starting any further candidate."""
    monkeypatch.setattr(bmod, "list_apps", lambda: [_app("alpha"), _app("beta")])
    monkeypatch.setattr(bmod.shutdown_event, "is_set", lambda: True)
    assert bmod._run_backstop_sweep({}) == []
    assert backstop_env["started"] == []


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def test_launcher_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two launches reuse one live daemon thread rather than starting a second."""
    import kiro_crew.apps.backend_runtime.backstop as bs

    started_threads: list[Any] = []

    real_thread_cls = bs.threading.Thread

    class _CountingThread(real_thread_cls):  # type: ignore[misc,valid-type]
        def start(self) -> None:  # noqa: D401 — test double
            started_threads.append(self)
            # Do NOT actually run _backstop_loop; it would sleep on the interval.

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(bs.threading, "Thread", _CountingThread)
    # Reset the owner's module-global thread handle (rebound with ``global`` in the
    # launcher, so it lives on the owner, not the facade mirror).
    monkeypatch.setattr(bs, "_backstop_thread", None, raising=False)
    bs.start_app_backend_backstop()
    bs.start_app_backend_backstop()
    assert len(started_threads) == 1


def test_revive_refuses_a_since_denied_activation_ceiling(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seated app whose activation ceiling tightened after the seat is NOT revived.

    Seating proves the app passed governance/admission AT THE SEAT, but the record can be
    lost by a lifecycle path outside the declared owners AFTER an administrator tightened
    POLICY ∩ PROFILE or admission. Reviving on the seated mark alone would respawn a
    backend the current ceiling refuses, so _revive_one_backend re-vets activation with the
    same check the boot/enable/restart spawn paths run. A non-transient deny spawns
    nothing and discards the seated name so later sweeps do not re-attempt it.
    """
    import kiro_crew.apps.backend_runtime.backstop as bs

    def _must_not_run(name: str) -> AppProcess | None:  # pragma: no cover - must not run
        backstop_env["started"].append(name)
        return AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)

    monkeypatch.setattr(bmod, "_start_app_backend", _must_not_run)
    # Build a real non-transient denial verdict.
    denied_verdict = ActivationVerdict(denied="policy ∩ profile refuses app", transient=False)
    monkeypatch.setattr(bs, "_activation_denied", lambda _n, _a: denied_verdict)

    assert "alpha" in bmod._seated_backends
    assert bs._revive_one_backend("alpha") is None
    assert backstop_env["started"] == []  # the spawn was never reached
    assert "alpha" not in bmod._seated_backends  # keep-down: not re-attempted


def test_revive_skips_but_keeps_seated_on_a_transient_activation_error(
    backstop_env: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient governance-evaluator error skips this sweep but keeps the seated name.

    An evaluator error is not an affirmative deny, so it is not decisive: discarding the
    seated name on it would strand a legitimately-enabled app whose ceiling still permits
    it. The replacement is not spawned this sweep and the name stays seated for the next.
    """
    import kiro_crew.apps.backend_runtime.backstop as bs

    def _must_not_run(name: str) -> AppProcess | None:  # pragma: no cover - must not run
        backstop_env["started"].append(name)
        return AppProcess(app_name=name, port=1, proc=_fake_proc(), healthy=True)

    monkeypatch.setattr(bmod, "_start_app_backend", _must_not_run)
    transient_verdict = ActivationVerdict(denied="governance evaluation error: x", transient=True)
    monkeypatch.setattr(bs, "_activation_denied", lambda _n, _a: transient_verdict)

    assert "alpha" in bmod._seated_backends
    assert bs._revive_one_backend("alpha") is None
    assert backstop_env["started"] == []  # the spawn was never reached
    assert "alpha" in bmod._seated_backends  # kept: retried next sweep
