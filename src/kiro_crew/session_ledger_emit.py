"""Write the ACP turn lifecycle to an append-only per-session ledger.

See ``docs/system-specs/modules/session-ledger-emitter.md`` for the contract this
module implements. In short: each entry point is a one-line call at a site in the
dashboard chat path where a lifecycle fact is already known, and every one of
them is **fail-soft** -- a ledger error is reported once and then swallowed, so a
broken ledger can never break a turn.

The whole module is inert unless ``KIROCREW_SESSION_LEDGER`` is truthy. With the
flag off nothing is created and every call returns immediately.

The grouping identity of a session entry is ``data.turn`` -- the runner's own turn
ordinal -- and, inside a turn, ``data.step``, the MODEL CALL the entry belongs to.
A tool entry also carries ``data.call_index``, its position among that turn's tool
calls, which is what ``step`` meant before a model call had a name of its own: one
model call can issue several tools at once, so the step cannot order them. All are
known at EMIT time, so an entry answers "what happened in this turn" on its own,
with nothing to look up and no earlier line to resolve against.

The envelope's ``thread`` field is left unset here. ``thread`` points at another
LINE's ``seq``, which only a unit whose anchor line is written before the entries
citing it can supply; that is the crew ledger's shape, not this one's.

Three things are recorded differently from a naive reading of the lifecycle, all
deliberate and all explained in the spec:

* ``compaction/applied`` carries context-usage **percentages**, because the
  compaction boundary never learns a raw token count.
* A tool call and an approval are identified by an id inside ``data``, not by
  ``ref``. A ``Ref`` is a citation of another ledger's lines, and a tool call id
  names a frame on the ACP stream, which is not a ledger unit.
* A turn that ends without its terminal event is still CLOSED, by the turn's own
  ``finally``: ``stop_reason: "failed"``, an ``error`` naming the exception class
  when one was caught, and no ``tokens`` or ``credits``, because none were
  measured. Leaving the start open would say the writer died, which this process
  being alive contradicts -- and nothing here would correct it, since the
  interrupted-turn repair is opt-in and only a resume asks for it. A recovery
  re-entry anchors its own thread carrying ``depth``.

**Storage never runs on the caller's thread when that thread is the event
loop.** Every entry point is called from the dashboard's async chat path, and
the storage call underneath takes the unit's lock, reads a bounded tail to
assign ``seq`` and ``fsync``s the appended line -- a waiting ``flock`` and a
kernel ``fsync`` once per tool frame and once per turn, on the one loop that
also drives the liveness heartbeat. So an entry point does only what must be
measured where it is called and hands the storage call to
:func:`kiro_crew.executors.ledger_executor`, whose single worker drains the work
in the order the call sites produced it.

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
written before the entries that cite it; that is the crew ledger's shape, not
this one's.

Two rules govern the writer, and neither gives way to the other: a lifecycle
record is never dropped for BACKPRESSURE, because a hole in an append-only log is
permanent and silent, and the event loop is never BLOCKED, because a turn must not
wait on a disk. So a producer appends to an in-memory buffer and returns, and one
worker drains it in batches. What gives under a filesystem that has stopped
keeping up is MEMORY -- reported at a high-water mark, never shed. Fail-soft
governs ERRORS, not backpressure.

A failed append is RETAINED, not swallowed. The batch goes back to the front of
its session's bucket, every later write for that session queues behind it, and the
writer retries with a short doubling backoff. Bounded, because entries live in
memory until they are written: past :data:`_MAX_WRITE_ATTEMPTS` consecutive failed
passes the batch is dropped and counted in :func:`dropped_writes`, so a wedged
disk becomes a reported loss instead of a wait no bounded caller can finish. A
REFUSAL -- a ``LedgerError``, decided before any byte is written -- is not retried
at all: it would be refused identically every time, and retrying it would hold
that session's whole log behind one entry that can never land.

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
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.constants import env_flag_enabled
from kiro_crew.executors import ledger_executor
from kiro_crew.ledger import MAX_ENTRY_BYTES, Ledger, LedgerError
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: Turning this on is a separate change from landing the emitter.
SESSION_LEDGER_ENV = "KIROCREW_SESSION_LEDGER"

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

#: The label a block with no marker of its own is reported under. ``split_blocks``
#: classifies by opening marker, and three blocks the design names -- steering,
#: tool specs, injected ledger context -- have none, so their characters land in
#: its unclassified bucket. Renaming that bucket here keeps the entry honest
#: about being a remainder rather than inventing three zeroed sources.
_OTHER_SOURCE = "other"
_UNCLASSIFIED_LABELS = frozenset({"unclassified", ""})

#: Who caused a turn to run. Every value names a STRUCTURAL producer the
#: dispatch layer identifies; ``user`` means a person typed the message, and
#: ``gateway`` means the gateway synthesized it and no narrower producer fits
#: (a task-runner summary, an orchestrator stage). Anything unrecognised is
#: recorded as ``other`` rather than guessed.
ACTORS = frozenset({"user", "crew", "cron", "autonudge", "subagent", "gateway", "other"})

#: Bounded so a long-lived gateway cannot grow any map without limit.
_MAX_OPEN_LEDGERS = 128
_MAX_PENDING_TOOLS = 512

#: A last-resort ceiling on live-turn records, set far above any plausible number
#: of concurrent turns. Reaching it does not mean the gateway is busy; it means
#: turns are ending without their terminal event, so nothing releases their
#: state. At that point unbounded growth is the worse failure, so the oldest
#: records are dropped and the leak is reported at error level.
_MAX_LIVE_TURNS = 4096


@dataclass
class _LiveTurn:
    """What a turn in flight owns. Created at its start, dropped at its end.

    Two counters, and the difference between them is the point.

    ``step`` numbers the turn's MODEL CALLS. One turn is several: the model
    speaks, calls tools, and is called again with their results. It is minted by
    :func:`on_step_started` and every entry produced inside that call carries it,
    so a reader can ask what one model call cost without inferring boundaries.

    ``call_index`` numbers the turn's TOOL CALLS, which is what this record's
    single counter meant before a step existed. It is kept because it answers a
    different question -- the order the runner issued the calls in -- and because a
    step can issue several tools at once, so the step alone cannot order them.

    One record rather than a map per counter: two structures can be evicted
    separately, and losing either counter mid-turn restarts its numbering so two
    entries claim one ordinal. One record cannot be half-dropped.
    """

    step: int = 0
    call_index: int = 0


@dataclass
class _RetryState:
    """A session's retained batch: how many passes have failed, and when to retry.

    ``attempts`` counts CONSECUTIVE failed passes over this session's owed
    entries, and any append that lands resets it. Counting it that way is what
    makes the retry terminate: a reset costs a real append, so the buffer strictly
    shrinks between resets and an alternating failure cannot retry forever. It
    also means the budget bounds a WEDGED writer rather than a slow one -- a pass
    that wrote something is not wedged, however much is still owed.

    ``not_before`` is the monotonic instant the next pass may claim the bucket. It
    is per session, so a wedged ledger's backoff paces only its own entries.
    """

    attempts: int = 0
    not_before: float = 0.0


#: How long the writer pauses before a drain pass, so one pass takes a turn's
#: burst rather than waking per entry. Fixed, so the worst case a producer can
#: impose on the file is a constant.
_BATCH_DEADLINE_SECONDS = 0.02
#: Buffered appends past which the backlog is reported once. NOT a cap: nothing
#: is dropped for crossing it. It exists so a filesystem that has stopped keeping
#: up shows up in the log as memory pressure instead of silently accumulating.
_PENDING_HIGH_WATER = 4096

#: Per-session ceiling on unwritten entries, and the one place this module sheds
#: memory. Reaching it means one session's appends are not landing at all while its
#: producers keep making them -- the shape a HUNG write takes, where the attempt
#: counter that bounds every other failure never advances because the call never
#: returns. From the producer side a filesystem that never answers is
#: indistinguishable from a slow one, so the only available bound is a ceiling.
_MAX_PENDING_PER_SESSION = 20_000
#: The same ceiling in serialized bytes, for the case where few entries are huge
#: rather than many being small. Whichever is reached first applies.
_MAX_PENDING_BYTES_PER_SESSION = 32 * 1024 * 1024

#: How long one write may take before the writer says so. A job past this is not
#: failed -- it may still land -- but nothing else for that session can proceed, so
#: silence here is what a stuck filesystem looks like from the outside.
_WRITE_STALL_SECS = 30.0

#: How long a synchronous caller waits for an in-flight writer batch before it
#: hands its job over instead of writing inline. Bounded because the caller is a
#: real thread doing real work, and generous because the alternative -- writing
#: beside a claimed batch -- reorders the file rather than merely delaying it.
_INLINE_ORDER_SECONDS = 5.0

#: How long shutdown waits for the writer to finish. Bounded so a wedged
#: filesystem delays exit rather than hanging it.
_SHUTDOWN_DRAIN_SECONDS = 5.0
#: Floor on the gap between shutdown retry passes. Without it a budget already
#: nearly spent would spin the remaining attempts away in microseconds, which is
#: how a filesystem that needed a moment becomes a permanent hole in the log.
_MIN_SHUTDOWN_RETRY_GAP = 0.01
#: Budget for the second inline attempt, after the timed wait on the writer has
#: already spent the caller's timeout. Short on purpose: exit must not be delayed
#: twice over for the same batch.
_SECOND_CHANCE_DRAIN_SECONDS = 0.5

#: How long the writer waits before retrying a batch whose append raised, and the
#: ceiling that wait doubles to. The errors worth retrying are the transient ones
#: -- an ENOSPC a rotation clears, an EIO on a network mount -- which resolve on a
#: timescale a short backoff covers. The wait is per SESSION, so one wedged ledger
#: paces only itself and another session's entries keep flowing.
_RETRY_BACKOFF_SECONDS = 0.05
_RETRY_BACKOFF_MAX_SECONDS = 2.0

#: How many consecutive failed passes a session's owed batch survives before it
#: is DROPPED and counted. There has to be such a number: entries live in memory
#: until they are written, so a filesystem that never answers would otherwise hold
#: them forever and every bounded caller -- :func:`flush`,
#: :func:`drain_for_shutdown` -- would time out instead of returning. A loss that
#: is counted and named can be investigated; a hang cannot.
_MAX_WRITE_ATTEMPTS = 6

_lock = threading.Lock()
#: Signals a change to the writer's state. It has its OWN lock, deliberately NOT
#: :data:`_lock`: a condition sharing that lock makes every waiter and notifier
#: hold the same non-reentrant mutex that guards the maps, so one helper called
#: from inside a ``with`` block on it self-deadlocks. Here the two concerns are
#: separate -- state is mutated under ``_lock`` and the wake is delivered outside
#: it, by :func:`_notify` -- and a waiter's predicate takes ``_lock`` for itself.
#: There is no lost wakeup: a notifier must acquire this lock to signal, and
#: ``wait_for`` holds it across both the predicate and the wait.
_drained = threading.Condition()
_open: "OrderedDict[str, Any]" = OrderedDict()
#: (session, turn) -> the state that turn OWNS while it is in flight, created at
#: ``turn/started`` and dropped by that turn's own end. The step counter lives
#: HERE, in the same record as the pin, rather than in a map of its own: two
#: structures can be evicted separately, and losing the counter while the turn
#: still runs restarts the numbering so two of its entries claim one ordinal.
#: One record cannot be half-dropped.
_live: "OrderedDict[tuple[str, int], _LiveTurn]" = OrderedDict()
#: session -> how many of its turns are live. Derived from :data:`_live` and
#: maintained with it under the same lock, so the two cannot drift. Read by the
#: eviction rule, which needs the question answered per SESSION: a handle is
#: per session, while a step ordinal is per turn.
_pinned: "dict[str, int]" = {}
#: (session, call_id) -> (started_monotonic, name, server, call_index, step).
#: The completion frame repeats none of this, so the call frame's identity, its
#: position among the turn's calls and the model call it belonged to are all
#: remembered here and filled in when it closes.
#: call key -> (began, name, server, call_index, step, turn). The TURN is part
#: of the value because a closer must name the turn the call was OPENED in: a
#: call left open by one turn and closed under the next one's ordinal is a
#: false statement about a turn that never used the tool.
_tool_started: "OrderedDict[tuple[str, str], tuple[float, str, str, int, int, int]]" = OrderedDict()
#: session -> the appends waiting to be written, in the order they were made.
#: Producers only ever append here; the writer prepends a batch it could not
#: write. Keyed per session because each session is a separate file, so one slow
#: ledger cannot reorder another's entries -- and because a retained batch has to
#: hold back exactly the entries that belong AFTER it, which is that session's
#: bucket and nothing else. A bucket is popped whole and is never left empty.
#: A pending job is ``(job, what, nbytes)``. ``nbytes`` is an O(1) size hint the
#: producer already had -- the body length for a body-bearing entry, 0 otherwise --
#: carried on the record so a RETAINED batch keeps its contribution to the byte
#: ceiling instead of being re-measured, which nothing here can do after the fact.
_PendingJob = "tuple[Callable[[], None], str, int]"
_pending: "OrderedDict[str, list[tuple[Callable[[], None], str, int]]]" = OrderedDict()
_pending_count = 0
#: The most ever buffered at once, for a caller that wants to see the backlog.
_pending_high_water = 0
#: session -> its retained batch's state, present only while one is owed. Its
#: presence is also what disables the inline fast path for that session, so a
#: later write cannot overtake the entries it is queued behind.
_retry: "dict[str, _RetryState]" = {}
#: Sessions a synchronous caller is writing inline for right now. The writer does
#: not claim a bucket in here: ``flock`` serializes two appends but does not order
#: them, so claiming beside an inline write is the reordering the inline gate
#: exists to prevent.
_inline: "set[str]" = set()
#: How many appends were dropped after their attempt budget was spent.
_dropped_count = 0
#: Sessions whose loss has already been reported, so a wedged disk is named once
#: rather than once per batch. Cleared by that session's next successful append,
#: which is what makes the recovery its own single line.
_dropped_reported: "set[str]" = set()
#: Sessions whose pending ceiling has been reported, so a wedged one names itself
#: once instead of per shed entry. Cleared by :func:`_note_progress`, which also
#: logs the recovery.
_ceiling_reported: "set[str]" = set()
#: Approximate serialized size of each session's unwritten entries.
_pending_bytes: "dict[str, int]" = {}
#: The write currently in progress: when it started, and what it was. A job that
#: HANGS never returns, so the writer cannot report on itself -- producers read this
#: and say so instead. 0.0 means no write is in flight.
_inflight_since: float = 0.0
_inflight_what: str = ""
_stall_reported = False
#: True while a drain pass is scheduled or running, so a producer wakes the
#: writer once rather than per entry. Never read directly: read
#: :func:`_writer_busy_locked`, which also answers the case where the pass this
#: flag was set for can never run.
_draining = False
#: The future :func:`_start_drain` submitted for the current pass, or None. Held
#: because the flag alone cannot say whether the pass is still coming: shutting
#: the pool down CANCELS a queued future, and the flag would then stay set for the
#: life of the process -- making every ``flush`` and ``drain_for_shutdown`` return
#: False and, worse, making the exit path's inline fallback refuse to write on the
#: belief that a batch is claimed. That loses exactly the entries the drain exists
#: to save.
_drain_future: "Any | None" = None
#: True once shutdown has asked for quiescence: the writer stops pausing to batch
#: and writes what it has as fast as it can.
_draining_for_shutdown = False
#: Set to make the writer thread abandon its current inter-pass wait and recompute
#: it. Without this the pause is a plain sleep: a writer that parked for the length
#: of a retry backoff cannot learn that a shutdown has since asked for quiescence,
#: so clamping the backoff would never reach the thread that honours it.
_wake = threading.Event()
#: When a bounded shutdown drain is running, the monotonic instant it gives up at.
#: A retained batch parked BEYOND it is retried just before it instead of never:
#: a retry schedule outliving its process does not defer a write, it loses it.
_shutdown_deadline: float = 0.0
#: When that drain began. The pair defines the budget, and the retry instants are
#: absolute points inside it -- a rolling "now + slice" is always in the future and
#: would never become claimable at all.
_shutdown_started: float = 0.0
#: session -> the last request configuration written for it, as a comparable
#: tuple. ``request/configured`` is a CHANGE record: rewriting an identical
#: configuration every turn would bury the turns where it actually moved, which
#: is the only thing a reader wants from it. Keyed per session, not per turn,
#: because the configuration outlives a turn.
_last_config: "OrderedDict[str, tuple[Any, ...]]" = OrderedDict()
#: session -> {turn ordinal -> the highest attempt opened at it}. A regenerate or
#: a rewind reruns a turn the ordinal already names, so without this two starts
#: at one ordinal are indistinguishable and a fold cannot tell a retry from a
#: duplicate write. Seeded from the file on resume, so a restart between two
#: retries does not reset the count and claim attempt 1 twice.
_attempts: "OrderedDict[str, dict[int, int]]" = OrderedDict()
_warned = False
_warned_high_water = False


def enabled() -> bool:
    """True when the emitter should write. Read per call, never cached."""
    return env_flag_enabled(SESSION_LEDGER_ENV)


def _notify() -> None:
    """Wake every waiter on the writer's state. Called WITHOUT ``_lock`` held."""
    with _drained:
        _drained.notify_all()


