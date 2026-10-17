"""Shared inventory vet for untrusted zip containers.

Every site that hands an attacker-supplied ``.docx`` / ``.xlsx`` / ``.pptx``
to ``zipfile`` needs the same guard, and it has to run *before*
``zipfile.ZipFile`` is constructed. This module is that guard, once.

Why the guard binds on the central-directory **byte size**
---------------------------------------------------------
``ZipFile.__init__`` does not read the EOCD's declared entry count when it
materializes the inventory. CPython's ``ZipFile._RealGetContents`` (3.12,
``zipfile/__init__.py``) drives construction off the directory's declared
size::

    size_cd = endrec[_ECD_SIZE]     # bytes in central directory
    ...
    data = fp.read(size_cd)
    total = 0
    while total < size_cd:
        centdir = fp.read(sizeCentralDir)   # 46 bytes, one ZipInfo per pass

So ``size_cd`` is the field that decides how many bytes are read into memory
and how many ``ZipInfo`` objects are allocated. The declared entry count
(``_ECD_ENTRIES_TOTAL``) is parsed and then never consulted during
construction, which means a cap built on the count alone is not a bound at
all: an archive can under-declare its count while carrying a large real
directory and sail straight through to the allocation the cap existed to
prevent.

This vet therefore caps ``size_cd`` as the binding limit, expressed as
``max_members * max_cdir_entry_bytes`` -- one record is 46 fixed bytes plus
its name/extra/comment, so a byte ceiling bounds the entry count as well as
the bytes read. The declared count is capped too, but only as a cheap
consistency check on top; it is never the guard.

ZIP64 is refused, not parsed
----------------------------
When the classic fields saturate (``0xFFFF`` entries / ``0xFFFFFFFF`` bytes)
the real numbers live in a ZIP64 record. Both saturation points are orders of
magnitude past every caller's cap, so no ZIP64 parse could change the verdict
-- the vet refuses outright rather than hand-rolling a second record-location
protocol. That also removes the record-shadowing surface (a crafted archive
declaring a small classic count beside a large ZIP64 record) and the
false-refusal risk of hunting a 4-byte locator signature through a tail that
legitimately contains compressed bytes.

Fail closed: an archive whose tail cannot be read or parsed is rejected, never
passed through. Callers pass their OWN caps -- a preview endpoint legitimately
runs tighter limits than an ingest path -- and translate
``ZipInventoryRejected`` into their own error channel.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

__all__ = [
    "DEFAULT_MAX_CDIR_ENTRY_BYTES",
    "ZipInventoryRejected",
    "vet_zip_inventory",
    "vet_zip_inventory_bytes",
]

# End-of-central-directory record: 4-byte signature + 18 fixed bytes, followed
# by a comment of at most 65535 bytes. The record therefore starts within the
# last 22 + 65535 bytes of the file, which is all this vet ever reads.
_EOCD_SIG = b"PK\x05\x06"
_EOCD_FIXED_SIZE = 22
_MAX_ARCHIVE_COMMENT = 65535
TAIL_WINDOW = _EOCD_FIXED_SIZE + _MAX_ARCHIVE_COMMENT

# Field offsets inside the EOCD record, relative to its signature.
_OFF_ENTRIES_TOTAL = 10  # 2 bytes
_OFF_CDIR_SIZE = 12  # 4 bytes

# Saturation sentinels that push the real values into a ZIP64 record.
_ENTRIES_SATURATED = 0xFFFF
_CDIR_SIZE_SATURATED = 0xFFFFFFFF

# Per-member byte allowance when deriving the directory-size cap from a member
# cap. Generous next to the 46-byte fixed record: OOXML part names are short,
# so this leaves ample room for name/extra/comment without letting the byte
# ceiling become the effective member limit.
DEFAULT_MAX_CDIR_ENTRY_BYTES = 512

ZipSource = Union[str, "os.PathLike[str]"]


class ZipInventoryRejected(Exception):
    """A zip container's declared inventory failed the vet.

    ``reason`` is a short machine-readable discriminator so each call site can
    map it onto its own existing error channel without re-deriving the cause:

    ``missing_eocd``
        No end-of-central-directory record in the tail window -- not a zip
        container as far as ``zipfile`` is concerned either.
    ``truncated_eocd``
        The record starts inside the window but the file ends before its fixed
        fields do.
    ``unreadable``
        The tail could not be read at all (missing path, I/O error).
    ``zip64_saturated``
        Classic fields saturate, so the archive declares an inventory orders of
        magnitude past any caller's cap.
    ``cdir_too_large``
        Declared central-directory byte size exceeds the cap -- the binding
        limit, since this is the field that drives allocation.
    ``too_many_members``
        Declared entry count exceeds the cap.
    """

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _read_tail(source: ZipSource) -> bytes:
    """Read at most ``TAIL_WINDOW`` bytes from the end of the archive at *source*.

    Reads only the tail, so a caller holding a path (knowledge ingest, document
    parsing) never has to load a whole upload into memory to have it vetted. A
    caller that already holds the bytes uses ``vet_zip_inventory_bytes``.
    """
    path = Path(os.fspath(source))
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > TAIL_WINDOW:
                fh.seek(size - TAIL_WINDOW)
            return fh.read(TAIL_WINDOW)
    except OSError as exc:
        raise ZipInventoryRejected("unreadable", f"cannot read archive tail: {exc}") from exc


def vet_zip_inventory_bytes(
    tail: bytes,
    *,
    max_members: int,
    max_cdir_entry_bytes: int = DEFAULT_MAX_CDIR_ENTRY_BYTES,
) -> None:
    """Vet an already-read archive tail. Raises ``ZipInventoryRejected``.

    *tail* must contain the end of the archive -- either the whole file or at
    least its last ``TAIL_WINDOW`` bytes.
    """
    # Search only the window CPython's _EndRecData searches. A caller may hand
    # us a whole file, and a `PK\x05\x06` sequence sitting in member data
    # further back is not an EOCD to zipfile, so it must not be one here either
    # -- reading an inventory from outside the window could only ever accept an
    # archive on numbers zipfile will never use.
    idx = tail.rfind(_EOCD_SIG, max(0, len(tail) - TAIL_WINDOW))
    if idx < 0:
        raise ZipInventoryRejected("missing_eocd", "no end-of-central-directory record")
    if len(tail) < idx + _EOCD_FIXED_SIZE:
        raise ZipInventoryRejected("truncated_eocd", "end-of-central-directory record is truncated")

    count = int.from_bytes(tail[idx + _OFF_ENTRIES_TOTAL : idx + _OFF_ENTRIES_TOTAL + 2], "little")
    cdir_size = int.from_bytes(tail[idx + _OFF_CDIR_SIZE : idx + _OFF_CDIR_SIZE + 4], "little")

    if count == _ENTRIES_SATURATED or cdir_size == _CDIR_SIZE_SATURATED:
        raise ZipInventoryRejected("zip64_saturated", "archive declares a ZIP64-scale inventory")

    # The binding cap: this is the field zipfile reads to size its directory
    # read and its ZipInfo allocation loop.
    if cdir_size > max_members * max_cdir_entry_bytes:
        raise ZipInventoryRejected(
            "cdir_too_large",
            f"central directory declares {cdir_size} bytes",
        )
    if count > max_members:
        raise ZipInventoryRejected("too_many_members", f"archive declares {count} members")


def vet_zip_inventory(
    source: ZipSource,
    *,
    max_members: int,
    max_cdir_entry_bytes: int = DEFAULT_MAX_CDIR_ENTRY_BYTES,
) -> None:
    """Vet an untrusted zip container's declared inventory before parsing it.

    *source* is a filesystem path; only the archive tail is read. Raises
    ``ZipInventoryRejected`` -- callers translate
    it into their own error channel. Returns ``None`` when the archive is
    within the caller's caps.

    This does synchronous file I/O, so async callers MUST run it off the event
    loop (``asyncio.to_thread``).
    """
    tail = _read_tail(source)
    vet_zip_inventory_bytes(
        tail, max_members=max_members, max_cdir_entry_bytes=max_cdir_entry_bytes
    )
