"""``kirocrew.session.duration`` + ``kirocrew.session.started``.

How long a session lives, and why it ended. Together they answer questions the
existing session instruments cannot: ``kirocrew.session.startup.duration``
measures the cold start of the agent PROCESS, and
``kirocrew.session.idle_expired`` counts one specific teardown cause, but
nothing recorded how long a session actually lasted or how its lifetimes split
across the ways a session can end.

**Why the reason is passed in, never derived.** There is no single teardown
function. ``session_lifecycle.SessionLifecycleService`` exposes six distinct end
paths -- ``reset``, ``remove``, ``remove_if_unclaimed``, ``destroy``,
``discard_conversation`` and ``close_all`` -- and each pops the registry itself
rather than delegating to a shared funnel. Two more paths pop it WITHOUT going
through any of those: ``retire_kiro_identity_sessions`` (an identity change
retires every idle process on the old account) and
``CompactionCoordinator._recycle_held`` (context overflow replaces the provider
in place). So each path states its own reason, the same contract
``metrics/turns.py`` holds its callers to.

``reset`` is the widest of them: the idle sweep and a slot reset both reach
teardown through it, so ``end_reason=reset`` is a path label, not a cause. That
is deliberate -- the finer causes behind it are already counted separately
(``kirocrew.session.idle_expired``, ``kirocrew.watchdog.recovery.outcome``), and
a metric must not be the reason a lifecycle signature every surface calls grows
a parameter. The identity retire and the compaction recycle are NOT inside it and
carry their own labels, because a metric that reported them as ``reset`` would be
claiming a teardown route they never take.

**Why a breadcrumb on disk.** A start time held only in memory dies with the
process, which is exactly the population most worth measuring: a session that
ends because the gateway crashed never runs any teardown path, so it would
contribute no sample at all and the histogram would describe only orderly
shutdowns. Each start therefore drops one small JSON file under
``<data home>/metrics/open-sessions/``. The clean path does NOT read that file --
it takes the start time from the in-memory table, so an end can account for
itself in the same tick as the registry removal it reports -- it only unlinks it.
Whatever is still there at the next boot belongs to a session that never ended
cleanly, so :func:`backfill_crashed_sessions` reads it, emits it as
``end_reason=crashed`` and unlinks it. The crumb is written only while telemetry
consent is in force, since it exists solely to feed an instrument that is a no-op
without it.

**No crumb syscall runs on the event loop.** Every path that touches a crumb
file -- the start's write, the end's unlink, a rolled-back start's discard and
the boot backfill's scan -- does it on a worker thread. A single ``unlink`` looks
too small to bother with until the data home is slow or network-backed, at which
point that one syscall parks every gateway task behind whichever session happens
to be closing: teardown runs on the paths a person is waiting on (closing a tab,
resetting a slot, shutting the gateway down), so the stall is felt as the whole
gateway freezing rather than as a metrics problem. Each hop is AWAITED rather
than queued, for the reason :func:`_unlink_generations` gives at length.

**Why not the transcript's existing close marker.** A transcript's metadata line
already carries ``created_at``, and the dashboard tab-close path stamps
``closed`` / ``closed_at`` beside it. Neither substitutes for the crumb. That
stamp is written by the dashboard close path ALONE, so a channel, cron, subagent
or task-runner session never gets one; and its ABSENCE is ambiguous by
construction -- a transcript with no ``closed`` is equally a crashed session, a
live-but-idle one, and one that was never a dashboard tab. There is no positive
crash flag on disk today, which is what the crumb supplies, for every surface.
Boot has no session sweep to piggyback on either: the restore path is seed-driven
from ``open_slots.json`` and never walks the transcript directory.

**Why the clean path consumes the crumb rather than reading it.** The
popped registry entry does carry an epoch ``created_at``, so a clean end could
compute its own duration without touching disk. It deliberately does not, for two
reasons. The clean and crashed paths must consume the SAME record or they
double-count: a crumb left behind by a clean end is indistinguishable at the next
boot from one left by a crash, and would be emitted a second time as
``crashed``. And that field is RESET when a provider is recycled in place, so it
measures the provider's age rather than the session's residency in the registry.

That unlink is what makes the accounting exact. A crumb is consumed at most
once, so the six teardown paths cannot double-count a session between them (the
idle sweep's ``reset`` is a real instance of this: the sweep and the path it
calls both sit on the same session), and a backfilled session cannot be counted
again on the boot after that.

**Where a crashed session's END time comes from.** The crumb is written once and
never rewritten, so its own mtime is its start. The session's transcript
(``<data home>/sessions/<stem>.jsonl``) is appended to as the conversation runs,
so its mtime is the last moment the session was observably alive -- the honest
maximum available after the fact. A session with no transcript on disk (a
subagent leaves only a replay log) yields no end time, so it is unlinked without
an emit rather than recorded as a plausible-looking zero: an absent sample reads
as "no data", a zero renders as a real 0ms lifetime.

Telemetry must never break the instrumented path, so every function here
swallows its failures after a debug log, and every attribute value is a
constant from the closed enums below.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: Session lifetime, in ms. Registered in ``metrics/provider.py``'s bucket table
#: under its own boundary family: this is the only kirocrew histogram whose range
#: is minutes to days, so it shares no family with the request/turn instruments.
SESSION_DURATION_METRIC = "kirocrew.session.duration"

#: One increment per session admitted to the registry -- the denominator the
#: duration histogram's population is a subset of.
SESSION_STARTED_COUNTER = "kirocrew.session.started"

# ---------------------------------------------------------------------------
# end_reason -- closed enum, one member per teardown path
# ---------------------------------------------------------------------------

#: ``SessionLifecycleService.reset`` -- the widest path (idle sweep, slot reset,
#: watchdog recycle). A compaction recycle is NOT here: it pops the registry
#: itself and reports :data:`END_REASON_RECYCLED`. See the module docstring.
END_REASON_RESET = "reset"
#: ``remove`` -- shut down, session-map entry deliberately preserved.
END_REASON_REMOVED = "removed"
#: ``remove_if_unclaimed`` -- a speculative session whose first turn never came.
END_REASON_UNCLAIMED = "unclaimed"
#: ``destroy`` -- session and its persistence entry both gone.
END_REASON_DESTROYED = "destroyed"
#: ``discard_conversation`` -- native conversation dropped, linkage kept.
END_REASON_DISCARDED = "discarded"
#: ``close_all`` -- gateway shutdown drained the registry.
END_REASON_SHUTDOWN = "shutdown"
#: ``retire_kiro_identity_sessions`` -- the account behind the session changed,
#: so every idle process on the old identity is torn down. Its own label rather
#: than folded into ``reset``: it pops the registry directly and never calls
#: ``reset``, and an identity change is a different event from an idle sweep.
#: ``reload_provider_factory`` reports this too for the registry it clears on a
#: provider switch -- both are "this process is no longer the right host for these
#: sessions", which is why they share a label rather than minting a second one.
END_REASON_RETIRED = "retired"
#: ``CompactionCoordinator._recycle_held`` -- context overflow replaced the
#: provider in place. Also pops directly rather than routing through ``reset``.
END_REASON_RECYCLED = "recycled"
#: A registry entry dropped because its provider was already dead
#: (``SessionAllocationService._evict_stale_session`` and the same check inside
#: ``get_or_create``). Its own label rather than folded into ``reset``: nothing
#: was torn down here, the process had already gone, so this population answers
#: "how often did a session die under us and get noticed on the next lookup"
#: rather than "how often did we end one". Left unrecorded it would be worse than
#: missing -- the crumb would survive and be reported as ``crashed`` at the next
#: boot, which is a different claim about a different boot.
END_REASON_EVICTED = "evicted"
#: Backfilled at boot from a crumb no teardown path ever consumed.
END_REASON_CRASHED = "crashed"

#: Every value ``end_reason`` can carry. The drift gate in
#: ``test/metrics/test_session_duration.py`` harvests this set, so a new path
#: cannot start emitting a label the tests do not know about.
END_REASONS = frozenset(
    {
        END_REASON_RESET,
        END_REASON_REMOVED,
        END_REASON_UNCLAIMED,
        END_REASON_DESTROYED,
        END_REASON_DISCARDED,
        END_REASON_SHUTDOWN,
        END_REASON_RETIRED,
        END_REASON_RECYCLED,
        END_REASON_EVICTED,
        END_REASON_CRASHED,
    }
)

# A runaway crumb directory must not turn boot into a stat storm, so the backfill
# emits at most this many samples per boot. Everything it walks is unlinked
# either way, so a directory that somehow grew past the cap is drained rather
# than left to be re-walked at every boot from then on.
_MAX_BACKFILL_EMITS = 2000

_CRUMB_DIR_NAME = "open-sessions"

# Start time per LIVE session key -- the authority for the normal path, so an end
# never has to read the disk and can therefore run in the same tick as the
# registry removal it reports. The crumb on disk exists only to survive process
# death. Last write wins: a key re-entering the registry is a new session whose
# lifetime must be its own (see record_session_started).
#
# **This lock guards the TABLE ONLY and is never held across I/O.** It is taken on
# the event loop (every end runs there, in the same tick as its registry pop), so
# holding it across a crumb read or write would park the loop behind filesystem
# latency -- which is exactly what happened when it also guarded the crumb: the
# backfill held it across a read plus an unlink on a worker thread while a
# teardown waited for it on the loop. Crumb file ordering is _crumb_io_lock's job.
#
# Bounded because not every registry removal records an end, so a stale entry is
# possible; the cap discards the oldest half rather than growing for the process
# lifetime, which costs samples and never memory.
_MAX_LIVE_SESSIONS = 4096
_live_lock = threading.Lock()
_live_starts: dict[str, float] = {}

# Orders the two WORKER-THREAD paths that touch crumb files: the writer, and the
# backfill's read-decide-unlink. Both run off the loop, so a hold across
# filesystem latency costs a worker and never the gateway's responsiveness --
# which is the whole reason it is a second lock rather than _live_lock.
#
# Nothing on the event loop may take this. The end path does not need it on either
# side of its worker hop: with ownership recorded in each crumb, the backfill skips
# every crumb whose owner is live, so a session ending in THIS process and the
# crumbs that scan reaps are disjoint sets. It is also worth being explicit that
# this is a THREAD lock and therefore never protected anything against a sibling
# PROCESS -- that is what the ownership field is for.
_crumb_io_lock = threading.Lock()


def _crumb_dir() -> Path:
    """``<data home>/metrics/open-sessions`` -- created on demand."""
    from kiro_crew.config.paths import config_dir

    return config_dir() / "metrics" / _CRUMB_DIR_NAME


def _crumb_path(session_key: str, started_at: float) -> Path:
    """Path of ONE generation of *session_key*'s crumb.

    Session keys carry channel ids and slot names and are not constrained to
    filesystem-safe characters, so the key is never used as a filename. The key
    itself is stored INSIDE the file, which is what lets the backfill find the
    session's transcript; it is not new exposure, since ``session_map.json``
    already holds every session key in the same data home.

    **The name identifies the WRITER and the GENERATION, not just the key, and
    that is what makes every unlink in this module safe.** Keying the file on the
    session key alone gave two live sessions one shared path, because a session
    key is not unique across processes: ``BACKGROUND_KEY`` is a fixed constant, so
    a ``kirocrew run`` and the gateway hold the same key at the same time, each
    with its own session behind it. Whichever ended first unlinked the file, and
    the survivor's crash then went unrecorded -- and the thread locks here never
    spanned processes, so nothing in this module could have prevented it. With the
    pid and the start time in the name, a crumb can only ever be removed by the
    session it belongs to, so a stale or late unlink cannot reach a live session's
    record. It also means the backfill legitimately finds SEVERAL files for one
    key: each is a distinct session, judged on its own recorded owner.
    """
    digest = hashlib.sha256(session_key.encode("utf-8", "surrogatepass")).hexdigest()
    return _crumb_dir() / f"{digest[:32]}-{os.getpid()}-{int(started_at * 1e6)}.json"


def _session_source(session_key: str) -> str:
    """Bounded label for which surface owns *session_key*.

    ``telemetry_channel_of`` exists for exactly this question and never returns
    the key itself, so cardinality is bounded whatever the caller passes. Same
    derivation ``metrics/turns.py`` uses, so the two instruments group by the
    same label.
    """
    from kiro_crew.messaging.link import telemetry_channel_of

    return telemetry_channel_of(session_key)


async def record_session_started(session_key: str) -> None:
    """Record one session start; never raises.

    The start time goes into an in-memory table keyed by session key. The crumb on
    disk is written on a worker thread, awaited before returning. The table is
    what the normal path reads; the crumb exists ONLY so a start can outlive its
    process (see :func:`backfill_crashed_sessions`).

    **The table entry is overwritten, and so is the crumb.** A key re-entering
    the registry is a NEW session, and its lifetime must be measured from ITS
    start, not from a predecessor's -- so last-writer-wins is what keeps a
    successor's lifetime its own.

    **Every registry removal must record an end.** Not because a missing sample
    would matter on its own, but because the crumb turns an unrecorded removal
    into a WRONG one: the crumb survives, and the next boot reports the session as
    ``crashed``. So this is not merely a lost sample -- it manufactures a failure
    that never happened, in exactly the population the histogram exists to
    measure. ``test_every_registry_removal_records_an_end`` is a fail-closed gate
    over every removal site in ``src/`` for that reason.

    **Why this is a coroutine, and awaited under the caller's lock.** Two
    constraints look opposed and are not. The write must not run on the event
    loop, because a slow or network-homed data home would stall every gateway
    task behind one session insertion -- the same reason ``SessionMap`` offloads
    its own persist to a worker rather than writing inline. But it also must not
    be fire-and-forget, because a writer that outlives its critical section can
    land after its session ended or after a SUCCESSOR registered under the same
    key, and every attempt to detect that after the fact was itself racy. Awaiting
    a worker hop while the caller still holds the session registry lock satisfies
    both: the syscalls happen off the loop, and no start, end or successor can
    interleave, because they all serialise on that same lock.
    """
    if not session_key:
        return
    started_at = time.time()
    orphaned: list[tuple[str, float]] = []
    with _live_lock:
        previous = _live_starts.get(session_key)
        if previous is not None and previous != started_at:
            # A start SUPERSEDING a live generation of the same key. The displaced
            # generation's crumb is named after it, so once the table forgets it no
            # end can ever name it again -- reap it here or it survives to the next
            # boot as a crash that never happened. Under one shared filename this
            # case needed no handling, because the successor's write simply
            # overwrote the file.
            orphaned.append((session_key, previous))
        _live_starts[session_key] = started_at
        if len(_live_starts) > _MAX_LIVE_SESSIONS:
            # A registry this large is not real; drop the oldest half rather than
            # growing for the process lifetime. Costs samples, never memory.
            oldest = sorted(_live_starts.items(), key=lambda kv: kv[1])
            for stale_key, stale_at in oldest[: len(oldest) // 2]:
                if _live_starts.pop(stale_key, None) is not None:
                    orphaned.append((stale_key, stale_at))
    if _crumbs_enabled():
        try:
            # The orphaned generations ride the SAME worker hop as this start's own
            # write. Dropping a table entry destroys the only record of which
            # generation this process owns, so an orphaned crumb no end can name
            # would survive to the next boot and be reported as a crash that never
            # happened -- and doing those unlinks here rather than on the loop
            # keeps up to half a table's worth of filesystem I/O off it without
            # opening a second cancellation window.
            await asyncio.to_thread(_write_crumb, session_key, started_at, tuple(orphaned))
        except Exception:
            logger.debug("session start crumb handoff failed", exc_info=True)
    # Counted LAST, after the only cancellable point in this function. Emitted
    # before it, a cancellation during the handoff -- which the caller answers by
    # rolling the registry entry back and discarding the start -- would leave this
    # counter reporting a session that never existed, and `started` is the
    # denominator every session-scoped rate is read against.
    try:
        from kiro_crew.metrics.events import emit_counter

        emit_counter(SESSION_STARTED_COUNTER, {"session_source": _session_source(session_key)})
    except Exception:
        logger.debug("session started counter failed", exc_info=True)


async def discard_session_start(session_key: str) -> None:
    """Undo a start that never became a live session; never raises.

    For the cancellation window between registering a session and finishing its
    start record: the caller is about to hard-kill the provider and roll the
    registry back, so there is no session left to have a lifetime. Emits NOTHING
    -- a session that never lived has no duration worth reporting -- but it must
    still consume the crumb, because a crumb with no session behind it is read at
    the next boot as a crash that never happened.

    Deliberately not one of the ``end_reason`` values: those describe how a live
    session ENDED, and inventing a twelfth member for "never started" would put a
    non-session into the lifetime histogram's population.

    **Why this is a coroutine, in the one place an await looks least welcome.**
    Its unlink is the same syscall the end path has, on the same event loop, so it
    has to leave the loop for the same reason. That it does so inside a rollback
    handler -- code whose entire job is to guarantee the crumb is consumed -- is
    safe only because :func:`_unlink_generations` never raises and never drops the
    unlink: its hop is shielded, so a cancellation does not reach the queued work and
    the executor still runs it. A second cancellation arriving mid-rollback therefore
    cannot cost the handler its crumb.

    **It absorbs that second cancellation, as both end paths do.** Its caller
    re-raises the failure which started the rollback on the very next line, so
    letting a cancellation out here would DISPLACE that failure with one of this
    function's own making. That is the same rule
    :func:`record_session_ended` follows for the same underlying reason: every
    caller of these three has already passed its point of no return, and a
    cancellation arriving afterwards can only leave the teardown half done.
    """
    if not session_key:
        return
    with _live_lock:
        started_at = _live_starts.pop(session_key, None)
    if started_at is None:
        # Nothing of OURS to remove, so remove nothing. The crumbs sharing this
        # key's digest belong to other processes or to earlier runs of this one,
        # and deleting those is exactly the bug the generation-scoped name exists
        # to prevent -- a discard that cannot name its own generation has no crumb
        # to consume, because it never wrote one.
        return
    # Off the loop and outside the lock, for the reason record_session_ended gives.
    try:
        await _unlink_generations(((session_key, started_at),))
    except BaseException:
        logger.debug("session start discard interrupted", exc_info=True)


def _crumbs_enabled() -> bool:
    """True while telemetry consent is in force; never raises.

    The crumb exists solely to feed :data:`SESSION_DURATION_METRIC`, and that
    instrument is a no-op without consent -- so writing one on an unopted host
    would persist state that nothing can ever read, against a documented default
    of collecting nothing. Fails CLOSED: if consent cannot be determined, no file
    is written.
    """
    try:
        from kiro_crew.metrics.provider import get_recorder

        return bool(get_recorder().enabled)
    except Exception:
        logger.debug("crumb consent check failed; skipping crumb", exc_info=True)
        return False


def _is_current_generation(session_key: str, started_at: float) -> bool:
    """True while *started_at* is still the generation installed for *session_key*.

    False once the key has been popped by an end or a discard, or overwritten by a
    successor's start -- each a reason an in-flight crumb write must not stand.
    """
    with _live_lock:
        return _live_starts.get(session_key) == started_at


def _write_crumb(
    session_key: str,
    started_at: float,
    orphaned: tuple[tuple[str, float], ...] = (),
) -> None:
    """Persist *session_key*'s open-session crumb; never raises.

    **Runs on a worker thread and self-corrects, which is what makes the caller's
    await safe to cancel.** Cancelling an ``asyncio.to_thread`` abandons the await
    but does NOT stop the thread, so the caller's rollback can unlink before this
    write lands and leave an orphan crumb the next boot reports as a crash. Rather
    than make the await non-cancellable, this checks the generation on BOTH sides
    of the write: it declines to write once its own ``started_at`` is no longer the
    installed generation, and removes what it just wrote if that changed while the
    write was in flight -- so an end, a discard and a cancellation are all covered
    by the same test.

    **The post-write unlink removes only this writer's own file, and the NAME is
    what guarantees that.** Unlinking a shared path unconditionally is what made
    this unsafe before: while every generation of a key wrote to one path, a late
    predecessor could delete a successor's crumb, and the read-back check that
    tried to detect it afterwards was only ever a narrowing -- it could not help
    across processes at all. Now the path encodes the writer and the generation,
    so the file this removes cannot belong to anyone else.

    Takes ``_crumb_io_lock`` (crumb files) and, inside it, ``_live_lock`` (the
    table). That order is the only one used anywhere, so the two cannot deadlock:
    no path takes the table lock and then the I/O lock.
    """
    try:
        from kiro_crew.atomic_write import atomic_write

        path = _crumb_path(session_key, started_at)
        payload = json.dumps(
            {
                "key": session_key,
                "started_at": started_at,
                # WHO owns this session. Without it a crumb is only "some session
                # started before I booted", and the gateway's boot scan cannot tell
                # a casualty of the last run from a session running RIGHT NOW in a
                # sibling process -- `kirocrew run` and the eval runner each build
                # their own SessionManager against the same data home. Reaping one
                # of those both invents a crash and destroys the live crumb, so the
                # real crash that session may later suffer goes unrecorded.
                # ``start_id`` guards pid reuse: a recycled pid belongs to a
                # different process and must not look like this one.
                "pid": os.getpid(),
                "start_id": _own_start_id(),
            },
            ensure_ascii=True,
        )
        with _crumb_io_lock:
            # Generations this start displaced -- superseded under the same key, or
            # evicted by the cap -- go first, and unconditionally: they must be
            # removed even when this start's own write turns out to be superseded
            # below, because once the table has forgotten a generation nothing can
            # ever name its file again.
            for stale_key, stale_at in orphaned:
                _unlink(_crumb_path(stale_key, stale_at))
            if not _is_current_generation(session_key, started_at):
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, payload)
            if not _is_current_generation(session_key, started_at):
                _unlink(path)
    except Exception:
        logger.debug("session start crumb write failed", exc_info=True)


def _own_start_id() -> str:
    """This process's start identifier, or ``""`` when unavailable."""
    try:
        from kiro_crew import platform_compat

        return platform_compat.get_process_start_id(os.getpid()) or ""
    except Exception:
        return ""


