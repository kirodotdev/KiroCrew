"""How a sub-agent run ended, as one closed vocabulary.

:class:`SubagentEnding` is the fine-grained ending computed from the run record
(``SubagentInfo.ending``); the parent's completion announce reads it, and only
``outcome`` goes out on the wire. Its :attr:`~SubagentEnding.coarse` value is the
three-way ``outcome`` (``completed`` / ``failed`` / ``stopped``) that
the task queue, the panels, the metrics series and the crew log already read, so
adding a finer ending changes none of them.
"""

from __future__ import annotations

from enum import Enum


class SubagentEnding(str, Enum):
    """The ending of one sub-agent run.

    ``COMPLETED``: the run ended normally with an answer.
    ``PARTIAL``: the run ended without an error, but its answer is incomplete, for
    example a turn that called tools and then wrote no reply.
    ``INTERRUPTED_BY_PARENT``: a deliberate stop from outside the run, which is a
    user Stop, the parent's own Stop or the parent conversation ending.
    ``REAPED``: a deadline or the reaper ended the run: its own execution
    timeout, or a reap for a deadline, a startup or start-queue limit, a silent
    first prompt or a spawn approval that never came.
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


_COARSE: dict[SubagentEnding, str] = {
    SubagentEnding.COMPLETED: "completed",
    SubagentEnding.PARTIAL: "completed",
    SubagentEnding.INTERRUPTED_BY_PARENT: "stopped",
    SubagentEnding.REAPED: "failed",
    SubagentEnding.FAILED: "failed",
}


def classify_ending(
    *, user_stopped: bool, error: str, deadline_ending: bool, partial: bool
) -> SubagentEnding:
    """The ending a run record describes, decided in one fixed order.

    A stop wins over an error, as the three-way ``outcome`` always has: a stopped
    run's error text names what the stop could not do, not a failure of the run.
    An error is ``reaped`` only when a deadline or the reaper wrote it; an error
    the run recorded first stays ``failed`` even when a reap later tore the run
    down. ``partial`` without an error is an incomplete answer, still reported
    as ``completed``.
    """
    if user_stopped:
        return SubagentEnding.INTERRUPTED_BY_PARENT
    if error:
        return SubagentEnding.REAPED if deadline_ending else SubagentEnding.FAILED
    if partial:
        return SubagentEnding.PARTIAL
    return SubagentEnding.COMPLETED
