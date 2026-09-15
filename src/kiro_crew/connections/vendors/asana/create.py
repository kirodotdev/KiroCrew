"""Asana create semantics: batch partial failure + no-idempotency readback.

WHAT THIS OWNS
==============
The pure-logic model of how Asana creates behave under batching and under an
ambiguous (retried) create. Network-free: it shapes a batch, reads a batch
result into per-item outcomes, and decides retry safety by MODELING what Asana
guarantees -- which is nothing, for creates. It makes no call and holds no
token.

NO IDEMPOTENCY KEY EXISTS -- MODELED AS "NO GUARANTEE + READBACK"
================================================================
Asana provides NO idempotency key, no client dedupe token, and no natural-key
uniqueness on ANY create, on BOTH the REST and MCP surfaces (confirmed by the
evidence pass across the create-task, create-project, and rate-limits docs).
Retry safety for a create is therefore the CALLER's responsibility, and the
only mechanism this connector can offer is READBACK verification: after a
create whose outcome is unknown (a timeout, a dropped connection), the caller
searches for an already-existing object matching the intended natural key
(name + context) BEFORE re-issuing, and treats a match as "already created".

This module models exactly that and refuses to claim more:

* :class:`RetryDisposition` has NO "exactly_once" value. The strongest it
  expresses is ``READBACK_REQUIRED`` (verify by reading before retrying) --
  because Asana offers no exactly-once primitive, asserting one would be false.
* :func:`plan_create_retry` returns ``READBACK_REQUIRED`` for an ambiguous
  create, never "safe to retry". A blind retry of an ambiguous create is
  modeled as UNSAFE (it can duplicate), which is the whole point.
* :func:`readback_matches` decides, from a readback the caller performed,
  whether the intended object already exists -- so the caller can skip the
  re-issue. It is a best-effort natural-key match, explicitly labelled as such,
  never a proof of exactly-once delivery.

BATCH PARTIAL FAILURE IS FIRST-CLASS
====================================
Asana's MCP ``create_tasks``/``update_tasks`` batch up to 50 items per call. A
batch can PARTIALLY fail -- some items succeed, others fail -- and the connector
must not collapse that into one all-or-nothing verdict. :func:`parse_batch_result`
reads a batch response into per-item :class:`BatchItemOutcome` rows, preserving
which specific items succeeded (with their new GIDs) and which failed (with
their error), so a caller can retry only the failed subset -- and, because of
the no-idempotency rule above, retry that subset only via readback.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No live call, no MCP tool schema (the batch cap of 50 is a documented count, not
a schema), no error taxonomy (a failed item's error is classified through
:mod:`kiro_crew.connections.vendors.asana.errors`). A malformed batch shape raises
:class:`AsanaCreateError`, a plain ``ValueError`` subclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence

from kiro_crew.connections.vendors.asana.errors import AsanaError, classify_error

#: Asana's documented per-call batch cap for create_tasks/update_tasks (MCP).
MCP_BATCH_MAX = 50


class AsanaCreateError(ValueError):
    """A create/batch request or result shape was invalid.

    A shaping fault (an over-cap batch, a malformed batch result). NOT a vendor
    error -- a failed batch ITEM's vendor error is classified via
    :mod:`kiro_crew.connections.vendors.asana.errors`.
    """


class RetryDisposition(str, Enum):
    """How a create may be retried. Deliberately has NO exactly-once value.

    Asana offers no idempotency primitive for creates, so the vocabulary caps
    at "verify by readback first". Expressing an ``EXACTLY_ONCE`` here would
    assert a guarantee the vendor does not provide.
    """

    #: The prior attempt is known to have FAILED cleanly (an error was received,
    #: nothing was created), so a fresh create is safe -- there is nothing to
    #: duplicate.
    SAFE_FRESH_CREATE = "safe_fresh_create"
    #: The prior attempt's outcome is UNKNOWN (timeout, dropped connection). A
    #: blind retry can duplicate; the caller MUST read back for an existing
    #: match before re-issuing. This is the strongest disposition an ambiguous
    #: create can hold -- there is no "safe to retry" for it.
    READBACK_REQUIRED = "readback_required"
    #: The prior attempt is known to have SUCCEEDED (a result with a GID was
    #: received). No retry is needed or wanted.
    ALREADY_CREATED = "already_created"


def plan_create_retry(*, prior_outcome: str, created_gid: Optional[str] = None) -> RetryDisposition:
    """Decide the retry disposition for a create from its prior attempt.

    ``prior_outcome`` is one of ``"failed"`` (a clean error was received),
    ``"unknown"`` (ambiguous -- no response observed), or ``"succeeded"`` (a
    result was received). An ``"unknown"`` outcome ALWAYS yields
    ``READBACK_REQUIRED`` -- never a blind-retry green light -- because Asana
    provides no idempotency key to make the retry safe. A ``"succeeded"``
    outcome requires the ``created_gid`` it produced (its absence is a shaping
    error, since a success with no GID is not a success this model recognizes).
    """

    if prior_outcome == "failed":
        return RetryDisposition.SAFE_FRESH_CREATE
    if prior_outcome == "unknown":
        return RetryDisposition.READBACK_REQUIRED
    if prior_outcome == "succeeded":
        if not (isinstance(created_gid, str) and created_gid.strip()):
            raise AsanaCreateError("a 'succeeded' create must carry the created GID")
        return RetryDisposition.ALREADY_CREATED
    raise AsanaCreateError(
        f"prior_outcome must be 'failed', 'unknown', or 'succeeded', got {prior_outcome!r}"
    )


@dataclass(frozen=True)
class CreateIntent:
    """The natural key a readback matches against.

    Asana has no server-side natural-key uniqueness, so "the same task" is a
    CLIENT-side judgement: an object with the same ``name`` in the same
    ``context`` (a workspace or project GID) as the intended create. This is a
    best-effort identity, explicitly not a guarantee -- two genuinely distinct
    tasks can share a name and context.
    """

    name: str
    context_gid: str


def readback_matches(intent: CreateIntent, existing: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Return the GID of an existing object matching ``intent``, or None.

    ``existing`` is the result of the readback the caller performed (a
    get_tasks / search for the intended name+context). A match is a
    name-equal (exact, case-sensitive -- Asana names are case-sensitive) object;
    the FIRST match's GID is returned so the caller can adopt it instead of
    re-creating. ``None`` means no match was found and a fresh create is
    therefore still required.

    This is best-effort de-duplication, NOT exactly-once: it can miss (the prior
    create had not yet become visible when the readback ran) and it can
    false-match (a pre-existing unrelated task of the same name). It is the only
    mechanism Asana's lack of an idempotency key leaves available, and it is
    labelled as such rather than presented as a delivery guarantee.
    """

    for obj in existing:
        if not isinstance(obj, Mapping):
            raise AsanaCreateError("each readback row must be a mapping")
        if obj.get("name") == intent.name:
            gid = obj.get("gid")
            if isinstance(gid, str) and gid.strip():
                return gid
    return None