def _owner_still_running(pid: object, start_id: object) -> bool:
    """True when *pid* is alive AND is still the process that wrote the crumb.

    Fails CLOSED -- an undecidable owner counts as RUNNING, so an ambiguous crumb
    is left on disk for a later boot rather than reaped into a fabricated crash.
    Losing a real crash sample costs one data point; inventing one corrupts the
    population this instrument exists to report.

    **Liveness comes from ``pid_exists``, identity only refines it.**
    ``get_process_start_id`` returns None on Windows and on any process it may not
    introspect, and its own contract says a None must NOT be read as a mismatch --
    so using it as the liveness test would judge every owner dead on an entire
    platform and reap live sibling sessions there. It is used only to catch pid
    REUSE, and only when both sides of the comparison are actually available --
    including reuse of THIS process's own pid, which a container restart turns
    from a freak collision into the normal case.
    """
    if not isinstance(pid, int) or pid <= 0:
        # A crumb from before ownership was recorded. It cannot be attributed, so
        # the cutoff is all that protects it -- treat it as not-running and let the
        # caller's cutoff decide, which is the pre-ownership behaviour.
        return False
    if pid == os.getpid():
        # NOT necessarily this process. A container hands the gateway the same pid
        # on every restart -- PID 1 -- so a crumb a crashed predecessor wrote
        # arrives here looking like one of our own. Answering True would skip it on
        # this boot and on every boot after, so its crash is never emitted and its
        # file is never cleaned: in a container that collision is deterministic,
        # not the coincidence it looks like on a developer's machine. Comparing the
        # start identifiers separates the two, and only when BOTH sides are
        # readable -- an unknown identity still fails closed to running.
        own = _own_start_id()
        if isinstance(start_id, str) and start_id and own:
            return start_id == own
        return True
    try:
        from kiro_crew import platform_compat

        if not platform_compat.pid_exists(pid):
            return False  # definitively gone: a casualty to be reported
        live_start_id = platform_compat.get_process_start_id(pid)
    except Exception:
        return True
    if not isinstance(start_id, str) or not start_id or not live_start_id:
        # SOMETHING holds that pid and the identities cannot be compared (Windows,
        # a pre-identity crumb, or a process we may not introspect). Fail closed.
        return True
    return live_start_id == start_id


