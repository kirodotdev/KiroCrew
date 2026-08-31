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

import hashlib
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
        "section_marker",
    }
)

# The MCP server name KiroCrew registers its own tools under (kiro-cli reports
# it in ``_meta.kiro.mcpServerName``). The consumer honours a directive ONLY
# from a call served by THIS server — a third-party MCP server that happens to
# expose a tool named e.g. ``monitor_start`` must never be able to drive a
# session directive. (A downstream fork adjusts this one constant to its own
# server name.)
CORE_MCP_SERVER = "kirocrew-core"

#: A chapter break. Deliberately absent from ``RECALL_ROLES``, ``_QUESTION_RETIRING_ROLES``
#: and ``_PROMPT_ROLES``; ``test_section_marker_directive`` pins all three exclusions.
SECTION_MARKER_ROLE = "section_marker"

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
# Public alias. The transport layer (acp/_dispatch) has to locate the marker in a
# raw frame to repair a payload that arrived JSON-escaped, and reaching for the
# private name from another module would make that dependency invisible here.
SENTINEL = _SENTINEL

# The ACP tool-result parser truncates each output part at 4000 chars
# (``acp/_dispatch.py`` ``str(text)[:4000]``). The marker is the TAIL of the
# result, so an oversized payload loses the marker entirely — the effect would be
# silently dropped after the model was told the request was made. Encode refuses
# above this bound instead, leaving headroom under the transport cap.
MAX_DIRECTIVE_CHARS = 3800

# The ACP layer truncates a joined tool result to this many characters before the
# consumer sees it (``acp/_dispatch.py``, which imports this constant so the two
# cannot drift). It lives HERE because both markers are tail-anchored and so must
# survive it: :data:`MAX_DIRECTIVE_CHARS` is deliberately far below it, and
# :func:`tag_refusal` bounds its text against it. An unbounded refusal was
# reachable -- ``validate_tool_args`` echoes the argument NAME, which the model
# chooses, so a 9,000-character name produced a 9,087-character result whose tail
# tag the cut removed, and the decline read as a lost marker again (#8635).
MAX_TOOL_RESULT_CHARS = 8000

# Stamped on a directive tool's marker-less result INSTEAD of the directive
# marker, so the consumer can tell a deliberate refusal apart from a marker that
# was lost in transport. Both cases decode to "no directive", but only the second
# is a bug, and the consumer's diagnostic for a lost marker is a WARNING that
# exists to catch rawOutput-envelope escaping regressions — a by-design refusal
# firing it trains operators to ignore the one signal that matters.
#
# Two producers stamp it, and together they make the invariant total: a directive
# tool's result either carries the marker, or it is tagged a refusal. :func:`encode`
# stamps its own oversized-payload refusal, and :func:`refuse_if_markerless`
# stamps every OTHER marker-less return — a schema rejection before the handler
# ran, a "this session can never carry the effect" refusal, an empty required
# argument. Before that second producer existed, only the oversized case was
# distinguishable and every other refusal read as a lost marker (#8635).
#
# Forgery-inert by construction: unlike the directive marker this token carries
# no payload and grants no effect, so a model emitting the literal bytes can only
# change how a log line reads, never what gets applied.
_REFUSAL_SENTINEL = "[[KIROCREW_SESSION_DIRECTIVE_REFUSED]]"
# What :func:`neutralize_markers` substitutes for sentinel bytes that arrived from
# outside this process. Deliberately NOT parseable as either sentinel and not a
# prefix of one, so no consumer can be talked back into reading it as a marker.
_DEFANGED = "[[kirocrew-marker-removed]]"
# Substituted for the middle of an over-long refusal by :func:`tag_refusal`, so
# the elision is visible rather than a silent cut.
_ELIDED_NOTE = " [... {n} chars elided so the refusal tag survives delivery ...] "
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
        return tag_refusal(
            f"Error: {kind} arguments are too large to deliver "
            f"({len(out)} chars, limit {MAX_DIRECTIVE_CHARS}). Shorten them "
            "(e.g. a briefer message / fewer items) and call the tool again — "
            "nothing was applied."
        )
    return out


