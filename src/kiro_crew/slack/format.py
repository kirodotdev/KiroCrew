"""Slack message formatting — markdown to mrkdwn conversion."""

from __future__ import annotations

import logging
import re
from typing import Callable, NamedTuple

from kiro_crew.constants import OPTIONS_RE_LINE, strip_action_markers
from kiro_crew.messaging.display_safety import redact_for_display, strip_ansi
from kiro_crew.messaging.renderer import cap_choices, format_overflow
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)

SLACK_MAX_TEXT = 39_000

# [OPTIONS: choice1 | choice2 | ...] — the marker ends a LINE here, so use the
# MULTILINE/single-line canonical parser. Defined once in constants.py (shared
# with dashboard/state.py and the renderer surfaces) so the ReDoS-hardened
# grammar can never drift between copies; see OPTIONS_RE_LINE for the full
# rationale. Per-choice whitespace is stripped by extract_options().
_OPTIONS_RE = OPTIONS_RE_LINE

# Action ID prefix for OPTIONS buttons
OPTIONS_ACTION_PREFIX = "options_choice_"

# Action ID for OPTIONS checkboxes and submit
OPTIONS_CHECKBOXES_ACTION = "options_checkboxes"
OPTIONS_SUBMIT_ACTION = "options_submit"

# Action ID prefix for cron acknowledge buttons
CRON_ACK_ACTION_PREFIX = "cron_ack_"

# Action ID prefix for subagent acknowledge buttons
SUBAGENT_ACK_ACTION_PREFIX = "subagent_ack_"

# Action ID for link-to-dashboard button
LINK_DASHBOARD_ACTION = "mc_link_dashboard"


def extract_options(text: str) -> tuple[str, list[str]]:
    """Extract OPTIONS choices from LLM response and strip the tag.

    Returns (cleaned_text, choices). If no OPTIONS found, choices is empty.

    An ``[OPTION-ACTIONS:]`` marker is STRIPPED AND DROPPED here: removed from
    the cleaned text, contributing nothing to *choices*. That is the whole
    channel policy, not a shortcut. Those entries name a LOCAL DASHBOARD UI
    action (closing a tab), so a Slack button built from one could not perform
    it — it would post a click that no surface can honour, and the honest
    rendering of an action this channel cannot take is no button at all. It must
    still be stripped, because Slack posts what it is handed: the head is inert
    for the OPTIONS parser, and inert means passed through VERBATIM, so skipping
    the strip publishes the raw marker into the thread.
    """
    # Strip actions FIRST. Ordering is deliberate: the OPTIONS branch below
    # returns ``text[:m.start()]``, discarding everything after the content
    # marker, so an action marker that FOLLOWS one is dropped either way — but
    # one that PRECEDES it, or appears with no content marker at all, survives
    # that path untouched. Doing it here covers every arrangement.
    #
    # The ``rstrip`` is applied ONLY when a marker was actually removed, so text
    # carrying no action marker is returned byte-identical to before. An
    # unconditional ``rstrip`` would quietly start trimming trailing whitespace
    # off every Slack post that has no OPTIONS marker either — a behaviour change
    # nobody asked for, on the no-marker path that is the overwhelming majority.
    without_actions = strip_action_markers(text)
    if without_actions != text:
        text = without_actions.rstrip()
    m = _OPTIONS_RE.search(text)
    if not m:
        return text, []
    choices = [c.strip() for c in m.group(1).split("|") if c.strip()]
    cleaned = text[: m.start()].rstrip()
    return cleaned, choices


def _redact_choices(
    choices: list[str],
    redactor: Callable[[str], str] | None,
) -> list[str]:
    """Normalise and redact OPTIONS choices before they are put on Slack.

    Choice text is LLM-authored and reaches Slack through Block Kit, which no
    redactor covers: ``build_options_blocks`` embeds it in a ``plain_text``
    label and in the button ``value`` that is echoed back into the session on
    submit. So a turn ending ``[OPTIONS: Retry with AKIA… | Abort]`` would put
    the key on the wire and then read it back.

    Redaction happens HERE, at the sink, rather than at each caller, for the
    same reason the message pipeline was hoisted: a caller that extracts the
    tag from raw text (which it must, so conversion's 39,000-char truncation
    cannot eat the tag) would otherwise hand over unscanned choices, and every
    future caller would have to remember. It runs BEFORE the ``[:75]`` / ``[:150]``
    slices below, because slicing first can cut a credential into a prefix the
    regex no longer matches -- the same hazard as the conversion ceiling.
    """
    if redactor is None:
        redactor = redact_via_context
    # redact_for_display, not the bare redactor: a selected choice is echoed back
    # in a mrkdwn summary, so Slack strips its markup and a backtick- or
    # emphasis-split key would be whole on screen -- the same vector as the body.
    return [redact_for_display(choice or "", redactor)[0] for choice in choices]


