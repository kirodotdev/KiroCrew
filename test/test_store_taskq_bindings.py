"""``TaskStore`` keeps every member it had, each bound from the owner that holds it.

The store's behaviour lives in the ``kiro_crew.taskq.store_*`` owners: each member's body
is a module-level function there whose first parameter is ``self``, and the class in
``kiro_crew.taskq.store`` binds it by name (``m = _owner.m``, or
``staticmethod(_owner.f)``). The instance state stays on the instance, so tests that patch
a method on the class or on a store, subclass it, or poke a private field, see the object
they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind and its signature (the ``self``
  annotation aside) as the one-module class had them, and the module that owns it now;
* each bound member IS its owner's function, no owner keeps an unbound ``self`` function,
  and a subclass's ``super()`` call reaches the owner's function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body, and none calls a sibling member by its
  bare name, which would step past a patch or an override of that member;
* no owner imports the facade at run time, each imports only modules listed before it
  in ``_STORE_OWNERS``, and nothing else in ``src/`` imports an owner;
* each owner's docstring names the instance fields its members write and read, and an
  owner that logs does so under the ``kiro_crew.taskq.store`` logger;
* ``TaskStoreUnavailable`` renders under the facade path, and every module-level name the
  one-module file bound still resolves on the facade, to the object its owner holds;
* no name the test trees patch through ``kiro_crew.taskq.store`` is read as a global by an
  owner: an owner reads its own namespace, so such a patch would not reach it.

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
import source_corpus
from facade_mirror_scan import global_reads, patched_names

from kiro_crew.taskq import store as store_module
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable

pytestmark = pytest.mark.xdist_group(name="tree_scan_store_taskq_bindings")

_FACADE = "kiro_crew.taskq.store"
_PACKAGE = "kiro_crew.taskq"
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: Member -> (kind, signature digest, owning module). The kind and signature of each
#: member are the one-module class's; the owner is where the member lives (``store`` is
#: ``taskq/store.py`` itself). A new member adds its row.
_MEMBERS: dict[str, tuple[str, str | None, str]] = {
    "__init__": ("function", "7a27e7759947", "store"),
    "_admitted_exclusion": ("staticmethod", "3b27d7a1bfc4", "store_reads"),
    "_append_event_in_tx": ("function", "f59eed4b3aa0", "store_events"),
    "_apply_journal_mode": ("function", "488187349949", "store"),
    "_c": ("function", "55cc4a2df3bc", "store"),
    "_exclusion": ("staticmethod", "b84d3f611190", "store_reads"),
    "_lane_in_tx": ("staticmethod", "b562f2c993cc", "store"),
    "_load_unstarted_ids": ("function", "488187349949", "store"),
    "_note_state": ("function", "c8c5ef45db8d", "store"),
    "_on_running_loop_thread": ("staticmethod", "e39dc1a793b2", "store"),
    "_open_connection": ("function", "55cc4a2df3bc", "store"),
    "_quarantine_corrupt_file": ("function", "06c325f39971", "store"),
    "_record_incarnation": ("function", "488187349949", "store"),
    "_rollback": ("staticmethod", "9dd8201bdc07", "store"),
    "_writer_executor": ("function", "9d39d0b32056", "store"),
    "accept": ("function", "9271fcba0045", "store"),
    "accept_one": ("function", "7f0b418eb339", "store"),
    "active_rows": ("function", "dd79c92622cf", "store_reads"),
    "advance": ("function", "dbcb83afdc15", "store"),
    "append_event": ("function", "66a295523266", "store_events"),
    "cancel": ("function", "d4013ca0bd37", "store"),
    "children_of": ("function", "bff61c8de7da", "store_waits"),
    "claim": ("function", "a7f3fdc80339", "store"),
    "claim_next": ("function", "1331a847b450", "store"),
    "close": ("function", "b60c5fea256e", "store"),
    "count": ("function", "27fbc4d049af", "store_reads"),
    "count_by_state": ("function", "d18b0d78226c", "store_reads"),
    "count_pending": ("function", "fccf5b871169", "store_reads"),
    "default_path": ("classmethod", "13e9c8d66d64", "store"),
    "defer": ("function", "47ec57e9ee6a", "store"),
    "deferred_longer_than": ("function", "766523931bac", "store"),
    "doctor_lines": ("function", "6806e867af4e", "store"),
    "enter_wait": ("function", "233981a63998", "store_waits"),
    "events": ("function", "dcbc79f76625", "store_events"),
    "expedite": ("function", "ee48a96a86fb", "store"),
    "fetch_dispatchable": ("function", "7e6bf916bc42", "store_reads"),
    "fetch_dispatchable_fair": ("function", "49927490a5d6", "store_reads"),
    "fetch_pending_by_batch": ("function", "7a679d452d1e", "store_reads"),
    "finish": ("function", "8a4938b244d9", "store"),
    "get": ("function", "c698159610c8", "store_reads"),
    "incarnation": ("property", "bb418bb7ece8", "store"),
    "insert_if_absent": ("function", "b306bf988231", "store"),
    "is_open": ("property", "a6632a57e602", "store"),
    "is_unstarted": ("function", "ae22938dd735", "store"),
    "journal_mode": ("property", "bb418bb7ece8", "store"),
    "latest_events": ("function", "e6770a72a485", "store_events"),
    "lease_secs": ("property", "59162e39f6f1", "store"),
    "list_pending": ("function", "59c914639c9c", "store_reads"),
    "list_rows": ("function", "4eb95eebd967", "store_reads"),
    "lost_owner_recoveries": ("function", "9f7ec827b073", "store_events"),
    "mark_reported": ("function", "2b842e351101", "store"),
    "next_eligible_at": ("function", "377d9e28d205", "store_reads"),
    "now": ("function", "59162e39f6f1", "store"),
    "oldest_wait_secs": ("function", "8aa6421280b2", "store_reads"),
    "open": ("function", "5f5c89c6d664", "store"),
    "orphaned_children": ("function", "e6c27deb2e7c", "store_waits"),
    "overdue_unleased_rows": ("function", "042e8a3e0db4", "store_reads"),
    "owed_reports": ("function", "543929ec8760", "store"),
    "path": ("property", "b08d8e6a45bd", "store"),
    "pending_lanes": ("function", "ff1b8b9a51eb", "store_reads"),
    "post": ("function", "b31cc9bb4ff4", "store"),
    "previous_incarnation": ("property", "f21e39ec5ffb", "store"),
    "quarantined_to": ("data", None, "store"),
    "record_progress": ("function", "d84afe8169cd", "store"),
    "release_lease": ("function", "ae22938dd735", "store"),
    "renew_lease": ("function", "159082685ec1", "store"),
    "restamp_accepted_by": ("function", "b00a4740ad36", "store"),
    "run": ("coroutine", "73fb713b0e67", "store"),
    "state_of": ("function", "b76f80fb2e09", "store_reads"),
    "storage_bytes": ("function", "898eb304ecf8", "store_work"),
    "storage_charge": ("function", "431f7367ff03", "store_work"),
    "transition": ("function", "133959325b30", "store"),
    "update_wait": ("function", "233981a63998", "store_waits"),
    "waiting_rows": ("function", "ab9ff9d81fb7", "store_waits"),
    "wake_wait": ("function", "05452748d518", "store_waits"),
    "window": ("property", "fa6cfecd5574", "store"),
    "work_by_scope": ("function", "a77debf4dcba", "store_work"),
    "work_compare_and_set": ("function", "18dedc32d302", "store_work"),
    "work_get": ("function", "730e45f2e0da", "store_work"),
    "work_insert_or_get": ("function", "c97b5dd8ed08", "store_work"),
    "work_purge": ("function", "5bad78d2e1c5", "store_work"),
    "work_rows": ("function", "f26ed91926c1", "store_work"),
}

#: Every module-level name the one-module ``store.py`` bound. The facade still binds each.
_BASE_MODULE_NAMES = frozenset("""
    ACTIVE ADMITTED ADOPTED_RESUME_EVENT Any BUSY_TIMEOUT_SECS CANCELLED CLAIMABLE Callable
    ClaimResult DEFAULT_DISPATCH_WINDOW InvalidTransition Iterable LEASE_SECS
    NETWORK_FS_TYPES OnLoopDBGuard Path QUEUED RECOVERING RETRY_WAIT RUNNING
    STRICT_ON_LOOP_ENV Sequence TASKQ_COMPLETIONS TERMINAL TaskEvent TaskRecord TaskStore
    TaskStoreUnavailable ThreadPoolExecutor WAITING WAITING_INFRA _CorruptStore
    _DARWIN_MNT_LOCAL _DarwinStatfs _EVENT_ID_CHUNK _EVENT_REPORTED _ON_LOOP_DB_GUARD
    _REPORT_OWED_BY _SQL_ACTIVE _SQL_CHILD_ONLY _SQL_CLAIMABLE _SQL_TERMINAL _SQL_UNSTARTED
    _SQL_WAITING _UNSTARTED _darwin_mount_facts _lanes _linux_mount_type _nearest_existing
    _typed_read annotations asyncio check_transition ctypes dataclass
    detect_network_filesystem emit_counter functools is_damaged_database_error is_terminal
    json logger logging migrate os platform_compat prepare_owner_only_sqlite
    quarantine_sqlite_file sqlite3 steps_to sys threading time uuid
    """.split())


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in store_module._STORE_OWNERS]


def _member_owners() -> list[str]:
    """The owners that hold members: every listed module but the shared vocabulary."""
    return [name for name in store_module._STORE_OWNERS if _short(name) != "store_vocab"]


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
    skip |= {"__firstlineno__", "__static_attributes__", "__annotations__"}
    skip |= {"__annotate__", "__annotate_func__", "__annotations_cache__"}
    return {name: value for name, value in vars(TaskStore).items() if name not in skip}


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
    # The class attribute IS the owner's object (for a typed read, the wrapper the owner
    # module binds), so a patch of the class attribute reaches every caller.
    assert _function_of(value) is vars(module)[name]


def test_every_owner_is_listed_and_holds_a_member() -> None:
    listed = {_short(name) for name in store_module._STORE_OWNERS}
    owning = {owner for _kind, _digest, owner in _MEMBERS.values()} - {"store"}
    assert listed - {"store_vocab"} == owning
    taskq = Path(store_module.__file__).parent
    assert {p.stem for p in taskq.glob("store_*.py")} == listed


def test_a_subclass_super_call_reaches_the_moved_member(tmp_path: Path) -> None:
    """``super().wake_wait`` in an override resolves to the owner's function, which the
    subclasses in the suite (``_RefusingStore`` and its kin) rely on."""
    from kiro_crew.taskq import model, store_waits
    from kiro_crew.taskq.waits import WaitRecord

    calls: list[str] = []

    class _Recording(TaskStore):
        def wake_wait(self, task_id: str, **kw: Any) -> int | None:
            calls.append(task_id)
            return super().wake_wait(task_id, **kw)

    assert _Recording.__mro__[1].__dict__["wake_wait"] is store_waits.wake_wait
    store = _Recording(tmp_path / "tasks.db", network_fs=False, clock=lambda: 100.0).open()
    try:
        store.accept_one(model.TaskRecord(id="t1", kind=model.KIND_SUBAGENT, params={}))
        assert store.claim("t1") is not None
        assert store.transition("t1", model.STARTING)
        assert store.transition("t1", model.RUNNING)
        assert store.enter_wait("t1", WaitRecord.input("call-1", since=100.0).to_dict())
        generation = store.get("t1").generation
        assert store.wake_wait("t1", reason="answered") == generation + 1
        assert calls == ["t1"]
        assert store.state_of("t1") == model.RETRY_WAIT
    finally:
        store.close()


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The top-level functions of *tree* whose first parameter is ``self``."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "self":
                found.append(node)
    return found


def _top_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.mark.parametrize("owner", _member_owners())
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    """Every ``self`` function, and every function the inventory records as a static
    member of this owner, is bound on the class; a plain helper may stay module-level.
    """
    module = importlib.import_module(owner)
    members = _members()
    static = {
        n for n, (kind, _d, o) in _MEMBERS.items() if kind == "staticmethod" and o == _short(owner)
    }
    bound = {node.name for node in _self_functions(_tree(module))} | static
    unbound = sorted(
        name for name in bound if _function_of(members.get(name)) is not vars(module)[name]
    )
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
    assert [c for fn in _top_functions(tree) for c in _class_only_constructs(fn)] == []


def _bare_member_calls(tree: ast.Module, members: set[str]) -> list[str]:
    """Calls in *tree* of a store member by its bare name rather than through ``self``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in members:
                found.append(f"{node.lineno}: {node.func.id}()")
    return found


