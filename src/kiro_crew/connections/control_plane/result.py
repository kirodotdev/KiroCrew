"""W01 · L01: the result envelope a connector operation returns.

A pure-type envelope, in the ``TypedDict`` + module-level schema-version shape
the connections subsystem already uses (``l0_probe.ProbeResult`` and
``l1_smoke.SmokeResult`` are the in-repo precedent). It does no IO: it describes
the OUTCOME of a call plus the DATA that call returned, so a downstream stream
and a runtime dispatch read one vocabulary for "did it fully succeed, partly
succeed, or is there more to fetch" -- and one vocabulary for "here is what came
back" -- instead of each inventing its own.

The envelope describes the outcome; a shape that carried NOTHING ELSE would make
the success path functionally empty: a caller that got ``{"status": "ok",
"next_cursor": None}`` would hold no items, no object and no bytes, so every operation
in the plane would succeed at returning nothing. :data:`OperationPayload` closes
that. It is ONE neutral channel with exactly three preserved shapes -- a
COLLECTION (:class:`CollectionPayload`), a SINGLE OBJECT
(:class:`ObjectPayload`), and RAW BYTES (:class:`BytesPayload`, which is how an
Office document travels) -- because those are the three things a connector
operation actually returns and collapsing any of them into another loses data:
a collection flattened to an object loses every item but one, and bytes coerced
to text corrupt an ``xlsx`` irreversibly.

The ``status`` axis is two-valued and deliberately distinct from the
error taxonomy in :mod:`kiro_crew.connections.control_plane.errors`:

- ``ok`` -- the operation completed and returned everything it was asked for.
- ``partial`` -- the operation returned a usable-but-incomplete result. This is
  the SUCCESS-side ``partial``: some data came back and the caller may act on
  it. It is NOT the same concept as the RUN-01 error class ``partial`` in
  ``errors.py``, which classifies a FAILURE that partially applied; the two
  live on opposite sides of the success/failure line on purpose and a consumer
  must not fold them together.

Pagination is carried by ``next_cursor``: an OPAQUE continuation token when
more results remain, or ``None`` when the result is complete. It is deliberately
a single opaque string and never the vendor's raw locator shape -- one operation
uses ``@odata.nextLink``, another a ``page``/``perPage`` pair, another
``queryMore``; the manifest declares each operation's own ``pagination``
contract, and this envelope only needs to say "here is where you resume, or you
are done". A ``next_cursor`` is meaningful for both ``ok`` and ``partial``: a
fully-successful page can still have a successor, and the terminal page carries
``None``.

``OperationResult.next_cursor`` is the SINGLE AUTHORITATIVE cursor, and the ONLY
place a cursor lives. :class:`CollectionPayload` carries items and nothing else.

An earlier shape put the cursor on BOTH -- the envelope and the collection -- and
called it deliberate duplication, on the argument that a consumer handed only the
items would lose the continuation. That argument does not survive the failure it
creates. :class:`OperationResult` is a ``TypedDict``, so nothing stops a producer
from building one by hand, or a middle layer from editing ``next_cursor`` on the
mapping it was handed; the moment the two copies disagree a paging walk either
STOPS while pages remain (silently dropped records) or re-fetches a page it
already has (silently duplicated records), and neither is visible at the seam --
both look exactly like a correct result. One field cannot disagree with itself,
which is the only durable form of the guarantee, so the second copy is gone and
:class:`~kiro_crew.connections.control_plane.executor.PageWalk` reads and advances
on ``OperationResult.next_cursor`` alone.

The consumer-hands-the-items-on concern is real and is answered by handing on the
ENVELOPE (or the ``(items, next_cursor)`` pair) rather than the bare payload: a
caller that drops the envelope has dropped the continuation on purpose, visibly,
in its own code -- as opposed to inheriting a truncation from two fields that
drifted somewhere upstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, Optional, Tuple, TypedDict, Union

#: Bumped when this envelope's shape changes, mirroring the sibling modules.
#:
#: ``2`` added the ``payload`` field to :class:`OperationResult`. It is a
#: REQUIRED key, so this is a breaking change for a producer and an old pin would
#: decode a v2 envelope wrong (it would not know a payload could be there at all,
#: and would keep reading a success as data-free). Making it optional was the
#: alternative and was rejected: a silently-absent payload is the exact defect
#: being fixed, and a required key forces every producer to say ``None`` on
#: purpose instead of omitting it by accident.
#:
#: ``3`` REMOVED ``CollectionPayload.next_cursor`` and moved
#: :func:`result_with_payload`'s cursor to an explicit keyword argument. Two
#: shape changes an old pin decodes wrong, in opposite directions: a v2 consumer
#: reading ``payload.next_cursor`` now raises ``AttributeError`` (loud, and the
#: cheap half), while a v2 PRODUCER that passes the cursor only on the collection
#: silently builds an envelope whose ``next_cursor`` is ``None`` -- a walk that
#: stops after page one and reports a complete result. The second is why this
#: number had to move rather than ride along on ``2``.
RESULT_SCHEMA_VERSION = 3

#: The operation completed and returned everything asked for.
RESULT_STATUS_OK = "ok"
#: The operation returned a usable-but-incomplete result (success side; NOT the
#: RUN-01 error class ``partial`` -- see the module docstring and ``errors.py``).
RESULT_STATUS_PARTIAL = "partial"

#: The result envelope's own two-value success axis.
ResultStatus = Literal["ok", "partial"]

#: Tuple form of :data:`ResultStatus`'s closed set.
RESULT_STATUSES: tuple[ResultStatus, ...] = ("ok", "partial")

#: A list of records plus the cursor that continues it.
PAYLOAD_KIND_COLLECTION = "collection"
#: Exactly one record.
PAYLOAD_KIND_OBJECT = "object"
#: Raw bytes with a declared media type (an Office document, a PDF, an image).
PAYLOAD_KIND_BYTES = "bytes"

#: The payload channel's own closed discriminant.
PayloadKind = Literal["collection", "object", "bytes"]

#: Tuple form of :data:`PayloadKind`'s closed set.
PAYLOAD_KINDS: tuple[PayloadKind, ...] = ("collection", "object", "bytes")

#: What a :class:`BytesPayload` reports when the provider declared no media type.
#: The generic binary type, never a guess derived from the bytes themselves: this
#: envelope reports what it was TOLD, and sniffing would make the plane assert a
#: format it did not read.
DEFAULT_MEDIA_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class CollectionPayload:
    """A page of records. ITEMS ONLY -- the cursor is NOT here.

    ``items`` -- the records this page returned, in the provider's order. A tuple
    because the envelope is handed across a seam and a caller must not be able to
    mutate another caller's page.

    There is deliberately NO ``next_cursor`` on this class. The continuation lives
    on :attr:`OperationResult.next_cursor` and NOWHERE else, because a second copy
    of a cursor is a second thing that can be wrong: an envelope is a mutable
    ``TypedDict``, either copy can be written independently, and a disagreement
    surfaces as a walk that stops early (dropped records) or repeats a page
    (duplicated records) with nothing at the seam to distinguish it from a correct
    result. A consumer that needs to hand the continuation on hands on the
    ENVELOPE, not the bare payload.

    ``kind`` -- the closed discriminant, so a consumer can switch on it instead of
    ``isinstance`` when it is routing rather than unpacking.
    """

    items: Tuple[Mapping[str, Any], ...] = ()
    kind: ClassVar[PayloadKind] = "collection"


@dataclass(frozen=True)
class ObjectPayload:
    """Exactly ONE record -- a single fetch, or the thing a write created.

    ``object`` -- the record itself. Kept as a :class:`Mapping` rather than
    flattened into the envelope so a field named ``status`` or ``next_cursor`` in
    a provider's object cannot collide with the envelope's own machinery.

    It is deliberately NOT a one-element :class:`CollectionPayload`: an operation
    whose contract is "one object" must not present as a collection a caller then
    pages, and a caller that asked for one object must not have to unwrap a list
    and guess what more than one element would have meant.
    """

    object: Mapping[str, Any]
    kind: ClassVar[PayloadKind] = "object"


@dataclass(frozen=True)
class BytesPayload:
    """RAW BYTES, never text-coerced and never dropped.

    This is how an Office document (``xlsx`` / ``docx`` / ``pptx``), a PDF, or any
    other binary body travels. The two properties that matter are both negative:

    * **Never coerced.** Nothing on this path calls ``.decode()``. An ``xlsx`` is
      a ZIP container: it is not valid UTF-8, so a decode either raises or (with
      ``errors="replace"``) silently substitutes replacement characters and
      produces a file that fails to open. ``data`` is the provider's bytes,
      byte for byte.
    * **Never dropped.** Discarding the body on the way to the envelope would
      turn a downloaded workbook into a bare ``status``.

    ``data`` -- the exact bytes. ``media_type`` -- what the provider DECLARED the
    bytes are (the ``Content-Type`` value, parameters included, verbatim), or
    ``application/octet-stream`` when it declared nothing; it is reported, never
    sniffed from the bytes. ``filename`` -- the provider-supplied name when there
    was one, so a consumer writing the bytes out has something to call them.
    """

    data: bytes
    media_type: str = DEFAULT_MEDIA_TYPE
    filename: Optional[str] = None
    kind: ClassVar[PayloadKind] = "bytes"


#: The ONE neutral data channel: a success carries exactly one of these shapes,
#: or ``None`` when the operation genuinely returned no data (a 204, an empty
#: acknowledgement). A union rather than one struct with three optional members,
#: so "a collection AND some bytes" is not representable and a consumer that
#: handled the three cases has handled all of them.
OperationPayload = Union[CollectionPayload, ObjectPayload, BytesPayload]


class OperationResult(TypedDict):
    """The outcome envelope for one connector operation invocation.

    Every field present, matching the sibling descriptors' shape.

    ``status`` -- ``ok`` or ``partial`` (success axis; a failure is carried by
    :mod:`kiro_crew.connections.control_plane.errors`, not by this envelope).
    ``next_cursor`` -- the SINGLE authoritative continuation token: an opaque
    string when more results remain, ``None`` when the result is complete. It is
    the only cursor in the plane, and
    :class:`~kiro_crew.connections.control_plane.executor.PageWalk` advances on it
    alone.
    ``payload`` -- the DATA the operation returned, as exactly one of the three
    :data:`OperationPayload` shapes, or ``None`` when it returned none. Build both
    with :func:`result_with_payload`.
    """

    status: ResultStatus
    next_cursor: str | None
    payload: OperationPayload | None


def result_with_payload(
    payload: Optional[OperationPayload],
    *,
    status: ResultStatus = "ok",
    next_cursor: Optional[str] = None,
) -> OperationResult:
    """Build an envelope carrying ``payload`` and, for a collection, its cursor.

    ``next_cursor`` is an EXPLICIT argument and lands in exactly one place: the
    envelope. Deriving it from a ``CollectionPayload.next_cursor`` would
    read as safe -- one constructor wrote both copies -- but only for envelopes
    that went through this function. An :class:`OperationResult` is a
    ``TypedDict``; a producer can build one literally and a middle layer can
    reassign ``next_cursor`` on the mapping, so "constructed consistently here"
    never covered the paths that matter. With one field there is nothing to keep
    consistent.

    A cursor is only meaningful for a COLLECTION. An :class:`ObjectPayload`, a
    :class:`BytesPayload` and ``None`` are not paged shapes, so passing a cursor
    with one raises :class:`ValueError` rather than being quietly ignored: a
    caller that believed it was returning a continuation must not have it dropped,
    and a caller that reused this constructor with the wrong shape has a bug that
    should be loud here instead of surfacing as a walk fetching a page that does
    not exist.
    """

    if next_cursor is not None and not isinstance(payload, CollectionPayload):
        raise ValueError(
            "next_cursor is only meaningful for a CollectionPayload; a single "
            "object, raw bytes and an empty result are not paged shapes, so a "
            "cursor here would point a caller at a page that does not exist"
        )
    return {"status": status, "next_cursor": next_cursor, "payload": payload}
