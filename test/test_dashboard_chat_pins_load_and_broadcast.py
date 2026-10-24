"""Chat pin load failures and the ``pins_changed`` broadcast.

Covers a transient read error on load, which keeps the in-memory pins, and the
broadcast that fires after every successful pin mutation and never after a failed
one. The route contract, persistence and slot isolation are in
``test_dashboard_chat_pins.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from dashboard_chat_pins_helpers import _client, _make_state, _raise_os_error

# ── Finding 2 (GPT 5.6): Transient I/O error must NOT replace valid in-memory state ──


@pytest.mark.asyncio
async def test_load_transient_io_error_preserves_existing_state(tmp_path, monkeypatch):
    """Transient read I/O error during load_chat_pins must NOT replace valid
    in-memory pins with an empty list — it must re-raise so the caller knows
    the load failed and the previous in-memory state remains intact."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    # Write a valid file so path.exists() returns True
    import json as _json

    valid_pin = {
        "id": "existing-pin-1",
        "slot_key": "slot-1",
        "mid": "m-existing-valid",
        "message_ts": "ts-1",
        "role": "user",
        "preview": "valid pin content",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    (tmp_path / "chat_pins.json").write_text(_json.dumps([valid_pin]), encoding="utf-8")

    state = _make_state(tmp_path)
    # Pre-populate in-memory state with the valid pin (simulating a prior good load)
    state._chat_pins = [valid_pin.copy()]

    # Simulate a transient read I/O error (e.g. permission/sharing violation).
    # Patch read_text rather than chmod(0o000): Windows ignores POSIX permission
    # bits for read access, so a chmod'd file still opens there and no OSError is
    # raised — the test must inject the error portably to exercise the re-raise.
    def _failing_read(*args, **kwargs):
        raise PermissionError("transient disk permission error")

    monkeypatch.setattr(type(tmp_path / "chat_pins.json"), "read_text", _failing_read)

    with pytest.raises(OSError):
        state.load_chat_pins()

    # Critical: in-memory state was NOT clobbered
    assert len(state._chat_pins) == 1
    assert state._chat_pins[0]["id"] == "existing-pin-1"


@pytest.mark.asyncio
async def test_load_transient_io_error_no_destructive_followon(tmp_path, monkeypatch):
    """After a transient I/O failure on load, a subsequent pin mutation must
    NOT overwrite the persisted file with stale empty state — the in-memory
    pins remain from the prior good load, and save persists them correctly."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    existing_pin = {
        "id": "protected-pin1",
        "slot_key": "slot-a",
        "mid": "m-protected-1234",
        "message_ts": "ts-p",
        "role": "assistant",
        "preview": "important pinned content",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    (tmp_path / "chat_pins.json").write_text(_json.dumps([existing_pin]), encoding="utf-8")

    state = _make_state(tmp_path)
    # Simulate prior good load
    state._chat_pins = [existing_pin.copy()]

    # Simulate transient I/O error on reload attempt
    call_count = [0]

    def _failing_read(*args, **kwargs):
        call_count[0] += 1
        raise PermissionError("transient disk permission error")

    monkeypatch.setattr(type(tmp_path / "chat_pins.json"), "read_text", _failing_read)

    with pytest.raises(OSError):
        state.load_chat_pins()

    # Restore read ability for save verification
    # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo that lifts the read_text failure before the saved file is read back, moved verbatim; tracked for a de-flake pass
    monkeypatch.undo()
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    # In-memory state preserved — now save it (simulating a subsequent mutation)
    state.save_chat_pins()

    saved = _json.loads((tmp_path / "chat_pins.json").read_text(encoding="utf-8"))
    assert len(saved) == 1
    assert saved[0]["id"] == "protected-pin1"


@pytest.mark.asyncio
async def test_load_missing_file_sets_empty(tmp_path, monkeypatch):
    """Missing chat_pins.json (first run) correctly sets empty list — this is
    the normal path, NOT a transient error."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    # Pre-populate to verify it gets replaced on missing file
    state._chat_pins = [
        {
            "id": "stale",
            "slot_key": "x",
            "mid": "m-x",
            "message_ts": "t",
            "preview": "p",
            "pinned_at": "t",
        }
    ]
    state.load_chat_pins()
    assert state._chat_pins == []


# ── Broadcast (pins_changed) ──

# Helper: inject a MagicMock for broadcast_ws_owners so tests can assert calls.


def _make_state_with_broadcast_spy(tmp_path):
    """_make_state() variant that replaces broadcast_ws_owners with a MagicMock."""
    state = _make_state(tmp_path)
    state.broadcast_ws_owners = MagicMock()
    return state


@pytest.mark.asyncio
async def test_broadcast_fires_after_create_pin(tmp_path):
    """broadcast_ws_owners is called with pins_changed and the correct slot_key
    after a successful pin creation."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-create",
                "mid": "m-bc-create1",
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
                "preview": "broadcast test",
            },
        )
        assert resp.status == 201
    state.broadcast_ws_owners.assert_called_once_with(
        "pins_changed", {"slot_key": "slot-bcast-create"}
    )


@pytest.mark.asyncio
async def test_broadcast_fires_after_delete_by_id(tmp_path):
    """broadcast_ws_owners is called with pins_changed after a successful delete-by-id."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-del-id",
                "mid": "m-bc-del-id1",
                "message_ts": "ts-1",
                "role": "user",
                "preview": "to delete",
            },
        )
        assert create_resp.status == 201
        pin = await create_resp.json()
        state.broadcast_ws_owners.reset_mock()

        del_resp = await client.delete(f"/api/chat/pins/{pin['id']}")
        assert del_resp.status == 200
    state.broadcast_ws_owners.assert_called_once_with(
        "pins_changed", {"slot_key": "slot-bcast-del-id"}
    )


