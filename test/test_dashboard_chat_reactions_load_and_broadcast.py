"""Chat reaction load failures and the ``reactions_changed`` broadcast.

Mirrors ``test_dashboard_chat_pins_load_and_broadcast.py``: a transient read
error on load must not clobber valid in-memory state, and the broadcast
fires after every successful mutation and never after a failed one. The
route contract and slot isolation are in ``test_dashboard_chat_reactions.py``.
"""

from __future__ import annotations

import json as _json
from unittest.mock import MagicMock

import pytest
from dashboard_chat_reactions_helpers import _client, _make_state, _raise_os_error


@pytest.mark.asyncio
async def test_load_transient_io_error_preserves_existing_state(tmp_path, monkeypatch):
    """A transient read I/O error during load_chat_reactions must NOT replace
    valid in-memory reactions with an empty list — it must re-raise so the
    caller knows the load failed and the previous in-memory state remains."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    valid_reaction = {
        "id": "existing-r1",
        "slot_key": "slot-1",
        "mid": "m-existing-valid",
        "emoji": "👍",
        "reacted_at": "2026-01-01T00:00:00+00:00",
    }
    (tmp_path / "chat_reactions.json").write_text(_json.dumps([valid_reaction]), encoding="utf-8")

    state = _make_state(tmp_path)
    state._chat_reactions = [valid_reaction.copy()]

    def _failing_read(*args, **kwargs):
        raise PermissionError("transient disk permission error")

    monkeypatch.setattr(type(tmp_path / "chat_reactions.json"), "read_text", _failing_read)

    with pytest.raises(OSError):
        state.load_chat_reactions()

    assert len(state._chat_reactions) == 1
    assert state._chat_reactions[0]["id"] == "existing-r1"


@pytest.mark.asyncio
async def test_load_missing_file_sets_empty(tmp_path, monkeypatch):
    """Missing chat_reactions.json (first run) correctly sets empty list."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    state._chat_reactions = [
        {
            "id": "stale",
            "slot_key": "x",
            "mid": "m-x",
            "emoji": "👍",
            "reacted_at": "t",
        }
    ]
    state.load_chat_reactions()
    assert state._chat_reactions == []


@pytest.mark.asyncio
async def test_load_malformed_entry_dropped_but_preserved_on_save(tmp_path, monkeypatch):
    """A record with an invalid emoji is dropped from the active list but
    round-tripped back to disk on the next save (mirrors chat_pins)."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "good-1",
        "slot_key": "slot-1",
        "mid": "m-good",
        "emoji": "👍",
        "reacted_at": "2026-01-01T00:00:00+00:00",
    }
    bad = {**good, "id": "bad-1", "mid": "m-bad", "emoji": "not-a-real-emoji"}
    (tmp_path / "chat_reactions.json").write_text(_json.dumps([good, bad]), encoding="utf-8")

    state = _make_state(tmp_path)
    state.load_chat_reactions()
    assert [r["id"] for r in state._chat_reactions] == ["good-1"]

    state.save_chat_reactions()
    saved = _json.loads((tmp_path / "chat_reactions.json").read_text(encoding="utf-8"))
    assert {r["id"] for r in saved} == {"good-1", "bad-1"}


@pytest.mark.asyncio
async def test_load_oversized_slot_key_dropped_but_preserved(tmp_path, monkeypatch):
    """A record whose slot_key exceeds the bound is not loaded active (so a
    hand-edit cannot reintroduce an unbounded retained field) but is kept
    verbatim for round-trip, like any other malformed record."""
    from kiro_crew.dashboard import state as state_module
    from kiro_crew.dashboard.chat_reactions import _MAX_SLOT_KEY_CHARS

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "good-1",
        "slot_key": "slot-1",
        "mid": "m-good",
        "emoji": "👍",
        "reacted_at": "2026-01-01T00:00:00+00:00",
    }
    bad = {**good, "id": "bad-slot", "slot_key": "s" * (_MAX_SLOT_KEY_CHARS + 1)}
    (tmp_path / "chat_reactions.json").write_text(_json.dumps([good, bad]), encoding="utf-8")

    state = _make_state(tmp_path)
    state.load_chat_reactions()
    assert [r["id"] for r in state._chat_reactions] == ["good-1"]

    state.save_chat_reactions()
    saved = _json.loads((tmp_path / "chat_reactions.json").read_text(encoding="utf-8"))
    assert {r["id"] for r in saved} == {"good-1", "bad-slot"}


@pytest.mark.asyncio
async def test_load_non_hashable_emoji_does_not_crash_and_is_preserved(tmp_path, monkeypatch):
    """A hand-edited record with a non-string ``emoji`` (``[]``) must not raise
    a TypeError from the ``in _VALID_EMOJI`` membership test — the whole gateway
    would fail to start. It is dropped to the preserved-unparsed partition and
    round-tripped back on save, like any other malformed record."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "good-1",
        "slot_key": "slot-1",
        "mid": "m-good",
        "emoji": "👍",
        "reacted_at": "2026-01-01T00:00:00+00:00",
    }
    bad = {**good, "id": "bad-list", "mid": "m-bad", "emoji": []}
    (tmp_path / "chat_reactions.json").write_text(_json.dumps([good, bad]), encoding="utf-8")

    state = _make_state(tmp_path)
    state.load_chat_reactions()  # must not raise
    assert [r["id"] for r in state._chat_reactions] == ["good-1"]

    state.save_chat_reactions()
    saved = _json.loads((tmp_path / "chat_reactions.json").read_text(encoding="utf-8"))
    assert {r["id"] for r in saved} == {"good-1", "bad-list"}


