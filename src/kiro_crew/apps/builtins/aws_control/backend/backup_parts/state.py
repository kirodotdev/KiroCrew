"""Durable backup state: ``backup.json``, its locks, and the recovery overlay.

This is the one state document every other part reads and writes. The module owns
the sidecar file lock and the in-process locks around it, and the backup engine's
single lock acquisition order (the LOCK ORDER note above :func:`_state_lock`). It
also owns the in-memory overlay that holds a completed upload's run record when its
state write fails.

The overlay lives here and not beside the run recorder in the ledger. The reason is
that :func:`_locked_state_update` merges it into EVERY successful write, so a held
record reaches disk through the next writer of any field, not only the next run
record. For the same reason this module owns the two facts that travel with a run
record into the document: the monotonic conversations-retained fact, and the end of
a nightly failure streak. Both entry points must apply them identically: a fresh
record written by the ledger, and a recovered one merged by :func:`_merge_pending`.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock, open_lock_file

logger = logging.getLogger(_FACADE_MODULE)


APP_NAME = "aws-control"


#: Wall clock for one backup push to S3, passed by both runners into
#: :func:`storage.put_file` rather than relying on its 600s default. The
#: nightly snapshot push runs unattended, and an owner-triggered sessions
#: archive may legitimately need the full hour -- the size ceiling is
#: ``storage._MAX_PINNED_TRANSFER_BYTES`` (5 GiB), which at 3600s still
#: requires a ~12 Mbit/s uplink, so a slower push fails at the bound rather
#: than holding the owner-billed transfer open indefinitely. Tests assert the
#: constant reaches the uploader on both paths, so it cannot go unread.
_PUSH_TIMEOUT_SECS = 3600


#: Wall clock allowed for the authorization that runs inside the state lock: the
#: STS identity check is bounded by ``deploy.engine._checked``'s own 30s default,
#: and this leaves the same again for the local consent and app-enabled reads
#: that follow it.
_AUTHORIZE_TIMEOUT_SECS = 60


#: How long a contender waits for the state file's sidecar lock. The Layer B
#: upload gate holds it across that authorization and the archive PUT, so the
#: wait must outlast their sum. ``platform_compat``'s default ceiling is
#: ``_LOCK_TIMEOUT_SECS`` (300s), sized for a sub-second read plus an atomic
#: rename, and ``file_lock`` requires any caller that can hold the lock longer to
#: override it -- otherwise the ceiling refuses a contender while this holder is
#: still working rather than because it is stuck. That refusal is not cosmetic:
#: it arrives as the ``OSError`` :func:`_record_run` absorbs, which keeps the run
#: in memory only, so a short-lived process that exits first loses it and leaves
#: the nightly loop due and re-uploading. Derived from the bounds it must cover
#: so the two cannot drift apart.
_STATE_LOCK_TIMEOUT_SECS = float(_PUSH_TIMEOUT_SECS + _AUTHORIZE_TIMEOUT_SECS)


#: Backup state, holding the ``nightly`` bit that AUTHORIZES the unattended
#: upload loop. ``security._CREW_SECRET_LEAVES`` carries the matching
#: ``apps/aws-control/data`` entry, which puts this file -- and the atomic-write
#: temporary it is renamed from, and every sibling state file -- behind the
#: shared agent file-tool floor. The owner toggles nightly through the
#: owner-gated endpoint, and an agent cannot flip it by writing any path in
#: there. A test pins the two together, because moving this file out of that
#: directory would silently un-protect it.
STATE_DIR_LEAF = f"apps/{APP_NAME}/data"


#: Per-account fact: at least one sessions archive this install uploaded and has NOT
#: retired carries a ``conversations/`` root. It is the ONE thing the retention sweep
#: needs in order to tell "this run carries no conversations and none were ever
#: retained" -- prune, nothing is at risk -- from "this run carries none while an older
#: retained archive does" -- do not prune, that archive is the only copy.
#:
#: Deliberately ONE BOOLEAN read through a predicate, not a list of archives or of
#: qualifying reasons. A second list that has to stay in sync with the archives is a
#: place to forget one, and the cost of forgetting here is a permanent delete.
#:
#: It only ever goes True, and that is correct rather than lazy: while it is True the
#: sweep is declined, so the archive it refers to is never retired, so the fact stays
#: true. A run that DOES carry conversations prunes normally -- the newest archive holds
#: them, so retiring older ones loses nothing -- which is what lets retention resume.
SESSIONS_CONVERSATIONS_RETAINED_KEY = "sessionsConversationsRetained"


#: The same fact carried ON the run record. It has to travel there as well, because the
#: ACCOUNT-level key does not survive a failed state write: ``_remember_unpersisted``
#: holds only the run record, and ``_merge_pending`` restores records, uploads and
#: versions -- no account-level key. Held only on the account, the fact would vanish on an
#: ``ENOSPC`` or read-only-filesystem write while the archive it protects stayed in the
#: drive, and the only thing that could set it again is another conversation-bearing run,
#: which a narrowed scope makes impossible. So the record carries it and
#: :func:`_merge_pending` puts it back.
_RUN_CONVERSATIONS_RETAINED = "conversations_retained"


def _set_conversations_retained(entry: dict[str, Any]) -> None:
    """Set the account-level conversations-retained fact. MONOTONIC by contract.

    The ONE writer, so the invariant lives in one place: this fact is only ever SET and
    never cleared, by this function or any other. A conversation-bearing archive that
    reached the drive is not undone by a later run, by a superseded record, or by a
    recovery merge -- and while the fact holds the retention sweep is declined, so the
    archive it refers to is never retired and the fact stays accurate rather than stale.

    Anything that lowered it would have to prove the archive is gone, and the only code
    that removes archives is the sweep this fact declines.
    """
    entry[SESSIONS_CONVERSATIONS_RETAINED_KEY] = True


def _state_path() -> Path:
    return app_data_dir(APP_NAME) / "backup.json"


def _read_state_checked() -> tuple[dict[str, Any], bool]:
    """``(state, readable)`` from ONE read of the state file.

    ``readable`` is False only when the file EXISTS and could not be read or
    parsed as an object. An ABSENT file is readable: there is nothing configured
    to misread, and every default in the backup engine is written for that case.

    The distinction exists for one caller. :func:`read_state` folds absent,
    unreadable and corrupt into ``{}`` because every other reader wants a
    default; the retention sweep must not, because there a default silently
    replaces a configured value and the difference is measured in permanently
    erased bytes. See :func:`_retention_keep_for_sweep`.
    """
    try:
        raw = _state_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`, so an OSError-only
        # catch lets a state file of non-UTF-8 bytes escape as an exception -- exactly
        # the "exists but could not be read" case this function promises to answer
        # `({}, False)` for. It matters more here than anywhere else in the engine: the
        # sweep resolves its keep count through this before entering its own
        # best-effort handler, and its caller's comment promises retention cannot fail
        # the run, so an escaping decode error would report a backup already off-host
        # as failed AND skip the audited unreadable branch.
        #
        # Deliberately not the wider `ValueError`: a surprising one is a bug that
        # should be loud rather than folded into "unreadable".
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    return data, True


def read_state() -> dict[str, Any]:
    return _read_state_checked()[0]


def write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=1))


class _StateUnreadable(OSError):
    """The state document exists but could not be read.

    A distinct type so :func:`_record_run` can say WHICH half of its
    read-modify-write failed. Both halves reach it as an ``OSError`` and the two
    are not interchangeable to whoever reads the log: "could not be read" sends
    that reader to check permissions and file handles, which is the wrong place
    to look when the truth is that the read was fine and ``write_state`` hit a
    full disk.

    It stays an ``OSError`` SUBCLASS deliberately. The other caller of
    :func:`_locked_state_update` -- :func:`set_nightly`, which lets the error
    reach its handler -- keeps behaving exactly as before this split, so nothing
    outside the backup engine has to learn the new type to stay correct.
    """


def _read_state_for_update() -> dict[str, Any]:
    """The state document a read-modify-write is allowed to publish over.

    :func:`read_state` is a DISPLAY read: every failure collapses to ``{}`` so a
    render never crashes on a state file it could not load. That reading is
    wrong as the BASE of a mutation, because :func:`_locked_state_update` writes
    the whole document back -- an empty base there does not mean "no fields to
    carry forward", it means "replace every account's nightly toggle and run
    history with this one field". The sidecar lock does not help: it serializes
    writers, and the loss happens inside it.

    Only the missing file is a failure where ``{}`` is the truth (nothing has
    been written yet). An unreadable one -- a transient EACCES/EIO, a scanner
    holding the handle on Windows -- is state we still have, so the error is
    allowed to propagate and the mutation is abandoned rather than published
    over state nobody read.

    Corruption keeps its existing repair-on-write behaviour, which is a
    deliberate decision documented on :func:`_account_state`: a document that
    parsed to nothing usable carries nothing to lose. That covers a file which
    DECODED and then failed to parse. Bytes that are not UTF-8 never reached the
    parser, so they are the unreadable kind, not the corrupt kind, and the
    mutation is abandoned rather than published over them.
    """
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except UnicodeDecodeError as exc:
        # Repair-on-write below is justified for a document that PARSED to nothing
        # usable. Bytes that are not UTF-8 never reached the parser, so that reasoning
        # does not cover them: the file is still state we have, and publishing over it
        # would replace every account's toggles, retention count and run history --
        # including the `upload_versions` records the sweep's ownership test depends on
        # -- on the strength of a document nobody read. It is caught explicitly because
        # it is a `ValueError`, so the `OSError` clause below cannot see it.
        raise _StateUnreadable(
            errno.EILSEQ, "state file is not valid UTF-8", str(_state_path())
        ) from exc
    except OSError as exc:
        raise _StateUnreadable(
            exc.errno, exc.strerror or "state file could not be read", exc.filename
        ) from exc
    return data if isinstance(data, dict) else {}


# -- LOCK ORDER -----------------------------------------------------------------
#
# One order, and every path in the backup engine obeys it:
#
#     _RETENTION_GATE -> state sidecar FILE lock -> _run_lock -> leaf locks
#                                                               (_unpersisted_lock,
#                                                                _fallback_lock)
#
# The hop that matters is the middle one: NOTHING may hold ``_run_lock`` while it
# waits for the sidecar file lock. ``_run_lock`` also serializes :func:`last_runs`,
# which the dashboard's backup-status read goes through, and the file lock is held
# across a PUT allowed ``_PUSH_TIMEOUT_SECS`` -- so a writer parked on the file lock
# while holding ``_run_lock`` puts every account's status read behind one account's
# upload, across accounts. Omitting ``_run_lock`` from the upload gate alone did not
# fix that: the stall arrived through the contending WRITER, not through the upload.
#
# Acquiring the two in the other order anywhere would close a cycle against this
# one, so a new holder of both belongs here rather than beside its own call site.
#
# Every site, for the reader who would rather check than take this on trust:
#   :func:`_state_lock`                     file lock, then ``_run_lock``
#   :func:`_upload_lock`                    the file lock alone
#   :func:`_delete_under_the_retention_gate`  ``_RETENTION_GATE``, then the file lock
#   :func:`_locked_state_update`            :func:`_state_lock`, then
#                                           ``_unpersisted_lock`` (via
#                                           :func:`_merge_pending` and the release
#                                           block on success, via the in-lock
#                                           failure handoff BEFORE it releases on
#                                           failure)
#   :func:`_record_run_locked`              ``_run_lock`` alone, for the sequence
#                                           bump, which cannot park
#   :func:`_record_run`, :func:`_record_skip`  nothing; they reach the file lock
#                                           through :func:`_state_lock`
#   :func:`last_runs`, :func:`uploaded_objects`  ``_run_lock`` alone, never the file
#                                           lock
#   ``_unpersisted_lock``, ``_fallback_lock``  leaves; they acquire nothing under
#                                           themselves
@contextlib.contextmanager
def _state_lock():
    """Hold the state file's sidecar lock.

    Extracted so a reader that must not be overtaken by a writer can hold the
    SAME lock the writer takes, rather than a second lock over the same
    invariant -- two locks guarding one document drift, and whichever is checked
    first wins. :func:`_locked_state_update` is its holder; the Layer B upload
    gate in :func:`run_sessions_backup` takes only this lock's FILE half via
    :func:`_upload_lock`, deliberately without ``_run_lock``, so an hour-long PUT
    does not stall the ``_run_lock`` status read.

    Takes the FILE lock first and ``_run_lock`` second, which is the backup engine's one
    acquisition order -- see the lock-order note above. The reverse is what made a
    contending writer park on the file lock while still holding ``_run_lock``, so
    :func:`last_runs` queued behind that writer for the length of an upload even
    though the upload gate itself held no ``_run_lock``.

    A THIRD site takes the same sidecar file lock without coming through here:
    :func:`_delete_under_the_retention_gate` composes it with
    :data:`_RETENTION_GATE` rather than ``_run_lock``, deliberately, so that a
    purge does not stall the status read. It keeps ``file_lock``'s default
    ceiling, so a sweep contending with an upload that holds this lock is
    REFUSED rather than parked. That is the direction to fail in: the sweep
    deletes nothing, audits as failed and the next run retries, whereas raising
    its ceiling would hold ``_RETENTION_GATE`` for the length of an upload and
    park :func:`set_retention_keep` -- an operator's own write -- behind it.

    Reentrant per thread only as far as ``_run_lock`` is: the file lock is taken
    on a fresh descriptor each time, so a nested acquisition inside one thread
    would deadlock on it. Neither holder nests.

    The ceiling is ``_STATE_LOCK_TIMEOUT_SECS`` rather than ``file_lock``'s
    default, because the upload gate holds this lock across a PUT allowed an
    hour. A ceiling shorter than the holder's real work would refuse a contender
    that is merely waiting, and that refusal reaches :func:`_record_run` as an
    ``OSError`` which keeps a completed upload's record in memory alone.
    """
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True, timeout=_STATE_LOCK_TIMEOUT_SECS):
            # ``_run_lock`` AFTER the file lock, never before -- see the lock-order
            # note above this function. Parking on the file lock while holding
            # ``_run_lock`` is what put the status read behind an in-flight upload.
            with _run_lock:
                yield


@contextlib.contextmanager
def _upload_lock():
    """Hold ONLY the state file's sidecar lock across the Layer B upload gate.

    Taken by that gate on every path but one: the attended owner's WITHHELD run.
    The ordering below is ordering against the SETTERS, so it is worth an exclusive
    hold wherever a permission read inside the gate can be overtaken by one. The
    permitted path has its Layer B recheck. A SCHEDULED run has the unattended
    grant, which `_authorize_upload` re-reads for scheduled callers alone and
    `set_nightly_sessions` writes under this very lock -- and it has that read
    whether or not Layer B is permitted, because the crew display half rides on
    every run. Only an owner-initiated withheld run has neither: its recheck
    short-circuits, both scheduled-only re-reads are skipped, and it takes no lock
    at all, which leaves its authorization adjacent to its upload. See the gate in
    :func:`run_sessions_backup`.

    Same sidecar file lock as :func:`_state_lock`, and deliberately NOT
    ``_run_lock`` -- the exact shape :func:`_delete_under_the_retention_gate`
    composes for the same reason. ``_run_lock`` also serializes
    :func:`last_runs`, and the dashboard's backup-status read goes through it,
    so holding it across a PUT allowed ``_PUSH_TIMEOUT_SECS`` would block every
    account's status surface for the length of one account's upload. The status
    read must not be overtaken by a writer, but it is not this upload's writer:
    the invariant the upload owns is that a consent withdrawal cannot interleave
    between its recheck and the PUT, and that is a cross-process AND cross-thread
    ordering against the SETTER, not against the reader.

    The setters (:func:`set_sessions_layer_b` and :func:`set_nightly_sessions`,
    each -> :func:`_locked_state_update` -> :func:`_state_lock`) take this same
    sidecar file lock EXCLUSIVELY. The file
    lock is per-descriptor, so an exclusive hold here blocks the setter's
    exclusive hold and vice versa, in this process and in a second install
    writing the same state. That is what makes a revocation land wholly before
    this block or wholly after it. The file lock alone carries that ordering, so
    omitting ``_run_lock`` here costs the guarantee nothing.

    Omitting it here is necessary and not sufficient on its own. A contending
    writer reaches this same file lock through :func:`_state_lock`, so a
    :func:`_state_lock` that took ``_run_lock`` BEFORE parking on the file lock
    would leave that writer holding ``_run_lock`` for this upload's whole duration,
    and :func:`last_runs` would queue behind the WRITER rather than behind this
    block. Two of the feature's own paths contend that way: a mid-upload revocation
    (which must contend on the file lock for the ordering above to mean anything)
    and any second account's :func:`_record_run` finishing. What keeps the reader
    free is the engine's single acquisition order -- :func:`_state_lock` takes the
    file lock first, and :func:`_record_run` does not wrap it in ``_run_lock``. See
    the lock-order note above :func:`_state_lock`.

    The ceiling is ``_STATE_LOCK_TIMEOUT_SECS`` for the reason :func:`_state_lock`
    documents: this gate holds the lock across the authorization and a PUT
    allowed an hour, and ``file_lock``'s default would refuse a contender that is
    merely waiting, a refusal :func:`_record_run` absorbs by keeping a completed
    upload's record in memory alone.

    Reentrant only as far as the file lock is -- taken on a fresh descriptor each
    time, so a nested acquisition inside one thread would deadlock. The upload
    gate does not nest, and nothing it reaches (:func:`_authorize_upload`,
    :func:`sessions_layer_b_enabled`, :func:`_refuse_upload`) re-enters it;
    :func:`_record_run` runs after the block has released.
    """
    lock_path = _state_path().with_suffix(".lock")
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True, timeout=_STATE_LOCK_TIMEOUT_SECS):
            yield


def _locked_state_update(mutate, on_in_lock_failure=None) -> Any:
    """Read-modify-write the state file under the sidecar lock.

    Two backup kinds can finish concurrently (a manual run racing the
    nightly loop); an unlocked read-modify-write would let the later atomic
    write silently discard the earlier run record. Same sidecar-lock shape
    as the share ledger.

    Raises ``OSError`` when the existing state could not be read; see
    :func:`_read_state_for_update` for why that is not collapsed to an empty
    document here.

    ``on_in_lock_failure`` -- when ANY step taken after the sidecar lock is
    acquired raises ``OSError`` (the read, the pending merge, ``mutate``, or the
    write) -- is called while the lock is STILL HELD, then the error propagates.
    This is the in-lock failure handoff, and it MUST run inside the lock:
    :func:`_record_run_locked` holds a completed upload's record in process memory
    when a state update it drove fails, and if that hand-off happened after this
    block released, a second run-record writer could take the sidecar lock in the
    gap, :func:`_merge_pending` in nothing (the first run is not held yet), and
    persist only its own record -- stranding the first upload in memory alone,
    forgotten on restart, reopening the unattended re-upload.

    Every one of these failures leaves that gap, not the write alone. The upload
    happens BEFORE :func:`_record_run` is called, so the completed-upload record
    exists whichever step then fails -- including a read failure, where the
    document a second writer would persist is one this run is absent from just as
    surely as after a failed write. Handing off inside the lock closes that gap at
    its source without a second lock: the record is held before any other writer
    can read the state it is missing from. It runs on any such failure and never on
    success. The callback takes only ``_unpersisted_lock``, a leaf below the two
    locks this block already holds, so the engine's one acquisition order stands.

    A failure to ACQUIRE :func:`_state_lock` never enters this block, so it cannot
    fire the callback. The caller's handler still holds the record in process
    memory, preventing another upload while this process lives. That path cannot
    promise immediate disk convergence: a peer may already hold the sidecar lock
    and commit state that does not include this run, and no callback can execute
    under a lock this caller never acquired. A restart may therefore re-upload
    that archive, which is the fallback for an unavailable state lock.
    """
    with _state_lock():
        try:
            state = _read_state_for_update()
            pending, uploads = _merge_pending(state)
            result = mutate(state)
            write_state(state)
        except OSError:
            # Hand the failed run off to recovery BEFORE releasing the lock, so no
            # concurrent writer can read past this point without seeing it. This
            # covers EVERY step after the lock was acquired -- read, merge, mutate,
            # write -- because the upload precedes `_record_run`, so a completed
            # upload's record exists no matter which one raised. See the
            # ``on_in_lock_failure`` note above.
            if on_in_lock_failure is not None:
                on_in_lock_failure()
            raise
        for account, kind, record in pending:
            _forget_unpersisted(account, kind, record)
        with _unpersisted_lock:
            for key, fingerprints in uploads.items():
                held = _unpersisted_uploads.get(key, {})
                held_versions = _unpersisted_versions.get(key, {})
                for name, fingerprint in fingerprints.items():
                    if held.get(name) == fingerprint:
                        held.pop(name, None)
                        # The version is cleared with the fingerprint it arrived
                        # with, never on its own: the persisted state now carries
                        # both, so keeping either would be a second copy that can
                        # go stale.
                        held_versions.pop(name, None)
                if not held:
                    _unpersisted_uploads.pop(key, None)
                if not held_versions:
                    _unpersisted_versions.pop(key, None)
            # Versions are ALSO released on their own contract, because the two
            # maps are bounded differently and the loop above can only reach a
            # version whose fingerprint counterpart is still held.
            _release_persisted_versions(state)
    return result


def _account_state(state: dict[str, Any], account: str) -> dict[str, Any]:
    """The per-account slice of the state file.

    Keyed by account, not global: two connected accounts each own their
    nightly toggle and run records, so switching the default cannot make one
    console report the other's backups. A corrupted file where either level
    decoded to a non-dict is REPLACED so mutations repair rather than crash
    (the read path treats the same corruption as empty).
    """
    accounts = state.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = state["accounts"] = {}
    entry = accounts.setdefault(account, {})
    if not isinstance(entry, dict):
        entry = accounts[account] = {}
    return entry


#: How many of this install's own uploaded keys are remembered per account. The
#: panel lists 20 per kind, so this covers a long history of both kinds while
#: keeping the state document bounded; the oldest entry is dropped when a new
#: upload arrives. Falling off the end is not a correctness problem -- an archive
#: whose record has aged out reads as :data:`ORIGIN_UNVERIFIED` and asks, which is
#: the safe direction to fail in.
#:
#: It bounds ``uploads`` ONLY. ``upload_versions`` is bounded separately, because
#: the two maps answer questions with different lifetimes; see
#: :data:`MAX_RECORDED_VERSIONS`.
MAX_REMEMBERED_UPLOADS = 200


#: The BACKSTOP on ``upload_versions``, and deliberately not a horizon.
#:
#: A version record is the only thing that lets a sweep retire an archive, so while
#: this map was trimmed to the keys ``uploads`` still held, a count chosen for a
#: PANEL decided what retention could ever collect. An install pushing nightly with
#: retention off -- the shipped default -- dropped its oldest version record at push
#: 201, and a ``keep`` count enabled later could not reach anything older: those
#: archives held no recorded version, the ownership test refused them, and their
#: bytes were billed permanently. The bound kept MINTING that floor.
#:
#: So the record's lifetime is now the ARCHIVE's, not the panel's:
#: :func:`_prune_recorded_versions` drops a record when a listing the sweep trusted
#: proves the object is gone, and this number is only the ceiling that stops a
#: pathological document growing without limit. A healthy install never reaches it,
#: because retention itself bounds the pile once enabled and the prune tracks it.
#:
#: 5000 keys, which at two nightly kinds is about six and a half years, and which
#: sits under what the sweep could act on anyway: ``storage.list_object_versions``
#: refuses a prefix holding more than ten full delete batches of version rows, so a
#: larger record cap would name archives retention can never enumerate. At roughly
#: 130 bytes per entry the ceiling is a state document under a megabyte.
#:
#: Overflow drops the OLDEST records, which is the only safe direction: the newest
#: archives are the ones a ``keep`` count protects, and a dropped record never
#: deletes anything -- it only returns that archive to the unreclaimable floor
#: :func:`retention_unrecorded` reports.
MAX_RECORDED_VERSIONS = 5000


#: Where a FAILED unattended attempt is recorded in ``backup.json``: per account, then
#: per kind, ``{"at": iso8601, "since": iso8601, "consecutive": int, "error": str}``.
#:
#: A separate key from ``runs`` on purpose, and the separation is the whole design.
#: ``runs`` is a record of bytes that reached the drive: :func:`uploaded_versions`,
#: :func:`_unchanged_baseline` and the retention sweep all read it as proof an archive
#: exists. A failed attempt proves the opposite, so filing it there would hand every one
#: of those readers a baseline to compare against and a version to retire for an upload
#: that never happened. Here it is read by exactly one consumer -- the due-check -- and
#: by the status projection that reports it.
NIGHTLY_FAILURE_STATE_KEY = "nightly_failures"


#: Runs whose archive reached the bucket but whose state write did not land,
#: held for the life of THIS process. :func:`last_runs` merges them in, and that
#: is the whole point: it is what stops :func:`due_for_nightly` re-firing the
#: unattended loop on a stamp that was never persisted. See :func:`_record_run`.
#:
#: Keyed by the state FILE as well as the account and kind. An entry is a claim
#: about one state document -- "this file is missing a run it should have" -- so it
#: must never answer for a different one. Production resolves a single fixed path
#: (``app_data_dir`` is ``app_dir(name) / "data"``, and nothing repoints it), so
#: this is not guarding a live scenario; what it buys is that the tests are
#: hermetic by construction instead of through a reset hook every future test has
#: to remember to call. :func:`_state_key` resolves the element without raising.
#:
#: Bounded by the accounts the owner has actually connected times the two backup
#: kinds, and an entry is dropped as soon as one write for that key succeeds.
_unpersisted_runs: dict[tuple[str, str, str], dict[str, Any]] = {}


_unpersisted_uploads: dict[tuple[str, str], dict[str, str]] = {}


#: Key -> the ``VersionId`` of a held upload, the version half of the map above.
#: Retention can only retire a key whose version it knows, so a held upload that
#: recovers its fingerprint and loses its version recovers into an archive nothing
#: can ever reclaim. Trimmed to the keys ``_unpersisted_uploads`` holds rather than
#: to its own count, so exactly ONE bound governs both and they cannot drift.
_unpersisted_versions: dict[tuple[str, str], dict[str, str]] = {}


_unpersisted_lock = threading.Lock()


# Serialize record creation through recovery/acknowledgement in this process.
# The sidecar still serializes disk updates across processes; it cannot order
# successful uploads whose state was inaccessible to another process.
#
# ORDER: this is the SECOND lock in the engine's one acquisition order, after the
# state sidecar file lock -- see the lock-order note above :func:`_state_lock`. A
# new holder of both takes the file lock first. Never hold this one while waiting
# for the file lock: it also serializes :func:`last_runs`, so a parked writer would
# put every account's status read behind one account's upload.
_run_lock = threading.RLock()


def _run_is_newer(candidate: dict[str, Any], previous: Any) -> bool:
    """Local sequence orders one process; other/legacy records use wall time."""
    if not isinstance(previous, dict):
        return True
    process = candidate.get("process")
    sequence, old_sequence = candidate.get("sequence"), previous.get("sequence")
    if (
        isinstance(process, str)
        and process
        and process == previous.get("process")
        and type(sequence) is int
        and type(old_sequence) is int
    ):
        return sequence > old_sequence
    old_at = previous.get("at")
    return not isinstance(old_at, str) or old_at < str(candidate.get("at", ""))


def _merge_uploads(
    entry: dict[str, Any],
    additions: dict[str, str],
    versions: Optional[dict[str, str]] = None,
) -> None:
    uploads = entry.setdefault("uploads", {})
    if not isinstance(uploads, dict):
        uploads = entry["uploads"] = {}
    uploads.update(additions)
    for stale in list(uploads)[: max(0, len(uploads) - MAX_REMEMBERED_UPLOADS)]:
        uploads.pop(stale, None)
    # The version map lives beside `uploads` rather than inside its values, because
    # `uploaded_objects` filters that map to STRING values and would silently drop a
    # key whose value became a dict -- taking `classify_key` and the restore
    # ownership check down with it.
    #
    # The two maps are bounded SEPARATELY, and the drift between them is the point
    # rather than a hazard to design out. `uploads` is panel history, so a count
    # chosen for a 20-per-kind listing is the right bound for it. A version record is
    # the only thing that lets a sweep retire an archive, so trimming this map to
    # that count let a panel number decide what retention could ever collect: an
    # install pushing nightly with retention off dropped its oldest version record at
    # push 201, and a keep count enabled later could not reach anything behind it.
    # The record now lives as long as the ARCHIVE does -- dropped by
    # `_prune_recorded_versions` when a listing the sweep trusted proves the object is
    # gone -- and `MAX_RECORDED_VERSIONS` is only the ceiling under which that stays
    # bounded. A record whose key has left `uploads` is therefore KEPT: it is exactly
    # the record that makes an older archive retireable, and `retention_owned_keys` is
    # what stops it being dead weight.
    recorded = entry.setdefault("upload_versions", {})
    if not isinstance(recorded, dict):
        recorded = entry["upload_versions"] = {}
    recorded.update(versions or {})
    # The overflow is COUNTED before anything is dropped, and said out loud with its
    # count. A silently truncated tail reads exactly like a population that never held
    # those records, and what is lost here is not display history: it is the proof that
    # makes an archive retireable, so the archives behind the dropped records stop being
    # collectable and nothing else in the app reports it. With retention off the sweep
    # returns before any listing, so no later measurement covers them either.
    #
    # The retained VALUE needs no length bound of its own: it is a version id S3 issues
    # under S3's own limit, and the key is minted by this app rather than accepted from a
    # caller. Truncating either would be worse than unbounded -- a shortened version id is
    # not the version, so it would silently fail the ownership test it exists to pass.
    overflow = max(0, len(recorded) - MAX_RECORDED_VERSIONS)
    if overflow:
        logger.warning(
            "aws-control retention: dropping %d oldest version record(s) past "
            "MAX_RECORDED_VERSIONS=%d; the archives behind them can no longer be "
            "proven this install's and retention will not retire them",
            overflow,
            MAX_RECORDED_VERSIONS,
        )
    for stale in list(recorded)[:overflow]:
        recorded.pop(stale, None)


def _merge_pending(state: dict[str, Any]) -> tuple[list, dict]:
    """Carry bounded recovery metadata into the next successful state update."""
    path = _state_key()
    with _unpersisted_lock:
        pending = [
            (account, kind, record)
            for (state_path, account, kind), record in _unpersisted_runs.items()
            if state_path == path
        ]
        uploads = {
            key: dict(fingerprints)
            for key, fingerprints in _unpersisted_uploads.items()
            if key[0] == path
        }
        versions = {key: dict(ids) for key, ids in _unpersisted_versions.items() if key[0] == path}
    for account, kind, record in pending:
        entry = _account_state(state, account)
        # Restored BEFORE the `_run_is_newer` gate and deliberately outside it, because
        # this fact is MONOTONIC while a run record is not. A record this document has
        # already superseded is still evidence that a conversation-bearing archive
        # reached the drive, and that archive does not un-exist because a later run's
        # record won the slot. Gating it would let the recovery path silently drop the
        # one fact that stops a later sweep erasing the only copy -- which is exactly
        # the hole a state write failing with ENOSPC opens.
        if record.get(_RUN_CONVERSATIONS_RETAINED) is True:
            _set_conversations_retained(entry)
        runs = entry.setdefault("runs", {})
        if not isinstance(runs, dict):
            runs = entry["runs"] = {}
        if _run_is_newer(record, runs.get(kind)):
            runs[kind] = record
            # The SECOND place a run record enters this document, and so the second
            # place the failure count has to go. `_record_run_locked` clears it beside
            # its own write, but a run whose state write raised is held in memory and
            # arrives HERE instead -- carrying the run and, before this line, not the
            # clear. The stale count then outlived the success that should have ended
            # it: in-process the overlay-merged run kept the account not-due, so it
            # only bit after a restart, and then withheld one nightly for up to the
            # ceiling on an account that had already backed up.
            #
            # Gated on `_run_is_newer` for the same reason the run write is: a record
            # this document already superseded is not evidence of anything, so it must
            # not clear a count a later failure legitimately accumulated.
            _clear_nightly_failure(entry, kind)
    for (_, account), fingerprints in uploads.items():
        # The versions travel with the fingerprints. Recovering a key WITHOUT its
        # version persists an upload retention can never retire, and the key carries
        # a timestamp and entropy so it is never re-uploaded to self-correct.
        _merge_uploads(
            _account_state(state, account),
            fingerprints,
            versions.get((path, account), {}),
        )
    return pending, uploads


def _release_persisted_versions(state: dict[str, Any]) -> None:
    """Drop held version records the written state already carries, byte-equal.

    Call with :data:`_unpersisted_lock` held, after ``write_state``. ``state`` must
    be the document that was just written, because equality against it is the only
    proof that the record is durable.

    The fingerprint-paired release in :func:`_locked_state_update` is not enough on
    its own. ``_unpersisted_uploads`` is bounded to the panel history while this map
    is bounded far above it, so a held version whose fingerprint counterpart was
    already evicted can never match that condition again -- and :func:`_merge_pending`
    copies this map WHOLE into every later state update, so such an entry would be
    written back on every update for the life of the process. That resurrects exactly
    the records :func:`_prune_recorded_versions` deleted on a trusted listing's proof,
    which would make the sweep's deletion decision silently temporary.

    Release is keyed on byte equality with the persisted id, never on the key's
    presence: a DIFFERENT id under the same key means this held record is the one
    the state does not have, which is what the overlay is for. The read is
    deliberately defensive rather than :func:`_account_state`, which would mutate the
    document after it was written.
    """
    path = _state_key()
    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        return
    for map_key in list(_unpersisted_versions):
        if map_key[0] != path:
            continue
        entry = accounts.get(map_key[1])
        persisted = entry.get("upload_versions") if isinstance(entry, dict) else None
        if not isinstance(persisted, dict):
            continue
        held = _unpersisted_versions.get(map_key, {})
        for name in [n for n, version in held.items() if persisted.get(n) == version]:
            held.pop(name, None)
        if not held:
            _unpersisted_versions.pop(map_key, None)


def _state_key() -> str:
    """The state-file element of a :data:`_unpersisted_runs` key, without raising.

    :func:`_state_path` is NOT a pure path join. It goes through
    :func:`app_data_dir`, whose last statement is
    ``mkdir(parents=True, exist_ok=True)``, so merely resolving the path raises
    ``OSError`` on a read-only filesystem, on EACCES/ENOSPC, or when a parent
    path is a file. Those are precisely the conditions this overlay exists to
    survive, which makes an unguarded key derivation self-defeating:
    :func:`_record_run` derives the key from INSIDE its own except handler, where
    an exception would 500 a request whose archive is already in the bucket --
    the exact defect this change exists to remove, reintroduced one layer in.
    The read is already guarded (:func:`read_state` swallows ``OSError``), so
    without this the failure is absorbed once and then raised by the very next
    statement.

    A failure returns a SENTINEL rather than skipping the work. Skipping would
    drop the held record in exactly the case the hold exists for. One sentinel is
    consistent for the life of the process, so the overlay still answers
    :func:`last_runs`, the completed upload still reports, and no caller raises.

    All three key sites go through here rather than each guarding itself: one
    place to reason about, and one place a future edit cannot forget.
    """
    try:
        return str(_state_path())
    except OSError:
        return ""


def _remember_unpersisted(account: str, kind: str, record: dict[str, Any]) -> None:
    path = _state_key()
    with _unpersisted_lock:
        run_key = (path, account, kind)
        if _run_is_newer(record, _unpersisted_runs.get(run_key)):
            _unpersisted_runs[run_key] = record
        uploads = _unpersisted_uploads.setdefault((path, account), {})
        uploads[record["key"]] = str(record.get("fingerprint", "") or "")
        version = str(record.get("version", "") or "")
        versions = _unpersisted_versions.setdefault((path, account), {})
        if version:
            versions[record["key"]] = version
        for stale in list(uploads)[: max(0, len(uploads) - MAX_REMEMBERED_UPLOADS)]:
            uploads.pop(stale, None)
        # Bounded separately from `uploads`, mirroring `_merge_uploads`: a version
        # record outlives the panel history because it is what makes an archive
        # retireable, so trimming it to `uploads` here would re-impose on the
        # recovery path the cliff `_merge_uploads` keeps off the normal one.
        #
        # Counted and said out loud for the same reason as there, and named as the
        # RECOVERY map so a reader of the log can tell the two evictions apart.
        held_overflow = max(0, len(versions) - MAX_RECORDED_VERSIONS)
        if held_overflow:
            logger.warning(
                "aws-control retention: dropping %d oldest held version record(s) past "
                "MAX_RECORDED_VERSIONS=%d from the recovery map; an upload whose state "
                "write never landed loses the proof that makes it retireable",
                held_overflow,
                MAX_RECORDED_VERSIONS,
            )
        for stale in list(versions)[:held_overflow]:
            versions.pop(stale, None)


def _forget_unpersisted(account: str, kind: str, persisted: dict[str, Any]) -> None:
    """Clear exactly the held record processed by a successful state update.

    Its fingerprint is merged even when the final run slot supersedes it.
    Equal wall times do not identify equal runs. Full record equality also
    keeps legacy records without process/sequence metadata acknowledgeable.
    """
    key = (_state_key(), account, kind)
    with _unpersisted_lock:
        if _unpersisted_runs.get(key) == persisted:
            _unpersisted_runs.pop(key, None)


def _merge_unpersisted(account: str, runs: dict[str, Any]) -> dict[str, Any]:
    """Overlay this process's unpersisted runs onto what the state file holds.

    Same-process runs use local sequence, including equal/backwards wall times.
    Other processes and legacy records retain the wall-time fallback; this is
    not proof of global order when their state updates were unobservable.
    """
    path = _state_key()
    with _unpersisted_lock:
        remembered = {
            kind: record
            for (state_path, acct, kind), record in _unpersisted_runs.items()
            if state_path == path and acct == account
        }
    for kind, record in remembered.items():
        if _run_is_newer(record, runs.get(kind)):
            runs[kind] = record
    return runs


def a_retained_archive_carries_conversations(account: str) -> bool:
    """Whether an archive this install has not retired carries a ``conversations/`` root.

    Read as a predicate over one persisted boolean -- see
    :data:`SESSIONS_CONVERSATIONS_RETAINED_KEY`. Anything that is not exactly ``True``
    reads as False, the same posture :func:`sessions_layer_b_enabled` takes: a document
    this code cannot understand must not be read as a reason to keep archives forever.

    False here is the safe direction for STORAGE and the unsafe one for DATA, which is
    the opposite of the grant readers, so it is worth stating why it is still right. An
    install that never carried conversations has nothing to protect, and a corrupted or
    absent value on an install that did will let one sweep retire the archive. The
    alternative -- read an unparseable value as True -- freezes retention on every
    install whose state file ever hiccups, which is the unbounded accumulation this
    module keeps having to remove.

    Read through the SAME unpersisted overlay as :func:`uploaded_objects` and
    :func:`retention_recorded_versions`, and that is load-bearing rather than tidiness.
    The sweep's candidate set and its version set both merge this process's held run
    records; a predicate that read only the persisted document would put the two halves
    of one decision on different snapshots BY CONSTRUCTION. Two same-account sessions
    runs can overlap -- the owner-triggered path does not pass the upload gate, so it is
    not serialized against a nightly run in flight -- and in that window the wide run's
    key was already a live candidate under ``keep=1`` while the fact that protects it was
    still invisible here. Same overlay, same lock, one snapshot.

    This closes the IN-PROCESS half only. A run held unpersisted by ANOTHER process is
    not visible to this map, and no reader of it can be -- see the retention spec for
    that residue and what bounds it.
    """
    if _account_view(account).get(SESSIONS_CONVERSATIONS_RETAINED_KEY, False) is True:
        return True
    path = _state_key()
    with _unpersisted_lock:
        return any(
            state_path == path
            and acct == account
            and record.get(_RUN_CONVERSATIONS_RETAINED) is True
            for (state_path, acct, _kind), record in _unpersisted_runs.items()
        )


def _account_view_checked(account: str) -> tuple[dict[str, Any], bool]:
    """:func:`_account_view` plus whether the state was actually readable.

    A non-dict level in a corrupted file is itself a read failure, not a missing
    key, so it reports False rather than an empty view that looks configured-as-
    default. Only :func:`_retention_keep_for_sweep` consults the flag; everything
    else goes through :func:`_account_view` and keeps its defaults.
    """
    state, readable = _read_state_checked()
    accounts = state.get("accounts", {})
    if not isinstance(accounts, dict):
        return {}, False
    entry = accounts.get(account, {})
    if not isinstance(entry, dict):
        return {}, False
    return entry, readable


def _account_view(account: str) -> dict[str, Any]:
    """Shape-safe read of one account's sub-dict: any non-dict level in a
    corrupted state file reads as empty instead of raising on ``.get``."""
    return _account_view_checked(account)[0]


def _clear_nightly_failure(entry: dict[str, Any], kind: str) -> None:
    """Drop one kind's failure record from an account entry being mutated.

    Takes the ENTRY rather than the account, because both callers are already inside
    a mutate that holds the document -- :func:`_record_run_locked`'s and
    :func:`_merge_pending`'s -- and reading the account again from there would be a
    second read of state the caller is midway through rewriting.

    Each caller pairs this with a run-record write under the SAME condition that
    write is gated on, because a superseded record is not evidence that the backoff
    it would retire is over.

    The key is REMOVED rather than zeroed, so "no failures" has one spelling.
    :func:`_backoff_withholds` already reads a non-positive count as no backoff, so
    a stored zero would behave identically and mean the same thing twice -- and
    :func:`nightly_failures` would then report a healthy kind as a row an operator
    has to interpret instead of an absence they can skip.
    """
    failures = entry.get(NIGHTLY_FAILURE_STATE_KEY)
    if isinstance(failures, dict):
        failures.pop(kind, None)
        if not failures:
            # The whole map goes when its last kind does, for the same reason the
            # kind goes rather than being zeroed: an empty dict left behind is a
            # third spelling of "nothing is failing".
            entry.pop(NIGHTLY_FAILURE_STATE_KEY, None)
