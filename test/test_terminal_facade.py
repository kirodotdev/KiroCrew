"""The terminal handler facade keeps the patch seams and the import surface the one module had.

``kiro_crew.dashboard.handlers.terminal`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.terminal_<role>`` owners. Routes, the handlers
package and tests still reach every name as ``kiro_crew.dashboard.handlers.terminal.<name>``,
and the moved code reads its names as plain module globals, so a patch through the facade
reaches that code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, every name the test
trees patch reaching every owner that binds it). These tests pin what is particular to this
facade: the owners are exactly the ``terminal_*.py`` siblings, in layer order; a name the
facade serves from outside the family stays visible to type checkers; the owners log under
the facade's name; and a patch through the facade reaches the owner that reads the name.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import terminal

FACADE = "kiro_crew.dashboard.handlers.terminal"
HANDLERS_DIR = Path(terminal.__file__).resolve().parent

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = (
    "kiro_crew.dashboard.handlers.terminal_session",
    "kiro_crew.dashboard.handlers.terminal_shell",
    "kiro_crew.dashboard.handlers.terminal_transport",
    "kiro_crew.dashboard.handlers.terminal_lifecycle",
    "kiro_crew.dashboard.handlers.terminal_complete",
    "kiro_crew.dashboard.handlers.terminal_routes",
)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(terminal)[_facade_mirror.INDEX_ATTRIBUTE]


def _family_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }


def test_the_owners_are_every_terminal_sibling_in_layer_order() -> None:
    """A ``terminal_*.py`` module the facade does not list would hold names a patch
    through the facade never reaches, so the sibling set and the owner tuple agree."""
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}" for path in HANDLERS_DIR.glob("terminal_*.py")
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
    tree = ast.parse(Path(terminal.__file__).read_text(encoding="utf-8"))
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
    kiro_crew.dashboard.handlers.terminal import config_path`` type-checks only because the
    facade names each one from its home under ``TYPE_CHECKING``; exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_owners_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert terminal.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert set(loggers) == {
        "kiro_crew.dashboard.handlers.terminal_lifecycle",
        "kiro_crew.dashboard.handlers.terminal_routes",
    }
    assert all(found is terminal.logger for found in loggers.values())


def test_a_facade_patch_of_the_config_read_reaches_the_owner_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``terminal._get_config`` is patched through the facade across the terminal suite;
    the gate that reads it lives in ``terminal_session`` and sees the patch."""
    session = importlib.import_module("kiro_crew.dashboard.handlers.terminal_session")
    monkeypatch.setattr(terminal, "_enabled_cache", [True, float("-inf")])
    monkeypatch.setattr(terminal, "_get_config", lambda request: {"enabled": False})
    assert session._get_config is terminal._get_config
    assert terminal._is_enabled(object()) is False


def test_a_facade_patch_of_a_shared_import_reaches_every_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``platform_compat`` is bound by the facade and by several owners; a write through
    the facade lands in each of them, and the undo restores each."""
    holders = _index().holders["platform_compat"]
    assert len(holders) >= 3
    original = terminal.platform_compat
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(terminal, "platform_compat", sentinel)
        assert [vars(sys.modules[h])["platform_compat"] for h in holders] == [sentinel] * len(
            holders
        )
    assert all(vars(sys.modules[h])["platform_compat"] is original for h in holders)
