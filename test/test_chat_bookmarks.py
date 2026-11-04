"""Per-session bookmarks: the store, the HTTP routes and the agent's directive applier."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_chat_pins_helpers import _make_state

from kiro_crew.dashboard import chat_bookmarks_store
from kiro_crew.dashboard.chat_bookmarks import (
    api_chat_bookmarks_create,
    api_chat_bookmarks_delete,
    api_chat_bookmarks_list,
)
from kiro_crew.dashboard.chat_bookmarks_store import (
    CHAT_BOOKMARKS_FILE,
    MAX_BOOKMARKS_PER_SLOT,
    BookmarkError,
    ChatBookmarkStore,
    normalize_url,
)
from kiro_crew.dashboard.session_directive_apply import apply_session_directive_outcome


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_bookmarks_store, "config_dir", lambda: tmp_path)
    return tmp_path


def _state(tmp_path, slots=("chat-1",)):
    state = _make_state(tmp_path)
    known = {key: SimpleNamespace(key=key, _app="") for key in slots}
    state.get_slot = known.get
    state.broadcast_ws_owners = MagicMock()
    return state


def _client(state) -> TestClient:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/bookmarks", api_chat_bookmarks_list)
    app.router.add_post("/api/chat/bookmarks", api_chat_bookmarks_create)
    app.router.add_delete("/api/chat/bookmarks/{id}", api_chat_bookmarks_delete)
    return TestClient(TestServer(app))


# ── URL rules ──


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "/relative", "https://", ""],
)
def test_non_http_urls_are_refused(url):
    with pytest.raises(BookmarkError):
        normalize_url(url)


def test_http_and_https_urls_are_kept():
    assert normalize_url("  https://github.com/o/r/pull/1 ") == "https://github.com/o/r/pull/1"
    assert normalize_url("http://localhost:8080/x") == "http://localhost:8080/x"


# ── Store ──


@pytest.mark.asyncio
async def test_add_is_idempotent_per_url_and_updates_the_title(home):
    store = ChatBookmarkStore()
    first, created = await store.add("chat-1", "https://example.com/a", "A")
    again, created_again = await store.add("chat-1", "https://example.com/a", "Better A")
    assert created and not created_again
    assert again["id"] == first["id"]
    assert [b["title"] for b in await store.list_for("chat-1")] == ["Better A"]


@pytest.mark.asyncio
async def test_bookmarks_are_scoped_to_their_slot(home):
    store = ChatBookmarkStore()
    await store.add("chat-1", "https://example.com/a")
    await store.add("chat-2", "https://example.com/b")
    assert [b["url"] for b in await store.list_for("chat-1")] == ["https://example.com/a"]
    assert await store.remove("chat-2", url="https://example.com/a") is False


@pytest.mark.asyncio
async def test_a_slot_keeps_at_most_the_cap(home):
    store = ChatBookmarkStore()
    for i in range(MAX_BOOKMARKS_PER_SLOT):
        await store.add("chat-1", f"https://example.com/{i}")
    with pytest.raises(BookmarkError) as exc:
        await store.add("chat-1", "https://example.com/one-more")
    assert exc.value.code == "bookmark_limit_reached"


@pytest.mark.asyncio
async def test_bookmarks_survive_a_reload_and_keep_malformed_rows(home):
    (home / CHAT_BOOKMARKS_FILE).write_text(json.dumps([{"id": "bad"}]))
    store = ChatBookmarkStore()
    await store.add("chat-1", "https://example.com/a", "A", origin="agent")
    rows = json.loads((home / CHAT_BOOKMARKS_FILE).read_text())
    assert {"id": "bad"} in rows
    reloaded = await ChatBookmarkStore().list_for("chat-1")
    assert [(b["url"], b["origin"]) for b in reloaded] == [("https://example.com/a", "agent")]


@pytest.mark.asyncio
async def test_an_unreadable_file_is_never_replaced(home):
    (home / CHAT_BOOKMARKS_FILE).write_text("{not json")
    store = ChatBookmarkStore()
    with pytest.raises(OSError):
        await store.add("chat-1", "https://example.com/a")
    assert (home / CHAT_BOOKMARKS_FILE).read_text() == "{not json"
    assert await store.list_for("chat-1") == []


@pytest.mark.parametrize(
    "url",
    [
        "https://user:hunter2pass@example.com/x",
        "https://example.com/x?token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
@pytest.mark.asyncio
async def test_a_url_carrying_a_credential_is_never_stored(home, url):
    store = ChatBookmarkStore()
    with pytest.raises(BookmarkError) as exc:
        await store.add("chat-1", url)
    assert exc.value.code == "url_contains_credential"
    assert not (home / CHAT_BOOKMARKS_FILE).exists()


# ── HTTP routes ──


@pytest.mark.asyncio
async def test_create_list_delete_round_trip(home):
    state = _state(home)
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/bookmarks",
            json={"slot_key": "chat-1", "url": "https://example.com/doc", "title": "Doc"},
        )
        assert resp.status == 201
        created = await resp.json()
        assert created["origin"] == "user"
        listed = await (await client.get("/api/chat/bookmarks?slot=chat-1")).json()
        assert [b["id"] for b in listed["bookmarks"]] == [created["id"]]
        resp = await client.delete(f"/api/chat/bookmarks/{created['id']}")
        assert resp.status == 200
        listed = await (await client.get("/api/chat/bookmarks?slot=chat-1")).json()
        assert listed["bookmarks"] == []
    frames = [c.args for c in state.broadcast_ws_owners.call_args_list]
    assert frames == [("bookmarks_changed", {"slot_key": "chat-1"})] * 2


@pytest.mark.asyncio
async def test_create_refuses_a_bad_url_and_an_unknown_slot(home):
    async with _client(_state(home)) as client:
        resp = await client.post(
            "/api/chat/bookmarks", json={"slot_key": "chat-1", "url": "javascript:alert(1)"}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_url"
        resp = await client.post(
            "/api/chat/bookmarks", json={"slot_key": "nope", "url": "https://example.com"}
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_delete_of_an_unknown_id_is_not_found(home):
    async with _client(_state(home)) as client:
        resp = await client.delete("/api/chat/bookmarks/does-not-exist")
        assert resp.status == 404
        assert (await resp.json())["code"] == "bookmark_not_found"


# ── Agent directive ──


async def _apply(state, slot, args, *, user_facing=True, key="dashboard_chat-1"):
    return await apply_session_directive_outcome(
        state, slot, key, "chat_bookmark", args, producer_is_user_facing=user_facing
    )


@pytest.mark.asyncio
async def test_the_directive_adds_and_removes_on_its_own_slot(home):
    state = _state(home)
    slot = state.get_slot("chat-1")
    outcome = await _apply(
        state, slot, {"add": [{"url": "https://example.com/pr/1", "title": "PR 1"}]}
    )
    assert outcome.text.startswith("Bookmarks updated.")
    assert "PR 1 <https://example.com/pr/1>" in outcome.text
    rows = await state.chat_bookmarks.list_for("chat-1")
    assert [(r["url"], r["origin"]) for r in rows] == [("https://example.com/pr/1", "agent")]

    outcome = await _apply(state, slot, {"remove": ["https://example.com/pr/1"]})
    assert "(none)" in outcome.text
    assert await state.chat_bookmarks.list_for("chat-1") == []


@pytest.mark.asyncio
async def test_the_directive_names_each_refused_entry(home):
    state = _state(home)
    slot = state.get_slot("chat-1")
    outcome = await _apply(
        state,
        slot,
        {"add": [{"url": "javascript:alert(1)"}, {"url": "https://example.com/ok"}]},
    )
    assert outcome.text.startswith("Some bookmarks were not changed: invalid_url:")
    outcome = await _apply(state, slot, {"add": [{"url": "ftp://example.com"}]})
    assert outcome.text.startswith("Error: invalid_url:")


@pytest.mark.asyncio
async def test_the_directive_is_refused_on_a_turn_no_person_sent(home):
    state = _state(home)
    outcome = await _apply(
        state,
        state.get_slot("chat-1"),
        {"add": [{"url": "https://example.com"}]},
        user_facing=False,
    )
    assert outcome.text.startswith("Error:")
    assert await state.chat_bookmarks.list_for("chat-1") == []


@pytest.mark.asyncio
async def test_the_directive_is_refused_without_a_dashboard_slot(home):
    state = _state(home)
    outcome = await _apply(state, None, {"add": [{"url": "https://example.com"}]})
    assert outcome.text.startswith("Error: chat_bookmark only works from a dashboard chat")
