"""The committed queue generation, kept where the transcript's editor cannot reach.

A slot's queued prompts persist on its session's metadata line, an ordinary
agent-writable file. Every record of one durable write is sealed under the
write's GENERATION (``slot_queue_repository.ORIGIN_GENERATION_KEY``), which is
what lets the restore refuse a record kept from an older write and slipped in
beside the current ones: the line then carries two generations, which no write
of this gateway produces. What the line alone cannot refuse is a line REPLACED
WHOLE by an older one -- every record on it names the same, older generation,
every seal verifies under it, and a ``/goal`` or ``/workflow`` the gateway had
already consumed comes back and runs again with the authority it was queued
with. Told from the line alone, that rollback is indistinguishable from a crash
before the newer write landed. So it is not told from the line alone.

This module keeps, for each slot, the generation of the durable write this
gateway LAST COMMITTED for it -- in a top-level crew-home directory
(:data:`STORE_DIR_LEAF`) that is masked from every sandboxed process
(``sandbox._CREW_HIDDEN_LEAVES``, pre-created before the mask so a fresh home
has a name to cover), fenced from the agent file tools and every shell form
(``security._CREW_SECRET_LEAVES``), and opened only by the gateway process,
directly. The save commits the generation here as it commits the line
(``chat_persistence``), and the restore REQUIRES it: told against the record,
a line whose one generation is not the committed one -- older, newer, absent,
or two of them -- is a line this gateway did not write last, and every record on
it is rejected; told against NO record for the transcript, a line that carries a
generation is nothing to honour and every record on it is dropped under its own
notice, not the tamper one (``slot_queue_repository.restore_queue_provenance``,
its three outcomes). The record is worth what
the fence and the mask are worth, which is what ``token_signing.key`` is
worth: it carries no secret and certifies nothing by itself, so it is not
signed.

A record names the TRANSCRIPT INCARNATION it was committed for, not only the
slot key. A slot key is reused: a session permanently deleted and a new one
opened under the same key is a new transcript, whose metadata line carries its
own ``created_at`` (minted with the line, carried through every rewrite --
``chat_persistence`` reads it as the file's identity for exactly this reason).
Keyed by the slot key alone, a record would outlive the transcript it was
committed for, and the deleted transcript put back from a copy -- a consumed,
sealed ``/goal`` among its entries -- would verify under it. So the save binds
the record to the committed line's ``created_at``, the restore reads it only for
the line that names the same one, and the permanent delete TOMBSTONES the
record (``handlers/sessions._delete_history_session``): the record is state
that must not survive into the next session on a recycled slot key. The
tombstone is two-phase, because the delete it belongs to can refuse AFTER it --
the ledger exclusion may be unwritable, the transcript's own unlink refuses on
a search-index row it cannot drop or an attachments directory it cannot move
-- and a record gone from under a transcript that survives is that
transcript's queued prompts dropped, with the notice, at the next restart. So
the record is first MOVED ASIDE, one rename inside the store directory
(:func:`stage_queue_generation_tombstone`; a move that fails refuses the
delete with the row intact and nothing written, as the ledger exclusion beside
it does), moved BACK by the same rename when any later step refuses
(:meth:`StagedTombstone.rollback`), and unlinked only once the transcript is
gone (:meth:`StagedTombstone.purge`) -- the last irreversible step, and one
whose failure leaves a name the restore never reads.

Two costs, both fail-closed and both stated so nobody reads them as a bug. A
process that dies between committing the line and committing its generation
here leaves the two apart, and the next restore drops that queue -- with the
dashboard notice, never silently -- exactly as it would drop a rollback. And an
operator who restores an older transcript from a backup restores a queue this
gateway will not honour: the prompts on it are dropped with the notice and must
be re-sent. Neither is a replay of a consumed command, which is the harm the
record exists to remove.

Every function is blocking file IO and runs OFF the event loop only: the save
runs in an executor (or a synchronous caller's thread), the restore reads the
record in its prefetch half (``_prefetch_rehydrate_inputs``,
``_prefetch_recent_session``), beside the metadata line it belongs to, and the
delete runs in the worker thread that holds the transcript lock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Iterable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: A dedicated top-level crew-home leaf, masked HIDDEN by every sandbox mode
#: (``sandbox._CREW_HIDDEN_LEAVES`` + pre-created via
#: ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``) and fenced from agent file tools by
#: ``security._CREW_SECRET_LEAVES``. Top-level because a mask covers the leaf,
#: not its ancestors, so no agent-writable directory can be renamed out from
#: under it. NOT beside the transcript it certifies: the sessions directory is
#: the agent-writable plane this record exists to check.
STORE_DIR_LEAF = "queue-generations"

#: A record written by a newer build is treated as absent (fail closed) rather
#: than guessed at. Version 2 binds the record to a transcript incarnation; a
#: version-1 record (slot key only) reads as absent for the same reason.
_SCHEMA_VERSION = 2

#: Upper bound on one record (``{"v": 2, "slot": <key>, "incarnation": <created_at>,
#: "generation": <32 hex>}`` is well under 1 KiB); anything larger is not a record
#: this module wrote.
_MAX_RECORD_BYTES = 4096

#: :func:`read_committed_generation`'s answer for a record that is this slot's but
#: names ANOTHER transcript incarnation -- left behind by a deletion this gateway
#: did not perform (the tombstone runs only on its own permanent delete). Never a
#: generation any line names: the restore reads it as "no record for this
#: transcript" exactly as it reads ``None`` -- a sealed line is dropped whole under
#: the unrecorded-write notice, not the tamper one (``slot_queue_repository.
#: UNRECORDED_GENERATION_CONSTRAINT``); unlike ``None`` it seeds the restored
#: slot's committed witness (``chat_persistence._commit_queue_generation``), so the
#: slot's next save commits a fresh generation over the stale record and retires it.
STALE_INCARNATION = "__stale_incarnation__"


class QueueGenerationTombstoneError(OSError):
    """The permanent delete could not move a slot's record aside.

    Raised by :func:`stage_queue_generation_tombstone` so the delete REFUSES
    with the transcript intact: a record that outlived its transcript is the
    replay residual this module exists to close. The log line written beside it
    names the record's PATH, for the operator; the exception itself carries the
    store leaf and never a path, because its message can travel into a response
    body.
    """


def _record_path(slot_key: str) -> Path:
    """One file per slot, named by a digest of the slot key: the key is stored
    inside the record and checked on read, so the name never has to carry it
    (slot keys are filename-safe today, and this does not depend on it)."""
    digest = hashlib.sha256(slot_key.encode("utf-8", "replace")).hexdigest()[:32]
    return config_dir() / STORE_DIR_LEAF / f"{digest}.json"


def _store_dir() -> Path:
    """The store directory, verified real and restricted to the owner.

    Mirrors ``chat_tag_grants._store_dir``: ``is_link_or_junction`` first (a
    Windows junction is not a symlink), then the owner-only helpers rather than
    ``mkdir(mode=0o700)``, whose mode bits are a no-op on Windows.
    """
    directory = config_dir() / STORE_DIR_LEAF
    if platform_compat.is_link_or_junction(directory):
        logger.error("%s is a link; removing it before writing queue generation state", directory)
        platform_compat.unlink_link_or_junction(directory)
    platform_compat.make_owner_only_dir(directory)
    platform_compat.restrict_dir_to_owner(directory)
    return directory


def commit_queue_generation(slot_key: str, generation: str, incarnation: str) -> bool:
    """Record *generation* as the one the durable queue line of *slot_key* carries.

    Called by the save right after the line is committed, with the generation
    the committed records name (or the slot's current one when the committed
    queue is empty, so a rollback to the queue that was just consumed does not
    match either) and *incarnation*, the committed line's metadata
    ``created_at`` -- the transcript the record is good for. Atomic replace of a
    sibling temp inside the store directory, owner-only. Blocking: off the loop
    only.

    Returns False, with a warning naming the slot and the PATH, when the record
    cannot be written, and False for a line that names no incarnation (nothing
    to bind to; the full save stamps one on every line it writes). The save then
    leaves the queue owed (its persisted-queue witness is not advanced), so the
    next flush pass re-saves the line and retries this write; until one lands,
    the line stands with no matching record, and a restart drops the queue it
    carries rather than honouring a line this gateway cannot place among its own
    writes.
    """
    if not slot_key or not generation or not incarnation:
        return False
    path = _record_path(slot_key)
    try:
        _store_dir()
        payload = json.dumps(
            {
                "v": _SCHEMA_VERSION,
                "slot": slot_key,
                "incarnation": incarnation,
                "generation": generation,
            },
            ensure_ascii=False,
        )
        atomic_write(path, payload, restrict_to_owner=True)
    except OSError as exc:
        logger.warning(
            "Slot %s queue generation could not be recorded at %s (%s); the queued "
            "prompts stay owed and the next flush retries, and a restart before one "
            "lands drops the prompts the line carries",
            slot_key,
            path,
            exc,
        )
        return False
    return True


def read_committed_generation(slot_key: str, incarnation: str) -> str | None:
    """The generation the gateway last committed for *slot_key*'s transcript
    *incarnation*, or None, or :data:`STALE_INCARNATION`.

    None for a slot with no record -- one whose queue this build has never
    written -- and for a record that is not one this module wrote (not a regular
    file, over :data:`_MAX_RECORD_BYTES`, malformed, another schema, or naming
    another slot). :data:`STALE_INCARNATION` for this slot's record when it names
    ANOTHER incarnation of the transcript: the line whose ``created_at`` is read
    here is a transcript the record was never about -- one restored from a copy
    after the slot key was reused, or the new transcript under a key whose old
    transcript was removed by a path that ran no tombstone. The restore treats
    both as "nothing to honour" -- a line that carries a generation anyway is
    dropped whole, under the unrecorded-write notice rather than the tamper one,
    since nothing on it was altered -- and, for the stale answer, seeds the
    witness that makes the slot's next save commit a fresh record over the stale
    one. A line naming no incarnation reads None. Never raises. Blocking: off the
    loop only.
    """
    if not slot_key or not incarnation:
        return None
    path = _record_path(slot_key)
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_RECORD_BYTES:
            return None
        with path.open("rb") as fh:
            record = json.loads(fh.read(_MAX_RECORD_BYTES).decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning(
            "Slot %s queue generation record at %s is unreadable; the queued prompts "
            "its line carries are not honoured",
            slot_key,
            path,
        )
        return None
    if not isinstance(record, dict) or record.get("v") != _SCHEMA_VERSION:
        return None
    if record.get("slot") != slot_key:
        return None
    generation = record.get("generation")
    if not isinstance(generation, str) or not generation:
        return None
    recorded = record.get("incarnation")
    if not isinstance(recorded, str) or not recorded:
        return None
    if recorded != incarnation:
        return STALE_INCARNATION
    return generation


class StagedTombstone:
    """Records moved aside for a permanent delete, not yet gone.

    The reversible half of the tombstone (:func:`stage_queue_generation_tombstone`
    makes one). The delete finishes it one way or the other: :meth:`purge` once
    the transcript is gone, :meth:`rollback` when any later step refused. Neither
    raises -- the delete's own outcome is decided by then -- and both are
    idempotent. An aside name (``<record>.deleting-<pid>-<hex>``) is never read
    as a record: the restore reads the record path alone.
    """

    __slots__ = ("_aside",)

    def __init__(self, aside: dict[str, Path]) -> None:
        # slot key -> where its record was moved to; empty for a slot with none.
        self._aside = aside

    @property
    def slot_keys(self) -> tuple[str, ...]:
        """The slots whose record is currently aside (still reversible)."""
        return tuple(self._aside)

    def rollback(self) -> bool:
        """Put every record back where the restore reads it: a later delete step
        refused, so the transcript survives and its queue must too.

        Returns False when a record could not be put back -- the directory turned
        unwritable between two renames -- with an error naming the slot and both
        paths; that queue is then dropped, with the notice, at the next restart.
        A record already back at the path (a save committed a fresh one while the
        delete was refusing) is newer than the copy aside, so it stands and the
        copy is purged: a rollback never moves the committed generation
        backwards, which is the one thing this module exists to refuse.
        """
        ok = True
        for slot_key, aside in list(self._aside.items()):
            path = _record_path(slot_key)
            try:
                os.lstat(path)
            except FileNotFoundError:
                pass
            except OSError:
                # Unknown whether a record is back; the copy aside is the record
                # that was there, so it goes back and an occupant is replaced.
                pass
            else:
                logger.info(
                    "Slot %s committed a fresh queue generation at %s while its delete was "
                    "refused; the copy moved aside at %s is older and is dropped",
                    slot_key,
                    path,
                    aside,
                )
                self._unlink_aside(slot_key, aside)
                continue
            try:
                os.replace(aside, path)
            except FileNotFoundError:
                # Nothing aside any more (a concurrent finish); nothing to put back.
                del self._aside[slot_key]
            except OSError as exc:
                logger.error(
                    "Slot %s queue generation record, moved aside at %s for a delete that "
                    "was then refused, could not be put back at %s (%s); the session was "
                    "NOT deleted, and until the record is put back by hand a restart drops "
                    "the queued prompts its transcript carries",
                    slot_key,
                    aside,
                    path,
                    exc,
                )
                ok = False
            else:
                del self._aside[slot_key]
        return ok

    def purge(self) -> None:
        """Remove every copy aside: the transcript is gone, so the record is state
        that must not survive into the next session on the slot key. The last
        irreversible step. A copy that cannot be unlinked is logged and left: it
        is not at the record path, so nothing reads it as a record."""
        for slot_key, aside in list(self._aside.items()):
            self._unlink_aside(slot_key, aside)

    def _unlink_aside(self, slot_key: str, aside: Path) -> None:
        try:
            aside.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "Slot %s tombstoned queue generation record left aside at %s (%s); it is "
                "not read as a record",
                slot_key,
                aside,
                exc,
            )
        self._aside.pop(slot_key, None)


def stage_queue_generation_tombstone(slot_keys: Iterable[str]) -> StagedTombstone:
    """Move the record of every slot in *slot_keys* aside: the permanent delete
    of their transcript begins.

    Runs BEFORE the ledger exclusion and the transcript's unlink, inside the
    same lock, for the reason the exclusion does: a record that outlives its
    transcript would verify that transcript put back from a copy under the same
    slot key -- a consumed, sealed command among its entries -- and a failure
    after the unlink has nothing left to refuse. One rename per record, inside
    the store directory, so putting it back needs no free space and no second
    write. Raises :class:`QueueGenerationTombstoneError` when a record cannot be
    moved -- with every record already moved put back first -- so the caller
    refuses the delete with the row intact and nothing written. A slot with no
    record, or an empty key, stages nothing. Blocking: off the loop only.
    """
    staged = StagedTombstone({})
    for slot_key in dict.fromkeys(slot_keys):
        if not slot_key:
            continue
        path = _record_path(slot_key)
        # A fresh name per attempt, never reused: a copy left aside by a crash
        # mid-delete is garbage this rename must not be redirected onto.
        aside = path.with_name(f"{path.name}.deleting-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            os.replace(path, aside)
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.error(
                "Slot %s queue generation record at %s could not be moved aside (%s); the "
                "session was NOT deleted, because the record would verify its transcript "
                "put back from a copy",
                slot_key,
                path,
                exc,
            )
            staged.rollback()
            raise QueueGenerationTombstoneError(
                f"queue generation record for slot {slot_key} under {STORE_DIR_LEAF} could not "
                "be moved aside"
            ) from exc
        staged._aside[slot_key] = aside
    return staged
