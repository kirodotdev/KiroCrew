"""Chat pin identity and ownership under concurrency and slot reuse.

Covers a slot deleted or replaced while a pin is being created, ``mid``-based pin
identity, record-level ``origin_app`` ownership and idempotent creates across apps
that reuse a slot. The route contract, persistence and slot isolation are in
``test_dashboard_chat_pins.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_chat_pins_helpers import _client, _make_app, _make_state, _pin

from kiro_crew.dashboard import chat_pins as chat_pins_module
from kiro_crew.dashboard.chat_pins import _MAX_PINS_PER_SLOT

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
async def test_slot_reuse_cross_app_delete_by_query_denial(tmp_path, monkeypatch):
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
    audit = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_pins.sel", lambda: audit)

    async with _client(tmp_path, state=state, app_name="app-b") as client:
        resp = await client.delete("/api/chat/pins/by-query?slot=slot-reuse&mid=m-old-app-a-two")
        assert resp.status == 404
        assert (await resp.json())["code"] == "pin_not_found"
        assert len(state._chat_pins) == 1
    denied = [
        call.kwargs
        for call in audit.log_api_access.call_args_list
        if call.kwargs.get("source") == "pin_record_ownership"
    ]
    assert len(denied) == 1
    assert denied[0]["caller"] == "app-b"


@pytest.mark.asyncio
async def test_own_app_create_list_delete_success(tmp_path):
    """Full CRUD cycle works for an app operating on its own pins."""
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-own", app="app-x")

    async with _client(tmp_path, state=state, app_name="app-x") as client:
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
    """The per-slot pin limit counts only the CALLER's own records.

    A foreign record occupying (slot, mid) does not satisfy idempotency for a
    different caller, and foreign records do not count against this caller's
    quota (they are invisible to it via list/delete). The limit still fires
    once the caller's OWN pins reach the cap.
    """
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-limit", app="app-new")
    # Fill slot to capacity with FOREIGN pins (app-old). These are invisible to
    # app-new and must NOT count against app-new's quota.
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
        # Target the same mid as filler-0000 — a foreign record exists but is
        # not app-new's, so this is a fresh creation, not idempotent, and the
        # foreign records do not exhaust app-new's quota.
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
        assert resp.status == 201, await resp.json()
        assert (await resp.json())["origin_app"] == "app-new"


@pytest.mark.asyncio
async def test_slot_pin_limit_counts_only_callers_own_records(tmp_path, monkeypatch):
    """Once the CALLER's own pins reach the cap, further pins are rejected —
    foreign-origin records in the same slot neither help nor hurt the count."""
    monkeypatch.setattr(chat_pins_module, "_MAX_PINS_PER_SLOT", 2)
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-limit", app="app-new")
    # Foreign filler (does not count) + app-new at its own cap (does count).
    state._chat_pins.extend(
        [
            _pin("foreign-0", "slot-limit", "ts-f0", origin_app="app-old"),
            _pin("mine-0", "slot-limit", "ts-m0", origin_app="app-new"),
            _pin("mine-1", "slot-limit", "ts-m1", origin_app="app-new"),
        ]
    )

    async with _client(tmp_path, state=state, app_name="app-new") as client:
        resp = await client.post(
            "/api/chat/pins",
            json={
                "slot_key": "slot-limit",
                "mid": "m-over-cap-app",
                "message_ts": "ts-over",
                "role": "user",
                "preview": "over cap",
            },
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "pin_limit_reached"