def build_options_blocks(
    choices: list[str],
    *,
    redactor: Callable[[str], str] | None = None,
    staleness_token: str | None = None,
) -> list[dict]:
    """Build Slack Block Kit checkboxes + Send button for multi-select OPTIONS.

    This is the single chokepoint for every Slack producer of OPTIONS blocks
    (renderer footer, legacy handler, cron delivery, subagent footer,
    send_message, dashboard mirror), so the ``max_buttons`` cap lives HERE:
    the first ``SLACK_CAPABILITIES.max_buttons`` choices render as checkbox
    options (the platform caps a checkboxes element at 10) and any overflow
    degrades to a numbered context block the user can answer by typing.
    Overflow is redacted like the widget labels — same LLM-authored text,
    same wire.

    *staleness_token* rides on the actions block's ``block_id``, which Slack
    echoes back on every click and also uses to key ``state.values``. That is
    what lets a click be judged against the conversation it was asked in without
    the gateway remembering anything: see
    :func:`kiro_crew.slack.outbound.encode_options_token`. Omitting it posts a
    control that cannot be proven stale, so clicks on it are honoured.
    """
    # Circular import: slack/transport.py imports SLACK_MSG_LIMIT from this
    # module at top level, so the capabilities object must be imported lazily.
    from kiro_crew.slack.transport import SLACK_CAPABILITIES

    kept, overflow = cap_choices(choices, SLACK_CAPABILITIES)
    safe = _redact_choices(kept, redactor)
    options = [
        {
            "text": {"type": "plain_text", "text": choice[:75]},
            "value": choice[:150],
        }
        for choice in safe
    ]
    actions: dict = {
        "type": "actions",
        "elements": [
            {
                "type": "checkboxes",
                "action_id": OPTIONS_CHECKBOXES_ACTION,
                "options": options,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Send"},
                "action_id": OPTIONS_SUBMIT_ACTION,
                "style": "primary",
            },
        ],
    }
    if staleness_token:
        actions["block_id"] = staleness_token
    blocks: list[dict] = [actions]
    if overflow:
        # Chunk instead of slicing: a single [:2900] would re-create the
        # silent data loss this cap exists to remove, one layer down. Slack
        # caps a context mrkdwn element at ~3000 chars; pack WHOLE numbered
        # lines (never split a choice mid-line) into up to three blocks.
        # BOUNDED: Slack rejects messages over 50 blocks outright — an
        # unbounded loop would make a pathological trailer delete the whole
        # footer. When even three blocks overflow, the tail is dropped with
        # a VISIBLE count marker; never silent.
        lines = format_overflow(_redact_choices(overflow, redactor), start=len(safe)).split("\n")
        packed: list[str] = []
        shown = 0
        for line in lines:
            if packed and len(packed[-1]) + 1 + len(line) <= 2900:
                packed[-1] += "\n" + line
            elif len(packed) < 3:
                # A single absurd choice can exceed a whole block: truncate
                # WITH a visible marker (the widget path truncates labels at
                # [:75] similarly, but there the ellipsis is implied by the
                # platform; here we own the rendering).
                packed.append(line if len(line) <= 2900 else line[:2899] + "…")
            else:
                break
            shown += 1
        for piece in packed:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": piece}],
                }
            )
        omitted = len(overflow) - shown
        if omitted > 0:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_…{omitted} more choices omitted (list too long)_",
                        }
                    ],
                }
            )
    return blocks


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as mrkdwn entity markup.

    Choice text is LLM-authored, so it can carry (or be talked into carrying)
    Slack's own entity syntax. ``<!channel>``, ``<!here>`` and ``<@U…>`` are
    interpreted when they land in a ``mrkdwn`` field, so an OPTIONS tag reading
    ``[OPTIONS: <!channel> | Skip]`` would notify an entire channel the moment
    the summary is rendered -- on backfill, on submit, or on expiry.

    A message's top-level ``text`` argument is the SAME kind of sink: Slack parses
    entities there too, and it is what notifications and block-less clients show.
    So a caller handing choice text to ``post_message`` / ``post_blocks`` /
    ``update_message`` as the fallback text has to escape it as well -- escaping
    only the blocks leaves the notification path wide open. Public for that
    reason: more than one module needs it, and a second copy would drift.

    Slack's documented escaping, and ``&`` MUST go first: doing it after ``<``
    would re-escape the ampersands the earlier substitutions just introduced and
    show ``&amp;lt;`` on screen.

    Deliberately NOT folded into ``_redact_choices``, even though every mrkdwn
    caller goes through it. Its output also feeds the ``plain_text`` checkbox
    label -- which Slack does not interpret, so escaping there would display a
    literal ``&lt;`` -- and the button ``value`` that is echoed back into the
    session on submit, where an escaped entity would change the answer the user
    actually picked. Escaping belongs at the Slack-facing sink only, never on the
    copy the agent reads back.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_options_selected_blocks(
    choices: list[str],
    selected_indices: list[int] | int,
    *,
    redactor: Callable[[str], str] | None = None,
) -> list[dict]:
    """Render OPTIONS as static text with selected choices highlighted.

    Input arrives re-derived from the rendered message's blocks, so it is
    already ≤ the cap; the shared ``cap_choices`` keeps the two paths driven
    by one value so they cannot drift.
    """
    from kiro_crew.slack.transport import SLACK_CAPABILITIES  # circular (see above)

    if isinstance(selected_indices, int):
        selected_indices = [selected_indices]
    selected_set = set(selected_indices)
    kept, _ = cap_choices(choices, SLACK_CAPABILITIES)
    parts = []
    for i, choice in enumerate(_redact_choices(kept, redactor)):
        # Slice THEN escape -- the opposite order from redaction above, and for
        # the mirror-image reason. Redaction runs before slicing because a cut
        # can leave a credential prefix its regex no longer matches; escaping
        # runs after because a cut through an already-expanded ``&amp;`` would
        # leave a mangled half-entity on screen. Escaping a truncated ``<`` is
        # still complete, so nothing is lost by waiting.
        if i in selected_set:
            parts.append(f"*{escape_mrkdwn(choice[:72])}*")
        else:
            parts.append(f"~{escape_mrkdwn(choice[:73])}~")
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  |  ".join(parts)}],
        }
    ]


