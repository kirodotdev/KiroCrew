"""Which scratch tree each session key last used, kept across a resume.

A conversation resumed after its process ended (a gateway restart, a transient
backend exit) gets a new provider and client, which know nothing of the tree
the previous process wrote to. :func:`record_tree` remembers that tree per
session key; :func:`recorded_tree` hands it back on the resume, and the spawn
passes it as ``shared_scratch`` through :func:`agent_scratch.shared_scratch_window`,
which re-validates it and falls back to a fresh directory when it is gone.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

from kiro_crew import agent_scratch, platform_compat
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Which tree each session key last exposed as ``$KIROCREW_SCRATCH``, so a key
#: that returns after its process ended rejoins that tree instead of starting an
#: empty directory while the old one waits for the sweep (:func:`recorded_tree`).
#:
#: Kept in the managed root, not in ``session_map.json``, for the boundary: the
#: root is masked from every sandboxed process, while the session map is
#: agent-writable. A tree name an agent could rewrite would let it point its own
#: key at another session's directory and mount it on the next resume. The sweep
#: removes plain directories only, so the file is never swept.
TREE_INDEX_FILENAME = ".trees.json"

#: Largest index this module will read. A row is a session key plus a directory
#: name, and rows whose tree is gone are pruned on every write.
_TREE_INDEX_MAX_BYTES = 1 << 20

_TREE_INDEX_LOCK = threading.Lock()


def _read_tree_index(root: Path) -> dict[str, str]:
    """The index rows, or empty for anything this module did not write."""
    index = root / TREE_INDEX_FILENAME
    agent_scratch._refuse_linked(index, TREE_INDEX_FILENAME)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(index, flags)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _TREE_INDEX_MAX_BYTES:
            return {}
        data = os.read(fd, _TREE_INDEX_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        rows = json.loads(data)
    except ValueError:
        return {}
    if not isinstance(rows, dict):
        return {}
    return {k: v for k, v in rows.items() if isinstance(k, str) and _is_tree_name(v)}


def _is_tree_name(name: object) -> bool:
    """A single directory name: no separator, no ``.``/``..``."""
    return isinstance(name, str) and name not in ("", ".", "..") and Path(name).name == name


#: Registration tickets prevent a delayed predecessor replacing its successor.
_TREE_INDEX_TICKETS: dict[str, int] = {}
_TREE_INDEX_UNPERSISTED: set[str] = set()
_TREE_INDEX_RETIRED_TICKET = -1
_TREE_INDEX_MAX_ENTRIES = 4096
_TREE_INDEX_MAX_KEY_CHARS = 1024
_TREE_INDEX_MAX_NAME_CHARS = 255
_TREE_INDEX_MAX_PATH_CHARS = 32768
#: A bounded, shrinking fallback once a key's persistence guard cannot be retained.
_TREE_INDEX_DISK_SNAPSHOT: tuple[Path, dict[str, str]] | None = None

#: Process-local bindings, retained even when the disk index is unwritable.
_TREE_INDEX_LIVE: dict[str, Path] = {}

#: Independent of the disk-index lock so cache readers do not wait for writes.
_TREE_INDEX_LIVE_LOCK = threading.Lock()


def _exclude_tree_disk_fallback(key: str) -> None:
    """Exclude one key without accumulating unbounded tombstones; live lock held."""
    global _TREE_INDEX_DISK_SNAPSHOT
    if _TREE_INDEX_DISK_SNAPSHOT is None:
        root = agent_scratch.scratch_root()
        try:
            rows = _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.warning(
                "agent-scratch: could not snapshot the tree index; uncached scratch resumes "
                "will start fresh until gateway restart",
                exc_info=True,
            )
            rows = {}
        # A live successor may not be persisted yet. Its predecessor must not
        # survive in the snapshot after that successor is reclaimed.
        for live_key in _TREE_INDEX_LIVE:
            rows.pop(live_key, None)
        _TREE_INDEX_DISK_SNAPSHOT = (root, rows)
        logger.warning(
            "agent-scratch: a binding guard was retired or refused; uncached scratch resumes "
            "use a bounded snapshot until gateway restart, excluding affected keys"
        )
    _TREE_INDEX_DISK_SNAPSHOT[1].pop(key, None)


def _retire_tree_binding(key: str) -> None:
    global _TREE_INDEX_RETIRED_TICKET
    _TREE_INDEX_LIVE.pop(key, None)
    _TREE_INDEX_RETIRED_TICKET = max(_TREE_INDEX_RETIRED_TICKET, _TREE_INDEX_TICKETS.pop(key, -1))
    # The disk row may still name a predecessor after failed persistence.
    if key in _TREE_INDEX_UNPERSISTED:
        _TREE_INDEX_UNPERSISTED.remove(key)
        _exclude_tree_disk_fallback(key)


def _admit_tree_binding(key: str, tree: Path, ticket: int | None) -> bool:
    global _TREE_INDEX_RETIRED_TICKET
    for old_key, old_tree in list(_TREE_INDEX_LIVE.items()):
        if not agent_scratch._is_plain_dir(old_tree):
            _retire_tree_binding(old_key)
    if ticket is not None:
        if ticket <= _TREE_INDEX_TICKETS.get(key, -1):
            return False
        if key not in _TREE_INDEX_LIVE and ticket <= _TREE_INDEX_RETIRED_TICKET:
            _exclude_tree_disk_fallback(key)
            return False
    if (
        len(key) > _TREE_INDEX_MAX_KEY_CHARS
        or len(tree.name) > _TREE_INDEX_MAX_NAME_CHARS
        or len(str(tree)) > _TREE_INDEX_MAX_PATH_CHARS
        or (key not in _TREE_INDEX_LIVE and len(_TREE_INDEX_LIVE) >= _TREE_INDEX_MAX_ENTRIES)
    ):
        if ticket is not None:
            _TREE_INDEX_RETIRED_TICKET = max(_TREE_INDEX_RETIRED_TICKET, ticket)
        _exclude_tree_disk_fallback(key)
        return False
    if _TREE_INDEX_DISK_SNAPSHOT is not None:
        _TREE_INDEX_DISK_SNAPSHOT[1].pop(key, None)
    _TREE_INDEX_LIVE[key] = tree
    _TREE_INDEX_UNPERSISTED.add(key)
    if ticket is not None:
        _TREE_INDEX_TICKETS[key] = ticket
    return True


def record_tree(key: str, tree: Path, *, ticket: int | None = None) -> bool:
    """Remember *tree* as *key*'s work directory without raising write failures.

    Return True only when the persisted row confirms the binding.

    Only a directory directly under the managed root is recorded. Rows whose
    directory is absent are dropped in the same write, so the index
    holds at most the trees the sweep has not reclaimed yet.

    *ticket* fences submission order, including failed writes. Retired tickets
    leave a scalar floor: an unretained key needs a newer ticket to be admitted,
    while retained keys keep their own ordering. ``None`` is unfenced.
    Admission bounds bindings, tickets and persistence guards together.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name):
        return False
    with _TREE_INDEX_LOCK:
        with _TREE_INDEX_LIVE_LOCK:
            if not _admit_tree_binding(key, tree, ticket):
                return False
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            rows = _read_tree_index(root)
            if rows.get(key) != tree.name:
                rows[key] = tree.name
                rows = {k: v for k, v in rows.items() if agent_scratch._is_plain_dir(root / v)}
                payload = json.dumps(rows, sort_keys=True)
                if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                    logger.warning(
                        "agent-scratch: tree index would exceed %d bytes; keeping existing "
                        "bindings. This session may start a fresh tree after a gateway restart.",
                        _TREE_INDEX_MAX_BYTES,
                    )
                    return False
                atomic_write(root / TREE_INDEX_FILENAME, payload)
                rows = _read_tree_index(root)
            persisted = rows.get(key) == tree.name
            if persisted:
                with _TREE_INDEX_LIVE_LOCK:
                    _TREE_INDEX_UNPERSISTED.discard(key)
            return persisted
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.debug("agent-scratch: could not record the tree for a session", exc_info=True)

    return False


