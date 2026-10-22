"""`snapshot` holds a bundle to the bound `restore` enforces, and both share one override.

A data home past the restore bound must not produce a bundle `restore` refuses: the
writer refuses before any tarball exists, and ``--max-archive-gib N`` on both commands is the explicit way to move a large
install. The decompression guard on restore is unchanged without the flag, and with it
holds the declared content to the free space of the extraction directory.
"""

from __future__ import annotations

import gzip
import io
import sqlite3
import tarfile
from pathlib import Path

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap

_no_gateway = snapshot_helpers._no_gateway

#: Far below the fake home's memory.db, so the default bound is exceeded by a real bundle.
_TINY_BOUND = 1024


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    src = tmp_path / "src"
    _setup_fake_kirocrew(src)
    monkeypatch.setenv("KIROCREW_HOME", str(src))
    return src


def _bundles(out: Path) -> list[Path]:
    return sorted(out.glob("kirocrew-*.tar.gz*")) if out.is_dir() else []


def _forged_archive(path: Path, declared: int) -> None:
    """A gzip tarball whose one header declares *declared* bytes it does not carry."""
    info = tarfile.TarInfo("kirocrew-snapshot-20260101T000000Z/bomb.bin")
    info.size = declared
    path.write_bytes(gzip.compress(info.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024))


class TestTheWriterRefusesWhatRestoreWouldRefuse:
    def test_an_oversized_snapshot_publishes_nothing_and_prunes_nothing(
        self, home, tmp_path, monkeypatch, capsys
    ):
        out = tmp_path / "out"
        out.mkdir()
        older = out / "kirocrew-snapshot-20250101T000000Z.tar.gz"
        older.write_bytes(b"older bundle that restores")
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_BYTES", _TINY_BOUND)
        opened: list[str] = []
        real_open = tarfile.open

        def _spy_open(name=None, mode="r", *a, **kw):
            opened.append(str(mode))
            return real_open(name, mode, *a, **kw)

        monkeypatch.setattr(snap.tarfile, "open", _spy_open)

        rc = snap.snapshot_main([str(out), "--keep", "1"] + unpinnable_argv())

        printed = capsys.readouterr().out
        assert rc == 1, printed
        assert "declares more than" in printed, printed
        assert "--components" in printed and "--max-archive-gib" in printed, printed
        # Refused while staging: no tarball was ever opened for writing, no temp is left,
        # and the older bundle that DOES restore survives.
        assert not any(m.startswith("w") for m in opened), opened
        assert _bundles(out) == [older], _bundles(out)

    def test_an_entry_count_refusal_does_not_offer_the_byte_override(
        self, home, tmp_path, monkeypatch, capsys
    ):
        # The member-count bound is fixed, so advice to raise --max-archive-gib would send
        # the operator into a retry that refuses the same way.
        out = tmp_path / "out"
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 2)

        rc = snap.snapshot_main([str(out)] + unpinnable_argv())

        printed = capsys.readouterr().out
        assert rc == 1, printed
        assert "entries" in printed, printed
        assert "--components" in printed, printed
        assert "--max-archive-gib" not in printed, printed
        assert _bundles(out) == [], _bundles(out)

    def test_the_override_writes_a_bundle_restore_accepts_only_with_an_override(
        self, home, tmp_path, monkeypatch, capsys
    ):
        out = tmp_path / "out"
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_BYTES", _TINY_BOUND)
        rc = snap.snapshot_main([str(out), "--max-archive-gib", "1"] + unpinnable_argv())
        assert rc == 0, capsys.readouterr().out
        (tarball,) = _bundles(out)

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        argv = [str(tarball), "--mode", "replace", "--force"] + unpinnable_argv()

        capsys.readouterr()
        assert snap.restore_main(argv) == 1
        refused = capsys.readouterr().out
        assert "--max-archive-gib" in refused and "Nothing was restored" in refused, refused
        assert not (fresh / "memory.db").exists()

        # Any bound at least the one it was written under restores it, not only the same one.
        assert snap.restore_main(argv + ["--max-archive-gib", "2"]) == 0
        conn = sqlite3.connect(str(fresh / "memory.db"))
        try:
            assert conn.execute("SELECT count(*) FROM semantic_memory").fetchone()[0] == 2
        finally:
            conn.close()


class TestAValidBundleStillRoundTripsWithoutTheFlag:
    def test_snapshot_and_restore_at_the_default_bound(self, home, tmp_path, monkeypatch, capsys):
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out)] + unpinnable_argv()) == 0
        (tarball,) = _bundles(out)
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        rc = snap.restore_main([str(tarball), "--mode", "replace", "--force"] + unpinnable_argv())
        assert rc == 0, capsys.readouterr().out
        assert (fresh / "workspace/doc.md").read_text() == "doc content"


