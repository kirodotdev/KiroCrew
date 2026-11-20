"""The prompt text each dashboard turn sent to the agent, in memory, for the Context tab.

The Context tab says how many characters each block of a turn had. A prompt bug (a
rule the model ignored, a block that doubled) needs the text itself, so this module
keeps it for the newest turns of each session. Process memory only: a gateway restart
forgets it and nothing reaches disk. The opt-in wire recorder
(``acp/_frame_record.py``) is the durable capture.

What is kept is the text the provider handed its transport, after
:class:`~kiro_crew.essential_delivery.EssentialDelivery` swapped its receipt envelope,
so a member turn whose private section was already delivered shows the prompt without
it, as the agent read it. The dashboard runner names a prompt's turn with
:func:`expect` just before the stream, and the delivery reports the text once, at the
send.

A record is keyed by the session's crew-log unit (its ACP session id) and the turn
number the composer stamps on ``context/composed``. Every dashboard turn with that
number (a retry, a regenerate, a turn after a rewind, a slash command or an
agent-started turn too) drops the records from it on through :func:`forget_from`,
called once by the runner before it opens any stream, as the Context fold drops its
rows, so a turn that keeps no text shows none rather than an earlier turn's. A prompt over
``MAX_PROMPT_CHARS`` keeps :data:`TOO_LONG` in its turn's place instead of its text, so
the reader can say why it is missing. The reader masks secrets:
``dashboard/handlers/prompt_as_sent.py``.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections import OrderedDict
from typing import Final, Literal, Union

from kiro_crew.essential_delivery import EssentialDelivery

logger = logging.getLogger(__name__)

#: Newest prompts kept per session.
MAX_TURNS: Final = 8
#: A longer prompt's text is not kept, so one huge paste cannot push every other session
#: out; its turn keeps :data:`TOO_LONG`, which counts no characters.
MAX_PROMPT_CHARS: Final = 1_000_000
#: Characters kept across all sessions; past it the least recently written session goes.
#: One session's most (``MAX_TURNS`` full prompts) fits, so the newest is never dropped.
MAX_TOTAL_CHARS: Final = MAX_TURNS * MAX_PROMPT_CHARS
#: Sessions kept, so many small sessions cannot grow the table without bound.
MAX_SESSIONS: Final = 32


class _TooLong(enum.Enum):
    TOO_LONG = "too_long"


#: Kept for a turn whose prompt was over ``MAX_PROMPT_CHARS``, in place of its text.
TOO_LONG: Final = _TooLong.TOO_LONG
#: What a record holds for its turn: the text as sent, or :data:`TOO_LONG`.
_Text = Union[str, Literal[_TooLong.TOO_LONG]]

_lock = threading.Lock()
#: unit -> its records as ``(turn, text)``, oldest first; least recently written first.
_sessions: "OrderedDict[str, list[tuple[int, _Text]]]" = OrderedDict()
_total = 0


def forget_from(unit: str, turn: int) -> None:
    """Drop turn *turn* of *unit* and every later one, before that turn sends.

    An attempt that never reaches the send (or has not yet) then shows nothing rather
    than an earlier turn's text. The unit keeps its place in the eviction order.
    """
    global _total
    with _lock:
        old = _sessions.get(unit)
        if old is None:
            return
        kept = [(n, t) for n, t in old if n < turn]
        _total -= _chars(old) - _chars(kept)
        if kept:
            _sessions[unit] = kept
        else:
            del _sessions[unit]


def expect(client: object, prompt: str, unit: str, turn: int) -> None:
    """Keep the text *client* sends for *prompt* as turn *turn* of *unit*."""
    delivery = getattr(client, "essential_delivery", None)
    if unit and turn > 0 and isinstance(delivery, EssentialDelivery):
        delivery.report_next_send(prompt, lambda text: record(unit, turn, text))


def record(unit: str, turn: int, text: str) -> None:
    """Keep *text* as turn *turn* of *unit*, dropping that turn and every later one."""
    global _total
    text = str(text)
    too_long = len(text) > MAX_PROMPT_CHARS
    with _lock:
        prior = _drop_from(unit, turn)
        kept = [*prior, (turn, TOO_LONG if too_long else text)][-MAX_TURNS:]
        _total += _chars(kept) - _chars(prior)
        _sessions[unit] = kept
        evicted: list[tuple[str, str]] = []
        while len(_sessions) > 1 and (len(_sessions) > MAX_SESSIONS or _total > MAX_TOTAL_CHARS):
            cap = "MAX_SESSIONS" if len(_sessions) > MAX_SESSIONS else "MAX_TOTAL_CHARS"
            gone, records = _sessions.popitem(last=False)
            _total -= _chars(records)
            evicted.append((gone, cap))
    for gone, cap in evicted:
        logger.info("prompt_trace: %s no longer kept: over %s", gone, cap)
    if too_long:
        logger.info(
            "prompt_trace: turn %d of %s not kept: over MAX_PROMPT_CHARS (%d characters)",
            turn,
            unit,
            MAX_PROMPT_CHARS,
        )


def read(unit: str, turn: int) -> _Text | None:
    """Turn *turn* of *unit* as sent, :data:`TOO_LONG` when it was over the cap, or
    None when it is not kept."""
    with _lock:
        return next((t for n, t in _sessions.get(unit, ()) if n == turn), None)


def _drop_from(unit: str, turn: int) -> list[tuple[int, _Text]]:
    """Pop *unit* and return its records before *turn*; the caller holds ``_lock``."""
    global _total
    old = _sessions.pop(unit, [])
    kept = [(n, t) for n, t in old if n < turn]
    _total -= _chars(old) - _chars(kept)
    return kept


def _chars(records: list[tuple[int, _Text]]) -> int:
    return sum(len(t) for _, t in records if isinstance(t, str))
