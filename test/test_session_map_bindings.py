"""``SessionMap`` keeps every member it had, and each is bound from the module that owns it.

The map's lock-and-file members live in ``kiro_crew.session_map_store`` and its channel-link
members in ``kiro_crew.session_map_links``: each body there is a module-level function, and the
class binds it by name -- ``m = _owner.m`` for one whose first parameter is ``self``,
``staticmethod(_owner.f)`` for the three static helpers. The state stays on the instance, so the live map, a throwaway
reader and every patch of a member see the same object they always did. Every member that
reads a name the tests patch through ``kiro_crew.session_map`` is defined in the facade, so
the facade is a plain module, with no write-forwarding mirror. This file pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature and the module that owns it -- and each bound member IS its owner's function;
* every member reads as a method of the class, through every wrapper: ``__qualname__`` is
  ``SessionMap.<name>`` and ``__module__`` is ``kiro_crew.session_map``;
* no owner keeps an unbound ``self`` function, and no bound function uses zero-argument
  ``super()``, ``__class__`` or a name-mangled attribute, which only work in a class body;
* no owner imports the facade at run time, and each imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* the owners log as ``kiro_crew.session_map``, and the moved ``ConversationOwnershipConflict``
  keeps it as its module;
* the facade still binds every module-level name it bound before the split, and the lock, its
  decorator and the moved exception are the owners' own objects;
* no owner reads, as a global of its own, a name the test trees patch through the facade, so
  every such patch still reaches the code that reads it.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import session_map as facade
from kiro_crew import session_map_links, session_map_store
from kiro_crew.session_map import SessionMap

# The reach check parses the test trees; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_session_map_bindings")

_FACADE = "kiro_crew.session_map"

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, bool, str, str]] = {
    "__init__": ("function", False, "b60c5fea256e", "session_map"),
    "_claim_for_flush": ("function", False, "81eec0f2b49e", "session_map_store"),
    "_emit_unbind_audit": ("function", False, "6dee7c04ead0", "session_map"),
    "_ensure_entry": ("function", False, "2691c02c86a5", "session_map"),
    "_evict_rival_claimants": ("function", False, "549d1bba1f9a", "session_map_links"),
    "_flush_async": ("function", True, "b60c5fea256e", "session_map"),
    "_inbound_binding": ("staticmethod", False, "e524cfbdf3b5", "session_map_links"),
    "_is_self_derived": ("staticmethod", False, "7a84b80958b0", "session_map_links"),
    "_load": ("function", False, "b60c5fea256e", "session_map_store"),
    "_mirror_key": ("function", False, "f1d40e624ae4", "session_map_links"),
    "_new_binding_nonce": ("staticmethod", False, "ce7757ffdacc", "session_map_links"),
    "_note_bind": ("function", False, "958babeb61c8", "session_map"),
    "_note_inbound_unbind": ("function", False, "0ca375d2aa08", "session_map"),
    "_rebuild_thread_index": ("function", False, "b60c5fea256e", "session_map_links"),
    "_remove_entry": ("function", False, "27b949e9015d", "session_map"),
    "_repair_or_remove_stale": ("function", False, "958babeb61c8", "session_map"),
    "_resolve_alias": ("function", False, "6fa4015b9565", "session_map"),
    "_restore_dirty": ("function", False, "b60c5fea256e", "session_map_store"),
    "_save": ("function", False, "b60c5fea256e", "session_map_store"),
    "_serialize": ("function", False, "0b041d09502d", "session_map_store"),
    "_take_pending_snapshot": ("function", False, "81eec0f2b49e", "session_map_store"),
    "_write": ("function", False, "b60c5fea256e", "session_map_store"),
    "_write_payload": ("function", False, "5f650127c38f", "session_map_store"),
    "aclose": ("function", True, "b60c5fea256e", "session_map_store"),
    "aflush": ("function", True, "b60c5fea256e", "session_map_store"),
    "batched_save": ("function", False, "c323b5c58041", "session_map_store"),
    "channel_key_for_stem": ("function", False, "99b8e84a3315", "session_map"),
    "clear_mirror_link": ("function", False, "e17394b73cfb", "session_map_links"),
    "clear_mirror_link_if": ("function", False, "1dd34978d404", "session_map_links"),
    "clear_mirror_links_at": ("function", False, "daeb9edbe83d", "session_map_links"),
    "clear_native_fork": ("function", False, "958babeb61c8", "session_map"),
    "clear_sid": ("function", False, "1eda252b53bd", "session_map"),
    "clear_sid_unlanded": ("function", False, "1eda252b53bd", "session_map"),
    "clear_slack_link": ("function", False, "1eda252b53bd", "session_map_links"),
    "clear_slack_link_if": ("function", False, "c9d53f91d7af", "session_map_links"),
    "delete": ("function", False, "c52498e8a791", "session_map"),
    "delete_if_sid": ("function", False, "70e3d61c5764", "session_map"),
    "find_key_by_sid": ("function", False, "08155f86f770", "session_map"),
    "find_mirror_sessions": ("function", False, "7b7ff4cef714", "session_map_links"),
    "flush": ("function", False, "b60c5fea256e", "session_map_store"),
    "get": ("function", False, "4b576121a346", "session_map"),
    "get_agent_override": ("function", False, "4b576121a346", "session_map"),
    "get_cwd": ("function", False, "f1d40e624ae4", "session_map"),
    "get_discarded_sid": ("function", False, "f1d40e624ae4", "session_map"),
    "get_flag": ("function", False, "903f7cc40737", "session_map"),
    "get_link": ("function", False, "cdc6a6d31d8b", "session_map_links"),
    "get_mirror_link": ("function", False, "cdc6a6d31d8b", "session_map_links"),
    "get_native_fork": ("function", False, "6d695f7e4ad4", "session_map"),
    "get_project_override": ("function", False, "4b576121a346", "session_map"),
    "get_provider": ("function", False, "f1d40e624ae4", "session_map"),
    "get_session_for_thread": ("function", False, "d1420626ba0f", "session_map_links"),
    "get_slack_link": ("function", False, "1ff511281524", "session_map_links"),
    "has_hint": ("function", False, "1eda252b53bd", "session_map"),
    "has_mirror_row": ("function", False, "1eda252b53bd", "session_map_links"),
    "is_mirror_paused": ("function", False, "8f19901e86b9", "session_map_links"),
    "is_slack_paused": ("function", False, "1eda252b53bd", "session_map_links"),
    "mapped_sid": ("function", False, "f1d40e624ae4", "session_map"),
    "mapped_sids_by_key": ("function", False, "8cab7a4db01b", "session_map"),
    "mark_sid_unlanded": ("function", False, "f4d175e0117d", "session_map"),
    "max_generation": ("function", False, "507f0cfc2b1b", "session_map"),
    "mirror_accepts_inbound": ("function", False, "1eda252b53bd", "session_map_links"),
    "mirror_claim_blockers": ("function", False, "1d8651fc9ebe", "session_map_links"),
    "mirror_link_nonce": ("function", False, "f1d40e624ae4", "session_map_links"),
    "privacy_flagged_entries": ("function", False, "d9cba8aba682", "session_map"),
    "privacy_flags": ("function", False, "349ed17a0181", "session_map"),
    "prune": ("function", False, "fa6cfecd5574", "session_map"),
    "replace_mirror_link_if": ("function", False, "7cf643151494", "session_map_links"),
    "reserve_generation": ("function", False, "57eca86d38c3", "session_map"),
    "set": ("function", False, "7b9b418f95e7", "session_map"),
    "set_agent_override": ("function", False, "f98e4973c29b", "session_map"),
    "set_flag": ("function", False, "d2eaf01aaf84", "session_map"),
    "set_link": ("function", False, "97790037e57a", "session_map_links"),
    "set_mirror_link": ("function", False, "e65f668f550c", "session_map_links"),
    "set_mirror_paused": ("function", False, "1293ad7be097", "session_map_links"),
    "set_native_fork": ("function", False, "5266421a6a05", "session_map"),
    "set_project_override": ("function", False, "b5eb8905539f", "session_map"),
    "set_slack_link": ("function", False, "3d8214a87052", "session_map_links"),
    "set_slack_paused": ("function", False, "f83180910a99", "session_map_links"),
    "sid_unlanded_pending": ("function", False, "1eda252b53bd", "session_map"),
    "slack_link_nonce": ("function", False, "f1d40e624ae4", "session_map_links"),
    "stamp_privacy_headers": ("function", True, "fa6cfecd5574", "session_map"),
}

#: Every module-level name the facade bound before the split, dunders aside, and the
#: standard-library modules bound by ``import <module>`` aside too (L21: a later change to
#: what the module imports is not a regression of its surface).
_BASE_NAMESPACE = frozenset("""
AbstractContextManager BindListener Callable ChannelLink ConversationOwnershipConflict
GENERATION_FLOOR_FIELD Iterator MIRROR_OPT_OUT_FLAG NATIVE_FORK_FIELD PRIVACY_LRU_MAX
PRIVACY_ROW_CAP PRIVACY_ROW_KEY_MAX PROVIDER_LABEL_DEFAULT ParamSpec Path PrivacyRowRefused
SESSION_MAP_FILENAME SLACK_NAMESPACE SUPPRESS_REPLAY_FLAG SessionMap TypeVar UNBIND_REASONS
UNBIND_REASON_ENTRY_DELETED UNBIND_REASON_PRUNED_STALE UNBIND_REASON_UNSPECIFIED
UNLANDED_SID_FIELD UnbindListener _BIND_LISTENER _DURABLE_FLAGS _FLUSH_DEBOUNCE_SECS
_KIRO_SESSIONS_DIR _MAP_LOCK _P _PRIVACY_STRICTNESS _R _RESUMABLE_JSONL_MIN_BYTES
_UNBIND_LISTENER _guarded _has_durable_flag _header_records_privacy_mode _infer_source
_jsonl_holds_a_turn _keeps_entry _kiro_sessions_dir _normalize_unbind_reason
_privacy_flags_on _serialized _stash_and_clear_sid _survives_prune annotations binding_token
bounded_session_id canonical_key config_dir contextmanager is_channel_session_key
kiro_sessions_dir legacy_dashboard_mirror_key logger needs_tightening nullcontext
replace_with_retry resume_decided_by_load sel session_files_resumable set_bind_listener
set_unbind_listener split_dm_session_key
""".split())

#: The names the split added to the facade: the owner aliases and the binding glue.
_ADDED = frozenset({"_links", "_store", "_OWNERS", "_own_methods"})

#: Module-level names the one-module ``session_map.py`` bound that the facade does not bind:
#: standard-library and typing imports only moved code reads. Every other name it bound
#: still resolves on the facade.
_DROPPED_IMPORTS = frozenset(
    {"AbstractContextManager", "Iterator", "ParamSpec", "TypeVar", "contextmanager", "nullcontext"}
)

#: The module-level names that moved to an owner, and the owner whose object the facade binds.
_MOVED = {
    "_MAP_LOCK": session_map_store,
    "_P": session_map_store,
    "_R": session_map_store,
    "_guarded": session_map_store,
    "ConversationOwnershipConflict": session_map_links,
}


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in facade._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(SessionMap)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    return value


def _source(module: ModuleType) -> str:
    return Path(module.__file__ or "").read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds with ``self``: every ``self``-first function."""
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.args.args
        and node.args.args[0].arg == "self"
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(SessionMap)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []
    assert SessionMap.__dict__.get("__annotations__", {}) == {}


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    value = vars(SessionMap)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) is coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    home = sys.modules[f"kiro_crew.{owner}"]
    # The body's own file, read off the code object (the function's ``__module__`` names the
    # facade on purpose, so it cannot say where the body lives).
    assert inspect.unwrap(fn).__code__.co_filename == home.__file__
    if owner != "session_map":
        assert vars(home)[name] is fn


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_every_member_reads_as_a_method_of_the_class(name: str) -> None:
    """A repr, a pickle path or a log of a bound member names it as it did before the split.

    ``_own_methods`` renames each object the class holds from an owner -- a function, or
    the ``staticmethod`` that copied its names -- and every function it wraps
    (``@_guarded``, ``@contextmanager``), after the class attribute it is bound to.
    """
    fn: Any = vars(SessionMap)[name]
    chain = []
    while fn is not None:
        chain.append((fn.__qualname__, fn.__module__))
        fn = getattr(fn, "__wrapped__", None)
    assert chain == [(f"SessionMap.{name}", _FACADE)] * len(chain)
    assert f"SessionMap.{name} " in repr(_member_function(name))
    assert "self" not in _member_function(name).__annotations__


