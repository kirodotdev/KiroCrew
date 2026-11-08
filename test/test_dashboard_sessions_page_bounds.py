"""``GET /api/sessions`` folds ``limit`` and ``offset`` into a real page.

The page is a Python list slice, where a negative bound counts from the END:
``offset=-3`` would read the far end of the list, and ``limit=-1`` would
return every row but one, past the 200-row cap. The route floors ``offset``
at 0 and keeps ``limit`` in ``[1, 200]``.

Real auth middleware and real route table, as in ``test_dashboard_sessions_named``.
Each case is compared with the unpaged listing of the same store, so the test
pins the page bounds without pinning the list's sort order.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.dashboard.routes import register_all

ROWS = 5


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"sessions-page-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.crons = None
    return st


async def _seed(state) -> None:
    """``ROWS`` closed transcripts, written off the event loop.

    Same reason as ``test_dashboard_sessions_named._seed``: the metadata write's
    Windows sharing-violation retry needs a thread with no loop of its own.
    """

    def _write() -> None:
        log = state.conversation_log
        for i in range(ROWS):
            key = f"dashboard:chat-{i}-17800000{i:02d}"
            log.append(key, "user", "hello")
            log.update_metadata(key, {"title": f"row {i}"})

    await asyncio.to_thread(_write)


async def _page(state, **params: str) -> dict:
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = state
    register_all(app)
    token = token_auth.generate_token("local-app")
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/sessions", params={"token": token, **params})
        assert resp.status == 200, await resp.text()
        return await resp.json()


def _keys(body: dict) -> list[str]:
    return [row["key"] for row in body["sessions"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "start", "stop", "has_more"),
    [
        # A negative offset reads the first page, not the far end.
        ({"offset": "-3", "limit": "2"}, 0, 2, True),
        # A negative or zero limit is a one-row page, not "all but one".
        ({"limit": "-1"}, 0, 1, True),
        ({"limit": "0"}, 0, 1, True),
        # In-range paging is unchanged.
        ({"offset": "1", "limit": "2"}, 1, 3, True),
        ({"offset": "3", "limit": "2"}, 3, 5, False),
        # A non-integer keeps the default page.
        ({"offset": "x", "limit": "y"}, 0, ROWS, False),
    ],
)
async def test_page_bounds_are_folded(state, params, start, stop, has_more) -> None:
    await _seed(state)
    full = _keys(await _page(state))
    assert len(full) == ROWS
    body = await _page(state, **params)
    assert _keys(body) == full[start:stop]
    assert body["total"] == ROWS
    assert body["has_more"] is has_more
