"""A rollback is a round-trip, and an empty file is not a healthy database."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import snapshot as snap


class TestRecoveryPutsBackWhatTheSavePreserved:
    def test_a_link_survives_the_whole_save_then_restore_round_trip(self, tmp_path: Path) -> None:
        """The save keeping a link is worth nothing if the restore drops it.

        Restore runs AFTER the live tree is cleared, so a link lost here is lost outright.
        """
        live = tmp_path / "home" / "workspace"
        (live / "sub").mkdir(parents=True)
        (live / "sub" / "real.md").write_text("mine\n", encoding="utf-8")
        (live / "sub" / "pointer.md").symlink_to(live / "sub" / "real.md")

        saved = tmp_path / "rollback" / "workspace"
        snap._copytree_rollback(live, saved)
        assert (saved / "sub" / "pointer.md").is_symlink(), "the save half regressed"

        # The restore half, as recovery performs it: clear the live tree, copy back.
        snap._clear_tree_root(live)
        snap._copytree_rollback(saved, live)

        assert (
            live / "sub" / "pointer.md"
        ).is_symlink(), "the link was dropped on the way back, after the live copy was already gone"
        assert (live / "sub" / "real.md").is_file()


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
