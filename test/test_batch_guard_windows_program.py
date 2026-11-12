"""The batch write guard reads a Windows spelling of git, gh or aws as that program.

cmd and PowerShell run ``git.exe``, ``Git``, ``GIT.EXE`` and a full path to
``git.exe`` as git, so the guard classifies them the way it classifies ``git``.
"""

from __future__ import annotations

import importlib

import pytest


def _guard():
    return importlib.import_module("kiro_crew.acp.batch_guard")


@pytest.mark.parametrize(
    "command",
    [
        "git.exe push origin main",
        "Git push origin main",
        "GIT.EXE push origin main",
        r'"C:\Program Files\Git\bin\git.exe" push origin main',
        "gh.exe pr merge 5",
        "aws.exe s3 rm s3://b/k",
    ],
)
def test_a_windows_spelling_of_a_write_is_a_write(command):
    assert _guard().classify_command(command) == "write"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "GIT.EXE log",
        "gitk",
        "git.exe status",
        "gh.exe pr view 7",
        "git log --grep='gh pr merge'",
    ],
)
def test_a_read_stays_a_read(command):
    assert _guard().classify_command(command) == "read"


def test_a_windows_spelling_of_git_push_is_a_creator():
    assert _guard()._shell_creates("git.exe push origin main")


def test_an_operator_prefix_still_matches_its_own_spelling():
    assert _guard().classify_command("MyDeploy run x", ("MyDeploy run",)) == "write"
