"""Tests for chat message pin API endpoints.

Uses ``async with _client()`` inside each test rather than an async-gen fixture:
the CI-pinned ``pytest-asyncio==0.20.3`` is incompatible with the pinned
``pytest==8.4.1`` for async fixtures (see test_denied_commands_api.py docstring).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import chat_pins as chat_pins_module
from kiro_crew.dashboard.chat_pins import (
    _MAX_PINS_PER_SLOT,
    api_chat_pins_create,
    api_chat_pins_delete,
    api_chat_pins_delete_by_query,
    api_chat_pins_list,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _raise_os_error():
    """Stub for save_chat_pins that always raises."""
    raise OSError("disk full")


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/pins", api_chat_pins_list)
    app.router.add_post("/api/chat/pins", api_chat_pins_create)
    app.router.add_delete("/api/chat/pins/by-query", api_chat_pins_delete_by_query)
    app.router.add_delete("/api/chat/pins/{id}", api_chat_pins_delete)
    return app


def _client(tmp_path, *, state=None, app_name: str = "") -> TestClient:
    state = state or _make_state(tmp_path)
    app = _make_app(state)
    if app_name:

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_as_app)
    return TestClient(TestServer(app))


# ── Create ──


@pytest.mark.asyncio
async def test_create_pin(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-abc123def456",
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
                "preview": "Hello world",
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["slot_key"] == "slot-abc"
        assert data["mid"] == "m-abc123def456"
        assert data["message_ts"] == "2026-01-01T00:00:00Z"
        assert data["role"] == "user"
        assert data["preview"] == "Hello world"
        assert len(data["id"]) == 12
        assert "pinned_at" in data


@pytest.mark.asyncio
async def test_create_pin_idempotent(tmp_path):
    async with _client(tmp_path) as client:
        body = {
            "slot_key": "slot-abc",
            "mid": "m-idem123456",
            "message_ts": "2026-01-01T00:00:00Z",
            "role": "assistant",
            "preview": "Some text",
        }
        resp1 = await client.post("/api/chat/pins", json=body)
        assert resp1.status == 201
        pin1 = await resp1.json()

        resp2 = await client.post("/api/chat/pins", json=body)
        assert resp2.status == 200
        pin2 = await resp2.json()
        assert pin1["id"] == pin2["id"]


@pytest.mark.asyncio
async def test_create_pin_truncates_preview(tmp_path):
    async with _client(tmp_path) as client:
        long_preview = "x" * 500
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-trunc1234567",
                "message_ts": "2026-01-01T12:00:00Z",
                "role": "user",
                "preview": long_preview,
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert len(data["preview"]) == 200


@pytest.mark.asyncio
async def test_create_pin_rejects_oversized_preview_before_redaction(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-oversized12345",
                "message_ts": "ts-oversized",
                "role": "assistant",
                "preview": "x" * (chat_pins_module._MAX_PREVIEW_INPUT_CHARS + 1),
            },
        )
        assert resp.status == 413
        assert (await resp.json())["code"] == "preview_too_large"


@pytest.mark.asyncio
async def test_create_pin_rejects_oversized_message_ts_without_mutation(tmp_path):
    state = _make_state(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-longts1234567",
                "message_ts": "x" * (chat_pins_module._MAX_MESSAGE_TS_CHARS + 1),
                "role": "assistant",
                "preview": "text",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "message_ts_too_large"
        assert state._chat_pins == []


@pytest.mark.asyncio
async def test_create_pin_rejects_invalid_role_without_mutation(tmp_path):
    state = _make_state(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-badrole1234567",
                "message_ts": "ts-invalid-role",
                "role": "system",
                "preview": "text",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_role"
        assert state._chat_pins == []


@pytest.mark.asyncio
async def test_create_pin_rejects_body_over_shared_limit(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-oversizedbody",
                "message_ts": "ts-oversized-body",
                "preview": "x" * (64 * 1024),
            },
        )
        assert resp.status == 413
        assert (await resp.json())["code"] == "payload_too_large"


@pytest.mark.asyncio
async def test_create_pin_enforces_per_slot_limit_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_pins_module, "_MAX_PINS_PER_SLOT", 2)
    state = _make_state(tmp_path)
    state._chat_pins = [
        {
            "id": f"pin-{idx}",
            "slot_key": "slot-abc",
            "mid": f"m-existing-{idx}",
            "message_ts": f"ts-{idx}",
            "role": "assistant",
            "preview": "existing",
            "pinned_at": "2026-01-01T00:00:00+00:00",
        }
        for idx in range(2)
    ]
    async with _client(tmp_path, state=state) as client:
        duplicate = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-existing-0",
                "message_ts": "ts-0",
                "role": "assistant",
                "preview": "existing",
            },
        )
        assert duplicate.status == 200
        assert (await duplicate.json())["id"] == "pin-0"

        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "mid": "m-new-one-here",
                "message_ts": "ts-new",
                "role": "assistant",
                "preview": "new",
            },
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "pin_limit_reached"
        assert [pin["mid"] for pin in state._chat_pins] == ["m-existing-0", "m-existing-1"]


@pytest.mark.asyncio
async def test_create_pin_missing_slot_key(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "mid": "m-noslotkeytest",
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
            },
        )
        assert resp.status == 400
        data = await resp.json()
        assert "required" in data["error"]


@pytest.mark.asyncio
async def test_create_pin_missing_mid(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
            },
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "missing_required_fields"


# ── List ──


@pytest.mark.asyncio
async def test_list_pins_empty(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.get("/api/chat/pins?slot=slot-empty")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"pins": []}


@pytest.mark.asyncio
async def test_list_pins_filtered_by_slot(tmp_path):
    async with _client(tmp_path) as client:
        await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-1",
                "mid": "m-slot1-mid1234",
                "message_ts": "ts1",
                "role": "user",
                "preview": "a",
            },
        )
        await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-2",
                "mid": "m-slot2-mid1234",
                "message_ts": "ts2",
                "role": "assistant",
                "preview": "b",
            },
        )
        resp = await client.get("/api/chat/pins?slot=slot-1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["pins"]) == 1
        assert data["pins"][0]["slot_key"] == "slot-1"


@pytest.mark.asyncio
async def test_list_requires_slot_query_param(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.get("/api/chat/pins")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"


# ── Delete by ID ──


@pytest.mark.asyncio
async def test_delete_pin_by_id(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-1",
                "mid": "m-del-by-id1234",
                "message_ts": "ts1",
                "role": "user",
                "preview": "a",
            },
        )
        pin = await resp.json()
        del_resp = await client.delete(f"/api/chat/pins/{pin['id']}")
        assert del_resp.status == 200
        data = await del_resp.json()
        assert data == {"ok": True}

        # Verify gone
        list_resp = await client.get("/api/chat/pins?slot=slot-1")
        list_data = await list_resp.json()
        assert len(list_data["pins"]) == 0


@pytest.mark.asyncio
async def test_delete_pin_unknown_id(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.delete("/api/chat/pins/nonexistent1")
        assert resp.status == 404


# ── Persistence ──


@pytest.mark.asyncio
async def test_persistence_roundtrip(tmp_path, monkeypatch):
    """Pins survive save + fresh load."""
    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    state._chat_pins = [
        {
            "id": "abc123def456",
            "slot_key": "slot-1",
            "mid": "m-persist-round",
            "message_ts": "2026-01-01T00:00:00Z",
            "role": "user",
            "preview": "test",
            "pinned_at": "2026-01-01T00:00:01Z",
        }
    ]
    state.save_chat_pins()

    # Fresh load
    state2 = _make_state(tmp_path)
    state2.load_chat_pins()
    assert len(state2._chat_pins) == 1
    assert state2._chat_pins[0]["id"] == "abc123def456"
    assert state2._chat_pins[0]["mid"] == "m-persist-round"


@pytest.mark.asyncio
async def test_corrupt_file_tolerance(tmp_path, monkeypatch):
    """Corrupt JSON file results in empty list, not a crash."""
    from kiro_crew.dashboard import state as state_module

    # state.py binds config_dir by direct import, so patch the module-level
    # name it actually calls (patching kiro_crew.config.loader is a no-op).
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    (tmp_path / "chat_pins.json").write_text("NOT VALID JSON {{{", encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert state._chat_pins == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "null",
        '"a string"',
        '{"not": "a list"}',
        "42",
    ],
)
async def test_load_ignores_non_list_json(tmp_path, monkeypatch, content):
    """Valid JSON that is not a list (null, object, scalar) is ignored on
    load -- assigning it verbatim would make every pin API 500 after restart."""
    from kiro_crew.dashboard import state as state_module

    # state.py binds config_dir by direct import, so patch the module-level
    # name it actually calls (patching kiro_crew.config.loader is a no-op).
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    (tmp_path / "chat_pins.json").write_text(content, encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert state._chat_pins == []


@pytest.mark.asyncio
async def test_load_drops_malformed_records_keeps_valid(tmp_path, monkeypatch):
    """Non-dict entries and records missing hard-indexed string fields
    (id/slot_key and at least one of mid/message_ts) are dropped on load."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "goodpin000001",
        "slot_key": "slot-1",
        "mid": "m-good-pin-load",
        "message_ts": "ts-1",
        "role": "user",
        "preview": "keep me",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    good_legacy = {
        "id": "legacypin0001",
        "slot_key": "slot-1",
        "message_ts": "ts-legacy",
        "role": "user",
        "preview": "legacy no mid",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    bad = [
        "bad",  # non-dict entry
        {"slot_key": "s", "message_ts": "t"},  # missing id
        {"id": "", "slot_key": "s", "message_ts": "t"},  # empty id
        {"id": 42, "slot_key": "s", "message_ts": "t", "preview": "text"},  # non-string id
        {"id": "pin-null", "slot_key": "s", "mid": "m-x", "message_ts": "t", "preview": None},
        {"id": "pin-missing", "slot_key": "s", "mid": "m-y"},  # missing preview
        {"id": "pin-no-id", "slot_key": "s"},  # no mid or message_ts
        None,
    ]
    (tmp_path / "chat_pins.json").write_text(
        _json.dumps([good, good_legacy, *bad]), encoding="utf-8"
    )
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert len(state._chat_pins) == 2
    assert state._chat_pins[0] == good
    assert state._chat_pins[1] == good_legacy


# ── Persist failure handling ──


@pytest.mark.asyncio
async def test_create_pin_persist_failure_returns_500_and_rolls_back(tmp_path, monkeypatch):
    """POST returns 500 with code=persist_failed and rolls back in-memory on save failure."""
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-fail",
                "mid": "m-persist-fail1",
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
                "preview": "should fail",
            },
        )
        assert resp.status == 500
        data = await resp.json()
        assert data["code"] == "persist_failed"
        assert "error" in data
        # In-memory state rolled back
        assert len(state._chat_pins) == 0


