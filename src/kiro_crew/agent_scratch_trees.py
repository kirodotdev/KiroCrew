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
#: tree's directory. No session key contains NUL, and the row's value is not a
#: tree name, so a reader that only wants bindings skips it.
_MARKS_ROW = "\x00quarantine"

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
        _is_tree_name(name)
        and (
            keys == _OVERFLOW_VALUE
            or (isinstance(keys, list) and all(isinstance(k, str) for k in keys))
        )
        for name, keys in value.items()
    ):
        raise OSError("the tree index holds a quarantine row this module did not write")
    return {
        name: _TOO_MANY_OWNERS if keys == _OVERFLOW_VALUE else frozenset(keys)
        for name, keys in value.items()
    }


def _merge_owners(held: frozenset[str], more: frozenset[str]) -> frozenset[str]:
    """Two owner sets as one; :data:`_TOO_MANY_OWNERS` once either is it or the bound is passed."""
    if held is _TOO_MANY_OWNERS or more is _TOO_MANY_OWNERS:
        return _TOO_MANY_OWNERS
    merged = held | more
    return _TOO_MANY_OWNERS if len(merged) > _QUARANTINE_MAX_OWNERS else merged


def _serialize_marks(root: Path, marks: dict[str, frozenset[str]]) -> dict[str, object] | None:
    live: dict[str, object] = {
        n: _OVERFLOW_VALUE if k is _TOO_MANY_OWNERS else sorted(k)
        for n, k in marks.items()
        if agent_scratch._is_plain_dir(root / n)
    }
    return live or None


def _index_payload(
    root: Path,
    rows: dict[str, str],
    marks: dict[str, frozenset[str]] | None = None,
    *,
    own: str | None = None,
    parent: tuple[str, str] | None = None,
) -> str:
    """The index text for *rows*, carrying the marks unless *marks* replaces them.

    Marks held in memory after a failed write are added to whichever marks are
    written, carried or given, except *own*'s: the caller that names it decided
    that directory's mark itself.
    A mark row this module cannot parse is carried unchanged, so a rewrite of
    the bindings never erases evidence that a directory was refused.
    """
    carried: object = None
    merged: dict[str, frozenset[str]] | None = None
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
    if marks is not None and any(name != own for name in _QUARANTINE_LIVE):
        # A mark held in memory after a failed write goes out with the next
        # write that fits, whichever writer that is.
        merged = dict(marks)
        for name, held in _QUARANTINE_LIVE.items():
            if name != own:
                merged[name] = _merge_owners(merged.get(name, frozenset()), held)
    if merged is not None:
        payload = _document(rows, _serialize_marks(root, merged), parents)
        if len(payload.encode("utf-8")) <= _TREE_INDEX_MAX_BYTES:
            return payload
        # Too large with the held marks: write without them, so a removal still
        # lands and the index stays readable; the marks stay held in memory. A
        # rewrite that takes an owner off a held directory while another owner
        # stays bound to it is refused: with no mark on disk, the owner left after
        # a restart would rejoin it.
        before = _read_tree_index(root)
        holding = {name for name in _QUARANTINE_LIVE if name != own}
        thinned = {name for k, name in before.items() if name in holding and rows.get(k) != name}
        if thinned & set(rows.values()):
            raise OSError("the tree index has no room for a held quarantine mark")
    if marks is not None:
        carried = _serialize_marks(root, marks)
    return _document(rows, carried, parents)


def _document(rows: dict[str, str], marks_row: object, parents: dict[str, str]) -> str:
    document: dict[str, object] = dict(rows)
    if marks_row is not None:
        document[_MARKS_ROW] = marks_row
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


