# SPDX-License-Identifier: Apache-2.0
"""The Windows arm of the read-only shell check: which lines ask there, which are
still read-only, and that other platforms are unchanged."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import platform_compat
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager, HooksConfig
from kiro_crew.security.readonly_bash import is_read_only_bash, unsafe_bash_reason

#: Lines that ask on Windows.
_ASKS_ON_WINDOWS = [
    "echo (x)",
    "cat (x)",
    "echo @(x)",
    "echo x(y)",
    "ls | sort {x}",
    "ls | cat {x}",
    "echo @{a=1}",
    'grep "(a)" notes.txt',
    "echo '{x}'",
    "echo $${x}",
    "ls 2>/dev/null",
    "ls >/dev/null",
]

#: Lines that are read-only on Windows too.
_READ_ONLY_ON_WINDOWS = [
    "ls",
    "cat notes.txt",
    "ls | sort",
    "echo ${HOME}",
    "head -n 5 notes.txt | wc -l",
    "ls 2>&1",
]


def _numbered(lines: list[str], name: str) -> list:
    """*lines* as cases named ``<name>-<n>``, so a test id carries no command text."""
    return [pytest.param(line, id=f"{name}-{n}") for n, line in enumerate(lines)]


@pytest.mark.parametrize("command", _numbered(_ASKS_ON_WINDOWS, "asks"))
def test_these_lines_ask_on_windows(command):
    with patch.object(platform_compat, "IS_WINDOWS", True):
        assert not is_read_only_bash(command)
        assert unsafe_bash_reason(command)


@pytest.mark.parametrize("command", _numbered(_READ_ONLY_ON_WINDOWS, "read-only"))
def test_these_lines_are_still_read_only_on_windows(command):
    with patch.object(platform_compat, "IS_WINDOWS", True):
        assert is_read_only_bash(command)


@pytest.mark.parametrize(
    "command",
    _numbered(
        ['grep "(a)" notes.txt', "echo '{x}'", "ls 2>/dev/null", *_READ_ONLY_ON_WINDOWS], "other"
    ),
)
def test_other_platforms_are_unchanged(command):
    with patch.object(platform_compat, "IS_WINDOWS", False):
        assert is_read_only_bash(command)


@pytest.mark.parametrize("command", _numbered(_ASKS_ON_WINDOWS, "asks"))
def test_the_gate_leaves_a_line_that_asks_to_the_approval_card_on_windows(command):
    manager = HookManager(HooksConfig())
    with patch.object(platform_compat, "IS_WINDOWS", True):
        result = manager.on_tool_call(command, command=command, is_shell=True, tool_kind="execute")
    assert result.action == TOOL_ALLOW


def test_the_gate_still_answers_a_plain_read_on_windows():
    manager = HookManager(HooksConfig())
    with patch.object(platform_compat, "IS_WINDOWS", True):
        result = manager.on_tool_call("ls", command="ls", is_shell=True, tool_kind="execute")
    assert result.action == TOOL_AUTO_APPROVE
