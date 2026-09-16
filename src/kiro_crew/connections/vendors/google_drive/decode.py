"""The Google Drive ResultDecode -- 2xx HttpReply -> L01 OperationResult.

W01's transport hands a vendor ``decode`` the raw 2xx
:class:`~kiro_crew.connections.control_plane.production.HttpReply` and expects an
:class:`~kiro_crew.connections.control_plane.result.OperationResult` back --
including the ``payload`` (the DATA) and, for a paged list, the single
authoritative ``next_cursor``. This module is the Google Drive fill.

W01 calls a ``decode`` with the reply ALONE (it does not pass the descriptor), so
one decode callable cannot tell ``files.list`` (a JSON collection with a
``nextPageToken``) from ``files.export`` (raw exported bytes). :func:`for_operation`
therefore returns the decode BOUND to one descriptor, and the operation runner
composes the transport with the right one per call. That keeps the "one cursor,
in one place" invariant: a list decode places Drive's ``nextPageToken`` on
``OperationResult.next_cursor`` via :func:`result_with_payload`, and nowhere else.

Shape per operation:

* ``files.list``  -> :class:`CollectionPayload` (the ``files`` array) + cursor.
* ``changes.list`` -> :class:`CollectionPayload` (the ``changes`` array) + cursor.
  The change feed's DURABLE ``newStartPageToken`` is not a within-batch cursor,
  so it is carried as a record in the collection (a sentinel row the operation
  runner reads), never confused with ``next_cursor``.
* ``files.get`` / ``changes.getStartPageToken`` -> :class:`ObjectPayload` (the
  JSON object).
* ``files.export`` / ``files.get?alt=media`` -> :class:`BytesPayload` (raw bytes).

It NEVER coerces bytes to text and NEVER guesses a cursor for a non-list op.
"""

from __future__ import annotations

from typing import Any, Mapping

from kiro_crew.connections.control_plane.production import HttpReply, decode_json_body
from kiro_crew.connections.control_plane.result import (
    DEFAULT_MEDIA_TYPE,
    BytesPayload,
    CollectionPayload,
    ObjectPayload,
    OperationResult,
    result_with_payload,
)

from . import descriptors

# The sentinel record key a changes.list collection uses to carry the durable
# resume checkpoint (newStartPageToken) out of band from the change rows. The
# operation runner reads it; it is not a Drive change record. Spelled with
# characters a real Drive change field cannot collide with.
CHANGES_CHECKPOINT_KEY = "__google_drive_new_start_page_token__"


def for_operation(descriptor):
    """Return the ResultDecode bound to ``descriptor`` (W01 calls it with reply only)."""
    op = descriptor["operation_id"]
    if op == descriptors.OP_LIST_FILES:
        return _decode_file_list
    if op == descriptors.OP_LIST_CHANGES:
        return _decode_change_list
    if op in (descriptors.OP_GET_METADATA, descriptors.OP_GET_START_PAGE_TOKEN):
        return _decode_object
    if op in (descriptors.OP_EXPORT, descriptors.OP_GET_MEDIA):
        return _decode_bytes
    raise ValueError(f"no google_drive decode for operation {op!r}")


def _decode_file_list(reply: HttpReply) -> OperationResult:
    body = decode_json_body(reply)
    raw = body.get("files")
    items = tuple(f for f in raw if isinstance(f, Mapping)) if isinstance(raw, list) else ()
    cursor = _str_or_none(body.get("nextPageToken"))
    return result_with_payload(
        CollectionPayload(items=items),
        status="ok" if cursor is None else "partial",
        next_cursor=cursor,
    )


def _decode_change_list(reply: HttpReply) -> OperationResult:
    body = decode_json_body(reply)
    raw = body.get("changes")
    changes = list(c for c in raw if isinstance(c, Mapping)) if isinstance(raw, list) else []
    cursor = _str_or_none(body.get("nextPageToken"))
    new_start = _str_or_none(body.get("newStartPageToken"))
    # Carry the durable checkpoint as a sentinel record so the runner can read it
    # without a second field on the envelope (which has exactly one cursor slot,
    # and that slot is the WITHIN-batch nextPageToken, not this).
    items = tuple(changes)
    if new_start is not None:
        items = items + ({CHANGES_CHECKPOINT_KEY: new_start},)
    return result_with_payload(
        CollectionPayload(items=items),
        status="ok" if cursor is None else "partial",
        next_cursor=cursor,
    )


def _decode_object(reply: HttpReply) -> OperationResult:
    body = decode_json_body(reply)
    return result_with_payload(ObjectPayload(object=body), status="ok")


def _decode_bytes(reply: HttpReply) -> OperationResult:
    declared = _content_type(reply.headers)
    return result_with_payload(
        BytesPayload(data=reply.body, media_type=declared),
        status="ok",
    )


def _content_type(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.lower() == "content-type" and value and value.strip():
            return value.strip()
    return DEFAULT_MEDIA_TYPE


def _str_or_none(value: Any) -> "str | None":
    return value if isinstance(value, str) and value else None


__all__ = ["CHANGES_CHECKPOINT_KEY", "for_operation"]
