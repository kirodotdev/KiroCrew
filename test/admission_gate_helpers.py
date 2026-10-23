"""Helpers shared by ``test_admission_gate.py`` and ``test_admission_gate_pressure_hold.py``.

``_TestSpawnAdmissionGateHarness`` holds the three helper methods ``TestSpawnAdmissionGate``
and ``TestSpawnAdmissionGatePressureHold`` both reach: ``_mgr``, ``_queued_sink`` and
``_spawn_capturing_queued``. The autouse ``_close_managers`` closes each manager a test
builds at teardown; ``_wait_for`` polls a predicate and ``_await_queued_event`` waits for a
``subagent_queued`` emit, each raising at its deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.testing.wait import async_wait_until


@pytest.fixture(autouse=True)
def _close_managers(close_subagent_managers) -> None:
    """Every manager built here opens tasks.db; close it at teardown, not at GC."""


#: Ceiling on waiting for a ``subagent_queued`` emit. The emit follows one
#: writer-thread round trip, measured in milliseconds; this bounds a lost run on
#: a loaded shard and is never a pass condition.
_QUEUED_EMIT_CEILING_SECS = 5.0


async def _await_queued_event(events: list[dict[str, Any]], reason: str | None = None) -> None:
    """Wait for a ``subagent_queued`` extra (one carrying *reason*, when given);
    RAISE, naming what was read, at the ceiling."""
    await async_wait_until(
        lambda: any(reason is None or e.get("reason") == reason for e in events),
        timeout=_QUEUED_EMIT_CEILING_SECS,
        describe=lambda: (
            f"no subagent_queued{f' with reason {reason!r}' if reason else ''}; events={events!r}"
        ),
    )


async def _wait_for(predicate, timeout=5.0, interval=0.05, message="predicate"):
    """Poll until predicate is true or timeout; the failure names *message*."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"Timed out after {timeout}s waiting for {message}")
        # flake-ok: pre-existing bounded poll interval, moved verbatim; it raises at its deadline
        await asyncio.sleep(interval)


class _TestSpawnAdmissionGateHarness:
    """Helper methods ``TestSpawnAdmissionGate`` and ``TestSpawnAdmissionGatePressureHold`` share.

    The leading underscore keeps it out of pytest's ``Test*`` class collection. ``_mgr`` builds
    a ``SubagentManager`` over mocked sessions; ``_spawn_capturing_queued`` runs a spawn on a
    live loop and returns the info, every ``subagent_queued`` extra ``_queued_sink`` gathers,
    and the SEL mock.
    """

    def _mgr(self):
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_agent_selection.return_value = ("template", "")
        # reset is awaited by _teardown_run_session_impl; give it an AsyncMock
        # so any admitted task that reaches teardown does not crash with a
        # TypeError from trying to await a plain MagicMock.
        sessions.reset = AsyncMock()
        return SubagentManager(
            sessions=sessions,
            ctx_builder=MagicMock(),
            on_done=MagicMock(),
            max_concurrent=3,
        )

    @staticmethod
    def _queued_sink() -> tuple[list[dict[str, Any]], Any]:
        """``(events, on_event)``: every ``subagent_queued`` extra *on_event* sees."""
        events: list[dict[str, Any]] = []

        async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
            if etype == "subagent_queued":
                events.append(dict(extra))

        return events, on_event

    def _spawn_capturing_queued(
        self,
        mgr,
        *,
        memory: tuple[bool, float] | None = None,
        memory_mode: str | None = None,
        floor_gb: float = 4.0,
        parent_session_key: str = "sess-1",
        **spawn_kwargs: Any,
    ) -> tuple[Any, list[dict[str, Any]], MagicMock]:
        """Run ``spawn`` on a live loop; return the info, every ``subagent_queued``
        extra, and the SEL mock. A refused or started spawn is not waited on.

        *memory* left at None runs the real floor reader (a test that wants the
        real reader fakes what is under it)."""
        events, on_event = self._queued_sink()

        async def run() -> tuple[Any, MagicMock]:
            mgr._on_event = on_event
            with contextlib.ExitStack() as stack:
                if memory is not None:
                    stack.enter_context(
                        patch("kiro_crew.subagent.check_memory_available", return_value=memory)
                    )
                mock_cfg = stack.enter_context(patch("kiro_crew.subagent.KiroCrewConfig"))
                mock_sel = stack.enter_context(patch("kiro_crew.subagent.sel"))
                mock_cfg.load.return_value.agent.spawn_min_memory_gb = floor_gb
                mock_cfg.load.return_value.agent.subagent_cost_gb = 0.5
                mock_sel.return_value.log_tool_invocation = MagicMock()
                info = mgr.spawn(
                    task="test task",
                    parent_session_key=parent_session_key,
                    _memory_mode=memory_mode,
                    **spawn_kwargs,
                )
            if info is not None and info.queued and not info.done:
                await _await_queued_event(events)
            return info, mock_sel

        info, mock_sel = asyncio.run(run())
        return info, events, mock_sel
