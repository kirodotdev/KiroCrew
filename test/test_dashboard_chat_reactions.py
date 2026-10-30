"""Tests for message reaction API endpoints.

Uses ``async with _client()`` inside each test, following the suite's
convention (see ``test_dashboard_chat_pins.py``). Reaction identity is
``(slot_key, mid, emoji)``: create is idempotent on that triple, and the one
delete route removes by the same triple — there is no numeric pin-list
ordering or jump-navigation concern here, so the surface is much smaller
than chat_pins.
"""

from __future__ import annotations

import pytest
from dashboard_chat_reactions_helpers import (
    _client,
    _make_state,
    _raise_os_error,
    _reaction,
    _seed_reactions,
)

from kiro_crew.dashboard import chat_reactions as chat_reactions_module

# ── Create ──


@pytest.mark.asyncio
async def test_create_reaction(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={
                "slot_key": "slot-abc",
                "mid": "m-abc123def456",
                "emoji": "👍",
            },
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["slot_key"] == "slot-abc"
        assert data["mid"] == "m-abc123def456"
        assert data["emoji"] == "👍"
        assert len(data["id"]) == 12
        assert "reacted_at" in data
        # The record carries only identity fields: no message_ts or role is
        # stored, because nothing reads them back (reactionsFor keys on mid+emoji).
        assert "message_ts" not in data
        assert "role" not in data


@pytest.mark.asyncio
async def test_create_reaction_idempotent(tmp_path):
    async with _client(tmp_path) as client:
        body = {
            "slot_key": "slot-abc",
            "mid": "m-idem123456",
            "emoji": "✅",
        }
        resp1 = await client.post("/api/chat/reactions", json=body)
        assert resp1.status == 201
        r1 = await resp1.json()

        resp2 = await client.post("/api/chat/reactions", json=body)
        assert resp2.status == 200
        r2 = await resp2.json()
        assert r1["id"] == r2["id"]


@pytest.mark.asyncio
async def test_create_reaction_different_emoji_same_message_both_kept(tmp_path):
    """Two different emoji on the same (slot, mid) are independent records."""
    async with _client(tmp_path) as client:
        base = {
            "slot_key": "slot-abc",
            "mid": "m-twoemoji1234",
        }
        resp1 = await client.post("/api/chat/reactions", json={**base, "emoji": "👀"})
        resp2 = await client.post("/api/chat/reactions", json={**base, "emoji": "⭐"})
        assert resp1.status == 201
        assert resp2.status == 201
        listing = await (await client.get("/api/chat/reactions?slot=slot-abc")).json()
        assert {r["emoji"] for r in listing["reactions"]} == {"👀", "⭐"}


@pytest.mark.asyncio
async def test_create_reaction_rejects_invalid_emoji_without_mutation(tmp_path):
    state = _make_state(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={
                "slot_key": "slot-abc",
                "mid": "m-badeemoji1234",
                "emoji": "🙃",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_emoji"
        assert state._chat_reactions == []


@pytest.mark.asyncio
async def test_create_reaction_missing_slot_key(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"mid": "m-noslotkey123", "emoji": "👍"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "missing_required_fields"


@pytest.mark.asyncio
async def test_create_reaction_missing_mid(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-abc", "emoji": "👍"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["code"] == "missing_required_fields"


@pytest.mark.asyncio
async def test_create_reaction_rejects_oversized_slot_key_without_mutation(tmp_path):
    state = _make_state(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={
                "slot_key": "s" * (chat_reactions_module._MAX_SLOT_KEY_CHARS + 1),
                "mid": "m-okmid",
                "emoji": "👍",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "slot_key_too_large"
        assert state._chat_reactions == []


@pytest.mark.asyncio
async def test_create_reaction_rejects_oversized_mid_without_mutation(tmp_path):
    state = _make_state(tmp_path)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={
                "slot_key": "slot-abc",
                "mid": "x" * (chat_reactions_module._MAX_MID_CHARS + 1),
                "emoji": "👍",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "mid_too_large"
        assert state._chat_reactions == []


@pytest.mark.asyncio
async def test_create_reaction_enforces_per_slot_limit_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_reactions_module, "_MAX_REACTIONS_PER_SLOT", 2)
    state = _make_state(tmp_path)
    _seed_reactions(
        state,
        [_reaction(f"r-{idx}", "slot-abc", f"m-existing-{idx}", "👍") for idx in range(2)],
    )
    async with _client(tmp_path, state=state) as client:
        # Re-reacting with the same (slot, mid, emoji) stays idempotent even at the cap.
        duplicate = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-abc", "mid": "m-existing-0", "emoji": "👍"},
        )
        assert duplicate.status == 200
        assert (await duplicate.json())["id"] == "r-0"

        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-abc", "mid": "m-new-one", "emoji": "⭐"},
        )
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "reaction_limit_reached"
        assert body["limit"] == 2
        assert len(state._chat_reactions) == 2


@pytest.mark.asyncio
async def test_create_reaction_quota_is_scoped_per_origin_app(tmp_path, monkeypatch):
    """Mirrors the chat_pins origin-scoped quota test."""
    monkeypatch.setattr(chat_reactions_module, "_MAX_REACTIONS_PER_SLOT", 2)
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-shared", app="app-a")
    _seed_reactions(
        state,
        [
            _reaction(f"r-legacy-{idx}", "slot-shared", f"m-legacy-{idx}", "👍", origin_app="")
            for idx in range(2)
        ],
    )
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-shared", "mid": "m-app-a-1", "emoji": "✅"},
        )
        assert resp.status == 201, await resp.json()
        created = await resp.json()
        assert created["origin_app"] == "app-a"


# ── List ──


@pytest.mark.asyncio
async def test_list_reactions_empty(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.get("/api/chat/reactions?slot=slot-empty")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"reactions": []}


@pytest.mark.asyncio
async def test_list_reactions_filtered_by_slot(tmp_path):
    async with _client(tmp_path) as client:
        await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-1", "mid": "m-slot1-mid1", "emoji": "👍"},
        )
        await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-2", "mid": "m-slot2-mid1", "emoji": "✅"},
        )
        resp = await client.get("/api/chat/reactions?slot=slot-1")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["reactions"]) == 1
        assert data["reactions"][0]["slot_key"] == "slot-1"


@pytest.mark.asyncio
async def test_list_reactions_missing_slot_param(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.get("/api/chat/reactions")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"


# ── Delete ──


@pytest.mark.asyncio
async def test_delete_reaction_by_query(tmp_path):
    async with _client(tmp_path) as client:
        await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-abc", "mid": "m-todelete123", "emoji": "👍"},
        )
        resp = await client.delete(
            "/api/chat/reactions?slot=slot-abc&mid=m-todelete123&emoji=👍",
        )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True}
        listing = await (await client.get("/api/chat/reactions?slot=slot-abc")).json()
        assert listing["reactions"] == []


