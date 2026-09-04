"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import socket
import struct
import sys
import warnings

import pytest
from hypothesis import HealthCheck, settings

from kiro_crew.safety_override import reset_singleton as _reset_safety_override
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.handler import _PHASE_EMOJIS, _build_phase_emojis

if os.name == "nt":

    class _WindowsTestProactorEventLoop(asyncio.ProactorEventLoop):
        """Close the loop wakeup socket without filling Windows' TIME_WAIT table."""

        def _close_self_pipe(self) -> None:
            # Windows implements ``socketpair`` with a localhost TCP connection.
            # pytest-asyncio 0.20 creates two fresh loops per async test, so this
            # 62k-test suite can consume the 16,384-port dynamic range before
            # TIME_WAIT entries expire.  Abortive close is safe for the loop's
            # private wakeup socket (it carries no application data) and releases
            # the port immediately while preserving a fresh Proactor loop per test.
            linger = struct.pack("hh", 1, 0)
            # Only the write end gets abortive close.  ``ProactorEventLoop``
            # cancels the pending read and normally closes ``_ssock`` first;
            # resetting that read end itself turns otherwise-clean async-test
            # teardown into ``ConnectionResetError``.  Resetting the peer after
            # the read end has closed still prevents a TIME_WAIT entry.
            write_socket = self._csock
            if write_socket is not None:
                try:
                    write_socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                except OSError:
                    pass
            super()._close_self_pipe()

    class _WindowsTestEventLoopPolicy(asyncio.WindowsProactorEventLoopPolicy):
        _loop_factory = _WindowsTestProactorEventLoop

    asyncio.set_event_loop_policy(_WindowsTestEventLoopPolicy())

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


def _can_create_symlink() -> bool:
    """PROBE, never a platform guess: can this process create a real symlink?

    Creating one on Windows needs ``SeCreateSymbolicLinkPrivilege``, held by an
    elevated or Developer-Mode account (GitHub's Windows runners do) and not by
    an ordinary one. Probing keeps the coverage wherever the privilege exists
    instead of blanket-skipping every Windows host — a bare
    ``skipif(IS_WINDOWS)`` would silently drop these assertions on CI, which is
    exactly where they need to run.

    Reserve this for tests about the SYMLINK MECHANISM itself. A test that only
    needs "a name meaning another directory" belongs on
    ``platform_compat.symlink_or_junction`` (junction on Windows, no privilege needed).
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "target")
        os.mkdir(target)
        try:
            os.symlink(target, os.path.join(tmp, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


_HAS_SYMLINKS = _can_create_symlink()

requires_symlinks = pytest.mark.skipif(
    not _HAS_SYMLINKS,
    reason="creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows",
)


def _find_posix_test_shell() -> str | None:
    """Return a real POSIX shell without mistaking Windows' WSL launcher for one."""
    if os.name != "nt":
        return shutil.which("sh")

    git = shutil.which("git")
    candidates: list[pathlib.Path] = []
    if git:
        candidates.append(pathlib.Path(git).resolve().parents[1] / "bin" / "bash.exe")
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(pathlib.Path(root) / "Git" / "bin" / "bash.exe")
    return next((str(path) for path in candidates if path.is_file()), None)


@pytest.fixture
def posix_test_shell() -> str:
    """Provide a shell for POSIX command-plumbing tests on every supported host."""
    shell = _find_posix_test_shell()
    if shell is None:
        pytest.skip("a POSIX test shell is not available")
    return shell


# ── Windows CI ──────────────────────────────────────────────────────────
# The backend runs natively on Windows (kiro_crew.platform_compat), but a
# handful of suites exercise POSIX-only-by-design features (OS-level
# sandbox, process groups / PGID semantics, PTY, AF_UNIX sockets -- see
# docs/guides/windows-install.md's per-feature table). Skip collecting them on
# Windows rather than marking test-by-test: several fail at import or
# fixture time on win32.
from kiro_crew import platform_compat  # noqa: E402

