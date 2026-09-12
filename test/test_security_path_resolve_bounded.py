"""The sensitive-path gates must never block the event loop on a stalled mount.

Field report (macOS, 0.6.x): ten identical watchdog crash dumps, the loop
parked in ``posixpath._joinrealpath`` under ``on_tool_call ->
is_sensitive_bash_command -> ... -> _candidate_forms``.  The tool call was
``ssh host 'cd /home/<user>/ws && ...'``; the gate ``realpath``'d the REMOTE
path token locally, ``/home`` on macOS is an autofs map answered by
opendirectoryd, and the directory server was unreachable during a VPN
transition -- so ``lstat`` blocked in the kernel for longer than the watchdog
budget.  No exception, so the ``except OSError`` never fired.  Widening the
watchdog budget from 25s to 90s only moved the crash.

These tests pin the fix: resolution is bounded; a stall is FAIL-CLOSED (the
gate refuses the path rather than matching its lexical spelling, so a
workspace symlink into a credential store cannot ride a stall); the cooldown a
stall opens is scoped to the stalled path prefix, so one wedged mount costs
one timeout per window without switching resolution off anywhere else; and a
resolution that merely FAILS (OSError) still falls back to the lexical forms,
which must fence a symlinked ``$HOME`` by its logical spelling.
"""

from __future__ import annotations

import errno
import logging
import ntpath
import os
import posixpath
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.agent_sdk import host_auth

# Captured BEFORE the autouse fixture below can stub it.  The fixture replaces this
# helper for every test in the file, so a test that wants to exercise the real state
# parsing has to hold its own reference or it silently asserts against the stub.
_REAL_BLOCKED_IN_FILESYSTEM = security.paths._worker_blocked_in_filesystem

# Likewise captured before the fixture scales them down: the tests that assert on the
# SHIPPED budgets have to read the shipped values, not the fast ones the stall tests run
# under, or they would assert the fixture's numbers back at themselves.
_REAL_CANDIDATE_BUDGET = security.paths._PATH_RESOLVE_TIMEOUT_SECS
_REAL_REBUILD_BUDGET = security.paths._PATH_RESOLVE_REBUILD_TIMEOUT_SECS
_REAL_GRACE_MAX = security.paths._PATH_RESOLVE_GRACE_MAX_SECS
_REAL_WAIT_CAP = security.paths._PATH_RESOLVE_WAIT_CAP_SECS


@pytest.fixture(autouse=True)
def _fresh_resolver_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_path_resolve_degraded", {})
    monkeypatch.setattr(security, "_path_resolve_wedged", [])
    # Short budgets keep the stall tests fast; the production values are pinned
    # separately below.
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
    # The anchor rebuild carries its OWN, larger budget in production. Scaled down
    # here for the same reason as the one above: a stall test that reaches the
    # rebuild would otherwise wait the full production budget.
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_REBUILD_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(security, "_PATH_RESOLVE_COOLDOWN_SECS", 30.0)
    # The stall doubles below stand in for a WEDGED MOUNT, so they must stand in for its
    # kernel state too: a real ``lstat`` on a dead mount sits in uninterruptible sleep,
    # whereas these block on a ``threading.Event`` and would read as merely descheduled.
    # Without this, every cooldown assertion here would exercise the load arm instead.
    # The tests that DO exercise that arm override this locally.
    # Patched on the OWNING module, not the package alias: ``_resolve_with_deadline``
    # calls this as a module global in ``security.paths``, so rebinding the re-exported
    # name on the package would not be seen by the code under test.
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: True)
    yield
    # A stubbed resolver may still hold an mc-pathres worker; drop the pool so
    # the wedge cannot leak into the next test's timing.
    ex.shutdown_maintenance_executor()


class _StalledResolver:
    """Stands in for ``os.path.realpath`` on a wedged automount: never returns
    until released, raises nothing."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, expanded: str) -> set[str]:
        with self._lock:
            self.calls.append(expanded)
        self.release.wait()
        return {expanded}


def test_symlink_alias_is_still_resolved_on_a_healthy_filesystem(tmp_path) -> None:
    # The whole point of the resolved forms is defeating a link bypass; bounding
    # the wait must not cost that on a filesystem that answers.
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    forms = security._candidate_forms(str(link / "id_rsa"))
    assert str(target / "id_rsa") in forms
    assert str(link / "id_rsa") in forms  # the lexical form is kept alongside


def test_a_stalled_resolution_is_refused_within_the_budget(monkeypatch) -> None:
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        started = time.monotonic()
        with pytest.raises(security.PathResolutionStalled) as info:
            security._candidate_forms("/home/someone/ws/../ws/file")
        elapsed = time.monotonic() - started
    finally:
        stalled.release.set()
    # Bounded: well under a second against a 0.2s budget, where the unbounded
    # call would have sat for as long as the mount did.
    assert elapsed < 1.5, f"caller blocked {elapsed:.2f}s on a stalled resolver"
    assert info.value.prefix == os.path.normpath("/home/someone")
    assert len(stalled.calls) == 1


def test_every_gate_fails_closed_on_a_stall(monkeypatch) -> None:
    # A path whose canonical form is unknown is REFUSED, never matched on its
    # lexical spelling: that is what keeps a stall from being a lever for a
    # workspace symlink into a credential store.
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        token = "/home/someone/ws/README.md"
        assert security.is_sensitive_path(token)
        assert security.is_sensitive_write_path(token)
        assert security.path_contains_sensitive("/home/someone/ws")
        assert security._is_keystone_publish_artifact("/home/someone/ws/x.tmp")
    finally:
        stalled.release.set()


def test_the_cooldown_is_scoped_to_the_stalled_prefix(monkeypatch, tmp_path) -> None:
    # One bash command can carry many path tokens against the SAME wedged mount:
    # paying the full timeout per token would put the loop straight back past
    # the watchdog, so after the first timeout its siblings must be refused for
    # free.  But the refusal must stop at that mount -- a stall on the remote
    # half of an ssh command must not switch resolution off for the local
    # workspace, which is exactly where a bypass symlink would live.
    clock = [1000.0]
    monkeypatch.setattr(security, "_path_resolve_clock", lambda: clock[0])
    real_resolver = security._resolved_spellings
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/a/one")  # times out -> opens cooldown
        assert len(stalled.calls) == 1
        for token in ("/home/a/two", "/home/a/deeper/three"):
            started = time.perf_counter()
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms(token)
            assert time.perf_counter() - started < 0.05, "cooldown must not touch the pool"
        assert len(stalled.calls) == 1, "no resolution may be attempted under the cooldown"
    finally:
        stalled.release.set()

    # A different prefix is untouched by the cooldown: resolution still runs,
    # and on a healthy filesystem a symlink there still resolves to its target.
    monkeypatch.setattr(security, "_resolved_spellings", real_resolver)
    target = tmp_path / "creds"
    target.write_text("k")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert str(target) in security._candidate_forms(str(link))

    # Past the cooldown the stalled prefix is tried again (once the released
    # worker has actually returned, so a free worker exists for the re-probe).
    deadline = time.monotonic() + 5
    while security._wedged_workers() and time.monotonic() < deadline:
        time.sleep(0.01)
    stalled2 = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled2)
    try:
        clock[0] += security._PATH_RESOLVE_COOLDOWN_SECS + 1
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/a/four")
        assert len(stalled2.calls) == 1
    finally:
        stalled2.release.set()


def test_stall_prefix_is_two_components() -> None:
    assert security._stall_prefix("/home/user/ws/file") == os.path.normpath("/home/user")
    assert security._stall_prefix("/Volumes/share/x/y") == os.path.normpath("/Volumes/share")
    assert security._stall_prefix("/tmp") == os.path.normpath("/tmp")
    assert security._stall_prefix("rel/path/file") == os.path.normpath("rel/path")


class _SlowThenFastResolver:
    """Misses the budget on its FIRST call by *delay*, then answers immediately.

    A merely slow resolution, not a wedged mount: exactly what a cold anchor rebuild
    (~130 ``realpath`` calls) looks like on a loaded runner.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, expanded: str) -> set[str]:
        with self._lock:
            self.calls.append(expanded)
            first = len(self.calls) == 1
        if first:
            time.sleep(self.delay)
        return {expanded}


