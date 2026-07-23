"""Persistent conversation history — JSONL per session + LLM consolidation.

Session files: ~/.kirocrew/sessions/{safe_key}.jsonl
Each entry tracks provenance (source_thread, source_user) for citation.
Files auto-rotate at 512KB, keeping last 200 lines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time as _time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.llm_helpers import ToolApprovalPolicy, stream_and_collect_json
from kiro_crew.messaging.link import legacy_key
from kiro_crew.preview_text import strip_markdown_preview
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.skills import AutoSkillProvenance
from kiro_crew.vector_memory_constants import (
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory import MemoryStore
    from kiro_crew.session import SessionManager
    from kiro_crew.skills import SkillsLoader
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

SESSIONS_DIR_NAME = "sessions"
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_RETENTION_DAYS = 7
_CONSOLIDATION_THRESHOLD = 30  # preferences/projects update threshold (messages)

_SESSION_MAX_BYTES = 2 * 1024 * 1024  # 2MB
_SESSION_KEEP_LINES = 200
SEARCH_MIN_CHARS = 2  # shortest query string that triggers backend search
_TITLE_BOOST = 10  # field-boost multiplier for title matches in search_sessions
_SEARCH_SCAN_WINDOW = 500  # cap files scanned per search to bound I/O


def _safe_mtime(path: Path) -> float | None:
    """Return a file's mtime, or None if it can't be stat'd."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _restore_mtime(path: Path, prev_mtime: float | None) -> None:
    """Restore a session file's mtime after a *housekeeping* rewrite.

    ``list_sessions`` orders sessions by file mtime as a proxy for "last
    activity", and only a genuine message :meth:`ConversationLog.append`
    should advance that. Consolidation, rotation, and metadata updates
    (tab_id backfill on restore, title/agent/folder edits, last_consolidated
    bookkeeping) are background housekeeping — they rewrite the file but do
    NOT represent new conversation activity. Left unchecked they bump the
    mtime to "now", so every gateway restart (which consolidates + rehydrates
    open slots) floats long-closed sessions to the top of the session list and
    the "most recent session" a new dashboard/Slack session resolves to becomes
    a stale, unrelated thread. Restoring the pre-write mtime keeps ordering
    faithful to real activity. No-op when ``prev_mtime`` is None (fresh file).
    """
    if prev_mtime is None:
        return
    try:
        os.utime(path, (prev_mtime, prev_mtime))
    except OSError:
        pass


def _sessions_dir() -> Path:
    return config_dir() / SESSIONS_DIR_NAME


def _archive_dir(base: Path | None = None) -> Path:
    return (base or _sessions_dir()) / ARCHIVE_DIR_NAME


def _archive_lines(key: str, lines: list[str], reason: str, base: Path | None = None) -> Path | None:
    """Append dropped message lines to archive/{key}.{YYYYMMDD-HHMMSS}.jsonl. Returns path or None."""
    if not lines:
        return None
    import itertools

    adir = _archive_dir(base)
    adir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    safekey = _safe_key(key)
    header = json.dumps({"_type": "archive", "reason": reason, "archived_at": now.isoformat(), "count": len(lines)}) + "\n"
    payload = header + "".join(lines)
    # Atomic exclusive-create to avoid TOCTOU clobber when two archives land in the same second.
    # Use '__' delimiter so keys containing dots (e.g. Slack thread_ts) don't confuse rfind('.') parsing.
    for n in itertools.count():
        if n > 1000:
            raise RuntimeError(f"Failed to create archive file after {n} attempts")
        candidate = adir / f"{safekey}__{stamp}{f'-{n}' if n else ''}.jsonl"
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(payload)
            break
        except FileExistsError:
            continue
    logger.info("Archived %d lines from session %s to %s (reason=%s)", len(lines), key, candidate.name, reason)
    _cleanup_old_archives(base=base)
    return candidate


_last_cleanup: float = 0.0


def _resolve_retention_days() -> int:
    """Read session.archive_retention_days from config.

    Returns the configured retention window in days, or ``-1`` when cleanup is
    disabled.  Falls back to the hardcoded default if config can't be loaded
    (e.g. during early init or in a stripped test environment).
    """
    try:
        return int(KiroCrewConfig.load().session.archive_retention_days)
    except Exception:
        return ARCHIVE_RETENTION_DAYS


def _cleanup_old_archives(retention_days: int | None = None, base: Path | None = None) -> int:
    """Delete archive files older than retention_days. Rate-limited to once per hour.

    When *retention_days* is None, the value is resolved from config
    (``session.archive_retention_days``).  A negative value disables cleanup
    entirely — the user manages archive deletion manually.
    """
    global _last_cleanup
    import time as _time

    # Explicit negative disables cleanup immediately (no config read needed).
    if retention_days is not None and retention_days < 0:
        return 0  # cleanup disabled
    # Rate-limit guard runs BEFORE resolving retention from config so a
    # throttled call (the common case on hot archive paths) returns without
    # the expensive KiroCrewConfig.load() disk read + parse (Bug #6).
    now = _time.time()
    if now - _last_cleanup < 3600:
        return 0
    # Past the throttle window: stamp _last_cleanup NOW, before resolving
    # retention. Otherwise a config-resolved "disabled" (negative) would return
    # without updating the window, so every subsequent archive write would
    # re-run the expensive KiroCrewConfig.load() — reintroducing the Bug #6
    # regression for the disabled case.
    _last_cleanup = now
    # Resolve retention from config if not given, honoring a config-resolved
    # negative as the disable signal too.
    if retention_days is None:
        retention_days = _resolve_retention_days()
    if retention_days < 0:
        return 0  # cleanup disabled
    adir = _archive_dir(base)
    if not adir.exists():
        return 0
    cutoff = now - retention_days * 86400
    removed = 0
    for p in adir.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Cleaned %d expired archive files (>%dd)", removed, retention_days)
    return removed


def _safe_key(key: str) -> str:
    """Convert a session key (e.g. Slack thread_ts) to a safe filename."""
    return re.sub(r"[^\w\-.]", "_", key)


