"""The rootdir conftest's isolation floor guards itself.

``test_host_service_guard.py`` covers the SERVICE half of that floor. This file covers
the three halves added around it, each of which protects a different piece of the
operator's machine from a test that forgot to isolate itself:

* the **data home** -- ``KIROCREW_HOME`` pinned per test, plus the ``~/.kiro`` paths
  production binds at IMPORT time, which the env var cannot reach;
* the **workspace root** -- ``KIROCREW_WORKSPACE`` pinned per test, a SEPARATE
  default from the data home that otherwise resolves to the operator's real
  ``~/workplace``;
* the **system temp directory** -- ``tempfile``'s base redirected per run, with residue
  reported rather than silently accumulated;
* the **worker budget** -- how many xdist workers the host can actually back.

Two jobs, the same split ``test_host_service_guard.py`` uses:

* **Behaviour** -- prove each guard is armed, catches what it claims, and stays silent
  on what it must not touch. A guard nobody exercises is a guard that stops working at
  the next refactor without anybody noticing.
* **Ratchet** -- pin the guarded set against what ``src/kiro_crew`` actually contains,
  so a NEW import-time ``Path.home()`` binding cannot land unpinned. Same shape as
  ``test_host_service_guard.py``'s ratchet and ``test_spawn_preexec_guard.py``'s
  ``_ALLOWED``.

This file keeps the guard itself, the data home, platform profile, workspace, pod plane,
agent-spec home, sessions dir and shared ``~/.kiro`` path pins with their ratchet, the
telemetry and consent-worker guards, the temp base and residue report, the working
directory and the test loop. The between-test restores (credential and shell environment,
logging state, CLI startup environment, hooks dispatcher) are in
``test_host_isolation_floor_restores.py`` and the worker budget in
``test_host_isolation_floor_worker_budget.py``; the pieces they share with this file live
in ``host_isolation_floor_helpers.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

import pytest
from git_maintenance_helpers import auto_maintenance_starts, trace_git_spawns
from host_isolation_floor_helpers import (
    _REPO_ROOT,
    _ROOT_CONFTEST,
    _root,
)

from kiro_crew import platform_compat

_SRC = _REPO_ROOT / "src" / "kiro_crew"


#: The real directories the isolation floor exists to keep tests out of.
#:
#: Deliberately these specific roots and NOT ``Path.home()``. On POSIX "not under
#: $HOME" reads as the stronger assertion, but on Windows ``tempfile.gettempdir()`` is
#: ``%LOCALAPPDATA%\\Temp`` -- i.e. ``C:\\Users\\<user>\\AppData\\Local\\Temp`` -- which is
#: itself under the home directory. So "not under $HOME" is unconditionally FALSE there
#: for every correctly-isolated tmp path, and the assertion could never pass on the
#: Windows shards. These roots are what the fixtures actually protect, and the narrower
#: form is true on all four targets.
_GUARDED_ROOTS: tuple[pathlib.Path, ...] = (
    pathlib.Path.home() / ".kiro",
    pathlib.Path.home() / ".kirocrew",
    pathlib.Path.home() / ".claude.json",
    pathlib.Path.home() / "workplace",
)


#: pytest's own basetemp for this run, recorded by :func:`_record_own_basetemp`.
#:
#: Whatever the operator's ``TMPDIR`` points at, a path under our basetemp is
#: test-owned by construction -- the floor's fixtures created it. That matters because
#: a Kiro Crew agent session sets ``TMPDIR`` to its scratch dir under
#: ``~/.kiro/crew/scratch/``, so every correctly-pinned home then resolves INSIDE the
#: guarded ``~/.kiro`` tree while touching nothing of the operator's. The guard asks
#: whether a pin ESCAPED to the real tree, and a path under our own basetemp did not.
_OWN_BASETEMP: pathlib.Path | None = None


@pytest.fixture(autouse=True, scope="module")
def _record_own_basetemp(tmp_path_factory):
    global _OWN_BASETEMP
    _OWN_BASETEMP = tmp_path_factory.getbasetemp().resolve()


def _inside_a_guarded_root(path: pathlib.Path) -> bool:
    """Whether *path* is, or is under, one of the operator's real guarded paths.

    A path under this run's own basetemp is never "the operator's", even when that
    basetemp itself sits under a guarded root (see :data:`_OWN_BASETEMP`).
    """
    resolved = path.resolve()
    if _OWN_BASETEMP is not None and (
        resolved == _OWN_BASETEMP or resolved.is_relative_to(_OWN_BASETEMP)
    ):
        return False
    for root in _GUARDED_ROOTS:
        candidate = root.resolve()
        if resolved == candidate or resolved.is_relative_to(candidate):
            return True
    return False


class TestTheGuardItself:
    """The guard's two edges, pinned so neither can drift silently."""

    def test_the_real_data_home_is_still_guarded(self) -> None:
        assert _inside_a_guarded_root(pathlib.Path.home() / ".kiro" / "crew")

    def test_our_own_basetemp_is_test_owned_even_under_a_guarded_root(self, monkeypatch) -> None:
        """A run whose TMPDIR is a Kiro Crew session scratch dir puts basetemp under
        ``~/.kiro``; a pin there must read as isolated, not as an escape."""
        scratch = (
            pathlib.Path.home() / ".kiro" / "crew" / "scratch" / "runtime-x" / "pytest-0"
        ).resolve()
        monkeypatch.setattr(sys.modules[__name__], "_OWN_BASETEMP", scratch)
        assert not _inside_a_guarded_root(scratch / "i0" / "1-kirocrew-home")
        assert _inside_a_guarded_root(pathlib.Path.home() / ".kiro" / "crew")


# ── the data home ─────────────────────────────────────────────────────────


class TestTheDataHomeIsPinnedForEveryTestpath:
    """``KIROCREW_HOME`` must be a tmp dir here, and it must be the SAME one the
    package resolves.

    These assertions run against the LIVE fixtures rather than a reconstruction,
    because the thing worth pinning is that the autouse chain actually fired. The
    stakes are specific: ``config_dir()`` is not a read -- it CREATES the home and its
    marker on first use and can run the one-time ``~/.kirocrew`` -> ``~/.kiro/crew``
    migration as a side effect. A test that resolves it unpinned mutates the
    operator's live install.
    """

    def test_kirocrew_home_is_not_the_operators_real_home(self) -> None:
        home = pathlib.Path(os.environ["KIROCREW_HOME"]).resolve()

        assert not _inside_a_guarded_root(home), f"KIROCREW_HOME is a real home path: {home}"

    def test_config_dir_resolves_to_that_same_pinned_home(self) -> None:
        """The env var is only worth pinning if the package actually follows it.

        ``config_dir()`` memoises its answer in a module global for the process
        lifetime, so this also proves the per-test reset of ``_resolved_home`` works --
        without it a home cached by an earlier test on this xdist worker would win.
        """
        from kiro_crew.config.paths import config_dir

        assert config_dir().resolve() == pathlib.Path(os.environ["KIROCREW_HOME"]).resolve()

    def test_a_test_can_still_override_the_home_itself(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage: a test that isolates itself wins."""
        from kiro_crew.config.paths import config_dir

        mine = tmp_path / "my-own-home"
        mine.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(mine))
        monkeypatch.setattr("kiro_crew.config.paths._resolved_home", None)

        assert config_dir().resolve() == mine.resolve()

    def test_a_tests_own_undo_cannot_lift_the_floor(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``monkeypatch.undo()`` in a test body unwinds the TEST's patches only.

        The floor's pins ride on their own ``MonkeyPatch`` (``conftest._floor_monkeypatch``),
        not on the ``monkeypatch`` fixture the test holds. Before that split, the third
        full-run audit caught ``test_browser_cli_install.py`` calling ``undo()`` to lift
        an ``os.name`` patch and then running ``detect()`` against the operator's real
        ``~/.kiro/crew``: ``undo()`` had lifted ``KIROCREW_HOME`` with it. Every pin
        named here is one the audit saw a real host write through.
        """
        pinned = {
            key: os.environ[key]
            for key in (
                "KIROCREW_HOME",
                "KIROCREW_WORKSPACE",
                "KIROCREW_PROFILE",
                "KIROCREW_TELEMETRY",
            )
        }
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "mine"))
        monkeypatch.setattr(os, "name", os.name)  # the shape the real call site undid

        monkeypatch.undo()

        for key, value in pinned.items():
            assert os.environ.get(key) == value, f"{key} was unpinned by the test's own undo()"
        assert not _inside_a_guarded_root(pathlib.Path(os.environ["KIROCREW_HOME"]).resolve())

    def test_the_floor_is_set_up_before_and_torn_down_after_the_tests_monkeypatch(
        self, request: pytest.FixtureRequest
    ) -> None:
        """The two undo stacks nest, so a same-key override restores the PIN, not the host.

        Pinned structurally rather than by observing one teardown: the ``monkeypatch``
        fixture is re-declared at the rootdir with ``_floor_monkeypatch`` as a dependency,
        which is what orders its setup after, and its teardown before, the floor's.
        """
        definition = request._fixturemanager.getfixturedefs("monkeypatch", request.node)
        assert definition, "no monkeypatch fixture resolved"
        active = definition[-1]
        assert "_floor_monkeypatch" in active.argnames, (
            "the monkeypatch fixture in force here is not the rootdir override; a test's "
            "undo would race the floor's teardown"
        )
        assert pathlib.Path(active.func.__code__.co_filename).resolve() == _ROOT_CONFTEST.resolve()


class TestThePlatformProfileIsPinnedForEveryTestpath:
    """``KIROCREW_PROFILE`` is ``standalone`` for every test, whatever the shell exported.

    A Kiro Crew agent session exports the enterprise ``KIROCREW_PROFILE`` to every child it
    spawns, and a dev box with an SSO marker resolves the same profile on its own. With
    no companion installed that profile FAILS CLOSED: ``current_context()`` raises
    ``PlatformCompositionError``, every governance-gated route answers 500, every gated
    notification is dropped. A pin in ``test/conftest.py`` reaches ``test/`` alone:
    ``test/`` stays green while 150+ tests under ``src/kiro_crew/apps/builtins/*/tests/``
    are red on exactly that box. The pin is a rootdir floor, like the data home.
    """

    def test_the_profile_is_standalone(self) -> None:
        assert os.environ.get("KIROCREW_PROFILE") == "standalone"

    def test_the_pin_is_an_autouse_fixture_of_the_rootdir_conftest(self) -> None:
        definition = getattr(_root, "_reset_platform_context")
        marker = getattr(definition, "_fixture_function_marker", None) or getattr(
            definition, "_pytestfixturefunction", None
        )
        assert marker is not None and marker.autouse

    def test_the_context_composes_the_open_source_default(self) -> None:
        """The observable consequence: no fail-closed refusal on an unbooted process."""
        from kiro_crew.platform.context import current_context

        assert current_context() is not None


