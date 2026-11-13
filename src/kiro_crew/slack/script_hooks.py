"""Who the Hooks page's script hooks run for on a Slack turn.

The hooks are the operator's own commands. On the shared channel pipeline they run
only for the operator's own conversation (:mod:`kiro_crew.messaging.dispatch`); this is
the same rule for Slack, which admits the operator and trusted bots (``events.py``).
A hook runs for a message the operator sent in a 1:1 DM. A channel, a group DM or a
trusted bot's message runs none.

The transport path (``transport_dispatch.py``) hands a :class:`ScriptHookTurn` to its
``TurnDriver``. The native path (``handler_runtime/turn.py``) streams without one, so
it fires the same two events through the helpers below.
"""

from __future__ import annotations

from collections.abc import Callable

from kiro_crew.messaging.script_hooks import (
    ScriptHookTurn,
    fire_prompt_submit,
    fire_stop_if_succeeded,
    with_hook_context,
)


def runs_script_hooks(channel: str, sender_id: str) -> bool:
    """Whether *sender_id*'s message in *channel* runs the Hooks page's hooks.

    A Slack 1:1 DM's id starts with ``D``; a channel's with ``C``, a group DM's with
    ``G``. ``is_allowed_user`` is the operator check ``events.py`` admits on, which a
    trusted bot does not pass. Read through the handler facade, which holds the
    owner id it compares against.
    """
    # circular import: slack.handler imports the turn modules that import this one.
    from kiro_crew.slack import handler

    return channel.startswith("D") and handler.is_allowed_user(sender_id)


def native_hook_turn(
    channel: str,
    sender_id: str,
    session_key: str,
    prompt: str,
    cancelled: Callable[[], bool],
) -> ScriptHookTurn | None:
    """The native Slack turn's hooks, or ``None`` when this message runs none.

    *cancelled* is the turn's own Stop check, bound to the Stop generation the
    native handler captured when it took the session (a replay keeps the first
    attempt's).
    """
    if not runs_script_hooks(channel, sender_id):
        return None
    return ScriptHookTurn(session_key, prompt, cancelled=cancelled)


async def prompt_with_hooks(hook_turn: ScriptHookTurn, prompt: str, message: str) -> str:
    """Fire ``UserPromptSubmit`` once for this message; *message* behind its context."""
    hook_turn.prompt = prompt
    return await with_hook_context(message, await fire_prompt_submit(hook_turn))


async def stop_hooks_after(hook_turn: ScriptHookTurn, stop_reason: str, reply: str) -> None:
    """Fire ``Stop`` with *reply* when the turn ended ``succeeded``, as the driver does."""
    await fire_stop_if_succeeded(hook_turn, stop_reason, reply)
