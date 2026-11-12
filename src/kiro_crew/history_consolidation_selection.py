"""Which rows of an over-cap semantic table a consolidation pass shows the model.

A pass renders the store's semantic rows as the ``## Current Semantic Memory`` table of
its prompt, held under ``_SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION``
(:func:`_select_semantic_block`). A table that fits renders whole. Over the cap,
``memory.consolidation_selection`` picks how the rows are chosen:

* ``newest`` (the default): the most recently updated rows, under
  :data:`_SEMANTIC_OMISSION_NOTICE_NEWEST` (:func:`_newest_semantic_table`). No row is
  scored and no rotation is read or recorded.
* ``relevance``: the rows relevant to the span, the newest, the lessons and a slice that
  rotates through the rest, then :data:`_SEMANTIC_OMISSION_NOTICE` or, for a block picked
  without relevance, :data:`_SEMANTIC_OMISSION_NOTICE_UNSCORED`
  (:func:`_bounded_semantic_table`).

:func:`_recency` ranks a row's ``updated_at`` for the newest-first cuts. The two table
functions read only their arguments; :func:`_select_semantic_block` reads the setting on
the event loop, and the store's rotation cursor and the scores off it. The pass
(``history_consolidation_pass``) records where the rotation reached once the model's
answer is applied.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports ``history_consolidation_span`` only inside
:func:`_select_semantic_block`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.vector_memory_constants import (
    _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
    _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION,
    _SEMANTIC_RELEVANCE_SCAN_SECS_PER_CONSOLIDATION,
    _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from kiro_crew.vector_memory import VectorMemoryStore


#: The one line under a bounded ``## Current Semantic Memory`` table, inside its
#: cap (a cap too small to hold it gets the empty table alone:
#: ``_bounded_semantic_table``). The prompt tells the model to update or delete
#: the keys it can see, so a table that lost rows silently would read as "those
#: facts do not exist" and invite a deletion of nothing or a near-duplicate of a
#: dropped key. Same vocabulary as the chat path's startup omission notice, so a
#: reader learns one shape. This is the line of a block whose rows were ranked by
#: relevance; :data:`_SEMANTIC_OMISSION_NOTICE_UNSCORED` is the line of one picked
#: without it.
_SEMANTIC_OMISSION_NOTICE = (
    "[Context budget: omitted {count} of {total} semantic rows above the "
    "{limit}-character consolidation budget; the rows shown were chosen for "
    "relevance to this conversation, recent updates and a rotating slice of the "
    "rest. The table above is PARTIAL: a key you do not see may still exist, so "
    "update or delete only keys listed above and treat a missing key as unknown, "
    "not absent.]"
)

#: :data:`_SEMANTIC_OMISSION_NOTICE` for a block picked without relevance: no row
#: scored above zero, or the pass had no scores (none were had, in time, or in the
#: rows' vector space). It names only the pools that picked the rows.
_SEMANTIC_OMISSION_NOTICE_UNSCORED = (
    "[Context budget: omitted {count} of {total} semantic rows above the "
    "{limit}-character consolidation budget; the rows shown are the most recent "
    "updates and a rotating slice of the rest. The table above is PARTIAL: a key "
    "you do not see may still exist, so update or delete only keys listed above "
    "and treat a missing key as unknown, not absent.]"
)


class _BoundedTable(NamedTuple):
    """A rendered semantic block, how many rows it left out, and the keys it shows.

    ``rotation_last`` is the last key the block's rotating slice showed or passed
    over, for the caller to record once the model's answer is applied; ``None``
    when there is no slice to record: the table fit whole, or the block shows no
    row because the cap is too small for the notice beside an empty table or the
    block would have rendered past its room. A block whose every row is wider than
    its room shows no row either, but records the last key its rotation passed over.
    """

    text: str
    omitted: int
    visible_keys: frozenset[str]
    rotation_last: str | None


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


def _bounded_semantic_table(
    rows: list[dict],
    entries: list[dict],
    cap: int,
    *,
    scores: list[float] | None = None,
    rotation_after: str | None = None,
) -> _BoundedTable:
    """Render ``entries`` as the prompt's semantic block, its notice included, in ``cap``.

    ``rows`` are the store rows (in the store's key order) that ``entries`` were
    rendered from, one to one; ``scores`` is each row's relevance to the span being
    consolidated (``score_consolidation_rows``) and ``rotation_after`` the last key
    the previous pass's rotating slice showed. A table that fits renders whole and
    byte-identical to the uncapped form, with no notice and no rotation.

    Over the cap the block is the kept rows, in the same key order as the whole
    table, then a newline and the omission notice, all within ``cap``. A cap too
    small for the notice beside an empty table keeps no row and renders ``[]``
    alone, or nothing under a cap of 0 or 1: a notice cut short would not say what
    it is for, and no row could fit beside a whole one. Rows are kept by pools, in
    order, each row once:

    1. rotation, ``_SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION``: the rows after
       ``rotation_after`` in key order, wrapping, for as long as each fits. The
       slice is contiguous, so the key it reached never lies past a row it could
       have shown and left out, and its first row only has to fit the block, so the
       slice reaches at least one row past ``rotation_after``; a row wider than the
       whole block is passed over and counted as omitted, so it cannot stall the
       rotation either.
    2. lessons, ``_SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION``: ``lesson.*`` rows,
       newest first. Not by relevance: ``write_lesson`` already dedups a new lesson
       against every lesson in its scope, so a lesson is shown here only so that a
       delete of it can go through.
    3. recency, ``_SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION``: the other rows,
       newest first.
    4. relevance, the rest of the block: the other rows that score above zero,
       best first.
    5. backfill, whatever is still free: the other rows newest first, then the
       lessons newest first.

    Pools 2 to 5 take every row that fits and pass over one that does not, and what
    pools 1 to 3 leave of their allowance stays free for 4 and 5. An allowance caps
    what its pool takes and reserves nothing: pools 2 and 3 run short of room only
    when the rotation leaves less than both their allowances. At the production cap
    the rotation takes at most its own allowance unless it shows one row wider than
    that, so they run short only on a pass whose rotation shows a row wider than
    the room less both allowances (about 48,800 characters), and then share what
    that row leaves, lessons first. Reserving their allowances instead would leave
    such a row unshown for as long as they have rows to fill them: it cannot fit
    beside them, and the rotation, the one pool that reaches every row, would have
    to stall at it or pass it.

    ``visible_keys`` are exactly the keys rendered, which is what the delete fence
    reads, whichever pool kept a row. ``scores`` that are not one finite number per
    row are no signal: the relevance pool is then empty. The notice says the rows
    were chosen for relevance only when some row scores above zero (:data:`_SEMANTIC_OMISSION_NOTICE`); otherwise it names the
    recent updates and the rotating slice alone
    (:data:`_SEMANTIC_OMISSION_NOTICE_UNSCORED`). The room is what the wider of the
    two leaves, so the line a block ends with never changes the rows it keeps.

    Costs are exact, not estimated, because ``json.dumps(list, indent=1)`` is
    additive: ``"[\\n"``, the rows joined by ``",\\n"``, ``"\\n]"``, each row
    rendered as it is alone in a list. A list of rows renders to 2 plus the sum
    of each row's one-element render less 2 (``[]`` included), so a pick is
    priced without re-rendering the block. A row is kept whole or not at all,
    because a value cut short would read as the fact itself. Returns the block,
    how many rows it left out, the keys it shows and the key its rotation
    reached.
    """
    keys = [str(r.get("key", "")) for r in rows]
    whole = json.dumps(entries, indent=1) if entries else "[]"
    if len(whole) <= cap:
        return _BoundedTable(whole, 0, frozenset(keys), None)
    total = len(rows)
    score = [0.0] * total
    if isinstance(scores, list) and len(scores) == total:
        try:
            given = [
                float(s) for s in scores if isinstance(s, (int, float)) and not isinstance(s, bool)
            ]
        except OverflowError:
            given = []
        if len(given) == total and all(math.isfinite(s) for s in given):
            score = given
    # The notice says relevance picked rows only when a score could pick one.
    template = (
        _SEMANTIC_OMISSION_NOTICE
        if any(s > 0 for s in score)
        else _SEMANTIC_OMISSION_NOTICE_UNSCORED
    )
    # The room is the same whichever line the block ends with, so the line chosen
    # never changes which rows fit.
    widest_notice = max(
        (
            notice.format(count=total, total=total, limit=cap)
            for notice in (_SEMANTIC_OMISSION_NOTICE, _SEMANTIC_OMISSION_NOTICE_UNSCORED)
        ),
        key=len,
    )
    # What the rows may add to the table's two bracket characters.
    room = cap - len("\n" + widest_notice) - 2
    if room < 0:
        return _BoundedTable("[]" if cap >= 2 else "", total, frozenset(), None)
    cost = [len(json.dumps([entry], indent=1)) - 2 for entry in entries]
    lesson = [key.startswith("lesson.") for key in keys]
    recency = [_recency(r.get("updated_at")) for r in rows]
    taken = [False] * total
    spent = 0

    def take(order: list[int], allowance: int | None) -> None:
        nonlocal spent
        pool = 0
        for index in order:
            if taken[index] or spent + cost[index] > room:
                continue
            if allowance is not None and pool + cost[index] > allowance:
                continue
            taken[index] = True
            spent += cost[index]
            pool += cost[index]

    by_key = sorted(range(total), key=lambda i: (keys[i], i))
    start = 0
    if isinstance(rotation_after, str):
        # The first key past the cursor, wrapping: a cursor whose own row is gone
        # still resumes at the next live key.
        start = next((pos for pos, i in enumerate(by_key) if keys[i] > rotation_after), 0)
    rotation_last: str | None = None
    rotated = 0
    for index in by_key[start:] + by_key[:start]:
        if cost[index] > room:
            rotation_last = keys[index]
            continue
        if rotated and rotated + cost[index] > min(
            _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION, room
        ):
            break
        taken[index] = True
        spent += cost[index]
        rotated += cost[index]
        rotation_last = keys[index]

    # Newest first, the key breaking a tie so rows written in the same second keep
    # one order across runs (a stable sort on top of the key order).
    newest = sorted(range(total), key=lambda i: keys[i])
    newest.sort(key=lambda i: recency[i], reverse=True)
    lessons = [i for i in newest if lesson[i]]
    relevant = sorted(
        (i for i in range(total) if not lesson[i] and score[i] > 0), key=lambda i: keys[i]
    )
    relevant.sort(key=lambda i: score[i], reverse=True)
    take(lessons, _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION)
    take([i for i in newest if not lesson[i]], _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION)
    take(relevant, None)
    take([i for i in newest if not lesson[i]] + [i for i in newest if lesson[i]], None)

    shown = [i for i in range(total) if taken[i]]
    table = json.dumps([entries[i] for i in shown], indent=1) if shown else "[]"
    if len(table) > room + 2:
        # The costs above are exact for any JSON value, so this does not happen;
        # if a row ever rendered unlike its cost, the bound would still hold, and
        # the rotation stays where it was rather than recording a slice it dropped.
        shown, table, rotation_last = [], "[]", None
    notice = template.format(count=total - len(shown), total=total, limit=cap)
    return _BoundedTable(
        f"{table}\n{notice}",
        total - len(shown),
        frozenset(keys[i] for i in shown),
        rotation_last,
    )


#: The line under a table cut by ``memory.consolidation_selection: "newest"``
#: (:func:`_newest_semantic_table`), the only cut before relevance selection: kept word
#: for word so the default prompt does not change.
_SEMANTIC_OMISSION_NOTICE_NEWEST = (
    "[Context budget: omitted {count} of {total} semantic rows above the "
    "{limit}-character consolidation budget; the least recently updated rows were "
    "left out. The table above is PARTIAL: a key you do not see may still exist, "
    "so update or delete only keys listed above and treat a missing key as "
    "unknown, not absent.]"
)


def _newest_semantic_table(rows: list[dict], entries: list[dict], cap: int) -> _BoundedTable:
    """Render ``entries`` as the prompt's semantic table, keeping the newest rows within ``cap``.

    ``rows`` are the store rows (in the store's key order) that ``entries`` were
    rendered from, one to one. A table that fits renders whole and byte-identical
    to the uncapped form. Over the cap, the most recently updated rows are kept
    -- the order the chat path's ``semantic_cap`` reads without a query -- and
    rendered in the same key order, then, when a row was left out, a newline and
    :data:`_SEMANTIC_OMISSION_NOTICE_NEWEST`. The rows fit ``cap``; the notice is
    added past it. ``rotation_last`` is always ``None``: this cut keeps no slice.

    The fit is found by bisection on the row count with the real renderer rather
    than by an estimate per row, so the bound is exact for whatever ``indent``
    and escaping produce, at the cost of O(log n) serialisations. A row is kept
    whole or not at all: when even the newest row alone is over the cap the
    table is ``[]`` and every row counts as omitted, because a value cut short
    would read as the fact itself.
    """
    whole = json.dumps(entries, indent=1) if entries else "[]"
    if len(whole) <= cap:
        return _BoundedTable(whole, 0, frozenset(str(r.get("key", "")) for r in rows), None)
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
    omitted = len(rows) - fits
    if omitted:
        notice = _SEMANTIC_OMISSION_NOTICE_NEWEST.format(count=omitted, total=len(rows), limit=cap)
        rendered = f"{rendered}\n{notice}"
    return _BoundedTable(rendered, omitted, visible, None)


def _consolidation_selection(logger: logging.Logger) -> str:
    """``memory.consolidation_selection`` from the live config; the default when unreadable."""
    from kiro_crew.config.memory_sections import consolidation_selection_mode_from

    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "consolidation_selection", None)
    except Exception:
        logger.debug(
            "Could not read memory.consolidation_selection; using the default", exc_info=True
        )
        raw = None
    return consolidation_selection_mode_from(raw)


async def _select_semantic_block(
    logger: logging.Logger,
    key: str,
    vector_store: VectorMemoryStore,
    rows: list[dict],
    entries: list[dict],
    *,
    chunk: list[dict],
    is_sub_slice: bool,
    fetched_in: object,
    run_pool: Callable[..., Awaitable[Any]],
) -> tuple[_BoundedTable, str | None]:
    """The pass's bounded semantic block, and the rotation cursor it was picked after.

    ``rows`` and ``entries`` are the store rows and their prompt entries, one to one;
    ``chunk`` is the span the pass prompts (``is_sub_slice`` when it is a head slice
    whose content is already stripped), ``fetched_in`` the store's
    ``space_generation`` read before the rows were, and ``run_pool`` the background
    memory pool the scorer runs on. Under ``newest`` this is
    :func:`_newest_semantic_table`, with no cursor. Under ``relevance`` an over-cap
    table reads the cursor and scores the rows against the span's user text, then
    packs the block (:func:`_bounded_semantic_table`); a table that fits needs no
    signal, so it costs no embed and leaves the rotation alone. The setting is read
    on the loop, as ``memory.kind_routing`` is (one small config read); every step
    that touches the table runs off it, since the table can run to megabytes.
    """
    cap = _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
    if _consolidation_selection(logger) != "relevance":
        return await asyncio.to_thread(_newest_semantic_table, rows, entries, cap), None
    semantic_scores: list[float] | None = None
    rotation_after: str | None = None
    overflows = await asyncio.to_thread(lambda: len(json.dumps(entries, indent=1)) > cap)
    if overflows:
        # Both reads are still before the billed turn, where an error would re-arm
        # the pass every idle tick, so each degrades on its own: a cursor that cannot
        # be read restarts the rotation at the first key, and scores that cannot be
        # had, or not in time, leave the block to the rotation and recency.
        try:
            cursor = await asyncio.to_thread(vector_store.consolidation_rotation_cursor)
        except Exception:
            logger.debug("Consolidation rotation cursor unavailable for %s", key, exc_info=True)
            cursor = None
        rotation_after = cursor if isinstance(cursor, str) else None
        semantic_scores = await _relevance_scores(
            logger,
            key,
            vector_store,
            rows,
            entries,
            chunk=chunk,
            is_sub_slice=is_sub_slice,
            fetched_in=fetched_in,
            run_pool=run_pool,
        )
    table = await asyncio.to_thread(
        _bounded_semantic_table,
        rows,
        entries,
        cap,
        scores=semantic_scores,
        rotation_after=rotation_after,
    )
    return table, rotation_after


async def _relevance_scores(
    logger: logging.Logger,
    key: str,
    vector_store: VectorMemoryStore,
    rows: list[dict],
    entries: list[dict],
    *,
    chunk: list[dict],
    is_sub_slice: bool,
    fetched_in: object,
    run_pool: Callable[..., Awaitable[Any]],
) -> list[float] | None:
    """Each row's relevance to the span, or ``None`` when no score can be had in time."""
    wait_secs = (
        _SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION
        + _SEMANTIC_RELEVANCE_SCAN_SECS_PER_CONSOLIDATION
    )
    try:
        # Imported here: the facade serves every name an owner binds at module
        # level, and its set of names is frozen.
        from kiro_crew.history_consolidation_span import _prompt_rows
        from kiro_crew.image_refs import STRIPPED_IMAGE_MARKER, strip_image_refs
        from kiro_crew.vector_memory_runtime.consolidation_relevance import _SCORER_IN_FLIGHT

        # Facts restate or contradict what the user said, so the user rows are the
        # query; an assistant-only slice of a long head falls back to every row it
        # prompts. The text is the rows' own words: not the ``[ts] ROLE:`` prefix the
        # conversation renders, which would match every ``user.*`` key on the word
        # "user", nor the marker an image reference renders as, whose words would
        # match any ``*image*`` key.
        prompted = _prompt_rows(chunk)
        spoken = [m for m in prompted if m.get("role") == "user"] or prompted
        query_text = "\n".join(
            (m["content"] if is_sub_slice else strip_image_refs(m["content"])).replace(
                STRIPPED_IMAGE_MARKER, " "
            )
            for m in spoken
        )
        # Bounded twice: the deadline travels into the worker, where the embed gives
        # up on it and the rows score on keywords, and the wait itself ends a scan's
        # allowance later whatever the thread is doing. A native call already running
        # cannot be stopped, so its thread is left to finish on its own, and it keeps
        # its pool admission until it does. So one scorer runs at a time in the
        # process: the claim is taken and given back on the pool thread by the call
        # itself, never by a wait that gave up, and a pass that finds it held takes
        # no scores rather than risk leaving a second thread behind.
        score = vector_store.score_consolidation_rows

        def _score_unless_one_runs(*args: Any, **kwargs: Any) -> list[float] | None:
            if not _SCORER_IN_FLIGHT.acquire(blocking=False):
                logger.debug(
                    "Consolidation relevance scores for %s skipped: another "
                    "pass's scorer is still running",
                    key,
                )
                return None
            try:
                return score(*args, **kwargs)
            finally:
                _SCORER_IN_FLIGHT.release()

        deadline = _time.monotonic() + _SEMANTIC_RELEVANCE_EMBED_SECS_PER_CONSOLIDATION
        scores = await asyncio.wait_for(
            run_pool(
                _score_unless_one_runs,
                rows,
                [str(entry["value_json"]) for entry in entries],
                query_text,
                deadline=deadline,
            ),
            wait_secs,
        )
        if vector_store.space_generation != fetched_in:
            # The rows' vectors predate a model swap the query's embed may not: a
            # cosine across two spaces is noise.
            return None
    except TimeoutError:
        logger.warning(
            "Consolidation relevance scores for %s took over %.0fs; this pass "
            "picks its semantic block by rotation and recency alone",
            key,
            wait_secs,
        )
        return None
    except Exception:
        logger.debug("Consolidation relevance signal unavailable for %s", key, exc_info=True)
        return None
    return scores
