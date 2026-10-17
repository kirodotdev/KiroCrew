"""A code channel's workspace: its git worktree, Code-tab diff and context bar.

Public surface (everything else in this package is internal):

* :class:`WorkspaceService` — one per gateway; :func:`register` attaches it to
  the core's turn hooks.
* :func:`release_channel_workspace` — the archive wrap-up's cleanup call.
* :func:`dashboard_project_setter` — points a bound dashboard session at the
  worktree through the dashboard's own ``set_project`` path.

Importing this package has no side effects: nothing runs until :func:`register`
is called, which the core does only while ``slack.code_channels`` is on.
"""

from kiro_crew.slack.code_channels.workspace.service import (
    WorkspaceService,
    dashboard_project_setter,
    register,
    release_channel_workspace,
)

__all__ = [
    "WorkspaceService",
    "dashboard_project_setter",
    "register",
    "release_channel_workspace",
]
