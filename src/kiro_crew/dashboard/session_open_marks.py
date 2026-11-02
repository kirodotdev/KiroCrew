"""Mark the session search rows whose transcript a live dashboard slot owns.

The Sessions search ranks a session that is open in the dashboard above older
history. Only the server can say which transcript an open slot writes: a cron tab
is ``cron-<id>`` live but stores ``cron_<id>``, a channel tab stores its channel
stem, and an ordinary tab stores ``dashboard_<slot>``. :func:`slot_history_key`
already owns that mapping, so the search rows are compared with it here instead
of the client guessing from slot names.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def open_transcript_stems(slots: Iterable[Any]) -> set[str]:
    """Every transcript filename stem one of *slots* writes to.

    A search row's ``key`` is its transcript's filename stem, so this is the set
    a row is looked up in. Both stems :func:`transcript_stems` names are kept,
    so a Slack thread stored under its pre-migration bare stem still matches.
    """
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key
    from kiro_crew.history import transcript_stems

    stems: set[str] = set()
    for slot in slots:
        stems.update(transcript_stems(slot_history_key(slot)))
    return stems


def mark_open_sessions(rows: Iterable[dict], state: Any) -> None:
    """Set ``open: True`` on each local search row a live slot of *state* owns.

    Only ``True`` is ever written; a row that is not open carries no new field.
    Rows from another gateway never reach this function. A state with no slot
    registry (a test double) marks nothing.
    """
    stems = open_transcript_stems(list((getattr(state, "_slots", None) or {}).values()))
    if not stems:
        return
    for row in rows:
        if row.get("key") in stems:
            row["open"] = True
