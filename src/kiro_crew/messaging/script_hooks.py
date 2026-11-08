"""A channel turn's prompt-side script hooks: ``AgentSpawn``, ``UserPromptSubmit`` and ``Stop``.

A dashboard turn fires the hooks the user saved on the Hooks page through
``dashboard.chat_turn.turn_spec_hooks._fire``. A channel turn runs through
:class:`kiro_crew.messaging.driver.TurnDriver` instead, so it fires them here,
from the same global store, with the same result handling for these events:
an exit-0 hook's stdout and an exit-2 hook's ``BLOCKED:`` line go into the
prompt's ``[Hook context]`` block, and any other result only logs.

An agent spec's own ``hooks`` ride the same fires, on a backend whose harness
never receives them (KAS, goose, opencode, and the events that cannot refuse a
call on claude; see :func:`kiro_crew.agent_sdk.spec_hooks.turn_spec_hooks`). The
store's hooks run only for a turn its :class:`ScriptHookTurn` says they run for
(``store_hooks``, the operator's own conversation), and so do the user's
standalone ``~/.kiro/hooks`` files, which the caller leaves out of *spec* on any
other turn (:class:`kiro_crew.messaging.tool_hooks.ChannelTurnHooks`); the spec's
run on every channel turn that admits tools, as the harness that would otherwise
run them does. The tool events are :mod:`kiro_crew.messaging.tool_hooks`. A Stop
hook's continuation request is not acted on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.hooks import (
    HOOK_EVENT_AGENT_SPAWN,
    HOOK_EVENT_STOP,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHookStore,
    get_global_hook_store,
)

if TYPE_CHECKING:
    from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
    from kiro_crew.hooks import ScriptHookResult

logger = logging.getLogger(__name__)


@dataclass
class ScriptHookTurn:
    """Who an inbound message's script hooks run for, and the prompt they match.

    *session_key* is the conversation's own key: per-session attribution and the
    governance scope of each run. *prompt* is the sender's message, not the
    context-built prompt, as on the dashboard.

    One per inbound message, shared by every attempt at it: ``UserPromptSubmit``
    runs once, and a replay after a failed compaction reuses its *prompt_context*.

    *store_hooks* false is a turn the Hooks page's hooks, and the user's
    standalone hook files, do not run for (a room, a guest): only an agent
    spec's own hooks fire through it, and its *prompt* is the one the backend
    receives, which is what that spec's harness hands its own hook.
    """

    session_key: str
    prompt: str
    prompt_context: list[str] | None = field(default=None, compare=False)
    # Bound by the caller to the Stop generation captured on session acquisition.
    # Checked after every pre-stream await, including the hook and monitor claim.
    cancelled: Callable[[], bool] | None = field(default=None, compare=False)
    store_hooks: bool = field(default=True, compare=False)

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


def observer_store(
    turn: ScriptHookTurn, spec: TurnSpecHooks | None = None
) -> ScriptHookStore | None:
    """The store *turn* fires an event that cannot refuse a tool call through.

    The Hooks page's own store when its hooks run for *turn*; otherwise, when
    *spec* has hooks, an empty snapshot store, so the spec's hooks run alone and
    no fire status is written back. ``None`` when nothing would run.
    """
    if turn.store_hooks:
        store = get_global_hook_store()
        if store is not None:
            return store
    if spec is None or not spec.hooks:
        return None
    return ScriptHookStore(load=False)


def spec_hook_kwargs(spec: TurnSpecHooks | None, acp_session_id: str = "") -> dict[str, Any]:
    """The ``ScriptHookStore.fire`` arguments that run *spec*'s hooks beside the store's.

    *acp_session_id* is the turn's ACP session id, which the spec's hooks read as
    ``session_id``, as kiro-cli hands it to them; ``""`` when not known yet.
    """
    if spec is None or not spec.hooks:
        return {}
    kwargs: dict[str, Any] = {"extra_hooks": spec.hooks, "extra_hooks_cwd": spec.cwd}
    if acp_session_id:
        kwargs["acp_session_id"] = acp_session_id
    return kwargs


async def fire_prompt_submit(
    turn: ScriptHookTurn, spec: TurnSpecHooks | None = None, *, acp_session_id: str = ""
) -> list[str]:
    """Fire ``UserPromptSubmit`` once per message; the texts its hook context carries."""
    if turn.prompt_context is None:
        turn.prompt_context = await _prompt_start(
            turn, HOOK_EVENT_USER_PROMPT_SUBMIT, turn.prompt, spec, acp_session_id
        )
    return turn.prompt_context


async def fire_agent_spawn(
    turn: ScriptHookTurn, spec: TurnSpecHooks | None = None, *, acp_session_id: str = ""
) -> list[str]:
    """Fire ``AgentSpawn`` for a turn that opened a new session; its hook context texts.

    The context is the session key, as the dashboard passes it. Fired by each
    attempt that opened one, so a replay on a fresh session runs it again.
    """
    return await _prompt_start(turn, HOOK_EVENT_AGENT_SPAWN, turn.session_key, spec, acp_session_id)


async def _prompt_start(
    turn: ScriptHookTurn,
    event: str,
    context: str,
    spec: TurnSpecHooks | None,
    acp_session_id: str = "",
) -> list[str]:
    store = observer_store(turn, spec)
    if store is None:
        return []
    try:
        results = await store.fire(
            event,
            context,
            session_key=turn.session_key,
            **spec_hook_kwargs(spec, acp_session_id),
        )
    except Exception:
        logger.warning("%s hook fire error", event, exc_info=True)
        return []
    return _hook_context(results)


def _hook_context(results: list[ScriptHookResult]) -> list[str]:
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


async def fire_stop_if_succeeded(turn: ScriptHookTurn, stop_reason: str, reply: str) -> None:
    """Fire ``Stop`` with *reply* when *stop_reason* classifies ``succeeded``.

    For a caller that streams without a ``TurnDriver`` and so judges the end of
    its own turn; the driver applies the same rule. ``stop_reason_landed`` is that
    one classifier read for a completion that was observed.
    """
    # circular import: turn_bracket -> agent_sdk -> acp.client, which is still
    # initializing when kiro_crew.slack.handler loads this module.
    from kiro_crew.messaging.turn_bracket import stop_reason_landed

    if stop_reason_landed(stop_reason):
        await fire_stop(turn, reply)


async def fire_stop(
    turn: ScriptHookTurn,
    reply: str,
    spec: TurnSpecHooks | None = None,
    *,
    acp_session_id: str = "",
) -> None:
    """Fire ``Stop`` with the turn's channel-safe reply as its context."""
    store = observer_store(turn, spec)
    if store is None:
        return
    try:
        await store.fire(
            HOOK_EVENT_STOP,
            reply,
            session_key=turn.session_key,
            **spec_hook_kwargs(spec, acp_session_id),
        )
    except Exception:
        logger.warning("Stop hook fire error", exc_info=True)
