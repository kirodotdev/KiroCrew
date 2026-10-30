"""Probe whether the host can build the OS sandbox.

The user-namespace probe (forked or spawned child, its handshake and failure reasons),
the remedy tokens, WSL / container / Seatbelt detection, and whether this process
already runs inside a sandbox. One of the owners ``kiro_crew.sandbox`` is carved into:
every name here stays readable and patchable as ``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import functools
import logging
import os
import select
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew.sandbox_tiers import _IN_SANDBOX_MARKER

if TYPE_CHECKING:
    from collections.abc import Callable

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


# ── Availability probes ──


# unshare(2) flags for the userns probe.
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWNS = 0x00020000
# mount(2) flags for the probe's propagation step: the launcher's first mount is
# ``mount(NULL, "/", NULL, MS_REC|MS_PRIVATE, NULL)`` inside the fresh mount
# namespace, so the probe performs exactly that call and nothing else.
_PROBE_MS_REC = 0x4000
_PROBE_MS_PRIVATE = 1 << 18

# Errnos that indicate a TRANSIENT resource failure (fork/CDLL under momentary
# pressure) — the kernel supports user namespaces, we just couldn't verify it
# right now. These must never be treated as "this host has no sandbox backend"
# (one EAGAIN during a cron spawn burst would otherwise fail-close every
# subsequent spawn because the failed probe result was cached).
_TRANSIENT_PROBE_ERRNOS = frozenset(
    {errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC}
)

# Delay before the single in-probe retry on a transient failure.
_PROBE_TRANSIENT_RETRY_DELAY_SECS = 0.05

# Ceiling on how long a blocking ``warm_backend`` waits for the probe to land.
# The probe itself is a fork + unshare (sub-millisecond) and the warm thread
# makes at most two attempts separated by one _PROBE_TRANSIENT_RETRY_DELAY_SECS
# sleep, so this is orders of magnitude of slack rather than a tuned value — it
# exists so a wedged probe cannot stall boot indefinitely. Exceeding it is not
# an error: the cache stays cold and the self-healing transient path applies.
_WARM_JOIN_TIMEOUT_SECS = 2.0

# Steps of the launcher's namespace handshake, named in probe failure reasons so
# a caller can tell the host mechanisms apart instead of seeing a bare errno: a
# NEWNS denial is Ubuntu's AppArmor userns restriction, while NEWUSER with
# ENOSPC/EUSERS is a hardened user.max_user_namespaces=0. ENOSPC is ALSO what
# momentary fd/disk pressure looks like, so the cap verdict stays TRANSIENT and
# is never cached; the remedy travels with it so a host at a cap of 0 — which is
# reported transient forever — still gets told which sysctl to raise.
#
# The third step is the launcher's FIRST mount: making propagation on ``/``
# private inside the new mount namespace. Both unshare calls can succeed while
# that mount is refused — a container whose runtime applies its default AppArmor
# profile (``deny mount``) but no seccomp filter, the Kubernetes default on an
# AppArmor node — and a probe that stopped at the unshares reported such a host
# sandbox-capable, so every real spawn then died in the launcher with
# ``sandbox: BLOCKED -- making mount propagation private on / failed: errno 13``
# and was misread as a broken CLI. The probe therefore performs exactly that
# mount, in a namespace that dies with the probe child, and nothing more.
_PROBE_STEP_NEWUSER = "unshare(CLONE_NEWUSER)"
_PROBE_STEP_NEWNS = "unshare(CLONE_NEWNS)"
_PROBE_STEP_MOUNT_PRIVATE = "mount(MS_REC|MS_PRIVATE) on /"
#: Wire step the probe child sends INSTEAD of "U" when its ``CLONE_NEWUSER`` EINVAL
#: is explained by the child having been multithreaded, carrying the thread count.
#: The parent classifies it exactly as it classifies a plain EINVAL -- the step
#: exists to carry the explanation, not to change the verdict. See
#: ``_probe_child_thread_count``.
_PROBE_STEP_MULTITHREADED = "M"

#: Trailing clause of the reason `_probe_parent_sequence` emits for the
#: `_PROBE_STEP_MULTITHREADED` collapse. A single shared spelling, because callers
#: that must RECOGNIZE the collapse (a fork child whose verdict is unobtainable is
#: an unknown reading, not a disagreement) match against the reason text -- a
#: hand-copied substring would drift the moment the wording changes.
_PROBE_MULTITHREADED_REASON = (
    "an os.register_at_fork hook started one, so the kernel's own verdict is "
    "unobtainable from this child"
)


def _probe_reason_is_multithreaded_collapse(reason: str) -> bool:
    """Whether a probe reason reports the multithreaded-fork-child collapse.

    True only for the fork path: the collapse text is emitted by
    `_probe_parent_sequence` when the probe child counted more than one thread,
    which happens when an ``os.register_at_fork`` hook armed earlier in the
    calling process starts a thread inside every fork child. The verdict such a
    child returns is the hook's artifact, not the kernel's answer.
    """
    return _PROBE_MULTITHREADED_REASON in (reason or "")


# A probe child that vanished mid-handshake is a harness failure, not a kernel
# verdict, so it must not be cached as "this host has no sandbox". Kept separate
# from _TRANSIENT_PROBE_ERRNOS so that set's cache semantics stay untouched.
# ESRCH/ENOENT surface when opening /proc/<pid>/... for a dead child; EPIPE
# surfaces when releasing a child that died after the maps were written.
_PROBE_CHILD_GONE_ERRNOS = frozenset({errno.ESRCH, errno.ENOENT, errno.EPIPE})

# Upper bound on the probe's pipe handshake. The real exchange is
# sub-millisecond; this only stops a pathological child from wedging the
# background warm thread forever.
_PROBE_HANDSHAKE_TIMEOUT_SECS = 5.0

# Upper bound on the macOS sandbox-backend probe (`sandbox-exec` running
# /usr/bin/true under an allow-default profile) that backend detection runs.
_SANDBOX_BACKEND_PROBE_TIMEOUT_SECS: float = 5.0

# ── Remedy tokens for a Linux user-namespace denial ──
# The probe already knows WHICH step failed and with which errno, and those two
# facts identify the host mechanism (see the table in docs/guides/install.md).
# A reason string alone would leave every presentation layer showing a bare
# ``errno 1 (EPERM)`` and no way forward.
# These tokens carry the mechanism out to callers machine-readably, so the
# dashboard, doctor and logs can each render their own remedy copy instead of
# pattern-matching English prose out of the detail.
#
# A token travels IN-BAND: every probe step returns it alongside its verdict, so
# it is never module state that a second probe could overwrite. ``""`` means the
# failure identifies no mechanism — a harness failure, a non-Linux host, or a
# deferred on-loop probe.
REMEDY_APPARMOR_USERNS = "apparmor_userns"  # Ubuntu >= 23.10 restricted profile
REMEDY_MAX_USER_NAMESPACES = "max_user_namespaces"  # user.max_user_namespaces=0
REMEDY_NO_USER_NS = "no_user_ns"  # kernel built without CONFIG_USER_NS
REMEDY_USERNS_DENIED = "userns_denied"  # userns creation refused outright
REMEDY_MOUNT_DENIED = "mount_denied"  # namespaces work; mount(2) refused inside them


def _remedy_for_step(label: str, err: int) -> str:
    """Name the host mechanism behind one failed probe step.

    ``label`` is one of the ``_PROBE_STEP_*`` constants for a real kernel
    verdict; any other label is a harness failure (fork/pipe under pressure)
    which says nothing about the host and therefore has no remedy.

    A NEWNS denial is only reachable AFTER NEWUSER succeeded, which is the
    signature of Ubuntu's restricted-profile restriction rather than of userns
    being unavailable — the distinction that decides whether the fix is an
    AppArmor profile or a sysctl.

    A refused propagation mount is only reachable after BOTH unshares succeeded:
    the process holds every capability inside its new user namespace, so the
    kernel itself never refuses this call — a policy layer did. EACCES is
    AppArmor's answer (the container runtimes' default profile carries ``deny
    mount``); EPERM is a seccomp filter's. Both are permanent for the life of
    the container and share one remedy: relax that policy, or accept the
    container as the only isolation boundary.
    """
    if label == _PROBE_STEP_MOUNT_PRIVATE:
        return REMEDY_MOUNT_DENIED if err in (errno.EACCES, errno.EPERM) else ""
    if label == _PROBE_STEP_NEWNS:
        return REMEDY_APPARMOR_USERNS if err == errno.EPERM else ""
    if label != _PROBE_STEP_NEWUSER:
        return ""
    if err in (errno.ENOSPC, errno.EUSERS):
        return REMEDY_MAX_USER_NAMESPACES
    if err in (errno.EINVAL, errno.ENOSYS):
        return REMEDY_NO_USER_NS
    if err == errno.EPERM:
        return REMEDY_USERNS_DENIED
    return ""


# Concrete, mechanism-specific first line for the ``no_backend`` guidance in a
# SandboxUnavailableError message. Kept as prose here (rather than only as a
# token) because logs, doctor and the Slack surface all read the message text —
# only the dashboard consumes the token and renders its own translated copy.
_LINUX_REMEDY_GUIDANCE = {
    REMEDY_APPARMOR_USERNS: (
        "This host looks like Ubuntu 23.10 or newer with "
        "kernel.apparmor_restrict_unprivileged_userns=1: the user namespace was "
        "created, then the mount namespace was denied because the restricted "
        "AppArmor profile carries no CAP_SYS_ADMIN. Run `kirocrew service "
        "install` to install the narrow kirocrew-userns AppArmor profile (it "
        "grants only `userns`). The profile is attached by path to the kirocrew "
        "launcher, so any launch through that launcher is confined, a foreground "
        "`kirocrew gateway` included; other entry points stay unconfined, and "
        "`aa-exec -p` cannot fix that for an unprivileged user because entering "
        "a named profile needs privilege and aa-exec execs unconfined rather "
        "than failing. The desktop app reuses a gateway already listening on the "
        "port, so installing the service covers that install too. Do NOT set the "
        "sysctl to 0 — that removes a kernel-wide protection to satisfy one app. "
    ),
    REMEDY_MAX_USER_NAMESPACES: (
        "User namespace creation hit the per-user cap, which usually means "
        "user.max_user_namespaces=0 (a CIS-hardened default). Raise that sysctl. "
    ),
    REMEDY_NO_USER_NS: (
        "The kernel rejected the user namespace outright, which means it was "
        "built without CONFIG_USER_NS. There is no host-level fix short of a "
        "different kernel. "
    ),
    REMEDY_USERNS_DENIED: (
        "User namespace creation was refused. On Debian-family hosts check "
        "kernel.unprivileged_userns_clone (it must be 1); inside a container "
        "this is usually the container's own seccomp filter denying unshare, "
        "which is fixed with container run flags rather than host config. "
    ),
    REMEDY_MOUNT_DENIED: (
        "The user and mount namespaces were created, but making mount "
        "propagation private on / inside them was refused, so the sandbox "
        "cannot hide anything. Nothing in the kernel refuses this to a process "
        "that owns the namespace: a policy layer did. Inside a container that is "
        "the runtime's default AppArmor profile (it carries `deny mount`; "
        "EACCES) or a seccomp filter without the mount family (EPERM). Run the "
        "container with AppArmor unconfined (docker: --security-opt "
        "apparmor=unconfined; Kubernetes 1.30+: securityContext.appArmorProfile.type "
        "Unconfined, earlier versions the "
        "container.apparmor.security.beta.kubernetes.io/<container>: unconfined "
        "annotation) and a seccomp profile that permits unshare and mount — no "
        "root or CAP_SYS_ADMIN is needed — or accept the container as the only "
        "isolation boundary with agent.sandbox_allow_unsandboxed_exec=true. "
    ),
}


def _linux_remedy_guidance(remedy: str) -> str:
    """Mechanism-specific guidance prefix for a remedy token (``""`` if none)."""
    return _LINUX_REMEDY_GUIDANCE.get(remedy, "")


def _close_probe_fds(*fds: int) -> None:
    """Close probe pipe fds, tolerating an already-closed one. Never raises.

    ``os.close`` blocks, but every probe path runs off the event loop
    (``_probe_unshare`` defers to the background warm thread when a loop is
    running), so this does not breach the no-blocking-call-on-event-loop rule.
    """
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


_PROBE_CHILD_FD_SWEEP_CAP = 4096
"""Fallback bound for the probe child's inherited-fd close sweep.

