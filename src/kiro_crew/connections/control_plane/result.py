"""W01 · L01: the result envelope a connector operation returns.

A pure-type envelope, in the ``TypedDict`` + module-level schema-version shape
the connections subsystem already uses (``l0_probe.ProbeResult`` and
``l1_smoke.SmokeResult`` are the in-repo precedent). It carries no payload and
does no IO: it describes the OUTCOME of a call, so a downstream stream and a
runtime dispatch read one vocabulary for "did it fully succeed, partly succeed,
or is there more to fetch" instead of each inventing its own.

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
"""

from __future__ import annotations

from typing import Literal, TypedDict

#: Bumped when this envelope's shape changes, mirroring the sibling modules.
RESULT_SCHEMA_VERSION = 1

#: The operation completed and returned everything asked for.
RESULT_STATUS_OK = "ok"
#: The operation returned a usable-but-incomplete result (success side; NOT the
#: RUN-01 error class ``partial`` -- see the module docstring and ``errors.py``).
RESULT_STATUS_PARTIAL = "partial"

#: The result envelope's own two-value success axis.
ResultStatus = Literal["ok", "partial"]

#: Tuple form of :data:`ResultStatus`'s closed set.
RESULT_STATUSES: tuple[ResultStatus, ...] = ("ok", "partial")


class OperationResult(TypedDict):
    """The outcome envelope for one connector operation invocation.

    Every field present, matching the sibling descriptors' shape.

    ``status`` -- ``ok`` or ``partial`` (success axis; a failure is carried by
    :mod:`kiro_crew.connections.control_plane.errors`, not by this envelope).
    ``next_cursor`` -- an opaque continuation token when more results remain, or
    ``None`` when the result is complete.
    """

    status: ResultStatus
    next_cursor: str | None
