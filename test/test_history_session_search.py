"""Tests for content search over conversation history.

Covers ``ConversationLog.search_sessions``: matching, ranking with its title
boost, length normalisation and bounded recency weighting, snippets, and the
query parse shared by the matcher and the snippet builders (``history_search``):
needle bounds, CJK segmentation, the single-text gate, and the saturation the parse
applies to short reference needles. The reference spellings themselves (digit
boundaries, ranking hints, the expansion budget and the provider seam) are in
``test_history_forge_refs.py``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from kiro_crew import history
from kiro_crew.history import ConversationLog


class TestSearchSessions:
    """Tests for content search over session JSONL files."""

    def test_matches_content(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "discussed TICKET-1234567 today")
        log.append("beta", "user", "unrelated chat")
        results = log.search_sessions("TICKET-1234567")
        keys = [s["key"] for s in results]
        assert keys == ["alpha"]

    def test_case_insensitive(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "KMS access denied exception")
        results = log.search_sessions("kms ACCESS")
        assert [s["key"] for s in results] == ["alpha"]

    def test_empty_query_returns_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "anything")
        assert log.search_sessions("") == []

    def test_respects_limit(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        for i in range(5):
            log.append(f"s{i}", "user", "match")
        results = log.search_sessions("match", limit=2)
        assert len(results) == 2

    def test_no_match_returns_empty(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello world")
        assert log.search_sessions("zzznope") == []

    def test_ignores_json_structural_fields(self, tmp_path):
        """Query must match message ``content`` only, not JSON keys/values.

        Searching for common tokens like ``user`` or ``role`` would otherwise
        hit every file because the raw JSONL contains
        ``"role": "user"`` on every line.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "hello there")
        # "role" appears in every JSONL line as a structural key — must not match
        assert log.search_sessions("role") == []
        # "user" appears as the role value — must not match on that alone
        assert log.search_sessions("user") == []
        # But a real content substring does match
        assert [s["key"] for s in log.search_sessions("hello")] == ["alpha"]

    def test_matches_query_with_json_escaped_chars(self, tmp_path):
        """Query containing backslash/quote must match despite JSON escaping.

        Regression: file paths like ``src\\kiro_crew`` are stored in JSONL
        as ``src\\\\kiro_crew`` (escaped).  A raw-line substring fast-path
        would miss them; parsing every line ensures the needle is compared
        against the un-escaped ``content`` value.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", r"edited src\kiro_crew\history.py today")
        results = log.search_sessions(r"src\kiro_crew")
        assert [s["key"] for s in results] == ["alpha"]

    def test_case_insensitive_unicode(self, tmp_path):
        """Non-ASCII case folding — ``Über`` in the file must match ``über``.

        NOTE: this test writes the JSONL file directly instead of using
        :meth:`ConversationLog.append` because Python's ``json.dumps``
        defaults to ``ensure_ascii=True`` and would escape ``Ü`` as
        ``\\u00dc``.  That would bypass the Unicode case-folding code path
        we want to exercise.  Writing raw UTF-8 simulates future storage
        formats or externally-pasted content that may contain non-ASCII
        bytes verbatim.
        """
        (tmp_path / "alpha.jsonl").write_text(
            '{"key": "alpha", "title": "alpha", "created": "2025-01-01T00:00:00"}\n'
            '{"role": "user", "content": "Über alles"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("über")
        assert [s["key"] for s in results] == ["alpha"]

    def test_casefold_matches_sharp_s(self, tmp_path):
        """``str.casefold`` folds German ``ß`` to ``ss`` so ``strasse`` matches ``straße``.

        ``str.lower`` (previous impl) left ``ß`` unchanged, so a search
        for ``strasse`` would miss content containing ``straße``.
        """
        (tmp_path / "alpha.jsonl").write_text(
            '{"key": "alpha", "title": "alpha", "created": "2025-01-01T00:00:00"}\n'
            '{"role": "user", "content": "Hauptstraße 5"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)
        assert [s["key"] for s in log.search_sessions("hauptstrasse")] == ["alpha"]

    def test_title_match_ranks_above_content_only(self, tmp_path):
        """Title match gets field boost, outranking a content-only match."""
        # content-only match, newer (would win on recency alone)
        (tmp_path / "content-only.jsonl").write_text(
            '{"_type": "metadata", "title": "unrelated topic"}\n'
            '{"role": "user", "content": "we discussed apollo deploy"}\n',
            encoding="utf-8",
        )
        # title match, older
        (tmp_path / "title-match.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo troubleshooting"}\n'
            '{"role": "user", "content": "help with the pipeline"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "title-match.jsonl", (1000, 1000))
        os.utime(tmp_path / "content-only.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["title-match", "content-only"]

    def test_more_content_hits_ranks_higher(self, tmp_path):
        """Session with more content occurrences ranks above one with fewer (same length + no title match).

        Expected winner ``many`` is written *older* than ``few`` so that
        recency alone would place it second - only the hit-count scoring
        can flip the order.
        """
        (tmp_path / "many.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "few.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo xxxxxx xxxxxx xxxxxx xxxxxx"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "many.jsonl", (1000, 1000))
        os.utime(tmp_path / "few.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["many", "few"]

    def test_length_norm_favors_short_focused_session(self, tmp_path):
        """Short session with N hits ranks above long session with same N hits.

        Expected winner ``short`` is written *older* so recency alone
        would place it second - only length normalization can flip it.
        """
        (tmp_path / "short.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "long.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo ' + "x " * 2000 + '"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "short.jsonl", (1000, 1000))
        os.utime(tmp_path / "long.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["short", "long"]

    def test_zero_match_sessions_excluded(self, tmp_path):
        """Sessions without a match must not appear in results, even with limit>count."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "apollo")
        log.append("miss", "user", "unrelated")
        assert [s["key"] for s in log.search_sessions("apollo")] == ["hit"]

    def test_recency_tiebreaker_when_scores_equal(self, tmp_path):
        """Equal-score sessions preserve recency order (newer first).

        Uses three sessions: a title-match (highest score) plus two
        content-only matches with identical score.  The middle higher-
        scoring entry forces the sort to actually reorder, so the
        newest-first-on-tie invariant isn't satisfied trivially.
        """
        (tmp_path / "older.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n' '{"role": "user", "content": "apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "middle-title.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo"}\n' '{"role": "user", "content": "x"}\n',
            encoding="utf-8",
        )
        (tmp_path / "newer.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n' '{"role": "user", "content": "apollo"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "older.jsonl", (1000, 1000))
        os.utime(tmp_path / "middle-title.jsonl", (1500, 1500))
        os.utime(tmp_path / "newer.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert [s["key"] for s in results] == ["middle-title", "newer", "older"]

    def test_respects_limit_after_ranking(self, tmp_path):
        """*limit* caps results **after** ranking, so top-scored wins are kept.

        Expected winner ``strong`` is written *older* so recency alone
        would place it second (and an old early-exit-at-limit code path
        would return ``weak`` instead).
        """
        (tmp_path / "strong.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        (tmp_path / "weak.jsonl").write_text(
            '{"_type": "metadata", "title": "t"}\n'
            '{"role": "user", "content": "apollo and other things"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "strong.jsonl", (1000, 1000))
        os.utime(tmp_path / "weak.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo", limit=1)
        assert [s["key"] for s in results] == ["strong"]

    def test_title_boost_outranks_heavy_content(self, tmp_path):
        """Single title match outranks many content hits in a short session.

        Locks in the magnitude of ``_TITLE_BOOST``: if the constant is
        silently reduced (e.g. to 2), a short session with 5+ content
        hits would outrank a single title match and this test would
        fail.  Guards the "title is strong evidence" invariant.
        """
        # Short session with 5 content hits, no title match.  Written
        # directly so the title doesn't auto-extract from content.
        (tmp_path / "heavy-content.jsonl").write_text(
            '{"_type": "metadata", "title": "chat about deployments"}\n'
            '{"role": "user", "content": "apollo apollo apollo apollo apollo"}\n',
            encoding="utf-8",
        )
        # Title-only match, no content hits.  Written *older* so recency
        # alone would place it second - only the title boost can flip it.
        (tmp_path / "title-only.jsonl").write_text(
            '{"_type": "metadata", "title": "apollo deploy"}\n'
            '{"role": "user", "content": "unrelated text"}\n',
            encoding="utf-8",
        )
        import os

        os.utime(tmp_path / "title-only.jsonl", (1000, 1000))
        os.utime(tmp_path / "heavy-content.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)
        results = log.search_sessions("apollo")
        assert results[0]["key"] == "title-only", (
            "A single title match must outrank even a heavy content-hit "
            "session - if this fails, _TITLE_BOOST was reduced below the "
            "threshold where title evidence dominates."
        )

    def test_short_needle_digit_noise_does_not_bury_a_title_phrase_match(self, tmp_path):
        """A titled ``case 5`` session outranks a long transcript dense in digit 5s.

        ``5`` is a one-character substring needle, and agent transcripts are
        dense in digits (timestamps, account ids, commit hashes), so on raw
        frequency a long session that merely CONTAINS thousands of 5s out-scores
        the session whose TITLE is the query. This pins the fix at realistic scale
        (~10,000 digit-5 hits in a ~45 KB transcript): the body hit count of a
        short needle is saturated, so digit noise cannot beat a
        ``_TITLE_BOOST``-weighted whole-phrase title hit. The noisy session is
        written NEWER so recency alone would rank it
        first — only the ranking change can flip it.
        """
        # ~10,000 digit-5 hits spread over a realistic transcript: ISO timestamps
        # and account ids on every line, plus a handful of real "case 5" mentions.
        noisy_lines = []
        for i in range(400):
            noisy_lines.append(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": (
                            f"2026-05-15T15:55:{i % 60:02d} ledger 5555-5555-5555 "
                            f"instance i-5f5e5d5c5b5a5{i:04d} rebooted; "
                            + ("edge case 5 needs live discovery" if i % 40 == 0 else "ok")
                        ),
                    }
                )
            )
        (tmp_path / "noisy.jsonl").write_text(
            '{"_type": "metadata", "title": "DR-11898 edge cases sweep"}\n'
            + "\n".join(noisy_lines)
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "titled.jsonl").write_text(
            '{"_type": "metadata", "title": "Case 5 ARM64 failback"}\n'
            '{"role": "user", "content": "unrelated text"}\n',
            encoding="utf-8",
        )
        os.utime(tmp_path / "titled.jsonl", (1000, 1000))
        os.utime(tmp_path / "noisy.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("case 5")

        assert [s["key"] for s in results] == ["titled", "noisy"], (
            "the session titled with the whole query must outrank a long "
            "transcript whose only advantage is thousands of incidental digit-5 "
            "substring hits"
        )

    def test_short_forge_needle_digit_noise_does_not_bury_a_title_match(self, tmp_path):
        noisy_lines = []
        for i in range(400):
            noisy_lines.append(
                json.dumps(
                    {
                        "role": "assistant",
                        "content": (
                            f"2026-05-15T15:55:{i % 60:02d} ledger 5555-5555-5555 "
                            f"instance i-5f5e5d5c5b5a5{i:04d} rebooted; "
                            + ("edge case 5 needs live discovery" if i % 40 == 0 else "ok")
                        ),
                    }
                )
            )
        (tmp_path / "noisy.jsonl").write_text(
            '{"_type": "metadata", "title": "DR-11898 edge cases sweep"}\n'
            + "\n".join(noisy_lines)
            + "\n",
            encoding="utf-8",
        )
        (tmp_path / "titled.jsonl").write_text(
            '{"_type": "metadata", "title": "Case 5 ARM64 failback"}\n'
            '{"role": "user", "content": "unrelated text"}\n',
            encoding="utf-8",
        )
        os.utime(tmp_path / "titled.jsonl", (1000, 1000))
        os.utime(tmp_path / "noisy.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("issue 5")

        assert results[0]["key"] == "titled"

    def test_short_needle_saturation_keeps_substantive_long_session_ahead(self, tmp_path):
        """Saturation must not invert body-only order for a short token.

        Query ``s3``: a long session that discusses S3 two hundred times must
        still outrank a short session with one incidental ``s3`` mention. The
        saturated count is the length-NORMALIZED one, so the length norm is
        applied once; saturating the raw count and then dividing by the norm
        compounds two penalties on the long session and flips this order.
        The short session is written NEWER so recency favors it.
        """
        long_lines = [
            json.dumps(
                {"role": "assistant", "content": f"bucket policy for s3 step {i}: " + "x" * 400}
            )
            for i in range(200)
        ]
        (tmp_path / "long-substantive.jsonl").write_text(
            '{"_type": "metadata", "title": "storage design"}\n' + "\n".join(long_lines) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "short-incidental.jsonl").write_text(
            '{"_type": "metadata", "title": "quick question"}\n'
            '{"role": "user", "content": "copied it from s3 once, unrelated otherwise"}\n',
            encoding="utf-8",
        )
        os.utime(tmp_path / "long-substantive.jsonl", (1000, 1000))
        os.utime(tmp_path / "short-incidental.jsonl", (2000, 2000))
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("s3")

        assert [s["key"] for s in results] == ["long-substantive", "short-incidental"]

    def test_short_needle_still_gates_on_body_presence(self, tmp_path):
        """Dampening is a ranking change only: a body-only short token still matches.

        The ``5`` in ``case 5`` may appear ONLY in a session's content. Dropping
        short needles from the body scan would fail the AND gate for exactly
        the sessions the query is for, so the gate is untouched and only the
        weight moves.
        """
        (tmp_path / "body-only.jsonl").write_text(
            '{"_type": "metadata", "title": "edge cases"}\n'
            '{"role": "user", "content": "case 5 needs live discovery"}\n',
            encoding="utf-8",
        )
        (tmp_path / "miss.jsonl").write_text(
            '{"_type": "metadata", "title": "edge cases"}\n'
            '{"role": "user", "content": "case four needs live discovery"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)

        assert [s["key"] for s in log.search_sessions("case 5")] == ["body-only"]
        assert [s["key"] for s in log.search_sessions("5")] == ["body-only"]

    def test_scan_window_caps_files_scored(self, tmp_path, monkeypatch):
        """Only the ``_SEARCH_SCAN_WINDOW`` newest files are scored.

        Files outside the window must not appear in results even if they
        would score higher, bounding per-search I/O.
        """
        monkeypatch.setattr("kiro_crew.history._SEARCH_SCAN_WINDOW", 2)
        log = ConversationLog(base_dir=tmp_path)
        # Oldest: strong match (would win on score if scanned)
        log.append("old-strong", "user", "apollo apollo apollo apollo apollo")
        # Two newer weak matches fill the scan window
        log.append("new-weak-1", "user", "apollo x")
        log.append("new-weak-2", "user", "apollo y")
        # Explicit mtimes: filesystems with 1-second granularity (macOS
        # HFS+) can give all three files the same mtime, making the
        # list_sessions() order non-deterministic without this.
        import os

        os.utime(tmp_path / "old-strong.jsonl", (1000, 1000))
        os.utime(tmp_path / "new-weak-1.jsonl", (2000, 2000))
        os.utime(tmp_path / "new-weak-2.jsonl", (3000, 3000))
        result_keys = [s["key"] for s in log.search_sessions("apollo")]
        assert "old-strong" not in result_keys
        assert "new-weak-1" in result_keys
        assert "new-weak-2" in result_keys

    def test_substring_scan_reads_the_file_and_leaves_msg_cache_alone(self, tmp_path):
        """search_sessions sources content from the file, never ``_msg_cache``.

        Both halves of a query read the session file directly: the fold that
        counts matches, and the snippet built for each returned row. Neither goes
        through ``_read_messages``, for two reasons.

        Memory: ``_read_messages`` memoizes the PARSED message dicts, and a scan
        touches every session in the window — so sourcing content through it makes
        searching pin the whole corpus's parsed form in RSS.

        Correctness: ``_msg_cache`` is populated by callers that hold no write
        lock, so an entry can be a pre-rewrite parse stored under a restored
        (unchanged) mtime. Folding from it would launder that staleness into the
        search cache, where the mtime guard cannot detect it.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("a", "user", "apollo deployment rollback notes")
        log._msg_cache.clear()
        calls: list[str] = []
        real = log._read_messages

        def counting(key: str) -> list[dict]:
            calls.append(key)
            return real(key)

        log._read_messages = counting  # type: ignore[assignment]
        hits = log.search_sessions("apollo")
        assert [s["key"] for s in hits] == ["a"]
        # The snippet still resolves, from the file rather than a parsed cache.
        assert "apollo" in hits[0]["snippet"]
        assert calls == [], "the search path must not enter _read_messages"
        assert len(log._msg_cache) == 0, "searching must not pin a parsed transcript"

    def test_multi_word_query_matches_scattered_tokens(self, tmp_path):
        """A multi-word query matches when its words appear APART, not adjacent.

        Regression: the query was matched as one whole-phrase substring, so a
        natural query like "ack contention hypotheses" silently returned nothing
        even though the session discussed all three — the exact phrase never
        occurs. Silent zero results are the worst failure mode for search: the
        caller cannot tell "not in history" from "phrased it wrong".
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "the ack path shows contention under load")
        log.append("hit", "assistant", "ranked the hypotheses by expected effect")
        log.append("miss", "user", "unrelated disk cleanup chatter")

        results = log.search_sessions("ack contention hypotheses")

        assert [s["key"] for s in results] == ["hit"]

    def test_multi_word_query_requires_every_token(self, tmp_path):
        """AND, not OR: a session missing one token must not surface.

        OR semantics would make a common word drag in the whole corpus, which is
        a different way of being useless than the phrase bug.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("both", "user", "contention in the hypotheses list")
        log.append("partial", "user", "contention everywhere but no hypothesis list")

        results = log.search_sessions("contention hypotheses")

        assert [s["key"] for s in results] == ["both"]

    def test_exact_phrase_outranks_same_words_apart(self, tmp_path):
        """At comparable token frequency, words appearing TOGETHER rank first.

        This is what ``_PHRASE_BOOST`` buys, and all it buys: adjacency wins the
        tie. It is not an override of term frequency — a session repeating one
        token far more often still ranks higher on raw count, which is the
        behaviour this ranker already had for single-token queries.

        Titles are written explicitly because ``list_sessions`` otherwise
        derives a title from the first message, which would hand a
        content-heavy session a ``_TITLE_BOOST`` and mask what is being tested.
        """
        (tmp_path / "apart.jsonl").write_text(
            '{"_type": "metadata", "title": "session one"}\n'
            '{"role": "user", "content": "ping at the start and pong at the end"}\n',
            encoding="utf-8",
        )
        (tmp_path / "together.jsonl").write_text(
            '{"_type": "metadata", "title": "session two"}\n'
            '{"role": "user", "content": "the ping pong bench numbers"}\n',
            encoding="utf-8",
        )
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("ping pong")

        assert {s["key"] for s in results} == {"apart", "together"}
        assert results[0]["key"] == "together"

    def test_multi_word_scattered_match_still_gets_a_snippet(self, tmp_path):
        """A scattered multi-word match must show WHY it surfaced.

        The snippet builder searched for the whole phrase, so every session that
        newly matches on scattered tokens would otherwise come back with no
        snippet — surfacing rows a caller cannot evaluate.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "the ack path shows contention under load")
        log.append("hit", "assistant", "ranked the hypotheses by expected effect")

        results = log.search_sessions("ack contention hypotheses")

        assert results[0]["snippet"], "expected a non-empty snippet"
        assert "contention" in results[0]["snippet"].casefold()

    def test_whitespace_only_query_matches_nothing_and_reads_no_file(self, tmp_path):
        """A whitespace-only query returns [] *and* reads no session file.

        It tokenizes to an empty list. Asserting only on the empty result would
        pass with or without the early return, because a tokenless loop scores
        nothing anyway — so this pins the I/O too: without the guard every
        session in the scan window still gets read and folded to reach a
        foregone conclusion.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "some content here")
        reads: list[str] = []
        real = log._folded_content

        def counting(key: str) -> tuple[int, str]:
            reads.append(key)
            return real(key)

        log._folded_content = counting  # type: ignore[assignment]

        assert log.search_sessions("   ") == []
        assert log.search_sessions("\t\n") == []
        assert reads == [], "a tokenless query must not read any session file"

    def test_single_token_query_behaviour_is_unchanged(self, tmp_path):
        """One token IS the phrase — substring matching must still apply.

        Guards the search-as-you-type path: a partial word has to keep matching
        the longer word it prefixes.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("alpha", "user", "deep contention analysis")

        assert [s["key"] for s in log.search_sessions("cont")] == ["alpha"]
        assert [s["key"] for s in log.search_sessions("contention")] == ["alpha"]


class TestSearchQueryTokens:
    """Bounds on the parse shared by the matcher and the snippet builders.

    Every needle costs one full scan of a session's text, so the needle list is
    the knob that multiplies search cost on a user-supplied string.
    """

    @staticmethod
    def _required_texts(query: str) -> tuple[list[str], str]:
        needles, phrase, _ = history.parse_search_query(query)
        return ([n.text for n in needles if n.required], phrase)

    def test_repeated_terms_collapse_to_one_needle(self):
        """A repeated term must not buy extra scans — it cannot change an AND match.

        Regression: a 256-char query of repeated "a " tokenized to 128 terms and
        drove 128 full scans per session instead of 1, stalling a keystroke-driven
        search.
        """
        texts, phrase = self._required_texts("a " * 128)

        assert texts == ["a"], "duplicates must collapse"
        assert phrase == " ".join(["a"] * 128), "the phrase keeps the query as typed"

    def test_distinct_terms_are_capped(self):
        """Dedup cannot bound the distinct case, so the cap does.

        Truncation keeps the FIRST terms, making the query looser rather than
        wrong — it can admit extra results but never hide a matching session.
        """
        query = " ".join(f"t{i}" for i in range(history.SEARCH_MAX_TOKENS + 25))

        texts, _ = self._required_texts(query)

        assert len(texts) == history.SEARCH_MAX_TOKENS
        assert texts[0] == "t0", "the cap keeps the first terms, in order"

    def test_whitespace_only_query_yields_no_needles(self):
        """No needles, so an all-required-present check cannot be vacuously true."""
        assert history.parse_search_query("   \t\n") == ([], "", False)

    def test_order_is_first_seen(self):
        """Needle order is stable and first-seen, so the snippet fallback is predictable."""
        texts, _ = self._required_texts("beta alpha beta gamma")

        assert texts == ["beta", "alpha", "gamma"]

    def test_deduped_query_still_gets_phrase_treatment(self, tmp_path):
        """\"a a" dedups to ONE token but its phrase is still two words.

        Keyed off `tokens != [phrase]` rather than `len(tokens) > 1`, so the
        session matched on the single token still resolves a snippet instead of
        searching only for a phrase it may not contain.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("hit", "user", "the letter a stands alone here")

        results = log.search_sessions("a a")

        assert [s["key"] for s in results] == ["hit"]
        assert results[0]["snippet"], "a deduped multi-word query must still snippet"


class TestCjkSearch:
    """CJK-aware query segmentation — gate on characters, rank on bigrams.

    CJK text is written without spaces, so whitespace tokenization hands the
    matcher a whole clause as ONE token and a multi-word query only ever
    matches its own literal sentence. These tests pin the recall fix (character
    gate) and the precision compensation (bigram-weighted ranking).
    """

    @staticmethod
    def _write_cjk_session(tmp_path, key: str, content: str) -> None:
        """Write a session file directly so CJK stays unescaped in the JSONL.

        Mirrors the unicode-casefold test above: ``json.dumps`` with default
        ``ensure_ascii=True`` would store ``\\uXXXX`` escapes, which the parser
        unescapes anyway — writing raw keeps the fixture human-readable.
        """
        line = json.dumps({"role": "user", "content": content}, ensure_ascii=False)
        (tmp_path / f"{key}.jsonl").write_text(line + "\n", encoding="utf-8")

    def test_spaceless_multiword_cjk_query_matches_separated_words(self, tmp_path):
        """\"内存泄漏" must find a session whose words appear apart.

        Regression: the whole run was one required substring, so only a
        transcript containing the literal string "内存泄漏" could match — the
        exact-sentence trap this segmentation exists to break.
        """
        self._write_cjk_session(tmp_path, "hit", "今天调查了内存里的数据泄漏问题")
        self._write_cjk_session(tmp_path, "miss", "完全无关的话题")
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("内存泄漏")

        assert [s["key"] for s in results] == ["hit"]

    def test_cjk_gate_is_still_an_and(self, tmp_path):
        """A session missing one query character stays disqualified."""
        self._write_cjk_session(tmp_path, "alpha", "内存充足没有问题")
        log = ConversationLog(base_dir=tmp_path)

        assert log.search_sessions("内存泄漏") == []

    def test_adjacent_cjk_match_outranks_scattered_characters(self, tmp_path):
        """Bigram + phrase weighting puts the real word hit first.

        Both sessions pass the character gate and the adjacency floor (each
        contains at least one query bigram); the one containing the query as an
        adjacent run must rank above the one holding only the words apart, or
        the gate's extra recall would degrade top-N precision.
        """
        self._write_cjk_session(tmp_path, "scattered", "内存里的数据泄漏了")
        self._write_cjk_session(tmp_path, "adjacent", "内存泄漏定位完成了")
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("内存泄漏")

        assert [s["key"] for s in results] == ["adjacent", "scattered"]

    def test_character_scatter_without_any_adjacency_is_excluded(self, tmp_path):
        """All query characters present but never adjacent — noise, not a hit.

        Individual han/kana characters are common enough that ranking alone
        cannot keep scatter off a result page with few real hits, so the
        adjacency floor excludes rather than down-ranks.
        """
        # Contains 内, 存, 泄, 漏 — but no bigram of "内存泄漏" appears adjacently.
        self._write_cjk_session(tmp_path, "scatter", "内部保存了泄压阀和漏水的记录")
        log = ConversationLog(base_dir=tmp_path)

        assert log.search_sessions("内存泄漏") == []

    def test_long_query_bigram_truncation_waives_the_adjacency_floor(self, tmp_path):
        """A 14+-char CJK query truncates its bigram set; the floor must not
        turn that cost cap into a hidden gate.

        Regression (Design review): a session whose only adjacency hit was a
        DROPPED bigram would be excluded by the floor — hiding results for
        exactly the long spaceless queries the segmentation exists to serve,
        and breaking the "truncation only loosens" safety rationale.
        """
        # 15 distinct chars -> 14 bigrams > _SEARCH_MAX_SCORING_EXTRAS (12).
        query = "".join(chr(0x4E00 + i) for i in range(15))
        # Session contains every char (satisfies the gate, capped at 12
        # required) but adjacently only the LAST bigram — one of the two the
        # cap drops — plus the rest scattered with separators.
        tail_bigram = query[-2:]
        scattered = "、".join(query[:-2]) + "。" + tail_bigram
        self._write_cjk_session(tmp_path, "longhit", scattered)
        log = ConversationLog(base_dir=tmp_path)

        assert [s["key"] for s in log.search_sessions(query)] == ["longhit"]

    def test_mixed_script_token_splits_at_script_boundary(self, tmp_path):
        """\"kirocrew部署" matches a doc where the ASCII and CJK parts sit apart."""
        self._write_cjk_session(tmp_path, "hit", "kirocrew 的部署流程记录")
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("kirocrew部署")

        assert [s["key"] for s in results] == ["hit"]

    def test_single_cjk_character_query_matches(self, tmp_path):
        self._write_cjk_session(tmp_path, "hit", "泄压阀已检查")
        log = ConversationLog(base_dir=tmp_path)

        assert [s["key"] for s in log.search_sessions("泄")] == ["hit"]

    def test_cjk_match_returns_snippet(self, tmp_path):
        """The snippet builder shares the parse, so a CJK hit still excerpts."""
        self._write_cjk_session(tmp_path, "hit", "前情提要之后我们讨论了内存泄漏的修复方案")
        log = ConversationLog(base_dir=tmp_path)

        results = log.search_sessions("内存泄漏")

        assert results and "内存泄漏" in results[0]["snippet"]


class TestParseSearchQuery:
    """Needle derivation — required/scoring split and its bounds."""

    def test_ascii_terms_are_required_full_weight(self):
        needles, phrase, floor = history.parse_search_query("deploy Timeout")

        assert needles == [
            history.SearchNeedle("deploy", 1.0, True),
            history.SearchNeedle("timeout", 1.0, True),
        ]
        assert phrase == "deploy timeout"
        assert floor is False, "no bigrams -> no adjacency floor"

    def test_cjk_run_gates_on_chars_and_scores_on_bigrams(self):
        needles, _, floor = history.parse_search_query("内存泄漏")

        required = [n for n in needles if n.required]
        scoring = [n for n in needles if not n.required]
        assert [n.text for n in required] == ["内", "存", "泄", "漏"]
        assert all(n.weight == history._CJK_CHAR_WEIGHT for n in required)
        assert [n.text for n in scoring] == ["内存", "存泄", "泄漏"]
        assert all(n.weight == 1.0 for n in scoring)
        assert floor is True, "untruncated bigrams enforce the adjacency floor"

    def test_single_cjk_char_run_is_a_full_weight_term(self):
        """A lone character IS the whole term — no bigrams, no down-weighting."""
        needles, _, _ = history.parse_search_query("泄")

        assert needles == [history.SearchNeedle("泄", 1.0, True)]

    def test_short_ascii_terms_saturate_body_only(self):
        """``5`` and ``s3`` keep full title weight and full gate strength.

        Only their CONTENT hit count is saturated (``saturate_body``): a one-
        or two-character substring matches incidental text (timestamps, ids,
        hashes) so often that its raw body frequency is noise, not relevance.
        Length is the whole test, digits included: incidental frequency falls
        roughly 10x per extra character, so ``4411`` keeps raw frequency
        alongside longer words -- saturating it would cap the specific term of
        a query like ``timeout 50051`` while its ordinary co-term stays
        linear, inverting which term drives body-only ranking. ``555`` and
        ``pod`` pin the boundary at exactly three characters.
        """
        needles, _, _ = history.parse_search_query("case 5 s3 555 pod 4411 deploy")

        by_text = {n.text: n for n in needles if n.required}
        for unsaturated in ("case", "deploy", "555", "pod", "4411"):
            assert by_text[unsaturated].saturate_body is False, unsaturated
        for short in ("5", "s3"):
            needle = by_text[short]
            assert needle.weight == 1.0, short
            assert needle.required is True, short
            assert needle.saturate_body is True, short

    @pytest.mark.parametrize("query", ["issue 5", "pr 5"])
    def test_forge_needles_with_a_short_spelling_are_saturated(self, query):
        needles, _, _ = history.parse_search_query(query)
        required = [needle for needle in needles if needle.required]

        assert len(required) == 1
        assert required[0].saturate_body is True

    def test_duplicate_reference_alt_merge_recomputes_saturation(self):
        """A duplicate reference recomputes saturation after merging a bare alt."""
        needles, _, _ = history.parse_search_query("#42 issue 42")
        required = [needle for needle in needles if needle.required]

        assert len(required) == 1
        needle = required[0]
        assert "42" in needle.alts
        assert needle.saturate_body is True

        needles, _, _ = history.parse_search_query("#4411 issue 4411")
        required = [needle for needle in needles if needle.required]

        assert len(required) == 1
        needle = required[0]
        assert "4411" in needle.alts
        assert needle.saturate_body is False

    @pytest.mark.parametrize("query", ["PR 4411", "#4411", "pull/4411"])
    def test_forge_needles_without_a_short_spelling_are_not_saturated(self, query):
        needles, _, _ = history.parse_search_query(query)
        required = [needle for needle in needles if needle.required]

        assert len(required) == 1
        assert required[0].saturate_body is False

    @pytest.mark.parametrize("query, expected", [("5", True), ("4411", False)])
    def test_bare_number_ranking_hint_saturation(self, query, expected):
        needles, _, _ = history.parse_search_query(query)
        ranking = [needle for needle in needles if not needle.required]

        assert len(ranking) == 1
        assert ranking[0].saturate_body is expected

    def test_cjk_needles_are_not_saturated(self):
        """The short-token rule is an ASCII noise rule; CJK ranking is untouched.

        A CJK bigram is two characters long and a lone character is one, and
        both are the module's *intended* CJK signal — the character needles
        already carry their own ``_CJK_CHAR_WEIGHT``.
        """
        needles, _, _ = history.parse_search_query("内存 泄")

        assert not any(n.saturate_body for n in needles), needles

    def test_short_non_ascii_words_are_not_saturated(self):
        """A two-character Hangul word is a whole word, not digit noise.

        Hangul is space-separated so it is NOT segmented as a CJK run and
        arrives on the plain-term path; the short-token rule must not treat
        ``한글`` like ``s3``. Same for a short accented Latin term.
        """
        needles, _, _ = history.parse_search_query("한글 où")

        assert [n.text for n in needles] == ["한글", "où"]
        assert not any(n.saturate_body for n in needles), needles

    def test_scoring_extras_are_capped(self):
        """Bigrams cost one scan each, so they get their own bound."""
        run = "".join(chr(0x4E00 + i) for i in range(40))

        needles, _, floor = history.parse_search_query(run)

        scoring = [n for n in needles if not n.required]
        required = [n for n in needles if n.required]
        assert len(scoring) == history._SEARCH_MAX_SCORING_EXTRAS
        assert len(required) == history.SEARCH_MAX_TOKENS
        assert floor is False, (
            "a truncated bigram set cannot prove no-adjacency-anywhere, so the "
            "floor is waived — truncation must only LOOSEN, never hide a session"
        )

    def test_snippet_needles_order_phrase_then_bigrams_then_chars(self):
        """Excerpts center on the first hit, so highest-signal needles go first."""
        needles = history.snippet_needles("内存泄漏")

        assert needles[0] == "内存泄漏"
        assert needles.index("内存") < needles.index("内")

    def test_snippet_needles_empty_for_whitespace(self):
        assert history.snippet_needles("   \t") == []

    def test_snippet_needles_keep_ascii_first_seen_order(self):
        """ASCII fallback order stays first-typed — the pre-CJK contract."""
        needles = history.snippet_needles("beta alphabet gamma")

        assert needles == ["beta alphabet gamma", "beta", "alphabet", "gamma"]


class TestNeedlesMatchText:
    """Single-text gate shared with title-only fallbacks (Discord resume)."""

    @staticmethod
    def _matches(query: str, text: str) -> bool:
        needles, _, floor = history.parse_search_query(query)
        return history.needles_match_text(needles, text.casefold(), floor)

    def test_ascii_all_words_required(self):
        assert self._matches("link specific", "Link to a Specific Session")
        assert not self._matches("link specific", "Link to a Session")

    def test_cjk_words_apart_match(self):
        """The exact trap the whitespace word-count test never caught: a
        spaceless CJK query is one 'word', so the old all-words gate demanded
        the literal substring."""
        assert self._matches("内存泄漏", "内存的泄漏问题排查")

    def test_cjk_scatter_without_adjacency_rejected(self):
        assert not self._matches("内存泄漏", "内部保存了泄压阀和漏水的记录")

    def test_cjk_missing_char_rejected(self):
        assert not self._matches("内存泄漏", "内存充足没有问题")

    def test_empty_needles_match_nothing(self):
        assert not history.needles_match_text([], "anything")


class TestRecencyBoost:
    """Bounded multiplicative recency weighting in search_sessions ranking."""

    @staticmethod
    def _set_age(tmp_path, key: str, days: float) -> None:
        # flake-ok: pre-existing real-clock backdating of the session file's mtime, moved verbatim; tracked for a de-flake pass
        t = time.time() - days * 86400
        os.utime(tmp_path / f"{key}.jsonl", (t, t))

    def test_recent_session_outranks_stale_equal_match(self, tmp_path):
        """At equal relevance the newer session must come first.

        Regression: scoring was pure term frequency, so a year-old session
        matching twice buried today's session matching once.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("stale", "user", "apollo deployment notes")
        log.append("fresh", "user", "apollo deployment notes")
        self._set_age(tmp_path, "stale", 365)
        self._set_age(tmp_path, "fresh", 0)

        results = log.search_sessions("apollo")

        assert [s["key"] for s in results] == ["fresh", "stale"]

    def test_stale_double_mention_loses_to_fresh_single_mention(self, tmp_path):
        """The canonical complaint, verbatim: a year-old session matching TWICE
        must not outrank today's session matching once. This is what sizes
        _RECENCY_MAX_BOOST — a ceiling of 1.0 left exactly this case unfixed.
        """
        log = ConversationLog(base_dir=tmp_path)
        log.append("stale", "user", "apollo apollo deployment notes here")
        log.append("fresh", "user", "apollo filler deployment notes here")
        self._set_age(tmp_path, "stale", 365)
        self._set_age(tmp_path, "fresh", 0)

        results = log.search_sessions("apollo")

        assert [s["key"] for s in results] == ["fresh", "stale"]

    def test_decisively_better_old_match_still_wins(self, tmp_path):
        """The boost is bounded, so it reorders near-ties, not clear wins."""
        log = ConversationLog(base_dir=tmp_path)
        log.append("stale", "user", "apollo apollo apollo apollo apollo notes")
        log.append("fresh", "user", "apollo filler filler filler filler notes")
        self._set_age(tmp_path, "stale", 365)
        self._set_age(tmp_path, "fresh", 0)

        results = log.search_sessions("apollo")

        assert [s["key"] for s in results] == ["stale", "fresh"]
