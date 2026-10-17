"""Who may write the sidebar board (``/api/chat/tag-columns``).

The board is one shared layout with no owner, like the tag vocabulary, so every
column write applies ``_refuse_vocabulary_write``: an app caller and an admitted
crew member are refused, and so is a ``dashboard:`` key naming a slot that is
gone. The person (the browser) keeps full authority, and the create endpoint
draws on a per-caller budget an internal caller cannot outrun. Reads stay open.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY

_TAG = {"id": "t00000000001", "name": "Active", "color": "#22c55e", "order": 0, "status": True}
_COLS = [
    {"id": "c1", "name": "One", "tag_ids": [], "mode": "any", "order": 0, "source": "tags"},
    {"id": "c2", "name": "Two", "tag_ids": [], "mode": "any", "order": 1, "source": "tags"},
]


@pytest.fixture(autouse=True)
def _hermetic_signing_secret(monkeypatch):
    from kiro_crew.dashboard import token_secret

    monkeypatch.setattr(token_secret, "_get_secret", lambda: b"test-signing-key")


def _board_app(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [dict(_TAG)]
    state._tag_boards = [dict(c) for c in _COLS]
    app = _make_tags_app(state)

    @web.middleware
    async def _claims(request, handler):
        # Stands in for the token middleware (app claim) and the chat-route gate
        # (verified member principal).
        if request.headers.get("X-As-App"):
            request["app"] = request.headers["X-As-App"]
        if request.headers.get("X-As-Member"):
            request[MEMBER_CHAT_PRINCIPAL_KEY] = request.headers["X-As-Member"]
        return await handler(request)

    app.middlewares.append(_claims)
    return state, app


_WRITES = [
    ("post", "/api/chat/tag-columns", {"name": "Coined", "tag_ids": ["t00000000001"]}),
    ("patch", "/api/chat/tag-columns/c1", {"name": "Renamed"}),
    ("delete", "/api/chat/tag-columns/c1", None),
    ("put", "/api/chat/tag-columns/order", {"ids": ["c2", "c1"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", _WRITES)
@pytest.mark.parametrize(
    "headers,code",
    [
        ({"X-As-App": "some-app"}, "app_forbidden"),
        ({"X-As-Member": "member:member-x-deadbeef"}, "app_forbidden"),
        ({"X-Session-Key": "dashboard:gone-slot"}, "caller_unattributable"),
    ],
)
async def test_no_agent_principal_writes_the_board(
    tmp_path, monkeypatch, method, path, body, headers, code
):
    """Mutation guard: drop the refusal from any one handler and its row goes 2xx."""
    state, app = _board_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status == 403
        assert (await resp.json())["code"] == code
    assert state._tag_boards == _COLS


@pytest.mark.asyncio
async def test_an_app_and_a_member_still_read_the_board(tmp_path, monkeypatch):
    _state, app = _board_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        for headers in ({"X-As-App": "some-app"}, {"X-As-Member": "member:member-x-deadbeef"}):
            resp = await client.get("/api/chat/tag-columns", headers=headers)
            assert resp.status == 200
            assert [c["id"] for c in await resp.json()] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_the_person_keeps_every_write(tmp_path, monkeypatch):
    state, app = _board_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        created = await client.post(
            "/api/chat/tag-columns", json={"name": "Mine", "tag_ids": ["t00000000001"]}
        )
        assert created.status == 201
        assert (
            await client.put("/api/chat/tag-columns/order", json={"ids": ["c2", "c1"]})
        ).status == 200
        assert (await client.patch("/api/chat/tag-columns/c1", json={"name": "Uno"})).status == 200
        assert (await client.delete("/api/chat/tag-columns/c2")).status == 200
    assert [c["name"] for c in state._tag_boards] == ["Uno", "Mine"]


@pytest.mark.asyncio
async def test_create_rate_limits_an_internal_caller(tmp_path, monkeypatch):
    """Mutation guard: drop the ``allow_create`` call and the whole burst is 201."""
    create_rate_limit.reset_for_tests()
    try:
        _state, app = _board_app(tmp_path, monkeypatch)
        headers = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
        async with TestClient(TestServer(app)) as client:
            allowed = 0
            for i in range(create_rate_limit.MAX_TAG_COLUMN_CREATES_PER_WINDOW + 3):
                resp = await client.post(
                    "/api/chat/tag-columns",
                    json={"name": f"C{i}", "tag_ids": ["t00000000001"]},
                    headers=headers,
                )
                if resp.status == 201:
                    allowed += 1
                else:
                    assert resp.status == 429
                    assert (await resp.json())["code"] == "create_rate_limited"
        assert allowed == create_rate_limit.MAX_TAG_COLUMN_CREATES_PER_WINDOW
    finally:
        create_rate_limit.reset_for_tests()


@pytest.mark.asyncio
async def test_create_does_not_rate_limit_the_browser(tmp_path, monkeypatch):
    create_rate_limit.reset_for_tests()
    try:
        _state, app = _board_app(tmp_path, monkeypatch)
        async with TestClient(TestServer(app)) as client:
            for i in range(create_rate_limit.MAX_TAG_COLUMN_CREATES_PER_WINDOW + 3):
                resp = await client.post("/api/chat/tag-columns", json={"name": f"B{i}"})
                assert resp.status == 201
    finally:
        create_rate_limit.reset_for_tests()


@pytest.mark.asyncio
async def test_ensure_returns_the_existing_twin_under_the_lock(tmp_path, monkeypatch):
    """Two agents creating the same named column converge on one."""
    state, app = _board_app(tmp_path, monkeypatch)
    body = {"name": "Doing", "tag_ids": ["t00000000001"], "ensure": True}
    async with TestClient(TestServer(app)) as client:
        first = await client.post("/api/chat/tag-columns", json=body)
        assert first.status == 201
        again = await client.post("/api/chat/tag-columns", json={**body, "name": "doing"})
        assert again.status == 200
        assert (await again.json())["id"] == (await first.json())["id"]
    assert sum(1 for c in state._tag_boards if c.get("name") == "Doing") == 1
    assert all("ensure" not in c for c in state._tag_boards)


@pytest.mark.asyncio
async def test_without_ensure_create_still_appends(tmp_path, monkeypatch):
    """The board UI adds columns without ``ensure``; its behaviour is unchanged."""
    state, app = _board_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        for _ in range(2):
            resp = await client.post("/api/chat/tag-columns", json={"name": "", "tag_ids": []})
            assert resp.status == 201
    assert len(state._tag_boards) == len(_COLS) + 2


@pytest.mark.asyncio
async def test_reorder_with_a_stale_base_is_refused(tmp_path, monkeypatch):
    state, app = _board_app(tmp_path, monkeypatch)
    async with TestClient(TestServer(app)) as client:
        stale = await client.put(
            "/api/chat/tag-columns/order", json={"ids": ["c2", "c1"], "base_ids": ["c2", "c1"]}
        )
        assert stale.status == 409
        assert (await stale.json())["code"] == "stale_base"
        assert [c["id"] for c in state._tag_boards] == ["c1", "c2"]
        fresh = await client.put(
            "/api/chat/tag-columns/order", json={"ids": ["c2", "c1"], "base_ids": ["c1", "c2"]}
        )
        assert fresh.status == 200
    assert [c["id"] for c in state._tag_boards] == ["c2", "c1"]


@pytest.mark.asyncio
async def test_an_agent_write_is_audited_as_the_agent_not_the_browser(tmp_path, monkeypatch):
    """SEL must tell an MCP-driven board change from the person clicking."""
    from unittest.mock import MagicMock

    from kiro_crew.dashboard import chat_tags

    create_rate_limit.reset_for_tests()
    audit = MagicMock()
    monkeypatch.setattr(chat_tags, "sel", lambda: audit)
    try:
        _state, app = _board_app(tmp_path, monkeypatch)
        headers = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
        body = {"name": "Doing", "tag_ids": ["t00000000001"], "ensure": True}
        async with TestClient(TestServer(app)) as client:
            for _ in range(2):  # the second call is the ensure twin-hit
                resp = await client.post("/api/chat/tag-columns", json=body, headers=headers)
                assert resp.status in (200, 201)
            resp = await client.put(
                "/api/chat/tag-columns/order", json={"ids": ["c2", "c1"]}, headers=headers
            )
            assert resp.status == 200
    finally:
        create_rate_limit.reset_for_tests()
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    assert len(rows) == 3
    assert all(r["source"] == "mcp" and r["caller"] == "kirocrew-dashboard" for r in rows), rows


@pytest.mark.asyncio
async def test_an_ensure_retry_does_not_spend_the_create_budget(tmp_path, monkeypatch):
    """Only an actual append is charged, so a retrying agent is not locked out."""
    create_rate_limit.reset_for_tests()
    try:
        _state, app = _board_app(tmp_path, monkeypatch)
        headers = {"X-Internal-Secret": "s3cret", "X-Internal-Caller": "kirocrew-dashboard"}
        body = {"name": "Doing", "tag_ids": ["t00000000001"], "ensure": True}
        async with TestClient(TestServer(app)) as client:
            for _ in range(create_rate_limit.MAX_TAG_COLUMN_CREATES_PER_WINDOW + 3):
                resp = await client.post("/api/chat/tag-columns", json=body, headers=headers)
                assert resp.status in (200, 201)
            fresh = await client.post(
                "/api/chat/tag-columns",
                json={"name": "Other", "tag_ids": ["t00000000001"]},
                headers=headers,
            )
            assert fresh.status == 201
    finally:
        create_rate_limit.reset_for_tests()


@pytest.mark.asyncio
async def test_ensure_does_not_match_a_column_that_also_shows_untagged(tmp_path, monkeypatch):
    state, app = _board_app(tmp_path, monkeypatch)
    state._tag_boards.append(
        {
            "id": "c3",
            "name": "Doing",
            "tag_ids": ["t00000000001"],
            "mode": "any",
            "order": 2,
            "source": "tags",
            "include_untagged": True,
        }
    )
    body = {"name": "Doing", "tag_ids": ["t00000000001"], "ensure": True}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/tag-columns", json=body)
        assert resp.status == 201
        assert (await resp.json())["id"] != "c3"


@pytest.mark.asyncio
async def test_ensure_skips_a_hand_edited_column_with_non_list_tag_ids(tmp_path, monkeypatch):
    state, app = _board_app(tmp_path, monkeypatch)
    state._tag_boards.append(
        {"id": "c3", "name": "Doing", "tag_ids": 7, "mode": "any", "order": 2, "source": "tags"}
    )
    body = {"name": "Doing", "tag_ids": ["t00000000001"], "ensure": True}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/tag-columns", json=body)
        assert resp.status == 201
