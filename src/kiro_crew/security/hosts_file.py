"""Pure helpers for the ssh self-target floor's hosts-file table.

The line parser that turns hosts-file text, fed one chunk at a time, into
``{name -> maps-to-local}``, the content digest a Windows cache key carries,
and the two exceptions the reads raise.  ``ssh_self_floor`` owns every read, the
cache and the gate; these live apart so that parsing plumbing does not count
against that module's per-module liveness cap.

Layer.  This module imports nothing from the package: ``ssh_self_floor`` imports
these names and re-binds them in its own namespace, which is the seam tests
monkeypatch.
"""

from __future__ import annotations

import codecs
import hashlib
import ipaddress
import os
from collections.abc import Iterable, Iterator


class _HostsFileTooLarge(Exception):
    """The file holds more than the *limit* a gate-path parse may read."""


class _HostsFileUnreadable(Exception):
    """The key's digest read failed; not an OSError, so the gate cannot take it as absent."""


def _hosts_content_digest_enabled() -> bool:
    """True where ``st_ctime`` is creation time (Windows); a seam for tests."""
    return os.name == "nt"


def _hosts_content_digest(data: bytes) -> bytes:
    """Content digest for a hosts-file key.

    blake2b, not crc32: crc32 measured about 4x faster at 64 KiB (16 us
    against 65 us of hash time), but a CRC is linear, so four free bytes in
    a comment make a same-size rewrite with the same CRC.
    """
    return hashlib.blake2b(data, digest_size=16).digest()


def _ends_with_break(text: str) -> bool:
    """True when *text*, one piece from ``str.splitlines(keepends=True)``, ends with a break.

    Only valid for such a piece (at most one break, at its end): for ``"a\\nb"``
    it answers True.
    """
    return text.splitlines()[0] != text if text else False


def _decoded_chunks(content: bytes, step: int) -> "Iterator[str]":
    """*content* decoded as UTF-8 (errors replaced), *step* bytes at a time."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    view = memoryview(content)
    for start in range(0, len(view), step):
        yield decoder.decode(view[start : start + step])
    yield decoder.decode(b"", final=True)


def _parse_hosts_chunks(chunks: "Iterable[str]", own: "frozenset[str]") -> "dict[str, bool]":
    """``{name -> maps-to-local}`` for hosts-file text arriving as *chunks*.

    A name is local when its address is loopback, unspecified, or in *own*;
    a name on several lines is local if ANY of them maps local.  Each chunk
    is split on its own, so no single decode+split call holds the GIL for
    long (one 4 MiB call measured about 16 ms, one chunk well under 1 ms).
    A line cut at a chunk edge is kept as a list of pieces and joined once,
    when its line break (or the end of the input) arrives, so every
    character is split and joined a bounded number of times and no line is
    dropped.
    """
    table: "dict[str, bool]" = {}

    def _add_line(line: str) -> None:
        fields = line.partition("#")[0].split()
        if len(fields) < 2:
            return
        addr = fields[0].split("%", 1)[0]
        try:
            ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
        except ValueError:
            return
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        local = ip.is_loopback or ip.is_unspecified or str(ip).lower() in own
        for name in fields[1:]:
            lowered = name.lower()
            table[lowered] = table.get(lowered, False) or local

    pending: "list[str]" = []
    for chunk in chunks:
        if not chunk:
            continue
        parts = chunk.splitlines(keepends=True)
        if pending:
            # The first part continues the unfinished line; if it has no
            # break either, it is the whole chunk and the line goes on.
            pending.append(parts[0])
            if not _ends_with_break(parts[0]):
                continue
            _add_line("".join(pending))
            pending = []
            parts = parts[1:]
        if parts and not _ends_with_break(parts[-1]):
            pending.append(parts.pop())
        for part in parts:
            _add_line(part)
    if pending:
        _add_line("".join(pending))
    return table
