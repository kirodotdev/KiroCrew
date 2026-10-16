"""Compaction rewrites and size-based rotation for conversation history.

``ConversationLog`` remains the identity-bearing owner of transcript paths,
locks, cache generations, and all mutable state.  This module owns the two
housekeeping rewrite workflows.  Only facade bindings with established
post-construction patch consumers are looked up at call time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import (
    atomic_write,
    discard_replacement,
    fsync_dir,
    publish_replacement,
    stage_replacement,
)

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")


def rewrite_transcript(path: Path, content: str | bytes) -> None:
    """Replace a whole transcript file with *content*, synced before the rename.

    The history store's writers that rename a new copy over a transcript go
    through here, whether they drop rows or only edit the metadata line (the
    dashboard save in ``chat_persistence`` keeps its own ``fsync=True`` call). The replacement holds
    every row the file keeps, and on a filesystem without ext4's
    ``auto_da_alloc`` an unsynced file renamed over the old one can come back
    empty or partial after a power loss, taking the whole conversation with it.
    There is deliberately no ``fsync`` switch, so a new caller cannot pick wrong.
    """
    atomic_write(path, content, fsync=True)


#: Marks a snapshot the unlocked pass could not take, so it never matches.
_UNREAD = object()

#: ``build(path, text)`` for :func:`rewrite_metadata_line`: *text* is the file as
#: ``Path.read_text`` returns it (``None`` when the file is absent), and the result
#: is the whole replacement, or ``None`` to write nothing.
MetadataLineBuild = Callable[[Path, "str | None"], "str | bytes | None"]


def _snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _as_text(data: bytes | None) -> str | None:
    # What Path.read_text() returns: UTF-8 with universal newlines.
    if data is None:
        return None
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def rewrite_metadata_line(log: ConversationLog, key: str, build: MetadataLineBuild) -> None:
    """Replace a transcript whose metadata line changed, syncing outside the session lock.

    The replacement carries every row, so it is synced before the rename. That
    flush grows with the transcript, and every writer of the session waits on the
    lock, so it is not paid there. ``build`` runs on an unlocked read, the result is
    staged and synced, and the lock is taken only to check that the file still holds
    exactly the bytes ``build`` read, then rename. Any difference (an append, another
    rewrite, a rotation, a different path for *key*) discards the staged file and
    runs ``build`` again under the lock, writing synced there, so no row or edit
    that landed in between is lost. The directory is synced after the lock is
    released, best effort: the rename has happened, and either complete file
    surviving a power loss keeps every row.
    """
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
            staged = stage_replacement(path, content)
    except Exception:
        # The unlocked pass is an optimisation. Whatever stopped it (a read racing
        # a rename, a torn line, a full disk) is judged again under the lock.
        # stage_replacement is its last step and removes its own temp file when
        # it raises, so nothing is staged here.
        seen = _UNREAD
    try:
        with log._locked(key):
            current_path = log._path(key)
            current = _snapshot(current_path)
            if staged is not None and current_path == path and current == seen:
                prev_mtime = facade._safe_mtime(path)
                published = True  # publish_replacement owns the file from here
                publish_replacement(staged, path)
            else:
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
            _facade_archive_lines(
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
        rewrite_transcript(path, "".join(lines))
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
        try:
            _facade_archive_lines(
                path.stem,
                dropped,
                reason="rotate",
                base=self._log._dir,
            )
        except Exception:
            # Fail CLOSED: archiving is a PRECONDITION of the rewrite below, not
            # a best-effort side effect. The rewrite is what removes these lines,
            # and until the archive write lands this transcript is their only
            # copy, so archiving best-effort and rewriting anyway turns a
            # recoverable unwritable archive directory into permanent transcript
            # loss. An oversized file is recoverable; a dropped row is not.
            #
            # Reported by returning rather than raising, because rotation is
            # housekeeping that runs after somebody else's append and a full
            # archive directory must not turn that append into a failure. The
            # transcript is untouched here, so the read cache stays accurate with
            # no invalidation.
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
                metadata["rotation_generation"] = (
                    int(metadata.get("rotation_generation", 0) or 0) + 1
                )
                metadata_line = json.dumps(metadata) + "\n"
            except json.JSONDecodeError:
                pass

        # Synced before the rename, as compaction is: an unsynced replacement can
        # come back empty after a power loss, and the kept rows are in no archive.
        rewrite_transcript(path, metadata_line + "".join(kept))
        _history_facade()._restore_mtime(path, prev_mtime)
        self._log._invalidate_cache(key)
        _HISTORY_LOGGER.info(
            "Rotated session file %s (%d → %d lines)",
            path.name,
            len(lines),
            len(kept) + (1 if metadata_line else 0),
        )


__all__ = ["HistoryRewriteCoordinator"]
