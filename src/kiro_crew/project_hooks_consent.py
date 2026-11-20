"""Whether a checkout's own agent-spec hooks run in a KAS session, per folder.

A project-only agent (one ``<folder>/.kiro/agents`` declares and the user level
does not) carries ``hooks``: commands from a cloned folder. They run only when
:func:`hooks_enabled` says so for that folder, decided in this order:

1. The owner's own choice in Crew, ``on`` or ``off``. A Crew ``off`` beats
   kiro-cli's trust. An ``on`` covers only the hooks the folder held when it was
   given (:func:`hooks_fingerprint`); once they differ it reads as
   :data:`STALE`: off, kiro-cli not consulted, and the notice asks again.
2. Otherwise kiro-cli's own "Trust this workspace?" record
   (:func:`kiro_crew.kiro_cli_workspace_trust.kiro_cli_trusts_folder`), read
   only. When kiro-cli later drops it, the hooks stop again.
3. Otherwise off, which is every folder today.

The record is ``project_hooks_consent.json`` on the keystone floor
(``config.loader_locations.project_hooks_consent_path``): neither the agent's file
tools nor its sandboxed shell can write it. The writers are the owner-gated
``/api/project-hooks/consent`` handler (:func:`set_choice`) and the gateway's
once-per-folder notice mark (:func:`mark_kiro_cli_notice_shown`), plus the
gateway's one-time fingerprint of a saved ``on`` (:func:`crew_choice`). Every read
fails soft to "no choice".
"""

from __future__ import annotations

import hashlib
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
    prints = doc.get("hook_fingerprints", {})
    if not isinstance(prints, dict) or not all(
        isinstance(k, str) and _is_fingerprint(v) for k, v in prints.items()
    ):
        raise StoreUnreadable("project-hooks consent store has an unknown fingerprint entry")
    return doc


def _is_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


#: One ``.kiro/agents`` file as one scan read it: its file name, the agent name it
#: answers to, and its parsed spec (``None`` when it did not read).
_Scanned = tuple[str, str, dict[str, Any] | None]


def _scan(folder: str) -> list[_Scanned]:
    """Every spec file of *folder*'s ``.kiro/agents``, read ONCE, in the order and by
    the name rule the firing path resolves an agent with (``project_agent_files``,
    ``project_agent_name``)."""
    from kiro_crew.agent_discovery import (
        _project_agent_fallback_name,
        _read_agent_spec,
        project_agent_files,
        spec_str,
    )

    scanned: list[_Scanned] = []
    for path in project_agent_files(
        folder, operation="project_hooks_fingerprint", source="unknown"
    ):
        spec = _read_agent_spec(path, operation="project_hooks_fingerprint", source="unknown")
        fallback = _project_agent_fallback_name(path)
        name = (spec_str(spec, "name", fallback) or fallback) if spec is not None else fallback
        scanned.append((path.name, name, spec))
    return scanned


