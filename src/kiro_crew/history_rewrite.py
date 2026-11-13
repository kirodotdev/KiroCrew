"""Compaction rewrites and size-based rotation for conversation history.

``ConversationLog`` remains the identity-bearing owner of transcript paths,
locks, cache generations, and all mutable state.  This module owns the two
housekeeping rewrite workflows.  Only facade bindings with established
post-construction patch consumers are looked up at call time.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import (
    TRANSCRIPT_REPLACE_BUDGET_SECONDS,
    atomic_write,
    discard_replacement,
    fsync_dir,
    publish_replacement,
    stage_replacement,
)
from kiro_crew.owner_only_files import OWNER_ONLY_FILE_MODE

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")


def rewrite_transcript(path: Path, content: str | bytes) -> None:
    """Replace a whole transcript file with *content*, synced before the rename.

    Every history-store rename over a transcript goes through here (the
    dashboard save in ``chat_persistence`` keeps its own ``fsync=True`` call).
    Without ext4's ``auto_da_alloc`` an unsynced file renamed over the old one
    can come back empty after a power loss, so there is no ``fsync`` switch.
    The mode is owner-only on every root: a transcript outside the data home
    (an eval workspace) would otherwise follow the umask and widen.

    The rename carries ``TRANSCRIPT_REPLACE_BUDGET_SECONDS`` so a Windows reader
    holding the file open does not defeat it. The budget is spent only off the
    event loop; on the loop ``replace_with_retry`` re-raises at once.
    """
    atomic_write(
        path,
        content,
        fsync=True,
        mode=OWNER_ONLY_FILE_MODE,
        replace_budget_s=TRANSCRIPT_REPLACE_BUDGET_SECONDS,
    )


#: Marks a snapshot the unlocked pass could not take, so it never matches.
_UNREAD = object()


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    """``(inode, size, mtime_ns)`` of *path*, or ``None`` when it cannot be read."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _drop_unlanded_archive(
    archived: Path | None, path: Path, before: tuple[int, int, int] | None
) -> None:
    """Remove *archived* when the rewrite of *path* that just raised did not land.

    The transcript is unchanged when its identity still equals *before*, so the
    archive is a second copy of rows it still holds. Removing it keeps a lasting
    rewrite fault (a full disk) from adding one more copy on every retry. A
    rewrite that replaced the file before raising keeps the archive: it is then
    their only copy.
    """
    if archived is None or before is None or _file_identity(path) != before:
        return
    try:
        os.unlink(archived)
    except OSError:
        _HISTORY_LOGGER.warning(
            "Could not remove the archive %s of a rewrite that did not land",
            archived,
            exc_info=True,
        )