Used only when ``SC_OPEN_MAX`` cannot be read or answers nonsense. When
sysconf answers, its value (the soft ``RLIMIT_NOFILE``) is trusted as the
bound: ``os.closerange`` delegates to ``close_range(2)`` on Linux >= 5.9, so
a wide span costs one syscall rather than a walk, and silently clamping the
bound would leave a high-numbered lock fd open with no diagnostic that the
sweep came up short.
"""


def _fd_sweep_ranges(keep: frozenset[int], limit: int | None = None) -> tuple[tuple[int, int], ...]:
    """Precompute the ``os.closerange`` spans covering ``[0, bound)`` minus *keep*.

    Runs in the PARENT, before ``os.fork()``. The probe child of a threaded
    process must not allocate or take locks — another thread may own the
    allocator lock at fork time and vanish, leaving it held forever in the
    child — so everything that sorts, boxes, or asks ``sysconf`` happens here,
    and the child is left executing bare ``closerange`` syscalls over the
    returned pairs (:func:`_close_fd_ranges`).

    The bound is ``SC_OPEN_MAX`` (the soft ``RLIMIT_NOFILE``);
    :data:`_PROBE_CHILD_FD_SWEEP_CAP` applies only when sysconf cannot answer.
    ``limit`` exists for tests. Never raises.
    """
    if limit is None:
        try:
            limit = int(os.sysconf("SC_OPEN_MAX"))
        except (AttributeError, OSError, ValueError):
            # AttributeError: os.sysconf does not exist off-POSIX (Windows);
            # the sweep only runs on Linux, but this helper must keep its
            # never-raises contract everywhere the tests exercise it.
            limit = _PROBE_CHILD_FD_SWEEP_CAP
    if limit <= 0:
        limit = _PROBE_CHILD_FD_SWEEP_CAP
    ranges: list[tuple[int, int]] = []
    low = 0
    for fd in sorted(k for k in keep if k >= 0):
        if fd >= limit:
            break
        if fd > low:
            ranges.append((low, fd))
        low = fd + 1
    if low < limit:
        ranges.append((low, limit))
    return tuple(ranges)


def _close_fd_ranges(ranges: tuple[tuple[int, int], ...]) -> None:
    """Close the precomputed fd spans: the probe child's half of the sweep.

    Runs between ``os.fork()`` and ``os._exit`` in a child that never execs,
    so ``O_CLOEXEC`` never fires and every inherited descriptor — the
    ``gateway.lock`` flock fd and the dashboard listen socket included — is
    still open. Without the sweep, a probe child orphaned by its parent's
    death (gateway OOM-killed between fork and reap) keeps the lock fd open
    and pins the data home until someone reclaims it.

    Only ``os.closerange`` is invoked here: the spans were computed pre-fork
    by :func:`_fd_sweep_ranges` precisely so this post-fork path does no
    allocation-bearing work beyond iterating a ready tuple. ``closerange``
    ignores bad fds, so this never raises.
    """
    for low, high in ranges:
        os.closerange(low, high)


def _probe_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Shape one failed probe step into ``(ok, transient, reason)``.

    EPERM stays PERMANENT: an AppArmor userns denial, or a kernel built without
    CONFIG_USER_NS, will not clear on a retry, and caching that verdict is what
    makes ``detect_backend()`` honest. Only the momentary-resource errnos are
    transient — widening that set is how one EAGAIN
    was cached as "this host has no sandbox" for an hour.

    Returns the step's remedy token IN-BAND with the verdict. Carrying it in a
    module global instead would let a second, concurrent probe interleave between
    one probe staging its token and the caller reading it, recording a reason with
    the wrong mechanism.
    """
    name = errno.errorcode.get(err, "?")
    return (
        False,
        err in _TRANSIENT_PROBE_ERRNOS,
        f"{label} failed with errno {err} ({name})",
        _remedy_for_step(label, err),
    )


