"""``kiro_crew.subagent`` stays one namespace while its definitions live in ``subagent_parts``.

``kiro_crew.subagent`` keeps its import path, its patch surface and every construct a
path-keyed repository guard reads in that file; the modules of ``kiro_crew.subagent_parts``
hold the rest, one responsibility each, and ``_facade_compose.compose`` runs every function
they define on the facade's globals. These tests pin what that composition promises:

* each owner function runs on the facade's globals, and every global it reads resolves
  there, so a patch of ``kiro_crew.subagent.<name>`` reaches it wherever it now lives -- and
  every name the test trees patch there is read by no owner code running anywhere else;
* a value an owner computes while it is imported (a dataclass field factory) resolves the
  names it reads in the owner's own module;
* each moved name keeps the kind and signature it had in the one-module file, and is ONE
  object whichever module a caller reads it from -- except a name owner code rebinds with
  ``global``, which lives on the facade while the owner keeps only its import-time binding;
* a function's ``__module__`` still names the facade, and an owner's classes keep their own,
  except ``SpawnApprovalUnreachable``, which reports the facade's;
* nothing but ``kiro_crew.subagent`` imports a module of ``subagent_parts`` at runtime,
  except ``manager.py``, which imports the ``*_surface`` modules its class binds and
  ``run_limits`` for its default arguments; an owner imports the facade only for type
  checking, and no owner carries an agent-sdk boundary edge;
* every import the facade keeps only for the owners is read by one;
* ``SubagentManager`` keeps every member of the one-module class with its kind and
  signature, each binding IS its collaborator's function, and no bound function needs
  the class body (zero-argument ``super()``, ``__class__``, a mangled name).
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import dis
import importlib
import importlib.util
import inspect
import pkgutil
import subprocess
import sys
import textwrap
import traceback
import types
from pathlib import Path

import pytest

import kiro_crew.subagent as subagent
from kiro_crew import subagent_parts
from kiro_crew._facade_compose import compose as facade_compose
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = subagent.__name__
_SRC = Path(subagent.__file__).resolve().parents[1]
_PARTS_DIR = Path(subagent_parts.__file__).resolve().parent
_REPO = _SRC.parent
_GLOBAL_OPS = frozenset({"LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"})
_TEST_TREES = (_REPO / "test", _SRC / "kiro_crew" / "apps" / "builtins")


def _owners() -> list[types.ModuleType]:
    """Every owner module, imported through the package the facade composes."""
    return [
        importlib.import_module(f"{subagent_parts.__name__}.{info.name}")
        for info in pkgutil.iter_modules([str(_PARTS_DIR)])
    ]


def _functions_of(member: object) -> list[types.FunctionType]:
    if isinstance(member, property):
        return [
            f for f in (member.fget, member.fset, member.fdel) if isinstance(f, types.FunctionType)
        ]
    fn = getattr(member, "__func__", member)
    return [fn] if isinstance(fn, types.FunctionType) else []


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level or as a
    member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        short = owner.__name__.rsplit(".", 1)[-1]
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                for fn in _functions_of(member):
                    if fn.__code__.co_filename == owner.__file__:
                        found.append((f"{short}.{label}", fn))
    return found


def _field_factories() -> list[tuple[str, types.FunctionType]]:
    """Dataclass field factories compiled from an owner file: compose cannot reach them."""
    found = []
    for owner in _owners():
        for name, value in vars(owner).items():
            if isinstance(value, type) and dataclasses.is_dataclass(value):
                for f in dataclasses.fields(value):
                    factory = f.default_factory
                    if (
                        isinstance(factory, types.FunctionType)
                        and factory.__code__.co_filename == owner.__file__
                    ):
                        found.append((f"{owner.__name__}.{name}.{f.name}", factory))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in _GLOBAL_OPS:
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _unbound_globals(fn: types.FunctionType) -> list[str]:
    namespace = fn.__globals__
    return sorted(
        {n for n in _global_names(fn.__code__) if n not in namespace and not hasattr(builtins, n)}
    )


def _owner_sources() -> dict[str, ast.Module]:
    return {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_PARTS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


def _runtime_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """``(line, module)`` of each import an owner executes when it is imported."""
    typing_only = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
            found += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
        elif isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
    return found


def _family_edges(module: str) -> bool:
    return (
        module == _FACADE
        or module.startswith(f"{subagent_parts.__name__}.")
        or module == subagent_parts.__name__
    )


#: Owner functions the sweeps must find (a floor, so a sweep that finds nothing fails).
_MIN_OWNER_FUNCTIONS = 300

#: Agent-sdk boundary edges ``subagent.py`` carries (``.github/agent-sdk-boundary-baseline.txt``).
_FACADE_EDGES = 6

#: Each moved name, the owner that holds it, and its kind and shape in the one-module file
#: (a function's signature, a value's type), captured from it before the split.
_BASE_SURFACE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "run_limits": (
        ("_MAX_CONCURRENT", "value", "int"),
        ("_RUN_ID_HEX_CHARS", "value", "int"),
        ("_TIMEOUT_SECS", "value", "int"),
        ("_TURN_LIMIT", "value", "int"),
        ("SUCCESSOR_PENDING", "value", "str"),
        ("SUCCESSOR_UNKNOWN", "value", "str"),
        ("_HEADLESS_DENY_REASON", "value", "str"),
        ("_LOW_FIDELITY_DENY_REASON", "value", "str"),
        ("_REAPER_INTERVAL", "value", "int"),
        ("_CONVERSATION_TTL_SECS", "value", "int"),
        ("_STEER_STARTUP_WAIT_SECS", "value", "float"),
        ("_STEER_STARTUP_POLL_SECS", "value", "float"),
        ("_WAVE_STUCK_SECS", "value", "int"),
        ("_RESET_TIMEOUT", "value", "float"),
        ("_TEARDOWN_REPORT_GRACE", "value", "float"),
        ("_RECOVERY_SLOT_WAIT_SECS", "value", "float"),
        ("_DEDICATED_TOPUP_WAIT_SECS", "value", "float"),
        ("_DEDICATED_TOPUP_POLL_SECS", "value", "float"),
        ("_PRESSURE_HOLD_PRUNE_FACTOR", "value", "int"),
        ("_PRESSURE_EPISODE_MAX_GAP_SECS", "value", "float"),
        ("SEL_MEMORY_PRESSURE_NEVER_STARTED", "value", "str"),
        ("_HOST_READ_OFF_LOOP_SECS", "value", "float"),
        ("_REPORT_DRAIN_TIMEOUT", "value", "float"),
        ("_STATE_DRAIN_TIMEOUT", "value", "float"),
        ("_STARTUP_TIMEOUT_SECS", "value", "int"),
        ("_STARTUP_HANDSHAKES", "value", "int"),
        ("_STARTUP_COLLECT_GRACE_SECS", "value", "int"),
        ("_STARTUP_LAUNCH_MARGIN_SECS", "value", "int"),
        ("_STARTUP_CAP_GATE_ROUNDS", "value", "int"),
        ("_RELEASE_REPUMP_SECS", "value", "float"),
        ("_ON_DONE_TIMEOUT", "value", "float"),
        ("_TRANSIENT_CONTINUE_MSG", "value", "str"),
        ("_CANCEL_RESUME_PREFIX", "value", "str"),
        ("_DEFAULT_INJECTION_TIMEOUT", "value", "float"),
        ("_env_float", "function", "(name: 'str', default: 'float') -> 'float'"),
        ("_resolve_injection_timeout", "function", "() -> 'float'"),
        ("INJECTION_TIMEOUT", "value", "float"),
        ("_STALL_IDLE_SECS", "value", "int"),
        ("_SUPPRESS_CEILING", "value", "int"),
        ("_DEFAULT_DIGEST_HOLD_SECS", "value", "float"),
        ("_digest_hold_secs", "function", "() -> 'float'"),
        ("DIGEST_HOLD_SECS", "value", "float"),
        ("_RSS_SAMPLES_TO_SETTLE", "value", "int"),
        ("_SETTLE_AFTER_SECS", "value", "int"),
        ("_SYSTEM_PREFIX", "value", "str"),
    ),
    "run_output": (
        ("_redact", "function", "(text: 'str') -> 'str'"),
        ("_redact_and_truncate", "function", "(text: 'str', max_chars: 'int') -> 'str'"),
        ("_MAX_ERROR_CHAIN", "value", "int"),
        ("_describe_exception", "function", "(exc: 'BaseException') -> 'str'"),
        ("_MAX_DONE_RESULT_LEN", "value", "int"),
        ("_done_result", "function", "(text: 'str') -> 'str'"),
        ("_finite_nonnegative_number", "function", "(value: 'object') -> 'float | None'"),
        ("format_subagent_usage", "function", "(credits: 'object', elapsed: 'object') -> 'str'"),
        ("SubagentDelivery", "class", ""),
        ("ORPHAN_NOTICE_OWED", "value", "str"),
        ("_RunCreditAccounting", "class", ""),
        (
            "_timeout_context",
            "function",
            "(info: \"'SubagentInfo'\", *, include_elapsed: 'bool' = True, turn_limit: 'int' = 0) -> 'str'",
        ),
    ),
    "agent_policy": (
        ("UNADVERTISED_AGENTS", "value", "frozenset"),
        ("AGENT_NOT_FOUND_CODE", "value", "str"),
        ("AGENT_NOT_AVAILABLE_CODE", "value", "str"),
        ("AGENT_INTERNAL_CODE", "value", "str"),
        (
            "_internal_agent_refusal",
            "function",
            "(requested: 'str', available: 'list[str]') -> 'str'",
        ),
        ("_AGENT_GLOB_RE", "value", "Pattern"),
        (
            "visible_agent_names",
            "function",
            "(names: 'Iterable[str]', *, exclude: 'Container[str]' = <frozenset>, limit: 'int | None' = None) -> 'tuple[list[str], int]'",
        ),
        ("_MAX_AVAILABLE_IN_ERROR", "value", "int"),
        ("_MAX_AVAILABLE_AGENTS_GLOBS", "value", "int"),
        ("_MAX_AVAILABLE_AGENTS_GLOB_CHARS", "value", "int"),
        ("_available_agents_hint", "function", "(available: 'list[str]') -> 'str'"),
        ("_validate_app_agent_ownership", "function", "(agent: 'str', app: 'str') -> 'str'"),
        ("AgentCheck", "value", "GenericAlias"),
        (
            "_validate_agent",
            "function",
            "(requested: 'str', project_dir: 'str' = '') -> 'tuple[str, str, str]'",
        ),
        (
            "_inherited_member_agent",
            "function",
            "(selection_kind: 'str', template_id: 'str', project_dir: 'str' = '') -> 'str'",
        ),
        (
            "_vet_spawn_governance",
            "function",
            "(parent_session_key: 'str', agent: 'str', app: 'str' = '') -> 'str | None'",
        ),
        ("spawn_allowlist", "function", "(spec: 'Mapping[str, Any]') -> 'tuple[str, ...] | None'"),
        (
            "agent_matches_allowlist",
            "function",
            "(agent: 'str', allowlist: 'Iterable[str]', *, app: 'str' = '') -> 'bool'",
        ),
        (
            "parent_spawn_allowlists",
            "function",
            "(parent_template: 'str', project_dirs: 'Sequence[str | Path]' = (), *, parent_session_key: 'str' = '') -> 'tuple[tuple[str, ...], ...] | None'",
        ),
        ("_ScopeAllowlists", "value", "GenericAlias"),
        (
            "_scope_spawn_allowlists",
            "function",
            "(agents_dir: 'Path', parent_template: 'str', memo: \"'AgentsDirMemo[_ScopeAllowlists]'\", operation: 'str') -> '_ScopeAllowlists'",
        ),
        ("_UNRESOLVED_PARENT_WARNED", "value", "set"),
        ("_UNRESOLVED_PARENT_WARNED_MAX", "value", "int"),
        ("_parent_project_resolver", "value", "NoneType"),
        (
            "set_parent_project_resolver",
            "function",
            "(resolver: \"Callable[[str], 'Iterable[str | Path]'] | None\") -> 'None'",
        ),
        ("parent_project_dirs", "function", "(parent_session_key: 'str') -> 'tuple[str, ...]'"),
        (
            "_surface_unresolved_parent_spec",
            "function",
            "(parent_template: 'str', parent_session_key: 'str') -> 'None'",
        ),
        ("_PARENT_PROJECT_ALLOWLIST_MEMO", "value", "AgentsDirMemo"),
        ("_PARENT_ALLOWLIST_MEMO", "value", "AgentsDirMemo"),
        (
            "_scan_parent_spawn_allowlists",
            "function",
            "(agents_dir: 'Path', parent_template: 'str', operation: 'str' = 'spawn_available_agents') -> '_ScopeAllowlists'",
        ),
        (
            "parent_template_allowlists",
            "function",
            "(parent_session_key: 'str', template: 'str', accepted_dirs: 'object' = ()) -> 'tuple[tuple[str, ...], ...] | None'",
        ),
        ("ParentRecordUnreadable", "class", ""),
        ("_parent_template_for_spawn", "function", "(parent_session_key: 'str') -> 'str'"),
        ("ParentSpawnPolicy", "value", "GenericAlias"),
        (
            "parent_spawn_policy",
            "function",
            "(parent_session_key: 'str', accepted_dirs: 'object' = ()) -> 'ParentSpawnPolicy'",
        ),
        (
            "_vet_parent_available_agents",
            "function",
            "(policy: 'ParentSpawnPolicy', agent: 'str', *, app: 'str' = '') -> 'str | None'",
        ),
        (
            "validate_cwd",
            "function",
            "(cwd: 'str', allowed_roots: 'list[str]') -> 'tuple[str, str]'",
        ),
    ),
    "model_choice": (
        ("_resolved_model_of", "function", "(client: 'object') -> 'str'"),
        ("_subagent_default_model", "function", "(cfg: 'Any' = None) -> 'str'"),
        ("_subagent_default_effort", "function", "(cfg: 'Any' = None) -> 'str'"),
        (
            "_route_unpinned_start",
            "async function",
            "(info: \"'SubagentInfo'\", kind: 'str', sessions: 'Any') -> 'None'",
        ),
        (
            "_spawn_effective_model",
            "function",
            "(model: 'str', agent: 'str', *, crew_agent: 'str | None' = None, backend: 'str | None' = None) -> 'str | None'",
        ),
        ("_effort_channel", "function", "(backend: 'str | None') -> 'str | None'"),
        ("_effort_judged_at_session_start", "function", "(backend: 'str | None') -> 'bool'"),
        (
            "effort_drop_reason",
            "function",
            "(model: 'str', reasoning_effort: 'str', agent: 'str' = '', *, crew_agent: 'str | None' = None, backend: 'str | None' = None) -> 'str'",
        ),
        (
            "effort_applied_note",
            "function",
            "(model: 'str', reasoning_effort: 'str', agent: 'str' = '', *, crew_agent: 'str | None' = None, backend: 'str | None' = None) -> 'str'",
        ),
        (
            "effort_pending_note",
            "function",
            "(model: 'str', reasoning_effort: 'str', agent: 'str' = '', *, crew_agent: 'str | None' = None, backend: 'str | None' = None) -> 'str'",
        ),
    ),
    "host_memory": (
        ("MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE", "value", "str"),
        ("MEMORY_CAUSE_READ_UNANSWERED", "value", "str"),
        ("_memory_check_cause", "value", "_local"),
        ("pop_memory_check_cause", "function", "() -> 'str'"),
        (
            "check_memory_available",
            "function",
            "(min_gb: 'float' = 2.0, *, path: 'str' = '/proc/meminfo') -> 'tuple[bool, float]'",
        ),
        (
            "_proc_subtree_sample",
            "function",
            "(pid: 'Optional[int]', *, pss: 'bool' = False) -> 'platform_compat.SubtreeSample'",
        ),
        (
            "_subtree_cpu_jiffies",
            "function",
            "(pid: 'int', *, pids: 'Optional[list[int]]' = None) -> 'int'",
        ),
        (
            "_attributed_count",
            "function",
            "(total: 'Optional[int]', sharers: 'int', previous: 'Optional[int]') -> 'Optional[int]'",
        ),
        ("_LEGACY_DEFAULT_MAX", "value", "int"),
        ("_available_memory_gb", "function", "() -> 'float'"),
        ("_host_available_gb", "function", "() -> 'float'"),
        ("_macos_vm_reclaimable_pages", "function", "() -> 'Optional[int]'"),
        ("_macos_available_memory_gb", "function", "() -> 'float'"),
        ("_CGROUP_UNLIMITED", "value", "int"),
        ("_read_int_file", "function", "(path: 'str') -> 'int | None'"),
        ("_MEMORY_STAT_READ_CAP", "value", "int"),
        (
            "_read_reclaimable_file_bytes",
            "function",
            "(directory: 'PurePosixPath | Path', v2: 'bool') -> 'int'",
        ),
        (
            "_working_set",
            "function",
            "(directory: 'PurePosixPath | Path', v2: 'bool', usage: 'int') -> 'int'",
        ),
        (
            "_cgroup_memory_roots",
            "function",
            "() -> 'list[tuple[PurePosixPath, PurePosixPath, bool]]'",
        ),
        ("_cgroup_available_gb", "function", "() -> 'float'"),
        ("_container_cgroup_available_gb", "function", "() -> 'float'"),
        ("_agents_slice_available_gb", "function", "() -> 'float'"),
        ("_auto_ceiling", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        ("_configured_max_subagents", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        ("subagent_count_ceiling", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        ("compute_max_subagents", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        (
            "compute_memory_sized_parallel_cap",
            "function",
            "(cfg: 'KiroCrewConfig', *, settled: 'bool' = False) -> 'int'",
        ),
        (
            "_host_mem_term",
            "function",
            "(cfg: 'KiroCrewConfig', *, settled: 'bool' = False) -> 'int | None'",
        ),
        ("resolve_max_subagents", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        ("_CLK_TCK", "value", "int"),
    ),
    "start_pricing": (
        ("_SHARED_START_SAVING_GB", "value", "float"),
        ("_SHARED_START_MIN_GB", "value", "float"),
        ("_UNLEARNED_DEDICATED_START_GB", "value", "float"),
        ("_SETTLED_PROJECTION_CEILING_GB", "value", "float"),
        ("_shared_start_price_gb", "function", "(dedicated_gb: 'float') -> 'float'"),
        ("_selection_kind", "function", "(info: 'SubagentInfo') -> 'str'"),
        ("_SharingPlan", "class", ""),
        (
            "_dedicated_start_price_gb",
            "function",
            "(cost_gb: 'float', settled_gb: 'Mapping[str, float] | None', bucket: 'str') -> 'float'",
        ),
        (
            "_spawn_memory_floor_and_cost",
            "function",
            "(agent_cfg: 'Any' = None) -> 'tuple[float, float]'",
        ),
        ("_host_memory_reading", "function", "(min_gb: 'float') -> 'tuple[float, str]'"),
        (
            "_host_memory_reading_off_loop",
            "async function",
            "(min_gb: 'float') -> 'tuple[float, str]'",
        ),
        ("_host_read_in_flight", "value", "NoneType"),
        ("_row_settled", "function", "(info: 'SubagentInfo', now: 'float') -> 'bool'"),
        ("_parked_at_spawn_approval", "function", "(info: 'SubagentInfo') -> 'bool'"),
        (
            "_owns_dedicated_runtime",
            "function",
            "(agents: 'Iterable[SubagentInfo]', *, claim_prices: 'Mapping[str, tuple[float, bool]]') -> 'bool'",
        ),
        ("_holds_dedicated_runtime", "function", "(info: 'SubagentInfo') -> 'bool'"),
        (
            "_startup_memory_reserve_gb",
            "function",
            "(agents: 'list[SubagentInfo]', *, running_count: 'int', cost_gb: 'float', next_start_gb: 'float | None' = None, settled_gb: 'Mapping[str, float] | None' = None, claim_prices: 'Iterable[float]' = (), now: 'float | None' = None) -> 'float'",
        ),
        (
            "_cost_bucket",
            "function",
            "(agent: 'str', execution: 'Any', *, remote: 'bool' = False) -> 'str'",
        ),
    ),
    "run_record": (
        ("_NEUTRAL_REAP_REASONS", "value", "frozenset"),
        ("_BareStateView", "class", ""),
        ("SubagentInfo", "class", ""),
        ("AnnounceCallback", "value", "_CallableGenericAlias"),
        ("_context_groups_of", "function", "(info: \"'SubagentInfo'\") -> 'frozenset[str]'"),
        ("_context_groups_field", "function", "(info: \"'SubagentInfo'\") -> 'str'"),
    ),
    "delivery": (
        ("_injection_notice_outcome", "function", "(info: \"'SubagentInfo'\") -> 'str'"),
        ("SubagentEventCallback", "value", "_CallableGenericAlias"),
        ("ToolApprovalCallback", "class", ""),
        ("SpawnApprovalUnreachable", "class", ""),
        ("SpawnApprovalCallback", "class", ""),
        ("PARKS_WHEN_SET", "value", "str"),
        ("PARKS_WHEN_UNSET", "value", "str"),
        ("NOT_DELIVERY_STATE", "value", "str"),
        ("DELIVERY_ROUTING_FIELDS", "value", "dict"),
        ("DELIVERY_ROUTING_MODULES", "value", "tuple"),
        ("delivery_is_parked", "function", "(info: \"'SubagentInfo'\") -> 'bool'"),
        ("_audit_ids", "function", "(ids: \"'Iterable[str]'\", cap: 'int' = 20) -> 'str'"),
        ("_TEARDOWN_GATE_TTL_SECS", "value", "float"),
        ("_AgingIdSet", "class", ""),
    ),
}


#: The owner module that defines ``SubagentManager`` and binds its collaborators' functions.
_HOST_OWNER = "manager"

#: Moved classes that report ``kiro_crew.subagent`` as their module; every other class an
#: owner defines reports the owner's.
_FACADE_MODULE_CLASSES = frozenset({"SpawnApprovalUnreachable"})

#: Class members bound from a collaborator (a floor: a binding sweep that finds none fails).
_MIN_BOUND_MEMBERS = 180

#: Every ``SubagentManager`` member of the one-module class, with its kind and signature.
_BASE_CLASS: dict[str, tuple[str, str]] = {
    "LIVE_CONFIG_PATHS": ("attribute", "tuple"),
    "SIZING_CONFIG_PATHS": ("attribute", "tuple"),
    "_COMPONENT_TYPES": ("attribute", "dict"),
    "_FOLLOWUP_BUSY_RETRIES": ("attribute", "int"),
    "_FOLLOWUP_BUSY_RETRY_SECS": ("attribute", "float"),
    "_FOLLOWUP_POLL_SECS": ("attribute", "float"),
    "__getattr__": ("method", "(self, name: 'str') -> 'Any'"),
    "__init__": (
        "method",
        "(self, sessions: 'SessionManager', ctx_builder: 'ContextBuilder', on_done: 'AnnounceCallback | None' = None, max_concurrent: 'int' = 3, default_turn_limit: 'int' = 1000, default_timeout: 'int' = 10800, startup_timeout: 'int' = 0, stall_idle_secs: 'int' = 120, on_tool_approval: 'ToolApprovalCallback | None' = None, on_tool_approval_factory: \"Callable[['SubagentInfo'], Callable[[LLMEvent], Awaitable[bool]]] | None\" = None, on_spawn_approval: 'SpawnApprovalCallback | None' = None, is_yolo: 'Callable[[], bool] | None' = None, on_event: 'SubagentEventCallback | None' = None, on_orphan_notify: 'Callable[..., Awaitable[bool | str]] | None' = None, on_orphan_dm: 'Callable[[str], Awaitable[bool]] | None' = None, completion_keep: 'str' = 'head', completion_keep_chars: 'int' = 3000, memory_mode_for_session: 'Callable[[str], str] | None' = None, defer_queue_dispatch: 'bool' = False, hold_orphan_notices: 'bool' = False)",
    ),
    "_admit_released_start": ("async method", "(self, info: 'SubagentInfo') -> 'str'"),
    "_after_dispatch": (
        "method",
        "(self, params: 'dict[str, Any]', drained: 'SubagentInfo | None', *, refill: 'Callable[..., int]') -> 'None'",
    ),
    "_announce_digest_flush": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_announce_followup_failure": (
        "async method",
        "(self, info: 'SubagentInfo', reason: 'str', failure_info: 'SubagentInfo | None' = None, messages: 'list | None' = None) -> 'None'",
    ),
    "_announce_rejection": ("method", "(self, info: 'SubagentInfo') -> 'SubagentInfo'"),
    "_arm_followup_watcher": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_audit_followup": ("method", "(self, info: 'SubagentInfo', outcome: 'str') -> 'None'"),
    "_await_lane_resume": (
        "async method",
        "(self, info: 'SubagentInfo', *, reason: 'str', timeout: 'float', request: 'bool' = True) -> 'bool'",
    ),
    "_await_late_start": (
        "async method",
        "(self, info: 'SubagentInfo', session_key: 'str', exc: 'Exception') -> \"'LLMProvider'\"",
    ),
    "_await_report": ("async staticmethod", "(task: \"'asyncio.Task[bool]'\") -> 'bool'"),
    "_bind_shared_handle": (
        "async method",
        "(self, info: 'SubagentInfo', session_key: 'str', runtime: \"'AcpRuntime'\", handle: 'Any') -> \"'LLMProvider'\"",
    ),
    "_cancel_task_intentionally": (
        "method",
        "(self, task: \"'asyncio.Task | asyncio.TimerHandle'\", info: \"'SubagentInfo | None'\" = None, *, reason: 'str') -> 'None'",
    ),
    "_cap_unclaimed_result": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_check_agent_off_loop": (
        "async method",
        "(self, agent: 'str', cwd: 'str', *, app: 'str' = '', execution_context: \"'Mapping[str, Any] | None'\" = None, prevalidated: 'bool' = False) -> \"'AgentCheck | None'\"",
    ),
    "_claim_continuation": (
        "method",
        "(self, conv_id: 'str', task: 'str', parent_session_key: 'str') -> 'tuple[SubagentInfo | None, SubagentInfo | None]'",
    ),
    "_claim_finalize": (
        "method",
        "(self, info: 'SubagentInfo', *, supersede_recovery: 'bool' = False, row_settled: 'bool' = False) -> 'bool'",
    ),
    "_clamp_effective_cap": ("method", "(self) -> 'int'"),
    "_clear_tool_dispatch": ("staticmethod", "(info: 'SubagentInfo') -> 'None'"),
    "_continue_prelude": (
        "method",
        "(self, conv_id: 'str', task: 'str', parent_session_key: 'str' = '', agent: 'str' = '', model: 'str | None' = None, max_turns: 'int' = 0, cwd: 'str' = '', _preassigned_id: 'str' = '', _memory_mode: 'str | None' = None, _crew_log_asked: \"'tuple[str, int, int] | None'\" = None, *, _execution_context=None, _captured_state=Ellipsis) -> \"'SubagentInfo | dict[str, Any] | None'\"",
    ),
    "_conversation_busy": ("method", "(self, conv_key: 'str') -> 'SubagentInfo | None'"),
    "_create_shared_session": (
        "async method",
        "(self, info: 'SubagentInfo', session_key: 'str', agent: 'str') -> \"'LLMProvider'\"",
    ),
    "_deliver_followups": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_dependency_coordinator": ("method", "(self) -> 'Any'"),
    "_dispatch_async": (
        "async method",
        "(self, params: 'dict[str, Any]') -> 'SubagentInfo | None'",
    ),
    "_drain_queue": ("method", "(self) -> 'None'"),
    "_drain_queue_async": ("async method", "(self) -> 'None'"),
    "_drain_queue_pass": ("async method", "(self) -> 'None'"),
    "_drain_queue_sync": (
        "method",
        "(self, *, refill: 'Callable[..., int]', dispatch: 'Callable[[dict[str, Any]], None] | None' = None) -> 'None'",
    ),
    "_drain_state_writer": (
        "async method",
        "(self, info: 'SubagentInfo', what: 'str', writer: \"'asyncio.Future[Any]'\", *, bound: 'float | None' = None) -> 'bool'",
    ),
    "_effective_turn_limit": ("method", "(self, info: 'SubagentInfo') -> 'int'"),
    "_emit_queue_depth": (
        "method",
        "(self, parent_session_key: 'str', batch_id: 'str' = '', *, wait: 'dict[str, Any] | None' = None) -> 'None'",
    ),
    "_end_leaked_processes": ("async method", "(self) -> 'None'"),
    "_end_orphan_process": (
        "async method",
        "(self, agent_id: 'str', state: 'dict', *, reason: 'str') -> 'str | None'",
    ),
    "_ensure_dedicated_start_priced": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_fire_event": (
        "async method",
        "(self, etype: 'str', info: 'SubagentInfo', extra: 'dict | None' = None) -> 'None'",
    ),
    "_force_reap": (
        "async method",
        "(self, agent_id: 'str', info: 'SubagentInfo', elapsed: 'float', *, reason: 'str' = '') -> 'None'",
    ),
    "_forget_pending_start": ("method", "(self, agent_id: 'str') -> 'None'"),
    "_forget_pressure_clock": ("method", "(self, agent_id: 'str') -> 'None'"),
    "_gate_exit_reset": ("method", "(self, info: 'SubagentInfo') -> 'Callable[..., None]'"),
    "_gate_wait_mark": ("method", "(self, info: 'SubagentInfo') -> 'Callable[..., None]'"),
    "_get_parent_runtime": (
        "method",
        "(self, parent_session_key: 'str') -> \"'AcpRuntime | None'\"",
    ),
    "_hold_for_detached_writer": (
        "method",
        "(self, info: 'SubagentInfo', what: 'str', writer: \"'asyncio.Future[Any]'\") -> 'None'",
    ),
    "_in_startup": ("staticmethod", "(info: 'SubagentInfo') -> 'bool'"),
    "_inherited_context_groups": ("method", "(self, conv_id: 'str') -> 'tuple[bool, bool, bool]'"),
    "_inherited_memory_store": ("method", "(self, conv_id: 'str') -> 'str'"),
    "_initialize_taskq": ("async method", "(self) -> 'None'"),
    "_is_cc_provider": ("staticmethod", "(provider: 'object') -> 'bool'"),
    "_is_orphan_process": ("staticmethod", "(pid: 'int', spawned_at: 'float') -> 'bool'"),
    "_is_pid_alive": ("staticmethod", "(pid: 'int') -> 'bool'"),
    "_is_startup_stalled": (
        "method",
        "(self, info: 'SubagentInfo', now: 'float', mono_now: 'float | None' = None) -> 'bool'",
    ),
    "_keep_recorded_on_disk": ("method", "(self, key: 'str') -> 'bool'"),
    "_kill_orphan_pid": ("async staticmethod", "(pid: 'int') -> 'str | None'"),
    "_leave_for_next_start": (
        "method",
        "(self, info: 'SubagentInfo', *, stop_reason: 'str | None' = None) -> 'bool'",
    ),
    "_leave_startup": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_live_shared_count": (
        "method",
        "(self, pid: 'int | None', agents: \"'list[SubagentInfo]'\") -> 'int'",
    ),
    "_log_spawned": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_maybe_flag_stall": (
        "async method",
        "(self, agent_id: 'str', info: 'SubagentInfo', now: 'float') -> 'None'",
    ),
    "_memory_pressure_hold": (
        "method",
        "(self, *, floor_gb: 'float | None' = None) -> 'int | None'",
    ),
    "_memory_pressure_holds": (
        "method",
        "(self, agent_id: 'str', level: 'int', *, parent_session_key: 'str' = '', batch_id: 'str' = '', available_gb: 'float | None' = None, relabel: 'bool' = False, commit_expiry: 'bool' = True) -> 'str'",
    ),
    "_mint_agent_id": ("method", "(self) -> 'str'"),
    "_note_startup_progress": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_note_tool_dispatch": ("staticmethod", "(info: 'SubagentInfo', event: 'Any') -> 'None'"),
    "_note_tool_result": ("staticmethod", "(info: 'SubagentInfo', event: 'Any') -> 'None'"),
    "_notify_cap_raised": ("method", "(self) -> 'None'"),
    "_notify_orphan": (
        "async method",
        "(self, agent_id: 'str', state: 'dict', has_result: 'bool') -> 'str | None'",
    ),
    "_open_taskq": ("method", "(self) -> 'Any'"),
    "_pressure_lane_started": (
        "method",
        "(self, parent_session_key: 'str', started_at: 'float') -> 'None'",
    ),
    "_promote_conversation": (
        "method",
        "(self, conv_id: 'str', conv_key: 'str', last_used: 'float | None' = None) -> 'None'",
    ),
    "_provider_label_of": ("staticmethod", "(provider: 'object') -> 'str'"),
    "_queued_depth": ("method", "(self, parent_session_key: 'str') -> 'int'"),
    "_queued_depth_async": ("async method", "(self, parent_session_key: 'str') -> 'int'"),
    "_reap_once": (
        "async method",
        "(self, agent_id: 'str', info: 'SubagentInfo', elapsed: 'float', *, reason: 'str' = '') -> 'None'",
    ),
    "_reaper_loop": ("async method", "(self) -> 'None'"),
    "_rebuild_conversation_registry": ("async method", "(self) -> 'None'"),
    "_reconcile_orphans": ("async method", "(self) -> 'None'"),
    "_record_cost": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_record_reap_ending": ("method", "(self, info: 'SubagentInfo', unfinished: 'str') -> 'None'"),
    "_record_slow_command": ("staticmethod", "(info: 'SubagentInfo', idle: 'float') -> 'None'"),
    "_refresh_learned_settled": ("method", "(self) -> 'None'"),
    "_release_admitted_start": ("method", "(self) -> 'str'"),
    "_release_slot": ("method", "(self, info: 'SubagentInfo') -> 'bool'"),
    "_report_queued_stop": (
        "method",
        "(self, params: 'dict', *, row_settled: 'bool' = False, error: 'str' = '', report_owed: 'bool' = False) -> \"'asyncio.Task[bool] | None'\"",
    ),
    "_report_terminal": (
        "async method",
        "(self, info: 'SubagentInfo', *, source: 'str', injection_timeout_reason: 'str', mark_delivered_on_success: 'bool', settle_digest: 'bool' = False, teardown_done: \"'asyncio.Event | None'\" = None, gate: \"'asyncio.Future[bool] | None'\" = None) -> 'bool'",
    ),
    "_report_terminal_guarded": (
        "async method",
        "(self, info: 'SubagentInfo', *, source: 'str', injection_timeout_reason: 'str', mark_delivered_on_success: 'bool', settle_digest: 'bool' = False, teardown_done: \"'asyncio.Event | None'\" = None) -> 'bool'",
    ),
    "_resolved_session_start_width": ("method", "(self) -> 'int'"),
    "_retain_process_handles": (
        "method",
        "(self, agent_id: 'str', session_key: 'str', pairs: 'list[tuple[Any, ProcessHandle]] | None' = None) -> 'list[ProcessHandle]'",
    ),
    "_retire_subagent_oracle": ("staticmethod", "(info: 'SubagentInfo') -> 'None'"),
    "_run": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_run_inner": ("async method", "(self, info: 'SubagentInfo', session_key: 'str') -> 'None'"),
    "_run_terminal_report": (
        "async method",
        "(self, info: 'SubagentInfo', *, source: 'str', injection_timeout_reason: 'str', mark_delivered_on_success: 'bool', settle_digest: 'bool' = False, teardown_done: \"'asyncio.Event | None'\" = None) -> 'bool'",
    ),
    "_safe_announce": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_sample_live_costs": ("method", "(self) -> 'None'"),
    "_scan_keep_states": ("method", "(self) -> 'list[tuple[str, str, str, str, str, float]]'"),
    "_schedule_cancel_recovery": (
        "method",
        "(self, info: 'SubagentInfo', *, reason: 'str' = 'unexpected_cancel') -> 'None'",
    ),
    "_send_orphan_slack_dm": ("async method", "(self, msg: 'str') -> 'None'"),
    "_sessions_under": (
        "method",
        "(self, session_key: 'str') -> 'list[tuple[Any, ProcessHandle]]'",
    ),
    "_settle_continuation": (
        "method",
        "(self, conv_id: 'str', original: 'SubagentInfo | None', result: 'SubagentInfo | None', *, raised: 'bool' = False) -> 'None'",
    ),
    "_settle_digest_holds": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_sharing_plan": (
        "method",
        "(self, info: 'SubagentInfo', *, cfg: 'Any' = None, child_aws_profile: 'ResolvedAwsProfile | None' = None) -> '_SharingPlan'",
    ),
    "_should_stagger_queue": ("method", "(self, now: 'float') -> 'tuple[bool, bool]'"),
    "_should_use_session_sharing": ("method", "(self, info: 'SubagentInfo') -> 'bool'"),
    "_sigkill_session": (
        "async method",
        "(self, session_key: 'str', handle: 'ProcessHandle | None', *, popped: \"'list[tuple[Any, ProcessHandle]] | None'\" = None) -> 'str | None'",
    ),
    "_sigkill_sessions": (
        "async method",
        "(self, session_key: 'str', handles: 'list[ProcessHandle]', *, popped: \"'list[tuple[Any, ProcessHandle]] | None'\" = None) -> 'str | None'",
    ),
    "_sizing_fields": ("staticmethod", "(cfg: 'KiroCrewConfig') -> 'tuple[object, ...]'"),
    "_spawn_after_memory_read": (
        "async method",
        "(self, first: 'Any', proceed: \"'Callable[[], bool] | None'\" = None, /, **reentry: 'Any') -> 'Any'",
    ),
    "_spawn_async_accepted": (
        "async method",
        "(self, task: 'str', prepared: 'PreparedSpawn', **kwargs: 'Any') -> 'SubagentInfo | None'",
    ),
    "_spawn_terminal_report": (
        "method",
        "(self, info: 'SubagentInfo', *, source: 'str', injection_timeout_reason: 'str', mark_delivered_on_success: 'bool', settle_digest: 'bool' = False, teardown_done: \"'asyncio.Event | None'\" = None, gate: \"'asyncio.Future[bool] | None'\" = None) -> \"'asyncio.Task[bool]'\"",
    ),
    "_spawn_with_approval": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_stall_verdict": ("async method", "(self, info: 'SubagentInfo') -> 'tuple[str, str]'"),
    "_start_result_file": ("async method", "(self, info: 'SubagentInfo', text: 'str') -> 'bool'"),
    "_startup_cap": ("method", "(self) -> 'int'"),
    "_startup_deadline": ("property", ""),
    "_startup_population": ("method", "(self, *, exclude: 'SubagentInfo | None' = None) -> 'int'"),
    "_stop_error_text": (
        "method",
        "(self, info: 'SubagentInfo', stop: 'Any', event: 'Any') -> 'str'",
    ),
    "_stop_recovery_wanted": ("method", "(self, info: 'SubagentInfo', stop: 'Any') -> 'bool'"),
    "_sweep_conversations_async": ("async method", "(self, now: 'float') -> 'None'"),
    "_sweep_digest_holds": ("method", "(self, now: 'float') -> 'None'"),
    "_sweep_digest_holds_async": ("async method", "(self, now: 'float') -> 'None'"),
    "_sweep_stuck_waves": ("method", "(self, now: 'float') -> 'None'"),
    "_sweep_stuck_waves_async": ("async method", "(self, now: 'float') -> 'None'"),
    "_taskq_note_stop_recovery": (
        "method",
        "(self, info: 'SubagentInfo', data: 'dict[str, Any]') -> 'None'",
    ),
    "_taskq_pump": ("method", "(self) -> 'None'"),
    "_teardown_run_session": (
        "async method",
        "(self, info: 'SubagentInfo', session_key: 'str') -> 'None'",
    ),
    "_touch_activity": ("async method", "(self, info: 'SubagentInfo') -> 'None'"),
    "_try_inject_orphan_notification": (
        "async method",
        "(self, parent_session: 'str', msg: 'str', meta: 'dict | None' = None, delivery: 'SubagentDelivery | None' = None) -> 'bool | str'",
    ),
    "_unqueue": ("method", "(self, agent_id: 'str', **kwargs: 'Any') -> 'dict | None'"),
    "_write_finished_result_off_loop": (
        "async method",
        "(self, info: 'SubagentInfo', text: 'str | None', *, hold_conversation: 'bool' = False) -> 'bool'",
    ),
    "_write_state_off_loop": (
        "async method",
        "(self, info: 'SubagentInfo', what: 'str', **fields: 'object') -> 'bool'",
    ),
    "_write_tombstone": (
        "staticmethod",
        "(info: 'SubagentInfo', cause: 'str', *, leaked_process: 'bool' = False) -> 'None'",
    ),
    "_yield_for_dependency": (
        "async method",
        "(self, info: 'SubagentInfo', signal: 'Any') -> 'bool | None'",
    ),
    "_yield_for_infra_retry": (
        "async method",
        "(self, info: 'SubagentInfo', infra: 'Any') -> 'str | None'",
    ),
    "_yield_for_stop_recovery": (
        "async method",
        "(self, info: 'SubagentInfo', event: 'Any') -> 'str | None'",
    ),
    "all_agents": ("property", ""),
    "apply_limits": (
        "method",
        "(self, cfg: 'KiroCrewConfig', *, max_concurrent: 'int | None' = None) -> 'None'",
    ),
    "arm_report_in_flight": ("method", "(self, info: 'SubagentInfo') -> 'None'"),
    "batch_members_pending": ("method", "(self, batch_id: 'str') -> 'bool'"),
    "batch_members_pending_async": ("async method", "(self, batch_id: 'str') -> 'bool'"),
    "batch_reports_in_flight": ("method", "(self, batch_id: 'str') -> 'bool'"),
    "cancel": ("async method", "(self, agent_id: 'str') -> 'bool'"),
    "cancel_all": ("async method", "(self) -> 'None'"),
    "cancel_for_parent": ("async method", "(self, parent_session_key: 'str') -> 'tuple[int, int]'"),
    "cancel_for_teardown": (
        "async method",
        "(self, agent_ids: \"'Sequence[str]'\", *, parent_session_key: 'str', verb: 'str' = '') -> 'int'",
    ),
    "cancel_queued": (
        "async method",
        "(self, agent_id: 'str', parent_session_key: 'str', *, stop_origin: 'str' = '') -> 'bool'",
    ),
    "claim_retry": ("method", "(self, failed: 'SubagentInfo') -> 'str'"),
    "close": ("method", "(self) -> 'None'"),
    "commit_identity_sweep_retire": (
        "method",
        "(self, parent_session_key: 'str', expected: \"'AbstractSet[str]'\") -> 'bool'",
    ),
    "consume_report_hold": ("method", "(self, batch_id: 'str', agent_id: 'str') -> 'None'"),
    "continue_conversation": (
        "method",
        "(self, conv_id: 'str', task: 'str', parent_session_key: 'str' = '', agent: 'str' = '', model: 'str | None' = None, max_turns: 'int' = 0, cwd: 'str' = '', _preassigned_id: 'str' = '', _memory_mode: 'str | None' = None, _crew_log_asked: \"'tuple[str, int, int] | None'\" = None) -> 'SubagentInfo | None'",
    ),
    "continue_conversation_async": (
        "async method",
        "(self, conv_id: 'str', task: 'str', parent_session_key: 'str' = '', agent: 'str' = '', model: 'str | None' = None, max_turns: 'int' = 0, cwd: 'str' = '', _preassigned_id: 'str' = '', _memory_mode: 'str | None' = None, _crew_log_asked: \"'tuple[str, int, int] | None'\" = None) -> 'SubagentInfo | None'",
    ),
    "count": ("property", ""),
    "dependency_coordinator": ("method", "(self) -> 'Any'"),
    "dependency_coordinator_async": ("async method", "(self) -> 'Any'"),
    "finalize_batch": ("method", "(self, batch_id: 'str') -> 'None'"),
    "follow_up_run": (
        "async method",
        "(self, agent_id: 'str', message: 'str') -> 'tuple[bool, str]'",
    ),
    "force_digest_flush": (
        "method",
        "(self, batch_id: 'str', parent_session_key: 'str', batch_total: 'int', held_secs: 'float') -> 'None'",
    ),
    "get": ("method", "(self, agent_id: 'str') -> 'SubagentInfo | None'"),
    "has_fenced_children": ("method", "(self, parent_session_key: 'str') -> 'bool'"),
    "has_in_memory_pending_work_for": (
        "method",
        "(self, parent_session_key: 'str', *, exclude_id: 'str' = '') -> 'bool'",
    ),
    "has_live_or_queued_children": ("method", "(self, parent_session_key: 'str') -> 'bool'"),
    "has_live_shared_session": ("method", "(self, session_key: 'str') -> 'bool'"),
    "has_pending_store_children": ("async method", "(self, parent_session_key: 'str') -> 'bool'"),
    "has_pending_work_for": ("method", "(self, parent_session_key: 'str') -> 'bool'"),
    "has_pending_work_for_async": ("async method", "(self, parent_session_key: 'str') -> 'bool'"),
    "has_unfinished_report_for": ("method", "(self, parent_session_key: 'str') -> 'bool'"),
    "is_queued": ("method", "(self, agent_id: 'str') -> 'bool'"),
    "max_concurrent": ("property", ""),
    "memory_pressure_hold_active": ("method", "(self) -> 'bool'"),
    "native_child_resume_refusal": ("method", "(self, conversation_id: 'str') -> 'str | None'"),
    "notify_injection_failed": (
        "method",
        "(self, info: 'SubagentInfo', reason: 'str' = 'delivery timed out') -> 'None'",
    ),
    "open_teardown_fence": ("method", "(self, parent_session_key: 'str') -> \"'set[str] | None'\""),
    "pending_work_count": ("property", ""),
    "prepare_spawn": (
        "method",
        "(self, task: 'str', **kwargs: 'Any') -> \"'SubagentInfo | PreparedSpawn | MemoryReadPoint | None'\"",
    ),
    "published_queued_depths": ("method", "(self) -> 'dict[str, int]'"),
    "queued_count": ("property", ""),
    "queued_count_for": ("method", "(self, parent_session_key: 'str') -> 'int'"),
    "queued_count_for_async": ("async method", "(self, parent_session_key: 'str') -> 'int'"),
    "queued_count_or_none_async": (
        "async method",
        "(self, parent_session_key: 'str') -> 'int | None'",
    ),
    "queued_run_async": ("async method", "(self, agent_id: 'str') -> \"'QueuedRun | None'\""),
    "queued_runs_async": (
        "async method",
        "(self, parent_session_key: 'str | None' = None, *, app: 'str | None' = None) -> \"'QueuedRunListing'\"",
    ),
    "reconfigure": ("async method", "(self, cfg: 'KiroCrewConfig') -> 'None'"),
    "record_lost_submission": (
        "method",
        "(self, batch_id: 'str', batch_total: 'int', reason: 'str', parent_session_key: 'str' = '') -> 'None'",
    ),
    "recorded_cwd": ("method", "(self, conv_id: 'str') -> 'str'"),
    "release_conversation": ("method", "(self, conv_id: 'str') -> 'tuple[bool, str]'"),
    "release_conversation_async": ("async method", "(self, conv_id: 'str') -> 'tuple[bool, str]'"),
    "release_orphan_notices": ("method", "(self) -> 'None'"),
    "release_queue_dispatch": ("method", "(self) -> 'None'"),
    "release_teardown_fence": (
        "method",
        "(self, parent_session_key: 'str', expected: \"'AbstractSet[str] | None'\" = None) -> 'None'",
    ),
    "retry_owed_teardown_sweeps": ("async method", "(self) -> 'int'"),
    "running": ("property", ""),
    "running_agents_for": ("method", "(self, parent_key: 'str') -> 'list[dict]'"),
    "running_count": ("property", ""),
    "set_cap_raise_listener": (
        "method",
        "(self, listener: 'Callable[[], object] | None') -> 'None'",
    ),
    "set_effective_cap": ("method", "(self, cap: 'int | None') -> 'int'"),
    "settle_before_delete": (
        "async method",
        "(self, agent_id: 'str') -> \"Literal['delivered', 'pending']\"",
    ),
    "settle_queued_delivery": (
        "async method",
        "(self, deliveries: 'list[SubagentDelivery]') -> 'None'",
    ),
    "settle_retry": (
        "method",
        "(self, failed: 'SubagentInfo', successor_id: 'str | None') -> 'None'",
    ),
    "snapshot_teardown_children": (
        "method",
        "(self, parent_session_key: 'str') -> 'tuple[str, ...]'",
    ),
    "snapshot_teardown_children_only": (
        "method",
        "(self, parent_session_key: 'str') -> 'tuple[str, ...]'",
    ),
    "spawn": (
        "method",
        "(self, task: 'str', parent_session_key: 'str' = '', agent: 'str' = '', max_turns: 'int' = 0, model: 'str | None' = None, reasoning_effort: 'str' = '', allowed_tools: 'list[str] | None' = None, bare: 'bool' = False, cwd: 'str' = '', approval_mode: 'str | None' = None, silent: 'bool' = False, batch_id: 'str' = '', batch_total: 'int' = 0, keep: 'bool' = False, conversation_key: 'str' = '', app: 'str' = '', include_memory: 'bool' = True, include_lessons: 'bool' = True, include_project: 'bool' = True, memory_store: 'str' = '', _agent_prevalidated: 'bool' = False, _from_queue: 'bool' = False, _preassigned_id: 'str' = '', _crew_log_asked: \"'tuple[str, int, int] | None'\" = None, _memory_mode: 'str | None' = None, _store_accepted: 'bool' = False, _stop_before_claim: 'bool' = False, _claimed: \"'tuple[int, bool, str] | None'\" = None, _window_hint: \"'bool | None'\" = None, _child_registration: 'bool' = True, *, crew: 'str' = '', target_member: 'str | None' = None, delegation: 'dict[str, str] | None' = None, backend: 'str | None' = None, label: 'str' = '', _execution_context: 'dict | None' = None, _parent_spawn_policy: \"'ParentSpawnPolicy | None'\" = None, _parent_project_dirs: \"'list[str] | None'\" = None, _agent_check: \"'AgentCheck | None'\" = None, _recovering_row: 'bool' = False, _stop_before_memory_read: 'bool' = False, _memory_reading: \"'tuple[float, str] | None'\" = None) -> 'SubagentInfo | None'",
    ),
    "spawn_async": (
        "async method",
        "(self, task: 'str', **kwargs: 'Any') -> 'SubagentInfo | None'",
    ),
    "start_reaper": ("method", "(self) -> 'None'"),
    "steer_run": ("async method", "(self, agent_id: 'str', message: 'str') -> 'tuple[bool, str]'"),
    "task_memory_rows": ("method", "(self) -> 'list[dict[str, object]]'"),
    "update_completion_keep": ("method", "(self, mode: 'str', max_chars: 'int') -> 'None'"),
    "user_max_concurrent": ("property", ""),
    "wait_taskq_ready": ("async method", "(self) -> 'None'"),
    "wave_has_live_nested_spawns": ("method", "(self, batch_id: 'str') -> 'bool'"),
}


# ── 1. one namespace ────────────────────────────────────────────────────────


def test_the_owners_define_functions_for_the_sweeps_to_check() -> None:
    by_owner: dict[str, int] = {}
    for label, _fn in _owner_functions():
        owner = label.split(".", 1)[0]
        by_owner[owner] = by_owner.get(owner, 0) + 1
    assert sorted(by_owner) == sorted(o.__name__.rsplit(".", 1)[-1] for o in _owners())
    assert sum(by_owner.values()) >= _MIN_OWNER_FUNCTIONS


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    elsewhere = [label for label, fn in _owner_functions() if fn.__globals__ is not vars(subagent)]
    assert elsewhere == []


def test_the_sweep_reports_a_global_the_namespace_does_not_bind() -> None:
    module_code = compile(
        "def f():\n    def g():\n        return absent\n    return present, g\n", "probe", "exec"
    )
    code = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    probe = types.FunctionType(code, {"__name__": "probe", "present": 1})
    assert _unbound_globals(probe) == ["absent"]


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    missing = {label: names for label, fn in _owner_functions() if (names := _unbound_globals(fn))}
    assert missing == {}


def test_a_value_an_owner_computes_at_import_resolves_in_the_owner() -> None:
    """A field factory runs on its owner's globals, so the owner must bind what it reads."""
    factories = _field_factories()
    assert factories, "the run accounting's credit total is such a factory"
    missing = {label: names for label, fn in factories if (names := _unbound_globals(fn))}
    assert missing == {}
    assert all(fn.__globals__ is not vars(subagent) for _label, fn in factories)


