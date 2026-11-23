"""The package-attribute floor's grouped read answers exactly as its row-by-row read.

``_PackageTable.intact`` decides, around every test, whether the ``kiro_crew`` module
graph is what the test inherited. It reads the rows through one C-level multi-key
read per dict; ``_intact_row_by_row`` is the plain reading it replaced and falls back
to. These tests drive both over generated module graphs and generated changes to
them and require the same answer every time, so the cheaper read cannot let through
a change the plain one catches, nor report one it does not.

The graphs are synthetic and live in a private ``sys.modules`` stand-in for the
length of one call, so no real module is evicted or replaced.
"""

from __future__ import annotations

import random
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEEDS = range(40)
_STEPS = 60


@pytest.fixture
def table_type(request):
    """``_PackageTable`` of the live rootdir conftest."""
    root = request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))
    return root._PackageTable


class _Modules(dict):
    """A ``sys.modules`` that is a dict subclass, which the grouped read must not trust."""


def _graph(rng: random.Random, *, packages: int, leaves: int) -> dict[str, object]:
    """A ``kiro_crew`` graph: *packages* nested packages holding *leaves* modules in all."""
    root = types.ModuleType("kiro_crew")
    root.__version__ = "0"
    root.a_global = object()
    modules: dict[str, object] = {"sys": sys, "kiro_crew": root, "unrelated.module": object()}
    package_names = ["kiro_crew"]
    for index in range(packages):
        parent = rng.choice(package_names)
        name = f"{parent}.pkg{index}"
        module = types.ModuleType(name)
        modules[name] = module
        setattr(modules[parent], f"pkg{index}", module)
        package_names.append(name)
    for index in range(leaves):
        parent = rng.choice(package_names)
        name = f"{parent}.leaf{index}"
        module = types.ModuleType(name)
        modules[name] = module
        shape = rng.random()
        if shape < 0.8:
            setattr(modules[parent], f"leaf{index}", module)
        elif shape < 0.9:
            # A facade name that shadows the submodule: never a row.
            setattr(modules[parent], f"leaf{index}", object())
        # else: loaded, but its parent holds no attribute for it.
    if rng.random() < 0.3:
        modules["kiro_crew.blocked"] = None
    return modules


def _change(rng: random.Random, modules: dict[str, object]) -> str:
    """Change the graph the way a test can, and say how."""
    names = [name for name in modules if name.startswith("kiro_crew.")]
    root = modules.get("kiro_crew")
    kind = rng.choice(
        ["replace", "evict", "none", "rebind", "unbind", "attr_none", "root_global", "root", "add"]
    )
    if kind == "root":
        modules["kiro_crew"] = types.ModuleType("kiro_crew") if rng.random() < 0.5 else root
        return kind
    if kind == "root_global" and root is not None:
        vars(root)[rng.choice(["a_global", "__version__", "new_global"])] = object()
        return kind
    if kind == "add":
        modules[f"kiro_crew.added{len(modules)}"] = types.ModuleType("added")
        return kind
    if not names:
        return "nothing"
    name = rng.choice(names)
    parent_name, _, child = name.rpartition(".")
    parent = modules.get(parent_name)
    if kind == "replace":
        modules[name] = types.ModuleType(name)
    elif kind == "evict":
        del modules[name]
    elif kind == "none":
        modules[name] = None
    elif parent is None:
        return "nothing"
    elif kind == "rebind":
        setattr(parent, child, types.ModuleType(name))
    elif kind == "unbind":
        vars(parent).pop(child, None)
    elif kind == "attr_none":
        setattr(parent, child, None)
    return kind