class TestNoMetricIsEmittedAtImport:
    """Collecting a test module must not build the process-global metrics recorder.

    An import-time emission (``ToolHookResult.allow()`` as a DEFAULT ARGUMENT of a
    module-level helper was the real case) runs ``get_recorder()`` before any pin
    exists. The recorder's first build read the operator's real config, and with
    ``telemetry.enabled`` there it started an exporter bound to the real
    ``~/.kiro/crew/metrics`` that wrote every minute for the life of the worker --
    invisible to the per-test leak guard (the thread predates every test) and to the
    per-test env pin (already built). The rootdir conftest now pins
    ``KIROCREW_TELEMETRY=0`` from process start AND records every module whose
    collection flipped ``metrics.provider._ever_built``; this asserts that record.
    """

    @staticmethod
    def _live_root_conftest(config: pytest.Config):
        """The rootdir conftest AS PYTEST LOADED IT, found by path, not by name.

        ``_root`` above is a second copy loaded for its definitions; the record this
        test reads is runtime state, so it has to come from the plugin instance that
        actually ran the collection hooks.
        """
        for plugin in config.pluginmanager.get_plugins():
            file = getattr(plugin, "__file__", None)
            if file and pathlib.Path(file).resolve() == _ROOT_CONFTEST.resolve():
                return plugin
        raise AssertionError("the rootdir conftest is not a registered plugin")

    def test_the_process_starts_with_telemetry_pinned_off(self) -> None:
        assert os.environ.get("KIROCREW_TELEMETRY") == "0"

    def test_no_module_built_the_recorder_while_being_collected(self, request) -> None:
        live = self._live_root_conftest(request.config)
        emitters = list(live.IMPORT_TIME_METRIC_EMITTERS)
        assert emitters == [], (
            "importing these test modules emitted a metric (a module-level default "
            "argument or constant that goes through kiro_crew.metrics): "
            f"{emitters}. Build the value inside the test or fixture instead; an "
            "import-time build binds the exporter to the operator's real data home."
        )

    def test_the_guard_names_a_module_that_emits_at_import(self, request, monkeypatch) -> None:
        """Negative control, driven on the standalone copy so the live record stays clean.

        The hookwrapper is a plain generator: enter it, emit a metric where the module
        import would have, leave it, and the module is recorded and the build undone.
        """
        from kiro_crew.hooks import ToolHookResult
        from kiro_crew.metrics import provider

        provider.reset_for_testing()
        monkeypatch.setattr(_root, "IMPORT_TIME_METRIC_EMITTERS", [])
        module = request.node.getparent(pytest.Module)
        assert module is not None

        wrapper = _root.pytest_make_collect_report(module)
        next(wrapper)
        ToolHookResult.allow()  # the import-time emission, with the process pin in force
        assert provider._ever_built, "the emission did not build a recorder; control is vacuous"
        report = pytest.CollectReport(module.nodeid, "passed", None, [])
        from pluggy import Result

        with pytest.raises(StopIteration):
            wrapper.send(Result.from_call(lambda: report))
        assert report.failed

        assert _root.IMPORT_TIME_METRIC_EMITTERS == [module.nodeid]
        assert not provider._ever_built, "the guard must undo the build it recorded"
        assert not any("Otel" in t.name for t in threading.enumerate())

    def test_a_build_that_predates_the_module_is_blamed_on_the_conftest_import(
        self, request, monkeypatch
    ) -> None:
        from kiro_crew.hooks import ToolHookResult
        from kiro_crew.metrics import provider

        provider.reset_for_testing()
        monkeypatch.setattr(_root, "IMPORT_TIME_METRIC_EMITTERS", [])
        module = request.node.getparent(pytest.Module)
        ToolHookResult.allow()  # already built when the module is reached

        wrapper = _root.pytest_make_collect_report(module)
        next(wrapper)
        report = pytest.CollectReport(module.nodeid, "passed", None, [])
        from pluggy import Result

        with pytest.raises(StopIteration):
            wrapper.send(Result.from_call(lambda: report))
        assert report.failed

        assert _root.IMPORT_TIME_METRIC_EMITTERS == [f"conftest import (before {module.nodeid})"]
        assert not provider._ever_built


class TestNoConsentWorkerOutlivesItsTest:
    """The metrics provider's consent-recheck thread is joined before a test's pins lift.

    ``get_recorder()`` hands a consent re-read to a daemon thread once per recheck
    window; that thread runs ``KiroCrewConfig.load()``, which reaches ``config_dir()``
    and ``mkdir``s whatever ``KIROCREW_HOME`` names AT THAT MOMENT. Nothing resets the
    recorder between tests, so the read can land under the next test's pin and create
    that test's data home from a thread it never started. On Windows that is visible:
    ``os.path.realpath`` keeps its ``\\\\?\\`` prefix when the directory above a
    not-yet-existing path appears between its two lookups, which is how
    ``test_pathres_loop_starvation.py`` saw ``\\\\?\\...\\<n>-kirocrew-home\\diag`` in one
    build of the sensitive-target set and not the other.

    The rootdir conftest's ``tryfirst`` teardown hook joins the worker, bounded, before
    any fixture (the home pin included) unwinds. These tests drive that join on the
    standalone copy with a scripted config read, so the live module state stays clean.
    """

    @staticmethod
    def _schedule_worker(monkeypatch, load):
        """Start a real consent worker whose ``KiroCrewConfig.load`` is *load*."""
        from kiro_crew.metrics import provider

        scripted = load

        class _Config:
            load = staticmethod(scripted)

        monkeypatch.setattr(provider, "KiroCrewConfig", _Config)
        with provider._lock:
            assert not provider._check_in_flight, "a worker is already in flight; test order?"
            provider._schedule_consent_check_locked()
        workers = _root._live_telemetry_consent_workers()
        assert len(workers) == 1, workers
        return provider, workers[0]

    def test_a_worker_still_reading_config_is_joined_under_this_tests_pins(
        self, monkeypatch
    ) -> None:
        reading = threading.Event()
        release = threading.Event()
        home_seen: list[str | None] = []

        def held_load():
            reading.set()
            # Held on an event, not a sleep: the worker is in flight for exactly as
            # long as this test says, however the scheduler treats either thread.
            assert release.wait(10.0), "the test never released the scripted read"
            home_seen.append(os.environ.get("KIROCREW_HOME"))
            raise RuntimeError("scripted: no config to read")

        provider, worker = self._schedule_worker(monkeypatch, held_load)
        try:
            assert reading.wait(5.0), "the worker never started its read"
            assert worker.is_alive(), "control: the read must still be in flight"
            # The join runs on a side thread so this one can observe it WAITING: the
            # worker cannot exit before the read is released, so the join cannot
            # return before it either. No clock is consulted on either side.
            outcome: list[object] = []
            joiner = threading.Thread(
                target=lambda: outcome.append(_root._join_telemetry_consent_worker()),
                name="test-consent-joiner",
            )
            joiner.start()
            assert joiner.is_alive(), "the join returned while the read was still held"
            release.set()
            joiner.join(10.0)
            assert not joiner.is_alive(), "the join did not return once the read finished"
            assert outcome == [None]
            assert not worker.is_alive()
            # The read finished while THIS test's pin was still in force -- the whole
            # point of joining before any fixture unwinds.
            assert home_seen == [os.environ["KIROCREW_HOME"]]
            assert not provider._check_in_flight
        finally:
            release.set()
            worker.join(5.0)

    def test_a_worker_past_the_bound_is_reported_not_waited_for_forever(self, monkeypatch) -> None:
        release = threading.Event()

        def stuck_load():
            release.wait(10.0)
            raise RuntimeError("scripted: released")

        monkeypatch.setattr(_root, "_EXECUTOR_JOIN_SECS", 0.2)
        provider, worker = self._schedule_worker(monkeypatch, stuck_load)
        try:
            # The read is never released before the join returns, so a return at all
            # proves the bound was honoured; the worker being alive afterwards proves
            # the join did not wait for it. No wall-clock measurement is needed.
            failure = _root._join_telemetry_consent_worker()
            assert isinstance(failure, AssertionError)
            assert _root._TELEMETRY_CONSENT_THREAD in str(failure)
            assert "KIROCREW_HOME" in str(failure)
            assert worker.is_alive(), "control: the worker was still stuck when reported"
        finally:
            release.set()
            worker.join(5.0)
        assert not worker.is_alive()

    def test_with_no_worker_the_join_is_a_no_op(self) -> None:
        assert _root._live_telemetry_consent_workers() == []
        assert _root._join_telemetry_consent_worker() is None

    def test_the_teardown_hook_joins_before_it_yields(self) -> None:
        """The join must run on the pre-``yield`` side, where the pins still hold."""
        source = inspect.getsource(_root.pytest_runtest_teardown)
        statements = [line.strip() for line in source.splitlines()]
        assert statements.count("yield") == 1, "the hook is no longer a single-yield hookwrapper"
        before = statements[: statements.index("yield")]
        after = statements[statements.index("yield") + 1 :]
        assert "wedged_consent_worker = _join_telemetry_consent_worker()" in before
        assert "raise wedged_consent_worker" in after


