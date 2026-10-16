"""What one history delete settles around its unlink: the claim, the cron owners, the ledger.

The parts of deleting a transcript that must hold before and around the file going: the
claim that pins which row is deleted (:class:`_HistoryDeleteClaim`), the cron jobs and
channel bindings the session still owns and their release, the linked session key a channel
transcript resolves to, the work-ledger exclusion of the slot's units and its rollback, and
:func:`_delete_history_session`, the unlink. The routes that drive it live in
:mod:`.delete_routes`, the live-slot teardown in :mod:`.slot_teardown`. New delete
preconditions go here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable, Collection

from aiohttp import web

# The migration module owns the pre-migration leftover-tab spelling.
from kiro_crew.channel_transcript_migration import _orphan_target_stem
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable, cron_owner_matches
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.handlers.sessions_parts.app_scope import _app_owns_transcript
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.history import (
    ConversationLog,
    HistoryLockTimeout,
    transcript_lock_stems,
    transcript_stem,
    transcript_stems,
)
from kiro_crew.messaging.link import canonical_key

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


async def _owner_keys_bound_to_transcript(crons: Any, keys: Collection[str]) -> dict[str, set[str]]:
    """Per history key, the STORE-side owner keys whose transcript is that row.

    Returns ``{history key: exact owner keys}``. Raises ``CronStoreBusy`` (past the
    shared bounded backoff) and ``CronStoreUnreadable`` — the store's own two ways
    of saying it cannot be seen — so the caller refuses the delete with a distinct
    code for each. Any other failure is logged and answers ``{}``: unknown, proceed
    loudly.

    The transcript's ``linked_session_key`` is the funnel's primary source for a
    channel session's exact owner key, but it cannot be the only one. Two states
    have a channel-owned job and NO such metadata, and neither is an error:

    * the ORDERING WINDOW — ``cron_add`` stamps the channel key on the job in one
      transaction while the slot save publishes ``linked_session_key`` in
      another, so a delete landing between them reads absent metadata over a job
      that is already owned.
    * a LEGACY transcript written before that metadata existed at all.

    In both, "absent" means "not recorded here", not "no owner to lose" — and
    absent is also the ordinary state of every dashboard session, so refusing the
    delete is not available as an answer. So the binding is resolved from the
    OTHER side: the store knows each job's exact owner key, and this row's own
    transcript stem says which owner keys name THIS conversation. A job whose
    owner folds onto that stem owns this row, and its exact key -- the spelling
    the release needs and the stem cannot reconstruct (``_safe_key`` maps every
    ``:`` to ``_``) -- is right there on the job.

    Matched on the row's OWN stem, never a stripped one. An owner matches when
    this row's stem is one of the filenames THAT owner logs under -- its canonical
    stem or, for a Slack thread predating the ``slack:<ts>`` key, the bare
    ``thread_ts`` stem ``ConversationLog._path`` still falls back to (hence
    ``transcript_stems``: the canonical stem alone misses a legacy transcript's
    owner silently, and the delete then strands it). What this deliberately does
    NOT do is strip a ``dashboard_`` prefix off the ROW to reach ``slack_<ts>``,
    even though ``dashboard_slack_<ts>`` is a real shape for a pre-migration
    leftover tab whose conversation IS ``slack:<ts>``: the stripped form names a
    DIFFERENT session, derived from a string a client chose (``POST
    /api/chat/slots`` takes the slot name), so honouring it releases a LIVE
    channel session's jobs when an unrelated dashboard row is deleted for being
    spelled like it.

    No marker on the transcript can license that fold. ``linked_session_key`` and
    ``channel_origin`` both live on the metadata line of an AGENT-WRITABLE file,
    so an agent that can create the lookalike transcript can write the marker onto
    it — a marker-gated fold is as reachable as an ungated one, one step longer.
    Nor is there a server-side record to consult instead: the absent-metadata case
    is precisely the SLOTLESS one, so there is no live slot holding provenance.

    Hence the rule this function fails closed on: provenance for a destructive
    CROSS-SESSION action must come from outside agent-writable storage, and where
    none exists the action is not inferred. The cost is a genuinely channel-born
    leftover keeping a job owned by a session that is gone — bounded, visible
    (:func:`_warn_unprovable_channel_binding` names the owner, the candidate ids
    and the recovery command) and RECOVERABLE by id with ``kirocrew cron adopt
    <id> --release``. A forged release is none of those things.

    A PRESENT ``linked_session_key`` is a different question and is still
    honoured: :func:`_linked_session_key_for_history_key` asks whether the claim
    the row already carries is about ITSELF, comparing the agent-written value
    against the route-derived history key. That comparison can only NARROW what a
    row reaches, never widen it to a session the row does not name.

    Swept for EVERY delete rather than only when metadata is absent. Knowing it
    is absent means having read it, and that read lives inside the same lock hold
    as the unlink (see :func:`_delete_history_session`) precisely so no
    writer can land a key between them; splitting the hold to decide whether to
    sweep would reopen that window to save one cache-warm read. When metadata IS
    present the sweep simply agrees with it, and a duplicate candidate costs
    nothing: ``release_jobs_owned_by`` re-resolves ownership and liveness inside
    the store lock, so nothing here can release a job the store disagrees about.

    Reads through ``owner_keys_async`` — a STRICT locked scan that reloads through
    ``_sync_for_write`` and RAISES on either store failure. Neither cache-shaped
    read can be used here, and the distinction is the whole point of the sweep:
    ``list_jobs`` lags a cross-process write by up to one timer poll, and
    ``list_jobs_async`` locks but DEGRADES — it swallows ``CronStoreBusy`` to
    return the cache, and syncs through ``_sync()``, whose ``_load`` flattens an
    unreadable store to an empty job list. Both answer "no owners" for a store
    that could not be read, which is the one answer this caller must not accept:
    it is about to destroy the last record of the binding.

    Only the STORE's own two ways of saying "you cannot see me" fail CLOSED, and
    each PROPAGATES so the caller can name the cause: sustained ``CronStoreBusy``
    (retried on the shared backoff first) and ``CronStoreUnreadable``. In those the
    job set is unknown rather than empty, so the caller refuses instead of
    unlinking the last record of a binding it could not check, and answers with a
    code and remedy specific to the cause — a busy store wants a retry, an
    unreadable one wants the file repaired, and the by-id CLI cannot help with
    either. An UNEXPECTED exception is different in kind: not the store's verdict
    but a defect or an environment failure this sweep never named (a mock in a
    test, an ``OSError`` off a network mount), on a read that fronts EVERY history
    delete and the bulk clear. Refusing on it would let one cron-read fault block
    all history deletion, so the unknown case proceeds LOUDLY: the exception, the
    session keys and the by-id recovery command are logged at WARNING, no owner is
    released on the strength of a read that did not happen, and the delete goes
    ahead. That is the strand-and-recover posture
    :func:`_warn_unprovable_channel_binding` already takes — a job left owned by a
    departed session is bounded, visible and recoverable by id, and the
    alternative here is unbounded. An object with no ``owner_keys_async`` is a
    different case entirely and is quiet: nothing to sweep, nothing withheld.

    The stranded-binding warning is emitted only where the row's name suggests a
    binding this refuses to infer, which is rare — a leftover-shaped row with a
    matching owner in the store — so the ordinary delete pays nothing for it.
    """
    wanted = [k for k in keys if k]
    reader = getattr(crons, "owner_keys_async", None)
    if crons is None or not wanted or reader is None:
        return {}
    owners: set[str] = set()
    for attempt in range(_CRON_RELEASE_ATTEMPTS):
        try:
            owners = {str(owner) for owner in await reader() if owner}
        except CronStoreBusy:
            if attempt + 1 < _CRON_RELEASE_ATTEMPTS:
                await asyncio.sleep(_CRON_RELEASE_BACKOFF_SECS * (attempt + 1))
                continue
            logger.warning("History delete: cron store busy, owner sweep not performed")
            raise
        except CronStoreUnreadable as exc:
            logger.warning("History delete: cron owner sweep not performed: %s", exc)
            raise
        except Exception as exc:
            # Unknown, proceed loudly -- see the docstring. Not the store's own
            # verdict, and this sweep fronts every history delete, so the fault
            # is named with its recovery rather than allowed to block them all.
            logger.warning(
                "History delete: cron owner sweep failed unexpectedly for %s (%s: %s); cron "
                "ownership is unknown and the delete proceeds, so a job owned by this session "
                "may be left owned by a session that no longer exists — release it by id with "
                "`kirocrew cron adopt <id> --release` (`kirocrew cron list` shows the "
                "owners)",
                ", ".join(wanted),
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return {}
        break
    found: dict[str, set[str]] = {}
    for history_key in wanted:
        stem = transcript_stem(history_key)
        matched = {owner for owner in owners if stem in transcript_stems(owner)}
        if matched:
            found[history_key] = matched
        # Not folded in: the stripped stem names ANOTHER session, on the strength
        # of this row's name alone. Say so instead, so the strand is recoverable.
        stripped = _orphan_target_stem(stem)
        if stripped:
            unprovable = {owner for owner in owners if stripped in transcript_stems(owner)}
            if unprovable:
                _warn_unprovable_channel_binding(crons, unprovable, history_key)
    return found


async def _owner_keys_after_unlink(crons: Any, keys: Collection[str]) -> dict[str, set[str]]:
    """The post-unlink owner re-sweep: the pre-sweep's read, the opposite failure direction.

    Both permanent-delete paths take :func:`_owner_keys_bound_to_transcript` twice:
    once BEFORE the unlink, when a store that cannot be seen refuses the delete,
    and once AFTER it has committed, to catch the one arrival the first pass
    cannot see. The pre-sweep knows only the jobs that existed when it scanned,
    and a channel ``cron_add`` commits under its own store lock: a job born
    between the scan and the unlink is stamped with a key the delete retires, and
    the store's add-time guard does not cover it (it resolves a ``cron:`` parent
    id; a channel key names no job). The single delete explains the window and
    why the two locks are not spanned; the bulk clear pays ONE such read for the
    whole batch, so *keys* is a collection.

    Here the rows are already gone, so refusing is not available. The store's own
    two failures -- busy past the shared backoff, unreadable -- are therefore
    named LOUDLY instead: the deleted rows, the cause and the by-id recovery
    (``cron adopt <id> --release``), at WARNING, and the caller releases what the
    pre-sweep found. That is the stance the sweep itself takes on an unexpected
    error, and the strand is bounded and recoverable by id. An add landing after
    this pass is the ordinary stranded case the release already reports.
    """
    try:
        return await _owner_keys_bound_to_transcript(crons, keys)
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        logger.warning(
            "History delete: %s deleted, but the post-delete owner scan could not read the "
            "cron store (%s), so a job created during the delete may still be owned "
            "by a deleted session — release it by id with `kirocrew cron adopt <id> --release` "
            "(`kirocrew cron list` shows the owners)",
            ", ".join(key for key in keys if key),
            exc,
        )
        return {}


def _stranded_job_ids(crons: Any, owner_keys: set[str]) -> list[str]:
    """Ids of jobs owned by *owner_keys*, for a warning to NAME. Never decides.

    Cache-only (``list_jobs``) and never raises ``CronStoreBusy``, so this
    diagnostic cannot fail for the same reason a release just did. Its staleness
    is acceptable precisely because nothing acts on it — see
    ``CronService.release_jobs_owned_by``, which re-resolves ownership inside the
    store lock. Answers ``[]`` rather than propagating, since a warning that
    raises replaces the message an operator needs with a traceback.
    """
    try:
        return sorted(
            job.id
            for job in crons.list_jobs(include_disabled=True)
            if getattr(job, "session_key", "")
            and any(
                cron_owner_matches(getattr(job, "session_key", ""), target) for target in owner_keys
            )
        )
    except Exception:
        logger.warning("History delete: could not enumerate stranded cron jobs", exc_info=True)
        return []


def _warn_unprovable_channel_binding(crons: Any, owner_keys: set[str], history_key: str) -> None:
    """Name a binding the sweep declined to infer, and how to finish it by hand.

    Fires when the deleted row's name STRIPS to a channel stem that some job in
    the store is actually owned under. Either the row is that channel
    conversation's leftover tab file and the job is now genuinely orphaned, or the
    row is an unrelated dashboard session that merely shares the spelling — and
    nothing durable distinguishes them, because every marker that could lives in
    the same agent-writable file as the name (see
    :func:`_owner_keys_bound_to_transcript`).

    Refusing to guess is the safe half; saying nothing would not be. An
    unrecoverable strand is one nobody was told about, so this logs the owner, the
    candidate ids and the one command that completes the release, turning a silent
    conservatism into a documented manual step.
    """
    stranded = _stranded_job_ids(crons, owner_keys)
    logger.warning(
        "History delete: %s strips to a channel stem owning cron job(s), but nothing outside the "
        "agent-writable transcript proves this row IS that conversation, so ownership was NOT "
        "released for owner(s) %s; job(s) %s may now be owned by a session that no longer exists "
        "— release them with `kirocrew cron adopt <id> --release`",
        history_key,
        ", ".join(sorted(owner_keys)),
        ", ".join(stranded) if stranded else "unknown (store not readable from cache)",
    )


#: Machine-readable codes for a delete the cron-ownership check REFUSES. Each names
#: a distinct cause with a distinct remedy, and the dashboard client keys its
#: localized message on the code rather than rendering the English ``error``.
CRON_STORE_UNREADABLE_CODE = "cron_store_unreadable"
CRON_STORE_BUSY_CODE = "cron_store_busy"
CRON_OWNERSHIP_UNKNOWN_CODE = "cron_ownership_unknown"

#: Reported for a row whose ledger exclusions could not be written. The transcript is
#: left alone: deleting it would leave its work state readable by the next session
#: in the same slot, which is not recoverable by the person it happens to.
LEDGER_EXCLUSION_UNWRITABLE_CODE = "ledger_exclusion_unwritable"


class _AppOwnershipLost(Exception):
    """An app's delete REFUSED because the transcript stopped recording that app.

    Raised by :func:`_delete_history_session` inside the transcript hold, before
    any exclusion or unlink, so the row is intact.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
