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
# ASCII-ONLY, deliberately. A leading U+2063 INVISIBLE SEPARATOR would render the
# marker invisibly and make every directive silently fail:
# ``validation.build_tool_response`` — the single exit point for
# all tool responses — strips category ``Cf``, so such a prefix is destroyed
# before the response leaves the MCP server and ``decode`` cannot match.
# A machine-facing framing token must not depend on characters that sanitisers,
# Unicode normalisers and transports all legitimately rewrite.
_SENTINEL = "[[KIROCREW_SESSION_DIRECTIVE]]"
# Public alias. The transport layer (acp/_dispatch) locates the marker in a raw
# frame to keep it under the result cut, and reaching for the private name from
# another module would make that dependency invisible here.
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
# :func:`tag_refusal` bounds its text against it. An unbounded refusal is
# otherwise reachable -- ``validate_tool_args`` echoes the argument NAME, which
# the model chooses, so a 9,000-character name yields a 9,087-character result
# whose tail tag the cut removes, and the decline reads as a lost marker.
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
# argument. Without that second producer only the oversized case is
# distinguishable and every other refusal reads as a lost marker.
#
# Forgery-inert by construction: unlike the directive marker this token carries
# no payload and grants no effect, so a model emitting the literal bytes can only
# change how a log line reads, never what gets applied.
_REFUSAL_SENTINEL = "[[KIROCREW_SESSION_DIRECTIVE_REFUSED]]"
# What :func:`neutralize_markers` substitutes for sentinel bytes that arrived from
# outside this process. Deliberately NOT parseable as either sentinel and not a
# prefix of one, so no consumer can be talked back into reading it as a marker.
# Also what :func:`strip_marker` substitutes for a marker it cannot cut to the
# end of the text without truncating an envelope around it.
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


def call_input_digest(tool: str, raw_args: Any) -> str:
    """Digest of a tool CALL's raw arguments -- the out-of-band SELECTOR.

    A directive tool's validated payload is parked on the gateway
    (``dashboard.directive_queue``) and the turn's consumer claims it. The
    consumer cannot learn WHICH record to claim from the tool RESULT text, because
    that text is whatever the backend chose to put on
    the wire: KAS re-serialises the envelope (quotes escaped), copies the result
    into two fields, replaces one of them with an offload reference above a size
    threshold, and caps every string at 30k chars with the tail-anchored marker
    falling off the end. Each shape is one more repair branch in the shared ACP
    parser, and each backend can add another at any time.

    The tool call's INPUT reaches both sides through no envelope at all. The MCP
    server receives it as the ``arguments`` of ``tools/call``; the consumer sees
    it as the ``rawInput`` of the ACP ``tool_call`` frame, which kiro-agent emits
    uncapped. So the tool digests what it was called with and parks that beside
    the payload, the consumer digests what it saw the model call with, and the
    two agree without either reading the result body.

    Same trust shape as the marker it replaces: model-controlled content, bound
    to the session because it arrives on that session's own event stream in the
    very call the tool served. A caller who can park a record for another
    session still needs THAT session's model to make a call with identical
    arguments in the same turn -- the bar the marker set. The applied payload is
    always the record's; the digest only picks which record.

    ``tool`` is the directive tool's own bare name (a :data:`DIRECTIVE_TOOLS`
    member). The MCP server knows it as the ``tools/call`` name; the consumer
    resolves it with :func:`directive_tool_from_call` from the frame's trusted
    ``_meta.kiro`` identity or, on a backend without one, from the wire title
    kiro-agent's MCP wrapper stamps as ``@<server>/<tool>``.

    ``_meta`` is dropped before hashing: kiro-agent's MCP wrapper strips it before
    ``callTool`` (it is a per-call transport block, not an argument), so the tool
    never sees it while the consumer's ``rawInput`` does. Keys are sorted and
    ``default=str`` mirrors :func:`encode`, so a value only one side could
    serialise still compares equal. Non-dict input digests as its ``str``: the
    schema-less deferred path can hand a string where a dict was meant, and both
    sides see the same string.
    """
    if isinstance(raw_args, dict):
        body = {k: v for k, v in raw_args.items() if k != "_meta"}
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    else:
        canon = str(raw_args)
    # The TOOL is part of the key, not just its arguments. Every no-argument tool
    # hashes ``{}`` identically, so an args-only key let a planted
    # ``reset_conversation({})`` record be claimed by the victim session's own
    # ``resource_status({})`` frame -- any same-args call of any tool. Binding the
    # tool name means a record is claimable only by a call to the tool that parked
    # it, which is the same correlation the marker's ``kind`` carries.
    return hashlib.sha256(f"{tool}\x00{canon}".encode("utf-8", "replace")).hexdigest()


#: Backend-authored display prefix on a tool-call title (kiro-cli, KAS).
_RUNNING_PREFIX = "Running: "


