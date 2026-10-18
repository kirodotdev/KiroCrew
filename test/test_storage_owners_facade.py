"""The AWS Control drive storage facade keeps the patch seams the one module had.

``kiro_crew.apps.builtins.aws_control.backend.storage`` is a mirror facade
(:mod:`kiro_crew._facade_mirror`) over the ``storage_drive``, ``storage_staging`` and
``storage_versions`` owners, and keeps the object operations itself. Callers and tests
still reach every name as ``...backend.storage.<name>``, and the moved code reads its
names as plain module globals, so a patch through the facade reaches it only because the
mirror writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules.
These tests pin what is particular to this one: every name the test trees patch through
it, by any spelling they use, reaches every module holding it; the owners are exactly the
``storage_*`` siblings, loaded lowest layer first; the two partial-failure errors keep the
facade as their home; and the names it serves from outside the family stay visible to
type checkers.
"""

from __future__ import annotations

import ast
import functools
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest
import source_corpus
from facade_mirror_scan import names_by_target, needs_syntax_tree, patch_events, text_patched_names

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.aws_control.backend import storage

pytestmark = pytest.mark.xdist_group(name="tree_scan_storage_owners_facade")

BACKEND = "kiro_crew.apps.builtins.aws_control.backend"
FACADE = f"{BACKEND}.storage"
BACKEND_DIR = Path(storage.__file__).resolve().parent

#: Names the test trees are known to patch through the facade, from every kind of seam
#: the engine has: the CLI chokepoint every owner calls, a function a moved owner reads,
#: a limit an owner reads, a name an owner only imports, and an operation the facade
#: keeps. The scan below must find all of them, so a scanner that stops matching goes
#: red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_checked",
        "_harden_bucket",
        "_preview_staging_parent",
        "staging_root",
        "effective_sandbox_mode",
        "_VERSION_ROWS_MAX",
        "find_drive",
        "list_object_versions",
        "put_file",
        "SEARCH_MAX_RESULTS",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return storage.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_paths() -> dict[str, Path]:
    return {f"{BACKEND}.{path.stem}": path for path in sorted(BACKEND_DIR.glob("storage_*.py"))}


def _family() -> list[ModuleType]:
    return [storage, *(sys.modules[name] for name in _index().owners)]


@functools.lru_cache(maxsize=1)
def _spellings() -> dict[str, str]:
    """The facade's own name, and every src module's module-level binding of it,
    a ``TYPE_CHECKING`` manifest's included: a facade serves what its manifest names."""
    spellings = {FACADE: FACADE}
    for path, _text, tree in source_corpus.parsed_candidates(require_any=("storage",)):
        root = source_corpus.src_root()
        parts = [root.name, *path.relative_to(root).with_suffix("").parts]
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        nodes = [*tree.body, *(s for n in tree.body if isinstance(n, ast.If) for s in n.body)]
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if f"{node.module}.{alias.name}" == FACADE and module != FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = FACADE
    return spellings


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/`` and the builtin apps' test directories."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    spellings = _spellings()
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "aws_control" not in text:
            continue
        found |= text_patched_names(text, spellings).get(FACADE, set())
        if needs_syntax_tree(text, spellings):
            try:
                events = patch_events(text, spellings)
            except SyntaxError:
                continue
            found |= names_by_target(events, {FACADE}).get(FACADE, set())
    return frozenset(found)


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """A write to ``storage.<name>`` is what every reader of that name reads: each module of
    the family that binds a patched name sees the patched object, and the undo restores
    all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(storage, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(storage, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(storage, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_storage_sibling_in_layer_order() -> None:
    """``_OWNERS`` lists every ``storage_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert sorted(owners) == sorted(_owner_paths())
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_paths()[owner].read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """No reader fetches a name of ``storage`` through ``importlib.import_module``, so the
    facade forwards no legacy table and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_partial_failure_errors_keep_the_facade_as_their_home() -> None:
    """A traceback, a logged error type and ``pickle`` name these errors by module, and
    they name ``storage``, the engine's one import path; the class itself is the one
    ``storage_versions`` defines and raises."""
    versions = sys.modules[f"{BACKEND}.storage_versions"]
    for name in ("DeleteObjectsPartialFailure", "PartialVersionDelete"):
        cls = getattr(storage, name)
        assert cls is vars(versions)[name]
        assert cls.__module__ == FACADE


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
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family."""
    family = set(_index().owners)
    tree = ast.parse(Path(storage.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner holds each of these, so the facade names it from its home module under
    ``TYPE_CHECKING``, and exactly those: ``storage.data_home`` type-checks through the facade
    path as it does at run time."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected
