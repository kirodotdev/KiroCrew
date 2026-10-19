"""``kiro_crew.platform.interfaces`` stays the one import path of every extension point.

The boot-layer extension points live in the module itself; the install / structural ones
(and the feature apps) live in ``interfaces_structural`` and the runtime-service / frontend
ones in ``interfaces_runtime``, both re-exported at its foot. Every moved body is a
``Protocol`` stub or a dataclass field list whose cross-references are string annotations,
so nothing reads a module global at run time and a plain re-export keeps the surface whole.

These tests pin that shape: every name an owner defines is bound on the facade as the
owner's own object; an owner imports no project module at run time and never the facade;
no module outside the family imports an owner directly, so callers keep the one path; the
one enum stays in the facade under its name; and each moved class reports its owner.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
import source_corpus

from kiro_crew.platform import interfaces

pytestmark = pytest.mark.xdist_group(name="tree_scan_interfaces_facade")

FACADE = "kiro_crew.platform.interfaces"
OWNERS = (
    "kiro_crew.platform.interfaces_runtime",
    "kiro_crew.platform.interfaces_structural",
)

#: One name of each owner, so a definition scan that went blind reads as a mismatch.
_KNOWN = {
    "kiro_crew.platform.interfaces_runtime": {
        "TunnelProvider",
        "RemoteProvisioner",
        "JailProvider",
    },
    "kiro_crew.platform.interfaces_structural": {"AppsLoader", "MemoryRoots", "FeatureApp"},
}


def _owner_path(owner: str) -> Path:
    return Path(interfaces.__file__).resolve().parent / f"{owner.rsplit('.', 1)[1]}.py"


def _defined(owner: str) -> set[str]:
    tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_every_owner_name_is_bound_on_the_facade_as_the_owners_object() -> None:
    for owner in OWNERS:
        defined = _defined(owner)
        assert _KNOWN[owner] <= defined, owner
        module = importlib.import_module(owner)
        wrong = sorted(n for n in defined if vars(interfaces).get(n) is not vars(module)[n])
        assert wrong == [], owner


def test_an_owner_imports_no_project_module_at_run_time_and_never_the_facade() -> None:
    """Annotations are strings here, so an owner needs ``kiro_crew`` names for type
    checkers only; a runtime import of one could only ever be a cycle back to the facade."""
    for owner in OWNERS:
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
        typing_only = {
            id(sub)
            for node in tree.body
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
            for sub in ast.walk(node)
        }
        runtime = sorted(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and id(node) not in typing_only
            and (node.module or "").startswith("kiro_crew")
        )
        facade = sorted(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == FACADE
        )
        assert (runtime, facade) == ([], []), owner


def test_no_source_module_outside_the_family_imports_an_owner() -> None:
    """Callers keep ``kiro_crew.platform.interfaces``: an owner is an implementation detail
    of where the stubs are written down, never a second import path."""
    root = source_corpus.repo_root()
    family = {Path(interfaces.__file__).resolve(), *(_owner_path(o) for o in OWNERS)}
    offenders = []
    scanned = 0
    for path in source_corpus.repo_files_named(".py"):
        if path.resolve() in family or not path.relative_to(root).as_posix().startswith("src/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned += "kiro_crew.platform.interfaces" in text
        if any(leaf in text for leaf in _OWNER_LEAVES) and _imports_an_owner(text):
            offenders.append(path.relative_to(root).as_posix())
    assert scanned, "no file names the facade -- the scan went blind"
    assert offenders == [], offenders


#: The owners' leaf names, for the ``from kiro_crew.platform import <owner>`` spelling.
_OWNER_LEAVES = frozenset(owner.rsplit(".", 1)[1] for owner in OWNERS)


def _imports_an_owner(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in OWNERS:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.platform":
            if {a.name for a in node.names} & _OWNER_LEAVES:
                return True
        if isinstance(node, ast.Import) and {a.name for a in node.names} & set(OWNERS):
            return True
    return False


def test_the_owner_scan_answers_both_ways() -> None:
    assert _imports_an_owner("from kiro_crew.platform.interfaces_runtime import TunnelProvider\n")
    assert _imports_an_owner("from kiro_crew.platform import interfaces_structural\n")
    assert _imports_an_owner("import kiro_crew.platform.interfaces_runtime\n")
    assert not _imports_an_owner("from kiro_crew.platform.interfaces import TunnelProvider\n")
    assert not _imports_an_owner("from kiro_crew.platform import interfaces\n")


def test_the_enum_stays_in_the_facade_and_moved_classes_report_their_owner() -> None:
    """``InterceptDecision`` is compared at the Slack route gate and recorded in its audit,
    so it keeps the facade's name; the moved Protocols and dataclasses are stubs nothing
    pickles or names by module, and say where they are written down."""
    assert interfaces.InterceptDecision.__module__ == FACADE
    assert interfaces.RemoteProvisioner.__module__ == "kiro_crew.platform.interfaces_runtime"
    assert interfaces.MemoryRoots.__module__ == "kiro_crew.platform.interfaces_structural"