def _rotation_generation(metadata: dict[str, Any]) -> int:
    """The stored ``rotation_generation`` as ``int()`` reads it, or 0 when it cannot."""
    try:
        return int(metadata.get("rotation_generation", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


#: ``build(path, text)`` for :func:`rewrite_metadata_line`: *text* is the file as
#: ``Path.read_text`` returns it (``None`` when the file is absent), except that a
#: first line that is not UTF-8 arrives as a blank placeholder (see
#: :func:`_as_text`). The result is the whole replacement, or ``None`` to write
#: nothing.
MetadataLineBuild = Callable[[Path, "str | None"], "str | bytes | None"]


def _snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _as_text(data: bytes | None) -> str | None:
    # What Path.read_text() returns (UTF-8 with universal newlines), except a
    # first line that is not UTF-8 becomes a blank placeholder, so a metadata
    # write can rebuild that line instead of raising. Later rows decode strictly.
    if data is None:
        return None
    # Lazy: history_projection imports this module.
    from kiro_crew.history_projection import _split_transcript_bytes

    return "".join(_split_transcript_bytes(data))


#: The check :func:`commit_check` installs for the metadata rewrites it wraps.
_COMMIT_CHECK: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "kiro_crew_history_commit_check", default=None
)


@contextlib.contextmanager
def commit_check(validate: Callable[[], None]) -> Iterator[None]:
    """Run *validate* under the session lock before each metadata rewrite in this block commits.

    For a caller whose claim (a consolidation lease) must still hold at the moment
    of the rename: the replacement is staged and synced with no lock held, and
    *validate* runs first inside the commit lock, so a raise there commits nothing.
    """
    token = _COMMIT_CHECK.set(validate)
    try:
        yield
    finally:
        _COMMIT_CHECK.reset(token)


def rewrite_metadata_line(
    log: ConversationLog,
    key: str,
    build: MetadataLineBuild,
    validate: Callable[[], None] | None = None,
) -> None:
    """Replace a transcript whose metadata line changed, syncing outside the session lock.

    The sync grows with the transcript and every session writer waits on the
    lock, so ``build`` runs on an unlocked read and the result is staged and
    synced first. Under the lock the file must still hold exactly the bytes
    ``build`` read; any difference discards the stage and reruns ``build``
    under the lock, so nothing that landed in between is lost. The directory is
    synced after release, best effort: either complete file keeps every row.

    *validate* (default: the one :func:`commit_check` installed, if any) runs first
    under the lock; when it raises, the staged file is discarded and nothing is
    written.
    """
    if validate is None:
        validate = _COMMIT_CHECK.get()
    path = log._path(key)
    seen: object = _UNREAD
    staged: str | None = None
    # Resolved before staging, so no call runs between the staging pass and the
    # try whose finally discards an unpublished staged file.
    facade = _history_facade()
    published = False
    try:
        data = _snapshot(path)
        seen = data
        content = build(path, _as_text(data))
        if content is not None:
            # Owner-only on every root, as rewrite_transcript writes it.
            staged = stage_replacement(path, content, mode=OWNER_ONLY_FILE_MODE)
    except Exception:
        # The unlocked pass is an optimisation. Whatever stopped it (a read racing
        # a rename, a torn line, a full disk) is judged again under the lock.
        # stage_replacement is its last step and removes its own temp file when
        # it raises, so nothing is staged here.
        seen = _UNREAD
    try:
        with log._locked(key):
            if validate is not None:
                validate()
            current_path = log._path(key)
            current = _snapshot(current_path)
            if staged is not None and current_path == path and current == seen:
                prev_mtime = facade._safe_mtime(path)
                published = True  # publish_replacement owns the file from here
                publish_replacement(staged, path, budget_s=TRANSCRIPT_REPLACE_BUDGET_SECONDS)
            else:
                # Free the stale staged copy before writing a new one, so the
                # fallback never needs room for two whole-transcript copies.
                if staged is not None:
                    discard_replacement(staged)
                    staged = None
                path = current_path
                content = build(path, _as_text(current))
                if content is None:
                    return
                prev_mtime = facade._safe_mtime(path)
                rewrite_transcript(path, content)
            facade._restore_mtime(path, prev_mtime)
            # Inside the lock, so no reader publishes the old line under the new
            # generation.
            log._invalidate_cache(key)
    except BaseException:
        # published hands the staged file over before publish_replacement enters
        # its own try, so an interrupt in that gap reaches neither its cleanup nor
        # the finally below. Discarding a name the rename already moved is a no-op.
        if published and staged is not None:
            discard_replacement(staged)
        raise
    finally:
        if staged is not None and not published:
            discard_replacement(staged)
    fsync_dir(path.parent, best_effort=True)


def _history_facade() -> Any:
    """Return the facade lazily, after its component imports have completed."""
    from kiro_crew import history

    return history


def _facade_session_max_bytes() -> int:
    """Honor post-construction rebinds of the rotation byte budget."""
    return int(_history_facade()._SESSION_MAX_BYTES)


def _facade_session_keep_lines() -> int:
    """Read the paired rotation line budget from the compatibility facade."""
    return int(_history_facade()._SESSION_KEEP_LINES)


def _facade_archive_lines(
    key: str,
    lines: list[str],
    reason: str,
    *,
    base: Path | None = None,
) -> Path | None:
    """Honor post-construction patches of the facade archive helper."""
    return _history_facade()._archive_lines(key, lines, reason, base=base)


class HistoryRewriteCoordinator:
    """Coordinate transcript compaction rewrites and bounded rotation.

    The coordinator intentionally stores only the facade owner.  Locking,
    paths, metadata reads, cache state, and invalidation generations stay on
    the real ``ConversationLog`` object.
    Calls between facade entry points route back through that owner so replacing
    ``_rewrite_session_locked`` or ``_maybe_rotate`` on an instance remains an
    effective test and diagnostic seam.
    """

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

    def rewrite_session(self, key: str, messages: list[dict]) -> None:
        """Rewrite one session under its cross-process facade lock."""
        with self._log._locked(key):
            # Route through the facade so an instance-level patch remains the
            # seam observed by callers of ``rewrite_session``.
            self._log._rewrite_session_locked(key, messages)

    def _rewrite_session_locked(self, key: str, messages: list[dict]) -> None:
        """Rewrite one locked transcript, archiving only discarded rows."""
        path = self._log._path(key)
        self._log._dir.mkdir(parents=True, exist_ok=True)
        # Compaction is housekeeping rather than new activity.  Preserve the
        # pre-write mtime so session recency continues to reflect real appends.
        prev_mtime = _history_facade()._safe_mtime(path)
        before = _file_identity(path)
        archived: Path | None = None

        # Archive only rows the rewrite drops.  Normalized JSON comparison
        # keeps the decision insensitive to object key order while preserving a
        # malformed row in the archive rather than silently deleting it.
        if path.exists():
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if old_lines and '"_type"' in old_lines[0]:
                old_lines = old_lines[1:]
            kept_serialized = {json.dumps(message, sort_keys=True) for message in messages}
            dropped: list[str] = []
            for line in old_lines:
                if not line.strip():
                    continue
                try:
                    normalized = json.dumps(json.loads(line), sort_keys=True)
                except ValueError:
                    dropped.append(line)
                    continue
                if normalized not in kept_serialized:
                    dropped.append(line)
            # Fail CLOSED, as rotation does: the archive is the precondition of
            # the replace below, which removes these rows. Until it lands the
            # transcript holds their only copy, so an archive failure raises and
            # the transcript is left untouched rather than compacted anyway.
            archived = _facade_archive_lines(
                key,
                dropped,
                reason="compact",
                base=self._log._dir,
            )

        # Compaction owns only these four metadata fields.  Rotation identity,
        # retry accounting, and user-facing slot metadata are carried through
        # verbatim so a housekeeping rewrite cannot erase another layer's state.
        original_metadata = self._log.get_metadata(key) or {}
        metadata = {
            "_type": "metadata",
            "created_at": original_metadata.get(
                "created_at",
                _history_facade().metadata_now_iso(),
            ),
            "last_consolidated": original_metadata.get("last_consolidated", 0),
            "compacted_at": _history_facade().metadata_now_iso(),
        }
        _history_facade().carry_unowned_metadata(
            metadata,
            original_metadata,
            _history_facade()._COMPACT_OWNED_META_KEYS,
        )

        # Preserve the facade's JSONL byte format: default json.dumps spacing,
        # default ensure_ascii behavior, one LF per row, and a trailing LF.
        lines = [json.dumps(metadata) + "\n"]
        lines.extend(json.dumps(message) + "\n" for message in messages)
        # Synced before the rename (rewrite_transcript): the dropped rows are only
        # in the archive now, and an unsynced replacement can come back empty after
        # a power loss, taking the kept rows with it.
        try:
            rewrite_transcript(path, "".join(lines))
        except Exception:
            _drop_unlanded_archive(archived, path, before)
            raise
        _history_facade()._restore_mtime(path, prev_mtime)
        self._log._invalidate_cache(key)

    def _maybe_rotate(self, path: Path, key: str) -> None:
        """Rotate an oversized transcript while its facade lock is held.

        ``key`` is the logical spelling used by cache readers.  The path stem is
        lossy, so invalidation must use ``key`` even though archive filenames
        continue to use ``path.stem`` for byte-compatible behavior.
        """
        try:
            if path.stat().st_size <= _facade_session_max_bytes():
                return
        except OSError:
            return

        # Rotation follows a genuine append.  Keep that append's mtime instead
        # of restamping the session when the retained tail is rewritten.
        prev_mtime = _history_facade()._safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        metadata_line = lines[0] if lines and '"_type"' in lines[0] else ""
        message_lines = lines[1:] if metadata_line else lines[:]
        if not message_lines:
            return
        metadata_bytes = len(metadata_line.encode("utf-8"))

        def kept_bytes(count: int) -> int:
            return metadata_bytes + sum(
                len(line.encode("utf-8")) for line in message_lines[-count:]
            )

        # First apply the line cap, then shrink the tail until it also fits the
        # byte budget.  This handles sessions made of a few very large rows.
        keep_count = min(_facade_session_keep_lines(), len(message_lines))
        while keep_count > 1 and kept_bytes(keep_count) > _facade_session_max_bytes():
            keep_count -= 1
        if keep_count >= len(message_lines):
            # A single unsplittable message may exceed the complete budget.
            # Rewriting would drop nothing, so leave the transcript intact.
            return

        kept = message_lines[-keep_count:]
        dropped = message_lines[:-keep_count]
        before = _file_identity(path)
        try:
            archived = _facade_archive_lines(
                path.stem,
                dropped,
                reason="rotate",
                base=self._log._dir,
            )
        except Exception:
            # Fail closed: until the archive lands this transcript is the rows'
            # only copy, and an oversized file is recoverable where a dropped row
            # is not. Return rather than raise, so a full archive directory does
            # not fail the append that triggered rotation; the transcript is
            # untouched, so the read cache needs no invalidation.
            _HISTORY_LOGGER.warning(
                "Declining to rotate %s: archiving the rotated lines failed",
                path.stem,
                exc_info=True,
            )
            return

        # Retained row offsets have changed.  Reset consolidation progress and
        # advance the content identity so any in-flight consolidation or derived
        # sidecar tied to the old body is rejected even though mtime is restored.
        if metadata_line:
            try:
                metadata = json.loads(metadata_line)
                metadata["last_consolidated"] = 0
                metadata["rotated_at"] = _history_facade().metadata_now_iso()
                metadata["rotation_generation"] = _rotation_generation(metadata) + 1
                metadata_line = json.dumps(metadata) + "\n"
            except json.JSONDecodeError:
                pass

        # Synced before the rename, as compaction is: an unsynced replacement can
        # come back empty after a power loss, and the kept rows are in no archive.
        try:
            rewrite_transcript(path, metadata_line + "".join(kept))
        except Exception:
            _drop_unlanded_archive(archived, path, before)
            raise
        _history_facade()._restore_mtime(path, prev_mtime)
        self._log._invalidate_cache(key)
        _HISTORY_LOGGER.info(
            "Rotated session file %s (%d → %d lines)",
            path.name,
            len(lines),
            len(kept) + (1 if metadata_line else 0),
        )


__all__ = ["HistoryRewriteCoordinator"]
