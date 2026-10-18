"""A session's crew logs in the trash: staging them, putting them back, and their holds.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

A session's crew-log units are staged inside the crew-log tree, never in the
agent-writable batch directory (see :func:`_stage_crew_logs`). This owner stages and
returns them, records and releases the holds a failed restore leaves, finds the staged
units no manifest lists (so nothing that removes the batch removes them), and discards a
batch's crew-log staging once an empty has removed the batch.
"""

from __future__ import annotations

import os
import shutil
import stat
from contextlib import suppress
from pathlib import Path
from typing import IO

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.session_storage_parts.batch_manifest import (
    _append_entry,
    _read_manifest,
    _sync_batch,
)
from kiro_crew.session_storage_parts.store_layout import (
    _UNIT_ID_RE,
    SessionStorageError,
    _tree_bytes,
    logger,
)

#: Lists, one per line, the units a failed restore left live and held for a session.
_CREW_HOLDS_FILE = "held"


def _crew_staging(batch_id: str, uid: str = "") -> Path:
    """Where a batch (or one of its sessions) stages crew logs, inside the crew-log tree.

    *uid* reaches here from a manifest, which is agent-writable, so it must be a bare
    unit id before it becomes a path segment: a ``..`` would otherwise step out of the
    hidden tree and let a restore publish an agent-written directory as a crew log.
    """
    from kiro_crew.crew_log.store import crew_log_trash_root

    if uid and not _UNIT_ID_RE.match(uid):
        raise SessionStorageError(f"not a valid session id: {uid!r}")
    base = crew_log_trash_root() / batch_id
    return base / uid if uid else base


def _stage_crew_logs(batch_id: str, uid: str, unit_ids: tuple[str, ...]) -> tuple[bool, int]:
    """Stage every crew-log unit of *uid* for *batch_id*: ``(all moved, bytes staged)``.

    Not into the batch directory: that tree is agent-writable, the crew-log tree is
    hidden from the sandbox, and a restore publishes staged files back into the live
    tree -- so a crew log waiting in the batch could be edited into entries the
    gateway later reads as its own. Each unit is renamed under its sole lease
    (:func:`kiro_crew.crew_log.store.stage_unit`).

    All or nothing, like the session's other halves: a unit a writer still holds
    means the session is in use, so the units already staged are put back and the
    caller leaves the whole session where it is. A unit already gone is not a
    failure -- there is nothing of it left to split.
    """
    if not unit_ids:
        return True, 0
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    base = _crew_staging(batch_id, uid)
    staged = 0
    for unit_id in unit_ids:
        directory = crew_store.unit_dir_for(KIND_SESSION, unit_id)
        if directory is None:
            continue
        size = _tree_bytes(directory)
        try:
            status = crew_store.stage_unit(KIND_SESSION, unit_id, base / directory.name)
        except Exception:
            logger.warning("could not stage crew log %r of %r", unit_id, uid, exc_info=True)
            status = crew_store.REMOVE_FAILED
        if status == crew_store.REMOVE_REMOVED:
            staged += size
        elif status != crew_store.REMOVE_ABSENT:
            logger.warning(
                "crew log %r of %r could not move (%s); leaving it", unit_id, uid, status
            )
            return False, 0
    return True, staged


def _put_back_crew_logs(manifest: IO[str], batch_id: str, uid: str) -> bool:
    """Return *uid*'s staged crew logs to the live tree; False when some had to stay.

    A unit stays staged when its name was taken while it was away -- a resume that
    recreated it. The staged copy is then the only one of that earlier history, so it
    is recorded in the manifest as a crew-log-only entry: listed, restorable, and
    never removed except by an empty the user asks for. The caller reports it rather
    than calling the session left in place.

    The units that went back are live again with the whole session, so their restore
    holds are released here, as a committed restore releases them.
    """
    restored = _restore_crew_logs(batch_id, uid)
    if restored is not None:
        _release_restored_holds(restored)
        return True
    try:
        _append_entry(manifest, {"uid": uid, "files": []})
    except OSError:
        logger.error(
            "could not record the staged crew logs of %r; they are in %s",
            uid,
            _crew_staging(batch_id, uid),
            exc_info=True,
        )
    return False