def test_a_resolution_that_finishes_inside_its_grace_does_not_charge_the_prefix(
    monkeypatch,
) -> None:
    """A missed budget alone must not switch the gate off for a whole mount.

    Charging the prefix is the expensive conclusion: it makes ``is_sensitive_path()``
    answer True for EVERY path beneath that prefix, without touching the filesystem,
    until the cooldown lapses. On a host where the syscall probe cannot say WHY the
    budget was missed -- every Windows host, since ``platform.machine()`` reports
    ``AMD64``, which is absent from the syscall table -- that made one slow
    resolution refuse unrelated paths wholesale: measured as 77
    ``ArtifactError: refusing to use sensitive path as artifact root`` failures across
    four unrelated test files on one Windows shard, all from a single charged stall.

    So a resolution gets a bounded grace to finish, and one that does finish is a
    success: the value is returned, nothing is charged, and later paths under the same
    prefix are unaffected. The probe is forced to the answer it gives on those hosts,
    because that is the configuration this exists for.
    """
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: True)
    # A wide grace, so what is asserted is the BEHAVIOUR (a completed resolution is
    # honoured and charges nothing) rather than a race against a narrow window on a
    # loaded runner: the budget stays 0.2s from the autouse fixture and the delay sits
    # far inside a 4s grace.
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_FACTOR", 20.0)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_MAX_SECS", 5.0)
    slow = _SlowThenFastResolver(delay=0.5)  # misses the 0.2s budget, finishes in grace
    monkeypatch.setattr(security, "_resolved_spellings", slow)

    forms = security._candidate_forms("/home/someone/ws/file")

    assert forms, "a resolution that completed must be used, not discarded"
    assert (
        security._path_resolve_degraded == {}
    ), "a completed resolution must not leave a cooldown behind"
    # The prefix is clean, so an unrelated path under it still resolves normally.
    assert security._candidate_forms("/home/someone/other/file")

    # The grace is what buys that, not the budget: with it disabled, the SAME
    # resolution charges the prefix and every path under it is refused. Asserted here
    # rather than by deleting the production line, so the mechanism cannot be removed
    # while this test keeps passing.
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_FACTOR", 0.0)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_GRACE_MAX_SECS", 0.0)
    security._path_resolve_degraded.clear()
    slow_again = _SlowThenFastResolver(delay=0.5)
    monkeypatch.setattr(security, "_resolved_spellings", slow_again)
    with pytest.raises(security.PathResolutionStalled):
        security._candidate_forms("/home/someone/ws/file")
    assert security._stall_prefix("/home/someone/ws/file") in security._path_resolve_degraded


def test_a_resolution_that_misses_budget_and_grace_still_charges_and_refuses(
    monkeypatch,
) -> None:
    """The grace must not become a way to never fail closed.

    A genuinely wedged mount misses both waits, and there the old conclusion is the
    right one: charge the prefix, refuse, and keep refusing without re-probing. This
    pins that the fail-CLOSED stance survives the grace -- the gate answers True for a
    path it could not canonicalise, rather than matching on the lexical spelling.
    """
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/someone/ws/file")
        assert security._stall_prefix("/home/someone/ws/file") in security._path_resolve_degraded
    finally:
        stalled.release.set()


def test_the_anchor_rebuild_gets_its_own_budget_not_the_candidates(monkeypatch) -> None:
    """One budget for both sized the wait to the wrong work.

    The rebuild is ONE pool job doing ~130 ``realpath`` calls; a candidate resolution
    does one or two. Sharing the candidate's budget left the rebuild running ~130x
    closer to its ceiling, which is why the miss was reachable at all on a loaded
    runner. Asserted as a relationship, not a magic number, so the pair cannot drift
    back together.
    """
    assert (
        _REAL_REBUILD_BUDGET > _REAL_CANDIDATE_BUDGET
    ), "the rebuild does far more work per job, so its budget must be larger"
    # And it must still be bounded well inside the loop-stall watchdog it protects,
    # counting the grace it can earn on top.
    assert (
        _REAL_REBUILD_BUDGET + _REAL_GRACE_MAX < 25.0
    ), "budget plus grace must stay inside the loop-stall watchdog"


def test_the_grace_is_a_fraction_of_the_budget_so_a_tight_budget_stays_tight(
    monkeypatch,
) -> None:
    """A caller that chooses a small budget must not inherit a fixed multi-second tail.

    The grace is expressed against the caller's own budget precisely so the per-call
    budget keeps meaning something; a constant would have made the tightest caller pay
    the loosest caller's tail.
    """
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        started = time.monotonic()
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/someone/ws/file")
        elapsed = time.monotonic() - started
    finally:
        stalled.release.set()
    budget = security.paths._PATH_RESOLVE_TIMEOUT_SECS  # 0.2 under the autouse fixture
    ceiling = budget * (1 + security.paths._PATH_RESOLVE_GRACE_FACTOR)
    assert elapsed < ceiling + 0.6, f"blocked {elapsed:.2f}s against a {budget:.2f}s budget"


def test_the_stall_prefix_counts_components_past_the_drive(monkeypatch) -> None:
    """A drive letter is not a path component, and counting it as one broke Windows.

    ``"C:\\Users\\bob"`` splits to ``["C:", "Users", "bob"]`` -- no leading empty
    component, unlike a POSIX absolute path -- so keeping "the first two" kept
    ``C:\\Users``. That single key contains ``$HOME``, ``%TEMP%``, the workspace and
    the checkout, so ONE stalled resolution anywhere in the profile refused path
    resolution for the whole host until the cooldown lapsed, which is the opposite of
    the per-mount isolation the prefix exists to give. Driven through ``ntpath`` so
    the Windows spelling is asserted on every platform.
    """
    monkeypatch.setattr(security.paths.os, "path", ntpath)
    monkeypatch.setattr(security.paths.os, "sep", ntpath.sep)

    assert security._stall_prefix(r"C:\Users\bob\AppData\Local\Temp\x") == r"C:\Users\bob"
    assert security._stall_prefix(r"C:\Users\bob") == r"C:\Users\bob"
    # A UNC share root IS the mount point, so it is the whole key.
    assert security._stall_prefix(r"\\server\share\u\f") == r"\\server\share"


def test_the_stall_prefix_is_unchanged_on_posix(monkeypatch) -> None:
    """The drive split must be a no-op wherever there are no drives.

    ``posixpath.splitdrive`` returns an empty drive, so every POSIX spelling has to
    key exactly as it did before -- otherwise narrowing Windows would have widened or
    moved the Linux/macOS keys, and the cooldown isolation there is already correct.
    """
    monkeypatch.setattr(security.paths.os, "path", posixpath)
    monkeypatch.setattr(security.paths.os, "sep", posixpath.sep)

    assert security._stall_prefix("/home/runner/work/repo/f") == "/home/runner"
    assert security._stall_prefix("/tmp/pytest-of-runner/pytest-0/t0/artifacts") == (
        "/tmp/pytest-of-runner"
    )
    assert security._stall_prefix("/net/host/share/f") == "/net/host"
    assert security._stall_prefix("rel/path/file") == "rel/path"


def test_unc_paths_are_recognised_in_both_spellings() -> None:
    assert security._is_unc_path("\\\\server\\share\\project\\readme.md")
    assert security._is_unc_path("//server//share//project//readme.md")
    assert not security._is_unc_path("/home/user/file")
    assert not security._is_unc_path("C:\\Users\\user\\file")
    assert not security._is_unc_path("/")


