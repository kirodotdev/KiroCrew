"""Two promises that were kept for one of their reasons only."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


class TestMergeNeverOverwrites:
    """`merge` adds what is missing. The existence test is the promise, not a hint."""

    def _tree(self, tmp_path: Path) -> tuple[Path, Path]:
        src = tmp_path / "from-backup"
        dst = tmp_path / "home"
        (src / "workspace").mkdir(parents=True)
        (dst / "workspace").mkdir(parents=True)
        (src / "workspace" / "notes.md").write_text("FROM THE BACKUP\n", encoding="utf-8")
        return src, dst

    def test_a_missing_file_is_merged_in(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        snap._copy_tree_no_overwrite(src, dst)
        assert (dst / "workspace" / "notes.md").read_text(encoding="utf-8") == ("FROM THE BACKUP\n")

    def test_an_existing_file_is_left_alone(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        target = dst / "workspace" / "notes.md"
        target.write_text("THE OPERATOR'S WORK\n", encoding="utf-8")
        snap._copy_tree_no_overwrite(src, dst)
        assert target.read_text(encoding="utf-8") == "THE OPERATOR'S WORK\n"

    def test_a_file_that_appears_after_the_test_is_still_not_overwritten(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The window between deciding and writing is the whole finding.

        `os.replace` closes it by destroying whatever arrived. Creating the target
        exclusively refuses instead, which is what the mode promised.
        """
        src, dst = self._tree(tmp_path)
        target = dst / "workspace" / "notes.md"
        real_copy = snap.shutil.copy2

        def copy_then_race(a, b, *args, **kwargs):
            out = real_copy(a, b, *args, **kwargs)
            # Someone else creates the target while the staged copy is being written.
            if not target.exists():
                target.write_text("ARRIVED MID-MERGE\n", encoding="utf-8")
            return out

        monkeypatch.setattr(snap.shutil, "copy2", copy_then_race)
        snap._copy_tree_no_overwrite(src, dst)
        assert target.read_text(encoding="utf-8") == "ARRIVED MID-MERGE\n"

    def test_the_fallback_branch_also_refuses_through_the_merge(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Forced onto the no-hard-link path, the walk must keep the same promise.

        The target has to APPEAR mid-merge, not exist up front: an existing target is
        skipped by the check before the write, so the fallback branch would never run and
        a clobbering fallback would pass unnoticed.
        """
        src, dst = self._tree(tmp_path)
        target = dst / "workspace" / "notes.md"
        real_copy = snap.shutil.copy2

        def copy_then_race(a, b, *args, **kwargs):
            out = real_copy(a, b, *args, **kwargs)
            if not target.exists():
                target.write_text("ARRIVED MID-MERGE\n", encoding="utf-8")
            return out

        def no_links(*a, **k):
            raise OSError(1, "this filesystem does not support hard links")

        monkeypatch.setattr(snap.shutil, "copy2", copy_then_race)
        monkeypatch.setattr(snap.os, "link", no_links)
        snap._copy_tree_no_overwrite(src, dst)
        assert target.read_text(encoding="utf-8") == "ARRIVED MID-MERGE\n"

    def test_the_fallback_branch_still_merges_a_missing_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        src, dst = self._tree(tmp_path)

        def no_links(*a, **k):
            raise OSError(1, "this filesystem does not support hard links")

        monkeypatch.setattr(snap.os, "link", no_links)
        snap._copy_tree_no_overwrite(src, dst)
        assert (dst / "workspace" / "notes.md").read_text(encoding="utf-8") == ("FROM THE BACKUP\n")

    def test_no_staging_file_is_left_behind(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        (dst / "workspace" / "notes.md").write_text("THE OPERATOR'S WORK\n", encoding="utf-8")
        snap._copy_tree_no_overwrite(src, dst)
        assert not [p.name for p in (dst / "workspace").iterdir() if p.name.startswith(".merge-")]

    def test_the_fallback_also_refuses_an_existing_target(self, tmp_path: Path) -> None:
        """The no-hard-link filesystem must keep the same promise."""
        src = tmp_path / "a.txt"
        src.write_text("FROM THE BACKUP\n", encoding="utf-8")
        target = tmp_path / "b.txt"
        target.write_text("THE OPERATOR'S WORK\n", encoding="utf-8")
        assert snap._merge_exclusive_copy(src, target) is False
        assert target.read_text(encoding="utf-8") == "THE OPERATOR'S WORK\n"

    def test_the_fallback_writes_when_the_target_is_absent(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_text("FROM THE BACKUP\n", encoding="utf-8")
        target = tmp_path / "b.txt"
        assert snap._merge_exclusive_copy(src, target) is True
        assert target.read_text(encoding="utf-8") == "FROM THE BACKUP\n"

    def test_the_merge_write_is_not_an_unconditional_rename(self) -> None:
        src = inspect.getsource(snap._copy_tree_no_overwrite)
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert (
            "os.replace" not in code
        ), "an unconditional rename cannot keep a no-overwrite promise"
        assert "os.link" in code


class TestAnEmptiedBackupIsRefused:
    """Dropping the payload and uploading the rest reports success and restores nothing."""

    def _stage(self, tmp_path: Path) -> Path:
        stage = tmp_path / "bundle"
        stage.mkdir()
        (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
        (stage / "workspace").mkdir()
        (stage / "workspace" / "notes.md").write_text("real memory\n", encoding="utf-8")
        return stage

    def _unprovable_db(self, path: Path) -> None:
        """A credential in the SCHEMA -- rows can be rewritten, DDL cannot."""
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                f"CREATE TABLE semantic(id INTEGER PRIMARY KEY, value TEXT DEFAULT '{KEY}')"
            )
            conn.execute("INSERT INTO semantic(id) VALUES(1)")
            conn.commit()
        conn.close()

    @pytest.mark.parametrize("rel", sorted(redact._PRODUCT_DATABASES))
    def test_every_payload_database_refuses_rather_than_being_dropped(
        self, tmp_path: Path, rel: str
    ) -> None:
        stage = self._stage(tmp_path)
        db = stage / rel
        db.parent.mkdir(parents=True, exist_ok=True)
        self._unprovable_db(db)

        with pytest.raises(redact.PayloadDatabaseUnprovable) as e:
            redact.redact_bundle_for_egress(stage)

        assert rel in e.value.paths
        assert db.exists(), "the payload was deleted instead of the upload being refused"

    def test_the_operators_own_database_still_refuses_too(self, tmp_path: Path) -> None:
        stage = self._stage(tmp_path)
        db = stage / "workspace" / "their-own.db"
        self._unprovable_db(db)
        with pytest.raises(redact.OpaqueFilesPresent):
            redact.redact_bundle_for_egress(stage)
        assert db.exists()

    def test_files_dropped_by_name_are_still_dropped(self, tmp_path: Path) -> None:
        """Only pure-secret and rebuildable files are removed, and that still happens."""
        stage = self._stage(tmp_path)
        assert redact._DERIVED_INDEXES or redact._DROP_ENTIRELY
        rel = sorted(redact._DROP_ENTIRELY)[0]
        target = stage / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"secret-material")

        report = redact.redact_bundle_for_egress(stage)

        assert rel in report.dropped
        assert not target.exists()

    def test_a_payload_database_is_never_unlinked_by_the_pass(self) -> None:
        src = inspect.getsource(redact._redact_database)
        assert "unlink" not in src, "the pass must not delete a database it cannot prove clean"

    def test_the_upload_reports_the_refusal_instead_of_crashing(self) -> None:
        """A bare raise would surface as a traceback, indistinguishable from a crash."""
        src = inspect.getsource(snap)
        assert "PayloadDatabaseUnprovable" in src
        idx = src.index("PayloadDatabaseUnprovable as e")
        assert "_RedactionFailed" in src[idx : idx + 900]


def test_the_merge_helper_uses_exclusive_creation() -> None:
    src = inspect.getsource(snap._merge_exclusive_copy)
    assert "O_EXCL" in src
    assert "unlink" in src, "a half-written target would look already-merged forever"
    assert os.O_EXCL  # the flag exists on every supported platform
