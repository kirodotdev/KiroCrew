"""The floor's patcher fixture writes an outer variable back only when the test changed it.

``_floor_monkeypatch`` remembers some variables of the worker's own environment
before a test and puts them back after its undo. These tests read which
variables from the fixture itself, then run one cycle of the real fixture per
variable on a private mapping that counts its writes, so no real environment
variable is touched.
"""

from __future__ import annotations

import ast
import inspect
import os
import textwrap

import pytest
from host_isolation_floor_helpers import _root


def _fixture_function():
    definition = _root._floor_monkeypatch
    return getattr(definition, "__wrapped__", definition)


def _remembered_names() -> tuple[str, ...]:
    """The variable names the fixture snapshots before the test, read from its body.

    The fixture builds one mapping with ``os.environ.get(name)`` for each name of a
    literal tuple. Every string constant of the first such mapping is a name.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(_fixture_function())))
    for node in ast.walk(tree):
        if isinstance(node, ast.DictComp) and "environ" in ast.unparse(node.value):
            return tuple(
                constant.value
                for constant in ast.walk(node.generators[0].iter)
                if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
            )
    return ()


_REMEMBERED = _remembered_names()


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


def _teardown(monkeypatch, before: dict[str, str], change) -> tuple[list, dict[str, str]]:
    """One fixture cycle around *change*, starting from *before*.

    Returns the writes the teardown made and the environment it left. *change*
    edits the mapping the way a test would, without being counted.
    """
    env = _CountingEnviron(before)
    monkeypatch.setattr(os, "environ", env)
    cycle = _fixture_function()()
    next(cycle)
    change(env)
    env.writes.clear()
    with pytest.raises(StopIteration):
        next(cycle)
    return env.writes, dict(env)


def _leave_alone(env, name) -> None:
    return None


def _set_another_value(env, name) -> None:
    dict.__setitem__(env, name, "left-by-the-test")


def _set_empty(env, name) -> None:
    dict.__setitem__(env, name, "")


def _delete(env, name) -> None:
    dict.pop(env, name, None)


def test_the_fixture_remembers_at_least_one_variable():
    """Everything below is per remembered variable, so none would test nothing."""
    assert _REMEMBERED
    assert all(name and name == name.strip() for name in _REMEMBERED)


@pytest.mark.parametrize("name", _REMEMBERED)
@pytest.mark.parametrize("held", ["a-value", ""], ids=["held", "held-empty"])
def test_a_variable_the_worker_holds_costs_a_write_only_if_the_test_changed_it(
    monkeypatch, name, held
):
    """Held and left alone: no write. Held and changed, emptied or deleted: one
    assignment of that variable, and its value is back. Empty is a value too."""
    before = {name: held, "UNRELATED": "kept"}

    writes, after = _teardown(monkeypatch, before, lambda env: _leave_alone(env, name))
    assert (writes, after) == ([], before)

    for change in (_set_another_value, _set_empty, _delete):
        writes, after = _teardown(monkeypatch, before, lambda env: change(env, name))
        changed = change is not _set_empty or held != ""
        assert writes == ([("set", name)] if changed else [])
        assert after == before


@pytest.mark.parametrize("name", _REMEMBERED)
def test_a_variable_the_worker_does_not_hold_is_removed_only_if_the_test_created_it(
    monkeypatch, name
):
    """Absent and left alone: no write. Absent and created, with a value or empty:
    one removal of that variable."""
    before = {"UNRELATED": "kept"}

    writes, after = _teardown(monkeypatch, before, lambda env: _leave_alone(env, name))
    assert (writes, after) == ([], before)

    for change in (_set_another_value, _set_empty):
        writes, after = _teardown(monkeypatch, before, lambda env: change(env, name))
        assert writes == [("delete", name)]
        assert after == before


def test_every_remembered_variable_changed_at_once_costs_one_write_each(monkeypatch):
    """Half held and changed, half absent and created: one write per variable, an
    assignment for a held one and a removal for an absent one."""
    held = {name: "a-value" for name in _REMEMBERED[::2]}
    before = {**held, "UNRELATED": "kept"}

    def change(env) -> None:
        for name in _REMEMBERED:
            _set_another_value(env, name)

    writes, after = _teardown(monkeypatch, before, change)
    assert after == before
    assert sorted(writes) == sorted(
        ("set" if name in held else "delete", name) for name in _REMEMBERED
    )
