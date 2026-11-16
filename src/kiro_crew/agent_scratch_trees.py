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
from typing import Any

from kiro_crew import agent_scratch, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.bounded_read import bounded_read_fd

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

#: The index row holding quarantine marks: ``{tree name: [keys]}`` for trees
#: whose ownership a resume could not settle. Kept in the index itself for the
#: same boundary: a mark an agent could delete would let it remount another
#: tree's directory. ``record_tree`` refuses a key holding NUL, and the row's value is
#: not a tree name, so a reader that only wants bindings skips it.
_MARKS_ROW = "\x00quarantine"

#: The index row holding the owners a key left behind when it moved off a
#: directory other keys are still bound to: ``{tree name: [keys]}``. Not a
#: refusal: only the resume ownership check reads it, so a live co-owner in the
#: same tree keeps mounting the directory, while a resume still weighs every key
#: that staged files there. Same schema and boundary as :data:`_MARKS_ROW`.
_FORMER_ROW = "\x00former"

#: The index row naming, for a subagent binding, the parent session the gateway
#: spawned it for: ``{subagent key: parent session key}``. Written only from the
#: spawn's own knowledge, and read only as a claim the resolver must corroborate
#: once the subagent's run record is gone.
_PARENTS_ROW = "\x00parents"

#: Largest index this module will read. A row is a session key plus a directory
#: name, and rows whose tree is gone are pruned on every write.
_TREE_INDEX_MAX_BYTES = 1 << 20

_TREE_INDEX_LOCK = threading.Lock()


