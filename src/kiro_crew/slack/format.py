"""Slack message formatting — markdown to mrkdwn conversion."""

from __future__ import annotations

import re

from kiro_crew.constants import OPTIONS_RE_LINE

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
    """
    m = _OPTIONS_RE.search(text)
    if not m:
        return text, []
    choices = [c.strip() for c in m.group(1).split("|") if c.strip()]
    cleaned = text[: m.start()].rstrip()
    return cleaned, choices


def build_options_blocks(choices: list[str]) -> list[dict]:
    """Build Slack Block Kit checkboxes + Send button for multi-select OPTIONS."""
    options = [
        {
            "text": {"type": "plain_text", "text": choice[:75]},
            "value": choice[:150],
        }
        for choice in choices[:10]  # checkboxes support up to 10
    ]
    return [
        {
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
        },
    ]


def build_options_selected_blocks(choices: list[str], selected_indices: list[int] | int) -> list[dict]:
    """Render OPTIONS as static text with selected choices highlighted."""
    if isinstance(selected_indices, int):
        selected_indices = [selected_indices]
    selected_set = set(selected_indices)
    parts = []
    for i, choice in enumerate(choices[:10]):
        if i in selected_set:
            parts.append(f"*{choice[:72]}*")
        else:
            parts.append(f"~{choice[:73]}~")
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  |  ".join(parts)}],
        }
    ]


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


def to_slack_mrkdwn(text: str, *, keep_tables: bool = False) -> str:
    """Convert LLM markdown to Slack mrkdwn format."""
    text = _strip_ansi(text)

    if len(text) > SLACK_MAX_TEXT:
        cut = text[:SLACK_MAX_TEXT].rfind("\n") or SLACK_MAX_TEXT
        text = f"{text[:cut]}\n\n_…truncated ({len(text)} chars total)_"

    if not keep_tables:
        text = _convert_tables(text)
    text = _convert_mermaid(text)

    out: list[str] = []
    in_code = False
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

# Markdown link [text](url) → Slack <url|text>
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
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


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


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
        # Previously the dashed branch was "⇠ if … else ⇠": both branches identical,
        # so dashed replies and dashed-open arrows rendered the same AND pointed back
        # at the source. Keep the four arrow types visually distinct and rightward.
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
