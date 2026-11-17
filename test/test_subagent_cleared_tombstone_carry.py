"""A tombstone the shutdown drain clears keeps the run's ending for the next write.

``clear_tombstone`` re-admits a run to orphan recovery when its report was
cancelled at shutdown; the tombstone that recovery writes carries no outcome, so
it must still carry the stopped outcome and the leaked-process flag.
Every test keeps its state under tmp_path.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import subagent_persistence as sp
from kiro_crew import subagent_persistence_tombstones as st


@pytest.fixture(autouse=True)
def _pinned_paths(tmp_path, _floor_monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


def _run(agent_id: str):
    sp.create_agent_folder(agent_id, task="t", agent="kirocrew", parent_session="chat-1-1")
    d = sp._agent_dir(agent_id)
    (d / "result.txt").write_text("partial\n")
    return d


def _ids(rows: list[dict]) -> list[str]:
    return [row.get("id", "") for row in rows]


def test_a_cleared_stopped_and_leaked_run_keeps_its_ending_after_recovery():
    d = _run("stop0001")
    st.write_tombstone(
        "stop0001",
        cause="stopped",
        recovery_action="none",
        outcome="stopped",
        leaked_process=True,
        taskq_attempt=2,
    )
    died = json.loads((d / "tombstone.json").read_text())["died"]

    assert st.clear_tombstone("stop0001") is True
    assert "stop0001" in _ids(st.list_orphans()), "the clear must re-admit the run"

    # What orphan recovery writes: no outcome of its own.
    st.write_tombstone("stop0001", cause="gateway_restart", recovery_action="partial")
    tombstone = json.loads((d / "tombstone.json").read_text())
    assert tombstone["outcome"] == "stopped"
    assert tombstone["leaked_process"] is True
    assert tombstone["taskq_attempt"] == 2
    assert tombstone["died"] == died
    assert sp.classify_persisted_ending(d)[0] == "stopped"
    assert "stop0001" in _ids(st.list_leaked_processes())
    assert not (d / st._CLEARED_TOMBSTONE).exists(), "the carried record is consumed"


def test_an_explicit_outcome_still_wins_over_a_cleared_one():
    d = _run("stop0002")
    st.write_tombstone("stop0002", cause="stopped", recovery_action="none", outcome="stopped")
    st.clear_tombstone("stop0002")

    st.write_tombstone(
        "stop0002", cause="gateway_restart", recovery_action="delivered", outcome="completed"
    )
    tombstone = json.loads((d / "tombstone.json").read_text())
    assert tombstone["outcome"] == "completed"
    assert "leaked_process" not in tombstone


def test_clearing_a_run_with_no_tombstone_changes_nothing():
    d = _run("stop0003")
    assert st.clear_tombstone("stop0003") is False
    assert not (d / st._CLEARED_TOMBSTONE).exists()

    st.write_tombstone("stop0003", cause="gateway_restart", recovery_action="partial")
    assert "outcome" not in json.loads((d / "tombstone.json").read_text())