def _read_index_document(root: Path, *, strict: bool = False) -> dict[str, object]:
    """The whole index object, or empty for anything this module did not write.

    With *strict*, an index that exists but cannot be used (not a regular file,
    over the size cap, not a JSON object) raises :class:`OSError` instead: the
    quarantine marks it may have held cannot be shown to be absent.
    """
    index = root / TREE_INDEX_FILENAME
    agent_scratch._refuse_linked(index, TREE_INDEX_FILENAME)
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)

    def unusable(why: str) -> dict[str, object]:
        if strict:
            raise OSError(f"the tree index {why}")
        return {}

    try:
        fd = os.open(index, flags)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _TREE_INDEX_MAX_BYTES:
            return unusable("is not a regular file under the size cap")
        data = bounded_read_fd(fd, _TREE_INDEX_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        rows = json.loads(data)
    except ValueError:
        return unusable("does not parse")
    if not isinstance(rows, dict):
        return unusable("is not a JSON object")
    return rows


def _read_tree_index(root: Path) -> dict[str, str]:
    """The index's binding rows, or empty for anything this module did not write."""
    rows = _read_index_document(root)
    return {
        k: v
        for k, v in rows.items()
        if isinstance(k, str) and isinstance(v, str) and _is_tree_name(v)
    }


def _parse_marks(value: object) -> dict[str, frozenset[str]]:
    """Marks from the index mark row; OSError for a row this module did not write."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        _is_tree_name(name) and isinstance(keys, list) and all(isinstance(k, str) for k in keys)
        for name, keys in value.items()
    ):
        raise OSError("the tree index holds a quarantine row this module did not write")
    return {name: frozenset(keys) for name, keys in value.items()}


def _serialize_marks(root: Path, marks: dict[str, frozenset[str]]) -> dict[str, object] | None:
    live: dict[str, object] = {
        n: sorted(k) for n, k in marks.items() if agent_scratch._is_plain_dir(root / n)
    }
    return live or None


def _index_payload(
    root: Path,
    rows: dict[str, str],
    marks: dict[str, frozenset[str]] | None = None,
    *,
    parent: tuple[str, str] | None = None,
    former: dict[str, frozenset[str]] | None = None,
) -> str:
    """The index text for *rows*, carrying the marks unless *marks* replaces them.

    A mark row this module cannot parse is carried unchanged, so a rewrite of
    the bindings never erases evidence that a directory was refused. The former
    owners row is carried the same way unless *former* replaces it.
    """
    carried: object = None
    parents = _read_parents(root)
    if parent is not None:
        key, named = parent
        if named:
            parents[key] = named
        else:
            parents.pop(key, None)
    parents = {k: v for k, v in parents.items() if k in rows}
    if marks is None:
        carried = _read_index_document(root).get(_MARKS_ROW)
        try:
            marks = _parse_marks(carried)
        except OSError:
            marks = None
    if marks is not None:
        carried = _serialize_marks(root, marks)
    if former is None:
        former_row = _read_index_document(root).get(_FORMER_ROW)
        try:
            former_row = _serialize_marks(root, _parse_marks(former_row))
        except OSError:
            pass  # carried unchanged: an unreadable row still refuses at the resume
    else:
        former_row = _serialize_marks(root, former)
    return _document(rows, carried, parents, former_row)


def _document(
    rows: dict[str, str], marks_row: object, parents: dict[str, str], former_row: object = None
) -> str:
    document: dict[str, object] = dict(rows)
    if marks_row is not None:
        document[_MARKS_ROW] = marks_row
    if former_row is not None:
        document[_FORMER_ROW] = former_row
    if parents:
        document[_PARENTS_ROW] = parents
    return json.dumps(document, sort_keys=True)


def _read_parents(root: Path) -> dict[str, str]:
    """The recorded spawn parents; a row this module did not write reads as none.

    Dropping an unreadable row only removes claims, so no owner gains a placement.
    """
    value = _read_index_document(root).get(_PARENTS_ROW)
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str) and v}


def backfill_parent(key: str, parent: str) -> bool:
    """Write *parent* as bound subagent *key*'s spawn parent when no row names one yet.

    The only writer of that row besides ``record_tree(..., parent=)``: a resume reads
    the parent from *key*'s run record while the record exists, so a binding made
    before the row was written does not lose its placement once the record is
    pruned. Never replaces a recorded parent. True once the row reads back.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            rows = _read_tree_index(root)
            if key not in rows or not parent or _read_parents(root).get(key):
                return _read_parents(root).get(key) == parent
            payload = _index_payload(root, rows, parent=(key, parent))
            if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                return False
            atomic_write(root / TREE_INDEX_FILENAME, payload)
            return _read_parents(root).get(key) == parent
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.debug("agent-scratch: could not backfill a subagent's parent", exc_info=True)
    return False


def recorded_parent(key: str) -> str | None:
    """The parent session the gateway recorded when it spawned subagent *key*, or None."""
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            return _read_parents(root).get(key)
        except (OSError, agent_scratch.ScratchBoundaryError):
            return None


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


def record_tree(key: str, tree: Path, *, ticket: int | None = None, parent: str = "") -> bool:
    """Remember *tree* as *key*'s work directory without raising write failures.

    Return True only when the persisted row confirms the binding.

    Only a directory directly under the managed root is recorded. Rows whose
    directory is absent are dropped in the same write, so the index
    holds at most the trees the sweep has not reclaimed yet.

    With *parent*, the spawn parent the gateway recorded for a dedicated subagent
    is written in the same index write as the binding, and the binding counts as
    persisted only when both read back: the index never holds the binding without
    the lineage a later resume needs to place it.

    *ticket* fences submission order, including failed writes. Retired tickets
    leave a scalar floor: an unretained key needs a newer ticket to be admitted,
    while retained keys keep their own ordering. ``None`` is unfenced.
    Admission bounds bindings, tickets and persistence guards together.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name) or "\x00" in key:
        # A key holding NUL could name a reserved row and make it unreadable.
        return False
    with _TREE_INDEX_LOCK:
        with _TREE_INDEX_LIVE_LOCK:
            prior = _TREE_INDEX_LIVE.get(key)
            if not _admit_tree_binding(key, tree, ticket):
                return False
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            rows = _read_tree_index(root)
            parent_known = not parent or _read_parents(root).get(key) == parent
            if rows.get(key) != tree.name or not parent_known:
                leaving = rows.get(key) != tree.name or (prior is not None and prior != tree)
                if leaving and not _mark_before_leaving(
                    root, rows, key, prior if prior != tree else None
                ):
                    # The mark did not land and is held in this process instead; the
                    # disk row stays, and the key keeps the tree it was admitted to.
                    return False
                rows = _read_tree_index(root)
                rows[key] = tree.name
                rows = {k: v for k, v in rows.items() if agent_scratch._is_plain_dir(root / v)}
                payload = _index_payload(root, rows, parent=(key, parent) if parent else None)
                if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                    logger.warning(
                        "agent-scratch: tree index would exceed %d bytes; keeping existing "
                        "bindings. This session may start a fresh tree after a gateway restart.",
                        _TREE_INDEX_MAX_BYTES,
                    )
                    return False
                atomic_write(root / TREE_INDEX_FILENAME, payload)
                rows = _read_tree_index(root)
            persisted = rows.get(key) == tree.name and (
                not parent or _read_parents(root).get(key) == parent
            )
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
            rows = _read_tree_index(root)
            if only is not None and rows.get(key, only.name) != only.name:
                return True
            # Mark first, over the disk rows and the live bindings alike: a binding
            # whose index write failed owns what it staged as much as a persisted one.
            marked = not (key in rows or live is not None) or _mark_before_leaving(
                root, rows, key, live
            )
            # The live binding goes either way, so this process never hands the
            # tree back; a mark that did not land is held in this process.
            with _TREE_INDEX_LIVE_LOCK:
                _exclude_tree_disk_fallback(key)
                _retire_tree_binding(key)
            if not marked:
                return False  # the disk row stays as it was
            if key in rows:
                rows = _read_tree_index(root)
                rows.pop(key, None)
                atomic_write(root / TREE_INDEX_FILENAME, _index_payload(root, rows))
            return key not in _read_tree_index(root)
        except (OSError, agent_scratch.ScratchBoundaryError):
            # The disk row may stay, but this process must never hand it back.
            with _TREE_INDEX_LIVE_LOCK:
                live = _TREE_INDEX_LIVE.get(key)
                if only is None or live is None or live == only:
                    _exclude_tree_disk_fallback(key)
                    _retire_tree_binding(key)
            logger.warning(
                "agent-scratch: could not persist abandoned binding for %s; "
                "it remains retired until gateway restart",
                key,
                exc_info=True,
            )
    return False


def abandon_tree_all(tree: Path, owners: frozenset[str]) -> bool:
    """Retire every binding that names *tree* and mark it with *owners*, in one index write.

    For a directory whose owners resolved to more than one session tree. The
    mark keeps it refused to any key later bound to it (a live co-owner's new
    subagent, say) until its owners resolve to one tree or the sweep reclaims
    it. Under the index lock, so no resume can read the directory as still
    bound to some keys after others were retired, and one write, so no restart
    finds the bindings gone without the mark. Returns True once both are on
    disk. A write that fails or would not fit is not made: the in-memory
    bindings are still retired, so this process never hands *tree* back, and the
    disk rows all stay, so every owner is still bound and the next ownership
    check, in this process or after a restart, refuses the directory again. No
    writer can thin those owners without a mark: a key leaving the directory
    writes the mark first or keeps its row (:func:`_mark_before_leaving`).
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:

        def _retire_live(disk_keys: set[str]) -> None:
            with _TREE_INDEX_LIVE_LOCK:
                keys = disk_keys | {k for k, live in _TREE_INDEX_LIVE.items() if live == tree}
                if _TREE_INDEX_DISK_SNAPSHOT is not None and _TREE_INDEX_DISK_SNAPSHOT[0] == root:
                    rows = _TREE_INDEX_DISK_SNAPSHOT[1]
                    keys |= {k for k, name in rows.items() if name == tree.name}
                for key in keys:
                    live = _TREE_INDEX_LIVE.get(key)
                    if live is None or live == tree:
                        _exclude_tree_disk_fallback(key)
                        _retire_tree_binding(key)

        try:
            try:
                info = os.lstat(root)
            except FileNotFoundError:
                _retire_live(set())
                return True
            if stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info):
                _retire_live(set())
                return True
            rows = _read_tree_index(root)
            doomed = {k for k, name in rows.items() if name == tree.name}
            _retire_live(doomed)
            marks = _read_marks(root)
            marks[tree.name] = marks.get(tree.name, frozenset()) | owners
            former = _read_former(root)
            former.pop(tree.name, None)  # the refusal mark now holds every owner
            kept = {k: v for k, v in rows.items() if k not in doomed}
            payload = _index_payload(root, kept, marks, former=former)
            # Every failure holds the refusal before the lock is released: the live
            # bindings are gone, so nothing else refuses the directory until a mark lands.
            if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                _hold_refusal(root, tree.name, owners)
                _write_owner_row(root, "refused", tree.name, owners)
                return False
            atomic_write(root / TREE_INDEX_FILENAME, payload)
            if tree.name in _read_tree_index(root).values() or (
                _read_marks(root).get(tree.name) != marks[tree.name]
            ):
                _hold_refusal(root, tree.name, owners)
                _write_owner_row(root, "refused", tree.name, owners)
                return False
            _REFUSED_UNPERSISTED.pop(tree.name, None)  # the durable mark holds it now
            return True
        except (OSError, agent_scratch.ScratchBoundaryError):
            _retire_live(set())
            _hold_refusal(root, tree.name, owners)
            _write_owner_row(root, "refused", tree.name, owners)
            logger.warning(
                "agent-scratch: could not persist the retirement of %s; its bindings "
                "remain retired until gateway restart",
                tree.name,
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
            if not _mark_before_leaving(root, rows, key):
                return False
            rows = _read_tree_index(root)
            rows.pop(key, None)
            atomic_write(root / TREE_INDEX_FILENAME, _index_payload(root, rows))
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


#: Directories whose quarantine the index did not take (a write that failed or
#: did not fit), refused by :func:`refused_across_trees` for the life of this
#: process. A live co-owner's respawn checks the mark, not the ownership of its
#: owners, so without this it would remount a directory a resume found shared
#: across trees. An entry goes when a durable mark or a settled resume supersedes
#: it, or when the sweep reclaims the directory, so it never outnumbers the live
#: scratch directories. Each entry keeps the owners its mark would have carried,
#: so a resume weighs them and settles it like a durable mark.
_REFUSED_UNPERSISTED: dict[str, frozenset[str]] = {}

#: The held refusals never outnumber the bindings the index itself admits, and
#: one refusal names no more owners than the index could bind.
_REFUSED_MAX_ENTRIES = _TREE_INDEX_MAX_ENTRIES
_REFUSED_MAX_OWNERS = _TREE_INDEX_MAX_ENTRIES

#: Set once a held refusal would pass either bound. The map does not grow; every
#: directory without a durable mark is refused instead, and no resume settles it,
#: until the process restarts.
_REFUSALS_OVERFLOWED = False

#: The owner :func:`clear_quarantine` adds to a held refusal while
#: :data:`_REFUSALS_OVERFLOWED` is set, so no checked owner set settles it. In
#: memory only: no session key contains NUL, and it is never written.
_OVERFLOW_OWNER = "\x00refusals-overflowed"


def _hold_refusal(root: Path, name: str, owners: frozenset[str]) -> None:
    """Refuse *name* in this process after its mark could not be written. Lock held."""
    global _REFUSALS_OVERFLOWED
    for gone in [n for n in _REFUSED_UNPERSISTED if not agent_scratch._is_plain_dir(root / n)]:
        del _REFUSED_UNPERSISTED[gone]
    merged = _REFUSED_UNPERSISTED.get(name, frozenset()) | owners
    new_entry = name not in _REFUSED_UNPERSISTED
    if len(merged) > _REFUSED_MAX_OWNERS or (
        new_entry and len(_REFUSED_UNPERSISTED) >= _REFUSED_MAX_ENTRIES
    ):
        if not _REFUSALS_OVERFLOWED:
            logger.warning(
                "agent-scratch: too many refusals are held in memory (at %r); every "
                "scratch directory without a durable mark is refused until the gateway "
                "restarts. Staged files are untouched on disk.",
                name,
            )
        _REFUSALS_OVERFLOWED = True
        return
    _REFUSED_UNPERSISTED[name] = merged


def held_refusal(tree: Path) -> frozenset[str] | None:
    """The owners of *tree*'s in-process refusal, or None when none is held.

    Real owners only: an overflow is refused by :func:`refused_across_trees`, so
    its in-memory owner never reaches an ownership check or a durable mark.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return None
    with _TREE_INDEX_LOCK:
        return _REFUSED_UNPERSISTED.get(tree.name)


def _read_marks(root: Path) -> dict[str, frozenset[str]]:
    return _parse_marks(_read_index_document(root, strict=True).get(_MARKS_ROW))


def _read_former(root: Path) -> dict[str, frozenset[str]]:
    return _parse_marks(_read_index_document(root, strict=True).get(_FORMER_ROW))


def former_owners(tree: Path) -> frozenset[str] | None:
    """The keys that moved off *tree* while others stayed bound to it, or None.

    Read only by the resume ownership check: these keys still own what they
    staged there, so they count when deciding whether the directory is one
    tree's. Raises :class:`OSError` when the row cannot be read, so the caller
    refuses rather than reading it as empty.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return None
    with _TREE_INDEX_LOCK:
        try:
            return _read_former(root).get(tree.name)
        except agent_scratch.ScratchBoundaryError as exc:
            raise OSError(str(exc)) from exc


#: The resolver for a directory with an open ownership question, installed by
#: ``session_allocation_parts.scratch_ownership`` (the one place owners are
#: weighed against the session tree). None in a process without it.
_OWNERSHIP_RESOLVER: Any = None


def set_ownership_resolver(resolver: Any) -> None:
    """Install the async ``resolver(tree) -> bool`` the mount seams call."""
    global _OWNERSHIP_RESOLVER
    _OWNERSHIP_RESOLVER = resolver


async def resolve_open_ownership(tree: Path) -> bool:
    """Answer *tree*'s open ownership question before a mount reads its refusal.

    A directory with a former-owners row was left by a key while others stayed
    bound, so whether its owners share one tree is still open. Every mount seam
    (a resume, a spawn on a parent's behalf, a respawn) calls this first: the
    resolver clears the row when the owners are one tree and quarantines the
    directory when they are not. A row that cannot be read, or a process with no
    resolver, quarantines it with every key bound to it, fail-closed, through
    :func:`quarantine_tree`, which holds the refusal in this process when the
    index cannot take the mark.

    True when the directory may be mounted. On False the refusal is also held in
    this process (:func:`_hold_refusal`), so :func:`refused_across_trees` agrees
    even when the resolver wrote no mark (an owner recorded after it read them).
    """
    import asyncio

    try:
        # Owners a settle kept as provenance, and owner rows for bindings the index
        # could not take, leave the question open the same way a former row does.
        retained = await asyncio.to_thread(_retained_owners, tree)
        # The former row is read last: a co-owner leaving writes only that row
        # (``_mark_before_leaving``), so one written after the retained read is
        # still seen here. A row written after both reads falls in the window
        # between this snapshot and the launch, which this check does not cover.
        former = await asyncio.to_thread(former_owners, tree)
    except OSError:
        former = None
        retained = frozenset()
        unreadable = True
    else:
        unreadable = False
    if former is None and not retained and not unreadable:
        return True
    resolver = _OWNERSHIP_RESOLVER
    if unreadable or resolver is None:
        owners = await asyncio.to_thread(keys_on_tree, tree)
        await asyncio.to_thread(quarantine_tree, tree, owners | (former or frozenset()) | retained)
        return False
    if await resolver(tree):
        return True
    owners = await asyncio.to_thread(keys_on_tree, tree)
    await asyncio.to_thread(_hold_unsettled, tree, owners | (former or frozenset()))
    return False


def _retained_owners(tree: Path) -> frozenset[str]:
    """The owner and provenance rows naming *tree*; OSError when unreadable. Blocking."""
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return frozenset()
    with _TREE_INDEX_LOCK:
        try:
            rows = _read_owners(root)
        except agent_scratch.ScratchBoundaryError as exc:
            raise OSError(str(exc)) from exc
    return rows["owner"].get(tree.name, frozenset()) | rows["provenance"].get(
        tree.name, frozenset()
    )


def _hold_unsettled(tree: Path, owners: frozenset[str]) -> None:
    """Hold a refusal of *tree* in this process after an ownership check failed. Blocking."""
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return
    with _TREE_INDEX_LOCK:
        _hold_refusal(root, tree.name, owners)


#: Ownership evidence the tree index could not take, kept beside it in the masked
#: root (``scratch/.owners.json``, masked by name like the index) and outside the
#: index's size cap: ``{"owner": {tree name: [keys]}, "refused": {tree name: [keys]}}``.
#: ``owner`` lists keys whose binding the index could not persist; ``refused`` lists
#: the owners of a retirement whose mark the index could not persist; ``provenance``
#: keeps the owners of a mark or former-owner row a check cleared, because they still
#: staged files there and may later leave the tree (kept until the sweep reclaims it).
OWNERS_FILENAME = ".owners.json"
_OWNERS_MAX_BYTES = 4 << 20
_OWNER_KINDS = ("owner", "refused", "provenance")


def _read_owners(root: Path) -> dict[str, dict[str, frozenset[str]]]:
    """The owner file's rows; OSError when it exists but this module did not write it.

    Opened without following links or blocking (a FIFO planted in its place must
    not hang the index lock), and read only as a regular file under its size cap.
    """
    path = root / OWNERS_FILENAME
    agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {kind: {} for kind in _OWNER_KINDS}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _OWNERS_MAX_BYTES:
            raise OSError("the scratch owner file is not a regular file under its size cap")
        data = bounded_read_fd(fd, _OWNERS_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise OSError("the scratch owner file does not parse") from exc
    if not isinstance(doc, dict):
        raise OSError("the scratch owner file is not one this module wrote")
    return {kind: _parse_marks(doc.get(kind)) for kind in _OWNER_KINDS}


def _write_owner_row(root: Path, kind: str, name: str, keys: frozenset[str] | None) -> bool:
    """Add *keys* to (or with None, drop) *name*'s *kind* row and read it back. Lock held."""
    try:
        rows = _read_owners(root)
        if keys is None:
            rows[kind].pop(name, None)
        else:
            rows[kind][name] = rows[kind].get(name, frozenset()) | keys
        doc = {
            k: {n: sorted(v) for n, v in rows[k].items() if agent_scratch._is_plain_dir(root / n)}
            for k in _OWNER_KINDS
        }
        payload = json.dumps(doc, sort_keys=True)
        if len(payload.encode("utf-8")) > _OWNERS_MAX_BYTES:
            return False  # the readable file stays as it was
        atomic_write(root / OWNERS_FILENAME, payload)
        written = doc[kind].get(name)
        return _read_owners(root)[kind].get(name) == (
            frozenset(written) if written is not None else None
        )
    except (OSError, agent_scratch.ScratchBoundaryError):
        logger.warning("agent-scratch: could not write the scratch owner file for %s", name)
        return False


def record_owner_marker(key: str, tree: Path) -> bool:
    """Record *key* as an owner of *tree* for a binding the index could not take."""
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name):
        return False
    with _TREE_INDEX_LOCK:
        return _write_owner_row(root, "owner", tree.name, frozenset({key}))


def record_live_co_owners(key: str, tree: Path) -> bool:
    """Record every owner of *tree* bound only in this process, before *key* joins it.

    A founder whose own index write failed is known only here; once another key
    shares the directory, a restart would forget it. True when nothing is owed or
    the owner file took them.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name):
        return True
    with _TREE_INDEX_LOCK:
        try:
            rows = _read_tree_index(root)
            recorded = _read_owners(root)["owner"].get(tree.name, frozenset())
        except (OSError, agent_scratch.ScratchBoundaryError):
            return False
        with _TREE_INDEX_LIVE_LOCK:
            live = {k for k, t in _TREE_INDEX_LIVE.items() if t == tree and k != key}
        owed = frozenset(k for k in live if rows.get(k) != tree.name and k not in recorded)
        return not owed or _write_owner_row(root, "owner", tree.name, owed)


def _refused_markers(root: Path, tree: Path) -> frozenset[str] | None:
    """The owners of *tree*'s refused row in the owner file, or None. Lock held.

    OSError when the file cannot be used: a refusal that cannot be read is not
    evidence the directory is safe to mount.
    """
    try:
        return _read_owners(root)["refused"].get(tree.name)
    except agent_scratch.ScratchBoundaryError as exc:
        raise OSError(str(exc)) from exc


def refused_across_trees(tree: Path) -> bool:
    """Whether *tree* carries a quarantine mark, or marks that cannot be read. Blocking.

    The one check every mount of a shared directory makes before handing it out:
    a resume, a spawn on a parent's behalf and a respawn.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return False
    # One acquisition, disk first: a hold that landed before it is seen, and none
    # can land during it.
    with _TREE_INDEX_LOCK:
        try:
            marks = _marks_locked(root)
            if _refused_markers(root, tree) is not None:
                return True
        except OSError:
            return True
        if _REFUSALS_OVERFLOWED or tree.name in _REFUSED_UNPERSISTED:
            return True
        return tree.name in marks


def quarantined_keys(tree: Path) -> frozenset[str] | None:
    """The keys *tree* was quarantined with, or None when it carries no mark.

    Raises :class:`OSError` when the marks cannot be read: a mark that cannot be
    read is not evidence the tree is safe to rejoin, so the caller refuses it.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return None
    with _TREE_INDEX_LOCK:
        marks = _marks_locked(root)
        sentinel = _refused_markers(root, tree)
    mark = marks.get(tree.name)
    if sentinel is None:
        return mark
    return (mark or frozenset()) | sentinel


def _marks_locked(root: Path) -> dict[str, frozenset[str]]:
    """The quarantine marks; the caller holds ``_TREE_INDEX_LOCK``. OSError when unreadable."""
    try:
        return _read_marks(root)
    except agent_scratch.ScratchBoundaryError as exc:
        raise OSError(str(exc)) from exc


def _write_marks(
    root: Path,
    marks: dict[str, frozenset[str]] | None,
    former: dict[str, frozenset[str]] | None = None,
) -> bool:
    """Rewrite the index with *marks* and *former* (None keeps either), bindings unchanged."""
    payload = _index_payload(root, _read_tree_index(root), marks, former=former)
    if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
        return False
    atomic_write(root / TREE_INDEX_FILENAME, payload)
    return True


def _keys_bound_to(root: Path, rows: dict[str, str], name: str) -> frozenset[str]:
    """Every key bound to *name*, on disk, live, or held in this process. Lock held.

    A binding whose index write failed lives in memory only, and it owns what it
    staged as much as a persisted one; a key whose disk row and live binding differ
    is counted for both, so ownership is weighed over every piece of evidence.
    """
    with _TREE_INDEX_LIVE_LOCK:
        live = {k for k, t in _TREE_INDEX_LIVE.items() if t.parent == root and t.name == name}
    # Owners held only in memory (a mark that did not fit) still own the directory.
    held = _REFUSED_UNPERSISTED.get(name, frozenset())
    return frozenset(k for k, v in rows.items() if v == name) | live | held


def _mark_before_leaving(
    root: Path, rows: dict[str, str], key: str, prior: Path | None = None
) -> bool:
    """Keep the owner set of the directory *key* is leaving as its former owners. Lock held.

    A key that moves away from a directory other keys are still bound to still
    owns what it staged there, so the owner set goes into that directory's former
    owners row before the row changes: without it, the key left behind would read
    as the directory's only owner and rejoin it. Not a refusal: a co-owner in the
    same tree keeps mounting the directory, and the next resume weighs every
    former owner (:func:`former_owners`). True when nothing is owed or it was
    persisted; False when it could not be, which holds the owed owners in this
    process (:func:`_hold_refusal`) and tells the caller to leave the disk row as
    it is. A directory gone from disk holds nothing and needs no mark.
    """
    # The disk row and the live binding can name different trees (a binding whose
    # write failed): the key is leaving both, so both are owed, all or nothing.
    names = {rows.get(key, ""), prior.name if prior is not None and prior.parent == root else ""}
    owed: dict[str, frozenset[str]] = {}
    for old in sorted(n for n in names if n and agent_scratch._is_plain_dir(root / n)):
        others = _keys_bound_to(root, rows, old) - {key}
        if others:
            owed[old] = others | {key}
    if not owed:
        return True
    try:
        former = _read_former(root)
        for old, owners in owed.items():
            former[old] = former.get(old, frozenset()) | owners
        if _write_marks(root, None, former):
            landed = _read_former(root)
            if all(landed.get(old) == former[old] for old in owed):
                return True
    except (OSError, agent_scratch.ScratchBoundaryError):
        pass
    # A mark is owed but did not land: this process holds each one, so no co-owner
    # here reads a directory as its sole owner's.
    for old, owners in owed.items():
        _hold_refusal(root, old, owners)
    return False


def quarantine_tree(tree: Path, keys: frozenset[str]) -> bool:
    """Mark *tree* as unresolved in the index, adding *keys* to any keys already marked."""
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name):
        return False
    with _TREE_INDEX_LOCK:
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            marks = _read_marks(root)
            marks[tree.name] = marks.get(tree.name, frozenset()) | keys
            if _write_marks(root, marks) and _read_marks(root).get(tree.name) == marks[tree.name]:
                _REFUSED_UNPERSISTED.pop(tree.name, None)  # the durable mark holds it now
                return True
        except (OSError, agent_scratch.ScratchBoundaryError):
            pass
        _hold_refusal(root, tree.name, keys)
        logger.error(
            "agent-scratch: could not persist the quarantine of %s; this process "
            "refuses it to every mount, and the next ownership check refuses it again",
            tree.name,
        )
    return False


def _mark_is_settled_by(mark: frozenset[str] | None, checked: frozenset[str]) -> bool:
    """Whether a caller that resolved *checked* to one tree may drop *mark*.

    Only when every owner in the mark was among the owners it checked: a mark
    that gained owners since stays.
    """
    return mark is None or mark <= checked


def clear_quarantine(tree: Path, checked: frozenset[str]) -> bool:
    """Drop *tree*'s mark and former owners once *checked* resolved to one tree.

    True when neither is left.

    A mark holding an owner outside *checked* (one added after the caller read
    it) is kept, and False is returned.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            marks = _read_marks(root)
            former = _read_former(root)
            if not _mark_is_settled_by(marks.get(tree.name), checked):
                return False
            if not _mark_is_settled_by(former.get(tree.name), checked):
                return False
            held = _REFUSED_UNPERSISTED.get(tree.name)
            if _REFUSALS_OVERFLOWED:
                held = (held or frozenset()) | {_OVERFLOW_OWNER}
            if not _mark_is_settled_by(held, checked):
                return False
            sentinel = _refused_markers(root, tree)
            if not _mark_is_settled_by(sentinel, checked):
                return False
            # The owners stay ownership evidence: one of them may later leave the tree.
            cleared = (
                (marks.get(tree.name) or frozenset())
                | (former.get(tree.name) or frozenset())
                | (sentinel or frozenset())
                | (_REFUSED_UNPERSISTED.get(tree.name) or frozenset())
            )
            if cleared and not _write_owner_row(root, "provenance", tree.name, cleared):
                return False
            if tree.name in marks or tree.name in former:
                marks.pop(tree.name, None)
                former.pop(tree.name, None)
                if not _write_marks(root, marks, former):
                    return False
                if tree.name in _read_marks(root) or tree.name in _read_former(root):
                    return False
            if sentinel is not None and not _write_owner_row(root, "refused", tree.name, None):
                return False
            _REFUSED_UNPERSISTED.pop(tree.name, None)  # settled: one tree owns it
            return True
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.warning(
                "agent-scratch: could not clear the quarantine of %s", tree.name, exc_info=True
            )
    return False


def keys_on_tree(tree: Path) -> frozenset[str]:
    """Every session key this process or the persisted index binds to *tree*.

    Read by the resume path to check that a recorded tree belongs to the
    resuming session's own session tree: the index can bind keys of unrelated
    trees to one directory, and a resume must not hand such a directory back.
    """
    root = agent_scratch.scratch_root()
    keys: set[str] = set()
    with _TREE_INDEX_LOCK:
        with _TREE_INDEX_LIVE_LOCK:
            keys.update(k for k, live in _TREE_INDEX_LIVE.items() if live == tree)
            if _TREE_INDEX_DISK_SNAPSHOT is not None and _TREE_INDEX_DISK_SNAPSHOT[0] == root:
                rows = _TREE_INDEX_DISK_SNAPSHOT[1]
                keys.update(k for k, name in rows.items() if name == tree.name)
        if tree.parent == root:
            try:
                keys.update(k for k, name in _read_tree_index(root).items() if name == tree.name)
            except (OSError, agent_scratch.ScratchBoundaryError):
                logger.debug("agent-scratch: could not read the tree index", exc_info=True)
            try:
                owners = _read_owners(root)
                keys.update(owners["owner"].get(tree.name, frozenset()))
                keys.update(owners["provenance"].get(tree.name, frozenset()))
            except (OSError, agent_scratch.ScratchBoundaryError):
                # quarantined_keys raises for the same file, so the caller refuses.
                logger.debug("agent-scratch: unreadable scratch owner file", exc_info=True)
    return frozenset(keys)


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
