"""A crewmate changes itself: ``update_self`` writes only the caller's own record.

The ``kirocrew-mate`` tool posts to ``POST /api/member/self/update``. The member
changed is the one whose pinned thread the verified caller's slot is, never a
name from the body; only a turn the user sent from the dashboard may do it; the
change lands in the chat as one ``self`` row the user can undo through
``POST /api/members/{slug}/self-changes/{id}/undo``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from guide_route_helpers import FakeState, agent, run_guide_app

from kiro_crew import mcp_mate
from kiro_crew import members as members_mod
from kiro_crew.agent_files import ASSISTANT_MEMBER_NAME
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard import chat_cards, member_self_changes
from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.handlers import member_self as self_routes
from kiro_crew.dashboard.mate_welcome import NOTE_CONFIDENTIALITY, welcome_kickoff

ORDINARY = "scout"
DEFAULT = "kirocrew"
MATE_SLOT = "member-mate"

UPDATE = ("POST", "/api/member/self/update", self_routes.api_member_self_update)
UNDO = (
    "POST",
    "/api/members/{slug}/self-changes/{change_id}/undo",
    self_routes.api_member_self_undo,
)


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


@pytest.fixture(autouse=True)
def _fresh_undo_store():
    member_self_changes.reset()
    yield
    member_self_changes.reset()


@pytest.fixture(autouse=True)
def _owner_ok(_floor_monkeypatch):
    """The undo is the owner's click; these tests are about what it restores."""

    async def allow(_request, _operation):
        return None

    _floor_monkeypatch.setattr(self_routes, "require_owner_dashboard_request", allow)


def _seed(avatar: dict | None = None) -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {
        DEFAULT: KiroCrewAgentConfig(kiro_agent=DEFAULT),
        ASSISTANT_MEMBER_NAME: KiroCrewAgentConfig(kiro_agent=DEFAULT, avatar=avatar or {}),
        ORDINARY: KiroCrewAgentConfig(kiro_agent=DEFAULT, display_name="Scout"),
    }
    cfg.default_agent = DEFAULT
    cfg.save()
    _invalidate_config_cache()


def _record(member: str) -> KiroCrewAgentConfig:
    _invalidate_config_cache()
    return KiroCrewConfig.load().agents[member]


def _labels() -> dict[str, str]:
    _invalidate_config_cache()
    return {k: v.display_name for k, v in KiroCrewConfig.load().agents.items()}


def _thread(state: FakeState, member: str, key: str = MATE_SLOT):
    slot = state.open_slot(key)
    slot.mode = members_mod.DM_SLOT_MODE
    slot.agent = member
    return slot


def _update(state: FakeState, fields: dict[str, Any], key: str = MATE_SLOT) -> tuple[int, dict]:
    async def go(c):
        r = await c.post(
            "/api/member/self/update", json={"fields": fields}, headers=agent(f"dashboard:{key}")
        )
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[UPDATE])


def _undo(
    state: FakeState, slug: str, change_id: str, slot: str | None = MATE_SLOT
) -> tuple[int, dict]:
    """Press Undo as the page does: naming the conversation the row is in."""

    async def go(c):
        r = await c.post(
            f"/api/members/{slug}/self-changes/{change_id}/undo",
            json={"slot": slot} if slot is not None else {},
        )
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[UNDO])


def _self_rows(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if m.get("role") == chat_cards.ROLE_CARD
        and (m.get("meta") or {}).get("card", {}).get("surface") == chat_cards.SURFACE_SELF
    ]


# ── the name ──


def test_the_caller_renames_itself_and_only_itself(_events) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble"})
    assert status == 200, body
    assert (body["member"], body["changed"], body["after"]) == (
        ASSISTANT_MEMBER_NAME,
        ["display_name"],
        {"display_name": "Pebble"},
    )
    labels = _labels()
    assert labels[ASSISTANT_MEMBER_NAME] == "Pebble"
    assert labels[ORDINARY] == "Scout"
    # The roster projection's own event, which the open chat already folds.
    (event,) = _events
    assert event[1] == ASSISTANT_MEMBER_NAME
    assert event[3] == {"display_name": "Pebble", "changed": ["display_name"]}
    # One row in the chat, drawn where the tool call was, with the undo record.
    (row,) = _self_rows(slot)
    card = row["meta"]["card"]
    assert card["id"] == body["change_id"]
    assert (card["fields"], card["status"]) == (["display_name"], "applied")
    assert (card["before"], card["after"]) == (
        {"display_name": ""},
        {"display_name": "Pebble"},
    )


