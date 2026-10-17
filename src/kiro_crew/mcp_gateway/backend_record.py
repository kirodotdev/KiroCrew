"""The out-of-band record of a gatewayd's pooled backends, and its reap.

Pooled backends are session leaders (``start_new_session=True``), so they
outlive a ``gatewayd`` that dies without draining them. The daemon therefore
publishes its live backends beside its socket in ``<socket>.backends`` on every
heartbeat, and whoever finds that record left behind by a dead generation reaps
it: the supervising manager after it force-kills a wedged daemon, and the next
daemon at boot once it holds the singleton lock -- which is what covers a crash,
an OOM-kill or an outside SIGKILL, where no supervisor ever kills anything.

One line per backend: ``<pid> <start-id>``, the start id being
:func:`platform_compat.get_process_start_id` read once right after spawn
(``Backend.start_id``). A reap signals a recorded pid only while the live process under
that pid still carries the same start id, so a pid recycled onto an unrelated
process (or its group) is never signalled. A line with no start id -- the bare
``<pid>`` a daemon from before this format wrote -- is unknown identity and is
left alone rather than killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from kiro_crew import platform_compat, process_identity

logger = logging.getLogger(__name__)


def record_path(socket_path: str | Path) -> Path:
    """Where the daemon serving *socket_path* records its backends."""
    return Path(f"{socket_path}.backends")


def detach(path: Path) -> list[Path]:
    """Move a dead generation's record out of the heartbeat's way, unread.

    The next heartbeat writes *path* afresh, so a boot reap that has not
    finished by then would lose the list it is working from. The record is
    therefore RENAMED (``os.replace``, atomic, no bytes rewritten) to a name of
    its own, ``<path>.reaping.<pid>.<ns>``, and the kills run off the startup
    path. Nothing here writes file contents, so no failure can truncate a
    record: a rename that fails leaves *path* exactly as it was. Every
    ``.reaping.*`` file present is returned, so one an interrupted earlier boot
    left behind is reaped again rather than forgotten.
    """
    if path.exists():
        target = path.with_name(f"{path.name}.reaping.{os.getpid()}.{time.time_ns()}")
        # Propagates. A record that cannot be moved aside is still at the path
        # this daemon's heartbeat writes and its clean shutdown unlinks, so a
        # daemon that went on to serve would destroy the only list of the
        # previous generation's backends. The caller refuses to start instead.
        os.replace(path, target)
    try:
        return sorted(path.parent.glob(f"{glob.escape(path.name)}.reaping.*"))
    except OSError:
        return []


async def reap_all(paths: Iterable[Path], *, reason: str) -> int:
    """:func:`reap` each of *paths* in turn; the total trees signalled."""
    killed = 0
    for one in paths:
        killed += await reap(one, reason=reason)
    return killed


def render(identities: Iterable[tuple[int, Optional[str]]]) -> str:
    """Render the record for ``(pid, start_id)`` pairs.

    The start id is the one each backend captured at spawn
    (``Backend.start_id``), never read here: a read at heartbeat time can meet a
    backend that has since exited and had its pid recycled, and would publish
    the stranger's identity as the backend's. A pair with no start id is
    written as a bare pid and will therefore never be reaped from this record.
    """
    return "\n".join(f"{pid} {start}" if start else str(pid) for pid, start in identities)


def parse(raw: str) -> list[tuple[int, Optional[str]]]:
    """``(pid, start_id)`` per line; ``start_id`` is ``None`` on a bare pid.

    Junk lines and pids that are not ``> 1`` are dropped: pid 1 and the
    broadcast values are never anyone's backend.
    """
    records: list[tuple[int, Optional[str]]] = []
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid <= 1:
            continue
        records.append((pid, fields[1] if len(fields) > 1 else None))
    return records


async def _kill_pinned(pid: int, recorded: str) -> bool:
    """SIGKILL *pid*'s tree only while it is still the process *recorded* names.

    The repository's own identity-pinned kills, not a compare followed by a
    pid-addressed kill: on POSIX :func:`process_identity.isolated_group_of`
    reads the start id on both sides of the group lookup and the group is then
    signalled by id; on Windows :func:`platform_compat.kill_process_tree_pinned`
    holds the verified handle across the terminate, so the pid ``taskkill``
    resolves cannot have been recycled in between. SIGKILL because a wrapper that
    ignores stdin EOF commonly ignores SIGTERM too. Returns whether a tree was
    signalled.
    """
    try:
        if platform_compat.IS_WINDOWS:
            return await asyncio.to_thread(
                platform_compat.kill_process_tree_pinned, pid, recorded, platform_compat.SIGKILL
            )
        pgid = process_identity.isolated_group_of(pid, recorded)
        if pgid is None:
            return False
        return platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return False


async def reap(path: Path, *, reason: str) -> int:
    """SIGKILL the process tree of every identity-confirmed backend in *path*.

    Best-effort and never raises. The record is removed afterwards whatever
    happened to each entry: it describes a generation that is gone, and leaving
    it would only let a later reap read it again. Returns how many trees were
    signalled.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    killed = 0
    skipped: list[int] = []
    for pid, recorded in parse(raw):
        if recorded is not None and await _kill_pinned(pid, recorded):
            killed += 1
        else:
            # Already gone, recycled onto another process, or written without an
            # identity. None of those is a backend this record can vouch for.
            skipped.append(pid)
    with contextlib.suppress(OSError):
        path.unlink()
    if killed or skipped:
        logger.warning(
            "mcp-gateway: reaped %d orphaned backend tree(s) from %s (%s); "
            "left %d entr(ies) whose identity did not match: %s",
            killed,
            path,
            reason,
            len(skipped),
            skipped,
        )
    return killed
