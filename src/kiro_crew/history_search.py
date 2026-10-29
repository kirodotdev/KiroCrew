"""The composed session catalog projection, and the facade of the transcript-search family.

Query parsing and the shared search memos live in owner modules.  The projection
is deliberately composed with a ConversationLog facade instead of inheriting
from it: calls back through the facade preserve the narrow monkeypatch seams
used by the history race and cost tests while keeping one owner for each cache,
lock, and path.

What a query means lives in :mod:`kiro_crew.history_search_query`, pure parsing with
no storage side effects, and what every search over one directory shares in
:mod:`kiro_crew.history_search_memo`. Every name they define stays importable from
here; the family block at the end of this file says what new work goes where.
"""

from __future__ import annotations

import heapq
import importlib as _importlib
import itertools
import json
import math
import os
import sys as _sys
import threading
import time as _time
from collections.abc import Callable, Container, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew._sqlite_compat import is_cjk_char, script_runs  # noqa: F401 - facade re-exports
from kiro_crew.history_cache import _GroupRecencyMemo
from kiro_crew.history_index import (
    INDEX_FILENAME,
    ChunkDecodeError,
    IndexRow,
    SessionSearchIndex,
    fold_texts,
)
from kiro_crew.history_search_memo import (  # noqa: F401 - facade re-exports
    _DIGEST_HEX_CHARS,
    _MEMO_ENTRY_OVERHEAD,
    _SEARCH_COUNT_MEMO_BYTES,
    _SEARCH_FOLD_BUDGET_BYTES,
    _SEARCH_SNIPPET_BUDGET_BYTES,
    _SEARCH_SNIPPET_MEMO_BYTES,
    _SHARED_MEMO_DIRS,
    _ContentCounts,
    _Flight,
    _flights,
    _flights_lock,
    _LiveDigests,
    _memo_entry_cost,
    _memo_group,
    _MemoGate,
    _MemoPair,
    _memos_for,
    _MemoValue,
    _needle_memo_key,
    _new_memo,
    _phrase_memo_key,
    _pinned,
    _shared_memos,
    _shared_memos_lock,
)
from kiro_crew.history_search_query import (  # noqa: F401 - facade re-exports
    _CJK_CHAR_WEIGHT,
    _CJK_SEGMENTER_BACKOFF_SECS,
    _CJK_WORDS_MEMO_RUNS,
    _FORGE_CHAIN_ONLY_WORDS,
    _FORGE_LEAD_PUNCT,
    _FORGE_MR_WORDS,
    _FORGE_REF_WEIGHT,
    _FORGE_REF_WORDS,
    _FORGE_REQUEST_WORDS,
    _FORGE_SIGIL_RE,
    _FORGE_TRAIL_PUNCT,
    _FORGE_TYPE_WORDS,
    _FORGE_URL_RE,
    _FORGE_URL_REPO_RE,
    _FORGE_WORD_NUM_RE,
    _MAX_SEARCH_REF_SPELLINGS,
    _SEARCH_MAX_FORGE_REFS,
    _SEARCH_MAX_SCORING_EXTRAS,
    _SHORT_NEEDLE_MAX_CHARS,
    _TITLE_SCOPE_PREFIX,
    CJK_SEGMENT_MAX_RUN_CHARS,
    CJK_SEGMENT_MAX_RUNS,
    SEARCH_MAX_TOKENS,
    CjkSegmenter,
    SearchNeedle,
    TitleScope,
    _cjk_slot,
    _cjk_words,
    _cjk_words_lock,
    _cjk_words_memo,
    _CjkSegmenterSlot,
    _forge_lead_in,
    _forge_spellings,
    _forge_type_suffix,
    _ForgeRef,
    _is_cjk_char,
    _is_short_term,
    _lead_names_a_type,
    _lead_names_merge_request,
    _parse_forge_ref,
    _provider_search_ref,
    _saturates,
    _script_runs,
    _snippet_tiers,
    _valid_words,
    count_needle,
    is_han_run,
    logger,
    needles_match_text,
    parse_search_query,
    register_cjk_segmenter,
    register_search_ref_resolver,
    reset_cjk_segmenter_for_tests,
    reset_search_ref_resolver_for_tests,
    snippet_needle_tiers,
    snippet_needles,
    split_title_scope,
    tiered_first_hit,
)
from kiro_crew.jsonl_util import bounded_records
from kiro_crew.shared_crew.attribution import public_attribution_from_meta

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

#: ``_scan_head_title`` outcome for a head that raised while being read; never cached.
_UNREADABLE = object()


SEARCH_MIN_CHARS = 2  # shortest query string that triggers backend search
_TITLE_BOOST = 10  # field-boost multiplier for title matches in search_sessions
_PHRASE_BOOST = 4  # extra weight per exact whole-query hit in a multi-word search
_SEARCH_SCAN_WINDOW = 500  # cap files scanned per search to bound I/O

#: Seconds a session's transcript must have gone unmodified before the backfill
#: pass indexes it. A session that is being written changes on every turn, and
#: every change re-reads, re-folds and re-walks the whole file — O(n^2) work
#: over the session's life. Deferring until the file goes quiet indexes it once
#: instead. Until then the session is simply not vouched for, so search scans
#: it directly — the same superset guarantee un-indexed sessions already have.
_INDEX_QUIET_WINDOW_SECS = 120.0
# Recency boost bounds for search_sessions: a session modified now scores
# ×(1 + _RECENCY_MAX_BOOST); the extra weight halves every
# _RECENCY_HALF_WEIGHT_DAYS of age and decays toward ×1.0 — never a penalty
# that could bury an old exact match, only a bounded tiebreaker toward
# recent work. 1.5 is sized to the canonical complaint: a year-old session
# matching TWICE must lose to today's session matching once (2 hits × ~1.11
# aged boost ≈ 2.2 < 1 hit × 2.5 fresh ceiling), while a decisively better
# old match (3+ hits) still wins — a 1.0 ceiling left the stated 2:1 case
# unfixed.
_RECENCY_MAX_BOOST = 1.5
_RECENCY_HALF_WEIGHT_DAYS = 30.0


def _facade_search_scan_window() -> int:
    """Read the facade's live scan limit without creating an import cycle.

    The import happens only when a search runs.  Keeping this as a getter is
    intentional: tests and callers patch
    ``kiro_crew.history._SEARCH_SCAN_WINDOW`` after construction.
    """
    from kiro_crew import history as history_facade

    return history_facade._SEARCH_SCAN_WINDOW


def _history_lock_timeout() -> type[BaseException]:
    """The facade's lock-timeout class, looked up late to avoid an import cycle.

    ``history`` imports this module to build its catalog projection, so the class
    cannot be imported at module scope. Same reason as
    :func:`_facade_search_scan_window` above.
    """
    from kiro_crew import history as history_facade

    return history_facade.HistoryLockTimeout


def _jsonl_stem(name: str) -> str:
    """``Path(name).stem`` for a directory entry name ending in ``.jsonl``.

    The final suffix is everything from the last dot, unless that dot starts
    the name, so only a name longer than the six-character suffix loses it.
    """
    return name[:-6] if len(name) > 6 else name


#: Session files whose stat failed with something other than ENOENT, already
#: warned about. One such file marks every listing incomplete, which keeps the
#: dashboard's unread drain held for the process lifetime, so the warning must
#: be visible; the listing runs every few seconds, so it is also logged only
#: once per path per process rather than on every pass.
_unreadable_session_files_warned: set[str] = set()
#: Listings run on executor threads, so the check and the add share one lock.
_unreadable_session_files_lock = threading.Lock()


#: The metadata-line fields a catalog row carries so a reader can tell a Crew Mode
#: root from a crew member without opening the file: the creator stamp, the agent's
#: namespace and the Crew Mode stamp (``crew_mode.saved_row_is_root``).
_CREW_CATALOG_FIELDS: tuple[str, ...] = ("created_by", "agent_kind", "crew_mode_agent")


