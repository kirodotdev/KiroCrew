"""An agent-authored task list left unfinished when the turn ended.

kiro-cli's ``todo_list`` tool gives the agent a checklist, and the dashboard
mirrors it above the composer (``_ChatSlot.set_todo``). Mirroring was all the
host did: a turn could end with items still open and nothing noticed. The pill
kept reading "2 of 3" indefinitely, and the next turn carried no sign of the open
work, so the agent answered follow-ups as though the plan were finished.

This module holds the pure pieces of the fix, so the gate and the wording can be
tested without driving ``_run_chat``:

* :func:`open_plan_from_todo` reads the slot's stored snapshot into an
  :class:`OpenPlan` (``None`` when nothing is open).
* :func:`should_queue_plan_reconcile` is the end-of-turn gate for ONE bounded
  follow-up turn that asks the agent to reconcile the list.
* :func:`build_plan_reconcile_body` and :func:`build_open_plan_context` are the
  two texts the model reads.

Both texts carry only integers computed here: counts and 1-based positions. The
task TEXT is agent-authored prose that a tool result or fetched page could have
shaped, and these strings land on channels the model trusts (a runner-authored
continuation, the per-turn context rail). The agent already has the item wording
in its own conversation history, so repeating it adds an injection surface and no
information.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Positions listed before the rest collapse to "+N more". Bounds the text on a
#: long plan; the counts still report the true total.
OPEN_POSITIONS_SHOWN_MAX = 10


@dataclass(frozen=True)
class OpenPlan:
    """How much of the agent's task list is still open.

    ``open_positions`` are 1-based positions in the list as stored, in order,
    uncapped (the caps are applied when rendering).
    """

    completed: int
    total: int
    open_positions: tuple[int, ...]

    @property
    def open_count(self) -> int:
        return len(self.open_positions)


def open_plan_from_todo(todo: Mapping[str, Any] | None) -> OpenPlan | None:
    """The open part of a stored todo snapshot, or ``None`` when nothing is open.

    Accepts the shape ``_ChatSlot`` stores (``{"description", "tasks": [...]}``)
    and tolerates malformed entries the same way ``todo_payload`` does: a
    non-dict task is skipped rather than counted.
    """
    if not isinstance(todo, Mapping):
        return None
    raw_tasks = todo.get("tasks")
    if not isinstance(raw_tasks, list):
        return None
    tasks = [t for t in raw_tasks if isinstance(t, Mapping)]
    open_positions = tuple(i for i, t in enumerate(tasks, start=1) if not t.get("completed"))
    if not open_positions:
        return None
    return OpenPlan(
        completed=len(tasks) - len(open_positions),
        total=len(tasks),
        open_positions=open_positions,
    )


def _positions_text(plan: OpenPlan) -> str:
    shown = plan.open_positions[:OPEN_POSITIONS_SHOWN_MAX]
    text = ", ".join(str(p) for p in shown)
    hidden = plan.open_count - len(shown)
    if hidden > 0:
        text += f", +{hidden} more"
    noun = "item" if plan.open_count == 1 else "items"
    return f"{noun} {text}"


def build_plan_reconcile_body(plan: OpenPlan) -> str:
    """The instruction for the one follow-up turn after a plan was left open.

    Written so the continuation cannot become authority the user never gave. The
    agent is asked to do only what is still within the request, to leave anything
    that needs the user alone, and to say why an item is not done. It is not told
    to finish at any cost. An item the agent parked on purpose (waiting for
    approval, say) must end as a stated reason, not as an action the user was
    still deciding on.
    """
    return (
        f"Your turn ended with {plan.open_count} of {plan.total} items in your own "
        f"task list (todo_list) still open ({_positions_text(plan)}). Reconcile the "
        "list with what actually happened before you stop:\n"
        "- An open item that is still part of the user's request and needs no "
        "decision from them: do it now, then mark it complete.\n"
        "- An item that is already done: mark it complete.\n"
        "- An item that needs the user's decision, approval or input, or is no "
        "longer needed: do NOT do it. Leave it open or remove it, and say in one "
        "line why it is not done.\n"
        "Do not repeat your previous answer, and do not start work the user did "
        "not ask for."
    )


def build_open_plan_context(plan: OpenPlan) -> str:
    """The per-turn ``[OPEN PLAN]`` context line while items remain open.

    Reference, not an instruction to resume. The new message may have moved on,
    and the right response then is to clear the stale list, not to finish it.
    """
    return (
        f"[OPEN PLAN] Your task list (todo_list) has {plan.open_count} of "
        f"{plan.total} items still open ({_positions_text(plan)}) from earlier "
        "work. If the current request continues that work, finish or update "
        "those items. If it supersedes them, clear or update the list so it does "
        "not show stale work.\n\n"
    )


def should_queue_plan_reconcile(
    *,
    plan: OpenPlan | None,
    todo_touched_this_turn: bool,
    ended_normally: bool,
    user_stopped: bool,
    needs_reset: bool,
    already_used: bool,
    is_reconcile_turn: bool,
    is_monitor_wake: bool,
    in_stage_execution: bool,
    plan_gate_armed: bool,
    other_continuation_queued: bool,
    user_followup_queued: bool,
    pending_steers: bool,
    handed_to_user: bool,
) -> bool:
    """Whether a turn that just ended owes one plan-reconcile follow-up.

    All of these must hold:

    * something is still open, and the agent used its todo tool THIS turn. A
      list untouched for many turns is stale state, not this turn's dropped
      work. The ``[OPEN PLAN]`` context line covers that case without spending
      a turn.
    * the turn ended normally (``end_turn``). A Stop, a pending reset, or an
      error path each own what happens next.
    * the one-shot budget is unspent and this is not itself a reconcile turn.
      The budget re-arms only on a genuine new prompt, so a reconcile turn
      that leaves items open lands as it is and cannot loop.
    * no other runner continuation is already queued (refusal recovery, a Stop
      hook, a promise-only nudge...). That turn continues the work anyway, and
      two stacked continuations would race each other.
    * no user follow-up or unconsumed steer is waiting. The user's next words
      win over our nudge.
    * the agent did not hand the turn to the user on purpose: an
      ``[OPTIONS:]`` footer or an open question card. Items parked behind that
      decision are correctly open.
    * not a monitor wake, a plan-stage execution, or a plan turn waiting on its
      own approval gate. Those have their own drivers, and an extra turn
      corrupts their bookkeeping or bills each cycle twice.
    """
    if plan is None or not todo_touched_this_turn:
        return False
    if not ended_normally or user_stopped or needs_reset:
        return False
    if already_used or is_reconcile_turn:
        return False
    if is_monitor_wake or in_stage_execution or plan_gate_armed:
        return False
    if other_continuation_queued or user_followup_queued or pending_steers:
        return False
    return not handed_to_user
