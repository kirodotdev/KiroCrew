"""Tests for live slot stale-window reconciliation (#4373).

When a live slot's in-memory window diverges from disk (messages on disk that
the slot does not know about), both the resume and detail endpoints must
self-heal on the next request by appending the missing tail from disk.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.mark.asyncio
async def test_resume_reconciles_stale_slot_from_disk(tmp_path, monkeypatch):
    """Resume of a live slot detects new on-disk messages and appends them."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    # Seed the transcript with 3 messages.
    log.append("dashboard:s1", "user", "msg1")
    log.append("dashboard:s1", "assistant", "reply1")
    log.append("dashboard:s1", "user", "msg2")

    async with TestClient(TestServer(_make_app(state))) as client:
        # First resume loads 3 messages into the slot.
        r1 = await (
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        ).json()
        assert r1["ok"] is True
        assert r1["total"] == 3
        assert all(not (message.get("meta") or {}).get("mid") for message in r1["messages"])

        # Now write 2 more messages directly to disk WITHOUT going through
        # the slot's append — simulating the divergence scenario.
        log.append("dashboard:s1", "assistant", "reply2")
        log.append("dashboard:s1", "user", "msg3")

        # The slot still thinks it has only 3 messages.
        slot = state._slots["s1"]
        assert len(slot.messages) == 3

        # Simulate the periodic flush having persisted the slot (clears dirty).
        slot._dirty = False

        # Second resume (page refresh) should reconcile and return all 5.
        r2 = await (
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        ).json()
        assert r2["ok"] is True
        assert r2["total"] == 5, f"Expected 5 messages after reconciliation, got {r2['total']}"
        assert all(not (message.get("meta") or {}).get("mid") for message in r2["messages"])
        # The slot's in-memory window should now have all 5.
        assert len(slot.messages) == 5
        assert all(not (message.get("meta") or {}).get("mid") for message in slot.messages)


@pytest.mark.asyncio
async def test_detail_reconciles_stale_slot_from_disk(tmp_path, monkeypatch):
    """Detail endpoint detects stale window and includes missing disk rows."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    # Seed the transcript.
    log.append("dashboard:s1", "user", "msg1")
    log.append("dashboard:s1", "assistant", "reply1")

    async with TestClient(TestServer(_make_app(state))) as client:
        # Resume to create the live slot.
        await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        slot = state._slots["s1"]
        assert len(slot.messages) == 2

        # Simulate the periodic flush having persisted the slot (clears dirty).
        slot._dirty = False

        # Write directly to disk — bypassing the in-memory slot.
        log.append("dashboard:s1", "assistant", "reply2")
        log.append("dashboard:s1", "user", "msg2")
        log.append("dashboard:s1", "assistant", "reply3")

        # Detail request should reconcile and include all 5 messages.
        r = await (await client.get("/api/chat/slots/s1")).json()
        # Total includes all messages (memory reconciled from disk).
        assert (
            r["total"] == 5
        ), f"Expected 5 messages in detail after reconciliation, got {r['total']}"
        assert all(not (message.get("meta") or {}).get("mid") for message in r["messages"])
        # The slot should also be updated in memory.
        assert len(slot.messages) == 5
        assert all(not (message.get("meta") or {}).get("mid") for message in slot.messages)


@pytest.mark.asyncio
async def test_reconcile_no_op_when_not_stale(tmp_path, monkeypatch):
    """Reconciliation is a no-op when disk and memory are in sync."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    log.append("dashboard:s1", "user", "msg1")
    log.append("dashboard:s1", "assistant", "reply1")

    async with TestClient(TestServer(_make_app(state))) as client:
        # Resume loads both messages.
        await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        slot = state._slots["s1"]
        assert len(slot.messages) == 2

        # Resume again — no divergence, should be a no-op.
        r = await (
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        ).json()
        assert r["ok"] is True
        assert r["total"] == 2
        assert len(slot.messages) == 2


@pytest.mark.asyncio
async def test_reconcile_preserves_disk_window_len(tmp_path, monkeypatch):
    """After reconciliation, _disk_window_len covers the full window."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    log.append("dashboard:s1", "user", "msg1")

    async with TestClient(TestServer(_make_app(state))) as client:
        await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        slot = state._slots["s1"]

        # Simulate the periodic flush having persisted the slot (clears dirty).
        slot._dirty = False

        # Write directly to disk.
        log.append("dashboard:s1", "assistant", "reply1")
        log.append("dashboard:s1", "user", "msg2")

        # Trigger reconciliation via detail.
        await client.get("/api/chat/slots/s1")

        # _disk_window_len must cover all messages so the next save does not
        # duplicate them.
        assert slot._disk_window_len == len(slot.messages) == 3


@pytest.mark.asyncio
async def test_reconcile_with_disk_older_count(tmp_path, monkeypatch):
    """Reconciliation works when slot has a non-zero _disk_older_count."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    log = state.conversation_log

    # Write 6 messages to disk.
    for i in range(6):
        log.append("dashboard:s1", "user" if i % 2 == 0 else "assistant", f"msg{i}")

    async with TestClient(TestServer(_make_app(state))) as client:
        await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        slot = state._slots["s1"]
        assert len(slot.messages) == 6

        # Simulate a truncated window by setting _disk_older_count to 2
        # and removing the first 2 messages from the slot window (consistent state).
        del slot.messages[:2]
        slot._disk_older_count = 2
        slot._disk_window_len = len(slot.messages)  # 4
        slot._dirty = False  # Simulate flush

        # Slot now represents 2 + 4 = 6 lines, which matches disk (6 total).
        # Add messages to disk beyond what the slot covers.
        log.append("dashboard:s1", "user", "extra1")
        # Disk = 7, slot covers 2 + 4 = 6. Should reconcile 1.

        r = await (
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
        ).json()
        assert r["ok"] is True
        assert len(slot.messages) == 5  # 4 + 1 reconciled