def record_parent(key: str, parent: str) -> bool:
    """Record *parent* as the session the gateway spawned bound subagent *key* for.

    Gateway-only: the index lives in the masked root. The caller passes the spawn
    call's own parent, never a value the subagent supplied. True once read back.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            rows = _read_tree_index(root)
            if key not in rows:
                return False
            if _read_parents(root).get(key) == parent:
                return True
            payload = _index_payload(root, rows, parent=(key, parent))
            if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                return False
            atomic_write(root / TREE_INDEX_FILENAME, payload)
            return _read_parents(root).get(key) == parent
        except (OSError, agent_scratch.ScratchBoundaryError):
            logger.debug("agent-scratch: could not record a subagent's parent", exc_info=True)
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
                payload = _index_payload(root, rows)
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
    disk. A write that would not fit is not made: the in-memory bindings are
    still retired, so this process never hands *tree* back, and the disk rows
    stay for the caller to quarantine.
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
                return True
            if stat.S_ISLNK(info.st_mode) or platform_compat.lstat_is_name_surrogate(info):
                return True
            rows = _read_tree_index(root)
            doomed = {k for k, name in rows.items() if name == tree.name}
            _retire_live(doomed)
            marks = _read_marks(root)
            marks[tree.name] = _merge_owners(_current_mark(marks, tree.name) or frozenset(), owners)
            kept = {k: v for k, v in rows.items() if k not in doomed}
            payload = _index_payload(root, kept, marks, own=tree.name)
            if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
                return False
            atomic_write(root / TREE_INDEX_FILENAME, payload)
            if tree.name in _read_tree_index(root).values():
                return False
            if _read_marks(root).get(tree.name) != marks[tree.name]:
                return False
            _QUARANTINE_LIVE.pop(tree.name, None)  # the index holds it now
            return True
        except (OSError, agent_scratch.ScratchBoundaryError):
            _retire_live(set())
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
            del rows[key]
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


#: Marks this process could not persist, one per live scratch directory at most;
#: consulted with the index so a failed write never lets a later resume in this
#: process rejoin the directory.
_QUARANTINE_LIVE: dict[str, frozenset[str]] = {}

#: Most owner keys one unpersisted mark keeps in memory. A mark that would hold
#: more is kept as :data:`_TOO_MANY_OWNERS`, which refuses that directory.
_QUARANTINE_MAX_OWNERS = 256
_TOO_MANY_OWNERS: frozenset[str] = frozenset({"\x00too-many-owners"})
#: How :data:`_TOO_MANY_OWNERS` is written in the index, so it survives a restart.
_OVERFLOW_VALUE = "overflow"


def _read_marks(root: Path) -> dict[str, frozenset[str]]:
    return _parse_marks(_read_index_document(root, strict=True).get(_MARKS_ROW))


def refused_across_trees(tree: Path) -> bool:
    """Whether *tree* carries a quarantine mark, or marks that cannot be read. Blocking.

    The one check every mount of a shared directory makes before handing it out:
    a resume, a spawn on a parent's behalf and a respawn.
    """
    try:
        return quarantined_keys(tree) is not None
    except OSError:
        return True


def quarantined_keys(tree: Path) -> frozenset[str] | None:
    """The keys *tree* was quarantined with, or None when it carries no mark.

    Raises :class:`OSError` when the marks cannot be read: a mark that cannot be
    read is not evidence the tree is safe to rejoin, so the caller refuses it.
    """
    root = agent_scratch.scratch_root()
    if tree.parent != root:
        return None
    with _TREE_INDEX_LOCK:
        live = _QUARANTINE_LIVE.get(tree.name)
        if live is _TOO_MANY_OWNERS:
            raise OSError(f"the unpersisted quarantine of {tree.name} outgrew its bound")
        try:
            marks = _read_marks(root)
        except agent_scratch.ScratchBoundaryError as exc:
            raise OSError(str(exc)) from exc
    keys = marks.get(tree.name)
    if keys is _TOO_MANY_OWNERS:
        raise OSError(f"the quarantine of {tree.name} outgrew its bound")
    if keys is None and live is None:
        return None
    return (keys or frozenset()) | (live or frozenset())


def _write_marks(root: Path, marks: dict[str, frozenset[str]], own: str) -> bool:
    """Rewrite the index with *marks*, its bindings unchanged; True once read back."""
    payload = _index_payload(root, _read_tree_index(root), marks, own=own)
    if len(payload.encode("utf-8")) > _TREE_INDEX_MAX_BYTES:
        return False
    atomic_write(root / TREE_INDEX_FILENAME, payload)
    return True


def _hold_unpersisted_mark(root: Path, tree: Path, owners: frozenset[str]) -> None:
    """Keep a mark the index refused in memory until it can be written.

    Entries for directories the sweep has reclaimed are dropped, so memory never
    holds more marks than there are live scratch directories.
    """
    for name in [n for n in _QUARANTINE_LIVE if not agent_scratch._is_plain_dir(root / n)]:
        del _QUARANTINE_LIVE[name]
    _QUARANTINE_LIVE[tree.name] = _merge_owners(
        _QUARANTINE_LIVE.get(tree.name, frozenset()), owners
    )
    logger.error(
        "agent-scratch: could not persist the quarantine of %s; it holds in this "
        "process only and the next refusal writes it again",
        tree.name,
    )


def quarantine_tree(tree: Path, keys: frozenset[str]) -> bool:
    """Mark *tree* as unresolved in the index, adding *keys* to any keys already marked."""
    root = agent_scratch.scratch_root()
    if tree.parent != root or not _is_tree_name(tree.name):
        return False
    with _TREE_INDEX_LOCK:
        owners = _merge_owners(_QUARANTINE_LIVE.get(tree.name, frozenset()), keys)
        try:
            agent_scratch._refuse_linked(root, f"managed root {agent_scratch._SUBDIR!r}")
            marks = _read_marks(root)
            marks[tree.name] = _merge_owners(marks.get(tree.name, frozenset()), owners)
            if (
                _write_marks(root, marks, tree.name)
                and _read_marks(root).get(tree.name) == marks[tree.name]
            ):
                # The index is the record now (an outgrown mark as its overflow
                # value); memory keeps only marks that failed.
                _QUARANTINE_LIVE.pop(tree.name, None)
                return True
        except (OSError, agent_scratch.ScratchBoundaryError):
            pass
        _hold_unpersisted_mark(root, tree, owners)
    return False


def _current_mark(marks: dict[str, frozenset[str]], name: str) -> frozenset[str] | None:
    """*name*'s mark as it stands: the index's merged with any held in memory."""
    disk = marks.get(name)
    held = _QUARANTINE_LIVE.get(name)
    if disk is None and held is None:
        return None
    return _merge_owners(disk or frozenset(), held or frozenset())


def _mark_is_settled_by(mark: frozenset[str] | None, checked: frozenset[str]) -> bool:
    """Whether a caller that resolved *checked* to one tree may drop *mark*.

    Only when every owner in the mark was among the owners it checked: a mark
    that gained owners since, or overflowed and lost them, stays.
    """
    return mark is None or (mark is not _TOO_MANY_OWNERS and mark <= checked)


def clear_quarantine(tree: Path, checked: frozenset[str]) -> bool:
    """Drop *tree*'s mark once *checked* resolved to one tree; True when no mark is left.

    A mark holding an owner outside *checked* (one added after the caller read
    it) or an overflow mark is kept, and False is returned.
    """
    root = agent_scratch.scratch_root()
    with _TREE_INDEX_LOCK:
        try:
            marks = _read_marks(root)
            if not _mark_is_settled_by(_current_mark(marks, tree.name), checked):
                return False
            if tree.name in marks:
                del marks[tree.name]
                if not _write_marks(root, marks, tree.name) or tree.name in _read_marks(root):
                    return False
            _QUARANTINE_LIVE.pop(tree.name, None)
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
