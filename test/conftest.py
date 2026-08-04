"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import socket
import sys

import pytest
from hypothesis import HealthCheck, settings

from kiro_crew import sel as _sel
from kiro_crew.safety_override import reset_singleton as _reset_safety_override
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler import _PHASE_EMOJIS, _build_phase_emojis

# ── Hypothesis profiles ─────────────────────────────────────────────────
# Default (CI): fast iteration.  Run ``HYPOTHESIS_PROFILE=thorough python -m pytest``
# for deeper coverage.
settings.register_profile("default", max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None)
settings.register_profile("thorough", max_examples=100)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))

# Ensure .hypothesis/tmp exists (build environment may not have it)
os.makedirs(os.path.join(os.path.dirname(__file__), "..", ".hypothesis", "tmp"), exist_ok=True)

_HAS_GIT = shutil.which("git") is not None

requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git not available")

# ── Windows CI ──────────────────────────────────────────────────────────
# The backend runs natively on Windows (kiro_crew.platform_compat), but a
# handful of suites exercise POSIX-only-by-design features (OS-level
# sandbox, process groups / PGID semantics, PTY, AF_UNIX sockets -- see
# docs/windows-install.md's per-feature table). Skip collecting them on
# Windows rather than marking test-by-test: several fail at import or
# fixture time on win32.
from kiro_crew import platform_compat  # noqa: E402

if platform_compat.IS_WINDOWS:
    collect_ignore = [
        "test_harness.py",  # spawns real gateways; PGID + socketpair plumbing
        "test_sandbox_argv.py",  # OS sandbox is POSIX-only
        "test_sandbox_cc_mode.py",  # OS sandbox is POSIX-only
        "test_pid_lifecycle.py",  # process-group semantics
        "test_pid_sweep_helpers.py",  # process-group semantics
        "test_process_tree_kill.py",  # killpg/getpgid semantics
        "test_source_providers.py",  # providers require the POSIX sandbox
        # Feature-parity gaps observed on the first Windows runs -- each is a
        # POSIX-shaped feature or test suite tracked as a Windows follow-up:
        "test_terminal_handler.py",  # PTY web terminal is POSIX-only
        "test_acp_client.py",  # asserts os.killpg/SIGKILL process control
        "test_stop_kill_cancel.py",  # killpg/getuid kill-path semantics
        "test_app_backend_stale_reap.py",  # getpgid/killpg reaping semantics
        "test_env.py",  # krb5 ccache resolver is Linux-only (getuid paths)
        "test_outbox_notify_broadcast.py",  # hardcoded /tmp outbox paths
        "test_outbox_binary.py",  # hardcoded /tmp outbox paths
        "test_deploy_web_handlers.py",  # deploy staging is POSIX-shaped (/tmp, uid)
        "test_snapshot.py",  # replace-while-open (WinError 32) semantics
        "test_theme_install.py",  # POSIX readable/mode gate rejects NTFS modes
        "test_webapp_preview.py",  # preview routes 404 on Windows backends
        "test_file_raw.py",  # 0o600/0o644 mode-bit assertions
        "test_file_download.py",  # 0o600/0o644 mode-bit assertions
        "test_dashboard_file_io.py",  # 0o600/0o644 mode-bit assertions
        "test_dev_fleet_app.py",  # POSIX app-runner assumptions
    ]


def pytest_collection_modifyitems(config, items):
    """On Windows, skip the tracked known-gap tests (all parametrizations).

    The list lives in test/windows-expected-failures.txt -- one
    unparametrized node id per line, captured from the first Windows CI
    runs. It is a burn-down backlog: fixed tests get their line deleted,
    and anything NOT on the list still fails the job, so the Windows line
    holds for the ~16k tests that pass today.
    """
    if not platform_compat.IS_WINDOWS:
        return
    listfile = os.path.join(os.path.dirname(__file__), "windows-expected-failures.txt")
    with open(listfile, encoding="utf-8") as fh:
        expected = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    marker = pytest.mark.skip(
        reason="known Windows gap -- tracked in test/windows-expected-failures.txt"
    )
    for item in items:
        if item.nodeid.split("[")[0] in expected:
            item.add_marker(marker)


