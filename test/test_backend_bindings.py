"""``Backend`` keeps every member it had, and each is bound from the module that owns it.

The class's behaviour lives in the ``kiro_crew.mcp_gateway.backend_*`` owner modules: each
member's body is a module-level function there whose first parameter is ``self``, and the
class binds it by name (``m = _owner.m``). The dataclass's fields stay on the class, so
``Backend(...)``, tests that build a backend by hand and every caller see the same object
they always did. This file pins the binding contract:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, its
  signature (the ``self`` annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade or the class's module at run time, and each owner imports
  only owners listed before it;
* each owner's docstring names the instance fields its members write and read;
* owners log under the facade's logger, and a moved class keeps the facade as its module;
* a write through ``kiro_crew.mcp_gateway.backend`` reaches every owner that reads the name,
  for every name the test trees patch there (the facade's mirror, ``_facade_mirror``), and
  a star import binds what it bound before the split.
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
from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import Backend

pytestmark = pytest.mark.xdist_group(name="tree_scan_backend_bindings")

#: Member -> (kind, coroutine, signature digest, owning module). Frozen at the split: the
#: kind, coroutine flag and signature are BASE's; the owner is where the member lives now.
#: A dataclass field is "data" when it has a plain default (a class attribute) and
#: "annotation" when it has none or a ``default_factory``.
_MEMBERS: dict[str, tuple[str, bool, str, str]] = {
    "__init__": ("function", False, "5a1d3bbc33ff", "backend"),
    "__post_init__": ("function", False, "b60c5fea256e", "backend"),
    "_apply_inflight_report": ("function", False, "44c5cabfd721", "backend_routing"),
    "_apps_declared_uris": ("annotation", False, "", ""),
    "_apps_tasks": ("annotation", False, "", ""),
    "_arm_init_deadline": ("function", False, "b60c5fea256e", "backend_handshake"),
    "_audit_visibility_withhold": ("function", False, "549dd3ef6cc6", "backend_mcp_apps"),
    "_broadcast": ("function", True, "44c5cabfd721", "backend_stubs"),
    "_broadcast_backend_gone": ("function", True, "4decff571a3a", "backend_liveness"),
    "_cancel_background_tasks": ("function", True, "b60c5fea256e", "backend_liveness"),
    "_cancel_init_deadline": ("function", False, "b60c5fea256e", "backend_handshake"),
    "_dead_reason": ("data", False, "", ""),
    "_deliver_cached_initialize": ("function", True, "97dabdd4a6ab", "backend_handshake"),
    "_deliver_to_stub": ("function", True, "5c4ede43d497", "backend_stubs"),
    "_drain_replacement_subscribes": ("function", True, "ae52d10d808b", "backend_leases"),
    "_enqueue_to_stub": ("function", True, "1c7ff33f6400", "backend_stubs"),
    "_fail_claimed_request": ("function", True, "c35a6d7d959b", "backend_routing"),
    "_fail_init": ("function", True, "98b3eb208330", "backend_handshake"),
    "_fail_oversize_request": ("function", True, "b410c0f7cf09", "backend_routing"),
    "_fail_unparseable_response": ("function", True, "3a5d34ab8233", "backend_routing"),
    "_fetch_and_deliver_ui": ("function", True, "7a07cdb6d3e6", "backend_mcp_apps"),
    "_forward_id_seq": ("data", False, "", ""),
    "_gone_broadcast": ("data", False, "", ""),
    "_gone_replay_uris": ("annotation", False, "", ""),
    "_grant_callers": ("annotation", False, "", ""),
    "_handle_initialize": ("function", True, "5c4ede43d497", "backend_handshake"),
    "_handle_resource_subscription": ("function", True, "62a32d39421c", "backend_leases"),
    "_handle_stdout_line": ("function", True, "3a5d34ab8233", "backend_routing"),
    "_heartbeat_once": ("function", True, "88f7fd52fdeb", "backend_liveness"),
    "_inbox_lock": ("annotation", False, "", ""),
    "_init_deadline": ("function", True, "88f413960dbe", "backend_handshake"),
    "_init_deadline_task": ("data", False, "", ""),
    "_init_done_event": ("annotation", False, "", ""),
    "_init_first_id": ("data", False, "", ""),
    "_init_first_stub": ("data", False, "", ""),
    "_init_pending": ("annotation", False, "", ""),
    "_init_result": ("data", False, "", ""),
    "_init_state": ("data", False, "", ""),
    "_isolate_stdout_line": ("function", True, "8429f76851d1", "backend_routing"),
    "_last_ping_response_mono": ("data", False, "", ""),
    "_lease_awaiting_grant": ("annotation", False, "", ""),
    "_lease_awaiting_release": ("annotation", False, "", ""),
    "_lease_pending_riders": ("annotation", False, "", ""),
    "_lease_release_waiters": ("annotation", False, "", ""),
    "_lease_replacement_subscribes": ("annotation", False, "", ""),
    "_maybe_intercept_ui_result": ("function", True, "40325cccaa1c", "backend_mcp_apps"),
    "_metric_tasks": ("annotation", False, "", ""),
    "_next_forward_id": ("function", False, "bb418bb7ece8", "backend_stubs"),
    "_notification_owner": ("function", False, "b9965eeb1d4b", "backend_routing"),
    "_now": ("staticmethod", False, "68f8b223e000", "backend"),
    "_on_resource_subscription_response": ("function", True, "54ff2e0a03d4", "backend_leases"),
    "_on_upstream_initialize": ("function", True, "548096ba86f3", "backend_handshake"),
    "_orphaned_leases": ("annotation", False, "", ""),
    "_parse_ui_contents": ("function", False, "cfabfdc984f2", "backend_mcp_apps"),
    "_pending_requests": ("annotation", False, "", ""),
    "_read_ui_resource": ("function", True, "ad837865fef2", "backend_mcp_apps"),
    "_record_hazard": ("function", False, "bdef4dec7d1f", "backend_routing"),
    "_rekey_generation": ("annotation", False, "", ""),
    "_release_upstream_subscriptions": ("function", True, "cc3d421ddfc2", "backend_leases"),
    "_reply_locally": ("function", True, "f8a4fcb66c0f", "backend_stubs"),
    "_resource_subscriptions": ("annotation", False, "", ""),
    "_resource_update_targets": ("function", False, "0b53913c1bee", "backend_leases"),
    "_retry_after_rehandshake": ("function", True, "a4278c137cf6", "backend_handshake"),
    "_route_backend_line": ("function", True, "3a5d34ab8233", "backend_routing"),
    "_route_backend_message": ("function", True, "90b4f8210134", "backend_routing"),
    "_served_tool_surfaces": ("annotation", False, "", ""),
    "_settle": ("function", True, "54ff2e0a03d4", "backend_routing"),
    "_shutdown_lock": ("annotation", False, "", ""),
    "_spawn_metric_task": ("function", False, "13f007277a39", "backend_routing"),
    "_stderr_task": ("data", False, "", ""),
    "_stdout_task": ("data", False, "", ""),
    "_stub_agents": ("annotation", False, "", ""),
    "_stub_inboxes": ("annotation", False, "", ""),
    "_stub_subscription_count": ("function", False, "b2d36fe0080b", "backend_leases"),
    "_upstream_init_frame": ("data", False, "", ""),
    "_warned_slow_ids": ("annotation", False, "", ""),
    "agent_for_stub": ("function", False, "a6b1ba1c75aa", "backend"),
    "attach_stub": ("function", True, "df6acb5aab58", "backend_stubs"),
    "attempt_id": ("annotation", False, "", ""),
    "backend_generation_id": ("annotation", False, "", ""),
    "cancel_in_flight_for_stub": ("function", True, "79d0f58f3397", "backend_stubs"),
    "carry_served_tool_surface": ("function", False, "e9efe0f115a2", "backend_stubs"),
    "control_plane": ("data", False, "", ""),
    "control_plane_denial": ("data", False, "", ""),
    "created_at": ("annotation", False, "", ""),
    "dead_reason": ("property", False, "04e00addceec", "backend"),
    "detach_stub": ("function", True, "b2d36fe0080b", "backend_stubs"),
    "evict_stub_subscriptions": ("function", True, "b2d36fe0080b", "backend_leases"),
    "exclusive_token": ("data", False, "", ""),
    "forward_from_stub": ("function", True, "99b43ef920a3", "backend_stubs"),
    "initialize_timeout_secs": ("data", False, "", ""),
    "is_alive": ("property", False, "a6632a57e602", "backend"),
    "last_used_at": ("annotation", False, "", ""),
    "lifecycle_fields": ("function", False, "5e8e0b6328bb", "backend"),
    "outstanding_work": ("property", False, "fa6cfecd5574", "backend"),
    "pid": ("property", False, "fcad3a249a2d", "backend"),
    "pinned": ("data", False, "", ""),
    "pool_key": ("annotation", False, "", ""),
    "prime_initialize": ("function", True, "ef091ff6364a", "backend_handshake"),
    "probe_tool_surface": ("function", True, "f047db7ce940", "backend_stubs"),
    "process": ("annotation", False, "", ""),
    "quarantined": ("data", False, "", ""),
    "recycle_if_idle": ("function", True, "a6632a57e602", "backend_liveness"),
    "refcount": ("data", False, "", ""),
    "replay_resource_subscriptions": ("function", True, "1c4ffd5899ac", "backend_leases"),
    "reports_inflight": ("data", False, "", ""),
    "resource_subscription_uris": ("function", False, "79d0f58f3397", "backend_leases"),
    "run_stdout_pump": ("function", True, "b60c5fea256e", "backend_routing"),
    "served_tool_surface": ("function", False, "0fd2a8171ea2", "backend_stubs"),
    "shutdown": ("function", True, "a9c45e0c08a5", "backend_liveness"),
    "start_id": ("data", False, "", ""),
    "stdin": ("annotation", False, "", ""),
    "stdout": ("annotation", False, "", ""),
    "storage_digest": ("property", False, "bb418bb7ece8", "backend"),
    "supports_caller_identity": ("data", False, "", ""),
    "touch": ("function", False, "ffb9b3a10afb", "backend"),
}

_PACKAGE = "kiro_crew.mcp_gateway"
_FACADE = backend_mod.__name__

#: What ``from kiro_crew.mcp_gateway.backend import *`` bound before the split: every
#: public name of the unsplit module's namespace.
_BASE_STAR_NAMES = frozenset("""
    Any Awaitable Backend BackendGone CALLER_CAPABILITY_KEY CALLER_META_KEY
    CANONICAL_TEMP_KEYS CallerContext HARD_WEDGE_CEILING_SECS HEARTBEAT_PING_ID
    HEARTBEAT_TIMEOUT_SECS ID_PROBE_BYTES INFLIGHT_CAPABILITY_KEY INFLIGHT_NOTIFICATION
    INTERNAL_STUB_PREFIXES KIROCREW_SPAWNED_ENV KIROCREW_SPAWNED_VALUE MCP_APPS_ENV_FALSE
    MCP_APPS_ENV_FLAG MCP_APPS_ENV_TRUE MCP_APPS_EXTENSION_KEY MCP_APPS_MIME_TYPE Mapping
    Optional PING_STALE_SECS POOLED_BACKEND_ENV POOLED_BACKEND_VALUE
    POOLED_RESPAWN_COMMAND_ENV PROGRESS_WEDGE_CEILING_SECS Path READ_BUFFER_LIMIT_BYTES
    RESPONSE_SPILL_THRESHOLD_BYTES SUBAGENT_TIMEOUT_SECS SecurityEventLog TENANT_META_KEY
    TOOL_SURFACE_STUB_PREFIX ToolSurface WithheldTools allocate_backend_tmp annotations
    append_marker asyncio base64 binascii build_caller_meta build_tenant_meta
    classify_declared_temp_env contextlib dataclass declared_temp_refusal_reasons
    extract_declared_ui_uris extract_ui_resource_uri failure_name field
    format_declared_temp_refusals has_recyclable_in_flight hazards image_executor json
    lifecycle_log line_may_carry_image_block logger logging maintenance_executor
    maybe_spill_response mcp_apps_env_override os parse_image_bearing_frame
    parse_json_object_line platform_compat project_tool_surface record_owner recover_line_id
    recover_top_level_id redact replace rewrite_image_frame send_initialize spawn_backend
    strip_model_hidden_tools sweep_backend_tmp time tmp_env uuid with_kill_failure
    write_spool