if platform_compat.IS_WINDOWS:
    # Read from windows-collect-ignore.txt rather than an inline list: the CI
    # reduced-scope selector (scripts/ci-surface-tests.py) has to apply the same
    # exclusion, because naming a file explicitly on the pytest command line
    # bypasses collect_ignore. One file, two readers, no drift.
    _ignore_listfile = os.path.join(os.path.dirname(__file__), "windows-collect-ignore.txt")
    with open(_ignore_listfile, encoding="utf-8") as _fh:
        collect_ignore = [
            name
            for name in (ln.split("#", 1)[0].strip() for ln in _fh)
            if name
        ]


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


def make_dir_link(link: pathlib.Path, target: pathlib.Path) -> None:
    """Create a reparse point at ``link`` that resolves to the directory ``target``.

    Same privilege reasoning as :func:`make_escaping_link`, for the tests that
    need a *directory* link rather than a path through one: a directory symlink
    needs SeCreateSymbolicLinkPrivilege on Windows (WinError 1314 in an
    unelevated shell), while a junction needs none and is followed by the same
    reparse machinery — ``rglob``, ``resolve`` and
    ``GetFinalPathNameByHandleW`` all traverse it identically. So the behaviour
    under test stays exercised on Windows instead of being skipped.
    """
    if platform_compat.IS_WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    link.symlink_to(target, target_is_directory=True)


#: ``pytest_collection_modifyitems`` -- which applies the
#: ``windows-expected-failures.txt`` skips -- lives in the ROOTDIR ``conftest.py``.
#: That list already names node ids under
#: ``src/kiro_crew/apps/builtins/auto_improvement/tests/``, and a hook rooted here never
#: runs when only those in-package tests are collected (which is exactly what CI's
#: reduced-scope Windows job does on a frontend-only diff), so the skips silently did
#: not apply where they were needed.
#:
#: ``collect_ignore`` above deliberately stays here: it names paths relative to its own
#: conftest's directory and every entry is a file under ``test/``, so it is correct
#: where it is.


@pytest.fixture(autouse=True)
def _windows_restrict_to_owner_stub(request, monkeypatch):
    """On Windows, no-op the secret lockdown for hermetic tests.

    Many tests stub ``subprocess.run`` (or strip PATH) for hermeticity, or
    monkeypatch the SID resolver; ``restrict_to_owner``'s DELIBERATE fail-loud
    OSError then cascades into hundreds of unrelated
    tests. The real Windows implementation keeps direct coverage in
    test_platform_compat / test_spawn_audit (exempted here) and the
    POSIX chmod path keeps full coverage on the Linux matrix. Product
    call sites that bound the symbol by value (tips.py) are unaffected
    by this module-attr patch -- acceptable: they surface as at most a
    handful of failures, handled individually.

    ``restrict_dir_to_owner`` is stubbed alongside it and must stay that
    way: it is the directory twin that ``make_owner_only_dir`` routes
    through, so stubbing only the file helper would leave every test that
    creates an owner-only directory writing a real DACL.

    Note the lockdown no longer spawns anything -- it applies the DACL through
    ``advapi32`` in-process -- so the subprocess-stub collision this fixture was
    built for is mostly gone. The stub is kept because a hermetic test that
    patches the SID resolver or the writer seam can still trip the fail-loud
    OSError, and narrowing it is a change to hundreds of tests' blast radius
    rather than part of the DACL swap.
    """
    if not platform_compat.IS_WINDOWS or request.module.__name__ in (
        "test_platform_compat",
        # Exempted for the same reason as test_platform_compat: these modules own
        # the direct Windows-branch coverage for the owner-only lockdown, so a
        # stub would make their assertions vacuous. They were passing on Linux
        # (where the fixture is inert) and silently asserting nothing on Windows.
        "test_platform_compat_coverage",
        "test_config_rmw_preserves_settings",
        "test_spawn_audit",
    ):
        yield
        return
    monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: None)
    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: None)
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

    # The sandbox probe prewarm moved to the ROOTDIR conftest's
    # ``pytest_runtest_setup``. A once-per-worker prewarm here reached neither the
    # in-package app suites (which never load this file) nor any test that followed
    # one of the ``test_sandbox_*.py`` files, both of which then hit the
    # never-probe-on-the-loop guard and read it as "this host has no sandbox".


