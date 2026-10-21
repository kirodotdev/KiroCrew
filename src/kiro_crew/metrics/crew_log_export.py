"""OTel export of crew-log usage counts: the subscriber, and the fold registry it reads.

WHAT THIS MODULE IS. One subscriber on the crew-log bus
(:mod:`kiro_crew.crew_log.bus`), filtered to :data:`~kiro_crew.crew_log.bus.FOLD_ADVANCED`
with ``scope=SCOPE_SESSION``, that turns a session-keyed usage fold's rendered value
into OTel counter adds. It is installed once, from the gateway
boot path, beside
:func:`~kiro_crew.dashboard.handlers.crew_log.install_crew_log_publisher` -- see
:func:`install_otel_crew_log_subscriber`.

WHY A REGISTRY AND NOT ONE HAND-WRITTEN CALLBACK. S1 ships one fold (``otel_usage``,
session-keyed); a later story (session tree, work ledger, ...) ships its own small
counting fold the same way :mod:`~kiro_crew.crew_log.projection_dashboard` ships several
folds for one surface. Each needs the same things from this module -- a bus
subscription filtered to its own fold name, a place to turn a rendered value into
attributed instrument writes, and the self-timing histogram that proves the export is
not slow -- so this module owns those and a later story adds a row to :data:`_EXPORTERS`
instead of writing its own subscriber. See that table's docstring, and
:class:`UsageExporter`'s, for the extension contract. :func:`on_fold_advanced`
dispatches by the event's own ``fold`` name to the matching row, so two session-keyed
rows share the one subscription.

THE BUS CONTRACT THIS MODULE OWES (see ``bus.py``'s module docstring at length): the
callback invoked here runs SYNCHRONOUSLY on the crew-log fold worker's own thread, never
the append path. It must do no I/O and take no lock that can block. Every exporter in
:data:`_EXPORTERS` is therefore a pure function from an OLD rendered value and a NEW one
to a list of :class:`Metric` writes -- arithmetic on dicts already in memory, handed to
it by this module -- and recording those onto the OTel SDK's own instruments, which
batch and send on their own schedule (the SDK's periodic exporter, not this thread), is
the only side effect. A subscriber that raises is logged by the bus and the other
subscribers still run (``bus.publish``'s own contract); this module additionally guards
each per-key step so one session's malformed value cannot cost the others in the same
coalesced batch.

WHY A REMEMBERED LAST VALUE PER KEY. A session's ``otel_usage`` render is CUMULATIVE
over that session's life (``_otel_usage_render``'s own docstring), while an OTel
counter wants a DELTA per add. ``turn/started`` can commit many times in one session,
each its own ``FoldAdvanced`` publish, so this module keeps the last rendered value it
exported per ``(fold_name, key)`` -- ``key`` is the session id -- and adds only what
changed since.

THE FIRST SIGHT OF A SESSION BASELINES ITS CUMULATIVE TOTALS. A session's render is
cumulative over its life, and the memo is in-memory, so the first delivery this process
sees for a session carries a total that may ALREADY have been exported by a previous
process (a resumed session after a gateway restart) or by this one before the
:data:`_MAX_TRACKED_SESSIONS` cap evicted its row. Diffing that against an empty memo
would re-add the whole total -- the double-count the first-principles review flagged.
:func:`_on_fold_advanced_inner` passes ``previous=None`` on every first sight, and a
``diff`` reads that as "baseline the cumulative parts": emit no turn delta, record the
render as the floor, and let every later delivery diff against it.

A SESSION THIS PROCESS CAN PROVE IS NEW IS COUNTED WHOLE, not baselined, so neither the
session-started counter nor a new session's first turn is lost. The fold carries
``opened_ts`` (the ``session/opened`` entry's own timestamp, off the log), and
:func:`_opened_in_this_process` calls a session new when its ``opened_ts`` is strictly
newer than the newest opening this process has already dropped --
:data:`_evicted_max_opened_ts`, which starts at :data:`_install_ms` and rises toward
each session the :data:`_MAX_TRACKED_SESSIONS` cap evicts. On the first sight of a new
session the diff counts its WHOLE render against zero: session.started once, and every
turn already in that first render (a new session's opening and first turn can share one
coalesced fold batch, so counting against zero is what keeps that first turn). Every
other first sight -- a resumed session (``opened_ts`` predates install), or an evicted
session seen again (its ``opened_ts`` is at most the evicted maximum) -- is BASELINED:
its render becomes the floor and nothing is emitted, so no restart or eviction replay
re-adds a total already exported. The S2 session-tree counters ride the same "new"
test.

THE RULE IS NEVER OVER-COUNT, AND EVERY UNDER-COUNT IS NAMED. There are three, all in
the fail-safe direction, and a reader of these counters is owed all three rather than a
bare claim that the loss is small.

1. A session opened before install -- every session live across a restart -- is not
   counted as started by this process.
2. The TURNS in such a session's first delivery after a restart are folded into the
   baseline, not counted. The fold publishes a resumed session only once a new entry
   wakes it, so that first delivery normally carries at least one real
   ``turn/started``, and those turns are lost. Per resumed session per restart the loss
   is one coalesced batch, which is what makes it a bound a reader can reason about --
   but it recurs on every restart, so it is NOT bounded over the life of an install, and
   calling it bounded without that qualifier would be the wrong claim. Counting exactly
   once instead needs a floor set to the state BEFORE that delivery's entries, which
   means either re-walking replayed state or persisting a per-session cursor; both are
   out of scope for this design, and the trade is stated in the pull request rather
   than hidden here.
3. Once the 4096 cap is saturated, a never-seen session that opened no later than some
   already-evicted session reads as not-new and is baselined rather than counted. This
   one needs the cap saturated to occur at all.

The alternative to each is an over-count, which is why the design takes them. The table
itself is bounded (:data:`_MAX_TRACKED_SESSIONS`).
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Final, Mapping, NamedTuple

logger = logging.getLogger(__name__)

#: Every value :func:`crew_log.bus` and this module hand each other is read
#: defensively; see :func:`_on_fold_advanced_inner`.
AttrValue = str | int | bool | float
Attrs = Mapping[str, AttrValue]


class Metric(NamedTuple):
    """One counter add a :class:`UsageExporter`'s ``diff`` asks this module to make.

    ``value`` is a DELTA added to a running total. Every row emits counters only: a
    duration or size is a counter with a closed ``bucket`` attribute, never a new
    histogram (see the module docstring's "WHY A REMEMBERED LAST VALUE" note for why the
    value is a delta).
    """

    name: str
    value: int | float
    attrs: Attrs


# --------------------------------------------------------------------------- #
# The extension point: one row per usage fold this export turns into metrics.
# --------------------------------------------------------------------------- #


class UsageExporter:
    """One usage fold's export contract: its name, and how to diff its render.

    ``fold_name`` is the crew-log fold this entry answers to (the registry's own
    name, e.g. ``"otel_usage"``); the subscription filters on it so one fold's
    delivery never runs another's diff function.

    ``diff`` takes ``(previous, current)`` -- the last rendered value this module
    exported for one session, or ``None`` on the FIRST sight of that session in this
    process (a cold boot, or a re-delivery after eviction) -- and the value this
    delivery carries, and returns the writes to make: a list of :class:`Metric`.
    ``previous is None`` is the first sight of this session in this process. A ``diff``
    then counts the WHOLE render against zero for a session it can prove is new
    (:func:`_opened_in_this_process`), and BASELINES (emits nothing, the render becomes
    the floor) for one it cannot -- so a resumed or evicted-and-re-seen session's total
    is never re-added. It must be PURE and must not raise for a malformed ``current``
    (validate and return ``[]`` instead); the caller still guards it.

    A NEW session-keyed usage fold adds one ``UsageExporter`` to :data:`_EXPORTERS`
    and writes its own ``diff``; nothing else in this module changes. ``diff`` owns
    100% of the attribute vocabulary decision (which closed enum a raw value folds
    into, and the ``other`` bucket for what is outside it) -- this module only
    sequences the subtraction and the recording, never the attribute choice.
    """

    __slots__ = ("fold_name", "diff")

    def __init__(
        self,
        fold_name: str,
        diff: Callable[["Mapping[str, Any] | None", Mapping[str, Any]], "list[Metric]"],
    ) -> None:
        self.fold_name = fold_name
        self.diff = diff


@functools.lru_cache(maxsize=1)
def _known_agent_kinds() -> frozenset[str]:
    """The closed ``agent_kind`` set this export puts on an attribute.

    The ``otel_usage`` fold folds the raw agent to one of these in its own ``step``
    (:func:`~kiro_crew.crew_log.projection_otel_usage._agent_kind`); this set is the
    export's backstop, so a rendered ``opened_agent_kind`` outside it (a damaged render)
    folds to ``other`` rather than reaching the attribute. Derived from the fold's own
    table (``_AGENT_KIND_BY_STEM``) plus the three values the fold answers that are not
    file stems -- the bare default agent, a crewmate's pinned DM session, and the
    catch-all -- so there is one copy of the list. Function-local and cached, the same
    shape as :func:`_known_actors`.
    """
    from kiro_crew.crew_log.projection_otel_usage import _AGENT_KIND_BY_STEM

    return frozenset(_AGENT_KIND_BY_STEM.values()) | {"default", "crewmate", "other"}


@functools.lru_cache(maxsize=1)
def _known_actors() -> frozenset[str]:
    """Every ``actor`` value ``turn/started`` can carry: the writer's own closed
    ``ACTOR_VALUES`` (``crew_log/entry_types_lifecycle.py``), which includes ``other``.

    Derived from that one definition rather than spelled again here, so the two cannot
    drift. A value outside it (a damaged line) folds to ``other`` instead of reaching an
    attribute. The import is function-local so the metrics layer takes no module-scope
    dependency on the crew-log package, and cached so a delivery pays it once.
    """
    from kiro_crew.crew_log.entry_types_lifecycle import ACTOR_VALUES

    return frozenset(ACTOR_VALUES)


#: ``kirocrew.crew.session.started`` -- one add per session this export has not
#: already reported as opened, attributed by the closed ``agent_kind`` set.
SESSION_STARTED_METRIC: Final[str] = "kirocrew.crew.session.started"

#: ``kirocrew.crew.turn.started`` -- one add per turn, attributed by the closed
#: ``actor`` set (``ACTOR_VALUES``, read through :func:`_known_actors`) and by the
#: closed ``agent_kind`` of the session the turn ran in (the same value
#: ``session.started`` carries, read from the same fold render), so turns inside
#: conductor sessions and worker sessions can be told apart.
TURN_STARTED_METRIC: Final[str] = "kirocrew.crew.turn.started"

#: ``kirocrew.crew.session.tree.depth`` -- one COUNTER add per session whose
#: ``session/opened`` recorded a depth (how many creators stood above it in the session
#: tree), attributed by ``depth`` in the closed bucket set ``0`` / ``1`` / ``2`` / ``3+``
#: (:data:`~kiro_crew.crew_log.otel_session_tree_fold.DEPTH_BUCKETS`). A counter per
#: bucket label rather than a histogram, so the distribution survives backends that do
#: not keep explicit histogram buckets.
SESSION_TREE_DEPTH_METRIC: Final[str] = "kirocrew.crew.session.tree.depth"

#: ``kirocrew.crew.session.tree.child`` -- one COUNTER add per child session a creator
#: makes, attributed by ``parent_kind`` (the creator session's closed ``agent_kind``, the
#: same set ``session.started`` uses) and by ``nth``, the child's ordinal among its
#: creator's children, in the closed bucket set ``1`` / ``2`` / ``3`` / ``4-7`` / ``8+``
#: (:data:`~kiro_crew.crew_log.otel_session_tree_fold.NTH_BUCKETS`). The number of
#: creators with at least ``k`` children is the sum of the adds at ``nth >= k``, so the
#: width distribution is read off a counter without waiting for a creator to end.
SESSION_TREE_CHILD_METRIC: Final[str] = "kirocrew.crew.session.tree.child"


def _otel_usage_diff(
    previous: "Mapping[str, Any] | None", current: Mapping[str, Any]
) -> "list[Metric]":
    """The writes owed by one session's ``otel_usage`` render.

    ``previous is None`` is the FIRST sight of the session in this process (see
    :class:`UsageExporter`). Then:

    - A session this process can prove is NEW (:func:`_opened_in_this_process`: its
      ``opened_ts`` is newer than any opening this process has already dropped) is
      counted WHOLE against zero -- session.started once AND every turn already in this
      first render. A new session's opening and first turn can arrive in one coalesced
      fold batch, so counting against zero is what keeps that first turn from being lost
      to a baseline.
    - A session NOT provably new (a resumed one, or one opened no later than an evicted
      session) is BASELINED: its cumulative render becomes the floor and nothing is
      emitted, so a restart or an eviction replay never re-adds a total a previous
      process -- or this one -- already exported.

    Later deliveries (``previous`` not ``None``) diff turns against the stored floor.
    Both metrics are counters. The rule is never over-count, at the price of
    the three named under-counts in this module's own docstring -- and the one this
    branch is responsible for is the second: a resumed session's first delivery after a
    restart normally already carries a real ``turn/started``, and baselining the whole
    render loses those turns. Read that paragraph before treating this function's output
    as a complete count.
    """
    metrics: "list[Metric]" = []
    first_sight = previous is None
    is_new = first_sight and _opened_in_this_process(current.get("opened_ts"))

    if first_sight and not is_new:
        # Resumed, or opened no later than an evicted session: baseline, emit nothing.
        # THIS LINE IS WHERE UNDER-COUNT 2 HAPPENS, named rather than left to be
        # discovered: the entries that woke this delivery are inside the render being
        # made the floor, so a resumed session's turns in that batch are not counted.
        # The alternative is diffing against an empty memo, which re-adds that session's
        # entire pre-restart total -- an unbounded over-count. Counting exactly once
        # needs a floor set to the state before this batch, which no field on the event
        # supplies without re-walking replayed state or persisting a cursor.
        return metrics

    # The floor to diff turns against: zero for a provably-new session's first sight (so
    # its whole render -- opening and first-batch turns -- counts), the stored render for
    # a later delivery.
    prev: Mapping[str, Any] = previous if isinstance(previous, Mapping) else {}

    # The fold already folded the raw agent to the closed ``agent_kind`` set in its own
    # ``step`` and stored only the enum, so this reads a closed value rather than
    # classifying a name here. One value serves both metrics: the session's own kind on
    # session.started, and the kind of session a turn ran in on turn.started.
    kind = _agent_kind_attr(current)

    if is_new and bool(current.get("opened")):
        metrics.append(Metric(SESSION_STARTED_METRIC, 1, {"agent_kind": kind}))

    prior_turns = prev.get("turns")
    prior: Mapping[str, Any] = prior_turns if isinstance(prior_turns, Mapping) else {}
    turns = current.get("turns")
    if isinstance(turns, Mapping):
        for actor, count in turns.items():
            if not isinstance(actor, str):
                continue
            total = count if isinstance(count, int) and not isinstance(count, bool) else 0
            was = prior.get(actor)
            before = was if isinstance(was, int) and not isinstance(was, bool) else 0
            delta = total - before
            if delta <= 0:
                # A total that did not grow is not an event; a shrink (a replay) is
                # never emitted as a negative add to a monotonic counter.
                continue
            attr_actor = actor if actor in _known_actors() else "other"
            metrics.append(
                Metric(
                    TURN_STARTED_METRIC,
                    delta,
                    {"actor": attr_actor, "agent_kind": kind},
                )
            )
    return metrics


def _agent_kind_attr(render: Mapping[str, Any]) -> str:
    """The ``agent_kind`` attribute value for one ``otel_usage`` render.

    Reads ``opened_agent_kind``, which the fold already mapped to the closed set. A
    value outside :func:`_known_agent_kinds` (a damaged render), or the empty value a session
    carries before its opening entry is folded, maps to ``other`` so nothing unbounded
    reaches an attribute.
    """
    kind = render.get("opened_agent_kind")
    if not isinstance(kind, str) or kind not in _known_agent_kinds():
        return "other"
    return kind


def _otel_session_tree_diff(
    previous: "Mapping[str, Any] | None", current: Mapping[str, Any]
) -> "list[Metric]":
    """The depth and child counter adds owed by one session's ``otel_session_tree``
    render.

    ``previous is None`` is the FIRST sight of the session in this process (see
    :class:`UsageExporter`), and it follows the same rule ``_otel_usage_diff`` does:

    - A session this process can prove is NEW (:func:`_opened_in_this_process`) is
      counted WHOLE against zero: one depth add when its ``session/opened`` carried a
      depth, and one child add for every ``session/spawned`` already in this first
      render.
    - A session NOT provably new (resumed, or opened no later than an evicted session)
      is BASELINED: its render becomes the floor and nothing is emitted, so a restart
      or an eviction replay never re-adds a depth or a child already exported.

    A later delivery (``previous`` not ``None``) emits no depth add -- the depth is
    counted once per session -- and one child add per ordinal its ``children`` total
    moved past since the stored floor, bucketed by
    :func:`~kiro_crew.crew_log.otel_session_tree_fold.nth_bucket_counts`. The fold
    keeps that total across restarts in its savepoint, so the ordinal stays the
    child's true position among its creator's children; only the adds in a resumed
    creator's first post-restart delivery are lost to the baseline, the same bounded
    under-count the turn counter carries.
    """
    from kiro_crew.crew_log.otel_session_tree_fold import depth_bucket, nth_bucket_counts

    metrics: "list[Metric]" = []
    first_sight = previous is None
    is_new = first_sight and _opened_in_this_process(current.get("opened_ts"))
    if first_sight and not is_new:
        return metrics
    if is_new:
        depth = current.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
            metrics.append(
                Metric(
                    SESSION_TREE_DEPTH_METRIC,
                    1,
                    {"depth": depth_bucket(depth)},
                )
            )
    prev: Mapping[str, Any] = previous if isinstance(previous, Mapping) else {}
    before = _child_total(prev)
    after = _child_total(current)
    if after > before:
        # The creator's own closed kind, read through the same backstop the usage
        # counters use, so a damaged render cannot put an unbounded value here.
        parent_kind = _agent_kind_attr({"opened_agent_kind": current.get("agent_kind")})
        for label, count in nth_bucket_counts(before, after):
            metrics.append(
                Metric(
                    SESSION_TREE_CHILD_METRIC,
                    count,
                    {"parent_kind": parent_kind, "nth": label},
                )
            )
    return metrics


def _child_total(render: Mapping[str, Any]) -> int:
    """The ``children`` count of one ``otel_session_tree`` render, ``0`` when absent or
    malformed."""
    value = render.get("children")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


#: Every usage fold this export subscribes to. ONE ROW PER FOLD -- a later story
#: A new session-keyed usage fold adds its own row here with its own ``diff``, and
#: nothing else in this module changes: :func:`on_fold_advanced` dispatches by the
#: event's fold name, so a new row is live the moment it is added. ``otel_session_tree``
#: is the source of the session-tree depth and child counters; the roots metric is NOT
#: here because its source is the whole session tree, which no session-keyed
#: ``FoldAdvanced`` carries
#: -- it is an observable gauge read at collect time
#: (:func:`kiro_crew.crew_log.otel_session_tree_roots.register_session_tree_gauge`,
#: registered from :mod:`kiro_crew.metrics.provider` the same way the inventory and
#: process gauges are).
_EXPORTERS: Final[tuple[UsageExporter, ...]] = (
    UsageExporter("otel_usage", _otel_usage_diff),
    UsageExporter("otel_session_tree", _otel_session_tree_diff),
)


# --------------------------------------------------------------------------- #
# Per-session last-value memo, and the self-timing histogram.
# --------------------------------------------------------------------------- #

#: Bound on the last-rendered-value table, across every fold in :data:`_EXPORTERS`
#: combined (keyed by ``(fold_name, session_key)``). See the module docstring's
#: eviction note.
_MAX_TRACKED_SESSIONS: Final[int] = 4096

#: ``kirocrew.crew.export.lag`` -- how long one EXPORTED delivery's diff-and-record
#: took, in milliseconds, with NO attribute: the module docstring's self-timing proof
#: that the export is not slow. One sample per delivery an :data:`_EXPORTERS` row
#: handled, never one per session-scoped delivery the bus hands over, so the
#: distribution holds only the work this module actually does and the count equals the
#: number of exports. Bucket boundaries are the SDK default for this build; the
#: right boundaries for this signal are TO BE MEASURED against a real install's
#: delivery sizes once this ships, by reading the exported histogram's bucket
#: occupancy and narrowing from there -- not guessed in advance of any data.
EXPORT_LAG_METRIC: Final[str] = "kirocrew.crew.export.lag"

_memo_lock = threading.Lock()
# OrderedDict as an LRU: a hit moves to the end, so the FRONT is always the entry
# that has gone longest without a delivery -- the one :data:`_MAX_TRACKED_SESSIONS`
# evicts first.
_last_values: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()


def _last_value(fold_name: str, session_key: str) -> dict[str, Any]:
    """The last rendered value exported for *(fold_name, session_key)*, or ``{}``."""
    key = (fold_name, session_key)
    with _memo_lock:
        found = _last_values.get(key)
        return dict(found) if found is not None else {}


#: Wall-clock ms (the same clock ``Entry.time`` uses, ``store.now_ms``) captured when
#: this process installed the subscriber. ``None`` until install.
_install_ms: "int | None" = None

#: The newest ``opened_ts`` this process has ever DROPPED from the memo -- the install
#: time to begin with, raised toward each session evicted by the
#: :data:`_MAX_TRACKED_SESSIONS` cap. A session counts as NEW only when its ``opened_ts``
#: is strictly greater than this, which is what stops an evicted session from being
#: re-counted when it is seen again: its own ``opened_ts`` is at most this high after it
#: was evicted, so its re-sight baselines instead of counting. Strictly ``>`` rather
#: than ``>=`` for exactly that boundary -- a session whose ``opened_ts`` equals the
#: evicted maximum must read as NOT new, so the eviction of a session never lets an
#: equal-timestamped re-sight of it count twice. The cost is the fail-safe direction: a
#: never-seen session opened no later than some already-evicted session is read as not
#: new and under-counted, which can only happen once the 4096 cap is saturated. ``None``
#: until install, mirroring :data:`_install_ms`.
_evicted_max_opened_ts: "int | None" = None


def _opened_in_this_process(opened_ts: Any) -> bool:
    """Whether *opened_ts* marks a session this process should count as NEW: opened
    strictly after the newest opening this process has already dropped (install time, or
    the latest evicted session). ``False`` when the value is missing or not yet anchored
    -- never guess a session is new, since that is the direction that double-counts.
    """
    if not isinstance(opened_ts, int) or isinstance(opened_ts, bool) or opened_ts <= 0:
        return False
    with _memo_lock:
        anchor = _evicted_max_opened_ts
    return anchor is not None and opened_ts > anchor


def _seen(fold_name: str, session_key: str) -> bool:
    """Whether this process has ALREADY recorded a baseline for *(fold_name,
    session_key)*. ``False`` on the first delivery after a cold boot, and again after
    :data:`_MAX_TRACKED_SESSIONS` eviction drops this key -- the two moments where the
    memo holds no prior and a diff against ``{}`` would re-add the whole cumulative
    render. See :func:`_on_fold_advanced_inner` for how the first sight is baselined.
    """
    with _memo_lock:
        return (fold_name, session_key) in _last_values


def _remember_value(fold_name: str, session_key: str, value: Mapping[str, Any]) -> None:
    """Record *value* as the last one exported for *(fold_name, session_key)*.

    On eviction (the memo passing :data:`_MAX_TRACKED_SESSIONS`) the dropped session's
    ``opened_ts`` raises :data:`_evicted_max_opened_ts`, so a later re-sight of that
    session reads as not-new and baselines instead of re-counting.
    """
    global _evicted_max_opened_ts
    key = (fold_name, session_key)
    with _memo_lock:
        _last_values[key] = dict(value)
        _last_values.move_to_end(key)
        while len(_last_values) > _MAX_TRACKED_SESSIONS:
            _, dropped = _last_values.popitem(last=False)
            dropped_ts = dropped.get("opened_ts")
            if (
                isinstance(dropped_ts, int)
                and not isinstance(dropped_ts, bool)
                and _evicted_max_opened_ts is not None
                and dropped_ts > _evicted_max_opened_ts
            ):
                _evicted_max_opened_ts = dropped_ts


def _record_metrics(metrics: "list[Metric]") -> None:
    """Hand each :class:`Metric` to the recorder as one counter add.

    The recorder (:class:`~kiro_crew.metrics.recorder.MetricsRecorder`) is a no-op
    when telemetry is disabled, and :func:`install_otel_crew_log_subscriber` already
    refuses to subscribe at all in that case -- this is a second, cheaper guard for
    the window between a consent flip and the next subscribe/unsubscribe cycle.
    """
    if not metrics:
        return
    from kiro_crew.metrics.provider import get_recorder

    recorder = get_recorder()
    for metric in metrics:
        recorder.counter(metric.name, metric.value, attrs=dict(metric.attrs))


def on_fold_advanced(event: Any) -> None:
    """THE BUS CALLBACK: one session-keyed ``FoldAdvanced`` for a fold in
    :data:`_EXPORTERS`, turned into OTel counter adds.

    Called SYNCHRONOUSLY on the crew-log fold worker's thread -- see the module
    docstring's "THE BUS CONTRACT" section. Every step below is in-memory
    dict arithmetic and an OTel SDK instrument call, which the SDK itself documents
    as cheap and non-blocking; nothing here opens a file or waits on a lock that any
    other thread can hold for long.

    The event's own ``fold`` name selects the matching :class:`UsageExporter` row;
    the row's ``diff`` and this function's bookkeeping (:func:`_last_value` /
    :func:`_remember_value`) key on ``(fold_name, event.key)``.

    Defensive by construction: a malformed event, an unknown fold name, or a diff
    function that raises costs this one delivery and is logged, never the bus's other
    subscribers (the bus's own ``publish`` already isolates subscribers from each
    other).
    """
    started = time.monotonic()
    try:
        exported = _on_fold_advanced_inner(event)
    except Exception:  # telemetry must never break the caller
        logger.debug("otel crew-log export failed for one fold delivery", exc_info=True)
        return
    if not exported:
        # ONE SAMPLE PER REAL EXPORT, not one per delivery. The subscription filters on
        # ``scope=SCOPE_SESSION`` alone, so every session-keyed fold the eager worker
        # advances -- ``status``, ``class``, ``usage``, ``timeline`` -- arrives here and
        # is dropped by the ``_EXPORTERS`` lookup above. Timing those drops would fill
        # the distribution with the cost of one dict lookup, which is the population
        # this histogram exists to EXCLUDE: it is read to answer how long the
        # diff-and-record work takes, and a near-zero no-op swamping the buckets makes
        # that unanswerable while the count stops matching the number of exports.
        return
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().histogram(
            EXPORT_LAG_METRIC, (time.monotonic() - started) * 1000.0, unit="ms"
        )
    except Exception:  # telemetry must never break the caller
        logger.debug("otel crew-log export lag emit failed", exc_info=True)


def _on_fold_advanced_inner(event: Any) -> bool:
    """Diff and record one delivery. ``True`` when an :data:`_EXPORTERS` row actually
    handled it, which is what :func:`on_fold_advanced` times -- see the note there.
    ``False`` for a fold this module does not export and for a malformed event.
    """
    fold_name = str(getattr(event, "fold", "") or "")
    exporter = next((item for item in _EXPORTERS if item.fold_name == fold_name), None)
    if exporter is None:
        return False
    key = str(getattr(event, "key", "") or "")
    value = getattr(event, "value", None)
    if not key or not isinstance(value, Mapping):
        return False
    # FIRST sight of this (fold, session) in this process -- cold boot, or a
    # re-delivery after LRU eviction dropped its row -- is passed ``previous=None``;
    # every later delivery passes the stored render and diffs against it. On first
    # sight the ``diff`` asks :func:`_opened_in_this_process`: a session whose
    # ``opened_ts`` is strictly newer than :data:`_evicted_max_opened_ts` (install
    # time, raised by each eviction) is provably new, and its WHOLE render is counted
    # against zero -- session.started plus every turn already in it. Any other session
    # (resumed, or opened no later than an evicted one) is baselined: its cumulative
    # render becomes the floor and nothing is emitted, because diffing it against the
    # empty memo would re-add totals a previous process, or this one before the
    # eviction, already exported. No persistence, no I/O on this bus-callback thread.
    previous: "Mapping[str, Any] | None"
    if _seen(fold_name, key):
        previous = _last_value(fold_name, key)
    else:
        previous = None
    metrics = exporter.diff(previous, value)
    _record_metrics(metrics)
    _remember_value(fold_name, key, value)
    return True


# --------------------------------------------------------------------------- #
# Installation -- the gateway boot path's one call into this module.
# --------------------------------------------------------------------------- #

_dispose: "Callable[[], None] | None" = None
_install_lock = threading.Lock()


def install_otel_crew_log_subscriber() -> bool:
    """Subscribe :func:`on_fold_advanced` to the crew-log bus, once per process, for
    :data:`~kiro_crew.crew_log.bus.FOLD_ADVANCED` events scoped to
    :data:`~kiro_crew.crew_log.bus.SCOPE_SESSION`.

    Returns ``False`` and subscribes nothing when telemetry is disabled
    (``telemetry.enabled``, default ``False``) -- read ONCE here, through
    :func:`~kiro_crew.metrics.provider.get_recorder`'s own ``.enabled``, the same
    gate :mod:`kiro_crew.metrics.sessions` already uses for its own consent check.
    This mirrors ``install_crew_log_publisher``'s own posture toward
    ``crew_log_enabled()``: a flag flipped later in the process's life is picked up
    on the next restart, not pushed into a live subscription -- there is no
    unsubscribe-and-resubscribe wired to the consent re-check here in S1.

    Every row in :data:`_EXPORTERS` is session-keyed, so one subscription serves them
    all: :func:`on_fold_advanced` dispatches by the event's own fold name.

    Idempotent: a second call disposes the earlier subscription first, so a gateway
    restarted inside one interpreter is never subscribed twice.

    Returns ``True`` when a subscription was installed.
    """
    global _dispose
    try:
        from kiro_crew.metrics.provider import get_recorder

        if not get_recorder().enabled:
            return False
    except Exception:  # telemetry must never break the caller
        logger.debug("otel crew-log export install check failed; skipping", exc_info=True)
        return False
    global _install_ms, _evicted_max_opened_ts
    with _install_lock:
        if _dispose is not None:
            _dispose()
            _dispose = None
        try:
            from kiro_crew.crew_log import bus as crew_log_bus
        except Exception:  # pragma: no cover - telemetry must never break the caller
            logger.debug("otel crew-log export bus import failed; skipping", exc_info=True)
            return False
        # Anchor for "new to this process", captured on the same wall clock Entry.time
        # uses. The evicted-max starts at install, so before any eviction a session is
        # new exactly when it opened strictly after install. Set BEFORE subscribing so
        # no delivery can race it.
        with _memo_lock:
            _install_ms = int(time.time() * 1000)
            _evicted_max_opened_ts = _install_ms
        _dispose = crew_log_bus.subscribe(
            crew_log_bus.FOLD_ADVANCED, on_fold_advanced, scope=crew_log_bus.SCOPE_SESSION
        )
    return True


def uninstall_otel_crew_log_subscriber() -> None:
    """Drop the subscription installed by :func:`install_otel_crew_log_subscriber`.

    Idempotent. A TEST SEAM, named as one -- the subscription is process-global the
    same way the crew-log bus's own registry is, so a test installing one must be
    able to remove it without leaking into the next case.
    """
    global _dispose
    with _install_lock:
        dispose, _dispose = _dispose, None
    if dispose is not None:
        dispose()


def reset_for_tests() -> None:
    """Drop the subscription, the per-session memo, and the time anchors. A TEST
    SEAM, named as one."""
    global _install_ms, _evicted_max_opened_ts
    uninstall_otel_crew_log_subscriber()
    with _memo_lock:
        _last_values.clear()
        _install_ms = None
        _evicted_max_opened_ts = None
