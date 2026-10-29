"""The Crew Mode run, steer and wait counters (``metrics.crew_mode``).

A run's close counts ``runs_finished{state}`` and ``steers_per_run{steers}``; a crew
member's ``blocked`` or ``question`` report counts ``worker_waits{status,reason}``
and any other status counts nothing. Every attribute value is a member of a closed
tuple or :data:`OTHER`, and no session key, item id or text is emitted.
"""

from __future__ import annotations

import pytest

from kiro_crew.metrics import crew_mode as m


@pytest.fixture
def emitted(_floor_monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    _floor_monkeypatch.setattr(m, "emit_counter", lambda name, attrs: rows.append((name, attrs)))
    return rows


@pytest.mark.parametrize(
    ("count", "bucket"),
    [(0, "0"), (1, "1"), (2, "2"), (3, "3+"), (40, "3+"), (-1, m.OTHER), (None, m.OTHER)],
)
def test_steer_bucket_is_a_closed_set(count, bucket) -> None:
    assert m.steer_bucket(count) == bucket


def test_a_bool_is_not_a_steer_count() -> None:
    assert m.steer_bucket(True) == m.OTHER


def test_a_finished_run_counts_its_state_and_its_steers(emitted) -> None:
    m.record_run_finished("accepted", 2)
    assert emitted == [
        (m.CREW_MODE_RUNS_FINISHED, {"state": "accepted"}),
        (m.CREW_MODE_STEERS_PER_RUN, {"steers": "2"}),
    ]


def test_an_unknown_state_and_an_unreadable_steer_count_map_to_other(emitted) -> None:
    m.record_run_finished("it_0123abcd", None)
    assert emitted == [
        (m.CREW_MODE_RUNS_FINISHED, {"state": m.OTHER}),
        (m.CREW_MODE_STEERS_PER_RUN, {"steers": m.OTHER}),
    ]


@pytest.mark.parametrize(
    ("status", "reason", "want_reason"),
    [
        ("blocked", "approval", "approval"),
        ("question", "needs_human", "needs_human"),
        ("blocked", None, m.NO_REASON),
        ("question", "", m.NO_REASON),
        ("blocked", "a free-text reason", m.OTHER),
    ],
)
def test_a_waiting_report_counts_status_and_reason(emitted, status, reason, want_reason) -> None:
    m.record_worker_report(status, reason)
    assert emitted == [(m.CREW_MODE_WORKER_WAITS, {"status": status, "reason": want_reason})]


@pytest.mark.parametrize("status", ["progress", "done", None, "it_0123abcd"])
def test_a_report_that_does_not_wait_counts_nothing(emitted, status) -> None:
    m.record_worker_report(status, "approval")
    assert emitted == []


class _Slot:
    def __init__(self, agent: str, created_by: str = "") -> None:
        self.agent = agent
        self.agent_kind = "template"
        self._created_by = created_by
        self._lineage_minted = bool(created_by)
        self.crew_mode_agent = ""
        self.mode = ""
        self.memory_mode = "persistent"


class _State:
    def __init__(self, **slots: _Slot) -> None:
        self._slots = slots


def test_a_board_is_crew_mode_when_its_slot_is_a_live_root(_floor_monkeypatch) -> None:
    from kiro_crew import crew_mode

    _floor_monkeypatch.setattr(crew_mode, "known_roots", lambda: frozenset())
    state = _State(root=_Slot("kirocrew-conductor"), plain=_Slot("kirocrew"))
    assert m.board_is_crew_mode(state, "root") is True
    assert m.board_is_crew_mode(state, "plain") is False
    assert m.board_is_crew_mode(state, "missing") is False


def test_a_closed_root_the_gateway_remembers_still_counts(_floor_monkeypatch) -> None:
    from kiro_crew import crew_mode

    _floor_monkeypatch.setattr(crew_mode, "known_roots", lambda: frozenset({"gone"}))
    assert m.board_is_crew_mode(_State(), "gone") is True


def test_a_fault_in_the_root_test_answers_false(_floor_monkeypatch) -> None:
    from kiro_crew import crew_mode

    def _boom() -> frozenset[str]:
        raise RuntimeError("no roots")

    _floor_monkeypatch.setattr(crew_mode, "known_roots", _boom)
    assert m.board_is_crew_mode(_State(root=_Slot("kirocrew-conductor")), "root") is False


def test_a_turned_on_chat_counts_how_and_whether_it_is_a_repeat(emitted) -> None:
    m.record_turned_on("create", repeat=False)
    m.record_turned_on("switch", repeat=True)
    m.record_turned_on("it_0123abcd", repeat=1)
    assert emitted == [
        (m.CREW_MODE_TURNED_ON, {"via": "create", "repeat": False}),
        (m.CREW_MODE_TURNED_ON, {"via": "switch", "repeat": True}),
        (m.CREW_MODE_TURNED_ON, {"via": m.OTHER, "repeat": True}),
    ]


def test_a_new_chat_counts_whether_it_was_born_in_crew_mode(emitted) -> None:
    m.record_new_chat(crew_mode=True)
    m.record_new_chat(crew_mode=False)
    assert emitted == [
        (m.CREW_MODE_NEW_CHATS, {"crew_mode": True}),
        (m.CREW_MODE_NEW_CHATS, {"crew_mode": False}),
    ]