class TestTheWorkspaceRootIsPinnedForEveryTestpath:
    """``KIROCREW_WORKSPACE`` must be a tmp dir here, independent of the data home.

    ``config.loader.workspace_root()`` does not derive from ``config_dir()`` -- with
    no override and no saved ``<config_dir>/workspace_dir`` file it falls back to the
    platform's ``_default_workspace_base()`` plus ``kirocrew-workspace``, which on
    Linux/macOS is the operator's real ``~/workplace``. Pinning ``KIROCREW_HOME``
    alone therefore leaves this resolver unpinned, and any test that reaches an agent
    working directory ``mkdir``s and writes into that real tree.
    """

    def test_kirocrew_workspace_is_not_the_operators_real_workplace(self) -> None:
        workspace = pathlib.Path(os.environ["KIROCREW_WORKSPACE"]).resolve()

        assert not _inside_a_guarded_root(
            workspace
        ), f"KIROCREW_WORKSPACE is a real home path: {workspace}"

    def test_workspace_root_resolves_to_that_same_pinned_dir(self) -> None:
        """The env var is only worth pinning if the package actually follows it.

        Unlike ``config_dir()``, ``workspace_root()`` caches nothing across calls, so
        this only needs to prove the env var reaches the resolver -- there is no
        module-global memo to reset alongside it.
        """
        from kiro_crew.config.loader import workspace_root

        assert (
            workspace_root().resolve() == pathlib.Path(os.environ["KIROCREW_WORKSPACE"]).resolve()
        )

    def test_a_test_can_still_override_the_workspace_itself(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage: a test that isolates itself wins."""
        from kiro_crew.config.loader import workspace_root

        mine = tmp_path / "my-own-workspace"
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(mine))

        assert workspace_root().resolve() == mine.resolve()


class TestThePodPlaneIsPinnedForEveryTestpath:
    """``KIROCREW_POD_ROOT`` / ``KIROCREW_POD_ENV_DIR`` must be tmp dirs here.

    A fourth axis, and one the data home cannot reach BY DESIGN:
    ``pod.config.PodConfig.load()`` derives ``pods_dir`` from ``_default_home()`` --
    the operator's real ``~/.kiro/crew/pods`` -- so that a pod process running with
    its own isolated ``KIROCREW_HOME`` cannot redirect the host's pod registry into
    a throwaway home, and ``pod_root`` from ``Path.home()/.kirocrew-pods``. A test
    that boots a pod or records a refusal through an unpinned ``PodConfig.load()``
    therefore writes ``<name>.env`` / ``<name>.refused`` into the developer's real
    pod plane; five full-suite runs left ``viability-*.refused`` notes there.
    """

    def test_the_pod_env_dir_is_not_the_operators_real_home(self) -> None:
        pods_dir = pathlib.Path(os.environ["KIROCREW_POD_ENV_DIR"]).resolve()

        assert not _inside_a_guarded_root(
            pods_dir
        ), f"KIROCREW_POD_ENV_DIR is a real home path: {pods_dir}"

    def test_the_pod_root_is_not_the_operators_real_home(self) -> None:
        pod_root = pathlib.Path(os.environ["KIROCREW_POD_ROOT"]).resolve()

        assert not _inside_a_guarded_root(
            pod_root
        ), f"KIROCREW_POD_ROOT is a real home path: {pod_root}"

    def test_pod_config_resolves_to_those_same_pinned_dirs(self) -> None:
        """``load()`` reads the environment fresh, so the pin only has to reach it."""
        from kiro_crew.pod.config import PodConfig

        cfg = PodConfig.load()

        assert cfg.pods_dir.resolve() == pathlib.Path(os.environ["KIROCREW_POD_ENV_DIR"]).resolve()
        assert cfg.pod_root.resolve() == pathlib.Path(os.environ["KIROCREW_POD_ROOT"]).resolve()
        assert not _inside_a_guarded_root(cfg.refusal_file("probe").parent)

    def test_a_test_can_still_override_the_pod_plane_itself(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage: a test that isolates itself wins."""
        from kiro_crew.pod.config import PodConfig

        mine = tmp_path / "my-own-pods"
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(mine))

        assert PodConfig.load().pods_dir.resolve() == mine.resolve()


class TestTheFloorSurvivesATestsOwnUndo:
    """``monkeypatch.undo()`` inside a test must not take the floor down with it.

    ``undo()`` reverts every record on the instance it is called on. Around ninety
    tests call it mid-way to drop one of their own patches before a final
    assertion; while the floor patched through the same shared ``monkeypatch``
    fixture, each of those calls also unpinned ``KIROCREW_HOME`` (and every other
    floor pin) for the rest of the test -- ``test_session_storage`` then ran
    ``empty_trash()`` against the operator's real trash. The floor now patches
    through its own ``MonkeyPatch`` instance, undone at its own teardown.
    """

    def test_undo_does_not_drop_the_data_home_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pinned = os.environ["KIROCREW_HOME"]
        monkeypatch.setenv("KIROCREW_FLOOR_PROBE", "1")  # something for undo() to revert

        monkeypatch.undo()

        assert (
            "KIROCREW_FLOOR_PROBE" not in os.environ
        ), "undo() must still revert the test's own records"
        assert os.environ.get("KIROCREW_HOME") == pinned
        assert not _inside_a_guarded_root(pathlib.Path(pinned))

    def test_undo_does_not_drop_the_other_pins_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = {
            k: os.environ.get(k)
            for k in ("KIROCREW_WORKSPACE", "KIROCREW_POD_ROOT", "KIROCREW_POD_ENV_DIR")
        }
        monkeypatch.setenv("KIROCREW_FLOOR_PROBE", "1")

        monkeypatch.undo()

        assert {k: os.environ.get(k) for k in before} == before
        for value in before.values():
            assert value and not _inside_a_guarded_root(pathlib.Path(value))