@pytest.fixture(autouse=True)
def _windows_restrict_to_owner_stub(request, monkeypatch):
    """On Windows, no-op the icacls secret lockdown for hermetic tests.

    Many tests stub ``subprocess.run`` (or strip PATH) for hermeticity;
    ``restrict_to_owner``'s whoami/icacls spawns then fail and its
    DELIBERATE fail-loud OSError cascades into hundreds of unrelated
    tests. The real Windows implementation keeps direct coverage in
    test_platform_compat / test_spawn_audit (exempted here) and the
    POSIX chmod path keeps full coverage on the Linux matrix. Product
    call sites that bound the symbol by value (tips.py) are unaffected
    by this module-attr patch -- acceptable: they surface as at most a
    handful of failures, handled individually.
    """
    if not platform_compat.IS_WINDOWS or request.module.__name__ in (
        "test_platform_compat",
        "test_spawn_audit",
    ):
        yield
        return
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_aim_skills_dir(monkeypatch):
    """Prevent SkillsLoader from discovering edition-contributed skill roots.

    SkillsLoader now sources extra skill roots from the CPP seam
    ``McpToolingProvider.extra_skills()`` (public Default ``[]``) rather than a
    hardcoded ``~/.aim/skills``. Pin the Default to ``[]`` so a developer with a
    composed companion (or leftover roots) can't inflate session context beyond
    _MAX_CONTEXT_CHARS and cause silent truncation / non-deterministic xdist
    failures.

    Does NOT request ``tmp_path``: this fixture only patches a method, and being
    autouse it made every one of the suite's ~26k tests allocate a temp directory it
    never touched -- the single largest fixed cost in the suite's setup path.
    """
    from kiro_crew.platform.defaults import DefaultMcpToolingProvider

    monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


def pytest_configure(config: pytest.Config) -> None:
    """Pre-import ``tracemalloc`` so pytest's unraisable hook can't crash on it.

    pytest's ``_pytest/unraisableexception`` plugin replaces ``sys.unraisablehook``
    and, when a leaked object (an un-awaited coroutine, an orphaned
    ``SessionManager._cleanup_loop`` task, etc.) is garbage-collected, calls
    ``tracemalloc_message()`` which runs ``import tracemalloc`` *from inside the
    GC callback*. If ``tracemalloc`` has not been imported yet, that first import
    lands in a partially-initialized state (a CPython circular-import artifact
    observed on 3.12) and raises ``AttributeError: partially initialized module
    'tracemalloc' has no attribute 'get_object_traceback'``. pytest then re-raises
    it as ``RuntimeError: Failed to process unraisable exception`` and reports it
    as an ERROR at the *next* test's setup — turning a benign "object was never
    awaited" warning into a hard build failure that lands on an innocent test.

    Importing the module eagerly here (once per xdist worker, before any test
    runs or any GC fires) makes the hook's ``import tracemalloc`` a no-op
    ``sys.modules`` hit against a fully-built module, so leaks degrade back to
    warnings instead of failing the suite. Touch ``get_object_traceback`` to
    force full initialization and to keep the import from reading as unused.
    """
    import tracemalloc

    assert hasattr(tracemalloc, "get_object_traceback")

    # ── Sandbox probe prewarm ───────────────────────────────────────────────
    # Production processes (gateway, gatewayd) call prewarm_backend() at boot so
    # the sandbox probe cache is populated off-loop before any on-loop handler
    # runs.  pytest-xdist workers have no equivalent boot path, so whichever
    # aiohttp-route test first exercises wrap_argv() on a cold worker hits the
    # "never probe on event loop" guard and gets a transient failure.  Mirror the
    # production prewarm here: one synchronous detect_backend() per worker
    # process, populating the cache before any test runs.
    #
    # Tests that intentionally reset_backend() in their own fixtures
    # (test_sandbox_backend_cache.py) are unaffected — they manage their own
    # cache state.
    try:
        from kiro_crew.sandbox import detect_backend

        detect_backend()
    except Exception:
        pass  # Probe failure must not break unrelated tests.


_MAX_WORKERS_ENV = "KIROCREW_MAX_TEST_WORKERS"
_SLOT_DIR_ENV = "KIROCREW_TEST_SLOT_DIR"
_DEFAULT_WORKER_CAP = 32
_GIB = 1024**3
# Headroom to reserve per worker. Measured peak RSS for a worker on a heavy
# 1,231-test subset is ~0.37 GiB (~0.50 GiB under --cov), so 2 GiB reserves
# roughly 4x typical and keeps the term from binding on ordinary hosts while
# still refusing to spawn, say, 32 workers on an 8 GiB machine. Note this sizes
# for EXPECTED footprint: it cannot save a host from a genuinely leaking worker
# (one orphaned run was observed at 4.3 GiB RSS), which is a separate bug.
_GIB_PER_WORKER = 2

# Lock files this process holds for its whole lifetime -- the fds MUST stay open,
# because the lock lives exactly as long as the fd does.
_held_slots: list[int] = []


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _slot_root() -> pathlib.Path:
    override = os.environ.get(_SLOT_DIR_ENV)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".cache" / "kirocrew" / "test-slots"


def _host_key() -> str:
    """Filesystem-safe identity for this machine, as a single path segment."""
    raw = socket.gethostname() or ""
    safe = "".join(ch if (ch.isalnum() or ch in "-._") else "_" for ch in raw)[:64]
    return safe.strip(".") or "unknown-host"