def _probe_harness_failure(label: str, err: int) -> tuple[bool, bool, str, str]:
    """Classify a probe-scaffolding failure, treating a vanished child as transient.

    A child that dies mid-handshake reaches the parent as ESRCH/ENOENT on a
    ``/proc`` map write, or EPIPE on the write that releases it. None of those is
    a kernel verdict about user namespaces, so caching them permanently would
    strand every later spawn until restart.
    """
    if err in _PROBE_CHILD_GONE_ERRNOS:
        name = errno.errorcode.get(err, "?")
        return (False, True, f"{label} failed with errno {err} ({name})", "")
    return _probe_failure(label, err)


def _probe_child_unshare(libc: ctypes.CDLL, flags: int) -> int:
    """Run one ``unshare(2)`` in the probe child; return 0 or the errno.

    A module-level seam so a test can simulate the Ubuntu >= 23.10 shape
    ("NEWUSER ok, NEWNS EPERM") without needing a restricted kernel.
    """
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def _probe_child_make_private(libc: ctypes.CDLL) -> int:
    """Make ``/`` recursively private in the probe child's new mount namespace.

    Returns 0 or the errno, like :func:`_probe_child_unshare`, and is the same
    seam for tests: the container shape ("both unshares ok, mount EACCES") is
    simulated here rather than by running under a ``deny mount`` profile.

    Runs ONLY after ``unshare(CLONE_NEWNS)`` succeeded, so the propagation
    change is confined to a namespace no other process shares and that ends
    with the child. Nothing is bind-mounted and no path is hidden: this is the
    one mount the launcher performs before any of that, and the one a
    container's policy refuses first.
    """
    ctypes.set_errno(0)
    if libc.mount(None, b"/", None, _PROBE_MS_REC | _PROBE_MS_PRIVATE, None) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def _probe_bind_libc(libc: ctypes.CDLL) -> None:
    """Declare the two libc calls the probe child makes, for either probe path."""
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    libc.mount.restype = ctypes.c_int


