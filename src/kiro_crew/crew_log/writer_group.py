"""Group commit for the crew log writer: which queued appends may share one write.

:class:`GroupPart` is what an APPEND job carries to be written together with its
neighbours, and :func:`group_run` picks the run. :mod:`kiro_crew.crew_log.writer` does
the rest (commit, land, retain, drop) and re-exports :class:`GroupPart`. Split out of
the writer only to keep that file under the line cap; nothing else imports it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover -- typing only
    from kiro_crew.crew_log.writer import _Pending


@dataclass(frozen=True)
class GroupPart:
    """How an APPEND job may share one durable write with its neighbours.

    A run of consecutive jobs of one unit whose parts carry an equal ``key`` is written
    by ONE ``commit(items)`` call -- the ``item`` of each, in order -- instead of each
    job's own ``run``. ``commit`` must write them all or none (one ``write()`` and one
    ``fsync`` under one lock) and return one result per item, in order. Then, for each
    job in order, ``landed(result)`` runs and then the job's ``after``: so nothing a job
    publishes and no acknowledgement runs before the whole run is durable.

    A ``commit`` that raises is treated exactly like a single job's ``run``: a failure
    retains the whole run from its first job; a refusal (decided before any byte is
    written) falls back to running each job's own ``run`` alone, so one bad entry is
    still the only casualty. ``run`` is also what the inline path and a lone job use.
    """

    key: object
    item: Any
    commit: Callable[[list[Any]], list[Any]]
    landed: Callable[[Any], None]


def group_run(jobs: "Sequence[_Pending]", index: int, cap: int) -> int:
    """How many jobs from *index* on may share one group commit. At least 1.

    Consecutive jobs only, with an equal :attr:`GroupPart.key` and no loss marker among
    them, at most *cap*: a job without a part is a barrier, so everything before it is
    durable before it runs and nothing after it runs first.
    """
    first = jobs[index].group
    if first is None or jobs[index].loss is not None:
        return 1
    end = index + 1
    stop = min(len(jobs), index + cap)
    while end < stop:
        part = jobs[end].group
        if part is None or jobs[end].loss is not None or part.key != first.key:
            break
        end += 1
    return end - index
