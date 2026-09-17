"""Bulk API 2.0 ingest partial-results contract.

A Bulk API 2.0 ingest job moves through the states
``Open -> UploadComplete -> InProgress -> JobComplete`` (or ``Failed`` /
``Aborted``). Reaching ``JobComplete`` means processing finished and the results
are *available to download* -- it does NOT mean the caller already holds the
per-row outcomes, and it does NOT mean every row succeeded. The per-row results
live in three SEPARATE result sets, each fetched from its own endpoint:

* ``successfulResults`` -- the rows that were applied, with their record ids;
* ``failedResults`` -- the rows that were rejected, each with its own error;
* ``unprocessedRecords`` -- rows never processed (returned for a failed/aborted
  job).

(Facts search-snippet corroborated; ``developer.salesforce.com`` rejects
automated fetches with HTTP 403.)

The contract this module enforces: a job's per-row outcomes are preserved
row-by-row and NEVER aggregated into a single pass/fail. A job can be
``JobComplete`` and still have failed rows; collapsing that into "the job
failed" or "the job succeeded" loses exactly the information a caller needs to
retry or reconcile individual records. :func:`partition_row_outcomes` keeps the
three sets distinct, and :func:`results_downloadable` says whether the per-row
results can be fetched yet -- a question ``JobComplete`` alone does not answer
for a caller that has not yet downloaded them.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class BulkStateError(ValueError):
    """A bulk job status/result payload did not match the ingest contract."""


class BulkIngestState(str, enum.Enum):
    """The Bulk API 2.0 ingest job lifecycle states (corroborated)."""

    OPEN = "Open"
    UPLOAD_COMPLETE = "UploadComplete"
    IN_PROGRESS = "InProgress"
    JOB_COMPLETE = "JobComplete"
    FAILED = "Failed"
    ABORTED = "Aborted"


#: The full closed set of ingest states, by their vendor string.
BULK_INGEST_STATES = frozenset(s.value for s in BulkIngestState)

#: States from which per-row results can be downloaded. ``JobComplete`` yields
#: successful + failed results; ``Failed``/``Aborted`` yield unprocessed records
#: (and any partial results produced before the stop). A job still ``Open`` /
#: ``UploadComplete`` / ``InProgress`` has nothing downloadable yet.
_DOWNLOADABLE_STATES = frozenset(
    {
        BulkIngestState.JOB_COMPLETE.value,
        BulkIngestState.FAILED.value,
        BulkIngestState.ABORTED.value,
    }
)


@dataclass(frozen=True)
class BulkRowOutcome:
    """One row's outcome, preserved verbatim from a bulk result set.

    ``kind`` is exactly one of ``"successful"``, ``"failed"``, or
    ``"unprocessed"`` -- the result set the row came from. ``fields`` is the raw
    row dict as the vendor returned it (e.g. ``sf__Id`` / ``sf__Error`` columns
    for a failed row), never summarized.
    """

    kind: str
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class BulkIngestResult:
    """The three per-row result sets of a bulk ingest job, kept distinct.

    Never reduced to a single verdict. A consumer that wants a count reads
    ``len(...)`` on the set it cares about; the core does not precompute an
    aggregate that would tempt a caller to treat a partially-failed job as
    wholly failed or wholly succeeded.
    """

    state: str
    successful: Sequence[BulkRowOutcome] = field(default_factory=tuple)
    failed: Sequence[BulkRowOutcome] = field(default_factory=tuple)
    unprocessed: Sequence[BulkRowOutcome] = field(default_factory=tuple)

    @property
    def has_failures(self) -> bool:
        """Whether any row failed -- true even when ``state`` is JobComplete."""

        return len(self.failed) > 0 or len(self.unprocessed) > 0


def parse_ingest_job_status(raw: Mapping[str, Any]) -> str:
    """Extract and validate the ingest job ``state`` from a job-info response.

    Raises :class:`BulkStateError` if ``state`` is missing or is not one of the
    corroborated ingest states -- an unrecognized state is vendor drift the
    caller must see, not a value to pass through.
    """

    state = raw.get("state")
    if not isinstance(state, str):
        raise BulkStateError("bulk job status missing a string 'state'")
    if state not in BULK_INGEST_STATES:
        raise BulkStateError(
            f"unrecognized bulk ingest state {state!r}; expected one of "
            f"{sorted(BULK_INGEST_STATES)}"
        )
    return state


def results_downloadable(state: str) -> bool:
    """Whether per-row results can be fetched for a job in ``state``.

    This is the explicit answer to "JobComplete -- can I download results now?":
    yes for ``JobComplete`` / ``Failed`` / ``Aborted``, no for the earlier
    states. It is a distinct question from "did the job succeed"; a downloadable
    ``JobComplete`` job may still have failed rows.

    Raises :class:`BulkStateError` for an unrecognized state (same discipline as
    :func:`parse_ingest_job_status`).
    """

    if state not in BULK_INGEST_STATES:
        raise BulkStateError(f"unrecognized bulk ingest state {state!r}")
    return state in _DOWNLOADABLE_STATES


def partition_row_outcomes(
    state: str,
    *,
    successful_results: Sequence[Mapping[str, Any]] = (),
    failed_results: Sequence[Mapping[str, Any]] = (),
    unprocessed_records: Sequence[Mapping[str, Any]] = (),
) -> BulkIngestResult:
    """Build a :class:`BulkIngestResult` preserving all three sets row by row.

    Each input list is the raw rows from that result set's own download
    endpoint. The rows are tagged with their origin and carried through verbatim
    -- nothing is merged, deduplicated, or reduced to a count. A job with both
    successful and failed rows produces a result with both populated, so the
    caller can retry exactly the failed rows and reconcile the successful ones.

    Raises :class:`BulkStateError` if ``state`` is not downloadable but per-row
    results were nonetheless supplied -- a caller cannot have downloaded results
    for a job whose results are not yet available, so that combination is a bug
    in the caller, surfaced rather than silently accepted.
    """

    if state not in BULK_INGEST_STATES:
        raise BulkStateError(f"unrecognized bulk ingest state {state!r}")
    supplied_any = bool(successful_results or failed_results or unprocessed_records)
    if supplied_any and not results_downloadable(state):
        raise BulkStateError(
            f"per-row results supplied for state {state!r}, whose results are not "
            "downloadable yet; a caller cannot hold results for an in-flight job"
        )
    return BulkIngestResult(
        state=state,
        successful=tuple(BulkRowOutcome("successful", dict(r)) for r in successful_results),
        failed=tuple(BulkRowOutcome("failed", dict(r)) for r in failed_results),
        unprocessed=tuple(BulkRowOutcome("unprocessed", dict(r)) for r in unprocessed_records),
    )
