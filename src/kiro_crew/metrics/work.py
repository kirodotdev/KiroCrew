"""``kirocrew.work.*`` -- what the conductor work ledger does, in four counters.

The ledger had no instrument at all: nothing recorded how many goals and items a
conductor opens, how its items end, how long they take, how often work is handed
to a worker, or what workers report back. These four counters answer those
questions and nothing else -- no item id, no slot key, no title, no summary, no
acceptance text ever reaches an attribute. Every attribute value below is a
member of a closed tuple declared in this module, and an unrecognised value maps
to :data:`OTHER` rather than entering the series under its own name.

**Why a counter at the write's commit point, and not a fold.** The crew log's
``work`` fold is the ledger's own record, and a counting fold over it would be
the cheaper source -- it is how session usage is counted. It cannot serve here:
the fold keeps only the newest ``WORK_CLOSED_WINDOW`` closed items whole and
forgets the rest, keeping just a total, so an item's ``state``, ``fails`` and
duration are gone from it by the time a later export reads it. The three
attributes of :data:`WORK_ITEM_CLOSED` only exist at the moment the close
commits, so that is where it is counted.

**Exactly once per successful write.** Each call site sits AFTER its
``work/recorded`` entry has landed in the crew log, which is the point the route
itself treats as the commit: a refused write (a validation refusal, a closed
item, the goal's concurrency cap, a ``done`` with no evidence) returns before
reaching it, and a write whose cache commit could not be recorded is undone and
also returns before it. Nothing here runs on the rebuild path, so
``work_ledger_rebuild`` re-materialising a board from the log re-counts nothing.

**A duration is a counter with a bucket, not a histogram.** The bucket edges are
:data:`DURATION_BUCKET_EDGE_SECS` and they are stated in
``docs/system-specs/modules/metrics.md`` beside the row, so a backend that keeps
no explicit histogram buckets still carries the distribution. ``fails`` and
``depth`` are small integers that could in principle grow, so they are bucketed
the same way rather than emitted raw.

Recording is an in-memory add on a thread the route already owns: no disk write,
no network call, no log line. Telemetry consent gates it, because
``emit_counter`` resolves a no-op recorder when telemetry is off.

Telemetry must never break the instrumented path, and ``emit_counter`` is where
that is enforced: it swallows every failure of the emit after a debug log. This
module adds no second layer over it, because the only code that would sit under
one is the bucketing below, and that is written to be TOTAL -- every input,
including a type that is not a value at all, maps to a declared member of its
own enum rather than raising. Two tests hold that, so it is a checked property
and not an assumption a wrapper would hide.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kiro_crew.metrics.events import emit_counter
from kiro_crew.work_vocab import WORK_BLOCKED_REASONS, WORK_ITEM_STATES, WORK_WORKER_STATUSES

# ---------------------------------------------------------------------------
# Instrument names
# ---------------------------------------------------------------------------

#: One per ledger-opening goal, and one per item created. ``kind`` separates the
#: two; ``depth`` is the board's own depth, so a sub-conductor's items are
#: distinguishable from a top-level conductor's.
WORK_CREATED = "kirocrew.work.created"

#: One per item that reached a terminal state through ``close``, carrying how it
#: ended, how many acceptance failures it took, and how long it was open.
WORK_ITEM_CLOSED = "kirocrew.work.item_closed"

#: One per item handed to a target -- the ``bind`` that names the worker.
WORK_DISPATCHES = "kirocrew.work.dispatches"

#: One per worker report that committed, by ``status`` and stuck ``reason``.
WORK_REPORTS = "kirocrew.work.reports"


# ---------------------------------------------------------------------------
# Closed attribute enums
# ---------------------------------------------------------------------------

#: Where a value the call site could not recognise goes. It must never be a
#: value's own spelling: that is what would make the series unbounded.
OTHER = "other"

#: What :data:`WORK_CREATED` counts. ``goal`` is the goal write that OPENED the
#: board (see :func:`record_goal_created`), ``item`` one minted work item.
CREATED_KINDS: tuple[str, ...] = ("goal", "item")

#: How deep the creating board sits. ``2+`` collapses the tail so the series
#: cannot grow with the depth cap.
DEPTH_BUCKETS: tuple[str, ...] = ("0", "1", "2+")

#: How an item ended. Derived from the ledger's own state vocabulary minus
#: ``open``, because ``close`` refuses ``open`` -- so a drift in that tuple shows
#: up here rather than silently sending every close to :data:`OTHER`.
CLOSED_STATES: tuple[str, ...] = tuple(state for state in WORK_ITEM_STATES if state != "open")

#: How many acceptance failures an item took before it closed.
FAILS_BUCKETS: tuple[str, ...] = ("0", "1", "2", "3+")

#: How long an item was open, from its ``created_at`` to its ``closed_at``.
DURATION_BUCKETS: tuple[str, ...] = ("le_10m", "le_1h", "le_4h", "gt_4h")

#: The bucket edges, in seconds, each INCLUSIVE of its own edge: an item open for
#: exactly ten minutes is ``le_10m``, and one open a second longer is ``le_1h``.
#: Stated here and in the metrics doc, as rule 2 of the RFC requires.
DURATION_BUCKET_EDGE_SECS: tuple[tuple[float, str], ...] = (
    (600.0, "le_10m"),
    (3600.0, "le_1h"),
    (14400.0, "le_4h"),
)

#: What sort of party a dispatch went to. ``session`` is the only kind the
#: ``bind`` route builds today; ``crew`` is declared because the crew log's
#: dispatch contract names it (``crew_log/entry_types.py::CREW_TARGET_KINDS``,
#: pinned by this module's tests) and a second target kind must land in its own
#: series rather than in :data:`OTHER`.
TARGET_KINDS: tuple[str, ...] = ("session", "crew")

#: What a report may say. The ledger's worker half can commit the first four and
#: only those, so ``failed`` is never emitted by the route today; it is declared
#: because the crew log's report contract names it
#: (``crew_log/entry_types.py::CREW_REPORT_STATUSES``), so a writer that gains it
#: lands in its own series instead of in :data:`OTHER`.
REPORT_STATUSES: tuple[str, ...] = WORK_WORKER_STATUSES + ("failed",)

#: Why a report is stuck. ``none`` is the value carried when the report named no
#: reason, which is every ``progress`` and ``done`` report: an absent attribute
#: would make the two cases indistinguishable from a dropped attribute.
NO_REASON = "none"
REPORT_REASONS: tuple[str, ...] = WORK_BLOCKED_REASONS + (NO_REASON,)


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def _instant(stamp: Any) -> datetime | None:
    """*stamp* as a datetime, or ``None`` when it is not one.

    The stdlib alone, deliberately. The store has its own stamp reader, but the
    work ledger is reachable through exactly two allowlisted modules
    (``test_work_ledger.py::test_only_the_phase_2_seams_import_the_module``), and
    that allowlist guards a PRIVILEGE: it is what stops one module naming another
    session's ledger. Claiming a seam in it to borrow a date parser would widen
    an identity rule for a reason that has nothing to do with identity. Nothing
    here needs the store -- only the two strings the call site already holds.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def _is_count(value: Any) -> bool:
    """Whether *value* is a plain non-negative integer.

    ``bool`` is excluded explicitly: it is an ``int`` subclass, so ``True`` would
    otherwise bucket as ``1`` and report a depth or a fail count nobody wrote.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def depth_bucket(depth: Any) -> str:
    """*depth* as a member of :data:`DEPTH_BUCKETS`, or :data:`OTHER`."""
    if not _is_count(depth):
        return OTHER
    return "2+" if depth >= 2 else str(depth)


def fails_bucket(fails: Any) -> str:
    """*fails* as a member of :data:`FAILS_BUCKETS`, or :data:`OTHER`."""
    if not _is_count(fails):
        return OTHER
    return "3+" if fails >= 3 else str(fails)


def duration_bucket(created_at: Any, closed_at: Any) -> str:
    """How long the item was open, as a member of :data:`DURATION_BUCKETS`.

    :data:`OTHER` when either stamp is missing or is not a stamp -- a record from
    before ``created_at`` was written has nothing to measure, and a guessed zero
    would render as a real sub-ten-minute item.

    A span that comes out NEGATIVE is read as zero rather than as
    :data:`OTHER`. Both stamps are written by the store itself, so a close before
    its own create is the host's clock moving backwards between the two writes,
    not an unmeasurable item: the item was open for approximately no time, and
    the smallest bucket is the honest reading.
    """
    start = _instant(created_at)
    end = _instant(closed_at)
    if start is None or end is None:
        return OTHER
    if (start.tzinfo is None) != (end.tzinfo is None):
        # The store writes a stamp with an offset and wrote one without it once,
        # so a long-lived item's span can have a naive end and an aware one.
        # Subtracting across that raises, so the naive end is read as local time:
        # the same reading the store gives its own offsetless stamps.
        try:
            start = start.astimezone() if start.tzinfo is None else start
            end = end.astimezone() if end.tzinfo is None else end
        except (OSError, OverflowError, ValueError):
            return OTHER
    try:
        seconds = (end - start).total_seconds()
    except (OverflowError, TypeError, ValueError):
        return OTHER
    seconds = max(0.0, seconds)
    for edge, name in DURATION_BUCKET_EDGE_SECS:
        if seconds <= edge:
            return name
    return "gt_4h"


def _closed(value: Any, allowed: tuple[str, ...]) -> str:
    """*value* when *allowed* holds it, else :data:`OTHER`."""
    return value if isinstance(value, str) and value in allowed else OTHER


# ---------------------------------------------------------------------------
# Call sites
#
# None of these wraps itself in try/except, and that is deliberate rather than
# an omission. ``emit_counter`` already swallows every failure of the emit after
# a debug log, so a second layer here would catch only what the bucketing above
# could raise -- and it raises nothing: each helper is TOTAL, mapping every
# input, including a type that is not a value at all, onto a declared member of
# its own enum. ``test_every_bucket_function_answers_only_inside_its_own_enum``
# and ``test_every_record_function_is_total_over_hostile_input`` are what hold
# that, so the guarantee is checked rather than merely wrapped.
# ---------------------------------------------------------------------------


def record_goal_created(depth: Any) -> None:
    """One board opened by a goal write.

    Called ONLY for the goal write that opened the ledger -- the one whose
    committed header carries ``goal_version == 1``. A later goal write is a
    reword, a round bump, a cap raise or an explicit ``new_goal``, and counting
    any of them would make this counter report "goal writes" while its name
    claims boards: a conductor that rewords its goal five times would read as
    five conductors. What the later writes cost is visible in the items they
    create, which :func:`record_item_created` counts.

    A board PURGED and reopened on the same slot does count again, and should:
    the purge takes the header with it, so the reopened board's own first goal
    write carries ``goal_version == 1`` from a fresh record. A rebuild is the
    other way round and also right -- it keeps the version the log holds, so the
    board it re-materialises is not counted a second time.
    """
    emit_counter(WORK_CREATED, {"kind": "goal", "depth": depth_bucket(depth)})


def record_item_created(depth: Any) -> None:
    """One work item minted, at the creating board's depth."""
    emit_counter(WORK_CREATED, {"kind": "item", "depth": depth_bucket(depth)})


