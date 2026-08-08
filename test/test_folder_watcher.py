"""Tests for FolderWatcher and folder source integration."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge import folder_watcher
from kiro_crew.knowledge.connectors.local_folder import LocalFolderConnector
from kiro_crew.knowledge.folder_watcher import MAX_SCAN_ATTEMPTS, FolderWatcher
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def vault(tmp_path):
    """Create a mock vault directory with some files."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "note1.md").write_text("# Hello\nThis is note 1")
    (vault_dir / "note2.md").write_text("# World\nThis is note 2")
    (vault_dir / "sub").mkdir()
    (vault_dir / "sub" / "deep.md").write_text("# Deep\nNested note")
    (vault_dir / ".git").mkdir()
    (vault_dir / ".git" / "config").write_text("gitconfig")
    (vault_dir / "image.png").write_bytes(b"\x89PNG")
    return vault_dir


@pytest.fixture()
def pipeline():
    p = MagicMock()
    p.ingest_file = AsyncMock(return_value="job123")
    return p


class TestLocalFolderConnector:
    def test_validate_valid_dir(self, tmp_path):
        c = LocalFolderConnector()
        valid, err = c.validate_config({"url": str(tmp_path)})
        assert valid is True
        assert err == ""

    def test_validate_nonexistent(self):
        c = LocalFolderConnector()
        valid, err = c.validate_config({"url": "/nonexistent/path"})
        assert valid is False
        assert "does not exist" in err

    def test_validate_file_not_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hi")
        c = LocalFolderConnector()
        valid, err = c.validate_config({"url": str(f)})
        assert valid is False
        assert "not a directory" in err

    def test_validate_empty_path(self):
        c = LocalFolderConnector()
        valid, err = c.validate_config({"url": ""})
        assert valid is False


