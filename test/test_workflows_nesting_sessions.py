"""Workflow nesting caps through dashboard sessions a run opens.

A run's ``ctx.open_session`` makes an ordinary dashboard session, and a session
that works for a run can open more with ``session_create``. A run started from
any of them counts as a child of the run, so a chain of opened sessions cannot
restart the depth count.
"""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.dashboard.handlers.session_control import _inherit_workflow_lineage
from kiro_crew.dashboard.workflow_lineage import workflow_run_of_session
from kiro_crew.dashboard.workflow_session_port import WorkflowSessionPort
from kiro_crew.history import ConversationLog
from kiro_crew.workflows.nesting import ERROR_CODE_DEPTH
from kiro_crew.workflows.service import WorkflowService

QUICK = (
    'META = {"name": "quick", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    return {'ok': True}\n"
)

# Turns a lost run into a failed test well under pytest-timeout; never a barrier.
_HANG_GUARD_SECS = 30.0


class _Sessions:
    async def get_or_create(self, key, **kw):
        return SimpleNamespace(), True, False

    def release(self, key, *, cleanup=False):
        pass

    async def destroy(self, key):
        pass


@pytest.fixture
def setup(tmp_path):
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    launcher = _slot(state, "chat-1")
    svc = WorkflowService(
        sessions=_Sessions(),
        persist=False,
        pool_agents=False,
        max_nesting_depth=1,
        run_of_session=functools.partial(workflow_run_of_session, state),
    )
    state.workflow_service = svc
    return state, launcher, svc, WorkflowSessionPort(state)


async def _settle(svc: WorkflowService, run_id: str) -> None:
    handle = svc.registry.get(run_id)
    assert handle is not None and handle.task is not None
    await asyncio.wait_for(asyncio.shield(handle.task), timeout=_HANG_GUARD_SECS)


async def _root_and_opened(svc, launcher, port) -> tuple[str, str]:
    root = await svc.start(QUICK, session_key=_key(launcher))
    assert "run_id" in root, root
    opened = await port.open_session(
        run_id=root["run_id"], memory_mode="persistent", caller_session_key=_key(launcher)
    )
    return root["run_id"], opened


def test_a_run_started_from_a_session_the_run_opened_is_its_child(setup):
    state, launcher, svc, port = setup

    async def scenario() -> None:
        root, opened = await _root_and_opened(svc, launcher, port)
        assert svc.nesting.opened_by(opened) == root
        out = await svc.start(QUICK, session_key=_key(state.get_slot(opened)))
        assert out.get("error_code") == ERROR_CODE_DEPTH, out
        await _settle(svc, root)

    asyncio.run(scenario())


def test_the_lookup_maps_an_opened_sessions_history_key_to_its_run(setup):
    state, launcher, svc, port = setup

    async def scenario() -> None:
        root, opened = await _root_and_opened(svc, launcher, port)
        assert workflow_run_of_session(state, _key(state.get_slot(opened))) == root
        assert workflow_run_of_session(state, _key(launcher)) is None
        await _settle(svc, root)

    asyncio.run(scenario())


def test_a_session_created_from_an_opened_session_inherits_its_run(setup):
    state, launcher, svc, port = setup

    async def scenario() -> None:
        root, opened = await _root_and_opened(svc, launcher, port)
        _inherit_workflow_lineage(state, _key(state.get_slot(opened)), "chat-created")
        assert svc.nesting.opened_by("chat-created") == root
        _inherit_workflow_lineage(state, _key(launcher), "chat-untraced")
        assert svc.nesting.opened_by("chat-untraced") == ""
        await _settle(svc, root)

    asyncio.run(scenario())
