"""Whether a pod HOME is still in use, judged from the pod rather than from systemd."""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import gateway_lock
from kiro_crew.pod import runtime

#: ``(pid, link targets)`` of one process, as :func:`proc_links` reads it.
ProcLinks = list[tuple[int, list[str]]]


def proc_links(proc_root: str = "/proc") -> ProcLinks:
    """The ``cwd`` and open-descriptor targets of every process but this one,
    as far as this user may read them."""
    try:
        pids = sorted(int(e) for e in os.listdir(proc_root) if e.isdigit())
    except OSError:
        return []
    out: ProcLinks = []
    for pid in pids:
        if pid == os.getpid():
            continue
        base = f"{proc_root}/{pid}"
        names = ["cwd"]
        try:
            names += [f"fd/{fd}" for fd in os.listdir(f"{base}/fd")]
        except OSError:
            pass
        targets: list[str] = []
        for link in names:
            try:
                targets.append(os.readlink(f"{base}/{link}"))
            except OSError:
                continue
        out.append((pid, targets))
    return out


def reclaim_blocker(home: Path, links: ProcLinks) -> str | None:
    """Why *home* may still be in use, or ``None`` when nothing is seen using it.

    Two signals, both the pod's own:

    * the HOME's ``gateway.lock``, which every gateway holds with an exclusive
      ``flock`` for its whole life (:mod:`kiro_crew.gateway_lock`). A recycled pid
      or port cannot claim a kernel lock, as it could fool a pid file or a
      ``/health`` probe. A probe that cannot answer blocks.
    * a process in *links* whose ``cwd`` or an open descriptor is under the HOME:
      a gateway too old to take the lock, or a child that outlived one.

    A ``/proc`` entry this user cannot read (another user's process, or one that
    made itself non-dumpable) shows no links and blocks nothing. Refusing on it
    would refuse every reclaim on a host running ``systemd --user``, whose own
    ``(sd-pam)`` is that shape.
    """
    try:
        holder = gateway_lock.lock_holder(home)
    except gateway_lock.LockProbeError as exc:
        return f"whether a gateway still serves it cannot be determined ({exc})"
    if holder.pid is not None:
        return f"a gateway (pid {holder.pid}) still holds its {gateway_lock.LOCK_FILENAME}"
    root = os.path.realpath(home)
    held = [
        pid
        for pid, targets in links
        if any(t == root or t.startswith(root + os.sep) for t in targets)
    ]
    if held:
        return f"{len(held)} process(es) hold files under it (pid {', '.join(map(str, held[:5]))})"
    return None


def unused(homes: list[Path]) -> list[Path]:
    """The *homes* nothing is seen using, from one ``/proc`` pass.

    Every one of them off systemd: launchd and Task Scheduler keep a per-pod
    definition, whose presence already says a name is installed.
    """
    if runtime.IS_MACOS or runtime.IS_WINDOWS or not homes:
        return homes
    links = proc_links()
    return [h for h in homes if reclaim_blocker(h, links) is None]
