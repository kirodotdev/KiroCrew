"""Opt-in discovery for a ``test_*_shared_checks.py`` file.

A family of sibling test files can hold a check whose body is the same in every file.
The body lives once in the family's shared-checks file, parametrized with one id per
sibling. A sibling opts in by naming the check in a module-level tuple (for example
``SHARED_BINDINGS_CHECKS``). :func:`discover` reads that tuple from the text of every
file matching the family's glob at collection, without importing anything, and
:meth:`OptIns.each` parametrizes a check over the files that named it. Each param carries
the file's ``xdist_group``, so it runs on the worker that already did that file's work,
and the test imports the file by name, which under pytest's default import mode is the
module object pytest already collected.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

_GROUP = re.compile(
    r"""^pytestmark\s*=.*?xdist_group\(\s*name\s*=\s*["']([^"']+)["']""", re.M | re.S
)


@dataclasses.dataclass(frozen=True)
class OptIns:
    """Each sibling's opted-in checks and xdist group, plus any opt-in that did not parse."""

    checks: dict[str, frozenset[str]]
    groups: dict[str, str | None]
    errors: list[str]

    def each(self, check: str) -> Callable:
        """Parametrize ``contract`` over the siblings that opted into *check*."""
        return pytest.mark.parametrize(
            "contract",
            [
                pytest.param(
                    stem,
                    id=stem,
                    marks=(
                        [pytest.mark.xdist_group(name=group)]
                        if (group := self.groups[stem])
                        else []
                    ),
                )
                for stem, checks in self.checks.items()
                if check in checks
            ],
        )

    def unknown(self, known: frozenset[str]) -> dict[str, list[str]]:
        """Per sibling, the opted-in names outside *known* (a typo opts a file out)."""
        return {s: sorted(c - known) for s, c in self.checks.items() if c - known}

    def unused(self, known: frozenset[str]) -> list[str]:
        """Known checks no sibling runs: a blind discovery would collect nothing and pass."""
        return sorted(known - set().union(*self.checks.values()))


def discover(test_dir: Path, pattern: str, tuple_name: str) -> OptIns:
    """Read *tuple_name* from every ``test_dir.glob(pattern)`` file that defines it."""
    opt_in = re.compile(rf"^{tuple_name}\s*=\s*(\([^)]*\))", re.M)
    checks: dict[str, frozenset[str]] = {}
    groups: dict[str, str | None] = {}
    errors: list[str] = []
    for path in sorted(test_dir.glob(pattern)):
        text = path.read_text(encoding="utf-8")
        if tuple_name not in text:
            continue
        match = opt_in.search(text)
        if match is None:
            errors.append(f"{path.name}: {tuple_name} is not a one-statement tuple")
            continue
        group = _GROUP.search(text)
        checks[path.stem] = frozenset(ast.literal_eval(match.group(1)))
        groups[path.stem] = group.group(1) if group else None
    return OptIns(checks, groups, errors)


def load(stem: str) -> ModuleType:
    """The sibling test module named *stem*."""
    return importlib.import_module(stem)
