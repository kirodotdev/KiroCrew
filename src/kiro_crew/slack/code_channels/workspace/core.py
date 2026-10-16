"""Wire the workspace to the code-channel core: its client, hooks and state.

:func:`install` builds the gateway's one :class:`WorkspaceService` over the
core's own :class:`~kiro_crew.slack.code_channels.client.CodeChannelsClient`
(error mapping, the capability latch, log redaction) and subscribes it to the
core's turn hooks; the core's bind awaits :func:`prepare` before a channel's
first turn.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.slack.code_channels.workspace.service import (
    WorkspaceService,
    dashboard_project_reader,
    dashboard_project_setter,
    register,
)

logger = logging.getLogger(__name__)

_SERVICE: WorkspaceService | None = None


def install(slack_provider: Callable[[], Any]) -> WorkspaceService:
    """Build the gateway's workspace service and subscribe it to the core's hooks.

    Called from the core's own ``install`` (idempotent). Inert while the
    feature's gate is shut: the core fires its hooks only for a bound channel
    with the feature on, and the service checks the same gate itself.
    """
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    from kiro_crew.slack.code_channels import hooks
    from kiro_crew.slack.code_channels.client import CodeChannelsClient
    from kiro_crew.slack.code_channels.routing import _dashboard_state

    service = WorkspaceService(
        lambda: CodeChannelsClient.from_slack(slack_provider()),
        set_project=dashboard_project_setter(_dashboard_state),
        project_of=dashboard_project_reader(_dashboard_state),
    )
    register(hooks, service)
    _SERVICE = service
    return service


async def prepare(binding: Any) -> None:
    """Make the channel's worktree right after the bind, before its first turn.

    Awaited by the core's bind, ahead of routing the first message: the core
    captures a session's project when it starts the first turn, so a worktree
    that is still being cut then would leave that turn in the shared checkout.
    The wait is bounded by git's own per-call timeout. The bind runs as a
    shielded per-channel task, so a cancelled caller leaves it finishing. Never
    raises (``CancelledError`` propagates).
    """
    if _SERVICE is None:
        return
    try:
        await _SERVICE.ensure(binding)
    except Exception:
        logger.warning(
            "code channel %s: workspace not ready at bind", binding.channel_id, exc_info=True
        )