def test_an_old_thread_cannot_change_the_crewmate_that_replaced_it(_events) -> None:
    """Mate deleted and created again while its old thread still ran: the old
    turn's thread is on the deleted crewmate's store, so the new row is kept."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME).memory_store = "member-mate-deleted"
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    assert _events == []


def test_a_crewmate_whose_row_cannot_be_published_is_refused_not_a_500(monkeypatch) -> None:
    """A crewmate declared only in ``config.local.json`` has no base row to write."""
    from kiro_crew.memory_stores import UnknownMemoryStore

    def refuse(*_a, **_k):
        raise UnknownMemoryStore("no base-config row")

    monkeypatch.setattr(members_mod, "update_member_self", refuse)
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (409, "member_not_writable")


def test_a_turn_that_did_not_come_from_the_dashboard_cannot_change() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot._turn_channel_origin = True
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (403, "channel_caller")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_a_session_with_no_dashboard_turn_running_cannot_change() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot.task = None
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (409, "no_dashboard_turn")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_only_a_turn_the_user_sent_can_change() -> None:
    """The hidden welcome kickoff, a wake or a cron turn runs in Mate's own
    thread with no message from the user, so it cannot pick Mate's name; the
    user's own reply can."""
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    slot._turn_user_sent = False
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (403, "not_user_turn")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    slot._turn_user_sent = True
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["after"]["display_name"]) == (200, "Pebble")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"


@pytest.mark.parametrize("member", ["", DEFAULT, "default", "nobody"])
def test_only_a_crewmate_thread_can_change(member: str) -> None:
    _seed()
    state = FakeState()
    if member:
        _thread(state, member)
    else:
        state.open_slot(MATE_SLOT)  # an ordinary chat, not a crewmate's thread
    status, body = _update(state, {"display_name": "Pebble"})
    assert (status, body["code"]) == (403, "not_a_crewmate")
    assert "Pebble" not in _labels().values()


def test_an_ordinary_chat_running_as_mate_is_not_mates_thread() -> None:
    # A session opened on Mate's agent is not Mate's own pinned thread.
    _seed()
    state = FakeState()
    slot = state.open_slot(MATE_SLOT)
    slot.agent = ASSISTANT_MEMBER_NAME
    status, body = _update(state, {"display_name": "Pebble"})
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
    status, body = _update(state, {"display_name": raw})
    assert (status, body["after"]["display_name"]) == (200, saved)
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
    status, body = _update(state, {"display_name": raw})
    assert (status, body["code"]) == (400, code)
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


@pytest.mark.parametrize("taken", ["Scout", DEFAULT])
def test_a_name_another_crew_shows_is_refused_and_nothing_changes(taken: str, _events) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": taken})
    assert (status, body["code"]) == (400, "name_taken")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    assert _events == []


def test_a_credential_shaped_name_is_refused_and_not_echoed(_events) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    raw = "https://user:password@example.com"
    status, body = _update(state, {"display_name": raw})
    assert (status, body["code"]) == (400, "invalid_name")
    assert raw not in str(body)
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""
    assert _events == []


def test_its_own_current_values_are_kept_without_a_write_or_a_row(_events) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, key="member-scout")
    status, body = _update(state, {"display_name": "Scout"}, key="member-scout")
    assert (status, body["changed"]) == (200, [])
    assert "change_id" not in body
    assert _events == []
    assert _self_rows(slot) == []


# ── the description and the face ──


