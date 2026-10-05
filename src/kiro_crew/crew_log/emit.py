"""Write the ACP turn lifecycle to an append-only per-session crew log.

See ``docs/system-specs/modules/crew-log-emitter.md`` for the contract this
module implements. In short: each entry point is a one-line call at a site in the
dashboard chat path where a lifecycle fact is already known, and every one of
them is **fail-soft** -- a crew log error is reported once and then swallowed, so a
broken crew log can never break a turn.

The module is on by default and inert when ``KIROCREW_CREW_LOG`` is set to a
falsy value (``0``/``false``/``no``/``off``). With the flag off nothing is
created and every call returns immediately.

The grouping identity of a session entry is ``data.turn`` -- the runner's own turn
ordinal -- and, inside a turn, ``data.step``, the MODEL CALL the entry belongs to.
A tool entry also carries ``data.call_index``, its position among that turn's tool
calls, which is what ``step`` meant before a model call had a name of its own: one
model call can issue several tools at once, so the step cannot order them. All are
known at EMIT time, so an entry answers "what happened in this turn" on its own,
with nothing to look up and no earlier line to resolve against.

The envelope's ``thread`` field is left unset here. ``thread`` points at another
LINE's ``seq``, which only a unit whose anchor line is written before the entries
citing it can supply; that is the crew's log shape, not this one's.

Three things are recorded differently from a naive reading of the lifecycle, all
deliberate and all explained in the spec:

* ``compaction/applied`` carries context-usage **percentages**, because the
  compaction boundary never learns a raw token count.
* A tool call and an approval are identified by an id inside ``data``, not by
  ``ref``. A ``Ref`` is a citation of another crew log's lines, and a tool call id
  names a frame on the ACP stream, which is not a crew log unit.
* A turn that ends without its terminal event is still CLOSED, by the turn's own
  ``finally``: ``stop_reason: "failed"``, an ``error`` naming the exception class
  when one was caught, and no ``tokens`` or ``credits``, because none were
  measured. Leaving the start open would say the writer died, which this process
  being alive contradicts -- and nothing here would correct it, since the
  interrupted-turn repair is opt-in and only a resume or a supersede asks for it.
  A recovery re-entry anchors its own thread carrying ``depth``.

**Storage never runs on the caller's thread when that thread is the event
loop.** Every entry point is called from the dashboard's async chat path, and
the storage call underneath takes the unit's lock, reads a bounded tail to
assign ``seq`` and ``fsync``s the appended line -- a waiting ``flock`` and a
kernel ``fsync`` once per tool frame and once per turn, on the one loop that
also drives the liveness heartbeat. So an entry point does only what must be
measured where it is called and builds the storage call as a job for the durable
writer, :class:`kiro_crew.crew_log.writer.CrewLogWriter`, whose single worker (the
pool :func:`kiro_crew.executors.crew_log_executor` builds) drains the work in the
order the call sites produced it.

**Which turn an entry belongs to is carried IN the entry, not looked up.** Every
session entry that belongs to a turn names it in ``data.turn`` -- the runner's own
turn ordinal, which the call site already holds -- plus ``data.step`` for the model
call it happened in, and a tool entry also carries ``data.call_index``. So nothing
has to be cached,
read back from a line written earlier, or kept alive across a queue: an entry is
self-describing the moment it is built, and a lost or evicted piece of in-process
state cannot make it name the WRONG turn.

The envelope's ``thread`` field stays unset on a session entry. ``thread`` points
at another LINE's seq, which is only knowable for a unit whose anchor line is
written before the entries that cite it; that is the crew's log shape, not
this one's.

**Two collaborators own the rest, and this module installs both.** The durable
write policy -- the buffer and its ceilings, retention and backoff, the
``write/dropped`` loss marker, the inline path, the shutdown drain -- is
:mod:`kiro_crew.crew_log.writer`. The live-turn memos -- the step and call ordinals,
the settle-once tool registry, the attempt counts, the supersede repair's debt -- are
:class:`kiro_crew.crew_log.turn_tracker.TurnTracker`. Both are imported and built on
first use rather than at import, for the boot-path reason :func:`_crew_log` gives,
and :func:`reset_caches` retires both so the next use builds fresh ones. New work on
how an append reaches the file belongs in the writer; new per-turn bookkeeping belongs
in the tracker; what a lifecycle fact records belongs here.

With no event loop running the write happens inline on the calling thread --
which is what makes a synchronous caller, and the test suite, deterministic.
:func:`flush` waits for the buffers to drain when a caller needs the file on disk
before it looks, and :func:`drain_for_shutdown` is the quiescence barrier a
restart needs: buffered entries are in memory, so an exit that skips it loses the
last thing each session did.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import math
import sys
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.constants import CREW_LOG_ENV, crew_log_enabled
from kiro_crew.executors import crew_log_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    from concurrent.futures import Executor

    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.store import CrewLog
    from kiro_crew.crew_log.turn_tracker import TurnTracker
    from kiro_crew.crew_log.writer import CrewLogWriter, WarningBudget, WriteJob, WriterLimits

logger = logging.getLogger(__name__)

_KIND = "session"

#: Facts read off the ACP stream vs. facts the gateway decided by itself.
_SRC_ACP = "acp"
_SRC_GATEWAY = "gateway"

#: Used when a call site has no agent name, matching the repo's own default.
_DEFAULT_AGENT = "kirocrew"

#: Characters per token, the repo's own estimate for a prompt it cannot tokenize
#: exactly (``dashboard/handlers/usage.py`` applies the same 4.0). Restated here
#: rather than imported: that module is a dashboard handler and this one is
#: imported BY the dashboard chat path, so importing it back would close an
#: import cycle. Any block count derived from it is an ESTIMATE and the spec says
#: so -- the only exact tokenizer available is the wrong one for the served model.
_EST_CHARS_PER_TOKEN = 4.0

#: Where a ``message/chunk`` slice STARTS when a body is too big for a single
#: line. Not a guarantee: :func:`_text_slices` measures each piece and halves it
#: until it fits, because ``ensure_ascii`` escapes per code UNIT -- six bytes for a
#: BMP character, twelve for a surrogate pair -- so no character count can be
#: turned into a byte budget in advance.
_CHUNK_TEXT_CHARS = 8 * 1024

#: Bytes reserved on a line for everything that is not the body: the envelope,
#: the turn and step ordinals, a cited chunk list. Generous on purpose -- an
#: append refused for being one byte over is a body silently missing from the
#: log, and the cost of over-reserving is one extra chunk.
_ENVELOPE_HEADROOM = 4 * 1024

#: The label a source with NO NAME AT ALL is reported under. ``split_blocks``
#: classifies by opening marker, and three blocks the design names -- steering, tool
#: specs, injected crew log context -- have none, so their characters land in its
#: ``unclassified`` bucket.
#:
#: That bucket keeps its own name and is NOT renamed here. It is a label the readers
#: already know: the Context panel carries a translated string for ``unclassified``
#: and none for ``other``, so folding the two together made a named remainder render
#: as an untranslated word in every shipped locale -- visible the moment that panel
#: started reading these sources instead of the token row's own ``split_blocks``
#: output. Only the empty label lands here, because an empty label names nothing and
#: a reader cannot be given a translation for it.
_OTHER_SOURCE = "other"
_UNCLASSIFIED_LABELS = frozenset({""})

#: Who caused a turn to run. Every value names a STRUCTURAL producer the
#: dispatch layer identifies; ``user`` means a person typed the message,
#: ``app`` means an installed app's backend sent it under its own token, and
#: ``gateway`` means the gateway synthesized it and no narrower producer fits
#: (a task-runner summary, an orchestrator stage). Anything unrecognised is
#: recorded as ``other`` rather than guessed.
#:
#: A reader never gates on this field, so a producer landing later widens the
#: set without breaking a reader built against the narrower one -- unlike an
#: entry TYPE, which aborts reconstruction when a reader does not know it.
ACTORS = frozenset({"user", "app", "crew", "cron", "autonudge", "subagent", "gateway", "other"})

#: Bounded so a long-lived gateway cannot grow any map without limit.
_MAX_OPEN_CREW_LOGS = 128

#: Ceiling for a SHORT field -- an approval's shown reason, a plan item's text, a
#: child's failure reason. These are not bodies: a body goes through
#: :func:`_append_body_entry`, which slices an oversize one into ``message/chunk``
#: entries so nothing is lost. A short field has no such path, so the choice is
#: between clipping it and letting one long value push the whole entry past
#: ``MAX_ENTRY_BYTES`` -- where the append is REFUSED and the fact disappears with
#: it. Clipping loses a tail; refusing loses the record.
_MAX_SHORT_TEXT = 512

#: Ceiling for an identifier the agent chose (a plan item's id). Short because a
#: value longer than this is not an identifier.
_MAX_ID_TEXT = 64

#: How many plan items one ``plan/updated`` entry carries. The agent re-sends its
#: whole list on every change, so a long plan is re-serialized on each update; the
#: entry keeps the real count in ``total`` when it clips.
_MAX_PLAN_ITEMS = 100

#: Guards this module's own maps below. The writer and the turn tracker each hold their
#: own lock; when this one is held while calling the tracker (its ``trim`` and
#: ``pop_unless_live``), the order is always this lock first, and nothing the tracker
#: does reaches back for it. The writer's adapters (``_handle``, ``_notify_growth``,
#: ``_writer_pool``) DO take this lock, and an inline submit runs them on the caller's
#: thread, so the writer is never called while this lock is held.
_lock = threading.Lock()
#: session -> its open crew log handle, bounded by :data:`_MAX_OPEN_CREW_LOGS` under the
#: tracker's never-evict-a-live-turn rule. This cache IS the writer's ``open_unit``
#: adapter (see :func:`_handle`).
_open: "OrderedDict[str, Any]" = OrderedDict()
#: Sessions whose crew log CREATION failed permanently -- the file-creating record
#: was refused or spent its attempt budget, so no crew log file exists and no later
#: append for the session can ever land. Distinct from a session that legitimately
#: has none (feature off, never opened): that stays a silent policy no-op, this
#: makes later discards COUNT as loss instead of returning a silent None. Cleared
#: when the session closes and by ``reset_caches``; a permanently failed creation does
#: not recover on its own.
_creation_failed: "set[str]" = set()
#: How many child origins were dropped while their child was still running, so
#: the entries that pin would have carried are absent from that session's log.
#: Counted rather than only logged: a hole an append-only reader cannot see is the
#: one loss this module refuses, and this sits beside the other counts a shutdown
#: report shows. Reaching it takes the whole cap's worth of children live at once.
_lost_child_origins = 0
#: Whether a lost origin is named in the log already, so the state is reported
#: once rather than once per eviction.
_lost_origin_reported = False
#: True once the backstop ``atexit`` drain has been registered. Deliberately NOT
#: cleared by ``reset_caches``: the registration is a property of this process, not
#: of the writer's state, and clearing it would let a second registration stack up
#: another drain on exit.
_shutdown_hook_registered = False
#: session -> the last request configuration written for it, as a comparable
#: tuple. ``request/configured`` is a CHANGE record: rewriting an identical
#: configuration every turn would bury the turns where it actually moved, which
#: is the only thing a reader wants from it. Keyed per session, not per turn,
#: because the configuration outlives a turn.
_last_config: "OrderedDict[str, tuple[Any, ...]]" = OrderedDict()
#: session -> the last class this process stated for it, as ``(memory, app,
#: channel)``. ``session/class`` is a CHANGE record for the same reason
#: ``request/configured`` is: the class holds still for the whole life of most
#: sessions, and restating it every turn would bury the turn where it moved.
#:
#: A MISSING entry is treated as a change rather than as agreement, so the bound
#: below can cost a redundant line but never a missed restriction. That direction
#: is deliberate: the fold that reads these entries takes the most restrictive
#: value each member ever held, so a duplicate changes no verdict, while a
#: dropped transition would silently widen who may read the log.
_last_class: "OrderedDict[str, tuple[str, str, bool, str]]" = OrderedDict()
#: dispatched child id -> (parent session id, the parent turn that asked, opened).
#: A child's spawn entry, steer and terminal outcome are all produced after the
#: asking turn has ended, so the ordinal cannot be re-derived when they land; it is
#: captured at the dispatch and read back here. ``opened`` says whether the run
#: actually STARTED: a spawn is pinned when accepted and promoted only at the site
#: that begins the run, so a spawn declined at the approval gate closes nothing.
#: NOT trimmed by turn liveness like the maps above -- an entry deliberately
#: OUTLIVES the turn that created it, which is the whole reason it exists -- so it
#: is bounded FIFO and released by the child's own terminal entry.
_child_origin: "OrderedDict[str, tuple[str, int, bool]]" = OrderedDict()
#: Answers "is this ``agent_id`` still running IN THIS PROCESS", registered by the
#: gateway because it owns the subagent manager and this module cannot reach it.
#: Consulted only by the resume repair, to decide whether an unmatched
#: ``subagent/spawned`` may be closed. It is deliberately NOT ``_child_origin``:
#: that map is this module's own bookkeeping and ``reset_caches`` clears it, which
#: is exactly the idle-teardown path where a child keeps running -- reading it
#: would report a live child as finished. Unset means no repair closes a child.
_child_liveness: "Callable[[str], bool] | None" = None

#: Consumers to wake when a session's log grows. Registered by
#: :func:`add_growth_listener`, held here rather than imported so the writer
#: names no reader. Survives ``reset_caches``: a listener is installed once per
#: process, and each writer generation reports growth through the same list.
_growth_listeners: "list[Callable[[str], None]]" = []

#: Guards installing the collaborators below. Its own lock, so a caller already
#: holding :data:`_lock` (the handle cache trimming through the tracker) can install
#: the tracker without self-deadlocking.
_install_lock = threading.Lock()
#: The installed durable writer, the turn tracker and the shared warning budget, each
#: built on first use (see the module docstring). ``None`` until then, and again after
#: :func:`reset_caches`.
_writer_instance: "CrewLogWriter | None" = None
_tracker_instance: "TurnTracker | None" = None
_warnings_instance: "WarningBudget | None" = None
#: The limits the NEXT writer is built with, set by :func:`reset_caches` -- ``None``
#: means the writer's defaults.
_next_limits: "WriterLimits | None" = None

#: How long :func:`drain_for_shutdown` gives the writer by default. Bounded so a wedged
#: filesystem delays exit rather than hanging it.
_SHUTDOWN_DRAIN_SECONDS: Final[float] = 5.0


_subsystem: Any = None


def _crew_log() -> Any:
    """The storage package, imported the first time a call actually needs it.

    This module is reachable from the gateway boot path, and AUTOSDE's
    ``no-new-work-on-gateway-boot-path`` rule asks for an optional subsystem's
    IMPORT to be gated, not merely its calls. So the split is: THIS module is the
    gate -- pure glue, no import-time work, no shutdown hook registered until a
    write happens -- and the package it fronts (the store, the schema and the
    lease) stays unloaded until one of the entry points below reaches storage. A
    launch with ``KIROCREW_CREW_LOG`` switched off never imports it, because
    ``enabled()`` refuses before any of those paths is taken.
    """
    global _subsystem
    if _subsystem is None:
        from kiro_crew import crew_log

        _subsystem = crew_log
    return _subsystem


def enabled() -> bool:
    """True when the emitter should write. Read per call, never cached."""
    return crew_log_enabled()


def _warnings() -> "WarningBudget":
    """The warning budget every crew log failure in this process is reported through."""
    global _warnings_instance
    budget = _warnings_instance
    if budget is None:
        with _install_lock:
            if _warnings_instance is None:
                from kiro_crew.crew_log.writer import WarningBudget

                _warnings_instance = WarningBudget(logger)
            budget = _warnings_instance
    return budget


def _writer() -> "CrewLogWriter":
    """The installed durable writer, built on first use.

    Its adapters are this module's: :func:`_late_handle` opens a unit for the writer's
    own loss markers, :func:`_writer_pool` hands it the one-worker crew-log pool (and
    registers the exit drain on first use), :func:`_notify_growth` tells the registered
    listeners a log grew, and :func:`_late_retry_delay` is the retry schedule. The two
    ``_late_*`` adapters read their target by name at call time, because tests patch
    :func:`_handle` and :func:`_retry_delay` here, and a writer built while a patch was
    in force must not keep it after the patch is undone. Logging stays on this module's
    logger, so every record the write path produces carries one logger name.
    """
    global _writer_instance
    writer = _writer_instance
    if writer is None:
        warnings = _warnings()
        with _install_lock:
            if _writer_instance is None:
                from kiro_crew.crew_log.writer import DEFAULT_LIMITS, CrewLogWriter

                _writer_instance = CrewLogWriter(
                    _late_handle,
                    executor=_writer_pool,
                    limits=_next_limits if _next_limits is not None else DEFAULT_LIMITS,
                    retry_delay=_late_retry_delay,
                    on_grew=_notify_growth,
                    warnings=warnings,
                    logger=logger,
                )
            writer = _writer_instance
    return writer


def _tracker() -> "TurnTracker":
    """The installed turn tracker, built on first use."""
    global _tracker_instance
    tracker = _tracker_instance
    if tracker is None:
        with _install_lock:
            if _tracker_instance is None:
                from kiro_crew.crew_log.turn_tracker import TurnTracker

                _tracker_instance = TurnTracker(max_sessions=_MAX_OPEN_CREW_LOGS, logger=logger)
            tracker = _tracker_instance
    return tracker


def _writer_pool() -> "Executor":
    """The writer's executor adapter: the crew-log pool, with the exit drain registered.

    The registration happens HERE, immediately before the pool is first asked for work,
    so ``atexit``'s last-registered-first order still runs the pool's own handler ahead of
    this module's drain (see :func:`_ensure_shutdown_hook`).
    """
    _ensure_shutdown_hook()
    return crew_log_executor()


def _late_retry_delay(attempts: int) -> float:
    """The writer's retry schedule, read through :func:`_retry_delay` at call time."""
    return _retry_delay(attempts)


def _late_handle(session_id: str) -> Any:
    """The writer's ``open_unit`` adapter, read through :func:`_handle` at call time."""
    return _handle(session_id)


def _retry_delay(attempts: int) -> float:
    """How long to wait before retry number *attempts* + 1. Seconds.

    The writer's schedule (``WriterLimits.retry_delay``), exposed here as one named
    thing because the writer this module installs reads it through this name at call
    time: a test shrinks it to zero and drives the whole attempt budget
    deterministically, instead of waiting out real backoff under load and asserting on
    a stopwatch. Called under the writer's lock, so it must not call back into the
    writer.
    """
    writer = _writer_instance
    if writer is None:
        from kiro_crew.crew_log.writer import DEFAULT_LIMITS

        return DEFAULT_LIMITS.retry_delay(attempts)
    return writer.limits.retry_delay(attempts)


def flush(timeout: float = 5.0) -> bool:
    """Wait until no append is queued. True when the queue drained in time.

    For a caller that must read the file it just wrote -- a test, a shutdown
    path -- since an entry point returns as soon as the work is HANDED to the
    writer. Never called from the turn path: waiting there would reintroduce the
    block this queue exists to remove.

    Terminates even against a filesystem that never answers: a batch the writer
    cannot write is retried a bounded number of times and then dropped, so the
    buffer reaches empty rather than holding entries no wait could ever satisfy.
    A process that never wrote has no writer, and nothing to wait for.
    """
    writer = _writer_instance
    return True if writer is None else writer.flush(timeout=timeout)


def dropped_writes() -> int:
    """How many appends were given up on after their attempt budget was spent.

    That is the only way an append is abandoned: the filesystem kept refusing it.
    Nothing is discarded for backlog depth, however deep it gets, so a non-zero
    reading always names a storage refusal rather than pressure.

    Normally 0, and a non-zero reading is a real hole in one or more session logs:
    the writer could not append those entries and stopped trying. It is counted
    rather than merely logged because the alternative to a bounded loss is an
    unbounded wait -- entries live in memory until they are written, so a wedged
    filesystem would otherwise hold them forever and make every bounded caller
    time out. A loss a caller can read is the lesser failure, and this is how it
    is read.
    """
    writer = _writer_instance
    return 0 if writer is None else writer.stats().dropped


def overflow_writes(session_id: str | None = None) -> int:
    """How many appends were rejected for crossing the buffer's memory ceiling.

    With *session_id*, only that session's rejections: a caller judging its own
    append reads this figure before and after, and another session's rejection in
    the same window must not read as its own.

    Distinct from :func:`dropped_writes`: that names a storage refusal the writer
    gave up on, this names backpressure the buffer refused to hold once it reached
    its count or byte ceiling (``WriterLimits.max_pending_count`` /
    ``max_pending_bytes``). Normally 0. A non-zero reading means the writer fell so
    far behind that the backlog became a memory-exhaustion risk, and the newest
    entries of the overwhelmed session were rejected -- at the tail, so that log is
    short by them from a named point rather than holed in its middle. Reading it
    apart from :func:`dropped_writes` is how a stuck disk is told from a saturated one.
    """
    writer = _writer_instance
    if writer is None:
        return 0
    if session_id is not None:
        return writer.overflowed_for(session_id)
    return writer.stats().overflowed


def lost_child_origins() -> int:
    """How many children lost their pinned origin while they were still running.

    A pin carries the parent session and asking turn every later entry about that
    child reuses, and it is released by the child's own terminal entry. Dropping a
    live one costs that child its remaining entries: its opener when the pin goes
    before the run starts, its outcome when the pin goes after. Distinct from
    :func:`dropped_writes` and :func:`overflow_writes`, which count entries the
    writer refused; this counts attribution the map could not keep, so the entries
    are never composed at all.

    Normally 0, and reaching it needs the pin cap's worth of children running at
    once: a pin whose child has finished is dropped in preference, which is what
    keeps children lost to a crash from filling the map over long uptime.

    The same count also covers a pin REFUSED because its session id is longer than
    :data:`_MAX_SESSION_ID_CHARS`. One count for both, because the consequence a
    reader cares about is identical -- that child's entries are absent -- and the
    log line names which cause fired.
    """
    with _lock:
        return _lost_child_origins


def buffered_writes() -> int:
    """How many appends are waiting on the writer right now."""
    writer = _writer_instance
    return 0 if writer is None else writer.stats().buffered


def peak_buffered_writes() -> int:
    """The most appends ever waiting at once since the last reset."""
    writer = _writer_instance
    return 0 if writer is None else writer.stats().peak_buffered


