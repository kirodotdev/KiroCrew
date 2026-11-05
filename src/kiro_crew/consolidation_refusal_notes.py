"""The semantic values a consolidation pass refused for size, carried into the next prompt.

The consolidation size bound (``history_consolidation_memory._refuse_oversized_semantic``)
refuses an over-bound value whole, and the pass still marks its span consolidated, so the
messages that held the fact are not read again. Without a note the model never learns that
the value was dropped. Each refused value is recorded here, in the store's own
``memory_meta`` table (every lineage has it, so no schema change and no new file), and the
session's next consolidation prompt lists it so the model can propose the fact again as
separate keys within the bound. The note is cleared once a pass that showed it has written
its answer.

The record is one row, :data:`REFUSAL_NOTES_META_KEY`, holding a JSON list of at most
:data:`MAX_REFUSAL_NOTES` entries, newest last; a new refusal pushes the oldest one out. An
entry is ``{"session", "key", "value", "chars"}``. Each value is one ``validate_semantic``
already let through, so it is under the store's byte ceiling and has passed its injection
check, the same checks a stored row has passed before the prompt's semantic table shows
it. Only the session that refused a value is shown it, so a fact is offered back to the
conversation it came from. The audit row in ``memory_events`` stays as it is; this record
holds the whole value, which that row cuts to 200 characters.

Every function here is best effort: a record that cannot be read or written costs the
note, never the pass, and is logged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger("kiro_crew.history")

#: The ``memory_meta`` key that holds the pending refusal notes.
REFUSAL_NOTES_META_KEY = "consolidation_refused_semantic"

#: How many refused values the record keeps across every session of the store.
MAX_REFUSAL_NOTES = 5

#: The heading of the prompt section that lists a session's refused values.
REFUSAL_NOTICE_HEADING = "## Semantic Values Refused Last Pass"


def _decode(raw: object) -> list[dict]:
    """The well-formed entries of a stored record, in order; ``[]`` for anything else."""
    if not isinstance(raw, str):
        return []
    try:
        decoded = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(decoded, list):
        return []
    return [
        entry
        for entry in decoded
        if isinstance(entry, dict)
        and isinstance(entry.get("session"), str)
        and isinstance(entry.get("key"), str)
        and isinstance(entry.get("chars"), int)
        and "value" in entry
    ]


def _read(store: VectorMemoryStore) -> list[dict]:
    return _decode(store._read_meta(REFUSAL_NOTES_META_KEY))


def _write(store: VectorMemoryStore, entries: list[dict]) -> None:
    """Replace the record with *entries*; the caller holds ``store._db_lock``."""
    store._write_meta_in_transaction(
        REFUSAL_NOTES_META_KEY, json.dumps(entries, ensure_ascii=False)
    )
    store.db.commit()


def _rollback(store: VectorMemoryStore) -> None:
    try:
        store.db.rollback()
    except Exception:
        logger.debug("Could not roll back the refusal notes write", exc_info=True)


def record_refusals(
    store: VectorMemoryStore, session_key: str, refused: Iterable[tuple[str, object, int]]
) -> None:
    """Add each ``(key, value, chars)`` the size bound refused for *session_key*.

    An entry equal to one already recorded is not added twice. The record keeps the
    newest :data:`MAX_REFUSAL_NOTES` entries.
    """
    fresh = [
        {"session": session_key, "key": key, "value": value, "chars": chars}
        for key, value, chars in refused
    ]
    if not fresh:
        return
    try:
        with store._db_lock:
            entries = _read(store)
            for entry in fresh:
                if entry not in entries:
                    entries.append(entry)
            _write(store, entries[-MAX_REFUSAL_NOTES:])
    except Exception:
        _rollback(store)
        logger.warning(
            "Could not record the refused semantic values for %s", session_key, exc_info=True
        )


def pending_refusals(store: VectorMemoryStore, session_key: str) -> tuple[dict, ...]:
    """The refused values recorded for *session_key*, oldest first."""
    try:
        entries = _read(store)
    except Exception:
        logger.debug("Could not read the refused semantic values", exc_info=True)
        return ()
    return tuple(entry for entry in entries if entry["session"] == session_key)


def render_refusal_notice(entries: tuple[dict, ...], limit: int) -> str:
    """The prompt section listing *entries*, or ``""`` when there are none."""
    if not entries:
        return ""
    listed = [
        {"key": entry["key"], "chars": entry["chars"], "value": entry["value"]} for entry in entries
    ]
    return (
        f"\n\n{REFUSAL_NOTICE_HEADING}\n"
        f"An earlier pass over this conversation proposed these values. Each was over "
        f"the {limit}-character bound, so it was refused whole and is NOT stored. If a "
        f'fact is still true and worth keeping, propose it again in "semantic" as '
        f"separate keys of at most {limit} characters each, one fact per key; otherwise "
        f"leave it out. This list is shown once.\n"
        + json.dumps(listed, indent=1, ensure_ascii=False)
    )


def clear_reported(store: VectorMemoryStore, reported: tuple[dict, ...]) -> None:
    """Drop every entry equal to one in *reported*, the entries a prompt showed.

    A value the pass refused again unchanged is equal to the entry it showed, so it is
    dropped too: the model has already been told about it once.
    """
    if not reported:
        return
    try:
        with store._db_lock:
            entries = _read(store)
            kept = [entry for entry in entries if entry not in reported]
            if len(kept) != len(entries):
                _write(store, kept)
    except Exception:
        _rollback(store)
        logger.warning("Could not clear the reported refused semantic values", exc_info=True)
