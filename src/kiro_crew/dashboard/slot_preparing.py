"""Mark a slot while the gateway prepares an assistant message outside a turn.

Some producers post an assistant row after a background model call that no
turn owns (the warm task brief is one). ``slot.running`` stays false for them,
so without a mark the chat shows no working indicator and the thread looks
hung. :func:`preparing_message` raises the slot's ``preparing`` flag in the
slots frame for exactly the span it wraps, with an optional caption saying
what is being prepared.

The flag is display-only. It never sets ``running``, admits nothing and holds
no lock, so sends, queueing, patrol turns and any producer logic that reads
``slot.running`` behave as if it did not exist. It lives in memory only: a
gateway restart drops the work and the flag together, and a reconnecting client
reads the current value from the next slots frame.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def is_preparing(slot: "_ChatSlot") -> bool:
    """Whether any background message is being prepared for *slot* now."""
    return int(getattr(slot, "_preparing_count", 0) or 0) > 0


def preparing_caption(slot: "_ChatSlot") -> tuple[str, str]:
    """``(text, language)`` of the newest open span's caption, ``("", "")`` for none."""
    captions = getattr(slot, "_preparing_texts", None) if is_preparing(slot) else None
    return next((c for c in reversed(captions or ()) if c[0]), ("", ""))


@contextmanager
def preparing_message(
    state: "DashboardState", slot: "_ChatSlot", text: str = "", language: str = ""
) -> Iterator[None]:
    """Show the slot's working indicator, captioned *text* in *language*, for the span.

    A client shows the caption only when *language* is its own UI language, and
    its generic caption otherwise. Post the finished row INSIDE the span so a
    client sees the row before the indicator drops. The flag and its caption
    clear on every exit, cancellation included.
    """
    captions = getattr(slot, "_preparing_texts", None)
    if not isinstance(captions, list):
        captions = slot._preparing_texts = []
    caption = (text, language)
    captions.append(caption)
    slot._preparing_count = int(getattr(slot, "_preparing_count", 0) or 0) + 1
    state.push_slots_update()
    try:
        yield
    finally:
        slot._preparing_count = max(0, slot._preparing_count - 1)
        captions.remove(caption)
        state.push_slots_update()