def test_the_description_is_cleaned_and_saved_and_the_row_names_the_field(_events) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    raw = "I watch\u200b the builds.\n\n\n\nI\tfile what breaks.  "
    status, body = _update(state, {"description": raw})
    assert status == 200, body
    assert body["after"]["description"] == "I watch the builds.\n\nI file what breaks."
    assert (
        _record(ASSISTANT_MEMBER_NAME).description == "I watch the builds.\n\nI file what breaks."
    )
    (row,) = _self_rows(slot)
    assert row["meta"]["card"]["kind"] == "description"
    (event,) = _events
    assert event[3]["changed"] == ["description"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (42, "invalid_description"),
        ("x" * (members_mod.MEMBER_DESCRIPTION_MAX_CHARS + 1), "invalid_description"),
        ("token https://user:password@example.com", "invalid_description"),
    ],
)
def test_an_unusable_description_is_refused(raw: object, code: str, _events) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"description": raw})
    assert (status, body["code"]) == (400, code)
    assert _events == []


def test_a_partial_face_pick_on_a_name_derived_face_fills_the_plain_mark() -> None:
    """Leaving an axis out never draws a face with that axis missing."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": {"traits": {"eyes": "sparkle", "accessory": "cap"}}})
    assert status == 200, body
    traits = body["after"]["avatar"]["traits"]
    assert traits["eyes"] == "sparkle" and traits["accessory"] == "cap"
    assert traits["mouth"] == "none" and traits["prop"] == "none" and traits["brows"] == "none"
    assert traits["tile"] == members_mod.DEFAULT_GHOST_TRAITS["tile"]
    assert _record(ASSISTANT_MEMBER_NAME).avatar["kind"] == "ghost"


def test_a_face_pick_keeps_the_pinned_axes_and_the_reactions_it_did_not_name() -> None:
    _seed(
        avatar={
            "kind": "ghost",
            "traits": {
                "eyes": "wink",
                "brows": "raised",
                "mouth": "grin",
                "accessory": "halo",
                "prop": "mug",
                "blush": True,
                "flip": False,
                "tile": "#259d85",
            },
            "sounds": {"done": "chime"},
        }
    )
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": {"traits": {"eyes": "glasses"}}})
    assert status == 200, body
    after = body["after"]["avatar"]
    assert after["traits"]["eyes"] == "glasses"
    assert after["traits"]["accessory"] == "halo" and after["traits"]["tile"] == "#259d85"
    assert after["traits"]["blush"] is True
    assert after["sounds"] == {"done": "chime"}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("sparkle", "invalid_avatar"),
        ({"kind": "image"}, "invalid_avatar"),
        ({"traits": {}}, "invalid_avatar"),
        ({"traits": {"hat": "cap"}}, "invalid_avatar"),
        ({"traits": {"eyes": "sparkle"}, "file": "x.png"}, "invalid_avatar"),
    ],
)
def test_an_unusable_face_is_refused(raw: object, code: str, _events) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": raw})
    assert (status, body["code"]) == (400, code)
    assert _events == []


@pytest.mark.parametrize(
    "worn",
    [{"kind": "image", "file": "0123456789abcdef.png"}, {"kind": "pack", "id": "pixel-pals"}],
)
def test_a_picture_or_a_pack_is_never_replaced_from_the_chat(worn: dict, _events) -> None:
    _seed(avatar=worn)
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": {"traits": {"eyes": "sparkle"}}})
    assert (status, body["code"]) == (400, "avatar_not_ghost")
    assert _record(ASSISTANT_MEMBER_NAME).avatar["kind"] == worn["kind"]
    assert _events == []


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({}, "no_fields"),
        ({"workspace": "incidents"}, "unknown_field"),
        ({"memory_store": "other"}, "unknown_field"),
        ({"model": "x"}, "unknown_field"),
    ],
)
def test_only_the_presentation_fields_are_the_crewmates_to_change(fields: dict, code: str) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, fields)
    assert (status, body["code"]) == (400, code)


def test_several_fields_change_together_in_one_row(_events) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(
        state, {"display_name": "Pebble", "avatar": {"traits": {"eyes": "heart"}}}
    )
    assert status == 200, body
    assert body["changed"] == ["display_name", "avatar"]
    (row,) = _self_rows(slot)
    assert row["meta"]["card"]["kind"] == "profile"
    assert row["meta"]["card"]["fields"] == ["display_name", "avatar"]
    (event,) = _events
    assert event[3]["changed"] == ["display_name", "avatar"]


# ── the undo ──


def test_the_user_takes_a_change_back_and_the_row_says_so(_events) -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble", "description": "I watch builds."})
    assert status == 200, body
    change_id = body["change_id"]
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, undone = _undo(state, slug, change_id)
    assert status == 200, undone
    assert undone["status"] == "undone"
    assert sorted(undone["changed"]) == ["description", "display_name"]
    record = _record(ASSISTANT_MEMBER_NAME)
    assert (record.display_name, record.description) == ("", "")
    (row,) = _self_rows(slot)
    assert row["meta"]["card"]["status"] == "undone"
    assert [e[3]["changed"] for e in _events] == [
        ["display_name", "description"],
        ["display_name", "description"],
    ]


def test_an_undo_restores_the_face_as_it_was_stored() -> None:
    before = {
        "kind": "ghost",
        "traits": {
            "eyes": "wink",
            "brows": "none",
            "mouth": "none",
            "accessory": "none",
            "prop": "none",
            "blush": False,
            "flip": False,
            "tile": "#25679d",
        },
    }
    _seed(avatar=before)
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": {"traits": {"eyes": "heart", "tile": "#de2121"}}})
    assert status == 200, body
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, _undone = _undo(state, slug, body["change_id"])
    assert status == 200
    assert _record(ASSISTANT_MEMBER_NAME).avatar == before


def test_an_undo_is_bound_to_the_conversation_the_change_was_made_in() -> None:
    """A row is transcript data the agent can copy into another crewmate's chat
    with its id and slug intact. The page names the conversation Undo was pressed
    in, from its own state, and the record must have been made there."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble"})
    assert status == 200, body
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    # Pressed in another crewmate's chat: refused, and nothing is written.
    status, refused = _undo(state, slug, body["change_id"], slot="member-scout")
    assert (status, refused["code"]) == (410, "undo_unavailable")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"
    # A request that names no conversation cannot be bound and is refused.
    status, refused = _undo(state, slug, body["change_id"], slot=None)
    assert (status, refused["code"]) == (400, "slot_required")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"
    # In its own chat: the undo lands.
    assert _undo(state, slug, body["change_id"])[0] == 200
    assert _labels()[ASSISTANT_MEMBER_NAME] != "Pebble"


