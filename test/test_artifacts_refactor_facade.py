"""The artifact facade forwards the rule data its owners read, and holds no copy of it.

A name an owner module's own code reads -- a field limit, a grammar, the event-type
vocabulary, a folder limit, a per-format image sniffer -- has one binding, in that
owner. :mod:`kiro_crew.artifacts` forwards it: a read answers from the owner, a write
or a delete lands in the owner, and the facade's own code reads it off the owner too.
These tests pin each half of that, the machinery that makes it true for type
checkers and for a loaded owner, and that importing the facade loads every owner.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import artifact_store
from kiro_crew import artifacts as art_mod
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE_SOURCE = Path(inspect.getfile(art_mod))
_FORWARDED = sorted(art_mod._EXPORTS.items())
_FORWARDED_IDS = [name for name, _owner in _FORWARDED]


def _binding(name: str) -> object:
    return vars(sys.modules[art_mod._EXPORTS[name]])[name]


@pytest.mark.parametrize(("name", "owner_name"), _FORWARDED, ids=_FORWARDED_IDS)
def test_a_read_answers_from_the_owner_and_the_facade_binds_no_copy(
    name: str, owner_name: str
) -> None:
    assert owner_name.startswith(f"{artifact_store.__name__}.")
    assert getattr(art_mod, name) is vars(importlib.import_module(owner_name))[name]
    assert name not in vars(art_mod)


def test_no_owner_import_binds_a_copy_only_the_owners_read() -> None:
    """A by-name import of an owner's value that the facade never reads is a copy a
    facade patch would change and no reader would see. Types are the exception: a
    class is never rebound, and its string annotations here resolve through it."""
    tree = ast.parse(_FACADE_SOURCE.read_text(encoding="utf-8"))
    copies = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith(f"{artifact_store.__name__}.")
        for alias in node.names
    }
    read_here = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    owner_reads: set[str] = set()
    for info in pkgutil.iter_modules(artifact_store.__path__):
        owner = importlib.import_module(f"{artifact_store.__name__}.{info.name}")
        owner_reads |= {
            node.id
            for node in ast.walk(ast.parse(Path(inspect.getfile(owner)).read_text("utf-8")))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
    inert = sorted(
        name
        for name in copies - read_here
        if name in owner_reads and not isinstance(getattr(art_mod, name), type)
    )
    assert inert == []


@pytest.mark.parametrize(("name", "owner_name"), _FORWARDED, ids=_FORWARDED_IDS)
def test_a_write_and_a_delete_through_the_facade_reach_the_owner(
    name: str, owner_name: str
) -> None:
    owner = vars(importlib.import_module(owner_name))
    original = owner[name]
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(art_mod, name, sentinel)
        assert owner[name] is sentinel
        assert getattr(art_mod, name) is sentinel
        assert name not in vars(art_mod)
    assert owner[name] is original
    with pytest.MonkeyPatch.context() as patch:
        patch.delattr(art_mod, name)
        assert name not in owner
        assert not hasattr(art_mod, name)
    assert owner[name] is original
    assert name not in vars(art_mod)


@pytest.mark.parametrize(
    "patcher",
    [
        lambda new: mock.patch("kiro_crew.artifacts.MAX_TAGS", new),
        lambda new: mock.patch.object(art_mod, "MAX_TAGS", new),
    ],
    ids=["mock.patch", "mock.patch.object"],
)
def test_mock_patch_through_the_facade_puts_the_owners_binding_back(patcher) -> None:
    """``mock.patch`` exits by deleting the name and then, finding it gone, writing
    its original back -- both halves forwarded to the owner."""
    original = _binding("MAX_TAGS")
    with patcher(3):
        assert _binding("MAX_TAGS") == 3
    assert _binding("MAX_TAGS") is original
    assert "MAX_TAGS" not in vars(art_mod)


@pytest.mark.parametrize("inner_tool", ["monkeypatch", "mock.patch.object"])
@pytest.mark.parametrize("order", ["owner-then-facade", "facade-then-owner", "facade-then-facade"])
def test_nested_patches_unwind_in_order(order: str, inner_tool: str) -> None:
    owner = sys.modules[art_mod._EXPORTS["MAX_TAGS"]]
    outer_target, inner_target = {
        "owner-then-facade": (owner, art_mod),
        "facade-then-owner": (art_mod, owner),
        "facade-then-facade": (art_mod, art_mod),
    }[order]
    original = _binding("MAX_TAGS")
    with pytest.MonkeyPatch.context() as outer:
        outer.setattr(outer_target, "MAX_TAGS", 1)
        with contextlib.ExitStack() as stack:
            if inner_tool == "monkeypatch":
                stack.enter_context(pytest.MonkeyPatch.context()).setattr(
                    inner_target, "MAX_TAGS", 2
                )
            else:
                stack.enter_context(mock.patch.object(inner_target, "MAX_TAGS", 2))
            assert (_binding("MAX_TAGS"), art_mod.MAX_TAGS) == (2, 2)
        assert (_binding("MAX_TAGS"), art_mod.MAX_TAGS) == (1, 1)
    assert _binding("MAX_TAGS") is original
    assert "MAX_TAGS" not in vars(art_mod)


def test_a_name_outside_the_table_keeps_plain_module_behaviour() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(art_mod, "MAX_VERSIONS", 2)
        assert vars(art_mod)["MAX_VERSIONS"] == 2
    assert vars(art_mod)["MAX_VERSIONS"] == 50
    with pytest.raises(AttributeError, match="_not_a_forwarded_name"):
        getattr(art_mod, "_not_a_forwarded_name")
    assert "MAX_TAGS" in dir(art_mod)


def _bare_loads(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for each Load of a forwarded name as a bare global, at any
    depth, outside an import -- so the ``TYPE_CHECKING`` imports are allowed."""

    class _Loads(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: list[tuple[int, str]] = []

        def visit_Import(self, node: ast.Import) -> None:
            return

        visit_ImportFrom = visit_Import  # type: ignore[assignment]

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in art_mod._EXPORTS:
                self.found.append((node.lineno, node.id))

    loads = _Loads()
    loads.visit(ast.parse(source))
    return loads.found


def test_the_facade_reads_no_forwarded_name_as_a_bare_global() -> None:
    """A function defined here resolves a bare global through this module's own
    namespace, which ``__getattr__`` never answers, so it reads ``_rules.<name>``."""
    assert _bare_loads(_FACADE_SOURCE.read_text(encoding="utf-8")) == []


def test_the_bare_read_check_can_fail() -> None:
    assert _bare_loads("def f(tags):\n    return len(tags) > MAX_TAGS\n") == [(2, "MAX_TAGS")]
    assert (
        _bare_loads(
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.artifact_store.rules import MAX_TAGS\n"
            "def f(tags):\n"
            "    return len(tags) > _rules.MAX_TAGS\n"
        )
        == []
    )


def test_type_checkers_resolve_every_forwarded_name_without_the_forwarding_hook() -> None:
    """A visible ``__getattr__`` would let mypy accept any name read through the
    facade; hidden, each forwarded name needs a typed import from its owner, and
    every name production imports from the facade must resolve one way or the other."""
    tree = ast.parse(_FACADE_SOURCE.read_text(encoding="utf-8"))
    hooks = [
        (ast.unparse(node.test) if isinstance(node, ast.If) else "", statement.name)
        for node in tree.body
        for statement in (node.body if isinstance(node, ast.If) else [node])
        if isinstance(statement, ast.FunctionDef) and statement.name == "__getattr__"
    ]
    assert hooks == [("not TYPE_CHECKING", "__getattr__")]
    typed = {
        alias.asname or alias.name: statement.module
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for statement in node.body
        if isinstance(statement, ast.ImportFrom)
        for alias in statement.names
    }
    assert {name: typed.get(name) for name in art_mod._EXPORTS} == art_mod._EXPORTS
    imported: set[str] = set()
    for path in _FACADE_SOURCE.parent.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if art_mod.__name__ not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == art_mod.__name__:
                imported.update(alias.name for alias in node.names)
    assert "_SLUG_RE" in imported, "the scan found no production importer of a forwarded name"
    assert sorted(imported - set(typed) - set(vars(art_mod))) == []


def test_a_loaded_owner_is_read_without_import_module() -> None:
    """``importlib.import_module`` is an attribute any caller can rebind, so a
    loaded owner is read from ``sys.modules``; the import only answers a miss."""
    owner = sys.modules[art_mod._EXPORTS["MAX_TAGS"]]
    original = owner.MAX_TAGS
    assert art_mod.importlib is importlib
    with mock.patch.object(
        importlib, "import_module", side_effect=AssertionError("resolution imported")
    ) as refused:
        assert art_mod.MAX_TAGS is original
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(art_mod, "MAX_TAGS", 1)
            assert owner.MAX_TAGS == 1
        assert owner.MAX_TAGS is original
        assert refused.call_count == 0
        with pytest.raises(AssertionError, match="resolution imported"):
            art_mod._module("kiro_crew._artifacts_absent_owner_probe")
        assert refused.call_count == 1


def test_importing_the_facade_loads_every_owner(tmp_path: Path) -> None:
    """Owners load with the facade, so none is imported for the first time inside a
    test's patch, and the forwarding never has to import one."""
    owners = sorted(
        f"{artifact_store.__name__}.{info.name}"
        for info in pkgutil.iter_modules(artifact_store.__path__)
    )
    assert len(owners) >= 6 and set(art_mod._EXPORTS.values()) <= set(owners)
    code = (
        "import sys\n"
        "import kiro_crew.artifacts\n"
        f"print(','.join(m for m in {owners!r} if m not in sys.modules))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_FACADE_SOURCE.parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        cwd=str(tmp_path),
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )
    assert out.stdout.splitlines() == [""], out.stdout + out.stderr