""".split())


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in backend_mod._BACKEND_OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(Backend)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first module-level function."""
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
        for name in vars(Backend)
        if not (name.startswith("__") and name.endswith("__"))
        or name in ("__init__", "__post_init__")
    }
    present |= set(Backend.__dict__.get("__annotations__", {}))
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, digest, owner = _MEMBERS[name]
    if kind in ("data", "annotation"):
        assert (name in vars(Backend)) is (kind == "data")
        assert name in Backend.__dataclass_fields__
        return
    value = vars(Backend)[name]
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) is coroutine
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"{_PACKAGE}.{owner}"
    if owner != "backend":
        assert vars(sys.modules[f"{_PACKAGE}.{owner}"])[name] is fn


def test_every_owner_lists_in_the_facade_and_holds_its_members() -> None:
    homes = {owner for _kind, _c, _d, owner in _MEMBERS.values() if owner and owner != "backend"}
    listed = {name.rsplit(".", 1)[-1] for name in backend_mod._BACKEND_OWNERS}
    assert homes <= listed


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
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
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    tree = _tree(sys.modules[owner])
    assert {fn.name: _class_only_constructs(fn) for fn in _self_functions(tree)} == {
        fn.name: [] for fn in _self_functions(tree)
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import the facade outside a ``TYPE_CHECKING`` block."""
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
                node.module == _PACKAGE and any(a.name == "backend" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == _FACADE for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    # Spelled from the names, so the corpus scans do not read an import of the facade here.
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        f"if TYPE_CHECKING:\n    from {_FACADE} import Backend\n"
        f"def f():\n    from {_FACADE} import x\n"
        f"import {_FACADE}\n"
        f"from {_PACKAGE} import {_FACADE.rpartition('.')[2]}\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


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
        "extend",
        "remove",
        "insert",
    }
)

#: The members that are callables (functions, properties, static methods): a ``self.<name>``
#: of one of these is a call into the class, not a field.
_CALLABLES = frozenset(
    name for name, row in _MEMBERS.items() if row[0] not in ("data", "annotation")
)


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
        aliases, field_names = _aliases(fn)

        def targets(node: ast.AST) -> set[str]:
            name = _self_field(node)
            if name is not None:
                return {name}
            return aliases.get(node.id, set()) if isinstance(node, ast.Name) else set()

        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                written |= targets(node.value)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                written |= targets(func.value)
            if (
                isinstance(func, ast.Name)
                and func.id in ("getattr", "hasattr", "setattr")
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "self"
            ):
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    named = {arg.value}
                else:
                    named = field_names.get(arg.id, set()) if isinstance(arg, ast.Name) else set()
                (written if func.id == "setattr" else read).update(named)
    return written - _CALLABLES, read - written - _CALLABLES


def _aliases(fn: ast.AST) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Local names that stand for ``self`` fields: a loop or an assignment over the
    fields themselves (``for t in (self.a, self.b)``, ``t = self.a``), and a loop over a
    literal tuple of field-name strings (``for attr in ("a", "b")``) read through
    ``getattr``/``setattr``."""
    aliases: dict[str, set[str]] = {}
    field_names: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            items = node.iter.elts if isinstance(node.iter, (ast.Tuple, ast.List)) else []
            named = [_self_field(e) for e in items]
            if items and all(named):
                aliases.setdefault(node.target.id, set()).update(n for n in named if n)
            if items and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in items
            ):
                field_names.setdefault(node.target.id, set()).update(e.value for e in items)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = _self_field(node.value)
            if name is not None and isinstance(node.targets[0], ast.Name):
                aliases.setdefault(node.targets[0].id, set()).add(name)
    return aliases, field_names


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    for t in (self.k, self.l):\n        t.clear()\n"
        "    m = self.m\n    m[k] = v\n"
        "    for attr in ('n', 'o'):\n        setattr(self, attr, None)\n"
        "    for name in ('p',):\n        getattr(self, name)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e, self.touch()\n"
    )
    assert _fields([tree.body[0]]) == (
        {"a", "b", "c", "d", "e", "f", "g", "k", "l", "m", "n", "o"},
        {"h", "i", "j", "p"},
    )


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module)))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
def test_owners_log_under_the_facade_logger(owner: str) -> None:
    module = sys.modules[owner]
    assert getattr(module, "logger", backend_mod.logger) is backend_mod.logger
    assert backend_mod.logger.name == _FACADE


