"""What an over-cap semantic block reports about itself.

``_select_semantic_rows`` returns the block ``_bounded_semantic_table`` renders and,
over the cap, a ``_SemanticSelection``: the size of the table, what the block shows,
what each pool took, and where the rotating slice began. The Hypothesis property in
``test_consolidation_semantic_selection`` checks the counts against every block it
draws; this file tells every pool label from every other, places the rotation by
ordinals, a slice that ends on the last key included, and fixes the two blocks that
show no row: one with no room for a row, and one that would render past its room.
``test_consolidation_semantic_pins`` pins the blocks themselves.
"""

from __future__ import annotations

import json

import pytest
from test_consolidation_semantic_selection import _ROTATION, _Growing, _room, _Table, _uniform

from kiro_crew.history_consolidation import _select_semantic_rows

#: Each pool in the order it picks, and the key prefix only it can take in `_by_pool`.
_POOL_PREFIXES = (
    ("rotation", "project.a_rot_"),
    ("lessons", "lesson."),
    ("recency", "project.n_"),
    ("relevance", "project.r_"),
    ("backfill", "project.b_"),
)


def _by_pool() -> tuple[_Table, list[float]]:
    """A table in which only one pool can take the rows of each prefix.

    Oldest first: six rows of about 2,000 characters just past the cursor, of which
    the rotation allowance holds four and nothing else reaches; twenty relevant facts
    of about 700; forty plain facts of about 900, newer than those, which the
    backfill takes newest first until the block is full; and the seven newest facts,
    about 1,150 each, which fill the recency allowance. Eight lessons of about 1,580
    fill theirs with five. No two pools take as many rows, or as many characters.
    """
    items = [(f"project.a_rot_{i}", "x" * 1_921, 0) for i in range(6)]
    items += [(f"project.r_{i:02d}", "x" * 624, 1) for i in range(20)]
    items += [(f"project.b_{i:02d}", "x" * 824, 2 + i) for i in range(40)]
    items += [(f"project.n_{i}", "x" * 1_075, 100 + i) for i in range(7)]
    items += [(f"lesson.l_{i}", "y" * 1_506, 50) for i in range(8)]
    table = _Table(items)
    scores = [
        0.9 - index / 100 if key.startswith("project.r_") else 0.0
        for index, key in enumerate(table.keys)
    ]
    return table, scores


def test_each_pool_is_credited_with_the_rows_only_it_could_take() -> None:
    table, scores = _by_pool()

    block, selection = _select_semantic_rows(
        table.rows, table.entries, 65_536, scores=scores, rotation_after="project.a"
    )

    assert selection is not None
    taken = [
        [key for key in block.visible_keys if key.startswith(prefix)]
        for _, prefix in _POOL_PREFIXES
    ]
    assert sum(map(len, taken)) == len(block.visible_keys)
    assert selection.pool_rows == tuple(len(keys) for keys in taken)
    assert selection.pool_chars == tuple(sum(table.cost[key] for key in keys) for keys in taken)
    # The fixture can tell any two labels apart.
    assert len(set(selection.pool_rows)) == len(set(selection.pool_chars)) == 5
    assert 0 not in selection.pool_rows


def test_the_rotation_is_placed_by_ordinals() -> None:
    table = _Table(_uniform(100))
    keys = table.keys

    def placed(after):
        _, selection = _select_semantic_rows(
            table.rows, table.entries, 65_536, rotation_after=after
        )
        assert selection is not None
        return selection.resumed, selection.rotation_start, selection.rotation_wrapped

    assert placed(None) == (False, 0, False)
    assert placed(keys[40]) == (True, 41, False)
    # Before the first key the slice starts there without coming round; at or past
    # the last it falls back to the first key, which is a wrap.
    assert placed("a") == (True, 0, False)
    assert placed(keys[-1]) == (True, 0, True)
    assert placed("zzz") == (True, 0, True)
    # Next to last, the slice runs off the end and on from the first key.
    assert placed(keys[-3]) == (True, 98, True)


