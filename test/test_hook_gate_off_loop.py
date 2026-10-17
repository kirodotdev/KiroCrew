"""The tool gate's path tier must not park the event loop.

``HookManager.on_tool_call`` reaches ``security.sensitive_path_refusal``, which
resolves an agent-supplied path in the ``mc-pathres`` child and waits for the
answer on the CALLING thread, up to the resolve budget plus its grace. Live
py-spy dumps caught the gateway's main thread in that wait, called inline from a
coroutine, so one slow resolution stalled every session on the loop.

These tests pin the fix: the async callers await ``executors.run_in_tool_gate_pool(hooks.on_tool_call, ...)``
(the channel TurnDriver runs its sync gate callable the same way), the
loop keeps ticking while a resolver is wedged, and the verdict -- fail-closed on
a stall included -- is the synchronous gate's, word for word.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew
import kiro_crew.executors as ex
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, AcpEvent
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.messaging import APPROVAL_AUTO, TransportCapabilities, TurnDriver
from kiro_crew.messaging.dispatch import build_tool_gate
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.security import is_unverifiable_path_refusal, paths
from kiro_crew.subprocess_pool import SubprocessPoolTimeout

_BUDGET = 0.4
_TICK = 0.01
_TARGET = "/home/someone/ws/README.md"


@pytest.fixture(autouse=True)
def _stalled_resolver(_floor_monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every candidate resolution wedges like a child on a dead mount."""

    def stalled(expanded: str) -> set[str]:
        deadline = getattr(paths._child_budget, "deadline", None)
        if deadline is not None:
            time.sleep(max(0.0, deadline - time.monotonic()))
        raise SubprocessPoolTimeout("stub child did not answer", child_syscall=b"6")

    _floor_monkeypatch.setattr(paths, "_path_resolve_degraded", {})
    _floor_monkeypatch.setattr(paths, "_path_resolve_thread_waits", {})
    _floor_monkeypatch.setattr(paths, "_PATH_RESOLVE_TIMEOUT_SECS", _BUDGET)
    _floor_monkeypatch.setattr(paths, "_child_blocked_in_filesystem", lambda sampled: True)
    _floor_monkeypatch.setattr(paths, "_resolved_spellings", stalled)
    yield
    ex.shutdown_maintenance_executor()


def _busy(waited: float):
    raise AssertionError(f"the gate pool refused a call after {waited:.1f}s in its queue")


def _read_kwargs() -> dict:
    return {
        "session_key": "dashboard:f6",
        "agent": "kirocrew",
        "tool_kind": "read",
        "raw_params": {"path": _TARGET},
    }


