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

    A miss in the live map is not "no process": the run's own teardown reset
    pops the session before the awaits that can hang, so a reap arriving after
    the pop finds nothing under the key while that reset still holds the
    process. That is the ordinary shape of a run hung in teardown. The session
    manager keeps each popped session readable through ``tearing_down`` for
    the life of its teardown, so those are candidates too.

    A live successor a cold start registered during the teardown can stand
    under the key as well. The reap ends the KEY, so every one is a candidate,
    each verified and killed on its own handle; preferring one would leave the
    other's process unrecorded. One handle per ``(pid, start id)``
    (:meth:`_handles_of`). Empty when there is nothing to kill.
    """
    if not self._sessions:
        return []
    return self._handles_of(self._sessions_under(session_key))


def _sessions_under(self: CronService, session_key: str) -> list[tuple[Any, ProcessHandle]]:
    """The sessions the key names right now, each with its kill handle: every torn-down one first, in pop order, then the live one.

    The torn-down ones are every session a reset popped under the key and
    still holds (``SessionManager.tearing_down``); each is a process the reap
    must answer. Their handle is the one captured AT THE POP
    (:class:`kiro_crew.session_lifecycle.TornDown`), never a re-read: the
    teardown clears the provider's pid in its own awaits, so a handle read now
    could say "no process" while the process stands. The live one is whatever
    the map holds under the key, its handle read now.

    :meth:`_end_run_processes` reads this twice: once for the kill snapshot,
    and once after the pass to find, by identity, a session that landed after
    the pop. An entry of another shape (a double's default return) is skipped.
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

    A handle with no pid names no process and is skipped. Identity is
    ``(pid, start id)``, never the pid alone: two readings of one incarnation
    merge into one handle (``process_identity.add_handle``), while a reused pid
    under a new start id is a second process and is kept.
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
    ``_force_reap`` / ``cancel()`` raise synchronously before awaiting this and
    hold until the run's record and audit are written. While it is up, a new
    claim or allocation under the key is HELD at ``get_or_create`` and lands
    after the record, so a racing sub-agent completion is delivered late, never
    dropped. An allocation already inside ``provider.start()`` is refused at
    registration and its provider hard-killed. A start still past its spawn
    door when the pass ends (:meth:`_spawn_in_flight`) is reported as a named
    kill failure, not waited for; a claim still ahead of that door started
    nothing and is not named. Nothing here lifts the fence.

    One pass (:meth:`_reset_and_kill_once`) is enough: with the fence up, every
    door into the live map either meets it or never registers a ``cron:`` key.
    The key is still read once more: a session the pass did not handle (by
    identity) got past the fence and is a NAMED kill failure, never reset or
    signalled, so the audit says ``failed``, not ``reaped``. Returns every
    failure joined, or None when every session was answered.
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

    Called by ``_force_reap`` and ``cancel()`` before any await, so each fence
    is up before that key's first snapshot; ``fences`` lifts them after the
    run's record and audit. A run with no registered key is ended on the job's
    stable key ``cron:<job id>``.
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

    ``keys`` were fenced by :meth:`_fence_run_keys`. Each is ended through
    :meth:`_end_run_processes`; a key whose ending returned no failure is
    retired (:meth:`clear_active_session_key`) and returned, and that list is
    the audit's ``session_keys``. A failed key stays registered, its failure in
    ``last_error``, and is never ended twice.

    A round ends its keys CONCURRENTLY: one after another, a many-key run would
    hold its fences for the sum of the bounds, past
    ``session_allocation.ENDING_FENCE_WAIT_SECS``, and a held completion would
    be refused. Together each round takes one key's bound (under 40 s), at most
    ``_ENDING_ROUNDS`` rounds, inside the 180 s a held caller waits. A key whose
    ending RAISED (a reaper cancel at shutdown) interrupts no sibling: the
    round finishes before the first exception is re-raised.

    The run's task is still alive here, so a sequential run can register its
    NEXT agent's key meanwhile; each round re-reads the keys and ends new ones.
    A key registered after the last round is a named kill failure, never reset
    or signalled, so the audit says ``failed``, not ``reaped``.
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

    ``pairs`` is the snapshot (:meth:`_sessions_under`), whose handles are
    taken FIRST: the reset pops the session before it can hang, so a lookup
    afterwards would find nothing and leave the process running. The session
    the reset ACTUALLY pops is captured at the pop
    (:func:`kiro_crew.process_identity.teardown_capture`), since a cold start
    can register a new one between snapshot and pop.

    A reset that hung or failed gets every handle killed. A completed one is
    not proof the process is gone (its shutdown can fail without raising; a
    False reset stopped nothing), so every handle is checked
    (``process_survived``) and a survivor gets the fallback kill. A popped
    session with no handle is a failure, not nothing.
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

    ``handle`` was taken before the reset (:meth:`_session_process_handles`)
    and is the ONLY thing that names the process; the map is never read here,
    since whatever is under the key now is a successor. ``session_key`` and
    ``who`` are for the log. ``None`` means nothing to kill, not a failure.
    The kill itself is :func:`kiro_crew.process_identity.kill_verified_process`,
    shared with the sub-agent manager. Never raises (the caller must still
    finish the run's claim) and never swallows: it returns what stopped the
    kill, so the audit never says ``reaped`` over a live process tree.
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
    # Ownership gate, asked once before escalating. It refuses on a LEASE (the
    # session that owns the runtime) or a TENANCY (a party mid-flight on it
    # without a lease, e.g. a session-sharing sub-agent at cap=1). A tenant's
    # refusal means let it finish; its last release hands the orphan back.
    #
    # The run's OWN lease is released first. A reset releases it inside
    # ``provider.shutdown()``, so a teardown that hung or raised before that
    # lands here still holding it, and the gate would refuse the session's own
    # last-resort kill, leaving the wedged pid refused by every later sweep.
    # Only a DIFFERENT tenant's lease may withhold the signal, and that refusal
    # is reported as what stopped the kill. ``popped`` is passed because a reset
    # abandoned on timeout does not expose the session via ``tearing_down``.
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