@pytest.mark.asyncio
async def test_delete_by_id_persist_failure_returns_500_and_rolls_back(tmp_path, monkeypatch):
    """DELETE by id returns 500 and re-inserts pin on save failure."""
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        # Create a pin first (save works normally here)
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-x",
                "mid": "m-del-persist-f",
                "message_ts": "ts-1",
                "role": "user",
                "preview": "hi",
            },
        )
        assert resp.status == 201
        pin = await resp.json()
        assert len(state._chat_pins) == 1

        # Now break save
        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        del_resp = await client.delete(f"/api/chat/pins/{pin['id']}")
        assert del_resp.status == 500
        data = await del_resp.json()
        assert data["code"] == "persist_failed"
        # Pin restored in memory
        assert len(state._chat_pins) == 1
        assert state._chat_pins[0]["id"] == pin["id"]


@pytest.mark.asyncio
async def test_create_pin_redacts_credentials_in_preview(tmp_path):
    """A preview containing a credential is redacted before storage and listing."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-r",
                "mid": "m-redact-cred12",
                "message_ts": "ts-r",
                "role": "assistant",
                "preview": f"use key {secret} to auth",
            },
        )
        assert resp.status == 201
        created = await resp.json()
        assert secret not in created["preview"]
        # Stored value is redacted too (what save_chat_pins persists)
        assert secret not in state._chat_pins[0]["preview"]
        # And the list response
        list_resp = await client.get("/api/chat/pins?slot=slot-r")
        listed = await list_resp.json()
        assert secret not in listed["pins"][0]["preview"]


@pytest.mark.asyncio
async def test_create_pin_redacts_credential_straddling_truncation_boundary(tmp_path):
    """A credential straddling the 200-char truncation boundary must not
    survive as an unrecognized fragment: redaction runs BEFORE truncation."""
    secret = "AKIAIOSFODNN7EXAMPLE"  # 20 chars
    preview = "x" * 181 + secret + " trailing text beyond the cap"
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-b",
                "mid": "m-boundary-cred",
                "message_ts": "ts-b",
                "role": "user",
                "preview": preview,
            },
        )
        assert resp.status == 201
        created = await resp.json()
        assert secret not in created["preview"]
        assert secret[:12] not in created["preview"]
        assert len(created["preview"]) <= 200
        assert secret not in state._chat_pins[0]["preview"]
        assert secret[:12] not in state._chat_pins[0]["preview"]


@pytest.mark.asyncio
async def test_create_pin_non_string_fields_return_400(tmp_path):
    """Non-string slot_key/mid must yield a structured 400, not a 500."""
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={"slot_key": 1, "mid": "m-valid", "message_ts": "x"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "missing_required_fields"


@pytest.mark.asyncio
async def test_create_pin_non_object_body_returns_400(tmp_path):
    """A JSON array body must yield a structured 400, not an AttributeError 500."""
    async with _client(tmp_path) as client:
        resp = await client.post("/api/chat/pins", json=["not", "an", "object"])
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_create_pin_body_error_with_none_text(tmp_path, monkeypatch):
    """body_error.text being None must not raise TypeError in json.loads."""
    from kiro_crew.dashboard import chat_pins as _pins_mod

    async def _patched_read(request, max_bytes=65536):
        # Simulate a response with text=None (edge case from aiohttp)
        err = web.Response(status=400, body=None)
        assert err.text is None  # confirm precondition
        return None, err

    monkeypatch.setattr(_pins_mod, "read_bounded_json", _patched_read)
    async with _client(tmp_path) as client:
        resp = await client.post("/api/chat/pins", json={"anything": True})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_create_pin_body_error_with_malformed_text(tmp_path, monkeypatch):
    """body_error.text containing non-JSON must not raise in the error rewrite path."""
    from kiro_crew.dashboard import chat_pins as _pins_mod

    async def _patched_read(request, max_bytes=65536):
        # Simulate a response whose text is not valid JSON
        err = web.Response(status=400, text="not json at all")
        return None, err

    monkeypatch.setattr(_pins_mod, "read_bounded_json", _patched_read)
    async with _client(tmp_path) as client:
        resp = await client.post("/api/chat/pins", json={"anything": True})
        assert resp.status == 400
        # The raw response is forwarded (no rewrite since it's not body_not_object)
        text = await resp.text()
        assert text == "not json at all"


@pytest.mark.asyncio
async def test_list_re_redacts_stale_unredacted_previews_from_disk(tmp_path):
    """A pre-existing pin with an unredacted credential in chat_pins.json is
    redacted at the list output boundary (stored text is never trusted)."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        # Simulate a stale on-disk record that predates redaction
        state._chat_pins.append(
            {
                "id": "stalepin00001",
                "slot_key": "slot-s",
                "message_ts": "ts-s",
                "role": "assistant",
                "preview": f"legacy key {secret} here",
                "pinned_at": "2026-01-01T00:00:00+00:00",
            }
        )
        resp = await client.get("/api/chat/pins?slot=slot-s")
        assert resp.status == 200
        listed = await resp.json()
        assert secret not in listed["pins"][0]["preview"]
        assert "legacy key" in listed["pins"][0]["preview"]