def _resolve(module: str, qualname: str) -> Any:
    """What a pickle, or any import of a dotted reference, resolves ``module`` +
    ``qualname`` to: the module's attribute chain along the qualified name."""
    target: Any = importlib.import_module(module)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def test_a_bound_member_resolves_by_its_facade_path() -> None:
    for name in ("set_mirror_link", "batched_save", "_inbound_binding", "get"):
        fn = _member_function(name)
        assert _resolve(fn.__module__, fn.__qualname__) is fn


def test_every_owner_is_listed_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _k, _c, _d, owner in _MEMBERS.values() if owner != "session_map"}
    assert {name.rsplit(".", 1)[-1] for name in facade._OWNERS} == homes


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    functions = _self_functions(_tree(module))
    assert functions, f"{owner} binds nothing"
    for node in functions:
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


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import ``kiro_crew.session_map`` outside an ``if TYPE_CHECKING:`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == _FACADE or (
                node.module == "kiro_crew" and any(a.name == "session_map" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.session_map import SessionMap\n"
        "def f():\n    from kiro_crew.session_map import x\n"
        "import kiro_crew.session_map\n"
        "from kiro_crew import session_map\n"
        "from kiro_crew import session_map_store\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The ``kiro_crew.session_map_*`` owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("kiro_crew.session_map_"):
                found.add(node.module)
            elif node.module == "kiro_crew":
                found.update(
                    f"kiro_crew.{a.name}" for a in node.names if a.name.startswith("session_map_")
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kiro_crew.session_map_"))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.session_map_store import _guarded\n"
        "from kiro_crew import session_map_links as _l, validation\n"
        "import kiro_crew.session_map_extra\n"
        "from kiro_crew import session_map\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.session_map_store",
        "kiro_crew.session_map_links",
        "kiro_crew.session_map_extra",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = facade._OWNERS
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
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, a mutator call on it and ``setattr(self, "<f>",
    ...)``. Reads: every other load and ``getattr``/``hasattr(self, "<f>")``."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                ctx = getattr(node, "ctx", None)
                (written if isinstance(ctx, (ast.Store, ast.Del)) else read).add(name)
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
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    assert _fields([node]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module)))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_owners_log_as_the_facade(owner: str) -> None:
    """``caplog`` scopes and operator log filters key on ``kiro_crew.session_map``."""
    module = sys.modules[owner]
    assert getattr(module, "logger", facade.logger) is facade.logger


@pytest.mark.parametrize("owner", facade._OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """A traceback, an HTTP 409's log line or a pickle renders a raised class as
    ``module.qualname``; a class that moved to an owner keeps naming the facade."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: _FACADE for name in defined
    }


def test_the_ownership_conflict_resolves_by_its_facade_path() -> None:
    cls = facade.ConversationOwnershipConflict
    assert (cls.__module__, cls.__qualname__) == (_FACADE, "ConversationOwnershipConflict")
    assert _resolve(cls.__module__, cls.__qualname__) is cls


# --------------------------------------------------------------------------- #
# The facade keeps its module surface, and its moved names are the owners' objects.
# --------------------------------------------------------------------------- #


def test_the_facade_binds_every_name_it_bound_before_the_split() -> None:
    names = {n for n in vars(facade) if not (n.startswith("__") and n.endswith("__"))}
    assert _DROPPED_IMPORTS <= _BASE_NAMESPACE
    assert sorted(_BASE_NAMESPACE - _DROPPED_IMPORTS - names) == []
    assert sorted(_ADDED - names) == []


@pytest.mark.parametrize("name", sorted(_MOVED))
def test_a_moved_module_name_is_its_owners_object(name: str) -> None:
    """The dashboard's storage move holds ``session_map._MAP_LOCK`` around a map write the
    store's ``_guarded`` takes: one lock only if the facade binds the owner's own object."""
    assert vars(facade)[name] is vars(_MOVED[name])[name]


def test_the_links_owner_takes_the_lock_through_the_stores_decorator() -> None:
    assert session_map_links._guarded is session_map_store._guarded
    assert session_map_store._guarded.__globals__["_MAP_LOCK"] is facade._MAP_LOCK


# --------------------------------------------------------------------------- #
# Every name patched through the facade reaches the code that reads it.
# --------------------------------------------------------------------------- #

#: The test tree the patch scan reads; a test edit there can change what this file checks.
_TEST_TREE = Path(__file__).resolve().parents[1] / "test"

#: Patches the test trees make through the facade at the split, and a file that makes each,
#: so the scan is seen to measure.
_KNOWN_PATCHES = {
    "config_dir": "test_session_map_locking.py",
    "_KIRO_SESSIONS_DIR": "test_session_map_flush.py",
    "_kiro_sessions_dir": "test_eager_spawn.py",
    "_FLUSH_DEBOUNCE_SECS": "test_channel_connect_row.py",
    "sel": "test_session_map_mirror.py",
    "_survives_prune": "test_session_map_mirror.py",
    "PRIVACY_ROW_CAP": "test_messaging_privacy_mode.py",
    "_header_records_privacy_mode": "test_session_map_conv_state.py",
}


_SPELLINGS = {_FACADE: _FACADE}


def _patched_in(text: str) -> set[str]:
    """The names *text* patches through the facade, read by both of ``facade_mirror_scan``'s
    readers: the regular expressions, then the syntax tree for the shapes they cannot see."""
    names = facade_mirror_scan.text_patched_names(text, _SPELLINGS).get(_FACADE, set())
    try:
        events = facade_mirror_scan.patch_events(text, _SPELLINGS)
    except SyntaxError:
        return names
    return names | facade_mirror_scan.names_by_target(events, [_FACADE]).get(_FACADE, set())


def _patched_through_the_facade() -> set[str]:
    """Every name a file pytest collects tests or fixtures from patches on the facade,
    derived from the patch calls themselves: the root conftest, ``test/``, and the test
    modules and conftests under ``src/kiro_crew/apps/builtins``."""
    patched: set[str] = set()
    root = source_corpus.repo_root()
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        collected = path.name.startswith("test_") or path.name == "conftest.py"
        if not (
            rel == "conftest.py"
            or rel.startswith("test/")
            or (rel.startswith("src/kiro_crew/apps/builtins/") and collected)
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "session_map" in text:
            patched |= _patched_in(text)
    return patched


def _reach_gaps(patched: set[str], modules: dict[str, tuple[str, dict[str, Any]]]) -> list[str]:
    """``owner.name`` for each patched name an owner's code reads as a global it binds
    itself. A write through the facade rebinds the facade's name alone, so such a read
    would keep the original object."""
    gaps = []
    for owner, (source, namespace) in modules.items():
        for name in sorted(patched & facade_mirror_scan.global_reads(source, owner)):
            if name in namespace:
                gaps.append(f"{owner}.{name}")
    return gaps


def _owner_sources() -> dict[str, tuple[str, dict[str, Any]]]:
    return {m.__name__: (_source(m), vars(m)) for m in _owners()}


def test_every_name_patched_through_the_facade_reaches_the_code_reading_it() -> None:
    patched = _patched_through_the_facade()
    for name, where in _KNOWN_PATCHES.items():
        assert name in (_TEST_TREE / where).read_text(encoding="utf-8"), (name, where)
    assert set(_KNOWN_PATCHES) <= patched
    assert _reach_gaps(patched, _owner_sources()) == []


def test_the_reach_check_can_fail() -> None:
    """A planted owner that reads a patched name as its own global is reported; one that
    reads a name it does not bind (a builtin) is not, and neither is an unpatched read."""
    planted = (
        "from kiro_crew.config.paths import config_dir\n"
        "def f(self):\n"
        "    return config_dir(), len(self._data)\n"
    )
    modules = {"planted": (planted, {"config_dir": facade.config_dir})}
    assert _reach_gaps({"config_dir", "len"}, modules) == ["planted.config_dir"]
    assert _reach_gaps({"sel"}, modules) == []


def test_both_patch_readers_see_the_facade() -> None:
    """The regular expressions read a dotted target; the syntax tree reads an alias chain
    they cannot follow (``sm = kiro_crew.session_map`` then ``setattr(sm, ...)``)."""
    dotted = 'def test_x(monkeypatch):\n    monkeypatch.setattr("kiro_crew.session_map.sel", 1)\n'
    chained = (
        "import kiro_crew\n"
        "def test_x(monkeypatch):\n"
        "    sm = kiro_crew.session_map\n"
        "    monkeypatch.setattr(sm, 'config_dir', 1)\n"
    )
    assert _patched_in(dotted) == {"sel"}
    assert _patched_in(chained) == {"config_dir"}


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve()
    assert this_file.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
