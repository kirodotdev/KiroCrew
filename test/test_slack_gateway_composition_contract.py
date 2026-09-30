"""The Slack gateway stays one namespace while its owners live in ``gateway_runtime``.

``kiro_crew.slack.gateway`` keeps its import path, its patch surface and every
construct a path-keyed repository guard reads in that file; the modules of
``kiro_crew.slack.gateway_runtime`` hold the rest, one responsibility each, and
``gateway_runtime.compose`` runs every function they define on the facade's
globals. These tests pin what that composition promises:

* each owner function runs on the facade's globals, and every global it reads
  resolves there, so a patch of ``kiro_crew.slack.gateway.<name>`` reaches it;
* each moved name keeps the kind and signature it had in the one-module file,
  and is ONE object whichever module a caller reads it from;
* a function's ``__module__`` / ``__qualname__`` still name the facade and the
  orchestrator, and an owner's classes keep their own module;
* nothing but the facade imports an owner, and no owner imports another at
  runtime, so the facade is the only edge;
* the guards that enumerate constructs in ``slack/gateway.py`` by path keep seeing
  every construct they enumerate, because no owner holds one.
"""

from __future__ import annotations

import ast
import builtins
import dis
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

import kiro_crew.slack.gateway as gateway
from kiro_crew.slack import gateway_runtime
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = gateway.__name__
_SRC = Path(gateway.__file__).resolve().parents[2]
_RUNTIME_DIR = Path(gateway_runtime.__file__).resolve().parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})


