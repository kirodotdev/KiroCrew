"""Compaction rewrites and size-based rotation for conversation history.

``ConversationLog`` remains the identity-bearing owner of transcript paths,
locks, cache generations, and all mutable state.  This module owns the two
housekeeping rewrite workflows.  Only facade bindings with established
post-construction patch consumers are looked up at call time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.atomic_write import atomic_write

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")


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
            try:
                _facade_archive_lines(
                    key,
                    dropped,
                    reason="compact",
                    base=self._log._dir,
                )
            except Exception:
                _HISTORY_LOGGER.warning(
                    "Failed to archive dropped lines for %s",
                    key,
                    exc_info=True,
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
        atomic_write(path, "".join(lines))
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

        atomic_write(path, metadata_line + "".join(kept))
        _history_facade()._restore_mtime(path, prev_mtime)
        self._log._invalidate_cache(key)
        _HISTORY_LOGGER.info(
            "Rotated session file %s (%d → %d lines)",
            path.name,
            len(lines),
            len(kept) + (1 if metadata_line else 0),
        )


__all__ = ["HistoryRewriteCoordinator"]
