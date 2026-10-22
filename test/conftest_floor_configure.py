"""Process-wide redirects ``pytest_configure`` installs once per run (the
hypothesis database, bytecode, child ``PYTHONPATH``, the pytest-asyncio scan gate, the
crew-log switch), and the hook that trims every recorded warning.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import functools
import os
import pathlib
import sys
import warnings

import pytest

# ── Hypothesis example database (rootdir floor) ─────────────────────────────
# ``test/conftest.py`` registers the "default"/"thorough" profiles without a
# ``database=`` (its derandomized "ci" profile sets ``database=None`` and is left
# alone below), so hypothesis falls back to its own default: ``.hypothesis/examples``
# resolved against the CURRENT WORKING DIRECTORY, i.e. the repo root, for every test
# collected from ANY testpath -- including the ~108 modules under
# ``src/kiro_crew/apps/builtins/*/tests/`` that never import ``test/conftest.py`` at all.
# That writes an untracked directory into the checkout on every run (git status shows it
# under "Ignored files" since ``.gitignore`` covers it, but it still needs a place to live
# that is not the source tree).
#
# Profiles are process-global, so whichever profile is active when a ``@given`` test runs
# applies regardless of which testpath collected it. Registering a "default" profile here
# at module level would not stick: ``test/conftest.py`` re-registers "default" by name
# right after this module loads (pytest imports the rootdir conftest first, then
# ``test/conftest.py`` -- see ``pytest_load_initial_conftests``), and ``register_profile``
# without ``parent=`` builds a fresh settings object from scratch, silently resetting
# ``database`` back to the on-disk default. Doing the redirect from ``pytest_configure``
# instead runs after both conftests' module-level code, so it lands last: ``parent=`` keeps
# whatever ``max_examples``/``deadline``/health-check suppression ``test/conftest.py`` set,
# and only ``database`` is overridden.
_HYPOTHESIS_DB_DIR: str | None = None


def _redirect_hypothesis_database() -> None:
    """Point the active hypothesis profile's example database off the checkout.

    Uses a stable per-user directory under the platform cache root
    (``$XDG_CACHE_HOME`` or ``~/.cache``, ``kirocrew/hypothesis``) rather than
    ``database=None`` or a per-run temp dir: the shrunk counterexample hypothesis
    saves is what makes a property-test failure replay on the NEXT run instead of
    costing a full re-search, so the database has to outlive the process. What
    moves is only WHERE it lives -- out of the checkout and into the same cache
    tree the xdist slot files already use -- not whether it persists. Tolerates
    hypothesis being absent (a partial checkout, or an environment where it was
    never installed), an unwritable cache root (falls back to ``database=None``
    rather than to the checkout), and no profile named "default" existing yet,
    since this floor must not be the reason collection fails for a suite that
    does not use hypothesis at all.
    """
    global _HYPOTHESIS_DB_DIR
    try:
        from hypothesis import settings as _hyp_settings
        from hypothesis.database import DirectoryBasedExampleDatabase
    except ImportError:
        return
    try:
        _current = _hyp_settings.get_profile(_hyp_settings._current_profile)
    except Exception:  # noqa: BLE001 - no active profile to inherit from
        return
    if _HYPOTHESIS_DB_DIR is None:
        cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache"
        )
        candidate = os.path.join(cache_root, "kirocrew", "hypothesis")
        try:
            os.makedirs(candidate, exist_ok=True)
            _HYPOTHESIS_DB_DIR = candidate
        except OSError:
            _HYPOTHESIS_DB_DIR = ""
        # The example database is only one tenant of ``.hypothesis/``: the
        # unicode-data cache (``storage_directory("unicode_data", ...)``) is written
        # through hypothesis's HOME directory, which defaults to ``.hypothesis``
        # under the CWD, i.e. the checkout. Move the home too, so the whole tree
        # lands in the cache dir; the env var is what a spawned child reads.
        if _HYPOTHESIS_DB_DIR:
            try:
                from hypothesis.configuration import set_hypothesis_home_dir

                set_hypothesis_home_dir(_HYPOTHESIS_DB_DIR)
                os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", _HYPOTHESIS_DB_DIR)
            except Exception:  # noqa: BLE001 - an older hypothesis without the hook
                pass
    if getattr(_current, "derandomize", False):
        # A derandomized profile (``test/conftest.py``'s "ci") keeps no database on
        # purpose; re-registering it here would quietly give it one back.
        return
    database = DirectoryBasedExampleDatabase(_HYPOTHESIS_DB_DIR) if _HYPOTHESIS_DB_DIR else None
    _hyp_settings.register_profile(
        _hyp_settings._current_profile,
        parent=_current,
        database=database,
    )
    _hyp_settings.load_profile(_hyp_settings._current_profile)


def _redirect_bytecode_cache() -> None:
    """Write every ``.pyc`` this run compiles under the per-user cache, not the checkout.

    Importing a module writes its bytecode beside the source, and the suite imports
    a lot that is not a package: ``scripts/*.py``, ``packaging/signing/*.py``, every
    skill's ``scripts/`` helper, ``.github/scripts``, all loaded by path through
    ``spec_from_file_location`` + ``exec_module``. Each such import left a
    ``__pycache__/`` in the tree (fifteen of them per full run at the last count),
    after two audits had closed individual sites with a scoped
    ``sys.dont_write_bytecode`` (``test/skill_script_helpers.py`` is the helper for
    that). A per-site opt-out is the "remember to" contract this file exists to
    delete, so the class is closed here instead: ``sys.pycache_prefix`` sends the
    bytecode of EVERY import to a mirror tree under the cache root, and the env var
    does the same for every interpreter a test spawns. The cache still persists
    across runs (it is keyed on the absolute source path), so warm imports stay
    warm; only WHERE the files land changes. Same cache root, same fallback posture
    and same tolerance of an unwritable root as the hypothesis redirect above.
    Runs in ``pytest_configure``, before any test module or ``test/conftest.py`` is
    imported, so the test tree's own bytecode moves too.
    """
    if os.environ.get("PYTHONDONTWRITEBYTECODE"):
        return  # nothing is written anywhere; nothing to redirect
    cache_root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    candidate = os.path.join(cache_root, "kirocrew", "pycache")
    try:
        os.makedirs(candidate, exist_ok=True)
    except OSError:
        return
    sys.pycache_prefix = candidate
    os.environ["PYTHONPYCACHEPREFIX"] = candidate


def _put_the_tree_under_test_first_on_child_paths() -> None:
    """Make every interpreter a test spawns import the ``kiro_crew`` this run imports.

    ``[tool:pytest] pythonpath = src`` (or a ``-o pythonpath=<tree>/src`` override)
    puts the tree under test on THIS interpreter's ``sys.path`` only. A
    ``sys.executable -c`` child resolves ``kiro_crew`` from the venv's install
    instead. On CI the install is the checkout being tested, so nothing differs; in
    a linked worktree that borrows another checkout's venv, every child imported
    THAT checkout, and a child calling a symbol the worktree added failed with
    ``AttributeError`` (or passed against code nobody changed). Prepending the
    directory the package resolves from closes the class for every spawn at once,
    the same way ``PYTHONPYCACHEPREFIX`` does above.

    ``find_spec`` locates the package without executing it (the module-level
    no-``kiro_crew`` rule in this file's docstring). An inherited ``PYTHONPATH`` is
    kept after it, and no empty component is ever written: an empty entry is the
    child's CWD on ``sys.path`` (``test_stdlib_shadow.py``). A test that builds a
    child env from scratch, or strips ``PYTHONPATH`` on purpose, is unaffected.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("kiro_crew")
    except (ImportError, ValueError):
        return
    if spec is None or not spec.origin:
        return
    src = str(pathlib.Path(spec.origin).resolve().parent.parent)
    inherited = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p and p != src]
    os.environ["PYTHONPATH"] = os.pathsep.join([src, *inherited])


class _AsyncFixtureScanGate:
    """Run pytest-asyncio's fixture scan only when a fixture was registered since.

    pytest-asyncio 0.21.2 hooks ``pytest_pycollect_makeitem`` and, for EVERY test
    function name it sees, walks EVERY fixture definition the session has registered
    so far to wrap the async ones (``_preprocess_async_fixtures``). Fixtures already
    wrapped are skipped by a set lookup, but the ~1,400 synchronous ones are
    re-inspected with ``asyncio.iscoroutinefunction`` on each call. That is
    O(tests x fixtures): cProfile of a ``--collect-only`` over this suite (112,246
    tests) counted 87,244 scans x ~1,420 fixtures = 123.8 million coroutine checks,
    1,098 of the 1,285 profiled seconds -- 85% of collection. Every xdist worker pays
    it in full, and under coverage instrumentation each check costs ~2.3x more, which
    is what made the CI shards' ~33-minute "collection" phase.

    The scan's result only changes when a fixture is ADDED, and pytest funnels every
    registration -- conftest, module, class, unittest, plugin -- through
    ``FixtureManager._register_fixture``. So this wraps that one method to raise a
    dirty flag, and lets the scan through only while the flag is up. A scan on a clean
    flag would iterate the same definitions and find nothing new: the async marker
    (``_force_asyncio_fixture``) is set by the decorator at definition time and
    ``asyncio_mode`` is fixed for the run, so the skip is behaviour-preserving.

    Pinned to the plugin version it patches: upstream's own fix for this (v0.25.1,
    then v1.0.0) sits behind the v0.23 event-loop-scope rework this suite has not
    migrated to. When pytest-asyncio moves, delete this class and the install below.
    """

    def __init__(self, scan) -> None:
        self._scan = scan
        self.dirty = True
        self.scans = 0

    def mark_dirty(self) -> None:
        self.dirty = True

    def __call__(self, config, processed_fixturedefs) -> None:
        if not self.dirty:
            return
        self._scan(config, processed_fixturedefs)
        self.scans += 1
        self.dirty = False


def _gate_pytest_asyncio_fixture_scan() -> None:
    """Install :class:`_AsyncFixtureScanGate` once per process (each xdist worker)."""
    try:
        import pytest_asyncio.plugin as pa
    except ImportError:  # pragma: no cover - plugin absent; nothing to gate
        return
    from _pytest.fixtures import FixtureManager

    scan = getattr(pa, "_preprocess_async_fixtures", None)
    register = getattr(FixtureManager, "_register_fixture", None)
    if isinstance(scan, _AsyncFixtureScanGate):
        return  # already installed (pytest_configure re-entered in-process)
    if scan is None or register is None:
        # Both seams are private to their packages. A version that renamed either
        # must not turn into a crash before collection; it turns into the slow
        # collection this gate exists to remove, said out loud so the pin is revisited.
        warnings.warn(
            "pytest-asyncio fixture-scan gate not installed: a private seam moved "
            "(pytest_asyncio.plugin._preprocess_async_fixtures / "
            "_pytest.fixtures.FixtureManager._register_fixture); collection will be slow",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    gate = _AsyncFixtureScanGate(scan)

    @functools.wraps(register)
    def _register_fixture(self, *args, **kwargs):
        gate.mark_dirty()
        return register(self, *args, **kwargs)

    FixtureManager._register_fixture = _register_fixture
    pa._preprocess_async_fixtures = gate


def _pin_crew_log_off_for_the_process() -> None:
    """``KIROCREW_CREW_LOG=0`` for the whole run, so a test opts IN to the crew log.

    The gateway records a crew log by default. A test that drives the chat path for
    some other reason would otherwise write one into its data home, and the logs
    this suite asserts on would pick up entries from code the test never meant to
    exercise. Every crew-log test sets the variable itself: ``"1"`` to record,
    ``"0"`` to assert the off path, and ``monkeypatch.delenv`` to assert the default.
    """
    os.environ["KIROCREW_CREW_LOG"] = "0"


@pytest.hookimpl(trylast=True)
def pytest_warning_recorded(warning_message, when, nodeid, location) -> None:
    """Drop a recorded warning's ``source`` object once the warning has been rendered.

    Every warning this suite emits is retained for the whole session, and one kind of
    warning brings a whole test's object graph with it.

    The chain: ``_pytest/warnings.py`` records each warning with
    ``pytest_warning_recorded.call_historic(...)``, and pluggy appends those kwargs to
    ``_HookCaller._call_history`` -- a list it never clears, because a historic hook
    exists precisely to be replayed to plugins registered later. So the
    ``warnings.WarningMessage`` survives to the end of the run. Ordinarily that is a
    few hundred bytes. But ``WarningMessage.source`` is the OBJECT the warning is
    about, and for ``RuntimeWarning: coroutine '...' was never awaited`` that object is
    the coroutine: it holds its frame, its frame holds every local, and those locals
    hold whatever the test built. One un-awaited coroutine therefore pins an entire
    test's graph for the session, in every worker.

    ``trylast`` is what makes this free of diagnostic loss, and it is not a
    preference. ``TerminalReporter.pytest_warning_recorded`` renders the warning to a
    plain string IMMEDIATELY -- including ``tracemalloc_message(source)``, the
    "Object allocated at:" traceback when tracemalloc is tracing and the "Enable
    tracemalloc" hint when it is not -- and stores that string. Running last means the
    text is already built before the reference goes, so nothing a reader would have
    seen is lost. Clearing it ``tryfirst`` WOULD lose that suffix, which is why the
    ordering is pinned by a test.

    Kept as a BACKSTOP rather than for its average saving, and the distinction is
    measured. Where un-awaited coroutines cluster it is large: ``test_dashboard_chat.py``
    emits 60 of them across 590 tests, and clearing ``source`` takes that file from 284
    to 219 MiB. On a mixed slice it is worth nothing at all -- an AsyncMock-dense
    3,401-test set emits 11 and measures +1.7 MiB, i.e. noise. So this is not a
    general-purpose win; it removes a tail risk whose cost is one attribute write per
    warning, and which scales with however many un-awaited coroutines the suite acquires
    later.

    The residual edge: a plugin registered AFTER a warning was recorded receives the
    replay with ``source`` already gone. Conftest plugins are all registered before
    tests run, so in this repo that is unreachable.
    """
    warning_message.source = None
