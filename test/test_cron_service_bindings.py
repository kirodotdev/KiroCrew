"""``CronService`` keeps every member it had, and each is bound from the module that owns it.

The service's behaviour lives in the ``kiro_crew.cron_service.service`` owner modules: each
member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The service's
state stays on the instance, so callers, ``CronService.__new__`` builds and every patch of a
member on the class or an instance see the same object they always did. ``kiro_crew.cron``
stays a plain module, and a name tests patch there is read through it on each call. This
file pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports ``kiro_crew.cron`` at import time, and at call time only as
  ``from kiro_crew import cron as seams``;
* every name the test trees patch on ``kiro_crew.cron``, and every name ``cron.py`` itself
  defines, is read by an owner through ``seams``, never as a global of its own, so a patch
  on the facade reaches the moved code as it did before the members moved;
* no ``async def`` in an owner reaches a blocking call through ``seams``
  (``seams.time.sleep``), a spelling the sync-io gate cannot see;
* each owner's docstring names the instance fields its members write and read, every owner
  logs on the ``kiro_crew.cron`` logger, and a frame in an owner is the cron surface to the
  loop-stall classifier.
"""

from __future__ import annotations

import ast
import dis
import functools
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any

import facade_mirror_scan
import pytest

import kiro_crew.cron as cron
from kiro_crew.cron import CronService
from kiro_crew.cron_service import service as service_package

# The reach checks read the whole test tree; keep them on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_cron_service_bindings")

_REPO = Path(__file__).resolve().parents[1]
_PACKAGE = service_package.__name__

