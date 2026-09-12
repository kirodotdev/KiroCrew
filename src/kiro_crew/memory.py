"""Persistent memory — structured files, daily history, and FTS5 search.

Structure:
    ~/.kiro/crew/workspace/memory/
    ├── preferences.md      # Learned user preferences
    ├── projects.md         # Active project context
    └── history/
        └── 2026-02-16.md   # Daily conversation summaries

    ~/.kiro/crew/memory_index.db  # FTS5 full-text search index

The DEFAULT store's index sits in the data-home root, beside ``memory.db``, and
not inside the markdown tree it describes: that is where the snapshot ``memory``
component, ``portability``'s export zip and the remote-sync script all name it.
A NAMED store's index lives inside that store's own directory instead. Which of
the two a store gets is ``memory_stores.memory_index_path_for``'s decision, not
this module's — see docs/system-specs/modules/memory-skills-hooks.md.
"""

from __future__ import annotations

import heapq
import logging
import os
import stat as _stat
import time
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kiro_crew._sqlite_compat import (
    FTS5_UNAVAILABLE_HINT,
    fts5_available,
    fts5_quote_tokens,
    sqlite3,
)
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.hooks import (
    FileTooLargeError,
    is_unc_shape,
    safe_read_file_bytes_nolink,
    unc_probe_allowed,
)
from kiro_crew.memory_startup import require_memory_ready
from kiro_crew.memory_stores import named_store_operation
from kiro_crew.metrics.db_metrics import timed, timed_query
from kiro_crew.pinned_fs import fd_real_path
from kiro_crew.platform_compat import file_lock, first_linked_ancestor, is_link_or_junction

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

# ── Paths ──

WORKSPACE_DIR_NAME = "workspace"
MEMORY_DIR_NAME = "memory"
#: FTS5 index filename. Only the NAME is owned here — WHERE a given store's
#: index sits is store policy and belongs to
#: ``memory_stores.memory_index_path_for``. Named so the snapshot, portability
#: and remote-sync consumers that spell this file out have one definition to
#: point at.
INDEX_DB_FILE = "memory_index.db"
HISTORY_DIR_NAME = "history"
PREFERENCES_FILE = "preferences.md"
PROJECTS_FILE = "projects.md"

_DEFAULT_PREFERENCES = "# User Preferences\n\n<!-- Learned from conversations -->\n"
_DEFAULT_PROJECTS = "# Active Projects\n\n<!-- Current work context -->\n"

# Explicit history readers can stat and read many daily files. The assembled
# string changes only when a day's history file is written
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


# ── MemoryStore ──


def _fts5_literal_query(query: str) -> str:
    """Turn user words into an FTS5 expression that matches them literally.

    Tokens are quoted by the shared :func:`fts5_quote_tokens` primitive and
    joined with FTS5's implicit AND: every word the user typed must appear. That
    differs deliberately from knowledge retrieval, which drops stopwords and ORs
    for natural-language recall -- a hand-typed memory query is precise, so
    widening it would bury the both-words hit under single-word noise.

    Returns "" when the query holds no tokens, which the caller treats as no
    match rather than handing FTS5 an empty expression to reject.
    """
    return " ".join(fts5_quote_tokens(query))


