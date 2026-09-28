"""The unattended schedule: nightly grants, failure records, backoff and due-ness.

The module owns the two per-account unattended grants (``nightly`` and
``nightly_sessions``) and the per-kind registry the upload gate re-reads. It also
owns the consecutive-failure record, its run-slot witness, and the due checks the
app's nightly loop asks on every wake.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any, Optional

from kiro_crew import snapshot_redact
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import sanitize_label
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    KIND_SESSIONS,
    KIND_SNAPSHOT,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.ledger import last_runs
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    NIGHTLY_FAILURE_STATE_KEY,
    _account_state,
    _account_view,
    _locked_state_update,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.traversal import (
    kind_unavailable_reason,
)

logger = logging.getLogger(_FACADE_MODULE)


#: How long a completed run keeps the nightly quiet, in seconds. Named rather than
#: inlined because :data:`NIGHTLY_RETRY_BACKOFF_SECS` is bounded BY it: a retry delay
#: that reached this would be indistinguishable from the nightly not being due at all,
#: so the two numbers have to be comparable in one place instead of one being a literal
#: inside :func:`_a_day_since_last_run` and the other a literal here.
NIGHTLY_WINDOW_SECS = 23 * 3600


#: The wait after N consecutive failed unattended attempts, indexed by N-1, with the
#: last entry as the ceiling for anything beyond.
#:
#: The FIRST entry is zero deliberately. A single failure is not yet evidence of a
#: pattern, and retrying it on the next wake is the behaviour the reported issue calls
#: correct for a transient fault; backing off from the SECOND failure is the first point
#: at which the loop has seen the fault twice. So nothing about a one-off blip changes.
#:
#: The ceiling is what makes this a backoff rather than a mute. It is asserted below to
#: sit under :data:`NIGHTLY_WINDOW_SECS`, so however long a deterministic fault persists
#: the loop still attempts more often than once a window -- a backoff must never become
#: a second way for a backup the owner enabled to go quiet, which is the same rule
#: :func:`_a_day_since_last_run` follows when it reads an unparseable stamp as due.
#:
#: With the half-hourly wake this takes a permanent fault from roughly 48 attempts a day
#: to 2, and the first day from 48 to 6.
NIGHTLY_RETRY_BACKOFF_SECS: tuple[int, ...] = (
    0,  # 1 failure: the next wake retries, exactly as before this existed
    3600,  # 2 failures: 1 h
    2 * 3600,  # 3 failures: 2 h
    4 * 3600,  # 4 failures: 4 h
    8 * 3600,  # 5 failures: 8 h
    12 * 3600,  # 6 or more: 12 h, the ceiling
)


# Stated as an assertion and not only as prose, the shape `probes/gh_pr.py` uses on the
# same kind of constant-versus-constant invariant, because the prose above is what a
# reader is asked to trust and prose cannot fail.
assert max(NIGHTLY_RETRY_BACKOFF_SECS) < NIGHTLY_WINDOW_SECS, (
    "the retry ceiling must stay under the nightly window, or a long-lived fault turns "
    "the backoff into a second way for a backup the owner enabled to go quiet"
)


def _granted(account: str, key: str) -> bool:
    """Whether one unattended-upload consent bit is stored as a real ``True``.

    ``is True`` and not ``bool(...)``, because every other reader in the backup engine
    treats a non-conforming state file as something to survive rather than
    something that cannot happen -- ``_account_view`` flattens a corrupt level to
    empty, ``_a_day_since_last_run`` catches a non-string stamp. Inside that
    recognized corruption class a truthy non-bool (the string ``"false"`` is the
    cheap example) would read as consent GRANTED, which turns a bit documented as
    fail-closed into a fail-open one. The only writers are ``set_nightly`` and
    ``set_nightly_sessions``, both of which store ``bool(...)``, so nothing this
    package produces is rejected by the stricter read.

    Shared by both consent bits deliberately. Two readers of the same kind of
    answer, one strict and one not, is the shape that drifts: whichever is looser
    becomes the way in, and a reader comparing them cannot tell which strictness
    was intended.
    """
    return _account_view(account).get(key) is True


def nightly_enabled(account: str) -> bool:
    """Whether the owner has authorized unattended uploads for this account.

    Reads through :func:`read_state`, so an unreadable state file answers False.
    That is FAIL-CLOSED, and it is the opposite of what :func:`last_runs` does
    with a run it could not persist -- the asymmetry is deliberate, because the
    two answers cost different things when they are wrong.

    This bit AUTHORIZES spending the owner's money without them present. Read it
    optimistically and a corrupt or unreadable file becomes a reason to start
    uploading; refuse, and a transient failure costs one skipped nightly window
    that the next wake picks up. The run record is the mirror image: it is a
    record of something that ALREADY happened and is already paid for, so
    dropping it does not prevent a charge, it causes one.
    """
    return _granted(account, "nightly")


def set_nightly(account: str, enabled: bool) -> None:
    def mutate(state: dict[str, Any]) -> None:
        _account_state(state, account)["nightly"] = bool(enabled)

    _locked_state_update(mutate)


def nightly_sessions_enabled(account: str) -> bool:
    """Whether the owner has authorized unattended TRANSCRIPT uploads.

    A SEPARATE key from ``nightly``, and never a read of it. The two
    authorizations are not the same question: ``nightly`` authorizes uploading
    the memory and workspace snapshot, while this one authorizes uploading
    everything the agent was ever shown. Riding the snapshot's bit would mean an
    operator who said yes to "back up my memory" had also, without being asked,
    said yes to "upload my conversations".

    Fail-closed for the same reason :func:`nightly_enabled` is: an unreadable
    state file must not become a reason to start uploading transcripts. The
    absent key answers False, so every install that has not asked for this is
    off, and :func:`_granted` requires a real ``True`` so a corrupt truthy value
    cannot answer for the owner either.
    """
    return _granted(account, "nightly_sessions")


def set_nightly_sessions(account: str, enabled: bool) -> None:
    def mutate(state: dict[str, Any]) -> None:
        _account_state(state, account)["nightly_sessions"] = bool(enabled)

    _locked_state_update(mutate)


#: The bit that authorizes each kind's UNATTENDED upload, read by
#: :func:`_authorize_upload` immediately before the payload leaves.
#:
#: A lookup rather than a branch, and a lookup that is asserted COMPLETE against
#: :data:`JOB_KINDS` by its own test, because the failure this table exists to
#: prevent is silent: a kind added without a bit would otherwise fall through to
#: whatever the code does when it finds nothing. Here finding nothing refuses.
#: Each kind maps to its OWN reader and never to another's, so no kind can end up
#: uploaded on a grant the owner gave for something else.
_NIGHTLY_CONSENT_READERS: dict[str, Callable[[str], bool]] = {
    KIND_SNAPSHOT: nightly_enabled,
    KIND_SESSIONS: nightly_sessions_enabled,
}


def _a_day_since_last_run(account: str, kind: str, now: Optional[dt.datetime]) -> bool:
    """True when ``kind`` has not completed a run in the last ~23 hours.

    The stamp reasoning is shared by every nightly kind, so it lives once. The
    CONSENT question is deliberately not in here: each kind reads its own bit at
    its own call site, so a new kind cannot inherit another kind's grant by
    calling a helper that already answered it.
    """
    runs = last_runs(account).get(kind)
    if not runs:
        return True
    try:
        last = dt.datetime.fromisoformat(runs["at"])
    except (KeyError, ValueError, TypeError):
        # TypeError: a corrupted state file carrying a non-string (list/number).
        # Anything unusable reads as "due" -- an unparseable stamp must not be
        # the reason a backup the owner enabled silently stops running.
        return True
    if last.tzinfo is None:
        # A timezone-less stamp parses FINE, so it escapes the try above and
        # would raise TypeError on the aware subtraction below -- outside the
        # guard, in the nightly loop, every wake. costs.is_fresh and
        # shares._prune already normalize this; this site was the one left out.
        last = last.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last).total_seconds() > NIGHTLY_WINDOW_SECS


def nightly_run_witness(account: str, kind: str) -> Optional[tuple[str, int]]:
    """The run slot's identity right now, or ``None`` when it holds no usable record.

    Read BEFORE an unattended attempt starts and handed back to
    :func:`record_nightly_failure`, which refuses to write a failure when the slot has
    moved since. ``(process, sequence)`` is the identity the run ledger already established
    for exactly that compare-and-set -- see ``_record_run_locked``'s ``expected``
    parameter, which `_record_skip` uses the same way. Neither ``at`` nor ``key`` can
    stand in for it: ``datetime.now`` resolves to the platform's clock tick, so two
    writes can share a microsecond value, and a skip copies the matched run's key.

    ``None`` covers both "nothing has ever run" and "the record is too old or too
    corrupt to identify". Those are not distinguished because the caller does not need
    them to be: it compares this value against a second reading of the same expression,
    and two ``None`` results mean the slot did not move, which is the whole question.
    """
    record = last_runs(account).get(kind)
    if not isinstance(record, dict):
        return None
    process, sequence = record.get("process"), record.get("sequence")
    # `type(...) is not int` for the reason `_record_run_locked` gives: `True` is an int
    # subclass, and a corrupted document must not present a bool as a sequence number.
    if not isinstance(process, str) or not process or type(sequence) is not int:
        return None
    return (process, sequence)


#: Bound on the stored failure message. Longer than :data:`LABEL_MAX_CHARS` because this
#: one is a diagnostic an operator reads, not a caption: 64 characters cuts a message like
#: "N file(s) are not text, so they cannot be shown free of credentials" mid-sentence. It
#: is still bounded, so one pathological message cannot grow the state document on every
#: wake for as long as the fault lasts. The fuller text survives in the SEL audit record,
#: which does not egress.
FAILURE_ERROR_MAX_CHARS = 200


def record_nightly_failure(
    account: str,
    kind: str,
    error: str = "",
    *,
    run_witness: Optional[tuple[str, int]],
) -> Optional[dict[str, Any]]:
    """Record that one UNATTENDED attempt was made and failed. Never raises.

    This is the record whose absence is the reported defect: without it the state
    file holds nothing at all about a nightly that has been failing since a
    particular day, so :func:`due_for_nightly` cannot tell a fault it has already
    met from one it is seeing for the first time, and the loop re-attempts on every
    wake forever.

    ``run_witness`` is :func:`nightly_run_witness` read BEFORE the attempt began, and it
    is REQUIRED rather than defaulted. A default would let a call site added later opt
    out of the race protocol silently, which is the shape of the bug it exists to close:
    the two writers serialize under the sidecar lock, but each mutate re-reads fresh
    state, so an unconditional write here can land AFTER a concurrent manual success
    cleared the count and record a failure against a kind that just succeeded.

    What that costs was MEASURED rather than assumed, because the obvious claim is wrong:
    the raced write restarts the count at 1, :func:`nightly_retry_delay_secs` answers 0
    there, and the fresh run record already holds the account not-due for the window --
    so it withholds no attempt. What it does produce is a false :func:`nightly_failures`
    row for an account that just backed up, plus a one-step skew on the next genuine
    failure. The row is the reason this guard ships: making that state readable is half
    of what this change is for, so writing a knowingly false one would undo it at the
    surface it just built.

    Returns ``None`` when the slot moved during the attempt, having written nothing. That
    direction is deliberate: skipping a real failure costs the extra attempts the loop
    already makes today, while writing a false one publishes a failure row against an
    account that just backed up and skews the next genuine failure's count by one, so
    every ambiguity here resolves toward attempting the backup -- the same posture
    :func:`_backoff_withholds` and :func:`_a_day_since_last_run` take.

    SCHEDULED callers only, and that is the same line
    :func:`_unattended_sessions_redaction_gap` already draws one screen up: an owner
    pressing the button is present, sees the failure, and chooses whether to try
    again, so recording their attempt here would let a person retrying by hand push
    out the unattended schedule they are retrying on behalf of. What the owner path
    does reach is the CLEAR, inside :func:`_record_run_locked` -- a success counts
    from anywhere, a failure only counts where nobody was watching.

    Never raises, by the rule :func:`_record_run` follows on the same state file:
    this runs on a path that is already handling a failed backup, so letting an
    unwritable state file raise here would replace a logged failure with an
    unhandled one and cost the caller its audit record. A count that did not
    persist leaves the loop retrying as it does today, which is the direction this
    whole change is careful to fail in.
    """
    stamped: dict[str, Any] = {
        "consecutive": 1,
        # Provisional, like `_record_run_locked`'s: the authoritative stamp is taken
        # inside `mutate` under the sidecar lock. This value survives only on the
        # path where the state update never ran.
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
        # When the CURRENT run of failures began, as distinct from `at`. The two answer
        # different questions and both are needed: `at` is the backoff's clock, so it has
        # to be the LATEST attempt or a long streak's wait would expire against a stamp
        # from days ago; this one is what the reported issue asks for in so many words --
        # "no indication that the nightly has been failing since a particular day" -- and
        # a single overwritten stamp cannot say both. Carried forward while the streak
        # continues, and cleared with the row, so it always describes the run it sits in.
        "since": "",
        # This value EGRESSES -- :func:`nightly_failures` serves it and the backup status
        # route publishes it as ``nightlyFailures`` -- and its text is not ours: it is
        # ``str(exc)`` from whatever failed, which on this path includes
        # ``snapshot.RedactionFailed``, whose message embeds file names out of the bundle.
        # ``snapshot._safe_name`` makes those PRINTABLE and says so; credential-free is a
        # different job it does not do. So this takes the same pipeline a foreign-authored
        # label takes, at a diagnostic's bound. Control characters go first (they survive
        # both redactors), the redactors run before the bound (truncating first can cut a
        # credential mid-token and leave a partial secret the redactor cannot match), and a
        # non-string or unrenderable message stores empty rather than raising.
        "error": sanitize_label(error, limit=FAILURE_ERROR_MAX_CHARS),
    }

    def mutate(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        entry = _account_state(state, account)
        # Compare-and-set against the RUN slot, before touching the failure map. A run
        # recorded since this attempt began is direct positive evidence that backups are
        # reaching the drive, which supersedes a failure whose own attempt is already
        # over -- and it is the write whose clear this would otherwise undo. Read from
        # `entry` under the same lock as the write, so nothing can move in between.
        runs_now = entry.get("runs")
        current = runs_now.get(kind) if isinstance(runs_now, dict) else None
        witness_now: Optional[tuple[str, int]] = None
        if isinstance(current, dict):
            process, sequence = current.get("process"), current.get("sequence")
            if isinstance(process, str) and process and type(sequence) is int:
                witness_now = (process, sequence)
        if witness_now != run_witness:
            # Absent-to-absent compares equal, which is what keeps the reported case --
            # a nightly that has NEVER succeeded, so there is no run record at all --
            # writing its count normally. Only an actual move refuses.
            return None
        failures = entry.setdefault(NIGHTLY_FAILURE_STATE_KEY, {})
        if not isinstance(failures, dict):
            # Repair-on-write, the rule `_account_state` states and
            # `_record_run_locked` applies to a corrupted `runs`: a non-dict here
            # carries nothing to lose, and raising would abort the only write that
            # can stop the loop this function exists to slow down.
            failures = entry[NIGHTLY_FAILURE_STATE_KEY] = {}
        previous = failures.get(kind)
        held = previous.get("consecutive") if isinstance(previous, dict) else None
        # `type(...) is not int` and not `isinstance`, the spelling `_record_run_locked`
        # uses on `sequence`: `True` is an `int` subclass, so a corrupted document
        # carrying a bool would otherwise count as a previous attempt. Anything
        # unusable restarts the count at 1 rather than reading as a long history, so
        # corruption can only ever shorten a backoff.
        # Narrowed ONCE into a value rather than tested twice: the increment and the
        # streak-start carry both ask "is there a usable count to continue", and two
        # copies of that expression is how the two answers drift apart. Zero means no
        # usable previous count, so `if streak` reads as "the streak continues". Written
        # as a statement rather than a conditional expression because the type checker
        # narrows `type(held) is int` there and cannot narrow it through a bool variable.
        streak = 0
        if type(held) is int and held > 0:
            streak = held
        if streak:
            stamped["consecutive"] = streak + 1
        stamped["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        # The streak's start is CARRIED, never re-stamped, for as long as the streak
        # lasts -- that is the whole point of having a second field. It is read from the
        # stored row rather than recomputed, and it has to PARSE to be carried: a
        # non-empty string is not enough. This value is published in an operator-facing
        # row, so a stored stamp that is a string but not a timestamp would be carried
        # for the life of the streak and rendered as the day the failures began.
        # Validated with the `fromisoformat`-inside-`try` spelling `_backoff_withholds`
        # uses on `at`, the only other place this module reads a stored stamp, rather
        # than a second spelling of the same check. Anything unusable starts the streak
        # here, so a corrupt value can only ever under-report how long the nightly has
        # been failing. The backoff never reads this field, so a corrupt value cannot
        # affect scheduling in either direction.
        carried = previous.get("since") if isinstance(previous, dict) else None
        stamped["since"] = stamped["at"]
        if streak and isinstance(carried, str) and carried:
            try:
                dt.datetime.fromisoformat(carried)
            except ValueError:
                pass
            else:
                stamped["since"] = carried
        failures[kind] = stamped
        return stamped

    try:
        recorded = _locked_state_update(mutate)
    except OSError as exc:
        # Deliberately NOT held in process memory the way `_record_run` holds an
        # unpersisted run. That overlay exists because dropping a run record CAUSES a
        # duplicate paid upload; dropping a failure count only costs the extra
        # attempts the loop already makes today, and an in-memory backoff would be a
        # second source of truth for a decision the persisted record owns.
        logger.warning(
            "aws-control: %s backup for %s failed and its failure count could not be "
            "recorded, so the nightly loop will retry without backing off: %s",
            kind,
            account,
            exc,
        )
        return None
    if recorded is None:
        # Said out loud, because otherwise a skipped backoff looks like the recorder
        # silently not working. This is the good case: a run landed while this attempt
        # was failing, so backups are demonstrably reaching the drive and there is
        # nothing for a backoff to protect.
        logger.info(
            "aws-control: %s backup for %s failed, but a run completed while it was "
            "running, so no failure is recorded against an account that just backed up",
            kind,
            account,
        )
    return recorded


def nightly_failures(account: str) -> dict[str, Any]:
    """Per kind, the consecutive-failure record for UNATTENDED attempts.

    ``{kind: {"at": iso8601, "since": iso8601, "consecutive": int, "error": str}}``, and
    absent for a kind whose last attempt completed -- :func:`_record_run_locked` clears
    the entry as it writes the run, and :func:`_merge_pending` clears it when a recovered
    run arrives that way instead.

    ``at`` is the LATEST attempt and is what the backoff measures from; ``since`` is when
    the current run of failures began. Both are reported because they answer different
    questions, and the reported issue asks for the second one by name: an operator needs
    to see that the nightly "has been failing since a particular day", which a single
    overwritten stamp cannot say.

    Served by the backup status read for the reason :func:`retention_unclaimed` is:
    the run record says when the nightly last SUCCEEDED, and without this an
    operator cannot see that it has been failing since a particular day, or how many
    times, which is the half of the reported issue a backoff alone does not answer.
    NO console renderer ships with this either; the surface is HTTP only, and the
    absence is stated here so someone deciding whether to build the panel finds it.

    Leaf values are served as stored, exactly as :func:`last_runs` serves a run
    record, so this stays one projection of the state file rather than a second
    validator of it -- :func:`_backoff_withholds` is where the values are judged.
    """
    recorded = _account_view(account).get(NIGHTLY_FAILURE_STATE_KEY, {})
    if not isinstance(recorded, dict):
        return {}
    return {str(kind): dict(row) for kind, row in recorded.items() if isinstance(row, dict)}


def nightly_retry_delay_secs(consecutive: int) -> int:
    """How long to wait after ``consecutive`` failed unattended attempts.

    Pure, and separate from the state read, so the schedule can be asserted against
    :data:`NIGHTLY_RETRY_BACKOFF_SECS` without building a state file -- and so the
    ceiling applies to every count above the table's length instead of the table
    needing a row per failure.
    """
    if consecutive <= 0:
        return 0
    index = min(consecutive, len(NIGHTLY_RETRY_BACKOFF_SECS)) - 1
    return NIGHTLY_RETRY_BACKOFF_SECS[index]


def _backoff_withholds(account: str, kind: str, now: Optional[dt.datetime]) -> bool:
    """True while a recorded run of failures is still holding this kind back.

    Every unusable reading answers False, which is DUE. That direction is the one
    property this function must not get wrong: :func:`_a_day_since_last_run` already
    states that an unparseable stamp must not be the reason a backup the owner
    enabled silently stops running, and a failure record is a new place for exactly
    that to happen. So a corrupt count, a corrupt stamp, a missing field and a clock
    that stepped backwards all read as "attempt it", never as "stay quiet".
    """
    recorded = _account_view(account).get(NIGHTLY_FAILURE_STATE_KEY)
    if not isinstance(recorded, dict):
        return False
    row = recorded.get(kind)
    if not isinstance(row, dict):
        return False
    consecutive = row.get("consecutive")
    # `type(...) is not int` and not `isinstance`, the spelling `_granted` argues for:
    # two readers of the same kind of answer, one strict and one not, is the shape that
    # drifts, and `record_nightly_failure` reads this same field strictly -- there the
    # strictness IS observable, because a bool read as a previous attempt makes the next
    # count 2 instead of 1.
    #
    # Here it is currently an EQUIVALENT mutant, and saying so is cheaper than leaving
    # the next reader to measure it: a bool is worth 0 or 1, and the first row of
    # NIGHTLY_RETRY_BACKOFF_SECS is zero, so the loose spelling reaches `delay <= 0` and
    # answers due exactly as the strict one does. It becomes load-bearing the moment
    # that first row is non-zero, which is why the spelling stays rather than being
    # relaxed to match what is observable today.
    if type(consecutive) is not int:
        return False
    delay = nightly_retry_delay_secs(consecutive)
    if delay <= 0:
        return False
    at = row.get("at")
    if not isinstance(at, str):
        return False
    try:
        last = dt.datetime.fromisoformat(at)
    except ValueError:
        return False
    if last.tzinfo is None:
        # The same normalization `_a_day_since_last_run` applies, and for the same
        # reason: a timezone-less stamp PARSES, so it escapes the guard above and
        # would raise TypeError on the aware subtraction below -- inside the nightly
        # loop, on every wake.
        last = last.replace(tzinfo=dt.timezone.utc)
    elapsed = ((now or dt.datetime.now(dt.timezone.utc)) - last).total_seconds()
    if elapsed < 0:
        # The record is stamped in the future, so the host clock stepped backwards
        # (or the file was carried from a machine that was ahead). Withholding on
        # that arithmetic would keep the nightly quiet for as long as the skew
        # lasts, with nothing in the state file an operator could read as the cause.
        return False
    return elapsed < delay


def due_for_nightly(account: str, now: Optional[dt.datetime] = None) -> bool:
    """True when the nightly snapshot has not run in the last ~23 hours.

    The backoff is read LAST, after the grant and after the window, because it is
    the narrowest of the three: the first two answer whether a run is wanted at all,
    and this only answers whether to attempt one again yet.
    """
    if not nightly_enabled(account):
        return False
    if not _a_day_since_last_run(account, KIND_SNAPSHOT, now):
        return False
    return not _backoff_withholds(account, KIND_SNAPSHOT, now)


def _unattended_sessions_redaction_gap() -> Optional[str]:
    """Why an operator who asked for redaction gets no UNATTENDED transcript upload.

    ``None`` when nothing stands in the way. Redaction is opt-IN and off by
    default (see ``snapshot_redact.outbound_redaction_enabled``), and the default
    is not an oversight: the destination is owner-only and re-verified at every
    upload, so the documented trade is that hardening protects the payload and
    redaction is a rewrite an operator may additionally ask for.

    The asymmetry this closes is narrow and only exists for an operator who DID
    ask. :func:`run_snapshot_backup` routes its payload through
    ``snapshot.prepare_redacted_copy`` and so honours the switch; the sessions
    archive cannot use that seam, because it refuses an archive with more than one
    root ("expected one bundle root to redact") and this one has two, ``crew`` and
    ``cli``. So for that operator the snapshot leaves redacted and the transcripts
    would leave unredacted -- while transcripts are the payload most likely to
    hold a pasted secret in the first place.

    Refusing rather than uploading, because ``_redacted_upload_copy``'s own rule
    is that "could not redact" must never fall through to "send it unredacted",
    and unattended is exactly where nobody is present to notice that it did.

    Scheduled path only. An owner pressing the button is present and is choosing
    this archive knowingly, and that path shipped before the nightly existed;
    reading this at :func:`kind_unavailable_reason` instead would take a working
    button away from them.
    """
    try:
        if not snapshot_redact.outbound_redaction_enabled():
            return None
    except snapshot_redact.RedactionSwitchUnreadable as exc:
        # Cannot tell which way the operator set it. Off would ignore a request to
        # scrub and on cannot be honoured here, so the unattended path declines
        # rather than guessing silently in either direction.
        #
        # The exception goes to the log and NOT into the returned string, because
        # this string is console copy: it reaches the owner under the nightly
        # switch, where an exception repr is noise they cannot act on. The log is
        # where a person diagnosing it looks, and it keeps the detail in full.
        logger.warning("the outbound redaction switch could not be read: %s", exc)
        return (
            "the outbound redaction setting for this account could not be read, so an "
            "unattended transcript upload is declined until it can be"
        )
    return (
        "this account has outbound redaction turned on, and the sessions archive cannot be "
        "redacted on the way out yet. An unattended upload is declined rather than sent "
        "unredacted; an owner-triggered archive still runs, since somebody is present to "
        "choose it."
    )


BLOCK_HOST_UNSUPPORTED = "host_unsupported"


BLOCK_REDACTION_ON = "redaction_on"


BLOCK_OTHER_ACCOUNT = "other_account"


def scheduled_sessions_blocked_code(*, scheduled_account: bool = True) -> Optional[str]:
    """Which condition stops a nightly transcript archive here, or ``None``.

    A stable token rather than a sentence, because the one surface that shows this
    to a person has to say it in their language and a sentence chosen here can only
    ever be English. The prose below is derived from this, so the console and the
    log agree on WHICH condition holds while each words it for its own reader.

    Every condition in one place, because a caller asking "can this run" wants the
    answer and not a list of causes to check. The capability comes first: it is a
    property of the machine that no setting changes, while the redaction gap is
    something the operator can act on.

    ``scheduled_account`` is the caller's answer to "is the account being asked
    about the one the nightly loop runs for". It is a question only a SURFACE can
    be wrong about: the loop reads this for the account it just resolved, so the
    condition is false there by construction, which is why the default keeps every
    scheduling caller reading exactly as before. A per-account console is the
    caller that must pass it -- the grant is settable on any account while the
    loop resolves one, so without this an operator can switch transcripts on for a
    second account and be shown a running schedule that nothing will ever run.
    """
    if kind_unavailable_reason(KIND_SESSIONS) is not None:
        return BLOCK_HOST_UNSUPPORTED
    if _unattended_sessions_redaction_gap() is not None:
        return BLOCK_REDACTION_ON
    if not scheduled_account:
        return BLOCK_OTHER_ACCOUNT
    return None


def scheduled_sessions_blocked_reason() -> Optional[str]:
    """The same answer in prose, for logs, audit subjects and upload refusals.

    The grant and this are separate answers on purpose. The grant is what the
    owner asked for and must read back exactly as they set it; this says whether
    asking for it achieves anything on this host, which is what lets a surface
    show the switch as granted AND say it is not running. Reporting only the
    grant is what makes the failure silent, and silent is the whole cost here: an
    owner sees transcripts scheduled, nothing ever uploads, and they find out at
    the host loss the feature exists to survive.

    Derived from :func:`scheduled_sessions_blocked_code` rather than deciding
    again, so prose can only ever describe the condition that function selected.
    """
    code = scheduled_sessions_blocked_code()
    if code is None:
        return None
    if code == BLOCK_HOST_UNSUPPORTED:
        return kind_unavailable_reason(KIND_SESSIONS)
    return _unattended_sessions_redaction_gap()


def due_for_sessions_nightly(account: str, now: Optional[dt.datetime] = None) -> bool:
    """True when the nightly SESSIONS archive is authorized, possible, and due.

    Four conditions, and the second is why this is not just
    :func:`due_for_nightly` with a different kind. A platform without
    descriptor-pinned traversal is NEVER due: :func:`run_sessions_backup` refuses
    there by design, so calling it anyway would raise on every wake, record a
    failed run and audit a failure every half hour for a payload that platform
    can never produce. Answering "not due" makes the capability question a
    scheduling fact rather than a recurring error.

    The redaction gap is read the same way and for the same reason: where the
    operator has asked for outbound redaction this payload cannot honour, the
    honest scheduling answer is "not due" rather than an unattended upload that
    ignores what they asked for.

    Both are read through :func:`scheduled_sessions_blocked_reason`, which is also
    what the status route reports. One predicate, so a surface cannot show this
    grant as running while the loop withholds it, or the reverse.

    The fourth condition is the retry backoff, and this kind needs it for the same
    reason the snapshot does rather than for a reason of its own: the two failure
    records are per kind, so a transcript archive failing deterministically backs
    off on its own count and a snapshot that is still working keeps its window. The
    two conditions above cannot cover it -- both describe a kind that is refused
    before it runs, while this one describes a kind that ran and raised.
    """
    if not nightly_sessions_enabled(account):
        return False
    if scheduled_sessions_blocked_reason() is not None:
        return False
    if not _a_day_since_last_run(account, KIND_SESSIONS, now):
        return False
    return not _backoff_withholds(account, KIND_SESSIONS, now)
