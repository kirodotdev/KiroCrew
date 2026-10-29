"""The data-home floor: ``KIROCREW_HOME`` from conftest import onwards, and the
per-test pins of the operator's data home, SEL directory and ``~/.kiro`` paths.

Part of the rootdir ``conftest.py``'s host-mutation floor. That file loads this module by
path and binds the names it needs, so pytest registers every fixture and hook here as
the rootdir conftest's own; see "the floor's own modules" there. Module-level imports
are stdlib, pytest and the floor parts loaded before this one, and nothing here imports
``conftest``: the name is ambiguous, and an in-package app suite runs with ``test/`` off
``sys.path``.
"""

from __future__ import annotations

import atexit
import getpass
import importlib
import os
import pathlib
import shutil
import sys
import tempfile

import pytest

# ── how this run names the directories it leaves in the platform temp root ────
#
# Defined here, in the floor part loaded first, rather than beside :func:`_create_tmp_root`
# in ``conftest_floor_tmp``, because the import-time data-home floor below is created
# BEFORE any of that runs and has to be named by the same stem: a stray directory is attributable
# to the run that made it or it is attributable to nobody.


#: Prefix for the run's own temp base, a sibling of the platform temp root.
#:
#: The name is ``kc-pytest-<user>-<pid>``. The pid is what lets a later run tell an
#: ABANDONED root (its process is gone) from one a concurrent run is still using. The
#: user segment is not decoration: on POSIX the platform temp root is SHARED between
#: accounts, so a bare pid collides across users -- two accounts can hold the same pid
#: at the same time, and the second would try to reuse a directory it cannot write.
#: Windows gives each account its own temp root, so there the segment is redundant and
#: harmless.
_TMP_ROOT_PREFIX = "kc-pytest-"


def _tmp_root_prefix_for_run() -> str:
    """``kc-pytest-<user>-<pid>-`` -- the stem this run's temp root is created under.

    The user segment is not decoration: on POSIX the platform temp root is SHARED between
    accounts, so a bare pid collides across users -- two accounts can hold the same pid at
    the same time. The pid is what lets a later run tell an ABANDONED root from one a
    concurrent run is still using. The trailing hyphen is where ``mkdtemp`` appends its
    random component; see :func:`_create_tmp_root` for why that randomness is required and
    not cosmetic.
    """
    try:
        raw = getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry and no env fallback
        raw = "u"
    user = "".join(ch if ch.isalnum() else "_" for ch in raw)[:24] or "u"
    return f"{_TMP_ROOT_PREFIX}{user}-{os.getpid()}-"


# ── The data-home floor, installed at IMPORT time ────────────────────────────
#
# Every per-test redirect of ``KIROCREW_HOME`` is a value laid over whatever the
# process started with, so the danger is never a wrong value -- it is the ABSENCE
# of one. ``config.paths._valid_override_home`` reads the variable and, finding it
# unset, falls through to ``_default_home()``: the operator's real ``~/.kiro/crew``.
# Anything that can return the variable to "unset" mid-session therefore aims the
# whole suite at live data, and a single ``monkeypatch.undo()`` on the shared
# function-scoped instance does exactly that -- it reverts every patch on that
# stack, including a redirect some fixture installed. One such undo truncated an
# operator's real 36 MB ``memory.db`` to 29 bytes, with no backup.
#
# Setting the variable HERE, before pytest imports a fixture or a test module,
# removes "unset" from the set of reachable states: an undo restores the value the
# process started with, which is now this scratch directory rather than nothing.
# That closes the entire class at the process level, so it holds no matter which
# fixture owns a redirect, whether that fixture used a private ``MonkeyPatch``, or
# what a future test does to the shared one.
#
# An operator's own ``KIROCREW_HOME`` is honoured untouched: someone running the
# suite against a deliberately chosen home keeps it, and only the unset case --
# the one that resolves to live data -- is given a floor.
# ``mkdtemp``, never ``mkdir(exist_ok=True)`` on a pid-derived name -- the same rule
# :func:`_create_tmp_root` states and for the same reason: a pid-derived name is
# PREDICTABLE, so another local account can pre-create it as a SYMLINK to a directory
# it controls, and ``exist_ok=True`` succeeds against a symlink-to-directory. This
# directory becomes the data home the whole session falls back to, so every secret,
# token and ``memory.db`` the suite fabricates would land where that account chose.
# ``mkdtemp`` creates with O_EXCL at mode 0700 and fails rather than adopting.
# These literals are also the session guard's source of truth below. They must
# exist before choosing the import-time floor so even an inherited temp root
# inside live memory cannot influence that choice.
_REAL_DATA_HOMES = (
    pathlib.Path.home() / ".kiro" / "crew",
    pathlib.Path.home() / ".kirocrew",
)


def _create_host_home_floor() -> pathlib.Path:
    """Create the import-time floor without consulting temp environment vars.

    Named with :func:`_tmp_root_prefix_for_run` plus ``home-``, so the directory reads
    ``kc-pytest-<user>-<pid>-home-<random>`` and sorts beside the temp root the same run
    creates later. That naming is the whole point: this floor is removed only by the
    ``atexit`` handler below, which a SIGKILL (a CI timeout, a watchdog, an agent-driven
    run's kill) does not run, and ``_tmp_residue`` scans only under the run's OWN temp
    base -- so a surviving floor is invisible to the residue report and the account plus
    pid in its name is the only signal a human has for attributing it. The earlier
    ``kirocrew-test-floor-`` name identified neither the account nor the run, so a stray
    one could not be told from another user's or from a run still in progress.
    """
    if os.name == "nt":
        candidates = (
            pathlib.Path.home() / "AppData" / "Local" / "Temp",
            pathlib.Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp",
            pathlib.Path(os.environ.get("ProgramData", r"C:\ProgramData")),
        )
    else:
        candidates = (pathlib.Path("/tmp"), pathlib.Path("/var/tmp"), pathlib.Path("/usr/tmp"))
    for candidate in candidates:
        try:
            base = candidate.resolve(strict=True)
        except OSError:
            continue
        unsafe = False
        for real in _REAL_DATA_HOMES:
            try:
                live = real.resolve()
            except OSError:
                live = real.absolute()
            if base == live or live in base.parents or base in live.parents:
                unsafe = True
                break
        if unsafe or not base.is_dir():
            continue
        try:
            prefix = f"{_tmp_root_prefix_for_run()}home-"
            return pathlib.Path(tempfile.mkdtemp(dir=base, prefix=prefix))
        except OSError:
            continue
    raise RuntimeError("no writable system temp directory exists outside live Kiro Crew homes")


