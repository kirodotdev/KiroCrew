"""``KnowledgeStore`` keeps every member it had, each bound from the owner that holds it.

The store's behaviour lives in the ``kiro_crew.knowledge.store_parts`` owners: each
member's body is a module-level function there whose first parameter is ``self``, and
the class in ``kiro_crew.knowledge.store`` binds it by name (``m = _owner.m``, or
``staticmethod(_owner.f)``). The instance state stays on the instance, so tests that
patch a method on the class or on a store, or poke a private field, see the object they
always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind and its signature (the ``self``
  annotation aside) as the one-module class had them, and the module that owns it now;
* each bound member IS its owner's function, and no owner keeps an unbound ``self``
  function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and each imports only owners listed before
  it in ``_STORE_OWNERS``;
* each owner's docstring names the instance fields its members write and read, and an
  owner that logs does so under the ``kiro_crew.knowledge.store`` logger;
* every module-level name the one-module file bound still resolves on the facade, to the
  object its owner holds;
* no name the test trees patch through ``kiro_crew.knowledge.store`` is read as a global
  by an owner: an owner reads its own namespace, so such a patch would not reach it.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import inspect
import re
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from facade_mirror_scan import global_reads, patched_names

from kiro_crew.knowledge import store as store_module
from kiro_crew.knowledge.store import KnowledgeStore

pytestmark = pytest.mark.xdist_group(name="tree_scan_store_parts_bindings")

_FACADE = "kiro_crew.knowledge.store"
_PACKAGE = "kiro_crew.knowledge.store_parts"
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: Member -> (kind, signature digest, owning module). The kind and signature of each
#: member that existed before the split are the one-module class's; the owner is where the
#: member lives (``store`` is ``knowledge/store.py`` itself). A new member adds its row.
_MEMBERS: dict[str, tuple[str, str | None, str]] = {
    "_FTS_REBUILD_BATCH": ("data", None, "fulltext_index"),
    "_INITIAL_SYNC_STATUSES": ("data", None, "store"),
    "_ITEM_COLUMNS": ("data", None, "item_rows"),
    "_SEARCH_INDEXES": ("data", None, "schema_migrations"),
    "_SOURCE_COLUMNS": ("data", None, "source_rows"),
    "_VECTOR_ITEM_COLUMNS": ("data", None, "schema_migrations"),
    "__init__": ("function", "e466847a8ade", "store"),
    "_adopt_agent_sole_copy_residue": ("function", "013e708783e5", "maintenance_sweeps"),
    "_adopt_reassigned_item": ("function", "616360dcb0a9", "item_ownership"),
    "_bundle_blocked_items": ("function", "c6fb873cb13b", "bundle_ownership"),
    "_bundle_group_bounds": ("function", "e847919a7039", "bundle_transfer"),
    "_bundle_membership_ids": ("function", "e847919a7039", "bundle_transfer"),
    "_claimed_item_ids": ("function", "c7985e186c11", "bundle_ownership"),
    "_close_all_for_tests": ("function", "f4ca892d9e9f", "store"),
    "_connect": ("function", "55cc4a2df3bc", "store"),
    "_contested_claims": ("function", "efe932043d33", "bundle_ownership"),
    "_delete_item_cascade": ("function", "c968aad76a4e", "item_rows"),
    "_ensure_search_indexes_if_empty": ("function", "b60c5fea256e", "schema_migrations"),
    "_ensure_vector_generation": ("function", "b60c5fea256e", "schema_migrations"),
    "_fts_index": ("function", "c959edb5238b", "fulltext_index"),
    "_fts_terms": ("function", "6dfaa9bbf8bd", "fulltext_index"),
    "_fts_terms_segmented": ("function", "a6632a57e602", "fulltext_index"),
    "_fts_unindex": ("function", "c959edb5238b", "fulltext_index"),
    "_import_bundle_state": ("function", "fce7c08a08a2", "bundle_ownership"),
    "_init_schema": ("function", "f4ca892d9e9f", "schema_migrations"),
    "_initial_status_or_default": ("staticmethod", "4aaf288cb541", "store"),
    "_initial_sync_status": ("staticmethod", "19088ab13a72", "store"),
    "_items_exist": ("function", "9b88eadaa166", "bundle_ownership"),
    "_load_graph": ("function", "f4ca892d9e9f", "entity_graph"),
    "_migrate": ("function", "f4ca892d9e9f", "schema_migrations"),
    "_migrate_fts_index": ("function", "b60c5fea256e", "fulltext_index"),
    "_prune_orphan_entities": ("function", "b60c5fea256e", "entity_graph"),
    "_retire_one_in_txn": ("function", "d4597982558d", "source_rows"),
    "_sanitize_fts5": ("staticmethod", "8c2eaa320050", "fulltext_index"),
    "_serialize_item": ("staticmethod", "90e88c4040d7", "item_rows"),
    "_state_row_owns_items": ("function", "593e5b9fd8ff", "bundle_ownership"),
    "add_entity": ("function", "fc113ff01d75", "entity_graph"),
    "add_entity_relation": ("function", "dd9f11760b73", "entity_graph"),
    "add_item": ("function", "97f1ab4d6091", "item_rows"),
    "add_mention": ("function", "123c2662e807", "entity_graph"),
    "add_source": ("function", "a571982a4ff8", "source_rows"),
    "add_source_location": ("function", "4cf2da4b899d", "item_ownership"),
    "add_source_location_in_txn": ("function", "4cf2da4b899d", "item_ownership"),
    "agent_owned_item_ids": ("function", "013e708783e5", "item_ownership"),
    "aggregate_stats": ("function", "7b52df734575", "content_stats"),
    "close": ("function", "f4ca892d9e9f", "store"),
    "db": ("property", "55cc4a2df3bc", "store"),
    "delete_item": ("function", "c968aad76a4e", "item_rows"),
    "delete_items_batch": ("function", "403c1f3a03c9", "item_rows"),
    "delete_items_batch_in_txn": ("function", "403c1f3a03c9", "item_rows"),
    "delete_source_cascade": ("function", "99191a176ec8", "item_rows"),
    "detach_source_location_by_hash": ("function", "75820b543a01", "item_ownership"),
    "ensure_fts_index_current": ("function", "b60c5fea256e", "fulltext_index"),
    "ensure_graph_loaded": ("function", "b60c5fea256e", "entity_graph"),
    "ensure_search_indexes": ("function", "b60c5fea256e", "schema_migrations"),
    "export_all": ("function", "07128394bed8", "bundle_transfer"),
    "export_item": ("function", "7e52befa88f7", "bundle_transfer"),
    "find_doc_by_content_hash": ("function", "4ae4f07bd6e4", "item_rows"),
    "find_entity": ("function", "88e313dd364c", "entity_graph"),
    "get_entity_subgraph": ("function", "e16a0a6a1be5", "entity_graph"),
    "get_item": ("function", "c968aad76a4e", "item_rows"),
    "get_neighbors": ("function", "40ac2c67dd9c", "entity_graph"),
    "get_source_by_uri": ("function", "9185e27dbb22", "source_rows"),
    "get_stats": ("function", "44dc9acb7120", "content_stats"),
    "graph": ("property", "758f8289654d", "store"),
    "has_embed_state_index": ("function", "a6632a57e602", "schema_migrations"),
    "has_pending_one_time_work": ("function", "a6632a57e602", "schema_migrations"),
    "import_bundle": ("function", "a799faeee9da", "bundle_transfer"),
    "ingestion_in_flight": ("function", "3ef35315efe6", "store"),
    "maintenance_window": ("function", "0c950929ae8f", "store"),
    "merge_entities": ("function", "66dda62cb15a", "entity_graph"),
    "merge_source_properties": ("function", "4c45d701c5d1", "source_rows"),
    "open_read_only": ("classmethod", "ea0961fa9b58", "store"),
    "open_recovering": ("classmethod", "ea0961fa9b58", "store"),
    "reassign_item_source": ("function", "616360dcb0a9", "item_ownership"),
    "reclaim_agent_source_residue": ("function", "fa6cfecd5574", "maintenance_sweeps"),
    "reclaim_orphans": ("function", "b60c5fea256e", "maintenance_sweeps"),
    "release_stale_claim": ("function", "8b847ac7816f", "item_ownership"),
    "reload_graph": ("function", "b60c5fea256e", "entity_graph"),
    "retire_auto_registered_folder": ("function", "5e5e18959e63", "source_rows"),
    "revise_source_properties": ("function", "86af4c627e5b", "source_rows"),
    "search_items_fts": ("function", "b580228dc456", "fulltext_index"),
    "source_count": ("function", "fa6cfecd5574", "content_stats"),
    "sources_holding_item": ("function", "38e1d5ac5668", "item_ownership"),
    "surviving_group_in_txn": ("function", "90373a793d37", "item_ownership"),
    "update_item": ("function", "f074042316f2", "item_rows"),
    "update_source": ("function", "dfb808fc1ab1", "source_rows"),
    "vector_generation": ("function", "8dc15be1dfda", "schema_migrations"),
}

#: Module-level names the one-module ``store.py`` bound that the facade does not bind:
#: standard-library and typing imports only moved code reads. Every other name it bound
#: still resolves on the facade.
_DROPPED_IMPORTS = frozenset(
    {"Any", "Callable", "Counter", "base64", "dataclass", "datetime", "defaultdict", "uuid4"}
)

#: Every module-level name the one-module ``store.py`` bound.
_BASE_MODULE_NAMES = frozenset("""
    AUTO_ADDED_PROP AUTO_REGISTRATION_RETIRED_PROP Any BUNDLE_STATE_KEY_COL Callable
    ContentStats Counter FTS_INDEX_VERSION IngestionGate Iterator KnowledgeBundleError
    KnowledgeStore MAINTENANCE_WAIT_SECS OnLoopDBGuard Path STORE_STRICT_ENV
    SidecarNotMovable SimpleDiGraph SourceContentStats _ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS
    _BUNDLE_STATE_CARRIED_COLS _BUNDLE_STATE_REQUIRED_COLS _BUNDLE_STATE_RESTORED_TABLES
    _DOC_STATE_KEY_COL _DOC_STATE_TABLES _EdgeView _MAX_ITEM_GROUP_MEMBERS
    _MAX_ITEM_ID_CHARS _MAX_SQL_PARAMS _NodeView _ON_LOOP_DB_GUARD _OWNERSHIP_HASH_COL
    _RECLAIM_CHUNK _ThreadAffineTestConnection _WALKING_SOURCE_TYPES _bundle_item_group
    _bundle_state_restores _bundle_state_text _sql_param_chunks _validated_aliases
    _validated_embedding_sig _validated_json_column _validated_properties
    _without_sync_status annotations base64 contextmanager dataclass datetime defaultdict
    fts5_cjk_match_groups fts5_segment_for_index is_auto_registered
    is_damaged_database_error json logger logging prepare_owner_only_sqlite
    quarantine_sqlite_file sqlite3 threading time uuid4
    """.split())


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in store_module._STORE_OWNERS]


def _short(module_name: str) -> str:
    return "store" if module_name == _FACADE else module_name.rpartition(".")[2]


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _kind(value: object) -> str:
    if isinstance(value, staticmethod):
        return "staticmethod"
    if isinstance(value, classmethod):
        return "classmethod"
    if isinstance(value, property):
        return "property"
    if inspect.iscoroutinefunction(value):
        return "coroutine"
    if inspect.isfunction(value):
        return "function"
    return "data"


def _function_of(value: object) -> Any:
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _signature_digest(value: object) -> str | None:
    """The member's signature with the ``self`` annotation removed, hashed."""
    fn = _function_of(value)
    if not callable(fn):
        return None
    signature = inspect.signature(fn)
    params = [
        p.replace(annotation=inspect.Parameter.empty) if p.name == "self" else p
        for p in signature.parameters.values()
    ]
    text = str(signature.replace(parameters=params))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _members() -> dict[str, object]:
    skip = {"__module__", "__qualname__", "__dict__", "__weakref__", "__doc__"}
    skip |= {"__firstlineno__", "__static_attributes__"}
    return {name: value for name, value in vars(KnowledgeStore).items() if name not in skip}


