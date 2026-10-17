"""Chunk intake and the live frame of a Telegram turn.

Text chunks arrive here: steer markers rotate the segment, the held steer chip is
materialized, trailing ``[OPTIONS:]`` markup is held back from the live frame, and the
plaintext live frame is edited in place at most every ``_EDIT_THROTTLE_S``, then retired
or deleted when its segment is sealed. Each function here whose first parameter is
``self`` is bound as the ``TelegramRenderer`` attribute of the same name in
:mod:`kiro_crew.telegram.renderer`, which keeps the renderer's state, so
``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_buf``, ``_chat_id``, ``_client``,
``_frame_lock``, ``_last_edit``, ``_pending_chip``, ``_reply_to``, ``_shown``,
``_steer_texts``, ``_stream_mid``, ``_thread_id`` and ``_tool``, and write ``_buf``,
``_last_edit``, ``_pending_chip``, ``_reply_to``, ``_shown``, ``_shown_stall``,
``_stream_mid`` and ``_tool``. New live-frame work goes here; sealing stays in
:mod:`kiro_crew.telegram.renderer`.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.constants import strip_control_comments
from kiro_crew.telegram.renderer_display import (
    _STEER_MARKER_RE,
    _STEER_SUMMARY_RE,
    _extract_options,
    _neutralize_md,
    _strip_hr,
    _strip_steering,
)

if TYPE_CHECKING:
    from kiro_crew.telegram.renderer import TelegramRenderer

logger = logging.getLogger("kiro_crew.telegram.renderer")


# Min seconds between live streaming edits to one message. Telegram rate-limits
# edits (~this cadence), so we coalesce chunks and edit the message in place at
# most this often. The final formatted edit always lands regardless of throttle.
_EDIT_THROTTLE_S = 1.0


async def on_text_chunk(self: TelegramRenderer, text: str) -> None:
    self._buf.append(text)
    self._note_progress()
    self._tool = ""  # text resumed -> drop the transient tool footer
    # 1) Defensive fallback for callers that bypass TurnDriver and
    #    deliver raw protocol text directly to the renderer.
    await self._rotate_at_markers()
    # 1b) Materialize the pending chip once real post-steer text exists.
    self._materialize_chip()
    # 2) Rotate when a segment would exceed one Telegram message.
    await self._rotate_on_length()
    # 3) Live-stream the current segment: edit the message in place (throttled).
    await self._stream_live()


def _materialize_chip(self: TelegramRenderer) -> None:
    """Prepend the pending steer chip to the segment — but only when the
    segment carries real text. Keeps an end-of-stream marker from ever
    posting a chip-only ack bubble."""
    if self._pending_chip and self._segment_text().strip():
        body = "".join(self._buf).lstrip("\n")
        self._buf = [f"{self._pending_chip}\n\n{body}"]
        self._pending_chip = ""


async def _rotate_at_markers(self: TelegramRenderer) -> None:
    """Defence for callers that bypass TurnDriver and pass raw markers."""
    while True:
        self._materialize_chip()
        raw = "".join(self._buf)
        marker = _STEER_MARKER_RE.search(raw)
        if marker is None:
            return
        self._buf = [raw[: marker.start()]]
        summary_match = _STEER_SUMMARY_RE.match(raw, marker.start())
        summary = _neutralize_md(summary_match.group(1)) if summary_match else ""
        await self.on_steer_consumed(summary)
        self._buf = [raw[marker.end() :]]


def _open_new_message(self: TelegramRenderer) -> None:
    """Next render creates a fresh message instead of editing the old one."""
    self._stream_mid = None
    self._shown = ""


def _segment_text(self: TelegramRenderer) -> str:
    """Current segment's markdown source (chip already seeded into _buf),
    with the steer marker and horizontal-rule noise stripped."""
    return _strip_hr(_strip_steering("".join(self._buf)))


def _consume_reply_to(self: TelegramRenderer) -> int | None:
    """The reply target for this send, spent so only the FIRST one carries it.

    Every later bubble of the same turn — a rotated frame, an image, a
    follow-up segment — is already attached by adjacency, and a reply quote on
    each one would triple the visual weight of a multi-part answer for no
    added information. Consuming here rather than at the call sites means a
    send path that runs first cannot leave the target for a later one to spend
    a second time; whichever path opens the turn gets it.
    """
    target, self._reply_to = self._reply_to, None
    return target


async def _stream_live(self: TelegramRenderer, *, force: bool = False) -> None:
    """Throttled in-place edit of the current segment (plaintext, so partial
    markdown never 400s). Sends the message on first render. The transient
    ``🔧 {tool}…`` footer and the stall mark are appended ONLY here (live
    frames) — seals and the final render read ``_segment_text`` and never
    carry either. ``force`` bypasses the throttle so a tool-call event, or a
    stall mark the typing tick noticed, surfaces immediately.

    Serialized by ``_frame_lock``: the typing loop is a SEPARATE task, so
    without it a stall-mark frame could interleave with a token frame and
    leave the older text on screen.
    """
    async with self._frame_lock:
        await self._stream_live_locked(force=force)


async def _stream_live_locked(self: TelegramRenderer, *, force: bool) -> None:
    now = time.monotonic()
    if not force and now - self._last_edit < _EDIT_THROTTLE_S:
        return
    # Hold back trailing [OPTIONS:] markup (complete or still-streaming
    # partial) from live frames — it is an internal directive, extracted
    # into the inline keyboard at finalization.
    seg, _ = _extract_options(self._segment_text())
    # A control-tag line still arriving is held off the frame the same way.
    seg = strip_control_comments(seg, hide_partial=True)
    body = await self._safe_body(seg)
    stall = self._stall_mark()
    # The tool footer wins: it names what is happening, which is strictly
    # more informative than "nothing has happened".
    footer = f"🔧 {self._tool}…" if self._tool else stall
    if footer:
        # Keep the footer visible even when it must displace body tail chars.
        room = self._limit() - len(footer) - 2
        text = f"{body[:room]}\n\n{footer}".strip() if room > 0 else footer
    else:
        text = body[: self._limit()]
    # Recorded BEFORE the suppression check: when the frame is unchanged there
    # is nothing to send, but the typing tick compares against this to decide
    # whether to try at all — leaving it stale makes it retry every tick for
    # the rest of the turn.
    self._shown_stall = stall
    if not text or text == self._shown:
        return
    self._last_edit = now
    self._shown = text
    if self._stream_mid is None:
        mid = await self._client.send_message(
            self._chat_id,
            text,
            message_thread_id=self._thread_id,
            reply_to_message_id=self._consume_reply_to(),
        )
        if mid is not None:
            self._stream_mid = mid
    else:
        await self._client.edit_message(self._chat_id, self._stream_mid, text)


async def _retire_live_frame(self: TelegramRenderer) -> None:
    """Remove a live bubble that ended up carrying no answer of its own.

    Deleted rather than blanked: Telegram rejects an empty ``editMessageText``,
    and a placeholder would be one more thing above the real content. Failure
    is non-fatal — a stale footer is worse than nothing, not worse than a
    crash.
    """
    async with self._frame_lock:
        mid, self._stream_mid, self._shown = self._stream_mid, None, ""
    if mid is None:
        return
    try:
        await self._client.delete_message(self._chat_id, mid)
    except Exception:
        logger.debug("Telegram: retiring the live frame failed", exc_info=True)


async def _delete_replaced_frame(self: TelegramRenderer, mid: int | None) -> None:
    """Delete a live frame whose content landed in new messages instead.

    Both seal edits can fail while the frame still exists (the per-chat edit
    budget, or two timeouts), and the content is then sent as new messages
    below it. The frame is an older plain draft, so it is deleted, as the rich
    path deletes the bubble it supersedes. Call it only once every part of the
    segment has landed: until then the frame may show rows of the parts still
    missing that no sent message carries.
    Best-effort: a frame the user deleted cannot be deleted again. The
    callers hold ``_frame_lock``, so it takes no lock of its own, unlike
    ``_retire_live_frame``.
    """
    if mid is None:
        return
    try:
        await self._client.delete_message(self._chat_id, mid)
    except Exception:
        logger.debug("Telegram: deleting the replaced live frame failed", exc_info=True)


def note_steer(self: TelegramRenderer, text: str) -> None:
    """Record the user's own mid-turn steer text (their typed words, NOT the
    redacted backend echo). Called by the dispatcher when a steer is actually
    injected; rendered as an inline "↪️ steered: …" chip in on_done. Capped to
    avoid unbounded growth on a pathological steer burst."""
    t = (text or "").strip()
    if t and len(self._steer_texts) < 50:
        self._steer_texts.append(t)


def _chip_for_seal(self: TelegramRenderer, i: int) -> str | None:
    """The steer chip (a "> quote" blockquote of the USER's own words) that
    heads the segment opened by the i-th rotation. None when we have no
    recorded text for that rotation (chip is simply omitted)."""
    if 0 <= i < len(self._steer_texts):
        t = _neutralize_md(self._steer_texts[i])
        return f"> {t}" if t else None
    return None
