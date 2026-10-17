"""``ConversationLog`` keeps every member it had, and each is bound from the module that owns it.

Part of the class's behaviour lives in the ``kiro_crew.history_*`` owner modules the facade
lists in ``_OWNERS``: each moved member's body is a module-level function there whose first
parameter is ``self``, and the class binds it by name (``m = _owner.m``, or
``staticmethod(_owner.f)``). The instance state, and the process-wide lock registries, stay on
the class, so tests, subclasses and every caller see the same object they always did. This
file pins the binding contract:

* the frozen member inventory -- every member, its kind, its signature (the ``self``
  annotation aside) and the module that owns it -- and that no member is a coroutine;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read, and the
  process-wide registries it reads through ``type(self)``;
* no subclass of ``ConversationLog`` binds its own value for a registry the owners read
  through ``type(self)``, which is the one case where that read differs from the class's;
* owners log under the facade's logger, and a moved class keeps the facade as its module;
* a write through ``kiro_crew.history`` reaches every owner that binds the name (the
  facade's mirror, ``_facade_mirror``).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiro_crew import history
from kiro_crew.history import ConversationLog

pytestmark = pytest.mark.xdist_group(name="tree_scan_history_bindings")

_PACKAGE = "kiro_crew"
_REPO = Path(__file__).resolve().parents[1]

#: Member -> (kind, signature digest, owning module). Frozen at the split: the kind and
#: signature are BASE's; the owner is where the member lives now (``history`` is the facade).
_MEMBERS: dict[str, tuple[str, str, str]] = {
    "_PREVIEW_MAX_CHARS": ("data", "", ""),
    "_PREVIEW_TAIL_BYTES": ("data", "", ""),
    "_SNIPPET_LEAD": ("data", "", ""),
    "_SNIPPET_MAX": ("data", "", ""),
    "_SNIPPET_TRAIL": ("data", "", ""),
    "_TAIL_AVG_MSG_BYTES": ("data", "", ""),
    "_TAIL_MAX_GROWTHS": ("data", "", ""),
    "_TAIL_MIN_BYTES": ("data", "", ""),
    "__init__": ("function", "8c32ef45d37b", "history"),
    "_attempts_describe_current_span": ("function", "70a892aa42ac", "history_marker"),
    "_build_folded": ("function", "0e70f010552c", "history"),
    "_bump_cache_gen": ("function", "10bd08640d34", "history"),
    "_cache_fill_lock": ("function", "65509f0a467a", "history"),
    "_cache_gen": ("function", "028659ce42ac", "history"),
    "_cache_gens": ("data", "", ""),
    "_cache_gens_guard": ("data", "", ""),
    "_cache_identity": ("staticmethod", "cbf292677e06", "history"),
    "_cache_key_identities": ("staticmethod", "77592e3ec310", "history"),
    "_canonical_key": ("staticmethod", "44f943477a65", "history"),
    "_content_snippet": ("function", "496aef9863f6", "history"),
    "_content_text": ("staticmethod", "3c37f6d88434", "history"),
    "_count_messages": ("function", "c29ea0d1b59a", "history"),
    "_current_span_fields": ("function", "2dc818c6b1f1", "history_marker"),
    "_delete_in_flight_marks": ("function", "4be47402b13f", "history_locks"),
    "_deletes_in_flight": ("data", "", ""),
    "_deletes_in_flight_guard": ("data", "", ""),
    "_file_lock": ("function", "bc31c6c18d74", "history_locks"),
    "_file_locks": ("data", "", ""),
    "_file_locks_guard": ("data", "", ""),
    "_flock_epochs": ("data", "", ""),
    "_flock_guard": ("data", "", ""),
    "_flock_hold_witness": ("function", "8a8352d8863e", "history_locks"),
    "_flock_state": ("data", "", ""),
    "_folded_content": ("function", "b927bd9ae0f3", "history"),
    "_intent_summary_cache_path": ("function", "445450696709", "history_summaries"),
    "_invalidate_cache": ("function", "958babeb61c8", "history"),
    "_iter_message_texts": ("function", "6a8b4191d1b9", "history"),
    "_last_row_ts": ("function", "4b576121a346", "history"),
    "_lock_path": ("function", "445450696709", "history_locks"),
    "_locked": ("function", "e1e3936a7475", "history_locks"),
    "_locked_stem": ("function", "e1e3936a7475", "history_locks"),
    "_maybe_rotate": ("function", "f1cc2ed06af8", "history"),
    "_path": ("function", "445450696709", "history"),
    "_pause_for_transient_retry": ("function", "b60c5fea256e", "history"),
    "_persist_inline_attachments": ("function", "c18505814ef3", "history_append"),
    "_prune_search_memos": ("function", "6e33d46745e6", "history"),
    "_publish_if_current": ("function", "90da3125bc72", "history"),
    "_read_messages": ("function", "d4d3f09b758c", "history"),
    "_read_messages_locked": ("function", "8ff63ff67a0e", "history"),
    "_read_metadata": ("function", "2691c02c86a5", "history"),
    "_read_metadata_state": ("function", "6a42f505e2b6", "history"),
    "_read_metadata_status": ("function", "3af6e6a8bc73", "history"),
    "_read_tail_messages": ("function", "4ad85d997c9e", "history"),
    "_rebuild_tab_id_index": ("function", "b60c5fea256e", "history"),
    "_recent_cache_key": ("staticmethod", "5ae23f0ed909", "history"),
    "_recent_via_tail": ("function", "f47e3c417ab9", "history"),
    "_rewrite_session_locked": ("function", "b67b482624a6", "history"),
    "_run_fd_cleanup_off_loop": ("staticmethod", "7b61a072f2a9", "history_locks"),
    "_schedule_flock_release": ("function", "9a7d7f5b5bf9", "history_locks"),
    "_snippet_texts": ("function", "6a8b4191d1b9", "history"),
    "_sub_offset_for_generation": ("staticmethod", "4abae807ffde", "history_marker"),
    "_summary_cache_path": ("function", "445450696709", "history_summaries"),
    "_transcript_holds_mid": ("function", "1e020a871173", "history_threads"),
    "_update_metadata_locked": ("function", "c50c92c12054", "history"),
    "_withhold_if_restricted": ("function", "958babeb61c8", "history_derivation"),
    "advance_consolidation_sub_offset": ("function", "d349ae2beaab", "history_marker"),
    "agent_usage": ("function", "e1e8f90e5eef", "history"),
    "append": ("function", "f78c841b2d80", "history_append"),
    "append_if_absent": ("function", "05473a22f234", "history_append"),
    "append_thread_reply": ("function", "6591823e2c38", "history_threads"),
    "atomic_appends": ("function", "e1e3936a7475", "history_locks"),
    "chain_mid_rotation": ("function", "1eda252b53bd", "history"),
    "chained_keys": ("function", "349ed17a0181", "history"),
    "clear_closed": ("function", "f44a8f79dc9b", "history"),
    "consolidated_offset": ("function", "028659ce42ac", "history_marker"),
    "consolidation_counts": ("function", "286ecf2c0fe3", "history_marker"),
    "consolidation_retry_state": ("function", "945c2956ab9c", "history_marker"),
    "delete_in_flight": ("function", "1eda252b53bd", "history_locks"),
    "delete_in_flight_window": ("function", "e1e3936a7475", "history_locks"),
    "delete_memory_consolidation_session": ("function", "8cd62139c6bd", "history"),
    "delete_session": ("function", "822cf7171de0", "history"),
    "derivation_hold": ("function", "dfeef8bbc1c0", "history_derivation"),
    "derive_messages": ("function", "d4d3f09b758c", "history_derivation"),
    "derive_messages_chained": ("function", "d4d3f09b758c", "history_derivation"),
    "derive_messages_chained_with_keys": ("function", "81471b1c6628", "history_derivation"),
    "derive_recent": ("function", "cac529f460f5", "history_derivation"),
    "get_cached_intent_summary": ("function", "ffa13bebb6bc", "history_summaries"),
    "get_cached_summary": ("function", "4b576121a346", "history_summaries"),
    "get_metadata": ("function", "2691c02c86a5", "history"),
    "get_metadata_status": ("function", "3af6e6a8bc73", "history"),
    "get_unconsolidated": ("function", "4c402cd67e6f", "history"),
    "has_log": ("function", "1eda252b53bd", "history"),
    "has_messages": ("function", "1eda252b53bd", "history"),
    "init": ("function", "b60c5fea256e", "history"),
    "invalidate_tab_id_cache": ("function", "b60c5fea256e", "history"),
    "last_message_info": ("function", "5ce93cd5cafb", "history"),
    "last_message_preview": ("function", "67642ad2d3e2", "history"),
    "last_row_ts": ("function", "4b576121a346", "history"),
    "last_speech_info": ("function", "5c2c36671674", "history"),
    "list_sessions": ("function", "32c901f96e6a", "history"),
    "locked_stems": ("function", "dfeef8bbc1c0", "history_locks"),
    "mark_consolidated": ("function", "82b842603c08", "history_marker"),
    "metadata_line_state": ("function", "f1d40e624ae4", "history"),
    "mtime_of": ("function", "eed46331c744", "history"),
    "newest_session_stems": ("function", "37c4401320a5", "history"),
    "note_tab_id": ("function", "d619a539dc08", "history"),
    "publication_hold": ("function", "71bd4ae68dce", "history_derivation"),
    "read_file_change_messages": ("function", "d4d3f09b758c", "history"),
    "read_intent_summary": ("function", "ed187f9bbc5a", "history_summaries"),
    "read_messages": ("function", "d4d3f09b758c", "history"),
    "read_messages_chained": ("function", "d4d3f09b758c", "history"),
    "read_messages_chained_full": ("function", "d4d3f09b758c", "history"),
    "read_messages_chained_page": ("function", "053fbeec09b6", "history"),
    "read_messages_chained_tail": ("function", "ce37b325fd52", "history"),
    "read_rotated_messages_chained": ("function", "d4d3f09b758c", "history"),
    "read_threads": ("function", "fc4954d3d484", "history_threads"),
    "recent": ("function", "3151c6bfcca2", "history"),
    "recent_chained": ("function", "3151c6bfcca2", "history"),
    "recent_from_source": ("function", "6b83bff8d534", "history"),
    "recent_with_provenance": ("function", "0634157096f5", "history"),
    "record_consolidation_environment_failure": ("function", "ca5863c12a56", "history_marker"),
    "record_consolidation_failure": ("function", "fe8a2b418865", "history_marker"),
    "rewrite_session": ("function", "b67b482624a6", "history"),
    "rotation_generation": ("function", "028659ce42ac", "history_marker"),
    "search_sessions": ("function", "cb218591aedd", "history"),
    "session_mtime": ("function", "eed46331c744", "history"),
    "set_cached_intent_summary": ("function", "271ca4530d1f", "history_summaries"),
    "set_cached_summary": ("function", "1d6bdfb57c5e", "history_summaries"),
    "set_title": ("function", "e8c95d28974e", "history"),
    "sliding_window": ("function", "fa03f21e682f", "history"),
    "snapshot_for_consolidation": ("function", "3943122bc725", "history_marker"),
    "thread_transcript_identity": ("function", "4b576121a346", "history_threads"),
    "threads_sidecar_path": ("function", "445450696709", "history_threads"),
    "unconsolidated_count": ("function", "028659ce42ac", "history_marker"),
    "update_metadata": ("function", "5e1ab1217b61", "history"),
    "update_metadata_if": ("function", "316452efe533", "history"),
}

#: Class attributes holding process-wide state. ``history_locks`` reads the lock ones as
#: ``type(self).<name>``, because an owner cannot import the facade's class at run time;
#: ``_cache_gens`` and its guard reach ``history_cache`` through ``registry_owner``.
_REGISTRIES = frozenset(
    {
        "_cache_gens",
        "_cache_gens_guard",
        "_deletes_in_flight",
        "_deletes_in_flight_guard",
        "_file_locks",
        "_file_locks_guard",
        "_flock_epochs",
        "_flock_guard",
        "_flock_state",
    }
)

#: The exception classes the split moved: their names reach tracebacks and logs, so each
#: keeps the facade as its ``__module__``.
_MOVED_EXCEPTIONS = (
    "HistoryLockTimeout",
    "OnLoopPersistError",
    "ThreadStoreUnreadable",
    "TranscriptBusy",
    "TranscriptWithheld",
)


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in history._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(ConversationLog)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module, owner: str = "") -> list[ast.FunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home and f"{_PACKAGE}.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(ConversationLog)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    present |= set(ConversationLog.__dict__.get("__annotations__", {}))
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, digest, owner = _MEMBERS[name]
    value = vars(ConversationLog)[name]
    if kind == "data":
        assert not inspect.isfunction(value) and not isinstance(value, staticmethod)
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert not (inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn))
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != "history":
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _d, owner in _MEMBERS.values() if owner and owner != "history"}
    listed = {name.rsplit(".", 1)[-1] for name in history._OWNERS}
    assert homes <= listed


@pytest.mark.parametrize("owner", history._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: f"{_PACKAGE}.{home}"
        for name, (kind, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home != "history"
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _class_only_constructs(fn: ast.FunctionDef) -> list[str]:
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
    assert sorted(_class_only_constructs(tree.body[0])) == [  # type: ignore[arg-type]
        "__class__",
        "__hidden",
        "super()",
    ]


@pytest.mark.parametrize("owner", history._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade (``kiro_crew.history``) outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == f"{_PACKAGE}.history" or (
                node.module == _PACKAGE and any(a.name == "history" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == f"{_PACKAGE}.history" for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.history import ConversationLog\n"
        "def f():\n    from kiro_crew.history import x\n"
        "import kiro_crew.history\n"
        "from kiro_crew import history\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", history._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The history owner modules *tree* imports, by dotted name."""
    owners = set(history._OWNERS)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in owners:
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}" for a in node.names if f"{_PACKAGE}.{a.name}" in owners
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in owners)
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.history_files import _safe_key\n"
        "from kiro_crew import history_schema as _schema, platform_compat\n"
        "import kiro_crew.history_offload\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.history_files",
        "kiro_crew.history_schema",
        "kiro_crew.history_offload",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = history._OWNERS
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