def _sync_batch_or_warn(
    target: Path, staged_dirs: set[Path], manifest: IO[str], source_dirs: set[Path]
) -> None:
    """:func:`_sync_batch` on a path that is already failing: a sync error is logged.

    The refusal that follows names where the staged units are, which is the pointer an
    operator needs; replacing it with an ``OSError`` would lose that.
    """
    try:
        _sync_batch(target, staged_dirs, manifest, source_dirs)
    except OSError:
        logger.warning("could not sync %s before refusing", target, exc_info=True)


def _crew_logs_left_staged(uid: str, target: Path) -> SessionStorageError:
    """The refusal for a session whose crew logs could not all be put back."""
    return SessionStorageError(
        f"session {uid!r} changed while being staged and some of its crew logs could "
        f"not be put back; they are recorded in {target} and can be restored"
    )


def _staged_crew_dirs(batch_id: str, uid: str) -> list[Path]:
    """The crew-log units staged for *uid* in *batch_id*, links never followed.

    Empty for a *uid* that is not a bare unit id -- such a manifest entry names no
    staging this module wrote -- and when no staging exists for it. A staging that
    exists but cannot be listed raises ``OSError``: an unreadable directory may hold
    the only copy of a session's crew logs, so no caller may read it as empty.
    """
    if not _UNIT_ID_RE.match(uid):
        return []
    base = _crew_staging(batch_id, uid)
    try:
        info = base.lstat()
    except FileNotFoundError:
        return []
    if platform_compat.is_link_or_junction(base) or not stat.S_ISDIR(info.st_mode):
        return []
    return sorted(
        child
        for child in base.iterdir()
        if child.is_dir() and not platform_compat.is_link_or_junction(child)
    )


def _has_staged_crew(batch_id: str, uid: str) -> bool:
    """Whether *uid* has crew logs staged in *batch_id*; an unreadable staging counts."""
    try:
        return bool(_staged_crew_dirs(batch_id, uid))
    except OSError:
        logger.warning(
            "cannot list the crew-log staging of %r in %r; treating it as holding units",
            uid,
            batch_id,
            exc_info=True,
        )
        return True


def _restore_crew_logs(batch_id: str, uid: str) -> list[tuple[str, Path]] | None:
    """Put back every crew-log unit staged for *uid*: what went back, or None.

    All or nothing: when one cannot go back -- its name is occupied by a unit created
    since, which is newer -- the ones already restored are staged again and None is
    returned, so the caller keeps the session in the batch with every half together.

    Each unit is HELD before it is published, so a retention sweep running before the
    rest of the session is back cannot expire it. The caller releases the holds with
    :func:`_release_restored_holds` once the whole session has been restored.
    """
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    restored: list[tuple[str, Path]] = []
    if not _UNIT_ID_RE.match(uid):
        return restored
    try:
        staged_units = _staged_crew_dirs(batch_id, uid)
    except OSError:
        # Not knowing what is staged is not "nothing is staged": committing the rest
        # of the session would leave its crew logs behind, unlisted and unrestorable.
        logger.warning(
            "cannot list the crew-log staging of %r; leaving the session staged",
            uid,
            exc_info=True,
        )
        return None
    for staged in staged_units:
        held = crew_store.hold_staged_unit(staged)
        unit_id = crew_store.restore_staged_unit(KIND_SESSION, staged) if held else None
        if unit_id is None:
            logger.warning("crew log %r of %r could not be restored", staged.name, uid)
            _return_crew_logs(batch_id, uid, restored)
            return None
        restored.append((unit_id, staged))
    for directory in (_crew_staging(batch_id, uid), _crew_staging(batch_id)):
        with suppress(OSError):
            directory.rmdir()
    return restored


