"""The memory a consolidation pass reads into its prompt and writes back.

A pass shows the model a bounded ``## Current Semantic Memory`` table
(:func:`_bounded_semantic_table`) and withholds deletes of keys the table did not show
(:func:`_withhold_unseen_deletes`). A ``lesson.*`` key the model put in the ``semantic``
array is moved to the ``lessons`` array first (:func:`_reroute_lesson_semantic_keys`), so
it gets the lessons path's dedup and checks instead of a plain semantic write. It then
writes what the model extracted:
lessons through :func:`_save_lessons`, which drops a lesson with a malformed scope and
forwards its authored tier, and semantic and episodic rows through
:func:`_write_structured_memory`, which refuses to delete a standing lesson
(:func:`_lesson_delete_decision`) and whose inline embeddings :class:`_EmbedBudget` bounds
per pass. That writer, and the pass's member-store branch before ``apply_consolidation``,
refuse whole a semantic value over the consolidation size bound, after kind routing
(:func:`_refuse_oversized_semantic`); a lesson is not held to it.
:func:`_is_plausible_memory_file` refuses placeholder text before it replaces a V1
memory file, and :func:`_session_facets` gives every row the session's carve axes. Each
writer publishes inside ``HistoryConsolidator._publication_hold_checked``, which reads
``memory.persistence_enabled`` and ``memory.writes_enabled`` through
``_persistence_disabled`` before the run's first commit: a switch turned off before that
commit refuses the run before it writes anything, and a run that has committed once
finishes its remaining writes.

Each function here whose first parameter is ``self`` is a ``HistoryConsolidator`` method:
the class binds it under the same name (``history_consolidation.py``), so it reads the
consolidator's own fields and calls its siblings through ``self``.

Instance fields read: ``_lesson_store``, ``_vector_store``.

Instance fields written: none.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports only ``history_consolidation_span``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.history_consolidation_span import _RunCommitState
from kiro_crew.lesson_validation import (
    LESSON_APPLIES_ON_TOPIC,
    authored_lesson_applies,
    extracted_lesson_applies,
)
from kiro_crew.project_scope import scope_is_admissible
from kiro_crew.vector_memory_constants import (
    _MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS,
    _MAX_EPISODIC_PER_CONSOLIDATION,
    _MAX_LESSONS_PER_CONSOLIDATION,
    _MAX_SEMANTIC_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from kiro_crew.history_consolidation import HistoryConsolidator
    from kiro_crew.learn import LessonStore
    from kiro_crew.memory_schema import MemoryFacets
    from kiro_crew.vector_memory import VectorMemoryStore


#: Wall-clock ceiling on the memory writes of ONE consolidation pass that embed
#: inline. A pass writes up to ``_MAX_SEMANTIC_PER_CONSOLIDATION`` +
#: ``_MAX_EPISODIC_PER_CONSOLIDATION`` rows and each one embeds its text through a
#: blocking inference call, so a degraded embedder makes the pass cost N times one
#: call's latency — all of it on an embed-pool worker, which is shared with every
#: other embed consumer. Past the ceiling the rest of the pass is written with its
#: embedding deferred, so it stops queueing inference it has measured to be slow.
#: Deferred rows are filled in by the standing repair sweep
#: (``backfill_missing_embeddings``), which is what makes deferral lossless.
_EMBED_BUDGET_SECS_PER_PASS = 60.0


#: The one line under a bounded ``## Current Semantic Memory`` table. The prompt
#: tells the model to update or delete the keys it can see, so a table that lost
#: rows silently would read as "those facts do not exist" and invite a deletion
#: of nothing or a near-duplicate of a dropped key. Same vocabulary as the chat
#: path's startup omission notice, so a reader learns one shape.
_SEMANTIC_OMISSION_NOTICE = (
    "[Context budget: omitted {count} of {total} semantic rows above the "
    "{limit}-character consolidation budget; the least recently updated rows were "
    "left out. The table above is PARTIAL: a key you do not see may still exist, "
    "so update or delete only keys listed above and treat a missing key as "
    "unknown, not absent.]"
)


class _BoundedTable(NamedTuple):
    """A rendered semantic table, how many rows it left out, and the keys it shows."""

    text: str
    omitted: int
    visible_keys: frozenset[str]


#: Rows per ``get_all_semantic`` call when a consolidation pass snapshots the table.
_SEMANTIC_SNAPSHOT_PAGE = 500


def _semantic_snapshot_in_pages(
    vector_store: "VectorMemoryStore", page: int = _SEMANTIC_SNAPSHOT_PAGE
) -> list[dict]:
    """The active semantic table in key order, fetched one bounded page at a time.

    A single unbounded ``get_all_semantic()`` holds the store's DB lock for the
    whole table, so every memory read and write behind it waits out the copy.
    Keyset pages (``key > cursor``) cost each call its own rows and
    release the lock between calls. The result is the same rows in the same order
    as the single read, short of rows written between two pages, which the
    single read could not promise either once the model answers minutes later.

    A page whose rows do not move the cursor ends the walk, so a store that
    ignores ``after_key`` returns its rows once rather than looping.
    """
    rows: list[dict] = []
    # ``""`` rather than no cursor, so the FIRST page takes the key-index plan too
    # (``key > ?`` is what asks for it). A semantic key is never empty: every writer
    # validates it, so ``key > ''`` leaves no row out.
    cursor = ""
    while True:
        batch = vector_store.get_all_semantic(limit=page, after_key=cursor)
        fresh = [r for r in batch if str(r.get("key", "")) > cursor]
        rows.extend(fresh)
        if len(batch) < page or not fresh:
            return rows
        cursor = str(fresh[-1].get("key", ""))


def _recency(updated_at: object) -> tuple[int, float]:
    """Rank an ``updated_at`` for the newest-first cut.

    Stamps are parsed, not compared as text: the store writes ISO 8601 with an
    offset, while imported or older rows can carry a naive stamp, a space
    separator or an epoch, and as text those shapes rank by their separator
    before their instant. A stamp that parses ranks by instant; one that does
    not ranks after every one that does.
    """
    if isinstance(updated_at, (int, float)) and math.isfinite(updated_at):
        return 1, float(updated_at)
    if not isinstance(updated_at, str):
        return 0, 0.0
    text = updated_at.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            return 1, float(text)
        except ValueError:
            return 0, 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return 1, parsed.timestamp()


def _bounded_semantic_table(rows: list[dict], entries: list[dict], cap: int) -> _BoundedTable:
    """Render ``entries`` as the prompt's semantic table within ``cap`` characters.

    ``rows`` are the store rows (in the store's key order) that ``entries`` were
    rendered from, one to one. A table that fits renders whole and byte-identical
    to the uncapped form. Over the cap, the most recently updated rows are kept
    -- the order the chat path's ``semantic_cap`` reads without a query -- and
    rendered in the same key order, so the block keeps its shape and only loses
    its oldest rows. Returns the block, the number of rows left out, and the keys
    the block shows, which is what the writers may update or delete.

    The fit is found by bisection on the row count with the real renderer rather
    than by an estimate per row, so the bound is exact for whatever ``indent``
    and escaping produce, at the cost of O(log n) serialisations. A row is kept
    whole or not at all: when even the newest row alone is over the cap the
    table is ``[]`` and every row counts as omitted, because a value cut short
    would read as the fact itself.
    """
    whole = json.dumps(entries, indent=1) if entries else "[]"
    if len(whole) <= cap:
        return _BoundedTable(whole, 0, frozenset(str(r.get("key", "")) for r in rows))
    # Newest first; key as the tiebreak so rows written in the same second keep
    # one order across runs (a stable sort on top of the key order given).
    by_recency = sorted(range(len(rows)), key=lambda i: str(rows[i].get("key", "")))
    by_recency.sort(key=lambda i: _recency(rows[i].get("updated_at")), reverse=True)

    def _render(count: int) -> str:
        kept = sorted(by_recency[:count])
        return json.dumps([entries[i] for i in kept], indent=1) if kept else "[]"

    fits, overflows = 0, len(rows)
    rendered = "[]"
    while overflows - fits > 1:
        middle = (fits + overflows) // 2
        candidate = _render(middle)
        if len(candidate) <= cap:
            fits, rendered = middle, candidate
        else:
            overflows = middle
    visible = frozenset(str(rows[i].get("key", "")) for i in by_recency[:fits])
    return _BoundedTable(rendered, len(rows) - fits, visible)


def _withhold_unseen_deletes(
    result: dict, visible_keys: frozenset[str] | None, logger: logging.Logger
) -> tuple[dict, int]:
    """Drop every ``delete`` naming a key the bounded semantic table never showed.

    Keys are guessable (``user.work_email``, ``project.*``), so a model reading a
    partial table can name a row it never read, and the omission notice alone
    does not stop it. A delete has nothing behind it to arbitrate, so it is
    withheld here, on the model's answer, before either write path reads it --
    ``_write_structured_memory`` and the member store's ``apply_consolidation``
    share this one fence so they cannot drift. An update is NOT withheld: the
    new value's authority is the transcript, not the rendered table, and the
    store arbitrates the old one (``_write_semantic`` skips a consolidation
    overwrite of a user-stated row; the member store turns a conflicting update
    into an owner proposal). Refusing it would also refuse a user's genuine
    correction for a key the cap cut from the table, and the span is marked
    consolidated either way, so that correction would be gone for good. ``None``
    means no bounded table was rendered and nothing is withheld.

    Returns the result (a shallow copy when anything was dropped) and how many
    distinct keys were withheld; each is logged once, however often it is named.
    """
    items = result.get("semantic")
    if visible_keys is None or not isinstance(items, list):
        return result, 0
    kept: list = []
    withheld: set[str] = set()
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("delete")
            and isinstance(item.get("key"), str)
            and item["key"] not in visible_keys
        ):
            if item["key"] not in withheld:
                withheld.add(item["key"])
                logger.warning(
                    "Semantic consolidation refused delete of %r: key not in the rendered table"
                    " (omitted above the prompt budget), so the model never saw its value",
                    item["key"],
                )
            continue
        kept.append(item)
    if not withheld:
        return result, 0
    return {**result, "semantic": kept}, len(withheld)


def _v1_consolidation_confidence(item: dict) -> float | None:
    """The confidence :func:`_write_structured_memory` writes *item* at, or ``None`` if it skips.

    That writer skips a confidence ``float()`` cannot read, one that is not finite and one
    outside ``0..1``. An integer too large for a float raises ``OverflowError``, which the
    writer does not catch. :func:`_refuse_oversized_semantic` reads the same answer for an
    item this writer reads, so the two cannot disagree on skipping an item for its
    confidence.
    """
    try:
        confidence = float(item.get("confidence", 0.5))
    except (ValueError, TypeError):
        return None
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        return None
    return confidence


def _v1_consolidation_metadata(item: dict, vector_store: VectorMemoryStore) -> Any:
    """The metadata :func:`_write_structured_memory` hands ``set_semantic`` for *item*.

    Only a member store takes model metadata on this path, so this is ``None`` for a V1
    store and for an item that carries none. ``set_semantic`` validates it with
    ``memory_record_metadata.normalize_metadata`` after ``validate_semantic`` and refuses
    the item as ``conflict_skip`` when that raises. :func:`_refuse_oversized_semantic`
    passes the same answer to that same function, so the two agree on refusing an item
    for its metadata; that call is the screen's copy of the one ``set_semantic`` makes,
    kept in step by hand.
    """
    if getattr(vector_store, "algorithm_version", "v1") != "v2":
        return None
    return item.get("metadata")


class _OversizedItem(NamedTuple):
    """A semantic value the consolidation size bound refused: key, value, length, place.

    ``place`` is the value's index in the writers' window, where the V1 writer gives it
    its turn (:func:`_write_structured_memory`).
    """

    key: str
    value: object
    chars: int
    place: int


def _refuse_oversized_semantic(
    result: dict,
    logger: logging.Logger,
    *,
    vector_store: VectorMemoryStore,
    source: str,
    member: bool,
) -> tuple[dict, tuple[_OversizedItem, ...]]:
    """Refuse every semantic value in the writers' window that is over the size bound.

    The bound is :data:`_MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS` characters, inclusive,
    of the value as the per-turn facts block renders it (``get_semantic_context``, on
    both store versions): ``len()`` of a string, of the JSON text of a dict or list with
    every non-ASCII character as written (``ensure_ascii=False``, as that block renders
    it), and of ``str()`` of any other value. So a Cyrillic letter inside a list counts
    as the one character the block shows. It is measured apart from the byte ceiling in
    ``validate_semantic``. An item over it is refused whole and
    never split or shortened: no split point keeps each piece true on its own key, and a
    cut value would read as the fact itself. The prompt states the bound so the model
    draws that boundary itself, as separate keys.

    An item is refused for size only when every check its writer applies to the item
    alone would let it through, so the size code takes the place of none of them: not
    ``validate_semantic``'s refusal, not the writer's confidence or metadata check, not
    the writer's skip rules, and not kind routing. Each item over the bound first meets,
    in its writer's order, that writer's confidence reader, then *vector_store*'s own
    ``validate_semantic`` with the writers' *source* and that confidence, then the
    writer's metadata check.
    *member* says the member store's ``apply_consolidation`` reads the result, through
    ``vector_memory_member._member_consolidation_confidence`` and
    ``vector_memory_member._member_consolidation_metadata``, rather than
    ``_write_structured_memory``, through :func:`_v1_consolidation_confidence` and
    :func:`_v1_consolidation_metadata`, whose answer ``set_semantic`` validates with
    ``memory_record_metadata.normalize_metadata``. Each writer calls those functions
    itself, so the screen and its writer cannot disagree on skipping an item for its
    confidence, on the confidence it is validated at, on the member store's metadata
    check or on which metadata the V1 writer hands over. What is copied
    rather than shared must be kept in step by hand: the V1 check itself,
    ``normalize_metadata``, which ``set_semantic`` calls rather than a function the
    screen shares; the screen's skip rules (an item that is not an object, a key that is
    not a string, a delete, an item with no value), which copy the writers' own tests;
    and its ``lesson.*`` rule, which copies the reroute's. A test drives an item under
    each skip rule, its value over the bound where it has one, through both writers in a
    whole pass. In a pass the reroute has already moved or dropped every ``lesson.*``
    create, so a ``lesson.*`` item reaches the screen only as a delete. An item
    those checks refuse or skip (a malformed or unallowed key, a low confidence, a value
    over the byte ceiling or one the injection check flags, metadata that is not an
    object or holds an invalid field) is left untouched, and its writer refuses or skips
    it under its own code and counts it as it would without the bound: the member
    receipt's ``metadata_rejected``, the V1 ``conflict_skip`` line. So no key that fails
    ``_validate_key`` is ever refused for size. Every check here only reads, so the
    screen writes no row and counts nothing for an item it leaves.

    The writers' checks that read stored rows are not consulted, so the size code does
    take their place, and that is known. An over-bound overwrite of a ``user_explicit``
    row, which the V1 store refuses as ``conflict_skip``, is refused as
    ``consolidation_value_size`` instead: no fact is stored either way, but the report
    differs (the log line, and the ``memory_events`` row ``conflict_skip`` writes), and
    so does the run's commit: that write commits nothing, and the refused value's turn
    commits the run (the exception stated below). A
    value the store would save as a review proposal rather than write (a member-store
    update of an existing key, a V1 write to a deleted key) is refused for size too, so
    that proposal, a ``memory_revisions`` row the owner would review, is never made: no
    proposal asks the owner to approve a value over the bound. And a refused value is
    never written, so a later item in the same pass that names its key (or, on a member
    store, the same fact identity) meets the stored row without it: it can be written
    where, after the refused write, the store would have refused it as ``conflict_skip``
    or saved it as a proposal. Nothing else that write would do in the store happens
    either: no ``create`` or ``update`` row for the value is logged in ``memory_events``,
    the value is not embedded, so it spends none of the V1 pass's inline embedding
    budget, and the store retires no episode that asserted the value it would have
    replaced. The size code reads no stored row, so a value over the bound
    is refused even when the store already holds the same value.

    A refused value still takes its write's turn in the pass. The V1 writer reaches it
    where it would write it, takes the publication hold that write takes, and marks the
    run committed as a write the store accepts does, writing nothing. The member store
    writes the whole answer in one ``apply_consolidation`` call under one hold, which
    commits the run whatever the answer holds. So on both paths the pass takes the
    holds it takes without the bound, and whether a later write passes the hold, and
    whether a restricted or locked transcript later in the pass refuses the pass or
    marks its span consolidated, are decided as they are without the bound whenever the
    store would accept the refused write. Where a stored row would make the V1 store
    refuse that write with a reject code (a ``conflict_skip``), the write would have
    committed nothing, and the refused value's turn still commits the run.

    Unknown metadata field names are judged as each writer judges them. The member
    store's ``apply_consolidation`` drops them and writes the fact, so on that path an
    over-bound item whose metadata holds only those is refused for size, and its
    receipt counts no ``metadata_fields_dropped`` for it. The V1 writer hands a member
    store's ``set_semantic`` the metadata whole, and ``normalize_metadata`` refuses an
    unknown name, so there the item keeps ``conflict_skip``. A V1 store is handed no
    metadata, so on it any metadata is no refusal and the item is refused for size.

    Each write path calls this at the same point, on the answer kind routing returned
    (``memory_kind_routing.route_consolidation_by_kind``, which runs right after the
    unseen-delete fence) and before the path's first write: :func:`_write_structured_memory`,
    and ``_consolidate``'s member-store branch before ``apply_consolidation``. Both run
    after the write-boundary recheck, so a discarded result refuses and logs nothing.
    Routing therefore reads the model's answer as it would without the bound, in every
    ``memory.kind_routing`` mode: every item it routes keeps its decision, counted and,
    under ``shadow``, sampled, and a ``state`` item ``enforce`` keeps out is refused as
    ``work_state``, never for size, because routing has already taken it out of the list
    this reads. So the size code never takes the place of routing's refusal either.

    Only the window the writer reads is screened: the first
    ``_MAX_SEMANTIC_PER_CONSOLIDATION`` items of that list, which the fence and routing
    have already shaped. An item past the window is never written, so it is neither
    measured nor logged. A refused item keeps its place as ``None``, which the member
    store skips like any item that is not an object; the V1 writer gives the refused
    value its turn at that place, found by its ``place`` rather than by reading the
    ``None``, so a refusal never moves a later item into the window.

    Only consolidation is bound: owner edits, imports and the dashboard never reach it.
    A delete, a ``lesson.*`` key (the reroute owns those)
    and an item with no value pass through untouched for the writers to judge.

    Returns the result (a shallow copy when anything was refused) and every refused
    value in window order, for its write path to audit in ``memory_events``
    (:func:`_audit_oversized_semantic`). Each is logged once, naming its key, the reject
    code, the length and the bound; the value itself is never logged. The same key may
    be refused one value and written another in one pass, so a line reports a refused
    value, not a refused key.
    """
    items = result.get("semantic")
    if not isinstance(items, list):
        return result, ()
    from kiro_crew import memory_record_metadata, vector_memory_member

    read_confidence = (
        vector_memory_member._member_consolidation_confidence
        if member
        else _v1_consolidation_confidence
    )
    screened = list(items)
    refused: list[_OversizedItem] = []
    for index, item in enumerate(items[:_MAX_SEMANTIC_PER_CONSOLIDATION]):
        key = item.get("key") if isinstance(item, dict) else None
        if (
            not isinstance(key, str)
            or key.startswith("lesson.")
            or item.get("delete")
            or item.get("value") is None
        ):
            continue
        value = item["value"]
        # The facts block's own rendering of a value (``get_semantic_context``), so the
        # bound measures the value as that block shows it; a test pins the two.
        chars = len(
            json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        )
        if chars <= _MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS:
            continue
        # The writer's own reader, so an item it skips is never refused for size. The V1
        # writer does not catch OverflowError (an integer too large for a float), so that
        # item is left for it to raise on, like one it skips; the member reader clamps it.
        try:
            confidence = read_confidence(item)
        except OverflowError:
            continue
        if confidence is None:
            continue
        if vector_store.validate_semantic(key, value, confidence, source) is not None:
            continue
        # The writer's own metadata check, after validate_semantic as the writer runs it,
        # so an item it skips or refuses for its metadata keeps that answer.
        try:
            if member:
                vector_memory_member._member_consolidation_metadata(item)
            elif (metadata := _v1_consolidation_metadata(item, vector_store)) is not None:
                memory_record_metadata.normalize_metadata(metadata)
        except ValueError:
            continue
        from kiro_crew.vector_memory import SemanticRejectCode

        screened[index] = None
        refused.append(_OversizedItem(key, value, chars, index))
        logger.warning(
            "Semantic consolidation refused a value for %r: %s: %d characters, over the"
            " %d-character consolidation bound",
            key,
            SemanticRejectCode.CONSOLIDATION_VALUE_SIZE.value,
            chars,
            _MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS,
        )
    if not refused:
        return result, ()
    logger.info(
        "Consolidation refused %d semantic value(s) over the %d-character bound",
        len(refused),
        _MAX_CONSOLIDATION_SEMANTIC_VALUE_CHARS,
    )
    return {**result, "semantic": screened}, tuple(refused)


def _audit_oversized_semantic(
    vector_store: VectorMemoryStore,
    oversized: tuple[_OversizedItem, ...],
    key: str,
) -> None:
    """Write one ``memory_events`` row for each value :func:`_refuse_oversized_semantic` refused.

    It takes no publication hold of its own: each caller writes the rows inside a hold
    the pass takes without the bound too, so the rows land exactly when that hold lets
    the pass write. :func:`_write_structured_memory` writes a refused value's row in
    that value's own turn, under the hold its write would take, on the pool thread it
    runs on; ``_consolidate``'s member-store branch writes them under the hold
    ``apply_consolidation`` runs under, on the background pool, before that call opens
    its transaction. So writes turned off before the run's first commit, or a lost
    lease, write no row, and writes turned off after it (an earlier write, or on the V1
    path the history entry ``append_history`` appends before the writer runs) do not
    stop one, as they do not stop that write without the bound.
    """
    from kiro_crew.vector_memory import SemanticRejectCode

    for oversized_item in oversized:
        vector_store.log_reject_event(
            SemanticRejectCode.CONSOLIDATION_VALUE_SIZE,
            oversized_item.key,
            oversized_item.value,
            f"consolidation:{key}",
            value_json=json.dumps(oversized_item.value, ensure_ascii=False),
        )


#: The keys a rerouted ``lesson.*`` semantic item may carry into a lesson, mapped
#: from the semantic shape to the lesson shape. A lesson's rule is the semantic
#: ``value``; ``category``, ``negative``, ``applies`` and ``repo_scope`` are
#: already the lesson field names and copy across unchanged.
_LESSON_PASSTHROUGH_FIELDS = ("category", "negative", "applies", "repo_scope")


def _reroute_lesson_semantic_keys(result: dict, logger: logging.Logger) -> dict:
    """Move ``semantic`` items whose key is ``lesson.*`` into the ``lessons`` array.

    The prompt asks the model for ``pref.*`` / ``project.*`` / ``user.*`` keys in
    ``semantic`` and for lessons in ``lessons``, but that split is not enforced.
    A ``lesson.*`` key left in ``semantic`` is written straight through
    ``set_semantic`` (V1) or ``_write_semantic`` (V2), so it skips the exact-rule
    dedup, the volatile-fact screen and the extraction-enabled gate that
    ``write_lesson`` / the member store's lesson branch apply -- which is how the
    same rule ends up stored several times under different model-chosen keys.
    Rerouting it to ``lessons`` before any write hands it the same
    checks as the model's other lessons, on BOTH paths, because this runs once on
    the shared ``result`` before it branches to ``_write_structured_memory`` /
    ``_save_lessons`` (V1) and ``apply_consolidation`` (V2).

    Only a CREATE is rerouted. A ``delete`` of a ``lesson.*`` key stays in
    ``semantic``: the delete site already tier-protects standing lessons
    (``_lesson_delete_decision``), and the lessons array has no delete verb.
    The lesson's rule is the item's ``value``, so an item whose value is not a
    non-empty string is not a lesson and is DROPPED (restating the issue's note
    that a value like ``true`` is not a lesson) rather than left to be written as
    a plain semantic row -- which would reintroduce exactly the un-deduped
    ``lesson.*`` semantic row this reroute exists to remove.

    Returns the result -- a shallow copy with fresh ``semantic`` / ``lessons``
    lists when anything moved, otherwise the input unchanged. Reroutes and drops
    are logged once for the pass; no caller needs the counts.
    """
    items = result.get("semantic")
    if not isinstance(items, list):
        return result
    kept_semantic: list = []
    moved_lessons: list = []
    dropped = 0
    for item in items:
        key = item.get("key") if isinstance(item, dict) else None
        if not (isinstance(key, str) and key.startswith("lesson.")) or (
            isinstance(item, dict) and item.get("delete")
        ):
            kept_semantic.append(item)
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            # A lesson's rule is text. A non-string (``true``, a number, an
            # object) or an empty value is not a lesson; drop it rather than
            # let it fall through to a plain semantic write.
            dropped += 1
            continue
        lesson: dict = {"rule": value}
        for field in _LESSON_PASSTHROUGH_FIELDS:
            if field in item:
                lesson[field] = item[field]
        moved_lessons.append(lesson)
    if not moved_lessons and not dropped:
        return result
    existing = result.get("lessons")
    lessons = list(existing) if isinstance(existing, list) else []
    lessons.extend(moved_lessons)
    if moved_lessons:
        logger.info(
            "Consolidation rerouted %d lesson.* semantic item(s) to the lessons path",
            len(moved_lessons),
        )
    if dropped:
        logger.warning(
            "Consolidation dropped %d lesson.* semantic item(s) with a non-text value",
            dropped,
        )
    return {**result, "semantic": kept_semantic, "lessons": lessons}


class _EmbedBudget:
    """One consolidation pass's embed-time ceiling, and the latch it arms.

    The ceiling is measured over the store WRITES, not over the embed calls
    themselves: the consolidation layer has no seam onto an individual embed, and
    write time is the quantity that actually has to be bounded. Inference is the
    only unbounded part of a write — the rest is local SQLite work — so a slow
    embedder is what normally spends this budget, though lock contention or a
    stalled disk can spend it too. The remedy for either is the same, and it is
    self-healing: the repair sweep embeds what this pass deferred.

    Once tripped the latch stays tripped for the rest of the pass — that is the
    whole point. Without it, an embedder that is slow for the first row is slow for
    every row, and the pass pays that latency once per item before finishing with
    exactly the same rows it would have written anyway (a failed embed already
    stores a NULL vector for the repair sweep to fill).
    """

    __slots__ = ("_budget", "_logger", "_spent", "tripped")

    def __init__(self, budget_secs: float, logger: logging.Logger) -> None:
        self._budget = budget_secs
        self._logger = logger
        self._spent = 0.0
        self.tripped = False

    @contextlib.contextmanager
    def measured(self):
        """Time one store write and charge it to the pass, arming the latch once."""
        started = _time.monotonic()
        try:
            yield
        finally:
            self._spent += _time.monotonic() - started
            if not self.tripped and self._spent >= self._budget:
                self.tripped = True
                # Once per pass, not once per row: a degraded embedder would
                # otherwise repeat this line for every remaining item.
                self._logger.warning(
                    "Consolidation spent %.1fs on memory writes (budget %.1fs); "
                    "embedding is deferred to the repair sweep for the rest of this pass",
                    self._spent,
                    self._budget,
                )


#: Default for the two write helpers' store arguments, meaning "argument not
#: supplied — use the global handle off ``self``". It cannot be ``None``, because
#: ``ContextBuilder.ensure_store`` may answer ``None`` for an unavailable legacy
#: V1 silo (private V2 raises), and that answer must skip the tier rather than
#: inherit the global store:
#: inheriting files one crew's rows into the operator's own memory, which is the
#: misfiling the resolved handles exist to prevent. Typed ``Any`` so each parameter
#: keeps its real annotation.


class _InheritGlobal:
    """Sentinel type for "argument omitted, inherit the consolidator's handle".

    A CLASS rather than a bare ``object()`` so the parameters it defaults can keep
    their real annotations. Typed ``Any``, the sentinel erased mypy's view of both
    store parameters — and ``_save_lessons`` is called positionally, so a swapped
    ``(vector_store, lesson_store)`` pair would have written one crew's lessons
    through the other's handle with nothing to catch it.
    """

    __slots__ = ()


_INHERIT_GLOBAL = _InheritGlobal()


class _LessonDeleteDecision(NamedTuple):
    """A consolidation lesson-delete decision plus the body it was read from.

    ``checked_value_json`` carries the exact ``value_json`` the tier was read
    from so an allowed delete can compare-and-delete against it; it is ``None``
    for a protected decision and for a non-lesson key.

    ``reason`` tells the three protected causes apart so they log and count
    separately, since they mean different things to an operator:

    * ``"allow"`` -- not protected; the delete proceeds.
    * ``"tier"`` -- a standing (``always``/unstated) or non-mapping lesson row:
      a real tier refusal.
    * ``"absent"`` -- no active row under a ``lesson.*`` key: nothing exists to
      protect, refused only to keep an unconditional delete out of the
      delete-plus-re-add window.
    * ``"unreadable"`` -- the row read raised: a store outage, not a tier
      decision, and worth a warning.
    """

    protected: bool
    checked_value_json: str | None
    reason: str


_PLACEHOLDER_BODIES = frozenset(
    {
        "unchanged",
        "no change",
        "no changes",
        "no change needed",
        "no changes needed",
        "no changes required",
        "no update",
        "no updates",
        "no update needed",
        "no updates needed",
        "nothing changed",
        "nothing to update",
        "nothing to change",
        "none",
        "n/a",
        "na",
        "empty",
        "same",
        "same as before",
        "as before",
        "see above",
        "content unchanged",
        "file unchanged",
    }
)


def _is_plausible_memory_file(content: str, header: str) -> bool:
    """Refuse placeholder text before it overwrites a complete memory file."""
    first_line, _, body = content.strip().partition("\n")
    if first_line.strip() != header:
        return False
    normalized = body.strip().lower().strip(" \t\"'`*_~.,!()[]")
    return normalized not in _PLACEHOLDER_BODIES


def _session_facets(meta: dict, key: str) -> "MemoryFacets":
    """The carve axes for everything this session's consolidation writes.

    The consolidator is the writer worth threading first: it is the one that
    produces most rows and it already holds every axis one line away. The others
    have no identity in scope and would have to invent one.

    ``crew`` comes from the session's ``agent`` metadata, which is the CREW alias
    (``cfg.agents`` key) rather than the kiro-cli agent template. Confusing the two
    is a named bug -- resolving a store from the template answers ``default`` for
    exactly the crew that configured otherwise -- so this reads ``meta["agent"]`` and
    never ``kiro_agent``. Note that is a DIFFERENT key from the one
    :func:`kiro_crew.context.store_of_session` reads (``meta["memory_store"]``): a crew
    alias and the store it binds to are separate facts. Each named member now owns a
    unique private store; recording the alias separately preserves its provenance.

    ``surface`` uses ``telemetry_channel_of``, whose output is a BOUNDED label and
    never the key itself, so the column cannot acquire one value per conversation.
    Not ``sel._infer_source``, which fails OPEN to ``"slack"`` for an unrecognised
    key and would file dashboard rows inside a slack carve.

    Never raises: a facet is an index projection, and no carve axis is worth
    failing a consolidation over.
    """
    from kiro_crew.memory_schema import MemoryFacets

    try:
        from kiro_crew.messaging.link import telemetry_channel_of

        surface = telemetry_channel_of(key)
    except Exception:
        surface = ""
    crew = meta.get("agent")
    return MemoryFacets(
        surface=surface,
        crew=crew if isinstance(crew, str) else "",
        session_key=key,
    )


def _gated_lesson_scope(self: HistoryConsolidator, item: dict) -> tuple[str | None, bool]:
    """The lesson's ``repo_scope`` to forward, plus whether to DROP the lesson.

    Returns ``(scope, False)`` for no scope (absent, ``None``, or a
    whitespace-only string -- the unchanged global path) or an admissible
    one, and ``(None, True)`` (with the reason logged) when a PRESENT scope
    is malformed. A malformed scope refuses the WHOLE lesson rather than
    stripping the scope: storing it globally would be the fail-open a
    scoped lesson must never take (``write_lesson`` draws the same line for
    inadmissible strings). The value is untrusted model output, so this
    seam also refuses the shapes the stores' own guards cannot see -- a
    non-string would slip past ``write_lesson``'s string-only admissibility
    check and be canonicalised to a GLOBAL write, and ``LessonStore.save``
    never checks admissibility at all.
    """
    raw = item.get("repo_scope")
    refusal: str | None = None
    if raw is None:
        return None, False
    elif not isinstance(raw, str):
        refusal = "scope_not_a_string"
    elif not raw.strip():
        return None, False
    elif not scope_is_admissible(raw):
        refusal = "scope_inadmissible"
    if refusal:
        # Only the closed-set reason code is interpolated, never the
        # untrusted value itself.
        self._logger.warning(
            "Dropping consolidation lesson with malformed repo_scope (%s)",
            refusal,
        )
        return None, True
    return raw, False


def _lesson_tier(self: HistoryConsolidator, item: dict) -> str | None:
    """The lesson's authored ``applies`` tier to forward, or ``None`` for unstated.

    One policy for every consolidation write path, so the member-store path
    in ``VectorMemoryStore.apply_consolidation`` and this one cannot drift:
    see ``extracted_lesson_applies``.
    """
    return extracted_lesson_applies(item.get("applies"), self._logger)


def _lesson_delete_decision(
    self: HistoryConsolidator, vector_store: "VectorMemoryStore", del_key: object
) -> "_LessonDeleteDecision":
    """Whether a consolidation delete of *del_key* must be refused, and the
    exact stored body the decision was read from.

    A model's guessed contradiction may retire an ``on_topic`` finding but
    never a standing rule the user taught -- the same invariant the
    ``/api/lessons`` contradiction sweep enforces before it supersedes a
    candidate. Only ``lesson.*`` keys carry a tier, so a non-lesson key is
    never protected here. The stored row is read authoritatively (the tier is
    write-once, so the persisted value is the author's), and anything that is
    not the ``on_topic`` tier -- ``always``, an unstated row, an unreadable or
    missing value -- is protected: the narrowest fail-safe, since demoting a
    real standing rule is the costlier mistake.

    ``checked_value_json`` is the ``value_json`` the tier was read from, so an
    allowed delete can COMPARE-AND-DELETE against it: ``_lesson_key`` keys on
    rule text plus scope alone, so a delete-plus-re-add is the documented way
    to change a tier and can put a standing rule under the same key between
    this read and the delete. Passing the checked body as ``expect_value_json``
    makes the delete a no-op when the row moved, closing that race without a
    lock this call site cannot hold. It is ``None`` for a protected decision
    (no delete follows) and for a non-lesson key (which the caller deletes
    unconditionally, its existing contract).
    """
    if not isinstance(del_key, str) or not del_key.startswith("lesson."):
        return _LessonDeleteDecision(protected=False, checked_value_json=None, reason="allow")
    try:
        row = vector_store.get_semantic(del_key)
    except Exception:
        # An unreadable row is protected, not silently deletable: a lookup
        # failure must never widen what a guess is allowed to retire. This is
        # a store outage, not a tier decision, so it carries its own reason.
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="unreadable")
    if not isinstance(row, dict):
        # No active row under this lesson key. This is NOT a safe no-op: the
        # allow path deletes unconditionally (no value to compare against),
        # and the absent state is the intermediate state of the documented
        # delete-plus-re-add re-tier -- a concurrent learn_add can recreate
        # the key as a standing rule inside the window between this read and
        # the delete, which the unconditional delete would then tombstone.
        # There is nothing legitimate for a guess to delete under an absent
        # lesson key anyway, so protect: refuse rather than race.
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="absent")
    raw = row.get("value_json")
    checked_value_json = raw if isinstance(raw, str) else None
    decoded: object = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            # A row whose body will not decode has no readable tier; treat it
            # as the protected (standing) class rather than guessing it is a
            # finding.
            return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="tier")
    # Only the mapping shape can carry a tier. A legacy string row, or any
    # value that does not decode to a mapping, has no author-stated tier and
    # is protected -- the same unstated->standing treatment readers give it.
    if not isinstance(decoded, dict):
        return _LessonDeleteDecision(protected=True, checked_value_json=None, reason="tier")
    protected = authored_lesson_applies(decoded.get("applies")) != LESSON_APPLIES_ON_TOPIC
    return _LessonDeleteDecision(
        protected=protected,
        checked_value_json=None if protected else checked_value_json,
        reason="tier" if protected else "allow",
    )


def _save_lessons(
    self: HistoryConsolidator,
    raw: object,
    vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    lesson_store: "LessonStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    *,
    facets: "MemoryFacets | None" = None,
    key: str = "",
    commit_state: _RunCommitState | None = None,
) -> None:
    """Save extracted lessons from consolidation result.

    Both stores are passed in rather than read off ``self`` so a crew's
    corrections land in its own silo. Omitting them keeps the historical
    behaviour (the global handles), which is what the workspace and default
    arms of the caller want.

    An explicitly passed ``None`` is NOT omission (:data:`_INHERIT_GLOBAL`):
    it means the silo has no store of that tier, so the tier is skipped. The
    caller reaches this method whenever EITHER store is live, so a silo whose
    vector store could not be stood up arrives here with
    ``vector_store=None`` and a real ``lesson_store`` — and inheriting the
    global vector store there would take the dedup-aware branch below and
    file the crew's corrections into the operator's own table, never touching
    the silo's ``lessons.jsonl`` at all.
    """
    if isinstance(vector_store, _InheritGlobal):
        vector_store = self._vector_store
    if isinstance(lesson_store, _InheritGlobal):
        lesson_store = self._lesson_store
    if not isinstance(raw, list):
        return

    # Cap like semantic/episodic: each write_lesson can perform up to 6
    # blocking embeds, so an uncapped LLM lessons array would occupy a
    # worker thread for minutes.
    max_lessons = _MAX_LESSONS_PER_CONSOLIDATION
    if len(raw) > max_lessons:
        self._logger.warning(
            "Consolidation returned %d lessons; capping to %d",
            len(raw),
            max_lessons,
        )
        raw = raw[:max_lessons]

    # Prefer vector store (dedup-aware) over JSONL
    if vector_store:
        count = 0
        for item in raw:
            if isinstance(item, dict) and item.get("rule"):
                scope, drop = self._gated_lesson_scope(item)
                if drop:
                    continue
                rule_generation = vector_store.space_generation
                rule_emb = vector_store.embed_lesson(item["rule"])
                with self._publication_hold_checked(key, commit_state) as publication:
                    ok = vector_store.write_lesson(
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        source="consolidation",
                        rule_emb=rule_emb,
                        rule_emb_generation=rule_generation,
                        rule_emb_resolved=True,
                        defer_backfills=True,
                        # Gated by _gated_lesson_scope above; write_lesson
                        # canonicalises and re-checks admissibility itself.
                        repo_scope=scope,
                        # Already normalized by _lesson_tier, so write_lesson's
                        # own raising check cannot fire on it.
                        applies=self._lesson_tier(item),
                        facets=facets,
                    )
                    if ok:
                        publication.mark_committed()
                if ok:
                    count += 1
        if count:
            self._logger.info("Extracted %d lesson(s) from chat (vector store)", count)
        return

    if not lesson_store:
        return
    from datetime import timezone as _tz

    from kiro_crew.learn import Lesson

    count = 0
    for item in raw:
        if isinstance(item, dict) and item.get("rule"):
            scope, drop = self._gated_lesson_scope(item)
            if drop:
                continue
            with self._publication_hold_checked(key, commit_state) as publication:
                outcome = lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=_tz.utc).isoformat(),
                        rule=item["rule"],
                        category=item.get("category", "knowledge"),
                        negative=item.get("negative"),
                        # Gated by _gated_lesson_scope above (LessonStore.save
                        # canonicalises but never checks admissibility itself).
                        repo_scope=scope,
                        # None is dropped by _serializable, so an unstated row
                        # is byte-identical to one written before the field.
                        applies=self._lesson_tier(item),
                    )
                )
                if outcome != "refused":
                    publication.mark_committed()
            if outcome != "refused":
                count += 1
    if count:
        self._logger.info("Extracted %d lesson(s) from chat", count)


def _write_structured_memory(
    self: HistoryConsolidator,
    result: dict,
    key: str,
    vector_store: "VectorMemoryStore | None | _InheritGlobal" = _INHERIT_GLOBAL,
    *,
    facets: "MemoryFacets | None" = None,
    snapshot: dict | None = None,
    visible_keys: frozenset[str] | None = None,
    messages: list[dict] | None = None,
    commit_state: _RunCommitState | None = None,
) -> None:
    """Write semantic + episodic entries from consolidation result.

    *vector_store* is the session's RESOLVED store. Omitting it keeps the
    global handle, which is what the workspace and default arms want; an
    explicit ``None`` means the silo has no vector store and the tier is
    skipped, the same distinction :meth:`_save_lessons` draws.

    *visible_keys* is the set of keys the prompt's bounded semantic table
    showed the model; a ``delete`` of a key outside it is withheld before the
    loop (``_withhold_unseen_deletes`` says why an update is not). ``None``
    means the caller rendered no bounded table and nothing is withheld.
    """
    if isinstance(vector_store, _InheritGlobal):
        vector_store = self._vector_store
    if not vector_store:
        return
    source = f"consolidation:{key}"
    private_policy = getattr(vector_store, "algorithm_version", "v1") == "v2"
    # Shared by both tiers below: each embeds inline, so both charge the same
    # pass and either can arm the latch for the other.
    budget = _EmbedBudget(_EMBED_BUDGET_SECS_PER_PASS, self._logger)

    from kiro_crew.memory_kind_routing import record_kind_routing, route_consolidation_by_kind

    result, withheld = _withhold_unseen_deletes(result, visible_keys, self._logger)
    routing = route_consolidation_by_kind(result, self._logger)
    result = routing.result
    # The consolidation size bound, on the answer routing returned, so routing reads and
    # logs every item as it would without the bound (_refuse_oversized_semantic).
    result, oversized = _refuse_oversized_semantic(
        result, self._logger, vector_store=vector_store, source=source, member=False
    )
    refused_at = {refusal.place: refusal for refusal in oversized}

    # Semantic entries
    semantic_items = result.get("semantic")
    if isinstance(semantic_items, list):
        written = 0
        deleted = 0
        skipped = 0
        refused = 0
        protected = 0
        absent = 0
        unreadable = 0
        stale_skipped = 0
        for index, item in enumerate(semantic_items[:_MAX_SEMANTIC_PER_CONSOLIDATION]):
            if index in refused_at:
                # The value the size bound refused takes its write's turn: the hold that
                # write takes, and the commit it makes when the store accepts it, so the
                # rest of the pass meets the run as it would without the bound.
                with self._publication_hold_checked(key, commit_state) as publication:
                    _audit_oversized_semantic(vector_store, (refused_at[index],), key)
                    publication.mark_committed()
                continue
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            # Handle deletion of stale keys
            if item.get("delete"):
                # A model's guess may retire an on_topic finding, never a
                # standing rule the user taught. The /api/lessons
                # contradiction sweep enforces this; consolidation is the
                # sibling model-judged deletion path, so it enforces the same
                # invariant here rather than in delete_semantic -- an explicit
                # forget must still be able to remove a standing rule, and
                # only this call site knows the delete is an inference.
                decision = self._lesson_delete_decision(vector_store, item["key"])
                if decision.protected:
                    # Three protected causes mean different things to an
                    # operator, so they log and count apart rather than as one
                    # "protected" total that hid a store outage among real
                    # tier refusals.
                    if decision.reason == "absent":
                        absent += 1
                        self._logger.info(
                            "Semantic consolidation skipped delete of %r: no "
                            "active lesson row (refused to avoid an "
                            "unconditional delete racing a re-add)",
                            item["key"],
                        )
                    elif decision.reason == "unreadable":
                        unreadable += 1
                        self._logger.warning(
                            "Semantic consolidation could not read lesson %r to "
                            "check its tier; refused the delete (store read "
                            "failed)",
                            item["key"],
                        )
                    else:
                        protected += 1
                        self._logger.info(
                            "Semantic consolidation refused to retire standing "
                            "lesson %r: a guess may retire an on_topic finding, "
                            "never a standing rule",
                            item["key"],
                        )
                    continue
                with self._publication_hold_checked(key, commit_state) as publication:
                    if private_policy:
                        published = vector_store.propose_semantic_delete(item["key"], source)
                        if published:
                            refused += 1
                    else:
                        # Compare-and-delete against the body the tier was
                        # read from: _lesson_key keys on rule text plus scope,
                        # so a concurrent delete-plus-re-add (the documented
                        # way to change a tier) can put a standing rule under
                        # this key between the read and here. expect_value_json
                        # makes the delete a no-op when the row moved, so a
                        # guess cannot tombstone a replacement it never checked.
                        # A non-lesson key carries None and deletes as before.
                        published = vector_store.delete_semantic(
                            item["key"],
                            source,
                            expect_value_json=decision.checked_value_json,
                        )
                        if published:
                            deleted += 1
                        elif decision.checked_value_json is not None:
                            # The compare-and-delete lost: the row body moved
                            # between the guard's read and the UPDATE, so
                            # nothing was tombstoned. Announce it like the
                            # sibling paths do rather than letting it vanish
                            # into "0 deleted", which reads as no delete items.
                            stale_skipped += 1
                            self._logger.info(
                                "Semantic consolidation skipped delete of %r: "
                                "the row changed under the key after the tier "
                                "check (compare-and-delete no-op)",
                                item["key"],
                            )
                    if published:
                        publication.mark_committed()
                continue
            if "value" not in item or item["value"] is None:
                # Counted and logged here because this path returns before set_semantic, so
                # the VALUE_EMPTY reject event never fires for the omission that motivated it.
                skipped += 1
                self._logger.warning(
                    "Semantic consolidation skipped %r: item carries no value", item["key"]
                )
                continue
            conf = _v1_consolidation_confidence(item)
            if conf is None:
                skipped += 1
                continue
            # Always our own source, never "user_explicit": a confidence of 1.0 is the
            # LLM's claim that the user stated the fact, not proof of it. Under
            # `consolidation:<key>` the conflict resolution in _write_semantic protects
            # a genuine user-stated row from a re-summarization (conflict_skip) while
            # still letting consolidation create new keys and update its own.
            extra = {}
            metadata = _v1_consolidation_metadata(item, vector_store)
            if metadata is not None:
                extra["metadata"] = metadata
            if private_policy and snapshot and messages and item["key"] in snapshot:
                from kiro_crew.memory_record_metadata import verified_correction

                evidence = verified_correction(
                    key=item["key"],
                    before=snapshot[item["key"]],
                    value=item["value"],
                    quote=item.get("correction_quote"),
                    messages=messages,
                    session_key=key,
                )
                if evidence:
                    extra["correction"] = evidence
                    extra["expected_revision"] = evidence.revision
            previous = snapshot.get(item["key"]) if snapshot else None
            previous_value_json = previous.get("value_json") if isinstance(previous, dict) else None
            if not isinstance(previous_value_json, str):
                previous_value_json = None
            defer = budget.tripped
            embedding_generation = vector_store.space_generation
            with budget.measured():
                embedding = (
                    None if defer else vector_store.embed_semantic(item["key"], item["value"])
                )
                retirement_embedding = (
                    None
                    if defer or previous_value_json is None
                    else vector_store.embed_semantic_retirement(item["key"], previous_value_json)
                )
            with self._publication_hold_checked(key, commit_state) as publication:
                err = vector_store.set_semantic(
                    key=item["key"],
                    value=item["value"],
                    confidence=conf,
                    source=source,
                    facets=facets,
                    defer_embedding=defer,
                    embedding=embedding,
                    embedding_resolved=True,
                    embedding_generation=embedding_generation,
                    retirement_embedding=retirement_embedding,
                    retirement_embedding_resolved=True,
                    retirement_value_json=previous_value_json,
                    **extra,
                )
                if err is None:
                    publication.mark_committed()
            if err is None:
                written += 1
            else:
                # Counted apart from `skipped`: several reject causes reach here and only
                # VALUE_EMPTY is a missing value, so a shared label names the wrong cause.
                reject_code, reason = err
                refused += 1
                # The reason names the specific cause a bare code cannot (which
                # confidence lost, which proposal holds the value). Causes the store
                # audits leave a memory_events row under the cause as the event type:
                # log_reject_event stores only the rejected value (old_value None,
                # new_value cut to 200 chars). A conflict_skip row stores both values,
                # but not every conflict_skip refusal writes one: a revision mismatch
                # or bad metadata returns the code with no row. VALUE_SIZE and
                # VALUE_ENCODING audit nothing, which is why the pointer is scoped
                # rather than a promise for every code.
                self._logger.warning(
                    "Semantic consolidation refused %r: %s: %s"
                    " (memory_events records the rejected value for audited causes;"
                    " a conflict_skip row, when written, records both)",
                    item["key"],
                    reject_code.value,
                    reason,
                )
        if (
            written
            or deleted
            or skipped
            or refused
            or protected
            or absent
            or unreadable
            or stale_skipped
            or withheld
        ):
            self._logger.info(
                "Semantic consolidation: %d written, %d deleted, %d skipped (no value), "
                "%d refused, %d protected (standing rule), %d absent, %d unreadable, "
                "%d stale-skipped (compare-and-delete no-op), "
                "%d withheld (delete of a key not in the rendered table)",
                written,
                deleted,
                skipped,
                refused,
                protected,
                absent,
                unreadable,
                stale_skipped,
                withheld,
            )

    if routing.decisions:
        # Under the same hold as every other write of this pass: a switch turned
        # off, or a lease lost, before the run's first commit refuses the log too.
        with self._publication_hold_checked(key, commit_state) as publication:
            if record_kind_routing(vector_store, routing, source):
                publication.mark_committed()

    # Episodic entries
    episodic_items = result.get("episodic")
    if isinstance(episodic_items, list):
        written = 0
        deferred = 0
        for item in episodic_items[:_MAX_EPISODIC_PER_CONSOLIDATION]:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            tags = item.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                continue
            try:
                importance = float(item.get("importance", 0.5))
            except (ValueError, TypeError):
                continue
            if not math.isfinite(importance) or not 0 <= importance <= 1:
                continue
            # `defer_embedding` stores the row with a NULL vector instead of
            # embedding it here. The text is keyword-searchable at once and the
            # repair sweep fills the vector in.
            #
            # `preserve_existing` comes with it, and is not optional: without a
            # vector the similarity dedup cannot run, and on a legacy V1 store
            # at its episodic cap the insert would then tombstone the
            # lowest-importance row to make room for a paraphrase it never
            # compared against. A write that cannot arbitrate a conflict has no
            # standing to evict, so at the cap the deferred row is refused
            # instead — the transcript it came from is still on disk, and the
            # row it would have displaced is not recoverable.
            #
            # Read before the write, so the row that SPENDS the budget is the
            # last one to pay for an embed rather than the first to skip one.
            defer = budget.tripped
            with budget.measured():
                embedding_generation = vector_store.space_generation
                embedding = None if defer else vector_store.embed_episodic(item["text"])
                with self._publication_hold_checked(key, commit_state) as publication:
                    ep_ok = vector_store.write_episodic(
                        text=item["text"],
                        embedding=embedding,
                        embedding_resolved=True,
                        embedding_generation=embedding_generation,
                        conversation_id=key,
                        tags=tags,
                        importance=importance,
                        source=source,
                        facets=facets,
                        defer_embedding=defer,
                        preserve_existing=defer,
                    )
                    if ep_ok:
                        publication.mark_committed()
            if ep_ok:
                written += 1
                if defer:
                    deferred += 1
        if written:
            self._logger.info(
                "Wrote %d episodic entries from consolidation (%d with embedding deferred)",
                written,
                deferred,
            )