def test_the_bare_call_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n" "    self.events('t')\n" "    return events(self, 't')\n")
    assert _bare_member_calls(tree, {"events"}) == ["3: events()"]


@pytest.mark.parametrize("owner", _member_owners())
def test_an_owner_calls_a_sibling_member_through_self(owner: str) -> None:
    """A moved body reaches its siblings as ``self.<member>``, so a class or instance patch
    of that member, and a subclass override, still intercept it."""
    tree = _tree(importlib.import_module(owner))
    assert _bare_member_calls(tree, set(_MEMBERS)) == []


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
        "from kiro_crew.taskq import store_vocab\n"
        "if TYPE_CHECKING:\n"
        "    from .store import TaskStore\n"
        "from .store_events import events\n"
        "def f():\n"
        "    import kiro_crew.taskq.store\n"
        "def g():\n"
        "    from .store import TaskStore\n"
    )
    found = _runtime_imports(tree)
    assert f"{_PACKAGE}.store_vocab" in found
    assert f"{_PACKAGE}.store_events" in found  # a relative import, resolved
    assert _FACADE in found  # the function-local imports are run-time imports
    assert "kiro_crew.taskq.store.TaskStore" in found  # from g's relative import
    typing_only = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .store import TaskStore\n"
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


def _module_and_package(src: Path, path: Path) -> tuple[str, str]:
    """The dotted module *path* is under *src*, and the package its relative imports
    resolve against (a package ``__init__`` is its own package)."""
    parts = path.relative_to(src).with_suffix("").parts
    if parts[-1] == "__init__":
        dotted = ".".join(parts[:-1])
        return dotted, dotted
    dotted = ".".join(parts)
    return dotted, dotted.rpartition(".")[0]


