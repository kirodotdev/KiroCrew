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
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import pathlib
import queue
import sys
import tempfile
import threading
from logging.handlers import QueueListener

import pytest

from kiro_crew import cli
from kiro_crew.log_redaction import install_log_redaction

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_CONFTEST = _REPO_ROOT / "conftest.py"
_SRC = _REPO_ROOT / "src" / "kiro_crew"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    pytest already loads it as a plugin, but reaching it through the plugin manager
    depends on the name pytest happened to register. Loading it by path is
    deterministic, and the fixtures it defines are inert in this namespace (a
    ``@pytest.fixture`` decorator only marks a function; nothing collects them here).
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_isolation_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()


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
        scratch = (pathlib.Path.home() / ".kiro" / "crew" / "scratch" / "runtime-x" / "pytest-0").resolve()
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
            for key in ("KIROCREW_HOME", "KIROCREW_WORKSPACE", "KIROCREW_PROFILE", "KIROCREW_TELEMETRY")
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
        with pytest.raises(StopIteration):
            next(wrapper)

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
        with pytest.raises(StopIteration):
            next(wrapper)

        assert _root.IMPORT_TIME_METRIC_EMITTERS == [f"conftest import (before {module.nodeid})"]
        assert not provider._ever_built


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

        assert workspace_root().resolve() == pathlib.Path(
            os.environ["KIROCREW_WORKSPACE"]
        ).resolve()

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

        assert "KIROCREW_FLOOR_PROBE" not in os.environ, "undo() must still revert the test's own records"
        assert os.environ.get("KIROCREW_HOME") == pinned
        assert not _inside_a_guarded_root(pathlib.Path(pinned))

    def test_undo_does_not_drop_the_other_pins_either(self, monkeypatch: pytest.MonkeyPatch) -> None:
        before = {k: os.environ.get(k) for k in ("KIROCREW_WORKSPACE", "KIROCREW_POD_ROOT", "KIROCREW_POD_ENV_DIR")}
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
    were deleted (#4912).
    """

    def test_the_spec_write_target_is_not_the_operators_real_home(self) -> None:
        from kiro_crew import agent

        target = agent.kiro_agents_dir_path().resolve()

        assert not _inside_a_guarded_root(target), (
            f"the agent-spec write target is a real home path: {target}"
        )

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

        assert agent._decline_shared_agent_home(audit=False) is None, (
            "the floor's pinned target is being treated as the shared agent home"
        )

    def test_the_ambient_resolver_has_exactly_one_caller(self) -> None:
        """``ambient_agents_dir`` is override-BLIND, so a second caller is a leak.

        Its docstring says "not a general-purpose reader" -- this makes that
        enforceable. Anything that reads it resolves the operator's REAL agents dir
        even under the floor's pin, which is the leak this seam exists to close; the
        write guard is the one caller whose question is genuinely about the
        environment.
        """
        allowed = {"kiro_crew/config/paths.py", "kiro_crew/agent.py"}
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
        self, tmp_path, monkeypatch, tmp_path_factory
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
        body(lambda name: tmp_path / name, monkeypatch, tmp_path_factory)

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
        # The kiro-cli/amazon-q sqlite tuples that used to sit here as direct
        # ``Path.home()`` bindings are now PROJECTIONS over the canonical table in
        # ``kiro_crew/identity_stores.py`` (``sqlite_dbs(...)`` resolves the home
        # inside the call), so they no longer match this tripwire's import-time
        # shape and need no exclusion. Their anchor rule ("must name the REAL
        # home"; stub the READER, never move the anchor) is carried forward by
        # ``test_identity_stores.py::TestUsageTuplesAnchorTheRealHome``.
        # An ALLOW-LIST root, so the same rule applies from the other direction: the
        # file browser's first permitted root is the operator's real home BY DESIGN,
        # since that is the directory the user is entitled to browse. Redirecting it
        # would make every containment test assert against a root that does not ship.
        # Nothing here writes: the module reads the value to bound path resolution.
        ("kiro_crew/apps/builtins/file_explorer/server.py", "_HOME"): "security anchor: the browsing allow-list root",
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
            + "\n".join(f"    {mod}:{line} {attr}" for (mod, attr), line in sorted(unhandled.items()))
            + "\nA test that reaches one of these writes the operator's real home. Pin it, "
            "or exclude it and say why."
        )

    def test_the_exclusion_list_has_not_gone_stale(self) -> None:
        """An exclusion for a binding that no longer exists hides the next one."""
        bindings = self._home_bindings()
        stale = [key for key in self._EXCLUDED if key not in bindings]

        assert not stale, f"_EXCLUDED names bindings that no longer exist: {stale}"


# ── the system temp directory ─────────────────────────────────────────────


class TestTheTempBaseIsRedirected:
    """``tempfile``'s base must be a per-run directory, not the shared temp root.

    The point is not tidiness. A bare ``mkdtemp()`` whose cleanup is missing or skipped
    used to leave its directory in the platform temp root forever, and MEASURED on the
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

    def test_pytests_own_basetemp_is_not_inside_the_redirect(
        self, tmp_path: pathlib.Path
    ) -> None:
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

    def test_the_root_is_created_atomically_and_owner_only(
        self, tmp_path: pathlib.Path
    ) -> None:
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

    def test_two_roots_in_the_same_process_never_collide(
        self, tmp_path: pathlib.Path
    ) -> None:
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

    def test_a_third_party_or_by_design_entry_is_not_residue(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A guard that cries wolf gets deleted, and then it protects nothing.

        Redirecting `tempfile`'s base also redirects every CHILD's, so the browser and
        its driver put their scratch here too, and production's deliberately-persistent
        screenshot spool lands here rather than in the real temp root. None of that is a
        test forgetting to clean up.
        """
        for name in ("kirocrew-computer-shots", "playwright-transform-cache-1001",
                     ".org.chromium.Chromium.AHpK6x"):
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

    def test_an_unreadable_base_is_not_reported_as_residue(
        self, tmp_path: pathlib.Path
    ) -> None:
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

    pytest-asyncio 0.20 ends the loop with a bare ``loop.close()``: a still-pending
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

    def test_the_hook_runs_before_fixture_finalizers(self) -> None:
        """Placement is the guarantee: a fixture would tear down after the unpin."""
        source = _ROOT_CONFTEST.read_text(encoding="utf-8")
        hook_at = source.index("def pytest_runtest_teardown(item, nextitem):")
        body = source[hook_at : source.index("\ndef ", hook_at + 1)]
        assert "_join_test_loop_executor(item)" in body, (
            "the loop retirement is not called from the tryfirst pytest_runtest_teardown "
            "hook; anywhere later, KIROCREW_HOME may already be unpinned when it runs"
        )


# ── driving the floor's own between-test restores ─────────────────────────


def _autouse_floor_generator(name: str):
    """The plain generator function behind one of the rootdir conftest's autouse fixtures.

    The floor tests below drive one setup -> teardown cycle of the REAL fixture code,
    which is what lets a SINGLE test observe the restore the floor performs between
    tests. An injector/observer pair cannot do that reliably: pytest-split partitions
    the collected suite into shard groups BEFORE xdist runs, so it can place the two
    halves in different CI shards regardless of any ``xdist_group`` mark, and an
    observer whose meaning depends on which test ran before it is unprovable in a
    sharded run.

    Asserts the attribute still IS an autouse fixture on the way through: the floor's
    guarantee is that it fires around every test, and a fixture demoted to a plain
    helper (or one whose ``autouse`` was dropped) would still pass a direct drive.
    """
    definition = getattr(_root, name)
    marker = getattr(definition, "_fixture_function_marker", None) or getattr(
        definition, "_pytestfixturefunction", None
    )
    assert marker is not None and marker.autouse, (
        f"conftest.{name} is not an autouse fixture, so the floor it implements is unarmed"
    )
    return getattr(definition, "__wrapped__", definition)


def _listener_thread_alive(listener: QueueListener) -> bool:
    """Whether the listener's drain thread is still running.

    ``QueueListener.stop`` joins the thread and then drops the handle, so a stopped
    listener reports ``None`` here. Checked rather than trusting the cleared slot: the
    leak this floor absorbs is the live thread and the descriptor it holds, and dropping
    the reference alone would satisfy every assertion about the slot while leaking both.
    """
    thread = getattr(listener, "_thread", None)
    return thread is not None and thread.is_alive()


class TestDynamicCredentialEnvironmentIsRestored:
    """A dynamic per-host Jira token must not leak to the next test.

    ``load_credentials`` propagates ``JIRA_TOKEN_<HEX>`` keys even though they are
    not members of the fixed ``_CREDENTIAL_KEYS`` tuple, so the floor's restore has
    to recognise the dynamic shape too — a snapshot bounded to the fixed keys would
    pass over it. The restore under test is ``conftest._no_credential_env_residue``'s
    own teardown, driven directly (see ``_autouse_floor_generator``).
    """

    def test_this_test_starts_without_the_dynamic_token(self) -> None:
        """Which is only true if no earlier test on this worker leaked one."""
        assert "JIRA_TOKEN_AABBCC" not in os.environ

    def test_an_injected_dynamic_token_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot while absent, inject, restore.

        A failure part-way cannot leak the token past this test: the live autouse
        instance of the same fixture wraps this test too, and its snapshot predates
        the injection.
        """
        cycle = _autouse_floor_generator("_no_credential_env_residue")()
        next(cycle)  # the floor's setup: snapshot, taken while the token is absent

        os.environ["JIRA_TOKEN_AABBCC"] = "token-from-this-test"

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert "JIRA_TOKEN_AABBCC" not in os.environ


class TestInheritedShellEnvironmentIsScrubbed:
    """The entries ``name_grant`` refuses as inherited preloads are hidden per test.

    On a RHEL-family host ``which2.sh`` puts ``BASH_FUNC_which%%`` in every login
    shell's environment, and ``name_grant``'s AMBIGUOUS_ENV refusal -- checked before
    every narrower code -- then rewrote what 79 unrelated assertions observed
    (issue #8395). The scrub under test is ``conftest._scrub_inherited_preload_env``,
    driven directly (see ``_autouse_floor_generator``); the domain-level regression
    lives in ``test_name_grant.py::TestInheritedHostEnvironment``, but only this
    direct drive survives ``autouse=True`` being dropped or the restore half being
    lost, because it asserts the marker and both halves of one real cycle.
    """

    def test_this_test_starts_without_the_injected_entries(self) -> None:
        """True on every host: the live scrub hides even a genuinely inherited entry."""
        assert "BASH_FUNC_kcfloor%%" not in os.environ
        assert "BASH_ENV" not in os.environ

    def test_an_inherited_entry_is_scrubbed_then_restored_by_one_cycle(self) -> None:
        """One full cycle of the real fixture: inject first, so it reads as inherited.

        The fixture records its removals on the monkeypatch instance it is handed,
        so the restore under test is that instance's ``undo`` -- driven explicitly
        here, exactly as pytest drives the shared per-test instance after every
        fixture teardown. A failure part-way cannot leak the entries past this
        test: the live autouse instance of the same fixture wraps this test too,
        and its teardown sweep removes matching entries its own snapshot never saw.
        """
        mp = pytest.MonkeyPatch()
        os.environ["BASH_FUNC_kcfloor%%"] = "() { :; }"
        os.environ["BASH_ENV"] = "/etc/kc-floor-rc"
        try:
            cycle = _autouse_floor_generator("_scrub_inherited_preload_env")(mp)
            next(cycle)  # the floor's setup: the scrub under test
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            assert "BASH_ENV" not in os.environ
            with pytest.raises(StopIteration):
                next(cycle)  # the floor's teardown sweep: inherited keys stay absent
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            mp.undo()  # the restore under test: rides the monkeypatch undo stack
            assert os.environ.get("BASH_FUNC_kcfloor%%") == "() { :; }"
            assert os.environ.get("BASH_ENV") == "/etc/kc-floor-rc"
        finally:
            mp.undo()
            os.environ.pop("BASH_FUNC_kcfloor%%", None)
            os.environ.pop("BASH_ENV", None)

    def test_an_entry_leaked_during_a_test_is_swept_by_the_floors_own_teardown(self) -> None:
        """The sweep half: a raw write that appeared mid-test does not leak on."""
        mp = pytest.MonkeyPatch()
        try:
            cycle = _autouse_floor_generator("_scrub_inherited_preload_env")(mp)
            next(cycle)  # the floor's setup: nothing inherited, nothing recorded

            os.environ["BASH_FUNC_kcfloor%%"] = "() { leaked; }"

            with pytest.raises(StopIteration):
                next(cycle)  # the floor's teardown: the sweep under test
            assert "BASH_FUNC_kcfloor%%" not in os.environ
            mp.undo()  # no record for a raw write, so the sweep's removal stands
            assert "BASH_FUNC_kcfloor%%" not in os.environ
        finally:
            mp.undo()
            os.environ.pop("BASH_FUNC_kcfloor%%", None)


# ── the logging record factory ─────────────────────────────────────────────


class TestTheLogRecordFactoryIsRestored:
    """``logging.setLogRecordFactory`` is ONE process-global slot, per worker.

    ``log_redaction``'s wrapper clears ``args`` and ``exc_info`` on every record created
    after it, so a test that leaves it installed reds whatever unrelated test later
    asserts on either field -- and because PR CI shards, the victim usually lands in a
    different process and the pollution is invisible until an unsharded release run.
    ``conftest._restore_log_record_factory`` is what removes the class; without a test,
    an edit to it reverts silently and the failures reappear in files that have nothing
    to do with the cause. Its teardown is driven directly (see
    ``_autouse_floor_generator``), so the proof needs no adjacent observer test.
    """

    def test_this_test_starts_with_the_stdlib_factory(self) -> None:
        """Which is only true if no earlier test on this worker left a wrapper installed."""
        assert logging.getLogRecordFactory() is logging.LogRecord

    def test_a_leaked_wrapper_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, install and leave installed, restore.

        The install is exactly the leak the floor absorbs in the wild — a test driving
        the real ``cli.main()`` reaches ``_setup_cli_logging``, which installs this
        wrapper and never removes it. A failure part-way cannot leak the wrapper past
        this test: the live autouse instance of the same fixture wraps this test too,
        and its snapshot predates the install.
        """
        cycle = _autouse_floor_generator("_restore_log_record_factory")()
        next(cycle)  # the floor's setup: snapshot, taken while the stdlib factory holds

        install_log_redaction([])
        assert logging.getLogRecordFactory() is not logging.LogRecord

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert logging.getLogRecordFactory() is logging.LogRecord, (
            f"the record factory is still {logging.getLogRecordFactory()!r} -- a wrapper "
            "left installed by a test would rewrite every later record on its worker"
        )

    def test_the_restore_target_is_what_the_test_inherited_not_the_stdlib(self) -> None:
        """The floor restores the INHERITED factory, so a higher-scoped installer survives.

        Pinned because it is the documented reason the floor is a fixture rather than a
        ``pytest_runtest_setup`` hookimpl: a class- or module-scoped fixture that installs
        a factory for its whole scope must not have it torn out after the first test.
        """
        sentinel_calls: list[str] = []

        def sentinel(*args: object, **kwargs: object) -> logging.LogRecord:
            sentinel_calls.append("made")
            return logging.LogRecord(*args, **kwargs)  # type: ignore[arg-type]

        before = logging.getLogRecordFactory()
        logging.setLogRecordFactory(sentinel)
        try:
            cycle = _autouse_floor_generator("_restore_log_record_factory")()
            next(cycle)  # snapshot taken while the sentinel is installed
            install_log_redaction([])
            with pytest.raises(StopIteration):
                next(cycle)
            assert logging.getLogRecordFactory() is sentinel, (
                "the floor restored past the factory this cycle inherited, so a "
                "higher-scoped installer would be torn out after its first test"
            )
        finally:
            logging.setLogRecordFactory(before)


# ── logger levels ──────────────────────────────────────────────────────────


class TestLoggerLevelsAreRestored:
    """A logger's level is PROCESS-GLOBAL, per worker, and HIERARCHICAL.

    Together those are what make this leak class so hard to attribute. ``Logger.debug``
    gates on the EFFECTIVE level, so an explicit level left on ``kiro_crew`` decides what
    every ``kiro_crew.*`` logger in the worker may emit, and it outranks the root level
    ``caplog.at_level()`` sets -- the victim gets ``caplog.text == ""``, nothing at all
    rather than the wrong text, from a test that passes alone. The suite reaches this
    through ``cli._setup_cli_logging``, which pins ``kiro_crew`` at WARNING, and which
    test modules across the suite run for real by driving ``cli.main()`` in process.

    ``conftest._restore_logger_levels`` is what removes the class; without a test, an edit
    to it reverts silently and the failures reappear in files that have nothing to do with
    the cause. Its teardown is driven directly (see ``_autouse_floor_generator``), so the
    proof needs no adjacent observer test.
    """

    def test_this_test_starts_with_an_unconfigured_kiro_crew_logger(self) -> None:
        """Which is only true if no earlier test on this worker left a level on it."""
        assert logging.getLogger("kiro_crew").level == logging.NOTSET

    def test_a_leaked_level_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, pin a level and leave it, restore.

        The pin is exactly the leak the floor absorbs in the wild — ``_setup_cli_logging``
        sets ``kiro_crew`` to WARNING once per process and never undoes it. A failure
        part-way cannot leak the level past this test: the live autouse instance of the
        same fixture wraps this test too, and its snapshot predates the pin.
        """
        cycle = _autouse_floor_generator("_restore_logger_levels")()
        next(cycle)  # the floor's setup: snapshot, taken while the level is NOTSET

        logging.getLogger("kiro_crew").setLevel(logging.WARNING)
        assert logging.getLogger("kiro_crew").level == logging.WARNING

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        level = logging.getLogger("kiro_crew").level
        assert level == logging.NOTSET, (
            f"kiro_crew is still pinned at {logging.getLevelName(level)} -- every "
            "kiro_crew.* record below that level would be dropped before it reaches "
            "the root handler caplog captures through"
        )

    def test_a_logger_created_during_the_test_is_restored_to_pristine(self) -> None:
        """The gap a plain snapshot cannot see: a logger that did not exist at setup.

        The floor restores a name missing from its "before" snapshot to the pristine
        ``(NOTSET, enabled)`` state rather than passing over it, and this is the only
        place that behaviour is exercised with a genuinely fresh name.
        """
        name = "kiro_crew._floor_probe_6351"
        assert name not in logging.Logger.manager.loggerDict

        cycle = _autouse_floor_generator("_restore_logger_levels")()
        next(cycle)  # snapshot taken while the logger does not exist

        probe = logging.getLogger(name)
        probe.setLevel(logging.CRITICAL)
        probe.disabled = True

        with pytest.raises(StopIteration):
            next(cycle)
        assert probe.level == logging.NOTSET and probe.disabled is False, (
            "a logger created mid-test kept its configuration -- the floor's "
            "missing-from-snapshot branch no longer restores to pristine"
        )

    def test_a_debug_record_from_a_kiro_crew_logger_still_reaches_caplog(self, caplog) -> None:
        """The capability the level restore exists to protect, asserted directly.

        The level assertion above pins the mechanism; this pins the OUTCOME, in the exact
        shape the victim test uses -- ``at_level`` on the root logger, a ``debug`` call on
        a ``kiro_crew.*`` child -- so a future floor that restores something subtly
        different still has to keep this working.
        """
        with caplog.at_level("DEBUG"):
            logging.getLogger("kiro_crew.slack.gateway").debug("floor canary")
        assert "floor canary" in caplog.text


# ── the CLI log queue listener ─────────────────────────────────────────────


class TestTheLogQueueListenerIsRestored:
    """``cli._LOG_QUEUE_LISTENER`` is ONE process-global slot, per worker.

    ``_setup_cli_logging`` starts a ``QueueListener`` for a LONG-LIVED command and never
    stops it -- correct in production, which does it once per process -- so every test
    that drives the real ``cli.main()`` for ``serve`` / ``gateway`` / ``chat`` leaves one
    running. The SHORT-LIVED branch then reads it: it takes the ``else`` path and does not
    touch the global, so a test asserting a short-lived verb starts no listener sees the
    PREVIOUS test's and fails on its own first line, in a file that cleans up after itself
    correctly. Two tests in ``test_cli_logging.py`` assert exactly that, and under
    ``-n auto --dist loadgroup`` whether a leaker precedes them on the worker varies run
    to run, so it surfaces as an intermittent failure rather than an ordering bug.

    ``conftest._restore_log_queue_listener`` is what removes the class; without a test, an
    edit to it reverts silently and the failures reappear as a flake. Its teardown is
    driven directly (see ``_autouse_floor_generator``), so the proof needs no adjacent
    observer test.
    """

    @pytest.fixture(autouse=True)
    def _isolated_cli_logging(self, monkeypatch):
        """Foreground mode, and remove the handlers these tests' real setup calls attach.

        Driving the real ``_setup_cli_logging`` is the point -- a test that assigned the
        global instead would pass even if the production install site moved -- but it also
        attaches a handler holding an open descriptor, and the floor under test
        deliberately does not restore handlers. Removing only what this test ADDED is what
        ``_pristine_logging`` does; the snapshotted list is never written back, because
        ``caplog`` swaps a handler on the root logger at every phase boundary and writing
        a setup-phase list back during teardown would drop the one it is capturing
        through.
        """
        monkeypatch.setattr("kiro_crew.cli._fd_targets_file", lambda fd, path: False)
        loggers = (logging.getLogger(), logging.getLogger("kiro_crew"))
        saved = [(lgr, lgr.handlers[:]) for lgr in loggers]
        yield
        for lgr, handlers in saved:
            for handler in lgr.handlers[:]:
                if handler not in handlers:
                    lgr.removeHandler(handler)
                    handler.close()

    def test_this_test_starts_with_no_listener(self) -> None:
        """Which is only true if no earlier test on this worker left one running."""
        assert cli._LOG_QUEUE_LISTENER is None

    def test_a_leaked_listener_is_removed_by_the_floors_own_teardown(self) -> None:
        """One full cycle of the real fixture: snapshot, leak a listener, restore.

        The leak is produced by the real ``_setup_cli_logging`` on the real long-lived
        branch, which is exactly how it happens in the wild, so the test still fails if
        the production install site moves. A failure part-way cannot leak the listener
        past this test: the live autouse instance of the same fixture wraps this test too,
        and its snapshot predates the install.
        """
        cycle = _autouse_floor_generator("_restore_log_queue_listener")()
        next(cycle)  # the floor's setup: snapshot, taken while the slot is empty

        cli._setup_cli_logging("gateway", 1)
        leaked = cli._LOG_QUEUE_LISTENER
        assert leaked is not None, "the long-lived branch no longer starts a listener"

        with pytest.raises(StopIteration):
            next(cycle)  # the floor's teardown: the restore under test
        assert cli._LOG_QUEUE_LISTENER is None, (
            f"the slot still holds {cli._LOG_QUEUE_LISTENER!r} -- the next test to run "
            "_setup_cli_logging for a SHORT-LIVED command would read this listener and "
            "fail asserting it started none"
        )
        assert not _listener_thread_alive(leaked), (
            "the listener object was dropped but its thread is still running -- it holds "
            "the file handler's descriptor open on a gateway.log under a tmp_path the "
            "next test deletes"
        )

    def test_the_restore_target_is_what_the_test_inherited(self, monkeypatch) -> None:
        """The floor restores the INHERITED listener, so a higher-scoped installer survives.

        Same reason the record-factory floor restores to its snapshot: a class- or
        module-scoped fixture that starts a listener for its whole scope must not have it
        torn out after the first test.
        """
        sentinel = QueueListener(queue.SimpleQueue())
        monkeypatch.setattr(cli, "_LOG_QUEUE_LISTENER", sentinel)

        cycle = _autouse_floor_generator("_restore_log_queue_listener")()
        next(cycle)  # snapshot taken while the sentinel holds the slot

        cli._setup_cli_logging("gateway", 1)
        assert cli._LOG_QUEUE_LISTENER is not sentinel

        with pytest.raises(StopIteration):
            next(cycle)
        assert cli._LOG_QUEUE_LISTENER is sentinel, (
            "the floor restored past the listener this cycle inherited, so a "
            "higher-scoped installer would be torn out after its first test"
        )

    def test_a_short_lived_command_leaves_the_slot_alone(self, monkeypatch) -> None:
        """The production behaviour the floor exists to accommodate, pinned at source.

        ``_setup_cli_logging`` deliberately does not clear the global on the short-lived
        branch -- a listener a long-lived command started genuinely still exists -- which
        is why the leak is absorbed in the test seam rather than by clearing it there. If
        this ever changes, the floor becomes redundant rather than wrong, and this test is
        what says so.
        """
        sentinel = QueueListener(queue.SimpleQueue())
        monkeypatch.setattr(cli, "_LOG_QUEUE_LISTENER", sentinel)

        cli._setup_cli_logging("status", 1)

        assert cli._LOG_QUEUE_LISTENER is sentinel


# ── the worker budget ─────────────────────────────────────────────────────


class TestTheWorkerBudgetIsMemoryBounded:
    """How many xdist workers the host can actually back, not just how many cores.

    Cores alone oversubscribe: two worktrees each taking 10 workers on a 10-core box
    once produced a load average of ~590 with zero tests completing in 21 minutes. But
    TOTAL RAM alone is also the wrong number -- it is the MACHINE's, so it over-reports
    inside a memory-capped container and says nothing about a host already using most
    of its memory for something else.

    The failure mode that matters most here is the inverted one: a reading that comes
    back wrongly SMALL collapses the whole run to one worker, which looks like a hang
    rather than a bug. So each reading must degrade to "skip this bound", never to zero.
    """

    def test_the_budget_is_registered_from_the_rootdir_not_from_test_conftest(self) -> None:
        """The gap that was silent for every testpath but ``test/``.

        ``test/conftest.py`` is not loaded when the target is ``transfer`` or an
        in-package app suite, so a budget registered there resolved ``-n auto`` to
        the raw core count for those invocations -- and ignored every knob, with
        nothing in the output to say so, because an absent budget is
        indistinguishable from a budget that chose not to clamp.

        Asserted against the SOURCE of both files, so it holds however the run was
        invoked: a test that inspected only the live plugin manager would pass from
        ``test/`` even after a regression put the hook back in the wrong file.
        """
        root_conftest = (_REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
        suite_conftest = (_REPO_ROOT / "test" / "conftest.py").read_text(encoding="utf-8")
        hook = "def pytest_xdist_auto_num_workers"

        assert hook in root_conftest, (
            "the worker budget must be registered from the ROOTDIR conftest, which is "
            "the only one every testpath loads"
        )
        assert hook not in suite_conftest, (
            "a budget registered in test/conftest.py is absent from transfer/ and from "
            "the in-package app suites, and the absence is silent"
        )

    def test_the_budget_module_is_importable_from_every_testpath(self) -> None:
        """The repository root is on ``sys.path`` for every invocation shape.

        That is what lets one module serve both conftests. It holds because pytest
        imports the rootdir conftest in ``prepend`` mode -- but it is load-bearing
        and invisible, so it is pinned rather than assumed.
        """
        import xdist_budget

        assert pathlib.Path(xdist_budget.__file__).parent == _REPO_ROOT
        assert callable(xdist_budget.resolve_workers)
        assert callable(xdist_budget.release_worker_slots)

    @pytest.fixture
    def slot_dir_env(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
        """Point the host-global slot directory at a tmp dir, and release what is taken.

        Mirrors ``test_xdist_host_budget.py``'s ``slot_dir``, and both halves matter. The
        real slot directory is under ``~/.cache``, shared with every other run on the
        machine, so a test that claims slots must not compete there. And a claim HOLDS an
        open file descriptor for the process lifetime by design -- that is how the kernel
        owns the lease -- so the descriptors have to be closed here or the test keeps real
        capacity for the rest of the session, and on Windows the open handle also blocks
        ``tmp_path`` cleanup and fails teardown.

        ``_held_slots`` is REPLACED rather than cleared, so the suite's own list is never
        touched and ``monkeypatch`` restores it even if the test fails.
        """
        import xdist_budget as budget

        monkeypatch.setenv(budget._SLOT_DIR_ENV, str(tmp_path / "slots"))
        held: list[int] = []
        monkeypatch.setattr(budget, "_held_slots", held)
        yield tmp_path
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_every_reading_is_optional_and_the_tightest_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 64)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 8 * 1024)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        # 8 GiB ceiling at 2 GiB/worker is the tightest real reading, and the
        # unavailable one (0) is skipped rather than read as "no memory".
        assert budget._static_memory_bounded_capacity(32) == 4

    def test_a_starved_host_is_bounded_rather_than_read_as_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The inversion this unit choice exists to prevent.

        In whole GiB, 860 MiB free truncates to 0 -- indistinguishable from "could not
        determine", which is SKIPPED. The live bound would therefore drop out on exactly
        the starved host it protects, leaving the static total-RAM term to allow 16
        workers on under a gigabyte of free memory.
        """
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_available_mib", lambda: 860)

        assert budget._live_memory_bounded_cap(32) == 1

    def test_a_small_container_ceiling_is_not_read_as_no_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same inversion, reached through the cgroup reading instead."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 256)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 512)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        assert budget._static_memory_bounded_capacity(32) == 1

    def test_the_static_bound_shapes_the_shared_range_not_just_this_run(
        self, monkeypatch: pytest.MonkeyPatch, slot_dir_env: pathlib.Path
    ) -> None:
        """The memory budget is SHARED between concurrent runs, not granted to each.

        This is the property that decides where each bound goes. A 64-core / 32 GiB host
        can back 16 workers, so there must be 16 SLOTS in total -- a first run takes them
        all and a second gets its floor. Put the static bound only on the per-run cap and
        both runs take 16 each: 32 workers against a 16-worker budget, which is the
        swapping incident the budget exists to prevent, reached from the other end.
        """
        import xdist_budget as budget

        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(budget, "_host_total_gib", lambda: 32)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)
        monkeypatch.delenv(budget._MAX_WORKERS_ENV, raising=False)
        # Kiro Crew seeds this cap at every agent spawn boundary; a run started
        # from an agent shell would otherwise read the spawner's cap here.
        monkeypatch.delenv(budget._XDIST_ENV_CAP, raising=False)

        first = budget.resolve_workers()
        # A second run in this same process cannot re-lock what it already holds, so the
        # slot RANGE is what the assertion has to pin: 16, never 64.
        assert first == 16
        assert budget._static_memory_bounded_capacity(64) == 16

    def test_the_live_bound_does_not_shrink_the_shared_range(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half: a transient reading must not reshape the namespace.

        Slots fill from index 0, so a range shortened by a momentary dip excludes exactly
        the slots an earlier run left free -- collapsing the later run while the machine
        idles.
        """
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 128)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 2048)

        assert budget._static_memory_bounded_capacity(32) == 32

    def test_an_unavailable_reading_never_collapses_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS has no /proc/meminfo and no /sys/fs/cgroup; Windows raises on
        ``os.sysconf``. All three readings returning 0 must leave the core count
        standing."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 0)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 0)

        assert budget._static_memory_bounded_capacity(12) == 12

    def test_a_tiny_host_still_gets_one_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slow beats stalled: the floor is one worker, never zero."""
        import xdist_budget as budget

        monkeypatch.setattr(budget, "_host_total_gib", lambda: 1)
        monkeypatch.setattr(budget, "_cgroup_limit_mib", lambda: 0)
        monkeypatch.setattr(budget, "_host_available_mib", lambda: 1024)

        assert budget._static_memory_bounded_capacity(8) == 1

    @pytest.mark.parametrize(
        ("kb", "expected"),
        [
            (99_328_704, 97_000),  # a large host, in whole MiB
            (8_388_608, 8192),
            (1_048_576, 1024),  # exactly 1 GiB
            (900_000, 878),  # under 1 GiB: a REAL reading, not the unknown sentinel
            (500, 0),  # half a MiB: genuinely below the resolution, reads as unknown
        ],
    )
    def test_meminfo_is_parsed_into_whole_mib(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, kb: int, expected: int
    ) -> None:
        """Parsing is asserted against a FIXTURE, never against the live host.

        ``assert available > 0`` on the real reading looks like a smoke test and is
        actually the wall-clock-race flake class applied to memory: the value truncates
        to whole GiB, so any host with under 1 GiB free returns 0 -- which is the
        function's own "could not determine" sentinel, i.e. a CORRECT return that the
        assertion would call a failure. Small CI containers are the most exposed.

        The platform is pinned for the same reason the reading is: this exercises the
        ``/proc/meminfo`` branch specifically, and reaching it must not depend on the
        host the test happens to run on -- otherwise the whole parametrization silently
        stops asserting anything on the macOS and Windows shards.
        """
        import xdist_budget as budget
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        meminfo = tmp_path / "meminfo"
        meminfo.write_text(
            f"MemTotal:       131549320 kB\nMemAvailable:   {kb} kB\nBuffers: 1 kB\n",
            encoding="utf-8",
        )
        real_open = open

        def _fake(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                return real_open(meminfo, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake)

        assert budget._host_available_mib() == expected

    def test_available_never_exceeds_total_on_a_real_host(self) -> None:
        """The one invariant that holds at ANY memory level, so it cannot flake.

        Gated on the READING rather than on ``/proc/meminfo``, so it runs on macOS
        and Windows too. Gating it on the Linux file is what kept the only
        real-host assertion here from ever executing on the platforms whose
        readings were added last.
        """
        import xdist_budget as budget

        available = budget._host_available_mib()
        if available == 0:
            pytest.skip("no available-memory reading on this platform")
        assert available <= budget._host_total_gib() * 1024

    def test_a_missing_meminfo_is_reported_as_unknown_not_as_zero_memory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """On Linux a missing ``/proc/meminfo`` is unknown, never zero memory.

        Zero would be indistinguishable from a genuinely starved host, and the
        two must diverge: ``_bounded_by`` SKIPS an unknown reading and would
        otherwise collapse every run to a single worker.
        """
        import xdist_budget as budget

        # Imported in-body, not at module scope: this file's own ratchet asserts
        # that no import-time ``~/.kiro`` binding escapes the isolation fixtures,
        # and importing kiro_crew during COLLECTION binds them against the real
        # home before any fixture has run.
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        real_open = open

        def _no_meminfo(path, *args, **kwargs):
            if str(path) == "/proc/meminfo":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _no_meminfo)

        assert budget._host_available_mib() == 0

    def test_a_platform_with_no_reading_at_all_is_unknown_not_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform none of the three branches claims returns 0 = unknown.

        The branch that used to stand in for macOS and Windows. Those now have
        readings of their own, so this covers only the genuinely unknown host --
        and it must stay 0 so such a host keeps its parallelism instead of
        silently dropping to one worker.
        """
        import xdist_budget as budget
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

        assert budget._host_available_mib() == 0

    def test_an_unlimited_cgroup_is_not_read_as_a_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """cgroup v2 spells "no limit" as the literal ``max``.

        v1 instead uses a huge sentinel (0x7FFFFFFFFFFFF000), which needs no special
        case: divided into GiB it is a number no ``min()`` will ever pick. Both are
        exercised here because the two files are read in the same loop.
        """
        import xdist_budget as budget

        limit_file = tmp_path / "memory.max"
        limit_file.write_text("max\n", encoding="utf-8")
        v1_file = tmp_path / "memory.limit_in_bytes"
        v1_file.write_text(f"{0x7FFFFFFFFFFFF000}\n", encoding="utf-8")

        real_open = open

        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                return real_open(v1_file, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        # "max" is skipped outright; the v1 sentinel converts to a ceiling far above
        # any real core count, so neither can bind.
        assert budget._static_memory_bounded_capacity(8) <= 8
        assert budget._cgroup_limit_mib() >= 8 * 1024

    def test_a_real_cgroup_ceiling_binds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
    ) -> None:
        """The container case: 8 GiB inside a cgroup on a 256 GiB machine."""
        import xdist_budget as budget

        limit_file = tmp_path / "memory.max"
        limit_file.write_text(str(8 * 1024**3), encoding="utf-8")
        real_open = open

        # Falls THROUGH for every other path rather than raising. `builtins.open` is
        # shared by every thread in this worker, and the SEL writer is a session-lived
        # daemon thread that opens a file on each flush -- a blanket raise would hand it
        # FileNotFoundError for a path that exists, and could kill the writer so that a
        # later, unrelated SEL test on this worker fails for a reason it cannot see.
        def _fake_cgroup(path, *args, **kwargs):
            if str(path) == "/sys/fs/cgroup/memory.max":
                return real_open(limit_file, *args, **kwargs)
            if str(path) == "/sys/fs/cgroup/memory/memory.limit_in_bytes":
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr("builtins.open", _fake_cgroup)

        assert budget._cgroup_limit_mib() == 8 * 1024