def reset_caches(*, limits: "WriterLimits | None" = None) -> None:
    """Drop cached handles, live turns and timings. Tests and restart.

    Retires the installed writer first -- :meth:`CrewLogWriter.close` waits a bounded
    moment for what it owes, because a job still holding a stale handle would otherwise
    write after the reset that was meant to forget it, and then DISCARDS whatever it
    could not write. Reaching that point means the writer is wedged, and the handles
    those jobs would have written through are being dropped in this same call -- so
    keeping them would leave the writer retrying entries against a crew log this
    process has stopped believing in. A successful wait makes the discard a no-op,
    which is every call that is not recovering from a wedge. The writer is retired
    while still installed, so a job that queues follow-on work during that wait queues
    it into the writer being retired rather than building a new one.

    Then the writer, the turn tracker and the warning budget are uninstalled, so the
    next use builds fresh ones -- with *limits* for the next writer when given, which is
    how a test asks for small ceilings -- and this module's own maps are cleared. The
    growth listeners, the child-liveness probe and the exit-hook registration are
    properties of the process and survive.
    """
    global _writer_instance, _tracker_instance, _warnings_instance, _next_limits
    global _lost_child_origins, _lost_origin_reported
    writer = _writer_instance
    if writer is not None:
        # Bounded well below the default: this wait exists so a job holding a stale
        # handle cannot write after the reset, and it returns the instant the writer is
        # quiet. When the writer is GONE -- the pool shut down with work still owed --
        # no job can run at all, so waiting the full default would stall every caller
        # for nothing and the discard is the answer either way.
        writer.close(timeout=2.0)
    with _install_lock:
        _writer_instance = None
        _tracker_instance = None
        _warnings_instance = None
        _next_limits = limits
    with _lock:
        _open.clear()
        _last_config.clear()
        _last_class.clear()
        _child_origin.clear()
        _creation_failed.clear()
        _lost_child_origins = 0
        _lost_origin_reported = False


def session_id_of(client: Any) -> str:
    """The ACP session id behind a session handle, or ``""`` when it has none.

    A provider exposes it as ``session_id`` and the inner client as
    ``_session_id``; a turn that failed before ``session/new`` has neither, and
    an empty id makes every call in this module a no-op.
    """
    for candidate in (client, getattr(client, "client", None)):
        if candidate is None:
            continue
        for attr in ("session_id", "_session_id"):
            value = getattr(candidate, attr, "")
            if isinstance(value, str) and value:
                return value
    return ""


def _report(what: str, exc: BaseException, *, op: str) -> None:
    """Report a crew log failure through the shared warning budget.

    One budget for the whole write path, writer and emitter alike, so a kind of failure
    is named once per window however many sites hit it -- see
    :class:`kiro_crew.crew_log.writer.WarningBudget` for the key and the text-only
    record rule. Takes the budget's own lock, so it is never called with :data:`_lock`
    held.
    """
    _warnings().report(what, exc, op=op)


def add_growth_listener(listener: "Callable[[str], None]") -> None:
    """Call *listener* with a session id after that session's log GROWS.

    The one signal a consumer of this stream needs and cannot get from the file:
    that there is something new to read. It fires once per drained batch rather
    than once per entry, because the write-behind already groups a turn's burst
    into one pass, and a listener woken per entry would do the same work several
    times over the same read.

    Registered rather than imported: this module is imported BY the dashboard, so
    calling into a dashboard publisher from here would close an import cycle and
    would put a consumer's name in the writer's own code. A listener that raises
    is reported like a failed write and cannot stop the drain.

    It runs on the WRITER thread, so a listener that does real work must hand it
    to its own loop. Registering the same callable twice registers it twice; the
    gateway installs its publisher once, at startup.
    """
    with _lock:
        _growth_listeners.append(listener)


def _notify_growth(session_id: str) -> None:
    """Tell every listener *session_id* has new entries. Never raises."""
    with _lock:
        listeners = list(_growth_listeners)
    for listener in listeners:
        try:
            listener(session_id)
        except Exception as exc:  # pragma: no cover - a listener's own failure
            _report("growth listener", exc, op="growth-listener")


#: The one entry type whose payload names a BOARD other than its unit's own slot. A
#: worker's report carries the conductor's ``slot``, so that is the fold it belongs to.
#: Spelled here rather than imported from ``entry_types``: this module is the boot-path
#: import gate (see ``_crew_log``), and one string is cheaper than pulling the vocabulary
#: in. ``test_the_real_append_path_wakes_the_eager_fold`` drives this path end to end.
_WORK_TYPE: Final[str] = "work/recorded"


def _note_eager(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    """Tell the eager folder one entry of *entry_type* committed. Never raises.

    Called from inside the append job, immediately after the append returned -- the same
    place the causal-order publish goes, and for the same reason: until the entry is
    really on disk there is nothing to fold, and a fold run before it would have to be
    run again.

    The whole call is one ``put_nowait`` behind a set membership test
    (:func:`kiro_crew.crew_log.eager.note_commit`). It does not resolve the slot, fold
    anything or build a frame: this runs on the writer thread that every append of this
    session is serialized through, so work done here is latency for the next entry.

    The BOARD is read from *data* here rather than at each call site, so the rule lives in
    one place. Only ``work/recorded`` carries a board of its own: a worker's report names
    the CONDUCTOR's slot, which is not what the worker unit's header says, so folding by
    the header would advance the worker's board and leave the conductor's -- the one a
    dashboard reads -- stale. Every other type has no board field and the header is right
    for it, which is what an empty value asks the folder to use.

    The import is function-local, which is this module's standing rule for anything that
    reaches the fold surface -- a launch that never commits an eager entry never loads
    it.
    """
    seq = int(getattr(entry, "seq", 0) or 0)
    if seq <= 0:
        return
    try:
        # boot-path import gate, the same one ``_crew_log`` above documents: this module is
        # reachable from the gateway's boot path and the fold surface is not, so the import
        # is paid by the first process that actually commits an eager entry.
        from kiro_crew.crew_log import eager

        board = str(data.get("slot") or "") if entry_type == _WORK_TYPE else ""
        eager.note_commit(session_id, entry_type, seq, board)
    except Exception:  # pragma: no cover - a cache must not cost a committed entry
        # Rendered text, never ``exc_info``: this runs inside the append job, whose frame
        # binds the live ``CrewLog`` whose finalizer releases the write lease, so a record
        # carrying the traceback would keep that handle and its lease alive past the drop
        # that should have released it. The store's ``log_exception_text`` does exactly
        # this, but this module is the boot-path import gate (see ``_crew_log``) and may
        # not import the store at module level, so the render uses the ``traceback``
        # module already imported above. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "crew log eager wake not delivered for %s:\n%s",
                entry_type,
                traceback.format_exc().rstrip(),
            )


def _submit(
    job: Callable[[], None],
    what: str,
    session_id: str,
    nbytes: int = 0,
    *,
    after: Callable[[], None] | None = None,
    on_permanent_drop: Callable[[], None] | None = None,
) -> None:
    """Hand one ordinary append to the writer.

    The writer's contract applies (:meth:`kiro_crew.crew_log.writer.CrewLogWriter.submit`):
    a lifecycle record is never dropped for backpressure alone, the event loop is never
    blocked, and with no event loop running the write happens inline, ordered against
    the writer. ``after`` runs once the append lands or is definitively dropped;
    ``on_permanent_drop`` runs, ahead of it, only when it is given up on.
    """
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        session_id,
        WriteJob.append(job, what, nbytes=nbytes, after=after, on_drop=on_permanent_drop),
    )


def _hand_over(session_id: str, job: "WriteJob") -> None:
    """Submit *job* for *session_id*, and settle it here if the ceiling rejected it."""
    if _writer().submit(session_id, job):
        return
    _settle_rejected(session_id, job)


def _settle_rejected(session_id: str, job: "WriteJob") -> None:
    """Finish a job the writer rejected at its memory ceiling. Never raises.

    The rejected entry never entered the buffer, so nothing will ever run its cleanup
    unless it runs here -- release the pin it holds now, and let the writer keep
    draining the entries that did fit. A ceiling rejection is a PERMANENT loss of this
    entry, exactly like a spent retry budget, so the tail repair that stood down for a
    dropped terminal must be re-queued here too: a terminal handed over for a
    superseded crew log carries :func:`_terminal_dropped`, and its loss is the trigger
    that re-queues the repair. Skipping it leaves the repair debt set with nothing to
    consume it, so the predecessor's tail stays open for the life of the file.

    TWO constraints pull in opposite directions, and neither ordering of "finish" and
    "fire the hook" satisfies both, because finishing does two things at once. The
    terminal's ``after`` is :func:`_closing`'s release, which (a) releases the turn's
    live pin AND (b) clears the repair debt once no live turn of the session remains.

    * Fire the hook BEFORE finishing: the hook re-queues a repair that runs on the
      WRITER thread and can observe this turn's pin still live (our finish not yet
      run), stand down, re-record the debt -- which our finish then erases, with no
      trigger left. (cross-thread race)
    * Fire the hook AFTER finishing: finishing has already cleared the debt, so the
      hook reads an empty slot and re-queues nothing.

    So neither is done blindly. The debt is read BEFORE finishing (preserved through
    the cleanup), the finish releases the live-turn pin, and the repair is re-queued
    from that reading -- now the pin is gone, so the repair closes the tail truthfully
    instead of standing down against our own dying turn. :func:`_queue_tail_repair` is
    the one submission site and is guarded, so re-queuing from the reading is exactly
    what the hook would have done, minus the stale read. A job with no debt behind it
    fires its own drop hook as-is.
    """
    tracker = _tracker_instance
    owed_slot = tracker.repair_debt(session_id) if tracker is not None else ""
    if job.after is not None:
        try:
            job.after()
        except Exception as exc:
            _report(f"finishing {job.what}", exc, op="finish-write")
    if owed_slot:
        # A crew-log terminal stood a repair down; its pin is released now, so re-queue
        # the repair directly from the reading. The finish may already have consumed
        # the debt, so consume the reading rather than reading again.
        if tracker is not None:
            tracker.take_repair_debt(session_id)
        logger.warning(
            "session log %s: the terminal of superseded crew log %s was REJECTED "
            "at the pending ceiling, so re-queueing the tail repair that stood "
            "down for it -- the turn it deferred to has no outcome coming now, "
            "and its tail would otherwise stay open for the life of the file",
            owed_slot,
            session_id,
        )
        _queue_tail_repair(session_id, owed_slot)
    elif job.on_drop is not None:
        try:
            job.on_drop()
        except Exception as exc:
            _report(f"flagging a permanent drop of {job.what}", exc, op="flag-permanent-drop")


def _writer_owes(session_id: str) -> bool:
    """Whether the installed writer still owes *session_id* anything. Takes no lock here."""
    writer = _writer_instance
    return writer is not None and writer.owes(session_id)


def _ensure_shutdown_hook() -> None:
    """Register the backstop drain once, on the first pass that needs a writer.

    Registered HERE rather than at import for the boot-path rule: a launch with
    the flag unset never reaches a drain pass, so it registers nothing. Ordering
    is preserved because ``atexit`` runs handlers last-registered-first and this
    runs immediately BEFORE the pool is first asked for work -- so the executor's
    own hook, registered when it builds that pool, still runs ahead of this drain.
    """
    global _shutdown_hook_registered
    with _lock:
        if _shutdown_hook_registered:
            return
        _shutdown_hook_registered = True
    atexit.register(drain_for_shutdown)


def drain_for_shutdown(timeout: float = _SHUTDOWN_DRAIN_SECONDS) -> bool:
    """Write out everything buffered, then let the eager folder finish what it was handed.

    Returns whether the LOG is complete, which is the record:
    :meth:`~kiro_crew.crew_log.writer.CrewLogWriter.drain_for_shutdown` is the writer's
    bounded quiescence barrier, and it names what it could not write. A process that
    never wrote has no writer and nothing to drain. The fold step after it is part of
    the quiescence barrier and not of that answer: the eager folder runs on its own
    thread and writes savepoints into the data home, so a caller that tears the home
    down (a test's temp directory, a gateway exiting) right after this call would
    otherwise race a fold still reading or writing there. It is waited for within what
    is left of *timeout*, with a short floor so a writer that spent the whole budget
    does not skip it entirely, and only when that module is loaded -- a process that
    never folded eagerly pays nothing. A fold that does not settle in time costs a
    savepoint, never an entry.
    """
    started = time.monotonic()
    writer = _writer_instance
    drained = True if writer is None else writer.drain_for_shutdown(timeout).drained
    eager = sys.modules.get("kiro_crew.crew_log.eager")
    settle = getattr(eager, "drain", None)
    if settle is not None:
        budget = max(started + timeout - time.monotonic(), _EAGER_SETTLE_FLOOR_SECONDS)
        try:
            if not settle(budget):
                logger.debug("crew log eager folds still in flight %.1fs into shutdown", budget)
        except Exception:  # pragma: no cover - a fold must not cost the shutdown
            # Rendered text, never ``exc_info``, for the reason ``_note_eager`` gives.
            logger.debug(
                "crew log eager settle failed at shutdown:\n%s", traceback.format_exc().rstrip()
            )
    return drained


#: The least time :func:`drain_for_shutdown` gives the eager folder to settle, even when
#: the writer spent the whole budget: one batch of folds over a busy session's tail.
_EAGER_SETTLE_FLOOR_SECONDS: Final[float] = 1.0


def live_turn(session_id: str) -> int:
    """The ordinal of *session_id*'s running turn, or 0 when none is running.

    For callers that must name the turn an entry belongs to but do not hold the
    ordinal themselves. The live record is opened by ``on_turn_started``, which
    runs BEFORE the ACP client is published on the slot -- so a caller reading a
    slot attribute assigned later in the turn sees 0 or the PREVIOUS turn's
    ordinal in that window, and attributes its entry to a turn that did not
    produce it. Reading the record closes that window.

    The HIGHEST live ordinal, because a session can hold more than one: a nested
    turn pins its own record while its parent's is still open, and the newest is
    the one currently producing entries.

    Returns 0 rather than raising when nothing is running. 0 is not a turn, so a
    caller must treat it as "no running turn" and record the fact it actually has
    -- not stamp 0 on an entry.
    """
    tracker = _tracker_instance
    if not session_id or tracker is None:
        return 0
    return tracker.live_turn(session_id)


def _bound_open() -> None:
    """Trim the handle cache. ``_lock`` held. See :meth:`TurnTracker.trim` for the rule."""
    _tracker().trim(_open, _MAX_OPEN_CREW_LOGS, lambda k: k, "open handles")


def _remember(session_id: str, log: CrewLog) -> None:
    with _lock:
        _open[session_id] = log
        _open.move_to_end(session_id)
        _bound_open()


def _handle(session_id: str) -> Any:
    """An open crew log for *session_id*, or None when it has none. Never creates one.

    The writer's ``open_unit`` adapter, and the handle every job here appends through.

    None means exactly one thing: this session has no crew log file AND its creation
    was never attempted (feature off, never opened), so an event for it is
    deliberately not written rather than starting a crew log with no header. That is
    a policy no-op, not a loss, and it is not counted as one.

    A session whose creation FAILED permanently is different: it was opened, so its
    later entries are a real loss. That case raises a refusal rather than returning
    None (see the ``_creation_failed`` branch below), so the entry is dropped and
    counted instead of vanishing as a silent no-op.

    A FAILURE to reopen is different and is allowed to propagate. This runs inside
    a queued job, so the exception reaches the writer and the entry is
    retained and retried like any other failed append -- where returning None would
    make the job succeed with nothing written, and the entry would vanish with no
    retry and no entry in :func:`dropped_writes`. That is the same silent hole the
    retention exists to close, one layer up. The ``exists`` stat is inside that
    rule too: a stat that raises has not answered whether the crew log is there, and
    reading it as absent would discard the entry on a guess.

    Runs on the writer, so the stat and the open read are off the loop like the
    append they precede.
    """
    if not session_id or not enabled():
        return None
    with _lock:
        cached = _open.get(session_id)
        if cached is not None:
            _open.move_to_end(session_id)
            return cached
        creation_failed = session_id in _creation_failed
    if not _crew_log().CrewLog.exists(_KIND, session_id):
        if creation_failed:
            # The file-creating record died permanently, so this session's crew log
            # will never exist -- but it was OPENED, so its later entries are a real
            # loss, not the silent no-op an unopened session gets. Raise a refusal
            # rather than return None: the exception reaches the writer, which reads a
            # CrewLogError as a refusal (never retried), and the entry is dropped AND
            # counted in dropped_writes, with a write/dropped marker owed, instead
            # of vanishing uncounted.
            raise _crew_log().CrewLogError(
                "session log creation failed permanently; entry cannot be recorded",
                code=_crew_log().CODE_NO_LEDGER,
            )
        return None
    # RECONNECT, never a resume: this path is reached when a handle is missing
    # from the cache, which says nothing about the writer's health -- an
    # eviction is enough. So it opens WITHOUT repair; closing a turn here
    # would close one that is still running.
    log = _crew_log().CrewLog.open(_KIND, session_id)
    _remember(session_id, log)
    return log


def _seed_attempts(session_id: str, log: CrewLog) -> None:
    """Rebuild *session_id*'s attempt map from the entries already in its file.

    Runs on the writer thread, and only when memory cannot answer instead: a
    resume, or a session whose map the bound evicted. Without it a restart between
    two retries of one ordinal resets the count and writes attempt 1 twice, which
    is precisely the collision this field exists to prevent -- and the in-memory map
    cannot survive the restart that makes the collision possible.

    NOT on every open. The scan is O(file) and runs on the single writer thread, so
    seeding a session that already holds its counts would cost one full rescan per
    open against a file that only grows -- paid by every other session's appends
    too, since they queue behind it.

    Best-effort: a file this cannot read leaves the map empty, which degrades to
    the old behaviour rather than failing the resume.
    """
    highest: dict[int, int] = {}
    try:
        # Like read_page, this best-effort scan keeps readable records; folds still refuse seq damage.
        for entry in log.iter_from(1, strict_seq=False):
            if entry.type != "turn/started":
                continue
            turn = entry.data.get("turn")
            if not isinstance(turn, int) or isinstance(turn, bool):
                continue
            attempt = entry.data.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                attempt = 1
            if attempt > highest.get(turn, 0):
                highest[turn] = attempt
    except Exception as exc:
        _report("seeding turn attempts", exc, op="seed-turn-attempts")
        return
    _tracker().seed_attempts(session_id, highest)


def close_open_tool_calls(
    session_id: str,
    turn: int,
    *,
    status: str = "unknown",
    is_error: bool | None = None,
) -> int:
    """Close every tool call of *turn* that is still open. Returns how many.

    A tool that emits no terminal UPDATE sends no result frame at all, so
    ``tool_final`` never arrives and the completion path never runs: its
    ``tool/called`` would stay open for the life of the file, and a fold
    counting open calls would report a turn that never finished using a tool it
    had in fact finished with.
    The transcript already compensates for this on the runner's side -- text after
    a tool group means every tool in it is done -- and this is the same inference
    for the log.

    ``result_bytes`` is 0 rather than absent, and there is no ``result_hash``:
    the tool genuinely produced no bytes, which is a different claim from "the
    payload was not recorded". A closer written here carries no ``elapsed_ms``
    when the call frame is gone from memory, for the same reason.

    *status* is what separates the two callers, and the default is the SAFE one.
    At a tool-group boundary the inference above is grounded -- the model went on
    to say something, so the tools it was waiting on are done -- and that caller
    passes ``"completed"`` explicitly. At turn end nothing of the kind is known:
    the stream may have raised, the process may have died, and a call still open
    there has an outcome no site observed. Recording that as ``"completed"``
    states success in a file nothing rewrites, so the default is ``"unknown"`` --
    the same word ``repair_interrupted_turn`` writes for an unmatched
    ``tool/called``, which is the identical claim reached from the file instead of
    from memory.

    ``is_error`` stays absent on an unknown close rather than being set from the
    turn's own failure. A turn that raised says so in its own terminal; asserting
    the TOOL errored would be a second, unobserved claim.
    """
    if not session_id or not enabled():
        return 0
    # The sweep IS each call's closer, so the tracker marks each one settled: a
    # terminal frame arriving after the sweep closed the call must not write a second
    # ``tool/completed`` -- the same settle-once rule ``on_tool_completed`` applies.
    closed = 0
    for call in _tracker().sweep_open_calls(session_id, turn):
        data: dict[str, Any] = {
            "turn": int(turn),
            "call_id": call.call_id,
            "name": call.name,
            "server": call.server,
            "status": status,
            "result_bytes": 0,
        }
        if call.call_index:
            data["call_index"] = call.call_index
        if call.step:
            data["step"] = call.step
        data["elapsed_ms"] = call.elapsed_ms
        if is_error is not None:
            data["is_error"] = bool(is_error)
        _write(session_id, "tool/completed", data)
        closed += 1
    return closed


def _closing(session_id: str, turn: int) -> "Callable[[], None]":
    """Mark *turn*'s pin as owed to the writer, and return the release for it.

    Called where a terminal event is HANDED OVER, which is not where it lands: the
    entry is queued, and the pin it releases has to outlive the handover so eviction
    cannot take the handle before the closer is on disk. See
    :meth:`TurnTracker.closing` for the release and the repair debt it pays.

    With no tracker installed no turn of this process is pinned, so there is nothing
    to mark and nothing to release -- which is also what keeps a flag-off terminal
    from building one.
    """
    tracker = _tracker_instance
    if tracker is None:
        return _nothing_to_release
    return tracker.closing(session_id, turn)


def _nothing_to_release() -> None:
    """The release of a turn this process never pinned."""


def _safe_text(text: Any) -> str:
    """*text* with secrets removed, or ``""`` when redaction itself failed.

    The same two helpers the transcript store applies before it persists a
    message, in the same order -- exfiltration URLs, then credentials -- so the log
    and the transcript cannot disagree about what a body is allowed to contain.

    Redaction happens HERE rather than being trusted from the call site. Some
    sites hand over text that is already clean (the assistant flush, a streamed
    delta) and some hand over raw input a person just typed; a rule enforced at
    the boundary cannot be forgotten by the next site that is added.

    A redaction failure yields the empty string, never the input. Failing closed
    is the only safe direction: this module's promise is that ``data`` carries no
    sensitive text, whether it came from a message body or a work record.
    """
    if not isinstance(text, str) or not text:
        return ""
    try:
        cleaned, _ = redact_exfiltration_urls(text)
        cleaned, _ = redact_credentials(cleaned)
        return cleaned
    except Exception as exc:
        _report("redacting a body", exc, op="redact-body")
        return ""


_WORK_PLAIN_TEXT_FIELDS = frozenset({"title", "goal", "decision", "summary", "event"})
_WORK_NESTED_TEXT_FIELDS = frozenset({"acceptance", "artifacts"})


class WorkFieldError(ValueError):
    """A work mapping cannot be redacted safely. Answered as a validation refusal."""


class WorkFieldCollisionError(WorkFieldError):
    """A nested work mapping cannot be redacted without losing a key."""


class WorkFieldTooDeepError(WorkFieldError):
    """A nested work mapping is deeper than the redaction walk follows."""


# Far above anything a board writes: the deepest shape these fields take is a
# mapping of mappings, so this refuses no real acceptance or artifact map.
_WORK_MAX_NESTING = 32


def _safe_work_value(value: Any, depth: int = 0) -> Any:
    """Recursively redact string keys and values while preserving field shape.

    The walk recurses once per nesting level and the caller's own JSON decides how
    many there are, so the depth is bounded: unbounded, a deeply nested body
    exhausts the stack and the ``RecursionError`` escapes the route as a 500.
    Refusing at this boundary makes it the same 400 every other unusable field
    gets, and it happens before any lock is taken or byte committed.
    """
    if depth > _WORK_MAX_NESTING:
        raise WorkFieldTooDeepError(
            f"work field nesting is deeper than {_WORK_MAX_NESTING} levels; flatten it"
        )
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [_safe_work_value(item, depth + 1) for item in value]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            safe_key = _safe_text(key) if isinstance(key, str) else key
            if safe_key in cleaned:
                raise WorkFieldCollisionError("work field keys collide after redaction")
            cleaned[safe_key] = _safe_work_value(item, depth + 1)
        return cleaned
    return value


