"""How an over-cap ``## Current Semantic Memory`` block picks its rows.

``_bounded_semantic_table`` renders a table that fits byte-identical. Over the cap it
fills pools in order -- a slice of the key order that rotates from pass to pass, the
lessons most relevant to the span, the newest facts, the most relevant facts, and a
newest-first backfill -- inside one bound that covers the omission notice too. What
must never break, because nothing downstream would notice:

* the keys it reports as visible are exactly the keys it renders: the delete fence
  trusts them and nothing else;
* the whole block, notice included, stays inside the cap, whatever the cap;
* the rotation's slice is contiguous and reaches at least one row past the cursor, so
  the cursor never advances past a row the slice could have shown and left out, and
  every live row is shown within a bounded number of advances;
* the lessons and recency allowances reserve nothing, and the room binds them only
  on a pass whose rotation leaves less of it than both.

The function is driven directly with rows and the entries rendered from them;
``test_consolidation_semantic_cap`` drives the pass end to end.
"""

from __future__ import annotations

import itertools
import json
import math
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.history_consolidation import (
    _SEMANTIC_OMISSION_NOTICE,
    _SEMANTIC_OMISSION_NOTICE_UNSCORED,
    _bounded_semantic_table,
)
from kiro_crew.history_consolidation_memory import _recency
from kiro_crew.vector_memory_constants import (
    _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION,
    _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION,
    _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION,
)

_CAP = _SEMANTIC_PROMPT_CAP_PER_CONSOLIDATION
_ROTATION = _SEMANTIC_ROTATION_CHARS_PER_CONSOLIDATION
_LESSONS = _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION
_RECENCY = _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION


def _entry(key: str, value: object) -> dict:
    return {"key": key, "value_json": json.dumps(value), "confidence": 0.9}


def _cost(entry: dict) -> int:
    """What one row adds to a rendered table: its one-element render less two."""
    return len(json.dumps([entry], indent=1)) - 2


#: The two lines an over-cap block can end with: rows ranked by relevance, or not.
_NOTICES = (_SEMANTIC_OMISSION_NOTICE, _SEMANTIC_OMISSION_NOTICE_UNSCORED)


def _room(cap: int, total: int) -> int:
    """What the rows of an over-cap block may add to its two bracket characters.

    The wider notice is reserved whichever one the block ends with.
    """
    widest = max(len(notice.format(count=total, total=total, limit=cap)) for notice in _NOTICES)
    return cap - 1 - widest - 2


class _Table:
    """Rows in key order and the entries rendered from them, one to one."""

    def __init__(self, items: list[tuple[str, object, object]]) -> None:
        """*items* are ``(key, value, updated_at)``; the store hands them in key order."""
        items = sorted(items, key=lambda item: item[0])
        self.keys = [key for key, _, _ in items]
        self.rows = [{"key": key, "updated_at": stamp} for key, _, stamp in items]
        self.entries = [_entry(key, value) for key, value, _ in items]
        self.cost = {key: _cost(entry) for key, entry in zip(self.keys, self.entries)}

    def render(self, cap: int = _CAP, **signal):
        return _bounded_semantic_table(self.rows, self.entries, cap, **signal)

    def whole(self) -> str:
        return json.dumps(self.entries, indent=1) if self.entries else "[]"


def _uniform(count: int, *, prefix: str = "project.k_", chars: int = 900) -> list[tuple]:
    """*count* rows of one width, the higher the index the newer."""
    return [(f"{prefix}{index:04d}", "x" * chars, index) for index in range(count)]


def _shown(text: str) -> list[str]:
    table, _notice = text.rsplit("\n", 1)
    return [entry["key"] for entry in json.loads(table)]


def _cycle(keys: list[str], after: object) -> list[str]:
    """The key order the rotation walks: from the first key past *after*, wrapping."""
    start = 0
    if isinstance(after, str):
        start = next((index for index, key in enumerate(keys) if key > after), 0)
    return keys[start:] + keys[:start]


def _split(text: str) -> tuple[str, str | None]:
    """A block's table and its notice line, ``None`` for a block that carries none.

    No line of an indented JSON table starts like the notice, which is what tells a
    block with one from a table alone.
    """
    head, _, last = text.rpartition("\n")
    if last.startswith("[Context budget:"):
        return head, last
    return text, None


