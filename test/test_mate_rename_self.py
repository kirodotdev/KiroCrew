"""Mate takes the name the user gives it: ``rename_self`` renames only the caller.

The ``kirocrew-guide`` tool posts to ``POST /api/guide/agent/rename``. The member
renamed is the one whose pinned thread the verified caller's slot is, never a
name from the body, and only a turn the user sent from the dashboard may do it.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from guide_route_helpers import FakeState, agent, run_guide_app

from kiro_crew import mcp_guide
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.mate_welcome import NOTE_CONFIDENTIALITY, welcome_kickoff

ORDINARY = "scout"
DEFAULT = "kirocrew"
MATE_SLOT = "member-mate"


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch):
    class _Null:
        def log_api_access(self, **_kw):
            return None

    _floor_monkeypatch.setattr(guide_routes, "sel", lambda: _Null())


@pytest.fixture(autouse=True)
def _events(_floor_monkeypatch) -> list[tuple]:
    seen: list[tuple] = []
    from kiro_crew import eventlog_hooks

    _floor_monkeypatch.setattr(eventlog_hooks, "emit", lambda *a, **_k: seen.append(a))
    return seen


def _seed() -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {
        DEFAULT: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ASSISTANT_MEMBER_NAME: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ORDINARY: KiroCrewAgentConfig(kiro_agent=DEFAULT, display_name="Scout"),
    }
    cfg.default_agent = DEFAULT
    cfg.save()
    _invalidate_config_cache()


def _labels() -> dict[str, str]:
    _invalidate_config_cache()
    return {k: v.display_name for k, v in KiroCrewConfig.load().agents.items()}


def _thread(state: FakeState, member: str, key: str = MATE_SLOT):
    slot = state.open_slot(key)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = member
    return slot


def _rename(state: FakeState, body: dict[str, Any], key: str = MATE_SLOT) -> tuple[int, dict]:
    async def go(c):
        r = await c.post("/api/guide/agent/rename", json=body, headers=agent(f"dashboard:{key}"))
        return r.status, await r.json()

    return run_guide_app(go, state)


def test_the_caller_renames_itself_and_only_itself(_events) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    # A member named in the body is ignored: the caller's own thread decides.
    status, body = _rename(state, {"name": "Pebble", "member": ORDINARY})
    assert (status, body["display_name"], body["member"]) == (200, "Pebble", ASSISTANT_MEMBER_NAME)
    labels = _labels()
    assert labels[ASSISTANT_MEMBER_NAME] == "Pebble"
    assert labels[ORDINARY] == "Scout"
    # The roster projection's own event, which the open chat already folds.
    (event,) = _events
    assert event[1] == ASSISTANT_MEMBER_NAME
    assert event[3] == {"display_name": "Pebble", "changed": ["display_name"]}


def test_an_old_thread_cannot_rename_the_crewmate_that_replaced_it(_events) -> None:
    """Mate deleted and created again while its old thread still ran: the old
    turn's thread is on the deleted crewmate's store, so the new row keeps its
    name."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME).memory_store = "member-mate-deleted"
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    assert _events == []


def test_a_crewmate_whose_row_cannot_be_published_is_refused_not_a_500(monkeypatch) -> None:
    """A crewmate declared only in ``config.local.json`` has no base row to write."""
    from kiro_crew.memory_stores import UnknownMemoryStore

    def refuse(*_a, **_k):
        raise UnknownMemoryStore("no base-config row")

    monkeypatch.setattr(members_mod, "rename_member_display", refuse)
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (409, "member_not_writable")


def test_a_turn_that_did_not_come_from_the_dashboard_cannot_rename() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot._turn_channel_origin = True
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (403, "channel_caller")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_a_session_with_no_dashboard_turn_running_cannot_rename() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot.task = None
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (409, "no_dashboard_turn")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_only_a_turn_the_user_sent_can_rename() -> None:
    """The hidden welcome kickoff, a wake or a cron turn runs in Mate's own
    thread with no message from the user, so it cannot pick Mate's name; the
    user's own reply can."""
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot._turn_user_sent = False
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (403, "not_user_turn")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    slot._turn_user_sent = True
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["display_name"]) == (200, "Pebble")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"


@pytest.mark.parametrize("member", ["", DEFAULT, "default", "nobody"])
def test_only_a_crewmate_thread_can_rename(member: str) -> None:
    _seed()
    state = FakeState()
    if member:
        _thread(state, member)
    else:
        state.open_slot(MATE_SLOT)  # an ordinary chat, not a crewmate's thread
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert "Pebble" not in _labels().values()


