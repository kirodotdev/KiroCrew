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
searches for an object matching the intended natural key (name + context) that
was NOT present before the attempt. Such a newly-appeared object is reported as
AMBIGUOUS -- it could be the attempt's own result or a concurrent unrelated
create, and Asana provides no discriminator to tell them apart -- so the
connector surfaces it to the caller rather than silently treating it as
"already created". A pre-existing distinct object of the same name+context is
never even a candidate.

This module models exactly that and refuses to claim more:

* :class:`RetryDisposition` has NO "exactly_once" value. The strongest it
  expresses is ``READBACK_REQUIRED`` (verify by reading before retrying) --
  because Asana offers no exactly-once primitive, asserting one would be false.
* :func:`plan_create_retry` returns ``READBACK_REQUIRED`` for an ambiguous
  create, never "safe to retry". A blind retry of an ambiguous create is
  modeled as UNSAFE (it can duplicate), which is the whole point.
* :func:`readback_matches` decides, from a readback the caller performed AND
  the set of GIDs it observed BEFORE the attempt, whether a same-name+context
  object appeared afterwards -- and returns a typed :class:`ReadbackOutcome`
  (``NO_MATCH`` or ``AMBIGUOUS`` with candidate GIDs), NEVER a silently-adopted
  GID. Because Asana has no attempt discriminator, a newly-appeared object
  cannot be proven to be this attempt's own rather than a concurrent unrelated
  create, so the connector surfaces the ambiguity to the caller instead of
  guessing -- consistent with modeling "no exactly-once guarantee" rather than
  manufacturing one.

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
from typing import AbstractSet, Any, List, Mapping, Optional, Sequence

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

    The caller carries a load-bearing precondition when it maps a vendor error
    to ``prior_outcome``: ``"failed"`` (-> ``SAFE_FRESH_CREATE``) is ONLY correct
    for a response where non-commitment is CERTAIN -- a 4xx validation rejection
    the server could not have acted on. A 5xx or otherwise ambiguous create
    response, where the object may or may not have been committed, MUST be
    classified ``"unknown"`` (-> ``READBACK_REQUIRED``), never ``"failed"`` --
    otherwise a fresh create would duplicate a possibly-committed object, the
    exact trap this module exists to prevent. This function decides the
    disposition from the classification; the classification itself is the
    caller's (wiring-slice) responsibility, made explicit here so it is not left
    as an uncited assumption.
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


def _row_in_context(obj: Mapping[str, Any], context_gid: str) -> bool:
    """True if ``obj`` (a readback row) belongs to ``context_gid``.

    ``context_gid`` is a workspace OR project GID (:class:`CreateIntent`'s
    natural key half). A row matches when its ``workspace.gid`` equals it, OR
    one of its ``projects[].gid`` memberships equals it. Both the nested-mapping
    (``{"gid": ...}``) and bare-string shapes Asana uses are accepted. A row that
    does not state a matching context is NOT a match -- that is the whole point:
    a same-named object in a different workspace/project must never be adopted.
    """

    workspace = obj.get("workspace")
    if isinstance(workspace, Mapping):
        if workspace.get("gid") == context_gid:
            return True
    elif isinstance(workspace, str):
        if workspace == context_gid:
            return True

    projects = obj.get("projects")
    if isinstance(projects, Sequence) and not isinstance(projects, (str, bytes)):
        for project in projects:
            if isinstance(project, Mapping):
                if project.get("gid") == context_gid:
                    return True
            elif isinstance(project, str):
                if project == context_gid:
                    return True
    return False


class ReadbackResolution(str, Enum):
    """The outcome of an ambiguous-create readback. NO adopted-GID value exists.

    Asana provides no attempt-specific discriminator (no idempotency key), so a
    readback can NEVER prove that a newly-appeared same-name+context object is
    THIS attempt's own result rather than a concurrent unrelated create. The
    vocabulary therefore refuses to express "adopt this GID": the strongest
    honest answers are "nothing new appeared" and "something appeared but it is
    ambiguous -- you decide".
    """

    #: No same-name+same-context object appeared that was absent before the
    #: attempt. The create did not (visibly) take effect; a fresh create is
    #: still required. (It may still have taken effect but not yet be visible --
    #: that is the caller's unknown to own, not a match.)
    NO_MATCH = "no_match"
    #: One or more same-name+same-context objects appeared that were absent from
    #: the pre-attempt snapshot. ANY of them could be this attempt's result OR a
    #: concurrent unrelated create -- indistinguishable without a discriminator
    #: Asana does not provide. The candidates are surfaced; the connector does
    #: NOT silently adopt one. The caller decides (e.g. by a higher-level
    #: correlation it owns, or by escalating), never this pure layer.
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReadbackOutcome:
    """The result of :func:`readback_matches`.

    ``resolution`` is the typed verdict. ``candidate_gids`` holds the
    newly-appeared same-name+context GIDs when ``resolution`` is ``AMBIGUOUS``
    (empty for ``NO_MATCH``). There is deliberately NO "adopted"/"matched" GID
    field: the connector never resolves the ambiguity to a single GID on its
    own, because Asana gives it no way to do so honestly.
    """

    resolution: ReadbackResolution
    candidate_gids: tuple[str, ...] = ()