def test_unc_paths_are_never_probed_on_windows(monkeypatch) -> None:
    # On Windows realpath() on a UNC path is a network round-trip to the named
    # host; a dead host would stall and, fail-closed, refuse an ordinary share
    # reference.  Surfaced by main's own Windows test that expects
    # ``Get-Content \\\\server\\share\\...`` to stay allowed.  UNC tokens are
    # matched lexically and never handed to the resolver.
    monkeypatch.setattr(security, "_ON_WINDOWS", True)
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        token = "//server//share//project//readme.md"
        forms = security._candidate_forms(token)
        assert forms == {os.path.normpath(token), token}
        assert not security.is_sensitive_path(token)
        assert stalled.calls == []
    finally:
        stalled.release.set()


def test_repeated_stalls_back_off_exponentially_and_recovery_resets(monkeypatch) -> None:
    # A mount that stays dead is probed rarely, not every 30s: each re-probe
    # that stalls doubles the refusal window up to the cap.  Once the mount
    # answers again the history is dropped so a later stall starts small.
    clock = [1000.0]
    monkeypatch.setattr(security, "_path_resolve_clock", lambda: clock[0])
    base = security._PATH_RESOLVE_COOLDOWN_SECS
    monkeypatch.setattr(security, "_PATH_RESOLVE_COOLDOWN_MAX_SECS", base * 4)
    stubs: list[_StalledResolver] = []
    try:
        expected = [base, base * 2, base * 4, base * 4]  # capped at the fourth
        for n, want in enumerate(expected, start=1):
            stub = _StalledResolver()
            stubs.append(stub)
            monkeypatch.setattr(security, "_resolved_spellings", stub)
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/user/x")
            until, stalls = security._path_resolve_degraded[os.path.normpath("/home/user")]
            assert stalls == n
            assert until == pytest.approx(clock[0] + want)
            # Release THIS stall so the worker is free again, then step past
            # the window: the next iteration is a genuine re-probe.
            stub.release.set()
            deadline = time.monotonic() + 5
            while security._wedged_workers() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert security._wedged_workers() == 0
            clock[0] = until + 1
        # Recovery: a resolution that completes clears the history.
        monkeypatch.setattr(security, "_resolved_spellings", lambda e: {e})
        security._candidate_forms("/home/user/x")
        assert os.path.normpath("/home/user") not in security._path_resolve_degraded
    finally:
        for stub in stubs:
            stub.release.set()


def test_a_known_stalled_prefix_is_not_reprobed_onto_the_last_free_worker(
    monkeypatch, tmp_path
) -> None:
    # A timed-out worker is never reclaimed.  With two workers, re-probing a
    # dead mount every cooldown would pin the second within two cycles and
    # leave every healthy path queueing behind wedged futures -- the per-prefix
    # isolation would hold only while free workers remained.  So a prefix with
    # a stall history is re-probed only while that leaves one worker free, and
    # once every worker is pinned nothing is submitted at all.
    assert security._MAX_PATH_RESOLVE_WORKERS == 2
    clock = [1000.0]
    monkeypatch.setattr(security, "_path_resolve_clock", lambda: clock[0])
    real_resolver = security._resolved_spellings
    first = _StalledResolver()
    second = _StalledResolver()
    try:
        monkeypatch.setattr(security, "_resolved_spellings", first)
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/user/x")  # worker 1 pinned
        assert security._wedged_workers() == 1
        clock[0] += security._PATH_RESOLVE_COOLDOWN_SECS + 1
        # Re-probe would pin the last free worker: refused without a submit,
        # and NOT charged as a stall -- nothing was observed, so the backoff
        # stays where the real stall left it.
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/user/y")
        assert len(first.calls) == 1
        assert security._path_resolve_degraded[os.path.normpath("/home/user")][1] == 1
        # The free worker still serves a healthy prefix.
        monkeypatch.setattr(security, "_resolved_spellings", real_resolver)
        target = tmp_path / "creds"
        target.write_text("k")
        link = tmp_path / "link"
        link.symlink_to(target)
        assert str(target) in security._candidate_forms(str(link))
        # A SECOND dead mount may take the last worker (no history yet) ...
        monkeypatch.setattr(security, "_resolved_spellings", second)
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/net/other/z")
        assert security._wedged_workers() == 2
        # ... after which a fresh prefix is refused immediately rather than
        # queued behind two wedged futures: nothing reaches the resolver.
        started = time.perf_counter()
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/srv/fresh/w")
        assert time.perf_counter() - started < 0.05
        assert len(second.calls) == 1
        # ... and that healthy prefix is not charged a stall it never had, so
        # it is served again the moment a worker frees up.
        assert os.path.normpath("/srv/fresh") not in security._path_resolve_degraded
    finally:
        first.release.set()
        second.release.set()


def test_a_failed_resolution_still_falls_back_to_lexical_forms(monkeypatch) -> None:
    # FAILURE (OSError inside the worker -> empty set) is not a STALL: it keeps
    # the pre-existing lexical fallback and never refuses.
    monkeypatch.setattr(security, "_resolved_spellings", lambda e: set())
    token = "/home/someone/ws/../ws/README.md"
    assert security._candidate_forms(token) == {os.path.normpath(token), token}
    assert not security.is_sensitive_path(token)
    assert security.is_sensitive_path("~/.aws/credentials")


def test_symlinked_home_is_fenced_by_its_logical_spelling_when_resolution_fails(
    monkeypatch, tmp_path
) -> None:
    # Found while writing these tests on a cloud desktop where
    # ``/home/x -> /local/home/x``: the target set was anchored on the RESOLVED
    # home only (the cache keys on resolved roots), so once the candidate could
    # not be resolved, a key path spelled through the link matched nothing -- a
    # fail-OPEN that predates the bound and was merely masked by candidate
    # resolution always completing.  The logical spelling is now an anchor.
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    (real_home / ".ssh").mkdir()
    (real_home / ".ssh" / "id_rsa").write_text("k")
    link_home = tmp_path / "link-home"
    link_home.symlink_to(real_home, target_is_directory=True)
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows; set both so
    # the logical home is the link on every platform.
    monkeypatch.setenv("HOME", str(link_home))
    monkeypatch.setenv("USERPROFILE", str(link_home))
    security._home_targets_cache.clear()
    assert str(security._resolved_root_key()[0]) == str(real_home.resolve())

    monkeypatch.setattr(security, "_resolved_spellings", lambda e: set())
    try:
        # Spelled through the LINK, unresolvable: must still be denied.
        assert security.is_sensitive_path(str(link_home / ".ssh" / "id_rsa"))
        # Spelled through the REAL home: denied as before.
        assert security.is_sensitive_path(str(real_home / ".ssh" / "id_rsa"))
        # And an ordinary file under either spelling stays allowed.
        assert not security.is_sensitive_path(str(link_home / "ws" / "README.md"))
    finally:
        security._home_targets_cache.clear()


def test_production_budgets_sit_under_the_watchdog(monkeypatch) -> None:
    # The gate runs on the event loop.  Its one paid timeout per cooldown
    # window has to land below the watchdog's 15s enrichment tier, with room
    # for the rest of the tool call, or the fix merely narrows the crash.
    monkeypatch.undo()
    assert 0 < security._PATH_RESOLVE_TIMEOUT_SECS <= 5.0
    assert security._PATH_RESOLVE_COOLDOWN_SECS >= 10.0


