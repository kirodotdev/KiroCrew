"""A finished subagent of a remote session drops its inherited environment row.

Every remote subagent writes a ``session-choices.json`` row naming its parent's
workspace; without a drop at the end of the run the file grows by one row per
subagent and is re-parsed on every session start.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.coder import session_launch
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until


def _make_manager() -> SubagentManager:
    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=4)
    mgr._fire_event = AsyncMock()
    mgr._write_tombstone = MagicMock()
    mgr._record_cost = MagicMock()
    mgr._on_done = AsyncMock()
    return mgr


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [True, False])
async def test_run_end_drops_the_inherited_row_only_for_a_remote_child(monkeypatch, remote):
    mgr = _make_manager()
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    info._session_sharing = False
    mgr._running_count = 1
    forgotten: list[str] = []

    async def forget(child_key: str) -> None:
        forgotten.append(child_key)

    monkeypatch.setattr(session_launch, "forget_subagent_environment", forget)
    seen_keys: list[str] = []

    async def run_inner(run, session_key):
        seen_keys.append(session_key)
        run._remote_workspace = remote
        run.result = "done"

    async def teardown(_info, _key):
        await asyncio.sleep(0)

    mgr._run_inner = run_inner  # type: ignore[assignment]
    mgr._teardown_run_session = teardown  # type: ignore[assignment]
    await await_until(asyncio.ensure_future(mgr._run(info)), what="remote child cleanup")
    assert forgotten == (seen_keys if remote else [])
    assert mgr._running_count == 0