def replace_options_blocks(blocks: list[dict], selected_blocks: list[dict]) -> list[dict]:
    """Replace OPTIONS actions block(s) with *selected_blocks* in place.

    Walks *blocks* looking for any ``actions`` block whose elements include
    ``OPTIONS_CHECKBOXES_ACTION``, ``OPTIONS_SUBMIT_ACTION``, or an action_id
    starting with ``OPTIONS_ACTION_PREFIX``. The first such block is replaced
    by *selected_blocks* (inserted in order); subsequent OPTIONS actions blocks
    are dropped. All other blocks are preserved unchanged.

    Returns a new list; the input blocks are never mutated, and preserved
    blocks keep their identity so a caller can assert on them.
    """
    result: list[dict] = []
    inserted = False
    for block in blocks:
        if block.get("type") != "actions":
            result.append(block)
            continue
        elements = block.get("elements", [])
        is_options_block = any(
            el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
            or el.get("action_id", "").startswith(OPTIONS_ACTION_PREFIX)
            for el in elements
        )
        if not is_options_block:
            result.append(block)
            continue
        if not inserted:
            result.extend(selected_blocks)
            inserted = True
        # Drop the OPTIONS actions block itself
    if not inserted:
        # No OPTIONS actions block found — append selected_blocks at end so
        # the user still sees their selection (defensive fallback).
        logger.warning(
            "OPTIONS actions block not found in parent message blocks; "
            "appending selection at end"
        )
        result.extend(selected_blocks)
    return result


def build_cron_ack_block(job_id: str) -> list[dict]:
    """Build a Slack Block Kit acknowledge button for cron notifications."""
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Acknowledge"},
                    "action_id": f"{CRON_ACK_ACTION_PREFIX}{job_id}",
                    "value": job_id,
                    "style": "primary",
                }
            ],
        }
    ]


def build_subagent_ack_block(subagent_id: str) -> list[dict]:
    """Build a Slack Block Kit acknowledge button for subagent notifications."""
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Acknowledge"},
                    "action_id": f"{SUBAGENT_ACK_ACTION_PREFIX}{subagent_id}",
                    "value": subagent_id,
                    "style": "primary",
                }
            ],
        }
    ]


def build_link_dashboard_button() -> dict:
    """Single button element for linking a Slack thread to the dashboard."""
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": "Link to Dashboard"},
        "action_id": LINK_DASHBOARD_ACTION,
    }


