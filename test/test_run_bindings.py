"""``RunEventCoordinator`` keeps every member it had, most bound from the ``run_*`` owner that holds it.

``subagent_manager/run.py`` keeps the run's read surface, its queued-depth publication, its
MCP notices and its tool-approval ladder, and binds every other member by name from five
sibling owners: ``run_records``, ``run_lifecycle``, ``run_turn``, ``run_yields`` and
``run_start``. Each body is a module-level function whose first parameter is ``self`` (the
two ``*_db`` members are bound through ``staticmethod``), so ``bind_component_globals`` and
every test that reaches a member through the class see the code they always did. This file
pins that contract:

* the frozen member inventory: every member, its kind, its signature (the ``self``
  annotation aside) and the module that holds it;
* each bound member runs its owner's code: a plain or static member IS the owner's
  function, and a ``*_impl`` member, which ``bind_component_globals`` rebinds in place, runs
  the owner's code object; no owner keeps a function the class does not bind;
* every ``*_impl`` member runs on ``kiro_crew.subagent``'s globals and every global it reads
  resolves there, so a patch of a ``kiro_crew.subagent`` name reaches it; every other member
  runs on its owner's globals and every global it reads resolves there;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports ``run.py`` at run time, and nothing else in ``src`` imports an owner;
* each owner's docstring names the manager fields its members write and read;
* no name the test trees patch through ``kiro_crew.subagent_manager.run`` is read as a global
  by an owner.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import builtins
import dis
import functools
import importlib
import inspect
import re
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import Any

import pytest
from facade_mirror_scan import patched_names

import kiro_crew.subagent as subagent_module
from kiro_crew.subagent_manager import run as run_module
from kiro_crew.subagent_manager.run import RunEventCoordinator

pytestmark = pytest.mark.xdist_group(name="tree_scan_run_bindings")

_PACKAGE = "kiro_crew.subagent_manager"
_RUN = f"{_PACKAGE}.run"
_OWNERS = ("run_records", "run_lifecycle", "run_turn", "run_yields", "run_start")
_TEST_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TEST_ROOT.parent

#: Member -> (kind, signature with the ``self`` annotation removed, holding module). The kind
#: and signature are the one-module class's. ``run`` is ``subagent_manager/run.py`` itself;
#: ``subagent_persistence`` holds the three persistence helpers the class head wraps.
_MEMBERS: dict[str, tuple[str, str, str]] = {
    "_publish_identity": (
        "staticmethod",
        "(agent_id: 'str', *, session_id: 'str' = '', provider: 'str' = '', cwd: 'str' = '', keep: 'bool | None' = None, conversation_key: 'str' = '') -> 'None'",
        "subagent_persistence",
    ),
    "_remember_identity": (
        "staticmethod",
        "(agent_id: 'str', *, session_id: 'str' = '', provider: 'str' = '', cwd: 'str' = '', keep: 'bool | None' = None, conversation_key: 'str' = '') -> 'None'",
        "subagent_persistence",
    ),
    "_stop_in_progress": ("staticmethod", "(info: 'SubagentInfo') -> 'bool'", "run"),
    "_write_run_agent": (
        "staticmethod",
        "(agent_id: 'str', agent: 'str | None', *, kind: 'str' = 'template') -> 'None'",
        "subagent_persistence",
    ),
    "_forget_finished_live_state": (
        "function",
        "(self, info: 'SubagentInfo') -> 'None'",
        "run_records",
    ),
    "_effective_turn_limit_impl": (
        "function",
        "(self, info: 'SubagentInfo') -> 'int'",
        "run_lifecycle",
    ),
    "_write_state_off_loop_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', what: 'str', **fields: 'object') -> 'bool'",
        "run_records",
    ),
    "_write_finished_result_off_loop_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', text: 'str | None', *, hold_conversation: 'bool' = False) -> 'bool'",
        "run_records",
    ),
    "_start_result_file_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', text: 'str') -> 'bool'",
        "run_records",
    ),
    "_drain_state_writer_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', what: 'str', writer: \"'asyncio.Future[Any]'\", *, bound: 'float | None' = None) -> 'bool'",
        "run_records",
    ),
    "_hold_for_detached_writer_impl": (
        "function",
        "(self, info: 'SubagentInfo', what: 'str', writer: \"'asyncio.Future[Any]'\") -> 'None'",
        "run_records",
    ),
    "_cap_unclaimed_result_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo') -> 'None'",
        "run_records",
    ),
    "_remember_identity_off_loop": (
        "coroutine",
        "(self, info: 'SubagentInfo', *, session_id: 'str', provider: 'str', cwd: 'str' = '', keep: 'bool | None' = None, conversation_key: 'str' = '') -> 'None'",
        "run_records",
    ),
    "_await_identity_write": (
        "coroutine",
        "(self, info: 'SubagentInfo', writer: 'asyncio.Future[None]') -> 'None'",
        "run_records",
    ),
    "update_completion_keep_impl": (
        "function",
        "(self, mode: 'str', max_chars: 'int') -> 'None'",
        "run_lifecycle",
    ),
    "running_agents_for_impl": ("function", "(self, parent_key: 'str') -> 'list[dict]'", "run"),
    "get_impl": ("function", "(self, agent_id: 'str') -> 'SubagentInfo | None'", "run"),
    "is_queued_impl": ("function", "(self, agent_id: 'str') -> 'bool'", "run"),
    "_teardown_run_session_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', session_key: 'str') -> 'None'",
        "run_lifecycle",
    ),
    "_record_reap_ending_impl": (
        "function",
        "(self, info: 'SubagentInfo', unfinished: 'str') -> 'None'",
        "run_lifecycle",
    ),
    "_run_impl": ("coroutine", "(self, info: 'SubagentInfo') -> 'None'", "run_lifecycle"),
    "_touch_activity_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo') -> 'None'",
        "run_lifecycle",
    ),
    "_fire_event_impl": (
        "coroutine",
        "(self, etype: 'str', info: 'SubagentInfo', extra: 'dict | None' = None) -> 'None'",
        "run_lifecycle",
    ),
    "_queued_depth_impl": ("function", "(self, parent_session_key: 'str') -> 'int'", "run"),
    "_window_depth": (
        "function",
        "(self, parent_session_key: 'str', *, include_recovering: 'bool' = True) -> 'int'",
        "run",
    ),
    "_queued_depth_async_impl": ("coroutine", "(self, parent_session_key: 'str') -> 'int'", "run"),
    "queued_count_for_impl": ("function", "(self, parent_session_key: 'str') -> 'int'", "run"),
    "queued_count_for_async_impl": (
        "coroutine",
        "(self, parent_session_key: 'str') -> 'int'",
        "run",
    ),
    "_in_window_count": ("function", "(self, parent_session_key: 'str') -> 'int'", "run"),
    "_has_live_parent_run_task": (
        "function",
        "(self, parent_session_key: 'str', *, exclude_id: 'str' = '') -> 'bool'",
        "run",
    ),
    "has_unfinished_report_for_impl": (
        "function",
        "(self, parent_session_key: 'str') -> 'bool'",
        "run",
    ),
    "_has_live_parent_followup_watcher": (
        "function",
        "(self, parent_session_key: 'str') -> 'bool'",
        "run",
    ),
    "has_in_memory_pending_work_for_impl": (
        "function",
        "(self, parent_session_key: 'str', *, exclude_id: 'str' = '') -> 'bool'",
        "run",
    ),
    "queued_count_or_none_async_impl": (
        "coroutine",
        "(self, parent_session_key: 'str') -> 'int | None'",
        "run",
    ),
    "has_pending_work_for_impl": ("function", "(self, parent_session_key: 'str') -> 'bool'", "run"),
    "has_pending_work_for_async_impl": (
        "coroutine",
        "(self, parent_session_key: 'str') -> 'bool'",
        "run",
    ),
    "_emit_queue_depth_impl": (
        "function",
        "(self, parent_session_key: 'str', batch_id: 'str' = '', *, wait: 'dict[str, Any] | None' = None) -> 'None'",
        "run",
    ),
    "_live_depth_emit": (
        "function",
        "(self, parent_session_key: 'str') -> '_PendingDepthEmit | None'",
        "run",
    ),
    "_request_queue_depth": (
        "function",
        "(self, parent_session_key: 'str', batch_ids: 'set[str]', attempt: 'int' = 0, *, heal: 'bool' = False) -> 'None'",
        "run",
    ),
    "_queue_depth_burst": (
        "coroutine",
        "(self, parent_session_key: 'str', emit: '_PendingDepthEmit') -> 'None'",
        "run",
    ),
    "_published_depth": ("function", "(self, parent_session_key: 'str') -> 'int'", "run"),
    "_publish_queue_depth": (
        "coroutine",
        "(self, parent_session_key: 'str', depth: 'int', batch_id: 'str', *, forget_label: 'bool') -> 'None'",
        "run",
    ),
    "_arm_queue_depth_retry": (
        "function",
        "(self, parent_session_key: 'str', attempt: 'int', batch_ids: 'set[str]') -> 'None'",
        "run",
    ),
    "_fire_queue_depth_retry": ("function", "(self, parent_session_key: 'str') -> 'None'", "run"),
    "_disarm_queue_depth_retry": ("function", "(self, parent_session_key: 'str') -> 'None'", "run"),
    "_read_queue_depth": ("coroutine", "(self, parent_session_key: 'str') -> 'int | None'", "run"),
    "_warn_unusable_mcp_servers": (
        "function",
        "(self, info: 'SubagentInfo', client: 'LLMProvider') -> 'str'",
        "run",
    ),
    "_spawn_mcp_notice": ("function", "(self, summary: 'str') -> 'str'", "run"),
    "_crew_log_approval_id": (
        "function",
        "(self, info: 'SubagentInfo', request_id: 'object') -> 'str'",
        "run",
    ),
    "_record_crew_log_tool_approval_requested": (
        "function",
        "(self, info: 'SubagentInfo', *, approval_id: 'str', tool: 'str', reason: 'str') -> \"'tuple[str, int]'\"",
        "run",
    ),
    "_permission_policy": (
        "function",
        "(self, info: 'SubagentInfo', *, spec: 'Any', parent_policy: 'str', consult: 'Callable[[LLMEvent], object]', sel: 'Callable[[], Any]', log: '_logging.Logger') -> 'Policy'",
        "run",
    ),
    "_run_inner_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', session_key: 'str', usage: '_RunCreditAccounting') -> 'None'",
        "run_turn",
    ),
    "_stop_recovery_wanted_impl": (
        "function",
        "(self, info: 'SubagentInfo', stop: 'Any') -> 'bool'",
        "run_yields",
    ),
    "_yield_for_stop_recovery_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', event: 'Any') -> 'str | None'",
        "run_yields",
    ),
    "_await_lane_resume_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', *, reason: 'str', timeout: 'float', request: 'bool' = True) -> 'bool'",
        "run_yields",
    ),
    "_withdraw_resume": ("function", "(self, info: \"'SubagentInfo'\") -> 'None'", "run_yields"),
    "_yield_for_dependency_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', signal: 'Any') -> 'bool | None'",
        "run_yields",
    ),
    "_yield_for_infra_retry_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', infra: 'Any') -> 'str | None'",
        "run_yields",
    ),
    "ensure_running_marked": (
        "function",
        "(self, info: \"'SubagentInfo'\") -> 'None'",
        "run_yields",
    ),
    "_dependency_report_db": (
        "staticmethod",
        "(coordinator: 'Any', store: 'Any', task_id: 'str', signal: 'Any', generation: 'int | None') -> 'Any'",
        "run_yields",
    ),
    "_dependency_verdict": (
        "coroutine",
        "(self, coordinator: \"'Any'\", info: \"'SubagentInfo'\", signal: \"'Any'\") -> \"'Any'\"",
        "run_yields",
    ),
    "lane_wait_for_status": (
        "function",
        "(self, info: \"'SubagentInfo'\", event: 'Any') -> 'bool'",
        "run_yields",
    ),
    "_taskq_note_stop_recovery_impl": (
        "function",
        "(self, info: 'SubagentInfo', data: 'dict[str, Any]') -> 'None'",
        "run_yields",
    ),
    "_stop_recovery_note_db": (
        "staticmethod",
        "(store: \"'Any'\", task_id: 'str', generation: 'int', data: \"'dict[str, Any]'\") -> 'None'",
        "run_yields",
    ),
    "_stop_error_text_impl": (
        "function",
        "(self, info: 'SubagentInfo', stop: 'Any', event: 'Any') -> 'str'",
        "run_yields",
    ),
    "_should_use_session_sharing_impl": (
        "function",
        "(self, info: 'SubagentInfo') -> 'bool'",
        "run_start",
    ),
    "_sharing_plan_impl": (
        "function",
        "(self, info: 'SubagentInfo', *, cfg: 'Any' = None, child_aws_profile: 'ResolvedAwsProfile | None' = None) -> '_SharingPlan'",
        "run_start",
    ),
    "_aws_profile_split_possible": (
        "function",
        "(self, info: 'SubagentInfo') -> 'bool'",
        "run_start",
    ),
    "_resolve_child_aws_profile": (
        "coroutine",
        "(self, info: 'SubagentInfo') -> 'ResolvedAwsProfile | None'",
        "run_start",
    ),
    "_ensure_dedicated_start_priced_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo') -> 'None'",
        "run_start",
    ),
    "_gate_exit_reset_impl": (
        "function",
        "(self, info: 'SubagentInfo') -> \"'Callable[..., None]'\"",
        "run_start",
    ),
    "_gate_wait_mark_impl": (
        "function",
        "(self, info: 'SubagentInfo') -> \"'Callable[..., None]'\"",
        "run_start",
    ),
    "_create_shared_session_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', session_key: 'str', agent: 'str') -> \"'LLMProvider'\"",
        "run_start",
    ),
    "_await_late_start_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', session_key: 'str', exc: 'Exception') -> \"'LLMProvider'\"",
        "run_start",
    ),
    "_bind_shared_handle_impl": (
        "coroutine",
        "(self, info: 'SubagentInfo', session_key: 'str', runtime: \"'AcpRuntime'\", handle: 'Any') -> \"'LLMProvider'\"",
        "run_start",
    ),
    "_get_parent_runtime_impl": (
        "function",
        "(self, parent_session_key: 'str') -> \"'AcpRuntime | None'\"",
        "run_start",
    ),
}


def _owner(name: str) -> ModuleType:
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))


def _kind(value: object) -> str:
    if isinstance(value, staticmethod):
        return "staticmethod"
    if inspect.iscoroutinefunction(value):
        return "coroutine"
    if inspect.isfunction(value):
        return "function"
    return "data"


def _function(value: Any) -> FunctionType:
    return value.__func__ if isinstance(value, staticmethod) else value


def _signature(value: Any) -> str:
    """The member's signature with the ``self`` annotation removed."""
    signature = inspect.signature(_function(value))
    params = [
        p.replace(annotation=inspect.Parameter.empty) if p.name == "self" else p
        for p in signature.parameters.values()
    ]
    return str(signature.replace(parameters=params))


