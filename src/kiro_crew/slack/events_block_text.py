"""Readable text recovered from a Slack message's Block Kit blocks and attachments.

``_extract_blocks_text`` renders rich-text, section, context and table blocks to plain
lines within one character budget (``_MAX_RECOVERED_TEXT_CHARS``). On top of it,
``_extract_shared_text`` recovers a forwarded message's text from its attachments,
``_extract_table_text`` renders pasted-spreadsheet ``table`` blocks, and
``_extract_trusted_bot_attachment_text`` recovers an admitted trusted bot's attachment
text. ``_route_message`` reads an inbound message's text through these.

Split out of ``kiro_crew.slack.events``, which stays the import path and the patch
target: every name here is reached as ``kiro_crew.slack.events.<name>``, and a patch
through that facade reaches this module (``kiro_crew._facade_mirror``). Nothing here
imports the facade. New block-text recovery work goes here.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Iterator, Literal, overload

logger = logging.getLogger("kiro_crew.slack.events")


# Maximum characters to recover from block extraction (DoS guard).
# Slack message bodies can be ~40k; 16k is a safe recovery cap for downstream processing.
_MAX_RECOVERED_TEXT_CHARS = 16000


def _string_field(element: dict, field: str) -> str:
    """Return one string field, ignoring malformed non-string scalars."""
    value = element.get(field)
    return value if isinstance(value, str) else ""


def _render_rich_text_element(el: dict) -> str:
    """Render a single rich_text inline element to plain text.

    Handles all documented Slack rich_text element types:
    text, link, emoji, user, usergroup, channel, broadcast, date.
    """
    if not isinstance(el, dict):
        return ""
    el_type = el.get("type")
    if el_type == "text":
        return _string_field(el, "text")
    if el_type == "link":
        # link: show "text (url)" if both present; else whichever exists
        text = _string_field(el, "text")
        url = _string_field(el, "url")
        if text and url:
            return f"{text} ({url})"
        return text or url
    if el_type == "emoji":
        # emoji: use :name: format; fall back to unicode if name missing
        name = _string_field(el, "name")
        if name:
            return f":{name}:"
        return _string_field(el, "unicode")
    if el_type == "user":
        user_id = _string_field(el, "user_id")
        return f"<@{user_id}>" if user_id else ""
    if el_type == "usergroup":
        usergroup_id = _string_field(el, "usergroup_id")
        return f"<!subteam^{usergroup_id}>" if usergroup_id else ""
    if el_type == "channel":
        channel_id = _string_field(el, "channel_id")
        return f"<#{channel_id}>" if channel_id else ""
    if el_type == "broadcast":
        # broadcast range: here, channel, or everyone
        range_val = _string_field(el, "range")
        return f"<!{range_val}>" if range_val else ""
    if el_type == "date":
        # date: use fallback text if present (human-readable rendering)
        return _string_field(el, "fallback")
    # Unknown element type — attempt text field, log for observability
    logger.debug("Unknown rich_text element type=%r, attempting text field", el_type)
    return _string_field(el, "text")


class _BoundedBlockText:
    """Rendered Block Kit lines retained within one character budget.

    The output shape is the one ``_extract_blocks_text`` always produced -- lines
    joined with ``"\\n"``, surrounding whitespace stripped -- but the cap is a
    budget rather than a slice of a fully joined result: no more than *limit*
    characters are ever retained, and ``full`` tells the traversal to stop
    reading an oversized payload as soon as the budget is spent.
    """

    def __init__(self, limit: int) -> None:
        self._remaining = max(0, limit)
        self._consumed = 0
        self._parts: list[str] = []

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def full(self) -> bool:
        return self._remaining <= 0

    def add_line(self, fragments: Iterable[str], prefix: str = "") -> None:
        """Retain one line built from *fragments*, reading none past the budget.

        The line -- its ``"\\n"`` separator and *prefix* included -- is opened by
        the first non-empty fragment, so fragments that all render empty add no
        line, the rule the former ``if inline:`` check applied to the joined
        inline text.  *fragments* is consumed lazily: once the budget is spent
        the next fragment is never requested, so its element is never rendered.
        """
        iterator = iter(fragments)
        opened = False
        while not self.full:
            fragment = next(iterator, None)
            if fragment is None:
                return
            if not fragment:
                continue
            if not opened:
                opened = True
                if self._parts:
                    self._retain("\n")
                self._retain(prefix)
            self._retain(fragment)

    def _retain(self, text: str) -> None:
        if self.full or not text:
            return
        start = 0
        if not self._parts:
            # Leading whitespace never reached the joined-and-stripped result,
            # so it does not spend budget here; scan past it rather than copy
            # the (possibly oversized) fragment to strip it.
            while start < len(text) and text[start].isspace():
                start += 1
        kept = text[start : start + self._remaining]
        if kept:
            self._parts.append(kept)
            self._remaining -= len(kept)
            self._consumed += len(kept)

    def render(self) -> str:
        """Join only the retained fragments; trailing whitespace is dropped."""
        return "".join(self._parts).rstrip()


def _until_full(values: Iterable[Any], output: _BoundedBlockText) -> Iterator[Any]:
    """Yield *values* only while *output* has budget, never reading past it."""
    iterator = iter(values)
    while not output.full:
        try:
            yield next(iterator)
        except StopIteration:
            return


@overload
def _extract_blocks_text(
    blocks: object,
    limit: int = _MAX_RECOVERED_TEXT_CHARS,
    *,
    include_section_fields: bool = False,
    _with_budget: Literal[False] = False,
) -> str: ...


@overload
def _extract_blocks_text(
    blocks: object,
    limit: int,
    *,
    include_section_fields: bool = False,
    _with_budget: Literal[True],
) -> tuple[str, int, bool]: ...


def _extract_blocks_text(
    blocks: object,
    limit: int = _MAX_RECOVERED_TEXT_CHARS,
    *,
    include_section_fields: bool = False,
    _with_budget: bool = False,
) -> str | tuple[str, int, bool]:
    """Extract readable text from Block Kit blocks (rich_text, section, context, table).

    Handles the common block types Slack uses for user messages and shared
    content.  Returns empty string if no text can be recovered.
    Defensive: never raises on malformed input.

    This is the one Block Kit renderer: forwarded-message recovery and trusted-bot
    attachment recovery both go through it.  *limit* is a caller-requested
    smaller traversal budget, clamped to ``_MAX_RECOVERED_TEXT_CHARS`` rather
    than an override of the global cap. Once the effective budget is spent no
    further block, element or inline fragment is read and nothing past it is
    retained, so the recovered output never exceeds the cap.
    ``include_section_fields`` is enabled only for trusted ordinary cards;
    forwarding callers leave it off so field-only shares retain empty output.
    ``_with_budget`` exposes that same renderer's consumed-character count and
    exhaustion state to the trusted-bot outer attachment budget.
    """
    effective_limit = min(max(0, limit), _MAX_RECOVERED_TEXT_CHARS)
    if not isinstance(blocks, list) or effective_limit <= 0:
        if _with_budget:
            return "", 0, effective_limit <= 0
        return ""
    out = _BoundedBlockText(effective_limit)
    for block in _until_full(blocks, out):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "rich_text":
            elements = block.get("elements")
            if not isinstance(elements, list):
                continue
            for element in _until_full(elements, out):
                if not isinstance(element, dict):
                    continue
                el_type = element.get("type")
                child_els = element.get("elements")
                if not isinstance(child_els, list):
                    continue
                if el_type == "rich_text_list":
                    # Prefix each list item with "- " to preserve list structure.
                    # (Numbered vs bulleted distinction not preserved — simple bullet.)
                    for child in _until_full(child_els, out):
                        if not isinstance(child, dict):
                            continue
                        sub_els = child.get("elements")
                        if isinstance(sub_els, list):
                            out.add_line(map(_render_rich_text_element, sub_els), prefix="- ")
                elif el_type == "rich_text_quote":
                    # Quote blocks: prefix with "> "
                    out.add_line(map(_render_rich_text_element, child_els), prefix="> ")
                else:
                    # rich_text_section, rich_text_preformatted
                    out.add_line(map(_render_rich_text_element, child_els))
        elif block_type == "section":
            text_obj = block.get("text")
            if isinstance(text_obj, dict):
                out.add_line((_string_field(text_obj, "text"),))
            if include_section_fields:
                fields = block.get("fields")
                if isinstance(fields, list):
                    for field in _until_full(fields, out):
                        if isinstance(field, dict):
                            out.add_line((_string_field(field, "text"),))
        elif block_type == "context":
            ctx_elements = block.get("elements")
            if not isinstance(ctx_elements, list):
                continue
            for el in _until_full(ctx_elements, out):
                if isinstance(el, dict):
                    out.add_line((_string_field(el, "text"),))
        elif block_type == "table":
            # Pasted spreadsheet: one "a | b" line per row. A cell is raw_text
            # (its text field) or rich_text (rendered like a rich_text block).
            rows = block.get("rows")
            if not isinstance(rows, list):
                continue
            for row in _until_full(rows, out):
                if isinstance(row, list):
                    out.add_line(_table_row_fragments(row))
    rendered = out.render()
    if _with_budget:
        return rendered, out.consumed, out.full
    return rendered


def _table_row_fragments(row: list) -> Iterator[str]:
    """Yield one table row as ``" | "``-joined cell fragments, rendering each cell lazily.

    Matches ``" | ".join(cells)`` over the row's dict cells, and yields nothing
    when every cell renders empty, so an all-blank row adds no line.
    """
    pending = ""
    seen_text = False
    first = True
    for cell in row:
        if not isinstance(cell, dict):
            continue
        if not first:
            pending += " | "
        first = False
        if cell.get("type") == "rich_text":
            text = _extract_blocks_text([cell])
        else:
            text = str(cell.get("text") or "")
        if text:
            seen_text = True
            yield pending
            pending = ""
            yield text
    if seen_text and pending:
        yield pending


# Slack's generic fallback strings for messages whose content lives in blocks.
# NOTE: These are best-effort, undocumented, English-only Slack placeholder strings.
# They may change or be localized — recovery is best-effort for non-English workspaces.
# No fuzzy/structural detection is attempted (out of scope; would change behavior broadly).
_SLACK_BLOCK_FALLBACKS = frozenset(
    {
        "This message contains interactive elements.",
        "This content can't be displayed.",
    }
)


def _normalize_message_blocks(raw: list) -> list[dict]:
    """Drill into the Slack message_blocks wrapper structure.

    ``message_blocks`` is a wrapper list:
    ``[{"team":..., "channel":..., "ts":..., "message": {"blocks": [...]}}]``
    This extracts and flattens the inner blocks from each wrapper.
    """
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        msg = item.get("message")
        if isinstance(msg, dict):
            inner_blocks = msg.get("blocks")
            if isinstance(inner_blocks, list):
                result.extend(b for b in inner_blocks if isinstance(b, dict))
    return result


def _extract_shared_text(event: dict) -> str:
    """Recover message text from forwarded-message attachments.

    Slack forwards carry their content in the ``attachments`` array (entries
    flagged ``is_share`` / ``is_msg_unfurl``), not in the top-level ``text``
    field. Link-unfurl attachments are excluded so pasted URLs don't leak
    preview text into the routed message body.

    When the attachment's ``text`` is empty and ``fallback`` is a generic
    Slack placeholder (e.g. "This message contains interactive elements."),
    attempts to reconstruct content from the attachment's ``blocks`` or the
    event-level ``blocks`` array.
    """
    attachments = event.get("attachments")
    if not isinstance(attachments, list):
        attachments = []
    parts: list[str] = []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        if not (att.get("is_share") or att.get("is_msg_unfurl")):
            continue
        att_text = _string_field(att, "text")
        if att_text:
            parts.append(att_text)
            continue
        # text is empty — try blocks before falling back to the generic fallback.
        # att["blocks"] is already a flat block list; att["message_blocks"] is a
        # wrapper list that must be normalized first.
        att_blocks = att.get("blocks")
        if isinstance(att_blocks, list) and att_blocks:
            extracted = _extract_blocks_text(att_blocks)
            if extracted:
                parts.append(extracted)
                continue
        msg_blocks = att.get("message_blocks")
        if msg_blocks:
            normalized = _normalize_message_blocks(msg_blocks)
            if normalized:
                extracted = _extract_blocks_text(normalized)
                if extracted:
                    parts.append(extracted)
                    continue
        # Last resort: use fallback unless it's a generic Slack placeholder
        fallback = _string_field(att, "fallback")
        if fallback and fallback not in _SLACK_BLOCK_FALLBACKS:
            parts.append(fallback)
    # If attachments yielded nothing, try event-level blocks (Slack sometimes
    # puts the real content there for shared messages).
    if not parts:
        event_blocks = event.get("blocks")
        if isinstance(event_blocks, list) and event_blocks:
            extracted = _extract_blocks_text(event_blocks)
            if extracted:
                return extracted
    return "\n\n".join(part for part in parts if part).strip()


def _extract_table_text(event: dict) -> str:
    """Render pasted-spreadsheet ``table`` blocks of *event* and its non-link-preview attachments."""
    blocks = event.get("blocks")
    blocks = list(blocks) if isinstance(blocks, list) else []
    for att in event.get("attachments") or []:
        if (
            isinstance(att, dict)
            and not att.get("from_url")
            and isinstance(att.get("blocks"), list)
        ):
            blocks.extend(att["blocks"])
    tables = [b for b in blocks if isinstance(b, dict) and b.get("type") == "table"]
    return _extract_blocks_text(tables) if tables else ""


def _extract_trusted_bot_attachment_text(event: dict) -> str:
    """Recover ordinary attachment text for an already-admitted trusted bot.

    Human messages keep the shared-message-only path so link previews never
    become routed content. Trusted bot cards prefer Slack's plain-text fallback
    and use their Block Kit text -- rendered by ``_extract_blocks_text``, the same
    renderer the shared path uses -- only when the fallback is absent or generic.

    ``_MAX_RECOVERED_TEXT_CHARS`` bounds the whole result as a budget: each
    attachment is rendered only up to what is left, and once the budget cannot
    fit another separator and character no later attachment is read, so the
    external sequence is never joined in full before being capped.
    """
    attachments = event.get("attachments")
    if not isinstance(attachments, list):
        return ""
    separator = "\n\n"
    parts: list[str] = []
    remaining = _MAX_RECOVERED_TEXT_CHARS
    for attachment in attachments:
        separator_cost = len(separator) if parts else 0
        budget = remaining - separator_cost
        if budget <= 0:
            break
        if not isinstance(attachment, dict):
            continue
        if attachment.get("is_share") or attachment.get("is_msg_unfurl"):
            continue
        # Placeholder classification needs the bounded candidate, not its output-sized
        # prefix: a later attachment may have only one output character left.
        fallback = _string_field(attachment, "fallback")[:_MAX_RECOVERED_TEXT_CHARS].strip()
        consumed = 0
        renderer_exhausted = False
        if fallback and fallback not in _SLACK_BLOCK_FALLBACKS:
            candidate = fallback[:budget]
            consumed = len(candidate)
        else:
            candidate, consumed, renderer_exhausted = _extract_blocks_text(
                attachment.get("blocks"),
                budget,
                include_section_fields=True,
                _with_budget=True,
            )
        if not candidate:
            remaining -= consumed
            if renderer_exhausted:
                break
            continue
        parts.append(candidate)
        remaining -= separator_cost + consumed
        if renderer_exhausted:
            break
    return separator.join(parts).strip()
