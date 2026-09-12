"""Tests for kiro_crew.apps.hook_reconcile — reload app hooks on out-of-process CLI mutation.

Feature: the CLI enable/disable/install/uninstall does not reach the
running gateway, so backend.hooks are never reloaded.

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
from types import SimpleNamespace
from typing import Any

import pytest

import kiro_crew.apps.hook_reconcile as hr
import kiro_crew.apps.hooks_integration as hi
import kiro_crew.apps.teardown as teardown
from kiro_crew.apps.manager import app_enabled_state as real_app_enabled_state


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Pin KIROCREW_HOME so hook_signature's stats resolve under a throwaway dir,
    and reset the SHARED loaded-signature registry around every test."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()
    hr._inflight_app_tasks.clear()
    hr._pending_respawn.clear()
    hr._stopping = False
    yield
    hi._loaded_hook_signatures.clear()
    hi._loaded_hook_manifests.clear()
    hr._inflight_app_tasks.clear()
    hr._pending_respawn.clear()
    hr._stopping = False


def _app_info(name: str, *, enabled: bool = True, version: str = "1.0.0", hooks: bool = True):
    backend = {"hooks": {"on_startup": "backend.hooks:on_startup"}} if hooks else {}
    return {
        "name": name,
        "enabled": enabled,
        "version": version,
        "manifest": {"backend": backend, "permissions": {}},
    }


def _plain_backend_app(name: str, *, enabled: bool = True):
    """An app with a gateway-spawned backend and NO Python hooks (e.g. Cost AI)."""
    info = _app_info(name, enabled=enabled, hooks=False)
    info["manifest"]["backend"] = {"entryPoint": "backend/server.mjs", "port": "9200"}
    info["resources"] = "gateway"
    return info


#: Backend-process double shared between ``_harness`` and the tests below.
_BACKEND: dict[str, Any] = {}


def _spawned():
    """A tracked record for a child THIS gateway spawned (``proc`` set)."""
    return SimpleNamespace(proc=object())


def _adopted():
    """A tracked record adopted from another supervisor (``proc`` is None)."""
    return SimpleNamespace(proc=None)


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

    def fake_enabled_state(name):
        # Tri-state like the real read: True/False from staged metadata; an app
        # with nothing staged is confirmed ABSENT (False), which is what the real
        # read answers under the throwaway home. Tests that need "unknown"
        # (None) override this explicitly.
        info = state["current"].get(name)
        return False if info is None else bool(info.get("enabled"))

    def fake_denied(name):
        return state["denied"]

    monkeypatch.setattr(hr, "on_app_enable", fake_enable)
    monkeypatch.setattr(hr, "on_app_disable", fake_disable)
    monkeypatch.setattr(hr, "get_app", fake_get_app)
    monkeypatch.setattr(hr, "app_enabled_state", fake_enabled_state)
    monkeypatch.setattr(hr, "hook_enable_denied", fake_denied)
    # Backend process transitions are recorded, never executed: no test here
    # may spawn or signal a real process. State lives in the module-level
    # ``_BACKEND`` double so tests can stage a tracked process / generation
    # verdict without changing the harness's return shape.
    _BACKEND.clear()
    _BACKEND.update({"process": None, "generation": None, "lifecycle": []})
    monkeypatch.setattr(hr, "get_app_process", lambda name: _BACKEND["process"])
    monkeypatch.setattr(hr, "tracked_backend_names", lambda: [])
    monkeypatch.setattr(
        hr, "backend_secret_generation_matches", lambda name: _BACKEND["generation"]
    )

    def fake_stop(name):
        _BACKEND["lifecycle"].append("stop")
        _BACKEND["process"] = None
        return True

    def fake_start(name):
        _BACKEND["lifecycle"].append("start")
        _BACKEND["process"] = _spawned()
        return _BACKEND["process"]

    monkeypatch.setattr(hr, "stop_app_backend", fake_stop)
    monkeypatch.setattr(hr, "start_app_backend", fake_start)

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
    assert _BACKEND["lifecycle"] == []  # no backend declared -> nothing to converge


# ---------------------------------------------------------------------------
# No-hook gateway-managed backends: the process generation is runtime state too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_reinstall_rotating_the_secret_replaces_a_hookless_backend(_harness):
    """The Cost AI defect: ``kirocrew app uninstall`` + ``install`` rotated
    ``.app_secret`` while the gateway kept the OLD child running and healthy,
    so every gateway-signed request was refused as PROXY_AUTH_FAILED until a
    full restart. The app declares no Python hooks, so the hook-only candidate
    set never examined it. The reconciler must stop the retired generation and
    spawn one under the current secret — without touching hook lifecycle."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()  # tracked, spawned under the old secret
    _BACKEND["generation"] = False  # on-disk secret differs from the spawn-time one

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == ["stop", "start"]
    assert calls == []  # no hook enable/disable for a hookless app


