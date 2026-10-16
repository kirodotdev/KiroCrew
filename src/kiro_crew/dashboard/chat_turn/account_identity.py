"""A dashboard turn's reaction to the signed-in kiro-cli account changing: retiring the sessions
an identity change invalidates, and the signed-out latch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import logger


async def _retire_sessions_on_identity_change(state: Any, *, unattended: bool = False) -> None:
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

    ``unattended`` marks a caller with no person behind it, an agent cron. An
    EMPTY live fingerprint is never reconciled, so every call under it sweeps, and
    that sweep spares nothing: it retires every idle kiro-backed session,
    cancelling an idle parent's ``spawn_run`` children and dropping each one's
    native conversation. After a real sign-out that is the point: every child
    still holding the old credential has to go. But the same
    empty fingerprint comes from a store the reader cannot identify (a social
    login, an unreadable or relocated store), where it never goes away; a cron
    would then repeat the sweep on every fire. Its own session survives from one
    fire to the next mostly while its sub-agents are still running, so even
    retiring just that one would usually end them. So an unattended caller sweeps
    under an empty fingerprint only when the read proved nobody is signed in
    (``identity_absence_is_definitive``), and only until one sweep under that
    sign-out completes (``identity_absence_already_swept``): a session started after
    it holds no account or brings its own ``KIRO_API_KEY`` (a cron job's ``env``
    block), which no sweep spares, so sweeping again would only cancel its
    sub-agents. Otherwise it does nothing.

    Best-effort: a failure here must not fail the turn, which would be a worse
    outcome than the staleness it exists to correct.
    """

    service = getattr(state, "kiro_prerequisite_service", None)
    sessions = getattr(state, "sessions", None)
    if service is None or sessions is None:
        return
    try:
        changed, live = await service.identity_changed_since_sessions()
        # Capture the observation generation NOW, before the sweep runs:
        # observations up to this point are covered by the sweep's own read,
        # while any that land during the sweep (a status poll observing a
        # store flap after the permits reopen) postdate its coverage and must
        # keep the latch armed -- see note_sessions_reconciled. Looked up
        # defensively like the other optional surfaces in this gate.
        observed_gen = getattr(service, "identity_observation_generation", None)
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
            return
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
                return
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
    except Exception:
        logger.debug("Could not apply a Kiro identity change", exc_info=True)


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
