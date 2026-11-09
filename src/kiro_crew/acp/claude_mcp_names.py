"""The name Claude Code gives an MCP tool, which its permission rules must spell.

Claude Code names a tool of an MCP server ``mcp__<server>__<tool>``, writing each
character of either half outside ``[A-Za-z0-9_-]`` as ``_``. A ``permissions.deny``
rule is matched against that name, so a rule spelled with the raw server name of a
server called ``dot.probe`` names a tool that does not exist and denies nothing.
Measured on claude-agent-acp 0.94.0 delegating to the host ``claude``: the tool
arrived as ``mcp__dot_probe__dot_hidden``, the rule spelled
``mcp__dot.probe__dot_hidden`` did not stop it, and the rule spelled the way this
module spells it did.

Two raw names can fold onto one (``dot.probe`` and ``dot_probe``). A deny rule
then also covers the other server's tool of the same name, which is the safe
direction for a rule that only ever takes a tool away.
"""

from __future__ import annotations

import string

#: The characters Claude Code keeps in each half of an MCP tool name. ASCII only:
#: a non-ASCII letter or digit is written ``_`` like any other character.
_CLAUDE_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_-")


def _claude_name_part(name: str) -> str:
    """*name* with each character Claude Code does not keep written as ``_``."""
    return "".join(c if c in _CLAUDE_NAME_CHARS else "_" for c in name)


def claude_mcp_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>`` in the spelling Claude Code gives the tool."""
    return f"mcp__{_claude_name_part(server)}__{_claude_name_part(tool)}"
