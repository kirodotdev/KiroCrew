"""The crew log handler facade keeps the patch seams and the import surface the one module had.

``kiro_crew.dashboard.handlers.crew_log`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers.crew_log_<role>`` owners. The handlers package, the
server, the WebSocket hub and tests still reach every name as
``kiro_crew.dashboard.handlers.crew_log.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror writes it
into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one object
per name, the manifest, owners never importing their facade, every name the test trees patch
reaching every owner that binds it). These tests pin what is particular to this facade: the owners
are exactly the ``crew_log_*.py`` siblings beside it, in layer order; a name the facade serves
from outside the family stays visible to type checkers; the owners log under the facade's name;
the routes stay the facade's own guarded objects; and a patch through the facade reaches the
owner that reads the name.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import crew_log

FACADE = "kiro_crew.dashboard.handlers.crew_log"
HANDLERS_DIR = Path(crew_log.__file__).resolve().parent

#: The owners, lowest layer first: each imports only owners listed before it.
_OWNERS = ("kiro_crew.dashboard.handlers.crew_log_scope",)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(crew_log)[_facade_mirror.INDEX_ATTRIBUTE]


def _family_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
    }


def test_the_owners_are_every_crew_log_sibling_in_layer_order() -> None:
    """A ``crew_log_*.py`` module the facade does not list would hold names a patch through
    the facade never reaches, so the sibling set and the owner tuple agree."""
    siblings = {
        f"kiro_crew.dashboard.handlers.{path.stem}" for path in HANDLERS_DIR.glob("crew_log_*.py")
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
    tree = ast.parse(Path(crew_log.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in _OWNERS:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """A name an owner imports from the rest of the project and the facade serves without
    binding it is named from its home under ``TYPE_CHECKING``. Every such import of the
    scope owner is one the facade makes itself today, so none is served from outside; the
    facade's own ``CrewLogError`` typing import is not a served name and stays as it is."""
    served = set(_index().exports)
    homes = _home_imports()
    assert homes, "the owner imports nothing from the project -- the scan went blind"
    expected = {name: home for name, home in homes.items() if name in served}
    manifest = _home_manifest()
    assert {name: home for name, home in manifest.items() if name in served} == expected
    assert set(manifest) - served == {"CrewLogError"}


def test_the_owners_log_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs; the
    dispatch-fence suite captures the scope refusals under that name."""
    assert crew_log.logger.name == FACADE
    loggers = {
        owner: vars(sys.modules[owner])["logger"]
        for owner in _OWNERS
        if "logger" in vars(sys.modules[owner])
    }
    assert set(loggers) == set(_OWNERS)
    assert all(found is crew_log.logger for found in loggers.values())


def test_every_route_is_the_facade_own_guarded_object() -> None:
    """``guard_owner_surface_routes`` wraps the routes in the facade's own namespace, so every
    route stays defined there: an ``api_*`` coroutine served from an owner would be the
    unguarded original."""
    routes = [name for name in vars(crew_log) if name.startswith("api_")]
    assert len(routes) == 7
    assert not any(name.startswith("api_") for name in _index().exports)
    for name in routes:
        route = vars(crew_log)[name]
        assert inspect.iscoroutinefunction(route.__wrapped__), name
        assert route.__module__ == FACADE, name


def test_a_facade_patch_of_the_grant_recheck_reaches_every_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``crew_log._stale_grant_refusal`` is patched through the facade by the dispatch-fence
    suite; the scope owner defines it and the facade's offload helper reads it, and a write
    through the facade lands in both."""
    scope = sys.modules["kiro_crew.dashboard.handlers.crew_log_scope"]
    original = crew_log._stale_grant_refusal

    async def _never_stale(*args: object, **kwargs: object) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(crew_log, "_stale_grant_refusal", _never_stale)
        assert vars(crew_log)["_stale_grant_refusal"] is _never_stale
        assert vars(scope)["_stale_grant_refusal"] is _never_stale
    assert vars(scope)["_stale_grant_refusal"] is original
    assert vars(crew_log)["_stale_grant_refusal"] is original