def _scores_read(table: _Table, scores: object) -> dict[str, float]:
    """Each key's score as the packer reads *scores*: all zero unless each is finite."""
    if (
        isinstance(scores, list)
        and len(scores) == len(table.keys)
        and all(isinstance(score, float) and math.isfinite(score) for score in scores)
    ):
        return dict(zip(table.keys, scores))
    return dict.fromkeys(table.keys, 0.0)


def _assert_invariants(
    table: _Table, cap: int, after: object, result, *, scores: object = None
) -> None:
    whole = table.whole()
    if len(whole) <= cap:
        assert result == (whole, 0, frozenset(table.keys), None)
        return
    total = len(table.keys)
    body, notice = _split(result.text)
    rendered = json.loads(body) if body else []
    shown = [entry["key"] for entry in rendered]

    # One bound for the whole block, whatever the cap, and the fence reads exactly
    # what the model reads, whichever pool kept a row.
    assert len(result.text) <= cap
    assert result.visible_keys == frozenset(shown)
    assert result.omitted == total - len(shown) >= min(total, 1)
    room = _room(cap, total)
    if room < 0:
        # No room for the notice beside an empty table: no row, no notice, and no
        # slice to record.
        assert result.text == ("[]" if cap >= 2 else "")
        assert result.rotation_last is None
        return
    # The notice counts every row left out, and claims relevance only when a score
    # could have picked a row.
    ranked = any(score > 0 for score in _scores_read(table, scores).values())
    template = _SEMANTIC_OMISSION_NOTICE if ranked else _SEMANTIC_OMISSION_NOTICE_UNSCORED
    assert notice == template.format(count=result.omitted, total=total, limit=cap)
    # Whole rows, never a cut value, in the key order of the whole table.
    by_key = dict(zip(table.keys, table.entries))
    assert rendered == [by_key[key] for key in sorted(shown)]

    cost = table.cost
    # The rotation walks the cycle from the cursor to the key it reports and
    # passes no row it could have shown; it shows at least one when any fits.
    cycle = _cycle(table.keys, after)
    assert result.rotation_last in table.keys
    run = cycle[: cycle.index(result.rotation_last) + 1]
    assert [key for key in run if key not in shown and cost[key] <= room] == []
    rotated = [key for key in run if key in shown]
    assert rotated or all(cost[key] > room for key in table.keys)
    assert len(rotated) <= 1 or sum(cost[key] for key in rotated) <= _ROTATION
    if len(run) < len(cycle):
        # It stopped at a row it could show but whose turn would pass its allowance.
        stop = cycle[len(run)]
        assert cost[stop] <= room
        assert sum(cost[key] for key in rotated) + cost[stop] > min(_ROTATION, room)
    # The lessons and recency allowances reserve nothing, but the room binds them
    # only when the rotation leaves less of it than both: each pool then takes what
    # its allowance alone admits, in its own order.
    if room - sum(cost[key] for key in rotated) >= _LESSONS + _RECENCY:
        score = _scores_read(table, scores)
        stamp = {row["key"]: _recency(row["updated_at"]) for row in table.rows}
        lessons = sorted(key for key in table.keys if key.startswith("lesson."))
        lessons.sort(key=lambda key: (score[key], stamp[key]), reverse=True)
        facts = sorted(key for key in table.keys if not key.startswith("lesson."))
        facts.sort(key=lambda key: stamp[key], reverse=True)
        held = set(rotated)
        for order, allowance in ((lessons, _LESSONS), (facts, _RECENCY)):
            pool = 0
            for key in order:
                if key not in held and pool + cost[key] <= allowance:
                    pool += cost[key]
                    assert key in shown, key
    # Nothing left out would still fit: every pool after the rotation is first-fit
    # and the backfill reaches every row.
    left = room - sum(cost[key] for key in shown)
    assert left >= 0
    assert [key for key in table.keys if key not in shown and cost[key] <= left] == []


# ── one property over every shape ─────────────────────────────────────────────────────

#: Characters JSON escapes (quotes, backslashes, controls), non-ASCII the default
#: ``ensure_ascii`` writes as ``\\uXXXX`` (one astral, as a surrogate pair), and plain text.
_CHARS = list('ab /"\\\n\té中\U0001f600')