def _probe_child_thread_count() -> int:
    """Live threads in the probe child, or 0 when it cannot be determined.

    ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD``, which the kernel refuses
    with **EINVAL** unless the caller's thread group holds exactly one task. A
    ``fork()`` child is single-threaded by construction, so this normally reads 1 --
    but ``os.register_at_fork`` handlers run INSIDE ``os.fork()``, before it
    returns, and a library can start a thread there. OpenTelemetry's metric SDK does
    exactly that: its ``PeriodicExportingMetricReader`` registers an
    ``after_in_child`` hook that restarts its exporter thread in every child.

    Used ONLY to explain an EINVAL, never to reclassify one. EINVAL is genuinely
    ambiguous here -- a kernel built without ``CONFIG_USER_NS`` returns it too, and a
    multithreaded child cannot tell the two apart, because it never gets far enough to
    ask. Calling it transient would be just as wrong as calling it permanent, and it
    would additionally withhold the ``no_backend`` opt-in (``sandbox_allow_unsandboxed_exec``)
    from a host that really has no user namespaces. So the classification stays exactly
    as it was and the REASON names the thread, which is the part a reader cannot infer:
    a bare "errno 22 (EINVAL)" sends them to check their kernel config, which is the
    wrong place. Making such a process probe successfully needs a single-threaded
    child, i.e. a different spawn mechanism, and that is its own change.

    ``st_nlink`` of ``/proc/self/task`` is ``2 + threads`` (each thread is a
    subdirectory), so this is one ``stat`` and no list: the probe child of a threaded
    process must not allocate, because another thread may have owned the allocator lock
    at fork time and does not exist in the child to release it. Linux-only, like the
    rest of the probe.
    """
    try:
        return max(0, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 0


def _probe_write_identity_maps(pid: int, uid: int, gid: int) -> tuple[str, int] | None:
    """Write the probe child's identity maps, exactly as the launcher's parent does.

    Returns ``None`` on success, else ``(label, errno)`` for the first
    ``/proc/<pid>/`` file that could not be written. A child that died between
    the fork and this write surfaces here as ESRCH/ENOENT instead of raising.
    """
    for name, payload in (
        ("setgroups", "deny"),
        ("uid_map", f"{uid} {uid} 1\n"),
        ("gid_map", f"{gid} {gid} 1\n"),
    ):
        try:
            with open(f"/proc/{pid}/{name}", "w") as handle:
                handle.write(payload)
        except OSError as exc:
            return (f"/proc/<pid>/{name} write", exc.errno or 0)
    return None


def _probe_read_step(fd: int) -> tuple[str, int] | None:
    """Read one ``<step>:<errno>`` report from the probe child.

    ``None`` means the child closed the pipe without reporting, sent junk, or
    stayed silent past the handshake deadline — the deadline being what stops a
    pathological child from wedging the background warm thread forever.

    Uses ``poll`` rather than ``select``: ``select`` raises ``ValueError`` once a
    descriptor reaches FD_SETSIZE (1024), and a long-lived gateway can easily
    hand the probe a pipe fd past that. Raising there would kill the warm thread
    and leave ``wrap_argv`` rejecting every sandboxed spawn.
    """
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    deadline = time.monotonic() + _PROBE_HANDSHAKE_TIMEOUT_SECS
    buf = b""
    try:
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                if not poller.poll(max(1, int(remaining * 1000))):
                    return None
                chunk = os.read(fd, 32)
            except OSError:
                return None
            if not chunk:
                return None  # writer closed (POLLHUP) without a full report
            buf += chunk
    finally:
        try:
            poller.unregister(fd)
        except (KeyError, OSError):
            pass
    step, _, value = buf.split(b"\n", 1)[0].decode("ascii", "replace").partition(":")
    try:
        return (step, int(value))
    except ValueError:
        return None


def _probe_child_death(pid: int) -> str:
    """Describe how a silent probe child ended, for the failure reason.

    A child killed by a signal (the OOM killer, a stray SIGKILL) is a momentary
    environmental failure rather than a kernel verdict, so naming the signal
    preserves the diagnostic the child's exit status carries instead of reporting a
    bare failure. Non-blocking, so a child wedged past the handshake
    deadline is described instead of waited on.
    """
    try:
        reaped, status = os.waitpid(pid, os.WNOHANG)
    except OSError:
        return "exited without reporting"
    if reaped != pid:
        return f"stayed silent for {_PROBE_HANDSHAKE_TIMEOUT_SECS:g}s"
    if os.WIFSIGNALED(status):
        return f"killed by signal {os.WTERMSIG(status)}"
    if os.WIFEXITED(status):
        return f"exited with status {os.WEXITSTATUS(status)}"
    return "exited without reporting"


def _probe_reap(pid: int) -> None:
    """Reap the probe child on every exit path so no zombie or stuck child leaks.

    Reaps a child that already exited without signalling it — the common case,
    and the one that must never send SIGKILL at a pid the kernel could have
    recycled. Only a child still running after the handshake ended (it cannot
    make progress: its pipes are closed) is killed, which bounds the reap
    without spinning. ``platform_compat.kill_pid`` deliberately propagates
    ``ProcessLookupError``, so an exit in that race is caught here.
    """
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return
    except OSError:
        return  # not our child, or already reaped
    try:
        platform_compat.kill_pid(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def _probe_child_sequence(
    libc: ctypes.CDLL,
    c2p_r: int,
    c2p_w: int,
    p2c_r: int,
    p2c_w: int,
    sweep_ranges: tuple[tuple[int, int], ...],
) -> None:
    """Probe child: run the launcher's namespace handshake, reporting each step.

    Three steps, in the launcher's order: ``unshare(CLONE_NEWUSER)``, then —
    once the parent has written the identity maps — ``unshare(CLONE_NEWNS)``,
    then — once the parent has read that verdict — the propagation mount on
    ``/``. Each step is one line on the pipe and the child waits for the parent
    between them: a report and its successor written back to back could land in
    the same read and the second would be lost.

    Never returns. It reports raw errnos and classifies nothing, so the entire
    verdict lives in the parent where a test can drive it without forking.
    ``sweep_ranges`` was computed pre-fork by :func:`_fd_sweep_ranges` so this
    path performs no allocation-bearing bookkeeping of its own.
    """
    try:
        _close_probe_fds(c2p_r, p2c_w)
        # Drop every other inherited descriptor before touching namespaces:
        # an orphaned probe child must not keep the gateway.lock fd (or the
        # dashboard listen socket) open and pin the home. Only the handshake
        # ends and the standard streams survive.
        _close_fd_ranges(sweep_ranges)
        # Read BEFORE the unshare: it is the only moment the count is the one the
        # kernel judged. Reported only alongside an EINVAL, and only to explain it --
        # see _probe_child_thread_count for why it must not change the verdict.
        threads = _probe_child_thread_count()
        err = _probe_child_unshare(libc, _CLONE_NEWUSER)
        if err == errno.EINVAL and threads > 1:
            os.write(c2p_w, b"M:%d\n" % threads)
            os._exit(0)
        os.write(c2p_w, b"U:%d\n" % err)
        if err:
            os._exit(0)
        # NEWNS needs a mapped UID, so wait for the parent's maps first. This
        # ordering is the entire point of the probe.
        if not os.read(p2c_r, 1):
            os._exit(0)  # parent abandoned the handshake; it already has a verdict
        err = _probe_child_unshare(libc, _CLONE_NEWNS)
        os.write(c2p_w, b"N:%d\n" % err)
        if err:
            os._exit(0)
        if not os.read(p2c_r, 1):
            os._exit(0)
        os.write(c2p_w, b"P:%d\n" % _probe_child_make_private(libc))
        os._exit(0)
    except BaseException:
        os._exit(1)


def _probe_parent_sequence(
    pid: int,
    c2p_r: int,
    p2c_w: int,
    uid: int,
    gid: int,
    death: Callable[[int], str] = _probe_child_death,
) -> tuple[bool, bool, str, str]:
    """Parent half of the probe: drive the handshake and decide the verdict.

    ``death`` describes a child that stopped reporting. It is injected because the
    spawned probe's child is owned by a ``Popen`` -- calling ``waitpid`` on it here
    would race that object's own bookkeeping -- while the forked probe's child is
    reaped by this module. The verdict logic is identical for both.
    """
    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWUSER} result", "")
    step, err = report
    if step == _PROBE_STEP_MULTITHREADED:
        # Same classification and same remedy as a plain EINVAL -- deliberately, see
        # _probe_child_thread_count. Only the reason gains the thread count, because
        # that is the one part a reader cannot infer from the errno.
        ok, transient, reason, remedy = _probe_failure(_PROBE_STEP_NEWUSER, errno.EINVAL)
        return (
            ok,
            transient,
            f"{reason}; the probe child had {err} threads, which alone makes it "
            "return EINVAL (CLONE_NEWUSER implies CLONE_THREAD) -- "
            f"{_PROBE_MULTITHREADED_REASON}",
            remedy,
        )
    if step != "U":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWUSER, err)

    failed_map = _probe_write_identity_maps(pid, uid, gid)
    if failed_map is not None:
        label, map_errno = failed_map
        return _probe_harness_failure(label, map_errno)

    try:
        os.write(p2c_w, b"x")
    except OSError as exc:
        return _probe_harness_failure("probe handshake write", exc.errno or 0)

    report = _probe_read_step(c2p_r)
    if report is None:
        return (False, True, f"probe child {death(pid)}; no {_PROBE_STEP_NEWNS} result", "")
    step, err = report
    if step != "N":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_NEWNS, err)

    # Release the child for the propagation mount only after its NEWNS report
    # has been read: the two verdicts share one pipe and must not share a read.
    try:
        os.write(p2c_w, b"m")
    except OSError as exc:
        return _probe_harness_failure("probe handshake write", exc.errno or 0)

    report = _probe_read_step(c2p_r)
    if report is None:
        return (
            False,
            True,
            f"probe child {death(pid)}; no {_PROBE_STEP_MOUNT_PRIVATE} result",
            "",
        )
    step, err = report
    if step != "P":
        return (False, True, f"probe child sent unexpected step {step!r}", "")
    if err:
        return _probe_failure(_PROBE_STEP_MOUNT_PRIVATE, err)
    return (True, False, "ok", "")


