"""Unit tests for the /side parent-snapshot context builder.

The parent snapshot is what gives a side conversation the context of the
currently open session. The regression these lock: when the transcript
exceeds the char cap, the snapshot must keep the *most recent* turns (what a
follow-up question is about), not the oldest ones.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.dashboard import side_context as sc
from kiro_crew.dashboard.side_state import SideState


def _slot(messages):
    """Minimal slot stub: build_side_message only reads .messages and ._side."""
    return SimpleNamespace(
        messages=messages,
        _side=SideState(open=True),
        key="parent",
        agent=None,
    )


def _msg(role, content):
    return {"role": role, "content": content}


def test_side_prompt_states_the_tool_boundary_without_fake_remediation():
    """The prompt describes the boundary the execution path enforces.

    The turn runs under ``ToolApprovalPolicy.READ_ONLY``: reads run without
    asking, everything that changes state is refused. The prompt must say the
    same thing — a prompt that forbids every tool makes a compliant model
    redirect a read-backed question to the main chat instead of reading, and a
    prompt that permits more than the gate does makes the model claim a tool
    is unconfigured and suggest enabling it. An explicit request for a change
    is still refused and must point to the main chat.
    """
    prompt = sc.build_side_message(_slot([]), "use Builder MCP", is_first_turn=True)

    assert "lookups work here, but changes don't" in prompt
    # The live pod run showed a model refusing `pwd` as a "command" until the
    # prompt named read-only shell commands as allowed.
    assert "read-only shell commands such as ls, cat, pwd and git status run here without asking" in prompt
    assert "MCP tools are refused here" in prompt
    assert "even when the user explicitly requests them" in prompt
    assert "use the main chat to take action" in prompt
    assert "Never claim that a tool is unconfigured" in prompt
    assert "tool and MCP execution is unavailable" not in prompt
    assert "Do not emit tool calls" not in prompt


def test_short_session_includes_all_turns_in_order():
    """A session under the cap carries every user/assistant turn, chronological."""
    messages = [
        _msg("user", "deploy the alpha stack"),
        _msg("assistant", "Deployed alpha stack."),
        _msg("user", "now run the integ tests"),
    ]
    out = sc.build_side_message(
        _slot(messages), "what did we just deploy?", is_first_turn=True
    )
    assert sc._PARENT_SNAPSHOT_HEADER in out
    assert "User: deploy the alpha stack" in out
    assert "Assistant: Deployed alpha stack." in out
    assert "User: now run the integ tests" in out
    # chronological: the deploy turn precedes the integ-test turn.
    assert out.index("deploy the alpha stack") < out.index("now run the integ tests")


def test_long_session_retains_most_recent_turns():
    """When the transcript exceeds the cap, keep the tail, drop the head.

    A follow-up almost always concerns the recent turns; the old forward
    iterate-and-break kept the oldest turns and starved the model of the
    relevant context.
    """
    messages = []
    for i in range(200):
        messages.append(_msg("user", f"older question {i} " + "x" * 400))
        messages.append(_msg("assistant", f"older answer {i} " + "y" * 400))
    messages.append(_msg("user", "the prod role ARN is arn:aws:iam::123:role/CriticalRole"))
    messages.append(_msg("assistant", "Noted, pinned CriticalRole as the prod principal."))

    out = sc.build_side_message(
        _slot(messages), "what was the prod role ARN?", is_first_turn=True
    )

    # the decisive recent turns survive; the oldest are dropped under the cap.
    assert "CriticalRole" in out
    assert "older question 0" not in out
    # retained block stays chronological (user turn before its assistant reply).
    assert out.index("the prod role ARN is") < out.index("Noted, pinned CriticalRole")


def test_no_parent_turns_omits_snapshot_block():
    """Tool/error-only (no user/assistant) transcript yields no parent block."""
    messages = [
        _msg("permission", "approve tool call?"),
        _msg("chunk", "streaming fragment"),
    ]
    out = sc.build_side_message(_slot(messages), "hello?", is_first_turn=True)
    assert sc._PARENT_SNAPSHOT_HEADER not in out


def test_side_prompt_states_no_tools_on_a_harness_without_the_read_only_allowance():
    """Off ``ACP_BACKENDS_SIDE_READONLY`` the turn runs REJECT_ALL, and the prompt
    says so — tools are unavailable, changes go to the main chat — so the model
    neither attempts a read the gate would refuse nor claims a tool is missing."""
    prompt = sc.build_side_message(
        _slot([]), "use Builder MCP", is_first_turn=True, tools_available=False
    )

    assert "tools are unavailable here" in prompt
    assert "even when the user explicitly requests them" in prompt
    assert "use the main chat to take action" in prompt
    assert "Never claim that a tool is unconfigured" in prompt
    assert "lookups work here, but changes don't" not in prompt
    assert "run without asking" not in prompt
