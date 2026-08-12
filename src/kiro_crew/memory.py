"""Persistent memory — structured files, daily history, and FTS5 search.

Structure:
    ~/.kiro/crew/workspace/memory/
    ├── preferences.md      # Learned user preferences
    ├── projects.md         # Active project context
    └── history/
        └── 2026-02-16.md   # Daily conversation summaries

    ~/.kiro/crew/memory_index.db  # FTS5 full-text search index
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from collections.abc import Iterator
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import platform_compat
from kiro_crew._sqlite_compat import FTS5_UNAVAILABLE_HINT, fts5_available, sqlite3
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.metrics.db_metrics import timed, timed_query
from kiro_crew.platform_compat import file_lock

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

# ── Paths ──

WORKSPACE_DIR_NAME = "workspace"
MEMORY_DIR_NAME = "memory"
HISTORY_DIR_NAME = "history"
PREFERENCES_FILE = "preferences.md"
PROJECTS_FILE = "projects.md"

_DEFAULT_PREFERENCES = "# User Preferences\n\n<!-- Learned from conversations -->\n"
_DEFAULT_PROJECTS = "# Active Projects\n\n<!-- Current work context -->\n"

# ── Degenerate-write guard ──
#
# The consolidator asks an LLM for "the COMPLETE updated file" and writes the
# reply verbatim as the new file body.  A model that reads the "return the
# existing content if nothing changed" instruction as a sentinel request answers
# with a placeholder word instead of the file, and the whole memory file is
# replaced by that word.  Such a reply is never a legitimate memory file, so
# guarded writes reject it rather than destroying real content.
_DEGENERATE_BODIES = frozenset(
    {
        "unchanged",
        "(unchanged)",
        "no change",
        "no changes",
        "no changes needed",
        "no changes to make",
        "nothing changed",
        "nothing to change",
        "nothing to update",
        "nochange",
        "not changed",
        "no update",
        "no updates",
        "no change required",
        "no changes required",
        "no update required",
        "no updates required",
        "no changes necessary",
        "same",
        "same as above",
        "same as before",
        "none",
        "n/a",
        "na",
        "null",
        "nil",
    }
)

# A guarded write that keeps less than this fraction of a substantive existing
# file is a truncation, not an edit.  The consolidator does prune stale entries,
# but dropping four fifths of the file in one pass is the clobber signature.
_MIN_RETAINED_FRACTION = 0.2

# Below this size the shrink heuristic is meaningless (a nearly empty file can
# legitimately be replaced by something equally small).
_SHRINK_GUARD_MIN_CHARS = 400

# Lock file serializing read-modify-write of the markdown memory files across
# processes.  Concurrent Kiro Crew sessions each rewrite these files wholesale.
_WRITE_LOCK_FILE = ".write.lock"

# Bounded wait for that lock.  The hold window is a read, a compare and an
# atomic rename (sub-millisecond), so real contention clears immediately; the
# ceiling exists so a wedged or crashed holder can never stall a caller
# indefinitely.  Acquisition is always non-blocking + retry, never a bare
# blocking ``flock``, so the worst case for any caller is a bounded wait.
_WRITE_LOCK_TIMEOUT_SECS = 5.0
_WRITE_LOCK_POLL_SECS = 0.02

# read_recent_history runs on every message turn (context build) and statting +
# reading up to 181 daily files synchronously on the event loop is a per-message
# cost. The assembled string changes only when a day's history file is written
# (append_history) or pruned, so a short TTL keeps it off the hot path while
# staying fresh; the cache key includes the day so the decay window shifting at
# midnight invalidates naturally, and append/prune invalidate explicitly.
_HISTORY_CACHE_TTL_SECS = 5.0

# How long a sqlite connection waits out 'database is locked' contention before
# giving up. Applied both as connect(timeout=) and PRAGMA busy_timeout so a
# transient lock is retried/waited out rather than surfacing as an error the
# self-heal path would misread as corruption.
_DB_BUSY_TIMEOUT_SECS = 5.0

# Substrings that mark a *genuinely* corrupt on-disk index (safe to delete +
# rebuild). Note: 'database is locked'/'is busy' are transient contention, NOT
# corruption, and must never trigger the delete-and-rebuild self-heal.
_DB_CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "malformed",
    "not a database",
)


def _is_corruption_error(exc: BaseException) -> bool:
    """True only for errors indicating genuine on-disk FTS index corruption.

    Deleting and rebuilding the index is destructive (it drops all indexed
    data), so it must fire only for real corruption. A 'database is locked' /
    'database is busy' error is transient contention under concurrent access —
    treating it as corruption would turn normal lock contention into permanent
    data loss, so those are explicitly excluded.
    """
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return False
    return any(marker in msg for marker in _DB_CORRUPTION_MARKERS)


def workspace_dir() -> Path:
    return config_dir() / WORKSPACE_DIR_NAME


def memory_dir() -> Path:
    return workspace_dir() / MEMORY_DIR_NAME


def memory_file() -> Path:
    """Legacy path — kept for backward compat with context.py references."""
    return memory_dir() / PREFERENCES_FILE


def legacy_memory_present() -> bool:
    """True when legacy markdown memory has real content worth migrating.

    Shared by the /api/memory/stats handler and the gateway's boot-time
    auto-migration so both agree on what "there is something to migrate" means:
    any ``- `` bullet in preferences.md/projects.md, any ``history/*.md`` file,
    or a non-trivial ``lessons.jsonl``.
    """
    md = memory_dir()
    for name in (PREFERENCES_FILE, PROJECTS_FILE):
        f = md / name
        if f.is_file() and any(
            line.strip().startswith("- ")
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines()
        ):
            return True
    history = md / HISTORY_DIR_NAME
    if history.is_dir() and any(history.glob("*.md")):
        return True
    lessons_path = config_dir() / "lessons.jsonl"
    if lessons_path.is_file() and lessons_path.stat().st_size > 5:
        return True
    return False


# ── Guarded-write helpers ──


def _memory_body(content: str) -> str:
    """Return *content* stripped of chrome that carries no memory of its own.

    Drops the leading ``# H1`` title, the ``_Updated: <date>_`` stamp that
    :meth:`MemoryStore.write_projects` adds, HTML comment placeholders, and
    blank lines.  What remains is the substance of the file, which is what the
    guard below reasons about: a reply of ``"unchanged"`` and a reply of
    ``"# Active Projects\\n\\nunchanged"`` are equally destructive, and only
    comparing bodies catches both.
    """
    kept: list[str] = []
    for line in content.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("_Updated:") and stripped.endswith("_"):
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def _normalize_placeholder(body: str) -> str:
    """Reduce a body to a bare token for placeholder matching.

    Lowercases, collapses inner whitespace, and strips surrounding markdown
    emphasis/code/quote/bracket characters and trailing punctuation, so
    "Unchanged.", "*unchanged*" and "`no changes`" all reduce to a token the
    _DEGENERATE_BODIES lookup can catch.
    """
    s = re.sub(r"\s+", " ", body.strip().lower())
    s = re.sub(r"^[\s*_`~\"'()\[\]-]+", "", s)
    s = re.sub(r"[\s*_`~\"'()\[\].,!?;:-]+$", "", s)
    return s


def degenerate_write_reason(new: str, current: str) -> str | None:
    """Return why writing *new* over *current* would destroy memory, else ``None``.

    Three rejections, cheapest first: an empty body, a placeholder body (the
    consolidator answering the prompt instead of returning the file), and a
    write that discards most of a substantive file.
    """
    body = _memory_body(new)
    if not body:
        return "empty body"
    if _normalize_placeholder(body) in _DEGENERATE_BODIES:
        return f"placeholder body {body!r}"
    current_body = _memory_body(current)
    if (
        len(current_body) >= _SHRINK_GUARD_MIN_CHARS
        and len(body) < len(current_body) * _MIN_RETAINED_FRACTION
    ):
        return f"truncation: {len(body)} chars would replace {len(current_body)}"
    return None


class MemoryWriteLockTimeout(RuntimeError):
    """Raised when the memory write lock cannot be acquired within the bound.

    Fail-closed signal: the guarded writers refuse rather than write
    unserialized (which could lose a concurrent update). Callers on the event
    loop offload the write and turn this into a transient error / retry.
    """


@contextlib.contextmanager
def _memory_write_lock(memory_dir_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock over the markdown memory files.

    Serializes the read-compare-write sequence in the guarded writers across
    processes, so two concurrent sessions cannot interleave their read and
    their write and lose one another's update.

    Acquisition is non-blocking with a bounded retry (never a bare blocking
    ``flock``): a wedged holder makes this fail closed -- it raises
    ``MemoryWriteLockTimeout`` rather than write unserialized (which could lose
    a concurrent update) or stall the caller forever.  Even so, the whole write
    is synchronous and must not run
    on the event loop thread; the async call sites offload it (see
    ``history.py`` ``_consolidate`` and the dashboard memory handlers), and
    ``test_context.py::TestAsyncCallSitesUseToThread`` fails the build if a new
    coroutine calls a guarded writer inline.
    """
    memory_dir_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(memory_dir_path / _WRITE_LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        deadline = time.monotonic() + _WRITE_LOCK_TIMEOUT_SECS
        while True:
            acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(_WRITE_LOCK_POLL_SECS)
        if not acquired:
            # Fail closed: a wedged holder means we cannot serialize the
            # read-compare-write, so refuse rather than write unserialized and
            # risk losing the holder's concurrent update. The wait is bounded
            # (never blocks the caller); callers on the event loop offload the
            # write and translate this into a transient retry.
            raise MemoryWriteLockTimeout(
                "memory write lock not acquired within "
                f"{_WRITE_LOCK_TIMEOUT_SECS:.1f}s"
            )
        yield
    finally:
        if acquired:
            platform_compat.release_lock(fd)
        os.close(fd)


# ── MemoryStore ──


class MemoryStore:
    """Structured memory: preferences.md, projects.md, daily history, FTS5 search."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace or workspace_dir()
        self._memory_dir = self._workspace / MEMORY_DIR_NAME
        self._history_dir = self._memory_dir / HISTORY_DIR_NAME
        self._preferences_file = self._memory_dir / PREFERENCES_FILE
        self._projects_file = self._memory_dir / PROJECTS_FILE
        self._index_db = (workspace or config_dir()) / "memory_index.db"
        self._vector_store: "VectorMemoryStore | None" = None
        # TTL cache for read_recent_history, keyed by `days` so callers using
        # different windows (context build=14, suggestions=2, dashboard=30) don't
        # evict each other. Value: (monotonic_deadline, day_iso, result).
        self._history_cache: dict[int, tuple[float, str, str]] = {}

    @property
    def vector_store(self) -> "VectorMemoryStore | None":
        return self._vector_store

    @vector_store.setter
    def vector_store(self, store: "VectorMemoryStore | None") -> None:
        self._vector_store = store

    def init(self) -> None:
        """Create directory structure and default files."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        if not self._preferences_file.exists():
            self._preferences_file.write_text(_DEFAULT_PREFERENCES, encoding="utf-8")
        if not self._projects_file.exists():
            self._projects_file.write_text(_DEFAULT_PROJECTS, encoding="utf-8")

    # ── Preferences ──

    def read_preferences(self) -> str:
        """Read user preferences markdown file."""
        if self._preferences_file.exists():
            return self._preferences_file.read_text(encoding="utf-8")
        return ""

    def write_preferences(
        self, content: str, *, expected: str | None = None, force: bool = False
    ) -> bool:
        """Write user preferences and update FTS index.

        See :meth:`write_projects` for the guard semantics; they are identical.
        Returns ``True`` when the file was written.
        """
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        with _memory_write_lock(self._memory_dir):
            if not force and not self._write_allowed(
                PREFERENCES_FILE, content, self.read_preferences(), expected
            ):
                return False
            atomic_write(self._preferences_file, content)
        self._index_file(self._preferences_file, content)
        return True

    def add_preference(self, preference: str) -> None:
        """Append a preference line, avoiding duplicates."""
        content = self.read_preferences()
        if preference not in content:
            content += f"- {preference}\n"
            self.write_preferences(content)

    # ── Projects ──

    def read_projects(self) -> str:
        """Read active projects markdown file."""
        if self._projects_file.exists():
            return self._projects_file.read_text(encoding="utf-8")
        return ""

    def _write_allowed(
        self, name: str, content: str, current: str, expected: str | None
    ) -> bool:
        """Return whether a guarded write of *content* over *current* may proceed.

        Must be called with the memory write lock held, so *current* is the
        content the write will actually replace.
        """
        if expected is not None and current.strip() != expected.strip():
            logger.warning(
                "Rejected stale %s write: on-disk content changed after it was "
                "read, so this update was computed from a stale view and would "
                "drop a concurrent writer's changes",
                name,
            )
            return False
        if reason := degenerate_write_reason(content, current):
            logger.warning("Rejected degenerate %s write: %s", name, reason)
            return False
        return True

    def write_projects(
        self, content: str, *, expected: str | None = None, force: bool = False
    ) -> bool:
        """Write active projects, adding header if missing, and update FTS index.

        Returns ``True`` when the file was written and ``False`` when the write
        was rejected.  Automated callers (the consolidator) get two protections:

        - **Degenerate-write guard.** A body that is empty, a placeholder such
          as ``unchanged``, or a truncation of a substantive file is rejected
          instead of overwriting real content.
        - **Compare-and-swap.** When *expected* is supplied, the write lands
          only if the on-disk content still matches it, so an update computed
          from a stale read never silently overwrites a concurrent writer.

        Both checks and the write run under an exclusive cross-process lock.
        Pass ``force=True`` for an explicit human edit that intends to shrink or
        clear the file.
        """
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y-%m-%d")
        # Don't double-wrap if content already has the header
        if content.strip().startswith("# Active Projects"):
            full = content.strip() + "\n"
        else:
            full = f"# Active Projects\n\n_Updated: {date}_\n\n{content}\n"
        with _memory_write_lock(self._memory_dir):
            if not force and not self._write_allowed(
                PROJECTS_FILE, full, self.read_projects(), expected
            ):
                return False
            atomic_write(self._projects_file, full)
        self._index_file(self._projects_file, full)
        return True

    # ── Legacy read/write (used by consolidator) ──

    def read(self) -> str:
        """Read preferences + projects as combined memory (legacy compat)."""
        parts: list[str] = []
        prefs = self.read_preferences()
        if prefs.strip() and prefs.strip() != _DEFAULT_PREFERENCES.strip():
            parts.append(prefs)
        projects = self.read_projects()
        if projects.strip() and projects.strip() != _DEFAULT_PROJECTS.strip():
            parts.append(projects)
        return "\n\n".join(parts)

    def write(self, content: str) -> None:
        """Write combined memory — splits into preferences + projects sections.

        An explicit whole-memory set (CLI, eval seeding), so it bypasses the
        guards that protect the consolidator from itself.
        """
        if "# Active Projects" in content:
            idx = content.index("# Active Projects")
            self.write_preferences(content[:idx].strip() + "\n", force=True)
            # Use atomic_write + index (not write_projects which adds header)
            projects_content = content[idx:].strip() + "\n"
            self._memory_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(self._projects_file, projects_content)
            self._index_file(self._projects_file, projects_content)
        else:
            self.write_preferences(content, force=True)

    # ── Daily History ──

    def _today_history_file(self) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        return self._history_dir / f"{date}.md"

    def append_history(self, entry: str) -> None:
        """Append a timestamped entry to today's daily history file.

        The whole read-modify-write is serialized behind an exclusive advisory
        file lock so concurrent appends from other sessions/threads or processes
        cannot interleave and clobber each other's entries. The lock uses
        :func:`kiro_crew.platform_compat.file_lock` (real cross-platform locking
        — ``flock`` on POSIX, ``msvcrt`` on Windows). The lock covers read +
        rewrite; indexing and cache invalidation run after it is released.
        """
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._today_history_file()
        lock_path = self._history_dir / ".append.lock"
        timestamp = datetime.now().astimezone().strftime("%H:%M %Z")

        with open(lock_path, "w") as fd:
            with file_lock(fd.fileno(), exclusive=True):
                content = ""
                if path.exists():
                    content = path.read_text(encoding="utf-8")
                if not content:
                    date = datetime.now().strftime("%Y-%m-%d")
                    content = f"# {date}\n"

                content += f"\n#### {timestamp}\n{entry.strip()}\n"
                path.write_text(content, encoding="utf-8")
        self._index_file(path, content)
        self._invalidate_history_cache()  # today's window changed

    def prune_history(self, keep_days: int = 365) -> int:
        """Delete daily history files older than *keep_days*. Returns count deleted."""
        if not self._history_dir.exists():
            return 0
        cutoff = datetime.now().date() - timedelta(days=keep_days)
        deleted = 0
        for f in self._history_dir.glob("*.md"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d").date()
                if file_date < cutoff:
                    f.unlink()
                    deleted += 1
            except ValueError:
                continue
        if deleted:
            logger.info("Pruned %d history files older than %d days", deleted, keep_days)
            self._invalidate_history_cache()
        return deleted

    def read_recent_history(self, days: int = 14) -> str:
        """Load daily history with natural decay: recent=full, older=summary.

        TTL-cached (keyed on ``days`` + today's date) because this runs on every
        message turn and otherwise stats + reads up to 181 files synchronously.
        ``append_history``/``prune_history`` invalidate the cache on write.
        """
        if days <= 0:
            return ""
        today = datetime.now().date()
        today_iso = today.strftime("%Y-%m-%d")
        cached = self._history_cache.get(days)
        if cached is not None and time.monotonic() < cached[0] and cached[1] == today_iso:
            return cached[2]
        result = self._read_recent_history_uncached(days, today)
        self._history_cache[days] = (
            time.monotonic() + _HISTORY_CACHE_TTL_SECS,
            today_iso,
            result,
        )
        return result

    def _invalidate_history_cache(self) -> None:
        """Drop all cached recent-history windows (after append/prune)."""
        self._history_cache.clear()

    def _read_recent_history_uncached(self, days: int, today: _date) -> str:
        """Assemble the decayed recent-history string (no caching)."""
        parts: list[str] = []
        for i in range(181):
            day = today - timedelta(days=i)
            path = self._history_dir / f"{day.strftime('%Y-%m-%d')}.md"
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                continue

            if i < days:
                parts.append(content)
            elif i < 61:
                parts.append(self._summarize_day(content))
            else:
                n = content.count("####")
                parts.append(f"# {day.strftime('%Y-%m-%d')}\n_{n} conversation(s)_")
        return "\n\n".join(parts)

    @staticmethod
    def _summarize_day(content: str) -> str:
        """Extract header + first entry from a daily history file."""
        sections = content.split("####")
        header = sections[0].strip()
        first = sections[1].strip() if len(sections) > 1 else ""
        result = header + ("\n#### " + first if first else "")
        n_more = len(sections) - 2
        if n_more > 0:
            result += f"\n_…{n_more} more entries_"
        return result

    def read_history(self) -> str:
        """Read all history from the last 30 days (legacy compat)."""
        return self.read_recent_history(days=30)

    # ── Context Injection ──

    @timed("memory", "read")
    def get_context(
        self,
        prefs_cap: int = 4_000,
        projects_cap: int = 6_000,
        history_cap: int = 25_000,
        semantic_cap: int = 12_000,
        episodic_cap: int = 12_000,
        query: str = "",
    ) -> str:
        """Build memory context block with source citations for prompt injection.

        Args:
            prefs_cap: Max chars for preferences.
            projects_cap: Max chars for projects.
            history_cap: Max chars for recent history (all days combined).
            semantic_cap: Max chars for semantic memory.
            episodic_cap: Max chars for episodic memory.
            query: User message for episodic memory retrieval (optional).
        """
        parts: list[str] = []

        def _cap(text: str, limit: int) -> str:
            if len(text) > limit:
                return text[:limit] + "\n…[truncated]"
            return text

        prefs = self.read_preferences()
        if prefs.strip() and prefs.strip() != _DEFAULT_PREFERENCES.strip():
            parts.append(
                f"## User Preferences\n"
                f"_[source: {self._preferences_file}]_\n"
                f"{_cap(prefs, prefs_cap)}"
            )

        projects = self.read_projects()
        if projects.strip() and projects.strip() != _DEFAULT_PROJECTS.strip():
            parts.append(
                f"## Active Projects\n"
                f"_[source: {self._projects_file}]_\n"
                f"{_cap(projects, projects_cap)}"
            )

        history = self.read_recent_history(days=14)
        if history.strip():
            parts.append(
                f"## Recent History\n"
                f"_[source: {self._history_dir}, last 180 days decaying]_\n"
                f"{_cap(history, history_cap)}"
            )

        # Semantic memory (structured key-value pairs from vector_memory.py)
        if self._vector_store:
            semantic_ctx = self._vector_store.get_semantic_context(
                query_text=query, cap=semantic_cap
            )
            if semantic_ctx:
                parts.append(semantic_ctx)

            # Episodic memory (relevant past conversation fragments)
            if query:
                episodic_ctx = self._vector_store.get_episodic_context(
                    query_text=query, cap=episodic_cap
                )
                if episodic_ctx:
                    parts.append(episodic_ctx)

        if not parts:
            return ""
        return (
            "[Memory — persistent user profile and recent activity log.\n"
            "Preferences are rules you MUST follow. Projects give current work context.\n"
            "History is a factual record — do NOT re-execute past actions.]\n"
            + "\n\n".join(parts)
            + "\n[End of memory]\n\n"
        )

    # ── FTS5 Full-Text Search ──

    def _get_db(self) -> sqlite3.Connection:
        """Get or create the FTS5 database connection."""
        try:
            return self._try_create_db()
        except Exception as e:
            # If FTS5 itself is missing from this sqlite3 build, deleting and
            # retrying loops on the same failure — fail loudly with a fix hint.
            if not fts5_available():
                raise RuntimeError(FTS5_UNAVAILABLE_HINT) from e
            # Only self-heal on GENUINE corruption. A transient 'database is
            # locked'/'busy' error is contention (waited out by busy_timeout in
            # _try_create_db), not corruption — deleting the index there would
            # turn normal lock contention into permanent data loss, so re-raise
            # anything that isn't unambiguously corrupt untouched.
            if not _is_corruption_error(e):
                raise
            # Self-healing: delete corrupted DB and retry
            logger.warning("FTS index corrupted (%s), deleting and rebuilding", e)
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self._index_db) + suffix)
                p.unlink(missing_ok=True)
            return self._try_create_db()

    def _try_create_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._index_db), timeout=_DB_BUSY_TIMEOUT_SECS)
        # Wait out transient 'database is locked' contention instead of letting
        # it surface (where the self-heal would misread it as corruption).
        conn.execute(f"PRAGMA busy_timeout={int(_DB_BUSY_TIMEOUT_SECS * 1000)}")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "path, content, tokenize='porter unicode61')"
        )
        return conn

    def _index_file(self, path: Path, content: str) -> None:
        """Index a single file (incremental update)."""
        conn = None
        try:
            conn = self._get_db()
            path_str = str(path)
            conn.execute("DELETE FROM memory_fts WHERE path = ?", (path_str,))
            conn.execute(
                "INSERT INTO memory_fts (path, content) VALUES (?, ?)",
                (path_str, content),
            )
            conn.commit()
        except Exception:
            logger.debug("FTS index update failed", exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    def rebuild_index(self) -> int:
        """Rebuild the full FTS index from all memory files. Returns file count."""
        files: list[tuple[str, str]] = []
        for path in (self._preferences_file, self._projects_file):
            if path.exists():
                files.append((str(path), path.read_text(encoding="utf-8")))
        if self._history_dir.exists():
            for path in self._history_dir.glob("*.md"):
                files.append((str(path), path.read_text(encoding="utf-8")))

        conn = None
        try:
            conn = self._get_db()
            conn.execute("DELETE FROM memory_fts")
            for path_str, content in files:
                conn.execute(
                    "INSERT INTO memory_fts (path, content) VALUES (?, ?)",
                    (path_str, content),
                )
            conn.commit()
        except Exception:
            logger.warning("FTS rebuild failed", exc_info=True)
        finally:
            if conn is not None:
                conn.close()
        return len(files)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search memory using FTS5. Returns [{path, snippet, rank}]."""
        conn = None
        try:
            # Inside the try, not around it: this method handles its own errors
            # and returns [], so a timer wrapping the whole call would record
            # every failure as a success. Here a raising query is tagged
            # outcome=error before the except below swallows it.
            with timed_query("memory", "search"):
                conn = self._get_db()
                cursor = conn.execute(
                    "SELECT path, snippet(memory_fts, 1, '>>>', '<<<', '...', 32), rank "
                    "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                    (query, limit),
                )
                results = [
                    {"path": row[0], "snippet": row[1], "rank": row[2]}
                    for row in cursor.fetchall()
                ]
            return results
        except Exception:
            logger.debug("FTS search failed", exc_info=True)
            return []
        finally:
            if conn is not None:
                conn.close()
