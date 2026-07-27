"""Keep a POSIX setup subprocess group anchored until all descendants exit."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

_POLL_SECONDS = 0.05


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


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def main() -> None:
    argv = sys.argv[1:]
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