@st.composite
def _stores(draw):
    named = draw(
        st.lists(
            st.tuples(
                st.sampled_from(["lesson.", "pref.", "project.", "user."]), st.integers(0, 99)
            ),
            min_size=1,
            max_size=30,
            unique=True,
        )
    )
    items = []
    for prefix, number in named:
        # A run of one character plus a short tail reaches tens of thousands of
        # rendered characters cheaply, so some rows are wider than a whole block.
        value = draw(st.sampled_from(_CHARS)) * draw(st.integers(0, 9_000)) + draw(
            st.text(alphabet=_CHARS, max_size=12)
        )
        stamp = draw(
            st.one_of(
                st.integers(0, 20),
                st.sampled_from(["2026-01-05 12:00:00", "2026-01-05T01:00:00+00:00"]),
                st.just("yesterday"),
                st.none(),
            )
        )
        items.append((f"{prefix}k{number:02d}", value, stamp))
    return _Table(items)


# Each example costs a few milliseconds; the property is the one test that sees
# every pool against every other, so it gets more of them than the default.
@settings(max_examples=200)
@given(st.data())
def test_every_block_shows_what_it_reports_inside_the_cap_and_rotates_without_skipping(
    data,
) -> None:
    table = data.draw(_stores())
    total = len(table.keys)
    # The first cap with room for the notice beside an empty table.
    edge = next(cap for cap in range(2_000) if _room(cap, total) >= 0)
    # Half the caps fall inside the table's own width, so most blocks go over and
    # keep some of their rows, which is where the pools meet; the rest reach down
    # to nothing, through the caps too small for the notice and the edge of it.
    cap = data.draw(
        st.one_of(
            st.integers(min_value=600, max_value=70_000),
            st.integers(min_value=600, max_value=max(600, len(table.whole()))),
            st.integers(min_value=0, max_value=600),
            st.integers(min_value=edge - 3, max_value=edge + 3),
        )
    )
    scores = data.draw(
        st.one_of(
            st.none(),
            st.lists(st.floats(0, 1), min_size=total, max_size=total),
            st.lists(st.sampled_from([0.0, 0.5, math.nan]), min_size=total, max_size=total),
        )
    )
    after = data.draw(
        st.one_of(
            st.none(),
            st.sampled_from(table.keys),
            st.text(alphabet="abeklnoprstu._0123456789", max_size=14),
        )
    )

    result = table.render(cap, scores=scores, rotation_after=after)

    _assert_invariants(table, cap, after, result, scores=scores)


def test_the_rendered_length_is_the_sum_of_the_row_costs(seeded_rng) -> None:
    """The identity the packer prices rows with, on rows JSON has to escape."""
    for _ in range(40):
        entries = [
            _entry(
                f"project.k{index}",
                "".join(seeded_rng.choice(_CHARS) for _ in range(seeded_rng.randrange(0, 60))),
            )
            for index in range(seeded_rng.randrange(0, 30))
        ]
        rendered = json.dumps(entries, indent=1) if entries else "[]"
        costs = [len(json.dumps([entry], indent=1)) - 4 for entry in entries]
        assert len(rendered) == 2 + sum(cost + 2 for cost in costs)
        if entries:
            assert len(rendered) == 4 + sum(costs) + 2 * (len(entries) - 1)


# ── the fast path and the edge of the cap ─────────────────────────────────────────────


def test_a_table_that_fits_renders_whole_whatever_the_signal() -> None:
    table = _Table(_uniform(20, chars=40))

    for signal in ({}, {"scores": [1.0] * 20, "rotation_after": "project.k_0005"}):
        result = table.render(**signal)
        assert result == (table.whole(), 0, frozenset(table.keys), None)


def test_a_table_exactly_at_the_cap_renders_whole_and_one_over_it_does_not() -> None:
    table = _Table(_uniform(80))
    exact = len(table.whole())

    assert table.render(exact) == (table.whole(), 0, frozenset(table.keys), None)
    over = table.render(exact - 1)
    assert over.omitted >= 1 and len(over.text) <= exact - 1