def _writer_busy_locked() -> bool:
    """Whether a drain pass is really coming or running. ``_lock`` held.

    ``_draining`` says a pass was SCHEDULED, which is not the same claim. Shutting
    the writer pool down cancels a queued future, and the drain loop that would
    have cleared the flag then never runs -- so the flag alone reports a batch in
    flight forever. Deriving the answer from the future instead makes the state
    self-healing: the next producer starts a fresh pass, and until then every
    barrier reads the truth.

    Pure on purpose. Clearing the flag here would make a waiter's predicate mutate
    shared state, and a second waiter would still sit until its own timeout because
    nothing notified it. Reporting instead leaves the correction to
    :func:`_mark_draining_locked`, which assigns a new flag AND a new future
    together.
    """
    if not _draining:
        return False
    return _drain_future is None or not _drain_future.done()


def _quiet() -> bool:
    """True when nothing is owed and no batch is in flight. Takes ``_lock``."""
    with _lock:
        return not _pending and not _writer_busy_locked()


def flush(timeout: float = 5.0) -> bool:
    """Wait until no append is queued. True when the queue drained in time.

    For a caller that must read the file it just wrote -- a test, a shutdown
    path -- since an entry point returns as soon as the work is HANDED to the
    writer. Never called from the turn path: waiting there would reintroduce the
    block this queue exists to remove.

    Terminates even against a filesystem that never answers: a batch the writer
    cannot write is retried a bounded number of times and then dropped, so the
    buffer reaches empty rather than holding entries no wait could ever satisfy.
    """
    with _drained:
        return _drained.wait_for(_quiet, timeout=timeout)


