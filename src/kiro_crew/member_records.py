"""Where the gateway keeps each crewmate's governing records, and their one-time move.

Three record kinds govern a crewmate and are written only by the gateway, on a person's
action: its permanent rules (``member-rules/``), its goals (``member-goals/``) and its DM
thread binding (``member-bindings/``), one ``<slug>.json`` each. They live in the
top-level ``<data home>/member-records/`` directory, which the OS sandbox masks in every
mode (``sandbox_leaves._CREW_HIDDEN_LEAVES``) and the agent file tools refuse
(``security.paths_lists._CREW_SECRET_LEAVES``). Nothing that runs inside a sandbox reads
or writes them, so a sandboxed process can neither read nor change them.

An older build kept the same files under ``<data home>/trust/<kind>/``. ``trust/`` stays
read-write inside the sandbox for the SEL key and log, which is why the records moved
out. :func:`migrate_from_trust` moves them once; :func:`ensure_migrated` runs that move
from every process outside the sandbox before a record path is used, and spawn prep runs
it before an agent of this build starts.

This module imports nothing from the member or sandbox modules, so both can call it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.config.paths import data_home
from kiro_crew.platform_lock_compat import acquire_lock, open_create_or_existing, release_lock

logger = logging.getLogger(__name__)

#: The records directory, a direct child of the data home. Byte-identical to its entry
#: in ``sandbox_leaves._CREW_HIDDEN_LEAVES``; a test pins the two.
MEMBER_RECORDS_LEAF = "member-records"

#: The record kinds, each a subdirectory of :data:`MEMBER_RECORDS_LEAF` and, in the old
#: layout, of ``trust/``. Byte-identical to ``member_space.RULES_DIR_NAME``,
#: ``member_goals.GOALS_DIR_NAME`` and ``member_space.DM_BINDINGS_DIR_NAME``; a test pins
#: them.
RECORD_KINDS: tuple[str, ...] = ("member-rules", "member-goals", "member-bindings")

#: Where the old layout kept the kinds, relative to the data home.
LEGACY_PARENT = "trust"

#: Written in the records directory when the move's one pass ends. Its existence closes
#: the move for good, so a file written under ``trust/`` later is never promoted.
MIGRATION_MARKER = ".moved-from-trust"

#: Held by a pass from its marker check to its marker write.
LOCK_NAME = ".moving"

#: Set by the sandbox launcher in every child (``sandbox_tiers._IN_SANDBOX_MARKER``).
#: Spelled here so this module needs no sandbox import; a test pins the two.
_IN_SANDBOX_MARKER = "KIROCREW_SANDBOX_ACTIVE"

_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: int = getattr(os, "O_NONBLOCK", 0)


def records_root() -> Path:
    """``<data home>/member-records``, resolved from the data home on every call."""
    return data_home().resolve() / MEMBER_RECORDS_LEAF


class RecordsNotMoved(OSError):
    """The one-time move has not finished and *kind* still has records in the old layout.

    Raised instead of handing back the new directory, because a record there would
    read as absent while its real copy is still under ``trust/``: a crewmate's rules
    would be silently dropped from its prompt. Callers treat it as an unreadable
    record, never as "never set".
    """


def records_dir(kind: str) -> Path:
    """The directory holding one record kind, after the one-time move has had its chance.

    Lexical: the caller containment-checks the record path it builds under it. Raises
    :class:`RecordsNotMoved` when the move did not seal and the old layout still holds
    a record of this kind, so no reader takes a not-yet-moved record for an absent one.
    """
    if kind not in RECORD_KINDS:
        raise ValueError(f"unknown member record kind {kind!r}")
    if not ensure_migrated() and not _inside_sandbox() and _legacy_records_remain(kind):
        raise RecordsNotMoved(
            f"member {kind} records are still in the old layout and could not be moved; "
            "the move is retried on the next use"
        )
    return records_root() / kind


def _legacy_records_remain(kind: str) -> bool:
    """Whether the old layout still holds a record of *kind*, movable or not."""
    try:
        src_dir, blocked = _legacy_state(data_home().resolve() / LEGACY_PARENT, kind)
        return blocked or (src_dir is not None and bool(_record_names(src_dir)))
    except (OSError, RuntimeError, ValueError):
        # Unreadable is not proof of absence: refuse rather than read an empty layout.
        return True


def outstanding_kinds() -> list[str]:
    """The record kinds whose old layout still holds a record, in :data:`RECORD_KINDS` order.

    A refusal names these kinds. Readers refuse a kind it lists; spawn prep refuses
    every launch while the move is unsealed, listed kinds or not.
    """
    return [kind for kind in RECORD_KINDS if _legacy_records_remain(kind)]


#: Why the latest pass in this process did not seal, for the spawn refusal's message.
last_failure: str = ""


def _inside_sandbox() -> bool:
    return bool(os.environ.get(_IN_SANDBOX_MARKER))


def ensure_migrated() -> bool:
    """Run :func:`migrate_from_trust` on the live data home; whether the move is sealed.

    One ``stat`` once sealed. Never raises: a failed pass is logged, its source files
    stay where they are, and the next call tries again. Does nothing inside a sandbox,
    where the records directory is a masked stand-in and a move would land nowhere.
    """
    if _inside_sandbox():
        return False
    try:
        home = data_home().resolve()
    except Exception:  # noqa: BLE001 - the data home is unresolvable; nothing to move
        logger.debug("could not resolve the data home for the member records move", exc_info=True)
        return False
    root = home / MEMBER_RECORDS_LEAF
    if (root / MIGRATION_MARKER).is_file():
        return True
    if not home.is_dir():
        return False
    try:
        migrate_from_trust(root, home / LEGACY_PARENT)
    except Exception as exc:  # noqa: BLE001 - logged; the next call retries
        global last_failure
        last_failure = str(exc) or type(exc).__name__
        logger.warning(
            "the one-time move of member rules, goals and DM bindings into %s did not "
            "finish; their old copies are kept and the move is retried",
            root,
            exc_info=True,
        )
        return False
    return (root / MIGRATION_MARKER).is_file()


def _read_regular_no_follow(path: Path) -> bytes | None:
    """The bytes of *path* if it is a regular file reached without following its last link.

    ``None`` when absent, a link or not a regular file. Any other failure raises, so a
    pass never seals over a record it could not read.
    """
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except FileNotFoundError:
        return None
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _plain_dir(path: Path, *, create: bool) -> bool:
    """Whether *path* is a real directory (never a link), creating it at 0700 if asked."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a plain directory")
    return True