def test_an_undo_is_refused_twice_and_for_another_crewmate() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble"})
    assert status == 200, body
    change_id = body["change_id"]
    other = members_mod.member_slug(ORDINARY)
    status, refused = _undo(state, other, change_id)
    assert (status, refused["code"]) == (410, "undo_unavailable")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    assert _undo(state, slug, change_id)[0] == 200
    status, again = _undo(state, slug, change_id)
    assert (status, again["code"]) == (409, "already_undone")
    assert _labels()[ASSISTANT_MEMBER_NAME] == ""


def test_an_undo_the_gateway_no_longer_remembers_is_unavailable() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"display_name": "Pebble"})
    assert status == 200, body
    member_self_changes.reset()  # a restart
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, body["change_id"])
    assert (status, refused["code"]) == (410, "undo_unavailable")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Pebble"


def test_an_undo_refuses_a_name_another_crewmate_has_taken_since() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    # Mate was "Pebble", becomes "Rock"; Scout then takes "Pebble".
    assert _update(state, {"display_name": "Pebble"})[0] == 200
    status, body = _update(state, {"display_name": "Rock"})
    assert status == 200, body
    cfg = KiroCrewConfig.load()
    cfg.agents[ORDINARY].display_name = "Pebble"
    cfg.save()
    _invalidate_config_cache()
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, body["change_id"])
    assert (status, refused["code"]) == (409, "name_taken")
    assert _labels()[ASSISTANT_MEMBER_NAME] == "Rock"


