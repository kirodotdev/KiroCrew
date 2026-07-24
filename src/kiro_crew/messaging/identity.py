"""Per-turn session-identity publication — the single shared writer.

Every surface that runs an agent turn (the dashboard, native Slack, and each
channel ``transport_dispatch``) must publish the ``session_pid_<pid>.txt``
mapping so the gateway's ancestor PID-walk can resolve the caller's
``X-Session-Key`` for session-keyed managed MCP tools (``learn_add``, cron
management, and every other such handler). When a surface omits it the header
is empty and those tools reject the call with HTTP 400 ``missing
X-Session-Key`` (#232).

The obligation lives here — in one function every turn-running surface calls —
rather than as a copy-pasted, per-surface opt-in block. That duplication is the
architectural root cause of #232: two pre-existing writers (dashboard, native
Slack) did not stop five channel dispatchers from shipping the gap. Centralizing
means a new channel gets identity publication by calling one function, and any
change to the publish contract happens in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.executors import maintenance_executor
from kiro_crew.session_pid_sig import publish_session_pid

logger = logging.getLogger(__name__)


async def publish_turn_identity(sessions: Any, session_key: str) -> None:
    """Publish this turn's ``session_pid_<pid>.txt`` mapping (+ HMAC sidecar).

    Keyed by the session's kiro-cli host PID (via ``sessions.get_pid``) so the
    gateway PID-walk resolves ``X-Session-Key``. Offloaded to the maintenance
    executor: publishing does a key read plus two ``atomic_write()``
    replacements — blocking filesystem work that must not run on the event
    loop. Fail-safe: a missing pid (session not yet spawned) or any filesystem
    error is swallowed so identity publication can never break a turn.
    """
    try:
        pid = sessions.get_pid(session_key)
        if isinstance(pid, int):
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), publish_session_pid, pid, session_key
            )
    except Exception:
        logger.debug(
            "publish_turn_identity failed for %s", session_key, exc_info=True
        )
