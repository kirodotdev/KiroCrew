"""One inventory vet for every site that parses an UNTRUSTED zip container.

``.docx``/``.xlsx``/``.pptx`` are zip archives, so any upload path that hands one
to a parser is parsing attacker-controlled container metadata. Two bounds matter,
and they are not interchangeable:

* **How much the archive expands.** A small file whose members declare gigabytes
  of uncompressed content is a decompression bomb (CWE-409/770). The declared
  sizes in the central directory are enough to refuse it without extracting.
* **How many entries the archive declares.** This is the one that is easy to get
  wrong, and the reason this module exists rather than a shared constant:

      ``ZipFile.__init__`` materializes one ``ZipInfo`` per declared
      central-directory entry.

  So a cap checked through ``infolist()`` runs AFTER the allocation it is meant
  to bound. An archive declaring hundreds of thousands of entries exhausts memory
  during construction, while the check that was supposed to stop it sits on the
  next line, never reached. The only guard that actually binds is a raw-bytes
  preflight over the End Of Central Directory record — reading the fields at the
  exact fixed offsets ``zipfile`` itself parses — performed BEFORE the
  ``ZipFile`` is constructed.

Caps are parameters, not constants here: a preview endpoint legitimately runs
tighter limits than an ingest path. The implementation is what is shared.
"""

from __future__ import annotations

import logging
import os
import struct
import zipfile

logger = logging.getLogger(__name__)

# End Of Central Directory record: signature, then fixed-width fields. The
# comment that may follow it is why the record has to be searched for rather
# than read at a fixed offset from the end.
_EOCD_SIG = b"PK\x05\x06"
_EOCD_SIZE = 22
_EOCD_MAX_COMMENT = 0xFFFF

# ZIP64 End Of Central Directory locator/record. A ZIP64 archive stores 0xFFFF /
# 0xFFFFFFFF sentinels in the classic EOCD and the real counts here, so trusting
# the classic record alone lets a crafted archive declare a tiny entry count and
# still carry a huge directory -- the "ZIP64 shadow record" case.
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_SIG = b"PK\x06\x06"

#: Bytes read from the tail when hunting the EOCD. The record is at most 22
#: bytes plus a 64 KiB comment, so this always contains it when one exists.
_TAIL_READ = _EOCD_SIZE + _EOCD_MAX_COMMENT + _ZIP64_LOCATOR_SIZE


class ZipVetError(Exception):
    """An untrusted archive was refused. ``reason`` is a short stable token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_tail(path: str) -> bytes:
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        fh.seek(max(0, size - _TAIL_READ))
        return fh.read()


def _declared_entry_count(path: str) -> int:
    """Entries the archive's directory DECLARES, from raw bytes only.

    Raises :class:`ZipVetError` when no EOCD record is present — a file that
    reached here passed a PK magic-byte gate, so a missing directory means
    truncated or crafted, not "an empty archive".
    """
    tail = _read_tail(path)
    idx = tail.rfind(_EOCD_SIG)
    if idx < 0 or len(tail) - idx < _EOCD_SIZE:
        raise ZipVetError("missing_eocd")
    # EOCD: sig(4) disk(2) cd_disk(2) this_disk_entries(2) total_entries(2) ...
    total_entries = struct.unpack_from("<H", tail, idx + 10)[0]

    # ZIP64: the classic field saturates at 0xFFFF, and the true count lives in
    # the ZIP64 EOCD that the locator points at. Consult it whenever a locator
    # is present, not only when the classic count is saturated -- a crafted
    # archive can declare a small classic count beside a large ZIP64 record, and
    # `zipfile` reads the ZIP64 one.
    loc = tail.rfind(_ZIP64_LOCATOR_SIG)
    if loc >= 0 and len(tail) - loc >= _ZIP64_LOCATOR_SIZE:
        z64_offset = struct.unpack_from("<Q", tail, loc + 8)[0]
        try:
            with open(path, "rb") as fh:
                fh.seek(z64_offset)
                rec = fh.read(56)
        except OSError:
            raise ZipVetError("bad_zip64_locator") from None
        # A locator pointing at something that is not a ZIP64 EOCD -- including
        # past EOF, where the read simply comes back short rather than raising --
        # is itself a refusal: the archive claims a record it does not contain.
        if rec[:4] != _ZIP64_EOCD_SIG or len(rec) < 40:
            raise ZipVetError("bad_zip64_locator")
        # ZIP64 EOCD: sig(4) size(8) vers(2) vers_needed(2) disk(4)
        # cd_disk(4) this_disk_entries(8) total_entries(8)
        total_entries = max(total_entries, struct.unpack_from("<Q", rec, 32)[0])
    return total_entries


def vet_zip_inventory(
    path: str,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> None:
    """Refuse an untrusted zip container that exceeds either bound.

    Order is the whole point:

    1. the raw-bytes EOCD preflight, BEFORE ``ZipFile`` is constructed, so the
       declared entry count is bounded before it can drive an allocation;
    2. only then the central-directory walk for declared expansion size.

    Raises :class:`ZipVetError` with a short stable reason; returns ``None`` when
    the archive is within both bounds. Does synchronous file I/O, so callers on
    the event loop must run it via ``asyncio.to_thread``.
    """
    declared = _declared_entry_count(path)
    if declared > max_members:
        logger.warning(
            "zip vet: refused archive declaring %d entries (cap %d)", declared, max_members
        )
        raise ZipVetError("too_many_members")

    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            # Belt to the preflight's braces: a directory whose real entry count
            # disagrees with its declared one is malformed, and the parser about
            # to run would be reading the real one.
            if len(infos) > max_members:
                raise ZipVetError("too_many_members")
            uncompressed = 0
            for zi in infos:
                uncompressed += zi.file_size
                if uncompressed > max_uncompressed_bytes:
                    raise ZipVetError("uncompressed_too_large")
    except zipfile.BadZipFile:
        raise ZipVetError("bad_archive") from None
    except OSError:
        raise ZipVetError("bad_archive") from None
