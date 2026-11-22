"""A native Slack stream withholds a reply's trailing control-tag line, as delivery does.

The stream holds a line-leading ``<!--`` back while it may be the reply's tail and
settles that hold when the turn ends. Settled against a reply whose control tags
were already stripped, the hold always read as content and the tag streamed as
visible text, while the delivered text had none.
"""

from __future__ import annotations

import pytest
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tail", ["<!-- keep-visible -->", "<!-- keep-visible -->\n", "\n<!-- keep-visible -->"]
)
async def test_a_trailing_control_tag_never_streams(monkeypatch, tail):
    import kiro_crew.slack.handler as _h

    monkeypatch.setattr(_h, "_EDIT_INTERVAL", 0.0)  # flush on every chunk
    slack = MockSlackClient()
    slack._stream_enabled = True  # the native chat.startStream path
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="Report ready.\n"),
            LLMEvent(kind="text_chunk", text=tail),
        ]
    )
    await handle_message(slack, FakeSessionManager(provider), "C1", "go", None, "msg1", "U1")

    streamed = "".join(a[1]["text"] for a in slack.actions if a[0] == "append_stream")
    assert "Report ready." in streamed
    assert "keep-visible" not in streamed
    wire = [
        a[1].get("text") or ""
        for a in slack.actions
        if a[0] in ("append_stream", "stop_stream", "update", "post")
    ]
    assert all("keep-visible" not in t for t in wire), wire


@pytest.mark.asyncio
async def test_a_quoted_tag_mid_reply_still_streams(monkeypatch):
    """Non-vacuity: a tag that is content (inside a fence) is released, not dropped."""
    import kiro_crew.slack.handler as _h

    monkeypatch.setattr(_h, "_EDIT_INTERVAL", 0.0)
    slack = MockSlackClient()
    slack._stream_enabled = True
    provider = FakeProvider(
        [
            LLMEvent(kind="text_chunk", text="Use it like this:\n```md\n"),
            LLMEvent(kind="text_chunk", text="<!-- keep-visible -->\n```\n"),
        ]
    )
    await handle_message(slack, FakeSessionManager(provider), "C1", "go", None, "msg1", "U1")

    streamed = "".join(a[1]["text"] for a in slack.actions if a[0] == "append_stream")
    assert "```md\n<!-- keep-visible -->\n```" in streamed
