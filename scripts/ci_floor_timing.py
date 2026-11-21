"""MEASUREMENT ONLY: per-worker timing of the per-test floor. Not for merge.

``scripts.ci_pytest_progress`` enables it when ``KIROCREW_FLOOR_TIMING`` names the
``--ci-progress-dir`` in use. It writes one
``floor-timing-<worker>-<pid>.jsonl`` beside the progress files, holding one
summary record per worker:

* time in each floor guard and each function-scoped fixture, setup and teardown apart;
* for ``tmp_path``: creating the numbered directory, the ``current`` symlink attempt
  inside that, and removing the directory;
* count and time of the environment writes made through ``MonkeyPatch.setenv`` /
  ``delenv`` while a floor is running, and of ``MonkeyPatch.undo``;
* time in the setup and teardown phases outside fixtures;
* how many environment variables, loggers and modules the worker holds;
* what the instrumentation itself costs, from a calibration at the end.

It only reads clocks. Every wrapper calls what it wraps with the same arguments and
lets its result or exception through unchanged; a failure in the bookkeeping is
counted in ``plugin_errors`` and never raised.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import sys
import time
from collections import defaultdict

import pytest

_ENV = "KIROCREW_FLOOR_TIMING"
_clock = time.perf_counter


class _Timing:
    def __init__(self, config, directory):
        self.config = config
        self.directory = directory
        self.worker = getattr(config, "workerinput", {}).get("workerid", "controller")
        self.errors = 0
        self.tests = 0
        #: component -> [setup seconds, teardown seconds, calls, longest single setup];
        #: the longest setup is usually a first import, to be read apart from the rest.
        self.all: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0, 0.0])
        self.bare: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0, 0.0])
        self.counts = {"all": 0, "bare": 0, "tmp_path": 0, "async": 0}
        self.phase = {"all": defaultdict(float), "bare": defaultdict(float)}
        self.is_bare = False
        self.in_floor = 0
        self.fixture_stack: list[float] = []
        self.wrapped_calls = 0
        self.wrappers: dict[int, object] = {}
        self.restore: list[tuple[object, str, object]] = []
        self.guards_installed = False
        self.finished = False

    # -- bookkeeping -----------------------------------------------------------------

    def add(self, name: str, slot: int, seconds: float, count: bool = True) -> None:
        self.wrapped_calls += 1
        for row in (self.all[name], self.bare[name]) if self.is_bare else (self.all[name],):
            row[slot] += seconds
            row[2] += count
            if slot == 0 and seconds > row[3]:
                row[3] = seconds

    def swap(self, owner, name: str, replacement) -> None:
        self.restore.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    # -- guards ----------------------------------------------------------------------

    def timed_guard(self, guard):
        cached = self.wrappers.get(id(guard))
        if cached is not None:
            return cached
        name = "guard:" + guard.__name__
        timing = self

        def wrapper(*args):
            timing.in_floor += 1
            started = _clock()
            try:
                cycle = guard(*args)
            finally:
                timing.in_floor -= 1
                timing.add(name, 0, _clock() - started)
            if not inspect.isgenerator(cycle):
                return cycle
            return timed_cycle(cycle)

        def timed_cycle(cycle):
            # A transparent proxy: yields what the guard yields, as often as it yields,
            # and lets whatever it raises through.
            slot = 0
            while True:
                timing.in_floor += 1
                started = _clock()
                try:
                    value = next(cycle)
                except StopIteration:
                    return
                finally:
                    timing.in_floor -= 1
                    timing.add(name, slot, _clock() - started, count=False)
                slot = 1
                yield value

        wrapper.__name__ = guard.__name__
        wrapper.__qualname__ = getattr(guard, "__qualname__", guard.__name__)
        wrapper.__per_test_guard__ = guard.__per_test_guard__
        self.wrappers[id(guard)] = wrapper
        return wrapper

    def install_guard_timing(self) -> None:
        module = sys.modules.get("per_test_guards")
        if module is None or self.guards_installed:
            return
        self.guards_installed = True
        run_guards = module.run_guards
        timing = self

        def timed_run_guards(guards, request, yielded, **fixtures):
            try:
                guards = tuple(timing.timed_guard(guard) for guard in guards)
            except Exception:  # noqa: BLE001 - bookkeeping must never fail a test
                timing.errors += 1
            return run_guards(guards, request, yielded, **fixtures)

        self.swap(module, "run_guards", timed_run_guards)

    # -- fixtures --------------------------------------------------------------------

    def install_fixture_timing(self) -> None:
        from _pytest import fixtures

        finish = fixtures.FixtureDef.finish
        timing = self

        def timed_finish(fixturedef, request):
            function_scoped = getattr(fixturedef, "scope", None) == "function"
            had_work = bool(getattr(fixturedef, "_finalizers", None)) or (
                getattr(fixturedef, "cached_result", None) is not None
            )
            timing.fixture_stack.append(0.0)
            timing.in_floor += 1
            started = _clock()
            try:
                return finish(fixturedef, request)
            finally:
                elapsed = _clock() - started
                timing.in_floor -= 1
                inner = timing.fixture_stack.pop()
                if had_work:
                    name = (
                        "fixture:" + fixturedef.argname
                        if function_scoped
                        else "wider:" + str(getattr(fixturedef, "scope", "?"))
                    )
                    timing.add(name, 1, elapsed - inner, count=False)
                if timing.fixture_stack:
                    timing.fixture_stack[-1] += elapsed

        self.swap(fixtures.FixtureDef, "finish", timed_finish)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        self.in_floor += 1
        started = _clock()
        try:
            yield
        finally:
            self.in_floor -= 1
            scope = fixturedef.scope
            name = "fixture:" + fixturedef.argname if scope == "function" else "wider:" + scope
            self.add(name, 0, _clock() - started)

    # -- tmp_path and the environment --------------------------------------------------

    def timed(self, function, name: str, slot: int = 0, only_in_floor: bool = False):
        timing = self

        def wrapper(*args, **kwargs):
            if only_in_floor and not timing.in_floor:
                return function(*args, **kwargs)
            started = _clock()
            try:
                return function(*args, **kwargs)
            finally:
                timing.add(name, slot, _clock() - started)

        try:
            functools.update_wrapper(wrapper, function)
        except Exception:  # noqa: BLE001 - metadata only
            self.errors += 1
        return wrapper

    def install_step_timing(self) -> None:
        from _pytest import monkeypatch, pathlib, tmpdir

        self.swap(
            tmpdir, "make_numbered_dir", self.timed(tmpdir.make_numbered_dir, "tmp_path:create")
        )
        self.swap(pathlib, "_force_symlink", self.timed(pathlib._force_symlink, "tmp_path:symlink"))
        self.swap(tmpdir, "rmtree", self.timed(tmpdir.rmtree, "tmp_path:remove", slot=1))
        patcher = monkeypatch.MonkeyPatch
        self.swap(patcher, "setenv", self.timed(patcher.setenv, "env:setenv", only_in_floor=True))
        self.swap(patcher, "delenv", self.timed(patcher.delenv, "env:delenv", only_in_floor=True))
        self.swap(patcher, "undo", self.timed(patcher.undo, "monkeypatch:undo", slot=1))

    def pytest_collection_finish(self, session):
        # The conftests that define the guard runner are loaded by now, whatever
        # order pytest configured the plugins in.
        try:
            self.install_guard_timing()
        except Exception:  # noqa: BLE001 - guards then go untimed
            self.errors += 1

    # -- phases ----------------------------------------------------------------------

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        try:
            info = getattr(item, "_fixtureinfo", None)
            argnames = tuple(getattr(info, "argnames", ()) or ())
            is_async = inspect.iscoroutinefunction(getattr(item, "obj", None))
            self.is_bare = (
                not argnames and not is_async and item.get_closest_marker("usefixtures") is None
            )
            self.counts["all"] += 1
            self.counts["bare"] += self.is_bare
            self.counts["tmp_path"] += "tmp_path" in getattr(item, "fixturenames", ())
            self.counts["async"] += is_async
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a test
            self.errors += 1
        started = _clock()
        try:
            yield
        finally:
            self._phase("setup", _clock() - started)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        started = _clock()
        try:
            yield
        finally:
            self._phase("teardown", _clock() - started)
            self.is_bare = False

    def _phase(self, name: str, seconds: float) -> None:
        self.phase["all"][name] += seconds
        if self.is_bare:
            self.phase["bare"][name] += seconds

    # -- output ----------------------------------------------------------------------

    def calibrate(self) -> dict:
        """Seconds one wrapped call adds, measured on wrappers around functions that do nothing."""

        def nothing():
            return None

        def nothing_guard():
            yield

        nothing_guard.__per_test_guard__ = ()
        rounds = 20000
        plain = self.timed(nothing, "calibration:plain")
        started = _clock()
        for _ in range(rounds):
            nothing()
        bare_call = (_clock() - started) / rounds
        started = _clock()
        for _ in range(rounds):
            plain()
        wrapped_call = (_clock() - started) / rounds
        guard = self.timed_guard(nothing_guard)
        started = _clock()
        for _ in range(rounds):
            cycle = guard()
            next(cycle)
            next(cycle, None)
        wrapped_guard = (_clock() - started) / rounds
        started = _clock()
        for _ in range(rounds):
            cycle = nothing_guard()
            next(cycle)
            next(cycle, None)
        bare_guard = (_clock() - started) / rounds
        for name in ("calibration:plain", "guard:nothing_guard"):
            self.all.pop(name, None)
            self.bare.pop(name, None)
        return {
            "per_timed_call_s": wrapped_call - bare_call,
            "per_guard_cycle_s": wrapped_guard - bare_guard,
        }

    def write(self) -> None:
        wrapped_calls = self.wrapped_calls
        calibration = self.calibrate()
        record = {
            "event": "floor_timing",
            "worker": self.worker,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "tests": self.counts,
            "phase_seconds": {scope: dict(values) for scope, values in self.phase.items()},
            "components_all": {name: row for name, row in self.all.items()},
            "components_bare": {name: row for name, row in self.bare.items()},
            "environment_variables": len(os.environ),
            "loggers": sum(
                isinstance(obj, logging.Logger)
                for obj in list(logging.Logger.manager.loggerDict.values())
            ),
            "modules": len(sys.modules),
            "kiro_crew_modules": sum(name.startswith("kiro_crew") for name in list(sys.modules)),
            "wrapped_calls": wrapped_calls,
            "calibration": calibration,
            "guards_timed": self.guards_installed,
            "plugin_errors": self.errors,
        }
        try:
            factory = getattr(self.config, "_tmp_path_factory", None)
            basetemp = getattr(factory, "_basetemp", None)
            if basetemp is not None:
                record["basetemp_entries"] = len(os.listdir(basetemp))
        except OSError:
            pass
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"floor-timing-{self.worker}-{os.getpid()}.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")

    def close(self) -> None:
        for owner, name, original in reversed(self.restore):
            try:
                setattr(owner, name, original)
            except Exception:  # noqa: BLE001 - nothing to do about it at exit
                self.errors += 1
        self.restore.clear()


def _asked_for(directory) -> bool:
    """Whether the switch is on for *directory*.

    The switch must name the directory itself, so a nested pytest a test starts with
    its own progress directory inherits the variable and stays untimed.
    """
    value = os.environ.get(_ENV, "")
    if not value:
        return False
    try:
        return os.path.normcase(os.path.abspath(value)) == os.path.normcase(
            os.path.abspath(str(directory))
        )
    except (OSError, ValueError):
        return False


def enable_for(config, directory) -> None:
    """Register the timing for this process if the switch asks for it. Never raises."""
    try:
        if not _asked_for(directory) or config.pluginmanager.get_plugin("ci-floor-timing"):
            return
        if not hasattr(config, "workerinput") and getattr(config.option, "numprocesses", 0):
            return  # the xdist controller runs no test
        timing = _Timing(config, directory)
        config.pluginmanager.register(timing, "ci-floor-timing")
        config.add_cleanup(lambda: _finish(config))
    except Exception:  # noqa: BLE001 - the measurement must never fail a run
        return
    for install in (
        timing.install_guard_timing,
        timing.install_fixture_timing,
        timing.install_step_timing,
    ):
        try:
            install()
        except Exception:  # noqa: BLE001 - an install that fails leaves that part untimed
            timing.errors += 1


def pytest_configure(config):
    directory = config.getoption("ci_progress_dir", None)
    if directory:
        from pathlib import Path

        enable_for(config, Path(directory))


def _finish(config) -> None:
    timing = config.pluginmanager.get_plugin("ci-floor-timing")
    if timing is None or timing.finished:
        return
    timing.finished = True
    try:
        timing.write()
    except Exception:  # noqa: BLE001 - a missing summary must not fail the session
        pass
    timing.close()


def pytest_sessionfinish(session):
    _finish(session.config)
