"""Chunk intake for a Discord turn: the segment buffer every frame and seal reads.

Text chunks arrive here: a complete steer marker rotates the segment, the held steer
chip is materialized once real text follows it, finished tables are converted for
delivery while the canonical buffer keeps the source, an authored ``[OPTIONS:]`` trailer
is taken from the canonical text, and the current segment is answered as it will be
shown. Each function here whose first parameter is ``self`` is bound as the
``DiscordRenderer`` attribute of the same name in :mod:`kiro_crew.discord.renderer`,
which keeps the renderer's state, so ``self.<name>(...)`` calls and instance and class
patches reach it as they reach a method defined in the class body. They read ``_buf``,
``_delivery_text``, ``_pending_chip``, ``_segment_uploads_safe``, ``_steer_texts`` and
``_table_pending``, write ``_bubble_above``, ``_buf``, ``_delivery_text``,
``_pending_chip``, ``_shown``, ``_stream_mid``, ``_table_pending`` and ``_tool``, and
append to ``_steer_texts``. New intake work goes here; the throttled live frame,
rotation and every seal stay in :mod:`kiro_crew.discord.renderer`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.constants import strip_control_comments
from kiro_crew.discord.renderer_text import (
    _STEER_MARKER_RE,
    _STEER_SUMMARY_RE,
    _extract_options,
    _neutralize_md,
    _strip_steering,
    _transform_buries_refs,
)
from kiro_crew.messaging.status_reactions import PHASE_THINKING
from kiro_crew.messaging.tables import TABLE_POLICY_CARDS

if TYPE_CHECKING:
    from kiro_crew.discord.renderer import DiscordRenderer


async def on_text_chunk(self: DiscordRenderer, text: str) -> None:
    self._set_phase(PHASE_THINKING)
    # Land any accumulated reasoning first so it reads above the answer
    # (no-op when show_thinking is off or nothing accumulated).
    await self._flush_thinking()
    self._buf.append(text)
    self._delivery_text = None
    self._tool = ""  # text resumed -> drop the transient tool footer
    # 1) Rotate to a fresh message at each COMPLETE [STEERING …] marker.
    await self._rotate_at_markers()
    # 1b) Materialize the pending chip once real post-steer text exists.
    self._materialize_chip()
    # 1c) Convert finished tables before anything measures the segment.
    await self._convert_tables(final=False)
    # A valid trailing table may still receive rows. Splitting or displaying
    # it now would either freeze a partial card or expose raw pipes; keep the
    # segment buffered until prose terminates the run or the turn finishes.
    if not self._table_pending:
        # 2) Rotate when a segment would exceed one Discord message.
        await self._rotate_on_length()
        # 3) Live-stream the current segment (throttled in-place edit).
        await self._stream_live()


def _render_tables_for_delivery(self: DiscordRenderer, raw: str, *, final: bool) -> str:
    """Adapt tables without handing a generated fence to message rotation."""
    converted = self.render_tables_for_target(raw, final=final)
    if converted != raw and len(converted) > self._limit():
        # Generated grids can require a longer fence when a cell contains
        # backticks. Prefer cards because they remain independently readable
        # across ordinary text chunks; an unrepresentable card run reports
        # its retained grid so a fitting safe raw table can stay whole.
        forced, generated_grid = self.render_tables_for_target_with_metadata(
            raw,
            policy=TABLE_POLICY_CARDS,
            final=final,
        )
        if generated_grid and len(forced) > self._limit():
            safe_raw = self.safe_raw_table_fallback(
                raw,
                policy=TABLE_POLICY_CARDS,
                final=final,
            )
            if safe_raw is not None and len(safe_raw) <= self._limit():
                return safe_raw
        return forced
    return converted


async def _convert_tables(self: DiscordRenderer, *, final: bool) -> None:
    """Refresh outbound table presentation while preserving canonical text.

    Protocol recognition always reads ``_buf``. The rendered snapshot is
    used only for streaming, sizing, and delivery, so card text that happens
    to resemble a control marker remains visible content. A transformed
    segment that already exceeds Discord's cap stays buffered until a
    terminal seal, when its presentation can be split without needing to
    map display offsets back onto still-growing Markdown source.

    A card is abandoned when it would bury a local image ref on a transport
    that can upload one: a delivered picture outranks table presentation,
    and shipping the raw path as text would both lose the upload and expose
    a local path. Such a segment degrades to raw pipes with a working image.
    """
    raw = _strip_steering("".join(self._buf))
    converted = self._render_tables_for_delivery(raw, final=final)
    self._delivery_text = converted if converted != raw else None
    if (
        self._delivery_text is not None
        and self._uploads_enabled()
        and self._segment_uploads_safe
        and await asyncio.to_thread(_transform_buries_refs, raw, converted)
    ):
        self._delivery_text = None
    if final:
        self._table_pending = False
        return

    final_render = self._render_tables_for_delivery(raw, final=True)
    trailing_table = converted != final_render
    transformed_over_limit = self._delivery_text is not None and len(converted) > self._limit()
    self._table_pending = trailing_table or transformed_over_limit


def _take_canonical_options(self: DiscordRenderer) -> list[str]:
    """Remove an authored OPTIONS trailer before presentation transforms.

    Table cards can join separate cells into text that resembles protocol.
    Extracting once from the canonical buffer ensures only an actual model
    directive becomes controls; generated display text remains content.
    """
    body, options = _extract_options(strip_control_comments("".join(self._buf)))
    # Trailing control-tag lines are protocol too, and a message that
    # carries both puts one of them last -- so strip on both sides of the
    # trailer. Complete tags only: the seal is the end of the stream, and a
    # partial tail there is the assistant's own prose.
    body = strip_control_comments(body)
    self._buf = [body]
    self._delivery_text = None
    return options


def _materialize_chip(self: DiscordRenderer) -> None:
    """Prepend the pending steer chip to the segment — but only when the
    segment carries real text (an end-of-stream marker never posts a
    chip-only ack bubble)."""
    if self._pending_chip and self._segment_text().strip():
        body = "".join(self._buf).lstrip("\n")
        self._buf = [f"{self._pending_chip}\n\n{body}"]
        self._delivery_text = None
        self._pending_chip = ""


async def _rotate_at_markers(self: DiscordRenderer) -> None:
    """Defence for callers that bypass TurnDriver and pass raw markers."""
    while True:
        self._materialize_chip()
        raw = "".join(self._buf)
        marker = _STEER_MARKER_RE.search(raw)
        if marker is None:
            return
        self._buf = [raw[: marker.start()]]
        self._delivery_text = None
        summary_match = _STEER_SUMMARY_RE.match(raw, marker.start())
        summary = _neutralize_md(summary_match.group(1)) if summary_match else ""
        await self.on_steer_consumed(summary)
        self._buf = [raw[marker.end() :]]
        self._delivery_text = None


def _open_new_message(self: DiscordRenderer) -> None:
    """Next render creates a fresh message instead of editing the old one."""
    self._stream_mid = None
    self._shown = ""
    # The next fresh send opens a new bubble whose predecessor is whatever the
    # reader last saw (``_sent_tail``); capture it there, not here, so this
    # stays empty until the bubble actually exists.
    self._bubble_above = ""


def _segment_text(self: DiscordRenderer) -> str:
    """Current outbound text, with protocol removed only from canonical source.

    Presentation snapshots are derived only after canonical protocol
    handling and must be returned verbatim: parsing them again could
    reinterpret table cards as control markers.
    """
    if self._delivery_text is not None:
        return self._delivery_text
    return _strip_steering("".join(self._buf))


def note_steer(self: DiscordRenderer, text: str) -> None:
    """Record the user's own mid-turn steer text (their typed words, NOT
    the redacted backend echo); rendered as an inline "↪️ steered" chip.
    Capped to avoid unbounded growth on a pathological steer burst."""
    t = (text or "").strip()
    if t and len(self._steer_texts) < 50:
        self._steer_texts.append(t)


def _chip_for_seal(self: DiscordRenderer, i: int) -> str | None:
    """The steer chip (a "> quote" blockquote of the USER's own words) that
    heads the segment opened by the i-th rotation."""
    if 0 <= i < len(self._steer_texts):
        t = _neutralize_md(self._steer_texts[i])
        return f"> ↪️ {t}" if t else None
    return None
