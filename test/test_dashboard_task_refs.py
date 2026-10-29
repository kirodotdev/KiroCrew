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

#: The dashboard files whose dropped tasks this change fixed. The guard below
#: keeps a bare ``create_task(...)`` statement from coming back in them.
GUARDED = (
    "src/kiro_crew/dashboard/server.py",
    "src/kiro_crew/dashboard/state_parts/dashboard_services.py",
    "src/kiro_crew/dashboard/handlers/weixin_qr.py",
)


def _dropped_task_sites(source: str) -> list[int]:
    """Lines of statements that call ``create_task`` / ``ensure_future`` and drop the result."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("create_task", "ensure_future"):
                lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("rel", GUARDED)
def test_no_dropped_task_statement(rel: str) -> None:
    lines = _dropped_task_sites((REPO / rel).read_text(encoding="utf-8"))
    assert lines == [], f"{rel} drops a task at line(s) {lines}; hold it in a set until done"


def test_the_guard_sees_a_dropped_task() -> None:
    assert _dropped_task_sites("import asyncio\nasyncio.create_task(f())\n") == [2]
    assert _dropped_task_sites("t = asyncio.create_task(f())\n") == []


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