@pytest.mark.parametrize("after", [5, object()], ids=["int", "object"])
def test_a_cursor_that_is_not_text_places_the_rotation_as_no_cursor_does(after: object) -> None:
    # The pass hands on a text cursor only; the block reads any other as none.
    table = _Table(_uniform(100))

    block, selection = _select_semantic_rows(
        table.rows, table.entries, 65_536, rotation_after=after
    )

    assert selection is not None and selection.resumed is False
    assert (selection.rotation_start, selection.rotation_wrapped) == (0, False)
    assert (block, selection) == _select_semantic_rows(table.rows, table.entries, 65_536)


def test_a_slice_comes_round_only_once_its_walk_passes_the_last_key() -> None:
    # The last key's row is wider than the block, so the slice passes over it: a row
    # passed over is walked as surely as one shown.
    items = _uniform(100)
    items[-1] = (items[-1][0], "x" * 70_000, 99)
    table = _Table(items)
    keys = table.keys
    assert table.cost[keys[-1]] > _room(65_536, 100), "wider than the block"
    held = _ROTATION // table.cost[keys[0]]

    def placed(after):
        block, selection = _select_semantic_rows(
            table.rows, table.entries, 65_536, rotation_after=after
        )
        assert selection is not None and selection.pool_rows[0] == held
        return block.rotation_last, selection.rotation_start, selection.rotation_wrapped

    # Its walk ends exactly on the last key: it has not come round.
    start = len(keys) - 1 - held
    assert placed(keys[start - 1]) == (keys[-1], start, False)
    # One key later it shows the first key too, past the last.
    assert placed(keys[start]) == (keys[0], start + 1, True)


def test_a_block_with_no_room_for_a_row_still_reports_the_scores_it_read() -> None:
    table = _Table(_uniform(3, chars=300))
    edge = next(cap for cap in range(2_000) if _room(cap, 3) >= 0)

    block, selection = _select_semantic_rows(
        table.rows, table.entries, edge - 1, scores=[1.0] * 3, rotation_after=table.keys[0]
    )

    assert block == ("[]", 3, frozenset(), None)
    assert selection is not None
    assert (selection.scored, selection.ranked, selection.resumed) == (True, True, True)
    assert (selection.shown_rows, selection.shown_chars) == (0, 0)
    assert selection.pool_rows == selection.pool_chars == (0,) * 5
    assert (selection.rotation_start, selection.rotation_wrapped) == (0, False)


@pytest.mark.parametrize(
    "after", ["project.k_0097", "zzz"], ids=["runs-off-the-end", "past-the-last-key"]
)
def test_a_block_that_would_render_over_its_room_counts_no_row_and_no_wrap(after: str) -> None:
    # Either cursor's slice comes round: one walks off the end of the key order,
    # the other finds no key past the cursor.
    table = _Table(_uniform(100))
    plain, walked = _select_semantic_rows(table.rows, table.entries, 65_536, rotation_after=after)
    assert walked is not None and walked.rotation_wrapped
    # One shown row renders just enough wider than its price, once the block is
    # built, to take the table one character past its room.
    body = plain.text.rsplit("\n", 1)[0]
    extra = _room(65_536, 100) + 3 - len(body)
    assert 0 < extra and len(body) + extra <= 65_536
    index = table.keys.index(sorted(plain.visible_keys)[0])
    entry = table.entries[index]
    overhead = len(json.dumps([dict(entry, padding="")], indent=1)) - len(
        json.dumps([entry], indent=1)
    )
    entries = list(table.entries)
    entries[index] = _Growing(entry, extra - overhead)

    block, selection = _select_semantic_rows(table.rows, entries, 65_536, rotation_after=after)

    assert (block.visible_keys, block.rotation_last) == (frozenset(), None)
    assert selection is not None
    assert (selection.shown_rows, selection.shown_chars) == (0, 0)
    assert selection.pool_rows == selection.pool_chars == (0,) * 5
    # Where the slice began stays; its wrap goes with the slice the block dropped.
    assert (selection.rotation_start, selection.rotation_wrapped) == (walked.rotation_start, False)


def test_a_table_that_fits_has_no_selection() -> None:
    table = _Table(_uniform(20, chars=40))

    block, selection = _select_semantic_rows(table.rows, table.entries, 65_536, scores=[1.0] * 20)

    assert block == (table.whole(), 0, frozenset(table.keys), None)
    assert selection is None
