"""Expansions that assign a shell variable never auto-approve, whatever the verb."""

from __future__ import annotations

import time

import pytest

from kiro_crew.security.readonly_bash import is_read_only_bash


@pytest.mark.parametrize(
    "cmd",
    [
        "echo $[PATH=0]; ls",
        "cat $[PATH=0]; ls",
        "head ${X:=1} f; ls",
        'echo "$[PATH=0]" && ls',
        "cat ${PATH=0}; ls",
        "cat ${a[PATH=0]}; ls",
        "ls | head ${X:=1}",
        "cat ${a[${b}PATH=0]}; ls",
        "echo ${X:-${a[${b}PATH=0]}}; ls",
        'echo "${a[0${v:-0}+(PATH=0)]}"; ls',
        "echo ${a[${b:-${c}}PATH=0]}; ls",
        'echo "${a[0${v:+\\}}+(PATH=0)]}"; ls',
        'echo "${a[0${v:+"}"}+(PATH=0)]}"; ls',
        "echo ${a[0|| echo +(PATH=0)]}; ls",
        "echo ${a[0| cat +(PATH=0)]}; ls",
        "echo ${a[0||\necho +(PATH=0)]}; ls",
        # Any `=` after a `${` prompts, even one that only follows an expansion.
        "ls ${D} --color=auto",
        "cat ${F}; git log --format=%h",
    ],
)
def test_assigning_expansion_prompts(cmd: str) -> None:
    assert not is_read_only_bash(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "cat $HOME/.bashrc",
        "head -20 ${LOG}",
        "echo ${HOME:-/tmp}",
        "ls -la",
        "grep -rn 'a=b' ${SRC}",
    ],
)
def test_non_assigning_expansion_stays_read_only(cmd: str) -> None:
    assert is_read_only_bash(cmd)


def test_long_command_is_classified_in_linear_time() -> None:
    # Many `${` with no `=`: a backtracking pattern took seconds here.
    cmd = "echo " + "${" * 32_000
    started = time.monotonic()
    assert is_read_only_bash(cmd)
    assert time.monotonic() - started < 1.0
