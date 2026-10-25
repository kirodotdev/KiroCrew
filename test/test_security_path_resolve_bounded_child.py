"""The resolver child behind the bounded sensitive-path resolution.

Whether a stall charges its prefix rests on the child's kernel state: one
descheduled worker or a saturated pool refuses without opening a cooldown, until
a run of descheduled probes spends the window's allowance; a worker blocked in a
filesystem syscall still charges; and the syscall table that tells the two apart
is pinned for every architecture and against this host's kernel headers, with
the ``/proc`` sampling it rests on. Then the wiring itself: the ``realpath``
runs in ``subprocess_pool``'s child, reached from the calling thread, and every
fault path fails closed. The shared fixture and stall doubles live in
``security_path_resolve_bounded_helpers.py``.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import platform
import re
import threading
import time

import pytest
from security_path_resolve_bounded_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _REAL_BLOCKED_IN_FILESYSTEM,
    _fresh_resolver_state,
    _StalledResolver,
)

import kiro_crew.executors as ex
from kiro_crew import security
from kiro_crew.subprocess_pool import SubprocessPoolUnavailable
from kiro_crew.subprocess_pool.executor import proc_syscall


@contextlib.contextmanager
def _armed(seconds: float = 10.0):
    """Arm the per-thread deadline the resolver's child requests share.

    Outside a bounded call the resolver answers in-process by contract (the inline
    entry point), so a test that wants to exercise the CHILD path must arm one, as
    ``_run_resolution_bounded`` does in production.
    """
    security.paths._child_budget.deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        security.paths._child_budget.deadline = None


def test_a_descheduled_worker_does_not_charge_the_prefix(monkeypatch) -> None:
    # THE LOAD ARM.  A worker that STARTED and then lost the CPU has learned nothing
    # about the mount, so charging the prefix converts ordinary contention into a
    # cooldown that refuses every path under it -- including, in the field, every
    # scheduled cron script for as long as the ceiling allowed.  The refusal of THIS
    # resolution is unchanged: the gate still fails closed, it just stops generalising
    # from one descheduled thread to a whole subtree.  kirodotdev/KiroCrew#9482.
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: False)
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
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: True)
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
    assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(threading.get_native_id())) is False
    # ...and it fails TOWARD the pre-existing behaviour when /proc cannot answer, so a
    # non-Linux host or an exited process keeps charging the prefix as it did before.
    assert _REAL_BLOCKED_IN_FILESYSTEM(None) is True
    assert proc_syscall(2**31 - 1) is None
    assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(2**31 - 1)) is True


def test_a_saturated_pool_refuses_without_charging_or_tracking(caplog) -> None:
    # THE QUEUED ARM, end to end on the real pool.  kirodotdev/KiroCrew#9482:
    # simultaneous cron fires lease both children, so a third resolution times
    # out having never been TAKEN by a child.  Lease wait is evidence about
    # load, not the mount: the call is still refused (fail-closed, unchanged),
    # but no cooldown opens -- otherwise one busy morning refuses every path
    # under the home prefix without a single slow filesystem operation.
    pool = ex.path_resolve_executor()
    leased = [pool._free.get(timeout=5.0) for _ in pool._children]
    try:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.security.paths"):
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/someone/ws/file")
        assert security._path_resolve_degraded == {}, "lease wait must open no cooldown"
        assert any("the resolution never started" in r.message for r in caplog.records)
    finally:
        for child in leased:
            pool._free.put(child)
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
            time.sleep(
                0.02
            )  # flake-ok: measurement interval; sampling /proc/<tid>/syscall at regular intervals
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
        assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(tid)) is True
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr + 1000}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(tid)) is False
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset())
        assert (
            _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(tid)) is True
        ), "an unmapped arch must charge"
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

    """
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: False)
    monkeypatch.setattr(security.paths, "_path_resolve_degraded", {})
    monkeypatch.setattr(security.paths, "_path_resolve_load_probes", {})
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
    monkeypatch.setattr(security.paths, "_child_blocked_in_filesystem", lambda sampled: False)
    monkeypatch.setattr(security.paths, "_path_resolve_degraded", {})
    monkeypatch.setattr(security.paths, "_path_resolve_load_probes", {})
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

    ``/usr/include/asm`` is a *host-architecture* uapi tree, so ``asm/unistd_64.h`` only
    carries the x86_64 numbering on an x86_64 host -- on an aarch64 host the same path is the
    aarch64 header (which has no ``__NR_stat`` at all). The x86_64 header entry is therefore
    gated on the host machine, while ``asm-generic/unistd.h`` is architecture-neutral and
    validated on every host. The sibling
    ``test_the_syscall_table_matches_the_documented_numbers_on_every_architecture`` still pins
    the shipped table for BOTH architectures regardless of host, so nothing is left unchecked.
    """
    host_machine = platform.machine()
    checked = 0
    for header, arch, indirect, host_specific in (
        ("/usr/include/asm/unistd_64.h", "x86_64", {}, True),
        (
            "/usr/include/asm-generic/unistd.h",
            "aarch64",
            {"newfstatat": "__NR3264_fstatat", "fstat": "__NR3264_fstat"},
            False,
        ),
    ):
        if host_specific and host_machine != arch:  # pragma: no cover - arch-dependent
            # The host-arch asm/ tree carries a different architecture's numbering here;
            # cross-checking it against this arch's table would be comparing the wrong header.
            continue
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
            time.sleep(
                0.05
            )  # flake-ok: measurement interval; sampling /proc/<tid>/stat and /syscall at regular intervals
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
        assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(tid)) is True
        monkeypatch.setattr(security.paths, "_FS_BLOCKING_SYSCALLS", frozenset({blocked_nr + 1000}))
        assert _REAL_BLOCKED_IN_FILESYSTEM(proc_syscall(tid)) is False
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


# ---------------------------------------------------------------------------
# The wiring itself: the ``realpath`` runs in ``subprocess_pool``'s child, reached
# from the calling thread, and every fault path fails closed.  kirodotdev/KiroCrew#10255.
# ---------------------------------------------------------------------------


def _hog_the_gil(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(i * i for i in range(20_000))


def test_the_child_answers_identically_to_in_process_resolution(tmp_path) -> None:
    """Parity for both ops on the shapes that matter: a symlink, a missing path, a
    name with a newline (where the filesystem permits one), a ``..`` AFTER a symlink,
    and the anchor batch with a failing entry."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(target, target_is_directory=True)
    deep = tmp_path / "store" / "inner"
    deep.mkdir(parents=True)
    into_store = tmp_path / "into_store"
    into_store.symlink_to(deep, target_is_directory=True)
    candidates = [
        str(link / "id_rsa"),
        str(tmp_path / "missing"),
        # ``..`` after a symlink: the kernel walks INTO the store and back up one
        # level (``store/credentials``); a lexical normpath would say ``tmp/credentials``.
        str(into_store / ".." / "credentials"),
        "rel/path",
        "rel/../other",
    ]
    if os.name != "nt":
        odd = tmp_path / "with\nnewline"
        odd.write_text("x")
        candidates.append(str(odd))
    with _armed():
        for candidate in candidates:
            assert security._resolved_spellings(
                candidate
            ) == security.paths._resolved_spellings_inline(candidate)
        dotdot = str(into_store / ".." / "credentials")
        resolved = security._resolved_spellings(dotdot)
    assert resolved == security.paths._resolved_spellings_inline(dotdot)
    if os.name != "nt":
        # POSIX: the kernel walked INTO the store, so the answer ends in
        # store/credentials, not the lexical tmp/credentials a normpath before
        # resolution would produce. (``ntpath.realpath`` normalises first, so on
        # Windows both resolvers agree on the lexical answer; the parity above is
        # the whole assertion there.)
        assert all(p.endswith(os.path.join("store", "credentials")) for p in resolved), resolved
        assert not any(p == str(tmp_path / "credentials") for p in resolved)
    anchors = [str(link), str(tmp_path / "missing"), str(into_store / ".." / "x"), "\0bad"]
    with _armed():
        assert security.paths._realpaths_or_none(anchors) == [
            security.paths._realpath_inline(anchor) for anchor in anchors
        ]


