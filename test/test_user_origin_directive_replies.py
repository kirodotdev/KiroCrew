"""The replies of ``set_project``, ``chat_tag`` and ``suggest_followup`` name the
turns the user-origin gate refuses.

The gateway applies a directive after the tool has returned, so the tool's own
reply is all the model reads about the outcome. A Bot that calls one of these on
its heartbeat must read that such a turn is refused, not only that the change was
requested, as ``reset_conversation``'s reply already says.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import directive_origin, mcp_core, session_directive

_CALLS: dict[str, dict[str, Any]] = {
    "set_project": {"path": "/tmp"},
    "chat_tag": {"set_state": "review"},
    "suggest_followup": {"items": [{"title": "t", "description": "d", "prompt": "p"}]},
}

#: What each reply still says besides the refused turns.
_KEPT: dict[str, str] = {
    "set_project": "an invalid or sensitive path is rejected when this turn's result is processed.",
    "chat_tag": "a human-only tag or an unknown tag is refused.",
    "suggest_followup": "end your turn now.",
}


@pytest.mark.parametrize("tool", sorted(_CALLS))
def test_the_reply_names_the_refused_turns(tool: str) -> None:
    assert tool in directive_origin.USER_ORIGIN_DIRECTIVES
    result = mcp_core._call_tool_inner(tool, _CALLS[tool])
    assert isinstance(result, str)
    human = session_directive.strip_marker(result).lower()
    for turn in (
        "heartbeat",
        "goal check-in",
        "worker report",
        "monitor wake",
        "cron job",
        "sub-agent",
    ):
        assert turn in human, (turn, human)
    assert "is refused and changes nothing" in human
    assert "a person sent" in human
    assert _KEPT[tool] in human, (_KEPT[tool], human)
