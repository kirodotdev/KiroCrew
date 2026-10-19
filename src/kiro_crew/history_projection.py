"""Read and metadata projections for the conversation-history facade.

``ConversationLog`` remains the identity-bearing owner of transcript paths,
locks, caches, and process-wide invalidation generations.  These components
only implement focused operations against that owner.  Calls that form public
or test patch seams deliberately route back through the owner instead of
calling another component method directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib as _importlib
import json
import logging
import os
import secrets
import sys as _sys
import time as _time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, AbstractSet, Any, Literal, overload

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, replace_with_retry
from kiro_crew.chat_attachments import (
    purge_staged_attachments,
    restore_staged_attachments,
    stage_attachments_removal,
)
from kiro_crew.execution_context import STRICTEST_MEMORY_MODE
from kiro_crew.history_cache import (
    FileStamp,
    _FileChangeCacheEntry,
    _TranscriptIndexBase,
    _TranscriptRowIndexEntry,
)
from kiro_crew.history_projection_contract import (
    _HISTORY_LOGGER,
    METADATA_LINE_CORRUPT,
    TranscriptRevisionChanged,
    _history_facade,
)
from kiro_crew.jsonl_util import (
    bounded_raw_records,
    raise_if_splitlines_boundary,
    strict_raw_records_with_offsets,
)
from kiro_crew.preview_text import speech_preview
from kiro_crew.transcript_count import count_messages, message_row
from kiro_crew.warm_task_brief_contract import is_warm_task_brief_message

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


class SessionMetadataProjection:
    """Locked metadata mutation and permanent session deletion operations."""

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

    @overload
    def delete_session(
        self,
        key: str,
        *,
        skip_pinned: Literal[False] = ...,
    ) -> bool: ...

    @overload
    def delete_session(
        self,
        key: str,
        *,
        skip_pinned: Literal[True],
    ) -> bool | None: ...

    def delete_session(
        self,
        key: str,
        *,
        skip_pinned: bool = False,
    ) -> bool | None:
        """Delete a session atomically with its optional pin check.

        ``None`` means deletion was skipped because pinned metadata was
        unreadable or asserted the pin.  The lock sidecar intentionally remains:
        its inode is the cross-process mutex for any later recreation.
        """
        existed = False
        try:
            with self._log._locked(key):
                if skip_pinned:
                    try:
                        metadata, readable = self._log.get_metadata_status(key)
                    except Exception:
                        _HISTORY_LOGGER.warning(
                            "delete_session: unexpected error reading metadata " "for %s, skipping",
                            key,
                            exc_info=True,
                        )
                        return None
                    if not readable or not isinstance(metadata, dict):
                        return None
                    if metadata.get("pinned"):
                        return None
                path = self._log._path(key)
                existed = path.exists()
                # The search index holds a copy of this session's message text, so
                # it goes FIRST. Removing it before the transcript means a failure
                # here has destroyed nothing yet and the delete can abort cleanly;
                # the reverse order would leave the text readable in the index
                # after the transcript was already gone. drop() reports failure
                # rather than swallowing it for exactly this reason.
                #
                # EVERY spelling of the key is dropped, not the caller's one. Rows
                # are written under ``list_sessions``' key, which is the file's
                # ``stem``; a caller holding the logical form (``dashboard:mochi``)
                # names a row that does not exist, and an empty match is a
                # successful drop -- so the transcript would be unlinked while its
                # indexed text stayed readable. ``_cache_key_identities`` is the
                # existing owner of "every spelling of this transcript", used by
                # cache invalidation for the same reason.
                search_index = self._log._catalog_projection.search_index
                identities = set(self._log._cache_key_identities(key)) | {key, path.stem}
                if existed and search_index.available and not search_index.drop(identities):
                    _HISTORY_LOGGER.warning(
                        "delete_session: could not remove the search index row, "
                        "not deleting key=%s",
                        key,
                    )
                    return False
                # An index that exists on disk but cannot be opened is the one case
                # that must fail CLOSED: a copy of this session's text may be in it
                # and nothing here can remove it, so reporting the delete as done
                # would be a claim we cannot support. Removing the index file
                # unblocks it, which is why the path is named in the log.
                if existed and not search_index.available and search_index.store_exists():
                    _HISTORY_LOGGER.warning(
                        "delete_session: search index present but unreadable, so a "
                        "copy of this session's text may remain; not deleting "
                        "key=%s (remove the index to proceed)",
                        key,
                    )
                    return False
                # The images this session's messages showed are its content,
                # served by ``/api/file-raw`` the way the transcript's text is
                # served by the session view. They leave with the transcript in
                # three all-or-nothing steps: the attachments directory is moved
                # aside in ONE rename (a failure aborts with everything intact),
                # the transcript is unlinked (a failure moves the directory back,
                # so the retained rows still resolve), and only then are the
                # staged bytes purged -- nothing references them any more, so a
                # leftover is an orphan for an operator, never a served image.
                try:
                    staged = stage_attachments_removal(path.parent, path.stem)
                except OSError:
                    _HISTORY_LOGGER.warning(
                        "delete_session: cannot move attachments aside for key=%s, not deleting",
                        key,
                        exc_info=True,
                    )
                    return False
                # The reply threads are primary content too (replies cannot be
                # regenerated, unlike the summary caches below), so the sidecar
                # takes the same all-or-nothing route: moved aside in ONE rename
                # before the transcript goes, moved back if the transcript's
                # unlink fails, purged only once nothing references it. A
                # best-effort unlink after the transcript could leave the
                # replies behind while the delete reported success.
                threads_path = self._log.threads_sidecar_path(key)
                threads_staged: Path | None = None
                threads_dir_fd = -1
                if threads_path.parent.exists() and platform_compat.is_link_or_junction(
                    threads_path.parent
                ):
                    # A link where the sidecar directory should be would carry
                    # this delete outside the session store: not ours to touch,
                    # and not a state a delete may report success over.
                    if staged is not None:
                        restore_staged_attachments(staged, path.parent, path.stem)
                    _HISTORY_LOGGER.warning(
                        "delete_session: thread sidecar directory is a link for key=%s, "
                        "not deleting",
                        key,
                    )
                    return False
                # ``Path.exists`` swallows EVERY OSError as "absent", so a sidecar
                # the process cannot stat (EACCES, EIO, a stale mount) would read
                # as no sidecar and the transcript would go while the replies
                # stayed behind. Only a genuine absence lets the delete proceed
                # without the sidecar step; anything else fails closed.
                try:
                    os.lstat(threads_path)
                except FileNotFoundError:
                    threads_present = False
                except OSError:
                    if staged is not None:
                        restore_staged_attachments(staged, path.parent, path.stem)
                    _HISTORY_LOGGER.warning(
                        "delete_session: cannot inspect the thread sidecar for key=%s, "
                        "not deleting",
                        key,
                        exc_info=True,
                    )
                    return False
                else:
                    threads_present = True
                if threads_present:
                    # A fresh name per attempt, and a move that REFUSES an
                    # occupied destination: a staged sidecar left behind by an
                    # earlier delete whose rollback failed (or by a crash) is
                    # the only copy of those replies, and a same-named move
                    # aside would silently write over it.
                    threads_staged = threads_path.with_name(
                        f"{threads_path.name}.deleting-{os.getpid()}-{secrets.token_hex(4)}"
                    )
                    try:
                        if platform_compat.IS_POSIX:
                            # Pin the directory and move the leaf relative to it,
                            # so a parent swapped under us cannot redirect the move.
                            # link() is the exclusive step (EEXIST on a taken
                            # name); the unlink of the old name completes the move
                            # under the per-key lock, so no reader sees two names.
                            threads_dir_fd = os.open(
                                threads_path.parent,
                                os.O_RDONLY
                                | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0),
                            )
                            os.link(
                                threads_path.name,
                                threads_staged.name,
                                src_dir_fd=threads_dir_fd,
                                dst_dir_fd=threads_dir_fd,
                                follow_symlinks=False,
                            )
                            try:
                                os.unlink(threads_path.name, dir_fd=threads_dir_fd)
                            except OSError:
                                # Half a move: drop the second name so the
                                # sidecar is left exactly as it was.
                                with contextlib.suppress(OSError):
                                    os.unlink(threads_staged.name, dir_fd=threads_dir_fd)
                                raise
                        else:
                            # os.rename refuses an existing destination on Windows.
                            os.rename(threads_path, threads_staged)
                    except OSError:
                        if threads_dir_fd >= 0:
                            os.close(threads_dir_fd)
                        if staged is not None:
                            restore_staged_attachments(staged, path.parent, path.stem)
                        _HISTORY_LOGGER.warning(
                            "delete_session: cannot move the thread sidecar aside for key=%s, "
                            "not deleting",
                            key,
                            exc_info=True,
                        )
                        return False
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    if staged is not None:
                        restore_staged_attachments(staged, path.parent, path.stem)
                    if threads_staged is not None:
                        try:
                            if threads_dir_fd >= 0:
                                os.replace(
                                    threads_staged.name,
                                    threads_path.name,
                                    src_dir_fd=threads_dir_fd,
                                    dst_dir_fd=threads_dir_fd,
                                )
                            else:
                                os.replace(threads_staged, threads_path)
                        except OSError:
                            _HISTORY_LOGGER.warning(
                                "delete_session: thread sidecar left aside at %s for key=%s",
                                threads_staged,
                                key,
                                exc_info=True,
                            )
                    if threads_dir_fd >= 0:
                        os.close(threads_dir_fd)
                    return False
                if staged is not None:
                    purge_staged_attachments(staged)
                if threads_staged is not None:
                    try:
                        if threads_dir_fd >= 0:
                            os.unlink(threads_staged.name, dir_fd=threads_dir_fd)
                        else:
                            threads_staged.unlink(missing_ok=True)
                    except OSError:
                        # Nothing references the staged bytes any more: an
                        # orphan for an operator, never a served reply.
                        _HISTORY_LOGGER.warning(
                            "delete_session: staged thread sidecar %s not removed",
                            threads_staged,
                            exc_info=True,
                        )
                if threads_dir_fd >= 0:
                    os.close(threads_dir_fd)
                for sidecar in (
                    self._log._summary_cache_path(key),
                    self._log._intent_summary_cache_path(key),
                ):
                    try:
                        sidecar.unlink(missing_ok=True)
                    except OSError:
                        pass
        except _history_facade().HistoryLockTimeout:
            _HISTORY_LOGGER.warning(
                "delete_session: lock timeout, not deleting key=%s",
                key,
            )
            return False
        if existed:
            self._log._invalidate_cache(key)
            self._log.invalidate_tab_id_cache()
        return existed

    def set_title(self, key: str, title: str) -> None:
        """Persist a title into the session metadata line."""
        self._log.update_metadata(key, {"title": title})

    def update_metadata(self, key: str, fields: dict) -> None:
        """Merge fields under the same lock used by transcript writers."""
        with self._log._locked(key):
            self._log._update_metadata_locked(key, fields)
        if "tab_id" in fields:
            self._log.invalidate_tab_id_cache()

    def update_metadata_if(
        self,
        key: str,
        fields: dict,
        guard: Callable[[dict], bool],
        *,
        require_existing: bool = False,
        require_write: bool = False,
        after_commit_under_lock: Callable[[], None] | None = None,
    ) -> bool:
        """Merge fields only when the locked on-disk metadata passes a guard.

        *require_write* refuses a skipped write on a legacy message-first file.
        By default, a passing guard still returns True for that no-op; callers
        requiring durable fields must read them back. The commit callback runs
        only when the metadata line was actually written.

        *require_existing* additionally refuses a session that has no file at
        all. The guard cannot express that itself: :meth:`_read_metadata_status`
        answers ``({}, True)`` for an ABSENT path -- no metadata, reported as
        readable -- so through the dict the guard receives, a session DELETED
        since the caller's own read and one whose file carries no metadata line
        are the same value. :meth:`_update_metadata_locked` then upserts, so any
        caller whose guard accepts an empty record recreates a deleted session as
        a metadata-only line with no transcript behind it.

        Decided INSIDE the lock the write takes, which is the whole point: a
        deletion landing between a checked-then-written pair is precisely the
        window this closes, so the caller cannot do it for itself beforehand.
        ``after_commit_under_lock`` runs after the atomic metadata rewrite but
        before that lock is released. It is for an in-process witness whose
        ordering against readers must match the line commit; it must not perform
        I/O or mutate event-loop-affine state.

        Off by default, per caller rather than for everyone, because creating the
        line is the documented behaviour some callers depend on:
        ``bind_session_execution`` publishes a session's execution context and
        memory store into its record, and a session whose record does not exist
        yet must still end up carrying the mode it was admitted under. Refusing
        there would leave a restricted session with no durable record of being
        restricted, which is worse than the stub this flag prevents.
        Refuses to write over a line it cannot read -- with one distinction. A
        TRANSIENT read failure defers: the guard is not run and ``False`` is
        returned, so the caller retries later. A CORRUPT first line (bytes that
        are not JSON) will never become readable, and a writer that kept
        deferring on it would never persist anything again -- an empty tab's
        ``closed`` could never land and the tab would resurrect on every
        restart. So the guard is run against the line as the write below will
        REBUILD it: no identity, no store, ``memory_mode`` at
        :data:`~kiro_crew.execution_context.STRICTEST_MEMORY_MODE`. The line's
        real contract is unknowable and the ratchet forbids relabelling it
        looser, so the strictest mode is the only value the rewrite may carry;
        :meth:`_update_metadata_locked` enforces the same fold on the bytes it
        writes, whatever *fields* say.
        """
        with self._log._locked(key):
            if require_existing and not self._log._path(key).exists():
                return False
            # Peer metadata writes can preserve mtime; CAS must read disk under
            # the writer lock even when the local cache identity still matches.
            self._log._meta_cache.pop(key, None)
            metadata, readable = self._log._read_metadata_status(key)
            if not readable:
                if self._log.metadata_line_state(key) != METADATA_LINE_CORRUPT:
                    return False
                metadata = {"memory_mode": STRICTEST_MEMORY_MODE}
            if not guard(metadata):
                return False
            if not self._log._update_metadata_locked(key, fields):
                # Legacy callers use read-back to distinguish a skipped write;
                # lease callers require a persisted claim before reporting success.
                return not require_write
            if after_commit_under_lock is not None:
                after_commit_under_lock()
        if "tab_id" in fields:
            self._log.invalidate_tab_id_cache()
        return True

    def _update_metadata_locked(self, key: str, fields: dict) -> bool:
        """Merge or upsert one metadata line while the owner lock is held.

        Returns True when the line was written, False when the write was
        SKIPPED — a first line that is JSON but not a metadata object (a
        legacy row-first file) is left as it was, as before, and the caller's
        CAS can require a write rather than report a claim that is not on disk.

        A first line that is not JSON is REWRITTEN rather than left alone: the
        old bytes are dropped, the rows after them are kept, and the new line
        carries no ``created_at`` (the identity is unknowable, and a minted one
        would read to the owning slot's next save as a fresh incarnation born
        after a delete -- an absent field fails that check open, the way a
        legacy line does, and the slot's save restores its recorded identity),
        no ``memory_store`` (a restricted line names none) and ``memory_mode``
        at the strictest value, whatever *fields* carry. A first line that IS
        JSON but not a metadata object (a legacy row-first file) is left as it
        was, as before.
        """
        path = self._log._path(key)
        previous_mtime = _history_facade()._safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        healing_corrupt_line = False
        if lines:
            try:
                metadata = json.loads(lines[0])
            except json.JSONDecodeError:
                _HISTORY_LOGGER.warning(
                    "history: metadata line for %s is corrupt (not JSON); rewriting it "
                    "under the %s mode so the transcript can be written again",
                    key,
                    STRICTEST_MEMORY_MODE,
                )
                healing_corrupt_line = True
                metadata = {
                    "_type": "metadata",
                    "last_consolidated": 0,
                    "memory_mode": STRICTEST_MEMORY_MODE,
                }
            if not isinstance(metadata, dict):
                return False
            if metadata.get("_type") != "metadata":
                return False
        else:
            self._log._dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "_type": "metadata",
                "created_at": _history_facade().metadata_now_iso(),
                "last_consolidated": 0,
            }
            lines = [""]

        metadata.update(fields)
        if healing_corrupt_line:
            metadata["memory_mode"] = STRICTEST_MEMORY_MODE
            metadata.pop("memory_store", None)
        lines[0] = json.dumps(metadata) + "\n"

        # This hot one-line edit remains crash-atomic without paying for an
        # fsync while every other writer of the session is excluded.
        import tempfile

        data = "".join(lines).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
            # The shared retrying rename, not a bare ``os.replace``: on Windows the
            # rename fails with ``PermissionError`` while any other handle -- a
            # concurrent transcript READER -- is open on the destination, and this
            # path is hit right after a session writes, when readers are busiest.
            # Three unrelated test files flaked on exactly this line in five full
            # runs; the retry is what every other tmp-plus-rename writer here has.
            replace_with_retry(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        _history_facade()._restore_mtime(path, previous_mtime)
        self._log._invalidate_cache(key)
        return True

    def mtime_of(self, key: str) -> float | None:
        """Return a session file mtime without reading its contents."""
        try:
            return self._log._path(key).stat().st_mtime
        except OSError:
            return None

    def clear_closed(
        self,
        key: str,
        *,
        only_if_closed_before: float | None = None,
    ) -> None:
        """Remove a stale closed marker with an optional compare-and-clear."""
        with self._log._locked(key):
            path = self._log._path(key)
            if not path.exists():
                return
            previous_mtime = _history_facade()._safe_mtime(path)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not lines:
                return
            try:
                metadata = json.loads(lines[0])
            except json.JSONDecodeError:
                return
            if not isinstance(metadata, dict):
                return
            if metadata.get("_type") != "metadata" or "closed" not in metadata:
                return
            if only_if_closed_before is not None:
                raw = metadata.get("closed_at")
                try:
                    close_time = float(raw) if raw is not None else None
                except (TypeError, ValueError):
                    close_time = None
                if close_time is None:
                    close_time = previous_mtime
                if close_time is not None and close_time >= only_if_closed_before:
                    return
            metadata.pop("closed", None)
            metadata.pop("closed_at", None)
            lines[0] = json.dumps(metadata) + "\n"
            atomic_write(path, "".join(lines), fsync=False)
            _history_facade()._restore_mtime(path, previous_mtime)
            # Invalidate before releasing the lock so no preserved-mtime fill
            # can publish the pre-clear metadata under the current generation.
            self._log._invalidate_cache(key)


#: Imports this module keeps bound for its importers and for the patches the mirror
#: routes through it, though its own code does not read them.
_BOUND_FOR_IMPORTERS = (
    AbstractSet,
    Any,
    FileStamp,
    IO,
    Iterator,
    _FileChangeCacheEntry,
    _TranscriptIndexBase,
    _TranscriptRowIndexEntry,
    _time,
    asyncio,
    bounded_raw_records,
    count_messages,
    dataclass,
    datetime,
    hashlib,
    is_warm_task_brief_message,
    logging,
    message_row,
    raise_if_splitlines_boundary,
    speech_preview,
    strict_raw_records_with_offsets,
)
#: Tracebacks and logged error chains name this exception by the path callers import.
TranscriptRevisionChanged.__module__ = __name__
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = ("kiro_crew.history_projection_contract", "kiro_crew.history_projection_reads")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.history_projection_contract import (  # noqa: F401
        _TRANSCRIPT_INDEX_FINGERPRINT_BYTES,
        _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS,
        _TRANSCRIPT_PAGE_INDEX_STRIDE,
        _TRANSCRIPT_PAGE_READ_ATTEMPTS,
        _TRANSCRIPT_READ_CHUNK,
        DISPLAY_ONLY_ROLES,
        METADATA_LINE_READABLE,
        METADATA_LINE_TRANSIENT,
        ChainRevision,
        TranscriptPage,
        _facade_flock_acquire_timeout,
        _iter_splitlines,
        drop_persisted_tail_prefix,
    )
    from kiro_crew.history_projection_reads import TranscriptReadProjection  # noqa: F401
