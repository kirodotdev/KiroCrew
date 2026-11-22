"""Children a process image hands to its ``os.execv`` successor, and their reaping.

An in-app restart replaces the gateway's image with ``os.execv``, which keeps the
pid. Every child the old image started is still this pid's child afterwards, but
the ``Popen`` objects and asyncio watchers that would have collected them are gone
with the old image. Their pipes and terminals close on exec, so most of them exit
within a moment, and each one then stays a zombie until the gateway itself exits:
the realpath resolver pool, the terminal shells and other helpers, one set per
restart.

So the old image names its children in ``REEXEC_CHILDREN_ENV`` right before the
exec (``platform_compat_runtime._publish_children_for_successor``, called by the
two reexec seams, so no module beyond the compatibility layer has to be loadable
at exec time), and the new image collects exactly those pids (:func:`adopt_inherited_children`,
called at gateway boot). It never waits on any other pid: everything the new image
spawns keeps its own owner. A pid named here cannot be reused by a stranger while
it is unreaped, because an unreaped child keeps its pid; once it is reaped (or the
kernel says it is not our child) it is dropped from the list and never touched
again.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Iterable

from kiro_crew.platform_compat_native import IS_POSIX
from kiro_crew.platform_compat_runtime import REEXEC_CHILDREN_ENV

logger = logging.getLogger(__name__)

# How often the reaper looks at children that were still running at the exec.
# Most exit at once (their pipes close on exec) and are collected by the first
# pass; this only paces the few that take longer, such as a shell finishing up.
_REAP_INTERVAL_SECONDS = 5.0


def take_inherited_children(environ: dict[str, str] | None = None) -> list[int]:
    """Pop and parse :data:`REEXEC_CHILDREN_ENV`, so no descendant inherits it."""
    env = os.environ if environ is None else environ
    raw = env.pop(REEXEC_CHILDREN_ENV, "")
    pids: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit() and int(token) > 0:
            pids.append(int(token))
    return pids


def reap_once(
    pids: Iterable[int],
    waitpid: Callable[[int, int], tuple[int, int]] | None = None,
) -> list[int]:
    """Collect every listed child that has exited; return the ones still running.

    ``ChildProcessError`` means the pid is not our child (any more), so it is
    dropped as well.
    """
    wait = waitpid or os.waitpid
    remaining: list[int] = []
    for pid in pids:
        try:
            reaped, _status = wait(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        except OSError:
            remaining.append(pid)
            continue
        if reaped == 0:
            remaining.append(pid)
    return remaining


def adopt_inherited_children(
    environ: dict[str, str] | None = None,
    *,
    interval: float = _REAP_INTERVAL_SECONDS,
    stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Reap, on a daemon thread, the children the previous image named.

    Only the hand-off is read on the calling thread (one environment pop, so no
    child spawned meanwhile inherits it); every ``waitpid`` runs on the thread,
    off the boot path. Returns the thread, or ``None`` when no child was named.
    The thread ends as soon as every listed child is collected.
    """
    if not IS_POSIX:
        return None
    pids = take_inherited_children(environ)
    if not pids:
        return None
    halt = stop or threading.Event()

    def _run() -> None:
        left = reap_once(pids)
        collected = len(pids) - len(left)
        if collected:
            logger.info("reaped %d child process(es) left by the previous image", collected)
        while left and not halt.wait(interval):
            left = reap_once(left)

    thread = threading.Thread(target=_run, name="kirocrew-reexec-reaper", daemon=True)
    thread.start()
    return thread
