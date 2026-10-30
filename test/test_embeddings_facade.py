"""The embedding runtime's facade keeps the patch seams the one module had.

``kiro_crew.embeddings`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over seven
sibling owners and defines nothing itself. Callers, tests and documents still reach every
name as ``kiro_crew.embeddings.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this facade: every name the test trees patch through it
reaches every module holding it; the owners load lowest layer first; each global the code
rebinds at run time lives in the one owner that rebinds it, so a read through the facade
is that owner's current binding; the vendored-runtime directory still resolves beside the
package; names served from outside the family type-check through the facade; and the
family logs under the facade's name.
"""

from __future__ import annotations

import ast
import functools
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror, embeddings

pytestmark = pytest.mark.xdist_group(name="tree_scan_embeddings_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.embeddings"
SRC_DIR = Path(embeddings.__file__).resolve().parent
#: The owners, lowest layer first.
OWNERS = (
    "kiro_crew.embeddings_settings",
    "kiro_crew.embeddings_native",
    "kiro_crew.embeddings_gguf",
    "kiro_crew.embeddings_model",
    "kiro_crew.embeddings_llama",
    "kiro_crew.embeddings_shared",
    "kiro_crew.embeddings_download",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam the
#: family has: the config path and directory, the vendored directory and library loader,
#: the native-runtime checks, the model file probe, the shared embedder, the download
#: manager, the sync embed cache's stripes, and the model verification thread. The scan
#: below must find all of them.
_KNOWN_SEAMS = frozenset(
    {
        "config_path",
        "_VENDOR_DIR",
        "_load_llama_class",
        "_missing_windows_msvc_runtime",
        "model_file_present",
        "_shared_embedder",
        "get_shared_embedder",
        "_sync_embed_stripes",
        "_model_verification_thread",
    }
)

#: The globals the family rebinds at run time with ``global``, and the owner holding each.
_REBOUND_GLOBALS = {
    "_model_verification_thread": "kiro_crew.embeddings_model",
    "_last_custom_model_error": "kiro_crew.embeddings_model",
    "_shared_embedder": "kiro_crew.embeddings_shared",
    "_backend_factory": "kiro_crew.embeddings_shared",
    "_sync_embed_cache_backend": "kiro_crew.embeddings_shared",
    "_download_manager": "kiro_crew.embeddings_download",
    "_download_task": "kiro_crew.embeddings_download",
}


def _index() -> _facade_mirror.MirrorIndex:
    return embeddings.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return SRC_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [embeddings, *(sys.modules[name] for name in _index().owners)]


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
        if "embeddings" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``embeddings.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(embeddings, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(embeddings, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(embeddings, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_the_owners_load_lowest_layer_first() -> None:
    """``_OWNERS`` is the seven owners, and each owner imports only owners listed before
    it -- the order the facade loads them and a read resolves in."""
    owners = list(_index().owners)
    assert owners == list(OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
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
    """``embeddings.py`` was a plain module with no forwarding table, so the facade forwards
    no legacy table and keeps the default ``forward_reads``, and every served name comes
    from the index."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def _global_statements() -> dict[str, set[str]]:
    """Owner -> the names its code rebinds with ``global``."""
    out: dict[str, set[str]] = {}
    for owner in _index().owners:
        tree = ast.parse(_owner_path(owner).read_text(encoding="utf-8"))
        names = {
            name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
        }
        if names:
            out[owner] = names
    return out


def test_each_rebound_global_lives_in_the_one_owner_that_rebinds_it() -> None:
    """A ``global`` write lands in one namespace, so the global, every function that
    rebinds it and every reader share one owner, and the facade binds no copy: a read
    through the facade is the owner's current binding."""
    found = {name: owner for owner, names in _global_statements().items() for name in names}
    assert found == _REBOUND_GLOBALS
    for name, owner in _REBOUND_GLOBALS.items():
        assert name not in vars(embeddings), name
        assert [m.__name__ for m in _family() if name in vars(m)] == [owner], name
        assert _index().exports[name] == owner, name


def test_a_read_through_the_facade_follows_a_rebinding_in_its_owner() -> None:
    """The facade serves the owner's current binding, not a copy taken at import. The
    owner alone binds ``_backend_factory``, so writing it there is what a rebinding in
    ``register_embedding_backend`` does."""
    shared = sys.modules["kiro_crew.embeddings_shared"]
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(shared, "_backend_factory", sentinel)
        assert embeddings._backend_factory is sentinel
    assert embeddings._backend_factory is vars(shared)["_backend_factory"]


def test_the_vendored_runtime_directory_still_resolves_beside_the_package() -> None:
    """``_VENDOR_DIR`` is computed from its module's own path, so its owner sits beside
    ``embeddings.py`` in ``src/kiro_crew`` and the directory is the one it always was."""
    assert embeddings._VENDOR_DIR == SRC_DIR / "_vendor"
    assert Path(sys.modules["kiro_crew.embeddings_native"].__file__).resolve().parent == SRC_DIR


def test_the_sync_embed_function_still_takes_a_priority() -> None:
    """``_shared_sync_embed.accepts_priority`` is set by a top-level statement beside the
    function; vector memory reads it to pass its priority through."""
    assert embeddings._shared_sync_embed.accepts_priority is True
    assert embeddings.make_sync_embed_fn() is embeddings._shared_sync_embed


def _home_imports() -> dict[str, tuple[str, str]]:
    """Served names an owner imports from a project module outside the family."""
    family = set(_index().owners)
    found: dict[str, tuple[str, str]] = {}
    for owner in _index().owners:
        for node in ast.parse(_owner_path(owner).read_text(encoding="utf-8")).body:
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
    tree = ast.parse(Path(embeddings.__file__).read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """An owner binds these by import, so the facade names each one from its home module
    under ``TYPE_CHECKING`` and ``from kiro_crew.embeddings import operator_lib_path_override``
    type-checks as it did before the split."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every owner that logs:
    ``caplog`` filters keyed on ``kiro_crew.embeddings`` keep seeing the runtime's
    records."""
    assert embeddings.logger.name == FACADE
    loggers = {m.__name__: vars(m)["logger"] for m in _family() if "logger" in vars(m)}
    assert {"kiro_crew.embeddings_native", "kiro_crew.embeddings_llama"} <= set(loggers)
    others = sorted(name for name, value in loggers.items() if value is not embeddings.logger)
    assert others == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_embeddings.py" in walked