#: Member -> (kind, coroutine, signature digest, owner). Frozen at the split: the kind,
#: coroutine flag and signature are BASE's. The owner is the ``kiro_crew.cron_service.service``
#: module the member's function lives in now, or, for a member the class body still defines,
#: the module (under ``kiro_crew``) its function has always come from.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "a34214912333", "cron"),
    "_ack_job_locked": ("function", False, "3e7a3fe3f9e2", "job_writes"),
    "_adopt_job_locked": ("function", False, "a32584aeae3d", "job_ownership"),
    "_alert_timeout": ("function", False, "a8bd674a6d78", "run_execution"),
    "_apply_held_run_record": ("staticmethod", False, "98caeae2438c", "run_merge"),
    "_apply_held_run_records_to": ("function", False, "ee6ebb250947", "run_merge"),
    "_apply_loop_stall_breaker": ("function", False, "f21e39ec5ffb", "stall_breaker"),
    "_arm_timer": ("function", False, "b60c5fea256e", "wake_timer"),
    "_audit_refused_removal": ("function", False, "a8d0fc85119c", "job_removal"),
    "_audit_requested_batch_removal": ("function", False, "cececffc99d5", "job_removal"),
    "_audit_requested_removal": ("function", False, "e0a80251bf41", "cron"),
    "_build_job": ("staticmethod", False, "822f1fc1a15e", "cron_service.fields"),
    "_bump_grant_epochs_for": ("function", False, "0ae10d347b25", "job_removal"),
    "_chat_folder_ids_locked": ("function", False, "67a56d539800", "job_ownership"),
    "_claim_run": ("function", False, "abf4e5d80aab", "run_registry"),
    "_claims": ("property", False, "61d4362847a8", "cron"),
    "_compute_jitter": ("staticmethod", False, "8d1f5b45e4eb", "cron_service.schedule"),
    "_delete_history_of": ("function", False, "bd77ce5a8186", "cron"),
    "_drain_pending_removals_locked": ("function", False, "6806e867af4e", "job_removal"),
    "_drop_owner_if_parent_gone": ("function", False, "73b9f40c4e1a", "job_writes"),
    "_effective_delay": ("function", False, "59162e39f6f1", "wake_timer"),
    "_enable_job_locked": ("function", False, "871892528f47", "cron"),
    "_end_run_processes": ("function", True, "57a0bf994fdd", "run_kill"),
    "_end_run_sessions": ("function", True, "92caee7be2d5", "run_kill"),
    "_execute": ("function", True, "e1b387179d10", "run_execution"),
    "_execute_with_timeout": ("function", True, "e1b387179d10", "run_execution"),
    "_fence_run_keys": ("function", False, "86e916bf9578", "run_kill"),
    "_file_lock": ("function", False, "84efc5a30403", "store_sync"),
    "_flush_held_run_records": ("function", False, "b60c5fea256e", "run_merge"),
    "_force_reap": ("function", True, "c186de9a361e", "run_teardown"),
    "_forget_store_document": ("function", False, "b60c5fea256e", "store_sync"),
    "_guard_off_event_loop": ("staticmethod", False, "6d7a93dbe237", "cron"),
    "_handles_of": ("staticmethod", False, "22fc29ddcddf", "run_kill"),
    "_hold_run_record": ("function", False, "205f7c442654", "run_merge"),
    "_is_due": ("staticmethod", False, "505a69c02ff3", "cron_service.schedule"),
    "_keep_store_document": ("function", False, "b474d2621c43", "store_sync"),
    "_load": ("function", False, "b76bacb293b4", "store_sync"),
    "_loop_stall_breaker_verdict": ("function", False, "f21e39ec5ffb", "stall_breaker"),
    "_merge_job_result": ("function", False, "73b9f40c4e1a", "run_merge"),
    "_merge_terminal_state_locked": ("function", False, "6fff88a9b9c7", "run_merge"),
    "_merge_terminal_state_with_retry": ("function", True, "a15ce612fc0c", "run_merge"),
    "_next_wake_secs": ("function", False, "c2eec8a41739", "wake_timer"),
    "_on_timer": ("function", True, "b60c5fea256e", "wake_timer"),
    "_owner_keys_locked": ("function", False, "67a56d539800", "job_ownership"),
    "_pause_for_loop_stall": ("function", False, "f7581280c3e0", "stall_breaker"),
    "_persist_add_if_absent_locked": ("function", False, "505e847311f7", "job_writes"),
    "_persist_add_locked": ("function", False, "73b9f40c4e1a", "job_writes"),
    "_quarantine_unreadable_locked": ("function", False, "d921fd8d443d", "service_lifecycle"),
    "_reaper_loop": ("function", True, "b60c5fea256e", "run_teardown"),
    "_reapply_held_run_records": ("function", False, "b60c5fea256e", "run_merge"),
    "_record_fingerprint": ("function", False, "b60c5fea256e", "store_sync"),
    "_release_children_of_removed": ("function", False, "ce71e5344713", "job_removal"),
    "_release_held_run_record": ("function", False, "2465eb865788", "run_merge"),
    "_release_jobs_owned_by_locked": ("function", False, "0eb751fdb080", "job_ownership"),
    "_remove_job_if_locked": ("function", False, "fce460c577da", "cron"),
    "_remove_job_locked": ("function", False, "3da177de389d", "cron"),
    "_remove_job_rows": ("function", False, "ce71e5344713", "job_removal"),
    "_remove_jobs_by_owner_locked": ("function", False, "c866068d019f", "job_removal"),
    "_remove_jobs_locked": ("function", False, "143dcf019388", "job_removal"),
    "_reset_and_kill_once": ("function", True, "3b055aa25104", "run_kill"),
    "_reset_fingerprint": ("function", False, "b60c5fea256e", "store_sync"),
    "_run_claimed_manual": ("function", True, "d0a5f3855125", "run_registry"),
    "_run_job_isolated": ("function", True, "3e2c7932e9d5", "run_execution"),
    "_run_session_keys": ("function", False, "abecf674945c", "run_registry"),
    "_save": ("function", False, "ce13e6013574", "store_sync"),
    "_save_held_run_records_locked": ("function", False, "b60c5fea256e", "run_merge"),
    "_saved_job_state": ("function", False, "7d3f7de35812", "run_merge"),
    "_session_process_handles": ("function", False, "21bf19694843", "run_kill"),
    "_sessions_under": ("function", False, "4006e562409e", "run_kill"),
    "_sigkill_session": ("function", True, "2bd4ceeed5eb", "run_kill"),
    "_sigkill_sessions": ("function", True, "020bda9371ba", "run_kill"),
    "_snapshot": ("staticmethod", False, "e950d87d36c5", "store_sync"),
    "_spawn_in_flight": ("function", False, "57a0bf994fdd", "run_kill"),
    "_store_extra": ("data", False, "", ""),
    "_store_version": ("data", False, "", ""),
    "_sync": ("function", False, "b60c5fea256e", "store_sync"),
    "_sync_for_write": ("function", False, "b60c5fea256e", "store_sync"),
    "_synced_snapshot": ("function", False, "74cca144766b", "store_sync"),
    "_tick_scan_locked": ("function", False, "20629d2361f6", "wake_timer"),
    "_unack_job_locked": ("function", False, "3da177de389d", "job_writes"),
    "_unknown_fields": ("data", False, "", ""),
    "_unreadable_error": ("function", False, "f5125dfea80e", "store_sync"),
    "_update_job_locked": ("function", False, "b04f778a42dc", "cron"),
    "_update_job_locked_kw": ("function", False, "189e74a8c74e", "job_writes"),
    "ack_job": ("function", False, "3e7a3fe3f9e2", "job_writes"),
    "ack_job_async": ("function", True, "3e7a3fe3f9e2", "job_writes"),
    "active_session_keys": ("function", False, "602c14087aa8", "run_registry"),
    "add_job": ("function", False, "90e3abc12fb8", "job_writes"),
    "add_job_async": ("function", True, "f5a2ec551e9e", "job_writes"),
    "add_job_if_absent": ("function", False, "43f1e87b70b1", "job_writes"),
    "add_job_if_absent_async": ("function", True, "43f1e87b70b1", "job_writes"),
    "add_managed_job": ("function", False, "f55b6bbe148d", "job_writes"),
    "add_managed_job_async": ("function", True, "f55b6bbe148d", "job_writes"),
    "adopt_job": ("function", False, "a32584aeae3d", "job_ownership"),
    "attach_run_task": ("function", False, "14f3442243cf", "run_registry"),
    "audit_one_shot_removal": ("function", False, "0f61bc996ebf", "cron"),
    "cancel": ("function", True, "3da177de389d", "run_teardown"),
    "chat_folder_ids_async": ("function", True, "67a56d539800", "job_ownership"),
    "clear_active_session_key": ("function", False, "6525c2a94d9c", "run_registry"),
    "count_enabled_from_disk": ("function", False, "fa6cfecd5574", "store_sync"),
    "create": ("classmethod", True, "44ceb0ae7bd2", "service_lifecycle"),
    "defer_removal": ("function", False, "7b49b266082c", "job_removal"),
    "discard_finished_run": ("function", False, "3da177de389d", "run_registry"),
    "enable_job": ("function", False, "871892528f47", "job_writes"),
    "enable_job_async": ("function", True, "871892528f47", "job_writes"),
    "get_history": ("function", False, "ad97cb884e09", "run_registry"),
    "get_job": ("function", False, "04e3cc96a38d", "store_sync"),
    "get_job_async": ("function", True, "04e3cc96a38d", "store_sync"),
    "is_running": ("function", False, "3da177de389d", "run_registry"),
    "list_jobs": ("function", False, "767f787d6393", "store_sync"),
    "list_jobs_async": ("function", True, "767f787d6393", "store_sync"),
    "owner_keys_async": ("function", True, "67a56d539800", "job_ownership"),
    "raise_if_store_unreadable": ("function", False, "b60c5fea256e", "store_sync"),
    "register_active_session_key": ("function", False, "6525c2a94d9c", "run_registry"),
    "release_jobs_owned_by": ("function", True, "0eb751fdb080", "job_ownership"),
    "remove_job": ("function", False, "8fea9f91e426", "job_removal"),
    "remove_job_async": ("function", True, "8fea9f91e426", "job_removal"),
    "remove_job_if_async": ("function", True, "c88830f6412b", "job_removal"),
    "remove_jobs": ("function", True, "f314181d1f02", "job_removal"),
    "remove_jobs_by_owner": ("function", True, "c866068d019f", "job_removal"),
    "remove_jobs_by_owner_sync": ("function", False, "1dda3a2e9ae6", "job_removal"),
    "remove_jobs_sync": ("function", False, "f314181d1f02", "job_removal"),
    "run_job": ("function", False, "59a56549db3e", "run_registry"),
    "running_since": ("function", False, "987a386c5e2e", "run_registry"),
    "set_refresh_callback": ("function", False, "7a2c52d9ea93", "run_registry"),
    "set_timeout_alert": ("function", False, "7a2c52d9ea93", "run_registry"),
    "start": ("function", True, "b60c5fea256e", "service_lifecycle"),
    "start_reaper": ("function", False, "bbb4b664cf76", "run_teardown"),
    "status": ("function", False, "16d9fe32838b", "store_sync"),
    "stop": ("function", True, "b60c5fea256e", "service_lifecycle"),
    "unack_job": ("function", False, "3da177de389d", "job_writes"),
    "unack_job_async": ("function", True, "3da177de389d", "job_writes"),
    "update_job": ("function", False, "b04f778a42dc", "job_writes"),
    "update_job_async": ("function", True, "b04f778a42dc", "job_writes"),
}


