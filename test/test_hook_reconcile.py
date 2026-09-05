"""Tests for kiro_crew.apps.hook_reconcile — reload app hooks on out-of-process CLI mutation.

Feature: issue #7880 (CLI enable/disable/install/uninstall does not reach the
running gateway, so backend.hooks are never reloaded).

The in-process teardown/reimport itself (on_app_enable / on_app_disable /
unload_app_modules / the detached-startup machinery) is covered by
test_lifecycle_hooks.py; these tests drive the RECONCILER's decision logic.
The loaded-state is the SHARED registry in hooks_integration (record/clear/read),
not a private map, so a dashboard-driven enable/disable that updates it leaves
the reconciler nothing to re-do. The reconciler re-reads app state UNDER
app_lifecycle_lock and re-checks admission before enabling, so those are stubbed
per-test to a deterministic answer.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import kiro_crew.apps.hook_reconcile as hr
import kiro_crew.apps.hooks_integration as hi


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Pin KIROCREW_HOME so hook_signature's stats resolve under a throwaway dir,
    and reset the SHARED loaded-signature registry around every test."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()
    hr._inflight_app_tasks.clear()
    hr._stopping = False
    yield
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()
    hr._inflight_app_tasks.clear()
    hr._stopping = False


def _app_info(name: str, *, enabled: bool = True, version: str = "1.0.0", hooks: bool = True):
    backend = {"hooks": {"on_startup": "backend.hooks:on_startup"}} if hooks else {}
    return {
        "name": name,
        "enabled": enabled,
        "version": version,
        "manifest": {"backend": backend, "permissions": {}},
    }


@pytest.fixture
def _harness(monkeypatch):
    """Wire the reconciler's dependencies to deterministic in-memory doubles.

    - on_app_enable / on_app_disable record the calls and maintain the shared
      registry the way the real ones do (enable records, settled disable clears);
    - get_app returns whatever the test stages as the "current on-disk" state,
      re-read under the lock (defaults to the same snapshot);
    - hook_enable_denied returns "" (admitted) unless the test overrides it.

    Returns (calls, setters) where setters lets a test stage: current app_info,
    a disable result (to simulate an unsettled teardown), and a denial reason.
    """
    calls: list[tuple[str, str]] = []
    state: dict[str, Any] = {"current": {}, "disable_result": {}, "denied": ""}

    async def fake_enable(name, app_info, **kwargs):
        calls.append(("enable", name))
        # Mirror on_app_enable: the execution-DENIED path records the anti-churn
        # SIGNATURE ONLY (no manifest) and loads nothing; the admitted path
        # records the full loaded signature + manifest.
        if state["denied"]:
            await hi.record_hook_antichurn_signature(name, app_info)
        else:
            await hi.record_loaded_hook_signature(name, app_info)

    async def fake_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        res = dict(state["disable_result"])
        if not str(res.get("startup_cleanup", "")).startswith("failed:"):
            hi.clear_loaded_hook_signature(name)
        return res

    def fake_get_app(name):
        return state["current"].get(name)

    def fake_denied(name):
        return state["denied"]

    monkeypatch.setattr(hr, "on_app_enable", fake_enable)
    monkeypatch.setattr(hr, "on_app_disable", fake_disable)
    monkeypatch.setattr(hr, "get_app", fake_get_app)
    monkeypatch.setattr(hr, "hook_enable_denied", fake_denied)

    def set_current(*app_infos):
        state["current"] = {a["name"]: a for a in app_infos}

    def set_disable_result(result):
        state["disable_result"] = result

    def set_denied(reason):
        state["denied"] = reason

    return calls, (set_current, set_disable_result, set_denied)


# ---------------------------------------------------------------------------
# Transition: newly-enabled hook app -> load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newly_enabled_hook_app_is_loaded(_harness):
    calls, (set_current, _, _) = _harness
    app = _app_info("watchtower")
    set_current(app)
    await hr.reconcile_once([app])
    assert calls == [("enable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is not None


@pytest.mark.asyncio
async def test_enabled_app_without_hooks_is_ignored(_harness):
    calls, (set_current, _, _) = _harness
    app = _app_info("plain", hooks=False)
    set_current(app)
    await hr.reconcile_once([app])
    assert calls == []


@pytest.mark.asyncio
async def test_unchanged_signature_is_a_noop_second_pass(_harness):
    calls, (set_current, _, _) = _harness
    app = _app_info("watchtower")
    set_current(app)
    await hr.reconcile_once([app])
    await hr.reconcile_once([app])  # identical signature — no second enable
    assert calls == [("enable", "watchtower")]


@pytest.mark.asyncio
async def test_dashboard_enable_already_recorded_leaves_nothing_to_do(_harness):
    """Shared-source-of-truth: a dashboard enable already recorded the signature,
    so the reconciler's next tick must NOT re-run on_startup."""
    calls, (set_current, _, _) = _harness
    app = _app_info("watchtower")
    set_current(app)
    await hi.record_loaded_hook_signature("watchtower", app)  # as the handler would
    await hr.reconcile_once([app])
    assert calls == []