# --------------------------------------------------------------------------- #
# The member inventory and the binding.
# --------------------------------------------------------------------------- #


def test_the_kind_reader_tells_a_coroutine_from_a_function() -> None:
    async def stand_in(self: object) -> None:
        return None

    assert (_kind(stand_in), _kind(lambda self: None)) == ("coroutine", "function")


def test_the_member_set_is_the_frozen_inventory() -> None:
    assert sorted(_members()) == sorted(_MEMBERS)


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_owner(name: str) -> None:
    kind, digest, owner = _MEMBERS[name]
    value = _members()[name]
    assert _kind(value) == kind
    assert _signature_digest(value) == digest
    if owner == "store":
        fn = _function_of(value)
        if callable(fn):
            assert fn.__module__ == _FACADE
        return
    module = importlib.import_module(f"{_PACKAGE}.{owner}")
    # The class attribute IS the owner's object: a function, or a class constant.
    assert _function_of(value) is vars(module)[name]


def test_every_owner_is_listed_and_holds_a_member() -> None:
    listed = {_short(name) for name in store_module._STORE_OWNERS}
    owning = {owner for _kind, _digest, owner in _MEMBERS.values()} - {"store"}
    assert listed == owning
    parts = Path(store_module.__file__).parent / "store_parts"
    files = {p.stem for p in parts.glob("*.py")} - {"__init__"}
    assert files == listed


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The top-level functions of *tree* whose first parameter is ``self``."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "self":
                found.append(node)
    return found


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = importlib.import_module(owner)
    members = _members()
    unbound = [
        node.name
        for node in _self_functions(_tree(module))
        if _function_of(members.get(node.name)) is not vars(module)[node.name]
    ]
    assert unbound == []


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What in *fn* only works inside a class body."""
    found = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            found.append(f"{fn.name}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append(f"{fn.name}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            found.append(f"{fn.name}: {node.attr}")
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().g()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).g()\n", False),
        ("def f(self):\n    return self.__dict__, self._private\n", False),
    ],
)
def test_the_class_only_scan_sees_each_shape(source: str, flagged: bool) -> None:
    (fn,) = _self_functions(ast.parse(source))
    assert bool(_class_only_constructs(fn)) is flagged


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(importlib.import_module(owner))
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert [c for fn in functions for c in _class_only_constructs(fn)] == []


# --------------------------------------------------------------------------- #
# Owners never import the facade, and import each other in one direction.
# --------------------------------------------------------------------------- #


def _runtime_imports(tree: ast.Module, package: str = _PACKAGE) -> set[str]:
    """Dotted modules *tree* imports outside ``if TYPE_CHECKING:``, a relative import
    resolved against *package* (the owners' package)."""
    typing_only: set[int] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "TYPE_CHECKING"
        ):
            typing_only.update(id(n) for n in ast.walk(node))
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                base = f"{parent}.{base}" if base else parent
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_runtime_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew.knowledge.store_parts import entity_graph\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.knowledge.store import KnowledgeStore\n"
        "from .item_rows import add_item\n"
        "def f():\n"
        "    import kiro_crew.knowledge.store\n"
        "def g():\n"
        "    from ..store import KnowledgeStore\n"
    )
    found = _runtime_imports(tree)
    assert f"{_PACKAGE}.entity_graph" in found
    assert f"{_PACKAGE}.item_rows" in found  # a relative import, resolved
    assert _FACADE in found  # the function-local imports are run-time imports
    assert "kiro_crew.knowledge.store.KnowledgeStore" in found  # from g's relative import
    typing_only = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.knowledge.store import KnowledgeStore\n"
    )
    assert _FACADE not in _runtime_imports(typing_only)


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    found = _runtime_imports(_tree(importlib.import_module(owner)))
    assert sorted(name for name in found if name == _FACADE or name.startswith(_FACADE + ".")) == []


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    owners = store_module._STORE_OWNERS
    later = {
        owner: sorted(
            name
            for name in _runtime_imports(_tree(sys.modules[owner]))
            if name in owners and name not in owners[:i]
        )
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


def _owner_importers(src: Path, owners: frozenset[str]) -> list[str]:
    """Files under *src* that import one of *owners* at all, the owners and facade aside."""
    found = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "store_parts" not in text:
            continue
        module = ".".join(path.relative_to(src).with_suffix("").parts)
        if module in owners or module == _FACADE:
            continue
        tree = ast.parse(text)
        package = module.rpartition(".")[0]
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    base = f"{parent}.{base}" if base else parent
                names = {base, *(f"{base}.{alias.name}" for alias in node.names)}
            elif isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            if names & owners:
                found.append(path.relative_to(src).as_posix())
                break
    return found


def test_the_owner_importer_scan_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "knowledge"
    pkg.mkdir(parents=True)
    (pkg / "other.py").write_text(
        "from .store_parts.entity_graph import SimpleDiGraph\n", encoding="utf-8"
    )
    (pkg / "fine.py").write_text("from kiro_crew.knowledge.store import X\n", encoding="utf-8")
    owners = frozenset({"kiro_crew.knowledge.store_parts.entity_graph"})
    assert _owner_importers(tmp_path, owners) == ["kiro_crew/knowledge/other.py"]


def test_nothing_else_in_src_imports_an_owner() -> None:
    """``kiro_crew.knowledge.store`` is the one import path of the store."""
    src = Path(store_module.__file__).resolve().parents[2]
    assert _owner_importers(src, frozenset(store_module._STORE_OWNERS)) == []


# --------------------------------------------------------------------------- #
# Owner docstrings name their fields; owners log under the store's logger.
# --------------------------------------------------------------------------- #

#: The fields ``KnowledgeStore.__init__`` sets: the instance state the owners share.
_FIELDS = frozenset("""
    _connections _connections_lock _db_path _fts_index_current _fts_lock _fts_segmented
    _generation _graph _graph_loaded _graph_lock _ingestion_gate _read_only _thread_local
    warnings
    """.split())


def _fields(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(fields written, fields only read) through ``self.<field>`` in *tree*'s methods."""
    writes: set[str] = set()
    reads: set[str] = set()
    for fn in _self_functions(tree):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in _FIELDS
            ):
                (writes if isinstance(node.ctx, ast.Store) else reads).add(node.attr)
    return writes, reads - writes


def test_the_field_scan_sees_writes_and_reads() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self._graph = 1\n"
        "    with self._graph_lock:\n"
        "        return self._graph, self.db, self._other\n"
    )
    assert _fields(tree) == ({"_graph"}, {"_graph_lock"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\s|$)", " ".join(doc.split()))
    assert match, f"no '{label}:' line"
    return set(re.findall(r"``(\w+)``", match.group(1)))


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = importlib.import_module(owner)
    writes, reads = _fields(_tree(module))
    doc = module.__doc__ or ""
    assert (_listed(doc, "Writes"), _listed(doc, "Reads")) == (writes, reads)


def _foreign_loggers(tree: ast.Module) -> list[str]:
    """Each ``getLogger(...)`` call in *tree* whose argument is not the store's name."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "getLogger"
        ):
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Constant) and arg.value == _FACADE):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse(
        "import logging\n"
        "logger = logging.getLogger('kiro_crew.knowledge.store')\n"
        "_log = logging.getLogger(__name__)\n"
        "from logging import getLogger\n"
        "other = getLogger('kiro_crew.knowledge.store_parts.x')\n"
    )
    assert [line.split(":")[0] for line in _foreign_loggers(tree)] == ["3", "5"]


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_an_owner_that_logs_uses_the_store_logger(owner: str) -> None:
    module = importlib.import_module(owner)
    assert _foreign_loggers(_tree(module)) == []
    if "logger" in vars(module):
        assert module.logger is store_module.logger
    assert store_module.logger.name == _FACADE


#: Every class an owner defines -> the ``__module__`` it carries. An exception keeps the
#: facade's name, because a traceback renders a raised class as ``module.qualname``; the
#: graph and the stats result types name their owner, since nothing renders, pickles or
#: keys on their module path.
_CLASS_MODULES = {
    "KnowledgeBundleError": _FACADE,
    "SimpleDiGraph": f"{_PACKAGE}.entity_graph",
    "_NodeView": f"{_PACKAGE}.entity_graph",
    "_EdgeView": f"{_PACKAGE}.entity_graph",
    "SourceContentStats": f"{_PACKAGE}.content_stats",
    "ContentStats": f"{_PACKAGE}.content_stats",
}


def test_each_class_an_owner_defines_carries_its_frozen_module() -> None:
    found = {
        node.name: vars(module)[node.name].__module__
        for module in _owners()
        for node in _tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert found == _CLASS_MODULES


def test_a_bundle_rejection_renders_under_the_facade_path() -> None:
    text = "".join(traceback.format_exception_only(store_module.KnowledgeBundleError("bad")))
    assert text == "kiro_crew.knowledge.store.KnowledgeBundleError: bad\n"


# --------------------------------------------------------------------------- #
# The facade keeps every name the one-module file bound.
# --------------------------------------------------------------------------- #


def test_every_base_module_name_still_resolves_on_the_facade() -> None:
    missing = sorted(
        name
        for name in _BASE_MODULE_NAMES - _DROPPED_IMPORTS
        if name != "annotations" and not hasattr(store_module, name)
    )
    assert missing == []
    assert _DROPPED_IMPORTS <= _BASE_MODULE_NAMES


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_a_module_name_an_owner_binds_is_the_same_object_on_the_facade(owner: str) -> None:
    """A moved definition, or an import both bind, is one object in both namespaces."""
    module = importlib.import_module(owner)
    shared = sorted(_BASE_MODULE_NAMES & set(vars(module)) - _DROPPED_IMPORTS)
    assert shared
    assert [name for name in shared if getattr(store_module, name) is not vars(module)[name]] == []


# --------------------------------------------------------------------------- #
# No name a test patches through the facade is read by an owner.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/`` and the builtin apps' test modules and conftests."""
    files = [_REPO_ROOT / "conftest.py", *sorted(_TEST_ROOT.rglob("*.py"))]
    builtins_root = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins"
    for path in sorted(builtins_root.rglob("*.py")):
        rel = path.relative_to(builtins_root).as_posix()
        if "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py":
            files.append(path)
    return files


def _facade_spellings(src: Path) -> dict[str, str]:
    """The facade's own name, and every src module's module-level binding of it
    (``kiro_crew.cli.knowledge_store``): a patch through either reaches the facade."""
    spellings = {_FACADE: _FACADE}
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "knowledge" not in text or "store" not in text:
            continue
        module = ".".join(path.relative_to(src).with_suffix("").parts)
        for node in ast.parse(text).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if f"{node.module}.{alias.name}" == _FACADE and module != _FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = _FACADE
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _FACADE and alias.asname:
                        spellings[f"{module}.{alias.asname}"] = _FACADE
    return spellings


def _patched_through_the_facade(files: list[Path], spellings: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Every spelling of the facade, and every alias a test binds to one, says "knowledge".
        if "knowledge" not in text:
            continue
        try:
            found = patched_names(text, spellings)
        except SyntaxError:
            continue
        names |= found.get(_FACADE, set())
    return names


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through any spelling of the facade, read once."""
    spellings = _facade_spellings(_REPO_ROOT / "src")
    return frozenset(_patched_through_the_facade(_test_tree_files(), spellings))


def _owner_reads() -> dict[str, set[str]]:
    return {
        owner.__name__: global_reads(
            Path(owner.__file__).read_text(encoding="utf-8"), owner.__file__
        )
        for owner in _owners()
    }


def _reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> list[str]:
    return sorted(f"{owner}.{name}" for owner, names in reads.items() for name in patched & names)


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    files = _test_tree_files()
    assert Path(__file__).resolve() in files
    assert _REPO_ROOT / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


def test_the_spellings_include_each_src_binding_of_the_facade() -> None:
    spellings = _facade_spellings(_REPO_ROOT / "src")
    assert {
        "kiro_crew.cli.knowledge_store",
        "kiro_crew.mcp_tools.knowledge.knowledge_store",
    } <= set(spellings)
    assert set(spellings.values()) == {_FACADE}


def test_the_patch_scan_sees_the_facade_patches_it_must() -> None:
    patched = _corpus_patched_names()
    # conftest's session flag and the on-loop guard recorder, both read by kept code.
    assert {"_ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS", "_ON_LOOP_DB_GUARD"} <= patched


def test_no_name_patched_through_the_facade_is_read_by_an_owner() -> None:
    patched = set(_corpus_patched_names())
    assert _reach_gaps(patched, _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.knowledge import store as store_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(store_mod, '_RECLAIM_CHUNK', 1)\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch('kiro_crew.cli.knowledge_store._MAX_SQL_PARAMS', 1):\n"
        "        pass\n",
        encoding="utf-8",
    )
    spellings = {_FACADE: _FACADE, "kiro_crew.cli.knowledge_store": _FACADE}
    patched = _patched_through_the_facade([probe], spellings)
    assert patched == {"_RECLAIM_CHUNK", "_MAX_SQL_PARAMS"}
    assert _reach_gaps(patched, _owner_reads()) == [
        f"{_PACKAGE}.bundle_ownership._MAX_SQL_PARAMS",
        f"{_PACKAGE}.maintenance_sweeps._RECLAIM_CHUNK",
    ]


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