def _owner_names() -> list[str]:
    return sorted(
        f"{_PACKAGE}.{info.name}" for info in pkgutil.iter_modules(service_package.__path__)
    )


_OWNERS = _owner_names()


def _owners() -> list[ModuleType]:
    return [importlib.import_module(name) for name in _OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(CronService)[name]
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _home(owner: str) -> str:
    stems = {name.rsplit(".", 1)[-1] for name in _OWNERS}
    return f"{_PACKAGE}.{owner}" if owner in stems else f"kiro_crew.{owner}"


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


def _self_functions(module: ModuleType) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static or class methods owned there."""
    statics = {
        name
        for name, (kind, _, _, owner) in _MEMBERS.items()
        if kind in ("staticmethod", "classmethod") and _home(owner) == module.__name__
    }
    return [
        node
        for node in ast.parse(_source(module)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_owner_package_holds_the_owners_and_nothing_else() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values()}
    stems = {name.rsplit(".", 1)[-1] for name in _OWNERS}
    assert stems and stems <= homes
    assert not any(info.ispkg for info in pkgutil.iter_modules(service_package.__path__))


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(CronService)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(CronService)[name]
    if kind == "data":
        assert not callable(value), f"{name} is no longer plain data"
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "classmethod": isinstance(value, classmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == _home(owner)
    assert vars(sys.modules[_home(owner)]).get(name, fn) is fn


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(module):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


@pytest.mark.parametrize("owner", _OWNERS)
def test_the_class_binds_each_owner_function_in_one_block(owner: str) -> None:
    """``cron.py`` binds each owner's members under one comment header naming the owner."""
    text = _source(cron)
    header = f"``{owner}``."
    assert text.count(header) == 1, header
    block = text[text.index(header) :].split("\n    #", 1)[0]
    leaf = owner.rsplit(".", 1)[-1]
    bound = set(
        re.findall(rf"^    (\w+) = (?:(?:static|class)method\()?_{leaf}\.\1\)?$", block, re.M)
    )
    assert bound == {fn.name for fn in _self_functions(sys.modules[owner])}


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What breaks when a method body leaves its class: ``super()`` with no arguments, the
    ``__class__`` cell, and a name-mangled ``__attr`` (``__x`` without a trailing ``__``)."""
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        if isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        spelled = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(spelled, str) and spelled.startswith("__") and not spelled.endswith("__"):
            found.append(spelled)
    return found


def test_the_class_only_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    fns = _self_functions(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in fns} == {fn.name: [] for fn in fns}


def _facade_imports(tree: ast.Module) -> tuple[list[int], list[int]]:
    """``(import-time, call-time)`` lines that import ``kiro_crew.cron``, a ``TYPE_CHECKING``
    import aside. A call-time import is one inside a function; only the spelling
    ``from kiro_crew import cron as seams`` is a call-time import the owners may use."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    in_function: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            in_function.update(id(inner) for inner in ast.walk(node) if inner is not node)
    import_time: list[int] = []
    call_time: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        hit = False
        if isinstance(node, ast.ImportFrom):
            hit = node.module == "kiro_crew.cron" or (
                node.module == "kiro_crew" and any(a.name == "cron" for a in node.names)
            )
        elif isinstance(node, ast.Import):
            hit = any(a.name == "kiro_crew.cron" for a in node.names)
        if not hit:
            continue
        seam = (
            isinstance(node, ast.ImportFrom)
            and node.module == "kiro_crew"
            and [(a.name, a.asname) for a in node.names] == [("cron", "seams")]
        )
        if id(node) in in_function and seam:
            call_time.append(node.lineno)
        else:
            import_time.append(node.lineno)
    return sorted(import_time), sorted(call_time)


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.cron import CronService\n"
        "def f():\n    from kiro_crew import cron as seams\n    from kiro_crew.cron import x\n"
        "import kiro_crew.cron\n"
        "from kiro_crew import cron\n"
    )
    assert _facade_imports(tree) == ([6, 7, 8], [5])


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_imports_the_facade_but_through_its_seams(owner: str) -> None:
    import_time, _call_time = _facade_imports(ast.parse(_source(sys.modules[owner])))
    assert import_time == []