def test_the_undo_store_keeps_only_the_newest_changes(monkeypatch) -> None:
    monkeypatch.setattr(member_self_changes, "MAX_CHANGES", 2)
    ids = [
        member_self_changes.record(
            member="m",
            slug="m",
            slot_key="member-m",
            memory_store="default",
            changed=["description"],
            before={"description": str(i)},
            after={"description": str(i + 1)},
        )["id"]
        for i in range(3)
    ]
    assert member_self_changes.get(ids[0]) is None
    assert member_self_changes.get(ids[1]) is not None
    assert member_self_changes.get(ids[2]) is not None


# ── what the undo may touch, and what the row may show ──


def test_an_undo_touches_only_the_fields_that_change_actually_moved() -> None:
    """A description change sent with the unchanged current name records only the
    description; undoing it later leaves a rename made in between alone."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    assert _update(state, {"display_name": "Scout II"})[0] == 200
    status, body = _update(state, {"display_name": "Scout II", "description": "I watch builds."})
    assert status == 200, body
    assert body["changed"] == ["description"]
    assert body["after"] == {"display_name": "Scout II", "description": "I watch builds."}
    assert _update(state, {"display_name": "Pebble"})[0] == 200
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, undone = _undo(state, slug, body["change_id"])
    assert status == 200, undone
    assert undone["changed"] == ["description"]
    record = _record(ASSISTANT_MEMBER_NAME)
    assert (record.display_name, record.description) == ("Pebble", "")


def test_the_row_masks_a_credential_shaped_trait_but_the_undo_keeps_it() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    planted = "AKIAIOSFODNN7EXAMPLE"
    status, body = _update(state, {"avatar": {"traits": {"eyes": planted}}})
    assert status == 200, body
    (row,) = _self_rows(slot)
    shown = row["meta"]["card"]["after"]["avatar"]["traits"]["eyes"]
    assert planted not in shown
    assert (
        member_self_changes.get(body["change_id"])["after"]["avatar"]["traits"]["eyes"] == planted
    )


def test_the_row_names_the_crewmate_by_its_display_name_not_its_key() -> None:
    _seed()
    state = FakeState()
    slot = _thread(state, ORDINARY, key="member-scout")
    status, body = _update(state, {"description": "I watch builds."}, key="member-scout")
    assert status == 200, body
    assert body["name"] == "Scout"
    (row,) = _self_rows(slot)
    assert row["meta"]["card"]["name"] == "Scout"


def test_the_crewmate_hears_about_an_undo_once_on_its_next_turn() -> None:
    from kiro_crew.context_blocks import render_self_change_undos

    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"avatar": {"traits": {"eyes": "heart"}}})
    assert status == 200, body
    # Nothing to tell while the change stands.
    assert member_self_changes.take_unreported_undos(MATE_SLOT) == []
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    assert _undo(state, slug, body["change_id"])[0] == 200
    (told,) = member_self_changes.take_unreported_undos(MATE_SLOT)
    assert told["changed"] == ["avatar"]
    # Told once: the turn after next hears nothing again.
    assert member_self_changes.take_unreported_undos(MATE_SLOT) == []
    # Another thread's turn hears nothing about this one.
    assert member_self_changes.take_unreported_undos("member-scout") == []
    block = render_self_change_undos([told])
    assert "pressed Undo" in block and "your avatar" in block
    assert "heart" not in block
    assert render_self_change_undos([]) == ""


def test_an_undo_is_refused_once_the_field_was_edited_again() -> None:
    """An old row's Undo never overwrites a newer edit to the same field."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, first = _update(state, {"description": "I watch builds."})
    assert status == 200, first
    cfg = KiroCrewConfig.load()
    cfg.agents[ASSISTANT_MEMBER_NAME].description = "I watch builds and file what breaks."
    cfg.save()
    _invalidate_config_cache()
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, first["change_id"])
    assert (status, refused["code"]) == (409, "stale_undo")
    assert _record(ASSISTANT_MEMBER_NAME).description == "I watch builds and file what breaks."
    # Still undoable later if the newer value is put back by hand.
    cfg = KiroCrewConfig.load()
    cfg.agents[ASSISTANT_MEMBER_NAME].description = "I watch builds."
    cfg.save()
    _invalidate_config_cache()
    assert _undo(state, slug, first["change_id"])[0] == 200
    assert _record(ASSISTANT_MEMBER_NAME).description == ""


