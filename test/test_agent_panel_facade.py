"""The agent panel handler facade keeps the patch seams and the import surface the one module had.

``kiro_crew.dashboard.handlers.agent_panel`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.agent_panel_<role>`` owners. The MCP route binder, the
member dashboard handlers and tests still reach every name as
``kiro_crew.dashboard.handlers.agent_panel.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror writes it
into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one object
per name, the manifest, owners never importing their facade, every name the test trees patch
reaching every owner that binds it). These tests pin what is particular to this facade: the owners
are exactly the ``agent_panel_*.py`` siblings, in layer order; a name the facade serves from
outside the family stays visible to type checkers; the owners log under the facade's name; and a
patch through the facade reaches the owner that reads the name.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import agent_panel

FACADE = "kiro_crew.dashboard.handlers.agent_panel"
HANDLERS_DIR = Path(agent_panel.__file__).resolve().parent

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = (
    "kiro_crew.dashboard.handlers.agent_panel_read",
    "kiro_crew.dashboard.handlers.agent_panel_publish",
    "kiro_crew.dashboard.handlers.agent_panel_dashboard",
    "kiro_crew.dashboard.handlers.agent_panel_snapshot",
    "kiro_crew.dashboard.handlers.agent_panel_record",
)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(agent_panel)[_facade_mirror.INDEX_ATTRIBUTE]


def _family_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }


def test_the_owners_are_every_agent_panel_sibling_in_layer_order() -> None:
    """An ``agent_panel_*.py`` module the facade does not list would hold names a patch
    through the facade never reaches, so the sibling set and the owner tuple agree."""
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}"
        for path in HANDLERS_DIR.glob("agent_panel_*.py")
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
    tree = ast.parse(Path(agent_panel.__file__).read_text(encoding="utf-8"))
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
    kiro_crew.dashboard.handlers.agent_panel import _normalize_slot_key`` type-checks only
    because the facade names each one from its home under ``TYPE_CHECKING``; exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_owners_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert agent_panel.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert set(loggers) == set(_OWNERS)
    assert all(found is agent_panel.logger for found in loggers.values())


def test_a_facade_patch_of_the_panel_slot_reaches_every_owner_reading_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``agent_panel._panel_slot`` is patched through the facade by the route suites; the
    card slot in ``agent_panel_read``, the publish route and the dashboard caller resolution
    each read it as a global of their own module, and each sees the patch."""
    calls: list[tuple[object, str, str]] = []

    def _no_slot(cfg: object, member: str, slug: str) -> str:
        calls.append((cfg, member, slug))
        return ""

    monkeypatch.setattr(agent_panel, "_panel_slot", _no_slot)
    holders = _index().holders["_panel_slot"]
    # The snapshot route resolves its caller through the dashboard owner's gate and
    # holds no slot lookup of its own; the record door resolves its writer's slot through
    # the same ``_panel_slot`` and so holds the name.
    assert set(holders) == set(_OWNERS) - {"kiro_crew.dashboard.handlers.agent_panel_snapshot"}
    assert all(vars(sys.modules[owner])["_panel_slot"] is _no_slot for owner in holders)
    cfg = SimpleNamespace(agents={})
    assert agent_panel._card_slot(cfg, "Oncall", "oncall") == ""
    assert calls == [(cfg, "Oncall", "oncall")]


def test_a_name_the_family_only_imports_is_served_from_its_home_object() -> None:
    """``member_dashboard_fields`` imports ``_normalize_slot_key`` through this facade inside a
    ``try``; a facade that stopped serving it would be swallowed there and leave every
    dashboard writer unresolved, so the served object is pinned to its home's."""
    state = importlib.import_module("kiro_crew.dashboard.state")
    assert "_normalize_slot_key" in _index().exports
    assert agent_panel._normalize_slot_key is state._normalize_slot_key
