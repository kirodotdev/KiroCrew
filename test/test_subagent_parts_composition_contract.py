"""``kiro_crew.subagent`` stays one namespace while its definitions live in ``subagent_parts``.

``kiro_crew.subagent`` keeps its import path, its patch surface and every construct a
path-keyed repository guard reads in that file; the modules of ``kiro_crew.subagent_parts``
hold the rest, one responsibility each, and ``subagent_parts.compose`` runs every function
they define on the facade's globals. These tests pin what that composition promises:

* each owner function runs on the facade's globals, and every global it reads resolves
  there, so a patch of ``kiro_crew.subagent.<name>`` reaches it wherever it now lives -- and
  every name the test trees patch there is read by no owner code running anywhere else;
* a value an owner computes while it is imported (a dataclass field factory) resolves the
  names it reads in the owner's own module;
* each moved name keeps the kind and signature it had in the one-module file, and is ONE
  object whichever module a caller reads it from;
* a function's ``__module__`` still names the facade, and an owner's classes keep their own;
* nothing but the facade imports an owner, an owner imports the facade only for type
  checking and imports no sibling at runtime, and no owner carries an agent-sdk boundary
  edge;
* every import the facade keeps only for the owners is read by one.
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
import types
from pathlib import Path

import pytest

import kiro_crew.subagent as subagent
from kiro_crew import subagent_parts
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
_MIN_OWNER_FUNCTIONS = 70

#: Agent-sdk boundary edges ``subagent.py`` carries (``.github/agent-sdk-boundary-baseline.txt``).
_FACADE_EDGES = 7

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
            "(parent_session_key: 'str', template: 'str') -> 'tuple[tuple[str, ...], ...] | None'",
        ),
        ("ParentRecordUnreadable", "class", ""),
        ("_parent_template_for_spawn", "function", "(parent_session_key: 'str') -> 'str'"),
        ("ParentSpawnPolicy", "value", "GenericAlias"),
        ("parent_spawn_policy", "function", "(parent_session_key: 'str') -> 'ParentSpawnPolicy'"),
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
        ("_windows_available_memory_gb", "function", "() -> 'float'"),
        ("_macos_vm_reclaimable_pages", "function", "() -> 'Optional[int]'"),
        ("_macos_available_memory_gb", "function", "() -> 'float'"),
        ("_CGROUP_UNLIMITED", "value", "int"),
        ("_read_int_file", "function", "(path: 'str') -> 'int | None'"),
        ("_MEMORY_STAT_READ_CAP", "value", "int"),
        (
            "_read_inactive_file_bytes",
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
        ("compute_memory_sized_parallel_cap", "function", "(cfg: 'KiroCrewConfig') -> 'int'"),
        ("_host_mem_term", "function", "(cfg: 'KiroCrewConfig') -> 'int | None'"),
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
    for label, fn in _owner_functions():
        if "." in label.split(".", 1)[1]:
            continue  # a method of an owner's class
        assert fn.__module__ == _FACADE, label
        assert fn.__qualname__ == label.split(".", 1)[1], label
    for owner in _owners():
        for value in vars(owner).values():
            if isinstance(value, type) and value.__name__ in vars(owner):
                if value.__module__.startswith(subagent_parts.__name__):
                    assert value.__module__ == owner.__name__


def test_the_owners_log_through_the_facade_logger() -> None:
    assert subagent.logger.name == _FACADE
    assert [o.__name__ for o in _owners() if "logger" in vars(o)] == []
    assert any("logger" in set(_global_names(fn.__code__)) for _label, fn in _owner_functions())


# ── 4. the import graph ─────────────────────────────────────────────────────


def _bad_owner_imports(tree: ast.Module) -> list[str]:
    bad = [f"{line}: {mod}" for line, mod in _runtime_imports(tree) if _family_edges(mod)]
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


def test_an_owner_imports_the_facade_only_for_type_checking_and_no_sibling() -> None:
    bad = {
        name: found
        for name, tree in _owner_sources().items()
        if (found := _bad_owner_imports(tree))
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
    subagent_parts.compose(namespace, owner.Host, (owner,))
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