def dropped_writes() -> int:
    """How many appends were given up on after their attempt budget was spent.

    Normally 0, and a non-zero reading is a real hole in one or more session logs:
    the writer could not append those entries and stopped trying. It is counted
    rather than merely logged because the alternative to a bounded loss is an
    unbounded wait -- entries live in memory until they are written, so a wedged
    filesystem would otherwise hold them forever and make every bounded caller
    time out. A loss a caller can read is the lesser failure, and this is how it
    is read.
    """
    with _lock:
        return _dropped_count


def buffered_writes() -> int:
    """How many appends are waiting on the writer right now."""
    with _lock:
        return _pending_count


def peak_buffered_writes() -> int:
    """The most appends ever waiting at once since the last reset."""
    with _lock:
        return _pending_high_water


def _retry_delay(attempts: int) -> float:
    """How long to wait before retry number *attempts* + 1. Seconds.

    Its own function rather than an expression inline in :func:`_retain` so the
    SCHEDULE is one named thing: a test shrinks it to zero and drives the whole
    attempt budget deterministically, instead of waiting out real backoff under
    load and asserting on a stopwatch.
    """
    return min(
        _RETRY_BACKOFF_SECONDS * 2 ** (max(1, attempts) - 1),
        _RETRY_BACKOFF_MAX_SECONDS,
    )


def reset_caches() -> None:
    """Drop cached handles, live turns and timings. Tests and restart.

    Drains first: a job still holding a stale handle would otherwise write after
    the reset that was meant to forget it.

    Whatever the drain could not write is then DISCARDED rather than carried
    across, and so is the retry state behind it, and so is the claim on the writer.
    Reaching that point means the writer is wedged, and the handles those jobs
    would have written through are being dropped in this same call -- so keeping
    them would leave the writer retrying entries against a ledger this process has
    stopped believing in. Clearing the claim matters as much as clearing the
    buffer: a claim nothing will ever release makes every later barrier report a
    batch in flight, so one wedged pass would otherwise fail every ``flush`` for
    the life of the process. A successful drain makes all of it a no-op, which is
    every call that is not recovering from a wedge.
    """
    global _warned, _warned_high_water, _pending_high_water, _draining_for_shutdown
    global _shutdown_deadline, _shutdown_started
    global _inflight_since, _inflight_what, _stall_reported
    global _pending_count, _dropped_count, _draining, _drain_future
    # Bounded well below the default: this wait exists so a job holding a stale
    # handle cannot write after the reset, and it returns the instant the writer is
    # quiet. When the writer is GONE -- the pool shut down with work still owed --
    # no job can run at all, so waiting the full default would stall every caller
    # for nothing and the discard below is the answer either way.
    flush(timeout=2.0)
    with _lock:
        _open.clear()
        _live.clear()
        _pinned.clear()
        _tool_started.clear()
        _last_config.clear()
        _attempts.clear()
        _pending.clear()
        _pending_count = 0
        _retry.clear()
        _inline.clear()
        _dropped_reported.clear()
        _ceiling_reported.clear()
        _pending_bytes.clear()
        _inflight_since = 0.0
        _inflight_what = ""
        _stall_reported = False
        _dropped_count = 0
        _pending_high_water = 0
        _draining = False
        _drain_future = None
        _warned = False
        _warned_high_water = False
        _draining_for_shutdown = False
        _shutdown_deadline = 0.0
        _shutdown_started = 0.0
        _wake.clear()
    _notify()


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


def _report(what: str, exc: BaseException) -> None:
    """Report a ledger failure once at warning level, then stay quiet."""
    global _warned
    with _lock:
        first = not _warned
        _warned = True
    if first:
        logger.warning(
            "session ledger writes are failing (%s: %s%s); further failures "
            "are logged at debug only",
            what,
            exc,
            f", code={getattr(exc, 'code', '')}" if getattr(exc, "code", "") else "",
        )
    else:
        logger.debug("session ledger %s failed", what, exc_info=True)


