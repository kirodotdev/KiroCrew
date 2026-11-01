"""Fold copies of one test body into a single parametrized test.

Two tests whose bodies are identical check one behaviour with different data. One
parametrized test runs every case the copies ran, and ``named_cases`` keeps each case
traceable to the test it came from: its id is ``<old test name>-<old param id>``, so a
CI history or a ``windows-expected-failures.txt`` line still names the original.

The old param id is rebuilt the way pytest builds it, so the part after the name is
the id the case had before: an explicit ``pytest.param(id=...)`` first, then the
copy's ``ids=``, then the value itself (strings, numbers, ``None``, enums, patterns,
named objects) and finally ``<argname><index>``, where the index is the case's place
in its own copy's list.
"""

from __future__ import annotations

import enum
import re
from typing import Any, Callable, Iterable, Sequence

import pytest

_PARAM_TYPE = type(pytest.param())


def _argnames(argnames: str | Sequence[str]) -> list[str]:
    if isinstance(argnames, str):
        return [name.strip() for name in argnames.split(",") if name.strip()]
    return list(argnames)


def _value_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        # pytest escapes the finished id once; latin-1 maps each byte to the code point
        # whose escape is the byte's own, so the id reads as pytest spells the bytes.
        return value.decode("latin-1")
    if value is None or isinstance(value, (bool, int, float, complex)):
        return str(value)
    if isinstance(value, re.Pattern):
        return str(value.pattern)
    if isinstance(value, enum.Enum):
        return str(value)
    name = getattr(value, "__name__", None)
    if isinstance(name, str):
        return name
    return None


def _auto_id(value: Any, argname: str, index: int, idfn: Callable[[Any], Any] | None) -> str:
    if idfn is not None:
        made = idfn(value)
        if made is not None:
            return str(made)
    found = _value_id(value)
    return found if found is not None else f"{argname}{index}"


def named_cases(
    name: str,
    argnames: str | Sequence[str] = (),
    argvalues: Iterable[Any] | None = None,
    *,
    ids: Sequence[str | None] | Callable[[Any], Any] | None = None,
    marks: Sequence[Any] = (),
) -> list[Any]:
    """Return the cases of the former test ``name`` as ``pytest.param`` entries.

    ``argnames``, ``argvalues`` and ``ids`` are that test's own parametrize arguments.
    A test that was not parametrized passes only ``name`` and becomes one case.
    ``marks`` carries the marks only that test had, such as a platform skip.
    """
    if argvalues is None:
        return [pytest.param(marks=list(marks), id=name)]
    names = _argnames(argnames)
    idfn = ids if callable(ids) else None
    id_list = None if ids is None or callable(ids) else list(ids)
    cases = []
    for index, value in enumerate(argvalues):
        own_marks: Sequence[Any] = ()
        own_id = None
        if isinstance(value, _PARAM_TYPE):
            values = tuple(value.values)
            own_marks = tuple(value.marks)
            own_id = value.id
        elif len(names) == 1:
            values = (value,)
        else:
            values = tuple(value)
        if own_id is None and id_list is not None and index < len(id_list):
            own_id = id_list[index]
        if own_id is None:
            own_id = "-".join(
                _auto_id(item, argname, index, idfn) for argname, item in zip(names, values)
            )
        cases.append(pytest.param(*values, marks=[*marks, *own_marks], id=f"{name}-{own_id}"))
    return cases
