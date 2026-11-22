"""The floor's patcher fixture writes an outer variable back only when the test changed it.

``_floor_monkeypatch`` remembers four variables of the worker's own environment
before a test and puts them back after its undo. These tests run one cycle of
the real fixture on a private mapping that counts its writes, so no real
environment variable is touched.
"""

from __future__ import annotations

import os

import pytest
from host_isolation_floor_helpers import _root

_OUTER = ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL", "PYTHONUTF8", "PYTHONIOENCODING")
#: What the worker holds before the test: two set, two absent.
_HELD = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class _CountingEnviron(dict):
    """A stand-in for ``os.environ`` that lists every write made through it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes: list[tuple[str, str]] = []

    def __setitem__(self, name, value):
        self.writes.append(("set", name))
        super().__setitem__(name, value)

    def __delitem__(self, name):
        self.writes.append(("delete", name))
        super().__delitem__(name)

    def pop(self, name, *default):
        if name in self:
            self.writes.append(("delete", name))
        return super().pop(name, *default)


def _writes_at_teardown(monkeypatch, change) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Run one fixture cycle around *change*; return the teardown's writes and the end state."""
    env = _CountingEnviron(_HELD, UNRELATED="kept")
    monkeypatch.setattr(os, "environ", env)
    definition = _root._floor_monkeypatch
    cycle = getattr(definition, "__wrapped__", definition)()
    next(cycle)
    change(env)
    env.writes.clear()
    with pytest.raises(StopIteration):
        next(cycle)
    return env.writes, dict(env)


def _nothing(env) -> None:
    return None


def _changes_a_set_one(env) -> None:
    dict.__setitem__(env, "PYTHONUTF8", "0")


def _deletes_a_set_one(env) -> None:
    dict.__delitem__(env, "PYTHONIOENCODING")


def _creates_an_absent_one(env) -> None:
    dict.__setitem__(env, "KIROCREW_SANDBOX_ACTIVE", "1")


def _empties_a_set_one(env) -> None:
    dict.__setitem__(env, "PYTHONUTF8", "")


def _changes_all_four(env) -> None:
    dict.__setitem__(env, "KIROCREW_SANDBOX_ACTIVE", "1")
    dict.__setitem__(env, "KIROCREW_SANDBOX_LEVEL", "strict")
    dict.__setitem__(env, "PYTHONUTF8", "0")
    dict.__delitem__(env, "PYTHONIOENCODING")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (_nothing, []),
        (_changes_a_set_one, [("set", "PYTHONUTF8")]),
        (_deletes_a_set_one, [("set", "PYTHONIOENCODING")]),
        (_creates_an_absent_one, [("delete", "KIROCREW_SANDBOX_ACTIVE")]),
        (_empties_a_set_one, [("set", "PYTHONUTF8")]),
        (
            _changes_all_four,
            [
                ("delete", "KIROCREW_SANDBOX_ACTIVE"),
                ("delete", "KIROCREW_SANDBOX_LEVEL"),
                ("set", "PYTHONUTF8"),
                ("set", "PYTHONIOENCODING"),
            ],
        ),
    ],
    ids=lambda value: value.__name__.strip("_") if callable(value) else None,
)
def test_the_teardown_writes_one_outer_variable_for_each_the_test_changed(
    monkeypatch, change, expected
):
    """No write for a variable the test left as it was, one for each it changed, and
    the worker's environment is what it was before the test either way."""
    writes, after = _writes_at_teardown(monkeypatch, change)
    assert after == {**_HELD, "UNRELATED": "kept"}
    assert writes == expected


def test_an_outer_variable_the_worker_holds_empty_is_put_back_empty(monkeypatch):
    """Empty is a value: a variable held as ``""`` is neither removed nor rewritten
    when the test left it alone, and comes back as ``""`` when the test changed it."""
    monkeypatch.setitem(_HELD, "PYTHONUTF8", "")

    writes, after = _writes_at_teardown(monkeypatch, _nothing)
    assert after["PYTHONUTF8"] == ""
    assert writes == []

    writes, after = _writes_at_teardown(monkeypatch, _changes_a_set_one)
    assert after["PYTHONUTF8"] == ""
    assert writes == [("set", "PYTHONUTF8")]


def test_the_four_outer_variables_are_the_ones_the_fixture_remembers():
    """The names this file counts are the names in the fixture, so a fifth added
    there without a case here is seen."""
    import inspect

    definition = _root._floor_monkeypatch
    source = inspect.getsource(getattr(definition, "__wrapped__", definition))
    start = source.index("cli_process_environment = {")
    block = source[start : source.index("}", start)]
    named = tuple(part for part in block.split('"')[1::2])
    assert named == _OUTER