def ends_inside_code_fence(text: str, start: bool = False) -> bool:
    """Whether *text* leaves the reader inside a ``` fenced block.

    Companion to ``to_slack_mrkdwn(..., in_code=...)``: a caller that converts
    text in blocks uses this to carry fence state from one block to the next.
    Counts the same toggles the converter does, so the two cannot disagree.
    """
    in_code = start
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
    return in_code


def to_slack_mrkdwn(text: str, *, keep_tables: bool = False, in_code: bool = False) -> str:
    """Convert LLM markdown to Slack mrkdwn format.

    **Do not call this to put text on Slack.** Use :func:`render_for_slack` (or
    :func:`render_one_for_slack`) instead — a build gate
    (``test_slack_render_pipeline.py``) fails if any module outside this one calls
    this function, and there are currently zero such callers.

    The reason is not style. This function strips ANSI escapes and self-truncates
    at ``SLACK_MAX_TEXT`` before converting, and each of those defeats credential
    redaction from a different side, so there is no ordering a call site can pick
    that is safe on its own: redact-then-convert lets the ANSI strip *reassemble*
    a credential the escapes had split, and convert-then-redact lets the
    truncation cut one into an unmatchable prefix. The render helpers exist to
    hold the only ordering that closes both.

    ``in_code`` says the text BEGINS inside a ``` fenced block. It exists for
    callers that convert in blocks: fence state is per-call, so a block starting
    mid-fence would otherwise be treated as prose and have its code lines rewritten
    by inline conversion. A fenced region can be longer than any block size, so
    choosing cut points cannot avoid this -- the state has to be carried. Use
    :func:`ends_inside_code_fence` to compute it for the next block.
    """
    text = strip_ansi(text)

    if len(text) > SLACK_MAX_TEXT:
        # rfind returns -1 (no newline in window) or the last newline's index —
        # NOT a bool. `rfind(...) or SLACK_MAX_TEXT` mishandles both: -1 is
        # truthy so text[:-1] keeps ~39000 chars (Slack then rejects the
        # message), and a newline at index 0 falls through to the cap. Use the
        # explicit -1 check already used by split_message().
        cut = text[:SLACK_MAX_TEXT].rfind("\n")
        if cut <= 0:
            cut = SLACK_MAX_TEXT
        text = f"{text[:cut]}\n\n_…truncated ({len(text)} chars total)_"

    # Table and mermaid conversion scan for their own markers, which a block
    # opening mid-fence would find INSIDE code. Skip both while in a fence.
    if not in_code:
        if not keep_tables:
            text = _convert_tables(text)
        text = _convert_mermaid(text)

    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(line)
        elif in_code:
            out.append(line)
        else:
            line = _convert_inline(line)
            out.append(line)
    return "\n".join(out)


# ── Inline conversions (outside code blocks) ──

# Markdown link [text](url) → Slack <url|text>. The lookbehind holds IMAGE
# syntax out of the rewrite: Slack mrkdwn has no image form, so converting
# ``![alt](dest)`` yields a stray ``!`` plus a clickable link to a destination
# Slack cannot open — a local screenshot path is not even reachable from its
# servers. Images pass through raw instead, as they do on Discord, until the
# outbound upload path claims them.
_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
# Headings: # text → *text*
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# Horizontal rule: --- or *** or ___ (3+ chars)
_HR_RE = re.compile(r"^[\s]*([-*_])\1{2,}\s*$")
# Strikethrough: ~~text~~ → ~text~
_STRIKE_RE = re.compile(r"~~(.+?)~~")


def _convert_inline(line: str) -> str:
    """Convert a single non-code line from markdown to Slack mrkdwn."""
    # Headings → bold
    m = _HEADING_RE.match(line)
    if m:
        return f"*{m.group(2).strip()}*"

    # Horizontal rule → unicode line
    if _HR_RE.match(line):
        return "─" * 30

    # **bold** → *bold*
    line = line.replace("**", "*")

    # ~~strike~~ → ~strike~
    line = _STRIKE_RE.sub(r"~\1~", line)

    # [text](url) → <url|text>
    line = _LINK_RE.sub(r"<\2|\1>", line)

    return line