def _src_files(src: Path) -> list[Path]:
    """The ``.py`` files under *src*: the ones git sees for the checkout's own tree
    (``source_corpus``, so an ignored or nested copy never answers), a walk for a
    self-test's tree under ``tmp_path``."""
    root = source_corpus.repo_root().resolve()
    if src.resolve().is_relative_to(root):
        return sorted(p for p in source_corpus.repo_files_named(".py") if p.is_relative_to(src))
    return sorted(src.rglob("*.py"))


def _owner_importers(src: Path, owners: frozenset[str]) -> list[str]:
    """Files under *src* that import one of *owners* at all, the owners and facade aside."""
    found = []
    for path in _src_files(src):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "store_" not in text:
            continue
        module, package = _module_and_package(src, path)
        if module in owners or module == _FACADE:
            continue
        tree = ast.parse(text)
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
    pkg = tmp_path / "kiro_crew" / "taskq"
    pkg.mkdir(parents=True)
    (pkg / "other.py").write_text("from .store_events import events\n", encoding="utf-8")
    (pkg / "__init__.py").write_text("from . import store_reads\n", encoding="utf-8")
    (pkg / "fine.py").write_text("from kiro_crew.taskq.store import X\n", encoding="utf-8")
    owners = frozenset({f"{_PACKAGE}.store_events", f"{_PACKAGE}.store_reads"})
    assert _owner_importers(tmp_path, owners) == [
        "kiro_crew/taskq/__init__.py",
        "kiro_crew/taskq/other.py",
    ]