def directive_tool_from_call(mcp_server_name: str, tool_name: str, title: str) -> str:
    """The directive tool a tool CALL frame was for, or ``""`` -- the consumer's
    half of :func:`call_input_digest`'s ``tool`` argument.

    Trusted identity first: :func:`directive_tool_for` over the ``_meta.kiro``
    pair (kiro-cli). A backend that emits none still names the tool on the
    wire: kiro-agent's MCP wrapper (KAS) sets the ``tool_call`` title to
    ``@<serverName>/<toolName>`` from its own tool config, and claude-agent-acp
    (Claude) passes Claude's raw tool name ``mcp__<server>__<tool>`` through as
    the title. opencode names an MCP tool ``<server>_<tool>`` with a SINGLE
    underscore and emits no ``_meta.kiro`` at all, so for that harness the title
    is the only identity a call carries (measured on 1.18.30 --
    ``agent-host-contract.md`` section 9). Any of the three spellings with
    ``kirocrew-core`` as the server resolves.
    Anything else is ``""``, and a call with no resolvable tool records no digest.

    *title* MUST be the frame's ``wire_title`` -- the backend's own field -- and
    never the display ``title``: ``acp/_dispatch.select_tool_title`` fills the
    display label from a shell call's ``rawInput.description``, which the model
    writes, so a shell call described as ``@kirocrew-core/reset_conversation``
    would otherwise resolve here and record a digest it has no business holding.

    This is a SELECTOR input, never a grant: a model that forges the title has
    only chosen which record to look up, and the record was still parked by a real
    tool call under this session's kernel-checked key with the tool's own name.
    Forging it buys exactly what forging the marker's ``kind`` buys.
    """
    resolved = directive_tool_for(mcp_server_name or "", tool_name or "")
    if resolved:
        return resolved
    if not isinstance(title, str) or not title:
        return ""
    # kiro-cli and KAS both prefix a tool-call title with the backend's own
    # ``Running: `` (agent-host-contract.md §7, "Tool-call titles"): a recorded
    # KAS frame carried ``Running: @kirocrew-core/ask_question``. The prefix is
    # the backend's, not the model's -- rawInput.description never reaches the
    # WIRE title -- so stripping it here widens nothing.
    if title.startswith(_RUNNING_PREFIX):
        title = title[len(_RUNNING_PREFIX) :]
    # KAS: kiro-agent's wrapper stamps ``@<serverName>/<toolName>``.
    prefix = f"@{CORE_MCP_SERVER}/"
    if title.startswith(prefix):
        candidate = title[len(prefix) :].strip()
        return candidate if candidate in DIRECTIVE_TOOLS else ""
    # Claude (claude-agent-acp): ``toolInfoFromToolUse`` has no MCP case, so the
    # title is the raw Claude tool name, ``mcp__<server>__<tool>``. Same server
    # check, spelled the way that adapter spells it; the server half is what a
    # third-party server exposing a same-named tool fails.
    if title.startswith(f"mcp__{CORE_MCP_SERVER}__"):
        candidate = title[len(f"mcp__{CORE_MCP_SERVER}__") :].strip()
        return candidate if candidate in DIRECTIVE_TOOLS else ""
    # opencode: ``<server>_<tool>``, joined by ONE underscore, and no
    # ``_meta.kiro`` anywhere -- so this is the only channel that names the tool.
    return _server_underscore_qualified(title)


