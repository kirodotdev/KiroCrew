"""A claude session starts its main thread AS the Claude Code agent its spec names.

A Crew-started Claude Code session carries no agent, so the agent's system
prompt, tool restrictions and model do not apply and Claude Code leaves the
``SessionStart`` payload's ``agent_type`` unset (anything keying on it cannot
tell which agent is running). An agent whose spec sets ``claudeCodeAgent`` has
that name forwarded to the SDK ``agent`` option via
``_meta.claudeCode.options.agent``; an agent that does not, or whose spec is
unreadable, is left agentless as before.
"""

from __future__ import annotations

import json
from pathlib import Path

from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

_AGENT = "my-agent"
_CLAUDE_AGENT = "local-MyPackage-my-agent"


def _write_spec(agents_dir: Path, *, claude_code_agent: str | None) -> None:
    spec: dict = {"name": _AGENT, "prompt": "p", "tools": ["*"], "mcpServers": {}}
    if claude_code_agent is not None:
        spec["claudeCodeAgent"] = claude_code_agent
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec), encoding="utf-8")


def _options(work_dir: Path) -> dict:
    client = AcpClient(work_dir=work_dir, agent=_AGENT, acp_backend=ACP_BACKEND_CLAUDE)
    return client._claude_session_meta()["claudeCode"]["options"]


def test_the_spec_agent_rides_the_envelope(tmp_path, agents_dir):
    """``claudeCodeAgent`` becomes ``_meta.claudeCode.options.agent``."""
    _write_spec(agents_dir, claude_code_agent=_CLAUDE_AGENT)
    assert _options(tmp_path)["agent"] == _CLAUDE_AGENT


def test_a_spec_without_the_field_sends_no_agent(tmp_path, agents_dir):
    """No ``claudeCodeAgent`` leaves the main thread agentless."""
    _write_spec(agents_dir, claude_code_agent=None)
    assert "agent" not in _options(tmp_path)


def test_a_blank_field_is_treated_as_absent(tmp_path, agents_dir):
    """A whitespace-only value is dropped, never sent as an empty agent name."""
    _write_spec(agents_dir, claude_code_agent="   ")
    assert "agent" not in _options(tmp_path)


def test_an_unreadable_spec_is_silent(tmp_path, agents_dir):
    """A missing spec resolves to no agent rather than failing the session."""
    # No spec file written for _AGENT: agent_spec_snapshot returns None.
    assert "agent" not in _options(tmp_path)
