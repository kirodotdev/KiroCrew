"""Shared Crew participants: who has written in a session, for its row's initials (#35145).

Driven through the real slot append, the real metadata codec and the real full
save, like ``test_shared_crew_attribution``.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import _make_state
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.dashboard import state as state_module
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.shared_crew import attribution as attr
from kiro_crew.shared_crew import participants as parts
from kiro_crew.shared_crew.contracts import ActorContext, Principal, Provenance, Transport
from kiro_crew.shared_crew.identity import tailnet_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

ALICE = tailnet_principal("alice@example.com", "laptop.tail.ts.net", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "phone.tail.ts.net", Provenance.TAILSCALE_SERVE)
CAROL = tailnet_principal("carol@example.com", "desk.tail.ts.net", Provenance.TAILSCALE_SERVE)


class _PartSlot(_ChatSlot):
    """The real slot plus an instance dict, as the attribution suite uses."""


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r-1", transport=Transport.HTTP)


def _write(slot, principal: Principal, text: str = "hi") -> None:
    slot.append("user", text, "msg msg-u", meta=attr.stamp_message_author({}, _actor(principal)))


def _ids(slot) -> list[str]:
    return [p["id"] for p in parts.ordered(slot)]


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(state_module, "_ChatSlot", _PartSlot)
    return _make_state(tmp_path)


def test_only_person_authored_live_rows_add_participants(state):
    slot = state.get_or_create_slot("parts-who")
    _write(slot, ALICE)
    # Crew automation, an Unknown channel sender, a row with no author and a
    # replayed row (broadcast=False) are not participants.
    from kiro_crew.shared_crew.admission_gate import AUTOMATION_PRINCIPAL

    slot.append(
        "user", "cron", "msg msg-u", meta=attr.stamp_principal_author({}, AUTOMATION_PRINCIPAL)
    )
    slot.append(
        "user", "slack", "msg msg-u", meta=attr.stamp_principal_author({}, Principal.unknown())
    )
    slot.append("user", "nobody", "msg msg-u")
    slot.append(
        "user",
        "replay",
        "msg msg-u",
        broadcast=False,
        meta=attr.stamp_message_author({}, _actor(BOB)),
    )
    slot.append(
        "assistant", "reply", "msg msg-a", meta=attr.stamp_message_author({}, _actor(CAROL))
    )
    assert _ids(slot) == [ALICE.id]
    # Only the public shape: no issuer or subject reaches a list row.
    assert set(parts.ordered(slot)[0]) == {"id", "kind", "display"}


def test_creator_first_then_most_recent_activity(state):
    slot = state.get_or_create_slot("parts-order")
    attr.stamp_creator(slot, _actor(ALICE), now=1.0)
    parts.note_author(slot, attr.stamp_message_author({}, _actor(ALICE)), now=1.0)
    parts.note_author(slot, attr.stamp_message_author({}, _actor(BOB)), now=2.0)
    parts.note_author(slot, attr.stamp_message_author({}, _actor(CAROL)), now=3.0)
    assert _ids(slot) == [ALICE.id, CAROL.id, BOB.id]
    # Bob writes again: he moves ahead of Carol, and Alice stays first.
    parts.note_author(slot, attr.stamp_message_author({}, _actor(BOB)), now=4.0)
    assert _ids(slot) == [ALICE.id, BOB.id, CAROL.id]


def test_the_list_is_capped_without_dropping_the_creator(monkeypatch):
    monkeypatch.setattr(parts, "MAX_PARTICIPANTS", 2)
    slot = _PartSlot("parts-cap")
    attr.stamp_creator(slot, _actor(ALICE), now=1.0)
    for i, who in enumerate((ALICE, BOB, CAROL)):
        parts.note_author(slot, attr.stamp_message_author({}, _actor(who)), now=float(i + 1))
    assert _ids(slot) == [ALICE.id, CAROL.id]


def test_participants_survive_a_full_save_and_restart(state):
    slot = state.get_or_create_slot("parts-save")
    attr.stamp_creator(slot, _actor(ALICE), now=1.0)
    _write(slot, ALICE)
    _write(slot, BOB)
    assert _save_slot_to_history(state, slot, force=True)

    path = state.conversation_log._path(slot_history_key(slot))
    with open(path, encoding="utf-8") as f:
        line = json.loads(f.readline())
    assert [e["principal"]["id"] for e in line["participants"]] == [ALICE.id, BOB.id]

    fresh = _PartSlot("parts-save-restored")
    codec.apply(state, fresh, line, codec.Restore(name="parts-save-restored"))
    assert _ids(fresh) == _ids(slot) == [ALICE.id, BOB.id]


def test_an_unreadable_or_non_person_stored_entry_reads_as_nobody():
    slot = _PartSlot("parts-damaged")
    parts.restore_from_metadata(
        slot,
        {
            "participants": [
                "junk",
                {"principal": Principal.unknown().to_public_dict(), "last_at": 1.0},
                {"principal": {"id": "x"}, "last_at": "soon"},
                {"principal": BOB.to_public_dict(), "last_at": True},
            ]
        },
    )
    assert _ids(slot) == [BOB.id]
    parts.restore_from_metadata(slot, {"participants": {"not": "a list"}})
    assert parts.ordered(slot) == []
    assert (
        codec.encode(
            slot, folds=codec.SaveFolds(memory_mode=slot.memory_mode, created_at=slot.created_at)
        ).get("participants")
        is None
    )


def test_the_slot_projection_carries_participants(state):
    slot = state.get_or_create_slot("parts-row")
    _write(slot, BOB)
    assert [p["id"] for p in slot.to_dict()["participants"]] == [BOB.id]
