"""Arithmetic behind the compaction method ladder (``session_compaction_methods``).

Pure functions over row lists and numbers, so every test here is a table: no
sessions, no files, no clock. The one contract that reaches outside the module
is ``split_tail`` agreeing with ``context.build_session_replay`` about where the
verbatim tail starts, because that is the tail the successor session is seeded
with; ``TestSplitTailMatchesReplay`` drives both over the same rows.
"""

from __future__ import annotations

import re

import pytest

from kiro_crew import session_compaction_methods as scm
from kiro_crew.config.sections import COMPACTION_METHODS


def _rows(*contents: str, role: str = "user") -> list[dict]:
    """Chronological rows alternating user/assistant unless *role* is forced."""
    rows = []
    for idx, content in enumerate(contents):
        rows.append(
            {
                "role": role if role != "user" or idx % 2 == 0 else "assistant",
                "content": content,
                "ts": f"2026-09-15T00:00:{idx:02d}+00:00",
            }
        )
    return rows


class TestConstants:
    def test_advance_outcomes_are_distinct_strings(self):
        assert {scm.OUTCOME_UNAVAILABLE, scm.OUTCOME_INSUFFICIENT} == {
            "unavailable",
            "insufficient",
        }

    def test_method_names_come_from_the_config_section(self):
        # The ladder's name set is owned by the config layer; the module re-exports
        # it rather than restating the names, so the two cannot drift apart.
        assert scm.COMPACTION_METHODS is COMPACTION_METHODS
        assert set(scm.COMPACTION_METHODS) == {
            scm.COMPACTION_METHOD_NATIVE,
            scm.COMPACTION_METHOD_SOFT,
            scm.COMPACTION_METHOD_SHAKE,
        }


class TestTokenArithmetic:
    @pytest.mark.parametrize(
        ("chars", "expected"),
        [(0, 0), (-5, 0), (1, 1), (4, 1), (5, 2), (80_000, 20_000)],
    )
    def test_estimate_rounds_up(self, chars, expected):
        assert scm.estimate_tokens(chars) == expected

    def test_no_second_tail_budget_exists(self):
        # The tail figure is the replay's own (``context_budget.replay_budget_chars``);
        # a module-local token->chars budget is the second number that opened
        # the digest/tail gap, so its absence is pinned.
        assert not hasattr(scm, "tail_budget_chars")


class TestWalkTail:
    def test_reserved_rows_are_skipped_not_stopped_at_once_their_share_is_spent(self):
        rows = _rows("old conversation", "y" * 50, "z" * 50, "newest")
        rows[1]["role"] = rows[2]["role"] = "inject"
        walk = scm.walk_tail(rows, budget_chars=10_000, reserve_role="inject", reserve_chars=60)
        # Newest first: the newest inject fits its share, the older one spills
        # and is skipped, and the walk still reaches the conversation behind it.
        assert walk.lines == [
            "Assistant: newest",
            "Inject: " + "z" * 50,
            "User: old conversation",
        ]
        # The tail starts at the OLDEST kept row, so the skipped inject inside
        # it does not move the boundary: rows[0:] is the tail, nothing dropped.
        assert walk.cut == 0
        split = scm.split_tail(rows, budget_chars=10_000, reserve_role="inject", reserve_chars=60)
        assert split.dropped == ()

    def test_the_cut_is_the_oldest_kept_row_not_a_count(self):
        rows = _rows("dropped", "x" * 40, "kept-1", "kept-2")
        rows[1]["role"] = "inject"
        # The budget admits the two newest rows; the inject spills its share and
        # is skipped; "dropped" then overflows the budget and ends the walk.
        budget = len("Assistant: kept-2") + 2 + len("User: kept-1") + 2 + 5
        walk = scm.walk_tail(rows, budget_chars=budget, reserve_role="inject", reserve_chars=1)
        assert walk.lines == ["Assistant: kept-2", "User: kept-1"]
        assert walk.cut == 2, "the skipped inject at index 1 is older than the tail's start"

    def test_a_custom_renderer_decides_the_line_and_its_cost(self):
        rows = _rows("abc", "defgh")
        walk = scm.walk_tail(rows, budget_chars=10_000, line_of=lambda r: r["content"])
        assert walk.lines == ["defgh", "abc"]
        # The budget is charged the renderer's line: one character short of
        # both rendered lines plus their separator keeps only the newest.
        tight = len("defgh") + scm.LINE_SEPARATOR_CHARS + len("abc") - 1
        walk = scm.walk_tail(rows, budget_chars=tight, line_of=lambda r: r["content"])
        assert walk.lines == ["defgh"]

    def test_an_oversized_newest_row_is_carried_whole_by_default_as_on_main(self):
        # The plain replay (no rotation method configured) walks exactly as
        # main's did: the one row the walk admits unconditionally is carried
        # whole, however large, and no truncation mark is written. Only a
        # rotation's projection charges the budget for the tail, so only a
        # rotation asks for the clip (``clip_newest``).
        budget = 120
        rows = _rows("older", "x" * (budget * 3))
        walk = scm.walk_tail(rows, budget_chars=budget)
        assert walk.lines == [scm.replay_line(rows[1])]
        assert scm._TRUNCATED_SUFFIX not in walk.lines[0]
        assert walk.cut == 1

    def test_an_oversized_newest_row_is_clipped_to_the_budget_when_asked(self):
        # With a rotation method configured the replay asks for the clip: the
        # fixed-budget projection charges budget_chars for the tail, so the
        # tail may not exceed it.
        budget = 120
        rows = _rows("older", "x" * (budget * 3))
        walk = scm.walk_tail(rows, budget_chars=budget, clip_newest=True)
        assert len(walk.lines) == 1
        assert len(walk.lines[0]) == budget
        assert walk.lines[0].endswith(scm._TRUNCATED_SUFFIX)
        assert walk.lines[0].startswith("Assistant: xxx")
        assert walk.cut == 1, "clipping changes the line, never which rows the tail covers"
        split = scm.split_tail(rows, budget_chars=budget)
        assert split.dropped == (rows[0],)

    def test_a_newest_row_that_fits_is_never_clipped(self):
        rows = _rows("older", "fits exactly")
        line = "Assistant: fits exactly"
        walk = scm.walk_tail(rows, budget_chars=len(line), clip_newest=True)
        assert walk.lines == [line]
        assert scm._TRUNCATED_SUFFIX not in walk.lines[0]

    def test_the_walk_and_the_digest_clip_an_overflowing_line_by_one_rule(self):
        line = "Assistant: " + "y" * 500
        clipped = scm._clip_line(line, 60)
        assert len(clipped) == 60 and clipped.endswith(scm._TRUNCATED_SUFFIX)
        rows = _rows("older", "y" * 500)
        assert scm.walk_tail(rows, budget_chars=60, clip_newest=True).lines == [clipped]
        digest_lines, _left_out = scm._select_within_budget([line], 60)
        assert digest_lines == [clipped]