@pytest.mark.asyncio
async def test_duplicate_create_re_redacts_stale_unredacted_preview(tmp_path):
    """A duplicate POST for an already-pinned message takes the idempotent
    `existing` branch -- that response path must re-redact too, or a stale
    unredacted credential on disk is returned verbatim."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        # Simulate a stale on-disk record that predates redaction
        state._chat_pins.append(
            {
                "id": "stalepin00002",
                "slot_key": "slot-d",
                "mid": "m-stale-dup-pin",
                "message_ts": "ts-d",
                "role": "assistant",
                "preview": f"legacy key {secret} here",
                "pinned_at": "2026-01-01T00:00:00+00:00",
            }
        )
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-d",
                "mid": "m-stale-dup-pin",
                "message_ts": "ts-d",
                "role": "assistant",
                "preview": "",
            },
        )
        assert resp.status == 200  # idempotent duplicate, not 201
        returned = await resp.json()
        assert returned["id"] == "stalepin00002"
        assert secret not in returned["preview"]
        assert "legacy key" in returned["preview"]


# ── App-token slot isolation ──


def _pin(pin_id: str, slot_key: str, message_ts: str, *, origin_app: str = "") -> dict:
    return {
        "id": pin_id,
        "slot_key": slot_key,
        "mid": f"m-{pin_id}",
        "message_ts": message_ts,
        "role": "assistant",
        "preview": f"preview for {slot_key}",
        "pinned_at": "2026-01-01T00:00:00+00:00",
        "origin_app": origin_app,
    }


@pytest.mark.asyncio
async def test_app_list_requires_owned_slot(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    state.get_or_create_slot("slot-other", app="app-b")
    state.get_or_create_slot("slot-dashboard")
    state._chat_pins.extend(
        [
            _pin("pin-own", "slot-own", "ts-own", origin_app="app-a"),
            _pin("pin-other", "slot-other", "ts-other", origin_app="app-b"),
            _pin("pin-dashboard", "slot-dashboard", "ts-dashboard"),
        ]
    )

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        missing = await client.get("/api/chat/pins")
        assert missing.status == 400
        assert (await missing.json())["code"] == "missing_query_params"

        own = await client.get("/api/chat/pins?slot=slot-own")
        assert own.status == 200
        assert [pin["id"] for pin in (await own.json())["pins"]] == ["pin-own"]

        foreign = await client.get("/api/chat/pins?slot=slot-other")
        assert foreign.status == 404
        assert (await foreign.json())["code"] == "slot_not_found"


@pytest.mark.asyncio
async def test_app_create_requires_owned_slot(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    state.get_or_create_slot("slot-other", app="app-b")

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        own = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-own",
                "mid": "m-app-own-12345",
                "message_ts": "ts-own",
                "role": "assistant",
                "preview": "owned",
            },
        )
        assert own.status == 201

        foreign = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-other",
                "mid": "m-app-foreign12",
                "message_ts": "ts-other",
                "role": "assistant",
                "preview": "foreign",
            },
        )
        assert foreign.status == 404
        assert (await foreign.json())["code"] == "slot_not_found"
        assert [pin["slot_key"] for pin in state._chat_pins] == ["slot-own"]


@pytest.mark.asyncio
async def test_app_delete_by_id_requires_owned_slot(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    state.get_or_create_slot("slot-other", app="app-b")
    state._chat_pins.extend(
        [
            _pin("pin-own", "slot-own", "ts-own", origin_app="app-a"),
            _pin("pin-other", "slot-other", "ts-other", origin_app="app-b"),
        ]
    )

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        foreign = await client.delete("/api/chat/pins/pin-other")
        assert foreign.status == 404
        assert (await foreign.json())["code"] == "pin_not_found"
        assert {pin["id"] for pin in state._chat_pins} == {"pin-own", "pin-other"}

        own = await client.delete("/api/chat/pins/pin-own")
        assert own.status == 200
        assert [pin["id"] for pin in state._chat_pins] == ["pin-other"]


@pytest.mark.asyncio
async def test_owned_app_pin_operations_are_sel_audited(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_pins.sel", lambda: audit)

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        listed = await client.get("/api/chat/pins?slot=slot-own")
        assert listed.status == 200
        created = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-own",
                "mid": "m-sel-audit1234",
                "message_ts": "ts-own",
                "role": "assistant",
                "preview": "owned",
            },
        )
        assert created.status == 201
        deleted = await client.delete(f"/api/chat/pins/{(await created.json())['id']}")
        assert deleted.status == 200

    allowed = [
        call.kwargs
        for call in audit.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "allowed"
    ]
    # create fires _authorize_app_slot twice: pre-lock + in-lock re-check
    assert [event["operation"] for event in allowed] == [
        "chat.pins_list",
        "chat.pins_create",
        "chat.pins_create",
        "chat.pins_delete",
    ]
    assert all(event["caller"] == "app-a" for event in allowed)
    assert all(event["resources"] == "slot=slot-own" for event in allowed)


@pytest.mark.asyncio
async def test_remove_chat_pins_for_slots_persists_filtered_list(tmp_path):
    state = _make_state(tmp_path)
    state._chat_pins = [
        _pin("pin-a", "slot-a", "ts-a"),
        _pin("pin-b", "slot-b", "ts-b"),
    ]

    removed = await state.remove_chat_pins_for_slots({"slot-a"})

    assert removed == 1
    assert [pin["id"] for pin in state._chat_pins] == ["pin-b"]
    state.load_chat_pins()
    assert [pin["id"] for pin in state._chat_pins] == ["pin-b"]


@pytest.mark.asyncio
async def test_remove_chat_pins_for_slots_rolls_back_on_persist_failure(tmp_path):
    state = _make_state(tmp_path)
    original = [_pin("pin-a", "slot-a", "ts-a")]
    state._chat_pins = original.copy()
    state.save_chat_pins = _raise_os_error

    with pytest.raises(OSError, match="disk full"):
        await state.remove_chat_pins_for_slots({"slot-a"})

    assert state._chat_pins == original


# ── Finding 1: pinned_at validation on load ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pinned_at_value,should_survive",
    [
        ("2026-01-01T00:00:00+00:00", True),  # valid ISO string
        ({"time": "2026-01-01"}, False),  # object value
        ("", False),  # empty string
        (None, False),  # null / missing type
        (42, False),  # non-string numeric
        ([], False),  # array
        (True, False),  # boolean
    ],
)
async def test_load_drops_pins_with_malformed_pinned_at(
    tmp_path, monkeypatch, pinned_at_value, should_survive
):
    """Pins with non-string, empty, or object-valued pinned_at are dropped on
    load — they must not crash the list endpoint's sort-by-pinned_at."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    pin = {
        "id": "testpin123456",
        "slot_key": "slot-1",
        "message_ts": "ts-1",
        "role": "user",
        "preview": "text",
        "pinned_at": pinned_at_value,
    }
    (tmp_path / "chat_pins.json").write_text(_json.dumps([pin]), encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_chat_pins()
    if should_survive:
        assert len(state._chat_pins) == 1
        assert state._chat_pins[0]["id"] == "testpin123456"
    else:
        assert state._chat_pins == []


@pytest.mark.asyncio
async def test_load_preserves_valid_pins_alongside_malformed_pinned_at(tmp_path, monkeypatch):
    """Valid pins survive alongside dropped pins with bad pinned_at."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "goodpin000001",
        "slot_key": "slot-1",
        "message_ts": "ts-1",
        "role": "user",
        "preview": "keep",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    bad_object = {
        "id": "badpin0000001",
        "slot_key": "slot-1",
        "message_ts": "ts-2",
        "role": "user",
        "preview": "drop me",
        "pinned_at": {"nested": "object"},
    }
    bad_empty = {
        "id": "badpin0000002",
        "slot_key": "slot-1",
        "message_ts": "ts-3",
        "role": "user",
        "preview": "drop me too",
        "pinned_at": "",
    }
    (tmp_path / "chat_pins.json").write_text(
        _json.dumps([good, bad_object, bad_empty]), encoding="utf-8"
    )
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert len(state._chat_pins) == 1
    assert state._chat_pins[0]["id"] == "goodpin000001"


# ── Finding 3: query-form DELETE route registration and contract ──


@pytest.mark.asyncio
async def test_delete_by_query_removes_pin(tmp_path):
    """DELETE /api/chat/pins/by-query?slot=x&mid=y removes the pin."""
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-q",
                "mid": "m-query-del1234",
                "message_ts": "ts-q",
                "role": "user",
                "preview": "query delete",
            },
        )
        assert resp.status == 201

        del_resp = await client.delete("/api/chat/pins/by-query?slot=slot-q&mid=m-query-del1234")
        assert del_resp.status == 200
        data = await del_resp.json()
        assert data == {"ok": True}

        # Verify gone
        list_resp = await client.get("/api/chat/pins?slot=slot-q")
        list_data = await list_resp.json()
        assert len(list_data["pins"]) == 0


@pytest.mark.asyncio
async def test_delete_by_query_missing_params(tmp_path):
    """DELETE /api/chat/pins/by-query without required params returns 400."""
    async with _client(tmp_path) as client:
        # Missing both
        resp = await client.delete("/api/chat/pins/by-query")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"

        # Missing mid and message_ts
        resp = await client.delete("/api/chat/pins/by-query?slot=x")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"

        # Missing slot
        resp = await client.delete("/api/chat/pins/by-query?mid=y")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"


@pytest.mark.asyncio
async def test_delete_by_query_not_found(tmp_path):
    """DELETE /api/chat/pins/by-query for non-existent pin returns 404."""
    async with _client(tmp_path) as client:
        resp = await client.delete("/api/chat/pins/by-query?slot=no-slot&mid=no-mid")
        assert resp.status == 404
        assert (await resp.json())["code"] == "pin_not_found"


@pytest.mark.asyncio
async def test_delete_by_query_persist_failure_rolls_back(tmp_path, monkeypatch):
    """DELETE /api/chat/pins/by-query returns 500 and re-inserts on save failure."""
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-pf",
                "mid": "m-persist-fail2",
                "message_ts": "ts-pf",
                "role": "user",
                "preview": "persist fail",
            },
        )
        assert resp.status == 201
        assert len(state._chat_pins) == 1

        monkeypatch.setattr(state, "save_chat_pins", _raise_os_error)
        del_resp = await client.delete("/api/chat/pins/by-query?slot=slot-pf&mid=m-persist-fail2")
        assert del_resp.status == 500
        assert (await del_resp.json())["code"] == "persist_failed"
        assert len(state._chat_pins) == 1


@pytest.mark.asyncio
async def test_delete_by_query_app_isolation(tmp_path):
    """App tokens can only query-delete pins in their own slots."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    state.get_or_create_slot("slot-other", app="app-b")
    state._chat_pins.extend(
        [
            _pin("pin-own", "slot-own", "ts-own", origin_app="app-a"),
            _pin("pin-other", "slot-other", "ts-other", origin_app="app-b"),
        ]
    )

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        # Cannot delete in foreign slot
        foreign = await client.delete("/api/chat/pins/by-query?slot=slot-other&mid=m-pin-other")
        assert foreign.status == 404
        assert {pin["id"] for pin in state._chat_pins} == {"pin-own", "pin-other"}

        # Can delete in own slot
        own = await client.delete("/api/chat/pins/by-query?slot=slot-own&mid=m-pin-own")
        assert own.status == 200
        assert [pin["id"] for pin in state._chat_pins] == ["pin-other"]


# ── Finding 1: Race condition — slot deleted concurrently ──


@pytest.mark.asyncio
async def test_create_pin_returns_404_when_slot_deleted_concurrently(tmp_path):
    """Creating a pin for a slot that was deleted concurrently returns 404 for app callers."""
    state = _make_state(tmp_path)
    # Slot existed but was deleted — app caller gets 404 inside pin lock
    # (simulated by never creating the slot for app-a)
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-deleted",
                "mid": "m-ghost-pin1234",
                "message_ts": "ts-ghost",
                "role": "user",
                "preview": "should not persist",
            },
        )
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "slot_not_found"
        # No durable pin data created
        assert state._chat_pins == []


