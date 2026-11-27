"""A Crew worker's status and its side panel group (``GET .../crew-workers``).

The status is what the worker's slot row says it does now: an approval or a question
card open, a turn running, or nothing. The group places the worker in the side panel
from real signals only: that status, its ledger item's own progress, and whether its
own loop will wake it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kiro_crew.dashboard.session_activity import SessionActivity, session_execution_active

#: Worker statuses, in the vocabulary the side panel shows.
STATUS_WORKING = "working"
STATUS_IDLE = "idle"
STATUS_NEEDS_APPROVAL = "needs_approval"
#: A worker whose question card is open (one from before it was adopted); it
#: waits on its conductor like a question report.
STATUS_ASKING = "asking"
STATUS_CLOSED = "closed"


def _status(row: Mapping[str, Any], activity: tuple[SessionActivity, ...] = ()) -> str:
    if row.get("pending_approval"):
        return STATUS_NEEDS_APPROVAL
    if row.get("needs_input"):
        return STATUS_ASKING
    if row.get("running") or row.get("subagents_running") or session_execution_active(activity):
        return STATUS_WORKING
    return STATUS_IDLE


#: How the side panel groups workers. A worker never asks the person itself: its
#: questions and blockers go to its conductor (``with_conductor``), which answers or
#: asks the person in its own chat. The one thing a conductor cannot answer is a
#: tool approval (``approval``), which the person resolves on the conductor's
#: approval card. Then what is moving, what has not started, what is finished. The
#: dashboard applies the same rule to live slot rows (``lib/crewWorkers.ts``).
GROUP_APPROVAL = "approval"
GROUP_WORKING = "working"
GROUP_WITH_CONDUCTOR = "with_conductor"
GROUP_WAITING = "waiting"
GROUP_DONE = "done"


def _group(status: str, progress: str = "", looping: bool = False) -> str:
    """The side panel group from real signals only.

    A tool approval is ``approval``; a running turn or attached sub-agent is
    ``working``; a closed tab, a closed ledger item, or a worker that reported
    ``done`` is ``done``; a worker
    that reported ``blocked`` or ``question`` (whatever its ``reason``), or one with
    an open question card, waits on its conductor: ``with_conductor``. An idle
    worker whose own loop will wake it (*looping*, ``ledger_wake.worker_looping``:
    a bounded ``monitor_start`` loop waiting on CI) is still ``working``: the flow
    runs, only the result is outside. Anything else is idle, ``waiting``: having
    replied is not having finished.
    """
    if status == STATUS_NEEDS_APPROVAL:
        return GROUP_APPROVAL
    if status == STATUS_WORKING:
        return GROUP_WORKING
    if status == STATUS_CLOSED or progress == GROUP_DONE:
        return GROUP_DONE
    if status == STATUS_ASKING or progress == GROUP_WITH_CONDUCTOR:
        return GROUP_WITH_CONDUCTOR
    if looping:
        return GROUP_WORKING
    return GROUP_WAITING


def _looping(key: str) -> bool:
    """Whether *key*'s session holds a loop that will wake it, as the ledger reads it."""
    from kiro_crew import ledger_wake
    from kiro_crew.autonudge import get_instance

    return ledger_wake.worker_looping(get_instance(), key)


#: Report statuses that mean the worker waits on its conductor.
_WAITING_REPORTS = frozenset({"blocked", "question"})


def _progress_of(item: Any) -> str:
    """``done`` / ``with_conductor`` / ``""`` from a ledger item's own fields.

    A ``blocked`` or ``question`` report is the conductor's to answer whatever its
    ``reason`` says: a ``needs_human`` reason tells the conductor to ask the person
    in its own chat, never the side panel to show the worker as needing them.
    """
    if getattr(item, "is_terminal", False) or getattr(item, "status", None) == "done":
        return GROUP_DONE
    if getattr(item, "status", None) in _WAITING_REPORTS:
        return GROUP_WITH_CONDUCTOR
    return ""
