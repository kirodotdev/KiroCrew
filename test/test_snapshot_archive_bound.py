"""Tests for the archive-size bound that refuses an archive too large to be safe.

A compressed archive can declare orders of magnitude more content than it occupies, so
the bytes it holds say nothing about what extraction would write. The bound runs on the
member headers before `extractall`.
"""

from __future__ import annotations

import tarfile

import pytest

from kiro_crew import snapshot as snap


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
