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

This module pins the candidate and anchor resolution: the budget, the fail-closed refusal,
the cooldown and its prefix, the grace, the resolver pool's knob and the cumulative wait.
The resolver child -- the kernel state that decides whether a stall charges its prefix,
and the wiring that reaches the child -- is pinned in
``test_security_path_resolve_bounded_child.py``; the shared fixture and stall doubles live
in ``security_path_resolve_bounded_helpers.py``.
"""

from __future__ import annotations

import logging
import ntpath
import os
import posixpath
import subprocess
import sys
import threading
import time
import types

import pytest
from security_path_resolve_bounded_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _REAL_CANDIDATE_BUDGET,
    _REAL_GRACE_MAX,
    _REAL_REBUILD_BUDGET,
    _REAL_WAIT_CAP,
    _fresh_resolver_state,
    _stall_like_a_child,
    _StalledResolver,
)

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.agent_sdk import host_auth
from kiro_crew.subprocess_pool import OP_REALPATH_MANY, SubprocessPoolTimeout
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import wait_until


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


def test_a_stall_is_refused_as_unverifiable_not_as_a_match(monkeypatch) -> None:
    # Same decision as the boolean gate above (refused), different WORDS: the
    # refusal says the path could not be verified and is NOT a match, so an agent
    # reading it retries instead of hunting for a credential in a project file.
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        token = "/home/someone/ws/README.md"
        reason = security.sensitive_path_refusal(token)
    finally:
        stalled.release.set()
    assert reason is not None
    assert security.is_unverifiable_path_refusal(reason)
    assert "NOT a match" in reason
    assert repr(token) in reason
    assert "access to sensitive path" not in reason
    # The boolean gate is the producer's ``is not None``: it cannot say otherwise.
    assert security.is_sensitive_path(token) is True


def test_a_match_and_a_benign_path_keep_their_answers(tmp_path) -> None:
    benign = tmp_path / "notes.md"
    benign.write_text("x")
    assert security.sensitive_path_refusal(str(benign)) is None
    assert security.is_sensitive_path(str(benign)) is False
    assert security.sensitive_path_refusal("~/.aws/credentials") == (
        "Blocked: access to sensitive path: ~/.aws/credentials"
    )
    assert security.is_sensitive_path("~/.aws/credentials") is True
    assert security.sensitive_path_refusal("") is None


def test_a_stalled_publish_artifact_check_is_also_worded_as_unverifiable(monkeypatch) -> None:
    # The producer's second matcher raises through it too: a stall while judging
    # the keystone-temp rule must not fall back to the match wording.
    def stalled(*args, **kwargs):
        raise security.PathResolutionStalled("/home/someone/ws/x.tmp", "/home/someone")

    monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: False)
    monkeypatch.setattr(security.paths, "_is_keystone_publish_artifact", stalled)
    reason = security.sensitive_path_refusal("/home/someone/ws/x.tmp")
    assert reason is not None and security.is_unverifiable_path_refusal(reason)


def test_a_matched_path_spelled_like_the_stall_wording_is_still_a_match(monkeypatch) -> None:
    # Both refusals embed the caller-chosen path, so the stall is recognised by a
    # fixed PREFIX the path cannot reach, never by searching the text. A path
    # carrying the whole stall opening keeps the match wording and is not a stall.
    forged = f"/home/someone/{security.UNVERIFIABLE_PATH_PREFIX}/x"
    monkeypatch.setattr(security.paths, "_path_in_home_dirs", lambda *a, **k: True)
    reason = security.sensitive_path_refusal(forged)
    assert reason == f"Blocked: access to sensitive path: {forged}"
    assert security.is_unverifiable_path_refusal(reason) is False
    # And the genuine stall wording quotes the path LAST, behind the fixed prefix.
    assert security.is_unverifiable_path_refusal(
        f"{security.UNVERIFIABLE_PATH_PREFIX} (...). Path: {forged!r}"
    )


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
    real_resolve = security.paths._resolve_on_calling_thread
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/a/one")  # times out -> opens cooldown
        assert len(stalled.calls) == 1
        # "For free" is a claim about the RESOLVER, not about the wall clock.  This
        # was a 50ms stopwatch around each refusal, which is inside the noise
        # band these runners actually produce -- one scheduler stall or GC pause
        # inside the window reds the test with a regression that never
        # happened -- and it is also blind in the other direction, since a
        # regression that resolves and comes straight back stays under the
        # ceiling.  Count resolutions instead: the cooldown short-circuit must
        # be reached BEFORE ``_resolve_on_calling_thread``.
        submissions: list[str] = []

        def _counting(worker, expanded, timeout):  # noqa: ANN001, ANN202
            submissions.append(getattr(worker, "__name__", repr(worker)))
            return real_resolve(worker, expanded, timeout)

        monkeypatch.setattr(security.paths, "_resolve_on_calling_thread", _counting)
        for token in ("/home/a/two", "/home/a/deeper/three"):
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms(token)
        assert submissions == [], "cooldown must not reach the resolver"
        assert len(stalled.calls) == 1, "no resolution may be attempted under the cooldown"
    finally:
        stalled.release.set()

    # A different prefix is untouched by the cooldown: resolution still runs,
    # and on a healthy filesystem a symlink there still resolves to its target.
    # That needs the live resolver back, not the counting stand-in.
    monkeypatch.setattr(security.paths, "_resolve_on_calling_thread", real_resolve)
    monkeypatch.setattr(security, "_resolved_spellings", real_resolver)
    target = tmp_path / "creds"
    target.write_text("k")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert str(target) in security._candidate_forms(str(link))

    # Past the cooldown the stalled prefix is tried again.
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
            deadline = security.paths._child_budget.deadline
            if deadline is not None and time.monotonic() + self.delay > deadline:
                _stall_like_a_child()  # the child is killed before it can answer
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
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: True)
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


def test_the_stall_prefix_never_ends_on_a_container_of_homes(monkeypatch) -> None:
    """A host whose homes sit one level deeper than ``/home`` reproduced the ``C:\\Users``
    collapse on POSIX: two components of ``/local/home/<user>/...`` is ``/local/home``,
    one key for every user, workspace, checkout, data home and credential store on
    the machine, so one stall anywhere under it refused the whole host for the
    cooldown (kirodotdev/KiroCrew#12386).  The key must not stop on a container of
    homes; it takes the user too, which is exactly the key ``/home/<user>`` gets.
    Driven through ``posixpath`` so the layout is asserted on every platform.
    """
    monkeypatch.setattr(security.paths.os, "path", posixpath)
    monkeypatch.setattr(security.paths.os, "sep", posixpath.sep)

    # The shared-mount shape, inverted: two users, two keys.
    alice = security._stall_prefix("/local/home/alice/.aws/credentials")
    bob = security._stall_prefix("/local/home/bob/project/file.py")
    assert alice == "/local/home/alice"
    assert bob == "/local/home/bob"
    assert alice != bob
    # Every layout that puts the homes one level deeper keeps the word.
    assert security._stall_prefix("/usr/home/x/f") == "/usr/home/x"
    assert security._stall_prefix("/var/home/x/f") == "/var/home/x"
    assert security._stall_prefix("/export/home/x/f") == "/export/home/x"
    # The container alone is still its own key: there is no user to take.
    assert security._stall_prefix("/local/home") == "/local/home"
    assert security._stall_prefix("/home") == "/home"
    # A home at the usual depth, a mount and a plain directory are untouched: the
    # rule only fires when the key WOULD have ended on the container.
    assert security._stall_prefix("/home/x/ws/f") == "/home/x"
    assert security._stall_prefix("/Volumes/share/x/y") == "/Volumes/share"
    assert security._stall_prefix("/tmp/x/y") == "/tmp/x"


class _StallsOneHome:
    """Wedged for one user's tree, healthy for every other path."""

    def __init__(self, wedged_under: str) -> None:
        self.wedged_under = wedged_under
        self.release = threading.Event()
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, expanded: str) -> set[str]:
        with self._lock:
            self.calls.append(expanded)
        if expanded.startswith(self.wedged_under):
            _stall_like_a_child()
        return {expanded}


