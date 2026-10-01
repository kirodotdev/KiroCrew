"""Keyword-only lesson ranking: rare words decide, long rows do not win on length.

Startup renders lessons with no query vector, so the keyword score is the whole
ranking there. A capped overlap count would tie every rule sharing ten tokens with
a long first message, and the stable sort would then return newest-first. These
cases pin the scorer used instead: rarity-weighted overlap divided by the square
root of the row's number of distinct words.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew import vector_memory
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import lessons, text_scoring

# Every word here is a stop word to write_lesson's dedup (or two letters long),
# so rows sharing all of it are still stored as separate lessons, while each one
# shares well over ten ranking tokens with a request written in the same words.
SKELETON = (
    "Always use the {a} for the {b} and never the {c}: it is not in that {d} "
    "with this, so it should be on {e} or must be of {f}."
)
TERM = "flywheel"


def prose(tag: str) -> str:
    """One ordinary rule in the shared skeleton, with content words unique to *tag*."""
    return SKELETON.format(**{slot: f"{slot}{tag}word" for slot in "abcdef"})


def long_request(term: str = TERM) -> str:
    """A first message of 1,500+ characters: the skeleton's words, then *term* once."""
    filler = (
        "It is not that the plan should be on hold, and it must be in this shape for "
        "the team, so use it with care or not at all; never the same way twice. "
    )
    text = filler * 10 + f"The {term} is the part I need help with."
    assert len(text) >= 1_500
    return text


def shown(block: str) -> list[str]:
    """The lesson bodies rendered in *block*, in order."""
    return [line[2:] for line in block.splitlines() if line.startswith("- ")]


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[VectorMemoryStore]:
    memory = VectorMemoryStore(db_path=tmp_path / "mem.db")
    memory.init()
    # Distinct write instants, so newest-first is a defined order to beat.
    ticks = iter(f"2026-01-01T00:00:00.{tick:06d}+00:00" for tick in range(1, 1000))
    monkeypatch.setattr(vector_memory, "_now_iso", lambda: next(ticks))
    # Startup has no query vector; pin that no embedder is consulted either way.
    memory.embed_fn = None
    yield memory
    memory.close()


def render_startup(store: VectorMemoryStore, query: str, budget: int) -> str:
    return store.get_lessons_context(
        query,
        background=True,
        hard_cap=99_000,
        directive_budget=budget,
        experience_budget=1_500,
    )


class TestRareTermSurvivesTheBudget:
    def test_an_older_on_topic_rule_is_kept_over_forty_newer_ones(
        self, store: VectorMemoryStore
    ) -> None:
        """A long request shares ten-plus tokens with every row; the rare one decides."""
        target = SKELETON.format(
            a=TERM, b="bearing", c="housing", d="gearbox", e="shaft", f="coupling"
        )
        store.write_lesson(target)
        for index in range(40):
            store.write_lesson(prose(f"n{index:02d}"))
        assert len(store.get_lessons()) == 41, "dedup merged fixture rows"

        # Room for about ten rows, far fewer than the 41 stored.
        block = render_startup(store, long_request(), budget=1_600)

        assert 5 <= len(shown(block)) <= 15
        assert "omitted" in block
        assert shown(block)[0] == target

    def test_a_request_matching_no_row_keeps_newest_first(self, store: VectorMemoryStore) -> None:
        store.write_lesson("Rotate the signing certificate every ninety days")
        store.write_lesson("Pin container images by digest rather than tag")

        block = render_startup(store, "unrelated zebra xylophone", budget=7_000)

        assert shown(block) == [
            "Pin container images by digest rather than tag",
            "Rotate the signing certificate every ninety days",
        ]


class TestLengthDoesNotBuyRank:
    def test_a_focused_row_outranks_a_long_one_with_incidental_overlap(
        self, store: VectorMemoryStore
    ) -> None:
        """Both carry the rare term; the long one adds only common words."""
        short = "Rotate the flywheel bearings every quarter in the depot."
        long = (
            "Always calibrate the flywheel gauge before the audit: it is not in that "
            "cabinet with this crate, so it should be on the trolley or must be of "
            "the certified batch."
        )
        assert 2.5 <= len(long) / len(short) <= 3.5
        store.write_lesson(short)
        # Common words need a population to be common in: ordinary rules that
        # share the request's function words and not its rare term.
        for index in range(20):
            store.write_lesson(prose(f"f{index:02d}"))
        store.write_lesson(long)
        assert len(store.get_lessons()) == 22, "dedup merged fixture rows"

        order = shown(render_startup(store, long_request(), budget=20_000))

        assert order.index(short) < order.index(long)
        # Both carry the rare term, so both still lead every row without it.
        assert set(order[:2]) == {short, long}

    def test_inflected_newer_row_keeps_recency_when_distinct_word_counts_match(
        self, store: VectorMemoryStore
    ) -> None:
        older = "Run the test before push near amber glacier quartz"
        newer = "Run the tests before pushing beside cobalt orchard nebula tests"
        assert len(set(re.findall(r"\w+", older.lower()))) == len(
            set(re.findall(r"\w+", newer.lower()))
        )
        store.write_lesson(older)
        store.write_lesson(newer)
        assert len(store.get_lessons()) == 2, "dedup merged fixture rows"

        order = shown(render_startup(store, "run tests", budget=7_000))

        assert order == [newer, older]


class TestStartupCost:
    def test_the_startup_render_never_calls_the_embedder(self, store: VectorMemoryStore) -> None:
        for index in range(5):
            store.write_lesson(prose(f"e{index:02d}"))
        store.embed_fn = Mock(side_effect=AssertionError("startup must not embed"))

        block = render_startup(store, long_request(), budget=7_000)

        assert shown(block)
        store.embed_fn.assert_not_called()

    def test_ranking_stems_only_the_request_once_rows_are_memoised(
        self, store: VectorMemoryStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Document frequency reads the memoised row token sets; no row is re-stemmed."""
        for index in range(12):
            store.write_lesson(prose(f"s{index:02d}"))
        request = long_request()
        render_startup(store, request, budget=7_000)  # warms the row-token memo

        calls: list[str] = []
        original = text_scoring._stem_one

        def counting(word: str) -> str:
            calls.append(word)
            return original(word)

        # The ranking reads two bindings of the stemmer: ``lessons._stem_one``
        # for the request's words, and ``text_scoring._stem_one`` for row
        # tokens through ``_stem_words``. Count at both, so a re-stemmed row
        # shows up as well as a request stemmed more than once.
        monkeypatch.setattr(lessons, "_stem_one", counting)
        monkeypatch.setattr(text_scoring, "_stem_one", counting)
        render_startup(store, request, budget=7_000)

        request_words = set(re.findall(r"\w+", request.lower()))
        assert sorted(calls) == sorted(request_words)


class TestInflectionDoesNotBuyRank:
    def test_surface_inflection_keeps_equal_stem_matches_in_recency_order(
        self, store: VectorMemoryStore
    ) -> None:
        older = "Rotate certificates using cobalt orchard nebula fixtures"
        newer = "Rotate certificate using amber glacier quartz fixtures"
        store.write_lesson(older)
        store.write_lesson(newer)
        assert len(store.get_lessons()) == 2, "dedup merged fixture rows"

        order = shown(render_startup(store, "please rotate the certificates", budget=7_000))

        assert order == [newer, older]