# ── 2. the patch surface reaches every reader ───────────────────────────────


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Local names a test file binds to the ``kiro_crew.subagent`` module object."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            aliases |= {a.asname or a.name for a in node.names if a.name == "subagent"}
        elif isinstance(node, ast.Assign) and _FACADE in ast.unparse(node.value):
            text = ast.unparse(node.value)
            if text.startswith(("importlib.import_module(", "sys.modules[")):
                aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases


def _patched_facade_names(sources) -> set[str]:
    """Every name a test patches on ``kiro_crew.subagent``, from ``(label, text)`` pairs."""
    prefix = f"{_FACADE}."
    names: set[str] = set()
    for _label, text in sources:
        if _FACADE not in text and "import subagent" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        aliases = _facade_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(prefix):
                    names.add(node.value[len(prefix) :].split(".", 1)[0])
            elif isinstance(node, ast.Call) and len(node.args) >= 2:
                target, name = node.args[0], node.args[1]
                if (
                    ast.unparse(node.func).endswith(("setattr", "patch.object", "delattr"))
                    and isinstance(target, ast.Name)
                    and target.id in aliases
                    and isinstance(name, ast.Constant)
                    and isinstance(name.value, str)
                ):
                    names.add(name.value)
    return names


def _test_sources():
    for root in _TEST_TREES:
        for path in sorted(root.rglob("*.py")):
            if root.name == "builtins" and "tests" not in path.parts:
                continue
            yield str(path), path.read_text(encoding="utf-8", errors="replace")
    yield "conftest.py", (_REPO / "conftest.py").read_text(encoding="utf-8")