_HOST_HOME_FLOOR: "pathlib.Path | None" = None
if not os.environ.get("KIROCREW_HOME"):
    _HOST_HOME_FLOOR = _create_host_home_floor()
    os.environ["KIROCREW_HOME"] = str(_HOST_HOME_FLOOR)

    @atexit.register
    def _remove_host_home_floor() -> None:
        """Remove the floor, so it does not accumulate in the platform temp root.

        This is the ONLY removal, and it is deliberately the only one. ``atexit`` is
        skipped by the SIGKILL that produces the residue actually MEASURED on developer
        hosts (one stray floor per aborted run), but no in-process hook does better: a
        session-fixture finalizer or ``pytest_unconfigure`` is skipped by the same signal,
        and under ``-n auto`` the process that OWNS the floor is the CONTROLLER -- it sets
        ``KIROCREW_HOME`` at conftest import, every execnet worker inherits it and so takes
        the branch above as false, and the controller runs no session fixtures at all.
        Sweeping OTHER runs' floors is ruled out by the same unsoundness argument
        :func:`_isolate_tempfile_base` spells out for the temp root: the name is
        pre-createable by another local account and a pid means nothing across PID
        namespaces. So a killed run leaves TWO directories for the platform to reclaim --
        the run's temp root and this floor -- and the floor leaks MORE often, because it is
        created at conftest import and therefore survives even a run aborted during
        collection, before any session fixture existed to make a temp root.

        Nothing reports it either: ``_tmp_residue`` scans only under the run's own temp
        base, and this floor is a SIBLING of that base in the platform temp root, so
        neither the residue warning nor ``KIROCREW_TMP_RESIDUE_STRICT`` can see it or
        anything a test wrote into it. That invisibility is why the directory carries
        ``_tmp_root_prefix_for_run()`` in its name.
        """
        if _HOST_HOME_FLOOR is not None:
            shutil.rmtree(_HOST_HOME_FLOOR, ignore_errors=True)


def _refuse_a_real_data_home() -> None:
    """Refuse to run at all if the session would resolve a real data home.

    A second, independent barrier to the floor above, and the one that fails LOUD.
    The floor prevents the accident; this catches the case where someone exports
    ``KIROCREW_HOME`` to their real home by hand -- copying a command out of a
    runbook, or reusing a shell that was pointed at live data -- which no amount of
    per-test isolation can distinguish from a deliberate choice.

    Called from ``pytest_configure`` rather than being a second hook of that name:
    a module defines one, and a duplicate silently replaces the earlier definition
    instead of running alongside it.
    """
    resolved = pathlib.Path(os.environ["KIROCREW_HOME"]).expanduser()
    for real in _REAL_DATA_HOMES:
        try:
            here, live = resolved.resolve(), real.resolve()
        except OSError:  # an unreadable component cannot be the live home
            continue
        # CONTAINMENT in both directions, not equality. A PARENT is the dangerous
        # miss: ``~/.kiro`` is kiro-cli's own home and resolves the whole live tree
        # underneath it, and ``~`` resolves everything. A CHILD is refused too, since
        # the suite deletes directories it believes it created.
        if here == live or live in here.parents or here in live.parents:
            raise pytest.UsageError(
                f"KIROCREW_HOME points at the live data home {real}. The suite writes, "
                f"truncates and deletes under it, so running here destroys real memory, "
                f"sessions and config. Unset KIROCREW_HOME or point it at a scratch dir."
            )


# ── the operator's data home is host state too ────────────────────────


@pytest.fixture(scope="session")
def _isolation_root(tmp_path_factory):
    """One session-scoped parent for the per-test isolation dirs below.

    ``tmp_path_factory.mktemp`` picks its numbered suffix by scanning the whole
    basetemp, so its cost grows with the number of entries already there. The
    autouse fixtures that need a directory ``mkdir`` under this root instead, which
    is a single syscall and does not scan.

    Named ``i`` rather than something descriptive to keep the paths short: Windows
    still caps a path at 260 characters unless long paths are enabled, and
    everything a test writes under ``KIROCREW_HOME`` nests inside here.
    """
    return tmp_path_factory.mktemp("i")


@pytest.fixture
def _isolation_dirs(_isolation_root):
    """Return an allocator for this test's isolation PATHS.

    Each call returns ``<root>/<counter>-<name>``, so one test's paths cannot collide
    with another's (the counter is per-process, and pytest-xdist gives every worker
    its own ``basetemp``). Flat rather than nested, to keep Windows paths short.

    **A path, not a directory: nothing is created.** Every consumer only needs somewhere
    to POINT, and creates the directory itself if it ever writes -- ``config_dir()``
    creates the data home, ``create_agent_folder`` creates the subagent registry,
    ``OLLAMA_MODELS`` is only ever read. Creating them eagerly cost a ``mkdir`` per name
    on every test and left the directories behind for the whole session, because they are
    not ``tmp_path`` dirs and no retention policy reaches them.

    MEASURED on one full ``test/`` run at ``-n 8``: 366,716 of the run's 372,126
    basetemp inodes -- 98.5% -- were these directories, ~317k of them empty. On a
    ``/tmp`` tmpfs with a fixed 1,048,576-inode budget that is a third of the machine's
    entire allowance spent on directories nothing ever opened, and it is what made a
    second concurrent run fail with ENOSPC while 90% of the bytes were free.

    A future consumer that genuinely needs the directory to exist should ``mkdir`` at its
    own call site, where the reason is visible, rather than through a flag here.
    """
    made: dict[str, pathlib.Path] = {}
    _isolation_dirs.seq += 1  # type: ignore[attr-defined]
    stem = _isolation_dirs.seq  # type: ignore[attr-defined]

    def _get(name: str) -> pathlib.Path:
        path = made.get(name)
        if path is None:
            path = _isolation_root / f"{stem}-{name}"
            made[name] = path
        return path

    return _get


_isolation_dirs.seq = 0  # type: ignore[attr-defined]


#: Set on an item by ``_isolate_kirocrew_home`` once its pins are in force. The
#: teardown check reads it: a test skipped at setup (a ``skipif`` marker) never ran
#: the floor, so ``paths._resolved_home`` still holds whatever collection resolved
#: (the operator's real home, legitimately) and must not be blamed for it.
_HOME_PIN_ARMED = pytest.StashKey[bool]()


