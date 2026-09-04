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

import os
import threading
import time
from collections.abc import Iterator

import pytest

import kiro_crew.executors as ex
from kiro_crew import security


@pytest.fixture(autouse=True)
def _fresh_resolver_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_path_resolve_degraded", {})
    monkeypatch.setattr(security, "_path_resolve_wedged", [])
    # Short budgets keep the stall tests fast; the production values are pinned
    # separately below.
    monkeypatch.setattr(security, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
    monkeypatch.setattr(security, "_PATH_RESOLVE_COOLDOWN_SECS", 30.0)
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
    for _field, env in security._OVERRIDE_ROOT_ENVS:
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
    # call; six thread hops there would cost more than the inline realpath it
    # replaces.  All six roots travel in one submission.
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
    assert roots.codex_home == str(security.Path(tmp_path / "codex").resolve())


def test_a_stalled_rebuild_refuses_even_with_a_warm_cache(monkeypatch, tmp_path) -> None:
    # The rebuild (home + every keystone leaf under KIROCREW_HOME) used to
    # realpath() inline on the event loop every time the 0.1s cache expired.
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
