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
    api_chat_pins_create,
    api_chat_pins_delete,
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
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
                "preview": "Hello world",
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["slot_key"] == "slot-abc"
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
                "message_ts": "ts-new",
                "role": "assistant",
                "preview": "new",
            },
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "pin_limit_reached"
        assert [pin["message_ts"] for pin in state._chat_pins] == ["ts-0", "ts-1"]


@pytest.mark.asyncio
async def test_create_pin_missing_slot_key(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "message_ts": "2026-01-01T00:00:00Z",
                "role": "user",
            },
        )
        assert resp.status == 400
        data = await resp.json()
        assert "required" in data["error"]


@pytest.mark.asyncio
async def test_create_pin_missing_message_ts(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-abc",
                "role": "user",
            },
        )
        assert resp.status == 400


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
                "message_ts": "ts1",
                "role": "user",
                "preview": "a",
            },
        )
        await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-2",
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

    # state.py binds config_dir by direct import, so patch the module-level
    # name it actually calls (patching kiro_crew.config.loader is a no-op).
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    state = _make_state(tmp_path)
    state._chat_pins = [
        {
            "id": "abc123def456",
            "slot_key": "slot-1",
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
    (id/slot_key/message_ts) are dropped on load; valid records survive."""
    import json as _json

    from kiro_crew.dashboard import state as state_module

    # state.py binds config_dir by direct import, so patch the module-level
    # name it actually calls (patching kiro_crew.config.loader is a no-op).
    monkeypatch.setattr(state_module, "config_dir", lambda: tmp_path)

    good = {
        "id": "goodpin000001",
        "slot_key": "slot-1",
        "message_ts": "ts-1",
        "role": "user",
        "preview": "keep me",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }
    bad = [
        "bad",  # non-dict entry
        {"slot_key": "s", "message_ts": "t"},  # missing id
        {"id": "", "slot_key": "s", "message_ts": "t"},  # empty id
        {"id": 42, "slot_key": "s", "message_ts": "t", "preview": "text"},  # non-string id
        {"id": "pin-null", "slot_key": "s", "message_ts": "t", "preview": None},
        {"id": "pin-missing", "slot_key": "s", "message_ts": "t"},  # missing preview
        None,
    ]
    (tmp_path / "chat_pins.json").write_text(_json.dumps([good, *bad]), encoding="utf-8")
    state = _make_state(tmp_path)
    state.load_chat_pins()
    assert state._chat_pins == [good]


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
    survive as an unrecognized fragment: redaction runs BEFORE truncation, so
    truncating first (leaving e.g. a 19-char prefix of a 20-char AWS key that
    the redactor no longer matches) is a regression."""
    secret = "AKIAIOSFODNN7EXAMPLE"  # 20 chars
    # Start the secret at offset 181 so a truncate-first bug keeps chars
    # 181..199 -- a 19-char fragment that bypasses the credential pattern.
    preview = "x" * 181 + secret + " trailing text beyond the cap"
    async with _client(tmp_path) as client:
        state: DashboardState = client.app["state"]
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-b",
                "message_ts": "ts-b",
                "role": "user",
                "preview": preview,
            },
        )
        assert resp.status == 201
        created = await resp.json()
        assert secret not in created["preview"]
        # No partial fragment of the key either (any 12+ char substring)
        assert secret[:12] not in created["preview"]
        assert len(created["preview"]) <= 200
        # Stored value is safe too
        assert secret not in state._chat_pins[0]["preview"]
        assert secret[:12] not in state._chat_pins[0]["preview"]


@pytest.mark.asyncio
async def test_create_pin_non_string_fields_return_400(tmp_path):
    """Non-string slot_key/message_ts must yield a structured 400, not a 500."""
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/pins",
            json={"slot_key": 1, "message_ts": "x"},
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
                "message_ts": "ts-d",
                "role": "assistant",
                "preview": f"legacy key {secret} here",
                "pinned_at": "2026-01-01T00:00:00+00:00",
            }
        )
        resp = await client.post(
            "/api/chat/pins",
            json={"slot_key": "slot-d", "message_ts": "ts-d", "role": "assistant", "preview": ""},
        )
        assert resp.status == 200  # idempotent duplicate, not 201
        returned = await resp.json()
        assert returned["id"] == "stalepin00002"
        assert secret not in returned["preview"]
        assert "legacy key" in returned["preview"]


# ── App-token slot isolation ──


def _pin(pin_id: str, slot_key: str, message_ts: str) -> dict:
    return {
        "id": pin_id,
        "slot_key": slot_key,
        "message_ts": message_ts,
        "role": "assistant",
        "preview": f"preview for {slot_key}",
        "pinned_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_app_list_requires_owned_slot(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-a")
    state.get_or_create_slot("slot-other", app="app-b")
    state.get_or_create_slot("slot-dashboard")
    state._chat_pins.extend(
        [
            _pin("pin-own", "slot-own", "ts-own"),
            _pin("pin-other", "slot-other", "ts-other"),
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
            _pin("pin-own", "slot-own", "ts-own"),
            _pin("pin-other", "slot-other", "ts-other"),
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
    assert [event["operation"] for event in allowed] == [
        "chat.pins_list",
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