def test_a_cap_too_small_for_the_notice_renders_the_empty_table_alone() -> None:
    # Three 300-character rows: the empty table with the whole notice beside it
    # comes to 364 characters, past a cap of 200.
    table = _Table(_uniform(3, chars=300))
    edge = next(cap for cap in range(2_000) if _room(cap, 3) >= 0)

    # The edge is the wider notice's, so a block that would end with the narrower one
    # renders the empty table alone below it too.
    for cap, scores in itertools.product((0, 1, 2, 200, edge - 1), ([1.0] * 3, None)):
        result = table.render(cap, scores=scores, rotation_after="project.k_0000")
        assert result == ("[]" if cap >= 2 else "", 3, frozenset(), None)
    # From the edge up the notice fits, here beside a table with no room for a row.
    result = table.render(edge, scores=[1.0] * 3)
    assert result.text == "[]\n" + _SEMANTIC_OMISSION_NOTICE.format(count=3, total=3, limit=edge)
    assert len(result.text) == edge
    assert result.visible_keys == frozenset()
    unscored = table.render(edge)
    assert unscored.text == "[]\n" + _SEMANTIC_OMISSION_NOTICE_UNSCORED.format(
        count=3, total=3, limit=edge
    )
    assert len(unscored.text) < edge
    # An empty store under a cap of 0 or 1 renders nothing, and raises nothing.
    for cap in (0, 1):
        assert _bounded_semantic_table([], [], cap) == ("", 0, frozenset(), None)


# ── what the notice says about how the rows were picked ───────────────────────────────

_PARTIAL = "The table above is PARTIAL"


def _notice_of(result) -> str:
    return result.text.rsplit("\n", 1)[1]


def test_a_block_ranked_by_relevance_says_so() -> None:
    table = _Table(_uniform(100))
    scores = [1.0 if key == "project.k_0050" else 0.0 for key in table.keys]

    result = table.render(scores=scores)

    assert _notice_of(result) == _SEMANTIC_OMISSION_NOTICE.format(
        count=result.omitted, total=100, limit=_CAP
    )
    assert "chosen for relevance to this conversation" in _notice_of(result)


@pytest.mark.parametrize(
    "scores",
    [None, [0.0] * 100, [math.nan] * 100, [1.0] * 99],
    ids=["none", "all-zero", "not-finite", "one-short"],
)
def test_a_block_picked_without_relevance_does_not_claim_it(scores) -> None:
    table = _Table(_uniform(100))

    result = table.render(scores=scores, rotation_after="project.k_0040")

    notice = _notice_of(result)
    assert notice == _SEMANTIC_OMISSION_NOTICE_UNSCORED.format(
        count=result.omitted, total=100, limit=_CAP
    )
    assert "relevance" not in notice
    assert "recent updates and a rotating slice" in notice
    # The instruction that follows is the scored line's, word for word.
    tail = _SEMANTIC_OMISSION_NOTICE[_SEMANTIC_OMISSION_NOTICE.index(_PARTIAL) :]
    assert notice[notice.index(_PARTIAL) :] == tail


def test_the_line_a_block_ends_with_does_not_change_its_rows() -> None:
    """The room is the wider line's either way, so only the line differs."""
    table = _Table(_uniform(100))
    # The one relevant row is the newest, which the recency pool shows anyway.
    scores = [1.0 if key == "project.k_0099" else 0.0 for key in table.keys]

    ranked = table.render(scores=scores, rotation_after="project.k_0040")
    unranked = table.render(rotation_after="project.k_0040")

    assert _notice_of(ranked) != _notice_of(unranked)
    assert ranked.text.rsplit("\n", 1)[0] == unranked.text.rsplit("\n", 1)[0]
    assert ranked[1:] == unranked[1:]


# ── what each pool is for ─────────────────────────────────────────────────────────────


def test_the_most_relevant_row_is_shown_though_it_is_the_oldest() -> None:
    items = _uniform(100)
    # The middle key is the span's subject and the oldest row of the table.
    items[50] = ("project.k_0050", "x" * 900, -1)
    table = _Table(items)
    scores = [1.0 if key == "project.k_0050" else 0.0 for key in table.keys]
    # The slice starts far from it, so only relevance can show it.
    after = "project.k_0080"

    assert "project.k_0050" not in table.render(rotation_after=after).visible_keys
    assert "project.k_0050" in table.render(scores=scores, rotation_after=after).visible_keys


