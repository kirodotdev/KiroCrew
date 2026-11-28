"""Test that OPTIONS tags in subagent-triggered responses render as Slack buttons."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_handler_helpers import hide_slack_timing_footer

import kiro_crew.slack.gateway as gw
from conftest import MockSlackClient
from kiro_crew.slack.format import (
    OPTIONS_ACTION_PREFIX,
    OPTIONS_SEVERAL_ACTION,
    build_options_blocks,
    extract_options,
)
from kiro_crew.subagent import SubagentInfo


class TestSubagentOptionsExtraction:
    """OPTIONS tags in subagent-triggered LLM responses must be extracted and rendered."""

    def test_extract_options_from_subagent_response(self):
        """Regression: subagent completion responses had OPTIONS posted as plain text."""
        response = (
            "That subagent got confused.\n\n"
            "Want me to commit what we have and raise a CR?\n\n"
            "[OPTIONS: Commit and raise CR | Spawn adversarial review | Just commit]"
        )
        from kiro_crew.slack.format import to_slack_mrkdwn

        reply_text = to_slack_mrkdwn(response)
        reply_text, options = extract_options(reply_text)

        assert options == ["Commit and raise CR", "Spawn adversarial review", "Just commit"]
        assert "[OPTIONS:" not in reply_text

    def test_options_blocks_appended_to_footer(self):
        """Extracted options should produce one Block Kit button per choice."""
        options = ["Choice A", "Choice B"]
        footer_blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": "⏱ 5s"}]}]
        footer_blocks.extend(build_options_blocks(options))

        assert len(footer_blocks) == 2
        actions = footer_blocks[1]
        assert actions["type"] == "actions"
        assert [el["action_id"] for el in actions["elements"]] == [
            f"{OPTIONS_ACTION_PREFIX}0",
            f"{OPTIONS_ACTION_PREFIX}1",
            OPTIONS_SEVERAL_ACTION,
        ]

    def test_no_options_no_extra_blocks(self):
        """When response has no OPTIONS tag, footer should not get option blocks."""
        response = "Here is my response with no options."
        _, options = extract_options(response)
        assert options == []

        footer_blocks = [{"type": "context", "elements": []}]
        if options:
            footer_blocks.extend(build_options_blocks(options))
        assert len(footer_blocks) == 1


#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30


def _slack_gateway(reply: str) -> Any:
    """A gateway whose Slack parent ``slack:thread1`` injects and gets *reply* back."""
    gateway = gw.GatewayOrchestrator.__new__(gw.GatewayOrchestrator)
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, True))
    sessions.get_thread = MagicMock(return_value="thread1")
    sessions.get_channel = MagicMock(return_value="C1")
    sessions.reset = AsyncMock()
    gateway.sessions = sessions
    gateway.ctx_builder = None
    gateway.conv_log = None
    gateway.dashboard_state = None
    gateway.slack = MockSlackClient()
    gateway._owner_id = "U1"
    gateway._cron_injecting = {}
    gateway.cron_svc = None
    gateway.subagent_mgr = MagicMock()
    gateway.subagent_mgr.running = []
    gateway.subagent_mgr.queued_count_for_async = AsyncMock(return_value=0)
    gateway._channel_reply_link = MagicMock(return_value=None)
    gateway._deliver_channel_reply = AsyncMock()
    gateway._inject_with_retry = AsyncMock(return_value=reply)
    gateway._remember_options = MagicMock()
    return gateway


async def _inject(gateway: Any) -> None:
    info = SubagentInfo(id="child-1", task="t", parent_session_key="slack:thread1")
    info.result, info.done = "done", True
    with patch("kiro_crew.slack.gateway._MAX_INJECT_ATTEMPTS", 1):
        await asyncio.wait_for(
            gateway._inject_subagent_into_channel_parent(
                info, announce="a", body="b", title="t", parent_key="slack:thread1", carried=()
            ),
            _HANG_CEILING_S,
        )


@pytest.mark.asyncio
async def test_a_hidden_timing_footer_posts_no_footer_after_a_subagent_reply(monkeypatch, tmp_path):
    hide_slack_timing_footer(monkeypatch, tmp_path)
    gateway = _slack_gateway("the child is done")

    await _inject(gateway)

    actions = gateway.slack.actions
    assert any(a[0] == "post" and "the child is done" in a[1]["text"] for a in actions)
    assert [a for a in actions if a[0] == "blocks"] == [], "no footer may post"
    gateway._remember_options.assert_not_called()


@pytest.mark.asyncio
async def test_a_hidden_timing_footer_still_posts_a_subagent_replys_choices(monkeypatch, tmp_path):
    hide_slack_timing_footer(monkeypatch, tmp_path)
    gateway = _slack_gateway("pick one [OPTIONS: alpha | beta]")

    await _inject(gateway)

    (footer,) = [kw for name, kw in gateway.slack.actions if name == "blocks"]
    assert [b["type"] for b in footer["blocks"]] == ["actions"]
    assert footer["text"] == "Options" and "Finished in" not in json.dumps(footer)
    gateway._remember_options.assert_called_once()
    assert gateway._remember_options.call_args.args[3] == ["alpha", "beta"]
