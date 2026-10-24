"""Tests for chat message pin API endpoints.

Uses ``async with _client()`` inside each test rather than an async-gen fixture,
following the suite's convention for teardown that must await on the test's own loop.

The shared builders are in ``dashboard_chat_pins_helpers.py``; identity and ownership
under concurrency are in ``test_dashboard_chat_pins_ownership.py``, and a transient read
error on load and the ``pins_changed`` broadcast in
``test_dashboard_chat_pins_load_and_broadcast.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from dashboard_chat_pins_helpers import _client, _make_state, _pin, _raise_os_error

from kiro_crew.dashboard import chat_pins as chat_pins_module
from kiro_crew.dashboard.state import DashboardState

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
async def test_create_pin_quota_is_scoped_per_origin_app(tmp_path, monkeypatch):
    """One origin's pins filling a slot to the cap must not block another origin.

    The quota is scoped by (slot_key, origin_app) to match the idempotency
    lookup and api_chat_pins_list. An app-owned slot pre-filled to the cap with
    foreign-origin records (e.g. legacy pins carrying no origin_app) must still
    let the app create its own pin -- otherwise it gets a 409 for records it can
    neither see nor unpin.
    """
    monkeypatch.setattr(chat_pins_module, "_MAX_PINS_PER_SLOT", 2)
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-shared", app="app-a")
    # Slot already at the cap, but every record belongs to a DIFFERENT origin
    # (legacy blank origin_app here) that the caller cannot see or manage.
    state._chat_pins = [
        _pin(f"pin-legacy-{idx}", "slot-shared", f"ts-{idx}", origin_app="") for idx in range(2)
    ]

    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-shared",
                "mid": "m-app-a-first-1",
                "message_ts": "ts-app-a",
                "role": "assistant",
                "preview": "app-a pin",
            },
        )
        # Before the fix the global slot count (2) hits the cap and returns 409;
        # after the fix app-a has zero pins of its own so the pin is created.
        assert resp.status == 201, await resp.json()
        created = await resp.json()
        assert created["origin_app"] == "app-a"
        assert {p["id"] for p in state._chat_pins} == {
            "pin-legacy-0",
            "pin-legacy-1",
            created["id"],
        }


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


@pytest.mark.parametrize("query_field", ["mid", "message_ts"])
@pytest.mark.asyncio
async def test_delete_by_query_selects_callers_record_before_foreign_collision(
    tmp_path, query_field
):
    """A stale foreign record must not mask the caller's own matching pin."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-reused", app="app-b")
    foreign = _pin("foreign-pin", "slot-reused", "ts-shared", origin_app="app-a")
    own = _pin("own-pin", "slot-reused", "ts-shared", origin_app="app-b")
    if query_field == "mid":
        foreign["mid"] = own["mid"] = "m-shared"
        query = "mid=m-shared"
    else:
        foreign.pop("mid")
        own.pop("mid")
        query = "message_ts=ts-shared"
    state._chat_pins.extend([foreign, own])

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp = await client.delete(f"/api/chat/pins/by-query?slot=slot-reused&{query}")

    assert resp.status == 200
    assert [(pin["id"], pin["origin_app"]) for pin in state._chat_pins] == [
        ("foreign-pin", "app-a")
    ]
