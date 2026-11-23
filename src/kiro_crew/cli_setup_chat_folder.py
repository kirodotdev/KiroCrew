"""The setup wizard's note on the folder a new dashboard chat starts in.

The workspace step's answer does not move that folder, so the step names it,
chosen by the rule slot creation applies to a chat whose session folder names no
project: a folder of its own under ``dashboard.session_folder_root`` when that is
set, else :func:`kiro_crew.chat_default_project.usable_default_project`, else
``default_project_dir()``.
"""

from __future__ import annotations

import os

from kiro_crew.chat_default_project import usable_default_project
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import default_project_dir, workspace_dir_for

_FOLDER_OVERRIDE_LINE = (
    "  A session folder that names its own project overrides this for its chats."
)
_SET_BY_WORKSPACE_LINES = [
    "  (listed by `kirocrew workspace list`; set by `workspaces.<name>.dir` in config.json,",
    "  unless `dashboard.default_project` names a folder)",
]


def dashboard_chat_folder_lines() -> list[str]:
    """The lines naming where a new dashboard chat starts and the setting that sets it.

    Never raises: the note is informational, and a config this cannot load must
    not stop the wizard.
    """
    try:
        dashboard = KiroCrewConfig.load().dashboard
        root = getattr(dashboard, "session_folder_root", "")
        override = usable_default_project(dashboard.default_project)
        folder = "" if override else default_project_dir()
        # default_project_dir() answers "" for a folder not created yet, which a
        # fresh install has until setup or the gateway makes it.
        pending = "" if override or folder else str(workspace_dir_for())
        if pending and os.path.exists(pending):
            pending = ""
    except Exception:
        return [
            "  The folder dashboard chats start in could not be resolved;",
            "  `kirocrew workspace list` names the default workspace's folder.",
        ]
    lines: list[str] = []
    if isinstance(root, str) and root:
        lines = [
            f"  Each new dashboard chat gets a folder of its own under: {root}",
            "  (set by `dashboard.session_folder_root` in config.json); when one cannot be made:",
        ]
    if override:
        return [
            *lines,
            f"  Dashboard chats start in: {override}",
            "  (set by `dashboard.default_project` in config.json)",
            _FOLDER_OVERRIDE_LINE,
        ]
    if pending:
        return [
            *lines,
            f"  Dashboard chats start in the default workspace's folder: {pending}",
            "  (created when the gateway first starts)",
            *_SET_BY_WORKSPACE_LINES,
            _FOLDER_OVERRIDE_LINE,
        ]
    if not folder:
        # default_project_dir() refuses a sensitive folder, and slot creation
        # then leaves the chat's project empty.
        return [
            *lines,
            "  Dashboard chats start with no project: the default workspace's folder",
            "  cannot be used.",
            *_SET_BY_WORKSPACE_LINES,
            _FOLDER_OVERRIDE_LINE,
        ]
    return [
        *lines,
        f"  Dashboard chats start in the default workspace's folder: {folder}",
        *_SET_BY_WORKSPACE_LINES,
        _FOLDER_OVERRIDE_LINE,
    ]