def _owners() -> list[types.ModuleType]:
    """Every owner module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{gateway_runtime.__name__}.{info.name}")
        for info in pkgutil.iter_modules([str(_RUNTIME_DIR)])
    ]


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(member, property):
                    fn = member.fget
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _owner_trees() -> list[tuple[str, ast.Module]]:
    return [
        (path.name, ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_RUNTIME_DIR.glob("*.py"))
        if path.name != "__init__.py"
    ]


#: Each moved name, the owner that holds it, and its kind and signature in the
#: one-module gateway (captured from it before the split).
_BASE_SURFACE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "admission": (
        ("_start_subagent_dispatch_after_memory_ready", "async method", "(self) -> 'None'"),
        ("_register_child_liveness", "method", "(self) -> 'None'"),
        (
            "_start_adaptive_controller",
            "method",
            "(self, cfg: 'KiroCrewConfig | None' = None) -> 'None'",
        ),
        ("_wire_overload_health", "method", "(self, controller: 'AdaptiveController') -> 'None'"),
        ("_subagent_dependency_coordinator", "method", "(self) -> 'Any'"),
        ("_ensure_subagent_coordinator", "async method", "(self) -> 'None'"),
        ("_unwire_overload_health", "method", "(self) -> 'None'"),
        ("_wire_runner_admission", "method", "(self) -> 'None'"),
        (
            "_subscribe_runner_admission",
            "staticmethod",
            "(coordinator: 'Any', admission: 'Any') -> 'None'",
        ),
        ("_attach_runner_admission_consumers", "method", "(self, admission: 'Any') -> 'None'"),
        ("_runner_admission_store_ready", "async method", "(self) -> 'None'"),
        ("_unwire_runner_admission", "method", "(self) -> 'None'"),
        ("_start_dashboard_workers_after_memory_ready", "method", "(self) -> 'None'"),
    ),
    "channel_lifecycle": (
        ("_channel_transport_permitted", "function", "(member: 'str') -> 'bool'"),
        (
            "_push_observe_limits",
            "function",
            "(history: 'ChannelHistory', max_entries: 'int', ttl_secs: 'int') -> 'None'",
        ),
        ("_connect_slack", "async method", "(self) -> 'bool'"),
        ("_on_channel_config_change", "async method", "(self, change: 'ConfigChange') -> 'None'"),
        (
            "_restart_changed_channels",
            "async method",
            "(self, to_restart: 'list[tuple[str, frozenset[str]]]') -> 'None'",
        ),
        ("channel_restarts_settled", "async method", "(self) -> 'None'"),
        (
            "restart_channel",
            "async method",
            "(self, channel_type: 'str', *, cfg: 'KiroCrewConfig | None' = None) -> 'object | None'",
        ),
        (
            "_forget_superseded_start",
            "method",
            "(self, channel_type: 'str', client: 'object') -> 'None'",
        ),
        ("_close_channel_locked", "async method", "(self, channel_type: 'str') -> 'None'"),
        ("_on_slack_config_change", "async method", "(self, change: 'ConfigChange') -> 'None'"),
        (
            "_adopt_channel_sections_from_watcher",
            "async method",
            "(self, boot: \"'tuple[ChannelDescriptor, ...]'\") -> 'dict[str, str]'",
        ),
        (
            "_adopt_channel_section_from_watcher",
            "method",
            "(self, desc: \"'ChannelDescriptor'\", creds: 'dict[str, str]') -> 'None'",
        ),
        ("_schedule_inbound_replay", "method", "(self) -> 'None'"),
        (
            "_replay_spooled_inbound",
            "async method",
            "(self, *, spool: 'Path | None' = None) -> 'None'",
        ),
        (
            "_badge_unready_channels",
            "method",
            "(self, bootable: \"'tuple[ChannelDescriptor, ...]'\") -> 'None'",
        ),
    ),
    "cron_dispatch": (
        ("_CRON_TRANSIENT_RETRIES", "value", "'int'"),
        ("_CRON_POSTTOKEN_CONTINUE_MSG", "value", "'str'"),
        ("_defer_cron_before_dispatch", "function", "(job: 'CronJob', reason: 'str') -> 'None'"),
        (
            "_await_cron_fire_time_gate",
            "async function",
            "(job: 'CronJob', *, tool_name: 'str', tool_kind: 'str') -> 'tuple[str | None, bool]'",
        ),
        ("_CRON_RESERVED_ENV_KEYS", "value", "'frozenset'"),
        (
            "cron_job_env_without_reserved",
            "function",
            "(job_env: 'dict[str, str] | None') -> 'dict[str, str]'",
        ),
        (
            "_pre_create_cron_slot",
            "async function",
            "(dashboard_state: \"'DashboardState'\", job: 'CronJob') -> 'None'",
        ),
        ("CronClaimTimeDenied", "class", ""),
        ("CronClaimAbandoned", "class", ""),
        ("_ClaimHandoff", "class", ""),
        ("CronVetOverran", "class", ""),
        ("claim_vet_bound", "function", "(job: 'CronJob') -> 'float'"),
        ("_claim_backstop", "function", "(job: 'CronJob', subprocess_bound: 'int') -> 'float'"),
        (
            "_vet_at_claim_then",
            "function",
            "(handoff: '_ClaimHandoff', job: 'CronJob', fn: 'Callable[..., Any]', *args: 'Any') -> 'Any'",
        ),
        ("_annotate_model_fallback", "function", "(text: 'str', provider: 'Any') -> 'str'"),
        (
            "_cron_stream_with_posttoken_resume",
            "async function",
            "(client: 'Any', message: 'str', *, job_name: 'str', **stream_kwargs: 'Any') -> 'tuple[str, float | None]'",
        ),
    ),
    "cron_verdict": (
        ("_VOLATILE_RE", "value", "'Pattern'"),
        ("_EPOCH_RE", "value", "'Pattern'"),
        ("_EPOCH_WINDOW_SECS", "value", "'int'"),
        ("_SUCCESS_REMINDER_SECS", "value", "'int'"),
        ("_FAILURE_REMINDER_SECS", "value", "'int'"),
        ("_CRON_FAILURE_DETAIL_CAP", "value", "'int'"),
        ("_NO_RESPONSE", "value", "'str'"),
        ("_GateTally", "class", ""),
        (
            "_annotate_partial_block",
            "function",
            "(result_text: 'str', tally: '_GateTally') -> 'str'",
        ),
        ("_result_hash", "function", "(text: 'str') -> 'str'"),
    ),
    "delivery": (
        (
            "_open_dm_with_retry",
            "async method",
            "(self, user_id: 'str', job_name: 'str', max_attempts: 'int' = 3) -> 'str | None'",
        ),
        ("_record_cron_delivery", "method", "(self, job: 'CronJob', result_hash: 'str') -> 'None'"),
        (
            "_remember_options",
            "method",
            "(self, session_key: 'str', channel: 'str', ts: 'str', choices: 'list[str]', blocks: 'list[dict]', text: 'str') -> 'None'",
        ),
        (
            "_channel_reply_link",
            "method",
            "(self, parent_key: 'str') -> 'tuple[ChannelLink, bool] | None'",
        ),
        ("_cron_origin_key", "method", "(self, parent_key: 'str') -> 'str'"),
        (
            "_deliver_cron_to_channel",
            "async method",
            "(self, origin_key: 'str', text: 'str', *, actor_key: 'str') -> 'bool'",
        ),
        ("_cron_job_is_silent", "method", "(self, parent_key: 'str') -> 'bool'"),
    ),
    "mcp_broker": (
        ("_init_mcp_discovery", "method", "(self) -> 'None'"),
        (
            "_schedule_mcp_launch_approval_persist",
            "method",
            "(self, approvals: 'LaunchApprovals') -> 'None'",
        ),
        (
            "_init_mcp_gateway",
            "async method",
            "(self, stub_servers: 'frozenset[str] | None' = None) -> 'None'",
        ),
        ("_mcp_resolve_refresh_secs", "method", "(self) -> 'float'"),
        (
            "_mcp_resolve_prefetch_loop",
            "async method",
            "(self, target_env: 'dict[str, str]') -> 'None'",
        ),
        (
            "_prefetch_mcp_resolutions",
            "async method",
            "(self, target_env: 'dict[str, str]', *, force: 'bool' = False) -> 'dict[str, str]'",
        ),
        ("_refresh_mcp_resolutions", "async method", "(self) -> 'dict'"),
        ("_stop_mcp_broker", "async method", "(self) -> 'None'"),
        ("_apply_mcp_gateway_enabled", "async method", "(self, enabled: 'bool') -> 'dict'"),
        ("_apply_mcp_stub", "async method", "(self) -> 'dict'"),
        ("_wire_mcp_gateway_dashboard", "method", "(self) -> 'None'"),
    ),
    "memory_lifecycle": (
        ("_initialize_memory_worker", "method", "(self) -> 'bool'"),
        ("_stop_memory_startup", "method", "(self) -> 'None'"),
        ("_start_memory_after_ready", "method", "(self) -> 'None'"),
        ("_schedule_memory_preparation", "method", "(self) -> \"'asyncio.Task[None] | None'\""),
        ("_wait_for_memory_preparation", "async method", "(self) -> 'bool'"),
        ("_repair_member_memory_once", "method", "(self) -> 'None'"),
        ("_repair_member_memory", "async method", "(self) -> 'None'"),
        ("_start_embeddings", "async method", "(self) -> 'None'"),
        ("_auto_migrate_memory", "async method", "(self) -> 'None'"),
        ("_set_memory_migrated", "async method", "(self, value: 'bool') -> 'None'"),
    ),
    "tool_policy": (
        ("_BACKGROUND_APPROVAL_SOURCES", "value", "'frozenset'"),
        ("_READ_ONLY_TOOL_PREFIXES", "value", "'tuple'"),
        ("_WRITE_INDICATORS", "value", "'tuple'"),
        ("_is_read_only_tool", "function", "(event_title: 'str') -> 'bool'"),
        ("HEARTBEAT_SAFE_TOOLS", "value", "'frozenset'"),
        ("_HEARTBEAT_STATUS_PREFIXES", "value", "'tuple'"),
        ("_is_heartbeat_safe_tool", "function", "(event_title: 'str') -> 'bool'"),
        ("_build_heartbeat_hooks", "function", "(user_hooks: 'HookManager') -> 'HookManager'"),
        ("_bare_tool_name", "function", "(title: 'str') -> 'str'"),
    ),
}


# ── one namespace ─────────────────────────────────────────────────────────────


def test_the_owners_define_functions_for_the_sweeps_to_check() -> None:
    """Every sweep below proves nothing unless it has functions to sweep."""
    labels = {label for label, _ in _owner_functions()}
    callables = sum(
        kind.endswith(("function", "method"))
        for rows in _BASE_SURFACE.values()
        for _, kind, _ in rows
    )
    assert len(labels) >= callables
    assert "channel_lifecycle._channel_transport_permitted" in labels
    assert "cron_verdict._GateTally.note" in labels


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.slack.gateway.<name>`` reaches an owner function only
    because the function reads the facade's globals, not its own module's."""
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(gateway) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_gateway_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_gateway_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from the
    facade surfaces only when its line runs -- often inside an ``except`` that turns
    the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(gateway)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for, exercised end to end on two owners."""
    denied = types.SimpleNamespace(permitted=False, reason="policy", rule="r", layer="policy")
    calls: list[str] = []

    def _deny(scope: str, member: str, **_kw: object) -> object:
        calls.append(member)
        return denied

    monkeypatch.setattr(gateway, "governance_permits", _deny)
    monkeypatch.setattr(
        gateway, "sel", lambda: types.SimpleNamespace(log_governance_decision=lambda **_: None)
    )
    assert gateway._channel_transport_permitted("telegram") is False
    assert calls == ["telegram"]

    monkeypatch.setattr(gateway, "vet_job_at_fire_time", lambda job: "denied at claim")
    job = types.SimpleNamespace(name="j", id="j1")
    with pytest.raises(gateway.CronClaimTimeDenied, match="denied at claim"):
        gateway._vet_at_claim_then(gateway._ClaimHandoff(), job, lambda: None)


# ── the moved surface ─────────────────────────────────────────────────────────


def _facade_member(name: str) -> tuple[object, str]:
    """``(object, kind)`` for a moved name, read the way callers read it."""
    raw = vars(gateway.GatewayOrchestrator).get(name)
    if raw is not None:
        wrap = type(raw).__name__ if isinstance(raw, (staticmethod, classmethod)) else ""
        fn = raw.__func__ if wrap else raw
        return fn, ("async " if inspect.iscoroutinefunction(fn) else "") + (wrap or "method")
    obj = getattr(gateway, name)
    if inspect.isclass(obj):
        return obj, "class"
    if inspect.isfunction(obj):
        return obj, ("async " if inspect.iscoroutinefunction(obj) else "") + "function"
    return obj, "value"


def _signature(fn: object) -> str:
    """The signature with the first parameter's annotation dropped: an owner spells
    ``self: GatewayOrchestrator`` where the class body said ``self``."""
    sig = inspect.signature(fn)  # type: ignore[arg-type]
    params = list(sig.parameters.values())
    if params and params[0].name in ("self", "cls"):
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return str(sig.replace(parameters=params))


@pytest.mark.parametrize(
    ("owner", "name", "kind", "signature"),
    [(owner, *row) for owner, rows in _BASE_SURFACE.items() for row in rows],
    ids=[f"{owner}:{row[0]}" for owner, rows in _BASE_SURFACE.items() for row in rows],
)
def test_a_moved_name_keeps_its_base_shape_and_owner(
    owner: str, name: str, kind: str, signature: str
) -> None:
    """Every name that moved keeps the kind and signature it had in the one-module
    gateway, lives in the owner its responsibility names, and is ONE object: the
    facade attribute, the class attribute and the owner's are the same."""
    obj, found_kind = _facade_member(name)
    assert found_kind == kind
    if kind == "value":
        assert type(obj).__name__ == signature.strip("'")
    elif kind != "class":
        assert _signature(obj) == signature
    module = importlib.import_module(f"{gateway_runtime.__name__}.{owner}")
    assert getattr(module, name) is obj


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs, pickling by reference and nested functions' names read as they did
    before the split: every owner function resolves back through its own
    ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "fget", target)
            target = getattr(target, "__func__", target)
        if target is not fn:
            wrong.append(label)
    assert wrong == []
    nested = gateway.GatewayOrchestrator._schedule_inbound_replay.__code__
    inner = [c.co_qualname for c in nested.co_consts if isinstance(c, types.CodeType)]
    assert inner and all(
        q.startswith("GatewayOrchestrator._schedule_inbound_replay.") for q in inner
    )


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """``from kiro_crew.slack.gateway import *`` exports what the one-module file did."""
    probe = tmp_path / "gateway_star_probe.py"
    probe.write_text("from kiro_crew.slack.gateway import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gateway_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in (
        "HEARTBEAT_SAFE_TOOLS",
        "CronClaimTimeDenied",
        "claim_vet_bound",
        "cron_job_env_without_reserved",
    ):
        assert getattr(module, name) is getattr(gateway, name)