def test_resolution_completes_inside_the_budget_while_a_sibling_thread_hogs_the_gil(
    monkeypatch, tmp_path
) -> None:
    """The reproduction: anchors and candidate, cold cache, beside a CPU-bound thread.

    In-process this workload expired the budget (measured 1.7 s for the rebuild alone
    beside one hog, 3.5 s for one six-component path beside 48); the child pays one GIL
    handoff for the whole answer.  The bound is coarse on purpose -- a loaded CI host
    must not red it -- and the real assertions are the two that cannot flake: a healthy
    file is not refused, and no prefix is charged.
    """
    # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that drops the autouse fixture's scaled-down budgets so the reproduction runs on the shipped ones, moved verbatim
    monkeypatch.undo()
    security._path_resolve_degraded.clear()
    security._home_targets_cache.clear()
    target = tmp_path / "ws" / "README.md"
    target.parent.mkdir()
    target.write_text("x")
    with _armed():
        assert security._resolved_spellings("/")  # the one-time child spawn is not the workload
    stop = threading.Event()
    hog = threading.Thread(target=_hog_the_gil, args=(stop,), daemon=True)
    hog.start()
    try:
        # flake-ok: pre-existing 50 ms head start for the GIL hog, moved verbatim; the assertions that cannot flake (no refusal, no charged prefix) do not depend on it
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing head start for the hog thread, moved verbatim; tracked for a de-flake pass
        time.sleep(0.05)
        started = time.monotonic()
        refusal = security.sensitive_path_refusal(str(target))
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        # flake-ok: the hog's loop checks the stop event set on the line above on every pass, so the join returns
        hog.join()
        security._home_targets_cache.clear()
    assert refusal is None, f"a healthy project file must not be refused: {refusal!r}"
    if os.name != "nt":  # ~0.05 s alone; headroom for a loaded CI host
        # flake-ok: a coarse ceiling (about 30 times the unloaded time) the docstring explains, skipped on Windows; pre-existing, moved verbatim
        assert elapsed < 1.5, f"resolution took {elapsed:.2f}s under one GIL hog"
    assert security._path_resolve_degraded == {}, "no prefix may be charged for a healthy disk"


