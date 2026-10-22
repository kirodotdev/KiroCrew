"""Where the Notes builtin keeps its state, and its one-time move out of the old layout.

The Notes (md-notebook) builtin keeps three state files: ``pat`` (a GitHub token),
``vaults.json`` (the registry the auto-sync loop trusts as its push target) and
``settings.json`` (``autoSync``, the bit that authorizes that push). They live INSIDE the
top-level ``<crew-home>/md-notebook-staging/`` directory, which the OS sandbox masks as one
directory and hands back to the Notes backend alone. A writer stages its temp in that same
directory and renames it onto a name in it, so the publish never crosses a mount point.

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


def sealed_default_legacy_state() -> bool:
    """Run :func:`migrate_legacy_state` on the live data home; whether it is now sealed.

    Called by the gateway before the Notes backend spawns, which refuses to start until
    this is true. Never raises: a failure is logged, and the next start tries again.
    """
    state_dir = default_state_dir()
    try:
        migrate_legacy_state(state_dir, default_legacy_dir())
    except Exception:
        logger.warning("Notes legacy state move did not finish; will retry", exc_info=True)
    return (state_dir / MIGRATION_MARKER).is_file()
