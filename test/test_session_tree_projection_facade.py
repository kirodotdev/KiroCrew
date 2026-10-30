"""``kiro_crew.crew_log.session_tree_projection`` stays the one import path and patch surface.

``SessionTreeProjection``'s checkpoint and cold start live in two owner modules beside it,
``session_tree_projection_checkpoint`` and ``session_tree_projection_seed``. An owner
function whose first parameter is ``self`` is bound as the class attribute of the same
name, and every other moved name is served from its owner through
:mod:`kiro_crew._facade_mirror`. ``test/test_facade_mirror_guards.py`` holds every mirrored
facade to the shared rules; these tests pin what is particular to this one:

* the owners are exactly the ``session_tree_projection_*`` siblings, layered lowest first,
  and nothing outside the family imports one;
* every module-level name the unsplit file bound still resolves through the facade, and
  the facade serves only names its owners define, so every name it imports from the rest
  of ``kiro_crew`` stays bound by that import and visible to type checkers;
* every name the test trees patch through the facade reaches every module holding it;
* every owner logs under the facade's logger name;
* the class keeps every member it had, each bound one is its owner's own function with its
  base signature, and no bound function needs the class body.

Each structural check that could pass over nothing has a self-test that shows it can fail.
The layout is described in ``docs/system-specs/modules/crew-log-projection.md`` (section 6).
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.crew_log import session_tree_projection as facade
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Walks the test trees for patches and src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_session_tree_projection_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

FACADE = "kiro_crew.crew_log.session_tree_projection"
OWNER_PREFIX = "kiro_crew.crew_log.session_tree_projection_"
CLS = facade.SessionTreeProjection
_SRC = Path(kiro_crew.__file__).resolve().parent
_CREW_LOG = Path(facade.__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()

#: Owner modules, lowest layer first: an owner imports only owners listed before it.
OWNERS = (
    "kiro_crew.crew_log.session_tree_projection_checkpoint",
    "kiro_crew.crew_log.session_tree_projection_seed",
)

#: ``SessionTreeProjection`` members an owner defines, by owner, each bound on the class.
MOVED_MEMBERS = {
    "kiro_crew.crew_log.session_tree_projection_checkpoint": (
        "_schedule_checkpoint",
        "cancel_pending_checkpoint",
        "_debounced_write",
        "flush_checkpoint",
    ),
    "kiro_crew.crew_log.session_tree_projection_seed": (
        "ensure_seeded",
        "_retry_due_locked",
        "_install_seed_locked",
        "_install_seed_edges_locked",
        "_install_seed_spawned_locked",
        "_seed",
        "_seed_inner",
        "_replay_tail",
        "_replay_spawned",
        "_arm_seed_retry",
        "_start_seed_retry_timer",
    ),
}

#: Every member the class had at the base, with its kind.
BASE_MEMBER_KINDS = {
    "__init__": "function",
    "_fold_locked": "function",
    "nodes": "function",
    "reading": "function",
    "over_cap": "property",
    "seeded": "property",
    "seeded_for_current_store": "property",
    "seed_retry_due": "property",
    "trusted_creator": "function",
    "spawned_creator": "function",
    "apply": "function",
    "apply_edge": "function",
    "apply_spawned": "function",
    "_evict_spawned_to_cap_locked": "function",
    "_replace_unit_spawned_locked": "function",
    "_evict_edges_to_cap_locked": "function",
    "_evict_to_cap_locked": "function",
    "retract_parent": "function",
    "reconcile_edge": "function",
    "_reconcile_spawned": "function",
    "forget": "function",
    "ensure_seeded": "function",
    "_retry_due_locked": "function",
    "_install_seed_locked": "function",
    "_install_seed_edges_locked": "function",
    "_install_seed_spawned_locked": "function",
    "_seed": "function",
    "_seed_inner": "function",
    "_replay_tail": "function",
    "_replay_spawned": "function",
    "_changed": "function",
    "_announce": "function",
    "_schedule_checkpoint": "function",
    "_arm_seed_retry": "function",
    "_start_seed_retry_timer": "function",
    "cancel_pending_checkpoint": "function",
    "_debounced_write": "function",
    "flush_checkpoint": "function",
}

#: The base signature of each bound member, as ``inspect`` renders it. The owner annotates
#: ``self`` for the type checker; that one annotation is all the comparison removes.
BASE_BOUND_SIGNATURES = {
    "_schedule_checkpoint": "(self) -> 'None'",
    "cancel_pending_checkpoint": "(self) -> 'None'",
    "_debounced_write": (
        "(self, target: \"'Path | None'\" = None, epoch: 'int' = -1, "
        "root: \"'str | None'\" = None) -> 'None'"
    ),
    "flush_checkpoint": "(self) -> 'bool'",
    "ensure_seeded": "(self, live_sids: \"'tuple[str, ...]'\" = ()) -> 'None'",
    "_retry_due_locked": "(self) -> 'bool'",
    "_install_seed_locked": "(self, scanned: \"'dict[str, OpenedRecord]'\") -> 'None'",
    "_install_seed_edges_locked": (
        "(self, scanned: \"'dict[tuple[str, str], EdgeRecord]'\") -> 'None'"
    ),
    "_install_seed_spawned_locked": (
        "(self, scanned: \"'dict[tuple[str, str], EdgeRecord]'\") -> 'None'"
    ),
    "_seed": "(self, live_sids: \"'tuple[str, ...]'\") -> 'None'",
    "_seed_inner": "(self, live_sids: \"'tuple[str, ...]'\") -> 'None'",
    "_replay_tail": (
        "(self, loaded: 'dict[str, OpenedRecord]', loaded_edges: 'dict[tuple[str, str], "
        "EdgeRecord]', loaded_scans: \"'dict[str, ScanIdentity] | None'\" = None, *, faults: "
        "\"'_ScanFaults | None'\" = None) -> \"'_ReplayResult'\""
    ),
    "_replay_spawned": (
        "(self, records: 'dict[str, OpenedRecord]', loaded_spawned: 'dict[tuple[str, str], "
        "EdgeRecord]', loaded_scans: \"'dict[str, ScanIdentity]'\", *, faults: \"'_ScanFaults | "
        "None'\" = None) -> \"'tuple[dict[tuple[str, str], EdgeRecord], dict[str, "
        "ScanIdentity], bool]'\""
    ),
    "_arm_seed_retry": "(self, root: \"'str | None'\", epoch: 'int') -> 'None'",
    "_start_seed_retry_timer": "(self, delay: 'float', fire: \"'Any'\") -> 'None'",
}

#: Every module-level name ``session_tree_projection.py`` bound at the base, less the
#: modules it bound by ``import <module>`` and the ``__future__`` feature.
BASE_MODULE_NAMES = frozenset("""
    Any CHECKPOINT_DEBOUNCE_SECS CHECKPOINT_DIR CHECKPOINT_NAME CHECKPOINT_VERSION EdgeRecord
    Final KIND_SESSION MAX_ACP_SESSION_ID_LEN MAX_CHECKPOINT_BYTES MAX_SHORT_STRING
    OpenedRecord Optional Path SEED_RETRY_COOLDOWN_SECS ScanIdentity SessionTreeProjection
    TREE_UNIT_CAP TreeNode TreeReading _LIVE_PROJECTIONS _LoadedCheckpoint _ReplayResult
    _ScanFaults _checkpoint_path _current_root _edge_from_json _edge_to_json
    _edge_within_bounds _load_checkpoint _projection _projection_lock _record_from_json
    _record_to_json _save_checkpoint _scans_from_json _store_name _within_bounds
    edge_supersedes fold_tree forget_unit inherited_parent log_exception_text log_rank_of
    logger projection reconcile_unit_edge record_adopted record_opened record_released
    record_spawned replace reset_for_tests retract_unit_parent spawned_creator
    """.split())

#: Names the test trees are known to patch through the facade, one from each kind of seam
#: it has: a helper the owner's code reads (``_save_checkpoint``), the store identity
#: every member reads (``_current_root``), an owner's constant (``CHECKPOINT_DEBOUNCE_SECS``,
#: ``SEED_RETRY_COOLDOWN_SECS``), a name the facade only imports (``TREE_UNIT_CAP``), the
#: class, and the doors callers import from the facade. The scan must find every one, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "CHECKPOINT_DEBOUNCE_SECS",
        "SEED_RETRY_COOLDOWN_SECS",
        "SessionTreeProjection",
        "TREE_UNIT_CAP",
        "_current_root",
        "_save_checkpoint",
        "forget_unit",
        "projection",
        "reconcile_unit_edge",
        "record_opened",
        "retract_unit_parent",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _owner(name: str) -> ModuleType:
    return importlib.import_module(name)


def _source(module_name: str) -> str:
    path = _owner(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def _family() -> list[ModuleType]:
    return [facade, *(sys.modules[name] for name in _index().owners)]


# --------------------------------------------------------------------------- #
# The owners
# --------------------------------------------------------------------------- #


def test_the_facade_serves_a_mirror_over_every_sibling_owner_in_layer_order() -> None:
    assert isinstance(facade, _facade_mirror.MirroredModule)
    index = _index()
    on_disk = sorted(
        f"kiro_crew.crew_log.{path.stem}" for path in _CREW_LOG.glob("session_tree_projection_*.py")
    )
    assert sorted(OWNERS) == on_disk
    assert tuple(index.owners) == OWNERS == facade._OWNERS
    assert index.excluded == ()
    assert index.divergent() == ()


def _runtime_imports(source: str) -> set[str]:
    """Modules *source* imports when it loads; ``if TYPE_CHECKING:`` blocks excluded."""
    loaded: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            loaded |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            loaded.add(node.module)
            loaded |= {f"{node.module}.{alias.name}" for alias in node.names}
    return loaded


def test_an_owner_imports_only_owners_below_it_and_never_its_facade() -> None:
    for position, owner in enumerate(OWNERS):
        loaded = _runtime_imports(_source(owner))
        assert FACADE not in loaded, owner
        assert {name for name in loaded if name in OWNERS} <= set(OWNERS[:position]), owner


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
    """Callers read the projection through the facade; an owner is no import path."""
    family = {f"crew_log/{name.rpartition('.')[2]}.py" for name in (FACADE, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_all=("session_tree_projection_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "crew_log/session_tree_projection_seed.py" in importers  # it imports the checkpoint
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root
        / "pkg"
        / "caller.py": (
            "from kiro_crew.crew_log.session_tree_projection_seed import ensure_seeded\n"
        ),
        root / "other.py": "from kiro_crew.crew_log import session_tree_projection_checkpoint\n",
        root / "crew_log" / "relative.py": "from .session_tree_projection_seed import x\n",
        root / "crew_log" / "deep" / "up.py": "from .. import session_tree_projection_seed\n",
        root
        / "facade_user.py": "from kiro_crew.crew_log.session_tree_projection import projection\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "crew_log/relative.py",
        "crew_log/deep/up.py",
    }


def test_every_owner_logs_under_the_facades_name() -> None:
    """``caplog`` and log filters key on the module's logger name, and the moved code
    keeps logging under it."""
    shared = logging.getLogger(FACADE)
    assert facade.logger is shared
    for owner in OWNERS:
        assert vars(_owner(owner)).get("logger") is shared, owner


def test_the_facade_installs_its_mirror_when_run_as_a_script(tmp_path: Path) -> None:
    """Under ``python -m`` the facade is ``__main__``: one logger by literal name keeps
    the family to one object per name, which the mirror's install requires."""
    result = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# The module surface
