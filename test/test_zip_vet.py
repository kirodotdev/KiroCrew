"""The shared untrusted-zip inventory vet (#3908).

The load-bearing property, and the reason a shared constant would not have been
enough: ``ZipFile.__init__`` materializes one ``ZipInfo`` per DECLARED
central-directory entry, so a member cap read off ``infolist()`` runs after the
allocation it is meant to bound. These pin that the declared count is refused
from raw EOCD bytes BEFORE any ``ZipFile`` is constructed.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from kiro_crew.zip_vet import ZipVetError, vet_zip_inventory


def _zip_with(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _forge_declared_entry_count(path: Path, count: int) -> None:
    """Rewrite the EOCD's total-entries field without adding real members.

    This is the crafted-archive shape the preflight exists for: a tiny file that
    DECLARES a huge directory. `zipfile` believes the declaration during
    construction, which is where the allocation happens.
    """
    raw = bytearray(path.read_bytes())
    idx = raw.rfind(b"PK\x05\x06")
    assert idx >= 0, "test fixture has no EOCD"
    struct.pack_into("<H", raw, idx + 8, min(count, 0xFFFF))   # this-disk entries
    struct.pack_into("<H", raw, idx + 10, min(count, 0xFFFF))  # total entries
    path.write_bytes(bytes(raw))


class TestDeclaredEntryCount:
    def test_a_forged_entry_count_is_refused_before_zipfile_is_built(self, tmp_path, monkeypatch):
        """The whole point: refusal happens without ``ZipFile`` being constructed.

        Asserted by making construction fail loudly — if the vet reached it, the
        test would surface that error instead of the refusal.
        """
        p = _zip_with(tmp_path / "a.docx", {"word/document.xml": b"<x/>"})
        _forge_declared_entry_count(p, 60000)

        def _must_not_construct(*a, **k):
            raise AssertionError("ZipFile was constructed before the member cap bound")

        monkeypatch.setattr(zipfile, "ZipFile", _must_not_construct)

        with pytest.raises(ZipVetError) as exc:
            vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)
        assert exc.value.reason == "too_many_members"

    def test_an_ordinary_archive_within_bounds_passes(self, tmp_path):
        p = _zip_with(tmp_path / "ok.docx", {"word/document.xml": b"<x/>"})
        vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)

    def test_a_missing_eocd_is_refused(self, tmp_path):
        """A file that got here passed a PK magic gate, so no directory means
        truncated or crafted -- not 'an empty archive'."""
        p = tmp_path / "trunc.docx"
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        with pytest.raises(ZipVetError) as exc:
            vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)
        assert exc.value.reason == "missing_eocd"

    def test_a_zip64_locator_pointing_outside_the_file_is_refused(self, tmp_path):
        """A ZIP64 shadow record: the archive claims a record it does not carry."""
        p = _zip_with(tmp_path / "z.docx", {"a": b"x"})
        raw = bytearray(p.read_bytes())
        idx = raw.rfind(b"PK\x05\x06")
        locator = bytearray(b"PK\x06\x07")
        locator += struct.pack("<I", 0)              # disk with ZIP64 EOCD
        locator += struct.pack("<Q", 1 << 40)        # offset far past EOF
        locator += struct.pack("<I", 1)              # total disks
        raw[idx:idx] = locator
        p.write_bytes(bytes(raw))

        with pytest.raises(ZipVetError) as exc:
            vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)
        assert exc.value.reason == "bad_zip64_locator"


class TestExpansionBound:
    def test_declared_expansion_over_the_cap_is_refused(self, tmp_path):
        # Highly compressible content: small on disk, large declared.
        p = _zip_with(tmp_path / "bomb.docx", {"big": b"0" * 5_000_000})
        with pytest.raises(ZipVetError) as exc:
            vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1_000_000)
        assert exc.value.reason == "uncompressed_too_large"

    def test_a_bad_zipfile_becomes_a_refusal_not_an_escaping_exception(
        self, tmp_path, monkeypatch
    ):
        """`zipfile` is tolerant of several malformed directories and recovers,
        so the mapping is pinned at the boundary rather than by crafting bytes
        that a future CPython might also decide to recover from."""
        p = _zip_with(tmp_path / "x.docx", {"a": b"x"})

        def _bad(*a, **k):
            raise zipfile.BadZipFile("truncated central directory")

        monkeypatch.setattr(zipfile, "ZipFile", _bad)
        with pytest.raises(ZipVetError) as exc:
            vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)
        assert exc.value.reason == "bad_archive"

    def test_an_empty_archive_is_accepted(self, tmp_path):
        """An EOCD with zero entries is a VALID empty zip, not a refusal."""
        p = tmp_path / "empty.docx"
        with zipfile.ZipFile(p, "w"):
            pass
        vet_zip_inventory(p, max_members=100, max_uncompressed_bytes=1 << 30)


class TestCallSitesAreRouted:
    """Both untrusted-archive parse sites on main go through the shared vet."""

    def test_knowledge_ingest_reports_the_shared_reason(self, tmp_path):
        from kiro_crew.dashboard.handlers.knowledge import _inspect_zip_archive

        p = _zip_with(tmp_path / "k.docx", {"word/document.xml": b"<x/>"})
        _forge_declared_entry_count(p, 60000)
        assert _inspect_zip_archive(str(p)) == "too_many_members"

    def test_knowledge_ingest_still_accepts_an_ordinary_archive(self, tmp_path):
        from kiro_crew.dashboard.handlers.knowledge import _inspect_zip_archive

        p = _zip_with(tmp_path / "k2.docx", {"word/document.xml": b"<x/>"})
        assert _inspect_zip_archive(str(p)) is None

    @pytest.mark.parametrize("suffix", ["docx", "pptx"])
    def test_doc_parser_refuses_a_forged_archive_instead_of_opening_it(self, tmp_path, suffix):
        """These two sites had NO inventory vet at all before this change."""
        import kiro_crew.doc_parser as dp

        member = "word/document.xml" if suffix == "docx" else "ppt/slides/slide1.xml"
        p = _zip_with(tmp_path / f"d.{suffix}", {member: b"<x/>"})
        _forge_declared_entry_count(p, 60000)

        extract = dp._extract_docx if suffix == "docx" else dp._extract_pptx
        assert extract(str(p)) == ""