class _TimedResolutionPool:
    """A started resolution whose result consumes only injected clock time."""

    def __init__(self, monkeypatch):
        self.clock = [1000.0]
        self.delay = 0.25
        self.error = False
        self.queued = False
        self.submissions: list[str] = []
        self.waits: list[float] = []
        monkeypatch.setattr(security, "_path_resolve_clock", lambda: self.clock[0])
        monkeypatch.setattr(security, "path_resolve_executor", lambda: self)
        # Creating these on an implementation without accounting lets the behavioural
        # assertions, rather than a missing attribute, demonstrate the missing bound.
        monkeypatch.setattr(security.paths, "_PATH_RESOLVE_WAIT_CAP_SECS", 1.0, raising=False)
        monkeypatch.setattr(security.paths, "_PATH_RESOLVE_WAIT_WINDOW_SECS", 25.0, raising=False)
        monkeypatch.setattr(security.paths, "_path_resolve_thread_waits", {}, raising=False)

    def submit(self, fn, arg):
        self.submissions.append(arg)
        value = None if self.queued else fn(arg)
        pool = self

        class _Future:
            remaining = pool.delay

            def result(self, timeout):
                pool.waits.append(timeout)
                elapsed = min(timeout, self.remaining)
                pool.clock[0] += elapsed
                self.remaining -= elapsed
                if self.remaining > 0:
                    raise FutureTimeoutError
                if pool.error:
                    raise ValueError("resolution failed")
                return value

            def cancel(self):
                return pool.queued

            def done(self):
                return self.remaining <= 0

        return _Future()


def test_cumulative_wait_stops_distinct_prefixes_without_charging(monkeypatch) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    for n in range(4):
        token = f"/mount/{n}/file"
        assert security._run_resolution_bounded(token, lambda p: p, budget=1.0) == token
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/fresh/mount/file", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 4
    assert security._path_resolve_degraded == {}, "an unsubmitted path earns no cooldown"


def test_cumulative_wait_clamps_budget_and_skips_exhausted_grace(monkeypatch) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 0.75
    assert security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    pool.delay = 100.0
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=8.0)
    assert pool.waits == [1.0, 0.25], "neither the budget nor grace may exceed the remainder"
    assert pool.clock[0] == 1001.0
    assert (
        security._stall_prefix("/mount/two/file") not in security._path_resolve_degraded
    ), "a budget/grace clamped by the allowance says nothing about the mount"


def test_cumulative_wait_clamps_and_accounts_for_grace(monkeypatch) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 0.5
    assert security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    pool.delay = 100.0
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=0.25)
    assert pool.waits == [1.0, 0.25, 0.25]
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/three/file", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 2
    assert security._stall_prefix("/mount/three/file") not in security._path_resolve_degraded


@pytest.mark.parametrize(
    "outcome",
    ["grace_success", "grace_failure", "on_time_failure", "load_timeout", "queued_timeout"],
)
def test_cumulative_wait_accounts_for_every_result_outcome(monkeypatch, outcome) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 1.0
    pool.error = outcome.endswith("failure")
    pool.queued = outcome == "queued_timeout"
    if outcome.endswith("timeout"):
        pool.delay = 100.0
        monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: False)
        with pytest.raises(security.PathResolutionStalled):
            security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    else:
        budget = 1.0 if outcome == "on_time_failure" else 0.5
        value = security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=budget)
        assert value == (None if pool.error else "/mount/one/file")
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 1
    assert security._path_resolve_degraded == {}


