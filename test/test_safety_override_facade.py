"""``kiro_crew.safety_override`` keeps the patch seams and the surface the one module had.

``kiro_crew.safety_override`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
``safety_override_records``, ``safety_override_permits``, ``safety_override_breadcrumb``
and ``safety_override_grant``. Callers, tests and documents still reach every name as
``kiro_crew.safety_override.<name>``, and the moved code reads its names as plain module
globals, so a patch through the facade reaches that code only because the mirror writes
it into every module holding the name.

``test/test_facade_mirror_guards.py`` holds every mirror facade to the shared contract.
These tests pin what is particular to this one: every name the test trees patch through
it reaches every module holding it; the owners are exactly the ``safety_override_*.py``
siblings, each importing only the owners before it; each global the code rebinds at run
time lives in the one owner that rebinds it, so a read or a patch through the facade is
that owner's binding; the ceiling hooks are registered once, at import; names served from
outside the family type-check through the facade; and the family logs under the facade's
name.
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

from kiro_crew import _facade_mirror, safety_override
from kiro_crew.platform import context as ctx_mod

pytestmark = pytest.mark.xdist_group(name="tree_scan_safety_override_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.safety_override"
SRC_DIR = Path(safety_override.__file__).resolve().parent
#: The owners, lowest layer first.
OWNERS = (
    "kiro_crew.safety_override_records",
    "kiro_crew.safety_override_permits",
    "kiro_crew.safety_override_breadcrumb",
    "kiro_crew.safety_override_grant",
)
_THIS_FILE = Path(__file__).resolve()

#: Names the test trees are known to patch through the facade, one of each seam: the SEL
#: accessor every owner calls, the governance permit the verdict push asks, the data-home resolver and the breadcrumb path and queue the class reaches,
#: the pushed verdict's own state, the breadcrumb drain, and the two names the
#: declared-grant entry point reads. The scan below must find all of them.
_KNOWN_SEAMS = frozenset(
    {
        "sel",
        "approval_mode_permitted",
        "config_dir",
        "_breadcrumb_path",
        "_enqueue_breadcrumb",
        "_yolo_policy_permitted",
        "_yolo_policy_resolved",
        "flush_breadcrumb_writes",
        "declared_grant_permitted",
        "apply_config_duration",
    }
)

#: The globals the family rebinds at run time with ``global``, and the owner holding each.
_REBOUND_GLOBALS = {
    "_breadcrumb_pending": "kiro_crew.safety_override_breadcrumb",
    "_breadcrumb_worker": "kiro_crew.safety_override_breadcrumb",
    "_singleton": "kiro_crew.safety_override_grant",
    "_suspended_trust_restore": "kiro_crew.safety_override_grant",
    "_yolo_policy_permitted": "kiro_crew.safety_override_grant",
    "_yolo_policy_resolved": "kiro_crew.safety_override_grant",
}


def _index() -> _facade_mirror.MirrorIndex:
    return safety_override.__dict__[_facade_mirror.INDEX_ATTRIBUTE]


def _owner_path(owner: str) -> Path:
    return SRC_DIR / f"{owner.rsplit('.', 1)[1]}.py"


def _family() -> list[ModuleType]:
    return [safety_override, *(sys.modules[name] for name in _index().owners)]


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
        if "safety_override" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {FACADE: FACADE})
        found |= names.get(FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``safety_override.<name>`` was what
    every reader of that name read. Each module of the family that binds a patched name
    must see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(safety_override, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(safety_override, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(safety_override, name) is not sentinel:
                stale.append(FACADE)
        checked += 1
        if stale:
            missed[name] = stale
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


def test_a_patched_sel_reaches_the_class_and_the_breadcrumb_reader() -> None:
    """``sel`` is defined in the records owner and read by ``SafetyOverride`` and by
    ``take_dropped_grant`` in two other owners; one patch through the facade is the
    accessor both of them call."""
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(safety_override, "sel", lambda: sentinel)
        for owner in ("kiro_crew.safety_override_grant", "kiro_crew.safety_override_breadcrumb"):
            assert vars(sys.modules[owner])["sel"]() is sentinel, owner
    assert vars(sys.modules["kiro_crew.safety_override_grant"])["sel"] is safety_override.sel


def test_the_clock_the_tests_patch_resolves_through_the_facade() -> None:
    """``mock.patch("kiro_crew.safety_override.time.monotonic")`` patches the stdlib clock
    the facade names: ``safety_override.time`` is still that module, and it is the one the
    grant owner's ``SafetyOverride`` reads."""
    import time

    assert safety_override.time is time
    assert vars(sys.modules["kiro_crew.safety_override_grant"])["time"] is time