def validate_batch_size(items: Sequence[Any]) -> int:
    """Return the batch length, or raise if it exceeds the 50-item cap.

    A per-call batch over :data:`MCP_BATCH_MAX` is refused at shaping time
    rather than as a discovered vendor rejection. An empty batch is a shaping
    error too -- there is nothing to create.
    """

    count = len(items)
    if count == 0:
        raise AsanaCreateError("batch must contain at least one item")
    if count > MCP_BATCH_MAX:
        raise AsanaCreateError(
            f"batch of {count} exceeds the per-call cap of {MCP_BATCH_MAX}; split it"
        )
    return count


@dataclass(frozen=True)
class BatchItemOutcome:
    """One item's outcome within a partially-successful batch.

    ``index`` is the item's position in the submitted batch. ``succeeded`` says
    whether it was created; ``gid`` is its new GID on success (else ``None``);
    ``error`` is the classified vendor error on failure (else ``None``). Exactly
    one of ``gid`` / ``error`` is populated.
    """

    index: int
    succeeded: bool
    gid: Optional[str]
    error: Optional[AsanaError]


@dataclass(frozen=True)
class BatchResult:
    """The per-item breakdown of a batch create/update.

    ``outcomes`` is one row per submitted item, in submission order.
    ``all_succeeded`` / ``any_failed`` are derived conveniences. ``failed_indices``
    lists the positions a caller would retry -- and, per the no-idempotency
    rule, retry only via readback.
    """

    outcomes: tuple[BatchItemOutcome, ...]

    @property
    def all_succeeded(self) -> bool:
        return all(o.succeeded for o in self.outcomes)

    @property
    def any_failed(self) -> bool:
        return any(not o.succeeded for o in self.outcomes)

    @property
    def failed_indices(self) -> tuple[int, ...]:
        return tuple(o.index for o in self.outcomes if not o.succeeded)


def parse_batch_result(
    rows: Sequence[Mapping[str, Any]], *, expected_count: Optional[int] = None
) -> BatchResult:
    """Read a batch response into per-item outcomes, preserving partial failure.

    Each row is either a success (carrying a created ``gid``) or a failure
    (carrying an error ``status`` and optional ``errors`` body, classified via
    :func:`~kiro_crew.connections.vendors.asana.errors.classify_error`). A batch where
    some rows succeed and others fail is preserved AS SUCH -- never collapsed
    into a single all-or-nothing verdict, so a caller can retry only the failed
    subset.

    ``expected_count`` (when given) pins that the response has exactly one row
    per submitted item: a short or long result is a shaping fault, because a
    caller cannot map outcomes back to submitted items otherwise.

    A row is read as a failure when it carries a ``status`` (an HTTP error code)
    or an explicit ``"error"``/``errors`` shape; otherwise it is a success and
    must carry a ``gid``. A row that is neither a well-formed success nor a
    well-formed failure is a shaping fault.
    """

    outcomes: List[BatchItemOutcome] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise AsanaCreateError(f"batch result row {index} must be a mapping")
        status = row.get("status")
        errors_body = row.get("errors")
        has_error_shape = status is not None or errors_body is not None or "error" in row
        if has_error_shape:
            # A failure row: classify its vendor error. When it carries no HTTP
            # status (some MCP error rows may not), fall back to a synthetic 400
            # so classification still yields a typed category rather than raising
            # -- the point is to preserve the failure, not to assert its code.
            effective_status = (
                status if isinstance(status, int) and not isinstance(status, bool) else 400
            )
            body = {"errors": errors_body} if isinstance(errors_body, list) else None
            error = classify_error(effective_status, body)
            outcomes.append(BatchItemOutcome(index=index, succeeded=False, gid=None, error=error))
            continue
        gid = row.get("gid")
        if not (isinstance(gid, str) and gid.strip()):
            raise AsanaCreateError(
                f"batch result row {index} is neither a failure nor a success carrying a gid"
            )
        outcomes.append(BatchItemOutcome(index=index, succeeded=True, gid=gid, error=None))

    if expected_count is not None and len(outcomes) != expected_count:
        raise AsanaCreateError(
            f"batch result has {len(outcomes)} rows, expected {expected_count} "
            "(one per submitted item)"
        )
    return BatchResult(outcomes=tuple(outcomes))
