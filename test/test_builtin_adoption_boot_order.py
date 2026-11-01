"""APPLY runs before any route is registered (matrix row 2), and STAGE after the listener."""

from __future__ import annotations

import ast
import asyncio
import inspect
from types import SimpleNamespace

import pytest

from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.dashboard import server
from kiro_crew.dashboard.server_runtime import app_platform

#: Boot steps that register a route, open an app's code or reconcile its resources.
_AFTER_APPLY = (
    "_register_mcp_routes",
    "register_all",
    "register_builtin_apps",
    "_warm_builtin_app_names",
    "_warm_materialized_agents",
    "_reconcile_app_resources",
    "_start_app_backends",
    "_kick_default_app_installs",
    "_kick_builtin_adoption_staging",
)


def _call_lines(function) -> dict[str, list[int]]:
    """``{callee name: [line, ...]}`` for every call (or bare reference) in *function*."""
    tree = ast.parse(inspect.getsource(function))
    lines: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            lines.setdefault(node.id, []).append(node.lineno)
        elif isinstance(node, ast.Attribute):
            lines.setdefault(node.attr, []).append(node.lineno)
    return lines


def test_apply_is_awaited_before_every_route_and_app_step():
    lines = _call_lines(server.start_dashboard)
    assert len(lines["_apply_builtin_adoptions"]) == 1
    apply_line = lines["_apply_builtin_adoptions"][0]
    for step in _AFTER_APPLY:
        assert step in lines, f"{step} is no longer in start_dashboard"
        assert min(lines[step]) > apply_line, f"{step} runs before APPLY"


def test_apply_is_awaited_not_kicked():
    tree = ast.parse(inspect.getsource(server.start_dashboard))
    awaited = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "_apply_builtin_adoptions"
    ]
    assert len(awaited) == 1


def test_staging_is_kicked_after_the_listener_serves():
    lines = _call_lines(server.start_dashboard)
    assert min(lines["_kick_builtin_adoption_staging"]) > min(lines["_kick_default_app_installs"])


@pytest.mark.asyncio
async def test_the_boot_steps_run_in_order_with_stubs(monkeypatch):
    order: list[str] = []

    async def fake_apply():
        order.append("apply")
        return {}

    monkeypatch.setattr(adoption, "apply_builtin_adoptions", fake_apply)
    await app_platform._apply_builtin_adoptions()
    order.append("routes")
    assert order == ["apply", "routes"]


@pytest.mark.asyncio
async def test_staging_is_not_kicked_when_nothing_is_pinned(monkeypatch, tmp_path):
    from builtin_adoption_helpers import install_pins

    install_pins(monkeypatch, tmp_path / "empty.json", [])
    state = SimpleNamespace(_background_tasks=set())
    app_platform._kick_builtin_adoption_staging(state)
    assert state._background_tasks == set()


@pytest.mark.asyncio
async def test_staging_is_a_tracked_background_task_when_pinned(monkeypatch, tmp_path):
    from builtin_adoption_helpers import install_pins, pin_row

    install_pins(monkeypatch, tmp_path / "pins.json", [pin_row("a" * 40, "b" * 64, "c" * 64)])
    started = asyncio.Event()

    async def fake_stage():
        started.set()

    monkeypatch.setattr(adoption, "stage_builtin_adoptions", fake_stage)
    state = SimpleNamespace(_background_tasks=set())
    app_platform._kick_builtin_adoption_staging(state)
    assert len(state._background_tasks) == 1
    await asyncio.wait_for(started.wait(), timeout=10)
    task = next(iter(state._background_tasks))
    await asyncio.wait_for(task, timeout=10)
    assert task.done() and task.exception() is None
    pins_mod.load_adoption_pins.cache_clear()
