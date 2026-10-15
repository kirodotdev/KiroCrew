"""Prove and hand back Crew's ownership of a claude settings seed on disk.

Bounded no-follow reads of the seed path, the inode-pinned move-aside and no-clobber
restore, whose-file checks, and the settle, discard and release steps that end a
session's claim. The three seed static methods that read the ``AcpClient`` global
(``_settings_path_holds``, ``_claim_pathname_if_ours``, ``_restore_aside_without_clobber``)
stay on the class.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_settings_authored``,
``_claude_settings_claim_unrevoked``, ``_claude_settings_shared``,
``_claude_settings_written``, ``_permission_surface_share_validated``.

Instance fields read: ``_seed_owner``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp import seed_provenance

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def _claude_local_settings_path(self: AcpClient) -> Path:
    return self._work_dir / ".claude" / "settings.local.json"


def _claude_settings_is_still_ours(self: AcpClient) -> bool:
    """Whether settings.local.json still holds the bytes CREW wrote.

    The second half of the ownership test (the first is having created it --
    in this session, or in an earlier one per the durable record).
    A user can replace the file atomically after Crew's create, and the
    replacement is theirs: it must not be overwritten by a re-seed nor
    deleted on reset. An unreadable path answers "not ours" -- declining to
    touch a file Crew cannot verify is the safe direction here.

    **Bounded and non-blocking, because ``_reset_state`` is synchronous and
    runs ON the event loop.** By reset time the path is whatever the world
    left there, and a plain ``read_text`` trusts it three ways: a FIFO blocks
    the open forever and takes the whole gateway's loop with it, a symlink
    redirects the read, and a multi-gigabyte file is pulled into memory to be
    compared against a few hundred bytes. So the check opens the path itself
    with ``O_NONBLOCK`` (a FIFO returns immediately instead of waiting for a
    writer) and ``O_NOFOLLOW``, rejects anything that is not a regular file,
    rejects a size that cannot possibly match, and reads AT MOST one byte
    past the payload it is comparing against. Every refusal answers "not
    ours", which leaves the file alone -- the safe direction.
    """
    return self._settings_path_holds(
        self._claude_local_settings_path(), self._expected_settings_fingerprint()
    )


def _settings_path_bytes(path: Path, expectation: tuple[int, str] | None) -> bytes | None:
    """The bytes at *path* when they are exactly *expectation*, else ``None``.

    The read behind :meth:`_settings_path_holds`, for a caller that needs the
    verified bytes themselves: no link followed, no FIFO waited on, nothing but
    a regular file of the expected size, and never more than one byte past it.
    """
    if expectation is None:
        return None
    size, sha = expectation
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        # A FIFO, device or directory is not Crew's file, and reading one is
        # the hazard. Size first: it settles a huge file without reading it.
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            return None
        # ONE byte past the recorded length, so a file that grew between the
        # fstat and the read is rejected on length rather than matching on a
        # prefix hash. Bounded either way: a few hundred bytes, never the file.
        chunk = os.read(fd, size + 1)
        if len(chunk) == size and hashlib.sha256(chunk).hexdigest() == sha:
            return chunk
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _expected_settings_fingerprint(self: AcpClient) -> tuple[int, str] | None:
    """``(size, sha256)`` of the seed Crew believes is at the settings path.

    Session memory first -- authoritative for a file this client just wrote,
    and available even when the sidecar could not be persisted -- then the
    durable record in :mod:`kiro_crew.acp.seed_provenance`, which is what lets
    a seed orphaned by a killed session (or written by an older Crew) still be
    recognized as Crew's own instead of reading as a stranger's file forever.
    Both sources prove the same thing the same way: the bytes on disk are the
    bytes Crew wrote. Neither is a permission to touch an arbitrary path.

    ``None`` means Crew has no claim to check, which callers read as "not
    ours" -- including the case where the durable record belongs to a SIBLING
    client still seeding this path in this process, since an orphan is what
    the record is for. In-memory only, so this stays safe to call from the
    event loop.
    """
    written = getattr(self, "_claude_settings_written", None)
    if written is not None:
        payload = written.encode("utf-8")
        return len(payload), hashlib.sha256(payload).hexdigest()
    # getattr for the same reason as _reset_state's: tests build clients
    # without __init__, and a shared "" owner there is a consistent identity.
    return seed_provenance.recorded(
        self._claude_local_settings_path(), getattr(self, "_seed_owner", "")
    )


def _withdraw_shared_reader_lease(self: AcpClient, path: Path, owner: str) -> bool:
    """Withdraw the durable reader lease before clearing its lease flag."""
    if not seed_provenance.unshare(path, owner):
        return False
    self._claude_settings_shared = False
    return True


def _rename_aside_noreplace(aside: Path, path: Path) -> bool:
    """Move *aside* back to an ABSENT *path* as the entry it is; ``False`` with no primitive.

    A rename moves the directory entry itself, whatever its type -- a regular
    file, a symlink, a directory -- and reads none of its bytes, so it needs no
    size cap and no follow check. Windows holds
    :func:`platform_compat.pin_directory` across ``os.rename``, which refuses a
    reparse point already at the shared parent and prevents that parent or its
    ancestors from being swapped while held. POSIX uses
    :func:`platform_compat.rename_noreplace` with both names relative to their
    shared parent's descriptor. Raises :class:`FileExistsError` when *path* is
    occupied, so the caller never replaces a file that arrived while the entry
    was out. ``False`` means the host or filesystem has no no-clobber rename,
    the POSIX parent cannot be pinned, or the names do not share a parent. A
    cross-parent Windows aside therefore fails closed and stays aside.
    """
    if platform_compat.IS_WINDOWS:
        if aside.parent != path.parent:
            return False
        pin = platform_compat.pin_directory(path.parent)
        try:
            os.rename(aside, path)
        finally:
            os.close(pin)
        return True
    if not platform_compat.RENAME_NOREPLACE_AVAILABLE or aside.parent != path.parent:
        return False
    try:
        parent_fd = os.open(os.fspath(path.parent), pinned_fs.dir_flags())
    except OSError:
        return False
    try:
        platform_compat.rename_noreplace(
            aside.name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
        )
    except NotImplementedError:
        return False
    finally:
        os.close(parent_fd)
    return True


def _copy_aside_back_without_clobber(
    aside: Path, path: Path, expect: tuple[int, int]
) -> str | None:
    """The validated-copy restore, for a POSIX host without a no-clobber rename.

    Publishes the moved inode's bytes under *path* through
    :func:`pinned_fs.put_back_no_clobber`, both names pinned to the shared
    parent's descriptor, and, once the name is back, removes the aside only
    while it still holds the inode *expect*. Returns the put-back outcome:
    ``None`` when the name is back. A parent that cannot be opened is
    :data:`pinned_fs.PUT_BACK_FAILED` outright: the pinned copy is the only
    restore that verifies what it publishes, so with no descriptor the caller
    keeps the aside as ``.crew-gc`` litter rather than copying by name.
    """
    try:
        parent_fd = os.open(os.fspath(path.parent), pinned_fs.dir_flags())
    except OSError:
        return pinned_fs.PUT_BACK_FAILED
    try:
        try:
            back = pinned_fs.put_back_no_clobber(
                parent_fd, parent_fd, aside.name, path.name, expect_ino=expect[1]
            )
        except NotImplementedError:
            back = pinned_fs.PUT_BACK_FAILED
        if back is None:
            pinned_fs.unlink_verified(parent_fd, aside.name, expect)
    finally:
        os.close(parent_fd)
    return back


async def _discard_claude_settings_seed(self: AcpClient) -> None:
    """Remove the ``settings.local.json`` THIS session seeded, off the loop.

    So a permission mode never outlives its session and an inherited
    ``bypassPermissions`` cannot persist after a crash. Only a file Crew
    created AND still owns is removed: the writer declines a path that already
    holds a foreign file, and the content check below covers the remaining
    case -- a user replacing Crew's file atomically after the create, whose
    replacement is theirs to keep (see ``_write_claude_local_settings``).

    Async, because the disk half is blocking and this is teardown on the event
    loop: the ownership hash, the durable revoke and the unlink all block, and a
    heartbeat must not queue behind them. ``_reset_state`` keeps only the
    in-memory ``release_local`` half.

    **The whole disk half is ONE shielded thread, not a sequence of awaited
    steps, and that is the cancellation contract.** Teardown runs on paths that
    are themselves being cancelled -- a turn cancel, a session close, a
    shutdown -- and a suspension point between the ownership check, the revoke
    and the unlink meant a cancellation could land with the claim already
    revoked and the file still on disk. That is the one state nothing can
    repair: unrecorded bytes carrying a ``permissions.defaultMode`` that no
    later session is permitted to touch. A thread cannot be interrupted
    part-way, so the transaction has either not started or run to completion,
    and ``asyncio.shield`` is what keeps a cancelled awaiter from abandoning it
    before it is scheduled. Every caller pairs this with ``_reset_state`` in a
    ``finally`` for the mirror-image reason: the in-memory reset must happen
    even when the await is cancelled.
    """
    authored = getattr(self, "_claude_settings_authored", False)
    shared = getattr(self, "_claude_settings_shared", False)
    claim_unrevoked = getattr(self, "_claude_settings_claim_unrevoked", False)
    if not authored and not shared and not claim_unrevoked:
        return
    path = self._claude_local_settings_path()
    owner = getattr(self, "_seed_owner", "")
    if claim_unrevoked and not authored:
        # A user replaced Crew's seed and the in-session hand-back could not
        # reach the sidecar. The file at the path is THEIRS, so none of the
        # move/unlink transaction below applies -- only the durable owner
        # holder is still Crew's to withdraw. Same refused-withdrawal
        # contract as the reader lease: a refusal here leaves the flag set,
        # and the caller's ``_reset_state`` drops the in-memory live slot.
        hand_back = asyncio.ensure_future(
            asyncio.to_thread(self._hand_back_unrevoked_claim, path, owner)
        )
        await asyncio.shield(hand_back)
        if not shared:
            return
    if shared and not authored:
        # A refused durable withdrawal deliberately leaves the instance flag
        # set. The caller's ``_reset_state`` then drops only the in-memory
        # half; the persisted pid+start-id lease is stale and reclaimable
        # once this process exits, matching the recorded-orphan residual.
        release = asyncio.ensure_future(
            asyncio.to_thread(self._withdraw_shared_reader_lease, path, owner)
        )
        await asyncio.shield(release)
        return
    # Captured HERE, on the loop, so the transaction is a pure function of its
    # arguments: the ``finally`` below may clear these flags while the thread is
    # still running, and a transaction that re-read them could decide ownership
    # against state that changed underneath it.
    payload = getattr(self, "_claude_settings_written", None)
    expectation = self._expected_settings_fingerprint()
    # A task rather than a bare coroutine: ``shield`` protects a future that
    # already exists, and the point is that this one is scheduled and therefore
    # WILL run even if the cancellation arrives before the first step. Needs no
    # done-callback to consume an exception because the transaction contracts
    # never to raise -- which is also what keeps a cancelled awaiter from
    # leaving an unretrieved error behind.
    settle = asyncio.ensure_future(
        asyncio.to_thread(self._settle_claude_settings_seed, path, owner, payload, expectation)
    )
    try:
        await asyncio.shield(settle)
    finally:
        self._claude_settings_authored = False
        self._claude_settings_written = None


def _hand_back_unrevoked_claim(self: AcpClient, path: Path, owner: str) -> None:
    """Retry the durable hand-back of an owner holder on a file that is not Crew's.

    The disk half of ``_claude_settings_claim_unrevoked``: the same two steps
    the foreign-replace branch took in-session, in the same order. ``forget``
    drops the whole record and fails closed while a live sibling sharer still
    validates against it; ``release`` then drops only this owner's holder and
    leaves the record and the sharer's lease standing. Never touches the
    pathname -- the file there is the user's. Never raises, for the reason the
    settle transaction never raises: the caller shields it. Blocking; runs off
    the loop.
    """
    try:
        if seed_provenance.forget(path, owner) or seed_provenance.release(path, owner):
            self._claude_settings_claim_unrevoked = False
            return
        logger.warning(
            "could not durably hand back Crew's claim on %s; retaining it until process "
            "exit, after which the persisted holder is stale and reclaimable",
            path,
        )
    except Exception:  # pragma: no cover - defensive; teardown must not raise
        logger.debug("could not hand back Crew's claim on %s", path, exc_info=True)


def _settle_claude_settings_seed(
    self: AcpClient,
    path: Path,
    owner: str,
    payload: str | None,
    expectation: tuple[int, str] | None,
) -> None:
    """The seed's entire disk half, as one blocking transaction. Never raises.

    **The pathname is claimed by an atomic move-aside, THEN revoked, THEN the
    moved inode is deleted.** Verifying ownership by pathname and then unlinking
    that pathname is a TOCTOU: a user who atomically replaced the file in the gap
    (which this transaction widens, since the revoke now takes a cross-process
    lock and writes) would have their settings deleted. ``_claim_pathname_if_ours``
    closes it -- ``os.replace`` captures the file into ``aside`` in one step, and
    only that fixed inode is ever deleted; a replacement that raced in is detected
    and left in place. The revoke still happens BEFORE the delete: ``forget``
    reports whether the sidecar on disk actually stopped naming the path, and
    deleting first would let a failed sidecar write leave a record that outlives
    its file, so the next process adopts, rewrites and deletes a byte-identical
    copy the user had restored. On a failed revoke the moved file is restored
    under its pathname, so a later session repairs the orphan.

    Never raises, because the caller shields it: an exception here would reach
    nobody but the "never retrieved" logger, and a half-settled transaction that
    also lost its error is worse than one that logged and left the file owned.

    The whole transaction runs under ``seed_provenance.SETTLE_LOCK``, the
    same lock a sharer's validate-then-take-lease sequence holds: without
    it, this transaction's own move-aside manufactures a vacancy at the
    pathname, and a user replacement racing into that vacancy is
    (correctly) preserved by the no-clobber restore -- leaving a sharer
    that validated the ORIGINAL bytes governed against a file it never
    verified. Under the lock a sharer validates either before the move
    (its registration then pins the post-move barrier below) or after the
    transaction settles, never inside the window.
    """
    with seed_provenance.SETTLE_LOCK:
        self._settle_claude_settings_seed_locked(path, owner, payload, expectation)


def release_settings_seed_claim(self: AcpClient) -> None:
    """Hand back this client's live ``settings.local.json`` registrations, synchronously.

    The seed hand-back for a client torn down WITHOUT :meth:`shutdown`. The
    session layer's teardown for a provider it is abandoning
    (``_dispatch_hard_kill`` -> ``session_pid._sync_kill_provider``) ends the
    process and drops the client object; neither
    :meth:`_discard_claude_settings_seed` nor :meth:`_reset_state` runs on that
    path, so without this hand-back the registrations this client took in
    :mod:`~kiro_crew.acp.seed_provenance` outlive it for the rest of the
    gateway process. A stale live slot keeps ``claim`` and ``held_by_another``
    refusing every later client on this ``work_dir``, and a client whose
    payload the stale seed cannot be shared with runs with its whole
    ``mcpServers`` array withheld.

    Both halves of each registration are withdrawn, because the persisted
    holder carries this PROCESS's live identity and ``claim`` re-reads it under
    the cross-process lock: dropping only the in-memory slot (``release_local``)
    leaves the next client's adoption refused by a holder whose client is gone.
    An owner claim is handed back with :func:`seed_provenance.release`,
    never ``forget``: the file and its durable record stay, which is the
    recorded-orphan shape an abrupt end already leaves and the one the next
    session adopts, re-seeds for itself and removes on its own teardown. A
    reader lease is withdrawn with :func:`seed_provenance.unshare`, so the
    owner's teardown stops holding the file for a reader that is gone.

    Gated on the registrations this client's own token actually holds (see
    :func:`seed_provenance.release_held`): a client that holds neither -- every
    kiro-backend client, a claude client that never seeded -- performs no
    sidecar I/O at all, so no backend check is needed or made. Synchronous and
    blocking (each withdrawal rewrites the sidecar under the cross-process
    lock); the caller runs it OFF the event loop, on the thread the teardown
    itself runs on. Never raises: a refused durable withdrawal leaves the
    persisted holder reclaimable once this process exits, the same contract
    :meth:`_discard_claude_settings_seed` carries, and an exception out of here
    would cost the teardown that follows it. ``getattr`` throughout, because
    the teardown may reach a client built without ``__init__``.
    """
    owner = getattr(self, "_seed_owner", "")
    if not owner or getattr(self, "_work_dir", None) is None:
        return
    try:
        path = self._claude_local_settings_path()
        if not seed_provenance.release_held(path, owner):
            logger.warning(
                "could not durably hand back Crew's registration on %s while its "
                "provider was torn down; retaining the persisted holder until process "
                "exit, after which it is stale and reclaimable",
                path,
            )
        if getattr(self, "_claude_settings_claim_unrevoked", False):
            # The owner holder still owed on a file a user replaced: the same
            # retry the discard runs, on the same off-loop footing.
            self._hand_back_unrevoked_claim(path, owner)
    except Exception:  # pragma: no cover - defensive; teardown must not raise
        logger.debug("could not hand back Crew's settings seed registration", exc_info=True)
    # The in-memory registrations are gone whatever the durable outcome, as
    # after ``_discard_claude_settings_seed`` and ``_reset_state``; the
    # instance flags that mirror them follow, so nothing later reads this
    # client as still governing or still owing a hand-back.
    self._claude_settings_authored = False
    self._claude_settings_written = None
    self._claude_settings_claim_unrevoked = False
    self._claude_settings_shared = False
    self._permission_surface_share_validated = False


def _foreign_settings_owner(self: AcpClient, local_settings: Path) -> str:
    """Who a ``settings.local.json`` Crew does not govern belongs to, as a log phrase.

    The bytes Crew's durable record names are a Crew seed another open
    session holds, not the project's own file: removing or renaming it only
    lasts until that session re-seeds it. Same bounded, no-follow read as
    the share check.
    """
    if self._foreign_settings_is_crew_seed(local_settings):
        return "is another open Crew session's settings seed for this work dir"
    return "belongs to the project"


def _foreign_settings_is_crew_seed(self: AcpClient, local_settings: Path) -> bool:
    """Whether *local_settings* holds the bytes Crew's durable record names."""
    recorded = seed_provenance.recorded_durable(local_settings)
    return self._settings_path_bytes(local_settings, recorded) is not None


