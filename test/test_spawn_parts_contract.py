"""``mcp_tools/spawn.py`` is a mirrored facade over ``mcp_tools/spawn_parts``.

The descriptors and the agent roster, the list and status reads and the batch spawn live
in the owner modules under ``kiro_crew.mcp_tools.spawn_parts``; ``kiro_crew.mcp_tools.spawn``
stays the domain module ``mcp_tools.DOMAIN_MODULES`` names, the import path and the patch
seam, built on ``kiro_crew._facade_mirror``. The contract every mirrored facade holds to
(one object per name, the ``TYPE_CHECKING`` manifest, no owner importing the facade,
every patched name reaching every reader) is ``test_facade_mirror_guards.py``'s. These
cases pin what is particular to this family: its owners are exactly the package's modules
in layer order, no name had to be excluded, both halves of every tool still resolve on
the facade, a moved class reports the owner it lives in, and the names the owners import
from elsewhere stay visible to type checkers through the facade.
"""

from __future__ import annotations

import ast
import importlib
import logging
import pkgutil
from pathlib import Path
from types import ModuleType

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.mcp_tools import DOMAIN_MODULES, _schemas_accepts_names_only, spawn, spawn_parts

# One worker per file, so the shared checks run beside the tests that load this module.
pytestmark = pytest.mark.xdist_group(name="spawn_parts_contract")

SHARED_FACADE_CHECKS = (
    "no_name_of_the_family_is_excluded_from_the_mirror",
    "owners_import_only_owners_listed_before_them",
)

_FACADE = spawn.__name__
_PACKAGE = spawn_parts.__name__
_MIRROR: _facade_mirror.MirrorIndex = vars(spawn)[_facade_mirror.INDEX_ATTRIBUTE]


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in _MIRROR.owners]


def _tree(module: ModuleType) -> ast.Module:
    assert module.__file__ is not None
    return ast.parse(Path(module.__file__).read_text("utf-8"))


def _defined_names(owner: ModuleType) -> set[str]:
    return _facade_mirror._defined_names(_tree(owner))


def _in_family(module: str) -> bool:
    return module == _FACADE or module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


def test_the_facade_serves_a_mirror_over_its_owner_package() -> None:
    assert type(spawn) is _facade_mirror.MirroredModule
    assert _MIRROR.facade == _FACADE
    assert dict(spawn._EXPORTS) == _MIRROR.served
    assert {"schemas", "_SubAgentsStep", "_queued_why"} <= set(_MIRROR.served)


def test_the_owners_are_the_package_modules_lowest_layer_first() -> None:
    package_modules = {
        f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(spawn_parts.__path__)
    }
    assert set(_MIRROR.owners) == package_modules
    assert _MIRROR.owners[0] == f"{_PACKAGE}.schemas", "the roster layer sits lowest"
    assert _MIRROR.owners[-1] == f"{_PACKAGE}.wave", "the batch spawn sits highest"


def test_the_owner_package_is_not_a_tool_domain() -> None:
    """``DOMAIN_MODULES`` names the facade; an owner listed there would advertise twice."""
    assert "spawn" in DOMAIN_MODULES
    assert not any(domain.startswith("spawn_parts") for domain in DOMAIN_MODULES)
    halves = {"schemas", "HANDLERS"}
    assert not [
        name
        for name, value in vars(spawn_parts).items()
        if name in halves and not isinstance(value, ModuleType)
    ], "the package binds a tool half of its own"


def test_both_halves_of_every_tool_resolve_on_the_facade() -> None:
    """``build_tool_list`` reads ``spawn.schemas`` and ``dispatch`` reads
    ``spawn.HANDLERS``; the descriptor builder is the owner's own function, so the
    ``names_only`` keyword the names-only build detects is still on it."""
    assert spawn.schemas is vars(importlib.import_module(f"{_PACKAGE}.schemas"))["schemas"]
    assert _schemas_accepts_names_only(spawn.schemas)
    names = [tool["name"] for tool in spawn.schemas(names_only=True)]
    assert names and set(names) == set(spawn.HANDLERS)
    for handler in spawn.HANDLERS.values():
        assert getattr(spawn, handler.__name__) is handler


def test_the_facade_keeps_its_logger_name() -> None:
    """The owner that logs binds the facade's logger for a stable module name."""
    assert spawn.logger is logging.getLogger(_FACADE)
    loggers = [vars(owner)["logger"] for owner in _owners() if "logger" in vars(owner)]
    assert loggers == [
        spawn.logger
    ], "wave.py is the one owner allowed to log, using the facade's logger"


def test_a_moved_class_reports_the_owner_it_lives_in() -> None:
    classes = [
        (owner, value)
        for owner in _owners()
        for name, value in vars(owner).items()
        if isinstance(value, type) and name in _defined_names(owner)
    ]
    assert {cls.__name__ for _owner, cls in classes} == {"_ResumeHold", "_SubAgentsStep"}
    for owner, cls in classes:
        assert cls.__module__ == owner.__name__
        assert getattr(spawn, cls.__name__) is cls


def _reexport_group() -> set[str]:
    """Names the facade's last ``TYPE_CHECKING`` block imports from outside the family."""
    blocks = [
        node
        for node in _tree(spawn).body
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
    assert {"visible_agent_names", "redact", "SPAWN_SUB_AGENTS_SCHEMA"} <= imported_from_elsewhere
    assert len(imported_from_elsewhere) >= 15, "the scan found too few names to measure"
    assert _reexport_group() == imported_from_elsewhere


def test_a_patch_through_the_facade_reaches_both_roster_readers() -> None:
    """``is_internal_agent_spec`` is read by the description roster and by ``spawn_list``,
    in two owners; a patch through the facade reaches both and is undone in both."""
    holders = [importlib.import_module(name) for name in _MIRROR.holders["is_internal_agent_spec"]]
    assert {owner.__name__.rpartition(".")[2] for owner in holders} == {"schemas", "listing"}
    original = spawn.is_internal_agent_spec
    marker = object()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(spawn, "is_internal_agent_spec", marker)
        assert all(vars(owner)["is_internal_agent_spec"] is marker for owner in holders)
    assert all(vars(owner)["is_internal_agent_spec"] is original for owner in holders)


def test_a_patch_through_the_facade_reaches_the_description_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The descriptors are built before the handlers load, so their owner binds no
    sub-agent reader: it reads the facade's binding, where a patch lands."""
    schemas = importlib.import_module(f"{_PACKAGE}.schemas")
    assert not {"visible_agent_names", "resolve_max_subagents"} & set(vars(schemas))
    for name in (
        "visible_agent_names",
        "resolve_max_subagents",
        "parent_spawn_allowlists",
        "agent_matches_allowlist",
    ):
        assert schemas._subagent_reader(name) is getattr(spawn, name), name
        marker = object()
        monkeypatch.setattr(spawn, name, marker)
        assert schemas._subagent_reader(name) is marker, name
