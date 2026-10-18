"""``_TaskqBridgeMixin`` keeps every member it had, each bound from the module that holds it.

The durable-queue glue of the admission coordinator is one class, ``_TaskqBridgeMixin`` in
``admission/taskq_bridge.py``. Its body keeps the store handle, the shared exclusion sets, the
store's lifecycle and a run's own writes as methods; every other member's body is a
module-level function of a flat ``admission/taskq_bridge_*.py`` owner whose first parameter is
``self``, and the class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The
state stays on the manager, so tests that patch a member on ``SpawnAdmissionCoordinator``, copy
one into a fake class or call one unbound see the object they always did. This file pins the
binding contract:

* the frozen member inventory -- every member, its kind (a coroutine apart from a function) and
  its signature (the ``self`` annotation aside) as the one-module class had them, and the module
  that holds it now; each bound member IS its owner's function, and no owner keeps an unbound
  ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled attribute,
  which only work inside a class body, and none is named ``*_impl`` (``bind_component_globals``
  would rebind it onto ``subagent``'s namespace);
* an owner calls another member only through ``self``, so a class-level patch reaches the call;
* no owner imports the facade or another owner at run time, and nothing else in ``src`` imports
  an owner;
* a member a sibling mixin declares as a typing stub stays a method of the class body (mypy's
  multiple-inheritance check binds ``self`` only on a ``def``);
* each owner's docstring names the manager fields its functions write and read, and an owner
  logs through the admission logger;
* every module-level name the one-module file bound still resolves on the facade, a moved one to
  the object its owner holds, bound there only by importing it;
* no name the test trees patch through the facade is read as a global by an owner: an owner reads
  its own namespace, so such a patch would not reach it.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import inspect
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from facade_mirror_scan import global_reads, patched_names

import kiro_crew.subagent as subagent_mod
from kiro_crew.subagent_manager.admission import taskq_bridge as facade
from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

pytestmark = pytest.mark.xdist_group(name="tree_scan_taskq_bridge_bindings")

_FACADE = "kiro_crew.subagent_manager.admission.taskq_bridge"
_PACKAGE = "kiro_crew.subagent_manager.admission"
_ADMISSION_LOGGER = "kiro_crew.subagent_manager.admission"
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: The owners the class binds members from, by module stem. A new owner adds its row here.
_OWNERS = (
    "taskq_bridge_accept",
    "taskq_bridge_expiry",
    "taskq_bridge_reads",
    "taskq_bridge_window",
    "taskq_bridge_cancel",
)

#: Member -> (kind, signature digest, holding module stem). Kind and signature are the
#: one-module class's; ``taskq_bridge`` is the facade's own class body. A new member adds a row.
_MEMBERS: dict[str, tuple[str, str | None, str]] = {
    "__slots__": ("data", None, "taskq_bridge"),
    "_clear_owed_report_after": ("function", "6b0e27b1b800", "taskq_bridge_expiry"),
    "_evict_for_lanes": ("function", "70e2baf55384", "taskq_bridge_window"),
    "_expire_memory_waits_db": ("staticmethod", "953f89bd69aa", "taskq_bridge_expiry"),
    "_live_run_ids": ("function", "6806e867af4e", "taskq_bridge_reads"),
    "_memory_wait_exclusions": ("function", "6806e867af4e", "taskq_bridge_expiry"),
    "_overflow_query": ("function", "c493d7f1c2bf", "taskq_bridge_reads"),
    "_owed_replay_wanted": ("function", "a6632a57e602", "taskq_bridge_expiry"),
    "_post_store_write": ("function", "4588bfef5a46", "taskq_bridge"),
    "_queued_run": ("function", "624970a57df9", "taskq_bridge_reads"),
    "_queued_run_from_row": ("function", "e2e687ef3a33", "taskq_bridge_reads"),
    "_refill_absent": ("function", "f9f1a666367b", "taskq_bridge_window"),
    "_refill_apply": ("function", "76ecec12c2c1", "taskq_bridge_window"),
    "_refill_fetch": ("function", "8c9621a8c5d1", "taskq_bridge_window"),
    "_refill_fetch_retiring": ("function", "68631f9cf773", "taskq_bridge_window"),
    "_refill_idle_wake_read": ("function", "57824d5f15b2", "taskq_bridge_window"),
    "_refill_make_room": ("function", "91fe718552bf", "taskq_bridge_window"),
    "_refill_schedule_wake": ("function", "d2a5d800e638", "taskq_bridge_window"),
    "_release_taken_start": ("staticmethod", "88b2c6801a15", "taskq_bridge"),
    "_release_untaken_claim": ("staticmethod", "88b2c6801a15", "taskq_bridge"),
    "_report_memory_waits_expired": ("function", "b85318a14121", "taskq_bridge_expiry"),
    "_report_retired_stage_rows": ("function", "4518b202e81c", "taskq_bridge_window"),
    "_retained_claim_ids_for": ("function", "e98ffea20ad3", "taskq_bridge_reads"),
    "_retained_starting_ids": ("staticmethod", "d48075c1d70b", "taskq_bridge_reads"),
    "_retire_legacy_stage_rows": ("staticmethod", "0126d480eb6b", "taskq_bridge_window"),
    "_sweep_inputs": ("function", "257f34fe1377", "taskq_bridge_expiry"),
    "_window_entry": ("staticmethod", "44d01f3428d8", "taskq_bridge_window"),
    "await_pending_defer": ("coroutine", "a7044e95935d", "taskq_bridge_accept"),
    "finish_parked_defer": ("coroutine", "8a9d36d2a473", "taskq_bridge_accept"),
    "lane_refill_scheduler": ("function", "020878443ab5", "taskq_bridge_window"),
    "park_defer": ("function", "d89931cb3585", "taskq_bridge_accept"),
    "taskq_accept": ("function", "31185aeae9eb", "taskq_bridge_accept"),
    "taskq_accept_record": ("function", "8046567e12c6", "taskq_bridge_accept"),
    "taskq_admit_wait_secs": ("function", "59162e39f6f1", "taskq_bridge"),
    "taskq_advance": ("function", "b70495611d88", "taskq_bridge"),
    "taskq_arm_reopen": ("function", "ad7cb1f4a178", "taskq_bridge"),
    "taskq_artifact_probe": ("staticmethod", "9a4b8cbf4de4", "taskq_bridge"),
    "taskq_batch_pending": ("function", "f583177755f4", "taskq_bridge_reads"),
    "taskq_batch_pending_async": ("coroutine", "f583177755f4", "taskq_bridge_reads"),
    "taskq_boot_dispatch": ("function", "b60c5fea256e", "taskq_bridge"),
    "taskq_build_record": ("function", "287602130dfe", "taskq_bridge_accept"),
    "taskq_cancel_queued": ("function", "9f25d5da13b6", "taskq_bridge_cancel"),
    "taskq_cancel_queued_async": ("coroutine", "9f25d5da13b6", "taskq_bridge_cancel"),
    "taskq_cancel_queued_each": ("function", "dffc118b1018", "taskq_bridge_cancel"),
    "taskq_chip_excluded_ids": ("function", "6806e867af4e", "taskq_bridge_reads"),
    "taskq_chip_overflow_async": ("coroutine", "d92f11a663b3", "taskq_bridge_reads"),
    "taskq_clear_owed_report_if_delivered": ("function", "2726da679e64", "taskq_bridge_expiry"),
    "taskq_clear_owed_reports": ("function", "c71df92fa7a2", "taskq_bridge_expiry"),
    "taskq_defer": ("function", "97438f14bf56", "taskq_bridge_accept"),
    "taskq_defer_posted": ("function", "49f1fc64f591", "taskq_bridge_accept"),
    "taskq_dispatch_excluded_ids": ("function", "e74d1fb976c9", "taskq_bridge"),
    "taskq_excluded_ids": ("function", "cdd1976eccb3", "taskq_bridge"),
    "taskq_expire_memory_waits": ("function", "fa6cfecd5574", "taskq_bridge_expiry"),
    "taskq_expire_memory_waits_async": ("coroutine", "fa6cfecd5574", "taskq_bridge_expiry"),
    "taskq_fail": ("function", "505028c6a263", "taskq_bridge"),
    "taskq_mark": ("function", "1eda5def8934", "taskq_bridge"),
    "taskq_memory_wait_bound_secs": ("function", "59162e39f6f1", "taskq_bridge_expiry"),
    "taskq_open": ("function", "1db23407d81c", "taskq_bridge"),
    "taskq_overflow": ("function", "c9d9151f01f4", "taskq_bridge_reads"),
    "taskq_overflow_async": ("coroutine", "c9d9151f01f4", "taskq_bridge_reads"),
    "taskq_overflow_or_none_async": ("coroutine", "02cb33ee54c2", "taskq_bridge_reads"),
    "taskq_pending_ids_for": ("function", "e98ffea20ad3", "taskq_bridge_reads"),
    "taskq_pending_ids_for_async": ("coroutine", "479dddcc4996", "taskq_bridge_reads"),
    "taskq_post_cancel_queued": ("function", "fba1cc4be416", "taskq_bridge_cancel"),
    "taskq_queued_run_async": ("coroutine", "e45c565d6310", "taskq_bridge_reads"),
    "taskq_queued_runs_async": ("coroutine", "7203a2acc47c", "taskq_bridge_reads"),
    "taskq_refill_window": ("function", "d7d19b2d4314", "taskq_bridge_window"),
    "taskq_refill_window_async": ("coroutine", "d7d19b2d4314", "taskq_bridge_window"),
    "taskq_reopen_if_due": ("function", "a6632a57e602", "taskq_bridge"),
    "taskq_replay_owed_expiries_async": ("coroutine", "fa6cfecd5574", "taskq_bridge_expiry"),
    "taskq_report_refused_settle": ("staticmethod", "7767105a1bdf", "taskq_bridge"),
    "taskq_required_but_unavailable": ("function", "f21e39ec5ffb", "taskq_bridge"),
    "taskq_row_is_claimed_unstarted_async": ("coroutine", "6140c73448b2", "taskq_bridge_cancel"),
    "taskq_schedule_owed_replay": ("function", "b60c5fea256e", "taskq_bridge_expiry"),
    "taskq_settle": ("function", "75cad662f3a5", "taskq_bridge"),
    "taskq_should_window": ("function", "6140c73448b2", "taskq_bridge_window"),
    "taskq_should_window_async": ("coroutine", "6140c73448b2", "taskq_bridge_window"),
    "taskq_store": ("function", "ae33a6d89b58", "taskq_bridge"),
    "taskq_superseded_by_live_owner": ("staticmethod", "4ac3f90aa4e1", "taskq_bridge"),
    "taskq_window_ids": ("function", "6806e867af4e", "taskq_bridge"),
    "track_store_task": ("function", "b9848cb44167", "taskq_bridge"),
}

#: Module-level names the one-module ``taskq_bridge.py`` bound that the facade does not bind:
#: standard-library and typing imports only moved code reads.
_DROPPED_IMPORTS = frozenset({"Sequence", "_functools"})

#: Every module-level name the one-module ``taskq_bridge.py`` bound at run time.
_BASE_MODULE_NAMES = frozenset("""
    Any Collection DeferPoint FairnessSettings MEMORY_WAIT_UNTIL_KEY MIN_RECHECK_DELAY_SECS
    ManagerComponent Mapping QUEUED_LISTING_CAP QUEUED_WAIT_EXPIRED_TEXT QueuedReadUnavailable
    QueuedRun QueuedRunListing RESUMING_AFTER_RESTART RESUMING_RETRY Sequence TYPE_CHECKING
    UNKNOWN_PENDING WINDOW_ENTRY_RECOVERING _DEFER_CLOSERS _OVERDUE_WAKE_GRACE_SECS
    _OVERDUE_WAKE_WARN_EVERY_SECS _OWED_REPLAY_PAGE _STATE_READ_ATTEMPTS _STATE_READ_BACKOFF_SECS
    _TaskqBridgeMixin _asyncio _defer_details _functools _glue_logger _in_memory_queued_entries
    _logging _read_queued_row _read_queued_rows _time annotations outcome_task_state
    tombstone_terminal_state
    """.split())

#: Module-level names an owner holds now -> that owner. The facade re-exports each one.
_MOVED_MODULE_NAMES = {
    "UNKNOWN_PENDING": "taskq_bridge_reads",
    "QUEUED_LISTING_CAP": "taskq_bridge_reads",
    "_DEFER_CLOSERS": "taskq_bridge_reads",
    "_defer_details": "taskq_bridge_reads",
    "_read_queued_row": "taskq_bridge_reads",
    "_read_queued_rows": "taskq_bridge_reads",
    "_in_memory_queued_entries": "taskq_bridge_reads",
    "_OWED_REPLAY_PAGE": "taskq_bridge_expiry",
    "_OVERDUE_WAKE_GRACE_SECS": "taskq_bridge_window",
    "_OVERDUE_WAKE_WARN_EVERY_SECS": "taskq_bridge_window",
    "_STATE_READ_ATTEMPTS": "taskq_bridge_cancel",
    "_STATE_READ_BACKOFF_SECS": "taskq_bridge_cancel",
}


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"{_PACKAGE}.{stem}")


def _owners() -> list[ModuleType]:
    return [_owner(stem) for stem in _OWNERS]


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
    """The class's own attributes, the interpreter's dunders aside (``__slots__`` is the class's)."""
    return {
        name: value
        for name, value in vars(_TaskqBridgeMixin).items()
        if name == "__slots__" or not (name.startswith("__") and name.endswith("__"))
    }


# --------------------------------------------------------------------------- #
# The member inventory and the binding.
# --------------------------------------------------------------------------- #


def test_the_kind_reader_tells_a_coroutine_from_a_function() -> None:
    async def stand_in(self: object) -> None:
        return None

    assert (_kind(stand_in), _kind(lambda self: None)) == ("coroutine", "function")
    assert _kind(staticmethod(stand_in)) == "staticmethod"


def test_the_member_set_is_the_frozen_inventory() -> None:
    assert sorted(_members()) == sorted(_MEMBERS)


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_owner(name: str) -> None:
    kind, digest, owner = _MEMBERS[name]
    value = _members()[name]
    assert _kind(value) == kind
    assert _signature_digest(value) == digest
    fn = _function_of(value)
    if owner == "taskq_bridge":
        if callable(fn):
            assert fn.__module__ == _FACADE
        return
    # The class attribute IS the owner's function.
    assert fn is vars(_owner(owner))[name]


def test_every_owner_is_a_flat_sibling_and_holds_a_member() -> None:
    admission = Path(facade.__file__).resolve().parent
    on_disk = sorted(p.stem for p in admission.glob("taskq_bridge_*.py"))
    assert on_disk == sorted(_OWNERS)
    # The admission scans (the registration census, the counter sweep) glob ``*.py`` flat.
    assert [p.name for p in admission.glob("taskq_bridge_*") if p.is_dir()] == []
    for module in _owners():
        assert Path(module.__file__).resolve().parent == admission
    holding = {owner for _kind, _digest, owner in _MEMBERS.values()} - {"taskq_bridge"}
    assert holding == set(_OWNERS)


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The top-level functions of *tree* whose first parameter is ``self``."""
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args
            if args and args[0].arg == "self":
                found.append(node)
    return found


@pytest.mark.parametrize("stem", _OWNERS)
def test_no_owner_keeps_an_unbound_self_function(stem: str) -> None:
    module = _owner(stem)
    members = _members()
    unbound = [
        node.name
        for node in _self_functions(_tree(module))
        if _function_of(members.get(node.name)) is not vars(module)[node.name]
    ]
    assert unbound == []


def test_no_bound_member_is_named_impl() -> None:
    """``bind_component_globals`` rebinds every ``*_impl`` onto ``subagent``'s globals."""
    assert [name for name in _members() if name.endswith("_impl")] == []


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


@pytest.mark.parametrize("stem", _OWNERS)
def test_no_bound_function_needs_its_class_body(stem: str) -> None:
    tree = _tree(_owner(stem))
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert functions
    assert [c for fn in functions for c in _class_only_constructs(fn)] == []


def _direct_member_calls(tree: ast.Module, members: set[str]) -> list[str]:
    """Calls in *tree* that name a member as a bare function, bypassing ``self``."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in members
        ):
            found.append(f"{node.lineno}: {node.func.id}")
    return found


def test_the_direct_call_scan_can_fail() -> None:
    tree = ast.parse(
        "def taskq_overflow(self):\n"
        "    return _overflow_query(self, None), self._overflow_query(None)\n"
    )
    assert _direct_member_calls(tree, {"_overflow_query"}) == ["2: _overflow_query"]


@pytest.mark.parametrize("stem", _OWNERS)
def test_an_owner_calls_another_member_only_through_self(stem: str) -> None:
    """A class-level patch of a member reaches a call made through ``self``, not a bare one."""
    members = {name for name in _MEMBERS if not name.startswith("__")}
    assert _direct_member_calls(_tree(_owner(stem)), members) == []


# --------------------------------------------------------------------------- #
# Imports: owners never import the facade or each other; nothing else imports an owner.
# --------------------------------------------------------------------------- #


def _resolve(node: ast.ImportFrom, package: str) -> str:
    base = node.module or ""
    if node.level:
        parent = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
        base = f"{parent}.{base}" if base else parent
    return base


def _runtime_imports(tree: ast.Module, package: str = _PACKAGE) -> set[str]:
    """Dotted modules *tree* imports outside ``if TYPE_CHECKING:``, a relative import
    resolved against *package* (the admission package)."""
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
            base = _resolve(node, package)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_runtime_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "from . import taskq_bridge_reads\n"
        "if TYPE_CHECKING:\n"
        "    from .taskq_bridge import _TaskqBridgeMixin\n"
        "def f():\n"
        "    import kiro_crew.subagent_manager.admission.taskq_bridge\n"
        "def g():\n"
        "    from .taskq_bridge_window import _window_entry\n"
    )
    found = _runtime_imports(tree)
    assert f"{_PACKAGE}.taskq_bridge_reads" in found
    assert _FACADE in found  # the function-local import is a run-time import
    assert f"{_PACKAGE}.taskq_bridge_window" in found  # a relative one, resolved
    typing_only = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .taskq_bridge import _TaskqBridgeMixin\n"
    )
    assert _runtime_imports(typing_only) & {_FACADE, f"{_FACADE}._TaskqBridgeMixin"} == set()


@pytest.mark.parametrize("stem", _OWNERS)
def test_no_owner_imports_the_facade_or_another_owner_at_run_time(stem: str) -> None:
    found = _runtime_imports(_tree(_owner(stem)))
    banned = {_FACADE, *(f"{_PACKAGE}.{other}" for other in _OWNERS)}
    assert sorted(name for name in found if name in banned or name.startswith(_FACADE + ".")) == []


def _owner_importers(src: Path, owners: frozenset[str]) -> list[str]:
    """Files under *src* that import one of *owners*, the owners and the facade aside."""
    found = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "taskq_bridge_" not in text:
            continue
        module = ".".join(path.relative_to(src).with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
            package = module
        else:
            package = module.rpartition(".")[0]
        if module in owners or module == _FACADE:
            continue
        for node in ast.walk(ast.parse(text)):
            names: set[str] = set()
            if isinstance(node, ast.ImportFrom):
                base = _resolve(node, package)
                names = {base, *(f"{base}.{alias.name}" for alias in node.names)}
            elif isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            if names & owners:
                found.append(path.relative_to(src).as_posix())
                break
    return found


def test_the_owner_importer_scan_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "subagent_manager" / "admission"
    pkg.mkdir(parents=True)
    (pkg / "other.py").write_text(
        "from .taskq_bridge_reads import UNKNOWN_PENDING\n", encoding="utf-8"
    )
    (pkg / "fine.py").write_text(
        "from .taskq_bridge import UNKNOWN_PENDING  # taskq_bridge_reads\n", encoding="utf-8"
    )
    owners = frozenset({f"{_PACKAGE}.taskq_bridge_reads"})
    assert _owner_importers(tmp_path, owners) == ["kiro_crew/subagent_manager/admission/other.py"]


def test_nothing_else_in_src_imports_an_owner() -> None:
    """``admission/taskq_bridge.py`` is the one import path of the bridge."""
    src = Path(facade.__file__).resolve().parents[3]
    owners = frozenset(f"{_PACKAGE}.{stem}" for stem in _OWNERS)
    assert _owner_importers(src, owners) == []


# --------------------------------------------------------------------------- #
# A member a sibling mixin declares as a typing stub stays a def of the class body.
# --------------------------------------------------------------------------- #


def _sibling_stubs(tree: ast.Module) -> set[str]:
    """Names the classes of *tree* declare inside a class-level ``if TYPE_CHECKING:``."""
    found: set[str] = set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for node in cls.body:
            if not (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
            ):
                continue
            for stub in node.body:
                if isinstance(stub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(stub.name)
                elif isinstance(stub, ast.AnnAssign) and isinstance(stub.target, ast.Name):
                    found.add(stub.target.id)
    return found


def test_the_sibling_stub_scan_can_fail() -> None:
    tree = ast.parse(
        "class _Sibling:\n"
        "    if TYPE_CHECKING:\n"
        "        def taskq_store(self): ...\n"
        "        pump_off_loop: bool\n"
        "    def own(self): ...\n"
    )
    assert _sibling_stubs(tree) == {"taskq_store", "pump_off_loop"}


def test_a_member_a_sibling_mixin_stubs_stays_a_def_of_the_class() -> None:
    """mypy compares a sibling's stub with this class's attribute; a binding fails that check."""
    admission = Path(facade.__file__).resolve().parent
    stubbed: set[str] = set()
    for path in sorted(admission.glob("*.py")):
        if path.stem == "taskq_bridge" or path.stem in _OWNERS:
            continue
        stubbed |= _sibling_stubs(ast.parse(path.read_text(encoding="utf-8")))
    shared = sorted(stubbed & set(_MEMBERS))
    assert shared  # the four the siblings declare today
    assert {name: _MEMBERS[name][2] for name in shared} == {name: "taskq_bridge" for name in shared}


# --------------------------------------------------------------------------- #
# Owner docstrings name their manager fields; owners log through the admission logger.
# --------------------------------------------------------------------------- #

_MUTATORS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "remove",
        "setdefault",
        "update",
    }
)


@functools.lru_cache(maxsize=1)
def _manager_methods() -> frozenset[str]:
    """The methods ``SubagentManager`` defines or binds from a collaborator: calls, not fields."""
    return frozenset(
        name
        for name, member in vars(subagent_mod.SubagentManager).items()
        if inspect.isfunction(member) or isinstance(member, (staticmethod, classmethod, property))
    )


def _manager_fields(fn: ast.AST, methods: frozenset[str]) -> tuple[set[str], set[str]]:
    """(written, only read) ``SubagentManager`` fields *fn* reaches through ``self._manager``.

    Through ``self._manager.F``, a local alias of ``self._manager`` (or a helper's ``manager``
    parameter), or ``getattr``/``setattr`` with a literal name. A write is an assignment,
    ``setattr``, a subscript store or a mutator call on the field, directly or on a local bound
    to the field's value.
    """
    aliases: set[str] = set()
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        aliases |= {a.arg for a in fn.args.args if a.arg == "manager"}
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and ast.unparse(n.value) == "self._manager"
        ):
            aliases.add(n.targets[0].id)

    def is_mgr(expr: ast.AST) -> bool:
        return ast.unparse(expr) == "self._manager" or (
            isinstance(expr, ast.Name) and expr.id in aliases
        )

    def field_of(expr: ast.AST) -> str | None:
        if isinstance(expr, ast.Attribute) and is_mgr(expr.value):
            return expr.attr
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "getattr"
            and len(expr.args) >= 2
            and is_mgr(expr.args[0])
            and isinstance(expr.args[1], ast.Constant)
        ):
            return str(expr.args[1].value)
        return None

    holders: dict[str, str] = {}
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            value = n.value.values[0] if isinstance(n.value, ast.BoolOp) else n.value
            if isinstance(value, ast.Call) and ast.unparse(value.func) in {"list", "dict"}:
                continue  # a copy: mutating it does not reach the field
            field = field_of(value)
            if field is not None:
                holders[n.targets[0].id] = field
    written: set[str] = set()
    read: set[str] = set()
    for n in ast.walk(fn):
        field = field_of(n)
        if field is not None:
            read.add(field)
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
            if is_mgr(n.value):
                written.add(n.attr)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr":
            if len(n.args) >= 2 and is_mgr(n.args[0]) and isinstance(n.args[1], ast.Constant):
                written.add(str(n.args[1].value))
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _MUTATORS
        ):
            base = n.func.value
            field = field_of(base)
            if field is None and isinstance(base, ast.Name):
                field = holders.get(base.id)
            if field is not None:
                written.add(field)
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
            field = field_of(n.value)
            if field is None and isinstance(n.value, ast.Name):
                field = holders.get(n.value.id)
            if field is not None:
                written.add(field)
    return written - methods, (read - written) - methods


def _owner_fields(tree: ast.Module) -> tuple[set[str], set[str]]:
    written: set[str] = set()
    read: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            w, r = _manager_fields(node, _manager_methods())
            written |= w
            read |= r
    return written, read - written


def test_the_field_scan_sees_writes_and_reads() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    mgr = self._manager\n"
        "    mgr._a = 1\n"
        "    setattr(self._manager, '_b', 2)\n"
        "    pending = getattr(self._manager, '_c', None)\n"
        "    pending[1] = 2\n"
        "    self._manager._d.add(3)\n"
        "    copy = list(getattr(self._manager, '_e', ()) or ())\n"
        "    copy.append(4)\n"
        "    self._manager._drain_queue()\n"
        "    return self._manager._f\n"
        "def _helper(manager):\n"
        "    return manager._g\n"
    )
    methods = frozenset({"_drain_queue"})
    written: set[str] = set()
    read: set[str] = set()
    for node in tree.body:
        w, r = _manager_fields(node, methods)
        written |= w
        read |= r
    assert (written, read - written) == ({"_a", "_b", "_c", "_d"}, {"_e", "_f", "_g"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\s|$)", " ".join(doc.split()))
    assert match, f"no '{label}:' line"
    return set(re.findall(r"``(\w+)``", match.group(1)))


@pytest.mark.parametrize("stem", _OWNERS)
def test_each_owner_names_the_manager_fields_it_writes_and_reads(stem: str) -> None:
    module = _owner(stem)
    doc = module.__doc__ or ""
    assert (_listed(doc, "Writes"), _listed(doc, "Reads")) == _owner_fields(_tree(module))


def _foreign_loggers(tree: ast.Module) -> list[str]:
    """Each ``getLogger(...)`` call in *tree* whose argument is not the admission logger's name."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "getLogger"
        ):
            arg = node.args[0] if node.args else None
            if not (isinstance(arg, ast.Constant) and arg.value == _ADMISSION_LOGGER):
                found.append(f"{node.lineno}: {ast.unparse(node)}")
    return found


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse(
        "import logging as _logging\n"
        "_glue_logger = _logging.getLogger('kiro_crew.subagent_manager.admission')\n"
        "_log = _logging.getLogger(__name__)\n"
        "from logging import getLogger\n"
        "other = getLogger('kiro_crew.subagent_manager.admission.taskq_bridge_reads')\n"
    )
    assert [line.split(":")[0] for line in _foreign_loggers(tree)] == ["3", "5"]


