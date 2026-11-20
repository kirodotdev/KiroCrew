"""Expansion forms whose result is re-evaluated are not read-only."""

from __future__ import annotations

import pytest

from kiro_crew.security.readonly_bash import is_read_only_bash, unsafe_bash_reason


@pytest.mark.parametrize(
    "cmd",
    [
        "echo ${!_}",
        "echo ${HOME:_}",
        "echo ${HOME:_:1}",
        "echo ${HOME: _}",
        "echo ${a[_]}",
        "echo ${#a[_]}",
        "echo ${_@P}",
        "echo ${_@E}",
        "echo ${HOME:-${!_}}",
        "echo ${HOME:-${a[_]}}",
        'echo "${!_}"',
        'echo "${HOME:_}"',
        'echo "${a[_]}"',
        "echo ${!PATH*}",
        "echo ${!arr[@]}",
        "echo ${ true; }",
        "echo ${| true; }",
    ],
)
def test_reevaluating_expansion_is_refused(cmd: str) -> None:
    assert not is_read_only_bash(cmd)
    assert "result is re-evaluated" in unsafe_bash_reason(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "echo ${HOME}",
        "echo ${HOME:-/tmp}",
        "echo ${HOME:+set} ${HOME:?unset}",
        "echo ${#HOME} ${HOME#/} ${HOME%/*}",
        "cat ${HOME}/x | wc -l",
    ],
)
def test_plain_expansion_stays_read_only(cmd: str) -> None:
    assert is_read_only_bash(cmd)