def _on_event_loop() -> bool:
    """True when this thread is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _permanent(exc: BaseException) -> bool:
    """Whether retrying *exc* is pointless because it will be refused again.

    A :class:`~kiro_crew.ledger.LedgerError` is a REFUSAL, not a failure: the
    storage layer checks the entry against the format and declines it before any
    byte is written, so the file is byte-identical and the same entry is declined
    the same way every time. Retaining one would spend the whole retry budget on a
    verdict that cannot change, and hold every later entry of that session behind
    it while doing so -- turning one refused entry into a stall for the log it is
    the only casualty of. So a refusal is a loss immediately, counted and named
    like any other; only an error that MIGHT clear -- an ENOSPC, an EIO, a
    filesystem that stopped answering, which is what this retention exists for --
    is retried.
    """
    return isinstance(exc, LedgerError)


def _run_job(job: Callable[[], None], what: str) -> BaseException | None:
    """Run one storage job. Never raises. Returns the failure, or None.

    The exception is returned rather than a bare False because the caller's next
    decision depends on WHICH failure it was: a refusal is a loss now, and
    anything else is retried. It is already reported by the time it comes back.
    """
    global _inflight_since, _inflight_what, _stall_reported
    with _lock:
        _inflight_since = time.monotonic()
        _inflight_what = what
    try:
        job()
    except Exception as exc:
        _report(what, exc)
        return exc
    finally:
        with _lock:
            _inflight_since = 0.0
            _inflight_what = ""
            # Armed again, so a SECOND stall is reported rather than swallowed as a
            # repeat of the first.
            _stall_reported = False
    return None


def _note_stall_if_any() -> None:
    """Say once that the write in flight has been running too long.

    Called by producers, because the thread that would notice is the one blocked in
    the call. A stalled job is not a failed one -- it may still land, and its
    retry budget has not moved, since nothing raised -- so this reports and does
    nothing else. It is the operator's signal for the state the pending ceiling is
    silently absorbing.
    """
    global _stall_reported
    with _lock:
        since = _inflight_since
        what = _inflight_what
        if not since or _stall_reported:
            return
        if time.monotonic() - since < _WRITE_STALL_SECS:
            return
        _stall_reported = True
        held = _pending_count
    logger.error(
        "session ledger writer stalled: %s has been in progress for over %.0fs and "
        "has neither returned nor failed; %d append(s) are waiting behind it",
        what,
        _WRITE_STALL_SECS,
        held,
    )


def _submit(job: Callable[[], None], what: str, session_id: str, nbytes: int = 0) -> None:
    """Hand *job* to the writer. Returns without waiting and without writing.

    The two rules this satisfies together, which the queue-with-a-cap could not:

    A lifecycle record is never dropped for BACKPRESSURE. A hole in an append-only
    log is permanent and silent -- a reader cannot tell a turn that never completed
    from one whose completion was discarded -- so there is no cap on the buffer past
    which an entry is thrown away, however far behind the writer falls. An entry is
    given up on only when the filesystem refuses it or keeps refusing to take it,
    and then it is counted in :func:`dropped_writes` rather than lost quietly.

    The event loop is never BLOCKED. A producer appends to an in-memory buffer and
    returns; it never waits for the writer and never touches the filesystem. So a
    disk that has stopped keeping up costs memory, which is visible and reported,
    instead of turn latency, which stalls the gateway.

    The buffer is therefore bounded by memory rather than by policy, and crossing
    the high-water mark is reported rather than acted on: shedding records for
    being numerous is the thing the first rule forbids.

    With no event loop running the write happens inline. That is not a fallback
    on the loop path -- there is no loop to protect, the caller is a thread that
    asked for this, and it keeps a synchronous caller and the test suite
    deterministic. It is ORDERED against the writer in both directions.
    ``flock`` stops two appends from interleaving but does not decide which lands
    first, so writing here while the writer holds a claimed batch could put this
    entry at a lower seq than one emitted before it -- and a log whose seq
    disagrees with causality is worse than a short one, because a fold cannot
    detect it. So the inline path is taken only for a session that owes nothing:
    no retained batch, no buffered entry, no other inline write. And an inline
    write that FAILS is retained like any other, never swallowed -- otherwise a
    synchronous caller's entry would be the one kind this writer silently loses.
    """
    if not _on_event_loop():
        if _owes_entries(session_id):
            # This session already owes entries this one belongs after, so there is
            # nothing to wait for: queueing behind them keeps the order and costs
            # the caller nothing, while waiting for the debt to clear would put a
            # real thread behind the filesystem for as long as the debt lasts.
            _buffer(session_id, job, what, nbytes)
            return
        with _drained:
            _drained.wait_for(lambda: _inline_ready(session_id), timeout=_INLINE_ORDER_SECONDS)
        if _claim_inline(session_id):
            try:
                failure = _run_job(job, what)
                if failure is None:
                    _note_progress(session_id)
                elif _permanent(failure):
                    _drop(session_id, [(job, what, nbytes)])
                else:
                    _retain(session_id, [(job, what, nbytes)])
            finally:
                _release_inline(session_id)
            return
        # The writer is mid-batch. Hand the job over rather than race it: the entry
        # then waits, which is visible in the buffer count and recoverable at
        # shutdown, instead of landing out of order, which is neither.
        _buffer(session_id, job, what, nbytes)
        return
    _buffer(session_id, job, what, nbytes)


def _owes_entries(session_id: str) -> bool:
    """Whether this session already has entries queued or retained. Takes ``_lock``."""
    with _lock:
        return session_id in _pending or session_id in _retry


def _inline_ready(session_id: str) -> bool:
    """Whether an inline write for *session_id* may go ahead. Takes ``_lock``."""
    with _lock:
        return _inline_claimable_locked(session_id)


def _inline_claimable_locked(session_id: str) -> bool:
    """The inline path's whole precondition. ``_lock`` held.

    Every clause is an ordering rule. ``_draining`` covers a batch the writer may
    already hold, for any session, because a claimed batch is absent from
    ``_pending`` and cannot be seen there. ``_pending`` and ``_retry`` cover this
    session's own owed entries, which this one belongs after. ``_inline`` covers
    another synchronous caller already writing for it.
    """
    return (
        not _writer_busy_locked()
        and session_id not in _pending
        and session_id not in _retry
        and session_id not in _inline
    )


def _claim_inline(session_id: str) -> bool:
    """Take the inline slot for *session_id*, re-checking under one lock hold.

    The wait and the claim cannot be one operation -- the predicate reads ``_lock``
    while the wait holds the condition's own lock -- so the decision is made again
    here, atomically, and the claim is published in the same hold. Without that
    re-read a producer could start the writer in the window between the two.
    """
    with _lock:
        if not _inline_claimable_locked(session_id):
            return False
        _inline.add(session_id)
        return True


def _release_inline(session_id: str) -> None:
    """Give the inline slot back and wake the writer, which may have been waiting."""
    with _lock:
        _inline.discard(session_id)
    _notify()


def _buffer(session_id: str, job: Callable[[], None], what: str, nbytes: int = 0) -> None:
    """Append one job to its session's pending buffer and wake the writer.

    Refuses past a per-session ceiling, which is the ONLY place this module sheds
    memory -- see :data:`_MAX_PENDING_PER_SESSION` for why a hung write leaves no
    other bound. The NEWEST entry is the one dropped: the entries already buffered
    are older facts, and a log missing its most recent line is a shorter record,
    while one missing a line in the middle is a false one.
    """
    global _pending_count, _pending_high_water
    with _lock:
        held = _pending.setdefault(session_id, [])
        held_bytes = _pending_bytes.get(session_id, 0)
        if (
            len(held) >= _MAX_PENDING_PER_SESSION
            or held_bytes + nbytes > _MAX_PENDING_BYTES_PER_SESSION
        ):
            shed = (job, what, nbytes)
            first_shed = session_id not in _ceiling_reported
            _ceiling_reported.add(session_id)
        else:
            shed = None
            first_shed = False
            held.append((job, what, nbytes))
            _pending_bytes[session_id] = held_bytes + nbytes
            _pending_count += 1
            if _pending_count > _pending_high_water:
                _pending_high_water = _pending_count
        over = _pending_count >= _PENDING_HIGH_WATER and not _warned_high_water
        start_writer = not _writer_busy_locked()
        if start_writer:
            _mark_draining_locked()
    if shed is not None:
        _drop_at_ceiling(session_id, [shed], first_shed)
        # The ceiling is reached by writes not landing, and a hung one is the shape
        # that leaves no other trace: name it while the shedding is happening.
        _note_stall_if_any()
    if over:
        _report_high_water()
    if start_writer:
        _start_drain()


def _drop_at_ceiling(
    session_id: str,
    jobs: "list[tuple[Callable[[], None], str, int]]",
    first: bool,
) -> None:
    """Count an entry shed at the pending ceiling, and say so once per session."""
    global _dropped_count
    with _lock:
        _dropped_count += len(jobs)
        total = _dropped_count
    if first:
        logger.warning(
            "session ledger is holding %d unwritten append(s) for session %s and "
            "cannot take more; its writes are not landing while its producers keep "
            "making them, which is what a hung filesystem looks like from here. "
            "Newest appends are dropped from now on; %d dropped in total. Further "
            "losses for this session are logged at debug until one lands",
            _MAX_PENDING_PER_SESSION,
            session_id,
            total,
        )
    else:
        logger.debug(
            "session ledger shed 1 more append for session %s at its pending ceiling",
            session_id,
        )
    _notify()


def _retain(session_id: str, jobs: "list[tuple[Callable[[], None], str, int]]") -> None:
    """Put a failed batch back at the FRONT of its session's bucket.

    The front, and the whole remainder rather than the one job that failed,
    because the entries after a failure belong AFTER it in the file: writing them
    while the failed one waits would put this session's log on disk in an order
    that never happened, which a fold reads as fact. Producers append to the same
    bucket behind them, so a later write queues behind the retained batch by
    construction -- there is no second structure for it to overtake.

    Spending the last attempt drops the batch instead. Nothing else can: entries
    live in memory until they are written, so a filesystem that never answers
    would hold them forever and turn every bounded caller into a timeout.
    """
    global _pending_count, _pending_high_water
    with _lock:
        state = _retry.setdefault(session_id, _RetryState())
        state.attempts += 1
        spent = state.attempts >= _MAX_WRITE_ATTEMPTS
        if spent:
            _retry.pop(session_id, None)
            start_writer = False
        else:
            state.not_before = time.monotonic() + _retry_delay(state.attempts)
            _pending.setdefault(session_id, [])[:0] = list(jobs)
            _pending_count += len(jobs)
            _pending_bytes[session_id] = _pending_bytes.get(session_id, 0) + sum(
                job[2] for job in jobs
            )
            if _pending_count > _pending_high_water:
                _pending_high_water = _pending_count
            start_writer = not _writer_busy_locked()
            if start_writer:
                _mark_draining_locked()
    if start_writer:
        _start_drain()
    if spent:
        _drop(session_id, jobs)


def _drop(session_id: str, jobs: "list[tuple[Callable[[], None], str, int]]") -> None:
    """Give up on *jobs* and count them. The one place a record is lost.

    Reported once per session rather than once per batch, so a wedged disk names
    itself and then stops talking; the recovery is its own single line, written by
    :func:`_note_progress` when a write for that session lands again. Between the
    two, :func:`dropped_writes` is the exact count.
    """
    global _dropped_count
    with _lock:
        _dropped_count += len(jobs)
        total = _dropped_count
        first = session_id not in _dropped_reported
        _dropped_reported.add(session_id)
    if first:
        logger.warning(
            "session ledger gave up on %d append(s) for session %s after %d failed "
            "attempts; %d dropped in total. That log's tail is short by them. "
            "Further losses for this session are logged at debug until one lands",
            len(jobs),
            session_id,
            _MAX_WRITE_ATTEMPTS,
            total,
        )
    else:
        logger.debug(
            "session ledger dropped %d more append(s) for session %s",
            len(jobs),
            session_id,
        )
    _notify()


def _note_progress(session_id: str) -> None:
    """An append landed: clear the retry state, and say so if it had been failing.

    Clearing on progress is what makes the retry terminate. The budget counts
    CONSECUTIVE failed passes, and a reset costs a real append -- so the buffer
    strictly shrinks between resets and an intermittent failure cannot retry
    without bound.
    """
    with _lock:
        had_retry = _retry.pop(session_id, None) is not None
        recovered = session_id in _dropped_reported or session_id in _ceiling_reported
        _dropped_reported.discard(session_id)
        _ceiling_reported.discard(session_id)
    if recovered:
        logger.warning(
            "session ledger writes for session %s are landing again; entries "
            "dropped while it was failing stay missing from that log",
            session_id,
        )
    if had_retry or recovered:
        _notify()


def _mark_draining_locked() -> None:
    """Claim the next pass. ``_lock`` held.

    The future is cleared HERE rather than left pointing at the previous pass, so
    :func:`_writer_busy_locked` reports busy for the window before
    :func:`_start_drain` publishes the new one -- a pass that is about to be
    submitted is coming, and a stale done-future would read as "nothing coming"
    and let a second producer submit a duplicate.
    """
    global _draining, _drain_future
    _draining = True
    _drain_future = None


def _start_drain() -> None:
    """Ask the writer pool for one drain pass. Never raises."""
    global _draining, _drain_future
    try:
        future = ledger_executor().submit(_drain_loop)
    except Exception as exc:  # pool shut down, or thread creation refused
        with _lock:
            _draining = False
            _drain_future = None
        _notify()
        _report("scheduling the ledger writer", exc)
        return
    with _lock:
        # Published so the barriers can tell a pass that is still coming from one
        # that was CANCELLED by a pool shutdown and will never run.
        _drain_future = future
    _notify()


def _drain_loop() -> None:
    """Drain the pending buffers in batches until they are empty. Writer thread.

    The pause before each pass is what makes a batch: a turn emits several entries
    in a burst, and waiting a fixed moment lets one pass take all of them rather
    than waking per entry. It is a fixed deadline rather than an adaptive one so
    the worst case a producer can impose on the file is a constant.

    A session inside its retry backoff is not claimable, so when nothing is
    claimable this waits exactly as long as the soonest backoff has left rather
    than spinning on a bucket it may not touch. The loop still exits on an empty
    buffer alone, which a retained batch reaches by being written or by being
    dropped -- never by waiting forever.
    """
    global _draining
    try:
        while True:
            with _lock:
                if not _pending:
                    _draining = False
                    idle = True
                    delay = 0.0
                else:
                    idle = False
                    delay = _next_pass_delay_locked()
            if idle:
                _notify()
                return
            if delay:
                # Interruptible: a shutdown that clamps the backoff sets this so the
                # pause is recomputed against the new deadline rather than slept out.
                _wake.wait(delay)
                _wake.clear()
            _drain_once()
    except BaseException:
        with _lock:
            _draining = False
        _notify()
        raise


def _next_pass_delay_locked() -> float:
    """How long to wait before the next pass. ``_lock`` held, buffer non-empty.

    The batching pause when a session is claimable now -- skipped once shutdown
    has asked for quiescence, which is the only thing that pause gives up. Nothing
    claimable means every owed bucket is inside a backoff or held by an inline
    write, so the wait is exactly what the soonest of those has left: the writer
    sleeps instead of spinning, and one wedged ledger paces only its own entries.

    A shutdown does not COLLAPSE the backoff -- burning six attempts in no time at
    all against a filesystem that needed a moment converts a delay into a guaranteed
    loss, at the one point where the entries matter most. It CLAMPS it instead, to
    just inside the drain's own deadline, so a schedule longer than the budget still
    gets one attempt rather than none. See :func:`_backoff_ready_at_locked`.
    """
    now = time.monotonic()
    soonest: float | None = None
    for session_id, jobs in _pending.items():
        if not jobs:
            continue
        if _claimable_locked(session_id, now):
            return 0.0 if _draining_for_shutdown else _BATCH_DEADLINE_SECONDS
        state = _retry.get(session_id)
        if state is None:
            continue  # held by an inline write rather than by a backoff
        ready_at = _backoff_ready_at_locked(state)
        if soonest is None or ready_at < soonest:
            soonest = ready_at
    if soonest is None:
        return _BATCH_DEADLINE_SECONDS
    return max(0.0, soonest - now)


def _claimable_locked(session_id: str, now: float) -> bool:
    """Whether the writer may take this session's bucket. ``_lock`` held.

    Two reasons to leave it alone. Its retained batch is inside its backoff, and
    claiming early would burn an attempt against a filesystem that has not been
    given time to recover. Or a synchronous caller is writing inline for it, and
    claiming beside that is the reordering the inline gate exists to prevent.

    A bounded shutdown CLAMPS the first reason without waiving it -- see
    :func:`_backoff_ready_at_locked`.
    """
    if session_id in _inline:
        return False
    state = _retry.get(session_id)
    return state is None or _backoff_ready_at_locked(state) <= now


def _backoff_ready_at_locked(state: "_RetryState") -> float:
    """When this retained batch may next be attempted. ``_lock`` held.

    Its own schedule, except that a bounded shutdown drain pulls a time BEYOND the
    deadline back to just inside it. Waiting out a backoff that outlasts the process
    does not defer the write, it loses it -- and these are the last entries each
    session produced.

    Clamped rather than collapsed. Zeroing the schedule would spend every remaining
    attempt in microseconds against a filesystem that needed a moment, converting a
    delay into a guaranteed drop at exactly the wrong time.

    The budget is divided into one slice per attempt, and attempt N is allowed at
    ``started + N * slice``. Waiting until the deadline would leave room for a single
    attempt; a slice each leaves room for all of them, which is strictly better for
    the case the backoff exists for -- several chances spread across the budget beat
    one chance at its last moment. The instants are ABSOLUTE for a reason: a rolling
    ``now + slice`` recomputes into the future on every check and the batch would
    never become claimable at all.
    """
    if not _draining_for_shutdown or _shutdown_deadline <= 0.0:
        return state.not_before
    budget = _shutdown_deadline - _shutdown_started
    if budget <= 0.0:
        return _shutdown_started
    slice_secs = max(_MIN_SHUTDOWN_RETRY_GAP, budget / _MAX_WRITE_ATTEMPTS)
    return min(state.not_before, _shutdown_started + state.attempts * slice_secs)


def _drain_once() -> None:
    """Write every claimable session's owed entries, in per-session order."""
    global _pending_count
    now = time.monotonic()
    with _lock:
        claimed: "list[tuple[str, list[tuple[Callable[[], None], str, int]]]]" = []
        for session_id in list(_pending):
            if not _claimable_locked(session_id, now):
                continue
            jobs = _pending.pop(session_id)
            _pending_count -= len(jobs)
            _pending_bytes.pop(session_id, None)
            claimed.append((session_id, jobs))
    for session_id, jobs in claimed:
        _write_batch(session_id, jobs)
    _notify()