class TestTheAgentSpecHomeIsPinnedForEveryTestpath:
    """The agent specs decide which MCP servers the operator's real agent has.

    A third axis, and the reason it needs one: ``kiro_agents_dir()`` is a LAZY
    resolver (``kiro_home()`` -> ``$KIRO_HOME`` or ``Path.home()/.kiro``), so the data
    home does not reach it and neither does ``_SHARED_KIRO_PATHS`` -- whose own ratchet
    docstring records lazy resolvers as outside its scope. Before this floor part
    existed, a suite run inside a throwaway clone rewrote the machine-wide
    ``kirocrew.json`` with that clone's venv and a per-test data home in ``env``, and
    every new session on the machine then failed ``internal_auth_mismatch`` once both
    were deleted.
    """

    def test_the_spec_write_target_is_not_the_operators_real_home(self) -> None:
        from kiro_crew import agent

        target = agent.kiro_agents_dir_path().resolve()

        assert not _inside_a_guarded_root(
            target
        ), f"the agent-spec write target is a real home path: {target}"

    def test_every_seam_in_the_tables_is_actually_pinned(self) -> None:
        """A table entry nobody patches is documentation, not isolation."""
        unpinned = []
        for module, attr in _root._AGENT_SPEC_HOOKS:
            mod = sys.modules.get(module)
            if mod is None:
                unpinned.append(f"{module} not imported, so {attr} could not be set")
                continue
            value = getattr(mod, attr, None)
            if value is None or _inside_a_guarded_root(pathlib.Path(value).resolve()):
                unpinned.append(f"{module}.{attr} = {value!r}")

        assert not unpinned, "these agent-spec seams still resolve a real home:\n" + "\n".join(
            f"    {entry}" for entry in unpinned
        )

    def test_the_resolver_itself_is_pinned_so_no_consumer_needs_registering(self) -> None:
        """The single accessor: one override inside the function body covers everyone.

        This is what replaced a per-module table. 16 modules bind ``kiro_agents_dir``
        by name and that copies the function OBJECT, so patching this module's
        attribute never reached them -- but a value the function BODY reads does,
        because a function's globals are always its defining module's. Asserting on a
        module that binds the name by hand is what proves the reach, not the
        definition site.
        """
        from kiro_crew.config import paths
        from kiro_crew.slack import handler

        assert paths._agents_dir_override is not None, "the floor installed no override"
        assert handler.kiro_agents_dir is paths.kiro_agents_dir, (
            "slack.handler no longer binds the resolver by name, so this test has "
            "stopped proving that a bound copy honours the override"
        )
        assert not _inside_a_guarded_root(handler.kiro_agents_dir().resolve())

    def test_the_hook_modules_are_imported_so_the_table_can_reach_them(self) -> None:
        """The session fixture's whole job: patching cannot precede importing.

        Distinct from the assertion above, which would also pass if a module simply
        happened to be imported by collection. This one is what makes the four write
        seams' coverage independent of collection order.
        """
        missing = [
            module for module, _attr in _root._AGENT_SPEC_HOOKS if sys.modules.get(module) is None
        ]

        assert not missing, f"hook modules never imported, so their seams leak: {missing}"

    def test_a_test_that_redirects_the_home_itself_is_followed(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin defers rather than overriding a test's own isolation.

        ~35 tests isolate with ``patch("<module>.Path.home", ...)`` -- global by
        construction, since ``from pathlib import Path`` binds the same class object
        everywhere -- and then read through a module's own resolver. A pin that
        answered a fixed tmp dir regardless would hand those tests an empty directory
        instead of the tree they just built, which is how an earlier revision of this
        floor broke five test groups at once.
        """
        from kiro_crew.config import paths

        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

        assert paths.kiro_agents_dir() == tmp_path / ".kiro" / "agents"

    def test_the_two_resolvers_cannot_drift_apart_on_the_default(
        self, unpinned_agent_spec_home
    ) -> None:
        """With no override installed the two answers must be the SAME default.

        They are compared against each other by the write guard, so a layout change
        landing in only one of them would read a shared target as private and fail
        OPEN on the machine-wide home. Delegation makes that impossible by
        construction; this pins it so a future edit that re-spells the default in both
        places still has to keep them equal.
        """
        from kiro_crew.config import paths

        assert paths.kiro_agents_dir() == paths.ambient_agents_dir()

    def test_one_directory_is_shared_across_the_seams(self) -> None:
        """A spec written through one seam has to be readable through another."""
        from kiro_crew import agent, agent_discovery

        assert agent.KIRO_AGENTS_DIR == agent_discovery._KIRO_AGENTS_DIR

    def test_the_guards_ambient_reference_is_left_resolving_the_real_home(self) -> None:
        """The write guard's AMBIENT reference must stay override-blind.

        ``_decline_shared_agent_home`` asks "is my target the one every instance under
        this environment shares?", so it reads ``ambient_agents_dir()`` -- which
        deliberately does NOT follow ``_agents_dir_override``. Point it at the honouring
        resolver instead and the pin moves both sides of that comparison together: the
        guard reads a privately redirected target as the shared one, then declines from
        a linked git worktree -- green in CI, red on a developer machine, which is the
        setup this repo mandates.
        """
        from kiro_crew.config import paths

        assert _inside_a_guarded_root(paths.ambient_agents_dir().resolve()), (
            "ambient_agents_dir followed the override; the write guard can no longer "
            "tell a redirected target from the shared one"
        )
        assert (
            paths.kiro_agents_dir() != paths.ambient_agents_dir()
        ), "the two resolvers agree, so the guard's comparison proves nothing"

        from kiro_crew import agent

        assert (
            agent._decline_shared_agent_home(audit=False) is None
        ), "the floor's pinned target is being treated as the shared agent home"

    def test_the_ambient_resolver_has_exactly_one_caller(self) -> None:
        """``ambient_agents_dir`` is override-BLIND, so a second caller is a leak.

        Its docstring says "not a general-purpose reader" -- this makes that
        enforceable. Anything that reads it resolves the operator's REAL agents dir
        even under the floor's pin, which is the leak this seam exists to close; the
        write guard is the one caller whose question is genuinely about the
        environment.
        """
        allowed = {
            "kiro_crew/config/paths.py",
            "kiro_crew/agent.py",
            "kiro_crew/agent_parts/shared_home.py",
        }
        callers = set()
        for path in sorted(_SRC.rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            rel = path.relative_to(_REPO_ROOT / "src").as_posix()
            if rel in allowed:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - unreadable source
                continue
            if "ambient_agents_dir" in source:
                callers.add(rel)

        assert not callers, (
            "ambient_agents_dir is override-blind and gained new readers, each of "
            f"which resolves the operator's real agents dir: {sorted(callers)}. Use "
            "kiro_agents_dir() unless the question is genuinely about the environment."
        )

    def test_a_new_agents_dir_hook_is_an_opt_in_none_not_a_frozen_path(self) -> None:
        """The half of the deleted ratchet that did NOT become obsolete.

        Retiring the per-module table killed the bound-NAME half of that ratchet -- a
        bound copy now honours the override by construction. This half survives: a
        module-level ``*KIRO_AGENTS_DIR`` initialized to a RESOLVED path freezes the
        answer at import, so it follows neither the override nor the hook table and
        nothing goes red. ``None`` (the opt-in hook shape) and a plain string literal
        (``security``'s ``.kiro/agents`` matcher) are the two legitimate shapes.
        """
        frozen: dict[str, int] = {}
        for path in sorted(_SRC.rglob("*.py")):
            if "_vendor" in path.parts or "/tests/" in path.as_posix():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover - unreadable source
                continue
            rel = path.relative_to(_REPO_ROOT / "src").as_posix()
            pending: list[ast.stmt] = list(tree.body)
            while pending:
                node = pending.pop(0)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                pending.extend(
                    child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)
                )
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                named = [
                    t
                    for t in targets
                    if isinstance(t, ast.Name) and t.id.endswith("KIRO_AGENTS_DIR")
                ]
                if not named:
                    continue
                if isinstance(node.value, ast.Constant) and (
                    node.value.value is None or isinstance(node.value.value, str)
                ):
                    continue
                for target in named:
                    frozen[f"{rel} {target.id}"] = node.lineno

        assert not frozen, (
            "these module-level agents-dir hooks are initialized to something other "
            "than None or a string literal, so they freeze a path at import and follow "
            "neither the resolver override nor the hook table:\n"
            + "\n".join(f"    {where}:{line}" for where, line in sorted(frozen.items()))
        )

    def test_a_test_can_still_override_the_seams_itself(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage."""
        from kiro_crew import agent

        mine = tmp_path / "my-own-agents"
        monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", mine)

        assert agent.kiro_agents_dir_path() == mine


class TestTheKiroSessionsDirIsPinnedForEveryTestpath:
    """kiro-cli's replay store: ``<kiro home>/sessions/cli``, machine-wide.

    A fourth ``~/.kiro`` axis, alongside the data home, ``_SHARED_KIRO_PATHS`` and the
    agent-spec home: ``kiro_sessions_dir()`` is a LAZY resolver (same shape as
    ``kiro_agents_dir()``), so none of the other three reach it. Before this floor part
    existed, session-teardown tests (``AcpSessionHandle.destroy()`` -> a bare
    ``kiro_sessions_dir()`` call with no override) deleted ``<sid>.json``/``.jsonl`` out
    of the operator's REAL kiro-cli session store.
    """

    def test_the_resolved_dir_is_not_the_operators_real_home(self) -> None:
        from kiro_crew.config import paths

        assert not _inside_a_guarded_root(paths.kiro_sessions_dir().resolve())

    def test_the_resolver_itself_is_pinned_so_no_consumer_needs_registering(self) -> None:
        """Same proof as the agent-spec resolver: a bound-by-name copy still follows.

        ``session_map`` binds ``kiro_sessions_dir`` by name, so this is what proves the
        override reaches a copied function object rather than only the definition site.
        """
        from kiro_crew import session_map
        from kiro_crew.config import paths

        assert paths._sessions_dir_override is not None, "the floor installed no override"
        assert session_map.kiro_sessions_dir is paths.kiro_sessions_dir, (
            "session_map no longer binds the resolver by name, so this test has "
            "stopped proving that a bound copy honours the override"
        )
        assert not _inside_a_guarded_root(session_map.kiro_sessions_dir().resolve())

    def test_the_pin_is_installed_even_when_paths_was_not_imported_before_setup(
        self, tmp_path, monkeypatch, tmp_path_factory, _floor_monkeypatch
    ) -> None:
        """The floor imports the leaf itself; it does not wait for a test to.

        A test that imports its subject inside its own body is a normal shape here, and
        this floor guards a DELETE path. A patch-if-imported fixture would find no module
        at setup and leave the override ``None`` for exactly that test. In a real session
        a sibling fixture usually imports ``paths`` first, which is why this drives the
        fixture BODY directly with the module evicted from ``sys.modules``: the only
        way the override can be present afterwards is if the fixture imported it.
        """
        import kiro_crew.config as config_pkg

        original = sys.modules["kiro_crew.config.paths"]
        # Restore the package attribute the re-import will overwrite, and the
        # sys.modules entry, when monkeypatch unwinds.
        monkeypatch.setattr(config_pkg, "paths", original)
        monkeypatch.delitem(sys.modules, "kiro_crew.config.paths")
        monkeypatch.delitem(sys.modules, "kiro_crew.session_map", raising=False)

        body = _root._isolate_kiro_sessions_dir
        body = getattr(body, "__wrapped__", body)
        body(lambda name: tmp_path / name, _floor_monkeypatch, tmp_path_factory)

        reimported = importlib.import_module("kiro_crew.config.paths")
        assert reimported is not original, "sys.modules eviction did not take"
        assert reimported._sessions_dir_override is not None, (
            "the floor left kiro_sessions_dir() unpinned for a test whose first import "
            "of config.paths happens in its own body"
        )
        assert reimported.kiro_sessions_dir() == tmp_path / "kiro-sessions-cli"

    def test_a_relocated_kiro_home_is_followed_not_pinned(self, tmp_path, monkeypatch) -> None:
        """The pin is a fence around the REAL store, not a redirect of every resolution.

        ~35 tests relocate kiro-cli's home themselves (``KIRO_HOME``, or a patched
        ``Path.home``) and then read the tree they built there; a pin that answered the
        isolation dir regardless would send every one of them to an empty directory.
        """
        from kiro_crew.config import paths

        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "relocated"))
        assert paths.kiro_sessions_dir() == tmp_path / "relocated" / "sessions" / "cli"

    def test_the_real_store_is_fenced_even_through_kiro_home(self, monkeypatch) -> None:
        """Pointing ``KIRO_HOME`` back at the operator's real ``~/.kiro`` is still fenced.

        The guard keys on WHERE the unpinned resolution lands, not on which lever set
        it, so a test cannot reach the real transcript store by spelling the real path
        explicitly.
        """
        from kiro_crew.config import paths

        monkeypatch.setenv("KIRO_HOME", str(_root._REAL_KIRO_HOME_AT_START))
        assert not _inside_a_guarded_root(paths.kiro_sessions_dir().resolve())

    def test_the_session_map_own_hook_is_pinned_too(self) -> None:
        """``session_map._KIRO_SESSIONS_DIR`` is a second, opt-in lever on the same seam.

        Pinned to the SAME directory as the resolver override, not merely a private one,
        so a test that writes through one seam and reads through the other still sees
        one tree.
        """
        from kiro_crew import session_map
        from kiro_crew.config import paths

        assert session_map._KIRO_SESSIONS_DIR is not None
        assert session_map._KIRO_SESSIONS_DIR == paths.kiro_sessions_dir()

    def test_a_test_that_redirects_the_override_itself_is_followed(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor is a safety net, not a cage."""
        from kiro_crew.config import paths

        mine = tmp_path / "my-own-sessions"
        monkeypatch.setattr(paths, "_sessions_dir_override", lambda: mine)

        assert paths.kiro_sessions_dir() == mine


class TestTheSharedKiroPathsArePinned:
    """``~/.kiro`` is kiro-cli's own home -- machine-wide, shared with the real agent.

    A test that writes ``~/.kiro/settings/mcp.json`` edits the MCP servers of the
    operator's LIVE agent, and ``KIROCREW_HOME`` does not help: these paths are bound
    at import time from ``Path.home()``, before any test could set an env var.
    """

    @pytest.mark.parametrize(
        ("module", "attr"),
        [(module, attr) for module, attr, _ in _root._SHARED_KIRO_PATHS],
    )
    def test_each_pinned_path_is_outside_the_real_home(self, module: str, attr: str) -> None:
        importlib.import_module(module)
        value = pathlib.Path(getattr(sys.modules[module], attr))

        assert not _inside_a_guarded_root(value), (
            f"{module}.{attr} still resolves inside the operator's real "
            f"kiro-cli/data home: {value.resolve()}"
        )

    def test_the_mcp_lock_stays_a_sibling_of_the_mcp_json(self) -> None:
        """A derived pair must be redirected together, or it is worse than neither.

        ``_McpFileLockSync.__enter__`` creates ``_GLOBAL_MCP_JSON.parent`` and then
        touches ``_MCP_LOCK_PATH``. Redirect only the json and the code creates a tmp
        directory, then touches a lock in the REAL one whose parent nothing created --
        ``FileNotFoundError`` on any host where ``~/.kiro/settings`` does not already
        exist. Pinning both is not enough on its own: they must land in the SAME
        directory, which is what this asserts.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        assert mcp_mod._MCP_LOCK_PATH.parent == mcp_mod._GLOBAL_MCP_JSON.parent

    def test_the_table_names_a_real_attribute_on_a_real_module(self) -> None:
        """A renamed constant would make its entry a silent no-op.

        The fixture patches with ``raising=False`` so a partial checkout cannot break
        collection, which is right for the fixture and exactly why the assertion has to
        live here instead.
        """
        for module, attr, _relative in _root._SHARED_KIRO_PATHS:
            imported = importlib.import_module(module)
            assert hasattr(imported, attr), f"{module} has no attribute {attr!r}"


class TestTheSharedKiroPathRatchet:
    """A NEW import-time ``Path.home()`` binding must not land unpinned.

    The fixture can only redirect what its table names, so the table is the guarded
    set and this is what stops it from silently falling behind ``src/``. Deliberately
    WIDER than the fixture acts on: an entry has to be either pinned or explicitly
    excluded with a reason, so adding one forces a decision rather than an omission.
    """

    #: Import-time ``Path.home()`` bindings that deliberately need no redirect.
    #: Each entry states why, in the same spirit as
    #: ``test_spawn_preexec_guard.py``'s ``_ALLOWED``.
    #:
    #: Note the two shapes here are excluded for OPPOSITE reasons: the launchd paths
    #: are already redirected somewhere else, while the security anchors must NOT be
    #: redirected at all.
    _EXCLUDED: dict[tuple[str, str], str] = {
        # Already redirected, by the rootdir conftest's own ``_isolate_launchd_paths``
        # fixture. It has to move the whole macOS launchd set together (PLIST_DIR,
        # PLIST_PATH, LOG_DIR, STDOUT_LOG, STDERR_LOG, LIVE_PROGRAM) because both
        # consumers import them by value.
        ("kiro_crew/service/macos.py", "PLIST_DIR"): "covered by _isolate_launchd_paths",
        ("kiro_crew/service/macos.py", "LOG_DIR"): "covered by _isolate_launchd_paths",
        # The kiro-cli/amazon-q sqlite tuples here are not direct
        # ``Path.home()`` bindings; they are PROJECTIONS over the canonical table in
        # ``kiro_crew/identity_stores.py`` (``sqlite_dbs(...)`` resolves the home
        # inside the call), so they do not match this tripwire's import-time
        # shape and need no exclusion. Their anchor rule ("must name the REAL
        # home"; stub the READER, never move the anchor) is carried forward by
        # ``test_identity_stores.py::TestUsageTuplesAnchorTheRealHome``.
        # An ALLOW-LIST root, so the same rule applies from the other direction: the
        # file browser's first permitted root is the operator's real home BY DESIGN,
        # since that is the directory the user is entitled to browse. Redirecting it
        # would make every containment test assert against a root that does not ship.
        # Nothing here writes: the module reads the value to bound path resolution.
        (
            "kiro_crew/apps/builtins/file_explorer/server.py",
            "_HOME",
        ): "security anchor: the browsing allow-list root",
    }

    @staticmethod
    def _home_bindings() -> dict[tuple[str, str], int]:
        """Every module-level assignment whose value calls ``Path.home()``.

        Parsed rather than grepped so a multi-line or parenthesised expression is
        found too, and so a ``Path.home()`` inside a FUNCTION -- which is re-evaluated
        per call and therefore already follows a patched home -- is correctly ignored.

        Deliberately catches only a DIRECT call. A binding derived from another
        (``PLIST_PATH = PLIST_DIR / "x.plist"``) is invisible here, so this is a
        tripwire for the common shape rather than a completeness proof: pinning the
        root of such a chain does not pin the leaves, which is why
        ``_isolate_launchd_paths`` enumerates its whole set by hand.
        """
        found: dict[tuple[str, str], int] = {}
        for path in sorted(_SRC.rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover - unreadable source
                continue
            # Module SCOPE, not just ``tree.body``. The real cut is a FUNCTION or CLASS
            # body, which is re-evaluated per call and so is not a value frozen at
            # import. Note that is NOT the same as "already isolated": the floor pins
            # neither ``Path.home()`` nor ``$HOME``, so a LAZY resolver
            # (``config.paths.kiro_home()`` and its callers) still names the operator's
            # real ``~/.kiro`` and this ratchet does not cover it. What this guards is
            # precisely the import-time shape. Every other nested statement -- a
            # module-level ``try:`` or a platform ``if:``, which is the normal shape for
            # cross-platform code here -- still runs exactly once at import, so the
            # "re-evaluated per call" reason for skipping it does not apply. Excluding
            # the two scope-opening node kinds rather than enumerating control-flow
            # kinds means ``match``, ``with`` and anything a later Python adds are
            # covered without another edit.
            pending: list[ast.stmt] = list(tree.body)
            while pending:
                node = pending.pop(0)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                pending.extend(
                    child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)
                )
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                # A bare ``x: Path`` annotation binds nothing, and walking None raises.
                if node.value is None:
                    continue
                calls_home = any(
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "home"
                    for inner in ast.walk(node.value)
                )
                if not calls_home:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        rel = path.relative_to(_REPO_ROOT / "src").as_posix()
                        found[(rel, target.id)] = node.lineno
        return found

    def test_every_import_time_home_binding_is_pinned_or_excluded(self) -> None:
        pinned = {
            (module.replace(".", "/") + ".py", attr)
            for module, attr, _relative in _root._SHARED_KIRO_PATHS
        }
        unhandled = {
            key: line
            for key, line in self._home_bindings().items()
            if key not in pinned and key not in self._EXCLUDED
        }

        assert not unhandled, (
            "these module-level Path.home() bindings are neither pinned by the rootdir "
            "conftest's _SHARED_KIRO_PATHS nor excluded with a reason in _EXCLUDED:\n"
            + "\n".join(
                f"    {mod}:{line} {attr}" for (mod, attr), line in sorted(unhandled.items())
            )
            + "\nA test that reaches one of these writes the operator's real home. Pin it, "
            "or exclude it and say why."
        )

    def test_the_exclusion_list_has_not_gone_stale(self) -> None:
        """An exclusion for a binding that does not exist hides the next one."""
        bindings = self._home_bindings()
        stale = [key for key in self._EXCLUDED if key not in bindings]

        assert not stale, f"_EXCLUDED names bindings that no longer exist: {stale}"


# ── the system temp directory ─────────────────────────────────────────────


class TestTheTempBaseIsRedirected:
    """``tempfile``'s base must be a per-run directory, not the shared temp root.

    The point is not tidiness. A bare ``mkdtemp()`` whose cleanup is missing or skipped
    leaves its directory in the platform temp root forever, and MEASURED on the
    hosts this was written against, ``/tmp`` is a tmpfs with a hard 1,048,576-INODE cap
    that returns ENOSPC to unrelated processes while 90% of the BYTES are still free.
    """

    @pytest.mark.skipif(
        bool(os.environ.get("KIROCREW_TMP_PER_TEST")),
        reason="per-test diagnostic mode nests the base one level deeper on purpose",
    )
    def test_gettempdir_is_this_runs_own_root(self) -> None:
        base = pathlib.Path(tempfile.gettempdir())

        assert base.name.startswith(_root._TMP_ROOT_PREFIX), (
            f"tempfile base is {base}, not a {_root._TMP_ROOT_PREFIX}* root -- the "
            "redirect did not take effect"
        )
        assert base.is_dir()

    def test_pytests_own_basetemp_is_not_inside_the_redirect(self, tmp_path: pathlib.Path) -> None:
        """pytest resolves basetemp lazily from ``gettempdir()``, so ORDER decides this.

        If the redirect wins the race, pytest's whole basetemp lands inside the run's
        temp root -- which the session teardown deletes, taking every failed test's
        retained ``tmp_path`` with it, and adding ~25 characters to every temp path in
        the suite. ``_isolate_tempfile_base`` forces ``getbasetemp()`` before
        redirecting to make that impossible; this is what keeps it that way.
        """
        assert not tmp_path.resolve().is_relative_to(
            pathlib.Path(tempfile.gettempdir()).resolve()
        ), f"pytest basetemp {tmp_path} is inside the redirected temp root"

    def test_a_mkdtemp_with_no_dir_argument_lands_inside_it(self) -> None:
        made = pathlib.Path(tempfile.mkdtemp())
        try:
            assert made.parent == pathlib.Path(tempfile.gettempdir())
        finally:
            made.rmdir()

    def test_the_env_vars_carry_the_redirect_to_child_processes(self) -> None:
        """A child re-derives its own temp dir, so the global alone is not enough.

        All three names are set because the platforms disagree on which is real:
        ``TMPDIR`` on POSIX, ``TEMP``/``TMP`` on Windows.
        """
        base = tempfile.gettempdir()

        for name in _root._TMP_ENV_VARS:
            assert os.environ.get(name) == base, f"{name} does not carry the redirect"

    def test_the_root_name_carries_the_account_and_the_pid(self) -> None:
        """A bare pid collides across accounts: POSIX shares one temp root.

        Two accounts can hold the same pid simultaneously, so the account segment is what
        keeps one run's root distinct from another's. The pid is carried for a HUMAN
        reading a stray directory -- nothing parses it, and no code reclaims a root it did
        not create.
        """
        prefix = _root._tmp_root_prefix_for_run()

        assert prefix.startswith(_root._TMP_ROOT_PREFIX)
        assert prefix.endswith(f"-{os.getpid()}-")
        # the account segment sits between the two, and is non-empty
        assert len(prefix) > len(_root._TMP_ROOT_PREFIX) + len(str(os.getpid())) + 2

    def test_the_root_is_created_atomically_and_owner_only(self, tmp_path: pathlib.Path) -> None:
        """A predictable name in a world-writable temp root is a hijack.

        Another local account can pre-create the exact pid-derived name as a SYMLINK to a
        directory it controls; ``mkdir(exist_ok=True)`` adopts it, the redirect follows it,
        and every temp write in the run lands somewhere that account chose and can read.
        ``mkdtemp`` closes it three ways: a random component nobody can guess ahead of
        time, ``O_EXCL`` so nothing existing is adopted, and mode 0o700.
        """
        made = _root._create_tmp_root(tmp_path)

        assert made.is_dir() and not made.is_symlink()
        assert made.parent == tmp_path
        assert made.name.startswith(_root._tmp_root_prefix_for_run())
        # A random component after the pid is what makes the name unguessable.
        assert made.name != _root._tmp_root_prefix_for_run().rstrip("-")
        if os.name != "nt":  # POSIX mode bits; Windows uses ACLs
            assert (made.stat().st_mode & 0o777) == 0o700

    def test_two_roots_in_the_same_process_never_collide(self, tmp_path: pathlib.Path) -> None:
        """Which also proves nothing pre-existing is ever adopted."""
        first = _root._create_tmp_root(tmp_path)
        second = _root._create_tmp_root(tmp_path)

        assert first != second


class TestTheTempResidueReport:
    """Residue must be REPORTED, not just relocated somewhere pytest prunes."""

    def test_it_names_what_was_left_behind(self, tmp_path: pathlib.Path) -> None:
        message = _root._tmp_residue_report(tmp_path, ["tmpleaked"], per_test=False)

        assert "tmpleaked" in message
        # The fix belongs in the message: an rmtree in tearDown is the shape that
        # leaks, because unittest skips tearDown entirely when setUp raises.
        assert "addCleanup" in message
        assert _root._TMP_PER_TEST_ENV in message

    def test_it_names_the_test_tempfile_was_serving_when_it_made_each_entry(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Session teardown can only blame the LAST test a worker ran; the creator is
        what names the culprit in the one CI run that leaks."""
        creators = {
            "tmpduring": ("test/test_a.py::test_leaks", True, ""),
            "tmpafter": ("test/test_b.py::test_left_a_thread", False, "Thread-7 (late)"),
            "tmplater": ("test/test_c.py::test_innocent", True, "Thread-7 (late)"),
            "tmpearly": ("", False, ""),
        }
        message = _root._tmp_residue_report(
            tmp_path,
            ["tmpduring", "tmpafter", "tmplater", "tmpearly", "tmpchild"],
            per_test=False,
            creators=creators,
        )

        assert "tmpduring  <- made by tempfile during test/test_a.py::test_leaks\n" in message
        assert (
            "tmpafter  <- made by tempfile on thread 'Thread-7 (late)' after "
            "test/test_b.py::test_left_a_thread finished: a thread or callback it left running"
        ) in message
        # A thread an EARLIER test left running can create during a later one; naming
        # the thread is what keeps the later test from taking the blame.
        assert (
            "tmplater  <- made by tempfile on thread 'Thread-7 (late)' during "
            "test/test_c.py::test_innocent"
        ) in message
        assert "tmpearly  <- made by tempfile before the first test" in message
        assert "tmpchild  <- not made by tempfile in this worker: a child process" in message

    def test_per_test_mode_lists_names_without_a_creator(self, tmp_path: pathlib.Path) -> None:
        message = _root._tmp_residue_report(
            tmp_path, ["test_guilty/tmpleaked"], per_test=True, creators={}
        )

        assert "    test_guilty/tmpleaked\n" in message
        assert "<-" not in message

    @pytest.mark.skipif(
        bool(os.environ.get("KIROCREW_TMP_PER_TEST")),
        reason="per-test diagnostic mode gives each test its own base, which is not recorded",
    )
    def test_the_audit_hook_records_a_bare_temp_entry_against_the_running_test(
        self, request: pytest.FixtureRequest
    ) -> None:
        live = TestNoMetricIsEmittedAtImport._live_root_conftest(request.config)
        made_dir = pathlib.Path(tempfile.mkdtemp())
        fd, made_file = tempfile.mkstemp()
        os.close(fd)
        try:
            for name in (made_dir.name, pathlib.Path(made_file).name):
                assert live._TMP_CREATORS.get(name) == (request.node.nodeid, True, "")
        finally:
            made_dir.rmdir()
            os.unlink(made_file)

    def test_a_third_party_or_by_design_entry_is_not_residue(self, tmp_path: pathlib.Path) -> None:
        """A guard that cries wolf gets deleted, and then it protects nothing.

        Redirecting `tempfile`'s base also redirects every CHILD's, so the browser and
        its driver put their scratch here too, and production's deliberately-persistent
        screenshot spool lands here rather than in the real temp root. None of that is a
        test forgetting to clean up.
        """
        for name in (
            "kirocrew-computer-shots",
            "playwright-transform-cache-1001",
            ".org.chromium.Chromium.AHpK6x",
        ):
            (tmp_path / name).mkdir()

        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_it_stays_silent_when_nothing_was_left(self, tmp_path: pathlib.Path) -> None:
        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_a_nested_pytests_basetemp_is_not_residue(self, tmp_path: pathlib.Path) -> None:
        """Several tests spawn a nested pytest, which computes its own basetemp inside
        ours because it resolves ``gettempdir()`` after the redirect. That is a child
        runner's bookkeeping, with its own retention, not residue this suite dropped."""
        (tmp_path / "pytest-of-someone").mkdir()

        assert _root._tmp_residue(tmp_path, per_test=False) == []

    def test_per_test_mode_reports_the_leaf_not_the_test_directory(
        self, tmp_path: pathlib.Path
    ) -> None:
        """In per-test mode the immediate children are bases the fixture itself made.

        Reporting those would name every test in the run as its own leak and answer
        nothing -- the whole point of the mode is that the leaf's PARENT is the test id.
        """
        (tmp_path / "test_guilty").mkdir()
        (tmp_path / "test_guilty" / "tmpleaked").mkdir()
        (tmp_path / "test_innocent").mkdir()

        assert _root._tmp_residue(tmp_path, per_test=True) == ["test_guilty/tmpleaked"]

    def test_an_unreadable_base_is_not_reported_as_residue(self, tmp_path: pathlib.Path) -> None:
        """A guard that reds the suite on an unanswerable question gets deleted, and
        then it protects nothing."""
        assert _root._tmp_residue(tmp_path / "does-not-exist", per_test=False) == []


# ── the process working directory ──────────────────────────────────────────


#: Written by one test and asserted by the next: a failure raised inside a finalizer
#: is reported as a teardown error against an innocent test id.
_CWD_ORDER: dict[str, str] = {}


class TestTheWorkingDirectoryIsRestored:
    """The CWD is per-PROCESS, so one test's ``os.chdir`` is every later test's start.

    Survivable only while the directory outlived the run. With
    ``tmp_path_retention_policy = failed`` a passing test's ``tmp_path`` is removed at that
    test's own teardown, so a leaked CWD leaves the worker in a DELETED directory and
    ``Path.cwd()`` then raises ``FileNotFoundError`` in every later test that reaches it --
    including inside production code (``TaskRunner.__init__`` does ``work_dir or Path.cwd()``).
    """

    def test_this_test_starts_somewhere_real(self) -> None:
        """Which is only true if no earlier test on this worker leaked its directory."""
        assert pathlib.Path.cwd().is_dir()

    def test_a_chdir_is_undone_before_fixture_finalizers_run(
        self, tmp_path: pathlib.Path, request: pytest.FixtureRequest
    ) -> None:
        """ORDER, not just eventual restoration -- and it is load-bearing on Windows.

        An outer autouse FIXTURE would tear down LAST, after ``tmp_path`` cleanup had
        already tried to remove a directory the process was still sitting in; Windows
        refuses to delete its own working directory, so that cleanup fails there. A
        ``tryfirst`` ``pytest_runtest_teardown`` hookimpl runs before the default one,
        which is what performs fixture finalization -- so a finalizer registered here
        observes the CWD already restored.

        The observation is asserted by the NEXT test rather than in a finalizer, because a
        failure raised inside a finalizer is reported as a teardown error against an
        innocent-looking test id.
        """
        _CWD_ORDER["expected"] = os.getcwd()
        request.addfinalizer(lambda: _CWD_ORDER.__setitem__("at_finalizer", os.getcwd()))

        os.chdir(tmp_path)
        assert pathlib.Path.cwd().samefile(tmp_path)

    def test_the_finalizer_saw_the_cwd_already_restored(self) -> None:
        """Reads what the previous test recorded. Ordered by definition order in the file."""
        assert _CWD_ORDER.get("at_finalizer") == _CWD_ORDER.get("expected"), (
            f"CWD at fixture-finalizer time was {_CWD_ORDER.get('at_finalizer')!r}, "
            f"expected {_CWD_ORDER.get('expected')!r} -- the restore ran too late, so "
            "tmp_path cleanup would be asked to delete the process's own directory"
        )


class TestTheTestLoopIsRetiredBeforeThePinsLift:
    """What a test's loop leaves behind must finish while ``KIROCREW_HOME`` is pinned.

    pytest-asyncio 0.21 ends the loop with a bare ``loop.close()``: a still-pending
    task's coroutine then runs its ``finally`` blocks at garbage collection, and a
    default-executor job (``asyncio.to_thread``) is abandoned mid-flight. Either one
    reaching ``config_dir()`` after the unpin resolves the OPERATOR's data home -- a fresh
    fake ``HOME`` grew ``~/.kiro/crew`` and the breadcrumb behind four subagent
    ``on_done`` tests. The floor retires both in its ``tryfirst`` teardown hook, before any
    fixture teardown. Driven here on a private loop, through the same ``item.funcargs``
    shape the hook reads.
    """

    def test_a_pending_task_and_an_executor_job_finish_inside_the_hook(self) -> None:
        import asyncio
        import threading
        import types

        loop = asyncio.new_event_loop()
        try:
            seen: list[str] = []
            gate = threading.Event()

            async def detached() -> None:
                try:
                    await asyncio.sleep(3600)
                finally:
                    seen.append("finally")  # runs at cancel, not at garbage collection

            def job() -> None:
                gate.wait(5.0)
                seen.append("job")

            async def arm() -> None:
                asyncio.ensure_future(detached())
                loop.run_in_executor(None, job)
                await asyncio.sleep(0)  # let both start

            loop.run_until_complete(arm())
            gate.set()
            item = types.SimpleNamespace(funcargs={"event_loop": loop})

            _root._join_test_loop_executor(item)

            assert sorted(seen) == ["finally", "job"], (
                f"the hook left {set(['finally', 'job']) - set(seen)} to run after the pins "
                "lift -- that is when a config load resolves the operator's home"
            )
            assert not [t for t in asyncio.all_tasks(loop) if not t.done()]
            # Still a working loop: an inner fixture's async teardown may need one more
            # to_thread, and shutdown_default_executor alone would have latched a refusal.
            assert loop.run_until_complete(asyncio.to_thread(lambda: "ok")) == "ok"
        finally:
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()

    def test_a_task_spawned_while_the_hook_cancels_is_retired_too(self) -> None:
        """A cancelled task whose ``finally`` starts another task leaves no orphan.

        A dashboard turn cancelled at teardown runs its queue cycle on the way out, and
        that cycle dispatches the next queued turn as a NEW task. One cancel-and-wait
        pass over a snapshot taken before the cancel never sees it, so it stays
        pending on a loop pytest-asyncio then closes, and asyncio logs ``Task was
        destroyed but it is pending!`` at whatever later moment the collector runs --
        inside another test's ``caplog`` window, on another file's assertion.
        """
        import asyncio
        import types

        loop = asyncio.new_event_loop()
        try:
            seen: list[str] = []

            async def successor() -> None:
                try:
                    await asyncio.Event().wait()  # parked until cancelled
                finally:
                    seen.append("successor finally")

            async def turn() -> None:
                try:
                    await asyncio.Event().wait()  # parked until cancelled
                finally:
                    asyncio.ensure_future(successor())  # the next queued turn
                    seen.append("turn finally")

            async def arm() -> None:
                asyncio.ensure_future(turn())
                await asyncio.sleep(0)  # let it start

            loop.run_until_complete(arm())
            item = types.SimpleNamespace(funcargs={"event_loop": loop})

            _root._join_test_loop_executor(item)

            left = [t for t in asyncio.all_tasks(loop) if not t.done()]
            assert not left, (
                f"the hook left {len(left)} task(s) pending that a cancelled task started "
                "while unwinding; loop.close() orphans them and the collector logs them "
                "into a later test"
            )
            assert seen == ["turn finally", "successor finally"]
        finally:
            loop.close()

    def test_the_hook_runs_before_fixture_finalizers(self) -> None:
        """Placement is the guarantee: a fixture would tear down after the unpin."""
        source = _ROOT_CONFTEST.read_text(encoding="utf-8")
        hook_at = source.index("def pytest_runtest_teardown(item, nextitem):")
        body = source[hook_at : source.index("\ndef ", hook_at + 1)]
        assert "_join_test_loop_executor(item)" in body, (
            "the loop retirement is not called from the tryfirst pytest_runtest_teardown "
            "hook; anywhere later, KIROCREW_HOME may already be unpinned when it runs"
        )


#: The values the floor gives every key it turns off (``GIT_MAINTENANCE_OFF_CONFIG``).
_MAINTENANCE_OFF = {"maintenance.auto": "false", "gc.auto": "0"}


def _git_config_values(cwd, *scope: str, env=None) -> dict[str, str]:
    """What git reads for each maintenance key in *cwd*, in *scope* (all scopes if none)."""
    git = platform_compat.trusted_git_bin()
    if git is None:
        pytest.fail(
            "prerequisite missing: git is not installed, so the maintenance keys cannot be read"
        )
    return {
        key: subprocess.run(
            [git, "config", *scope, "--get", key],
            cwd=cwd,
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
        ).stdout.strip()
        for key in _MAINTENANCE_OFF
    }


@pytest.fixture(scope="module")
def _maintenance_config_before_any_test(tmp_path_factory) -> tuple[dict, dict]:
    """What git reads for the maintenance keys before the function-scoped floor runs."""
    cwd = tmp_path_factory.mktemp("module-git")
    return _git_config_values(cwd), _git_config_values(cwd, "--global")


class TestGitAutoMaintenanceIsOff:
    """The repo-root ``conftest`` turns git's auto maintenance off for every test and fixture.

    A commit otherwise leaves a detached ``git maintenance run --auto`` adding and
    removing lock files under .git after it returns, so a test that walks or copies
    the repo right after can see a file vanish. ``_git_global_config`` covers
    session, module and class fixtures on every testpath, through a global config
    file and ``GIT_CONFIG_COUNT``; ``_git_maintenance_off`` re-adds the entries for
    a test whose environment lost them. A fixture helper that sets its own
    ``GIT_CONFIG_COUNT`` applies ``git_maintenance_off_entries`` to its env.
    """

    @staticmethod
    def _floor():
        floor = _root._git_maintenance_off
        return getattr(floor, "__wrapped__", floor)

    def test_git_reads_both_keys_as_off_in_a_module_fixture(
        self, _maintenance_config_before_any_test: tuple[dict, dict]
    ) -> None:
        # Session-, module- and class-scoped fixtures run before the
        # function-scoped floor, and some commit a template every test copies.
        effective, global_file = _maintenance_config_before_any_test
        assert effective == _MAINTENANCE_OFF
        assert global_file == _MAINTENANCE_OFF

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [
            pytest.param(
                {"GIT_CONFIG_COUNT": "many"},
                {
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_KEY_1": "gc.auto",
                    "GIT_CONFIG_VALUE_1": "0",
                    "GIT_CONFIG_COUNT": "2",
                },
                id="malformed-count-restarts-at-0",
            ),
            pytest.param(
                {"GIT_CONFIG_COUNT": "-1"},
                {
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_KEY_1": "gc.auto",
                    "GIT_CONFIG_VALUE_1": "0",
                    "GIT_CONFIG_COUNT": "2",
                },
                id="negative-count-restarts-at-0",
            ),
            pytest.param(
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_KEY_1": "GC.Auto",
                    "GIT_CONFIG_VALUE_1": "0",
                },
                {},
                id="already-off-adds-nothing",
            ),
            pytest.param(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "false",
                },
                {
                    "GIT_CONFIG_KEY_1": "maintenance.auto",
                    "GIT_CONFIG_VALUE_1": "false",
                    "GIT_CONFIG_KEY_2": "gc.auto",
                    "GIT_CONFIG_VALUE_2": "0",
                    "GIT_CONFIG_COUNT": "3",
                },
                id="gc-auto-missing-appends-both",
            ),
            pytest.param(
                {
                    "GIT_CONFIG_COUNT": "3",
                    "GIT_CONFIG_KEY_0": "maintenance.auto",
                    "GIT_CONFIG_VALUE_0": "false",
                    "GIT_CONFIG_KEY_1": "gc.auto",
                    "GIT_CONFIG_VALUE_1": "0",
                    "GIT_CONFIG_KEY_2": "Maintenance.Auto",
                    "GIT_CONFIG_VALUE_2": "true",
                },
                {
                    "GIT_CONFIG_KEY_3": "maintenance.auto",
                    "GIT_CONFIG_VALUE_3": "false",
                    "GIT_CONFIG_KEY_4": "gc.auto",
                    "GIT_CONFIG_VALUE_4": "0",
                    "GIT_CONFIG_COUNT": "5",
                },
                id="a-later-true-entry-is-overridden",
            ),
        ],
    )
    def test_the_entries_follow_what_the_environment_already_carries(
        self, environ: dict[str, str], expected: dict[str, str]
    ) -> None:
        assert _root._git_maintenance_off_entries(environ) == expected

    def test_git_reads_both_keys_as_off_inside_a_test(self, tmp_path) -> None:
        assert _git_config_values(tmp_path) == _MAINTENANCE_OFF
        assert _git_config_values(tmp_path, "--global") == _MAINTENANCE_OFF

    def test_the_global_file_keeps_the_includes_ahead_of_the_keys(self) -> None:
        # The keys must come after the includes, or an included file could
        # turn auto maintenance back on.
        text = _root._GIT_MAINTENANCE.git_global_config_text(['C:/a "b"\\c'])
        assert text == (
            '[include]\n\tpath = "C:/a \\"b\\"\\\\c"\n'
            "[maintenance]\n\tauto = false\n"
            "[gc]\n\tauto = 0\n"
        )

    def test_a_local_push_starts_receive_pack_with_both_keys_off(self, tmp_path) -> None:
        # A push over the local transport starts receive-pack in the remote
        # without GIT_CONFIG_* (git's local_repo_env), so only a config file
        # reaches it. The remote here has no repo config of its own.
        git = platform_compat.trusted_git_bin()
        if git is None:
            pytest.fail("prerequisite missing: git is not installed, so the push cannot be run")

        def run(cwd, *args: str) -> None:
            subprocess.run([git, *args], cwd=cwd, capture_output=True, check=True)

        remote = tmp_path / "remote.git"
        run(tmp_path, "init", "-q", "--bare", str(remote))
        hook = remote / "hooks" / "post-receive"
        # LF on every host: Git for Windows' sh would read a CRLF line's redirect
        # target with its CR, and neither file would appear under its real name.
        hook.write_text(
            "#!/bin/sh\n"
            "git config --get maintenance.auto > seen-maintenance.txt\n"
            "git config --get gc.auto > seen-gc.txt\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)
        work = tmp_path / "work"
        run(tmp_path, "clone", "-q", str(remote), str(work))
        (work / "a.txt").write_text("x\n", encoding="utf-8")
        run(work, "add", "a.txt")
        run(work, "commit", "-q", "-m", "one")
        run(work, "push", "-q", "origin", "HEAD:refs/heads/main")
        seen = {
            "maintenance.auto": (remote / "seen-maintenance.txt").read_text(encoding="utf-8"),
            "gc.auto": (remote / "seen-gc.txt").read_text(encoding="utf-8"),
        }
        assert {key: value.strip() for key, value in seen.items()} == _MAINTENANCE_OFF

    @pytest.mark.parametrize(
        ("module_name", "helper_name", "takes_repo"),
        [
            ("test_governance_updates", "_fixture_git_env", True),
            ("test_pipeline_conductor_claim_preflight_end_to_end", "_fixture_git_env", False),
            ("test_preflight_write_permission", "_fixture_git_env", False),
            ("test_prepare_pr_diff_signals", "_git_env", False),
            ("test_prepare_pr_local_review", "_fixture_git_env", False),
            ("push_guard_helpers", "_fixture_git_env", False),
            ("test_ratchet_scope", "_fixture_git_env", False),
        ],
    )
    def test_a_fixture_git_env_that_sets_its_own_config_count_keeps_maintenance_off(
        self, module_name: str, helper_name: str, takes_repo: bool, tmp_path, monkeypatch
    ) -> None:
        # These helpers set GIT_CONFIG_COUNT themselves, which drops the floor's
        # entries. The floor's global file is held away here, as most of them
        # do themselves, so only the entries the helper applies can pass this.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        helper = getattr(importlib.import_module(module_name), helper_name)
        env = helper(str(tmp_path)) if takes_repo else helper()
        assert _git_config_values(tmp_path, env=env) == _MAINTENANCE_OFF

    @pytest.mark.parametrize(
        "driver",
        [
            "add_contributor_git_env",
            "ai_review_git",
            "ai_review_shallow_fetch_env",
            "apps_registry_init",
            "dev_fleet_allowlisted_env",
            "file_line_cap_git",
            "macos_gate_git_env",
        ],
    )
    def test_a_helper_that_builds_its_own_git_env_starts_no_auto_maintenance(
        self, driver: str, tmp_path, monkeypatch
    ) -> None:
        # These helpers drop or never carry the suite's GIT_CONFIG_* entries
        # (they strip GIT_*, build the env from nothing, or filter os.environ
        # through an allowlist). Setting GIT_TRACE2_EVENT in os.environ cannot
        # see their git, so trace_git_spawns adds it to each spawn's own env.
        # The control commit proves the trace reaches git under that helper.
        repo = tmp_path / "repo"
        repo.mkdir()
        trace = trace_git_spawns(monkeypatch, tmp_path / "trace")
        run = getattr(self, f"_drive_{driver}")(repo, tmp_path)
        run("-c", "maintenance.auto=true", "commit", "-q", "--allow-empty", "-m", "control")
        control = auto_maintenance_starts(trace)
        assert control, f"the trace did not see the control commit under {driver}"
        for entry in trace.iterdir():
            entry.unlink()
        run("commit", "-q", "--allow-empty", "-m", "fixture")
        assert auto_maintenance_starts(trace) == []

    @staticmethod
    def _run_git(repo, env):
        git = platform_compat.trusted_git_bin()
        if git is None:
            pytest.fail("prerequisite missing: git is not installed, so the helper cannot be run")

        def run(*args: str) -> None:
            subprocess.run(
                [git, "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                cwd=repo,
                env=env,
                capture_output=True,
                check=True,
            )

        return run

    def _drive_add_contributor_git_env(self, repo, tmp_path):
        module = importlib.import_module("test_add_contributor_push_skip")
        env = module._git_env(tmp_path, tmp_path)
        module._git(repo, "init", "-q", env=env)
        return lambda *args: module._git(repo, *args, env=env)

    def _drive_ai_review_git(self, repo, tmp_path):
        cls = importlib.import_module("ai_review_workflows_helpers").UxEvidenceGateHarness
        case = cls()
        case._git(repo, "init", "-q")
        return lambda *args: case._git(repo, *args)

    def _drive_ai_review_shallow_fetch_env(self, repo, tmp_path):
        module = importlib.import_module("test_ai_review_shallow_fetch")
        run = self._run_git(repo, module._git_env(tmp_path))
        run("init", "-q")
        return run

    def _drive_apps_registry_init(self, repo, tmp_path):
        module = importlib.import_module("test_apps_registry_git_transport")
        run = self._run_git(repo, module._registry_fixture_env(tmp_path))
        module._init_registry_fixture_repo(run)
        return run

    def _drive_dev_fleet_allowlisted_env(self, repo, tmp_path):
        module = importlib.import_module("test_dev_fleet_app")
        run = self._run_git(repo, module._allowlisted_fixture_git_env())
        run("init", "-q")
        return run

    def _drive_file_line_cap_git(self, repo, tmp_path):
        module = importlib.import_module("test_file_line_cap")
        module._git(repo, "init", "-q")
        return lambda *args: module._git(repo, *args)

    def _drive_macos_gate_git_env(self, repo, tmp_path):
        module = importlib.import_module("test_macos_platform_tests_gate")
        run = self._run_git(repo, module._fixture_git_env(tmp_path))
        run("init", "-q")
        return run

    #: What a git hook exports to a suite it starts: ``GIT_INDEX_FILE`` always,
    #: ``GIT_DIR`` and ``GIT_WORK_TREE`` when its caller set them.
    _HOOK_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

    @staticmethod
    def _expected_stripped() -> frozenset[str]:
        """What the floor must strip, read from the production list, not from the floor."""
        from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

        assert "GIT_CEILING_DIRECTORIES" in _GIT_LOCATION_VARS
        return (_GIT_LOCATION_VARS - {"GIT_CEILING_DIRECTORIES"}) | {"GIT_CONFIG_PARAMETERS"}

    @staticmethod
    def _outer_values(sentinel) -> dict[str, str]:
        """A value for every stripped variable, each aimed at the *sentinel* repository."""
        git_dir = sentinel / ".git"
        return {
            "GIT_DIR": str(git_dir),
            "GIT_COMMON_DIR": str(git_dir),
            "GIT_WORK_TREE": str(sentinel),
            "GIT_INDEX_FILE": str(git_dir / "index"),
            "GIT_OBJECT_DIRECTORY": str(git_dir / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects"),
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
            "GIT_NAMESPACE": "outer",
            "GIT_CONFIG_PARAMETERS": "'maintenance.auto'='true'",
        }

    @pytest.mark.parametrize(
        "driver",
        [
            "inherited_env",
            "add_contributor_git_env",
            "ai_review_git",
            "ai_review_shallow_fetch_env",
            "apps_registry_init",
            "dev_fleet_allowlisted_env",
            "file_line_cap_git",
            "macos_gate_git_env",
        ],
    )
    @pytest.mark.parametrize(
        "outer_vars",
        [("GIT_INDEX_FILE",), _HOOK_LOCATION_VARS, None],
        ids=["index-only", "dir-work-tree-index", "every-stripped-var"],
    )
    def test_a_fixture_commit_under_a_hook_env_leaves_the_outer_index_alone(
        self, driver: str, outer_vars: tuple[str, ...] | None, tmp_path, monkeypatch
    ) -> None:
        # The location variables point at a sentinel repository, as they point
        # at the operator's when a hook runs the suite. The floor removes them,
        # so a fixture repo's add and commit, under each helper or an inherited
        # env, cannot stage into the sentinel's index.
        sentinel = tmp_path / "sentinel"
        sentinel.mkdir()
        outer = self._run_git(sentinel, None)
        outer("init", "-q")
        (sentinel / "kept.txt").write_text("kept\n", encoding="utf-8")
        outer("add", "kept.txt")
        index = sentinel / ".git" / "index"
        before = index.read_bytes()
        outer_env = self._outer_values(sentinel)
        assert set(outer_env) == self._expected_stripped()
        for name in sorted(outer_env) if outer_vars is None else outer_vars:
            monkeypatch.setenv(name, outer_env[name])
        floor = _root._git_location_unset
        getattr(floor, "__wrapped__", floor)(monkeypatch, None)
        assert sorted(name for name in outer_env if name in os.environ) == []
        repo = tmp_path / "repo"
        repo.mkdir()
        run = getattr(self, f"_drive_{driver}")(repo, tmp_path)
        (repo / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        failed = None
        try:
            run("add", "fixture.txt")
            run("commit", "-q", "-m", "fixture")
        except subprocess.CalledProcessError as exc:
            failed = exc
        # The sentinel first: a commit that staged into it can then fail on
        # the sentinel's objects, which would hide where the add went.
        assert index.read_bytes() == before
        assert failed is None, failed.stderr

    def test_the_session_floor_removes_them_before_any_fixture_runs(
        self, tmp_path, tmp_path_factory, monkeypatch
    ) -> None:
        # Session, module and class fixtures run before the function-scoped
        # floor, and some commit a template repo every test copies.
        outer = self._outer_values(tmp_path)
        assert set(outer) == self._expected_stripped()
        for name, value in outer.items():
            monkeypatch.setenv(name, value)
        session = _root._git_global_config
        floor = getattr(session, "__wrapped__", session)(tmp_path / "xdg", tmp_path_factory)
        try:
            next(floor)
            assert [name for name in outer if name in os.environ] == []
        finally:
            floor.close()
        assert {name: os.environ.get(name) for name in outer} == outer

    def test_the_discovery_ceiling_survives_the_strip(self, monkeypatch) -> None:
        # The floor sets GIT_CEILING_DIRECTORIES itself for every test, after the
        # session strip, so only the function strip could remove it.
        ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        assert ceiling, "the floor's discovery ceiling is unset inside a test"
        floor = _root._git_location_unset
        getattr(floor, "__wrapped__", floor)(monkeypatch, None)
        assert os.environ.get("GIT_CEILING_DIRECTORIES") == ceiling

    def test_an_operator_config_parameter_cannot_turn_maintenance_back_on(
        self, tmp_path, monkeypatch
    ) -> None:
        # A hook started by `git -c ...` exports GIT_CONFIG_PARAMETERS, which git
        # reads over the floor's GIT_CONFIG_COUNT entries. The control shows it
        # does; the floor strips it.
        operator = "'maintenance.auto'='true'"
        control = _git_config_values(
            tmp_path, env={**os.environ, "GIT_CONFIG_PARAMETERS": operator}
        )
        assert control["maintenance.auto"] == "true"
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", operator)
        floor = _root._git_location_unset
        getattr(floor, "__wrapped__", floor)(monkeypatch, None)
        assert "GIT_CONFIG_PARAMETERS" not in os.environ
        assert _git_config_values(tmp_path) == _MAINTENANCE_OFF

    def _drive_inherited_env(self, repo, tmp_path):
        # As test_a_local_push_starts_receive_pack_with_both_keys_off runs git.
        run = self._run_git(repo, None)
        run("init", "-q")
        return run

    def test_the_entries_are_appended_after_config_the_environment_already_carries(
        self,
    ) -> None:
        mp = pytest.MonkeyPatch()
        try:
            mp.setenv("GIT_CONFIG_COUNT", "2")
            mp.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
            mp.setenv("GIT_CONFIG_VALUE_0", "false")
            mp.setenv("GIT_CONFIG_KEY_1", "core.hooksPath")
            mp.setenv("GIT_CONFIG_VALUE_1", os.devnull)
            self._floor()(mp, None)
            assert os.environ["GIT_CONFIG_COUNT"] == "4"
            entries = [
                (os.environ[f"GIT_CONFIG_KEY_{i}"], os.environ[f"GIT_CONFIG_VALUE_{i}"])
                for i in range(4)
            ]
            assert entries == [
                ("core.fsmonitor", "false"),
                ("core.hooksPath", os.devnull),
                ("maintenance.auto", "false"),
                ("gc.auto", "0"),
            ]
        finally:
            mp.undo()
