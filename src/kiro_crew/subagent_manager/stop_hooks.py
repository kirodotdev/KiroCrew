"""The Stop hooks a sub-agent's prompt fires when it ends.

A child run fires ``Stop`` (Hooks-page hooks and the spec hooks its backend
cannot run) each time one of its prompts ends, as the dashboard does at a turn
end, so a Stop gate also covers work a parent delegates. A hook's block
decision becomes the child's next prompt, behind the dashboard's
``HOOK_CONTINUATION_RECOVERY_PREFIX``. The run loop in ``run_turn`` owns the
per-run state (the depth, the final segment, the result text); this module
decides whether a continuation is owed and what it says.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable


async def child_stop_continuation(
    store: Any,
    *,
    final_text: str,
    session_key: str,
    info: Any,
    spec: Any,
    client: Any,
    depth: int,
    ended_ok: bool,
    may_continue: bool,
    can_continue: Callable[[], bool],
    log: logging.Logger,
) -> str | None:
    """Fire Stop for the prompt that just ended and return the continuation prompt, or None.

    ``final_text`` is the prompt's final segment, redacted here before any hook
    reads it; ``depth`` is how many continuations the run has already sent.
    A continuation needs ``may_continue`` (False for a prompt a recovery or a
    refusal resume ends, which has its own next step), ``ended_ok`` (the
    prompt's stop reason classified as a success),
    ``can_continue()`` true before and after the config read (no stop, reap or
    shutdown), and room under ``agent.max_stop_hook_nudges`` (0 = uncapped).
    """
    # Deferred: a core module must not import the dashboard layer at import time.
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.state import (
        HOOK_CONTINUATION_RECOVERY_PREFIX,
        parse_hook_continuations,
    )
    from kiro_crew.hooks import HOOK_EVENT_STOP
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    _final = redact_credentials(redact_exfiltration_urls(final_text)[0])[0]
    # The ACP session id the spec hooks would see as KIRO_SESSION_ID under
    # their own harness; the Hooks-page hooks never get it.
    _acp_sid = getattr(client, "session_id", "") if spec.hooks else ""
    try:
        results = await store.fire(
            HOOK_EVENT_STOP,
            _final,
            session_key=session_key,
            subagent_id=info.id,
            parent_session_key=info.parent_session_key or None,
            agent_role=info.agent or None,
            hook_continuation_count=depth,
            extra_hooks=spec.hooks,
            extra_hooks_cwd=spec.cwd,
            acp_session_id=_acp_sid if isinstance(_acp_sid, str) and _acp_sid else None,
        )
    except Exception:
        log.warning("Stop hook error in subagent %s", info.id, exc_info=True)
        return None
    reasons = parse_hook_continuations(
        [r.stdout for r in results or () if r.exit_code == 0 and r.stdout]
    )
    if not may_continue or not reasons or not ended_ok or not can_continue():
        return None
    try:
        cap = (await asyncio.to_thread(KiroCrewConfig.load)).agent.max_stop_hook_nudges
    except Exception:
        # The run already ended well: an unreadable cap ends it as it stands.
        log.warning("Subagent %s: could not read the Stop-hook cap", info.id, exc_info=True)
        return None
    if cap and depth >= cap:
        log.warning(
            "Subagent %s: a Stop hook asked to continue past "
            "agent.max_stop_hook_nudges = %d; ending the run",
            info.id,
            cap,
        )
        return None
    # The config load yields: a Stop landing in it still wins.
    if not can_continue():
        return None
    # A reason is external process output: redacted as the dashboard's
    # dequeue path redacts it.
    _reason = redact_credentials(redact_exfiltration_urls("\n\n".join(reasons))[0])[0]
    return HOOK_CONTINUATION_RECOVERY_PREFIX + "\n" + _reason
