"""Keep a POSIX setup subprocess group anchored until all descendants exit.

Also applies the caller's resource limits. This runs AFTER ``exec``, in a
single-threaded process, which is the whole point: applying them via
``preexec_fn`` instead forced CPython to ``fork()`` the multi-GB, ~118-thread
gateway and run Python in the child before ``exec``. Locks other threads held at
fork time are unreleasable there, so the child deadlocked in a futex, never
exec'd, and never exited -- while pinning every fd it inherited (see the
gateway.lock reclaim fix). Limits set here are inherited by the exec'd child and
all of its descendants, so coverage is unchanged.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import time
from pathlib import Path

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows has no POSIX rlimits
    _resource = None  # type: ignore[assignment]

_POLL_SECONDS = 0.05
_RLIMIT_FLAG = "--rlimits="
_REAP_SURVIVORS_FLAG = "--reap-survivors"
_REAP_GRACE_SECONDS = 1.0


def _apply_rlimits(spec: str) -> None:
    """Apply ``RLIMIT_NAME:value`` pairs from *spec* to this process.

    Mirrors ``kiro_crew.security.apply_resource_limits``'s clamp rules -- take
    the min of the request and the inherited hard limit, set soft AND hard so the
    child cannot raise its own ceiling back up, and skip anything the kernel or
    platform rejects rather than failing the spawn. Duplicated rather than
    imported on purpose: this file is executed as an immutable ``python -I -c``
    source string, so it must stay stdlib-only.

    The ``resource`` import is guarded because this module is also imported
    directly by the test suite, including on Windows, where the supervisor itself
    is never spawned.
    """
    if _resource is None:
        return
    res = _resource
    for item in spec.split(","):
        name, _, raw = item.partition(":")
        try:
            res_id = getattr(res, name)
            requested = int(raw)
        except (AttributeError, ValueError):
            continue
        try:
            _soft, hard = res.getrlimit(res_id)
            if hard != res.RLIM_INFINITY:
                requested = min(requested, hard)
            res.setrlimit(res_id, (requested, requested))
        except (ValueError, OSError):
            continue


def _bias_oom_score() -> None:
    """Bias the OOM killer toward this process tree (inherited by descendants).

    Mirrors ``kiro_crew.security._bias_child_oom_score``: a memory-ballooning
    tool subprocess should be killed before the cgroup ceiling takes out the
    whole agent scope. Linux-only, unprivileged, best-effort.
    """
    if sys.platform != "linux":
        return
    try:
        fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
        try:
            os.write(fd, b"1000")
        finally:
            os.close(fd)
    except OSError:
        pass


def _proc_stat_group_member(text: str, pgid: int) -> bool:
    """Return whether one Linux stat record is a live member of *pgid*."""

    fields = text.rpartition(")")[2].split()
    # After the command name: state, ppid, pgrp. Zombies can remain visible
    # indefinitely under a PID 1 that does not reap promptly, but they cannot
    # retain pipes or execute work and therefore must not hold this supervisor.
    return len(fields) >= 3 and fields[0] != "Z" and int(fields[2]) == pgid


def _parse_ps_group_members(output: str, pgid: int, ps_pid: int) -> set[int]:
    """Parse ``ps pid,pgid,state`` output, excluding zombies and ps itself."""

    members: set[int] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            pid = int(fields[0])
            candidate_pgid = int(fields[1])
        except ValueError:
            continue
        if candidate_pgid == pgid and pid != ps_pid and not fields[2].startswith("Z"):
            members.add(pid)
    return members


def _linux_group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            if _proc_stat_group_member(text, pgid):
                members.add(int(entry.name))
        except (OSError, ValueError):
            continue
    return members


def _ps_group_members(pgid: int) -> set[int]:
    read_fd, write_fd = os.pipe()
    ps_pid = os.fork()
    if ps_pid == 0:
        try:
            os.close(read_fd)
            os.dup2(write_fd, 1)
            os.close(write_fd)
            os.execv("/bin/ps", ["ps", "-axo", "pid=,pgid=,state="])
        except OSError:
            os._exit(127)
    os.close(write_fd)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(read_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        os.waitpid(ps_pid, 0)
    return _parse_ps_group_members(
        b"".join(chunks).decode("utf-8", "replace"),
        pgid,
        ps_pid,
    )


def _group_members(pgid: int) -> set[int]:
    if sys.platform.startswith("linux") and Path("/proc").is_dir():
        return _linux_group_members(pgid)
    return _ps_group_members(pgid)


# pidfd_open(2) and pidfd_send_signal(2) each have one syscall number that is
# the same on every Linux architecture (they postdate the per-arch tables).
# Used through ctypes when the interpreter was built without the ``os`` /
# ``signal`` wrappers: the python-build-standalone CPython 3.12 that uv and mise
# install has neither, and it is what runs Kiro Crew on the host that hit this.
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434
_libc: object = None


def _syscall(number: int, *args: object) -> int:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    result = _libc.syscall(number, *args)  # type: ignore[attr-defined]
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(result)


def _pidfd_open(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return int(opener(pid))
    return _syscall(_SYS_PIDFD_OPEN, pid, 0)


def _pidfd_send_signal(fd: int, sig: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(fd, sig)
        return
    _syscall(_SYS_PIDFD_SEND_SIGNAL, fd, sig, None, 0)


def can_reap() -> bool:
    """Whether a group member can be pinned before it is signalled (Linux pidfd)."""
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return False
    try:
        os.close(_pidfd_open(os.getpid()))
    except (OSError, AttributeError):
        return False
    return True


def _signal_member(pid: int, pgid: int, sig: int) -> bool:
    """Signal *pid* only if it is provably still a member of *pgid*.

    A bare ``os.kill`` after listing the group could reach a stranger: the
    listed member may exit and its pid be reissued before the signal lands. So
    the process is pinned with a pidfd first, and membership is re-read AFTER
    the pin. If the pinned process is still alive, that re-read describes it; if
    it has exited, the pidfd signal fails with ESRCH and reaches nobody. Joining
    this group is impossible from outside this session, so a member read back
    here is always this command's own descendant.
    """
    try:
        fd = _pidfd_open(pid)
    except OSError:
        return False
    try:
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        if not _proc_stat_group_member(text, pgid):
            return False
        try:
            _pidfd_send_signal(fd, sig)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)


def _signal_members(pgid: int, own_pid: int, sig: int) -> set[int]:
    """Send *sig* to every live member of *pgid* except this process.

    Per member rather than ``killpg``: this leader ignores SIGTERM but not
    SIGKILL, and it has to outlive the members to keep the group id anchored.
    Returns the members that were found.
    """
    members = _group_members(pgid) - {own_pid}
    for pid in members:
        _signal_member(pid, pgid, sig)
    return members


def _terminate_survivors(pgid: int, own_pid: int) -> None:
    """End the descendants a finished command left in this group.

    Seen in the wild: a kiro-cli launcher wrapper starts a credential helper
    (about 140 threads) for each call and does not stop it when the call
    returns. Reparented to a subreaper rather than pid 1, the helper never
    notices it is orphaned, so every call left one running until the agent's
    cgroup ran out of pids. SIGTERM first, SIGKILL after a short grace.
    """
    try:
        if not _signal_members(pgid, own_pid, signal.SIGTERM):
            return
    except OSError:
        return
    deadline = time.monotonic() + _REAP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            if not (_group_members(pgid) - {own_pid}):
                return
        except OSError:
            pass
        time.sleep(_POLL_SECONDS)
    try:
        _signal_members(pgid, own_pid, signal.SIGKILL)
    except OSError:
        pass


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main() -> None:
    argv = sys.argv[1:]
    # Bias the OOM killer FIRST and unconditionally. It is an independent control
    # from the rlimits, so gating it on the presence of ``--rlimits=`` would
    # silently drop it for an operator who disables every limit. Inherited by the
    # exec'd child and its descendants.
    _bias_oom_score()
    # Optional leading --rlimits=NAME:value,... from the spawning gateway. Applied
    # before the fork below so the exec'd child and every descendant inherit the
    # ceiling.
    #
    # Optional leading --reap-survivors: once the command exits, end whatever it
    # left running in this group instead of waiting for it. Without it the wait
    # below lasts as long as the longest-lived descendant, which is what a caller
    # that owns the whole tree's lifetime (``_run_process``) wants.
    reap_survivors = False
    while argv and argv[0].startswith("--"):
        if argv[0].startswith(_RLIMIT_FLAG):
            _apply_rlimits(argv[0][len(_RLIMIT_FLAG) :])
        elif argv[0] == _REAP_SURVIVORS_FLAG:
            reap_survivors = True
        else:
            raise SystemExit(127)
        argv = argv[1:]
    if not argv or not Path(argv[0]).is_absolute():
        raise SystemExit(127)

    # The gateway sends TERM to the whole session, then escalates to KILL.
    # Keeping this leader alive through TERM prevents a numeric PGID-reuse race
    # while a pipe-holding descendant is still running.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)

    child_pid = os.fork()
    if child_pid == 0:
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            # This private supervisor is launched only by `_run_process`, which
            # supplies a server-owned absolute executable and fixed argv. Avoid
            # PATH lookup and keep the inherited, already-sanitized environment.
            os.execve(  # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
                argv[0],
                argv,
                os.environ,
            )
        except OSError:
            os._exit(127)

    _, status = os.waitpid(child_pid, 0)
    own_pid = os.getpid()
    pgid = os.getpgrp()
    if reap_survivors and pgid == own_pid and can_reap():
        # Only while this process LEADS its own group: the caller spawned it with
        # start_new_session=True, so every member is something this command
        # started. Without that guarantee the group could be the caller's own,
        # and signalling it would take the caller down with it. Where a member
        # cannot be pinned (no pidfd), nothing is signalled and the wait below
        # behaves as it does without the flag.
        _terminate_survivors(pgid, own_pid)
    while True:
        try:
            if not (_group_members(pgid) - {own_pid}):
                break
        except OSError:
            # Unknown membership must not release the identity anchor.
            pass
        time.sleep(_POLL_SECONDS)
    raise SystemExit(_exit_code(status))


if __name__ == "__main__":
    main()
