"""The Kiro account check a channel member runs before each message.

A member keeps the lease it took at spawn until it dies, so it never acquires again
and a sweep only flags its session. The check therefore runs here, before each
message, and a session it flagged is replaced so the message runs on the current
account instead of the previous account's process.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

RESTART_FAILED_TEXT = (
    "❌ This agent's session could not be restarted on the current Kiro account. "
    "Wake it to try again."
)


async def session_after_account_check(
    sessions: Any,
    agent: Any,
    client: Any,
    channel: Any,
    thread_id: str | None,
    replace_session: Callable[[Any, Any], Awaitable[Any | None]],
) -> Any | None:
    """The client *agent*'s next message runs on, or None when its member must stop.

    Runs the session manager's account check. A session the check left unflagged keeps
    *client*. A flagged one is replaced through *replace_session*, and when that fails
    the member is marked failed and the channel is told, so the caller ends its loop.
    """
    await sessions.run_identity_check()
    if not sessions.is_retiring(agent.session_key):
        return client
    replacement = await replace_session(sessions, agent)
    if replacement is None:
        await channel.post(
            agent.id,
            RESTART_FAILED_TEXT,
            from_role=agent.role,
            msg_type="system",
            thread_id=thread_id,
        )
        agent.state = "failed"
    return replacement
