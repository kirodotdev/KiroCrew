"""Persistent conversation history — JSONL per session + LLM consolidation.

Session files: ~/.kiro/crew/sessions/{safe_key}.jsonl
Each entry tracks provenance (source_thread, source_user) for citation.
Appends through ``ConversationLog.append`` auto-rotate at 10MB, keeping up to 200
lines within that byte cap. The dashboard whole-file save does not rotate, so a
transcript written only through it is bounded by its message window instead.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - facade re-export
import contextlib
import contextvars  # noqa: F401 - facade re-export
import importlib as _importlib
import json
import logging
import math  # noqa: F401 - facade re-export
import os
import re  # noqa: F401 - facade re-export
import stat  # noqa: F401 - facade re-export
import sys as _sys
import threading
import time as _time  # noqa: F401 - facade re-export
import uuid  # noqa: F401 - facade re-export
import weakref
from collections.abc import (  # noqa: F401 - facade re-export
    Callable,
    Container,
    Iterable,
    Iterator,
    Sequence,
)
from collections.abc import Set as AbstractSet
from datetime import datetime, timedelta  # noqa: F401 - facade re-export
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload

from kiro_crew import platform_compat  # noqa: F401 - facade re-export
from kiro_crew import _facade_mirror
from kiro_crew import history_append as _history_append
from kiro_crew import history_derivation as _history_derivation
from kiro_crew import history_locks as _history_locks
from kiro_crew import history_marker as _history_marker
from kiro_crew import history_summaries as _history_summaries
from kiro_crew import history_threads as _history_threads
from kiro_crew.atomic_write import (  # noqa: F401 - facade re-export
    append_line,
    atomic_write,
    atomic_write_at,
    fsync_dir,
)
from kiro_crew.chat_attachments import (  # noqa: F401 - facade re-export
    persist_inline_images,
    same_text_modulo_images,
)
from kiro_crew.config.loader import KiroCrewConfig, config_dir  # noqa: F401 - facade re-export

# Every ``memory_mode`` a transcript header may record, least strict first. The one
# allowlist lives in `execution_context`, whose order `stricter_memory_mode` reads;
# re-exported here for the header parse and the dashboard's ``VALID_MEMORY_MODES``.
from kiro_crew.execution_context import MEMORY_MODES  # noqa: F401 - facade re-export
from kiro_crew.executors import run_in_embed_pool  # noqa: F401 - facade re-export
from kiro_crew.frontmatter import (  # noqa: F401 - facade re-exports
    SKILL_UPDATE,
    frontmatter_value,
)
from kiro_crew.history_append import (  # noqa: F401 - facade re-exports
    _redact_at_write_boundary,
)
from kiro_crew.history_cache import (
    _METADATA_CACHE_MAX,
    _PARSED_BYTES_PER_FILE_BYTE,
    _TRANSCRIPT_CACHE_IDLE_TTL,
    _TRANSCRIPT_CACHE_MAX,
    _TRANSCRIPT_CACHE_MAX_BYTES,
    HistoryCacheCoordinator,
    _FileChangeCacheEntry,
    _LRUCache,
    _SearchTextCache,
    _TranscriptIndexBase,
    _TranscriptRowIndexEntry,
)
from kiro_crew.history_consolidation import (  # noqa: F401 - facade re-exports
    _CONSOLIDATION_BACKOFF_BASE_SECS,
    _CONSOLIDATION_BACKOFF_MAX_SECS,
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_META_KEYS,
    _CONSOLIDATION_PROMPT_BUDGET_CHARS,
    _CONSOLIDATION_REFUSED,
    _CONSOLIDATION_SUBOFFSET_META_KEYS,
    _CONSOLIDATION_THRESHOLD,
    _PLACEHOLDER_BODIES,
    _SENSITIVE_TOOL_PATTERNS,
    _SKILL_DETECTION_WINDOW,
    _TOOL_ROLES,
    AttemptedSpan,
    HistoryConsolidator,
    _consolidation_chunk,
    _ConsolidationNotDispatched,
    _ConsolidationRefusedSentinel,
    _count_tool_call_messages,
    _fmt_message,
    _frontmatter_value,
    _is_plausible_memory_file,
    _merge_trigger_lists,
    _session_touched_sensitive,
    _strip_code_fence,
    _strip_skill_frontmatter,
)
from kiro_crew.history_derivation import (  # noqa: F401 - facade re-exports
    INCOGNITO_EXCLUDED_HEADER,
    INCOGNITO_MEMORY_MODES,
    TRANSCRIPT_HEADER_MAX_BYTES,
    TranscriptBusy,
    TranscriptWithheld,
    is_incognito_transcript,
    memory_mode_from_header_line,
    transcript_privacy_mode,
    transcript_withholds_derivation,
)
from kiro_crew.history_files import (  # noqa: F401 - facade re-exports
    _TAB_ID_INDEX_GLOB,
    _TAB_ID_INDEX_STEM_PREFIX,
    ARCHIVE_DIR_NAME,
    ARCHIVE_RETENTION_DAYS,
    ARCHIVE_SEGMENT_DELIMITER,
    SESSIONS_DIR_NAME,
    CacheIdentity,
    _archive_dir,
    _archive_lines,
    _cache_identity,
    _cleanup_expired_crew_logs,
    _cleanup_old_archives,
    _index_key_for_stem,
    _resolve_retention_days,
    _restore_mtime,
    _safe_key,
    _safe_mtime,
    _sessions_dir,
    can_hold_tab_id_index_entry,
    transcript_lock_stems,
    transcript_stem,
    transcript_stems,
)
from kiro_crew.history_locks import (  # noqa: F401 - facade re-exports
    _FLOCK_ACQUIRE_TIMEOUT_S,
    _FLOCK_POLL_INTERVAL_S,
    HistoryLockTimeout,
)
from kiro_crew.history_offload import (  # noqa: F401 - facade re-exports
    _ON_LOOP_FALSY,
    _ON_LOOP_TRUTHY,
    _ON_LOOP_WARN_INTERVAL_S,
    OnLoopPersistError,
    _allow_on_loop_persist,
    _check_on_loop_persist_discipline,
    _on_loop_persist_strict,
    allow_on_loop_persist,
    append_if_absent_off_loop,
    append_off_loop,
    append_rows_if_absent_off_loop,
    on_loop_persist_strict,
    update_metadata_off_loop,
)
from kiro_crew.history_projection import (  # noqa: F401 - facade re-exports
    METADATA_LINE_CORRUPT,
    METADATA_LINE_READABLE,
    METADATA_LINE_TRANSIENT,
    ChainRevision,
    SessionMetadataProjection,
    TranscriptPage,
    TranscriptReadProjection,
)
from kiro_crew.history_rewrite import HistoryRewriteCoordinator
from kiro_crew.history_schema import (  # noqa: F401 - facade re-exports
    _COMPACT_OWNED_META_KEYS,
    HUMAN_TURN_META_KEY,
    PROVENANCE_FIELDS,
    ROWS_ONLY_DEFERRED_META_KEYS,
    ROWS_ONLY_OWNED_META_KEYS,
    SLOT_OWNED_META_KEYS,
    _parse_transcript_ts,
    carry_provenance,
    carry_unowned_metadata,
    latest_transcript_ts,
    metadata_now_iso,
    mint_row_mid,
    monotonic_transcript_ts,
    transcript_sort_key,
)
from kiro_crew.history_search import (  # noqa: F401 - facade re-exports
    _CJK_CHAR_WEIGHT,
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
    _PHRASE_BOOST,
    _RECENCY_HALF_WEIGHT_DAYS,
    _RECENCY_MAX_BOOST,
    _SEARCH_FOLD_BUDGET_BYTES,
    _SEARCH_MAX_FORGE_REFS,
    _SEARCH_MAX_SCORING_EXTRAS,
    _SEARCH_SCAN_WINDOW,
    _SEARCH_SNIPPET_BUDGET_BYTES,
    _TITLE_BOOST,
    SEARCH_MAX_TOKENS,
    SEARCH_MIN_CHARS,
    SearchNeedle,
    SessionCatalogProjection,
    _forge_lead_in,
    _forge_spellings,
    _forge_type_suffix,
    _ForgeRef,
    _is_cjk_char,
    _lead_names_a_type,
    _lead_names_merge_request,
    _parse_forge_ref,
    _script_runs,
    count_needle,
    needles_match_text,
    parse_search_query,
    snippet_needles,
)
from kiro_crew.history_threads import (  # noqa: F401 - facade re-exports
    _THREAD_REPLY_ROLES,
    _THREAD_REPLY_TS_RE,
    THREAD_MID_RE,
    THREAD_REPLY_CONTENT_MAX_CHARS,
    THREAD_REPLY_FIELDS,
    THREAD_REPLY_ID_RE,
    THREADS_DIR_NAME,
    THREADS_MAX_REPLIES_PER_SIDECAR,
    THREADS_MAX_REPLIES_PER_THREAD,
    THREADS_SIDECAR_MAX_BYTES,
    THREADS_SIDECAR_SUFFIX,
    ThreadStoreUnreadable,
    _thread_reply_row,
    _write_thread_sidecar,
    threads_sidecar_for_stem,
)
from kiro_crew.llm_helpers import (  # noqa: F401 - facade re-exports
    ToolApprovalPolicy,
    background_turn,
    stream_and_collect,
    stream_and_collect_json,
)
from kiro_crew.messaging.link import (  # noqa: F401 - facade re-export
    canonical_key,
    is_legacy_slack_key,
    legacy_key,
)
from kiro_crew.owner_only_files import (  # noqa: F401 - facade re-export
    OWNER_ONLY_FILE_MODE,
    mkdirs_owner_only,
    owner_only_opener,
)
from kiro_crew.preview_text import (  # noqa: F401 - facade re-export
    PREVIEW_MAX_CHARS,
    strip_markdown_preview,
)
from kiro_crew.security import (  # noqa: F401 - facade re-export
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401 - facade re-export
from kiro_crew.skills import (  # noqa: F401 - facade re-export
    AUTO_SKILL_MAX_PROCEDURE_CHARS,
    AutoSkillProvenance,
)
from kiro_crew.skills_dedupe import (  # noqa: F401 - facade re-exports
    VERDICT_DUP,
    VERDICT_NEW,
    VERDICT_UPDATE,
    metadata_dedupe_verdict,
)
from kiro_crew.skills_script_validator import validate_skill_script  # noqa: F401 - facade re-export
from kiro_crew.vector_memory_constants import (  # noqa: F401 - facade re-exports
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

logger = logging.getLogger(__name__)

HistoryConsolidator.__module__ = __name__
AttemptedSpan.__module__ = __name__
ThreadStoreUnreadable.__module__ = __name__
HistoryLockTimeout.__module__ = __name__
OnLoopPersistError.__module__ = __name__
TranscriptWithheld.__module__ = __name__
TranscriptBusy.__module__ = __name__


_SESSION_MAX_BYTES = 10 * 1024 * 1024  # 10MB
_SESSION_KEEP_LINES = 200


# Metadata reads retry briefly before reporting "no metadata": on Windows a
# just-written session file can be transiently unopenable while an indexer or AV
# scanner holds it (ERROR_SHARING_VIOLATION -> PermissionError). Those holds are
# measured in milliseconds, and the caller that matters here (the open-tab
# restore) treats an empty result as "session never existed" and drops the tab.
_METADATA_READ_ATTEMPTS = 3
_METADATA_READ_RETRY_SECS = 0.02


class ConversationLog:
    """Append-only JSONL conversation store with provenance and rotation."""

    # Per-file reentrant locks shared by every instance in this process, so
    # writes to one session file are serialized.
    #
    # A weak map lets a lock evict once unreferenced. That is safe only because
    # every caller of ``_file_lock`` holds a strong reference for its whole
    # critical section, so a held lock cannot evict and concurrent writers get
    # the SAME object. Do NOT pop the entry on release in
    # ``history_locks._file_lock``: that could hand two writers different locks.
    _file_locks: weakref.WeakValueDictionary[str, threading.RLock] = weakref.WeakValueDictionary()
    _file_locks_guard = threading.Lock()

    # Cross-process advisory-lock state. ``_locked`` layers a POSIX ``flock`` on a
    # per-session ``.lock`` sidecar over the in-process RLock, so subagent, cron
    # and CLI processes cannot interleave writes or see a torn rewrite.
    # ``_flock_state`` maps lock_key -> ``[fd, depth, held]``: a re-entrant
    # acquire reuses the held fd, because ``flock``-ing a second fd of the same
    # file blocks against ourselves. The entry stays ``held``=1 between a depth->0
    # exit and the off-loop release, so a re-acquire in that window reuses it
    # instead of failing its non-blocking on-loop attempt. ``_flock_guard``
    # covers only the dict bookkeeping; the blocking ``flock`` runs outside it.
    _flock_state: dict[str, list[int]] = {}
    _flock_guard = threading.Lock()

    # Permanent deletes in flight in THIS process, keyed by store directory and
    # transcript lock stem (see ``_delete_in_flight_marks``) and counted so nested windows (the session-delete
    # handler's window around ``delete_session``'s own) release cleanly. A
    # delete holds the transcript lock across its whole transaction, so a
    # lock-free existence probe made inside it still sees the file and cannot
    # tell "present" from "about to be unlinked". Resume consults this before
    # it publishes a slot (see :meth:`delete_in_flight`). Cross-process deletes
    # (the CLI) are not visible here; the slot save's delete-won guard is the
    # backstop for those.
    _deletes_in_flight: dict[str, int] = {}
    _deletes_in_flight_guard = threading.Lock()

    # Count of flock RELEASES per lock_key, bumped under ``_flock_guard``. Part of
    # the unlocked-fill publish witness (:meth:`_flock_hold_witness`): "held" at
    # two instants does not prove a continuous hold, and fd numbers recycle, but
    # an unchanged (fd, epoch) pair does.
    _flock_epochs: dict[str, int] = {}

    # Per-key invalidation generation, bumped by ``_invalidate_cache`` BEFORE it
    # drops entries. Fill paths snapshot it before their stat and publish only
    # while it is unmoved, which proves the fill stayed write-free.
    # Class-level because the writer may be another ``ConversationLog`` over the
    # same directory. Keyed by ``(transcript dir, sanitized stem)`` (see
    # ``_cache_gen``): no I/O, and both key spellings share one counter.
    # ``_cache_gens_guard`` is always innermost (taken under ``_file_lock``, never
    # across I/O). Never evicted: a dropped counter would reset to 0 and let a
    # stale snapshot read as current.
    _cache_gens: dict[tuple[str, str], int] = {}
    _cache_gens_guard = threading.Lock()
    #: Per transcript directory, the total of every bump above: one int per
    #: directory, moved under ``_cache_gens_guard`` with the bumps it sums.
    _dir_write_epochs: dict[str, int] = {}

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        cache_max: int = _TRANSCRIPT_CACHE_MAX,
    ):
        self._dir = base_dir or _sessions_dir()
        # Bounded identity-keyed LRU caches. Bounded so
        # a long-lived gateway touching thousands of sessions cannot grow the
        # parsed-transcript working set without limit. Eviction is
        # least-recently-used and deterministic; writes invalidate per-key via
        # _invalidate_cache so a stale entry can never outlive a file change.
        # Also bounded by bytes: entry [0] is the file's CacheIdentity, whose
        # [2] is its size, so the weight is the file size times the measured
        # parse ratio (see _TRANSCRIPT_CACHE_MAX_BYTES).
        self._msg_cache: _LRUCache[tuple[CacheIdentity, int, list[dict]]] = _LRUCache(
            cache_max,
            idle_ttl=_TRANSCRIPT_CACHE_IDLE_TTL,
            max_weight=_TRANSCRIPT_CACHE_MAX_BYTES,
            weigh=lambda entry: entry[0][2] * _PARSED_BYTES_PER_FILE_BYTE,
        )
        #: ``(identity, gen, meta)``. A hit needs both identity and generation, so
        #: a metadata edit through another instance still unhits.
        #:
        #: Sized by ``_METADATA_CACHE_MAX``, not ``cache_max``: ``list_sessions``
        #: reads it in a whole-directory cyclic scan that a smaller LRU cannot hit.
        #: Tests needing a small bound assign ``_meta_cache`` directly.
        self._meta_cache: _LRUCache[tuple[CacheIdentity, int, dict]] = _LRUCache(
            _METADATA_CACHE_MAX
        )
        #: ``((identity, size), gen, title_or_None)`` — the first-user-message
        #: title ``list_sessions`` derives for a session whose metadata line
        #: carries no title. Without it every listing would re-open and re-read
        #: the head of every untitled transcript to compute a value that only
        #: changes when the file does. Same generation guard and bound as
        #: ``_meta_cache``, plus the size: the identity alone does not move for
        #: two writes inside one timestamp tick. ``None`` records "no title in
        #: the head", which is just as cacheable.
        self._title_scan_cache: _LRUCache[tuple[tuple[tuple[int, int, int], int], int, object]] = (
            _LRUCache(_METADATA_CACHE_MAX)
        )
        #: size or inode moves on every write path in this repo; ctime moves on POSIX only
        self._has_messages_cache: _LRUCache[tuple[CacheIdentity, int, bool]] = _LRUCache(
            _METADATA_CACHE_MAX
        )
        #: Bounded identity-keyed LRU of formatted ``recent()`` windows keyed by
        #: (key, max_messages, roles). The tail-read fast path intentionally
        #: never warms ``_msg_cache`` (it returns a partial view), so a session
        #: accessed *only* via ``recent()`` — the hot per-turn context path —
        #: would otherwise re-open and re-parse the file tail on every single
        #: call. This memoizes the formatted window; the stored identity guards
        #: staleness even when a housekeeping write preserves mtime. Own
        #: ``_LRUCache`` → own internal lock.
        self._recent_cache: _LRUCache[tuple[CacheIdentity, int, list[dict]]] = _LRUCache(
            cache_max, idle_ttl=_TRANSCRIPT_CACHE_IDLE_TTL
        )
        #: Bounded memo of message projections holding only ``ts`` and
        #: ``meta.file_changes``, for the Artifacts "All" scan, so it does not
        #: retain every parsed transcript. The stamp includes inode and size so
        #: rotations and atomic rewrites miss. Sized by ``_METADATA_CACHE_MAX``
        #: because a cyclic scan defeats a smaller LRU; entries are small.
        self._file_change_cache: _LRUCache[_FileChangeCacheEntry] = _LRUCache(_METADATA_CACHE_MAX)
        #: Sparse row-to-byte indexes for paginated transcript reads. Entries
        #: contain offsets and counts only, never message content, so a bounded
        #: page does not retain the complete parsed transcript in memory.
        self._page_index_cache: _LRUCache[_TranscriptRowIndexEntry] = _LRUCache(cache_max)
        #: The last complete row index per transcript, deliberately NOT dropped by
        #: ``_invalidate_cache``: an append only extends the file, so the next
        #: bounded page scans the new bytes instead of the whole file. It is
        #: re-validated against the file on every use (see ``_TranscriptIndexBase``).
        self._page_index_base: _LRUCache[_TranscriptIndexBase] = _LRUCache(cache_max)
        #: Bounded memo of ``(identity, gen, doc_chars, casefolded_blob)`` per
        #: session, for :meth:`search_sessions`. A hit needs both identity and
        #: generation (:meth:`_cache_gen`), because a rewrite through another
        #: instance only bumps the generation.
        #:
        #: ``str.casefold`` over the corpus dominates a search and holds the GIL,
        #: so it would stall the event loop per keystroke; the fold is memoized.
        #: Bounded by BYTES (``_SEARCH_FOLD_BUDGET_BYTES``): :class:`_SearchTextCache`
        #: refuses admission instead of evicting, so a cyclic scan keeps hitting.
        self._folded_cache: _SearchTextCache[tuple[CacheIdentity, int, int, str]] = (
            _SearchTextCache(_SEARCH_FOLD_BUDGET_BYTES, lambda v: v[3].__sizeof__(), "fold")
        )
        #: session key → (identity, gen, raw message texts) for snippet extraction.
        #: Without it every returned row re-parsed its JSONL up to the first hit
        #: (92% of a warm query when profiled). Filled by :meth:`_build_folded`,
        #: which already builds this list, so it costs no extra read. Raw because
        #: the snippet is shown to the user. Same generation guard as
        #: ``_folded_cache``: both go stale together.
        self._snippet_cache: _SearchTextCache[tuple[CacheIdentity, int, list[str]]] = (
            _SearchTextCache(
                _SEARCH_SNIPPET_BUDGET_BYTES,
                lambda v: v[2].__sizeof__() + sum(t.__sizeof__() for t in v[2]),
                "snippet",
            )
        )
        #: tab_id → [session keys] chain index. ``None`` means "stale, rebuild
        #: on next chained read"; a dict is an authoritative snapshot. Rebuilt
        #: lazily by _rebuild_tab_id_index, invalidated by
        #: invalidate_tab_id_cache on every append / metadata edit / delete
        #: that can change the chain. Guarded by ``self._lock`` because
        #: ``read_messages_chained`` is reachable from worker threads while the
        #: event loop may mark it stale — an unsynchronized rebuild/read/clear
        #: produced a transient empty index or ``AttributeError``.
        self._tab_id_index: dict[str, list[str]] | None = None
        #: session key → (mtime, tab_id) memo feeding the rebuild above. A plain
        #: dict, not an _LRUCache: the rebuild is a cyclic scan, which defeats a
        #: bounded LRU. Values are 12-char ids.
        #:
        #: Two guards. The pop in _invalidate_cache covers this instance's writes,
        #: which restore mtime (_restore_mtime). The stamp (st_mtime_ns, st_size,
        #: st_ino) covers hand edits and other instances' writes; ino is needed
        #: because an equal-length rewrite keeps mtime and size.
        self._tab_id_by_key: dict[str, tuple[tuple[int, int, int], str]] = {}
        #: Bumped by _invalidate_cache. The rebuild samples it before reading a
        #: file's metadata and declines to memoize if it moved, so a store cannot
        #: land after a concurrent writer's pop and resurrect a stale id.
        #: _invalidate_cache deliberately does not take self._lock, so the
        #: rebuild cannot exclude it.
        self._tab_id_generation = 0
        #: Coarse instance lock protecting the lazily-built ``_tab_id_index``
        #: rebuild/read/clear. The message/metadata/recent LRUs are each
        #: internally locked; this guards the shared mutable state that lives
        #: directly on the instance.
        self._lock = threading.RLock()
        #: When True, ``recent``/``recent_chained`` may satisfy a cache miss by
        #: reading only the TAIL of the session file instead of parsing the
        #: whole thing. Correctness-neutral — see
        #: :meth:`_read_tail_messages`.
        self._tail_reads = True
        self._cache_coordinator = HistoryCacheCoordinator(
            self,
            safe_key=lambda key: _safe_key(key),
            registry_owner=ConversationLog,
        )

        self._catalog_projection = SessionCatalogProjection(self)
        self._read_projection = TranscriptReadProjection(self)
        self._metadata_projection = SessionMetadataProjection(self)
        self._rewrite_coordinator = HistoryRewriteCoordinator(self)

    @staticmethod
    def _cache_identity(stat: os.stat_result) -> CacheIdentity:  # noqa: F811
        return _cache_identity(stat)

    def init(self) -> None:
        """Create sessions directory if missing (owner-only, like every transcript in it)."""
        mkdirs_owner_only(self._dir)

    def _path(self, key: str) -> Path:
        p = self._dir / f"{_safe_key(key)}.jsonl"
        if not p.exists():
            # Back-compat: Slack threads created before the canonical
            # ``slack:<ts>`` session-key migration logged under the bare
            # thread_ts filename. Keep reading/appending the legacy file for
            # those threads so a thread active across the migration doesn't
            # split its log; brand-new threads create the canonical file.
            bare = legacy_key(key)
            if bare is not None:
                legacy = self._dir / f"{_safe_key(bare)}.jsonl"
                if legacy.exists():
                    return legacy
        return p

    def has_log(self, key: str) -> bool:
        """Return True if a conversation log file exists for *key*."""
        return self._path(key).exists()

    def has_messages(self, key: str) -> bool:
        """Return True if *key*'s transcript holds at least one message row.

        A transcript file is created by the first METADATA write -- a title,
        an agent pick, a model pick -- long before any message is exchanged,
        so :meth:`has_log` answers "does a file exist", not "was anything
        said". Callers deciding whether a conversation already carries
        context before selecting a member need the second question:
        a metadata-only file is an empty conversation.

        An absent file is empty, but a file that exists and cannot be
        read raises ``OSError`` rather than reading as empty, and a record that
        cannot be delivered intact or is not valid JSON counts as content --
        unverifiable history is still history. The forgiving tail readers are
        not used here for that reason.
        """
        from kiro_crew.jsonl_util import UnreadableRecord, strict_records

        path = self._path(key)
        with self._locked(key):
            try:
                handle = open(path, "rb")
            except FileNotFoundError:
                return False
            with handle:
                try:
                    for record in strict_records(handle, path):
                        line = record.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except ValueError:
                            return True
                        if isinstance(data, dict) and data.get("_type") == "metadata":
                            continue
                        return True
                except UnreadableRecord:
                    return True
        return False

    def listing_has_messages(self, key: str) -> bool:
        """:meth:`has_messages` for the Older Sessions listing, memoized per file.

        The listing asks this of every untitled transcript on each refresh. An
        answer is reused while the file's stamp and this log's write generation
        both match, so an unchanged transcript costs one ``stat`` instead of the
        per-key lock and an open. Only the listing calls this: the callers that
        gate memory on the answer keep the locked, uncached :meth:`has_messages`.
        """
        path = self._path(key)
        # Generation BEFORE the stat, as in list_sessions: a write landing
        # between them bumps it and the publish below discards itself.
        gen = self._cache_gen(key)
        try:
            identity = self._cache_identity(os.stat(path))
        except OSError:
            # Absent or unstattable: the locked read decides, uncached.
            return self.has_messages(key)
        cached = self._has_messages_cache.get(key)
        if cached is not None and cached[0] == identity and cached[1] == self._cache_gen(key):
            return cached[2]
        result = self.has_messages(key)
        self._publish_if_current(
            self._has_messages_cache, key, (identity, gen, result), key=key, gen=gen
        )
        return result

    def session_mtime(self, key: str) -> float | None:
        """Return the session file's mtime, or None if it can't be stat'd.

        Advances on every real message :meth:`append` but is preserved across
        metadata-only writes (see :func:`_restore_mtime`), so it is a faithful
        "has this conversation changed?" signal — used as the cache-validity
        signature for derived artifacts like on-demand session summaries.
        """
        return _safe_mtime(self._path(key))

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent(key, max_messages, roles, exclude_last_n=exclude_last_n)

    def recent_chained(
        self,
        key: str,
        max_messages: int = 20,
        roles: AbstractSet[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent_chained(
            key, max_messages, roles, exclude_last_n=exclude_last_n
        )

    def recent_with_provenance(
        self,
        key: str,
        max_messages: int = 3,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        return self._read_projection.recent_with_provenance(
            key, max_messages, exclude_last_n=exclude_last_n
        )

    def get_unconsolidated(self, key: str) -> tuple[list[dict], int]:
        """Return (messages_after_last_consolidated, total_message_count)."""
        messages = self._read_messages(key)
        return messages[self.consolidated_offset(key) :], len(messages)

    def chained_keys(self, key: str) -> list[str]:
        """Every transcript key :meth:`read_messages_chained` concatenates for *key*."""
        return self._read_projection.chained_keys(key)

    @staticmethod
    def _canonical_key(key: str) -> str:
        return SessionCatalogProjection._canonical_key(key)

    def list_sessions(self, *, keys: Iterable[str] | None = None) -> list[dict]:
        if keys is None:
            return self._catalog_projection.list_sessions()
        return self._catalog_projection.list_sessions(keys=keys)

    def list_sessions_status(self) -> tuple[list[dict], bool]:
        return self._catalog_projection.list_sessions_status()

    def newest_session_stems(self, max_entries: int) -> tuple[list[tuple[str, float]], bool]:
        return self._catalog_projection.newest_session_stems(max_entries)

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        return self._catalog_projection.agent_usage()

    def search_sessions(
        self, query: str, limit: int = 50, *, keys: Container[str] | None = None
    ) -> list[dict]:
        return self._catalog_projection.search_sessions(query, limit, keys=keys)

    def warm_search(self) -> None:
        self._catalog_projection.warm_search()

    def _folded_content(self, key: str) -> tuple[int, str]:
        return self._catalog_projection._folded_content(key)

    def _prune_search_memos(self, live_keys: set[str]) -> None:
        self._catalog_projection._prune_search_memos(live_keys)

    def _build_folded(
        self,
        key: str,
        identity: CacheIdentity,
        gen: int,
    ) -> tuple[int, str] | None:
        return self._catalog_projection._build_folded(key, identity, gen)

    def _iter_message_texts(self, key: str) -> Iterator[str]:
        return self._catalog_projection._iter_message_texts(key)

    _SNIPPET_LEAD = SessionCatalogProjection._SNIPPET_LEAD
    _SNIPPET_TRAIL = SessionCatalogProjection._SNIPPET_TRAIL
    _SNIPPET_MAX = SessionCatalogProjection._SNIPPET_MAX

    def _snippet_texts(self, key: str) -> Iterator[str]:
        return self._catalog_projection._snippet_texts(key)

    def _content_snippet(self, key: str, query: str) -> str:
        return self._catalog_projection._content_snippet(key, query)

    def recent_from_source(
        self, source_prefix: str, exclude_key: str = "", max_messages: int = 20
    ) -> list[dict]:
        return self._read_projection.recent_from_source(source_prefix, exclude_key, max_messages)

    def read_messages(self, key: str) -> list[dict]:
        return self._read_projection.read_messages(key)

    def read_file_change_messages(self, key: str) -> list[dict]:
        return self._read_projection.read_file_change_messages(key)

    def read_messages_chained(self, key: str) -> list[dict]:
        return self._read_projection.read_messages_chained(key)

    def read_messages_chained_tail(self, key: str, limit: int) -> list[dict]:
        """The newest *limit* rows of :meth:`read_messages_chained`, read from the end.

        See ``TranscriptReadProjection.read_messages_chained_tail``.
        """
        return self._read_projection.read_messages_chained_tail(key, limit)

    def read_messages_chained_full(self, key: str) -> list[dict]:
        """Chained transcript INCLUDING each key's size-rotated archive head.

        The pagination/fork index space: `before` / `next_before` cursors from
        the slot-detail handler address rows of THIS corpus. The plain
        `read_messages_chained` stays the window/consolidation corpus — its
        callers hold offsets (``_disk_older_count``, ``last_consolidated``)
        counted against the un-archived files, which rotated rows must not shift.
        """
        return self._read_projection.read_messages_chained_full(key)

    def read_rotated_messages_chained(self, key: str) -> list[dict]:
        return self._read_projection.read_rotated_messages_chained(key)

    def read_rotated_messages(self, key: str) -> list[dict]:
        """Rows size-rotation archived out of *key*'s own transcript, oldest first."""
        return self._read_projection.read_rotated_messages(key)

    def chain_mid_rotation(self, key: str) -> bool:
        """See ``HistoryReadProjection.chain_mid_rotation``."""
        return self._read_projection.chain_mid_rotation(key)

    def read_messages_chained_page(
        self,
        key: str,
        *,
        limit: int,
        before: int | None = None,
        expected_revision: ChainRevision | None = None,
    ) -> TranscriptPage:
        return self._read_projection.read_messages_chained_page(
            key,
            limit=limit,
            before=before,
            expected_revision=expected_revision,
        )

    def _rebuild_tab_id_index(self) -> None:
        self._read_projection._rebuild_tab_id_index()

    def invalidate_tab_id_cache(self) -> None:
        self._read_projection.invalidate_tab_id_cache()

    def note_tab_id(self, key: str, tab_id: str | None) -> None:
        self._read_projection.note_tab_id(key, tab_id)

    @overload
    def delete_session(self, key: str, *, skip_pinned: Literal[False] = ...) -> bool: ...

    @overload
    def delete_session(self, key: str, *, skip_pinned: Literal[True]) -> bool | None: ...

    def delete_session(self, key: str, *, skip_pinned: bool = False) -> bool | None:
        transcript = self._path(key)
        with self.delete_in_flight_window(key):
            if skip_pinned:
                deleted = self._metadata_projection.delete_session(key, skip_pinned=True)
            else:
                deleted = self._metadata_projection.delete_session(key, skip_pinned=False)
        if deleted:
            # A deleted session's restart-surviving vouch goes with it, so the
            # vouched-executions/ files track live sessions, not every one ever made.
            from kiro_crew._durable_vouch import forget_durable_vouch

            forget_durable_vouch(key)
            # And its persisted page index, so the sidecars track live transcripts.
            self._read_projection.forget_page_index(transcript)
        return deleted

    def delete_memory_consolidation_session(self, key: str, expected_store: str) -> bool:
        """Delete every artifact of one retired generated consolidation turn."""
        from kiro_crew.member_memory_auth import (
            read_private_session_store,
            require_memory_consolidation_session_key,
        )

        require_memory_consolidation_session_key(key, expected_store)
        binding = read_private_session_store(key)
        if binding is not None and binding != expected_store:
            raise ValueError("The transient session belongs to another memory store")
        path = self._path(key)
        existed = path.exists()
        deleted = self.delete_session(key)
        if existed and not deleted:
            raise OSError(f"Could not delete transient consolidation session {key!r}")

        removed = bool(deleted)
        archive_dir = _archive_dir(self._dir)
        stem = _safe_key(key) + ARCHIVE_SEGMENT_DELIMITER
        if archive_dir.exists():
            for archived in archive_dir.glob(f"{stem}*.jsonl"):
                archived.unlink()
                removed = True
        lock_path = self._lock_path(key)
        if lock_path.exists():
            lock_path.unlink()
            removed = True
        return removed

    def set_title(self, key: str, title: str) -> None:
        self._metadata_projection.set_title(key, title)

    def update_metadata(self, key: str, fields: dict) -> None:
        self._metadata_projection.update_metadata(key, fields)

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
        return self._metadata_projection.update_metadata_if(
            key,
            fields,
            guard,
            require_existing=require_existing,
            require_write=require_write,
            after_commit_under_lock=after_commit_under_lock,
        )

    def _update_metadata_locked(self, key: str, fields: dict) -> bool:
        return self._metadata_projection._update_metadata_locked(key, fields)

    def mtime_of(self, key: str) -> float | None:
        return self._metadata_projection.mtime_of(key)

    def clear_closed(
        self,
        key: str,
        *,
        only_if_closed_before: float | None = None,
        only_if_closed_id: str | None = None,
    ) -> None:
        self._metadata_projection.clear_closed(
            key, only_if_closed_before=only_if_closed_before, only_if_closed_id=only_if_closed_id
        )

    def _read_messages(self, key: str) -> list[dict]:
        return self._read_projection._read_messages(key)

    def _count_messages(self, key: str, *, hold_writer_lock: bool = True) -> int:
        return self._read_projection._count_messages(key, hold_writer_lock=hold_writer_lock)

    @contextlib.contextmanager
    def _cache_fill_lock(self, key: str) -> Iterator[bool]:
        with self._read_projection._cache_fill_lock(key) as locked:
            yield locked

    def _read_messages_locked(
        self,
        key: str,
        *,
        gen: int | None,
        flock_witness: tuple[int, int] | None,
    ) -> list[dict]:
        return self._read_projection._read_messages_locked(
            key, gen=gen, flock_witness=flock_witness
        )

    _TAIL_MIN_BYTES = 8_192
    _TAIL_AVG_MSG_BYTES = 512
    _TAIL_MAX_GROWTHS = 6

    def _recent_via_tail(
        self,
        key: str,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict] | None:
        return self._read_projection._recent_via_tail(key, max_messages, roles)

    @staticmethod
    def _recent_cache_key(key: str, max_messages: int, roles: AbstractSet[str] | None) -> str:
        return TranscriptReadProjection._recent_cache_key(key, max_messages, roles)

    def _read_tail_messages(
        self,
        path: Path,
        max_messages: int,
        roles: AbstractSet[str] | None,
    ) -> list[dict]:
        return self._read_projection._read_tail_messages(path, max_messages, roles)

    def _last_row_ts(self, key: str) -> str | None:
        return self._read_projection._last_row_ts(key)

    def last_row_ts(self, key: str) -> str | None:
        return self._read_projection.last_row_ts(key)

    @staticmethod
    def _cache_key_identities(key: str) -> tuple[str, ...]:
        """Every cache-key spelling that can refer to *key*'s session.

        One session is addressable by its logical key, its sanitized filename
        stem, and — for Slack threads — the pre-migration bare ``thread_ts``
        in either role. The closure must be BIDIRECTIONAL: a writer told only
        the bare legacy spelling (e.g. rotation deriving it from the file
        name) must still reach the canonical spelling readers use, or its
        invalidation is invisible to them. Pure string math, no I/O.
        """
        idents = dict.fromkeys((key, *transcript_stems(key)))
        canon = canonical_key(key)
        if canon != key:
            idents.update(dict.fromkeys((canon, *transcript_stems(canon))))
        return tuple(idents)

    def _cache_gen(self, key: str) -> int:
        return self._cache_coordinator._cache_gen(key)

    def _bump_cache_gen(self, key: str, idents: tuple[str, ...]) -> None:
        self._cache_coordinator._bump_cache_gen(key, idents)

    def _dir_write_epoch(self) -> int:
        return self._cache_coordinator._dir_write_epoch()

    def _publish_if_current(
        self,
        cache: _LRUCache[Any] | _SearchTextCache[Any],
        entry_key: str,
        value: Any,
        *,
        key: str,
        gen: int,
    ) -> None:
        self._cache_coordinator._publish_if_current(cache, entry_key, value, key=key, gen=gen)

    def _invalidate_cache(self, key: str) -> None:
        self._cache_coordinator._invalidate_cache(key)

    #: Bytes read from the end of a session file for the last-message preview.
    #: One tail block comfortably covers several trailing JSONL lines without
    #: paying a full-file parse on large sessions.
    _PREVIEW_TAIL_BYTES = 16_384
    #: Max characters returned in a last-message preview.
    _PREVIEW_MAX_CHARS = PREVIEW_MAX_CHARS

    def last_message_preview(self, key: str, sanitize=None) -> str:
        return self._read_projection.last_message_preview(key, sanitize=sanitize)

    def last_message_info(self, key: str, sanitize=None) -> tuple[str, float, bool]:
        return self._read_projection.last_message_info(key, sanitize=sanitize)

    def last_speech_info(self, key: str, sanitize=None) -> tuple[str, float, bool, bool]:
        return self._read_projection.last_speech_info(key, sanitize=sanitize)

    @staticmethod
    def _content_text(content: object) -> str:
        return TranscriptReadProjection._content_text(content)

    def get_metadata(self, key: str) -> dict:
        return self._read_projection.get_metadata(key)

    def get_metadata_status(self, key: str) -> tuple[dict, bool]:
        return self._read_projection.get_metadata_status(key)

    def metadata_line_state(self, key: str) -> str:
        return self._read_projection.metadata_line_state(key)

    def _pause_for_transient_retry(self) -> None:
        self._read_projection._pause_for_transient_retry()

    def _read_metadata(self, key: str) -> dict:
        return self._read_projection._read_metadata(key)

    def _read_metadata_status(self, key: str) -> tuple[dict, bool]:
        return self._read_projection._read_metadata_status(key)

    def _read_metadata_state(self, key: str) -> tuple[dict, str]:
        return self._read_projection._read_metadata_state(key)

    def sliding_window(self, key: str, keep_recent: int = 5) -> tuple[list[dict], list[dict]]:
        return self._read_projection.sliding_window(key, keep_recent)

    def rewrite_session(self, key: str, messages: list[dict]) -> None:
        self._rewrite_coordinator.rewrite_session(key, messages)

    def _rewrite_session_locked(self, key: str, messages: list[dict]) -> None:
        self._rewrite_coordinator._rewrite_session_locked(key, messages)

    def _maybe_rotate(self, path: Path, key: str) -> None:
        self._rewrite_coordinator._maybe_rotate(path, key)

    # -- Transcript locks and delete-in-flight marks (``history_locks``).
    _file_lock = _history_locks._file_lock
    _lock_path = _history_locks._lock_path
    _run_fd_cleanup_off_loop = staticmethod(_history_locks._run_fd_cleanup_off_loop)
    locked_stems = _history_locks.locked_stems
    _locked = _history_locks._locked
    _locked_stem = _history_locks._locked_stem
    atomic_appends = _history_locks.atomic_appends
    _schedule_flock_release = _history_locks._schedule_flock_release
    delete_in_flight_window = _history_locks.delete_in_flight_window
    delete_in_flight = _history_locks.delete_in_flight
    _delete_in_flight_marks = _history_locks._delete_in_flight_marks
    _flock_hold_witness = _history_locks._flock_hold_witness

    # -- The derivation seam (``history_derivation``).
    derivation_hold = _history_derivation.derivation_hold
    publication_hold = _history_derivation.publication_hold
    _withhold_if_restricted = _history_derivation._withhold_if_restricted
    derive_messages = _history_derivation.derive_messages
    derive_messages_chained = _history_derivation.derive_messages_chained
    derive_messages_chained_with_keys = _history_derivation.derive_messages_chained_with_keys
    derive_recent = _history_derivation.derive_recent

    # -- Reply-thread sidecar (``history_threads``).
    threads_sidecar_path = _history_threads.threads_sidecar_path
    read_threads = _history_threads.read_threads
    thread_transcript_identity = _history_threads.thread_transcript_identity
    append_thread_reply = _history_threads.append_thread_reply
    _transcript_holds_mid = _history_threads._transcript_holds_mid

    # -- Summary sidecar caches (``history_summaries``).
    _summary_cache_path = _history_summaries._summary_cache_path
    get_cached_summary = _history_summaries.get_cached_summary
    set_cached_summary = _history_summaries.set_cached_summary
    _intent_summary_cache_path = _history_summaries._intent_summary_cache_path
    get_cached_intent_summary = _history_summaries.get_cached_intent_summary
    read_intent_summary = _history_summaries.read_intent_summary
    set_cached_intent_summary = _history_summaries.set_cached_intent_summary

    # -- Appending rows (``history_append``).
    _persist_inline_attachments = _history_append._persist_inline_attachments
    append = _history_append.append
    append_if_absent = _history_append.append_if_absent
    _holds_identical_row = _history_append._holds_identical_row

    # -- Consolidation marker and retry ledger (``history_marker``).
    rotation_generation = _history_marker.rotation_generation
    snapshot_for_consolidation = _history_marker.snapshot_for_consolidation
    _sub_offset_for_generation = staticmethod(_history_marker._sub_offset_for_generation)
    mark_consolidated = _history_marker.mark_consolidated
    advance_consolidation_sub_offset = _history_marker.advance_consolidation_sub_offset
    unconsolidated_count = _history_marker.unconsolidated_count
    consolidation_counts = _history_marker.consolidation_counts
    consolidated_offset = _history_marker.consolidated_offset
    consolidation_retry_state = _history_marker.consolidation_retry_state
    _attempts_describe_current_span = _history_marker._attempts_describe_current_span
    _current_span_fields = _history_marker._current_span_fields
    record_consolidation_failure = _history_marker.record_consolidation_failure
    record_consolidation_environment_failure = (
        _history_marker.record_consolidation_environment_failure
    )


