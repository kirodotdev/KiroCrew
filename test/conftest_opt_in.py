"""Helpers and opt-in fixtures a test imports or requests by name: reparse
links, host-absolute paths, environment and walk ceilings, store and loader closers,
short socket dirs, and pinned host readings.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own and ``from conftest import ...`` keeps working;
see the import block there. Plain module, never collected.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import pytest

from kiro_crew import platform_compat


def make_escaping_link(inside: pathlib.Path, outside: pathlib.Path) -> str:
    """Create a reparse link inside ``inside`` pointing at ``outside``.

    Returns the ``inside``-relative path of a file reached THROUGH the link, for
    tests asserting that a canonical-containment check (resolve +
    is_relative_to) catches a link escaping a sandbox root. ``outside`` must
    already contain a file named ``secret.py``.

    A file symlink needs SeCreateSymbolicLinkPrivilege on Windows, which an
    unelevated developer shell lacks (WinError 1314) even though CI runners hold
    it. A directory junction needs NO privilege and resolves through the same
    reparse machinery, so the containment assertion stays exercised locally
    instead of being skipped.
    """
    if platform_compat.IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(outside), str(inside / "linked"))
        return "linked/secret.py"
    (inside / "link.py").symlink_to(outside / "secret.py")
    return "link.py"


def plant_day_link(link: pathlib.Path, secret_file: pathlib.Path) -> None:
    """Plant a reparse point at the dated history name ``link`` that leads outside.

    The property under test is that a memory reader never publishes bytes that
    live outside the memory tree when the agent-writable dated ``.md`` name is a
    reparse point. On POSIX the planted shape is a FILE symlink to
    ``secret_file``, the exact credential-exfiltration vector. On Windows a file
    symlink needs SeCreateSymbolicLinkPrivilege, so the stand-in is a directory
    JUNCTION at ``link`` pointing at ``secret_file.parent``: a junction needs no
    privilege, is a reparse point at the same dated name, and the guarded reader
    refuses it through ``is_link_or_junction`` and its non-regular check, so the
    outside directory's contents can never be read as a day. Both variants keep
    the assertion running on every CI platform instead of skipping it.
    """
    if platform_compat.IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(secret_file.parent), str(link))
        return
    link.symlink_to(secret_file)


def host_abs(*parts: str) -> str:
    """A fixture path that is absolute on THIS host: ``/opt/shims`` or ``C:\\opt\\shims``.

    Production filters and validates paths with ``os.path.isabs`` -- spec PATH
    entries, trusted binaries, upload references, socket paths -- and from
    Python 3.13 ``ntpath.isabs`` rejects a bare leading slash (a path
    without a drive is relative to the current drive). A POSIX literal such as
    ``"/usr/bin"`` therefore changes meaning per interpreter on Windows: absolute
    on 3.12, relative on 3.13, so a test written with one silently exercises the
    rejection branch there. Spell fixtures through this helper instead; it touches
    no filesystem, and its result is what ``os.path.isabs`` accepts everywhere.
    """
    return os.path.abspath(os.path.join(os.sep, *parts))


def forget_env_at_teardown(monkeypatch, *names: str) -> None:
    """Make ``monkeypatch`` remove *names* from ``os.environ`` at teardown.

    For a variable the code under test is about to WRITE (a saved channel token
    exported for the running gateway, a ``PORT`` a booted server publishes, a
    ``--env`` a CLI applies), neither obvious spelling restores the environment:

    * ``monkeypatch.delenv(name, raising=False)`` BEFORE the write records nothing
      when the variable is absent -- pytest only records an undo for a key that
      existed -- so the value written later survives the test;
    * ``monkeypatch.delenv(name)`` AFTER the write records the written value as
      the thing to restore, so teardown puts the token BACK.

    Both shapes were found leaking across tests in a full run. This records the
    current state (absent or present) as the undo, so teardown returns the
    variable to exactly what it was before the test, whatever the test wrote.
    """
    for name in names:
        if name in os.environ:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, "")  # records "was absent" as the undo
            monkeypatch.delenv(name)


def cap_project_root_walk(monkeypatch, ceiling: pathlib.Path) -> None:
    """Make ``kiro_crew.artifact_source`` see NO project root above ``ceiling``.

    ``classify_source`` walks up from a file looking for ``PROJECT_ROOT_MARKERS``
    (``.git``, ``Makefile``, ``package.json``, ``.kiro``, ...), so a test that
    asserts COPY for "a plain directory" under ``tmp_path`` is also asserting
    that nothing ABOVE ``tmp_path`` carries a marker. That is not the test's to
    decide: pytest's temp root sits wherever ``TMPDIR`` points, and a checkout or
    a ``.kiro`` workspace a few levels up turns the whole temp tree into a
    project. Observed with ``TMPDIR`` under ``~/.kiro/crew/workspace``: every
    such assertion answered LINK to that workspace instead of COPY.

    Directories outside ``ceiling`` report no marker; inside it the real probe
    runs, so the markers a test plants (``proj/.git``) still count. Pair it with
    the ``_tempdir`` narrowing these tests already do -- the two seams together
    make the rest of ``tmp_path`` ordinary, UNMARKED filesystem.
    """
    from kiro_crew import artifact_source

    real_marker = artifact_source.project_root_marker
    top = os.path.normcase(os.path.realpath(str(ceiling)))

    def _capped(directory: str) -> str | None:
        here = os.path.normcase(os.path.realpath(directory))
        if here != top and not here.startswith(top + os.sep):
            return None
        return real_marker(directory)

    monkeypatch.setattr(artifact_source, "project_root_marker", _capped)


def cap_node_module_walk(monkeypatch, ceiling: pathlib.Path) -> None:
    """Make the ACP adapter resolvers' ``node_modules`` walk stop at ``ceiling``.

    ``acp.client._node_module_search_dirs`` walks every ancestor of an adapter
    entry's real path the way Node resolves a bare import -- to the filesystem
    root. A test that asserts "this adapter's dependency is reachable NOWHERE" is
    therefore also asserting that no ``node_modules`` above ``tmp_path`` holds it,
    which is the host's to decide, not the test's: a ``TMPDIR`` inside a checkout
    that ran ``npm install`` at its root would make the refusal pass. Directories
    outside ``ceiling`` are dropped from the walk; inside it the real walk runs, so
    the ``node_modules`` a test plants beside a link target still counts.
    """
    from kiro_crew.acp import client as acp_client

    real_walk = acp_client._node_module_search_dirs
    top = os.path.normcase(os.path.realpath(str(ceiling)))

    def _capped(start: pathlib.Path):
        for node_modules in real_walk(start):
            here = os.path.normcase(os.path.realpath(str(node_modules)))
            if here == top or here.startswith(top + os.sep):
                yield node_modules

    monkeypatch.setattr(acp_client, "_node_module_search_dirs", _capped)


def absent_sysconf(name):
    """Stand-in for a missing ``os.sysconf`` (Windows has none).

    A test that fakes ONE ``os.sysconf`` name must delegate every other name to
    the real function -- and on Windows there is no real function to delegate to.
    Capturing ``getattr(os, "sysconf", absent_sysconf)`` gives the delegating fake
    the same "unavailable" answer production sees there, instead of an
    ``AttributeError`` at capture time.
    """
    raise ValueError(f"os.sysconf unavailable for {name!r}")


def drain_breadcrumb_writes(timeout: float = 5.0) -> None:
    """Block until every queued safety-override breadcrumb publish has run.

    ``safety_override.flush_breadcrumb_writes`` is production's best-effort
    drain and reports a bool; a test that relies on the drain to prove the
    write landed inside its own context needs certainty, so a drain that does
    not complete raises instead of returning a value a fixture could ignore.
    """
    from kiro_crew.safety_override import flush_breadcrumb_writes

    if not flush_breadcrumb_writes(timeout):
        raise TimeoutError(
            f"breadcrumb worker did not drain within {timeout}s; a queued publish "
            "may still run after this test's fixtures tear down"
        )


@pytest.fixture
def opened():
    """Close every store a test hands it, whichever way the test ends.

    ``store = opened(VectorMemoryStore(...))`` / ``opened(KnowledgeStore(...))`` /
    ``opened(SkillsLoader(...))``: the object comes back unchanged and is closed
    at teardown in reverse order of registration. An unclosed
    ``sqlite3.Connection`` is a reference cycle on CPython 3.11+ (its statement
    cache is an ``lru_cache`` wrapping the connection itself), so a store that is
    merely dropped keeps its ``db``/``-wal``/``-shm`` descriptors until the cyclic
    collector runs -- the tenth hygiene pass measured 52 tests at +5..+9
    descriptors from exactly that. A ``KnowledgeStore`` hands each THREAD its own
    connection, and ``close()`` releases only the calling thread's, so for it the
    test-only every-thread seam is used; every other store closes through
    ``close()``.
    """
    stores: list = []

    def _track(store):
        stores.append(store)
        return store

    yield _track
    for store in reversed(stores):
        closer = getattr(store, "_close_all_for_tests", None) or store.close
        closer()


@pytest.fixture
def close_skills_loaders(monkeypatch):
    """Close every ``SkillsLoader`` built while the test runs, the default one included.

    Construction opens the skill search index (a SQLite connection: ``db`` +
    ``-wal`` + ``-shm``) and the first discovery starts the ``skill-catalog-refresh``
    worker; production closes both by exiting, and a ``ContextBuilder`` built
    inline in a test never does, so every builder leaked three descriptors and a
    thread -- sixty of each from the twenty examples of one property test. Tracks
    every instance through ``SkillsLoader.__init__`` and releases it at teardown,
    the shape ``test_spawn_reasoning_effort`` uses for managers. Opt-in, not
    autouse: patching the constructor for all 126k tests to serve the few dozen
    that build loaders inline is not worth its cost, so a module that builds
    ``ContextBuilder``s requests it from a one-line module-level autouse fixture.
    """
    from kiro_crew.skills import SkillsLoader

    created: list = []
    orig_init = SkillsLoader.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(SkillsLoader, "__init__", _tracking_init)
    try:
        yield
    finally:
        for loader in created:
            loader.close()
        # ``close()`` wakes the ``skill-catalog-refresh`` worker to exit but does
        # not join it, by design (a daemon walk may outlive a short-lived loader
        # in production). A per-test thread probe reads the still-exiting worker
        # as a leak, so the join here -- bounded -- is what proves it left.
        for loader in created:
            worker = loader._catalog_worker
            if worker is not None:
                worker.join(timeout=_CATALOG_WORKER_JOIN_SECS)


#: Ceiling for joining a closed loader's catalog worker; it exits on its next wake.
_CATALOG_WORKER_JOIN_SECS = 5.0


@pytest.fixture
def close_subagent_managers(monkeypatch):
    """Close every ``SubagentManager`` built while the test runs.

    Construction opens the durable task queue (``tasks.db`` + ``-wal`` + ``-shm``,
    inline under the ``open_store_off_loop=False`` floor in ``conftest_resets``) and ``cancel_all``
    never releases it, so a manager a test merely drops keeps three descriptors
    until the cyclic collector runs -- a macOS sweep measured +3..+9
    per test, GC-timed, across four files that build managers inline. Tracks every
    instance through ``SubagentManager.__init__`` and calls ``close()`` at
    teardown. Opt-in like ``close_skills_loaders`` and for the same reason; a
    module that builds managers inline requests it from a one-line module-level
    autouse fixture instead of carrying its own copy of this body.
    """
    import kiro_crew.subagent as _subagent_mod

    created: list = []
    orig_init = _subagent_mod.SubagentManager.__init__

    def _tracking_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(_subagent_mod.SubagentManager, "__init__", _tracking_init)
    try:
        yield
    finally:
        for mgr in created:
            try:
                mgr.close()
            except Exception:  # pragma: no cover - a half-built manager
                pass


@pytest.fixture
def short_sock_dir(tmp_path):
    """A temp dir short enough to hold an AF_UNIX socket path.

    ``sockaddr_un.sun_path`` caps a unix-socket path at ~104 bytes on macOS (108
    on Linux). pytest's ``tmp_path`` is derived from the platform temp root plus
    the test name, and on macOS that root is ``/private/var/folders/<...>/T``,
    which already blows the cap before a filename is appended — so binding under
    ``tmp_path`` fails with ``OSError: AF_UNIX path too long`` on a developer
    machine while passing in CI (Linux, short ``/tmp``).

    Yields a short-rooted dir instead, cleaned up afterwards. Falls back to
    ``tmp_path`` where no short root exists (notably Windows, where AF_UNIX tests
    are skipped anyway), so this never hard-fails on an unusual platform.

    The root comes from ``tmpdir_helpers.short_tmp_base()`` -- the ONE seam the suite
    has for "short enough for ``sun_path``" -- and not from a literal ``/tmp`` spelled
    here. A second spelling of the same platform rule is what let the two drift: this
    fixture kept creating ``/tmp/kcsock-XXXX`` directories that no run owned, while the
    helper was the place the run-owned short root could be introduced once.
    """
    import tempfile

    from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

    short_root = short_tmp_base()
    if short_root is None or not os.path.isdir(short_root):
        yield tmp_path
        return
    path = tempfile.mkdtemp(dir=short_root, prefix=SHORT_TMP_PREFIX + "unixsock-")
    try:
        yield pathlib.Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def named_cron_caller(monkeypatch):
    """Give the calling test an identity the cron MCP server can resolve.

    ``mcp_cron`` refuses a WRITE from a caller it cannot name (see
    ``_unidentified_caller_refusal``): on a pooled backend an unidentified stub
    shares the process with identified ones, so granting it authority over stored
    rows would let it reach another session's jobs.

    Tests about cron's FIELD handling -- schedules, channels, models, validation
    -- have always assumed a caller the gateway vouches for; they simply never
    said so. This
    states the precondition. A test that is actually ABOUT the unidentified
    caller must not use this fixture.

    Yields the key it set, so a test that mocks ``CronService`` can stamp the same
    owner onto its fake job instead of hardcoding this value.
    """
    key = "dashboard:conftest-slot"
    monkeypatch.setenv("KIROCREW_SESSION_KEY", key)
    return key


#: Comfortably clear of the memory floor ``SubagentManager.spawn`` admits on at
#: defaults (``agent.spawn_min_memory_gb``, 2 GB after a start of up to 1 GB,
#: plus the starts still warming). A HEALTHY host, not an infinite one: the
#: floor still compares against this figure, so a test asking for more than it
#: is refused the way a real 8 GB host would refuse it.
_HEALTHY_AVAILABLE_GB = 8.0


@pytest.fixture
def healthy_host_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the host-memory reading ``SubagentManager.spawn`` consults.

    ``spawn`` queues -- returning before it registers anything in ``_tasks`` --
    whenever the machine looks short of memory: the absolute floor
    (``check_memory_available`` against ``agent.spawn_min_memory_gb``). It reads
    the host the suite happens to be running on, so without this the verdict is
    the operator's machine rather than the test's own input.

    The failure it produces is misleading, which is why it is worth a shared
    fixture: a queued spawn IS a ``SubagentInfo``, so ``assert info is not None``
    still passes and the test dies one line later on ``mgr._tasks[info.id]``
    with a bare ``KeyError``. Measured on a CI runner with ~0.5 GB free.

    Only the HOST reading is pinned: a caller that names its own ``path`` is
    feeding the ``/proc/meminfo`` parser a fixture file rather than asking about
    this machine, so it still runs the real function and a parser regression
    still goes red. A test that is actually ABOUT the guard patches it in its
    own body, which lands on top of this and reverts to it.

    The pinned host has ``_HEALTHY_AVAILABLE_GB`` free and the floor is still
    compared against it: ``(8.0 >= min_gb, 8.0)``, the real reader's answer on
    such a host. Answering ``True`` whatever was asked would let a test pass on
    a bar no 8 GB machine clears -- a wave whose reserve outgrew the host would
    read as admitted here and as queued on the operator's.
    """
    import kiro_crew.subagent as subagent
    from kiro_crew.constants import DEFAULT_SPAWN_MIN_MEMORY_GB

    real_check = subagent.check_memory_available

    def _pinned_check(min_gb: float | None = None, path: str | None = None) -> tuple[bool, float]:
        if path is None:
            floor = DEFAULT_SPAWN_MIN_MEMORY_GB if min_gb is None else min_gb
            return (_HEALTHY_AVAILABLE_GB >= floor, _HEALTHY_AVAILABLE_GB)
        if min_gb is None:
            return real_check(path=path)
        return real_check(min_gb=min_gb, path=path)

    monkeypatch.setattr(subagent, "check_memory_available", _pinned_check)