# --------------------------------------------------------------------------- #
# A patch on ``kiro_crew.cron`` reaches the moved code.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> Iterator[Path]:
    """The files pytest collects tests or fixtures from: the root conftest, ``test/``, and
    the test modules and conftests under ``src/kiro_crew/apps/builtins``."""
    yield _REPO / "conftest.py"
    yield from sorted((_REPO / "test").rglob("*.py"))
    for path in sorted((_REPO / "src" / "kiro_crew" / "apps" / "builtins").rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py":
            yield path


def _loop_names(tree: ast.Module, line: int) -> list[str] | None:
    """The names a ``for name in ("a", "b"): setattr(facade, name, ...)`` loop patches,
    for the ``setattr`` call on *line*; None when that is not the shape."""
    for loop in ast.walk(tree):
        if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
            continue
        if not isinstance(loop.iter, (ast.Tuple, ast.List, ast.Set)):
            continue
        values = [elt.value for elt in loop.iter.elts if isinstance(elt, ast.Constant)]
        if len(values) != len(loop.iter.elts) or not all(isinstance(v, str) for v in values):
            continue
        for call in ast.walk(loop):
            if (
                isinstance(call, ast.Call)
                and call.lineno == line
                and len(call.args) > 1
                and isinstance(call.args[1], ast.Name)
                and call.args[1].id == loop.target.id
            ):
                return [str(v) for v in values]
    return None


def patched_on_the_facade(source: str) -> tuple[set[str], list[int]]:
    """``(names, unresolved lines)``: every name *source* patches as an attribute of
    ``kiro_crew.cron`` (:func:`facade_mirror_scan.patch_events`), a name a loop over a
    literal tuple patches included; the lines of any other computed name."""
    events = facade_mirror_scan.patch_events(source, {cron.__name__: cron.__name__})
    names: set[str] = set()
    unresolved: list[int] = []
    tree: ast.Module | None = None
    for event in events:
        if event.module != cron.__name__ or event.name == "":
            continue
        if event.name != facade_mirror_scan._DYNAMIC:
            names.add(event.name)
            continue
        tree = tree or ast.parse(source)
        found = _loop_names(tree, event.line)
        if found is None:
            unresolved.append(event.line)
        else:
            names.update(found)
    return names, unresolved


def test_the_patch_scan_reads_every_spelling_and_resolves_a_literal_loop() -> None:
    source = (
        "from unittest import mock\n"
        "import kiro_crew.cron as cron_mod\n"
        "from kiro_crew import cron\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(cron_mod, 'a', 1)\n"
        "    monkeypatch.setattr('kiro_crew.cron.b', 1)\n"
        "    mock.patch.object(cron, 'c')\n"
        "    mock.patch('kiro_crew.cron.d')\n"
        "    for name in ('e', 'f'):\n"
        "        monkeypatch.setattr(cron_mod, name, 1)\n"
        "    monkeypatch.setattr(cron_mod, compute(), 1)\n"
    )
    names, unresolved = patched_on_the_facade(source)
    assert names == {"a", "b", "c", "d", "e", "f"}
    assert unresolved == [11]


@functools.lru_cache(maxsize=1)
def _corpus() -> tuple[set[str], list[str]]:
    """Every name the test trees patch on ``kiro_crew.cron``, read once per process on first
    use (never at import, so collecting this file costs nothing)."""
    names: set[str] = set()
    unresolved: list[str] = []
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "cron" not in text or "kiro_crew" not in text:
            continue
        found, lines = patched_on_the_facade(text)
        names |= found
        unresolved += [f"{path.relative_to(_REPO).as_posix()}:{line}" for line in lines]
    return names, unresolved


def _facade_defined() -> set[str]:
    """Every name ``cron.py`` binds by a definition of its own (not an import)."""
    return {
        name
        for name in _defined_names(ast.parse(_source(cron)))
        if not (name.startswith("__") and name.endswith("__"))
    }


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    return names


def _seams() -> set[str]:
    """The names an owner reads through the facade: tests patch them there, or cron.py
    defines them (an owner cannot import cron.py, which imports it)."""
    return (_corpus()[0] | _facade_defined()) - {"logger"}


def bare_global_reads(source: str, filename: str) -> set[str]:
    """Names the functions in *source* read as globals of their own module.

    From the compiled code: every ``LOAD_GLOBAL`` in a function, a nested one included. The
    module body's own reads (its imports, a default argument, a decorator) run once, at
    import, so they are no call-time read and are not counted.
    """
    module = compile(source, filename, "exec")
    found: set[str] = set()
    stack = [const for const in module.co_consts if isinstance(const, CodeType)]
    while stack:
        code = stack.pop()
        for instruction in dis.get_instructions(code):
            if instruction.opname == "LOAD_GLOBAL":
                found.add(instruction.argval)
        stack += [const for const in code.co_consts if isinstance(const, CodeType)]
    return found


def seam_reads(source: str) -> set[str]:
    """Every ``seams.<name>`` *source* reads."""
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "seams"
    }