# Markdown table: line starting with | and containing at least one more |
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
# Separator row: only |, -, :, spaces
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _convert_tables(text: str) -> str:
    """Convert markdown tables to vertical list format for mobile readability."""
    lines = text.split("\n")
    result: list[str] = []
    headers: list[str] = []
    data_rows: list[list[str]] = []

    def _flush_table() -> None:
        if not headers or not data_rows:
            return
        for row in data_rows:
            parts: list[str] = []
            for i, cell in enumerate(row):
                if not cell:
                    continue
                if i < len(headers):
                    parts.append(f"*{headers[i]}:* {cell}")
                else:
                    parts.append(cell)
            result.append("• " + " | ".join(parts))
        headers.clear()
        data_rows.clear()

    for line in lines:
        if _TABLE_ROW_RE.match(line):
            if _TABLE_SEP_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not headers:
                headers.extend(cells)
            else:
                data_rows.append(cells)
        else:
            _flush_table()
            result.append(line)

    _flush_table()
    return "\n".join(result)


# ── Mermaid → text ──

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)
# graph/flowchart edges: A[label] -->|text| B[label]  or  A --> B
_GRAPH_EDGE_RE = re.compile(
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
    r"\s*(-->|---|-\.->|==>)(?:\|([^|]*)\|)?\s*"
    r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|(?:\([^)]*\)))?"
)
# sequence: Actor->>Actor: message
_SEQ_RE = re.compile(r"(\S+?)\s*(->>|-->>|->|-->)\s*(\S+?):\s*(.+)")


def _convert_mermaid(text: str) -> str:
    """Replace ```mermaid blocks with readable text diagrams."""

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        body = m.group(1).strip()
        first = body.split("\n", 1)[0].strip().lower()

        if first.startswith(("graph ", "flowchart ")):
            return _mermaid_graph(body)
        if first.startswith("sequencediagram"):
            return _mermaid_sequence(body)
        # Unknown diagram type — show as plain code block
        return f"```\n{body}\n```"

    return _MERMAID_BLOCK_RE.sub(_replace, text)


def _mermaid_graph(body: str) -> str:
    """Convert graph/flowchart to text arrows."""
    labels: dict[str, str] = {}
    edges: list[str] = []
    for line in body.split("\n")[1:]:  # skip "graph TD" line
        m = _GRAPH_EDGE_RE.search(line.strip())
        if not m:
            continue
        src, sl1, sl2, _, edge_label, dst, dl1, dl2 = m.groups()
        if sl1 or sl2:
            labels[src] = sl1 or sl2
        if dl1 or dl2:
            labels[dst] = dl1 or dl2
        src_name = labels.get(src, src)
        dst_name = labels.get(dst, dst)
        arrow = f" ({edge_label.strip()}) " if edge_label else " "
        edges.append(f"  {src_name} →{arrow}{dst_name}")
    return "\n".join(edges) if edges else body


def _mermaid_sequence(body: str) -> str:
    """Convert sequenceDiagram to text arrows."""
    lines: list[str] = []
    for line in body.split("\n")[1:]:  # skip "sequenceDiagram"
        m = _SEQ_RE.match(line.strip())
        if not m:
            continue
        src, arrow_type, dst, msg = m.groups()
        # Mermaid arrows read left-to-right (src → dst), so the glyph must point
        # toward dst. ">>" is a solid arrowhead; "--" marks a dashed (reply) line.
        # Keep the four arrow types visually distinct and rightward so dashed
        # replies and dashed-open arrows don't render identically or point back
        # at the source.
        if "--" in arrow_type:
            arrow = "⇒" if ">>" in arrow_type else "⤳"  # dashed reply vs dashed open
        else:
            arrow = "→" if ">>" in arrow_type else "⇢"  # solid reply vs solid open
        lines.append(f"  {src} {arrow} {dst}: {msg.strip()}")
    return "\n".join(lines) if lines else body


# Slack message character limit (API rejects above ~4000)
SLACK_MSG_LIMIT = 3900
TRUNCATION_NOTICE = "\n\n⚠️ _Response truncated (Slack message limit)_"
CONTINUATION = "\n\n_(continued…)_"

# Inline thinking tags that some models embed in text
_THINKING_TAG_RE = re.compile(
    r"<(?:thinking|antml:thinking)>.*?</(?:thinking|antml:thinking)>",
    re.DOTALL,
)


def strip_thinking_tags(text: str, *, strip_whitespace: bool = True) -> tuple[str, str]:
    """Strip inline <thinking> tags from text.

    Returns (cleaned_text, extracted_thinking).
    """
    thinking_parts: list[str] = []
    for m in _THINKING_TAG_RE.finditer(text):
        # Extract content between tags
        block = m.group(0)
        inner = re.sub(r"^<[^>]+>|<[^>]+>$", "", block).strip()
        if inner:
            thinking_parts.append(inner)
    cleaned = _THINKING_TAG_RE.sub("", text)
    if strip_whitespace:
        cleaned = cleaned.strip()
    return cleaned, "\n\n".join(thinking_parts)