def test_nothing_else_in_src_imports_an_owner() -> None:
    """``kiro_crew.taskq.store`` is the one import path of the store."""
    src = Path(store_module.__file__).resolve().parents[2]
    assert _owner_importers(src, frozenset(store_module._STORE_OWNERS)) == []


# --------------------------------------------------------------------------- #
# Owner docstrings name their fields; owners log under the store's logger.
# --------------------------------------------------------------------------- #


def _store_fields() -> frozenset[str]:
    """The instance state the owners share: what ``__init__`` assigns on ``self``, and
    the class-level annotated attributes."""
    cls = next(
        node
        for node in ast.parse(Path(store_module.__file__).read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef) and node.name == "TaskStore"
    )
    fields: set[str] = set()
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                    and isinstance(sub.ctx, ast.Store)
                ):
                    fields.add(sub.attr)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and not isinstance(node.value, ast.Attribute)
        ):
            fields.add(node.target.id)
    return frozenset(fields)


def test_the_field_list_is_what_the_store_holds() -> None:
    fields = _store_fields()
    assert {"_lock", "_conn", "_incarnation", "warnings", "quarantined_to"} <= fields
    assert "events" not in fields  # an annotated binding is a member, not a field


def _fields(tree: ast.Module) -> tuple[set[str], set[str]]:
    """(fields written, fields only read) through ``self.<field>`` in *tree*'s methods."""
    known = _store_fields()
    writes: set[str] = set()
    reads: set[str] = set()
    for fn in _self_functions(tree):
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in known
            ):
                (writes if isinstance(node.ctx, ast.Store) else reads).add(node.attr)
    return writes, reads - writes