@pytest.mark.asyncio
async def test_matching_or_unknown_generation_leaves_the_backend_alone(_harness):
    """Only a POSITIVE mismatch is destructive. ``None`` covers an adopted
    external backend and the window mid-transaction where the secret file is
    absent; restarting on either would be the churn this reconciler avoids."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    for verdict in (True, None):
        _BACKEND["generation"] = verdict
        await hr.reconcile_once([app])
        assert _BACKEND["lifecycle"] == [], verdict


@pytest.mark.asyncio
async def test_cli_disable_leaves_a_tracked_hookless_backend_alone(_harness, monkeypatch):
    """Stopping a disabled app's backend is not this module's transition: the
    proxy refuses a disabled app with 403 before forwarding, and the reconciler
    stops a process only to replace it. A respawn owed to that generation is
    dropped, so a later re-enable is a first start (boot's / the dashboard's)."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai", enabled=False)
    set_current(app)
    _BACKEND["process"] = _spawned()
    hr._pending_respawn["cost-ai"] = (0.0, None)
    monkeypatch.setattr(hr, "tracked_backend_names", lambda: ["cost-ai"])

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == [] and "cost-ai" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_cli_uninstall_leaves_a_tracked_hookless_backend_alone(_harness, monkeypatch):
    """The CLI uninstall does not stop a running backend and neither does the
    reconciler (the proxy refuses a removed app with 403). Only a respawn owed
    to the removed install is dropped -- and only on a POSITIVELY confirmed
    absence; unreadable metadata is not absence."""
    calls, (set_current, _, _) = _harness
    set_current()  # get_app -> None
    _BACKEND["process"] = _spawned()
    hr._pending_respawn["cost-ai"] = (0.0, None)
    monkeypatch.setattr(hr, "tracked_backend_names", lambda: ["cost-ai"])

    monkeypatch.setattr(hr, "app_enabled_state", lambda name: None)
    await hr.reconcile_once([])
    assert _BACKEND["lifecycle"] == [] and "cost-ai" in hr._pending_respawn

    monkeypatch.setattr(hr, "app_enabled_state", lambda name: False)
    await hr.reconcile_once([])
    assert _BACKEND["lifecycle"] == [] and "cost-ai" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_reconciler_never_starts_a_backend_it_did_not_stop(_harness):
    """An enabled app with nothing tracked is NOT started here: a first start
    belongs to boot and the dashboard, which vet policy before they spawn. The
    reconciler only restores a generation it took down itself."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = None

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == [] and calls == []


@pytest.mark.asyncio
async def test_failed_respawn_is_retried_only_as_a_pending_replacement(_harness, monkeypatch):
    """The stop half of a replacement succeeded but the start half did not. The
    next ticks finish the replacement (under backoff) because this reconciler
    began it; the same missing process without a pending replacement is left
    alone (see the test above)."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False
    real_start = hr.start_app_backend
    monkeypatch.setattr(hr, "start_app_backend", lambda name: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])

    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"] and "cost-ai" in hr._pending_respawn

    # Inside the window: no retry.
    monkeypatch.setattr(hr, "start_app_backend", real_start)
    clock["t"] += 1.0
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"]

    # Window elapsed: the replacement is finished.
    clock["t"] += hr.START_RETRY_BACKOFF_SECS
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop", "start"]
    assert "cost-ai" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_hookless_backend_is_not_spawned_once_shutdown_began(_harness):
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = None
    hr._pending_respawn["cost-ai"] = (0.0, None)  # a replacement is owed
    hr._stopping = True

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_unstoppable_adopted_backend_is_not_doubled(_harness, monkeypatch):
    """stop_app_backend restores the tracking record when an adopted backend
    cannot be signalled; spawning a competitor onto the same fixed port would
    crash-loop on EADDRINUSE."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    kept = _spawned()
    _BACKEND["process"] = kept
    _BACKEND["generation"] = False

    def refusing_stop(name):
        _BACKEND["lifecycle"].append("stop")
        return False  # record restored, process still tracked

    monkeypatch.setattr(hr, "stop_app_backend", refusing_stop)
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"]


@pytest.mark.asyncio
async def test_a_backend_that_fails_to_start_is_not_retried_every_tick(_harness, monkeypatch):
    """The boot path tries once; the reconciler must not turn a permanently
    failing respawn (crash, held port, execution refused) into a 15s
    retry-and-log loop."""
    calls, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False  # a replacement is due
    attempts: list[str] = []

    def failing_start(name):
        attempts.append(name)
        return None

    monkeypatch.setattr(hr, "start_app_backend", failing_start)
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])

    await hr.reconcile_once([app])
    await hr.reconcile_once([app])
    await hr.reconcile_once([app])
    assert attempts == ["cost-ai"], "only the first attempt inside the backoff window"

    clock["t"] += hr.START_RETRY_BACKOFF_SECS + 1
    await hr.reconcile_once([app])
    assert attempts == ["cost-ai", "cost-ai"]


# ---------------------------------------------------------------------------
# Hook-declaring apps: the backend process converges around the hooks
# ---------------------------------------------------------------------------


def _hook_backend_app(name: str, *, enabled: bool = True):
    """An app with BOTH Python hooks and a gateway-spawned backend."""
    info = _app_info(name, enabled=enabled, hooks=True)
    info["manifest"]["backend"]["entryPoint"] = "backend/server.py"
    info["resources"] = "gateway"
    return info


@pytest.mark.asyncio
async def test_cli_reinstall_of_a_hook_app_replaces_its_backend_between_the_hooks(
    _harness, monkeypatch
):
    """Same defect as the hookless case, for an app whose hooks the reconciler
    already reloaded: the hooks came back fresh while the backend kept the OLD
    secret. Order is load-bearing -- on_shutdown against the live old backend,
    then stop/start, then on_startup against the new one."""
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    await hi.record_loaded_hook_signature("watchtower", app)  # hooks loaded, gen A
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False  # reinstall rotated the secret

    # Force a signature change (what a reinstall's new .app_secret mtime does).
    monkeypatch.setattr(hr, "compute_hook_signature", _async_return(("2.0.0", 0, 999)))
    order: list[str] = []
    monkeypatch.setattr(hr, "on_app_disable", _recording_disable(order, calls, hi))
    monkeypatch.setattr(hr, "on_app_enable", _recording_enable(order, calls, hi))
    real_stop, real_start = hr.stop_app_backend, hr.start_app_backend
    monkeypatch.setattr(hr, "stop_app_backend", lambda n: order.append("stop") or real_stop(n))
    monkeypatch.setattr(hr, "start_app_backend", lambda n: order.append("start") or real_start(n))

    await hr.reconcile_once([app])

    assert order == ["on_shutdown", "stop", "start", "on_startup"]
    assert _BACKEND["lifecycle"] == ["stop", "start"]


@pytest.mark.asyncio
async def test_hook_app_with_unchanged_hooks_still_gets_a_drifted_backend_replaced(
    _harness, monkeypatch
):
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    await hi.record_loaded_hook_signature("watchtower", app)
    loaded = hi.loaded_hook_signature("watchtower")
    monkeypatch.setattr(hr, "compute_hook_signature", _async_return(loaded))
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False

    await hr.reconcile_once([app])

    assert calls == []  # hooks untouched
    assert _BACKEND["lifecycle"] == ["stop", "start"]


@pytest.mark.asyncio
async def test_hook_app_gone_tears_down_hooks_and_leaves_its_backend_alone(_harness, monkeypatch):
    """Uninstall: the in-gateway hooks are this module's to tear down; the
    backend PROCESS is not its to stop (the CLI uninstall leaves it running and
    the proxy refuses a removed app with 403). A respawn owed to the removed
    install is dropped."""
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    await hi.record_loaded_hook_signature("watchtower", app)
    set_current()  # uninstalled
    monkeypatch.setattr(hr, "app_enabled_state", lambda name: False)
    _BACKEND["process"] = _spawned()
    hr._pending_respawn["watchtower"] = (0.0, None)

    await hr.reconcile_once([])

    assert calls == [("disable", "watchtower")]
    assert _BACKEND["lifecycle"] == [] and "watchtower" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_hook_app_disabled_tears_down_hooks_and_leaves_its_backend_alone(_harness):
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    await hi.record_loaded_hook_signature("watchtower", app)
    set_current(_hook_backend_app("watchtower", enabled=False))
    _BACKEND["process"] = _spawned()
    hr._pending_respawn["watchtower"] = (0.0, None)

    await hr.reconcile_once([])

    assert calls == [("disable", "watchtower")]
    assert _BACKEND["lifecycle"] == [] and "watchtower" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_hook_app_that_stops_declaring_hooks_keeps_its_backend(_harness):
    """Still enabled, just hookless now: tear the hooks down, leave the process
    for the plain path to own from the next tick."""
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    await hi.record_loaded_hook_signature("watchtower", app)
    set_current(_plain_backend_app("watchtower"))
    _BACKEND["process"] = _spawned()

    await hr.reconcile_once([])

    assert calls == [("disable", "watchtower")]
    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_unsettled_hook_teardown_leaves_the_backend_running(_harness, monkeypatch):
    """If on_shutdown could not settle (retained startup task), nothing else
    moves either: the old backend stays up for the retry, never orphaned mid-swap."""
    calls, (set_current, set_disable_result, _) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    await hi.record_loaded_hook_signature("watchtower", app)
    set_disable_result({"startup_cleanup": "failed: still running"})
    monkeypatch.setattr(hr, "compute_hook_signature", _async_return(("2.0.0", 0, 999)))
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False

    await hr.reconcile_once([app])

    assert calls == [("disable", "watchtower")]
    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_cli_enable_of_a_hook_app_loads_hooks_but_starts_no_backend(_harness, monkeypatch):
    """Nothing loaded, nothing running: the reconciler loads the hooks (the
    in-gateway state it owns) but does not start a first backend -- that is
    boot's and the dashboard's, which vet policy before they spawn. The
    reconciler only ever restores a generation it took down itself."""
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    order: list[str] = []
    monkeypatch.setattr(hr, "on_app_enable", _recording_enable(order, calls, hi))

    await hr.reconcile_once([app])

    assert order == ["on_startup"] and _BACKEND["lifecycle"] == []
    assert hi.loaded_hook_signature("watchtower") is not None


@pytest.mark.asyncio
async def test_loaded_hook_app_with_a_missing_backend_is_left_alone(_harness, monkeypatch):
    """Hooks current, process gone (crashed, or spawned before the digest
    existed and reaped): not this module's to restart unless it stopped it."""
    calls, (set_current, _, _) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    await hi.record_loaded_hook_signature("watchtower", app)
    monkeypatch.setattr(
        hr, "compute_hook_signature", _async_return(hi.loaded_hook_signature("watchtower"))
    )
    _BACKEND["process"] = None

    await hr.reconcile_once([app])

    assert calls == [] and _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_cli_disable_of_a_hook_app_whose_hooks_never_loaded_leaves_its_backend_alone(
    _harness, monkeypatch
):
    """A hook-declaring app whose hooks did not wire (denied, degraded startup)
    can still own the backend the dashboard enable spawned. A CLI disable is not
    this module's cue to stop it; only a respawn owed to it is dropped."""
    calls, (set_current, _, _) = _harness
    set_current(_hook_backend_app("watchtower", enabled=False))
    _BACKEND["process"] = _spawned()
    hr._pending_respawn["watchtower"] = (0.0, None)
    monkeypatch.setattr(hr, "tracked_backend_names", lambda: ["watchtower"])

    await hr.reconcile_once([])

    assert calls == []
    assert _BACKEND["lifecycle"] == [] and "watchtower" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_denied_hook_app_does_not_get_a_backend_started(_harness):
    """Admission is checked before anything runs: a denied app gets neither
    hooks nor a process from the reconciler."""
    calls, (set_current, _, set_denied) = _harness
    app = _hook_backend_app("watchtower")
    set_current(app)
    set_denied("trust withdrawn")

    await hr.reconcile_once([app])

    assert calls == [("enable", "watchtower")]  # routed through the denied enable path
    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_uninstall_clears_a_pending_respawn_so_a_reinstall_is_a_fresh_install(
    _harness, monkeypatch
):
    """The backoff belongs to the install whose respawn failed. A CLI uninstall +
    reinstall inside the window is a NEW install; the reconciler owes it nothing
    (a first start is boot's / the dashboard's) and must not hold a stale entry."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False
    monkeypatch.setattr(hr, "start_app_backend", lambda name: None)
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"] and "cost-ai" in hr._pending_respawn

    # Uninstalled (confirmed absent): the entry goes with the install.
    set_current()
    monkeypatch.setattr(hr, "app_enabled_state", lambda name: False)
    await hr.reconcile_once([])  # no tracked_backend_names stub: the entry is the only route
    assert "cost-ai" not in hr._pending_respawn


@pytest.mark.asyncio
async def test_a_reinstall_between_ticks_is_not_held_by_the_old_installs_backoff(
    _harness, monkeypatch
):
    """Uninstall + reinstall inside one 15s tick never shows the reconciler an
    absent app, so nothing clears the entry. It is bound to the install identity
    (the .app_secret every install mints afresh): a new identity means the
    backoff does not apply, and the pending replacement is finished at once."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False
    identity = {"v": (1, 100)}
    monkeypatch.setattr(hr, "_install_identity", lambda name: identity["v"])
    real_start = hr.start_app_backend
    monkeypatch.setattr(hr, "start_app_backend", lambda name: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(hr.time, "monotonic", lambda: clock["t"])

    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"] and "cost-ai" in hr._pending_respawn

    # Same install, inside the window: held back.
    monkeypatch.setattr(hr, "start_app_backend", real_start)
    clock["t"] += 1.0
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop"]

    # Reinstalled between ticks (new secret file): attempted at once.
    identity["v"] = (2, 200)
    clock["t"] += 1.0
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop", "start"]


# ---------------------------------------------------------------------------
# Shutdown leaves no unsupervised backend of this gateway's own making
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_registered_after_the_shutdown_sweep_is_stopped(_harness, monkeypatch):
    """A slow respawn can outlive the shutdown drain and register its child after
    on_gateway_shutdown swept the tracked set. The spawn observes that shutdown
    began while it ran and stops the child itself, so no unsupervised
    third-party process this gateway spawned survives the gateway."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False

    def slow_start(name):
        _BACKEND["lifecycle"].append("start")
        _BACKEND["process"] = _spawned()
        hr._stopping = True  # shutdown began while the spawn was in flight
        return _BACKEND["process"]

    monkeypatch.setattr(hr, "start_app_backend", slow_start)

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == ["stop", "start", "stop"]
    assert _BACKEND["process"] is None


@pytest.mark.asyncio
async def test_adopted_backend_registered_during_shutdown_is_left_alone(_harness, monkeypatch):
    """start_app_backend may ADOPT an external instance already on the fixed
    port instead of spawning. An adopted backend's contract is to survive
    gateway exit (the shutdown sweep excludes it), so the shutdown guard here
    must not signal it either."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False

    def adopting_start(name):
        _BACKEND["lifecycle"].append("adopt")
        _BACKEND["process"] = _adopted()
        hr._stopping = True
        return _BACKEND["process"]

    monkeypatch.setattr(hr, "start_app_backend", adopting_start)

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == ["stop", "adopt"]
    assert _BACKEND["process"] is not None


@pytest.mark.asyncio
async def test_respawn_is_refused_once_shutdown_began(_harness):
    """Checked at the chokepoint itself, not only in the callers."""
    hr._stopping = True
    hr._pending_respawn["cost-ai"] = (0.0, None)
    assert hr._respawn_with_backoff("cost-ai") is False
    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_adopted_backend_is_never_replaced(_harness):
    """Replacement is for children THIS gateway spawned. An adopted record has
    no generation the gateway can vouch for and is not its process to cycle."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _adopted()
    _BACKEND["generation"] = False  # even a (fabricated) positive mismatch

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == []


# ---------------------------------------------------------------------------
# A tick landing inside a CLI uninstall + install must not decide the outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tick_between_uninstall_and_install_does_not_change_the_outcome(
    _harness, monkeypatch
):
    """The reported workflow with the worst tick timing: the app is confirmed
    absent between the two CLI steps. The reconciler does nothing to the still-
    running child (a removed app's backend is not its to stop), so the reinstall
    lands with the old process still tracked, its secret rotated, and the
    replacement happens exactly as it would had the tick landed after both
    steps. The outcome does not depend on where the tick lands."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    monkeypatch.setattr(hr, "tracked_backend_names", lambda: ["cost-ai"])

    set_current()  # uninstalled: confirmed absent by the harness read
    await hr.reconcile_once([])
    assert _BACKEND["lifecycle"] == [] and _BACKEND["process"] is not None

    set_current(app)  # installed again: new .app_secret, old child still tracked
    _BACKEND["generation"] = False
    await hr.reconcile_once([app])
    assert _BACKEND["lifecycle"] == ["stop", "start"]


@pytest.mark.asyncio
async def test_a_disable_drops_a_pending_respawn_and_a_re_enable_starts_nothing(
    _harness, monkeypatch
):
    """Disable retires the generation the respawn was owed to; a later re-enable
    is a first start and not this module's to perform."""
    _, (set_current, _, _) = _harness
    set_current(_plain_backend_app("cost-ai", enabled=False))
    hr._pending_respawn["cost-ai"] = (0.0, None)

    await hr.reconcile_once([])
    assert "cost-ai" not in hr._pending_respawn

    set_current(_plain_backend_app("cost-ai"))
    await hr.reconcile_once([_plain_backend_app("cost-ai")])
    assert _BACKEND["lifecycle"] == []


@pytest.mark.asyncio
async def test_a_disable_landing_during_the_respawn_stops_the_new_child(_harness, monkeypatch):
    """``enabled`` was read under the lifecycle lock, but a CLI disable in
    another process can land while the spawn is in flight. Enablement is read
    again after the spawn and a positively disabled app's new child is stopped
    at once, not left serving until the next tick."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False

    def start_then_disable(name):
        _BACKEND["lifecycle"].append("start")
        _BACKEND["process"] = _spawned()
        set_current(_plain_backend_app("cost-ai", enabled=False))  # lands mid-spawn
        return _BACKEND["process"]

    monkeypatch.setattr(hr, "start_app_backend", start_then_disable)

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == ["stop", "start", "stop"]
    assert _BACKEND["process"] is None


@pytest.mark.asyncio
async def test_a_disable_landing_before_the_respawn_skips_it(_harness, monkeypatch):
    """Same race, earlier: the disable landed after the locked read but before
    the spawn. Enablement is re-read immediately before spawning; nothing
    starts, and the debt is dropped by the disable path on the next tick."""
    _, (set_current, _, _) = _harness
    app = _plain_backend_app("cost-ai")
    set_current(app)
    _BACKEND["process"] = _spawned()
    _BACKEND["generation"] = False
    real_stop = hr.stop_app_backend

    def stop_then_disable(name):
        set_current(_plain_backend_app("cost-ai", enabled=False))  # lands after the stop
        return real_stop(name)

    monkeypatch.setattr(hr, "stop_app_backend", stop_then_disable)

    await hr.reconcile_once([app])

    assert _BACKEND["lifecycle"] == ["stop"]


def _async_return(value):
    async def _f(*_a, **_k):
        return value

    return _f


def _recording_disable(order, calls, hooks_mod):
    async def _disable(name, app_info, **kwargs):
        order.append("on_shutdown")
        calls.append(("disable", name))
        hooks_mod.clear_loaded_hook_signature(name)
        return {}

    return _disable


def _recording_enable(order, calls, hooks_mod):
    async def _enable(name, app_info, **kwargs):
        order.append("on_startup")
        calls.append(("enable", name))
        await hooks_mod.record_loaded_hook_signature(name, app_info)

    return _enable


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
    """When a pass is already IN FLIGHT and hung, stop's
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
    from a source that does not match its execution grant lands on the
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
async def test_unreadable_metadata_does_not_read_as_uninstalled(_harness, tmp_path, monkeypatch):
    """``get_app`` returning None does not mean the app is gone.

    ``_read_installed`` leads with ``Path.is_file()``, which answers a silent False
    for a dangling symlink, a directory or fifo in the metadata's place, a symlink
    loop and a non-directory parent -- and folds every OSError and a corrupt JSON
    body into the same None. Acting on that collapsed answer means one broken path
    unloads a HEALTHY app's routes and modules on the next tick, every tick, until
    the fault clears. Absence is confirmed through the tri-state read instead.
    """
    calls, (set_current, _, _) = _harness
    # This test exercises the REAL tri-state read against a broken shape on disk.
    monkeypatch.setattr(hr, "app_enabled_state", real_app_enabled_state)
    # A real broken shape on disk: something occupies the app directory's own path,
    # so app_enabled_state reports unknown rather than a definite False.
    apps = tmp_path / "home" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / "watchtower").write_text("not a directory", encoding="utf-8")

    set_current()  # get_app -> None, which alone would look like an uninstall
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))

    await hr.reconcile_once([])

    assert calls == [], "a healthy app was torn down on an unreadable metadata path"
    assert hi.loaded_hook_signature("watchtower") is not None