def test_the_patch_target_scan_sees_every_spelling() -> None:
    sources = [
        ("a", 'from unittest.mock import patch\npatch("kiro_crew.subagent.alpha")\n'),
        (
            "b",
            "import kiro_crew.subagent as sa\ndef t(monkeypatch):\n    monkeypatch.setattr(sa, 'beta', 1)\n",
        ),
        (
            "c",
            "from kiro_crew import subagent\nfrom unittest import mock\nmock.patch.object(subagent, 'gamma')\n",
        ),
        (
            "d",
            "import importlib\nmod = importlib.import_module('kiro_crew.subagent')\ndef t(mp):\n    mp.delattr(mod, 'delta')\n",
        ),
        (
            "e",
            'patch("kiro_crew.subagent.SubagentManager._run")\npatch("kiro_crew.subagent_manager.x")\n',
        ),
        (
            "f",
            "import kiro_crew.other as sa\ndef t(monkeypatch):\n    monkeypatch.setattr(sa, 'never', 1)\n",
        ),
    ]
    assert _patched_facade_names(sources) == {"alpha", "beta", "gamma", "delta", "SubagentManager"}


@pytest.mark.xdist_group(name="tree_scan_test_subagent_parts_composition_contract")
def test_every_name_the_tests_patch_on_the_facade_reaches_every_owner_reader() -> None:
    """A patch of the facade reaches owner code only where that code runs on its globals."""
    patched = _patched_facade_names(_test_sources())
    assert {"sel", "Stats", "check_memory_available", "_RESET_TIMEOUT"} <= patched
    missed: list[str] = []
    readers = [*_owner_functions(), *_field_factories()]
    for label, fn in readers:
        loaded = set(_global_names(fn.__code__)) & patched
        if loaded and fn.__globals__ is not vars(subagent):
            missed.append(f"{label}: {sorted(loaded)}")
    assert missed == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagent, "_env_float", lambda _name, _default: 7.0)
    assert subagent._resolve_injection_timeout() == min(7.0, subagent._ON_DONE_TIMEOUT)
    monkeypatch.setattr(subagent, "_redact", lambda text: "[redacted]")
    info = subagent.SubagentInfo(id="a1", task="t", turns=1, last_tool="Running: x")
    assert "last tool: [redacted]" in subagent._timeout_context(info, include_elapsed=False)