@pytest.mark.asyncio
async def test_create_pin_succeeds_when_slot_exists(tmp_path):
    """Creating a pin for an existing slot succeeds normally (app caller)."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-alive", app="app-a")
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-alive",
                "mid": "m-alive-pin1234",
                "message_ts": "ts-alive",
                "role": "user",
                "preview": "should persist",
            },
        )
        assert resp.status == 201
        assert len(state._chat_pins) == 1


# ── Finding 2: mid-based identity — same timestamp distinct mids ──


@pytest.mark.asyncio
async def test_same_timestamp_distinct_mids_both_pin(tmp_path):
    """Two messages with identical timestamps but different mids can both be pinned."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-ts")
    async with _client(tmp_path, state=state) as client:
        resp1 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-ts",
                "mid": "m-msg-alpha1234",
                "message_ts": "2026-08-01T10:00:00Z",
                "role": "user",
                "preview": "first message",
            },
        )
        assert resp1.status == 201

        resp2 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-ts",
                "mid": "m-msg-beta12345",
                "message_ts": "2026-08-01T10:00:00Z",
                "role": "assistant",
                "preview": "second message",
            },
        )
        assert resp2.status == 201

        pin1 = await resp1.json()
        pin2 = await resp2.json()
        assert pin1["id"] != pin2["id"]
        assert pin1["mid"] != pin2["mid"]
        assert pin1["message_ts"] == pin2["message_ts"]
        assert len(state._chat_pins) == 2