@pytest.mark.parametrize("stem", _OWNERS)
def test_an_owner_that_logs_uses_the_admission_logger(stem: str) -> None:
    """Tests filter records on the admission logger's name, so an owner's records carry it."""
    module = _owner(stem)
    assert _foreign_loggers(_tree(module)) == []
    if "_glue_logger" in vars(module):
        assert module._glue_logger is facade._glue_logger
    assert facade._glue_logger.name == _ADMISSION_LOGGER


# --------------------------------------------------------------------------- #
# The facade keeps every name the one-module file bound.
# --------------------------------------------------------------------------- #


def test_every_base_module_name_still_resolves_on_the_facade() -> None:
    assert _DROPPED_IMPORTS <= _BASE_MODULE_NAMES
    missing = sorted(
        name
        for name in _BASE_MODULE_NAMES - _DROPPED_IMPORTS
        if name != "annotations" and not hasattr(facade, name)
    )
    assert missing == []


@pytest.mark.parametrize("name", sorted(_MOVED_MODULE_NAMES))
def test_a_moved_module_name_is_its_owners_object_on_the_facade(name: str) -> None:
    owner = _owner(_MOVED_MODULE_NAMES[name])
    assert getattr(facade, name) is vars(owner)[name]


def _facade_bindings(tree: ast.Module) -> dict[str, list[str]]:
    """Each module-level name of *tree* -> how it is bound: ``import <module>`` or ``assign``."""
    found: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.setdefault(alias.asname or alias.name, []).append(
                    f"import {_resolve(node, _PACKAGE)}"
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for n in ast.walk(target):
                    if isinstance(n, ast.Name):
                        found.setdefault(n.id, []).append("assign")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.setdefault(node.name, []).append("assign")
    return found


def test_the_facade_binding_scan_sees_an_assignment() -> None:
    tree = ast.parse("from .taskq_bridge_reads import UNKNOWN_PENDING\nUNKNOWN_PENDING = 1\n")
    assert _facade_bindings(tree)["UNKNOWN_PENDING"] == [
        f"import {_PACKAGE}.taskq_bridge_reads",
        "assign",
    ]


def test_the_facade_binds_a_moved_name_only_by_importing_it_from_its_owner() -> None:
    """``is`` cannot tell two copies of a small int apart, so read how the facade binds them."""
    bindings = _facade_bindings(_tree(facade))
    assert {name: bindings.get(name) for name in _MOVED_MODULE_NAMES} == {
        name: [f"import {_PACKAGE}.{owner}"] for name, owner in _MOVED_MODULE_NAMES.items()
    }


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


def _patched_through_the_facade(files: list[Path]) -> tuple[set[str], int]:
    """Every name *files* patch through the facade, and how many files name it at all."""
    names: set[str] = set()
    naming = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Every spelling of the facade, and every alias a test binds to it, names it.
        if re.search(r"\btaskq_bridge\b", text) is None:
            continue
        naming += 1
        try:
            found = patched_names(text, {_FACADE: _FACADE})
        except SyntaxError:
            continue
        names |= found.get(_FACADE, set())
    return names, naming


@functools.lru_cache(maxsize=1)
def _corpus_patches() -> tuple[frozenset[str], int]:
    names, naming = _patched_through_the_facade(_test_tree_files())
    return frozenset(names), naming


def _owner_reads() -> dict[str, set[str]]:
    return {
        module.__name__: global_reads(
            Path(module.__file__).read_text(encoding="utf-8"), module.__file__
        )
        for module in _owners()
    }


def _reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> list[str]:
    return sorted(f"{owner}.{name}" for owner, names in reads.items() for name in patched & names)


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    files = _test_tree_files()
    assert Path(__file__).resolve() in files
    assert _REPO_ROOT / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


def test_the_patch_scan_reads_the_files_that_name_the_facade() -> None:
    _patched, naming = _corpus_patches()
    # This file, the queued-visibility, queue-depth and max-wait suites at least.
    assert naming >= 4


def test_no_name_patched_through_the_facade_is_read_by_an_owner() -> None:
    patched, _naming = _corpus_patches()
    assert _reach_gaps(set(patched), _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.subagent_manager.admission import taskq_bridge as bridge\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(bridge, 'QUEUED_LISTING_CAP', 1)\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch(\n"
        "        'kiro_crew.subagent_manager.admission.taskq_bridge._STATE_READ_ATTEMPTS', 1\n"
        "    ):\n"
        "        pass\n",
        encoding="utf-8",
    )
    patched, naming = _patched_through_the_facade([probe])
    assert (patched, naming) == ({"QUEUED_LISTING_CAP", "_STATE_READ_ATTEMPTS"}, 1)
    assert _reach_gaps(patched, _owner_reads()) == [
        f"{_PACKAGE}.taskq_bridge_cancel._STATE_READ_ATTEMPTS",
        f"{_PACKAGE}.taskq_bridge_reads.QUEUED_LISTING_CAP",
    ]


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