# ── 3. one object, the BASE shape, the facade's identity ────────────────────


class _Shown:
    """Stands in for a default whose repr is unordered (a set's repr varies by hash seed)."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _signature(fn: types.FunctionType) -> str:
    sig = inspect.signature(fn)
    params = [
        (
            p.replace(default=_Shown(f"<{type(p.default).__name__}>"))
            if isinstance(p.default, (set, frozenset, dict))
            else p
        )
        for p in sig.parameters.values()
    ]
    return str(sig.replace(parameters=params))


def _kind(value: object) -> tuple[str, str]:
    if inspect.iscoroutinefunction(value):
        return "async function", _signature(value)  # type: ignore[arg-type]
    if isinstance(value, types.FunctionType):
        return "function", _signature(value)
    if isinstance(value, type):
        return "class", ""
    return "value", type(value).__name__


def _stored_globals(code: types.CodeType):
    """Every global a code object and its nested code objects assign or delete."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _stored_globals(constant)


def _state_the_owners_rebind() -> set[str]:
    """Names owner code rebinds with ``global``: compose runs that code on the facade, so
    the live value is the facade's, and the owner keeps only its import-time binding."""
    return {name for _label, fn in _owner_functions() for name in _stored_globals(fn.__code__)}


def test_every_moved_name_keeps_its_base_shape_and_is_one_object() -> None:
    moved = {name for names in _BASE_SURFACE.values() for name, _kind_, _sig in names}
    assert len(moved) == sum(len(names) for names in _BASE_SURFACE.values())
    rebound = _state_the_owners_rebind()
    assert {"_host_read_in_flight", "_parent_project_resolver"} <= rebound
    for short, names in _BASE_SURFACE.items():
        owner = importlib.import_module(f"{subagent_parts.__name__}.{short}")
        for name, kind, shape in names:
            assert name in vars(owner), f"{short} does not define {name}"
            if name in rebound:
                # Live state lives on the facade (a setter or a test may have rebound it);
                # the BASE shape is the owner's import-time binding.
                assert name in vars(subagent), name
                assert _kind(vars(owner)[name]) == (kind, shape), name
                continue
            value = vars(subagent)[name]
            assert value is vars(owner)[name], f"{name} is two objects"
            assert _kind(value) == (kind, shape), name