_AppOwnershipLost.__module__ = "kiro_crew.dashboard.handlers.sessions"


class _OwnerKeyUnreadable(Exception):
    """A delete REFUSED because the exact cron owner key of the row cannot be read.

    Raised by :func:`_delete_history_session` with the row intact. It is
    an exception rather than a ``None`` result so the bulk clear can tell this
    refusal apart from the ``None`` a pinned row answers under ``skip_pinned``,
    and report it by id with :data:`CRON_OWNERSHIP_UNKNOWN_CODE`.
    """


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
_OwnerKeyUnreadable.__module__ = "kiro_crew.dashboard.handlers.sessions"


def _cron_store_refusal(exc: Exception, **counts: Any) -> web.Response:
    """409 for a delete whose owner sweep could not SEE the cron store, by cause.

    ``CronStoreUnreadable`` carries the store path and the repair route in its
    own message (``CronService._unreadable_error`` is the one wording every
    surface reads verbatim), so that text is surfaced as-is. The ``kirocrew
    cron adopt`` CLI is deliberately NOT named here: it reads the same file,
    so on an unreadable store it cannot work either. ``CronStoreBusy`` is
    contention and wants nothing but a retry. Neither is a filesystem failure of
    the delete, so neither is ``200 {ok: false}`` -- a 409 is what the dashboard
    client rejects on, which is what keeps the refused row in the sidebar.
    ``counts`` lets the bulk clear carry its ``cleared``/``skipped``/``failed``
    tallies beside the refusal, for callers that read the body.
    """
    if isinstance(exc, CronStoreUnreadable):
        code = CRON_STORE_UNREADABLE_CODE
        error = (
            "the cron store could not be read, so cron ownership could not be checked "
            f"and nothing was deleted: {exc}"
        )
    else:
        code = CRON_STORE_BUSY_CODE
        error = (
            "the cron store is busy, so cron ownership could not be checked and nothing "
            "was deleted; retry in a moment."
        )
    if not counts:
        return web.json_response({"ok": False, "error": error, "code": code}, status=409)
    return web.json_response(
        {
            "ok": False,
            "error": error,
            "code": code,
            "cleared": counts.get("cleared", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
        },
        status=409,
    )


def _cron_ownership_unknown_refusal(crons: Any, owner_keys: Collection[str]) -> web.Response:
    """409 for a row whose transcript metadata could not be read.

    Here the remedy IS the CLI plus transcript repair: the store is readable,
    the row is not, so the operator releases candidate jobs by id, repairs the
    row's metadata, and retries. ``owner_keys`` are the owners the store-side
    sweep bound to this row ahead of the read, and the message names their job
    ids when the cache can list them -- a cache-only listing that decides nothing
    (see :func:`_stranded_job_ids`).
    """
    stranded = _stranded_job_ids(crons, set(owner_keys)) if owner_keys else []
    ids = f" (candidate job id(s): {', '.join(stranded)})" if stranded else ""
    return web.json_response(
        {
            "ok": False,
            "error": (
                "the cron jobs this session owns could not be determined because its "
                "transcript metadata is unreadable, so deleting it would leave them owned "
                "by nobody. Release candidate jobs with `kirocrew cron adopt <id> --release`"
                f"{ids} -- `kirocrew cron list` shows the owners -- repair the "
                "transcript metadata, then delete the session again."
            ),
            "code": CRON_OWNERSHIP_UNKNOWN_CODE,
        },
        status=409,
    )


@dataclass(frozen=True)
class _HistoryDeleteClaim:
    """Slot, manager, and exact cron ownership captured before transcript unlink."""

    registry_key: str | None
    slot: Any | None
    history_key: str | None
    session_key: str | None
    session_generation: int
    path_match_verified: bool
    complete: bool
    slot_task: Any | None = None
    cron_owner_keys: frozenset[str] = frozenset()
    #: Unit ids the locked transaction excluded from this slot's ledger. Recorded there
    #: rather than by the caller because only the resolved claim PROVES which slot owns
    #: this transcript: a provisional claim can name a stacked duplicate's slot, and
    #: excluding on it would empty an unrelated LIVE session's record.
    ledger_excluded_units: frozenset[str] = frozenset()
    #: Whether that transaction TOMBSTONED the slot's legacy pre-projection document --
    #: wrote the committed carry marker itself, rather than finding one already there.
    #: Only the writer may take it back, so this travels with the claim to the rollback.
    ledger_carry_tombstoned: bool = False
    #: The ACP session id behind *session_key*, read before the teardown that
    #: destroys the session it names. This is what identifies the session
    #: LEDGER, whose unit id is the ACP id rather than the slot key, and it is
    #: captured here for the same reason every other field is: after the
    #: teardown there is nothing left to ask.
    acp_session_id: str | None = None


def _history_delete_candidate_keys(key: str) -> tuple[str, ...]:
    """Every exact/legacy spelling that may index *key*'s dashboard slot."""
    stripped = key
    if stripped.startswith("dashboard:"):
        stripped = stripped[len("dashboard:") :]
    while stripped.startswith("dashboard_"):
        stripped = stripped[len("dashboard_") :]
    canonical = canonical_key(key)
    return tuple(
        dict.fromkeys(
            (
                key,
                stripped,
                "dashboard_" + key,
                _normalize_slot_key(key),
                canonical,
                _normalize_slot_key(canonical),
            )
        )
    )


def _capture_history_delete_claim(state: DashboardState, key: str) -> _HistoryDeleteClaim:
    """Capture exact slot identity and monotonic manager generation pre-unlink."""
    deleted_stems = transcript_stems(key)
    deleted_identities = {ConversationLog._canonical_key(stem) for stem in deleted_stems}
    for candidate_key in _history_delete_candidate_keys(key):
        candidate_slot = state._slots.get(candidate_key)
        if candidate_slot is None:
            continue
        try:
            history_key = slot_history_key(candidate_slot)
            candidate_stems = transcript_stems(history_key)
            owned_stems = {ConversationLog._canonical_key(stem) for stem in candidate_stems}
        except Exception:
            return _HistoryDeleteClaim(None, None, None, None, 0, True, False)
        if deleted_identities.isdisjoint(owned_stems):
            continue
        # Only byte-identical filename-stem sets prove ownership without I/O.
        # Canonicalization can collapse distinct stacked dashboard files, and
        # canonical Slack keys may resolve to a pre-migration bare file. Resolve
        # every other alias in the delete worker under the transcript lock.
        path_match_verified = deleted_stems == candidate_stems
        try:
            session_key = effective_session_key(candidate_slot)
            generation = state.sessions.session_generation(session_key)
        except Exception:
            return _HistoryDeleteClaim(
                candidate_key,
                candidate_slot,
                history_key,
                None,
                0,
                path_match_verified,
                False,
            )
        # Best-effort and read LAST: an unreadable ACP id must not downgrade a
        # claim the checks above already completed, because every other cleanup
        # this claim authorizes is more important than collecting one crew log --
        # which the retention sweep collects anyway once the session is closed.
        # Type-checked rather than merely truthy: this value goes on to ADDRESS a
        # directory, so anything that is not a real id must read as absent.
        try:
            resumable = state.sessions.resumable_sid(session_key)
        except Exception:
            logger.debug(
                "History delete: ACP session id unreadable for %s; leaving its crew log "
                "to the retention sweep",
                session_key,
                exc_info=True,
            )
            resumable = None
        acp_session_id = resumable if isinstance(resumable, str) and resumable else None
        return _HistoryDeleteClaim(
            candidate_key,
            candidate_slot,
            history_key,
            session_key,
            generation,
            path_match_verified,
            True,
            getattr(candidate_slot, "task", None),
            acp_session_id=acp_session_id,
        )
    return _HistoryDeleteClaim(None, None, None, None, 0, True, True)


def _resolve_history_delete_claim(
    log: ConversationLog,
    key: str,
    claim: _HistoryDeleteClaim,
) -> _HistoryDeleteClaim:
    """Verify ambiguous transcript ownership in the off-loop delete worker."""
    if claim.slot is None or claim.path_match_verified:
        return claim
    if claim.history_key is None:
        return replace(
            claim,
            registry_key=None,
            slot=None,
            session_key=None,
            path_match_verified=True,
            complete=False,
            # Travels with ``session_key``: a claim that names no session must not
            # still name a session's ledger.
            acp_session_id=None,
        )
    try:
        matches = log._path(key).stem == log._path(claim.history_key).stem
    except Exception:
        logger.warning(
            "History delete: could not resolve pre-unlink transcript ownership for %s",
            key,
            exc_info=True,
        )
        return replace(
            claim,
            registry_key=None,
            slot=None,
            session_key=None,
            path_match_verified=True,
            complete=False,
            # Travels with ``session_key``: a claim that names no session must not
            # still name a session's ledger.
            acp_session_id=None,
        )
    if matches:
        return replace(claim, path_match_verified=True)
    # The alias located a slot, but that slot currently resolves to another
    # transcript (for example canonical and legacy Slack files coexist).
    return _HistoryDeleteClaim(None, None, None, None, 0, True, claim.complete)


def _linkable_stems_for_history_key(key: str) -> set[str]:
    """Every filename stem a row named *key* may legitimately be LINKED as.

    ``slot_history_key`` returns ``linked_session_key`` verbatim as the slot's
    transcript key whenever it is set, so the binding is not a free-form pointer:
    a row carrying a linked key must BE that key's own transcript. That makes the
    expected relationship checkable from the history key alone, which is what
    :func:`_linked_session_key_for_history_key` needs — the history key arrives
    from the route or the session list and is not attacker-writable, while the
    metadata value is.

    Only the row's CANONICAL stem. A leftover-shaped name is deliberately NOT
    stripped to reach the channel session it is spelled like: ``dashboard:slack_
    <ts>`` is not the session ``slack:<ts>``, so admitting the stripped form would
    WIDEN the row to a session it does not name -- and since the transcript store
    is agent-writable, a forged ``linked_session_key`` on a lookalike file would
    then release a LIVE channel session's cron ownership on any history delete.
    That is the same stance :func:`_owner_keys_bound_to_transcript` takes on the
    store side, for the same reason, so the two cannot disagree about what a row
    may reach.

    The bare ``thread_ts`` stem a Slack thread predating the canonical
    ``slack:<ts>`` key still logs under needs no entry here: it is contributed on
    the VALUE side by ``transcript_stems``, where ``ConversationLog._path``'s
    fallback lives.

    A genuinely channel-born leftover therefore keeps its job owned by a session
    that is gone. That cost is bounded, visible -- the sweep's
    :func:`_warn_unprovable_channel_binding` names the owner, the candidate job
    ids and the command -- and RECOVERABLE by id through ``kirocrew cron adopt
    <id> --release``. A forged release is none of those things.

    Scoped to VALIDATING a link the row already carries: the row is CLAIMING a
    binding, and the check asks only whether the claim is about itself, comparing
    the agent-written value against the route-derived key. Restricted to the
    canonical stem, that comparison can only ever NARROW what this row reaches.
    """
    stems = {transcript_stem(key)}
    orphan_of = _orphan_target_stem(transcript_stem(key))
    if orphan_of:
        # A leftover-shaped row is NOT the channel session it is spelled like, so
        # the claim is refused rather than honoured. Warned about, not silent: the
        # sweep's _warn_unprovable_channel_binding names the owner, the candidate
        # job ids and the by-id recovery command.
        logger.warning(
            "History delete: session %s is spelled like a leftover of %r; a link it carries "
            "cannot license releasing that session's jobs, so only its own key is accepted",
            key,
            orphan_of,
        )
    return stems


def _linked_session_key_for_history_key(log: Any, key: str) -> tuple[str, bool]:
    """``(exact session key or "", metadata was READABLE)`` for a history row.

    A channel-born tab persists ``linked_session_key`` in its transcript
    metadata because nothing recreates that binding on restart. Cron jobs the
    session created are stamped with THAT key, not with any ``dashboard:``
    spelling of the transcript name, so it is the only string that can match
    their owner — and after the transcript is unlinked there is nowhere left to
    read it from.

    Reads through ``get_metadata_status``, not ``get_metadata``, and reports the
    readability flag rather than folding it away. The two cases the caller has to
    tell apart both answer ``""``:

    * **ABSENT** — no metadata line, or a metadata line carrying no
      ``linked_session_key`` (readable ``True``). ``{}`` is this case, not a
      quieter malformed one: it is the store's own answer for a row with no
      metadata line and for a first line that is not metadata-typed. There is no
      owner key to lose, so the delete proceeds.
    * **UNREADABLE** — an existing metadata line that could not be read, or that
      came back in a shape this cannot trust: a partial write, a prior ENOSPC,
      on-disk corruption, an agent-edited transcript (readable ``False``,
      including every exception this raises). The row MAY name a cron owner, and
      the unlink destroys the only copy, so deleting here strands that ownership
      with nothing left to notice it — :func:`_warn_stranded_cron_ownership`
      fires only when a release FAILS, never when the key was never read. The
      caller refuses the delete instead.

    A successful read is NOT automatically a trustworthy one. Two payloads are
    reported unreadable rather than folded into the absent case, because each
    would otherwise answer ``""`` (or a key that should never have been honoured)
    and let the delete proceed on a row that may well name an owner:

    * a payload that is not a ``dict`` at all — a bare string, a list, a scalar.
      The store's contract is a mapping, so anything else means the line was
      salvaged, hand-edited or partially written, and its ``linked_session_key``
      is unreadable rather than absent.
    * a ``linked_session_key`` present but not a ``str``. The only writer
      (``slot_projection``) persists a string field, so a mapping or a list here
      is corruption; ``str()``-ing it would mint a key that matches no job, and
      the release would report success having matched nothing.

    A ``linked_session_key`` that does not name THIS row (see
    :func:`_linkable_stems_for_history_key`) is a third case, and deliberately
    not an unreadable one: it answers ``("", True)`` with a warning, so the
    delete proceeds and releases nothing on account of the claim. The transcript
    store is agent-writable, so the value is untrusted INPUT to a privileged
    action -- whatever it names is fed to ``release_jobs_owned_by`` as a retired
    owner, and left unvalidated an agent that can write metadata would point its
    own row at a VICTIM session and have this delete clear the cron ownership of
    the victim. Dropping the claim keeps that closed. Refusing the delete did
    more than that: the mismatch is a property of the file, so a legitimate
    pre-migration leftover tab (``dashboard_slack_<ts>``) was refused identically
    on every retry, and the 409 remedy (release by id, delete again) could never
    clear it.
    """
    try:
        reader = getattr(log, "get_metadata_status", None)
        meta, readable = reader(key) if reader is not None else (log.get_metadata(key), True)
    except Exception:
        logger.warning("History delete: metadata read failed for %s", key, exc_info=True)
        return "", False
    if not readable:
        return "", False
    if not isinstance(meta, dict):
        logger.warning(
            "History delete: metadata for %s is not a mapping (%s); treating it as unreadable",
            key,
            type(meta).__name__,
        )
        return "", False
    linked = meta.get("linked_session_key")
    if linked is not None and not isinstance(linked, str):
        logger.warning(
            "History delete: linked_session_key for %s is a %s, not a string; treating the "
            "metadata as unreadable",
            key,
            type(linked).__name__,
        )
        return "", False
    if linked and not set(transcript_stems(linked)) & _linkable_stems_for_history_key(key):
        # NOT the unreadable case. The row is readable and makes a claim this
        # funnel will not honour -- a stem mismatch is "I will not act on this",
        # not "I cannot read this row". Refusing the delete here made a
        # legitimate pre-migration leftover tab (``dashboard_slack_<ts>``)
        # permanently undeletable: the mismatch is a property of the file, so
        # every retry and the 409 remedy itself read it identically. The claim is
        # dropped instead: the delete proceeds and releases NOTHING on its
        # account, so the other session keeps its jobs; a genuine leftover keeps
        # its strand, which the sweep names with the by-id recovery command.
        logger.warning(
            "History delete: session %s claims linked_session_key %r, which is not the "
            "session this transcript belongs to -- a row can only be linked to the session "
            "whose transcript it IS. The claim is not honoured: the row is deleted without "
            "releasing any cron job owned by %r; a job it really owned is recoverable by "
            "id (the owner sweep warning names the command).",
            key,
            linked,
            linked,
        )
        return "", True
    return (linked or ""), True


def _delete_history_session(
    log: ConversationLog,
    key: str,
    claim: _HistoryDeleteClaim,
    *,
    skip_pinned: bool = False,
    exact_owner_keys: Collection[str] = (),
    # Returns a ``session_ledger.SlotExclusion``: the ids added AND whether the slot
    # was tombstoned. Typed loosely for the same reason the module is imported inside
    # the functions that use it -- this handler does not import the ledger at module
    # scope, and the tests pass a stub in its place.
    exclude: "Callable[[str], Any] | None" = None,
    owner_app: str = "",
) -> tuple[bool | None, _HistoryDeleteClaim]:
    """Bind slot and cron ownership, then unlink under one transcript lock.

    With *owner_app* set (an app caller), :func:`_app_owns_transcript` is re-judged
    first inside the hold and a failure raises :class:`_AppOwnershipLost` before
    anything is excluded or unlinked, so the handler's earlier check cannot be
    outlived by a same-key delete and recreate.

    *exclude* records this slot's ledger units and answers the ids it added. It runs
    INSIDE the lock and only once the claim is RESOLVED, because that is the first point
    where the slot owning this transcript is proved: a provisional claim can name a
    stacked duplicate's slot, and excluding on it empties an unrelated live session's
    record. A refusal propagates, so nothing is unlinked when the exclusion cannot be
    written.
    """
    # ``delete_session`` re-enters one of these locks on the same worker thread.
    # Acquire every stable stem in deterministic order so canonical Slack restore
    # and a legacy-file delete cannot synchronize on different sidecars.
    try:
        with log.locked_stems(transcript_lock_stems(key)):
            if owner_app and not _app_owns_transcript(log, owner_app, key):
                raise _AppOwnershipLost(key)
            resolved_claim = _resolve_history_delete_claim(log, key, claim)
            linked, readable = _linked_session_key_for_history_key(log, key)
            if not readable:
                logger.error(
                    "History delete REFUSED for %s: its metadata is unreadable, so the exact "
                    "cron owner key cannot be read and deleting the transcript would strand "
                    "any cron job it owns under a key no session can ever present again. "
                    "The session was NOT deleted. Release its jobs by hand first with "
                    "`kirocrew cron adopt <id> --release` (`kirocrew cron list` shows the "
                    "owners), repair the transcript metadata, then delete it again.",
                    key,
                )
                raise _OwnerKeyUnreadable(key)
            owner_keys = {owner for owner in exact_owner_keys if owner}
            if (
                resolved_claim.complete
                and resolved_claim.path_match_verified
                and resolved_claim.session_key
            ):
                owner_keys.add(resolved_claim.session_key)
            if linked:
                owner_keys.add(linked)
            resolved_claim = replace(
                resolved_claim,
                cron_owner_keys=frozenset(owner_keys),
            )
            if (
                exclude is not None
                and resolved_claim.registry_key
                and resolved_claim.complete
                and resolved_claim.path_match_verified
            ):
                recorded = exclude(resolved_claim.registry_key)
                resolved_claim = replace(
                    resolved_claim,
                    ledger_excluded_units=frozenset(recorded.added),
                    ledger_carry_tombstoned=recorded.carry_tombstoned,
                )
            if skip_pinned:
                result = log.delete_session(key, skip_pinned=True)
            else:
                result = log.delete_session(key)
    except HistoryLockTimeout:
        logger.warning("delete_session: lock timeout, not deleting key=%s", key)
        return False, claim
    return result, resolved_claim


async def _unstrand_shared_ledger_unit(
    state: DashboardState, session_ledger: Any, claim: _HistoryDeleteClaim
) -> None:
    """Undo the exclusion when ANOTHER retained key still runs this ACP session.

    Two keys imported onto one ACP session share its crew-log unit. Deleting one of
    them excludes that unit, but the delete preserves it for the survivor -- whose
    ledger would then read empty for good, because the rollback for a delete that did
    not happen never covers a delete that did. The exclusion belongs to the conversation
    that went, so it comes back out when the session is still somebody's.
    """
    units = tuple(claim.ledger_excluded_units)
    sid = claim.acp_session_id or ""
    if not units or not sid or not claim.registry_key:
        return
    try:
        # EXCLUDING the key this delete retired. Its own mapping can still be present
        # here, and a lookup that returns it reads as "nobody else has this session",
        # which is the answer that leaves the real survivor's record empty.
        survivor = state.sessions.find_key_by_sid(sid, exclude=claim.session_key or "")
    except Exception:
        logger.warning(
            "History delete: could not check whether %r still runs session %r; "
            "its ledger exclusions stand",
            claim.registry_key,
            sid,
            exc_info=True,
        )
        return
    if not survivor:
        return
    logger.info(
        "History delete: session %r is still run by %r, so slot %r's ledger exclusion "
        "of its unit is taken back",
        sid,
        survivor,
        claim.registry_key,
    )
    # The transcript IS gone here, so this is not a delete that did not happen: only the
    # shared unit comes back out, and the slot's legacy document stays tombstoned.
    await _rollback_ledger_exclusion(session_ledger, claim, restore_carry=False)


async def _rollback_ledger_exclusion(
    session_ledger: Any, claim: _HistoryDeleteClaim, *, restore_carry: bool
) -> None:
    """Take back what the locked transaction excluded, for a delete that did not happen.

    Only the ids that transaction reported adding, so a concurrent delete of the same
    slot keeps its own. Off-loop because the rewrite fsyncs under the slot's lock.

    *restore_carry* has NO default, so every caller states which case it is. True where
    the delete did not proceed: the session still exists, so the legacy document this
    transaction tombstoned is still owed to it. False where the transcript IS gone and
    only a shared unit comes back out -- a delete of this slot proceeded, and the
    document may be the deleted conversation's, so the tombstone stands.

    Runs when EITHER the ids or the tombstone need taking back. Gating it on the ids
    alone would skip the whole rollback for a delete that recorded no units and
    tombstoned the slot, which is the empty-unit delete.
    """
    units = tuple(claim.ledger_excluded_units)
    lift = restore_carry and claim.ledger_carry_tombstoned
    if not claim.registry_key or not (units or lift):
        return
    await asyncio.to_thread(
        session_ledger.unexclude_units, claim.registry_key, units, restore_carry=lift
    )


def _ledger_rollback_refusal() -> web.Response:
    """503 for a rollback that could not be written: the record reads empty until it is."""
    return web.json_response(
        {
            "error": (
                "This session was not deleted, but restoring its ledger record failed, so "
                "the record reads empty. Try again."
            ),
            "code": "ledger_rollback_failed",
        },
        status=503,
    )


def _exclude_slot_units(
    state: DashboardState, session_ledger: Any, claim: _HistoryDeleteClaim
) -> Any:
    """List the slot's crew-log units and exclude them; return what was RECORDED.

    A ``session_ledger.SlotExclusion``: the ids this transaction added and whether it
    tombstoned the slot's legacy document. Both are what a rollback may take back, and
    neither is inferable afterwards, which is why they travel with the claim.

    One thread hop for the whole transaction, because both halves block: the listing
    walks the crew-log root and reads a header per unit, and the write fsyncs under the
    slot's lock. Only the ids this call added come back, so a rollback cannot take away
    an exclusion a concurrent delete of the same slot recorded.

    A slot key is RECYCLED, and the listing is by slot key, so the units it returns are
    not all this conversation's: a reset in the window before the delete gives the slot a
    live successor whose unit lands under the same key. Two things keep the successor's
    record out of it -- the captured session GENERATION must still be current, and the
    unit the slot is serving on NOW is never excluded. The removal itself is fenced by
    ACP session id and does not need this; the exclusion does, because its key is the
    recyclable one.

    BOTH are re-read AFTER the listing, immediately before the ids are persisted, because
    either read taken before it cannot describe what the listing then returned. What makes
    the second generation read sufficient rather than merely narrower: a successor's unit
    is written by a session that published its reservation on the slot key first, and that
    publication ADVANCES the generation -- so a unit new enough to be a successor's cannot
    be in the listing without the generation having already moved by the time it is
    re-read. The first read stays, to refuse a stale claim before doing the walk at all.
    """
    from kiro_crew.crew_log.store import session_units_for_slot

    slot_key = claim.registry_key or ""

    def prove_generation() -> None:
        """Refuse unless the slot is still on the generation the claim captured."""
        if not claim.session_key:
            return
        try:
            if state.sessions.session_generation(claim.session_key) != claim.session_generation:
                raise session_ledger.LedgerExclusionError(
                    f"slot {slot_key!r} was reset before the delete; its units are a "
                    "successor's and must not be excluded"
                )
        except session_ledger.LedgerExclusionError:
            raise
        except Exception as exc:
            # Cannot prove the generation, so cannot prove which conversation the units
            # belong to. Refusing leaves the row deletable again; excluding could empty a
            # live successor's record for good.
            raise session_ledger.LedgerExclusionError(
                f"slot {slot_key!r}'s session generation is unreadable"
            ) from exc

    def live_unit() -> str:
        """The unit the slot serves NOW, or ``""`` when that is this conversation's own.

        The live unit is protected ONLY when it is not this conversation's. A slot still
        serving the session being deleted reports that session as live, and skipping it
        would leave the deleted conversation's own unit foldable -- the post-unlink stage
        would then be the only thing excluding it, and a failure there has nothing left
        to refuse. Anything else the slot is serving now belongs to a successor.
        """
        sid = _live_slot_sid(state, slot_key)
        return "" if sid == (claim.acp_session_id or "") else sid

    def listed_units() -> "tuple[str, ...]":
        """Every unit whose header names the slot; refuses when that cannot be complete.

        An established unit left out because its header would not read stays foldable
        by the next session on the recycled slot key, so a short listing is no listing.
        A crew-log store never created holds no units.
        """
        try:
            return session_units_for_slot(slot_key, strict=True)
        except FileNotFoundError:
            return ()
        except Exception as exc:
            raise session_ledger.LedgerExclusionError(
                f"slot {slot_key!r}'s crew logs could not all be listed"
            ) from exc

    prove_generation()
    protect = live_unit()
    units = tuple(unit for unit in listed_units() if unit != protect)
    # Re-read, in this order: the live unit first, so a successor that has recorded but
    # whose generation this thread has not observed yet is still dropped, then the
    # generation as the LAST thing before the write.
    protect_now = live_unit()
    units = tuple(unit for unit in units if unit != protect_now)
    prove_generation()
    return session_ledger.exclude_units(slot_key, units)


def _live_slot_sid(state: DashboardState, slot_key: str) -> str:
    """The ACP session id the slot is serving on RIGHT NOW, or ``""``.

    Read at exclusion time rather than taken from the claim, because the claim names the
    conversation being deleted and this names whoever holds the slot key now.
    """
    from kiro_crew.crew_log.resolve import unit_for_session_key

    try:
        slot = state._slots.get(slot_key)
        if slot is None:
            return ""
        return str(unit_for_session_key(state.sessions, effective_session_key(slot)) or "")
    except Exception:
        logger.debug("History delete: resolving the slot's live unit failed", exc_info=True)
        return ""


# A release that loses the store-lock race is not recoverable later: the session
# is already gone, nothing re-runs this funnel, and the job's owner key can never
# be presented again. CronStoreBusy is transient contention (a large atomic save
# on network storage, the CLI process, the off-loop batch worker), so a short
# bounded backoff clears essentially every real case; past that the failure is
# reported loudly instead of dropped.
_CRON_RELEASE_ATTEMPTS = 3
_CRON_RELEASE_BACKOFF_SECS = 0.2


def _warn_stranded_cron_ownership(crons: Any, owner_keys: set[str], reason: str) -> None:
    """Name the owner and the jobs whose ownership could not be released."""
    stranded = _stranded_job_ids(crons, owner_keys)
    logger.warning(
        "History delete: cron ownership release FAILED (%s) after %d attempt(s) for owner(s) %s; "
        "job(s) %s may still be owned by a session that no longer exists — release them with "
        "`kirocrew cron adopt <id> --release`",
        reason,
        _CRON_RELEASE_ATTEMPTS,
        ", ".join(sorted(owner_keys)),
        ", ".join(stranded) if stranded else "unknown (store not readable from cache)",
    )


async def _release_cron_ownership(crons: Any, owner_keys: set[str]) -> list[str]:
    """Release every cron job owned by a RETIRED key among ``owner_keys``.

    Delegates to :meth:`CronService.release_jobs_owned_by`, which resolves BOTH
    decisions inside the store lock on its freshly reloaded state: which keys name
    a retired principal, and which jobs still carry one of them. Nothing is
    filtered here, deliberately — every candidate key goes down. Reading either
    decision from ``list_jobs`` would read a cache with up to one timer-poll
    interval of cross-process staleness, and both answers are then wrong in a way
    that strands jobs this funnel will never revisit: a job re-adopted since the
    read gets its new owner cleared, and a cron deleted by the CLI inside the
    window is called live so its children are never released.

    :class:`CronStoreBusy` is retried with a bounded backoff. Any failure that
    outlives the retries — sustained contention, or an unreadable store that
    ``_sync_for_write`` refuses to write over — is surfaced by
    :func:`_warn_stranded_cron_ownership` naming the owner and the candidate job
    ids, because nothing re-runs this funnel and the deleted session can never
    present its key again. Returns the ids released (empty when there was nothing
    to release, and when the release ultimately failed).
    """
    keys = {k for k in owner_keys if k}
    if crons is None or not keys:
        return []
    reason = "cron store busy"
    for attempt in range(_CRON_RELEASE_ATTEMPTS):
        try:
            return await crons.release_jobs_owned_by(keys)
        except CronStoreBusy:
            if attempt + 1 < _CRON_RELEASE_ATTEMPTS:
                await asyncio.sleep(_CRON_RELEASE_BACKOFF_SECS * (attempt + 1))
        except Exception as exc:
            # Not contention, so a retry cannot help (an unreadable store, a
            # failed save). Still report it through the recovery path rather than
            # letting the caller's generic handler log it without the ids or the
            # command that fixes it.
            reason = f"{type(exc).__name__}: {exc}"
            break
    _warn_stranded_cron_ownership(crons, keys, reason)
    return []