def _return_crew_logs(batch_id: str, uid: str, restored: list[tuple[str, Path]]) -> None:
    """Stage again the crew-log units a failed restore had already put back.

    A unit that cannot go back -- a resumed session now writes it, or a sync failed --
    stays live while the rest of its session stays in the batch. It is HELD out of
    retention (:func:`kiro_crew.crew_log.store.hold_unit`) and recorded beside the
    session's staging, so the sweep cannot expire half of a session the user can still
    restore; the hold is released when that session is restored or the batch emptied.
    """
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    held: list[str] = []
    for unit_id, staged in reversed(restored):
        try:
            status = crew_store.stage_unit(KIND_SESSION, unit_id, staged)
        except Exception:
            status = crew_store.REMOVE_FAILED
        if status != crew_store.REMOVE_REMOVED:
            logger.error(
                "crew log %r was restored alone (%s); holding it out of retention until "
                "session %r is restored or trash batch %r is emptied",
                unit_id,
                status,
                uid,
                batch_id,
            )
            if crew_store.hold_unit(KIND_SESSION, unit_id):
                held.append(unit_id)
    if held:
        _record_crew_holds(batch_id, uid, held)


def _release_restored_holds(restored: list[tuple[str, Path]]) -> None:
    """Let the units a committed restore put back age out again."""
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    for unit_id, _staged in restored:
        crew_store.release_unit_hold(KIND_SESSION, unit_id)


def _crew_holds_path(batch_id: str, uid: str) -> Path:
    """Where the units held for *uid* in *batch_id* are listed, inside the hidden tree."""
    return _crew_staging(batch_id, uid) / _CREW_HOLDS_FILE


def _read_crew_holds(path: Path) -> list[str]:
    """The unit ids listed in *path*; empty when there is none or it is a link."""
    try:
        if platform_compat.is_link_or_junction(path):
            return []
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning("could not read the crew-log holds in %s", path, exc_info=True)
        return []
    return [line for line in text.splitlines() if _UNIT_ID_RE.match(line)]


def _record_crew_holds(batch_id: str, uid: str, unit_ids: list[str]) -> None:
    """Add *unit_ids* to the holds listed for *uid*. A unit whose listing fails stays held."""
    path = _crew_holds_path(batch_id, uid)
    listed = dict.fromkeys([*_read_crew_holds(path), *unit_ids])
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(path, "".join(f"{unit_id}\n" for unit_id in listed), fsync=True)
    except OSError:
        logger.error(
            "could not record the crew-log holds of %r; %s stay held",
            uid,
            ", ".join(unit_ids),
            exc_info=True,
        )


def _release_crew_holds(batch_id: str, uid: str) -> None:
    """Release every hold recorded for *uid* in *batch_id*: its session is whole or gone."""
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.schema import KIND_SESSION

    if not _UNIT_ID_RE.match(uid):
        return
    path = _crew_holds_path(batch_id, uid)
    for unit_id in _read_crew_holds(path):
        crew_store.release_unit_hold(KIND_SESSION, unit_id)
    with suppress(OSError):
        path.unlink()
    for directory in (path.parent, path.parent.parent):
        with suppress(OSError):
            directory.rmdir()


def _unlisted_crew_staging(batch: Path) -> list[str]:
    """The sessions with crew logs staged for *batch* that its manifest does not list.

    The crew-log counterpart of :func:`_unlisted_files`: a unit staged for a session no
    entry names is still the only copy of that history, so nothing that removes the
    batch may remove it. An unreadable manifest lists nothing, the safe direction.
    """
    parsed = _read_manifest(batch)
    listed = {str(entry.get("uid") or "") for entry in parsed[1]} if parsed else set()
    base = _crew_staging(batch.name)
    try:
        if platform_compat.is_link_or_junction(base) or not base.is_dir():
            return []
        staged = sorted(child.name for child in base.iterdir())
    except OSError:
        return [base.name]
    return [uid for uid in staged if uid not in listed]


def _discard_crew_staging(batch_id: str) -> int:
    """Remove a batch's crew-log staging for good; the bytes it held.

    Called only for a batch the user selected and that was emptied without a skip.
    Nothing else removes crew-log staging: the batch directory is agent-writable, so
    its absence is no evidence the user approved deleting what it staged.
    """
    base = _crew_staging(batch_id)
    try:
        if platform_compat.is_link_or_junction(base) or not base.is_dir():
            return 0
        uids = [child.name for child in base.iterdir()]
    except OSError:
        return 0
    size = _tree_bytes(base)
    for uid in uids:
        _release_crew_holds(batch_id, uid)
    shutil.rmtree(base, ignore_errors=True)
    return 0 if base.exists() else size
