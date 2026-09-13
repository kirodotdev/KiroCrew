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
import os
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

#: A helper program that never answers: how the tests wedge a request the way a
#: dead mount does, on every platform (a SIGSTOP would do it on POSIX only).
_WEDGED_HELPER_SOURCE = "import time\nwhile True:\n    time.sleep(3600)\n"


@pytest.fixture(autouse=True)
def _fresh_resolver_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_path_resolve_degraded", {})
    monkeypatch.setattr(security, "_path_resolve_wedged", [])
    # Short budgets keep the stall tests fast; the production values are pinned
    # separately below.
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
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


def _stub_anchor_resolver(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    """Stand *fn* in for the anchor primitive in BOTH shapes: the per-anchor
    ``_realpath_or_none`` the rebuild calls, and the batched ``_realpaths_or_none``
    the per-call root key sends as one helper request."""
    monkeypatch.setattr(security, "_realpath_or_none", fn)
    monkeypatch.setattr(security, "_realpaths_or_none", lambda paths: [fn(p) for p in paths])


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
    _stub_anchor_resolver(monkeypatch, stalled)
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
    _stub_anchor_resolver(monkeypatch, stalled)
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
    _stub_anchor_resolver(monkeypatch, stalled)
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
    _stub_anchor_resolver(monkeypatch, stalled)  # ...under a stall
    try:
        # Only the anchors stall; the candidate resolves through the real
        # resolver on its own healthy prefix, exactly as in the review scenario.
        assert security.is_sensitive_path(str(real_b / "security_policy.json")) is True
        assert security.is_sensitive_path(str(link / "security_policy.json")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    # And once the disk answers again, B is anchored canonically.
    _stub_anchor_resolver(monkeypatch, real_resolver)
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

    _stub_anchor_resolver(monkeypatch, canonicalising)
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


# ── Out-of-process resolution (the GIL-convoy regression) ──────────────────────
#
# Field report (Linux dev desktop, 64 cores, local XFS): 57 stalls in one day,
# each preceded by an "event-loop heartbeat: lag" warning, on a disk where a full
# anchor rebuild measured 2 ms. The resolver was not waiting on the disk; it was
# waiting on the GIL. ``realpath`` releases and re-acquires the GIL once per path
# component (one ``lstat`` + one ``readlink``), the anchor rebuild does ~400 of
# those per call, and each re-acquisition waits a full switch interval when any
# other thread is CPU-bound -- 400 x 5 ms = the 2 s budget, on a healthy disk.
# Every gate then refused ordinary project files as "sensitive" for the cooldown
# window. The tests below pin the fix: the resolution runs in a helper PROCESS
# (its own GIL), so one busy sibling thread cannot spend the budget.


def _hog_the_gil(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(i * i for i in range(20_000))


@pytest.fixture
def _real_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[security.pathres_client.ResolverHelper]:
    """A fresh helper process, torn down after the test; in-process mode off."""
    fresh = security.pathres_client.ResolverHelper()
    monkeypatch.setattr(security.pathres_client, "_helper", fresh)
    try:
        yield fresh
    finally:
        fresh.close()


def test_resolution_completes_inside_the_budget_while_a_sibling_thread_hogs_the_gil(
    _real_helper, monkeypatch, tmp_path
) -> None:
    """The reproduction. In-process the same call took 2-4 s on this host under one hog."""
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 2.0)
    monkeypatch.setattr(
        security.paths, "_worker_blocked_in_filesystem", _REAL_BLOCKED_IN_FILESYSTEM
    )
    security._home_targets_cache.clear()
    target = tmp_path / "ws" / "README.md"
    target.parent.mkdir()
    target.write_text("x")
    # The helper's one-time spawn is not the workload under test: it is paid once per
    # gateway, and on Windows ``Popen`` alone is hundreds of GIL handoffs.
    assert _real_helper.resolve("/") is not None
    stop = threading.Event()
    hog = threading.Thread(target=_hog_the_gil, args=(stop,), daemon=True)
    hog.start()
    try:
        time.sleep(0.05)
        started = time.monotonic()
        # Anchors (62 realpaths) AND the candidate, cold cache: the exact workload
        # that expired the budget in-process.
        refusal = security.sensitive_path_refusal(str(target))
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        hog.join()
    assert refusal is None, f"a healthy project file must not be refused: {refusal!r}"
    # The refusal is the strict check: in-process the same workload expires the 2.0 s
    # budget (measured 2.15 s) and is refused as unverifiable. The wall-clock cap is a coarse
    # bound with headroom for a loaded CI host (alone this takes ~0.3 s; under a full
    # xdist run it has been seen at 1.04 s), not the discriminator; it was measured on
    # POSIX, so on Windows the budget itself (via the refusal above) is the only bound.
    if os.name != "nt":
        assert (
            elapsed < 1.5
        ), f"resolution took {elapsed:.2f}s under one GIL hog; the convoy is back"
    assert security._path_resolve_degraded == {}, "no prefix may be charged for a healthy disk"


def test_helper_answers_are_identical_to_in_process_resolution(_real_helper, tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    paths = [str(link / "f.txt"), str(tmp_path / "missing" / "deep"), str(tmp_path)]
    for path in paths:
        answer = _real_helper.resolve(path)
        assert answer is not None
        realpath, resolved = answer
        assert realpath == os.path.realpath(path)
        assert resolved == str(security.Path(path).resolve())
    assert not _real_helper._latched()


def test_resolve_many_answers_in_order_and_matches_single_requests(_real_helper, tmp_path) -> None:
    paths = [str(tmp_path), str(tmp_path / "missing" / "deep"), "/", str(tmp_path / "x.txt")]
    many = _real_helper.resolve_many(paths)
    assert many is not None and len(many) == len(paths)
    assert many == [_real_helper.resolve(p) for p in paths]
    assert _real_helper.resolve_many([]) == []


def test_the_root_key_resolves_every_anchor_in_one_helper_request(
    _real_helper, monkeypatch, tmp_path
) -> None:
    """Every gate call re-resolves ``$HOME`` and the override roots. Measured on
    Windows CI: seven single requests per call became ~80,000 round-trips for one
    test file, and each round-trip there is a scheduler tick, so the round-trips
    were the time. They travel as one request."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    requests: list[object] = []
    real_many = _real_helper.resolve_many
    real_one = _real_helper.resolve

    def counting_many(paths):
        requests.append(list(paths))
        return real_many(paths)

    def counting_one(path):
        requests.append(path)
        return real_one(path)

    monkeypatch.setattr(_real_helper, "resolve_many", counting_many)
    monkeypatch.setattr(_real_helper, "resolve", counting_one)
    security._home_targets_cache.clear()
    roots = security._resolve_root_anchors(str(security.Path.home()))
    assert len(requests) == 1 and isinstance(requests[0], list), requests
    assert str(security.Path.home()) in requests[0]
    assert str(tmp_path / "crew") in requests[0] and str(tmp_path / "kiro") in requests[0]
    assert roots.home == os.path.realpath(str(security.Path.home()))
    assert roots.crew_home == os.path.realpath(str(tmp_path / "crew"))


def test_a_path_with_undecodable_bytes_survives_the_pipe(_real_helper, tmp_path) -> None:
    weird = str(tmp_path) + "/\udcff\udcfe/x"  # surrogate-escaped bytes, as os.fsdecode yields
    answer = _real_helper.resolve(weird)
    assert answer is not None
    assert answer[0] == os.path.realpath(weird)


def test_killing_a_wedged_helper_frees_the_pool_worker(_real_helper, monkeypatch) -> None:
    """A timed-out THREAD doing realpath is pinned for the process lifetime; a timed-out
    HELPER is killed, its worker reads EOF and is reclaimed, and the next request
    gets a fresh helper."""
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.3)
    # Stand in for a wedged mount: the helper never answers. Achieved by making the
    # helper's ``resolve`` block on the pipe the way it would on a dead lstat.
    proc = _real_helper._spawn()
    assert proc is not None and proc.stdin is not None
    original_resolve = _real_helper.resolve

    def wedged_resolve(paths):
        # Write a request the helper will never see complete (no newline), then
        # block reading -- exactly the wait a dead mount produces.
        _real_helper._inflight_pid = proc.pid
        try:
            assert proc.stdout is not None
            return proc.stdout.readline() and original_resolve(paths)
        finally:
            _real_helper._inflight_pid = None

    monkeypatch.setattr(_real_helper, "resolve", wedged_resolve)
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: True)
    with pytest.raises(security.PathResolutionStalled):
        security._resolved_forms_bounded("/some/where")
    # The helper was killed at the deadline...
    deadline = time.monotonic() + 5.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proc.poll() is not None, "the wedged helper must be killed, not waited for"
    # ...so the worker blocked on it unwound and is not pinned.
    while security._wedged_workers() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert security._wedged_workers() == 0
    # And the next request runs on a fresh helper.
    monkeypatch.setattr(_real_helper, "resolve", original_resolve)
    assert _real_helper.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert _real_helper._proc is not proc


def test_the_helper_is_chosen_under_the_request_lock(_real_helper, monkeypatch) -> None:
    """A waiter that picked its handle BEFORE the lock could hold the child another
    request's deadline then killed, read EOF, and degrade to lexical-only -- a pass
    for a workspace symlink into a credential store. Under the lock it runs after
    the abort and sees the dropped handle instead."""
    real_spawn = _real_helper._spawn
    observed: list[bool] = []

    def spawn_under_lock():
        observed.append(_real_helper._request_lock.locked())
        return real_spawn()

    monkeypatch.setattr(_real_helper, "_spawn", spawn_under_lock)
    assert _real_helper.resolve("/") is not None
    assert observed and all(observed), "the helper must be selected while holding _request_lock"


def test_a_dead_handle_found_at_the_lock_is_replaced_before_the_write(_real_helper) -> None:
    """The stale-handle case: the child is dead but still recorded (as it is between
    another request's abort and this one's turn at the lock). ``_spawn()`` runs
    UNDER the lock, sees ``poll()`` is not None, and hands the request a fresh child."""
    proc = _real_helper._spawn()
    assert proc is not None
    proc.kill()
    proc.wait(timeout=5)
    assert _real_helper._proc is proc  # the corpse, exactly as a waiter would find it
    assert _real_helper.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert _real_helper._proc is not None and _real_helper._proc is not proc


def test_a_kill_landing_after_the_handle_was_chosen_is_a_fault_not_an_answer(
    _real_helper, monkeypatch
) -> None:
    """The tighter race: the child dies after selection, before the write. The request
    reports a transport fault (``None``) -- which the candidate caller refuses
    fail-closed, see ``test_a_transport_fault_on_a_candidate_fails_closed_not_lexical``
    -- and reaps the corpse so the next caller does not inherit it. No retry: a
    retry would re-submit a possibly wedged path and pin the worker again."""
    real_spawn = _real_helper._spawn

    def spawn_then_kill():
        p = real_spawn()
        assert p is not None
        # Another request's deadline aborting the shared child: recorded the way
        # ``abort()`` records it, so the EOF reads as a wedge, never as "cannot run".
        _real_helper._killed_pids.add(p.pid)
        p.kill()
        p.wait(timeout=5)
        return p

    monkeypatch.setattr(_real_helper, "_spawn", spawn_then_kill)
    assert _real_helper.resolve("/") is None
    assert _real_helper._proc is None, "the dead child is reaped, not kept for the next caller"
    assert not _real_helper._latched(), "a kill the gateway performed never latches to in-process"


def test_an_aborted_request_does_not_resubmit_its_wedged_path(_real_helper, monkeypatch) -> None:
    """After the deadline's abort() the request's EOF must NOT spawn a replacement
    helper for the same path: that would pin the just-freed pool worker on the same
    mount again, and two such aborts would exhaust the two-worker pool."""
    spawns: list[int] = []
    real_spawn = _real_helper._spawn

    def counting_spawn():
        p = real_spawn()
        if p is not None:
            spawns.append(p.pid)
        return p

    monkeypatch.setattr(_real_helper, "_spawn", counting_spawn)
    # Wedge the request the way a dead mount does: the helper never answers. Then
    # abort() it from "the deadline" on another thread.
    monkeypatch.setattr(security.pathres_client, "_HELPER_SOURCE", _WEDGED_HELPER_SOURCE)
    proc = _real_helper._spawn()
    assert proc is not None and proc.stdin is not None
    stuck = threading.Thread(target=lambda: _real_helper.resolve("/some/where"), daemon=True)
    stuck.start()
    time.sleep(0.2)
    before = len(spawns)
    _real_helper.abort()  # SIGKILL -> the stopped child dies -> worker reads EOF
    stuck.join(timeout=5)
    assert not stuck.is_alive(), "the worker blocked on the killed helper must unwind"
    assert len(spawns) == before, "no replacement helper was spawned for the aborted path"
    assert _real_helper._proc is None


def test_abort_kills_without_waiting_on_the_event_loop(_real_helper, monkeypatch) -> None:
    """abort() runs on the event-loop thread at the resolve deadline; a wait there is
    the loop stall this module exists to prevent. It kills and detaches; the worker
    that reads EOF reaps off-loop."""
    proc = _real_helper._spawn()
    assert proc is not None

    def forbidden_wait(*args, **kwargs):
        raise AssertionError("abort() must not wait on the child")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(proc, "wait", forbidden_wait)
        started = time.monotonic()
        _real_helper.abort()
    assert time.monotonic() - started < 0.5
    assert _real_helper._proc is None, "the handle is dropped so the next request respawns"
    proc.wait(timeout=5)  # the child was in fact killed
    assert proc.returncode is not None


def test_a_transport_fault_on_a_candidate_fails_closed_not_lexical(
    _real_helper, monkeypatch, tmp_path
) -> None:
    """A ``None`` from the helper is a resolution that did not complete. Returning
    the empty set instead would read as "resolved, no other spelling": the gate
    would compare the lexical form only, and a workspace symlink into a credential
    store would pass. So it is refused like a timeout -- and charges no prefix,
    because the disk did not stall."""
    monkeypatch.setattr(_real_helper, "resolve", lambda path: None)
    monkeypatch.setattr(_real_helper, "resolve_many", lambda paths: None)
    security._home_targets_cache.clear()
    with pytest.raises(security.PathResolutionStalled):
        security._resolved_forms_bounded(str(tmp_path / "ws" / "link"))
    refusal = security.sensitive_path_refusal(str(tmp_path / "ws" / "link"))
    assert refusal is not None and security.UNVERIFIABLE_PATH_ANCHOR in refusal
    assert security.is_sensitive_path(str(tmp_path / "ws" / "link")) is True
    assert security._path_resolve_degraded == {}, "a transport fault is not a stalled mount"


def test_a_transport_fault_on_an_anchor_refuses_the_sandbox_mask(_real_helper, monkeypatch) -> None:
    """The OS deny mask (``sandbox_credential_targets``) is built from the anchors
    and does no per-candidate canonicalisation, so an anchor degraded to its
    lexical spelling would leave a symlinked credential home (``/home/u`` ->
    ``/local/home/u``) reachable through the unmasked canonical path for the
    sandbox's lifetime. A transport fault on an anchor therefore raises, and the
    spawn path refuses to start the adapter without its mask."""
    monkeypatch.setattr(_real_helper, "resolve", lambda path: None)
    monkeypatch.setattr(_real_helper, "resolve_many", lambda paths: None)
    security._home_targets_cache.clear()
    with pytest.raises(security.PathResolutionStalled):
        security._realpath_or_none(str(security.Path.home()))
    with pytest.raises(security.PathResolutionStalled):
        security.sandbox_credential_targets()
    assert security._path_resolve_degraded == {}, "a transport fault is not a stalled mount"


def test_a_queued_request_never_samples_another_requests_wedged_helper(
    _real_helper, monkeypatch
) -> None:
    """Requests serialise through one helper. A worker whose budget expires while it
    is QUEUED behind a wedged request must not read the helper's syscall as its
    own evidence: that helper is blocked on the OTHER request's mount, and charging
    this worker's (healthy) prefix with it would refuse every path under it."""
    if not os.path.exists("/proc/self/syscall"):
        pytest.skip("/proc/<pid>/syscall is Linux-only")
    proc = _real_helper._spawn()
    assert proc is not None
    # Stand in for the in-flight request: some other worker thread owns it and the
    # helper is (for the sample's purposes) blocked in a filesystem syscall.
    _real_helper._inflight_pid = proc.pid
    _real_helper._inflight_tid = threading.get_native_id() + 1_000_000  # not us
    try:
        # Our worker (this thread) asks about ITS request: no attribution -> None.
        assert (
            _real_helper.blocked_in_filesystem(
                security.paths._FS_BLOCKING_SYSCALLS, threading.get_native_id()
            )
            is None
        )
        # The owning worker gets a definite answer for the same helper.
        owner = _real_helper._inflight_tid
        assert (
            _real_helper.blocked_in_filesystem(security.paths._FS_BLOCKING_SYSCALLS, owner)
            is not None
        )
    finally:
        _real_helper._inflight_pid = None
        _real_helper._inflight_tid = None


def test_a_helper_that_cannot_start_falls_back_in_process_once(monkeypatch, caplog) -> None:
    fresh = security.pathres_client.ResolverHelper()
    monkeypatch.setattr(security.pathres_client, "_helper", fresh)
    monkeypatch.setattr(security.pathres_client, "_INTERPRETER", "/nonexistent/python")
    with caplog.at_level("WARNING", logger="kiro_crew.security.pathres_client"):
        first = fresh.resolve("/")
        second = fresh.resolve("/")
    assert first == second == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert fresh._latched()
    assert sum("could not start" in r.message for r in caplog.records) == 1, "warn once"


def test_an_in_process_latch_re_probes_after_the_cool_off(monkeypatch) -> None:
    """A spawn failure can be transient (fork ``EAGAIN``/``ENOMEM``, an OOM-killed
    child) and is likeliest exactly when the gateway is loaded -- the condition the
    helper exists for. So a latch is bounded: after the cool-off the next request
    spawns again, and a helper that then answers clears it."""
    fresh = security.pathres_client.ResolverHelper()
    monkeypatch.setattr(security.pathres_client, "_helper", fresh)
    monkeypatch.setattr(security.pathres_client, "_LATCH_COOLOFF_SECS", 0.05)
    real_executable = security.pathres_client._INTERPRETER
    monkeypatch.setattr(security.pathres_client, "_INTERPRETER", "/nonexistent/python")
    try:
        assert fresh.resolve("/") is not None
        assert fresh._latched(), "latched"
        assert fresh._proc is None, "no spawn is attempted inside the cool-off"
        monkeypatch.setattr(security.pathres_client, "_INTERPRETER", real_executable)
        time.sleep(0.1)
        assert fresh.resolve("/") == (os.path.realpath("/"), str(security.Path("/").resolve()))
        assert not fresh._latched(), "the re-probe succeeded and the latch is gone"
        assert fresh._proc is not None and fresh._proc.poll() is None
    finally:
        fresh.close()


def test_a_helper_that_dies_before_its_first_answer_latches_to_in_process(
    monkeypatch, caplog
) -> None:
    """The crash-loop shape: an interpreter that starts but cannot come up in the
    child environment (Windows without ``SystemRoot``) exits before answering. That
    is a fact about the host, not a wedge, so it latches to in-process exactly as a
    failed spawn does -- otherwise every path check on that host is refused."""
    fresh = security.pathres_client.ResolverHelper()
    monkeypatch.setattr(security.pathres_client, "_helper", fresh)
    monkeypatch.setattr(security.pathres_client, "_HELPER_SOURCE", "import sys; sys.exit(3)")
    with caplog.at_level("WARNING", logger="kiro_crew.security.pathres_client"):
        first = fresh.resolve("/")
        second = fresh.resolve("/")
    assert first == second == (os.path.realpath("/"), str(security.Path("/").resolve()))
    assert fresh._latched()
    assert sum("before answering its first request" in r.message for r in caplog.records) == 1
    fresh.close()


def test_an_abort_never_latches_to_in_process(_real_helper, monkeypatch) -> None:
    """A kill the gateway performed itself is a wedge on a mount, not a helper that
    cannot run: the request reports a fault, and the next request respawns."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(security.pathres_client, "_HELPER_SOURCE", _WEDGED_HELPER_SOURCE)
        proc = _real_helper._spawn()
    assert proc is not None
    results: list[object] = []
    stuck = threading.Thread(target=lambda: results.append(_real_helper.resolve("/x")), daemon=True)
    stuck.start()
    time.sleep(0.2)
    _real_helper.abort()
    stuck.join(timeout=5)
    assert results == [None], "the aborted request is a fault, refused upstream"
    assert not _real_helper._latched(), "an abort is not evidence the helper cannot run"
    # The override is out of scope: the respawn runs the healthy helper program.
    assert _real_helper.resolve("/")


def test_a_worker_still_spawning_or_queued_is_the_load_arm_even_without_proc(
    _real_helper, monkeypatch
) -> None:
    """The child's spawn happens inside the resolve budget, under the request lock. A
    budget that expires while the worker is still starting the child (a cold spawn on a
    loaded host) or still queued behind another request never reached a filesystem
    wait, so it must not charge the path's prefix with a mount stall -- and that must
    hold on a host with no ``/proc`` syscall table, where the thread sample defaults to
    "blocked" for every expiry."""
    monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset())
    hold = threading.Event()
    spawning_tid: list[int] = []
    real_spawn = _real_helper._spawn

    def slow_spawn():
        spawning_tid.append(threading.get_native_id())
        hold.wait(5.0)
        return real_spawn()

    monkeypatch.setattr(_real_helper, "_spawn", slow_spawn)
    first = threading.Thread(target=lambda: _real_helper.resolve("/"), daemon=True)
    first.start()
    for _ in range(200):
        if spawning_tid:
            break
        time.sleep(0.01)
    assert spawning_tid, "the first worker never reached the spawn"
    queued_tid: list[int] = []

    def queued():
        queued_tid.append(threading.get_native_id())
        _real_helper.resolve("/")

    second = threading.Thread(target=queued, daemon=True)
    second.start()
    for _ in range(200):
        if _real_helper.worker_phase(queued_tid[0] if queued_tid else None) == "queued":
            break
        time.sleep(0.01)
    try:
        assert _real_helper.worker_phase(spawning_tid[0]) == "spawning"
        assert _real_helper.worker_phase(queued_tid[0]) == "queued"
        # The classifier the deadline owner calls: load arm for both, no /proc needed.
        assert _REAL_BLOCKED_IN_FILESYSTEM(spawning_tid[0]) is False
        assert _REAL_BLOCKED_IN_FILESYSTEM(queued_tid[0]) is False
        # A tid with no request here keeps the fail-closed default of a no-/proc host.
        assert _REAL_BLOCKED_IN_FILESYSTEM(threading.get_native_id()) is True
    finally:
        hold.set()
        first.join(timeout=5)
        second.join(timeout=5)
    assert _real_helper.worker_phase(spawning_tid[0]) is None, "phases are cleared on exit"


def test_abort_if_inflight_kills_only_for_the_owning_worker(_real_helper) -> None:
    """A deadline owner that knows its worker's tid must not fault a healthy helper
    that is busy with ANOTHER request (its own deadline handles that one)."""
    proc = _real_helper._spawn()
    assert proc is not None
    _real_helper._inflight_pid = proc.pid
    _real_helper._inflight_tid = 424242
    try:
        assert _real_helper.abort_if_inflight(None) is False
        assert _real_helper.abort_if_inflight(424243) is False
        assert _real_helper._proc is proc, "a non-owner's deadline leaves the helper alone"
        assert _real_helper.abort_if_inflight(424242) is True
        assert _real_helper._proc is None, "the owner's deadline kills it"
    finally:
        _real_helper._inflight_pid = None
        _real_helper._inflight_tid = None
        proc.wait(timeout=5) == (os.path.realpath("/"), str(security.Path("/").resolve()))


def test_the_helper_runs_boot_captured_source_not_the_file_on_disk(
    _real_helper, monkeypatch
) -> None:
    """The helper respawns on demand, so if it ran BY PATH an agent able to edit
    ``pathres_helper.py`` (an editable install) would get its code running with
    gateway privileges outside the sandbox on the next path check, with no
    restart. The child runs ``-c <source captured at import>``: an edit takes
    effect at the next gateway restart, like every other product file."""
    seen: dict[str, object] = {}
    real_popen = security.pathres_client.subprocess.Popen

    def recording_popen(*args, **kwargs):
        seen["argv"] = args[0]
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(security.pathres_client.subprocess, "Popen", recording_popen)
    _real_helper.close()
    assert _real_helper.resolve("/") is not None
    argv = seen["argv"]
    assert argv[0] == security.pathres_client._INTERPRETER == security.pathres_client.sys.executable
    assert argv[1:4] == [
        "-I",
        "-S",
        "-c",
    ], "isolated, no site: no .pth or sitecustomize runs at startup"
    assert argv[4] == security.pathres_client._HELPER_SOURCE
    assert not any(str(a).endswith("pathres_helper.py") for a in argv), "never run by path"
    helper_file = security.Path(security.pathres_client.__file__).with_name("pathres_helper.py")
    assert helper_file.read_text(encoding="utf-8") == security.pathres_client._HELPER_SOURCE


def test_the_helper_receives_a_minimal_environment(_real_helper, monkeypatch) -> None:
    """The helper reads no variables and must not inherit the gateway's credentials."""
    seen: dict[str, object] = {}
    real_popen = security.pathres_client.subprocess.Popen

    def recording_popen(*args, **kwargs):
        seen["env"] = kwargs.get("env")
        seen["argv"] = args[0]
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(security.pathres_client.subprocess, "Popen", recording_popen)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    _real_helper.close()
    assert _real_helper.resolve("/") is not None
    assert set(seen["env"]) <= set(security.pathres_client._CHILD_ENV_KEYS)
    assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]
    assert (
        "-I" in seen["argv"]
    ), "isolated mode: no user site, no PYTHON* env, no script dir on sys.path"


def test_a_starved_worker_is_logged_as_contention_not_as_a_mount(monkeypatch, caplog) -> None:
    """The load arm's log line must name the interpreter, not the disk: operators
    reading 'stalled mount?' on a healthy local disk spent the diagnosis on the
    wrong layer."""
    monkeypatch.setattr(security.paths, "_worker_blocked_in_filesystem", lambda tid: False)
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with caplog.at_level("WARNING", logger="kiro_crew.security.paths"):
            with pytest.raises(security.PathResolutionStalled):
                security._resolved_forms_bounded("/home/a/b")
    finally:
        stalled.release.set()
    messages = [r.message for r in caplog.records]
    assert any("interpreter was busy" in m for m in messages), messages
    assert not any("mount" in m for m in messages), messages
    assert security._path_resolve_degraded == {}, "the load arm charges no prefix"


def test_a_real_stall_is_logged_as_a_mount_that_is_not_answering(monkeypatch, caplog) -> None:
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with caplog.at_level("WARNING", logger="kiro_crew.security.paths"):
            with pytest.raises(security.PathResolutionStalled):
                security._resolved_forms_bounded("/home/a/b")
    finally:
        stalled.release.set()
    messages = [r.message for r in caplog.records]
    assert any(
        "blocked in a filesystem syscall" in m and "not answering" in m for m in messages
    ), messages
    assert not any("stalled mount?" in m for m in messages), "the guessing question mark is gone"


# ── The refusal: a stall is reported as unverifiable, never as a match ────────


def test_a_stall_reads_as_unverifiable_and_a_healthy_path_as_clear(monkeypatch, tmp_path) -> None:
    ws = tmp_path / "ws" / "notes.md"
    ws.parent.mkdir()
    ws.write_text("x")
    security._home_targets_cache.clear()
    assert security.sensitive_path_refusal(str(ws)) is None
    assert security.sensitive_path_refusal(str(security.Path.home() / ".aws" / "credentials")) == (
        f"Blocked: access to sensitive path: {security.Path.home() / '.aws' / 'credentials'}"
    )
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        refusal = security.sensitive_path_refusal(str(ws))
        assert refusal is not None
        assert security.UNVERIFIABLE_PATH_ANCHOR in refusal
        assert "access to sensitive path" not in refusal
        # Same DECISION as the boolean gate -- unverifiable is refused -- different words.
        assert security.is_sensitive_path(str(ws)) is True
    finally:
        stalled.release.set()
