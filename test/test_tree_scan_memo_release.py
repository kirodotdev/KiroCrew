"""A ``tree_scan`` group's module memos are emptied when the worker leaves the group.

``test/conftest.py``'s teardown hook calls ``release_tree_scan_memos``. Driven here on
stand-in items so each case is exact; the hook itself runs around every test.
"""

from __future__ import annotations

import functools
import types

import pytest

import conftest


class _Item:
    def __init__(self, group: str | None, module: types.ModuleType | None) -> None:
        self._mark = pytest.mark.xdist_group(name=group).mark if group else None
        self.module = module

    def get_closest_marker(self, name: str):
        return self._mark if name == "xdist_group" else None


def _module(name: str) -> types.ModuleType:
    """A module holding one memo of its own and one imported from elsewhere."""
    module = types.ModuleType(name)

    @functools.lru_cache(maxsize=None)
    def memo() -> list[str]:
        return ["text"]

    memo.__module__ = name
    module.memo = memo
    module.borrowed = functools.lru_cache(maxsize=None)(lambda: ["elsewhere"])
    module.borrowed.__module__ = "somewhere_else"
    memo()
    module.borrowed()
    return module


@pytest.fixture(autouse=True)
def _empty_group_record(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(conftest, "_TREE_SCAN_GROUP_MODULES", {})


def test_the_memo_survives_inside_the_group() -> None:
    scan = _module("scan_a")

    conftest.release_tree_scan_memos(
        _Item("tree_scan_a", scan), _Item("tree_scan_a", _module("other"))
    )

    assert scan.memo.cache_info().currsize == 1


def test_leaving_the_group_empties_every_module_it_ran() -> None:
    first, second = _module("scan_a"), _module("shared_checks")

    conftest.release_tree_scan_memos(_Item("tree_scan_a", first), _Item("tree_scan_a", second))
    conftest.release_tree_scan_memos(_Item("tree_scan_a", second), _Item(None, None))

    assert first.memo.cache_info().currsize == 0
    assert second.memo.cache_info().currsize == 0


def test_the_last_item_of_the_run_releases_too() -> None:
    scan = _module("scan_a")

    conftest.release_tree_scan_memos(_Item("tree_scan_a", scan), None)

    assert scan.memo.cache_info().currsize == 0


def test_only_the_modules_own_memos_are_cleared() -> None:
    scan = _module("scan_a")

    conftest.release_tree_scan_memos(_Item("tree_scan_a", scan), None)

    assert scan.borrowed.cache_info().currsize == 1


def test_a_test_outside_any_tree_scan_group_releases_nothing() -> None:
    plain = _module("plain")

    conftest.release_tree_scan_memos(_Item(None, plain), None)
    conftest.release_tree_scan_memos(_Item("other_group", plain), None)

    assert plain.memo.cache_info().currsize == 1
