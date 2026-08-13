"""Session-directive protocol — stateless session-bound MCP tools.

Some KiroCrew MCP tools act on *the session that called them* — arm a monitor
loop, set a chat slot's project, render a follow-up card. In the unpooled
(gateway-off) topology the MCP server cannot know which session is calling
(one ``kirocrew-core`` serves the whole runtime, and the ``/proc`` walk is
refused because a session-sharing subagent would misattribute to its parent).

Rather than invent a per-process identity source, these tools stay STATELESS:
the tool VALIDATES its arguments and returns a *directive* — a human-readable
confirmation line plus a machine-readable marker carrying the validated payload
(and NO session key). The session-aware consumer that processes the tool result
(:func:`dashboard.chat_runner._run_chat`'s ``EVENT_TOOL_RESULT`` handler, which
runs for every interactive surface and owns ``slot.key``) decodes the marker and
applies the effect against ITS OWN session, then strips the marker from the
stored transcript.

Subagent isolation is therefore STRUCTURAL, not cryptographic: a subagent's
tool result flows through the subagent's own runner, so it can only ever bind to
the subagent's session — never its parent's. There is no walk to get wrong.

FORGERY: the marker payload is model-visible (it comes back as the tool result
text), so a model *could* emit the literal bytes. The consumer defends by
honouring a directive ONLY when the tool call it arrived under was recorded — by
KiroCrew observing the tool CALL — as an MCP-served call whose CANONICAL name
(``_meta.kiro.toolName``, with ``_meta.kiro.mcpServerName`` set) is one of
:data:`DIRECTIVE_TOOLS`. That identity comes from kiro-cli's out-of-band ``_meta``
channel, NOT the ``title`` (which is LLM-authored prose for shell tools — a shell
command titled ``"monitor_start"`` whose stdout forges the marker must NOT be
honoured). The gate fails closed when ``_meta`` identity is absent. The payload
never carries a session key (the session is supplied by the consumer), and the
consumer additionally refuses native-sub-agent tool calls, which surface as flat
events in the parent loop but have no independently bindable slot. A model
echoing the marker from any non-directive (or non-MCP) tool resolves to no
directive tool and is ignored.
"""

from __future__ import annotations

import json
from typing import Any

# The stateless, session-bound tools. ``ask_question`` joins
# them as a NON-BLOCKING card: the consumer broadcasts a question card (with no
# ``ask_id``) to its own slot and the agent ends its turn; the user's answer
# arrives as an ordinary next message that resumes the session (the full
# transcript/context reloads), rather than blocking the turn on a server-side
# wait. This drops only the mid-turn pause — never a capability.
DIRECTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "monitor_start",
        "monitor_update",
        "autonudge_stop",
        "set_project",
        "suggest_followup",
        "ask_question",
    }
)

# The MCP server name KiroCrew registers its own tools under (kiro-cli reports
# it in ``_meta.kiro.mcpServerName``). The consumer honours a directive ONLY
# from a call served by THIS server — a third-party MCP server that happens to
# expose a tool named e.g. ``monitor_start`` must never be able to drive a
# session directive. (A downstream fork adjusts this one constant to its own
# server name.)
CORE_MCP_SERVER = "kirocrew-core"

# Marker begins a line; the remainder of that line is the compact-JSON payload
# ``{"kind": <tool>, "args": {...}}``. Placed on its own trailing line after the
# human-readable confirmation so a consumer-less surface still shows sane text.
#
# ASCII-ONLY, deliberately. This previously carried a leading U+2063 INVISIBLE
# SEPARATOR so the marker rendered invisibly, and that made every directive
# silently fail: ``validation.build_tool_response`` — the single exit point for
# all tool responses — strips category ``Cf``, so the prefix was destroyed
# before the response left the MCP server and ``decode`` could no longer match.
# A machine-facing framing token must not depend on characters that sanitisers,
# Unicode normalisers and transports all legitimately rewrite.
_SENTINEL = "[[KIROCREW_SESSION_DIRECTIVE]]"

# The ACP tool-result parser truncates each output part at 4000 chars
# (``acp/_dispatch.py`` ``str(text)[:4000]``). The marker is the TAIL of the
# result, so an oversized payload loses the marker entirely — the effect would be
# silently dropped after the model was told the request was made. Encode refuses
# above this bound instead, leaving headroom under the transport cap.
MAX_DIRECTIVE_CHARS = 3800

