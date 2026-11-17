"""Relevance ranking caches each row's tokens and returns the pre-cache order.

``get_preferences_context`` and the lesson tiers rank every row on every call with
``order_by_request_relevance``. A row's tokens only change when its text does, so
``_relevance_tokens`` is cached per text. These tests pin two things: a repeated
call tokenizes nothing, and the ranking is identical to the old per-call code,
which is copied below as the oracle.
"""

from __future__ import annotations

import random

import pytest

from kiro_crew import _sqlite_compat, lesson_validation
from kiro_crew._sqlite_compat import _CJK_RANGES, is_cjk_char, script_runs
from kiro_crew.lesson_validation import _relevance_tokens, order_by_request_relevance

_MIN = lesson_validation._RELEVANCE_MIN_TOKEN


def _old_is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _old_split_cjk_word(word: str) -> tuple[list[str], set[str]]:
    if not any(_old_is_cjk_char(ch) for ch in word):
        return [word], set()
    pieces: list[str] = []
    pairs: set[str] = set()
    for run, cjk in script_runs(word):
        if cjk:
            pairs.update(run[i : i + 2] for i in range(len(run) - 1))
        else:
            pieces.append(run)
    return pieces, pairs


def _old_relevance_tokens(text: str) -> frozenset[str]:
    words, word = [], []
    for char in text.lower():
        if char.isalnum():
            word.append(char)
        else:
            words.append("".join(word))
            word = []
    words.append("".join(word))
    tokens: set[str] = set()
    for candidate in words:
        if len(candidate) >= _MIN:
            tokens.add(candidate)
        pieces, pairs = _old_split_cjk_word(candidate)
        tokens.update(piece for piece in pieces if len(piece) >= _MIN)
        tokens |= pairs
    return frozenset(tokens)


def _old_order(entries: list[tuple[object, str]], query_text: str) -> list[tuple[object, str]]:
    if not query_text.strip() or not entries:
        return entries
    wanted = _old_relevance_tokens(query_text)
    if not wanted:
        return entries
    return sorted(entries, key=lambda e: -len(wanted & _old_relevance_tokens(e[1])))


_WORDS = [
    "redis",
    "cache",
    "deploy",
    "Gateway",
    "ab",
    "检查",
    "缓存配置",
    "部署",
    "日本語テスト",
    "회의",
    "naïve",
    "a-b",
]


def _fixture(n: int = 300) -> list[tuple[object, str]]:
    rng = random.Random(35197)
    rows: list[tuple[object, str]] = []
    for i in range(n):
        parts = [rng.choice(_WORDS) for _ in range(rng.randint(0, 8))]
        if i % 7 == 0:
            parts.append("检查redis缓存")  # mixed CJK and Latin in one word
        rows.append((i, f"pref.k{i}: " + " ".join(parts)))
    rows.append((n, ""))
    rows.append((n + 1, "pref.dup: redis cache"))
    rows.append((n + 2, "pref.dup: redis cache"))  # exact tie with the row above
    return rows


_QUERIES = [
    "fix the redis cache deploy",
    "检查缓存 部署",
    "check 检查redis缓存 please",
    "日本語テスト gateway",
    "zz",  # no token reaches the minimum length
    "   ",
    "nothing here matches anything",
]


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    for name in ("_lru_row_tokens", "_lru_cjk_word_tokens"):
        getattr(getattr(lesson_validation, name, None), "cache_clear", lambda: None)()


@pytest.mark.parametrize("query", _QUERIES)
def test_ranking_is_identical_to_the_uncached_oracle(query: str) -> None:
    entries = _fixture()
    expected = _old_order(entries, query)
    assert order_by_request_relevance(entries, query) == expected
    assert order_by_request_relevance(entries, query) == expected  # warm cache, same answer
    for kept in (0, 1, 2, len(entries) // 2, len(entries) - 1, len(entries)):
        assert order_by_request_relevance(entries, query)[:kept] == expected[:kept]


def test_tokens_match_the_oracle_for_every_fixture_row() -> None:
    for _, text in _fixture():
        assert _relevance_tokens(text) == _old_relevance_tokens(text)
        assert lesson_validation._row_tokens(text) == _old_relevance_tokens(text)


def test_a_request_is_not_retained_by_the_cache() -> None:
    entries = _fixture(20)
    order_by_request_relevance(entries, "zzz")  # warms the row caches only
    rows = lesson_validation._lru_row_tokens.cache_info().currsize
    words = lesson_validation._lru_cjk_word_tokens.cache_info().currsize
    request = "pasted log secret12345 检查缓存配置密钥 redis"
    order_by_request_relevance(entries, request)
    lesson_validation.any_request_overlap(entries, request)
    assert lesson_validation._lru_row_tokens.cache_info().currsize == rows
    assert lesson_validation._lru_cjk_word_tokens.cache_info().currsize == words


def test_an_oversized_row_is_ranked_identically_and_not_retained() -> None:
    huge = "pref.big: redis " + "检查缓存 " * (lesson_validation._MAX_CACHED_ROW_CHARS // 4)
    long_word = "检查" * lesson_validation._MAX_CACHED_WORD_CHARS + "redis"
    entries = [*_fixture(20), (900, huge), (901, f"pref.w: {long_word} cache")]
    for query in ("redis cache", f"{long_word} 检查缓存"):
        assert order_by_request_relevance(entries, query) == _old_order(entries, query)
    rows = {text for _, text in entries if len(text) <= lesson_validation._MAX_CACHED_ROW_CHARS}
    assert huge not in rows
    assert lesson_validation._lru_row_tokens.cache_info().currsize == len(rows)
    before = lesson_validation._lru_cjk_word_tokens.cache_info().currsize
    lesson_validation._row_tokens(f"x {long_word}")
    assert lesson_validation._lru_cjk_word_tokens.cache_info().currsize == before


def test_repeated_call_does_not_retokenize(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = _fixture(50)
    calls = 0
    real = lesson_validation.split_cjk_word

    def counting(word: str) -> tuple[list[str], set[str]]:
        nonlocal calls
        calls += 1
        return real(word)

    monkeypatch.setattr(lesson_validation, "split_cjk_word", counting)
    order_by_request_relevance(entries, "redis cache")
    assert calls > 0
    calls = 0
    for _ in range(3):
        order_by_request_relevance(entries, "redis cache")
    assert calls == 0


def test_is_cjk_char_matches_the_range_table_at_every_boundary() -> None:
    points = {0, 0x7F, _sqlite_compat._CJK_MIN - 1}
    for lo, hi in _CJK_RANGES:
        points |= {lo - 1, lo, lo + 1, hi - 1, hi, hi + 1}
    for cp in sorted(p for p in points if 0 <= p <= 0x10FFFF):
        ch = chr(cp)
        assert is_cjk_char(ch) == _old_is_cjk_char(ch), hex(cp)
    assert is_cjk_char("检") and is_cjk_char("あ") and not is_cjk_char("a")
    assert not is_cjk_char("\ud68c")  # Hangul stays outside the set