def test_a_setter_moved_into_an_owner_rebinds_the_facade_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``set_parent_project_resolver`` writes the global ``parent_project_dirs`` reads."""
    monkeypatch.setattr(subagent, "_parent_project_resolver", None)

    def resolver(_key: str) -> tuple[str, ...]:
        return ()

    subagent.set_parent_project_resolver(resolver)
    assert subagent._parent_project_resolver is resolver


def test_module_and_qualname_still_name_the_facade() -> None:
    """A function the manager binds keeps its one-module qualname, ``SubagentManager.<name>``."""
    host = subagent.SubagentManager
    bound = {id(fn): name for name, member in vars(host).items() for fn in _functions_of(member)}
    for label, fn in _owner_functions():
        rest = label.split(".", 1)[1]
        if "." in rest:
            continue  # a method of an owner's class
        assert fn.__module__ == _FACADE, label
        expected = f"{host.__name__}.{bound[id(fn)]}" if id(fn) in bound else rest
        assert fn.__qualname__ == expected, label
    defined: set[str] = set()
    for owner in _owners():
        tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
        for name in (node.name for node in tree.body if isinstance(node, ast.ClassDef)):
            defined.add(name)
            expected = _FACADE if name in _FACADE_MODULE_CLASSES else owner.__name__
            assert vars(owner)[name].__module__ == expected, name
            if name in _FACADE_MODULE_CLASSES:
                # compose selects an owner's classes by ``__module__``, so it skips this one:
                # it must define no function compose would have to rebind.
                own = [
                    fn
                    for member in vars(vars(owner)[name]).values()
                    for fn in _functions_of(member)
                    if fn.__code__.co_filename == owner.__file__
                ]
                assert own == [], (name, [fn.__name__ for fn in own])
    assert _FACADE_MODULE_CLASSES <= defined, sorted(_FACADE_MODULE_CLASSES - defined)


def test_the_moved_exception_reports_the_facade_module() -> None:
    """``SpawnApprovalUnreachable`` keeps the module the one-module file gave it.

    A traceback, a log line and a pickle name an exception by its module, so it reports
    ``kiro_crew.subagent``, and that module resolves the name to the same class.
    """
    cls = subagent.SpawnApprovalUnreachable
    assert cls.__module__ == _FACADE
    assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls
    line = traceback.format_exception_only(cls("no dashboard client is connected"))[-1]
    assert line == f"{_FACADE}.SpawnApprovalUnreachable: no dashboard client is connected\n"


def test_the_owners_log_through_the_facade_logger() -> None:
    assert subagent.logger.name == _FACADE
    assert [o.__name__ for o in _owners() if "logger" in vars(o)] == []
    assert any("logger" in set(_global_names(fn.__code__)) for _label, fn in _owner_functions())


# ── 4. the import graph ─────────────────────────────────────────────────────


def _is_collaborator(module: str) -> bool:
    """An owner module the manager's class body binds from (any owner but itself)."""
    return module.startswith(subagent_parts.__name__) and _HOST_OWNER not in module.split(".")