async def _ticking(coro):
    """Await *coro* while a 10 ms ticker runs; return ``(result, ticks)``."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(_TICK)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)  # let the ticker start before the gate runs
    try:
        result = await coro
    finally:
        stop.set()
        await task
    return result, ticks


def _min_ticks(elapsed: float) -> int:
    # Generous slack for a loaded runner: a third of the ticks the wait allows.
    return max(5, int(elapsed / _TICK / 3))


def test_the_awaitable_gate_keeps_the_loop_ticking_through_a_stalled_resolver() -> None:
    mgr = HookManager(HooksConfig())

    async def main():
        start = time.monotonic()
        result, ticks = await _ticking(
            ex.run_in_tool_gate_pool(
                mgr.on_tool_call, f"Reading {_TARGET}", on_queue_timeout=_busy, **_read_kwargs()
            )
        )
        return result, ticks, time.monotonic() - start

    result, ticks, elapsed = asyncio.run(main())
    # Fail-closed, worded as unverifiable: exactly the sync gate's stall answer.
    assert result.action == TOOL_DENY
    assert is_unverifiable_path_refusal(result.reason)
    assert elapsed >= _BUDGET * 0.9, "the stub did not stall; the test proves nothing"
    assert ticks >= _min_ticks(elapsed), f"loop ticked {ticks}x in {elapsed:.2f}s"


def test_the_awaitable_gate_returns_the_sync_verdict_word_for_word(tmp_path) -> None:
    mgr = HookManager(HooksConfig())
    title = f"Reading {_TARGET}"
    sync = mgr.on_tool_call(title, **_read_kwargs())
    paths._path_resolve_degraded.clear()
    paths._path_resolve_thread_waits.clear()
    off = asyncio.run(
        ex.run_in_tool_gate_pool(mgr.on_tool_call, title, on_queue_timeout=_busy, **_read_kwargs())
    )
    assert (off.action, off.reason) == (sync.action, sync.reason)
    assert off.action == TOOL_DENY


def test_the_gate_runs_on_its_own_pool_not_the_default_executor() -> None:
    """A stalled gate call holds its worker for the whole resolve budget, so it
    must queue on ``mc-toolgate`` and never take a default-executor thread the
    loop needs for DNS. A default executor that refuses every submit proves it,
    and both async gate forms still answer."""

    class _RefusingExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            raise AssertionError("the tool gate reached the loop's default executor")

    mgr = HookManager(HooksConfig())
    seen: list[str] = []
    real = mgr.on_tool_call

    def recording(title, **kwargs):
        seen.append(threading.current_thread().name)
        return real(title, **kwargs)

    mgr.on_tool_call = recording  # type: ignore[method-assign]

    def channel_gate(event):
        seen.append(threading.current_thread().name)
        return "allow"

    async def main():
        refusing = _RefusingExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(refusing)
        try:
            result = await ex.run_in_tool_gate_pool(
                mgr.on_tool_call, f"Reading {_TARGET}", on_queue_timeout=_busy, **_read_kwargs()
            )
            verdict = await ex.run_in_tool_gate_pool(channel_gate, object(), on_queue_timeout=_busy)
        finally:
            refusing.shutdown(wait=False)
        return result, verdict

    result, verdict = asyncio.run(main())
    assert result.action == TOOL_DENY and verdict == "allow"
    assert len(seen) == 2
    assert all(name.startswith("mc-toolgate") for name in seen), seen
    assert ex.tool_gate_executor()._max_workers == ex._MAX_TOOL_GATE_WORKERS


def test_the_channel_turn_driver_runs_its_gate_off_the_loop() -> None:
    """The TurnDriver behind Slack, Discord, Telegram and the other channels."""

    class _Renderer(Renderer):
        def __init__(self) -> None:
            super().__init__(TransportCapabilities())

        async def on_text_chunk(self, text):
            pass

        async def on_thinking(self, text):
            pass

        async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
            pass

        async def on_prompt_choice(
            self, options, request_id, tool_title="", tool_purpose="", tool_input=""
        ):
            pass

        async def on_compaction(self, pct):
            pass

        async def on_done(self, stop_reason=""):
            pass

    class _Provider:
        def __init__(self) -> None:
            self.approved: list = []
            self.rejected: list = []

        async def stream(self, message):
            yield AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="rq1",
                title=f"Reading {_TARGET}",
                tool_kind="read",
                raw_tool_params={"path": _TARGET},
                options=[{"id": "approve"}],
            )
            yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        async def approve_tool(self, request_id, *, always=False):
            self.approved.append(request_id)

        async def reject_tool(self, request_id):
            self.rejected.append(request_id)

    ctx_builder = SimpleNamespace(hooks=HookManager(HooksConfig()))
    gate = build_tool_gate(ctx_builder, session_key="slack:f6", agent="kirocrew")
    provider = _Provider()
    driver = TurnDriver(provider, _Renderer(), approval_mode=APPROVAL_AUTO, tool_gate=gate)

    async def main():
        start = time.monotonic()
        _, ticks = await _ticking(driver.run("hello"))
        return ticks, time.monotonic() - start

    ticks, elapsed = asyncio.run(main())
    # APPROVAL_AUTO would approve; the gate's fail-closed stall must reject first.
    assert provider.rejected == ["rq1"] and provider.approved == []
    assert is_unverifiable_path_refusal(gate.last_deny_reason)
    assert elapsed >= _BUDGET * 0.9, "the stub did not stall; the test proves nothing"
    assert ticks >= _min_ticks(elapsed), f"loop ticked {ticks}x in {elapsed:.2f}s"


def test_no_coroutine_consults_the_gate_inline() -> None:
    """STRUCTURAL: a ``HookManager.on_tool_call`` call written directly inside an
    ``async def`` runs the path tier's wait on the loop. Async code awaits
    ``run_in_tool_gate_pool(hooks.on_tool_call, ...)`` instead. An awaited ``...on_tool_call(`` is a
    renderer's unrelated handler, not the gate, and is not an offender."""
    root = Path(kiro_crew.__file__).resolve().parent
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if "/tests/" in f"/{rel}":
            continue
        text = path.read_text(encoding="utf-8")
        if ".on_tool_call(" not in text:
            continue
        tree = ast.parse(text)
        awaited = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Await)}

        def visit(node: ast.AST, in_async: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.AsyncFunctionDef):
                    visit(child, True)
                elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                    visit(child, False)
                else:
                    if (
                        in_async
                        and isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "on_tool_call"
                        and id(child) not in awaited
                    ):
                        offenders.append(f"{rel}:{child.lineno}")
                    visit(child, in_async)

        visit(tree, False)
    assert not offenders, "inline gate calls in async code:\n" + "\n".join(offenders)