# --------------------------------------------------------------------------- #


def test_every_base_module_name_resolves_through_the_facade_to_each_holders_object() -> None:
    index = _index()
    assert sorted(name for name in BASE_MODULE_NAMES if not hasattr(facade, name)) == []
    for name in sorted(BASE_MODULE_NAMES):
        value = getattr(facade, name)
        holders = [_owner(holder) for holder in index.holders.get(name, ())]
        assert all(vars(holder)[name] is value for holder in holders), name


def _defined(owner: str) -> set[str]:
    return set(_facade_mirror._defined_names(ast.parse(_source(owner))))


def test_the_facade_serves_only_owner_defined_names_its_own_code_does_not_bind() -> None:
    """The facade binds every name its kept code reads and keeps every base import, so a
    name it serves is one an owner defines, and nothing from outside the family is
    served: ``from kiro_crew.crew_log.session_tree_projection import TREE_UNIT_CAP`` still
    binds the import the facade itself holds, which is what type checkers read."""
    index = _index()
    defined = set().union(*(_defined(owner) for owner in OWNERS))
    served = set(index.exports)
    assert {"_load_checkpoint", "_save_checkpoint", "SEED_RETRY_COOLDOWN_SECS"} <= served
    assert sorted(served - defined) == []
    assert sorted(name for name in served if name in vars(facade)) == []
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_no_bound_method_is_served_as_a_module_name() -> None:
    """The methods the class binds were never module names, so the facade does not list or
    serve them."""
    bound = {name for names in MOVED_MEMBERS.values() for name in names}
    assert sorted(bound & set(_index().exports)) == []
    assert sorted(name for name in bound if name in dir(facade)) == []


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
        if "session_tree_projection" not in text:
            continue
        try:
            found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
        except SyntaxError:
            continue
    return frozenset(found)


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_session_tree_push.py" in walked


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``session_tree_projection.<name>`` was
    what every reader of that name read. Each module of the family that binds a patched
    name must see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(facade, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        before = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(facade, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(facade, name) is not sentinel:
                stale.append(FACADE)
        restored = {module.__name__: vars(module).get(name) for module in holders}
        if stale or restored != before:
            missed[name] = stale or ["undo"]
    assert missed == {}
    assert _index().divergent() == ()


def test_a_patched_owner_name_reaches_the_bound_method_that_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound method reads its owner's globals; a patch through the facade lands there."""
    debounced = vars(CLS)["_debounced_write"]
    assert debounced.__globals__ is vars(_owner(OWNERS[0]))
    monkeypatch.setattr(facade, "CHECKPOINT_DEBOUNCE_SECS", 0.25)
    assert debounced.__globals__["CHECKPOINT_DEBOUNCE_SECS"] == 0.25
    seeded = vars(CLS)["ensure_seeded"]
    monkeypatch.setattr(facade, "_current_root", lambda: "planted")
    assert seeded.__globals__["_current_root"]() == "planted"
    assert facade._current_root() == "planted"


# --------------------------------------------------------------------------- #
# The class surface
# --------------------------------------------------------------------------- #


def _kind(member: Any) -> str:
    if isinstance(member, property):
        return "property"
    if isinstance(member, staticmethod):
        return "staticmethod"
    if inspect.isfunction(member):
        return "coroutine" if inspect.iscoroutinefunction(member) else "function"
    return "attribute"


def test_the_class_keeps_every_base_member_with_its_kind() -> None:
    members = vars(CLS)
    assert sorted(set(BASE_MEMBER_KINDS) - set(members)) == []
    assert {name: _kind(members[name]) for name in BASE_MEMBER_KINDS} == BASE_MEMBER_KINDS


def test_a_bound_member_is_its_owners_function_with_its_base_signature() -> None:
    members = vars(CLS)
    bound = {name for names in MOVED_MEMBERS.values() for name in names}
    assert bound == set(BASE_BOUND_SIGNATURES)
    for owner, names in MOVED_MEMBERS.items():
        module = _owner(owner)
        for name in names:
            member = members[name]
            assert member is vars(module)[name], f"{owner}.{name}"
            assert member.__module__ == owner, name
            assert member.__qualname__ == name, name
            signature = str(inspect.signature(member)).replace(f"self: '{CLS.__name__}'", "self", 1)
            assert signature == BASE_BOUND_SIGNATURES[name], name


def test_every_owner_function_of_self_is_bound_on_the_class() -> None:
    bound = {name for names in MOVED_MEMBERS.values() for name in names}
    for owner in OWNERS:
        module = _owner(owner)
        for name, value in vars(module).items():
            if not inspect.isfunction(value) or value.__module__ != owner:
                continue
            if list(inspect.signature(value).parameters)[:1] == ["self"]:
                assert name in bound and vars(CLS)[name] is value, f"{owner}.{name}"


def _class_body_reads(source: str) -> list[str]:
    """What only a class body provides: zero-argument ``super()``, ``__class__``, mangling."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            hits.append(f"{node.lineno}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            hits.append(f"{node.lineno}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            hits.append(f"{node.lineno}: {node.attr}")
    return sorted(hits)


def test_no_bound_function_needs_the_class_body() -> None:
    """A function bound into the class from an owner was compiled outside it: it has no
    ``__class__`` cell, so a zero-argument ``super()`` or a ``__class__`` read raises, and
    a ``self.__name`` is never mangled. So no owner spells one; a method that needs one
    stays in the class body."""
    assert {owner: hits for owner in OWNERS if (hits := _class_body_reads(_source(owner)))} == {}


def test_the_class_body_scan_can_fail() -> None:
    planted = "def f(self):\n    super().g()\n    return __class__, self.__secret, self.__dict__\n"
    assert _class_body_reads(planted) == ["2: super()", "3: __class__", "3: __secret"]
