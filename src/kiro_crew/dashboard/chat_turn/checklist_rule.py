"""The rule that asks a dashboard chat's agent to keep a step list the chat can show.

The chat's step pill (``TaskProgressBar``) renders the agent's ``todo_list``
results, so it shows nothing unless the agent keeps a list. The rule is a turn
prefix rather than a line of ``config/prompt.md`` because that prompt is the
agent's for every surface: a cron run, a sub-agent and a loop wake read it too,
and none of them has a person watching a pill. Prepended in
``turn_dispatch_gates``, model input only, like the checklist sync block.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

CHECKLIST_RULE = (
    "[Progress checklist]\n"
    "When this request takes 3 or more steps, call todo_list `create` with those "
    "steps before you start the first one, then call `complete` for each step as "
    "soon as it is done, so the person watching this chat sees which step is "
    "running. Skip the list for a shorter request.\n"
    "[End progress checklist]"
)


def checklist_rule_for(turn: _ChatTurn, client: Any) -> str:
    """:data:`CHECKLIST_RULE` for a person's dashboard message, ``""`` for any other turn.

    A person's message is a new depth-0 turn a human sent (``_directive_user_origin``
    and the ``user`` actor, the same pair the dispatch gates read for a run's start),
    so a cron script's ``send_to_session``, a loop wake and a sub-agent delivery get
    nothing. It must also have been typed in the dashboard: a message typed in a
    linked channel (a Slack thread, a Telegram, Teams or Discord hand-off) carries
    ``_directive_channel_origin``, and its reader sees no pill. The harness must
    hold the tool (``has_todo_list_tool``).
    """
    # Imported at call time: tests rebind ``capabilities_of`` on the runner.
    from kiro_crew.agent_sdk.capabilities import capabilities_of

    if turn._directive_channel_origin or not capabilities_of(client).has_todo_list_tool:
        return ""
    if turn._prompt_depth != 0 or turn._directive_user_origin is not True:
        return ""
    if turn._crew_log_actor != "user":
        return ""
    return CHECKLIST_RULE