def split_message(text: str, limit: int = SLACK_MSG_LIMIT) -> list[str]:
    """Split text into chunks that fit within Slack's message limit.

    Splits on newline boundaries when possible to avoid breaking mid-line.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # Reserve space for continuation marker on non-final chunks. Guard against
        # a limit <= len(CONTINUATION): without the max(., 1) floor, chunk_limit
        # would be <= 0, cut would be 0, the remainder would never shrink, and the
        # loop would spin forever. Keep at least one character of progress per
        # iteration so the function always terminates regardless of `limit`.
        chunk_limit = max(1, limit - len(CONTINUATION))
        # Try to split at last newline within limit
        cut = text.rfind("\n", 0, chunk_limit)
        if cut <= 0:
            cut = chunk_limit
        remainder = text[cut:].lstrip("\n")
        if remainder:
            parts.append(text[:cut] + CONTINUATION)
        else:
            parts.append(text[:cut])
        text = remainder
    return parts


def _render_blocks(
    text: str,
    *,
    presplit: Callable[[str, int], list[str]],
    keep_tables: bool | None,
    redactor: Callable[[str], str],
) -> tuple[list[str], bool]:
    """THE ordering core, shared by both public render forms.

    ``strip_ansi -> redact -> pre-split -> convert -> redact``, once. The two
    public forms differ only in how they ASSEMBLE the result (separate posts vs
    one joined message) and in which pre-splitter they need; the security-critical
    part -- the order these steps run in -- lives here so an ordering fix cannot
    land in one form and silently miss the other.

    Args:
        text: Raw text.
        presplit: How to cut the normalised text into conversion-sized blocks.
            The multi-part form uses ``split_message`` (its blocks become separate
            posts, so its continuation markers are wanted); the collapsed form
            uses :func:`_lossless_blocks` (its blocks are rejoined, so the split
            must be invisible).
        keep_tables: ``True`` forces raw tables; ``None``/``False`` derives it from
            whether a split occurred. The flag can only ADD rawness -- see
            :func:`render_one_for_slack`.
        redactor: Text-to-text redactor.

    Returns:
        ``(converted_blocks, redaction_fired)``. ``converted_blocks`` is empty when
        there was nothing to say. ``redaction_fired`` is True when either pass
        changed the text -- the signal a caller that already published the text
        needs in order to go back and replace it.
    """
    changed = False

    def _redact(value: str) -> str:
        """Redact, remembering whether it actually changed anything.

        Only the redactor's effect counts. The ANSI strip also changes text, but
        stripping escapes is normalisation, not a disclosure that was caught.
        """
        nonlocal changed
        out = redactor(value)
        if out != value:
            changed = True
        return out

    # First pass goes through redact_for_display, which also cross-checks the
    # delimiter-canonical form -- Slack renders emphasis away, so a key split by
    # ``**`` is whole to the reader while both literal scans miss it.
    cleaned, first_changed = redact_for_display(text, redactor)
    changed = changed or first_changed
    if not cleaned.strip():
        return [], changed

    blocks = presplit(cleaned, SLACK_MAX_TEXT // 2)
    # A single block IS the whole message, so no table can be straddled.
    resolved_keep_tables = bool(keep_tables) or len(blocks) > 1
    out: list[str] = []
    # Fence state is per-conversion-call, so it has to be carried across blocks: a
    # block that opens mid-``` would otherwise have its code lines rewritten as
    # prose. A fenced region can exceed the block size, so no cut point avoids it.
    in_code = False
    for block in blocks:
        out.append(
            _redact(to_slack_mrkdwn(block, keep_tables=resolved_keep_tables, in_code=in_code))
        )
        in_code = ends_inside_code_fence(block, in_code)
    return out, changed


def render_for_slack(
    text: str,
    *,
    limit: int = SLACK_MSG_LIMIT,
    prefix: str = "",
    header: str = "",
    redactor: Callable[[str], str] | None = None,
) -> list[str]:
    """Render arbitrary text into postable Slack messages, redacting safely.

    This is the ONLY supported way to put text on Slack. It exists because the
    two obvious orderings of redact-vs-convert are each unsafe on their own, so
    a call site that picks one is exposed to whichever hazard it did not pick:

    - **redact then convert** — ``to_slack_mrkdwn`` calls :func:`strip_ansi`,
      and that strip *reassembles* a credential the escapes had broken up. A key
      written as ``AKIA<esc>IOSF…`` does not match the credential regex on the
      way in and arrives at Slack whole.
    - **convert then redact** — ``to_slack_mrkdwn`` self-truncates at
      ``SLACK_MAX_TEXT`` before converting, so it can cut a credential in half
      before the regex ever runs, leaving an unmatchable prefix on the wire (and
      silently dropping everything past 39,000 characters).

    The pipeline that holds is therefore::

        strip_ansi -> redact -> pre-split -> convert -> redact -> split

    Normalising first means the first redaction sees the credential whole while
    the text is still one piece. Pre-splitting below ``SLACK_MAX_TEXT`` means
    conversion never reaches its own truncation, so neither the tail nor a
    secret is cut there; blocks are halved against the limit so a conversion
    that *grows* text (table and mermaid rewriting) still cannot reach it.
    Redaction runs a second time on each converted block because conversion can
    still reorder or drop characters (inline markup, link rewriting) in ways
    that reveal a secret only afterwards — redacting on both sides of the
    transform is what makes the guarantee independent of what conversion does to
    the bytes.

    When — and only when — the pre-split produces more than one block, tables are
    left as raw markdown. ``_convert_tables`` keys a table's labels off the first
    ``|`` row it sees, so a block beginning part-way through a table adopts a
    DATA row as its header: that row's values are then only emitted as labels
    (and vanish entirely if no rows follow, because ``_flush_table`` returns
    early on an empty body). Raw pipes read worse on mobile, which is why this is
    not the default — but a message that never splits keeps the nicer rendering,
    and one that does keeps all of its rows.

    Args:
        text: Raw text to render. ``None``-ish and blank input yields ``[]``.
        limit: Per-message character ceiling, ``prefix`` included. Callers with a
            tighter budget than Slack's (cron uses 3,000) pass their own.
        prefix: Prepended to every returned part and charged against ``limit``,
            so a decorated part cannot overflow. Splitting first and decorating
            afterwards is what made the backfill's icon overflow the limit.
        header: A caption for the FIRST part only -- ``"⏰ *Cron: name*"`` and
            friends. Redacted but NOT converted, because these captions are
            already Slack mrkdwn and ``to_slack_mrkdwn`` would re-interpret their
            ``*bold*``. Skipping conversion is exactly why a caption needs its own
            redaction pass, and why doing it HERE rather than at each call site
            matters: captions are usually built from LLM-authored data (a cron's
            name comes from the ``cron_add`` tool), so a hand-rolled
            ``header + parts[0]`` has to remember to redact every single time --
            the same per-site-memory failure this function removes for bodies. One
            site had already forgotten.
        redactor: Text-to-text redactor. Defaults to the platform-context shim
            :func:`kiro_crew.platform.context.redact_via_context`, which is
            fail-closed and honours a host's own credential policy. Injectable so
            tests can assert ordering without composing a platform context.

    Returns:
        Ready-to-post strings, each already prefixed and within ``limit``.
        Empty when there is nothing to say, so callers can post unconditionally --
        EXCEPT when a ``header`` was given, which always yields at least the
        header, so a caller that attaches an action block to the first part still
        has a message to attach it to.
    """
    if redactor is None:
        redactor = redact_via_context

    safe_header = redact_for_display(header, redactor)[0] if header else ""
    # split_message here (not _lossless_blocks): these blocks become SEPARATE
    # posts, so its continuation markers are wanted rather than an artefact.
    converted_blocks, _ = _render_blocks(
        text,
        presplit=lambda t, n: split_message(t, limit=n),
        keep_tables=None,
        redactor=redactor,
    )
    if not converted_blocks:
        return [safe_header] if safe_header else []

    # Charge prefix and header against the limit rather than adding them
    # afterwards. The header only lands on the first part, but the whole body is
    # split against the reduced limit: giving the first chunk its own limit would
    # mean splitting twice, and over-reserving a caption's worth of characters on
    # later parts is cheaper than being wrong about the ceiling.
    body_limit = max(1, limit - len(prefix) - len(safe_header))
    parts: list[str] = []
    for converted in converted_blocks:
        parts.extend(f"{prefix}{part}" for part in split_message(converted, limit=body_limit))
    if safe_header:
        parts[0] = safe_header + parts[0]
    return parts


def _lossless_blocks(text: str, limit: int) -> list[str]:
    """Split *text* into blocks whose concatenation is EXACTLY *text*.

    For the INTERNAL pre-split of a message that will be joined back together.
    :func:`split_message` cannot be used for that: it is built for splitting into
    separate posts, so it appends a ``CONTINUATION`` marker and ``lstrip``s
    newlines at the boundary. That makes it lossy in a way no single rejoin can
    undo -- joining with ``""`` drops a newline it consumed, and joining with
    ``"\\n"`` INVENTS one inside a long unbroken line. A 19,501-character
    single-line response would gain a line break that was never in the text.

    Here the newline stays in the left block, so ``"".join(result) == text``
    holds character for character. A line boundary is still preferred inside the
    window, so conversion only ever sees a mid-line cut when the text genuinely
    has no newline to cut at.
    """
    if len(text) <= limit:
        return [text] if text else []
    blocks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        # +1 keeps the newline on the left block; a window with no newline
        # (rfind -> -1) or one at index 0 falls back to a hard cut at the limit.
        cut = cut + 1 if cut > 0 else limit
        blocks.append(text[:cut])
        text = text[cut:]
    if text:
        blocks.append(text)
    return blocks


class SlackRender(NamedTuple):
    """One rendered Slack message, plus whether redaction changed anything.

    The flag exists because a caller can need to ACT on the fact that redaction
    fired, not merely receive the redacted text. Slack's streaming path is that
    case: it has already posted the answer incrementally, and it overwrites that
    visible message ONLY when it learns the final text had to be redacted. If the
    render absorbed the redaction silently, the caller would see clean text,
    conclude nothing had happened, and leave the unredacted stream on screen.
    """

    text: str
    redacted: bool


def render_one_for_slack(
    text: str,
    *,
    limit: int = SLACK_MAX_TEXT,
    keep_tables: bool | None = None,
    redactor: Callable[[str], str] | None = None,
) -> SlackRender:
    """Same pipeline as :func:`render_for_slack`, collapsed to ONE message.

    For sinks that take a single string and manage their own presentation --
    Slack's streaming ``stop_stream`` / ``chat.update``, and the review-mode
    draft block. Those cannot accept a parts list without reshaping the live
    turn path, but they still need the ordering guarantee, so they share the
    pipeline through this form instead of calling ``to_slack_mrkdwn`` directly.

    The difference from :func:`render_for_slack` is only what happens at the
    ceiling. Conversion is still kept away from its own ``SLACK_MAX_TEXT``
    self-truncation by pre-splitting, so a credential can neither be reassembled
    by the ANSI strip nor cut in half before the regex runs. What cannot be
    preserved is the tail: a single message has nowhere to put it. So the
    overflow is cut HERE, after both redaction passes, and announced -- rather
    than being silently dropped inside a conversion the caller cannot see.

    Args:
        text: Raw text to render.
        limit: Character ceiling for the returned message.
        keep_tables: Force markdown tables to stay raw. This flag can only ever
            ADD rawness -- it cannot switch the straddle guard off. When the text
            had to be pre-split, tables stay raw regardless of what the caller
            passed, because a block beginning part-way through a table adopts a
            DATA row as its header and that row's values are then lost silently.
            Forcing raw costs rendering quality; forcing conversion would cost
            data, so only the first direction is a caller's to choose.
        redactor: See :func:`render_for_slack`.

    Returns:
        A :class:`SlackRender`. ``text`` is ``""`` when there is nothing to say;
        ``redacted`` is True when either redaction pass changed the text, which is
        the signal a caller that already published the text needs in order to go
        back and replace it.
    """
    if redactor is None:
        redactor = redact_via_context

    # _lossless_blocks here (not split_message): these blocks are REJOINED into
    # one message, so the pre-split must leave no trace -- no continuation marker
    # and no invented or swallowed newline at a boundary.
    converted_blocks, changed = _render_blocks(
        text,
        presplit=_lossless_blocks,
        keep_tables=keep_tables,
        redactor=redactor,
    )
    if not converted_blocks:
        return SlackRender("", changed)

    # Joined with "" because _lossless_blocks kept every boundary character in the
    # block it came from, so the pre-split is invisible in the result.
    rendered = "".join(converted_blocks)

    if len(rendered) > limit:
        cut = rendered[:limit].rfind("\n")
        if cut <= 0:
            cut = limit
        notice = f"\n\n_…truncated ({len(rendered)} chars total)_"
        # Charge the notice against the ceiling so the result really fits.
        cut = max(1, min(cut, limit - len(notice)))
        rendered = rendered[:cut] + notice
    return SlackRender(rendered, changed)