@contextlib.contextmanager
def _pass_lock(root: Path) -> Iterator[None]:
    """Serialize whole passes across threads and processes (marker check to marker write)."""
    fd = open_create_or_existing(root / LOCK_NAME, os.O_RDWR | _O_NOFOLLOW, 0o600)
    try:
        acquire_lock(fd, exclusive=True)
        try:
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _record_names(directory: Path) -> list[str]:
    """The ``<slug>.json`` names in *directory*, sorted; ``[]`` when there is no directory.

    Lists through a link when *directory* is reached through one: the listing decides
    only whether records exist, never what is promoted.
    """
    try:
        names = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(n for n in names if n.endswith(".json") and not n.startswith("."))


def _legacy_state(trust: Path, kind: str) -> tuple[Path | None, bool]:
    """Where to move *kind* from, and whether its old records cannot be moved safely.

    ``(trust/<kind>, False)`` when both are real directories. A link at either is never
    followed for a move, because nothing reached through one may be promoted; but the old
    build did read through it, so records behind it are the crewmate's real records and
    the answer is ``(None, True)``: the move does not seal, the readers refuse, and the
    operator replaces the link with the directory it points to. ``(None, False)`` when
    there is nothing there (absent, a plain file, or a link that leads to no records).
    """
    linked = False
    for path in (trust, trust / kind):
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None, False
        if stat.S_ISLNK(st.st_mode):
            linked = True
        elif not stat.S_ISDIR(st.st_mode):
            return None, False
    if linked:
        return None, bool(_record_names(trust / kind))
    return trust / kind, False


