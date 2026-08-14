"""Both-locks discipline for Slack ``config.json`` writers (#3102 review).

``run_config_write`` exists because two writer generations serialize on two
DIFFERENT locks: ``update_config_locked`` takes the sidecar advisory flock,
while the dashboard's legacy handlers (e.g. the memory-settings PUT) do a bare
read-modify-write under only the loop-side ``_get_config_lock()``. A Slack
writer holding only the flock can interleave with the legacy family and
silently revert its settings. The helper is the single async entry point that
holds both: asyncio lock on the loop, blocking writer in a worker thread.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from kiro_crew.dashboard.chat_utils import run_config_write

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

#: The sync config-writing callables Slack's async handlers invoke. Every
#: invocation from async Slack code must go through run_config_write.
_CONFIG_WRITERS = (
    "update_config_locked",
    "_set_default_agent",
    "_persist_channel_config",
    "persist_allowed_user",
    "persist_tracking_channel",
)

_SLACK_SOURCES = ("interactions.py", "handler.py", "events.py", "allowlist.py")


class TestRunConfigWrite:
    @pytest.mark.asyncio
    async def test_returns_the_callables_result(self) -> None:
        assert await run_config_write(lambda a, b=0: a + b, 40, b=2) == 42

    @pytest.mark.asyncio
    async def test_holds_the_loop_side_config_lock_while_fn_runs(self) -> None:
        """While the dashboard lock is held, the write must not start."""
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        ran = asyncio.Event()

        async def _write() -> None:
            await run_config_write(lambda: ran.set())

        lock = _get_config_lock()
        async with lock:
            task = asyncio.create_task(_write())
            await asyncio.sleep(0.05)
            assert not ran.is_set(), "write started while a legacy writer held the lock"
        await asyncio.wait_for(task, timeout=5)
        assert ran.is_set()

    @pytest.mark.asyncio
    async def test_runs_fn_off_the_event_loop(self) -> None:
        """The blocking flock wait must land in a worker thread, not the loop."""
        import threading

        loop_thread = threading.current_thread()
        fn_thread: list[threading.Thread] = []
        await run_config_write(lambda: fn_thread.append(threading.current_thread()))
        assert fn_thread and fn_thread[0] is not loop_thread


class TestSlackWritersUseBothLocks:
    """Structural lock-ins: no Slack async caller bypasses the helper."""

    def test_no_slack_config_write_is_offloaded_without_the_loop_lock(self) -> None:
        # `asyncio.to_thread(<config writer>` holds only the flock — the exact
        # single-lock bypass this round's review blocked on.
        pattern = re.compile(
            r"asyncio\.to_thread\(\s*(?:" + "|".join(_CONFIG_WRITERS) + r")\b"
        )
        offenders = [
            f"{name}: {m.group(0)!r}"
            for name in _SLACK_SOURCES
            for m in pattern.finditer((SRC / "slack" / name).read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"config writers offloaded without _get_config_lock: {offenders}"

    def test_no_slack_async_function_calls_a_config_writer_inline(self) -> None:
        """Inside `async def`, a bare (un-awaited-wrapper) writer call blocks the
        loop on the flock. Definitions and run_config_write args don't match."""
        import ast

        offenders: list[str] = []
        for name in _SLACK_SOURCES:
            tree = ast.parse((SRC / "slack" / name).read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    callee = node.func
                    if isinstance(callee, ast.Name) and callee.id in _CONFIG_WRITERS:
                        offenders.append(f"{name}:{node.lineno} {fn.name} -> {callee.id}")
        assert offenders == [], f"inline config-writer calls on the event loop: {offenders}"
