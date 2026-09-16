"""Refusals the append-only ledger raises, each carrying a stable ``code``.

Every refusal is a :class:`LedgerError` with a machine-readable ``code``, the
same contract :mod:`kiro_crew.work_ledger` uses: a caller (a route handler, a
tool wrapper) branches on the code, and the human-readable message is free to
change without breaking that caller.

Codes are additive-only. A code string, once shipped, is never renamed or
repurposed -- it is part of the API surface, not a log string.
"""

from __future__ import annotations

#: The requested ledger kind is neither ``crew`` nor ``session``.
CODE_BAD_KIND = "bad_kind"
#: The unit id is empty, carries a path separator or a NUL, or resolves outside
#: its own root.
CODE_INVALID_ID = "invalid_id"
#: ``create`` was asked for a ledger whose file is already there.
CODE_ALREADY_EXISTS = "already_exists"
#: Another process owns writes to this unit's ledger, so this one appended
#: nothing. Distinct from every code above: the entry was well formed and the
#: file is healthy -- this process is simply not the writer. A caller reports the
#: loss rather than retrying, because ownership is held for the life of the
#: owning process and every later entry for that unit would queue behind the one
#: waiting for it.
CODE_ALREADY_OWNED = "already_owned"
#: ``open`` was asked for a ledger whose file is not there.
CODE_NO_LEDGER = "no_ledger"
#: Line 1 is missing, unparseable, or describes a different unit than the one
#: asked for. A ledger without a readable header is not a ledger.
CODE_BAD_HEADER = "bad_header"
#: A header field is missing, of the wrong python type, or unknown.
CODE_BAD_HEADER_FIELD = "bad_header_field"

#: ``type`` is not spelled ``domain/action``.
CODE_BAD_TYPE = "bad_type"
#: ``src`` is not one of the fixed emitters nor a well-formed namespaced one.
CODE_BAD_SRC = "bad_src"
#: ``data`` is not a JSON object, or holds something not JSON-serializable.
CODE_BAD_DATA = "bad_data"
#: This ledger kind does not own that ``type`` prefix (rule 1).
CODE_EVENT_TYPE_NOT_OWNED = "event_type_not_owned"
#: A guest emitter wrote outside its own namespace, or wrote a guest type into
#: a session ledger (rule 2).
CODE_NAMESPACE_VIOLATION = "namespace_violation"
#: ``thread`` does not name an existing, earlier seq in this same ledger.
CODE_BAD_THREAD = "bad_thread"
#: ``ref`` is malformed: bad unit, bad id, non-positive bounds, inverted
#: bounds, or a span wider than the read cap.
CODE_BAD_REF = "bad_ref"
#: The serialized entry line is over the size ceiling.
CODE_ENTRY_TOO_LARGE = "entry_too_large"

#: The header names a format version this build does not read. Distinct from
#: ``bad_header`` on purpose: the file is not damaged, this process is old. A
#: newer version may legitimately fail every structural check below, so the
#: caller has to be told to upgrade rather than told the log is corrupt.
CODE_UNSUPPORTED_VERSION = "unsupported_version"
#: A reader that declared the types it understands met one it does not, and the
#: entry did not mark itself ``ignorable``. Raised on the READ path only: an
#: entry a reader cannot interpret may change the meaning of every entry after
#: it, so reconstruction stops rather than silently skipping it.
CODE_UNKNOWN_ENTRY_TYPE = "unknown_entry_type"
#: A segment's header does not belong to the ledger being read, or its filename's
#: declared first seq disagrees with its readable physical first entry. The message
#: names the offending segment so an operator can quarantine the damaged object.
CODE_BAD_SEGMENT = "bad_segment"
#: Seq is not contiguous across a segment boundary: the segment after the gap
#: does not start where the one before it ended. Retention removes whole
#: segments off the FRONT, which leaves no gap between the ones that remain, so
#: a gap here is damage or a partial copy rather than a pruned log.
CODE_SEGMENT_GAP = "segment_gap"
#: A kind's root directory under ``ledgers`` is a link, or resolves outside the
#: data home's own ledger tree. Containment resolves its base first, so a linked
#: kind root would make the link's TARGET the containment root and every path
#: under it would pass -- private history readable and forgeable at a location
#: the protections established for the data home do not cover. The message names
#: the directory so an operator can inspect what it points at.
CODE_BAD_ROOT = "bad_root"


class LedgerError(Exception):
    """A refused ledger operation.

    ``code`` is the stable identifier; ``field`` names the offending input when
    one field is to blame, so a caller can point at it without parsing prose.
    """

    def __init__(self, message: str, *, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class IndeterminateAppend(Exception):
    """An append failed AND could not be rolled back.

    The ordinary failure is definite: bytes reach the file before the fsync runs, so
    an append that fails anywhere truncates the file back to the size it had and
    raises the original error, leaving nothing behind for a retry to reason about.
    This is the case where that cleanup ALSO failed -- likely, since whatever broke
    the write is often still broken -- so the file may hold bytes no entry claims.

    Deliberately NOT a :class:`LedgerError`. A LedgerError is a refusal, meaning the
    entry was declined before any byte was written and retrying is pointless. Here
    the file has been touched and the outcome is unknown, which is the opposite.

    The residue is an unterminated or unparseable tail, which is the shape the next
    ``open`` truncates, so the recovery already exists. The distinct type is so a
    caller can tell "nothing happened" from "something may have".
    """

    def __init__(self, message: str, *, written: bytes, offset: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.written = written
        self.offset = offset

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message