def _members() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(RunEventCoordinator).items()
        if isinstance(value, (FunctionType, staticmethod))
    }


def _bound() -> list[str]:
    return sorted(name for name, (_k, _s, holder) in _MEMBERS.items() if holder in _OWNERS)


# --------------------------------------------------------------------------- #
# The member inventory and the binding.
# --------------------------------------------------------------------------- #


def test_the_kind_reader_tells_each_kind_apart() -> None:
    async def stand_in(self: object) -> None:
        return None

    def plain(self: object) -> None:
        return None

    assert (_kind(stand_in), _kind(plain), _kind(staticmethod(plain)), _kind(1)) == (
        "coroutine",
        "function",
        "staticmethod",
        "data",
    )


def test_the_member_set_is_the_frozen_inventory() -> None:
    assert sorted(_members()) == sorted(_MEMBERS)


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_holder(name: str) -> None:
    kind, signature, holder = _MEMBERS[name]
    value = _members()[name]
    assert _kind(value) == kind
    assert _signature(value) == signature
    assert _function(value).__module__.rsplit(".", 1)[-1] == holder


@pytest.mark.parametrize("name", _bound())
def test_a_bound_member_runs_its_owners_code(name: str) -> None:
    value = _members()[name]
    owned = vars(_owner(_MEMBERS[name][2]))[name]
    if name.endswith("_impl"):
        # ``bind_component_globals`` replaced the class attribute with a copy that runs the
        # owner's code object on ``kiro_crew.subagent``'s globals.
        assert _function(value).__code__ is owned.__code__
    else:
        # The class attribute IS the owner's function, not a wrapper around it.
        assert _function(value) is owned


