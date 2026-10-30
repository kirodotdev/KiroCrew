"""``SecurityEventLog`` keeps every member it had, and each is bound from the module that owns it.

The log's collaborators live in the ``kiro_crew.sel_*`` owner modules: each member's body
there is a module-level function whose first parameter is ``self``, and the class binds it
by name (``m = _owner.m``). The instance state stays on the instance, so the singleton,
tests and every caller see the same object they always did. This file pins that contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports ``kiro_crew.sel`` at run time, and each imports only owners below it;
* each owner's docstring names the instance fields its members write and read;
* every owner logs as ``kiro_crew.sel``, and a class that moved keeps ``kiro_crew.sel`` as
  its module;
* the facade serves exactly the module-level names that moved, every one defined by its
  owner, and a write through ``kiro_crew.sel`` reaches every owner that reads the name;
* no member of the family imports ``time`` (the on-loop guard in ``test_sel.py`` reads the
  facade's own attribute; this reads every owner).
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

import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew import sel as sel_mod
from kiro_crew.sel import SecurityEventLog

# The reach check parses the test trees; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_sel_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, bool, str, str]] = {
    "__init__": ("function", False, "9459aec7b8ff", "sel"),
    "__new__": ("staticmethod", False, "512a9712019e", "sel"),
    "_append_chained_locked": ("function", False, "9dc2f4ce0d80", "sel_append"),
    "_append_lines_locked": ("function", False, "4596c9cd7911", "sel_append"),
    "_chain_hold_relabel": ("function", False, "77cb90ef3feb", "sel_chain_lock"),
    "_chain_lock": ("function", False, "095245d9a46e", "sel_chain_lock"),
    "_chain_lock_path": ("function", False, "b08d8e6a45bd", "sel_chain_lock"),
    "_chain_lock_target": ("function", False, "973268c74bf6", "sel_chain_lock"),
    "_chain_locked": ("function", False, "9b85ac532e17", "sel_append"),
    "_compute_hash": ("function", False, "e00b66d886a5", "sel_trust_root"),
    "_decr_pending": ("function", False, "7722f6b4d0a5", "sel"),
    "_ends_without_newline": ("function", False, "a6632a57e602", "sel_append"),
    "_enforce_segment_retention_locked": ("function", False, "fa6cfecd5574", "sel_rotation"),
    "_ensure_segment_dir": ("function", False, "a6632a57e602", "sel_rotation"),
    "_ensure_writer": ("function", False, "b60c5fea256e", "sel"),
    "_flush_batch": ("function", False, "cdad18fcce0b", "sel"),
    "_flush_with_retry": ("function", False, "8d7d264c42fc", "sel"),
    "_forward_event": ("function", False, "4a2839931ea2", "sel"),
    "_init_lock": ("data", False, "", ""),
    "_init_locked": ("function", False, "d6364a41d78e", "sel"),
    "_initialized": ("data", False, "", ""),
    "_instance": ("data", False, "", ""),
    "_iter_lines_backward": ("function", False, "7a5aa6573f86", "sel_reader"),
    "_live_identity": ("function", False, "73f46ab0b3bf", "sel_append"),
    "_live_size": ("function", False, "fa6cfecd5574", "sel_append"),
    "_load_or_create_hmac_key": ("function", False, "d535b099352d", "sel_trust_root"),
    "_may_rotate": ("function", False, "a6632a57e602", "sel"),
    "_newest_timestamp": ("function", False, "dd4f3743646b", "sel_rotation"),
    "_next_segment_path": ("function", False, "d921fd8d443d", "sel_rotation"),
    "_open_rotation_lock": ("function", False, "f55b53a55ebd", "sel_rotation"),
    "_probe_live": ("function", False, "a8889b3a4593", "sel_append"),
    "_prune_live_locked": ("function", False, "70e0bd12cbf4", "sel_rotation"),
    "_prune_segments_locked": ("function", False, "bf740f702b7e", "sel_rotation"),
    "_read_last_hash": ("function", False, "da09ae3bd522", "sel_append"),
    "_read_sources_newest_first": ("function", False, "6b47aa0f1553", "sel_reader"),
    "_reader_handle": ("function", False, "3a0cc08b1cfb", "sel_reader"),
    "_reanchor_if_replaced": ("function", False, "fa6cfecd5574", "sel_append"),
    "_reanchor_now": ("function", False, "b60c5fea256e", "sel_append"),
    "_record_signature_matches": ("function", False, "dff636aaefc0", "sel_trust_root"),
    "_rotate_under_lock": ("function", False, "b60c5fea256e", "sel_rotation"),
    "_rotation_event": ("function", False, "20d63be29459", "sel"),
    "_rotation_window": ("function", False, "c323b5c58041", "sel_rotation"),
    "_segment_is_signed_by_us": ("function", False, "f149e3ce4665", "sel_reader"),
    "_segments_oldest_first": ("function", False, "caf1b654cf48", "sel_rotation"),
    "_verify_file": ("function", False, "b3f842b77537", "sel_reader"),
    "_writer_loop": ("function", False, "b60c5fea256e", "sel"),
    "flush": ("function", False, "a9c45e0c08a5", "sel"),
    "log": ("function", False, "105f9e23f712", "sel"),
    "log_api_access": ("function", False, "205f9b11a348", "sel"),
    # `redact` added for the context-less MCP gateway daemon (OTLP host audit).
    "log_governance_decision": ("function", False, "f3c3785395e6", "sel"),
    "log_governance_degraded": ("function", False, "9b129b9270d1", "sel"),
    "log_tool_invocation": ("function", False, "28f3ece4bc30", "sel"),
    "prune": ("function", False, "254f3f4fcb91", "sel_rotation"),
    "recent": ("function", False, "612406e814ec", "sel_reader"),
    "set_forward_callback": ("function", False, "2c29453596f8", "sel"),
    "verify_integrity": ("function", False, "cf15a5644924", "sel_reader"),
}

#: Class-level annotations: the two BASE ones, and the fields first set from a member an
#: owner defines, declared in the class so type checkers know them. An annotation creates no
#: attribute.
_ANNOTATED = frozenset({"_hmac_key", "_hmac_key_file", "_initialized", "_instance", "_last_hash"})

_FACADE = "kiro_crew.sel"


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in sel_mod._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(SecurityEventLog)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function."""
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
        for name in vars(SecurityEventLog)
        if not (name.startswith("__") and name.endswith("__")) or name in ("__init__", "__new__")
    }
    assert sorted(present ^ set(_MEMBERS)) == []
    annotated = set(SecurityEventLog.__dict__.get("__annotations__", {}))
    assert annotated == _ANNOTATED
    assert not {"_hmac_key", "_hmac_key_file", "_last_hash"} & set(vars(SecurityEventLog))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind == "data":
        assert name in vars(SecurityEventLog)
        return
    value = vars(SecurityEventLog)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) is coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"kiro_crew.{owner}"
    if owner != "sel":
        assert vars(sys.modules[f"kiro_crew.{owner}"])[name] is fn


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != "sel"}
    assert {name.rsplit(".", 1)[-1] for name in sel_mod._OWNERS} == homes


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module)):
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


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    functions = _self_functions(_tree(sys.modules[owner]))
    assert functions, f"{owner} binds nothing"
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import ``kiro_crew.sel`` outside an ``if TYPE_CHECKING:`` block."""
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
                node.module == "kiro_crew" and any(a.name == "sel" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.sel import SecurityEventLog\n"
        "def f():\n    from kiro_crew.sel import x\n"
        "import kiro_crew.sel\n"
        "from kiro_crew import sel\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The ``kiro_crew.sel_*`` owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("kiro_crew.sel_"):
                found.add(node.module)
            elif node.module == "kiro_crew":
                found.update(f"kiro_crew.{a.name}" for a in node.names if a.name.startswith("sel_"))
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kiro_crew.sel_"))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.sel_reader import X\n"
        "from kiro_crew import sel_rotation as _r, platform_compat\n"
        "import kiro_crew.sel_append\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.sel_reader",
        "kiro_crew.sel_rotation",
        "kiro_crew.sel_append",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = sel_mod._OWNERS
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


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module)))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_owners_log_as_the_facade(owner: str) -> None:
    """Operator log filters and ``caplog`` scopes key on ``kiro_crew.sel``."""
    module = sys.modules[owner]
    assert getattr(module, "logger", sel_mod.logger) is sel_mod.logger


@pytest.mark.parametrize("owner", sel_mod._OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """A traceback or a warning logged with ``exc_info`` renders a raised class as
    ``module.qualname``; a class that moved to an owner keeps naming ``kiro_crew.sel``."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: _FACADE for name in defined
    }


