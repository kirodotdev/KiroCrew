"""Append-only ledgers for crews and sessions -- the storage layer, nothing else.

The format, the rules and how this stream relates to the lifecycle event track are
specified in ``docs/system-specs/modules/ledger-core.md``. Two units, one file
each, the gateway the only writer::

    from kiro_crew.ledger import Ledger

    crew = Ledger.create("crew", "qa")
    crew.append("member/joined", {"who": "s-7f3a"}, src="gateway")

A crew header carries nothing but the unit's id and its creation time, which is
why the example passes neither: a display name belongs to the members store, which
owns it and can change it, and ``create`` refuses an unknown header field with
``bad_header_field`` rather than dropping it.

This package holds no dashboard, route, tool or migration code. It reads and
writes its own files and imports only path, lock and containment primitives, so
a consumer can be added without this layer learning about it.
"""

from __future__ import annotations

from kiro_crew.ledger.errors import (
    CODE_ALREADY_EXISTS,
    CODE_BAD_DATA,
    CODE_BAD_HEADER,
    CODE_BAD_HEADER_FIELD,
    CODE_BAD_KIND,
    CODE_BAD_REF,
    CODE_BAD_SRC,
    CODE_BAD_THREAD,
    CODE_BAD_TYPE,
    CODE_ENTRY_TOO_LARGE,
    CODE_EVENT_TYPE_NOT_OWNED,
    CODE_INVALID_ID,
    CODE_NAMESPACE_VIOLATION,
    CODE_NO_LEDGER,
    CODE_SEGMENT_GAP,
    CODE_UNKNOWN_ENTRY_TYPE,
    LedgerError,
)
from kiro_crew.ledger.schema import (
    FIXED_SOURCES,
    KIND_CREW,
    KIND_SESSION,
    KINDS,
    MAX_ENTRY_BYTES,
    MAX_REF_SPAN,
    SCHEMA_VERSION,
    TYPE_OWNERSHIP,
    CrewHeader,
    Entry,
    Header,
    Ref,
    SessionHeader,
    SessionThread,
)
from kiro_crew.ledger.store import (
    DEFAULT_PAGE_LIMIT,
    LEDGER_FILE,
    MAX_PAGE_LIMIT,
    STATUS_CORRUPT,
    STATUS_GONE,
    STATUS_OK,
    STATUS_PRUNED,
    Ledger,
    Page,
    Resolution,
    ledger_dir,
    ledger_path,
    ledger_root,
    now_ms,
    segment_first_seqs,
    segment_paths,
)

__all__ = [
    "CODE_ALREADY_EXISTS",
    "CODE_BAD_DATA",
    "CODE_BAD_HEADER",
    "CODE_BAD_HEADER_FIELD",
    "CODE_BAD_KIND",
    "CODE_BAD_REF",
    "CODE_BAD_SRC",
    "CODE_BAD_THREAD",
    "CODE_BAD_TYPE",
    "CODE_ENTRY_TOO_LARGE",
    "CODE_EVENT_TYPE_NOT_OWNED",
    "CODE_INVALID_ID",
    "CODE_NAMESPACE_VIOLATION",
    "CODE_NO_LEDGER",
    "CODE_UNKNOWN_ENTRY_TYPE",
    "CODE_SEGMENT_GAP",
    "DEFAULT_PAGE_LIMIT",
    "FIXED_SOURCES",
    "KINDS",
    "KIND_CREW",
    "KIND_SESSION",
    "LEDGER_FILE",
    "MAX_ENTRY_BYTES",
    "MAX_PAGE_LIMIT",
    "MAX_REF_SPAN",
    "SCHEMA_VERSION",
    "STATUS_CORRUPT",
    "STATUS_GONE",
    "STATUS_OK",
    "STATUS_PRUNED",
    "TYPE_OWNERSHIP",
    "CrewHeader",
    "Entry",
    "Header",
    "Ledger",
    "LedgerError",
    "Page",
    "Ref",
    "Resolution",
    "SessionHeader",
    "SessionThread",
    "ledger_dir",
    "ledger_path",
    "segment_first_seqs",
    "segment_paths",
    "ledger_root",
    "now_ms",
]
