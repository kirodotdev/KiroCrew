"""Shared pieces for the ``test_subagent_scale*.py`` modules.

Mocks a ``SubagentManager`` is built over, and the gateway orchestrator the wave-digest cases
drive. ``test_subagent_delivery_ttl_anchor.py`` and ``test_subagent_queue_max_wait.py`` import
some of these through ``test_subagent_scale.py``. The autouse ``_close_subagent_managers``
closes every manager a test builds; ``_ANNOUNCE_WAIT_SECS`` bounds an announce-task wait.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ceiling for a wait on an announce task that only the test ends (a release, a
# cancel or ``cancel_all``). It is far above what the in-memory work needs, so a
# regression that keeps an announce running fails at that wait by name instead
# of hanging the worker.
_ANNOUNCE_WAIT_SECS = 60.0


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx


def _make_orchestrator():
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.slack.gateway import GatewayOrchestrator

    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=False, no_crons=True, no_open=True)


def _mock_dashboard_state():
    ds = MagicMock()
    ds._slots = {}
    ds._yolo = False
    ds.notify = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.push_refresh = MagicMock()
    ds.broadcast_ws = MagicMock()
    ds.broadcast_ws_subagent_subscribers = MagicMock()
    ds.request_approval = AsyncMock(return_value=True)
    ds.resolve_approval = MagicMock()
    ds.resolve_slot = MagicMock(return_value=None)
    ds.get_slot = MagicMock(return_value=None)
    ds.get_or_create_slot = MagicMock()
    ds.close_all_ws = AsyncMock()
    ds._background_tasks = set()
    return ds


async def _drain_injections(orch) -> None:
    """Run every injection turn the orchestrator has spawned to completion.

    A sibling announce task left parked in ``bounded_chat_turn``'s off-loop hop would have
    the loop close under it and report ``_run_chat`` as never awaited.
    """
    pending = [t for t in orch.dashboard_state._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