@pytest.fixture(autouse=True)
def _isolate_kirocrew_home(request, _isolation_dirs, _floor_monkeypatch):
    """Pin ``KIROCREW_HOME`` and ``KIROCREW_WORKSPACE`` to per-test tmp dirs, for EVERY testpath.

    The pin is applied through a PRIVATE ``pytest.MonkeyPatch`` instance, not the
    shared function-scoped ``monkeypatch`` fixture. The shared instance is one undo
    stack for every fixture and helper the test touched, so a test helper that calls
    ``monkeypatch.undo()`` to revert its own patch also reverts THIS pin -- and every
    path the test resolves afterwards lands in the operator's live ``~/.kiro/crew``.
    That is not hypothetical: a memory-store test did exactly that and then wrote
    ``b"this is not a sqlite database"`` over an operator's real 36 MB ``memory.db``,
    which had no backup. With its own instance the pin can only be undone by this
    fixture's teardown. The public ``monkeypatch`` fixture depends on
    ``_floor_monkeypatch``, so the test's overrides unwind before the floor and a
    later shared undo cannot reinstall a stale per-test home.

    This lives at the rootdir rather than in ``test/conftest.py`` because the leak it
    closes is worst in the testpaths that conftest does not reach. The ~108 test
    modules under ``src/kiro_crew/apps/builtins/*/tests/`` ship inside the package and
    see only the rootdir conftest, so before this fixture existed any of them that touched
    ``config_dir()`` resolved the operator's live data home -- and that resolution is
    not read-only: ``config_dir()`` CREATES the home and its marker on first use, and
    can run the one-time ``~/.kirocrew`` -> ``~/.kiro/crew`` migration as a side
    effect. Two of the eight app suites had grown their own redirect fixture; the
    other six had not, which is exactly the "remember to" contract this file exists to
    delete.

    A test that sets its own ``KIROCREW_HOME`` still wins: ``monkeypatch.setenv``
    applied later in setup overrides this, and reverts independently.

    ``config.paths._resolved_home`` is reset with it. ``config_dir()`` memoises the
    resolved home in that module global for the process lifetime, and under xdist one
    worker runs thousands of tests in one process, so a value cached by an earlier
    test would otherwise leak into a later one. Resetting it also invalidates
    ``_config_dir_memo``, which is keyed on that global by identity.

    ``KIROCREW_PROJECT_DIR`` is cleared for a different reason -- to match CI on a dev
    box. It is auto-set to the repo root when running from a checkout, so
    ``skills._project_skills_dir()`` resolves the repo's real ``skills/`` and a test
    driving ``_ensure_builtin_skills`` against a tmp dir sees live skills as a
    "source", flipping relocation behaviour: green in CI, red locally.

    ``KIROCREW_BOUND_PORT`` is cleared because ``_export_bound_port`` writes it into
    the real process environment when a test boots a server, so a port exported by one
    test would leak into every later test's port resolution on that worker.
    ``KIROCREW_DEV_MODE`` / ``KIROCREW_STRICT_ON_LOOP_PERSIST`` are cleared so a
    developer who exports them does not flip the off-loop-IO guards strict for the
    whole suite.

    ``KIROCREW_SCRATCH_ROOT`` is cleared so a developer who relocates their own
    scratch tree (``agent_scratch.scratch_root()`` reads it) does not make
    every test resolve scratch to that drive instead of the per-test home -- which
    also flips the ``scratch_root() == config_dir()/"scratch"`` invariant a sandbox
    seal test pins. A test that wants the override sets it itself.

    ``KIRO_HOME`` is deliberately NOT pinned here, even though the lazy
    ``config.paths.kiro_home()`` does name the operator's real machine-wide kiro-cli
    home. The env var takes precedence over ``Path.home()`` by design, and ~35 tests
    isolate that resolver the other way round -- ``patch("pathlib.Path.home",
    return_value=tmp_path)`` -- so pinning the variable overrides their own isolation
    and they read an empty directory instead of the tree they just built. A test that
    reaches ``kiro_home()`` therefore isolates it itself, with whichever of the two
    levers it already uses.

    ``KIROCREW_WORKSPACE`` is pinned alongside ``KIROCREW_HOME`` for the same
    reason as the rest of this fixture: ``config.loader.workspace_root()`` is its
    own default, independent of ``config_dir()`` -- with no override and no saved
    ``<config_dir>/workspace_dir`` file it falls back to the platform's
    ``_default_workspace_base()`` plus ``kirocrew-workspace``, which on Linux/macOS
    is the operator's real ``~/workplace``. Any test that reaches an agent working
    directory (``workspace_root()``, ``_session_work_dir()``, ``outbox_dir()``)
    without setting its own ``KIROCREW_WORKSPACE`` therefore ``mkdir``s and writes
    into that real tree. Unlike ``config_dir()``, ``workspace_root()`` reads the env
    var fresh on every call and caches nothing, so there is no module-global memo to
    reset here -- setting the variable is the whole fix.

    ``KIROCREW_POD_ROOT`` and ``KIROCREW_POD_ENV_DIR`` are pinned for the same reason
    again, and this one is BY DESIGN unreachable from ``KIROCREW_HOME``:
    ``pod.config.PodConfig.load()`` derives ``pods_dir`` from ``_default_home()`` --
    the operator's real ``~/.kiro/crew/pods`` -- precisely so a pod process running
    with its own isolated ``KIROCREW_HOME`` cannot redirect the HOST's pod registry
    into a throwaway home, and ``pod_root`` from ``Path.home()/.kirocrew-pods``. Both
    are read fresh from the environment on every ``load()``. Without this pin a test
    that boots a pod, or records a refusal, writes ``<name>.env`` / ``<name>.refused``
    into the operator's real pod plane: five full-suite runs left
    ``~/.kiro/crew/pods/viability-*.refused`` behind on the developer's machine, from
    a fixture that was simply ``PodConfig.load()``. ``test_pod.py::TestConfig::
    test_defaults`` still asserts the unpinned defaults -- it clears every
    ``KIROCREW_POD_*`` variable itself first, which is the documented way to test
    the default resolution.
    """
    monkeypatch = _floor_monkeypatch
    monkeypatch.setenv("KIROCREW_HOME", str(_isolation_dirs("kirocrew-home")))
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(_isolation_dirs("workspace")))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(_isolation_dirs("pod-root")))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(_isolation_dirs("pods")))
    # Cleared for the test AND removed again at teardown. ``monkeypatch.delenv`` on a
    # variable that is absent records no undo, so a value the code under test WRITES
    # -- ``_export_bound_port`` publishing KIROCREW_BOUND_PORT when a server boots,
    # ``cli.main`` pinning KIROCREW_PROJECT_DIR -- outlived its test and reached the
    # next one on the worker (observed for both in a full run). Recording the
    # absence first makes teardown delete whatever landed.
    for _name in (
        "KIROCREW_PROJECT_DIR",
        "KIROCREW_BOUND_PORT",
        "KIROCREW_DEV_MODE",
        "KIROCREW_STRICT_ON_LOOP_PERSIST",
        "KIROCREW_SCRATCH_ROOT",
    ):
        if _name in os.environ:
            monkeypatch.delenv(_name)
        else:
            monkeypatch.setenv(_name, "")  # the undo for this entry is "was absent"
            monkeypatch.delenv(_name)
    _reset_path_resolver_degradation(monkeypatch)
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
        monkeypatch.setattr(paths, "_override_home_pin", None, raising=False)
        monkeypatch.setattr(paths, "_scratch_root_override_pin", paths._UNSET, raising=False)
        monkeypatch.setattr(
            paths,
            "_write_recovery_breadcrumb",
            _breadcrumb_guard(paths._write_recovery_breadcrumb),
            raising=False,
        )
    request.node.stash[_HOME_PIN_ARMED] = True