def test_the_read_scans_can_fail() -> None:
    source = (
        "import time\n"
        "LIMIT = 3\n"
        "def f(self, deadline=LIMIT):\n"
        "    from kiro_crew import cron as seams\n"
        "    def inner():\n        return time.time()\n"
        "    return seams.sel, [time for _ in ()], inner\n"
    )
    assert bare_global_reads(source, "<t>") == {"time"}
    assert seam_reads(source) == {"sel"}


def test_the_corpus_saw_the_patches_it_must_route() -> None:
    names, unresolved = _corpus()
    assert unresolved == [], "a computed patch target on kiro_crew.cron: resolve it here"
    # Non-vacuity: names the tests are known to patch there, by every spelling.
    assert {"admission_check", "_REAPER_RESET_TIMEOUT", "time", "_vet_allowance"} <= names


@pytest.mark.parametrize("owner", _OWNERS)
def test_an_owner_reads_every_facade_name_through_the_facade(owner: str) -> None:
    module = sys.modules[owner]
    source = _source(module)
    bare = bare_global_reads(source, module.__file__ or owner) & _seams()
    assert sorted(bare) == [], f"{owner} reads these as its own globals, past a patch on cron"
    assert sorted(seam_reads(source) - _seams()) == [], f"{owner} reads these through cron"


