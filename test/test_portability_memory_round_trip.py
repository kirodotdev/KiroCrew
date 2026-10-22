"""What an export-then-import cycle keeps of a crew's memory.

The full round trip with semantic and episodic memory, the named-store generations a
merge import preserves, the read-only SQLite backup the export takes, and the
memory-only bundle's export, manifest validation and import.

Export, validation and the merge and replace imports are pinned in
``test_portability.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import sqlite3
import tempfile
import tracemalloc
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import portability_helpers
import pytest

from kiro_crew import platform_compat, portability, portability_export, snapshot
from kiro_crew.portability import apply_import_zip, create_export_zip, validate_import_zip

# Fixtures shared with the sibling modules, bound by assignment so pytest registers
# them for the tests in this module.
fake_kirocrew_home = portability_helpers.fake_kirocrew_home
patched_config_dir = portability_helpers.patched_config_dir


# ── Round-Trip Tests ──


class TestRoundTrip:
    """Verify export→import→export produces consistent state."""

    def test_full_round_trip(self, patched_config_dir, tmp_path):
        """Export from instance A, import to empty B, export from B — manifests should match."""
        # Export from A
        zip_bytes_a, manifest_a = create_export_zip()

        # Import to B
        target = tmp_path / "instance_b"
        target.mkdir()
        zip_path = tmp_path / "export_a.zip"
        zip_path.write_bytes(zip_bytes_a)

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                apply_import_zip(zip_path, mode="replace")

        # Export from B
        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                _, manifest_b = create_export_zip()

        # Content counts should match
        assert (
            manifest_b["contents"]["workspace_files"] == manifest_a["contents"]["workspace_files"]
        )
        assert manifest_b["contents"]["skill_count"] == manifest_a["contents"]["skill_count"]

    def test_export_import_preserves_semantic_memory(self, patched_config_dir, tmp_path):
        """Semantic memory entries survive a full export→import cycle."""
        zip_bytes, _ = create_export_zip()

        target = tmp_path / "target"
        target.mkdir()
        zip_path = tmp_path / "export.zip"
        zip_path.write_bytes(zip_bytes)

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                apply_import_zip(zip_path, mode="replace")

        # Verify semantic memory
        conn = sqlite3.connect(str(target / "memory.db"))
        rows = conn.execute("SELECT key, value_json FROM semantic_memory").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "user.name"
        assert json.loads(rows[0][1]) == "Alice"

    def test_export_import_preserves_episodic_memory(self, patched_config_dir, tmp_path):
        """Episodic memory entries survive a full export→import cycle."""
        zip_bytes, _ = create_export_zip()

        target = tmp_path / "target"
        target.mkdir()
        zip_path = tmp_path / "export.zip"
        zip_path.write_bytes(zip_bytes)

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                apply_import_zip(zip_path, mode="replace")

        conn = sqlite3.connect(str(target / "memory.db"))
        rows = conn.execute("SELECT id, text FROM episodic_memories").fetchall()
        conn.close()
        assert len(rows) == 1
        assert "deployment" in rows[0][1]


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("per_file_copy", [False, True])
def test_import_merge_preserves_store_generations(tmp_path, monkeypatch, existing, per_file_copy):
    from test_snapshot_memory_stores import (
        _per_file_store_merge,
        _seed_store_generation,
        _store_bytes,
    )

    from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME
    from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(source))
    for name in ("acme", "other"):
        _seed_store_generation(source, name, 2)
    archive = tmp_path / "export.zip"
    archive.write_bytes(create_export_zip()[0])
    monkeypatch.setenv("KIROCREW_HOME", str(destination))
    kept = destination / MEMORY_STORES_DIR_NAME / "acme"
    if existing:
        _seed_store_generation(destination, "acme", 1)
    before = _store_bytes(kept)
    if per_file_copy:
        monkeypatch.setattr(portability, "_merge_named_stores", _per_file_store_merge)
    summary = apply_import_zip(archive, mode="merge")
    if existing:
        if per_file_copy:
            assert _store_bytes(kept) != before
            assert (kept / "memory/projects.md").read_text() == "source generation"
        else:
            assert _store_bytes(kept) == before
            assert not (kept / "memory/projects.md").exists()
            assert any("acme (kept the existing store;" in item for item in summary["items"])
        with contextlib.closing(VectorMemoryStore(db_path=kept / "memory.db")) as store:
            store.init()
            assert store.algorithm_version == "v1"
    else:
        with contextlib.closing(
            open_member_database(kept / "memory.db", member_id="acme", store_id="acme")
        ) as store:
            assert store.algorithm_version == "v2"
    for name in ("acme", "other"):
        directory = destination / MEMORY_STORES_DIR_NAME / name
        assert not (directory / "member-memory.json").exists()
        assert not (directory / "memory_index.db").exists()
        if name == "other" or not existing:
            assert (directory / "memory/preferences.md").read_text(
                encoding="utf-8"
            ) == "generation 2"
            assert (directory / "memory/projects.md").read_text(
                encoding="utf-8"
            ) == "source generation"
    with contextlib.closing(
        open_member_database(
            destination / MEMORY_STORES_DIR_NAME / "other" / "memory.db",
            member_id="other",
            store_id="other",
        )
    ) as store:
        assert store.algorithm_version == "v2"


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize(
    "filename",
    [
        "memory#.db",
        pytest.param(
            "memory?#.db",
            marks=pytest.mark.skipif(
                platform_compat.IS_WINDOWS, reason="Windows forbids '?' in filenames"
            ),
        ),
    ],
)
def test_database_export_opens_read_only(tmp_path, exists, filename):
    source = tmp_path / filename
    if exists:
        with contextlib.closing(sqlite3.connect(str(source))) as db:
            db.execute("CREATE TABLE sample (value TEXT)")
            db.execute("INSERT INTO sample VALUES ('kept')")
            db.commit()
        before = source.read_bytes()
        output = io.BytesIO()
        portability._backup_sqlite(source, output)
        assert source.read_bytes() == before
        restored = tmp_path / "restored.db"
        restored.write_bytes(output.getvalue())
        with contextlib.closing(sqlite3.connect(str(restored))) as db:
            assert db.execute("SELECT value FROM sample").fetchall() == [("kept",)]
    else:
        with pytest.raises(portability.sqlite3.OperationalError):
            portability._backup_sqlite(source, io.BytesIO())
        assert not source.exists()
        # A read-write SQLite open creates this missing source instead of refusing it.
        with contextlib.closing(sqlite3.connect(str(source))):
            pass
        assert source.exists()


# ── Database members stream ──

#: Big enough that one whole-database buffer dwarfs the copy buffer, small enough
#: to build in well under a second. Random blobs, so deflate cannot shrink the
#: member and hide a whole-member write behind compression.
_BIG_DB_BYTES = 6 * 1024 * 1024


def _grow_memory_db(mc: Path) -> int:
    rng = random.Random(23234)
    with contextlib.closing(sqlite3.connect(str(mc / "memory.db"))) as db:
        db.execute("CREATE TABLE bulk (payload BLOB)")
        db.executemany(
            "INSERT INTO bulk VALUES (?)",
            ((rng.randbytes(4096),) for _ in range(_BIG_DB_BYTES // 4096)),
        )
        db.commit()
    return (mc / "memory.db").stat().st_size


class TestDatabaseMembersStream:
    """A SQLite member is backed up to a temp file and streamed into the archive,
    so neither the archive nor the database is ever held whole in memory."""

    def test_a_large_database_streams_in_bounded_memory(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        db_size = _grow_memory_db(patched_config_dir)
        # A small copy chunk, so the sink-write bound below is about the chunk and
        # not the database.
        monkeypatch.setattr(portability_export, "_SQLITE_COPY_CHUNK_BYTES", 64 * 1024)
        write_bound = 1024 * 1024
        spool = tmp_path / "spool"
        spool.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(spool))
        out_path = tmp_path / "export.zip"
        writes: list[int] = []

        plan = portability.plan_export()
        with open(out_path, "wb") as out:

            def sink(chunk: bytes) -> None:
                writes.append(len(chunk))
                out.write(chunk)

            tracemalloc.start()
            try:
                manifest = portability.stream_export_zip(sink, plan)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        # A database read whole and written in one piece peaks at several times
        # its size and hands the sink one write about as large as itself. The
        # streamed peak is a few copy chunks plus the zip writer's and the
        # platform's own buffers (about 0.4 MiB on Linux, 1.4 MiB on Windows),
        # so it is held to a third of the database: well clear of either
        # platform's streamed peak, far below the buffered one.
        assert db_size > 6 * write_bound
        assert peak < db_size // 3, (peak, db_size)
        assert max(writes) < write_bound, (max(writes), db_size)
        assert len(writes) > 4
        # The temp file the backup went through is gone.
        assert list(spool.iterdir()) == []

        # Same contents: the member is a complete database the importer accepts.
        ok, error, _ = validate_import_zip(out_path)
        assert ok, error
        with zipfile.ZipFile(out_path) as zf:
            (name,) = [n for n in zf.namelist() if n.endswith("/memory.db")]
            assert zf.getinfo(name).file_size == manifest["contents"]["memory.db"]
            restored = tmp_path / "restored.db"
            restored.write_bytes(zf.read(name))
        with contextlib.closing(sqlite3.connect(str(restored))) as db:
            assert db.execute("SELECT count(*) FROM bulk").fetchone() == (_BIG_DB_BYTES // 4096,)
            assert db.execute("SELECT value_json FROM semantic_memory").fetchall() == [('"Alice"',)]

    def test_the_streamed_member_matches_a_buffered_backup(self, patched_config_dir, tmp_path):
        _grow_memory_db(patched_config_dir)
        whole = io.BytesIO()
        size = portability._backup_sqlite(patched_config_dir / "memory.db", whole)
        assert size == len(whole.getvalue())

        zip_bytes, _ = create_export_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            (name,) = [n for n in zf.namelist() if n.endswith("/memory.db")]
            assert zf.read(name) == whole.getvalue()
            # The member keeps the mode ``writestr`` gave it.
            assert (zf.getinfo(name).external_attr >> 16) & 0o777 == 0o600

    def test_a_failed_copy_removes_the_temp_file(self, patched_config_dir, tmp_path, monkeypatch):
        spool = tmp_path / "spool"
        spool.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(spool))

        class _Broken(io.BufferedIOBase):
            def writable(self) -> bool:
                return True

            def write(self, b) -> int:
                raise OSError("client went away")

        with pytest.raises(OSError, match="client went away"):
            portability._backup_sqlite(patched_config_dir / "memory.db", _Broken())
        assert list(spool.iterdir()) == []


# ── Memory-only export and import ──


def _rest_names(zip_bytes: bytes) -> set[str]:
    """Archive member names below the export prefix."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return {"/".join(PurePosixPath(n).parts[1:]) for n in zf.namelist() if not n.endswith("/")}