@pytest.mark.asyncio
async def test_idempotency_by_mid_not_message_ts(tmp_path):
    """Idempotency is keyed on mid, not message_ts."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-idem")
    async with _client(tmp_path, state=state) as client:
        # First pin
        resp1 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-idem",
                "mid": "m-idem-unique12",
                "message_ts": "ts-shared",
                "role": "user",
                "preview": "msg A",
            },
        )
        assert resp1.status == 201

        # Same mid = idempotent
        resp2 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-idem",
                "mid": "m-idem-unique12",
                "message_ts": "ts-shared",
                "role": "user",
                "preview": "msg A updated",
            },
        )
        assert resp2.status == 200
        assert (await resp2.json())["id"] == (await resp1.json())["id"]

        # Different mid, same message_ts = new pin
        resp3 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-idem",
                "mid": "m-idem-other123",
                "message_ts": "ts-shared",
                "role": "assistant",
                "preview": "msg B",
            },
        )
        assert resp3.status == 201
        assert len(state._chat_pins) == 2


@pytest.mark.asyncio
async def test_delete_correctness_by_mid(tmp_path):
    """Deleting by mid removes the correct pin when multiple share a timestamp."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-dc")
    async with _client(tmp_path, state=state) as client:
        await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-dc",
                "mid": "m-dc-alpha12345",
                "message_ts": "ts-same",
                "role": "user",
                "preview": "alpha",
            },
        )
        await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-dc",
                "mid": "m-dc-beta123456",
                "message_ts": "ts-same",
                "role": "assistant",
                "preview": "beta",
            },
        )
        assert len(state._chat_pins) == 2

        # Delete by mid
        del_resp = await client.delete("/api/chat/pins/by-query?slot=slot-dc&mid=m-dc-alpha12345")
        assert del_resp.status == 200
        assert len(state._chat_pins) == 1
        assert state._chat_pins[0]["mid"] == "m-dc-beta123456"


