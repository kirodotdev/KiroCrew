"""A code channel's workspace: its git worktree, Code-tab diff and context bar.

Public surface (everything else in this package is internal):

* :class:`WorkspaceService` — one per gateway; :func:`register` attaches it to
  the core's turn hooks.
* :func:`dashboard_project_setter` — points a bound dashboard session at the
  worktree through the dashboard's own ``set_project`` path.

Importing this package has no side effects: nothing runs until :func:`register`
is called, and the service acts only while ``features.code_channels_enabled``
holds.
"""

from kiro_crew.slack.code_channels.workspace.service import (
    WorkspaceService,
    dashboard_project_setter,
    register,
)

__all__ = [
    "WorkspaceService",
    "dashboard_project_setter",
    "register",
]