def _both(table, modules) -> tuple[bool, bool]:
    """``(grouped answer, row-by-row answer)`` with *modules* standing in for ``sys.modules``."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "modules", modules)
        return table.intact(), table._intact_row_by_row()


def _rebuilt(table_type, modules):
    table = table_type()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "modules", modules)
        table.rebuild()
    return table


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_grouped_read_answers_as_the_row_by_row_read_through_any_changes(table_type, seed):
    rng = random.Random(seed)
    modules = _graph(rng, packages=rng.randrange(0, 6), leaves=rng.randrange(0, 14))
    table = _rebuilt(table_type, modules)
    assert _both(table, modules) == (True, True)
    answers = set()
    for step in range(_STEPS):
        kind = _change(rng, modules)
        grouped, plain = _both(table, modules)
        assert grouped is plain, f"seed {seed}, step {step}, after {kind}"
        answers.add(plain)
        if not plain and rng.random() < 0.5:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(sys, "modules", modules)
                table.restore()
            assert _both(table, modules) == (True, True), f"seed {seed}, step {step}: restore"
        elif rng.random() < 0.2:
            table = _rebuilt(table_type, modules)
            assert _both(table, modules) == (True, True), f"seed {seed}, step {step}: rebuild"
    assert answers == {True, False}, "the changes never produced both answers"


@pytest.mark.parametrize("rows", [0, 1, 2, 3])
def test_a_table_of_any_size_reads_the_same_both_ways(table_type, rows):
    """No rows, one row (one key per read) and a few: each shape of the grouped read."""
    root = types.ModuleType("kiro_crew")
    modules: dict[str, object] = {"kiro_crew": root}
    for index in range(rows):
        module = types.ModuleType(f"kiro_crew.m{index}")
        modules[module.__name__] = module
        setattr(root, f"m{index}", module)
    table = _rebuilt(table_type, modules)
    assert len(table.names) == rows
    assert _both(table, modules) == (True, True)
    for name in [n for n in modules if n != "kiro_crew"]:
        kept = modules.pop(name)
        assert _both(table, modules) == (False, False)
        modules[name] = kept
        assert _both(table, modules) == (True, True)
        child = name.rpartition(".")[2]
        setattr(root, child, types.ModuleType(name))
        assert _both(table, modules) == (False, False)
        setattr(root, child, kept)
    root.extra = object()
    table = _rebuilt(table_type, modules)
    root.extra = object()
    assert _both(table, modules) == (False, False)


@pytest.mark.parametrize("seed", range(10))
def test_a_sys_modules_that_is_not_a_plain_dict_is_read_row_by_row(table_type, seed):
    rng = random.Random(seed)
    modules = _Modules(_graph(rng, packages=3, leaves=8))
    table = _rebuilt(table_type, modules)
    for step in range(_STEPS):
        kind = _change(rng, modules)
        grouped, plain = _both(table, modules)
        assert grouped is plain, f"seed {seed}, step {step}, after {kind}"


def test_the_live_table_reads_the_real_graph_the_same_both_ways(request):
    """On the real ``kiro_crew`` graph of this worker, mid-test, with the live table."""
    root = request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))
    table = root._PackageTable()
    table.rebuild()
    assert len(table.names) > 50, "the real graph was not loaded"
    assert table._checks is not None, "the real graph is not read by the grouped read"
    assert table.intact() is True and table._intact_row_by_row() is True
    package = sys.modules["kiro_crew"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package, table.root_items[0][0], object())
        assert table.intact() is False and table._intact_row_by_row() is False
    assert table.intact() is True
    name = table.names[len(table.names) // 2]
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, name, types.ModuleType(name))
        assert table.intact() is False and table._intact_row_by_row() is False
    assert table.intact() is True and table._intact_row_by_row() is True


class _EqualToAnything(types.ModuleType):
    """A module whose ``==`` says yes to everything, ``None`` included."""

    def __eq__(self, other):
        return True

    __hash__ = types.ModuleType.__hash__


def test_a_missing_entry_is_judged_by_the_row_by_row_read(table_type):
    """The row-by-row read compares a missing entry as ``None`` with ``==``.

    So a row whose module equals ``None`` reads as unchanged when it is evicted or its
    parent attribute is deleted. The grouped read cannot see a missing key as a
    value, and must hand that case over and not answer it itself.
    """
    root = types.ModuleType("kiro_crew")
    package = types.ModuleType("kiro_crew.pkg")
    odd = _EqualToAnything("kiro_crew.pkg.odd")
    plain = types.ModuleType("kiro_crew.pkg.plain")
    modules: dict[str, object] = {
        "kiro_crew": root,
        package.__name__: package,
        odd.__name__: odd,
        plain.__name__: plain,
    }
    root.pkg, package.odd, package.plain = package, odd, plain
    table = _rebuilt(table_type, modules)
    assert table._checks is not None and len(table.names) == 3
    del modules[odd.__name__]
    assert _both(table, modules) == (True, True)
    modules[odd.__name__] = odd
    del package.odd
    assert _both(table, modules) == (True, True)
    package.odd = odd
    del modules[plain.__name__]
    assert _both(table, modules) == (False, False)