class TestFolderWatcherWalk:
    def test_discovers_supported_files(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), [], set())
        paths = [p for p, _ in files]
        assert any("note1.md" in p for p in paths)
        assert any("note2.md" in p for p in paths)
        assert any("deep.md" in p for p in paths)

    def test_skips_git_dir(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), [], set())
        paths = [p for p, _ in files]
        assert not any(".git" in p for p in paths)

    def test_skips_unsupported_extensions(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), [], set())
        paths = [p for p, _ in files]
        assert not any("image.png" in p for p in paths)

    def test_respects_ignore_patterns(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), ["sub/*"], set())
        paths = [p for p, _ in files]
        assert not any("deep.md" in p for p in paths)

    def test_extra_skip_dirs(self, store, pipeline, vault):
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian" / "config.json").write_text("{}")
        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), [], {".obsidian"})
        paths = [p for p, _ in files]
        assert not any(".obsidian" in p for p in paths)

    def test_skips_os_temp_and_junk_files(self, store, pipeline, tmp_path):
        """OS-generated temp/lock/junk files are excluded even with supported names."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "real.md").write_text("# Real doc")
        # Junk that would otherwise pass the extension allowlist:
        (vault / "._real.docx").write_bytes(b"AppleDouble")  # macOS resource fork
        (vault / "~$real.docx").write_bytes(b"lock")         # Office owner/lock file
        (vault / ".DS_Store").write_bytes(b"\x00\x00")       # macOS Finder metadata (uppercase)
        (vault / "draft.tmp").write_text("temp")
        (vault / "note.md.swp").write_bytes(b"swap")         # vim swap
        (vault / "backup.md~").write_text("backup")          # editor backup
        (vault / "download.part").write_bytes(b"partial")    # incomplete download

        fw = FolderWatcher(store, pipeline)
        files = fw._walk(str(vault), [], set())
        names = {Path(p).name for p, _ in files}

        assert "real.md" in names
        for junk in ("._real.docx", "~$real.docx", ".DS_Store", "draft.tmp",
                     "note.md.swp", "backup.md~", "download.part"):
            assert junk not in names, f"{junk} should be ignored by default"

    def test_discovers_pdf_files(self, store, pipeline, vault):
        # Part 1: PDFs must be walked, not silently skipped. Before .pdf joined
        # FileReader.SUPPORTED the extension filter dropped folder PDFs.
        (vault / "report.pdf").write_bytes(b"%PDF-1.4 minimal")
        fw = FolderWatcher(store, pipeline)
        paths = [p for p, _ in fw._walk(str(vault), [], set())]
        assert any(p.endswith("report.pdf") for p in paths)

    def test_pdf_in_supported_set(self):
        from kiro_crew.knowledge.readers import FileReader
        assert ".pdf" in FileReader.SUPPORTED

    def test_discovers_org_files(self, store, pipeline, vault):
        """Org-mode (.org) files must be walked by the folder watcher."""
        (vault / "notes.org").write_text("* TODO Buy milk\nPlain org content")
        fw = FolderWatcher(store, pipeline)
        suffixes = {Path(p).suffix for p, _ in fw._walk(str(vault), [], set())}
        assert ".org" in suffixes

    def test_org_in_supported_set(self):
        from kiro_crew.knowledge.readers import FileReader
        assert ".org" in FileReader.SUPPORTED

    def test_org_file_reader_returns_text(self, tmp_path):
        from kiro_crew.knowledge.readers import FileReader
        org_file = tmp_path / "example.org"
        org_file.write_text("#+TITLE: Test\n* Heading\nBody text")
        reader = FileReader()
        text, meta = reader.read(str(org_file))
        assert "Body text" in text
        assert meta["extension"] == ".org"


#: Real-world capitalisation, so the case-insensitive basename match is exercised.
LOCK_FILES = (
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lockb", "bun.lock", "poetry.lock", "uv.lock", "Pipfile.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "packages.lock.json",
    "gradle.lockfile", "flake.lock",
)


class TestGeneratedArtifactDefaults:
    """Generated build output and dependency locks must not reach the embedder.

    They ingest fine, which is what makes them expensive: each is regenerated on
    every build, so a sweep re-chunks them and pays an extraction call per chunk
    for content that answers no question.
    """

    def _names(self, store, pipeline, root):
        fw = FolderWatcher(store, pipeline)
        return {Path(p).name for p, _ in fw._walk(str(root), [], set())}

    def _cdk_tree(self, tmp_path):
        root = tmp_path / "repo"
        (root / "cdk.out" / "asset.9f3c").mkdir(parents=True)
        (root / "cdk.out" / "tree.json").write_text('{"tree": {}}')
        (root / "cdk.out" / "asset.9f3c" / "manifest.json").write_text("{}")
        (root / "notes.md").write_text("# real doc")
        return root

    def _lock_tree(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "notes.md").write_text("# real doc")
        for name in LOCK_FILES:
            (root / name).write_text("generated resolution output")
        return root

    def test_cdk_out_is_pruned(self, store, pipeline, tmp_path):
        root = self._cdk_tree(tmp_path)
        names = self._names(store, pipeline, root)
        assert "notes.md" in names
        assert "tree.json" not in names
        assert "manifest.json" not in names

    def test_cdk_out_entry_is_load_bearing(self, store, pipeline, tmp_path, monkeypatch):
        # Nothing else keeps synth output out: the name only CONTAINS a dot so the
        # dot-prefix rule skips it, the bare "out" entry does not match it, and
        # .json is reader-supported.
        monkeypatch.setattr(folder_watcher, "HARD_SKIP_DIRS",
                            folder_watcher.HARD_SKIP_DIRS - {"cdk.out"})
        names = self._names(store, pipeline, self._cdk_tree(tmp_path))
        assert "tree.json" in names
        assert "manifest.json" in names

    def test_dependency_lock_files_are_never_discovered(self, store, pipeline, tmp_path):
        names = self._names(store, pipeline, self._lock_tree(tmp_path))
        assert "notes.md" in names
        assert names.isdisjoint(LOCK_FILES)

    def test_lock_files_stay_out_when_their_extension_is_readable(
            self, store, pipeline, tmp_path, monkeypatch):
        # ``.lock``/``.lockb``/``.lockfile`` are not reader-supported today, so the
        # extension filter would mask a missing glob. Widening SUPPORTED isolates
        # the globs as the thing under test.
        monkeypatch.setattr(FileReader, "SUPPORTED",
                            FileReader.SUPPORTED | {".lock", ".lockb", ".lockfile"})
        names = self._names(store, pipeline, self._lock_tree(tmp_path))
        assert "notes.md" in names
        assert names.isdisjoint(LOCK_FILES)

    def test_lock_globs_are_load_bearing(self, store, pipeline, tmp_path, monkeypatch):
        lock_globs = {n.lower() for n in LOCK_FILES}
        monkeypatch.setattr(FileReader, "SUPPORTED",
                            FileReader.SUPPORTED | {".lock", ".lockb", ".lockfile"})
        monkeypatch.setattr(folder_watcher, "DEFAULT_IGNORE_GLOBS", tuple(
            g for g in folder_watcher.DEFAULT_IGNORE_GLOBS if g not in lock_globs))
        names = self._names(store, pipeline, self._lock_tree(tmp_path))
        assert set(LOCK_FILES) <= names

    def test_every_lock_glob_is_registered_lowercase(self):
        # The match runs against a lowercased basename, so an entry carrying any
        # uppercase character can never fire.
        for name in LOCK_FILES:
            assert name.lower() in folder_watcher.DEFAULT_IGNORE_GLOBS, name


class TestFolderWatcherScan:
    @pytest.mark.asyncio
    async def test_new_files_ingested(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        # Mock ingest to create items
        async def fake_ingest(path, **kwargs):
            store.add_item("title", "content", "doc", source_id=source_id)
            return "job1"
        pipeline.ingest_file = fake_ingest

        stats = await fw.scan_source(source)
        assert stats["new"] == 3  # note1, note2, sub/deep
        assert stats["deleted"] == 0

    @pytest.mark.asyncio
    async def test_unchanged_files_skipped(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        # First scan
        await fw.scan_source(source)

        # Second scan — nothing changed
        stats = await fw.scan_source(source)
        assert stats["new"] == 0
        assert stats["changed"] == 0

    @pytest.mark.asyncio
    async def test_deleted_file_archived(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        await fw.scan_source(source)

        # Delete a file
        (vault / "note1.md").unlink()

        stats = await fw.scan_source(source)
        assert stats["deleted"] == 1

    @pytest.mark.asyncio
    async def test_max_files_cap(self, store, pipeline, tmp_path):
        vault = tmp_path / "big_vault"
        vault.mkdir()
        for i in range(10):
            (vault / f"note{i}.md").write_text(f"Note {i}")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": json.dumps({"max_files": 5})}

        stats = await fw.scan_source(source)
        assert stats["new"] == 5
        assert stats["capped"] == 5

    @pytest.mark.asyncio
    async def test_changed_file_reingested(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        await fw.scan_source(source)

        # Modify a file with explicit future mtime (avoids 1s granularity flakiness)
        import os
        (vault / "note1.md").write_text("# Updated content")
        os.utime(vault / "note1.md", (9999999999, 9999999999))

        stats = await fw.scan_source(source)
        assert stats["changed"] == 1

    @pytest.mark.asyncio
    async def test_junk_files_create_no_state_rows(self, store, pipeline, tmp_path):
        """Default-ignored junk files never enter folder_file_state, so a source
        is not left permanently stalled below 100% (the AppleDouble stall bug)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "doc1.md").write_text("# Doc 1")
        (vault / "doc2.md").write_text("# Doc 2")
        (vault / "._doc1.md").write_bytes(b"AppleDouble")  # would otherwise fail + stall
        (vault / "~$doc2.docx").write_bytes(b"lock")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        async def fake_ingest(path, **kwargs):
            store.add_item("title", "content", "doc", source_id=source_id)
            return "job1"
        pipeline.ingest_file = fake_ingest

        stats = await fw.scan_source(source)
        assert stats["new"] == 2
        assert stats["skipped"] == 0
        assert stats["failed"] == 0

        rows = store.db.execute(
            "SELECT file_path FROM folder_file_state WHERE source_id = ?", (source_id,)).fetchall()
        tracked = {Path(r["file_path"]).name for r in rows}
        assert tracked == {"doc1.md", "doc2.md"}