@pytest.mark.parametrize("owner", backend_mod._BACKEND_OWNERS)
def test_a_moved_class_keeps_the_facade_as_its_module(owner: str) -> None:
    """Error chains and tracebacks render a class as ``module.qualname``; a class that
    moved to an owner keeps naming the path callers import it from."""
    module = sys.modules[owner]
    defined = [n.name for n in _tree(module).body if isinstance(n, ast.ClassDef)]
    assert {name: vars(module)[name].__module__ for name in defined} == {
        name: _FACADE for name in defined
    }


def _owner_imports(tree: ast.Module) -> set[str]:
    """The backend owner modules *tree* imports, by dotted name."""
    owners = set(backend_mod._BACKEND_OWNERS)
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
        "from kiro_crew.mcp_gateway.backend_wire import X\n"
        "from kiro_crew.mcp_gateway import backend_leases as _l, backend_tmp\n"
        "import kiro_crew.mcp_gateway.backend_mcp_apps\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.mcp_gateway.backend_wire",
        "kiro_crew.mcp_gateway.backend_leases",
        "kiro_crew.mcp_gateway.backend_mcp_apps",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_BACKEND_OWNERS`` lists the owners lowest layer first: an owner imports only
    owners earlier in the tuple, so the order is the import order."""
    owners = backend_mod._BACKEND_OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


# --------------------------------------------------------------------------- #
# A write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #


def _test_tree_texts() -> list[str]:
    """The text of every file pytest collects tests or fixtures from (the root conftest,
    ``test/``, and the builtin apps' test modules) that names the gateway package."""
    root = source_corpus.repo_root()
    texts = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if not (
            rel == "conftest.py"
            or rel.startswith("test/")
            or (rel.startswith("src/kiro_crew/apps/builtins/") and "/tests/" in rel)
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "mcp_gateway" in text:
            texts.append(text)
    return texts


def _patched_through_the_facade() -> set[str]:
    """Every name a test-tree file patches through ``kiro_crew.mcp_gateway.backend``."""
    spellings = {_FACADE: _FACADE}
    names: set[str] = set()
    for text in _test_tree_texts():
        try:
            found = facade_mirror_scan.patched_names(text, spellings).get(_FACADE, set())
        except SyntaxError:
            continue
        names |= {name for name in found if not name.startswith("<")}
    return names


def test_a_sibling_module_import_is_no_alias_of_the_facade() -> None:
    """``facade_tmp as ft`` names a sibling module, so a patch on a local ``facade``
    object beside it is no patch through the facade; a bare ``facade`` import is one.
    Spelled on a synthetic package: this file is itself read by the corpus scans."""
    spellings = {"pkg.facade": "pkg.facade"}
    beside = (
        "from pkg import facade as facade_mod\n"
        "from pkg import facade_tmp as ft\n"
        "def test_x(monkeypatch, facade):\n"
        "    monkeypatch.setattr(facade, '_member', None)\n"
        "    monkeypatch.setattr(facade_mod, '_LIMIT', None)\n"
    )
    bare = (
        "from pkg import facade\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(facade, '_LIMIT', None)\n"
    )
    assert facade_mirror_scan.text_patched_names(beside, spellings) == {"pkg.facade": {"_LIMIT"}}
    assert facade_mirror_scan.text_patched_names(bare, spellings) == {"pkg.facade": {"_LIMIT"}}


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
    # The scan saw the patches this split had to keep reaching moved code.
    assert {"logger", "_METRICS_PATH", "_write_json_line", "SecurityEventLog"} <= patched
    assert _reach_gaps(patched, backend_mod._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = backend_mod._MIRROR
    owner = "kiro_crew.mcp_gateway.backend_routing"
    holders = dict(index.holders)
    holders["logger"] = tuple(h for h in holders["logger"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"logger"}, backend_mod._MIRROR) == []
    assert _reach_gaps({"logger"}, blind) == [f"{owner}.logger"]


def test_the_mirror_indexes_one_object_per_name_and_serves_only_moved_names() -> None:
    index = backend_mod._MIRROR
    assert index.excluded == ()
    assert index.divergent() == ()
    assert dict(index.forwards) == {}
    # Every name the facade serves without binding is one an owner defines at module
    # level; the members the class binds are never module attributes of the facade.
    defined = {
        name: owner.__name__
        for owner in _owners()
        for name in _facade_mirror._defined_names(_tree(owner))
    }
    assert {name: defined.get(name) for name in backend_mod._EXPORTS} == backend_mod._EXPORTS
    assert not set(backend_mod._EXPORTS) & set(_MEMBERS)


def test_a_star_import_binds_what_it_bound_before_the_split() -> None:
    assert sorted(set(backend_mod.__all__) ^ _BASE_STAR_NAMES) == []


def test_a_patch_through_the_facade_lands_on_every_holder_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holders = backend_mod._MIRROR.holders["_write_json_line"]
    assert len(holders) > 1
    before = {name: vars(sys.modules[name])["_write_json_line"] for name in holders}
    sentinel = object()
    with monkeypatch.context() as patch:
        patch.setattr(backend_mod, "_write_json_line", sentinel)
        assert backend_mod._write_json_line is sentinel
        assert {name: vars(sys.modules[name])["_write_json_line"] for name in holders} == {
            name: sentinel for name in holders
        }
    assert {name: vars(sys.modules[name])["_write_json_line"] for name in holders} == before