# ── xdist INTERNALERROR terminal report (issue #2803) ───────────────────
# When TWO pytest-timeout worker kills land in the same ``--dist loadgroup``
# shard, xdist's loadscope scheduler can die with ``KeyError:
# <WorkerController gwN>`` (a replaced node present in ``assigned_work`` but
# absent from ``registered_collections``). pytest then exits 3 WITHOUT a
# ``short test summary info`` section, so the red names no failing test at
# all. The upstream defect is xdist's to fix; what this repo preserves is the
# REPORT: record every crashed worker and the test it was running (the
# pytest-timeout victim), and replay them from ``pytest_internalerror`` --
# a hook that fires only on the already-broken path, so healthy runs pay
# nothing. The run still exits non-zero: nothing here suppresses the
# INTERNALERROR traceback or touches the exit status.
#
# State lives at module level on the controller only: ``pytest_testnodedown``
# and ``pytest_handlecrashitem`` are controller-side xdist hooks that never
# fire inside a worker, and ``pytest_internalerror`` only emits when a crash
# was recorded, so a non-xdist internal error is reported exactly as before.

_crashed_workers: list[tuple[str, str]] = []  # (worker id, error text)
_crash_victims: list[str] = []  # test nodeids running when their worker died


def _reset_xdist_crash_state() -> None:
    """Test seam: clear the recorded crashes (module state is process-global)."""
    _crashed_workers.clear()
    _crash_victims.clear()


def pytest_testnodedown(node, error) -> None:
    """Record a crashed worker (controller only; ``error`` is None on clean exit)."""
    if error is None:
        return
    worker_id = getattr(getattr(node, "gateway", None), "id", None) or "<unknown worker>"
    _crashed_workers.append((str(worker_id), str(error)))


def pytest_handlecrashitem(crashitem, report, sched) -> None:
    """Record the test a crashed worker was running -- the timeout victim."""
    _crash_victims.append(str(crashitem))


def _format_abandoned_run_report(
    crashes: list[tuple[str, str]], victims: list[str]
) -> str:
    """Build the terminal report for a run abandoned after worker crashes.

    Wording is deliberately non-causal: worker replacement is routine here
    (``--max-worker-restart=2`` exists because workers die under memory
    pressure), so a later INTERNALERROR is not necessarily caused by the
    recorded crashes. The block replays what was RECORDED earlier in this
    run and leaves attribution to the reader.
    """
    lines = [
        "",
        "=" * 72,
        f"xdist run ABANDONED: INTERNALERROR after {len(crashes)} crashed-worker "
        f"replacement{'s' if len(crashes) != 1 else ''}",
        "=" * 72,
        "pytest hit an INTERNALERROR after replacing crashed workers, so the",
        "normal 'short test summary info' section was never written. The worker",
        "crashes recorded earlier in this run are replayed here so this red",
        "stays diagnosable:",
        "",
        "Crashed workers:",
    ]
    for worker_id, error in crashes:
        # The error is commonly a remote traceback: its FIRST line is the
        # constant "Traceback (most recent call last):", so the last
        # non-empty line (the exception itself) is the informative one.
        stripped = [ln for ln in error.strip().splitlines() if ln.strip()]
        summary = stripped[-1].strip() if stripped else error
        lines.append(f"    {worker_id}: {summary}")
    if victims:
        lines.append("")
        lines.append("Tests running when their worker died (recorded earlier in this run):")
        for victim in victims:
            lines.append(f"    {victim}")
    else:
        lines.append("")
        lines.append("No in-flight test was recorded for the crashed workers.")
    lines.append("")
    lines.append("The run still fails (INTERNALERROR, non-zero exit); this block only")
    lines.append("preserves the report that the crash would otherwise erase.")
    lines.append("=" * 72)
    return "\n".join(lines)


def pytest_internalerror(excrepr, excinfo) -> None:
    """Replay recorded worker crashes when an INTERNALERROR kills the run.

    Written to ``sys.stderr`` directly rather than through the terminal
    reporter: this hook runs on a path where pytest's own reporting machinery
    has already failed, and stderr is the one sink that cannot depend on it.
    Returns ``None`` (never ``True``) so pytest still prints the
    ``INTERNALERROR>`` traceback and exits 3 -- the goal is a diagnosable red,
    not a green.
    """
    if not _crashed_workers:
        return
    print(
        _format_abandoned_run_report(list(_crashed_workers), list(_crash_victims)),
        file=sys.stderr,
        flush=True,
    )


