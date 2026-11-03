"""A prompt that may only continue its client's live session, never open a new one.

``AcpClient.ensure_ready`` runs before every prompt on a direct client. When the
child has exited, or holds no session, it spawns a replacement and opens a session
there, so a prompt sent after the process died lands in a NEW conversation. Most
callers want exactly that. A cron sub-agent's completion must not: it continues the
orchestration its parent is running, and a ``cron:`` key resumes nothing, so the
replacement would hold none of that orchestration and would act on the completion as
a new, unattended agent.

The caller names the provider or client it prompts (``continue_only``) for the span
of its prompts, and ``refuses_start`` then answers yes for that client only: a task
the span creates inherits the context but prompts other clients unaffected. The
client refuses where a NEW session would be opened: at ``ensure_ready``'s cold start
when there is nothing of its own to restore (no resume id and no pending fork, so a
spawn could only end in ``session/new``), and at ``_initialize_session``'s
``session/new`` itself, which a failed load or fork falls back to. A replacement that
does restore the client's own session is not refused. The refusal is
``SessionStartRefused``, which ``ensure_ready`` does not retry.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from kiro_crew.acp.transport_errors import AcpProcessDied

_TARGET: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "acp_continue_only", default=None
)


@contextmanager
def continue_only(target: object) -> Iterator[None]:
    """Within the block, *target* (a provider or the client it drives) may not start a session."""
    token = _TARGET.set(target)
    try:
        yield
    finally:
        _TARGET.reset(token)


def refuses_start(client: object) -> bool:
    """Whether the current context names *client*, directly or as its provider's ``_client``."""
    target = _TARGET.get()
    if target is None:
        return False
    return target is client or getattr(target, "_client", None) is client


class SessionStartRefused(AcpProcessDied):
    """A client named by ``continue_only`` refused to open a new session.

    An ``AcpProcessDied`` to the caller, since the session it continues is gone.
    ``ensure_ready`` ends a child it spawned for the attempt and does not retry: a
    respawn would meet the same refusal.
    """
