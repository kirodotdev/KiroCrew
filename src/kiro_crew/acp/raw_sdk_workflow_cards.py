"""A Claude Code Workflow run's agents as native Subagents cards, on ``AcpClient``.

``acp.raw_sdk_tasks`` hands this module the messages of a ``local_workflow`` task.
A ``task_progress`` of one may carry ``workflow_progress``: the run's rows as Claude
Code holds them, the whole set when it is there (Claude Code 2.1.325 merges each change
into the task's row list by agent index and sends the list). It is left out of a beat
whose rows only moved an agent's progress within ten seconds of the last full set, so
a beat may carry no rows at all. Two row types are read:

* ``workflow_phase`` ``{index, title}``;
* ``workflow_agent`` ``{index, label, state, phaseIndex, phaseTitle, promptPreview,
  model, agentId?, agentType?, attempt, lastToolName?, lastToolSummary?,
  resultPreview?, error?, ...}``, whose ``state`` is ``start`` (queued or running),
  ``progress``, ``done`` or ``error``. An agent still queued has no ``agentId`` yet.

Every other row type (a run's log lines) is skipped. A ``task_notification`` may
carry the closing rows the same way.

Each agent is one :class:`WorkflowAgent`, and one row of the native card roster in
the kiro-cli shape (:func:`roster_row`), a :class:`BackgroundCardRow`, which
``raw_sdk_tasks.pending_events`` lists after the turn's Agent-tool children so
``native_cards._native_subagent_sync`` draws one card per agent:

* the card id is ``<task_id>:<agentId>`` when the first row naming the agent carries
  an ``agentId``, else ``<task_id>:<index>``, and it never changes after that, so an
  agent first seen queued keeps the card it got. The task id keeps the cards of two runs
  apart: a resumed run's cached agents carry the ``agentId`` of the run they come from.
  A run that resumes an ended one keeps its task id, so its card ids carry its own run
  id too, ``<task_id>:<run_id>:<agent>`` (:attr:`WorkflowRun.run_id`);
* the label is ``<phase title>: <label>``, the line Claude Code itself shows for the
  agent; the task is its ``promptPreview``, else the label; the agent name its
  ``agentType``, else the run's name, else ``workflow``; the model the ``model`` it
  runs on;
* ``start`` and ``progress`` keep the card running, its tool line
  ``<lastToolName>: <lastToolSummary>`` when the row names a tool; ``done`` completes
  it, with ``resultPreview`` as its result; ``error`` fails it with the row's
  ``error``; a state this build does not know keeps it running;
* the run's end (:func:`finish`) gives every card still running its final status:
  completed, or failed with ``failed`` or ``stopped``.

A row is marked ``background``: the run outlives the turn that started it, so the
dashboard keeps its card open past that turn's end until the run reports it. The row's
type is what lets the cards read that mark and the ``label``, ``model`` and ``result``
it carries: a roster row off the wire (kiro-cli's ``list_update``) is a plain dict, and
whatever keys it has, the cards read only kiro-cli's. A card listed with a final status
in one turn is not listed again in a later one (:func:`begin_listing`), so a later turn's
fresh card tracker does not draw it twice.

Every display string is cut to ``NATIVE_CHILD_LABEL_CAP`` for storage by the cut
that never ends inside a token (``client_native_children._label``) and is redacted
whole by the cards. The agents of one run are bounded by
:data:`MAX_WORKFLOW_AGENTS`; a row past it is skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.acp.client_native_children import _label

logger = logging.getLogger(__name__)

#: ``workflow_progress`` row types read here.
ROW_PHASE = "workflow_phase"
ROW_AGENT = "workflow_agent"

#: ``workflow_agent.state`` values. ``start`` is a queued or running agent.
STATE_START = "start"
STATE_PROGRESS = "progress"
STATE_DONE = "done"
STATE_ERROR = "error"

#: How a run ended, as ``raw_sdk_tasks`` names it.
END_COMPLETED = "completed"
END_FAILED = "failed"
END_STOPPED = "stopped"

#: Card status types the native cards read (``_native_subagent_sync``).
_WORKING = "working"
_COMPLETED = "completed"
_FAILED = "failed"

#: The agent name a card shows when neither the row nor the run names one.
DEFAULT_AGENT_NAME = "workflow"

#: Agents and phases kept per run, and rows read per message: a run is bounded
#: however many rows a message carries. A row past a bound is counted
#: (:attr:`WorkflowRun.refused`) and said once per run.
MAX_WORKFLOW_AGENTS = 256
MAX_WORKFLOW_PHASES = 64
MAX_ROWS_READ = 1024
#: The largest row index or phase index read.
_MAX_INDEX = 1_000_000
#: An ``agentId`` usable as a card id.
_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@dataclass
class WorkflowAgent:
    """One agent of a run and the card it is drawn as."""

    card_id: str
    label: str = ""
    task: str = ""
    agent: str = ""
    model: str = ""
    result: str = ""
    status: dict[str, str] = field(default_factory=lambda: {"type": _WORKING, "message": ""})
    #: Listed with a final status this turn, and in an earlier turn.
    final_listed: bool = False
    settled: bool = False

    @property
    def final(self) -> bool:
        return self.status["type"] != _WORKING


@dataclass
class WorkflowRun:
    """The agents of one ``local_workflow`` task, by row index.

    ``run_id`` is empty for a task's first run; a run that resumes an ended one of the
    same task (``raw_sdk_tasks``) has its own, and its card ids carry it.
    """

    task_id: str
    name: str = ""
    run_id: str = ""
    phases: dict[int, str] = field(default_factory=dict)
    agents: dict[int, WorkflowAgent] = field(default_factory=dict)
    ended: bool = False
    #: Rows the newest message carried past a bound, and whether that was said.
    refused: int = 0
    refusal_said: bool = False


def _index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_INDEX:
        return None
    return value


def card_text(value: Any) -> str:
    """*value* stripped and bounded for a card, or "" for a non-string."""
    return _label(value.strip()) if isinstance(value, str) else ""


def _tool_line(row: dict[str, Any]) -> str:
    """``<lastToolName>: <lastToolSummary>``, either alone, or ``""``."""
    name, summary = card_text(row.get("lastToolName")), card_text(row.get("lastToolSummary"))
    return _label(f"{name}: {summary}") if name and summary else name or summary


def _status(row: dict[str, Any]) -> dict[str, str]:
    state = row.get("state")
    if state == STATE_DONE:
        return {"type": _COMPLETED, "message": ""}
    if state == STATE_ERROR:
        return {"type": _FAILED, "message": card_text(row.get("error")) or END_FAILED}
    return {"type": _WORKING, "message": _tool_line(row)}


def _merge_agent(run: WorkflowRun, row: dict[str, Any]) -> bool | None:
    """Merge one ``workflow_agent`` row into its agent; True when the row was taken,
    None when a new agent was refused at :data:`MAX_WORKFLOW_AGENTS`."""
    index = _index(row.get("index"))
    if index is None:
        return False
    agent = run.agents.get(index)
    if agent is None:
        if len(run.agents) >= MAX_WORKFLOW_AGENTS:
            return None
        agent_id = row.get("agentId")
        named = isinstance(agent_id, str) and _AGENT_ID.match(agent_id) is not None
        run_part = f"{run.task_id}:{run.run_id}" if run.run_id else run.task_id
        card_id = f"{run_part}:{agent_id if named else index}"
        agent = run.agents[index] = WorkflowAgent(card_id=card_id)
    label = card_text(row.get("label"))
    phase_index = _index(row.get("phaseIndex"))
    phase = card_text(row.get("phaseTitle")) or (
        run.phases.get(phase_index, "") if phase_index is not None else ""
    )
    agent.label = _label(f"{phase}: {label}") if phase and label else label or phase or agent.label
    agent.task = card_text(row.get("promptPreview")) or agent.task or agent.label or run.name
    agent.agent = card_text(row.get("agentType")) or agent.agent or run.name or DEFAULT_AGENT_NAME
    agent.model = card_text(row.get("model")) or agent.model
    agent.result = card_text(row.get("resultPreview")) or agent.result
    agent.status = _status(row)
    return True


def merge_rows(run: WorkflowRun, rows: Any) -> bool:
    """Merge a message's ``workflow_progress`` into *run*; True when an agent row was taken.

    Phases first, so an agent row names its phase whichever order the rows came in.
    A run that has ended takes no rows: its cards have their final status.
    """
    if run.ended or not isinstance(rows, list):
        return False
    read = [row for row in rows[:MAX_ROWS_READ] if isinstance(row, dict)]
    refused = max(0, len(rows) - MAX_ROWS_READ)
    for row in read:
        if row.get("type") == ROW_PHASE:
            index, title = _index(row.get("index")), card_text(row.get("title"))
            if index is None or not title:
                continue
            if index in run.phases or len(run.phases) < MAX_WORKFLOW_PHASES:
                run.phases[index] = title
            else:
                refused += 1
    taken = False
    for row in read:
        if row.get("type") == ROW_AGENT:
            merged = _merge_agent(run, row)
            refused += merged is None
            taken = bool(merged) or taken
    _note_refused(run, refused)
    return taken


def _note_refused(run: WorkflowRun, refused: int) -> None:
    """Keep the newest message's count of rows past a bound, said once per run."""
    run.refused = refused
    if refused and not run.refusal_said:
        run.refusal_said = True
        logger.warning(
            "Workflow task %s: %d progress row(s) past the bounds of %d agents, %d phases "
            "and %d rows a message were not drawn as cards",
            run.task_id,
            refused,
            MAX_WORKFLOW_AGENTS,
            MAX_WORKFLOW_PHASES,
            MAX_ROWS_READ,
        )