@pytest.fixture(autouse=True)
def _release_stt_engine():
    """Never let a loaded speech model outlive the test that loaded it.

    ``kiro_crew.stt.engine`` keeps ONE recogniser per process on purpose: the
    whole point of the module is that an utterance does not pay for a model load.
    Under xdist that same property makes it cross-test state, and the instance
    carries the idle-eviction window the first caller passed, so a later test
    reading a different setting would silently get the earlier one.
    """
    yield
    from kiro_crew.stt import engine as stt_engine

    stt_engine._engine = None


@pytest.fixture(autouse=True)
def _reset_safety_override_between_tests():
    """Reset the SafetyOverride singleton between tests to prevent state leaking."""
    _reset_safety_override()
    yield
    _reset_safety_override()


@pytest.fixture(autouse=True)
def _reset_degraded_config_observations():
    """Forget the loader's process-sticky malformed-config observations.

    ``kiro_crew.config.loader`` remembers every malformed config section it has
    ever seen for the LIFE OF THE PROCESS (deliberately: ``load()``'s migration
    repairs the file on first read, so the observation is the only surviving
    evidence, and the publish gate fails closed on it). Tests share one
    interpreter, so without this reset any test that writes a malformed
    ``config.json`` — loader error-path tests do — makes the publish/deploy
    gate deny in every LATER test in the same worker, failing tests in files
    that never touched the config (seen as ``TestPending`` 4xx refusals in
    ``test_deploy_handlers_coverage.py`` under random orderings).

    ``reset_degraded_observations`` documents tests as its only legitimate
    caller; this fixture is that caller.
    """
    from kiro_crew.config.loader import reset_degraded_observations

    reset_degraded_observations()
    yield
    reset_degraded_observations()


@pytest.fixture(autouse=True)
def _restore_autonudge_singleton():
    """Floor under ``autonudge._INSTANCE`` — the process-global service reference.

    ``AutoNudgeService.start()`` publishes itself here and ``stop()`` clears it, so a test
    that starts the service (or drives a dashboard handler that does) leaves a live
    instance behind, holding timer TASKS created on that test's event loop. Every later
    test in the same worker then reaches those tasks through the singleton, on a loop that
    has since closed — which is how `test_dashboard_chat.py`'s
    ``TestCloseBroadcastDurability`` came to answer 500 with no production-code change,
    from a leak in a file that has nothing to do with it.

    Restores what the test INHERITED rather than a pristine ``None``, so a leak from an
    earlier test is not re-reported against every test after it. Restores silently rather
    than failing: production really does publish this singleton, and a test driving that
    code cannot avoid inheriting it — the damage is to other tests, and stopping it
    propagating is the part that is never optional.

    Retiring the leaked instance's timers goes through ``_cancel_timer``, which is the one
    place that knows a task on a closed loop must be dropped rather than cancelled.
    """
    from kiro_crew import autonudge as _an

    inherited = _an._INSTANCE
    try:
        yield
    finally:
        leaked = _an._INSTANCE
        if leaked is not None and leaked is not inherited:
            for loop_id in list(getattr(leaked, "_timers", {})):
                try:
                    leaked._cancel_timer(loop_id)
                except Exception:  # noqa: BLE001 - teardown must not mask the test result
                    pass
        _an._INSTANCE = inherited


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


#: ``_isolation_root`` / ``_isolation_dirs`` / ``_isolate_kirocrew_home`` live in the
#: ROOTDIR ``conftest.py``, not here. The data home has to be pinned for every
#: testpath, including the ~108 test modules that ship inside the package under
#: ``src/kiro_crew/apps/builtins/*/tests/`` and never see this file. The fixtures
#: below still request ``_isolation_dirs`` and resolve it up the hierarchy.


