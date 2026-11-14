"""``kirocrew.chat_runtime_share.*`` -- does chat runtime sharing help, and does it stall.

The ``chat_runtime_sharing`` feature preview places several dashboard chats on one
agent process. Before it can be on by default, a week of numbers has to answer four
questions: how many gateways turned it on, how many processes and how much memory
it saved, whether a chat's turn starts later when it shares a process, and how many
chats one crash takes down. These instruments answer them and nothing else.

=================================================  =======  ======================================
name                                               kind     attributes
=================================================  =======  ======================================
``kirocrew.chat_runtime_share.toggled``            counter  ``state`` (on / off)
``kirocrew.chat_runtime_share.placed``             counter  ``outcome``, ``reason``, ``tenants``
``kirocrew.chat_runtime_share.processes``          gauge    none: live processes placed by sharing
``kirocrew.chat_runtime_share.tenants``            gauge    none: chat leases on those processes
``kirocrew.chat_runtime_share.rss_sampled``        counter  ``tenants``, ``rss``
``kirocrew.chat_runtime_share.reclaimed``          counter  ``reason`` (rss)
``kirocrew.chat_runtime_share.process_died``       counter  ``expected``, ``tenants``
``kirocrew.chat_runtime_share.turn_started``       counter  ``tenants``, ``delay``, ``harness``
``kirocrew.chat_runtime_share.turn_ended``         counter  ``tenants``, ``outcome``, ``harness``
``kirocrew.chat_runtime_share.kas_queued``         counter  ``tenants``
``kirocrew.kas.engine_install``                    counter  ``outcome``, ``caller``
=================================================  =======  ======================================

Whether the preview is on is the existing ``kirocrew.inventory.config.toggle`` gauge
with ``key=preview_chat_runtime_sharing`` (``inventory_gauges.read_config_toggles``), the
way every other switch reports adoption.

**Every attribute value is closed.** Each set is a tuple below, and every input
outside it maps to :data:`OTHER`. No session key, pid, path, model id, prompt or
config value reaches an attribute. ``tenants`` is the chat lease count on the
process, bucketed to ``0`` / ``1`` / ``2`` / ``3`` / ``4+``: ``0`` is a turn whose
session holds no chat lease on its process (a subagent on its host chat's process, or
a background runtime), ``1`` is a chat
alone on its process (unshared, or a shared process with one tenant left), and
``2`` and up is a shared process.

**A distribution is a bucket-labelled counter, not a histogram,** because
``product_metrics.PRODUCT_METRICS`` admits counters and gauges only: a histogram's
buckets may not survive the receiving backend. The edges are
:data:`DELAY_BUCKET_EDGE_SECS` and :data:`RSS_BUCKET_EDGE_MB`, stated in
``docs/system-specs/modules/metrics.md`` beside each row.

**Recording never changes what it measures.** ``emit_counter`` swallows every
failure of the emit after a debug log, and the bucketing below is total: every
input, including one that is not a number, maps onto a member of its own tuple.
Every call site records after the step it measures has happened.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

from kiro_crew.metrics.events import emit_counter
from kiro_crew.metrics.harness import ATTR_HARNESS, harness_for_backend
from kiro_crew.runtime_ownership import RUNTIME_OWNERSHIP, outstanding_leases

if TYPE_CHECKING:
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Instrument names
# ---------------------------------------------------------------------------

TOGGLED = "kirocrew.chat_runtime_share.toggled"
PLACED = "kirocrew.chat_runtime_share.placed"
GAUGE_PROCESSES = "kirocrew.chat_runtime_share.processes"
GAUGE_TENANTS = "kirocrew.chat_runtime_share.tenants"
RSS_SAMPLED = "kirocrew.chat_runtime_share.rss_sampled"
RECLAIMED = "kirocrew.chat_runtime_share.reclaimed"
PROCESS_DIED = "kirocrew.chat_runtime_share.process_died"
TURN_STARTED = "kirocrew.chat_runtime_share.turn_started"
TURN_ENDED = "kirocrew.chat_runtime_share.turn_ended"
KAS_QUEUED = "kirocrew.chat_runtime_share.kas_queued"
KAS_ENGINE_INSTALL = "kirocrew.kas.engine_install"

ALL_METRIC_NAMES: tuple[str, ...] = (
    TOGGLED,
    PLACED,
    GAUGE_PROCESSES,
    GAUGE_TENANTS,
    RSS_SAMPLED,
    RECLAIMED,
    PROCESS_DIED,
    TURN_STARTED,
    TURN_ENDED,
    KAS_QUEUED,
    KAS_ENGINE_INSTALL,
)

# ---------------------------------------------------------------------------
# Closed attribute values
# ---------------------------------------------------------------------------

OTHER = "other"
NONE = "none"

TOGGLE_STATES: tuple[str, ...] = ("on", "off")

PLACED_OUTCOMES: tuple[str, ...] = ("spawned", "joined", "refused")

#: Why an eligible-looking start kept its own process: the branch names
#: ``chat_runtime_sharing.chat_sharing_ineligible_reason`` returns, plus
#: ``native_subagents`` (``providers/chat_share.py``), plus ``none`` on a placement.
#: ``sharing_disabled`` is listed because the function can return it, though the
#: placement path returns before asking it while the preview is off.
#: ``test_chat_runtime_share_metrics`` holds this set equal to the function's own.
REFUSAL_REASONS: tuple[str, ...] = (
    "sharing_disabled",
    "backend_not_chat_shareable",
    "kas_engine_not_shareable",
    "session_environment",
    "memory_mode_not_shareable",
    "origin_not_dashboard",
    "member_session",
    "native_subagents",
)

TENANT_BUCKETS: tuple[str, ...] = ("0", "1", "2", "3", "4+")

#: Prompt written to first event of that turn back, each edge INCLUSIVE.
DELAY_BUCKET_EDGE_SECS: tuple[tuple[float, str], ...] = (
    (1.0, "le_1s"),
    (2.0, "le_2s"),
    (5.0, "le_5s"),
    (10.0, "le_10s"),
    (20.0, "le_20s"),
    (30.0, "le_30s"),
    (60.0, "le_60s"),
)
DELAY_BUCKETS: tuple[str, ...] = tuple(n for _, n in DELAY_BUCKET_EDGE_SECS) + ("gt_60s",)

#: One shared process's tree RSS at an RSS watchdog tick, MiB, each edge INCLUSIVE.
RSS_BUCKET_EDGE_MB: tuple[tuple[int, str], ...] = (
    (512, "le_512m"),
    (1024, "le_1g"),
    (2048, "le_2g"),
    (4096, "le_4g"),
    (8192, "le_8g"),
)
RSS_BUCKETS: tuple[str, ...] = tuple(n for _, n in RSS_BUCKET_EDGE_MB) + ("gt_8g",)

RECLAIM_REASONS: tuple[str, ...] = ("rss",)

BOOL_VALUES: tuple[str, ...] = ("true", "false")

#: How a turn ended is ``metrics.turns.turn_outcome`` and which harness ran it is
#: ``metrics.harness.harness_for_backend``: the same classifiers
#: ``kirocrew.turn.completed`` uses, so the two series split turns the same way.

INSTALL_OUTCOMES: tuple[str, ...] = ("installed", "failed", "host_unsupported")
INSTALL_CALLERS: tuple[str, ...] = ("spawn", "dashboard")

# ---------------------------------------------------------------------------
# Bucketing (total: every input lands inside its own tuple)
# ---------------------------------------------------------------------------


def _closed(value: Any, allowed: tuple[str, ...]) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return float(value)


def tenants_bucket(count: Any) -> str:
    """A chat lease count as a member of :data:`TENANT_BUCKETS`, else ``other``."""
    n = _number(count)
    if n is None or n < 0:
        return OTHER
    return "4+" if n >= 4 else str(int(n))


def delay_bucket(seconds: Any) -> str:
    """A turn's start delay as a member of :data:`DELAY_BUCKETS`, else ``other``.

    A negative span (the monotonic clock cannot go back, so only a caller bug
    gives one) is read as zero.
    """
    n = _number(seconds)
    if n is None:
        return OTHER
    n = max(0.0, n)
    for edge, name in DELAY_BUCKET_EDGE_SECS:
        if n <= edge:
            return name
    return "gt_60s"


def rss_bucket(mib: Any) -> str:
    """A tree RSS in MiB as a member of :data:`RSS_BUCKETS`, else ``other``."""
    n = _number(mib)
    if n is None or n < 0:
        return OTHER
    for edge, name in RSS_BUCKET_EDGE_MB:
        if n <= edge:
            return name
    return "gt_8g"


def _turn_outcome(stop_reason: Any) -> str:
    """``metrics.turns.turn_outcome`` over *stop_reason*; a non-string reads as an error.

    Imported here, not at module scope: ``metrics.turns`` imports
    ``metrics.provider``, and this module is imported by the ACP turn loop.
    """
    from kiro_crew.metrics.turns import turn_outcome

    if stop_reason is not None and not isinstance(stop_reason, str):
        stop_reason = repr(type(stop_reason))
    return turn_outcome(stop_reason)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


def chat_leases_on(runtime: Any, session_key: Any = "") -> int:
    """The chat leases on *runtime* when *session_key* holds one of them, else ``0``.

    A subagent or background session runs on a process it holds no lease on (its
    host chat's, or its own unleased one), so it reads ``0`` rather than its
    host's count. Never raises.
    """
    try:
        if not isinstance(session_key, str) or not session_key:
            return 0
        pid = getattr(runtime, "pid", None)
        if not isinstance(pid, int) or session_key not in RUNTIME_OWNERSHIP.lease_keys_on_pid(pid):
            return 0
        return int(outstanding_leases(runtime))
    except Exception:  # noqa: BLE001 -- a read for telemetry never breaks a turn
        logger.debug("chat lease read failed", exc_info=True)
        return 0


def record_toggled(on: bool) -> None:
    """The preview moved to *on* (any writer: Settings, CLI, a hand edit)."""
    emit_counter(TOGGLED, {"state": "on" if on else "off"})


def record_refused(reason: Any) -> None:
    """An eligible-looking chat start, preview on, kept its own process."""
    _note_sharing_in_use()
    emit_counter(
        PLACED,
        {"outcome": "refused", "reason": _closed(reason, REFUSAL_REASONS), "tenants": NONE},
    )


def record_placed(*, joined: bool, tenants: Any) -> None:
    """A chat start landed: founded a process or joined one, with *tenants* after it."""
    _note_sharing_in_use()
    emit_counter(
        PLACED,
        {
            "outcome": "joined" if joined else "spawned",
            "reason": NONE,
            "tenants": tenants_bucket(tenants),
        },
    )


def record_rss_sample(*, tenants: Any, rss_mb: Any) -> None:
    """One RSS watchdog reading of a shared process (two or more chat leases)."""
    emit_counter(RSS_SAMPLED, {"tenants": tenants_bucket(tenants), "rss": rss_bucket(rss_mb)})


def record_reclaimed(reason: str) -> None:
    """A shared process was closed to new chats so it drains (over its RSS ceiling)."""
    emit_counter(RECLAIMED, {"reason": _closed(reason, RECLAIM_REASONS)})


def record_process_died(*, expected: Any, tenants: Any) -> None:
    """An agent process died; *tenants* is the chat leases it held at that instant."""
    emit_counter(
        PROCESS_DIED,
        {"expected": "true" if expected is True else "false", "tenants": tenants_bucket(tenants)},
    )


def record_turn_started(*, tenants: Any, delay_secs: Any, backend: Any) -> None:
    """A turn's first event arrived *delay_secs* after its prompt was written."""
    emit_counter(
        TURN_STARTED,
        {
            "tenants": tenants_bucket(tenants),
            "delay": delay_bucket(delay_secs),
            ATTR_HARNESS: harness_for_backend(backend),
        },
    )


def record_turn_ended(*, tenants: Any, stop_reason: Any, backend: Any) -> None:
    """A turn's terminal reached its consumer; *tenants* as read when it started."""
    emit_counter(
        TURN_ENDED,
        {
            "tenants": tenants_bucket(tenants),
            "outcome": _turn_outcome(stop_reason),
            ATTR_HARNESS: harness_for_backend(backend),
        },
    )


def record_kas_queued(*, tenants: Any) -> None:
    """KAS told a running prompt it waits for another session's turn (``kind: queued``).

    The engine sends this when a turn waits for its one active model slot
    (``AgentExecutionWaitingForActiveSlot``). From 0.66.104 a chat turn never
    waits there, so on a shared process this must stay at about zero.
    """
    emit_counter(KAS_QUEUED, {"tenants": tenants_bucket(tenants)})


def record_engine_install(*, outcome: str, caller: Any) -> None:
    """One attempt to download the pinned KAS engine finished with *outcome*."""
    emit_counter(
        KAS_ENGINE_INSTALL,
        {"outcome": _closed(outcome, INSTALL_OUTCOMES), "caller": _closed(caller, INSTALL_CALLERS)},
    )


# ---------------------------------------------------------------------------
# Gauges: processes placed by sharing, and the chats on them
# ---------------------------------------------------------------------------

#: True once this process placed or refused a chat with the preview on. Until
#: then the gauges publish nothing, so a CLI or a gatewayd process, which never
#: places a chat, exports no series of zeros.
_in_use = False


def _note_sharing_in_use() -> None:
    global _in_use
    _in_use = True


def read_census(
    entries: Callable[[], Iterable[int]] | None = None,
) -> tuple[int, int] | None:
    """``(processes, tenants)`` over live runtimes placed by sharing, or ``None``.

    ``None`` until this process has used sharing. *entries* replaces the ownership
    table's own reader (``RuntimeOwnership.keyed_lease_counts``) in tests only.
    """
    if not _in_use:
        return None
    if entries is None:
        entries = RUNTIME_OWNERSHIP.keyed_lease_counts
    counts = [int(n) for n in entries() if n > 0]
    return len(counts), sum(counts)


def _census_callback(index: int) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        try:
            census = read_census()
        except Exception:  # noqa: BLE001 -- never let a read break the exporter
            logger.debug("chat runtime share census failed", exc_info=True)
            return
        if census is not None:
            yield Observation(census[index])

    return _callback


def register_chat_runtime_share_gauges(meter: "Meter") -> None:
    """Register the two census gauges on *meter* (once per live provider build)."""
    from kiro_crew.metrics.schema import validate_name

    for name in ALL_METRIC_NAMES:
        validate_name(name)
    meter.create_observable_gauge(
        GAUGE_PROCESSES,
        callbacks=[_census_callback(0)],
        unit="1",
        description="Live agent processes placed by chat runtime sharing",
    )
    meter.create_observable_gauge(
        GAUGE_TENANTS,
        callbacks=[_census_callback(1)],
        unit="1",
        description="Chat leases held on live processes placed by chat runtime sharing",
    )
