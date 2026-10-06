"""Host sizing for the start limits, and ``agent.session_start_concurrency = "auto"``.

Two layers, so every ``auto`` start limit sizes from the same reading of the host:

* :class:`HostCapacity` / :func:`host_capacity` -- the cores this process may use
  (affinity mask capped by a cgroup v2 ``cpu.max`` quota) and the available
  memory, read ONCE per process and then fixed.
* :func:`resolve_session_start_sizing` -- the SessionStartGate's width
  (``acp/runtime_start.py``), which bounds how many ACP ``session/new`` requests
  are outstanding at once.

One kirocrew-agent ``session/new`` costs about 12 to 18 CPU-seconds over 4 to 6 s
(3 to 4 cores while it runs) and a session tree holds about 2.6 GB RSS, so the
gate width a host can serve without every start slowing is roughly ``cpus // 4``
and ``available_GB // 3``, whichever is smaller, clamped to ``[2, 16]``. Measured
on a 128-vCPU host: at 2 the starts serialise to one per 1.9 s, at 8 each stays
at its 3.8 s isolated cost.

Static, not adaptive: the reading is taken once (gateway boot) and cached. The
adaptive loops on this resource are the gatewayd spawn gate and the
execution-cap controller; a second adapting loop would oscillate against them.

An explicit integer in config always wins; ``auto`` is the default.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from kiro_crew.cpu_affinity import affinity_cpu_count

logger = logging.getLogger(__name__)

AUTO = "auto"

#: Clamp for the auto-sized session-start width. The floor is the historical
#: fixed default, so no host is sized below what it ran at before.
AUTO_FLOOR = 2
AUTO_CEILING = 16
#: Cores one in-flight ``session/new`` occupies.
CPUS_PER_START = 4
#: GB of available memory one started session tree holds.
GB_PER_START = 3

_CGROUP_V2_ROOT = Path("/sys/fs/cgroup")
_READ_CAP = 4096


# ── Host reading ──────────────────────────────────────────────────────────────


def parse_cpu_max(text: str) -> float | None:
    """CPUs granted by one cgroup v2 ``cpu.max`` line, or ``None`` for no quota."""
    fields = text.split()
    if not fields or fields[0] == "max":
        return None
    try:
        quota = int(fields[0])
        period = int(fields[1]) if len(fields) > 1 else 100_000
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def cgroup_cpu_quota(
    proc_self_cgroup: str | None = None, root: Path = _CGROUP_V2_ROOT
) -> float | None:
    """Tightest cgroup v2 ``cpu.max`` quota over this process's cgroup ancestry.

    ``affinity_cpu_count`` cannot see a CFS quota (``docker run --cpus``,
    systemd ``CPUQuota=``): it sets no affinity mask. Any ancestor's quota
    bounds the process, so the smallest along the path to *root* binds. Inside
    a cgroup namespace the membership path is ``/`` and the walk reads only
    *root*, which is where the container's own limit is mounted.
    ``None`` when there is no quota, no cgroup v2, or nothing is readable.
    """
    if proc_self_cgroup is None:
        try:
            with open("/proc/self/cgroup", encoding="utf-8") as fh:
                proc_self_cgroup = fh.read(_READ_CAP)
        except (OSError, UnicodeDecodeError):
            return None
    membership: PurePosixPath | None = None
    for line in proc_self_cgroup.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and not controllers:
            candidate = PurePosixPath(path.strip())
            # Never let a crafted path walk outside *root*.
            if candidate.is_absolute() and ".." not in candidate.parts:
                membership = candidate
            break
    if membership is None:
        return None
    best: float | None = None
    directory = root.joinpath(*membership.parts[1:])
    while True:
        try:
            with open(directory / "cpu.max", encoding="utf-8") as fh:
                quota = parse_cpu_max(fh.read(_READ_CAP))
        except (OSError, UnicodeDecodeError):
            quota = None
        if quota is not None and (best is None or quota < best):
            best = quota
        if directory == root or directory.parent == directory:
            break
        directory = directory.parent
    return best


def effective_cpus(affinity: int | None, quota: float | None) -> int | None:
    """Affinity cores capped by a CFS quota (rounded down, at least 1)."""
    if quota is None:
        return affinity
    capped = max(1, int(quota))
    return capped if affinity is None else min(affinity, capped)


@dataclass(frozen=True)
class HostCapacity:
    """What this process may use: cores and available memory, ``None`` if unread."""

    affinity_cpus: int | None
    quota_cpus: float | None
    cpus: int | None
    available_gb: float | None

    def describe(self) -> str:
        quota = "none" if self.quota_cpus is None else f"{self.quota_cpus:g}"
        mem = "unknown" if self.available_gb is None else f"{self.available_gb:.1f}GB"
        return f"cpus={self.cpus} affinity={self.affinity_cpus} " f"cpu.max={quota} available={mem}"


def _read_available_gb() -> float | None:
    """Available memory from the same cgroup-clamped probe ``resource_status`` uses."""
    try:
        from kiro_crew.resource_status import _read_available_gb as probe

        gb = probe()
    except Exception:
        logger.debug("available-memory probe failed", exc_info=True)
        return None
    return gb if gb >= 0 else None


def probe_host_capacity() -> HostCapacity:
    """Read the host now. Disk I/O: call off the event loop."""
    affinity = affinity_cpu_count()
    quota = cgroup_cpu_quota()
    return HostCapacity(
        affinity_cpus=affinity,
        quota_cpus=quota,
        cpus=effective_cpus(affinity, quota),
        available_gb=_read_available_gb(),
    )


_host_lock = threading.Lock()
_host_cached: HostCapacity | None = None


def host_capacity() -> HostCapacity:
    """The process's one host reading, probed on first use and then fixed.

    Every ``auto`` start limit sizes from this one reading, so the limits a
    gateway runs at agree with each other and with its boot log.
    """
    global _host_cached
    with _host_lock:
        if _host_cached is None:
            _host_cached = probe_host_capacity()
        return _host_cached


# ── agent.session_start_concurrency ───────────────────────────────────────────


def size_session_start_concurrency(cpus: int | None, available_gb: float | None) -> int:
    """The SessionStartGate width ``auto`` gives a host with these inputs; pure.

    ``clamp(min(cpus // CPUS_PER_START, available_gb // GB_PER_START),
    AUTO_FLOOR, AUTO_CEILING)``. An unknown input (``None``, or a negative
    memory figure) drops out of the ``min`` rather than collapsing it, so a
    host whose memory probe fails is still sized by its cores. With neither
    known the floor applies.
    """
    terms: list[int] = []
    if cpus is not None and cpus > 0:
        terms.append(cpus // CPUS_PER_START)
    if available_gb is not None and available_gb >= 0:
        terms.append(int(available_gb // GB_PER_START))
    if not terms:
        return AUTO_FLOOR
    return max(AUTO_FLOOR, min(AUTO_CEILING, min(terms)))


@dataclass(frozen=True)
class SessionStartSizing:
    """The resolved gate width and, for ``auto``, the host reading it came from."""

    limit: int
    source: str  # "auto" (host-sized) or "config" (explicit integer)
    host: HostCapacity | None = None

    def describe(self) -> str:
        if self.host is None:
            return f"{self.limit} (config)"
        return (
            f"{self.limit} (auto: {self.host.describe()}; "
            f"clamp(min(cpus//{CPUS_PER_START}, GB//{GB_PER_START}), "
            f"{AUTO_FLOOR}, {AUTO_CEILING}))"
        )


def is_auto(configured: object) -> bool:
    return isinstance(configured, str) and configured.strip().lower() == AUTO


def resolve_session_start_sizing(configured: object) -> SessionStartSizing:
    """Sizing for a configured ``agent.session_start_concurrency`` value.

    An integer is taken as is (the loader already clamped it to ``1..64``);
    ``"auto"`` sizes from the process's cached :func:`host_capacity`.
    """
    if is_auto(configured):
        host = host_capacity()
        return SessionStartSizing(
            limit=size_session_start_concurrency(host.cpus, host.available_gb),
            source=AUTO,
            host=host,
        )
    return SessionStartSizing(limit=int(configured), source="config")  # type: ignore[call-overload]


def effective_session_start_concurrency(configured: object) -> int:
    """The gate width for *configured* (an int, or ``"auto"``)."""
    return resolve_session_start_sizing(configured).limit


def cached_session_start_concurrency(configured: object) -> int:
    """The gate width for *configured* without probing or waiting; event-loop safe.

    An integer is returned as is. ``"auto"`` is sized from the cached host
    reading when one exists, and is :data:`AUTO_FLOOR` until then. It never
    takes ``_host_lock``: reading the module global is a single reference
    load, and the reading is immutable once published.
    """
    if not is_auto(configured):
        return int(configured)  # type: ignore[call-overload]
    host = _host_cached
    if host is None:
        return AUTO_FLOOR
    return size_session_start_concurrency(host.cpus, host.available_gb)


def _reset_for_tests() -> None:
    global _host_cached
    with _host_lock:
        _host_cached = None
