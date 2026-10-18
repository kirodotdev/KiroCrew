"""The hooks handler facade keeps the patch seams and the import surface the one module had.

``kiro_crew.dashboard.handlers.hooks`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
the ``kiro_crew.dashboard.handlers.hooks_<role>`` owners. The handlers package, the route tables
and tests still reach every name as ``kiro_crew.dashboard.handlers.hooks.<name>``, and the moved
code reads its names as plain module globals, so a patch through the facade reaches that code only
because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one object
per name, the manifest, owners never importing their facade, every name the test trees patch
reaching every owner that binds it). These tests pin what is particular to this facade: the owners
are exactly the ``hooks_*.py`` siblings, in layer order; a name the facade serves from outside the
family stays visible to type checkers; the owners log under the facade's name; the moved
exception classes keep the facade's ``__module__``; and a patch through the facade reaches the
owner that reads the name.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import hooks

FACADE = "kiro_crew.dashboard.handlers.hooks"
HANDLERS_DIR = Path(hooks.__file__).resolve().parent

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = (
    "kiro_crew.dashboard.handlers.hooks_webhook_intake",
    "kiro_crew.dashboard.handlers.hooks_scripts",
    "kiro_crew.dashboard.handlers.hooks_webhooks",
)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(hooks)[_facade_mirror.INDEX_ATTRIBUTE]


def _family_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }


def test_the_owners_are_every_hooks_sibling_in_layer_order() -> None:
    """A ``hooks_*.py`` module the facade does not list would hold names a patch through
    the facade never reaches, so the sibling set and the owner tuple agree."""
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}" for path in HANDLERS_DIR.glob("hooks_*.py")
    }
    assert set(_OWNERS) == siblings
    assert _index().owners == _OWNERS
    for position, owner in enumerate(_OWNERS):
        path = Path(sys.modules[owner].__file__)
        assert _family_imports(path) <= set(_OWNERS[:position]), owner


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    found: dict[str, tuple[str, str]] = {}
    for owner in _OWNERS:
        tree = ast.parse(Path(sys.modules[owner].__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in _OWNERS:
                    continue
                for alias in node.names:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    tree = ast.parse(Path(hooks.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in _OWNERS:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner holds these and the facade serves them, so ``from
    kiro_crew.dashboard.handlers.hooks import agents_spec_lock`` type-checks only because the
    facade names each one from its home under ``TYPE_CHECKING``; exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_owners_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert hooks.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert set(loggers) == {
        "kiro_crew.dashboard.handlers.hooks_scripts",
        "kiro_crew.dashboard.handlers.hooks_webhooks",
    }
    assert all(found is hooks.logger for found in loggers.values())


@pytest.mark.parametrize(
    "name", ["_StoreUnavailable", "_HookBodyTooLarge", "_DestinationAgentGone"]
)
def test_a_moved_exception_class_keeps_the_facade_module(name: str) -> None:
    """An error chain renders a raised class as ``module.qualname``; the owner the class moved
    into stays out of that text, so a traceback reads as it did before the split."""
    cls = getattr(hooks, name)
    assert issubclass(cls, Exception)
    assert cls.__module__ == FACADE
    assert cls.__qualname__ == name


def test_a_facade_patch_of_the_store_path_reaches_the_owner_reading_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``hooks._HOOK_STORE_PATH`` is patched through the facade across the webhook suites;
    ``_hook_store_path`` in ``hooks_webhook_intake`` reads it and sees the patch."""
    intake = sys.modules["kiro_crew.dashboard.handlers.hooks_webhook_intake"]
    target = tmp_path / "hooks.json"
    monkeypatch.setattr(hooks, "_HOOK_STORE_PATH", target)
    assert intake._HOOK_STORE_PATH == target
    assert hooks._hook_store_path() == target


def test_a_facade_patch_of_the_sel_accessor_reaches_every_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hooks._sel`` is bound by the facade and the owners that log SEL events; a write
    through the facade lands in each of them, and the undo restores each."""
    holders = _index().holders["_sel"]
    assert len(holders) >= 2
    original = hooks._sel
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(hooks, "_sel", sentinel)
        assert vars(hooks)["_sel"] is sentinel
        assert [vars(sys.modules[h])["_sel"] for h in holders] == [sentinel] * len(holders)
    assert vars(hooks)["_sel"] is original
    assert all(vars(sys.modules[h])["_sel"] is original for h in holders)
