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
import os
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
    from kiro_crew.slack.code_channels import board, hooks
    from kiro_crew.slack.code_channels.routing import _dashboard_state
    from kiro_crew.slack.code_channels.surface_client import SurfaceClient

    svc = WorkspaceService(
        # The surface client: its calls meet the per-channel policy at write
        # time, so the bar (which carries the agent's own items) and the diff
        # view never reach a channel the policy refuses, whoever starts them.
        lambda: SurfaceClient.from_slack(slack_provider()),
        set_project=dashboard_project_setter(_dashboard_state),
        project_of=dashboard_project_reader(_dashboard_state),
        read_board=board.read,
        publish_board=board.publish,
    )
    register(hooks, svc)
    hooks.on_turn_start(_watch_on_turn)
    _SERVICE = svc
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # installed outside the gateway's loop (a test): no sweep
    if loop is not None:
        from kiro_crew.slack import features

        # Installed before the connect probe has read the app's scopes, when
        # the gate still reads shut and ``ensure`` does nothing: sweep again
        # each time the probe records them (a reconnect re-probes too).
        def _on_scopes() -> None:
            loop.call_soon_threadsafe(_sweep, svc)

        features.on_scopes_recorded(_on_scopes)
        _sweep(svc)
        # At gateway start: watch at once, so a board a restart interrupted is
        # read again and kept current even when no channel turn ever starts
        # again. Inert while the feature's gate is shut.
        start_watch()
    return svc


def service() -> WorkspaceService | None:
    """The gateway's workspace service (``None`` before :func:`install`)."""
    return _SERVICE


#: The running sweep, held so it is not collected while it runs.
_SWEEP: set[asyncio.Task[None]] = set()
#: A sweep was asked for while one ran: run once more when it ends.
_SWEEP_AGAIN = False


def _sweep(service: WorkspaceService) -> None:
    """Start :func:`prepare_existing` on the running loop; one at a time."""
    global _SWEEP_AGAIN
    if _SWEEP:
        _SWEEP_AGAIN = True
        return
    task = asyncio.get_running_loop().create_task(prepare_existing(service))
    _SWEEP.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        global _SWEEP_AGAIN
        _SWEEP.discard(t)
        if t.cancelled() or t.get_loop().is_closed():
            _SWEEP_AGAIN = False  # shutting down: start nothing new
            return
        if _SWEEP_AGAIN:
            _SWEEP_AGAIN = False
            _sweep(service)

    task.add_done_callback(_done)


async def prepare_existing(service: WorkspaceService) -> None:
    """Prepare every channel already bound when the gateway starts.

    A channel bound before its workspace existed (an upgrade) would otherwise
    get its worktree only from the turn-start hook, after the turn's session
    was admitted with its old project: that turn would run in the shared
    checkout. One channel at a time, off the turn path; a channel whose record
    already matches costs no git or Slack call. Never raises.
    """
    from kiro_crew.slack.code_channels.store import store as channel_store

    try:
        await asyncio.to_thread(channel_store().load)
        bindings = channel_store().bindings()
    except Exception:
        logger.warning("code channel workspaces: stored bindings unreadable", exc_info=True)
        return
    for binding in bindings:
        if getattr(binding, "archived_at", 0) or getattr(binding, "wrapped_up_at", 0):
            continue  # a finished channel gets no tree back
        try:
            await service.ensure(binding)
        except Exception:
            logger.warning(
                "code channel %s: workspace not prepared at start",
                binding.channel_id,
                exc_info=True,
            )


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


def planned_root(team_id: str, channel_id: str) -> str:
    """The checkout ``plan_session`` cut the channel's worktree beside, or ``""``.

    Blocking (a store read). The core copies it onto the binding it stores
    right after planning, because the worktree is cut before the binding exists.
    """
    if _SERVICE is None:
        return ""
    try:
        record = _SERVICE._store.get(team_id, channel_id)
    except Exception:
        logger.debug("code channel %s: planned root unreadable", channel_id, exc_info=True)
        return ""
    if record is None or not record.worktree or not record.repo_root:
        return ""
    if os.path.realpath(record.repo_root) == os.path.realpath(record.worktree):
        return ""
    return record.repo_root


def start_watch() -> None:
    """Start watching the sessions filed in each channel's folder (idempotent)."""
    if _SERVICE is None:
        return
    from kiro_crew.slack.code_channels import board
    from kiro_crew.slack.code_channels.routing import _dashboard_state

    # The ledger trigger listens from the first bound turn, not the first board:
    # a code channel's first dispatch is what POSTS its board.
    board.subscribe()
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
    from kiro_crew.slack.code_channels import board

    # A new binding: a conductor key earlier found bound to no channel may be now.
    board.forget_unbound()
    start_watch()
    try:
        await _SERVICE.ensure(binding)
    except Exception:
        logger.warning(
            "code channel %s: workspace not ready at bind", binding.channel_id, exc_info=True
        )