def readback_matches(
    intent: CreateIntent,
    existing: Sequence[Mapping[str, Any]],
    pre_attempt_gids: AbstractSet[str],
) -> ReadbackOutcome:
    """Resolve an ambiguous-create readback WITHOUT silently adopting a GID.

    ``existing`` is the result of the readback the caller performed (a
    get_tasks / search for the intended name+context). ``pre_attempt_gids`` is
    the set of GIDs the caller observed for that same name+context BEFORE it
    issued the create -- a REQUIRED argument with no default: without a
    pre-attempt snapshot the readback cannot even tell which objects are new, so
    the type system refuses to let the caller omit it. Pass an empty set only
    when the caller has genuinely confirmed nothing pre-existed.

    The candidates are the rows whose name equals ``intent.name`` (exact,
    case-sensitive), whose workspace/project context equals
    ``intent.context_gid``, AND whose GID is NOT in ``pre_attempt_gids`` (i.e.
    they appeared AFTER the attempt). The result:

    * :attr:`ReadbackResolution.NO_MATCH` -- no such new object appeared; a
      fresh create is still required.
    * :attr:`ReadbackResolution.AMBIGUOUS` -- one or more did; their GIDs are
      returned in ``candidate_gids`` and the connector does NOT pick one.

    Why no adopted GID is EVER returned: Asana has no idempotency key and no
    attempt-specific discriminator, so a newly-appeared same-name+context object
    is indistinguishable between "my create's result" and "a concurrent
    unrelated create in the snapshot-to-readback window". Returning one as
    "mine" would silently attribute a possibly-unrelated GID to this attempt and
    drop the intended create -- exactly the exactly-once claim this module
    refuses to make. Surfacing ambiguity hands the decision to the caller, which
    is the honest boundary of a no-idempotency vendor: this layer models the
    guarantee (none) rather than manufacturing one.
    """

    candidates: List[str] = []
    for obj in existing:
        if not isinstance(obj, Mapping):
            raise AsanaCreateError("each readback row must be a mapping")
        if obj.get("name") == intent.name and _row_in_context(obj, intent.context_gid):
            gid = obj.get("gid")
            if isinstance(gid, str) and gid.strip() and gid not in pre_attempt_gids:
                candidates.append(gid)
    if not candidates:
        return ReadbackOutcome(resolution=ReadbackResolution.NO_MATCH)
    return ReadbackOutcome(
        resolution=ReadbackResolution.AMBIGUOUS, candidate_gids=tuple(candidates)
    )


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


def parse_batch_result(rows: Sequence[Mapping[str, Any]], *, expected_count: int) -> BatchResult:
    """Read a batch response into per-item outcomes, preserving partial failure.

    Each row is either a success (carrying a created ``gid``) or a failure
    (carrying an error ``status`` and optional ``errors`` body, classified via
    :func:`~kiro_crew.connections.vendors.asana.errors.classify_error`). A batch where
    some rows succeed and others fail is preserved AS SUCH -- never collapsed
    into a single all-or-nothing verdict, so a caller can retry only the failed
    subset.

    ``expected_count`` is REQUIRED (the number of items the caller submitted)
    and the exact-cardinality check ALWAYS runs: the response must have exactly
    one row per submitted item. A short/empty response (some items missing) or a
    long one is a shaping fault, refused here -- because otherwise a caller
    cannot map outcomes back to submitted items, and an empty/short response
    would make ``all_succeeded`` vacuously true (``all([])`` is ``True``) and
    silently lose the omitted items. Making it required with no default removes
    that silent-loss path at the type level.

    A row is read as a failure when it carries a ``status`` (an HTTP error code)
    or an explicit ``"error"``/``errors`` shape; otherwise it is a success and
    must carry a ``gid``. A row that is neither a well-formed success nor a
    well-formed failure is a shaping fault.
    """

    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise AsanaCreateError("expected_count must be an int (the submitted item count)")
    if expected_count < 1:
        raise AsanaCreateError("expected_count must be >= 1; an empty submission has no batch")

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

    if len(outcomes) != expected_count:
        raise AsanaCreateError(
            f"batch result has {len(outcomes)} rows, expected {expected_count} "
            "(one per submitted item)"
        )
    return BatchResult(outcomes=tuple(outcomes))
