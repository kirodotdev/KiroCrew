"""Shared pytest configuration and fixtures."""

from __future__ import annotations

import asyncio
import asyncio.proactor_events
import errno
import json
import os
import pathlib
import shutil
import site
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse

import pytest
from hypothesis import HealthCheck, settings

if os.name == "nt":

    class _WindowsTestProactorEventLoop(asyncio.ProactorEventLoop):
        """Close the loop wakeup socket without filling Windows' TIME_WAIT table."""

        def _close_self_pipe(self) -> None:
            # Windows implements ``socketpair`` with a localhost TCP connection.
            # pytest-asyncio 0.21 creates two fresh loops per async test, so this
            # suite is large enough to consume the 16,384-port dynamic range before
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

#: What the proactor loop's self-pipe needs. ``_make_self_pipe`` calls
#: ``socket.socketpair`` through ``asyncio.proactor_events.socket``, and the Windows
#: fallback compares its family against the module-global ``AF_INET``/``AF_INET6`` at
#: call time. One of these left rebound makes every later ``new_event_loop()`` on the
#: worker raise, so pytest-asyncio's ``event_loop`` finalizer fails every remaining
#: async test in the shard with "Event loop is closed".
_SOCKET_STATE = (
    (socket, "socket"),
    (socket, "socketpair"),
    (socket, "AF_INET"),
    (socket, "AF_INET6"),
    (asyncio.proactor_events, "socket"),
)
#: Module globals that later tests read at call time, checked with the socket state
#: above. A stand-in one test leaves here fails tests in other files on the worker.
LEAK_SENTINELS = (
    # An inline stand-in runs every later ``asyncio.to_thread`` hop on the event loop,
    # so a test that asserts its work runs off the loop fails.
    (asyncio, "to_thread"),
    # The event loop's clock (``BaseEventLoop.time``): a fake one stops or skews the
    # timers of every later loop.
    (time, "monotonic"),
    # Every later retry, poll and backoff sleeps through these two.
    (time, "sleep"),
    (asyncio, "sleep"),
)
_CHECKED_STATE = _SOCKET_STATE + LEAK_SENTINELS
_SOCKET_STATE_KEY = pytest.StashKey[tuple]()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    item.stash[_SOCKET_STATE_KEY] = tuple(getattr(m, n) for m, n in _CHECKED_STATE)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    """Put back a checked global a test left rebound, then fail that test by name.

    A tripwire, not a leak finder: it stops one leak from killing the shard and names
    the test that left it. A hook rather than an autouse fixture because it must read
    the state after the test's ``monkeypatch`` has undone; autouse fixtures in one
    conftest are set up in alphabetical order and an earlier one already requests
    ``monkeypatch``, so a fixture here would be torn down first.
    """
    leaked: list[str] = []
    try:
        result = yield
    finally:
        before = item.stash.get(_SOCKET_STATE_KEY, None)
        for (module, name), value in zip(_CHECKED_STATE, before or ()):
            if getattr(module, name, None) is not value:
                setattr(module, name, value)
                leaked.append(f"{module.__name__}.{name}")
    if leaked:
        pytest.fail(f"test left {', '.join(leaked)} rebound (restored)", pytrace=False)
    return result


# ── Hypothesis profiles ─────────────────────────────────────────────────
# "default": fast iteration, new examples on every run.  "ci" (loaded whenever CI is
# set; hypothesis's own check also honours TF_BUILD, this suite keys on CI alone) is
# that profile derandomized: each test replays one fixed example sequence derived from
# the test, so a @given test cannot red a pull request whose diff never touched it,
# and it keeps no example database, which would make the sequence depend on what
# earlier runs saved.  Exploration stays with "default" locally or
# ``HYPOTHESIS_PROFILE=thorough``; a red reproduces from the printed blob.
settings.register_profile(
    "default", max_examples=20, suppress_health_check=[HealthCheck.too_slow], deadline=None
)
settings.register_profile(
    "ci", parent=settings.get_profile("default"), derandomize=True, database=None, print_blob=True
)
settings.register_profile("thorough", max_examples=100)
settings.load_profile(
    os.getenv("HYPOTHESIS_PROFILE") or ("ci" if "CI" in os.environ else "default")
)


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

# Captured at import, BEFORE any test can monkeypatch the constant away: tests that
# simulate the flag's absence must not be confused with a platform that truly lacks it.
_HAS_O_NOFOLLOW = bool(getattr(os, "O_NOFOLLOW", 0))

requires_o_nofollow = pytest.mark.skipif(
    not _HAS_O_NOFOLLOW,
    reason=(
        "notification import refuses outright without O_NOFOLLOW, because a by-name "
        "reparse check followed by a by-name open is a check-to-open window; the "
        "refusal itself is covered by TestNotificationCopyRefusalWithoutONofollow"
    ),
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


# ── Windows and macOS CI ────────────────────────────────────────────────
# The backend runs natively on Windows (kiro_crew.platform_compat), but a
# handful of suites exercise POSIX-only-by-design features (OS-level
# sandbox, process groups / PGID semantics, PTY, AF_UNIX sockets -- see
# docs/guides/windows-install.md's per-feature table). Skip collecting them on
# Windows rather than marking test-by-test: several fail at import or
# fixture time on win32.
#
# macOS reuses the same file-driven mechanism (macos-collect-ignore.txt). macOS is
# POSIX, so the reasons that fill the Windows list do not apply there; that list is
# expected to stay short or empty, and a file exists so a whole-file exclusion has
# one documented home instead of an inline literal. The hook, and why it is a hook
# rather than a ``collect_ignore`` list, is in platform_collect_ignore_helpers.py.
from platform_collect_ignore_helpers import pytest_ignore_collect  # noqa: E402,F401

from kiro_crew import platform_compat  # noqa: E402


@pytest.fixture
def nonbundled_python_without_user_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a non-bundled venv whose user site is already unavailable."""
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)


@pytest.fixture
def nonbundled_python_with_user_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a hosted interpreter whose user site is enabled."""
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)


