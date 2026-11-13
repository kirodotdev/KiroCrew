"""Lesson tokenizers and unspaced Chinese text.

``lesson_validation._relevance_tokens`` (JSONL ordering and findings admission)
returns a superset of the plain word split: each whole word, plus the non-CJK
pieces and the two-character pairs of each unspaced CJK run, so a Chinese request
shares a token with a lesson about the same topic.
The vector store's read paths (findings admission, ranking and per-turn lessons)
add the same pieces and pairs on both the request and the lesson side.
``vector_memory_runtime.lessons.lesson_keywords`` (the lesson dedup) keeps an
unspaced CJK run whole. These tests pin the tokenizers on CJK and Latin text.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import lesson_validation
from kiro_crew.lesson_validation import (
    _relevance_tokens,
    any_request_overlap,
    order_by_request_relevance,
)
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime.lessons import lesson_keywords


def test_pure_chinese_request_shares_a_pair_with_its_lesson() -> None:
    query = _relevance_tokens("处理一下启动预热的问题")
    lesson = _relevance_tokens("预热必须覆盖真实调用路径")
    assert query == {
        "处理一下启动预热的问题",
        "处理",
        "理一",
        "一下",
        "下启",
        "启动",
        "动预",
        "预热",
        "热的",
        "的问",
        "问题",
    }
    assert query & lesson == {"预热"}


def test_mixed_chinese_and_latin_splits_at_the_script_boundary() -> None:
    query = _relevance_tokens("检查Redis缓存")
    lesson = _relevance_tokens("Redis 缓存应按实际命中核对")
    assert query == {"检查redis缓存", "检查", "redis", "缓存"}
    assert query & lesson == {"redis", "缓存"}


def test_relevance_tokens_keep_the_whole_cjk_word() -> None:
    text = "部署前必须运行测试"
    assert text in _relevance_tokens(text)


@pytest.mark.parametrize(
    "tokenize, expected",
    [
        pytest.param(
            _relevance_tokens,
            {"redis", "cache", "warmup", "should", "complete", "before", "db2"},
            id="relevance_tokens",
        ),
        pytest.param(
            lesson_keywords,
            {"redis", "cache", "warmup", "complete", "before", "v2_db2"},
            id="lesson_keywords",
        ),
    ],
)
def test_english_text_tokenizes_as_a_plain_word_split(tokenize, expected) -> None:
    # Stop words, two-letter words and punctuation are dropped; the underscore
    # is a word character only for ``\W``.
    assert tokenize("redis cache warmup should complete before it, v2_db2!") == expected


def test_lesson_keywords_keep_an_unspaced_cjk_run_whole() -> None:
    # The lesson dedup retires a row on this overlap, so CJK pairs must not reach it.
    assert lesson_keywords("部署前必须运行测试") == {"部署前必须运行测试"}


def test_a_word_with_no_cjk_comes_back_whole_with_no_pairs() -> None:
    assert lesson_validation.split_cjk_word("warmup") == (["warmup"], set())


def test_a_one_character_cjk_run_yields_no_pair() -> None:
    assert lesson_validation.split_cjk_word("用redis") == (["redis"], set())


def test_japanese_kana_runs_also_yield_pairs() -> None:
    assert lesson_validation.split_cjk_word("キャッシュ")[1] == {"キャ", "ャッ", "ッシ", "シュ"}


def test_a_chinese_request_ranks_and_admits_its_lesson() -> None:
    entries = [
        ("newer", "Always run the linter before pushing"),
        ("older", "预热必须覆盖真实调用路径"),
    ]
    request = "处理一下启动预热的问题"
    assert [key for key, _ in order_by_request_relevance(entries, request)] == ["older", "newer"]
    assert any_request_overlap(entries, request)


REDIS_LESSON = "检查Redis缓存之前先备份数据库快照"


@pytest.fixture
def vector_store(tmp_path: Path) -> Iterator[VectorMemoryStore]:
    memory = VectorMemoryStore(db_path=tmp_path / "memory.db")
    memory.init()
    memory.embed_fn = None
    yield memory
    memory.close()


def test_vector_findings_admission_matches_an_unspaced_chinese_request(vector_store) -> None:
    entries = [({"key": "lesson.redis"}, REDIS_LESSON)]
    assert vector_store._any_lesson_overlap(entries, "我们要怎么检查Redis缓存？")
    assert not vector_store._any_lesson_overlap(entries, "Rotate the deploy keys")


def test_vector_ranking_puts_the_chinese_lesson_first(vector_store) -> None:
    entries = [
        ({"key": "newer"}, "Always run the linter before pushing"),
        ({"key": "older"}, "预热必须覆盖真实调用路径"),
    ]
    ranked = vector_store._rank_lessons(entries, "处理一下启动预热的问题")
    assert [row["key"] for row, _ in ranked] == ["older", "newer"]


@pytest.mark.parametrize("message", ["我们要怎么检查Redis缓存？", "检查 Redis 缓存"])
def test_a_chinese_follow_up_message_earns_its_lesson(vector_store, message) -> None:
    vector_store.write_lesson(REDIS_LESSON)
    vector_store.write_lesson("预热必须覆盖真实调用路径")
    assert len(vector_store.get_lessons()) == 2, "dedup merged fixture rows"
    chosen = vector_store.turn_lessons(
        message, shown=lambda _text: False, max_rows=3, max_chars=2_000
    )
    assert [text for _, text in chosen] == [REDIS_LESSON]