@pytest.mark.asyncio
async def test_a_confirmed_absence_still_tears_down(_harness, tmp_path):
    """The control: a genuinely missing record must still be acted on.

    Deferring on unknown must not become deferring on everything, or the teardown
    this reconciler exists to perform would never run.
    """
    calls, (set_current, _, _) = _harness
    # The app directory exists and is a directory; installed.json is simply absent.
    (tmp_path / "home" / "apps" / "watchtower").mkdir(parents=True, exist_ok=True)

    set_current()
    await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))

    await hr.reconcile_once([])

    assert calls == [("disable", "watchtower")]
    assert hi.loaded_hook_signature("watchtower") is None


@pytest.mark.asyncio
async def test_uninstall_drops_the_apps_in_process_hook_registries(_harness):
    """A surviving slot-close hook makes an uninstalled app's tabs undismissable.

    ``forget_app_hooks`` had exactly one caller -- the DASHBOARD uninstall handler --
    so a CLI uninstall reached this reconciler's teardown and left the registries
    behind. The stale hook closes over a store the uninstall deleted, and
    ``notify_slot_closed`` reporting its failure is what ``api_chat_slot_delete``
    turns into a tab the user cannot dismiss for an app that does not exist.
    """

    async def _stale(_key: str) -> None:
        raise RuntimeError("store is gone")

    calls, (set_current, _, _) = _harness
    teardown.register_slot_close_hook("watchtower", _stale)
    try:
        assert await teardown.notify_slot_closed("watchtower", "slot-1") is False

        set_current()  # get_app -> None, i.e. uninstalled
        await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
        await hr.reconcile_once([])

        assert calls == [("disable", "watchtower")]
        # No hook registered is the path that returns True and lets the close through.
        assert await teardown.notify_slot_closed("watchtower", "slot-1") is True
    finally:
        teardown.forget_app_hooks("watchtower")


