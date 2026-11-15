"""The Spend ranking never names a private conversation.

``/api/telemetry/startup`` attaches a title to each ranked conversation, from
the live slot or, for a closed one, from the transcript's metadata line. An
incognito or temporary session is documented as never listable or searchable,
and the metadata line's ``memory_mode`` is its privacy contract. A title is a
summary of what the session was about, so a private session's title is left
off its row exactly as an unnamed conversation's is: the row still reports its
spend, because the cost belongs to the user's account, not to the session.

These drive the REAL ``ConversationLog`` and write the metadata line the way a
restricted session's header records it, so the privacy read is production's.
"""

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers.telemetry import _with_conversation_titles
from kiro_crew.history import ConversationLog


def _request(slots: dict, conversation_log=None) -> SimpleNamespace:
    state = SimpleNamespace(
        get_slot=lambda key: slots.get(key),
        conversation_log=conversation_log,
    )
    return SimpleNamespace(app={"state": state})


def _cost(*slot_keys: str) -> dict:
    return {"conversations": [{"slot": k, "credits": 1.0} for k in slot_keys]}


def _write_header(tmp_path, slot_key: str, memory_mode: str, title: str) -> None:
    key = slot_transcript_key(slot_key)
    header = {
        "_type": "metadata",
        "created_at": "2026-11-10T10:00:00+00:00",
        "title": title,
        "memory_mode": memory_mode,
    }
    path = tmp_path / (key.replace(":", "_") + ".jsonl")
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary", "Temporary"])
async def test_a_private_transcripts_title_is_left_off_its_row(tmp_path, mode):
    _write_header(tmp_path, "chat-1-1", mode, "Billing audit")
    log = ConversationLog(tmp_path)

    out = await _with_conversation_titles(_request({}, conversation_log=log), _cost("chat-1-1"))

    row = out["conversations"][0]
    assert "title" not in row, f"{mode} title published: {row.get('title')!r}"
    assert row["credits"] == 1.0


@pytest.mark.asyncio
async def test_a_persistent_transcripts_title_is_still_published(tmp_path):
    _write_header(tmp_path, "chat-1-1", "persistent", "Billing audit")
    log = ConversationLog(tmp_path)

    out = await _with_conversation_titles(_request({}, conversation_log=log), _cost("chat-1-1"))

    assert out["conversations"][0]["title"] == "Billing audit"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_private_live_slots_title_is_left_off_its_row(tmp_path, mode):
    # The live title wins over the persisted one for an ordinary chat; a private
    # slot is not named from either source.
    _write_header(tmp_path, "chat-1-1", mode, "Old name")
    log = ConversationLog(tmp_path)
    slots = {"chat-1-1": SimpleNamespace(display_title="Billing audit", memory_mode=mode)}

    out = await _with_conversation_titles(_request(slots, conversation_log=log), _cost("chat-1-1"))

    row = out["conversations"][0]
    assert "title" not in row, f"{mode} live title published: {row.get('title')!r}"


@pytest.mark.asyncio
async def test_a_live_slot_whose_transcript_turned_private_is_not_named(tmp_path):
    # The line is a ratchet any writer may tighten (another gateway, a cron on
    # the same session); a slot whose own record lags it falls through to the
    # persisted read, which consults the line.
    _write_header(tmp_path, "chat-1-1", "incognito", "Billing audit")
    log = ConversationLog(tmp_path)
    slots = {"chat-1-1": SimpleNamespace(display_title="", memory_mode="persistent")}

    out = await _with_conversation_titles(_request(slots, conversation_log=log), _cost("chat-1-1"))

    assert "title" not in out["conversations"][0]


@pytest.mark.asyncio
async def test_a_titled_live_slot_whose_transcript_turned_private_is_not_named(tmp_path):
    # Same ratchet, but the lagging slot already carries a title of its own.
    _write_header(tmp_path, "chat-1-1", "incognito", "Billing audit")
    log = ConversationLog(tmp_path)
    slots = {"chat-1-1": SimpleNamespace(display_title="Billing audit", memory_mode="persistent")}

    out = await _with_conversation_titles(_request(slots, conversation_log=log), _cost("chat-1-1"))

    assert "title" not in out["conversations"][0]


@pytest.mark.asyncio
async def test_a_titled_live_slot_keeps_its_title_over_the_persisted_one(tmp_path):
    # Pins main: a rename shows before it is persisted.
    _write_header(tmp_path, "chat-1-1", "persistent", "Old name")
    log = ConversationLog(tmp_path)
    slots = {"chat-1-1": SimpleNamespace(display_title="Billing audit", memory_mode="persistent")}

    out = await _with_conversation_titles(_request(slots, conversation_log=log), _cost("chat-1-1"))

    assert out["conversations"][0]["title"] == "Billing audit"


@pytest.mark.asyncio
async def test_an_unreadable_header_names_nothing(tmp_path):
    key = slot_transcript_key("chat-1-1")
    path = tmp_path / (key.replace(":", "_") + ".jsonl")
    path.write_text('{"_type": "metadata", "title": "Billing audit"\n', encoding="utf-8")
    log = ConversationLog(tmp_path)

    out = await _with_conversation_titles(_request({}, conversation_log=log), _cost("chat-1-1"))

    assert "title" not in out["conversations"][0]
