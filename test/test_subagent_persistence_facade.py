"""``kiro_crew.subagent_persistence`` keeps the patch seams and the surface the one module had.

``kiro_crew.subagent_persistence`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners in
its ``_OWNERS``, lowest layer first: ``subagent_persistence_layout``, ``subagent_persistence_dismissals``, ``subagent_persistence_state`` and ``subagent_persistence_tombstones``. It keeps two of the readers that sweep every
run folder: the panel records (:func:`read_panel_records`) and the stale-tombstone prune. Every name an owner defines
still resolves as ``kiro_crew.subagent_persistence.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it, and the scan finds the seams it is known to have; the owner list and
its layering; that every public name the module held before the split still type-checks
through it; that the owners log as ``kiro_crew.subagent_persistence``; that
``RetentionPromotionResult`` keeps the facade as its module; that a write of
``os`` or of the registry root through the facade reaches the code reading it;
and the frozen import inventory that stands in for the F401 check
``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import logging
import re
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror, subagent_persistence

pytestmark = pytest.mark.xdist_group(name="tree_scan_subagent_persistence_facade")

FACADE = "kiro_crew.subagent_persistence"
OWNERS = (
    "kiro_crew.subagent_persistence_layout",
    "kiro_crew.subagent_persistence_dismissals",
    "kiro_crew.subagent_persistence_state",
    "kiro_crew.subagent_persistence_tombstones",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: the registry root the suite pins, the record write several owners
#: call, a state read the facade's own sweep reads, a constant an owner reads, and a
#: module rebound wholesale. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_SUBAGENTS_DIR",
        "_subagents_dir",
        "_atomic_write",
        "atomic_write",
        "fsync_dir",
        "read_state",
        "_read_tombstone_at",
        "mark_delivered",
        "promote_retention",
        "tighten_run_memory_mode",
        "_OWED_TOMBSTONES_MAX",
        "_PANEL_ID_CAP",
        "_create_exclusive",
        "_cleanup_session_files_sync",
        "_panel_result_text",
        "os",
    }
)

#: A test file worth parsing spells the facade itself, never only a ``subagent_persistence_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.subagent_persistence(?![\w])"
    r"|from\s+kiro_crew\s+import\s+(?:\([^)]*?\bsubagent_persistence\b(?!_)|[^\n]*?\bsubagent_persistence\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Callable Collection Enum ExecutionContext NamedTuple PROVIDER_LABEL_DEFAULT Path Protocol
    _is_safe_path asyncio atomic_write data_home ensure_directory fsync_dir heapq
    is_sensitive_path json kiro_sessions_dir logging os owner_only_opener_for platform_compat re
    read_bytes_with_retry redact_credentials redact_exfiltration_urls rotate_jsonl_at shutil
    tempfile threading time weakref
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its manifest guard.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(subagent_persistence)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [subagent_persistence, *(sys.modules[name] for name in _index().owners)]


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew import subagent_persistence\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(subagent_persistence, 'A', 1)\n",
            {"A"},
        ),
        (
            "import kiro_crew.subagent_persistence as facade\n"
            "from unittest import mock\n"
            "def t():\n    with mock.patch.object(facade, 'B'):\n        pass\n",
            {"B"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.subagent_persistence.C'):\n        pass\n",
            {"C"},
        ),
        (
            "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.subagent_persistence.D', 1)\n",
            {"D"},
        ),
        # An owner is not the facade, even bound under the facade's name.
        (
            "from kiro_crew import subagent_persistence_layout as subagent_persistence\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(subagent_persistence, 'X', 1)\n",
            set(),
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.subagent_persistence_tombstones.Y'):\n        pass\n",
            set(),
        ),
    ],
    ids=[
        "monkeypatch-from-import",
        "patch-object-import-as",
        "mock-patch-string",
        "dotted-setattr",
        "owner-named-like-the-facade",
        "owner-string",
    ],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert patched_names(source) == expected


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach tests over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


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


def test_every_scanned_name_is_routed_to_every_owner_holding_it() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _index()) == []


def test_the_reach_scan_can_fail() -> None:
    """The routing check reads the index: blind one name and the gap shows."""
    index = _index()
    name, holders = next((n, h) for n, h in sorted(index.holders.items()) if h)
    blind = dataclasses.replace(index, holders={**index.holders, name: ()})
    assert _reach_gaps({name}, blind) == [f"{owner}.{name}" for owner in holders]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(subagent_persistence, name) or isinstance(
            getattr(subagent_persistence, name), ModuleType
        ):
            continue  # a shared module (os, time) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(subagent_persistence, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(subagent_persistence, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


def test_a_facade_write_of_the_os_module_reaches_every_reader() -> None:
    """``test_subagent_owed_tombstones.py`` swaps the whole ``os`` binding through the
    facade (``setattr(sp, "os", fake)``) to kill a retry mid-write, and that write now
    lives in ``subagent_persistence_tombstones``: the swap must land there."""
    sentinel = object()
    readers = [m for m in _family() if "os" in vars(m)]
    assert {m.__name__ for m in readers} >= {FACADE, "kiro_crew.subagent_persistence_tombstones"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subagent_persistence, "os", sentinel)
        assert [m.__name__ for m in readers if vars(m)["os"] is not sentinel] == []
    assert all(vars(m)["os"] is not sentinel for m in readers)


def test_a_facade_write_of_the_registry_root_is_the_root_every_owner_resolves(
    tmp_path: Path,
) -> None:
    """The suite pins ``_SUBAGENTS_DIR`` through the facade (``conftest.py``); the
    registry root, every run folder and the facade's own sweep must resolve under it."""
    root = tmp_path / "subagents"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
        assert subagent_persistence._subagents_dir() == root
        assert subagent_persistence._agent_dir("abc123").parent == root.resolve()
        layout = sys.modules["kiro_crew.subagent_persistence_layout"]
        assert layout._SUBAGENTS_DIR == root


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == subagent_persistence._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == subagent_persistence._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_subagent_persistence_sibling() -> None:
    assert subagent_persistence.__file__ is not None
    siblings = sorted(
        f"kiro_crew.{path.stem}"
        for path in Path(subagent_persistence.__file__).parent.glob("subagent_persistence_*.py")
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


def test_each_owner_imports_only_owners_listed_before_it_and_never_the_facade() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    assert _layering_gaps(trees) == {}


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.subagent_persistence_tombstones import x\n")
    trees[OWNERS[1]] = ast.parse("import kiro_crew.subagent_persistence\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[-1]],
        OWNERS[1]: [FACADE],
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
    assert sorted(public - set(vars(subagent_persistence))) == []
    assert not any(name in _index().exports for name in public)


def test_no_two_members_of_the_family_define_the_same_name() -> None:
    """Each moved definition has one home: the facade keeps what it kept, an owner the rest."""
    seen: dict[str, str] = {}
    clashes = []
    for module in _family():
        for name in sorted(_facade_mirror._defined_names(_tree(module))):
            if name in {"__all__"}:
                continue
            if name in seen:
                clashes.append(f"{name}: {seen[name]} and {module.__name__}")
            seen[name] = module.__name__
    assert clashes == []


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
    for node in _tree(subagent_persistence).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in node.body:
                if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
                    for alias in stmt.names:
                        out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """A name an owner imports from a project module and the facade serves without
    binding is named from its home under ``TYPE_CHECKING`` (none today: the facade
    keeps binding every project import it had)."""
    assert _home_imports(), "the owners import nothing from the project -- the scan went blind"
    served = set(_index().exports)
    assert _home_manifest() == {n: h for n, h in _home_imports().items() if n in served}


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(subagent_persistence))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """Callers import the moved names from here, so a type checker must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(subagent_persistence))) == []


# --------------------------------------------------------------------------- #
# The logger
# --------------------------------------------------------------------------- #


def _logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


def test_the_owners_log_under_the_facade_name() -> None:
    """The moved code logs as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``)."""
    names = [n for owner in OWNERS for n in _logger_names(_tree(sys.modules[owner]))]
    assert names == [repr(FACADE)], names
    holders = [m for m in _family() if "logger" in vars(m)]
    assert len(holders) > 2, "too few modules hold the logger -- the scan went blind"
    assert all(vars(m)["logger"] is logging.getLogger(FACADE) for m in holders)


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse("import logging\nlogging.getLogger(__name__).warning('x')\n")
    assert _logger_names(tree) == ["__name__"]


@pytest.mark.parametrize("name", ["RetentionPromotionResult"])
def test_a_moved_class_keeps_the_facade_module(name: str) -> None:
    """The outcome's dotted name, which pickle and introspection resolve, is the import path."""
    value = getattr(subagent_persistence, name)
    homes = [m for m in _family()[1:] if name in vars(m)]
    assert homes and all(vars(m)[name] is value for m in homes)
    assert f"{value.__module__}.{value.__qualname__}" == f"{FACADE}.{name}"
    # What pickle and introspection do with a class reference: resolve that dotted name.
    assert getattr(importlib.import_module(value.__module__), value.__qualname__) is value