def _bad_owner_imports(tree: ast.Module, *, host: bool = False) -> list[str]:
    """Runtime imports of the family; only the host's module may import its collaborators."""
    bad = [
        f"{line}: {mod}"
        for line, mod in _runtime_imports(tree)
        if _family_edges(mod) and not (host and _is_collaborator(mod))
    ]
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.ImportFrom) and stmt.module != _FACADE:
                    bad.append(f"{stmt.lineno}: type-only {stmt.module}")
    return bad


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.subagent import sel\n", True),
        ("import kiro_crew.subagent\n", True),
        ("from kiro_crew.subagent_parts import run_limits\n", True),
        ("from kiro_crew.subagent_parts.run_limits import X\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.subagent import sel\n", False),
        ("if TYPE_CHECKING:\n    from kiro_crew.session import X\n", True),
        ("from kiro_crew.subagent_manager import X\nimport os\n", False),
    ],
)
def test_the_owner_import_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_bad_owner_imports(ast.parse(source))) is flagged


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.subagent_parts import run_surface as _run_surface\n", False),
        ("from kiro_crew.subagent_parts.run_limits import _TURN_LIMIT\n", False),
        ("from kiro_crew.subagent_parts.manager import SubagentManager\n", True),
        ("from kiro_crew.subagent import sel\n", True),
        ("import kiro_crew.subagent\n", True),
    ],
)
def test_the_host_may_import_only_its_collaborators(source: str, flagged: bool) -> None:
    assert bool(_bad_owner_imports(ast.parse(source), host=True)) is flagged