def test_an_ordinary_chat_running_as_mate_is_not_mates_thread() -> None:
    # A session opened on Mate's agent is not Mate's own pinned thread.
    _seed()
    state = FakeState()
    slot = state.open_slot(MATE_SLOT)
    slot.agent = ASSISTANT_MEMBER_NAME
    status, body = _rename(state, {"name": "Pebble"})
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


@pytest.mark.parametrize(
    ("raw", "saved"),
    [("  Peb\u200bble\x07 ", "Pebble"), ("Mr\tPebble\nthe Second", "Mr Pebble the Second")],
)
def test_the_name_is_cleaned_before_it_is_saved(raw: str, saved: str) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _rename(state, {"name": raw})
    assert (status, body["display_name"]) == (200, saved)
    assert _labels()[ASSISTANT_MEMBER_NAME] == saved


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("\x00 \u200b ", "empty_name"),
        ("", "empty_name"),
        ("x" * (members_mod.MEMBER_NAME_MAX_CHARS + 1), "invalid_name"),
        (42, "invalid_name"),
    ],
)
def test_an_unusable_name_is_refused_and_nothing_changes(raw: object, code: str) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _rename(state, {"name": raw})
    assert (status, body["code"]) == (400, code)
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_the_shim_sends_only_the_name(monkeypatch) -> None:
    sent: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(mcp_guide, "_strict_session_key", lambda: ("dashboard:member-mate", ""))
    monkeypatch.setattr(
        mcp_guide,
        "_post",
        lambda path, body, session_key: sent.append((path, body, session_key))
        or {"ok": True, "display_name": "Pebble"},
    )
    out = mcp_guide._call_tool_inner("rename_self", {"name": "Pebble"})
    assert sent == [("/api/guide/agent/rename", {"name": "Pebble"}, "dashboard:member-mate")]
    assert "Pebble" in out


@pytest.mark.parametrize("others", [False, True])
def test_mates_first_welcome_asks_for_a_name_for_mate(others: bool) -> None:
    entry = KiroCrewAgentConfig(kiro_agent=DEFAULT)
    kickoff = welcome_kickoff(entry, has_other_crewmates=others)
    assert "Introduce yourself as Mate" in kickoff
    # The three things Mate offers, and nothing beyond them.
    assert (
        "getting Kiro Crew set up, sorting out anything that is not working in it, or "
        "showing them around. Keep to those three" in kickoff
    )
    # It finishes on what to call it -- the name is Mate's, not the user's.
    assert "Finish by asking what they would like to call you (you, not them)" in kickoff
    # Asked the way a person asks it, not explained.
    assert '"What would you like to call me?"' in kickoff
    assert "being named" not in kickoff
    # rename_self only once the user answers with a name.
    assert "When they answer with a name for you, call rename_self" in kickoff
    # The reply that renames says so, so the old name in the welcome above is not left unexplained.
    assert "say plainly in that reply that you go by the new name" in kickoff
    # The greeting turn itself still calls nothing.
    assert "Call no tools in this reply." in kickoff
    # Plain words for a newcomer: no session explainer, no developer terms.
    assert "session" not in kickoff and "no developer terms" in kickoff


def test_mates_welcome_fits_a_user_who_already_has_crewmates() -> None:
    entry = KiroCrewAgentConfig(kiro_agent=DEFAULT)
    alone = welcome_kickoff(entry, has_other_crewmates=False)
    crewed = welcome_kickoff(entry, has_other_crewmates=True)
    assert "you are their first crewmate" in alone
    assert "first crewmate" not in crewed
    assert "They already have other crewmates" in crewed
    assert "one more teammate" in crewed


@pytest.mark.parametrize("others", [False, True])
def test_a_welcome_kickoff_is_written_as_principles_not_prohibitions(others: bool) -> None:
    """Outside the one note-confidentiality sentence a kickoff carries no
    must / never / do not / exactly wording."""
    entry = KiroCrewAgentConfig(kiro_agent=DEFAULT, description="ship the release")
    kickoff = welcome_kickoff(entry, has_other_crewmates=others)
    assert NOTE_CONFIDENTIALITY in kickoff
    rest = kickoff.replace(NOTE_CONFIDENTIALITY, "")
    found = re.findall(r"\b(?:must|never|do not|exactly)\b", rest, flags=re.IGNORECASE)
    assert found == []
