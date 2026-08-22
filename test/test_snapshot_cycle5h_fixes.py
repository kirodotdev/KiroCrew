"""Absent is not the same as wrong-type, and a rollback directory belongs to one restore.

Both defects come from a `continue` or a reused name standing in for a decision: an entry
of the wrong type read as "not there" and skipped every check; a second-granular rollback
name read as "mine" and collided with the previous restore's saved state.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap


def _real_db(path: Path) -> bytes:
    conn = snap.sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(snap, "_mc_dir", lambda: h)
    monkeypatch.setattr(snap, "_is_gateway_running", lambda: False)
    return h


class TestAWrongTypeEntryIsRefusedNotSkipped:
    def test_a_directory_named_like_a_declared_file_is_refused(self, home, tmp_path, capsys):
        """Otherwise it reads as absent: replace moves live memory aside and restores none."""
        live = home / "memory.db"
        live.write_bytes(_real_db(tmp_path / "live.db"))
        before = live.read_bytes()

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "memory.db").mkdir(parents=True)  # a DIRECTORY with a file's name
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "dirfile.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "not a regular file" in out, out
        assert live.read_bytes() == before, "live memory was moved for a bundle that "
        "could never restore it"

    def test_a_file_named_like_a_declared_tree_is_refused(self, home, tmp_path, capsys):
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace").mkdir(parents=True)
        (payload / "workspace" / "knowledge").write_text("not a dir", encoding="utf-8")
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "sound.db"))
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "filetree.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "not a directory" in out, out

    def test_a_genuinely_absent_entry_still_skips(self, home, tmp_path, capsys):
        """The refusal must not turn a selective bundle into an error."""
        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        payload.mkdir(parents=True)
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "s2.db"))
        # memory_index.db and the trees are simply not in this bundle.
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "partial.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 0, capsys.readouterr().out


class TestTheRollbackDirectoryBelongsToOneRestore:
    def test_a_second_allocation_in_the_same_second_gets_its_own_directory(self, home):
        """Two restores inside one UTC second must not share a rollback set."""
        first = snap._allocate_rollback_dir(home)
        second = snap._allocate_rollback_dir(home)
        assert first != second, "the second restore reused the first's rollback directory"
        assert first.is_dir() and second.is_dir()
        assert second.name.startswith(
            first.name
        ), f"the suffixed name should stay recognisable: {second.name}"

    def test_the_operator_readable_stem_is_kept(self, home):
        got = snap._allocate_rollback_dir(home)
        assert got.name.startswith("pre-restore-"), got.name

    def test_allocation_refuses_rather_than_crashing_when_exhausted(self, home, monkeypatch):
        """A refusal names the problem; an uncaught FileExistsError does not."""

        def always_taken(self, *a, **kw):
            raise FileExistsError(str(self))

        monkeypatch.setattr(Path, "mkdir", always_taken)
        with pytest.raises(snap.SourceComponentUnsound) as e:
            snap._allocate_rollback_dir(home)
        assert "rollback directory" in str(e.value)

    def test_two_restores_in_one_second_both_complete(self, home, tmp_path, capsys):
        """End to end: the second restore must not die on the first's rollback path."""
        (home / "memory.db").write_bytes(_real_db(tmp_path / "live2.db"))
        mem = home / "workspace" / "memory"
        mem.mkdir(parents=True)
        (mem / "note.md").write_text("local", encoding="utf-8")

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace" / "memory").mkdir(parents=True)
        (payload / "workspace" / "memory" / "in.md").write_text("in", encoding="utf-8")
        (payload / "memory.db").write_bytes(_real_db(tmp_path / "in2.db"))
        (payload / "MANIFEST.json").write_text(
            '{"version": 3, "components": {"memory": "unresolved"}}', encoding="utf-8"
        )
        bundle = tmp_path / "twice.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        argv = [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        assert snap.restore_main(argv) == 0, capsys.readouterr().out
        assert snap.restore_main(argv) == 0, capsys.readouterr().out
        saved = sorted(p.name for p in home.glob("pre-restore-*"))
        assert len(saved) >= 2, f"each restore needs its own rollback set: {saved}"