@pytest.mark.asyncio
async def test_create_pin_rejects_missing_mid(tmp_path):
    """A pin request without mid returns a machine-readable validation error."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-no-mid")
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-no-mid",
                "message_ts": "ts-1",
                "role": "user",
                "preview": "no mid",
            },
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "missing_required_fields"
        assert "mid" in data["error"]


@pytest.mark.asyncio
async def test_load_preserves_legacy_pins_without_mid(tmp_path, monkeypatch):
    """Legacy pins (pre-mid era) without the mid field are preserved on load."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    legacy_pin = {
        "id": "legacypin12345",
        "slot_key": "slot-1",
        "message_ts": "ts-legacy",
        "role": "user",
        "preview": "old pin no mid",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    (tmp_path / "chat_pins.json").write_text(_json.dumps([legacy_pin]), encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert len(state._chat_pins) == 1
    assert state._chat_pins[0]["id"] == "legacypin12345"
    assert "mid" not in state._chat_pins[0]


@pytest.mark.asyncio
async def test_delete_by_query_falls_back_to_message_ts_for_legacy(tmp_path):
    """Legacy pins without mid can be deleted by message_ts fallback."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-leg")
    state._chat_pins.append(
        {
            "id": "legdel0000001",
            "slot_key": "slot-leg",
            "message_ts": "ts-legacy-del",
            "role": "user",
            "preview": "legacy",
            "pinned_at": "2026-01-01T00:00:00+00:00",
        }
    )
    async with _client(tmp_path, state=state) as client:
        resp = await client.delete("/api/chat/pins/by-query?slot=slot-leg&message_ts=ts-legacy-del")
        assert resp.status == 200
        assert state._chat_pins == []


# ── Concurrency regression: slot replaced between pre-check and lock ──


@pytest.mark.asyncio
async def test_create_pin_race_slot_replaced_by_another_app(tmp_path):
    """App A passes pre-lock authorization, but before it acquires the lock
    the slot is deleted and replaced by App B with the same key.  App A must
    fail with an authorization error and never persist a pin into App B's slot.

    This is a deterministic behavioral test for the TOCTOU race between the
    pre-lock _authorize_app_slot and the in-lock re-check.
    """
    state = _make_state(tmp_path)
    # Initially, App A owns the slot.
    state.get_or_create_slot("slot-contested", app="app-a")

    app_a_app = _make_app(state)
    app_b_app = _make_app(state)

    @web.middleware
    async def _as_app_a(request, handler):
        request["app"] = "app-a"
        return await handler(request)

    @web.middleware
    async def _as_app_b(request, handler):
        request["app"] = "app-b"
        return await handler(request)

    app_a_app.middlewares.append(_as_app_a)
    app_b_app.middlewares.append(_as_app_b)

    # Step 1: App A's request passes the pre-lock authorization (slot owned
    # by app-a).  We simulate the race by replacing the slot BEFORE App A's
    # create call reaches the lock — since both happen in the same event loop
    # tick in test, we replace the slot synchronously first and then call the
    # handler.  The pre-lock check will pass because at that moment the slot
    # still belongs to app-a; we need to replace it BETWEEN the pre-lock check
    # and the in-lock re-check.  To do that deterministically, monkeypatch
    # the lock's __aenter__ to perform the replacement just before yielding.

    original_lock = state._chat_pins_lock
    replace_done = False

    class _RacingLock:
        """Wraps the real lock but replaces the slot on first acquire."""

        async def __aenter__(self):
            nonlocal replace_done
            if not replace_done:
                replace_done = True
                # Simulate concurrent slot replacement by App B
                state._slots.pop("slot-contested", None)
                state.get_or_create_slot("slot-contested", app="app-b")
            await original_lock.__aenter__()
            return self

        async def __aexit__(self, *args):
            await original_lock.__aexit__(*args)

    state._chat_pins_lock = _RacingLock()

    async with TestClient(TestServer(app_a_app)) as client_a:
        resp = await client_a.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-contested",
                "mid": "m-race-pin-aaaa",
                "message_ts": "ts-race",
                "role": "user",
                "preview": "should never persist",
            },
        )
        # App A must be denied (404 — indistinguishable from "not found" per CWE-204)
        assert resp.status == 404
        data = await resp.json()
        assert data["code"] == "slot_not_found"

    # No pin was persisted for either app
    assert state._chat_pins == []

    # Restore real lock for App B to confirm it CAN pin in its own slot
    state._chat_pins_lock = original_lock
    async with TestClient(TestServer(app_b_app)) as client_b:
        resp = await client_b.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-contested",
                "mid": "m-appb-legit-pin",
                "message_ts": "ts-b",
                "role": "assistant",
                "preview": "App B's legitimate pin",
            },
        )
        assert resp.status == 201
        pin = await resp.json()
        assert pin["slot_key"] == "slot-contested"
        assert pin["mid"] == "m-appb-legit-pin"

    # Only App B's pin exists
    assert len(state._chat_pins) == 1
    assert state._chat_pins[0]["mid"] == "m-appb-legit-pin"


# ── Finding 1: Record-level app ownership (origin_app) ──


@pytest.mark.asyncio
async def test_create_pin_stores_origin_app(tmp_path):
    """Pins created by app callers persist their originating app identity."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-app", app="app-a")
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-app",
                "mid": "m-origin-app123",
                "message_ts": "ts-1",
                "role": "user",
                "preview": "app-a pin",
            },
        )
        assert resp.status == 201
        assert state._chat_pins[0]["origin_app"] == "app-a"


