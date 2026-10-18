"""The vocabulary the runner adapters share: lanes, row ids, row params, modes and refusals.

What a runner row is called and how it is filed: the lane a run queues in
(:func:`lane_for`), the row-id scheme and the owner routing the cancel adapter reads
(:func:`step_task_id`, :func:`owner_of`), the ``params`` keys the admission stamps on a
row, the lane modes, the state sets a cancel or an answer is fenced on, the admit timing
bounds, the bound on a durable operator answer, and the three refusals an admission raises.

``kiro_crew.taskq.adapters.runner`` stays the import path of every name here. The runner's
owners import them from this module because an owner never imports that facade, and each
refusal names the facade as its ``__module__``: a traceback renders a raised class as
``module.qualname``, and that text reaches logs and a run's recorded error.
"""

from __future__ import annotations

from typing import Any

from ..lanes import SYSTEM_LANE, lane_key_for
from ..migrate import TASKRUNNER_ID_PREFIX
from ..model import KIND_TASKRUNNER_STEP, KIND_WORKFLOW_AGENT, QUEUED, WAITING_INPUT

#: The lane every cron- and hook-launched run shares (RFC §13 Q5, weight 1).
LANE_SYSTEM = SYSTEM_LANE
#: Launch sources that have no watching session and therefore no lane of their own.
SYSTEM_SOURCES: frozenset[str] = frozenset({"cron", "hook"})

WORKFLOW_ID_PREFIX = "workflow:"

MODE_AIMD = "aimd"
MODE_FIXED = "fixed"

#: Kinds the runner's recovery adapter (``adopt_orphaned_rows``) rebuilds from a stored row.
RUNNER_RECOVERY_ADAPTERS: frozenset[str] = frozenset({KIND_TASKRUNNER_STEP, KIND_WORKFLOW_AGENT})

#: The only state a row an ``admit`` never claimed can be cancelled from, and
#: the only one the queued-orphan sweep may end: ``queued`` says no ``claim``
#: ever took the row, so nothing has run under it. A row that has LEFT it
#: belongs to whoever claimed it, whatever an earlier read said.
UNCLAIMED: frozenset[str] = frozenset({QUEUED})

#: The only wait an operator's answer may end (``answer_input``). An answer
#: answers ONE question, and the other three waits in :data:`model.WAITING` are
#: different questions, so a row that has moved to one of them is refused rather
#: than woken by an answer to a question that is already over.
INPUT_WAIT_ONLY: frozenset[str] = frozenset({WAITING_INPUT})

#: Row param naming the lane the row queues in.
PARAM_LANE = "lane"
#: Row param: the runner may re-run the interrupted step from its checkpoint.
PARAM_SAFE_RETRY = "safe_retry"
#: Row param: the ``TaskStore.incarnation`` whose ``accept`` wrote the row. The
#: adoption sweep needs to tell a row a LIVE coroutine of this process accepted
#: from one a dead incarnation left, and for a row that was never claimed the
#: lease cannot say: ``claim`` is what takes one.
PARAM_ACCEPTED_BY = "accepted_by"

_DEFAULT_ADMIT_WAIT_SECS = 30.0
#: Bound on one sleep inside ``admit``: a deferred row re-checks the store at
#: least this often so a cancel landing during the wait is seen promptly.
_MAX_ADMIT_SLEEP_SECS = 30.0

#: Bound on the operator answer carried on a row's ``wake`` event, so one
#: pasted document cannot become the store's size.
_MAX_DURABLE_ANSWER_CHARS = 2000
#: Wake-event key that says the durable copy is SHORT of the answer given. The
#: marker is what makes the bound safe: a replay reading it refuses instead of
#: feeding a model a cut answer as if it were the whole one.
ANSWER_TRUNCATED = "answer_truncated"


def _answer_detail(answer: str) -> dict[str, Any]:
    """The ``wake`` event detail carrying an operator's answer.

    The stored copy is bounded; when the bound cuts it, the event SAYS so and
    names the length that was given. Every reader of this copy is a REPLAY --
    a rebuilt step, or one answered through another incarnation
    (:meth:`RunnerAdmission.recorded_answer`) -- and a silently cut answer is
    one the model cannot tell from the operator's own words.
    """
    text = str(answer)
    detail: dict[str, Any] = {"answer": text[:_MAX_DURABLE_ANSWER_CHARS]}
    if len(text) > _MAX_DURABLE_ANSWER_CHARS:
        detail[ANSWER_TRUNCATED] = True
        detail["answer_chars"] = len(text)
    return detail


