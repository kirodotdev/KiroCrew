"""The ``code_channel_*`` tools: a bound session's own Slack code channel.

``schemas()`` is the ADVERTISEMENT half (name, model-facing description, the
JSON Schema a call is built against) and ``HANDLERS`` maps each name to the
function that runs it; ``test_mcp_tool_registry`` holds the two together.

Every handler is the same thin forwarder: resolve the caller STRICTLY
(``mcp_core.require_strict_session_key`` -- never the ``/proc`` walk, under which
a sub-agent would resolve to its parent's slot) and POST the call to the
gateway's ``/api/slack/code-channel/tool`` with that key. The gateway attests the
key, derives the channel from it and decides everything else
(:mod:`kiro_crew.slack.code_channels.tools`); no argument here names a channel.

Listing follows the ``slack.code_channels`` setting (:func:`advertised`): with it
off ``tools/list`` is exactly what it is without code channels. Whether THIS
session is bound to a channel cannot be known when kiro-cli lists tools once per
session (a pooled server answers many sessions, and a warm-pool process may list
before it has one), so a call from an unbound session is refused at the gateway
with a message saying so.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.catalog.code_channels import (  # noqa: F401
    _PIN,
    _obj,
    _toggle_on,
    advertised,
    schemas,
)

logger = logging.getLogger(__name__)

_ROUTE = "/api/slack/code-channel/tool"


def _forward(name: str, args: dict[str, Any]) -> str:
    session_key, refusal = mcp_core.require_strict_session_key(
        f"Error: {name} acts on the calling session's own Slack code channel, and this "
        "session could not be identified."
    )
    if refusal:
        return refusal
    reply = mcp_core._post(
        _ROUTE,
        {"tool": name, "args": args if isinstance(args, dict) else {}},
        session_key=session_key,
        timeout=60,
    )
    if not isinstance(reply, dict):
        return f"Error: {name} got no answer from the gateway."
    text = reply.get("text")
    if isinstance(text, str) and text:
        return text if reply.get("ok") or text.startswith("Error") else f"Error: {text}"
    error = reply.get("error") or "the gateway did not complete the call"
    return f"Error: {error}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    tool["name"]: _forward for tool in schemas()
}
