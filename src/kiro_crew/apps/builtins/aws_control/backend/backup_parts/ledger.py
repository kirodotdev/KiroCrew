"""The run ledger: completed runs, and what this install uploaded.

:func:`_record_run` writes a completed upload's record, and :func:`_record_skip` an
unchanged run's, through the state transaction. The projections read the persisted
records merged with this process's held ones, so an upload whose state write failed
still counts as this install's own.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import uuid
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.identity import (
    _KIND_BY_SUBPATH,
    KIND_SUBPATHS,
    _key_segments,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import (
    _RUN_CONVERSATIONS_RETAINED,
    _account_state,
    _account_view,
    _clear_nightly_failure,
    _locked_state_update,
    _merge_unpersisted,
    _merge_uploads,
    _remember_unpersisted,
    _run_lock,
    _set_conversations_retained,
    _state_key,
    _StateUnreadable,
    _unpersisted_lock,
    _unpersisted_runs,
    _unpersisted_uploads,
    _unpersisted_versions,
)

logger = logging.getLogger(_FACADE_MODULE)


def uploaded_objects(account: str) -> dict[str, str]:
    """Every archive key THIS install recorded uploading, with its body fingerprint.

    The trusted half of :func:`classify_key`. It is trustworthy for one reason: it
    is local. It is written only by :func:`_record_run`, after this process's own
    successful push, into the state document the shared agent file-tool fence
    already covers -- so unlike the key prefix, nothing that can write to the
    BUCKET can add to it.

    The fingerprint is what makes the record about an OBJECT rather than a path.
    :func:`restore_download` compares it against the bytes that actually arrive, so
    an archive overwritten at a recorded key stops counting as ours. An entry may
    carry an empty fingerprint (a run recorded before one was available), which
    authenticates as unproven rather than as ours -- unknown is not a pass.

    Includes this process's unpersisted uploads. A push whose state write failed
    still happened, and the archive is still in the bucket; leaving it out would
    make an operator confirm an archive this process uploaded minutes ago.
    """
    with _run_lock:
        return _uploaded_objects_locked(account)


def _uploaded_objects_locked(account: str) -> dict[str, str]:
    entry = _account_view(account)
    stored = entry.get("uploads")
    objects: dict[str, str] = {}
    if isinstance(stored, dict):
        objects = {k: v for k, v in stored.items() if isinstance(k, str) and isinstance(v, str)}
    elif isinstance(stored, list):
        # A document written before the fingerprint existed. Its keys are still
        # this install's own, but nothing pins their bytes, so they carry no
        # fingerprint and authenticate as unproven.
        objects = {k: "" for k in stored if isinstance(k, str)}
    path = _state_key()
    with _unpersisted_lock:
        objects.update(_unpersisted_uploads.get((path, account), {}))
        for (state_path, acct, _kind), record in _unpersisted_runs.items():
            if state_path == path and acct == account:
                held = record.get("key")
                if isinstance(held, str):
                    objects[held] = str(record.get("fingerprint", "") or "")
    return objects


def uploaded_keys(account: str) -> set[str]:
    """Just the keys, for the offline classification the listing does per row."""
    return set(uploaded_objects(account))


def retention_owned_keys(account: str) -> set[str]:
    """The keys a RETENTION sweep may consider: remembered, or version-recorded.

    The union, because a version record is strictly stronger evidence than an
    ``uploads`` entry. Both are written only by this install's own successful push
    into the local state document, so neither can be added by anything that can write
    to the bucket -- but an ``uploads`` entry proves only that this install wrote
    SOMETHING at a key, while a version record names which version it wrote. Reading
    only ``uploads`` therefore discarded the better record: a key trimmed out of the
    panel history still carried a version this install is certain of, and the sweep
    filtered it out of the listing before the ownership test ever ran, so keeping the
    record under :data:`MAX_RECORDED_VERSIONS` would have changed nothing.

    This widens what the sweep may LOOK at, and nothing else. Every key admitted here
    still has to pass :func:`_current_version_is_ours` before it can hold a ``keep``
    slot, and the delete draws only from that set, so no object is erased on weaker
    proof than before -- the recorded id has to be the version a restore would fetch.
    A key with neither record is still skipped, counted by
    :data:`RETENTION_UNRECORDED_STATE_KEY`, and never touched.

    Deliberately NOT used by :func:`classify_key` or the restore path. Their question
    is whether this install vouches for these BYTES, which the fingerprint in
    ``uploads`` answers and a version id does not; widening their answer is a separate
    decision about a separate record.
    """
    return uploaded_keys(account) | set(uploaded_versions(account))


def uploaded_versions(account: str) -> dict[str, str]:
    """Key -> the ``VersionId`` this install recorded writing under it.

    A key is absent when no version was recorded for it: an archive uploaded before
    this was recorded at all, an unversioned bucket, or a ``put-object`` response
    that named none. Retention reads absence as "do not touch", which is the only
    safe reading -- being in ``uploaded_keys`` proves this install wrote A version
    of a key, and only this map says WHICH.

    Merges the in-process records for the same reason :func:`uploaded_objects`
    does: a run whose state write failed is still this install's own upload, and
    its version is the one thing that makes it retireable.
    """
    entry = _account_view(account)
    stored = entry.get("upload_versions")
    versions: dict[str, str] = {}
    if isinstance(stored, dict):
        versions = {
            key: value
            for key, value in stored.items()
            if isinstance(key, str) and isinstance(value, str) and value
        }
    path = _state_key()
    with _unpersisted_lock:
        versions.update(_unpersisted_versions.get((path, account), {}))
        for (state_path, acct, _kind), record in _unpersisted_runs.items():
            if state_path != path or acct != account:
                continue
            held = record.get("key")
            version = record.get("version")
            if isinstance(held, str) and isinstance(version, str) and version:
                versions[held] = version
    return versions


_run_process = uuid.uuid4().hex


_run_sequence = 0


_UNCONDITIONAL_RUN_WRITE = object()


def _record_run(
    account: str,
    kind: str,
    key: str,
    size: int,
    fingerprint: str = "",
    version: str = "",
    *,
    tree: str = "",
    uploaded: bool = True,
    layer_b: bool | None = None,
    conversations_skipped: str = "",
    layer_b_scope: str = "",
    conversations_retained: bool = False,
) -> dict[str, Any]:
    # No ``_run_lock`` here, deliberately. Wrapping this call in it would hold it
    # while :func:`_state_lock` parks on the sidecar file lock, and
    # :func:`last_runs` queues on ``_run_lock`` -- so one account's in-flight upload
    # would stall every account's status read. :func:`_record_run_locked` takes it
    # for the sequence bump alone, which cannot park. See the lock-order note above
    # :func:`_state_lock`.
    recorded = _record_run_locked(
        account,
        kind,
        key,
        size,
        fingerprint,
        version,
        tree=tree,
        uploaded=uploaded,
        layer_b=layer_b,
        conversations_skipped=conversations_skipped,
        layer_b_scope=layer_b_scope,
        conversations_retained=conversations_retained,
    )
    assert recorded is not None
    return recorded


def _record_run_locked(
    account: str,
    kind: str,
    key: str,
    size: int,
    fingerprint: str,
    version: str = "",
    *,
    tree: str = "",
    uploaded: bool = True,
    expected: object = _UNCONDITIONAL_RUN_WRITE,
    layer_b: bool | None = None,
    conversations_skipped: str = "",
    layer_b_scope: str = "",
    conversations_retained: bool = False,
) -> Optional[dict[str, Any]]:
    global _run_sequence
    # Under ``_run_lock``, and under NOTHING else. The callers do not hold it, so this
    # hold is the only thing serialising the increment -- without it the increment
    # would race and two records could share one ``sequence``.
    # That pair, ``(process, sequence)``, is the identity the compare-and-set in
    # ``mutate`` below reads and the one :func:`_run_is_newer` orders by, so a
    # duplicate would let a stale baseline pass a check it should fail.
    #
    # Bumped HERE rather than inside ``mutate`` for two reasons. ``mutate`` does not
    # run at all when the state read or write fails, and that path still hands this
    # ``record`` to :func:`_remember_unpersisted`, so a sequence assigned only inside
    # ``mutate`` would leave every in-memory run sharing one value. And holding
    # ``_run_lock`` across ``_locked_state_update`` is exactly the stall this change
    # removes: this block cannot park, because nothing inside it waits.
    with _run_lock:
        _run_sequence += 1
        sequence = _run_sequence
    record: dict[str, Any] = {
        # Include PID so a fork cannot reuse its parent's sequence namespace.
        "process": f"{_run_process}:{os.getpid()}",
        "sequence": sequence,
        "key": key,
        "bytes": size,
        # Carried on the held record as well, so an upload whose state write failed
        # can still be authenticated from this process's memory.
        "fingerprint": fingerprint,
        # The S3 version this upload created. On a versioned bucket this is the only
        # value that identifies WHICH bytes under the key we wrote, which is what
        # retention needs before it erases anything. Empty when the bucket is
        # unversioned or the response named none, and retention then declines the
        # key rather than guessing.
        "version": version,
        # What the archive CARRIED, as `_tree_fingerprint` computes it -- the value the
        # next run compares to decide whether it has anything new to send. Empty on a
        # record written before this field existed, and an empty value can never match,
        # so an upgraded install re-uploads once rather than skipping on no evidence.
        "tree": tree,
        # Whether this run actually sent bytes. False is a run that found the tree
        # unchanged and skipped: it carries the MATCHED run's key, fingerprint, version
        # and tree, so the baseline survives for the next comparison, and it takes a
        # fresh `at` so `due_for_nightly` does not rebuild the archive on every wake.
        "uploaded": uploaded,
        # Provisional. The authoritative stamp is taken inside `mutate`, under the
        # sidecar lock -- see there. This value survives only on the path where the
        # READ fails, because `mutate` never runs then.
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
    }
    # Which layers the archive actually holds, stated rather than inferred from an
    # absent key -- the same reason the file export sets ``layer_b_skipped``. It is
    # a RECORD for whoever inspects the run, not an input to anything:
    # ``restore_download`` does not read it, and two archives with the same
    # stamped name are otherwise indistinguishable, so without this field nobody
    # can tell whether a given archive can resume a session at full fidelity or
    # only replay its transcript. ``None`` for a kind where the question does not
    # arise (the snapshot), so its records keep their shape.
    #
    # The caller passes what it ARCHIVED, never what it was permitted to archive.
    # A permitted run can still add no Layer B file and no conversation row -- see
    # :func:`run_sessions_backup` -- and this record is written once, so a value
    # taken from the permission would state a fidelity the object does not hold
    # and nothing afterwards would correct it.
    if layer_b is not None:
        record["layer_b"] = bool(layer_b)
    # Only when there IS a reason, so a run that carried everything keeps its
    # record shape. An absent key reads as "nothing was skipped", which is the
    # common case and needs no field; a present one names what was left out, so an
    # operator reading the record can tell a host with no terminal store from one
    # whose store this export declines to reach.
    if conversations_skipped:
        record["conversations_skipped"] = conversations_skipped
    # Recorded as the GRANT's state, not as a skip, so it does not reach the
    # retention predicate above. Present only when Layer B is on and its grant does
    # not cover the conversation export, which is the state an operator needs named
    # to explain an absent `conversations/` root without a skip reason.
    if layer_b_scope:
        record["layer_b_scope"] = layer_b_scope
    if conversations_retained:
        # Set HERE, before `_locked_state_update`, for the same reason `sequence` is
        # assigned out here: the update can abort BEFORE `mutate` ever runs -- the read
        # raising `EACCES`/`EIO`, a scanner holding the file on Windows, non-UTF-8 bytes,
        # or `_state_lock` timing out -- and the `except OSError` then hands
        # `_remember_unpersisted` whatever the record already says. Assigned inside
        # `mutate`, the field was missing from exactly the records that most need it, so
        # both the persisted key and the overlay read False and the sweep could retire the
        # only archive holding the conversations.
        #
        # The ACCOUNT-level key stays inside `mutate`: it is part of the document being
        # written, so it belongs in the same atomic update as the run record.
        record[_RUN_CONVERSATIONS_RETAINED] = True

    def mutate(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        entry = _account_state(state, account)
        runs = entry.setdefault("runs", {})
        if not isinstance(runs, dict):
            # A corrupted non-dict `runs` must not crash AFTER the archive
            # already uploaded (500 + no ledger entry + duplicate on retry).
            runs = entry["runs"] = {}
        if expected is not _UNCONDITIONAL_RUN_WRITE:
            # The slot must still hold the RECORD this baseline was read from, and
            # `(process, sequence)` is what establishes that: `sequence` is bumped on
            # every write above and `process` carries the pid, so no two records this
            # install writes share the pair. That pairing is the one `_run_is_newer`
            # already uses, for the same reason -- a bare sequence counts one process's
            # own writes, so two processes can both sit at the same number.
            # Neither `at` nor `key` can stand in for it, which is why neither is
            # compared here. `datetime.now` resolves to the platform's clock tick, so on
            # a coarse one two writes land on the same microsecond value; and a skip
            # copies the matched run's key, so the slot's key still matches after one.
            # Windows CI produced exactly that pair of collisions and accepted a second
            # skip against a baseline the first had already replaced. Comparing them
            # alongside the pair was measured to add nothing: the pair already refuses
            # every state either could catch, so no mutation of them is observable.
            # A record written before these fields existed carries neither, so the type
            # guards refuse and the caller uploads a full copy. That is deliberate and
            # matches every other proof in this design; a fallback to comparing `at`
            # alone would reopen the collision this closes. The two sequence type guards
            # cover each other on that state, so dropping either one alone is not
            # observable while dropping both accepts an absent sequence as identity --
            # they are required as a pair, not individually.
            current = runs.get(kind)
            if not isinstance(expected, dict) or not isinstance(current, dict):
                return None
            want_process, want_sequence = expected.get("process"), expected.get("sequence")
            if (
                not isinstance(want_process, str)
                or not want_process
                or type(want_sequence) is not int
                or type(current.get("sequence")) is not int
                or current.get("process") != want_process
                or current.get("sequence") != want_sequence
            ):
                return None
        if uploaded:
            # Only a real upload adds to the uploads/versions maps. Today this guard is
            # DEFENSIVE rather than behavioural, and saying so is cheaper than leaving
            # the next reader to discover it: a skip passes the matched run's own key,
            # fingerprint and version, so merging them would rewrite identical values
            # and a mutation that drops the guard changes nothing observable. It is here
            # because that equality is a property of `_record_skip`, not of this
            # function -- a later skip that carried any other key would otherwise write
            # a map entry for an upload that never happened, and `uploaded_versions` is
            # what retention reads before it erases object versions.
            _merge_uploads(entry, {key: fingerprint}, {key: version} if version else None)
        # Keep the observed wall time, even on a coarse or backwards clock.
        # Local sequence, not timestamp precision, orders this process's runs.
        record["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        # This locked write supersedes prior state EXCEPT where this process has
        # already persisted a higher-sequenced run for the same kind. The bump and
        # this write are not one critical section -- ``_run_lock`` is released before
        # the file lock is taken -- so two same-kind runs in this process can reach
        # the file lock in an order that differs from their sequence order, and the
        # loser would otherwise leave the slot holding the older key, tree and
        # ``layer_b``. A manual run overlapping a nightly wake for one account is
        # reachable: the nightly loop calls ``work`` directly and so does not take
        # the Job SDK's ``(kind, account)`` dedupe.
        #
        # This is the same-process half of :func:`_run_is_newer` and deliberately
        # not a call to it: its other half compares wall time, and applying that
        # here would let a peer install with a lagging clock refuse a locked write
        # that really is newer. A FOREIGN record still loses to this write, exactly
        # as an unguarded assignment would have it -- clock and process comparisons
        # stay confined to best-effort recovery.
        #
        # The uploads and versions merge above stays unconditional on purpose: the
        # object IS in the bucket whichever run persists, and ``uploaded_versions``
        # is what retention reads before it erases object versions.
        previous = runs.get(kind)
        superseded = (
            isinstance(previous, dict)
            and previous.get("process") == record["process"]
            and type(previous.get("sequence")) is int
            and previous["sequence"] > sequence
        )
        if conversations_retained:
            # OUTSIDE the `superseded` guard: a superseded run still PUT a
            # conversation-bearing archive in the drive. Which record wins the slot says
            # nothing about what the drive holds, and the sweep erases objects, not
            # records.
            #
            # The record's own field is set BEFORE `_locked_state_update` rather than
            # here, because this function may never run -- see the assignment there.
            _set_conversations_retained(entry)
        if not superseded:
            runs[kind] = record
            # A completed run ends the retry backoff, and it does so HERE -- inside the
            # same mutate, under the same sidecar lock as the record that proves the run
            # -- rather than as a second call beside it. A separate write would leave a
            # window in which the run is recorded and the failure count is not yet
            # cleared, and this state is read by a loop that wakes on its own schedule:
            # that window is exactly long enough for a wake to land in it and withhold
            # the next attempt on the strength of failures that are already over.
            #
            # Both outcomes clear it. `uploaded=False` is a run that found the tree
            # unchanged, which is a successful comparison against an archive that is
            # provably in the drive, not a failure -- and it takes a fresh `at` for the
            # same reason.
            #
            # Reached by the OWNER-triggered path too, and that asymmetry is deliberate:
            # only the unattended loop RECORDS a failure (see
            # :func:`record_nightly_failure`), while any success clears one. An owner who
            # presses the button and watches it work has just demonstrated the fault is
            # gone, so making them wait out a backoff measured for an unattended loop
            # would be withholding the schedule on evidence that has been superseded.
            #
            # INSIDE the supersession guard, sharing the run write's condition for the
            # reason :func:`_merge_pending` states at the other place a run record and
            # this clear travel together: a record this document has already superseded
            # is not evidence of anything, so it must not clear a count a later failure
            # legitimately accumulated. The two callers therefore answer one question
            # the same way. It matters in one window: the winner clears the backoff in
            # its own mutate, so the placements diverge only when a failure is recorded
            # BETWEEN the winner's commit and a loser's, and there the loser is a run
            # whose own record was refused as stale -- too stale to write a key, and so
            # too stale to retire a newer failure.
            #
            # The conversations-retained fact above is deliberately NOT gated, and the
            # difference is what each one is evidence OF. That fact is monotonic and
            # concerns the drive's contents, which no record can undo; this concerns
            # whether a run is current, which is exactly what losing the slot settles.
            _clear_nightly_failure(entry, kind)
        return record

    # The in-lock failure handoff runs INSIDE the sidecar lock (see
    # :func:`_locked_state_update`'s ``on_in_lock_failure``), so a second run-record
    # writer cannot take the lock and persist past this run in the window between a
    # failed state update and its recovery hand-off -- which would strand this upload
    # in memory alone. It covers ANY step after the lock is acquired -- the read, the
    # pending merge, ``mutate``, the write -- because the upload happens BEFORE this
    # function is called, so a completed-upload record exists whichever step then
    # raises, and a read failure leaves a second writer persisting a document this run
    # is absent from just as surely as a failed write does. Only the UNCONDITIONAL
    # write holds such a record: the conditional (``expected``) path re-uploads a full
    # copy on failure and remembers nothing, so it passes no callback and its handler
    # below still just returns.
    #
    # ``handed_off`` records whether that in-lock hand-off actually ran. The ONLY
    # OSError that does not reach it is a failure to ACQUIRE :func:`_state_lock`
    # itself: that never enters the locked block, so the callback cannot fire and the
    # record is held from the handler below. This prevents another upload while the
    # process lives but does not promise immediate disk convergence: a peer may
    # already hold the sidecar lock and commit state without this run. A restart may
    # therefore re-upload the archive, the fallback for an unavailable state
    # lock. This keeps the original "any OSError holds the run" behaviour while
    # moving every in-lock failure's hand-off under the lock.
    handed_off = False

    def _hand_off() -> None:
        nonlocal handed_off
        handed_off = True
        _remember_unpersisted(account, kind, record)

    on_in_lock_failure = _hand_off if expected is _UNCONDITIONAL_RUN_WRITE else None
    try:
        recorded = _locked_state_update(mutate, on_in_lock_failure=on_in_lock_failure)
    except OSError as exc:
        if expected is not _UNCONDITIONAL_RUN_WRITE:
            logger.info(
                "aws-control: %s backup for %s could not recheck its recorded baseline "
                "while the archive was being built, so it is uploading a full copy: %s",
                kind,
                account,
                exc,
            )
            return None
        # Two things are true here and only one of them was handled before.
        #
        # (1) The archive is ALREADY in the bucket, so raising would 500 a
        # request whose upload succeeded and send the operator back to the button
        # for a duplicate -- the same harm the corrupted-`runs` branch above
        # avoids. So this still does not raise.
        #
        # (2) Not raising is not the end of it. `due_for_nightly` decides
        # due-ness from the PERSISTED stamp and `hooks._run_once` calls it on
        # every wake, so a write that never landed leaves the loop permanently
        # due: it re-uploads, unattended and billable, on every wake for as long
        # as this process lives, behind one log line nobody reads. Holding the
        # run in process-local memory -- which `last_runs` merges in -- bounds
        # that to at most one extra upload per gateway restart.
        #
        # Which half failed decides the wording, because both arrive as OSError
        # and they send a reader to different places: `_StateUnreadable` means
        # the existing document could not be read and was deliberately not
        # published over, while a plain OSError means the read was fine and
        # `write_state` failed (ENOSPC, EROFS, EIO). Reporting a full disk as
        # "could not be read" points at permissions instead.
        stage = "could not be read" if isinstance(exc, _StateUnreadable) else "could not be written"
        if not handed_off:
            # Reached ONLY when :func:`_state_lock` could not be ACQUIRED -- the
            # sidecar file lock timed out, or opening its descriptor raised. The
            # locked block never ran, so nothing reached disk and no concurrent
            # writer can have lost a record that was never written; this hand-off
            # has no gap to close and stays out here. Every failure that DID enter
            # the lock (read, merge, mutate, write) already handed the record off
            # INSIDE it (see `on_in_lock_failure`), so it is not repeated.
            _remember_unpersisted(account, kind, record)
        logger.error(
            "aws-control: %s backup for %s %s, but its state file %s, so the run is "
            "not on disk; holding it in memory for this process so the nightly loop does "
            "not re-upload the same archive: %s",
            kind,
            account,
            # The two outcomes reach this branch for different reasons and send a reader
            # somewhere different, so the line must not assert the upload happened: a
            # skipped run sent nothing, and saying it uploaded would have an operator
            # hunting a transfer that never occurred.
            "uploaded" if uploaded else "found the tree unchanged and skipped the upload",
            stage,
            exc,
        )
        return record
    return recorded


def _record_skip(
    account: str,
    kind: str,
    baseline: dict[str, Any],
    tree: str,
    *,
    layer_b: bool | None = None,
    conversations_skipped: str = "",
    layer_b_scope: str = "",
) -> Optional[dict[str, Any]]:
    """Record a run that sent nothing, carrying the baseline it matched.

    The stamp is FRESH, and that is load-bearing rather than cosmetic:
    :func:`due_for_nightly` decides due-ness from ``at``, so a skip that left the old
    stamp in place would read as due on the very next wake and rebuild the archive
    every few minutes for as long as the tree stayed unchanged -- turning a saving into
    a busy loop. Everything else is copied from the matched run so the next comparison
    still has a key it can prove and a version retention can retire.

    ``layer_b``, ``conversations_skipped`` and ``layer_b_scope`` are passed by the
    CALLER from what it
    just measured, not copied from ``baseline``, and that distinction is the point:
    this function REPLACES the run slot outright, so a field it does not forward is
    erased. Coverage facts are exactly the fields an operator reads to decide whether
    an archive holds their conversations, and a skip that dropped them would let the
    first ordinary unchanged run quietly restore an assertion of complete coverage
    over a run that had reported a gap. The caller measures them on every run,
    including this one, so forwarding the CURRENT answer is also more correct than
    preserving the old one.

    Returns ``None`` unless the run slot still holds the very record this baseline was
    read from, identified by its ``(process, sequence)`` pair. The caller uploads
    instead of replacing a concurrent run record with the stale baseline.
    """
    # No ``_run_lock`` here, for the reason :func:`_record_run` states. The
    # compare-and-set this function depends on is enforced inside ``mutate``, under
    # the sidecar file lock, so dropping the outer lock does not weaken it: two
    # concurrent skips still serialize on the file lock and the loser's baseline no
    # longer matches, which is exactly the refusal it is there to produce.
    return _record_run_locked(
        account,
        kind,
        str(baseline.get("key", "")),
        int(baseline.get("bytes", 0) or 0),
        str(baseline.get("fingerprint", "") or ""),
        str(baseline.get("version", "") or ""),
        tree=tree,
        uploaded=False,
        expected=baseline,
        layer_b=layer_b,
        conversations_skipped=conversations_skipped,
        layer_b_scope=layer_b_scope,
    )


def remembered_archives(account: str) -> dict[str, int]:
    """Per kind, how many uploaded archives this install still holds a record of.

    ``{kind: int}`` for every kind in :data:`KIND_SUBPATHS`, always present: a kind
    with no record reads 0. That is the one way it differs from
    :func:`retention_unclaimed` and :func:`retention_unrecorded`, which are stored
    by a sweep, so an absent kind THERE means "never measured" and a caller must
    not read it as zero. This one is derived from the state document on every call
    and has no unmeasured state to distinguish.

    Served BESIDE :func:`last_runs`, and that pairing is the whole point. The
    ledger holds ONE run per kind, so a second nightly overwrites the first
    record while both archives stay in the drive -- a surface reading only the run
    record therefore reports one archive for a prefix holding several, and an
    operator cannot see that anything is accumulating there. This count says the
    single run line is not the list.

    A COUNT OF RECORDS, never an inventory, and it misses in BOTH directions.
    It reads LOW when :data:`MAX_REMEMBERED_UPLOADS` drops the oldest record once
    the map is full, and when another install wrote to the same drive, since only
    this install's own pushes are recorded. It reads HIGH after retention: the
    sweep deletes the object and :func:`_prune_recorded_versions` clears only the
    ``upload_versions`` entry, so the ``uploads`` key this counts outlives the
    archive it names -- with ``keep=3`` after ten nightlies this answers 10 while
    the list the row's own button opens shows 3. Only a listing can say what the
    drive really holds, and that call is the OPT-IN half of the backup status read
    for what it costs. The row is therefore worded as a record count and hands the
    reader to that listing rather than standing in for it.

    Counted from :func:`uploaded_objects`, so it carries this process's
    unpersisted pushes for the reason that function does: the archive is in the
    bucket whether or not the state write landed.

    A key is attributed by its FIRST segment, which is the kind's subpath -- the
    segment, not a string prefix, so one subpath that starts with another's text
    cannot absorb its keys. A legacy key from before the install-id namespace
    still carries that segment, so its archive counts for the kind that wrote it
    rather than being dropped. A key under no known subpath is counted for no
    kind, because there is no kind to attribute it to.

    Local and free -- no AWS call -- so it rides on the unpolled half of the
    status read, like :func:`nightly_failures`.
    """
    counts = dict.fromkeys(KIND_SUBPATHS, 0)
    for key in uploaded_objects(account):
        kind = _KIND_BY_SUBPATH.get(_key_segments(key)[0])
        if kind is not None:
            counts[kind] += 1
    return counts


def last_runs(account: str) -> dict[str, Any]:
    """The last run per kind, including runs this process could not persist.

    The merge is not cosmetic. A run whose state write failed really did upload,
    and :func:`due_for_nightly` reads its answer from here -- so without the
    overlay the nightly loop treats the account as never backed up and uploads
    again on every wake. See :data:`_unpersisted_runs`.
    """
    with _run_lock:
        runs = _account_view(account).get("runs", {})
        runs = dict(runs) if isinstance(runs, dict) else {}
        return _merge_unpersisted(account, runs)
