"""The slot-detail read carries the stop machine's phase.

ChatEmbed learns a slot's state only from this poll, so without ``stop_state``
its Stop button cannot tell a pending soft cancel from an unpressed Stop, and
the next press (which the backend escalates to a queue-clearing hard kill)
would look like a first press.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import slot_projection
from kiro_crew.session_lifecycle import STOP_DECLINED_ESCALATION_SECS


async def _detail(state, slot_key: str) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot_key}")
        assert resp.status == 200
        return await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "soft_pending", "killing"])
async def test_detail_reports_stop_state(tmp_path, monkeypatch, phase):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot._stop_state = phase

    body = await _detail(state, "s1")

    assert body["stop_state"] == phase


# The decline marker's own window, read with the projection's clock frozen at its
# module binding: armed inside the window, expired at its edge, clear when unset.
_NOW = 1000.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declined_at", "armed"),
    [
        (_NOW - 1.0, True),
        (_NOW - STOP_DECLINED_ESCALATION_SECS, False),
        (0.0, False),
    ],
    ids=["armed", "expired", "clear"],
)
async def test_detail_reports_stop_declined(tmp_path, monkeypatch, declined_at, armed):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(slot_projection, "time", SimpleNamespace(monotonic=lambda: _NOW))
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot._stop_declined_at = declined_at

    body = await _detail(state, "s1")

    assert body["stop_declined"] is armed
