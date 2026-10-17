"""Real-process tests for the provider teardown's whole process tree.

``_sync_kill_provider`` against live provider roots, their stubborn
grandchildren and their zombies, and ``_reap_provider_root``'s refusal to
wait on an identity it cannot read.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pid_lifecycle_helpers import _POSIX_ONLY

from kiro_crew import platform_compat
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import wait_until

# A grandchild that ignores SIGTERM, so only a SIGKILL that actually reaches it
# can end it. ``{setsid}`` is filled with ``os.setsid()`` for the variant that
# escapes the provider's process group, and with nothing for the variant that
# stays in it.
_STUBBORN_GRANDCHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "{setsid}"
    # Announced only once SIG_IGN is installed. The pid exists from the fork, so a
    # test that waits for the pid alone can send the group SIGTERM into the window
    # where this process still has the DEFAULT disposition -- and a grandchild that
    # dies to SIGTERM is not the stubborn descendant these tests are built on.
    "sys.stdout.write('stubborn\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


# A provider root: forks one grandchild, then waits. Plain SIGTERM handling, so
# the root itself dies on the first signal and the test measures what happens to
# the tree BELOW it. Its own stdout carries the grandchild's pid, and it reports
# that pid only after the grandchild says it is stubborn, so the caller's read of
# this line is the barrier. The grandchild gets a pipe of its OWN rather than
# inheriting this one, which would put its announcement in front of the pid the
# caller reads. Reporting nothing when the announcement does not arrive keeps the
# failure loud: the caller raises on the empty read rather than proceeding with an
# unguarded tree.
_PROVIDER_ROOT = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', {src!r}], stdout=subprocess.PIPE)\n"
    "if not p.stdout.readline():\n"
    "    raise SystemExit(1)\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(300)\n"
)


@_POSIX_ONLY
class TestSyncKillProviderTree:
    """The sync kill must reap the provider's whole tree, not just its root.

    An agent runtime is a tree (sandbox launcher, agent binary, chat subprocess,
    MCP stub children) whose root is an isolated process-group leader. A
    pid-scoped signal reaps that root alone and the rest reparents to init,
    holding hundreds of megabytes with no tracking entry left to find it by.
    """

    @staticmethod
    def _provider(pid: int, child_pids: dict | None = None) -> MagicMock:
        """A provider stand-in whose tracked pid is *pid* (the ACP shape).

        ``_start_time`` carries the pid's REAL start id, the way the ACP layer
        records it at spawn, because the teardown refuses to signal a root whose
        recorded identity does not match the live process.
        """
        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = MagicMock(spec=["_pid", "_child_pids", "_start_time"])
        provider._client._pid = pid
        provider._client._child_pids = child_pids if child_pids is not None else {}
        provider._client._start_time = platform_compat.get_process_start_id(pid)
        provider._proc = None
        provider._active_proc = None
        return provider

    #: pid -> start-time identity, captured while the pid was provably ours.
    #: ``_reap`` signals a pid only while its identity still matches this record.
    _pinned: dict[int, str]

    @pytest.fixture(autouse=True)
    def _identity_pins(self) -> None:
        self._pinned = {}

    def _pin(self, *pids: int) -> None:
        """Record each pid's identity NOW, at the moment it is known to be ours.

        Every pid these tests hand to ``_reap`` is proven dead by the body first, and
        a grandchild's number is held by nobody once init has collected it -- the
        root's, too, once production's ``_reap_provider_root`` has waited on it. A
        bare ``os.kill`` in the ``finally`` would therefore go out on every PASSING
        run at whatever holds that number by then. The identity is read through
        ``process_start_time`` rather than ``get_process_start_id``: several tests
        below patch the latter (and ``kill_pid``) on ``platform_compat`` to script
        production's view of the root, and the teardown must read the real table.

        An identity that cannot be read (the macOS ``ps`` leg times out or is
        missing) is not stored as a pin ``_reap`` would then skip: that would leave
        the 300-second sleeper behind. This is the one moment every pid here is
        ours by construction -- just spawned, or just reported by a root that is
        still ours -- so the whole batch is killed on the spot and the test fails
        on the capture, before any of it can be mistaken for a stranger later.
        """
        tokens = {pid: platform_compat.process_start_time(pid) for pid in pids}
        unreadable = [pid for pid, token in tokens.items() if token is None]
        if unreadable:
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
            raise AssertionError(
                f"could not read the start-time identity of {unreadable}, so the "
                f"teardown could not have pinned the kill; killed {list(pids)} now"
            )
        for pid, token in tokens.items():
            self._pinned.setdefault(pid, token)

    def _spawn_isolated(self) -> subprocess.Popen:
        """A 300-second sleeper in its own session, pinned for ``_reap``.

        The stand-in for a provider root, a stray or a bystander: a direct child
        of this process whose status production may still collect before the
        teardown runs, so its number is not guaranteed held either.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        self._pin(proc.pid)
        return proc

    def _spawn_tree(self, *, escape_group: bool) -> tuple[subprocess.Popen, int]:
        """Spawn an isolated group leader that forks one stubborn grandchild.

        Returns the root AND its grandchild's pid, which the root reports on
        stdout. Cleanup then never depends on discovery: a walk that times out
        would otherwise leave the grandchild's 300-second sleep running, since the
        caller would have no pid to reap.

        Returning is also the barrier for the grandchild being STUBBORN, not merely
        alive: the root reports its pid only after the grandchild announces that it
        installed ``SIG_IGN``. Every test here signals the group and then reads what
        survived, so a grandchild still holding the default SIGTERM disposition
        breaks the premise rather than the assertion -- the tree really is gone, and
        the teardown is right to stop early.

        Both pids are pinned for ``_reap`` here, while the tree is provably ours.
        """
        grandchild = _STUBBORN_GRANDCHILD.format(setsid="os.setsid()\n" if escape_group else "")
        proc = subprocess.Popen(
            [sys.executable, "-c", _PROVIDER_ROOT.format(src=grandchild)],
            start_new_session=True,  # what the real provider spawn does
            stdout=subprocess.PIPE,
        )
        assert proc.stdout is not None
        reported = proc.stdout.readline().strip()
        if not reported:
            proc.kill()
            proc.wait(timeout=10)
            raise AssertionError("provider root never reported its grandchild pid")
        gc_pid = int(reported)
        self._pin(proc.pid, gc_pid)
        return proc, gc_pid

    def _await_descendants(self, pid: int, timeout: float = 10.0) -> list[int]:
        """Wait for the root's fork to appear; return the (pinned) descendant pids."""
        from kiro_crew.acp.client import _get_child_pids

        found = wait_until(
            lambda: _get_child_pids(pid),
            timeout=timeout,
            describe=lambda: f"no descendants of {pid} yet",
        )
        self._pin(*found)
        return found

    @staticmethod
    def _await_gone(pids: list[int], timeout: float = 10.0) -> list[int]:
        """Return the pids still alive after waiting up to *timeout*."""
        alive = list(pids)

        def _all_gone() -> bool:
            nonlocal alive
            alive = [p for p in pids if platform_compat.pid_exists(p)]
            return not alive

        try:
            wait_until(_all_gone, timeout=timeout)
        except AssertionError:
            pass  # return survivors below
        return alive

    #: ``os.waitid`` is Linux-only in CPython -- macOS has the syscall but the module
    #: does not export it -- so every use of it here needs a fallback. It is the only
    #: NON-DESTRUCTIVE way to ask "has this child exited": ``WNOWAIT`` reports the
    #: status without consuming it. Where it is missing there is no such peek, and the
    #: questions below are answered from pid liveness plus a wait instead.
    _HAS_WAITID = hasattr(os, "waitid")

    @classmethod
    def _our_child_exited(cls, pid: int) -> bool:
        """True when *pid*, a child of THIS process, has stopped running.

        Only valid for a child of this process. Without ``os.waitid`` this cannot tell
        an unreaped zombie from a live process -- both answer a liveness probe as
        present -- so it reports only the unambiguous half, "the pid is gone", and
        callers there prove the exit with a bounded ``wait`` instead.
        """
        if not cls._HAS_WAITID:
            return not platform_compat.pid_exists(pid)
        try:
            peek = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return False
        return peek is not None

    @classmethod
    def _status_still_collectable(cls, pid: int) -> bool:
        """True while this process's child *pid* has an UNCOLLECTED exit status.

        With ``os.waitid`` this is exact: ``WNOWAIT`` peeks without consuming and
        ``ChildProcessError`` means the status is already gone. Without it, liveness
        stands in -- a collected pid is released, so a pid that still answers is one
        whose status nobody has taken.
        """
        if not cls._HAS_WAITID:
            return platform_compat.pid_exists(pid)
        try:
            return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
        except (ChildProcessError, OSError):
            return False

    @staticmethod
    def _running_state(pid: int) -> str | None:
        """The state of *pid* if it is still RUNNING, else None.

        A liveness probe cannot answer this: `kill(pid, 0)` succeeds for a zombie,
        so a pid that answers is either a process the teardown missed or one it
        killed whose reaper has not got to it yet. Only the first breaks a promise,
        and the two need telling apart through the state the OS itself reports.

        `ps -o stat=` is the reader that answers on every POSIX platform, where a
        leading `Z` marks a zombie. It is asked ONLY about a pid that already
        outlived the caller's wait, so the cost of spawning it is paid once per
        survivor rather than once per poll.

        FAIL-CLOSED: a live pid whose state cannot be read reports `"unknown"` and
        therefore counts as running. Treating an unreadable state as stopped would
        turn every reader failure into a pass.
        """
        if not platform_compat.pid_exists(pid):
            return None
        try:
            probe = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        state = probe.stdout.split()
        if not state:
            # `ps` found nothing: the pid went between the liveness probe and here.
            return None
        return None if state[0].startswith("Z") else state[0]

    def _assert_tree_stopped(self, root: subprocess.Popen, descendants: list[int]) -> None:
        """Nothing of the tree is running, and the root's status is accounted for.

        STOPPED is what the teardown promises: the root exited and no recorded
        descendant is still running. The root is checked through this process's own
        child status rather than through liveness, because a zombie answers a liveness
        probe as present and off Linux there is no zombie state to read instead.

        A descendant is judged by whether it RUNS, not by whether its pid is free.
        The pid of a descendant is not the teardown's to release: a grandchild
        reparents to init the moment the root exits, so who collects its status and
        how promptly is that reaper's business -- prompt enough on Linux to look like
        part of the teardown, lazy enough under launchd to outlive this wait. The
        promise the teardown actually makes is that nothing of the tree still runs,
        so that is what is asserted, and a still-running descendant fails WITH the
        state that says so.

        WHO COLLECTED THE ROOT'S STATUS is asserted of production on every platform,
        and BEFORE this helper's own wait, so the cleanup cannot stand in for the thing
        under test. A killed root's identity survives its exit everywhere the teardown
        runs -- Linux reads it from ``/proc``, macOS from the kernel's zombie list --
        so `_reap_provider_root` is obliged to reap it, and this asserts that it did.
        Where ``waitid`` exists the exit is observed separately first, because a peek
        at a still-running child reports nothing to collect and would otherwise pass
        the collection check by default.
        ONE absolute deadline covers every phase below, so the budget for the whole
        teardown to finish is the same 10 seconds a single `_await_gone` asserts.
        Per-phase deadlines would sum instead, letting a slower teardown pass on
        several times that budget.
        """
        deadline = time.monotonic() + 10.0

        def _left() -> float:
            """Seconds still available, never negative, for a phase that takes one."""
            return max(0.0, deadline - time.monotonic())

        alive = list(descendants)

        def _desc_gone() -> bool:
            nonlocal alive
            alive = [pid for pid in alive if platform_compat.pid_exists(pid)]
            return not alive

        try:
            wait_until(_desc_gone, timeout=max(0.0, deadline - time.monotonic()))
        except AssertionError:
            pass
        running = {pid: state for pid in alive if (state := self._running_state(pid)) is not None}
        assert not running, f"the teardown left a descendant running: {running}"

        if self._HAS_WAITID:
            # Where a non-destructive peek exists, observe the EXIT separately: a
            # still-running child answers the peek with "nothing to collect", which the
            # collection assertion below cannot tell from a status already taken.
            wait_until(
                lambda: self._our_child_exited(root.pid),
                timeout=max(0.0, deadline - time.monotonic()),
                describe=lambda: "the teardown left the root running",
            )
            assert self._our_child_exited(root.pid), "the teardown left the root running"

        # WHO COLLECTED THE STATUS, asserted of production on every platform and
        # BEFORE the wait below, so this helper's own cleanup cannot stand in for the
        # thing under test. A teardown that stopped reaping would otherwise still pass
        # the RELEASED half, which is a ratchet that only loosens. Off `waitid` the
        # same question is read through the pid: a collected child is released, so a
        # pid that still answers is one whose status nobody has taken -- and that
        # reading also covers the exit, because a running child answers too.
        wait_until(
            lambda: not self._status_still_collectable(root.pid),
            timeout=max(0.0, deadline - time.monotonic()),
            describe=lambda: "root exit status still uncollected",
        )
        assert not self._status_still_collectable(root.pid), (
            "the teardown left the root's exit status uncollected: "
            "_reap_provider_root did not reap it"
        )
        root.wait(timeout=_left())
        # The pid was already free before that wait, so this re-reads it as a
        # cross-check rather than as an assertion this helper can satisfy by itself.
        left = self._await_gone([root.pid, *descendants], timeout=_left())
        assert left == [], f"pids still held after the tree was torn down: {left}"

    def _reap(self, pids: list[int]) -> None:
        """Best-effort teardown so no test process survives the run.

        Each pid is signalled ONLY while it still carries the identity ``_pin``
        recorded when it was ours. A pid the body proved dead reads a different
        identity here and is left alone: SIGKILL at a recycled number is a signal
        at a stranger. An unpinned pid is a test bug, not a stranger to spare --
        fail loudly rather than leak a 300-second sleeper.

        A pid that is still present but whose identity cannot be read (the macOS
        ``ps`` leg can time out under a loaded run) is neither proven ours nor
        proven gone. The read is retried a few times; if it never answers, the
        teardown does not guess -- it fails the test naming the pid, so the leak
        is reported rather than silent. The sleeper itself exits within 300 s.

        ``os.kill`` directly, not ``platform_compat.kill_pid``: tests in this class
        patch ``kill_pid`` on ``platform_compat`` to observe production's decisions,
        and the teardown must not route through the fake it left behind.
        """
        unconfirmable: list[int] = []
        for pid in pids:
            assert pid in self._pinned, f"pid {pid} was never pinned; call _pin at spawn"
            identity = platform_compat.process_start_time(pid)
            if identity is None and platform_compat.pid_exists(pid):
                try:
                    found = wait_until(
                        lambda pid=pid: platform_compat.process_start_time(pid)
                        or not platform_compat.pid_exists(pid),
                        timeout=0.6,
                        describe=lambda pid=pid: f"start time for {pid} still unreadable",
                    )
                    if isinstance(found, str):
                        identity = found  # use the identity the predicate already read
                except AssertionError:
                    pass  # unreadable after retries; unconfirmable path handles it
            if identity is None and platform_compat.pid_exists(pid):
                unconfirmable.append(pid)
                continue
            if identity != self._pinned[pid]:
                continue  # already gone and possibly reissued
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        assert not unconfirmable, (
            f"pids {unconfirmable} are still present but their identity could not be "
            "read, so the teardown could not confirm they are ours to kill; they were "
            "left running (300-second sleepers)"
        )

    def test_a_zombie_descendant_does_not_count_as_running(self) -> None:
        """The state reader separates a stopped descendant from a live one.

        `_assert_tree_stopped` rests on this distinction, so pin it directly instead
        of only through a teardown: a zombie is a process that has STOPPED and whose
        pid its reaper has not yet collected, and a liveness probe answers the same
        for it as for a process still running. Judging the teardown by liveness alone
        therefore charges it for the reaper's timing -- prompt under init, lazy under
        launchd -- rather than for what it promised.

        Both arms use a real child of this process: one killed and left uncollected,
        one still running.
        """
        zombie = subprocess.Popen([sys.executable, "-c", ""])
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            deadline = time.monotonic() + 10.0
            wait_until(
                lambda: not (
                    platform_compat.pid_exists(zombie.pid) and self._running_state(zombie.pid)
                ),
                timeout=max(0.0, deadline - time.monotonic()),
                describe=lambda: "child never became a zombie",
            )
            assert platform_compat.pid_exists(
                zombie.pid
            ), "the pid was collected, so there is no zombie left to classify"
            assert self._running_state(zombie.pid) is None, "a zombie must not count as running"

            state = self._running_state(live.pid)
            assert state is not None, "a running child must count as running"
            assert not state.startswith("Z"), f"a running child reported a zombie state: {state}"
        finally:
            for child in (zombie, live):
                try:
                    child.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                child.wait(timeout=10)

    def test_in_group_grandchild_is_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A SIGTERM-ignoring grandchild inside the group dies with the group.

        A pid-scoped kill reaches the root only; the grandchild ignores SIGTERM
        and would survive it, so this pins the signal being addressed to the
        process GROUP.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        # The escalation budget is not the behaviour under test; shorten it so
        # the SIGTERM -> SIGKILL path runs without a multi-second wait.
        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            # Precondition: the grandchild really is in the root's group, so a
            # group signal is what covers it.
            assert os.getpgid(grandchildren[0]) == root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_setsid_grandchild_is_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A grandchild that setsid-ed out of the group is still reaped.

        The group signal cannot reach it and it ignores SIGTERM, so the only
        thing that ends it is the snapshot-driven descendant sweep.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=True)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            escaped = grandchildren[0]
            # Precondition: it left the group, so killpg provably misses it.
            deadline = time.monotonic() + 10.0
            wait_until(
                lambda: os.getpgid(escaped) != root.pid,
                timeout=max(0.0, deadline - time.monotonic()),
                describe=lambda: f"pid {escaped} still in root's group",
            )
            assert os.getpgid(escaped) != root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_recorded_descendant_outside_the_tree_is_reaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A descendant recorded at spawn time is reaped even off the live tree.

        Once a child reparents, a walk from the root cannot find it again. The
        runtime's own ``_child_pids`` snapshot is the only record that still
        names it, so the sweep has to read that too.
        """
        from kiro_crew.acp.client import _capture_child_records
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root = self._spawn_isolated()
        stray = self._spawn_isolated()
        try:
            # Not a descendant of root at all — exactly the reparented case.
            provider = self._provider(root.pid, child_pids=_capture_child_records([stray.pid]))

            _sync_kill_provider(provider)

            self._assert_tree_stopped(root, [])
            # Popen.wait reaps: this stray is a direct child of the TEST process
            # (a real one hangs off the runtime), so without a wait it lingers as
            # a zombie and a pid probe still reads it as alive. A negative code
            # is the signal that ended it.
            assert stray.wait(timeout=10) < 0
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_group_signal_alone_reaps_the_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no descendant snapshot, the group signal still reaps the tree.

        Isolates the scope of the signal from the sweep that backs it up: a
        descendant forked between the snapshot and the kill is in the group and
        in no record, so the group is the only thing that covers it. An empty
        snapshot reproduces that state deterministically.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        monkeypatch.setattr(
            "kiro_crew.session_pid._provider_descendant_records", lambda provider, pid, **_kw: {}
        )
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)
            assert os.getpgid(grandchildren[0]) == root.pid

            _sync_kill_provider(self._provider(root.pid))

            self._assert_tree_stopped(root, grandchildren)
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_root_zombie_is_held_until_group_signalling_finishes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No reap may precede the last group signal.

        A zombie owns its pid, and a group leader's pid IS the pgid. Reaping the
        root during the SIGTERM grace frees that number while the SIGKILL
        escalation still aims at it, so a pid recycled into a new group leader
        would take a SIGKILL meant for ours -- unrecoverable, and invisible to
        the descendant sweep's own recycle guard, which only covers descendants.
        Signal ordering is the deterministic pin; the race itself is not
        reproducible on demand.
        """
        from kiro_crew import session_pid as sp
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.3)

        events: list[str] = []
        real_killpg = os.killpg
        real_reap = sp._reap_provider_root

        def tracking_killpg(pgid: int, sig: int) -> None:
            events.append(f"killpg:{sig}")
            real_killpg(pgid, sig)

        def tracking_reap(pid: int, recorded_start: str | None, *, gated: bool) -> None:
            events.append("reap")
            real_reap(pid, recorded_start, gated=gated)

        monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)
        monkeypatch.setattr("kiro_crew.session_pid._reap_provider_root", tracking_reap)

        # The grandchild ignores SIGTERM and stays in the group, so the tree is
        # not gone at the deadline and the SIGKILL escalation actually runs.
        root, gc_pid = self._spawn_tree(escape_group=False)
        grandchildren: list[int] = []
        try:
            grandchildren = self._await_descendants(root.pid)

            _sync_kill_provider(self._provider(root.pid))

            killpg_at = [i for i, e in enumerate(events) if e.startswith("killpg")]
            assert killpg_at, f"no group signal was sent: {events}"
            assert "reap" in events, f"the root was never reaped: {events}"
            assert events.index("reap") > max(
                killpg_at
            ), f"the root was reaped before the last group signal: {events}"
        finally:
            self._reap([root.pid, gc_pid, *grandchildren])
            root.wait(timeout=10)

    def test_unverified_root_is_not_signalled_but_records_still_are(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root whose recorded identity does not match the live pid is spared.

        ``_client._pid`` outlives a failed start, so it can name a process the OS
        has since handed to someone else. For a group leader that pid IS the pgid,
        so signalling it unverified would take a stranger's whole tree, and the
        pid-scoped fallback is the same hazard one process wide -- hence nothing is
        sent to the root at all. Descendants recorded at spawn are still swept:
        each is verified on its own identity, which is the check the root lacked.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = self._spawn_isolated()
        stray = self._spawn_isolated()
        try:
            provider = self._provider(
                root.pid,
                child_pids={stray.pid: (platform_compat.get_process_start_id(stray.pid), None)},
            )
            # A start id no live process can hold: the pid is now "not ours".
            provider._client._start_time = "0.000000"

            _sync_kill_provider(provider)

            assert root.poll() is None, "an unverified root must not be signalled"
            assert stray.wait(timeout=10) < 0, "a recorded descendant must still be swept"
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_group_is_not_signalled_once_no_verified_member_owns_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pgid is re-checked before every signal, not trusted from entry.

        This teardown is not the only reaper: asyncio's own child watcher can
        collect the leader zombie, which frees the pid that IS the pgid, and under
        pid-space wraparound a new group leader can take it inside the grace. A
        group holding no member this code can verify is therefore left alone --
        signalling it would reap a stranger's tree. Descendants recorded
        at spawn are still signalled, because each is named and verified on its own.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = self._spawn_isolated()
        stray = self._spawn_isolated()
        killpg_calls: list[tuple[int, int]] = []
        real_pgroup_of = platform_compat.pgroup_of
        seen = {"n": 0}

        def pgroup_of_then_handed_on(probe_pid: int) -> int | None:
            # First read resolves OUR group, as it does in production; every read
            # after it reports a different owner, i.e. the group emptied and the
            # number moved on mid-grace.
            seen["n"] += 1
            return real_pgroup_of(probe_pid) if seen["n"] == 1 else 999_999

        monkeypatch.setattr(
            "kiro_crew.session_pid.os.killpg",
            lambda pgid, sig: killpg_calls.append((pgid, sig)),
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of", pgroup_of_then_handed_on
        )
        try:
            provider = self._provider(
                root.pid,
                child_pids={stray.pid: (platform_compat.get_process_start_id(stray.pid), None)},
            )

            _sync_kill_provider(provider)

            assert killpg_calls == [], f"signalled a group nothing verified owns: {killpg_calls}"
            assert stray.wait(timeout=10) < 0, "a recorded descendant must still be swept"
        finally:
            self._reap([root.pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_windows_tree_kill_is_identity_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Windows arm pins the identity across taskkill, and honours a refusal.

        ``taskkill /T /F /PID`` resolves the pid from a SEPARATE process, so a
        caller that reads the start id and then calls the plain tree kill has
        released every handle by then: the provider can exit and Windows can hand
        that pid to something else, whose whole tree taskkill then tears down.
        ``kill_process_tree_pinned`` holds the verifying query handle open across
        the terminate -- Windows reserves a pid while any handle to the process
        object lives -- and returns False instead of killing when it cannot
        confirm. This runs on POSIX by faking the platform flag, because the
        branch it pins is unreachable here.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        pinned_calls: list[tuple[int, str, int]] = []
        unpinned_calls: list[tuple[int, int]] = []

        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True)
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_process_tree_pinned",
            lambda pid, expected, sig: (pinned_calls.append((pid, expected, sig)), False)[1],
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_process_tree",
            lambda pid, sig: unpinned_calls.append((pid, sig)),
        )

        provider = self._provider(os.getpid())
        provider._client._start_time = platform_compat.get_process_start_id(os.getpid())

        _sync_kill_provider(provider)

        assert pinned_calls, "the Windows arm must use the identity-pinned tree kill"
        assert pinned_calls[0][0] == os.getpid()
        assert pinned_calls[0][1] == provider._client._start_time
        # A False return is a refusal to reap: no un-pinned kill may follow it.
        assert unpinned_calls == [], f"fell back to an unpinned kill: {unpinned_calls}"

    def test_fresh_walk_is_discarded_when_the_root_goes_stale_during_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root verified before the walk can be a different process after it.

        The pid can be reused while the walk runs, so a walk that started on our
        tree can finish on a replacement's -- and those pids WOULD verify, because
        their identity is captured here and re-read moments later. Bracketing the
        walk is what stops a stale verification authorising the sweep: the freshly
        walked entries are dropped, while the spawn-time snapshot, recorded when
        the tree was provably ours, is still signalled.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root, gc_pid = self._spawn_tree(escape_group=False)
        stray = self._spawn_isolated()
        real_start_id = platform_compat.get_process_start_id
        self._await_descendants(root.pid)  # the walk has something to find
        # Built BEFORE the patch: _provider reads the start id itself, and counting
        # that read would shift the failure onto the PRE-walk check instead.
        provider = self._provider(
            root.pid, child_pids={stray.pid: (real_start_id(stray.pid), None)}
        )
        root_reads = {"n": 0}

        def start_id_going_stale(probe_pid: int) -> str | None:
            # Reads 1 (entry gate) and 2 (pre-walk) see our root; read 3 is the
            # post-walk re-check and sees a value no live process here holds.
            if probe_pid == root.pid:
                root_reads["n"] += 1
                if root_reads["n"] >= 3:
                    return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_going_stale
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            # The DECISION is the pin, not a liveness read: a freshly walked pid
            # must never be handed to a signal, whereas a recorded one must be.
            assert gc_pid not in signalled, f"a freshly walked pid was signalled: {signalled}"
            assert stray.pid in signalled, "the spawn-time snapshot must still be swept"
        finally:
            self._reap([root.pid, gc_pid, stray.pid])
            root.wait(timeout=10)
            stray.wait(timeout=10)

    def test_walked_pid_is_dropped_when_it_leaves_the_tree_before_capture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration and identity capture are separate reads of the process table.

        A descendant can exit in between and its pid be reused, so the id captured
        would be a stranger's -- and it verifies at signal time, because it was read
        after the swap. Re-confirming ancestry after the capture drops that pid. The
        second snapshot is only an intersection filter, so it can shrink the set and
        never add to it.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = self._spawn_isolated()
        bystander = self._spawn_isolated()
        provider = self._provider(root.pid)
        walks = {"n": 0}

        def walk_then_lose_it(probe_pid: int) -> list[int]:
            # The first walk reports the pid; by the re-confirmation it is gone,
            # which is what a reuse between the two reads looks like from here.
            if probe_pid != root.pid:
                return []
            walks["n"] += 1
            return [bystander.pid] if walks["n"] == 1 else []

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", walk_then_lose_it
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            assert bystander.pid not in signalled, f"a dropped pid was signalled: {signalled}"
            assert platform_compat.pid_exists(bystander.pid), "the bystander must survive"
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    @pytest.mark.skipif(
        not hasattr(os, "waitid"),
        reason=(
            "needs a NON-DESTRUCTIVE 'has this child exited' peek to stand in for the "
            "post-exit identity read and to prove the reaper refused without consuming "
            "the status; os.waitid is Linux-only in CPython, and consuming the status "
            "to look at it would destroy the state under test. This test is new in "
            "this change, so nothing universal is narrowed by skipping it -- the "
            "portable half of the same property is asserted by _assert_tree_stopped."
        ),
    )
    def test_the_tree_stops_even_where_the_root_cannot_be_identified_after_it_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the teardown promises off Linux: killed, not necessarily reaped.

        Stands in for the macOS post-exit identity read -- `proc_pidinfo` reports
        nothing once a process has exited, while a RUNNING process still answers -- so
        the reaper cannot confirm the root it just killed and correctly refuses to wait
        on it. The tree is still STOPPED, which is the portable promise, and the root's
        pid stays held by its zombie, which is what this asserts: a refusal leaves the
        status uncollected.

        A zombie is not a survivor: its memory is released, it cannot run again, and
        asyncio's child watcher may still collect it.

        Mutation guard: permitting the wait when the live read is None -- reaping on an
        unconfirmable identity -- consumes the status and reddens the last assertion.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)
        root, gc_pid = self._spawn_tree(escape_group=False)
        try:
            grandchildren = self._await_descendants(root.pid)
            provider = self._provider(root.pid)
            real_start_id = platform_compat.get_process_start_id

            def _unreadable_once_exited(probe_pid: int) -> str | None:
                if probe_pid == root.pid and self._our_child_exited(probe_pid):
                    return None
                return real_start_id(probe_pid)

            monkeypatch.setattr(
                "kiro_crew.session_pid.platform_compat.get_process_start_id",
                _unreadable_once_exited,
            )
            _sync_kill_provider(provider)

            deadline = time.monotonic() + 10.0
            wait_until(
                lambda: self._our_child_exited(root.pid),
                timeout=max(0.0, deadline - time.monotonic()),
                describe=lambda: "the root was not killed",
            )
            assert self._our_child_exited(root.pid), "the root was not killed"
            assert self._await_gone(grandchildren) == [], "a descendant kept running"
            assert self._status_still_collectable(
                root.pid
            ), "the root was reaped on an identity the reaper could not confirm"
        finally:
            self._reap([root.pid, gc_pid])
            root.wait(timeout=10)

    def test_reap_refuses_a_pid_whose_identity_changed(self) -> None:
        """A wait on a recycled pid consumes an unrelated child's exit status.

        Nothing detects or repairs that loss, so the reaper re-reads the identity
        instead of trusting the one taken at entry. Uses a real zombie: the status is
        either still there to collect or it is gone, which is the whole effect.

        Two things here are platform-specific and are handled rather than assumed.
        The identity is recorded while the child is still RUNNING, which every
        platform can read -- reading it off the zombie instead only works on Linux,
        where `/proc/<pid>/stat` outlives the exit. And whether a correctly identified
        zombie can then be reaped at all is Linux-only for the same reason: off Linux
        `proc_pidinfo` reports nothing once a process has exited, so the reaper cannot
        confirm the identity and correctly refuses. The refusal arm -- the actual
        subject of this test -- runs everywhere.

        `_pid_exited_but_unreaped` is deliberately NOT the barrier: off Linux it falls
        back to plain liveness, and a zombie answers a liveness probe as present, so it
        never reports the state this constructs. `_our_child_exited` asks this
        process's own child status instead, which is answerable anywhere.
        """
        from kiro_crew import session_pid as sp

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            # Recorded while it RUNS, the way the ACP layer records it at spawn.
            recorded = platform_compat.get_process_start_id(child.pid)
            assert recorded is not None, "a running child's identity must be readable"
            child.kill()

            # Wait for the exit without consuming the status the refusal arm inspects:
            # ``waitid(WNOWAIT)`` peeks where it exists; elsewhere (macOS) the kernel's
            # own process state reports the zombie, which is a pid that has exited and
            # whose status is still uncollected.
            def _exited() -> bool:
                if self._HAS_WAITID:
                    return self._our_child_exited(child.pid)
                return platform_compat.pid_is_zombie(child.pid) is True

            wait_until(
                _exited,
                timeout=10,
                describe=lambda: (
                    f"the child never exited; pid_exists={platform_compat.pid_exists(child.pid)}"
                ),
            )
            assert self._status_still_collectable(child.pid), "the status must start uncollected"

            sp._reap_provider_root(child.pid, "0.000000", gated=True)
            assert self._status_still_collectable(
                child.pid
            ), "the status was consumed despite an identity mismatch"

            if sys.platform == "linux":
                # Read straight off the zombie, which only Linux keeps readable.
                sp._reap_provider_root(child.pid, recorded, gated=True)
            else:
                # Elsewhere the READER is stood in for, because the property under test
                # is "a CONFIRMED identity is waited on", not "this platform can confirm
                # one after the process exits". Gating the assertion away instead would
                # narrow a universal property to a platform allowlist.
                with pytest.MonkeyPatch.context() as patch:
                    patch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: recorded)
                    sp._reap_provider_root(child.pid, recorded, gated=True)
            assert not self._status_still_collectable(
                child.pid
            ), "a zombie whose identity is confirmed must be reaped"
        finally:
            # Unconditional: an assertion that fails BEFORE the kill above would
            # otherwise leave a running sleeper behind, and fabricating a return code
            # for it would hide that. Killing something already dead is a no-op, and
            # `Popen.wait` treats a child collected elsewhere as collected.
            try:
                child.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            child.wait(timeout=10)

    def test_fresh_walk_is_discarded_when_the_root_goes_stale_across_the_rescan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-scan is bracketed too, because an intersection is not only membership.

        If the ROOT pid is reused by a foreign process while this runs, the re-scan
        enumerates that stranger's children, so a first-walk pid its child now holds
        is confirmed by the filter and carries the stranger's captured id -- which the
        signal-time re-read then agrees with instead of catching.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root = self._spawn_isolated()
        bystander = self._spawn_isolated()
        real_start_id = platform_compat.get_process_start_id
        provider = self._provider(root.pid)
        # Both scans report the pid, so the intersection keeps it; only the root
        # identity going stale across them can reject it.
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants",
            lambda probe: [bystander.pid] if probe == root.pid else [],
        )
        root_reads = {"n": 0}

        def start_id_going_stale(probe_pid: int) -> str | None:
            # Reads 1 (entry), 2 (pre-walk) and 3 (post-walk) hold; read 4 is the
            # post-re-scan check and sees a value no live process here holds.
            if probe_pid == root.pid:
                root_reads["n"] += 1
                if root_reads["n"] >= 4:
                    return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_going_stale
        )
        signalled: list[int] = []
        real_kill_pid = platform_compat.kill_pid
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.kill_pid",
            lambda p, sig: (signalled.append(p), real_kill_pid(p, sig))[1],
        )
        try:
            _sync_kill_provider(provider)

            assert (
                bystander.pid not in signalled
            ), f"a pid confirmed only by a stranger's tree was signalled: {signalled}"
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    def test_reaped_leader_does_not_suppress_the_group_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Group ownership authorizes killpg, so losing the root must not skip it.

        asyncio's watcher can collect the leader zombie during the grace. If that
        also cancelled the group escalation, a descendant that forked into the group
        after the snapshot would be reached by neither the group signal nor the
        recorded sweep, and would survive the teardown.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        root, gc_pid = self._spawn_tree(escape_group=False)
        real_start_id = platform_compat.get_process_start_id
        self._await_descendants(root.pid)
        # The grandchild is recorded at spawn, so _pgroup_still_ours can verify it
        # owns the pgid without consulting the root at all.
        provider = self._provider(root.pid, child_pids={gc_pid: (real_start_id(gc_pid), None)})

        real_descendants = platform_compat.process_descendants
        records_built = {"done": False}

        def counting_descendants(probe_pid: int) -> list[int]:
            found = real_descendants(probe_pid)
            # Both record-building scans have run by the second call; from here on
            # the root reads as a stranger, which is what an asyncio reap looks like.
            if probe_pid == root.pid:
                records_built["done"] = True
            return found

        def start_id_after_reap(probe_pid: int) -> str | None:
            if probe_pid == root.pid and records_built["done"]:
                return "0.000000"
            return real_start_id(probe_pid)

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", counting_descendants
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", start_id_after_reap
        )
        killpg_calls: list[int] = []
        real_killpg = os.killpg

        def tracking_killpg(pgid: int, sig: int) -> None:
            killpg_calls.append(sig)
            real_killpg(pgid, sig)

        monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)
        try:
            _sync_kill_provider(provider)

            assert platform_compat.SIGKILL in killpg_calls, (
                "the group SIGKILL is suppressed once the leader is reaped: "
                f"killpg signals seen = {killpg_calls}"
            )
        finally:
            self._reap([root.pid, gc_pid])
            root.wait(timeout=10)

    def test_recycled_descendant_is_not_signalled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A recorded pid whose identity does not match the live process is left alone.

        Deny-by-default: a pid recycled to another process inside the teardown
        window must not be signalled, so a wrong record is skipped rather than
        used.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.5)

        root = self._spawn_isolated()
        bystander = self._spawn_isolated()
        try:
            # A record claiming an impossible start time: the live process cannot
            # match it, so the sweep must refuse to signal that pid.
            provider = self._provider(root.pid, child_pids={bystander.pid: (1, b"python3")})

            _sync_kill_provider(provider)

            self._assert_tree_stopped(root, [])
            # Popen.poll reaps: a signalled direct child of the TEST process
            # would linger as a zombie that a pid probe still reads as alive, so
            # an exit status is what proves nothing was sent to it.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                assert bystander.poll() is None, "a pid failing verification was signalled"
                time.sleep(0.05)  # flake-ok: absence check over bounded window; no signal to await
        finally:
            self._reap([root.pid, bystander.pid])
            root.wait(timeout=10)
            bystander.wait(timeout=10)

    def _spawn_reaped_leader(self, ready_dir: Path) -> tuple[int, str | None, int, int]:
        """Spawn an isolated leader with two SIGTERM-ignoring children, then reap it.

        Returns the leader's pid, the start id recorded for it while it was alive,
        and the two children's pids, pinned for ``_reap``. On return the leader is
        gone from ``/proc`` while its group still holds both children -- the shape a
        pid alone cannot tell apart from a recycled one.

        Both children ignore SIGTERM and touch ``ready_dir/<pid>`` once they have,
        so a caller can wait out the window in which a SIGTERM would still kill them
        and the test would prove nothing about the SIGKILL escalation. One of them
        stays out of the caller's records: only a group signal can reach it, which
        is the property under test.
        """
        child = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open(os.path.join({str(ready_dir)!r}, str(os.getpid())), 'w').close()\n"
            "time.sleep(300)\n"
        )
        source = (
            "import subprocess, sys\n"
            # DEVNULL, not the inherited pipe: a child holding the write end keeps
            # the parent's readline() waiting for EOF even after the root dies, so
            # an inherited pipe would stall the malformed-report cleanup below for
            # the child's whole 300-second sleep.
            f"witness = subprocess.Popen([sys.executable, '-c', {child!r}],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"unrecorded = subprocess.Popen([sys.executable, '-c', {child!r}],\n"
            "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(witness.pid, unrecorded.pid, flush=True)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", source],
            start_new_session=True,
            stdout=subprocess.PIPE,
            # The spawned tree writes only absolute paths under ready_dir, but it
            # would otherwise inherit the checkout as its CWD; anchor it so no
            # relative write can ever reach the repository.
            cwd=str(ready_dir),
        )
        assert proc.stdout is not None
        try:
            recorded_start = platform_compat.get_process_start_id(proc.pid)
            # The leader exits after reporting, and neither child holds its pipe.
            # Bound BOTH the read and the reap, not just a wait after readline().
            output, _ = proc.communicate(timeout=10)
            reported = output.split()
            assert len(reported) == 2, "provider root never reported both child pids"
            witness_pid, unrecorded_pid = (int(value) for value in reported)
            self._pin(witness_pid, unrecorded_pid)
            return proc.pid, recorded_start, witness_pid, unrecorded_pid
        except BaseException:
            # Kill the GROUP, not just the root. One child can already be running
            # when setup fails, even if its pid was never reported.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.kill()
            proc.wait(timeout=10)
            raise
        finally:
            proc.stdout.close()

    @pytest.mark.parametrize("report", ["timeout", "missing", "invalid"])
    def test_reaped_leader_setup_failure_cleans_up_group(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report: str
    ) -> None:
        """A failed setup must not leave unreported children or an open pipe."""
        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = UNALLOCATABLE_PID
        proc.stdout = MagicMock()
        if report == "timeout":
            error = subprocess.TimeoutExpired("provider fixture", 10)
            proc.communicate.side_effect = error
            proc.stdout.readline.side_effect = error
            expected = subprocess.TimeoutExpired
        else:
            output = b"" if report == "missing" else b"invalid 4244\n"
            proc.communicate.return_value = (output, None)
            proc.stdout.readline.return_value = output
            expected = AssertionError if report == "missing" else ValueError
        monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: proc)
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "start")
        killpg = MagicMock()
        monkeypatch.setattr(os, "killpg", killpg)

        with pytest.raises(expected):
            self._spawn_reaped_leader(tmp_path)

        proc.communicate.assert_called_once_with(timeout=10)
        killpg.assert_called_once_with(proc.pid, signal.SIGKILL)
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=10)
        proc.stdout.close.assert_called_once_with()

    def test_group_is_recovered_when_the_leader_was_reaped_before_teardown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A leader already gone at entry must not cost the group its signal.

        ``pgroup_of`` needs the leader in ``/proc``, so a leader reaped before this
        teardown starts leaves no readable group id and the escalation sends nothing
        to the group at all -- while the members left in it keep running. Recovering
        the group from a descendant recorded at spawn is what reaches the member no
        record names: ``unrecorded`` here is absent from the records and ignores
        SIGTERM, so only the group SIGKILL can end it.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.2)
        leader_pid, recorded_start, witness_pid, unrecorded_pid = self._spawn_reaped_leader(
            tmp_path
        )
        try:
            wait_until(
                lambda: (tmp_path / str(witness_pid)).exists()
                and (tmp_path / str(unrecorded_pid)).exists(),
                timeout=10.0,
                describe=lambda: "children never installed SIGTERM handlers",
            )
            assert (
                tmp_path / str(witness_pid)
            ).exists(), "the recorded child never installed its SIGTERM handler"
            assert (
                tmp_path / str(unrecorded_pid)
            ).exists(), "the unrecorded child never installed its SIGTERM handler"
            # Preconditions: the leader is unreadable, and the group it led still
            # holds both children -- one recorded, one only reachable via the group.
            assert platform_compat.get_process_start_id(leader_pid) is None
            assert platform_compat.pgroup_of(witness_pid) == leader_pid
            assert platform_compat.pgroup_of(unrecorded_pid) == leader_pid

            witness_start = platform_compat.get_process_start_id(witness_pid)
            provider = self._provider(leader_pid, child_pids={witness_pid: (witness_start, None)})
            provider._client._start_time = recorded_start

            killpg_calls: list[tuple[int, int]] = []
            real_killpg = os.killpg

            def tracking_killpg(pgid: int, sig: int) -> None:
                killpg_calls.append((pgid, sig))
                real_killpg(pgid, sig)

            monkeypatch.setattr("kiro_crew.session_pid.os.killpg", tracking_killpg)

            _sync_kill_provider(provider)

            assert self._await_gone([unrecorded_pid]) == [], (
                "a SIGTERM-ignoring member of a reaped leader's group survived teardown, "
                "and no record named it"
            )
            assert {pgid for pgid, _sig in killpg_calls} == {leader_pid}
            assert platform_compat.SIGKILL in {
                sig for _pgid, sig in killpg_calls
            }, f"the group escalation never reached SIGKILL: {killpg_calls}"
        finally:
            self._reap([witness_pid, unrecorded_pid])

    def test_reaped_leader_recovers_no_group_without_a_witness_in_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A witness that left the group cannot vouch for it.

        The recovered id is only as good as the member proving the group is ours. A
        descendant that called ``setsid`` leads a group of its own, so it says
        nothing about the leader's -- which stays unsignalled, exactly as before.
        """
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        # The witness verifies, but it leads its own group.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4243)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("same", None)}) is None

    def test_recovered_group_refuses_a_group_the_pid_does_not_lead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A group id that is not ``pid``'s own is refused even with a live witness.

        This is the foreign-group fence on its own. The witness verifies AND belongs
        to the resolved group, so the ownership check would vouch for it -- but the
        group is not the one this leader led, so signalling it would reach past our
        tree. Only ``candidate == pid`` stops that, which is why this case exists
        separately from the witness-left-the-group one above.
        """
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        # A group the recorded leader 4242 does not lead.
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4243
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        # The witness is genuinely in 4243, so ownership alone would say yes.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4243)

        assert _group_from_witnessed_descendant(4242, "same", {4243: ("same", None)}) is None

    def test_group_recovery_is_not_attempted_for_a_live_handle_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `_proc` shape never reaches group recovery, so a walk cannot vouch.

        That shape passes ``gated=False``, which makes every ``_root_identity_holds``
        check pass on trust -- including the two bracketing the live descendant walk.
        A pid recycled to a foreign root would therefore have THAT root's children
        walked into ``records`` and verified against identities read from the same
        stranger, so a witness drawn from them proves nothing about our tree. The
        shape also cannot need recovery: its root was alive when the handle was read.
        """
        from kiro_crew.session_pid import _sync_kill_provider

        monkeypatch.setattr("kiro_crew.session_pid._PROVIDER_TERM_GRACE_SECONDS", 0.1)
        calls: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.session_pid._group_from_witnessed_descendant",
            lambda pid, *a, **k: calls.append(pid) or 4242,
        )
        # A `_proc`-shaped provider: no _client, a live handle with returncode None.
        provider = MagicMock(spec=["_client", "_proc", "_active_proc"])
        provider._client = None
        provider._proc = MagicMock(spec=["pid", "returncode"])
        provider._proc.pid = UNALLOCATABLE_PID
        provider._proc.returncode = None
        provider._active_proc = None
        # Not an isolated leader, so _isolated_provider_group yields None and the
        # recovery branch is the only thing that could produce a pgid.
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 999)
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.process_descendants", lambda _p: []
        )
        killpg_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "kiro_crew.session_pid.os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.kill_pid", lambda _p, _s: None)
        # The fake pid names no process, so the root's reap is observed, not run.
        reaped: list[int] = []
        monkeypatch.setattr(
            "kiro_crew.session_pid._reap_provider_root", lambda pid, *a, **k: reaped.append(pid)
        )

        _sync_kill_provider(provider)

        assert reaped == [UNALLOCATABLE_PID], "the teardown never reached the root's reap"
        assert calls == [], "group recovery was attempted for a non-gated live-handle root"
        assert killpg_calls == [], f"a group was signalled for a live-handle root: {killpg_calls}"

    def test_recovered_group_refuses_a_recycled_witness(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorded pid whose live identity differs cannot vouch for the group."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "live"
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4242)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("recorded", None)}) is None

    def test_recovered_group_needs_a_witness_with_a_recorded_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A record carrying no start id proves nothing and is skipped."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: 4242
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: None
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: 4242)

        assert _group_from_witnessed_descendant(4242, "root", {4243: (None, None)}) is None

    def test_recovered_group_is_never_our_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Our own group is refused: killpg on it would signal the gateway itself."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        own = os.getpgrp()
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: own
        )
        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.get_process_start_id", lambda _pid: "same"
        )
        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.pgroup_of", lambda _pid: own)

        assert _group_from_witnessed_descendant(own, "root", {os.getpid(): ("same", None)}) is None

    def test_recovered_group_refuses_a_denied_leader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``None`` from the primitive means signalling is denied, so nothing is sent."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr(
            "kiro_crew.session_pid.platform_compat.pgroup_of_leader", lambda _pid: None
        )

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("same", None)}) is None

    def test_no_group_is_recovered_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows has no such group; teardown there goes through kill_process_tree."""
        from kiro_crew.session_pid import _group_from_witnessed_descendant

        monkeypatch.setattr("kiro_crew.session_pid.platform_compat.IS_WINDOWS", True)

        assert _group_from_witnessed_descendant(4242, "root", {4243: ("start", None)}) is None


