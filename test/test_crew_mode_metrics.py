"""Crew Mode decision 1: the ``kirocrew.crew_mode.*`` counters.

Each counter is pinned to its closed attribute set, and no attribute may carry a
session key, title or message text.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import crew_mode
from kiro_crew.metrics import crew_mode as m


@pytest.fixture
def emitted(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(m, "emit_counter", lambda name, attrs: out.append((name, attrs)))
    return out


def test_turned_on_and_new_chats_give_the_share_and_repeat_use(emitted):
    m.record_new_chat(crew_mode=False)
    m.record_new_chat(crew_mode=True)
    m.record_turned_on("create", repeat=False)
    m.record_turned_on("switch", repeat=True)
    m.record_turned_on("chat-7 secret title", repeat=True)
    assert emitted == [
        (m.CREW_MODE_NEW_CHATS, {"crew_mode": False}),
        (m.CREW_MODE_NEW_CHATS, {"crew_mode": True}),
        (m.CREW_MODE_TURNED_ON, {"via": "create", "repeat": False}),
        (m.CREW_MODE_TURNED_ON, {"via": "switch", "repeat": True}),
        (m.CREW_MODE_TURNED_ON, {"via": "other", "repeat": True}),
    ]


def test_a_finished_run_counts_its_state_and_its_steers_bucketed(emitted):
    m.record_run_finished("accepted", 0)
    m.record_run_finished("abandoned", 5)
    m.record_run_finished("open", None)
    assert emitted == [
        (m.CREW_MODE_RUNS_FINISHED, {"state": "accepted"}),
        (m.CREW_MODE_STEERS_PER_RUN, {"steers": "0"}),
        (m.CREW_MODE_RUNS_FINISHED, {"state": "abandoned"}),
        (m.CREW_MODE_STEERS_PER_RUN, {"steers": "3+"}),
        (m.CREW_MODE_RUNS_FINISHED, {"state": "other"}),
        (m.CREW_MODE_STEERS_PER_RUN, {"steers": "other"}),
    ]


def test_only_a_waiting_report_counts_as_stuck_or_needing_the_person(emitted):
    m.record_worker_report("progress", None)
    m.record_worker_report("done", None)
    m.record_worker_report("blocked", None)
    m.record_worker_report("question", "needs_human")
    m.record_worker_report("blocked", "approval")
    assert emitted == [
        (m.CREW_MODE_WORKER_WAITS, {"status": "blocked", "reason": "none"}),
        (m.CREW_MODE_WORKER_WAITS, {"status": "question", "reason": "needs_human"}),
        (m.CREW_MODE_WORKER_WAITS, {"status": "blocked", "reason": "approval"}),
    ]


def test_a_steer_counts_whether_it_landed_on_an_item(emitted):
    m.record_steer(on_item=True)
    m.record_steer(on_item=False)
    assert emitted == [
        (m.CREW_MODE_STEERS, {"on_item": True}),
        (m.CREW_MODE_STEERS, {"on_item": False}),
    ]


def test_every_attribute_value_is_closed(emitted):
    m.record_turned_on("switch", repeat=True)
    m.record_run_finished("rejected", 2)
    m.record_worker_report("question", "approval")
    m.record_steer(on_item=True)
    allowed = (
        set(m.TURNED_ON_VIA)
        | set(m.RUN_STATES)
        | set(m.STEER_BUCKETS)
        | set(m.WAIT_STATUSES)
        | set(m.WAIT_REASONS)
        | {m.OTHER, True, False}
    )
    for _name, attrs in emitted:
        assert set(attrs.values()) <= allowed


def test_a_crew_mode_board_is_a_known_or_live_root(monkeypatch):
    monkeypatch.setattr(crew_mode, "_known_roots", {"closed-root"})
    live = SimpleNamespace(
        key="live",
        agent="kirocrew-team-lead",
        agent_kind="template",
        crew_mode_agent="",
        mode="",
        memory_mode="persistent",
        _created_by="",
        _app="",
        instance_id="",
    )
    state = SimpleNamespace(_slots={"live": live})
    assert m.board_is_crew_mode(state, "closed-root")
    assert m.board_is_crew_mode(state, "live")
    assert not m.board_is_crew_mode(state, "plain")


def test_every_counter_is_documented_and_wired():
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs/system-specs/modules/metrics.md").read_text(encoding="utf-8")
    for name in (
        m.CREW_MODE_TURNED_ON,
        m.CREW_MODE_NEW_CHATS,
        m.CREW_MODE_RUNS_FINISHED,
        m.CREW_MODE_STEERS_PER_RUN,
        m.CREW_MODE_STEERS,
        m.CREW_MODE_WORKER_WAITS,
    ):
        assert f"`{name}`" in doc
    src = root / "src/kiro_crew/dashboard"
    sites = {
        "chat_api/agent_switch.py": "record_turned_on(",
        "chat_api/slot_create.py": "record_new_chat(",
        "handlers/work_ledger_conductor.py": "record_run_finished(",
        "handlers/work_ledger_worker.py": "record_worker_report(",
        "handlers/crew_workers.py": "record_steer(",
    }
    for path, call in sites.items():
        assert call in (src / path).read_text(encoding="utf-8"), path
