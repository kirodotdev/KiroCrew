"""``kirocrew app|agent|workspace`` with no sub-action exits non-zero.

These subparsers are not `required` (no CLI subparser is), so a bare
`kirocrew app` leaves the action unset and the handler's `else` branch runs.
It printed a Usage line to stdout and returned None, and the dispatcher
discards the return -- so the process exited 0 after a usage error, and a
`kirocrew app && …` chain proceeded past a typo. The branch now prints to
stderr and exits 2, the argparse convention.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew.cli_commands_agent import _handle_agent
from kiro_crew.cli_commands_app import _handle_app
from kiro_crew.cli_commands_workspace import _handle_workspace


@pytest.mark.parametrize(
    ("handler", "action_attr"),
    [
        (_handle_app, "app_action"),
        (_handle_agent, "agent_action"),
        (_handle_workspace, "workspace_action"),
    ],
    ids=["app", "agent", "workspace"],
)
def test_no_subaction_exits_two_with_usage_on_stderr(handler, action_attr, capsys) -> None:
    args = argparse.Namespace(**{action_attr: None})
    with pytest.raises(SystemExit) as exited:
        handler(args)
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # the usage line is an error: stderr, not stdout
    assert "Usage: kirocrew" in captured.err


@pytest.mark.parametrize(
    ("handler", "action_attr"),
    [
        (_handle_app, "app_action"),
        (_handle_agent, "agent_action"),
        (_handle_workspace, "workspace_action"),
    ],
    ids=["app", "agent", "workspace"],
)
def test_unknown_subaction_exits_two(handler, action_attr, capsys) -> None:
    args = argparse.Namespace(**{action_attr: "definitely-not-a-real-action"})
    with pytest.raises(SystemExit) as exited:
        handler(args)
    assert exited.value.code == 2