def has_live_tree(key: str) -> bool:
    """Whether memory or disk has a binding a no-tree successor must retire."""
    with _TREE_INDEX_LIVE_LOCK:
        if key in _TREE_INDEX_LIVE or (
            _TREE_INDEX_DISK_SNAPSHOT is not None and key in _TREE_INDEX_DISK_SNAPSHOT[1]
        ):
            return True
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            try:
                info = os.lstat(root)
            except FileNotFoundError:
                return False
            if stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info):
                return False
            return key in _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            # Allocation must attempt durable retirement when the index is unreadable.
            return True


def abandon_tree(key: str, only: Path | None = None) -> bool:
    """Retire a binding without deleting its tree; report durable removal.

    With *only*, retire the binding only while it still names that tree: a
    successor another allocation already bound for *key* is kept.
    A linked or absent root has no usable binding to retire.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            try:
                info = os.lstat(root)
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info):
                return True
            with _TREE_INDEX_LIVE_LOCK:
                live = _TREE_INDEX_LIVE.get(key)
                if only is not None and live is not None and live != only:
                    return True
                _exclude_tree_disk_fallback(key)
                _retire_tree_binding(key)
            rows = _read_tree_index(root)
            if only is not None and rows.get(key, only.name) != only.name:
                return True
            if key in rows:
                del rows[key]
                atomic_write(root / TREE_INDEX_FILENAME, json.dumps(rows, sort_keys=True))
            return key not in _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.warning(
                "agent-scratch: could not persist abandoned binding for %s; "
                "it remains retired until gateway restart",
                key,
                exc_info=True,
            )
    return False


def holds_live_tree(key: str, tree: Path) -> bool:
    """Whether *key*'s in-memory binding is *tree*."""
    with _TREE_INDEX_LIVE_LOCK:
        return _TREE_INDEX_LIVE.get(key) == tree


