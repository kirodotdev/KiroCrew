"""Tests for the shared untrusted-archive inventory vet (kiro_crew.zip_vet).

The crafted-archive cases here were previously spelled only against
/api/file-sheet's inline vet (test_file_sheet.py); they now live at the shared
module, which is what all three untrusted-archive parse sites route through.

The load-bearing case is `test_declared_directory_size_is_the_binding_cap`:
CPython's ZipFile._RealGetContents drives its ZipInfo allocation off the
declared central-directory SIZE, never off the declared entry count, so a vet
that caps only the count does not bind against an archive that under-declares
its count.
"""

from __future__ import annotations

import zipfile

import pytest

from kiro_crew.zip_vet import (
    TAIL_WINDOW,
    ZipInventoryRejected,
    vet_zip_inventory,
    vet_zip_inventory_bytes,
)

_EOCD = b"PK\x05\x06"
_EOCD_FIXED = 22


def _tiny_archive(path, members: int = 1) -> bytes:
    with zipfile.ZipFile(path, "w") as z:
        for i in range(members):
            z.writestr(f"m{i}", b"x")
    return path.read_bytes()


def _set_u16(data: bytearray, at: int, value: int) -> None:
    data[at : at + 2] = value.to_bytes(2, "little")


def _set_u32(data: bytearray, at: int, value: int) -> None:
    data[at : at + 4] = value.to_bytes(4, "little")


# ── the four crafted-archive cases ───────────────────────────────────────────


def test_declared_entry_count_over_cap_is_refused(tmp_path):
    data = bytearray(_tiny_archive(tmp_path / "crafted.zip"))
    eocd = data.rfind(_EOCD)
    _set_u16(data, eocd + 10, 0xFFFE)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(bytes(data), max_members=4096)
    assert exc.value.reason == "too_many_members"


def test_declared_directory_size_is_the_binding_cap(tmp_path):
    """A forged-LOW entry count beside a large declared directory must still be
    refused -- this is the archive shape a count-only vet lets through.

    ZipFile._RealGetContents reads `size_cd = endrec[_ECD_SIZE]`, then
    `data = fp.read(size_cd)` and loops `while total < size_cd`, allocating one
    ZipInfo per 46-byte record. The declared entry count is parsed and never
    consulted, so it is the directory size -- and only the directory size --
    that bounds the allocation.
    """
    data = bytearray(_tiny_archive(tmp_path / "crafted.zip"))
    eocd = data.rfind(_EOCD)
    _set_u16(data, eocd + 10, 1)  # honest-looking, well under any cap
    _set_u32(data, eocd + 12, 0xFFFFFFF0)  # ~4 GiB of directory to walk
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(bytes(data), max_members=4096)
    assert exc.value.reason == "cdir_too_large"


def test_missing_eocd_is_refused():
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(b"PK\x03\x04" + b"\x00" * 64, max_members=4096)
    assert exc.value.reason == "missing_eocd"


def test_zip64_saturated_fields_are_refused_without_parsing_zip64(tmp_path):
    """Saturated classic fields are refused outright.

    Both saturation points are orders of magnitude past every caller's cap, so
    no ZIP64 parse could change the verdict -- and refusing here means the vet
    never hunts a locator signature through a tail that legitimately contains
    compressed bytes.
    """
    body = bytearray(_tiny_archive(tmp_path / "crafted.zip"))
    eocd = body.rfind(_EOCD)
    _set_u16(body, eocd + 10, 0xFFFF)
    data = b"PK\x06\x06" + b"\x00" * 52 + bytes(body)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(data, max_members=4096)
    assert exc.value.reason == "zip64_saturated"

    body2 = bytearray(_tiny_archive(tmp_path / "crafted2.zip"))
    eocd2 = body2.rfind(_EOCD)
    _set_u32(body2, eocd2 + 12, 0xFFFFFFFF)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(bytes(body2), max_members=4096)
    assert exc.value.reason == "zip64_saturated"


# ── refusal happens before ZipFile is constructed ────────────────────────────