def _reset_path_resolver_degradation(monkeypatch) -> None:
    """Give every test an unstalled sensitive-path resolver, and leave none behind.

    ``security.paths`` remembers resolver degradation and cumulative wait in four
    PROCESS-GLOBAL structures, and a charged prefix makes ``is_sensitive_path()``
    answer True for every path beneath it, without touching the filesystem, until
    the cooldown lapses. Since ``_stall_prefix`` keys on the mount, one test whose
    resolution is merely slow on a loaded runner can therefore fail every LATER
    test on the same xdist worker whose paths live under the same prefix -- observed
    as 77 ``ArtifactError: refusing to use sensitive path as artifact root`` errors
    across four unrelated files on one Windows shard, from a single charged stall.
    Resetting on both sides makes that cascade impossible to inherit and impossible
    to export, so a test that provokes a stall on purpose still sees only its own.

    ``_path_resolve_degraded`` is re-exported by the
    ``kiro_crew.security`` facade, so it is set THROUGH it: the facade mirrors a
    write onto the owning submodule, while patching the owner alone would leave the
    facade holding the original object and break the export-identity contract
    ``test_security_facade`` pins. ``_path_resolve_load_probes`` and
    ``_path_resolve_thread_waits`` are NOT re-exported, so they must be set on the
    submodule -- a facade write for those names would mirror nowhere and silently
    do nothing.
    """
    security = sys.modules.get("kiro_crew.security")
    paths = sys.modules.get("kiro_crew.security.paths")
    if security is not None:
        monkeypatch.setattr(security, "_path_resolve_degraded", {}, raising=False)
    if paths is not None:
        monkeypatch.setattr(paths, "_path_resolve_load_probes", {}, raising=False)
        monkeypatch.setattr(paths, "_path_resolve_thread_waits", {}, raising=False)


def _breadcrumb_guard(real):
    """Refuse the recovery breadcrumb while ``Path.home()`` is the operator's.

    ``config_dir()`` on its DEFAULT path writes ``~/.kirocrew.breadcrumb`` (outside
    ``~/.kiro`` on purpose) naming the home it resolved. A test that relocates the
    default with ``paths._resolve_default_home = lambda: tmp_path / ...`` and nothing
    else therefore rewrites the operator's real breadcrumb to point at a pytest tmp
    dir: the third audit found six such writes per run, each repaired only because
    the live gateway rewrote the file later. The teardown check cannot see this one
    (``_resolved_home`` holds the tmp stand-in), so the guard sits on the writer:
    with ``Path.home()`` still the real home it FAILS the test naming the fix; with
    a faked home (``monkeypatch.setattr(Path, "home", ...)``) it delegates, so the
    tests OF the breadcrumb keep exercising the real writer against their fake home.
    """

    def _guarded(data_home):
        try:
            home_is_real = pathlib.Path.home().resolve() == _REAL_USER_HOME.resolve()
        except OSError:  # pragma: no cover - a faked home that does not exist
            home_is_real = False
        if home_is_real:
            raise AssertionError(
                "this test would write the operator's real ~/.kirocrew.breadcrumb "
                f"(pointing it at {data_home}): the default-home path was taken with "
                "Path.home() still the real home. Relocate the WHOLE default, not just the "
                "resolver: monkeypatch HOME and pathlib.Path.home to a dir under tmp_path, "
                "or stub paths._write_recovery_breadcrumb when the breadcrumb is not what "
                "the test is about; see conftest._breadcrumb_guard."
            )
        return real(data_home)

    return _guarded


def _is_the_operators_default_home(path) -> bool:
    """Whether *path* is the real ``~/.kiro/crew`` (not a tmp stand-in for it)."""
    try:
        return pathlib.Path(path).resolve() == (_REAL_USER_HOME / ".kiro" / "crew").resolve()
    except OSError:
        return False


def _resolved_real_default_home():
    """The operator's real home if ``paths._resolved_home`` holds it right now, else None."""
    paths = sys.modules.get("kiro_crew.config.paths")
    resolved = getattr(paths, "_resolved_home", None) if paths is not None else None
    if resolved is not None and _is_the_operators_default_home(resolved):
        return resolved
    return None


def _refuse_a_resolved_real_default_home(resolved) -> None:
    """Fail the test that let ``config_dir()`` fall through to the operator's home.

    ``paths._resolved_home`` is only ever written by ``_resolve_default_home()``, i.e.
    by a ``config_dir()`` / ``data_home()`` call made with NO override in force. So a
    value equal to the operator's real ``~/.kiro/crew`` at teardown means the test
    dropped the pin (``monkeypatch.delenv("KIROCREW_HOME")``) to exercise the default
    path and let production resolve, and CREATE, the real data home, plus the
    recovery breadcrumb beside it. Five tests did in the third full-run audit; each
    passed. A test of the default path relocates the default too
    (``monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "d")``,
    or ``_resolved_home`` itself); ``test_lazy_data_home_paths.py`` is the shape.

    The evidence is read by ``pytest_runtest_teardown`` BEFORE the test's own
    ``monkeypatch`` unwinds (a test that set ``_resolved_home = None`` through
    monkeypatch and then resolved the real home would otherwise have it restored to
    ``None`` before any fixture could look), and the failure is raised AFTER every
    fixture has been torn down, so the floor's pins are always restored. One attribute
    read and one path comparison per test, so it costs nothing on the ~92k tests that
    never touch the global.
    """
    raise AssertionError(
        "this test resolved the operator's REAL default data home "
        f"({resolved}): it dropped the KIROCREW_HOME pin and let config_dir() / "
        "data_home() fall through to ~/.kiro/crew, which CREATES that directory. "
        "A test of the default path pins the default too: "
        "monkeypatch.setattr(kiro_crew.config.paths, '_resolve_default_home', "
        "lambda: tmp_path / 'default'); see test_lazy_data_home_paths.py and "
        "conftest._refuse_a_resolved_real_default_home."
    )


@pytest.fixture(autouse=True, scope="session")
def _isolate_sel_default_dir(tmp_path_factory):
    """Redirect the Security Event Log's default dir to a session-local tmp dir.

    SEL is a process SINGLETON whose writer is a DAEMON THREAD, and
    ``_init_locked`` binds ``self._dir`` once, from whatever ``_default_dir()``
    resolved at that moment. Two consequences, and the second is why this belongs at
    the rootdir rather than in ``test/conftest.py``:

    * Every event any test emits through the default ``sel()`` appends to ONE file.
      Unredirected that is the operator's real ``security_events.jsonl`` -- a
      non-atomic append, shared across xdist workers.
    * Whichever test calls ``sel()`` FIRST fixes the directory for the whole
      process, and the writer thread keeps using it after that test ends. When the
      first caller is a test whose home is a per-test tmp dir, the thread outlives
      the tmp dir and RE-CREATES it on the next flush -- ``_flush_batch`` opens with
      ``self._dir.mkdir(parents=True, exist_ok=True)``. So the directory reappears
      *after* the test's own cleanup removed it, and no amount of tidying in the
      test can win against a thread that rebuilds the path. MEASURED: this is
      exactly what left one stray ``mkdtemp`` directory behind per run of the
      ops-mission-control suite. Full telling:
      ``docs/system-specs/common/testing-conventions.md`` § Rules.

    Session scope is what fixes both: the thread's directory is stable for the whole
    run and belongs to no individual test, so nothing deletes it underneath the
    writer, and the thread is not churned per test.

    Patches the ``_default_dir()`` accessor rather than a captured constant, because
    the module resolves its default lazily so that importing ``kiro_crew.sel`` never
    triggers the one-time data-home migration as an import side effect. Tests that
    manage their own ``SecurityEventLog`` (passing ``base_dir`` and resetting
    ``_instance``) are unaffected.
    """
    try:
        from kiro_crew import sel as _sel
    except ImportError:  # pragma: no cover - partial checkout
        yield
        return
    original_default = _sel._default_dir
    original_instance = _sel.SecurityEventLog._instance
    sel_dir = tmp_path_factory.mktemp("sel")
    _sel._default_dir = lambda: sel_dir
    _sel.SecurityEventLog._instance = None
    try:
        yield
    finally:
        _sel._default_dir = original_default
        _sel.SecurityEventLog._instance = original_instance


