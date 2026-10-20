"""Chinese dictionary words in session search (``history_search`` + ``history_words``).

The invariant: with a segmenter installed, each pure-Han query run is split into
dictionary words; each word is a required needle whose own characters must sit
together; a session holding the run as typed ranks above every session that holds
only its words; and the snippet lands on the best tier any message holds. With no
segmenter, or one that fails, the parse is exactly the character path, and a query in
any other script never reaches the segmenter.

Nearby inputs pinned here: a run that is one word (no verbatim needle), a run that
splits into single characters only (character path), a kana run, a run past the
length bound and a query past the run bound (never sent), a raising segmenter (backs
off, then retries), an answer that does not join back to its run (dropped), Korean
and English queries (unchanged).

The search tests use the real pinned dictionary (``rjieba``, a core dependency), so
they also pin the splits the feature relies on. The parse tests use a fixed fake so
they do not depend on the dictionary at all.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from kiro_crew import history_search, history_words, mcp_core
from kiro_crew._sqlite_compat import fts5_available
from kiro_crew.history import ConversationLog
from kiro_crew.history_index import SessionSearchIndex
from kiro_crew.history_search import SearchNeedle, needles_match_text, parse_search_query


class _Dictionary:
    """A fixed segmenter that records every batch it is asked to split."""

    def __init__(self, splits: dict[str, list[str]] | None = None) -> None:
        self.splits = splits or {}
        self.calls: list[list[str]] = []

    def __call__(self, runs: list[str]) -> list[list[str]]:
        self.calls.append(list(runs))
        return [self.splits.get(run, list(run)) for run in runs]


@pytest.fixture()
def fake():
    def install(splits: dict[str, list[str]] | None = None) -> _Dictionary:
        dictionary = _Dictionary(splits)
        history_search.register_cjk_segmenter(dictionary)
        return dictionary

    return install


@pytest.fixture()
def words():
    """The gateway's real segmenter: the pinned rjieba dictionary, loaded lazily."""
    history_search.register_cjk_segmenter(history_words.segment_runs)


def _character_path(query: str) -> tuple[list[SearchNeedle], str, bool]:
    """*query* parsed with no segmenter installed: today's character path."""
    history_search.reset_cjk_segmenter_for_tests()
    return parse_search_query(query)


