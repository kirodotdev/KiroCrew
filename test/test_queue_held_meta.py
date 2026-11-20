"""The slot-detail queue says per entry whether it waits for a person (``meta.held``).

``meta.restored`` is provenance: the entry came back from disk. Whether the
drain will send it without another action is ``is_held_queue_entry``, and the
queue card reads that verdict, so a restored entry the startup drain accepted
does not claim it will wait.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.chat_delivery import queue_entry_view
from kiro_crew.dashboard.slot_queue_repository import (
    RESTORED_BOOT_ACCEPTED_KEY,
    RESTORED_QUEUE_KEY,
    RESTORED_SEND_CHOSEN_KEY,
    _release_boot_acceptance,
)


def _meta(item: dict) -> dict:
    return queue_entry_view(item).get("meta") or {}


def test_a_restored_entry_nobody_chose_is_held():
    meta = _meta({"id": "r1", "content": "hi", RESTORED_QUEUE_KEY: True})
    assert meta.get("restored") is True
    assert meta.get("held") is True


def test_a_live_entry_is_not_held():
    assert "held" not in _meta({"id": "q1", "content": "hi"})


def test_a_restored_entry_the_person_chose_is_not_held():
    item = {"id": "r1", "content": "hi", RESTORED_QUEUE_KEY: True, RESTORED_SEND_CHOSEN_KEY: True}
    meta = _meta(item)
    assert meta.get("restored") is True
    assert "held" not in meta


def test_a_boot_accepted_entry_is_not_held_until_the_acceptance_releases():
    item = {"id": "r1", "content": "hi", RESTORED_QUEUE_KEY: True, RESTORED_BOOT_ACCEPTED_KEY: True}
    meta = _meta(item)
    assert meta.get("restored") is True
    assert "held" not in meta

    pushes: list[int] = []
    state = SimpleNamespace(
        _slots={"s": SimpleNamespace(_queue=[item])},
        push_slots_update=lambda: pushes.append(1),
    )
    _release_boot_acceptance(state, ["s"])

    assert pushes == [1]
    assert _meta(item).get("held") is True


@pytest.mark.asyncio
async def test_the_slot_detail_queue_carries_held_for_a_held_entry_only(tmp_path, monkeypatch):
    """Through the real slot-detail read, whose off-loop snapshot copies the
    entries: a restored entry nobody chose reads held, a boot-accepted one does not."""
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app
    from chat_test_helpers import _make_state as _make_full_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_full_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("held-chat")
    slot._queue.extend(
        [
            {"id": "held-1", "content": "waits", "meta": {}, RESTORED_QUEUE_KEY: True},
            {
                "id": "boot-1",
                "content": "goes",
                "meta": {},
                RESTORED_QUEUE_KEY: True,
                RESTORED_BOOT_ACCEPTED_KEY: True,
            },
        ]
    )
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/held-chat")
        assert resp.status == 200
        queue = {q["id"]: q.get("meta") or {} for q in (await resp.json())["queue"]}

    assert queue["held-1"].get("held") is True
    assert queue["held-1"].get("restored") is True
    assert "held" not in queue["boot-1"]
    assert queue["boot-1"].get("restored") is True