def test_the_field_scan_sees_writes_and_reads() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.warnings = []\n"
        "    with self._lock:\n"
        "        return self.warnings, self.now(), self._other\n"
    )
    assert _fields(tree) == ({"warnings"}, {"_lock"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\s|$)", " ".join(doc.split()))
    assert match, f"no '{label}:' line"
    return set(re.findall(r"``(\w+)``", match.group(1)))


@pytest.mark.parametrize("owner", _member_owners())
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
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "getLogger"
        ):
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Constant) and arg.value == _FACADE):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
    return sorted(found, key=lambda entry: int(entry.split(":")[0]))


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse(
        "import logging\n"
        "logger = logging.getLogger('kiro_crew.taskq.store')\n"
        "_log = logging.getLogger(__name__)\n"
        "from logging import getLogger\n"
        "other = getLogger('kiro_crew.taskq.store_waits')\n"
    )
    assert [line.split(":")[0] for line in _foreign_loggers(tree)] == ["3", "5"]


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_an_owner_that_logs_uses_the_store_logger(owner: str) -> None:
    """The store logs under ``kiro_crew.taskq.store``; an owner that took a logger of its
    own would log under a name no filter or capture expects."""
    module = importlib.import_module(owner)
    assert _foreign_loggers(_tree(module)) == []
    assert getattr(module, "logger", store_module.logger) is store_module.logger
    assert store_module.logger.name == _FACADE


#: Every class an owner defines -> the ``__module__`` it carries. ``TaskStoreUnavailable``
#: keeps the facade's name, because a traceback and ``subagent._describe_exception`` render
#: a raised class as ``module.qualname``.
_CLASS_MODULES = {"TaskStoreUnavailable": _FACADE, "WorkRow": "kiro_crew.taskq.store_work"}


