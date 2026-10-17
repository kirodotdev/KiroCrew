"""End the processes and sessions a cron run holds, bounded.

A run's session keys are fenced, each key is reset once and whatever the reset
could not stop is killed by its :class:`~kiro_crew.process_identity.ProcessHandle`
(never by a bare pid), over at most ``_ENDING_ROUNDS`` rounds, so a key the run
registers meanwhile is ended too or named as a kill failure.

Each function here is a :class:`~kiro_crew.cron.CronService` member: the class binds
it by name, so it reads and writes the service's own state on the instance, and a
patch of the member on the class or on an instance reaches every caller. A name
tests patch on :mod:`kiro_crew.cron`, or one that module defines, is read through
it on each call (``seams``).

Instance fields read: ``_sessions``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from kiro_crew.cron_service.claims import _RunClaim
from kiro_crew.process_identity import (
    ProcessHandle,
    add_handle,
    ending_fence,
    join_failures,
    kill_each,
    kill_set,
    process_handle_of,
    process_survived_async,
    refresh_live_process_tree,
    release_teardown_lease,
    spawn_in_flight,
    teardown_barriers,
    teardown_capture,
)
from kiro_crew.runtime_ownership import authorize_runtime_kill

if TYPE_CHECKING:
    from kiro_crew.cron import CronService

#: The cron service's logger: operators filter, and tests ``caplog``, on ``kiro_crew.cron``.
logger = logging.getLogger("kiro_crew.cron")


def _session_process_handles(self: CronService, session_key: str) -> list[ProcessHandle]:
    """Every process the key names at snapshot time, the torn-down one first.

    Read BEFORE the reset by ``_force_reap`` and ``cancel`` (see
    :class:`kiro_crew.process_identity.ProcessHandle` for why the reset itself loses it).

    A miss in the live map is not yet "no process". The run's OWN teardown
    reset -- the run body's ``finally`` in the gateway, or the deferred reset
    a late sub-agent completion triggers -- pops the session out of the map
    before the awaits that can hang, so a reap or cancel that arrives after
    that pop finds nothing under the key while that reset still holds the
    process. That is the ordinary shape of a run that hangs in its teardown,
    not a timing corner: the reaper's cancel of the run task is what ends the
    hung reset, and nothing else re-examines the process once the audit is
    written. The session manager keeps every popped session readable through
    ``tearing_down`` for exactly the life of its teardown (one entry per
    teardown in flight, released when it ends however it ends -- that cancel
    included), so a successor whose own reset popped it and hung beside the
    run's teardown is a candidate too.

    Two sessions can stand under one key at once: that torn-down one, and a
    live successor a cold start registered under the key during the
    teardown's awaits (a sub-agent completion delivering into the key). The
    run's process is the torn-down one, but the reap ends the KEY: it resets
    whatever is live under it too, so both are candidates, each verified and
    killed on its own handle -- preferring either alone would leave the
    other's process unrecorded (a torn-down run process abandoned mid-hung
    shutdown by the reap's cancel, or a live successor a completed reset
    failed to stop). Distinct process incarnations only: the same ``(pid,
    start id)`` under both is one handle, the same pid under two start ids
    is two (:meth:`_handles_of`). Empty when neither exists: nothing to kill.
    """
    if not self._sessions:
        return []
    return self._handles_of(self._sessions_under(session_key))


def _sessions_under(self: CronService, session_key: str) -> list[tuple[Any, ProcessHandle]]:
    """The sessions the key names right now, each with its kill handle: every torn-down one first, in pop order, then the live one.

    The torn-down ones are the sessions a reset popped under the key and
    still holds (``SessionManager.tearing_down``): the run's OWN teardown's,
    and any successor's whose own reset popped it and hung while the first
    teardown still ran -- each is a process the reap must answer, and
    retaining the first alone hid the successor's behind a record that said
    ``reaped``. Their handle is the one the retention captured AT THE POP
    (:class:`kiro_crew.session_lifecycle.TornDown`), never a re-read of the
    session: the teardown that holds them clears the provider's pid in its
    own awaits (the ACP client's reset, after a kill it could not confirm,
    then hangs on the transport), so a handle read now says the session
    names no process while the process stands, and the reap that trusted it
    recorded ``reaped`` over it. The live one is whatever the map holds
    under the key -- a successor a cold start registered during those
    teardowns -- with its handle read now, before the reset pops it. Read
    twice by :meth:`_end_run_processes`: once for the snapshot the kill
    works from, and once after the pass to tell a session the pass has not
    handled (a registration that landed after the reset's pop) from the ones
    it has -- by identity, since a session is handled once its process was
    answered, whatever key it sits under now. An entry of another shape (a
    double's default return) is not a session.
    """
    if not self._sessions:
        return []
    tearing_down = getattr(self._sessions, "tearing_down", None)
    torn = tearing_down(session_key) if callable(tearing_down) else None
    pairs: list[tuple[Any, ProcessHandle]] = []
    if isinstance(torn, list):
        for entry in torn:
            session = getattr(entry, "session", None)
            handle = getattr(entry, "handle", None)
            if session is not None and isinstance(handle, ProcessHandle):
                pairs.append((session, handle))
    live = self._sessions._sessions.get(session_key)
    if live:
        pairs.append((live, process_handle_of(live)))
    return pairs


def _handles_of(pairs: list[tuple[Any, ProcessHandle]]) -> list[ProcessHandle]:
    """One kill handle per distinct process incarnation among ``pairs``, in order.

    A handle with no pid names no process (the session had never spawned,
    or its client was already reset, when the handle was read).
    Distinctness is the handle's own identity, ``(pid, start id)``: two
    sessions naming the same incarnation are one handle -- their child records
    and group merged (``process_identity.add_handle``), since the two readings
    were taken at different moments -- while the same pid under two start ids
    is two processes -- a successor the kernel handed a dead predecessor's
    number -- and both are kept. A pid alone is never the key.
    """
    handles: list[ProcessHandle] = []
    for _session, candidate in pairs:
        if candidate.pid is None:
            continue
        add_handle(handles, candidate)
    return handles


async def _end_run_processes(
    self: CronService, session_key: str, *, job_id: str, who: str
) -> str | None:
    """Reset ``session_key`` and kill what the reset could not stop -- for every session that lands under it, bounded; the caller holds the key's ending fence.

    Runs under the key's ENDING FENCE (``SessionManager.ending_key``), which
    the caller -- ``_force_reap``, ``cancel()`` -- raises synchronously
    before awaiting this (so the first snapshot below is read with the fence
    already up) and holds until the run's terminal record and audit are
    written. While it is up, a claim or a new allocation under the key is
    HELD at the door of ``get_or_create`` -- it lands once the fence lifts,
    under a key whose run is recorded, so a sub-agent completion that races
    the reap is delivered after the record, never dropped and never into a
    key that is neither being ended nor recorded -- and an allocation
    already in flight when it went up -- a late sub-agent completion
    cold-starting the parent key again, caught inside ``provider.start()``
    with nothing published yet that any snapshot could see -- is
    invalidated: refused at registration when its start returns, fence up
    or lifted, with the provider it started hard-killed there by the
    closing manager's own path, and its call re-allocated after the lift.
    Without the fence that start published after the passes, with its
    process and the completion's injection running on behind a record that
    said ``reaped``. A start still past its spawn door when the passes end
    (:meth:`_spawn_in_flight`) is therefore reported, not waited for (its
    start may take seconds, and the fence has already settled what happens
    to it): a named kill failure, so the audit says ``failed`` -- while a
    claim merely waiting behind the key, or a cold start still ahead of that
    door, has started nothing and is not named. Nothing here lifts the
    fence: a registration after the passes is a new life under the key, not
    the run's process, and it lands only once the record it follows is
    written.

    One pass (:meth:`_reset_and_kill_once`) answers the sessions it saw: the
    snapshot (:meth:`_sessions_under`) and the session the reset popped. It
    is the ONLY pass, because nothing can land under the key after the pop
    while the fence is up: every door into the live map either meets the
    fence -- ``get_or_create``, at its front door, again after its busy-turn
    wait and again at registration, for a cold start and a warm-pool claim
    alike -- or never registers under a cron key at all
    (``open_task_session`` publishes the task runner's own ``taskrunner:``
    keys and refuses a key being ended at its door; the background session
    publishes its own key), and every cron key is ``cron:``-prefixed. A
    second reset-and-kill pass for a registrant the fence let through had
    no trigger on that path and is gone. The key is still read once more
    after the pass: a session under it that the pass did not handle (by
    identity -- the sessions the snapshot and the pop capture named, kept
    referenced so the comparison holds) is a registration the fence did not
    stop -- a manager without the fence, or a door this reasoning missed --
    and is a NAMED kill failure: reported, never reset, never signalled, so
    the audit says ``failed``, not ``reaped``. Returns every failure,
    joined, or None once every session under the key was answered and no
    start is past its spawn door under it.
    """
    if self._sessions:
        # Record the children the run's turns started, so they can vouch for
        # the group once the leader exits (see ``refresh_live_process_tree``).
        await refresh_live_process_tree(self._sessions, session_key)
    pairs = self._sessions_under(session_key)
    failure, popped = await self._reset_and_kill_once(session_key, pairs, job_id=job_id, who=who)
    handled = [session for session, _handle in pairs]
    handled.extend(session for session in popped if not any(session is s for s in handled))
    late = [
        (session, handle)
        for session, handle in self._sessions_under(session_key)
        if not any(session is s for s in handled)
    ]
    late_failure: str | None = None
    if late:
        # Not chased: whatever landed under the key past the fence is
        # reported, and the run is not recorded as reaped over it.
        named = ", ".join(
            f"pid {handle.pid}" if handle.pid is not None else "no process handle yet"
            for _session, handle in late
        )
        logger.error(
            "%s: a session registered under %s after the reset's pop for cron %s (%s); "
            "not reset, not signalled",
            who,
            session_key,
            job_id,
            named,
        )
        late_failure = (
            f"a session registered under the key after the reset's pop ({named}); "
            "not reset, not signalled"
        )
    return join_failures(
        failure, late_failure, self._spawn_in_flight(session_key, job_id=job_id, who=who)
    )


def _fence_run_keys(self: CronService, job_id: str, run: _RunClaim, fences: ExitStack) -> list[str]:
    """Raise the ending fence on every key ``run`` holds, synchronously, and return them newest first.

    Called by ``_force_reap`` and ``cancel()`` on entry to their fenced
    block, before any await: each fence is up before that key's first
    snapshot is read (see :meth:`_end_run_processes`), and ``fences`` lifts
    them all when the block ends -- after the run's terminal record and
    audit. A run that registered no key (a persistent job before
    registration, a script or command job, a legacy caller) is ended on the
    job's stable key ``cron:<job id>``, which then also names its record.
    """
    keys = self._run_session_keys(job_id, run) or [f"cron:{job_id}"]
    for key in keys:
        fences.enter_context(ending_fence(self._sessions, key))
    return keys


async def _end_run_sessions(
    self: CronService,
    job_id: str,
    run: _RunClaim,
    keys: list[str],
    fences: ExitStack,
    *,
    who: str,
) -> tuple[str | None, list[str]]:
    """End every key of the run -- ``keys`` first, then any the run registers meanwhile, bounded -- and return the joined failure and the keys actually ended.

    ``keys`` are the run's keys the caller fenced on entry
    (:meth:`_fence_run_keys`); each is reset and its processes killed through
    :meth:`_end_run_processes`, and a key every session of which was answered
    is retired from the registry (:meth:`clear_active_session_key`), as the
    gateway retires a key its own reset completed. The list returned is
    exactly those keys -- the ones whose ending returned no failure -- and
    is what the audit's ``session_keys`` says was ended: a key whose kill was
    refused or failed is NOT in it (it stays registered, and its failure is
    in the record's ``last_error``), and it is not ended a second time
    either, because a key any round has attempted is never read back as a
    late registration. The keys of a round are
    ended CONCURRENTLY (``asyncio.gather``): each key's ending is bounded on
    its own (one reset of ``_REAPER_RESET_TIMEOUT`` and verified kills), and
    the fences stay up until the record is written, so a run with many keys
    -- one per agent of a sequential job -- ended one after another would
    hold its fences for the SUM of those bounds, past
    ``session_allocation.ENDING_FENCE_WAIT_SECS``, and a completion held at
    the door would be refused for a teardown that was merely long; ended
    together, the fences are up for one key's bound -- per round
    ``_REAPER_RESET_TIMEOUT`` plus the kill's own bounds (under 40 s),
    ``_ENDING_ROUNDS`` rounds at most, so well inside the 180 s a held
    caller waits. A key whose ending RAISED (a cancellation of the
    reaper task at shutdown is what can) interrupts no other key's: every
    ending of the round completes before the first exception is re-raised
    to the caller, which records it as the run's kill failure and re-raises
    it after the record, as for one key. The run's
    task is still alive while its keys are ended -- the caller cancels it
    only after the passes -- so a sequential run whose hung agent's session
    is reset can move on and register its NEXT agent's key while an earlier
    key is still being ended: after the round the run's keys are read again,
    a key no round has attempted is fenced and ended too, and the chase is
    bounded by ``_ENDING_ROUNDS`` rounds. A key the run registers after
    the last round is a named kill failure -- reported, never reset, never
    signalled -- so the audit says ``failed``, not ``reaped``.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    ended: list[str] = []
    attempted: list[str] = []
    failures: list[str | None] = []
    pending = list(keys)
    for round_no in range(1, seams._ENDING_ROUNDS + 1):
        results = await asyncio.gather(
            *(self._end_run_processes(key, job_id=job_id, who=who) for key in pending),
            return_exceptions=True,
        )
        raised: BaseException | None = None
        for key, result in zip(pending, results):
            attempted.append(key)
            if isinstance(result, BaseException):
                if raised is None:
                    raised = result
                else:
                    logger.error(
                        "%s: ending %s for cron %s also raised",
                        who,
                        key,
                        job_id,
                        exc_info=result,
                    )
                continue
            failures.append(result)
            if result is None:
                # Ended: every session under the key was answered. A key
                # whose kill was refused or failed is neither listed as
                # ended nor retired -- the record says ``failed`` over it.
                ended.append(key)
                self.clear_active_session_key(job_id, key)
        if raised is not None:
            # Every ending of the round is over (``return_exceptions``): no
            # sibling is left running under a fence about to lift.
            raise raised
        pending = [key for key in self._run_session_keys(job_id, run) if key not in attempted]
        if not pending:
            break
        if round_no == seams._ENDING_ROUNDS:
            # Not chased: whatever the run keeps registering is reported,
            # and the run is not recorded as reaped over it.
            named = ", ".join(pending)
            logger.error(
                "%s: cron %s registered %s after %d rounds of resets; not reset, not signalled",
                who,
                job_id,
                named,
                seams._ENDING_ROUNDS,
            )
            failures.append(
                f"the run registered {named} after {seams._ENDING_ROUNDS} rounds of resets; "
                "not reset, not signalled"
            )
            break
        logger.warning(
            "%s: cron %s registered %s while its keys were being ended; "
            "ending it too (round %d of %d)",
            who,
            job_id,
            ", ".join(pending),
            round_no + 1,
            seams._ENDING_ROUNDS,
        )
        for key in pending:
            fences.enter_context(ending_fence(self._sessions, key))
    return join_failures(*failures), ended


def _spawn_in_flight(self: CronService, session_key: str, *, job_id: str, who: str) -> str | None:
    """The named failure for a cold start past its spawn door under the fenced key, or None -- logged under the job.

    The read itself, and what it names, is
    :func:`kiro_crew.process_identity.spawn_in_flight` (the sub-agent
    manager's reap reads it the same way); this adds the cron job to the log.
    """
    failure = spawn_in_flight(self._sessions, session_key)
    if failure is not None:
        logger.error("%s: %s -- under %s for cron %s", who, failure, session_key, job_id)
    return failure


async def _reset_and_kill_once(
    self: CronService,
    session_key: str,
    pairs: list[tuple[Any, ProcessHandle]],
    *,
    job_id: str,
    who: str,
) -> tuple[str | None, list[Any]]:
    """One reset of the key, then the kill of what it could not stop; the failure and the popped sessions.

    ``pairs`` is the snapshot -- the session objects the key named before
    the reset, each with its kill handle: the one the retention captured at
    its pop for a torn-down session, the one read now for the live one --
    whose kill handles are taken FIRST: the reset pops the
    session from the map before it can hang, so a kill that looked the key
    up afterwards would find nothing and leave the process it names running
    (see :class:`kiro_crew.process_identity.ProcessHandle`). The snapshot is
    keyed by name, so the session the reset ACTUALLY pops is captured at the
    pop itself (:func:`kiro_crew.process_identity.teardown_capture`): a cold
    start can register a new session under the key between the snapshot and
    the pop, and that one is
    what a hung reset then holds. A reset that hung or failed gets every
    handle killed; a completed one -- True, or False for a key a concurrent
    reset had already popped (the common False is the run's OWN teardown
    reset, popped before the caller looked and hung since; the caller's
    ``RunClaims.finish_taken`` cancel is what ends it) -- is not proof the
    process is gone (its own shutdown can fail without raising out of it; a
    False one stopped nothing), so every handle is asked (pid + recorded
    start id, then the tree it led: ``process_survived``) and a process still
    standing gets the fallback, whose report is what the record says. A
    popped session with no handle is a failure, not nothing.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._sessions:
        return None, []
    handles = self._handles_of(pairs)
    reset_kwargs, popped = teardown_capture(self._sessions, session_key)
    seen = [session for session, _handle in pairs]
    failure: str | None = None
    try:
        await asyncio.wait_for(
            # Same class for the reaper and cancel: the run is over, so its
            # conversation is over and its sub-agent runs end with it -- not
            # a recycle.
            self._sessions.reset(session_key, ends_conversation=True, **reset_kwargs),
            timeout=seams._REAPER_RESET_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("%s: reset hung for cron %s, attempting SIGKILL", who, job_id)
        targets, missing = kill_set(handles, popped, seen=seen)
        failure = join_failures(
            await self._sigkill_sessions(session_key, targets, who=who, popped=popped),
            missing,
        )
    except Exception:
        logger.exception("%s: reset failed for cron %s, attempting SIGKILL", who, job_id)
        targets, missing = kill_set(handles, popped, seen=seen)
        failure = join_failures(
            await self._sigkill_sessions(session_key, targets, who=who, popped=popped),
            missing,
        )
    else:
        targets, missing = kill_set(handles, popped, seen=seen)
        # Off the loop: the scan walks every recorded child.
        survivors = [handle for handle in targets if await process_survived_async(handle)]
        if survivors:
            logger.warning(
                "%s: process survived the reset for cron %s, attempting SIGKILL", who, job_id
            )
            failure = await self._sigkill_sessions(session_key, survivors, who=who, popped=popped)
        failure = join_failures(failure, missing)
    return failure, [session for session, _handle in popped]


async def _sigkill_sessions(
    self: CronService,
    session_key: str,
    handles: list[ProcessHandle],
    *,
    who: str = "Reaper",
    popped: list[tuple[Any, ProcessHandle]] | None = None,
) -> str | None:
    """Kill every handle's process (:func:`kiro_crew.process_identity.kill_each`); the failures joined, or None.

    ``popped`` is the caller's captured pop, forwarded so each kill can release
    the lease of the session its own reset destroyed even when that reset was
    cancelled before ``provider.shutdown()`` -- the case where the manager's
    torn-down table has already been unwound and holds nothing.
    """
    return await kill_each(
        handles,
        lambda handle: self._sigkill_session(session_key, handle, who=who, popped=popped),
    )


async def _sigkill_session(
    self: CronService,
    session_key: str,
    handle: ProcessHandle | None,
    *,
    who: str = "Reaper",
    popped: list[tuple[Any, ProcessHandle]] | None = None,
) -> str | None:
    """Best-effort SIGKILL when the graceful reset hangs, fails, or left the process standing.

    ``handle`` is one process handle the caller took before the reset
    (:meth:`_session_process_handles`), and it is the ONLY thing that names
    the process: the map is never consulted here (a session under the key
    now is a successor registered during the reset's awaits, verified and
    killed on its own handle by the caller), and ``session_key`` names the
    run in the log only, prefixed with ``who`` -- the caller's name. ``None``
    means no session was live, or being torn down, under the key before the
    reset: nothing to kill, not a failure. The kill itself -- the two
    start-id reads around the child walk, the group signal (by the group id
    retained while the leader was alive once the leader is gone), the
    pid-scoped fallback, the escaped-children sweep -- is
    :func:`kiro_crew.process_identity.kill_verified_process`, the one
    definition this method shares with the sub-agent manager's teardown
    paths. It never
    raises (the caller took the run's claim and must still finish it) and
    never swallows: it returns what stopped the kill, and the caller records
    it so the audit does not say the run was reaped over a process tree left
    alive.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    if not self._sessions:
        return None  # no session manager: nothing to kill
    if handle is None:
        # Nothing to kill, not a failure: no session was live, or being
        # torn down, under the key before the reset, so the run has no
        # process group to answer for. The map is deliberately not read:
        # whatever it holds under the key now was registered after the
        # snapshot -- a successor, not this run's process.
        logger.warning("%s: no session found for %s", who, session_key)
        return None
    # Ownership, asked once before the escalation below starts. The gate answers
    # from two tables and refuses on either: a LEASE, held by the session that
    # owns the runtime, and a TENANCY, held by a party mid-flight on the process
    # without owning it. At cap=1 a session-sharing sub-agent takes no lease --
    # an acquisition cannot join an occupied runtime -- so the tenancy table is
    # what speaks for it, and the gate reads that too. A refusal for a tenant is
    # not this caller's mistake: the answer is to let the tenant finish, and its
    # own last release hands the orphan back for teardown.
    #
    # The run's OWN lease is released first, and that ordering is the whole
    # correctness of the gate here. A reset releases the lease inside
    # ``provider.shutdown()``, so every await before it -- the ended-record
    # write, the queue unlink, the child probes -- is a point where the
    # teardown can hang or raise and land on this path with the lease still
    # held. Asking the gate then would let the session being destroyed refuse
    # its own last-resort kill: the wedged process would survive, and its pid
    # would go on being refused by every sweep for the gateway's life while
    # the run recorded a false "leased by another tenant". The rule this
    # follows is the one the allocation path's own failure handler states --
    # release before the kill, or the cleanup refuses its own teardown.
    #
    # What survives the release is a lease held by a DIFFERENT tenant, which
    # is the only thing that may withhold the signal. A refusal is then
    # reported the same way a refused pid is: as the thing that stopped the
    # kill, so the run is never recorded as reaped over a process tree that is
    # still standing.
    # The captured pop goes with it: a reset this run abandoned on its timeout
    # has already unwound the scope that made the subject readable through
    # ``tearing_down``, and the pop the caller holds is then the only thing that
    # still names the session whose lease must go before the gate is asked.
    await release_teardown_lease(self._sessions, session_key, handle, who=who, popped=popped)
    handle_pid = getattr(handle, "pid", None)
    if isinstance(handle_pid, int) and not authorize_runtime_kill(
        handle_pid,
        reason=f"cron run teardown for {session_key}",
        caller="cron._sigkill_session",
    ):
        logger.warning("%s: %s still leased by another tenant; not signalling it", who, session_key)
        return "runtime still leased by another tenant"
    # The client's child-tree probe, record capture and escaped-children sweep,
    # resolved through the session module at call time (circular import:
    # session → cron; and a test's patch of the client module is what the
    # sweep must run). Cron itself never reaches the ACP layer.
    from kiro_crew.session import child_process_helpers

    # The gate's answer above is separated from the first signal by the verified
    # kill's own start-id read, group resolution and descendant walk. The barrier
    # makes it current and shuts that window; a pid it does not grant has gained a
    # tenant, and the refusal is reported the same way a refused pid is.
    with teardown_barriers([handle_pid], who=who) as barriered:
        if isinstance(handle_pid, int) and not barriered:
            logger.warning(
                "%s: %s gained a tenant after the gate allowed it; not signalling it",
                who,
                session_key,
            )
            return "a tenant claimed the runtime after the gate allowed it"
        return await seams.kill_verified_process(
            handle, who=who, key=session_key, child_helpers=child_process_helpers()
        )