@pytest.mark.parametrize(
    ("name", "home"),
    [
        ("publish_live_cleanup_identity", "kiro_crew.subagent_persistence_layout"),
        ("remember_live_cleanup_identity", "kiro_crew.subagent_persistence_layout"),
        ("write_run_agent", "kiro_crew.subagent_persistence_state"),
    ],
)
def test_the_run_coordinators_helpers_keep_the_facade_module(name: str, home: str) -> None:
    """The run coordinator binds these three as ``kiro_crew.subagent_persistence`` helpers,
    and ``test_run_bindings.py`` pins the module each reports: the owner's object, under the
    facade's name."""
    owner = sys.modules[home]
    value = getattr(subagent_persistence, name)
    assert value is vars(owner)[name]
    assert value.__module__ == FACADE
    assert value.__code__.co_filename == owner.__file__


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
    assert unexpected_imports(_source(subagent_persistence), _owner_names()) == []


def test_every_base_import_is_still_bound_on_the_facade() -> None:
    """The header is the BASE header: each name it bound is still in the namespace."""
    assert sorted(_BASE_IMPORTS - set(vars(subagent_persistence))) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    planted = (
        _source(subagent_persistence)
        + "\nimport pickle\nfrom kiro_crew.subagent_persistence_layout import read_panel_records\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "pickle",
        "kiro_crew.subagent_persistence_layout.read_panel_records",
    ]


def test_the_inventory_check_accepts_a_planted_import_the_code_reads() -> None:
    """A new import that code in the module reads is accounted for, not refused."""
    planted = (
        _source(subagent_persistence) + "\nimport pickle\n_PROTOCOL = pickle.HIGHEST_PROTOCOL\n"
    )
    assert unexpected_imports(planted, _owner_names()) == []