@pytest.fixture(autouse=True)
def _disable_dev_fleet_background_tasks(monkeypatch):
    """Stop dev-fleet's app-startup hook from starting its background loops.

    A test that boots the real app via ``dev_fleet.server.create_app()`` (to
    exercise middleware, for instance) otherwise starts ``_status_refresher``,
    a genuine network ``git fetch``, as a fire-and-forget task. That task can
    still be running when the test's client tears down, and cancelling it then
    is what leaked into unrelated tests and flaked ``Gateway Tests (macOS)``
    (issue #1832). A test that wants the real refresher overrides this itself
    via ``monkeypatch.setattr(worktree_ops, "_background_tasks_disabled", lambda: False)``.
    """
    monkeypatch.setenv("KIROCREW_DEVFLEET_NO_BACKGROUND", "1")


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
def _isolate_message_entry_cache():
    """Give every test an EMPTY ``chat_persistence`` persisted-entry cache.

    The memoised message-entry builder keeps a process-global cache keyed on a
    content hash of the whole message, so two tests using the same message
    content share an entry. That is harmless while the builder is pure, and a
    silent trap the moment a test makes it impure: a test that monkeypatches
    ``chat_persistence.redact_credentials`` (or the uncached builder) and reuses
    content another test already cached is served the earlier, pre-patch entry.
    The assertion then passes against a value the patched code never produced —
    worst of all for a redaction test, which would go green having seen the
    redacted entry it was written to prove absent.

    Lives here rather than in the memoisation test module because the hazard runs
    the other way: the module that pollutes the cache is not the one that
    misreads it.

    The byte counter is part of the same state, so resetting only the dict would
    leave the memory ceiling mis-accounted and evict a healthy cache.

    The memoised config bounds are reset for the same reason: they are resolved
    once per process from whatever KIROCREW_HOME the first caller saw, so a test
    that resolved them under its own home would otherwise leak its bounds into
    every later test's cache behaviour.
    """
    from kiro_crew.dashboard import chat_persistence as _cp

    _cp._entry_cache.clear()
    _cp._entry_cache_bytes = 0
    _cp._entry_cache_bounds_cached = None
    _cp._entry_cache_bounds_read_warned = False
    try:
        yield
    finally:
        _cp._entry_cache.clear()
        _cp._entry_cache_bytes = 0
        _cp._entry_cache_bounds_cached = None
        _cp._entry_cache_bounds_read_warned = False


@pytest.fixture(autouse=True)
def _disarm_agent_slice_memory_high():
    """Disarm the agent-slice ``MemoryHigh`` reconcile for every test.

    ``cgroup_scope_argv`` reconciles ``MemoryHigh`` on the shared agent slice
    via a real ``systemctl --user set-property`` before wrapping a spawn. On a
    Linux host WITH cgroup delegation the probe passes for real, so any test
    that reaches ``cgroup_scope_argv`` (spawn-audit, the real pids.max
    enforcement test, integration paths) would mutate the developer's live
    user manager — exactly the class of side effect the root conftest's
    host-service guard refuses (``set-property`` is a mutating verb), turning
    those tests into guard failures. Pre-disarm via the module's own kill
    switch and restore all four state globals after, so tests of the
    reconciler itself can re-arm explicitly in their own body.
    """
    import kiro_crew.sandbox as _sb

    saved_disabled = _sb._SLICE_MEMHIGH_DISABLED
    saved_applied = _sb._SLICE_MEMHIGH_APPLIED
    saved_events_seen = _sb._SLICE_MEMHIGH_EVENTS_SEEN
    saved_climb_warned = _sb._SLICE_MEMHIGH_CLIMB_WARNED
    _sb._SLICE_MEMHIGH_DISABLED = True
    try:
        yield
    finally:
        _sb._SLICE_MEMHIGH_DISABLED = saved_disabled
        _sb._SLICE_MEMHIGH_APPLIED = saved_applied
        _sb._SLICE_MEMHIGH_EVENTS_SEEN = saved_events_seen
        _sb._SLICE_MEMHIGH_CLIMB_WARNED = saved_climb_warned


