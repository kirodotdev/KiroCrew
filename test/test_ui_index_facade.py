"""``kiro_crew.ui_index`` keeps the patch seams and the surface the one module had.

``kiro_crew.ui_index`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
``ui_index_match``, ``ui_index_load`` and ``ui_index_records``, and keeps the public
surface itself: ``find_ui``, ``browse_ui`` and its areas, the guide bindings and
``setting_labels``. Callers, tests and documents still reach every name as
``kiro_crew.ui_index.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``ui_index_*.py``
siblings, each importing only the owners before it; the packaged index paths still
resolve beside the package; the family logs through one logger named for the facade;
and the exception classes an owner defines still report the facade's module.
"""

from __future__ import annotations

import ast
import enum
import functools
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, ui_index

pytestmark = pytest.mark.xdist_group(name="tree_scan_ui_index_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.ui_index"
SRC_DIR = Path(ui_index.__file__).resolve().parent
#: The owners, lowest layer first.
OWNERS = (
    "kiro_crew.ui_index_match",
    "kiro_crew.ui_index_load",
    "kiro_crew.ui_index_records",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam: the two
#: packaged index paths and the read cap ``load_index`` checks, the response cap the
#: facade's own ``find_ui`` and ``browse_ui`` read, and ``find_ui`` itself, which the
#: guide binding calls. The scan below must find all of them.
_KNOWN_SEAMS = frozenset(
    {"AUTO_INDEX_PATH", "INDEX_PATH", "MAX_INDEX_BYTES", "MAX_RESPONSE_BYTES", "find_ui"}
)


def _index() -> _facade_mirror.MirrorIndex:
    return ui_index.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return SRC_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [ui_index, *(sys.modules[name] for name in _index().owners)]


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
        if "ui_index" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``ui_index.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(ui_index, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ui_index, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(ui_index, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_a_patched_index_path_reaches_the_loader() -> None:
    """``load_index`` reads ``INDEX_PATH`` in its owner; a patch through the facade is
    the path it reads, as it was when both lived in one module."""
    load = sys.modules["kiro_crew.ui_index_load"]
    missing = SRC_DIR / "docs" / "no-such-ui-index.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ui_index, "INDEX_PATH", missing)
        assert vars(load)["INDEX_PATH"] is missing
        with pytest.raises(ui_index.IndexUnavailable):
            ui_index.load_index()
    assert vars(load)["INDEX_PATH"] is ui_index.INDEX_PATH


def test_the_owners_load_lowest_layer_first() -> None:
    """``_OWNERS`` names every ``ui_index_*.py`` sibling, and each owner imports only
    owners listed before it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert owners == list(OWNERS)
    on_disk = sorted(f"kiro_crew.{p.stem}" for p in SRC_DIR.glob("ui_index_*.py"))
    assert on_disk == sorted(OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if FACADE in imported:
            later.append(FACADE)
        if later:
            late[owner] = later
    assert late == {}


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``ui_index.py`` was a plain module with no forwarding table, so the facade forwards
    no legacy table and keeps the default ``forward_reads``."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_index_cache_is_one_dict_reached_through_the_facade() -> None:
    """The test trees reset the cache with ``ui_index._cache.update(...)``: the facade must
    serve the very dict ``load_index`` reads, not a copy."""
    load = sys.modules["kiro_crew.ui_index_load"]
    assert "_cache" not in vars(ui_index)
    assert ui_index._cache is vars(load)["_cache"]


def test_the_packaged_index_paths_resolve_beside_the_package() -> None:
    """``INDEX_PATH``, ``AUTO_INDEX_PATH`` and the built-in apps directory are computed from
    their module's own path, so their owners sit beside ``ui_index.py`` in
    ``src/kiro_crew`` and the files are the ones they always were."""
    assert ui_index.INDEX_PATH == SRC_DIR / "docs" / "ui-index.generated.json"
    assert ui_index.AUTO_INDEX_PATH == SRC_DIR / "static" / "dist" / "ui-index.auto.json"
    assert ui_index._BUILTIN_APPS_DIR == SRC_DIR / "apps" / "builtins"
    for owner in OWNERS:
        assert Path(sys.modules[owner].__file__ or "").resolve().parent == SRC_DIR


def test_the_family_logs_through_one_logger_named_for_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.ui_index`` keep seeing the moved code."""
    assert ui_index.logger.name == FACADE
    loggers = {m.__name__: vars(m)["logger"] for m in _family() if "logger" in vars(m)}
    assert "kiro_crew.ui_index_load" in loggers
    assert sorted(name for name, value in loggers.items() if value is not ui_index.logger) == []


def test_an_exception_an_owner_defines_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so an
    exception class an owner defines reports ``kiro_crew.ui_index``, as it did before it
    moved."""
    found = {
        name: value
        for owner in _index().owners
        for name, value in vars(sys.modules[owner]).items()
        if isinstance(value, type)
        and issubclass(value, (BaseException, enum.Enum))
        and name
        in _facade_mirror._defined_names(ast.parse(_owner_path(owner).read_text(encoding="utf-8")))
    }
    assert {"IndexUnavailable", "AutoTierUnavailable"} <= set(found)
    assert {name: cls.__module__ for name, cls in found.items()} == dict.fromkeys(found, FACADE)


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_find_ui.py" in walked