def test_an_oversized_original_is_applied_but_keeps_no_undo_record(monkeypatch) -> None:
    """A value already on the record may exceed the write paths' caps; such a
    change is drawn without Undo rather than retained unbounded or truncated."""
    monkeypatch.setattr(member_self_changes, "SNAPSHOT_FIELD_MAX_CHARS", 40)
    _seed()
    cfg = KiroCrewConfig.load()
    cfg.agents[ASSISTANT_MEMBER_NAME].description = "x" * 200
    cfg.save()
    _invalidate_config_cache()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"description": "short"})
    assert status == 200, body
    assert body["undoable"] is False
    assert _record(ASSISTANT_MEMBER_NAME).description == "short"
    (row,) = _self_rows(slot)
    assert row["meta"]["card"]["undoable"] is False
    assert member_self_changes.get(body["change_id"]) is None
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, body["change_id"])
    assert (status, refused["code"]) == (410, "undo_unavailable")


def test_the_undo_compare_and_set_runs_inside_the_config_lock(monkeypatch) -> None:
    """A crew-editor save that commits after the undo's snapshot check, but before
    its write, is kept: the persist re-reads the field under the config lock."""
    from kiro_crew import memory_stores

    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"description": "I watch builds."})
    assert status == 200, body
    real_persist = memory_stores.persist_member_config

    def commit_between(config, member, **kw):
        # The competing save lands between the snapshot check and the locked write.
        other = KiroCrewConfig.load()
        other.agents[member].description = "I watch builds and file what breaks."
        other.save()
        _invalidate_config_cache()
        return real_persist(config, member, **kw)

    monkeypatch.setattr(memory_stores, "persist_member_config", commit_between)
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, body["change_id"])
    assert (status, refused["code"]) == (409, "stale_undo")
    assert _record(ASSISTANT_MEMBER_NAME).description == "I watch builds and file what breaks."


def test_a_save_landing_between_the_snapshot_and_the_write_is_refused(monkeypatch) -> None:
    """The undo record must hold what was on disk at the write: a crew-editor save
    that commits in between is kept and the crewmate is told to try again."""
    from kiro_crew import memory_stores

    _seed()
    state = FakeState()
    slot = _thread(state, ASSISTANT_MEMBER_NAME)
    real_persist = memory_stores.persist_member_config

    def commit_between(config, member, **kw):
        other = KiroCrewConfig.load()
        other.agents[member].description = "Set from the editor."
        other.save()
        _invalidate_config_cache()
        return real_persist(config, member, **kw)

    monkeypatch.setattr(memory_stores, "persist_member_config", commit_between)
    status, body = _update(state, {"description": "I watch builds."})
    assert (status, body["code"]) == (409, "changed_meanwhile")
    assert _record(ASSISTANT_MEMBER_NAME).description == "Set from the editor."
    assert _self_rows(slot) == []


def test_a_description_the_write_path_accepts_always_keeps_its_undo_record() -> None:
    """One bound for one population: the longest description the crewmate may
    save, full of quotes and line breaks (which its serialized form escapes), is
    retained and undoable; the record measures a string as the write path does."""
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    longest = ('"\n' * (members_mod.MEMBER_DESCRIPTION_MAX_CHARS // 2))[
        : members_mod.MEMBER_DESCRIPTION_MAX_CHARS
    ]
    assert len(longest) == members_mod.MEMBER_DESCRIPTION_MAX_CHARS
    assert member_self_changes.SNAPSHOT_FIELD_MAX_CHARS == members_mod.MEMBER_DESCRIPTION_MAX_CHARS
    status, body = _update(state, {"description": longest})
    assert status == 200, body
    assert body["undoable"] is True
    assert member_self_changes.get(body["change_id"]) is not None
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    assert _undo(state, slug, body["change_id"])[0] == 200


def test_an_oversized_display_name_keeps_no_undo_record_either(monkeypatch) -> None:
    monkeypatch.setattr(member_self_changes, "SNAPSHOT_FIELD_MAX_CHARS", 40)
    _seed()
    cfg = KiroCrewConfig.load()
    cfg.agents[ASSISTANT_MEMBER_NAME].display_name = "N" * 60
    cfg.save()
    _invalidate_config_cache()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"description": "short"})
    assert status == 200, body
    assert body["undoable"] is False
    assert member_self_changes.get(body["change_id"]) is None