# ---------------------------------------------------------------------------
# Admission re-check under the lock (GPT: revive-during-trust-withdrawal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_app_is_not_revived_and_does_not_churn(_harness):
    """An admission-denied enabled hook app must route through on_app_enable's
    denied path (which loads nothing) — never a live reimport — and must not be
    re-attempted every tick once its signature is recorded."""
    calls, (set_current, _, set_denied) = _harness
    app = _app_info("watchtower")
    set_current(app)
    set_denied("third-party execution not granted")
    await hr.reconcile_once([app])
    assert calls == [("enable", "watchtower")]  # the denied on_app_enable path
    # Denied on_app_enable recorded the anti-churn signature, so a second tick is quiet.
    calls.clear()
    await hr.reconcile_once([app])
    assert calls == []


@pytest.mark.asyncio
async def test_denied_app_loads_once_trust_is_granted(_harness):
    """GPT finding: a denied app is recorded SIGNATURE-ONLY (anti-churn), and
    granting execution trust does NOT change the hook signature. A plain
    ``sig == loaded`` short-circuit would strand the now-admitted app forever, so
    the reconciler re-checks admission for a manifest-less (denied) record and
    loads it once it is admitted."""
    calls, (set_current, _, set_denied) = _harness
    app = _app_info("watchtower")
    set_current(app)
    # First tick: denied -> routes through on_app_enable's denied path, recording
    # a signature-only anti-churn record (no manifest).
    set_denied("third-party execution not granted")
    await hr.reconcile_once([app])
    assert calls == [("enable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is not None
    assert hi.loaded_hook_manifest("watchtower") is None  # signature-only

    # Trust is now granted; the on-disk hooks (and thus the signature) are
    # UNCHANGED. The reconciler must still load the app rather than short-circuit.
    calls.clear()
    set_denied("")
    await hr.reconcile_once([app])
    assert calls == [("enable", "watchtower")], "a now-admitted denied app must load"


@pytest.mark.asyncio
async def test_one_failing_app_does_not_stop_reconcile_of_the_rest(_harness, monkeypatch):
    """GPT [BLOCKING]: reconcile_once runs apps CONCURRENTLY with per-app
    exception isolation, so one app's raising teardown must not stop the others,
    AND -- unlike a cancelling timeout -- a slow teardown is never interrupted
    mid-sequence (which would skip route deregistration and leave a disabled
    app's routes callable). Here 'boom' raises and 'slow' runs to completion
    concurrently; both the raiser's siblings still reconcile."""
    calls, (set_current, _, _) = _harness
    order: list[str] = []

    async def concurrent_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        if name == "boom":
            raise RuntimeError("teardown blew up")
        if name == "slow":
            await asyncio.sleep(0.1)  # runs concurrently, NOT cancelled
        order.append(name)
        hi.clear_loaded_hook_signature(name)
        return {}

    monkeypatch.setattr(hr, "on_app_disable", concurrent_disable)
    # Three loaded apps, all now gone -> all hit the teardown branch.
    for n in ("boom", "slow", "quick"):
        await hi.record_loaded_hook_signature(n, _app_info(n))
    set_current()  # get_app -> None for all
    await hr.reconcile_once([])

    # The raising app did not stop the others: both completed their teardown.
    assert "slow" in order and "quick" in order
    assert hi.loaded_hook_signature("slow") is None
    assert hi.loaded_hook_signature("quick") is None
    # The raiser is left recorded (its teardown did not settle) -> retried next tick.
    assert hi.loaded_hook_signature("boom") is not None


@pytest.mark.asyncio
async def test_hung_teardown_does_not_wedge_the_pass(_harness, monkeypatch):
    """GPT [BLOCKING]: a nonterminating on_shutdown is awaited unbounded inside
    the dispatcher, so without a pass watchdog it would hang reconcile_once and
    -- since the loop awaits the pass -- wedge every future tick (later CLI
    disables never reconcile). The per-app WATCHDOG must let the pass RETURN while
    leaving the hung teardown RUNNING (never cancelled -- cancelling mid-on_shutdown
    would skip route dereg). Here 'hang' blocks forever and 'quick' tears down;
    the pass must complete and quick must be reconciled despite hang never
    returning."""
    calls, (set_current, _, _) = _harness
    monkeypatch.setattr(hr, "PER_APP_PASS_WATCHDOG_SECS", 0.1)  # trip fast in-test
    hang_cancelled = {"v": False}

    async def maybe_hang_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        if name == "hang":
            try:
                await asyncio.sleep(3600)  # nonterminating on_shutdown
            except asyncio.CancelledError:
                hang_cancelled["v"] = True
                raise
            return {}
        hi.clear_loaded_hook_signature(name)
        return {}

    monkeypatch.setattr(hr, "on_app_disable", maybe_hang_disable)
    for n in ("hang", "quick"):
        await hi.record_loaded_hook_signature(n, _app_info(n))
    set_current()  # get_app -> None for both -> teardown branch

    # The pass must RETURN despite hang never finishing (watchdog), within a bound
    # well under hang's 3600s sleep.
    await asyncio.wait_for(hr.reconcile_once([]), timeout=2.0)

    # quick was reconciled; hang's teardown was left running, NOT cancelled.
    assert hi.loaded_hook_signature("quick") is None, "quick must reconcile despite hang"
    assert hi.loaded_hook_signature("hang") is not None, "hung teardown left pending -> retried"
    assert hang_cancelled["v"] is False, "watchdog must NOT cancel the hung teardown"


@pytest.mark.asyncio
async def test_hung_teardown_is_not_respawned_next_tick(_harness, monkeypatch):
    """GPT [BLOCKING]: a straggler left running by the watchdog still holds the
    app's lifecycle lock, so re-spawning it every tick would pile up lock-waiter
    tasks until OOM. The reconciler must track one in-flight task per app and SKIP
    an app that still has a live one -- so a hung teardown produces exactly ONE
    outstanding task no matter how many ticks run."""
    calls, (set_current, _, _) = _harness
    monkeypatch.setattr(hr, "PER_APP_PASS_WATCHDOG_SECS", 0.05)

    async def hang_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        await asyncio.sleep(3600)  # nonterminating
        return {}

    monkeypatch.setattr(hr, "on_app_disable", hang_disable)
    await hi.record_loaded_hook_signature("hang", _app_info("hang"))
    set_current()  # get_app -> None -> teardown branch

    # Three back-to-back ticks while the teardown stays hung.
    for _ in range(3):
        await asyncio.wait_for(hr.reconcile_once([]), timeout=2.0)

    # Only ONE teardown was ever spawned; later ticks skipped the in-flight app.
    assert calls == [("disable", "hang")], "hung app must not be re-spawned each tick"
    assert len([t for t in hr._inflight_app_tasks.values() if not t.done()]) == 1


@pytest.mark.asyncio
async def test_stop_final_drain_is_bounded(monkeypatch):
    """GPT [BLOCKING]: the shutdown-time drain must be bounded so a hung reconcile
    cannot exceed _hooks_shutdown's ~10s budget and leave spawned backends running
    past exit. stop_hook_reconciler must return within ~SHUTDOWN_DRAIN_BUDGET_SECS
    even if the final reconcile pass never completes."""
    monkeypatch.setattr(hr, "POLL_INTERVAL_SECS", 100.0)
    monkeypatch.setattr(hr, "SHUTDOWN_DRAIN_BUDGET_SECS", 0.2)
    monkeypatch.setattr(hr, "list_apps", lambda: [])

    async def hung_reconcile_once(installed):
        await asyncio.sleep(3600)  # never settles

    monkeypatch.setattr(hr, "reconcile_once", hung_reconcile_once)

    hr.init_hook_reconciler()
    # stop must return well under the hung pass's 3600s despite the drain hanging.
    await asyncio.wait_for(hr.stop_hook_reconciler(), timeout=2.0)


@pytest.mark.asyncio
async def test_stop_does_not_reawait_a_hung_in_flight_pass(monkeypatch):
    """GPT round-10 [BLOCKING]: when a pass is already IN FLIGHT and hung, stop's
    bounded drain expires -- but the loop's cancel handler then re-awaits the same
    pass. That second await must ALSO be bounded, or it blocks up to the 60s pass
    watchdog and blows the ~10s graceful-shutdown budget before backend cleanup.
    Here a pass is hung and running when stop is called; stop must still return
    within a small multiple of the drain budget, not wait on the hung pass."""
    entered = asyncio.Event()

    async def hung_in_flight(installed):
        entered.set()
        await asyncio.sleep(3600)  # never settles -- simulates a wedged on_shutdown

    monkeypatch.setattr(hr, "POLL_INTERVAL_SECS", 0.01)
    monkeypatch.setattr(hr, "SHUTDOWN_DRAIN_BUDGET_SECS", 0.2)
    monkeypatch.setattr(hr, "list_apps", lambda: [])
    monkeypatch.setattr(hr, "reconcile_once", hung_in_flight)

    hr.init_hook_reconciler()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2.0)  # a pass is now hung in flight
        # Two drain budgets (0.2s each) + a final drain; must be well under the
        # 3600s hang and under the 60s pass watchdog. 3.0s is generous headroom.
        await asyncio.wait_for(hr.stop_hook_reconciler(), timeout=3.0)
    finally:
        await hr.stop_hook_reconciler()


@pytest.mark.asyncio
async def test_stop_drains_in_flight_pass_without_cancelling_it(monkeypatch):
    """GPT [BLOCKING]: stop_hook_reconciler must let an in-flight reconcile pass
    finish before cancelling the loop -- cancelling mid-teardown would interrupt
    an async on_shutdown's worker cleanup / buffer flush (app code survives or
    data is lost). Here a pass is made slow; stop is called while it runs and the
    pass must still complete. stop also runs ONE final drain pass after cancelling
    (see test_stop_runs_a_final_drain_pass_before_shutdown), so reconcile_once is
    called a second time -- what matters here is the in-flight pass was NOT
    cancelled."""
    completed: list[str] = []
    entered = asyncio.Event()

    async def slow_reconcile_once(installed):
        entered.set()
        await asyncio.sleep(0.2)  # simulate an in-flight teardown
        completed.append("done")

    monkeypatch.setattr(hr, "POLL_INTERVAL_SECS", 0.01)
    monkeypatch.setattr(hr, "list_apps", lambda: [])
    monkeypatch.setattr(hr, "reconcile_once", slow_reconcile_once)

    hr.init_hook_reconciler()
    try:
        await asyncio.wait_for(entered.wait(), timeout=2.0)  # a pass is now running
        await hr.stop_hook_reconciler()  # called mid-pass
        # The in-flight pass finished (not cancelled); the final drain adds one more.
        assert completed and completed[0] == "done", "in-flight pass must finish, not be cancelled"
    finally:
        await hr.stop_hook_reconciler()


@pytest.mark.asyncio
async def test_stop_runs_a_final_drain_pass_before_shutdown(monkeypatch):
    """GPT [BLOCKING]: a CLI disable landing between the last poll and stop would
    be dropped -- the loop is cancelled and on_gateway_shutdown only tears down
    what is still loaded, so the just-disabled app's on_shutdown flush is skipped.
    stop_hook_reconciler must run ONE final reconcile pass (after cancelling the
    poll loop, before returning to the caller that then calls on_gateway_shutdown)
    to settle that pending disable. It is one-shot, not a resurrected poll."""
    passes: list[str] = []

    async def counting_reconcile_once(installed):
        passes.append("pass")

    monkeypatch.setattr(hr, "POLL_INTERVAL_SECS", 100.0)  # no natural tick during the test
    monkeypatch.setattr(hr, "list_apps", lambda: [])
    monkeypatch.setattr(hr, "reconcile_once", counting_reconcile_once)

    hr.init_hook_reconciler()
    await hr.stop_hook_reconciler()
    # Exactly one final drain pass ran (the poll interval was too long to tick).
    assert passes == ["pass"], "stop must run exactly one final drain reconcile pass"


@pytest.mark.asyncio
async def test_degraded_app_with_retained_startup_is_torn_down(_harness, monkeypatch):
    """GPT [BLOCKING]: a degraded/timed-out startup leaves the loaded-signature
    record CLEARED (so the wiring retries on recovery) yet its detached startup
    task keeps running. A cleared record drops the app out of the teardown
    candidate set, so a later uninstall would orphan the task. The reconciler
    must still examine + tear down an app that answers app_has_retained_startup."""
    import kiro_crew.apps.lifecycle as lc

    calls, (set_current, _, _) = _harness
    # No loaded signature recorded (degraded), but a live detached startup task.
    task = asyncio.ensure_future(asyncio.sleep(3600))
    lc._DETACHED_HOOK_TASKS["watchtower"] = {task}
    try:
        assert hi.loaded_hook_signature("watchtower") is None  # degraded -> cleared
        assert lc.app_has_retained_startup("watchtower") is True
        assert "watchtower" in lc.apps_with_retained_startup()

        set_current()  # get_app -> None (uninstalled)
        await hr.reconcile_once([])  # candidate set must include the retained app

        # It was torn down (hooks-skipped teardown), not orphaned.
        assert calls == [("disable", "watchtower")]
    finally:
        task.cancel()
        lc._DETACHED_HOOK_TASKS.pop("watchtower", None)


@pytest.mark.asyncio
async def test_stopping_blocks_enable_but_not_teardown(_harness):
    """GPT [BLOCKING]: once the shutdown sweep begins (_stopping), a
    watchdog-stranded reconcile task must NOT enable/re-import hooks (it would
    resurrect app code after on_gateway_shutdown), but teardown must still run so
    the final drain can settle a pending disable."""
    calls, (set_current, _, _) = _harness

    # ENABLE is blocked while stopping: a newly-enabled app is NOT loaded.
    hr._stopping = True
    app = _app_info("watchtower")
    set_current(app)
    await hr.reconcile_once([app])
    assert calls == [], "no enable while stopping"
    assert hi.loaded_hook_signature("watchtower") is None

    # TEARDOWN still runs while stopping (the final drain needs it).
    await hi.record_loaded_hook_signature("watchtower", app)
    set_current()  # get_app -> None
    await hr.reconcile_once([])
    assert calls == [("disable", "watchtower")], "teardown must still run while stopping"


@pytest.mark.asyncio
async def test_denied_app_teardown_runs_no_shutdown_hook(_harness):
    """GPT [BLOCKING]: an execution-denied app is recorded SIGNATURE-ONLY (no
    retained manifest) because nothing of it was ever started. When it is later
    disabled/uninstalled, teardown must run NO ``on_shutdown`` for it -- running
    a denied app's shutdown-only code would be an execution vector. With no
    retained manifest, _disable_loaded falls back to the hooks-skipped teardown
    (run_app_hooks=False; route dereg + module unload still run by name)."""
    calls, (set_current, _, set_denied) = _harness
    run_flags: list[bool] = []

    async def capture_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        run_flags.append(kwargs.get("run_app_hooks"))
        hi.clear_loaded_hook_signature(name)
        return {}

    import kiro_crew.apps.hook_reconcile as _hr

    # Denied enable records signature-only (no manifest).
    app = _app_info("watchtower")
    set_current(app)
    set_denied("third-party execution not granted")
    await _hr.reconcile_once([app])
    assert hi.loaded_hook_signature("watchtower") is not None
    assert (
        hi.loaded_hook_manifest("watchtower") is None
    ), "denied app must retain NO manifest, or teardown could run its on_shutdown"
    # Now the app is uninstalled; the teardown branch fires.
    calls.clear()
    set_current()  # get_app -> None
    _hr.on_app_disable = capture_disable  # type: ignore[assignment]
    await _hr.reconcile_once([])
    assert calls == [("disable", "watchtower")]
    assert run_flags == [False], "denied app teardown must NOT run on_shutdown"


@pytest.mark.asyncio
async def test_denied_reinstall_of_loaded_app_is_torn_down_first(_harness):
    """Opus [BLOCKING]: a LOADED (running) hook app reinstalled out-of-process
    from a source that no longer matches its execution grant lands on the
    "signature changed + now denied" branch. The denied enable path deregisters
    routes + records anti-churn + drops the manifest, but does NOT stop the
    already-loaded module or the background task its on_startup spawned -- so the
    withdrawn-admission code would keep running indefinitely. The reconciler must
    ``_disable_loaded`` (tear the live module down) BEFORE routing through the
    denied enable."""
    calls, (set_current, _, set_denied) = _harness
    # v1 is loaded and RUNNING: recorded with a full manifest (admitted load).
    v1 = _app_info("watchtower", version="1.0.0")
    await hi.record_loaded_hook_signature("watchtower", v1)
    assert hi.loaded_hook_manifest("watchtower") is not None  # a live loaded app

    # Out-of-process reinstall to v2 (signature changes) from a source that is
    # now execution-denied.
    v2 = _app_info("watchtower", version="2.0.0")
    set_current(v2)
    set_denied("third-party execution not granted")
    await hr.reconcile_once([v2])

    # Teardown of the live module MUST precede the denied enable, in that order.
    assert calls == [("disable", "watchtower"), ("enable", "watchtower")], (
        "a denied reinstall of a loaded app must tear the running module down "
        "before the denied enable, not orphan it"
    )


@pytest.mark.asyncio
async def test_denied_reinstall_retries_when_teardown_unsettled(_harness):
    """Opus [BLOCKING] follow-through: if tearing the live module down does not
    settle, the reconciler must bail (retry next tick) rather than proceed to the
    denied enable and leave the app half-torn-down."""
    calls, (set_current, set_disable_result, set_denied) = _harness
    v1 = _app_info("watchtower", version="1.0.0")
    await hi.record_loaded_hook_signature("watchtower", v1)
    v2 = _app_info("watchtower", version="2.0.0")
    set_current(v2)
    set_denied("third-party execution not granted")
    set_disable_result({"startup_cleanup": "failed: task still running"})
    await hr.reconcile_once([v2])
    # Teardown attempted but did not settle -> no denied enable this tick.
    assert calls == [("disable", "watchtower")], "must retry, not proceed to enable"


# ---------------------------------------------------------------------------
# Transition: loaded app disabled / uninstalled -> teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loaded_app_turned_off_is_torn_down(_harness):
    calls, (set_current, _, _) = _harness
    app_off = _app_info("watchtower", enabled=False)
    set_current(app_off)
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
    await hr.reconcile_once([app_off])
    assert calls == [("disable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is None


@pytest.mark.asyncio
async def test_loaded_app_uninstalled_is_torn_down(_harness):
    calls, (set_current, _, _) = _harness
    set_current()  # get_app returns None → gone
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
    await hr.reconcile_once([])
    assert calls == [("disable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is None


@pytest.mark.asyncio
async def test_retained_startup_hook_defers_teardown(_harness):
    calls, (set_current, set_disable_result, _) = _harness
    app_off = _app_info("watchtower", enabled=False)
    set_current(app_off)
    set_disable_result({"startup_cleanup": "failed: detached startup hook is still running"})
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
    await hr.reconcile_once([app_off])
    assert calls == [("disable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is not None  # kept for retry


# ---------------------------------------------------------------------------
# Transition: reinstall of new code under a still-enabled app -> evict + reimport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_signature_evicts_then_reimports(_harness):
    calls, (set_current, _, _) = _harness
    v2 = _app_info("watchtower", version="2.0.0")
    set_current(v2)
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower", version="1.0.0"))
    await hr.reconcile_once([v2])
    assert calls == [("disable", "watchtower"), ("enable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is not None


@pytest.mark.asyncio
async def test_changed_signature_reimport_deferred_if_teardown_unsettled(_harness):
    calls, (set_current, set_disable_result, _) = _harness
    v2 = _app_info("watchtower", version="2.0.0")
    set_current(v2)
    set_disable_result({"startup_cleanup": "failed: detached startup hook is still running"})
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower", version="1.0.0"))
    await hr.reconcile_once([v2])
    assert calls == [("disable", "watchtower")]  # no half-swap


@pytest.mark.asyncio
async def test_reload_tears_down_from_retained_not_replacement_manifest(_harness):
    """GPT [BLOCK-MERGE]: on a code change the reload branch must stop the OLD
    running hook using the manifest it was LOADED from (retained registry), not
    the replacement on-disk manifest. Resolving teardown from the replacement
    would stop the wrong on_shutdown (or none, if renamed/removed) and leave the
    old hook running. Assert on_app_disable receives the retained (v1) manifest."""
    calls, (set_current, _, _) = _harness
    seen_manifests: list[dict[str, Any]] = []

    async def capture_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        seen_manifests.append(app_info.get("manifest"))
        hi.clear_loaded_hook_signature(name)
        return {}

    import kiro_crew.apps.hook_reconcile as _hr

    # v1 loaded with an on_shutdown that v2 removes; record v1 as the handler would.
    v1 = _app_info("watchtower", version="1.0.0")
    v1["manifest"]["backend"]["hooks"]["on_shutdown"] = "backend.hooks:on_shutdown_v1"
    await hi.record_loaded_hook_signature("watchtower", v1)
    v2 = _app_info("watchtower", version="2.0.0")  # no on_shutdown in v2
    set_current(v2)
    _hr.on_app_disable = capture_disable  # type: ignore[assignment]
    await _hr.reconcile_once([v2])

    assert calls[0] == ("disable", "watchtower")
    teardown_hooks = seen_manifests[0]["backend"]["hooks"]
    assert teardown_hooks.get("on_shutdown") == "backend.hooks:on_shutdown_v1", (
        "reload teardown must use the retained loaded (v1) manifest, not the "
        "replacement (v2) manifest whose on_shutdown differs"
    )


# ---------------------------------------------------------------------------
# Signature is hook-identity only (no reload on unrelated metadata edit)
# ---------------------------------------------------------------------------


def test_signature_ignores_installed_json_and_matches_on_hook_identity():
    a = _app_info("watchtower", version="1.0.0")
    assert hi.hook_signature(a) == hi.hook_signature(_app_info("watchtower", version="1.0.0"))
    assert hi.hook_signature(a) != hi.hook_signature(_app_info("watchtower", version="2.0.0"))
    # The signature is hook-CODE identity only: the ``enabled`` flag is an
    # orthogonal axis the reconciler checks separately, so it must NOT change the
    # signature (otherwise a disabled-copy record churns the next poll).
    assert hi.hook_signature(a) == hi.hook_signature(_app_info("watchtower", enabled=False))


@pytest.mark.asyncio
async def test_uninstalled_app_runs_shutdown_via_retained_manifest(_harness):
    """GPT security finding: a CLI disable+uninstall within one tick must still
    run on_shutdown so a background task the hook spawned is stopped, not orphaned
    after uninstall removes execution trust. The manifest is retained at load, so
    the gone-app teardown resolves on_shutdown from it (run_app_hooks=True)."""
    calls, (set_current, _, _) = _harness
    run_flags: list[bool] = []

    async def capture_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        run_flags.append(kwargs.get("run_app_hooks"))
        hi.clear_loaded_hook_signature(name)
        return {}

    import kiro_crew.apps.hook_reconcile as _hr

    # Record the loaded manifest (as on_app_enable would), then the app vanishes.
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
    set_current()  # get_app -> None (uninstalled)
    _hr.on_app_disable = capture_disable  # type: ignore[assignment]
    await _hr.reconcile_once([])
    assert calls == [("disable", "watchtower")]
    assert run_flags == [True], "on_shutdown must run against the retained manifest"


@pytest.mark.asyncio
async def test_uninstalled_app_with_no_retained_manifest_skips_hooks(_harness):
    """Fallback: an app we never recorded a manifest for cannot resolve its
    on_shutdown, so the gone-app teardown skips app hooks (run_app_hooks=False)
    while gateway-owned teardown still runs by name."""
    calls, (set_current, _, _) = _harness
    run_flags: list[bool] = []

    async def capture_disable(name, app_info, **kwargs):
        calls.append(("disable", name))
        run_flags.append(kwargs.get("run_app_hooks"))
        hi.clear_loaded_hook_signature(name)
        return {}

    import kiro_crew.apps.hook_reconcile as _hr

    # A signature present but NO retained manifest (simulate a legacy record).
    hi._loaded_hook_signatures["watchtower"] = ("1.0.0", 0, 0)
    set_current()
    _hr.on_app_disable = capture_disable  # type: ignore[assignment]
    await _hr.reconcile_once([])
    assert run_flags == [False]


@pytest.mark.asyncio
async def test_reimport_failure_leaves_signature_unset_for_retry(monkeypatch):
    calls: list[str] = []
    current = {"watchtower": _app_info("watchtower")}

    async def boom_enable(name, app_info, **kwargs):
        calls.append(name)
        raise RuntimeError("import blew up")

    async def ok_disable(name, app_info, **kwargs):
        hi.clear_loaded_hook_signature(name)
        return {}

    monkeypatch.setattr(hr, "on_app_enable", boom_enable)
    monkeypatch.setattr(hr, "on_app_disable", ok_disable)
    monkeypatch.setattr(hr, "get_app", lambda name: current.get(name))
    monkeypatch.setattr(hr, "hook_enable_denied", lambda name: "")
    await hr.reconcile_once([current["watchtower"]])
    assert calls == ["watchtower"]
    assert hi.loaded_hook_signature("watchtower") is None


@pytest.mark.asyncio
async def test_failed_shutdown_is_unsettled_and_retained(monkeypatch):
    """GPT round-11 [BLOCKING]: a failed on_shutdown means the app's own stop
    routine did not complete, so its worker may still be live. _disable_loaded
    must treat hooks_shutdown=='failed' as UNSETTLED -- return False and RETAIN
    the loaded record so the reconciler retries, rather than clearing the
    signature and accepting teardown while the worker survives."""
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))

    async def failing_shutdown_disable(name, app_info, **kwargs):
        return {"hooks_shutdown": "failed"}

    monkeypatch.setattr(hr, "on_app_disable", failing_shutdown_disable)
    monkeypatch.setattr(hr, "unload_app_modules", lambda name: 0)

    settled = await hr._disable_loaded("watchtower", {"name": "watchtower"})
    assert settled is False, "a failed on_shutdown must be reported as unsettled"
    assert (
        hi.loaded_hook_signature("watchtower") is not None
    ), "a failed shutdown must retain the loaded record for retry"
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()


@pytest.mark.asyncio
async def test_settled_teardown_unloads_modules_for_clean_reimport(monkeypatch):
    """GPT round-11 [BLOCKING]: a CLI reinstall (disable/enable without a process
    restart) that does not unload the app's modules reuses stale transitive
    helper modules via relative imports, so old code stays active. A SETTLED
    teardown must call unload_app_modules so the next enable re-imports fresh."""
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
    unloaded: list[str] = []

    async def clean_disable(name, app_info, **kwargs):
        hi.clear_loaded_hook_signature(name)
        return {"hooks_shutdown": "ok"}

    monkeypatch.setattr(hr, "on_app_disable", clean_disable)
    monkeypatch.setattr(hr, "unload_app_modules", lambda name: unloaded.append(name) or 1)

    settled = await hr._disable_loaded("watchtower", {"name": "watchtower"})
    assert settled is True
    assert unloaded == ["watchtower"], "settled teardown must unload the app's modules"
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()