def test_the_relevance_pool_takes_the_best_scores_first() -> None:
    # Relevance rises with the key order while recency falls with it, so neither
    # the key order nor recency picks the rows relevance would.
    table = _Table([(key, value, -index) for index, (key, value, _) in enumerate(_uniform(100))])
    scores = [(index + 1) / 100 for index in range(100)]

    # The slice and the recency allowance take the middle and the start of the keys.
    result = table.render(scores=scores, rotation_after="project.k_0029")

    assert {f"project.k_{index:04d}" for index in range(90, 100)} <= result.visible_keys
    assert "project.k_0040" not in result.visible_keys


def test_with_no_signal_the_block_is_the_slice_and_the_newest_rows() -> None:
    table = _Table(_uniform(100))
    cost = table.cost["project.k_0000"]
    assert len(set(table.cost.values())) == 1
    per_slice = _ROTATION // cost

    result = table.render(rotation_after="project.k_0009")

    sliced = {f"project.k_{index:04d}" for index in range(10, 10 + per_slice)}
    fit = _room(_CAP, 100) // cost
    newest = [f"project.k_{index:04d}" for index in range(99, -1, -1)]
    newest = [key for key in newest if key not in sliced][: fit - per_slice]
    assert result.visible_keys == sliced | set(newest)
    assert result.rotation_last == f"project.k_{9 + per_slice:04d}"


def test_the_newest_row_is_shown_even_when_relevant_rows_could_fill_the_block() -> None:
    items = _uniform(100)
    table = _Table(items)
    # Every row but the newest is relevant, and there is far more of them than
    # room: the row just written (say with its vector still deferred) scores 0.
    scores = [0.0 if key == "project.k_0099" else 0.5 for key in table.keys]

    result = table.render(scores=scores, rotation_after="project.k_0009")

    assert "project.k_0099" in result.visible_keys


def test_a_store_of_mostly_lessons_still_shows_its_facts_and_its_relevant_lesson() -> None:
    # Ninety lessons, all newer than the ten facts, and one old lesson the span is about.
    items = [(f"lesson.l_{index:04d}", "y" * 900, 1_000 + index) for index in range(90)]
    items[40] = ("lesson.l_0040", "y" * 900, -1)
    items += [(f"project.f_{index:04d}", "x" * 900, index) for index in range(10)]
    table = _Table(items)
    scores = [1.0 if key == "lesson.l_0040" else 0.0 for key in table.keys]

    result = table.render(scores=scores)

    assert {f"project.f_{index:04d}" for index in range(10)} <= result.visible_keys
    assert "lesson.l_0040" in result.visible_keys


def test_lessons_keep_to_their_allowance_however_relevant() -> None:
    lessons = [(f"lesson.l_{index:04d}", "y" * 900, index) for index in range(40)]
    facts = [(f"project.f_{index:04d}", "x" * 900, index) for index in range(60)]
    table = _Table(lessons + facts)
    # Every lesson outranks every fact, and the facts alone can fill the block.
    scores = [1.0 if key.startswith("lesson.") else 0.5 for key in table.keys]

    # Past the lessons, so the slice rotates through facts.
    result = table.render(scores=scores, rotation_after="lesson.zzzz")

    shown_lessons = [key for key in result.visible_keys if key.startswith("lesson.")]
    assert shown_lessons, "the lessons allowance shows the most relevant lessons"
    assert sum(table.cost[key] for key in shown_lessons) <= (
        _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION
    )
    # The facts take the rest of the block, by relevance.
    assert len(result.visible_keys) - len(shown_lessons) > (
        _SEMANTIC_RECENCY_CHARS_PER_CONSOLIDATION // table.cost["project.f_0000"]
    )


def test_the_recency_allowance_shows_the_newest_fact_however_new_the_lessons_are() -> None:
    # Lessons newer than every fact, facts that could fill the block on relevance,
    # and one fact just written that scores nothing (say its vector is deferred).
    lessons = [(f"lesson.l_{index:02d}", "y" * 900, 2_000 + index) for index in range(20)]
    facts = [(f"project.f_{index:03d}", "x" * 900, index) for index in range(100)]
    table = _Table([*lessons, *facts, ("project.new_fact", "x" * 900, 1_500)])
    scores = [0.5 if key.startswith("project.f_") else 0.0 for key in table.keys]

    result = table.render(scores=scores, rotation_after="project.f_050")

    assert "project.new_fact" in result.visible_keys
    shown_lessons = [key for key in result.visible_keys if key.startswith("lesson.")]
    assert sum(table.cost[key] for key in shown_lessons) <= (
        _SEMANTIC_LESSON_CHARS_PER_CONSOLIDATION
    )