def _write_batch(session_id: str, jobs: "list[tuple[Callable[[], None], str, int]]") -> None:
    """Write one session's owed entries in order, stopping at the first FAILURE.

    Stopping rather than skipping ahead: the entries after a failed append belong
    after it in the file, so the remainder -- the failed entry and everything
    behind it -- goes to :func:`_retain`, which puts it back at the front of this
    session's bucket. Writing past it would put the log on disk in an order that
    never happened, which a fold reads as fact.

    A REFUSAL is different and the pass continues past it. A refused entry leaves
    the file byte-identical and will be refused again, so it is simply gone -- and
    the entries behind it would be waiting for something that is never going to
    land.

    A pass that wrote something before it failed calls :func:`_note_progress`
    first, so the attempt budget starts over. That is what makes the retry
    terminate: the budget bounds a WEDGED writer, and a pass that shortened the
    buffer is not wedged.
    """
    landed = False
    for index, (job, what, _nb) in enumerate(jobs):
        failure = _run_job(job, what)
        if failure is None:
            landed = True
            continue
        if _permanent(failure):
            _drop(session_id, [(job, what, _nb)])
            continue
        if landed:
            _note_progress(session_id)
        _retain(session_id, jobs[index:])
        return
    _note_progress(session_id)


def _report_high_water() -> None:
    """Report the buffer growing past its mark once, then stay quiet."""
    global _warned_high_water
    with _lock:
        first = not _warned_high_water
        _warned_high_water = True
        held = _pending_count
    if first:
        logger.warning(
            "session ledger has %d appends buffered, past the %d mark: the writer "
            "is not keeping up and the backlog is held in memory rather than "
            "dropped; further growth is not reported",
            held,
            _PENDING_HIGH_WATER,
        )


def _drain_inline_until(deadline: float) -> bool:
    """Write buffered entries from THIS thread until quiet or *deadline*.

    Used only by :func:`drain_for_shutdown`, and only when no writer batch is
    claimed, so there is no second appender to race.

    Retries a batch that is inside its retry backoff, because the schedule cannot
    outlive the process: honouring it here means the entries are never written at
    all. But the attempts are PACED across the remaining budget rather than spent
    at once -- a batch has a small, fixed number of attempts before it is dropped,
    and burning them in microseconds would turn a filesystem that needed a moment
    into a permanent hole. So each pass is followed by a wait sized to leave one
    attempt per slice of what is left.

    Returns True when the buffers reached empty with nothing in flight.
    """
    while True:
        _drain_once()
        if _quiet():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        with _lock:
            owed = bool(_pending)
        if not owed:
            # Nothing left to attempt; anything short is a claimed batch, which
            # this thread must not touch.
            return _quiet()
        time.sleep(min(remaining, max(_MIN_SHUTDOWN_RETRY_GAP, remaining / _MAX_WRITE_ATTEMPTS)))


def drain_for_shutdown(timeout: float = _SHUTDOWN_DRAIN_SECONDS) -> bool:
    """Write out everything buffered, then stop accepting batching pauses.

    The quiescence barrier a restart needs: entries live in memory until the
    writer takes them, so a process that exits without this loses whatever had
    not been written yet -- exactly the records a crash-time log is wanted for.

    Blocking here is correct where blocking a producer is not: this runs on the
    shutdown path, which has nothing left to keep responsive. Bounded all the
    same, so a wedged filesystem delays exit by *timeout* rather than hanging it.
    Returns True when the buffers reached empty AND no batch is still in flight.

    Both conditions, because a claimed batch is not in ``_pending`` any more: the
    writer takes a batch out of the buffer before it writes it, so an empty buffer
    on its own says nothing about whether those entries reached the file. Reporting
    drained there is the worst available answer -- the caller exits believing the
    record is complete, and the entries in that batch are the last thing each
    session did.

    The inline fallback runs at most one pass, and only when NO writer batch is
    claimed. Two threads appending to one file is not merely a race for the lock:
    ``flock`` serializes the writes but does not order them, so the batch claimed
    second can reach the file first and a ``turn/started`` can land at a lower seq
    than the ``session/opened`` before it. A log whose seq disagrees with causality
    is worse than a short one, because a fold cannot detect it. The timed wait above
    already waits on ``not _draining``, so reaching the fallback with a batch still
    claimed means the writer is wedged holding it -- and then the only safe answer is
    to write nothing.

    Residual, in two shapes. A batch the wedged writer holds cannot be finished from
    this thread. And when that batch is still claimed, the entries buffered BEHIND it
    are left alone too, so they are lost at exit rather than written out of order. A
    False return means exactly that: the log's tail is short, and the warning says
    how short.

    A retained batch is retried rather than waited out. Its backoff is a schedule in
    seconds and this process is leaving, so honouring it here does not defer the
    write, it loses it. :func:`_drain_inline_until` therefore claims a batch inside
    its backoff -- but PACES the attempts across the remaining budget, because a
    batch has few attempts before it is dropped and spending them in microseconds
    turns a filesystem that needed a moment into a permanent hole. What bounds this
    path is still *timeout*; a batch still owed when it expires is reported by the
    warning, and one that ran out of attempts is counted in :func:`dropped_writes`.
    """
    global _draining_for_shutdown, _shutdown_deadline, _shutdown_started
    with _lock:
        _draining_for_shutdown = True
        _shutdown_started = time.monotonic()
        # Published before anything is claimed, so the writer thread's own pass
        # sees the same deadline this call will give up at.
        _shutdown_deadline = _shutdown_started + timeout
        pending = bool(_pending)
        running = _writer_busy_locked()
    # Outside the lock, and BEFORE the wait below: a writer already parked for the
    # length of a retry backoff has to recompute that pause against the deadline,
    # or the clamp above changes a value nothing rereads.
    _wake.set()
    if not pending and not running:
        return True
    if not running:
        # Nothing is in flight, so this thread may write without racing anyone --
        # and it does, rather than asking the pool for a pass. The pool is routinely
        # already GONE by the time this runs (the executor's own exit hook runs
        # first), and asking would then build a fresh one during interpreter
        # shutdown: new non-daemon threads, plus an ``atexit`` registered while
        # ``atexit`` is already draining. Writing here is what the promise "the drain
        # finishes the work in its own thread instead of losing it" actually means.
        drained = _drain_inline_until(time.monotonic() + timeout)
    else:
        with _drained:
            drained = _drained.wait_for(_quiet, timeout=timeout)
    if not drained:
        with _lock:
            # Re-read under the lock: the wait may have failed on either condition,
            # and only one of them permits an inline write.
            claimed = _writer_busy_locked()
        if not claimed:
            # The writer is gone rather than mid-batch, so nothing else will touch
            # the file. Finish what is still buffered here instead of losing it.
            # A short second budget: the first wait already spent *timeout*, and
            # exit cannot be delayed twice over for the same batch.
            drained = _drain_inline_until(time.monotonic() + _SECOND_CHANCE_DRAIN_SECONDS)
    if not drained:
        with _lock:
            held = _pending_count
            running = _writer_busy_locked()
        logger.warning(
            "session ledger did not finish writing within %.1fs of shutdown; "
            "%d append(s) buffered, batch in flight=%s",
            timeout,
            held,
            running,
        )
    return drained


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
    if not session_id:
        return 0
    with _lock:
        return max((turn for (sid, turn) in _live if sid == session_id), default=0)


