"""Wire the workspace to the code-channel core: its client, hooks and state.

:func:`install` builds the gateway's one :class:`WorkspaceService` over the
core's own :class:`~kiro_crew.slack.code_channels.client.CodeChannelsClient`
(error mapping, the capability latch, log redaction) and subscribes it to the
core's turn hooks; the core's bind awaits :func:`prepare` before a channel's
first turn.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    hooks.on_turn_start(_watch_on_turn)
    _SERVICE = service
    return service


async def _watch_on_turn(_binding: Any, _event: Any = None) -> None:
    """Make sure the folder watcher runs (after a restart no channel is re-bound)."""
    start_watch()


async def plan_session(
    state: Any,
    spec: Any,
    *,
    channel_id: str,
    team_id: str,
    title: str,
    channel_name: str = "",
) -> Any:
    """The core's session spec, moved into the channel's own folder and worktree.

    Called by the core's bind BEFORE it creates the channel's session: when the
    project is a git repository the channel's worktree is cut now and a folder
    for the channel is created under the spec's folder, with the worktree as
    its project. The returned spec files the session there and starts it in
    the worktree; a conductor running in it files its workers under the same
    folder, so they inherit the worktree too. The spec comes back unchanged
    when there is no service, no git project, or anything refuses. Never raises.
    """
    if _SERVICE is None:
        return spec
    try:
        project = str(getattr(spec, "project_dir", "") or "")
        if not project:
            # The core's own resolver for the configured default project, so a
            # git default project is isolated per channel too. The core's last
            # fallback, the agent workspace directory, is not a user repository
            # and stays as the core resolves it (no worktree).
            from kiro_crew.slack.code_channels.binding import _usable_project_dir, live_config

            dashboard = getattr(live_config(), "dashboard", None)
            configured = getattr(dashboard, "default_project", "")
            if isinstance(configured, str) and configured:
                project = await asyncio.to_thread(_usable_project_dir, configured)
        if not project:
            return spec
        planned = await _SERVICE.plan_session(
            state,
            channel_id=channel_id,
            team_id=team_id,
            title=title,
            channel_name=channel_name,
            project_dir=project,
            parent_folder_id=str(getattr(spec, "folder_id", "") or ""),
        )
    except Exception:
        logger.warning("code channel %s: workspace not planned", channel_id, exc_info=True)
        return spec
    if planned is None:
        return spec
    folder_id, session_dir = planned
    return dataclasses.replace(spec, folder_id=folder_id, project_dir=session_dir)


def start_watch() -> None:
    """Start watching the sessions filed in each channel's folder (idempotent)."""
    if _SERVICE is None:
        return
    from kiro_crew.slack.code_channels.routing import _dashboard_state

    _SERVICE.start_watch(_dashboard_state)


async def prepare(binding: Any) -> None:
    """Make the channel's worktree right after the bind, before its first turn.

    Awaited by the core's bind, ahead of routing the first message: the core
    captures a session's project when it starts the first turn, so a worktree
    that is still being cut then would leave that turn in the shared checkout.
    The wait is bounded by git's own per-call timeout. The bind runs as a
    shielded per-channel task, so a cancelled caller leaves it finishing. Never
    raises (``CancelledError`` propagates). Also starts the folder watcher, on
    the running loop, the first time a channel is bound.
    """
    if _SERVICE is None:
        return
    start_watch()
    try:
        await _SERVICE.ensure(binding)
    except Exception:
        logger.warning(
            "code channel %s: workspace not ready at bind", binding.channel_id, exc_info=True
        )
