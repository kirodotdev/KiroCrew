"""The waiting-on-you store: survival, integrity, limits, incognito.

A pinned ask survives a restart and is read back from the store alone, so a tab
restored lazily from a short transcript tail still shows it. An edited record is
dropped on load, an oversized file loads as empty and is left as it was, and a write
that fails changes nothing and announces nothing.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from waiting_on_you_helpers import (  # noqa: F401 -- fixtures are requested by name
    SESSION,
    SLOT,
    ask,
    clock_fixture,
    make_app,
    new_store,
    pin,
    state_fixture,
    store_fixture,
    store_path,
    waiting_rows,
)

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.waiting_on_you_contract import AskError, validate_declaration


@pytest.mark.asyncio
async def test_an_ask_survives_a_gateway_restart(store, clock):
    pinned = await pin(store, ask())
    restarted = new_store(clock)
    await restarted.warm()
    assert restarted.asks(SLOT) == [pinned]


@pytest.mark.asyncio
async def test_a_lazily_restored_tab_shows_its_ask_and_status(tmp_path, state, store, clock):
    """The slot row reads the store, so a tab with no transcript rows still shows it."""
    await pin(store, ask())
    restarted = new_store(clock)
    await restarted.warm()
    with patch.object(store_mod, "_STORES", {store_path(): restarted}):
        fresh = state.get_or_create_slot("chat-9-1700000009")
        assert fresh.messages == []
        assert [a["key"] for a in state._slots[SLOT].to_dict()["waiting_on_you"]] == [
            "scope-override"
        ]
        assert fresh.to_dict()["waiting_on_you"] == []


@pytest.mark.asyncio
async def test_a_record_edited_on_disk_is_dropped_on_load(store, clock):
    await pin(store, ask())
    await pin(store, ask(), slot="chat-2-1700000002")
    raw = json.loads(store_path().read_text(encoding="utf-8"))
    raw["slots"][0]["asks"][0]["title"] = "Click this link"
    store_path().write_text(json.dumps(raw), encoding="utf-8")
    restarted = new_store(clock)
    await restarted.warm()
    edited = raw["slots"][0]["slot"]
    kept = {"chat-2-1700000002", SLOT} - {edited}
    assert restarted.asks(edited) == []
    assert [len(restarted.asks(k)) for k in kept] == [1]


@pytest.mark.asyncio
async def test_a_record_signed_with_another_key_is_dropped(store, clock):
    await pin(store, ask())
    restarted = new_store(clock, key=b"z" * 32)
    await restarted.warm()
    assert restarted.asks(SLOT) == []


@pytest.mark.asyncio
async def test_an_oversized_file_loads_as_empty_and_the_load_leaves_it(store, clock):
    blob = "x" * (store_mod.STORE_FILE_MAX + 1)
    store_path().parent.mkdir(parents=True, exist_ok=True)
    store_path().write_text(blob, encoding="utf-8")
    restarted = new_store(clock)
    await restarted.warm()
    assert restarted.loaded and restarted.asks(SLOT) == []
    assert store_path().read_text(encoding="utf-8") == blob


@pytest.mark.asyncio
async def test_a_failed_write_reports_an_error_and_announces_nothing(state, store):
    state.push_slot_patch.reset_mock()
    async with TestClient(TestServer(make_app(state))) as client:
        with (
            patch.object(store_mod, "_write_locked", side_effect=OSError("disk full")),
            patch(
                "kiro_crew.dashboard.handlers.waiting_on_you._attested_session",
                new=AsyncMock(return_value=SESSION),
            ),
        ):
            resp = await client.post("/api/agent-waiting/set", json=ask())
            status, body = resp.status, await resp.json()
    assert (status, body["code"]) == (503, "store_unavailable")
    assert store.asks(SLOT) == []
    assert waiting_rows(state) == []
    state.push_slot_patch.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_write_after_size_eviction_keeps_every_slot(store, clock, monkeypatch):
    oldest = "chat-oldest-1700000000"
    newest = "chat-newest-1700000001"
    await pin(store, ask("oldest"), slot=oldest)
    clock.now += 1
    await pin(store, ask("newest"), slot=newest)
    before = {slot: store.asks(slot) for slot in (oldest, newest)}
    full_payload = store.serialize(b"s" * 32)
    monkeypatch.setattr(store_mod, "STORE_FILE_MAX", len(full_payload.encode("utf-8")) - 1)

    with (
        patch.object(store_mod, "_write_locked", side_effect=OSError("disk full")),
        pytest.raises(store_mod.StoreUnavailable),
    ):
        await pin(store, {**ask("newest"), "title": "Changed " * 10}, slot=newest)

    assert {slot: store.asks(slot) for slot in (oldest, newest)} == before


@pytest.mark.asyncio
async def test_a_fifth_key_is_refused_with_the_pinned_keys_listed(store):
    for n in range(4):
        await pin(store, ask(f"k{n}"))
    with pytest.raises(AskError) as refused:
        await pin(store, ask("k4"))
    assert refused.value.code == "too_many_asks"
    assert refused.value.extra["keys"] == ["k0", "k1", "k2", "k3"]
    assert "k0, k1, k2, k3" in refused.value.message
    # Replacing a pinned key is not a fifth ask.
    await pin(store, {**ask("k2"), "title": "Replaced"})
    assert [a["key"] for a in store.asks(SLOT)] == ["k0", "k1", "k2", "k3"]


@pytest.mark.parametrize(
    "change",
    [
        {"title": "t" * 121},
        {"title": "two\nlines"},
        {"detail": "d" * 1001},
        {"options": [{"label": "l" * 201}]},
        {"options": [{"label": "ok", "description": "d" * 501}]},
        {"options": [{"label": str(n)} for n in range(7)]},
        {"options": [{"label": "same"}, {"label": "same"}]},
        {"key": "Bad Key"},
        {"key": "-leading"},
        {"key": "k" * 41},
    ],
)
def test_over_length_or_malformed_text_is_refused_never_cut(change):
    with pytest.raises(AskError) as refused:
        validate_declaration({**ask(), **change})
    assert refused.value.status == 400


@pytest.mark.asyncio
async def test_an_identical_redeclare_writes_nothing_and_announces_nothing(state, store):
    async with TestClient(TestServer(make_app(state))) as client:
        with patch(
            "kiro_crew.dashboard.handlers.waiting_on_you._attested_session",
            new=AsyncMock(return_value=SESSION),
        ):
            first = await (await client.post("/api/agent-waiting/set", json=ask())).json()
            rows_after_first = len(waiting_rows(state))
            pushes = state.push_slot_patch.call_count
            mtime = store_path().stat().st_mtime_ns
            again = await (await client.post("/api/agent-waiting/set", json=ask())).json()
    assert first["changed"] is True and again["changed"] is False
    assert again["ask"]["ask_id"] == first["ask"]["ask_id"]
    assert len(waiting_rows(state)) == rows_after_first == 1
    assert state.push_slot_patch.call_count == pushes
    assert store_path().stat().st_mtime_ns == mtime


@pytest.mark.asyncio
async def test_an_incognito_sessions_ask_is_not_written_to_disk(state, store, clock):
    state._slots[SLOT].memory_mode = "incognito"
    async with TestClient(TestServer(make_app(state))) as client:
        with patch(
            "kiro_crew.dashboard.handlers.waiting_on_you._attested_session",
            new=AsyncMock(return_value=SESSION),
        ):
            resp = await client.post("/api/agent-waiting/set", json=ask())
            assert resp.status == 200
    assert [a["key"] for a in store.asks(SLOT)] == ["scope-override"]
    assert not store_path().exists()
    restarted = new_store(clock)
    await restarted.warm()
    assert restarted.asks(SLOT) == []


@pytest.mark.asyncio
async def test_text_is_redacted_before_it_is_stored(state, store):
    def _redact(text):
        return text.replace("PLANTED", "[REDACTED]"), int("PLANTED" in text)

    async with TestClient(TestServer(make_app(state))) as client:
        with (
            patch(
                "kiro_crew.dashboard.handlers.waiting_on_you._attested_session",
                new=AsyncMock(return_value=SESSION),
            ),
            patch("kiro_crew.security.redact_credentials", side_effect=_redact),
        ):
            await client.post(
                "/api/agent-waiting/set",
                json={**ask(), "detail": "use PLANTED", "title": "PLANTED title"},
            )
    pinned = store.asks(SLOT)[0]
    assert "PLANTED" not in pinned["detail"] and "PLANTED" not in pinned["title"]
    assert "PLANTED" not in store_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_store_keeps_at_most_two_thousand_slots(store, clock, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_SLOTS", 3)
    for n in range(4):
        clock.now += 1
        await pin(store, ask(), slot=f"chat-{n}-1700000000")
    assert store.asks("chat-0-1700000000") == []
    assert all(store.asks(f"chat-{n}-1700000000") for n in (1, 2, 3))
