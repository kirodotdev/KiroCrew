"""A board or panel write that resolved a unit before its slot's carry lands where the carry is.

A worker report resolves the conductor's unit (``_conductor_unit``), the board relay
resolves the board's newest unit (``work_relay._target``) and the panel route resolves the
member's live unit before their writer job runs. A reset can open the slot's next unit and
commit its carry in between. A write that still lands in the older unit, and publishes it
as the unit that recorded last, makes the fold read the carried state and then replay the
older unit's whole history.

Each case runs twice: with the older unit closed ``superseded`` once the carry landed, and
with a session-map key still mapping it, so it stays open and only the carry's own record
of what it carried (``late_writes.note_carried``) can say the slot moved past it.
"""

from __future__ import annotations

from typing import Any

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import emit, emit_runtime, projection
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store, work_relay
from kiro_crew.crew_log.entry_types import PANEL_FOLD_NAME
from kiro_crew.crew_log.writer import JobKind
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

BOARD = "member-late-board"
BOARD_SESSION_KEY = f"dashboard:{BOARD}"
WORKER = "dashboard:late-worker"
PANEL = "member-late-panel"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter and the carry folded inline.

    The session tree's debounced checkpoint is not armed: its worker sleeps on the
    maintenance pool past the test, and the conftest teardown drops the projection that
    armed it before this fixture's own teardown could wait for it.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    monkeypatch.setattr(stp.SessionTreeProjection, "_schedule_checkpoint", lambda self: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        stp.reset_for_tests()


@pytest.fixture(params=["closed", "mapped"])
def older(request) -> str:
    """Whether the older unit is closed ``superseded`` after the carry or kept by a key."""
    return request.param


def _probe(older: str, unit: str, key: str) -> None:
    """The session map: nothing mapped, or *key* still mapping *unit*."""
    if older == "mapped":
        emit.set_session_key_probe(lambda session_id: key if session_id == unit else None)
    else:
        emit.set_session_key_probe(lambda session_id: None)


def _closed(unit: str) -> bool:
    return store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, unit))


def _create() -> dict:
    return {
        "slot": BOARD,
        "actor": "conductor",
        "by": BOARD,
        "action": "create",
        "item_id": "it-1",
        "title": "item it-1",
    }


def _report() -> dict:
    return {
        "slot": BOARD,
        "actor": "worker",
        "by": WORKER,
        "action": "report",
        "item_id": "it-1",
        "status": "done",
        "summary": "the worker reported",
    }


def _create_then_reset(older: str) -> None:
    """``c-a`` records one create, then ``c-b`` opens for the board and carries it."""
    _probe(older, "c-a", BOARD_SESSION_KEY)
    emit.on_session_opened("c-a", agent="kirocrew", slot=BOARD)
    assert_drained()
    assert emit.on_work_recorded("c-a", _create())
    assert_drained()
    emit.on_session_opened(
        "c-b", agent="kirocrew", slot=BOARD, previous_sid="c-a", session_key=BOARD_SESSION_KEY
    )
    assert_drained()
    assert _closed("c-a") is (older == "closed")


def _board() -> dict:
    projection.forget_slot_folds()
    return projection.read_slot_projection(BOARD, "work").value


@pytest.mark.usefixtures("carrying")
def test_a_report_resolved_to_the_older_unit_reads_once_after_the_carry(older):
    """The GPT trigger: the report's unit was chosen before the reset carried the board."""
    _create_then_reset(older)

    assert emit.on_work_recorded("c-a", _report(), timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()

    board = _board()
    assert board["conductor"]["entries"] == 2
    assert board["omitted"] == 0
    assert session_ledger.work_crew_log_units(BOARD)[-1] == "c-b"


@pytest.mark.usefixtures("carrying")
def test_a_relayed_copy_resolved_to_the_older_unit_reads_once_after_the_carry(older):
    """The relay's unit is chosen off the writer too, so the same reset reaches it."""
    _create_then_reset(older)
    copy = {
        "slot": "member-late-sub",
        "actor": "conductor",
        "by": "member-late-sub",
        "action": "create",
        "item_id": "sub-1",
        "title": "item sub-1",
        "relay": "c-sub:3",
    }

    work_relay.hand_over([work_relay.Copy(BOARD, "c-a", "work/recorded", copy)])
    assert_drained()

    board = _board()
    assert board["conductor"]["entries"] == 1
    assert board["omitted"] == 0
    assert session_ledger.work_crew_log_units(BOARD)[-1] == "c-b"


@pytest.mark.usefixtures("carrying")
def test_a_waiter_that_gave_up_before_the_handed_on_job_ran_gets_nothing_landed(older, monkeypatch):
    """``False`` is final across the hand-over: the newer unit's job appends nothing."""
    _create_then_reset(older)
    held: "list[tuple[str, Any]]" = []
    real = emit_runtime._hand_over

    def _hold_follow_ons(unit: str, job: Any) -> None:
        if job.kind is JobKind.FOLLOW_ON:
            held.append((unit, job))
            return
        real(unit, job)

    with pytest.MonkeyPatch.context() as patch:
        # Through the facade: every member that binds ``_hand_over`` takes the patch.
        patch.setattr(emit, "_hand_over", _hold_follow_ons)
        # The handed-on job is held, so nothing can wake this waiter: a 0 s wait gives up.
        answered = emit.on_work_recorded("c-a", _report(), timeout=0.0)

    assert [unit for unit, _job in held] == ["c-b"], "the job was handed to the carrier"
    assert answered is False
    for unit, job in held:
        real(unit, job)
    assert_drained()

    assert _board()["conductor"]["entries"] == 1


def _publish(unit: str, title: str) -> None:
    record = {
        "template": "default",
        "data": {"cycle": title},
        "title": title,
        "crew": "late-panel",
        "crew_key": "a" * 40,
    }
    assert emit.on_panel_published(unit, record, timeout=FALLBACK_TIMEOUT_SECS)
    assert_drained()


@pytest.mark.usefixtures("carrying")
def test_a_publish_resolved_to_the_older_unit_reads_once_after_the_carry(older):
    """The panel route resolves the member's unit before its job runs, as the report does."""
    _probe(older, "p-a", f"dashboard:{PANEL}")
    emit.on_session_opened("p-a", agent="kirocrew", slot=PANEL)
    assert_drained()
    _publish("p-a", "first")
    emit.on_session_opened(
        "p-b", agent="kirocrew", slot=PANEL, previous_sid="p-a", session_key=f"dashboard:{PANEL}"
    )
    assert_drained()
    assert _closed("p-a") is (older == "closed")

    _publish("p-a", "second")

    projection.forget_slot_folds()
    panel = projection.read_slot_projection(PANEL, PANEL_FOLD_NAME).value
    assert panel["title"] == "second"
    assert panel["publishes"] == 2
    assert [row["title"] for row in panel["history"]] == ["first"]
    assert session_ledger.panel_crew_log_units(PANEL)[-1] == "p-b"
