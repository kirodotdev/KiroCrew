"""The shared handler helpers' facade keeps the patch seams the one module had.

``kiro_crew.dashboard.handlers._shared`` is a mirror facade (:mod:`kiro_crew._facade_mirror`)
over the ``kiro_crew.dashboard.handlers._shared_<role>`` owners and keeps no code of its own.
The handler modules, tests and documents still reach every name as
``kiro_crew.dashboard.handlers._shared.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees patch
through it reaches every module holding it; the owners are exactly the ``_shared_*.py``
siblings, loaded lowest layer first; the facade defines nothing; every name it serves from
outside the family is visible to type checkers; the family logs under the facade's name; and
no module outside the family imports an owner.
"""

from __future__ import annotations

import ast
import functools
import importlib
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.dashboard.handlers import _shared

pytestmark = pytest.mark.xdist_group(name="tree_scan__shared_handlers_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.dashboard.handlers._shared"
OWNER_PREFIX = "kiro_crew.dashboard.handlers._shared_"
HANDLERS_DIR = Path(_shared.__file__).resolve().parent
_SRC = Path(kiro_crew.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the helpers
#: have: the capability manager, the restricted-session test and the persisted-session probe,
#: the member scope, the owner check, the skill catalog walk and the parsed agent specs, the
#: config directory and the logger. The scan below must find all of them, so a scanner that
#: stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_capability_manager",
        "_is_restricted_session",
        "_probe_persisted_session",
        "member_request_scope",
        "internal_memory_scope",
        "require_owner_dashboard_request",
        "walk_skill_catalog",
        "_load_parsed_agents",
        "config_dir",
        "logger",
    }
)

#: Every owner, lowest layer first: the order the facade imports them and a read resolves in.
_OWNERS = tuple(
    f"{OWNER_PREFIX}{role}"
    for role in (
        "scrub",
        "request_body",
        "access",
        "session_memory",
        "member_scope",
        "route_guards",
        "workspace",
        "skill_roots",
        "skill_listing",
        "skill_catalog",
        "memory_stores",
        "config_followups",
    )
)


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(_shared)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner_paths() -> dict[str, Path]:
    return {
        f"{OWNER_PREFIX}{path.stem[len('_shared_'):]}": path
        for path in sorted(HANDLERS_DIR.glob("_shared_*.py"))
    }


def _family() -> list[ModuleType]:
    return [_shared, *(sys.modules[name] for name in _index().owners)]


def _tree(module_name: str) -> ast.Module:
    path = Path(importlib.import_module(module_name).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "handlers" not in text or "_shared" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``_shared.<name>`` was what every reader
    of that name read. Each module of the family that binds a patched name must see the
    patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(_shared, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(_shared, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(_shared, name) is not sentinel:
                stale.append(FACADE)
        stale += [m.__name__ for m in holders if vars(m).get(name) is not before[m.__name__]]
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_shared_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``_shared_*.py`` sibling, and each owner imports only owners
    listed before it -- the order the facade loads them and a read resolves in."""
    index = _index()
    assert (index.owners, index.excluded, dict(index.forwards)) == (_OWNERS, (), {})
    assert sorted(_OWNERS) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(_OWNERS):
        imported = {
            node.module
            for node in ast.walk(_tree(owner))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in _OWNERS and _OWNERS.index(m) >= position)
        if later or FACADE in imported:
            late[owner] = later + ([FACADE] if FACADE in imported else [])
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_no_code_of_its_own() -> None:
    """Every name is served from the index, through the default read path, and the facade
    defines no function or class: new shared helpers go to the owner of their responsibility."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)
    defined = [
        node.name
        for node in _tree(FACADE).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, original)."""
    family = set(_index().owners)
    out: dict[str, tuple[str, str]] = {}
    for node in _tree(FACADE).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner binds these by import, so the facade names each one from its home module
    under ``TYPE_CHECKING`` and ``from kiro_crew.dashboard.handlers._shared import
    redact_credentials`` type-checks."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs."""
    assert _shared.logger.name == FACADE
    holders = [m for m in _family() if "logger" in vars(m)]
    assert len(holders) >= 5
    assert all(vars(m)["logger"] is logging.getLogger(FACADE) for m in holders)


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers keep importing the facade: an owner import from outside the family would
    bind its own copy of a name and stop seeing a patch the facade fans out."""
    family = {
        f"dashboard/handlers/{name.rpartition('.')[2]}.py" for name in (FACADE, *_index().owners)
    }
    sources = source_corpus.iter_candidate_sources(require_all=("_shared_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "dashboard/handlers/_shared_memory_stores.py" in importers  # owners import lower ones
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.dashboard.handlers._shared_scrub import x\n",
        root / "other.py": "from kiro_crew.dashboard.handlers import _shared_access as acc\n",
        root / "dashboard" / "handlers" / "rel.py": "from ._shared_workspace import a\n",
        root / "dashboard" / "handlers" / "deep" / "up.py": "from .. import _shared_scrub\n",
        root / "facade_user.py": "from kiro_crew.dashboard.handlers._shared import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "dashboard/handlers/rel.py",
        "dashboard/handlers/deep/up.py",
    }


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_json_object_body_guard.py" in walked
