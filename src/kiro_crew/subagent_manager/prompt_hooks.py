"""The ``AgentSpawn`` and ``UserPromptSubmit`` hooks a sub-agent's prompt fires.

A dashboard turn fires both before its stream starts (``AgentSpawn`` only on a fresh
session) and puts each exit-0 hook's stdout in front of the prompt as a
``[Hook context]`` block. A child run fires the same two events before its first
``client.stream``, with the child's own identity on the payload, so a hook that
injects policy or context reaches work the parent delegated too.

The hook stdout is external process output that lands in front of the trusted
prompt the context builder made, so its structural markers are neutralized before
it is prepended, as the dashboard's egress scrub does for its own prefix.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from kiro_crew.hooks import HOOK_EVENT_AGENT_SPAWN, HOOK_EVENT_USER_PROMPT_SUBMIT

if TYPE_CHECKING:
    from kiro_crew.hooks import ScriptHook, ScriptHookStore
    from kiro_crew.subagent import SubagentInfo

logger = logging.getLogger(__name__)


async def prompt_hook_prefix(
    store: ScriptHookStore | None,
    info: SubagentInfo,
    *,
    session_key: str,
    task: str,
    fresh_session: bool,
    spec_hooks: Sequence[ScriptHook] = (),
    spec_hooks_cwd: str | None = None,
) -> str:
    """Fire the child's prompt-start hooks and return the text to prepend, or "".

    ``AgentSpawn`` fires only when *fresh_session* (the run started a new
    conversation, not a resumed one), with *session_key* as its context, as the
    dashboard passes it. ``UserPromptSubmit`` fires every run with *task*, the text
    the parent wrote. Non-zero exits are warn-only on both events, so only exit-0
    stdout is injected, and a store that raises costs the prompt nothing.
    """
    if store is None:
        return ""
    events = [(HOOK_EVENT_AGENT_SPAWN, session_key)] if fresh_session else []
    events.append((HOOK_EVENT_USER_PROMPT_SUBMIT, task))
    injected: list[str] = []
    for event, context in events:
        try:
            results = await store.fire(
                event,
                context,
                session_key=session_key,
                subagent_id=info.id,
                parent_session_key=info.parent_session_key or None,
                agent_role=info.agent or None,
                extra_hooks=spec_hooks,
                extra_hooks_cwd=spec_hooks_cwd,
            )
        except Exception:  # noqa: BLE001 - a warn-only event must never fail the run
            logger.warning("%s hook error in subagent %s", event, info.id, exc_info=True)
            continue
        injected.extend(r.stdout for r in results or () if r.exit_code == 0 and r.stdout)
    if not injected:
        return ""
    from kiro_crew.context import (  # circular: context -> subagent
        _neutralize_structural_markers,
    )

    hook_ctx = await asyncio.to_thread(_neutralize_structural_markers, "\n\n".join(injected))
    return f"[Hook context]\n{hook_ctx}\n[End hook context]\n\n"