@pytest.fixture(autouse=True)
def _reset_options_control_state():
    """Clear the per-message OPTIONS registries between tests.

    ``kiro_crew.slack.outbound`` holds two process-global maps keyed by
    ``(channel, ts)``: the per-message edit lock, and the once-only answer claim
    that stops a second Send click dispatching a duplicate turn. Both are
    correct as process state in the gateway, where a control's ts is unique and
    lives as long as the message does.

    Tests are the opposite: fixtures reuse a fixed pair like ``("CH1", "msg1")``
    across unrelated cases, so without this the first test to submit claims the
    control and every later test's click is silently dropped as a duplicate.
    Reset per test rather than making production defensive about it.
    """
    from kiro_crew.slack import outbound

    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()
    yield
    outbound._ANSWERED.clear()
    outbound._EDIT_LOCKS.clear()
    outbound._LOCK_USERS.clear()


#: ``_isolate_subagents_dir``, ``_no_model_download`` and
#: ``_isolate_agent_state_sidecar`` live in the ROOTDIR ``conftest.py``. Each one
#: protects a real HOST path (the subagent registry a running gateway sweeps as
#: orphans, a 610MB model download, the operator's agent-state sidecar), so by the
#: same test the data home meets they belong to the floor that every testpath sees --
#: not to this file, which the in-package suites never load.


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
    created_loop = None
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            created_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(created_loop)
    except RuntimeError:
        created_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(created_loop)

    yield

    # ``asyncio.run`` clears the policy's current loop, so the next test needs a
    # replacement.  On Windows each ProactorEventLoop owns a socketpair; leaving
    # every replacement open eventually exhausts the ephemeral-port range and
    # makes ``new_event_loop()`` hang until pytest kills every xdist worker.
    if created_loop is not None and not created_loop.is_closed():
        created_loop.close()
    try:
        if asyncio.get_event_loop() is created_loop:
            asyncio.set_event_loop(None)
    except RuntimeError:
        pass


@pytest.fixture(autouse=True)
def _restore_default_child_watcher():
    """Restore a FRESH ThreadedChildWatcher after every test.

    Some tests install a real, non-default asyncio child watcher via the
    gateway's ``_install_child_watcher()`` -- notably
    ``test_cli.py::test_real_subprocess_works_after_install_on_linux``, which on
    Linux installs a ``PidfdChildWatcher`` and runs ``asyncio.run``. On exit,
    ``asyncio.run`` detaches the watcher's loop, leaving a loop-less watcher in
    the global policy. Two distinct failures follow from that leak, and which
    one bites depends purely on xdist sharding, so adding or removing unrelated
    tests can flip a green run red with no production-code change:

    * On 3.10 the leaked watcher's ``is_active()`` is False, so the NEXT
      subprocess-spawning test fails with "asyncio.get_child_watcher() is not
      activated, subprocess support is not available".
    * On 3.12 the leaked watcher is still ATTACHED to callbacks bound to a loop
      that later closes. ``set_event_loop`` calls ``watcher.attach_loop()``,
      which reaps already-exited children and fires their callbacks -- against
      the closed loop -- raising ``RuntimeError: Event loop is closed``. Since
      pytest-asyncio calls ``set_event_loop`` when setting up every test that
      needs a loop, ONE leaked watcher fails every later test in that worker.

    The condition is therefore derived from whether the watcher API EXISTS, not
    from a version number: child watchers were only DEPRECATED in 3.12 and are
    removed in 3.14. A previous ``sys.version_info >= (3, 12): return`` guard
    skipped this cleanup on exactly the version where the second failure mode
    lives, which is what turned one leaked watcher into thousands of cascading
    failures in a full parallel run.
    """
    yield
    get_watcher = getattr(asyncio, "get_child_watcher", None)
    set_watcher = getattr(asyncio, "set_child_watcher", None)
    threaded = getattr(asyncio, "ThreadedChildWatcher", None)
    if not (get_watcher and set_watcher and threaded):
        # 3.14+, or a platform with no child watchers at all -- nothing to do.
        return
    try:
        with warnings.catch_warnings():
            # 3.12 deprecates these; the call is still the only way to clear the
            # leak on 3.12, so silence the warning rather than skip the fix.
            warnings.simplefilter("ignore", DeprecationWarning)
            current = get_watcher()
            # Install a FRESH watcher when the current one is the wrong type OR
            # is still holding pid->callback entries: those callbacks are bound
            # to a loop that may already be closed, and matching on type alone
            # would leave them in place.
            if not isinstance(current, threaded) or getattr(current, "_callbacks", None):
                set_watcher(threaded())
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


