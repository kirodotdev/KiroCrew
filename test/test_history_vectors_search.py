"""Tests for session search by meaning: the query role, the vector leg and the route.

Sessions are real transcript files; the chunk pass and the fill build the store as
they do in production, with a word-counting tokenizer and a stand-in embedder whose
vectors put words of one topic on one axis, so a paraphrase lands near its session
without a model. Pins RFC ``rfc-session-semantic-search`` 7.3 tests 1 to 5, the
section 9 ownership test, and the section 12 deletion and count tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from session_meaning_helpers import (
    _DIM,
    _TOKENIZER,
    _build,
    _corpus,
    _Embedder,
    _write,
    install_leg,
)

from kiro_crew import embeddings
from kiro_crew import history_vectors_fill as fill
from kiro_crew import history_vectors_index as chunking
from kiro_crew import history_vectors_search as meaning
from kiro_crew.history import ConversationLog


@pytest.fixture
def leg_on(monkeypatch: pytest.MonkeyPatch) -> _Embedder:
    return install_leg(monkeypatch)


def _search(base: Path, query: str, **kwargs):
    log = ConversationLog(base_dir=base)
    try:
        return log.search_sessions(query, **kwargs)
    finally:
        log._catalog_projection.vector_leg.release()


class TestQueryRole:
    def test_the_default_role_sends_the_text_as_given(self, monkeypatch: pytest.MonkeyPatch):
        from kiro_crew.embeddings_llama import LlamaCppEmbedder

        sent: list[list[str]] = []
        backend = LlamaCppEmbedder.__new__(LlamaCppEmbedder)
        backend._inflight_lock = __import__("threading").Lock()
        backend._inflight = 0
        monkeypatch.setattr(
            backend, "_embed_batch", lambda texts, priority: sent.append(list(texts)) or [[0.0]]
        )

        backend.embed_batch(["plain words"])
        backend.embed("plain words")
        backend.embed("find it", role=embeddings.EMBED_ROLE_QUERY)

        assert sent == [
            ["plain words"],
            ["plain words"],
            [embeddings.SESSION_QUERY_INSTRUCTION + "find it"],
        ]

    def test_an_unknown_role_is_refused(self):
        from kiro_crew.embeddings_llama import LlamaCppEmbedder

        backend = LlamaCppEmbedder.__new__(LlamaCppEmbedder)
        with pytest.raises(ValueError):
            backend.embed_batch(["x"], role="passage")


class TestRoute:
    def test_the_flag_off_returns_todays_rows(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        _corpus(tmp_path)
        _build(tmp_path)
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: False)
        on_rows = None
        rows = _search(tmp_path, "pipeline")
        assert [row["key"] for row in rows] == ["ship"]
        assert all("match" not in row for row in rows)
        assert getattr(rows, "semantic", None) is None
        assert leg_on.calls == [] and on_rows is None

    def test_no_store_returns_todays_rows(self, tmp_path: Path, leg_on: _Embedder):
        _corpus(tmp_path)
        rows = _search(tmp_path, "pipeline")
        assert [row["key"] for row in rows] == ["ship"]
        assert all("match" not in row for row in rows)
        assert not (tmp_path / ".index" / "session_vectors.db").exists()

    def test_a_remote_embedder_returns_todays_rows(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        _corpus(tmp_path)
        _build(tmp_path)
        monkeypatch.setattr(fill, "embeds_on_this_host", lambda e: False)
        rows = _search(tmp_path, "how did we ship the release")
        assert list(rows) == []
        assert leg_on.calls == []

    def test_a_description_finds_its_session_by_meaning(self, tmp_path: Path, leg_on: _Embedder):
        _corpus(tmp_path)
        _build(tmp_path)

        rows = _search(tmp_path, "when did we ship the rollout")

        assert rows[0]["key"] == "ship"
        assert rows[0]["match"] == "meaning"
        assert "pipeline" in rows[0]["snippet"]
        assert leg_on.calls[0] == ("when did we ship the rollout", embeddings.EMBED_ROLE_QUERY)
        assert rows.semantic == {"indexed": 3, "eligible": 3}

    def test_a_long_literal_query_keeps_the_keyword_top_first(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _corpus(tmp_path)
        _write(tmp_path, "ticket", "Ticket ZX-4411-QQ needs the pizza order fixed.")
        _build(tmp_path)

        rows = _search(tmp_path, "ZX-4411-QQ needs")

        assert rows[0]["key"] == "ticket"
        assert rows[0]["match"] == "text"

    def test_a_short_literal_query_takes_the_vector_ranking(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _corpus(tmp_path)
        _write(tmp_path, "mention", "Someone said lunch once, then talked about the release.")
        _build(tmp_path)

        rows = _search(tmp_path, "lunch")

        assert len("lunch") < meaning.SEMANTIC_LITERAL_MIN_CHARS
        assert [row["key"] for row in rows][:2] == ["food", "mention"]
        # Both hold the typed word, so both stay text rows in the vector order.
        assert [row["match"] for row in rows][:2] == ["text", "text"]

    def test_a_keyword_hit_led_by_the_vector_ranking_keeps_its_excerpt(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _corpus(tmp_path)
        _build(tmp_path)

        rows = _search(tmp_path, "pipeline")

        ship = next(row for row in rows if row["key"] == "ship")
        assert ship["match"] == "text"
        assert "<<<pipeline>>>" in ship["snippet"] or "pipeline" in ship["snippet"]
        assert all(row["match"] == "meaning" for row in rows if row["key"] != "ship")

    def test_a_partial_index_puts_keyword_rows_first(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        _corpus(tmp_path)
        _build(tmp_path)
        _write(tmp_path, "late", "A note about the pizza pipeline nobody embedded yet.")
        monkeypatch.setattr(meaning, "SEMANTIC_MIN_COVERAGE", 0.9)

        rows = _search(tmp_path, "pipeline")

        assert rows.semantic == {"indexed": 3, "eligible": 4}
        matches = [row["match"] for row in rows]
        assert matches[: matches.count("text")] == ["text"] * matches.count("text")
        assert {row["key"] for row in rows if row["match"] == "text"} == {"ship", "late"}

    def test_an_excluded_term_keeps_the_keyword_rows(self, tmp_path: Path, leg_on: _Embedder):
        _corpus(tmp_path)
        _build(tmp_path)
        rows = _search(tmp_path, "pipeline -pizza")
        assert all("match" not in row for row in rows)
        assert leg_on.calls == []


class TestPrivacyAndOwnership:
    def test_a_foreign_session_from_the_vector_leg_is_dropped(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _corpus(tmp_path)
        _build(tmp_path)

        rows = _search(tmp_path, "when did we ship the rollout", keys={"db", "food"})

        assert "ship" not in {row["key"] for row in rows}
        assert rows.semantic is None

    def test_a_deleted_session_never_ranks(self, tmp_path: Path, leg_on: _Embedder):
        _corpus(tmp_path)
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        assert _search(tmp_path, "when did we ship the rollout")[0]["key"] == "ship"

        assert log.delete_session("ship") is True
        rows = _search(tmp_path, "when did we ship the rollout")

        assert "ship" not in {row["key"] for row in rows}

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_a_session_made_private_never_ranks_even_while_its_vectors_remain(
        self, tmp_path: Path, leg_on: _Embedder, mode: str
    ):
        _corpus(tmp_path)
        _build(tmp_path)
        _write(tmp_path, "ship", "The rollout went through the pipeline.", mode=mode)

        rows = _search(tmp_path, "when did we ship the rollout")

        assert "ship" not in {row["key"] for row in rows}
        assert rows.semantic == {"indexed": 2, "eligible": 2}

    def test_a_private_session_in_the_window_is_never_a_candidate(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        _corpus(tmp_path)
        _build(tmp_path)
        _write(tmp_path, "ship", "The rollout went through the pipeline.", mode="incognito")
        offered: list[set[str]] = []
        rank = meaning.VectorLeg.rank

        def spy(self, query, candidates, embedder, signature, accept=None):
            offered.append(set(candidates))
            return rank(self, query, candidates, embedder, signature, accept)

        monkeypatch.setattr(meaning.VectorLeg, "rank", spy)

        _search(tmp_path, "when did we ship the rollout")

        assert offered == [{"db", "food"}]

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_the_snippet_read_refuses_a_session_made_private(
        self, tmp_path: Path, leg_on: _Embedder, mode: str
    ):
        _corpus(tmp_path)
        _write(tmp_path, "ship", "The rollout went through the pipeline.", mode=mode)
        log = ConversationLog(base_dir=tmp_path)
        hit = meaning.MeaningHit("ship", 1.0, 0, 0, 10)

        assert meaning.meaning_snippet(log, "ship", hit) is None
        assert meaning.meaning_snippet(log, "db", hit._replace(key="db")) == "We moved t…"

    def test_private_sessions_never_move_the_counts(self, tmp_path: Path, leg_on: _Embedder):
        visible = tmp_path / "visible"
        mixed = tmp_path / "mixed"
        for base in (visible, mixed):
            base.mkdir()
            _corpus(base)
        for index, mode in enumerate(["incognito", "temporary"]):
            _write(mixed, f"p{index}", "private pipeline release talk", mode=mode)
        for base in (visible, mixed):
            _build(base)

        counts = [
            _search(base, "when did we ship the rollout").semantic for base in (visible, mixed)
        ]

        assert counts[0] == counts[1] == {"indexed": 3, "eligible": 3}


class TestReviewFixes:
    """The cap filled from the caller's rows, coverage over the scan window, one shared matrix."""

    def test_the_cap_is_filled_from_the_callers_own_rows(self, tmp_path: Path, leg_on: _Embedder):
        for index in range(meaning.SEMANTIC_MAX_ROWS):
            _write(tmp_path, f"other{index}", "pipeline release rollout", age=3600 + index)
        _write(tmp_path, "mine", "lunch pizza dinner and one rollout", age=7200)
        _build(tmp_path)

        everyone = _search(tmp_path, "deploy")
        mine = _search(tmp_path, "deploy", meaning_accept=lambda meta: meta["key"] == "mine")

        assert "mine" not in {row["key"] for row in everyone}
        assert [(row["key"], row["match"]) for row in mine] == [("mine", "meaning")]

    def test_a_workspace_scoped_tool_search_finds_a_hit_past_the_cap(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew import mcp_core

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
        base = tmp_path / "sessions"
        base.mkdir()
        for index in range(meaning.SEMANTIC_MAX_ROWS):
            _write(
                base, f"other{index}", "pipeline release rollout", age=3600 + index, workspace="w2"
            )
        _write(base, "mine", "lunch pizza dinner and one rollout", age=7200)
        _build(base)
        try:
            out = mcp_core._call_tool_inner("search_chat_history", {"query": "deploy"})
        finally:
            ConversationLog(base_dir=base)._catalog_projection.vector_leg.release()

        assert "`mine`  ·  meaning match" in out
        assert "other0" not in out

    def test_coverage_counts_the_scan_window_only(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew import history

        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 3)
        _write(tmp_path, "db", "We moved the schema to postgres.", age=3600)
        _write(tmp_path, "ship", "The rollout went through the pipeline.", age=3700)
        _write(tmp_path, "food", "We ordered pizza for lunch.", age=3800)
        _write(tmp_path, "old", "An old pipeline release note.", age=3900)
        _build(tmp_path)

        # No session holds the word, so every row comes from the vector leg.
        rows = _search(tmp_path, "deploy")

        assert rows.semantic == {"indexed": 3, "eligible": 3}
        assert rows[0]["match"] == "meaning", "a fully embedded window routes meaning first"

    def test_legs_over_one_directory_share_one_matrix_and_one_timer(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        timers: list[object] = []

        class _Timer:
            cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        def _start(secs, callback):
            timers.append(_Timer())
            return timers[-1]

        loads: list[str] = []
        load = meaning._load_matrix

        def _counted(store, signature, dim, fingerprint):
            loads.append(signature)
            return load(store, signature, dim, fingerprint)

        monkeypatch.setattr(meaning, "_start_idle_timer", _start)
        monkeypatch.setattr(meaning, "_load_matrix", _counted)
        _corpus(tmp_path)
        _build(tmp_path)
        logs = [ConversationLog(base_dir=tmp_path) for _ in range(3)]
        try:
            for log in logs:
                log.search_sessions("when did we ship the rollout")
            legs = [log._catalog_projection.vector_leg for log in logs]

            assert legs[0]._state is legs[1]._state is legs[2]._state
            assert loads == ["topics-1"]
            assert [timer.cancelled for timer in timers] == [True, True, False]
        finally:
            logs[0]._catalog_projection.vector_leg.release()


class TestPickerAndStore:
    """The channel picker's fallback and cap, the store revision and the per-test reset."""

    @pytest.mark.asyncio
    async def test_meaning_rows_do_not_suppress_the_pickers_title_fallback(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew import history
        from kiro_crew.messaging.session_resume import resolve_session_choices

        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 3)
        for index, text in enumerate(["schema postgres", "pipeline rollout", "pizza lunch"]):
            _write(tmp_path, f"dashboard_chat-{index}", text, age=3600 + index)
        _write(
            tmp_path, "dashboard_chat-old", "Older notes.", age=7200, title="Quarterly Llama Budget"
        )
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)

        choices, _total, _note = await resolve_session_choices(
            log, "quarterly llama budget", lambda text, _max: text
        )

        assert (choices[0].key, choices[0].related) == ("dashboard:chat-old", False)
        assert any(choice.related for choice in choices[1:]), "the meaning rows follow it"

    @pytest.mark.asyncio
    async def test_the_pickers_cap_is_filled_from_sessions_it_can_offer(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        from kiro_crew.messaging.session_resume import resolve_session_choices

        for index in range(meaning.SEMANTIC_MAX_ROWS):
            _write(tmp_path, f"other{index}", "pipeline release rollout", age=3600 + index)
        _write(tmp_path, "dashboard_chat-mine", "lunch pizza dinner and one rollout", age=7200)
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)

        choices, _total, _note = await resolve_session_choices(
            log, "deploy", lambda text, _max: text
        )

        assert [(choice.key, choice.related) for choice in choices] == [
            ("dashboard:chat-mine", True)
        ]

    def test_the_offer_filter_admits_the_native_bucket_by_its_stem(self):
        from kiro_crew.messaging.session_resume import _may_offer_filter

        native = "discord:kirocrew:direct:42"
        may_offer = _may_offer_filter(object(), native)

        assert may_offer({"key": "dashboard_chat-1"})
        assert may_offer({"key": "discord_kirocrew_direct_42"})
        assert may_offer({"key": "discord_kirocrew_direct_42_gen2"})
        assert not may_offer({"key": "discord_kirocrew_direct_420"})
        assert not may_offer({"key": "discord_kirocrew_direct_7"})
        assert not may_offer({"key": "dashboard_chat-1", "memory_mode": "incognito"})
        assert not _may_offer_filter(None, "")({"key": "discord_kirocrew_direct_42"})

    def test_the_revision_moves_when_a_dropped_session_is_replaced_alike(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _write(tmp_path, "a", "The rollout went through the pipeline.")
        _write(tmp_path, "b", "The rollout went through the pipeline.", age=3700)
        _build(tmp_path)
        store = chunking.vector_store_for(tmp_path)
        try:
            before = store.vectors_fingerprint("topics-1")
            assert ConversationLog(base_dir=tmp_path).delete_session("b") is True
            # The same text again: the shared chunk is already embedded, so the
            # reference and vector counts come back as they were.
            _write(tmp_path, "c", "The rollout went through the pipeline.", age=3800)
            chunking.backfill_chunks(ConversationLog(base_dir=tmp_path), store, _TOKENIZER)

            assert len(store.vector_rows("topics-1")) == 2
            assert store.vectors_fingerprint("topics-1") != before
        finally:
            store.close()

    def test_the_reset_releases_every_matrix_and_timer(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        cancelled: list[bool] = []

        class _Timer:
            def cancel(self) -> None:
                cancelled.append(True)

        monkeypatch.setattr(meaning, "_start_idle_timer", lambda secs, callback: _Timer())
        _corpus(tmp_path)
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.search_sessions("when did we ship the rollout")
        state = log._catalog_projection.vector_leg._state
        assert state.matrix is not None and meaning._STATES

        meaning.reset_for_tests()

        assert not meaning._STATES
        assert state.matrix is None and cancelled == [True]


class TestRouteOrder:
    def test_a_short_identifiers_only_text_match_stays_in_the_first_rows(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        for index in range(meaning.SEMANTIC_MAX_ROWS + 1):
            _write(tmp_path, f"other{index}", "alpha beta gamma delta epsilon", age=3600 + index)
        _write(tmp_path, "ticket", "Ticket zx4411 about lunch pizza dinner food", age=7200)
        _build(tmp_path)

        rows = _search(tmp_path, "zx4411", limit=10)

        assert len("zx4411") < meaning.SEMANTIC_LITERAL_MIN_CHARS
        assert rows.semantic == {"indexed": 12, "eligible": 12}
        assert [(row["key"], row["match"]) for row in rows][1] == ("ticket", "text")
        assert rows[0]["match"] == "meaning", "row one stays the vector ranking's"


class TestUnits:
    def test_is_literal_needs_length_and_the_whole_query(self):
        assert meaning.is_literal("ZX-4411-QQ needs", "ticket zx-4411-qq needs the order")
        assert not meaning.is_literal("ZX-4411", "ticket zx-4411-qq needs the order")
        assert not meaning.is_literal("ZX-4411-QQ fixed", "ticket zx-4411-qq needs the order")
        assert not meaning.is_literal("ZX-4411-QQ needs", None)

    def test_the_chunk_snippet_redacts_the_whole_message_before_the_cut(self):
        from kiro_crew.security import redact

        text = "pad " * 60 + "\n  password: hunter2hunter2\n" + "pad " * 60
        start = text.index("hunter2")
        hit = meaning.MeaningHit("k", 1.0, 0, start, start + len("hunter2hunter2"))

        assert "hunter2" in meaning.chunk_snippet([text], hit, 30)
        snippet = meaning.chunk_snippet([text], hit, 30, redact)
        assert "hunter2" not in snippet and snippet.strip("…")

    def test_the_meaning_snippet_is_redacted_before_it_is_cut(self, tmp_path: Path):
        text = "pad " * 60 + "\n  password: hunter2hunter2\n" + "pad " * 60
        _write(tmp_path, "k", text)
        start = text.index("hunter2")
        hit = meaning.MeaningHit("k", 1.0, 0, start, start + len("hunter2hunter2"))

        snippet = meaning.meaning_snippet(ConversationLog(base_dir=tmp_path), "k", hit)

        assert snippet and "hunter2" not in snippet

    def test_the_chunk_snippet_is_cut_around_its_centre(self):
        text = "a" * 100 + "MIDDLE" + "b" * 100
        hit = meaning.MeaningHit("k", 1.0, 0, 0, len(text))
        snippet = meaning.chunk_snippet([text], hit, 20)
        assert "MIDDLE" in snippet and snippet.startswith("…") and snippet.endswith("…")
        assert meaning.chunk_snippet([text], hit._replace(message_index=5), 20) == ""

    def test_the_fingerprint_moves_when_a_session_is_chunked_again(
        self, tmp_path: Path, leg_on: _Embedder
    ):
        _corpus(tmp_path)
        _write(tmp_path, "food2", "We ordered pizza for lunch and talked about dinner.")
        _build(tmp_path)
        store = chunking.vector_store_for(tmp_path)
        try:
            before = store.vectors_fingerprint("topics-1"), store.vector_rows("topics-1")
            # food's text moves to a chunk db already holds, and its old chunk stays
            # with food2: every count comes back the same, yet food's row changed.
            _write(
                tmp_path,
                "food",
                "We moved the schema to postgres and ran the migration on the table.",
            )
            chunking.backfill_chunks(ConversationLog(base_dir=tmp_path), store, _TOKENIZER)
            after = store.vectors_fingerprint("topics-1"), store.vector_rows("topics-1")
            assert sorted(after[1]) != sorted(before[1])
            assert after[0] != before[0]
        finally:
            store.close()

    def test_the_matrix_is_released_after_the_embedders_idle_time(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        armed: list[tuple[float, object]] = []

        class _Timer:
            cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        def _start(secs, callback):
            armed.append((secs, callback))
            return _Timer()

        monkeypatch.setattr(meaning, "_start_idle_timer", _start)
        _corpus(tmp_path)
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        leg = log._catalog_projection.vector_leg
        idle = embeddings.embedding_idle_evict_secs()
        assert idle > 0, "the test home keeps the shipped idle time"

        log.search_sessions("when did we ship the rollout")
        assert leg._state.matrix is not None and leg._state.memo
        assert [secs for secs, _callback in armed] == [idle]

        armed[-1][1]()

        assert leg._state.matrix is None and not leg._state.memo

    def test_a_reload_waits_out_a_moving_store(self, tmp_path: Path, leg_on: _Embedder):
        _corpus(tmp_path)
        _build(tmp_path)
        store = chunking.vector_store_for(tmp_path)
        leg = meaning.VectorLeg(store)
        try:
            first = leg.current_matrix("topics-1", _DIM)
            _write(tmp_path, "new", "another pizza dinner")
            _build(tmp_path)
            assert leg.current_matrix("topics-1", _DIM) is first
            first.loaded_at -= meaning._RELOAD_MIN_SECS
            assert leg.current_matrix("topics-1", _DIM) is not first
        finally:
            leg.release()
            store.close()


def _partial(base: Path) -> None:
    """The corpus fully embedded, then one more session no fill has reached."""
    _corpus(base)
    _build(base)
    _write(base, "late", "A note nobody embedded yet.")


class TestSurfaces:
    """RFC 8.2 and 8.3: each surface carries the match kind, the chunk and the note."""

    @staticmethod
    def _app(log: ConversationLog):
        from types import SimpleNamespace

        from aiohttp import web

        from kiro_crew.dashboard.handlers import api_sessions_search

        app = web.Application()
        app["state"] = SimpleNamespace(conversation_log=log)
        app.router.add_get("/api/sessions/search", api_sessions_search)
        return app

    @pytest.mark.asyncio
    @pytest.mark.parametrize("excluding", [False, True])
    async def test_the_dashboard_search_carries_the_chunk_and_the_note(
        self, tmp_path: Path, leg_on: _Embedder, excluding: bool
    ):
        from aiohttp.test_utils import TestClient, TestServer

        _partial(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        # No word of the query is in the transcript, so only the chunk can show why.
        params = {"q": "deploy"}
        if excluding:
            params["exclude_incognito"] = "1"
        try:
            async with TestClient(TestServer(self._app(log))) as client:
                body = await (await client.get("/api/sessions/search", params=params)).json()
                top = body["sessions"][0]
                assert (top["key"], top["match"]) == ("ship", "meaning")
                assert "pipeline" in top["snippet"]
                assert body["semantic"] == {"indexed": 3, "eligible": 4}

                _build(tmp_path)
                body = await (await client.get("/api/sessions/search", params=params)).json()
                assert "semantic" not in body, "a complete index says nothing"
        finally:
            log._catalog_projection.vector_leg.release()

    def test_the_message_search_keeps_the_keyword_ranking(self, tmp_path: Path, leg_on: _Embedder):
        from kiro_crew.dashboard.handlers.sessions_parts import message_search

        _corpus(tmp_path)
        _build(tmp_path)
        log = ConversationLog(base_dir=tmp_path)

        out, ranked, _agents = message_search._search(
            log, "pipeline", "", "", None, frozenset({"user"}), 10
        )

        assert [row["key"] for row in out] == ["ship"]
        assert all("match" not in row for row in ranked)
        assert getattr(ranked, "semantic", None) is None
        assert leg_on.calls == []

    def test_search_chat_history_names_the_match_kind(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew import mcp_core

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
        base = tmp_path / "sessions"
        base.mkdir()
        _partial(base)
        try:
            # No word of the query is in the transcript, so only the chunk can show why.
            out = mcp_core._call_tool_inner("search_chat_history", {"query": "deploy"})
        finally:
            ConversationLog(base_dir=base)._catalog_projection.vector_leg.release()

        assert "_Meaning search covers 3 of 4 sessions so far._" in out
        head, _sep, rest = out.partition("`ship`")
        assert rest.startswith("  ·  meaning match (may not contain your words)")
        assert "pipeline" in rest.split("\n---")[0]

    def test_search_chat_history_without_the_leg_prints_todays_lines(
        self, tmp_path: Path, leg_on: _Embedder, monkeypatch: pytest.MonkeyPatch
    ):
        from kiro_crew import mcp_core

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:global-v1")
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: False)
        base = tmp_path / "sessions"
        base.mkdir()
        _partial(base)

        out = mcp_core._call_tool_inner("search_chat_history", {"query": "pipeline"})

        assert "**session ship**  ·  `ship`\n" in out
        assert "text match" not in out and "meaning match" not in out
        assert "Meaning search" not in out

    @pytest.mark.asyncio
    async def test_the_channel_picker_marks_a_meaning_row(self, tmp_path: Path, leg_on: _Embedder):
        from kiro_crew.messaging.session_resume import RELATED_MARKER, resolve_session_choices

        _write(tmp_path, "dashboard_chat-db", "We moved the schema to postgres for the table.")
        _write(tmp_path, "dashboard_chat-ship", "The rollout went through the pipeline.")
        _write(tmp_path, "dashboard_chat-food", "We ordered pizza for lunch.")
        _build(tmp_path)
        _write(tmp_path, "dashboard_chat-late", "A note nobody embedded yet.")
        log = ConversationLog(base_dir=tmp_path)
        try:
            choices, _total, note = await resolve_session_choices(
                log, "when did we ship the rollout", lambda text, _max: text
            )
        finally:
            log._catalog_projection.vector_leg.release()

        top = choices[0]
        assert (top.key, top.related) == ("dashboard:chat-ship", True)
        assert top.label == RELATED_MARKER + top.title
        assert not top.title.startswith(RELATED_MARKER), "a resume notice quotes the bare title"
        assert note == "Meaning search covers 3 of 4 sessions so far."
