"""Which ``config.json`` / ``config.local.json`` objects THIS gateway published.

The Linux namespace launcher seals both files read-only and pins each one against the
identity the pre-spawn pass recorded. Every gateway config write publishes through
``atomic_write`` (temp + rename), so the file at the name is a NEW inode after each
save, and a save landing between the pass and the pin made an unrelated spawn refuse
with "a DIFFERENT object holds that name". The gateway saves these files on its own --
the boot meta-stamp refresh, load-time migrations, first-run seeding, settings writes
-- so on a freshly provisioned host, where those writes and the first sandboxed
spawns (the MCP probe fan-out, app registry clones) run in the same minutes, the
refusal was a matter of timing rather than of anything hostile.

This module is the gateway's answer to the one question the launcher cannot settle on
its own: is the object now at the name one the gateway itself wrote? Each config write
records the ``(dev, ino)`` of the staged file it is about to publish, read off the
writer's own descriptor before the rename, in a small record inside the
``config-provenance`` directory. Reading the descriptor rather than the name binds the
entry to the object the gateway made: a stat of the name after the rename could meet
a file someone else put there in between. The gateway keeps a descriptor open on each
identity the record lists, so the inode cannot be freed and handed to another file
while the record still vouches for its number. That directory is a no-follow read-only leaf of every
sandbox (``sandbox_leaves._CREW_READONLY_LEAVES``), so no sandboxed process can write
the record or rename the directory, and the gateway's own writes inside it leave the
directory's inode -- the thing its seal pins -- unchanged. The launcher accepts a
changed occupant at one of these two names ONLY when the pinned object's identity is in
the record and the record carries this gateway process's nonce. Anything else -- a
rewrite the gateway did not make, a swap after a gateway write, a record left by an
earlier process -- is refused exactly as before. ``docs/system-specs/modules/security.md``
lists this as the one recreation the pin admits, and why.

A record that cannot be written is removed instead, which leaves the launcher with no
vouching entry: the refusal the pin always made. Only when it can be neither written
nor removed does the config save itself fail, rather than leave an older file vouched.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger("kiro_crew.config.loader")

#: The data-home directory that holds the record. Sealed read-only, no-follow and
#: pre-created in every sandbox; see this module's docstring.
RECORD_DIR_LEAF = "config-provenance"
#: The record file inside it. Plain text, so the launcher needs no import to read it:
#: the first line is the nonce, each further line
#: ``<leaf> <replaced dev> <replaced ino> <published dev> <published ino>``.
RECORD_FILE = "published"
#: The leaves whose gateway writes are vouched for. The two sealed runtime-config files.
PUBLISHED_LEAVES: tuple[str, ...] = ("config.json", "config.local.json")
#: Fresh per gateway process. The launcher is told it in its plan and accepts only a
#: record that carries it, so a record written by an earlier process, or planted by a
#: sandbox of a build that did not yet seal this directory, vouches for nothing.
_NONCE = secrets.token_hex(16)

_LOCK = threading.Lock()
#: Per leaf, the LATEST save: the identity of the file it replaced, the identity it
#: published, and the descriptors held on both. The launcher admits the new file only
#: for a spawn whose pass saw the replaced one, so an older file put back at the name, or
#: a save by another process in between, is not vouched for. The descriptors keep both
#: inodes allocated, so neither number can be reused by a file the gateway did not write
#: while the record lists it.
_Entry = tuple[tuple[int, int], tuple[int, int], tuple[int, ...]]
_PUBLISHED: dict[str, _Entry] = {}


def record_path() -> Path:
    """Where the record lives: ``<data home>/config-provenance/published``."""
    return config_dir() / RECORD_DIR_LEAF / RECORD_FILE


def launcher_record() -> tuple[str, str, tuple[str, ...]]:
    """``(record path, nonce, names)`` for the launcher's plan.

    *names* are the two config files under the data home, the only targets whose
    changed occupant the launcher will look the record up for.
    """
    home = config_dir()
    return (
        str(home / RECORD_DIR_LEAF / RECORD_FILE),
        _NONCE,
        tuple(str(home / leaf) for leaf in PUBLISHED_LEAVES),
    )


def _render(published: dict[str, _Entry]) -> str:
    lines = [_NONCE]
    for leaf in PUBLISHED_LEAVES:
        if leaf in published:
            (pdev, pino), (dev, ino), _held = published[leaf]
            lines.append(f"{leaf} {pdev} {pino} {dev} {ino}")
    return "\n".join(lines) + "\n"


def staging_hook(name: Path) -> Callable[[int], None] | None:
    """An ``atomic_write`` *on_staged* callback recording the file it is about to publish.

    *name* is the path being written. It is recorded only when it is one of
    :data:`PUBLISHED_LEAVES` under the data home, or the file such a name resolves to
    when that name is a symlink (the gateway writes a symlinked config through to its
    referent, and the sandbox seals the name). For any other path this returns ``None``.
    """
    try:
        target = name.resolve()
        home = config_dir()
        leaf = next((each for each in PUBLISHED_LEAVES if (home / each).resolve() == target), None)
    except OSError:
        return None
    if leaf is None:
        return None

    def _record(fd: int) -> None:
        # Before the rename, so this is the file the save is about to replace. Opened, not
        # only statted: the descriptor is what keeps its inode number from being reused.
        try:
            prev: int | None = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            prev = None
        try:
            _note_staged(leaf, fd, prev)
        finally:
            if prev is not None:
                os.close(prev)

    return _record


def _note_staged(leaf: str, fd: int, prev: int | None) -> None:
    """Record the staged file open at *fd* as the one about to be published at *leaf*.

    The record on disk never lists an older file once a newer one is about to take the
    name: when the new record cannot be written, the record is removed before the save
    goes ahead, which leaves the pin refusing exactly as it did before the record
    existed. When it cannot be removed either, this raises, and ``atomic_write`` then
    abandons the config save rather than publish a file the record does not describe.
    *prev* is a descriptor on the file being replaced, or ``None`` when there is none.
    """
    with _LOCK:
        try:
            _record_staged(leaf, fd, prev)
        except Exception:
            logger.debug("config provenance: could not record a config write", exc_info=True)
            _withdraw()


def _record_staged(leaf: str, fd: int, prev: int | None) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("the staged config is not a regular file")
    ident = (info.st_dev, info.st_ino)
    current = _PUBLISHED.get(leaf)
    if current is not None and current[1] == ident:
        return
    directory = config_dir() / RECORD_DIR_LEAF
    if prev is None:
        replaced = (0, 0)
    else:
        pinfo = os.fstat(prev)
        replaced = (pinfo.st_dev, pinfo.st_ino)
    held = (os.dup(fd),) if prev is None else (os.dup(fd), os.dup(prev))
    try:
        try:
            entry = os.lstat(directory)
        except FileNotFoundError:
            directory.mkdir(mode=0o700, exist_ok=True)
            entry = os.lstat(directory)
        if not stat.S_ISDIR(entry.st_mode):
            # A link or a file at the name is not a place the launcher will read (it
            # opens the directory no-follow), and writing through a link would put the
            # record somewhere else entirely.
            raise NotADirectoryError(str(directory))
        atomic_write(
            directory / RECORD_FILE,
            _render({**_PUBLISHED, leaf: (replaced, ident, held)}),
            mode=0o600,
        )
    except BaseException:
        _close(held)
        raise
    _PUBLISHED[leaf] = (replaced, ident, held)
    if current is not None:
        _close(current[2])


def _close(fds: tuple[int, ...]) -> None:
    for each in fds:
        os.close(each)


def _withdraw() -> None:
    """Remove the record so it vouches for nothing; raise if it cannot be removed."""
    try:
        os.unlink(record_path())
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning(
            "config provenance: could not remove a stale record; refusing the config save",
            exc_info=True,
        )
        raise
    for _replaced, _ident, held in _PUBLISHED.values():
        _close(held)
    _PUBLISHED.clear()