def migrate_from_trust(root: Path, trust: Path) -> list[str]:
    """Move every record from ``trust/<kind>/`` into ``root/<kind>/``, in one pass, once.

    Runs outside the sandbox, which sees the real data home with no masks. Each
    ``<slug>.json`` that is a regular file (last component not followed, parent not a
    link) is read ONCE and those bytes are written to the new name with
    ``atomic_write(fsync=True)``. Only after every copy is written and its directory, the
    records directory and the data home that names it are synced is any old file
    removed, so a crash at any point leaves at least one complete copy and the next pass
    finishes the job.

    A new name that already holds the same bytes is the trace of an earlier pass that
    stopped after its copy; its old file is removed now. A new name that holds different
    bytes wins and the old file is left in place, logged, so no bytes are lost.

    Ends by writing :data:`MIGRATION_MARKER`, whether or not anything moved. A copy that
    fails raises before any removal or the marker, so the pass runs again. So does an old
    record the pass will not promote -- one reached through a link at ``trust`` or
    ``trust/<kind>``, or a ``<slug>.json`` that is not a regular file -- because the old
    build read those, and sealing over them would turn a crewmate's real rules into none.
    Returns the destinations it wrote.
    """
    moved: list[str] = []
    marker = root / MIGRATION_MARKER
    if marker.is_file():
        return moved
    _plain_dir(root, create=True)
    with _pass_lock(root):
        if marker.is_file():
            return moved
        removable: list[Path] = []
        touched: list[Path] = []
        refused: list[str] = []
        for kind in RECORD_KINDS:
            src_dir, blocked = _legacy_state(trust, kind)
            if blocked:
                refused.append(
                    f"{trust / kind} is reached through a link; replace the link with the "
                    "directory it points to"
                )
            if src_dir is None:
                continue
            dst_dir = root / kind
            _plain_dir(dst_dir, create=True)
            for name in _record_names(src_dir):
                src = src_dir / name
                dst = dst_dir / name
                data = _read_regular_no_follow(src)
                if data is None:
                    if os.path.lexists(src):
                        refused.append(f"{src} is not a regular file; replace it with one")
                    continue
                if os.path.lexists(dst):
                    if _read_regular_no_follow(dst) == data:
                        removable.append(src)
                    else:
                        logger.warning("kept %s: %s already holds a different record", src, dst)
                    continue
                atomic_write(
                    dst, data, restrict_to_owner=True, restrict_on_error="warn", fsync=True
                )
                moved.append(str(dst))
                removable.append(src)
            fsync_dir(dst_dir)
            touched.append(src_dir)
        if refused:
            # Unsealed and nothing removed: the readers refuse these kinds, spawn prep
            # refuses to launch, and the next pass after the operator's fix moves them.
            raise OSError("; ".join(refused))
        fsync_dir(root)
        # The data home's entry naming the records directory: this pass may have just
        # created it, and a crash that keeps the removals below but loses that name
        # would leave no copy anywhere. Raises, so nothing is removed unless it holds.
        fsync_dir(root.parent)
        for src in removable:
            try:
                os.unlink(src)
            except FileNotFoundError:
                pass
        for src_dir in touched:
            fsync_dir(src_dir, best_effort=True)
            with contextlib.suppress(OSError):
                os.rmdir(src_dir)
        if touched:
            fsync_dir(trust, best_effort=True)
        atomic_write(marker, "{}\n", restrict_to_owner=True, restrict_on_error="warn", fsync=True)
        fsync_dir(root)
    if moved:
        logger.info("moved %d member record(s) into %s", len(moved), root)
    return moved
