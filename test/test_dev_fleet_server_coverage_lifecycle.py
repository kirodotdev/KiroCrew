"""Service and process lifecycle tests for the Dev Fleet standalone backend.

This module pins the live gateway unit, launchd label and pod detection the backend
derives from its config home, the foreground service backend off POSIX, the systemd
drop-in rollback and the worktree path lookup that make-live relies on, the cleanup
hook that kills runs and cancels workers, the app factory's hooks and health routes,
and ``main()``'s loopback bind and fail-closed platform boot. The helper, pod and
rebase cases live in ``test_dev_fleet_server_coverage.py`` and the HTTP layer in
``test_dev_fleet_server_coverage_http_api.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dev_fleet_server_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _pin_module_state,
    _same,
)

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import (
    gateway_service,
    http_api,
    live,
    repository,
    runtime,
    worktree_ops,
)


# --------------------------------------------------------------------------
# gateway identity helpers
# --------------------------------------------------------------------------
def test_gateway_unit_name_defaults_to_live_unit(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert live._gateway_unit_name() == live._LIVE_GATEWAY_UNIT


def test_gateway_unit_name_uses_pod_instance(monkeypatch, tmp_path):
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    assert live._gateway_unit_name() == "kirocrew-pod@feat.service"


def test_gateway_unit_name_falls_back_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert live._gateway_unit_name() == live._LIVE_GATEWAY_UNIT


def test_gateway_label_defaults_to_live_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert live._gateway_label() == live._LIVE_GATEWAY_LABEL


def test_gateway_label_uses_pod_agent(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    pod_config = pytest.importorskip("kiro_crew.pod.config")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.delenv("KIROCREW_POD_UNIT_PREFIX", raising=False)
    expected = f"{launchd.LABEL_PREFIX}.{pod_config.DEFAULT_UNIT_PREFIX}.feat"
    assert live._gateway_label() == expected


def test_gateway_label_honours_unit_prefix_override(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "altplane")
    assert live._gateway_label() == f"{launchd.LABEL_PREFIX}.altplane.feat"


@pytest.mark.parametrize(
    ("home_parts", "expected"),
    [((".kirocrew-pods", "feat"), True), (("home", "kirocrew"), False)],
)
def test_in_pod_detection(monkeypatch, tmp_path, home_parts, expected):
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: tmp_path.joinpath(*home_parts)
    )
    assert live._in_pod() is expected


def test_in_pod_is_none_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert live._in_pod() is None


def test_foreground_backend_is_none_off_posix(monkeypatch):
    monkeypatch.setattr(live.sys, "platform", "win32")
    assert live._foreground_backend() is None


# --------------------------------------------------------------------------
# drop-in rollback + path selector
# --------------------------------------------------------------------------
def _systemd_backend(dropin: Path) -> gateway_service.SystemdBackend:
    """A SystemdBackend whose drop-in path is *dropin*.

    ``rollback`` touches only the injected drop-in path and the module-level
    ``atomic_write_text``, so the remaining seams stay inert stubs.
    """
    return gateway_service.SystemdBackend(
        AsyncMock(return_value=(0, "", "")),
        lambda: "kirocrew.service",
        platform="linux",
        which=lambda _name: "/usr/bin/systemctl",
        dropin_path=lambda: dropin,
        dropin_content=lambda _wt, _kcbin: "",
    )


def test_service_rollback_deletes_the_dropin_when_there_was_none(tmp_path):
    """No prior drop-in -> the file staged over it is removed, not left behind."""
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("[Service]\n", encoding="utf-8", newline="\n")
    assert _systemd_backend(dropin).rollback(None) is True
    assert not dropin.exists()


def test_service_rollback_rewrites_prior_content(tmp_path):
    """A prior drop-in -> its exact content is restored over the staged one."""
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("new\n", encoding="utf-8", newline="\n")
    assert _systemd_backend(dropin).rollback("prior\n") is True
    assert dropin.read_text(encoding="utf-8") == "prior\n"


def test_service_rollback_reports_failure(monkeypatch, tmp_path):
    """A failed restore returns False so the caller can report
    ``rolled_back: false`` rather than claiming a rollback that did not land."""

    def _boom(path, content):
        raise OSError("read-only fs")

    monkeypatch.setattr(gateway_service, "atomic_write_text", _boom)
    backend = _systemd_backend(tmp_path / "make-live.conf")
    assert backend.rollback("prior") is False


@pytest.mark.asyncio
async def test_find_worktree_by_path_requires_path():
    wt, err = await repository._find_worktree_by_path("")
    assert wt is None
    assert err is not None and "non-empty string" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_unknown_path(monkeypatch, tmp_path):
    monkeypatch.setattr(repository, "_discover_worktrees", AsyncMock(return_value=[]))
    wt, err = await repository._find_worktree_by_path(str(tmp_path / "nope"))
    assert wt is None
    assert err is not None and "not a known worktree" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_matches_known_worktree(monkeypatch, tmp_path):
    wanted = tmp_path / "kirocrew-wt-alpha"
    wanted.mkdir()
    monkeypatch.setattr(
        repository,
        "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(tmp_path / "other")}, {"path": str(wanted)}]),
    )
    wt, err = await repository._find_worktree_by_path(str(wanted))
    assert err is None
    assert wt is not None and _same(wt["path"], str(wanted))


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_kills_runs_and_cancels_workers(monkeypatch):
    async def _never():
        # flake-ok: pre-existing placeholder coroutine that never finishes on its own; the cleanup cancels it, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    refresher = asyncio.create_task(_never())
    # flake-ok: pre-existing one-turn yield so the tasks start before the cleanup, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid, moved verbatim; the process double is never signalled
    proc.pid = 4242
    kill_tree = AsyncMock()
    monkeypatch.setattr(runtime, "_kill_tree", kill_tree)
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", refresher)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())

    kill_tree.assert_awaited_once_with(4242)
    proc.kill.assert_called_once()
    assert runtime._ACTIVE_RUNS == {}
    assert run_task.cancelled() or run_task.done()
    assert refresher.cancelled() or refresher.done()
    assert worktree_ops._refresher_task is None


@pytest.mark.asyncio
async def test_cleanup_tolerates_already_dead_process(monkeypatch):
    async def _never():
        # flake-ok: pre-existing placeholder coroutine that never finishes on its own; the cleanup cancels it, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing sleep, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    # flake-ok: pre-existing one-turn yield so the task starts before the cleanup, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid, moved verbatim; the process double is never signalled
    proc.pid = 77
    proc.kill.side_effect = ProcessLookupError
    monkeypatch.setattr(runtime, "_kill_tree", AsyncMock())
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", None)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    assert runtime._ACTIVE_RUNS == {}


@pytest.mark.asyncio
async def test_cleanup_skips_finished_process(monkeypatch):
    async def _done():
        return None

    run_task = asyncio.create_task(_done())
    await run_task

    proc = MagicMock()
    proc.returncode = 0
    kill_tree = AsyncMock()
    monkeypatch.setattr(runtime, "_kill_tree", kill_tree)
    monkeypatch.setattr(runtime, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(worktree_ops, "_refresher_task", None)
    monkeypatch.setattr(worktree_ops, "_warm_task", None)
    monkeypatch.setattr(worktree_ops, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    kill_tree.assert_not_awaited()
    proc.kill.assert_not_called()


# --------------------------------------------------------------------------
# app factory + entry point
# --------------------------------------------------------------------------
def test_create_app_registers_lifecycle_hooks_by_name():
    app = mod.create_app()
    startup_names = [getattr(h, "__name__", "") for h in app.on_startup]
    cleanup_names = [getattr(h, "__name__", "") for h in app.on_cleanup]
    assert "dev_fleet_startup" in startup_names
    assert "dev_fleet_cleanup" in cleanup_names


def test_create_app_exposes_health_on_both_paths():
    app = mod.create_app()
    paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
    assert {"/health", "/api/health"} <= paths
    # The cutover moved to the gateway process (gateway_routes.py).
    assert "/api/make-live" not in paths


def test_main_runs_the_app_on_loopback(monkeypatch):
    seen: dict = {}

    def fake_run_app(app, host=None, port=None, print=None):
        seen.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(mod.web, "run_app", fake_run_app)
    assert mod.main() == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == http_api.PORT


def test_main_boots_platform_before_serving(monkeypatch):
    """main() must install the platform context BEFORE create_app/run_app.

    The app backend launches this module as its own subprocess, which inherits
    no installed platform context. If it served without booting, the first read
    of current_context() (e.g. the sandbox floor resolved while wrapping the
    app's own git worktree scan) would raise on a non-standalone edition and the
    error would surface verbatim in the UI. The ORDER is the invariant: boot
    resolves the profile and installs the context, so create_app never runs
    against a cold context.
    """
    calls: list[str] = []

    def _fake_boot(_cfg):
        calls.append("boot")
        return SimpleNamespace()

    monkeypatch.setattr(mod, "boot_platform", _fake_boot)
    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace()))
    monkeypatch.setattr(mod, "create_app", lambda: calls.append("create_app") or MagicMock())
    monkeypatch.setattr(mod.web, "run_app", lambda *a, **k: calls.append("run_app"))

    assert mod.main() == 0
    assert calls == ["boot", "create_app", "run_app"]


def test_main_fails_closed_when_platform_cannot_compose(monkeypatch):
    """A composition failure must ABORT main(), never serve a cold backend.

    Mirrors the CLI entry point's fail-closed posture: a non-standalone profile
    whose companion cannot compose must not fall through to serving the backend
    with no security overlay.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    def _boom(_cfg):
        raise PlatformCompositionError("companion missing")

    served: list[str] = []
    monkeypatch.setattr(mod, "boot_platform", _boom)
    monkeypatch.setattr(mod.KiroCrewConfig, "load", classmethod(lambda cls: SimpleNamespace()))
    monkeypatch.setattr(mod, "create_app", lambda: served.append("create_app") or MagicMock())
    monkeypatch.setattr(mod.web, "run_app", lambda *a, **k: served.append("run_app"))

    with pytest.raises(PlatformCompositionError):
        mod.main()
    assert served == []