def _pin(session_id: str, turn: int) -> None:
    """Open the live-turn record for *turn*. Caller's thread.

    Idempotent on purpose: a second start for the same turn keeps the record it
    already has, because resetting the step counter is the corruption this state
    exists to prevent.
    """
    if not session_id:
        return
    with _lock:
        _live_state(session_id, turn)


def _live_state(session_id: str, turn: int) -> "_LiveTurn":
    """The live record for one turn, created on first use. ``_lock`` held.

    Creating on first use covers the turn whose start was never recorded -- the
    flag turned on mid-turn -- so its tool calls still number consistently. The
    record is released by that turn's end or its session closing, exactly like
    one opened by a start.
    """
    key = (session_id, int(turn))
    state = _live.get(key)
    if state is None:
        state = _LiveTurn()
        _live[key] = state
        _pinned[session_id] = _pinned.get(session_id, 0) + 1
        # Not trimmed by pressure from other turns: every record here belongs to
        # a turn that is still running, and dropping one corrupts that turn's
        # numbering. The ceiling below is a leak alarm, not a cache policy.
        if len(_live) > _MAX_LIVE_TURNS:
            _drop_oldest_live()
    return state


def _drop_oldest_live() -> None:
    """Shed the oldest live-turn records once the ceiling is hit. ``_lock`` held."""
    shed = 0
    while len(_live) > _MAX_LIVE_TURNS:
        (old_session, _old_turn), _state = _live.popitem(last=False)
        remaining = _pinned.get(old_session, 0) - 1
        if remaining > 0:
            _pinned[old_session] = remaining
        else:
            _pinned.pop(old_session, None)
        shed += 1
    logger.error(
        "session ledger is tracking more than %d turns in flight and shed %d of "
        "the oldest: turns are ending without a terminal event",
        _MAX_LIVE_TURNS,
        shed,
    )


def _release_session_live(session_id: str) -> None:
    """Close every live-turn record of one session. Used by a fresh claim."""
    if not session_id:
        return
    with _lock:
        for key in [k for k in _live if k[0] == session_id]:
            _release_live(*key)


def _release_live(session_id: str, turn: int) -> None:
    """Drop one turn's live record. ``_lock`` held."""
    if _live.pop((session_id, int(turn)), None) is None:
        return
    remaining = _pinned.get(session_id, 0) - 1
    if remaining > 0:
        _pinned[session_id] = remaining
    else:
        _pinned.pop(session_id, None)


def _bound_open() -> None:
    """Trim the handle cache. See :func:`_bound_unpinned` for the rule."""
    _bound_unpinned(_open, _MAX_OPEN_LEDGERS, lambda k: k, "open handles")


def _remember(session_id: str, ledger: Any) -> None:
    with _lock:
        _open[session_id] = ledger
        _open.move_to_end(session_id)
        _bound_open()


def _handle(session_id: str) -> Any:
    """An open ledger for *session_id*, or None when it has none. Never creates one.

    None means exactly one thing: this session has no ledger file, so an event for
    it is deliberately not written rather than starting a ledger with no header.
    That is a policy no-op, not a loss, and it is not counted as one.

    A FAILURE to reopen is different and is allowed to propagate. This runs inside
    a queued job, so the exception reaches :func:`_run_job` and the entry is
    retained and retried like any other failed append -- where returning None would
    make the job succeed with nothing written, and the entry would vanish with no
    retry and no entry in :func:`dropped_writes`. That is the same silent hole the
    retention exists to close, one layer up. The ``exists`` stat is inside that
    rule too: a stat that raises has not answered whether the ledger is there, and
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
    if not Ledger.exists(_KIND, session_id):
        return None
    # RECONNECT, never a resume: this path is reached when a handle is missing
    # from the cache, which says nothing about the writer's health -- an
    # eviction is enough. So it opens WITHOUT repair; closing a turn here
    # would close one that is still running.
    ledger = Ledger.open(_KIND, session_id)
    _remember(session_id, ledger)
    return ledger


def _bound_unpinned(
    store: "OrderedDict[Any, Any]",
    limit: int,
    session_of: Callable[[Any], str],
    what: str,
) -> None:
    """Trim *store* to *limit*, never dropping an entry of a live turn.

    Called with ``_lock`` held. Every map in this module is state CORRELATED TO a
    turn, and the rule is the same for all of them: an entry is released by an
    event of its OWN turn -- ``turn/completed``, or the session closing -- never by
    pressure from other sessions. Evicting mid-turn is what turns a bounded cache
    into a correctness bug rather than a performance one: a dropped handle sends
    the next emit back through ``open``, and a dropped step ordinal restarts the
    numbering so two calls in one turn claim the same position.

    When every entry belongs to a live turn the store is allowed to exceed
    *limit*. That is the honest trade: the cap exists so UPTIME cannot grow a map
    without limit, and each entry is released by its own turn's event, so the
    overshoot is bounded by concurrent live turns instead.
    """
    if len(store) <= limit:
        return
    for key in list(store):
        if len(store) <= limit:
            break
        if session_of(key) not in _pinned:
            store.pop(key, None)
    if len(store) > limit:
        logger.debug(
            "session ledger %s holds %d entries, over the %d cap: every one "
            "belongs to a turn in flight",
            what,
            len(store),
            limit,
        )


def _mint_call_index(session_id: str, turn: int) -> int:
    """The next TOOL-CALL ordinal inside *turn*. Caller's thread.

    Numbers the tool calls of one turn in the order the runner made them, so a
    reader can order them without comparing seq -- and can tell two calls of the
    same tool apart when their ids are opaque. A model call may issue several
    tools at once, so its ``step`` cannot order them and this counter is not
    redundant with it. The counter is the turn's own state, so no pressure from
    other sessions can reset it and hand two calls the same ordinal.
    """
    with _lock:
        state = _live_state(session_id, turn)
        state.call_index += 1
        return state.call_index


def _mint_step(session_id: str, turn: int) -> int:
    """The next MODEL-CALL ordinal inside *turn*. Caller's thread."""
    with _lock:
        state = _live_state(session_id, turn)
        state.step += 1
        return state.step


def _current_step(session_id: str, turn: int) -> int:
    """Which model call *turn* is inside right now, or 0 before the first.

    Reads without minting: every entry produced during a model call names that
    call, and only :func:`on_step_started` may advance it. Zero means no step has
    been announced -- the flag came on mid-turn, or a call site that does not
    track steps produced the entry -- and the entry then omits ``step`` rather
    than claiming to belong to a model call nobody observed.
    """
    if not session_id:
        return 0
    with _lock:
        state = _live.get((session_id, int(turn)))
        return 0 if state is None else state.step


def _next_attempt(session_id: str, turn: int) -> int:
    """Which try this is at *turn*, counting from 1. WRITER thread only.

    Called from inside a queued job, never on the caller's thread: the resume
    seed is an earlier job on the same session, so running here is what orders
    this read behind it.

    A regenerate or a rewind reruns a turn the ordinal already names. Without a
    discriminator the two starts are identical lines, so a fold cannot tell a
    retry from a duplicate write, and `(turn, attempt)` is the identity it groups
    on. Derived HERE rather than asked of the call sites: `chat_regenerate` and
    `chat_rewind` are two of several rerun paths, and a field only the audited
    ones supply is a field a reader cannot trust.
    """
    with _lock:
        seen = _attempts.setdefault(session_id, {})
        attempt = seen.get(int(turn), 0) + 1
        seen[int(turn)] = attempt
        _attempts.move_to_end(session_id)
        _bound_unpinned(_attempts, _MAX_OPEN_LEDGERS, lambda k: k, "turn attempts")
        return attempt


