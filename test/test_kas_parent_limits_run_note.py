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


def test_a_run_key_keeps_every_parent_up_to_the_cap_and_marks_one_more(monkeypatch):
    monkeypatch.setattr(kas_parent_limits, "_NOTED", type(kas_parent_limits._NOTED)())
    monkeypatch.setattr(kas_parent_limits, "_NOTED_DIRS", type(kas_parent_limits._NOTED_DIRS)())
    monkeypatch.setattr(kas_parent_limits, "_INCOMPLETE", type(kas_parent_limits._INCOMPLETE)())
    cap = kas_parent_limits.MAX_PARENTS_PER_RUN
    parents = [f"dashboard:p{n}" for n in range(cap)]
    for parent in parents:
        kas_parent_limits.note_parent("subagent:r", parent, f"/dir/{parent}")
    before = (dict(kas_parent_limits._NOTED), dict(kas_parent_limits._NOTED_DIRS))
    assert "subagent:r" not in kas_parent_limits._INCOMPLETE

    # One more never raises and is not recorded; the key is marked instead.
    kas_parent_limits.note_parent("subagent:r", "dashboard:one-more", "/dir/one-more")

    assert (dict(kas_parent_limits._NOTED), dict(kas_parent_limits._NOTED_DIRS)) == before
    assert kas_parent_limits._NOTED["subagent:r"] == tuple(parents)
    assert "subagent:r" in kas_parent_limits._INCOMPLETE
    # A parent already recorded is accepted as before.
    kas_parent_limits.note_parent("subagent:r", parents[0])
    assert set(kas_parent_limits._NOTED["subagent:r"]) == set(parents)


def test_a_marked_key_keeps_its_mark_while_its_parents_are_kept(monkeypatch):
    monkeypatch.setattr(kas_parent_limits, "_NOTED", type(kas_parent_limits._NOTED)())
    monkeypatch.setattr(kas_parent_limits, "_NOTED_DIRS", type(kas_parent_limits._NOTED_DIRS)())
    monkeypatch.setattr(kas_parent_limits, "_INCOMPLETE", type(kas_parent_limits._INCOMPLETE)())
    monkeypatch.setattr(kas_parent_limits, "MAX_PARENTS_PER_RUN", 1)
    monkeypatch.setattr(kas_parent_limits, "_MAX_NOTED", 2)
    kas_parent_limits.note_parent("subagent:r", "dashboard:first")
    kas_parent_limits.note_parent("subagent:r", "dashboard:omitted", "/dir/omitted")
    assert "dashboard:omitted" not in kas_parent_limits._NOTED_DIRS
    # Other keys fill the notes while this key is used again from its noted parent:
    # it stays the newest row, and its mark stays with it.
    for n in range(3):
        kas_parent_limits.note_parent(f"subagent:other{n}", "dashboard:x")
        kas_parent_limits.note_parent("subagent:r", "dashboard:first")
    assert kas_parent_limits._NOTED["subagent:r"] == ("dashboard:first",)
    assert "subagent:r" in kas_parent_limits._INCOMPLETE
    # The mark leaves only with the row, so the marks never outnumber the notes.
    for n in range(3):
        kas_parent_limits.note_parent(f"subagent:more{n}", "dashboard:x")
    assert "subagent:r" not in kas_parent_limits._NOTED
    assert kas_parent_limits._INCOMPLETE <= set(kas_parent_limits._NOTED)


@pytest.mark.asyncio
async def test_a_run_past_its_keys_cap_still_runs_off_kas(monkeypatch):
    # The manager's own path with a non-KAS session provider, as on Kiro CLI.
    monkeypatch.setattr(kas_parent_limits, "MAX_PARENTS_PER_RUN", 0)
    stream_factory, calls = _single_turn(STOP_REASON_END_TURN)
    manager = _manager(_mock_sessions(stream_factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("do work", parent_session_key="dashboard:parent")
        assert info is not None
        await _await_run(manager, info)
    await _wait_writers_landed(manager, info)

    assert info.done and not info.error
    assert calls != []
    assert kas_parent_limits._NOTED.get(f"subagent:{info.id}") == ()
    assert f"subagent:{info.id}" in kas_parent_limits._INCOMPLETE


def _chain(monkeypatch):
    """A run ``subagent:c`` started from the run ``subagent:r``, with no records."""
    monkeypatch.setattr(kas_parent_limits, "_NOTED", type(kas_parent_limits._NOTED)())
    monkeypatch.setattr(kas_parent_limits, "_INCOMPLETE", type(kas_parent_limits._INCOMPLETE)())
    monkeypatch.setattr("kiro_crew.subagent._parent_template_for_spawn", lambda key: "worker")
    kas_parent_limits.note_parent("subagent:r", "dashboard:top")
    kas_parent_limits.note_parent("subagent:c", "subagent:r")


def test_a_kas_run_below_a_marked_key_is_refused(monkeypatch):
    _chain(monkeypatch)
    monkeypatch.setattr(kas_parent_limits, "MAX_PARENTS_PER_RUN", 1)
    kas_parent_limits.note_parent("subagent:r", "dashboard:second")

    for key in ("subagent:r", "subagent:c"):
        with pytest.raises(kas_parent_limits.TooManyParents, match="already continued from 1"):
            kas_parent_limits.holding_agents(key)
    # The refusal is a ParentLimitsUnreadable: the session is refused, never started
    # without its parents, and the warm pool never serves it.
    assert issubclass(kas_parent_limits.TooManyParents, kas_parent_limits.ParentLimitsUnreadable)
    assert kas_parent_limits.run_is_held("subagent:c", "worker") is True


def test_a_kas_chain_with_no_marked_key_is_read_as_before(monkeypatch):
    _chain(monkeypatch)
    assert kas_parent_limits.holding_agents("subagent:c") == [
        ("worker", "subagent:r"),
        ("worker", "dashboard:top"),
    ]