def test_an_owner_imports_the_facade_only_for_type_checking_and_no_sibling() -> None:
    bad = {
        name: found
        for name, tree in _owner_sources().items()
        if (found := _bad_owner_imports(tree, host=name == _HOST_OWNER))
    }
    assert bad == {}


def _imports_an_owner(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")
            if "subagent_parts" in mod or any(a.name == "subagent_parts" for a in node.names):
                return True
        elif isinstance(node, ast.Import) and any("subagent_parts" in a.name for a in node.names):
            return True
    return False


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from kiro_crew.subagent_parts import agent_policy\n", True),
        ("from kiro_crew.subagent_parts.host_memory import x\n", True),
        ("import kiro_crew.subagent_parts.run_limits\n", True),
        ("from kiro_crew import subagent_parts\n", True),
        ("from .subagent_parts import x\n", True),
        ("from kiro_crew.subagent import x\n", False),
    ],
)
def test_the_importer_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert _imports_an_owner(source) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    importers = sorted(
        str(path.relative_to(_SRC))
        for path in sorted((_SRC / "kiro_crew").rglob("*.py"))
        if path.parent != _PARTS_DIR
        and path.name != "subagent.py"
        and "subagent_parts" in (text := path.read_text(encoding="utf-8"))
        and _imports_an_owner(text)
    )
    assert importers == []