# ── one edge ──────────────────────────────────────────────────────────────────

_RUNTIME_PACKAGE = gateway_runtime.__name__


def _package_of(path: Path) -> str:
    """The package a relative import in a file under ``src/`` resolves against."""
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way.

    Covers ``import a.b``, ``from a import b`` (which may name module ``a.b``),
    relative ``from . import x`` / ``from ..x import y`` resolved against *package*,
    and a string-literal ``importlib.import_module(...)`` / ``__import__(...)`` call.
    """
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            name = node.args[0].value
            if name.startswith("."):
                anchor = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(anchor, ast.Constant) and isinstance(anchor.value, str)):
                    continue
                name = importlib.util.resolve_name(name, anchor.value)
            found.append((node, name))
    return found


def _is_owner(target: str) -> bool:
    return target == _RUNTIME_PACKAGE or target.startswith(f"{_RUNTIME_PACKAGE}.")


def _is_facade_or_owner(target: str) -> bool:
    return target == _FACADE or target.startswith(f"{_FACADE}.") or _is_owner(target)


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    return {
        id(stmt)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in ast.walk(node)
    }


def _owner_importers(source: str, package: str) -> list[int]:
    """Lines where a module other than the facade imports an owner, in any spelling
    and under ``TYPE_CHECKING`` too (a type-only import still makes an owner part of
    another module's surface)."""
    tree = ast.parse(source)
    return sorted(
        {node.lineno for node, target in _import_targets(tree, package) if _is_owner(target)}
    )


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports the facade or an owner outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if _is_facade_or_owner(target) and id(node) not in guarded
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import channel_lifecycle\n", True),
        ("from .channel_lifecycle import restart_channel\n", True),
        ("from .. import gateway\n", True),
        ("from ..gateway import sel\n", True),
        ("from kiro_crew.slack import gateway\n", True),
        ("from kiro_crew.slack import gateway_runtime\n", True),
        ("import kiro_crew.slack.gateway\n", True),
        ("import kiro_crew.slack.gateway as facade\n", True),
        ("from kiro_crew.slack.gateway import sel\n", True),
        ("def f():\n    from kiro_crew.slack.gateway import sel\n", True),
        ("import importlib\nimportlib.import_module('kiro_crew.slack.gateway')\n", True),
        ("__import__('kiro_crew.slack.gateway_runtime.admission')\n", True),
        (
            "import importlib\nimportlib.import_module('.admission', 'kiro_crew.slack.gateway_runtime')\n",
            True,
        ),
        ("if TYPE_CHECKING:\n    from kiro_crew.slack.gateway import sel\n", False),
        ("if TYPE_CHECKING:\n    from .. import gateway\n", False),
        ("from kiro_crew.slack import handler\n", False),
        ("import kiro_crew.slack.gateway_helpers_elsewhere\n", False),
        ("from kiro_crew.llm_helpers import annotate_model_fallback\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The runtime-edge check below is only as good as the spellings it resolves."""
    assert bool(_owner_runtime_edges(source, _RUNTIME_PACKAGE)) is flagged


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from .gateway_runtime import delivery\n", True),
        ("from . import gateway_runtime\n", True),
        ("from .gateway_runtime.delivery import _cron_origin_key\n", True),
        ("import kiro_crew.slack.gateway_runtime.delivery\n", True),
        ("from kiro_crew.slack.gateway_runtime import delivery as d\n", True),
        ("if TYPE_CHECKING:\n    from .gateway_runtime import delivery\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.slack.gateway_runtime.delivery')\n",
            True,
        ),
        ("from . import gateway\n", False),
        ("from .gateway import GatewayOrchestrator\n", False),
        ("from kiro_crew.slack import gateway\n", False),
        ("import importlib\nimportlib.import_module(name)\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    """The one-import-path check below is only as good as the spellings it resolves."""
    assert bool(_owner_importers(source, "kiro_crew.slack")) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the only import path, so there is one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        if (
            _RUNTIME_DIR in path.resolve().parents
            or path.resolve() == Path(gateway.__file__).resolve()
            or "_vendor" in path.parts
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if "gateway_runtime" not in text:
            continue
        importers.extend(
            f"{path.relative_to(_SRC)}:{line}" for line in _owner_importers(text, _package_of(path))
        )
    assert importers == []


def test_an_owner_imports_the_facade_and_its_siblings_only_for_type_checking() -> None:
    """No owner imports the facade or another owner at runtime: the facade imports
    the owners and nothing points back, so there is no import cycle to order."""
    offenders = [
        f"{name}:{line}"
        for name, _ in _owner_trees()
        for line in _owner_runtime_edges(
            (_RUNTIME_DIR / name).read_text(encoding="utf-8"), _RUNTIME_PACKAGE
        )
    ]
    assert offenders == []


def test_every_owner_with_a_coroutine_is_in_the_config_dir_guard() -> None:
    """``test_no_config_dir_in_async`` scans the files it lists, so an owner that
    gains an ``async def`` must be listed there, or its coroutines escape the guard."""
    guard = _SRC.parent / "test" / "test_no_config_dir_in_async.py"
    listed: set[str] = set()
    for node in ast.parse(guard.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ASYNC_CHECKED_FILES" for t in node.targets
        ):
            listed = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
    assert listed, "_ASYNC_CHECKED_FILES not found; this check went stale"
    with_coroutines = {
        f"slack/gateway_runtime/{name}"
        for name, tree in _owner_trees()
        if any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    }
    assert with_coroutines, "no owner defines a coroutine; this check went stale"
    assert sorted(with_coroutines - listed) == []


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    """A test that purges the facade and imports it again gets owners that run on
    the NEW namespace, and the orchestrator's qualnames are not prefixed twice."""
    script = textwrap.dedent("""
        import sys
        import kiro_crew.slack.gateway as first
        del sys.modules["kiro_crew.slack.gateway"]
        import kiro_crew.slack.gateway as second
        from kiro_crew.slack.gateway_runtime import delivery
        fn = second.GatewayOrchestrator._deliver_cron_to_channel
        assert second is not first
        assert fn.__globals__ is vars(second)
        assert delivery._deliver_cron_to_channel is fn
        assert fn.__qualname__ == "GatewayOrchestrator._deliver_cron_to_channel"
        assert fn.__code__.co_qualname == "GatewayOrchestrator._deliver_cron_to_channel"
        assert second._result_hash.__globals__ is vars(second)
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    names = sorted(info.name for info in pkgutil.iter_modules([str(_RUNTIME_DIR)]))
    assert len(names) >= 8, names
    script = textwrap.dedent("""
        import sys
        import kiro_crew.slack.gateway
        missing = [n for n in sys.argv[1:] if f"kiro_crew.slack.gateway_runtime.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """)
    result = subprocess.run(
        [sys.executable, "-c", script, *names],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the path-keyed guards keep their reach ────────────────────────────────────

#: Constructs repository guards enumerate in ``slack/gateway.py`` by path (usage-row
#: persists, runtime-death attribution and streaks, spawn and exit sites, chat-turn
#: dispatches, the nudge composer, the memory-store and transcript seams, the
#: boot-order anchors, the event-log publish). An owner that grew one would move it
#: out of such a guard's sight without failing it, so each stays in the facade.
_FACADE_ONLY_TOKENS = (
    "persist_token_record",
    ".read_messages",
    "begin_turn",
    "os._exit",
    "save_conversation_turn_off_loop",
    "session_store_for_turn",
    "create_subprocess_",
    "isolated_python_argv",
    "respawn_executable",
    "reexec_python_module",
    "_running_script_ids.add",
    "caused_by_this_session",
    "note_shared_death",
    "clear_marker",
    "init_socket_mode",
    "build_message",
    "VectorMemoryStore",
    "configure_default_ladder",
    "eventlog_hooks",
    "autonudge_state",
    "_run_chat",
    "spawn_guarded_turn",
    "compose_nudge_body",
    "clear_shared_deaths",
    "run_in_cron_pool(",
    "consecutive_failures",
    "_delivery_queued",
    "_digest_held",
    "_deliver_failure_alert",
    "render_for_slack(",
)


@pytest.mark.parametrize("token", _FACADE_ONLY_TOKENS)
def test_a_construct_the_facade_guards_count_stays_in_the_facade(token: str) -> None:
    assert token in Path(gateway.__file__).read_text(encoding="utf-8")
    holders = [
        name
        for name, _ in _owner_trees()
        if token in (_RUNTIME_DIR / name).read_text(encoding="utf-8")
    ]
    assert holders == []


def test_no_owner_reads_a_hoisted_channel_attribute() -> None:
    """``test_channel_boot_hot_reload`` checks every ``_<channel>_*`` attribute read in
    ``slack/gateway.py`` against that channel's hoist; the reads stay there, so no
    owner holds one."""
    channels = (
        "wecom",
        "telegram",
        "weixin",
        "whatsapp",
        "feishu",
        "discord",
        "webex",
        "imessage",
        "teams",
    )
    offenders = [
        f"{name}:{node.lineno}:{node.attr}"
        for name, tree in _owner_trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and any(node.attr.startswith(f"_{c}_") for c in channels)
    ]
    assert offenders == []


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_methods_and_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a bound method, a static method and a
    property of an owner class all read the host namespace afterwards, the method and
    its nested function carry the host class's qualname, and the owner class keeps its
    own module (``inspect`` finds a class's source through it)."""
    owner = _write_module(
        tmp_path,
        "compose_owner_probe",
        """
        def helper():
            return VALUE

        def method(self):
            def inner():
                return VALUE
            return inner

        def static():
            return VALUE

        class Tally:
            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "compose_host_probe", "VALUE": "host"}

    class Host:
        pass

    Host.method = owner.method  # type: ignore[attr-defined]
    Host.static = staticmethod(owner.static)  # type: ignore[attr-defined]
    namespace["helper"] = owner.helper
    gateway_runtime.compose(namespace, Host, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert Host().method()() == "host"  # type: ignore[attr-defined]
    assert Host.static() == "host"  # type: ignore[attr-defined]
    assert owner.Tally().value == "host"
    assert Host.method.__qualname__ == f"{Host.__qualname__}.method"  # type: ignore[attr-defined]
    inner = Host().method()  # type: ignore[attr-defined]
    assert inner.__qualname__ == f"{Host.__qualname__}.method.<locals>.inner"
    assert owner.Tally.__module__ == "compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"