@pytest.mark.asyncio
async def test_delete_reaction_not_found(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.delete(
            "/api/chat/reactions?slot=slot-abc&mid=m-nonexistent&emoji=👍",
        )
        assert resp.status == 404
        assert (await resp.json())["code"] == "reaction_not_found"


@pytest.mark.asyncio
async def test_delete_reaction_missing_query_params(tmp_path):
    async with _client(tmp_path) as client:
        resp = await client.delete("/api/chat/reactions?slot=slot-abc")
        assert resp.status == 400
        assert (await resp.json())["code"] == "missing_query_params"


@pytest.mark.asyncio
async def test_delete_reaction_only_removes_matching_emoji(tmp_path):
    """Deleting one emoji on a message must not disturb a second emoji on it."""
    async with _client(tmp_path) as client:
        base = {"slot_key": "slot-abc", "mid": "m-multi123456"}
        await client.post("/api/chat/reactions", json={**base, "emoji": "👍"})
        await client.post("/api/chat/reactions", json={**base, "emoji": "⭐"})
        resp = await client.delete("/api/chat/reactions?slot=slot-abc&mid=m-multi123456&emoji=👍")
        assert resp.status == 200
        listing = await (await client.get("/api/chat/reactions?slot=slot-abc")).json()
        assert [r["emoji"] for r in listing["reactions"]] == ["⭐"]


# ── Persistence failure ──


@pytest.mark.asyncio
async def test_create_reaction_persist_failure_rolls_back(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    monkeypatch.setattr(state, "save_chat_reactions", _raise_os_error)
    async with _client(tmp_path, state=state) as client:
        resp = await client.post(
            "/api/chat/reactions",
            json={"slot_key": "slot-abc", "mid": "m-persistfail1", "emoji": "👍"},
        )
        assert resp.status == 500
        assert (await resp.json())["code"] == "persist_failed"
        assert state._chat_reactions == []


# ── App isolation ──


@pytest.mark.asyncio
async def test_app_cannot_list_reactions_for_unowned_slot(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-owned-by-b", app="app-b")
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.get("/api/chat/reactions?slot=slot-owned-by-b")
        assert resp.status == 404
        assert (await resp.json())["code"] == "slot_not_found"


@pytest.mark.asyncio
async def test_app_only_sees_own_origin_reactions(tmp_path):
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-shared", app="app-a")
    _seed_reactions(
        state,
        [
            _reaction("r-a", "slot-shared", "m-a", "👍", origin_app="app-a"),
            _reaction("r-dashboard", "slot-shared", "m-dash", "✅", origin_app=""),
        ],
    )
    async with _client(tmp_path, state=state, app_name="app-a") as client:
        resp = await client.get("/api/chat/reactions?slot=slot-shared")
        data = await resp.json()
        assert [r["id"] for r in data["reactions"]] == ["r-a"]


@pytest.mark.asyncio
async def test_dashboard_delete_does_not_strand_an_app_owned_duplicate(tmp_path):
    """A dashboard caller never sees or deletes an app's copy of the same emoji.

    Dashboard and app-a both react with the same emoji on the same message, so
    two records exist with identical (slot, mid, emoji) and different
    origin_app. The dashboard list must show only its own record, the dashboard
    delete must remove only its own, and after that delete the dashboard list
    must stay empty — the removed reaction cannot reappear from the surviving
    app-owned record.
    """
    state = _make_state(tmp_path)
    state.get_or_create_slot("slot-shared", app="app-a")
    _seed_reactions(
        state,
        [
            _reaction("r-dash", "slot-shared", "m-dup", "👍", origin_app=""),
            _reaction("r-app-a", "slot-shared", "m-dup", "👍", origin_app="app-a"),
        ],
    )
    # Dashboard caller (no app token): sees only its own record.
    async with _client(tmp_path, state=state) as client:
        listing = await (await client.get("/api/chat/reactions?slot=slot-shared")).json()
        assert [r["id"] for r in listing["reactions"]] == ["r-dash"]
        resp = await client.delete("/api/chat/reactions?slot=slot-shared&mid=m-dup&emoji=👍")
        assert resp.status == 200, await resp.json()
        after = await (await client.get("/api/chat/reactions?slot=slot-shared")).json()
        # The dashboard's reaction is gone and does NOT reappear.
        assert after["reactions"] == []
    # The app's own record survived the dashboard's delete.
    assert any(r["id"] == "r-app-a" for r in state._chat_reactions)
    assert not any(r["id"] == "r-dash" for r in state._chat_reactions)
