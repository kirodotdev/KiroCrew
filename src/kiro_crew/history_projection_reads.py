"""``TranscriptReadProjection``: the read side of the ``kiro_crew.history_projection`` family.

Every bounded transcript, tab-chain, rotated-archive, page, tail and metadata-line read
``ConversationLog`` delegates. ``kiro_crew.history_projection`` is the import path and the
patch surface; this owner imports only ``history_projection_contract`` from that family.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time as _time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, AbstractSet

from kiro_crew.history_cache import (
    FileStamp,
    _FileChangeCacheEntry,
    _TranscriptIndexBase,
    _TranscriptRowIndexEntry,
)
from kiro_crew.history_metadata_line import (
    METADATA_LINE_READABLE,
    METADATA_LINE_TRANSIENT,
    parse_metadata_line,
)
from kiro_crew.history_projection_contract import (
    _HISTORY_LOGGER,
    _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS,
    _TRANSCRIPT_PAGE_INDEX_STRIDE,
    _TRANSCRIPT_PAGE_READ_ATTEMPTS,
    ChainRevision,
    TranscriptPage,
    TranscriptRevisionChanged,
    _facade_flock_acquire_timeout,
    _history_facade,
    _iter_splitlines,
    drop_persisted_tail_prefix,
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


class TranscriptReadProjection:
    """Bounded transcript, metadata, and tab-chain read projections."""

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

    def forget_page_index(self, path: Path) -> None:
        """Remove *path*'s persisted page index (see :mod:`kiro_crew.history_page_index`)."""
        from kiro_crew import history_page_index

        history_page_index.forget(path)
        from kiro_crew.transcript_count import forget_count

        forget_count(path)

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Return the newest messages projected to role and content."""
        if exclude_last_n == 0 and self._log._tail_reads:
            tail = self._log._recent_via_tail(key, max_messages, roles)
            if tail is not None:
                return tail
        messages = self._log._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        messages = [message for message in messages if not is_warm_task_brief_message(message)]
        if roles:
            messages = [message for message in messages if message["role"] in roles]
        return [
            {"role": message["role"], "content": message["content"]}
            for message in messages[-max_messages:]
        ]

    def recent_chained(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Return recent messages across files sharing a tab identity."""
        messages = self._log.read_messages_chained(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        messages = [message for message in messages if not is_warm_task_brief_message(message)]
        if roles:
            messages = [message for message in messages if message["role"] in roles]
        return [
            {"role": message["role"], "content": message["content"]}
            for message in messages[-max_messages:]
        ]

    def recent_with_provenance(
        self,
        key: str,
        max_messages: int = 3,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Return recent source-bearing entries for cross-session citation.

        Only what was said is cited: the user's and the assistant's rows. A
        channel turn also records its tool steps with the turn's provenance, and
        a step's title in one of these few slots would push the question it
        answered out of the next session's context.
        """
        messages = self._log._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        result: list[dict] = []
        cited = [
            item
            for item in messages
            if item.get("source_thread")
            and item.get("role") in ("user", "assistant")
            and not is_warm_task_brief_message(item)
        ]
        for message in cited[-max_messages:]:
            content = message["content"]
            snippet = content[:150] + "…" if len(content) > 150 else content
            result.append(
                {
                    "source_thread": message["source_thread"],
                    "ts": message.get("ts", "?"),
                    "snippet": snippet,
                }
            )
        return result

    def recent_from_source(
        self,
        source_prefix: str,
        exclude_key: str = "",
        max_messages: int = 20,
    ) -> list[dict]:
        """Return recent messages from a bounded set of matching sessions."""
        if not self._log._dir.exists():
            return []
        safe_exclude = _history_facade()._safe_key(exclude_key) if exclude_key else ""
        safe_prefix = _history_facade()._safe_key(source_prefix)
        paths: list[Path] = []
        for path in self._log._dir.glob(f"{safe_prefix}*.jsonl"):
            if safe_exclude and path.stem == safe_exclude:
                continue
            paths.append(path)
        paths.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)

        candidates: list[dict] = []
        included = 0
        max_scan = 50
        for path in paths[:max_scan]:
            if included >= 5:
                break
            is_restricted = False
            try:
                with open(path, encoding="utf-8") as handle:
                    for _, line in zip(range(5), handle):
                        try:
                            data = json.loads(line.strip())
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get(
                            "_type"
                        ) == "metadata" and _history_facade().is_incognito_transcript(
                            data.get("memory_mode")
                        ):
                            is_restricted = True
                            break
            except OSError:
                continue
            if is_restricted:
                continue
            included += 1
            candidates.extend(self._log._read_tail_messages(path, 50, None))
        candidates = [message for message in candidates if not is_warm_task_brief_message(message)]
        candidates.sort(key=lambda message: message.get("ts", ""))
        return [
            {"role": message["role"], "content": message["content"]}
            for message in candidates[-max_messages:]
        ]

    def read_messages(self, key: str) -> list[dict]:
        """Return the shared read-only cached transcript projection."""
        return self._log._read_messages(key)

    def read_file_change_messages(self, key: str) -> list[dict]:
        """Return lightweight rows carrying only ``ts`` and the changed paths.

        Each row is ``{"ts", "role", "meta": {"file_changes": [{"path": str}, ...]}}``,
        plus ``meta.file_changes_omitted_files`` when the row records files its
        turn's budget left out;
        change entries without a string ``path`` and rows left with none are
        dropped, and every other field of a change (``before``/``after``) is
        not kept.
        """
        path = self._log._path(key)
        generation = self._log._cache_gen(key)
        try:
            before = path.stat()
        except FileNotFoundError:
            self._log._file_change_cache.pop(key, None)
            return []
        stamp = (before.st_mtime_ns, before.st_size, before.st_ino, before.st_dev)
        cached = self._log._file_change_cache.get(key)
        if (
            cached is not None
            and cached.stamp == stamp
            and cached.generation == self._log._cache_gen(key)
        ):
            return cached.messages

        messages: list[dict] = []
        with open(path, "rb") as handle:
            for raw in bounded_raw_records(handle, path, label="history_projection"):
                if b'"file_changes"' not in raw:
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("_type") == "metadata":
                    continue
                meta = data.get("meta")
                if not isinstance(meta, dict):
                    continue
                file_changes = meta.get("file_changes")
                if not isinstance(file_changes, list):
                    continue
                # Keep only the string ``path`` of each change: the one reader
                # (the Artifacts session-docs scan) uses nothing else, and the
                # ``before``/``after`` bodies made the cache ~112 MB on a real
                # store. Entries without a string path were skipped by that
                # reader anyway, so dropping them here changes no output.
                # ``omitted_reason`` is kept too, a short marker: the Files tab
                # skips a write whose outcome was never proven.
                paths = [
                    {"path": fc["path"]}
                    | (
                        {"omitted_reason": fc["omitted_reason"]}
                        if isinstance(fc.get("omitted_reason"), str)
                        else {}
                    )
                    for fc in file_changes
                    if isinstance(fc, dict) and isinstance(fc.get("path"), str)
                ]
                # How many changed files the turn's budget left out of
                # ``file_changes``: a reader listing them must know the row is short.
                omitted = meta.get("file_changes_omitted_files")
                if not (isinstance(omitted, int) and not isinstance(omitted, bool) and omitted > 0):
                    omitted = 0
                if not paths and not omitted:
                    continue
                kept_meta: dict = {"file_changes": paths}
                if omitted:
                    kept_meta["file_changes_omitted_files"] = omitted
                messages.append({"ts": data.get("ts"), "role": data.get("role"), "meta": kept_meta})

        try:
            after = path.stat()
        except OSError:
            return messages
        after_stamp = (after.st_mtime_ns, after.st_size, after.st_ino, after.st_dev)
        if after_stamp == stamp:
            self._log._publish_if_current(
                self._log._file_change_cache,
                key,
                _FileChangeCacheEntry(stamp, generation, messages),
                key=key,
                gen=generation,
            )
        return messages

    def _chain_keys(self, key: str) -> list[str]:
        """Return the established chronological tab chain for *key* (never empty)."""
        return self.chained_keys(key) or [key]

    def chained_keys(self, key: str) -> list[str]:
        """Every transcript key ``read_messages_chained(key)`` concatenates, in order.

        EMPTY when the key carries no ``tab_id`` or the index knows no chain for
        it: the chained read then serves ``_read_messages(key)``'s object itself
        (on a cache hit the shared cached list, which callers treat as immutable
        and the fork path relies on by identity), while any indexed chain -- a
        single member included -- builds a fresh concatenation, exactly as
        before. Exposed so a caller that must validate or lock EVERY file the
        chained read touches (the derivation seam) can name them before reading;
        such a caller reads an empty result as ``[key]``.
        """
        metadata = self._log.get_metadata(key)
        tab_id = metadata.get("tab_id")
        if not tab_id:
            return []
        with self._log._lock:
            if self._log._tab_id_index is None:
                self._log._rebuild_tab_id_index()
            index = self._log._tab_id_index or {}
            return list(index.get(tab_id, []))

    def read_messages_chained(self, key: str) -> list[dict]:
        """Concatenate chronologically ordered files sharing the same tab id."""
        keys = self.chained_keys(key)
        if not keys:
            return self._log._read_messages(key)
        messages: list[dict] = []
        for chained_key in keys:
            messages.extend(self._log._read_messages(chained_key))
        return messages or self._log._read_messages(key)

    def read_messages_chained_tail(self, key: str, limit: int) -> list[dict]:
        """The newest *limit* rows of :meth:`read_messages_chained`, read from the end.

        Reads only the end of the chain's newest file, growing back as far as
        *limit* rows need, and skips the lines the full read skips, so the cost
        follows *limit* and not the transcript's size. Bytes that are not UTF-8
        are replaced where the full read would raise, which suits a display
        tail; anything that needs the rows themselves reads them in full. When
        that file alone cannot fill *limit* and the chain holds other files, the
        exact chained page read answers instead.
        """
        if limit <= 0:
            return []
        keys = self._chain_keys(key)
        if self._log._tail_reads:
            newest = self._log._read_tail_messages(self._log._path(keys[-1]), limit, None)
            if len(newest) >= limit or keys == [key]:
                return newest
        return self.read_messages_chained_page(key, limit=limit).messages

    def read_rotated_messages(self, key: str) -> list[dict]:
        """Messages size-rotation archived out of *key*'s transcript, oldest first.

        Only ``reason="rotate"`` segments participate: those are the transcript's
        own head, moved aside verbatim when the file outgrew its byte budget.
        Every other archive reason (``compact``, ``foreign-dedup``, rewrite
        drops) is content the product DISCARDED — a rewind, a regenerate, a
        dedup — and resurfacing it would undo that edit in the reader's view.

        Cached per key on the segment files' stat signature: pagination re-reads
        the corpus once per page and archives change only on a rotation, so the
        multi-MB parse must not repeat per scroll step.
        """
        facade = _history_facade()
        adir = facade._archive_dir(self._log._dir)
        stem = facade._safe_key(key) + facade.ARCHIVE_SEGMENT_DELIMITER

        def _seg_order(p: Path) -> tuple[str, int]:
            # Same-second segments carry ``-N`` suffixes; lexicographic order
            # puts ``-10`` before ``-2``, so split the numeric part out.
            rest = p.name[len(stem) : -len(".jsonl")]
            stamp, dash, suffix = rest.partition("-")
            # The stamp itself contains one dash (YYYYMMDD-HHMMSS): re-join,
            # then look for a SECOND dash carrying the collision counter.
            if dash:
                stamp2, dash2, suffix2 = suffix.partition("-")
                stamp = f"{stamp}-{stamp2}"
                suffix = suffix2 if dash2 else ""
            try:
                n = int(suffix) if suffix else 0
            except ValueError:
                n = 0
            return (stamp, n)

        try:
            segs = sorted((p for p in adir.glob(f"{stem}*.jsonl")), key=_seg_order)
        except OSError as exc:
            # `return []` here is indistinguishable from "this session never
            # rotated", and that is the one answer this function must not guess.
            # An unreadable archive DIRECTORY hides an unknown number of rows, so
            # every index above them moves and the reader is told their older
            # history does not exist. Raise and let the handlers answer with the
            # retryable 503 they already have.
            raise OSError(f"rotated archive for {key} could not be enumerated") from exc
        if not segs:
            return []
        sig: list[tuple[str, int, int]] = []
        for p in segs:
            try:
                st = p.stat()
            except OSError as exc:
                # The signature is the cache key. A segment we cannot stat would
                # be silently absent from it, so a later read of a CHANGED
                # archive would hit a stale entry and serve the wrong corpus.
                raise OSError(f"rotated segment {p.name} could not be stat'd") from exc
            sig.append((p.name, st.st_size, st.st_mtime_ns))
        cache: dict[str, tuple[list[tuple[str, int, int]], list[dict]]] | None = getattr(
            self, "_rotated_cache", None
        )
        if cache is None:
            cache = {}
            self._rotated_cache = cache
        hit = cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        rows: list[dict] = []
        prev_seg_rows: list[dict] = []
        # A transient segment read failure is neither cached nor served. A
        # finished rotation's stat signature never changes, so a cached empty
        # entry would hide the transcript's head until restart. Serving it
        # would shift every cursor and fork index above the lost rows; both
        # consumers turn a raised failure into a retryable 503 instead.
        complete = True
        for p in segs:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                _HISTORY_LOGGER.warning("rotated segment unreadable: %s", p.name, exc_info=True)
                complete = False
                continue
            if not lines:
                # A zero-byte segment. Archiving is a PRECONDITION of the live-file
                # rewrite, so a rotation that got this far and no further never
                # rewrote the live file — those rows are still in it, and a healthy
                # read of an empty file contributes nothing either. Skipping here
                # changes no index.
                continue
            try:
                header = json.loads(lines[0])
            except ValueError:
                # A damaged header on a segment whose rows may be perfectly intact.
                # Unlike the classification skip below, a healthy read WOULD have
                # contributed those rows, so dropping them shortens the corpus and
                # shifts every index above it. Worse, a retry rotation can archive
                # the same rows successfully, and then a partial read of this
                # segment duplicates them.
                _HISTORY_LOGGER.warning("rotated segment header unparseable: %s", p.name)
                complete = False
                continue
            if not isinstance(header, dict):
                # DAMAGE, not classification. A header that parses as valid JSON
                # but is not an object (`[]`, a bare string) is not "some other
                # archive reason" -- no writer produces that shape, so the segment
                # is corrupt and its rows are unaccounted for.
                _HISTORY_LOGGER.warning("rotated segment header is not an object: %s", p.name)
                complete = False
                continue
            if header.get("reason") != "rotate":
                # CLASSIFICATION, not damage: `compact`, `foreign-dedup` and rewrite
                # archives are other reasons, and this corpus is size-rotation only
                # (see the docstring). A healthy read skips these too, so this one
                # must stay a plain skip.
                continue
            seg_rows: list[dict] = []
            for ln in lines[1:]:
                if not ln.strip():
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    # Same reasoning as the header: a healthy read yields this row,
                    # so silently dropping it moves every index above it.
                    _HISTORY_LOGGER.warning("rotated segment row unparseable in %s", p.name)
                    complete = False
                    continue
                if not isinstance(row, dict):
                    # Damage again, for the same reason the header check gives.
                    _HISTORY_LOGGER.warning("rotated segment row is not an object in %s", p.name)
                    complete = False
                    continue
                if "_type" not in row:
                    # `_type` rows are deliberate control records, not messages --
                    # skipping them IS the classification this corpus wants.
                    seg_rows.append(row)
            # A crash between archiving the live head and rewriting the live
            # file makes the next rotation re-archive the same rows, and a
            # duplicate shifts every cursor and fork index above it. A row is
            # dropped only on one of two proofs, since deleting a real row is
            # worse than serving a duplicate:
            # 1. Provenance: the first min(len) rows of adjacent segments are
            #    identical records, a shape no organic transcript produces.
            # 2. Identity: past that prefix, equal ``meta.mid``
            #    (``require_mid=True``), with no ``(ts, role, content)`` fallback.
            # The archive-to-live seam is handled in read_messages_chained_full.
            provenance = 0
            k = min(len(prev_seg_rows), len(seg_rows))
            if k and seg_rows[:k] == prev_seg_rows[:k]:
                provenance = k
            remainder = seg_rows[provenance:]
            merged = drop_persisted_tail_prefix(rows, remainder, require_mid=True)
            dropped = provenance + (len(remainder) - len(merged))
            if dropped:
                # A fired dedupe is the on-disk signature of a rotation that
                # crashed inside the archive-to-rewrite window. Every other
                # anomaly in this reader logs; dropping rows silently would
                # make a genuine (mis)drop unattributable in the field.
                _HISTORY_LOGGER.warning(
                    "rotated segment %s overlaps the corpus: dropped %d duplicate row(s)",
                    p.name,
                    dropped,
                )
            rows.extend(merged)
            prev_seg_rows = seg_rows
        if complete:
            if not rows:
                # Segments exist yet no rotate rows parsed — the signature of a
                # transcript head gone missing. Loud on purpose: this state should be
                # impossible for a healthy rotation archive.
                _HISTORY_LOGGER.warning(
                    "rotated archive for %s: %d segment(s) matched but 0 rows parsed",
                    key,
                    len(segs),
                )
            cache[key] = (sig, rows)
            # One entry per key is enough; a stale key's rows would pin MBs forever.
            if len(cache) > 8:
                cache.pop(next(iter(cache)))
            return rows
        raise OSError(
            f"rotated archive for {key} is incomplete: at least one segment could not be read"
        )

    def read_messages_chained_full(self, key: str) -> list[dict]:
        """`read_messages_chained` plus each key's size-rotated archive head.

        Per chain key the rotated rows PRECEDE the live file's rows — rotation
        drops the file's head, so segment order (filename timestamp) followed by
        the surviving file is that key's true chronology, and whole-key blocks
        are already chronological in chain order. This is the pagination corpus:
        the index space `before`/`next_before` cursors live in, shared with the
        fork index path so a rendered row's index resolves to the same message
        everywhere.
        """
        metadata = self._log.get_metadata(key)
        tab_id = metadata.get("tab_id")
        keys: list[str] = []
        if tab_id:
            with self._log._lock:
                if self._log._tab_id_index is None:
                    self._log._rebuild_tab_id_index()
                index = self._log._tab_id_index or {}
                keys = list(index.get(tab_id, []))
        if not keys:
            keys = [key]
        messages: list[dict] = []
        for chained_key in keys:
            # LIVE FIRST, then the archive. These are two separate snapshots, so a
            # rotation can land between them, and the ORDER decides which way that
            # race fails. Archive-then-live loses rows: the archive snapshot
            # predates the rotation so it lacks the newly-archived rows, the live
            # snapshot postdates the head rewrite that removed them, and those rows
            # appear in NEITHER — silently absent, with every index above them
            # shifted. Live-then-archive can only ever DUPLICATE them, which is the
            # exact condition `drop_persisted_tail_prefix` below already removes.
            live = self._log._read_messages(chained_key)
            rotated = self.read_rotated_messages(chained_key)
            # Rotation archives first and rewrites the live head second, so it
            # never loses the only copy; a crash in between leaves rows in both
            # files. A duplicate would shift every cursor and fork index above it.
            live = drop_persisted_tail_prefix(rotated, live)
            messages.extend(rotated)
            messages.extend(live)
        return messages

    def read_rotated_messages_chained(self, key: str) -> list[dict]:
        """All rotate-archived rows across *key*'s tab chain, chain order."""
        metadata = self._log.get_metadata(key)
        tab_id = metadata.get("tab_id")
        keys: list[str] = []
        if tab_id:
            with self._log._lock:
                if self._log._tab_id_index is None:
                    self._log._rebuild_tab_id_index()
                index = self._log._tab_id_index or {}
                keys = list(index.get(tab_id, []))
        if not keys:
            keys = [key]
        rows: list[dict] = []
        for chained_key in keys:
            rows.extend(self.read_rotated_messages(chained_key))
        return rows

    def chain_mid_rotation(self, key: str) -> bool:
        """True when any chain member AFTER the first has archive segments.

        The slot-detail fast path's single `next_before` cursor is exact only
        when the archived rows are a prefix of the chained corpus, i.e. only the
        first member rotated; otherwise the caller must serve the full corpus.

        Stat-only and conservative: a non-rotate segment (``compact`` etc.) also
        answers True, which is slower but never wrong, and avoids a parse per
        segment.
        """
        metadata = self._log.get_metadata(key)
        tab_id = metadata.get("tab_id")
        keys: list[str] = []
        if tab_id:
            with self._log._lock:
                if self._log._tab_id_index is None:
                    self._log._rebuild_tab_id_index()
                index = self._log._tab_id_index or {}
                keys = list(index.get(tab_id, []))
        if len(keys) <= 1:
            return False
        facade = _history_facade()
        adir = facade._archive_dir(self._log._dir)
        for chained_key in keys[1:]:
            stem = facade._safe_key(chained_key) + facade.ARCHIVE_SEGMENT_DELIMITER
            try:
                if next(adir.glob(f"{stem}*.jsonl"), None) is not None:
                    return True
            except OSError as exc:
                # NOT `continue`. This predicate is consumed as a decision, not a
                # hint: False selects the prefix-cursor path, which is correct only
                # when the rotation is on the FIRST chain member. Swallowing the
                # failure lets the loop finish and answer False, so "cannot tell"
                # becomes "definitely not mid-chain" -- and if it actually was, the
                # sandwiched archived rows go unreachable and an index-addressed
                # fork picks the wrong cutoff. Both callers turn this into their
                # retryable 503.
                raise OSError(
                    f"mid-rotation probe for {chained_key} could not enumerate the archive"
                ) from exc
        return False

    @staticmethod
    def _message_record(raw: bytes) -> dict | None:
        """Decode one record using the full-reader's skip semantics.

        Malformed JSON is skipped exactly as the full reader skips it. Bytes that
        are not valid UTF-8 are NOT skipped: the full reader's strict text-mode
        read fails closed on them, and a skipped row here would undercount
        ``total`` and shift every cursor above it. The strict ``decode`` below raises
        ``UnicodeDecodeError`` before any JSON parsing. Decoding explicitly, rather than
        letting ``json.loads(bytes)`` sniff a BOM or accept ``surrogatepass``
        escapes, keeps this reader's notion of a valid row identical to the strict
        text-mode full reader's. A record the full reader's ``splitlines()``
        would break into several fragments is refused outright
        (:class:`SplitlinesBoundaryRecord`): the caller hands the read to that
        reader instead of building an index that disagrees with it.
        """
        text = raw.decode("utf-8")
        raise_if_splitlines_boundary(text)
        # Match the full reader exactly: ``str.strip`` (Unicode whitespace) like
        # its per-line strip, and skip only ``json.JSONDecodeError``. Any other
        # ``ValueError`` (an int past ``sys.get_int_max_str_digits()``) fails
        # the full reader closed, so it propagates here; swallowing it would
        # shift every cursor above.
        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("_type") == "metadata":
            return None
        return data

    @staticmethod
    def _file_stamp(path: Path) -> FileStamp | None:
        """Stat ``path``; ``None`` only when it does not exist.

        Only a missing file is the empty transcript. Any other ``OSError`` (a
        permission denial, an I/O error) propagates so the caller retries or
        answers unreadable, instead of serving ``total=0`` for history that is
        there but could not be stat'ed.

        ``st_ctime_ns`` is part of the stamp because a writer outside this
        process can restore ``mtime`` after a same-size in-place rewrite, and
        such a writer never bumps the invalidation generation; on POSIX
        ``ctime`` cannot be set from user space, so that rewrite still misses
        the cache. (Windows reports creation time here, so the component is
        inert there and the generation remains the in-process guard.)
        """
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino, stat.st_dev)

    @staticmethod
    def _handle_extends_base(
        handle: IO[bytes], base: _TranscriptIndexBase
    ) -> _TranscriptIndexBase | None:
        """*base* moved onto the open file when that file still holds its rows, else ``None``.

        Judged on the handle the caller then scans, never on the path, so a file
        replaced between the check and the scan cannot be resumed from another
        file's offsets. See :func:`kiro_crew.history_page_index.relocate`: the
        content decides, so a copied, restored or rewritten file with the same rows
        resumes, and one whose rows changed is rescanned.
        """
        from kiro_crew import history_page_index

        return history_page_index.relocate(handle, base)

    def _row_index(self, key: str) -> _TranscriptRowIndexEntry:
        """Return a sparse valid-message-row index for one file revision."""
        from kiro_crew import history_page_index

        path = self._log._path(key)
        generation = self._log._cache_gen(key)
        stamp = self._file_stamp(path)
        if stamp is None:
            # No file yet (a slot that has not flushed): report a stable empty
            # revision. ``None`` is what the stability re-stat also returns.
            self._log._page_index_cache.pop(key, None)
            return _TranscriptRowIndexEntry(None, generation, 0, ())
        cached = self._log._page_index_cache.get(key)
        if cached is not None and cached.stamp == stamp and cached.generation == generation:
            return cached

        row_count = 0
        stride = _TRANSCRIPT_PAGE_INDEX_STRIDE
        checkpoints: list[tuple[int, int]] = []
        resume_at = 0
        scanned_end = 0
        ends_on_newline = False
        with open(path, "rb") as handle:
            remembered = self._log._page_index_base.get(key)
            base = None if remembered is None else self._handle_extends_base(handle, remembered)
            if base is None:
                # Nothing in memory that fits this file -- a new process, or a
                # rewrite that changed rows -- so try the persisted base before
                # framing from byte 0. It passes the same check or is ignored.
                persisted = history_page_index.load(path)
                if persisted is not None:
                    base = self._handle_extends_base(handle, persisted)
            if base is not None:
                # The saved prefix still holds the same rows (moved by the first
                # line's change in length), so only the rows after it need scanning.
                row_count = base.row_count
                checkpoints = list(base.checkpoints)
                resume_at = base.scanned_end
                scanned_end = base.scanned_end
                ends_on_newline = True  # a base is only ever saved at a newline
                if len(checkpoints) > 1:
                    stride = checkpoints[1][0] - checkpoints[0][0]
            handle.seek(resume_at)
            records = strict_raw_records_with_offsets(handle, path)
            for start, end, raw in records:
                scanned_end = end
                ends_on_newline = raw.endswith(b"\n")
                if self._message_record(raw) is None:
                    continue
                if row_count % stride == 0:
                    checkpoints.append((row_count, start))
                    if len(checkpoints) > _TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS:
                        # Bound the retained index: keep every other checkpoint and
                        # double the stride. Kept rows are multiples of the old
                        # stride at even positions, hence of the new stride too.
                        checkpoints = checkpoints[::2]
                        stride *= 2
                row_count += 1
            if scanned_end and ends_on_newline:
                # Keep where this scan stopped so the next one can resume from it.
                # The digest is read from the handle that was scanned, so it
                # describes the same file the offsets came from.
                head_len = (
                    base.head_len if base is not None else history_page_index.head_length(handle)
                )
                frozen = tuple(checkpoints)
                digest = (
                    None
                    if head_len is None
                    else history_page_index.body_digest(handle, head_len, scanned_end, frozen)
                )
                if head_len is not None and digest is not None:
                    saved = _TranscriptIndexBase(head_len, scanned_end, digest, row_count, frozen)
                    self._log._page_index_base[key] = saved
                    history_page_index.persist(path, saved, force=False)

        entry = _TranscriptRowIndexEntry(
            stamp,
            generation,
            row_count,
            tuple(checkpoints),
        )
        if self._file_stamp(path) == stamp:
            self._log._publish_if_current(
                self._log._page_index_cache,
                key,
                entry,
                key=key,
                gen=generation,
            )
        return entry

    def _read_message_range(
        self,
        key: str,
        start: int,
        end: int,
        index: _TranscriptRowIndexEntry,
    ) -> list[dict]:
        """Read ``[start, end)`` while decoding at most one extra index stride."""
        if start >= end or index.row_count == 0:
            return []
        checkpoint_row = 0
        checkpoint_offset = 0
        for row, offset in index.checkpoints:
            if row > start:
                break
            checkpoint_row, checkpoint_offset = row, offset

        path = self._log._path(key)
        messages: list[dict] = []
        logical_row = checkpoint_row
        with open(path, "rb") as handle:
            handle.seek(checkpoint_offset)
            records = strict_raw_records_with_offsets(handle, path)
            for _record_start, _record_end, raw in records:
                message = self._message_record(raw)
                if message is None:
                    continue
                if logical_row >= end:
                    break
                if logical_row >= start:
                    messages.append(message)
                logical_row += 1
        return messages

    def _read_messages_chained_page_once(
        self,
        key: str,
        *,
        limit: int,
        before: int | None,
    ) -> tuple[TranscriptPage, list[tuple[str, _TranscriptRowIndexEntry]], list[str]]:
        keys = self._chain_keys(key)
        indexed = [(chained_key, self._row_index(chained_key)) for chained_key in keys]
        total = sum(index.row_count for _chained_key, index in indexed)
        if total == 0 and keys != [key]:
            # Mirror ``read_messages_chained``'s terminal fallback: a chain whose
            # members yield no rows is served from *key*'s own file.
            indexed = [(key, self._row_index(key))]
            total = indexed[0][1].row_count
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)

        messages: list[dict] = []
        base = 0
        for chained_key, index in indexed:
            file_end = base + index.row_count
            overlap_start = max(start, base)
            overlap_end = min(end, file_end)
            if overlap_start < overlap_end:
                messages.extend(
                    self._read_message_range(
                        chained_key,
                        overlap_start - base,
                        overlap_end - base,
                        index,
                    )
                )
            base = file_end
            if base >= end:
                break
        revision: ChainRevision = tuple(
            (chained_key, index.stamp, index.generation) for chained_key, index in indexed
        )
        return TranscriptPage(messages, total, start, start > 0, revision), indexed, keys

    def read_messages_chained_page(
        self,
        key: str,
        *,
        limit: int,
        before: int | None = None,
        expected_revision: ChainRevision | None = None,
    ) -> TranscriptPage:
        """Read one exact chained page without materialising complete transcripts.

        A stable revision decodes the requested page plus at most one sparse-index
        stride per intersecting file. A file changing during the read is retried.
        ``expected_revision`` pins the several range reads composing one HTTP
        response to the same chain membership, file stamps, and generations.
        ``limit=0`` is the total/revision probe: it composes the indexes and
        decodes no rows.
        """
        if limit < 0:
            raise ValueError("limit must be >= 0")
        # A pinned read cannot succeed on a second attempt: the file stamp that
        # broke the pin includes mtime_ns and size, so the expected revision is
        # gone for good. One attempt, then the caller's retry policy decides.
        attempts = 1 if expected_revision is not None else _TRANSCRIPT_PAGE_READ_ATTEMPTS
        for _attempt in range(attempts):
            page, indexed, keys = self._read_messages_chained_page_once(
                key, limit=limit, before=before
            )
            stable = self._chain_keys(key) == keys and all(
                self._file_stamp(self._log._path(chained_key)) == index.stamp
                and self._log._cache_gen(chained_key) == index.generation
                for chained_key, index in indexed
            )
            if stable and (expected_revision is None or page.revision == expected_revision):
                return page
        if expected_revision is not None:
            raise TranscriptRevisionChanged("chained transcript changed during page composition")
        if limit == 0:
            # The total/revision probe decodes no rows. Re-reading the whole
            # transcript to answer it would cost far more than the probe it
            # replaces, so the caller's retry policy decides instead.
            raise TranscriptRevisionChanged("chained transcript changed during total probe")

        for _attempt in range(_TRANSCRIPT_PAGE_READ_ATTEMPTS):
            keys = self._chain_keys(key)
            indexed = [(chained_key, self._row_index(chained_key)) for chained_key in keys]
            revision: ChainRevision = tuple(
                (chained_key, index.stamp, index.generation) for chained_key, index in indexed
            )
            messages = self.read_messages_chained(key)
            stable = self._chain_keys(key) == keys and all(
                self._file_stamp(self._log._path(chained_key)) == index.stamp
                and self._log._cache_gen(chained_key) == index.generation
                for chained_key, index in indexed
            )
            if stable:
                total = len(messages)
                end = total if before is None else max(0, min(before, total))
                start = max(0, end - limit)
                return TranscriptPage(messages[start:end], total, start, start > 0, revision)
        raise TranscriptRevisionChanged("chained transcript changed during full-read fallback")

    def _rebuild_tab_id_index(self) -> None:
        """Rebuild the tab-id chain index while the owner lock is held."""
        index: dict[str, list[str]] = {}
        for path in sorted(self._log._dir.glob(_history_facade()._TAB_ID_INDEX_GLOB)):
            key = _history_facade()._index_key_for_stem(path.stem)
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            cached = self._log._tab_id_by_key.get(key)
            if cached is not None and cached[0] == stamp:
                tab_id = cached[1]
            else:
                generation = self._log._tab_id_generation
                self._log._meta_cache.pop(key, None)
                try:
                    metadata, readable = self._log._read_metadata_status(key)
                except Exception:
                    continue
                if not readable:
                    continue
                raw = metadata.get("tab_id")
                tab_id = raw if isinstance(raw, str) else ""
                if self._log._tab_id_generation == generation:
                    self._log._tab_id_by_key[key] = (stamp, tab_id)
            if tab_id:
                index.setdefault(tab_id, []).append(key)
        self._log._tab_id_index = index

    def invalidate_tab_id_cache(self) -> None:
        """Mark the owner tab-id index stale for the next chained read."""
        with self._log._lock:
            self._log._tab_id_index = None

    def note_tab_id(self, key: str, tab_id: str | None) -> None:
        """Update one already-warm tab-id chain entry when safe to do so."""
        if not _history_facade().can_hold_tab_id_index_entry(key):
            return
        if not tab_id:
            self._log.invalidate_tab_id_cache()
            return
        with self._log._lock:
            index = self._log._tab_id_index
            if index is None:
                return
            keys = index.get(tab_id)
            if not keys:
                self._log.invalidate_tab_id_cache()
                return
            chained = _history_facade()._index_key_for_stem(_history_facade().transcript_stem(key))
            if chained not in keys:
                keys.append(chained)

    def _read_messages(self, key: str) -> list[dict]:
        """Read all non-metadata rows through the owner's guarded message cache.

        A cache hit returns the shared list object by identity.  Callers must
        copy before mutating it.
        """
        path = self._log._path(key)
        try:
            identity = self._log._cache_identity(path.stat())
        except OSError:
            identity = None
        if identity is not None:
            cached = self._log._msg_cache.get(key)
            if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
                return cached[2]

        generation = self._log._cache_gen(key)
        witness = self._log._flock_hold_witness(key)
        with self._log._cache_fill_lock(key) as locked:
            messages = self._log._read_messages_locked(
                key,
                gen=None if locked else generation,
                flock_witness=witness,
            )
            if not locked and (
                generation != self._log._cache_gen(key)
                or witness is None
                or witness != self._log._flock_hold_witness(key)
            ):
                # A broken generation/flock witness makes only the memo unsafe;
                # the parsed value remains valid for the read that produced it.
                self._log._msg_cache.pop(key, None)
            return messages

    def _count_messages(self, key: str, *, hold_writer_lock: bool = True) -> int:
        """``len(self._read_messages(key))`` without parsing into or filling the cache.

        A warm cache entry answers for free; otherwise the file is streamed
        (:func:`kiro_crew.transcript_count.count_messages`) under the full
        read's writer lock and retry contract, publishing nothing, so the
        startup idle-seed scan does not fill the cache.

        ``hold_writer_lock=False`` skips the lock: the consolidator counts in a
        worker thread and must not make a blocking event-loop writer wait. With
        nothing published, no memo depends on the lock.
        """
        from kiro_crew.transcript_count import count_and_store, stored_count

        path = self._log._path(key)
        try:
            identity = self._log._cache_identity(path.stat())
        except OSError:
            identity = None
        if identity is not None:
            cached = self._log._msg_cache.get(key)
            if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
                return len(cached[2])
        fill_lock = (
            self._log._cache_fill_lock(key) if hold_writer_lock else contextlib.nullcontext(False)
        )
        with fill_lock:
            if not path.exists():
                return 0
            stored = stored_count(path)
            if stored is not None:
                return stored
            attempts = _history_facade()._METADATA_READ_ATTEMPTS
            for attempt in range(attempts):
                try:
                    return count_and_store(path, count_messages)
                except FileNotFoundError:
                    return 0
                except OSError:
                    if attempt + 1 < attempts:
                        self._log._pause_for_transient_retry()
                        continue
                    _HISTORY_LOGGER.warning(
                        "history: could not count messages for %s after %d attempts",
                        key,
                        attempts,
                        exc_info=True,
                    )
                    raise
        return 0

    @contextlib.contextmanager
    def _cache_fill_lock(self, key: str) -> Iterator[bool]:
        """Best-effort bounded hold of the owner's per-file writer lock."""
        lock = self._log._file_lock(key)
        on_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop = False
        if on_loop:
            held = lock.acquire(blocking=False)
        else:
            held = lock.acquire(timeout=_facade_flock_acquire_timeout())
        if not held:
            _HISTORY_LOGGER.debug(
                "history: writer lock for %s still busy (%s); filling the "
                "message cache unlocked rather than waiting unbounded",
                key,
                "event loop, single non-blocking attempt" if on_loop else "off-loop deadline",
            )
        try:
            yield held
        finally:
            if held:
                lock.release()

    def _read_messages_locked(
        self,
        key: str,
        *,
        gen: int | None,
        flock_witness: tuple[int, int] | None,
    ) -> list[dict]:
        """Fill the message cache under a writer lock or publish witnesses."""
        path = self._log._path(key)
        if not path.exists():
            self._log._msg_cache.pop(key, None)
            return []
        attempts = _history_facade()._METADATA_READ_ATTEMPTS
        for attempt in range(attempts):
            try:
                identity = self._log._cache_identity(path.stat())
                cached = self._log._msg_cache.get(key)
                if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
                    return cached[2]
                messages: list[dict] = []
                with open(path, encoding="utf-8") as handle:
                    for line in _iter_splitlines(handle):
                        data = message_row(line)
                        if data is not None:
                            messages.append(data)
            except FileNotFoundError:
                # A delete racing the read is a definitive empty answer, not a
                # transient sharing failure worth retrying.
                self._log._msg_cache.pop(key, None)
                return []
            except OSError:
                if attempt + 1 < attempts:
                    self._log._pause_for_transient_retry()
                    continue
                _HISTORY_LOGGER.warning(
                    "history: could not read messages for %s after %d attempts; "
                    "re-raising so restore can retry",
                    key,
                    attempts,
                    exc_info=True,
                )
                raise

            entry_generation = self._log._cache_gen(key) if gen is None else gen
            if gen is None or (
                gen == self._log._cache_gen(key)
                and flock_witness is not None
                and flock_witness == self._log._flock_hold_witness(key)
            ):
                self._log._msg_cache[key] = (identity, entry_generation, messages)
            return messages
        return []

    def _recent_via_tail(
        self,
        key: str,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict] | None:
        """Return a cached bounded tail, or defer to the full-read path."""
        path = self._log._path(key)
        generation = self._log._cache_gen(key)
        try:
            identity = self._log._cache_identity(path.stat())
        except OSError:
            return None
        cached = self._log._msg_cache.get(key)
        if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
            return None
        recent_key = self._log._recent_cache_key(key, max_messages, roles)
        recent = self._log._recent_cache.get(recent_key)
        if recent is not None and recent[0] == identity and recent[1] == self._log._cache_gen(key):
            return [dict(message) for message in recent[2]]
        # Brief rows are dropped before the window is cut, as on the full-read
        # path, so each one in the tail asks the reader for one more row.
        wanted = max_messages
        while True:
            tail = self._log._read_tail_messages(path, wanted, roles)
            kept = [message for message in tail if not is_warm_task_brief_message(message)]
            if len(kept) >= max_messages or len(tail) < wanted:
                break
            wanted += max_messages - len(kept)
        formatted = [
            {"role": message["role"], "content": message["content"]}
            for message in kept[-max_messages:]
        ]
        self._log._publish_if_current(
            self._log._recent_cache,
            recent_key,
            (identity, generation, formatted),
            key=key,
            gen=generation,
        )
        return [dict(message) for message in formatted]

    @staticmethod
    def _recent_cache_key(
        key: str,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> str:
        """Build a stable key for a formatted recent-message window."""
        roles_part = ",".join(sorted(roles)) if roles else ""
        return f"{key}\x00{max_messages}\x00{roles_part}"

    def _read_tail_messages(
        self,
        path: Path,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict]:
        """Read the true newest messages from a geometrically grown tail."""
        if max_messages <= 0:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        window = max(
            self._log._TAIL_MIN_BYTES,
            max_messages * self._log._TAIL_AVG_MSG_BYTES * 2,
        )
        messages: list[dict] = []
        for _ in range(self._log._TAIL_MAX_GROWTHS):
            covered = size <= window
            try:
                with open(path, "rb") as handle:
                    if not covered:
                        handle.seek(size - window)
                        handle.readline()
                    raw = handle.read().decode("utf-8", errors="replace")
            except OSError:
                return []
            messages = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("_type") == "metadata":
                    continue
                if roles and data.get("role") not in roles:
                    continue
                messages.append(data)
            if covered or len(messages) >= max_messages:
                break
            window *= 4
        return messages[-max_messages:]

    def _last_row_ts(self, key: str) -> str | None:
        """Return the final persisted row timestamp while the owner is locked."""
        tail = self._log._read_tail_messages(self._log._path(key), 1, None)
        if not tail:
            return None
        timestamp = tail[-1].get("ts")
        return timestamp if isinstance(timestamp, str) and timestamp else None

    def last_row_ts(self, key: str) -> str | None:
        """Return a lock-consistent final row timestamp."""
        with self._log._locked(key):
            return self._log._last_row_ts(key)

    def last_message_preview(
        self,
        key: str,
        sanitize: Callable[[str], str] | None = None,
    ) -> str:
        """Return a bounded preview of the newest message."""
        return self._log.last_message_info(key, sanitize=sanitize)[0]

    def last_message_info(
        self,
        key: str,
        sanitize: Callable[[str], str] | None = None,
    ) -> tuple[str, float, bool]:
        """Return the newest preview, the recency epoch, and a stop flag.

        Every previewable row counts (the sessions sidebar's read). Three of the
        four values the tail walk yields; :meth:`last_speech_info` documents it.
        """
        preview, epoch, stopped, _exhaustive = self._tail_walk(key, sanitize, speech_only=False)
        return preview, epoch, stopped

    def last_speech_info(
        self,
        key: str,
        sanitize: Callable[[str], str] | None = None,
    ) -> tuple[str, float, bool, bool]:
        """Return the newest SPEECH preview, the recency epoch, a stop flag, and
        whether the read was EXHAUSTIVE.

        Speech only: rows ``is_speech_row`` accepts (user / assistant, minus
        system notices and the workflow / sub-agent envelopes). The Crew
        Members roster is the reader: a member's chat draws only what the
        member says (``crew-mode.md``, "A crewmate's chat"), so its row's
        one-line preview must quote the same thing, or a patroller whose chat
        is empty sits beside a row quoting a shell command. The recency epoch
        is unchanged by it -- it still reads the newest row, because a patrol
        IS activity and the roster orders by it.
        """
        return self._tail_walk(key, sanitize, speech_only=True)

    def _tail_walk(
        self,
        key: str,
        sanitize: Callable[[str], str] | None,
        *,
        speech_only: bool,
    ) -> tuple[str, float, bool, bool]:
        """The one tail walk behind both reads above.

        - ``preview``: the newest conversational row's text, skipping the
          trailing stop card and other non-previewable rows.
        - ``epoch``: the thread's recency, taken from a newer skipped stop row
          when one exists (a stop is activity); other skips keep the previewed
          row's timestamp.
        - ``newest_is_stop``: the newest real row is a stop card, so the client
          can show a localized "Stopped" chip; the preview text alone would read
          as ongoing work. Uses the same ``is_stop_event_row`` as the skip, and
          stays True until the member says something new.
        - ``exhaustive``: the walk reached the start of the log. When False, an
          empty ``preview`` may hide older speech, and the roster reconcile must
          not write it to the append-only member log as authoritative.
        """
        # Function-local: dashboard.state imports kiro_crew.history at module
        # scope, which lands back here, so a top-level import would be a
        # cycle. By preview time the dashboard module is long since loaded.
        from kiro_crew.dashboard.state import is_stop_event_row
        from kiro_crew.dashboard.system_notices import is_speech_row

        path = self._log._path(key)
        try:
            size = path.stat().st_size
        except OSError:
            return "", 0.0, False, False
        windows = (
            self._log._PREVIEW_TAIL_BYTES,
            self._log._PREVIEW_TAIL_BYTES * 16,
        )

        def _row_epoch(row: dict) -> float:
            timestamp = row.get("ts")
            if isinstance(timestamp, str) and timestamp:
                try:
                    return datetime.fromisoformat(
                        timestamp.strip().replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    pass
            return 0.0

        # Recency from a skipped stop row (both walks) or machinery row
        # (speech-only walk): callers order by this epoch, so a just-active
        # thread must not sink below older ones. In the plain walk other
        # non-previewable rows keep the previewed row's timestamp
        # (test_preview_text.py pins it).
        newest_epoch = 0.0
        # Whether the NEWEST real row (first non-metadata row walking back) is
        # a stop card. `None` until the first real row is seen, so the
        # larger-window retry below cannot re-answer it: the small window is the
        # tail, so the newest real row it holds IS the thread's newest real row,
        # and a second pass reading further back must not overwrite that verdict.
        newest_is_stop: bool | None = None
        for window in windows:
            try:
                with open(path, "rb") as handle:
                    if size > window:
                        handle.seek(size - window)
                        handle.readline()
                    tail = handle.read().decode("utf-8", errors="replace")
            except OSError:
                return "", 0.0, False, False
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("_type") == "metadata":
                    continue
                # A Stop press's card is a `system` row whose content IS the
                # JSON stop payload (see the is_stop_event_row docstring), so
                # surfacing it hands `{"kind": "stop_event", …}` to every
                # preview caller — the Crew Members roster subtitle and the
                # session-list preview both render it verbatim otherwise.
                # Reuse the shared predicate rather than a fresh kind check:
                # its docstring documents why matching one carrier is the trap.
                row_is_stop = is_stop_event_row(data)
                # The first real row's stop-ness is the thread's `newest_is_stop`,
                # fixed here and never revised by a later row.
                if newest_is_stop is None:
                    newest_is_stop = row_is_stop
                if row_is_stop:
                    if not newest_epoch:
                        newest_epoch = _row_epoch(data)
                    continue
                # Normalised FIRST: a structured (list) content row is speech if
                # its text blocks say something, exactly as the slot detail
                # renders it; handing the raw list to the predicate would read
                # legacy structured speech as machinery and blank the roster.
                text = self._log._content_text(data.get("content"))
                if speech_only and not is_speech_row(data.get("role"), text, data.get("meta")):
                    # Machinery: skipped for the TEXT, kept for the recency.
                    if not newest_epoch:
                        newest_epoch = _row_epoch(data)
                    continue
                if not text:
                    continue
                # The ONE spelling of a roster preview (strip -> sanitize -> cap),
                # shared with the live `member/message` writer in state_parts/slot_directory.py so
                # the roster read never disagrees with what the live path folded.
                preview = speech_preview(text, sanitize, self._log._PREVIEW_MAX_CHARS)
                if not preview:
                    continue
                return preview, newest_epoch or _row_epoch(data), bool(newest_is_stop), True
            if size <= window:
                # The window held the whole file: nothing previewable exists.
                return "", newest_epoch, bool(newest_is_stop), True
        # Both windows spent, older rows unread: "" is not an answer.
        return "", newest_epoch, bool(newest_is_stop), False

    @staticmethod
    def _content_text(content: object) -> str:
        """Extract displayable text from plain or structured message content."""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text)
            return " ".join(part.strip() for part in parts if part.strip())
        return ""

    def get_metadata(self, key: str) -> dict:
        """Return session metadata for a logical key."""
        return self._log._read_metadata(key)

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        """Return metadata plus whether an existing file was readable."""
        return self._log._read_metadata_status(key)

    def metadata_line_state(self, key: str) -> str:
        """Say WHY a first line is unreadable: one of the ``METADATA_LINE_*`` values.

        For a writer that met ``readable=False`` from :meth:`get_metadata_status`:
        ``METADATA_LINE_TRANSIENT`` means defer and retry, ``METADATA_LINE_CORRUPT``
        means no retry will ever read the line. A ``METADATA_LINE_READABLE`` answer
        after an unreadable tuple means the file changed between the two reads,
        which a writer treats as transient. Readers do not need this: the
        two-tuple already tells them to refuse.
        """
        return self._log._read_metadata_state(key)[1]

    def _pause_for_transient_retry(self) -> None:
        """Sleep between read attempts only when off the event loop."""
        on_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop = False
        if not on_loop:
            _time.sleep(_history_facade()._METADATA_READ_RETRY_SECS)

    def _read_metadata(self, key: str) -> dict:
        """Read metadata while dropping the explicit readability flag."""
        return self._log._read_metadata_status(key)[0]

    def _read_metadata_status(self, key: str) -> tuple[dict, bool]:
        """The two-tuple view of :meth:`_read_metadata_state`: readable or not."""
        metadata, state = self._log._read_metadata_state(key)
        return metadata, state == METADATA_LINE_READABLE

    def _read_metadata_state(self, key: str) -> tuple[dict, str]:
        """Read the first JSONL line with guarded caching and bounded retries.

        Returns the metadata plus one of the ``METADATA_LINE_*`` states: an absent
        file, an empty first line and a first line that is JSON but not a
        metadata object all read as ``({}, METADATA_LINE_READABLE)``.
        """
        path = self._log._path(key)
        if not path.exists():
            self._log._meta_cache.pop(key, None)
            return {}, METADATA_LINE_READABLE
        attempts = _history_facade()._METADATA_READ_ATTEMPTS
        for attempt in range(attempts):
            generation = self._log._cache_gen(key)
            try:
                identity = self._log._cache_identity(path.stat())
                cached = self._log._meta_cache.get(key)
                if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
                    return cached[2], METADATA_LINE_READABLE
                # Bytes, decoded below: a first line that is not UTF-8 is a
                # damaged line no retry will ever read, not a read in flight.
                with open(path, "rb") as handle:
                    raw_first = handle.readline()
            except OSError:
                if attempt + 1 < attempts:
                    self._log._pause_for_transient_retry()
                    continue
                _HISTORY_LOGGER.warning(
                    "history: could not read metadata for %s after %d attempts; "
                    "reporting no metadata",
                    key,
                    attempts,
                    exc_info=True,
                )
                return {}, METADATA_LINE_TRANSIENT
            # Damaged bytes are reported CORRUPT, never TRANSIENT: reporting them
            # transient made every writer defer for good, so the line could never
            # be rewritten and the session refused every turn.
            metadata, state = parse_metadata_line(raw_first)
            if state != METADATA_LINE_READABLE:
                return {}, state
            if not raw_first.decode("utf-8").strip():
                # An empty first line is readable and holds nothing to cache.
                return {}, METADATA_LINE_READABLE
            self._log._publish_if_current(
                self._log._meta_cache,
                key,
                (identity, generation, metadata),
                key=key,
                gen=generation,
            )
            return metadata, METADATA_LINE_READABLE
        return {}, METADATA_LINE_READABLE

    def sliding_window(
        self,
        key: str,
        keep_recent: int = 5,
    ) -> tuple[list[dict], list[dict]]:
        """Split a transcript into compactable and live message windows."""
        messages = self._log._read_messages(key)
        split = max(0, len(messages) - keep_recent * 2)
        return messages[:split], messages[split:]
