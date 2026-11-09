"""Whether a checkout's own agent-spec hooks run in a KAS session, per folder.

A project-only agent (one ``<folder>/.kiro/agents`` declares and the user level
does not) carries ``hooks``: commands from a cloned folder. They run only when
:func:`hooks_enabled` says so for that folder, decided in this order:

1. The owner's own choice in Crew, ``on`` or ``off``. A Crew ``off`` beats
   kiro-cli's trust.
2. Otherwise kiro-cli's own "Trust this workspace?" record
   (:func:`kiro_crew.kiro_cli_workspace_trust.kiro_cli_trusts_folder`), read
   only. When kiro-cli later drops it, the hooks stop again.
3. Otherwise off, which is every folder today.

The record is ``project_hooks_consent.json`` on the keystone floor
(``config.loader_locations.project_hooks_consent_path``): neither the agent's file
tools nor its sandboxed shell can write it. The writers are the owner-gated
``/api/project-hooks/consent`` handler (:func:`set_choice`) and the gateway's
once-per-folder notice mark (:func:`mark_kiro_cli_notice_shown`). Every read
fails soft to "no choice".
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from kiro_crew.config.loader_locations import project_hooks_consent_path

logger = logging.getLogger(__name__)

ON = "on"
OFF = "off"
SOURCE_CREW = "crew"
SOURCE_KIRO_CLI = "kiro_cli"

#: Bound on the remembered folders, so the record cannot grow without end. A full
#: record refuses a new folder rather than drop an old choice.
_MAX_FOLDERS = 1000

_LOCK = threading.Lock()


class StoreUnreadable(OSError):
    """The record exists but cannot be read, so no writer may rewrite it."""


class StoreFull(Exception):
    """A new folder's choice was refused: the record is at :data:`_MAX_FOLDERS`."""


def folder_key(folder: str | os.PathLike[str] | None) -> str | None:
    """The key a folder is stored under: its resolved path, case kept.

    Never case-folded: on Windows a directory can be case-sensitive, so ``Repo`` and
    ``repo`` may be two folders, and a folded key would let one's choice cover the
    other. ``realpath`` already returns the on-disk spelling, so one folder spelled
    two ways still lands on one key.
    """
    if not folder:
        return None
    try:
        resolved = os.path.realpath(os.fspath(folder), strict=True)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(resolved):
        return None
    return resolved