def _digest(scanned: list[_Scanned]) -> str:
    entries = sorted(
        (
            [file, spec.get("name"), spec["hooks"]]
            for file, _name, spec in scanned
            if isinstance(spec, dict) and "hooks" in spec
        ),
        key=lambda e: e[0],
    )
    data = json.dumps(entries, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def hooks_fingerprint(folder: str) -> str:
    """sha256 of every hook definition *folder*'s ``.kiro/agents`` specs declare.

    An ``on`` is bound to this, not to the path alone: a folder deleted and
    re-created (or re-cloned) at the same path with other hooks gives another
    value, so its hooks wait for a new ``on``. Covers each spec that reads and sets
    ``hooks``; that is a superset of the hooks that can run, so a change can only
    re-ask, never skip one. Only ``hooks`` is hashed: an edit to a prompt or tools
    list does not re-ask.

    shortcut: a script a hook command runs is not hashed, so a swapped folder that
    copies the old hook text but changes that script still runs; hash referenced
    files if that ever matters.
    """
    return _digest(_scan(folder))


#: :func:`crew_choice` for a record that exists but cannot be trusted: hooks off.
UNREADABLE = "unreadable"
#: :func:`crew_choice` for an ``on`` given to other hooks than the folder holds now:
#: hooks off, kiro-cli not consulted, and the notice asks again.
STALE = "stale"


def _crew_choice(key: str, seen: list[_Scanned] | None) -> str | None:
    """:func:`crew_choice` for a resolved *key*. A saved ``on`` leaves the scan it was
    checked against in *seen*, whether it matched or not, so a caller fires exactly
    those hooks and can still tell an unreadable spec from changed hooks."""
    try:
        # Under the writers' lock: an in-place save passes through bytes that are
        # not a whole record, which an unlocked read would take as UNREADABLE.
        with _LOCK:
            doc = _load()
            choice = doc.get("folders", {}).get(key)
            if choice != ON:
                return choice
            try:
                scanned = _scan(key)
                current = _digest(scanned)
            except Exception:  # noqa: BLE001 - fail closed: unread hooks are not consented
                logger.warning("could not read the folder's hooks; project hooks stay off")
                return STALE
            stored = doc.get("hook_fingerprints", {}).get(key)
            if stored is None:
                doc["hook_fingerprints"] = {**doc.get("hook_fingerprints", {}), key: current}
                try:
                    _write(doc)
                except OSError:
                    logger.debug("could not record the project-hooks fingerprint", exc_info=True)
                stored = current
    except StoreUnreadable:
        logger.warning("project-hooks consent store is unreadable; project hooks stay off")
        return UNREADABLE
    if seen is not None:
        seen[:] = scanned
    if stored != current:
        logger.info("project hooks changed since they were turned on; they wait for a new yes")
        return STALE
    return ON


def crew_choice(folder: str | os.PathLike[str] | None) -> str | None:
    """The owner's own choice for *folder*: ``"on"``, ``"off"``, ``None`` (no choice),
    :data:`UNREADABLE` when the record exists but cannot be read, or :data:`STALE`
    when the ``on`` was given to other hooks than the folder declares now.

    An ``on`` saved before fingerprints existed adopts the current one here, once,
    so an existing user is not asked again; if that save fails it stays ``on`` and
    the next read tries again.
    """
    key = folder_key(folder)
    if key is None:
        return None
    return _crew_choice(key, None)


def _enabled(
    folder: str | os.PathLike[str] | None, seen: list[_Scanned] | None
) -> tuple[bool, str]:
    key = folder_key(folder)
    choice = None if key is None else _crew_choice(key, seen)
    if choice == ON:
        return True, SOURCE_CREW
    if choice is not None:  # OFF, STALE or UNREADABLE: never fall back to kiro-cli
        return False, SOURCE_CREW
    from kiro_crew.kiro_cli_workspace_trust import kiro_cli_trusts_folder

    if folder and kiro_cli_trusts_folder(folder):
        if seen is not None and key is not None:
            seen[:] = _scan(key)
        return True, SOURCE_KIRO_CLI
    return False, ""


def hooks_enabled(folder: str | os.PathLike[str] | None) -> tuple[bool, str]:
    """Whether *folder*'s project-only spec hooks run, and who decided it.

    ``(True, "crew")``, ``(True, "kiro_cli")``, ``(False, "crew")`` for an owner
    turn-off, an ``on`` whose hooks have changed since (:data:`STALE`), or a Crew
    record that cannot be read (kiro-cli is not consulted then),
    or ``(False, "")`` when nobody turned them on. Writes only to record the
    fingerprint a saved ``on`` without one adopts (:func:`crew_choice`).
    """
    return _enabled(folder, None)


def consented_project_hooks(folder: str, agent_id: str) -> dict[str, Any] | None:
    """*agent_id*'s ``hooks`` from *folder*'s ``.kiro/agents``, when they may run.

    Taken from the SAME read the consent was checked against, so a spec rewritten
    between the check and the use cannot fire hooks nobody agreed to. ``None`` when
    the folder does not declare the agent or its hooks are off; ``{}`` for a spec
    without ``hooks``. A declared spec that cannot be read raises ``ValueError``, so
    PreToolUse fails closed.
    """
    seen: list[_Scanned] = []
    enabled = _enabled(folder, seen)[0]
    for _file, name, spec in seen:  # first by file stem wins, as the firing resolver
        if name != agent_id:
            continue
        if spec is None:
            # Raised for a saved "on" even when the unread file made it STALE: an
            # unreadable spec under a yes fails closed, it is not read as "off".
            raise ValueError(f"the project spec for agent {agent_id!r} could not be read")
        if not enabled:
            return None
        return {"hooks": spec["hooks"]} if "hooks" in spec else {}
    return None


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
        # The "on" covers the hooks the folder holds now (:func:`hooks_fingerprint`).
        prints = dict(doc.get("hook_fingerprints", {}))
        if enabled:
            prints[key] = hooks_fingerprint(key)
        else:
            prints.pop(key, None)
        doc["hook_fingerprints"] = prints
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