def test_every_owner_holds_a_member_and_sits_beside_run() -> None:
    holding = {holder for _kind, _sig, holder in _MEMBERS.values()} & set(_OWNERS)
    assert holding == set(_OWNERS)
    siblings = {p.stem for p in Path(str(run_module.__file__)).parent.glob("run_*.py")}
    assert siblings == set(_OWNERS)


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_keeps_a_function_the_class_does_not_bind(owner: str) -> None:
    module = _owner(owner)
    members = _members()
    unbound = [
        node.name
        for node in _functions(_tree(module))
        if node.name not in members
        or _function(members[node.name]).__code__ is not vars(module)[node.name].__code__
    ]
    assert unbound == []


# --------------------------------------------------------------------------- #
# Where each member's globals come from.
# --------------------------------------------------------------------------- #


def _global_loads(code: CodeType) -> set[str]:
    """Every name *code* (and each function nested in it) reads as a module global."""
    found: set[str] = set()
    stack = [code]
    while stack:
        current = stack.pop()
        for instruction in dis.get_instructions(current):
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}:
                found.add(str(instruction.argval))
        stack += [const for const in current.co_consts if isinstance(const, CodeType)]
    return found


def _unresolved(function: FunctionType) -> list[str]:
    namespace = function.__globals__
    return sorted(
        name
        for name in _global_loads(function.__code__)
        if name not in namespace and not hasattr(builtins, name)
    )