def _boundary_gate():
    spec = importlib.util.spec_from_file_location(
        "_g07_agent_sdk_boundary", _REPO / "scripts" / "check_agent_sdk_boundary.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_agent_sdk_boundary_edges_stay_in_the_facade() -> None:
    gate = _boundary_gate()
    edges = {
        path.name: len(gate._violations_in_source(str(path), path.read_text(encoding="utf-8")))
        for path in sorted(_PARTS_DIR.glob("*.py"))
    }
    assert edges == dict.fromkeys(edges, 0)
    facade = Path(subagent.__file__)
    assert (
        len(gate._violations_in_source(str(facade), facade.read_text(encoding="utf-8")))
        == _FACADE_EDGES
    )


# ── 5. imports kept for the owners ──────────────────────────────────────────


def _type_only_imports_from_the_facade() -> set[str]:
    names: set[str] = set()
    roots = [_PARTS_DIR, _SRC / "kiro_crew" / "subagent_manager"]
    for root in roots:
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and (
                    node.module == _FACADE or (node.level and node.module == "subagent")
                ):
                    names |= {a.asname or a.name for a in node.names}
    return names


def _stale(kept: tuple[str, ...], read: set[str]) -> list[str]:
    return sorted(set(kept) - read)


def test_the_stale_check_reports_a_name_nobody_reads() -> None:
    assert _stale(("read", "unread"), {"read", "other"}) == ["unread"]


def _project_imports() -> set[str]:
    """Public names the facade imports from ``kiro_crew``: its surface, kept whoever reads them."""
    tree = ast.parse(Path(subagent.__file__).read_text(encoding="utf-8"))
    return {
        a.asname or a.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module
        and (node.module == "kiro_crew" or node.module.startswith("kiro_crew."))
        for a in node.names
        if not (a.asname or a.name).startswith("_")
    }


def test_every_import_kept_for_the_owners_is_read_by_one() -> None:
    read = {n for _label, fn in _owner_functions() for n in _global_names(fn.__code__)}
    read |= _type_only_imports_from_the_facade() | _project_imports()
    kept = tuple(
        name
        for name, value in vars(subagent).items()
        if any(value is item for item in getattr(subagent, "_PARTS_GLOBAL_BINDINGS", ()))
    )
    assert kept, "the owner-only imports are listed in _PARTS_GLOBAL_BINDINGS"
    assert _stale(kept, read) == []


# ── 6. composition mechanics ────────────────────────────────────────────────


def test_the_facade_composes_through_the_shared_helper() -> None:
    """The facade composes its owners with the one shared helper,
    ``kiro_crew._facade_compose.compose``, calling it exactly once with
    ``host=SubagentManager``, and the owner package keeps no copy of its own: its
    ``__init__`` defines no function or class."""
    assert subagent._facade_compose is facade_compose
    source = Path(subagent.__file__).read_text(encoding="utf-8")
    assert source.count("\n_facade_compose(\n") == 1
    assert "    host=SubagentManager,\n)" in source
    package = ast.parse(Path(subagent_parts.__file__).read_text(encoding="utf-8"))
    defined = [
        node.name
        for node in package.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_methods_and_members(tmp_path: Path) -> None:
    owner = _write_module(
        tmp_path,
        "g07_compose_owner_probe",
        """
        VALUE = "owner"

        def helper():
            return VALUE

        def method(self):
            def nested():
                return VALUE
            return nested()

        def static():
            return VALUE

        class Host:
            def own(self):
                return VALUE

            @property
            def prop(self):
                return VALUE

            bound = method
            frozen = staticmethod(static)
        """,
    )
    namespace = {"__name__": "g07_compose_facade_probe", "VALUE": "facade", "helper": owner.helper}
    facade_compose(namespace, (owner,), host=owner.Host)
    host = owner.Host()
    assert (host.own(), host.prop, host.bound(), owner.Host.frozen()) == ("facade",) * 4
    assert namespace["helper"]() == "facade" and namespace["helper"] is owner.helper
    assert owner.helper.__module__ == "g07_compose_facade_probe"
    assert owner.Host.bound.__qualname__ == "Host.bound"
    assert owner.Host.__module__ == "g07_compose_owner_probe"


def test_a_fresh_facade_import_loads_every_owner() -> None:
    probe = (
        "import sys, kiro_crew.subagent as s\n"
        "print(sorted(m for m in sys.modules if m.startswith('kiro_crew.subagent_parts.')))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, check=True, timeout=120, **UTF8_TEXT
    )
    loaded = ast.literal_eval(done.stdout.strip().splitlines()[-1])
    assert loaded == sorted(o.__name__ for o in _owners())


# ── 7. the manager class binds its collaborators' functions (L33) ───────────


def _member_kind(member: object) -> tuple[str, str]:
    """``(kind, signature)`` of a class member, the binding's ``self`` annotation left out."""
    if isinstance(member, property):
        return "property", ""
    if isinstance(member, staticmethod):
        fn = member.__func__
        kind = "async staticmethod" if inspect.iscoroutinefunction(fn) else "staticmethod"
        return kind, _signature(fn)
    if isinstance(member, types.FunctionType):
        kind = "async method" if inspect.iscoroutinefunction(member) else "method"
        sig = inspect.signature(member)
        params = list(sig.parameters.values())
        if params and params[0].name == "self":
            params[0] = params[0].replace(annotation=inspect.Parameter.empty)
        shown = [
            (
                p.replace(default=_Shown(f"<{type(p.default).__name__}>"))
                if isinstance(p.default, (set, frozenset, dict))
                else p
            )
            for p in params
        ]
        return kind, str(sig.replace(parameters=shown))
    return "attribute", type(member).__name__


def test_the_manager_keeps_every_base_member_with_its_kind_and_signature() -> None:
    host = vars(subagent.SubagentManager)
    members = {
        name for name in host if not name.startswith("__") or name in ("__init__", "__getattr__")
    }
    assert members == set(_BASE_CLASS)
    for name, (kind, shape) in _BASE_CLASS.items():
        assert _member_kind(host[name]) == (kind, shape), name


def test_every_bound_member_is_its_owner_function() -> None:
    """A binding is the owner's function itself, not a wrapper around it."""
    bound = 0
    for owner in _owners():
        short = owner.__name__.rsplit(".", 1)[-1]
        if short == _HOST_OWNER:
            continue
        for name, value in vars(owner).items():
            if (
                not isinstance(value, types.FunctionType)
                or value.__code__.co_filename != owner.__file__
            ):
                continue
            member = vars(subagent.SubagentManager).get(name)
            if member is None:
                continue
            assert getattr(member, "__func__", member) is value, f"{short}.{name}"
            bound += 1
    assert bound >= _MIN_BOUND_MEMBERS


def _class_cell_uses(tree: ast.AST) -> list[str]:
    """Spellings that tie a function to the class body it was written in."""
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
        ):
            if not node.args:
                found.append(f"{node.lineno}: zero-argument super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append(f"{node.lineno}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            found.append(f"{node.lineno}: mangled {node.attr}")
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().x()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).x(), self.__dict__\n", False),
    ],
)
def test_the_class_cell_scan_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_class_cell_uses(ast.parse(source))) is flagged


def test_no_collaborator_function_needs_the_class_body() -> None:
    """Zero-argument ``super()``, ``__class__`` and mangled names bind at the class body."""
    found = {
        name: hits
        for name, tree in _owner_sources().items()
        if name != _HOST_OWNER and (hits := _class_cell_uses(tree))
    }
    assert found == {}
