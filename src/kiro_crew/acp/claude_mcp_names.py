"""The name Claude Code gives an MCP tool, which its permission rules must spell.

Claude Code names a tool of an MCP server ``mcp__<server>__<tool>``, writing each
character of either half outside ``[A-Za-z0-9_-]`` as ``_``. A ``permissions.deny``
rule is matched against that name, so a rule spelled with the raw server name of a
server called ``dot.probe`` names a tool that does not exist and denies nothing.
Measured on claude-agent-acp 0.94.0 delegating to the host ``claude``
(``test/fixtures/acp_frames/claude/member-tool-limits-live.jsonl``): the tool
arrived as ``mcp__dot_probe__dot_hidden``, the rule spelled
``mcp__dot.probe__dot_hidden`` did not stop it, and the rule spelled the way this
module spells it did.

Two raw names can fold onto one (``dot.probe`` and ``dot_probe``). A deny rule
then also covers the other server's tool of the same name, which is the safe
direction for a rule that only ever takes a tool away.
"""

from __future__ import annotations

import re

#: The characters Claude Code keeps in each half of an MCP tool name.
_CLAUDE_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def claude_mcp_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` in the spelling Claude Code gives the tool."""
    return (
        f"mcp__{_CLAUDE_UNSAFE_NAME_CHARS.sub('_', server)}"
        f"__{_CLAUDE_UNSAFE_NAME_CHARS.sub('_', tool)}"
    )


def claude_mcp_server_rule(server: str) -> str:
    """``mcp__<server>``, the rule naming every tool of *server*, spelled the same way."""
    return f"mcp__{_CLAUDE_UNSAFE_NAME_CHARS.sub('_', server)}"