def test_cumulative_wait_requires_a_quiet_window_before_reset(monkeypatch) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 0.5
    assert security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    pool.clock[0] += 24.0
    assert security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=1.0)
    pool.clock[0] += 1.0  # past the first wait's window, but not the second's
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/three/file", lambda p: p, budget=1.0)
    pool.clock[0] += 24.0
    assert security._run_resolution_bounded("/mount/three/file", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 3, "refusals must not extend the quiet window"


def test_cumulative_wait_is_independent_for_each_calling_thread(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 1.0
    with ThreadPoolExecutor(max_workers=1) as background:
        assert background.submit(
            security._run_resolution_bounded, "/mount/background/file", lambda p: p, budget=1.0
        ).result(timeout=5.0)
        assert security._run_resolution_bounded("/mount/loop/file", lambda p: p, budget=1.0)
        with pytest.raises(security.PathResolutionStalled):
            background.submit(
                security._run_resolution_bounded, "/mount/background/next", lambda p: p, budget=1.0
            ).result(timeout=5.0)
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/loop/next", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 2
    assert len(security.paths._path_resolve_thread_waits) == 2
    assert security._path_resolve_degraded == {}


def test_cumulative_wait_cleanup_preserves_live_thread_budgets(monkeypatch) -> None:
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 1.0
    assert security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    waits = security.paths._path_resolve_thread_waits
    # Negative ids cannot alias a live Python thread id.
    waits.update({-n: (pool.clock[0] - 1.0, 1.0) for n in range(1, 66)})
    waits[-66] = (pool.clock[0] + 25.0, 1.0)
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=1.0)
    assert len(waits) == 2
    assert waits[-66][1] == 1.0, "cleanup must not refund another live thread's wait"
    assert len(pool.submissions) == 1


def test_cumulative_production_wait_leaves_watchdog_headroom() -> None:
    cap = getattr(security.paths, "_PATH_RESOLVE_WAIT_CAP_SECS", float("inf"))
    window = getattr(security.paths, "_PATH_RESOLVE_WAIT_WINDOW_SECS", 0.0)
    assert 0 < cap <= _REAL_REBUILD_BUDGET + _REAL_GRACE_MAX < 25.0
    assert window >= 25.0, "a reset must separate bursts by a full watchdog interval"


def test_a_budget_or_grace_clamped_by_the_allowance_never_charges_the_prefix(
    monkeypatch,
) -> None:
    """A wait cut short by the calling thread's allowance proves nothing about the mount.

    Charging the prefix here would recreate the false-charge blast radius this cumulative bound
    exists to close, in a new arm: on a host where the syscall probe cannot discriminate (every
    Windows host, and Apple silicon), the grace is the ONLY signal available, and a clamp that
    truncates it below its entitled length answers nothing either way. The resolution never
    finishes in this test, so a charge here could only be explained by the clamp itself, not by
    anything the filesystem did.
    """
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 0.5
    # Spend the allowance down to a sliver: the next call's budget and grace both get
    # clamped far below what an 8s/32s request would otherwise be entitled to.
    assert security._run_resolution_bounded("/mount/one/file", lambda p: p, budget=1.0)
    pool.delay = 100.0  # never finishes: the clamp is the only reason for the refusal
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/two/file", lambda p: p, budget=8.0)
    assert (
        security._path_resolve_degraded == {}
    ), "a clamp-only refusal must not open a cooldown across the whole prefix"
    # A later path under the SAME prefix is still probed once the allowance frees up.
    pool.clock[0] += 25.0
    pool.delay = 0.1
    assert security._run_resolution_bounded("/mount/two/other", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 3


def test_a_long_run_of_fast_successes_below_the_floor_never_exhausts_the_allowance(
    monkeypatch,
) -> None:
    """Bulk work paying sub-floor round-trip overhead must never trade a crash for an outage.

    A wait this short is resolver-pool overhead, not filesystem latency -- exactly the shape a
    project-tree listing, a knowledge-indexing pass, or a directory-wide
    ``path_contains_sensitive`` scan produces. Summing it would let ordinary bulk work exhaust
    the allowance with zero mount evidence and then refuse EVERY path for the quiet window: a
    reachable silent host-wide refusal, which is precisely the blast radius this bound removes.
    """
    pool = _TimedResolutionPool(monkeypatch)
    pool.delay = 0.02  # well under the 100ms floor: pool round-trip, not filesystem latency
    for n in range(500):
        assert security._run_resolution_bounded(f"/mount/{n}/file", lambda p: p, budget=1.0)
    assert len(pool.submissions) == 500
    window_end, seconds_spent = security.paths._path_resolve_thread_waits.get(
        threading.get_ident(), (0.0, 0.0)
    )
    assert seconds_spent == 0.0, "sub-floor waits must not accumulate toward the allowance"
    # The allowance is untouched, so a distinct prefix is still probed with no refusal.
    assert security._run_resolution_bounded("/mount/fresh/file", lambda p: p, budget=1.0)


def test_a_run_of_slow_but_completing_waits_still_exhausts_the_allowance(monkeypatch) -> None:
    """The reviewer's slow-but-completing scenario: waits that each finish just under budget.

    13 waits at ~2s apiece is ~25s of loop block -- far above any sane floor -- so the floor must
    not be implemented as "only count timeouts": every one of these clears 100ms by a wide margin
    and must still count, or this exact shape reopens the unbounded-loop-block case the PR closes.
    Uses the PRODUCTION allowance (12s), not the pool fixture's scaled-down test cap, so the
    exhaustion point matches the reviewer's own arithmetic (six waits at 1.9s spend 11.4s of the
    12s cap; the seventh's remaining allowance of 0.6s is below what a 1.9s-long resolution needs,
    so its budget is clamped and it times out on the allowance, not the mount -- still a refusal,
    still driven entirely by real filesystem-latency waits, never by the sub-floor exclusion).
    """
    pool = _TimedResolutionPool(monkeypatch)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_WAIT_CAP_SECS", _REAL_WAIT_CAP)
    pool.delay = 1.9  # well over the floor, and under the 2.0s default candidate budget
    for n in range(6):
        assert security._run_resolution_bounded(f"/mount/{n}/file", lambda p: p, budget=2.0)
    with pytest.raises(security.PathResolutionStalled):
        security._run_resolution_bounded("/mount/seven/file", lambda p: p, budget=2.0)
    assert pool.waits[:6] == [2.0] * 6, "the first six must run to their full requested budget"
    assert pool.waits[6] < 2.0, "the seventh's budget must be clamped by the exhausted allowance"


# ---------------------------------------------------------------------------
# The TARGET anchors -- $HOME, the override roots and the keystone leaves --
# are the other half of every gate, and until the change these tests pin they
# were still ``realpath``'d inline on the event loop.  Field report (Windows,
# 0.6.x): the loop-stall dump's main thread sat in ``_home_dir_targets_uncached
# -> ntpath.realpath`` for the full 25s budget while a full test run plus six
# subagents saturated the disk, and the gateway exited with every subagent.
# Bounded candidate resolution (above) could not help: the stall was in the
# anchors, not the candidate.
#
# INVARIANT under test: the gate only compares against anchors resolved fresh,
# canonically, within the budget; anything else refuses.  Three weaker
# fallbacks were each found open in review -- lexical spellings, a UNC skip,
# and serving the previous canonical resolution -- and are pinned shut below.
# ---------------------------------------------------------------------------


class _StalledRealpath:
    """Stands in for ``os.path.realpath`` on a slow-to-stat home: blocks until
    released, raises nothing, and records what it was asked to resolve."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, path: str) -> str | None:
        with self._lock:
            self.calls.append(path)
        self.release.wait()
        return path


def _clear_override_roots(monkeypatch) -> None:
    """Unset every anchor variable, the host's own AND each harness's.

    A harness credential home is declared rather than listed in
    ``_OVERRIDE_ROOT_ENVS``, so iterating that tuple alone would leave a developer
    machine's exported ``CODEX_HOME`` anchoring a real extra root -- and a case that
    counts resolutions would then count one the assertion does not expect.
    """
    for _field, env in security._OVERRIDE_ROOT_ENVS:
        monkeypatch.delenv(env, raising=False)
    for env in host_auth.home_override_env_vars():
        monkeypatch.delenv(env, raising=False)


def test_a_stalled_root_anchor_refuses_within_the_budget(monkeypatch, tmp_path) -> None:
    _clear_override_roots(monkeypatch)
    security._home_targets_cache.clear()
    security._resolved_root_key()  # a warm, canonical resolution must NOT be served later
    stalled = _StalledRealpath()
    monkeypatch.setattr(security, "_realpath_or_none", stalled)
    try:
        started = time.monotonic()
        with pytest.raises(security.PathResolutionStalled):
            security._resolved_root_key()
        elapsed = time.monotonic() - started
        # ...and every gate turns that into a refusal, exactly as it does for a
        # stalled candidate: an ordinary workspace file is denied, not passed on
        # a stale or lexical anchor set.
        assert security.is_sensitive_path(str(tmp_path / "ws" / "README.md")) is True
        assert security.path_contains_sensitive(str(tmp_path / "ws")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    assert elapsed < 1.0
    logical_home = str(security.Path.home())
    assert stalled.calls == [logical_home]
    # The stall was recorded against the home's prefix, the same bookkeeping a
    # candidate stall uses, so the anchors do not re-probe every 0.1s.
    assert security._stall_prefix(logical_home) in security._path_resolve_degraded


def test_a_stalled_anchor_is_not_reprobed_until_the_cooldown_lapses(monkeypatch) -> None:
    _clear_override_roots(monkeypatch)
    clock = [1_000.0]
    monkeypatch.setattr(security, "_path_resolve_clock", lambda: clock[0])
    security._home_targets_cache.clear()
    stalled = _StalledRealpath()
    monkeypatch.setattr(security, "_realpath_or_none", stalled)
    logical_home = str(security.Path.home())
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._resolved_root_key()
        assert stalled.calls == [logical_home]
        # Inside the cooldown: refused at once, nothing submitted -- a rebuild
        # of the target set every 0.1s must not queue a fresh worker onto the
        # wedged mount each time.
        clock[0] += 1.0
        with pytest.raises(security.PathResolutionStalled):
            security._resolved_root_key()
        assert stalled.calls == [logical_home]
        # Past the cooldown the anchor is probed again -- once the wedged
        # worker has been reclaimed, since a known-stalled prefix is never
        # re-probed onto the last free worker (pinned above).
        clock[0] += security._PATH_RESOLVE_COOLDOWN_SECS + 1.0
        stalled.release.set()
        deadline = time.monotonic() + 5.0
        while security._wedged_workers() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert security._wedged_workers() == 0
        roots = security._resolved_root_key()
        assert stalled.calls.count(logical_home) == 2
        assert roots.logical_home == logical_home
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()


def test_root_anchors_resolve_in_one_pool_hop(monkeypatch, tmp_path) -> None:
    # ``_resolved_root_key`` runs on the event loop once per is_sensitive_path
    # call; one thread hop per root would cost more than the inline realpath it
    # replaces.  Every root -- the host's own and each declared harness home --
    # travels in one submission.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    submissions: list[str] = []
    real_executor = security.path_resolve_executor

    class _Counting:
        def submit(self, fn, *args):
            submissions.append(getattr(fn, "__name__", repr(fn)))
            return real_executor().submit(fn, *args)

    monkeypatch.setattr(security, "path_resolve_executor", lambda: _Counting())
    security._home_targets_cache.clear()
    try:
        roots = security._resolved_root_key()
    finally:
        security._home_targets_cache.clear()
    assert submissions == ["_resolve_root_anchors"]
    assert roots.crew_home == str(security.Path(tmp_path / "crew").resolve())
    assert roots.kiro_home == str(security.Path(tmp_path / "kiro").resolve())
    # A harness's credential home travels in the same worker call as the host's own
    # roots, keyed by the variable its declaration names.
    assert dict(roots.adapter_roots)["CODEX_HOME"] == str(
        security.Path(tmp_path / "codex").resolve()
    )


def test_a_stalled_rebuild_refuses_even_with_a_warm_cache(monkeypatch, tmp_path) -> None:
    # The rebuild (home + every keystone leaf under KIROCREW_HOME) would
    # realpath() inline on the event loop every time the 0.1s cache expires.
    # An expired slot is NOT served through a stall: a symlink repointed during
    # the stall would move a credential out from under the stale anchor.
    _clear_override_roots(monkeypatch)
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    clock = [1_000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    security._home_targets_cache.clear()
    warm = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)  # canonical
    assert str(crew_home / "token_signing.key").casefold() in warm
    clock[0] += security._HOME_TARGETS_TTL_SECS + 0.01  # the slot expires
    logical_home = str(security.Path.home())
    stalled = _StalledRealpath()
    monkeypatch.setattr(security, "_realpath_or_none", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        # is_sensitive_path refuses rather than comparing against the expired set.
        assert security.is_sensitive_path(str(tmp_path / "ws" / "README.md")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    # One paid probe -- the root key's -- then everything under the home's
    # prefix (the rebuild included) is refused without touching the filesystem
    # for the cooldown: the ~40 leaves cost nothing, and the expired slot is
    # never handed back.
    assert stalled.calls == [logical_home]


def test_the_rebuild_is_one_pool_job(monkeypatch, tmp_path) -> None:
    # A single bash command can drive ~200 rebuilds; 40 hops each is what turns
    # a 9s gate into a 15s one.  Roots and rebuild are one submission apiece.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    submissions: list[str] = []
    real_executor = security.path_resolve_executor

    class _Counting:
        def submit(self, fn, *args):
            submissions.append(getattr(fn, "__name__", repr(fn)))
            return real_executor().submit(fn, *args)

    monkeypatch.setattr(security, "path_resolve_executor", lambda: _Counting())
    security._home_targets_cache.clear()
    try:
        targets = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
    finally:
        security._home_targets_cache.clear()
    assert len(submissions) == 2, submissions
    assert submissions[0] == "_resolve_root_anchors"
    assert str(tmp_path / "crew" / "token_signing.key").casefold() in targets


def test_a_repointed_override_root_is_never_served_stale_through_a_stall(
    monkeypatch, tmp_path
) -> None:
    # The review scenario, round three: KIROCREW_HOME is a symlink, the anchors
    # were resolved while it pointed at A, then it is repointed at B while the
    # home stalls.  A gate that served the previous canonical roots would still
    # anchor on A and let the canonical B credential through; refusing does not.
    _clear_override_roots(monkeypatch)
    real_a = tmp_path / "a" / "kirocrew"
    real_b = tmp_path / "b" / "kirocrew"
    real_a.mkdir(parents=True)
    real_b.mkdir(parents=True)
    link = tmp_path / "link-crew"
    try:
        link.symlink_to(real_a, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover -- Windows w/o privilege
        pytest.skip("symlink creation not permitted on this platform")
    monkeypatch.setenv("KIROCREW_HOME", str(link))
    clock = [1_000.0]
    monkeypatch.setattr(security.time, "monotonic", lambda: clock[0])
    security._home_targets_cache.clear()
    assert security.is_sensitive_path(str(real_a / "security_policy.json")) is True  # warm on A
    clock[0] += security._HOME_TARGETS_TTL_SECS + 0.01
    link.unlink()
    link.symlink_to(real_b, target_is_directory=True)  # repointed...
    real_resolver = security._realpath_or_none
    stalled = _StalledRealpath()
    monkeypatch.setattr(security, "_realpath_or_none", stalled)  # ...under a stall
    try:
        # Only the anchors stall; the candidate resolves through the real
        # resolver on its own healthy prefix, exactly as in the review scenario.
        assert security.is_sensitive_path(str(real_b / "security_policy.json")) is True
        assert security.is_sensitive_path(str(link / "security_policy.json")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    # And once the disk answers again, B is anchored canonically.
    monkeypatch.setattr(security, "_realpath_or_none", real_resolver)
    for _ in range(500):  # the clock is frozen, so bound the wait by iterations
        if not security._wedged_workers():
            break
        time.sleep(0.01)
    security._home_targets_cache.clear()
    security._path_resolve_degraded.clear()
    assert str(security.Path(real_b / "security_policy.json").resolve()).casefold() in (
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
    )


def test_a_unc_home_still_has_its_anchors_resolved(monkeypatch) -> None:
    # The review scenario, round two: a UNC home (a roaming profile on
    # ``\\server\share``) with a junction inside KIROCREW_HOME.  The UNC
    # shortcut is a stance about agent-supplied CANDIDATE tokens -- a share
    # spelling is how an agent names a share, and the fence holds no UNC
    # targets -- so it must not skip the anchors, or the junction is never
    # canonicalised and a canonical-spelling request misses the governance file.
    _clear_override_roots(monkeypatch)
    monkeypatch.setattr(security, "_ON_WINDOWS", True)
    unc_home = "\\\\server\\share\\user"
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", unc_home)
    monkeypatch.setenv("USERPROFILE", unc_home)
    calls: list[str] = []

    def canonicalising(path: str) -> str:
        calls.append(path)
        return path + "\\canonical"  # stands in for the junction's target

    monkeypatch.setattr(security, "_realpath_or_none", canonicalising)
    security._home_targets_cache.clear()
    try:
        roots = security._resolved_root_key()
    finally:
        security._home_targets_cache.clear()
    assert roots.logical_home == unc_home
    assert calls == [unc_home], "the UNC home was probed, on the pool"
    assert roots.home == unc_home + "\\canonical"
    # ...while the candidate-side shortcut is untouched: a UNC token is still
    # matched lexically and never probed.
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        assert security._resolved_forms_bounded("\\\\server\\share\\file") == set()
    finally:
        stalled.release.set()
    assert stalled.calls == []


def test_sandbox_mask_resolves_inline_and_never_sees_a_stall(monkeypatch, tmp_path) -> None:
    # ``sandbox_credential_targets`` already runs off the loop (the spawn
    # preflight wraps it in asyncio.to_thread), and its caller's exception
    # ladder does not know PathResolutionStalled (found in review).  It
    # therefore resolves the roots inline and simply waits: an open cooldown on
    # the home's prefix must neither raise nor degrade the mask.
    _clear_override_roots(monkeypatch)
    crew_home = tmp_path / "crew"
    crew_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew_home))
    logical_home = str(security.Path.home())
    prefix = security._stall_prefix(logical_home)
    security._path_resolve_degraded[prefix] = (security._path_resolve_clock() + 1_000.0, 1)
    with pytest.raises(security.PathResolutionStalled):
        security._resolved_root_key()  # the gate's path refuses...
    mask = security.sandbox_credential_targets()  # ...the mask does not
    assert any(p.startswith(str(security.Path(crew_home).resolve())) for p in mask)


def test_a_descheduled_worker_does_not_charge_the_prefix(monkeypatch) -> None:
    # THE LOAD ARM.  A worker that STARTED and then lost the CPU has learned nothing
    # about the mount, so charging the prefix converts ordinary contention into a
    # cooldown that refuses every path under it -- including, in the field, every
    # scheduled cron script for as long as the ceiling allowed.  The refusal of THIS
    # resolution is unchanged: the gate still fails closed, it just stops generalising
    # from one descheduled thread to a whole subtree.  kirodotdev/KiroCrew#9482.
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: False)
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/someone/ws/file")
    finally:
        stalled.release.set()
    assert os.path.normpath("/home/someone") not in security._path_resolve_degraded


def test_a_worker_blocked_in_the_kernel_still_charges_the_prefix(monkeypatch) -> None:
    # NEGATIVE CONTROL for the test above, and the reason it is not simply a weakening:
    # with the SAME stall, a worker in uninterruptible sleep IS evidence about the
    # filesystem and must still open the cooldown.  If this ever fails together with
    # the test above, the discriminator has disabled the escalation wholesale rather
    # than narrowed it to the case it was meant for.
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: True)
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/someone/ws/file")
    finally:
        stalled.release.set()
    assert os.path.normpath("/home/someone") in security._path_resolve_degraded


def test_the_discriminator_reads_a_running_thread_as_not_blocked() -> None:
    """The calling thread is on-CPU by definition, so it must read as NOT blocked.

    This is the positive control proving ``/proc`` is really being parsed: a helper that
    always returned True would pass every other assertion in this file while restoring the
    behaviour the change exists to fix. The second half pins the opposite contract -- when
    ``/proc`` cannot answer, the prefix is still charged.
    """
    if not os.path.isdir("/proc/self/task"):  # pragma: no cover - Linux-only probe
        pytest.skip("/proc/self/task is Linux-only")
    assert _REAL_BLOCKED_IN_FILESYSTEM(threading.get_native_id()) is False
    # ...and it fails TOWARD the pre-existing behaviour when /proc cannot answer, so a
    # non-Linux host or an exited thread keeps charging the prefix as it did before.
    assert _REAL_BLOCKED_IN_FILESYSTEM(None) is True
    assert _REAL_BLOCKED_IN_FILESYSTEM(2**31 - 1) is True


def test_a_future_claimed_at_the_deadline_aborts_instead_of_probing(monkeypatch) -> None:
    # A queued future can be claimed by a freeing worker in the same instant the
    # budget fires: cancel() fails, yet the resolution has not begun.  The
    # timeout arm classifies it never-run, and the handshake makes that binding:
    # the late worker sees the abandonment and returns WITHOUT touching the
    # filesystem -- so it cannot probe a wedged mount while untracked, and there
    # is nothing to charge or to count as wedged.
    probes: list[str] = []

    def _resolver(expanded: str) -> set[str]:
        probes.append(expanded)
        return {expanded}

    monkeypatch.setattr(security, "_resolved_spellings", _resolver)

    captured: list = []

    class _ClaimedAtDeadline:
        """Times out, and refuses cancellation as a just-claimed future does."""

        def result(self, timeout=None):  # noqa: ANN001, ANN202, ARG002
            raise FutureTimeoutError

        def cancel(self) -> bool:
            return False

        def done(self) -> bool:
            return False

    class _Pool:
        def submit(self, fn, arg):  # noqa: ANN001, ANN202
            # Hold the callable instead of running it: the worker has claimed
            # the future but not yet entered it when the deadline fires.
            captured.append((fn, arg))
            return _ClaimedAtDeadline()

    tracked: list = []
    monkeypatch.setattr(security.paths, "_path_resolve_wedged", tracked)
    monkeypatch.setattr(security.paths, "path_resolve_executor", lambda: _Pool())

    with pytest.raises(security.PathResolutionStalled):
        security._candidate_forms("/home/someone/ws/file")

    assert security._path_resolve_degraded == {}, "a late claim must not open a cooldown"
    assert security.paths._wedged_workers() == 0
    # The worker only now gets around to running the future's callable: the
    # handshake sends it straight back without a filesystem probe.
    fn, arg = captured[0]
    assert fn(arg) is None
    assert probes == [], "an abandoned resolution must never touch the filesystem"


def test_a_saturated_pool_refuses_without_charging_or_tracking(caplog) -> None:
    # THE QUEUED ARM, end to end on the real pool.  kirodotdev/KiroCrew#9482:
    # simultaneous cron fires pin both ``mc-pathres`` workers, so a third
    # resolution times out having never STARTED.  Queue wait is evidence about
    # load, not the mount: the call is still refused (fail-closed, unchanged),
    # but no cooldown opens, nothing lands in ``_path_resolve_wedged``, and the
    # pool-exhaustion guard reads 0 -- otherwise one busy morning refuses every
    # path under the home prefix without a single slow filesystem operation.
    gate = threading.Event()
    pinned = threading.Semaphore(0)

    def _pin_worker() -> None:
        pinned.release()
        gate.wait()

    pool = ex.path_resolve_executor()
    blockers = [pool.submit(_pin_worker) for _ in range(ex._MAX_PATH_RESOLVE_WORKERS)]
    try:
        for _ in blockers:
            assert pinned.acquire(timeout=5.0), "blocker never reached a worker"
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.security.paths"):
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/someone/ws/file")
        assert security._path_resolve_degraded == {}, "queue wait must open no cooldown"
        assert security._path_resolve_wedged == [], "a never-started future is not wedged"
        assert security._wedged_workers() == 0
        assert any("the resolution never started" in r.message for r in caplog.records)
    finally:
        gate.set()
        for blocker in blockers:
            blocker.result(timeout=5.0)
    # The pool freed: the very next call under the SAME prefix resolves
    # normally, with no inherited backoff from the refusal above.
    forms = security._candidate_forms("/home/someone/ws/file")
    assert os.path.normpath("/home/someone/ws/file") in forms


def test_a_thread_stuck_in_a_monitored_syscall_reads_as_blocked(monkeypatch) -> None:
    """A thread parked in a kernel wait holds ONE syscall number on every sample.

    That stability is the property the discriminator rests on, and it is exactly what the
    ``/proc`` state field cannot supply: measured on this host, a thread doing ordinary
    ``lstat`` work and a thread doing nothing but burn CPU both alternate between ``R`` and
    ``S``, so state cannot separate a wedged mount from CPU starvation. A pipe read stands in
    for the wedged stat, which cannot be manufactured in a test.
    """
    if not os.path.isdir("/proc/self/task"):  # pragma: no cover - Linux-only probe
        pytest.skip("/proc/self/task is Linux-only")
    read_fd, write_fd = os.pipe()
    ready = threading.Event()
    tid_seen: list[int] = []

    def _park() -> None:
        tid_seen.append(threading.get_native_id())
        ready.set()
        os.read(read_fd, 1)

    thread = threading.Thread(target=_park, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "helper thread never started"
        tid = tid_seen[0]
        samples: list[bytes] = []
        for _ in range(20):
            time.sleep(0.02)
            try:
                with open(f"/proc/self/task/{tid}/syscall", "rb") as fh:
                    head = fh.read().split()
            except OSError:  # pragma: no cover - kernel without the syscall field
                pytest.skip("/proc/<tid>/syscall is unreadable on this kernel")
            if head:
                samples.append(head[0])
        blocking = {s for s in samples if s != b"running"}
        if len(blocking) != 1:  # pragma: no cover - scheduler noise
            pytest.skip(f"no single stable blocking syscall observed: {blocking!r}")
        blocked_nr = int(next(iter(blocking)))

        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(tid) is True
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr + 1000}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(tid) is False
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset())
        assert _REAL_BLOCKED_IN_FILESYSTEM(tid) is True, "an unmapped arch must charge"
    finally:
        os.write(write_fd, b"x")
        thread.join(5)
        os.close(read_fd)
        os.close(write_fd)


def test_a_load_arm_run_still_opens_a_cooldown_once_the_window_allowance_is_gone(
    monkeypatch,
) -> None:
    """A run of descheduled probes under one prefix must still open a cooldown.

    The per-prefix cooldown's second job is bounding event-loop wait: one call can carry many
    path tokens, and ten tokens each paying the full budget puts the event loop back past the
    watchdog. The load arm declines to charge the prefix, which removes that bound, so the arm
    has to carry it -- no single descheduled probe is evidence of a stall, but a run of them is
    still a liveness problem.

    The throwaway ``_path_resolve_wedged`` matters: this test wedges more futures than any
    other, and the autouse fixture patches the package alias rather than the owning module, so
    without it they outlive the test and can trip the pool-exhaustion guard on the same worker.
    """
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: False)
    monkeypatch.setattr(security.paths, "_path_resolve_degraded", {})
    monkeypatch.setattr(security.paths, "_path_resolve_load_probes", {})
    monkeypatch.setattr(security.paths, "_path_resolve_wedged", [])
    prefix = os.path.normpath("/home/someone")

    def _probe() -> None:
        stalled = _StalledResolver()
        monkeypatch.setattr(security, "_resolved_spellings", stalled)
        try:
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/someone/ws/file")
        finally:
            stalled.release.set()

    for _ in range(security.paths._PATH_RESOLVE_LOAD_MAX_PROBES):
        _probe()
    assert (
        prefix not in security.paths._path_resolve_degraded
    ), "probes inside the allowance must not charge the prefix"

    _probe()
    assert (
        prefix in security.paths._path_resolve_degraded
    ), "the probe past the allowance must charge the prefix and restore the bound"


def test_a_success_between_load_arm_probes_does_not_refund_the_allowance(monkeypatch) -> None:
    """An interleaved successful resolution must not reset the probe count.

    The count measures event-loop time already spent, not the prefix's health, so a later
    success cannot refund it. Clearing it on success let an alternating success /
    CPU-starved-timeout run under one prefix pay the full budget on every timeout while never
    crossing the allowance -- the watchdog exceedance the bound exists to stop, reachable from
    ordinary bursty contention rather than any extreme case.
    """
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: False)
    monkeypatch.setattr(security.paths, "_path_resolve_degraded", {})
    monkeypatch.setattr(security.paths, "_path_resolve_load_probes", {})
    monkeypatch.setattr(security.paths, "_path_resolve_wedged", [])
    prefix = os.path.normpath("/home/someone")

    def _stall_once() -> None:
        stalled = _StalledResolver()
        monkeypatch.setattr(security, "_resolved_spellings", stalled)
        try:
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/someone/ws/file")
        finally:
            stalled.release.set()

    def _succeed_once() -> None:
        monkeypatch.setattr(security, "_resolved_spellings", lambda expanded: {expanded})
        security._candidate_forms("/home/someone/ws/file")

    for _ in range(security.paths._PATH_RESOLVE_LOAD_MAX_PROBES + 1):
        _stall_once()
        if prefix in security.paths._path_resolve_degraded:
            break
        _succeed_once()

    assert (
        prefix in security.paths._path_resolve_degraded
    ), "an interleaved success must not refund the event-loop allowance"


_DOCUMENTED_SYSCALL_TABLE: dict[str, dict[str, int]] = {
    # /usr/include/asm/unistd_64.h
    "x86_64": {
        "stat": 4,
        "fstat": 5,
        "lstat": 6,
        "readlink": 89,
        "newfstatat": 262,
        "readlinkat": 267,
        "statx": 332,
    },
    # /usr/include/asm-generic/unistd.h, the aarch64 numbering: no stat/lstat/readlink there,
    # since __NR_stat sits behind an undefined __NR3264_stat and __NR_readlink is absent.
    "aarch64": {
        "readlinkat": 78,
        "newfstatat": 79,
        "fstat": 80,
        "statx": 291,
    },
}


def test_the_syscall_table_matches_the_documented_numbers_on_every_architecture() -> None:
    """Every documented entry, for BOTH architectures, must be in the shipped table.

    ``realpath`` blocks in more than ``lstat``: CPython's ``posixpath.realpath`` calls
    ``os.lstat`` AND ``os.readlink`` per component, and modern glibc can route ``stat`` through
    ``statx``. A mount that answers one of those from cache and hangs another would read as not
    blocked, take the load arm, and pay an uncharged full-budget probe per token instead of
    opening one cooldown. Asserting only the host architecture would also let the other one
    regress unnoticed, since the discriminator silently returns True for an unmapped machine.
    """
    shipped = security.paths._FS_BLOCKING_SYSCALLS_BY_ARCH
    assert set(shipped) == set(
        _DOCUMENTED_SYSCALL_TABLE
    ), f"architecture coverage differs: shipped {sorted(shipped)}"
    for arch, documented in _DOCUMENTED_SYSCALL_TABLE.items():
        assert shipped[arch] == frozenset(documented.values()), (
            f"{arch}: shipped {sorted(shipped[arch])} != documented "
            f"{sorted(documented.values())} for {', '.join(sorted(documented))}"
        )


def test_the_documented_syscall_numbers_come_from_this_host_kernel_headers() -> None:
    """The golden table is read back from the kernel headers, not taken on trust.

    Without this, the table above would only restate the constant it checks, and both could
    drift together. ``asm/unistd_64.h`` is the authority for x86_64; the aarch64 numbering
    lives in ``asm-generic/unistd.h`` and is cross-checked wherever that header is present.
    """
    checked = 0
    for header, arch, indirect in (
        ("/usr/include/asm/unistd_64.h", "x86_64", {}),
        (
            "/usr/include/asm-generic/unistd.h",
            "aarch64",
            {"newfstatat": "__NR3264_fstatat", "fstat": "__NR3264_fstat"},
        ),
    ):
        if not os.path.exists(header):  # pragma: no cover - header not installed
            continue
        with open(header, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        defines = dict(re.findall(r"^#define (\w+) (\d+)$", text, re.MULTILINE))
        for name, number in _DOCUMENTED_SYSCALL_TABLE[arch].items():
            symbol = indirect.get(name, f"__NR_{name}")
            assert defines.get(symbol) == str(number), (
                f"{header}: {symbol} is {defines.get(symbol)!r}, "
                f"the table says {number} for {arch}/{name}"
            )
            checked += 1
    if not checked:  # pragma: no cover - no kernel headers at all
        pytest.skip("no kernel syscall headers available to cross-check")


def test_an_in_s_filesystem_wait_is_sampled_stably_and_reads_as_blocked(
    monkeypatch, tmp_path
) -> None:
    """An interruptible FILESYSTEM wait must sample stably, not just an uninterruptible one.

    This is the state class the discriminator's premise depends on and the one prior evidence
    did not cover: a wedged FUSE or CIFS mount waits in ``S``, while the earlier measurements
    used a pipe ``read`` and a ``clock_nanosleep`` -- stable, but neither a filesystem
    operation. An ``openat`` on a FIFO with no writer blocks interruptibly while operating on a
    real filesystem path, which is an in-``S`` filesystem wait obtainable with no privileges and
    no mount.

    Measured on a 48-core x86_64 host: 15 of 15 samples reported state ``S`` and syscall 257
    (``openat``), with no other value observed. A genuinely wedged NFS/FUSE/CIFS mount remains
    un-observed -- see the helper's docstring -- but the sampling mechanism this rests on is
    confirmed for interruptible filesystem waits by this test.
    """
    if not os.path.isdir("/proc/self/task"):  # pragma: no cover - Linux-only probe
        pytest.skip("/proc/self/task is Linux-only")
    fifo = tmp_path / "gate"
    os.mkfifo(fifo)
    ready = threading.Event()
    tid_box: list[int] = []

    def blocker() -> None:
        tid_box.append(threading.get_native_id())
        ready.set()
        try:
            fd = os.open(fifo, os.O_RDONLY)
        except OSError:  # pragma: no cover - only on teardown races
            return
        os.close(fd)

    thread = threading.Thread(target=blocker, daemon=True)
    thread.start()
    try:
        assert ready.wait(5), "helper thread never started"
        tid = tid_box[0]
        states: set[str] = set()
        calls: set[bytes] = set()
        for _ in range(15):
            time.sleep(0.05)
            try:
                with open(f"/proc/self/task/{tid}/stat", "rb") as fh:
                    states.add(fh.read().rpartition(b")")[2].split()[0].decode())
                with open(f"/proc/self/task/{tid}/syscall", "rb") as fh:
                    head = fh.read().split()
            except (OSError, IndexError):  # pragma: no cover - kernel without these fields
                pytest.skip("/proc/<tid>/{stat,syscall} unreadable on this kernel")
            if head:
                calls.add(head[0])
        if states != {"S"} or len(calls) != 1 or b"running" in calls:
            # pragma: no cover - scheduler noise
            pytest.skip(f"no stable in-S filesystem wait observed: {states} {calls}")

        blocked_nr = int(next(iter(calls)))
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(tid) is True
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr + 1000}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(tid) is False
    finally:
        # A one-shot O_NONBLOCK write-open gets ENXIO if the blocker has not reached os.open
        # yet, leaving the reader blocked with no writer, so the writer must retry.
        deadline = time.monotonic() + 10.0
        while thread.is_alive() and time.monotonic() < deadline:
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError as exc:
                if exc.errno != errno.ENXIO:
                    raise
            thread.join(0.05)
    assert not thread.is_alive(), "the FIFO blocker thread survived teardown"