class ConversationLog:
    """Append-only JSONL conversation store with provenance and rotation."""

    def __init__(self, base_dir: Path | None = None):
        self._dir = base_dir or _sessions_dir()
        # mtime-based message cache: key → (mtime, messages)
        self._msg_cache: dict[str, tuple[float, list[dict]]] = {}
        # mtime-based metadata cache: key → (mtime, metadata)
        self._meta_cache: dict[str, tuple[float, dict]] = {}

    def init(self) -> None:
        """Create sessions directory if missing."""
        self._dir.mkdir(parents=True, exist_ok=True)

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

    def append(
        self,
        key: str,
        role: str,
        content: str,
        tools: list[str] | None = None,
        source_thread: str | None = None,
        source_user: str | None = None,
        agent: str | None = None,
        tab_id: str | None = None,
    ) -> None:
        """Append a message with optional provenance to the session log.

        If the session file does not yet exist, it will be created with an
        initial metadata line.  When *agent* is supplied, the agent name is
        recorded in that metadata so the session can be resumed under the
        correct agent later.  (Has no effect if the file already exists;
        use :meth:`update_metadata` to change the agent after creation.)
        """
        path = self._path(key)
        if not path.exists():
            self._dir.mkdir(parents=True, exist_ok=True)
            meta: dict = {
                "_type": "metadata",
                "created_at": datetime.now().isoformat(),
                "last_consolidated": 0,
            }
            if agent:
                meta["agent"] = agent
            if tab_id:
                meta["tab_id"] = tab_id
            path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        msg: dict = {
            "role": role,
            "content": content,
            "ts": datetime.now().isoformat(),
        }
        if tools:
            msg["tools"] = tools
        if source_thread:
            msg["source_thread"] = source_thread
        if source_user:
            msg["source_user"] = source_user

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg) + "\n")

        # Invalidate cache since file changed
        self._invalidate_cache(key)

        # Rotate if file exceeds size limit
        self._maybe_rotate(path)

    def recent(
        self,
        key: str,
        max_messages: int = 20,
        roles: set[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Return last *max_messages* entries as ``[{role, content}]``.

        When *roles* is provided, only messages with matching roles are
        counted toward the limit.  This filters out low-signal entries
        (e.g. tool display titles) so the budget is spent on user and
        assistant content.

        When *exclude_last_n* > 0, drops that many trailing raw entries
        BEFORE role filtering. Used by the dashboard to avoid re-injecting
        the just-flushed current-turn user message as history when the
        background flush wins the race against kiro-cli spawn.
        """
        messages = self._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        if roles:
            messages = [m for m in messages if m["role"] in roles]
        return [{"role": m["role"], "content": m["content"]} for m in messages[-max_messages:]]

    def recent_chained(
        self,
        key: str,
        max_messages: int = 20,
        roles: set[str] | None = None,
        *,
        exclude_last_n: int = 0,
    ) -> list[dict]:
        """Like recent() but reads across all chained session files (same tab_id).

        For long-lived sessions that span multiple session files (linked by
        tab_id), this reads the full chain. Falls back to single-file read
        for legacy sessions without a tab_id.

        See :meth:`recent` for *exclude_last_n* semantics.
        """
        messages = self.read_messages_chained(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        if roles:
            messages = [m for m in messages if m["role"] in roles]
        return [{"role": m["role"], "content": m["content"]} for m in messages[-max_messages:]]

    def recent_with_provenance(
        self, key: str, max_messages: int = 3, *, exclude_last_n: int = 0
    ) -> list[dict]:
        """Return recent entries with source_thread provenance for cross-session citation.

        See :meth:`recent` for *exclude_last_n* semantics.
        """
        messages = self._read_messages(key)
        if exclude_last_n > 0:
            messages = messages[:-exclude_last_n]
        with_source = [m for m in messages if m.get("source_thread")]
        result: list[dict] = []
        for m in with_source[-max_messages:]:
            snippet = m["content"][:150] + "…" if len(m["content"]) > 150 else m["content"]
            result.append(
                {
                    "source_thread": m["source_thread"],
                    "ts": m.get("ts", "?"),
                    "snippet": snippet,
                }
            )
        return result

    def get_unconsolidated(self, key: str) -> tuple[list[dict], int]:
        """Return (messages_after_last_consolidated, total_message_count)."""
        messages = self._read_messages(key)
        offset = self._read_metadata(key).get("last_consolidated", 0)
        return messages[offset:], len(messages)

    def mark_consolidated(self, key: str, offset: int) -> None:
        """Rewrite metadata line with updated last_consolidated offset."""
        path = self._path(key)
        if not path.exists():
            return
        prev_mtime = _safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not lines:
            return
        meta = json.loads(lines[0])
        meta["last_consolidated"] = offset
        meta["updated_at"] = datetime.now().isoformat()
        lines[0] = json.dumps(meta) + "\n"
        path.write_text("".join(lines), encoding="utf-8")
        # Housekeeping bookkeeping — must not advance the session's mtime
        # (see _restore_mtime). Otherwise consolidation floats stale sessions
        # to the top of list_sessions on every gateway restart.
        _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def unconsolidated_count(self, key: str) -> int:
        """Count messages not yet processed by the consolidator."""
        messages = self._read_messages(key)
        offset = self._read_metadata(key).get("last_consolidated", 0)
        return max(0, len(messages) - offset)

    def load_transcript(self, key: str) -> str:
        """Load full session as formatted text for LLM summarization."""
        messages = self._read_messages(key)
        if not messages:
            return ""
        lines: list[str] = []
        for m in messages:
            role = m["role"].title()
            lines.append(f"{role}: {m['content']}")
        return "\n\n".join(lines)

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

    def list_sessions(self) -> list[dict]:
        """Return metadata for all session files, newest first.

        Deduplicates stacked ``dashboard_`` prefix files, keeping the
        most recently modified version.  Uses mtime-based metadata cache
        when available, falling back to reading only the first line for
        title extraction.
        """
        sessions: list[dict] = []
        if not self._dir.exists():
            return sessions
        # Deduplicate stacked dashboard_ prefixes by canonical key, keeping newer
        by_canon: dict[str, dict] = {}
        for path in self._dir.glob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            # Skip symlinks — these are handoff aliases pointing to the real session
            if path.is_symlink():
                continue
            key = path.stem
            meta: dict = {
                "key": key,
                "messages": max(1, int(stat.st_size / 200)),
                "modified": stat.st_mtime,
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            # Try metadata cache first (populated by _read_metadata calls)
            cached_meta = self._meta_cache.get(key)
            if cached_meta and cached_meta[0] == stat.st_mtime:
                d = cached_meta[1]
                if d.get("created_at"):
                    meta["created"] = d["created_at"]
                if d.get("title"):
                    meta["title"] = d["title"]
                if d.get("agent"):
                    meta["agent"] = d["agent"]
                meta["memory_mode"] = d.get("memory_mode", "persistent")
                if d.get("folder_id"):
                    meta["folder_id"] = d["folder_id"]
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
                            self._meta_cache[key] = (stat.st_mtime, d)
                except Exception:
                    pass
            # Ensure memory_mode is always present (old sessions lack it)
            meta.setdefault("memory_mode", "persistent")
            # Extract first user message as title fallback
            if "title" not in meta:
                msg_cached = self._msg_cache.get(key)
                if msg_cached and msg_cached[0] == stat.st_mtime:
                    for m in msg_cached[1]:
                        if m.get("role") == "user" and m.get("content"):
                            meta["title"] = m["content"][:80]
                            break
                else:
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
                                    meta["title"] = d["content"][:80]
                                    break
                    except Exception:
                        pass
            if "title" not in meta:
                meta["title"] = key
            # Deduplicate: keep newer entry per canonical key
            canon = self._canonical_key(key)
            existing = by_canon.get(canon)
            if existing is None or stat.st_mtime >= existing["modified"]:
                by_canon[canon] = meta
        sessions = list(by_canon.values())
        sessions.sort(key=lambda s: s.get("modified", 0), reverse=True)
        return sessions

    def agent_usage(self) -> dict[str, tuple[int, float]]:
        """Return ``{agent_name: (session_count, last_used_mtime)}`` per agent.

        Built on top of :meth:`list_sessions` (not a fresh directory glob) so it
        inherits that method's canonical-session dedup and symlink-skip — counts
        are therefore per logical conversation, not per raw ``.jsonl`` file.
        Sessions whose metadata never recorded an agent are ignored.
        """
        usage: dict[str, tuple[int, float]] = {}
        for meta in self.list_sessions():
            agent = meta.get("agent")
            if not agent:
                continue
            count, last_used = usage.get(agent, (0, 0.0))
            usage[agent] = (count + 1, max(last_used, meta.get("modified", 0.0)))
        return usage

    def search_sessions(self, query: str, limit: int = 50) -> list[dict]:
        """Return session metadata for files whose message content matches *query*.

        Case-insensitive substring match over each message's ``content``
        field using full Unicode case folding via :meth:`str.casefold`
        (so e.g. German ``ß`` folds to ``ss``).  Matching only on parsed
        ``content`` avoids false positives from JSON structural elements
        (e.g. the word ``"user"`` matching every ``"role": "user"`` line).

        Ranking (higher is better)::

            score = (title_matches * _TITLE_BOOST)
                  + (content_matches / sqrt(1 + doc_chars / 1024))

        Title matches get a strong field boost - titles are short and
        intentional, so a hit there is strong evidence.  Content matches
        are normalized by a sqrt length factor so a long session with a
        casual mention doesn't outrank a short, focused one.  (Simpler
        than BM25's ``(1-b) + b*(dl/avgdl)`` because we avoid the
        two-pass scan needed for corpus stats.)  Sessions with zero
        matches are dropped.  Ties break by recency (existing
        ``list_sessions`` order - newest first).  Caps results at *limit*.
        Only the ``_SEARCH_SCAN_WINDOW`` most recent files are scored, so
        I/O stays bounded even with hundreds of sessions.
        """
        if not query or limit <= 0 or not self._dir.exists():
            return []
        needle = query.casefold()
        scored: list[tuple[float, int, dict]] = []  # (score, -rank, meta)
        for rank, meta in enumerate(self.list_sessions()[:_SEARCH_SCAN_WINDOW]):
            content_hits = 0
            doc_chars = 0
            texts: list[str] = []
            # Pull parsed messages from the mtime-keyed cache (_read_messages)
            # rather than re-opening + re-parsing the file here. The snippet
            # path (search_chat_history) already reads each matched key via the
            # same cache, so this collapses the prior two-parses-per-query into
            # one. Content semantics are unchanged: only string ``content``
            # fields are counted, in file order, so the \x00-join hit count and
            # the doc_chars length normalizer stay identical to the previous
            # inline scan. _read_messages is OSError-safe and returns [] for a
            # missing/unreadable file, so it also subsumes the old try/except.
            for obj in self._read_messages(meta["key"]):
                raw = obj.get("content") if isinstance(obj, dict) else None
                text = raw if isinstance(raw, str) else ""
                if text:
                    doc_chars += len(text)
                    texts.append(text)
            # Casefold + count once per file instead of per line: a 200-line
            # session produces one temporary casefolded string instead of 200,
            # bounding GC pressure under rapid-fire search keystrokes.  The
            # ``\x00`` separator can't appear in user queries, so cross-line
            # false matches are impossible.
            if texts:
                content_hits = "\x00".join(texts).casefold().count(needle)
            title_hits = (meta.get("title") or "").casefold().count(needle)
            if not title_hits and not content_hits:
                continue
            length_norm = math.sqrt(1 + doc_chars / 1024)
            score = title_hits * _TITLE_BOOST + content_hits / length_norm
            # Match-centered content snippet (why the session surfaced when the
            # title doesn't contain the query). Best-effort, display-only.
            snippet = ""
            if content_hits and texts:
                joined = " ".join(texts)
                # casefold() to match the hit detection above — .lower() misses
                # matches casefold finds (ß→ss, İ), yielding an empty snippet
                # despite content_hits > 0. casefold can shift offsets slightly
                # (length changes); acceptable for a display-only best-effort
                # window.
                pos = joined.casefold().find(query.casefold())
                if pos >= 0:
                    start = max(0, pos - 40)
                    end = min(len(joined), pos + len(query) + 100)
                    frag = " ".join(joined[start:end].split())
                    snippet = (
                        ("…" if start > 0 else "") + frag + ("…" if end < len(joined) else "")
                    )[:200]
            out_meta = {**meta, "snippet": snippet} if snippet else meta
            # Negate rank so a smaller (newer) rank wins ties after score desc sort.
            scored.append((score, -rank, out_meta))
        scored.sort(reverse=True)
        return [meta for _, _, meta in scored[:limit]]

    def recent_from_source(
        self, source_prefix: str, exclude_key: str = "", max_messages: int = 20
    ) -> list[dict]:
        """Return recent messages from sessions matching *source_prefix*.

        Optimized: only scans the 5 most recently modified files and reads
        only the last 50 lines from each, avoiding full-file I/O on large
        session histories.
        """
        if not self._dir.exists():
            return []
        safe_exclude = _safe_key(exclude_key) if exclude_key else ""
        safe_prefix = _safe_key(source_prefix)
        # Collect matching paths and sort by mtime (newest first)
        paths: list[Path] = []
        for path in self._dir.glob(f"{safe_prefix}*.jsonl"):
            if safe_exclude and path.stem == safe_exclude:
                continue
            paths.append(path)
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        candidates: list[dict] = []
        included = 0
        _max_scan = 50  # bound I/O even with many ephemeral sessions
        for path in paths[:_max_scan]:
            if included >= 5:
                break
            # Single-pass read: check metadata head, then read remainder via same handle
            is_restricted = False
            try:
                with open(path, encoding="utf-8") as f:
                    head_lines = []
                    for _, line in zip(range(5), f):
                        head_lines.append(line)
                        try:
                            d = json.loads(line.strip())
                            if d.get("_type") == "metadata" and d.get("memory_mode") in ("incognito", "temporary"):
                                is_restricted = True
                                break
                        except (json.JSONDecodeError, ValueError):
                            pass
                    if is_restricted:
                        continue
                    raw = "".join(head_lines) + f.read()
            except OSError:
                continue
            included += 1
            lines = raw.splitlines()
            for line in lines[-50:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_type") == "metadata":
                    continue
                candidates.append(data)
        # Sort by timestamp and return most recent
        candidates.sort(key=lambda m: m.get("ts", ""))
        return [{"role": m["role"], "content": m["content"]} for m in candidates[-max_messages:]]

    def read_messages(self, key: str) -> list[dict]:
        """Public access to session messages."""
        return self._read_messages(key)

    def read_messages_chained(self, key: str) -> list[dict]:
        """Read messages from all session files sharing the same ``tab_id``.

        Returns messages from the current file only if no ``tab_id`` is set
        (legacy sessions).  Otherwise finds all sibling files with the same
        ``tab_id``, sorts chronologically, and concatenates their messages.

        Uses a ``_tab_id_index`` cache (built lazily, invalidated on save)
        to avoid scanning every file on each call.
        """
        meta = self.get_metadata(key)
        tid = meta.get("tab_id")
        if not tid:
            return self._read_messages(key)
        if not hasattr(self, "_tab_id_index"):
            self._tab_id_index: dict[str, list[str]] = {}
        if tid not in self._tab_id_index:
            self._rebuild_tab_id_index()
            if tid not in self._tab_id_index:
                self._tab_id_index[tid] = []  # sentinel: prevent repeated rebuilds
        keys = self._tab_id_index.get(tid, [])
        if not keys:
            return self._read_messages(key)
        all_msgs: list[dict] = []
        for k in keys:
            all_msgs.extend(self._read_messages(k))
        return all_msgs or self._read_messages(key)

    def _rebuild_tab_id_index(self) -> None:
        """Scan all dashboard session files and build tab_id → [keys] mapping."""
        index: dict[str, list[str]] = {}
        for path in sorted(self._dir.glob("dashboard_chat-*.jsonl")):
            try:
                with path.open(encoding="utf-8") as f:
                    first_line = f.readline()
                m = json.loads(first_line)
                tid = m.get("tab_id")
                if tid:
                    index.setdefault(tid, []).append(path.stem.replace("_", ":", 1))
            except Exception:
                continue
        self._tab_id_index = index

    def invalidate_tab_id_cache(self) -> None:
        """Clear the tab_id index so it's rebuilt on next chained read."""
        if hasattr(self, "_tab_id_index"):
            self._tab_id_index.clear()

    def delete_session(self, key: str) -> bool:
        """Delete a session file. Returns True if deleted."""
        path = self._path(key)
        if path.exists():
            path.unlink()
            self._invalidate_cache(key)
            self.invalidate_tab_id_cache()
            return True
        return False

    def set_title(self, key: str, title: str) -> None:
        """Persist a title into the session's metadata line."""
        self.update_metadata(key, {"title": title})

    def update_metadata(self, key: str, fields: dict) -> None:
        """Merge *fields* into the session's metadata line and persist.

        Upsert semantics: if the session file does not exist yet (e.g. ``!ta
        <agent> --clean`` is issued before the first message is logged), the
        file is created with a fresh metadata line carrying *fields*.  Without
        this, the selection would live only in the in-memory caches and be lost
        on restart -- the session would silently resume under the default agent
        with the default toolset.
        """
        path = self._path(key)
        # A metadata-only edit (title/agent/folder/tab_id/pin) is not new
        # conversation activity — preserve the pre-write mtime so it doesn't
        # reorder list_sessions. None when the file is absent (upsert): a
        # genuinely new session should get a natural mtime. See _restore_mtime.
        prev_mtime = _safe_mtime(path)
        lines = (
            path.read_text(encoding="utf-8").splitlines(keepends=True)
            if path.exists()
            else []
        )
        # Parse the existing metadata line, or synthesize a fresh one when the
        # file is absent/empty (the upsert case). A first line that exists but
        # isn't valid metadata is left untouched -- we never clobber it.
        if lines:
            try:
                meta = json.loads(lines[0])
            except json.JSONDecodeError:
                return
            if meta.get("_type") != "metadata":
                return
        else:
            self._dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "_type": "metadata",
                "created_at": datetime.now().isoformat(),
                "last_consolidated": 0,
            }
            lines = [""]  # placeholder; replaced below

        meta.update(fields)
        lines[0] = json.dumps(meta) + "\n"
        import os as _os
        import tempfile as _tf

        data = "".join(lines).encode("utf-8")
        fd, tmp = _tf.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            try:
                _os.write(fd, data)
                _os.fsync(fd)
            finally:
                _os.close(fd)
            _os.replace(tmp, str(path))
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise
        _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def _read_messages(self, key: str) -> list[dict]:
        """Read all non-metadata entries from a session JSONL file.

        Uses mtime-based caching to avoid re-parsing unchanged files.
        """
        path = self._path(key)
        if not path.exists():
            self._msg_cache.pop(key, None)
            return []
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []
        cached = self._msg_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        messages: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("_type") == "metadata":
                continue
            messages.append(data)
        self._msg_cache[key] = (mtime, messages)
        return messages

    def _invalidate_cache(self, key: str) -> None:
        """Invalidate caches for a key after a write operation."""
        self._msg_cache.pop(key, None)
        self._meta_cache.pop(key, None)

    #: Bytes read from the end of a session file for the last-message preview.
    #: One tail block comfortably covers several trailing JSONL lines without
    #: paying a full-file parse on large sessions.
    _PREVIEW_TAIL_BYTES = 16_384
    #: Max characters returned in a last-message preview.
    _PREVIEW_MAX_CHARS = 120

    def last_message_preview(self, key: str) -> str:
        """Return a short preview of the session's last message ('' if none).

        Reads only the tail of the JSONL file (bounded), scanning backwards
        for the newest parseable message line — cheap even on large sessions.
        Handles both plain-string ``content`` and structured list-form content
        blocks (text extracted from ``{"type": "text"}`` / ``text`` fields).
        If the initial tail window yields nothing parseable (a single trailing
        line larger than the window), retries once with a 16× window before
        giving up.
        """
        path = self._path(key)
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        for window in (self._PREVIEW_TAIL_BYTES, self._PREVIEW_TAIL_BYTES * 16):
            try:
                with open(path, "rb") as f:
                    if size > window:
                        f.seek(size - window)
                        f.readline()  # discard the (likely partial) first line
                    tail = f.read().decode("utf-8", errors="replace")
            except OSError:
                return ""
            for line in reversed(tail.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("_type") == "metadata":
                    continue
                text = self._content_text(data.get("content"))
                if not text:
                    continue
                preview = strip_markdown_preview(text)
                if not preview:
                    continue
                if len(preview) > self._PREVIEW_MAX_CHARS:
                    preview = preview[: self._PREVIEW_MAX_CHARS].rstrip() + "…"
                return preview
            if size <= window:
                break  # the window already covered the whole file — no retry
        return ""

    @staticmethod
    def _content_text(content: object) -> str:
        """Best-effort plain text from a message ``content`` field.

        Plain strings pass through; list-form content blocks (the structured
        shape newer turns use) contribute their ``text`` fields in order.
        Anything else yields ''.
        """
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
            return " ".join(p.strip() for p in parts if p.strip())
        return ""

    def get_metadata(self, key: str) -> dict:
        """Return session metadata for *key*."""
        return self._read_metadata(key)

    def _read_metadata(self, key: str) -> dict:
        """Read the metadata line (first line) from a session JSONL file.

        Uses mtime-based caching to avoid re-reading unchanged files.
        """
        path = self._path(key)
        if not path.exists():
            self._meta_cache.pop(key, None)
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._meta_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        first = path.read_text(encoding="utf-8").split("\n", 1)[0].strip()
        if not first:
            return {}
        try:
            data = json.loads(first)
            meta = data if data.get("_type") == "metadata" else {}
        except json.JSONDecodeError:
            meta = {}
        self._meta_cache[key] = (mtime, meta)
        return meta

    def sliding_window(self, key: str, keep_recent: int = 5) -> tuple[list[dict], list[dict]]:
        """Split messages into (older, recent) for compaction.

        *keep_recent* is the number of recent user/assistant pairs to retain.
        Returns ``(older_messages, recent_messages)``.
        """
        messages = self._read_messages(key)
        # keep_recent pairs = keep_recent * 2 individual messages
        split = max(0, len(messages) - keep_recent * 2)
        return messages[:split], messages[split:]

    def rewrite_session(self, key: str, messages: list[dict]) -> None:
        """Rewrite session JSONL with only the given messages."""
        path = self._path(key)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Compaction is housekeeping, not new activity — preserve the pre-write
        # mtime so it doesn't reorder list_sessions (see _restore_mtime).
        prev_mtime = _safe_mtime(path)
        # Archive only messages being dropped (old content minus what's being kept).
        # Compare by normalized JSON (sort_keys) to be resilient to key ordering changes.
        if path.exists():
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if old_lines and '"_type"' in old_lines[0]:
                old_lines = old_lines[1:]
            kept_serialized = {json.dumps(m, sort_keys=True) for m in messages}
            dropped = []
            for ln in old_lines:
                if not ln.strip():
                    continue
                try:
                    normalized = json.dumps(json.loads(ln), sort_keys=True)
                except (json.JSONDecodeError, ValueError):
                    dropped.append(ln)  # corrupted line → archive it
                    continue
                if normalized not in kept_serialized:
                    dropped.append(ln)
            try:
                _archive_lines(key, dropped, reason="compact", base=self._dir)
            except Exception:
                logger.warning("Failed to archive dropped lines for %s", key, exc_info=True)
        # Preserve select fields from original metadata
        orig_meta = self.get_metadata(key) or {}
        meta = {
            "_type": "metadata",
            "created_at": orig_meta.get("created_at", datetime.now().isoformat()),
            "last_consolidated": orig_meta.get("last_consolidated", 0),
            "compacted_at": datetime.now().isoformat(),
        }
        if orig_meta.get("memory_mode"):
            meta["memory_mode"] = orig_meta["memory_mode"]
        lines = [json.dumps(meta) + "\n"]
        for m in messages:
            lines.append(json.dumps(m) + "\n")
        atomic_write(path, "".join(lines))
        _restore_mtime(path, prev_mtime)
        self._invalidate_cache(key)

    def _maybe_rotate(self, path: Path) -> None:
        """Rotate session file if it exceeds size limit."""
        try:
            if path.stat().st_size <= _SESSION_MAX_BYTES:
                return
        except OSError:
            return
        # Rotation is triggered right after a genuine append; preserve that
        # append's mtime rather than re-stamping to "now" (see _restore_mtime).
        prev_mtime = _safe_mtime(path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= _SESSION_KEEP_LINES:
            return
        # Keep metadata line + last N message lines
        meta_line = lines[0] if lines and '"_type"' in lines[0] else ""
        kept = lines[-_SESSION_KEEP_LINES:]
        dropped_start = 1 if meta_line else 0
        # Edge case: if len(lines) <= _SESSION_KEEP_LINES + dropped_start, the slice
        # is empty and _archive_lines returns None (noop). The guard above already
        # returns when len(lines) <= _SESSION_KEEP_LINES, so this only fires when
        # there are genuinely more lines than we keep.
        try:
            _archive_lines(path.stem, lines[dropped_start:-_SESSION_KEEP_LINES], reason="rotate", base=self._dir)
        except Exception:
            logger.warning("Failed to archive rotated lines for %s", path.stem, exc_info=True)

        # Reset last_consolidated since offsets are now invalid
        if meta_line:
            try:
                meta = json.loads(meta_line)
                meta["last_consolidated"] = 0
                meta["rotated_at"] = datetime.now().isoformat()
                meta_line = json.dumps(meta) + "\n"
            except json.JSONDecodeError:
                pass

        content = meta_line + "".join(kept)
        atomic_write(path, content)
        _restore_mtime(path, prev_mtime)
        # Invalidate cache — offsets changed
        safe = path.stem
        self._invalidate_cache(safe)
        logger.info("Rotated session file %s (%d → %d lines)", path.name, len(lines), len(kept))


# ── Module-level helpers for auto skill eligibility ──
#
# Kept at module level so they're trivially unit-testable without
# instantiating HistoryConsolidator.

# Canonical tool titles that indicate a read targeting a sensitive path.
# Supplements is_sensitive_path() and is_sensitive_bash_command() which
# handle the actual runtime blocking — this is a second-layer defense
# that refuses to extract a skill if the session tried to access a
# sensitive path, even when the attempt was denied at hook time.
_SENSITIVE_TOOL_PATTERNS: tuple[str, ...] = (
    ".aws/",
    ".ssh/",
    ".gnupg/",
    ".gpg/",
    ".docker/config",
    ".kube/config",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".kirocrew/.env",
    "169.254.169.254",  # IMDS
)


_TOOL_ROLES: frozenset[str] = frozenset({"tool", "tool_call", "tool_result"})


def _count_tool_call_messages(messages: list[dict]) -> int:
    """Count messages that represent tool invocations under either schema.

    Two recording formats exist:
    - Legacy (Slack pipeline): assistant messages carry a ``tools`` list field.
    - Dashboard pipeline: separate messages with ``role`` in {"tool", "tool_call",
      "tool_result"} and the tool name embedded in ``content``.

    A message matching EITHER condition counts once (no double-counting).
    """
    count = 0
    for msg in messages:
        tools = msg.get("tools")
        if isinstance(tools, list) and tools:
            count += 1
        elif msg.get("role") in _TOOL_ROLES:
            count += 1
    return count


def _session_touched_sensitive(messages: list[dict]) -> bool:
    """Return True if any tool call in the session referenced a sensitive path.

    Checks both recording schemas:
    - Legacy: substring match over each entry in ``msg["tools"]`` list.
    - Dashboard: substring match over ``content`` when ``role`` indicates a tool event.

    Designed to be conservative — a false positive just means we skip
    auto-creation for this session.
    """
    for msg in messages:
        # Legacy schema: tools list on assistant messages
        tools = msg.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, str):
                    continue
                lower = tool.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
        # Dashboard schema: role="tool" with tool info in content
        if msg.get("role") in _TOOL_ROLES:
            content = msg.get("content", "")
            if isinstance(content, str):
                lower = content.lower()
                for pattern in _SENSITIVE_TOOL_PATTERNS:
                    if pattern in lower:
                        return True
    return False


class HistoryConsolidator:
    """Summarize old messages into structured memory via LLM.

    Two consolidation paths:
    - Preferences/projects: triggered by message count (30 messages)
    - Daily history: triggered by idle time (3h default) or end of day
    """

    def __init__(
        self,
        log: ConversationLog,
        memory: MemoryStore,
        sessions: SessionManager | None = None,
        lesson_store: LessonStore | None = None,
        history_idle_secs: float = 3 * 3600,
        vector_store: "VectorMemoryStore | None" = None,
        migrated: bool = False,
        # ── Auto skill creation ──
        # All-default so callers unaware of this feature continue to work.
        skills_loader: "SkillsLoader | None" = None,
        auto_skills_enabled: bool = False,
        auto_refine_enabled: bool = False,
        auto_min_tool_calls: int = 5,
        auto_similarity_threshold: float = 0.85,
    ) -> None:
        self._log = log
        self._memory = memory
        self._sessions = sessions
        self._lesson_store = lesson_store
        self._history_idle_secs = history_idle_secs
        self._vector_store = vector_store
        self._migrated = migrated
        self._skills_loader = skills_loader
        self._auto_skills_enabled = auto_skills_enabled
        self._auto_refine_enabled = auto_refine_enabled
        self._auto_min_tool_calls = auto_min_tool_calls
        self._auto_similarity_threshold = auto_similarity_threshold
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Track last activity per session for idle-based history consolidation
        self._last_activity: dict[str, float] = {}
        self._history_consolidated: dict[str, float] = {}  # key → last history consolidation time
        # Separate offset for prefs-only consolidation (doesn't advance main offset)
        self._prefs_offset: dict[str, int] = {}

    def maybe_consolidate(self, key: str) -> None:
        """Fire preferences/projects consolidation if message threshold exceeded."""
        self._last_activity[key] = _time.time()
        if key in self._running:
            return
        total = len(self._log._read_messages(key))
        prefs_off = self._prefs_offset.get(key, 0)
        if total - prefs_off < _CONSOLIDATION_THRESHOLD:
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=False))
        self._tasks.add(t)

        def _on_done(fut: asyncio.Task, k: str = key, off: int = total) -> None:  # type: ignore[type-arg]
            self._tasks.discard(fut)
            if not fut.cancelled() and fut.exception() is None:
                self._prefs_offset[k] = off

        t.add_done_callback(_on_done)

    def check_idle_sessions(self) -> None:
        """Check all tracked sessions for idle-based history consolidation."""
        now = _time.time()
        for key, last in list(self._last_activity.items()):
            if (
                now - last < self._history_idle_secs
                or self._log.unconsolidated_count(key) < 1
                or now - self._history_consolidated.get(key, 0) < self._history_idle_secs
                or key in self._running
            ):
                continue
            self._running.add(key)
            captured_now = now
            t = asyncio.create_task(self._consolidate(key, include_history=True))
            self._tasks.add(t)

            def _on_idle_done(
                fut: asyncio.Task,  # type: ignore[type-arg]
                k: str = key,
                ts: float = captured_now,
            ) -> None:
                self._tasks.discard(fut)
                if not fut.cancelled() and fut.exception() is None:
                    self._history_consolidated[k] = ts

            t.add_done_callback(_on_idle_done)

    def consolidate_session(self, key: str) -> None:
        """Trigger history consolidation for *key* (fire-and-forget).

        Used by session-end hooks (dashboard close, Slack end, idle expiry)
        and the ``kirocrew consolidate`` CLI command.  Skips if the session
        is already being consolidated or has no unconsolidated messages.

        Safety: _consolidate() internally enforces _session_touched_sensitive()
        as part of the auto_skills_eligible gate — sensitive sessions never
        produce skills regardless of entry point.
        """
        if key in self._running:
            return
        if self._log.unconsolidated_count(key) < 1:
            return
        # Short-circuit sensitive sessions before scheduling a task
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            logger.info("consolidate_session skipped for %s: sensitive session", key)
            return
        self._running.add(key)
        t = asyncio.create_task(self._consolidate(key, include_history=True))
        self._tasks.add(t)

        def _on_done(
            fut: asyncio.Task,  # type: ignore[type-arg]
            k: str = key,
        ) -> None:
            self._tasks.discard(fut)
            self._running.discard(k)
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is None:
                self._history_consolidated[k] = _time.time()
            else:
                logger.warning("consolidate_session failed for %s: %s", k, exc)

        t.add_done_callback(_on_done)

    async def consolidate_now(self, key: str) -> None:
        """Consolidate a session synchronously (blocking).

        Unlike consolidate_session() which is fire-and-forget, this awaits
        completion. Used by the CLI command.

        Safety: defense-in-depth — also checked inside _consolidate().
        """
        if self._log.unconsolidated_count(key) < 1:
            return
        messages = self._log._read_messages(key)
        if _session_touched_sensitive(messages):
            logger.info("consolidate_now skipped for %s: sensitive session", key)
            return
        await self._consolidate(key, include_history=True)

    async def _consolidate(self, key: str, include_history: bool = True) -> None:
        """Run LLM consolidation for a session."""
        try:
            unconsolidated, total = self._log.get_unconsolidated(key)
            if not unconsolidated:
                return

            # Resolve workspace-scoped memory from session metadata
            meta = self._log.get_metadata(key)
            ws_name = meta.get("workspace")
            if ws_name:
                from kiro_crew.context import ContextBuilder

                memory = ContextBuilder.get_memory_for(ws_name)
            else:
                memory = self._memory

            def _fmt(m: dict) -> str:
                tools = f" [tools: {', '.join(m['tools'])}]" if m.get("tools") else ""
                return f"[{m.get('ts', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}"

            conversation = "\n".join(_fmt(m) for m in unconsolidated)

            current_prefs = memory.read_preferences()
            current_projects = memory.read_projects()

            # Build prompt keys dynamically based on consolidation type
            keys: list[str] = []
            if include_history:
                keys.append(
                    '"history_entry": A concise paragraph (2-5 sentences) summarizing '
                    "what happened. Use local time [YYYY-MM-DD HH:MM]. Focus on "
                    "decisions, outcomes, facts. Use user's real name if known."
                )

            # Structured memory extraction (when vector store is available)
            has_vector = self._vector_store is not None
            if has_vector and self._vector_store is not None:
                current_semantic = self._vector_store.get_all_semantic()
                semantic_json = (
                    json.dumps(
                        [
                            {k: e[k] for k in ("key", "value_json", "confidence")}
                            for e in current_semantic
                        ],
                        indent=1,
                    )
                    if current_semantic
                    else "[]"
                )
                keys.append(
                    '"semantic": Array of structured facts to remember long-term. '
                    'Each: {"key": "<dotted.key>", "value": <json_value>, "confidence": 0.0-1.0, '
                    '"delete": false}. '
                    "Rules: keys must start with pref.*, project.*, or user.* "
                    "(e.g. pref.color, user.favorite_language, project.name). "
                    "confidence 1.0 = user stated, 0.8-0.9 = clearly implied, <0.8 = uncertain (rejected). "
                    "value must be a JSON primitive (string, number, boolean) — NOT objects or arrays. "
                    "IMPORTANT: Check existing semantic memory above. If a key already covers "
                    "the same topic, UPDATE that key instead of creating a new one. "
                    "Do NOT create near-duplicate keys (e.g. project.x.approach AND project.x.refined). "
                    'To DELETE a stale/invalidated key, set "delete": true (e.g. pet died → delete '
                    "user.pet.name; project cancelled → delete project.x.status). "
                    f"Max {_MAX_SEMANTIC_PER_CONSOLIDATION} items."
                )
                keys.append(
                    '"episodic": Array of conversation fragments worth remembering. '
                    'Each: {"text": "...", "tags": ["tag1"], "importance": 0.0-1.0}. '
                    "Rules: text 10-2000 chars, factual. importance 0.9+ = critical, "
                    "0.7-0.9 = useful, 0.5-0.7 = minor. Skip greetings/small talk. "
                    f"Max {_MAX_EPISODIC_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Do NOT write simple key-value facts here that belong in semantic "
                    "(e.g. 'Favorite color: blue'). Episodic is for events, decisions, and context "
                    "— not for duplicating semantic facts."
                )

            # Markdown memory (backward compat when not migrated)
            if not self._migrated:
                keys.append(
                    '"preferences_update": The COMPLETE updated preferences file. '
                    "Merge duplicates, keep only newest if contradicted, remove stale "
                    "one-off observations. Keep '# User Preferences' header. "
                    "Return existing content exactly if nothing changed."
                )
                keys.append(
                    '"projects_update": The COMPLETE updated projects file. '
                    "Only active projects, remove stale entries, update facts. "
                    "Keep '# Active Projects' header. Return existing if unchanged."
                )

            if include_history:
                keys.append(
                    '"lessons": Array of corrections the user taught '
                    '(e.g. "no, do X", "always Y", "never Z"). '
                    'Each: {"rule": "...", "negative": "...", "category": "tool|preference|knowledge"}. '
                    "Empty [] if no corrections. Skip general preferences. "
                    f"Max {_MAX_LESSONS_PER_CONSOLIDATION} items. "
                    "IMPORTANT: Only extract lessons that the user did NOT explicitly ask "
                    "to remember (those are already saved via learn_add). Only extract "
                    "implicit corrections the user made without saying 'remember'."
                )

            # ── Auto skill creation ──
            # Only eligible when the feature is enabled, we have a loader to
            # write to, we're on the history path (so prefs-only doesn't retrigger
            # extraction), and the session has enough tool calls to be non-trivial.
            auto_skills_eligible = (
                include_history
                and self._auto_skills_enabled
                and self._skills_loader is not None
                and _count_tool_call_messages(unconsolidated) >= self._auto_min_tool_calls
                and not _session_touched_sensitive(unconsolidated)
            )
            if auto_skills_eligible:
                keys.append(
                    '"new_skill": Object or null. Return an object ONLY if this '
                    "session contained a non-trivial reusable multi-step procedure "
                    "that future sessions would benefit from (e.g. debugging a "
                    "specific class of error, running a multi-command sequence, "
                    "a research synthesis flow). Shape: "
                    '{"slug": "<kebab-case-4-to-60-chars>", '
                    '"description": "<=150 chars, starts with verb>", '
                    '"triggers": "<3-8 comma-separated keywords/phrases>", '
                    '"procedure_md": "<concise markdown body with '
                    "## When to use / ## Steps / ## Gotchas sections, "
                    '<=8000 chars>"}. '
                    'Return null if the session was trivial, a single-shot answer, '
                    "a one-off failure, or involved sensitive paths. Do NOT "
                    "include absolute paths, credentials, tokens, or user PII in "
                    "the procedure body."
                )
                if self._auto_refine_enabled:
                    keys.append(
                        '"refined_skill": Object or null. If an existing '
                        '"auto/..." skill was loaded during this session AND '
                        "the agent found a better procedure than the one "
                        "documented in that skill, return: "
                        '{"name": "auto/<existing-slug>", '
                        '"description": "<updated>", "triggers": "<updated>", '
                        '"procedure_md": "<refined markdown>"}. Return null '
                        "if nothing was refined. Do not fabricate refinements."
                    )

            numbered = "\n\n".join(f"{i + 1}. {k}" for i, k in enumerate(keys))
            prompt_parts = [
                "You are a memory consolidation agent. Process this conversation "
                f"and return a JSON object with these keys:\n\n{numbered}",
            ]
            if has_vector:
                prompt_parts.append(f"\n\n## Current Semantic Memory\n{semantic_json}")
            if not self._migrated:
                prompt_parts.append(f"\n\n## Current Preferences\n{current_prefs or '(empty)'}")
                prompt_parts.append(f"\n\n## Current Projects\n{current_projects or '(empty)'}")
            prompt_parts.append(f"\n\n## Conversation to Process\n{conversation}")
            prompt_parts.append("\n\nRespond with ONLY valid JSON, no markdown fences.")
            prompt = "".join(prompt_parts)

            result = await self._call_llm(prompt)
            if not result:
                return

            if entry := result.get("history_entry"):
                memory.append_history(entry)
                logger.info("Consolidated %d messages for %s", len(unconsolidated), key)

            # Structured memory writes (Phase 2/3). Offloaded to a worker thread:
            # _write_structured_memory embeds each item via a blocking urllib call
            # to the in-process embedder, and _consolidate runs on the event loop thread (fired via
            # asyncio.create_task). Running it inline stalls the whole gateway loop
            # if the embedding endpoint is slow/hung (heartbeats, Slack, dashboard).
            if self._vector_store:
                await run_in_embed_pool(self._write_structured_memory, result, key)

            # Markdown writes (backward compat — skip if migrated)
            if not self._migrated:
                if prefs := result.get("preferences_update"):
                    if prefs.strip() != current_prefs.strip():
                        memory.write_preferences(prefs)

                if projects := result.get("projects_update"):
                    if projects.strip() != current_projects.strip():
                        memory.write_projects(projects)

            # Lesson extraction: _save_lessons calls write_lesson which embeds
            # each rule (+ up to 5 lazy backfills) via blocking urllib to Ollama.
            # Same rationale as _write_structured_memory above — must offload.
            if (self._lesson_store or self._vector_store) and (
                raw_lessons := result.get("lessons")
            ):
                await run_in_embed_pool(self._save_lessons, raw_lessons)

            # Auto skill creation / refinement.
            # Guarded by flag + eligibility — failures are logged, never fatal.
            if auto_skills_eligible:
                try:
                    self._process_auto_skills(result, key)
                except Exception:
                    logger.warning(
                        "Auto-skill processing failed for %s", key, exc_info=True
                    )

            # Only advance the consolidated offset for history consolidation.
            # Prefs-only consolidation uses a separate in-memory offset.
            if include_history:
                self._log.mark_consolidated(key, total)

        except Exception:
            logger.exception("Consolidation failed for %s", key)
            raise
        finally:
            self._running.discard(key)

    def _save_lessons(self, raw: object) -> None:
        """Save extracted lessons from consolidation result."""
        if not isinstance(raw, list):
            return

        # Cap like semantic/episodic: each write_lesson can perform up to 6
        # blocking embeds, so an uncapped LLM lessons array would occupy a
        # worker thread for minutes.
        if len(raw) > _MAX_LESSONS_PER_CONSOLIDATION:
            logger.warning(
                "Consolidation returned %d lessons; capping to %d",
                len(raw),
                _MAX_LESSONS_PER_CONSOLIDATION,
            )
            raw = raw[:_MAX_LESSONS_PER_CONSOLIDATION]

        # Prefer vector store (dedup-aware) over JSONL
        if self._vector_store:
            count = 0
            for item in raw:
                if isinstance(item, dict) and item.get("rule"):
                    ok = self._vector_store.write_lesson(
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                    )
                    if ok:
                        count += 1
            if count:
                logger.info("Extracted %d lesson(s) from chat (vector store)", count)
            return

        if not self._lesson_store:
            return
        from datetime import timezone as _tz

        from kiro_crew.learn import Lesson

        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                self._lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                    )
                )
                count += 1
        if count:
            logger.info("Extracted %d lesson(s) from chat", count)

    def _write_structured_memory(self, result: dict, key: str) -> None:
        """Write semantic + episodic entries from consolidation result."""
        if not self._vector_store:
            return
        source = f"consolidation:{key}"

        # Semantic entries
        semantic_items = result.get("semantic")
        if isinstance(semantic_items, list):
            written = 0
            deleted = 0
            for item in semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "key" not in item:
                    continue
                # Handle deletion of stale keys
                if item.get("delete"):
                    if self._vector_store.delete_semantic(item["key"], source):
                        deleted += 1
                    continue
                conf = float(item.get("confidence", 0.5))
                # Confidence 1.0 means user explicitly stated it — escalate source
                # so it can overwrite previous user_explicit entries
                item_source = "user_explicit" if conf >= 1.0 else source
                err = self._vector_store.set_semantic(
                    key=item["key"],
                    value=item.get("value"),
                    confidence=conf,
                    source=item_source,
                )
                if err is None:
                    written += 1
            if written or deleted:
                logger.info("Semantic consolidation: %d written, %d deleted", written, deleted)

        # Episodic entries
        episodic_items = result.get("episodic")
        if isinstance(episodic_items, list):
            written = 0
            for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
                if not isinstance(item, dict) or "text" not in item:
                    continue
                ep_ok = self._vector_store.write_episodic(
                    text=item["text"],
                    conversation_id=key,
                    tags=item.get("tags", []),
                    importance=float(item.get("importance", 0.5)),
                    source=source,
                )
                if ep_ok:
                    written += 1
            if written:
                logger.info("Wrote %d episodic entries from consolidation", written)

    def _process_auto_skills(self, result: dict, key: str) -> None:
        """Extract + write auto-generated skills from the consolidation result.

        Handles both ``new_skill`` and ``refined_skill`` result keys.  Each
        is validated, redacted via ``security.redact_*``, then deduped
        against existing skills (for new creation) before being written
        through ``SkillsLoader``.  Every successful write emits a SEL audit
        event via ``sel().log_tool_invocation``.
        """
        if self._skills_loader is None:
            return

        def _redact(text: object) -> str:
            """Run the same two-pass redaction used for Slack/dashboard output."""
            if not isinstance(text, str):
                return ""
            safe, _ = redact_exfiltration_urls(text)
            safe, _ = redact_credentials(safe)
            return safe

        # Create path
        new_skill = result.get("new_skill")
        if isinstance(new_skill, dict):
            slug = str(new_skill.get("slug", "")).strip()
            description = _redact(new_skill.get("description", ""))
            triggers = _redact(new_skill.get("triggers", ""))
            procedure_md = _redact(new_skill.get("procedure_md", ""))
            if not (slug and description and procedure_md):
                # Required fields missing (or stripped empty by redaction).
                # Audit the rejection so operators can see that a create
                # attempt happened but lacked the minimum inputs.
                logger.info(
                    "Auto-skill create skipped: empty slug/description/procedure "
                    "after redaction (slug=%r)",
                    slug,
                )
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_create",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={
                        "slug": slug or "(empty)",
                        "reason": "empty_after_redaction",
                    },
                )
            else:
                similar = self._skills_loader.find_similar(
                    description, threshold=self._auto_similarity_threshold
                )
                if similar:
                    logger.info(
                        "Auto-skill synthesis skipped: '%s' overlaps existing skill '%s'",
                        slug,
                        similar,
                    )
                    sel().log_tool_invocation(
                        session_key=key,
                        tool_name="auto_skill_create",
                        tool_kind="skills",
                        outcome="rejected",
                        metadata={
                            "slug": slug,
                            "reason": "similar_exists",
                            "existing": similar,
                        },
                    )
                else:
                    provenance = AutoSkillProvenance(
                        session_key=key,
                        created_at=AutoSkillProvenance.now_iso(),
                    )
                    name = self._skills_loader.create_auto_skill(
                        slug,
                        description=description,
                        triggers=triggers,
                        procedure_md=procedure_md,
                        provenance=provenance,
                    )
                    if name:
                        logger.info("Auto-created skill %s from session %s", name, key)
                        sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="invoked",
                            metadata={"name": name},
                        )
                    else:
                        # create_auto_skill returned None: invalid slug,
                        # oversized procedure, or directory already exists.
                        # Audit the rejection so operators can see why.
                        logger.info(
                            "Auto-skill creation rejected for slug '%s' (creation_failed)",
                            slug,
                        )
                        sel().log_tool_invocation(
                            session_key=key,
                            tool_name="auto_skill_create",
                            tool_kind="skills",
                            outcome="rejected",
                            metadata={
                                "slug": slug,
                                "reason": "creation_failed",
                            },
                        )

        # Refine path (only if explicitly enabled)
        if not self._auto_refine_enabled:
            return
        refined = result.get("refined_skill")
        if isinstance(refined, dict):
            name = str(refined.get("name", "")).strip()
            if not self._skills_loader.is_auto_generated(name):
                logger.info(
                    "Auto-skill refine rejected for %s: not in auto namespace", name
                )
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "not_auto_namespace"},
                )
                return
            description = _redact(refined.get("description", ""))
            triggers = _redact(refined.get("triggers", ""))
            procedure_md = _redact(refined.get("procedure_md", ""))
            if not description or not procedure_md:
                logger.info(
                    "Auto-skill refine skipped for %s: empty description/procedure "
                    "after redaction",
                    name,
                )
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "empty_after_redaction"},
                )
                return
            provenance = AutoSkillProvenance(
                session_key=key,
                created_at=AutoSkillProvenance.now_iso(),
                refined_at=AutoSkillProvenance.now_iso(),
            )
            ok = self._skills_loader.update_auto_skill(
                name,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            if ok:
                logger.info("Auto-refined skill %s from session %s", name, key)
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="invoked",
                    metadata={"name": name},
                )
            else:
                # update_auto_skill returned False: oversized procedure,
                # file missing, or other internal rejection.  Audit it so
                # operators can trace why a refine was proposed but not
                # applied.
                logger.info(
                    "Auto-skill refine rejected for %s (update_failed)", name
                )
                sel().log_tool_invocation(
                    session_key=key,
                    tool_name="auto_skill_refine",
                    tool_kind="skills",
                    outcome="rejected",
                    metadata={"name": name, "reason": "update_failed"},
                )

    async def _call_llm(self, prompt: str) -> dict | None:
        """Call LLM for consolidation via the persistent background session.

        Uses the shared background kiro-cli process (no spawn/teardown cost).
        Returns parsed JSON dict or None on failure.
        """
        if not self._sessions:
            logger.warning("LLM consolidation skipped — no session manager")
            return None

        session_key = BACKGROUND_KEY
        # Timing instrumentation (_bg stall investigation): measure both the
        # wait to acquire the shared `_bg` session (queue contention behind
        # other `_bg` consumers like chat_nav link-preview) and the LLM turn
        # itself. No behavior change. Logged at DEBUG: silent in normal
        # operation, surfaced only when log_level is raised to investigate a
        # consolidation stall.
        t_start = _time.monotonic()
        try:
            client, _is_new, _resumed = await self._sessions.get_or_create(
                session_key, agent="kirocrew-lite"
            )
            t_acquired = _time.monotonic()
            wait_s = t_acquired - t_start
            # Reject all tools: this is a text/JSON-only generation turn. kiro
            # scopes the kirocrew-lite session to tools:[] via set_mode, but the
            # Claude Code backend skips set_mode and injects the full
            # kirocrew-core/cron toolset — without REJECT_ALL a background
            # consolidation turn could fire side-effecting tools (send_message,
            # learn_add, spawn_run). REJECT_ALL keeps both providers tool-free.
            result = await stream_and_collect_json(
                client, prompt, approval_policy=ToolApprovalPolicy.REJECT_ALL
            )
            turn_s = _time.monotonic() - t_acquired
            logger.debug(
                "Consolidation LLM turn: wait=%.1fs turn=%.1fs total=%.1fs ok=%s",
                wait_s,
                turn_s,
                _time.monotonic() - t_start,
                result is not None,
            )
            return result
        except Exception:
            logger.warning(
                "LLM consolidation call failed after %.1fs",
                _time.monotonic() - t_start,
                exc_info=True,
            )
            return None
        finally:
            self._sessions.release(session_key)
            await self._sessions.recycle_background()