def test_the_resolution_scan_can_fail() -> None:
    def reads_a_missing_global(self: object) -> object:
        return _a_name_no_module_binds  # type: ignore[name-defined]  # noqa: F821

    assert _unresolved(reads_a_missing_global) == ["_a_name_no_module_binds"]


@pytest.mark.parametrize("name", sorted(n for n in _MEMBERS if n.endswith("_impl")))
def test_an_impl_member_runs_on_the_subagent_globals(name: str) -> None:
    rebound = _function(_members()[name])
    assert rebound.__globals__ is vars(subagent_module)
    assert _unresolved(rebound) == []


@pytest.mark.parametrize("name", sorted(n for n in _bound() if not n.endswith("_impl")))
def test_a_plain_member_runs_on_its_owner_globals(name: str) -> None:
    function = _function(_members()[name])
    assert function.__globals__ is vars(_owner(_MEMBERS[name][2]))
    assert _unresolved(function) == []


# --------------------------------------------------------------------------- #
# Constructs that only work inside a class body.
# --------------------------------------------------------------------------- #


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What in *fn* only works inside a class body."""
    found = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            found.append(f"{fn.name}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            found.append(f"{fn.name}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            found.append(f"{fn.name}: {node.attr}")
    return found


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().g()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).g()\n", False),
        ("def f(self):\n    return self.__dict__, self._private\n", False),
    ],
)
def test_the_class_only_scan_sees_each_shape(source: str, flagged: bool) -> None:
    (fn,) = _functions(ast.parse(source))
    assert bool(_class_only_constructs(fn)) is flagged


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    functions = _functions(_tree(_owner(owner)))
    assert [c for fn in functions for c in _class_only_constructs(fn)] == []


# --------------------------------------------------------------------------- #
# Owners never import run.py, and only run.py imports an owner.
# --------------------------------------------------------------------------- #


def _runtime_imports(tree: ast.Module) -> set[str]:
    """Dotted modules *tree* imports outside ``if TYPE_CHECKING:``, a relative import
    resolved against the ``subagent_manager`` package."""
    found: set[str] = set()

    def visit(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
                visit(node.orelse)
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    found.update(alias.name for alias in sub.names)
                elif isinstance(sub, ast.ImportFrom):
                    if sub.level:
                        base = _PACKAGE.split(".")[: len(_PACKAGE.split(".")) - sub.level + 1]
                        module = ".".join(base + ([sub.module] if sub.module else []))
                    else:
                        module = sub.module or ""
                    found.add(module)
                    found.update(f"{module}.{alias.name}" for alias in sub.names)

    visit(tree.body)
    return found


def test_the_runtime_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .run import RunEventCoordinator\n"
        "from . import run\n"
        "def f(self):\n"
        "    from .run import RunEventCoordinator\n"
    )
    imports = _runtime_imports(tree)
    assert f"{_PACKAGE}.run" in imports
    assert f"{_RUN}.RunEventCoordinator" in imports


@pytest.mark.parametrize("owner", _OWNERS)
def test_no_owner_imports_run_at_run_time(owner: str) -> None:
    imports = _runtime_imports(_tree(_owner(owner)))
    assert {_RUN, f"{_RUN}.RunEventCoordinator"} & imports == set()


def _owner_importers(src: Path) -> list[str]:
    """Every ``src`` module other than run.py that imports a ``run_*`` owner."""
    owners = {f"{_PACKAGE}.{name}" for name in _OWNERS}
    found = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).with_suffix("")
        module = ".".join(rel.parts)
        if module in owners | {_RUN}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(name in text for name in _OWNERS):
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                parent = rel.parts[:-1]
                if node.level:
                    base = list(parent[: len(parent) - node.level + 1])
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                names = {f"{target}.{alias.name}" for alias in node.names}
                if ({target} | names) & owners:
                    found.append(module)
            elif isinstance(node, ast.Import):
                if {alias.name for alias in node.names} & owners:
                    found.append(module)
    return found


def test_the_owner_importer_scan_can_fail(tmp_path: Path) -> None:
    pkg = tmp_path / "kiro_crew" / "subagent_manager"
    pkg.mkdir(parents=True)
    (pkg / "terminal.py").write_text("from .run_turn import f\n", encoding="utf-8")
    (tmp_path / "kiro_crew" / "other.py").write_text(
        "from kiro_crew.subagent_manager import run_start\n", encoding="utf-8"
    )
    assert _owner_importers(tmp_path) == [
        "kiro_crew.other",
        "kiro_crew.subagent_manager.terminal",
    ]


def test_nothing_else_in_src_imports_an_owner() -> None:
    assert _owner_importers(_REPO_ROOT / "src") == []


# --------------------------------------------------------------------------- #
# Each owner names the manager fields it writes and reads.
# --------------------------------------------------------------------------- #

_MUTATORS = frozenset(
    {"pop", "add", "discard", "setdefault", "append", "clear", "update", "remove"}
)

#: Fields an owner changes in place through a call the scan cannot read as a write:
#: ``run_lifecycle`` records a frame's depth with ``_published_depths.record``, and
#: ``run_turn`` passes ``_agents`` to ``evict_completed_agents``, which deletes the
#: completed runs it prunes.
_WRITES_THE_SCAN_MISSES: dict[str, frozenset[str]] = {
    "run_lifecycle": frozenset({"_published_depths"}),
    "run_turn": frozenset({"_agents"}),
}


def _fields(tree: ast.Module) -> tuple[set[str], set[str]]:
    """``(written, read)`` manager fields: ``self._manager.<f>`` and ``<alias>.<f>`` for a
    name bound to ``self._manager`` (by assignment or as a parameter default), plus a write
    through a name bound to ``self._manager.<f>``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and ast.unparse(node.value) == "self._manager"
        ):
            aliases.add(node.targets[0].id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            positional = args.posonlyargs + args.args
            pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            aliases.update(a.arg for a, d in pairs if ast.unparse(d) == "self._manager")

    def on_manager(holder: ast.expr) -> bool:
        return (isinstance(holder, ast.Name) and holder.id in aliases) or (
            isinstance(holder, ast.Attribute)
            and holder.attr == "_manager"
            and isinstance(holder.value, ast.Name)
            and holder.value.id == "self"
        )

    field_aliases = {
        node.targets[0].id: node.value.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Attribute)
        and on_manager(node.value.value)
    }
    written: set[str] = set()
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and on_manager(node.value):
            (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(node.attr)
    for node in ast.walk(tree):
        # ``mgr.f[k] = v``, ``mgr.f.pop(k)`` and ``q = mgr.f; q.pop(k)`` write ``f`` too.
        target = None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            target = node.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
        ):
            target = node.func.value
        if isinstance(target, ast.Attribute) and on_manager(target.value):
            written.add(target.attr)
        elif isinstance(target, ast.Name) and target.id in field_aliases:
            written.add(field_aliases[target.id])
    return written, read - written


