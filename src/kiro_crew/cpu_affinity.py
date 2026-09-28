"""How many CPUs this process may actually run on.

A leaf on purpose: stdlib only, no intra-package imports, so the cold paths that
need it pay nothing for it -- the embedding download thread, the embedding
backend factory, and the STT engine, whose own module imports ``numpy`` at module
level and so cannot be imported *from*.
"""

from __future__ import annotations

import os


def affinity_cpu_count() -> int | None:
    """Return the core count this process is allowed to use, or ``None``.

    ``os.sched_getaffinity`` rather than ``os.cpu_count``: under a CPU-set
    restriction (``--cpuset-cpus``, ``taskset``, a cgroup ``cpuset`` controller)
    the latter reports the whole machine, which is exactly the environment that
    over-threads worst. Falls back to ``os.cpu_count`` where affinity is
    unavailable (macOS, Windows).

    A CFS **quota** -- ``docker run --cpus``, ``cpu.max`` -- sets no affinity mask,
    so it is invisible here and a process under one reads the host's cores. A limit
    a scheduler turns into an exclusive cpuset instead IS a mask and is seen: a
    Kubernetes Guaranteed pod with an integer CPU limit, on a node running the
    static CPU manager policy, reads its pinned count here.

    ``None`` means no source could answer, and is deliberately not collapsed to
    1 so a caller can tell an unknown count from a one-core host -- what a
    drop-in replacement for ``os.cpu_count`` has to preserve.
    """
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0)) or None
        except OSError:
            pass
    return os.cpu_count()
