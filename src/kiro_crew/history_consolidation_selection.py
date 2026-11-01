"""Which rows of an over-cap semantic table a consolidation pass shows the model.

A pass renders the store's semantic rows as the ``## Current Semantic Memory`` table of
its prompt, held under a cap (:func:`_select_semantic_rows`). A table that fits renders
whole; over the cap the block is the rows relevant to the span, the newest and a slice
that rotates through the rest, then an omission notice (:data:`_SEMANTIC_OMISSION_NOTICE`,
or :data:`_SEMANTIC_OMISSION_NOTICE_UNSCORED` for a block picked without relevance).
Beside the block, a :class:`_BoundedTable`, it returns a :class:`_SemanticSelection` of
what each pool added, which the pass logs as one line on the logger it hands
:meth:`_SemanticSelection.log`; :func:`_bounded_semantic_table` returns the block alone.
:func:`_recency` ranks a row's ``updated_at`` for the newest-first pools.

Every function here reads only its arguments, so none touches a store, the consolidator
or the event loop: the pass (``history_consolidation_pass``) gathers and scores the rows,
runs the selection off the loop and records where the rotation reached.

Part of the ``kiro_crew.history_consolidation`` family: that module is the import path and
the patch surface; this owner imports no other owner.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.vector_memory_constants import (
    _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION,
)

if TYPE_CHECKING:
    # Typing-only, so it adds no module name to the facade's frozen set.
    from typing import Literal


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


class _SemanticSelection(NamedTuple):
    """What an over-cap semantic block was picked from, and what each pool added to it.

    Counts, sizes and flags only, never a memory key. A character is what the cap
    measures, so a row's characters are its one-element render less two, and
    ``shown_chars`` is the block's table less its two brackets. ``pool_rows`` and
    ``pool_chars`` hold one number per pool in the order the pools pick (rotation,
    lessons, recency, relevance, backfill); each shown row counts once, in the pool
    that took it, so the pools sum to ``shown_rows`` and ``shown_chars``.

    ``scored`` is whether the scores were one finite number per row, ``ranked``
    whether some row scored above zero (the notice then names relevance), and
    ``resumed`` whether a text cursor was given. The rotation is placed by ordinals:
    ``rotation_start`` is the position in key order of the first key its slice
    considered, and ``rotation_wrapped`` whether the slice came round from the last
    key to the first. A block that shows no row because the cap leaves no room for
    one starts no slice (``0``, ``False``); one that would have rendered past its room
    keeps where its slice began but records no wrap, like the rotation it does not
    record.
    """

    table_rows: int
    table_chars: int
    cap: int
    shown_rows: int
    shown_chars: int
    pool_rows: tuple[int, ...]
    pool_chars: tuple[int, ...]
    scored: bool
    ranked: bool
    resumed: bool
    rotation_start: int
    rotation_wrapped: bool

    def log(
        self,
        failure: Literal["timeout", "error", "space_changed"] | None,
        key: str,
        logger: logging.Logger,
    ) -> None:
        """Log the selection as one INFO line on *logger* for the pass of session *key*.

        ``scores=`` is *failure*, the word for how the pass lost its scores, when it
        lost them; otherwise ``no_signal`` when they were not one finite number per
        row, ``no_positive`` when none was above zero, and ``used``. The other fields
        are counts and the closed words ``set``/``none`` and ``yes``/``no``, passed as
        exact ``int`` and ``str`` so the redacting record factory keeps the args as
        they are. Like every consolidation line it names the pass by its session key,
        and never a memory key, value, span text or exception message.
        """
        scores: str
        if failure is not None:
            scores = failure
        elif not self.scored:
            scores = "no_signal"
        elif not self.ranked:
            scores = "no_positive"
        else:
            scores = "used"
        logger.info(
            "Consolidation semantic block for %s: table_rows=%d table_chars=%d cap=%d "
            "shown_rows=%d shown_chars=%d scores=%s cursor=%s rotation_start=%d "
            "rotation_wrapped=%s rotation_rows=%d rotation_chars=%d lessons_rows=%d "
            "lessons_chars=%d recency_rows=%d recency_chars=%d relevance_rows=%d "
            "relevance_chars=%d backfill_rows=%d backfill_chars=%d",
            key,
            self.table_rows,
            self.table_chars,
            self.cap,
            self.shown_rows,
            self.shown_chars,
            scores,
            "set" if self.resumed else "none",
            self.rotation_start,
            "yes" if self.rotation_wrapped else "no",
            *(n for pair in zip(self.pool_rows, self.pool_chars) for n in pair),
        )


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
    """The block :func:`_select_semantic_rows` renders, without its counts."""
    block, _ = _select_semantic_rows(
        rows, entries, cap, scores=scores, rotation_after=rotation_after
    )
    return block


def _select_semantic_rows(
    rows: list[dict],
    entries: list[dict],
    cap: int,
    *,
    scores: list[float] | None = None,
    rotation_after: str | None = None,
) -> tuple[_BoundedTable, _SemanticSelection | None]:
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
    2. lessons, ``_SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION``: ``lesson.*`` rows by
       score, then recency.
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
    row are no signal: the relevance pool is then empty and lessons rank by recency
    alone. The notice says the rows were chosen for relevance only when some row
    scores above zero (:data:`_SEMANTIC_OMISSION_NOTICE`); otherwise it names the
    recent updates and the rotating slice alone
    (:data:`_SEMANTIC_OMISSION_NOTICE_UNSCORED`). The room is what the wider of the
    two leaves, so the line a block ends with never changes the rows it keeps.

    Costs are exact, not estimated, because ``json.dumps(list, indent=1)`` is
    additive: ``"[\\n"``, the rows joined by ``",\\n"``, ``"\\n]"``, each row
    rendered as it is alone in a list. A list of rows renders to 2 plus the sum
    of each row's one-element render less 2 (``[]`` included), so a pick is
    priced without re-rendering the block. A row is kept whole or not at all,
    because a value cut short would read as the fact itself.

    Returns the block, as a :class:`_BoundedTable` (its text, how many rows it left
    out, the keys it shows and the key its rotation reached), and beside it, over
    the cap, a :class:`_SemanticSelection`; a table that fits has none. The
    selection counts each shown row once, in the pool that took it, at the price
    above, so its pools sum to what the block shows. It says how the scores read,
    and places the rotation by ordinals, never by a key: where in key order its
    slice began, and whether the slice came round past the last key. A block that
    shows no row counts none in any pool.
    """
    keys = [str(r.get("key", "")) for r in rows]
    whole = json.dumps(entries, indent=1) if entries else "[]"
    if len(whole) <= cap:
        return _BoundedTable(whole, 0, frozenset(keys), None), None
    total = len(rows)
    score = [0.0] * total
    scored = False
    if isinstance(scores, list) and len(scores) == total:
        try:
            given = [
                float(s) for s in scores if isinstance(s, (int, float)) and not isinstance(s, bool)
            ]
        except OverflowError:
            given = []
        if len(given) == total and all(math.isfinite(s) for s in given):
            score = given
            scored = True
    ranked = any(s > 0 for s in score)
    resumed = isinstance(rotation_after, str)
    # The notice says relevance picked rows only when a score could pick one.
    template = _SEMANTIC_OMISSION_NOTICE if ranked else _SEMANTIC_OMISSION_NOTICE_UNSCORED
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
        nothing = (0,) * 5
        return _BoundedTable("[]" if cap >= 2 else "", total, frozenset(), None), (
            _SemanticSelection(
                total, len(whole), cap, 0, 0, nothing, nothing, scored, ranked, resumed, 0, False
            )
        )
    cost = [len(json.dumps([entry], indent=1)) - 2 for entry in entries]
    lesson = [key.startswith("lesson.") for key in keys]
    recency = [_recency(r.get("updated_at")) for r in rows]
    taken = [False] * total
    # The pool that took each row, by its place in the pick order; read for shown rows.
    pool_of = [-1] * total
    spent = 0

    def take(order: list[int], allowance: int | None, which: int) -> None:
        nonlocal spent
        pool = 0
        for index in order:
            if taken[index] or spent + cost[index] > room:
                continue
            if allowance is not None and pool + cost[index] > allowance:
                continue
            taken[index] = True
            pool_of[index] = which
            spent += cost[index]
            pool += cost[index]

    by_key = sorted(range(total), key=lambda i: (keys[i], i))
    start = 0
    came_round = False
    if isinstance(rotation_after, str):
        # The first key past the cursor, wrapping: a cursor whose own row is gone
        # still resumes at the next live key, and one at or past the last key
        # comes round to the first.
        found = next((pos for pos, i in enumerate(by_key) if keys[i] > rotation_after), None)
        came_round = found is None
        start = found or 0
    rotation_last: str | None = None
    rotated = 0
    # The positions the slice took or passed over, so its walk can be told to have
    # crossed the end of the key order.
    walked = 0
    for index in by_key[start:] + by_key[:start]:
        if cost[index] > room:
            rotation_last = keys[index]
            walked += 1
            continue
        if rotated and rotated + cost[index] > min(
            _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION, room
        ):
            break
        taken[index] = True
        pool_of[index] = 0
        spent += cost[index]
        rotated += cost[index]
        rotation_last = keys[index]
        walked += 1

    # Newest first, the key breaking a tie so rows written in the same second keep
    # one order across runs (a stable sort on top of the key order).
    newest = sorted(range(total), key=lambda i: keys[i])
    newest.sort(key=lambda i: recency[i], reverse=True)
    lessons = sorted((i for i in range(total) if lesson[i]), key=lambda i: keys[i])
    lessons.sort(key=lambda i: (score[i], recency[i]), reverse=True)
    relevant = sorted(
        (i for i in range(total) if not lesson[i] and score[i] > 0), key=lambda i: keys[i]
    )
    relevant.sort(key=lambda i: score[i], reverse=True)
    take(lessons, _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION, 1)
    take([i for i in newest if not lesson[i]], _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION, 2)
    take(relevant, None, 3)
    take([i for i in newest if not lesson[i]] + [i for i in newest if lesson[i]], None, 4)

    shown = [i for i in range(total) if taken[i]]
    table = json.dumps([entries[i] for i in shown], indent=1) if shown else "[]"
    if len(table) > room + 2:
        # The costs above are exact for any JSON value, so this does not happen;
        # if a row ever rendered unlike its cost, the bound would still hold, and
        # the rotation stays where it was rather than recording a slice it dropped.
        shown, table, rotation_last, walked, came_round = [], "[]", None, 0, False
    notice = template.format(count=total - len(shown), total=total, limit=cap)
    pools = range(5)
    return _BoundedTable(
        f"{table}\n{notice}",
        total - len(shown),
        frozenset(keys[i] for i in shown),
        rotation_last,
    ), _SemanticSelection(
        total,
        len(whole),
        cap,
        len(shown),
        len(table) - 2,
        tuple(sum(1 for i in shown if pool_of[i] == p) for p in pools),
        tuple(sum(cost[i] for i in shown if pool_of[i] == p) for p in pools),
        scored,
        ranked,
        resumed,
        start,
        # Came round: no key sorts past the cursor, or the walk crossed the end.
        came_round or start + walked > total,
    )