def test_an_allowance_passes_over_a_row_too_wide_for_what_is_left_of_it() -> None:
    # In score order the lessons cost about 5,000, 4,000 and 2,000 characters, and
    # in recency order so do three unscored facts: the middle one of each does not
    # fit what is left of its 8 KiB allowance, the last one does. Relevant facts
    # fill the rest of the block, so neither pool's last row can come in any other
    # way.
    lessons = [
        ("lesson.a_wide", "y" * 5_000, 0),
        ("lesson.b_mid", "y" * 4_000, 0),
        ("lesson.c_narrow", "y" * 2_000, 0),
    ]
    newest = [
        ("project.w_1", "x" * 5_000, 302),
        ("project.w_2", "x" * 4_000, 301),
        ("project.w_3", "x" * 2_000, 300),
    ]
    facts = [(f"project.f_{index:03d}", "x" * 900, index) for index in range(120)]
    table = _Table([*lessons, *newest, *facts])
    relevance = {"lesson.a_wide": 0.9, "lesson.b_mid": 0.8, "lesson.c_narrow": 0.7}
    scores = [
        relevance.get(key, 0.5 if key.startswith("project.f_") else 0.0) for key in table.keys
    ]

    result = table.render(scores=scores, rotation_after="project.f_050")

    assert {"lesson.a_wide", "lesson.c_narrow"} <= result.visible_keys
    assert "lesson.b_mid" not in result.visible_keys
    assert {"project.w_1", "project.w_3"} <= result.visible_keys
    assert "project.w_2" not in result.visible_keys


def test_a_row_too_wide_to_fit_beside_the_allowances_squeezes_them_on_its_turn_only() -> None:
    """The lessons and recency allowances cap their pools and reserve nothing.

    One 64,000-character fact at the cursor and twenty newer 300-character facts the
    recency allowance alone would show whole, beside a relevant lesson and older
    facts. The wide fact cannot fit beside full lessons and recency pools, so the
    pass whose rotation shows it gives them what it leaves, lessons first; every
    other pass of the sweep shows the lesson and all twenty, and the wide fact comes
    round again within one sweep of cursor advances.
    """
    wide = ("project.a_big", "x" * 64_000, 0)
    newer = [(f"project.z{index:02d}", "x" * 300, 1_000 + index) for index in range(20)]
    older = [(f"project.m_{index:03d}", "x" * 900, 1 + index) for index in range(100)]
    table = _Table([wide, *newer, *older, ("lesson.l_only", "y" * 300, 500)])
    scores = [1.0 if key == "lesson.l_only" else 0.0 for key in table.keys]
    room = _room(_CAP, len(table.keys))
    newest = [key for key, _, _ in reversed(newer)]
    # The twenty fit the recency allowance, the wide fact fits the block alone, and
    # it does not fit beside both allowances.
    assert sum(table.cost[key] for key in newest) <= _RECENCY
    assert room - _LESSONS - _RECENCY < table.cost["project.a_big"] <= room

    # Its turn: the slice is the wide fact alone, the lesson takes what it needs of
    # what is left, and the recency pool the newest of the twenty that still fit.
    turn = table.render(scores=scores, rotation_after="lesson.l_only")
    assert turn.rotation_last == "project.a_big"
    left = room - table.cost["project.a_big"] - table.cost["lesson.l_only"]
    kept = left // table.cost["project.z00"]
    assert 0 < kept < 20
    assert turn.visible_keys == {"project.a_big", "lesson.l_only", *newest[:kept]}

    # Every other pass of the sweep leaves both allowances whole.
    after = turn.rotation_last
    for _ in range(len(table.keys)):
        result = table.render(scores=scores, rotation_after=after)
        if "project.a_big" in result.visible_keys:
            break
        assert {"lesson.l_only", *newest} <= result.visible_keys
        after = result.rotation_last
    else:
        pytest.fail("the wide fact did not come round within one sweep of advances")
    assert result.rotation_last == "project.a_big"