#: ``_isolate_sel_default_dir`` lives in the ROOTDIR ``conftest.py`` too, and for a
#: sharper reason than the data home: SEL's writer is a DAEMON THREAD on a process
#: singleton, so it outlives the test that first called ``sel()`` and keeps writing to
#: the directory that test resolved.


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


@pytest.fixture(autouse=True)
def _no_release_feed_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the update check's network seam unreachable for the whole suite.

    ``handlers.updates._do_update_check`` now has a second branch: any install
    that is NOT a git checkout is compared against the release-channel feed on the
    CDN. Two ordinary things reach it without a test asking to — ``/api/status``
    fires ``_do_update_check`` as a background task once
    ``_UPDATE_CHECK_INTERVAL`` has elapsed (and ``_last_update_check`` starts at
    ``0.0``, so the first call always qualifies), and any direct call in a test
    env with no ``KIROCREW_PROJECT_DIR`` takes the feed branch by definition.

    Without this fixture the suite would make real HTTPS requests to
    ``updates.crew.kiro.dev`` — slow, flaky, offline-hostile, and CI traffic
    nobody asked for. Tests that WANT a feed response stub this same seam, which
    overrides the fixture for that test.

    The refusal is an ``AssertionError`` because that is the loudest signal
    available, but note ``_do_update_check``'s outer ``except Exception`` net will
    convert it into ``error="unknown"`` rather than failing the test — so this is
    a NETWORK guard first and a diagnostic second. A test that means to exercise
    the feed branch must stub the seam and assert on the result.
    """

    async def _refuse(url: str) -> tuple[int, bytes]:
        raise AssertionError(
            f"test reached the real release feed ({url}) — stub "
            "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes instead"
        )

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.updates._fetch_feed_bytes", _refuse, raising=True
    )


@pytest.fixture(autouse=True)
def _no_live_catalog_network(monkeypatch: pytest.MonkeyPatch):
    """Make the official app catalog's network seam unreachable for the suite.

    ``official_catalog._open_catalog`` is THE seam every catalog fetch goes
    through (its own docstring says tests must intercept there). Two paths
    reach it without a test asking to: the install path's
    ``inventory_for_install`` performs a fresh, deliberately UNCACHED HTTPS
    fetch of ``official-registry.json`` on every call (#4236), and store
    listings can trigger ``load_official_catalog``. Without this fixture,
    any test that walks either path makes a real HTTPS request to the live
    CDN — slow, offline-hostile, and nondeterministic: the test's verdict
    then depends on the CI runner's network, and one transient failure also
    poisons the module's on-disk failure memory for the rest of the worker.

    Tests that want a catalog answer stub a higher seam
    (``fetch_document``, ``fetch_inventory_entries``, or
    ``inventory_for_install``), which keeps this fixture from ever being
    reached. A test that genuinely needs the real opener — e.g. against a
    loopback server it started itself — must opt in explicitly: request
    this fixture by name and monkeypatch ``_open_catalog`` back to the
    original it yields.

    The refusal is an ``AssertionError`` — deliberately OUTSIDE the
    exception family ``fetch_document`` degrades on — but note the install
    path's fail-closed ``except Exception`` in
    ``registry._resolve_registry_row`` will convert it into a catalog
    refusal rather than failing the test with this message. Like
    ``_no_release_feed_network`` above, this is a NETWORK guard first and a
    diagnostic second.
    """
    from kiro_crew.apps import official_catalog

    original = official_catalog._open_catalog

    def _refuse(req: object) -> None:
        url = getattr(req, "full_url", repr(req))
        raise AssertionError(
            f"test reached the live app catalog ({url}) — stub "
            "kiro_crew.apps.official_catalog.fetch_document (or a higher "
            "seam such as inventory_for_install) instead"
        )

    monkeypatch.setattr(official_catalog, "_open_catalog", _refuse, raising=True)
    yield original


@pytest.fixture
def named_cron_caller(monkeypatch):
    """Give the calling test an identity the cron MCP server can resolve.

    ``mcp_cron`` refuses a WRITE from a caller it cannot name (see
    ``_unidentified_caller_refusal``): on a pooled backend an unidentified stub
    shares the process with identified ones, so granting it authority over stored
    rows would let it reach another session's jobs.

    Tests about cron's FIELD handling -- schedules, channels, models, validation
    -- have always assumed a caller the gateway vouches for; they simply never
    said so, because the unidentified state used to be allowed to write. This
    states the precondition. A test that is actually ABOUT the unidentified
    caller must not use this fixture.

    Yields the key it set, so a test that mocks ``CronService`` can stamp the same
    owner onto its fake job instead of hardcoding this value.
    """
    key = "dashboard:conftest-slot"
    monkeypatch.setenv("KIROCREW_SESSION_KEY", key)
    return key


#: Comfortably clear of both memory guards ``SubagentManager.spawn`` runs: the
#: absolute floor (``agent.spawn_min_memory_gb``, 4 GB) and the posture tier
#: (``agent.resource_critical_gb``, 2 GB).
_HEALTHY_AVAILABLE_GB = 8.0


@pytest.fixture
def healthy_host_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the host-memory readings ``SubagentManager.spawn`` consults.

    ``spawn`` refuses -- returning before it registers anything in ``_tasks`` --
    whenever the machine looks short of memory, and it does so twice: an
    absolute floor (``check_memory_available`` against
    ``agent.spawn_min_memory_gb``) and the posture tier
    (``cached_admission_check``, which refuses while the cgroup-clamped reading
    is CRITICAL). Both read the host the suite happens to be running on, so
    without this the verdict is the operator's machine rather than the test's
    own input.

    The failure it produces is misleading, which is why it is worth a shared
    fixture: a refusal IS a ``SubagentInfo`` -- a done one carrying ``error`` --
    so ``assert info is not None`` still passes and the test dies one line later
    on ``mgr._tasks[info.id]`` with a bare ``KeyError``. Measured on a CI runner
    with ~0.5 GB free.

    Only the HOST reading is pinned: a caller that names its own ``path`` is
    feeding the ``/proc/meminfo`` parser a fixture file rather than asking about
    this machine, so it still runs the real function and a parser regression
    still goes red. A test that is actually ABOUT either guard patches it in its
    own body, which lands on top of this and reverts to it.
    """
    import kiro_crew.resource_status as resource_status
    import kiro_crew.subagent as subagent

    real_check = subagent.check_memory_available

    def _pinned_check(
        min_gb: float | None = None, path: str | None = None
    ) -> tuple[bool, float]:
        if path is None:
            return (True, _HEALTHY_AVAILABLE_GB)
        if min_gb is None:
            return real_check(path=path)
        return real_check(min_gb=min_gb, path=path)

    def _admit() -> resource_status.AdmissionDecision:
        return resource_status.AdmissionDecision(
            admitted=True,
            posture=resource_status.POSTURE_AMPLE,
            available_gb=_HEALTHY_AVAILABLE_GB,
        )

    monkeypatch.setattr(subagent, "check_memory_available", _pinned_check)
    # Also keeps the 5s-TTL refresh thread behind the cached verdict from
    # starting, so no test leaves one probing the host after it ends.
    monkeypatch.setattr(subagent, "cached_admission_check", _admit)


@pytest.fixture(autouse=True)
def _reset_create_rate_limit_buckets():
    """Clear the session/folder creation rate limiter between tests.

    ``kiro_crew.dashboard.create_rate_limit`` keeps its per-(verb, caller)
    buckets in MODULE-LEVEL state (deliberately: the production guard needs no
    durable state), so every session-creating test in a pytest process
    accumulates timestamps under shared caller keys. A shard whose test
    composition performs more than the per-window budget of creates within one
    wall-clock window then refuses a legitimate test create with
    ``create_rate_limited`` — a pass/fail outcome decided by shard composition
    and runner speed, not the code under test (#7836; observed twice on the
    Windows shard in one day, on PRs touching neither the limiter nor
    session_control). The limiter's own direct tests build their scenarios on
    top of a clean slate, so clearing between tests changes nothing for them.
    """
    from kiro_crew.dashboard import create_rate_limit

    with create_rate_limit._lock:
        create_rate_limit._buckets.clear()
    yield
    with create_rate_limit._lock:
        create_rate_limit._buckets.clear()
