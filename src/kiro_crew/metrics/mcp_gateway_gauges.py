"""MCP gateway pool gauges -- whether shared MCP backends save processes and memory.

The gateway daemon (``gatewayd``) pools MCP server processes so several sessions
share one backend. Nothing measured the saving: the process gauges read only the
calling process, and the backends are separate child processes. These three
observable gauges read the live pool at collection time:

=================================================  ====  ========  ===========================
name                                               unit  attrs     meaning
=================================================  ====  ========  ===========================
``kirocrew.mcp_gateway.backends.live``             1     ``class``  live backend processes
``kirocrew.mcp_gateway.sessions.attached``         1     ``class``  stub attachments held
``kirocrew.mcp_gateway.backends.rss_bytes``        By    ``class``  summed RSS of those backends
=================================================  ====  ========  ===========================

``sessions.attached - backends.live`` is how many backend processes pooling
avoided; times ``backends.rss_bytes / backends.live`` it estimates bytes saved.
gatewayd's own RSS is the existing ``kirocrew.process.memory.rss_bytes`` of the
resource (``process.pid``) that publishes these gauges, so it has no instrument
here. An attachment is one stub connection. A runtime hosting several sessions opens
one stub per server, so as a SESSION count it is a lower bound (see
``BackendPool.metrics_snapshot_async``), which makes the saving a lower bound too.

``class`` is a closed set: ``first_party`` for a server Kiro Crew itself manages
(``mcp_discovery_managed._MANAGED_SERVER_NAMES``, reused rather than restated),
``third_party`` for any other name, ``other`` when the name is missing. Server
names are read to classify and then dropped; no name, command, path or pid ever
reaches an attribute.

Registration happens in every process's recorder build, inside the consent
gate. Only a process that called :func:`bind_pool` -- gatewayd -- observes
anything; elsewhere each callback returns before reading, so no series exists.
Callbacks run on the metric reader's thread, never on the event loop, and read
the pool through ``BackendPool.all_backends`` (a lock-free snapshot list) and the RSS
through ``platform_compat``. A failing read loses that cycle's samples, never
the reader.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.metrics.schema import validate_attrs, validate_name

if TYPE_CHECKING:
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

logger = logging.getLogger(__name__)

GAUGE_BACKENDS_LIVE = "kirocrew.mcp_gateway.backends.live"
GAUGE_SESSIONS_ATTACHED = "kirocrew.mcp_gateway.sessions.attached"
GAUGE_BACKENDS_RSS = "kirocrew.mcp_gateway.backends.rss_bytes"

ALL_METRIC_NAMES = (
    GAUGE_BACKENDS_LIVE,
    GAUGE_SESSIONS_ATTACHED,
    GAUGE_BACKENDS_RSS,
)

CLASS_FIRST_PARTY = "first_party"
CLASS_THIRD_PARTY = "third_party"
CLASS_OTHER = "other"

#: Classes always published while a pool is bound, so "none right now" reads as
#: 0 rather than as a missing series. ``other`` appears only when it is non-zero.
_ALWAYS_PUBLISHED = (CLASS_FIRST_PARTY, CLASS_THIRD_PARTY)

#: The bound pool, or ``None`` outside gatewayd. Typed loosely: this module must
#: not import the gateway package into every process that builds a recorder.
_pool: Optional[Any] = None


def bind_pool(pool: Any) -> None:
    """Make *pool* the one these gauges read (gatewayd, right after building it)."""
    global _pool
    _pool = pool


def unbind_pool(pool: Any) -> None:
    """Stop reading *pool*, but only if it is still the bound one (daemon shutdown)."""
    global _pool
    if _pool is pool:
        _pool = None


def classify(server_name: object) -> str:
    """Map a server name to the closed ``class`` set. The name is not kept."""
    if not isinstance(server_name, str) or not server_name:
        return CLASS_OTHER
    from kiro_crew.mcp_discovery_managed import _MANAGED_SERVER_NAMES

    return CLASS_FIRST_PARTY if server_name in _MANAGED_SERVER_NAMES else CLASS_THIRD_PARTY


def _empty() -> dict[str, int]:
    return {cls: 0 for cls in _ALWAYS_PUBLISHED}


def read_pool(
    rss_for_pid: Callable[[int], Optional[int]] = platform_compat.proc_rss_bytes_for_pid,
) -> Optional[dict[str, dict[str, int]]]:
    """Per-class ``live`` / ``attached`` / ``rss`` sums, or ``None`` when unbound.

    ``rss`` holds only the classes where at least one live backend gave a
    reading. A pid with no reading (it died between the snapshot and the read, or
    the platform has no per-pid probe -- macOS) adds 0 and is skipped, and a
    class where NO backend could be read is left out: a gap, never a fake zero.
    A class with no live backend reports a true 0.
    """
    pool = _pool
    if pool is None:
        return None
    live = _empty()
    attached = _empty()
    rss = _empty()
    unread: set[str] = set()
    read_any: set[str] = set()
    for backend in pool.all_backends():
        if not backend.is_alive:
            continue
        pid = backend.pid
        refcount = backend.refcount
        cls = classify(backend.pool_key.server_name)
        live[cls] = live.get(cls, 0) + 1
        attached[cls] = attached.get(cls, 0) + max(0, int(refcount))
        reading = rss_for_pid(pid) if pid else None
        if reading:
            rss[cls] = rss.get(cls, 0) + int(reading)
            read_any.add(cls)
        else:
            rss.setdefault(cls, 0)
            unread.add(cls)
    for cls in unread - read_any:
        del rss[cls]
    return {"live": live, "attached": attached, "rss": rss}


def _per_class(field: str) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        try:
            data = read_pool()
        except Exception:  # noqa: BLE001 -- never let a probe break the exporter
            logger.debug("mcp gateway pool read failed", exc_info=True)
            return
        if data is None:
            return
        for cls, value in data[field].items():
            yield Observation(value, attributes=validate_attrs({"class": cls}))

    return _callback


def register_mcp_gateway_gauges(meter: "Meter") -> None:
    """Register the three pool gauges on *meter* (once per live provider build)."""
    for name in ALL_METRIC_NAMES:
        validate_name(name)
    meter.create_observable_gauge(
        GAUGE_BACKENDS_LIVE,
        callbacks=[_per_class("live")],
        unit="1",
        description="Live MCP backend processes held by the gateway, per class",
    )
    meter.create_observable_gauge(
        GAUGE_SESSIONS_ATTACHED,
        callbacks=[_per_class("attached")],
        unit="1",
        description="Stub attachments held on live MCP backends, per class",
    )
    meter.create_observable_gauge(
        GAUGE_BACKENDS_RSS,
        callbacks=[_per_class("rss")],
        unit="By",
        description="Summed current RSS of live MCP backend processes, per class",
    )
