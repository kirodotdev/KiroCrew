"""The Hooks page's script hooks for a channel turn: ``UserPromptSubmit`` and ``Stop``.

A dashboard turn fires the hooks the user saved on the Hooks page through
``dashboard.chat_turn.turn_spec_hooks._fire``. A channel turn runs through
:class:`kiro_crew.messaging.driver.TurnDriver` instead, so it fires them here,
from the same global store, with the same result handling for these two events:
an exit-0 hook's stdout and an exit-2 hook's ``BLOCKED:`` line go into the
prompt's ``[Hook context]`` block, and any other result only logs.

Only the store's hooks run here. An agent spec's own ``hooks`` stay with the
backend that runs them, and a Stop hook's continuation request is not acted on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_crew.hooks import HOOK_EVENT_STOP, HOOK_EVENT_USER_PROMPT_SUBMIT, get_global_hook_store

logger = logging.getLogger(__name__)


@dataclass
class ScriptHookTurn:
    """Who an inbound message's script hooks run for, and the prompt they match.

    *session_key* is the conversation's own key: per-session attribution and the
    governance scope of each run. *prompt* is the sender's message, not the
    context-built prompt, as on the dashboard.

    One per inbound message, shared by every attempt at it: ``UserPromptSubmit``
    runs once, and a replay after a failed compaction reuses its *prompt_context*.
    """

    session_key: str
    prompt: str
    prompt_context: list[str] | None = field(default=None, compare=False)
    # Bound by the caller to the Stop generation captured on session acquisition.
    # Checked after every pre-stream await, including the hook and monitor claim.
    cancelled: Callable[[], bool] | None = field(default=None, compare=False)

    @classmethod
    def stoppable(cls, sessions: Any, session_key: str, prompt: str) -> ScriptHookTurn:
        """A turn whose ``cancelled`` reads a Stop recorded on *sessions* from now on.

        Takes the session's Stop generation at once, so call it after the session
        is acquired and before anything awaits.
        """
        from kiro_crew.messaging.dispatch_recovery import session_stop_generation

        at_entry = session_stop_generation(sessions, session_key)
        return cls(
            session_key,
            prompt,
            cancelled=lambda: session_stop_generation(sessions, session_key) != at_entry,
        )


async def fire_prompt_submit(turn: ScriptHookTurn) -> list[str]:
    """Fire ``UserPromptSubmit`` once per message; the texts its hook context carries."""
    if turn.prompt_context is None:
        turn.prompt_context = await _prompt_submit(turn)
    return turn.prompt_context


async def _prompt_submit(turn: ScriptHookTurn) -> list[str]:
    store = get_global_hook_store()
    if store is None:
        return []
    try:
        results = await store.fire(
            HOOK_EVENT_USER_PROMPT_SUBMIT, turn.prompt, session_key=turn.session_key
        )
    except Exception:
        logger.warning("UserPromptSubmit hook fire error", exc_info=True)
        return []
    injected: list[str] = []
    for r in results:
        if r.exit_code == 0 and r.stdout:
            injected.append(r.stdout)
        elif r.exit_code == 2 and r.refused:
            injected.append(f"BLOCKED:{r.hook_name}:{r.failure_detail}")
        elif r.exit_code == 2:
            injected.append(
                f"BLOCKED:{r.hook_name}:{r.stderr[:200] if r.stderr else 'hook denied'}"
            )
        elif r.exit_code != 0 and r.stderr:
            logger.warning("Hook %s warning: %s", r.hook_name, r.stderr[-200:])
    return injected


async def with_hook_context(message: str, injected: list[str]) -> str:
    """*message* behind a ``[Hook context]`` block, its structural markers neutralized.

    Hook output is untrusted text placed in front of the trusted prompt, so a
    marker in it must not pass for one of the prompt's own boundaries.
    """
    if not injected:
        return message
    from kiro_crew.context import _neutralize_structural_markers  # circular at import

    block = "\n\n".join(injected)
    safe = await asyncio.to_thread(_neutralize_structural_markers, block)
    return f"[Hook context]\n{safe}\n[End hook context]\n\n{message}"


async def fire_stop(turn: ScriptHookTurn, reply: str) -> None:
    """Fire ``Stop`` with the turn's channel-safe reply as its context."""
    store = get_global_hook_store()
    if store is None:
        return
    try:
        await store.fire(HOOK_EVENT_STOP, reply, session_key=turn.session_key)
    except Exception:
        logger.warning("Stop hook fire error", exc_info=True)