# This module is the facade of the conversation history family. Its names are
# bound here or, for ``_SERVED_ONLY``, served from the owner that rebinds them;
# a write through it (``monkeypatch.setattr``, ``mock.patch``) reaches every
# owner binding the same object (:mod:`kiro_crew._facade_mirror`). Owners never
# import this module at run time. New work goes to the owner of its job:
#
#   ``history_files``      -- stems and aliases, the session dirs, archive retention
#   ``history_schema``     -- metadata-key ownership, row provenance, timestamp order
#   ``history_offload``    -- the on-loop persistence discipline, off-loop writers
#   ``history_locks``      -- the transcript lock and the delete-in-flight marks
#   ``history_derivation`` -- the header's privacy contract and the derivation seam
#   ``history_threads``    -- the reply-thread sidecar
#   ``history_summaries``  -- the summary sidecar caches
#   ``history_append``     -- appending rows
#   ``history_marker``     -- consolidation progress and the retry ledger
#
# ``test/test_history_bindings.py`` pins the member inventory, binding and reach.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.history_files",
    "kiro_crew.history_schema",
    "kiro_crew.history_offload",
    "kiro_crew.history_locks",
    "kiro_crew.history_derivation",
    "kiro_crew.history_threads",
    "kiro_crew.history_summaries",
    "kiro_crew.history_append",
    "kiro_crew.history_marker",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Globals an owner rebinds at run time: the facade serves them and never binds them.
_SERVED_ONLY = frozenset({"_last_cleanup", "_on_loop_warn_last"})
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
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.history_files import _last_cleanup  # noqa: F401
    from kiro_crew.history_offload import _on_loop_warn_last  # noqa: F401