def _project_crew_fields(meta: dict, line: dict) -> None:
    """Copy the non-empty :data:`_CREW_CATALOG_FIELDS` of metadata *line* onto catalog row *meta*."""
    for field in _CREW_CATALOG_FIELDS:
        value = line.get(field)
        if isinstance(value, str) and value:
            meta[field] = value


def _warn_unreadable_session_file(path: str, error: OSError) -> None:
    with _unreadable_session_files_lock:
        if path in _unreadable_session_files_warned:
            return
        _unreadable_session_files_warned.add(path)
    logger.warning(
        "Session file %s cannot be stat'd (%s); the session list is reported "
        "incomplete and unread dots stay held until it is readable or removed",
        path,
        error,
    )


class SessionCatalogProjection:
    """List, aggregate, search, and snippet-project conversation sessions."""

    def __init__(self, log: "ConversationLog") -> None:
        self._log = log
        self._index: "SessionSearchIndex | None" = None
        #: ``digest + needle key -> (count, cost)`` over the folded text of a
        #: session the index vouches for, and ``digest + repr(snippet tiers) ->
        #: (snippet, cost)`` for the same sessions; each ``None`` when switched
        #: off. Shared with every projection over this directory.
        self._count_memo: "_GroupRecencyMemo[_MemoValue] | None"
        self._snippet_memo: "_GroupRecencyMemo[_MemoValue] | None"
        self._count_memo, self._snippet_memo = _memos_for(str(log._dir))

    @property
    def search_index(self) -> "SessionSearchIndex":
        """The candidate index, opened on first search rather than on construction.

        Lazy because a ``ConversationLog`` is built for many reasons that never
        search — every test fixture, every append-only writer — and opening a
        SQLite file per instance would cost handles nothing asks for. Owned here
        rather than on the log because the index exists only to serve this
        projection; the delete path reaches it through the log's catalog
        projection.

        Kept in a ``.index`` subdirectory OF the session directory, so a pod, a
        temporary home, or a test's ``tmp_path`` indexes its own sessions instead
        of sharing one file with whatever else lives beside it. A subdirectory
        rather than a sibling of the transcripts because the index is derived
        data: anything walking the session directory should be able to ignore it
        by name, and losing it costs one backfill.
        """
        if self._index is None:
            self._index = SessionSearchIndex(self._log._dir / ".index" / INDEX_FILENAME)
        return self._index

    @staticmethod
    def _canonical_key(key: str) -> str:
        """Collapse stacked ``dashboard_`` prefixes to a single one.

        Files like ``dashboard_dashboard_chat-1-123`` are duplicates of
        ``dashboard_chat-1-123`` caused by resume round-trips.  Return
        the canonical (single-prefix) form so callers can deduplicate.
        """
        if not key.startswith("dashboard_"):
            return key
        stripped = key
        while stripped.startswith("dashboard_"):
            stripped = stripped[len("dashboard_") :]
        return f"dashboard_{stripped}" if stripped else key

    @staticmethod
    def _scan_head_title(path: "str | Path") -> object:
        """The first user message's content[:80] within the first 21 lines.

        Returns the title, ``None`` when the head holds none, or ``_UNREADABLE``
        when reading or interpreting the head raised -- that outcome is not
        cached, so a transient error is retried on the next listing exactly as
        the uncached read always was.
        """
        try:
            with open(path, encoding="utf-8") as f:
                for i, ln in enumerate(f):
                    if i > 20:
                        break
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if d.get("role") == "user" and d.get("content"):
                        return d["content"][:80]
        except Exception:
            return _UNREADABLE
        return None

    def newest_session_stems(self, max_entries: int) -> tuple[list[tuple[str, float]], bool]:
        """Return up to *max_entries* ``(stem, mtime)`` pairs, newest first.

        A bounded catalog read for callers that walk the archive under a cap.
        It stats each transcript and keeps only the newest *max_entries* in a
        heap, so what it holds is bounded by *max_entries* however large the
        directory is. It reads no file content: no title, no metadata line.
        Symlinked handoff aliases are skipped, as in :meth:`list_sessions`.
        Stacked ``dashboard_`` duplicates are NOT folded here; a caller that
        folds keys and keeps the first one it sees gets the newest copy, which
        is what :meth:`list_sessions` keeps. The second value is True when more
        transcripts existed than were returned.
        """
        if max_entries < 1 or not self._log._dir.exists():
            return [], False
        counted = 0

        def _stats() -> Iterator[tuple[float, str]]:
            nonlocal counted
            for path in self._log._dir.glob("*.jsonl"):
                try:
                    if path.is_symlink():
                        continue
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                counted += 1
                yield mtime, path.stem

        newest = heapq.nlargest(max_entries, _stats())
        return [(stem, mtime) for mtime, stem in newest], counted > max_entries

    def list_sessions(self, *, keys: Iterable[str] | None = None) -> list[dict]:
        return self._list_sessions_status(keys=keys)[0]

    def list_sessions_status(self) -> tuple[list[dict], bool]:
        """Return sessions plus whether the directory listing was complete."""
        return self._list_sessions_status()

    def _list_sessions_status(
        self, *, keys: Iterable[str] | None = None
    ) -> tuple[list[dict], bool]:
        """Return metadata for session files, newest first.

        Deduplicates stacked ``dashboard_`` prefix files, keeping the
        most recently modified version. Uses the metadata cache
        when available, falling back to reading only the first line for
        title extraction. Explicit *keys* read only those files, so a caller
        holding one transcript lock can refresh its row without a directory scan.
        """
        sessions: list[dict] = []
        try:
            self._log._dir.stat()
        except FileNotFoundError:
            return sessions, True
        except OSError:
            return sessions, False
        complete = True
        # Deduplicate stacked dashboard_ prefixes by canonical key, keeping newer
        by_canon: dict[str, dict] = {}
        if keys is None:
            # One scandir pass: the entry's d_type answers is_symlink without an
            # lstat, and the order is the same directory order glob() used
            # (glob iterates scandir), so ties in the final mtime sort resolve
            # exactly as before. The suffix test is glob("*.jsonl")'s match:
            # pathlib matches dot-names and directories, and folds case only
            # where the platform's paths do (Windows). Entries carry their
            # path as a string and their stem from the name, so a listing
            # builds no ``Path`` per file.
            base = os.fspath(self._log._dir)
            try:
                with os.scandir(base) as it:
                    entries: list[tuple[str, str, os.DirEntry[str] | None]] = [
                        (e.path, _jsonl_stem(e.name), e)
                        for e in it
                        if os.path.normcase(e.name).endswith(".jsonl")
                    ]
            except OSError:
                return [], False
        else:
            entries = []
            for key in keys:
                p = self._log._path(key)
                entries.append((os.fspath(p), p.stem, None))
        for path, key, entry in entries:
            # Snapshot the invalidation generation BEFORE the stat: the
            # The first-line fill records this stat's cache identity. The
            # generation independently proves the stat → read → publish window
            # stayed write-free for this process.
            gen = self._log._cache_gen(key)
            try:
                # Windows DirEntry.stat() reports st_ino as 0, so its cache
                # identity would never equal the one every other reader takes
                # from path.stat(); there the entry answers only is_symlink.
                stat = os.stat(path) if entry is None or os.name == "nt" else entry.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                _warn_unreadable_session_file(path, exc)
                complete = False
                continue
            identity = self._log._cache_identity(stat)
            # Skip symlinks — these are handoff aliases pointing to the real session
            if os.path.islink(path) if entry is None else entry.is_symlink():
                continue
            meta: dict = {
                "key": key,
                "messages": max(1, int(stat.st_size / 200)),
                "modified": stat.st_mtime,
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            # Try metadata cache first (populated by _read_metadata calls)
            cached_meta = self._log._meta_cache.get(key)
            if (
                cached_meta
                and cached_meta[0] == identity
                and cached_meta[1] == self._log._cache_gen(key)
            ):
                d = cached_meta[2]
                if d.get("created_at"):
                    meta["created"] = d["created_at"]
                if d.get("title"):
                    meta["title"] = d["title"]
                if d.get("agent"):
                    meta["agent"] = d["agent"]
                meta["memory_mode"] = d.get("memory_mode", "persistent")
                if d.get("folder_id"):
                    meta["folder_id"] = d["folder_id"]
                _project_crew_fields(meta, d)
                meta["attribution"] = public_attribution_from_meta(d, key=key)
            else:
                # Read only the first line for metadata
                try:
                    with open(path, encoding="utf-8") as f:
                        first_line = f.readline().strip()
                    if first_line:
                        d = json.loads(first_line)
                        if d.get("_type") == "metadata":
                            if d.get("created_at"):
                                meta["created"] = d["created_at"]
                            if d.get("title"):
                                meta["title"] = d["title"]
                            if d.get("agent"):
                                meta["agent"] = d["agent"]
                            meta["memory_mode"] = d.get("memory_mode", "persistent")
                            if d.get("folder_id"):
                                meta["folder_id"] = d["folder_id"]
                            _project_crew_fields(meta, d)
                            meta["attribution"] = public_attribution_from_meta(d, key=key)
                            # Guarded publish — discard the fill if a write
                            # invalidated this key inside the stat → read
                            # window (see the generation snapshot above).
                            self._log._publish_if_current(
                                self._log._meta_cache,
                                key,
                                (identity, gen, d),
                                key=key,
                                gen=gen,
                            )
                        elif isinstance(d, dict):
                            # A first line that is an object but not metadata
                            # reads as no metadata, the same ``{}`` that
                            # ``_read_metadata`` caches for it, so the next
                            # listing answers from the cache instead of the file.
                            self._log._publish_if_current(
                                self._log._meta_cache,
                                key,
                                (identity, gen, {}),
                                key=key,
                                gen=gen,
                            )
                except Exception:
                    pass
            # Ensure memory_mode is always present (old sessions lack it)
            meta.setdefault("memory_mode", "persistent")
            # A session with no readable metadata line has no creator: Unattributed.
            if "attribution" not in meta:
                meta["attribution"] = public_attribution_from_meta(None, key=key)
            # Extract first user message as title fallback
            if "title" not in meta:
                msg_cached = self._log._msg_cache.get(key)
                if (
                    msg_cached
                    and msg_cached[0] == identity
                    and msg_cached[1] == self._log._cache_gen(key)
                ):
                    for m in msg_cached[2]:
                        if m.get("role") == "user" and m.get("content"):
                            meta["title"] = m["content"][:80]
                            break
                else:
                    scanned = self._log._title_scan_cache.get(key)
                    if (
                        scanned is not None
                        and scanned[0] == (identity, stat.st_size)
                        and scanned[1] == self._log._cache_gen(key)
                    ):
                        if scanned[2] is not None:
                            meta["title"] = scanned[2]
                    else:
                        found = self._scan_head_title(path)
                        if found is not _UNREADABLE:
                            if found is not None:
                                meta["title"] = found
                            # Guarded like the metadata fill above.
                            self._log._publish_if_current(
                                self._log._title_scan_cache,
                                key,
                                ((identity, stat.st_size), gen, found),
                                key=key,
                                gen=gen,
                            )
            if "title" not in meta:
                meta["title"] = key
            # Deduplicate: keep newer entry per canonical key
            canon = self._log._canonical_key(key)
            existing = by_canon.get(canon)
            if existing is None or stat.st_mtime >= existing["modified"]:
                by_canon[canon] = meta
        sessions = list(by_canon.values())
        sessions.sort(key=lambda s: s.get("modified", 0), reverse=True)
        return sessions, complete

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        """Return ``{agent_name: (session_count, last_used_mtime)}`` per agent.

        Built on top of :meth:`list_sessions` (not a fresh directory glob) so it
        inherits that method's canonical-session dedup and symlink-skip — counts
        are therefore per logical conversation, not per raw ``.jsonl`` file.
        Sessions whose metadata never recorded an agent are ignored.
        """
        usage: dict[str, tuple[int, float]] = {}
        for meta in self._log.list_sessions():
            agent = meta.get("agent")
            if not agent:
                continue
            count, last_used = usage.get(agent, (0, 0.0))
            usage[agent] = (count + 1, max(last_used, meta.get("modified", 0.0)))
        return usage

    def search_sessions(
        self, query: str, limit: int = 50, *, keys: Container[str] | None = None
    ) -> list[dict]:
        """Return session metadata for files whose message content matches *query*.

        Ranking, gating and *keys* are documented on :meth:`_search_sessions`.

        Unrestricted callers asking the same *query* and *limit* while one such
        search is running wait for it and get its rows, rather than each paying
        the same cold pass in turn on one GIL. A caller joins only while no write
        has landed in this process since the running search began, so a search
        that starts after a write never returns rows computed before it.
        """
        if keys is not None or not query or limit <= 0:
            return self._search_sessions(query, limit, keys=keys)
        slot = (str(self._log._dir), query, limit)
        epoch = self._log._dir_write_epoch()
        with _flights_lock:
            running = _flights.get(slot)
            if running is None or running.epoch != epoch:
                flight = _Flight(epoch)
                _flights[slot] = flight
                running = None
        if running is not None:
            running.done.wait()
            if running.result is not None:
                return [dict(row) for row in running.result]
            return self._search_sessions(query, limit)
        try:
            rows = self._search_sessions(query, limit)
            # A private copy, so a caller editing the rows it was handed cannot
            # change what a waiting caller receives.
            flight.result = [dict(row) for row in rows]
            return rows
        finally:
            with _flights_lock:
                if _flights.get(slot) is flight:
                    del _flights[slot]
            flight.done.set()

    def warm_search(self) -> None:
        """Do the query-independent part of a first search ahead of it.

        Reads every window session's metadata into the log's cache and opens
        the index with one shortlist read, which together are most of what a
        first query costs beyond any later new query. Blocking; run off the loop.
        """
        if not self._log._dir.exists():
            return
        window = self._log.list_sessions()[: _facade_search_scan_window()]
        self._index_shortlist(window, [])

    def _search_sessions(
        self, query: str, limit: int = 50, *, keys: Container[str] | None = None
    ) -> list[dict]:
        """Return session metadata for files whose message content matches *query*.

        This is the ONE ranking every transcript-search consumer shares — the
        dashboard history filter, the ``search_chat_history`` MCP tool, and
        Discord session resume — so the recency weighting and CJK adjacency
        floor below apply to all of them by design: "the best match for this
        query" should not depend on which surface asked.

        The query is parsed by :func:`parse_search_query` into needles, and a
        session matches only when **every** REQUIRED needle appears somewhere in
        its title or content — an AND over the whole document, not a single
        whole-query substring. That is what makes a natural multi-word query
        work: ``"ack contention hypotheses"`` finds a session discussing all
        three, which a whole-phrase match missed because those exact words never
        sit adjacent. A single-token query is unchanged by this — one token IS
        the phrase. CJK terms gate on their individual characters (CJK text has
        no spaces to split on, so the run itself would demand the user's exact
        sentence), rank on their character bigrams, and require at least one
        bigram hit (the adjacency floor) — see :func:`parse_search_query` for
        the recall/precision split.

        A term naming a forge item (``#4411``, ``pr 4411``, ``pull/4411``, a PR
        URL, ``owner/repo#4411``) gates on ANY spelling of that item rather than
        on the literal term, so the session that discussed the pull request is
        found whichever form its transcript used. A bare number additionally
        RANKS on those spellings while still gating on the plain digits.

        Each needle is matched case-insensitively as a SUBSTRING (so ``"cont"``
        hits ``"contention"``, which keeps search-as-you-type responsive) using
        full Unicode case folding via :meth:`str.casefold` (so e.g. German ``ß``
        folds to ``ss``).  Matching only on parsed ``content`` avoids false
        positives from JSON structural elements (e.g. the word ``"user"``
        matching every ``"role": "user"`` line).

        Ranking (higher is better)::

            score = ((title_hits * _TITLE_BOOST)
                  + (content_hits / sqrt(1 + doc_chars / 1024))
                  + saturated_hits) * recency

        where ``*_hits`` sum the per-needle weighted counts, plus
        ``_PHRASE_BOOST`` per occurrence of the exact whole query when it
        carries more than a single needle. A short ASCII needle (``5``,
        ``s3``; one or two characters, see ``_SHORT_NEEDLE_MAX_CHARS``)
        contributes to ``saturated_hits`` instead of
        ``content_hits``: ``log1p`` of its length-normalized body count. As a
        substring it matches timestamps,
        ids and hashes far more often than prose about the thing does, and
        unsaturated a long transcript's thousands of incidental digit hits
        out-score the session whose title IS the query. Saturating the
        NORMALIZED count keeps body-only matches in frequency order (a
        substantive long discussion still beats one stray mention in a short
        session) without dividing by the length norm a second time. Its title
        hits and its place in the AND gate are unchanged, and longer terms,
        digit runs of three or more (``4411``) included,
        keep raw frequency. The phrase bonus rewards adjacency:
        at comparable term frequency, the session containing the words TOGETHER
        as typed ranks above one that merely mentions them far apart.  It is
        deliberately a bonus and not an override — a session repeating one term
        far more often still wins on raw term frequency, exactly as it already
        did for a single-token query.  (Saturating term frequency generally,
        BM25-style, would change that and re-rank every existing single-token
        query, so it stays out of scope; only the short needles above
        are saturated, because for them frequency was measuring digit density,
        not relevance.)

        ``recency`` is a bounded multiplicative boost — ``1 +
        _RECENCY_MAX_BOOST / (1 + age_days / _RECENCY_HALF_WEIGHT_DAYS)`` — so
        at comparable relevance today's session outranks last year's, while a
        decisively better old match still wins (the boost tops out at ×2.5 and
        never drops below ×1).

        Rows sort by the number of ``verbatim`` needles they hold first and by
        score second. Only a pure-Han run the dictionary split into words carries
        one (see :func:`parse_search_query`): the run is the user's own term, and
        the split words are how a session WITHOUT it still matches. So a session
        holding the run as typed ranks above one holding its words apart, however
        often those words repeat. Every other query sorts by score alone.

        Title matches get a strong field boost - titles are short and
        intentional, so a hit there is strong evidence.  Content matches
        are normalized by a sqrt length factor so a long session with a
        casual mention doesn't outrank a short, focused one.  (Simpler
        than BM25's ``(1-b) + b*(dl/avgdl)`` because we avoid the
        two-pass scan needed for corpus stats.)  Sessions missing any
        required needle are dropped.  Ties break by recency (existing
        ``list_sessions`` order - newest first).  Caps results at *limit*.
        Only the ``_SEARCH_SCAN_WINDOW`` most recent files are scored, so
        I/O stays bounded even with hundreds of sessions.

        *keys*, when given, restricts scoring to those session keys (as
        ``list_sessions`` spells them) inside that same window. It is applied
        BEFORE ranking and the *limit* cap, so sessions outside it can never
        crowd an allowed one off the page; an app-token caller passes the keys
        it owns. Membership is re-judged under the transcript lock through snippet
        extraction for each output row; a failed check or lock timeout drops the row.

        A query that starts with ``title:`` (see :func:`split_title_scope`) is
        scoped to titles: only sessions whose title matches its term qualify, and
        any text after the term is searched as above among them. With nothing after
        the term, :meth:`_search_titles` answers from titles alone. A bare
        ``title:`` names no title yet and matches nothing.
        """
        if not query or limit <= 0 or not self._log._dir.exists():
            return []
        title_filter: Callable[[str], bool] | None = None
        scope = split_title_scope(query)
        if scope is not None:
            if not scope.term:
                return []
            title_filter = scope.title_filter()
            if not scope.rest:
                return self._search_titles(title_filter, limit, keys)
            # The rest is the query from here on: it is what the content gate
            # and the snippet look for, and the term only narrows the window.
            query = scope.rest
        needles, phrase, adjacency_floor = parse_search_query(query)
        if not needles:
            # Whitespace-only query: no needles to require, so nothing can match.
            # Returning [] keeps this from degenerating into "match everything",
            # and skips reading every session file to reach that conclusion.
            return []
        required = [n for n in needles if n.required]
        # True when the phrase carries more than the single needle does, so an
        # exact-phrase bonus is meaningful. Not a token-count test: dedup
        # collapses "a a" to one needle while its phrase is still two words, and
        # a single CJK term expands to several needles while the phrase is what
        # rewards its characters appearing together.
        multi = [n.text for n in required] != [phrase]
        gate = _MemoGate(needles, phrase, multi)
        # Pinned for the whole query: a memo full of other terms evicts those,
        # never the terms this query is reading and filling right now.
        snippet_group = repr(_snippet_tiers(needles, phrase))
        with (
            _pinned(self._count_memo, gate.memo_keys),
            _pinned(self._snippet_memo, [snippet_group]),
        ):
            return self._search_parsed(
                query, limit, keys, needles, phrase, adjacency_floor, gate, title_filter
            )

    def _search_titles(
        self, title_filter: Callable[[str], bool], limit: int, keys: Container[str] | None
    ) -> list[dict]:
        """A ``title:`` query with nothing after its term: match on titles alone.

        Reads no transcript: ``list_sessions`` already carries every title, so
        this skips the index shortlist, the content folds and the snippets a
        content search pays for. For the same reason it is not held to the scan
        window, so a known chat older than the newest ``_SEARCH_SCAN_WINDOW``
        sessions is still found by its name. Rows keep ``list_sessions`` order,
        newest first, and carry no snippet. *keys* restricts the rows exactly as
        in :meth:`_search_parsed`, re-judged under the lock for each output row.
        """
        picked = [
            (meta, False)
            for meta in self._log.list_sessions()
            # The title first: it is a string test, while an app caller's *keys*
            # judges ownership per key.
            if title_filter(meta.get("title") or "") and (keys is None or meta["key"] in keys)
        ]
        return self._result_rows(picked[:limit], keys, "")

    def _search_parsed(
        self,
        query: str,
        limit: int,
        keys: Container[str] | None,
        needles: list[SearchNeedle],
        phrase: str,
        adjacency_floor: bool,
        gate: "_MemoGate",
        title_filter: Callable[[str], bool] | None = None,
    ) -> list[dict]:
        """The scoring and snippet half of :meth:`_search_sessions`.

        *title_filter*, when given (a ``title:`` query's term), makes the window
        the newest ``_SEARCH_SCAN_WINDOW`` sessions whose title it accepts, chosen
        from every session after the memo prune. Judging a title reads no
        transcript, so an older session that ``title:x`` finds is still searched
        for ``title:x more``, and the cap still bounds the transcripts scored.
        Like *keys*, it skips the content-memo prune, which must see every
        window session's row; the per-session cache prune still sees the usual
        window. What an older match adds to the caches and memos stays inside
        their byte budgets, and the next unrestricted search under pressure
        releases it.
        """
        multi = gate.multi
        now = _time.time()
        # (verbatim runs hit, score, -rank, meta, needs_snippet)
        scored: list[tuple[int, float, int, dict, bool]] = []
        sessions = self._log.list_sessions()
        scan_window = _facade_search_scan_window()
        window = sessions[:scan_window]
        self._log._prune_search_memos({m["key"] for m in window})
        if title_filter is not None:
            window = [m for m in sessions if title_filter(m.get("title") or "")]
        if keys is not None:
            # After the prune, which must see the whole window: the memos are
            # shared with every unrestricted search.
            window = [m for m in window if m["key"] in keys]
        if title_filter is not None:
            window = window[:scan_window]
        allowed, rowids = self._index_shortlist(window, needles)
        if keys is None and title_filter is None:
            # Only an unrestricted search sees every live session's row; one
            # restricted by *keys* or by a title would prune entries other
            # callers still reach.
            self._prune_content_memos(rowids)
        needle_keys = gate.needle_keys
        phrase_key = gate.phrase_key
        for rank, meta in enumerate(window):
            key = meta["key"]
            if allowed is not None and key not in allowed:
                # The index vouches for this session's current text and proved it
                # cannot satisfy the gate, so it is skipped without the read and
                # per-line parse that dominate a scan. This is the speedup; every
                # line below is the unchanged exact evaluation, run on far fewer
                # sessions.
                continue
            title_folded = (meta.get("title") or "").casefold()
            content = _ContentCounts(self, key, rowids, gate, title_folded)
            doc_chars = content.doc_chars
            length_norm = math.sqrt(1 + doc_chars / 1024)
            content_hits = 0.0
            saturated_hits = 0.0
            title_hits = 0.0
            adjacency_hits = 0
            verbatim_runs = 0
            disqualified = False
            for needle, needle_key in zip(needles, needle_keys):
                in_content = content.count(needle, needle_key)
                in_title = count_needle(needle, title_folded)
                if needle.required and not in_content and not in_title:
                    # AND semantics: one absent required needle disqualifies the
                    # session, so stop counting the rest.
                    disqualified = True
                    break
                if needle.verbatim:
                    # Ranks by the sort key below, not by score: its repeats
                    # already score through the words it is made of.
                    verbatim_runs += bool(in_content or in_title)
                    continue
                if needle.adjacency:
                    adjacency_hits += in_content + in_title
                if needle.saturate_body:
                    # A short substring's body frequency is mostly
                    # incidental text. Saturate the length-NORMALIZED count (hit
                    # density), so order among body-only matches is kept
                    # (1 < 10 < 1000 hits at equal length; one stray hit in a
                    # long transcript still loses to one in a short one) while
                    # thousands of digit hits cannot outrank a title. Kept
                    # apart from content_hits: it is already normalized, and
                    # dividing it again would compound the two penalties.
                    saturated_hits += math.log1p(in_content / length_norm) * needle.weight
                else:
                    content_hits += in_content * needle.weight
                title_hits += in_title * needle.weight
            if disqualified or not (content_hits or saturated_hits or title_hits):
                continue
            if adjacency_floor and not adjacency_hits:
                # Adjacency floor: a CJK query whose characters ALL appear but
                # never once adjacently (no bigram hit anywhere) is a character
                # scatter, not a word hit. Individual han/kana characters are
                # common enough that ranking alone cannot keep such noise off a
                # result page whose real hits number fewer than *limit* — so
                # scatter-only sessions are excluded, not merely down-ranked. A
                # document containing the query's words apart still qualifies:
                # each word is itself a bigram hit. The parse WAIVES the floor
                # (adjacency_floor=False) when its bigram set was truncated —
                # judging "no adjacency anywhere" against a partial set would
                # hide long-query matches — and when a required dictionary word
                # of 2+ characters already proves adjacency (see
                # parse_search_query).
                continue
            if multi:
                # Reward the words appearing together, in order, as typed.
                content_hits += content.count_phrase(phrase, phrase_key) * _PHRASE_BOOST
                title_hits += title_folded.count(phrase) * _PHRASE_BOOST
            score = title_hits * _TITLE_BOOST + content_hits / length_norm + saturated_hits
            # Recency boost: multiplicative and bounded to (1.0, 2.5], so a
            # fresh session with comparable relevance outranks a stale one, but
            # an old session with a decisively better match still wins — the
            # boost can at most double a score, never zero one out. Halves its
            # extra weight every _RECENCY_HALF_WEIGHT_DAYS of age.
            age_days = max(0.0, now - meta.get("modified", 0.0)) / 86400
            score *= 1.0 + _RECENCY_MAX_BOOST / (1.0 + age_days / _RECENCY_HALF_WEIGHT_DAYS)
            # Verbatim runs first: a session holding more of the query's split
            # runs exactly as typed outranks every session holding fewer, at any
            # score. Zero for every query with no dictionary split, so their
            # order is the score order it always was. Negate rank so a smaller
            # (newer) rank wins ties after the descending sort.
            scored.append((verbatim_runs, score, -rank, meta, (content_hits or saturated_hits) > 0))
        scored.sort(reverse=True)

        # Snippets are attached AFTER the sort+slice, so the cost is proportional
        # to the rows actually returned rather than to every session that
        # matched. A snippet cannot come from the folded cache (it needs the
        # original text, so offsets line up), so building one per match put a
        # full re-read back on the hot path — dominating the query once the fold
        # itself was memoized.
        picked = [(meta, needs_snippet) for _v, _s, _r, meta, needs_snippet in scored[:limit]]
        return self._result_rows(picked, keys, query)

    def _result_rows(
        self, picked: list[tuple[dict, bool]], keys: Container[str] | None, query: str
    ) -> list[dict]:
        """The output rows for ranked *picked* ``(meta, needs_snippet)`` pairs.

        A row flagged for a snippet gets the excerpt of *query* from its transcript.
        With *keys*, each row is re-judged under its transcript lock first.
        """
        out: list[dict] = []
        for meta, needs_snippet in picked:
            if keys is not None:
                # Scoring-time folds may race ownership and affect ranking/inclusion
                # only; the lock-held re-judge keeps foreign rows/snippets out of output.
                try:
                    with self._log._locked(meta["key"]):
                        if meta["key"] not in keys:
                            continue
                        # The scored title/count may predate the owned incarnation.
                        current = self._log.list_sessions(keys=(meta["key"],))
                        if not current:
                            continue
                        meta = current[0]
                        snippet = (
                            self._log._content_snippet(meta["key"], query) if needs_snippet else ""
                        )
                except _history_lock_timeout():
                    continue
            else:
                snippet = self._log._content_snippet(meta["key"], query) if needs_snippet else ""
            out.append({**meta, "snippet": snippet} if snippet else meta)
        return out

    def index_session(self, key: str) -> bool:
        """Index *key*'s current text, returning ``True`` when a row was written.

        Runs under the session's own ``_locked(key)`` for the whole stat → read →
        write, which is what orders it against deletion. Without the lock a delete
        landing between the read and the write re-inserts the text of a session
        that was just deleted: the transcript is gone, the row is gone, and then
        this writes the copy back — reachable, and it survives until some later
        backfill pass happens to evict it.

        The lock is not new cost. ``_build_folded`` — the scan path this replaces —
        already took it once per session PER QUERY, so moving the read behind the
        same lock in a background pass strictly reduces how long it is held.

        The stat is taken inside the lock and before the read, so the row records
        the revision its text actually came from.
        """
        index = self.search_index
        if not index.available:
            return False
        try:
            with self._log._locked(key):
                try:
                    st = self._log._path(key).stat()
                except OSError:
                    # The file is gone; make sure no row keeps claiming it.
                    index.drop([key])
                    return False
                try:
                    texts = list(self._log._iter_message_texts(key))
                except OSError:
                    return False
                index.sync(
                    key,
                    mtime_ns=st.st_mtime_ns,
                    size=st.st_size,
                    dev=st.st_dev,
                    ino=st.st_ino,
                    texts=texts,
                )
        except _history_lock_timeout():
            # A writer holds this session; the next pass picks it up. Reporting
            # False keeps the backfill's "remaining" count honest.
            return False
        return True

    def backfill_index(self, *, budget_secs: float = 5.0) -> dict[str, int]:
        """Index window sessions no row vouches for, within *budget_secs*.

        Returns counts so a caller can decide whether to run again soon. The
        budget is wall-clock and checked between sessions, so one very large
        session can overrun it — bounding that would mean abandoning a session
        half-indexed, and a row that describes half a file is exactly the kind of
        lie this design refuses to store.

        A session whose file changed within the last ``_INDEX_QUIET_WINDOW_SECS``
        is deferred, not indexed: it is still being written, and indexing it now
        buys a row the next turn invalidates. Deferred sessions are reported in
        their own ``deferred`` count, NOT in ``remaining`` — the caller's pass
        cadence keys on ``remaining``, and a deferral cannot be serviced by
        coming straight back, only by waiting out the window. They are picked up
        by the caller's idle-paced passes once quiet.

        Rows for sessions that have left the search window are dropped in the
        same pass. They are unreachable by search (the window is the only thing
        scored) so keeping them would grow the index without bound while
        answering nothing.
        """
        index = self.search_index
        if not index.available:
            return {"indexed": 0, "dropped": 0, "remaining": 0, "deferred": 0}
        window = self._log.list_sessions()[: _facade_search_scan_window()]
        window_keys = {meta["key"] for meta in window}
        stats: dict[str, os.stat_result] = {}
        for meta in window:
            try:
                stats[meta["key"]] = self._log._path(meta["key"]).stat()
            except OSError:
                continue
        fresh = index.fresh_keys(stats)
        quiet_cutoff_ns = _time.time_ns() - int(_INDEX_QUIET_WINDOW_SECS * 1e9)
        pending: list[str] = []
        deferred = 0
        for meta in window:
            key = meta["key"]
            if key in fresh:
                continue
            st = stats.get(key)
            if st is not None and st.st_mtime_ns > quiet_cutoff_ns:
                deferred += 1
                continue
            pending.append(key)
        departed = index.indexed_keys() - window_keys
        index.drop(departed)
        deadline = _time.monotonic() + budget_secs
        indexed = 0
        for key in pending:
            if _time.monotonic() > deadline:
                break
            if self.index_session(key):
                indexed += 1
        return {
            "indexed": indexed,
            "dropped": len(departed),
            "remaining": len(pending) - indexed,
            "deferred": deferred,
        }

    def _index_shortlist(
        self, window: list[dict], needles: list[SearchNeedle]
    ) -> tuple[set[str] | None, "dict[str, IndexRow]"]:
        """Narrow *window* to the sessions that could satisfy the gate.

        Returns ``(allowed, rowids)``. ``allowed`` is ``None`` when the index
        cannot narrow this query — no needle was long enough to look up, or the
        index is unavailable — in which case the caller scores every session
        exactly as it always did. ``rowids`` addresses the folded text of the
        sessions the index vouches for.

        Three unions make ``allowed`` a provable SUPERSET of the true hit set,
        and all three are load-bearing:

        * per NEEDLE, not per query, because the gate is satisfied field-wise —
          one needle may be found in the title while another is found in the
          content, so intersecting pre-combined content answers would drop that
          session;
        * a needle found in a session's TITLE is satisfied there, and titles are
          not indexed (they are already in memory from ``list_sessions``);
        * a session the index does not vouch for — never indexed, or its file has
          moved on — stays in play unconditionally, because no row describes its
          current text.
        """
        index = self.search_index
        if not index.available:
            return (None, {})
        stats: dict[str, os.stat_result] = {}
        for meta in window:
            try:
                stats[meta["key"]] = self._stat_key(meta["key"])
            except OSError:
                continue
        # One snapshot for both answers: read separately, a row dropped by the
        # backfill between them leaves its session vouched-for AND out of every
        # candidate set, which silently loses a hit the scan path would return.
        rowids, per_needle = index.shortlist(stats, needles)
        # Then re-stamp. The stats above were taken BEFORE the snapshot, so on their
        # own they can vouch for a row whose file has since grown -- not as a race,
        # but as an ordering error: the vouch would rest on an observation older than
        # the index read it certifies, and the session's superseded text would decide
        # whether it matches. Re-stating after the snapshot makes a vouch mean "as of
        # a moment no earlier than this read", which is the guarantee the scan path
        # gives by stat'ing and then folding. A key that moved is dropped here and
        # picked up by ``unvouched`` below, so it is scanned rather than skipped.
        for key in list(rowids):
            before = stats.get(key)
            try:
                after = self._stat_key(key)
            except OSError:
                del rowids[key]
                continue
            if (
                before is None
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_size != before.st_size
                or after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
            ):
                del rowids[key]
        if per_needle is None:
            return (None, rowids)
        unvouched = {meta["key"] for meta in window} - set(rowids)
        allowed: set[str] | None = None
        for pos, content_keys in per_needle.items():
            needle = needles[pos]
            in_title = {
                meta["key"]
                for meta in window
                if count_needle(needle, (meta.get("title") or "").casefold())
            }
            satisfied = content_keys | in_title | unvouched
            allowed = satisfied if allowed is None else (allowed & satisfied)
        return (allowed, rowids)

    def _stat_key(self, key: str) -> os.stat_result:
        """``self._log._path(key).stat()`` without building a ``Path`` per call.

        A key that is already a safe file name names ``<dir>/<key>.jsonl``
        whenever that file exists, which is the only case ``_path`` returns it
        without its legacy fallback; any failure defers to ``_path``, which
        owns that fallback.
        """
        from kiro_crew import history as history_facade

        if history_facade._safe_key(key) == key:
            try:
                return os.stat(os.path.join(self._log._dir, key + ".jsonl"))
            except OSError:
                pass
        return self._log._path(key).stat()

    def _folded_for(self, key: str, rowids: "dict[str, IndexRow]") -> tuple[int, str]:
        """Folded text for *key*, from the index when it vouches for the file.

        One index read replaces a whole-file read plus a ``json.loads`` per line —
        the ~85% of a scan that measured as pure overhead, since the bytes parsed
        are dominated by the ``meta`` blob that carries no searchable text. It
        also keeps the in-memory fold memo and its byte budget out of the picture
        for indexed sessions, so a warm query stops growing RSS with the corpus.

        Falls back to :meth:`_folded_content` — the original path, memo and all —
        whenever the row is missing, unreadable, or belongs to a different
        session, so a degraded or recycled index costs speed and never results.
        """
        row = rowids.get(key)
        if row is not None:
            doc = self.search_index.document(row.rowid, key)
            if doc is not None:
                return doc
        return self._log._folded_content(key)

    def _folded_content(self, key: str) -> tuple[int, str]:
        """Return ``(doc_chars, casefolded_content)`` for *key*, memoized by
        cache identity plus invalidation generation.

        ``doc_chars`` counts the ORIGINAL (unfolded) characters, because it
        feeds the length normalizer in :meth:`search_sessions` and folding can
        change a string's length (``ß`` -> ``ss``).

        The folded blob joins the messages' string ``content`` fields in file
        order with ``FOLD_SEPARATOR``. A user cannot type that character, so it
        prevents a match spanning two messages while still allowing one
        ``count`` call over the whole session instead of one per message. It is
        deliberately not ``\\x00``, which additionally ends the string for
        SQLite's tokenizers — see the constant for what that cost.

        Returns ``(0, "")`` for a missing/unreadable file or a session with no
        textual content. A read failure is deliberately NOT cached — see below.
        """
        path = self._log._path(key)
        try:
            identity = self._log._cache_identity(path.stat())
        except OSError:
            self._log._folded_cache.pop(key, None)
            self._log._snippet_cache.pop(key, None)
            return (0, "")
        cached = self._log._folded_cache.get(key)
        # The hit wants the LATEST generation (a moved counter means a write
        # landed, so a miss is the correct answer), so it is read at check
        # time rather than snapshotted earlier — matching ``_snippet_texts``.
        if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
            return (cached[2], cached[3])
        # Cold: serialize against this key's writers for the whole
        # stat -> read -> store sequence.
        #
        # The cache identity catches normal atomic housekeeping rewrites even
        # when they restore the pre-write mtime. The generation protects this
        # fill window too: a local writer can invalidate while a cache miss is
        # being built, so the store must not publish under its older generation.
        #
        # ``_file_lock`` is the same process-wide, path-keyed RLock every writer
        # takes first in ``_locked`` — shared across every ``ConversationLog``
        # instance over this file — so holding it here orders this fold against
        # append / rewrite / metadata edits for this key, whichever instance
        # performs them. It is acquired ONLY on the miss path: a warm search
        # never contends, and two threads racing the same cold key fold once
        # (the re-check below). What the lock CANNOT fix is invalidation reach:
        # a writer's ``_invalidate_cache`` pops only its own instance's caches,
        # so an entry already sitting warm in THIS instance survives a rewrite
        # performed through a different instance. That
        # is why entries carry the generation and the warm-hit checks above and
        # below require it to match.
        with self._log._file_lock(key):
            # Snapshot the fill baseline under the lock and BEFORE the stat.
            # An unmoved generation proves the stat → read → publish window
            # stayed write-free for this process. A writer that ran
            # between the lock-free probe and the acquire already bumped the
            # counter, and the fold below is ordered AFTER it, so its result is
            # current for this newer generation.
            gen = self._log._cache_gen(key)
            try:
                identity = self._log._cache_identity(path.stat())
            except OSError:
                self._log._folded_cache.pop(key, None)
                self._log._snippet_cache.pop(key, None)
                return (0, "")
            cached = self._log._folded_cache.get(key)
            if cached and cached[0] == identity and cached[1] == self._log._cache_gen(key):
                return (cached[2], cached[3])
            built = self._log._build_folded(key, identity, gen)
            if built is None:
                # The read failed rather than finding no content. Caching that
                # would be keyed by an mtime the file still has, so a session
                # made transiently unopenable (fd exhaustion, or a Windows
                # indexer / AV holding a just-written file — the same window
                # ``_METADATA_READ_ATTEMPTS`` exists for) would stay
                # unsearchable until something wrote to it again. Fail open:
                # report empty for this query and retry on the next one.
                return (0, "")
            self._log._publish_if_current(
                self._log._folded_cache, key, (identity, gen, built[0], built[1]), key=key, gen=gen
            )
            return built

    def _prune_content_memos(self, rowids: "dict[str, IndexRow]") -> None:
        """Free count and snippet entries whose content no window session has.

        Only runs for a memo whose budget is binding (it refused or evicted since
        the last prune), like :meth:`_prune_search_memos`. *rowids* holds every
        window session the index vouches for, so a digest absent from it belongs
        to text that has changed or left the window, and no search can ask for
        it again until that exact text returns.
        """
        live: _LiveDigests | None = None
        for memo in (self._count_memo, self._snippet_memo):
            if memo is not None and memo.refused_since_prune():
                if live is None:
                    live = _LiveDigests({row.digest for row in rowids.values()})
                memo.retain(live)

    def _prune_search_memos(self, live_keys: set[str]) -> None:
        """Free budget held by sessions that have left the scan window.

        Only runs for a memo that is currently refusing admissions, so an
        uncontended cache never pays the scan. Entries outside *live_keys* are
        unreachable by any future search (it walks only the
        ``_SEARCH_SCAN_WINDOW`` most recent sessions), so dropping them cannot
        cost a hit — which is what makes this safe where an LRU eviction would
        not be.

        Called before the walk, so a refusal raised *during* a walk is released
        on the NEXT query rather than that one: the prune needs the scan window,
        which the walk computes. The lag is one query (~20 ms for a keystroke
        caller) and self-clearing; what it prevents is budget staying pinned by
        aged-out sessions for the life of the process.
        """
        for cache in (self._log._folded_cache, self._log._snippet_cache):
            if cache.refused_since_prune():
                cache.retain(live_keys)

    def _build_folded(
        self,
        key: str,
        identity: tuple[int, int, int, int],
        gen: int,
    ) -> tuple[int, str] | None:
        """Parse *key* and fold its content — the cache-miss half of
        :meth:`_folded_content`.

        Returns ``None`` when the file could not be read, which the caller must
        distinguish from ``(0, "")`` (a session with no textual content): the
        former must not be cached.

        Reads the file via :meth:`_iter_message_texts` rather than going through
        :meth:`_read_messages`, for two reasons.

        Memory: that method memoizes the PARSED message dicts, and a search
        touches every session in the scan window, so routing the fold through it
        pins the whole corpus's parsed form in gateway RSS as a side effect of
        searching. On a 136 MB / 125-session corpus that is ~330 MB of parsed
        dicts versus ~37 MB for the folded strings this actually needs.

        Correctness: ``_msg_cache`` is filled by callers that do not hold this
        key's write lock. Reading the file makes the fold a function of the file
        alone, rather than coupling it to another cache's fill timing.

        The caller holds ``_file_lock``, which orders this read against writers
        in THIS process — the lock table is class-level and path-keyed, so that
        includes writers using other ``ConversationLog`` instances. A writer in
        another process holds only the cross-process flock, so it can still
        interleave. The cache identity catches the normal atomic rewrite even
        when it restores mtime; the process-local generation covers writers
        through another in-process instance. *gen* is the invalidation-generation
        snapshot the caller took alongside its stat; the snippet store below
        publishes under it and records it in the entry.

        Separated from :meth:`_folded_content` so the memoization is observable:
        a caller (or a test) can count how often the expensive fold actually
        runs, independent of how many queries were served.
        """
        texts: list[str] = []
        try:
            texts = list(self._log._iter_message_texts(key))
        except OSError:
            return None
        if not texts:
            return (0, "")
        # Hand the same list to the snippet memo. The caller has already stat'ed
        # under ``_file_lock`` and passes that identity and its generation
        # snapshot, so both memos are keyed by one observation of the file and
        # cannot disagree about which revision they hold. Storing here is why
        # the second corpus costs no extra read. The publish guard here is
        # generation-stamp hygiene: the lock already orders this store against
        # in-process writers, so its job is refusing to stamp an entry with an
        # already-superseded generation — the recorded generation is what the
        # warm-hit checks compare against.
        self._log._publish_if_current(
            self._log._snippet_cache, key, (identity, gen, texts), key=key, gen=gen
        )
        return (sum(len(t) for t in texts), fold_texts(texts))

    def _iter_message_texts(self, key: str) -> Iterator[str]:
        """Yield each message's non-empty string ``content`` from *key*'s file.

        One definition of "what counts as searchable text in a session file",
        shared by the fold and the snippet so their skip rules cannot drift apart
        as the on-disk format evolves. Yields in file order; skips blank lines,
        unparseable lines, non-object lines, and the metadata header.

        A generator rather than a list so a caller can stop early — closing it
        closes the file — which is what lets :meth:`_content_snippet` read only as
        far as its first match. Propagates ``OSError``: callers distinguish "could
        not read" from "no text", and that distinction is load-bearing (a read
        failure must not be cached).
        """
        path = self._log._path(key)
        with open(path, "rb") as handle:
            for line in bounded_records(handle, path, label="history_search"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("_type") == "metadata":
                    continue
                content = data.get("content")
                if isinstance(content, str) and content:
                    yield content

    #: Characters of context kept before / after a snippet's match.
    _SNIPPET_LEAD = 40
    _SNIPPET_TRAIL = 100
    #: Hard cap on a returned snippet.
    _SNIPPET_MAX = 200

    def _snippet_texts(self, key: str) -> Iterator[str]:
        """Yield *key*'s message texts for snippet extraction, memo first.

        Prefers ``_snippet_cache`` — filled by :meth:`_build_folded` from the same
        read that produced the fold — and falls back to re-reading the file.

        The memo is validated against the file's current cache identity AND the current
        invalidation generation (:meth:`_cache_gen`), so it degrades to the
        file read rather than serving a stale snippet. The generation clause
        covers a rewrite through a different in-process ``ConversationLog``
        instance (its ``_invalidate_cache`` pops only its own instance's caches).
        Both checks are cheap relative to the parse they avoid, and unlike the
        fold this path does NOT need ``_file_lock``: a snippet is display-only,
        so a racing rewrite can produce at most one stale preview line. The fold
        — which decides whether a row appears at all — keeps the lock.

        Falls back for four reasons, all of which must stay non-fatal: the entry
        was refused admission by the byte budget, the fold cached ``(0, "")`` for
        a session with no text and so stored nothing, the file changed since
        the fold, or the entry's generation was superseded by a write.
        Propagates ``OSError`` from the fallback read, which
        :meth:`_content_snippet` already treats as "no snippet".
        """
        cached = self._log._snippet_cache.get(key)
        if cached is not None:
            try:
                identity_now = self._log._cache_identity(self._stat_key(key))
                if cached[0] == identity_now and cached[1] == self._log._cache_gen(key):
                    return iter(cached[2])
            except OSError:
                # Let the fallback read raise the OSError the caller handles,
                # rather than deciding here what a vanished file means.
                pass
        # The index holds the same texts the memo would have, for every session
        # it vouches for — and unlike the memo it is not rationed by a byte
        # budget, so it answers for the whole window rather than the first ~80
        # sessions. This is where the last of the per-query file reads goes:
        # snippets are built for the returned rows only, and taking one from the
        # file costs a whole-transcript read and parse: 858 ms of a 947 ms query
        # once matching itself is indexed. Freshness is re-checked
        # inside ``iter_raw_texts`` against this stat, so a file that moved on since
        # the scoring pass falls through to the read below.
        if self._index is not None and self._index.available:
            try:
                st = self._stat_key(key)
            except OSError:
                st = None
            if st is not None:
                texts = self._index.iter_raw_texts(key, st)
                if texts is not None:
                    return self._texts_or_file(key, texts)
        return self._log._iter_message_texts(key)

    def _texts_or_file(self, key: str, texts: Iterator[str]) -> Iterator[str]:
        """Yield *texts*, continuing from the transcript if a stored chunk is unreadable."""
        served = 0
        try:
            for text in texts:
                served += 1
                yield text
            return
        except ChunkDecodeError:
            pass
        yield from itertools.islice(self._log._iter_message_texts(key), served, None)

    def _content_snippet(self, key: str, query: str) -> str:
        """Return a match-centered window of *key*'s content around *query*.

        Streams the file and stops at the FIRST matching message, so a query that
        hits early reads only as far as the hit — rather than parsing the whole
        transcript, which on the largest sessions dominated the query. Sharing
        :meth:`_iter_message_texts` with the fold (instead of reading the file
        directly) also keeps search from pinning a parsed transcript in
        ``_msg_cache`` for every row it returns, and keeps the two halves of a
        query agreeing on what counts as searchable text.

        The window is confined to the matching message, which keeps a snippet from
        reading as one sentence when it actually spans two — consistent with
        :meth:`_folded_content`, where the
        :data:`~kiro_crew.history_index.FOLD_SEPARATOR` join stops a match from
        bridging messages in the first place.

        Display-only and best effort: ``casefold`` is used for the search so it
        agrees with the hit detection in :meth:`search_sessions` (``.lower()``
        misses matches ``casefold`` finds, e.g. ``ß`` / ``İ``, which would yield
        an empty snippet despite a nonzero hit count), but folding can change a
        string's length, so the window may be off by a character or two.

        For a multi-word query the exact phrase is tried first and each needle
        is tried as a fallback (via :func:`snippet_needle_tiers`, highest-signal
        first), mirroring :meth:`search_sessions`' AND-over-needles matching —
        otherwise every session that matched on scattered words would come back
        with no snippet at all. Within a tier needles are tried per message, so
        the first MESSAGE containing any of them wins rather than the best
        needle overall; that preserves the streaming early-exit above. A query
        with no dictionary words is one tier. A query with them reads on past a
        lower-tier hit until a message holds the phrase, a verbatim run or a
        longer word, so a lone character in an early message cannot take the
        snippet from the phrase later on.

        Returns ``''`` when no needle is locatable in any single message, or
        when the file cannot be read (display-only — never raises at the caller).

        For a session the index vouches for, the result is memoized under the
        row's content digest, and a memoized snippet is built only from the
        stored text that the same read confirms still carries that digest.
        """
        tiers = snippet_needle_tiers(query)
        if not tiers:
            return ""
        memo = self._snippet_memo
        index = self._index
        if memo is None or index is None or not index.available:
            return self._build_snippet(key, tiers)
        try:
            st = self._stat_key(key)
        except OSError:
            return self._build_snippet(key, tiers)
        digest = index.fresh_digest(key, st)
        if digest is None:
            return self._build_snippet(key, tiers)
        # Keyed by the tiers, not the query: the snippet is a function of the
        # text and the tiers, and one query's tiers change when a segmenter
        # starts or stops answering.
        mkey = digest + repr(tiers)
        hit = memo.get(mkey)
        if hit is not None:
            return hit[0]  # type: ignore[return-value]
        texts = index.iter_raw_texts(key, st, digest)
        if texts is None:
            return self._build_snippet(key, tiers)
        try:
            snippet = self._snippet_from(texts, tiers)
        except ChunkDecodeError:
            return self._build_snippet(key, tiers)
        memo[mkey] = (snippet, _memo_entry_cost(mkey, snippet))
        return snippet

    def _build_snippet(self, key: str, tiers: list[list[str]]) -> str:
        """The match-centered window :meth:`_content_snippet` returns, uncached."""
        try:
            return self._snippet_from(self._log._snippet_texts(key), tiers)
        except OSError:
            return ""

    def _snippet_from(self, texts: Iterable[str], tiers: list[list[str]]) -> str:
        """The window around the best anchor :func:`tiered_first_hit` finds.

        An ``OSError`` while reading *texts* keeps the best hit read so far (none
        reports as no snippet); any other error propagates.
        """
        found = tiered_first_hit(texts, tiers)
        if found is None:
            return ""
        text, pos, needle = found
        start = max(0, pos - self._log._SNIPPET_LEAD)
        end = min(len(text), pos + len(needle) + self._log._SNIPPET_TRAIL)
        frag = " ".join(text[start:end].split())
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        return (prefix + frag + suffix)[: self._log._SNIPPET_MAX]


# --------------------------------------------------------------------------- #
# This module is the facade of the transcript-search family. Every name it defined,
# or bound from the rest of kiro_crew, before the owners existed is still reachable
# here -- bound, or, for the one global an owner rebinds at run time
# (``_SERVED_ONLY``), served from that owner --
# and a write through it (``monkeypatch.setattr``, ``mock.patch``) reaches every
# owner that binds the same object (:mod:`kiro_crew._facade_mirror`).
# ``SessionCatalogProjection`` stays here. Owners never import this module at run
# time. New work goes to the owner of its responsibility:
#
#   ``history_search_query`` -- query parsing: needles, forge references, provider
#                               spellings, the one-string match rule
#   ``history_search_memo``  -- the shared count and snippet memos, their byte
#                               ceilings, the single-flight table, memo-gated counts
#
# ``test/test_history_search_bindings.py`` pins the binding and the reach.
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = ("kiro_crew.history_search_query", "kiro_crew.history_search_memo")
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Globals an owner rebinds at run time: the facade serves them and never binds them.
_SERVED_ONLY = frozenset({"_search_ref_resolver"})
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | _SERVED_ONLY,
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.history_search_query import _search_ref_resolver  # noqa: F401