@pytest.fixture
def sel_private_root(tmp_path_factory):
    """Bind the SEL singleton to a directory PRIVATE to the requesting test.

    ``_isolate_sel_default_dir`` above gives every test on a worker ONE shared
    SEL directory, which is the right default tier: the writer is a daemon
    thread on a process singleton, and a per-test ``tmp_path`` would be deleted
    underneath it (see that fixture's docstring). But one shared root also
    means one shared CHAIN LOCK, and a test whose assertion transitively
    depends on a fail-closed critical audit WINNING that lock can be refused by
    a concurrent writer it never created — the loop-side acquire is a single
    non-blocking attempt by design (the issue-radar trust flake).

    This is the per-test tier of the same seam. It rebinds the singleton to a
    fresh directory nothing else writes, so no other test — on this worker or
    any other — can hold this test's chain lock:

    * The directory comes from ``tmp_path_factory``, whose numbered dirs are
      never deleted mid-run (a lingering writer can never resurrect a removed
      path) and whose basetemp is already per-xdist-worker (``popen-gwN``), so
      the isolation holds across workers as well as across tests.
    * The instance is built ``sync=True``: every event is written inline on its
      caller's thread and NO background writer starts, so the only writers on
      this root are the test's own threads — which the module-level chain-hold
      registry already lets join one another.
    * ``_default_dir()`` is deliberately NOT repointed: the displaced shared
      directory stays reachable through it, which is what lets a differential
      test hold the SHARED root's lock and prove this test does not contend
      for it.

    Teardown restores the PRIOR singleton (not ``None``), so later tests on
    this worker resume the session default exactly where it left off instead of
    minting a second instance — and a second writer thread — on the same
    directory.
    """
    try:
        from kiro_crew.sel import SecurityEventLog
    except ImportError:  # pragma: no cover - partial checkout
        yield None
        return
    root = tmp_path_factory.mktemp("sel-private")
    prior_instance = SecurityEventLog._instance
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    try:
        # Inside the try: ``__new__`` publishes to ``_instance`` before
        # ``__init__`` runs, so a failing ``_init_locked`` would otherwise
        # orphan the half-built object AND lose ``prior_instance`` — every
        # later test on this worker would then re-init against the shared
        # default with a second writer thread, the exact hazard the restore
        # exists to prevent.
        SecurityEventLog(base_dir=root, sync=True)
        yield root
    finally:
        SecurityEventLog._instance = prior_instance
        # Class attribute only (each instance shadows it in __new__); reset to
        # the declared default for symmetry with test_sel.py's convention —
        # the restored instance keeps its own per-instance _initialized=True.
        SecurityEventLog._initialized = False


#: ``~/.kiro`` paths production binds at IMPORT time, which ``KIROCREW_HOME`` cannot
#: reach: the module captured an absolute path from ``Path.home()`` before any test
#: set an environment variable, so the env override is read too late to matter.
#:
#: This directory is a SEPARATE isolation axis from the data home. ``~/.kiro`` is
#: kiro-cli's own home -- machine-wide, shared with the real installed agent -- so a
#: test that writes ``~/.kiro/settings/mcp.json`` edits the MCP servers of the
#: operator's live agent, not a copy of them.
#:
#: Each entry is ``(module, attribute, path relative to the per-test kiro home)``.
#: The relative paths keep production's real SHAPE (``.kiro/settings/mcp.json``, not a
#: flattened name) because several tests assert on the path's suffix, and a
#: same-shaped tmp path keeps those assertions meaningful.
#:
#: ``test/test_host_isolation_floor.py`` ratchets this table against the
#: ``Path.home()`` bindings ``src/kiro_crew`` actually has, so a new one cannot land
#: unpinned.
_SHARED_KIRO_PATHS: tuple[tuple[str, str, str], ...] = (
    ("kiro_crew.agent", "_KIRO_MCP_JSON", ".kiro/settings/mcp.json"),
    ("kiro_crew.agent", "_CC_MCP_JSON", ".claude.json"),
    ("kiro_crew.agent", "_DEFAULT_KIRO_HOOKS_DIR", ".kiro/hooks"),
    ("kiro_crew.learn", "_DEFAULT_DIR", ".kiro/crew"),
    ("kiro_crew.apps.bridges_mcp", "_LEGACY_SHARED_MCP_PATH", ".kiro/settings/mcp.json"),
    (
        "kiro_crew.dashboard.handlers.mcp_config_files",
        "_GLOBAL_MCP_JSON",
        ".kiro/settings/mcp.json",
    ),
    ("kiro_crew.dashboard.handlers.mcp", "_GLOBAL_MCP_JSON", ".kiro/settings/mcp.json"),
    # A DERIVED sibling (`_GLOBAL_MCP_JSON.with_suffix(".lock")`), and it has to move
    # WITH its json or the pair is worse than either alone: `_McpFileLockSync.__enter__`
    # creates `_GLOBAL_MCP_JSON.parent` and then touches `_MCP_LOCK_PATH`, so redirecting
    # only the json makes the code create a tmp directory and then touch a lock in the
    # REAL one -- whose parent nothing created, giving `FileNotFoundError` on any host
    # where `~/.kiro/settings` does not already exist. It passed on a developer box only
    # because that directory was there, and on CI only because an earlier test had
    # leaked into it. This is the sibling-binding case the ratchet's own docstring says
    # it cannot see, which is why the set is enumerated here by hand.
    ("kiro_crew.dashboard.handlers.mcp", "_MCP_LOCK_PATH", ".kiro/settings/mcp.lock"),
    # RELATIVE in production (``home / _AUTH_STAGING_RELATIVE``, home = ``Path.home()``),
    # and it must stay so: the sandbox hides ``~/.kiro/crew-auth-staging`` by that
    # HOME-relative spelling, so deriving it from the data home instead would leave a
    # relocated ``KIROCREW_HOME``'s staging tree visible to the sandboxed CLI. Pinning
    # the constant to an ABSOLUTE tmp path still lands, because ``pathlib`` discards
    # the left operand when the right one is absolute -- so the mkdir+chmod that
    # ``KiroPrerequisiteService`` performs on construction stops reaching the
    # operator's real ``~/.kiro`` (fifteen dashboard-server tests did).
    ("kiro_crew.kiro_prerequisite", "_AUTH_STAGING_RELATIVE", ".kiro/crew-auth-staging"),
)


