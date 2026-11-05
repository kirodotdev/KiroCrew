"""Keeps the resident semantic scoring set current across a write, in place.

A write to one fact swaps that one row in the set and in what is derived from it,
and installs the result under the new scoring generation. A recall after the patch
ranks exactly what a recall after a full rebuild ranks: the patched set holds the
same rows (in another order; the ranking breaks ties by key, not by row order), and
every derived piece is a function of one row, changed for that row alone.

The patch gives up, and the set is dropped for the next recall to rebuild, whenever
it cannot be sure it is current: no resident set, a set from an older generation,
another process has committed since the set was read (``PRAGMA data_version``), the
set would pass its byte budget, the term index vocabulary has outgrown its bound,
or anything in the patch raises. Dropping is always safe.

A patch is applied when the write is made, before its transaction ends (a
consolidation batch commits once, at its end). The row is read back on the shared
connection, which sees the uncommitted write. A transaction that rolls back leaves
no trace in the set, so a key patched inside an open transaction is remembered in
``store._semantic_scoring_pending`` and checked against the table by
:func:`reconcile_pending` on the next lookup.

Called with the store's ``_db_lock`` held.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from typing import TYPE_CHECKING

from kiro_crew.vector_memory_runtime import semantic as _semantic

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger("kiro_crew.vector_memory")

_Vectors = tuple[list[int], list[bytes], list[float]]
_DROPPED = object()


def patch_semantic_scoring(store: VectorMemoryStore, key: str) -> None:
    """Move the scoring generation and bring the resident set up to date for *key*."""
    with store._db_lock:
        resident = store._semantic_scoring
        previous = store._semantic_scoring_generation
        generation = store._semantic_scoring_generation = previous + 1
        store._semantic_scoring = None
        if resident is None or resident.generation != previous:
            store._semantic_scoring_pending.clear()
            return
        version = store._sqlite_data_version()
        if version is None or version != resident.data_version:
            store._semantic_scoring_pending.clear()
            return
        try:
            current = _reconciled(store, resident, version, generation)
            if current is not None:
                current = _patched(store, current, key, version, generation)
        except Exception:  # defensive: a failed patch must only cost a rebuild
            logger.debug(
                "semantic scoring patch failed for %r; dropping the set", key, exc_info=True
            )
            current = None
        store._semantic_scoring = current
        if current is None:
            store._semantic_scoring_pending.clear()
        elif store.db.in_transaction:
            store._semantic_scoring_pending.add(key)


def reconcile_pending(
    store: VectorMemoryStore, resident: _semantic._SemanticScoringSet
) -> _semantic._SemanticScoringSet | None:
    """*resident* with every key patched in an unfinished transaction checked; ``None`` drops it."""
    try:
        return _reconciled(store, resident, resident.data_version, resident.generation)
    except Exception:  # defensive: a failed check must only cost a rebuild
        logger.debug("semantic scoring reconcile failed; dropping the set", exc_info=True)
        store._semantic_scoring_pending.clear()
        return None


def _reconciled(
    store: VectorMemoryStore,
    resident: _semantic._SemanticScoringSet,
    version: int,
    generation: int,
) -> _semantic._SemanticScoringSet | None:
    pending = store._semantic_scoring_pending
    if not pending:
        return resident
    held = {row["key"]: row for row in resident.rows}
    current: _semantic._SemanticScoringSet | None = resident
    for key in tuple(pending):
        row = _read_row(store, key)
        if held.get(key) != row:
            assert current is not None
            current = _patched(store, current, key, version, generation)
            if current is None:
                break
    if current is None or not store.db.in_transaction:
        pending.clear()
    return current


def _read_row(store: VectorMemoryStore, key: str) -> dict[str, object] | None:
    fetched = store._fetch_all_locked(f"{_semantic._SCORING_SELECT} AND key = ?", (key,))
    return dict(fetched[0]) if fetched else None


def _patched(
    store: VectorMemoryStore,
    resident: _semantic._SemanticScoringSet,
    key: str,
    version: int,
    generation: int,
) -> _semantic._SemanticScoringSet | None:
    new_row = _read_row(store, key)
    rows = resident.rows
    pos = next((i for i, row in enumerate(rows) if row["key"] == key), None)
    old_row = None if pos is None else rows[pos]
    if old_row == new_row:
        # Nothing the set holds changed (a lesson, a repeated write): same rows,
        # same derived work, new generation.
        return _semantic._SemanticScoringSet(rows, generation, version, resident.derived)

    old = resident.derived
    used = old.get("used_bytes")
    limit = old.get("max_bytes")
    if used is None or limit is None:
        return None
    used += (_semantic.row_bytes(new_row) if new_row else 0) - (
        _semantic.row_bytes(old_row) if old_row else 0
    )
    if used > limit:
        return None

    kept = rows if pos is None else rows[:pos] + rows[pos + 1 :]
    new_rows = kept + ((new_row,) if new_row is not None else ())
    derived: dict = {"used_bytes": used, "max_bytes": limit}
    # Keyed on the text itself, so an entry for a text no row carries any more
    # is dead weight (bounded by the memo's byte cap) and never a wrong answer.
    if "tokens" in old:
        derived["tokens"] = old["tokens"]
        derived["token_bytes"] = old.get("token_bytes", 0)
    for name, value in tuple(old.items()):
        if isinstance(name, tuple) and name[0] == "vectors":
            derived[name] = _patched_vectors(value, name[1], pos, new_row, len(new_rows) - 1)
        elif name == "term_index":
            index = _patched_term_index(value, pos, new_row)
            if index is not _DROPPED:
                derived[name] = index
    return _semantic._SemanticScoringSet(new_rows, generation, version, derived)


def _patched_vectors(
    cached: _Vectors,
    width: int,
    pos: int | None,
    new_row: dict[str, object] | None,
    new_pos: int,
) -> _Vectors:
    """The row vectors of one width, minus the row at *pos* and plus *new_row* at *new_pos*."""
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    index, blobs, norms = cached
    cut = len(index) if pos is None else bisect_left(index, pos)
    held = pos is not None and cut < len(index) and index[cut] == pos
    skip = cut + 1 if held else cut
    new_index = index[:cut] + ([i - 1 for i in index[skip:]] if pos is not None else index[skip:])
    new_blobs = blobs[:cut] + blobs[skip:]
    new_norms = norms[:cut] + norms[skip:]
    blob = new_row.get("embedding") if new_row is not None else None
    if isinstance(blob, bytes) and len(blob) == width * 4:
        np = vm.np
        new_index.append(new_pos)
        new_blobs.append(blob)
        new_norms.append(
            float(np.linalg.norm(np.frombuffer(blob, dtype=np.float32).astype(np.float64)))
        )
    return new_index, new_blobs, new_norms


def _patched_term_index(cached, pos: int | None, new_row: dict[str, object] | None):
    """``(index, key texts)`` minus the row at *pos* and plus *new_row*, or ``_DROPPED``.

    A built-and-refused index (``None``) stays refused. The new row's key text is
    its bare key; a row that carries an identity label scores through the per-text
    memo instead, because the loop only trusts an indexed key text equal to the
    one it computes now.
    """
    if cached is None:
        return None
    index, key_texts = cached
    texts: list[str] = []
    added: list[str] = []
    if new_row is not None:
        key_text = str(new_row["key"]).replace("_", " ").replace(".", " ")
        added.append(key_text)
        texts = [key_text, str(new_row["value_json"]).lower()]
    patched = index.patched(pos, texts)
    if patched is None:
        return _DROPPED
    kept = key_texts if pos is None else key_texts[:pos] + key_texts[pos + 1 :]
    return patched, kept + tuple(added)