def record_item_closed(state: Any, fails: Any, created_at: Any, closed_at: Any) -> None:
    """One item closed, by how it ended, its fail count and how long it was open.

    Every argument comes from the COMMITTED record, never from the request, so an
    item closed with a state the store defaulted is reported as the store holds
    it.
    """
    emit_counter(
        WORK_ITEM_CLOSED,
        {
            "state": _closed(state, CLOSED_STATES),
            "fails": fails_bucket(fails),
            "duration": duration_bucket(created_at, closed_at),
        },
    )


def record_dispatch(target_kind: Any) -> None:
    """One work item handed to a target.

    Counted on the ``bind`` that names the target, after that bind's own
    ``work/recorded`` entry landed -- not on the ``crew/dispatch`` echo written
    just after it. The echo is best-effort and is skipped entirely for a board
    driven from a slot that belongs to no crew, so counting on it would silently
    drop every dispatch from such a board while the board plainly made one.
    """
    emit_counter(WORK_DISPATCHES, {"target_kind": _closed(target_kind, TARGET_KINDS)})


def record_report(status: Any, reason: Any) -> None:
    """One worker report that committed.

    *reason* rides only on ``blocked`` and ``question``; every other report
    carries :data:`NO_REASON`. Counted after the report's ``work/recorded`` entry
    landed, for the reason :func:`record_dispatch` gives about its own echo.
    """
    emit_counter(
        WORK_REPORTS,
        {
            "status": _closed(status, REPORT_STATUSES),
            "reason": _closed(reason, WORK_BLOCKED_REASONS) if reason else NO_REASON,
        },
    )
