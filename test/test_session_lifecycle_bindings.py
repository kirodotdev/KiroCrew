"""``SessionLifecycleService`` keeps every member it had, and each is bound from the module that owns it.

The lifecycle service's behaviour lives in the ``kiro_crew.session_lifecycle_parts`` owner
modules: each member's body is a module-level function there whose first parameter is
``self``, and the class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``).
The service state stays on the instance, so ``SessionManager``'s delegation, instance patches
of a member and the facade's call-time dependencies see the same object they always did. The
module-level stop-decline and parked-queue records moved to owners too, and the facade still
binds every module-level name it bound before the split. This file pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and each imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* the moved module helpers log under the facade's logger, and a class moved to an owner
  keeps the facade as its ``__module__``;
* the facade's namespace and ``__all__`` are BASE's, and a read through the facade sees
  the live value of a counter its owner rebinds;
* a write through ``kiro_crew.session_lifecycle`` reaches every owner that reads the name, for
  every name the test trees patch there (the facade's mirror, ``_facade_mirror``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
import typing
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import session_lifecycle
from kiro_crew.session_lifecycle import SessionLifecycleService

pytestmark = pytest.mark.xdist_group(name="tree_scan_session_lifecycle_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now
#: (``session_lifecycle`` for the class body itself).
_MEMBERS: dict[str, tuple[str, bool, str, str]] = {
    "__init__": ("function", False, "eab60089ec20", "session_lifecycle"),
    "_calling_task": ("staticmethod", False, "aa08d43876aa", "turn_stop"),
    "_cancel_parent_children": ("function", True, "c38136ad1de2", "parent_end"),
    "_child_teardown": ("property", False, "5d305fdfa44b", "session_lifecycle"),
    "_discard_replay_gap": ("function", False, "8964ba011f68", "turn_stop"),
    "_eager_respawn": ("function", True, "958babeb61c8", "turn_stop"),
    "_fire_recycle_callback": ("function", True, "d9b2aabefef9", "session_lifecycle"),
    "_identity_sweep_lock": ("property", False, "fd738ff11600", "session_lifecycle"),
    "_on_recycled": ("property", False, "67238bb9d853", "session_lifecycle"),
    "_open_teardown_fence": ("function", False, "63ea8acfe723", "parent_end"),
    "_origin_links": ("property", False, "16d9fe32838b", "session_lifecycle"),
    "_orphan_turn_holder": ("function", False, "a6707a152144", "turn_stop"),
    "_parent_has_sparable_child": ("function", True, "1eda252b53bd", "parent_end"),
    "_parent_owes_child_work": ("function", True, "1eda252b53bd", "parent_end"),
    "_parent_owes_child_work_now": ("function", False, "1eda252b53bd", "parent_end"),
    "_park_reset_queue": ("function", False, "9c24f37ef853", "teardown"),
    "_recycling": ("property", False, "fddda4965647", "session_lifecycle"),
    "_release_turn_ceiling": ("function", False, "3d48f535f3ee", "teardown"),
    "_respawn_as": ("function", True, "a068e8318381", "turn_stop"),
    "_retire_kiro_bg_runtime": ("function", True, "cf7d5d61b0c1", "identity_retirement"),
    "_retire_kiro_subagent_runtimes": ("function", True, "914a005c3545", "identity_retirement"),
    "_send_abort_for_session": ("function", True, "a6707a152144", "turn_stop"),
    "_snapshot_parent_children": ("function", False, "4be47402b13f", "parent_end"),
    "_stop_bucket": ("function", False, "f1d40e624ae4", "turn_stop"),
    "_suppress_replay": ("property", False, "67a56d539800", "session_lifecycle"),
    "_wake_turn_waiters": ("staticmethod", False, "09f69d038204", "turn_stop"),
    "absorb_orphaned_release": ("function", False, "1eda252b53bd", "turn_stop"),
    "adopt_turn": ("function", False, "958babeb61c8", "turn_stop"),
    "await_replay_gap": ("function", True, "958babeb61c8", "turn_stop"),
    "cancel_current": ("function", True, "9859e2116ea5", "turn_stop"),
    "cancel_owned_turn": ("function", True, "86bfc9daa5fd", "turn_stop"),
    "close_all": ("function", True, "9679ccba1fac", "fleet"),
    "close_replay_gap": ("function", False, "958babeb61c8", "turn_stop"),
    "destroy": ("function", True, "90a6b2cda311", "teardown"),
    "destroy_if": ("function", True, "b2e16ab8f2dd", "teardown"),
    "discard_conversation": ("function", True, "b225e6bcbd95", "teardown"),
    "drain_active_turns": ("function", True, "5944c6af44b2", "fleet"),
    "drain_all_providers": ("function", True, "0624c13b3dee", "fleet"),
    "end_children_for": ("function", True, "958babeb61c8", "parent_end"),
    "flag_identity_stamp_mismatches": ("function", True, "946212e80718", "identity_retirement"),
    "note_stop": ("function", False, "1eda252b53bd", "turn_stop"),
    "open_replay_gap": ("function", False, "958babeb61c8", "turn_stop"),
    "refresh_defaults": ("function", True, "8d9c819b471f", "fleet"),
    "reload_provider_factory": ("function", True, "8d9c819b471f", "fleet"),
    "remove": ("function", True, "958babeb61c8", "teardown"),
    "remove_if_unclaimed": ("function", True, "1eda252b53bd", "teardown"),
    "reset": ("function", True, "44042efb8303", "teardown"),
    "retire_kiro_identity_sessions": ("function", True, "0929bcd2b0e5", "identity_retirement"),
    "set_child_teardown_handler": ("function", False, "fdd1c8c5bbb9", "parent_end"),
    "set_recycle_callback": ("function", False, "eb879633e29d", "session_lifecycle"),
    "stop_generation": ("function", False, "028659ce42ac", "turn_stop"),
    "stop_turn": ("function", True, "67ebfe2370a5", "turn_stop"),
    "teardown_scope": ("function", False, "2b51a9463afa", "session_lifecycle"),
    "tearing_down": ("function", False, "8f783ea149c8", "session_lifecycle"),
}

#: Every name the module bound at its top level at the split, imports included. The facade
#: keeps binding each of them, except the counters its owners rebind (``_SERVED``).
_BASE_NAMESPACE = frozenset("""
    _ANY_SESSION _BackgroundRuntime _CHILD_CANCEL_TIMEOUT_SECS _ChildTeardownHandler
    _compaction_waits_for_turn _count_stop_declined_drop _drop_parked _END_REASON_SID_RETAINED
    _entry_within_bounds _folded _has_session _logger _park_queue _parked_dropped _parked_for
    _parked_queue _parked_total _RecycleCallback _ReplayGap _SessionEntry _SessionMapPort
    _SID_RETENTION_UNKNOWN _stop_declined_dropped _stop_declined_key _stop_declined_markers
    _TeardownScope _turn_in_flight AbstractSet adopt_parked_queue allocation_identity Any
    asyncio Awaitable Callable CancelOutcome canonical_key clear_stop_declined
    compaction_in_flight consume_stop_declined dataclass decline_stop END_REASON_DESTROYED
    END_REASON_DISCARDED END_REASON_REMOVED END_REASON_RESET END_REASON_RETIRED
    END_REASON_SHUTDOWN END_REASON_UNCLAIMED entry_channel Executor field
    force_stop_keeping_others hand_queue_to_successor identity_component_dropped
    identity_park_grace_remaining identity_stamp_mismatch Literal logging mark_identity_parked
    MutableMapping note_stop_declined OrderedDict PARKED_ENTRY_KWARGS_MAX_CHARS
    PARKED_ENTRY_TEXT_MAX_CHARS PARKED_ENTRY_TS_MAX_CHARS PARKED_QUEUE_MAX
    PARKED_QUEUE_TOTAL_MAX pool_kiro_agent PrioritySemaphore process_handle_of ProcessHandle
    Protocol ProviderFactory record_session_ended record_sessions_ended Sequence
    SessionLifecycleConstants SessionLifecycleDeps SessionLifecycleOwner SessionLifecycleService
    SessionLifecycleState spawn_identity_of spawned_under STOP_DECLINED_COMPACTING_TEXT
    STOP_DECLINED_ESCALATION_SECS STOP_DECLINED_KEY_MAX_CHARS STOP_DECLINED_MARKERS_MAX
    StopOutcome take_parked_queue time TornDown turn_ceiling wake_other_drains
    """.split())

#: The module's explicit star-import list, kept exactly as it was.
_BASE_ALL = [
    "CancelOutcome",
    "SessionLifecycleConstants",
    "SessionLifecycleDeps",
    "SessionLifecycleService",
    "SessionLifecycleState",
    "StopOutcome",
]

_PACKAGE = "kiro_crew.session_lifecycle_parts"
_STOP_DECLINE = f"{_PACKAGE}.stop_decline"
_PARKED_QUEUE = f"{_PACKAGE}.parked_queue"


def _module_of(owner: str) -> str:
    return session_lifecycle.__name__ if owner == "session_lifecycle" else f"{_PACKAGE}.{owner}"


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in session_lifecycle._OWNERS]


#: A default object's address in its repr: lowercase on POSIX, zero-padded uppercase on Windows.
_ADDRESS = re.compile(r" at 0x[0-9A-Fa-f]+")


def _signature_digest(fn: Any) -> str:
    """The signature's digest, ``self``'s annotation and default objects' addresses aside."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    text = _ADDRESS.sub("", str(sig.replace(parameters=params)))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_the_signature_digest_drops_an_address_in_either_spelling() -> None:
    posix, windows = "<object object at 0x7f199a57a970>", "<object object at 0x000001A9BFA701B0>"
    assert _ADDRESS.sub("", posix) == _ADDRESS.sub("", windows) == "<object object>"


