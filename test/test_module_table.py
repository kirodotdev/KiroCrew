"""``patched_sys_modules`` fakes module-table entries and never empties the table.

The table is ``sys.modules``. Emptying it, even for an instant, lets a thread that
imports in that instant register a second copy of a package; these tests pin that the
restore touches only the entries the block changed.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

import kiro_crew
from kiro_crew.testing.module_table import patched_sys_modules


class _Recording(dict):
    """A table that records every write, removal and ``clear``."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.written: list[str] = []
        self.removed: list[str] = []
        self.cleared = 0

    def __setitem__(self, key, value) -> None:
        self.written.append(key)
        super().__setitem__(key, value)

    def update(self, other=(), **kwargs) -> None:
        self.written.extend(dict(other, **kwargs))
        super().update(other, **kwargs)

    def __delitem__(self, key) -> None:
        self.removed.append(key)
        super().__delitem__(key)

    def pop(self, key, *default):
        if key in self:
            self.removed.append(key)
        return super().pop(key, *default)

    def clear(self) -> None:
        self.cleared += 1
        super().clear()


def _table() -> tuple[_Recording, dict]:
    kept, faked = types.ModuleType("kept"), types.ModuleType("faked")
    start = {"kept": kept, "faked": faked, "blocked": None}
    return _Recording(start), start


def test_the_block_sees_the_values_and_the_exit_puts_every_entry_back() -> None:
    table, start = _table()
    stand_in = types.ModuleType("stand_in")
    with patched_sys_modules({"faked": stand_in, "absent": None}, _table=table) as seen:
        assert seen is table
        assert table["faked"] is stand_in
        assert table["absent"] is None
        assert table["kept"] is start["kept"]
    assert dict(table) == start
    assert all(table[name] is module for name, module in start.items())


def test_what_the_block_added_removed_or_replaced_is_undone() -> None:
    table, start = _table()
    with patched_sys_modules(_table=table):
        table["first_import"] = types.ModuleType("first_import")
        del table["faked"]
        table["blocked"] = types.ModuleType("blocked")
    assert dict(table) == start
    assert table["faked"] is start["faked"]


def test_an_entry_the_block_left_alone_is_never_removed_or_rewritten() -> None:
    table, _ = _table()
    with patched_sys_modules({"faked": None, "absent": None}, _table=table):
        table["first_import"] = types.ModuleType("first_import")
    assert table.cleared == 0
    assert "kept" not in table.removed
    assert "kept" not in table.written
    assert sorted(table.removed) == ["absent", "first_import"]


def test_the_end_state_is_the_one_patch_dict_leaves() -> None:
    ours, start = _table()
    theirs = dict(start)
    values = {"faked": None, "absent": types.ModuleType("absent")}
    with patched_sys_modules(values, _table=ours), patch.dict(theirs, values):
        for table in (ours, theirs):
            table["first_import"] = None
            del table["kept"]
    assert dict(ours) == theirs == start


def test_one_instance_is_entered_in_turn_and_nested() -> None:
    table, start = _table()
    faking = patched_sys_modules({"faked": None}, _table=table)
    for _ in range(2):
        with faking:
            assert table["faked"] is None
            with faking:
                table["inner"] = None
            assert "inner" not in table
            assert table["faked"] is None
        assert dict(table) == start


def test_the_exit_restores_when_the_block_raises() -> None:
    table, start = _table()
    with pytest.raises(RuntimeError):
        with patched_sys_modules({"faked": None}, _table=table):
            raise RuntimeError("boom")
    assert dict(table) == start


def test_the_default_table_is_sys_modules_and_the_package_stays_bound() -> None:
    name = "module_table_probe_that_is_not_installed"
    with patched_sys_modules({name: None}):
        assert sys.modules[name] is None
        with pytest.raises(ImportError):
            __import__(name)
        assert sys.modules["kiro_crew"] is kiro_crew
    assert name not in sys.modules
    assert sys.modules["kiro_crew"] is kiro_crew