class MemoryStore:
    """Structured memory: preferences.md, projects.md, daily history, FTS5 search."""

    def __init__(
        self,
        workspace: Path | None = None,
        index_db: Path | None = None,
        *,
        memory_version: int = 1,
    ):
        """*index_db* is the FTS index file; omitting it keeps the default store's.

        The index location is STORE POLICY — the default store's sits in the
        data-home root, a named store's inside that store's own directory — and
        policy lives in ``memory_stores.memory_index_path_for``, which is the
        one place that knows a store name. Passing the resolved path in keeps
        this class independent of store-name/path policy. The caller also passes
        the resolved memory version: V2 retains history without age-based loss.

        The fallback derivation carries a quirk worth knowing: a bare
        ``MemoryStore()`` indexes to ``<home>/memory_index.db`` while
        ``MemoryStore(workspace=workspace_dir())`` indexes to
        ``<home>/workspace/memory_index.db``, though both share one
        ``_workspace`` and one markdown tree. Both forms are in use (``cli.py``
        takes the first, ``context.py`` the second) and both work, because
        ``rebuild_index`` regenerates the whole index from preferences.md,
        projects.md and history/*.md and reads no index state — so the cost is a
        duplicated rebuild, not a wrong answer. Only the root copy is in the
        snapshot ``memory`` component, so collapsing the two moves a default
        path, which takes a store name at every call site to do safely.
        """
        if memory_version not in (1, 2):
            raise ValueError("memory_version must be 1 or 2")
        self._memory_version = memory_version
        self._workspace = workspace or workspace_dir()
        from kiro_crew.memory_stores import named_store_of_db

        self._memory_store_name = named_store_of_db(self._workspace / "memory.db")
        self._memory_dir = self._workspace / MEMORY_DIR_NAME
        self._history_dir = self._memory_dir / HISTORY_DIR_NAME
        self._preferences_file = self._memory_dir / PREFERENCES_FILE
        self._projects_file = self._memory_dir / PROJECTS_FILE
        self._index_db = index_db or (workspace or config_dir()) / INDEX_DB_FILE
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
        if getattr(store, "algorithm_version", None) == "v2" and self._memory_version != 2:
            # A prepared private tier is positive identity evidence. Retention
            # stays enabled even while that tier is temporarily detached.
            self._memory_version = 2
            self._invalidate_history_cache()

    # ── Atomic writes (committed-versions-only contract) ──

    def _require_link_free_roots(self) -> None:
        """WRITE-path admission gate — the same single invariant the read
        surface enforces in :meth:`_read_root_guard`: no filesystem syscall
        may touch a path whose workspace, memory-root, or history-dir
        component is a link/junction (or an untrusted UNC workspace on
        Windows; on Windows the workspace's ancestor chain is walked too,
        while POSIX ancestors are deliberately excluded — see the read gate).

        Point defenses alone (hardened temp files, symlink-safe lock opens)
        leave each writer trusting the directories themselves, so a linked
        ``memory/`` or ``history/`` directory routes staging and replacement
        outside the workspace. One
        gate at every writer entry makes that whole class unreachable
        instead of patching instances. Writers must fail LOUD, not silently
        no-op, so this raises where the read gate returns ``False`` (a
        refused read degrades to an empty entry; a refused write must not
        look like success). The refusal is SEL-audited by the shared guard.
        """
        if not self._read_root_guard():
            raise OSError(
                f"memory write refused (linked root or untrusted workspace): {self._memory_dir}"
            )

    def _open_lock_nofollow(self, lock_path: Path) -> int:
        """Open (creating if absent) a lock file without following links.

        A bare ``open(path, "w")`` truncates before locking and follows a
        symlink, so an agent-planted ``.write.lock`` link would get its
        same-user TARGET truncated by the next memory write. This opens with
        ``O_NOFOLLOW`` (a symlink leaf fails with ELOOP instead of being
        traversed), never truncates (no ``O_TRUNC`` — a lock file carries no
        content), and requires a lone regular inode via ``fstat`` (rejects
        special files and hardlinked inodes). A planted link therefore makes
        the write fail closed rather than damage the link's target. Caller
        owns the returned fd and must ``os.close`` it.

        Windows has no ``O_NOFOLLOW`` (and ``O_NOFOLLOW`` would not cover a
        directory junction anyway), so the leaf is additionally rejected with
        an lstat-based link/junction check before the open — not race-free
        like the POSIX flag, but it matches the platform's best available
        primitive and the rest of this surface's Windows posture.
        """
        if is_link_or_junction(lock_path):
            raise OSError(f"refusing lock file (link or junction): {lock_path}")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise OSError(f"refusing lock file (not a lone regular inode): {lock_path}")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _atomic_write_text(self, path: Path, content: str, *, newline: str | None = None) -> None:
        """Publish *content* to *path* via unique temp file + ``os.replace``.

        ``write_text`` truncates then writes, so a concurrent reader can
        observe an empty or partial file between those two steps. Delegates
        to :func:`kiro_crew.atomic_write.atomic_write`, which stages the
        bytes in a ``tempfile.mkstemp`` sibling (``O_CREAT | O_EXCL`` with an
        unpredictable name, so an agent-planted symlink at a guessable temp
        path is never followed) and atomically renames it over the target —
        a reader only ever observes COMMITTED versions. The structured read
        surface additionally double-stats around its read, so a replace
        landing mid-read is retried rather than pairing one version's bytes
        with another version's mtime. The temp carries a ``.tmp`` suffix so
        history ``*.md`` globbing never picks it up.

        An existing destination's permission bits are preserved: ``write_text``
        truncated in place and never touched the mode, but a rename-based
        replace installs the temp file's mode, so without carrying the old
        mode over a user's ``0o600`` memory file would silently widen to the
        umask default on the next write.
        """
        # Admission gate FIRST (see _require_link_free_roots): even the mode
        # stat below traverses the directory chain, and on Windows a stat of
        # a UNC path is itself the outbound SMB probe.
        self._require_link_free_roots()
        # The LEAF must not be a link either: replacing a link with a regular
        # file is safe, but callers doing read-modify-write would have read
        # the link's TARGET, and the mode stat would report the target's
        # mode. Reject before any following syscall; metadata via lstat.
        if is_link_or_junction(path):
            self._audit_read_refusal("leaf_link", path, "memory write target is a link/junction")
            raise OSError(f"memory write refused (target is a link): {path}")
        mode: int | None = None
        try:
            mode = _stat.S_IMODE(os.lstat(path).st_mode)
        except OSError:
            pass  # new file: let atomic_write apply the umask default
        atomic_write(path, content, mode=mode, newline=newline)

    @named_store_operation
    def init(self) -> None:
        """Create directory structure and default files."""
        self._require_link_free_roots()  # gate before the first syscall
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)
        if not self._preferences_file.exists():
            self._atomic_write_text(self._preferences_file, _DEFAULT_PREFERENCES)
        if not self._projects_file.exists():
            self._atomic_write_text(self._projects_file, _DEFAULT_PROJECTS)

    # ── Preferences ──

    @named_store_operation
    def read_preferences(self) -> str:
        """Read user preferences markdown file."""
        require_memory_ready(self._memory_store_name)
        if self._memory_version == 2:
            return self._guarded_entry(self._preferences_file, require_readable=True)["content"]
        if self._preferences_file.exists():
            return self._preferences_file.read_text(encoding="utf-8")
        return ""

    @named_store_operation
    def write_preferences(self, content: str, *, expected_baseline: str | None = None) -> bool:
        """Write user preferences and update FTS index.

        Serialized behind the same advisory ``file_lock`` mechanism
        :meth:`append_history` uses: async callers offload these
        writes to worker threads, so a dashboard Save and a consolidation pass
        can run concurrently (the event loop does not accidentally
        serialize them), and without the lock two whole-file atomic writes
        of independently-read snapshots would silently last-writer-win.

        ``expected_baseline`` is the compare-and-swap guard for the
        read-merge-write callers: the consolidator reads the file, spends
        minutes in an LLM call, then writes back a whole-file result — a
        user's dashboard Save landing in that window would be silently
        reverted. Pass the content the merge was computed FROM; if the file
        no longer matches it EXACTLY (checked inside the lock — any byte
        difference, whitespace included, means the merge is stale), the
        write is skipped and ``False`` is returned. ``None`` writes
        unconditionally (direct user intent wins). Returns ``True`` when the
        write happened.
        """
        self._require_link_free_roots()  # gate before the first syscall
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = self._open_lock_nofollow(self._memory_dir / ".write.lock")
        try:
            with file_lock(lock_fd, exclusive=True):
                if expected_baseline is not None and self.read_preferences() != expected_baseline:
                    logger.info(
                        "Skipping stale preferences write: file changed since the "
                        "baseline this update was computed from"
                    )
                    return False
                self._atomic_write_text(self._preferences_file, content)
                # Indexed INSIDE the lock: with concurrent writers, indexing
                # after release lets writer B's file land while writer A's
                # index write runs last — file says B, search returns A.
                self._index_file(self._preferences_file, content)
        finally:
            os.close(lock_fd)
        return True

    @named_store_operation
    def add_preference(self, preference: str) -> None:
        """Append a preference line, avoiding duplicates."""
        content = self.read_preferences()
        if preference not in content:
            content += f"- {preference}\n"
            self.write_preferences(content)

    # ── Projects ──

    @named_store_operation
    def read_projects(self) -> str:
        """Read active projects markdown file."""
        require_memory_ready(self._memory_store_name)
        if self._memory_version == 2:
            return self._guarded_entry(self._projects_file, require_readable=True)["content"]
        if self._projects_file.exists():
            return self._projects_file.read_text(encoding="utf-8")
        return ""

    @named_store_operation
    def write_projects(self, content: str, *, expected_baseline: str | None = None) -> bool:
        """Write active projects, adding header if missing, and update FTS index.

        Locking and ``expected_baseline`` (compare-and-swap) semantics: see
        :meth:`write_preferences`.
        """
        self._require_link_free_roots()  # gate before the first syscall
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y-%m-%d")
        # Don't double-wrap if content already has the header
        if content.strip().startswith("# Active Projects"):
            full = content.strip() + "\n"
        else:
            full = f"# Active Projects\n\n_Updated: {date}_\n\n{content}\n"
        lock_fd = self._open_lock_nofollow(self._memory_dir / ".write.lock")
        try:
            with file_lock(lock_fd, exclusive=True):
                if expected_baseline is not None and self.read_projects() != expected_baseline:
                    logger.info(
                        "Skipping stale projects write: file changed since the "
                        "baseline this update was computed from"
                    )
                    return False
                self._atomic_write_text(self._projects_file, full)
                # Indexed inside the lock — see write_preferences.
                self._index_file(self._projects_file, full)
        finally:
            os.close(lock_fd)
        return True

    @named_store_operation
    def write_private_profile_validated(
        self, filename: str, content: str, validate: Callable[[str], None]
    ) -> None:
        """Validate both private anchors and commit one while holding their file lock."""
        if self._memory_version != 2 or filename not in {"preferences.md", "projects.md"}:
            raise ValueError("A validated private profile target is required")
        self._require_link_free_roots()
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        if filename == "projects.md":
            date = datetime.now().strftime("%Y-%m-%d")
            normalized = (
                content.strip() + "\n"
                if content.strip().startswith("# Active Projects")
                else f"# Active Projects\n\n_Updated: {date}_\n\n{content}\n"
            )
            target = self._projects_file
        else:
            normalized = content
            target = self._preferences_file
        lock_fd = self._open_lock_nofollow(self._memory_dir / ".write.lock")
        try:
            with file_lock(lock_fd, exclusive=True):
                validate(normalized)
                # Owner documents are read as exact UTF-8 bytes. Translating
                # existing CRLF again on Windows would add CR on every save.
                self._atomic_write_text(target, normalized, newline="")
                self._index_file(target, normalized)
        finally:
            os.close(lock_fd)

    # ── Legacy read/write (used by consolidator) ──

    @named_store_operation
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

    @named_store_operation
    def write(self, content: str) -> None:
        """Write combined memory — splits into preferences + projects sections."""
        if "# Active Projects" in content:
            idx = content.index("# Active Projects")
            self.write_preferences(content[:idx].strip() + "\n")
            # Atomic write + index (not write_projects which adds header);
            # same lock as write_preferences/write_projects.
            projects_content = content[idx:].strip() + "\n"
            self._require_link_free_roots()  # gate before the first syscall
            self._memory_dir.mkdir(parents=True, exist_ok=True)
            lock_fd = self._open_lock_nofollow(self._memory_dir / ".write.lock")
            try:
                with file_lock(lock_fd, exclusive=True):
                    self._atomic_write_text(self._projects_file, projects_content)
                    # Indexed inside the lock — see write_preferences.
                    self._index_file(self._projects_file, projects_content)
            finally:
                os.close(lock_fd)
        else:
            self.write_preferences(content)

    # ── Daily History ──

    def _today_history_file(self) -> Path:
        date = datetime.now().strftime("%Y-%m-%d")
        return self._history_dir / f"{date}.md"

    @named_store_operation
    def append_history(self, entry: str) -> None:
        """Append a timestamped entry to today's daily history file.

        The whole read-modify-write is serialized behind an exclusive advisory
        file lock so concurrent appends from other sessions/threads or processes
        cannot interleave and clobber each other's entries. The lock uses
        :func:`kiro_crew.platform_compat.file_lock` (real cross-platform locking
        — ``flock`` on POSIX, ``msvcrt`` on Windows). The lock covers read,
        rewrite AND the FTS index update, so the file and its index always
        publish under the same lock tenure; cache invalidation runs after
        release.
        """
        self._require_link_free_roots()  # gate before the first syscall
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._today_history_file()
        lock_path = self._history_dir / ".append.lock"
        timestamp = datetime.now().astimezone().strftime("%H:%M %Z")

        lock_fd = self._open_lock_nofollow(lock_path)
        try:
            with file_lock(lock_fd, exclusive=True):
                # Reject a planted link at today's dated name BEFORE the
                # read: read_text would follow it and this read-modify-write
                # would republish the link target's contents into memory
                # (and thus into show/export).
                if is_link_or_junction(path):
                    self._audit_read_refusal(
                        "leaf_link", path, "today's history file is a link/junction"
                    )
                    raise OSError(f"memory write refused (history leaf is a link): {path}")
                # A HARDLINK passes the link/junction check (it is a regular
                # inode), but reading it republishes the shared inode's
                # contents all the same — an existing leaf must be a LONE
                # regular inode, the same standard _open_lock_nofollow and the
                # hardened reader already enforce. lstat: never follows.
                try:
                    st = os.lstat(path)
                except OSError:
                    st = None  # missing: a fresh day, normal state
                if st is not None and (not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
                    self._audit_read_refusal(
                        "leaf_not_lone_regular",
                        path,
                        "today's history file is not a lone regular inode (hardlink/special)",
                    )
                    raise OSError(
                        f"memory write refused (history leaf is not a lone regular file): {path}"
                    )
                content = ""
                if path.exists():
                    content = path.read_text(encoding="utf-8")
                if not content:
                    date = datetime.now().strftime("%Y-%m-%d")
                    content = f"# {date}\n"

                content += f"\n#### {timestamp}\n{entry.strip()}\n"
                self._atomic_write_text(path, content)
                # Indexed inside the lock — see write_preferences.
                self._index_file(path, content)
        finally:
            os.close(lock_fd)
        self._invalidate_history_cache()  # today's window changed

    @named_store_operation
    def read_editable_history(self) -> str:
        """Read the history document replaced by the dashboard's daily edit.

        V2 edits only today's file; its retained aggregate remains available via
        ``read_recent_history``. V1 keeps the existing aggregate edit contract.
        """
        if self._memory_version != 2:
            return self.read_recent_history()
        return self._guarded_entry(
            self._today_history_file(),
            require_readable=True,
            _private_store=self._validated_private_read_store(),
        )["content"]

    @named_store_operation
    def write_today_history(
        self,
        content: str,
        *,
        expected_baseline: str,
        validate_current: Callable[[str], None],
    ) -> bool:
        """Replace today's file if the editable history has not changed.

        V2 compares today's exact content; V1 preserves its aggregate baseline.
        Compare under the same cross-process lock used by :meth:`append_history`,
        so a consolidation append cannot land between admission and replacement.
        Returns ``False`` for a stale baseline and leaves every file unchanged.
        """
        self._require_link_free_roots()
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._today_history_file()
        lock_fd = self._open_lock_nofollow(self._history_dir / ".append.lock")
        wrote = False
        try:
            with file_lock(lock_fd, exclusive=True):
                if is_link_or_junction(path):
                    self._audit_read_refusal(
                        "leaf_link", path, "today's history file is a link/junction"
                    )
                    raise OSError(f"memory write refused (history leaf is a link): {path}")
                try:
                    st = os.lstat(path)
                except FileNotFoundError:
                    st = None
                if st is not None and (not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
                    self._audit_read_refusal(
                        "leaf_not_lone_regular",
                        path,
                        "today's history file is not a lone regular inode (hardlink/special)",
                    )
                    raise OSError(
                        f"memory write refused (history leaf is not a lone regular file): {path}"
                    )
                current_today = ""
                if st is not None:
                    private = self._validated_private_read_store()
                    current_today = self._guarded_entry(
                        path,
                        require_readable=True,
                        missing_ok=False,
                        _private_store=private,
                    )["content"]
                # Validate the exact replacement target before comparing the
                # edit baseline so hidden or unreadable bytes can never be
                # overwritten, regardless of the displayed history scope.
                validate_current(current_today)
                current = (
                    current_today
                    if self._memory_version == 2
                    else self._read_recent_history_uncached(14, datetime.now().date())
                )
                if current != expected_baseline:
                    logger.info(
                        "Skipping stale history write: recent history changed since the "
                        "baseline this update was computed from"
                    )
                    # A different process can append without touching this
                    # instance's cache. The uncached comparison proved that cache
                    # stale, so make the caller's next read observe the winner.
                    self._invalidate_history_cache()
                    return False
                self._atomic_write_text(
                    path, content, newline="" if self._memory_version == 2 else None
                )
                self._index_file(path, content)
                wrote = True
        finally:
            os.close(lock_fd)
            if wrote:
                self._invalidate_history_cache()
        return True

    @named_store_operation
    def prune_history(self, keep_days: int = 365) -> int:
        """Delete daily history files older than *keep_days*. Returns count deleted."""
        require_memory_ready(self._memory_store_name)
        if self._memory_version == 2:
            return 0
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

    @named_store_operation
    def read_recent_history(self, days: int = 14) -> str:
        """Read history with V1 age tiers or bounded, full retained V2 entries.

        TTL-cached (keyed on ``days`` + today's date) for explicit readers.
        ``append_history``/``prune_history`` invalidate the cache on write.
        """
        require_memory_ready(self._memory_store_name)
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
        if self._memory_version == 2:
            # Read limits bound this response, never delete or summarize stored
            # history. Older files remain indexed and explicitly accessible.
            return "\n\n".join(
                entry["content"].strip()
                for entry in reversed(self.read_history_entries())
                if entry["content"].strip()
            )
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

    @named_store_operation
    def read_history(self) -> str:
        """Read all history from the last 30 days (legacy compat)."""
        return self.read_recent_history(days=30)

    # ── Structured markdown reads (CLI read API) ──

    def _audit_read_refusal(self, rule: str, path: Path | str, reason: str) -> None:
        """Best-effort SEL denial record for a refused markdown read.

        The refusal branches below are security controls (link/UNC/special-file
        admission gates over an agent-writable tree), so each denial must leave
        a tamper-evident record in the security event log, not only a process
        log line a same-host actor could suppress. Mirrors
        ``hooks._audit_governance``: lazy import, never lets an audit failure
        break the read path (the refusal itself already fails closed).
        """
        try:
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key="_host",
                tool_name="memory_markdown_read",
                item=str(path),
                outcome="denied",
                rule=rule,
                layer="memory_read_guard",
                reason=reason,
            )
        except Exception:
            logger.debug("memory read refusal audit emit failed", exc_info=True)

    def _read_root_guard(self) -> bool:
        """Single admission gate for the structured read surface.

        INVARIANT: no filesystem syscall in this surface may touch a path
        that has not passed this gate, and no component of a touched path
        may be a link -- on Windows including ancestors; on POSIX ancestors
        are deliberately excluded (see the gate comment below). That one
        property makes the whole REPARSE-POINT finding class
        (symlink/junction escapes, UNC credential probes, special-file reads)
        unreachable instead of patching instances. A mapped network drive or
        ``subst`` target (a ``Z:`` drive letter bound to a network share) is
        a residual outside this class: not UNC-shaped, no reparse point
        anywhere, resolved only at ``realpath`` time. The gates here do not
        screen it.

        Two gates enforce the invariant:

        1. Windows UNC gate — purely LEXICAL, evaluated before any syscall
           (``stat``/``glob``/``exists`` on a UNC path is itself the outbound
           SMB credential probe). Mirrors ``hooks.validate_file_path``.
        2. Reparse-point gate — the memory root and history dir must not be
           symlinks or Windows junctions (``lstat``-based check that never
           traverses the link). On Windows the workspace's ANCESTOR chain is
           walked root-first before any leaf lstat runs, because an lstat
           resolves every ancestor even when it does not follow the final
           component. Leaf files get the same check in
           :meth:`_guarded_entry`, so every component of every touched path
           is verified link-free.
        """
        require_memory_ready(self._memory_store_name)
        root = str(self._memory_dir)
        if os.name == "nt" and is_unc_shape(root) and not unc_probe_allowed(root):
            logger.warning("memory read refused (untrusted UNC workspace): %s", root)
            self._audit_read_refusal("unc_workspace", root, "untrusted UNC workspace")
            return False
        # On Windows a linked ANCESTOR of the workspace defeats the lexical
        # UNC gate above: the workspace path is not itself UNC-shaped -- only
        # the link's target is -- and the lstat-based leaf checks below
        # resolve every ancestor, so the probe itself would traverse the link
        # and open the SMB connection. The walk is root-first and runs before
        # any leaf lstat. On POSIX linked ancestors remain deliberately
        # unrejected: resolving the whole chain would refuse legitimate
        # setups like a symlinked /home, those components are not
        # agent-writable, and stat-ing through a symlink is harmless there --
        # the same Windows-only rationale as the themes wiring
        # (dashboard/handlers/themes.py::_resolve_local_source). The walk
        # assumes the operator-configured workspace is absolute (every
        # in-tree constructor passes one); a relative workspace would walk
        # only the components the path itself names.
        if os.name == "nt" and first_linked_ancestor(self._workspace) is not None:
            logger.warning("memory read refused (workspace ancestor is a link): %s", root)
            self._audit_read_refusal(
                "workspace_linked_ancestor", root, "a workspace ancestor is a link"
            )
            return False
        # The workspace leaf is checked FIRST among the lstat probes: a
        # workspace swapped for a link/junction would make the two descendant
        # checks below traverse it and validate paths inside the link's
        # target instead of the admitted tree. lstat-based, so the link
        # itself is never followed.
        if (
            is_link_or_junction(self._workspace)
            or is_link_or_junction(self._memory_dir)
            or is_link_or_junction(self._history_dir)
        ):
            logger.warning("memory read refused (memory root is a reparse point): %s", root)
            self._audit_read_refusal(
                "root_reparse_point", root, "workspace, memory root or history dir is a link"
            )
            return False
        return True

    @named_store_operation
    def markdown_snapshot(self, since: _date | None = None) -> dict:
        """Structured, read-only view of the markdown memory layer.

        Returns the three markdown surfaces as data::

            {"preferences": entry, "projects": entry, "history": [day, ...]}

        where ``entry`` is ``{"path", "updated_at", "content"}`` and each
        history ``day`` additionally carries its ``date`` (``YYYY-MM-DD``).
        ``updated_at`` is the file's mtime in UTC ISO-8601 so consumers can
        sync incrementally instead of re-reading everything.

        A missing or empty file is a normal state, not an error: the entry is
        returned with ``content: ""`` and ``updated_at: None``. ``since``
        filters history to days on or after that date.
        """
        private = self._validated_private_read_store()
        return {
            "preferences": self._guarded_entry(self._preferences_file, _private_store=private),
            "projects": self._guarded_entry(self._projects_file, _private_store=private),
            "history": self._history_entries(since=since, private_store=private),
        }

    @named_store_operation
    def read_history_entries(self, since: _date | None = None) -> list[dict]:
        """Per-day history entries, oldest first, as structured data.

        Each entry is ``{"date", "path", "updated_at", "content"}``. Files
        whose stem is not a ``YYYY-MM-DD`` date are skipped (mirrors
        :meth:`prune_history`). Unlike :meth:`read_recent_history` this
        enumerates full per-day content with no decay, so consumers get
        discrete entries rather than one concatenated blob.

        The aggregate is bounded: each file's read is individually
        size-capped, but the agent-writable history dir can hold arbitrarily
        many valid dated files, so without an aggregate cap a snapshot (and
        thus ``memory show`` / ``memory export``) could retain unbounded
        content and exhaust memory. At most
        :attr:`_HISTORY_SNAPSHOT_MAX_ENTRIES` entries and
        :attr:`_HISTORY_SNAPSHOT_MAX_BYTES` cumulative content bytes are
        returned; the newest days win when trimming, and the result stays
        oldest-first.
        """
        return self._history_entries(
            since=since, private_store=self._validated_private_read_store()
        )

    def _history_entries(self, *, since: _date | None, private_store: str) -> list[dict]:
        """One bounded snapshot shares identity admission, never opened-file checks."""
        if not self._read_root_guard():
            return []
        if not self._history_dir.exists():
            return []

        def _dated_files() -> "Iterator[tuple[_date, Path]]":
            for f in self._history_dir.glob("*.md"):
                try:
                    day = datetime.strptime(f.stem, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if since is not None and day < since:
                    continue
                yield (day, f)

        # Bounded selection DURING enumeration: the history dir is
        # agent-writable, so the number of valid dated files is unbounded and
        # materializing every (date, path) tuple before capping would let a
        # planted directory exhaust memory. heapq.nlargest keeps at most
        # _HISTORY_SNAPSHOT_MAX_ENTRIES candidates alive and returns them
        # newest-first, which is also the order the caps below want.
        candidates = heapq.nlargest(self._HISTORY_SNAPSHOT_MAX_ENTRIES, _dated_files())
        entries: list[dict] = []
        total_bytes = 0
        # Newest-first so the caps keep the most recent days (the ones a
        # consumer syncing memory actually needs), then restore oldest-first
        # order for the caller.
        for day, f in candidates:
            entry = self._guarded_entry(f, _private_store=private_store)
            # A glob-enumerated file exists, so missing updated_at means the
            # guarded read either REFUSED it (planted link, special file,
            # size cap — skip) or read a genuinely EMPTY day (retain, with
            # null metadata per the documented empty-state contract). A true
            # empty is a lone regular non-link file of size 0.
            if entry["updated_at"] is None:
                try:
                    st = os.lstat(f)
                except OSError:
                    continue
                if not (_stat.S_ISREG(st.st_mode) and st.st_size == 0):
                    continue  # refused, not empty
            size = len(entry["content"].encode("utf-8"))
            # Always admit the first (newest) entry so one large-but-valid day
            # cannot zero out the whole snapshot; the per-file read cap bounds
            # that single entry.
            if entries and total_bytes + size > self._HISTORY_SNAPSHOT_MAX_BYTES:
                break
            total_bytes += size
            entry["date"] = day.isoformat()
            entries.append(entry)
        entries.reverse()
        return entries

    # Aggregate bounds for the history snapshot. Per-file reads are size-capped
    # in _guarded_entry, but the number of valid dated files is attacker/agent
    # controlled, so the aggregate must be bounded too or `memory show` /
    # `memory export --include-markdown` retain unbounded content.
    _HISTORY_SNAPSHOT_MAX_ENTRIES = 366  # ~one year of daily files
    _HISTORY_SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024  # cumulative content bytes

    # One retry when a concurrent writer changes the file mid-read: the second
    # attempt almost always lands after the writer's atomic rewrite finishes.
    _GUARDED_READ_ATTEMPTS = 2

    def _validated_private_read_store(self) -> str:
        """Authorize one read/snapshot; the result is never cached on the store."""
        require_memory_ready(self._memory_store_name)
        private = ""
        if self._memory_version == 2:
            from kiro_crew.memory_stores import named_store_of_db, require_memory_store

            private = named_store_of_db(self._workspace / "memory.db")
            if private:
                require_memory_store(private)
        return private

    def _read_entry_bytes(self, path: Path, *, private_store: str | None = None) -> bytes | None:
        """Read one opened inode under a call-local validated store binding."""
        private = self._validated_private_read_store() if private_store is None else private_store
        if not private:
            return safe_read_file_bytes_nolink(str(path), within_root=str(self._memory_dir))

        # Agent file tools must refuse the whole private tree. This internal
        # reader has a validated store binding and may read only its own opened
        # inode; it never weakens the generic sensitive-path gate.
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            info = os.fstat(descriptor)
            opened = fd_real_path(descriptor)
            if not _stat.S_ISREG(info.st_mode):
                raise OSError("Private memory path is not a regular file")
            if info.st_nlink != 1:
                raise OSError("Private memory file has multiple hard links")
            if opened is None:
                raise OSError("Cannot verify the opened private memory file's location")
            root = os.path.normcase(os.path.realpath(self._memory_dir))
            actual = os.path.normcase(opened)
            expected = os.path.normcase(os.path.abspath(path))
            if actual != expected or os.path.commonpath([actual, root]) != root:
                raise OSError("Opened private memory file is outside its expected bound path")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(self._HISTORY_SNAPSHOT_MAX_BYTES + 1)
            if len(data) > self._HISTORY_SNAPSHOT_MAX_BYTES:
                raise FileTooLargeError("Private memory file exceeds the 8 MiB read limit")
            return data
        finally:
            os.close(descriptor)

    def _guarded_entry(
        self,
        path: Path,
        *,
        require_readable: bool = False,
        missing_ok: bool = True,
        _private_store: str | None = None,
    ) -> dict:
        """Shape one markdown file as ``{"path", "updated_at", "content"}``.

        The memory directory is agent-writable, so a planted dated ``.md``
        name could be a symlink, a hardlink, or a special file. Reads go
        through :func:`kiro_crew.hooks.safe_read_file_bytes_nolink` confined
        to the memory root: it opens with ``O_NOFOLLOW``, rejects non-regular
        files (so a ``/dev/zero`` target cannot wedge the read), rejects
        hardlinked inodes and sensitive resolved targets, and caps the size.
        A refused, unreadable, oversized, or undecodable file surfaces as an
        empty entry — same shape as a missing file — never as leaked content
        or a traceback.

        Private anchors and index rebuilds set ``require_readable`` so a refused
        source raises instead of appearing empty. Missing anchors may initialize
        normally; an already enumerated index source also sets ``missing_ok=False``.

        ``updated_at`` is snapshotted before the read and re-checked after,
        so the reported metadata always describes the bytes returned: when a
        concurrent consolidation rewrites or prunes the file mid-read, the
        read is retried once and then degrades to an empty entry rather than
        pairing one version's content with another version's mtime.
        """
        empty = {"path": str(path), "updated_at": None, "content": ""}

        def refused(reason: str) -> dict:
            if require_readable:
                raise OSError(f"Memory read refused ({reason}): {path}")
            return dict(empty)

        # Admission gate BEFORE the stat below — see _read_root_guard for the
        # invariant. The leaf gets its own lstat-based reparse check so every
        # component of the touched path (root, history dir, file) is verified
        # link-free before any following syscall.
        if not self._read_root_guard():
            return refused("unsafe memory root")
        if is_link_or_junction(path):
            logger.warning("memory read refused (file is a link): %s", path)
            self._audit_read_refusal("leaf_link", path, "memory file is a link/junction")
            return refused("file is a link or junction")
        for _ in range(self._GUARDED_READ_ATTEMPTS):
            try:
                st_before = path.stat()
            except FileNotFoundError:
                return dict(empty) if missing_ok else refused("source file disappeared")
            except OSError as exc:
                return refused(f"cannot inspect file: {exc}")
            # Reject non-regular files BEFORE any open: opening a planted FIFO
            # read-only blocks forever waiting for a writer, so the reader's
            # own fstat check would never be reached. stat() follows symlinks,
            # so a link to a device/FIFO is also rejected here. (A racing swap
            # to a FIFO after this check is the reader's O_NOFOLLOW + fstat
            # problem for symlinks; an active same-host attacker racing the
            # window is outside this surface's threat model.)
            if not _stat.S_ISREG(st_before.st_mode):
                logger.warning("memory read refused (not a regular file): %s", path)
                self._audit_read_refusal(
                    "not_regular_file", path, "memory path is not a regular file"
                )
                return refused("path is not a regular file")
            try:
                data = self._read_entry_bytes(path, private_store=_private_store)
            except FileTooLargeError:
                logger.warning("memory read refused (size cap) for %s", path)
                self._audit_read_refusal("size_cap", path, "memory file exceeds read size cap")
                return refused("file exceeds the read size cap")
            except OSError as exc:
                return refused(str(exc))
            if data is None:
                logger.warning("memory read refused or failed for %s", path)
                self._audit_read_refusal(
                    "read_refused", path, "hardened read refused the file (link/hardlink/target)"
                )
                return refused("linked, escaped or unreadable file")
            try:
                st_after = path.stat()
            except OSError as exc:
                return refused(f"source changed during read: {exc}")
            if (st_before.st_mtime_ns, st_before.st_size) != (
                st_after.st_mtime_ns,
                st_after.st_size,
            ):
                continue  # rewritten mid-read: retry for a stable version
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("memory file is not valid UTF-8: %s", path)
                return refused("file is not valid UTF-8")
            if content == "":
                # Documented empty-state contract: empty content carries null
                # metadata, same shape as a missing file — consumers key
                # incremental sync on updated_at, and an "updated" empty file
                # has nothing to sync.
                return dict(empty)
            updated_at = datetime.fromtimestamp(st_after.st_mtime, tz=timezone.utc).isoformat()
            return {"path": str(path), "updated_at": updated_at, "content": content}
        logger.warning("memory file kept changing during read: %s", path)
        return refused("file kept changing during read")

    # ── Context Injection ──

    @timed("memory", "read")
    @named_store_operation
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
            history_scope = (
                "retained full entries, bounded read"
                if self._memory_version == 2
                else "last 180 days decaying"
            )
            parts.append(
                f"## Recent History\n"
                f"_[source: {self._history_dir}, {history_scope}]_\n"
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
        require_memory_ready(self._memory_store_name)
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

    @named_store_operation
    def rebuild_index(self) -> int:
        """Rebuild the full FTS index from all memory files. Returns file count."""
        require_memory_ready(self._memory_store_name)
        files: list[tuple[str, str]] = []
        if self._memory_version == 2:
            # Validate roots before enumeration. Read every retained day in the
            # transaction below: an unreadable source rolls it back, while one
            # bounded file body at a time avoids retaining the entire history.
            if not self._read_root_guard():
                raise OSError(f"Memory index rebuild refused (unsafe roots): {self._memory_dir}")
            paths = []
            for path in (self._preferences_file, self._projects_file):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                paths.append(path)
            try:
                with os.scandir(self._history_dir) as entries:
                    for candidate in entries:
                        path = Path(candidate.path)
                        if path.suffix != ".md":
                            continue
                        try:
                            datetime.strptime(path.stem, "%Y-%m-%d")
                        except ValueError:
                            continue
                        paths.append(path)
            except FileNotFoundError:
                pass  # an uninitialized, otherwise safe history root is empty

            def guarded_files() -> "Iterator[tuple[str, str]]":
                for path in paths:
                    entry = self._guarded_entry(path, require_readable=True, missing_ok=False)
                    yield str(path), entry["content"]

            sources = guarded_files()
        else:
            for path in (self._preferences_file, self._projects_file):
                if path.exists():
                    files.append((str(path), path.read_text(encoding="utf-8")))
            if self._history_dir.exists():
                for path in self._history_dir.glob("*.md"):
                    files.append((str(path), path.read_text(encoding="utf-8")))
            sources = iter(files)

        conn = None
        indexed = 0
        try:
            conn = self._get_db()
            if self._memory_version == 2:
                conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM memory_fts")
            for path_str, content in sources:
                conn.execute(
                    "INSERT INTO memory_fts (path, content) VALUES (?, ?)",
                    (path_str, content),
                )
                indexed += 1
            conn.commit()
        except Exception:
            logger.warning("FTS rebuild failed", exc_info=True)
            if self._memory_version == 2:
                if conn is not None:
                    conn.rollback()
                raise
        finally:
            if conn is not None:
                conn.close()
        return indexed if self._memory_version == 2 else len(files)

    @named_store_operation
    def index_row_count(self) -> int | None:
        """Rows in the FTS index, or ``None`` when the index cannot be read.

        Lets a caller tell three states apart that :meth:`search` collapses into
        one empty list: the index is unreadable, the index is empty, or the query
        genuinely has no match. Without this, a corrupt or not-yet-built index
        reports as "you never wrote about this", which is the one answer a memory
        search must never give wrongly.
        """
        require_memory_ready(self._memory_store_name)
        conn = None
        try:
            conn = self._get_db()
            row = conn.execute("SELECT count(*) FROM memory_fts").fetchone()
            return int(row[0]) if row else 0
        except Exception:
            logger.debug("FTS index count failed", exc_info=True)
            return None
        finally:
            if conn is not None:
                conn.close()

    @named_store_operation
    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Search memory for the literal words in ``query``.

        Returns ``[{path, snippet, rank}]``. The query is treated as literal
        text, not FTS5 expression syntax, because callers pass words a user
        typed: a ticket id, a filename, a hyphenated term. Unescaped, ``-``
        ``.`` and a bare ``AND`` are FTS5 syntax, so ``PROJ-123`` raises inside
        the driver and the ``except`` below turns it into ``[]`` -- a silent
        "you never wrote about this" for one of the likeliest queries.
        """
        require_memory_ready(self._memory_store_name)
        conn = None
        try:
            # Inside the try, not around it: this method handles its own errors
            # and returns [], so a timer wrapping the whole call would record
            # every failure as a success. Here a raising query is tagged
            # outcome=error before the except below swallows it.
            with timed_query("memory", "search"):
                match = _fts5_literal_query(query)
                if not match:
                    return []
                conn = self._get_db()
                cursor = conn.execute(
                    "SELECT path, snippet(memory_fts, 1, '>>>', '<<<', '...', 32), rank "
                    "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                )
                results = [
                    {"path": row[0], "snippet": row[1], "rank": row[2]} for row in cursor.fetchall()
                ]
            return results
        except Exception:
            logger.debug("FTS search failed", exc_info=True)
            return []
        finally:
            if conn is not None:
                conn.close()
