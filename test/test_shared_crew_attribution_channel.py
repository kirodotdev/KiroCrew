"""Only a persisted pre-attribution session reads as the owner's.

A live slot nobody stamped yet, and a session a messaging channel started, are
legacy too, but a teammate may have started them, so they stay Unattributed on
every surface that reads public attribution: the session list's filters and the
People view.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import _make_state
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.shared_crew import attribution as attr
from kiro_crew.shared_crew import people
from kiro_crew.shared_crew.contracts import SessionQuery
from kiro_crew.shared_crew.owner import owner_principal, viewer_ids
from kiro_crew.shared_crew.session_query import query_sessions

pytestmark = pytest.mark.usefixtures("host_isolation")


class _AttrSlot(_ChatSlot):
    """The real slot plus an instance dict, so ``_attribution`` can be held."""


def _restored(key: str, **fields) -> _AttrSlot:
    slot = _AttrSlot(key)
    for name, value in fields.items():
        setattr(slot, name, value)
    attr.restore_from_metadata(slot, {})
    return slot


def test_a_restored_dashboard_slot_still_reads_as_the_owner():
    public = attr.public_attribution_of_slot(_restored("chat-old"))
    assert public["availability"] == "legacy_missing"
    assert public["creator"]["id"] == owner_principal().id


def test_a_live_slot_nobody_stamped_is_unattributed():
    public = attr.public_attribution_of_slot(_AttrSlot("chat-fresh"))
    assert public["availability"] == "legacy_missing"
    assert public["creator"]["kind"] == "unknown"


@pytest.mark.parametrize(
    "key, fields",
    [
        ("chat-surfaced", {"channel_origin": True}),
        ("slack:1712793600.123456", {}),
        ("chat-linked", {"linked_session_key": "discord:42"}),
        ("chat-linked-legacy", {"linked_session_key": "1712793600.123456"}),
    ],
    ids=[
        "channel-origin-flag",
        "channel-key",
        "linked-to-channel",
        "linked-to-legacy-slack",
    ],
)
def test_a_restored_channel_origin_slot_is_unattributed(key, fields):
    public = attr.public_attribution_of_slot(_restored(key, **fields))
    assert public["creator"]["kind"] == "unknown"


def test_a_cron_linked_slot_is_not_mistaken_for_a_channel():
    public = attr.public_attribution_of_slot(_restored("chat-x", linked_session_key="cron:abc"))
    assert public["creator"]["id"] == owner_principal().id


@pytest.mark.parametrize(
    "meta, key",
    [
        ({}, "slack_1712793600.123456"),
        ({}, "1712793600.123456"),
        (None, "discord_42"),
        ({"channel_origin": True}, "dashboard_chat-surfaced"),
        ({"linked_session_key": "slack:1.2"}, "dashboard_chat-linked"),
    ],
    ids=[
        "channel-stem",
        "legacy-slack-stem",
        "channel-stem-no-line",
        "channel-origin-flag",
        "linked-to-channel",
    ],
)
def test_a_persisted_channel_session_is_unattributed(meta, key):
    public = attr.public_attribution_from_meta(meta, key=key)
    assert public["availability"] == "legacy_missing"
    assert public["creator"]["kind"] == "unknown"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(state_module, "_ChatSlot", _AttrSlot)
    st = _make_state(tmp_path)
    lines = {
        "dashboard_old": {"_type": "metadata", "title": "old"},
        "slack_1712793600.123456": {"_type": "metadata", "title": "from slack"},
        "1712793600.654321": {"_type": "metadata", "title": "from legacy slack"},
        "dashboard_chat-surfaced": {"_type": "metadata", "channel_origin": True},
    }
    for key, line in lines.items():
        path = st.conversation_log._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    return [r for r in st.conversation_log.list_sessions() if r["key"] in lines]


def test_the_session_list_files_channel_sessions_under_unattributed(catalog):
    owner = owner_principal()
    mine = SessionQuery.from_query({"creator": "mine", "limit": "50"})
    page = query_sessions(catalog, mine, owner, viewer_ids=viewer_ids(owner))
    assert {i["key"] for i in page.items} == {"dashboard_old"}
    unattributed = SessionQuery.from_query({"creator": "unattributed", "limit": "50"})
    page = query_sessions(catalog, unattributed, owner)
    assert {i["key"] for i in page.items} == {
        "slack_1712793600.123456",
        "1712793600.654321",
        "dashboard_chat-surfaced",
    }


def test_people_counts_only_the_pre_attribution_session_to_the_owner(catalog):
    people.reset_for_tests()
    try:
        view = people.people_view(owner_principal(), catalog)
    finally:
        people.reset_for_tests()
    counts = {row["principal"]["id"]: row["sessions_created"] for row in view["people"]}
    assert counts == {owner_principal().id: 1}