@pytest.fixture(autouse=True)
def _isolate_shared_kiro_paths(_isolation_dirs, _floor_monkeypatch):
    """Redirect the import-time ``~/.kiro`` bindings to a per-test tmp tree.

    Patches only a module ALREADY in ``sys.modules``, the same tolerance
    ``_isolate_launchd_paths`` uses: several of these modules are heavy, and importing
    them for all ~26.5k tests would cost far more than the leak they close.

    That filter is not a coverage gap, but the reason is narrower than "collection
    imports everything": a module that is not loaded has no binding for a test to
    REACH, so there is nothing to leak. The residual hole is a module first imported
    inside a test's own body, after this fixture has already run for that test.

    Creates nothing. Every path here names a file whose absence is the normal
    fresh-install state, so a READER handles it already, and every test in the suite
    that WRITES one of them patches the same attribute itself (which wins over this
    fixture). Pre-creating the ``.kiro/settings`` parents instead cost a ``mkdir`` per
    entry on every test in the suite -- see ``_isolation_dirs`` for what that added up
    to. So a test that touches none of these modules pays one ``sys.modules`` lookup
    per entry and no syscall at all.
    """
    monkeypatch = _floor_monkeypatch
    targets = [
        (module, attr, relative)
        for module, attr, relative in _SHARED_KIRO_PATHS
        if sys.modules.get(module) is not None
    ]
    if not targets:
        return
    root = _isolation_dirs("kiro-home")
    for module, attr, relative in targets:
        monkeypatch.setattr(
            sys.modules[module], attr, root.joinpath(*relative.split("/")), raising=False
        )


# ── other real host paths a test must not reach ───────────────────────
#
# Same test as the data home above: each of these protects something on the
# operator's machine rather than the correctness of one suite, so it holds for every
# testpath. They were in ``test/conftest.py``, which the ~108 in-package test modules
# never load -- so an in-package test that spawned a subagent wrote the real registry,
# and one that reached the embeddings boot path could start a 610MB download.


@pytest.fixture(autouse=True)
def _isolate_subagents_dir(_isolation_dirs, _floor_monkeypatch):
    """Pin the subagent registry dir to a tmp dir for the whole suite.

    ``kiro_crew.subagent_persistence._SUBAGENTS_DIR`` is bound at import time to
    ``config_dir() / "subagents"``, so the ``KIROCREW_HOME`` safety net above
    cannot retroactively redirect it. Any test that calls ``SubagentManager.spawn``
    or ``create_agent_folder`` without isolating this global itself would write
    stub agent folders into the operator's real ``~/.kirocrew/subagents/``. On the
    next gateway start, orphan reconciliation sweeps those stubs and floods the
    logs with "lost to gateway restart" warnings (e.g. tasks ``t`` / ``ls /tmp``).
    The registry lives beneath its own per-test home so sibling protected
    identity records are isolated too, including runs with repeated ids.
    """

    monkeypatch = _floor_monkeypatch
    monkeypatch.setattr(
        "kiro_crew.subagent_persistence._SUBAGENTS_DIR",
        _isolation_dirs("subagents") / "subagents",
    )


#: Modules carrying the documented ``KIRO_AGENTS_DIR`` override hook (``None`` = live).
#: Each is ``(module, attribute)``; the fixture below points them all at ONE per-test
#: directory, so the hook production already offers "a caller (test/tooling)" is set
#: by default instead of per test.
_AGENT_SPEC_HOOKS: tuple[tuple[str, str], ...] = (
    ("kiro_crew.agent", "KIRO_AGENTS_DIR"),
    ("kiro_crew.agent_discovery", "_KIRO_AGENTS_DIR"),
    ("kiro_crew.apps.bridges", "KIRO_AGENTS_DIR"),
    ("kiro_crew.cli_doctor", "KIRO_AGENTS_DIR"),
    ("kiro_crew.doctor_deadpath", "KIRO_AGENTS_DIR"),
)

#: The real user home, captured at IMPORT -- before any test can patch
#: ``Path.home``. The pin below compares against it to tell "nobody redirected the
#: home, so use the per-test dir" from "this test redirected it, so follow the test".
#: ``patch("<module>.Path.home", ...)`` is the dominant isolation idiom in this suite
#: and it is global by construction (``from pathlib import Path`` binds the same class
#: object everywhere, so patching an attribute on it patches it for every module), which
#: is why the pin can detect it at all.
_REAL_USER_HOME = pathlib.Path.home()


@pytest.fixture(scope="session")
def _agent_spec_seam_modules():
    """Import the HOOK modules once per worker process.

    Ordering is what this buys: the per-test fixture below can only patch a module that
    is already imported, and these four carry the agent-spec write seams, so "whatever
    collection happened to import" is not a strong enough guarantee for them.

    Scoped to the hook table on purpose. The resolver-binding table is far wider and
    includes the heaviest modules in the repo (``slack.gateway``), which no worker should
    import to protect a binding that module cannot reach unless a test imported it
    anyway. Those keep the patch-if-imported tolerance ``_isolate_shared_kiro_paths``
    uses.

    ``ImportError`` is tolerated per module, matching the convention the other fixtures
    here follow: a partial checkout must not break collection, and a module that cannot
    import has no binding to leak.
    """
    for module, _attr in _AGENT_SPEC_HOOKS:
        try:
            importlib.import_module(module)
        except ImportError:  # pragma: no cover - partial checkout
            continue