@pytest.mark.parametrize(
    "scores",
    # Each would change the block if it were taken as scores: the valid entries
    # beside a bad one would fill the relevance pool.
    [
        MagicMock(),
        [0.5] * 99 + [math.nan],
        [0.5] * 99,
        [0.5] * 101,
        [True] * 100,
        "0.5",
        [0.5] * 99 + [None],
        [10**400] + [0.5] * 99,
    ],
    ids=["mock", "nan", "short", "long", "bools", "text", "none-entry", "huge-int"],
)
def test_scores_that_are_not_one_finite_number_per_row_are_no_signal(scores) -> None:
    items = _uniform(100)
    items[50] = ("project.k_0050", "x" * 900, -1)
    table = _Table(items)

    assert table.render(scores=scores, rotation_after="project.k_0080") == table.render(
        rotation_after="project.k_0080"
    )


# ── the rotation reaches every row ────────────────────────────────────────────────────


def _passes(
    table: _Table, count: int, *, after: str | None = None
) -> list[tuple[frozenset[str], list[str]]]:
    """*count* passes, each resuming where the previous one's slice reached.

    Returns, per pass, the keys the block showed and the run of the key order its
    rotation walked (from past the cursor to the key it reached).
    """
    passes = []
    for _ in range(count):
        result = table.render(rotation_after=after)
        cycle = _cycle(table.keys, after)
        run = cycle[: cycle.index(result.rotation_last) + 1] if result.rotation_last else []
        passes.append((result.visible_keys, run))
        after = result.rotation_last
    return passes


def test_rows_of_one_width_are_all_reached_within_a_sweep_of_slices() -> None:
    table = _Table(_uniform(240))
    per_slice = _ROTATION // table.cost["project.k_0000"]
    sweep = math.ceil(240 / per_slice)

    passes = _passes(table, sweep)

    walked = [key for _, run in passes for key in run]
    assert set(walked) == set(table.keys)
    # Each pass moves by a whole slice and never repeats a row inside one sweep,
    # so one pass fewer leaves rows the rotation has not reached.
    assert len(walked) == len(set(walked))
    assert set(walked[: -len(passes[-1][1])]) != set(table.keys)
    assert all(run[0] in visible for visible, run in passes)


def test_a_row_wider_than_the_slice_still_gets_its_turn() -> None:
    # Each row is wider than the rotation's whole allowance: only its first-row
    # rule lets the slice show one, and the old rows have no other way in.
    table = _Table(_uniform(30, chars=_ROTATION + 500))

    passes = _passes(table, 30)

    assert frozenset().union(*(visible for visible, _ in passes)) == frozenset(table.keys)
    assert all(len(run) == 1 for _, run in passes)


def test_a_row_wider_than_the_whole_block_is_passed_over_without_stalling() -> None:
    items = _uniform(100)
    items[20] = ("project.k_0020", "x" * (2 * _CAP), 20)
    table = _Table(items)

    result = table.render(rotation_after="project.k_0019")

    assert "project.k_0020" not in result.visible_keys
    assert "project.k_0021" in result.visible_keys
    assert result.rotation_last > "project.k_0020"


def test_a_cursor_whose_row_is_gone_resumes_at_the_next_live_key() -> None:
    table = _Table([item for item in _uniform(100) if item[0] != "project.k_0040"])

    result = table.render(rotation_after="project.k_0040")

    # The slice is the run of keys right after the missing one: no row is skipped.
    per_slice = _ROTATION // table.cost["project.k_0041"]
    assert result.rotation_last == f"project.k_{40 + per_slice:04d}"
    assert {f"project.k_{index:04d}" for index in range(41, 41 + per_slice)} <= (
        result.visible_keys
    )


def test_a_cursor_past_the_last_key_wraps_to_the_first() -> None:
    table = _Table(_uniform(100))

    result = table.render(rotation_after="project.zzzz")

    assert "project.k_0000" in result.visible_keys
    assert result.rotation_last < "project.k_0020"