class TestFolderFileStateTable:
    def test_table_exists(self, store):
        tables = {r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "folder_file_state" in tables

    def test_insert_and_query(self, store):
        source_id = store.add_source("test", "local_folder", "/tmp/test")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, item_ids, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, "/tmp/test/a.md", "abc123", 1000.0, '["item1"]', "2026-01-01"))
        store.db.commit()
        row = store.db.execute(
            "SELECT * FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, "/tmp/test/a.md")).fetchone()
        assert row is not None
        assert row["content_hash"] == "abc123"
        assert json.loads(row["item_ids"]) == ["item1"]


class TestFolderFileStateSchema:
    """Test that status and error_message columns exist."""

    def test_status_column_exists(self, store):
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(folder_file_state)").fetchall()}
        assert "status" in cols

    def test_error_message_column_exists(self, store):
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(folder_file_state)").fetchall()}
        assert "error_message" in cols

    def test_status_default_is_pending(self, store):
        source_id = store.add_source("test", "local_folder", "/tmp/test")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen) VALUES (?, ?, ?)",
            (source_id, "/tmp/test/a.md", "2026-01-01"))
        store.db.commit()
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, "/tmp/test/a.md")).fetchone()
        assert row["status"] == "pending"


class TestFolderWatcherPause:
    """Test pause stops scan mid-way."""

    @pytest.mark.asyncio
    async def test_pause_stops_scan(self, store, pipeline, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        for i in range(5):
            (vault / f"note{i}.md").write_text(f"Note {i}")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault),
                                     properties={"scan_paused": True})
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        stats = await fw.scan_source(source)
        assert stats["status"] == "paused"
        assert stats["new"] == 0  # Nothing processed

    @pytest.mark.asyncio
    async def test_resume_after_pause(self, store, pipeline, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        for i in range(3):
            (vault / f"note{i}.md").write_text(f"Note {i}")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault),
                                     properties={})
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        stats = await fw.scan_source(source)
        assert stats["new"] == 3
        assert "status" not in stats  # no status key means completed (not paused)