def test_a_stall_under_one_home_leaves_a_sibling_home_resolving(monkeypatch) -> None:
    """kirodotdev/KiroCrew#12386, consequence 1: the blast radius is one home, not the host.

    Through the real cooldown machinery, not the key alone.  A key that stops on the
    container charges Alice's stall to ``/local/home`` and refuses Bob's path for
    free, without probing, because every home on the host shares that container.
    Bob's tree is its own key, so its resolution is submitted and answered while
    Alice's cooldown is open.
    """
    resolver = _StallsOneHome("/local/home/alice/")
    monkeypatch.setattr(security, "_resolved_spellings", resolver)
    try:
        with pytest.raises(security.PathResolutionStalled) as info:
            security._candidate_forms("/local/home/alice/.aws/credentials")
        # Alice's tree is refused for free for the rest of the cooldown ...
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/local/home/alice/project/file.py")
        assert len(resolver.calls) == 1, "the cooldown must not re-probe the stalled home"
        # ... while Bob's is untouched: the resolution runs and its answer is used.
        forms = security._candidate_forms("/local/home/bob/project/file.py")
    finally:
        resolver.release.set()
    assert "/local/home/bob/project/file.py" in forms
    assert resolver.calls[-1] == "/local/home/bob/project/file.py"
    assert os.path.normpath("/local/home/bob") not in security._path_resolve_degraded
    # The stall was charged to Alice's home, not to the container both homes share.
    assert info.value.prefix == os.path.normpath("/local/home/alice")


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
            # Step past the window: the next iteration is a genuine re-probe.
            clock[0] = until + 1
        # Recovery: a resolution that completes clears the history.
        monkeypatch.setattr(security, "_resolved_spellings", lambda e: {e})
        security._candidate_forms("/home/user/x")
        assert os.path.normpath("/home/user") not in security._path_resolve_degraded
    finally:
        for stub in stubs:
            stub.release.set()