def test_the_owners_load_lowest_layer_first() -> None:
    """``_OWNERS`` names every ``safety_override_*.py`` sibling, and each owner imports only
    owners listed before it -- never the facade."""
    owners = list(_index().owners)
    assert owners == list(OWNERS)
    on_disk = sorted(f"kiro_crew.{p.stem}" for p in SRC_DIR.glob("safety_override_*.py"))
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
    """``safety_override.py`` was a plain module with no forwarding table, so the facade
    forwards no legacy table and keeps the default ``forward_reads``."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def _global_statements() -> dict[str, set[str]]:
    """Module -> the names its code rebinds with ``global``, over the facade and owners."""
    out: dict[str, set[str]] = {}
    for module, path in (
        (FACADE, Path(safety_override.__file__)),
        *((o, _owner_path(o)) for o in OWNERS),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names
        }
        if names:
            out[module] = names
    return out


def test_each_rebound_global_lives_in_the_one_owner_that_rebinds_it() -> None:
    """A ``global`` write lands in one namespace, so the global, every function that
    rebinds it and every reader share one owner, and the facade binds no copy: a read
    through the facade is the owner's current binding."""
    found = {name: owner for owner, names in _global_statements().items() for name in names}
    assert found == _REBOUND_GLOBALS
    for name, owner in _REBOUND_GLOBALS.items():
        assert name not in vars(safety_override), name
        assert [m.__name__ for m in _family() if name in vars(m)] == [owner], name
        assert _index().exports[name] == owner, name


def test_a_read_through_the_facade_follows_the_singleton_its_owner_holds() -> None:
    """``safety_override()`` creates the singleton in the grant owner; the facade serves
    that owner's current binding, so ``reset_singleton`` and a fresh ``safety_override()``
    are what a reader of ``safety_override._singleton`` sees."""
    grant = sys.modules["kiro_crew.safety_override_grant"]
    safety_override.reset_singleton()
    assert safety_override._singleton is None
    instance = safety_override.safety_override()
    assert safety_override._singleton is instance is vars(grant)["_singleton"]
    safety_override.reset_singleton()
    assert safety_override._singleton is None


def test_the_ceiling_hooks_are_registered_once_and_reach_the_verdict() -> None:
    """The facade registers the invalidate and install hooks at import, once each; the
    invalidate hook is the grant owner's function, which rebinds the verdict state there."""
    grant = sys.modules["kiro_crew.safety_override_grant"]
    invalidate = [
        h for h in ctx_mod._CEILING_INVALIDATE_HOOKS if h is grant._on_ceiling_invalidating
    ]
    install = [
        h for h in ctx_mod._CEILING_INSTALL_HOOKS if h is safety_override._on_ceiling_installed
    ]
    assert len(invalidate) == 1
    assert len(install) == 1
    assert safety_override._on_ceiling_invalidating is grant._on_ceiling_invalidating


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
    tree = ast.parse(Path(safety_override.__file__).read_text(encoding="utf-8"))
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
    under ``TYPE_CHECKING``, and ``from kiro_crew.safety_override import config_dir``
    type-checks as it did before the split."""
    served = set(_index().exports)
    expected = {name: home for name, home in _home_imports().items() if name in served}
    assert {"config_dir", "atomic_write", "current_context"} <= set(expected)
    assert _home_manifest() == expected


def test_the_family_logs_under_the_facade_name() -> None:
    """One logger, still named for the facade module, held by every owner that logs:
    ``caplog`` filters keyed on ``kiro_crew.safety_override`` keep seeing the records."""
    assert safety_override.logger.name == FACADE
    loggers = {m.__name__: vars(m)["logger"] for m in _family() if "logger" in vars(m)}
    expected = {
        "kiro_crew.safety_override_permits",
        "kiro_crew.safety_override_breadcrumb",
        "kiro_crew.safety_override_grant",
    }
    assert expected <= set(loggers)
    others = sorted(name for name, value in loggers.items() if value is not safety_override.logger)
    assert others == []


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_safety_override.py" in walked