def test_the_chain_contention_error_names_the_facade_in_an_error_chain() -> None:
    from kiro_crew.subagent import _describe_exception

    try:
        try:
            raise sel_mod.SelChainContention("kept writing")
        except sel_mod.SelChainContention as exc:
            raise RuntimeError("audit refused") from exc
    except RuntimeError as outer:
        text = _describe_exception(outer)
    assert text == (
        "RuntimeError: audit refused <- caused by " "kiro_crew.sel.SelChainContention: kept writing"
    )


# --------------------------------------------------------------------------- #
# The facade serves what moved, and a write through it reaches every reader.
# --------------------------------------------------------------------------- #


def test_the_facade_serves_exactly_the_module_level_names_that_moved() -> None:
    """Each served name is one its owner defines (no import from elsewhere is served),
    and no member the class binds becomes a module name of the facade."""
    index = sel_mod._MIRROR
    served = dict(index.served)
    by_owner = {name: owner for owner, names in sel_mod._SERVED_BY_OWNER.items() for name in names}
    assert served == by_owner
    defined = {
        owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in sel_mod._OWNERS
    }
    assert sorted(name for name, owner in served.items() if name not in defined[owner]) == []
    assert set(served).isdisjoint(_MEMBERS)
    moved = [n for n in _MEMBERS if not n.startswith("__") and _MEMBERS[n][3] != "sel"]
    assert moved
    assert [name for name in moved if hasattr(sel_mod, name)] == []