def safe_work_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Copy work-ledger input with every caller-controlled text value redacted.

    The routes apply this copy before either their fit probe or cache commit, so
    the cache and its ``work/recorded`` entry receive byte-identical values.
    ``title``, ``goal``, ``decision`` and ``summary`` are direct prose. Event text
    is derived from those cleaned fields (or a fixed vocabulary), but is included
    so a future direct producer cannot bypass the boundary. ``acceptance`` and
    artifact mappings may contain nested prose, paths or URLs in both their keys
    and values, so every string at that boundary is walked. A post-redaction key
    collision is refused rather than silently discarding one pointer. Identity
    fields and constrained vocabularies are deliberately unchanged.
    """
    cleaned = dict(fields)
    for name in _WORK_PLAIN_TEXT_FIELDS:
        if name in cleaned:
            cleaned[name] = _safe_text(cleaned[name])
    for name in _WORK_NESTED_TEXT_FIELDS:
        if name in cleaned:
            cleaned[name] = _safe_work_value(cleaned[name])
    return cleaned


def _clip(text: str, limit: int) -> str:
    """*text* bounded to *limit* characters, marked when it was cut.

    For SHORT fields only -- see :data:`_MAX_SHORT_TEXT` for why they are clipped
    rather than sliced. The ellipsis is part of the value on purpose: a reader must
    be able to tell a value that ends here from one that was cut, because the two
    support different conclusions and nothing else in the entry says which it is.

    Characters, not bytes. The byte ceiling is enforced by the append itself; this
    bound exists to keep one field from dominating a line, and a character count is
    what a call site can reason about.
    """
    if not text or limit <= 0 or len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "\u2026"


def _text_slices(text: str) -> list[str]:
    """*text* cut into pieces that each MEASURE small enough for one crew log line.

    Measured, not assumed. A character-count budget cannot be derived from the
    byte cap, because ``ensure_ascii`` escapes by code UNIT: a BMP character costs
    six bytes as ``\\uXXXX``, and one outside the BMP costs twelve as a surrogate
    pair. A slice sized for the six-byte case is refused for a body of emoji, and
    a refused chunk aborts the whole split -- so the body is lost entirely, not
    merely cut badly. So each piece starts at :data:`_CHUNK_TEXT_CHARS` and halves
    until it actually fits, which terminates because a single character always
    does.
    """
    slices: list[str] = []
    at = 0
    total = len(text)
    while at < total:
        take = min(_CHUNK_TEXT_CHARS, total - at)
        while take > 1 and not _fits_one_line(text[at : at + take]):
            take //= 2
        slices.append(text[at : at + take])
        at += take
    return slices


def _fits_one_line(text: str, extra: "dict[str, Any] | None" = None) -> bool:
    """Whether *text* can ride on a single entry, envelope included.

    Measured on the ESCAPED form, because that is what goes on the line: the
    crew log serializes with ``ensure_ascii``, so a non-ASCII character costs six
    bytes and a check against the raw length would pass a body the append then
    refuses. :data:`_ENVELOPE_HEADROOM` is left for the rest of the entry.

    *extra* is measured too, because it rides on the SAME line. The headroom is a
    fixed allowance for the envelope's own keys, not a slack fund for caller
    fields: a message carrying many attachment ids can exceed it on its own, and
    then a body that this said would fit is refused by the append and the whole
    message is dropped and counted -- the one outcome chunking exists to avoid.
    Measuring it here moves that message onto the chunked path instead.
    """
    escaped = len(json.dumps(text, ensure_ascii=True).encode("utf-8"))
    if extra:
        escaped += len(json.dumps(extra, ensure_ascii=True, default=str).encode("utf-8"))
    return escaped + _ENVELOPE_HEADROOM <= _crew_log().MAX_ENTRY_BYTES


def _write(
    session_id: str,
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str = _SRC_ACP,
    after: Callable[[], None] | None = None,
    on_permanent_drop: Callable[[], None] | None = None,
    on_settled: "Callable[[bool], None] | None" = None,
    ignorable: bool = False,
) -> None:
    """Queue one entry.

    ``thread`` is never set. A session entry names its turn in ``data.turn``,
    which the runner already knows when it calls -- so nothing has to be looked
    up, cached, or read back from a line written earlier. The envelope's
    ``thread`` field points at another LINE's seq, which is only knowable for a
    unit whose anchor line is written before the entries that cite it; that is
    the crew's log shape, not this one's.

    ``ignorable`` marks an entry a reader may skip when it does not know the
    type. It is the writer's promise that nothing later in the file depends on
    this entry having been interpreted, so only an entry that samples a stream
    carries it.

    ``after`` runs once the append lands or the writer definitively drops it. A
    retryable failure leaves it attached to the retained job.

    ``on_settled`` is handed the same answer with the OUTCOME attached: True only
    when this entry's ``append`` returned. It is for a caller that must not
    publish before the append commits, and it carries :class:`_TreeSettle`'s
    reasoning -- absence of a permanent drop is not success, because an entry
    rejected at the buffer's memory ceiling finishes with no drop hook at all,
    which is precisely the wedged-writer condition the ceiling exists for.
    """
    if not session_id or not enabled():
        if after is not None:
            after()
        if on_settled is not None:
            # No record was asked for, so none is owed. A caller that must not
            # publish without one reads False and says so.
            on_settled(False)
        return

    settle = _tree_settle_hooks(on_settled) if on_settled is not None else None

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            if settle is not None:
                settle.fail()
            return
        entry = log.append(entry_type, data, src=src, ignorable=ignorable)
        if settle is not None:
            settle.wrote()
        # Here rather than at each emitter: this is the append every ordinary entry type
        # goes through, so an entry type that becomes eager later is covered without a
        # second edit. The hook's own membership test drops the types no eager fold
        # names, which is nearly all of them.
        _note_eager(entry, entry_type, session_id, data)

    def _after() -> None:
        if settle is not None:
            settle.after()
        if after is not None:
            after()

    _submit(
        _job,
        f"appending {entry_type}",
        session_id,
        after=_after if (settle is not None or after is not None) else None,
        on_permanent_drop=settle.fail if settle is not None else on_permanent_drop,
    )


def _latch_class(session_id: str, observed: "tuple[str, str, bool, str]") -> None:
    """Remember the class a line just stated for *session_id*.

    Called only AFTER the entry carrying it is on disk, for the reason
    ``on_request_configured`` gives about its own fingerprint: committing the
    value before the append would let one transient failure suppress every later
    statement of the same class, leaving the log permanently without the record.
    """
    with _lock:
        _last_class[session_id] = observed
        _last_class.move_to_end(session_id)
        _tracker().trim(_last_class, _MAX_OPEN_CREW_LOGS, lambda k: k, "session classes")


def _note_class_change(
    session_id: str, log: CrewLog, observed: "tuple[str, str, bool, str] | None"
) -> None:
    """Append ``session/class`` when *observed* is not what this log last stated.

    The class recorded when a log is opened is true of that instant, and a session
    can be given a channel surface, an app owner or a different memory mode while
    it runs. A reader deciding whether one session may read this log has to be able
    to see that, and for a session that has closed the log is the only thing left
    to see it in -- so a move is recorded here rather than left to a live lookup
    that will not be available when the question is asked.

    ``observed`` is ``None`` when the caller supplied no memory mode. Nothing is
    written then, matching the opening entry: a log that states no class refuses
    every test built on one, and appending a transition to it would leave a log
    whose class history has a middle but no beginning.

    A latch MISS appends rather than seeds. The latch is bounded, so an eviction
    is possible while the log stays open, and the two directions are not
    symmetric: a redundant line cannot change a fold that takes the most
    restrictive value each member ever held, while a skipped one silently widens
    who may read the log.
    """
    if observed is None:
        return
    with _lock:
        known = _last_class.get(session_id)
    if known == observed:
        return
    memory, app, channel, workspace = observed
    data: dict[str, Any] = {"memory": memory}
    if app:
        data["app"] = app
    if channel:
        data["channel"] = True
    if workspace:
        data["workspace"] = workspace
    log.append("session/class", data, src=_SRC_GATEWAY)
    _latch_class(session_id, observed)


def on_class_observed(
    session_id: str,
    *,
    memory: str = "",
    app: str = "",
    channel: bool = False,
    workspace: str = "",
) -> None:
    """Record that this session's CLASS is now *(memory, app, channel, workspace)*.

    For the moment a class becomes true rather than the moment someone next samples
    it. The per-turn observation in :func:`on_session_opened` cannot see a channel
    link that commits and is removed inside ONE turn, and content authored through
    that link is in the log with nothing saying it was published -- so the surfaces
    that COMMIT a link call this instead of waiting to be sampled.

    Writes nothing when the class has not moved, and nothing at all when *memory* is
    empty: a log that states no class refuses every test built on one, and appending a
    transition to it would leave a history with a middle and no beginning.

    Returns without waiting, like every other emitter here, which is what lets a
    caller holding a lock use it. That is safe because a write this writer permanently
    loses is itself recorded, and the class fold reads a dropped write as a hole -- so
    a lost move costs a refusal rather than a silent grant.
    """
    if not session_id or not memory:
        return
    observed = (memory, app, channel, workspace)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        _note_class_change(session_id, log, observed)

    _submit(_job, "appending session/class", session_id)


class _TreeSettle:
    """The outcome signal a tree emitter hands to :func:`_submit`.

    Reports what the job DID, not what did not happen to it. ``wrote`` is set inside the
    job immediately after ``log.append`` returns, and ``after`` -- which ``_submit`` runs
    for every terminal outcome -- passes that flag on. Inferring success from the absence
    of a drop hook would be wrong in the arm that matters most: an entry rejected at the
    buffer's memory ceiling is finished WITHOUT ``on_permanent_drop``, which is exactly
    the wedged-writer condition the ceiling exists for, and the caller would be told its
    takeover landed while nothing was appended and the projection never moved.

    ``fail`` remains for the two cases that never reach the job's append at all: a
    permanent drop, and a job that finds no log to write to.
    """

    def __init__(self, on_settled: "Callable[[bool], None] | None") -> None:
        self._on_settled = on_settled
        self._wrote = False
        self._told = False

    def wrote(self) -> None:
        self._wrote = True

    def fail(self) -> None:
        self._wrote = False

    def after(self) -> None:
        if self._on_settled is None or self._told:
            return
        self._told = True
        self._on_settled(self._wrote)


def _tree_settle_hooks(on_settled: "Callable[[bool], None] | None") -> _TreeSettle:
    """One :class:`_TreeSettle` per emitted entry. Trivial, and named so the two tree
    emitters share the wiring rather than repeating it."""
    return _TreeSettle(on_settled)


#: How long :func:`awaiting_commit` waits before it answers False. Bounded because a
#: retryable write stays queued against a filesystem that may never answer, and the
#: callers are user-facing requests. Matched to :func:`flush`'s own default, the other
#: place that waits on this writer.
COMMIT_WAIT_SECONDS = 5.0


async def awaiting_commit(
    emit_one: "Callable[[Callable[[bool], None]], None]",
    *,
    what: str,
    timeout: float = COMMIT_WAIT_SECONDS,
) -> bool:
    """Queue one append through *emit_one* and wait, bounded, for it to COMMIT.

    For a caller that will PUBLISH on the strength of the append -- tell a user the
    card is gone, drop a run from live state. This queue returns as soon as the
    entry is handed over, so publishing on the handover publishes a record the
    writer may still drop, and when the log is the only record of the fact there is
    then nothing left to explain the reversal and nothing for a retry to act on.

    *emit_one* is handed the ``on_settled`` callback and must pass it to exactly one
    emitter call, or invoke it itself when it decides there is nothing to queue --
    otherwise this waits out the whole bound for an answer that is not coming.

    False on a drop AND on a timeout, which are the same thing to the caller: the
    record is not there to publish from. A timeout is not a failure of the append,
    which may still land later, so the caller's own answer should be retryable
    rather than final.

    Must be called from a running loop: the callback arrives on the writer's thread
    and is marshalled back onto this one.
    """
    loop = asyncio.get_running_loop()
    settled: "asyncio.Future[bool]" = loop.create_future()

    def _settle(wrote: bool) -> None:
        def _resolve() -> None:
            # Guarded because the timeout can win the race, and setting a result on
            # a future that already has one raises.
            if not settled.done():
                settled.set_result(wrote)

        loop.call_soon_threadsafe(_resolve)

    emit_one(_settle)
    try:
        return await asyncio.wait_for(settled, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "crew log: %s was not committed within %.1fs; reporting it as not recorded",
            what,
            timeout,
        )
        return False


def on_session_adopted(
    session_id: str,
    *,
    slot: str,
    parent_slot: str,
    parent_sid: str = "",
    previous_parent_slot: str = "",
    previous_parent_sid: str = "",
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that *parent_slot* has TAKEN OVER the session *session_id*.

    Written on the session that moved, which is the side ``session/opened.parent``
    already puts a creating edge on -- so the tree reads one axis from one place, and a
    takeover of a session that has children costs one entry rather than one per
    descendant, because descendants cite this session's slot and not a path through it.

    Nothing is rewritten, and nothing could be: the log is append-only, and the opening
    entry states who OPENED the session, which stays true. This entry states who holds
    it now, and the fold prefers the newest of the two.

    ``previous_parent`` is recorded for a reader of the log and is not folded. It is
    passed as two plain strings rather than a mapping so this signature says exactly
    which values it accepts, and the ``sid`` half is omitted when the caller has none:
    an empty string would read as a parent whose id is blank.

    Returns without waiting, like every other emitter here, and the projection is
    advanced inside the job AFTER the append succeeds -- durability first, then memory --
    so the disk can never hold a decision the memory lacks, and a lost write leaves the
    tree where it was rather than moving it on the strength of an append that did not
    land.

    ``on_settled`` is how a CALLER waits for that outcome, and this entry point has one
    where the others do not because the append IS the operation here: a verb that told
    its caller "adopted" and then lost the write would have reported a takeover that
    never happened. It is called once, off the caller's thread, with ``True`` when the
    entry is on disk and ``False`` when the write was given up on or there was no log to
    write to. Not awaited HERE -- ``_submit`` must never block the loop -- so the waiting
    is the caller's to bound.
    """
    if not session_id or not slot or not parent_slot:
        # No slot is not a tree edge: the tree is keyed by slot, so an entry with
        # neither side of the edge names nothing a reader could fold.
        if on_settled is not None:
            on_settled(False)
        return
    data: dict[str, Any] = {"parent": _parent_citation(parent_slot, parent_sid)}
    previous = _parent_citation(previous_parent_slot, previous_parent_sid)
    if previous:
        data["previous_parent"] = previous

    settle = _tree_settle_hooks(on_settled)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            settle.fail()
            return
        written = log.append("session/adopted", data, src=_SRC_GATEWAY)
        settle.wrote()
        _record_session_tree_decision(session_id, slot, written, parent_slot)

    _submit(
        _job,
        "appending session/adopted",
        session_id,
        after=settle.after,
        on_permanent_drop=settle.fail,
    )


