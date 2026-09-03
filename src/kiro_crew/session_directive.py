"""Session-directive protocol — stateless session-bound MCP tools.

Some KiroCrew MCP tools act on *the session that called them* — arm a monitor
loop, set a chat slot's project, render a follow-up card. In the unpooled
(gateway-off) topology the MCP server cannot know which session is calling
(one ``kirocrew-core`` serves the whole runtime, and the ``/proc`` walk is
refused because a session-sharing subagent would misattribute to its parent).

Rather than invent a per-process identity source, these tools stay STATELESS:
the tool VALIDATES its arguments and returns a *directive* — a human-readable
confirmation line plus a machine-readable marker carrying the validated payload
(and NO session key). A session-aware consumer that processes the tool result
decodes the marker and applies the effect against ITS OWN session, then keeps
the marker out of what it stores or renders. There are TWO consumers, one per
turn loop: :func:`dashboard.chat_runner._run_chat`'s ``EVENT_TOOL_RESULT``
handler (the dashboard-driven surfaces, which own ``slot.key``), and
:class:`messaging.driver.TurnDriver` (the standalone channel transports —
Telegram, Discord, standalone Slack, iMessage, Teams, Webex, WeCom, Weixin —
whose dispatchers inject a consumer bound to the turn's session key via
``messaging.dispatch.build_directive_consumer``). Both funnel into
``dashboard.session_directive_apply.apply_session_directive``, so the security
boundaries live in one place.

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
import re
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
        "monitor_watch",
        "monitor_update",
        "monitor_stop",
        "autonudge_stop",
        "set_project",
        "suggest_followup",
        "ask_question",
        "reset_conversation",
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
# A server-qualified canonical tool name separates server from tool with a RUN
# of underscores, and the run length is transport-specific ("___" from kiro-cli,
# "__" in the canonical MCP prefix form). Matching the run rather than one
# spelling is what lets :func:`match_tool` accept both without widening to a
# bare suffix match. Mirrors ``channel._MCP_SEPARATOR_RE``.
_MCP_SEPARATOR_RE = re.compile(r"_{2,}")


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


def has_marker(text: str | None) -> bool:
    """True iff *text* carries the directive marker sentinel.

    Used ONLY for diagnostics — never to authorize anything. A marker is
    model-visible text, so its presence proves nothing about provenance; what it
    does tell an operator is that a directive was EXPECTED here, which is the
    signal that made an identity-gate drop invisible (the gate returns ``""``
    with no log, so a backend that omits ``_meta.kiro`` produced silence rather
    than a diagnosis).
    """
    return bool(text) and _SENTINEL in (text or "")


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


def peek(text: str) -> tuple[str, dict[str, Any]] | None:
    """Parse the marker's ``(kind, args)`` with NO identity check, or ``None``.

    A SELECTOR, never a grant — and the distinction is the whole reason this is
    separate from :func:`decode`. ``decode`` answers "may I apply what this text
    says?" and therefore demands the trusted tool identity. This answers "which
    parked record is this frame talking about?", and its answer is only ever used
    to look one up: a caller matches it against a record the TOOL validated and
    the gateway parked, then applies the RECORD's payload. Nothing read here
    reaches an effect, so a model editing the JSON can only fail to find a record
    — it cannot smuggle a value past the tool's validation.

    Consequently ``kind`` is returned unvalidated except for being a known
    directive tool: an unknown kind can match no record anyway, and rejecting it
    here would only duplicate the lookup's own failure.
    """
    if not text:
        return None
    idx = text.find(_SENTINEL)
    if idx < 0:
        return None
    line = text[idx + len(_SENTINEL):].split("\n", 1)[0]
    try:
        block = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(block, dict):
        return None
    kind = block.get("kind")
    if not isinstance(kind, str) or kind not in DIRECTIVE_TOOLS:
        return None
    args = block.get("args")
    return kind, (args if isinstance(args, dict) else {})


def match_tool(raw: str) -> str:
    """Return the directive-tool name a recorded CANONICAL tool name refers to,
    or ``""``.

    ``raw`` MUST be the trusted ``_meta.kiro.toolName`` (NOT the LLM-authored
    title). For an MCP tool that name is the bare tool name (``"monitor_start"``);
    some transports server-qualify it, and the separator is NOT one fixed
    spelling: kiro-cli reports ``"<server>___<name>"`` while the canonical MCP
    prefix form is ``"mcp__<server>__<name>"``. Split on the LAST run of two or
    more underscores so BOTH qualified forms resolve — the same normalization
    ``channel._blocked_tool_named`` already applies for the same reason, which
    this deliberately mirrors rather than re-inventing.

    Still nothing wider than that: the separator must be a run of >= 2
    underscores, so a crafted path/namespace tail (``"a/b/monitor_start"``,
    ``"do_monitor_start"``) cannot smuggle a directive name in. The tool half
    never authenticates the SERVER either way — :func:`directive_tool_for`
    checks ``mcp_server_name`` independently, and that is the check a
    third-party server fails.
    """
    if not raw:
        return ""
    if raw in DIRECTIVE_TOOLS:
        return raw
    parts = _MCP_SEPARATOR_RE.split(raw)
    if len(parts) > 1 and parts[-1] in DIRECTIVE_TOOLS:
        return parts[-1]
    return ""


def directive_tool_for(mcp_server_name: str, tool_name: str) -> str:
    """Return the directive-tool name for a recorded tool CALL, or ``""``.

    THE forgery-gate identity predicate, spelled once: a directive-tool name is
    honoured ONLY when the call's trusted ``_meta.kiro`` identity says it was
    served by Kiro Crew's OWN core MCP server (:data:`CORE_MCP_SERVER`) AND its
    CANONICAL tool name resolves to a :data:`DIRECTIVE_TOOLS` member via
    :func:`match_tool`. Both ``EVENT_TOOL_CALL`` consumers (the dashboard's
    ``chat_runner`` and ``messaging.driver.TurnDriver``) MUST call this instead
    of inlining the two checks, so the boundary cannot silently diverge.

    Both arguments MUST come from the out-of-band ``_meta.kiro`` channel
    (``mcpServerName`` / ``toolName``) — never the LLM-authored title. A shell
    tool has no MCP server name and a canonical tool name like
    ``execute_bash``, so it resolves to ``""``; so does a third-party MCP
    server that merely exposes a tool named e.g. ``monitor_start``. Absent
    identity (empty server name) fails closed.
    """
    if mcp_server_name != CORE_MCP_SERVER:
        return ""
    return match_tool(tool_name or "")


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