@pytest.fixture
def isolate_mcp_host_paths(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``apply_import``'s MCP sidecar lock off the real ``~/.kiro/settings``.

    ``_write_mcp`` takes the dashboard handler's lock, whose paths the handler
    binds from ``Path.home()`` when it is imported. The host floor rebinds them
    only when that module is already loaded, so without this the first MCP write
    in a worker reaches the operator's real files. A test that patches these names
    itself still wins, because its own patch runs after this one.

    Opt in per module with
    ``pytestmark = pytest.mark.usefixtures("isolate_mcp_host_paths")``; the
    onboarding-import test modules do.
    """
    import importlib

    mcp_handlers = importlib.import_module("kiro_crew.dashboard.handlers.mcp")
    global_mcp = tmp_path / "host-kiro-settings" / "mcp.json"
    monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", global_mcp)
    monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", global_mcp.with_suffix(".lock"))


@pytest.fixture
def ample_host_free_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the free-memory reading behind the memory posture to a healthy host.

    ``resource_status._read_available_gb`` is the live input of the posture
    that ``admission_check`` and ``prewarm_allowance`` act on. While it reads
    critical, the cron due-scan defers interval and one-shot firings and the
    chat runner holds no eager pre-warm session. A test that fires a cron job
    or an eager spawn then fails on a runner with 2 GB or less free, with no
    hint that memory is involved: ``svc._claims[job_id]`` raises a bare
    ``KeyError``, a wait times out, or ``get_or_create`` is awaited 0 times.
    Seen on the v0.9.0-insider.1 release run (8 failures that pass on a host
    with free memory).

    The reading is pinned, not ``probe`` itself, because ``prewarm_allowance``
    reads it without going through ``probe``. Only this reading is pinned: a
    test about the slice, cgroup or host readers below it must not opt in,
    since this hides them. A test about the posture patches the reader in its
    own body, on top of this.

    Opt in per module with
    ``pytestmark = pytest.mark.usefixtures("ample_host_free_memory")``.
    """
    import kiro_crew.resource_status as resource_status

    monkeypatch.setattr(resource_status, "_read_available_gb", lambda: _HEALTHY_AVAILABLE_GB)


@pytest.fixture
def ample_host_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``resource_status.probe`` to AMPLE so no turn gains a ``[RESOURCES]`` line.

    ``ContextBuilder.build_message`` prepends a ``[RESOURCES]`` advisory whenever
    the host's own memory reading is tight or critical. Every test that asserts on
    the SHAPE of a built turn -- that it opens with the user's text, with a hook
    prefix, or with nothing at all -- therefore has the runner's free memory as a
    hidden input, and fails with the advisory glued to the front of the string it
    compared.

    Not a platform gap: a macos-15 runner under a 3-way xdist split is simply the
    first host observed under the threshold, and a loaded Linux runner reaches the
    same state. The probe is imported INSIDE ``build_message``, so patching it on
    its own module is what that call resolves.

    A test that is actually ABOUT the advisory patches the probe in its own body,
    which lands on top of this and reverts to it.
    """
    import kiro_crew.resource_status as resource_status

    def _ample(cfg: object | None = None) -> "resource_status.ResourceStatus":
        return resource_status.ResourceStatus(
            available_gb=_HEALTHY_AVAILABLE_GB,
            cpu_count=4,
            load_per_cpu=0.1,
            posture=resource_status.POSTURE_AMPLE,
            pressure_gb=4.0,
            critical_gb=2.0,
        )

    monkeypatch.setattr(resource_status, "probe", _ample)
