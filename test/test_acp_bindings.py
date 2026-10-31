"""``AcpProvider`` keeps every member it had, and each is bound from the module that owns it.

The class's behaviour lives in the ``kiro_crew.providers.acp_*`` owner modules: each moved
member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance
state stays on the instance, so tests, ``AcpProvider.__new__`` builds and
``MagicMock(spec=AcpProvider)`` see the same members they always did. This file pins the
binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine or an
  async generator, its signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* owners log under the facade's logger;
* a write through ``kiro_crew.providers.acp`` reaches every owner that reads the name, for
  every name the test trees patch there (the facade's mirror, ``_facade_mirror``), and the
  facade reads no name it serves as a bare global.
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
from facade_mirror_scan import needs_syntax_tree, patched_names, text_patched_names

from kiro_crew.providers import acp as acp_provider
from kiro_crew.providers.acp import AcpProvider

pytestmark = pytest.mark.xdist_group(name="tree_scan_acp_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, object, str, str]] = {
    "__init__": ("function", False, "22e43ccb505a", "acp"),
    "_abc_impl": ("data", False, "", ""),
    "_advertised_effort_levels": ("function", False, "93b23ca4342b", "acp_model_effort"),
    "_apply_effort_overlay": ("function", False, "66550c0d40fa", "acp_cli_overlay"),
    "_apply_initial_effort": ("function", True, "b60c5fea256e", "acp_model_effort"),
    "_apply_tool_search_overlay": ("function", False, "b60c5fea256e", "acp_cli_overlay"),
    "_bind_unbound_effort": ("function", True, "b60c5fea256e", "acp_model_effort"),
    "_effort_model": ("property", False, "bb418bb7ece8", "acp"),
    "_emit_kiro_startup_metric": ("function", False, "4c7cfd604f41", "acp_runtime_start"),
    "_inline_turn_finished_cleanly": ("function", False, "a6632a57e602", "acp_turns"),
    "_load_session_with_retry": ("function", True, "18f2e0d85137", "acp_runtime_start"),
    "_member_session_key": ("function", False, "bb418bb7ece8", "acp_runtime_start"),
    "_note_zero_tool_servers": ("function", True, "b60c5fea256e", "acp_runtime_start"),
    "_owning_channel_id": ("function", False, "f21e39ec5ffb", "acp_runtime_start"),
    "_owning_session_key": ("function", False, "bb418bb7ece8", "acp_runtime_start"),
    "_ready_confirmed_session": ("function", True, "b60c5fea256e", "acp_turns"),
    "_reclaim_work_dir": ("function", True, "b60c5fea256e", "acp_lifecycle"),
    "_resolve_effort": ("function", False, "f21e39ec5ffb", "acp_model_effort"),
    "_set_effort_config_option": ("function", True, "b9de7871fd78", "acp_model_effort"),
    "_start_kiro_runtime": ("function", True, "b60c5fea256e", "acp_runtime_start"),
    "_start_kiro_runtime_impl": ("function", True, "bb38224e64f4", "acp_runtime_start"),
    "_to_llm_event": ("staticmethod", False, "9400e81ecdfb", "acp_turns"),
    "_tool_search_settings": ("function", False, "da838953900b", "acp"),
    "accepts_effort_level": ("function", False, "581451d2b011", "acp_model_effort"),
    "accepts_elicitation": ("property", False, "a6632a57e602", "acp"),
    "agent_prompt_upgrade_notice": ("property", False, "bb418bb7ece8", "acp"),
    "agent_version": ("property", False, "bb418bb7ece8", "acp"),
    "answer_elicitation": ("function", True, "2fcb1ac189fa", "acp"),
    "approval_withdrawable": ("data", False, "", ""),
    "approve_tool": ("function", True, "d240c0f882a7", "acp"),
    "available_models": ("function", False, "92d6193dbe19", "acp"),
    "background_launch": ("function", False, "d85112f1c4e4", "acp"),
    "billing_stats": ("function", False, "8a5937500587", "acp"),
    "cancel": ("function", True, "c25facc6ab96", "acp_turns"),
    "capabilities": ("property", False, "83534fc4b1b5", "acp"),
    "capability_projection_gaps": ("property", False, "9d5ab4ea1828", "acp"),
    "change_effort": ("function", True, "581451d2b011", "acp_model_effort"),
    "child_fidelity_aware": ("property", False, "a6632a57e602", "acp"),
    "cleanup_session": ("function", True, "c284282f8a1d", "acp_lifecycle"),
    "clear_effort": ("function", True, "16180c63d5e6", "acp_model_effort"),
    "client": ("property", False, "4d5584e1d14b", "acp"),
    "compact": ("function", True, "416a3df991e6", "acp_turns"),
    "compaction_self_managed": ("property", False, "a6632a57e602", "acp"),
    "compaction_unmanaged_backend": ("property", False, "f21e39ec5ffb", "acp"),
    "context_incarnation": ("property", False, "c22fa5e2888a", "acp"),
    "context_provider_type": ("property", False, "bb418bb7ece8", "acp"),
    "context_usage_pct": ("function", False, "59162e39f6f1", "acp"),
    "context_usage_unknown": ("function", False, "a6632a57e602", "acp"),
    "context_used_tokens": ("function", False, "fa6cfecd5574", "acp"),
    "context_window_tokens": ("function", False, "fa6cfecd5574", "acp"),
    "cwd": ("property", False, "bb418bb7ece8", "acp"),
    "defer_replay_sid_promotion": ("property", False, "a6632a57e602", "acp"),
    "discard_unanswered_prompt": ("function", True, "a6632a57e602", "acp"),
    "disown_work_dir": ("function", False, "b60c5fea256e", "acp_lifecycle"),
    "exit_code": ("property", False, "8dc15be1dfda", "acp"),
    "get_valid_effort_levels": ("function", False, "6806e867af4e", "acp"),
    "harness_managed_compaction_backend": ("property", False, "f21e39ec5ffb", "acp"),
    "has_active_turn": ("function", False, "a6632a57e602", "acp"),
    "has_unfinished_turn": ("function", False, "a6632a57e602", "acp"),
    "is_acp_runtime_backend": ("property", False, "a6632a57e602", "acp"),
    "is_alive": ("function", False, "a6632a57e602", "acp"),
    "is_claude_backend": ("property", False, "a6632a57e602", "acp"),
    "is_codex_backend": ("property", False, "a6632a57e602", "acp"),
    "is_copilot_backend": ("property", False, "a6632a57e602", "acp"),
    "is_deepseek_backend": ("property", False, "a6632a57e602", "acp"),
    "is_goose_backend": ("property", False, "a6632a57e602", "acp"),
    "is_kas_backend": ("property", False, "a6632a57e602", "acp"),
    "is_kiro_backend": ("property", False, "a6632a57e602", "acp"),
    "is_opencode_backend": ("property", False, "a6632a57e602", "acp"),
    "is_pi_backend": ("property", False, "a6632a57e602", "acp"),
    "is_strands_backend": ("property", False, "a6632a57e602", "acp"),
    "is_process_alive": ("function", False, "a6632a57e602", "acp"),
    "is_session_sharing_eligible": ("property", False, "a6632a57e602", "acp"),
    "kas_active_mode_id": ("property", False, "bb418bb7ece8", "acp"),
    "kas_auto_approved_capabilities": ("property", False, "45debfdb1d02", "acp"),
    "kas_projected_agent": ("property", False, "bb418bb7ece8", "acp"),
    "kas_registered_agents": ("property", False, "908b88c4efab", "acp"),
    "last_compaction_transient": ("property", False, "a6632a57e602", "acp"),
    "last_infra_error": ("property", False, "134c3f4bdad2", "acp"),
    "last_steer_monotonic": ("property", False, "59162e39f6f1", "acp"),
    "loaded_capability_template": ("property", False, "bb418bb7ece8", "acp"),
    "manual_compact_unsupported_backend": ("property", False, "f21e39ec5ffb", "acp"),
    "mate_worker_kit_supported": ("property", False, "a6632a57e602", "acp"),
    "mate_worker_prompt_in_context": ("property", False, "a6632a57e602", "acp"),
    "mate_worker_tool_limits_supported": ("property", False, "a6632a57e602", "acp"),
    "maybe_refresh_available_models": ("function", True, "9de63ef05543", "acp_model_effort"),
    "mark_mcp_config_changed": ("function", False, "b60c5fea256e", "acp"),
    "mcp_config_hot_reload": ("property", False, "a6632a57e602", "acp"),
    "mcp_config_wire_reinject": ("property", False, "a6632a57e602", "acp"),
    "mcp_session_report": ("function", False, "89ea891969f7", "acp"),
    "member_capabilities_supported": ("property", False, "a6632a57e602", "acp"),
    "member_dispatch_mounted": ("property", False, "16180c63d5e6", "acp"),
    "member_tool_limits_supported": ("property", False, "a6632a57e602", "acp"),
    "model_pin_partial": ("property", False, "bb418bb7ece8", "acp"),
    "model_pin_refused": ("property", False, "bb418bb7ece8", "acp"),
    "native_context_documents": ("property", False, "8cab7a4db01b", "acp"),
    "native_steering": ("property", False, "a6632a57e602", "acp"),
    "new_conversation": ("function", True, "b60c5fea256e", "acp"),
    "note_user_message": ("function", False, "ce7410240196", "acp"),
    "process_instance": ("property", False, "bb418bb7ece8", "acp"),
    "prompt_timeout_for_deadline": ("function", False, "b97d05f9c710", "acp"),
    "reapply_live_effort": ("function", True, "581451d2b011", "acp_model_effort"),
    "reject_tool": ("function", True, "84bed4474307", "acp"),
    "reports_effort_support": ("data", False, "", ""),
    "reports_turn_usage": ("property", False, "a6632a57e602", "acp"),
    "served_model": ("property", False, "bb418bb7ece8", "acp"),
    "session_id": ("property", False, "bb418bb7ece8", "acp"),
    "set_model": ("function", True, "9c3df1e3fc63", "acp_model_effort"),
    "set_work_dir_claim_probe": ("function", False, "5fa0fa7a5959", "acp_lifecycle"),
    "settings_withheld_cause": ("function", False, "bb418bb7ece8", "acp"),
    "shutdown": ("function", True, "b60c5fea256e", "acp_lifecycle"),
    "start": ("function", True, "b60c5fea256e", "acp_lifecycle"),
    "steer": ("function", True, "b0a837a37097", "acp_turns"),
    "steer_needs_loss_recovery": ("property", False, "a6632a57e602", "acp"),
    "steer_upgrade_notice": ("property", False, "bb418bb7ece8", "acp"),
    "stream": ("function", "agen", "e4e5c768b805", "acp_turns"),
    "stream_command": ("function", "agen", "c5944af5c808", "acp_turns"),
    "supports_effort": ("function", False, "a6632a57e602", "acp_model_effort"),
    "supports_refusal_steer": ("property", False, "a6632a57e602", "acp"),
    "supports_steer": ("property", False, "a6632a57e602", "acp"),
    "tool_search_settings": ("property", False, "da838953900b", "acp"),
    "touch_activity": ("function", False, "b60c5fea256e", "acp"),
    "uses_kiro_identity_store": ("property", False, "a6632a57e602", "acp"),
    "wait_for_compaction": ("function", True, "1d8ef804d52e", "acp_turns"),
    "wait_for_declared_mcp": ("function", True, "921c3727b83c", "acp"),
    "wait_turn_done": ("function", True, "48da118c69ff", "acp"),
    "work_scratch_dir": ("property", False, "d921fd8d443d", "acp"),
}

#: Class-level annotations the split adds: fields first set by a member an owner defines,
#: declared in the class so type checkers know them. An annotation creates no attribute.
_DECLARED = frozenset({"_picker_probe_at"})

_PACKAGE = "kiro_crew.providers"
_FACADE = acp_provider.__name__


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in acp_provider._ACP_PROVIDER_OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(AcpProvider)[name]
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
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home and f"{_PACKAGE}.{home}" == owner
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
        for name in vars(AcpProvider)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    present |= set(AcpProvider.__dict__.get("__annotations__", {}))
    assert sorted(present ^ (set(_MEMBERS) | _DECLARED)) == []
    assert not _DECLARED & set(vars(AcpProvider))


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind == "data":
        assert name in vars(AcpProvider)
        return
    value = vars(AcpProvider)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != "acp":
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != "acp"}
    listed = {name.rsplit(".", 1)[-1] for name in acp_provider._ACP_PROVIDER_OWNERS}
    assert homes == listed


@pytest.mark.parametrize("owner", acp_provider._ACP_PROVIDER_OWNERS)
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
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", acp_provider._ACP_PROVIDER_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree, owner)} == {
        fn.name: [] for fn in _self_functions(tree, owner)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade (``kiro_crew.providers.acp``) outside a
    ``TYPE_CHECKING`` block."""
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
                node.module == _PACKAGE and any(a.name == "acp" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.providers.acp import AcpProvider\n"
        "def f():\n    from kiro_crew.providers.acp import x\n"
        "import kiro_crew.providers.acp\n"
        "from kiro_crew.providers import acp\n"
        "from kiro_crew.providers import acp_turns\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", acp_provider._ACP_PROVIDER_OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


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


@pytest.mark.parametrize("owner", acp_provider._ACP_PROVIDER_OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", acp_provider._ACP_PROVIDER_OWNERS)
def test_owners_log_under_the_provider_logger(owner: str) -> None:
    assert vars(sys.modules[owner])["logger"] is acp_provider.logger
    assert acp_provider.logger.name == "kiro_crew.providers.acp"


def test_the_bound_function_scans_include_every_static_method() -> None:
    statics = {
        name: f"{_PACKAGE}.{home}"
        for name, (kind, _, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and home.startswith("acp_")
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in _self_functions(_tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


def _owner_imports(tree: ast.Module) -> set[str]:
    """The provider owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(f"{_PACKAGE}.acp_"):
                found.add(node.module)
            elif node.module == _PACKAGE:
                found.update(
                    f"{_PACKAGE}.{a.name}" for a in node.names if a.name.startswith("acp_")
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith(f"{_PACKAGE}.acp_"))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.providers.acp_turns import X\n"
        "from kiro_crew.providers import acp_lifecycle as _lc, base\n"
        "import kiro_crew.providers.acp_model_effort\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.providers.acp_turns",
        "kiro_crew.providers.acp_lifecycle",
        "kiro_crew.providers.acp_model_effort",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_ACP_PROVIDER_OWNERS`` lists the owners lowest layer first: an owner imports only
    owners earlier in the tuple, so the order is the import order."""
    owners = acp_provider._ACP_PROVIDER_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #

_TEST_ROOT = Path(__file__).resolve().parent
_SPELLINGS = {_FACADE: _FACADE}


def _patched_through_the_facade() -> set[str]:
    """Every name a test file patches through ``kiro_crew.providers.acp``: the regular
    expressions read every file, and the syntax tree the files holding a patch shape the
    expressions cannot read."""
    names: set[str] = set()
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "kiro_crew.providers" not in text:
            continue
        names |= text_patched_names(text, _SPELLINGS).get(_FACADE, set())
        if not needs_syntax_tree(text, _SPELLINGS):
            continue
        try:
            names |= patched_names(text, _SPELLINGS).get(_FACADE, set())
        except SyntaxError:
            continue
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


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan saw names the owners read: a client, a runtime, a moved helper, a path.
    assert {"AcpClient", "AcpRuntime", "_write_cli_overlay", "kiro_sessions_dir"} <= patched
    assert _reach_gaps(patched, acp_provider._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = acp_provider._MIRROR
    owner = "kiro_crew.providers.acp_lifecycle"
    holders = dict(index.holders)
    holders["kiro_sessions_dir"] = tuple(h for h in holders["kiro_sessions_dir"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"kiro_sessions_dir"}, index) == []
    assert _reach_gaps({"kiro_sessions_dir"}, blind) == [f"{owner}.kiro_sessions_dir"]


def test_the_mirror_indexes_one_object_per_name_and_serves_only_moved_helpers() -> None:
    index = acp_provider._MIRROR
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}
    # What the facade serves without binding is exactly the moved module-level helpers
    # its own code does not read, each from the owner that defines it.
    assert set(index.exports) == set(acp_provider._SERVED)
    assert dict(index.exports) == acp_provider._EXPORTS
    for name, owner in index.exports.items():
        assert name not in vars(acp_provider)
        assert getattr(acp_provider, name) is vars(sys.modules[owner])[name]


def _bare_loads(tree: ast.Module, served: set[str]) -> list[tuple[int, str]]:
    """Every bare read of a served name outside an import statement."""
    import_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sorted(
        (node.lineno, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in served
        and node.lineno not in import_lines
    )


def test_the_facade_reads_no_served_name_as_a_bare_global() -> None:
    """A function defined in the facade resolves a bare global through the facade's own
    namespace, which the mirror's ``__getattr__`` never sees, so such a read would need
    the facade to bind the name -- a second copy no patch of the owner reaches."""
    assert _bare_loads(_tree(acp_provider), set(acp_provider._EXPORTS)) == []


def test_the_bare_global_scan_can_fail() -> None:
    tree = ast.parse("from x import y\n\ndef f():\n    return _write_cli_overlay\n")
    assert _bare_loads(tree, {"_write_cli_overlay"}) == [(4, "_write_cli_overlay")]
    assert (
        _bare_loads(
            ast.parse("from x import (\n    _write_cli_overlay,\n)\n"), {"_write_cli_overlay"}
        )
        == []
    )


def test_a_patch_through_the_facade_lands_on_every_owner_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = sys.modules["kiro_crew.providers.acp_runtime_start"]
    lifecycle = sys.modules["kiro_crew.providers.acp_lifecycle"]
    before = acp_provider.kiro_sessions_dir
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(acp_provider, "kiro_sessions_dir", sentinel)
        assert start.kiro_sessions_dir is sentinel
        assert lifecycle.kiro_sessions_dir is sentinel
        assert acp_provider.kiro_sessions_dir is sentinel
    assert start.kiro_sessions_dir is before
    assert lifecycle.kiro_sessions_dir is before
    assert acp_provider.kiro_sessions_dir is before


def test_a_patch_of_a_served_helper_lands_on_its_readers_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = sys.modules["kiro_crew.providers.acp_cli_overlay"]
    effort = sys.modules["kiro_crew.providers.acp_model_effort"]
    before = overlay._clear_cli_overlay_effort
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(acp_provider, "_clear_cli_overlay_effort", sentinel)
        assert overlay._clear_cli_overlay_effort is sentinel
        assert effort._clear_cli_overlay_effort is sentinel
        assert "_clear_cli_overlay_effort" not in vars(acp_provider)
    assert overlay._clear_cli_overlay_effort is before
    assert effort._clear_cli_overlay_effort is before
    assert acp_provider._clear_cli_overlay_effort is before