def _member_function(name: str) -> Any:
    value = vars(SessionLifecycleService)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The module-level functions of *tree* that a class binds: a first parameter ``self``, or
    one the inventory records as a static method of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and _module_of(home) == owner
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
        for name in vars(SessionLifecycleService)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []
    assert not SessionLifecycleService.__dict__.get("__annotations__")


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(SessionLifecycleService)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == _module_of(owner)
    if owner != "session_lifecycle":
        assert vars(sys.modules[_module_of(owner)])[name] is fn


def test_every_state_property_keeps_its_setter() -> None:
    properties = {n for n, (kind, *_rest) in _MEMBERS.items() if kind == "property"}
    assert properties == {
        "_child_teardown",
        "_identity_sweep_lock",
        "_on_recycled",
        "_origin_links",
        "_recycling",
        "_suppress_replay",
    }
    assert all(vars(SessionLifecycleService)[n].fset is not None for n in properties)


def test_the_service_keeps_an_instance_dict_for_instance_patches() -> None:
    """``SessionManager`` callers and tests assign members on the service instance."""
    assert "__slots__" not in vars(SessionLifecycleService)
    service = SessionLifecycleService(
        MagicMock(), MagicMock(), session_lifecycle.SessionLifecycleState()
    )
    sentinel = MagicMock(name="abort")
    service._send_abort_for_session = sentinel
    assert service._send_abort_for_session is sentinel