# Stamped on an oversized :func:`encode` result INSTEAD of the directive marker,
# so the consumer can tell a deliberate refusal apart from a marker that was lost
# in transport. Both cases decode to "no directive", but only the second is a
# bug, and the consumer's diagnostic for a lost marker is a WARNING that exists
# to catch rawOutput-envelope escaping regressions — a by-design refusal firing
# it trains operators to ignore the one signal that matters.
#
# Forgery-inert by construction: unlike the directive marker this token carries
# no payload and grants no effect, so a model emitting the literal bytes can only
# change how a log line reads, never what gets applied.
_REFUSAL_SENTINEL = "[[KIROCREW_SESSION_DIRECTIVE_REFUSED]]"


def encode(kind: str, args: dict[str, Any], human: str) -> str:
    """Build a tool-result string: a human confirmation + the directive marker.

    ``kind`` MUST be in :data:`DIRECTIVE_TOOLS`. ``args`` is the VALIDATED
    payload the consumer needs to apply the effect (never a session key).

    When the encoded directive would exceed :data:`MAX_DIRECTIVE_CHARS`, returns a
    plain ``"Error: …"`` string carrying NO directive marker: the caller returns it
    to the model verbatim, so an oversized request fails LOUDLY (and is audited
    failed) instead of being silently truncated past its marker and dropped. The
    refusal is tagged with :data:`_REFUSAL_SENTINEL` so the consumer reports it as
    a refusal rather than as a lost marker (see :func:`is_refusal`).
    """
    payload = json.dumps({"kind": kind, "args": args}, separators=(",", ":"), default=str)
    out = f"{human}\n{_SENTINEL}{payload}"
    if len(out) > MAX_DIRECTIVE_CHARS:
        return (
            f"Error: {kind} arguments are too large to deliver "
            f"({len(out)} chars, limit {MAX_DIRECTIVE_CHARS}). Shorten them "
            "(e.g. a briefer message / fewer items) and call the tool again — "
            f"nothing was applied.\n{_REFUSAL_SENTINEL}"
        )
    return out


def is_refusal(text: str | None) -> bool:
    """True iff *text* is an :func:`encode` refusal — a validated directive that
    was deliberately NOT emitted because its payload exceeded
    :data:`MAX_DIRECTIVE_CHARS`.

    Distinguishes "refused before delivery, and the model was told" from "a marker
    was expected and did not arrive", which are otherwise indistinguishable at the
    consumer: both decode to ``None``.
    """
    return bool(text) and _REFUSAL_SENTINEL in (text or "")


def decode(text: str, expected_tool: str) -> dict[str, Any] | None:
    """Return the directive ``args`` iff *text* carries a well-formed marker AND
    *expected_tool* (the name KiroCrew recorded for this tool call) matches the
    directive kind and is a known directive tool. Returns ``None`` otherwise —
    the forgery gate.
    """
    if expected_tool not in DIRECTIVE_TOOLS or not text:
        return None
    idx = text.find(_SENTINEL)
    if idx < 0:
        return None
    line = text[idx + len(_SENTINEL):].split("\n", 1)[0]
    try:
        block = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(block, dict) or block.get("kind") != expected_tool:
        return None
    args = block.get("args")
    return args if isinstance(args, dict) else {}


def match_tool(raw: str) -> str:
    """Return the directive-tool name a recorded CANONICAL tool name refers to,
    or ``""``.

    ``raw`` MUST be the trusted ``_meta.kiro.toolName`` (NOT the LLM-authored
    title). For an MCP tool that name is the bare tool name (``"monitor_start"``);
    some transports server-qualify it as ``"<server>___<name>"``. Accept exact
    membership plus that single ``___`` split — nothing wider, so a crafted
    string cannot smuggle a directive name in as a path/namespace tail.
    """
    if not raw:
        return ""
    if raw in DIRECTIVE_TOOLS:
        return raw
    if "___" in raw:
        tail = raw.rsplit("___", 1)[-1]
        if tail in DIRECTIVE_TOOLS:
            return tail
    return ""


def strip_marker(text: str) -> str:
    """Remove the directive or refusal marker line from *text* for transcript display."""
    idx = -1
    for sentinel in (_SENTINEL, _REFUSAL_SENTINEL):
        found = text.find(sentinel)
        if found >= 0 and (idx < 0 or found < idx):
            idx = found
    if idx < 0:
        return text
    # Drop the marker and any immediately-preceding blank separator line.
    head = text[:idx].rstrip("\n")
    return head
