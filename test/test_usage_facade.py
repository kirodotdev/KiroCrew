"""The usage handler facade keeps the patch seams and the import surface the one module had.

``kiro_crew.dashboard.handlers.usage`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.usage_<role>`` owners. Routes, the gateway's turn
writers and tests still reach every name as ``kiro_crew.dashboard.handlers.usage.<name>``,
and the moved code reads its names as plain module globals, so a patch through the facade
reaches that code only because the mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, every name the test
trees patch reaching every owner that binds it). These tests pin what is particular to this
facade: the owners are the ``usage_*.py`` siblings, in layer order; a name the
facade serves from outside the family stays visible to type checkers; the owners log under
the facade's name; a patch through the facade reaches the owner that reads the name; and
the facade's token-record writer stays the family's one ``kiro_crew.acp`` importer.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import usage

FACADE = "kiro_crew.dashboard.handlers.usage"
HANDLERS_DIR = Path(usage.__file__).resolve().parent

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = (
    "kiro_crew.dashboard.handlers.usage_store",
    "kiro_crew.dashboard.handlers.usage_spend",
    "kiro_crew.dashboard.handlers.usage_context",
    "kiro_crew.dashboard.handlers.usage_cost",
    "kiro_crew.dashboard.handlers.usage_history",
    "kiro_crew.dashboard.handlers.usage_sessions",
)
#: ``usage_series`` is a sibling handler that reads the facade by attribute, not an owner.
_NOT_OWNERS = frozenset({"kiro_crew.dashboard.handlers.usage_series"})


def _index() -> _facade_mirror.MirrorIndex:
    return vars(usage)[_facade_mirror.INDEX_ATTRIBUTE]


def _family_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }


def test_the_owners_are_every_usage_sibling_in_layer_order() -> None:
    """A ``usage_*.py`` module the facade does not list would hold names a patch through
    the facade never reaches, so the sibling set (less the one sibling handler) and the
    owner tuple agree."""
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}" for path in HANDLERS_DIR.glob("usage_*.py")
    }
    assert set(_OWNERS) == siblings - _NOT_OWNERS
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
    tree = ast.parse(Path(usage.__file__).read_text(encoding="utf-8"))
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
    kiro_crew.dashboard.handlers.usage import bounded_records`` type-checks only because the
    facade names each one from its home under ``TYPE_CHECKING``; exactly those."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_owners_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs:
    log captures keyed on ``kiro_crew.dashboard.handlers.usage`` see the owners' records."""
    assert usage.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert set(loggers) == {
        "kiro_crew.dashboard.handlers.usage_store",
        "kiro_crew.dashboard.handlers.usage_context",
        "kiro_crew.dashboard.handlers.usage_sessions",
    }
    assert all(found is usage.logger for found in loggers.values())


def test_a_facade_write_of_the_shard_override_moves_the_shard_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``usage._TOKEN_USAGE_DIR`` is the override tests set through the facade; the
    resolver and the shard lookups that read it live in ``usage_store``."""
    store = importlib.import_module("kiro_crew.dashboard.handlers.usage_store")
    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", tmp_path)
    assert store._TOKEN_USAGE_DIR is tmp_path
    assert usage._token_usage_dir() == tmp_path
    assert store._token_usage_dir() == tmp_path


def test_the_token_record_writer_stays_the_one_acp_importer() -> None:
    """``TurnUsage`` is the family's one ``kiro_crew.acp`` import, held by the facade's
    token-record writer; no owner imports from ``kiro_crew.acp`` or
    ``kiro_crew.providers`` in any form (the agent-sdk boundary has no owner rows)."""
    assert usage.TurnUsage.__module__ == "kiro_crew.acp.types"
    for owner in _OWNERS:
        tree = ast.parse(Path(sys.modules[owner].__file__).read_text(encoding="utf-8"))
        modules = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not {
            m for m in modules if m.startswith(("kiro_crew.acp", "kiro_crew.providers"))
        }, owner
