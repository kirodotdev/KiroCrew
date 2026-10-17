"""``SessionAllocationService`` keeps every member it had, each bound from the module that owns it.

The class's two largest member groups live in ``kiro_crew.session_allocation_parts``: each
member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``), so tests,
``SessionAllocationService.__new__`` builds and ``SessionManager``'s delegates see the
same class they always did. This file pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time;
* each owner's docstring names the instance fields its members write and read;
* the refusals keep naming ``kiro_crew.session_allocation`` as their module;
* the names the facade serves from its owners are the ones its ``TYPE_CHECKING`` manifest
  imports, each the object its home module holds;
* a write through ``kiro_crew.session_allocation`` reaches every owner that reads the name,
  for every name the test trees patch there (the facade's mirror, ``_facade_mirror``).
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiro_crew import session_allocation as facade
from kiro_crew.session_allocation import SessionAllocationService

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_allocation_bindings")

#: Owner label -> module. ``facade`` is ``kiro_crew.session_allocation`` itself.
_HOMES: dict[str, str] = {
    "facade": "kiro_crew.session_allocation",
    "companions": "kiro_crew.session_allocation_parts.companions",
    "get_or_create": "kiro_crew.session_allocation_parts.get_or_create",
}

#: Member -> (kind, coroutine, signature digest, owner). Frozen at the split: the kind,
#: coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "398c8297cf1c", "facade"),
    "_allocation_reservations": ("property", False, "32c2221e31f8", "facade"),
    "_append_companion_runtime_rows": ("function", False, "d30b1c0d706e", "companions"),
    "_closing": ("property", False, "a6632a57e602", "facade"),
    "_continuable_fallback": ("property", False, "bc1a086eaf85", "facade"),
    "_continuable_keys": ("property", False, "67a56d539800", "facade"),
    "_crew_pins_effort": ("function", True, "24de81494340", "get_or_create"),
    "_dispatch_hard_kill": ("function", False, "06546ba55a30", "get_or_create"),
    "_draining_subagent_runtimes": ("property", False, "0624c13b3dee", "facade"),
    "_evict_stale_session": ("function", True, "a6707a152144", "get_or_create"),
    "_fold_key": ("function", False, "f1d40e624ae4", "facade"),
    "_generation_key": ("function", False, "f1d40e624ae4", "facade"),
    "_get_or_bootstrap_run_runtime": ("function", True, "dc9735619fc4", "companions"),
    "_get_or_create_impl": ("function", True, "746de0f75f44", "get_or_create"),
    "_get_session_agent": ("function", False, "48f8c3736da9", "companions"),
    "_held_start_permit": ("function", False, "00575ac64dc4", "get_or_create"),
    "_inbound_callback_reservations": ("property", False, "71f690cdd0d7", "facade"),
    "_install_work_dir_claim_probe": ("function", False, "f7613dfa9df7", "facade"),
    "_is_continuable_key": ("function", False, "bcd6de7f056b", "facade"),
    "_is_member_key": ("staticmethod", False, "6721e663a426", "get_or_create"),
    "_lock": ("property", False, "fd738ff11600", "facade"),
    "_ownership_generations": ("property", False, "d8be45664668", "facade"),
    "_parent_aws_profile": ("function", False, "410e4e9178a1", "companions"),
    "_parent_runtime_kwargs": ("function", False, "f408fa261a2b", "companions"),
    "_reacquire_and_validate": ("function", True, "c69e8f69c8aa", "get_or_create"),
    "_refuse_if_ending": ("function", False, "4fa619d0b3bc", "facade"),
    "_release_reacquired": ("function", False, "a6707a152144", "get_or_create"),
    "_remember_capability_failure": ("function", False, "6efcfdf79280", "get_or_create"),
    "_remove_reservation_cancellation_drained": ("function", True, "d02e096bed1e", "get_or_create"),
    "_remove_reservation_now": ("function", False, "d02e096bed1e", "get_or_create"),
    "_resolve_agent_model": ("function", False, "e7a565bb1ba0", "facade"),
    "_runtime_pid": ("staticmethod", False, "9a018c130ad5", "companions"),
    "_safe_cleanup": ("function", True, "76a7c030508f", "facade"),
    "_sessions": ("property", False, "16d9fe32838b", "facade"),
    "_start_sem": ("property", False, "1f1197566c49", "facade"),
    "_starting_pids": ("property", False, "d657ba783a38", "facade"),
    "_subagent_runtime_locks": ("property", False, "ac58df2ab69b", "facade"),
    "_subagent_runtimes": ("property", False, "16d9fe32838b", "facade"),
    "active_providers": ("function", False, "6d42d4fe7cc2", "facade"),
    "advance_ownership_generation": ("function", False, "028659ce42ac", "facade"),
    "allocation_predecessor": ("function", False, "f1d40e624ae4", "facade"),
    "any_active_turn": ("function", False, "a6632a57e602", "facade"),
    "aws_profile_differs_from_parent": ("function", False, "76593e568eac", "companions"),
    "aws_profile_split_possible": ("function", False, "3780bd272b7a", "companions"),
    "begin_ending": ("function", False, "958babeb61c8", "facade"),
    "begin_turn": ("function", False, "958babeb61c8", "facade"),
    "cancel_queued": ("function", False, "50fe52427d8c", "facade"),
    "capability_runtime_view": ("function", False, "de139dca2f5d", "facade"),
    "clear_queue": ("function", False, "1c7c5164c922", "facade"),
    "conversation_provider": ("function", False, "f1d40e624ae4", "facade"),
    "dequeue": ("function", False, "99f1d34018d7", "facade"),
    "detach_queue": ("function", False, "197b82a5019f", "facade"),
    "end_ending": ("function", False, "958babeb61c8", "facade"),
    "enqueue": ("function", False, "d3c6da093136", "facade"),
    "forget_conversation": ("function", False, "4b576121a346", "facade"),
    "forget_conversation_if_sid": ("function", False, "70e3d61c5764", "facade"),
    "get_agent": ("function", False, "f1d40e624ae4", "facade"),
    "get_agent_selection": ("function", False, "b3496bff0391", "facade"),
    "get_approval_policy": ("function", False, "f1d40e624ae4", "facade"),
    "get_or_create": ("function", True, "c9826b33d6da", "get_or_create"),
    "get_pid": ("function", False, "1290edeed1b6", "facade"),
    "get_provider": ("function", False, "c66f77f7d253", "facade"),
    "get_subagent_runtime": ("function", True, "23c9947d4e41", "companions"),
    "has_allocation_reservation": ("function", False, "1eda252b53bd", "facade"),
    "has_session": ("function", False, "1eda252b53bd", "facade"),
    "inbound_callback_count": ("property", False, "fa6cfecd5574", "facade"),
    "is_busy": ("function", False, "1eda252b53bd", "facade"),
    "is_cancelled": ("function", False, "50fe52427d8c", "facade"),
    "is_continuable": ("function", False, "1eda252b53bd", "facade"),
    "is_provider_alive": ("function", True, "5ccb54f56604", "facade"),
    "is_session_sharing_eligible": ("function", False, "3780bd272b7a", "companions"),
    "mapped_session_keys": ("function", False, "602c14087aa8", "facade"),
    "mapped_sid": ("function", False, "f1d40e624ae4", "facade"),
    "mark_continuable": ("function", False, "958babeb61c8", "facade"),
    "open_task_session": ("function", True, "79d5142a16be", "facade"),
    "project_aws_profile": ("function", False, "cb2074e9df23", "companions"),
    "record_failure": ("function", True, "1eda252b53bd", "facade"),
    "record_success": ("function", False, "958babeb61c8", "facade"),
    "release": ("function", False, "b35cc323b390", "facade"),
    "release_subagent_runtime": ("function", True, "0f7ee42764e5", "companions"),
    "reserve_inbound_callback": ("function", False, "b58e9cc18136", "facade"),
    "restore_queue": ("function", False, "e233ef2976f2", "facade"),
    "resumable_hint": ("function", False, "1eda252b53bd", "facade"),
    "resumable_sid": ("function", False, "4b576121a346", "facade"),
    "runtime_pids": ("function", False, "2468fe0fd6a3", "companions"),
    "seed_conversation": ("function", False, "7b9b418f95e7", "facade"),
    "session_generation": ("function", False, "028659ce42ac", "facade"),
    "session_keys": ("function", False, "602c14087aa8", "facade"),
    "set_approval_policy": ("function", False, "33c822b68141", "facade"),
    "set_continuable_fallback": ("function", False, "b57343683ce0", "facade"),
    "spawn_in_flight": ("function", False, "4b576121a346", "facade"),
    "touch": ("function", False, "1eda252b53bd", "facade"),
    "try_acquire": ("function", True, "1eda252b53bd", "facade"),
    "unmark_continuable": ("function", False, "958babeb61c8", "facade"),
    "wait_for_ending_fence": ("function", True, "aad032b61643", "facade"),
}


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in facade._ALLOCATION_OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(SessionAllocationService)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module, owner: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and _HOMES[home] == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(SessionAllocationService)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(SessionAllocationService)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == _HOMES[owner]
    if owner != "facade":
        assert vars(sys.modules[_HOMES[owner]])[name] is fn


def test_every_owner_holding_members_is_listed_in_the_facade() -> None:
    homes = {_HOMES[owner] for _k, _c, _d, owner in _MEMBERS.values() if owner != "facade"}
    assert homes <= set(facade._ALLOCATION_OWNERS)


@pytest.mark.parametrize("owner", facade._ALLOCATION_OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


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
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert sorted(_class_only_constructs(node)) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", facade._ALLOCATION_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    functions = _self_functions(_tree(sys.modules[owner]), owner)
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines importing ``kiro_crew.session_allocation`` outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == facade.__name__ or (
                node.module == "kiro_crew"
                and any(a.name == "session_allocation" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == facade.__name__ for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.session_allocation import X\n"
        "def f():\n    from kiro_crew.session_allocation import y\n"
        "import kiro_crew.session_allocation\n"
        "from kiro_crew import session_allocation\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize(
    "owner", ("kiro_crew.session_allocation_parts", *facade._ALLOCATION_OWNERS)
)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(importlib.import_module(owner))) == []


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _methods() -> frozenset[str]:
    """Class members that are behaviour, not state: every function and static method. A
    property is one of the registry's fields, so an owner reading it reads that field."""
    return frozenset(name for name, (kind, _c, _d, _o) in _MEMBERS.items() if kind != "property")


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                target = _self_field(func.value)
                if target is not None:
                    written.add(target)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                (written if func.id == "setattr" else read).add(node.args[1].value)
    methods = _methods()
    return written - methods, read - written - methods


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    self.get_or_create(k)\n    self._sessions.pop(k)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e, self._lock\n"
    )
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert _fields([node]) == (
        {"a", "b", "c", "d", "e", "f", "g", "_sessions"},
        {"h", "i", "j", "_lock"},
    )