def _probe_unshare_once() -> tuple[bool, bool, str, str]:
    """One launcher-shaped namespace probe: ``(ok, transient, reason)``.

    Runs in a FRESH interpreter when one can be spawned, and falls back to
    :func:`_probe_unshare_via_fork` otherwise. The two produce the same verdict
    tuple through the same classifier; only the process the child half runs in
    differs. See :data:`_PROBE_SHIM_CODE` for why that difference is the whole
    point.
    """
    spawned = _probe_unshare_via_spawn()
    if spawned is not None:
        return spawned
    return _probe_unshare_via_fork()


#: Child half of the probe, run in a FRESH interpreter rather than a fork of the
#: caller. Same wire protocol as :func:`_probe_child_sequence`, so the reviewed
#: parent half drives either one unchanged.
#:
#: WHY A FRESH PROCESS. ``unshare(CLONE_NEWUSER)`` implies ``CLONE_THREAD`` and the
#: kernel refuses it with EINVAL unless the caller's thread group holds exactly one
#: task. A fork child inherits that condition: ``os.register_at_fork`` handlers run
#: INSIDE ``os.fork()`` before it returns, so a dependency that restarts a thread in
#: every child -- OpenTelemetry's ``PeriodicExportingMetricReader`` does exactly
#: this -- makes the child multithreaded before the probe can measure anything. The
#: verdict is then EINVAL, which is classified permanent and cached, and every later
#: sandboxed spawn on that process fails closed. A release gate lost 40 tests to one
#: such probe: all of them pass alone, none of them is a metrics test.
#:
#: A fresh interpreter starts single-threaded and runs no after-in-child fork hooks,
#: so its thread count at ``unshare()`` time is 1 regardless of the caller. This does
#: NOT soften the fail-closed rule: a genuinely single-threaded process that still
#: gets EINVAL means the host lacks ``CONFIG_USER_NS``, which stays permanent. It
#: removes the FALSE EINVAL, not the real one.
#:
#: It is also the more faithful probe. The real launcher is already a fresh
#: interpreter -- ``wrap_argv`` returns ``[sys.executable, launcher_path, ...]`` --
#: which then forks and unshares. So the fork-based probe was strictly MORE
#: pessimistic than the spawn it predicts, and this makes the two agree.
#:
#: Kept deliberately free of ``kiro_crew`` imports and run under ``-I -S``, like
#: ``_SPAWN_SHIM_CODE``: no site directory, no ``PYTHON*`` environment influence,
#: nothing to shadow. It writes ONLY wire steps on fd 1.
_PROBE_SHIM_CODE = r"""
import ctypes, errno, os

CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_REC = 0x4000
MS_PRIVATE = 1 << 18


def threads():
    # st_nlink of /proc/self/task is the thread count plus the two dir entries.
    try:
        return max(1, os.stat("/proc/self/task").st_nlink - 2)
    except OSError:
        return 1


def unshare(libc, flags):
    ctypes.set_errno(0)
    if libc.unshare(flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def make_private(libc):
    # The launcher's first mount, inside the namespace this child just created:
    # nothing is bind-mounted or hidden, and the namespace ends with the child.
    ctypes.set_errno(0)
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) == 0:
        return 0
    return ctypes.get_errno() or errno.EPERM


def main():
    try:
        # dlopen(NULL): resolve unshare() from the libc ALREADY loaded into this
        # interpreter. Never ctypes.util.find_library here -- on Linux it EXECUTES
        # helper processes (ldconfig, then a PATH-resolved gcc/cc/objdump on musl
        # hosts) to locate libc, and this probe runs before any confinement, so a
        # workspace-controlled `gcc` on PATH would be same-user code execution.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.unshare.argtypes = [ctypes.c_int]
        libc.unshare.restype = ctypes.c_int
        libc.mount.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p,
        ]
        libc.mount.restype = ctypes.c_int
    except BaseException:
        os._exit(1)
    n = threads()
    err = unshare(libc, CLONE_NEWUSER)
    if err == errno.EINVAL and n > 1:
        os.write(1, b"M:%d\n" % n)
        os._exit(0)
    os.write(1, b"U:%d\n" % err)
    if err:
        os._exit(0)
    if not os.read(0, 1):
        os._exit(0)
    err = unshare(libc, CLONE_NEWNS)
    os.write(1, b"N:%d\n" % err)
    if err:
        os._exit(0)
    if not os.read(0, 1):
        os._exit(0)
    os.write(1, b"P:%d\n" % make_private(libc))
    os._exit(0)


main()
"""

