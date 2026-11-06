"""The loopback Slack wire records what the real client actually sent."""

from __future__ import annotations

import pytest
from slack_sdk.errors import SlackApiError
from slack_wire_helpers import slack_wire


@pytest.mark.asyncio
async def test_a_threaded_block_post_reaches_the_wire_with_its_channel_blocks_and_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]
    async with slack_wire(monkeypatch) as wire:
        ts = await wire.client.post_blocks("C0CHAN", blocks, "hi", thread_ts="1700.000001")
    [post] = wire.posts()
    assert (post.get("channel"), post.get("blocks"), post.get("thread_ts")) == (
        "C0CHAN",
        blocks,
        "1700.000001",
    )
    assert post.get("unfurl_links") is False and post.get("unfurl_media") is False
    assert ts == "1700000000.000001"


@pytest.mark.asyncio
async def test_a_top_level_text_post_carries_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    async with slack_wire(monkeypatch) as wire:
        await wire.client.post_message("C0CHAN", "plain")
    [post] = wire.posts()
    assert post.get("text") == "plain"
    assert "thread_ts" not in post.args


@pytest.mark.asyncio
async def test_a_failed_method_raises_the_sdk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async with slack_wire(monkeypatch) as wire:
        wire.fail("chat.postMessage", "channel_not_found")
        with pytest.raises(SlackApiError):
            await wire.client.post_message("C0GONE", "x")
    assert [call.get("channel") for call in wire.posts()] == ["C0GONE"]


@pytest.mark.asyncio
async def test_form_encoded_arguments_are_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    async with slack_wire(monkeypatch) as wire:
        await wire.client.update_message("C0CHAN", "1700.000002", text="edited")
    [update] = wire.sent("chat.update")
    assert (update.get("channel"), update.get("ts"), update.get("text")) == (
        "C0CHAN",
        "1700.000002",
        "edited",
    )