def _seed_attempts(session_id: str, ledger: Any) -> None:
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
        for entry in ledger.iter_from(1):
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
        _report("seeding turn attempts", exc)
        return
    if not highest:
        return
    with _lock:
        seen = _attempts.setdefault(session_id, {})
        for turn, attempt in highest.items():
            if attempt > seen.get(turn, 0):
                seen[turn] = attempt
        _attempts.move_to_end(session_id)
        _bound_unpinned(_attempts, _MAX_OPEN_LEDGERS, lambda k: k, "turn attempts")


def close_open_tool_calls(
    session_id: str,
    turn: int,
    *,
    status: str = "unknown",
    is_error: bool | None = None,
) -> int:
    """Close every tool call of *turn* that is still open. Returns how many.

    A tool that produces NO OUTPUT sends no result frame at all, so ``tool_final``
    never arrives and the completion path never runs: its ``tool/called`` would
    stay open for the life of the file, and a fold counting open calls would
    report a turn that never finished using a tool it had in fact finished with.
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
    with _lock:
        # Filtered on the turn as well as the session. A transient can leave a call
        # open and the same session then runs another turn, so selecting every open
        # call here would close the earlier turn's call under THIS turn's ordinal --
        # a tool attributed to a turn that never used it. The earlier turn's own
        # closer already ran, and a call still open past it is the interrupted-turn
        # repair's job, which works from the file rather than from memory.
        stale = [
            (call_id, _tool_started.pop((sid, call_id)))
            for (sid, call_id), started in list(_tool_started.items())
            if sid == session_id and started[5] == int(turn)
        ]
    closed = 0
    for call_id, started in stale:
        began, name, server, call_index, step, _ = started
        data: dict[str, Any] = {
            "turn": int(turn),
            "call_id": call_id,
            "name": name,
            "server": server,
            "status": status,
            "result_bytes": 0,
        }
        if call_index:
            data["call_index"] = call_index
        if step:
            data["step"] = step
        data["elapsed_ms"] = max(0, int((time.monotonic() - began) * 1000))
        if is_error is not None:
            data["is_error"] = bool(is_error)
        _write(session_id, "tool/completed", data)
        closed += 1
    return closed


def _forget_turn(session_id: str, turn: int) -> None:
    with _lock:
        _release_live(session_id, turn)


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
    secrets, and a body is the one field that could.
    """
    if not isinstance(text, str) or not text:
        return ""
    try:
        cleaned, _ = redact_exfiltration_urls(text)
        cleaned, _ = redact_credentials(cleaned)
        return cleaned
    except Exception as exc:
        _report("redacting a body", exc)
        return ""


def _text_slices(text: str) -> list[str]:
    """*text* cut into pieces that each MEASURE small enough for one ledger line.

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
    ledger serializes with ``ensure_ascii``, so a non-ASCII character costs six
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
    return escaped + _ENVELOPE_HEADROOM <= MAX_ENTRY_BYTES


def _write(
    session_id: str,
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str = _SRC_ACP,
    after: Callable[[], None] | None = None,
    ignorable: bool = False,
) -> None:
    """Queue one entry.

    ``thread`` is never set. A session entry names its turn in ``data.turn``,
    which the runner already knows when it calls -- so nothing has to be looked
    up, cached, or read back from a line written earlier. The envelope's
    ``thread`` field points at another LINE's seq, which is only knowable for a
    unit whose anchor line is written before the entries that cite it; that is
    the crew ledger's shape, not this one's.

    ``ignorable`` marks an entry a reader may skip when it does not know the
    type. It is the writer's promise that nothing later in the file depends on
    this entry having been interpreted, so only an entry that samples a stream
    carries it.

    ``after`` runs whether or not the append did, matching the rule that a ledger
    failure never changes what the gateway does next.
    """
    if not session_id or not enabled():
        return

    def _job() -> None:
        try:
            ledger = _handle(session_id)
            if ledger is None:
                return
            ledger.append(entry_type, data, src=src, ignorable=ignorable)
        finally:
            if after is not None:
                after()

    _submit(_job, f"appending {entry_type}", session_id)


def on_session_opened(
    session_id: str,
    *,
    agent: str = "",
    slot: str = "",
    model: str = "",
    cwd: str = "",
    owner: str = "default",
    resumed: bool = False,
) -> None:
    """Create the ledger if this session has none, then echo its header.

    Called once per turn, because the per-turn session claim is where the ACP
    id becomes known -- but an entry is written only when there is something new
    to say: the ledger was just created, or this claim RE-ATTACHED to an existing
    conversation (a new gateway process taking over the same session id). A warm
    reuse of a session already carrying a ledger adds nothing, so it is silent.

    ``owner`` and ``agent`` are header fields, written once at create time and
    never rewritten. A resumed session id reuses its existing ledger and
    appends, so an agent or model switch that kept the conversation continues
    one log rather than starting a second one. ``model`` is not a header field
    in the storage schema, so it is carried on this entry instead.
    """
    if not session_id or not enabled():
        return
    # A claim ends every turn this process still believes is running: either it
    # is a fresh attach, or the previous turn's terminal event already released it.
    _release_session_live(session_id)
    # Latched on the FIRST attempt and read back on every later one. The decision
    # below is derived from filesystem state this job itself changes: a retry after
    # the header landed but the entry did not finds ``exists`` true and ``created``
    # false, so an unlatched decision would flip to "nothing new to say" and skip
    # the entry it still owes -- permanently, and without counting the loss.
    announce: "dict[str, bool]" = {}

    def _job() -> None:
        created = False
        if Ledger.exists(_KIND, session_id):
            # THE resume path, and the only caller that may repair. ``resumed``
            # means this claim re-attached to a conversation a DIFFERENT gateway
            # process was writing, so a turn left open in that file belongs to a
            # writer that is gone and closing it records what happened. A warm
            # reuse inside this process passes ``resumed=False`` and must not
            # repair: its turn may still be running. A reconnect after a cache
            # eviction never reaches here at all -- it goes through ``_handle``,
            # which opens without repair.
            ledger = Ledger.open(_KIND, session_id, repair=bool(resumed))
            # Rebuild the attempt counts from what is already in the file, for
            # the two cases where memory cannot answer: a resume, whose counts
            # belong to a process that is gone, and a session whose map was
            # evicted by the bound. A warm reuse still HOLDS its counts, and
            # re-seeding it would rescan the whole file on the one thread that
            # serializes every session's appends -- once per open, against a file
            # that only grows.
            with _lock:
                needs_seed = bool(resumed) or session_id not in _attempts
            if needs_seed:
                _seed_attempts(session_id, ledger)
        else:
            ledger = Ledger.create(
                _KIND,
                session_id,
                owner=owner or "default",
                agent=agent or _DEFAULT_AGENT,
                slot=slot or None,
                cwd=cwd or None,
            )
            created = True
        _remember(session_id, ledger)
        if not announce.setdefault("owed", created or bool(resumed)):
            return
        ledger.append(
            "session/opened",
            {
                "agent": agent or _DEFAULT_AGENT,
                "slot": slot,
                "model": model,
                "cwd": cwd,
                "owner": owner or "default",
                "resumed": bool(resumed),
            },
            src=_SRC_GATEWAY,
        )

    _submit(_job, "opening a ledger", session_id)


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
        _pin(session_id, turn)
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
        ledger = _handle(session_id)
        if ledger is None:
            return
        # Derived HERE, on the writer thread, NOT on the caller's. Resume seeding
        # runs as an earlier job for this same session, and the writer executes a
        # session's jobs in submission order -- so deriving here is ordered behind
        # the seed by construction. Deriving on the caller's thread instead reads
        # the map while that seed is still queued, and the first rerun after a
        # resume then writes an attempt the file already holds. The alternative,
        # making the caller wait for a seeded event, puts a filesystem read in
        # front of the turn on the event loop.
        n = attempt if attempt > 0 else _next_attempt(session_id, turn)
        payload = dict(data)
        # Which try this is at the same turn ordinal. Omitted at 1, which is every
        # turn that was never rerun -- the common case should not pay a field for
        # the rare one.
        if n > 1:
            payload["attempt"] = int(n)
        ledger.append("turn/started", payload, src=_SRC_GATEWAY)

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
    )
    # Nothing is in flight, so nothing may stay pinned on its behalf.
    _forget_turn(session_id, turn)


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
) -> None:
    """Record a turn's terminal event and what it cost."""
    data = _turn_closer(
        turn,
        duration_ms=duration_ms,
        stop_reason=stop_reason,
        model=model,
        provider=provider,
        depth=depth,
    )
    data["credits"] = float(credits)
    data["tokens"] = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    _write(session_id, "turn/completed", data)
    # The turn is over: its step counter has nothing left to number, and its
    # handle stops being live state that eviction must skip.
    _forget_turn(session_id, turn)


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
    streamed real text does not get a durable line claiming it cost nothing. Their
    absence also tells this synthesized closer from a provider-reported one.
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
    _write(session_id, "turn/completed", data, src=_SRC_GATEWAY)
    _forget_turn(session_id, turn)


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


