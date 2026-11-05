"""The session-not-found reload in ``_inject_with_retry`` starts an attempt of its own.

When the live backend has lost the parent's session, the injection resets it and
claims it again in the same task, then replays the announce. That replay is a second
turn inside one claim, so it must not share the first turn's attempt: whatever the
first turn received inline is settled by that turn's own end (it did not land), and
the replay's landing confirms only what the replay received.

The reload is refused once a tool ran, so in production the first turn holds no
marks; the pin drives the attempt rule directly rather than leaning on that guard.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_spawn_sub_agents_duplicate_never_loss import (
    _channel_route,
    _mark_under_the_live_turn,
    _new_sessions,
    _returned_child,
    _run_as_far_as_it_goes,
)
from test_spawn_sub_agents_settle_at_release import _nested_gateway, _settled
from test_subagent_injection_live_turn import _HANG_CEILING_S


@pytest.mark.asyncio
async def test_the_reloaded_replay_does_not_confirm_what_the_lost_turn_received() -> None:
    """Attempt 1 received the nested reply carrying G, then the backend answered that
    it holds no session under that id. The reload resets and re-claims the session in
    the same task and replays X's announce, which lands. G's mark belongs to attempt
    1, which never landed, so the replay's landing must not confirm it: G is
    delivered as a turn of its own."""
    from kiro_crew.acp.client import AcpError

    sessions = _new_sessions()
    key = "slack:C1:1700000000.1"
    x = _returned_child("child-x", key)
    g = _returned_child("child-g", key)
    gateway = _nested_gateway(sessions, x, g)
    seen: dict[str, Any] = {"turns": []}

    async def stream(_client: Any, message: str, **kw: Any) -> str:
        seen["turns"].append(message)
        if len(seen["turns"]) == 1:
            seen["marked"] = _mark_under_the_live_turn(sessions, gateway.subagent_mgr, key, g.id)
            seen["g_route"] = _channel_route(gateway, g, key)
            await _run_as_far_as_it_goes(seen["g_route"])
            raise AcpError("Session not found")  # the backend lost the session
        on_complete = kw.get("on_complete")
        if on_complete is not None:
            on_complete(SimpleNamespace(stop_reason="end_turn"))
        return f"answered {message}"

    with patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream):
        await asyncio.wait_for(_channel_route(gateway, x, key), _HANG_CEILING_S)
        await _settled(gateway)

    assert seen["marked"] == {"child-g"}
    # The reset frees the permit, so G's own turn may run before or after the
    # replay re-claims the session; what matters is that it runs.
    assert seen["turns"][0] == "child-x done"
    assert sorted(seen["turns"]) == ["child-g done", "child-x done", "child-x done"], (
        "the reloaded replay's landing confirmed the inline result the lost turn "
        f"received (turns={seen['turns']!r})"
    )
    assert g._report_delivered is True
    assert gateway._inline_parked == {}