async def record_session_ended(session_key: str, *, end_reason: str) -> None:
    """Emit *session_key*'s lifetime; never raises.

    **The pop and the sample stay in the same tick as the registry removal they
    report; only the unlink leaves it.** The start time is popped from the
    in-memory table and the histogram recorded in memory, both BEFORE this
    function's single suspension point -- so a teardown path still accounts for
    its session immediately after its ``_sessions.pop`` and before anything can
    interleave. The earlier version read the crumb off disk here, which forced the
    call to the end of teardown -- and a replacement session registering under the
    same key during those awaits then had its crumb consumed by its predecessor's
    teardown.

    Popping the table entry is also the double-count defence: the teardown paths
    are not mutually exclusive (the idle sweep calls ``reset``), so whichever
    reaches the session first is the one that records it, and a second call finds
    nothing.

    **Why the unlink is awaited off the loop rather than run inline.** Inline, it
    is one syscall against the data home on the event loop, which is nothing until
    that home is slow or network-backed and every gateway task parks behind one
    closing session. Queued fire-and-forget, it is dropped exactly when it matters
    -- see :func:`_unlink_generations`. Awaiting a worker hop is the only shape
    that is both off the loop and never dropped, and it is the shape
    :func:`record_session_started` already uses for the matching write. Callers
    hold the session registry lock across the await, so no start, end or successor
    can interleave with it either.

    **The sample is emitted BEFORE the hop, the opposite of the start path's
    counter, because the risks are opposite.** A cancelled start is rolled back
    and never existed, so counting it before its cancellable point would report a
    session that never was. An end cannot be rolled back -- the pop has already
    consumed the only record of this generation -- so the session HAS ended
    whatever happens next. Emitting first is therefore what keeps the sample when
    the await is cancelled, and it measures the lifetime at the pop rather than a
    worker round-trip later.

    **A cancellation at the hop is ABSORBED, not propagated, and that is what
    makes the new suspension point safe to give a teardown.** The registry pop is
    the caller's point of no return: past it the session is gone from the registry
    and only the caller's remaining cleanup can finish the job -- ``destroy``
    deletes the session-map entry in a ``finally`` BELOW this call, and
    ``discard_conversation`` and ``reset`` clear the resume SID on the line after
    the lock. Raising here would exit those callers before that cleanup, leaving a
    destroyed session's mapping behind for the next boot to resume: a cancellation
    cannot undo a teardown, it can only leave it half done, so the correct answer
    is to let the teardown finish. **The cancellation is genuinely DISCARDED, not
    deferred**: a swallowed ``CancelledError`` is not re-delivered by asyncio, so the
    caller runs to completion unless something cancels it again. That is the intended
    outcome rather than a side effect -- finishing a teardown that has already popped
    is what the callers' ``finally`` blocks exist for -- and it is what
    ``test_a_cancelled_end_lets_its_callers_post_pop_cleanup_run`` pins. The crumb is
    not at risk either way: :func:`_unlink_generations` shields its hop, so the
    executor still runs the unlink.

    Takes no lock across the hop, and must not take ``_crumb_io_lock`` at all:
    worker threads hold that across filesystem latency, so waiting on it here
    would park the loop on the very syscall this moved off it. No lock is needed
    either -- the backfill skips every crumb whose owner is live, so it never looks
    at one this process wrote, and an in-flight writer for this key self-corrects
    by re-checking the generation this function has just popped.
    """
    if not session_key or end_reason not in END_REASONS:
        return
    with _live_lock:
        started_at = _live_starts.pop(session_key, None)
    if started_at is None:
        # No generation of ours to consume, so nothing is unlinked. A removal with
        # no live entry never wrote a crumb in this process -- the entry was
        # evicted (which unlinks at eviction time) or the start was never recorded
        # -- and the files sharing this digest belong to other processes or earlier
        # runs. Unlinking those unconditionally was the sibling-clobbering bug.
        return
    try:
        _emit_duration(session_key, time.time() - started_at, end_reason)
    except Exception:
        logger.debug("session end emit failed", exc_info=True)
    # Consumed OUTSIDE the table lock and OFF the loop. Removing the file even when
    # the duration could not be emitted is deliberate -- the session is over, so its
    # crumb must not survive for the next boot to read as a crash. The path names
    # this writer and this generation, so no concurrent session's record is
    # reachable from here.
    try:
        await _unlink_generations(((session_key, started_at),))
    except BaseException:
        # Absorbed so the caller's post-pop cleanup still runs; see the docstring.
        logger.debug("session end crumb handoff interrupted", exc_info=True)