#: Ceiling on the spawned probe. The child does three syscalls and two blocking
#: reads whose writer is this process, so anything near this is a wedged
#: interpreter, not slow work. Exceeding it is reported TRANSIENT: a host that
#: cannot start a Python in 20 seconds is under momentary pressure, not
#: permanently sandbox-less.
_PROBE_SPAWN_TIMEOUT_SECONDS = 20.0

_probe_spawn_unavailable_logged = False


def _probe_spawned_death(proc: "subprocess.Popen[bytes]") -> str:
    """Describe how the spawned probe child ended, for a transient reason string."""
    code = proc.poll()
    if code is None:
        return "did not report"
    if code < 0:
        return f"was killed by signal {-code}"
    return f"exited with status {code}"


def _probe_unshare_via_spawn() -> tuple[bool, bool, str, str] | None:
    """Probe in a fresh interpreter. ``None`` means "cannot spawn, use the fork path".

    Returning ``None`` rather than a verdict is deliberate: an interpreter this
    process cannot start says nothing about the host's namespaces, so it must not
    become a sandbox verdict.
    """
    global _probe_spawn_unavailable_logged
    if not sys.executable:
        if not _probe_spawn_unavailable_logged:
            _probe_spawn_unavailable_logged = True
            logger.warning(
                "namespace probe cannot spawn a fresh interpreter (sys.executable is "
                "empty); falling back to a fork-based probe, which reports EINVAL on a "
                "multithreaded caller even where the sandbox works"
            )
        return None

    uid, gid = os.getuid(), os.getgid()
    try:
        # close_fds is subprocess's default and does the job the fork path has to do
        # by hand: the child gets only its standard streams, so an orphaned probe
        # cannot hold the gateway lock fd or the dashboard listen socket open.
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _PROBE_SHIM_CODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        return _probe_failure("probe spawn", exc.errno or 0)
    except Exception as exc:  # pragma: no cover - defensive
        return (False, True, f"probe spawn failed: {exc}", "")

    assert proc.stdin is not None and proc.stdout is not None
    try:
        return _probe_parent_sequence(
            proc.pid,
            proc.stdout.fileno(),
            proc.stdin.fileno(),
            uid,
            gid,
            death=lambda _pid: _probe_spawned_death(proc),
        )
    finally:
        # Closing stdin releases a child still waiting on the maps; the wait then
        # reaps it. Popen owns the pid, so _probe_reap must NOT run here.
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
        except Exception:  # pragma: no cover - a wedged interpreter
            proc.kill()
            try:
                proc.wait(timeout=_PROBE_SPAWN_TIMEOUT_SECONDS)
            except Exception:
                pass


