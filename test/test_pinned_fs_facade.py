"""``kiro_crew.pinned_fs`` keeps the patch seams and the surface the one module had.

``kiro_crew.pinned_fs`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners in
its ``_OWNERS``, lowest layer first: ``pinned_fs_primitives``, ``pinned_fs_dirs`` and ``pinned_fs_files``. It keeps its hand-written ``__all__``, the two
openers under a pinned parent (:func:`open_in_pinned_parent`, :func:`open_dir_pinned`) and the tree operations: :func:`stage_tree_pinned`
and the verified scan, removal and put-back of a tree. Every name an owner defines
still resolves as ``kiro_crew.pinned_fs.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it, and the scan finds the seams it is known to have; the owner list and
its layering; that every public name the module held before the split still type-checks
through it; that ``__all__`` is still the hand-written BASE list and each of its names
resolves; that ``PinnedPathRefusal`` and ``fd_real_path`` keep the facade as their
module;
and the frozen import inventory that stands in for the F401 check
``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, pinned_fs

pytestmark = pytest.mark.xdist_group(name="tree_scan_pinned_fs_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "owner_layering",
    "one_home_per_name",
    "home_manifest",
)

FACADE = "kiro_crew.pinned_fs"
OWNERS = (
    "kiro_crew.pinned_fs_primitives",
    "kiro_crew.pinned_fs_dirs",
    "kiro_crew.pinned_fs_files",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: a probe every owner reads, a helper the facade's own tree walk reads, a
#: helper an owner and the facade both read, and a function the facade defines. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "supports_pinned_walk",
        "supports_pinned_tree_walk",
        "open_dir_pinned",
        "pin_parent",
        "open_in_pinned_parent",
        "copy_file_pinned",
        "stat_at",
        "unlink_verified",
        "remove_dir_verified",
        "remove_tree_pinned",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``pinned_fs_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.pinned_fs(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bpinned_fs\b(?!_)|[^\n]*?\bpinned_fs\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Callable Iterable Iterator Mapping Path PurePath _stat atomic_write atomic_write_at
    contextmanager dataclass errno open_file_no_reparse os pin_directory shutil stat suppress
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(pinned_fs)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [pinned_fs, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew import pinned_fs\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(pinned_fs, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.pinned_fs as facade\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(facade, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.pinned_fs.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.pinned_fs.D', 1)\n",
        {"D"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-named-like-the-facade": (
        "from kiro_crew import pinned_fs_primitives as pinned_fs\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(pinned_fs, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.pinned_fs_files.Y'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(pinned_fs, name) or isinstance(getattr(pinned_fs, name), ModuleType):
            continue  # a shared module (os, time) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(pinned_fs, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(pinned_fs, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_write_of_the_pinned_walk_probe_reaches_every_reader() -> None:
    """``supports_pinned_walk`` is read by the directory and file owners and by
    ``supports_pinned_tree_walk``, which the facade's tree walk calls; the tests switch it off
    through the facade to take the fallback paths."""
    sentinel = object()
    readers = [m for m in _family() if "supports_pinned_walk" in vars(m)]
    assert {m.__name__ for m in readers} >= {
        "kiro_crew.pinned_fs_primitives",
        "kiro_crew.pinned_fs_dirs",
        "kiro_crew.pinned_fs_files",
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pinned_fs, "supports_pinned_walk", sentinel)
        assert [
            m.__name__ for m in readers if vars(m)["supports_pinned_walk"] is not sentinel
        ] == []
    assert all(vars(m)["supports_pinned_walk"] is not sentinel for m in readers)


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == pinned_fs._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == pinned_fs._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_pinned_fs_sibling() -> None:
    assert pinned_fs.__file__ is not None
    siblings = sorted(
        f"kiro_crew.{path.stem}" for path in Path(pinned_fs.__file__).parent.glob("pinned_fs_*.py")
    )
    assert siblings == sorted(OWNERS)


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.pinned_fs_files import x\n")
    trees[OWNERS[1]] = ast.parse("import kiro_crew.pinned_fs\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[-1]],
        OWNERS[1]: [FACADE],
    }


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


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
                name in OWNERS for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """A module outside the family that imported an owner would hold bindings the
    facade's mirror never indexes, so a patch through the facade would miss it."""
    root = Path(pinned_fs.__file__).resolve().parent
    family = {f"{name.rpartition('.')[2]}.py" for name in (FACADE, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_all=("pinned_fs_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(root)), root
    )
    assert importers & (family - {"pinned_fs.py"})  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owner = OWNERS[0].rpartition(".")[2]
    sources = {
        root / "pkg" / "caller.py": f"from kiro_crew.{owner} import x\n",
        root / "other.py": f"from kiro_crew import {owner} as low\n",
        root / "plain.py": f"import kiro_crew.{owner}\n",
        root / "relative.py": f"from .{owner} import x\n",
        root / "deep" / "up.py": f"from .. import {owner}\n",
        root / "facade_user.py": "from kiro_crew.pinned_fs import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "plain.py",
        "relative.py",
        "deep/up.py",
    }


def test_the_facade_binds_every_public_name_its_owners_define() -> None:
    """A reader of ``vars(facade)`` sees every public name the unsplit module held; only
    private owner names are served without a binding."""
    public = {
        name
        for owner in OWNERS
        for name in _facade_mirror._defined_names(_tree(sys.modules[owner]))
        if not name.startswith("_")
    }
    assert public, "the owners define no public name -- the scan went blind"
    assert sorted(public - set(vars(pinned_fs))) == []
    assert not any(name in _index().exports for name in public)


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family."""
    family = set(OWNERS)
    found: dict[str, tuple[str, str]] = {}
    for owner in OWNERS:
        for node in _tree(sys.modules[owner]).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    out: dict[str, tuple[str, str]] = {}
    for node in _tree(pinned_fs).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(pinned_fs))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """Callers import the moved names from here, so a type checker must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(pinned_fs))) == []


