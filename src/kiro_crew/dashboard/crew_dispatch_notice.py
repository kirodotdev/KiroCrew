"""The Crew Mode dispatch hint row: what it is and how a turn's tool-group walks treat it.

A leaf on purpose: the session-control part that writes the row and the chat-turn
walks that must read past it both import it, and it imports nothing of theirs.

The row is written by ``session_create`` while that call's own tool row is still open,
so it sits inside the turn's tool group, the way a ``card`` row a call puts in does.
The post-tool "mark tools done" walk, ``_flush_segment``'s segment walk and
``_tool_rows_settled`` read past it like a ``card`` row; otherwise they would stop at
it and leave an earlier tool of the group open.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

#: ``meta.kind`` of the hint row; the dashboard's renderer keys on it.
CREW_DISPATCH_HINT_KIND: Final[str] = "crew_dispatch_hint"


def is_crew_dispatch_hint(row: Mapping[str, Any]) -> bool:
    """Whether *row* is the gateway's Crew Mode dispatch hint."""
    meta = row.get("meta")
    return (
        row.get("role") == "notice"
        and isinstance(meta, Mapping)
        and meta.get("kind") == CREW_DISPATCH_HINT_KIND
    )