class TestFolderWatcherCrashRecovery:
    """Test that files left as 'scanning' are re-processed on next scan."""

    @pytest.mark.asyncio
    async def test_scanning_files_reprocessed(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))

        # Simulate a crash: insert a file as 'scanning' (interrupted)
        note1_path = str(vault / "note1.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, note1_path, "", 0, "[]", "2026-01-01", "scanning"))
        store.db.commit()

        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}
        stats = await fw.scan_source(source)

        # note1 should be re-processed (counted as new since it had no valid state)
        # note2 and sub/deep are new
        assert stats["new"] >= 2  # at least note2 + deep are new

        # Verify note1 is now 'done' (or at least not 'scanning')
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, note1_path)).fetchone()
        assert row["status"] != "scanning"


class TestFolderWatcherSkippedFiles:
    """Test that skipped files are not re-processed."""

    @pytest.mark.asyncio
    async def test_skipped_files_ignored(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))

        # Mark note1 as skipped
        note1_path = str(vault / "note1.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, note1_path, "abc", 9999999999.0, "[]", "2026-01-01", "skipped"))
        store.db.commit()

        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}
        stats = await fw.scan_source(source)

        # note1 should be skipped, only note2 + deep are new
        assert stats["skipped"] == 1
        assert stats["new"] == 2


class TestSandboxGuard:
    """Test is_sensitive_path rejection at add-time."""

    def test_sensitive_path_rejected(self):
        """Verify is_sensitive_path blocks known sensitive paths."""
        from kiro_crew.security import is_sensitive_path
        home = str(Path.home())
        assert is_sensitive_path(f"{home}/.ssh/id_rsa")
        assert is_sensitive_path(f"{home}/.aws/credentials")
        assert is_sensitive_path("~/.ssh/id_rsa")

    def test_normal_path_allowed(self):
        """Verify normal paths are not blocked."""
        from kiro_crew.security import is_sensitive_path
        assert not is_sensitive_path("/tmp/test/vault")
        assert not is_sensitive_path("/tmp/notes/readme.md")


