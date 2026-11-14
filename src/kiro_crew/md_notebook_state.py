"""Where the Notes builtin keeps its state, and its one-time moves out of older layouts.

The Notes (md-notebook) builtin keeps three state files: ``pat`` (a GitHub token),
``vaults.json`` (the registry the auto-sync loop trusts as its push target) and
``settings.json`` (``autoSync``, the bit that authorizes that push). They live INSIDE
``<crew-home>/apps/md-notebook/data/state/``, a directory the app's own manifest declares
in ``credentialMasks`` (``"state/"``), so the OS sandbox masks it as one directory and
hands it back to the Notes backend alone, through the generic App Kit seam. A writer
stages its temp in that same directory and renames it onto a name in it, so the publish
never crosses a mount point.

The previous layout kept the same files in the top-level ``<crew-home>/md-notebook-staging/``
directory, which core masked and handed back to the shipped backend by name.
:func:`move_state_to_app_data` copies them out once; that directory stays masked and
fenced, and holds the marker that closes the copy.

An older build kept the files at ``<crew-home>/workspace/md-notebook/<name>`` and staged
in the separate staging directory. Under the Linux sandbox the staging directory and each
file were separate mount points, so every state write failed with ``EXDEV``. Those bare
names stay masked and fenced; :func:`migrate_legacy_state` moves real state out of them.

This module imports no app code and no aiohttp, so the sandbox's spawn prep and the
gateway's app spawner can both call it, and the Notes backend can read its names.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, fsync_dir, refuse_linked_parent
from kiro_crew.owner_only_files import mkdirs_owner_only
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

logger = logging.getLogger(__name__)

#: The state directory's name, a direct child of the crew data home. Byte-identical to
#: ``sandbox_leaves._MD_NOTEBOOK_STAGING_LEAF``; a test pins the two. Spelled here, not
#: imported, so the Notes backend does not load the sandbox modules.
STATE_DIR_LEAF = "md-notebook-staging"

#: The legacy directory, relative to the crew data home, that held bare state files.
LEGACY_DIR_PARTS: tuple[str, ...] = ("workspace", "md-notebook")

#: The three state files, in the order the migration moves them.
STATE_FILE_NAMES: tuple[str, ...] = ("pat", "vaults.json", "settings.json")

#: What each state file holds when there is no real state. Byte-identical to the stubs
#: ``sandbox_precreate._MD_NOTEBOOK_PRECREATE_CONTENT`` plants; a test pins the two.
ABSENT_EQUIVALENT: dict[str, bytes] = {
    "pat": b"",
    "vaults.json": b"[]\n",
    "settings.json": b"{}\n",
}

#: Written in the state directory when the move's one pass ends. Its existence closes
#: promotion for good.
MIGRATION_MARKER = ".migrated"

#: Held by a pass from its marker check to its marker write.
LOCK_NAME = ".migrating"

#: Where the state lives now, relative to the crew data home: the app's own data
#: directory, under the directory entry its manifest declares in ``credentialMasks``.
APP_STATE_PARTS: tuple[str, ...] = ("apps", "md-notebook", "data", "state")

#: The app lifecycle's move-aside name for ``apps/<name>/data/`` is
#: ``apps/.<name>`` + this suffix. Byte-identical to
#: ``apps.credential_masks.STAGED_DATA_SUFFIX``; a test pins the two.
STAGED_DATA_SUFFIX = "-data-tmp"

#: Written in :data:`STATE_DIR_LEAF` (core-masked, so no agent can plant it) once its
#: state has been copied into :data:`APP_STATE_PARTS`. Its existence closes that copy.
APP_MOVE_MARKER = ".moved-to-app-data"


def _crew_home() -> Path:
    # Called per use, never at import: KIROCREW_HOME can change after this module loads.
    from kiro_crew.config.paths import config_dir

    try:
        return config_dir()
    except Exception:
        override = os.environ.get("KIROCREW_HOME")
        return Path(override) if override else Path.home() / ".kiro" / "crew"


def default_state_dir() -> Path:
    """``<crew-home>/md-notebook-staging``, resolved on every call."""
    return _crew_home() / STATE_DIR_LEAF


def default_app_state_dir() -> Path:
    """``<crew-home>/apps/md-notebook/data/state``, resolved on every call."""
    return _crew_home().joinpath(*APP_STATE_PARTS)


def default_legacy_dir() -> Path:
    """``<crew-home>/workspace/md-notebook``, resolved on every call."""
    return _crew_home().joinpath(*LEGACY_DIR_PARTS)


class SecondNameError(OSError):
    """The legacy PAT has a second hard link, so the move must not touch it.

    Copying it to a fresh inode and removing the masked name would leave the token readable
    at the other name, out of sight of the sandbox's hard-link check, which counts links on
    the masked name. The file is left in place, the pass is not sealed, and that check
    refuses the spawn. The registry and settings carry no secret, so a second name on them
    exposes nothing a copy adds, and they move as usual.
    """

    def __init__(self, path: Path, links: int) -> None:
        super().__init__(f"{path} has {links} hard links; remove the extra name(s) and retry")


def _read_regular_no_follow(path: Path, *, sole_link: bool = False) -> bytes | None:
    """The bytes of *path* if it is a regular file reached without following its last link.

    Opened once with ``O_NOFOLLOW`` and judged by ``fstat`` on that descriptor, so the
    bytes read are the bytes judged; the caller writes these bytes and never re-opens the
    path. ``None`` when absent, a link, or not a regular file; any other failure raises
    ``OSError``. With *sole_link*, raises :class:`SecondNameError` for a regular file with
    more than one hard link.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except FileNotFoundError:
        return None
    # Any other failure (EACCES, EIO, a race to a link) propagates, so the pass stays
    # unsealed and retries instead of sealing over state it could not read.
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if sole_link and st.st_nlink != 1:
            raise SecondNameError(path, st.st_nlink)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _empty_in_place(path: Path) -> None:
    """Truncate *path* to zero bytes through an ``O_NOFOLLOW`` descriptor, then sync it.

    Truncated, never unlinked: the inode is what a live namespace's bind mask sits on, and
    removing the name would detach that mask there. Zero bytes is the PAT's absent stub.
    """
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_nlink != 1:
            raise SecondNameError(path, st.st_nlink)
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_stub(name: str, path: Path) -> bool:
    """Whether *path* is absent or holds only *name*'s absent-equivalent bytes.

    An unreadable or irregular file is NOT a stub, so the move never overwrites it.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        return _read_regular_no_follow(path) == ABSENT_EQUIVALENT[name]
    except OSError:
        return False


@contextlib.contextmanager
def _pass_lock(state_dir: Path) -> Iterator[None]:
    """Serialize whole passes across threads and processes.

    Two spawns can start a pass at once. Held from the marker check through the marker
    write, so a late pass always sees the seal and cannot copy legacy bytes over state the
    backend saved after an earlier pass sealed. Bounded and fail-closed
    (:func:`platform_lock_compat.acquire_lock`). Only passes take it; a backend write never
    runs before the seal, so it needs none.
    """
    refuse_linked_parent(state_dir / LOCK_NAME)
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = open_create_or_existing(
        state_dir / LOCK_NAME, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def migrate_legacy_state(state_dir: Path, legacy_dir: Path) -> list[str]:
    """Move real legacy state from *legacy_dir* into *state_dir*, in one pass, once.

    Runs in the gateway process, which sees the real data home with no masks. For each
    state file it reads the bare legacy copy ONCE (regular file, last component not
    followed, parent chain free of planted links) and writes THOSE bytes into the state
    directory with ``atomic_write(restrict_to_owner=True, fsync=True)``, but only over an
    absent or stub destination. Copying rather than renaming also works when
    ``workspace/`` sits on another filesystem.

    Then the state directory itself is synced, so the copies survive power loss, and only
    after that is the legacy PAT emptied in place (:func:`_empty_in_place`): a leftover copy
    would keep a token the user may revoke at a second name. The registry and settings hold
    no secret and stay at their masked, fenced legacy names, so a rollback to a build that
    reads the bare names loses only the PAT. If the sync fails, nothing is emptied.

    A legacy directory reached through a link is sealed with nothing moved: an agent can
    swap a link in that chain for a real directory of its own, so nothing behind it may be
    promoted. Notes could not write state on that layout before this layout either (its
    carve-out was withheld), and the Notes backend does not start on it now.

    The pass ends by writing :data:`MIGRATION_MARKER`, whether or not anything moved, so a
    bare file an agent writes later is never a candidate. A copy that fails stops the pass
    UNSEALED (``OSError``), so the next spawn copies it again; until then the Notes
    backend does not start (spawn prep logs the failure and goes on).

    Passes hold :func:`_pass_lock` from the marker check to the marker write. No Notes
    backend writes before the seal: the backend spawn refuses to launch until it exists.
    A PAT with a second hard link (:class:`SecondNameError`) also stops the pass unsealed. The marker write itself raises, so a pass that cannot seal
    runs again on the next spawn. Returns the destinations it populated.
    """
    moved: list[str] = []
    marker = state_dir / MIGRATION_MARKER
    if marker.exists():
        # Sealed: one stat, no lock.
        return moved
    with _pass_lock(state_dir):
        if marker.exists():
            return moved
        try:
            refuse_linked_parent(legacy_dir / STATE_FILE_NAMES[0])
        except OSError:
            # Seal without moving: an agent can swap a link in that chain for a real
            # directory of its own, so nothing behind it may ever be promoted.
            logger.warning(
                "not moving legacy Notes state: %s is reached through a link, so its files "
                "cannot be trusted; they stay where they are, and the user enters them again",
                legacy_dir,
            )
            atomic_write(
                marker, "{}\n", restrict_to_owner=True, restrict_on_error="warn", fsync=True
            )
            return moved
        failed: list[str] = []
        for name in STATE_FILE_NAMES:
            src = legacy_dir / name
            dst = state_dir / name
            try:
                data = _read_regular_no_follow(src, sole_link=name == "pat")
                if data is None or data == ABSENT_EQUIVALENT[name]:
                    continue
                if not _is_stub(name, dst):
                    # A real destination wins; the legacy copy stays masked and fenced.
                    continue
                atomic_write(
                    dst, data, restrict_to_owner=True, restrict_on_error="warn", fsync=True
                )
            except SecondNameError:
                # Unsealed on purpose: once the extra name is gone the next spawn moves it.
                raise
            except OSError:
                logger.warning("could not move legacy Notes state %s", name, exc_info=True)
                failed.append(name)
                continue
            moved.append(str(dst))
        if moved:
            logger.info("moved %d legacy Notes state file(s) into %s", len(moved), state_dir)
            try:
                fsync_dir(state_dir)
            except OSError:
                logger.warning(
                    "could not sync %s; keeping the legacy Notes PAT", state_dir, exc_info=True
                )
            else:
                if str(state_dir / "pat") in moved:
                    try:
                        _empty_in_place(legacy_dir / "pat")
                    except OSError:
                        logger.warning("could not empty the legacy Notes PAT", exc_info=True)
        if failed:
            # Unsealed, so the next spawn copies them; until then Notes does not start.
            raise OSError(f"could not copy legacy Notes state {', '.join(failed)}")
        atomic_write(marker, "{}\n", restrict_to_owner=True, restrict_on_error="warn", fsync=True)
    return moved


def move_state_to_app_data(app_state_dir: Path, state_dir: Path) -> list[str]:
    """Copy the state in *state_dir* into *app_state_dir*, in one pass, once.

    Runs in the gateway process, which sees the real data home with no masks, and only
    after :func:`migrate_legacy_state` has sealed *state_dir* (it raises otherwise), so no
    older copy is still on its way in. *state_dir* is the source of truth until this pass
    seals: for each state file it reads the copy there ONCE (regular file, last component
    not followed; a PAT with a second hard link raises :class:`SecondNameError`) and writes
    those bytes, or the file's absent-equivalent when there is none, over whatever
    *app_state_dir* holds, with ``atomic_write(restrict_to_owner=True, fsync=True)``. So a
    file planted there by an older build's agent, before the directory was masked, is
    replaced, never trusted.

    Then *app_state_dir* and every directory above it are synced and :data:`APP_MOVE_MARKER`
    is written in *state_dir*, which no agent can write, and only after that seal is synced
    too is the old PAT emptied in place (a
    leftover copy would keep a token the user may revoke at a second name). Every return,
    a sealed pass's included, first syncs *state_dir*, so the marker's name is durable
    before the caller treats the state as final and the backend writes over the copies; a
    failed sync raises, and the backend does not start on that pass. In that order
    a pass that stops between the two leaves the source PAT whole, so no later pass can
    copy an emptied PAT over the moved one; a sealed pass that finds the old PAT still
    holding bytes empties it, and a failure to empty it is logged and retried by the next
    pass. The registry and settings hold no secret and stay where they were, so a rollback
    to the previous build loses only the PAT, as the earlier move did. Any failure before
    the seal stops the pass UNSEALED (``OSError``), so the next pass copies again; until
    then the shipped Notes backend does not start. Its one caller,
    :func:`sealed_default_legacy_state`, runs it only while no sandbox of an earlier build
    is alive, since such a sandbox masks *state_dir* but not *app_state_dir*. Passes hold
    :func:`_pass_lock` on *state_dir*. Returns the destinations it wrote.
    """
    written: list[str] = []
    marker = state_dir / APP_MOVE_MARKER
    if marker.exists() and _old_pat_is_empty(state_dir):
        fsync_dir(state_dir)
        return written
    with _pass_lock(state_dir):
        if marker.exists():
            fsync_dir(state_dir)
            _empty_moved_pat(app_state_dir, state_dir)
            return written
        if not (state_dir / MIGRATION_MARKER).is_file():
            raise OSError(f"{state_dir} is not sealed yet, so its state is not final")
        # The planted-link refusal ``atomic_write(restrict_to_owner=True)`` implies, checked
        # BEFORE any mkdir, which would walk through a planted link.
        refuse_linked_parent(app_state_dir / STATE_FILE_NAMES[0])
        if not _make_state_dir(app_state_dir):
            raise OSError(
                f"{app_state_dir.parent} is moved aside by an install, update or uninstall"
            )
        for name in STATE_FILE_NAMES:
            data = _read_regular_no_follow(state_dir / name, sole_link=name == "pat")
            atomic_write(
                app_state_dir / name,
                ABSENT_EQUIVALENT[name] if data is None else data,
                restrict_to_owner=True,
                restrict_on_error="warn",
                fsync=True,
            )
            written.append(str(app_state_dir / name))
        _sync_destination(app_state_dir, state_dir)
        atomic_write(marker, "{}\n", restrict_to_owner=True, restrict_on_error="warn", fsync=True)
        fsync_dir(state_dir)
        logger.info("copied the Notes state from %s into %s", state_dir, app_state_dir)
        _empty_moved_pat(app_state_dir, state_dir)
    return written


def ensure_app_state_dir(app_state_dir: Path) -> None:
    """Create *app_state_dir*, empty and owner-only, so every sandbox started now masks it.

    A sandbox masks a declared directory only when it exists at the spawn, so spawn prep
    calls this before every spawn until the move seals: no sandbox of this build then
    starts without the mask that :func:`move_state_to_app_data` later copies the token
    under. Nothing is copied here. Refuses a link anywhere in the chain, checked before
    the ``mkdir``, which would walk through one. While the app's lifecycle holds its
    ``data/`` aside it creates nothing (:func:`_make_state_dir`); the sandbox's own spawn
    prep creates ``state/`` inside the moved-aside tree then.
    """
    refuse_linked_parent(app_state_dir / STATE_FILE_NAMES[0])
    _make_state_dir(app_state_dir)


def _make_state_dir(app_state_dir: Path) -> bool:
    """Create *app_state_dir* owner-only, never making a ``data/`` the lifecycle holds aside.

    An install, update or keep-data uninstall moves ``apps/<name>/data/`` to
    ``apps/.<name>-data-tmp/`` and back by rename, so a ``data/`` made in between would
    turn the move back into a move INTO it, nesting the kept state where nothing masks
    it. So with ``data/`` present only ``state/`` is made in it; with ``data/`` absent the
    chain is made only when no moved-aside tree exists. ``data/`` is looked at first: a
    move aside after that look leaves the ``mkdir`` of ``state/`` nothing to land in.
    False when the lifecycle holds the data, so nothing was made.
    """
    data = app_state_dir.parent
    if data.is_dir():
        try:
            os.mkdir(app_state_dir, 0o700)
        except FileExistsError:
            if not app_state_dir.is_dir():
                raise
        except FileNotFoundError:
            return False
        return True
    if os.path.lexists(data.parent.with_name(f".{data.parent.name}{STAGED_DATA_SUFFIX}")):
        return False
    mkdirs_owner_only(app_state_dir)
    return True


def _sync_destination(app_state_dir: Path, state_dir: Path) -> None:
    """Sync *app_state_dir* and every directory above it up to the data home.

    The copied files are synced as they are written; this makes the names that reach
    them durable too, including a directory the pass just created, so a power loss after
    the old PAT is emptied cannot come back to a destination that was never recorded.
    The data home is *state_dir*'s parent; a destination outside it syncs only itself.
    """
    home = state_dir.parent
    cursor = app_state_dir
    while True:
        fsync_dir(cursor)
        if cursor == home or home not in cursor.parents:
            return
        cursor = cursor.parent


def _old_pat_is_empty(state_dir: Path) -> bool:
    """Whether *state_dir*'s PAT is absent or a zero-byte regular file (one ``lstat``)."""
    try:
        st = os.lstat(state_dir / "pat")
    except FileNotFoundError:
        return True
    return stat.S_ISREG(st.st_mode) and st.st_size == 0