def _load() -> dict[str, Any]:
    """The whole record, through the ONE reader every caller uses.

    Only a missing file reads as empty (no Crew choice anywhere). Anything else
    that is not exactly the shape this module writes -- an unreadable file, bad
    UTF-8 or JSON, a non-object, a ``folders`` that is not an object of ``on`` /
    ``off``, a ``kiro_cli_notified`` that is not a list of strings -- raises
    :class:`StoreUnreadable`. Readers answer that with hooks off and never fall
    back to kiro-cli's trust; writers abandon the write, so a damaged record is
    never replaced by a guess.
    """
    path = project_hooks_consent_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise StoreUnreadable(f"project-hooks consent store cannot be read: {exc}") from exc
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise StoreUnreadable("project-hooks consent store is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise StoreUnreadable("project-hooks consent store is not an object")
    folders = doc.get("folders", {})
    if not isinstance(folders, dict) or not all(
        isinstance(k, str) and v in (ON, OFF) for k, v in folders.items()
    ):
        raise StoreUnreadable("project-hooks consent store has an unknown folder entry")
    notified = doc.get("kiro_cli_notified", [])
    if not isinstance(notified, list) or not all(isinstance(k, str) for k in notified):
        raise StoreUnreadable("project-hooks consent store has an unknown notice entry")
    return doc


#: :func:`crew_choice` for a record that exists but cannot be trusted: hooks off.
UNREADABLE = "unreadable"


def crew_choice(folder: str | os.PathLike[str] | None) -> str | None:
    """The owner's own choice for *folder*: ``"on"``, ``"off"``, ``None`` (no choice),
    or :data:`UNREADABLE` when the record exists but cannot be read."""
    key = folder_key(folder)
    if key is None:
        return None
    try:
        # Under the writers' lock: an in-place save passes through bytes that are
        # not a whole record, which an unlocked read would take as UNREADABLE.
        with _LOCK:
            doc = _load()
    except StoreUnreadable:
        logger.warning("project-hooks consent store is unreadable; project hooks stay off")
        return UNREADABLE
    return doc.get("folders", {}).get(key)


def hooks_enabled(folder: str | os.PathLike[str] | None) -> tuple[bool, str]:
    """Whether *folder*'s project-only spec hooks run, and who decided it.

    ``(True, "crew")``, ``(True, "kiro_cli")``, ``(False, "crew")`` for an owner
    turn-off or a Crew record that cannot be read (kiro-cli is not consulted then),
    or ``(False, "")`` when nobody turned them on. Read-only.
    """
    choice = crew_choice(folder)
    if choice == ON:
        return True, SOURCE_CREW
    if choice is not None:  # OFF, or UNREADABLE: never fall back to kiro-cli
        return False, SOURCE_CREW
    from kiro_crew.kiro_cli_workspace_trust import kiro_cli_trusts_folder

    if folder and kiro_cli_trusts_folder(folder):
        return True, SOURCE_KIRO_CLI
    return False, ""


def _write(doc: dict[str, Any]) -> None:
    """Persist *doc*, rewriting an existing record IN PLACE so it keeps its inode.

    A running Linux sandbox bind-mounts that inode read-only at this name; a rename
    over it would detach the mount there and leave the new file writable from inside
    that sandbox. Same pattern as ``shared_crew.preview.write_stored``. A failed write
    (a full disk) puts the previous record back before it raises, so the stored
    choices survive. A read torn by the rewrite fails :func:`_load`'s strict parse and
    reads as ``UNREADABLE``: hooks off, no kiro-cli fallback. A symlink or hard-linked
    record is refused.
    """
    import stat

    path = project_hooks_consent_path()
    data = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
    base = os.O_RDWR | getattr(os, "O_BINARY", 0)
    base |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    created = True
    try:
        fd = os.open(path, base | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        created = False
        named = os.lstat(path)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if stat.S_ISLNK(named.st_mode) or (getattr(named, "st_file_attributes", 0) & reparse):
            raise OSError(f"refusing to rewrite {path.name}: it is a link") from None
        fd = os.open(path, base)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"refusing to rewrite {path.name}: not a single-link regular file")
        named = os.lstat(path)
        if (named.st_dev, named.st_ino) != (st.st_dev, st.st_ino):
            raise OSError(f"refusing to rewrite {path.name}: it changed while opening")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        old = _read_fd(fd, st.st_size)
        try:
            # Space is reserved before a byte of the old record is touched, so a
            # full disk usually fails here with the old choices intact.
            if hasattr(os, "posix_fallocate") and len(data) > len(old):
                os.posix_fallocate(fd, 0, len(data))
            _overwrite(fd, data)
        except BaseException:
            # Back to the state before this save, then re-raise: a record this call
            # created is removed (absent means "no Crew choice"); an existing one
            # gets its last good bytes back over the same inode.
            # shortcut: a crash between the two writes still leaves a torn record,
            # which _load reads as UNREADABLE (hooks off); a sidecar copy would close
            # it if that ever matters.
            try:
                if created:
                    # Closed first: Windows cannot unlink a file that is still open.
                    os.close(fd)
                    fd = -1
                    now = os.lstat(path)
                    if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
                        os.unlink(path)
                else:
                    _overwrite(fd, old)
            except OSError:
                logger.warning("could not restore %s after a failed save", path.name)
            raise
    finally:
        if fd >= 0:
            os.close(fd)


def _read_fd(fd: int, size: int) -> bytes:
    """The record's current bytes, read through the open descriptor."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while size > 0:
        chunk = os.read(fd, size)
        if not chunk:
            break
        chunks.append(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _overwrite(fd: int, data: bytes) -> None:
    """Write *data* from offset 0, then cut the file to its length, and fsync."""
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    os.ftruncate(fd, len(data))
    os.fsync(fd)


def set_choice(folder: str | os.PathLike[str], enabled: bool) -> str:
    """Record the owner's ``on``/``off`` for *folder*; returns its key.

    Raises ``ValueError`` for a folder that is not a directory, :class:`StoreFull`,
    and ``OSError`` (:class:`StoreUnreadable` included) when the record cannot be read.
    """
    key = folder_key(folder)
    if key is None:
        raise ValueError("folder is not an existing directory")
    with _LOCK:
        doc = _load()
        folders = dict(doc.get("folders", {}))
        if key not in folders and len(folders) >= _MAX_FOLDERS:
            # Never evict: a dropped Crew turn-off would hand the folder back to
            # kiro-cli's trust and start its hooks again.
            raise StoreFull("the project-hooks record holds the most folders it can")
        folders[key] = ON if enabled else OFF
        doc["folders"] = folders
        _write(doc)
    return key


def mark_kiro_cli_notice_shown(folder: str | os.PathLike[str]) -> bool:
    """Mark the "running because of kiro-cli" notice shown for *folder*.

    ``True`` only the first time, so the caller shows the notice once per folder.
    A failed write answers ``False`` (no notice) rather than raising.
    """
    key = folder_key(folder)
    if key is None:
        return False
    try:
        with _LOCK:
            doc = _load()
            shown = list(doc.get("kiro_cli_notified", []))
            if key in shown:
                return False
            doc["kiro_cli_notified"] = (shown + [key])[-_MAX_FOLDERS:]
            _write(doc)
    except Exception:  # noqa: BLE001 - a notice mark must never fail a turn
        logger.debug("could not record the project-hooks notice mark", exc_info=True)
        return False
    return True