def test_every_owner_is_listed_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner != "session_lifecycle"}
    listed = {name.rsplit(".", 1)[-1] for name in session_lifecycle._OWNERS}
    assert homes <= listed
    assert listed - homes == {"stop_decline", "parked_queue"}, "the module-level owners"


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What only works inside a class body: zero-argument ``super()``, the implicit
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


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: _module_of(home)
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod"
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "kiro_crew.session_lifecycle" or (
                node.module == "kiro_crew"
                and any(a.name == "session_lifecycle" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == "kiro_crew.session_lifecycle" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.session_lifecycle import SessionLifecycleService\n"
        "def f():\n    from kiro_crew.session_lifecycle import x\n"
        "import kiro_crew.session_lifecycle\n"
        "from kiro_crew import session_lifecycle\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}."):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(f"{_PACKAGE}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(f"{_PACKAGE}."))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.session_lifecycle_parts.teardown import X\n"
        "from kiro_crew.session_lifecycle_parts import fleet as _f\n"
        "import kiro_crew.session_lifecycle_parts.turn_stop\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.session_lifecycle_parts.teardown",
        "kiro_crew.session_lifecycle_parts.fleet",
        "kiro_crew.session_lifecycle_parts.turn_stop",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """Owners form one layering: an owner reads names only from owners earlier in the
    tuple, so the order is the import order."""
    owners = session_lifecycle._OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset({"add", "discard", "append", "pop", "update", "clear", "setdefault"})


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """``(written, read)`` instance fields of *fns*. Writes: a store or delete of the
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
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    assert _fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


# --------------------------------------------------------------------------- #
# Loggers, moved classes, the facade's namespace
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_owners_log_under_the_facade_logger(owner: str) -> None:
    """The moved module helpers log as ``kiro_crew.session_lifecycle``; the members log
    through the facade's ``self._deps.logger`` and bind no logger of their own."""
    module = sys.modules[owner]
    assert getattr(module, "_logger", session_lifecycle._logger) is session_lifecycle._logger
    assert session_lifecycle._logger.name == session_lifecycle.__name__
    assert "logger" not in vars(module)


@pytest.mark.parametrize("owner", session_lifecycle._OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """A class moved to an owner keeps naming the path callers import it from."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: session_lifecycle.__name__ for name in defined
    }


def test_the_moved_classes_and_outcome_aliases_are_the_ones_listed() -> None:
    """``_ReplayGap`` is the only class an owner defines; the two outcome names are
    ``typing.Literal`` aliases, whose ``__module__`` is ``typing`` wherever they are made."""
    defined = {
        f"{module.__name__}.{node.name}"
        for module in _owners()
        for node in _tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert defined == {f"{_PACKAGE}.turn_stop._ReplayGap"}
    assert session_lifecycle._ReplayGap.__module__ == session_lifecycle.__name__
    assert typing.get_args(session_lifecycle.CancelOutcome) == (
        "acked",
        "timeout",
        "no_turn",
        "error",
    )
    assert typing.get_args(session_lifecycle.StopOutcome) == ("soft", "hard", "idle", "compacting")
    assert (
        session_lifecycle.CancelOutcome.__module__
        == session_lifecycle.StopOutcome.__module__
        == "typing"
    )


def test_the_facade_binds_every_name_it_bound_at_the_split() -> None:
    served = set(session_lifecycle._SERVED)
    assert served == {"_parked_dropped", "_stop_declined_dropped"}
    assert sorted(_BASE_NAMESPACE - served - set(vars(session_lifecycle))) == []
    assert sorted(n for n in _BASE_NAMESPACE if not hasattr(session_lifecycle, n)) == []
    assert not served & set(vars(session_lifecycle))


def test_the_star_import_list_is_the_one_the_module_declared() -> None:
    assert session_lifecycle.__all__ == _BASE_ALL


@pytest.mark.parametrize(
    ("counter", "owner"),
    [("_stop_declined_dropped", _STOP_DECLINE), ("_parked_dropped", _PARKED_QUEUE)],
)
def test_a_read_through_the_facade_sees_the_counter_its_owner_rebinds(
    counter: str, owner: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The drop counters are rebound by ``global`` in their owner, and logged under the
    facade's logger name, so a ``caplog`` keyed on ``session_lifecycle.__name__`` sees them."""
    module = sys.modules[owner]
    # Through the facade, so the undo restores the owner's counter for later tests.
    monkeypatch.setattr(session_lifecycle, counter, 5)
    assert vars(module)[counter] == 5 and counter not in vars(session_lifecycle)
    too_long = "k" * (session_lifecycle.STOP_DECLINED_KEY_MAX_CHARS + 1)
    sessions = MagicMock()
    with caplog.at_level("WARNING", logger=session_lifecycle.__name__):
        if counter == "_stop_declined_dropped":
            session_lifecycle.note_stop_declined(too_long, "u", now=100.0)
        else:
            session_lifecycle._park_queue(sessions, too_long, (("t", "x", {}),))
    assert getattr(session_lifecycle, counter) == 6 == vars(module)[counter]
    assert [r.name for r in caplog.records] == [session_lifecycle.__name__]
    assert "6 dropped so far" in caplog.records[-1].getMessage()
    assert all(key != too_long for key, _presser in session_lifecycle._stop_declined_markers)
    assert too_long not in session_lifecycle._parked_queue


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #


def _test_tree_texts() -> list[tuple[str, str]]:
    root = source_corpus.repo_root()
    paths = [*(root / "test").rglob("*.py"), *(root / "src" / "kiro_crew" / "apps").rglob("*.py")]
    out = []
    for path in sorted(paths):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("src/") and "/tests/" not in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "session_lifecycle" in text:
            out.append((rel, text))
    return out


def _patched_through_the_facade() -> set[str]:
    spellings = {session_lifecycle.__name__: session_lifecycle.__name__}
    names: set[str] = set()
    for _rel, text in _test_tree_texts():
        names |= facade_mirror_scan.text_patched_names(text, spellings).get(
            session_lifecycle.__name__, set()
        )
    return names


def _reads(module: ModuleType) -> set[str]:
    tree = _tree(module)
    imports = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno not in imports
    }


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    gaps = []
    for module in _owners():
        for name in sorted(patched & _reads(module)):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def _index() -> Any:
    from kiro_crew import _facade_mirror

    return vars(session_lifecycle)[_facade_mirror.INDEX_ATTRIBUTE]


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan saw the facade's patch corpus: these are patched through it at the split.
    assert {"record_session_ended", "_CHILD_CANCEL_TIMEOUT_SECS"} <= patched
    assert _reach_gaps(patched, _index()) == []


def test_the_reach_scan_can_fail() -> None:
    index = _index()
    owner = f"{_PACKAGE}.teardown"
    holders = dict(index.holders)
    holders["record_session_ended"] = tuple(
        h for h in holders["record_session_ended"] if h != owner
    )
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"record_session_ended"}, index) == []
    assert _reach_gaps({"record_session_ended"}, blind) == [f"{owner}.record_session_ended"]


def test_the_mirror_indexes_one_object_per_name_and_serves_the_rebound_counters() -> None:
    index = _index()
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(session_lifecycle._EXPORTS) == set(session_lifecycle._SERVED)
    for name, owner in session_lifecycle._EXPORTS.items():
        assert getattr(session_lifecycle, name) is vars(sys.modules[owner])[name]


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readers = [m for m in _owners() if "_park_queue" in vars(m)]
    assert len(readers) >= 3
    before = session_lifecycle._park_queue
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(session_lifecycle, "_park_queue", sentinel)
        assert [vars(m)["_park_queue"] for m in readers] == [sentinel] * len(readers)
        assert vars(session_lifecycle)["_park_queue"] is sentinel
    assert [vars(m)["_park_queue"] for m in readers] == [before] * len(readers)
    assert session_lifecycle._park_queue is before