def _make_state_with_broadcast_spy(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws_owners = MagicMock()
    return state


@pytest.mark.asyncio
async def test_broadcast_fires_after_create(tmp_path):
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-bcast-create", "mid": "m-bc-create1", "emoji": "👍"},
        )
        assert resp.status == 201
    state.broadcast_ws_owners.assert_called_once_with(
        "reactions_changed", {"slot_key": "slot-bcast-create"}
    )


@pytest.mark.asyncio
async def test_broadcast_fires_after_delete(tmp_path):
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-bcast-del", "mid": "m-bc-del1", "emoji": "✅"},
        )
        assert create_resp.status == 201
        state.broadcast_ws_owners.reset_mock()

        del_resp = await client.delete(
            "/api/chat/reactions?slot=slot-bcast-del&mid=m-bc-del1&emoji=✅"
        )
        assert del_resp.status == 200
    state.broadcast_ws_owners.assert_called_once_with(
        "reactions_changed", {"slot_key": "slot-bcast-del"}
    )


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_create_persist_failure(tmp_path, monkeypatch):
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        monkeypatch.setattr(state, "save_chat_reactions", _raise_os_error)
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-bcast-fail-c", "mid": "m-bc-fail-c1", "emoji": "👍"},
        )
        assert resp.status == 500
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_delete_persist_failure(tmp_path, monkeypatch):
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-bcast-fail-d", "mid": "m-bc-fail-d1", "emoji": "⭐"},
        )
        assert create_resp.status == 201
        state.broadcast_ws_owners.reset_mock()

        monkeypatch.setattr(state, "save_chat_reactions", _raise_os_error)
        del_resp = await client.delete(
            "/api/chat/reactions?slot=slot-bcast-fail-d&mid=m-bc-fail-d1&emoji=⭐"
        )
        assert del_resp.status == 500
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_validation_failure(tmp_path):
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post("/api/chat/reactions", json={"emoji": "👍"})
        assert resp.status == 400
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_payload_contains_only_slot_key(tmp_path):
    """No message/emoji content crosses the WebSocket — slot_key only."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-payload-check", "mid": "m-payload-chk1", "emoji": "👀"},
        )
        assert resp.status == 201
    call_args = state.broadcast_ws_owners.call_args
    assert call_args is not None
    msg_type, payload = call_args[0]
    assert msg_type == "reactions_changed"
    assert set(payload.keys()) == {"slot_key"}
