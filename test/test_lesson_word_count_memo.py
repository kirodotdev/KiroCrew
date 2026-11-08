"""Lesson ranking reads each row's distinct-word count from the row-token memo."""

from __future__ import annotations

import re

import pytest

from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory_runtime import lessons as _lessons
from kiro_crew.vector_memory_runtime import text_scoring as ts

TEXTS = [
    "never use rm -rf; move the directory into scratch instead",
    "Always reply in Chinese, Chinese, CHINESE",
    "with_underscores and digits 42 42 42",
    "",
    "a",
    "running gateways rebuild caches, rebuilding gateway caches",
]
QUERIES = [
    {"chinese", "reply"},
    {"scratch", "rm", "move", "absent"},
    ts._stem_words({"running", "gateway", "rebuild"}),
    {"42", "digits"},
]


@pytest.fixture(autouse=True)
def empty_memo():
    vm._row_stem_tokens.cache_clear()
    yield
    vm._row_stem_tokens.cache_clear()


@pytest.mark.parametrize("text", TEXTS)
def test_the_memo_counts_what_a_fresh_scan_counts(text: str) -> None:
    lowered = text.lower()
    tokens = vm._row_stem_tokens(lowered)
    assert tokens == ts._row_stem_tokens_uncached(lowered)
    assert ts._row_distinct_words(tokens, lowered) == len(set(re.findall(r"\w+", lowered)))


def test_a_changed_lesson_gets_its_own_count() -> None:
    before = vm._row_stem_tokens("one two three")
    after = vm._row_stem_tokens("one two three four five")
    assert ts._row_distinct_words(before, "one two three") == 3
    assert ts._row_distinct_words(after, "one two three four five") == 5


def test_an_unmemoized_scan_still_counts() -> None:
    text = "alpha beta beta gamma"
    assert ts._row_distinct_words(ts._row_stem_tokens_uncached(text), text) == 3


@pytest.mark.parametrize("query", QUERIES, ids=range(len(QUERIES)))
def test_scores_match_the_unmemoized_scan(query: set[str]) -> None:
    entries = [({}, text) for text in TEXTS]
    memo = _lessons._lexical_lesson_scores(entries, query, vm._row_stem_tokens)
    plain = _lessons._lexical_lesson_scores(entries, query, ts._row_stem_tokens_uncached)
    assert memo == plain


def test_a_warm_ranking_does_not_rescan_the_rows(monkeypatch) -> None:
    """Once the memo holds the rows, ranking them again runs no ``re.findall``."""
    entries = [({}, text) for text in TEXTS]
    _lessons._lexical_lesson_scores(entries, QUERIES[0], vm._row_stem_tokens)
    calls: list[str] = []
    real = re.findall

    def counting(pattern, string, *args, **kwargs):
        calls.append(string)
        return real(pattern, string, *args, **kwargs)

    monkeypatch.setattr(re, "findall", counting)
    _lessons._lexical_lesson_scores(entries, QUERIES[1], vm._row_stem_tokens)
    assert calls == []


def test_a_row_with_cjk_parts_counts_its_words_and_parts() -> None:
    """A CJK row's length is its ``\\w+`` words plus its CJK parts, memo or not."""
    text = "回复用中文 reply in chinese"
    lowered = text.lower()
    parts = _lessons.cjk_parts(lowered)
    assert parts, "the fixture must carry CJK parts"
    expected = len(set(re.findall(r"\w+", lowered)) | parts)
    entries = [({}, text)]
    for row_tokens in (vm._row_stem_tokens, ts._row_stem_tokens_uncached):
        _, sizes, _ = _lessons._shared_lesson_stems(entries, {"reply"}, row_tokens)
        assert sizes == [expected]