def finish(run: WorkflowRun, ended: str, rows: Any = None) -> bool:
    """End *run* as *ended*, after merging its closing *rows*; True when a card moved.

    Every agent still running gets the run's outcome: completed, or failed with
    ``failed`` or ``stopped``. A second end changes nothing.
    """
    if run.ended:
        return False
    moved = merge_rows(run, rows)
    run.ended = True
    outcome = (
        {"type": _COMPLETED, "message": ""}
        if ended == END_COMPLETED
        else {"type": _FAILED, "message": END_STOPPED if ended == END_STOPPED else END_FAILED}
    )
    for agent in run.agents.values():
        if not agent.final:
            agent.status = dict(outcome)
            moved = True
    return moved


def begin_listing(run: WorkflowRun) -> None:
    """Settle the agents listed with a final status in the turn that just ended."""
    for agent in run.agents.values():
        if agent.final_listed:
            agent.settled = True
            agent.final_listed = False


class BackgroundCardRow(dict):
    """A native card roster row Crew built from a harness's background-task messages.

    The native cards read its ``background``, ``label``, ``model`` and ``result`` and
    no other row's (``native_cards._native_subagent_sync``): no row read off the wire
    is of this type, so none can carry them in.
    """


def roster_row(run: WorkflowRun, agent: WorkflowAgent) -> BackgroundCardRow:
    """*agent* as one native card roster row; listing it final marks it so."""
    if agent.final:
        agent.final_listed = True
    return BackgroundCardRow(
        sessionId=agent.card_id,
        sessionName=agent.task,
        agentName=agent.agent,
        initialQuery=agent.task,
        status=dict(agent.status),
        label=agent.label,
        model=agent.model,
        result=agent.result,
        background=True,
    )


def roster_rows(run: WorkflowRun) -> list[BackgroundCardRow]:
    """The rows of *run*'s agents not settled in an earlier turn, in row order."""
    return [
        roster_row(run, agent)
        for _index_key, agent in sorted(run.agents.items())
        if not agent.settled and agent.task
    ]