def on_session_released(
    session_id: str,
    *,
    slot: str,
    previous_parent_slot: str = "",
    previous_parent_sid: str = "",
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that the session *session_id* has been LET GO and is a root again.

    The counterpart of :func:`on_session_adopted` and the only entry that takes a
    parent edge away. A ``session/opened`` carrying no parent does not: it means that
    entry did not repeat a creator, which a reader must not read as a retraction, so
    the retraction needs a record of its own.

    ``previous_parent`` is the parent that let it go, recorded for a reader and not
    folded. It is optional because the entry's meaning does not depend on it: what this
    says is that there is no parent NOW.

    ``on_settled`` reports the durable outcome, for the reason it does on
    :func:`on_session_adopted`: the append is the operation, so a caller that must not
    claim a release it did not land waits for this.
    """
    if not session_id or not slot:
        if on_settled is not None:
            on_settled(False)
        return
    data: dict[str, Any] = {}
    previous = _parent_citation(previous_parent_slot, previous_parent_sid)
    if previous:
        data["previous_parent"] = previous

    settle = _tree_settle_hooks(on_settled)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            settle.fail()
            return
        written = log.append("session/released", data, src=_SRC_GATEWAY)
        settle.wrote()
        _record_session_tree_decision(session_id, slot, written, None)

    _submit(
        _job,
        "appending session/released",
        session_id,
        after=settle.after,
        on_permanent_drop=settle.fail,
    )


def _parent_citation(slot: str, sid: str) -> "dict[str, str]":
    """One ``{slot, sid?}`` citation, or ``{}`` when there is no slot to cite.

    ``sid`` is omitted rather than written empty, the same distinction
    :func:`on_session_opened` keeps on its own ``parent``: an empty string would read
    as a session whose id is blank, and "the gateway had no live handle for it" is a
    different fact from that.
    """
    if not slot:
        return {}
    citation: dict[str, str] = {"slot": slot}
    if sid:
        citation["sid"] = sid
    return citation


def _candidate_is_same_slot(candidate_sid: str, slot: str) -> bool:
    """Whether *candidate_sid*'s crew log records *slot* as its own.

    ``previous`` means the crew log the SAME slot was writing, and the reference
    says so, but the id reaches this emitter from the slot-to-session mapping --
    a persisted file whose entry can be stale or recycled by the time a successor
    cold-starts. So the invariant is CHECKED rather than assumed, against the
    candidate's own header, which is written once at create and never rewritten.
    Comparing the mapping against itself would prove nothing; the header is the
    crew log's own statement about which slot it belongs to.

    Answering False on any failure is deliberate, because an edge is worth writing
    only when the two crew logs are KNOWN to be one slot's. A candidate whose
    header cannot be read -- retention removed the crew log, or its header line is
    unreadable -- is not known to be this slot's, and sending a reader down an
    unverified edge lands it somewhere this slot never wrote, which is worse than
    ending the walk one link early. A session with no slot has no slot identity to
    match, so it gets no edge either.

    ``unit_header_slot`` is the accessor rather than ``CrewLog.open`` because this
    path must not write, and ``open`` does: its torn-tail truncation is
    unconditional, deliberately so, since trailing bytes that are not a whole line
    are not a record. Harmless in itself, and still wrong here -- a verification
    read would take the crew log's lock and rewrite a candidate's file while asking
    for nothing but one field. The accessor states the opposite contract, no lease
    and nothing written, and its ``None`` means "cannot prove" rather than "no such
    field", which is the refusal this function already wanted. It is also stricter
    than ``open``: it refuses a linked directory, and a header whose own ``id`` does
    not fold back to the directory holding it, both of which would let one store
    answer for another unit. Its own docstring names this caller's position exactly
    -- a unit id reached through a channel the caller does not fully trust.

    Nothing is caught here because the accessor answers ``None`` for every
    unreadable-crew-log case itself, down to the directory walk and the header
    parse. Anything it still raises is a bug in this module, and it belongs in the
    log rather than swallowed into a permanently silent "not the same slot", which
    would read exactly like a correct refusal while disabling the check for every
    slot at once.
    """
    if not slot or not candidate_sid:
        return False
    from kiro_crew.crew_log.store import unit_header_slot

    return unit_header_slot(_KIND, candidate_sid) == slot


def slot_previous_store(slot: str) -> "tuple[str, bool, bool]":
    """The crew log *slot* is writing NOW, as ``(sid, decided, complete)``.

    Read from the store, so it survives the process that wrote it. Every gateway
    process asking this question of the same slot gets the same answer: the units
    under *slot* and the succession edges they recorded are the whole input, and a
    restart reads them exactly as the process before it would have. The
    slot-to-session mapping cannot answer it -- an allocation whose history replay
    is pending holds the prior resumable id there on purpose, so for that window the
    mapping names a generation older than the store the slot is writing.

    FOUR answers, because a caller must tell three kinds of empty apart. A ``sid``
    names the store. ``decided`` false is "the units could not be read, or do not
    say" -- a unit that would not open, more than one uncited unit -- and falling
    back THERE would hand the edge to the very source this read was preferred over,
    which inside the replay window is a generation behind. The honest outcome is no
    edge: one citation lost transiently, rather than a wrong citation frozen into an
    append-only entry.

    The two DECIDED empties differ by ``complete``, and a caller that flattens them
    writes a false statement. Complete means the store holds no unit of this slot at
    all, so the absence of a predecessor is the whole truth and the caller may state
    it. Incomplete means the store holds units it cannot rank -- units written before
    these keys existed, or several each stating they start the chain -- so the caller
    may consult its next source and may state NOTHING, because an empty answer from
    that source means only that it had nothing to give, not that this slot has no
    earlier store.

    Blocking, and gated: a launch with the crew log off answers ``("", True, True)``,
    which is also what keeps the storage subsystem unimported there -- no unit exists,
    so there is nothing indeterminate about it and the absence is complete. The caller
    hops a thread for this
    (:func:`~kiro_crew.crew_log.session_tree.slot_chain_head` lists the store and
    reads a line pair per unit of the slot).
    """
    if not slot or not enabled():
        return ("", True, True)
    from kiro_crew.crew_log.session_tree import slot_chain_head

    head = slot_chain_head(slot)
    return (head.sid, head.decided, head.complete)


def _durable_previous(session_id: str) -> str:
    """The predecessor id *session_id*'s own opening entry records, or ``""``.

    The recovery read for a crew log this process is re-attaching to. Imported
    locally for the same reason :func:`_candidate_is_same_slot` does it: this module
    is on the gateway boot path and the storage package stays unloaded until a call
    actually reaches it.

    ``""`` rather than ``None`` because the caller latches the answer and an empty
    string is the latched "nothing to recover" every other decision in that job
    uses. The accessor's own ``None`` is "cannot prove" -- a crew log that is gone,
    a header that does not fold back, an opening entry not yet appended -- and all of
    those mean the same thing here: no recovery is available from this file.
    """
    from kiro_crew.crew_log.store import unit_opened_previous

    return unit_opened_previous(_KIND, session_id) or ""


def _queue_tail_repair(previous_sid: str, slot: str) -> None:
    """Queue the tail repair for *previous_sid*. The ONE submission site for it.

    Three things reach this: the supersede that names the predecessor, a re-attach
    recovering a job a crash lost, and a dropped terminal that a stand-down had
    deferred to. They differ only in what brought them here, never in what is
    submitted, so the guards, the bucket and the ceiling exemption cannot drift apart
    between them.

    Queued under the PREDECESSOR's id, which IS the deferral: the writer runs a
    session's jobs in submission order, so this cannot run until everything that crew
    log already owes has been attempted -- written, or dropped and admitted in a
    marker. A ``WriteJob.follow_on``, which is the writer's whole policy for such a job:
    never written inline (the callers that queue it from a job are on the writer
    thread, where the inline path would wait for the pass it is inside), never refused
    at the memory ceiling (the opening entry that queues it is exempt, so under pressure
    the successor's crew log would be created while its follow-on repair is refused),
    and submitted ONCE more if its whole retained batch is dropped, behind the loss
    marker, without being counted as lost.
    """
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        previous_sid,
        WriteJob.follow_on(
            lambda: _repair_superseded(previous_sid, slot),
            "closing a superseded crew log's interrupted tail",
        ),
    )


def _terminal_dropped(session_id: str) -> "Callable[[], None]":
    """The hook a terminal carries so its DROP re-queues a repair that stood down.

    Paired with :func:`_closing` at the same three handover sites, and separate from it
    because the two answer different questions. ``after`` runs when the append is
    RESOLVED, written or given up on alike, so it cannot tell those apart; a
    permanent-drop hook fires only on the giving up, which is the one outcome that
    makes a stand-down wrong in hindsight.

    A repair that stood down for a running turn is correct exactly while that turn's
    terminal is still coming. Once the terminal is gone the turn has no outcome coming
    at all, and leaving the stand-down in place keeps a ``turn/started`` open for the
    life of a file nothing rewrites.

    The returned callable does nothing unless this session actually owes a repair, so
    the ordinary terminal -- no supersede, nothing waiting -- pays one dict read.
    """

    def _dropped() -> None:
        tracker = _tracker_instance
        slot = tracker.take_repair_debt(session_id) if tracker is not None else ""
        if not slot:
            return
        logger.warning(
            "session log %s: the terminal of superseded crew log %s was DROPPED, so "
            "re-queueing the tail repair that stood down for it -- the turn it "
            "deferred to has no outcome coming now, and its tail would otherwise stay "
            "open for the life of the file",
            slot,
            session_id,
        )
        _queue_tail_repair(session_id, slot)

    return _dropped


def _repair_superseded(previous_sid: str, slot: str) -> None:
    """Close *previous_sid*'s interrupted tail. WRITER THREAD, never a caller's.

    The repair half of a supersede. The successor's ``session/opened`` only NAMES
    the crew log the slot was writing before; this closes that crew log's own
    dangling ``turn/started`` as ``turn/completed {stop_reason: "interrupted"}``
    and its open ``tool/called`` frames as ``status: "unknown"``, which is what
    makes a fold able to tell an interrupted turn from one still running. Without
    it every gateway restart leaves one permanently open tail per active slot, and
    that turn's cost, duration and outcome are absent from every reading of the
    file for the rest of its life.

    **The deferral is the queue, not a decision.** This job is submitted into
    ``previous_sid``'s OWN bucket, and the writer runs a session's jobs in
    submission order -- so it cannot run until everything already owed for that
    crew log has been attempted. That is the whole ordering requirement, and
    taking it from the buffer rather than from a fresh predicate is what makes it
    resumable by construction: a real ``turn/completed`` still queued or retrying
    at supersede time is written BEFORE this runs, and one abandoned after its
    attempt budget is spent is dropped and admitted in a ``write/dropped`` marker
    before this runs. Either way the tail is closed exactly once and the file
    never carries two outcomes for one turn. Asking at create time instead --
    whether the predecessor still owes anything, standing down while it does --
    leaves nothing that can re-run it: a superseded id is never resumed and
    nothing maps to it once the successor takes over, so precisely the crew log
    that most needs repairing is the one left open for good.

    **The candidate is re-verified adjacent to the write.** The edge already
    refused to NAME a crew log whose own header does not name this slot, so a
    forged mapping entry cannot reach this function through
    :func:`on_session_opened`. What CAN change between that decision and this one
    is the crew log itself: the deferral window lasts as long as the predecessor's
    outstanding writes do, and retention can collect the unit inside it. Asking
    again covers both -- the accessor's ``None`` means "cannot prove", which a
    collected crew log and a foreign one answer alike -- so a unit that is gone is
    a quiet stand-down rather than a ``no_ledger`` refusal the writer would drop
    and count as a lost append, reporting a hole in a file that is gone.
    Asking again is also what keeps the guarantee local: this is the one place an
    outcome is authored into a unit that is not this session's own, and a check
    whose failure costs a foreign outcome belongs next to the write rather than
    inherited from a caller two decisions away. The same
    :func:`_candidate_is_same_slot` the edge uses answers it, against the
    candidate's own immutable header -- one spelling of the rule, so the two
    cannot drift.

    A live turn of ours for that id stands the repair down, exactly as it does on
    the resume path and for the same reason: the turn is still producing entries,
    so closing it would record an outcome it never had and then be followed by the
    rest of it. Standing down is not a hole here, because such a turn ends in its
    own real ``turn/completed``, which closes the tail truthfully.

    Its live record is read as it stands, never released first. ``closer_owed`` is
    set where a terminal is HANDED OVER, so a turn still running is indistinguishable
    from a leaked record by that field alone, and releasing on it would drop the
    record of a turn a forced reset tore down mid-flight -- the one case whose
    closer arrives later, from its own ``finally``. ``on_session_closed`` preserves
    those records for exactly this reason; a release here would undo that and let
    this job write an outcome ahead of a real one. A predecessor whose turn was
    still running at supersede time and whose real closer is later DROPPED is
    covered: the stand-down records the debt with the turn tracker and the terminal's
    own permanent-drop hook (:func:`_terminal_dropped`) re-queues this job once its
    outcome is given up on, so the tail is closed rather than left open. The debt is
    cleared only once the session has no live turn left, so a nested turn landing
    first cannot pay it out from under a sibling still running.

    No ``child_gone`` predicate is passed, so an unmatched ``subagent/spawned`` is
    left OPEN. A superseded crew log's children were dispatched by a session that
    is gone, and this process cannot answer for another process's children: a child
    still running can file its own real terminal, and a synthesised ``unknown``
    ahead of it would leave two outcomes for one ``agent_id`` in a file nothing
    rewrites. Leaving the opener unmatched leaves a reader one fact short; closing
    it can leave a reader wrong.
    """
    # Live records are LEFT ALONE, which is the same rule `on_session_closed`
    # applies to them and for the same reason: a forced reset tears a session down
    # MID-TURN, that turn goes on running, and its closer is handed over later by
    # its own ``finally``. Releasing such a record here would take back the one
    # signal that says so -- ``closer_owed`` is set at handover, so a turn still
    # running reads exactly like a leaked record -- and ``live_turn`` would then
    # report 0 and let this job write ``interrupted`` ahead of a real
    # ``turn/completed`` that is still coming. That is the two-outcomes-for-one-turn
    # hazard, in a file nothing rewrites.
    # Whether a live turn stands the repair down, and the debt recorded when one
    # does, are read and written in ONE critical section of the tracker's
    # (`TurnTracker.stand_down`), for the window its docstring names. Standing down
    # is right only while that terminal is still COMING, and it may instead spend its
    # attempt budget and be dropped. The debt is recorded against the predecessor so
    # the drop can re-queue this job: the terminal's drop hook is the one site that
    # learns the deferral ended badly, and without the debt it has no way to know a
    # repair was waiting.
    running = _tracker().stand_down(previous_sid, slot)
    if running:
        logger.warning(
            "session log %s: NOT closing the interrupted tail of superseded crew "
            "log %s -- turn %d is still running for it in this process, so closing "
            "it would record an outcome that turn never had and then be followed "
            "by the rest of it",
            slot,
            previous_sid,
            running,
        )
        return
    if not _candidate_is_same_slot(previous_sid, slot):
        # Also the answer for a unit RETENTION COLLECTED while this job waited, and
        # deliberately the same branch: the accessor reports None for a crew log
        # that is gone as readily as for one whose header names another slot, and
        # both mean "not known to be this slot's predecessor". A separate existence
        # check ahead of it can change no outcome, because this branch already
        # returns before the open that would raise ``no_ledger``.
        logger.warning(
            "session log %s: NOT closing the interrupted tail of %s -- its own "
            "header does not name this slot, or the crew log is gone, so it is not "
            "known to be this slot's predecessor",
            slot,
            previous_sid,
        )
        return
    # Opened WITHOUT ``repair`` and repaired through the method, for the count:
    # ``open(repair=True)`` does the same work and returns a handle rather than how
    # many closers landed, and a repair that closed nothing is worth telling apart
    # from one that closed a turn and three calls.
    log = _crew_log().CrewLog.open(_KIND, previous_sid)
    closed = log.repair_interrupted_turn()
    if closed:
        logger.info(
            "session log %s: closed %d dangling entr%s on superseded crew log %s",
            slot,
            closed,
            "y" if closed == 1 else "ies",
            previous_sid,
        )
    # The handle goes out of scope here, which is what releases the write ownership
    # ``repair_interrupted_turn`` took: the lease is bound to the object's lifetime,
    # so holding this handle any longer would keep a crew log nothing is writing
    # owned by this process.


def on_session_opened(
    session_id: str,
    *,
    agent: str = "",
    slot: str = "",
    model: str = "",
    model_requested: str = "",
    cwd: str = "",
    owner: str = "default",
    resumed: bool = False,
    parent_slot: str = "",
    parent_sid: str = "",
    memory: str = "",
    app: str = "",
    channel: bool = False,
    workspace: str = "",
    previous_sid: str = "",
    previous_undecided: bool | None = None,
) -> None:
    """Create the crew log if this session has none, then echo its header.

    Called once per turn, because the per-turn session claim is where the ACP
    id becomes known -- but an entry is written only when there is something new
    to say: the crew log was just created, or this claim RE-ATTACHED to an existing
    conversation (a new gateway process taking over the same session id). A warm
    reuse of a session already carrying a crew log adds nothing, so it is silent.

    ``owner`` and ``agent`` are header fields, written once at create time and
    never rewritten. A resumed session id reuses its existing crew log and
    appends, so an agent or model switch that kept the conversation continues
    one log rather than starting a second one. ``model`` is not a header field
    in the storage schema, so it is carried on this entry instead.

    ``model`` is the id the backend CONFIRMED, and it is empty whenever that id
    is not known. It alone cannot say what the gateway chose: no tier resolved
    anything above the backend's own default, a chosen model was applied, or a
    chosen one never took effect and the backend's choice serves instead (a model
    this account cannot run is withheld before it is sent, and a ``set_model``
    that raises is logged and left alone). Each of the last two can end with an id
    here or without one.

    So ``model_requested`` records what the gateway SELECTED for the ALLOCATION
    that produced this session, and its presence is not conditioned on ``model``.
    The caller resolves that value once, hands it to the provider and retains it
    with the slot, because the turn that observes a session is not always the one
    that allocated it: an eager allocation can outlive a config change, and
    re-resolving at the first turn would record a model that session never used.
    Selection is not transmission either: the withhold happens inside the
    provider, so this field names the choice rather than a message the backend
    received. The pair is the record -- ``model`` states what serves the session,
    ``model_requested`` what was chosen -- and the entry infers nothing from the
    two. A difference between them is not by itself a refusal, because the backend
    serves the spelling it resolved; whether a choice was APPLIED is not something
    this entry knows, and a reader that needs it reads the provider's own outcome
    rather than comparing these strings. Absent ``model_requested`` means no tier
    resolved one, OR that this process did not observe the allocation (a re-attach
    carries provenance from a process that is gone) -- and on an entry written
    before the field existed it means nothing at all.

    ``parent_slot`` names the session that made this one through
    ``session_create`` (the slot's ``_created_by``), and ``parent_sid`` the
    creator's ACP session id as ``session_create`` froze it at mint (the slot's
    ``_created_by_sid``) -- the creator crew log that holds the call. The edge is
    written on the CHILD because that is the side that knows it: the creator is
    stamped on the slot at mint, before any turn, while the creator never learns
    the child's session id, which is assigned at the child's first turn. The sid
    is NOT read live here: a creator slot can be closed and replaced between the
    mint and the child's first turn, and a live read would cite the replacement's
    crew log in an entry that can never be corrected. Both empty means nobody
    created this session (a person's own tab, a fork) and no ``parent`` is
    written at all, so a fold can tell "no creator" from "creator unknown".

    ``memory``, ``app`` and ``channel`` record WHAT KIND of session this log
    belongs to: the slot's memory mode verbatim, the app that owns it if one does,
    and whether its conversation is published to a messaging channel by a link or
    a mirror. They are written as one ``class`` object and only when ``memory`` is
    given, which makes that member the witness that the class was recorded -- so a
    reader can tell a session with nothing to declare from a log written before
    this existed, and must refuse rather than assume on the second.

    They belong on the record rather than in a live lookup because the question
    they answer -- may another session read this log -- is asked about sessions
    that have CLOSED, and a closed session has no slot left to ask. They are also
    facts and not a verdict: recording "readable" would freeze this build's reading
    of a rule into an entry that can never be corrected. The facts are true when
    the log is opened; a class a session ACQUIRES later (a channel link added
    mid-conversation) is not in them, so a reader that can also see the live
    session applies both and refuses on either.

    ``previous_sid`` names the crew log this SLOT was writing before, and it
    answers the continuity ``resumed`` cannot. ``resumed`` is true only when this
    claim re-attached to the same crew log; when the ACP session was instead torn
    down and a successor cold-started, the successor has a different id and
    therefore a different unit, and nothing in the record joined the two. So a
    ``create`` that is handed a DIFFERENT prior id writes ``previous {sid}``, and
    the comparison is made here rather than trusted from the caller: on the resume
    path the prior id and this one are the same store, and an edge pointing at
    itself would make a chain walker loop. Empty, or equal to this session, means
    no edge is written -- the slot's first crew log, and a predecessor the gateway
    could not name, are both "nothing to follow" rather than a store with an empty
    name.

    The edge itself is a citation and nothing more: this entry records which store
    came before, and no writer here touches that store. Closing the superseded
    store's own dangling turn and tool calls is a SEPARATE job, queued under that
    store's id so it runs behind whatever that store still owes -- see
    :func:`_repair_superseded`. Keeping the two apart is what lets the entry land
    at once while the repair waits for an ordering it cannot have yet.
    """
    if not session_id or not enabled():
        return
    # Read BEFORE the release below, which is what makes this the only place the
    # evidence still exists: a claim ends every turn this process believes is
    # running, so by the time the write job runs the record is already gone.
    live_at_claim = live_turn(session_id)
    # A claim ends every turn this process has no closer coming for. A turn whose
    # terminal event is already queued keeps its record, because the write job
    # owes the release and the file still shows that turn open until the entry
    # lands.
    _tracker().release_unclosed(session_id)
    # Latched on the FIRST attempt and read back on every later one. The decisions
    # below are derived from filesystem state this job itself changes: a retry after
    # the header landed but the entry did not finds ``exists`` true and ``created``
    # false, so an unlatched decision would flip to "nothing new to say" and skip
    # the entry it still owes -- permanently, and without counting the loss. It
    # holds the supersede edge too, which is a session id rather than a flag, so
    # the values are not all bools.
    announce: "dict[str, Any]" = {}

    def _job() -> None:
        created = False
        if _crew_log().CrewLog.exists(_KIND, session_id):
            # THE resume path, and the only caller that may repair. ``resumed``
            # means this claim re-attached to a conversation a DIFFERENT gateway
            # process was writing, so a turn left open in that file belongs to a
            # writer that is gone and closing it records what happened. A warm
            # reuse inside this process passes ``resumed=False`` and must not
            # repair: its turn may still be running. A reconnect after a cache
            # eviction never reaches here at all -- it goes through ``_handle``,
            # which opens without repair.
            #
            # A live turn of OUR OWN contradicts the claim. ``resumed`` is the
            # caller's belief that the writer is gone, and this process holding a
            # running turn for that id is direct evidence that it is not -- it is
            # us. Repairing then closes a turn that is still producing entries, and
            # the file ends up saying the turn completed as interrupted and then
            # completed again for real, with the same tool closed both unknown and
            # completed: a fold reads two outcomes for one turn and cannot tell
            # which happened. Local evidence beats the flag, so this degrades to a
            # reconnect. A writer in ANOTHER process is caught one layer down
            # instead of here: the repair is an append, so it takes that unit's
            # write ownership and is refused while another process holds it, and
            # this check is what covers the same-process race a cache eviction
            # produces -- which no kernel lock can see, both handles being ours.
            may_repair = bool(resumed) and not live_at_claim
            if resumed and live_at_claim:
                logger.warning(
                    "session log %s: opened as a resume while turn %d is still "
                    "running in this process, so the open turn is NOT repaired -- "
                    "closing it would record an outcome the turn never had and then "
                    "be followed by the rest of that turn",
                    session_id,
                    live_at_claim,
                )
            # A repair may close an unmatched `subagent/spawned` only for a child
            # with no outcome still coming, and `resumed` cannot answer that: it is
            # raised for an IN-PROCESS `session/load` too, so it does not even imply
            # a different process wrote this file, let alone that the children that
            # process spawned have stopped. The registry of running children is the
            # only thing that knows, and a process with none registered closes no
            # child at all.
            log = _crew_log().CrewLog.open(
                _KIND, session_id, repair=may_repair, child_gone=_child_gone_probe(session_id)
            )
            # Rebuild the attempt counts from what is already in the file, for
            # the two cases where memory cannot answer: a resume, whose counts
            # belong to a process that is gone, and a session whose map was
            # evicted by the bound. A warm reuse still HOLDS its counts, and
            # re-seeding it would rescan the whole file on the one thread that
            # serializes every session's appends -- once per open, against a file
            # that only grows.
            needs_seed = bool(resumed) or not _tracker().knows_attempts(session_id)
            if needs_seed:
                _seed_attempts(session_id, log)
            if slot:
                # CRASH RECOVERY. The edge is DURABLE and the repair job is not: the
                # opening entry carrying ``previous.sid`` is an append, while the
                # repair rides the in-memory buffer, so a crash between the two loses
                # the repair and a superseded id is never resumed to re-queue it. A
                # re-attach is the one moment a later process holds this crew log
                # again and can read its OWN edge back, so the repair is recovered
                # from the file rather than from state that did not survive.
                #
                # This log's own entry, never the caller's ``previous_sid``. The latch
                # below refuses the caller's value on a re-attach for a stated reason
                # -- the unit it names may be this same one or an unrelated one that
                # is still LIVE -- and that reason does not apply here: this value was
                # written by an earlier attempt of THIS session's opening entry, which
                # verified it against the slot before writing it.
                #
                # Gated on ``slot`` because the repair verifies the candidate against
                # that slot and refuses without one, and nothing is lost by the gate:
                # an edge is only ever written for two crew logs KNOWN to be one
                # slot's, so a session with no slot has no durable edge to recover.
                #
                # Latched like every other decision in this job, so a retry acts on
                # the first attempt's reading rather than re-deriving from a file the
                # attempt before it may have changed.
                recovered = announce.setdefault(
                    "recovered_previous",
                    _durable_previous(session_id),
                )
                if recovered:
                    logger.info(
                        "session log %s: recovered the superseded crew log %s from "
                        "this log's own session/opened entry, so its interrupted "
                        "tail is repaired even though the queued repair did not "
                        "survive",
                        session_id,
                        recovered,
                    )
        else:
            log = _crew_log().CrewLog.create(
                _KIND,
                session_id,
                owner=owner or "default",
                agent=agent or _DEFAULT_AGENT,
                slot=slot or None,
                cwd=cwd or None,
            )
            created = True
        _remember(session_id, log)
        # The class as observed for THIS turn. The caller reads it off the live slot
        # on every turn rather than only the first, which is what lets a class the
        # session acquires LATER reach the log at all.
        observed = (memory, app, bool(channel), workspace) if memory else None
        # Latched on the FIRST attempt and read back on every later one, exactly like
        # ``owed`` below and for the same reason: the decision is derived from
        # filesystem state this job itself changes, so a retry after the header
        # landed reads ``exists`` true and ``created`` false, and an unlatched edge
        # would be dropped there -- silently, and permanently, while the entry it
        # belongs to still gets written. The latch holds the ID rather than a flag,
        # so the retry writes the edge the first attempt decided on, and an empty
        # string is a latched "no edge" that nothing downstream re-tests.
        #
        # ``created`` is part of the condition, not just the ``previous_sid``
        # comparison. A re-attach has a store already, so the unit the caller names
        # is either this same one or an unrelated one that may still be LIVE, and
        # repairing that is how a running turn gets an outcome it never had.
        superseded = announce.setdefault(
            "superseded",
            (
                previous_sid
                if (
                    created
                    and previous_sid
                    and previous_sid != session_id
                    and _candidate_is_same_slot(previous_sid, slot)
                )
                else ""
            ),
        )
        # Buffered beside the id above and gated the same way, because it answers the
        # same question: what this entry says about the slot's earlier store. It is
        # only meaningful when NOTHING was named -- a named edge already says the
        # predecessor is known -- so a caller passing both leaves the id winning.
        #
        # THREE values, not two, and the third is the one that keeps this honest. A
        # caller that looked reports what it found; a caller that never looked passes
        # nothing, and this entry then says nothing either way. Collapsing the last
        # two would make the emitter state a conclusion on behalf of a caller that
        # never reached one, which is the same defect as reading an absent key as a
        # conclusion, written from the other side.
        determined = announce.setdefault(
            "previous_determined", bool(created and previous_undecided is not None)
        )
        unresolved = announce.setdefault(
            "previous_unresolved",
            bool(created and previous_undecided is True and not superseded),
        )
        if not announce.setdefault("owed", created or bool(resumed)):
            # Nothing new to say about the OPENING, which is what this entry
            # records. A class that has moved since the last statement of it is
            # something new to say about the session, and it goes out as its own
            # entry rather than as a second opening entry: the opener is read as
            # what the session was CREATED as, and a fold over the transitions
            # after it is what gives a reader the whole life.
            _note_class_change(session_id, log, observed)
            return
        data: dict[str, Any] = {
            "agent": agent or _DEFAULT_AGENT,
            "slot": slot,
            "model": model,
            "cwd": cwd,
            "owner": owner or "default",
            "resumed": bool(resumed),
        }
        if model_requested:
            # Written whenever the gateway resolved one, and never conditioned on
            # ``model``. Both guards tried before this inferred the application
            # outcome from the two ids and both lost the record: suppressing on a
            # DIFFERENCE reported an honoured request as unconfirmed, because the
            # backend serves the spelling it resolved; suppressing on a KNOWN
            # ``model`` dropped the request whenever a refused pin left the session
            # on a concrete backend default rather than the auto sentinel, which is
            # ordinary operation. Recording the request outright costs one short
            # string and cannot lose the requested/served pair. The entry states two
            # facts and infers nothing: what the gateway asked for, and what serves.
            data["model_requested"] = model_requested
        if superseded:
            # No ``slot`` inside: it is the slot in ``data.slot``, and repeating it
            # would invite a reader to trust a second copy of one fact.
            data["previous"] = {"sid": superseded}
        elif unresolved:
            # A predecessor EXISTS and could not be named. Recorded BESIDE the
            # citation rather than as an empty one, because ``previous.sid`` is
            # required and a citation naming nothing would be a weaker promise for
            # every reader of it. This is a third thing from the two a reader already
            # tells apart: a named edge, and neither key, which means this log starts
            # the slot's chain. Without it this log would read as that chain start,
            # and a fold ranking the slot's logs would pass over it and elect the log
            # before it -- the citation this read refused to guess, written anyway by
            # another route and frozen into an append-only entry.
            data["previous_undecided"] = True
        elif created and determined and not previous_sid:
            # The caller LOOKED and there is no predecessor: this is the slot's first
            # store. Stated rather than left to the absence of the other two keys,
            # because a store written before any of these keys existed also has none of
            # them -- and ITS omission may equally be a predecessor the gateway of the
            # day failed to name. Only a store that says this may be passed over when a
            # later reader ranks the slot's stores. A named predecessor that was
            # REJECTED for belonging to another slot says nothing either way, so it
            # falls through to writing no key at all.
            #
            # ``determined`` is what makes the claim answerable for, and it is not a
            # formality: a caller that hands over an id it read from one source and
            # never established whether a predecessor exists would otherwise have this
            # entry declare, in an append-only record, that the slot has none. An empty
            # id from such a caller means "I have nothing to give you", which is the
            # unexplained silence this key exists to be distinguished FROM.
            data["previous_none"] = True
        if parent_slot:
            # Written only when there IS a creator, and ``sid`` only when the
            # creator still had a live handle: an empty string in either place
            # would read as a creator with an empty name.
            parent: dict[str, str] = {"slot": parent_slot}
            if parent_sid:
                parent["sid"] = parent_sid
            data["parent"] = parent
        if memory:
            # The class of session this log belongs to, as facts. Written only when
            # the caller supplied ``memory``, which every live slot has: that makes
            # one member the witness that the class was recorded at all, so a reader
            # can tell "recorded, and nothing applies" from "not recorded", and an
            # object that is never empty carries that distinction without a flag
            # saying so. A caller that passes no memory mode -- a test fixture, an
            # older build's log -- records no class, and a reader that needs one
            # must refuse rather than read the absence as "nothing applies".
            #
            # These live HERE, on the opening entry, because the crew log is the
            # authoritative record of a session and the question they answer is
            # asked about sessions that have CLOSED. A gate that read them from the
            # live slot instead can answer only for a session still being served,
            # which is the one case it does not need.
            #
            # Facts, not a verdict: what owns the session, whether it keeps memory,
            # whether its conversation is published to a channel. A verdict recorded
            # here would be this build's reading of a rule that may change, and the
            # entry cannot be rewritten.
            session_class: dict[str, Any] = {"memory": memory}
            if app:
                session_class["app"] = app
            if channel:
                session_class["channel"] = True
            if workspace:
                session_class["workspace"] = workspace
            data["class"] = session_class
        log.append("session/opened", data, src=_SRC_GATEWAY)
        # DURABILITY FIRST, THEN MEMORY. The session tree is a projection folded in
        # memory and advanced here, at commit, so no reader ever has to re-derive it
        # from disk; this line is the only thing that keeps it current. It runs AFTER
        # the append, never before, so the disk can never hold an edge the memory
        # lacks -- and if it did run first, an append that then failed would leave a
        # creator edge that no log records.
        #
        # This gateway is the store's only writer (a pod or the internal gateway has
        # its own data home), which is what makes an in-process projection complete
        # rather than a guess about somebody else's writes.
        #
        # Never raises: ``record_opened`` swallows its own failures, because an append
        # that already succeeded must not be reported as failed on account of the
        # memory image of it, and a projection that missed a record self-heals through
        # the tail replay on the next cold start.
        _record_session_tree_edge(session_id, slot, log, parent_slot, superseded)
        if observed is not None:
            # This line states the class, so it is also what later turns compare
            # themselves against. Seeding it here is what stops the first warm turn
            # from restating an unchanged class as though it had moved.
            _latch_class(session_id, observed)
        # The caller's edge when this open wrote one, else the durable edge recovered
        # from this log's own opening entry on a re-attach. ONE submission site for
        # both, so the guards, the bucket and the ceiling exemption cannot differ
        # between a first pass and a recovery.
        repair_target = superseded or str(announce.get("recovered_previous") or "")
        if repair_target:
            # AFTER the append, so the repair is queued exactly once: a failed
            # ``session/opened`` is retained and this job runs again, and queueing
            # above would queue one repair per attempt.
            _queue_tail_repair(repair_target, slot)

    def _flag_creation_failed() -> None:
        # The creating record died with no crew log file behind it: no later append
        # for this session can land, so _handle stops treating its absence as a
        # policy no-op and later discards are COUNTED instead of silently dropped.
        with _lock:
            _creation_failed.add(session_id)

    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        session_id,
        WriteJob.opening(_job, "opening a crew log", on_drop=_flag_creation_failed),
    )


def on_turn_started(
    session_id: str,
    turn: int,
    actor: str = "user",
    *,
    depth: int = 0,
    message_seq: int = 0,
    attempt: int = 0,
) -> None:
    """Anchor a turn's thread. ``turn`` is the message-boundary ordinal.

    ``attempt`` is normally DERIVED, not passed: 0 means "work it out", which is
    what every call site uses, because a rerun reaches this through several paths
    and a field only the audited ones fill is a field a reader cannot trust. A
    positive value overrides the count, for a caller that genuinely knows better.
    """
    # Open the turn's live record BEFORE the write is queued: from here until its
    # terminal event the handle and the step counter are live state, and losing
    # either costs a reopen mid-turn or a repeated step ordinal.
    #
    # Behind the flag, like every other allocation here: with the emitter off this
    # module holds nothing, so a turn costs one env read. The RELEASE paths are
    # deliberately not guarded -- a flag turned off mid-turn must still free what
    # it allocated while on.
    if session_id and enabled():
        _tracker().begin(session_id, turn)
    data: dict[str, Any] = {
        "turn": int(turn),
        "actor": actor if actor in ACTORS else "other",
        "depth": int(depth),
    }
    # The seq of the message entry that caused this turn, when the caller knows
    # it. Omitted rather than zeroed when it does not: 0 is not a seq, and a
    # reader must be able to tell "no pointer" from "points at line 0".
    if message_seq > 0:
        data["message_seq"] = int(message_seq)
    if not session_id or not enabled():
        return

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        # Derived HERE, on the writer thread, NOT on the caller's. Resume seeding
        # runs as an earlier job for this same session, and the writer executes a
        # session's jobs in submission order -- so deriving here is ordered behind
        # the seed by construction. Deriving on the caller's thread instead reads
        # the map while that seed is still queued, and the first rerun after a
        # resume then writes an attempt the file already holds. The alternative,
        # making the caller wait for a seeded event, puts a filesystem read in
        # front of the turn on the event loop.
        n = attempt if attempt > 0 else _tracker().next_attempt(session_id, turn)
        payload = dict(data)
        # Which try this is at the same turn ordinal. Omitted at 1, which is every
        # turn that was never rerun -- the common case should not pay a field for
        # the rare one.
        if n > 1:
            payload["attempt"] = int(n)
        log.append("turn/started", payload, src=_SRC_GATEWAY)

    _submit(_job, "appending turn/started", session_id)


def on_turn_refused(
    session_id: str,
    turn: int,
    reason: str,
    actor: str = "user",
    *,
    depth: int = 0,
) -> None:
    """Record a turn that was dispatched but never authorized to run.

    Its own fact rather than a ``turn/started`` with no completion. A started
    entry means the turn RAN, so writing one for a refusal would make a turn that
    never reached the model indistinguishable from one that died mid-flight, and
    the interrupted-turn repair would then close it as though it had. ``reason`` is
    the gateway's own word for which gate refused it.
    """
    _write(
        session_id,
        "turn/refused",
        {
            "turn": int(turn),
            "actor": actor if actor in ACTORS else "other",
            "reason": reason,
            "depth": int(depth),
        },
        src=_SRC_GATEWAY,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def on_turn_completed(
    session_id: str,
    turn: int,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    credits: float = 0.0,
    duration_ms: int = 0,
    stop_reason: str = "",
    model: str = "",
    provider: str = "",
    depth: int = 0,
    context_used: int = 0,
    context_window: int = 0,
) -> None:
    """Record a turn's terminal event and what it cost.

    ``context_used`` / ``context_window`` are the PROVIDER's own occupancy reading
    for this turn, and they are a different quantity from ``tokens`` beside them.
    ``tokens`` is what was BILLED: it is summed over every model call the turn made,
    so it answers what the turn cost. Occupancy answers how full the window was, and
    a reader wanting "how close to full did this session get" needs the second --
    dividing a billed total by a window size is not that number, and on a
    tool-using turn it is larger than the window it is divided by.

    The pair travels TOGETHER on one entry for the same reason: a used count from one
    turn over a window size from another describes no turn at all, and a model switch
    moves the window. Both are absent when the provider reports neither, so an
    unmeasured turn reads as unmeasured rather than as an empty window.

    ``tokens`` and ``credits`` follow the same rule, one field at a time: each is
    written only when the provider actually reported it. ``TurnUsage`` zero-fills
    every dimension a provider does not report, so at this seam a zero is "nothing
    was reported", not a measurement of nothing -- and a zero written as a
    measurement is what the ``usage`` fold would count as a reporting turn, putting
    a measured ``0 tokens`` beside a real bill. A present ``tokens`` keeps all FOUR
    dimensions, zeros included, because its schema requires each member and a zero
    INSIDE a reported block is a real zero; ``background/completed`` drops the zero
    members instead. The two writers agree on what an unreported count looks like
    -- absent -- and differ only in the shape of a reported one.
    """
    data = _turn_closer(
        turn,
        duration_ms=duration_ms,
        stop_reason=stop_reason,
        model=model,
        provider=provider,
        depth=depth,
    )
    # Positive and finite, or absent: the guard the two subagent closers use. A
    # provider that does not bill in credits reports 0.0 through ``TurnUsage``,
    # which is indistinguishable here from a free turn, so the zero is dropped and
    # absent keeps meaning unmetered.
    charge = float(credits)
    if charge > 0 and math.isfinite(charge):
        data["credits"] = charge
    tokens = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    if any(count > 0 for count in tokens.values()):
        data["tokens"] = tokens
    # Written only when the provider actually reported them. ``read_context_tokens``
    # answers (0, 0) for a provider without the accessors, and a stored zero would
    # be indistinguishable from a window of nothing.
    if int(context_used) > 0 or int(context_window) > 0:
        data["context"] = {"used": int(context_used), "window": int(context_window)}
    _write(
        session_id,
        "turn/completed",
        data,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def on_turn_failed(
    session_id: str,
    turn: int,
    *,
    error: str = "",
    duration_ms: int = 0,
    stop_reason: str = "failed",
    model: str = "",
    provider: str = "",
    depth: int = 0,
) -> None:
    """Close a turn that ended WITHOUT its terminal event, observed in process.

    A stream that raises, or a recovery path that returns before the terminal
    event, ends the turn while the writer is still alive and watching. Leaving the
    ``turn/started`` open would make that indistinguishable from a turn whose
    writer was killed mid-flight -- and nothing in this process would ever close
    it, since the interrupted-turn repair is opt-in and only a RESUME asks for it.
    A later resume would then close it as an interruption that never happened,
    stamped at the last real entry's time. So the observation is recorded where it
    is made.

    ``tokens`` and ``credits`` are ABSENT rather than zeroed, and that absence is
    the record: no usage event arrived, so nothing was measured, and a turn that
    streamed real text does not get a durable line claiming it cost nothing. The
    measured closer omits them too when its provider reported nothing, so absence
    does not tell this closer from a provider-reported one; ``stop_reason`` does.
    ``duration_ms`` IS measured -- the turn's own elapsed time -- and ``error``
    names the exception CLASS when one was caught, never its message, which can
    carry a path or a credential.
    """
    data = _turn_closer(
        turn,
        duration_ms=duration_ms,
        stop_reason=stop_reason or "failed",
        model=model,
        provider=provider,
        depth=depth,
    )
    if error:
        data["error"] = error
    _write(
        session_id,
        "turn/completed",
        data,
        src=_SRC_GATEWAY,
        after=_closing(session_id, turn),
        on_permanent_drop=_terminal_dropped(session_id),
    )


def _turn_closer(
    turn: int,
    *,
    duration_ms: int,
    stop_reason: str,
    model: str,
    provider: str,
    depth: int,
) -> "dict[str, Any]":
    """The fields every ``turn/completed`` carries, whichever path closes the turn.

    Shared so the measured closer and the failed one cannot drift into two
    different shapes for one type -- the difference between them is which fields
    they ADD, never which of these they leave out.
    """
    return {
        "turn": int(turn),
        "depth": int(depth),
        "stop_reason": stop_reason,
        "duration_ms": int(duration_ms),
        "model": model,
        "provider": provider,
    }


#: How a message body is represented in ``data``. ``text`` puts the redacted body
#: IN the log, which is the decided behaviour: FR-7 was amended in this change to
#: include bodies, because they are redacted before they are written. ``ref`` names
#: the migration shape -- a pointer at a transcript position -- which the dual-write
#: bridge adds alongside the body, NOT instead of it. The mode stays a single name
#: read in one place (:func:`_append_body_entry`) so the bridge is a change there
#: and nowhere else.
BODY_MODE_TEXT = "text"
BODY_MODE_REF = "ref"
BODY_MODE = BODY_MODE_TEXT


def _entry_line_fits(entry_type: str, data: dict[str, Any], *, src: str) -> bool:
    """Whether *data* fits its complete serialized crew log entry.

    Measured through the STORE's own serializer rather than a local ``json.dumps``
    with the same options: this decides whether an entry is written whole or split,
    and the writer refuses the line by its serialized length, so a second spelling
    of the encoding is a second answer waiting to disagree with the first. Only the
    two counters are guessed, at their widest, because the writer assigns them under
    its lock after this decision is made -- a fit that depends on a small seq would
    stop fitting on a long-lived log.
    """
    counter_ceiling = (2**63) - 1
    envelope = {
        "type": entry_type,
        "seq": counter_ceiling,
        "time": counter_ceiling,
        "src": src,
        "data": data,
    }
    try:
        # The schema module by its own import, and deliberately NOT at module
        # scope: the package front exports names, not submodules, so reaching it
        # as an attribute worked only after some earlier import had loaded it --
        # and a module-scope import here would load the schema on every flag-off
        # launch, which
        # ``test_crew_log_emit.py::test_a_flag_off_launch_does_not_load_the_storage_subsystem``
        # pins against ("the store, schema and lease stay unloaded until a call
        # reaches storage"). The ``top-level-imports`` convention is advisory;
        # that boot-path invariant is enforced, so the invariant wins.
        from kiro_crew.crew_log import schema as crew_log_schema

        line = crew_log_schema.serialize(envelope)
    except _crew_log().CrewLogError:
        # Not serializable at all. The append will refuse it for the same reason,
        # with the code that names it, so this reports "does not fit" rather than
        # deciding the outcome here.
        return False
    return len(line.encode("utf-8")) <= _crew_log().MAX_ENTRY_BYTES


def _bounded_attachment_data(
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str,
    inline_text: str | None = None,
) -> dict[str, Any]:
    """Keep the longest attachment prefix that fits the entry."""
    attachments = data.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return data

    def _candidate(kept: int) -> dict[str, Any]:
        candidate = dict(data)
        if kept:
            candidate["attachments"] = attachments[:kept]
        else:
            candidate.pop("attachments", None)
        omitted = len(attachments) - kept
        if omitted:
            candidate["attachments_omitted"] = omitted
        else:
            candidate.pop("attachments_omitted", None)
        return candidate

    def _fits(candidate: dict[str, Any]) -> bool:
        if not _entry_line_fits(entry_type, candidate, src=src):
            return False
        if inline_text is None:
            return True
        extra = {
            key: value for key, value in candidate.items() if key not in {"turn", "step", "text"}
        }
        return _fits_one_line(inline_text, extra)

    if _fits(data):
        return data
    low, high = 0, len(attachments) - 1
    best = _candidate(0)
    while low <= high:
        middle = (low + high) // 2
        candidate = _candidate(middle)
        if _fits(candidate):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _append_body_entry(
    log: CrewLog,
    entry_type: str,
    turn: int,
    *,
    step: int = 0,
    text: str,
    extra: "dict[str, Any] | None" = None,
    src: str = _SRC_ACP,
) -> None:
    """Append one body-bearing entry. THE single place a body becomes fields.

    Every family that carries a message body goes through here, so the shape of a
    body is decided once. Under :data:`BODY_MODE_TEXT` the redacted text rides on
    the entry, split across ``message/chunk`` entries the citing entry names in
    ``chunks`` when it cannot fit one line. Under :data:`BODY_MODE_REF` the entry would
    ALSO carry a pointer at the transcript position, for the bridge period in which
    both records exist and have to be reconcilable; the body itself stays either
    way.

    Runs on the writer thread with the crew log handle in hand, because the chunks
    must be on disk before the entry citing them can name their seqs.
    """
    data: dict[str, Any] = {"turn": int(turn)}
    if step:
        data["step"] = int(step)
    if extra:
        data.update(extra)
    inline_data = _bounded_attachment_data(
        entry_type, {**data, "text": text}, src=src, inline_text=text
    )
    inline_extra = {
        key: value for key, value in inline_data.items() if key not in {"turn", "step", "text"}
    }
    if _fits_one_line(text, inline_extra or None):
        log.append(entry_type, inline_data, src=src)
        return
    # A chunk group is written as ONE batch, not entry by entry. The chunks are
    # meaningless without the entry that cites their seqs: appending them
    # separately leaves a window in which a hard kill puts the body on disk with
    # nothing pointing at it -- stored and unreachable, and the entry that would
    # have explained it never written. One write leaves either the whole group or a
    # torn tail, and the tail is what the next append truncates.
    slices = _text_slices(text)
    group: list[dict[str, Any]] = [
        {
            "type": "message/chunk",
            "data": {"turn": int(turn), **({"step": int(step)} if step else {}), "delta": piece},
            "ignorable": True,
        }
        for piece in slices
    ]

    def _cite(seqs: list[int]) -> dict[str, Any]:
        """Build the citing entry from the seqs the group was actually allocated.

        Called inside the store's lock, which is what makes the citation unable to
        disagree with the allocation. Reading the tail here and citing the result
        would not: the lock is cross-process, so another handle can append between
        that read and the write, shifting the run so the ``chunks`` name entries
        belonging to the intruder -- seqs that exist and parse, so nothing later
        detects it.
        """
        citing_data = _bounded_attachment_data(
            entry_type,
            {**data, "chunks": list(seqs), "chars": len(text)},
            src=src,
        )
        return {"type": entry_type, "data": citing_data}

    log.append_many(group, src=src, cite=_cite)


def on_message_received(
    session_id: str,
    turn: int,
    *,
    role: str = "user",
    text: str = "",
    source: str = "",
    attachments: "tuple[str, ...] | list[str]" = (),
) -> None:
    """Record the body of a message the gateway accepted into this session.

    Written BEFORE the dispatch gates, so a turn that is refused still shows what
    was said. The alternative -- emitting beside ``turn/started`` -- would lose the
    body of exactly the turns a reader most wants to explain.

    Not emitted where the message is actually appended to the slot
    (``_ChatSlot.enqueue_or_run_prompt``): the crew log is keyed by the ACP session
    id and that site runs before the session is claimed, so there is no id to key
    by and no crew log to write to yet.

    ``source`` is the surface the message arrived on, a fact the dispatch layer
    supplies. ``attachments`` are identifiers, not ``Ref``s -- a ``Ref`` cites
    lines of another crew log and an attachment is not a crew log unit, the same
    reason a tool call id lives in ``data``.

    Gated on the flag before redaction runs: redaction walks the whole body with
    a set of patterns, once per message on the event loop, so a disabled emitter
    must not pay for it.

    The body goes through :func:`_append_body_entry` like every other body, which
    is what gives a pasted message too large for one line the same split the
    assistant side gets. Writing ``text`` directly here would refuse the append
    and lose the message whole.
    """
    if not session_id or not enabled():
        return
    body = _safe_text(text)
    extra: dict[str, Any] = {"role": role, "source": source}
    names = [str(item) for item in attachments if item]
    if names:
        extra["attachments"] = names

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        _append_body_entry(
            log,
            "message/received",
            turn,
            text=body,
            extra=extra,
            src=_SRC_GATEWAY,
        )

    _submit(_job, "appending message/received", session_id, len(body))


def on_message_sent(
    session_id: str,
    turn: int,
    *,
    step: int = 0,
    text: str = "",
    interrupted: bool = False,
) -> None:
    """Record a finished assistant message -- one model call's worth of text.

    The body goes through :func:`_append_body_entry`, the single place a body
    becomes fields: it splits a text too big for one line into ``message/chunk``
    entries the ``message/sent`` then cites in ``chunks``, so the whole text is
    recoverable in order and the 64 KiB ceiling still holds. Those overflow chunks
    carry a body that would otherwise be lost outright, and they are safe to write
    for a reason the removed streaming emitter could not satisfy: the body is
    redacted ONCE, whole, before it is sliced, so a credential cannot straddle two
    slices unmatched.

    All of it happens in ONE queued job. The chunks have to be on disk before the
    entry citing them can name their seqs, and splitting the work across jobs would
    let another entry land between a chunk and its citation.

    No ``usage``. The design's field is real but the runtime measures usage per
    TURN, not per assistant message, and the turn's numbers already ride on
    ``turn/completed``; dividing them across a turn's messages would be a guess
    presented as a measurement.
    """
    if not session_id or not enabled():
        return
    body = _safe_text(text)
    if not body:
        return
    extra: "dict[str, Any] | None" = {"interrupted": True} if interrupted else None
    # A caller that does not name the model call gets the one this turn is on.
    # Derived from the live record rather than asked for, so a caller holding only
    # a slot -- the segment flush does -- has nothing to thread through and nothing
    # to get stale.
    ordinal = int(step) if step else _tracker().current_step(session_id, turn)

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        _append_body_entry(
            log,
            "message/sent",
            turn,
            step=ordinal,
            text=body,
            extra=extra,
            src=_SRC_ACP,
        )

    _submit(_job, "appending message/sent", session_id, len(body))


def on_request_configured(
    session_id: str,
    turn: int,
    *,
    model: str = "",
    provider: str = "",
    system: str = "",
    context_window: int = 0,
) -> None:
    """Record the request configuration, but only when it CHANGED.

    Rewriting an identical configuration every turn would bury the turns where it
    actually moved, and those are the only ones a reader wants from this entry --
    a model swap, a provider fallback, a window that grew. So the last one written
    is remembered per session and an unchanged configuration is silent.

    ``system`` is a DIGEST of the system prompt, never its text: it is long, it is
    identical across most turns, and what a reader needs is whether it changed.

    No ``tools`` list. The design asks for one and the gateway cannot supply it:
    tool specs are served to the model by the backend with tool search on, and the
    only inventory this process holds is the per-stub surface the MCP gateway
    projected, which is keyed by stub rather than by session and carries names
    without spec sizes. An empty list every turn would read as "no tools", which
    is false, so the field is absent instead.
    """
    if not session_id or not enabled():
        return
    system_hash, system_bytes = _payload_digest(system) if system else ("", 0)
    fingerprint = (model, provider, system_hash, int(context_window))
    with _lock:
        if _last_config.get(session_id) == fingerprint:
            return
    data: dict[str, Any] = {
        "turn": int(turn),
        "model": model,
        "provider": provider,
        "context_window": int(context_window),
    }
    if system_hash:
        data["system"] = system_hash
        data["system_bytes"] = system_bytes

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            return
        log.append("request/configured", data, src=_SRC_GATEWAY)
        # Remembered only AFTER the line is on disk. Committing the fingerprint
        # before the append would let one transient failure suppress every later
        # identical configuration, leaving the session permanently without the
        # record -- a silent hole rather than a retry. The other direction costs a
        # duplicate entry when two turns queue before the first lands, and a
        # duplicate in an append-only log is a far cheaper wrong than a gap.
        with _lock:
            _last_config[session_id] = fingerprint
            _last_config.move_to_end(session_id)
            _tracker().trim(_last_config, _MAX_OPEN_CREW_LOGS, lambda k: k, "request configs")

    _submit(_job, "appending request/configured", session_id)


def on_context_composed(
    session_id: str,
    turn: int,
    *,
    step: int = 0,
    blocks: "dict[str, int] | None" = None,
    total_chars: int = 0,
    phase: str = "",
) -> None:
    """Record what the gateway put in front of the model, block by block.

    ``blocks`` is ``context_blocks.split_blocks()``'s own output: a label to
    CHARACTER count map whose values sum to the assembled prompt. Characters are
    what the repo measures exactly, so they are recorded as measured and
    ``tokens`` is derived at :data:`_EST_CHARS_PER_TOKEN` -- an ESTIMATE, and the
    spec says so. The one tokenizer available is the wrong one for the served
    model, and a fabricated exact count would be worse than an admitted estimate.

    Labels pass through as ``split_blocks`` named them, including its
    ``unclassified`` remainder -- three blocks the design names (steering, tool specs
    and injected crew log context) have no opening marker, so their characters are
    genuinely in that bucket, and reporting them as three zeroed sources would claim a
    measurement nobody took. Only a source whose label is EMPTY is renamed, to
    :data:`_OTHER_SOURCE`; see there for why the named remainder keeps its name.

    ``phase`` says which POPULATION this composition belongs to
    (:data:`~kiro_crew.context_blocks.PHASE_SESSION_START` or
    :data:`~kiro_crew.context_blocks.PHASE_PER_TURN`). A session-start injection is
    many times the size of a per-turn one, so a reader that cannot separate them
    either pools two populations into one meaningless distribution or lets the
    single largest composition set the scale for every other. Only the composer
    knows which it built, so the field is recorded here and DERIVED nowhere: an
    unstated phase is left absent, because the nearest available guess -- the first
    composition in a unit -- is wrong for the rebuild a replay triggers mid-session.
    """
    if not session_id or not enabled() or not blocks:
        return
    tallied: dict[str, int] = {}
    for label, chars in blocks.items():
        try:
            count = int(chars)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        key = _OTHER_SOURCE if label in _UNCLASSIFIED_LABELS else str(label)
        tallied[key] = tallied.get(key, 0) + count
    if not tallied:
        return
    sources = [
        {"kind": kind, "chars": chars, "tokens": int(round(chars / _EST_CHARS_PER_TOKEN))}
        for kind, chars in sorted(tallied.items(), key=lambda item: (-item[1], item[0]))
    ]
    chars_total = int(total_chars) or sum(tallied.values())
    data: dict[str, Any] = {
        "turn": int(turn),
        "sources": sources,
        "chars": chars_total,
        "tokens": int(round(chars_total / _EST_CHARS_PER_TOKEN)),
        "tokens_estimated": True,
    }
    if step:
        data["step"] = int(step)
    if phase:
        data["phase"] = str(phase)
    _write(session_id, "context/composed", data, src=_SRC_GATEWAY)


def on_step_started(session_id: str, turn: int) -> int:
    """Open a model call inside *turn* and return its ordinal.

    A step is ONE model call. A turn is several: the model speaks, calls tools,
    and is called again with their results. The stream carries no per-call event --
    its terminal event is the turn's -- so the boundary is synthesized at the one
    transition that is observable, a tool group followed by fresh text, and the
    spec records that this is a derived boundary rather than a reported one.

    Returns the ordinal so the caller can pass it to the entries produced inside
    the call and to :func:`on_step_completed`, without reading it back.
    """
    if not session_id or not enabled():
        return 0
    step = _tracker().next_step(session_id, turn)
    _write(
        session_id,
        "step/started",
        {"turn": int(turn), "step": step},
        src=_SRC_GATEWAY,
    )
    return step


def on_step_completed(session_id: str, turn: int, step: int, *, ms: int = 0) -> None:
    """Close a model call and record how long it took."""
    if not step:
        return
    _write(
        session_id,
        "step/completed",
        {"turn": int(turn), "step": int(step), "ms": max(0, int(ms))},
        src=_SRC_GATEWAY,
    )


def on_message_queued(
    session_id: str,
    *,
    source: str = "",
    size_bytes: int = 0,
    queued_seq: str = "",
) -> None:
    """Record a message that arrived while a turn was already running.

    No ``turn``, and the absence is the record: a queued message belongs to no
    turn yet. It names the turn it eventually runs as when that turn starts, and
    stamping the RUNNING turn's ordinal here would attribute one person's message
    to another's turn.

    The body is not recorded. It is recorded by ``message/received`` when the
    queue drains and the message is actually accepted, so writing it twice would
    put the same body in the log under two facts. Its SIZE is recorded, which is
    what a reader asks of a queue.
    """
    _write(
        session_id,
        "message/queued",
        {"source": source, "bytes": max(0, int(size_bytes)), "queued_seq": str(queued_seq or "")},
        src=_SRC_GATEWAY,
    )


def _payload_digest(payload: str) -> tuple[str, int]:
    """``(sha256, byte length)`` for *payload*, or ``("", -1)`` when there is none.

    A digest and a size, never the bytes. That is the whole point: the log can say
    two calls had the SAME arguments, or that a result was enormous, without the
    log becoming a place secrets and file contents accumulate. ``-1`` distinguishes
    "not recorded" from a genuinely empty payload, which is 0.
    """
    if not payload:
        return "", -1
    raw = payload.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def on_tool_called(
    session_id: str,
    turn: int,
    *,
    name: str,
    server: str = "",
    kind: str = "",
    call_id: str = "",
    args: str = "",
) -> None:
    """Record a tool call by its id. Arguments are digested, never recorded.

    Two ordinals, and they answer different questions. ``call_index`` is this
    call's position among the turn's calls, in the order the runner issued them.
    ``step`` is the model call that issued it -- one model call can issue several
    tools at once, so it cannot order them, and the pair together says both which
    request caused the call and where it sat in the sequence.

    The flag is checked HERE, not just in :func:`_write`. Digesting a payload is
    proportional to its size and this runs once per tool frame on the event loop,
    so a disabled emitter that still digested would charge every user for a
    feature they do not have.
    """
    if not session_id or not enabled():
        return
    tracker = _tracker()
    call_index = tracker.next_call_index(session_id, turn)
    step = tracker.current_step(session_id, turn)
    if call_id:
        tracker.call_opened(
            session_id,
            call_id,
            name=name,
            server=server,
            call_index=call_index,
            step=step,
            turn=turn,
        )
    data: dict[str, Any] = {
        "turn": int(turn),
        "call_id": call_id,
        "name": name,
        "server": server,
        "kind": kind,
    }
    if call_index:
        data["call_index"] = call_index
    if step:
        data["step"] = step
    args_hash, args_bytes = _payload_digest(args)
    if args_hash:
        data["args_hash"] = args_hash
        data["args_bytes"] = args_bytes
    _write(session_id, "tool/called", data)


def on_tool_completed(
    session_id: str,
    turn: int,
    *,
    name: str = "",
    server: str = "",
    status: str = "",
    call_id: str = "",
    is_error: bool | None = None,
    result: str = "",
    result_digest: str = "",
    result_bytes: int = -1,
) -> None:
    """Record a tool call's terminal frame. Results are digested, never recorded.

    The terminal frame does not repeat the tool's identity -- only the call
    frame carries the trusted name and server -- so both are remembered per
    call id and filled in here rather than being recorded empty. The call's
    ``call_index`` and ``step`` ride along the same way, so a completion sits at
    the same position in the turn, and inside the same model call, as the call it
    closes.

    ``is_error`` is tri-state: ``None`` means the caller did not say, which is
    not the same claim as ``False``, so it is left off the entry rather than
    recorded as a success nobody asserted.

    Gated on the flag here for the same reason as the call: a tool RESULT is the
    largest payload this module ever digests.
    """
    if not session_id or not enabled():
        return
    elapsed_ms = -1
    call_index = 0
    step = 0
    if call_id:
        # A tool call settles exactly ONCE. Both update parsers can produce a
        # status-only terminal frame for the same id, so a second frame arriving
        # after the first closed the call must add NOTHING -- otherwise one call gets
        # two ``tool/completed`` entries. The tracker answers None for a call it has
        # already settled; a call whose ``tool/called`` it never saw still settles and
        # still gets its closer, with empty name/server and no elapsed.
        settled = _tracker().call_settled(session_id, call_id, turn)
        if settled is None:
            return
        elapsed_ms = settled.elapsed_ms
        name = name or settled.name
        server = server or settled.server
        call_index = settled.call_index
        step = settled.step
    data: dict[str, Any] = {
        "turn": int(turn),
        "call_id": call_id,
        "name": name,
        "server": server,
        "status": status,
    }
    if call_index:
        data["call_index"] = call_index
    if step:
        data["step"] = step
    if elapsed_ms >= 0:
        data["elapsed_ms"] = elapsed_ms
    if is_error is not None:
        data["is_error"] = bool(is_error)
    if result_bytes < 0:
        result_digest, result_bytes = _payload_digest(result)
    if result_digest:
        data["result_hash"] = result_digest
    if result_bytes >= 0:
        data["result_bytes"] = result_bytes
    _write(session_id, "tool/completed", data)


def on_approval_requested(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    tool: str = "",
    reason: str = "",
) -> None:
    """Record that a tool call is waiting on a human.

    ``reason`` is what the human is being shown -- the card's title, or the
    command for a shell request. It arrives already display-redacted by the ACP
    transport and is redacted again here, because this module redacts at its own
    boundary rather than trusting a call site, and clipped so a long command
    cannot push the entry past the line ceiling and lose the whole fact.

    ``tool`` and ``reason`` are each absent rather than empty when the site had
    nothing to name. A permission frame can arrive without a resolvable tool name,
    and writing ``""`` there would record "the tool is the empty string" -- a value
    a reader cannot tell from a real one, in a log whose entire worth is that it
    only says what was observed.

    Guarded on the flag here for the same reason :func:`on_plan_updated` is: the
    redaction below runs before :func:`_write` gets its own chance to no-op, and the
    runner calls this unconditionally.
    """
    if not session_id or not enabled():
        return
    data: dict[str, Any] = {"turn": int(turn), "approval_id": approval_id}
    if tool:
        data["tool"] = tool
    shown = _clip(_safe_text(reason), _MAX_SHORT_TEXT)
    if shown:
        data["reason"] = shown
    _write(session_id, "approval/requested", data, src=_SRC_GATEWAY)


def on_approval_decided(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    decision: str,
    by: str = "",
    cause: str = "",
) -> None:
    """Record how an approval resolved, including a timeout.

    This runs on the task that handled the click, not the task running the
    turn, which is why it must never raise.

    ``by`` names WHO decided, and only the host itself can be named with
    certainty: an auto-decline the gateway made is attributable, so it says
    ``host``. A decision that came back through the approval future was made by a
    person at one of several surfaces -- dashboard click, Slack button -- and the
    site cannot see which, so it omits the field rather than asserting ``user``
    for something it did not observe.

    ``cause`` is WHY, and only a host decline has one: the gateway's own reason
    code for declining without a human (the window expired, the turn had no
    budget left, the prompt could not be delivered). It rides in its own field
    instead of replacing ``decision``, so a reader still learns what was decided
    and does not have to know the reason vocabulary to find out.
    """
    data: dict[str, Any] = {
        "turn": int(turn),
        "approval_id": approval_id,
        "decision": decision,
    }
    if by:
        data["by"] = by
    if cause:
        data["cause"] = cause
    _write(session_id, "approval/decided", data, src=_SRC_GATEWAY)


#: How many dispatched children this module remembers an origin for. A child's
#: origin is released by its own terminal entry, so this cap is only reached by
#: children that never reach one -- a queued member cancelled before it starts,
#: a run lost to a crash. Generous, because the cost of holding one is two small
#: values and the cost of evicting one is a closer that cannot be filed.
_MAX_CHILD_ORIGINS = 2048
#: How many of the OLDEST pins are examined for a finished child before the cap
#: falls back to dropping the oldest outright. A pin is released by its child's
#: terminal entry, so one still held while its neighbours have gone belongs to a
#: child that never reported, and those collect at the old end -- which is why a
#: window there is where they are found. Bounded because answering takes a call
#: into the subagent side per pin, and scanning the whole map would charge one
#: caller the entire cap's worth of them.
_ORIGIN_REAP_SCAN = 64
#: The longest session id a pin will retain. The count cap above bounds memory
#: only if every field of a pin is bounded too, and this one is authored by the
#: provider rather than by this process. Far above any id this codebase produces
#: -- a provider session id is UUID-shaped and a channel session key is shorter
#: still -- so it rejects nothing legitimate and exists only so a broken or
#: hostile provider cannot make the count cap meaningless. An id past it is
#: refused, never shortened: an identity that has been cut down names a
#: different unit or none.
_MAX_SESSION_ID_CHARS = 512


def set_child_liveness(probe: "Callable[[str], bool] | None") -> None:
    """Register the process's own answer to "is this subagent still running".

    Called once by the gateway, which constructs the subagent manager; this module
    has no route to it and must not grow one, since the emitters are called FROM
    that side. *probe* takes an ``agent_id`` and returns True while the child can
    still report its own outcome.

    Only the resume repair reads it, and only to decide whether an unmatched
    ``subagent/spawned`` may be closed. Nothing in the crew log file can answer
    that: an unbalanced opener is what a finished-but-unreported child and a
    still-running one both look like. Leaving it unset is safe and is what tests
    and any embedder without subagents get -- no child is ever closed, so a reader
    sees an open child rather than a fabricated outcome.
    """
    global _child_liveness
    _child_liveness = probe


def _child_gone_probe(session_id: str) -> "Callable[[str], bool] | None":
    """``child_gone`` for the store, or None when this process cannot answer.

    Inverted here rather than at the registration site so the gateway registers
    the fact it actually holds -- its manager lists what is RUNNING -- instead of
    a negation that reads backwards at the call site.

    A child is reported present while this process owes ANY entry for
    *session_id*, on top of what the registry says. A terminal outcome reaches
    the file through the writer and :func:`_submit` returns before it lands, so a
    child leaves the running set while its own closer is still queued: the file
    then shows an opener with no match while the registry omits the child. Closing on that reading puts a synthesised ``unknown`` ahead of the
    real outcome and leaves both standing in a file nothing rewrites. The debt
    set is the exact record of what is still owed -- queued, claimed by the
    writer, retained, and owed loss markers alike -- and it is the one source
    here carrying no bound that
    could shed a pending child, which the manager's completed-run retention does
    carry. An entry a hard ceiling refuses is absent from the debt set, which is
    the wanted answer: that closer is never coming, so the opener is the
    repair's to close.

    The two reads live in different lock domains, so they are sampled at
    different instants and their ORDER decides whether the gap between them can
    lie. Liveness is read first for that reason.

    Neither read is enough on its own, because the normal completion path flips
    ``done`` long BEFORE the closer is recorded: the manager's running set is
    everything not done, so the child leaves it at the flip, and the terminal
    entry is handed over later from the report task. Between those two the child
    is absent from the running set and owes nothing, and a repair reading exactly
    there synthesises an ``unknown`` that then stands beside the real outcome. So
    the child's OWN origin pin is consulted too: it is opened when the spawn is
    recorded and released only once the closer has been handed to the writer, an
    interval that contains that whole gap, and it is per-child rather than
    per-session. What remains is the pin the FIFO drops under its own ceiling,
    which is counted rather than silent.
    """
    probe = _child_liveness
    if probe is None:
        return None

    def _gone(agent_id: str) -> bool:
        if probe(agent_id):
            return False
        if child_origin(agent_id)[0]:
            return False
        return not _writer_owes(session_id)

    return _gone


def remember_child_origin(agent_id: str, session_id: str, turn: int) -> None:
    """Pin the parent session and turn that dispatched *agent_id*, unopened.

    A child's later facts -- its ``subagent/spawned`` entry, a steer, its terminal
    outcome -- are produced after the parent's turn has ended, often while the
    parent is on a different turn entirely. Reading the parent's CURRENT turn at
    any of those points would file the child under a turn that did not ask for it,
    so the ordinal is captured once, where the dispatch was accepted and the asking
    turn is still live, and every later entry about this child reuses it.

    The pin starts UNOPENED, because being accepted is not being started: a spawn
    still has to clear the approval gate, and a decline returns without ever
    running. :func:`open_child_origin` is what promotes it, at the one site that
    means "this run is really starting" -- so a declined spawn leaves no opener,
    and the closer helpers below refuse to close what was never opened.

    Idempotent: a queued member is accepted, waits behind the stagger gate, and
    re-enters the spawn path under the SAME id, which must not move the origin it
    was accepted with, nor un-open it.

    An over-long *session_id* is REFUSED rather than stored. A cap on how many
    pins are held bounds memory only if every field of a pin is bounded too, and
    the session id is authored by the provider, not by this process. It is refused
    rather than shortened because it is an IDENTITY: a truncated id names a
    different unit or none at all, so storing a cut-down copy would file this
    child's entries against the wrong crew log. The refusal is counted like any
    other lost origin, since the consequence is the same -- that child's entries
    are absent.
    """
    global _lost_child_origins, _lost_origin_reported

    if not agent_id or not session_id:
        return
    if len(session_id) > _MAX_SESSION_ID_CHARS:
        with _lock:
            _lost_child_origins += 1
            report = not _lost_origin_reported
            _lost_origin_reported = True
        if report:
            logger.warning(
                "crew log: a child's session id exceeds %d characters, so its "
                "origin is refused and its entries will be absent; counted in "
                "lost_child_origins()",
                _MAX_SESSION_ID_CHARS,
            )
        return
    with _lock:
        if agent_id in _child_origin:
            return
        _child_origin[agent_id] = (session_id, int(turn), False)
        if len(_child_origin) <= _MAX_CHILD_ORIGINS:
            return
        oldest = list(_child_origin)[:_ORIGIN_REAP_SCAN]
    _reap_child_origin(oldest)


def _reap_child_origin(oldest: "list[str]") -> None:
    """Make room in the pin map, dropping a finished child's pin before a live one.

    Called with ``_lock`` RELEASED. Choosing which pin to drop means asking the
    liveness probe, and that probe belongs to the subagent side; holding this
    module's non-reentrant lock across a foreign call is how a deadlock is built.
    The map is re-checked under the lock before anything is removed, so a release
    that happens in between simply leaves nothing to do.

    A pin is released by its child's terminal entry, so a pin held while its
    neighbours have gone belongs to a child that never reported one -- a run lost
    to a crash, a member cancelled before it started. Those are free to drop: the
    entries they could still carry are never coming. Dropping them is also what
    keeps this cap from being reached by accumulation over long uptime, rather than
    only by that many children genuinely running at once.

    When every candidate is still running, the oldest goes and the loss is COUNTED
    in :func:`lost_child_origins` and named in the log once. That child's opener or
    outcome will be absent, and an absence a reader cannot see is the one loss this
    module refuses to allow silently. Nothing is WRITTEN for it: the registry
    declares no type for a lost pin, and inventing one here would be a shape change
    made to describe a bug rather than a fact of the session.

    The scan is bounded, so a finished pin sitting past the window is missed and
    the oldest live pin is dropped instead. That trades an exact choice for a
    bounded cost in a state that is already pathological, and the trade is visible
    because the drop is counted either way.
    """
    global _lost_child_origins, _lost_origin_reported

    probe = _child_liveness
    finished: list[str] = []
    if probe is not None:
        for candidate in oldest:
            try:
                running = probe(candidate)
            except Exception:
                # An unanswerable probe is not an answer. Treat the child as
                # running, so a pin is never dropped on a failed read.
                logger.debug("crew log: child liveness probe failed", exc_info=True)
                continue
            if not running:
                finished.append(candidate)

    with _lock:
        if len(_child_origin) <= _MAX_CHILD_ORIGINS:
            return
        for candidate in finished:
            if _child_origin.pop(candidate, None) is not None:
                return
        _child_origin.popitem(last=False)
        _lost_child_origins += 1
        report = not _lost_origin_reported
        _lost_origin_reported = True
    if report:
        logger.warning(
            "crew log: the child-origin map is full of running children, so a "
            "live child's origin was dropped and its remaining entries will be "
            "absent; counted in lost_child_origins()"
        )


def open_child_origin(agent_id: str) -> "tuple[str, int]":
    """Mark *agent_id*'s pin OPENED and return it, or ``("", 0)`` if unknown.

    Called where the run actually begins. Returning the pinned pair rather than
    reading the parent's live turn is the whole point: this site can be reached a
    long human approval later, by which time the parent is on another turn.

    Idempotent, so a second call cannot produce a second opener.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None:
            return ("", 0)
        session_id, turn, _opened = found
        _child_origin[agent_id] = (session_id, turn, True)
        return (session_id, turn)


def child_origin(agent_id: str) -> "tuple[str, int]":
    """*agent_id*'s pinned origin if its spawn was recorded, else ``("", 0)``.

    Gated on opened: an entry about a child that has no ``subagent/spawned`` line
    would be a fact with no cause, which is worse than the fact being missing.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None or not found[2]:
            return ("", 0)
        return (found[0], found[1])


def dispatch_origin(agent_id: str) -> "tuple[str, int]":
    """*agent_id*'s pinned origin whether or not its spawn was recorded.

    The one reader that is correct BEFORE the opener exists. :func:`child_origin`
    refuses an unopened pin because a fact about a child that never started would
    have no cause in the log; a spawn approval is the exception, because the wait
    for it is the cause of the gap. It happens between the dispatch being accepted
    and the run starting, and on two of its three exits no run ever starts -- so
    gating it on opened would lose exactly the prompt a reader is looking for.

    Answers ``("", 0)`` for a child with no pin at all, which is what a caller
    needs to skip the write rather than guess a parent.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None:
            return ("", 0)
        return (found[0], found[1])


def forget_child_origin(agent_id: str) -> "tuple[str, int]":
    """Release *agent_id*'s origin and return it, or ``("", 0)``.

    Called from the child's terminal report, which is exclusive and one-shot, so
    the release happens exactly once and a second terminal cannot write a second
    closer with a resolved origin.

    Also gated on opened, and the release happens either way: a spawn declined at
    the approval gate is pinned but never opened, and its terminal report must
    close nothing while still dropping the pin rather than leaving it for the FIFO
    to evict.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.pop(agent_id, None)
        if found is None or not found[2]:
            return ("", 0)
        return (found[0], found[1])


def on_plan_updated(session_id: str, turn: int, *, items: Any) -> None:
    """Record the agent's own task list as the agent just restated it.

    A TODO update is a WHOLE list, not a delta: the agent re-sends every task on
    every change, so the entry is the list as of this update and a reader diffs
    consecutive entries itself. ``items`` is the stream's own ``tasks`` array of
    ``{id, text, completed}``. ``None`` means the event said nothing about the
    plan and no entry is written; an empty LIST means the agent cleared its plan,
    which is a change and is recorded as one.

    ``state`` is two-valued -- ``done`` / ``open`` -- because that is all the
    stream carries. The backend's todo model is a plain ``completed`` boolean with
    no in-progress state, as the slot's own snapshot code documents, so a
    three-state vocabulary would be invented here and is not written.

    The list is bounded twice, by COUNT and by BYTES, and both bounds keep the real
    count in ``total`` so a clipped record still says how much it is not showing.
    Count alone is not enough: ``_clip`` bounds each ``text`` in characters while the
    store serializes with ``ensure_ascii``, which spends six bytes on a BMP character
    and twelve on a surrogate pair -- so a hundred separately-legal rows of emoji
    serialize past the entry ceiling, where the append is REFUSED and the whole
    update disappears. Measured through the store's own serializer, because that is
    what the writer will measure.

    Guarded on the flag HERE rather than relying on :func:`_write`'s own guard,
    because this function does real work before it reaches one: a redaction per task
    and a serialize probe per admitted row, on the chat loop, for a feature that can
    be switched off. The subagent and background emitters are guarded at their callers
    instead; this and :func:`on_approval_requested` are the two the runner calls
    unconditionally, so they carry their own.
    """
    if not session_id or not enabled():
        return
    rows: list[dict[str, Any]] = []
    total = 0
    #: Set by the first row the line cannot hold. Every later row is then counted
    #: and not admitted, because ``items`` is read as the FRONT of the plan: a
    #: shorter row admitted past a dropped one would make it a subsequence, and a
    #: reader diffing consecutive entries would see tasks reorder and vanish.
    full = False
    if items is None:
        # Not the same as a plan of zero tasks. The event carried no task list at
        # all, so nothing was observed about the plan, and an entry claiming it is
        # now empty would be an invention. An event that DOES carry an empty list
        # is a cleared plan and is recorded as one.
        return
    for task in items:
        if not isinstance(task, dict):
            continue
        total += 1
        if full or len(rows) >= _MAX_PLAN_ITEMS:
            continue
        row = {
            "id": _clip(_safe_text(str(task.get("id") or len(rows) + 1)), _MAX_ID_TEXT),
            "text": _clip(_safe_text(task.get("text")), _MAX_SHORT_TEXT),
            "state": "done" if task.get("completed") else "open",
        }
        # Measured against the entry it is about to join, and the widest form of
        # that entry: `total` is included so admitting this row cannot be what
        # pushes the finished line over once the count field appears.
        probe = {"turn": int(turn), "items": rows + [row], "total": total}
        if rows and not _entry_line_fits("plan/updated", probe, src=_SRC_ACP):
            full = True
            continue
        rows.append(row)
    data: dict[str, Any] = {"turn": int(turn), "items": rows}
    if total > len(rows):
        data["total"] = total
    # A sampled stream: the agent overwrites its plan freely and nothing later in
    # the file depends on any single update having been read.
    _write(session_id, "plan/updated", data, src=_SRC_ACP, ignorable=True)


def on_background_completed(
    session_id: str,
    *,
    kind: str,
    model: str = "",
    provider: str = "",
    credits: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    duration_ms: int = 0,
) -> None:
    """Record a model call the gateway made ON this session's behalf.

    Titling, summarizing and memory consolidation spend the user's budget without
    the user asking, and until now that spend appeared in the usage store with no
    trace in the session it was charged to. This is that trace.

    No ``turn``. The call is not part of one -- it runs after a turn ends, on a
    separate background session -- and naming the turn that happened to be last
    would attribute the cost to work that did not cause it.

    ``tokens`` and ``credits`` are written only when a dimension was actually
    billed, following ``turn/completed``: a provider fills the dimensions it bills
    in and leaves the rest at 0, so a zero is "this provider does not bill here",
    not a measurement. ``duration_ms`` is the wall clock the background helper
    measured around the call itself, and it is likewise omitted at 0.
    """
    data: dict[str, Any] = {"kind": kind}
    if model:
        data["model"] = model
    if provider:
        data["provider"] = provider
    if credits:
        data["credits"] = float(credits)
    tokens = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    if any(tokens.values()):
        data["tokens"] = {name: count for name, count in tokens.items() if count}
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    _write(session_id, "background/completed", data, src=_SRC_GATEWAY)


def on_subagent_spawned(
    session_id: str,
    turn: int,
    *,
    agent_id: str,
    agent: str = "",
    model: str = "",
    task: str = "",
    scope: Any = None,
) -> None:
    """Record a child this session dispatched.

    ``turn`` is the turn that ASKED, captured where the spawn was accepted --
    which runs inside the parent's turn, since a spawn arrives as one of its tool
    calls. It is passed in rather than read here on purpose: the child starts,
    steers and finishes long after that turn has ended, and every later entry
    about this child reuses the captured ordinal instead of asking what turn the
    parent is on now.

    No ``ref`` into the child's log. The schema describes one, and a child that
    had a crew log would deserve it, but no subagent code path opens one: the only
    site that creates a session's crew log is the dashboard turn path, and a subagent
    run does not go through it. A ``ref`` written now would cite a file that does
    not exist, which a reader cannot distinguish from one that was deleted. It
    becomes writable, unchanged, the day subagent sessions get crew logs of their
    own.

    ``turn`` is ABSENT when no turn asked, the same way :func:`on_model_selected`
    omits its own. A spawn does not always arrive inside a model turn -- a slash
    command, a cron and a hook all dispatch children of a session with nothing
    running -- and turns are numbered from one, so a literal ``0`` would name a
    turn that never existed and match no ``turn/started``. The child is still
    recorded: it is a real child of that session, and losing it to keep a field
    populated would be the worse trade.

    ``task`` is what the child was asked to do, redacted and clipped on the same
    terms as ``plan/updated``'s item text -- the other place this module records
    text a person wrote. It is the one thing a reader needs to tell two children
    apart that is not derivable from anything else in the entry, and a surface
    rebuilding a child's card after the dispatching process is gone has nowhere
    else to read it from.

    It is written only when non-empty, so a dispatch that carried no task text
    leaves the field ABSENT rather than present-and-empty. The two are different
    facts to a reader: absent is "this log does not say", which is also what every
    log written before the field reads as, and a surface draws no task line for
    it. An empty string would claim the dispatch asked for nothing.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if turn:
        data["turn"] = int(turn)
    if agent:
        data["agent"] = agent
    if model:
        data["model"] = model
    asked = _clip(_safe_text(task), _MAX_SHORT_TEXT)
    if asked:
        data["task"] = asked
    if isinstance(scope, dict):
        data["scope"] = {
            "memory": bool(scope.get("memory")),
            "lessons": bool(scope.get("lessons")),
            "project": bool(scope.get("project")),
        }
    _write(session_id, "subagent/spawned", data, src=_SRC_GATEWAY)


def on_subagent_steered(session_id: str, *, agent_id: str, mode: str = "") -> None:
    """Record a correction sent into a running child.

    Written into the PARENT's log: the parent is what sent it, and the child has
    no crew log to receive it.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if mode:
        data["mode"] = mode
    _write(session_id, "subagent/steered", data, src=_SRC_GATEWAY)


def on_subagent_dismissed(
    session_id: str,
    *,
    agent_id: str,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that the user cleared a child's card from the panel.

    Written into the PARENT's log, like a steer, and for the same reason: the
    child has no crew log of its own and the act belongs to the session the panel
    was showing.

    It lives in the log rather than in a registry beside it because the panel's
    durable half is a FOLD of this log. A dismissal held anywhere else is a second
    record of a fact about this session, and the two are reclaimed on different
    schedules -- which is not hypothetical: the registry that held it was keyed on
    the run's folder at both ends, so a dismissed card came back the moment that
    folder was pruned while the log still carried the child.

    ``on_settled`` is handed True only once the append has COMMITTED. A caller that
    will tell the user the card is gone needs that, because this is the only record
    of the dismissal when the run's folder has already been reclaimed: the queue
    returns as soon as the entry is handed over, so publishing on the handover
    reports a dismissal the next reconnect can undo, with nothing left to explain
    it and nothing for a retry to act on.

    Opens and closes nothing. A dismissal is not an ending, and the child keeps
    whatever outcome its own closer recorded; a user may also clear a card while
    the child is still running, so this can precede any closer.
    """
    _write(
        session_id,
        "subagent/dismissed",
        {"agent_id": agent_id},
        src=_SRC_GATEWAY,
        on_settled=on_settled,
    )


def dismiss_child(agent_id: str, *, on_settled: "Callable[[bool], None] | None" = None) -> str:
    """Record a dismissal against the session this process dispatched *agent_id* from.

    Returns that session's id, or ``""`` when this process cannot name one -- the
    emitter is off, or the spawn pin for this child is gone, which is what a
    gateway restart leaves behind. A caller that must record the dismissal some
    other way reads the empty answer as "nothing was written here".

    The pin rather than a lookup, for the reason it exists: it is the session the
    child's own ``subagent/spawned`` was written to, so the dismissal lands in the
    log that holds the row it is about. :func:`child_origin` is gated on that entry
    having been opened, so a child whose spawn was never recorded answers ``""``
    rather than putting a dismissal in a log with no dispatch to match it.

    ``on_settled`` is passed to :func:`on_subagent_dismissed` and so reports
    whether the append COMMITTED. The returned session id says only that one was
    queued: a caller publishing a dismissal to the user needs the callback, since
    the empty-string answer and a queued-then-dropped append are the same outcome
    from the user's side and only one of them is visible in the return value.
    """
    if not enabled():
        return ""
    session_id, _turn = child_origin(agent_id)
    if not session_id:
        return ""
    on_subagent_dismissed(session_id, agent_id=agent_id, on_settled=on_settled)
    return session_id


def on_subagent_completed(
    session_id: str, *, agent_id: str, duration_ms: int = 0, credits: float = 0.0
) -> None:
    """Close a child that finished its work, and what it cost.

    Only for the ``completed`` outcome. A stopped or failed child closes through
    :func:`on_subagent_failed`, because the runtime's own three-way outcome exists
    precisely to stop consumers reading "no error" as success.

    ``credits`` is the run's own accumulator, cumulative across every attempted
    turn including billed retries that failed before the last one. It is written
    only when positive: a provider that does not bill in credits reports zero
    through the shared ``TurnUsage`` contract, which is indistinguishable at this
    seam from a run that was genuinely free, so writing the zero would present the
    absence of a measurement as a measurement of zero. ``tokens`` stays absent
    throughout -- nothing in the subagent runtime measures them.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    if credits > 0 and math.isfinite(credits):
        data["credits"] = float(credits)
    _write(session_id, "subagent/completed", data, src=_SRC_GATEWAY)


def on_subagent_failed(
    session_id: str,
    *,
    agent_id: str,
    reason: str = "",
    outcome: str = "failed",
    duration_ms: int = 0,
    credits: float = 0.0,
) -> None:
    """Close a child that did NOT finish its work, and what it cost anyway.

    Covers both non-success outcomes, and says which in ``outcome``: a run the
    user stopped is not a failure and must not read as one, but it is also not a
    completion, and the schema offers no third closer. Carrying the runtime's own
    outcome verbatim keeps the two distinguishable without renaming a frozen type
    or leaving the ``subagent/spawned`` entry open forever.

    ``credits`` follows :func:`on_subagent_completed`: positive only, because a
    zero cannot be told apart from an unbilled provider. A run that did not finish
    still billed for the turns it attempted, so this is the one place that charge
    would otherwise be lost. The crash-repair closer passes nothing, which is
    correct -- it knows only that the writer is gone.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    shown = _clip(_safe_text(reason), _MAX_SHORT_TEXT)
    if shown:
        data["reason"] = shown
    if outcome:
        data["outcome"] = outcome
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    if credits > 0 and math.isfinite(credits):
        data["credits"] = float(credits)
    _write(session_id, "subagent/failed", data, src=_SRC_GATEWAY)


def on_model_selected(session_id: str, model: str, source: str = "", *, turn: int = 0) -> None:
    """Record the model a session will serve and why it was chosen.

    ``turn`` is the turn the pick was made for. The fallback swap happens inside
    a running turn, so the site knows the ordinal and passes it; a pick made
    outside any turn records none rather than a placeholder.
    """
    data: dict[str, Any] = {"model": model, "source": source}
    if turn:
        data["turn"] = int(turn)
    _write(session_id, "model/selected", data, src=_SRC_GATEWAY)


def on_compaction_applied(
    session_id: str,
    *,
    pct_before: float,
    pct_after: float,
) -> None:
    """Record a compaction as context-usage percentages.

    No ``turn``, and the absence is the honest record: compaction is decided by
    the session's context meter between turns, and the settle path that confirms
    its effect can run turns later than the compaction it measures. Stamping one
    of those turns on the entry would name a turn that did not cause it.

    The compaction boundary measures ``provider.context_usage_pct()`` and never
    learns a raw token count, so this records what the site knows.
    """
    _write(
        session_id,
        "compaction/applied",
        {
            "pct_before": round(float(pct_before), 4),
            "pct_after": round(float(pct_after), 4),
            "freed_pct": round(float(pct_before) - float(pct_after), 4),
        },
        src=_SRC_GATEWAY,
    )


def on_ledger_recorded(session_id: str, data: dict[str, Any]) -> None:
    """Append ONE ``ledger/recorded`` entry -- a session's own durable work state.

    The write half of the session ledger. Every field the caller set rides on this
    single entry, including the event that explains a phase change, so the rule
    that a phase never moves without a logged reason is a property of one append
    rather than of two writes that a crash can separate.

    Queued through the same writer as every other entry, deliberately. The ledger
    could not open the file itself: an append takes that unit's WRITE OWNERSHIP,
    and while the emitter holds this session's handle a second handle in this
    process is refused -- so a ledger that wrote around the emitter would fail for
    exactly the sessions that are running. Going through the writer also keeps this
    entry ordered against the turn it was recorded inside.

    The caller is the one that establishes the session has a crew log to write to;
    this is the ordinary ``_write``, so a session without one is a policy no-op
    here and the refusal belongs where a user can be told about it.
    """
    _write(session_id, "ledger/recorded", data, src=_SRC_GATEWAY)


def ledger_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``ledger/recorded`` entry.

    Beside the write rather than inside it, because the two answers have different
    owners. ``_write`` is a policy no-op for a session with no crew log and refuses an
    oversized entry by COUNTING it -- both correct there, since it serves callers that
    cannot act on either. The ledger's caller can act on this one: an entry over the
    ceiling by construction can never land, so the record it would report as taken
    never exists, and only that caller can turn the refusal into an answer a user sees.

    It asks the same question the append will, through the same serializer with the
    same entry type and src, so the two cannot disagree about what fits. Not a
    reservation: a later entry does not make this one smaller, and nothing else
    consumes the budget.
    """
    return _entry_line_fits("ledger/recorded", data, src=_SRC_GATEWAY)


def on_object_observed(
    session_id: str,
    *,
    producer: str,
    kind: str,
    target: str,
    fingerprint: str,
    facts: "Mapping[str, Any]",
    observed_at: float,
) -> None:
    """Record the state of an object outside the session, as *producer* observed it.

    The producer half of the crew log's external-state record. A structured
    monitor's probe computes a canonical snapshot of the pull request it watches
    and, before this, threw that snapshot away once the wake was decided. This
    appends it into the OWNER session's log -- the session the monitor works for --
    so "what state is that pull request in" becomes a typed read beside the holder
    fold's "which session holds it", instead of a text search over whatever an
    agent happened to say about it.

    *producer* is refused outside
    :data:`~kiro_crew.crew_log.entry_types.OBJECT_PRODUCERS`, and refused HERE
    rather than coerced. The value is the point of the entry: a reader trusts a
    measured record because it can see which mechanism measured it, and a producer
    coerced to some default would attribute the record to a mechanism that did not
    make it. The ``ValueError`` is a programming error surfaced at the site that
    made it; the registry's closed enum behind this is the guard a caller cannot
    skip by writing around this function.

    The caller decides WHEN: one call per change of the probe's fingerprint, never
    one per poll, so the log holds distinct states rather than a heartbeat.

    *facts* is recorded verbatim. When the whole line would cross the store's
    ceiling -- a review host reporting hundreds of long check identities can do
    it -- the largest members are removed until it fits and are named in
    ``facts_omitted``, so the record is short by a NAMED part rather than lost
    whole or silently trimmed. Recording nothing was rejected: the change
    happened, and a reader that finds no entry cannot tell "unchanged" from
    "did not fit".
    """
    from kiro_crew.crew_log.entry_types import OBJECT_PRODUCERS

    if producer not in OBJECT_PRODUCERS:
        raise ValueError(
            f"object/observed producer must be one of {list(OBJECT_PRODUCERS)}, not {producer!r}"
        )
    # Off is free: the fit loop below serializes the snapshot and reaches the
    # storage package, work no disabled launch should do on the event loop.
    if not session_id or not enabled():
        return
    snapshot: dict[str, Any] = dict(facts)
    data: dict[str, Any] = {
        "producer": producer,
        "kind": str(kind),
        "target": str(target),
        "fingerprint": str(fingerprint),
        "facts": snapshot,
        "observed_at": float(observed_at),
    }
    omitted: list[str] = []
    while snapshot and not _entry_line_fits("object/observed", data, src=_SRC_GATEWAY):
        largest = max(
            snapshot,
            key=lambda name: len(json.dumps(snapshot[name], ensure_ascii=True, default=str)),
        )
        del snapshot[largest]
        omitted.append(largest)
        data["facts_omitted"] = omitted
    _write(session_id, "object/observed", data, src=_SRC_GATEWAY)


def on_radar_recorded(session_id: str, data: dict[str, Any]) -> None:
    """Append ONE ``radar/recorded`` entry -- an Issue Radar crew's own ledger update.

    The write half of the crew ledger. Every field the caller set rides on this single
    entry, including the event that explains a phase change and the skip row that
    indexes a pass, so the rules that a phase never moves without a logged reason and
    that an issue is never skipped without being indexed are properties of one append
    rather than of three writes a crash can separate.

    Queued through the same writer as every other entry, for the reason the session
    ledger gives: an append takes the unit's WRITE OWNERSHIP, and while the emitter
    holds a running session's handle a second handle in this process is refused, so a
    ledger that wrote around the emitter would fail for exactly the crews that are
    working. Going through the writer also keeps the entry ordered against the turn
    it was recorded inside.

    The caller establishes that the crew's session has a crew log to write to; this
    is the ordinary ``_write``, so a session without one is a policy no-op here and the
    refusal belongs where the crew can be told about it.
    """
    _write(session_id, "radar/recorded", data, src=_SRC_GATEWAY)


def radar_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``radar/recorded`` entry.

    Asked beside the write rather than inside it, because the caller can act on the
    answer and ``_write`` cannot: an entry over the ceiling by construction can never
    land, so the update it would report as taken never exists. Same serializer, same
    entry type and src as the append, so the two cannot disagree about what fits.
    """
    return _entry_line_fits("radar/recorded", data, src=_SRC_GATEWAY)


def work_entry_fits(data: dict[str, Any]) -> bool:
    """Whether a ``work/recorded`` entry carrying *data* fits one log line.

    The work ledger asks this BEFORE its own store commits, with the widest
    payload the commit can produce, so a mutation whose record could not be
    written is refused whole and no cache byte is touched: the store's caps
    refuse and never truncate, and this keeps that rule for the one bound the
    store cannot see, the line limit.
    """
    return _entry_line_fits("work/recorded", data, src=_SRC_GATEWAY)


def on_panel_published(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One publish of a crew's webview, appended to its DM session log, acknowledged.

    The write half of the crew panel. A publish REPLACES the whole panel, so the
    entry carries the document whole rather than the fields that changed: a panel
    describes one cycle's state, and a partial update would leave last cycle's rows
    beside this cycle's counters with nothing marking which is which. That is the
    one way this differs from the session ledger's entry, whose absent field means
    unchanged.

    WAITS, like ``on_work_recorded``, though not because this record is the panel's
    only one -- the file is, and the route has already written it. It waits so the
    caller learns whether THIS publish's history row landed, and so an append the
    waiter gave up on cannot land later. It returns ``True`` once the writer has
    appended, and ``False`` when the append was refused, permanently dropped, or not
    started within *timeout* seconds. ``False`` is FINAL -- an entry the waiter gave
    up on is abandoned and will not land later even if the writer retries the job.
    Without that, a slow store could report the row missing and commit it anyway, so
    one publish would end up with two history rows. An append already STARTED is waited to
    completion however long the store takes, and its outcome reported truthfully
    rather than guessed.

    Queued through the same writer as every other entry, for the reason the session
    ledger gives: an append takes the unit's WRITE OWNERSHIP, and while the emitter
    holds a running session's handle a second handle in this process is refused, so
    a store that wrote around the emitter would fail for exactly the crews that are
    publishing. Going through the writer also orders the entry against the turn the
    crew published inside.

    *session_id* is the PUBLISHING session's, which is the member's own DM session:
    the panel tool is mounted nowhere else, so the unit this lands in belongs to
    that member's slot and the slug-keyed read finds it without a binding of its
    own. A session with no crew log answers ``False`` here, and the refusal belongs
    where the crew can be told about it.
    """
    if not session_id or not enabled():
        return False
    landed = threading.Event()
    gate = threading.Lock()
    outcome = {"ok": False, "abandoned": False}

    def _job() -> None:
        with gate:
            # A waiter that gave up has abandoned the entry: it must not land later,
            # or a publish the crew was told failed would reappear on the next fold.
            # Under the gate the two outcomes cannot cross.
            if outcome["abandoned"]:
                return
            log = _handle(session_id)
            if log is None:
                return
            entry = log.append("panel/published", data, src=_SRC_GATEWAY)
            # The panel fold spans replacement sessions, and a unit header's clock can
            # step BACKWARD, which would fold a retired session's publish last and make
            # it the current panel with history built against the wrong predecessor.
            # Publish the causal order only after this append has really landed.
            from kiro_crew import session_ledger

            session_ledger.note_panel_unit_recorded("", session_id)
            # AFTER the order is recorded, never before. The fold the wake triggers reads
            # that order to decide which unit applies last, so a wake enqueued first can
            # be folded on a thread that still sees this unit unordered -- and the panel
            # fold takes the newest entry whole, so it would serve a retired session's
            # panel as the current one.
            _note_eager(entry, "panel/published", session_id, data)
            outcome["ok"] = True

    _submit(_job, "appending panel/published", session_id, after=landed.set)
    if landed.wait(timeout):
        return outcome["ok"]
    with gate:
        if outcome["ok"]:
            return True
        outcome["abandoned"] = True
    return False


def panel_entry_fits(data: dict[str, Any]) -> bool:
    """Whether *data* would fit one ``panel/published`` entry.

    Asked beside the write rather than inside it, because the caller can act on the
    answer and ``_write`` cannot: an entry over the ceiling by construction can
    never land, so the panel it would report as published never exists. The store's
    own byte ceiling bounds the payload, and this bounds the one thing the store
    cannot see -- the whole serialized line, envelope included. Same serializer,
    same entry type and src as the append, so the two cannot disagree about what
    fits.
    """
    return _entry_line_fits("panel/published", data, src=_SRC_GATEWAY)


def on_work_recorded(session_id: str, data: dict[str, Any], *, timeout: float = 5.0) -> bool:
    """One work-board mutation, appended to the ACTING session's log, acknowledged.

    *data* is the ``work/recorded`` payload the work ledger already validated
    against its own caps and against the declared type. Unlike the other
    emitters this one WAITS: it returns ``True`` once the writer has appended the
    entry, and ``False`` when the append was refused, permanently dropped, or
    not started within *timeout* seconds. ``False`` is final: an entry the
    waiter gave up on is abandoned and will not land later even if the writer
    retries the job, so the caller's answer and the record cannot diverge. An
    append the writer had already STARTED is waited to completion, however long
    the store takes: its outcome is then reported truthfully rather than guessed.
    The work ledger is a projection of these entries, so its routes report
    success only on ``True``.
    """
    if not session_id or not enabled():
        return False
    landed = threading.Event()
    gate = threading.Lock()
    outcome = {"ok": False, "abandoned": False}

    def _job() -> None:
        with gate:
            # A waiter that gave up has abandoned the entry: it must not land
            # later, or a write the caller was told failed would come back on
            # the next rebuild. Under the gate the two outcomes cannot cross.
            if outcome["abandoned"]:
                return
            log = _handle(session_id)
            if log is None:
                return
            entry = log.append("work/recorded", data, src=_SRC_GATEWAY)
            # The work fold spans replacement sessions, so header wall clocks are
            # not a causal order. Publish only after this append has really landed.
            from kiro_crew import session_ledger

            session_ledger.note_work_unit_recorded(str(data.get("by") or ""), session_id)
            # AFTER the order is recorded, for the reason the panel emitter gives: the
            # fold this wake triggers reads that order.
            _note_eager(entry, "work/recorded", session_id, data)
            outcome["ok"] = True

    _submit(_job, "appending work/recorded", session_id, after=landed.set)
    if landed.wait(timeout):
        return outcome["ok"]
    with gate:
        if outcome["ok"]:
            return True
        outcome["abandoned"] = True
    return False


# --------------------------------------------------------------------------- #
# The crew kind
# --------------------------------------------------------------------------- #
#
# The first writer for ``crew-log/crews/<store>/``. Deliberately NOT routed
# through the write-behind queue above: every structure that queue owns is keyed
# by an ACP SESSION id and ``_handle`` opens its unit with ``_KIND``, so handing
# it a crew's store name would make it look for a SESSION unit under that name
# and, failing to find one, drop the entry as a policy no-op. Threading a kind
# through the batch machinery is a change to the session path, which these two
# entries do not need: a dispatch is written once per work item and a report once
# per milestone, both already off the event loop in the route's worker thread.
#
# No handle is cached either. A handle holds the unit's write lease until it is
# dropped, and a cached crew handle would hold one for the process's life --
# refusing ``remove_unit`` for a unit nothing is writing. Opening per entry costs
# a bounded tail read, which is what the lease's own refcount makes safe to
# repeat.

_KIND_CREW = "crew"

CREW_DISPATCH = "crew/dispatch"
CREW_REPORT = "crew/report"


def crew_src(store: str) -> str:
    """The ``src`` a crew signs its own dispatches with.

    A crew writing into its OWN log is the guest form ``crew:<name>``, and the
    name is the unit's own id -- so this is derived rather than passed, and no
    caller can sign a dispatch as a crew it is not.
    """
    return f"crew:{store}"


def _crew_unit(store: str) -> Any:
    """An open crew log for *store*, created when it has none. ``None`` if inert.

    Unlike :func:`_handle`, this one CREATES. A session's crew log is created by
    the turn path, which knows whether the session is real; a crew's has no such
    moment -- the crew exists in the members store, and the first fact worth
    recording about its work is the first dispatch. So the first append opens the
    file, and a crew that dispatches nothing never gets one.

    ``None`` means the flag is off or the store is unnamed, which is a policy
    no-op. Every other failure is the caller's to treat as "not recorded".
    """
    if not store or not enabled():
        return None
    subsystem = _crew_log()
    if subsystem.CrewLog.exists(_KIND_CREW, store):
        return subsystem.CrewLog.open(_KIND_CREW, store)
    try:
        return subsystem.CrewLog.create(_KIND_CREW, store)
    except subsystem.CrewLogError as exc:
        # Two threads can pass the ``exists`` check together and both create. The
        # loser is told ``already_exists``, which is the file it wanted, so it
        # opens instead of reporting a failure.
        if exc.code != subsystem.CODE_ALREADY_EXISTS:
            raise
        return subsystem.CrewLog.open(_KIND_CREW, store)


def _dispatch_target_ok(data: "Mapping[str, Any]") -> bool:
    """Whether ``target`` names exactly one party, which the registry cannot ask.

    ``target.kind`` decides which of ``slot`` or ``name`` carries the party, and
    the two forms are EXCLUSIVE -- a target names a session slot or a crew, never
    both. A declaration has no spelling for a conditional requirement, so the
    obligation lands here, on the writer, where the entry is built.
    """
    target = data.get("target")
    if not isinstance(target, Mapping):
        return False
    kind = target.get("kind")
    carried = {"session": "slot", "crew": "name"}.get(kind if isinstance(kind, str) else "")
    if carried is None:
        return False
    absent = "name" if carried == "slot" else "slot"
    return bool(target.get(carried)) and absent not in target


def _newest_dispatch_for(log: CrewLog, item: str, start: int) -> "int | None":
    """The seq of the newest ``crew/dispatch`` naming *item* at or after *start*."""
    found: int | None = None
    for entry in log.iter_from(start):
        if entry.type != CREW_DISPATCH:
            continue
        if isinstance(entry.data, Mapping) and entry.data.get("item") == item:
            found = entry.seq
    return found


def _crew_thread(log: CrewLog, item: Any) -> "int | None":
    """The seq of the newest ``crew/dispatch`` for *item*, or ``None``.

    What makes a dispatch and its replies one conversation inside the crew's file.
    Read from the log rather than remembered, because the two writes are separate
    requests -- often in separate processes -- and an in-memory map would answer
    ``None`` for every report after a restart while the anchor sat on disk.

    ONE pass, from seq 1, because a narrower start would not be a cheaper read:
    :meth:`~kiro_crew.crew_log.store.CrewLog.iter_from` walks ``_iter_segments``
    from the first segment and decodes every entry, dropping the ones below its
    *seq* after parsing them. So a "recent entries" window costs the same full
    parse as the whole file, and a window MISS -- the ordinary case for an item
    whose dispatch has aged out -- would pay for that parse twice. A byte-tail
    reader like :func:`~kiro_crew.crew_log.store._anchor_exists`'s is what an
    actual bound would take, and it answers a different question (does this seq
    exist) than this one (which dispatch named this item).

    Reading the whole file is also what correctness wants, though not because a
    miss refuses the write: :func:`on_crew_report` records an unthreaded report
    rather than dropping it. What a miss costs is that the entry becomes
    indistinguishable from one volunteered with no dispatch behind it, which is a
    claim about where the work came from that nothing later can correct -- so every
    anchor the file actually holds is worth finding.

    An unreadable log answers ``None``, so a report still lands.
    """
    if not isinstance(item, str) or not item:
        return None
    try:
        return _newest_dispatch_for(log, item, 1)
    except Exception:  # noqa: BLE001 - an unthreaded report is better than none
        # Rendered text, never ``exc_info``: ``log`` is a live ``CrewLog`` in this frame,
        # so a record carrying the traceback carries this frame, and a handler that keeps
        # records (``caplog``, a ``MemoryHandler``) keeps the handle and its write lease
        # alive past the drop that should have released it. A string keeps no frames.
        # The render uses the ``traceback`` module imported above rather than the store's
        # ``log_exception_text``, because this module is the boot-path import gate (see
        # ``_crew_log``) and may not import the store at module level -- the same idiom
        # ``_record_session_tree_edge`` uses. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "crew log: dispatch anchor lookup failed for %r:\n%s",
                item,
                traceback.format_exc().rstrip(),
            )
        return None


def _crew_append(store: str, entry_type: str, data: dict[str, Any], **envelope: Any) -> int:
    """Append one crew entry and return its seq, or ``0`` when nothing was written.

    BEST EFFORT, and that is a scope decision rather than laxity: the work board's
    own authority is the ``work/recorded`` entry in the acting session's log, which
    its route already refuses to proceed without. This entry is the crew-side
    record of the same fact, so a crew log that cannot be written must not fail the
    ledger write that succeeded -- a caller reads ``0`` as "not recorded" and
    carries on.
    """
    try:
        log = _crew_unit(store)
        if log is None:
            return 0
        return int(log.append(entry_type, data, src=envelope.pop("src"), **envelope).seq)
    except Exception as exc:  # noqa: BLE001 - see the best-effort note above
        _report(f"appending {entry_type} for crew {store!r}", exc, op="crew-append")
        return 0


def on_crew_dispatch(store: str, data: dict[str, Any]) -> int:
    """One work item handed to a target, recorded in the dispatching crew's log.

    The OPENER of the dispatch family: the reports for this item thread onto the
    seq returned here. *data* is the ``crew/dispatch`` payload -- ``item``,
    ``target``, and an optional ``brief`` -- and the registry checks the rest.

    Returns the appended seq, or ``0`` when nothing was written: the flag is off,
    the crew is unnamed, or ``target`` does not name exactly one party.
    """
    if not _dispatch_target_ok(data):
        logger.warning(
            "crew log: refusing a dispatch whose target names no single party (crew=%r)", store
        )
        return 0
    return _crew_append(store, CREW_DISPATCH, data, src=crew_src(store))


def _max_ref_span() -> int:
    """The cited-span cap, read from the module that owns and enforces it.

    ``Ref`` validates a span against ``schema``, so ``schema`` holds the cap and is
    the single name a caller lowers to change it. The package re-exports the cap and
    caches the value on first access (:pep:`562`), which makes the re-export a
    second copy of one number: a reader that goes through the package can hold a
    value the owner does not have. Reading the owner keeps the clamp this function
    applies and the bound ``Ref`` enforces the same number.

    Imported per call, for the reason :func:`_crew_log` gives: this module stays
    free of import-time work. Every caller reaches here with the store already
    open, so the schema module is loaded by then and the lookup is a dict hit.
    """
    from kiro_crew.crew_log import schema

    return int(schema.MAX_REF_SPAN)


def on_crew_report(store: str, data: dict[str, Any], *, cite_unit: str) -> int:
    """One report on a work item, recorded in the DISPATCHING crew's log.

    ``src`` is ``gateway`` rather than a crew guest form: the reporting party here
    is a session, and the gateway is what writes a session's report into the crew's
    file.

    *cite_unit* is the reporting session's crew-log unit, and it is what makes the
    required ``ref`` the writer's obligation rather than the caller's: the span is
    built here, from that unit's own newest seq, so a report cannot be written
    without evidence. The span is clamped to the newest ``MAX_REF_SPAN`` lines,
    which is what the cap is for -- a long run is cited by its relevant span
    rather than in full. A unit with no readable log yields no citation and the
    report is not written, because a report with no ``ref`` is an unfalsifiable
    claim in a file nothing rewrites.

    A report whose dispatch anchor does not resolve is written UNTHREADED rather
    than dropped. The anchor can be missing for two reasons, and one of them does
    not heal: a read that failed transiently leaves the dispatch on disk, so the
    item's next report threads normally, but a dispatch whose own best-effort
    append failed leaves no dispatch entry at all -- and then refusing the reply
    refuses every later report for that item too, so the crew log reads for good
    as though the item was never dispatched. Silence about the work is the worse
    record: it is unbounded in time and invisible, while an unthreaded report
    states that the work happened and is merely missing its link.

    What that costs is worth naming, because it is not free. The spec reads a
    report with no ``thread`` as one volunteered with no dispatch behind it, so an
    unthreaded report here is indistinguishable from a volunteered one -- one
    field is ambiguous, rather than one item's whole history being absent. The
    anomaly is logged when it happens, which is where a reader looks to tell the
    two apart.

    The refusal that remains is the evidence one above: no citable unit means no
    write at all, because a report that cannot be checked is a claim, not a record.

    Returns the appended seq, or ``0`` when nothing was written.
    """
    subsystem = _crew_log()
    try:
        if not cite_unit or not subsystem.CrewLog.exists(_KIND, cite_unit):
            return 0
        last = int(subsystem.CrewLog.open(_KIND, cite_unit).last_seq)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"citing {cite_unit!r} for a crew report", exc, op="crew-report-cite")
        return 0
    if last < 1:
        return 0
    span = _max_ref_span()
    evidence = subsystem.Ref(_KIND, cite_unit, max(1, last - span + 1), last)
    log = None
    try:
        log = _crew_unit(store)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"opening the crew log for {store!r}", exc, op="crew-log-open")
    if log is None:
        return 0
    thread = _crew_thread(log, data.get("item"))
    if thread is None:
        # The OBSERVATION only. What happens next is not known yet: the append below
        # can fail, and its failure goes through ``_report``, which warns once per
        # process and is debug-only afterwards -- so a line claiming the report landed
        # would be the only default-level trace of a write that did not.
        logger.warning(
            "crew log: no dispatch to thread %r onto in crew %r",
            data.get("item"),
            store,
        )
    try:
        entry = log.append(CREW_REPORT, data, src=_SRC_GATEWAY, thread=thread, ref=evidence)
        if thread is None:
            logger.warning(
                "crew log: recorded the report for %r in crew %r unthreaded, so it reads "
                "as volunteered with no dispatch behind it",
                data.get("item"),
                store,
            )
        return int(entry.seq)
    except Exception as exc:  # noqa: BLE001 - see _crew_append's best-effort note
        _report(f"appending {CREW_REPORT} for crew {store!r}", exc, op="crew-report-append")
        return 0


def on_session_closed(session_id: str, reason: str) -> None:
    """Record a session teardown and drop its cached state.

    ``reason`` is the caller's own word for the teardown, recorded verbatim rather
    than remapped onto a second vocabulary. The reset route is the caller today,
    and it passes the same ``end_reason`` it records elsewhere; another teardown
    path passing its own reason needs no change here.

    This entry records a TEARDOWN, not the end of the file. A forced reset -- the
    model-switch route called with ``skip_running`` false -- tears a session down
    while a turn is still running, and that turn's closers are written later, by its
    own ``finally``, in another task; a steer already inside its RPC lands later
    still. So entries belonging to turns that were already in flight MAY follow this
    one, and a reader treats it as "the gateway stopped serving this session for
    reason R" rather than "nothing further appears".

    Holding the entry until the session went quiet was tried and abandoned: nothing
    in the gateway defines quiescence. A turn is pinned only once ``turn/started``
    is written, so an authorization await ahead of it is unpinned; eviction under
    the tracker's live-turn ceiling strands a held reason; and a resume that re-claims the id
    would have to decide whether the predecessor's teardown still happened. Each of
    those is a way to LOSE the terminal, which is worse than a terminal a late entry
    follows -- an absent one cannot be told apart from a crash.

    What this must not do is erase state a live turn still needs. The cleanup below
    drops only what a successor must not inherit; the live-turn records and the open
    tool calls of turns still running are left to their own release, so a turn whose
    session closed under it can still close its calls.
    """

    def _forget() -> None:
        tracker = _tracker_instance
        with _lock:
            # Only when no turn of this session is still running. A forced reset
            # tears a session down MID-TURN, and that turn goes on writing through
            # this handle -- which also carries the unit's write ownership, so
            # dropping it here releases the log to whichever process asks next
            # while the turn is still producing entries, and a successor's repair
            # then closes a turn that completes for real a moment later. A handle
            # left behind is not a leak: it belongs to a live turn, and the
            # capacity rule reclaims it once that turn is gone. With no tracker
            # installed, no turn of this process is live.
            if tracker is not None:
                tracker.pop_unless_live(_open, session_id)
            else:
                _open.pop(session_id, None)
            # A later session reusing this id must write its own configuration
            # rather than inheriting a closed session's as "unchanged".
            _last_config.pop(session_id, None)
            # The creation-failure flag is per SESSION, so it dies with the
            # session rather than living until the next ``reset_caches``: the
            # flag makes every later entry for this id a counted loss, and a
            # successor reusing the id creates its own crew log and must not
            # inherit that verdict. Cleared here rather than on the write path
            # because this runs as terminal cleanup, so it also runs when the
            # closing entry itself was dropped -- which is the case for exactly
            # the sessions the flag is set on.
            _creation_failed.discard(session_id)
        # The writer's overflow tally is per SESSION and is only ever read while that
        # session is writing, so it dies with the session. Left behind, a gateway that
        # runs for weeks keeps one entry per session that ever overflowed; and a
        # successor reusing the id would inherit a count it did not earn.
        writer = _writer_instance
        if writer is not None:
            writer.forget(session_id)
        # The attempt counts go with the session (a resume reseeds them from the file),
        # and so do the open calls and settled markers of turns already gone. A live
        # turn's are its own: dropping its open calls here would leave them open for the
        # life of the file, and a mid-teardown turn still suppresses its own duplicates.
        if tracker is not None:
            tracker.forget_session(session_id)

    _write(
        session_id,
        "session/closed",
        {"reason": reason},
        src=_SRC_GATEWAY,
        after=_forget,
    )


__all__ = [
    "ACTORS",
    "CREW_LOG_ENV",
    "buffered_writes",
    "drain_for_shutdown",
    "dropped_writes",
    "enabled",
    "flush",
    "peak_buffered_writes",
    "on_approval_decided",
    "on_approval_requested",
    "on_compaction_applied",
    "on_context_composed",
    "on_message_queued",
    "on_message_received",
    "on_message_sent",
    "on_model_selected",
    "on_request_configured",
    "on_session_adopted",
    "on_session_closed",
    "on_session_opened",
    "on_session_released",
    "on_step_completed",
    "on_step_started",
    "on_tool_called",
    "on_tool_completed",
    "on_turn_completed",
    "on_turn_failed",
    "on_turn_refused",
    "on_turn_started",
    "reset_caches",
    "slot_previous_store",
]

# The graceful path is the gateway's own cleanup hook, which drains in a thread
# before the process winds down. The backstop for every other exit -- a CLI run, a
# cron subprocess, a signal the server never sees -- is registered by
# ``_ensure_shutdown_hook`` on the first drain pass rather than here, so a launch
# with the flag unset registers nothing at all. See that function for why first
# use still puts this handler behind the executor's own.


def _record_session_tree_decision(
    session_id: str,
    slot: str,
    entry: Any,
    parent_slot: "str | None",
) -> None:
    """Fold a just-committed ``session/adopted`` or ``session/released`` into the
    in-memory session tree. ``parent_slot`` of ``None`` is the release.

    Called immediately AFTER the append succeeded, for the reason
    :func:`_record_session_tree_edge` is: the tree is a projection that applies deltas
    and never rescans, so this line is what makes a takeover visible without waiting
    for a cold start.

    *entry* is what ``append`` returned, so its ``seq`` and ``time`` are the values ON
    DISK. ``seq`` is what orders the decision, and taking it from the written line is
    what makes the live fold and a cold replay of that same line agree. Reading a clock
    here instead would order the fold by a moment the log does not record.

    Never raises, and never logs at a level an operator has to act on: the append has
    already succeeded, so the record is safe on disk whatever happens here, and a missed
    fold is recovered by the projection's tail replay on the next cold start.
    """
    try:
        from kiro_crew.crew_log.session_tree_projection import record_adopted, record_released

        raw = getattr(entry, "time", 0)
        at = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        raw_seq = getattr(entry, "seq", 0)
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
        if parent_slot:
            record_adopted(session_id, slot, at, parent_slot, seq)
        else:
            record_released(session_id, slot, at, seq)
    except Exception:  # pragma: no cover -- defensive; both doors guard themselves
        # Rendered text, never ``exc_info``, for the reason
        # :func:`_record_session_tree_edge` spells out: this frame names no handle, but
        # its CALLER is the writer job, which binds ``log`` -- and a retained traceback
        # reaches that frame through ``tb_frame.f_back``, so a handler that keeps records
        # would keep the handle and its write lease. Same ``traceback`` idiom, for the
        # same import-gate reason. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "session tree projection not advanced for %s:\n%s",
                session_id,
                traceback.format_exc().rstrip(),
            )


def _record_session_tree_edge(
    session_id: str,
    slot: str,
    log: CrewLog,
    parent_slot: str | None,
    superseded: str | None,
) -> None:
    """Fold a just-committed ``session/opened`` into the in-memory session tree.

    Called immediately AFTER the append succeeded, which is the whole point: the
    session tree is a projection (:mod:`kiro_crew.crew_log.session_tree_projection`)
    that applies deltas and never rescans, so without this line a reader would be
    back to re-deriving the tree from the whole store on every poll.

    The record is built from what was just WRITTEN, not from a re-read of it: the
    values are the emitter's own, and reading the entry back would be the disk access
    this design exists to remove.

    ``created_at`` comes from the log's immutable header, through the same
    ``getattr(..., "created_at", 0)`` idiom :mod:`kiro_crew.crew_log.read` uses on the
    same object. It only orders a slot's several records inside the fold, so a header
    that cannot answer costs ordering, never an edge.

    Never raises, and never logs at a level an operator has to act on: the append has
    already succeeded, so this session's history is safe on disk whatever happens here,
    and a missed record is recovered by the projection's tail replay on the next cold
    start. Raising would turn a bookkeeping miss into a failed session open.
    """
    # The body is the docstring and this ONE try: nothing sits outside the guard, so
    # nothing can raise into the writer job (pinned by the projection tests).
    try:
        from kiro_crew.crew_log.session_tree_projection import record_opened

        created_at = 0
        try:
            # ``header`` is a PROPERTY, not a method. Calling it raised TypeError, the
            # outer handler swallowed that, and every edge folded with created_at 0 --
            # which orders the tree wrong. Pinned by the projection test below.
            header = log.header
            raw = getattr(header, "created_at", 0)
            if isinstance(raw, int) and not isinstance(raw, bool):
                created_at = raw
        except Exception:
            # An unreadable header orders nothing and blocks nothing.
            created_at = 0
        record_opened(session_id, slot, created_at, parent_slot, superseded)
    except Exception:  # pragma: no cover -- defensive; record_opened guards itself
        # Rendered text, never ``exc_info``: ``log`` is a live ``CrewLog`` in this frame,
        # and a record carrying the traceback carries this frame, so a handler that keeps
        # records (``caplog``, a ``MemoryHandler``) keeps the handle and its write lease
        # alive past the drop that should have released it. A string keeps no frames.
        # The store's ``log_exception_text`` does exactly this, but this module is the
        # boot-path import gate (see ``_crew_log``) and may not import the store at module
        # level, so the render uses the ``traceback`` module already imported above --
        # the same idiom ``_report`` uses. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "session tree projection not advanced for %s:\n%s",
                session_id,
                traceback.format_exc().rstrip(),
            )