def _listed(doc: str, label: str) -> set[str]:
    import re

    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize(
    "owner",
    [
        "kiro_crew.session_allocation_parts.companions",
        "kiro_crew.session_allocation_parts.get_or_create",
    ],
)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    assert written and read, f"{owner}: the scan found no field, so it measured nothing"
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


#: The refusals the allocation boundary raises, defined in the ``errors`` owner.
_REFUSALS = (
    "SessionBusyError",
    "SessionClosingError",
    "SessionEndingError",
    "SpeculativeResumeRefused",
)


@pytest.mark.parametrize("name", _REFUSALS)
def test_a_moved_refusal_keeps_the_facade_as_its_module(name: str) -> None:
    """Error chains and tracebacks render a class as ``module.qualname``; a refusal that
    moved to an owner keeps naming the path callers import it from."""
    errors = sys.modules["kiro_crew.session_allocation_parts.errors"]
    cls = vars(errors)[name]
    assert getattr(facade, name) is cls
    assert cls.__module__ == facade.__name__


def test_every_class_an_owner_defines_is_a_listed_refusal() -> None:
    defined = {
        node.name
        for module in _owners()
        for node in _tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert defined == set(_REFUSALS)


def test_a_refusal_names_the_facade_in_an_error_chain() -> None:
    from kiro_crew.subagent import _describe_exception

    try:
        try:
            raise facade.SessionClosingError("manager is closing")
        except facade.SessionClosingError as exc:
            raise RuntimeError("turn refused") from exc
    except RuntimeError as outer:
        text = _describe_exception(outer)
    assert text == (
        "RuntimeError: turn refused <- caused by "
        "kiro_crew.session_allocation.SessionClosingError: manager is closing"
    )


def _type_checking_manifest() -> dict[str, str]:
    """Name -> module, for each name the facade's ``TYPE_CHECKING`` blocks import that the
    facade does not bind at run time: the names it serves, as type checkers see them."""
    manifest: dict[str, str] = {}
    for node in _tree(facade).body:
        if not (isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"):
            continue
        for sub in node.body:
            if isinstance(sub, ast.ImportFrom) and sub.module:
                manifest.update(
                    {a.name: sub.module for a in sub.names if a.name not in vars(facade)}
                )
    return manifest


def test_the_facade_serves_exactly_its_type_checking_manifest() -> None:
    manifest = _type_checking_manifest()
    assert set(manifest) == set(facade._SERVED_NAMES) == set(facade._EXPORTS)
    for name, home in manifest.items():
        assert name not in vars(facade), f"{name} is served, so the facade must not bind it"
        assert getattr(facade, name) is getattr(importlib.import_module(home), name), name


def test_a_served_name_is_listed_and_star_exported() -> None:
    public = {name for name in facade._SERVED_NAMES if not name.startswith("_")}
    assert public <= set(facade.__all__)
    assert public <= set(dir(facade))


#: The test tree the patch scan reads; a test edit there can change what this file checks.
_TEST_TREE = Path(__file__).resolve().parents[1] / "test"

#: The patches the test trees make through the facade at the split, and where they are.
_KNOWN_PATCHES = {
    "ENDING_FENCE_WAIT_SECS": "test_session.py",
    "record_session_started": "test_compaction_stop_declined.py",
    "resolve_project_profile": "test_aws_project_profiles.py",
    "wake_other_drains": "test_compaction_stop_declined.py",
}


def _patched_through_the_facade() -> set[str]:
    import test_facade_mirror_guards as guards

    return guards.facade_patched_names(facade.__name__)


def test_every_name_patched_through_the_facade_reaches_every_owner_reading_it() -> None:
    import test_facade_mirror_guards as guards

    patched = _patched_through_the_facade()
    # The patches the test trees make at the split, so the scan is seen to measure.
    for name, where in _KNOWN_PATCHES.items():
        assert name in (_TEST_TREE / where).read_text(encoding="utf-8"), (name, where)
    assert set(_KNOWN_PATCHES) <= patched
    index = facade._MIRROR
    assert guards.unrouted_patches(index, patched) == []
    assert guards.patch_reach_failures(facade, sorted(patched)) == []


def test_the_reach_check_can_fail() -> None:
    import dataclasses

    import test_facade_mirror_guards as guards

    index = facade._MIRROR
    owner = "kiro_crew.session_allocation_parts.get_or_create"
    holders = dict(index.holders)
    holders["record_session_started"] = tuple(
        h for h in holders["record_session_started"] if h != owner
    )
    blind = dataclasses.replace(index, holders=holders)
    sources = {
        name: Path(importlib.import_module(name).__file__ or "").read_text(encoding="utf-8")
        for name in (index.facade, *index.owners)
    }
    assert f"{owner} reads record_session_started without binding it" in (
        guards.unreached_readers(blind, sources)
    )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    import source_corpus

    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve()
    assert this_file.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
