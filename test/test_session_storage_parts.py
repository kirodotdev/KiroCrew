"""The session-storage facade keeps the patch seams the one module had.

``kiro_crew.session_storage`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
``kiro_crew.session_storage_parts`` owners. Callers, tests and documents still reach every
name as ``kiro_crew.session_storage.<name>``, and the moved code reads its names as plain
module globals, so a patch through the facade reaches that code only because the mirror
writes it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules (one
object per name, the manifest, owners never importing their facade, no shared global
rebound). These tests pin what is particular to this facade: every name the test trees
patch through it reaches every module holding it, and the scan finds the seams it is known
to have; the owners are exactly the ``session_storage_parts`` modules, layered lowest
first, the package init defines nothing and no two owners define one name; the facade
binds every name its owners define but the two caches ``store_inventory`` rebinds, and
forwards no table; the names served from outside the family stay visible to type
checkers; the store keeps logging, and naming its exceptions, under the facade's name; and
a facade write of the scan cache is still the cache a cached scan answers from.
"""

from __future__ import annotations

import ast
import functools
import importlib
import logging
import sys
import traceback
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, session_storage
from kiro_crew.session_storage import SessionIndex

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_storage_parts")

FACADE = "kiro_crew.session_storage"
PACKAGE = "kiro_crew.session_storage_parts"
PARTS_DIR = Path(session_storage.__file__).resolve().parent / "session_storage_parts"
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, covering each of these kinds
#: of seam: helpers several owners call, a constant another owner reads, a constant
#: only its own owner reads, names the store only imports, a read-side pass, and a public
#: function an owner above it calls. The scan below must find all of them, so a scanner
#: that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_FD_SAFE_DELETE",
        "_MANIFEST_RECORD_CAP",
        "_crew_sessions_dir",
        "_move_file_exclusive",
        "_scan_raw_uncached",
        "atomic_write",
        "fsync_dir",
        "list_trash",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    return session_storage.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _family() -> list[ModuleType]:
    return [session_storage, *(sys.modules[name] for name in _index().owners)]


def _owner_paths() -> dict[str, Path]:
    return {
        f"{PACKAGE}.{path.stem}": path
        for path in sorted(PARTS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


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
        if "session_storage" not in text:
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_trash_write_durability.py" in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass the reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``session_storage.<name>`` was what
    every reader of that name read. Each module of the family that binds a patched name
    must see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(session_storage, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(session_storage, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(session_storage, name) is not sentinel:
                stale.append(FACADE)
        restored = {module.__name__: vars(module).get(name) for module in holders}
        if stale or restored != before:
            missed[name] = stale or ["undo"]
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_are_every_parts_module_in_layer_order() -> None:
    """``_OWNERS`` lists every ``session_storage_parts`` module, and each owner imports
    only owners listed before it -- the order a read of a served name resolves in."""
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


def test_the_package_init_defines_nothing() -> None:
    """The package is only where the owners live: importing it binds no name of the store."""
    init = PARTS_DIR / "__init__.py"
    assert _facade_mirror._defined_names(ast.parse(init.read_text(encoding="utf-8"))) == set()


def test_the_facade_binds_every_name_its_owners_define_but_the_rebound_caches() -> None:
    """``session_storage.py`` held its definitions in its own namespace, and still does:
    each name an owner defines is bound here too, so ``vars(session_storage)`` still holds
    every definition and a ``mock.patch.object(session_storage, ...)`` of one of them is
    undone by one write. The scan and co-tenant caches are the exception, because ``store_inventory`` rebinds
    them (``global``) and a copy here would go stale; they are served instead."""
    defined = {
        name
        for path in _owner_paths().values()
        for name in _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert defined, "no owner defines anything -- the scan went blind"
    rebound = ["_cotenant_cache", "_scan_cache"]
    assert sorted(name for name in defined if name not in vars(session_storage)) == rebound
    assert sorted(name for name in _index().exports if name in defined) == rebound


def test_no_two_owners_define_the_same_name() -> None:
    """Each definition moved to exactly one owner, so a name has one home to edit."""
    homes: dict[str, list[str]] = {}
    for owner, path in _owner_paths().items():
        for name in _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8"))):
            homes.setdefault(name, []).append(owner)
    assert {name: owners for name, owners in homes.items() if len(owners) > 1} == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``session_storage.py`` was a plain module with no forwarding table, so there is no
    legacy owner fetch to keep: the facade forwards no table, keeps the default
    ``forward_reads``, and every served name comes from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family -> (home, original)."""
    found: dict[str, tuple[str, str]] = {}
    for path in _owner_paths().values():
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module.startswith(PACKAGE):
                    continue
                for alias in node.names:
                    found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _manifest() -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports: name -> (home, original)."""
    tree = ast.parse(Path(session_storage.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """At BASE the store bound these by import, so ``from kiro_crew.session_storage import
    ConversationLog`` type-checked. Now an owner holds them, so the facade names each one
    from its home module under ``TYPE_CHECKING``, and exactly those beside the two caches."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    outside = {name: home for name, home in _manifest().items() if not home[0].startswith(PACKAGE)}
    assert outside == expected
    inside = {name for name, home in _manifest().items() if home[0].startswith(PACKAGE)}
    assert inside == {"_cotenant_cache", "_scan_cache"}


def test_the_store_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every module that logs, so
    a ``caplog.at_level(logger="kiro_crew.session_storage")`` still hears the moved code."""
    assert session_storage.logger is logging.getLogger(FACADE)
    others = [
        m.__name__
        for m in _family()
        if vars(m).get("logger", session_storage.logger) is not session_storage.logger
    ]
    assert others == []


@pytest.mark.parametrize("name", ["SessionStorageError", "_OversizedManifestRecord"])
def test_an_exception_is_named_by_the_facade_in_a_traceback(name: str) -> None:
    """A traceback names an exception by its class's dotted name. Both classes moved to
    owners and still read as ``kiro_crew.session_storage.<name>``."""
    error_type = getattr(session_storage, name)
    assert error_type.__module__ == FACADE
    shown = "".join(traceback.format_exception_only(error_type("refused")))
    assert shown.startswith(f"{FACADE}.{name}: refused")


def test_a_facade_write_of_the_scan_cache_is_the_cache_a_cached_scan_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session_storage._scan_cache`` is the pass a cached read answers from and stores.
    A write through the facade is what the next cached scan reads, and the pass a scan
    stores is what the facade reads back: the facade serves the name from its one owner,
    where a copy bound on the facade would read stale. The cache is process state, so the
    test empties it on both sides instead of leaving an undo to restore a live pass."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIRO_HOME", str(tmp_path / "crew" / "kiro"))
    assert "_scan_cache" not in vars(session_storage)
    session_storage.invalidate_scan_cache()
    try:
        assert session_storage._scan_cache is None
        assert session_storage._scan_units(SessionIndex(), cached=True) == []
        stored = session_storage._scan_cache
        assert stored is not None and stored[2] == []
        planted = session_storage._RawUnit(uid="planted", sid="", stems=(), bytes=7, mtime=0.0)
        session_storage._scan_cache = (stored[0], stored[1], [planted])
        units = session_storage._scan_units(SessionIndex(), cached=True)
        assert [unit.uid for unit in units] == ["planted"]
    finally:
        session_storage.invalidate_scan_cache()
    assert session_storage._scan_cache is None
