"""Expansions that assign a shell variable never auto-approve, whatever the verb."""

from __future__ import annotations

import re

import pytest
from denied_commands_security_helpers import _interpreter_work, _linear_work
from regex_structure_helpers import run_cost_unproven

from kiro_crew.security import readonly_bash
from kiro_crew.security.readonly_bash import is_read_only_bash


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


def _patterns_handed(cmd: str) -> list[re.Pattern[str]]:
    """Every compiled pattern ``readonly_bash`` runs over *cmd* while classifying it."""
    handed: list[re.Pattern[str]] = []

    def record(pattern: re.Pattern[str], args: tuple[object, ...]) -> None:
        # The subject string is whichever argument is long (``sub`` takes the
        # replacement first).
        if any(isinstance(a, str) and len(a) >= len(cmd) // 2 for a in args):
            handed.append(pattern)

    class _Recorded:
        def __init__(self, real: re.Pattern[str]) -> None:
            self._real = real

        def __getattr__(self, name: str) -> object:
            attr = getattr(self._real, name)
            if not callable(attr):
                return attr

            def call(*args: object, **kwargs: object) -> object:
                record(self._real, args)
                return attr(*args, **kwargs)

            return call

    class _Re:
        def __getattr__(self, name: str) -> object:
            attr = getattr(re, name)
            if name not in ("search", "match", "fullmatch", "finditer", "findall", "sub", "split"):
                return attr

            def call(pattern: object, *args: object, **kwargs: object) -> object:
                record(re.compile(pattern) if isinstance(pattern, str) else pattern, args)
                return attr(pattern, *args, **kwargs)

            return call

    with pytest.MonkeyPatch.context() as mp:
        for name, value in list(vars(readonly_bash).items()):
            if isinstance(value, re.Pattern):
                mp.setattr(readonly_bash, name, _Recorded(value))
        mp.setattr(readonly_bash, "re", _Re())
        assert is_read_only_bash(cmd)
    return handed


def test_long_command_is_classified_in_linear_time() -> None:
    # Many `${` with no `=`: a backtracking pattern took seconds here. Linear
    # is asserted without a clock: every pattern the classifier runs over the
    # command must scan a `${` run without backtracking into it, and the
    # Python work around them must grow linearly with the run.
    unproven = [
        r for r in (run_cost_unproven(p, "${") for p in _patterns_handed("echo " + "${" * 64)) if r
    ]
    assert unproven == [], unproven
    work = [
        _interpreter_work(lambda n=n: is_read_only_bash("echo " + "${" * n))
        for n in (2000, 4000, 6000)
    ]
    assert _linear_work(work), f"classifier work {work} at 2000, 4000, 6000 `${{`"
    assert is_read_only_bash("echo " + "${" * 32_000)