async def record_sessions_ended(session_keys: Iterable[str], *, end_reason: str) -> None:
    """Record the end of a whole drained set in ONE hop; never raises.

    The bulk counterpart of :func:`record_session_ended`, for the four paths that
    pop many keys at once: ``close_all``, ``drain_all_providers``,
    ``retire_kiro_identity_sessions`` and the provider-factory reload.

    **Why those paths need this rather than a loop of awaits.** Awaiting once per
    key would put a cancellation point BETWEEN two keys, and every key after it
    would be popped but unrecorded -- so its crumb survives and the next boot
    reports it as ``crashed``. On ``close_all``, a path cancellation reaches by
    design, that turns one orderly shutdown into a burst of fabricated failures in
    the exact population this instrument exists to measure: strictly worse than the
    on-loop unlink the await replaced. Popping the whole set under one lock hold and
    unlinking it in one hop keeps the property the inline version had -- either
    every key in the set is accounted for, or none of them is -- and costs one
    worker round-trip instead of one per session on the path that drains the entire
    registry.

    A cancellation at the hop is ABSORBED for the reason
    :func:`record_session_ended` gives: every caller here has already popped, and
    ``close_all``'s own remaining shutdown work must still run.

    *session_keys* is materialised before the lock is taken, so a caller may pass a
    view over the registry it is draining without this iterating it under the lock.
    """
    if end_reason not in END_REASONS:
        return
    keys = [key for key in session_keys if key]
    if not keys:
        return
    consumed: list[tuple[str, float]] = []
    with _live_lock:
        for key in keys:
            started_at = _live_starts.pop(key, None)
            if started_at is not None:
                consumed.append((key, started_at))
    if not consumed:
        return
    # One clock reading for the whole set: these sessions ended together, and
    # letting each sample drift by its position in the loop would report the
    # ordering of a teardown as a difference in lifetime.
    ended_at = time.time()
    for key, started_at in consumed:
        try:
            _emit_duration(key, ended_at - started_at, end_reason)
        except Exception:
            logger.debug("session end emit failed", exc_info=True)
    try:
        await _unlink_generations(consumed)
    except BaseException:
        # Absorbed so the caller's post-pop cleanup still runs; see the docstring.
        logger.debug("session end crumb handoff interrupted", exc_info=True)


