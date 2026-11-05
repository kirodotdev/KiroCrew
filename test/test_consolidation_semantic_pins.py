"""Over-cap semantic blocks pinned as the packer renders them, and the edges of its pools.

Nothing here names more than ``_bounded_semantic_table`` and the selection tests'
helpers, so the file runs unchanged on the packer before any change to it: that is
where its digest was measured, and a change to the packer that is meant to keep the
selection keeps this file green. The digest pins whole blocks for tables whose room
sits on a row's edge; the tests after it put a row exactly on each pool's allowance,
exactly as wide as the room, and one character past each, where a comparison written
one character off would show or drop a row.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from test_consolidation_semantic_selection import (
    _CAP,
    _LESSONS,
    _RECENCY,
    _ROTATION,
    _cost,
    _entry,
    _room,
    _Table,
    _uniform,
)

from kiro_crew.history_consolidation import _bounded_semantic_table

_PREFIXES = ("lesson.", "project.", "user.", "pref.", "project.")
#: JSON escapes the quote and the backslash, and writes the accented letter as an escape.
_ESCAPED = "é" + chr(34) + chr(92)


def _golden_cases():
    """Over-cap tables whose blocks are pinned by `_GOLDEN`.

    Four shapes (lessons among facts, escaped and non-ASCII values, a cap below the
    production one, rows each wider than the block), each with no scores and mixed
    ones, and a cursor that is absent, a third of the way in, or next to last. Then
    one uniform table at two caps derived from the room: one whose room is exactly
    a whole number of rows, one a character short of the next row, so a block that
    took one character more or less of room shows a row more or fewer.
    """
    for count, width, cap in (
        (120, 900, 65_536),
        (300, 300, 65_536),
        (30, 2_500, 20_000),
        (12, 70_000, 65_536),
    ):
        keys = sorted(f"{_PREFIXES[i % 5]}g{(i * 7919) % 10_000:05d}" for i in range(count))
        rows = [
            {"key": key, "updated_at": f"2026-01-{1 + i % 28:02d}T00:{i % 60:02d}:00"}
            for i, key in enumerate(keys)
        ]
        entries = [
            {
                "key": key,
                "value_json": json.dumps(_ESCAPED + "x" * (width // 2 + (i * 37) % (width // 2))),
                "confidence": 0.9,
            }
            for i, key in enumerate(keys)
        ]
        for scores in (None, [((i * 7) % 5) / 4 for i in range(count)]):
            for after in (None, keys[count // 3], keys[-2]):
                yield rows, entries, cap, scores, after
    table = _Table(_uniform(100))
    cost = table.cost[table.keys[0]]
    for slack in (0, cost - 1):
        cap = next(cap for cap in range(70_000) if _room(cap, 100) == 50 * cost + slack)
        for scores in (None, [(i % 3) / 2 for i in range(100)]):
            for after in (None, table.keys[50]):
                yield table.rows, table.entries, cap, scores, after


#: One running sha256 over every pinned block, in case order, measured on the packer
#: as it was before its counts: each block updates it with the JSON text of
#: ``[text, omitted, sorted visible keys, rotation_last]``.
_GOLDEN = "89d9698834c4f7ebe0979b7793cae7a3fe57fd0c410c6f675593f8882b6dd13b"


def test_the_pinned_tables_render_the_blocks_they_rendered_before_the_counts() -> None:
    """A change meant to alter the selection re-measures `_GOLDEN` in the same commit."""
    digest = hashlib.sha256()
    cases = []
    for rows, entries, cap, scores, after in _golden_cases():
        block = _bounded_semantic_table(rows, entries, cap, scores=scores, rotation_after=after)
        case = json.dumps(
            [block.text, block.omitted, sorted(block.visible_keys), block.rotation_last]
        )
        cases.append((case, block.omitted))
        digest.update(case.encode())

    assert len(cases) == 32
    assert all(omitted > 0 for _, omitted in cases), "every pinned table is over its cap"
    assert len({case for case, _ in cases}) == 32, "no two pinned cases render one block"
    assert digest.hexdigest() == _GOLDEN


# ── a row on the edge of each bound ───────────────────────────────────────────────────


def _sized(key: str, cost: int, stamp: object) -> tuple[str, str, object]:
    """A row of *key* that adds exactly *cost* characters to a rendered table."""
    item = (key, "x" * (cost - _cost(_entry(key, ""))), stamp)
    assert _cost(_entry(key, item[1])) == cost
    return item


def _edge(allowance: int, before: int, past: int) -> list[int]:
    """Row widths whose running total reaches *allowance* exactly, or *past* it.

    *before* rows of 1,000 characters, then the row that brings the total to the
    allowance plus *past*.
    """
    return [1_000] * before + [allowance - 1_000 * before + past]


@pytest.mark.parametrize("past", [0, 1], ids=["exactly", "one-past"])
@pytest.mark.parametrize("before", [0, 7])
def test_the_lessons_pool_takes_a_lesson_that_fills_its_allowance_exactly(
    before: int, past: int
) -> None:
    # The lessons are newer than every fact, so the last of them in their own order
    # is the oldest; the facts alone overfill the block, so the backfill, which
    # reaches lessons only after every fact, never shows a lesson the pool left.
    widths = _edge(_LESSONS, before, past)
    lessons = [_sized(f"lesson.l_{i}", w, 1_000 - i) for i, w in enumerate(widths)]
    facts = [_sized(f"project.f_{i:02d}", 1_000, i) for i in range(80)]
    table = _Table([*lessons, *facts])
    assert sum(table.cost[key] for key, _, _ in facts) > _room(_CAP, len(table.keys))

    # Past the lessons, so the slice rotates through facts.
    result = table.render(rotation_after="lesson.zzzz")

    edge = lessons[-1][0]
    assert {key for key, _, _ in lessons[:-1]} <= result.visible_keys
    assert (edge in result.visible_keys) == (past == 0)


@pytest.mark.parametrize("past", [0, 1], ids=["exactly", "one-past"])
@pytest.mark.parametrize("before", [0, 7])
def test_the_recency_pool_takes_a_fact_that_fills_its_allowance_exactly(
    before: int, past: int
) -> None:
    # The newest facts score nothing, and the relevant facts behind them fill what
    # the slice and the recency pool leave, to less than one row: a newest fact the
    # pool left is not shown by the backfill either.
    widths = _edge(_RECENCY, before, past)
    newest = [_sized(f"project.n_{i}", w, 2_000 - i) for i, w in enumerate(widths)]
    relevant = [_sized(f"project.r_{i:02d}", 1_000, i) for i in range(80)]
    table = _Table([*newest, *relevant])
    scores = [0.5 if key.startswith("project.r_") else 0.0 for key in table.keys]

    # Past the newest facts, so the slice rotates through relevant ones.
    result = table.render(scores=scores, rotation_after="project.n_9")

    edge = newest[-1][0]
    assert {key for key, _, _ in newest[:-1]} <= result.visible_keys
    assert (edge in result.visible_keys) == (past == 0)
    assert [key for key, _, _ in relevant if key not in result.visible_keys], "relevance ran out"


@pytest.mark.parametrize("past", [0, 1], ids=["exactly", "one-past"])
@pytest.mark.parametrize("before", [1, 7])
def test_the_rotation_takes_a_row_that_fills_its_allowance_exactly(before: int, past: int) -> None:
    # The slice's first row only has to fit the block, so the allowance first binds
    # on the second.
    widths = _edge(_ROTATION, before, past)
    run = [_sized(f"project.k_{10 + i:04d}", w, i) for i, w in enumerate(widths)]
    rest = [_sized(f"project.k_{i:04d}", 1_000, i) for i in (*range(10), *range(18, 100))]
    table = _Table([*run, *rest])

    result = table.render(rotation_after="project.k_0009")

    reached = run[-1] if past == 0 else run[-2]
    assert result.rotation_last == reached[0]


@pytest.mark.parametrize("past", [0, 1], ids=["exactly", "one-past"])
def test_the_rotation_shows_a_row_exactly_as_wide_as_the_room(past: int) -> None:
    rest = [_sized(f"project.k_{i:04d}", 1_000, i) for i in range(19)]
    room = _room(_CAP, len(rest) + 1)
    table = _Table([_sized("project.a_wide", room + past, 100), *rest])

    result = table.render(rotation_after="project.a")

    if past == 0:
        # The slice's first row fills the block on its own.
        assert result.visible_keys == {"project.a_wide"}
        assert result.rotation_last == "project.a_wide"
    else:
        # One character wider, it is passed over and every other row fits.
        assert result.visible_keys == frozenset(key for key, _, _ in rest)
        assert result.rotation_last == "project.k_0007"


@pytest.mark.parametrize("short", [0, 1], ids=["exactly", "one-short"])
def test_a_room_under_the_rotation_allowance_bounds_the_slice_to_its_last_character(
    short: int,
) -> None:
    table = _Table([_sized(f"project.k_{i:04d}", 1_000, i) for i in range(20)])
    room = 5 * 1_000 - short
    assert room < _ROTATION
    cap = next(cap for cap in range(70_000) if _room(cap, 20) == room)

    result = table.render(cap, rotation_after="project.k_0009")

    # Five rows fill the room to its last character; one short, the fifth is cut.
    shown = [f"project.k_{i:04d}" for i in range(10, 15 - short)]
    assert result.visible_keys == frozenset(shown)
    assert result.rotation_last == shown[-1]