def neutralize_markers(text: str) -> str:
    """Defang any directive/refusal sentinel bytes in *text*.

    For text a caller KNOWS is not a directive — an error message, a rejection —
    that nonetheless interpolates content this process does not control. An
    argument NAME is such content: ``validate_tool_args`` reports an unknown field
    by echoing the key, and a key carrying the sentinel plus a JSON payload plus a
    newline makes the rejection string decode as a REAL directive under the
    genuine tool's own authenticated identity, bypassing the very validation that
    rejected it. Confirmed reachable, and reproducible on ``main`` — the marker is
    model-visible text, so a rejection that echoes model input can imitate one.

    Only the caller can know a string is not a directive, which is why this is not
    applied centrally to every tool result: doing that would defang the genuine
    marker too. Substitution rather than deletion so the operator reading a
    transcript still sees that something marker-shaped was submitted.
    """
    for sentinel in (_SENTINEL, _REFUSAL_SENTINEL):
        text = text.replace(sentinel, _DEFANGED)
    return text


def tag_refusal(text: str) -> str:
    """Stamp *text* as a deliberate refusal: no directive was emitted, and the
    model has been told so in *text* itself.

    Idempotent, and appended on its OWN LAST line because :func:`strip_marker`
    cuts from the sentinel to the end of the string — anything placed after it
    would be dropped from the transcript the user reads.

    Tail-anchored means transport-bounded: *text* is elided so the tag fits under
    :data:`MAX_TOOL_RESULT_CHARS`. Without that, a long enough decline lost its
    own tag to the ACP cut and read as a lost marker again — and the length is
    model-reachable, since a rejection echoes the argument name it rejected.
    """
    if is_refusal(text):
        return text
    room = MAX_TOOL_RESULT_CHARS - len(_REFUSAL_SENTINEL) - 1
    if len(text) > room:
        # Elide visibly, and from the MIDDLE: the head carries "Error: <field>"
        # and the tail carries the reason, so cutting either end alone throws
        # away the half a reader needs in order to act.
        #
        # Count what is actually GONE, which includes the note's own footprint:
        # the note occupies budget that would otherwise hold the caller's text, so
        # reporting ``len(text) - room`` understated the loss by the note's own
        # length. A note about a truncation has one job, and it is to be right
        # about the truncation.
        keep = max(room - len(_ELIDED_NOTE.format(n=len(text))), 0)
        head = keep // 2
        tail = keep - head
        text = text[:head] + _ELIDED_NOTE.format(n=len(text) - keep) + text[len(text) - tail :]
    return f"{text}\n{_REFUSAL_SENTINEL}"


def preserve_tail_marker(full: str, truncated: str) -> str:
    """Re-attach a tail-anchored marker that truncating *full* into *truncated* cut.

    Both sentinels are tail-anchored, and the transport truncates AFTER redacting
    -- and redaction can GROW the text, because a credential is replaced by a
    longer placeholder. So bounding the text before redaction is necessary but not
    sufficient: an 8,000-char rejection carrying an AKIA-shaped token expands past
    the cut and loses its tag, and the decline reads as a lost marker again.

    Mirrors the MCP App render marker's re-injection at the same seam, for the
    same reason: a control token that decides how a frame is interpreted must not
    be a casualty of a length cut applied to the frame's prose.
    """
    for sentinel in (_SENTINEL, _REFUSAL_SENTINEL):
        idx = full.rfind(sentinel)
        if idx < 0:
            continue
        tail = full[idx:]
        if tail in truncated:
            return truncated
        room = MAX_TOOL_RESULT_CHARS - len(tail) - 1
        if room <= 0:
            # A payload that cannot fit at all: leave the cut alone rather than
            # return a frame that is only a marker.
            return truncated
        return truncated[:room].rstrip("\n") + "\n" + tail
    return truncated


