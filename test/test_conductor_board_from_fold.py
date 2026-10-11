"""The goal conductor's drawer board is the work fold's, not the agent's last publish.

The bug these pin: the drawer showed only what the conductor last sent through
``panel_publish``, so a cycle that skipped the publish left new items off the board
for hours while the work ledger already held them. The board is now derived from the
fold on every read; an agent-written extra older than the ledger is labelled.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import agent_panel
from kiro_crew import crew_log as lg
from kiro_crew.conductor_board_contract import (
    BOARD_TEMPLATE_ID,
    build_conductor_board,
)
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import agent_panel as panel_routes

SLOT = "member-kirocrew-conductor"
UNIT = "acp-kirocrew-conductor"
CREW = "kirocrew-conductor"
SLUG = "kirocrew-conductor"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


def _unit() -> None:
    CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent=CREW, slot=SLOT)


def _work(*, action: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": SLOT, "actor": "conductor", "by": SLOT, "action": action}
    payload.update({k: v for k, v in fields.items() if v is not None})
    assert crew_log_emit.on_work_recorded(UNIT, payload, timeout=5.0) is True
    crew_log_emit.flush(timeout=5.0)
    crew_log.forget_slot_folds()


def _create(item_id: str, title: str) -> None:
    _work(
        action="create",
        item_id=item_id,
        title=title,
        acceptance={"kind": "pr_checks", "pr": "TBD", "repo": "octo/repo"},
        round=0,
    )


def _read(*, member: str = CREW) -> dict[str, Any] | None:
    return panel_routes._panel_record(SLOT, SLUG, agent_panel.crew_key(CREW), member=member)


def _titles(record: dict[str, Any]) -> list[str]:
    return [t["task"] for t in record["data"]["tasks"]]


def test_an_item_created_in_the_ledger_appears_with_no_publish_at_all() -> None:
    _unit()
    _work(action="goal", goal="polish the crew page", round=0)
    _create("it_00000001", "T: first task")

    record = _read()

    assert record is not None
    assert record["template"] == BOARD_TEMPLATE_ID
    assert record["crew_key"] == agent_panel.crew_key(CREW)
    assert _titles(record) == ["T: first task"]
    assert record["data"]["done"] == "0 of 1"
    # And it composes into the drawer document.
    assert "T: first task" in (agent_panel.render_record(record) or "")


def test_an_item_created_after_the_last_publish_appears_without_another() -> None:
    _unit()
    _work(action="goal", goal="polish the crew page", round=0)
    _create("it_00000001", "T: first task")
    agent_panel.publish(
        SLUG,
        template=BOARD_TEMPLATE_ID,
        data={
            "needs_you": "",
            "done": "0 of 1",
            "updated": "then",
            "next": "dispatch T",
            "tasks": [{"task": "T: first task", "state": "working", "step": "Code"}],
        },
        crew=CREW,
    )
    _create("it_00000002", "U: second task")
    _create("it_00000003", "V: third task")

    record = _read()

    assert record is not None
    assert _titles(record) == ["T: first task", "U: second task", "V: third task"]
    assert record["data"]["done"] == "0 of 3"


def test_needs_you_is_derived_from_a_question_or_a_block() -> None:
    _unit()
    _work(action="goal", goal="g", round=0)
    _create("it_00000001", "T: asks")
    _create("it_00000002", "U: quiet")
    _work(
        action="report",
        actor="worker",
        item_id="it_00000001",
        status="question",
        summary="which option?",
    )

    record = _read()

    assert record is not None
    assert "T: asks" in record["data"]["needs_you"]
    assert "which option?" in record["data"]["needs_you"]
    states = {t["task"]: t["state"] for t in record["data"]["tasks"]}
    assert states["T: asks"] == "needs you"
    assert states["U: quiet"] != "needs you"


def test_a_stale_extra_is_labelled_with_its_own_time() -> None:
    view: Any = {
        "conductor": {"last_entry_at": "2026-10-10T18:00:00+00:00", "entries": 3},
        "items": [],
        "omitted": 0,
    }
    published = {"next": "dispatch W", "tasks": [{"task": "old"}]}

    stale = build_conductor_board(view, published, "2026-10-10T15:02:00+00:00")
    fresh = build_conductor_board(view, published, "2026-10-10T18:05:00+00:00")

    assert stale["next"] == "dispatch W"
    assert stale["next_from"] == "2026-10-10 15:02 UTC"
    assert "next_from" not in fresh
    # The published task list is never the source.
    assert stale["tasks"] == [] and fresh["tasks"] == []


def test_a_published_board_with_no_ledger_is_served_as_published() -> None:
    _unit()
    record = agent_panel.publish(
        SLUG,
        template=BOARD_TEMPLATE_ID,
        data={"needs_you": "", "done": "", "updated": "", "next": "n", "tasks": []},
        crew=CREW,
    )

    served = _read()

    assert served is not None
    assert served["data"] == record["data"]


def test_nothing_is_synthesized_without_a_ledger_or_on_a_shared_slot() -> None:
    _unit()
    assert _read() is None
    _work(action="goal", goal="g", round=0)
    _create("it_00000001", "T")
    # ``member`` empty is how the read route says the slot is shared.
    assert _read(member="") is None
