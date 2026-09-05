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
import json
import logging
import time as _time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AbstractSet, Any, Literal, overload

from kiro_crew.atomic_write import atomic_write
from kiro_crew.history_cache import _FileChangeCacheEntry
from kiro_crew.jsonl_util import bounded_raw_records

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog


#: Every reader below decodes one ``.jsonl`` line at a time and then reads a
#: field off it. ``json.JSONDecodeError`` covers the line that will not parse;
#: it does NOT cover a line that parses to something other than an object
#: (``[]``, ``"text"``, ``12``, ``null``), which reaches ``.get`` and raises
#: ``AttributeError`` -- abandoning the read, and every valid row after the bad
#: one, on an error none of these callers expect. So a decode is followed by a
#: shape check, exactly as ``read_file_change_messages`` already does for the
#: same rows of the same files.
_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")


def _history_facade() -> Any:
    """Return the facade lazily, after its component imports have completed."""
    from kiro_crew import history

    return history


def _facade_flock_acquire_timeout() -> float:
    """Read the one timeout with an established facade rebind seam."""
    return float(_history_facade()._FLOCK_ACQUIRE_TIMEOUT_S)


def _facade_strip_markdown_preview(text: str) -> str:
    """Honor post-construction patches of the facade preview helper."""
    return _history_facade().strip_markdown_preview(text)


def drop_persisted_tail_prefix(
    full_disk: list[dict], tail: list[dict], *, require_mid: bool = False
) -> list[dict]:
    """`tail` minus whatever of its head `full_disk` already ends with.

    Two callers, one shape: a corpus read from disk, followed by rows that MAY
    already be in it.

    A fork rebuild appends the slot's unflushed tail onto a freshly read disk
    corpus. "Unflushed" was decided against an EARLIER read, so a save landing
    between the two reads leaves those rows in both lists.

    ``read_messages_chained_full`` concatenates a key's rotated archive with its
    live file. Rotation writes the archive first and rewrites the live file's head
    second, so a crash between those two steps leaves the rotated rows in both
    files permanently.

    In both cases appending blind duplicates rows, and a duplicated row is not
    cosmetic here: this is the index space pagination cursors and fork indices
    resolve against, so one extra row shifts every index above it.

    Rows are matched by their stable id (``meta.mid``) when BOTH carry one. That
    id is what ``history`` assigns per row, so it is the only identity that
    cannot collide. Without it the fallback requires ``(ts, role, content)`` to
    all match: ``(ts, role)`` alone is a spot-check elsewhere, but here a match
    DELETES a row, and two distinct rows can share a second and a role (the same
    speaker twice inside one second, or two processes stamping the same ts), so
    dropping on that alone would remove a genuinely unpersisted message. The
    longest overlap wins, so a partially-written duplication is handled as well as
    a whole one: both producers persist a PREFIX, never an interior slice.
    """
    if not full_disk or not tail:
        return tail

    def _same(a: dict, b: dict) -> bool:
        a_meta, b_meta = a.get("meta"), b.get("meta")
        a_mid = a_meta.get("mid") if isinstance(a_meta, dict) else None
        b_mid = b_meta.get("mid") if isinstance(b_meta, dict) else None
        if a_mid and b_mid:
            return bool(a_mid == b_mid)
        if require_mid:
            # A match here DELETES a row. Callers whose overlap can only come
            # from a re-archived prefix (whose rows always carry mids) opt in
            # to mid-proven matching: an id-less coincidence on the fallback
            # triple must be preserved, because deleting a genuine row is
            # strictly worse than serving a duplicate.
            return False
        return (
            a.get("ts", "") == b.get("ts", "")
            and a.get("role") == b.get("role")
            and a.get("content", "") == b.get("content", "")
        )

    for k in range(min(len(tail), len(full_disk)), 0, -1):
        base = len(full_disk) - k
        if all(_same(tail[i], full_disk[base + i]) for i in range(k)):
            return tail[k:]
    return tail