def backfill_crashed_sessions(started_before: float | None = None) -> int:
    """Emit ``end_reason=crashed`` for crumbs no teardown path consumed.

    *started_before* is an epoch cutoff: a crumb whose ``started_at`` is at or
    after it is left untouched. Callers pass their own process start time, which
    is what makes this safe to run OFF the boot path -- without the cutoff the
    scan had to complete before this process opened its first session, or it
    would back-fill a crumb it had just written as though a previous run had
    crashed. With it, ordering is irrelevant: only crumbs that predate this
    process can ever be claimed. ``None`` disables the cutoff (tests).

    Returns the number of samples emitted (0 on any failure); never raises.
    """
    emitted = 0
    try:
        crumb_dir = _crumb_dir()
        if not crumb_dir.is_dir():
            return 0
        for path in sorted(crumb_dir.glob("*.json")):
            # Read, judge and unlink under ONE lock hold, which now serialises this
            # scan against THIS process's own crumb writer rather than guarding an
            # identity race. The race it used to guard is gone: a path names one
            # writer and one generation, so a session registering mid-scan gets a
            # file of its own and the successor this once could have deleted no
            # longer shares a name with anything here.
            with _crumb_io_lock:
                key, started_at, pid, start_id = _read_crumb(path)
                if _owner_still_running(pid, start_id):
                    # A LIVE session, in this process or a sibling. Reaping it
                    # would both invent a crash and delete the evidence of the
                    # real one it may yet suffer.
                    continue
                if (
                    started_at is not None
                    and started_before is not None
                    and started_at >= started_before
                ):
                    # Pre-ownership crumb from this run: still live, not a casualty.
                    continue
                _unlink(path)
            if not key or started_at is None or emitted >= _MAX_BACKFILL_EMITS:
                continue
            ended_at = _last_activity(key)
            if ended_at is None:
                continue
            if _emit_duration(key, ended_at - started_at, END_REASON_CRASHED):
                emitted += 1
    except Exception:
        logger.debug("crashed-session backfill failed", exc_info=True)
    return emitted


