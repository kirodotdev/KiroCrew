"""Host-level budget for every backend process the gateway daemon is answerable for.

The daemon starts backends three ways -- pooled (``BackendPool.get_or_create``),
connection-private (``BackendPool.acquire_exclusive``) and, indirectly, the
per-session exec a stub runs after a ``rejected, fallback: true`` reply. Only the
first of those was ever bounded, and only by ``max_backends``, which is a
resident-pool ceiling and says nothing about what the host can carry. A private
backend deliberately skips that ceiling (exclusivity is a topology property),
and a fallback exec is a process the daemon never sees again. Under a wide
fan-out that is how one host ends up with a thousand MCP processes and a
saturated disk.

:class:`HostBudget` charges all three identically: a fixed estimate per backend
(``procs``, ``rss_mb``, ``fds``) is RESERVED before the process is spawned and
RELEASED when the process has been reaped -- not when ``shutdown`` returns,
because ``shutdown`` returns after a SIGKILL whether or not the target died, and
a process in uninterruptible sleep still holds its memory and descriptors. The
budget is admission, not measurement: it never inspects a live process, so it
cannot be fooled by one that lies about itself, and it costs nothing per call.

Single event loop, single writer: every method is synchronous and touches plain
counters. A ceiling of ``0`` means "unbounded" for that dimension, so an
operator can bound processes alone and leave memory to the OS.

``max_fds`` counts the daemon's OWN descriptors per backend (the three pipes),
which is what protects the daemon from ``EMFILE`` on its next accept -- a
failure that takes every attached session down at once. It does not model the
backend's descriptors: those belong to the backend's own rlimit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: Descriptors the daemon holds per spawned backend: stdin, stdout and stderr
#: pipes. The stub's own connection is counted by the transport, not here.
FDS_PER_BACKEND = 3

#: Working estimate of one MCP backend's resident set. Used ONLY to derive an
#: automatic process ceiling from available memory; a charged backend's
#: ``rss_mb`` is a reservation against ``max_rss_mb``, never a measurement.
DEFAULT_BACKEND_RSS_MB = 150

#: Share of the daemon's descriptor soft limit that backends may consume when
#: ``max_fds`` is derived automatically. The remainder is for stub connections,
#: the listening endpoint, logs and the diagnostic files.
_AUTO_FD_SHARE = 0.6

#: Floor on an automatically derived process ceiling, so a host whose memory
#: probe answers "nothing" still admits a working session's worth of backends
#: rather than refusing every spawn.
_AUTO_PROCS_FLOOR = 16


class HostBudgetExhausted(RuntimeError):
    """A reservation would exceed a ceiling. Carries which one.

    Raised BEFORE anything is spawned, so the caller has nothing to reap; the
    connection handler turns it into a ``rejected`` frame of class ``capacity``.
    """

    def __init__(self, dimension: str, wanted: int, in_use: int, ceiling: int) -> None:
        self.dimension = dimension
        self.wanted = wanted
        self.in_use = in_use
        self.ceiling = ceiling
        super().__init__(
            f"host budget exhausted on {dimension}: in_use={in_use} + wanted={wanted} "
            f"> ceiling={ceiling}"
        )


@dataclass(frozen=True)
class HostBudgetLimits:
    """Ceilings for one daemon. ``0`` in any dimension means unbounded."""

    max_procs: int = 0
    max_rss_mb: int = 0
    max_fds: int = 0

    def __post_init__(self) -> None:
        for name in ("max_procs", "max_rss_mb", "max_fds"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, got {value!r}")


def _nofile_soft_limit() -> int:
    """The daemon's open-file soft limit, or ``0`` where there is none.

    A module-level seam over :func:`platform_compat.nofile_soft_limit` so the
    budget's tests (and the adaptive controller's fd signal) pin the value in
    one place; the ``resource`` module is touched only by ``platform_compat``.
    """
    return platform_compat.nofile_soft_limit()


def resolve_limits(
    *,
    max_procs: int,
    max_rss_mb: int,
    max_fds: int,
    available_mb: Optional[float],
    max_backends: int,
) -> HostBudgetLimits:
    """Turn configured ceilings into effective ones, deriving ``0`` = auto.

    ``available_mb`` is the host's available memory as sampled by the caller
    (the gateway process's ``resource_status`` probe, passed on the daemon's
    argv); ``None`` means the probe was unavailable and the memory-derived
    dimensions fall back to their floors. ``max_backends`` is the resident pool
    ceiling: the automatic process ceiling is never below it, so the budget
    cannot refuse a spawn the pool would have admitted on a host with room.

    Only ``procs`` and ``fds`` are derived; an automatic ``rss_mb`` ceiling is
    left unbounded, because the process ceiling derived from the same sample
    already bounds the reservation and a second bound on the same estimate would
    double-count it.
    """
    procs = max_procs
    if procs == 0:
        if available_mb is not None and available_mb > 0:
            procs = int(available_mb // DEFAULT_BACKEND_RSS_MB)
        procs = max(procs, _AUTO_PROCS_FLOOR, max_backends)
    fds = max_fds
    if fds == 0:
        soft = _nofile_soft_limit()
        if soft > 0:
            fds = max(FDS_PER_BACKEND * _AUTO_PROCS_FLOOR, int(soft * _AUTO_FD_SHARE))
    return HostBudgetLimits(max_procs=procs, max_rss_mb=max_rss_mb, max_fds=fds)


class HostCharge:
    """One reservation. ``release`` is idempotent and safe from any path.

    The charge outlives the code that took it: a spawn path releases it on
    failure, a reap watcher releases it once the process is gone, and a drain
    releases whatever is left. Any of those may run twice or race; the first
    release wins and the rest are no-ops.
    """

    __slots__ = ("_budget", "label", "kind", "procs", "rss_mb", "fds", "_released")

    def __init__(
        self,
        budget: "HostBudget",
        *,
        label: str,
        kind: str,
        procs: int,
        rss_mb: int,
        fds: int,
    ) -> None:
        self._budget = budget
        self.label = label
        self.kind = kind
        self.procs = procs
        self.rss_mb = rss_mb
        self.fds = fds
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._budget._release(self)


class HostBudget:
    """Counters plus ceilings. See the module docstring for the contract."""

    def __init__(
        self,
        limits: HostBudgetLimits,
        *,
        rss_mb_per_backend: int = DEFAULT_BACKEND_RSS_MB,
        fds_per_backend: int = FDS_PER_BACKEND,
    ) -> None:
        self._limits = limits
        self._rss_mb_per_backend = max(0, int(rss_mb_per_backend))
        self._fds_per_backend = max(0, int(fds_per_backend))
        self._procs = 0
        self._rss_mb = 0
        self._fds = 0
        self._charges: set[HostCharge] = set()
        self._rejections = 0
        self._by_kind: dict[str, int] = {}
        # Residency that is OBSERVED but never charged, by kind and by
        # reporting label (e.g. harness-native subtasks per parent session):
        # it lives inside a process already charged above, so it appears on
        # the snapshot for health/telemetry and takes part in no ceiling.
        self._uncharged: dict[str, dict[str, int]] = {}

    @property
    def limits(self) -> HostBudgetLimits:
        return self._limits

    @property
    def procs_in_use(self) -> int:
        return self._procs

    @property
    def rss_mb_in_use(self) -> int:
        return self._rss_mb

    @property
    def fds_in_use(self) -> int:
        return self._fds

    def reserve(
        self,
        *,
        label: str,
        kind: str = "pooled",
        procs: int = 1,
        rss_mb: Optional[int] = None,
        fds: Optional[int] = None,
    ) -> HostCharge:
        """Charge one backend-to-be. Raises :class:`HostBudgetExhausted`.

        ``kind`` is diagnostic only (``pooled``, ``exclusive``, ``fallback``,
        ``prewarm``): every kind is charged identically, which is the point.
        """
        rss = self._rss_mb_per_backend if rss_mb is None else max(0, int(rss_mb))
        fd = self._fds_per_backend if fds is None else max(0, int(fds))
        procs = max(0, int(procs))
        self._check("procs", procs, self._procs, self._limits.max_procs)
        self._check("rss_mb", rss, self._rss_mb, self._limits.max_rss_mb)
        self._check("fds", fd, self._fds, self._limits.max_fds)
        charge = HostCharge(self, label=label, kind=kind, procs=procs, rss_mb=rss, fds=fd)
        self._procs += procs
        self._rss_mb += rss
        self._fds += fd
        self._charges.add(charge)
        self._by_kind[kind] = self._by_kind.get(kind, 0) + 1
        return charge

    def _check(self, dimension: str, wanted: int, in_use: int, ceiling: int) -> None:
        if ceiling <= 0 or wanted <= 0:
            return
        if in_use + wanted > ceiling:
            self._rejections += 1
            raise HostBudgetExhausted(dimension, wanted, in_use, ceiling)

    def _release(self, charge: HostCharge) -> None:
        if charge not in self._charges:
            return
        self._charges.discard(charge)
        self._procs = max(0, self._procs - charge.procs)
        self._rss_mb = max(0, self._rss_mb - charge.rss_mb)
        self._fds = max(0, self._fds - charge.fds)
        remaining = self._by_kind.get(charge.kind, 0) - 1
        if remaining > 0:
            self._by_kind[charge.kind] = remaining
        else:
            self._by_kind.pop(charge.kind, None)

    def release_all(self) -> int:
        """Drop every outstanding charge (daemon drain). Returns how many."""
        charges = list(self._charges)
        for charge in charges:
            charge.release()
        return len(charges)

    def report_uncharged(self, kind: str, count: int, *, label: str = "") -> None:
        """Record ``count`` residents of ``kind`` under ``label`` as OBSERVED,
        NOT CHARGED (RFC §14.8: harness-native subtasks live inside their
        parent's already-charged runtime process).

        Idempotent per ``(kind, label)`` — the latest report replaces the
        previous one, so a parent re-reporting each turn never accumulates.
        A zero or negative ``count`` removes the label. Touches no ceiling and
        no ``procs`` / ``rss_mb`` / ``fds`` counter: :meth:`reserve` admits
        exactly what it admitted before the report. Labels are bounded like
        the roster they mirror (256 per kind) so a report storm cannot grow
        the daemon's memory through this table.
        """
        kind = str(kind or "unknown")
        label = str(label or "")
        per_kind = self._uncharged.setdefault(kind, {})
        if count <= 0:
            per_kind.pop(label, None)
            if not per_kind:
                self._uncharged.pop(kind, None)
            return
        if label not in per_kind and len(per_kind) >= 256:
            return
        per_kind[label] = int(count)

    def uncharged(self, kind: str) -> int:
        """Total observed-but-uncharged residents of ``kind`` across labels."""
        return sum(self._uncharged.get(str(kind), {}).values())

    def snapshot(self) -> dict[str, Any]:
        """Point-in-time counters for the ``stats`` frame and tests.

        ``uncharged`` is reporting only (``{kind: total}``): it is what a
        health surface renders as "native children (uncharged)" and it is
        absent from every charged counter above it.
        """
        return {
            "procs": self._procs,
            "rss_mb": self._rss_mb,
            "fds": self._fds,
            "max_procs": self._limits.max_procs,
            "max_rss_mb": self._limits.max_rss_mb,
            "max_fds": self._limits.max_fds,
            "charges": len(self._charges),
            "by_kind": dict(self._by_kind),
            "rejections": self._rejections,
            "uncharged": {kind: sum(labels.values()) for kind, labels in self._uncharged.items()},
        }