def test_the_resolution_runs_on_the_calling_thread(monkeypatch) -> None:
    """The measured property the wiring rests on: the thread that wants the answer
    is the thread that does the round trip -- never a pool worker's future."""
    seen: list[int] = []

    def _recording(expanded: str) -> set[str]:
        seen.append(threading.get_ident())
        return {expanded}

    monkeypatch.setattr(security, "_resolved_spellings", _recording)
    security._candidate_forms("/home/someone/ws/file")
    assert seen == [threading.get_ident()]


@pytest.mark.parametrize("half", ["candidate", "anchors"])
def test_a_child_fault_fails_closed_without_a_cooldown(monkeypatch, half) -> None:
    """A dead or out-of-frame child is NOT "resolved to nothing".

    An empty answer on the candidate half would leave a workspace symlink into a
    credential store matched on its lexical spelling; on the anchor half it would
    rebuild the target set from lexical roots.  Both refuse, and neither charges the
    prefix: the disk did not stall.
    """

    def _faulting(op: int, payload: bytes) -> bytes:
        raise SubprocessPoolUnavailable("child closed the connection")

    monkeypatch.setattr(security.paths, "_child_request", _faulting)
    security._home_targets_cache.clear()
    try:
        if half == "candidate":
            with pytest.raises(security.PathResolutionStalled):
                security._candidate_forms("/home/someone/ws/file")
            assert security.is_sensitive_path("/home/someone/ws/file") is True
        else:
            with pytest.raises(security.PathResolutionStalled):
                security._resolved_root_key()
            assert security.is_sensitive_path("/tmp/anything") is True
    finally:
        security._home_targets_cache.clear()
    assert security._path_resolve_degraded == {}, "a child fault is not a stalled mount"


