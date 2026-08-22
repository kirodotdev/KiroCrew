"""Tests for the archive-size bound on a downloaded bundle.

A compressed archive can declare orders of magnitude more content than it occupies, so
the bytes that arrived say nothing about what extraction would write. The bound runs on
the member headers before `extractall`.
"""

from __future__ import annotations

import tarfile

import pytest

from kiro_crew import snapshot as snap
from kiro_crew import snapshot_remote


def _info(name: str, size: int, *, kind: str = "file") -> tarfile.TarInfo:
    """A REAL TarInfo, so isfile()/size behave as production sees them."""
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "link": tarfile.SYMTYPE}[kind]
    return ti


class _Walker:
    """Stands in for TarFile's member cursor only -- `next()` is the real API used."""

    def __init__(self, members):
        self._it = iter(members)

    def next(self):  # noqa: A003 - mirrors tarfile.TarFile.next
        return next(self._it, None)


class TestTheBoundRefusesWhatWouldNotFit:
    def test_too_many_members_is_refused(self):
        members = [_info(f"f{i}", 1) for i in range(snap._MAX_ARCHIVE_MEMBERS + 1)]
        with pytest.raises(snap._ArchiveTooLarge) as e:
            snap._refuse_oversized_archive(_Walker(members))
        assert "entries" in str(e.value)

    def test_too_many_declared_bytes_is_refused(self):
        members = [_info("huge", snap._MAX_ARCHIVE_BYTES + 1)]
        with pytest.raises(snap._ArchiveTooLarge) as e:
            snap._refuse_oversized_archive(_Walker(members))
        assert "GiB" in str(e.value)

    def test_the_bound_is_a_sum_not_a_per_member_check(self):
        """A bomb split across many honest-looking members must still be refused."""
        each = snap._MAX_ARCHIVE_BYTES // 4
        members = [_info(f"p{i}", each) for i in range(5)]
        with pytest.raises(snap._ArchiveTooLarge):
            snap._refuse_oversized_archive(_Walker(members))

    def test_a_normal_bundle_passes(self):
        members = [_info("kirocrew-snapshot-x/MANIFEST.json", 200)] + [
            _info(f"kirocrew-snapshot-x/f{i}", 4096) for i in range(50)
        ]
        snap._refuse_oversized_archive(_Walker(members))  # no raise

    def test_directory_and_link_headers_do_not_count_toward_the_size(self):
        """Their declared size is never written, so counting them would refuse
        honest archives."""
        members = [
            _info("d", snap._MAX_ARCHIVE_BYTES, kind="dir"),
            _info("l", snap._MAX_ARCHIVE_BYTES, kind="link"),
            _info("real", 10),
        ]
        snap._refuse_oversized_archive(_Walker(members))  # no raise

    def test_a_negative_declared_size_cannot_reduce_the_total(self):
        members = [
            _info("neg", -(snap._MAX_ARCHIVE_BYTES)),
            _info("big", snap._MAX_ARCHIVE_BYTES + 1),
        ]
        with pytest.raises(snap._ArchiveTooLarge):
            snap._refuse_oversized_archive(_Walker(members))

    def test_the_walk_stops_at_the_member_that_crosses_the_bound(self):
        """Bounded work, not work proportional to what the archive claims."""
        seen = 0

        def gen():
            nonlocal seen
            for i in range(snap._MAX_ARCHIVE_MEMBERS * 10):
                seen += 1
                yield _info(f"f{i}", 1)

        with pytest.raises(snap._ArchiveTooLarge):
            snap._refuse_oversized_archive(_Walker(gen()))
        assert (
            seen <= snap._MAX_ARCHIVE_MEMBERS + 1
        ), f"walked {seen} members; the bound must stop the walk"


class TestTheBoundIsActuallyWiredIntoTheDownloadPath:
    """A correct helper proves nothing if the restore path never calls it."""

    def test_a_downloaded_archive_over_the_bound_is_refused_and_removed(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        # Lower the bound instead of building a real bomb.
        monkeypatch.setattr(snap, "_MAX_ARCHIVE_BYTES", 1024)

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace").mkdir(parents=True)
        (payload / "workspace" / "big.md").write_text("x" * 4096, encoding="utf-8")
        bundle = tmp_path / "bundle.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        downloaded = tmp_path / "dl.tar.gz"

        def fake_download(uri, into, profile):
            downloaded.write_bytes(bundle.read_bytes())
            return downloaded

        monkeypatch.setattr(snap, "_resolve_aws_profile", lambda p: ("someprofile", "us-west-2"))
        monkeypatch.setattr(snapshot_remote, "download", fake_download)
        rc = snap.restore_main(["s3://b/k", "--force"])
        out = capsys.readouterr().out
        if rc == 0:
            pytest.fail(f"an over-bound download was accepted: {out}")
        assert "uncompressed content" in out or "entries" in out, out
        assert "removed the download" in out, out
        assert not downloaded.exists(), "the bad download must be removed"

    def test_a_within_bound_download_is_NOT_refused_by_this_check(
        self, tmp_path, monkeypatch, capsys
    ):
        """Guards the other direction: the bound must not reject an honest bundle."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()

        payload = tmp_path / "kirocrew-snapshot-20260101T000000Z"
        (payload / "workspace").mkdir(parents=True)
        (payload / "workspace" / "small.md").write_text("hi", encoding="utf-8")
        bundle = tmp_path / "ok.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(payload), arcname=payload.name)

        dl = tmp_path / "dl2.tar.gz"
        monkeypatch.setattr(snap, "_resolve_aws_profile", lambda p: ("someprofile", "us-west-2"))
        monkeypatch.setattr(
            snapshot_remote,
            "download",
            lambda uri, into, profile: (dl.write_bytes(bundle.read_bytes()), dl)[1],
        )
        snap.restore_main(["s3://b/k", "--force"])
        out = capsys.readouterr().out
        assert "uncompressed content" not in out, out
        assert "not a readable snapshot archive" not in out, out