def _write(tmp_path, key: str, *contents: str, title: str = "") -> None:
    """One session file, unescaped CJK, with an explicit neutral title.

    The title is explicit because ``list_sessions`` otherwise takes it from the
    first message, and a title hit carries ``_TITLE_BOOST``, which would mask the
    content ranking these tests are about.
    """
    lines = [json.dumps({"_type": "metadata", "title": title or f"session {key}"})]
    lines += [json.dumps({"role": "user", "content": c}, ensure_ascii=False) for c in contents]
    (tmp_path / f"{key}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _rows(log: ConversationLog, query: str) -> list[tuple[str, str]]:
    return [(row["key"], row.get("snippet", "")) for row in log.search_sessions(query)]


class TestParse:
    def test_a_han_run_splits_into_required_words_and_a_verbatim_run(self, fake):
        fake({"吉普力": ["吉普", "力"]})

        needles, phrase, floor = parse_search_query("吉普力")

        assert needles == [
            SearchNeedle("吉普", 1.0, True, word=True),
            SearchNeedle("力", history_search._CJK_CHAR_WEIGHT, True, word=True),
            SearchNeedle("吉普力", 0.0, False, verbatim=True),
        ]
        assert phrase == "吉普力"
        assert floor is False, "words carry their own adjacency; no bigram floor"

    def test_a_run_that_is_one_word_needs_no_verbatim_needle(self, fake):
        fake({"内存": ["内存"]})

        needles, _, floor = parse_search_query("内存")

        assert needles == [SearchNeedle("内存", 1.0, True, word=True)]
        assert floor is False

    def test_a_required_word_is_adjacency_evidence_beside_a_kana_run(self, fake):
        """A gated word of 2+ characters proves adjacency, so kana bigrams do not gate."""
        fake({"吉卜力": ["吉卜力"]})

        needles, _, floor = parse_search_query("吉卜力 テスト")

        assert needles_match_text(needles, "吉卜力のテ、ス、ト", floor)
        assert not needles_match_text(needles, "吉と卜と力のテスト", floor)
        assert floor is False

    def test_a_word_cut_by_the_token_cap_does_not_waive_the_floor(self, fake):
        """Only a word that survives the SEARCH_MAX_TOKENS cut gates, so only it proves."""
        fake({"吉卜力": ["吉卜力"]})
        ascii_terms = " ".join(f"t{i}x" for i in range(history_search.SEARCH_MAX_TOKENS))

        needles, _, floor = parse_search_query(f"{ascii_terms} 吉卜力 テスト")

        assert "吉卜力" not in [n.text for n in needles]
        assert floor is True

    def test_a_split_into_single_characters_keeps_the_character_path(self, fake):
        """Single characters add nothing to the character gate, so the run keeps it."""
        expected = _character_path("吉卜力")
        fake({"吉卜力": ["吉", "卜", "力"]})

        assert parse_search_query("吉卜力") == expected

    @pytest.mark.parametrize(
        "query",
        ["テスト内存", "ひらがな", "한글 검색", "deploy timeout", "kirocrew 部"],
    )
    def test_other_scripts_never_reach_the_segmenter(self, fake, query):
        expected = _character_path(query)
        dictionary = fake()

        assert parse_search_query(query) == expected
        assert dictionary.calls == []

    def test_runs_past_the_bounds_are_not_sent(self, fake):
        long_run = "内" * (history_search.CJK_SEGMENT_MAX_RUN_CHARS + 1)
        many = " ".join(
            f"内{chr(0x4E00 + i)}" for i in range(history_search.CJK_SEGMENT_MAX_RUNS + 1)
        )
        dictionary = fake()

        parse_search_query(long_run)
        parse_search_query(many)

        assert dictionary.calls == [many.split()[: history_search.CJK_SEGMENT_MAX_RUNS]]

    def test_an_answer_that_does_not_join_back_to_its_run_is_dropped(self, fake):
        expected = _character_path("吉普力")
        fake({"吉普力": ["吉普", "普力"]})

        assert parse_search_query("吉普力") == expected

    def test_a_failing_segmenter_backs_off_then_retries(self, monkeypatch):
        calls: list[list[str]] = []

        def broken(runs: list[str]) -> list[list[str]]:
            calls.append(list(runs))
            raise ConnectionRefusedError("gateway down")

        expected = _character_path("内存泄漏")
        history_search.register_cjk_segmenter(broken)
        started = time.monotonic()

        assert parse_search_query("内存泄漏") == expected
        assert parse_search_query("内存泄漏") == expected
        assert len(calls) == 1, "a failed segmenter is not asked again inside the back-off"
        retry_at = history_search._cjk_slot.retry_at
        assert retry_at >= started + history_search._CJK_SEGMENTER_BACKOFF_SECS
        # The window ends: the next query asks again.
        monkeypatch.setattr(history_search._cjk_slot, "retry_at", 0.0)
        assert parse_search_query("内存泄漏") == expected
        assert len(calls) == 2

    @pytest.mark.parametrize("answer", [None, "吉普 力", [["吉普", "力"], ["多"]], {"a": 1}])
    def test_a_malformed_answer_keeps_the_character_path_and_backs_off(self, answer):
        expected = _character_path("吉普力")
        history_search.register_cjk_segmenter(lambda runs: answer)

        assert parse_search_query("吉普力") == expected
        assert history_search._cjk_slot.retry_at > 0.0

    def test_answers_are_memoized_per_run(self, fake):
        dictionary = fake({"内存泄漏": ["内存", "泄漏"]})

        parse_search_query("内存泄漏")
        parse_search_query("内存泄漏 修复")
        history_search.snippet_needles("内存泄漏")

        assert dictionary.calls == [["内存泄漏"], ["修复"]]

    def test_with_no_segmenter_the_parse_is_the_character_path(self):
        needles, _, floor = _character_path("内存泄漏")

        assert [n.text for n in needles if n.required] == ["内", "存", "泄", "漏"]
        assert [n.text for n in needles if not n.required] == ["内存", "存泄", "泄漏"]
        assert not any(n.word or n.verbatim for n in needles)
        assert floor is True

    def test_a_missing_dictionary_is_latched_and_keeps_the_character_path(self, monkeypatch):
        expected = _character_path("内存泄漏")
        monkeypatch.setattr(history_words, "_cut", None)
        monkeypatch.setattr(history_words, "_unavailable", False)
        monkeypatch.setitem(sys.modules, "rjieba", None)

        assert history_words.segment_runs(["内存泄漏"]) is None
        monkeypatch.delitem(sys.modules, "rjieba")
        assert history_words.segment_runs(["内存泄漏"]) is None, "one failed import is final"
        history_search.register_cjk_segmenter(history_words.segment_runs)
        assert parse_search_query("内存泄漏") == expected

    def test_the_pinned_dictionary_gives_the_splits_search_relies_on(self):
        assert history_words.segment_runs(["修复内存泄漏的问题", "吉普力", "吉卜力"]) == [
            ["修复", "内存", "泄漏", "的", "问题"],
            ["吉普", "力"],
            ["吉卜力"],
        ]


class TestSearch:
    def test_the_snippet_lands_on_the_phrase_not_a_lone_character(self, tmp_path, words):
        """The reported bug: an early "普通" took the snippet from a later "吉普力"."""
        _write(tmp_path, "hit", "这个是普通的配置", "后来我们讨论了吉普力的方案")
        log = ConversationLog(base_dir=tmp_path)

        rows = _rows(log, "吉普力")

        assert rows[0] == ("hit", "后来我们讨论了吉普力的方案")
        messages = log.read_messages("hit")
        assert "<<<吉普力>>>" in mcp_core._extract_history_snippet(messages, "吉普力")

    def test_words_apart_still_match(self, tmp_path, words):
        _write(tmp_path, "hit", "我们修复了一个严重的问题", "最后定位到内存泄漏")
        _write(tmp_path, "miss", "完全无关的话题")
        log = ConversationLog(base_dir=tmp_path)

        assert [key for key, _ in _rows(log, "修复内存泄漏")] == ["hit"]

    def test_a_word_split_across_the_text_is_not_a_match(self, tmp_path, words):
        """Every character present, "泄漏" never adjacent: the character path took it."""
        _write(tmp_path, "scatter", "修复了内存问题", "泄压阀和漏水的记录")
        log = ConversationLog(base_dir=tmp_path)

        assert _rows(log, "修复内存泄漏") == []
        history_search.reset_cjk_segmenter_for_tests()
        assert [key for key, _ in _rows(log, "修复内存泄漏")] == ["scatter"]

    def test_a_word_beside_scattered_kana_matches_as_it_did_without_words(self, tmp_path, words):
        """The word is adjacency evidence: scattered kana do not drop the session."""
        _write(tmp_path, "hit", "吉卜力的作品", "テ、ス、ト")
        _write(tmp_path, "miss", "完全无关的话题", "テスト")
        log = ConversationLog(base_dir=tmp_path)

        with_words = [key for key, _ in _rows(log, "吉卜力 テスト")]
        history_search.reset_cjk_segmenter_for_tests()
        without_words = [key for key, _ in _rows(log, "吉卜力 テスト")]

        assert without_words == ["hit"]
        assert with_words == without_words

    def test_a_verbatim_session_ranks_above_one_with_more_word_hits(self, tmp_path, words):
        """The run as typed outranks its words apart, however often they repeat."""
        _write(tmp_path, "verbatim", "我们讨论了吉普力")
        _write(tmp_path, "scattered", "吉普和努力" * 20)
        log = ConversationLog(base_dir=tmp_path)

        assert [key for key, _ in _rows(log, "吉普力")] == ["verbatim", "scattered"]

    def test_without_a_dictionary_search_is_todays(self, tmp_path):
        """No segmenter: the early "普通" still wins the snippet, as it always did."""
        _write(tmp_path, "hit", "这个是普通的配置", "后来我们讨论了吉普力的方案")
        log = ConversationLog(base_dir=tmp_path)

        assert _rows(log, "吉普力") == [("hit", "这个是普通的配置")]

    @pytest.mark.parametrize(
        ("query", "sessions"),
        [
            ("テスト内存", {"a": ["テストの内存を確認", "別の話"], "b": ["内存だけ", "テスト"]}),
            ("ping pong", {"a": ["ping alone", "the ping pong bench"], "b": ["pong ping"]}),
            ("한글 검색", {"a": ["한글 문서", "검색 결과"], "b": ["검색"]}),
        ],
    )
    def test_other_scripts_search_the_same_with_a_dictionary(
        self, tmp_path, words, query, sessions
    ):
        for key, contents in sessions.items():
            _write(tmp_path, key, *contents)
        log = ConversationLog(base_dir=tmp_path)
        with_words = _rows(log, query)
        history_search.reset_cjk_segmenter_for_tests()

        assert with_words == _rows(log, query)


class TestSnippetTiers:
    def test_a_query_without_words_is_one_tier_in_snippet_needles_order(self):
        for query in ("beta alphabet gamma", "内存泄漏", "#4411 deploy"):
            tiers = history_search.snippet_needle_tiers(query)
            assert tiers == [history_search.snippet_needles(query)]

    def test_a_query_with_words_tiers_phrase_then_words_then_characters(self, fake):
        fake({"飞书机器人": ["飞", "书", "机器人"]})

        assert history_search.snippet_needle_tiers("飞书机器人") == [
            ["飞书机器人"],
            ["机器人"],
            ["飞", "书"],
        ]

    def test_a_one_tier_query_reads_only_to_its_first_match(self):
        def texts():
            yield "nothing here"
            yield "the ping pong bench"
            raise AssertionError("read past the first match")

        tiers = history_search.snippet_needle_tiers("ping pong")

        assert history_search.tiered_first_hit(texts(), tiers) == (
            "the ping pong bench",
            4,
            "ping pong",
        )

    def test_a_later_better_tier_wins_and_reading_stops_there(self):
        def texts():
            yield "only 力 here"
            yield "吉普 is here"
            yield "吉普力 is here"
            raise AssertionError("read past a tier-0 match")

        tiers = [["吉普力"], ["吉普"], ["力"]]

        assert history_search.tiered_first_hit(texts(), tiers) == ("吉普力 is here", 0, "吉普力")

    def test_a_read_error_keeps_the_best_hit_so_far(self):
        def texts():
            yield "吉普 first"
            raise OSError("transcript vanished")

        assert history_search.tiered_first_hit(texts(), [["吉普力"], ["吉普"]]) == (
            "吉普 first",
            0,
            "吉普",
        )


@pytest.mark.skipif(not fts5_available(), reason="SQLite built without FTS5")
class TestIndex:
    @pytest.fixture(autouse=True)
    def _no_quiet_window(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(history_search, "_INDEX_QUIET_WINDOW_SECS", 0.0)

    def test_a_two_character_word_looks_up_both_characters(self, fake):
        fake({"修复内存泄漏": ["修复", "内存", "泄漏"]})
        needles, _, _ = parse_search_query("修复内存泄漏")

        lookups = SessionSearchIndex._lookups(needles)

        assert lookups == [
            (0, "session_cjk", "修 复"),
            (1, "session_cjk", "内 存"),
            (2, "session_cjk", "泄 漏"),
        ]

    def test_a_three_character_word_is_a_trigram_lookup(self, fake):
        fake({"飞书机器人": ["飞", "书", "机器人"]})
        needles, _, _ = parse_search_query("飞书机器人")

        lookups = SessionSearchIndex._lookups(needles)

        assert lookups == [
            (0, "session_cjk", "飞"),
            (1, "session_cjk", "书"),
            (2, "session_fts", '("机器人")'),
        ]

    def test_indexed_search_equals_the_scan_and_never_reads_a_session_missing_a_word(
        self, tmp_path, words, monkeypatch, opened
    ):
        corpus = {
            "hit": ["我们修复了一个严重的问题", "最后定位到内存泄漏"],
            "verbatim": ["修复内存泄漏的问题已经合入"],
            "no-leak": ["修复了内存问题", "完全无关"],
        }
        logs = {}
        for name in ("scan", "indexed"):
            base = tmp_path / name
            base.mkdir()
            for key, contents in corpus.items():
                _write(base, key, *contents)
            logs[name] = ConversationLog(base_dir=base)
            opened(logs[name]._catalog_projection.search_index)
        logs["scan"]._catalog_projection.search_index.available = False
        assert logs["indexed"]._catalog_projection.backfill_index(budget_secs=30)["remaining"] == 0
        read: list[str] = []
        original = SessionSearchIndex.document

        def counted(self, rowid, session_key, digest=None):
            read.append(session_key)
            return original(self, rowid, session_key, digest)

        monkeypatch.setattr(SessionSearchIndex, "document", counted)

        indexed_rows = _rows(logs["indexed"], "修复内存泄漏")

        assert indexed_rows == _rows(logs["scan"], "修复内存泄漏")
        assert [key for key, _ in indexed_rows] == ["verbatim", "hit"]
        assert {"hit", "verbatim"} <= set(read), "the vouched rows were scored from the index"
        # "no-leak" holds no 泄: the character column proved it, so its text is
        # never read.
        assert "no-leak" not in read


def test_an_old_verbatim_session_outranks_a_new_scattered_one(tmp_path, words):
    """The verbatim tier outranks recency too, not only word frequency."""
    _write(tmp_path, "old", "我们讨论了吉普力")
    _write(tmp_path, "new", "吉普和努力" * 10)
    old = time.time() - 365 * 86400
    os.utime(tmp_path / "old.jsonl", (old, old))
    log = ConversationLog(base_dir=tmp_path)

    assert [key for key, _ in _rows(log, "吉普力")] == ["old", "new"]
