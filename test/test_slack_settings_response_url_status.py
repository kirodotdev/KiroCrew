"""A settings button whose ``response_url`` answers non-2xx falls back to ``chat.update``.

An expired, overused or rate-limited ``response_url`` replies 4xx or 5xx rather
than raising, so the agent picker and the allowlist / channel remove buttons must
read the status before trusting the replace. Otherwise the card keeps its stale
buttons and nothing tries ``chat.update``.

Config writes land in the per-test ``KIROCREW_HOME`` that ``conftest.py`` pins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_interactions_coverage_helpers import _mock_aiohttp_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _restore_module_globals  # noqa: F401
from slack_interactions_coverage_helpers import orch_fixture  # noqa: F401
from slack_interactions_coverage_helpers import _payload

from kiro_crew.slack import handler as sh
from kiro_crew.slack import interactions as ix


def _click(name: str, payload: dict):
    if name == "agent":
        action = {"action_id": "mc_agent_select", "selected_option": {"value": "off"}}
        return ix._handle_agent_select(payload, action, "C1", "m1", "U1")
    if name == "allowlist":
        return ix._handle_allowlist_remove(payload, {"value": "U9"}, "C1", "m1", "U1")
    return ix._handle_channel_remove(payload, {"value": "C9"}, "C1", "m1", "U1")


@pytest.mark.parametrize("status", [404, 429, 500])
@pytest.mark.parametrize("name", ["agent", "allowlist", "channel"])
@pytest.mark.asyncio
async def test_a_refused_response_url_falls_back_to_chat_update(
    name: str,
    status: int,
    orch: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    _mock_aiohttp: AsyncMock,
) -> None:
    monkeypatch.setattr(sh, "_set_default_agent", MagicMock())
    orch._allowed_users = {"U9"}
    orch._tracking_channels = {"C9"}
    _mock_aiohttp.post.return_value.status = status
    await _click(name, _payload(response_url="https://hooks.slack.com/z"))
    _mock_aiohttp.post.assert_awaited_once()
    orch.slack.update_message.assert_awaited_once()
    assert orch.slack.update_message.await_args.args[:2] == ("C1", "m1")
