"""A rollback is a round-trip, and an empty file is not a healthy database."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


class TestRecoveryRefusesToSaveATreeItCannotCopyWhole:
    """The rollback SAVE is `_backup_tree_or_refuse`, and it must refuse a link.

    Superseded contract: the old `_copytree_rollback` preserved a symlink so a
    save-then-restore round trip kept it. That helper is gone. Replace mode runs
    `rmtree` on the live tree AFTER the save, so a symlink the save cannot faithfully copy
    is a file the restore would delete with nothing to put back. `_backup_tree_or_refuse`
    therefore reports a skipped link as FATAL and REFUSES the whole save before any
    mutation, rather than making a partial backup. Refusing is the stronger guarantee, so
    this pins the refusal -- not the old preservation.
    """

    def test_a_tree_with_a_link_is_refused_rather_than_partially_backed_up(
        self, tmp_path: Path
    ) -> None:
        live = tmp_path / "home" / "workspace"
        (live / "sub").mkdir(parents=True)
        (live / "sub" / "real.md").write_text("mine\n", encoding="utf-8")
        (live / "sub" / "pointer.md").symlink_to(live / "sub" / "real.md")

        backup = tmp_path / "rollback"
        backup.mkdir(parents=True)
        saved = backup / "workspace"
        with pytest.raises(snap.pinned_fs.PinnedPathRefusal):
            snap._backup_tree_or_refuse(live, saved)

        # And the live tree is untouched -- the refusal arrives before any delete.
        assert (live / "sub" / "real.md").is_file()
        assert (live / "sub" / "pointer.md").is_symlink()

    def test_an_ordinary_tree_is_backed_up_faithfully(self, tmp_path: Path) -> None:
        """The refusal must not turn a link-free tree into a failure."""
        live = tmp_path / "home" / "workspace"
        (live / "sub").mkdir(parents=True)
        (live / "sub" / "real.md").write_text("mine\n", encoding="utf-8")

        backup = tmp_path / "rollback"
        backup.mkdir(parents=True)
        saved = backup / "workspace"
        snap._backup_tree_or_refuse(live, saved, allow_unpinned=bool(unpinnable_argv()))
        assert (saved / "sub" / "real.md").read_text(encoding="utf-8") == "mine\n"


class TestAnEmptyDatabaseIsRefused:
    """SQLite opens a zero-byte file as a valid empty database and calls it `ok`."""

    def test_sqlite_really_does_call_an_empty_file_healthy(self, tmp_path: Path) -> None:
        """The premise, pinned -- if this ever stops being true the guard can go."""
        empty = tmp_path / "empty.db"
        empty.touch()

        with snap.closing(snap.sqlite3.connect(str(empty))) as conn:
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"

    def test_a_zero_byte_product_database_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "memory.db"
        empty.touch()

        with pytest.raises(snap.SourceComponentUnsound) as caught:
            snap._refuse_unless_sound(empty, "memory.db", strict=True)

        message = str(caught.value)
        assert "EMPTY" in message
        assert "report success" in message, "the refusal does not say what it prevents"

    def test_a_real_database_still_passes(self, tmp_path: Path) -> None:
        good = tmp_path / "memory.db"
        with snap.closing(snap.sqlite3.connect(str(good))) as conn:
            conn.execute("CREATE TABLE t(x TEXT)")
            conn.commit()

        snap._refuse_unless_sound(good, "memory.db", strict=True)

    def test_the_size_check_precedes_the_open(self) -> None:
        import inspect

        src = inspect.getsource(snap._refuse_unless_sound)
        assert src.index("st_size == 0") < src.index(
            "sqlite3.connect("
        ), "opening first means the empty case is already judged healthy"

    def test_a_product_database_whose_size_cannot_be_read_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unreadable is not the same as fine -- for a declared database it is a refusal."""
        db = tmp_path / "memory.db"
        db.touch()

        def cannot_stat(self, *a, **kw):
            raise OSError("Input/output error")

        monkeypatch.setattr(Path, "stat", cannot_stat)

        with pytest.raises(snap.SourceComponentUnsound) as caught:
            snap._refuse_unless_sound(db, "memory.db", strict=True)

        assert "could not be read" in str(caught.value)

    def test_a_non_product_db_whose_size_cannot_be_read_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `.db` in the operator's own tree is not this code's business either way."""
        db = tmp_path / "theirs.db"
        db.touch()

        def cannot_stat(self, *a, **kw):
            raise OSError("Input/output error")

        monkeypatch.setattr(Path, "stat", cannot_stat)

        snap._refuse_unless_sound(db, "theirs.db", strict=False)