def test_the_field_scan_sees_writes_and_reads() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    manager = self._manager\n"
        "    manager._a = 1\n"
        "    manager._b[1] = 2\n"
        "    self._manager._c.pop(1)\n"
        "    self._manager._g -= 1\n"
        "    queue = self._manager._h\n"
        "    queue.pop(0)\n"
        "    def settled(m=self._manager):\n"
        "        return m._i\n"
        "    return self._manager._d, getattr(self._manager, '_e', None), self._f\n"
    )
    assert _fields(tree) == ({"_a", "_b", "_c", "_g", "_h"}, {"_d", "_i"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)(?:\. [A-Z]|\.$)", " ".join(doc.split()))
    assert match, f"no {label!r} sentence"
    return set(re.findall(r"``(\w+)``", match.group(1)))


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = _owner(owner)
    written, read = _fields(_tree(module))
    unscanned = _WRITES_THE_SCAN_MISSES.get(owner, frozenset())
    doc = module.__doc__ or ""
    assert _listed(doc, "Writes") == written | unscanned
    # The read list names what is read without being written, plus fields reached
    # through ``getattr`` (which the scan cannot see) -- never fewer than the scan finds.
    assert read - unscanned <= _listed(doc, "Reads")


# --------------------------------------------------------------------------- #
# No name a test patches through run.py is read by an owner.
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    """The root conftest, ``test/`` and the builtin apps' test modules and conftests."""
    files = [_REPO_ROOT / "conftest.py", *sorted(_TEST_ROOT.rglob("*.py"))]
    builtins_root = _REPO_ROOT / "src" / "kiro_crew" / "apps" / "builtins"
    for path in sorted(builtins_root.rglob("*.py")):
        rel = path.relative_to(builtins_root).as_posix()
        if "/tests/" in rel or path.name.startswith("test_") or path.name == "conftest.py":
            files.append(path)
    return files


def _patched_through_run(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # Every spelling of the run module, and every alias a test binds to it, names
        # the package.
        if "subagent_manager" not in text:
            continue
        try:
            found = patched_names(text, {_RUN: _RUN})
        except SyntaxError:
            continue
        names |= found.get(_RUN, set())
    return names


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    return frozenset(_patched_through_run(_test_tree_files()))


def _owner_reads() -> dict[str, set[str]]:
    reads: dict[str, set[str]] = {}
    for owner in _OWNERS:
        module = _owner(owner)
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        reads[module.__name__] = _global_loads(compile(source, str(module.__file__), "exec"))
    return reads


def _reach_gaps(patched: set[str], reads: dict[str, set[str]]) -> list[str]:
    return sorted(f"{owner}.{name}" for owner, names in reads.items() for name in patched & names)


def test_the_test_tree_walk_reaches_every_collected_tree() -> None:
    files = _test_tree_files()
    assert Path(__file__).resolve() in files
    assert _REPO_ROOT / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


def test_the_corpus_patches_names_through_run() -> None:
    # Non-vacuity: the scan finds the seams the run module is known to serve.
    assert {"_QUEUE_DEPTH_RETRY_SECS", "permission_pre_tool_block"} <= _corpus_patched_names()


def test_no_name_patched_through_run_is_read_by_an_owner() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _owner_reads()) == []


def test_the_reach_scan_can_fail(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from kiro_crew.subagent_manager import run as run_mod\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(run_mod, '_logging', None)\n"
        "def test_y():\n"
        "    from unittest import mock\n"
        "    with mock.patch('kiro_crew.subagent_manager.run.ResolvedAwsProfile'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    patched = _patched_through_run([probe])
    assert patched == {"_logging", "ResolvedAwsProfile"}
    assert _reach_gaps(patched, _owner_reads()) == [
        f"{_PACKAGE}.run_start.ResolvedAwsProfile",
        f"{_PACKAGE}.run_yields._logging",
    ]


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    monkeypatch.syspath_prepend(str(_REPO_ROOT / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    rel = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    assert rel in leaf_test_scope.corpus_gates(_REPO_ROOT)