class TestFolderWatcherFailedFiles:
    """Test that failed ingestion records error and doesn't block scan."""

    @pytest.mark.asyncio
    async def test_failed_ingestion_records_error(self, store, pipeline, vault):
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        # Make pipeline raise on ingest
        pipeline.ingest_file = AsyncMock(side_effect=RuntimeError("parse error"))

        stats = await fw.scan_source(source)
        assert stats["failed"] == 3  # all 3 files fail

        # Check error recorded in state
        rows = store.db.execute(
            "SELECT status, error_message FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchall()
        for row in rows:
            assert row["status"] == "failed"
            assert "parse error" in row["error_message"]


class TestHashFileSecurity:
    """Test _hash_file resolves symlinks and blocks sensitive paths."""

    def test_hash_normal_file(self, store, pipeline, tmp_path):
        f = tmp_path / "normal.md"
        f.write_text("hello")
        fw = FolderWatcher(store, pipeline)
        result = fw._hash_file(str(f))
        assert result is not None
        assert len(result) == 64  # sha256 hex

    def test_hash_sensitive_path_returns_none(self, store, pipeline, tmp_path):
        """Symlink to sensitive path should return None."""
        from unittest.mock import patch
        fw = FolderWatcher(store, pipeline)
        f = tmp_path / "normal.md"
        f.write_text("hello")
        with patch("kiro_crew.knowledge.folder_watcher.is_sensitive_path", return_value=True):
            result = fw._hash_file(str(f))
        assert result is None

    def test_hash_nonexistent_returns_none(self, store, pipeline):
        fw = FolderWatcher(store, pipeline)
        result = fw._hash_file("/nonexistent/file.md")
        assert result is None


class TestIngestFileSecurity:
    """Test _ingest_file TOCTOU protection."""

    @pytest.mark.asyncio
    async def test_ingest_blocks_sensitive_path(self, store, pipeline, tmp_path):
        """If path resolves to sensitive location at ingest time, return None."""
        from unittest.mock import patch
        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(tmp_path))
        f = tmp_path / "note.md"
        f.write_text("content")

        with patch("kiro_crew.knowledge.folder_watcher.is_sensitive_path", return_value=True):
            result = await fw._ingest_file(str(f), source_id, "default", {}, [])

        assert result == (None, "failed")
        # Should record as failed
        row = store.db.execute(
            "SELECT status, error_message FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, str(f))).fetchone()
        assert row["status"] == "failed"
        assert "sensitive path" in row["error_message"]


class TestPerSourceLock:
    """Test concurrent scan serialization."""

    @pytest.mark.asyncio
    async def test_lock_created_per_source(self, store, pipeline, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("a")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        await fw.scan_source(source)
        assert source_id in fw._locks

    @pytest.mark.asyncio
    async def test_concurrent_scans_serialized(self, store, pipeline, tmp_path):
        """Two concurrent scans of same source should not interleave."""
        import asyncio
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("a")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        # Run two scans concurrently — both should complete without error
        results = await asyncio.gather(
            fw.scan_source(source),
            fw.scan_source(source),
        )
        assert all("error" not in r for r in results)


class TestOrphanCleanupExclusion:
    """Test that folder sources aren't orphan-cleaned."""

    def test_source_with_file_state_not_orphaned(self, store):
        source_id = store.add_source("test", "local_folder", "/tmp/vault")
        # Add a folder_file_state row (simulates scan in progress)
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status) VALUES (?, ?, ?, ?)",
            (source_id, "/tmp/vault/a.md", "2026-01-01", "done"))
        store.db.commit()

        # Run migration which includes orphan cleanup
        store._migrate()

        # Source should still exist
        row = store.db.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
        assert row is not None