def test_the_vet_never_constructs_a_zipfile(tmp_path, monkeypatch):
    """Proven by making ZipFile construction itself an error: the vet must reach
    its verdict from raw tail bytes alone, for accept as well as refuse.

    The archives are written first, then ZipFile is replaced -- so the explosion
    can only come from the vet's own code path.
    """
    good = tmp_path / "ok.zip"
    body = bytearray(_tiny_archive(good, members=2))
    eocd = body.rfind(_EOCD)
    _set_u32(body, eocd + 12, 0xFFFFFFF0)
    crafted = tmp_path / "bad.zip"
    crafted.write_bytes(bytes(body))

    def _explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("zip_vet constructed a ZipFile")

    monkeypatch.setattr(zipfile, "ZipFile", _explode)

    vet_zip_inventory(good, max_members=4096)  # accepted, nothing constructed
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory(crafted, max_members=4096)
    assert exc.value.reason == "cdir_too_large"


# ── source handling: path, file object, tail-only reads ──────────────────────


def test_a_valid_archive_within_caps_is_accepted(tmp_path):
    path = tmp_path / "ok.zip"
    _tiny_archive(path, members=3)
    vet_zip_inventory(path, max_members=4096)
    vet_zip_inventory_bytes(path.read_bytes(), max_members=4096)


def test_an_empty_archive_is_accepted(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    vet_zip_inventory(path, max_members=4096)


def test_only_the_tail_is_read(tmp_path, monkeypatch):
    """A path source must never pull the whole archive into memory: the read is
    bounded by TAIL_WINDOW regardless of file size.

    This is what lets knowledge ingest and document parsing preflight an upload
    without loading it -- so it is asserted, not assumed.
    """
    path = tmp_path / "padded.zip"
    body = _tiny_archive(path, members=2)
    # Prepend well over one tail window of filler; zipfile tolerates a prefix.
    path.write_bytes(b"\x00" * (TAIL_WINDOW * 3) + body)
    assert path.stat().st_size > TAIL_WINDOW * 3

    sizes: list[int] = []
    real_open = open

    class _Recorder:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            sizes.append(n)
            return self._fh.read(n)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def _fake_open(*a, **kw):
        return _Recorder(real_open(*a, **kw))

    monkeypatch.setattr("kiro_crew.zip_vet.open", _fake_open, raising=False)
    vet_zip_inventory(path, max_members=4096)
    assert sizes, "the vet did not read the archive"
    assert max(sizes) <= TAIL_WINDOW
    assert sum(sizes) <= TAIL_WINDOW


def test_a_missing_path_fails_closed(tmp_path):
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory(tmp_path / "nope.zip", max_members=4096)
    assert exc.value.reason == "unreadable"


def test_a_truncated_eocd_fails_closed(tmp_path):
    # EOCD signature present but the record's fixed fields run past EOF.
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(b"\x00" * 16 + _EOCD + b"\x00" * 4, max_members=4096)
    assert exc.value.reason == "truncated_eocd"


# ── caps are the caller's ────────────────────────────────────────────────────


def test_caps_are_per_caller(tmp_path):
    data = _tiny_archive(tmp_path / "twelve.zip", members=12)
    vet_zip_inventory_bytes(data, max_members=4096)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(data, max_members=10)
    assert exc.value.reason == "too_many_members"


def test_the_directory_cap_is_derived_from_the_member_cap(tmp_path):
    """max_cdir_entry_bytes scales the byte ceiling: an endpoint tunes its own
    per-entry allowance without touching the shared implementation."""
    data = bytearray(_tiny_archive(tmp_path / "c.zip"))
    eocd = data.rfind(_EOCD)
    _set_u32(data, eocd + 12, 100 * 64)
    vet_zip_inventory_bytes(bytes(data), max_members=100, max_cdir_entry_bytes=64)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(bytes(data), max_members=100, max_cdir_entry_bytes=32)
    assert exc.value.reason == "cdir_too_large"


def test_an_eocd_signature_outside_the_tail_window_is_not_an_eocd(tmp_path):
    """zipfile's _EndRecData searches only the last TAIL_WINDOW bytes, so a
    stray PK\x05\x06 further back is not an EOCD -- and must not be read as
    one here, or the vet would judge an archive on numbers zipfile never uses.
    """
    body = _tiny_archive(tmp_path / "ok.zip", members=1)
    stray = bytearray(body)
    eocd = stray.rfind(_EOCD)
    # A plausible-looking but out-of-window record, then a tail with no EOCD.
    forged = bytes(stray[eocd : eocd + _EOCD_FIXED]) + b"\x00" * (TAIL_WINDOW + 64)
    with pytest.raises(ZipInventoryRejected) as exc:
        vet_zip_inventory_bytes(forged, max_members=4096)
    assert exc.value.reason == "missing_eocd"
