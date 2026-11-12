"""Whether a pre-output connection failure is really a missing kiro-cli sign-in.

A signed-out bundled ``kiro-cli-chat`` fails every ``session/prompt`` with what
looks like a retryable network drop; :func:`connection_failure_is_signed_out`
asks kiro-cli's own ``whoami`` to tell the two apart, so the turn raises
``AcpAuthRequired`` instead of retrying.

Each function here is an ``AcpSessionProvider`` method: the class binds it by name, so
it reads the provider's own state on the instance, and a patch of the method on the
class or on an instance reaches every caller.

Instance fields read: ``backend``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.acp.types import ACP_BACKEND_KIRO

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpError
    from kiro_crew.acp.session_provider import AcpSessionProvider

logger = logging.getLogger("kiro_crew.acp.session_provider")


async def connection_failure_is_signed_out(self: AcpSessionProvider, exc: AcpError) -> bool:
    """Whether a pre-output connection failure is really a missing sign-in.

    A signed-out bundled ``kiro-cli-chat`` starts and opens sessions normally,
    then fails every ``session/prompt`` with ``dispatch failure`` and writes
    nothing to stderr, so ``saw_not_logged_in`` stays False and the frame is
    classified as a retryable network drop. Only kiro-cli's own ``whoami``
    can tell the two apart; the gateway's prerequisite service runs it and
    answers True only on an explicit "not logged in".

    Asked only for the kiro backend (KAS has its own sign-in owner) and only
    for an error ``_raise_acp_error`` tagged ``connection_failure``, so every
    other error keeps its ordinary path with no probe.
    """
    kiro_connection_failure = (
        self.backend == ACP_BACKEND_KIRO and getattr(exc, "connection_failure", None) is True
    )
    if not kiro_connection_failure:
        return False
    # Local import: ``kiro_prerequisite`` pulls in the sandbox plane (see
    # ``client.py``'s imports of it for the same reason).
    from kiro_crew.kiro_prerequisite import kiro_signed_out_confirmed

    if not await kiro_signed_out_confirmed():
        return False
    logger.warning(
        "kiro-cli prompt failed as a connection error and whoami reports it is "
        "not logged in; raising AcpAuthRequired instead of retrying"
    )
    return True
