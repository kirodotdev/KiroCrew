"""Two promises that were kept for one of their reasons only."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import pinned_fs
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


class TestMergeNeverOverwrites:
    """`merge` adds what is missing. Exclusive creation is the promise, not a hint.

    Retargeted for M1: the merge no longer has a `copy2` + `os.link`/`os.replace` publish
    with a separate no-hard-link fallback -- `_merge_exclusive_copy` is gone. Every file
    now goes through `pinned_fs.copy_file_pinned(..., skip_existing=True)`, whose
    destination is created `O_CREAT|O_EXCL|O_NOFOLLOW`, so "it did not exist" and "this
    call created it" are one statement and there is no window to race.
    """

    def _tree(self, tmp_path: Path) -> tuple[Path, Path]:
        src = tmp_path / "from-backup"
        dst = tmp_path / "home"
        (src / "workspace").mkdir(parents=True)
        (dst / "workspace").mkdir(parents=True)
        (src / "workspace" / "notes.md").write_text("FROM THE BACKUP\n", encoding="utf-8")
        return src, dst

    def test_a_missing_file_is_merged_in(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert (dst / "workspace" / "notes.md").read_text(encoding="utf-8") == ("FROM THE BACKUP\n")

    def test_an_existing_file_is_left_alone(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        target = dst / "workspace" / "notes.md"
        target.write_text("THE OPERATOR'S WORK\n", encoding="utf-8")
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert target.read_text(encoding="utf-8") == "THE OPERATOR'S WORK\n"

    def test_a_target_that_appears_before_the_write_is_still_not_overwritten(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The window between deciding and writing is the whole finding.

        The old `os.replace`/`os.link` publish could destroy whatever arrived in that
        window. The exclusive create refuses it instead. This drives the real primitive:
        `copy_file_pinned` is wrapped so the target is created by a racing writer AFTER
        the merge decides to copy it but BEFORE the exclusive open runs -- the winner's
        bytes must survive.
        """
        src, dst = self._tree(tmp_path)
        target = dst / "workspace" / "notes.md"
        real = snap.pinned_fs.copy_file_pinned

        def create_then_copy(*a, **kw):
            # Someone else publishes the target in the pre-write window.
            if not target.exists():
                target.write_text("ARRIVED MID-MERGE\n", encoding="utf-8")
            return real(*a, **kw)

        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", create_then_copy)
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert target.read_text(encoding="utf-8") == "ARRIVED MID-MERGE\n", (
            "the exclusive create was not honoured -- a file that appeared before the "
            "write was overwritten"
        )

    def test_no_scratch_file_is_left_behind(self, tmp_path: Path) -> None:
        src, dst = self._tree(tmp_path)
        (dst / "workspace" / "notes.md").write_text("THE OPERATOR'S WORK\n", encoding="utf-8")
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        # Direct-to-final writes: no `.merge-*` or `.partial` remnant of the old publish.
        leftovers = [
            p.name
            for p in (dst / "workspace").iterdir()
            if p.name.startswith((".merge-", ".partial", ".")) and p.name != "notes.md"
        ]
        assert leftovers == [], leftovers

    def test_the_merge_write_is_exclusive_create_not_a_publish(self) -> None:
        code = "\n".join(
            line
            for line in inspect.getsource(snap._copy_tree_no_overwrite).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "skip_existing=True" in code, "the merge no longer requests the no-overwrite form"
        assert "os.replace" not in code, "an unconditional rename cannot keep no-overwrite"
        assert "os.link" not in code, "the exploitable link-publish design must not return"
        prim = inspect.getsource(pinned_fs.copy_file_pinned)
        assert "O_EXCL" in prim, "the destination is not created exclusively"


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
        assert "RedactionFailed" in src[idx : idx + 900]


def test_the_merge_primitive_uses_exclusive_creation() -> None:
    """The no-overwrite guarantee now lives in the shared primitive, not a merge helper.

    `_merge_exclusive_copy` was removed; `copy_file_pinned` creates the destination
    `O_CREAT|O_EXCL` and, on failure, empties the entry it holds a descriptor to rather
    than unlinking a name -- so a half-written target cannot masquerade as already-merged
    and cannot delete a concurrent writer's file.
    """
    src = inspect.getsource(pinned_fs.copy_file_pinned)
    assert "O_EXCL" in src
    assert "skip_existing" in src, "the no-overwrite mode is not offered by the primitive"
    assert os.O_EXCL  # the flag exists on every supported platform