def lane_for(session_key: str | None, source: str = "") -> str:
    """The lane a run queues in: ``taskq.lanes.lane_key_for`` (the ONE lane
    key policy: automation keys and the empty key are ``system``), with the
    runner's launch *source* as the extra fact the key alone cannot carry --
    a cron- or hook-launched run is a ``system`` root whatever its key says.
    """
    if str(source or "") in SYSTEM_SOURCES:
        return LANE_SYSTEM
    return lane_key_for(session_key)


def step_task_id(run_id: str, index: int) -> str:
    """Row id of one TaskRunner step; the run row is ``taskrunner:<run_id>``."""
    return f"{TASKRUNNER_ID_PREFIX}{run_id}:task{int(index)}"


def run_task_id(run_id: str) -> str:
    return f"{TASKRUNNER_ID_PREFIX}{run_id}"


def workflow_task_id(run_id: str, call_no: int) -> str:
    return f"{WORKFLOW_ID_PREFIX}{run_id}:agent{int(call_no)}"


OWNER_TASKRUNNER = "taskrunner"
OWNER_WORKFLOW = "workflow"


def owner_of(task_id: str) -> tuple[str, str] | None:
    """``(owner, run_id)`` for a runner row id, else None.

    ``taskrunner:<run>`` and ``taskrunner:<run>:task<N>`` belong to the
    TaskRunner run ``<run>``; ``workflow:<run>:agent<N>`` to the workflow run
    ``<run>``. A ``~N`` re-run suffix on the last segment is dropped. The
    cancel adapter routes on this: a step or an agent call is one unit of its
    run, so cancelling it means cancelling that run.
    """
    if task_id.startswith(TASKRUNNER_ID_PREFIX):
        rest = task_id[len(TASKRUNNER_ID_PREFIX) :]
        run_id = rest.split(":", 1)[0].split("~", 1)[0]
        return (OWNER_TASKRUNNER, run_id) if run_id else None
    if task_id.startswith(WORKFLOW_ID_PREFIX):
        rest = task_id[len(WORKFLOW_ID_PREFIX) :]
        run_id = rest.rsplit(":", 1)[0] if ":" in rest else rest.split("~", 1)[0]
        return (OWNER_WORKFLOW, run_id) if run_id else None
    return None


class RunnerAdmissionRefused(RuntimeError):
    """The row could not be written: NOTHING was accepted."""


class RunnerTaskCancelled(RuntimeError):
    """The row was cancelled (or ended) before the runner could start it."""


class RunnerLaneSelfBlocked(RunnerAdmissionRefused):
    """Every slot on the lane is held by one of the asking row's own ANCESTORS.

    A refusal, never a park: :meth:`RunnerLane.acquire` has no timeout, so a
    descendant in that position waits for a release only its own ancestry can
    make, and an ancestor blocked on the descendant never makes it. A
    :class:`RunnerAdmissionRefused` subclass so a caller that already reports a
    refused admission reports this one too.

    Raised ONLY when the ancestry holds every slot -- one unrelated holder is a
    release the FIFO queue will hand on, so that wait is parked as before. The
    sanctioned way to run nested work is for the ancestor to YIELD first
    (``Admitted.release_slot`` / ``reacquire_slot``, the same pair
    ``yield_dependency`` uses): the lane is FIFO, so the freed slot reaches the
    descendant once the waiters that arrived before it have had theirs.
    """


# A traceback renders a raised class as ``module.qualname``; these render under the
# runner's import path, where callers, logs and docs name them.
RunnerAdmissionRefused.__module__ = "kiro_crew.taskq.adapters.runner"
RunnerTaskCancelled.__module__ = "kiro_crew.taskq.adapters.runner"
RunnerLaneSelfBlocked.__module__ = "kiro_crew.taskq.adapters.runner"