def _server_underscore_qualified(name: str) -> str:
    """The directive tool *name* spells as ``<CORE_MCP_SERVER>_<tool>``, else ``""``.

    opencode's MCP tool id is its server name and tool name joined by a SINGLE
    underscore (each half with ``[^a-zA-Z0-9_-]`` replaced by ``_``; Crew's server
    names survive that unchanged). Deliberately an EXACT server-qualified match
    rather than teaching :func:`match_tool` that one underscore separates a
    qualifier: one underscore as a separator resolves ``do_monitor_start``, which
    that function excludes on purpose, and there is no way to tell that spelling
    apart from a bare tool name whose own words happen to end in a directive
    name. Exactness costs nothing here, because the whole point of this spelling
    is that both halves are on the wire.

    The SERVER half is the guard, as in the KAS and Claude branches: a
    third-party server exposing a tool literally named
    ``kirocrew-core_monitor_start`` spells its own id
    ``<that-server>_kirocrew-core_monitor_start`` and fails the prefix, and a
    longer Crew-looking name (``kirocrew-core_monitor_start_extra``) fails the
    :data:`DIRECTIVE_TOOLS` membership check on the tool half.
    """
    prefix = f"{CORE_MCP_SERVER}_"
    if not name.startswith(prefix):
        return ""
    candidate = name[len(prefix) :].strip()
    return candidate if candidate in DIRECTIVE_TOOLS else ""


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
    """Remove the directive or refusal marker from *text* for transcript display.

    Two shapes. When the marker is where the tool put it -- the LAST line of the
    result, after the human confirmation -- cut from the sentinel to the end, and
    drop the blank separator before it. When a backend has embedded the result
    inside a serialised envelope (KAS: ``{"response": "<text>\\n[[SENTINEL]]{...}",
    "message": ...}``) the sentinel sits mid-string, and a cut-to-end there
    truncates the JSON into unreadable half-output. So a marker that is not at the
    start of a line is REPLACED in place -- the sentinel and its brace-balanced
    payload -- leaving the envelope's other fields intact. Every occurrence, since
    the envelope may carry the text twice.

    Display only: nothing here is ever parsed back, so the replacement text can be
    anything readable. It is deliberately not the sentinel.
    """
    if not text:
        return text
    if _SENTINEL not in text and _REFUSAL_SENTINEL not in text:
        return text
    # Tail-anchored: the first sentinel begins a line (or the text). Cut to end.
    first = min(i for i in (text.find(_SENTINEL), text.find(_REFUSAL_SENTINEL)) if i >= 0)
    if first == 0 or text[first - 1] == "\n":
        return text[:first].rstrip("\n")
    # Embedded: replace each marker + its payload in place; then the refusal tag.
    out = _replace_embedded_payloads(text)
    return out.replace(_SENTINEL, _DEFANGED).replace(_REFUSAL_SENTINEL, _DEFANGED)


def _payload_end(text: str, start: int) -> int:
    """Index just past the JSON object beginning at ``text[start] == "{"``.

    Brace-counted rather than regex-matched, because a directive payload nests
    arbitrarily (``monitor_update`` is ``{"kind":..,"args":{"patch":{..}}}``) and
    a depth-limited pattern silently leaves the deeper ones in the transcript.

    Two spellings of the same object, told apart by the first character after
    the brace. Plain: the payload as :func:`encode` wrote it. Enveloped: the
    payload as a backend re-serialised it inside a JSON string, so every quote
    is ``\\"`` and every backslash is ``\\\\``; there a BARE ``"`` is the
    envelope's own closing quote, past which the payload cannot extend.

    The scan works on PAYLOAD-level characters: :func:`_unit` decodes one
    envelope unit (``\\"`` -> ``"``, ``\\\\`` -> ``\\``, plain otherwise) and
    the state machine then applies the payload's own string rules -- ``"``
    toggles a string, a ``\\`` inside one escapes the next payload character.
    Doing it in that order is what keeps a message containing ``" }}`` inside
    the string: on the wire it is ``\\\\\\" }}``, and reading ``\\"`` there as a
    delimiter (instead of as the escaped quote the preceding ``\\\\`` makes it)
    would close the string early and let ``}}`` end the object with the rest of
    the message left on display. Braces count only outside strings in either
    mode. Returns ``len(text)`` for an unterminated object, which is the right
    cut for a payload the transport truncated.
    """
    n = len(text)
    enveloped = text[start + 1 : start + 2] == "\\"
    depth = 0
    in_str = False
    i = start
    while i < n:
        c, i = _unit(text, i, enveloped)
        if c is None:
            return i  # the envelope's own string ends here
        if in_str:
            if c == "\\":
                if i >= n:
                    return n
                c, i = _unit(text, i, enveloped)  # the escaped character; skip it
                if c is None:
                    return i
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return n


def _unit(text: str, i: int, enveloped: bool) -> tuple[str | None, int]:
    """One payload-level character starting at ``text[i]`` and the index after it.

    Plain spelling: the character itself. Enveloped: ``\\"`` and ``\\\\`` decode
    to the quote and backslash they stand for; any other envelope escape
    (``\\n``, ``\\t``, ``\\uXXXX``...) stands for a character that is never a
    quote, brace or backslash, so it is returned as an opaque placeholder and
    its hex digits, if any, are scanned as the plain letters they are. A bare
    ``"`` is the envelope's closing quote: ``None``, index unchanged.
    """
    c = text[i]
    if not enveloped:
        return c, i + 1
    if c == '"':
        return None, i
    if c == "\\":
        nxt = text[i + 1 : i + 2]
        if nxt == '"':
            return '"', i + 2
        if nxt == "\\":
            return "\\", i + 2
        return "\x00", i + 2
    return c, i + 1


def _replace_embedded_payloads(text: str) -> str:
    """Replace every ``SENTINEL{...}`` in *text* with :data:`_DEFANGED`."""
    out: list[str] = []
    pos = 0
    while True:
        idx = text.find(_SENTINEL, pos)
        if idx < 0:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:idx])
        out.append(_DEFANGED)
        after = idx + len(_SENTINEL)
        pos = _payload_end(text, after) if text[after : after + 1] == "{" else after