def test_the_resolver_pool_ships_with_two_workers(monkeypatch) -> None:
    # The knob widens the pool for an operator who asks; it does not move the
    # shipped number.  Pinned as a literal because the PR that added the knob
    # promises the default is unchanged, and a later "just raise it" edit would
    # otherwise ride in silently.
    monkeypatch.delenv(ex._PATH_RESOLVE_WORKERS_ENV, raising=False)
    assert ex._PATH_RESOLVE_WORKERS_DEFAULT == 2
    assert ex._path_resolve_worker_count() == 2


@pytest.mark.parametrize("raw, expected", [("2", 2), ("3", 3), ("8", 8), (" 8 ", 8), ("64", 64)])
def test_an_in_range_resolver_pool_knob_is_honoured(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv(ex._PATH_RESOLVE_WORKERS_ENV, raw)
    assert ex._path_resolve_worker_count() == expected


def test_the_resolver_pool_knob_reaches_the_pool_in_a_fresh_interpreter() -> None:
    # The knob tests around this one call the parser directly, so an edit that
    # disconnected the parser from the module constant (``_MAX_PATH_RESOLVE_WORKERS
    # = 2`` again) would leave them all green while the knob did nothing.  Import in
    # a fresh interpreter, where the once-at-import read really happens, and follow
    # the value to the pool the gate submits to.
    probe = (
        "import kiro_crew.executors as ex\n"
        "print(ex._MAX_PATH_RESOLVE_WORKERS, len(ex.path_resolve_executor()._children))\n"
    )
    env = dict(os.environ)
    env[ex._PATH_RESOLVE_WORKERS_ENV] = "8"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, env=env, timeout=120, **UTF8_TEXT
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["8", "8"]


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("0", "outside"),
        # 1 is refused on purpose: a single child means one stalled resolution
        # refuses every path under every prefix until the deadline reclaims it;
        # the second child is what keeps a healthy prefix answering meanwhile.
        ("1", "outside"),
        ("-3", "outside"),
        ("65", "outside"),
        ("abc", "not an integer"),
        ("2.5", "not an integer"),
    ],
)
def test_a_bad_resolver_pool_knob_fails_soft_to_the_default_and_says_so(
    monkeypatch, caplog, raw, reason
) -> None:
    # Fail-soft TO THE DEFAULT is the conservative direction: the default is the
    # lower ceiling, so a typo can only ever leave the shipped behaviour in
    # place, never widen the pool.  The warning is what tells the operator the
    # knob they set was ignored -- a silent fallback would read as "8 workers
    # did not help" when the pool never left 2.
    monkeypatch.setenv(ex._PATH_RESOLVE_WORKERS_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=ex.__name__):
        assert ex._path_resolve_worker_count() == ex._PATH_RESOLVE_WORKERS_DEFAULT
    messages = [r.getMessage() for r in caplog.records if r.name == ex.__name__]
    assert len(messages) == 1
    assert reason in messages[0]
    assert ex._PATH_RESOLVE_WORKERS_ENV in messages[0]


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_resolver_pool_knob_is_the_default_without_a_warning(
    monkeypatch, caplog, raw
) -> None:
    # Empty is how a shell script clears a variable it may have set; it is not
    # a typo, so it gets the default silently.
    monkeypatch.setenv(ex._PATH_RESOLVE_WORKERS_ENV, raw)
    with caplog.at_level(logging.WARNING, logger=ex.__name__):
        assert ex._path_resolve_worker_count() == ex._PATH_RESOLVE_WORKERS_DEFAULT
    assert [r for r in caplog.records if r.name == ex.__name__] == []


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
    """A resolution whose child round trip consumes only injected clock time."""

    def __init__(self, monkeypatch):
        self.clock = [1000.0]
        self.delay = 0.25
        self.error = False
        self.queued = False
        self.submissions: list[str] = []
        self.waits: list[float] = []
        monkeypatch.setattr(security, "_path_resolve_clock", lambda: self.clock[0])
        monkeypatch.setattr(security.paths, "_resolve_on_calling_thread", self.resolve)
        # Creating these on an implementation without accounting lets the behavioural
        # assertions, rather than a missing attribute, demonstrate the missing bound.
        monkeypatch.setattr(security.paths, "_PATH_RESOLVE_WAIT_CAP_SECS", 1.0, raising=False)
        monkeypatch.setattr(security.paths, "_PATH_RESOLVE_WAIT_WINDOW_SECS", 25.0, raising=False)
        monkeypatch.setattr(security.paths, "_path_resolve_thread_waits", {}, raising=False)

    def resolve(self, worker, expanded, timeout):
        self.submissions.append(expanded)
        self.waits.append(timeout)
        self.clock[0] += min(timeout, self.delay)
        if self.delay > timeout:
            if self.queued:
                raise TimeoutError("no free child within the budget")
            raise SubprocessPoolTimeout("child did not answer", child_syscall=None)
        if self.error:
            raise ValueError("resolution failed")
        return worker(expanded)


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
    # Budget plus grace is ONE deadline (0.25 + 0.375), clamped to the 0.5 left.
    assert pool.waits == [1.0, 0.5]
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
        monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: False)
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
        _fut1 = background.submit(
            security._run_resolution_bounded, "/mount/background/file", lambda p: p, budget=1.0
        )
        wait_until(_fut1.done)
        assert _fut1.result()
        assert security._run_resolution_bounded("/mount/loop/file", lambda p: p, budget=1.0)
        with pytest.raises(security.PathResolutionStalled):
            _fut2 = background.submit(
                security._run_resolution_bounded, "/mount/background/next", lambda p: p, budget=1.0
            )
            wait_until(_fut2.done)
            _fut2.result()
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
    """The slow-but-completing scenario: waits that each finish just under budget.

    13 waits at ~2s apiece is ~25s of loop block -- far above any sane floor -- so the floor must
    not be implemented as "only count timeouts": every one of these clears 100ms by a wide margin
    and must still count, or this exact shape reopens the unbounded-loop-block case the PR closes.
    Uses the PRODUCTION allowance (12s), not the pool fixture's scaled-down test cap, so the
    exhaustion point matches the production arithmetic (six waits at 1.9s spend 11.4s of the
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
    # Budget plus grace as ONE deadline: 2.0 + min(2.0 * 1.5, 4.0) = 5.0 s. Each call
    # spends 1.9 s of the 12 s allowance, so the first four are granted in full and the
    # fifth and sixth are clamped to what remains (12 - 4 * 1.9 = 4.4 s, then 2.5 s),
    # both still enough for the 1.9 s resolution to complete.
    assert pool.waits[:6] == pytest.approx(
        [5.0, 5.0, 5.0, 5.0, 4.4, 2.5]
    ), "budget plus grace in full while the allowance lasts, then clamped to its remainder"
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
# fallbacks each leave the fence open -- lexical spellings, a UNC skip, and
# serving the previous canonical resolution -- and are pinned shut below.
# ---------------------------------------------------------------------------


class _StalledRealpath:
    """Stands in for the resolver child on a slow-to-stat home: never answers the
    anchor batch within the deadline, and records what it was asked to resolve."""

    def __init__(self) -> None:
        self.release = threading.Event()  # kept so callers' ``release.set()`` stay valid
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, paths: list[str]) -> list[str | None]:
        with self._lock:
            self.calls.extend(paths)
        _stall_like_a_child()
        return list(paths)


class _AnsweringRealpath(_StalledRealpath):
    """The same recorder, once the mount answers again."""

    def __call__(self, paths: list[str]) -> list[str | None]:
        with self._lock:
            self.calls.extend(paths)
        return list(paths)


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


def _count_child_requests(monkeypatch) -> list[int]:
    """Record the op of every request the resolver sends to a child."""
    requests: list[int] = []
    real_executor = security.paths.path_resolve_executor

    class _Counting:
        def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
            requests.append(op)
            return real_executor().call_op(op, payload, timeout)

    monkeypatch.setattr(security.paths, "path_resolve_executor", lambda: _Counting())
    return requests


def test_a_stalled_root_anchor_refuses_within_the_budget(monkeypatch, tmp_path) -> None:
    _clear_override_roots(monkeypatch)
    security._home_targets_cache.clear()
    security._resolved_root_key()  # a warm, canonical resolution must NOT be served later
    stalled = _StalledRealpath()
    monkeypatch.setattr(security.paths, "_realpaths_or_none", stalled)
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
    monkeypatch.setattr(security.paths, "_realpaths_or_none", stalled)
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
        # Past the cooldown the anchor is probed again, and the disk answers.
        clock[0] += security._PATH_RESOLVE_COOLDOWN_SECS + 1.0
        answering = _StalledRealpath()
        answering.__class__ = _AnsweringRealpath
        monkeypatch.setattr(security.paths, "_realpaths_or_none", answering)
        roots = security._resolved_root_key()
        assert answering.calls == [logical_home]
        assert roots.logical_home == logical_home
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()


def test_a_stalled_prefix_is_not_reprobed_on_the_pools_last_child(monkeypatch) -> None:
    """The re-probe after a cooldown is expected to wedge again, and a wedged child is
    replaced only while the pool's bounded list of killed-but-unexited children has
    room.  Spending the LAST live child on a known-bad prefix would therefore leave
    every other prefix refused, uncharged, for as long as that mount stays wedged.
    With one child left the re-probe is refused without reaching
    the resolver and the prefix's own cooldown is extended; with two, it proceeds."""
    clock = [1_000.0]
    monkeypatch.setattr(security, "_path_resolve_clock", lambda: clock[0])
    live = [2]
    monkeypatch.setattr(
        security.paths,
        "path_resolve_executor",
        lambda: types.SimpleNamespace(serviceable_children=lambda: live[0]),
    )
    prefix = security.paths._stall_prefix("/home/a/one")
    submissions: list[str] = []

    def _reached(worker, expanded, timeout):  # noqa: ANN001, ANN202
        submissions.append(expanded)  # the resolver WAS reached; refuse so nothing charges
        raise security.PathResolutionStalled(expanded, security.paths._stall_prefix(expanded))

    monkeypatch.setattr(security.paths, "_resolve_on_calling_thread", _reached)
    security._path_resolve_degraded.clear()
    try:
        # A prefix with stall history whose cooldown has lapsed.
        security._path_resolve_degraded[prefix] = (clock[0] - 1.0, 1)
        live[0] = 1
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/a/one")
        assert submissions == [], "with one live child the re-probe never reaches the resolver"
        until, stalls = security._path_resolve_degraded[prefix]
        assert until > clock[0], "the prefix's cooldown was extended"
        assert stalls == 1, "nothing was observed, so no new stall is recorded"
        # A prefix with NO history is unaffected by the pool being down to one child.
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/b/other")
        assert submissions == ["/home/b/other"]
        # With two live children the re-probe proceeds once the cooldown lapses.
        clock[0] = until + 1.0
        live[0] = 2
        with pytest.raises(security.PathResolutionStalled):
            security._candidate_forms("/home/a/one")
        assert submissions[-1] == "/home/a/one"
    finally:
        security._path_resolve_degraded.clear()