def _empty_moved_pat(app_state_dir: Path, state_dir: Path) -> None:
    """Empty *state_dir*'s PAT once its copy has sealed; a failure waits for the next pass.

    Called only once :data:`APP_MOVE_MARKER` is written and its name synced: the moved copy
    is final, and a sealed pass never copies again, so emptying the source cannot reach it.
    The copy's names are synced first too, on every pass that empties: a power loss that
    kept the emptied PAT but lost them would otherwise lose the token.
    """
    try:
        if _read_regular_no_follow(state_dir / "pat"):
            _sync_destination(app_state_dir, state_dir)
            _empty_in_place(state_dir / "pat")
    except OSError:
        logger.warning(
            "could not empty the moved Notes PAT in %s; will retry", state_dir, exc_info=True
        )


def older_sandboxes_masking_only_the_old_dir() -> list[int] | None:
    """Live sandboxes that mask :data:`STATE_DIR_LEAF` but not :data:`APP_STATE_PARTS`.

    Such a sandbox was started by a build that predates the app's own state directory,
    and it would see the token once :func:`move_state_to_app_data` copied it there. See
    :mod:`kiro_crew.older_sandboxes`; ``None`` means the process table could not be read.
    """
    # Deferred: the Notes backend imports this module for its names, never calls this, and
    # must not load the process-table helpers.
    from kiro_crew.older_sandboxes import sandboxes_masking_only

    return sandboxes_masking_only(_crew_home(), STATE_DIR_LEAF, "/".join(APP_STATE_PARTS))