# --------------------------------------------------------------------------- #
# The hand-written export list and the refusal type
# --------------------------------------------------------------------------- #

#: ``__all__`` as the unsplit module wrote it, in its order: what ``import *`` binds.
_BASE_ALL = (
    "PUT_BACK_FAILED",
    "PUT_BACK_NAME_TAKEN",
    "PinnedPathRefusal",
    "PinnedTree",
    "REMOVAL_FAILED",
    "REMOVAL_IDENTITY_CHANGED",
    "REMOVAL_STAGE_FAILED",
    "REMOVAL_UNVERIFIABLE",
    "SKIP_NOT_REGULAR",
    "SKIP_SYMLINK",
    "SKIP_UNREADABLE_ENTRY",
    "SKIP_VANISHED",
    "SkipReporter",
    "StagedRemoval",
    "TreeRemoval",
    "close_all",
    "copy_file_pinned",
    "create_and_open_dir_pinned",
    "dir_flags",
    "drain_verified_chain",
    "fatal_skip_reporter",
    "fd_real_path",
    "is_reparse_point",
    "is_regular_at",
    "omits_wanted_data",
    "stat_at",
    "open_dir_pinned",
    "open_in_pinned_parent",
    "open_pinned_descendant_dir",
    "open_verified_chain",
    "pin_parent",
    "put_back_no_clobber",
    "refuse_hardlink_alias",
    "remove_dir_verified",
    "remove_tree_pinned",
    "scan_tree_pinned",
    "stage_tree_pinned",
    "supports_pinned_tree_walk",
    "supports_pinned_walk",
    "unlink_verified",
    "unlink_verified_by_name",
)


def test_the_export_list_is_the_hand_written_base_list() -> None:
    assert tuple(pinned_fs.__all__) == _BASE_ALL


def test_every_exported_name_resolves_on_the_facade() -> None:
    assert sorted(n for n in pinned_fs.__all__ if not hasattr(pinned_fs, n)) == []


@pytest.mark.parametrize("name", ["PinnedPathRefusal"])
def test_a_moved_class_keeps_the_facade_module(name: str) -> None:
    """A refusal that escapes names the one import path in its traceback."""
    value = getattr(pinned_fs, name)
    homes = [m for m in _family()[1:] if name in vars(m)]
    assert homes and all(vars(m)[name] is value for m in homes)
    assert f"{value.__module__}.{value.__qualname__}" == f"{FACADE}.{name}"
    # What pickle and introspection do with a class reference: resolve that dotted name.
    assert getattr(importlib.import_module(value.__module__), value.__qualname__) is value


def test_fd_real_path_is_the_primitives_object_and_keeps_the_facade_module() -> None:
    """Every consumer binds the one ``fd_real_path``, which still reports the module it is
    imported from: ``test_pinned_staging_helper_contract.py`` pins that import path."""
    primitives = sys.modules["kiro_crew.pinned_fs_primitives"]
    assert pinned_fs.fd_real_path is vars(primitives)["fd_real_path"]
    assert pinned_fs.fd_real_path.__module__ == FACADE
    assert pinned_fs.fd_real_path.__code__.co_filename == primitives.__file__


# --------------------------------------------------------------------------- #
# The import inventory
# --------------------------------------------------------------------------- #


def _read_names(source: str) -> set[str]:
    """Every name *source*'s code reads: the loads of a bare name, annotations included."""
    return {
        node.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, from each owner module only names
    that owner defines, and any import the family's code reads, so a later change that
    imports a name for code it adds is not refused. Imports under ``if TYPE_CHECKING:``
    bind nothing at run time and are the mirror guard's business, so they are skipped.
    """
    reads = _read_names(source) | {
        name for owner in OWNERS for name in _read_names(_source(sys.modules[owner]))
    }
    found: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | reads:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    assert unexpected_imports(_source(pinned_fs), _owner_names()) == []


def test_every_base_import_is_still_bound_on_the_facade() -> None:
    """The header is the BASE header: each name it bound is still in the namespace."""
    assert sorted(_BASE_IMPORTS - set(vars(pinned_fs))) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    planted = (
        _source(pinned_fs)
        + "\nimport pickle\nfrom kiro_crew.pinned_fs_primitives import remove_tree_pinned\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "pickle",
        "kiro_crew.pinned_fs_primitives.remove_tree_pinned",
    ]


def test_the_inventory_check_accepts_a_planted_import_the_code_reads() -> None:
    """A new import that code in the module reads is accounted for, not refused."""
    planted = _source(pinned_fs) + "\nimport pickle\n_PROTOCOL = pickle.HIGHEST_PROTOCOL\n"
    assert unexpected_imports(planted, _owner_names()) == []