def test_the_mirror_indexes_one_object_per_name() -> None:
    index = sel_mod._MIRROR
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}


def test_a_star_import_binds_no_class_member_or_typing_glue() -> None:
    assert "TYPE_CHECKING" not in sel_mod.__all__
    assert set(sel_mod.__all__).isdisjoint(_MEMBERS)
    assert {"SelChainContention", "SelVerification", "SecurityEventLog", "sel"} <= set(
        sel_mod.__all__
    )


def _patched_through_the_facade() -> set[str]:
    patched: set[str] = set()
    root = source_corpus.repo_root()
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if not (rel == "conftest.py" or rel.startswith("test/") or "/tests/" in rel):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "kiro_crew" not in text or not re.search(r"\bsel\b|kiro_crew\.sel\b", text):
            continue
        found = facade_mirror_scan.text_patched_names(text, {_FACADE: _FACADE})
        patched |= found.get(_FACADE, set())
    return patched


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


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan saw names that moved to three different owners, and names the facade keeps.
    assert {"_SEGMENT_MAX_BYTES", "_open_segment_dir", "_pin_lock_dir", "_default_dir"} <= patched
    assert _reach_gaps(patched, sel_mod._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = sel_mod._MIRROR
    owner = "kiro_crew.sel_rotation"
    holders = dict(index.holders)
    holders["_SEGMENT_MAX_BYTES"] = tuple(h for h in holders["_SEGMENT_MAX_BYTES"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"_SEGMENT_MAX_BYTES"}, index) == []
    assert _reach_gaps({"_SEGMENT_MAX_BYTES"}, blind) == [f"{owner}._SEGMENT_MAX_BYTES"]


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = sys.modules["kiro_crew.sel_rotation"]
    before = owner._SEGMENT_MAX_BYTES
    with monkeypatch.context() as patch:
        patch.setattr(sel_mod, "_SEGMENT_MAX_BYTES", 7)
        assert owner._SEGMENT_MAX_BYTES == 7
        assert sel_mod._SEGMENT_MAX_BYTES == 7
    assert owner._SEGMENT_MAX_BYTES is before
    assert sel_mod._SEGMENT_MAX_BYTES is before
    holders = sel_mod._MIRROR.holders["_on_event_loop"]
    assert {"kiro_crew.sel_chain_lock", "kiro_crew.sel_append"} <= set(holders)
    with monkeypatch.context() as patch:
        patch.setattr(sel_mod, "_on_event_loop", lambda: True)
        assert all(sys.modules[h]._on_event_loop() for h in holders)
        assert sel_mod._on_event_loop()
    assert vars(sel_mod)["_on_event_loop"] is vars(sys.modules[holders[0]])["_on_event_loop"]


# --------------------------------------------------------------------------- #
# The on-loop guard covers the whole family.
# --------------------------------------------------------------------------- #


def _imports_time(tree: ast.Module) -> list[int]:
    """Lines binding the ``time`` module, or anything from it, anywhere in *tree*."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "time" for a in node.names):
            found.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            found.append(node.lineno)
    return found


def test_the_time_import_scan_can_fail() -> None:
    tree = ast.parse("import time\ndef f():\n    from time import sleep\nimport timeit\n")
    assert _imports_time(tree) == [1, 3]


@pytest.mark.parametrize("module", [_FACADE, *sel_mod._OWNERS])
def test_no_member_of_the_family_imports_time(module: str) -> None:
    """The append and lock paths run on the event loop for a critical audit; a timed wait
    there stalls every session the loop serves, so none of this code may reach ``time``."""
    assert _imports_time(_tree(sys.modules[module])) == []
    assert "time" not in vars(sys.modules[module])
