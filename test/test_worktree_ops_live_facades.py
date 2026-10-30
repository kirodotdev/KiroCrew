"""Dev Fleet's split ``worktree_ops`` and ``live`` keep the patch seams each had as one module.

Both modules under ``kiro_crew.apps.builtins.dev_fleet`` are mirror facades
(:mod:`kiro_crew._facade_mirror`) over the ``<stem>_<role>`` owners beside them. Callers,
``server.py``'s re-export of their ``__all__`` and the tests still reach every name as
``<stem>.<name>``, and the moved code reads its names as plain module globals, so a patch
through a facade reaches that code only because the mirror writes it into every module
holding the name. Each facade also keeps code of its own -- the code that rebinds the
state it shares -- and reads the owners' names it needs by importing them.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules: one
object per name, the manifest, owners never importing their facade, ``create=True``
patches, and the reach of every name the test trees patch through it. These tests pin what
is particular to these two: the patch scan sees the seams each is known to have, its owners
are exactly its ``<stem>_*.py`` siblings loaded lowest layer first, every name it serves
from outside its family stays visible to type checkers, ``__all__`` -- the table
``server.py`` builds its re-exports from -- still resolves on the facade, and a global an
owner rebinds is read through the facade at its current value.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.dev_fleet import live, server, worktree_ops

pytestmark = pytest.mark.xdist_group(name="tree_scan_worktree_ops_live_facades")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("corpus_gate", "owners_in_layer_order_each_facade")

PKG = "kiro_crew.apps.builtins.dev_fleet"


@dataclasses.dataclass(frozen=True)
class Facade:
    """One split module: the facade, the names tests are known to patch through it, and a
    global one of its owners rebinds at run time."""

    module: ModuleType
    #: Seams the scan must find, so a scan that stops matching goes red instead of letting
    #: the shared reach check pass over nothing.
    known_seams: frozenset[str]
    #: (owner leaf, name): a module global the owner's own code rebinds with ``global``.
    rebound: tuple[str, str]

    @property
    def stem(self) -> str:
        return self.module.__name__.rpartition(".")[2]

    @property
    def index(self) -> _facade_mirror.MirrorIndex:
        return vars(self.module)[_facade_mirror.INDEX_ATTRIBUTE]

    def owner_paths(self) -> dict[str, Path]:
        directory = Path(self.module.__file__ or "").resolve().parent
        return {f"{PKG}.{path.stem}": path for path in sorted(directory.glob(f"{self.stem}_*.py"))}


FACADES = (
    Facade(
        worktree_ops,
        # each owner's patched helpers, an imported name, and a name the facade binds too
        frozenset(
            {
                "_SYNC_RID",
                "_background_tasks_disabled",
                "_pod_down",
                "_read_pin_strict",
                "_reclaim_pod_locked",
                "_venv_python",
                "_worktree_remove",
                "sandboxed_spawn_argv",
            }
        ),
        ("worktree_ops_sync", "_SYNC_RID"),
    ),
    Facade(
        live,
        # each owner's patched registries and helpers, imported modules, the facade's own
        frozenset(
            {
                "_MAKE_LIVE_COMMITTED",
                "_POD_STARTS",
                "_REMOVAL_LEASES",
                "_dropin_path",
                "_gateway_backend",
                "_in_pod",
                "_live_worktree_path",
                "_staged_target",
                "_restart_gateway",
                "_running_checkout",
                "removal_lease_lost",
                "shutil",
                "sys",
            }
        ),
        ("live_service", "_GATEWAY_SERVICE_ACTIVE"),
    ),
)
each_facade = pytest.mark.parametrize("facade", FACADES, ids=[f.stem for f in FACADES])


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/``, and the builtin apps' own test packages."""
    root = source_corpus.repo_root()
    test_root = source_corpus.repo_root() / "test"
    found = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root):
            found.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel:
            found.append(path)
    return found


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> dict[str, frozenset[str]]:
    """Facade stem -> every name the test trees patch through it, from one scan of the tree."""
    spellings = {facade.module.__name__: facade.module.__name__ for facade in FACADES}
    found: dict[str, set[str]] = {facade.stem: set() for facade in FACADES}
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "dev_fleet" not in text:
            continue
        for module, names in facade_mirror_scan.patched_names(text, spellings).items():
            found[module.rpartition(".")[2]] |= names
    return {stem: frozenset(names) for stem, names in found.items()}


