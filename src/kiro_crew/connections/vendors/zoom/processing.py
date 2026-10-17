"""Zoom AI Companion processing state: a tri-state, never collapsed to two.

Pure logic. Reading a Zoom AI Companion meeting/webinar summary has three
outcomes that must stay distinct:

* ``COMPLETED`` -- the summary exists and is ready.
* ``PENDING`` -- requested/started but not ready; the readiness signal is the
  webhook ``meeting.summary_completed``. A pending read must surface the pending
  state and must NOT fabricate a placeholder or return an empty body as success.
* ``NOT_ENTITLED`` -- the account/user lacks the entitlement (AI Companion for a
  summary, Cloud Recording for recordings, the Webinar add-on for webinar reads).
  Permanent-until-entitlement-changes, distinct from a transient pending and from
  a real empty result.

This is the anti-conflation rule: Zoom's native AI Companion summary is a
vendor-side product and is NEVER the builtin Meetings app's own STT+LLM summary
(see ``docs/system-specs/modules/connector-zoom.md`` and ``meetings.md``).

See ``docs/system-specs/modules/connector-zoom.md``.
"""

from __future__ import annotations

import enum
from typing import Optional


class SummaryState(enum.Enum):
    """The three states an AI Companion summary read can resolve to."""

    COMPLETED = "completed"
    PENDING = "pending"
    NOT_ENTITLED = "not_entitled"


# Zoom signals "feature not enabled here" with a small set of forbidden /
# plan-missing codes rather than an empty-but-successful body. Data-driven so a
# newly-observed entitlement code is a data change. Code 2314 is the AI Companion
# "enable the Meeting/Webinar summary setting" forbidden; code 200 is Zoom's
# plan-missing / "only available for Paid account" business error.
_ENTITLEMENT_ERROR_CODES: frozenset[int] = frozenset({200, 2314})

# The webhook whose arrival means a summary is ready to read.
SUMMARY_READY_WEBHOOK = "meeting.summary_completed"


def classify_summary_state(
    *,
    http_status: int,
    summary_present: bool,
    error_code: Optional[int] = None,
    processing_status: Optional[str] = None,
) -> SummaryState:
    """Map an AI Companion summary read to exactly one tri-state value.

    Args:
        http_status: the HTTP status of the read.
        summary_present: whether the response actually carried summary content.
            A ``200`` with ``summary_present=False`` is NEVER treated as a real
            success -- that is the "200-empty fake success" this module refuses.
        error_code: Zoom's business error code, when the response carried one.
        processing_status: the summary's own status field when present
            (e.g. ``"completed"`` / ``"processing"``).

    Resolution order, tightest signal first:

    1. An entitlement error code (:data:`_ENTITLEMENT_ERROR_CODES`), or a 403
       carrying one, is :data:`SummaryState.NOT_ENTITLED` -- regardless of HTTP
       status, because Zoom returns some of these as ``200``-coded business
       errors.
    2. An explicit processing status decides next: a ``completed`` status is
       ``COMPLETED`` ONLY when real content is also present -- a ``completed``
       status with no body is a contradictory response and falls to ``PENDING``
       rather than being reported as a readable summary. Any other status
       (``processing``/``pending``/absent-yet) is ``PENDING``.
    3. On a ``200`` with no status field, presence of real content is
       ``COMPLETED``; its absence is ``PENDING`` -- never a fabricated success.
    4. A ``404``-class read with no entitlement signal is ``PENDING`` (the
       summary has not been produced yet), not a fake completion.
    """
    if error_code is not None and error_code in _ENTITLEMENT_ERROR_CODES:
        return SummaryState.NOT_ENTITLED
    if http_status == 403 and error_code in _ENTITLEMENT_ERROR_CODES:
        return SummaryState.NOT_ENTITLED

    if processing_status is not None:
        if processing_status.strip().lower() == "completed" and summary_present:
            return SummaryState.COMPLETED
        return SummaryState.PENDING

    if http_status == 200:
        return SummaryState.COMPLETED if summary_present else SummaryState.PENDING

    return SummaryState.PENDING


def is_summary_ready_signal(webhook_event: str) -> bool:
    """Whether a webhook event is the AI Companion summary readiness signal."""
    return webhook_event == SUMMARY_READY_WEBHOOK
