"""A dashboard turn's reaction to the signed-in kiro-cli account changing: retiring the sessions
an identity change invalidates, the check before every other session acquire and the helpers
it shares its result with, and the signed-out latch."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import logger


class _IdentityCheckRead(NamedTuple):
    """What one account check read: the live fingerprint, and the store read
    generation right after that read."""

    live: str
    reads_before: int | None


async def _retire_sessions_on_identity_change(
    state: Any, *, unattended: bool = False
) -> _IdentityCheckRead | None:
    """Recycle kiro-backed children when the signed-in account has changed.

    The counterpart to :func:`_mark_kiro_signed_out`, for the case that function
    can never see: an external ``kiro-cli logout`` (or a switch to another
    account) leaves a RUNNING child holding the old credential in memory. It
    keeps refreshing and keeps answering, so no ACP auth failure ever occurs and
    nothing reports the change -- turns simply continue under the account the user
    believes they left.

    This does NOT gate the send. A stale latch must never block a turn (see
    ``dashboard/kiro_readiness.py``), and nothing here can: the check retires an
    invalidated child and lets the send proceed on a fresh one, which is a
    process recycle rather than a readiness verdict. It stays off the spawn path
    too -- the trigger is a local database read, briefly cached, not a ``whoami``.

    ``unattended`` marks every caller but the dashboard chat turn: the check the
    session manager runs before each other acquire
    (:func:`install_acquire_identity_check`), an agent cron run among them. An
    EMPTY live fingerprint is never reconciled, so every call under it sweeps, and
    that sweep spares nothing: it retires every idle kiro-backed session,
    cancelling an idle parent's ``spawn_run`` children and dropping each one's
    native conversation. After a real sign-out that is the point: every child
    still holding the old credential has to go. But the same
    empty fingerprint comes from a store the reader cannot identify (a social
    login, an unreadable or relocated store), where it never goes away; an
    unattended caller would then repeat the sweep on every acquire, a cron on
    every fire. A cron's own session survives from one fire to the next mostly
    while its sub-agents are still running, so even retiring just that one would
    usually end them. So an unattended caller sweeps
    under an empty fingerprint only when the read proved nobody is signed in
    (``identity_absence_is_definitive``), and only until one sweep under that
    sign-out completes (``identity_absence_already_swept``): a session started after
    it holds no account or brings its own ``KIRO_API_KEY`` (a cron job's ``env``
    block), which no sweep spares, so sweeping again would only cancel its
    sub-agents. Otherwise it does nothing.

    Best-effort: a failure here must not fail the turn, which would be a worse
    outcome than the staleness it exists to correct.

    Returns what this check read (the live fingerprint it acted on and the
    store read generation right after that read), or None when there is no service
    or the check raised. A store read that fails reads as an empty fingerprint.
    :func:`install_acquire_identity_check` lets
    another unattended acquire take this check's result only while the store still
    reads that fingerprint and no read since returned another: one arriving while it
    runs, or one arriving within a minute after it left the sweep pending, while that
    sweep still is.
    """

    service = getattr(state, "kiro_prerequisite_service", None)
    sessions = getattr(state, "sessions", None)
    if service is None or sessions is None:
        return None
    try:
        changed, live = await service.identity_changed_since_sessions()
        # Capture the observation generation NOW, before the sweep runs:
        # observations up to this point are covered by the sweep's own read,
        # while any that land during the sweep (a status poll observing a
        # store flap after the permits reopen) postdate its coverage and must
        # keep the latch armed -- see note_sessions_reconciled. Looked up
        # defensively like the other optional surfaces in this gate.
        observed_gen = getattr(service, "identity_observation_generation", None)
        # And the store read count, so a concurrent acquire can tell whether any
        # read after this one returned another account (_shared_check_still_covers).
        read_gen = getattr(service, "identity_read_generation", None)
        # Captured with the read for the same reason: the sweep below awaits, and a
        # read that lands meanwhile must not speak for the one that started it.
        absence_proven = getattr(service, "identity_absence_is_definitive", False)
        absence_run = getattr(service, "identity_absence_run", None)
        if (
            unattended
            and not live
            and (not absence_proven or getattr(service, "identity_absence_already_swept", False))
        ):
            # See the docstring: an account the reader cannot identify, or a
            # sign-out a sweep already finished, is no reason for an unattended
            # caller to recycle anything.
            return _IdentityCheckRead(live, read_gen)
        # BEFORE the unchanged early-return: the stamp check exists precisely
        # for the case where the baseline (and the interim latch) compare
        # equal -- an A->B->A round trip no read ever observed -- while a
        # child's own spawn-time stamp still proves it authenticated as the
        # interim account. Flag-only: the existing eviction machinery recycles
        # the flagged session at its next acquire, nothing global or sticky is
        # touched, and on a healthy host every stamp equals ``live`` so this
        # is a per-turn no-op. Looked up defensively like the pending
        # fingerprint below: a holder that does not declare the surface is
        # left alone rather than aborting the whole best-effort gate.
        flag_stamp_mismatches = getattr(sessions, "flag_identity_stamp_mismatches", None)
        if flag_stamp_mismatches is not None:
            await flag_stamp_mismatches(live)
        if not changed:
            # An INCOMPLETE sweep is its own trigger, independent of the baseline.
            # The baseline advances only on a complete sweep, so after an A->B
            # sweep left a busy session behind, a switch BACK to A compares equal
            # to it and would return here -- while the successors that registered
            # under B keep serving turns on B's credential, silently and with no
            # auth failure to report it.
            #
            # Retry until a sweep COMPLETES, which is the only thing that clears
            # the pending fingerprint. Comparing it to the live one instead would
            # abandon the retry: this very trigger sweeps with `fingerprint=live`
            # while the baseline equals live, so the pending fingerprint it then
            # records IS the live one, and an equality test would send every later
            # turn back here with holders still to retire.
            pending = getattr(sessions, "pending_identity_sweep_fingerprint", "")
            if not pending:
                return _IdentityCheckRead(live, read_gen)
        # Defer an idle parent's live children only while its credential is still
        # VALID. An empty ``live`` is an external logout (or an unreadable store),
        # where the children must be cancelled rather than left running on a
        # credential that is gone -- the same fail-safe the sign-out path takes.
        # ``bool(live)`` alone is not enough: a PARTIAL loss (an external
        # ``kiro-cli logout`` while the Crew vault stays populated) leaves the
        # combined fingerprint nonempty, so a credential source that DROPPED since
        # the baseline must also force the cancel path -- otherwise the children
        # keep running on the logged-out store's in-memory credential. Only a
        # proven switch between two real accounts (non-empty ``live`` with no
        # dropped source) defers, which is the per-turn regression being fixed.
        spare_children = bool(live) and not service.identity_component_dropped(live)
        retired, complete = await sessions.retire_kiro_identity_sessions(
            fingerprint=live, spare_children=spare_children
        )
        # Advance THIS consumer's baseline ONLY on a complete sweep AND a real
        # identity. Anything left running -- a busy session, a child that would not
        # shut down, a start still in flight -- is still holding the previous
        # account, so recording the change as handled would mean its next turn sees
        # no change and reuses that account.
        #
        # An EMPTY fingerprint is refused for a different reason: it means the store
        # could not be read (relocated, unreadable, or signed out), and reconciling
        # it would make "cannot tell" the accepted steady state -- every later
        # account switch would then compare equal to "" and go undetected while
        # children keep running. Leaving it unreconciled means each turn re-sweeps,
        # which bounds how long a child can outlive the account it loaded to a
        # single turn. That is a real cost on a host whose store is not readable,
        # and it is the correct direction to pay it in: the replacement child reads
        # whatever the store now holds even when we cannot fingerprint it.
        if complete and live:
            service.note_sessions_reconciled(live, observations_before=observed_gen)
        elif complete and absence_proven and absence_run is not None:
            # A sign-out is never reconciled, so record that a sweep finished this
            # one: unattended callers then leave it alone (identity_absence_already_swept).
            note_absence_swept = getattr(service, "note_absence_swept", None)
            if note_absence_swept is not None:
                note_absence_swept(absence_run)
        # Narrow the latch ONLY on an actual sign-out (no identity on disk). On a
        # switch to another valid account, narrowing would strand readiness: if a
        # status poll observed the switch first it has already stamped the new
        # identity, so the fingerprints now MATCH and no ordinary poll re-probes --
        # the card would sit at "not signed in" until someone pressed Check again.
        # A switch needs no narrowing anyway: the poll that stamped the new
        # identity also refreshed the verdict for the account now in use, and the
        # fail-closed gates carry their own freshness bound.
        if not live:
            service.mark_signed_out()
        if not complete:
            # WARNING, not INFO: an incomplete sweep re-runs on every turn and
            # recycles idle sessions each time, so the holdout it names is what
            # an operator needs to find (an open channel member, a busy chat).
            waiting_on = getattr(sessions, "identity_sweep_waiting_on", ())
            logger.warning(
                "Kiro identity sweep incomplete, will retry next turn; "
                "retired %d session(s): %s; waiting on: %s",
                len(retired),
                ", ".join(retired) or "none",
                ", ".join(waiting_on) or "unknown",
            )
        elif retired:
            logger.info(
                "Kiro identity changed; retired %d session(s): %s",
                len(retired),
                ", ".join(retired),
            )
        return _IdentityCheckRead(live, read_gen)
    except Exception:
        logger.debug("Could not apply a Kiro identity change", exc_info=True)
        return None


#: How long after a check that left the identity sweep pending an acquire takes that
#: check's result instead of running the sweep again.
_PENDING_SWEEP_RETRY_SECS = 60.0


def install_acquire_identity_check(
    state: Any, *, clock: Callable[[], float] = time.monotonic
) -> None:
    """Make every session acquire on ``state.sessions`` run the account check first.

    Installs :func:`_retire_sessions_on_identity_change` as the session manager's
    ``identity_check_before_acquire``, as its unattended caller, so a task step, a
    workflow agent, a channel turn or ``/compact``, a sub-agent completion or an
    agent cron run retires a session left on a previous Kiro account before it can
    reuse one, and an acquire path added later cannot leave the check out. The dashboard chat
    turn runs the attended check itself and tells its claim so
    (``identity_checked``). Called once the prerequisite service has seeded its
    baseline, beside ``spawn_identity_reader``.

    Concurrent acquires share one check. While a sweep stays pending (a channel
    member holding its lease after a switch can keep it pending for hours), each
    check re-runs it, and a sweep drains every cold-start permit and empties the warm
    pool. Run once per acquire, a workflow fan-out would start its sessions one at a
    time and lose the pool on each. So an acquire that arrives while a check is in
    flight waits for that check and takes its result, unless a claimable Kiro process has
    a stamp that proves neither the live account nor another, or an account other than
    the one that check acted on has been read since: the store now reads another
    fingerprint, or some read in between returned one (an account switch during the
    sweep can leave a session that registered late on it, unstamped). The
    acquire then runs its own check, or waits for a newer one another acquire
    started. An empty result is never shared, because an
    account the reader cannot identify and a sign-out both read empty, and only the
    acquire's own check can tell them apart. An acquire that takes the result still
    runs the stamp check itself, because a session can register after the shared
    sweep scanned the registry.
    The shared check runs in its own task, so cancelling the acquire that started it
    does not cancel it under the others.

    Each re-run of a pending sweep drains every cold-start permit and empties the warm
    pool again. An unattended acquire normally re-runs it at most once a minute
    (``_PENDING_SWEEP_RETRY_SECS``, read on *clock*). Inside that minute an acquire
    that finds no check in flight may take the result of the last unattended check,
    which is the only caller that records one. It does so only while the sweep is still
    pending under that non-empty reading, the store still reads it and has not read
    another account since, and no claimable Kiro process has a stamp that proves neither
    the live account nor another, such as an empty stamp, a store or vault source empty
    on one side, or an API key that differs on a child that receives the key. Such a
    process forces another sweep, which retires it once idle. The holder guard covers
    both sharing paths and is consulted after their store reads. The recorded-result path
    also consults it before that read. The stamp check still flags a session stamped for
    another account and displaces such a runtime, without a sweep.
    ``_shared_check_still_covers`` refuses an empty reading, so under a sign-out
    every acquire still re-runs an incomplete sweep.
    """

    in_flight: asyncio.Task[_IdentityCheckRead | None] | None = None
    pending_sweep_check: _IdentityCheckRead | None = None
    pending_sweep_checked_at = 0.0

    async def _check_and_note_a_pending_sweep() -> _IdentityCheckRead | None:
        nonlocal pending_sweep_check, pending_sweep_checked_at
        checked = await _retire_sessions_on_identity_change(state, unattended=True)
        if (
            checked is not None
            and checked.live
            and getattr(state.sessions, "pending_identity_sweep_fingerprint", "") == checked.live
        ):
            pending_sweep_check, pending_sweep_checked_at = checked, clock()
        return checked

    def _holds_kiro_process_of_unknown_account(live: str) -> bool:
        holds_kiro_process_of_unknown_account = getattr(
            state.sessions, "holds_kiro_process_of_unknown_account", None
        )
        if holds_kiro_process_of_unknown_account is None:
            return True
        try:
            return bool(holds_kiro_process_of_unknown_account(live))
        except Exception:
            logger.debug(
                "Could not inspect Kiro holders before sharing a pending sweep",
                exc_info=True,
            )
            return True

    async def _check_before_acquire() -> None:
        nonlocal in_flight
        while True:
            joined = in_flight
            while joined is not None and not joined.done():
                shared = await asyncio.shield(joined)
                if (
                    shared is not None
                    and await _shared_check_still_covers(state, shared)
                    and not _holds_kiro_process_of_unknown_account(shared.live)
                ):
                    await _flag_stamp_mismatches_after_shared_check(state, shared.live)
                    return
                if in_flight is joined:
                    break
                # Another acquire saw the same mismatch first and started the check that
                # covers it: wait for that one instead of starting a third.
                joined = in_flight
            recorded = pending_sweep_check
            pending_fingerprint = getattr(state.sessions, "pending_identity_sweep_fingerprint", "")
            if (
                recorded is None
                or clock() - pending_sweep_checked_at >= _PENDING_SWEEP_RETRY_SECS
                or pending_fingerprint != recorded.live
            ):
                break
            if _holds_kiro_process_of_unknown_account(recorded.live):
                break
            if await _shared_check_still_covers(state, recorded):
                still_pending = getattr(state.sessions, "pending_identity_sweep_fingerprint", "")
                # A holder may register during that read, so ask again before taking the result.
                if still_pending == recorded.live and not _holds_kiro_process_of_unknown_account(
                    recorded.live
                ):
                    await _flag_stamp_mismatches_after_shared_check(state, recorded.live)
                    return
            if in_flight is joined:
                break
            # Another acquire started a check while this one read the store: share it
            # rather than run a second sweep beside it.
        check = asyncio.create_task(_check_and_note_a_pending_sweep())
        in_flight = check
        await asyncio.shield(check)

    state.sessions.identity_check_before_acquire = _check_before_acquire


async def _shared_check_still_covers(state: Any, shared: _IdentityCheckRead) -> bool:
    """Whether an acquire may take *shared*'s result instead of checking itself.

    Only a non-empty result, while the store still reads it and no read since it
    returned anything else. A service that cannot answer the second question
    shares nothing.
    """
    if not shared.live or shared.reads_before is None:
        return False
    if shared.live != await _live_identity_fingerprint(state):
        return False
    read_other_than = getattr(state.kiro_prerequisite_service, "identity_read_other_than", None)
    if read_other_than is None:
        return False
    try:
        return not read_other_than(shared.live, since=shared.reads_before)
    except Exception:
        logger.debug("Could not read the Kiro identity read record", exc_info=True)
        return False


async def _flag_stamp_mismatches_after_shared_check(state: Any, live: str) -> None:
    """Run the shared check's stamp step again for an acquire that took its result.

    A session can register after the shared sweep scanned the registry: a cold
    start that began under an interim account during an A->B->A round trip comes up
    stamped B while the store reads A again. Its stamp is the only evidence, so the
    acquire flags it before it claims, as its own check would.
    """
    flag_stamp_mismatches = getattr(state.sessions, "flag_identity_stamp_mismatches", None)
    if flag_stamp_mismatches is None:
        return
    try:
        await flag_stamp_mismatches(live)
    except Exception:
        logger.debug("Could not flag sessions after a shared identity check", exc_info=True)


async def _live_identity_fingerprint(state: Any) -> str | None:
    """The store's fingerprint read fresh, or None when it cannot be read."""
    try:
        read: str = await state.kiro_prerequisite_service.current_identity_fingerprint(
            allow_cached=False
        )
    except Exception:
        logger.debug("Could not read the Kiro identity store", exc_info=True)
        return None
    return read


def _mark_kiro_signed_out(state: Any) -> None:
    """Latch the prerequisite service to signed-out after an ACP auth failure.

    Readiness is probed at boot and on explicit user action only, so the ACP
    attempt is what discovers a mid-session logout. Feeding that back into the
    service is what keeps the still-fail-closed gates — the poll-driven kiro-cli
    spawn sites and the destructive reruns — from acting on a stale ready latch,
    with no timer re-probe. Best-effort: never disrupt the turn's teardown.
    """

    service = getattr(state, "kiro_prerequisite_service", None)
    if service is None:
        return
    try:
        service.mark_signed_out()
    except Exception:
        logger.debug("Could not latch Kiro signed-out state", exc_info=True)
