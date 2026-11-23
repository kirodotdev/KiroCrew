"""How each harness answers a tool call it could not resolve.

A model that calls a tool by a name its harness does not offer (a bare
``work_report`` where the harness names it ``mcp__kirocrew_work__work_report``)
gets an answer from the harness itself, and the call never reaches a server.
Each harness frames that call as a ``tool_call`` of kind ``other`` titled with the
name the model called, carrying no server and no tool identity; what differs is the
status it ends the call with and the text it answers. This table holds those, each
read off a live capture (the pi, OpenCode and Claude frames are in
``test/fixtures/unresolved_work_tool_frames.json``):

* KAS: ``failed``, ``Tool "<name>" is not available.``
  (:data:`kiro_crew.agent_sdk.kas_tool_names.KAS_TOOL_UNAVAILABLE_RE`).
* pi (pi-acp 0.0.34 over pi 1.4.0): ``failed``, ``Tool <name> not found``.
* Claude (claude-agent-acp 0.94.0): ``failed``, Claude Code's own
  ``<tool_use_error>Error: No such tool available: <name></tool_use_error>``. The
  content text wraps it in a ```` ``` ```` fence; ``rawOutput`` is bare.
* OpenCode (``opencode acp`` 1.19.1): ``completed`` -- it hands the call to its own
  ``invalid`` tool -- and an answer that STARTS ``The arguments provided to the tool
  are invalid: Model tried to call unavailable tool '<name>'.`` and goes on to list
  every tool the session has, so only its head is read.

A harness with no row is one whose answer has not been captured, or that sends no
frame at all: codex-acp 2.3.0 answers the model ``unsupported call: <name>`` and
puts nothing on the ACP wire. Identity is the provider's own label, a positive
comparison (harness-parity H5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kiro_crew.agent_sdk.kas_tool_names import KAS_TOOL_UNAVAILABLE_RE

__all__ = ["UnresolvedToolAnswer", "unresolved_tool_answer"]


@dataclass(frozen=True)
class UnresolvedToolAnswer:
    """One harness's answer to a call it could not resolve.

    ``pattern``'s ``name`` group is the name the model called, verbatim. With
    ``whole`` the pattern is the entire output; without it, only the output's head.
    """

    status: str
    pattern: re.Pattern[str]
    whole: bool


_KAS = UnresolvedToolAnswer("failed", KAS_TOOL_UNAVAILABLE_RE, whole=True)
_PI = UnresolvedToolAnswer(
    "failed", re.compile(r"Tool (?P<name>[^\r\n]{1,256}) not found"), whole=True
)
_OPENCODE = UnresolvedToolAnswer(
    "completed",
    re.compile(
        r"The arguments provided to the tool are invalid: "
        r"Model tried to call unavailable tool '(?P<name>[^'\r\n]{1,256})'\."
    ),
    whole=False,
)
_CLAUDE = UnresolvedToolAnswer(
    "failed",
    re.compile(
        r"(?P<fence>```\n)?<tool_use_error>Error: No such tool available: "
        r"(?P<name>[^<\r\n]{1,256})</tool_use_error>(?(fence)\n```)"
    ),
    whole=True,
)


def unresolved_tool_answer(provider_type: str | None) -> UnresolvedToolAnswer | None:
    """The answer the harness labelled *provider_type* gives an unresolved call, or
    ``None`` when its answer is not known.

    The label table lives in the ACP layer; it is read here, behind the agent-SDK
    boundary, and only when asked.
    """
    if not provider_type:
        return None
    from kiro_crew.acp.types import (
        PROVIDER_LABEL_CLAUDE,
        PROVIDER_LABEL_KAS,
        PROVIDER_LABEL_OPENCODE,
        PROVIDER_LABEL_PI,
    )

    return {
        PROVIDER_LABEL_KAS: _KAS,
        PROVIDER_LABEL_PI: _PI,
        PROVIDER_LABEL_OPENCODE: _OPENCODE,
        PROVIDER_LABEL_CLAUDE: _CLAUDE,
    }.get(provider_type)