@pytest.fixture
def bundled_python_with_user_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model the bundled interpreter with an otherwise enabled user site."""
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: True)


# Helpers and opt-in fixtures tests import or request by name.
from conftest_opt_in import (  # noqa: E402,F401 -- bound for pytest and importers
    absent_sysconf,
    ample_host_free_memory,
    ample_host_resources,
    cap_node_module_walk,
    cap_project_root_walk,
    close_skills_loaders,
    close_subagent_managers,
    drain_breadcrumb_writes,
    forget_env_at_teardown,
    healthy_host_memory,
    host_abs,
    isolate_mcp_host_paths,
    make_escaping_link,
    named_cron_caller,
    opened,
    plant_day_link,
    short_sock_dir,
)

# Per-test resets of process-global kiro_crew state, and the network fences.
from conftest_resets import (  # noqa: E402,F401 -- bound for pytest and importers
    _approve_every_mcp_launch,
    _crewmate_dynamic_capabilities_on,
    _disable_dev_fleet_background_tasks,
    _disarm_agent_slice_memory_high,
    _drop_live_config_snapshot,
    _ensure_event_loop,
    _fake_computer_use_backend,
    _forget_sandbox_launch_mark,
    _fresh_reexec_environment,
    _history_cleanup_throttle_closed,
    _inline_taskq_pump,
    _isolate_advertised_model_cache,
    _isolate_display_verdicts,
    _isolate_kiro_window_cache,
    _no_boot_heap_freeze,
    _no_boot_sandbox_sweep,
    _no_carried_auto_update_effect,
    _no_live_catalog_network,
    _no_live_kernel_memory_pressure,
    _no_managed_venv_override,
    _no_release_feed_network,
    _no_restart_ready_timeout_override,
    _release_source_corpus_after_module,
    _release_stt_engine,
    _reset_channel_turn_ceiling,
    _reset_cjk_segmenter,
    _reset_create_rate_limit_buckets,
    _reset_crewmate_worker_slots,
    _reset_degraded_config_observations,
    _reset_live_execution_records,
    _reset_runtime_ownership_tables,
    _reset_safety_override_between_tests,
    _reset_session_switch_locks,
    _restore_autonudge_singleton,
    _restore_default_child_watcher,
    _shut_down_shared_pools_in_session,
    _small_default_host,
)

# The in-memory Slack client and the Slack state resets.
from conftest_slack import (  # noqa: E402,F401 -- bound for pytest and importers
    MockSlackClient,
    _clean_emojis,
    _clean_slack_thread_state,
    _enterprise_bypass,
    _reset_options_control_state,
)

# ``make_dir_link`` lives in kiro_crew.testing.links so the app-embedded test
# packages, which never see this conftest, share the one copy. Re-exported so
# ``from conftest import make_dir_link`` keeps working.
from kiro_crew.testing.links import make_dir_link  # noqa: E402,F401

#: Thread-CPU budget for ONE rejection of a pump in the SMALL ramp. The shipped
#: grammars spend under one clock tick here; the exponential class a shared character
#: between two adjacent quantified classes produces measured 4.3 s at 24 characters
#: and doubles per character, so it is more than a decade over.
REDOS_SMALL_BUDGET_SECONDS = 0.5
#: Pump lengths for the small ramp, ONE unit at a time. Ramping (not one fixed size)
#: is what bounds the cost of catching a regression: the mutant with the steepest
#: growth measured (~8x per pumped block) spends at most ~growth x budget on the
#: first size that overruns, and the ramp stops there. A single 24-unit probe
#: against that mutant would not return inside pytest's ``--timeout``.
REDOS_SMALL_PUMPS = tuple(range(1, 25))
#: Thread-CPU budget for one rejection of a pump in the LARGE ramp. The shipped
#: grammars measured 0.03 s at 20 000; a quadratic regression is 4e8 steps there.
REDOS_LARGE_BUDGET_SECONDS = 2.0
#: Pump lengths for the polynomial class, ascending so a cubic overruns at 2 000
#: (8e9 steps) before 20 000 is ever attempted.
REDOS_LARGE_PUMPS = (200, 2_000, 20_000)
#: Each long pump is 10x the one before, so a linear scan costs about 10x as much
#: and a quadratic one about 100x. The absolute budget alone cannot see the
#: difference for a quadratic scan whose steps are cheap: a C-level rescan of the
#: rest of the text per opener measured 1.8 s at 20 000, under 2.0 s.
REDOS_SCALING_RATIO = 25
#: Below this the ratio is not asserted: a cost this small is a few clock ticks on
#: Windows (15.6 ms), where the cheaper reading can round to zero. The shipped
#: grammars measured 0.06 s or less at 20 000.
REDOS_SCALING_FLOOR_SECONDS = 0.25

# The PEM anchor is ASSEMBLED from fragments and split mid-word, so no source line
# here carries a whole BEGIN...KEY header for the internal content scan to flag.
# The runtime value is byte-identical to the marker the redactor matches.
_PEM_DASHES = "-" * 5
_PEM_TAIL_HALF = f"ATE KEY{_PEM_DASHES}\nMIIBOgIBAAJBAKJ2\n"

#: Credential shapes a message cap can SEVER. Each half is clean on its own, so
#: scrubbing the two pieces separately sees nothing, while a reader shown them one
#: after the other sees the key -- the platform renders the markup away.
#:
#: One row per character class a hand-written guard has to know about, plus the
#: shapes such a class cannot reach: a comma inside a link target, a cut through a
#: key carrying no markup at all, and a cut inside a link's URL -- which is the one
#: shape the two readings of a join disagree about, since completing the link hides
#: the URL from a scan of the concatenation while the screen still shows it.
#:
#: Shared because two suites pin the same table -- the primitive that decides where
#: a cut may fall (``test_display_split_safety.py``) and the renderer that applies
#: it (``test_wecom_renderer.py``) -- and a duplicated fixture table drifts.
CREDENTIAL_STRADDLE_SHAPES = [
    pytest.param("[AKIA](https://ex.test/a,b)", "IOSFODNN7EXAMPLE", id="link-target-comma"),
    pytest.param(
        "Authorization: Bearer",
        " abcdefghijklmnopqrstuvwxyz0123456789",
        id="header-whitespace",
    ),
    pytest.param("AKIAIOSF_", "_ODNN7EXAMPLE", id="underscore-emphasis"),
    pytest.param("AKIAIOSF~", "~ODNN7EXAMPLE", id="tilde-emphasis"),
    pytest.param("https://evil.test/?q=AKIAIOSFODNN7", "EXAMPLE.", id="url-punctuation"),
    pytest.param("[l](https://ex.test/x/AKIAIOSF", "ODNN7EXAMPLE)", id="cut-inside-a-url"),
    pytest.param(f"{_PEM_DASHES}BEGIN RSA PRIV", _PEM_TAIL_HALF, id="pem-anchor"),
    pytest.param(f"{_PEM_DASHES}BEG**IN** RSA PRIV", _PEM_TAIL_HALF, id="pem-anchor-markup-split"),
    pytest.param("AKIAIOSF", "ODNN7EXAMPLE", id="no-markup-at-all"),
]


def assert_rejected_without_backtracking(
    reject, build_pump, *, budget_seconds: float = REDOS_LARGE_BUDGET_SECONDS
) -> None:
    """Assert a marker grammar handles an adversarial pump in linear CPU time.

    ``build_pump(n)`` returns an input with an ``n``-unit pump (a run of tabs, ``n``
    repeated heads or blocks); ``reject(text)`` runs the grammar and asserts its own
    outcome (a refusal, or the one legitimate match the shape has). Replaces the
    ``elapsed < 1.0`` wall-clock shape, which flaked in one of five full runs:
    ``perf_counter`` charges the time this worker spent DESCHEDULED behind nine
    siblings to a regex that took 0.15 s of CPU (class 5 in testing-conventions),
    and a ``monotonic()`` ratio read 16x from a 15.6 ms clock tick. Four properties:

    * **Thread CPU, not wall clock.** ``time.thread_time`` counts this thread's
      own execution, so another worker's slice cannot inflate it; the regex runs
      in C, so coverage instrumentation does not either.
    * **A one-unit ramp FIRST, and it is what catches the real regression.** A
      shared character between two adjacent quantified classes in these grammars
      is not polynomial but EXPONENTIAL (measured: 0.27 s at 20 characters, 4.3 s
      at 24, doubling per character; an interior that admits every bracket grows
      ~8x per block), so the 200 000-character input the old tests used would never
      return under a regression and the worker would be killed at ``--timeout`` --
      a lost run (class 6), not a red test. Ramping one unit at a time means the
      first over-budget size costs at most ~growth x budget, and the assertion
      fires there; only when the whole ramp passes is a long pump tried at all.
    * **Ascending long pumps** for the polynomial class, so a cubic overruns at
      2 000 before 20 000 is attempted. Each must also cost at most
      ``REDOS_SCALING_RATIO`` times the one before (above a small floor): a
      quadratic scan built from cheap steps stays under the absolute budget at
      20 000 but grows ~100x per 10x of input where a linear one grows ~10x.
    * **Minimum of two readings, and the second is taken only if the first
      overran.** A gen-2 garbage collection charged to this thread mid-search is
      the one thing that can still spend CPU here; it cannot hit two consecutive
      readings, so a first reading under budget is a verdict on its own and two
      over-budget readings are a verdict the other way. A long pump is held to
      the absolute budget and, past the first, to the ratio bound as well; two
      readings over either line end that size, so the measurement never pays a
      regression's cost more than twice per size. A long pump the next one is
      compared against otherwise takes three readings, since its cheapest is
      that comparison's baseline.

    The budgets are generous on purpose (a decade or more over the shipped cost):
    a real complexity regression is orders of magnitude, and a tight bound only
    turns runner variance into red. ``budget_seconds`` is the long pumps' absolute
    budget; a converted guard passes the constant it already carried, since a
    ratchet may only tighten.
    """
    import time

    def cheapest(text: str, budget: float) -> float:
        start = time.thread_time()
        reject(text)
        first = time.thread_time() - start
        if first < budget:
            return first
        start = time.thread_time()
        reject(text)
        return min(first, time.thread_time() - start)

    for n in REDOS_SMALL_PUMPS:
        cost = cheapest(build_pump(n), REDOS_SMALL_BUDGET_SECONDS)
        assert cost < REDOS_SMALL_BUDGET_SECONDS, (
            f"handling a {n}-unit pump cost {cost:.2f}s of CPU -- the grammar "
            "backtracks catastrophically (a body class now shares a character with "
            "an adjacent quantified run, or two alternatives can consume one span?)"
        )

    def refused(cost: float, bound: float) -> bool:
        return cost >= budget_seconds or cost > bound

    def floor_of_readings(text: str, settle_below: float, bound: float) -> float:
        # Up to three readings; stop at the first one under ``settle_below``, and
        # stop after two once both are over the line this size is held to (a verdict).
        best = float("inf")
        for attempt in range(3):
            start = time.thread_time()
            reject(text)
            best = min(best, time.thread_time() - start)
            if best < settle_below or (attempt and refused(best, bound)):
                break
        return best

    previous: tuple[int, float] | None = None
    for n in REDOS_LARGE_PUMPS:
        bound = budget_seconds
        if previous is not None:
            bound = max(REDOS_SCALING_RATIO * previous[1], REDOS_SCALING_FLOOR_SECONDS)
        # A size the next one is compared against keeps all three readings: its
        # cheapest is the baseline, and an inflated baseline would hide a regression.
        settle_below = bound if n == REDOS_LARGE_PUMPS[-1] else 0.0
        cost = floor_of_readings(build_pump(n), settle_below, bound)
        assert cost < budget_seconds, (
            f"handling a {n}-unit pump cost {cost:.2f}s of CPU -- superlinear in the " "pump length"
        )
        if previous is not None:
            assert cost <= bound, (
                f"handling a {n}-unit pump cost {cost:.3f}s of CPU against "
                f"{previous[1]:.4f}s at {previous[0]} units -- "
                f"{cost / max(previous[1], 1e-9):.0f}x for {n // previous[0]}x the "
                f"input, past the {REDOS_SCALING_RATIO}x a linear scan stays under"
            )
        previous = (n, cost)


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
def _fresh_update_ownership(_floor_monkeypatch):
    """Give every test its own update-ownership registry, and fail one that leaks a step.

    ``update_ownership`` keeps process-wide state that production paths a test
    drives fill; a leftover entry would make a later test in the same worker see
    a gap owner that belongs to an earlier one. The registry is REBOUND, not
    cleared: ``step()`` removes its entry from the list it joined, so a step an
    earlier test left open unwinds against its own list, and the inherited state
    is put back at teardown. A step still open when this test ends is a task it
    never finished, which would own the gap in whatever test runs next, so that
    fails the test that left it.
    """
    from kiro_crew import update_ownership

    registry: list = []
    _floor_monkeypatch.setattr(update_ownership, "_live", registry)
    _floor_monkeypatch.setattr(update_ownership, "_deferred_restart_until", None)
    _floor_monkeypatch.setattr(update_ownership, "_restart_refusal", None)
    yield
    open_steps = [entry.step.label for entry in registry]
    assert not open_steps, f"the test left update steps open: {open_steps}"


@pytest.fixture
def fresh_darwin_libc_handles(_floor_monkeypatch):
    """Start the test with both macOS libc handle caches unbuilt, and put them back after.

    ``platform_compat`` builds its ``libc`` (``sysctl``) and ``libproc`` handles
    once per process, from whatever ``ctypes.CDLL`` and
    ``ctypes.util.find_library`` are at the first call, and keeps the result,
    ``None`` included. A first build that lands while a test has either one
    replaced caches ``None`` or a fake for the rest of the worker, so a later
    real-kernel read reports "unreadable" and a refusal assertion passes because
    ``None`` collapses to ``False``.

    Two kinds of test request it:

    * every test in a file that replaces ``ctypes.CDLL``, ``find_library`` or one
      of the cached handle names below, through the file's module
      ``pytestmark``, so whatever a test builds in that window is
      dropped at its teardown instead of leaking into the next test. Requested
      per file, not autouse, because only those files can damage the cache;
      ``test_darwin_libc_handle_ratchet.py`` fails a file that patches without
      requesting it.
    * every test that reads or builds either handle, so it starts unbuilt
      whatever ran before it in the worker.

    The tree-walk and fd prototype flags belong to the ``libproc`` handle they
    were declared on, so they are reset with it. Patched through
    ``_floor_monkeypatch`` so a test's ``monkeypatch.undo()`` cannot lift it, and
    every value is put back at teardown.
    """
    from kiro_crew import platform_compat

    for name, unbuilt in (
        ("_darwin_libc_sysctl", None),
        ("_darwin_libc_sysctl_loaded", False),
        ("_darwin_libproc", None),
        ("_darwin_libproc_loaded", False),
        ("_darwin_libproc_tree_bound", False),
        ("_darwin_libproc_fd_bound", False),
    ):
        _floor_monkeypatch.setattr(platform_compat, name, unbuilt)


@pytest.fixture(autouse=True)
def _a_shared_monkeypatch_first(monkeypatch):  # flake-ok: patches nothing; only fixes setup order
    """Build the test's shared ``monkeypatch`` before every other autouse fixture here.

    No autouse fixture in this file patches through the shared instance (each uses
    ``_floor_monkeypatch``, so a test's ``monkeypatch.undo()`` cannot lift it), and so
    without this anchor the shared instance would be created last and undone FIRST,
    before the teardowns below. Fixtures here are written for it being undone after
    them: ``_no_leaked_interleave_hook`` checks on the way in for exactly that reason,
    and the teardown tripwire is a hook rather than a fixture because of it. Named to
    sort first: autouse fixtures in one conftest are set up in name order.

    Every function-scoped fixture a test file declares is therefore set up after the
    shared instance and torn down before it. A fixture holding an attribute through
    ``with patch(...)`` puts the original back first; a ``monkeypatch.setattr`` the
    test made over that attribute is undone second and re-installs the fixture's
    stand-in for every later test on the worker. Scope such a patch to the one call
    with its own ``with patch(...)``; the tripwire fails a test that leaves a
    ``LEAK_SENTINELS`` entry rebound.
    """
    yield


@pytest.fixture(autouse=True)
def _windows_restrict_to_owner_stub(request, _floor_monkeypatch):
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

    Note the lockdown does not spawn anything -- it applies the DACL through
    ``advapi32`` in-process -- so the subprocess-stub collision this fixture was
    built for is mostly gone. The stub is kept because a hermetic test that
    patches the SID resolver or the writer seam can still trip the fail-loud
    OSError, and narrowing it is a change to hundreds of tests' blast radius
    rather than part of the DACL swap.
    """
    if not platform_compat.IS_WINDOWS or request.module.__name__ in (
        "test_platform_compat",
        # The topic siblings of test_platform_compat: every test of that family runs
        # against the real lockdown, whichever file holds it.
        "test_platform_compat_filesystem",
        "test_platform_compat_kill_reap",
        "test_platform_compat_listening_ports",
        "test_platform_compat_owner_only",
        "test_platform_compat_pinned_tools",
        "test_platform_compat_process_identity",
        "test_platform_compat_process_tree",
        "test_platform_compat_resources",
        "test_platform_compat_trusted_bins",
        "test_platform_compat_windows_tree",
        # Exempted for the same reason as test_platform_compat: these modules own
        # the direct Windows-branch coverage for the owner-only lockdown, so a
        # stub would make their assertions vacuous. They were passing on Linux
        # (where the fixture is inert) and silently asserting nothing on Windows.
        "test_platform_compat_coverage_filesystem",
        "test_config_rmw_preserves_settings",
        "test_spawn_audit",
    ):
        yield
        return
    _floor_monkeypatch.setattr(platform_compat, "restrict_to_owner", lambda p: None)
    _floor_monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", lambda p: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_aim_skills_dir(_floor_monkeypatch):
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

    _floor_monkeypatch.setattr(DefaultMcpToolingProvider, "extra_skills", lambda self: [])


#: The per-test timeout of every test in a ``tree_scan_*`` xdist group that carries no
#: ``pytest.mark.timeout`` of its own. Those groups hold the gates that parse or walk a
#: whole tree. Most take 10-37 s of CPU alone on a fast host, and a hosted runner is
#: about three times slower, which is past the suite-wide ``--timeout=120``. 300 s is
#: eight times 37.5 s. A scan measured above 37.5 s carries its own larger timeout.
#: ``test/test_tree_scan_budget.py`` pins this hook.
TREE_SCAN_TIMEOUT_SECS = 300


def in_tree_scan_group(item: pytest.Item) -> bool:
    """Whether *item*'s closest ``xdist_group`` mark names a ``tree_scan`` group."""
    marker = item.get_closest_marker("xdist_group")
    if marker is None:
        return False
    name = marker.kwargs.get("name", marker.args[0] if marker.args else "")
    return isinstance(name, str) and name.startswith("tree_scan")


def timeout_mark_value(marker: pytest.Mark) -> object:
    """The seconds a ``pytest.mark.timeout`` names, positional or ``timeout=``; None if none."""
    return marker.args[0] if marker.args else marker.kwargs.get("timeout")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give each ``tree_scan`` test with no timeout of its own the group default.

    pytest-timeout reads only the closest ``timeout`` mark. An explicit value is left
    alone, so a scan that needs more than the default keeps its own budget. A closest
    mark with no value (``timeout(method="thread")``) would fall back to the suite-wide
    ``--timeout``, so it takes the closest explicit value at any level (a module's
    ``timeout(600)``), or the default when there is none. That value is put in front
    of it and its ``method`` is kept.
    """
    for item in items:
        closest = item.get_closest_marker("timeout")
        if closest is not None and timeout_mark_value(closest) is not None:
            continue
        if not in_tree_scan_group(item):
            continue
        kwargs: dict[str, object] = {}
        seconds: object = TREE_SCAN_TIMEOUT_SECS
        if closest is not None:
            kwargs = {k: v for k, v in closest.kwargs.items() if k != "timeout"}
            if len(closest.args) >= 2:
                kwargs["method"] = closest.args[1]
            valued = (timeout_mark_value(m) for m in item.iter_markers("timeout"))
            seconds = next((v for v in valued if v is not None), TREE_SCAN_TIMEOUT_SECS)
        item.add_marker(pytest.mark.timeout(seconds, **kwargs), append=False)


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


# ── xdist INTERNALERROR terminal report ───────────────────
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


def _format_abandoned_run_report(crashes: list[tuple[str, str]], victims: list[str]) -> str:
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


@pytest.fixture(autouse=True)
def _reset_overlay_refresher():
    """Clear the MCP broker's overlay-refresh hook after each test.

    A test that starts a broker registers its orchestrator's refresh in a module
    global; left set, a later test's app registration would call into it.
    """
    yield
    from kiro_crew.mcp_gateway import overlay_refresh

    overlay_refresh.set_overlay_refresher(None)


#: ``_isolation_root`` / ``_isolation_dirs`` / ``_isolate_kirocrew_home`` live in the
#: ROOTDIR ``conftest.py``, not here. The data home has to be pinned for every
#: testpath, including the ~108 test modules that ship inside the package under
#: ``src/kiro_crew/apps/builtins/*/tests/`` and never see this file. The fixtures
#: below still request ``_isolation_dirs`` and resolve it up the hierarchy.


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


@pytest.fixture(autouse=True, scope="session")
def _knowledge_store_cross_thread_close():
    """Let ``KnowledgeStore._close_all_for_tests()`` close other threads' handles.

    Production connections keep SQLite's thread-affinity guard; the test seam
    needs it relaxed so a teardown on the loop thread can close the connections
    executor threads opened. Flipped once per session, before any store is built
    (the flag is read at connect time), and restored at session end.
    """
    from kiro_crew.knowledge import store as knowledge_store

    previous = knowledge_store._ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS
    knowledge_store._ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS = True
    try:
        yield
    finally:
        knowledge_store._ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS = previous


#: ``_isolate_subagents_dir``, ``_no_model_download`` and
#: ``_isolate_agent_state_sidecar`` live in the ROOTDIR ``conftest.py``. Each one
#: protects a real HOST path (the subagent registry a running gateway sweeps as
#: orphans, a 610MB model download, the operator's agent-state sidecar), so by the
#: same test the data home meets they belong to the floor that every testpath sees --
#: not to this file, which the in-package suites never load.


@pytest.fixture(autouse=True)
def _git_identity(_floor_monkeypatch, _git_global_config) -> None:
    """Make git tests hermetic: pin identity AND neutralize host global/system config.

    Two independent host-environment bleeds must be closed for git-backed tests
    (git_coord scenarios) to be deterministic across machines:

    1. Identity — a host without a global ``user.name``/``user.email`` makes
       ``git commit`` fail. Pin it via the ``GIT_AUTHOR_*``/``GIT_COMMITTER_*``
       env vars.
    2. Global/system config — the host's ``~/.gitconfig`` (via
       ``core.excludesFile`` → e.g. ``~/.gitignore_global`` containing ``*.png``)
       silently makes ``git add -A`` skip files, so a "commit a binary file"
       test sees an empty tree and gets an empty sha. Point ``GIT_CONFIG_SYSTEM``
       at ``/dev/null`` and ``GIT_CONFIG_GLOBAL`` at the root conftest's hermetic
       file, which holds only the keys that turn git's auto maintenance off, so
       no host-level config (excludes, aliases, hooks, signing) leaks into tests.
    """
    _floor_monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    _floor_monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    _floor_monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    _floor_monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    # Isolate from the host's global/system git config (Git >= 2.32).
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_git_global_config))
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


#: ``_isolate_sel_default_dir`` lives in the ROOTDIR ``conftest.py`` too, and for a
#: sharper reason than the data home: SEL's writer is a DAEMON THREAD on a process
#: singleton, so it outlives the test that first called ``sel()`` and keeps writing to
#: the directory that test resolved.


# ``_reset_platform_context`` (the per-test PlatformContext reset and the
# ``KIROCREW_PROFILE=standalone`` pin) lives in the ROOTDIR conftest, not here:
# the ~108 test modules under ``src/kiro_crew/apps/builtins/*/tests/`` never see
# this file, and an inherited enterprise ``KIROCREW_PROFILE`` failed 150+ of them
# closed on an operator's box while ``test/`` stayed green.


#: Test modules that own direct coverage of ``mcp_core``'s HTTP plumbing itself.
#: They call the real ``_post`` (or drive a tool through it) with the transport
#: patched BELOW it (``loopback_urlopen`` / ``_api_urlopen``), so a recorder
#: stub above ``_post`` would blind exactly the assertions those modules exist
#: to make. An exemption is NOT permission to reach the network: the fixture
#: below leaves ``_post`` real for these modules but replaces the transport
#: with a refuser, so a test here that forgets its own transport patch gets a
#: deterministic local failure instead of dialling the operator's gateway.
_REAL_MCP_POST_MODULES = frozenset(
    {
        "test_ephemeral_sessions",
        "test_ephemeral_sessions_recognition",
        "test_mcp_api_base_resolution",
        "test_mcp_core",
        "test_mcp_core_coverage",
        "test_mcp_internal_caller",
        "test_session_token_header_parity",
    }
)


class _InertGatewayPosts(list):
    """What an exempt module sees if it requests ``gateway_posts`` by name.

    No recorder ever appends here, so an equality check would pass vacuously
    (``== []``) or fail for a reason unrelated to the code under test. Refusing
    the comparison turns that silent vacuity into an immediate, named failure.
    """

    def __eq__(self, other: object) -> bool:  # pragma: no cover - failure path
        raise AssertionError(
            "gateway_posts is inert in a _REAL_MCP_POST_MODULES module: the"
            " recorder is not installed there, so nothing is ever appended."
            " Assert against your own transport patch instead."
        )

    __hash__ = None  # type: ignore[assignment]


def _exported_gateway_ports() -> frozenset[int]:
    """Client ports the shell that started pytest exports, read once at import.

    ``KIROCREW_PORT`` is the first step of the client's port resolution and the
    floor does not clear it, so a developer who exports it to reach their own
    gateway sends every unconfigured test there. ``KIROCREW_BOUND_PORT`` names the
    gateway that spawned the run when an agent session runs the suite. Read here,
    before any test edits the environment, so a test that sets either variable to
    its own server's port is not mistaken for the shell's choice.
    """
    ports: set[int] = set()
    for name in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT"):
        try:
            ports.add(int(os.environ.get(name, "")))
        except ValueError:
            continue
    return frozenset(ports)


_EXPORTED_GATEWAY_PORTS = _exported_gateway_ports()


def _refuse_developer_gateway(real_urlopen):
    """Wrap ``mcp_core.loopback_urlopen`` so a test cannot dial a gateway it did not start.

    Every ``mcp_core`` helper (``_get``, ``_post``, ``_patch``, ``_put``,
    ``_delete``) sends through ``_send`` and from there through this one name,
    and so does every copy of a helper another module bound by name at import.
    A client with a transport of its own (``mcp_computer._invoke``,
    ``cron_trigger``) does not pass through here. A request
    whose URL names the client's no-evidence default port, or a port in
    ``_EXPORTED_GATEWAY_PORTS``, is where a developer's own gateway listens:
    it is refused as a connection nobody answered, which is what a CI runner
    with nothing listening there returns. ``_send`` turns that into its usual
    ``{"error": ..., "refused": True}``, with this message appended. The URL's
    port also names the socket ``_send`` derives for the same attempt
    (``dashboard-<port>.sock``), so both transports are covered by one check.

    Any other target is one the test chose, by seeding ``mcp_core._API`` /
    ``_API_UNIX_SOCKET`` at its own server or exporting its own port, and goes
    to the real transport untouched. The default port is read at call time
    from ``port_resolution``, the module the client resolves it from.
    """
    from kiro_crew import port_resolution

    def _guarded(req, *args, **kwargs):
        try:
            port = urllib.parse.urlsplit(str(getattr(req, "full_url", req))).port
        except ValueError:
            port = None
        if port is not None and (
            port == port_resolution._DEFAULT_PORT or port in _EXPORTED_GATEWAY_PORTS
        ):
            raise urllib.error.URLError(
                ConnectionRefusedError(
                    errno.ECONNREFUSED,
                    "Connection refused by test/conftest.py: an unstubbed mcp_core request"
                    f" to port {port}, where a developer's own gateway listens. Stub the"
                    " helper the test calls, or point mcp_core._API at the test's own server",
                )
            )
        return real_urlopen(req, *args, **kwargs)

    return _guarded


def _refuse_developer_gateway_opener(real_build_loopback_opener):
    """Wrap ``loopback_http.build_loopback_opener`` so a test cannot dial a gateway it did not start.

    ``loopback_http.loopback_urlopen`` looks up ``build_loopback_opener`` in its own
    module on every TCP request, so wrapping this name intercepts every caller —
    ``cron_trigger``, ``cli_commands``, ``cli_server``, ``cron_script``,
    ``mcp_shared``, ``mcp_computer`` and others — regardless of how they imported
    ``loopback_urlopen``. A request whose URL names the client's no-evidence default
    port, or a port in ``_EXPORTED_GATEWAY_PORTS``, is where a developer's own
    gateway listens: refused as a connection nobody answered, which is what a CI
    runner with nothing listening there returns. Any other target is one the test
    chose (a server it started on a random port) and goes to the real opener.

    The default port is read at request time from ``port_resolution``, the module
    the client resolves it from, so a test can point it at a stand-in by patching
    that attribute and the guard will follow.
    """
    from kiro_crew import port_resolution

    def _guarded_opener():
        real_opener = real_build_loopback_opener()
        original_open = real_opener.open

        def _guarded_open(req, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
            try:
                port = urllib.parse.urlsplit(str(getattr(req, "full_url", req))).port
            except ValueError:
                port = None
            if port is not None and (
                port == port_resolution._DEFAULT_PORT or port in _EXPORTED_GATEWAY_PORTS
            ):
                raise urllib.error.URLError(
                    ConnectionRefusedError(
                        errno.ECONNREFUSED,
                        "Connection refused by test/conftest.py: an unstubbed loopback"
                        f" request to port {port}, where a developer's own gateway"
                        " listens. Stub the caller or point it at a server the test"
                        " started (see _refuse_developer_gateway_opener)",
                    )
                )
            return original_open(req, data=data, timeout=timeout)

        # Wrap only .open so add_handler and the rest of OpenerDirector's API
        # remain available to callers that use the full opener interface.
        real_opener.open = _guarded_open
        return real_opener

    return _guarded_opener


@pytest.fixture(autouse=True)
def gateway_posts(request, _floor_monkeypatch):
    """Record ``mcp_core._post`` calls instead of letting them reach a gateway.

    ``mcp_tools.control._emit_directive`` publishes every directive out of band
    via ``mcp_core._post("/api/session-directive", ...)`` on the resolved API
    port and swallows every exception — so a test that emits a directive
    without stubbing ``_post`` makes a REAL request to whatever is listening
    there, silently. On a developer machine that is the operator's own live
    gateway, and the request carries a real-looking session key. The suite only
    avoided a live write because this conftest's ``KIROCREW_HOME`` pin makes
    the client read a different instance credential, so the gateway refuses the
    call — an unrelated guard no test is entitled to rely on. Stubbing here
    makes reaching the network opt-in for the whole suite rather than opt-out.

    A RECORDER rather than a black hole, so the stub also buys coverage: the
    out-of-band publish is half of the directive contract (marker + parked
    record), and a test can request this fixture by name and assert on the
    ``(path, payload)`` records. The payload is round-tripped through JSON so a
    non-serializable body fails HERE — the point where the real ``_post`` would
    have failed (and ``_emit_directive`` would have silently swallowed it). The
    stub returns ``{}``: falsy for ``.get("ok")`` readers and free of
    ``"error"``, it invents no success shape the real ``_post`` never promised.

    Opting back in stays explicit and layered: a test's own
    ``monkeypatch.setattr(mcp_core, "_post", ...)`` simply replaces this stub
    for that test, and a module that owns direct coverage of ``_post``'s own
    plumbing lists itself in ``_REAL_MCP_POST_MODULES``. For those modules the
    transport is replaced with a refuser instead (their per-test transport
    patches override it), so the no-traffic property holds per test rather
    than resting on every future test remembering its own patch.

    The recorder covers only the ``_post`` name on ``mcp_core``. ``_get``,
    ``_patch``, ``_put`` and ``_delete`` reach a gateway the same way, and a
    module that imports a helper by name (``mcp_panel``, ``mcp_work`` and
    others do) holds the real function from collection on, before any fixture
    runs. Outside ``_REAL_MCP_POST_MODULES`` the transport all of them share is
    therefore wrapped by :func:`_refuse_developer_gateway`: a request to the port
    a developer's gateway listens on gets the refusal a CI runner gets, and a
    request to a server the test started goes through.

    Clients that import ``loopback_urlopen`` from ``kiro_crew.loopback_http``
    directly (``cron_trigger``, ``cli_commands``, ``cli_server``, ``cron_script``,
    ``mcp_shared``, ``mcp_computer``) bypass ``mcp_core``. They all call
    ``loopback_http.loopback_urlopen``, which looks up ``build_loopback_opener``
    in its own module on every request. Wrapping that name via
    :func:`_refuse_developer_gateway_opener` covers all of them regardless of how
    they imported ``loopback_urlopen``.
    """
    from kiro_crew import mcp_core

    if getattr(request.module, "__name__", "") in _REAL_MCP_POST_MODULES:

        def _refuse_network(*args, **kwargs):
            raise AssertionError(
                "test reached mcp_core's real transport: patch"
                " loopback_urlopen/_api_urlopen (or _post) in the test itself"
            )

        _floor_monkeypatch.setattr(mcp_core, "loopback_urlopen", _refuse_network)
        yield _InertGatewayPosts()
        return

    posted: list[tuple[str, dict | None]] = []

    def _capture(path: str, body: dict | None = None, **kwargs) -> dict:
        posted.append((path, json.loads(json.dumps(body)) if body is not None else None))
        return {}

    _floor_monkeypatch.setattr(mcp_core, "_post", _capture)
    _floor_monkeypatch.setattr(
        mcp_core, "loopback_urlopen", _refuse_developer_gateway(mcp_core.loopback_urlopen)
    )
    from kiro_crew import loopback_http

    _floor_monkeypatch.setattr(
        loopback_http,
        "build_loopback_opener",
        _refuse_developer_gateway_opener(loopback_http.build_loopback_opener),
    )
    yield posted


@pytest.fixture(autouse=True)
def _no_leaked_interleave_hook():
    """Fail a test that INHERITED a set ``chat_handlers._test_interleave``.

    The seam suspends a session teardown mid-pop, so a leaked hook does not
    merely pollute state -- it re-enters an unrelated test's teardown path and
    can await an event that test will never set, which under ``-n auto`` reads as
    a timeout in a file that never mentioned the seam. Nothing legitimately
    leaves it set, so this restores AND fails.

    Checked on the way IN, not at teardown, and that is not a preference. The
    supported way to set the hook is ``monkeypatch``, whose undo is registered
    against the shared ``monkeypatch`` fixture -- and that fixture is built early,
    as a dependency of an autouse fixture above, so its teardown runs AFTER this
    one. A teardown-side check therefore cannot tell a pending undo from a real
    leak and fails every legitimate test. Entry-side, the only thing that can
    still be set is a raw assignment, which is exactly the leak worth catching.
    The cost is that the report names the test that inherited the hook rather
    than the one that leaked it, so the message says so.

    Read through ``sys.modules`` rather than an import: a test that never touches
    the dashboard pays one dict lookup and does not drag ``chat_handlers`` and its
    import graph into every worker's collection.
    """
    mod = sys.modules.get("kiro_crew.dashboard.chat_handlers")
    if mod is not None and mod._test_interleave is not None:
        mod._test_interleave = None
        pytest.fail(
            "chat_handlers._test_interleave was already set on entry, so an "
            "earlier test leaked it (this test is the victim, not the cause). "
            "Set it with monkeypatch.setattr so it reverts even on failure."
        )