def _fields(fns: list[ast.FunctionDef]) -> tuple[set[str], set[str]]:
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
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def _registry_reads(fns: list[ast.FunctionDef]) -> set[str]:
    """Class attributes *fns* read as ``type(self).<name>``."""
    return {
        node.attr
        for fn in fns
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value) == "type(self)"
    }


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    type(self)._flock_state.pop(k)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert _fields([fn]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})
    assert _registry_reads([fn]) == {"_flock_state"}


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", history._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    fns = _self_functions(_tree(module), owner)
    written, read = _fields(fns)
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read
    registries = _registry_reads(fns)
    assert registries <= _REGISTRIES
    assert _listed(doc, r"class attributes of\s+``ConversationLog``") == registries


# --------------------------------------------------------------------------- #
# ``type(self).<registry>`` reads the class's object only while no subclass binds its own.
# --------------------------------------------------------------------------- #


def _class_names_binding_registries(source: str) -> list[str]:
    """Classes in *source* deriving from ``ConversationLog`` (directly, or from a class in
    the same source that does) whose body binds a registry name."""
    tree = ast.parse(source)
    aliases = {"ConversationLog"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == f"{_PACKAGE}.history":
            aliases.update(a.asname for a in node.names if a.name == "ConversationLog" and a.asname)
        elif isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
            if ast.unparse(node.value).rsplit(".", 1)[-1] == "ConversationLog":
                aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    derived: set[str] = set()
    found: list[str] = []
    changed = True
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    while changed:
        changed = False
        for node in classes:
            if node.name in derived:
                continue
            bases = {ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases}
            if bases & (aliases | derived):
                derived.add(node.name)
                changed = True
    for node in classes:
        if node.name not in derived:
            continue
        bound: set[str] = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                bound.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                bound.add(stmt.target.id)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(stmt.name)
        if bound & _REGISTRIES:
            found.append(node.name)
    return found


def test_the_subclass_registry_scan_can_fail() -> None:
    source = (
        "from kiro_crew.history import ConversationLog\n"
        "class Plain(ConversationLog):\n    def append(self, *a): ...\n"
        "class Own(history.ConversationLog):\n    _flock_state = {}\n"
        "class Deeper(Plain):\n    _file_locks: dict = {}\n"
        "from kiro_crew.history import ConversationLog as _Log\n"
        "Alias = history.ConversationLog\n"
        "class ByImport(_Log):\n    _flock_guard = None\n"
        "class ByAssign(Alias):\n    def _flock_epochs(self): ...\n"
        "class Unrelated:\n    _flock_state = {}\n"
    )
    assert sorted(_class_names_binding_registries(source)) == [
        "ByAssign",
        "ByImport",
        "Deeper",
        "Own",
    ]


def _sources_naming_the_class() -> dict[Path, str]:
    roots = [_REPO / "src", _REPO / "test"]
    texts = {
        path: path.read_text(encoding="utf-8", errors="replace")
        for root in roots
        for path in root.rglob("*.py")
    }
    return {path: text for path, text in sorted(texts.items()) if "ConversationLog" in text}


def test_no_subclass_binds_its_own_registry() -> None:
    """``history_locks`` reads the lock registries as ``type(self).<name>``; that is the
    class's own object for every subclass that does not bind the name itself. A class
    body can bind a registry only by spelling its name, so only the files that spell
    one are parsed."""
    sources = _sources_naming_the_class()
    assert any(path.name == "test_slack_dashboard_live_sync.py" for path in sources)
    candidates = {
        path: text for path, text in sources.items() if any(r in text for r in _REGISTRIES)
    }
    assert any(path.name == "test_session_storage.py" for path in candidates)
    offenders = {
        str(path.relative_to(_REPO)): names
        for path, text in candidates.items()
        if (names := _class_names_binding_registries(text))
    }
    assert offenders == {}
    pending = list(ConversationLog.__subclasses__())
    while pending:
        sub = pending.pop()
        pending.extend(sub.__subclasses__())
        assert not _REGISTRIES & set(vars(sub)), f"{sub.__qualname__} binds a registry"


# --------------------------------------------------------------------------- #
# Identity: the logger and the moved classes.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("owner", history._OWNERS)
def test_owners_log_under_the_history_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", history.logger) is history.logger


@pytest.mark.parametrize("owner", history._OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """Tracebacks and logged error chains render a class as ``module.qualname``; a class
    that moved to an owner keeps naming the path callers import it from."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: history.__name__ for name in defined
    }


def test_every_moved_exception_is_one_class_named_through_the_facade() -> None:
    for name in _MOVED_EXCEPTIONS:
        cls = getattr(history, name)
        assert issubclass(cls, BaseException)
        assert cls.__module__ == "kiro_crew.history"
        assert f"{cls.__module__}.{cls.__qualname__}" == f"kiro_crew.history.{name}"
    assert issubclass(history.TranscriptBusy, history.TranscriptWithheld)


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that binds the name.
# --------------------------------------------------------------------------- #


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


def _reach_gaps(index: Any) -> list[str]:
    """An owner that reads a name the facade binds, binding the same object, but that a
    write through the facade would not reach."""
    gaps = []
    facade = vars(history)
    for module in _owners():
        for name in sorted(_reads(module)):
            if name not in vars(module) or name not in facade:
                continue
            if vars(module)[name] is not facade[name]:
                continue
            if module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def _facade_index() -> Any:
    return vars(history)["__facade_mirror__"]


def test_every_name_the_facade_binds_reaches_the_owners_reading_it() -> None:
    index = _facade_index()
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(index.exports) == {"_last_cleanup", "_on_loop_warn_last"}
    assert _reach_gaps(index) == []


def test_the_reach_scan_can_fail() -> None:
    index = _facade_index()
    owner = "kiro_crew.history_locks"
    holders = dict(index.holders)
    holders["logger"] = tuple(h for h in holders["logger"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps(index) == []
    assert _reach_gaps(blind) == [f"{owner}.logger"]


@pytest.mark.parametrize(
    ("name", "owners"),
    [
        ("_FLOCK_ACQUIRE_TIMEOUT_S", ("kiro_crew.history_locks",)),
        ("THREADS_SIDECAR_MAX_BYTES", ("kiro_crew.history_threads",)),
        ("_sessions_dir", ("kiro_crew.history_files",)),
        (
            "datetime",
            ("kiro_crew.history_files", "kiro_crew.history_schema", "kiro_crew.history_append"),
        ),
    ],
)
def test_a_patch_through_the_facade_lands_on_the_owners_and_is_undone(
    monkeypatch: pytest.MonkeyPatch, name: str, owners: tuple[str, ...]
) -> None:
    before = getattr(history, name)
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(history, name, sentinel)
        assert getattr(history, name) is sentinel
        for owner in owners:
            assert vars(sys.modules[owner])[name] is sentinel
    assert getattr(history, name) is before
    for owner in owners:
        assert vars(sys.modules[owner])[name] is before


@pytest.mark.parametrize(
    ("name", "owner"),
    [
        ("_last_cleanup", "kiro_crew.history_files"),
        ("_on_loop_warn_last", "kiro_crew.history_offload"),
    ],
)
def test_a_global_its_owner_rebinds_is_served_live_and_never_bound_here(
    monkeypatch: pytest.MonkeyPatch, name: str, owner: str
) -> None:
    """The owner rebinds it at run time, so a copy bound on the facade would go stale."""
    assert name not in vars(history)
    module = sys.modules[owner]
    monkeypatch.setattr(module, name, 12.5)
    assert getattr(history, name) == 12.5
    monkeypatch.setattr(history, name, 7.0)
    assert vars(module)[name] == 7.0
    assert name not in vars(history)