def sealed_default_legacy_state() -> bool:
    """Run both moves on the live data home; whether the state is now final in the app dir.

    :func:`migrate_legacy_state` first, then :func:`move_state_to_app_data`, which waits
    while :func:`older_sandboxes_masking_only_the_old_dir` names any process (or cannot
    read the process table): until those end, the token stays where every running sandbox
    masks it, and Notes stays off. Called by the gateway before the shipped Notes backend
    spawns, which refuses to start until this is true, so every later start retries. Never
    raises: a failure is logged, and the next start tries again. True only when
    :func:`move_state_to_app_data` returned, so the marker's name is synced; a marker that
    merely exists does not start the backend.
    """
    state_dir = default_state_dir()
    try:
        migrate_legacy_state(state_dir, default_legacy_dir())
        if not (state_dir / APP_MOVE_MARKER).is_file():
            older = older_sandboxes_masking_only_the_old_dir()
            if older is None or older:
                who = (
                    "the process table could not be read"
                    if older is None
                    else "pid " + ", ".join(map(str, older))
                )
                logger.warning(
                    "not moving the Notes state into %s yet: a sandbox started by an earlier "
                    "build may still run (%s), and its masks do not cover that directory. "
                    "Notes starts once it has ended; every start retries",
                    default_app_state_dir(),
                    who,
                )
                return False
        move_state_to_app_data(default_app_state_dir(), state_dir)
    except Exception:
        logger.warning("Notes state move did not finish; will retry", exc_info=True)
        return False
    return True
