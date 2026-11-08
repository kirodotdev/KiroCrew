"""A steer is only written to an engine that has a prompt in flight.

``slot.turn_running`` reads True from the moment a turn's task exists, which is
before its prompt reaches the engine: the context build in ``_turn_build_prompt``
runs first, and so does the post-turn bookkeeping in the queue cycle. A send in
that window would be written as ``_session/steer`` to an engine with no prompt,
where nothing consumes it, and the chat sits on "thinking". Such a send takes
the queue path instead, so the next turn delivers it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import steer_requeue_helpers
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from steer_requeue_helpers import _running_slot

_patch_sel = steer_requeue_helpers._patch_sel


def _steer_client(*, prompt_in_flight: bool | None) -> MagicMock:
    """A steer-capable client. ``None`` stands for one that cannot say whether a
    prompt is in flight."""
    client = MagicMock(spec=["supports_steer", "steer_needs_loss_recovery", "steer"])
    client.supports_steer = True
    client.steer_needs_loss_recovery = False
    client.steer = AsyncMock(return_value=True)
    if prompt_in_flight is not None:
        client.has_active_turn = MagicMock(return_value=prompt_in_flight)
    return client


async def _post_steer(state, message: str) -> dict:
    async with TestClient(TestServer(_make_app(state))) as http:
        resp = await http.post(
            "/api/chat", json={"slot": "test", "message": message, "steer": True}
        )
        assert resp.status == 200
        return await resp.json()


def _slot_with_client(tmp_path, monkeypatch, client):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _running_slot(state)
    slot._acp_client = client
    return state, slot


@pytest.mark.asyncio
async def test_send_while_prompt_is_being_built_is_queued_not_steered(
    tmp_path, monkeypatch, _patch_sel
):
    client = _steer_client(prompt_in_flight=False)
    state, slot = _slot_with_client(tmp_path, monkeypatch, client)

    body = await _post_steer(state, "are you there")

    client.steer.assert_not_awaited()
    assert body.get("queued") is True and not body.get("steered")
    assert [e.get("content") for e in slot._queue] == ["are you there"]
    assert slot._pending_steers == []
    assert slot._steer_delivery_ids == {}


@pytest.mark.asyncio
async def test_send_during_a_running_prompt_still_steers(tmp_path, monkeypatch, _patch_sel):
    client = _steer_client(prompt_in_flight=True)
    state, slot = _slot_with_client(tmp_path, monkeypatch, client)

    body = await _post_steer(state, "also check the tests")

    client.steer.assert_awaited_once_with("also check the tests")
    assert body.get("steered") is True
    assert list(slot._queue) == []


@pytest.mark.asyncio
async def test_a_client_that_cannot_say_keeps_the_steer_path(tmp_path, monkeypatch, _patch_sel):
    client = _steer_client(prompt_in_flight=None)
    state, slot = _slot_with_client(tmp_path, monkeypatch, client)

    body = await _post_steer(state, "keep going")

    client.steer.assert_awaited_once_with("keep going")
    assert body.get("steered") is True
    assert list(slot._queue) == []