class TestMemoryOnly:
    def test_memory_only_export_carries_memory_and_nothing_else(self, patched_config_dir):
        zip_bytes, manifest = create_export_zip(memory_only=True, include_sessions=True)
        names = _rest_names(zip_bytes)

        assert manifest["components"] == ["memory"]
        assert {"memory.db", "memory_index.db", "MANIFEST.json"} <= names
        assert "workspace/memory/preferences.md" in names
        assert "workspace/memory/history/2026-05-17.md" in names
        for other in ("config.json", "hooks.json", "crons.json", "notifications.jsonl"):
            assert other not in names
        assert not any(n.startswith(("skills/", "plan_memory/", "sessions/")) for n in names)
        assert "session_count" not in manifest["contents"]
        assert manifest["contents"]["workspace_files"] == 4

    def test_memory_only_export_leaves_other_workspace_files_out(self, patched_config_dir):
        (patched_config_dir / "workspace" / "notes.md").write_text("not memory")
        knowledge = patched_config_dir / "workspace" / "knowledge"
        knowledge.mkdir()
        (knowledge / "doc.md").write_text("a knowledge file")

        names = _rest_names(create_export_zip(memory_only=True)[0])

        assert "workspace/knowledge/doc.md" in names
        assert "workspace/notes.md" not in names

    def test_full_export_declares_no_components(self, patched_config_dir):
        _, manifest = create_export_zip()
        assert "components" not in manifest

    def test_a_memory_bundle_is_outside_the_whole_install_version_range(
        self, patched_config_dir, tmp_path
    ):
        """A reader that only knows whole-install archives refuses a memory bundle."""
        zip_bytes, manifest = create_export_zip(memory_only=True)
        assert manifest["version"] == portability.MEMORY_EXPORT_MANIFEST_VERSION
        assert manifest["version"] > snapshot.EXPORT_MANIFEST_VERSION

        bundle = tmp_path / "mem.zip"
        bundle.write_bytes(zip_bytes)
        ok, error, read = validate_import_zip(bundle)
        assert ok, error
        assert read["components"] == ["memory"]

    def test_the_memory_bundle_version_without_the_declaration_is_refused(self, tmp_path):
        bundle = tmp_path / "odd.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            version = portability.MEMORY_EXPORT_MANIFEST_VERSION
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": version}))
        ok, error, _ = validate_import_zip(bundle)
        assert not ok
        assert "Unsupported manifest version" in error

    def test_a_deeply_nested_manifest_reads_as_unparsable(self, tmp_path):
        bundle = tmp_path / "deep.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("snap/MANIFEST.json", "[" * 100_000 + "]" * 100_000)
        with zipfile.ZipFile(bundle) as zf:
            assert portability._read_member_json(zf, "snap/MANIFEST.json") is None

    def test_memory_only_import_merges_memory_and_nothing_else(self, patched_config_dir, tmp_path):
        """A full archive imported memory-only leaves config, crons, hooks and skills alone."""
        zip_path = tmp_path / "full.zip"
        zip_path.write_bytes(create_export_zip()[0])
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = apply_import_zip(zip_path, mode="merge", memory_only=True)

        assert summary["components"] == ["memory"]
        assert (target / "memory.db").is_file()
        assert (target / "workspace" / "memory" / "preferences.md").is_file()
        for other in ("config.json", "hooks.json", "crons.json", "notifications.jsonl"):
            assert not (target / other).exists(), other
        assert not (target / "skills").exists()
        assert not (target / "plan_memory").exists()

    def test_an_archive_declaring_memory_only_imports_only_memory(
        self, patched_config_dir, tmp_path
    ):
        """The manifest's own declaration is honoured even without the flag."""
        zip_path = tmp_path / "mem.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2, "components": ["memory"]}))
            zf.writestr("snap/workspace/memory/preferences.md", "memory")
            zf.writestr("snap/crons.json", json.dumps({"jobs": []}))
            zf.writestr("snap/workspace/notes.md", "not memory")
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = apply_import_zip(zip_path, mode="merge")

        assert summary["components"] == ["memory"]
        assert (target / "workspace" / "memory" / "preferences.md").read_text() == "memory"
        assert not (target / "crons.json").exists()
        assert not (target / "workspace" / "notes.md").exists()

    def test_only_a_nested_manifest_is_no_manifest(self, patched_config_dir, tmp_path):
        """A MANIFEST.json below the top is content: the validator finds no manifest."""
        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            version = portability.MEMORY_EXPORT_MANIFEST_VERSION
            manifest = {"version": version, "components": ["memory"]}
            zf.writestr("snap/meta/MANIFEST.json", json.dumps(manifest))
            zf.writestr("snap/hooks.json", json.dumps({"hooks": []}))
            zf.writestr("snap/workspace/memory/preferences.md", "memory")
        ok, error, _ = validate_import_zip(zip_path)
        assert not ok
        assert "No MANIFEST.json" in error
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = apply_import_zip(zip_path, mode="merge")

        # The nested file declares nothing for the import, which stays whole-install.
        assert "components" not in summary
        assert (target / "hooks.json").is_file()

    @pytest.mark.parametrize("flag", [True, False])
    def test_memory_only_refuses_replace(self, patched_config_dir, tmp_path, flag):
        """Refused by the flag, and by a memory bundle's own manifest when the flag is off."""
        zip_path = tmp_path / "mem.zip"
        zip_path.write_bytes(create_export_zip(memory_only=True)[0])
        target = tmp_path / "target"
        target.mkdir()
        (target / "config.json").write_text("{}")

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                with pytest.raises(ValueError, match="merge only"):
                    apply_import_zip(zip_path, mode="replace", memory_only=flag)

        assert (target / "config.json").read_text() == "{}"

    @pytest.mark.parametrize(
        "name", ["C:/MANIFEST.json", "c:snap/hooks.json", "/snap/x", "snap/../x"]
    )
    def test_the_validator_refuses_names_an_extractor_can_move(self, tmp_path, name):
        bundle = tmp_path / "odd.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
            zf.writestr(name, "x")
        ok, error, _ = validate_import_zip(bundle)
        assert not ok
        assert "Rejected path traversal" in error

    @pytest.mark.parametrize(
        ("name", "separates", "unsafe"),
        [
            ("snap\\..\\hooks.json", True, True),
            ("C:\\snap\\x", True, True),
            ("\\snap\\x", True, True),
            ("snap/a\\b.md", True, False),
            ("snap\\..\\hooks.json", False, False),
            ("snap/a\\b.md", False, False),
        ],
    )
    def test_a_backslash_is_a_separator_only_where_the_host_says_so(self, name, separates, unsafe):
        assert portability._unsafe_member_name(name, backslash_separates=separates) is unsafe

    @pytest.mark.skipif(os.sep == "\\", reason="a backslash is a separator on Windows")
    def test_an_own_backup_with_a_backslash_file_name_still_validates(self, tmp_path):
        bundle = tmp_path / "own.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
            zf.writestr("snap/workspace/a\\b.md", "x")
        ok, error, _ = validate_import_zip(bundle)
        assert ok, error

    def test_the_validator_refuses_an_oversized_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(portability, "_MAX_SETTINGS_DOCUMENT_BYTES", 64)
        bundle = tmp_path / "big.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2, "pad": "x" * 100}))
        ok, error, _ = validate_import_zip(bundle)
        assert not ok
        assert "too large" in error

    @pytest.mark.parametrize("nested", ["large", "declares-memory"])
    def test_a_workspace_file_named_manifest_is_ordinary_content(
        self, patched_config_dir, tmp_path, monkeypatch, nested
    ):
        """A whole-install backup still imports in full whatever a nested MANIFEST.json says."""
        monkeypatch.setattr(portability, "_MAX_SETTINGS_DOCUMENT_BYTES", 256)
        if nested == "large":
            body = "[" + "1," * 500 + "1]"
        else:
            version = portability.MEMORY_EXPORT_MANIFEST_VERSION
            body = json.dumps({"version": version, "components": ["memory"]})
        zip_path = tmp_path / "nested.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
            zf.writestr("snap/workspace/proj/MANIFEST.json", body)
            zf.writestr("snap/workspace/notes.md", "not memory")
            zf.writestr("snap/workspace/memory/preferences.md", "memory")
        ok, error, _ = validate_import_zip(zip_path)
        assert ok, error
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                summary = apply_import_zip(zip_path, mode="merge")

        assert "components" not in summary
        assert (target / "workspace" / "notes.md").read_text() == "not memory"
        assert (target / "workspace" / "proj" / "MANIFEST.json").read_text() == body

    def test_a_manifest_within_the_export_budget_still_imports(self, patched_config_dir, tmp_path):
        """A many-chat backup's manifest runs past 1 MiB; the reader shares the export's budget."""
        times = {f"snap/sessions/chat-{i:06d}.jsonl": 1.7e9 + i for i in range(40_000)}
        manifest = {"version": 2, "session_mtimes": times}
        raw = json.dumps(manifest, indent=2).encode()
        assert 1024 * 1024 < len(raw) < portability._MAX_SETTINGS_DOCUMENT_BYTES
        bundle = tmp_path / "many-chats.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("snap/MANIFEST.json", raw)
            zf.writestr("snap/workspace/memory/preferences.md", "memory")
        ok, error, _ = validate_import_zip(bundle)
        assert ok, error
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                apply_import_zip(bundle, mode="merge")

        assert (target / "workspace" / "memory" / "preferences.md").is_file()

    def test_two_top_manifests_are_refused(self, patched_config_dir, tmp_path):
        """With two at the top no single manifest describes the archive."""
        zip_path = tmp_path / "dupes.zip"
        version = portability.MEMORY_EXPORT_MANIFEST_VERSION
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # zipfile warns on a duplicate name
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(
                    "snap/MANIFEST.json", json.dumps({"version": version, "components": ["memory"]})
                )
                zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
                zf.writestr("snap/hooks.json", json.dumps({"hooks": []}))
        ok, error, _ = validate_import_zip(zip_path)
        assert not ok
        assert "More than one" in error
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                with pytest.raises(ValueError, match="More than one"):
                    apply_import_zip(zip_path, mode="merge")

        assert not (target / "hooks.json").exists()

    def test_nested_manifest_names_are_never_read(self, patched_config_dir, tmp_path, monkeypatch):
        """Many nested copies of the name cost nothing: only the top manifest is read."""
        zip_path = tmp_path / "many.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("snap/MANIFEST.json", json.dumps({"version": 2}))
                for _ in range(20):
                    zf.writestr("snap/workspace/x/MANIFEST.json", json.dumps({"version": 2}))
        reads: list[str] = []
        real_read = zipfile.ZipFile.read

        def counting_read(self, member, pwd=None):
            info = member if isinstance(member, zipfile.ZipInfo) else self.getinfo(member)
            reads.append(info.filename)
            return real_read(self, member, pwd)

        monkeypatch.setattr(zipfile.ZipFile, "read", counting_read)
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                apply_import_zip(zip_path, mode="merge")

        assert "snap/workspace/x/MANIFEST.json" not in reads

    @staticmethod
    def _drive_bundle(path: Path) -> None:
        """A memory bundle whose names carry a drive, as Windows zipfile would strip it."""
        with zipfile.ZipFile(path, "w") as zf:
            version = portability.MEMORY_EXPORT_MANIFEST_VERSION
            manifest = {"version": version, "components": ["memory"]}
            zf.writestr("C:/MANIFEST.json", json.dumps(manifest))
            hooks = {"hooks": [{"event": "prompt", "command": "echo pwned"}]}
            zf.writestr("C:/memory_stores/hooks.json", json.dumps(hooks))

    def test_a_drive_prefixed_member_never_lands_outside_memory(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """On Windows extraction drops ``C:``; the hook must still not be installed."""
        import ntpath

        monkeypatch.setattr(zipfile.os.path, "splitdrive", ntpath.splitdrive)
        zip_path = tmp_path / "drive.zip"
        self._drive_bundle(zip_path)
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                with pytest.raises(ValueError):
                    apply_import_zip(zip_path, mode="merge")

        assert not (target / "hooks.json").exists()

    def test_a_member_moved_out_of_its_prefix_is_refused_after_extraction(
        self, patched_config_dir, tmp_path, monkeypatch
    ):
        """The post-extract check holds even for a name form the lexical check misses."""
        real_extract = zipfile.ZipFile.extract

        def extract_dropping_prefix(self, member, path=None, pwd=None):
            # Stands in for any extractor that strips a name's first part.
            info = self.getinfo(member) if isinstance(member, str) else member
            rest = "/".join(PurePosixPath(info.filename).parts[1:])
            if not rest:
                return real_extract(self, member, path, pwd)
            dest = Path(path) / rest
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.read(info))
            return str(dest)

        monkeypatch.setattr(zipfile.ZipFile, "extract", extract_dropping_prefix)
        zip_path = tmp_path / "moved.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            version = portability.MEMORY_EXPORT_MANIFEST_VERSION
            manifest = {"version": version, "components": ["memory"]}
            zf.writestr("snap/MANIFEST.json", json.dumps(manifest))
            zf.writestr("snap/memory_stores/hooks.json", json.dumps({"hooks": []}))
        target = tmp_path / "target"
        target.mkdir()

        with patch("kiro_crew.portability.config_dir", return_value=target):
            with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
                with pytest.raises(ValueError, match="left its archive prefix"):
                    apply_import_zip(zip_path, mode="merge")

        assert not (target / "hooks.json").exists()

    @pytest.mark.parametrize(
        ("rest", "kept"),
        [
            ((), True),
            (("MANIFEST.json",), True),
            (("memory.db",), True),
            (("memory_index.db",), True),
            (("workspace", "memory", "a.md"), True),
            (("workspace", "knowledge", "b.md"), True),
            (("memory_stores", "acme", "memory.db"), True),
            (("config.json",), False),
            (("workspace", "notes.md"), False),
            (("workspace", "memoryx", "a.md"), False),
            (("skills", "x", "SKILL.md"), False),
            (("sessions", "chat.jsonl"), False),
        ],
    )
    def test_is_memory_member(self, rest, kept):
        assert portability._is_memory_member(rest) is kept