def _emit_duration(session_key: str, seconds: float, end_reason: str) -> bool:
    """Record one histogram sample. Returns whether a sample was emitted.

    A non-positive lifetime is skipped rather than recorded, for the reason
    ``metrics/turns.py`` gives: an absent sample reads as "no data" on the
    Telemetry page, while a recorded 0 renders as a plausible 0ms lifetime.
    """
    if seconds <= 0:
        return False
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().histogram(
            SESSION_DURATION_METRIC,
            seconds * 1000.0,
            unit="ms",
            attrs={"end_reason": end_reason, "session_source": _session_source(session_key)},
            description="Session lifetime (ms), by how the session ended.",
        )
        return True
    except Exception:
        logger.debug("session duration emit failed", exc_info=True)
        return False


def _read_crumb(path: Path) -> tuple[str, float | None, object, object]:
    """Return ``(session_key, started_at, pid, start_id)`` from *path*.

    ``("", None, None, None)`` when the file is missing, unreadable or malformed.
    ``pid`` / ``start_id`` are absent on crumbs written before ownership existed.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", None, None, None
    if not isinstance(payload, dict):
        return "", None, None, None
    key = payload.get("key")
    started = payload.get("started_at")
    if not isinstance(key, str) or not isinstance(started, (int, float)):
        return "", None, None, None
    return key, float(started), payload.get("pid"), payload.get("start_id")


def _last_activity(session_key: str) -> float | None:
    """Mtime of *session_key*'s transcript -- when it was last observably alive.

    ``None`` when the session has no transcript on disk, which is normal rather
    than an error: a subagent run leaves only a replay log. The caller skips the
    emit in that case (see the module docstring).
    """
    try:
        from kiro_crew.config.paths import config_dir
        from kiro_crew.history import SESSIONS_DIR_NAME, transcript_stem

        path = config_dir() / SESSIONS_DIR_NAME / f"{transcript_stem(session_key)}.jsonl"
        return path.stat().st_mtime
    except Exception:
        return None


async def _unlink_generations(generations: Sequence[tuple[str, float]]) -> None:
    """Consume the crumbs of *generations* on a worker thread; never raises.

    **The hop is AWAITED, not queued, and that distinction is the whole reason the
    unlink stayed on the loop for as long as it did.**
    ``shutdown_maintenance_executor`` drains with ``cancel_futures=True``, so a
    fire-and-forget unlink is dropped at ``close_all`` -- exactly when teardown is
    heaviest -- and the crumb of a cleanly ended session then survives to the next
    boot to be back-filled as ``crashed``. A dropped unlink is worse than a slow
    one, because it manufactures a failure in the one population this instrument
    exists to measure. Awaiting keeps the syscall off the loop without ever
    dropping it, and the caller holds the session registry lock across it, so
    nothing can interleave either.

    **Cancelling the await must neither drop the unlink nor put it back on the
    loop, and a shielded bare executor future is the only shape that does both.**
    Three rounds of review narrowed this to one line, so the reasoning is recorded
    in full.

    ``asyncio.to_thread`` submits before it suspends, but submitted is not started:
    on a saturated executor the work item is still queued, and cancelling the await
    reaches the underlying ``concurrent.futures.Future`` through
    ``futures._chain_future``'s ``_call_check_cancel``, which cancels a work item
    that has not begun. That is the dropped unlink this whole design exists to
    prevent -- the crumb survives to the next boot and a cleanly ended session is
    reported as ``crashed``. Unlinking inline in the cancellation handler fixed the
    drop but put a filesystem syscall back on the event loop, which is the thing
    #7537 is about.

    So the hop is a BARE ``run_in_executor`` future, awaited through
    :func:`asyncio.shield`. Cancelling the shield leaves the inner future alone, so
    nothing cancels the queued work and it runs when a thread frees up. Two details
    make abandoning it safe, and both are why this is not the fire-and-forget shape
    the start path forbids:

    * It is a Future, NOT a Task. ``asyncio.runners._cancel_all_tasks`` at loop
      teardown iterates ``all_tasks()``, which holds Tasks only -- so shielding a
      ``to_thread`` COROUTINE would be wrong, because ``shield`` would wrap it in a
      Task that loop teardown cancels, dropping a still-queued item all over again.
      A bare future survives that sweep, and ``shutdown_default_executor(wait=True)``
      then drains the queue; even without it, ``ThreadPoolExecutor``'s ``atexit``
      hook runs queued items before its sentinel.
    * A worker finishing after the loop closed cannot raise "Event loop is closed"
      -- ``_chain_future._call_set_state`` returns early when ``dest_loop.is_closed()``
      rather than calling ``call_soon_threadsafe``. That failure (see
      ``test_the_crumb_write_is_never_a_loop_bound_future``) was a start-path write
      that was never awaited at all; here the normal path awaits to completion and
      only a cancellation abandons the future.

    The start path still must not do this: its write has to be ORDERED under the
    caller's registry lock, so it cannot be allowed to land late. An end's unlink
    can, because the crumb name encodes the writer and the generation -- a late
    unlink can only ever reach the file its own session wrote.

    This helper therefore never raises, so it cannot abort a caller mid-teardown
    either; each end path keeps its own guard for the reason
    :func:`record_session_ended` documents.

    Takes no lock. Each path names this process and one generation, and the
    backfill skips every crumb whose owner is live, so the files this removes and
    the files that scan reads are disjoint sets.
    """
    if not generations:
        return
    paths = [_crumb_path(session_key, started_at) for session_key, started_at in generations]
    try:
        hop = asyncio.get_running_loop().run_in_executor(None, _unlink_all, paths)
    except Exception:
        # No worker to hop onto -- the default executor is already shut down, which
        # only happens once the loop itself is being torn down. Left for the boot
        # backfill rather than unlinking on the loop; its ownership check means the
        # crumb is only ever claimed once this process is gone.
        logger.debug("session crumb unlink could not be scheduled", exc_info=True)
        return
    try:
        await asyncio.shield(hop)
    except BaseException:
        # The shield was cancelled, not the hop: the executor still owns the work
        # and runs it. Nothing is retried here, and nothing runs on the loop.
        logger.debug("session crumb unlink hop abandoned to its worker", exc_info=True)


def _unlink_all(paths: Sequence[Path]) -> None:
    """Unlink every path in one worker hop; never raises.

    One hop for the whole set rather than one per crumb: a mass teardown must not
    pay a worker round-trip per session, and the set is already indivisible for the
    caller (see :func:`record_sessions_ended`).
    """
    for path in paths:
        _unlink(path)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("session crumb unlink failed for %s", path.name, exc_info=True)