@pytest.mark.asyncio
async def test_create_pin_dashboard_stores_empty_origin_app(tmp_path):
    """Pins created by dashboard callers store empty origin_app."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-dash")
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-dash",
                "mid": "m-dashboard-pin",
                "message_ts": "ts-1",
                "role": "user",
                "preview": "dashboard pin",
            },
        )
        assert resp.status == 201
        assert state._chat_pins[0]["origin_app"] == ""


@pytest.mark.asyncio
async def test_slot_reuse_cross_app_list_exclusion(tmp_path):
    """After slot deletion and recreation for a different app, the new app
    cannot see pins created by the previous app owner (IDOR prevention)."""
    state = _make_state(tmp_path)
    # App A owns the slot and creates a pin
    state.get_or_create_slot("slot-reuse", app="app-a")
    state._chat_pins.append(
        {
            "id": "pin-from-app-a",
            "slot_key": "slot-reuse",
            "mid": "m-app-a-secret",
            "message_ts": "ts-secret",
            "role": "user",
            "preview": "secret data from app-a",
            "pinned_at": "2026-01-01T00:00:00+00:00",
            "origin_app": "app-a",
        }
    )
    # Simulate slot deletion and recreation for App B
    state._slots.pop("slot-reuse", None)
    state.get_or_create_slot("slot-reuse", app="app-b")

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp = await client.get("/api/chat/pins?slot=slot-reuse")
        assert resp.status == 200
        data = await resp.json()
        # App B sees zero pins — App A's data is NOT exposed
        assert data["pins"] == []


@pytest.mark.asyncio
async def test_slot_reuse_cross_app_delete_denial(tmp_path):
    """After slot recreation, new app cannot delete pins from the old app."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-reuse", app="app-b")
    state._chat_pins.append(
        {
            "id": "pin-old-app-a",
            "slot_key": "slot-reuse",
            "mid": "m-old-app-a-pin",
            "message_ts": "ts-old",
            "role": "user",
            "preview": "old app-a data",
            "pinned_at": "2026-01-01T00:00:00+00:00",
            "origin_app": "app-a",
        }
    )

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        # Delete by ID fails
        resp = await client.delete("/api/chat/pins/pin-old-app-a")
        assert resp.status == 404
        assert (await resp.json())["code"] == "pin_not_found"
        # Pin still exists
        assert len(state._chat_pins) == 1


@pytest.mark.asyncio
async def test_slot_reuse_cross_app_delete_by_query_denial(tmp_path):
    """After slot recreation, new app cannot delete old app pins by query."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-reuse", app="app-b")
    state._chat_pins.append(
        {
            "id": "pin-old-app-a2",
            "slot_key": "slot-reuse",
            "mid": "m-old-app-a-two",
            "message_ts": "ts-old2",
            "role": "user",
            "preview": "old data 2",
            "pinned_at": "2026-01-01T00:00:00+00:00",
            "origin_app": "app-a",
        }
    )

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp = await client.delete("/api/chat/pins/by-query?slot=slot-reuse&mid=m-old-app-a-two")
        assert resp.status == 404
        assert (await resp.json())["code"] == "pin_not_found"
        assert len(state._chat_pins) == 1


@pytest.mark.asyncio
async def test_own_app_create_list_delete_success(tmp_path):
    """Full CRUD cycle works for an app operating on its own pins."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-x")

    async with _client(tmp_path, state=state, app_name="app-x") as client:
        # Create
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-own",
                "mid": "m-app-x-pin-ok",
                "message_ts": "ts-x",
                "role": "assistant",
                "preview": "app-x content",
            },
        )
        assert resp.status == 201
        pin = await resp.json()
        assert pin["mid"] == "m-app-x-pin-ok"

        # List — sees own pin
        list_resp = await client.get("/api/chat/pins?slot=slot-own")
        assert list_resp.status == 200
        pins = (await list_resp.json())["pins"]
        assert len(pins) == 1
        assert pins[0]["mid"] == "m-app-x-pin-ok"

        # Delete by ID — succeeds
        del_resp = await client.delete(f"/api/chat/pins/{pin['id']}")
        assert del_resp.status == 200
        assert state._chat_pins == []


@pytest.mark.asyncio
async def test_dashboard_sees_all_pins_in_owned_slot(tmp_path):
    """Dashboard callers (no app claim) see ALL pins in a slot regardless of origin_app."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-all")
    state._chat_pins.extend(
        [
            {
                "id": "pin-from-app-a",
                "slot_key": "slot-all",
                "mid": "m-app-a-vis",
                "message_ts": "ts-a",
                "role": "user",
                "preview": "from app-a",
                "pinned_at": "2026-01-01T00:00:00+00:00",
                "origin_app": "app-a",
            },
            {
                "id": "pin-from-dash",
                "slot_key": "slot-all",
                "mid": "m-dash-vis",
                "message_ts": "ts-d",
                "role": "assistant",
                "preview": "from dashboard",
                "pinned_at": "2026-01-01T00:00:01+00:00",
                "origin_app": "",
            },
            {
                "id": "pin-legacy",
                "slot_key": "slot-all",
                "mid": "m-legacy-vis",
                "message_ts": "ts-l",
                "role": "user",
                "preview": "legacy no origin_app",
                "pinned_at": "2026-01-01T00:00:02+00:00",
            },
        ]
    )

    async with _client(tmp_path, state=state) as client:
        resp = await client.get("/api/chat/pins?slot=slot-all")
        assert resp.status == 200
        pins = (await resp.json())["pins"]
        assert len(pins) == 3


@pytest.mark.asyncio
async def test_legacy_pins_without_origin_app_hidden_from_app_callers(tmp_path):
    """Legacy pins (no origin_app field) are not visible to app callers."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-leg", app="app-a")
    state._chat_pins.append(
        {
            "id": "pin-legacy-nofield",
            "slot_key": "slot-leg",
            "mid": "m-legacy-hidden",
            "message_ts": "ts-leg",
            "role": "user",
            "preview": "legacy content",
            "pinned_at": "2026-01-01T00:00:00+00:00",
            # No origin_app field at all — legacy
        }
    )

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.get("/api/chat/pins?slot=slot-leg")
        assert resp.status == 200
        pins = (await resp.json())["pins"]
        # Legacy pin has origin_app="" (missing defaults to ""), app-a != ""
        assert pins == []

        # Cannot delete legacy pin either
        del_resp = await client.delete("/api/chat/pins/pin-legacy-nofield")
        assert del_resp.status == 404


