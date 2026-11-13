"""Where Slack's splitters break a long text: the last break in reach that counts.

``format.split_message`` (separate posts, line breaks only) and
``reply_pieces._append_pieces`` (appends to one streamed message, line breaks
then spaces) both cut here. A break counts only when it sits at least a quarter
into the budget, the floor ``messaging.split``'s ladder gives a line break. A
break nearer the start strands most of the budget: ``"Hi\\n"`` plus one long
answer becomes three pieces, not two.
"""

from __future__ import annotations

#: The separators ``last_break`` tries by default, in preference order.
LINE_OR_SPACE = ("\n", " ")

#: A break counts when the text before it and the separator together reach
#: ``reach // _FLOOR_DIVISOR``.
_FLOOR_DIVISOR = 4


def last_break(text: str, reach: int, separators: tuple[str, ...] = LINE_OR_SPACE) -> int:
    """Index of the separator to break *text* at within ``text[:reach]``, or -1.

    The last of the first *separators* that sits at or past the floor wins,
    tried in order. With none, the last separator in reach is still taken when
    the run after it (up to the next separator) fits one piece of ``reach``
    characters, so a long URL behind a short lead-in moves whole; a run too long
    for any piece is cut anyway (-1), so the budget is filled instead. A
    separator at index 0 never counts: breaking there leaves an empty piece.
    """
    floor = reach // _FLOOR_DIVISOR
    for separator in separators:
        index = text.rfind(separator, 0, reach)
        if index > 0 and index + 1 >= floor:
            return index
    index = max(text.rfind(separator, 0, reach) for separator in separators)
    if index <= 0:
        return -1
    start = index + 1
    ends = [i for i in (text.find(s, start, start + reach + 1) for s in separators) if i >= 0]
    run_end = min(ends) if ends else min(start + reach + 1, len(text))
    return index if run_end - start <= reach else -1
