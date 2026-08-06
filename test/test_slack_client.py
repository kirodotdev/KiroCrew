"""Tests for Slack client abstraction."""

from __future__ import annotations

import pytest

from conftest import MockSlackClient


class TestMockSlackClient:
    @pytest.mark.asyncio
    async def test_post_returns_ts(self):
        c = MockSlackClient()
        ts = await c.post_message("C1", "hi")
        assert "." in ts

    @pytest.mark.asyncio
    async def test_post_increments_ts(self):
        c = MockSlackClient()
        ts1 = await c.post_message("C1", "a")
        ts2 = await c.post_message("C1", "b")
        assert ts1 != ts2

    @pytest.mark.asyncio
    async def test_actions_recorded(self):
        c = MockSlackClient()
        await c.post_message("C1", "hello", "thread1")
        await c.add_reaction("C1", "ts1", "eyes")
        assert len(c.actions) == 2
        assert c.actions[0][0] == "post"
        assert c.actions[1][0] == "react"
        assert c.actions[1][1]["emoji"] == "eyes"

    @pytest.mark.asyncio
    async def test_update_and_delete(self):
        c = MockSlackClient()
        ts = await c.post_message("C1", "draft")
        await c.update_message("C1", ts, "final")
        await c.delete_message("C1", ts)
        assert c.actions[-2][0] == "update"
        assert c.actions[-1][0] == "delete"

    @pytest.mark.asyncio
    async def test_post_blocks(self):
        c = MockSlackClient()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
        ts = await c.post_blocks("C1", blocks, "fallback", "thread1")
        assert "." in ts
        assert c.actions[-1][0] == "blocks"
        assert c.actions[-1][1]["blocks"] == blocks

    @pytest.mark.asyncio
    async def test_upload_records_all_params(self):
        """upload_file still works as the Slack transport for file_send."""
        c = MockSlackClient()
        await c.upload_file("C1", "1234.5678", "/tmp/f.csv", "f.csv", "Title")
        rec = c.actions[-1][1]
        assert rec["file"] == "/tmp/f.csv"
        assert rec["title"] == "Title"
        assert rec["thread_ts"] == "1234.5678"

    @pytest.mark.asyncio
    async def test_upload_thread_ts_empty_by_default(self):
        c = MockSlackClient()
        await c.upload_file("C1", "", "/tmp/f.txt", "f.txt", "f.txt")
        assert c.actions[-1][1]["thread_ts"] == ""