@_POSIX_ONLY
@pytest.mark.skipif(
    not hasattr(os, "waitid"),
    reason=(
        "each case needs a CONFIRMED unreaped zombie before it can assert the reaper "
        "left the status alone, and without os.waitid -- Linux-only in CPython -- "
        "nothing here can confirm one: a live child and an unreaped zombie both answer "
        "a liveness probe as present, so the helper would hand the case a child that is "
        "still running and the assertion would hold no matter what the reaper did. "
        "Running these off Linux was measured to pass under a reaper mutated to wait on "
        "an unconfirmable identity, which is the harm they exist to catch. The portable "
        "half of the property is asserted by _assert_tree_stopped, which reddens six "
        "tests under that same mutation with waitid removed."
    ),
)
class TestReapProviderRoot:
    """An UNREADABLE identity refuses the wait, and that refusal is deliberate.

    `_sync_kill_provider` kills the root and holds its zombie unreaped until every
    group signal has gone out, so the reap runs against a process that has already
    exited. Whether its recorded identity can still be read there is a PLATFORM
    property: Linux keeps `/proc/<pid>/stat` readable for a zombie, macOS
    `proc_pidinfo` reports nothing once a process has exited.

    So off Linux the reap is refused, and that is deny-by-default working: an
    unreadable identity is exactly the case where our own zombie cannot be told from
    a pid already reaped by asyncio's watcher and recycled into another child of this
    process that is itself an unreaped zombie -- which reads as unreadable too, and
    whose status a wait would steal. What the refusal leaves is a zombie: memory
    already released, one process-table entry, and the watcher may still collect it.

    Simulating only the identity READ keeps this verifiable on any host.
    """

    @staticmethod
    def _zombie() -> tuple[subprocess.Popen, int]:
        """A real unreaped zombie: exited, never waited on. Returns it and its pid."""
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        wait_until(
            lambda: TestSyncKillProviderTree._status_still_collectable(proc.pid),
            timeout=10.0,
            # Deliberately NOT poll()/wait() -- either would reap it and free the pid.
            describe=lambda: f"child {proc.pid} never became a zombie",
        )
        return proc, proc.pid

    def _assert_refused(self, live_id: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import session_pid as sp

        proc, pid = self._zombie()
        try:
            monkeypatch.setattr(sp.platform_compat, "get_process_start_id", lambda _pid: live_id)
            sp._reap_provider_root(pid, "1758000000.000001", gated=True)
            assert TestSyncKillProviderTree._status_still_collectable(
                pid
            ), "the status was consumed on an identity the reaper could not confirm"
        finally:
            try:
                proc.wait(timeout=10)
            except ChildProcessError:
                pass

    def test_an_unreadable_identity_refuses_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The macOS post-exit case: nothing readable, so nothing is waited on.

        Mutation guard: permitting the wait when the live read is None -- which is
        what "reap it anyway off Linux" would do -- consumes the status and reddens
        this.
        """
        self._assert_refused(None, monkeypatch)

    def test_a_readable_different_identity_refuses_the_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recycled pid reads as a DIFFERENT id, and that refuses the wait too."""
        self._assert_refused("999.000999", monkeypatch)