def test_the_seams_the_owners_read_are_the_ones_the_facade_holds() -> None:
    read = set().union(*(seam_reads(_source(m)) for m in _owners()))
    assert read and read <= _seams()
    # A name the owners read through the facade that it does not bind is an AttributeError.
    assert sorted(name for name in read if not hasattr(cron, name)) == []


@pytest.mark.parametrize("owner", _OWNERS)
def test_a_seam_import_opens_the_function_that_reads_a_seam(owner: str) -> None:
    """``seams`` is bound by the function's first statement after its docstring, so no
    line of a moved body runs before the facade is in hand."""
    for fn in ast.parse(_source(sys.modules[owner])).body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        uses = any(isinstance(n, ast.Name) and n.id == "seams" for n in ast.walk(fn))
        body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
        opens = bool(body) and ast.unparse(body[0]) == "from kiro_crew import cron as seams"
        assert uses == opens, fn.name


def _sync_io_gate() -> Any:
    """``scripts/check_sync_io_in_async.py``, loaded the way its own tests load it."""
    spec = importlib.util.spec_from_file_location(
        "check_sync_io_in_async", _REPO / "scripts" / "check_sync_io_in_async.py"
    )
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def blocking_seam_calls(source: str, blocking: dict[str, dict[str, str]]) -> list[str]:
    """``seams.<module>.<call>(...)`` inside an ``async def`` for each blocking call the sync-io
    gate knows (``blocking`` is its module -> attribute table). The gate resolves a bare module
    name, so a call spelled through ``seams`` is invisible to it; this reads that spelling."""
    found: list[str] = []

    def visit(node: ast.AST, in_async: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                visit(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                visit(child, False)
            else:
                func = getattr(child, "func", None) if isinstance(child, ast.Call) else None
                if (
                    in_async
                    and isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "seams"
                    and func.attr in blocking.get(func.value.attr, {})
                ):
                    found.append(f"line {child.lineno}: seams.{func.value.attr}.{func.attr}")
                visit(child, in_async)

    visit(ast.parse(source), False)
    return found


def test_the_blocking_seam_scan_can_fail() -> None:
    blocking = _sync_io_gate().MODULE_FUNCS
    source = (
        "async def f(self):\n"
        "    from kiro_crew import cron as seams\n"
        "    seams.time.sleep(1)\n"
        "    seams.time.time()\n"
        "    def inner():\n        seams.time.sleep(2)\n"
        "def g():\n    seams.subprocess.run(['x'])\n"
    )
    assert blocking_seam_calls(source, blocking) == ["line 3: seams.time.sleep"]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_async_member_blocks_through_its_seams(owner: str) -> None:
    """A blocking call reached through ``seams`` would park the event loop, and the sync-io gate
    cannot see one spelled that way."""
    blocking = _sync_io_gate().MODULE_FUNCS
    assert {"time", "os", "subprocess", "socket"} <= set(blocking)
    assert blocking_seam_calls(_source(sys.modules[owner]), blocking) == []


# --------------------------------------------------------------------------- #
# Docs, logging and attribution.
# --------------------------------------------------------------------------- #


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset(
    {
        "add",
        "discard",
        "append",
        "pop",
        "update",
        "clear",
        "setdefault",
        "remove",
        "extend",
        "insert",
        "popitem",
    }
)


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the attribute,
    of a subscript or of an attribute on it, and a mutator call on it. Reads: every other
    load."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _MUTATORS:
                    target = _self_field(node.func.value)
                    if target is not None:
                        written.add(target)
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    self.d.x = 2\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n"
        "    return self.h, self.e, self._save()\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f"}, {"h"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(module))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


def test_the_package_lists_every_owner() -> None:
    doc = service_package.__doc__ or ""
    assert {name for name in _OWNERS if f":mod:`~{name}`" not in doc} == set()


@pytest.mark.parametrize("owner", _OWNERS)
def test_owners_log_on_the_cron_channel(owner: str) -> None:
    """Operators filter, and tests ``caplog``, on ``kiro_crew.cron``."""
    assert getattr(sys.modules[owner], "logger", cron.logger) is cron.logger


@pytest.mark.parametrize("owner", _OWNERS)
def test_a_stall_inside_an_owner_is_the_cron_surface(owner: str) -> None:
    """The loop-stall classifier names a stall in moved service code ``cron``, as it did
    when the code was in ``cron.py``: the frame's path is under ``cron_service/``."""
    from kiro_crew.stall_attribution import classify_surface, parse_frames

    leaf = owner.rsplit(".", 1)[-1]
    dashboard = '  File "/venv/site-packages/kiro_crew/dashboard/handlers/cron.py", line 1 in api'
    for frame in (
        f'  File "/venv/site-packages/kiro_crew/cron_service/service/{leaf}.py", line 9 in f',
        rf'  File "C:\venv\Lib\site-packages\kiro_crew\cron_service\service\{leaf}.py", line 9 in f',
    ):
        assert classify_surface(parse_frames([frame, dashboard])) == "cron", frame
