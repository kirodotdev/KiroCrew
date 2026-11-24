"""A sub-agent run records which session started it as its execution begins.

The KAS harness reads that record when it builds the run's session, to hold the run
to its parent agent's denies (``acp.kas_parent_limits``). The run path is the real
``SubagentManager`` one, with only the ACP session provider faked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from subagent_stop_reason_helpers import (
    _await_run,
    _manager,
    _mock_sessions,
    _single_turn,
    _wait_writers_landed,
)

from kiro_crew.acp import kas_parent_limits
from kiro_crew.acp.types import STOP_REASON_END_TURN

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")


async def _run(parent: str):
    stream_factory, _calls = _single_turn(STOP_REASON_END_TURN)
    manager = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("do work", parent_session_key=parent)
        assert info is not None
        await _await_run(manager, info)
    await _wait_writers_landed(manager, info)
    return info


@pytest.mark.asyncio
async def test_a_run_notes_the_session_that_started_it():
    info = await _run("dashboard:parent")
    assert kas_parent_limits._NOTED[f"subagent:{info.id}"] == ("dashboard:parent",)


@pytest.mark.asyncio
async def test_a_run_with_no_parent_notes_nothing():
    info = await _run("")
    assert f"subagent:{info.id}" not in kas_parent_limits._NOTED


def test_a_run_key_keeps_every_parent_up_to_the_cap_and_refuses_one_more(monkeypatch):
    monkeypatch.setattr(kas_parent_limits, "_NOTED", type(kas_parent_limits._NOTED)())
    monkeypatch.setattr(kas_parent_limits, "_NOTED_DIRS", type(kas_parent_limits._NOTED_DIRS)())
    cap = kas_parent_limits.MAX_PARENTS_PER_RUN
    parents = [f"dashboard:p{n}" for n in range(cap)]
    for parent in parents:
        kas_parent_limits.note_parent("subagent:r", parent, f"/dir/{parent}")
    before = (dict(kas_parent_limits._NOTED), dict(kas_parent_limits._NOTED_DIRS))

    with pytest.raises(kas_parent_limits.TooManyParents):
        kas_parent_limits.note_parent("subagent:r", "dashboard:one-more", "/dir/one-more")

    # Nothing recorded is dropped and the refused call changed nothing.
    assert (dict(kas_parent_limits._NOTED), dict(kas_parent_limits._NOTED_DIRS)) == before
    assert kas_parent_limits._NOTED["subagent:r"] == tuple(parents)
    # A parent already recorded is accepted as before.
    kas_parent_limits.note_parent("subagent:r", parents[0])
    assert set(kas_parent_limits._NOTED["subagent:r"]) == set(parents)


@pytest.mark.asyncio
async def test_a_run_its_key_cannot_take_another_parent_fails_before_its_session_starts(
    monkeypatch,
):
    monkeypatch.setattr(kas_parent_limits, "MAX_PARENTS_PER_RUN", 0)
    stream_factory, calls = _single_turn(STOP_REASON_END_TURN)
    manager = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("do work", parent_session_key="dashboard:parent")
        assert info is not None
        await _await_run(manager, info)
    await _wait_writers_landed(manager, info)

    assert info.done and "already continued from 0 different sessions" in (info.error or "")
    assert calls == []
    assert f"subagent:{info.id}" not in kas_parent_limits._NOTED