@pytest.mark.asyncio
async def test_a_plain_disable_keeps_the_hook_registries(_harness):
    """The asymmetry forget_app_hooks documents: only uninstall is terminal.

    These registries are repopulated from the app's own watchdog, not by the
    gateway, so clearing them on a disable would leave a window after a re-enable
    in which a dismissal silently fails to reach a live worker.
    """
    seen: list[str] = []

    async def _live(key: str) -> None:
        seen.append(key)

    calls, (set_current, _, _) = _harness
    teardown.register_slot_close_hook("watchtower", _live)
    try:
        app_off = _app_info("watchtower", enabled=False)
        set_current(app_off)
        await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
        await hr.reconcile_once([app_off])

        assert calls == [("disable", "watchtower")]
        assert await teardown.notify_slot_closed("watchtower", "slot-1") is True
        assert seen == ["slot-1"], "the app's own hook was not consulted"
    finally:
        teardown.forget_app_hooks("watchtower")


@pytest.mark.asyncio
async def test_an_unsettled_uninstall_keeps_them_for_the_retry(_harness):
    """Unsettled means app code is still running, so its off-switch stays.

    The reconciler retries next tick and drops them once teardown settles.
    """

    seen: list[str] = []

    async def _live(key: str) -> None:
        seen.append(key)

    calls, (set_current, set_disable_result, _) = _harness
    teardown.register_slot_close_hook("watchtower", _live)
    try:
        set_current()  # uninstalled
        set_disable_result({"startup_cleanup": "failed: detached startup hook is still running"})
        await hi.record_loaded_hook_signature("watchtower", _app_info("watchtower"))
        await hr.reconcile_once([])

        assert calls == [("disable", "watchtower")]
        # Still registered, so the app's off-switch is still reachable.
        assert await teardown.notify_slot_closed("watchtower", "slot-1") is True
        assert seen == ["slot-1"]
    finally:
        teardown.forget_app_hooks("watchtower")


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
    """A failed on_shutdown means the app's own stop
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
    """A CLI reinstall (disable/enable without a process
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