@pytest.fixture(autouse=True)
def _isolate_agent_spec_home(_agent_spec_seam_modules, _isolation_dirs, _floor_monkeypatch):
    """Pin the AGENT-SPEC home to a per-test tmp dir, for EVERY testpath.

    A third isolation axis, distinct from both the data home and the import-time
    ``~/.kiro`` bindings above. The agent specs are the file kiro-cli reads to learn
    which MCP servers exist, and ``kiro_agents_dir()`` is a LAZY resolver
    (``kiro_home()`` -> ``$KIRO_HOME`` or ``Path.home()/.kiro``), so neither
    ``KIROCREW_HOME`` nor ``_SHARED_KIRO_PATHS`` reaches it -- the shared-path ratchet's
    own docstring records that lazy resolvers are outside its scope.

    Without this, any test reaching the write path (``rebuild_agent_config`` and the
    per-agent writers around it, ``apps.bridges._register_agents``) rewrites the
    operator's machine-wide ``<kiro home>/agents/kirocrew.json``. Confirmed live
    on a host: a suite run inside a throwaway clone left every managed server's
    ``command`` pointing into that clone's venv and pinned the per-test data home into
    their ``env``, because ``_managed_mcp_env`` stamps the WRITER's paths. Both stop
    existing when the run ends, so afterwards every new session on the machine spawned
    ``kirocrew-core`` from a deleted venv against a data home recreated empty ->
    ``read_local_secret()`` returned "" -> every internal HTTP call failed
    ``internal_auth_mismatch`` (``received=absent``), killing ``spawn_run``,
    ``learn_add`` and ``cron_*`` while in-process tools kept working. A gateway restart
    healed it, and the next suite run re-broke it, which is what made it look
    intermittent and unfixable.

    ``KIRO_HOME`` is NOT the lever used here, deliberately -- see
    ``_isolate_kirocrew_home`` for why pinning that variable is refused: ~35 tests
    isolate ``kiro_home()`` the other way round with
    ``patch("pathlib.Path.home", ...)``, and an env pin overrides their own isolation
    so they read an empty directory instead of the tree they just built. Pinning the
    per-module seams instead leaves both of those levers untouched.

    One directory for every entry, not one each: production resolves a single agents
    dir, so a test that writes a spec through one seam and reads it through another
    (``rebuild_agent_config`` then ``agent_discovery``) has to see the same tree.

    Unlike ``_isolate_shared_kiro_paths``, the HOOK half does not settle for patching
    whatever happens to be in ``sys.modules``. That fixture's residual hole -- a module
    first imported inside a test's own body, after the fixture already ran -- is
    tolerable for a path that is only read, and not for these: ``rebuild_agent_config``
    and ``apps.bridges._register_agents`` WRITE, and a test that imports its subject in
    its own body is a normal shape here. ``_agent_spec_seam_modules`` therefore imports
    those four once per worker process. MEASURED cold on this tree: agent 177 ms,
    cli_doctor 87 ms, bridges 33 ms, agent_discovery 19 ms -- ~315 ms per worker for a
    whole session, against a shard that runs for minutes. Per-TEST import would have
    been the unaffordable shape, which is what that other fixture's tolerance is really
    about.

    The resolver-binding half keeps the patch-if-imported tolerance: it is wide and
    includes the repo's heaviest modules, and a module nobody imported has no bound name
    for a test to reach.

    Creates nothing -- an absent agents dir is the normal fresh-install state, and
    every writer ``mkdir(parents=True)`` first.

    A test that sets its own value still wins, through EITHER lever. ``monkeypatch``
    applied later in setup overrides the seams directly; and a test that sets
    ``KIRO_HOME`` -- the documented env override, which several tests use to place the
    agents dir under their own ``tmp_path`` -- is deferred to by the replacement
    resolver installed here. Deferring is safe because the variable is CLEARED first,
    so anything visible afterwards was chosen by the test rather than exported by the
    operator (whose value would name another real home).
    """
    monkeypatch = _floor_monkeypatch
    monkeypatch.delenv("KIRO_HOME", raising=False)
    root = _isolation_dirs("kiro-agents")
    paths = sys.modules.get("kiro_crew.config.paths")

    def _pinned_agents_dir() -> pathlib.Path:
        """The per-test dir, unless this test redirected the home itself."""
        if paths is None:  # pragma: no cover - defensive: package not importable
            return root
        if os.environ.get("KIRO_HOME") or pathlib.Path.home() != _REAL_USER_HOME:
            return paths.ambient_agents_dir()
        return root

    for module, attr in _AGENT_SPEC_HOOKS:
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, attr, root, raising=False)
    if paths is not None:
        monkeypatch.setattr(paths, "_agents_dir_override", _pinned_agents_dir)
        # The crewmate folder (``<kiro home>/mates/agents``) is the same lazy shape
        # and now holds the generated mate conductor and worker specs, so it is
        # pinned beside the agents dir. A test that places it itself -- by
        # patching ``kiro_home``, setting ``KIRO_HOME`` or redirecting the home --
        # is followed, exactly as above.
        mates_root = _isolation_dirs("kiro-mates") / "agents"
        real_kiro_home = paths.kiro_home

        def _pinned_mates_dir() -> pathlib.Path:
            if (
                paths.kiro_home is not real_kiro_home
                or os.environ.get("KIRO_HOME")
                or pathlib.Path.home() != _REAL_USER_HOME
            ):
                return paths.kiro_home() / "mates" / "agents"
            return mates_root

        monkeypatch.setattr(paths, "_mate_agents_dir_override", _pinned_mates_dir, raising=False)
    return paths.ambient_agents_dir if paths is not None else None


@pytest.fixture
def unpinned_agent_spec_home(_isolate_agent_spec_home, monkeypatch):
    """Opt out of the agent-spec pin, for a test that ASSERTS on the real layout.

    Two drift guards need the real resolution rather than an isolated copy: one checks
    that ``security``'s ``.kiro/agents`` literal is still the home-relative tail of
    ``kiro_agents_dir()``, the other that ``apps.bridges`` still targets the
    machine-wide registry (which is why a pod may not run ``app``). Pinned to a tmp
    path, both assert about a path that does not ship -- weakening the guard to satisfy
    an isolation fixture, which is backwards. Same call the shared-path ratchet's
    ``_EXCLUDED`` makes for its security anchors.

    READ-ONLY use only. This hands back the real machine-wide location, so a test that
    WRITES through it edits the operator's live agent. Nothing here stops that; the
    two current users only assert.
    """
    for module, attr in _AGENT_SPEC_HOOKS:
        mod = sys.modules.get(module)
        if mod is not None:
            monkeypatch.setattr(mod, attr, None, raising=False)
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_agents_dir_override", None)
    return _isolate_agent_spec_home


#: The operator's real kiro-cli home, captured before any test can repoint
#: ``Path.home``/``KIRO_HOME``. The sessions-dir floor compares against THIS, so a
#: test that relocates the home is followed and only the real store is fenced.
_REAL_KIRO_HOME_AT_START = (pathlib.Path.home() / ".kiro").resolve()


def _is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - unresolvable path cannot be the real home
        return False
    return resolved == root or root in resolved.parents


#: Single-slot memo for :func:`_test_owned_roots`.
#:
#: Both roots are session constants -- pytest memoises its basetemp per worker, and
#: ``_isolate_tempfile_base`` sets the ``tempfile`` base exactly once -- yet the two
#: ``resolve()`` walks below were re-done once per test by the autouse
#: :func:`_isolate_kiro_sessions_dir`, MEASURED at 26.9us and ~6 ``lstat`` calls each,
#: for one answer that cannot change. Keyed on both inputs, so
#: ``KIROCREW_TMP_PER_TEST`` -- the one mode that repoints ``tempfile``'s base per test
#: -- simply misses and pays exactly what it pays today. ONE slot rather than a dict:
#: in that mode an unbounded cache would grow an entry per test.
_OWNED_ROOTS_MEMO: "tuple[tuple[str, str | None], tuple[pathlib.Path, ...]] | None" = None