def test_the_fairness_bound_counts_cursor_advances_not_completed_passes() -> None:
    """Passes that read one cursor show one slice, and only one of them advances it.

    Each row is wider than the rotation's allowance, so a slice is one row and an
    advance moves the cursor by exactly one. Each round, one to four passes read the
    cursor and record their slice by compare-and-set, as the store does: the first
    moves the cursor and the rest change nothing. Every row is then shown within as
    many advances as the table has rows, while as many completed passes can leave
    rows unshown, since a repeated slice shows nothing new.
    """
    table = _Table(_uniform(30, chars=_ROTATION + 500))
    stored: str | None = None
    completed: list[frozenset[str]] = []
    advanced: list[frozenset[str]] = []
    for round_ in range(120):
        expected = stored
        readers = [table.render(rotation_after=expected) for _ in range(1 + round_ % 4)]
        for result in readers:
            completed.append(result.visible_keys)
            if stored == expected:
                stored = result.rotation_last
                advanced.append(result.visible_keys)

    every = frozenset(table.keys)
    window = len(table.keys)
    assert len(completed) > len(advanced) > window
    for start in range(len(advanced) - window + 1):
        assert frozenset().union(*advanced[start : start + window]) == every
    assert any(
        frozenset().union(*completed[start : start + window]) != every
        for start in range(len(completed) - window + 1)
    )


def test_rows_inserted_and_deleted_between_passes_are_each_shown_in_time(seeded_rng) -> None:
    """Every row that stays live for more cursor advances than the table has rows is shown.

    Every pass here advances the cursor, and rows are written only between passes,
    never while one is in flight. Each advance moves the slice by several rows and
    at most one row is inserted ahead of it, so the slice gains on every live row; a
    row inserted behind it waits at most one wrap.
    """
    live = {
        f"project.k_{index:04d}": ("x" * seeded_rng.randrange(100, 2_000), index)
        for index in range(0, 300, 2)
    }
    next_stamp = 1_000
    after: str | None = None
    history: list[tuple[set[str], frozenset[str]]] = []
    for _ in range(400):
        table = _Table([(key, value, stamp) for key, (value, stamp) in live.items()])
        result = table.render(rotation_after=after)
        _assert_invariants(table, _CAP, after, result)
        history.append((set(live), result.visible_keys))
        after = result.rotation_last
        if seeded_rng.random() < 0.5:
            del live[seeded_rng.choice(sorted(live))]
        if seeded_rng.random() < 0.5:
            key = f"project.k_{seeded_rng.randrange(0, 300):04d}"
            live.setdefault(key, ("x" * seeded_rng.randrange(100, 2_000), next_stamp))
            next_stamp += 1

    window = max(len(keys) for keys, _ in history) + 1
    for start in range(len(history) - window + 1):
        span = history[start : start + window]
        steady = set.intersection(*(keys for keys, _ in span))
        shown = frozenset().union(*(visible for _, visible in span))
        assert steady <= shown, sorted(steady - shown)[:5]


# ── the bound holds even against a row that renders unlike its price ──────────────────


class _Growing(dict):
    """An entry whose third and later renders carry a field it was not priced with.

    The packer renders an entry three times: in the whole table, alone for its
    price, and in the block. No real entry renders differently each time, since a
    JSON value renders the same every time; this one stands in for any future way a
    row could render unlike its price, so the bound can be seen to rest on the
    rendered block rather than on the sum of the prices.
    """

    def __init__(self, entry: dict, pad: int) -> None:
        super().__init__(entry)
        self.pad = pad
        self.renders = 0

    def items(self):
        self.renders += 1
        if self.renders >= 3:
            return [*super().items(), ("padding", "z" * self.pad)]
        return super().items()


def test_a_block_that_would_render_over_its_room_shows_nothing_and_keeps_the_rotation() -> None:
    table = _Table(_uniform(100))
    plain = table.render(rotation_after="project.k_0009")
    block = plain.text.rsplit("\n", 1)[0]
    room = _room(_CAP, 100)
    # One shown row renders just enough wider than its price to take the table one
    # character past its room and still inside the cap: only a check of the room
    # itself, not of the cap, sees that the notice does not fit beside it.
    extra = room + 3 - len(block)
    assert 0 < extra and len(block) + extra <= _CAP
    index = table.keys.index(sorted(plain.visible_keys)[0])
    entry = table.entries[index]
    padded = len(json.dumps([dict(entry, padding="")], indent=1))
    overhead = padded - len(json.dumps([entry], indent=1))
    table.entries = list(table.entries)
    table.entries[index] = _Growing(entry, extra - overhead)

    result = table.render(rotation_after="project.k_0009")

    assert len(result.text) <= _CAP
    assert result.visible_keys == frozenset()
    assert result.omitted == 100
    assert result.rotation_last is None