class TestTheDecompressionGuardStillHolds:
    @pytest.mark.parametrize("extra", [[], ["--max-archive-gib", "1"]])
    def test_a_forged_oversize_declaration_is_refused_before_extraction(
        self, tmp_path, monkeypatch, capsys, extra
    ):
        bomb = tmp_path / "bomb.tar.gz"
        _forged_archive(bomb, 5 * 1024**3)  # over both the default and the raised bound
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        rc = snap.restore_main([str(bomb), "--force"] + extra + unpinnable_argv())
        printed = capsys.readouterr().out
        assert rc == 1, printed
        assert "declares more than" in printed and "Nothing was restored" in printed, printed
        assert list(fresh.iterdir()) == []

    def test_a_raised_bound_is_still_held_to_the_free_space(self, tmp_path, monkeypatch, capsys):
        bomb = tmp_path / "big.tar.gz"
        payload = b"\0" * (2 * 1024**2)  # honest 2 MiB member, under the 1 GiB override
        info = tarfile.TarInfo("kirocrew-snapshot-20260101T000000Z/big.bin")
        info.size = len(payload)
        with tarfile.open(bomb, "w:gz") as tf:
            tf.addfile(info, io.BytesIO(payload))
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(fresh))
        real_usage = snap.shutil.disk_usage

        def _nearly_full(path):
            usage = real_usage(path)
            return usage._replace(free=1024**2)

        monkeypatch.setattr(snap.shutil, "disk_usage", _nearly_full)
        rc = snap.restore_main([str(bomb), "--force", "--max-archive-gib", "1"] + unpinnable_argv())
        printed = capsys.readouterr().out
        assert rc == 1, printed
        assert "GiB free where it would be extracted" in printed, printed
        assert list(fresh.iterdir()) == []


class TestTheOverrideIsBounded:
    @pytest.mark.parametrize("value", ["0", "-1", "1025"])
    def test_snapshot_refuses_an_out_of_range_override(self, home, tmp_path, value, capsys):
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--max-archive-gib", value] + unpinnable_argv())
        assert rc == 1
        assert "--max-archive-gib must be between" in capsys.readouterr().out
        assert _bundles(out) == []

    @pytest.mark.parametrize("value", ["0", "-1", "1025"])
    def test_restore_refuses_an_out_of_range_override(self, tmp_path, value, capsys):
        bomb = tmp_path / "any.tar.gz"
        _forged_archive(bomb, 10)
        rc = snap.restore_main([str(bomb), "--force", "--max-archive-gib", value])
        assert rc == 1
        assert "--max-archive-gib must be between" in capsys.readouterr().out


def test_the_tree_bound_counts_what_the_archive_bound_counts(tmp_path, monkeypatch):
    """Writer and reader agree at the boundary: a staged tree the writer passes, archived,
    passes the reader at the same bound, and one byte less refuses both."""
    stage = tmp_path / "kirocrew-snapshot-x"
    (stage / "d").mkdir(parents=True)
    (stage / "a.bin").write_bytes(b"a" * 300)
    (stage / "d" / "b.bin").write_bytes(b"b" * 200)
    archive = tmp_path / "x.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(str(stage), arcname=stage.name)

    snap._refuse_oversized_tree(stage, max_bytes=500)
    with tarfile.open(archive) as tf:
        assert snap._refuse_oversized_archive(tf, max_bytes=500) == 500

    with pytest.raises(snap._ArchiveTooLarge) as caught:
        snap._refuse_oversized_tree(stage, max_bytes=499)
    assert "a.bin" in str(caught.value)
    with tarfile.open(archive) as tf, pytest.raises(snap._ArchiveTooLarge):
        snap._refuse_oversized_archive(tf, max_bytes=499)

    # Four members: the root, d/, a.bin and d/b.bin.
    monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 3)
    with pytest.raises(snap._ArchiveTooLarge):
        snap._refuse_oversized_tree(stage, max_bytes=500)
    with tarfile.open(archive) as tf, pytest.raises(snap._ArchiveTooLarge):
        snap._refuse_oversized_archive(tf, max_bytes=500)
    monkeypatch.setattr(snap, "_MAX_ARCHIVE_MEMBERS", 4)
    snap._refuse_oversized_tree(stage, max_bytes=500)
    with tarfile.open(archive) as tf:
        snap._refuse_oversized_archive(tf, max_bytes=500)