def _probe_unshare_via_fork() -> tuple[bool, bool, str, str]:
    """The fork-based probe: same verdict, but the child inherits fork hooks.

    Retained as the fallback for a process that cannot spawn an interpreter at all.
    Its child can be made multithreaded by an ``os.register_at_fork`` handler, which
    is why :func:`_probe_unshare_via_spawn` is preferred whenever it is available.

    Mirrors the sequence ``_build_launcher_script()`` actually performs — fork,
    child ``unshare(CLONE_NEWUSER)``, parent writes the identity UID/GID map,
    child ``unshare(CLONE_NEWNS)``, child ``mount(MS_REC|MS_PRIVATE)`` on ``/``
    — because the steps do NOT behave the same way when combined or skipped. A
    single ``unshare(CLONE_NEWUSER | CLONE_NEWNS)`` is satisfied atomically and
    therefore SUCCEEDS on hosts where the split sequence fails: with Ubuntu's
    ``kernel.apparmor_restrict_unprivileged_userns = 1`` (the default since
    23.10), creating a user namespace moves the process into a restricted
    AppArmor profile carrying no CAP_SYS_ADMIN, so the *second* unshare returns
    EPERM. The previous combined probe reported those hosts as sandbox-capable
    and every real spawn then died with ``sandbox: unshare(NEWNS) failed: errno
    1``. The propagation mount is the same story one step later: a container
    under its runtime's default AppArmor profile passes both unshares and
    refuses the mount with EACCES, and a probe that stopped at the unshares
    reported it sandbox-capable too.

    ``reason`` names the failing step so a caller can tell the mechanisms apart
    — a NEWNS denial is the AppArmor userns restriction, NEWUSER with
    ENOSPC/EUSERS is ``user.max_user_namespaces=0``, and a mount denial is the
    container runtime's policy — rather than reporting a bare errno that fits
    all of them.

    Linux-only and off-loop only: ``_probe_unshare()`` guards the platform and
    defers to the background warm thread when a loop is running, so the fork,
    pipe reads and ``waitpid`` here never block the event loop.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _probe_bind_libc(libc)
    except OSError as exc:
        return (False, exc.errno in _TRANSIENT_PROBE_ERRNOS, f"libc load failed: {exc}", "")
    except Exception as exc:  # find_library returning junk, ABI issues, ...
        return (False, False, f"libc load failed: {exc}", "")

    uid, gid = os.getuid(), os.getgid()
    try:
        c2p_r, c2p_w = os.pipe()
    except OSError as exc:
        return _probe_failure("probe pipe", exc.errno or 0)
    try:
        p2c_r, p2c_w = os.pipe()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w)
        return _probe_failure("probe pipe", exc.errno or 0)

    # Compute the child's fd sweep BEFORE forking: sorting, sysconf, and tuple
    # building all allocate, and post-fork the allocator lock may be held by a
    # thread that does not exist in the child.
    sweep_ranges = _fd_sweep_ranges(frozenset({0, 1, 2, c2p_w, p2c_r}))

    try:
        pid = os.fork()
    except OSError as exc:
        _close_probe_fds(c2p_r, c2p_w, p2c_r, p2c_w)
        return _probe_failure("fork", exc.errno or 0)

    if pid == 0:
        _probe_child_sequence(libc, c2p_r, c2p_w, p2c_r, p2c_w, sweep_ranges)  # never returns
        os._exit(1)  # pragma: no cover - defensive

    _close_probe_fds(c2p_w, p2c_r)
    try:
        return _probe_parent_sequence(pid, c2p_r, p2c_w, uid, gid)
    finally:
        # Closing p2c_w also releases a child still waiting on the maps.
        _close_probe_fds(c2p_r, p2c_w)
        _probe_reap(pid)


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Public: True if this Linux host is running under Windows Subsystem for Linux.

    Centralized host probe (parallel to :func:`userns_available`) so consumers
    never re-implement WSL detection. WSL2 *does* expose working user
    namespaces, so :func:`userns_available` returns True there — but WSL's
    networking is a NAT'd virtual interface, and rootless-namespace jails
    (slirp4netns) make agentic command networking unreachable. A jail backend
    (JailProvider) uses this to opt WSL out of jailing.

    Detection (cheap, in order): the ``WSL_DISTRO_NAME`` / ``WSL_INTEROP`` env
    vars WSL injects into every login shell, then the ``microsoft`` marker the
    WSL kernel stamps into ``/proc/version`` (covers WSL1 + WSL2, both Microsoft
    and -microsoft-standard builds). Result is cached — the host's WSL-ness does
    not change within a process. Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


@functools.lru_cache(maxsize=1)
def is_docker_container() -> bool:
    """Public: True if this process is running inside a Docker/OCI container.

    Centralized host probe (parallel to :func:`is_wsl`) so consumers never
    re-implement container detection.  Used by :func:`wrap_argv` to produce an
    actionable error message when ``unshare(CLONE_NEWUSER)`` is blocked by the
    container runtime's seccomp/AppArmor policy instead of a kernel-level
    user-namespace restriction — the two cases warrant different remedies.

    Detection order (cheap, no I/O on fast paths):

    1. ``/.dockerenv`` — Docker daemon creates this in every container.
    2. ``/run/.containerenv`` — Podman's equivalent OCI marker.
    3. ``CONTAINER=oci`` env var — set by Podman rootless and some runtimes.
    4. ``/proc/1/cgroup`` — contains ``docker``, ``containerd``, or
       ``kubepods`` in container-managed cgroups; also fires in nested
       Docker-in-Docker setups.

    Result is cached — the container context does not change within a process.
    Always False off Linux.
    """
    if sys.platform != "linux":
        return False
    # Fast path: Docker always creates /.dockerenv; Podman creates /run/.containerenv.
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    # Podman rootless and some OCI runtimes export CONTAINER=oci.
    if os.environ.get("CONTAINER") == "oci":
        return True
    # Fallback: inspect the cgroup hierarchy for well-known runtime markers.
    try:
        with open("/proc/1/cgroup", encoding="utf-8", errors="replace") as fh:
            content = fh.read().lower()
        return "docker" in content or "containerd" in content or "kubepods" in content
    except OSError:
        return False


def _probe_sandbox_exec() -> bool:
    """Return True if macOS ``sandbox-exec`` actually works.

    Uses a file-based profile with fixed system paths for both
    ``sandbox-exec`` and its ``/usr/bin/true`` target. The probe tests with an
    ``(allow default)`` profile to detect kernel-level rejection, not merely
    executable presence.
    """
    if sys.platform != "darwin":
        return False
    # Decide empirically — do NOT hard-code a macOS version cutoff. A
    # `major >= 26 → return False` gate is wrong: sandbox-exec + the Seatbelt
    # kernel subsystem still work on macOS 26 (Tahoe) — verified that the real
    # generated profile compiles, runs kiro-cli, AND enforces (a strict profile
    # denies `cat ~/.aws/config`). Such a gate disables a working sandbox and forces
    # the agent onto the fail-closed no-isolation path. The probe below already
    # detects a genuinely-broken sandbox-exec on any host/version, so trust it.
    # Note: sandbox-exec / sandbox_init() are marked "deprecated" in headers
    # since macOS 10.8, but the Seatbelt kernel subsystem they use is NOT
    # deprecated — it's the same enforcement layer that backs App Sandbox and
    # iOS. All major AI CLIs (Claude Code, Codex, Gemini) rely on it.
    # Rather than hard-coding version checks, we probe empirically below.
    sb = "/usr/bin/sandbox-exec"
    if not os.path.exists(sb):
        return False
    # Probe with a file-based (allow default) profile against a TRUSTED, fixed
    # system binary. We deliberately do NOT probe the (user-writable) kiro-cli
    # binary: the probe runs under (allow default) with Kiro Crew's credentials,
    # so exec'ing a user-writable target here could run a planted payload
    # effectively unsandboxed. The probe only needs to confirm the kernel
    # accepts sandbox_apply, which /usr/bin/true validates safely.
    target = "/usr/bin/true"
    if not os.path.exists(target):
        return False
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="kirocrew_probe_")
    try:
        os.write(fd, b"(version 1)(allow default)")
        os.close(fd)
        r = subprocess.run(
            [sb, "-f", profile_path, target],
            capture_output=True,
            timeout=_SANDBOX_BACKEND_PROBE_TIMEOUT_SECS,
            # Pinned to the profile's own temp dir: the probe runs /usr/bin/true
            # and reads nothing, so it has no claim on the caller's cwd -- and a
            # spawn with cwd=None is indistinguishable, to a per-spawn audit,
            # from one that ran in the checkout under test (the one class-7
            # descriptor a per-spawn sweep reads, charged to an arbitrary
            # first test on every worker).
            cwd=os.path.dirname(profile_path),
        )
        if r.returncode != 0:
            detail = r.stderr.decode(errors="replace").strip()
            # A nested probe ALWAYS fails: Seatbelt cannot nest, so from inside a
            # sandbox `sandbox_apply` returns EPERM even under an (allow default)
            # profile. Reporting that at WARNING as a probe failure sent operators
            # hunting for a broken sandbox-exec on hosts where it works perfectly
            # unnested, so say what actually happened instead.
            if _macos_sandbox_state() is True:
                logger.info(
                    "sandbox-exec probe failed inside an existing Seatbelt "
                    "sandbox (exit %d: %s) — nesting is impossible; this host's "
                    "sandbox-exec is NOT broken",
                    r.returncode,
                    detail,
                )
            else:
                logger.warning(
                    "sandbox-exec probe failed (exit %d): %s",
                    r.returncode,
                    detail,
                )
        return r.returncode == 0
    except Exception as exc:
        logger.debug("sandbox-exec probe failed: %s", exc)
        return False
    finally:
        try:
            os.unlink(profile_path)
        except OSError:
            pass


def _inside_kirocrew_sandbox() -> bool:
    """True when this process already runs inside a Kiro Crew OS sandbox.

    Nested sandboxing is impossible on both backends: the Linux launcher's
    seccomp filter denies ``unshare``/``setns`` precisely so the sandboxed tree
    cannot manipulate namespaces, and macOS Seatbelt refuses ``sandbox_apply``
    with EPERM from inside an existing sandbox — even under an ``(allow default)``
    outer profile. An in-sandbox wrap_argv call must therefore pass through rather
    than fail closed — the outer sandbox still confines every descendant, so this
    is NOT the fail-open path. Failing closed here would refuse every in-sandbox
    MCP spawn with unshare EPERM and, on macOS, every app-backend spawn and MCP
    probe the gateway starts.

    Detection is deny-by-default: gated solely on the explicit, launcher-only
    ``KIROCREW_SANDBOX_ACTIVE`` marker (see ``_IN_SANDBOX_MARKER``).
    """
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


@functools.lru_cache(maxsize=1)
def _macos_sandbox_state() -> bool | None:
    """Kernel verdict on whether THIS macOS process is Seatbelt-confined.

    ``True``
        Confined — ``sandbox_check(pid, NULL, SANDBOX_FILTER_NONE)`` returned 1.
    ``False``
        Definitely not confined — the kernel answered 0.
    ``None``
        Unanswerable — non-darwin, or the symbol could not be loaded or called.

    Three states rather than a bool because the two negatives carry opposite
    security meanings. A definite ``False`` alongside a present
    ``KIROCREW_SANDBOX_ACTIVE`` marker proves the marker was forged or inherited
    into an unsandboxed process, and must NOT grant a passthrough. An
    unanswerable probe says nothing at all, and must not retroactively invalidate
    a marker the Linux path honours unconditionally.

    Cached for the process lifetime — a process cannot leave its sandbox.
    """
    if sys.platform != "darwin":
        return None
    try:
        libpath = ctypes.util.find_library("System") or "/usr/lib/libSystem.dylib"
        lib = ctypes.CDLL(libpath, use_errno=True)
        check = lib.sandbox_check
        # sandbox_check(pid_t, const char *operation, enum sandbox_filter_type, ...)
        # A NULL operation with SANDBOX_FILTER_NONE (0) asks the generic
        # "is this pid sandboxed at all?" question. The path-scoped form that
        # could identify WHICH paths the profile denies is variadic and returns
        # -1 through ctypes on arm64, so it is not usable here.
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        rc = check(os.getpid(), None, 0)
        if rc < 0:
            return None
        return rc == 1
    except Exception as exc:  # missing symbol, ABI change, restricted dyld, ...
        logger.debug("sandbox_check unavailable (%s); sandbox state unknown", exc)
        return None


def _inside_macos_sandbox() -> bool:
    """True when the kernel confirms a Seatbelt sandbox confines this process.

    It tells a *nesting* EPERM apart from a genuinely missing backend, so the
    fail-closed error names the real cause. Without it,
    :func:`_probe_sandbox_exec`'s EPERM is reported as "this host has no sandbox
    backend" — on a host whose ``sandbox-exec`` works perfectly when not nested.

    Unlike :data:`_IN_SANDBOX_MARKER` this is OS-authoritative: it cannot be
    spoofed through an agent-influenced environment, and it sees sandboxes
    Kiro Crew did NOT create — notably kiro-cli >= 2.13's own internal seatbelt
    (see ``_KIRO_INTERNAL_SETTINGS_PATH``), or an operator-wrapped gateway. It is
    therefore NOT sufficient on its own to grant a passthrough: it proves *some*
    sandbox is active, not that Kiro Crew built it or scrubbed the environment.
    The marker supplies that half; see :func:`wrap_argv`.
    """
    return _macos_sandbox_state() is True