class TestRowFingerprint:
    def test_a_repeated_delivery_id_on_a_different_row_is_a_different_fingerprint(self):
        # The id is digested WITH role, stamp and content: a newer row that
        # repeats the boundary's ``mid`` must not reproduce its fingerprint, or
        # the replay would stop at the newer row and drop the rows between.
        a = {"role": "user", "ts": "t1", "content": "x", "meta": {"mid": "m1"}}
        b = {"role": "user", "ts": "t2", "content": "y", "meta": {"mid": "m1"}}
        assert scm.row_fingerprint(a) != scm.row_fingerprint(b)
        assert scm.row_fingerprint(a) == scm.row_fingerprint(dict(a)), "stable for equal rows"

    def test_the_delivery_id_still_tells_two_otherwise_identical_rows_apart(self):
        a = {"role": "user", "ts": "t", "content": "x", "meta": {"mid": "m1"}}
        b = {"role": "user", "ts": "t", "content": "x", "meta": {"mid": "m2"}}
        c = {"role": "user", "ts": "t", "content": "x", "meta": {"sendId": "m1"}}
        assert len({scm.row_fingerprint(r) for r in (a, b, c)}) == 3
        assert scm.row_fingerprint(a).startswith("sha256:")

    def test_two_rows_with_one_stamp_have_two_fingerprints(self):
        a = {"role": "user", "ts": "2026-09-15T00:00:01+00:00", "content": "first"}
        b = {"role": "assistant", "ts": "2026-09-15T00:00:01+00:00", "content": "second"}
        assert scm.row_fingerprint(a) != scm.row_fingerprint(b)
        assert scm.row_fingerprint(a) == scm.row_fingerprint(dict(a)), "stable for equal rows"


class TestProjection:
    def test_unknown_window_yields_no_projection(self):
        assert scm.project_pct_after(window_tokens=0, carried_tokens=1000) is None
        assert scm.project_pct_after(window_tokens=-1, carried_tokens=1000) is None

    def test_projection_is_carried_plus_overhead_over_window(self):
        pct = scm.project_pct_after(
            window_tokens=1_000_000, carried_tokens=20_000, overhead_tokens=40_000
        )
        assert pct == pytest.approx(6.0)

    def test_projection_clamps_at_one_hundred(self):
        assert scm.project_pct_after(window_tokens=100, carried_tokens=500) == 100.0

    def test_negative_inputs_count_as_zero(self):
        assert scm.project_pct_after(window_tokens=100, carried_tokens=-7) == 0.0

    @pytest.mark.parametrize(
        ("pct_after", "sufficient"),
        [
            (None, False),  # no figure, no verdict
            (90.0, False),  # at the threshold
            (86.0, False),  # inside the min-effect band
            (85.0, True),  # exactly the margin
            (10.0, True),
        ],
    )
    def test_sufficiency_requires_the_min_effect_margin(self, pct_after, sufficient):
        assert (
            scm.method_sufficient(pct_after, threshold_pct=90.0, min_effect_pct_points=5.0)
            is sufficient
        )