def _slot_dir() -> pathlib.Path:
    """Where concurrent pytest runs ON THIS HOST contend for worker capacity.

    Deliberately host-global and *not* derived from ``KIROCREW_HOME``: the point
    is that two worktrees -- which have different homes and know nothing about
    each other -- still coordinate over the one thing they truly share, the
    machine's cores and RAM.

    Scoped by hostname because ``~/.cache`` is frequently a network home shared
    by many machines, whose contention is not ours.
    """
    return _slot_root() / _host_key()


def _slot_path(slot_dir: pathlib.Path, index: int) -> pathlib.Path:
    return slot_dir / f"worker-{index:03d}.lock"


def _host_total_gib() -> int:
    """Total physical RAM in GiB, or 0 when it cannot be determined."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    if pages <= 0 or page_size <= 0:
        return 0
    return int(pages * page_size // _GIB)


def _claim_worker_slots(capacity: int, cap: int) -> int:
    """Take up to ``cap`` of the host's ``capacity`` worker slots and HOLD them.

    ``capacity`` is how many slots the HOST has (cores, bounded by RAM) and is
    the range probed; ``cap`` is the most any single run may take. Keeping these
    separate matters on a large host: with 64 cores and a cap of 32, a first run
    takes slots 0-31 and a second still finds 32-63 free and gets its full 32.
    Probing only ``cap`` slots would have collapsed that second run to one
    worker while half the machine sat idle.

    Each slot is an advisory lock on its own file, acquired non-blocking and
    never released until the process exits. That is the whole design: the kernel
    owns the lease, so capacity returns automatically when a run ends --
    including a run that is orphaned or terminated outright, which is exactly
    the state that caused the incident this budget exists to prevent.

    This deliberately replaces an earlier design where runs wrote reservation
    FILES describing themselves. Files outlive their owners, so that version
    needed PID-liveness probing, a staleness backstop, ownership proof against
    look-alike files, and boot/suspend forensics to decide when a reservation
    was defunct -- and every one of those cleanup paths produced a real bug. A
    held lock needs none of it.

    Returns the number of slots taken, at least 1: a run arriving at a genuinely
    full host proceeds single-worker rather than stalling.
    """
    if _held_slots:
        # Already claimed in this process. Re-locking would fail: flock treats
        # two fds on one file as independent even within the same process, so a
        # second pass would take nothing and collapse the run to one worker.
        return len(_held_slots)
    try:
        # Resolution itself must be inside the guard: Path.home() raises
        # RuntimeError when the home directory cannot be determined, and
        # gethostname() can raise OSError. Neither may break pytest startup.
        root = _slot_root()
        slot_dir = _slot_dir()
        # Refuse a symlink at either level: the root is caller-supplied via
        # KIROCREW_TEST_SLOT_DIR and could redirect our writes.
        if root.exists() and root.is_symlink():
            return min(capacity, cap)
        slot_dir.mkdir(parents=True, exist_ok=True)
        if slot_dir.is_symlink() or not slot_dir.is_dir():
            return min(capacity, cap)
    except (OSError, RuntimeError, ValueError):
        return min(capacity, cap)  # fail open to the unbudgeted ceiling

    taken = 0
    for index in range(capacity):
        if taken >= cap:
            break
        try:
            fd = os.open(str(_slot_path(slot_dir, index)), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            break
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            _held_slots.append(fd)  # keep the fd -- closing it drops the lock
            taken += 1
        else:
            os.close(fd)
    return max(1, taken)


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Budget the worker count for ``-n auto`` (and ``-n logical``).

    Two separate quantities.

    **Host capacity** -- how many slots exist to compete for:

    1. **Cores.** ``os.cpu_count()``.
    2. **RAM.** ~2 GiB per worker. Cores alone are the wrong unit: a 10-core /
       32 GiB laptop cannot back 32 multi-GiB workers, and once it starts
       swapping the run stops making progress altogether.

    **Per-run cap** (``KIROCREW_MAX_TEST_WORKERS``, default 32) -- the most any
    single run may take. The optimal worker count for this suite plateaus around
    24-32 and then *regresses*: every extra worker re-imports the full app
    (aiohttp/boto3/numpy/pdfplumber/transcribe) and writes its own
    ``.coverage.*`` file to combine at the end. Measured on a 64-core host:
    156s @ 64 workers vs 92s @ 32 workers (-41%).

    Keeping them separate is what makes a big host behave: with 64 cores and a
    cap of 32, two runs get 32 workers each rather than the second collapsing
    while half the machine idles.

    Sharing is what stops the failure this hook was extended for. Two worktrees
    each running ``-n auto`` on a 10-core box previously took 10 workers *each*,
    and the resulting swap thrash produced a load average of ~590 with zero
    tests completing in 21 minutes. Now each run holds a lock per worker it
    intends to spawn (under ``~/.cache/kirocrew/test-slots/<hostname>``, root
    overridable with ``KIROCREW_TEST_SLOT_DIR``): a run alone takes the whole
    machine, and a later run takes only what is unlocked. The locks are held for
    the process's lifetime and released by the kernel when it exits, so an
    orphaned or terminated run frees its share with no cleanup logic at all.

    The cost is fairness, not safety, and only when the host is GENUINELY full:
    a late run arriving at a fully-locked machine drops to its floor of one
    worker -- slow, but never stalled, and never oversubscribing the host the
    way the incident did. While free capacity remains, a later run gets its full
    share.

    An explicit ``-n <N>`` on the command line always wins; this hook only fires
    for ``auto`` / ``logical``.
    """
    capacity = os.cpu_count() or 1
    total_gib = _host_total_gib()
    if total_gib:
        capacity = min(capacity, max(1, total_gib // _GIB_PER_WORKER))
    cap = max(1, _int_env(_MAX_WORKERS_ENV, _DEFAULT_WORKER_CAP))
    return _claim_worker_slots(capacity, cap)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop this run's worker slots promptly.

    Not strictly required -- the kernel releases every lock when the process
    exits -- but it returns capacity at the end of the run rather than at
    interpreter teardown, and is a no-op in xdist workers, which hold no slots.
    """
    while _held_slots:
        fd = _held_slots.pop()
        platform_compat.release_lock(fd)
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking."""
    _reset_safety_override()
    yield
    _reset_safety_override()


@pytest.fixture(autouse=True)
def _reset_reasoning_effort_globals():
    """Snapshot + restore the process-global reasoning-effort allowlist around
    each test. The allowlist is union-only/monotonic by design (persistence
    safety), and several AcpSessionHandle tests drive synthetic effort levels
    through ``_sync_effort_levels`` -> ``update_reasoning_effort_values``;
    without this, a level like ``"extreme"`` leaks into the global and poisons
    validation tests sharing the xdist worker (e.g. test_chat_slot_reasoning_effort)."""
    import kiro_crew.dashboard.chat_persistence as _cp

    saved_values = set(_cp._reasoning_effort_values)
    saved_ordered = list(_cp._reasoning_effort_ordered)
    try:
        yield
    finally:
        _cp._reasoning_effort_values = saved_values
        _cp._reasoning_effort_ordered = saved_ordered


@pytest.fixture(scope="session")
def _isolation_root(tmp_path_factory):
    """One session-scoped parent for the per-test isolation dirs below.

    ``tmp_path_factory.mktemp`` picks its numbered suffix by scanning the whole
    basetemp, so its cost grows with the number of entries already there. Four autouse
    fixtures called it per test, adding thousands of siblings to one directory. Those
    fixtures now ``mkdir`` under this root instead, which is a single syscall and does
    not scan.

    Named ``i`` rather than something descriptive to keep the paths short: Windows
    still caps a path at 260 characters unless long paths are enabled, and everything
    a test writes under ``KIROCREW_HOME`` nests inside here.
    """
    return tmp_path_factory.mktemp("i")


@pytest.fixture
def _isolation_dirs(_isolation_root):
    """Return an allocator for this test's isolation dirs, created on demand.

    Each call returns ``<root>/<counter>-<name>``, so one test's dirs cannot collide
    with another's (the counter is per-process, and pytest-xdist gives every worker its
    own ``basetemp`` -- verified: workers report ``popen-gw0``/``popen-gw1`` roots).
    Flat rather than nested, again to keep Windows paths short.
    """
    made: dict[str, pathlib.Path] = {}
    _isolation_dirs.seq += 1  # type: ignore[attr-defined]
    stem = _isolation_dirs.seq  # type: ignore[attr-defined]

    def _get(name: str) -> pathlib.Path:
        if name not in made:
            path = _isolation_root / f"{stem}-{name}"
            path.mkdir(parents=True, exist_ok=True)
            made[name] = path
        return made[name]

    return _get


_isolation_dirs.seq = 0  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _isolate_kirocrew_home(_isolation_dirs, monkeypatch):
    """Pin ``KIROCREW_HOME`` to a per-test tmp dir as a safety net.

    ``config_dir()`` reads ``KIROCREW_HOME`` on every call and falls back to the
    operator's real ``~/.kirocrew`` when it is unset. Any test that reaches a
    code path resolving ``apps_dir()`` / ``config_dir()`` (e.g. a lifecycle
    dispatch that calls ``app_dir(name)/"data".mkdir()``) without setting
    ``KIROCREW_HOME`` itself would otherwise create real dirs/files under the
    developer's home — and under Hypothesis that means one orphan per generated
    example, accumulating into thousands of stray ``~/.kirocrew/apps/<name>/``
    dirs over a dev's test history.

    This runs before the test body, so a test that sets its own
    ``KIROCREW_HOME`` via ``monkeypatch.setenv`` still wins (its value is applied
    later and reverted independently). The guard only changes behavior for tests
    that did NOT isolate the home themselves — exactly the leak we want to close.

    Also reset ``config.paths._resolved_home`` to ``None``: ``config_dir()`` now
    caches the resolved data home in that module global for the process lifetime
    (so the one-time ~/.kirocrew -> ~/.kiro/crew migration runs at most once).
    Under xdist a worker runs many tests in one process, so a value cached by an
    earlier test (which may have patched ``Path.home`` / cleared ``KIROCREW_HOME``)
    would otherwise leak into a later test that resolves the default home. Reset
    per test so each resolves fresh against its own patched environment.

    Also clear ``KIROCREW_PROJECT_DIR`` for the same "match CI on a dev box"
    reason: it is auto-set to the repo root when running from a checkout, so
    ``skills._project_skills_dir()`` resolves to the repo's real ``skills/``.
    A test that drives ``_ensure_builtin_skills`` against a tmp dir then sees the
    live ``skills/kirocrew-dev/*`` as a "source", which flips relocation/cleanup
    behavior — green in CI (env unset there), red locally. Tests that need a
    project dir set it themselves via ``monkeypatch`` (which still wins).
    """
    home = _isolation_dirs("kirocrew-home")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.setattr("kiro_crew.config.paths._resolved_home", None)


@pytest.fixture(autouse=True)
def _isolate_kiro_window_cache():
    """Give every test an EMPTY ``model_registry._KIRO_WINDOWS``, then restore it.

    The kiro-list window cache is process-global module state with two ways to
    couple tests:

    * **Test-to-test leak** — a test that exercises ``/api/models`` (which calls
      ``refresh_kiro_windows``) or seeds the cache directly would otherwise leave
      entries behind, e.g. a GPT window seeded here makes a "non-registry model
      is unknown" test in another module wrongly see GPT as known.
    * **Import-time host leak** — ``model_registry`` calls ``_load_kiro_windows()``
      at import, which reads ``<config_dir>/model_windows.json``. On a developer
      box that file holds the operator's real cached windows (e.g. a locally
      served ``deepseek-3.2`` at a non-registry value), so a test asserting the
      static supplementary floor for that same id fails ONLY on that machine —
      green in CI (no such file), red locally. Snapshotting-then-restoring alone
      preserved that polluted baseline for the duration of each test body.

    Clearing before the test (and restoring the original snapshot after) makes
    every test start from the same empty cache regardless of what the host had on
    disk — so a local run matches CI. Tests that need entries seed them in their
    own body.
    """
    import kiro_crew.model_registry as _mr

    saved = dict(_mr._KIRO_WINDOWS)
    _mr._KIRO_WINDOWS.clear()
    try:
        yield
    finally:
        _mr._KIRO_WINDOWS.clear()
        _mr._KIRO_WINDOWS.update(saved)


@pytest.fixture(autouse=True)
def _isolate_subagents_dir(_isolation_dirs, monkeypatch):
    """Pin the subagent registry dir to a tmp dir for the whole suite.

    ``kiro_crew.subagent_persistence._SUBAGENTS_DIR`` is bound at import time to
    ``config_dir() / "subagents"``, so the ``KIROCREW_HOME`` safety net above
    cannot retroactively redirect it. Any test that calls ``SubagentManager.spawn``
    or ``create_agent_folder`` without isolating this global itself would write
    stub agent folders into the operator's real ``~/.kirocrew/subagents/``. On the
    next gateway start, orphan reconciliation sweeps those stubs and floods the
    logs with "lost to gateway restart" warnings (e.g. tasks ``t`` / ``ls /tmp``).
    Redirecting the module global gives every test an isolated, empty registry.
    """
    monkeypatch.setattr(
        "kiro_crew.subagent_persistence._SUBAGENTS_DIR",
        _isolation_dirs("subagents"),
    )


@pytest.fixture(autouse=True)
def _no_model_download(monkeypatch, _isolation_dirs):
    """Never let a test trigger the 610MB embedding-model download.

    Embeddings are always-on, so any test that boots the gateway/server
    startup path would otherwise kick ``start_background_model_download()``.
    The env escape hatch is honored by ``ModelDownloadManager.ensure_model``
    and ``start_background_model_download`` — a test that wants to exercise
    the download path monkeypatches the manager's HTTP calls directly
    (see test_embeddings.py) rather than unsetting this.

    ``OLLAMA_MODELS`` is additionally pinned to an empty tmp dir so the
    legacy-blob salvage fast-path (``_salvage_legacy_ollama_blob``) can never
    read the developer's real ``~/.ollama`` store — without this, download
    tests would pass/fail machine-dependently on hosts that ran the
    Ollama-era embeddings.
    """
    monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
    monkeypatch.setenv("OLLAMA_MODELS", str(_isolation_dirs("ollama-models")))
    # Force telemetry OFF for every test. `_consent_enabled` reads this env var BEFORE
    # the config flag, which is what makes it a reliable gate: ~15 tests patch
    # `KiroCrewConfig.load` with a bare MagicMock, whose `telemetry.enabled` is TRUTHY,
    # so a real recorder starts and `Path(cfg.local_dir)` resolves the mock to the
    # RELATIVE path `MagicMock/load().telemetry.local_dir/...` -- writing metrics and a
    # lock file into the repo root, plus a background reader thread that outlives the
    # test. Tests that exercise telemetry delete this var themselves (test/metrics/).
    monkeypatch.setenv("KIROCREW_TELEMETRY", "0")


@pytest.fixture(autouse=True)
def _isolate_agent_state_sidecar(_isolation_dirs, monkeypatch):
    """Pin the agent_state sidecar to a tmp dir for the whole suite.

    ``kiro_crew.agent_state`` stores per-agent bookkeeping (model_managed,
    cc_model) in ``~/.kirocrew/agent_model_state.json`` via ``config_dir()``.
    Tests that exercise the install / refresh / migration / PATCH paths would
    otherwise read and write the operator's real sidecar. Redirect
    ``config_dir`` — referenced as a module attribute at call time — to a fresh
    tmp dir so every test starts from empty state.
    """
    sidecar_root = _isolation_dirs("agent-state")
    monkeypatch.setattr("kiro_crew.agent_state.config_dir", lambda: sidecar_root)


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Ensure a USABLE (open) event loop exists for tests that call
    ``asyncio.get_event_loop().run_until_complete(...)`` (e.g. test_knowledge).

    Two failure modes this guards, both seen on the loaded CI farm under xdist:
      * no current loop set (``RuntimeError``) — Python 3.9 Semaphore default_factory; and
      * a current loop that is set but CLOSED — left behind by a prior test in the same
        worker that ran ``asyncio.run(...)`` (which on 3.12 closes its loop at teardown).
        ``get_event_loop()`` returns that closed loop WITHOUT raising, so the next
        ``run_until_complete`` blows up with ``RuntimeError: Event loop is closed``. We
        detect a closed/absent loop and install a fresh open one so each test starts clean.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _restore_default_child_watcher():
    """Restore the always-active ThreadedChildWatcher after every test (Python 3.10).

    Some tests install a real, non-default asyncio child watcher via the
    gateway's ``_install_child_watcher()`` -- notably
    ``test_cli.py::test_real_subprocess_works_after_install_on_linux``, which on
    Linux installs a ``PidfdChildWatcher`` and runs ``asyncio.run``. On exit,
    ``asyncio.run`` detaches the watcher's loop, leaving a loop-less watcher in
    the global policy. On Python 3.10 that watcher's ``is_active()`` is then
    False, so the NEXT subprocess-spawning test scheduled on the same
    pytest-xdist worker (``test_taskrunner::TestGitCoord``, the code_reviewer git
    routes) fails with "asyncio.get_child_watcher() is not activated, subprocess
    support is not available". Whether the leak bites depends purely on xdist
    sharding, so adding or removing unrelated tests can flip a green run red with
    no production-code change. Resetting to ``ThreadedChildWatcher`` (whose
    ``is_active()`` is always True) after each test removes the cross-test
    coupling. No-op on 3.12+, where child watchers were removed and the event
    loop reaps children directly.
    """
    yield
    if sys.version_info >= (3, 12):
        return
    threaded = getattr(asyncio, "ThreadedChildWatcher", None)
    if threaded is None:  # non-Unix (no child watchers) -- nothing to restore
        return
    try:
        if not isinstance(asyncio.get_child_watcher(), threaded):
            asyncio.set_child_watcher(threaded())
    except Exception:  # noqa: BLE001 -- isolation cleanup must never fail a test
        # Test-isolation cleanup must never fail a test; worst case is the
        # pre-existing leak, which the next test's loop setup also tolerates.
        pass


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make git tests hermetic: pin identity AND neutralize host global/system config.

    Two independent host-environment bleeds must be closed for git-backed tests
    (git_coord scenarios) to be deterministic across machines:

    1. Identity — a host without a global ``user.name``/``user.email`` makes
       ``git commit`` fail. Pin it via the ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
       env vars.
    2. Global/system config — the host's ``~/.gitconfig`` (via
       ``core.excludesFile`` → e.g. ``~/.gitignore_global`` containing ``*.png``)
       silently makes ``git add -A`` skip files, so a "commit a binary file"
       test sees an empty tree and gets an empty sha. Point
       ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` at ``/dev/null`` so no
       host-level config (excludes, aliases, hooks, signing) leaks into tests.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    # Isolate from the host's global/system git config (Git >= 2.32). An empty
    # file (/dev/null) means git reads no global or system settings.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def _no_load_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip system load checks in tests — avoids real asyncio.sleep delays."""
    from unittest.mock import AsyncMock

    try:
        monkeypatch.setattr("kiro_crew.task_executor._wait_for_load", AsyncMock())
    except AttributeError:
        pass  # load guard not present in this branch


@pytest.fixture(autouse=True)
def _enterprise_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a default validated team_id so _route_message doesn't reject messages."""
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_team_id", "TTEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._validated_enterprise_id", "ETEST")
    monkeypatch.setattr("kiro_crew.slack.enterprise._allowed_team_ids", {"TTEST"})


@pytest.fixture(autouse=True)
def _clean_emojis():
    """Reset _PHASE_EMOJIS to defaults before each test (suppresses local config)."""
    original = dict(_PHASE_EMOJIS)
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(_build_phase_emojis({})[0])
    yield
    _PHASE_EMOJIS.clear()
    _PHASE_EMOJIS.update(original)


@pytest.fixture(autouse=True)
def _clean_slack_thread_state():
    """Reset the ``handler`` module-global thread-state maps between tests.

    ``handler`` keeps process-global maps for per-thread privacy and routing
    state: ``_thread_temporary`` / ``_thread_incognito`` (drive
    ``_is_slack_restricted`` — the memory-write gate consulted by ``!title``,
    consolidation, etc.), ``_titled_threads`` (auto-title claim), and
    ``_thread_agents`` (per-thread agent override). Nothing clears these
    globally, so a test that marks a thread restricted — including one that
    drives the real ``handle_message_transport`` drain path against a
    ``MagicMock`` session map, whose ``_hydrate_conv_flags`` reads truthy mock
    flags and calls ``_mark_incognito`` / ``_mark_temporary`` — leaves e.g.
    ``"thread1"`` in ``_thread_incognito`` forever. Under ``pytest -n auto``
    (``--dist load`` interleaves tests across files on each worker) a later
    ``test_title_updates_conversation_log`` then sees
    ``_is_slack_restricted("thread1") is True`` and skips ``set_title``,
    failing with no production-code change — a classic order-dependent flake.
    Clearing before and after every test makes each hermetic regardless of
    scheduling. Idempotent with per-file fixtures that already clear a subset.
    """
    from kiro_crew.slack import handler as _h

    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()
    yield
    for _m in (_h._thread_temporary, _h._thread_incognito, _h._titled_threads, _h._thread_agents):
        _m.clear()


@pytest.fixture(autouse=True, scope="session")
def _isolate_sel_default_dir(tmp_path_factory):
    """Redirect the Security Event Log default dir to a session-local tmp dir.

    SEL's default singleton writes to the real security_events.jsonl under the
    data home (``_default_dir()`` → ``config_dir()``, non-atomic append). Tests
    that emit events via the default ``sel()`` would otherwise pollute that real
    file and, under ``pytest -n auto``, share it across worker processes. Redirect
    the module-level default accessor to a per-session tmp dir. Session-scoped so
    we don't churn SEL's background writer thread per test; tests that manage
    their own ``SecurityEventLog`` (test_sel.py resets ``_instance`` + passes
    ``base_dir``) are unaffected.

    Patches the ``_default_dir()`` accessor (not a captured constant): the module
    now resolves its default lazily so importing ``kiro_crew.sel`` never triggers
    the one-time data-home migration as an import side effect.
    """
    orig_fn = _sel._default_dir
    orig_inst = _sel.SecurityEventLog._instance
    _sel_dir = tmp_path_factory.mktemp("sel")
    _sel._default_dir = lambda: _sel_dir
    _sel.SecurityEventLog._instance = None
    yield
    _sel._default_dir = orig_fn
    _sel.SecurityEventLog._instance = orig_inst


class MockSlackClient(SlackClientOps):
    """In-memory mock for testing."""

    def __init__(self):
        self.actions: list[tuple[str, dict]] = []
        self._next_ts = 1000000
        self._fetch_message_result: str | None = None
        self._fetch_thread_replies_result: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            ("post", {"channel": channel, "text": text, "thread_ts": thread_ts, "ts": ts,
                      "unfurl_links": unfurl_links, "unfurl_media": unfurl_media})
        )
        return ts

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "blocks",
                {
                    "channel": channel,
                    "blocks": blocks,
                    "text": text,
                    "thread_ts": thread_ts,
                    "ts": ts,
                    "unfurl_links": unfurl_links,
                    "unfurl_media": unfurl_media,
                },
            )
        )
        return ts

    async def update_message(self, channel, ts, text):
        self.actions.append(("update", {"channel": channel, "ts": ts, "text": text}))

    async def delete_message(self, channel, ts):
        self.actions.append(("delete", {"channel": channel, "ts": ts}))

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("react", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        self.actions.append(("unreact", {"channel": channel, "ts": ts, "emoji": emoji}))

    async def open_dm(self, user_id):
        self.actions.append(("open_dm", {"user_id": user_id}))
        return f"D{user_id}"

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        self.actions.append(("ephemeral", {"channel": channel, "user_id": user_id, "text": text, "blocks": blocks, "thread_ts": thread_ts}))

    async def views_publish(self, user_id, view):
        self.actions.append(("views_publish", {"user_id": user_id, "view": view}))

    async def views_open(self, trigger_id, view):
        self.actions.append(("views_open", {"trigger_id": trigger_id, "view": view}))

    async def views_update(self, view_id, view):
        self.actions.append(("views_update", {"view_id": view_id, "view": view}))

    async def upload_file(self, channel, thread_ts, file, filename, title):
        self.actions.append(
            (
                "upload_file",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "file": file,
                    "filename": filename,
                    "title": title,
                },
            )
        )

    async def start_stream(self, channel, thread_ts, initial_text=None, team_id=None, user_id=None):
        if not getattr(self, "_stream_enabled", False) or getattr(self, "_start_stream_fails", False):
            return None
        ts = f"{self._next_ts}.000000"
        self._next_ts += 1
        self.actions.append(
            (
                "start_stream",
                {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "text": initial_text,
                    "ts": ts,
                },
            )
        )
        return ts

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.actions.append(
            (
                "append_task",
                {
                    "channel": channel,
                    "ts": ts,
                    "task_id": task_id,
                    "title": title,
                    "status": status,
                    "details": details,
                },
            )
        )
        return True

    async def stop_stream(self, channel, ts, final_text=None):
        self.actions.append(("stop_stream", {"channel": channel, "ts": ts, "text": final_text}))
        return True

    async def set_thread_title(self, channel, thread_ts, title):
        self.actions.append(
            ("set_thread_title", {"channel": channel, "thread_ts": thread_ts, "title": title})
        )

    async def set_thread_status(self, channel, thread_ts, status):
        self.actions.append(
            ("set_thread_status", {"channel": channel, "thread_ts": thread_ts, "status": status})
        )

    async def fetch_message(self, channel: str, ts: str) -> str | None:
        self.actions.append(("fetch_message", {"channel": channel, "ts": ts}))
        return self._fetch_message_result

    async def fetch_thread_replies(self, channel: str, thread_ts: str, limit: int = 200, warn_on_pagination: bool = True) -> list[dict]:
        self.actions.append(("fetch_thread_replies", {"channel": channel, "thread_ts": thread_ts, "limit": limit, "warn_on_pagination": warn_on_pagination}))
        return self._fetch_thread_replies_result


@pytest.fixture(autouse=True, scope="module")
def _fake_computer_use_backend():
    """Register the shipped FAKE computer-use backend for the whole suite.

    Computer use reads another application's accessibility tree, captures its
    window pixels, and synthesizes clicks/keystrokes into it. CI must never do
    any of that, so this is one of the TWO mechanisms that keep the native path
    unreachable in tests:

    1. this process-wide registration, so ``get_shared_backend()`` always
       returns ``FakeComputerUseBackend`` (``platform_id == "fake"``);
    2. structural — the package has no module-scope ``CDLL``/``find_library``, so
       importing it on a Linux runner loads nothing native.

    Both are asserted: ``test_computer_use_backend.py::
    test_ci_never_selects_a_native_backend`` pins (1) and
    ``test_computer_use_unsupported.py`` pins (2).

    MODULE-scoped, not function-scoped, deliberately: the registration is a
    single module-global assignment plus a singleton drop, and paying that (plus
    the ``kiro_crew.testing.fake_computer_use`` import) on all ~16k tests would
    be pure overhead. Any test that swaps the backend itself is responsible for
    restoring it (see that file's ``restore_registry`` fixture) — a
    function-scoped fixture here would paper over such a leak instead of letting
    it fail.
    """
    from kiro_crew.computer_use.backend import (
        register_computer_use_backend,
        reset_shared_backend,
    )
    from kiro_crew.testing.fake_computer_use import FakeComputerUseBackend

    register_computer_use_backend(FakeComputerUseBackend)
    reset_shared_backend()
    yield
    register_computer_use_backend(None)
    reset_shared_backend()


@pytest.fixture(autouse=True)
def _reset_platform_context(monkeypatch):
    """Clear the process-global PlatformContext between tests.

    A test that composes a non-default context (e.g. an Amazon-overlay probe)
    must not leak it into the next test.  ``current_context()`` lazily rebuilds
    the standalone default on next access.

    Also pins ``KIROCREW_PROFILE=standalone`` by default so a dev box that has a
    real SSO-marker directory does not make ``boot_platform`` resolve the
    ``amazon`` profile and fail closed (no companion installed) for the many
    pre-existing tests that drive ``run_gateway`` / boot.  A test that wants the
    amazon profile overrides this env via its own ``monkeypatch.setenv`` (it
    runs after this autouse fixture), or composes the context directly via
    ``set_context`` without booting.
    """
    from kiro_crew.platform.bootstrap import _reset_boot_state
    from kiro_crew.platform.context import reset_context

    monkeypatch.setenv("KIROCREW_PROFILE", "standalone")
    reset_context()
    _reset_boot_state()
    yield
    reset_context()
    _reset_boot_state()


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
    """
    import tempfile

    short_root = "/tmp" if os.path.isdir("/tmp") else None
    if short_root is None:
        yield tmp_path
        return
    path = tempfile.mkdtemp(dir=short_root, prefix="kcsock-")
    try:
        yield pathlib.Path(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)