# ── the shim ──


def test_the_shim_sends_only_the_fields_it_was_given(monkeypatch) -> None:
    sent: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard:member-mate", ""))
    monkeypatch.setattr(
        mcp_mate,
        "_post",
        lambda path, body, session_key: sent.append((path, body, session_key))
        or {"ok": True, "changed": ["display_name"], "after": {"display_name": "Pebble"}},
    )
    out = mcp_mate._call_tool_inner("update_self", {"display_name": "Pebble"})
    assert sent == [
        (
            "/api/member/self/update",
            {"fields": {"display_name": "Pebble"}},
            "dashboard:member-mate",
        )
    ]
    assert "Pebble" in out and "Undo" in out


def test_the_shim_says_nothing_changed_when_the_gateway_refused(monkeypatch) -> None:
    monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard:chat-1", ""))
    monkeypatch.setattr(
        mcp_mate,
        "_post",
        lambda *_a, **_k: {"error": "only a crewmate can", "code": "not_a_crewmate"},
    )
    out = mcp_mate._call_tool_inner("update_self", {"description": "x"})
    assert out.startswith("Error:") and "Nothing about you changed" in out


def test_a_card_backed_ask_names_the_request_never_a_kind(monkeypatch) -> None:
    sent: list[tuple[str, dict, str]] = []
    monkeypatch.setattr(mcp_mate, "_strict_session_key", lambda: ("dashboard:member-mate", ""))
    monkeypatch.setattr(
        mcp_mate,
        "_post",
        lambda path, body, session_key: sent.append((path, body, session_key))
        or {"id": "c1", "status": "pending"},
    )
    mcp_mate._call_tool_inner("set_approval_mode", {"approval_mode": "normal", "reason": "why"})
    mcp_mate._call_tool_inner("set_model", {"model": "auto"})
    assert sent == [
        (
            "/api/member/self/propose",
            {"request": "approval_mode", "params": {"approval_mode": "normal"}, "reason": "why"},
            "dashboard:member-mate",
        ),
        (
            "/api/member/self/propose",
            {"request": "model", "params": {"model": "auto"}},
            "dashboard:member-mate",
        ),
    ]


def test_every_member_tool_is_granted_and_schema_checked() -> None:
    from kiro_crew.agent import _MATE_AUTO_GRANTS, _MATE_SERVER
    from kiro_crew.validation import MCP_MATE_SCHEMAS

    names = {t["name"] for t in mcp_mate._list_tools()}
    assert names == set(MCP_MATE_SCHEMAS)
    assert {g.removeprefix(f"@{_MATE_SERVER}/") for g in _MATE_AUTO_GRANTS} == names


# ── the card-backed asks ──

PROPOSE = ("POST", "/api/member/self/propose", self_routes.api_member_self_propose)


def _propose(state: FakeState, body: dict[str, Any], key: str = MATE_SLOT) -> tuple[int, dict]:
    async def go(c):
        r = await c.post("/api/member/self/propose", json=body, headers=agent(f"dashboard:{key}"))
        return r.status, await r.json()

    return run_guide_app(go, state, extra_routes=[PROPOSE])