class TranscriptReadProjection:
    """Bounded transcript, metadata, and tab-chain read projections."""

    def __init__(self, log: ConversationLog) -> None:
        self._log = log

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
        """Return recent source-bearing entries for cross-session citation."""
        messages = self._log._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        result: list[dict] = []
        for message in [item for item in messages if item.get("source_thread")][-max_messages:]:
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
        candidates.sort(key=lambda message: message.get("ts", ""))
        return [
            {"role": message["role"], "content": message["content"]}
            for message in candidates[-max_messages:]
        ]

    def read_messages(self, key: str) -> list[dict]:
        """Return the shared read-only cached transcript projection."""
        return self._log._read_messages(key)

    def read_file_change_messages(self, key: str) -> list[dict]:
        """Return lightweight rows carrying only ``meta.file_changes``."""
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
                messages.append(
                    {
                        "ts": data.get("ts"),
                        "meta": {"file_changes": file_changes},
                    }
                )

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

    def read_messages_chained(self, key: str) -> list[dict]:
        """Concatenate chronologically ordered files sharing the same tab id."""
        metadata = self._log.get_metadata(key)
        tab_id = metadata.get("tab_id")
        if not tab_id:
            return self._log._read_messages(key)
        with self._log._lock:
            if self._log._tab_id_index is None:
                self._log._rebuild_tab_id_index()
            index = self._log._tab_id_index or {}
            keys = list(index.get(tab_id, []))
        if not keys:
            return self._log._read_messages(key)
        messages: list[dict] = []
        for chained_key in keys:
            messages.extend(self._log._read_messages(chained_key))
        return messages or self._log._read_messages(key)

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
        # A transient per-segment read failure must not be CACHED as "no
        # archived rows": the stat signature of a finished rotation never
        # changes again, so a poisoned empty entry would outlive the incident
        # forever — the transcript's head silently unreachable until the next
        # process restart.
        #
        # It must not be SERVED either. This corpus is an index space: the
        # `before`/`next_before` cursors and the fork index path both resolve a
        # rendered row's position against it, so dropping a segment does not
        # merely hide those rows, it shifts every index above them. A reader then
        # pages a corpus whose positions no longer mean what they meant, and an
        # index-addressed fork copies a different cutoff than the one on screen —
        # silently, with nothing to retry. Both consumers already turn a raised
        # read failure into a retryable 503, which is strictly better than a
        # truncated transcript nobody can see is truncated.
        complete = True
        for p in segs:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
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
            # Rotation archives the live file's head BEFORE rewriting the live
            # file. A hard crash between those two writes leaves the archived
            # rows at the head of the live file too, so the NEXT rotation
            # re-archives them: this corpus is the index space pagination
            # cursors and fork indices resolve against, and a duplicated row
            # silently shifts every index above it. Two proofs may drop a
            # row here, and an id-less interior coincidence satisfies
            # neither, because deleting a genuine row is strictly worse than
            # serving a duplicate:
            #
            # 1. PROVENANCE. A failed rewrite leaves segment N's rows at the
            #    live file's head, so the next rotation re-archives them
            #    field-for-field: segment N+1 either begins with ALL of N
            #    (enough new rows accrued) or is itself a shorter verbatim
            #    prefix of N (the rotation kept a tail). Both reduce to the
            #    first min(len) rows of ADJACENT segments being identical
            #    records -- a shape an organic transcript cannot reproduce,
            #    and one that needs no row ids.
            # 2. IDENTITY. Beyond that shared prefix, a row is dropped only
            #    when proven by stable ``meta.mid`` equality
            #    (``require_mid=True``); the ``(ts, role, content)`` fallback
            #    does not apply at this seam.
            #
            # This covers segment-to-segment overlap only: the archive-to-live
            # seam in read_messages_chained_full still needs its own
            # drop_persisted_tail_prefix call.
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
                # Segments exist yet no rotate rows parsed — the exact signature
                # of the live incident where the transcript head went missing.
                # Loud on purpose: this state should be impossible for a healthy
                # rotation archive.
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
            # Rotation archives the dropped lines FIRST and rewrites the live
            # file's head second, deliberately (archiving is a precondition, so it
            # fails closed rather than losing the only copy of those rows). That
            # ordering leaves a window in which both files hold them, and a crash
            # or a kill inside it makes the duplication permanent — after which a
            # blind concatenation here serves every archived row twice.
            #
            # A duplicated row is not cosmetic in this corpus: it is the index
            # space `before`/`next_before` cursors and fork indices resolve
            # against, so one extra row shifts every index above it and a fork
            # taken at a rendered row lands on a different message.
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

        The slot-detail fast path advertises the whole archived head as one
        `next_before` cursor, which is exact only while the archived rows are
        a contiguous PREFIX of the chained corpus -- i.e. only the first chain
        member ever rotated. A later member's archive is sandwiched between
        rows the fast response already carries, so no single cursor can hand
        it to the client; the caller must serve the true chained corpus
        instead.

        Stat-only (segment existence, nothing parsed), and conservative on
        purpose: a non-rotate segment (``compact`` etc.) also answers True,
        which routes the caller to the always-correct full corpus -- slower,
        never wrong. The precise filter would cost a parse per segment.
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
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None:
            cached = self._log._msg_cache.get(key)
            if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
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
                mtime = path.stat().st_mtime
                cached = self._log._msg_cache.get(key)
                if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
                    return cached[2]
                with open(path, encoding="utf-8") as handle:
                    raw = handle.read()
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

            messages: list[dict] = []
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
                if data.get("_type") != "metadata":
                    messages.append(data)

            entry_generation = self._log._cache_gen(key) if gen is None else gen
            if gen is None or (
                gen == self._log._cache_gen(key)
                and flock_witness is not None
                and flock_witness == self._log._flock_hold_witness(key)
            ):
                self._log._msg_cache[key] = (mtime, entry_generation, messages)
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
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cached = self._log._msg_cache.get(key)
        if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
            return None
        recent_key = self._log._recent_cache_key(key, max_messages, roles)
        recent = self._log._recent_cache.get(recent_key)
        if recent is not None and recent[0] == mtime:
            return [dict(message) for message in recent[1]]
        tail = self._log._read_tail_messages(path, max_messages, roles)
        formatted = [{"role": message["role"], "content": message["content"]} for message in tail]
        self._log._publish_if_current(
            self._log._recent_cache,
            recent_key,
            (mtime, formatted),
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
    ) -> tuple[str, float]:
        """Return the newest message preview and that row's epoch timestamp."""
        path = self._log._path(key)
        try:
            size = path.stat().st_size
        except OSError:
            return "", 0.0
        windows = (
            self._log._PREVIEW_TAIL_BYTES,
            self._log._PREVIEW_TAIL_BYTES * 16,
        )
        for window in windows:
            try:
                with open(path, "rb") as handle:
                    if size > window:
                        handle.seek(size - window)
                        handle.readline()
                    tail = handle.read().decode("utf-8", errors="replace")
            except OSError:
                return "", 0.0
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
                text = self._log._content_text(data.get("content"))
                if not text:
                    continue
                preview = _facade_strip_markdown_preview(text)
                if not preview:
                    continue
                # Sanitization precedes truncation so a boundary cannot hide a
                # credential fragment from a caller's pattern-based redactor.
                if sanitize is not None:
                    preview = sanitize(preview)
                if len(preview) > self._log._PREVIEW_MAX_CHARS:
                    preview = preview[: self._log._PREVIEW_MAX_CHARS].rstrip() + "…"
                timestamp = data.get("ts")
                epoch = 0.0
                if isinstance(timestamp, str) and timestamp:
                    try:
                        epoch = datetime.fromisoformat(
                            timestamp.strip().replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        pass
                return preview, epoch
            if size <= window:
                break
        return "", 0.0

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
        """Read the first JSONL line with guarded caching and bounded retries."""
        path = self._log._path(key)
        if not path.exists():
            self._log._meta_cache.pop(key, None)
            return {}, True
        attempts = _history_facade()._METADATA_READ_ATTEMPTS
        for attempt in range(attempts):
            generation = self._log._cache_gen(key)
            try:
                mtime = path.stat().st_mtime
                cached = self._log._meta_cache.get(key)
                if cached and cached[0] == mtime and cached[1] == self._log._cache_gen(key):
                    return cached[2], True
                with open(path, encoding="utf-8") as handle:
                    first = handle.readline().strip()
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
                return {}, False
            if not first:
                return {}, True
            try:
                data = json.loads(first)
                metadata = (
                    data if isinstance(data, dict) and data.get("_type") == "metadata" else {}
                )
            except json.JSONDecodeError:
                metadata = {}
            self._log._publish_if_current(
                self._log._meta_cache,
                key,
                (mtime, generation, metadata),
                key=key,
                gen=generation,
            )
            return metadata, True
        return {}, True

    def sliding_window(
        self,
        key: str,
        keep_recent: int = 5,
    ) -> tuple[list[dict], list[dict]]:
        """Split a transcript into compactable and live message windows."""
        messages = self._log._read_messages(key)
        split = max(0, len(messages) - keep_recent * 2)
        return messages[:split], messages[split:]


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
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    return False
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
    ) -> bool:
        """Merge fields only when the locked on-disk metadata passes a guard."""
        with self._log._locked(key):
            metadata, readable = self._log._read_metadata_status(key)
            if not readable or not guard(metadata):
                return False
            self._log._update_metadata_locked(key, fields)
        if "tab_id" in fields:
            self._log.invalidate_tab_id_cache()
        return True

    def _update_metadata_locked(self, key: str, fields: dict) -> None:
        """Merge or upsert one metadata line while the owner lock is held."""
        path = self._log._path(key)
        previous_mtime = _history_facade()._safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        if lines:
            try:
                metadata = json.loads(lines[0])
            except json.JSONDecodeError:
                return
            if not isinstance(metadata, dict):
                return
            if metadata.get("_type") != "metadata":
                return
        else:
            self._log._dir.mkdir(parents=True, exist_ok=True)
            metadata = {
                "_type": "metadata",
                "created_at": _history_facade().metadata_now_iso(),
                "last_consolidated": 0,
            }
            lines = [""]

        metadata.update(fields)
        lines[0] = json.dumps(metadata) + "\n"

        # This hot one-line edit remains crash-atomic without paying for an
        # fsync while every other writer of the session is excluded.
        import os
        import tempfile

        data = "".join(lines).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
            os.replace(temporary, str(path))
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        _history_facade()._restore_mtime(path, previous_mtime)
        self._log._invalidate_cache(key)

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
        path = self._log._path(key)
        with self._log._locked(key):
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