def _append_body_entry(
    ledger: Any,
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

    Runs on the writer thread with the ledger handle in hand, because the chunks
    must be on disk before the entry citing them can name their seqs.
    """
    data: dict[str, Any] = {"turn": int(turn)}
    if step:
        data["step"] = int(step)
    if extra:
        data.update(extra)
    if _fits_one_line(text, extra):
        data["text"] = text
        ledger.append(entry_type, data, src=src)
        return
    # A chunk group is written as ONE batch, not entry by entry. The chunks are
    # meaningless without the entry that cites their seqs: appending them
    # separately leaves a window in which a hard kill puts the body on disk with
    # nothing pointing at it -- stored and unreachable, and the entry that would
    # have explained it never written. One write leaves either the whole group or a
    # torn tail, and the tail is what the next append truncates.
    slices = _text_slices(text)
    # The citing entry rides in the same batch, so its references have to be known
    # before the write. The seqs are allocated contiguously from one read of the
    # tail, and this handle has a single writer thread.
    planned = ledger.plan_group_seqs(len(slices) + 1)
    data["chunks"] = planned[: len(slices)]
    data["chars"] = len(text)
    group: list[dict[str, Any]] = [
        {
            "type": "message/chunk",
            "data": {"turn": int(turn), **({"step": int(step)} if step else {}), "delta": piece},
            "ignorable": True,
        }
        for piece in slices
    ]
    # LAST, so a torn tail can only cost the citing entry -- which is the orphan
    # shape the repair drops -- never a chunk the citing entry already named.
    group.append({"type": entry_type, "data": data})
    ledger.append_many(group, src=src)


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
    (``_ChatSlot.enqueue_or_run_prompt``): the ledger is keyed by the ACP session
    id and that site runs before the session is claimed, so there is no id to key
    by and no ledger to write to yet.

    ``source`` is the surface the message arrived on, a fact the dispatch layer
    supplies. ``attachments`` are identifiers, not ``Ref``s -- a ``Ref`` cites
    lines of another ledger and an attachment is not a ledger unit, the same
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
        ledger = _handle(session_id)
        if ledger is None:
            return
        _append_body_entry(
            ledger,
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
    ordinal = int(step) if step else _current_step(session_id, turn)

    def _job() -> None:
        ledger = _handle(session_id)
        if ledger is None:
            return
        _append_body_entry(
            ledger,
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
        ledger = _handle(session_id)
        if ledger is None:
            return
        ledger.append("request/configured", data, src=_SRC_GATEWAY)
        # Remembered only AFTER the line is on disk. Committing the fingerprint
        # before the append would let one transient failure suppress every later
        # identical configuration, leaving the session permanently without the
        # record -- a silent hole rather than a retry. The other direction costs a
        # duplicate entry when two turns queue before the first lands, and a
        # duplicate in an append-only log is a far cheaper wrong than a gap.
        with _lock:
            _last_config[session_id] = fingerprint
            _last_config.move_to_end(session_id)
            _bound_unpinned(_last_config, _MAX_OPEN_LEDGERS, lambda k: k, "request configs")

    _submit(_job, "appending request/configured", session_id)


def on_context_composed(
    session_id: str,
    turn: int,
    *,
    step: int = 0,
    blocks: "dict[str, int] | None" = None,
    total_chars: int = 0,
) -> None:
    """Record what the gateway put in front of the model, block by block.

    ``blocks`` is ``context_blocks.split_blocks()``'s own output: a label to
    CHARACTER count map whose values sum to the assembled prompt. Characters are
    what the repo measures exactly, so they are recorded as measured and
    ``tokens`` is derived at :data:`_EST_CHARS_PER_TOKEN` -- an ESTIMATE, and the
    spec says so. The one tokenizer available is the wrong one for the served
    model, and a fabricated exact count would be worse than an admitted estimate.

    Every label ``split_blocks`` does not classify is folded into a single
    ``other`` source. Three blocks the design names -- steering, tool specs and
    injected ledger context -- have no opening marker, so their characters are
    genuinely in that remainder; reporting them as three zeroed sources would
    claim a measurement that was never taken.
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
    step = _mint_step(session_id, turn)
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


def on_message_steered(session_id: str, turn: int, *, mode: str = "", text: str = "") -> None:
    """NO PRODUCTION CALLER YET -- see the emitter spec's deferred table.

    The fact is knowable only from the ``steering_consumed`` echo, while the text
    the steer interrupted is logged from the handler's segment cut, and the two
    race: under stdin backpressure the echo lands first and this entry would take a
    lower seq than the assistant text it cut. Wiring it needs a resolver that owns
    both facts. The API is here, specified and tested, for that resolver to call.

    Record a steer injected into a running turn.

    ``mode`` is the caller's own word: ``interrupt`` cuts the turn's current
    output, ``follow_up`` waits for it to finish. Recorded verbatim rather than
    normalized, matching how ``session/closed`` records the gateway's own reason.

    Gated on the flag before redaction, like the other body entry points, and
    written through :func:`_append_body_entry` for the same reason they are: a
    steer is a body a person typed, so one over the entry ceiling would be refused
    at append time and the steer that actually reached the turn would be missing
    from the log.
    """
    if not session_id or not enabled():
        return
    body = _safe_text(text)

    def _job() -> None:
        ledger = _handle(session_id)
        if ledger is None:
            return
        _append_body_entry(
            ledger,
            "message/steered",
            turn,
            text=body,
            extra={"mode": mode},
            src=_SRC_GATEWAY,
        )

    _submit(_job, "appending message/steered", session_id, len(body))


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
    call_index = _mint_call_index(session_id, turn)
    step = _current_step(session_id, turn)
    if call_id:
        with _lock:
            _tool_started[(session_id, call_id)] = (
                time.monotonic(),
                name,
                server,
                call_index,
                step,
                int(turn),
            )
            _bound_unpinned(_tool_started, _MAX_PENDING_TOOLS, lambda k: k[0], "pending tool calls")
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
        with _lock:
            started = _tool_started.pop((session_id, call_id), None)
        if started is not None:
            began, called_name, called_server, called_index, called_step, _ = started
            elapsed_ms = max(0, int((time.monotonic() - began) * 1000))
            name = name or called_name
            server = server or called_server
            call_index = called_index
            step = called_step
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
    result_hash, result_bytes = _payload_digest(result)
    if result_hash:
        data["result_hash"] = result_hash
        data["result_bytes"] = result_bytes
    _write(session_id, "tool/completed", data)


def on_approval_requested(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    tool: str = "",
) -> None:
    """Record that a tool call is waiting on a human."""
    _write(
        session_id,
        "approval/requested",
        {"turn": int(turn), "approval_id": approval_id, "tool": tool},
        src=_SRC_GATEWAY,
    )


def on_approval_decided(
    session_id: str,
    turn: int,
    *,
    approval_id: str,
    decision: str,
) -> None:
    """Record how an approval resolved, including a timeout.

    This runs on the task that handled the click, not the task running the
    turn, which is why it must never raise.
    """
    _write(
        session_id,
        "approval/decided",
        {"turn": int(turn), "approval_id": approval_id, "decision": decision},
        src=_SRC_GATEWAY,
    )


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


def on_session_closed(session_id: str, reason: str) -> None:
    """Record a session teardown and drop its cached state.

    ``reason`` is the gateway's own ``end_reason`` (``reset``, ``shutdown``,
    ``evicted``, ``destroyed`` and the rest), recorded verbatim rather than
    remapped onto a second vocabulary.

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
    ``_MAX_LIVE_TURNS`` strands a held reason; and a resume that re-claims the id
    would have to decide whether the predecessor's teardown still happened. Each of
    those is a way to LOSE the terminal, which is worse than a terminal a late entry
    follows -- an absent one cannot be told apart from a crash.

    What this must not do is erase state a live turn still needs. The cleanup below
    drops only what a successor must not inherit; the live-turn records and the open
    tool calls of turns still running are left to their own release, so a turn whose
    session closed under it can still close its calls.
    """

    def _forget() -> None:
        with _lock:
            _open.pop(session_id, None)
            # A later session reusing this id must write its own configuration
            # rather than inheriting a closed session's as "unchanged".
            _last_config.pop(session_id, None)
            # A later session reusing this id starts its own attempt counts;
            # a resume reseeds them from the file instead.
            _attempts.pop(session_id, None)
            # Only calls whose turn is already gone -- `_tool_started` is keyed by
            # call id, so the turn comes from the record's last field. A live turn's
            # open calls are its own to close, and dropping them here left them open
            # for the life of the file.
            for tool_key in [
                k
                for k, rec in _tool_started.items()
                if k[0] == session_id and (session_id, rec[5]) not in _live
            ]:
                _tool_started.pop(tool_key, None)

    _write(
        session_id,
        "session/closed",
        {"reason": reason},
        src=_SRC_GATEWAY,
        after=_forget,
    )


__all__ = [
    "ACTORS",
    "SESSION_LEDGER_ENV",
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
    "on_message_steered",
    "on_model_selected",
    "on_request_configured",
    "on_session_closed",
    "on_session_opened",
    "on_step_completed",
    "on_step_started",
    "on_tool_called",
    "on_tool_completed",
    "on_turn_completed",
    "on_turn_failed",
    "on_turn_refused",
    "on_turn_started",
    "reset_caches",
]

# The graceful path is the gateway's own cleanup hook, which drains in a thread
# before the process winds down. This is the backstop for every other exit -- a
# CLI run, a cron subprocess, a signal the server never sees. Registered at
# import, so the executor's own atexit (registered when its pool is first built)
# runs FIRST and may already have taken the writer pool down; the drain writes
# what is left in this thread when that happens, which is why it does not depend
# on the pool being alive.
atexit.register(drain_for_shutdown)
