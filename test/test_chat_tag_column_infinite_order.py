"""A tag column ``order`` that parses as infinity is refused, not raised as a 500.

``json.loads`` reads a number too large for a float, such as ``1e400``, as
infinity, and ``int()`` on it raises ``OverflowError``. The column writers answer
it as an invalid payload and leave the stored column as it was.
"""

from __future__ import annotations

import chat_tags_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_hermetic_signing_secret = chat_tags_helpers._hermetic_signing_secret

_INFINITE_ORDER = b'{"name": "Renamed", "order": 1e400}'


@pytest.mark.asyncio
async def test_an_update_with_an_infinite_order_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    app = _make_tags_app(state)
    async with TestClient(TestServer(app)) as client:
        col = await (await client.post("/api/chat/tag-columns", json={"name": "A"})).json()
        before = dict(next(c for c in state._tag_boards if c["id"] == col["id"]))

        resp = await client.patch(
            f"/api/chat/tag-columns/{col['id']}",
            data=_INFINITE_ORDER,
            headers={"Content-Type": "application/json"},
        )

        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_column_payload"
        after = next(c for c in state._tag_boards if c["id"] == col["id"])
        assert after == before


@pytest.mark.asyncio
async def test_a_word_order_is_still_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    app = _make_tags_app(state)
    async with TestClient(TestServer(app)) as client:
        col = await (await client.post("/api/chat/tag-columns", json={"name": "A"})).json()

        resp = await client.patch(
            f"/api/chat/tag-columns/{col['id']}", json={"name": "B", "order": "first"}
        )

        assert resp.status == 200
        body = await resp.json()
        assert body["name"] == "B"
        assert body["order"] == col["order"]
