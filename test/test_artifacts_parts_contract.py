"""``dashboard/handlers/artifacts.py`` is a mirrored facade over ``artifacts_parts``.

The artifact HTTP routes live in the owner modules under
``kiro_crew.dashboard.handlers.artifacts_parts``; ``kiro_crew.dashboard.handlers.artifacts``
stays the import path and the patch seam, built on ``kiro_crew._facade_mirror``. The
contract every mirrored facade holds to (one object per name, the ``TYPE_CHECKING``
manifest, no owner importing the facade, every patched name reaching every reader) is
``test_facade_mirror_guards.py``'s. These cases pin what is particular to this family: it
is one of those facades, its owners are exactly the modules of the package in layer
order, no name of it had to be excluded, every owner logs under the module's original
logger name, the names owners import from elsewhere stay visible to type checkers, and
every route the dashboard server registers is served by the facade.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import pkgutil
from pathlib import Path
from types import ModuleType

from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import artifacts, artifacts_parts

_FACADE = artifacts.__name__
_PACKAGE = artifacts_parts.__name__
_MIRROR: _facade_mirror.MirrorIndex = vars(artifacts)[_facade_mirror.INDEX_ATTRIBUTE]


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in _MIRROR.owners]


def _tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text("utf-8"))


def _in_family(module: str) -> bool:
    return module == _FACADE or module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


def test_the_facade_serves_a_mirror_over_its_owner_package() -> None:
    assert type(artifacts) is _facade_mirror.MirroredModule
    assert _MIRROR.facade == _FACADE
    assert dict(artifacts._EXPORTS) == _MIRROR.served
    assert {"api_artifacts_create", "api_remote_artifact_get"} <= set(_MIRROR.exports)


def test_the_owners_are_the_package_modules_lowest_layer_first() -> None:
    package_modules = {
        f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(artifacts_parts.__path__)
    }
    assert set(_MIRROR.owners) == package_modules
    assert (
        _MIRROR.owners[0] == f"{_PACKAGE}.record_serialization"
    ), "the output redaction sits lowest"


def test_no_name_of_the_family_is_excluded_from_the_mirror() -> None:
    """The family came from one namespace, so no name can mean two things in it."""
    assert _MIRROR.excluded == ()
    assert _MIRROR.divergent() == ()


def test_every_owner_logs_under_the_module_s_original_logger_name() -> None:
    """Moved records keep their logger, so log filters and captures keyed on it still match."""
    loggers = [vars(owner)["logger"] for owner in _owners() if "logger" in vars(owner)]
    assert len(loggers) >= 2, "too few owners log for this case to measure"
    assert {log.name for log in loggers} == {_FACADE}
    assert all(log is logging.getLogger(_FACADE) for log in loggers)


def _reexport_group() -> set[str]:
    """Names the facade's last ``TYPE_CHECKING`` block imports from outside the family."""
    blocks = [
        node
        for node in _tree(artifacts).body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    ]
    imports = [sub for sub in blocks[-1].body if isinstance(sub, ast.ImportFrom)]
    assert imports and not any(_in_family(sub.module or "") for sub in imports)
    return {alias.asname or alias.name for sub in imports for alias in sub.names}


def test_the_names_owners_import_from_elsewhere_stay_visible_to_type_checkers() -> None:
    """A served name an owner imports from another ``kiro_crew`` module was the facade's
    own import before the split; the last ``TYPE_CHECKING`` block keeps it there for
    mypy and IDEs, and names nothing else."""
    imported_from_elsewhere = set()
    for name, owner_name in _MIRROR.exports.items():
        owner = importlib.import_module(owner_name)
        for node in _tree(owner).body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("kiro_crew"):
                bound = {alias.asname or alias.name for alias in node.names}
                if name in bound and not _in_family(node.module or ""):
                    imported_from_elsewhere.add(name)
    assert {
        "get_default_store",
        "_is_restricted_session",
        "publish_sync",
    } <= imported_from_elsewhere
    assert len(imported_from_elsewhere) >= 40, "the scan found too few names to measure"
    assert _reexport_group() == imported_from_elsewhere


def test_owners_import_only_owners_listed_before_them() -> None:
    """Lowest layer first: an owner reads a sibling only when the sibling comes earlier."""
    order = {name: position for position, name in enumerate(_MIRROR.owners)}
    edges = 0
    for owner_name in _MIRROR.owners:
        for node in ast.walk(_tree(importlib.import_module(owner_name))):
            if isinstance(node, ast.ImportFrom) and node.module in order:
                edges += 1
                assert order[node.module] < order[owner_name], (owner_name, node.module)
    assert edges > 0, "no owner imports a sibling, so this case measures nothing"


def test_every_route_the_server_registers_is_a_coroutine_an_owner_defines() -> None:
    """The dashboard server imports each artifact route from the facade by name; each one
    is the coroutine function its owner defines, so a patch through the facade and the
    route the server registers are one object."""
    from kiro_crew.dashboard import server

    routes = {
        name for name in vars(server) if name.startswith(("api_artifact", "api_remote_artifact"))
    }
    assert len(routes) >= 40, "the server imports too few artifact routes to measure"
    for name in sorted(routes):
        route = getattr(artifacts, name)
        assert route is vars(server)[name], name
        assert inspect.iscoroutinefunction(route), name
        assert _MIRROR.exports[name] == route.__module__, name
        assert route.__module__.startswith(f"{_PACKAGE}."), name