class TestSplitTail:
    def test_empty_rows_split_to_nothing(self):
        split = scm.split_tail([], budget_chars=100)
        assert split.dropped == () and split.tail == ()

    def test_everything_fits_when_the_budget_is_large(self):
        rows = _rows("a", "b", "c")
        split = scm.split_tail(rows, budget_chars=10_000)
        assert split.dropped == ()
        assert [r["content"] for r in split.tail] == ["a", "b", "c"]

    def test_cut_falls_between_rows_newest_first(self):
        rows = _rows("old " * 10, "mid " * 10, "new " * 10)
        # Each line is "User: " or "Assistant: " plus 40 chars; two rows fit, three do not.
        budget = len(scm.replay_line(rows[2])) + 2 + len(scm.replay_line(rows[1]))
        split = scm.split_tail(rows, budget_chars=budget)
        assert [r["content"] for r in split.dropped] == [rows[0]["content"]]
        assert [r["content"] for r in split.tail] == [rows[1]["content"], rows[2]["content"]]

    def test_one_character_short_drops_the_second_newest_row(self):
        rows = _rows("old " * 10, "mid " * 10, "new " * 10)
        budget = len(scm.replay_line(rows[2])) + 2 + len(scm.replay_line(rows[1])) - 1
        split = scm.split_tail(rows, budget_chars=budget)
        assert [r["content"] for r in split.tail] == [rows[2]["content"]]
        assert len(split.dropped) == 2

    def test_newest_row_is_always_kept(self):
        rows = _rows("x" * 500)
        split = scm.split_tail(rows, budget_chars=1)
        assert split.dropped == ()
        assert len(split.tail) == 1


class TestSplitTailMatchesReplay:
    """The tail this module computes is the tail the successor is seeded with."""

    @pytest.mark.parametrize("window", [200_000, None])
    def test_tail_rows_are_exactly_the_replay_rows(self, window):
        from kiro_crew.context import build_session_replay
        from kiro_crew.context_budget import replay_budget_chars

        class _Log:
            def __init__(self, rows):
                self._rows = rows

            def read_messages_chained(self, key):
                return list(self._rows)

        # Rows sized so the budget (16K chars at 200K, 80K at the reference)
        # cuts somewhere in the middle of the list.
        rows = _rows(*[f"row {i} " + ("x" * 3_000) for i in range(60)])
        replay = build_session_replay(_Log(rows), "k", model_window=window)
        assert replay is not None
        split = scm.split_tail(rows, budget_chars=replay_budget_chars(window))
        assert split.dropped, "fixture must overflow the budget to test the cut"
        first_kept = split.tail[0]["content"]
        last_dropped = split.dropped[-1]["content"]
        assert replay.startswith(scm.replay_line(split.tail[0])[: len(first_kept) + 6])
        assert first_kept in replay
        assert last_dropped not in replay


class TestElideFences:
    def test_short_fences_are_kept(self):
        text = "see ```py\nx = 1\n``` done"
        assert scm.elide_fences(text, min_chars=2_000) == text

    def test_large_fence_becomes_a_token_placeholder(self):
        block = "```\n" + ("y" * 4_000) + "\n```"
        out = scm.elide_fences(f"before {block} after", min_chars=2_000)
        assert out == f"before [Output elided - {scm.estimate_tokens(len(block))} tokens] after"

    def test_unterminated_fence_runs_to_the_end_of_the_row(self):
        out = scm.elide_fences("log:\n```\n" + ("z" * 3_000), min_chars=2_000)
        assert out.startswith("log:\n[Output elided - ") and "zzz" not in out

    def test_each_large_fence_is_replaced(self):
        big = "```\n" + ("q" * 2_500) + "\n```"
        out = scm.elide_fences(f"{big}\n{big}\n{big}", min_chars=2_000)
        assert out.count("[Output elided - ") == 3 and "qqq" not in out