@each_facade
def test_the_patch_scan_finds_every_known_seam(facade: Facade) -> None:
    """Non-vacuity: a scan that finds nothing would pass the shared reach check over nothing."""
    assert facade.known_seams
    assert sorted(facade.known_seams - _corpus_patched_names()[facade.stem]) == []


def _home_imports(facade: Facade) -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family, with their home."""
    family = set(facade.index.owners)
    found: dict[str, tuple[str, str]] = {}
    for path in facade.owner_paths().values():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.module == "__future__":
                continue
            home = node.module or ""
            if node.level:
                home = PKG + (f".{node.module}" if node.module else "")
            if not home.startswith("kiro_crew") or home in family:
                continue
            for alias in node.names:
                if f"{home}.{alias.name}" not in family:
                    found.setdefault(alias.asname or alias.name, (home, alias.name))
    return found


def _home_manifest(facade: Facade) -> dict[str, tuple[str, str]]:
    """The facade's ``TYPE_CHECKING`` imports from outside the family: name -> (home, name)."""
    family = set(facade.index.owners)
    tree = ast.parse(Path(facade.module.__file__ or "").read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in family:
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


@each_facade
def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers(
    facade: Facade,
) -> None:
    """Before the split the module bound these by import, so ``<stem>.<name>`` type-checked.
    Now only an owner holds them, so the facade names each one from its home module under
    ``TYPE_CHECKING``, and exactly those."""
    served = set(facade.index.exports)
    defined: set[str] = set()
    for path in facade.owner_paths().values():
        defined |= _facade_mirror._defined_names(ast.parse(path.read_text(encoding="utf-8")))
    expected = {
        name: home
        for name, home in _home_imports(facade).items()
        if name in served and name not in defined
    }
    assert expected, "no served name comes from outside the family -- the scan went blind"
    assert _home_manifest(facade) == expected


@each_facade
def test_the_export_table_server_builds_still_resolves_on_the_facade(facade: Facade) -> None:
    """``server.py`` re-exports each name in the facade's ``__all__`` and forwards writes of
    it to the facade, so every one must be bound by the facade or served by the mirror, as
    the very object the owner holding it binds."""
    index = facade.index
    unresolved = []
    for name in facade.module.__all__:
        holders = index.holders.get(name, ())
        if name in vars(facade.module):
            continue
        if not holders or getattr(facade.module, name) is not vars(
            importlib.import_module(holders[0])
        ).get(name):
            unresolved.append(name)
    assert unresolved == []
    assert {
        name for name, owner in server._EXPORT_OWNERS.items() if owner == facade.module.__name__
    } == set(facade.module.__all__)


@each_facade
def test_a_global_an_owner_rebinds_is_read_through_the_facade_at_its_current_value(
    facade: Facade, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's own ``global`` write lands in the owner alone, so the facade must serve
    the name rather than bind a copy that would go stale on the first rebind."""
    leaf, name = facade.rebound
    owner = importlib.import_module(f"{PKG}.{leaf}")
    assert name not in vars(facade.module), f"the facade binds {name}, a copy that goes stale"
    assert facade.index.holders[name] == (owner.__name__,)
    sentinel = object()
    monkeypatch.setitem(vars(owner), name, sentinel)  # what the owner's ``global`` write does
    assert getattr(facade.module, name) is sentinel
    assert getattr(server, name) is sentinel
