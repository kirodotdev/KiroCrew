"""Live sandboxes whose masks cover one crew-home path but not another.

A sandbox's masks are fixed when it starts. A move that takes a secret out of a path
every build masks and into one only a newer build masks is safe only once no sandbox
that masks the first and not the second is still running: such a sandbox, started by an
older build, could read the secret where it lands. :func:`sandboxes_masking_only` names
those processes, so the move can wait for them.

**Linux** answers exactly, from the kernel. Every namespace sandbox masks a crew-home leaf
with a mount at its host path, and ``/proc/<pid>/mountinfo`` lists a process's mount
points for any same-uid reader, so a process counts when one of its mount points is
``<home>/<masked>`` (at any spelling of the home) and none is the same spelling's
``<home>/<uncovered>``. A confined process cannot add or remove a mount, nor read
around one, so nothing it does changes the answer. A process whose main thread has exited
while others run on has no view of its own; each live thread's
``/proc/<pid>/task/<tid>/mountinfo`` is judged instead. A process in the reader's own
mount namespace has no masks and never counts.

**macOS** answers from the kernel too: the Seatbelt policy of each same-uid process is
asked, with ``sandbox_check`` (:func:`platform_compat.process_can_read_under_sandbox`),
whether it may read ``<home>/<masked>`` and ``<home>/<uncovered>`` at the data home's
canonical spelling, the one Seatbelt judges a real open by. A process counts when it is
denied the first and not denied the second, or when either answer is unknown. A process
allowed the first could already read what moves, and a sandbox of this build is denied
both, so neither counts. The policy is inherited by every child and held by the kernel,
so a confined process cannot shed it by emptying its environment or detaching.

A process can exit between the moment a scan lists it and the moment it is judged, and
leave behind a child forked in between, which keeps its masks and was never listed. So
on both platforms a scan judges every process each new snapshot lists, and answers
"none" only once a snapshot shows nothing new it could not judge and every process the
previous one could not judge still there, unchanged (:func:`_settled`); a table that
keeps changing answers ``None``, as an unreadable one does. A Linux listing is a
directory read, so a child born while it runs, under a pid below the read's position,
is missed by that read; it is listed by the next one unless the pid space wraps under
each read in turn.

Only sandboxes alive NOW count. An unconfined process of an older build that starts a
sandbox after the answer is not seen; the one such process that outlives a gateway, the
MCP gateway daemon, is stood down by the next gateway's start when its code differs
(``mcp-gateway-daemon-lifecycle.md``), and its live backends are sandboxes that count.

**Windows** answers ``[]``: no build masks a path there, so no sandbox can be outrun.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from kiro_crew import platform_compat

#: ``mountinfo`` octal-escapes these four bytes in a mount point.
_MOUNTINFO_ESCAPES = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}


def _unescape(field: str) -> str:
    for escaped, plain in _MOUNTINFO_ESCAPES.items():
        field = field.replace(escaped, plain)
    return field


def _masks_only(mount_points: set[str], home: str, masked: str, uncovered: str) -> bool:
    """Whether some spelling of *home* has a mount at *masked* and none at *uncovered*."""
    tail = "/" + masked
    for point in mount_points:
        if not point.endswith(tail):
            continue
        root = point[: -len(tail)]
        if os.path.realpath(root) == home and f"{root}/{uncovered}" not in mount_points:
            return True
    return False


#: Passes :func:`_settled` takes before a table that keeps changing answers ``None``.
_PASSES = 5

#: One process table: an identity (start instant and whether it has exited) per pid.
_Snapshot = dict[int, object]


def _settled(
    snapshot: Callable[[], _Snapshot | None], judge: Callable[[int], bool | None]
) -> list[int] | None:
    """Judge every process the table lists, and answer ``[]`` only once it held still.

    *judge* answers True (it counts), False (it does not), or None (no answer: gone, a
    zombie, unreadable). Each pass takes a snapshot and judges every process in it not
    judged before, keyed by pid and identity. A process that exits after a snapshot lists
    it, or while one is being read, may leave a child that snapshot never listed, and
    the child keeps its masks; the child is in the next snapshot, so it is judged there.
    A pass answers ``[]`` only when every process the previous snapshot could not judge
    is still in this one, unchanged, and every process new in this one was judged: then
    no process left between the two snapshots without its children being judged. A
    table that never holds still that long answers ``None`` after :data:`_PASSES`
    passes; a process that counts answers at once.
    """
    verdicts: dict[tuple[int, object], bool | None] = {}
    previous: _Snapshot | None = None
    for _ in range(_PASSES):
        current = snapshot()
        if current is None:
            return None
        new_unjudged = False
        for pid, identity in current.items():
            if (pid, identity) not in verdicts:
                verdicts[(pid, identity)] = verdict = judge(pid)
                new_unjudged = new_unjudged or verdict is None
        found = sorted(pid for pid, identity in current.items() if verdicts[(pid, identity)])
        if found:
            return found
        if (
            previous is not None
            and not new_unjudged
            and all(
                current.get(pid) == identity
                for pid, identity in previous.items()
                if verdicts[(pid, identity)] is None
            )
        ):
            return []
        previous = current
    return None


def _linux_snapshot(proc: Path, me: int) -> _Snapshot | None:
    try:
        names = [e.name for e in proc.iterdir() if e.name.isdigit() and int(e.name) not in (me, 1)]
    except OSError:
        return None
    # A pid gone before its ``stat`` is read is left out (a child it left is listed by the
    # next snapshot), and so is one ``hidepid`` keeps from this user.
    listed: _Snapshot = {}
    for name in names:
        stat = platform_compat.read_proc_stat(int(name), proc_root=proc)
        if stat is not None and stat.start_ticks is not None:
            listed[int(name)] = (stat.start_ticks, stat.state in ("Z", "X", "x"))
    return listed


def _linux_view(
    task: Path, own_ns: int | None, home: str, masked: str, uncovered: str
) -> bool | None:
    """Judge one task's mount view; None when it has none left (gone, or exited)."""
    if own_ns is not None:
        try:
            if os.stat(task / "ns" / "mnt").st_ino == own_ns:
                return False
        except OSError:
            pass
    try:
        lines = (task / "mountinfo").read_text(encoding="utf-8", errors="surrogateescape")
    except PermissionError:
        # A live same-uid task whose mounts cannot be read: its masks are unknown.
        return True
    except OSError:
        # Gone, or exited with no mount namespace left (``EINVAL``).
        return None
    points = {_unescape(line.split()[4]) for line in lines.splitlines() if len(line.split()) > 4}
    return _masks_only(points, home, masked, uncovered)


