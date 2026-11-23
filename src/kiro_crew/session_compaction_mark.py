"""The persisted copy of a session's post-compaction re-injection mark.

The in-memory mark (``_Session.needs_context_reinjection``) does not survive a
restart or a replaced session record, so a resume right after a compaction would
miss the session-start context the compaction took. Its copy in the session map
(:data:`~kiro_crew.session_map.COMPACTED_FLAG`) is written when the mark is armed,
read once per session record, cleared once the turn that read it lands, and
dropped by a fresh claim, which sends the full session start anyway.

``CompactionCoordinator`` and ``note_session_head`` call these; nothing else
writes the flag.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.session_map import COMPACTED_FLAG

logger = logging.getLogger("kiro_crew.session")


def persist(session_map: Any, folded: str) -> None:
    """Record that ``folded`` owes the post-compaction context."""
    session_map.set_flag(folded, COMPACTED_FLAG, True)


async def aflush(session_map: Any, key: str) -> None:
    """Write the mark to disk before the compaction is reported.

    A restart right after the compaction then still owes the re-injection. The
    write runs off the loop; a failure is logged, and the mark is still in memory.
    """
    try:
        await session_map.aflush()
    except Exception:
        logger.warning("post-compaction mark for %s not written to disk yet", key, exc_info=True)


def take(session: Any, session_map: Any, folded: str) -> bool:
    """True once per session record while the persisted mark is set.

    The copy stays in place: :func:`settle` clears it when a turn lands, so a
    process that stops before then still owes the context on its next resume.
    """
    if getattr(session, "compaction_mark_taken", False) is True:
        return False
    return session_map.get_flag(folded, COMPACTED_FLAG) is True


def settle(session: Any, session_map: Any, folded: str) -> None:
    """Clear the persisted mark once a turn of the record that took it has landed.

    Not while a newer mark is armed: a compaction during the turn, or a re-arm,
    keeps it owed.
    """
    if getattr(session, "compaction_mark_taken", False) is not True:
        return
    if session.needs_context_reinjection:
        return
    drop(session_map, folded)


def drop(session_map: Any, folded: str) -> None:
    """Clear the persisted mark, if set."""
    if session_map.get_flag(folded, COMPACTED_FLAG) is True:
        session_map.set_flag(folded, COMPACTED_FLAG, False)