def test_each_class_an_owner_defines_carries_its_frozen_module() -> None:
    found = {
        node.name: vars(module)[node.name].__module__
        for module in _owners()
        for node in _tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert found == _CLASS_MODULES


def test_a_store_failure_renders_under_the_facade_path() -> None:
    text = "".join(traceback.format_exception_only(TaskStoreUnavailable("locked")))
    assert text == "kiro_crew.taskq.store.TaskStoreUnavailable: locked\n"


# --------------------------------------------------------------------------- #
# The facade keeps every name the one-module file bound.
# --------------------------------------------------------------------------- #


def test_every_base_module_name_still_resolves_on_the_facade() -> None:
    missing = sorted(
        name
        for name in _BASE_MODULE_NAMES
        if name != "annotations" and not hasattr(store_module, name)
    )
    assert missing == []


@pytest.mark.parametrize("owner", store_module._STORE_OWNERS)
def test_a_module_name_an_owner_binds_is_the_same_object_on_the_facade(
    owner: str,
) -> None:
    """A moved definition, or an import both bind, is one object in both namespaces."""
    module = importlib.import_module(owner)
    shared = sorted(_BASE_MODULE_NAMES & set(vars(module)) - {"annotations"})
    assert shared
    assert [name for name in shared if getattr(store_module, name) is not vars(module)[name]] == []


def test_a_typed_read_binding_carries_the_type_its_decorator_gives() -> None:
    """mypy reads the binding's declared type instead of inferring it, so a module checked
    before this class sees the type the decorated method always had."""
    typed = sorted(
        name
        for name, value in _members().items()
        if _MEMBERS[name][2] != "store" and hasattr(_function_of(value), "__wrapped__")
    )
    assert typed
    annotations = vars(TaskStore).get("__annotations__", {})
    assert {name: annotations.get(name) for name in typed} == {
        name: "Callable[..., Any]" for name in typed
    }


# --------------------------------------------------------------------------- #
# No name a test patches through the facade is read by an owner.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """Every file pytest collects tests or fixtures from, as git sees the checkout: the
    root conftest, ``test/``, and the test modules and conftests under the builtin apps.
    """
    root = source_corpus.repo_root()
    test_root = root / "test"
    files = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        name = path.name
        if rel == "conftest.py" or path.is_relative_to(test_root):
            files.append(path)
        elif rel.startswith("src/kiro_crew/apps/builtins/") and (
            "/tests/" in rel
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
        ):
            files.append(path)
    return sorted(files)


def _facade_spellings(src: Path) -> dict[str, str]:
    """The facade's own name, and every src module's module-level binding of it: a patch
    through either reaches the facade."""
    spellings = {_FACADE: _FACADE}
    for path in _src_files(src):
        text = path.read_text(encoding="utf-8", errors="replace")
        # A binding of the facade names ``taskq``, or sits in the package as ``. store``.
        if "taskq" not in text and not ("store" in text and path.parent.name == "taskq"):
            continue
        module, package = _module_and_package(src, path)
        for node in ast.parse(text).body:
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                or (isinstance(node, ast.ImportFrom) and node.level)
            ):
                base = node.module or ""
                if node.level:
                    parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                    base = f"{parent}.{base}" if base else parent
                for alias in node.names:
                    if f"{base}.{alias.name}" == _FACADE and module != _FACADE:
                        spellings[f"{module}.{alias.asname or alias.name}"] = _FACADE
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _FACADE and alias.asname:
                        spellings[f"{module}.{alias.asname}"] = _FACADE
    return spellings


def test_the_spelling_scan_sees_an_aliased_binding(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "cli"
    pkg.mkdir(parents=True)
    (pkg / "tasks.py").write_text("from kiro_crew.taskq import store as tstore\n", encoding="utf-8")
    (pkg / "rel.py").write_text("from ..taskq import store\n", encoding="utf-8")
    assert _facade_spellings(tmp_path) == {
        _FACADE: _FACADE,
        "kiro_crew.cli.tasks.tstore": _FACADE,
        "kiro_crew.cli.rel.store": _FACADE,
    }


def _patched_through_the_facade(files: list[Path], spellings: dict[str, str]) -> set[str]:
    names: set[str] = set()
    # A patch through a spelling names the module that spelling lives in, and a test that
    # imports the facade itself names ``taskq``.
    needles = {"taskq", *(spelling.rpartition(".")[0] for spelling in spellings)}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(needle in text for needle in needles):
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


def test_the_patch_scan_sees_the_facade_patches_it_must() -> None:
    # test_taskq_store.py's network-filesystem detector patches, both read by kept code.
    assert {"_linux_mount_type", "detect_network_filesystem"} <= _corpus_patched_names()


def test_no_name_patched_through_the_facade_is_read_by_an_owner() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.taskq import store as store_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(store_mod, '_SQL_CLAIMABLE', '(1)')\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch('kiro_crew.taskq.store.TaskStoreUnavailable', RuntimeError):\n"
        "        pass\n",
        encoding="utf-8",
    )
    patched = _patched_through_the_facade([probe], {_FACADE: _FACADE})
    assert patched == {"_SQL_CLAIMABLE", "TaskStoreUnavailable"}
    gaps = _reach_gaps(patched, _owner_reads())
    assert f"{_PACKAGE}.store_reads._SQL_CLAIMABLE" in gaps
    assert f"{_PACKAGE}.store_waits.TaskStoreUnavailable" in gaps


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