def drop_other_row(key: str, keep: Path) -> bool:
    """Drop *key*'s disk row when it names a tree other than *keep*.

    For a binding whose write failed: the in-memory binding stays, and a row
    left from an earlier tree must not be rejoined after a restart. Never
    raises. Returns False only when a row naming another tree was read and
    is still on disk; an index that cannot be read counts as no such row.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            info = os.lstat(root)
            if stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info):
                return True
            rows = _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            return True
        if rows.get(key, keep.name) == keep.name:
            return True
        try:
            del rows[key]
            atomic_write(root / TREE_INDEX_FILENAME, json.dumps(rows, sort_keys=True))
            return key not in _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.warning(
                "agent-scratch: could not drop the earlier tree row for %s; "
                "after a gateway restart it may resume into that earlier tree",
                key,
                exc_info=True,
            )
    return False


def _recorded_cached_tree(root: Path, key: str) -> Path | None:
    live = _TREE_INDEX_LIVE.get(key)
    if live is not None and (live.parent != root or not agent_scratch._is_plain_dir(live)):
        _retire_tree_binding(key)
        return None
    if live is None and _TREE_INDEX_DISK_SNAPSHOT is not None:
        snapshot_root, rows = _TREE_INDEX_DISK_SNAPSHOT
        name = rows.get(key) if root == snapshot_root else None
        tree = root / name if name else None
        return tree if tree is not None and agent_scratch._is_plain_dir(tree) else None
    return live


def recorded_tree(key: str) -> Path | None:
    """The tree most recently bound to *key* in this process, or None.

    A cached binding takes precedence; a reclaimed binding is retired. Once a
    guard is lost, a bounded disk snapshot preserves unrelated uncached keys,
    excluding each affected key. A fresh process reads the persisted row instead.

    Not validated further: the spawner hands the answer to
    :func:`shared_scratch_window`, which checks it is still a plain directory
    under the root and falls back to the process's own directory when it is
    gone.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LIVE_LOCK:
        live = _recorded_cached_tree(root, key)
        if live is not None or _TREE_INDEX_DISK_SNAPSHOT is not None:
            return live
    with _TREE_INDEX_LOCK:
        with _TREE_INDEX_LIVE_LOCK:
            live = _recorded_cached_tree(root, key)
            if live is not None or _TREE_INDEX_DISK_SNAPSHOT is not None:
                return live
        try:
            name = _read_tree_index(root).get(key)
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.debug("agent-scratch: could not read the tree index", exc_info=True)
            return None
    tree = root / name if name else None
    return tree if tree is not None and agent_scratch._is_plain_dir(tree) else None