@pytest.mark.asyncio
async def test_broadcast_fires_after_delete_by_query(tmp_path):
    """broadcast_ws_owners is called with pins_changed after a successful delete-by-query."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-del-q",
                "mid": "m-bc-del-q1",
                "message_ts": "ts-2",
                "role": "assistant",
                "preview": "to delete by query",
            },
        )
        assert create_resp.status == 201
        pin = await create_resp.json()
        state.broadcast_ws_owners.reset_mock()

        del_resp = await client.delete(
            f"/api/chat/pins/by-query?slot=slot-bcast-del-q&mid={pin['mid']}"
        )
        assert del_resp.status == 200
    state.broadcast_ws_owners.assert_called_once_with(
        "pins_changed", {"slot_key": "slot-bcast-del-q"}
    )


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_create_persist_failure(tmp_path, monkeypatch):
    """broadcast_ws_owners must NOT be called when persistence fails on create
    (rollback path — the in-memory state was reverted)."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-fail-c",
                "mid": "m-bc-fail-c1",
                "message_ts": "ts-3",
                "role": "user",
                "preview": "should fail",
            },
        )
        assert resp.status == 500
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_delete_persist_failure(tmp_path, monkeypatch):
    """broadcast_ws_owners must NOT be called when persistence fails on delete-by-id
    (rollback path — the pin was re-inserted)."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-fail-d",
                "mid": "m-bc-fail-d1",
                "message_ts": "ts-4",
                "role": "user",
                "preview": "persist fail delete",
            },
        )
        assert create_resp.status == 201
        pin = await create_resp.json()
        state.broadcast_ws_owners.reset_mock()

        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        del_resp = await client.delete(f"/api/chat/pins/{pin['id']}")
        assert del_resp.status == 500
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_query_delete_persist_failure(tmp_path, monkeypatch):
    """broadcast_ws_owners must NOT be called when persistence fails on delete-by-query
    (rollback path — the pin was re-inserted)."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        create_resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-bcast-fail-q",
                "mid": "m-bc-fail-q1",
                "message_ts": "ts-5",
                "role": "assistant",
                "preview": "persist fail query",
            },
        )
        assert create_resp.status == 201
        pin = await create_resp.json()
        state.broadcast_ws_owners.reset_mock()

        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        del_resp = await client.delete(
            f"/api/chat/pins/by-query?slot=slot-bcast-fail-q&mid={pin['mid']}"
        )
        assert del_resp.status == 500
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_does_not_fire_on_validation_failure(tmp_path):
    """broadcast_ws_owners must NOT be called when the request is rejected for
    validation reasons (missing required fields, etc.)."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        # Missing both slot_key and mid
        resp = await client.post("/api/chat/pins", json={"preview": "oops"})
        assert resp.status == 400
    state.broadcast_ws_owners.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_payload_contains_only_slot_key(tmp_path):
    """The broadcast payload must contain slot_key and nothing else (no pin
    content) so no sensitive material crosses the WebSocket to any listener."""
    state = _make_state_with_broadcast_spy(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-payload-check",
                "mid": "m-payload-chk1",
                "message_ts": "ts-pc",
                "role": "user",
                "preview": "SECRET content must not appear in broadcast",
            },
        )
        assert resp.status == 201
    call_args = state.broadcast_ws_owners.call_args
    assert call_args is not None
    msg_type, payload = call_args[0]
    assert msg_type == "pins_changed"
    assert set(payload.keys()) == {"slot_key"}
    assert "preview" not in payload
    assert "content" not in payload