#: :meth:`settings_withheld_cause` answers: another open Crew session's seed holds the file.
SETTINGS_WITHHELD_BY_SESSION = "session"
#: :meth:`settings_withheld_cause` answers: the project's own file holds the path.
SETTINGS_WITHHELD_BY_PROJECT = "project"


def settings_withheld_cause(self: AcpClient) -> str:
    """Why Crew's MCP tools are withheld from this session, or ``""`` when they are not.

    Reads the live governed state, the same predicate the claude mirror and the
    member projection read to withhold the ``mcpServers`` array
    (:attr:`_permission_surface_governed` on a backend that seeds the file), so
    it answers for every decline path and for a later re-earn alike. When
    withheld, names whose file holds the path: :data:`SETTINGS_WITHHELD_BY_SESSION`
    or :data:`SETTINGS_WITHHELD_BY_PROJECT`. A bounded, no-follow file read:
    call it off the event loop.
    """
    if not self._seeds_local_settings or self._permission_surface_governed:
        return ""
    local_settings = self._claude_local_settings_path()
    if self._foreign_settings_is_crew_seed(local_settings):
        return SETTINGS_WITHHELD_BY_SESSION
    # No file at all (the seed has not run, or its write failed): neither
    # answer would name the holder, so the caller shows nothing.
    if not os.path.lexists(local_settings):
        return ""
    return SETTINGS_WITHHELD_BY_PROJECT