def test_a_run_of_child_faults_is_warned_about_once_and_degrades_in_process(
    monkeypatch, caplog
) -> None:
    """Children that spawn but keep dying must not take every gate down with them.

    One fault is routine (killed and respawned, debug level) and is a refusal.  A run
    of them is a host where every ``is_sensitive_path`` call would refuse fail-closed
    -- an outage as total as a stalled mount -- so at the threshold the run is warned
    about once and resolution degrades to the bounded in-process fallback (the answer
    is the in-process one, never empty); a healthy child answer resets the streak so a
    later run warns again.  A child fault never charges a prefix.
    """

    class _Flaky:
        healthy = False

        def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
            if self.healthy:
                return security.paths.pack_strings([os.fsencode("/")])
            raise SubprocessPoolUnavailable("child closed the connection")

        def submit(self, fn, /, *args, **kwargs):  # the fallback's bounded in-process arm
            return ex.ThreadPoolExecutor(max_workers=1).submit(fn, *args, **kwargs)

    executor = _Flaky()
    monkeypatch.setattr(security.paths, "path_resolve_executor", lambda: executor)
    monkeypatch.setattr(security.paths, "_child_fault_streak", 0)
    monkeypatch.setattr(security.paths, "_child_fault_warned", False)
    threshold = security.paths._CHILD_FAULT_WARN_STREAK

    def _warnings() -> list[logging.LogRecord]:
        return [
            r for r in caplog.records if "faulted" in r.message and r.levelno >= logging.WARNING
        ]

    with _armed(), caplog.at_level(logging.DEBUG, logger="kiro_crew.security.paths"):
        for _ in range(threshold - 1):
            with pytest.raises(security.PathResolutionStalled):
                security._resolved_spellings("/home/someone/ws/file")
        assert _warnings() == [], "below the threshold a fault is a debug-level refusal"
        for _ in range(3):
            # At and past the threshold: the in-process answer, not a refusal.
            assert security._resolved_spellings("/") == security.paths._resolved_spellings_inline(
                "/"
            )
        assert len(_warnings()) == 1, "the run is announced once, not per fault"
        assert f"{threshold} times in a row" in _warnings()[0].message
        assert "in-process" in _warnings()[0].message

        executor.healthy = True
        assert security._resolved_spellings("/") == {"/"}
        assert security.paths._child_fault_streak == 0
        executor.healthy = False
        for _ in range(threshold - 1):
            with pytest.raises(security.PathResolutionStalled):
                security.paths._realpaths_or_none(["/home/someone"])
        assert security.paths._realpaths_or_none(["/"]) == [os.path.realpath("/")]
        assert len(_warnings()) == 2, "a new run after a healthy answer warns again"
    assert security._path_resolve_degraded == {}, "a child fault is not a stalled mount"


def test_a_child_that_cannot_start_falls_back_in_process_once(monkeypatch, caplog) -> None:
    """No interpreter to spawn (a hardened host, a broken venv): today's behaviour,
    said once.  A refusal here would take the whole gate down with the child."""

    class _Unspawnable:
        def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
            raise FileNotFoundError("python")

        def submit(self, fn, /, *args, **kwargs):  # the fallback's bounded in-process arm
            return ex.ThreadPoolExecutor(max_workers=1).submit(fn, *args, **kwargs)

    monkeypatch.setattr(security.paths, "path_resolve_executor", lambda: _Unspawnable())
    monkeypatch.setattr(security.paths, "_child_fallback_warned", False)
    with _armed(), caplog.at_level(logging.WARNING, logger="kiro_crew.security.paths"):
        for _ in range(3):
            assert security._resolved_spellings("/") == security.paths._resolved_spellings_inline(
                "/"
            )
        assert security.paths._realpaths_or_none(["/", "\0"]) == [
            os.path.realpath("/"),
            security.paths._realpath_inline("\0"),
        ]
    warnings = [r for r in caplog.records if "could not be started" in r.message]
    assert len(warnings) == 1, "the fallback is announced once, not per resolution"
    assert security._path_resolve_degraded == {}