def _test_owned_roots(tmp_path_factory) -> tuple[pathlib.Path, ...]:
    """This run's own temp trees: pytest's basetemp and the redirected ``tempfile`` base.

    A path under either was created by a fixture or by the test itself, so it is never
    the operator's -- even when the tree sits INSIDE a guarded root. That happens
    routinely: a Kiro Crew agent session sets ``TMPDIR`` to its scratch dir under
    ``~/.kiro/crew/scratch/``, which puts both trees under the real ``~/.kiro``. A fence
    keyed on the real home alone would then redirect a test that relocated kiro-cli's
    home to its OWN ``tmp_path`` away from the tree it just built.

    Memoised on its two inputs; see ``_OWNED_ROOTS_MEMO``. The key reads
    ``tempfile.gettempdir()`` -- the CALL, not the ``tempfile.tempdir`` global -- because
    a test that patches ``gettempdir`` still wins (``_isolate_tempfile_base`` says so),
    and keying on the global would make the memo blind to such a patch. The call costs
    0.09us once ``tempdir`` is set, so honouring it is free.
    """
    global _OWNED_ROOTS_MEMO
    basetemp = tmp_path_factory.getbasetemp()
    try:
        tmp_base: "str | None" = tempfile.gettempdir()
    except OSError:  # pragma: no cover - no usable temp dir at all
        tmp_base = None
    key = (str(basetemp), tmp_base)
    if _OWNED_ROOTS_MEMO is not None and _OWNED_ROOTS_MEMO[0] == key:
        return _OWNED_ROOTS_MEMO[1]
    roots = [basetemp.resolve()]
    if tmp_base is not None:
        try:
            roots.append(pathlib.Path(tmp_base).resolve())
        except OSError:  # pragma: no cover - unresolvable temp base
            pass
    value = tuple(roots)
    _OWNED_ROOTS_MEMO = (key, value)
    return value


@pytest.fixture(autouse=True)
def _isolate_kiro_sessions_dir(_isolation_dirs, _floor_monkeypatch, tmp_path_factory):
    """Pin kiro-cli's transcript store (``<kiro home>/sessions/cli``), for EVERY testpath.

    A fourth ``~/.kiro`` isolation axis, distinct from the data home, the import-time
    ``_SHARED_KIRO_PATHS`` bindings, and the agent-spec home above. ``kiro_sessions_dir()``
    is a LAZY resolver (``kiro_home()`` -> ``$KIRO_HOME`` or ``Path.home()/.kiro``), so
    neither ``KIROCREW_HOME`` nor an import-time path pin reaches it -- the same reason
    the agent-spec home needed its own fixture instead of folding into one of those.

    Without this, a test that reaches session teardown (``AcpSessionHandle.destroy()`` ->
    ``_cleanup_transcript``, which calls ``kiro_sessions_dir()`` directly with no override)
    deletes ``<sid>.json``/``.jsonl`` out of the operator's REAL kiro-cli session store --
    confirmed live: ``sA.json``, ``sA.jsonl``, ``new-sid.json(l)`` and even a fabricated
    ``<MagicMock ...>`` filename were removed from ``~/.kiro/sessions/cli`` by
    ``test_acp_runtime.py`` and ``test_session_transfer.py`` session-teardown tests and
    ``test_dashboard_chat_rewind.py``'s chained-history tests.

    Installs the override via the single accessor's function BODY, exactly like
    :func:`_isolate_agent_spec_home` does for ``_agents_dir_override``: ``kiro_sessions_dir``
    is bound by name in several modules (``session_map``, ``acp.client``,
    ``dashboard.session_transfer``, ...), and copies the function OBJECT, so patching only
    ``paths.kiro_sessions_dir`` would never reach them.

    ``session_map.py`` additionally exposes its OWN opt-in override
    (``_KIRO_SESSIONS_DIR``, ``None`` = defer to ``kiro_sessions_dir()``) for existing
    monkeypatch call sites -- pinned here too so a test that reads through that module
    sees the same tree as one that reads through the resolver directly.

    Creates nothing -- an absent sessions dir is the normal fresh-install state, and
    every writer creates its own parent first. A test that sets its own value still wins,
    through any lever: ``monkeypatch`` applied later in setup overrides both hooks, and a
    test that relocates kiro-cli's home itself -- ``KIRO_HOME``, ``HOME``, or a patched
    ``Path.home`` -- is followed there rather than pinned, because the override only
    fences the operator's REAL store (see ``_pinned_sessions_dir``). A test that asserts
    on the real default layout opts out with :func:`unpinned_kiro_sessions_dir`.
    """
    monkeypatch = _floor_monkeypatch
    root = _isolation_dirs("kiro-sessions-cli")
    # Imported unconditionally, never patch-if-imported: this floor guards a DELETE
    # path, and a test that imports its subject inside its own body would otherwise
    # find no module at setup, leave the override ``None``, and remove transcripts
    # from the operator's real store at teardown -- the exact failure the floor
    # exists to close. ``config.paths`` is the stdlib-only leaf, so the import costs
    # milliseconds once per worker; ``ImportError`` is tolerated so a partial
    # checkout cannot break collection (a module that cannot import has no seam).
    try:
        paths = importlib.import_module("kiro_crew.config.paths")
    except ImportError:  # pragma: no cover - partial checkout
        paths = None
    if paths is not None:
        real_kiro_home = paths.kiro_home
        own_roots = _test_owned_roots(tmp_path_factory)

        def _test_owned(path: pathlib.Path) -> bool:
            return any(_is_under(path, owned) for owned in own_roots)

        def _pinned_sessions_dir() -> pathlib.Path:
            # A GUARD, not a redirect. The lazy resolver is deliberately left to
            # each test (see the rootdir conftest's docstring): ~35 tests relocate it with
            # ``patch("pathlib.Path.home", ...)`` or ``KIRO_HOME``, and they must
            # keep reading the tree they just built. So resolve it the way
            # production would, and only when that lands in the operator's REAL
            # ``~/.kiro`` -- the one place a test may never delete from -- answer
            # the isolation dir instead.
            unpinned = real_kiro_home() / "sessions" / "cli"
            if _is_under(unpinned, _REAL_KIRO_HOME_AT_START) and not _test_owned(unpinned):
                return root
            return unpinned

        monkeypatch.setattr(paths, "_sessions_dir_override", _pinned_sessions_dir)
    # ``session_map`` defers to ``kiro_sessions_dir()`` when its own override is
    # unset, so the resolver pin above is the floor; this only keeps a module that IS
    # loaded reading the same tree through its own lever.
    session_map = sys.modules.get("kiro_crew.session_map")
    if session_map is not None:
        monkeypatch.setattr(session_map, "_KIRO_SESSIONS_DIR", root, raising=False)


@pytest.fixture
def unpinned_kiro_sessions_dir(_isolate_kiro_sessions_dir, monkeypatch):
    """Opt out of the sessions-dir pin, for a test that ASSERTS on the real layout.

    The two drift guards in ``test_agent_home_isolation`` check that the resolver
    still derives the transcript store from ``kiro_home()`` -- pinned, they would
    assert about a tmp path that does not ship. READ-ONLY use only: this hands back
    the operator's real store, and a test that deletes through it is the harm the
    pin exists to prevent.
    """
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_sessions_dir_override", None)
    session_map = sys.modules.get("kiro_crew.session_map")
    if session_map is not None:
        monkeypatch.setattr(session_map, "_KIRO_SESSIONS_DIR", None, raising=False)