def _settle_claude_settings_seed_locked(
    self: AcpClient,
    path: Path,
    owner: str,
    payload: str | None,
    expectation: tuple[int, str] | None,
) -> None:
    try:
        if seed_provenance.has_sharers(path):
            # A live SHARER session in this process delivered its MCP array
            # against exactly the bytes at this path, and it can neither see
            # nor stop whatever would occupy the pathname next. Unlinking here
            # would free the name for a different permission file -- another
            # session's mode, up to bypassPermissions -- under tools already
            # delivered. So the file and its durable record both stay: that is
            # precisely the recorded-orphan shape a kill -9 leaves, which the
            # next session adopts and repairs once the sharers are gone (claim
            # refuses adoption while any remain). Only this owner's live slot
            # is handed back.
            logger.info(
                "%s is still shared by a live sibling session; leaving the seed in "
                "place for it rather than deleting a permission file its tools were "
                "delivered against. The next session adopts and cleans it up once "
                "the sharers are gone.",
                path,
            )
            if not seed_provenance.release(path, owner):
                logger.warning(
                    "could not durably release Crew's claim on %s; retaining the claim "
                    "with the seed until a later retry or process exit",
                    path,
                )
            return
        claimed = self._claim_pathname_if_ours(path, expectation)
        if claimed is None:
            logger.info(
                "%s no longer holds the bytes Crew wrote; leaving the replacement in "
                "place instead of deleting a file Crew does not own.",
                path,
            )
            # Nothing at the pathname is Crew's to move, revoke or delete, but
            # the durable owner holder ``record`` published still carries this
            # live process's identity. Handed back here, as the sibling arms do:
            # left standing, no prune reclaims it while this process lives, and
            # every later session on this work_dir reads the pathname as held
            # by a live sibling. ``release`` drops only this owner's holder --
            # never the record, the file, or a sharer's lease.
            if not seed_provenance.release(path, owner):
                logger.warning(
                    "could not durably release Crew's claim on %s; retaining the claim "
                    "until a later retry or process exit",
                    path,
                )
            return
        aside, aside_ident = claimed
        # Re-checked AFTER the move-aside, not only at the top: the probe above
        # and the move are not one atomic step, and a sharer registers BEFORE it
        # validates -- so a sharer whose disk check passed read the file before
        # the move, and its registration is necessarily visible here. Without
        # this barrier the interleaving "probe sees none -> sharer registers and
        # validates -> move/forget/unlink proceed" frees the pathname under a
        # governed reader. Restore the moved inode and stand down instead.
        if seed_provenance.has_sharers(path):
            logger.info(
                "%s gained a live sharer while its teardown was starting; restoring "
                "the seed and leaving it in place for that session.",
                path,
            )
            # No-clobber, because the pathname has been free since the
            # move-aside: a settings file the user recreated in that window is
            # theirs, and a replace-semantics restore would silently destroy it.
            self._restore_aside_without_clobber(aside, path, aside_ident)
            if not seed_provenance.release(path, owner):
                logger.warning(
                    "could not durably release Crew's claim on %s; retaining the claim "
                    "with the restored seed until a later retry or process exit",
                    path,
                )
            return
        # The file is now the moved inode ``aside`` and the pathname is free, so a
        # user replacement racing in lands at a fresh ``path`` this never touches.
        if not seed_provenance.forget(path, owner):
            # Not revoked on disk, so the file must stay: a restart would still
            # read Crew as its owner, and a later session repairs it. Restore it
            # under the pathname and hand back the live claim.
            logger.warning(
                "could not durably revoke Crew's claim on %s; restoring the file so a "
                "later session can re-seed or remove it rather than deleting it "
                "behind a revocation that never reached the disk",
                path,
            )
            self._restore_aside_without_clobber(aside, path, aside_ident)
            if not seed_provenance.release(path, owner):
                logger.warning(
                    "could not durably release Crew's claim on %s; retaining the claim "
                    "with the restored seed until a later retry or process exit",
                    path,
                )
            return
        try:
            unlink_errors: list[OSError] = []
            pinned_fs.unlink_verified_by_name(
                path.parent,
                aside.name,
                aside_ident,
                on_error=unlink_errors.append,
            )
            if unlink_errors:
                raise unlink_errors[0]
        except OSError:
            # Revoke landed but the moved inode will not delete. Put it back under
            # the pathname and re-record, so the path is a repairable orphan rather
            # than a frozen ``.crew-gc`` no session names.
            logger.debug(
                "could not remove %s after session reset; re-recording Crew's claim so "
                "a later session can still re-seed or remove it",
                path,
                exc_info=True,
            )
            if payload is not None:
                if self._restore_aside_without_clobber(aside, path, aside_ident):
                    if seed_provenance.record(path, payload, owner):
                        if not seed_provenance.release(path, owner):
                            logger.warning(
                                "could not durably release Crew's re-recorded claim on "
                                "%s; retaining it until a later retry or process exit",
                                path,
                            )
                    else:
                        logger.warning(
                            "could not durably re-record Crew's restored seed at %s; "
                            "leaving the recoverable bytes in place",
                            path,
                        )
    except Exception:  # pragma: no cover - defensive; teardown must not raise
        logger.debug("could not settle Crew's settings seed at %s", path, exc_info=True)