class TestShakeElide:
    def test_empty_slice_is_an_empty_digest(self):
        digest = scm.shake_elide([], budget_chars=1_000)
        assert digest.text == "" and digest.rows_in == 0 and digest.through_ts is None

    def test_digest_within_budget_keeps_every_row_in_order(self):
        rows = _rows("first", "second", "third")
        digest = scm.shake_elide(rows, budget_chars=10_000)
        assert digest.text == "User: first\n\nAssistant: second\n\nUser: third"
        assert digest.rows_in == 3
        assert "[Output elided" not in digest.text and "left out of this digest" not in digest.text
        assert digest.through_ts == rows[-1]["ts"]

    def test_large_fences_are_elided(self):
        block = "```\n" + ("w" * 5_000) + "\n```"
        rows = _rows("run it", f"here:\n{block}")
        digest = scm.shake_elide(rows, budget_chars=10_000)
        assert digest.text.count("[Output elided - ") == 1
        assert "wwww" not in digest.text
        assert len(digest.text) < len("run it") + len(f"here:\n{block}")

    def test_overflow_keeps_both_chronological_ends_and_names_the_gap(self):
        rows = _rows(*[f"row{i:02d} " + ("m" * 200) for i in range(40)])
        budget = 2_000
        digest = scm.shake_elide(rows, budget_chars=budget)
        assert len(digest.text) <= budget
        assert digest.text.startswith("User: row00 ")
        assert digest.text.endswith("m" * 200)
        assert rows[-1]["content"] in digest.text
        gap = re.search(
            r"\[\.\.\. (\d+) rows between these left out of this digest \.\.\.\]", digest.text
        )
        assert gap is not None and int(gap.group(1)) > 0
        # The middle is what goes: the row right after the head is not carried.
        head_rows = digest.text.split("\n\n[... ")[0].count("\n\n") + 1
        assert rows[head_rows]["content"] not in digest.text

    def test_a_budget_below_one_row_carries_the_newest_row_truncated(self):
        rows = _rows("older " * 50, "newest " * 50)
        digest = scm.shake_elide(rows, budget_chars=40)
        assert len(digest.text) <= 40
        assert digest.text.startswith("Assistant: newest")
        assert digest.text.endswith("…[truncated]")
        assert "left out of this digest" not in digest.text, "one row carried, no gap line"

    def test_rows_without_timestamps_report_no_coverage_mark(self):
        digest = scm.shake_elide([{"role": "user", "content": "hi"}], budget_chars=100)
        assert digest.through_ts is None

    def test_an_earlier_seed_row_is_carried_first_and_its_rows_counted(self):
        prior = {
            "role": scm.SEED_ROLE,
            "content": "User: first goal\n\nAssistant: [Output elided - 9 tokens]",
            "ts": "2026-09-14T23:59:00+00:00",
            "meta": {"kind": scm.SEED_META_KIND, "method": "shake", "dropped_rows": 120},
        }
        rows = [prior, *_rows("a", "b", "c", "d", "e")]
        digest = scm.shake_elide(rows, budget_chars=10_000)
        assert digest.rows_in == 125, "120 rows the seed stood in for + 5 conversation rows"
        assert digest.text.startswith(
            "[Digest carried from an earlier compaction, standing in for 120 rows]\n\n"
            "User: first goal\n\nAssistant: [Output elided - 9 tokens]\n\n"
            "User: a\n\n"
        )
        assert digest.through_row == scm.row_fingerprint(rows[-1])

    def test_a_large_earlier_digest_is_clipped_to_its_share_never_omitted(self):
        budget = 8_000
        prior = {
            "role": scm.SEED_ROLE,
            "content": "User: first goal " + ("g" * budget),
            "meta": {"dropped_rows": 40},
        }
        rows = [prior, *_rows(*("row%02d " % i + "x" * 500 for i in range(30)))]
        digest = scm.shake_elide(rows, budget_chars=budget)
        head, carried, *rest = digest.text.split("\n\n")
        assert head == "[Digest carried from an earlier compaction, standing in for 40 rows]"
        assert carried.startswith("User: first goal ") and carried.endswith("…[truncated]")
        assert len(head) + 2 + len(carried) <= budget // scm._CARRIED_DIGEST_DIVISOR
        assert (
            rest and rest[-1].startswith("Assistant: row29") or rest[-1].startswith("User: row29")
        ), "the newest conversation row still digests after the carried block"
        assert len(digest.text) <= budget + 2
        assert digest.rows_in == 70

    def test_only_an_earlier_seed_dropped_is_carried_alone(self):
        prior = {"role": scm.SEED_ROLE, "content": "User: kept", "meta": {"dropped_rows": 3}}
        digest = scm.shake_elide([prior], budget_chars=1_000)
        assert digest.text == (
            "[Digest carried from an earlier compaction, standing in for 3 rows]\n\nUser: kept"
        )
        assert digest.rows_in == 3
        assert digest.through_row == scm.row_fingerprint(prior)