@pytest.mark.asyncio
async def test_record_ownership_denial_is_sel_audited(tmp_path, monkeypatch):
    """Record-level ownership denials are logged via SEL."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-audit", app="app-b")
    state._chat_pins.append(
        {
            "id": "pin-other-app",
            "slot_key": "slot-audit",
            "mid": "m-other-app-pin",
            "message_ts": "ts-aud",
            "role": "user",
            "preview": "other app data",
            "pinned_at": "2026-01-01T00:00:00+00:00",
            "origin_app": "app-a",
        }
    )
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_pins.sel", lambda: audit)

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        # Attempt delete — should be denied at record ownership level
        resp = await client.delete("/api/chat/pins/pin-other-app")
        assert resp.status == 404

    denied_calls = [
        call.kwargs
        for call in audit.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
        and call.kwargs.get("source") == "pin_record_ownership"
    ]
    assert len(denied_calls) == 1
    assert denied_calls[0]["caller"] == "app-b"
    assert "pin=pin-other-app" in denied_calls[0]["resources"]


# ── Idempotent create isolation (slot reuse + cross-app same-mid) ──


@pytest.mark.asyncio
async def test_idempotent_create_does_not_leak_foreign_app_record(tmp_path):
    """When App A already pinned mid=X, App B POSTing same (slot, mid) must NOT
    receive App A's record (preview/metadata leak, CWE-639). Instead, App B
    gets a new caller-owned record with its OWN preview/pinned_at."""
    state = _make_state(tmp_path)
    # Slot is owned by App B (simulates slot recycling scenario)
    state.get_or_create_slot("slot-reuse", app="app-b")
    # Leftover pin from App A (orphan after slot delete/recreate)
    state._chat_pins.append(
        {
            "id": "pin-app-a-legacy",
            "slot_key": "slot-reuse",
            "mid": "m-shared-mid-001",
            "message_ts": "ts-app-a",
            "role": "assistant",
            "preview": "secret preview from app-a",
            "pinned_at": "2025-06-01T00:00:00+00:00",
            "origin_app": "app-a",
        }
    )

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-reuse",
                "mid": "m-shared-mid-001",
                "message_ts": "ts-app-b",
                "role": "user",
                "preview": "app-b new preview",
            },
        )
        # App B creates a new record — status 201, not idempotent 200
        assert resp.status == 201
        data = await resp.json()
        # Record is owned by App B
        assert data["origin_app"] == "app-b"
        assert data["id"] != "pin-app-a-legacy"
        # No foreign metadata leaks
        assert data["preview"] == "app-b new preview"
        assert data["role"] == "user"
        assert data["message_ts"] == "ts-app-b"
        # App A's preview is not exposed
        assert "secret preview from app-a" not in str(data)


@pytest.mark.asyncio
async def test_idempotent_create_does_not_leak_dashboard_record_to_app(tmp_path):
    """Dashboard-created pin (origin_app='') must not be returned to an app
    caller that POSTs the same (slot, mid)."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-shared", app="app-x")
    # Legacy/dashboard-created pin (no origin_app or empty)
    state._chat_pins.append(
        {
            "id": "pin-dashboard-legacy",
            "slot_key": "slot-shared",
            "mid": "m-same-mid-dash",
            "message_ts": "ts-dashboard",
            "role": "assistant",
            "preview": "dashboard secret preview",
            "pinned_at": "2025-01-01T00:00:00+00:00",
            "origin_app": "",
        }
    )

    async with _client(tmp_path, state=state, app_name="app-x") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-shared",
                "mid": "m-same-mid-dash",
                "message_ts": "ts-app-x",
                "role": "user",
                "preview": "app-x preview",
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["origin_app"] == "app-x"
        assert data["id"] != "pin-dashboard-legacy"
        # No dashboard metadata leaked
        assert "dashboard secret preview" not in str(data)


@pytest.mark.asyncio
async def test_idempotent_create_does_not_leak_app_record_to_dashboard(tmp_path):
    """App-created pin must not be returned to a dashboard caller POSTing the
    same (slot, mid). Dashboard should get its own record."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-dash")
    # App pin exists
    state._chat_pins.append(
        {
            "id": "pin-app-only",
            "slot_key": "slot-dash",
            "mid": "m-same-mid-app",
            "message_ts": "ts-app",
            "role": "assistant",
            "preview": "app secret preview",
            "pinned_at": "2025-03-01T00:00:00+00:00",
            "origin_app": "app-z",
        }
    )

    # Dashboard caller (no app middleware)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-dash",
                "mid": "m-same-mid-app",
                "message_ts": "ts-dash",
                "role": "user",
                "preview": "dashboard preview",
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["origin_app"] == ""
        assert data["id"] != "pin-app-only"
        assert "app secret preview" not in str(data)


@pytest.mark.asyncio
async def test_idempotent_create_same_caller_returns_existing(tmp_path):
    """Same caller (app-b) posting same (slot, mid) twice gets idempotent 200."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-idem-app", app="app-b")

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp1 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-idem-app",
                "mid": "m-idem-app-same",
                "message_ts": "ts-1",
                "role": "assistant",
                "preview": "first call preview",
            },
        )
        assert resp1.status == 201
        pin1 = await resp1.json()

        resp2 = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-idem-app",
                "mid": "m-idem-app-same",
                "message_ts": "ts-2-ignored",
                "role": "user",
                "preview": "second call different preview",
            },
        )
        assert resp2.status == 200  # idempotent
        pin2 = await resp2.json()
        assert pin2["id"] == pin1["id"]
        assert pin2["origin_app"] == "app-b"
        # Original metadata preserved (idempotent, not upsert)
        assert pin2["preview"] == "first call preview"


@pytest.mark.asyncio
async def test_slot_reuse_same_mid_respects_pin_limit(tmp_path):
    """Even when a foreign record occupies (slot, mid), the new caller-owned
    record still counts against the slot-wide pin limit."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-limit", app="app-new")
    # Fill slot to capacity with foreign pins (app-old)
    for i in range(_MAX_PINS_PER_SLOT):
        state._chat_pins.append(
            {
                "id": f"filler-{i:04d}",
                "slot_key": "slot-limit",
                "mid": f"m-filler-{i:04d}",
                "message_ts": f"ts-{i}",
                "role": "user",
                "preview": f"filler {i}",
                "pinned_at": "2025-01-01T00:00:00+00:00",
                "origin_app": "app-old",
            }
        )

    async with _client(tmp_path, state=state, app_name="app-new") as client:
        # Target the same mid as filler-0000 — foreign record exists but
        # slot is at capacity, so creation should be rejected.
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-limit",
                "mid": "m-filler-0000",
                "message_ts": "ts-new",
                "role": "user",
                "preview": "new caller preview",
            },
        )
        assert resp.status == 409
        data = await resp.json()
        assert data["code"] == "pin_limit_reached"


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
    monkeypatch.undo()
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    # In-memory state preserved — now save it (simulating a subsequent mutation)
    state.save_chat_pins()

    # Verify file still has the valid pin
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