def test_outside_a_bounded_call_the_resolver_never_asks_the_pool(monkeypatch, tmp_path) -> None:
    """The inline entry point's contract: NO pool submission on either half.

    ``is_sensitive_resolved_path`` runs on worker threads (a skill walk) with no
    deadline armed.  Were its anchor rebuild to go through the child it would hold a
    child for up to the pool's ceiling and queue AHEAD of the event loop's bounded
    requests -- eight walkers could pin both children and every loop-side gate call
    would then refuse "no free child", uncharged, forever.  So with
    no deadline armed the resolver answers in this interpreter and the executor is
    never even constructed.
    """

    def _never() -> None:
        raise AssertionError("the pool was asked outside a bounded call")

    monkeypatch.setattr(security.paths, "path_resolve_executor", _never)
    security._home_targets_cache.clear()
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    try:
        assert security.paths._outside_bounded_call()
        assert security._resolved_spellings(str(link)) == security.paths._resolved_spellings_inline(
            str(link)
        )
        # The second entry raises on POSIX (embedded NUL -> ``None``) but Windows's
        # ``realpath`` falls back to the lexical spelling for an unresolvable name, so
        # the expectation is the inline mirror's own answer; the assertion that matters
        # is ``_never`` above -- neither entry reached the pool.
        assert security.paths._realpaths_or_none([str(link), "\0"]) == [
            os.path.realpath(str(link)),
            security.paths._realpath_inline("\0"),
        ]
        assert security.is_sensitive_resolved_path(os.path.realpath(str(tmp_path / "f"))) is False
        assert (
            security.is_sensitive_resolved_path(
                os.path.realpath(os.path.expanduser("~/.aws/credentials"))
            )
            is True
        )
    finally:
        security._home_targets_cache.clear()
    assert security._path_resolve_degraded == {}


def test_the_in_process_fallback_is_still_bounded(monkeypatch) -> None:
    """A host where the child cannot start does not get the original stall back: the
    in-process ``realpath`` runs on the pool's thread and the caller waits on it
    with the shared deadline, refusing fail-closed (and uncharged) when it misses."""
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    threads = ThreadPoolExecutor(max_workers=2)

    class _Unspawnable:
        def call_op(self, op: int, payload: bytes, timeout: float | None = None) -> bytes:
            raise FileNotFoundError("python")

        def submit(self, fn, /, *args, **kwargs):
            return threads.submit(fn, *args, **kwargs)

    def _wedged(expanded: str) -> set[str]:
        release.wait(30.0)
        return {expanded}

    monkeypatch.setattr(security.paths, "path_resolve_executor", lambda: _Unspawnable())
    monkeypatch.setattr(security.paths, "_child_fallback_warned", True)
    monkeypatch.setattr(security.paths, "_resolved_spellings_inline", _wedged)
    monkeypatch.setattr(security.paths, "_PATH_RESOLVE_TIMEOUT_SECS", 0.2)
    try:
        started = time.monotonic()
        with pytest.raises(security.PathResolutionStalled):
            security.paths._resolved_forms_bounded("/home/someone/ws/file")
        # flake-ok: a 5 s ceiling over a 0.2 s budget, checked after the refusal itself; pre-existing, moved verbatim
        assert time.monotonic() - started < 5.0
        assert security._path_resolve_degraded == {}, "a wedged fallback thread is not classified"
    finally:
        release.set()
        threads.shutdown(wait=True)


def test_an_empty_spelling_never_reaches_the_resolver(monkeypatch) -> None:
    """Both matchers short-circuit on an empty path (it can match nothing), so the
    shared resolution in front of them must too: otherwise an empty tool title,
    arriving while a real mount stall has a cooldown open, would be refused as an
    "unverifiable path" -- a refusal of an unrelated tool with no path in it."""
    security._home_targets_cache.clear()
    stalled = _StalledResolver()
    monkeypatch.setattr(security, "_resolved_spellings", stalled)
    try:
        assert security.sensitive_path_refusal("") is None
        assert security.sensitive_path_refusal("", "/some/base") is None
        assert security.is_sensitive_path("") is False
        assert security.is_sensitive_write_path("") is False
        assert stalled.calls == [], "an empty spelling was sent to the resolver"
    finally:
        security._home_targets_cache.clear()
