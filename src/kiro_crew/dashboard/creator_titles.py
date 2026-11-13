"""The last title each session was seen under, so a CLOSED creator can still be named.

A row whose creator has closed cites that creator by its slot key (``parent.slot``),
and a slot key is an id no reader recognises. A closed creator's title is in
neither payload -- both build their names from the LIVE rows -- and the
place it does live, the history metadata, is a disk read that neither payload may
make: the slots frame is assembled on the event loop, and the Sessions table joins
its lineage there too (see :func:`~kiro_crew.dashboard.session_memory.lineage_parents`).

So the title is remembered while the session is still LIVE, from the rows the two
payloads already serialize, and read back when a child cites a creator that is no
longer among them. Pure memory, no disk, no blocking: every write and read is a dict
operation on the event loop.

What it deliberately is not:

* Not durable. A gateway restart forgets every title, and a creator that closed
  before this process ever served a frame for it is simply not known here. The
  caller then renders the graceful generic label ("Created by a closed session"),
  which is exactly the answer it gave before this module existed.
* Not a new source of text. Every title it holds is one a payload already sent to
  the same dashboard, ALREADY redacted by the serializer that produced it (``slot
  .display_title`` through ``slot_projection``; ``session_title`` for the Sessions
  table), so naming a closed creator with it exposes nothing a reader had not seen.
* Not unbounded. A long-lived gateway opens sessions without limit, so the map is an
  LRU capped at :data:`_CAP` entries per spelling; a creator evicted from it falls
  back to the generic label like one this process never saw.

The placeholder title a session carries before one is generated is never stored:
naming a closed creator "New Session…" says less than the generic label does.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable

#: Most spellings remembered at once. A session is stored under every spelling its
#: rows use (bare slot key, ``dashboard:`` key, channel alias), so this is a few
#: hundred sessions -- far more than are ever cited by a still-open child at once.
_CAP = 1024

#: Longest title retained, in characters. The entry count alone does not bound memory:
#: a title restored from transcript metadata has no length ceiling of its own, and the
#: registry keeps it after the session closes. A label line shows far fewer characters
#: than this, so the cut never shortens a title a reader could see whole.
_MAX_TITLE_CHARS = 200

_titles: OrderedDict[str, str] = OrderedDict()


def remember(spellings: Iterable[str], title: object, *, placeholder: str) -> None:
    """Record *title* as the last-known title under every one of *spellings*.

    A blank title, a non-string and *placeholder* (the not-yet-titled text) are
    ignored, so a session that has not been named yet never overwrites a name it
    had, and is never recorded as one. A title longer than :data:`_MAX_TITLE_CHARS`
    is kept cut to that length with a trailing ellipsis.
    """
    if not isinstance(title, str):
        return
    title = title.strip()
    if not title or title == placeholder:
        return
    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    for spelling in spellings:
        if not isinstance(spelling, str) or not spelling:
            continue
        _titles[spelling] = title
        _titles.move_to_end(spelling)
    while len(_titles) > _CAP:
        _titles.popitem(last=False)


def last_known(spellings: Iterable[str]) -> str | None:
    """The last title remembered under the first of *spellings* that has one, or ``None``."""
    for spelling in spellings:
        if isinstance(spelling, str) and spelling:
            title = _titles.get(spelling)
            if title is not None:
                return title
    return None


def _reset_for_tests() -> None:
    """Forget every title. Test isolation only."""
    _titles.clear()
