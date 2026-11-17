"""An ACP ``plan`` session update read as the agent's checklist, on ``AcpClient``.

claude-agent-acp reports Claude Code's checklist as a legacy ``plan`` update to a
client that declares no ``clientCapabilities.plan``, and Crew declares none::

    {"sessionUpdate": "plan",
     "entries": [{"content": ..., "status": "pending" | "in_progress" | "completed",
                  "priority": "medium"}]}

The list is TodoWrite's, or, where Claude Code offers the Task* tools instead
(``TaskCreate``, ``TaskUpdate``, ``TaskList``), the session's task list. Every update
carries the whole list, in the agent's order, and ``entries: []`` means the list is
gone (claude-agent-acp 0.107.0 ``plans.js`` ``checklistPlanUpdate``, ``tools.js``
``planEntries`` / ``taskStateToPlanEntries``). An ``in_progress`` entry's ``content``
is the task's ``activeForm`` ("Running the tests") where it has one. A task created
or completed arrives twice, from the adapter's Task* hook and from the tool result
(``tools.js`` ``changedTaskPlanEntries``), and the slot's change test drops the
repeat.

:func:`todo_event` turns that frame into the ``EVENT_TODO_UPDATE`` kiro-cli's
``todo_list`` tool result becomes (``_dispatch.parse_todo_snapshot``): the same
``{description, tasks: [{id, text, completed}]}`` snapshot, bounded and redacted the
same way, so the dashboard's one todo path (``_on_todo_update``, ``slot.set_todo``,
the ``todo_update`` frame, the task progress bar) carries it. The snapshot also
carries ``read_only: true``. The harness keeps this list, and a person's tick could
not reach it: the dashboard's sync and recovery blocks name kiro-cli's ``todo_list``
tool, the ids are only positions, and an ``in_progress`` entry's text turns into its
``activeForm``. So the dashboard offers no toggle and refuses one.

The bar's ``current`` task is derived by the dashboard as the first entry not
completed, the rule a kiro-cli list has. It is the ``in_progress`` entry only while
that entry is the first unfinished one; an open entry above it is named instead. An
empty list becomes the zero-task snapshot, the clear the ``todo_list`` path sends
when the agent empties its list.

Gated on ``ACP_BACKENDS_PLAN_CHECKLIST``, and only a frame of this client's own
session is read. A native child's TodoWrite list never gets here: one on the child's
own session is consumed as child traffic before the parent's handling
(``client_native_children``), and one stamped with an Agent call on the parent's
session is that subagent's TodoWrite list, never the parent's, so it is declined.
A Task* list is one list every agent of the session shares (``plans.js``
``subagentChecklistPlanId``). A subagent's Task* tool result is stamped with its
Agent call and goes the same way, but the adapter's ``TaskCreated`` and
``TaskCompleted`` hooks send the shared list unstamped on the session's own id
(``v1/acp-agent.js`` ``publishTaskPlan``), so a subagent creating or finishing a
task moves the parent's bar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import redact_text
from kiro_crew.acp.native_child_stamp import parent_tool_use
from kiro_crew.acp.types import (
    EVENT_TODO_UPDATE,
    METHOD_SESSION_UPDATE,
    TODO_TASKS_MAX,
    TODO_TEXT_MAX,
    UPDATE_PLAN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_PLAN_CHECKLIST

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The ``PlanEntry.status`` that marks an entry done. ``pending`` and ``in_progress``
#: are open, and so is any status this module does not know, so an unknown value can
#: never tick a row the agent has not finished.
PLAN_STATUS_COMPLETED = "completed"


def plan_todo_snapshot(update: Any) -> dict[str, Any] | None:
    """The todo snapshot a ``plan`` update's entries make, or None for no ``plan``.

    Ids are the entries' positions from 1, as ``parse_todo_snapshot`` numbers a task
    that carries no id: a plan entry carries none. A non-object entry is skipped and
    keeps its position. The text is redacted before it is cut, and the list is cut
    at ``TODO_TASKS_MAX``. An update with no ``entries`` list is not a plan. The
    snapshot is ``read_only``: the harness, not the dashboard, keeps the list.
    """
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_PLAN:
        return None
    entries = update.get("entries")
    if not isinstance(entries, list):
        return None
    tasks: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        text = entry.get("content") or ""
        if not isinstance(text, str):
            text = str(text)
        tasks.append(
            {
                "id": str(idx + 1),
                "text": redact_text(text)[:TODO_TEXT_MAX],
                "completed": entry.get("status") == PLAN_STATUS_COMPLETED,
            }
        )
        if len(tasks) >= TODO_TASKS_MAX:
            break
    return {"description": "", "tasks": tasks, "read_only": True}


def todo_event(client: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """The ``EVENT_TODO_UPDATE`` a ``plan`` frame of this session carries, or None.

    A frame that names another session is not this session's list. A side effect of
    the frame, never a substitute: the caller keeps reading it.
    """
    if client.backend not in ACP_BACKENDS_PLAN_CHECKLIST:
        return None
    if not msg.is_method(METHOD_SESSION_UPDATE) or not isinstance(msg.params, dict):
        return None
    session_id = msg.params.get("sessionId")
    own = client._session_id
    if isinstance(session_id, str) and session_id and own and session_id != own:
        return None
    update = msg.params.get("update")
    if not isinstance(update, dict) or parent_tool_use(update):
        return None
    todo = plan_todo_snapshot(update)
    if todo is None:
        return None
    return AcpEvent(kind=EVENT_TODO_UPDATE, todo=todo)
