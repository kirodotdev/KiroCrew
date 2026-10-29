"""A Crew Mode crew member's ``ask_question`` is refused toward its conductor.

``_crew_worker_refusal`` answers ``409 crew_worker_asks_conductor`` for a session a
Crew Mode root dispatched (``conductor_open`` says whether that root's tab is live),
and ``None`` for a root or a plain chat. Who is a crew member is read from the same
lineage the slot frame reads: the session tree's edges over the birth stamps, so a
session its conductor released asks the person again, and an adopted one is sent
to its new conductor.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import crew_mode
from kiro_crew.dashboard import session_control
from kiro_crew.dashboard.handlers import ask_question as aq

CONDUCTOR = "kirocrew-conductor"


def _slot(key: str, *, agent: str = "kirocrew", created_by: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        agent=agent,
        agent_kind="template",
        mode="",
        memory_mode="persistent",
        _created_by=created_by,
        _saved_created_by="",
        _lineage_minted=bool(created_by),
        _app="",
        instance_id="",
        crew_mode_agent="",
    )


def _state(*slots: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(_slots={s.key: s for s in slots})


@pytest.fixture(autouse=True)
def _quiet_audit(_floor_monkeypatch):
    # The refusal audits through the SEL; a test state has none, and the handler
    # already fails open around it. Pin a stub so the test never writes a log.
    _floor_monkeypatch.setattr(
        aq, "sel", lambda: SimpleNamespace(log_tool_invocation=lambda **k: None)
    )
    _floor_monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: False)
    _floor_monkeypatch.setattr(session_control, "_frame_parents", lambda state: None)
    # The routing is off on this base (the Crew panel part turns it on); the rule
    # is tested as it will run then.
    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", True)


def _refusal(state: SimpleNamespace, key: str):
    return aq._crew_worker_refusal(state, key, f"dashboard:{key}")


def test_a_crew_member_is_refused_and_told_its_conductor_is_open():
    root = _slot("r", agent=CONDUCTOR)
    worker = _slot("w", created_by="r")
    resp = _refusal(_state(root, worker), "w")
    assert resp is not None and resp.status == 409
    body = json.loads(resp.text)
    assert body["code"] == "crew_worker_asks_conductor"
    assert body["conductor_open"] is True
    assert "work_report" in body["error"]


def test_a_crew_member_whose_conductor_tab_is_closed_is_still_refused_and_told_so(
    _floor_monkeypatch,
):
    # A closed root is still a root the gateway remembers (``crew_mode.known_roots``).
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", {"r"})
    worker = _slot("w", created_by="r")
    resp = _refusal(_state(worker), "w")
    assert resp is not None and resp.status == 409
    assert json.loads(resp.text)["conductor_open"] is False


def test_a_root_and_a_plain_chat_still_ask():
    root = _slot("r", agent=CONDUCTOR)
    plain = _slot("p")
    state = _state(root, plain)
    assert _refusal(state, "r") is None
    assert _refusal(state, "p") is None


def test_a_released_crew_member_asks_the_person_again(_floor_monkeypatch):
    """The tree is the record: a release clears the edge, and the stale birth stamp
    must not keep sending the session's questions to a conductor that let it go."""
    root = _slot("r", agent=CONDUCTOR)
    worker = _slot("w", created_by="r")
    state = _state(root, worker)
    _floor_monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    _floor_monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset({"w", "r"}))
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(session_control, "_frame_parents", lambda s: {"r": None, "w": None})
    assert _refusal(state, "w") is None


def test_an_adopted_crew_member_is_sent_to_its_new_conductor(_floor_monkeypatch):
    """The tree's edge outranks the birth stamp: after ``session_adopt`` the crew
    member belongs to its new conductor, open or not."""
    old = _slot("r", agent=CONDUCTOR)
    new = _slot("r2", agent=CONDUCTOR)
    worker = _slot("w", created_by="r")
    state = _state(old, new, worker)
    _floor_monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
    _floor_monkeypatch.setattr(
        crew_mode, "persisted_tree_slots", lambda: frozenset({"w", "r", "r2"})
    )
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {"w": "r2"})
    # The frame's ``parent`` names the creator (``slot``) and its nearest open
    # ancestor (``key``), the shape ``_attach_slot_parents`` writes.
    edge = {"slot": "r2", "key": "r2"}
    _floor_monkeypatch.setattr(
        session_control, "_frame_parents", lambda s: {"r": None, "r2": None, "w": edge}
    )
    resp = _refusal(state, "w")
    assert resp is not None and json.loads(resp.text)["conductor_open"] is True
    # With the new conductor's tab closed, the refusal stands and says so.
    del state._slots["r2"]
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", {"r2"})
    closed_edge = {"slot": "r2", "key": None}
    _floor_monkeypatch.setattr(
        session_control, "_frame_parents", lambda s: {"r": None, "w": closed_edge}
    )
    resp = _refusal(state, "w")
    assert resp is not None and json.loads(resp.text)["conductor_open"] is False


def test_a_stamp_fault_fails_open(_floor_monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no stamps")

    _floor_monkeypatch.setattr(crew_mode, "live_stamps", _boom)
    assert _refusal(_state(_slot("w", created_by="r")), "w") is None


def test_the_refusal_is_off_until_the_crew_panel_part_turns_it_on(_floor_monkeypatch):
    """On this base ``crew_mode.APPROVAL_ROUTING_LIVE`` is False, so a crew member's
    question still reaches the person, as it does today."""
    _floor_monkeypatch.setattr(crew_mode, "APPROVAL_ROUTING_LIVE", False)
    root = _slot("r", agent=CONDUCTOR)
    worker = _slot("w", created_by="r")
    assert _refusal(_state(root, worker), "w") is None