def _linux_judge(
    pid: int, proc: Path, uid: int, own_ns: int | None, home: str, masked: str, uncovered: str
) -> bool | None:
    base = proc / str(pid)
    # The real uid from ``status``, not the entry's owner: a process that makes itself
    # undumpable turns its ``/proc`` entry over to root, and must not drop out that way.
    real = _real_uid(base)
    if real is None:
        return None
    if real != uid:
        return False
    verdict = _linux_view(base, own_ns, home, masked, uncovered)
    if verdict is not None:
        return verdict
    # The leader has no view: gone, a zombie, or a leader that exited while other threads
    # of the process still run, each with the process's masks. Judge those threads.
    try:
        tids = [t for t in (base / "task").iterdir() if t.name.isdigit() and t.name != str(pid)]
    except OSError:
        return None
    views = [_linux_view(t, own_ns, home, masked, uncovered) for t in tids]
    if any(views):
        return True
    return False if False in views else None


def _linux(proc: Path, home: str, masked: str, uncovered: str) -> list[int] | None:
    uid = platform_compat.effective_uid()
    if uid is None:
        return None
    me = os.getpid()
    try:
        own_ns: int | None = os.stat(proc / str(me) / "ns" / "mnt").st_ino
    except OSError:
        own_ns = None
    return _settled(
        lambda: _linux_snapshot(proc, me),
        lambda pid: _linux_judge(pid, proc, uid, own_ns, home, masked, uncovered),
    )


def _real_uid(entry: Path) -> int | None:
    try:
        status = (entry / "status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
    return None


def _darwin_snapshot(me: int) -> _Snapshot | None:
    # ``sysctl KERN_PROC_ALL``: one in-process read of the whole table, no ``ps`` to lose.
    members = platform_compat._darwin_kinfo_members(platform_compat._DARWIN_KERN_PROC_ALL, 0)
    if not members:
        return None
    return {m.pid: (m.start_id, m.zombie) for m in members if m.pid > 1 and m.pid != me}


def _darwin_judge(pid: int, masked: Path, uncovered: Path) -> bool | None:
    # ``darwin_process_facts`` answers for a live same-uid process only: another user's
    # process cannot read this user's owner-only data home in any case.
    if platform_compat.darwin_process_facts(pid) is None:
        return None
    can_read_masked = platform_compat.process_can_read_under_sandbox(pid, masked)
    if can_read_masked is True:
        return False
    if can_read_masked is False:
        if platform_compat.process_can_read_under_sandbox(pid, uncovered) is False:
            return False
    # Unknown, or denied the old path and not the new: counts while the process lives,
    # and a process gone by now is left to the settle check.
    return True if platform_compat.darwin_process_facts(pid) is not None else None


def _darwin(home: str, masked: str, uncovered: str) -> list[int] | None:
    me = os.getpid()
    masked_path, uncovered_path = Path(home, masked), Path(home, uncovered)
    return _settled(
        lambda: _darwin_snapshot(me),
        lambda pid: _darwin_judge(pid, masked_path, uncovered_path),
    )


def sandboxes_masking_only(
    home: Path, masked: str, uncovered: str, *, proc_root: Path | None = None
) -> list[int] | None:
    """Pids of live sandboxes that mask ``<home>/<masked>`` and not ``<home>/<uncovered>``.

    *masked* and *uncovered* are ``/``-joined paths relative to the data home. ``None``
    when the process table cannot be read, which a caller must read as "some may be
    alive". *proc_root* is a fixture seam that takes the Linux path on any host.
    """
    resolved = os.path.realpath(str(home))
    if proc_root is not None or sys.platform == "linux":
        return _linux(proc_root or Path("/proc"), resolved, masked, uncovered)
    if platform_compat.IS_WINDOWS:
        return []
    return _darwin(resolved, masked, uncovered)