class TestAsyncToThread:
    """Test that _walk and _hash_file are offloaded to thread."""

    @pytest.mark.asyncio
    async def test_walk_runs_in_thread(self, store, pipeline, tmp_path):
        """Verify scan completes (implicitly tests asyncio.to_thread works)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text("hello")

        fw = FolderWatcher(store, pipeline)
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder", "properties": "{}"}

        stats = await fw.scan_source(source)
        assert stats["new"] == 1


class TestDeleteSourceCascade:
    """Verify delete_source_cascade cleans folder_file_state (FK constraint)."""

    def test_delete_folder_source_with_file_state(self, store):
        sid = store.add_source("folder", "local_folder", "/tmp/test")
        store.add_item("chunk1", "content", "doc", source_id=sid)
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, "/tmp/test/file.md", "abc", 1.0, "2024-01-01", "done"))
        store.db.commit()

        store.delete_source_cascade(sid)

        assert store.db.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (sid,)).fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM folder_file_state WHERE source_id = ?", (sid,)).fetchone()[0] == 0


class TestIngestTimeConfinement:
    """A symlink retargeted after the walk must not pull in an outside file."""

    @pytest.mark.asyncio
    async def test_a_path_that_escapes_the_root_is_refused_at_ingest_time(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("# secret-ish but not a sensitive path\n")
        link = root / "doc.md"
        link.symlink_to(outside)

        store = KnowledgeStore(str(tmp_path / "k.db"))
        try:
            src = store.add_source(name="project", source_type="local_folder",
                                   uri=str(root))
            watcher = FolderWatcher(store, MagicMock())
            # The pipeline must never be reached for an escaping path.
            watcher.pipeline = MagicMock()
            watcher.pipeline.ingest_file = AsyncMock(
                side_effect=AssertionError("ingested a file outside the root"))

            item_ids, outcome = await watcher._ingest_file(
                str(link), src, "default", {"confine_to_root": True}, [],
                root=str(root))

            assert outcome == "failed"
            assert item_ids is None
            watcher.pipeline.ingest_file.assert_not_awaited()
        finally:
            store.db.close()


class TestFailedIngestLeavesNoRetryLoop:
    """A file whose ingest fails must reach a terminal status, not keep the marker.

    ``scanning`` matches no skip gate, so a row that keeps it is re-chunked and
    re-extracted -- at full model cost -- on every sweep of the watcher.
    """

    @pytest.mark.asyncio
    async def test_failed_ingest_is_terminal_and_skipped_next_sweep(
            self, store, pipeline, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "bad.md").write_text("# Bad")
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        fw = FolderWatcher(store, pipeline)

        # A failure path that returns the documented (None, "failed") without
        # persisting anything itself: _do_scan owns the state transition.
        calls = []

        async def failing_ingest(file_path, *a, **kw):
            calls.append(file_path)
            return None, "failed"
        fw._ingest_file = failing_ingest

        stats = await fw.scan_source(source)
        assert stats["failed"] == 1
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchone()
        assert row["status"] == "failed"

        stats2 = await fw.scan_source(source)
        assert stats2["skipped"] == 1
        assert stats2["failed"] == 0
        assert len(calls) == 1, "a failed file must not be re-ingested"

    @pytest.mark.asyncio
    async def test_failed_row_keeps_the_version_that_failed(
            self, store, pipeline, tmp_path):
        """The failed row keeps the content hash / mtime the marker carried, and the
        reason recorded by the ingest path, so the failure is attributable."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "bad.md").write_text("# Bad")
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}

        async def boom(path, **kw):
            raise RuntimeError("reader exploded")
        pipeline.ingest_file = boom

        fw = FolderWatcher(store, pipeline)
        await fw.scan_source(source)

        row = store.db.execute(
            "SELECT status, content_hash, mtime, error_message FROM folder_file_state "
            "WHERE source_id = ?", (source_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["content_hash"], "the hash of the version that failed is retained"
        assert row["mtime"] > 0
        assert "reader exploded" in (row["error_message"] or "")


class TestInterruptedScanRetryCap:
    """Crash recovery retries a 'scanning' row, but not without bound."""

    @pytest.mark.asyncio
    async def test_interrupted_row_is_retried_then_retired(
            self, store, pipeline, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "hangs.md").write_text("# Hangs")
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        calls = []

        async def interrupted(path, **kw):
            calls.append(path)
            # CancelledError is a BaseException: it escapes the scan without any
            # terminal write, which is exactly how a real interruption looks.
            raise asyncio.CancelledError()
        pipeline.ingest_file = interrupted

        fw = FolderWatcher(store, pipeline)
        for _ in range(MAX_SCAN_ATTEMPTS + 3):
            with contextlib.suppress(asyncio.CancelledError):
                await fw.scan_source(source)

        assert len(calls) == MAX_SCAN_ATTEMPTS, (
            f"expected at most {MAX_SCAN_ATTEMPTS} billed attempts, got {len(calls)}")
        row = store.db.execute(
            "SELECT status, attempts, error_message FROM folder_file_state "
            "WHERE source_id = ?", (source_id,)).fetchone()
        assert row["status"] == "failed"
        # Retirement is a terminal write, so it clears the count like every other
        # one. What holds the file out of later sweeps is the 'failed' status.
        assert row["attempts"] == 0
        assert str(MAX_SCAN_ATTEMPTS) in (row["error_message"] or "")

    @pytest.mark.asyncio
    async def test_row_already_stuck_scanning_converges(
            self, store, pipeline, tmp_path):
        """An install that already carries a stuck row spends the same budget and
        retires it, rather than re-ingesting it on every sweep indefinitely."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "stuck.md").write_text("# Stuck")
        source_id = store.add_source("test", "local_folder", str(vault))
        stuck = str(vault / "stuck.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, "
            "item_ids, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, stuck, "oldhash", 1.0, "[]", "2026-01-01", "scanning"))
        store.db.commit()
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        calls = []

        async def interrupted(path, **kw):
            calls.append(path)
            raise asyncio.CancelledError()
        pipeline.ingest_file = interrupted

        fw = FolderWatcher(store, pipeline)
        for _ in range(MAX_SCAN_ATTEMPTS + 3):
            with contextlib.suppress(asyncio.CancelledError):
                await fw.scan_source(source)

        assert len(calls) == MAX_SCAN_ATTEMPTS
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, stuck)).fetchone()
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_successful_ingest_clears_the_retry_budget(
            self, store, pipeline, tmp_path):
        """A completed ingest resets attempts, so a transient interruption does not
        permanently eat a file's budget."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "ok.md").write_text("# Ok")
        source_id = store.add_source("test", "local_folder", str(vault))
        ok = str(vault / "ok.md")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, "
            "item_ids, last_seen, status, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, ok, "oldhash", 1.0, "[]", "2026-01-01", "scanning",
             MAX_SCAN_ATTEMPTS - 1))
        store.db.commit()
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}

        async def fake_ingest(path, **kw):
            store.add_item("title", "content", "doc", source_id=source_id)
            return "job1"
        pipeline.ingest_file = fake_ingest

        fw = FolderWatcher(store, pipeline)
        await fw.scan_source(source)

        row = store.db.execute(
            "SELECT status, attempts FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?", (source_id, ok)).fetchone()
        assert row["status"] == "done"
        assert row["attempts"] == 0

    @pytest.mark.asyncio
    async def test_editing_a_capped_file_resets_its_budget_and_ingests(
            self, store, pipeline, tmp_path):
        """The budget belongs to the version that failed, not to the path.

        A file the user edits after the cap was spent is new content that has never
        been attempted, so it must be ingested rather than retired on the strength of
        a retirement its previous version earned.
        """
        import os
        vault = tmp_path / "vault"
        vault.mkdir()
        doc = vault / "edited.md"
        doc.write_text("# Original")
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        calls = []

        async def interrupted(path, **kw):
            calls.append(path)
            raise asyncio.CancelledError()
        pipeline.ingest_file = interrupted

        fw = FolderWatcher(store, pipeline)
        for _ in range(MAX_SCAN_ATTEMPTS):
            with contextlib.suppress(asyncio.CancelledError):
                await fw.scan_source(source)
        row = store.db.execute(
            "SELECT status, attempts FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchone()
        assert (row["status"], row["attempts"]) == ("scanning", MAX_SCAN_ATTEMPTS)

        # The user edits the file. Explicit future mtime avoids 1s granularity.
        doc.write_text("# Rewritten after the cap was spent")
        os.utime(doc, (9999999999, 9999999999))

        async def fake_ingest(path, **kw):
            store.add_item("title", "content", "doc", source_id=source_id)
            return "job1"
        pipeline.ingest_file = fake_ingest

        await fw.scan_source(source)

        row = store.db.execute(
            "SELECT status, attempts FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchone()
        assert row["status"] == "done", "edited content must be ingested, not retired"
        assert row["attempts"] == 0

    @pytest.mark.asyncio
    async def test_an_unchanged_capped_file_stays_retired(
            self, store, pipeline, tmp_path):
        """The reset is keyed on the hash, so an untouched capped file still retires."""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "same.md").write_text("# Unchanged")
        source_id = store.add_source("test", "local_folder", str(vault))
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        calls = []

        async def interrupted(path, **kw):
            calls.append(path)
            raise asyncio.CancelledError()
        pipeline.ingest_file = interrupted

        fw = FolderWatcher(store, pipeline)
        for _ in range(MAX_SCAN_ATTEMPTS + 3):
            with contextlib.suppress(asyncio.CancelledError):
                await fw.scan_source(source)

        assert len(calls) == MAX_SCAN_ATTEMPTS
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchone()
        assert row["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_retried_capped_file_gets_a_working_budget(
            self, store, pipeline, tmp_path):
        """Clearing the status the way the dashboard's Retry does hands the file a
        full budget again.

        Retry clears ``status`` and ``error_message`` and nothing else, so a
        retirement that left the spent count on the row would put the file back into
        the scan already over budget: one interrupted attempt and the next sweep
        retires it again, making Retry a no-op the user cannot escape.
        """
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "capped.md").write_text("# Capped")
        source_id = store.add_source("test", "local_folder", str(vault))
        capped = str(vault / "capped.md")
        source = {"id": source_id, "uri": str(vault), "source_type": "local_folder",
                  "properties": "{}"}
        calls = []

        async def interrupted(path, **kw):
            calls.append(path)
            raise asyncio.CancelledError()
        pipeline.ingest_file = interrupted

        fw = FolderWatcher(store, pipeline)
        for _ in range(MAX_SCAN_ATTEMPTS + 1):
            with contextlib.suppress(asyncio.CancelledError):
                await fw.scan_source(source)
        row = store.db.execute(
            "SELECT status FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, capped)).fetchone()
        assert row["status"] == "failed", "precondition: the cap has retired the file"
        billed_when_capped = len(calls)

        # Exactly what POST /api/knowledge/sources/{id}/files/retry writes.
        store.db.execute(
            "UPDATE folder_file_state SET status = 'pending', error_message = NULL "
            "WHERE source_id = ? AND file_path = ?", (source_id, capped))
        store.db.commit()

        with contextlib.suppress(asyncio.CancelledError):
            await fw.scan_source(source)
        row = store.db.execute(
            "SELECT status, attempts FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?", (source_id, capped)).fetchone()
        assert len(calls) == billed_when_capped + 1, "the retry is actually attempted"
        assert (row["status"], row["attempts"]) == ("scanning", 1), (
            "the retry starts from a fresh budget, not from the spent one")

        # The budget is genuinely spendable: the sweep after the interruption retries
        # rather than retiring on a count inherited from before the retry.
        with contextlib.suppress(asyncio.CancelledError):
            await fw.scan_source(source)
        row = store.db.execute(
            "SELECT status, attempts FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?", (source_id, capped)).fetchone()
        assert len(calls) == billed_when_capped + 2
        assert (row["status"], row["attempts"]) == ("scanning", 2)


class TestFolderFileStateAttemptsMigration:
    def test_migration_adds_attempts_to_a_preexisting_db(self, tmp_path):
        """A database created before the retry cap gains the column, keeps its rows,
        and starts them on a full budget."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE folder_file_state (
                source_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_hash TEXT,
                text_hash TEXT,
                mtime REAL,
                item_ids TEXT DEFAULT '[]',
                last_seen TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                merged_into_source_id TEXT,
                PRIMARY KEY (source_id, file_path)
            )
        """)
        conn.execute(
            "INSERT INTO folder_file_state (source_id, file_path, content_hash, mtime, "
            "item_ids, last_seen, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("src-legacy", "/legacy/doc.md", "h", 1.0, "[]", "2026-01-01", "scanning"))
        conn.commit()
        conn.close()

        store = KnowledgeStore(str(db_path))
        try:
            cols = {r[1] for r in store.db.execute(
                "PRAGMA table_info(folder_file_state)").fetchall()}
            assert "attempts" in cols
            row = store.db.execute(
                "SELECT status, attempts FROM folder_file_state WHERE file_path = ?",
                ("/legacy/doc.md",)).fetchone()
            assert row["status"] == "scanning"
            assert row["attempts"] == 0
        finally:
            store.close()