def test_root_anchors_resolve_in_one_pool_hop(monkeypatch, tmp_path) -> None:
    # ``_resolved_root_key`` runs on the event loop once per is_sensitive_path
    # call; one thread hop per root would cost more than the inline realpath it
    # replaces.  Every root -- the host's own and each declared harness home --
    # travels in one submission.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "kiro"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    requests = _count_child_requests(monkeypatch)
    security._home_targets_cache.clear()
    try:
        roots = security._resolved_root_key()
    finally:
        security._home_targets_cache.clear()
    assert requests == [OP_REALPATH_MANY], "every root travels in one child request"
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
    # The EFFECTIVE expiry, read through the adaptive law rather than off the
    # floor constant: under a frozen clock the warm build above measures as
    # costing nothing, so the law returns its floor.
    clock[0] += security._home_targets_ttl(0.0) + 0.01  # the slot expires
    logical_home = str(security.Path.home())
    stalled = _StalledRealpath()
    monkeypatch.setattr(security.paths, "_realpaths_or_none", stalled)
    try:
        with pytest.raises(security.PathResolutionStalled):
            security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
        # is_sensitive_path refuses rather than comparing against the expired set.
        assert security.is_sensitive_path(str(tmp_path / "ws" / "README.md")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    # One paid probe -- the root key's single batch -- then everything under the
    # home's prefix (the rebuild included) is refused without touching the
    # filesystem for the cooldown: the ~40 leaves cost nothing, and the expired
    # slot is never handed back.
    assert stalled.calls == [logical_home, str(crew_home)]


def test_the_rebuild_is_one_pool_job(monkeypatch, tmp_path) -> None:
    # A single bash command can drive ~200 rebuilds; 40 hops each is what turns
    # a 9s gate into a 15s one.  Roots and rebuild are one submission apiece.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    requests = _count_child_requests(monkeypatch)
    security._home_targets_cache.clear()
    try:
        targets = security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
    finally:
        security._home_targets_cache.clear()
    assert requests == [OP_REALPATH_MANY, OP_REALPATH_MANY], requests  # roots, then leaves
    assert str(tmp_path / "crew" / "token_signing.key").casefold() in targets


def test_a_repointed_override_root_is_never_served_stale_through_a_stall(
    monkeypatch, tmp_path
) -> None:
    # The repointed-home scenario: KIROCREW_HOME is a symlink, the anchors
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
    clock[0] += security._home_targets_ttl(0.0) + 0.01
    link.unlink()
    link.symlink_to(real_b, target_is_directory=True)  # repointed...
    real_resolver = security.paths._realpaths_or_none
    stalled = _StalledRealpath()
    monkeypatch.setattr(security.paths, "_realpaths_or_none", stalled)  # ...under a stall
    try:
        # Only the anchors stall; the candidate resolves through the real
        # resolver on its own healthy prefix.
        assert security.is_sensitive_path(str(real_b / "security_policy.json")) is True
        assert security.is_sensitive_path(str(link / "security_policy.json")) is True
    finally:
        stalled.release.set()
        security._home_targets_cache.clear()
    # And once the disk answers again, B is anchored canonically.
    monkeypatch.setattr(security.paths, "_realpaths_or_none", real_resolver)
    security._home_targets_cache.clear()
    security._path_resolve_degraded.clear()
    assert str(security.Path(real_b / "security_policy.json").resolve()).casefold() in (
        security._home_dir_targets(security._SENSITIVE_HOME_DIRS)
    )


def test_a_unc_home_still_has_its_anchors_resolved(monkeypatch) -> None:
    # The UNC-home scenario: a UNC home (a roaming profile on
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

    def canonicalising(paths: list[str]) -> list[str | None]:
        calls.extend(paths)
        return [path + "\\canonical" for path in paths]  # the junction's target

    monkeypatch.setattr(security.paths, "_realpaths_or_none", canonicalising)
    security._home_targets_cache.clear()
    try:
        roots = security._resolved_root_key()
    finally:
        security._home_targets_cache.clear()
    assert roots.logical_home == unc_home
    assert calls == [unc_home], "the UNC home was probed, in the child"
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
    # ladder does not know PathResolutionStalled.  It
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
