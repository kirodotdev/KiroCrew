"""Dashboard fire-and-forget tasks are held until they finish.

The event loop keeps only a weak reference to a task. A task parked on a future
that nothing else references forms an unreachable cycle with it, so a
``gc.collect()`` destroys the task mid-run ("Task was destroyed but it is
pending!") unless something holds it.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import gc
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import weixin_qr

REPO = Path(__file__).resolve().parent.parent

#: Files that start fire-and-forget tasks. The guard below keeps a dropped
#: ``create_task(...)`` out of them.
GUARDED = (
    "src/kiro_crew/dashboard/server.py",
    "src/kiro_crew/dashboard/state_parts/dashboard_services.py",
    "src/kiro_crew/dashboard/handlers/weixin_qr.py",
    "src/kiro_crew/dashboard/handlers/crew_log.py",
    "src/kiro_crew/knowledge/artifact_ingest.py",
    "src/kiro_crew/remote_session/worker.py",
    "src/kiro_crew/shared_crew/admission_entry.py",
    "src/kiro_crew/sprites_session/terminal.py",
    "src/kiro_crew/subagent_manager/admission/gate.py",
    "src/kiro_crew/subagent_manager/terminal.py",
)

_TASK_STARTERS = ("create_task", "ensure_future")
#: Task methods that do not keep the task alive past the statement that calls them.
_NON_HOLDING_USES = ("add_done_callback", "cancelled", "done", "exception", "result")


def _starts_task(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    return name in _TASK_STARTERS


def _own_scope(fn: ast.AST) -> list[ast.AST]:
    """The nodes of *fn*'s body, not descending into nested functions or lambdas."""
    nodes: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(fn))
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return nodes


def _locally_dropped(fn: ast.FunctionDef) -> list[int]:
    """Tasks bound to a local name whose only uses are callbacks and state reads.

    Only a sync function's local is checked: an async frame stays alive while it
    awaits, so a local there holds the task for as long as the frame does. Uses
    are read from nested functions too, since a closure can hold the task.
    """
    parents = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
    scope = _own_scope(fn)
    outer = {name for n in scope if isinstance(n, (ast.Global, ast.Nonlocal)) for name in n.names}
    lines = []
    for node in scope:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id not in outer
            and _starts_task(node.value)
        ):
            continue
        name = node.targets[0].id
        uses = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)
        ]
        if all(
            isinstance(parents.get(use), ast.Attribute)
            and parents[use].attr in _NON_HOLDING_USES  # type: ignore[attr-defined]
            for use in uses
        ):
            lines.append(node.lineno)
    return lines


def _dropped_task_sites(source: str) -> list[int]:
    """Lines that start a task with ``create_task`` / ``ensure_future`` and drop it.

    Three shapes: a bare statement, a lambda that returns the task to a caller
    that discards it (a signal handler), and a sync function's local that is
    only given callbacks before the function returns.
    """
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Expr) and _starts_task(node.value):
            lines.append(node.lineno)
        elif isinstance(node, ast.Lambda) and _starts_task(node.body):
            lines.append(node.lineno)
        elif isinstance(node, ast.FunctionDef):
            lines.extend(_locally_dropped(node))
    return sorted(lines)


@pytest.mark.parametrize("rel", GUARDED)
def test_no_dropped_task_statement(rel: str) -> None:
    lines = _dropped_task_sites((REPO / rel).read_text(encoding="utf-8"))
    assert lines == [], f"{rel} drops a task at line(s) {lines}; hold it in a set until done"


def test_the_guard_sees_a_dropped_task() -> None:
    assert _dropped_task_sites("import asyncio\nasyncio.create_task(f())\n") == [2]
    assert _dropped_task_sites("t = asyncio.create_task(f())\n") == []


def test_the_guard_sees_a_task_dropped_by_a_lambda() -> None:
    source = "loop.add_signal_handler(15, lambda: asyncio.ensure_future(f()))\n"
    assert _dropped_task_sites(source) == [1]


def test_the_guard_sees_a_local_task_given_only_a_callback() -> None:
    dropped = "def g():\n    t = loop.create_task(f())\n    t.add_done_callback(cb)\n"
    held = dropped + "    TASKS.add(t)\n"
    stored = "def g():\n    global T\n    T = loop.create_task(f())\n"
    awaited = "async def g():\n    t = asyncio.create_task(f())\n    t.add_done_callback(cb)\n"
    assert _dropped_task_sites(dropped) == [2]
    assert _dropped_task_sites(held) == []
    assert _dropped_task_sites(stored) == []
    assert _dropped_task_sites(awaited) == []


@pytest.mark.asyncio
async def test_expired_qr_client_close_survives_a_gc_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ParkedClient:
        async def close(self) -> None:
            await asyncio.get_running_loop().create_future()

    # created_at 0 is decades past any TTL, so the prune expires it without a clock patch.
    monkeypatch.setattr(
        weixin_qr, "_SESSIONS", {"sid": {"client": _ParkedClient(), "created_at": 0}}
    )

    weixin_qr._prune_sessions()
    assert weixin_qr._SESSIONS == {}
    await asyncio.sleep(0)  # let the close task start and park on its future
    gc.collect()

    parked = [
        t for t in asyncio.all_tasks() if t.get_coro().__qualname__.endswith("_ParkedClient.close")
    ]
    assert len(parked) == 1, "the close task was garbage-collected mid-run"
    assert parked[0] in weixin_qr._CLOSE_TASKS
    parked[0].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await parked[0]
    assert parked[0] not in weixin_qr._CLOSE_TASKS