def refuse_if_markerless(tool_name: str, text: str) -> str:
    """Tag a directive tool's marker-less result as a refusal (see
    :data:`_REFUSAL_SENTINEL`). Any other tool's result is returned untouched.

    The producer-side half of the invariant "a directive tool's result either
    carries the marker, or it is a refusal". Called once, at the MCP server's
    outermost return, so it covers every way a directive tool can decline
    WITHOUT emitting a marker — including the ones its handler never sees,
    because argument validation runs in the dispatch wrapper AHEAD of the
    handler and returns a bare ``"Error: …"`` string.

    Deliberately keyed on the tool NAME alone and therefore inert elsewhere: the
    consumer honours a directive only from a call carrying this server's
    :data:`CORE_MCP_SERVER` identity, so tagging text cannot grant anything.
    Tagging is diagnostic; it changes how the consumer LOGS a result it was
    already going to drop, never whether an effect applies.
    """
    if not text or tool_name not in DIRECTIVE_TOOLS or has_marker(text):
        return text
    return tag_refusal(text)


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
    """True iff *text* is a tagged REFUSAL — a directive tool that deliberately
    returned no marker and said so in the text, whether because :func:`encode`
    would not fit the payload or because the call was declined before a directive
    could be built (see :func:`refuse_if_markerless`).

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
    line = text[idx + len(_SENTINEL) :].split("\n", 1)[0]
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
    line = text[idx + len(_SENTINEL) :].split("\n", 1)[0]
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


def content_free_digest(payload: str, _len: int = 12) -> str:
    """Short stable digest of *payload* that reveals none of its content.

    Directive diagnostics are logged on the failure path, where the payload is
    either malformed or came from model-visible text -- so the log line must not
    carry the bytes themselves. A digest keeps the one question those lines exist
    to answer: two logs naming the same digest saw the same payload, and two
    naming different digests did not. It is deliberately truncated: this is a
    correlation handle, not a signature, and a full hash only makes the line
    harder to read.

    Returns a marker instead of a digest for empty input, so a caller can print
    the result unconditionally without a special case.
    """
    if not payload:
        return "empty"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:_len]


def peek_failure_reason(text: str | None) -> str:
    """Name WHY :func:`peek` returned ``None`` for *text* -- diagnostics only.

    ``has_marker`` true with ``peek`` returning ``None`` is a real observed state
    and, until this existed, an undiagnosable one: the consumer could report that
    no record matched without being able to say whether the sentinel arrived
    without its payload, the payload was truncated mid-JSON, or the payload named
    a kind this build does not know. Mirrors :func:`peek`'s branches exactly, so a
    reason here is the branch peek actually took. Returns ``"ok"`` when peek
    succeeds, so a caller can log it unconditionally.

    The reason names the failure SHAPE only -- never the payload. The payload is
    model-visible text that reaches the dashboard log before anything has
    redacted it, and a malformed frame is exactly the case where the bytes are
    least trustworthy, so an excerpt here would publish unvalidated content to
    diagnose a parse error. Shape plus length is what actually distinguishes the
    failures; ``payload_sha`` correlates two log lines without revealing either.
    """
    if not text:
        return "empty-output"
    idx = text.find(_SENTINEL)
    if idx < 0:
        return "no-sentinel"
    line = text[idx + len(_SENTINEL) :].split("\n", 1)[0]
    if not line:
        return "sentinel-present-but-payload-empty (marker is the last thing in the frame)"
    try:
        block = json.loads(line)
    except (ValueError, TypeError) as exc:
        return "json-unparseable (%s); payload_len=%d payload_sha=%s" % (
            exc.__class__.__name__,
            len(line),
            content_free_digest(line),
        )
    if not isinstance(block, dict):
        return "json-not-an-object (%s)" % type(block).__name__
    kind = block.get("kind")
    if not isinstance(kind, str):
        return "kind-missing-or-not-a-string"
    if kind not in DIRECTIVE_TOOLS:
        # Shape, not the value: `kind` is read straight out of model-visible
        # marker text, so echoing it here would publish unvalidated content on
        # the same pre-redaction path as the excerpt above.
        return "unknown-kind (len=%d sha=%s)" % (len(kind), content_free_digest(kind))
    return "ok"


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
