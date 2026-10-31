"""How a sub-agent run ended, as one closed vocabulary.

:class:`ChildOutcome` is the fine-grained ending the terminal report computes once
and carries on ``subagent_done`` as ``child_outcome``. Its :attr:`~ChildOutcome.coarse`
value is the three-way ``outcome`` (``completed`` / ``failed`` / ``stopped``) that
the task queue, the panels, the metrics series and the crew log already read, so
adding a finer ending changes none of them.
"""

from __future__ import annotations

from enum import Enum


class ChildOutcome(str, Enum):
    """The ending of one sub-agent run.

    ``COMPLETED``: the run ended normally with an answer.
    ``PARTIAL``: the run ended without an error, but its answer is incomplete, for
    example a turn that called tools and then wrote no reply.
    ``INTERRUPTED_BY_PARENT``: a deliberate stop from outside the run, which is a
    user Stop, the parent's own Stop or the parent conversation ending.
    ``REAPED``: the reaper ended the run for missing a deadline (execution,
    startup, start queue or first prompt).
    ``FAILED``: the run's own error.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    INTERRUPTED_BY_PARENT = "interrupted_by_parent"
    REAPED = "reaped"
    FAILED = "failed"

    @property
    def coarse(self) -> str:
        """The three-way ``outcome`` this ending is reported as."""
        return _COARSE[self]


_COARSE: dict[ChildOutcome, str] = {
    ChildOutcome.COMPLETED: "completed",
    ChildOutcome.PARTIAL: "completed",
    ChildOutcome.INTERRUPTED_BY_PARENT: "stopped",
    ChildOutcome.REAPED: "failed",
    ChildOutcome.FAILED: "failed",
}


def classify_child_outcome(
    *, user_stopped: bool, error: str, reaper_wrote_error: bool, partial: bool
) -> ChildOutcome:
    """The ending a run record describes, decided in one fixed order.

    A stop wins over an error, as the three-way ``outcome`` always has: a stopped
    run's error text names what the stop could not do, not a failure of the run.
    An error is ``reaped`` only when the reaper's own record wrote it; an error
    the run recorded first stays ``failed`` even when a reap later tore the run
    down. ``partial`` without an error is an incomplete answer, still reported
    as ``completed``.
    """
    if user_stopped:
        return ChildOutcome.INTERRUPTED_BY_PARENT
    if error:
        return ChildOutcome.REAPED if reaper_wrote_error else ChildOutcome.FAILED
    if partial:
        return ChildOutcome.PARTIAL
    return ChildOutcome.COMPLETED