@pytest.fixture
def _proposed(monkeypatch) -> list[dict[str, Any]]:
    """Captures what the member route hands the shared propose body."""
    from aiohttp import web

    from kiro_crew.dashboard.handlers import change_cards as card_routes

    seen: list[dict[str, Any]] = []

    async def fake_propose(request, slot_key, sk, *, kind, params, reason):
        seen.append({"slot_key": slot_key, "kind": kind, "params": params, "reason": reason})
        return web.json_response({"id": "card-1", "kind": kind, "status": "pending"})

    monkeypatch.setattr(card_routes, "propose_for_caller", fake_propose)
    return seen


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"request": "rules", "params": {}}, "unknown_request"),
        ({"request": "goal", "params": {}}, "invalid_params"),
        ({"request": "goal", "params": {"long_term": "x", "member": "Other"}}, "invalid_params"),
        ({"request": "model"}, "invalid_params"),
        ({"request": "model", "params": {"workspace": "x"}}, "invalid_params"),
        ({"request": "model", "params": {}}, "invalid_params"),
        ({"request": "approval_mode", "params": {"model": "auto"}}, "invalid_params"),
    ],
)
def test_an_ask_names_a_known_request_with_its_own_fields(body: dict, code: str, _proposed) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, answer = _propose(state, body)
    assert (status, answer["code"]) == (400, code)
    assert _proposed == []


def test_an_ask_fixes_the_kind_and_the_member_itself(_proposed) -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, answer = _propose(
        state,
        {
            "request": "model",
            "params": {"model": "auto", "reasoning_effort": "low"},
            "reason": "why",
        },
    )
    assert (status, answer["status"]) == (200, "pending")
    status, _ = _propose(state, {"request": "approval_mode", "params": {"approval_mode": "normal"}})
    assert status == 200
    status, _ = _propose(
        state, {"request": "capabilities", "params": {"draft": {"operations": [], "member": "x"}}}
    )
    assert status == 200
    assert [p["kind"] for p in _proposed] == [
        "crewmate.update",
        "crewmate.update",
        "crewmate.capabilities",
    ]
    assert _proposed[0]["params"] == {
        "name": ASSISTANT_MEMBER_NAME,
        "fields": {"model": "auto", "reasoning_effort": "low"},
    }
    assert _proposed[0]["reason"] == "why"
    assert _proposed[1]["params"] == {
        "name": ASSISTANT_MEMBER_NAME,
        "fields": {"approval_mode": "normal"},
    }
    # The member is the caller's own: a member named in the draft is not used.
    assert _proposed[2]["params"] == {
        "member": ASSISTANT_MEMBER_NAME,
        "draft": {"operations": [], "member": "x"},
    }
    assert all(p["slot_key"] == MATE_SLOT for p in _proposed)


def test_only_a_crewmate_thread_can_ask(_proposed) -> None:
    _seed()
    state = FakeState()
    state.open_slot(MATE_SLOT)
    status, answer = _propose(state, {"request": "model", "params": {"model": "auto"}})
    assert (status, answer["code"]) == (403, "not_a_crewmate")
    assert _proposed == []


# ── the other refusals of the two routes ──


def test_fields_must_be_a_non_empty_object() -> None:
    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)

    async def go(c):
        r = await c.post(
            "/api/member/self/update", json={"fields": "x"}, headers=agent(f"dashboard:{MATE_SLOT}")
        )
        return r.status, await r.json()

    status, body = run_guide_app(go, state, extra_routes=[UPDATE])
    assert (status, body["code"]) == (400, "no_fields")


def test_an_undo_with_a_bad_slug_or_an_unpublishable_row_is_a_coded_refusal(monkeypatch) -> None:
    from kiro_crew.memory_stores import UnknownMemoryStore

    _seed()
    state = FakeState()
    _thread(state, ASSISTANT_MEMBER_NAME)
    status, body = _update(state, {"description": "I watch builds."})
    assert status == 200, body
    status, refused = _undo(state, "Not A Slug!", body["change_id"])
    assert (status, refused["code"]) == (400, "invalid_member_slug")

    def refuse(*_a, **_k):
        raise UnknownMemoryStore("no base-config row")

    monkeypatch.setattr(members_mod, "undo_member_self", refuse)
    slug = members_mod.member_slug(ASSISTANT_MEMBER_NAME)
    status, refused = _undo(state, slug, body["change_id"])
    assert (status, refused["code"]) == (409, "member_not_writable")


# ── Mate's welcome ──


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
    # update_self only once the user answers with a name.
    assert "When they answer with a name for you, call update_self with display_name" in kickoff
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
    assert "you are the first teammate on their crew" in alone
    assert "first teammate" not in crewed
    assert "They already have other mates" in crewed
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
