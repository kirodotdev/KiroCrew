"""FolderWatcher -- recursive directory scanner for folder-type knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

from .dedup import dedup_document
from .readers import FileReader

logger = logging.getLogger(__name__)

# Build-output dirs are excluded because a rebuild deletes/recreates hundreds of
# hashed chunk files at once; indexing that churn drives a bulk delete pass whose
# per-file graph reload runs on the event loop and can stall it past the loop
# watchdog (dist/assets/*.js was the motivating case). Dot-dirs (.cache, .next,
# .venv) are already pruned separately by the startswith(".") rule in _walk.
HARD_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "dist", "build", "out", "target"}
DEFAULT_MAX_FILES = 5000

# Extra skip dirs per source type
SOURCE_TYPE_SKIP_DIRS: dict[str, set[str]] = {
    "obsidian_vault": {".obsidian", ".trash"},
}

# OS-generated temp / lock / junk files that are never real documents. Matched
# case-insensitively against the file basename for every folder source type, in
# addition to any per-source ignore_patterns. Without this, files that carry an
# otherwise-supported extension are discovered, fail ingestion, and (since failed
# files are never auto-retried) leave a source permanently stalled below 100%.
# The motivating case: macOS AppleDouble sidecars (``._<name>.docx``).
DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "._*",            # macOS AppleDouble resource forks
    ".ds_store",      # macOS Finder metadata
    "~$*",            # Microsoft Office lock / owner files
    ".~lock.*",       # LibreOffice lock files
    "thumbs.db",      # Windows thumbnail cache
    "desktop.ini",    # Windows folder settings
    "*.tmp",          # generic temp files
    "*.temp",
    "*.swp",          # vim swap files
    "*.swo",
    "*~",             # editor backup files
    ".#*",            # emacs lock files
    "*.crdownload",   # incomplete browser downloads
    "*.part",
    "*.partial",
)


class FolderWatcher:
    """Recursively scans a directory source, tracks file state, triggers ingestion."""

    def __init__(self, store, pipeline):
        self.store = store
        self.pipeline = pipeline
        self._locks: dict[str, asyncio.Lock] = {}

    async def scan_source(self, source: dict) -> dict:
        """Scan a folder source. Returns {new, changed, deleted, skipped, capped}."""
        source_id = source["id"]
        if source_id not in self._locks:
            self._locks[source_id] = asyncio.Lock()
        async with self._locks[source_id]:
            return await self._do_scan(source)

    async def _do_scan(self, source: dict) -> dict:
        source_id = source["id"]
        uri = source["uri"]
        props = json.loads(source.get("properties") or "{}") if isinstance(
            source.get("properties"), str) else (source.get("properties") or {})

        if not Path(uri).is_dir():
            logger.warning("Folder source %s path missing: %s", source_id, uri)
            return {"error": f"Directory not found: {uri}"}

        max_files = props.get("max_files", DEFAULT_MAX_FILES)
        ignore_patterns = props.get("ignore_patterns", [])
        source_type = source.get("source_type", "local_folder")
        extra_skip_dirs = SOURCE_TYPE_SKIP_DIRS.get(source_type, set())

        # 1. Discover files
        discovered = await asyncio.to_thread(self._walk, uri, ignore_patterns, extra_skip_dirs)

        # Capture all discovered paths before cap (for accurate deletion detection)
        all_discovered_paths = {fp for fp, _ in discovered}

        # 2. Enforce cap (newest first)
        capped = 0
        if len(discovered) > max_files:
            discovered.sort(key=lambda f: f[1], reverse=True)  # sort by mtime desc
            capped = len(discovered) - max_files
            discovered = discovered[:max_files]
            logger.warning("Source %s: capped at %d files (%d skipped)", source_id, max_files, capped)

        # 3. Load existing state
        existing = self._load_state(source_id)
        now = datetime.now().isoformat()

        stats: dict[str, int] = {"new": 0, "changed": 0, "deleted": 0, "skipped": 0, "capped": capped, "failed": 0}
        ingested_paths: list[str] = []  # files (re)ingested this scan, for targeted dedup

        # 4. Detect deleted files (use full set, not capped set, to avoid false deletions)
        for file_path in list(existing.keys()):
            if file_path not in all_discovered_paths:
                await self._handle_deleted(source_id, file_path, existing[file_path])
                stats["deleted"] += 1

        # 5. Process discovered files
        namespace = props.get("namespace", "default")
        for file_path, mtime in discovered:
            # Check pause between files
            if self._is_paused(source_id):
                return {**stats, "status": "paused"}

            state = existing.get(file_path)

            # Skip files marked skipped/failed (user must retry) or deduped (already
            # collapsed into another source's copy -- don't re-ingest the on-disk file).
            if state and state.get("status") in ("skipped", "failed", "deduped"):
                stats["skipped"] += 1
                continue

            if state and state.get("status") == "done" and mtime <= state.get("mtime", 0):
                # Unchanged — just update last_seen
                self._update_last_seen(source_id, file_path, now)
                continue

            # mtime changed or new/interrupted file — check content hash
            content_hash = await asyncio.to_thread(self._hash_file, file_path)
            if not content_hash:
                stats["skipped"] += 1
                continue

            if state and state.get("status") == "done" and content_hash == state.get("content_hash"):
                # Touched but content unchanged
                self._update_state(source_id, file_path, content_hash, mtime, state.get("item_ids", "[]"), now, "done", commit=False)
                continue

            # New or changed file — ingest
            if state and state.get("status") == "done":
                old_ids = json.loads(state.get("item_ids", "[]"))
                stats["changed"] += 1
            else:
                old_ids = json.loads(state.get("item_ids", "[]")) if state else []
                if not state:
                    stats["new"] += 1

            # Mark scanning before processing (crash recovery: scanning = interrupted)
            self._update_state(source_id, file_path, content_hash, mtime, json.dumps(old_ids), now, "scanning")

            item_ids = await self._ingest_file(file_path, source_id, namespace, props, old_ids)
            if item_ids is None:
                # Ingestion failed
                stats["failed"] += 1
            else:
                self._update_state(source_id, file_path, content_hash, mtime, json.dumps(item_ids), now, "done", commit=False)
                ingested_paths.append(file_path)

        self.store.db.commit()  # Batch commit for all non-crash-recovery updates
        # Targeted cross-source dedup for each newly ingested/changed file, so a folder
        # copy collapses any matching one-shot upload. O(k*n) over the k changed files
        # rather than a full O(n^2) corpus sweep (knowledge/dedup.py).
        if getattr(self.pipeline, "_dedup_enabled", True):
            for fpath in ingested_paths:
                try:
                    dedup_document(self.store, source_id, file_path=fpath, apply=True)
                except Exception:
                    logger.debug("Per-file dedup skipped for %s", fpath, exc_info=True)
        return stats

    def _walk(self, root: str, ignore_patterns: list[str], extra_skip_dirs: set[str]) -> list[tuple[str, float]]:
        """Walk directory, return [(file_path, mtime)] for supported files."""
        supported = FileReader.SUPPORTED
        results = []
        skip_dirs = HARD_SKIP_DIRS | extra_skip_dirs

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place.
            # Windows: the `.`-prefix hidden check is POSIX-centric — Windows marks
            # hidden via the NTFS hidden attribute, not a dotfile name, so some
            # dirs that are hidden on Windows aren't pruned (benign over-ingestion).
            # Tracked in Mesh-2364 (https://taskei.amazon.dev/tasks/Mesh-2364).
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]

            rel_dir = os.path.relpath(dirpath, root)
            for fname in filenames:
                # Skip OS-generated temp/lock/junk files (basename, case-insensitive)
                if any(fnmatch(fname.lower(), pat) for pat in DEFAULT_IGNORE_GLOBS):
                    continue
                rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
                # Check ignore patterns
                if any(fnmatch(rel_path, pat) for pat in ignore_patterns):
                    continue
                # Extension filter
                ext = Path(fname).suffix.lower()
                if ext not in supported and ext != ".canvas":
                    continue
                full_path = os.path.join(dirpath, fname)
                resolved = str(Path(full_path).resolve())
                if is_sensitive_path(resolved):
                    continue
                try:
                    mtime = os.stat(full_path).st_mtime
                except OSError:
                    continue
                results.append((full_path, mtime))

        return results

    def _load_state(self, source_id: str) -> dict[str, dict]:
        """Load folder_file_state rows for this source."""
        rows = self.store.db.execute(
            "SELECT file_path, content_hash, mtime, item_ids, last_seen, status, error_message FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchall()
        return {r["file_path"]: dict(r) for r in rows}

    def _update_state(self, source_id: str, file_path: str, content_hash: str, mtime: float, item_ids: str, now: str, status: str = "done", error_message: str | None = None, *, commit: bool = True):
        self.store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, content_hash, mtime, item_ids, last_seen, status, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, file_path, content_hash, mtime, item_ids, now, status, error_message))
        if commit:
            self.store.db.commit()

    def _update_last_seen(self, source_id: str, file_path: str, now: str):
        self.store.db.execute(
            "UPDATE folder_file_state SET last_seen = ? WHERE source_id = ? AND file_path = ?",
            (now, source_id, file_path))

    def _is_paused(self, source_id: str) -> bool:
        """Check if source has scan_paused flag set."""
        row = self.store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        props = row["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                return False
        return bool((props or {}).get("scan_paused"))

    async def _handle_deleted(self, source_id: str, file_path: str, state: dict):
        """Archive items for a deleted file and remove state row."""
        item_ids = json.loads(state.get("item_ids", "[]"))
        if item_ids:
            self.store.delete_items_batch(item_ids)
        self.store.db.execute(
            "DELETE FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, file_path))
        self.store.db.commit()
        logger.info("Deleted file removed: %s (%d items)", file_path, len(item_ids))

    async def _ingest_file(self, file_path: str, source_id: str, namespace: str, props: dict, old_item_ids: list[str]) -> list[str] | None:
        """Ingest a single file and return list of created item IDs, or None on failure."""
        # Defense-in-depth: re-resolve symlinks before reading (TOCTOU protection)
        resolved = str(Path(file_path).resolve())
        if is_sensitive_path(resolved):
            logger.warning("TOCTOU: sensitive path blocked at ingest time: %s -> %s", file_path, resolved)
            sel().log_tool_invocation(
                session_key="watcher", agent="folder-watcher",
                tool_name="knowledge.source.file.ingest_denied",
                outcome="denied",
                resources=f"source_id={source_id} file_path={file_path} reason=sensitive_path_toctou",
            )
            now = datetime.now().isoformat()
            self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", "sensitive path blocked")
            return None
        try:
            before_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

            await self.pipeline.ingest_file(
                file_path, source_id=source_id, namespace=namespace,
                original_name=Path(file_path).name,
                old_item_ids=old_item_ids)

            # Detect partial failure (pipeline rolls back but doesn't raise)
            row = self.store.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row and row["sync_status"] == "error":
                now = datetime.now().isoformat()
                self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", "partial ingestion failure")
                return None

            after_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

            return list(after_ids - before_ids)
        except Exception as e:
            logger.exception("Failed to ingest %s", file_path)
            now = datetime.now().isoformat()
            self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", str(e)[:500])
            return None

    @staticmethod
    def _hash_file(path: str) -> str | None:
        try:
            resolved = str(Path(path).resolve())
            if is_sensitive_path(resolved):
                return None
            h = hashlib.sha256()
            with open(resolved, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (OSError, PermissionError):
            return None
