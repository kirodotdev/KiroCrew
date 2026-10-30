"""Tests for how vector memory recall selects and ranks the memories it returns.

They pin Snowball stemming in keyword scoring, the keyword fallback that keeps two-character
CJK words, per-tag recency decay and its rate sanitizer, MMR reranking over the full
candidate pool (cached Jaccard, negative scores, the FAISS tier's pool parity with the
sqlite tiers), the bounded FAISS search window, and the startup cap on ``pref.*`` rows that
keeps the rows matching the request. The store core lives in ``test_vector_memory.py``, and
stored vectors and FAISS index upkeep in ``test_vector_memory_embedding_lifecycle.py``.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew.vector_memory import (
    _HAS_FAISS,
    _HAS_NUMPY,
    _MMR_MAX_POOL,
    VectorMemoryStore,
    _get_snowball,
    _jaccard,
    _mmr_rerank,
    _sanitize_decay_rates,
    _stem_one,
    _stem_words,
    _tokenize,
)

# Same xdist group as ``test_vector_memory.py`` and the topic files its docstring names, so
# this file's tests share that group's worker; nothing in this file reads the source tree.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


class TestStemWords:
    """Tests for Snowball stemming in keyword scoring."""

    def test_preserves_originals(self) -> None:
        words = {"testing", "run"}
        result = _stem_words(words)
        assert "testing" in result
        assert "run" in result

    def test_adds_stems(self) -> None:
        result = _stem_words({"testing"})
        assert "test" in result

    def test_morphological_variants_overlap(self) -> None:
        pairs = [
            ({"testing"}, {"tests"}),
            ({"deployment"}, {"deploy"}),
            ({"shipped"}, {"shipping"}),
            ({"fixes"}, {"fixed"}),
            ({"running"}, {"runs"}),
        ]
        for a, b in pairs:
            assert _stem_words(a) & _stem_words(b), f"{a} and {b} should share a stem"

    def test_short_words_unchanged(self) -> None:
        result = _stem_words({"bug", "run", "fix"})
        assert {"bug", "run", "fix"} <= result

    def test_memoized_stem_matches_the_batch_stemmer(self) -> None:
        """The per-word cache must not change what stemming produces.

        The previous implementation stemmed a whole set in one ``stemWords``
        call. Memoizing per word is only safe if it yields the same set, so
        pin that against the stemmer directly rather than against itself.
        """
        words = {
            "testing",
            "deployment",
            "shipped",
            "fixes",
            "running",
            "caches",
            "relevance",
            "ranked",
            "lessons",
            "workspaces",
            "bug",
            "run",
        }
        direct = words | set(_get_snowball().stemWords(sorted(words)))

        assert _stem_words(words) == direct
        # And per single word, where a batch call cannot mask an ordering bug.
        for word in words:
            assert _stem_words({word}) == {word} | set(_get_snowball().stemWords([word]))

    def test_repeated_words_are_stemmed_once(self) -> None:
        """A word seen again is served from the cache instead of re-stemmed."""
        _stem_one.cache_clear()
        _stem_words({"provisioning"})
        after_first = _stem_one.cache_info()
        _stem_words({"provisioning"})
        after_second = _stem_one.cache_info()

        assert after_first.misses == 1
        assert after_second.misses == 1, "second call must not re-stem"
        assert after_second.hits == after_first.hits + 1


class TestMmrJaccardCacheAndRecall:
    """_mmr_rerank keeps the FULL candidate pool and memoizes the query-independent
    pairwise Jaccard, rather than truncating the pool toward `limit`.

    Truncating by relevance would silently drop a relevant-but-diverse tail item that
    MMR is specifically meant to surface. Recall is
    preserved by keeping the pool; cost is reduced by computing each candidate↔candidate
    similarity at most once (it depends only on the two token sets, not the query).
    """

    @staticmethod
    def _cands(n: int) -> list[dict]:
        return [{"text": f"fragment number {i} alpha{i}", "score": float(n - i)} for i in range(n)]

    def test_diverse_tail_item_is_selectable(self) -> None:
        # A low-relevance but highly DIVERSE item
        # must remain selectable. Top items are near-duplicates; one tail item is
        # unrelated. With limit=2, MMR must pick a top item + the diverse tail item.
        cands = [
            {"text": "python backend api server flask", "score": 0.90},
            {"text": "python backend api server django", "score": 0.88},
            {"text": "python backend api server fastapi", "score": 0.86},
            {"text": "python backend api server tornado", "score": 0.84},
            {"text": "kubernetes deployment yaml helm chart", "score": 0.50},
        ]
        got = _mmr_rerank([dict(c) for c in cands], limit=2)
        texts = {c["text"] for c in got}
        assert "kubernetes deployment yaml helm chart" in texts, (
            "MMR must still be able to select the diverse tail item; truncating the "
            "pool toward `limit` would drop it"
        )

    def test_cached_sim_matches_direct_jaccard(self) -> None:
        # The memoized pairwise similarity must equal a direct _jaccard computation —
        # caching is a speedup, never a behavior change.
        cands = self._cands(40)
        # Recreate the token sets the same way _mmr_rerank does.
        toks = [_tokenize(c["text"]) for c in cands]
        # Result must be deterministic and identical across repeated calls (cache is
        # per-call, so two calls exercise it independently and must agree).
        a = _mmr_rerank([dict(c) for c in cands], limit=6)
        b = _mmr_rerank([dict(c) for c in cands], limit=6)
        assert [c["text"] for c in a] == [c["text"] for c in b]
        # The helper the cache wraps is symmetric and in [0, 1].
        assert _jaccard(toks[0], toks[1]) == _jaccard(toks[1], toks[0])
        assert 0.0 <= _jaccard(toks[0], toks[1]) <= 1.0

    def test_full_pool_preserved_for_pure_relevance(self) -> None:
        # With strictly descending, well-separated scores and distinct text, MMR picks
        # the top `limit` by relevance — and the pool is NOT pre-truncated.
        cands = self._cands(500)
        got = _mmr_rerank(list(cands), limit=6)
        assert [c["text"] for c in got] == [c["text"] for c in cands[:6]]

    def test_recall_safe_ceiling_keeps_highest_relevance(self) -> None:
        # The only bound is a recall-safe ceiling far above realistic pools. If a
        # pathological pool exceeds it, the highest-relevance rows are kept.
        n = _MMR_MAX_POOL + 50
        cands = self._cands(n)  # score = n-i, so index 0 is highest
        got = _mmr_rerank(list(cands), limit=6)
        assert [c["text"] for c in got] == [c["text"] for c in cands[:6]]

    def test_small_pool_unaffected(self) -> None:
        cands = self._cands(5)
        got = _mmr_rerank(list(cands), limit=6)
        assert len(got) == 5
        assert got[0]["text"] == cands[0]["text"]

    def test_max_pool_constant_sane(self) -> None:
        assert _MMR_MAX_POOL >= 100  # comfortably above any realistic episodic pool


class TestMmrRerankNegativeScores:
    """Regression: MMR relevance normalization inverted ranking for negative scores.

    ``score = cosine_sim * (0.7 + 0.3*importance) * exp(-0.03*days)``. The index is
    ``faiss.IndexFlatIP`` (inner product on normalized vectors = cosine in [-1, 1]),
    so ``cosine_sim`` — and therefore ``score`` — can be NEGATIVE for a query that is
    dissimilar to the stored memories. The normalizer was::

        max_score = max(c[score_key] for c in candidates) or 1.0
        ...
        relevance = candidates[idx][score_key] / max_score

    The ``or 1.0`` only guards ``max_score == 0``. When every score is negative,
    ``max_score`` is negative and ``score / max_score`` GROWS as the true score gets
    worse (e.g. -1.0 / -0.1 = +10.0 vs -0.1 / -0.1 = +1.0), so MMR selects the LEAST
    relevant candidate first — an inverted ranking in the core recall path. The fix
    (``if max_score <= 0: max_score = 1.0``) is folded into this CR alongside the
    full-pool + cached-Jaccard rework, since both touch ``_mmr_rerank``.
    """

    def test_all_negative_scores_keep_best_first(self):
        # Distinct texts so the diversity term doesn't dominate; sorted desc by score.
        cands = [
            {"text": "alpha topic one", "score": -0.10},  # best (least negative)
            {"text": "beta topic two", "score": -0.20},
            {"text": "gamma topic three", "score": -0.50},
            {"text": "zeta topic four", "score": -1.00},  # worst
        ]
        out = _mmr_rerank(cands, limit=2)
        assert out[0]["score"] == -0.10, (
            f"MMR selected score {out[0]['score']} first; the best (least-negative, "
            "-0.10) candidate must rank first — negative max_score inverted the order"
        )
        # Confirm the *ordering*, not just the first pick: with near-zero diversity
        # (distinct texts → low Jaccard) the second pick should be the next-best score.
        assert out[1]["score"] == -0.20

    def test_mixed_sign_scores_best_first(self):
        cands = [
            {"text": "alpha relevant", "score": 0.50},
            {"text": "beta neutral", "score": 0.00},
            {"text": "gamma anti", "score": -0.50},
        ]
        out = _mmr_rerank(cands, limit=1)
        assert out[0]["score"] == 0.50

    def test_all_zero_scores_does_not_crash(self):
        # The historical `or 1.0` guard for the all-zero case must still hold.
        cands = [{"text": f"t{i}", "score": 0.0} for i in range(4)]
        out = _mmr_rerank(cands, limit=2)
        assert len(out) == 2

    def test_positive_scores_unchanged(self):
        cands = [
            {"text": "alpha", "score": 0.90},
            {"text": "beta", "score": 0.50},
            {"text": "gamma", "score": 0.10},
        ]
        out = _mmr_rerank(cands, limit=1)
        assert out[0]["score"] == 0.90

    def test_all_negative_similar_texts_returns_all_requested(self):
        # review-bot edge case: all-negative scores + identical token sets push the MMR
        # value to <= -1.0 (e.g. relevance=-1, max_sim=1 -> 0.6*-1 - 0.4*1 = -1.0). A
        # best_mmr floor of -1.0 with strict `>` would select nothing and break early,
        # returning fewer than `limit` results. With best_mmr=-inf, all are returned.
        cands = [{"text": "identical token set here", "score": -1.0} for _ in range(3)]
        out = _mmr_rerank([dict(c) for c in cands], limit=3)
        assert len(out) == 3, f"expected 3 results, got {len(out)} (early-break regression)"

    def test_very_negative_scores_first_iteration_not_empty(self):
        # Scores more negative than -1.0 (cosine * positive factors can exceed [-1,1]
        # in magnitude after weighting) must not yield an empty result on iteration 1
        # (where max_sim=0 → mmr = lam*relevance, which can be < -1.0).
        cands = [
            {"text": "alpha unique words", "score": -5.0},
            {"text": "beta different words", "score": -6.0},
        ]
        out = _mmr_rerank([dict(c) for c in cands], limit=2)
        assert len(out) == 2


@pytest.mark.skipif(not (_HAS_FAISS and _HAS_NUMPY), reason="faiss/numpy not available")
class TestBoundedFaissRecall:
    class _Index:
        def __init__(self, indices, distances, ntotal):
            self.ntotal = ntotal
            self.indices = indices
            self.distances = distances
            self.requested = None

        def search(self, vector, k):
            import numpy as np

            self.requested = k
            return (
                np.asarray([self.distances[:k]], dtype=np.float32),
                np.asarray([self.indices[:k]], dtype=np.int64),
            )

    @staticmethod
    def _store(tmp_path: Path, *, opened) -> tuple[VectorMemoryStore, str]:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=4))
        store.init()
        assert store.write_episodic(
            "The selected database is PostgreSQL", embedding=[1.0, 0.0, 0.0, 0.0]
        )
        row_id = store.get_episodic_list()[0]["id"]
        return store, row_id

    def test_native_hit_fetch_uses_index_score_without_embedding_blob(
        self, tmp_path: Path, opened
    ) -> None:
        store, row_id = self._store(tmp_path, opened=opened)
        index = self._Index([0], [0.9], 1)
        store._faiss_index = index
        store._faiss_id_map = [row_id]
        store._faiss_data_version = store._sqlite_data_version()
        real_batch = store._get_episodic_batch
        fetched = []

        def observe_batch(ids):
            fetched.append(ids)
            return real_batch(ids)

        with mock.patch.object(store, "_get_episodic_batch", side_effect=observe_batch):
            result = store.search_episodic(query_embedding=[1.0, 0.0, 0.0, 0.0], limit=8, mmr=False)

        assert [row["id"] for row in result] == [row_id]
        assert fetched == [[row_id]]
        assert "embedding" not in result[0]
        assert index.requested == 1

    def test_fixed_native_window_falls_back_when_ghosts_starve_results(
        self, tmp_path: Path, opened
    ) -> None:
        store, row_id = self._store(tmp_path, opened=opened)
        index = self._Index(list(range(16)), [0.99] * 16, 3001)
        store._faiss_index = index
        store._faiss_id_map = [f"deleted-{i}" for i in range(3000)] + [row_id]
        store._faiss_data_version = store._sqlite_data_version()

        with mock.patch.object(
            store, "_ineligible_ids", wraps=store._ineligible_ids
        ) as eligibility:
            result = store.search_episodic(query_embedding=[1.0, 0.0, 0.0, 0.0], limit=8, mmr=False)

        assert index.requested == 16
        assert len(eligibility.call_args_list[0].args[0]) == 16
        assert [row["id"] for row in result] == [row_id]


def test_eligible_rows_scopes_ineligibility_to_candidate_ids_no_full_scan(
    tmp_path: Path, opened
) -> None:
    """_eligible_rows looks up ineligibility by the candidate rows' own ids
    (through the record_id PRIMARY KEY) rather than scanning all of
    memory_record_meta -- a table that only grows as records are revised.
    """
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    store.init()

    # A few live preference facts: these are the candidate rows a session-start
    # render passes to _eligible_rows.
    for i in range(3):
        assert store.set_semantic(f"pref.topic.k{i}", f"v{i}", 0.9, "user_explicit") is None

    # The meta table grows with non-active revisions over time. Seed many so a
    # full scan of the unfiltered predicate would touch far more rows than the
    # handful of candidates.
    with store._db_lock:
        for i in range(2000):
            store.db.execute(
                "INSERT INTO memory_record_meta (record_id, kind, revision, status, "
                "content_hash, updated_at) VALUES (?,?,?,?,?,?)",
                (f"key:dead-{i}", "fact", 1, "superseded", "h", "2026-01-01"),
            )
        store.db.commit()

    rows = store._fetch_all_locked(
        "SELECT key, value_json FROM semantic_memory "
        "WHERE is_deleted = 0 AND key LIKE 'pref.%' ORDER BY key"
    )
    assert len(rows) == 3

    # The ineligibility lookup is scoped to the candidate ids, never issued
    # unfiltered (record_ids=None), so it rides the primary key.
    with mock.patch.object(store, "_ineligible_ids", wraps=store._ineligible_ids) as eligibility:
        kept = store._eligible_rows(rows, "fact")

    assert eligibility.call_count == 1
    passed_ids = eligibility.call_args_list[0].args[0]
    assert passed_ids is not None
    assert sorted(passed_ids) == [f"key:pref.topic.k{i}" for i in range(3)]
    assert all(key.startswith("key:pref.topic.k") for key in passed_ids)

    # All three live facts survive; the 2000 dead rows never enter the lookup.
    assert [row["key"] for row in kept] == [f"pref.topic.k{i}" for i in range(3)]

    # The query the fix issues uses the record_id PRIMARY KEY, not a full scan.
    predicate = "status != 'active' OR valid_from != '' OR valid_until != ''"
    placeholders = ",".join("?" * len(passed_ids))
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT record_id, status, valid_from, valid_until "
        f"FROM memory_record_meta WHERE record_id IN ({placeholders}) AND ({predicate})",
        tuple(passed_ids),
    ).fetchall()
    detail = " ".join(str(step[-1]) for step in plan)
    assert "SEARCH" in detail and "record_id" in detail
    assert "SCAN memory_record_meta" not in detail

    # Scoping does not change the eligibility verdict: the restricted lookup
    # agrees byte-for-byte with the unfiltered one over these candidate ids.
    scoped = store._ineligible_ids(list(passed_ids))
    unfiltered = store._ineligible_ids()
    assert scoped == {rid for rid in unfiltered if rid in set(passed_ids)}


class TestSanitizeDecayRates:
    """_sanitize_decay_rates: user-edited JSON is screened, not trusted."""

    def test_clamps_to_bounds(self) -> None:
        out = _sanitize_decay_rates({"fast": 99.0, "neg": -5.0, "ok": 0.5})
        assert out == {"fast": 10.0, "neg": 0.0, "ok": 0.5}

    def test_oversized_json_integers_clamp_instead_of_crashing(self) -> None:
        # json.loads parses arbitrary-precision integers; float()/math.isfinite()
        # raise OverflowError past ~1e308, which would abort store construction
        # (gateway boot) on a garbage config value. Clamping must happen first.
        big = 10**400
        out = _sanitize_decay_rates({"huge": big, "neghuge": -big})
        assert out == {"huge": 10.0, "neghuge": 0.0}

    def test_ignores_non_numeric_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
            out = _sanitize_decay_rates(
                {"legal": "forever", "flag": True, "nan": float("nan"), "ok": 0.1}
            )
        assert out == {"ok": 0.1}
        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len([m for m in warns if "decay_rates" in m]) == 3, warns

    def test_lowercases_and_strips_keys(self) -> None:
        assert _sanitize_decay_rates({" Legal ": 0.0}) == {"legal": 0.0}

    def test_empty_and_none_yield_empty(self) -> None:
        assert _sanitize_decay_rates(None) == {}
        assert _sanitize_decay_rates({}) == {}


class TestEpisodicDecayRates:
    """Per-tag configurable recency decay (memory.decay_rates) in retrieval scoring."""

    _DIM = 4

    @staticmethod
    def _vec() -> list[float]:
        # Unit vector: query == stored embedding gives cosine_sim of exactly 1.0.
        return [1.0, 0.0, 0.0, 0.0]

    def _store(
        self, tmp_path: Path, rates: dict[str, object] | None = None, *, opened
    ) -> VectorMemoryStore:
        store = opened(
            VectorMemoryStore(
                db_path=tmp_path / "mem.db",
                embedding_dim=self._DIM,
                decay_rates=rates,  # type: ignore[arg-type]
            )
        )
        store.init()
        return store

    def _write_backdated(
        self,
        store: VectorMemoryStore,
        text: str,
        days: int,
        tags: list[str] | None = None,
    ) -> None:
        assert store.write_episodic(text, embedding=self._vec(), tags=tags)
        # The extra hour keeps (now - created).days at exactly `days` for the
        # duration of the test regardless of sub-second timing.
        created = (datetime.now(tz=timezone.utc) - timedelta(days=days, hours=1)).isoformat()
        store.db.execute(
            "UPDATE episodic_memories SET created_at = ? WHERE text = ?", (created, text)
        )
        # init() runs the connection with implicit transactions (isolation_level
        # ""), so the raw UPDATE above opens one that must be committed here --
        # otherwise the next write_episodic's BEGIN IMMEDIATE raises "cannot
        # start a transaction within a transaction".
        store.db.commit()

    def _score(self, store: VectorMemoryStore, text: str) -> float:
        results = store.search_episodic(query_embedding=self._vec(), query_text="q", mmr=False)
        by_text = {r["text"]: r["score"] for r in results}
        assert text in by_text, f"memory {text!r} not retrieved: {sorted(by_text)}"
        return by_text[text]

    @staticmethod
    def _expected(rate: float, days: int) -> float:
        # cosine_sim = 1.0, default importance 0.5 -> base factor 0.85.
        return 1.0 * (0.7 + 0.3 * 0.5) * math.exp(-rate * days)

    def test_default_rate_unchanged_without_config(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, opened=opened)
        self._write_backdated(store, "note about the database migration", days=10)
        assert self._score(store, "note about the database migration") == pytest.approx(
            self._expected(0.03, 10), abs=1.5e-4
        )

    def test_per_tag_override_applied(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, rates={"trading_data": 1.0}, opened=opened)
        self._write_backdated(store, "yesterday's market prices", days=2, tags=["trading_data"])
        assert self._score(store, "yesterday's market prices") == pytest.approx(
            self._expected(1.0, 2), abs=1.5e-4
        )

    def test_multi_tag_picks_slowest_decay(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, rates={"legal": 0.0, "general": 0.5}, opened=opened)
        self._write_backdated(
            store, "legal reasoning on the contract", days=30, tags=["legal", "general"]
        )
        # Slowest decay (rate 0.0) wins: the broader tag must not age it out.
        assert self._score(store, "legal reasoning on the contract") == pytest.approx(
            self._expected(0.0, 30), abs=1.5e-4
        )

    def test_default_key_overrides_builtin_rate(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, rates={"default": 0.5}, opened=opened)
        self._write_backdated(store, "untagged observation from a session", days=5)
        assert self._score(store, "untagged observation from a session") == pytest.approx(
            self._expected(0.5, 5), abs=1.5e-4
        )

    def test_unmatched_tag_uses_builtin_default(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, rates={"legal": 0.0}, opened=opened)
        self._write_backdated(store, "note tagged outside the config", days=10, tags=["other"])
        assert self._score(store, "note tagged outside the config") == pytest.approx(
            self._expected(0.03, 10), abs=1.5e-4
        )

    def test_tag_matching_is_case_insensitive(self, tmp_path: Path, opened) -> None:
        store = self._store(tmp_path, rates={"LEGAL": 0.0}, opened=opened)
        self._write_backdated(store, "precedent kept forever", days=30, tags=["Legal"])
        assert self._score(store, "precedent kept forever") == pytest.approx(
            self._expected(0.0, 30), abs=1.5e-4
        )

    def test_invalid_values_fall_back_to_default(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, opened
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
            store = self._store(
                tmp_path, rates={"legal": "forever", "default": True}, opened=opened
            )
        self._write_backdated(store, "note with unusable decay config", days=10, tags=["legal"])
        assert self._score(store, "note with unusable decay config") == pytest.approx(
            self._expected(0.03, 10), abs=1.5e-4
        )
        warns = [r.getMessage() for r in caplog.records if "decay_rates" in r.getMessage()]
        assert len(warns) == 2, warns

    def test_faiss_path_applies_configured_rate(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")
        store = self._store(tmp_path, rates={"legal": 0.0}, opened=opened)
        self._write_backdated(store, "faiss path retention check", days=30, tags=["legal"])
        store.build_faiss_index()
        assert store._faiss_index is not None
        assert store._faiss_index.ntotal > 0  # type: ignore[attr-defined]
        assert self._score(store, "faiss path retention check") == pytest.approx(
            self._expected(0.0, 30), abs=1.5e-4
        )


class TestEpisodicKeywordFallbackCjk:
    """The no-embeddings episodic fallback must not discard whole-word CJK queries.

    ``search_episodic`` drops to ``_fts5_episodic_search`` whenever no query
    embedding is available -- the normal state for a deployment with no embedding
    model configured. That fallback matches with ``LIKE '%token%'``, which is a
    plain substring test and needs no tokenizer, so a spaceless CJK term is a
    perfectly good needle. The only thing standing between the query and the row
    is the minimum-token-length filter, which counts CHARACTERS: two characters
    is a stopword in English but an ordinary whole word in Chinese, Japanese and
    Korean, so the filter emptied the term list and the search returned nothing.
    """

    @staticmethod
    def _store(tmp_path: Path, *, opened) -> VectorMemoryStore:
        """A store with no embed_fn, so search_episodic takes the keyword fallback."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        return store

    @pytest.mark.parametrize(
        ("term", "sentence"),
        [
            ("模型", "用户决定用这个模型来做推理"),  # zh: "model"
            ("会議", "チームは会議で方針を決めた"),  # ja: "meeting"
            ("회의", "팀은 회의에서 방침을 정했다"),  # ko: "meeting"
        ],
        ids=["zh", "ja", "ko"],
    )
    def test_a_two_character_word_still_finds_its_row(
        self, tmp_path: Path, term: str, sentence: str, opened
    ) -> None:
        store = self._store(tmp_path, opened=opened)
        assert store.write_episodic(sentence)
        # The row plainly contains the term -- LIKE would match it.
        assert term in sentence
        hits = store.search_episodic(query_text=term, limit=8)
        assert [h["text"] for h in hits] == [sentence]

    def test_a_two_word_cjk_query_is_not_emptied(self, tmp_path: Path, opened) -> None:
        """Both tokens are two characters; the whole query must not filter to []."""
        store = self._store(tmp_path, opened=opened)
        sentence = "我们讨论了模型训练的流程"
        assert store.write_episodic(sentence)
        hits = store.search_episodic(query_text="模型 训练", limit=8)
        assert [h["text"] for h in hits] == [sentence]

    def test_a_longer_cjk_run_keeps_working(self, tmp_path: Path, opened) -> None:
        """Regression guard: >2 characters was already accepted and must stay so."""
        store = self._store(tmp_path, opened=opened)
        sentence = "团队选择了机器学习作为方向"
        assert store.write_episodic(sentence)
        hits = store.search_episodic(query_text="机器学习", limit=8)
        assert [h["text"] for h in hits] == [sentence]

    def test_a_short_latin_query_is_still_filtered_out(self, tmp_path: Path, opened) -> None:
        """Negative control: the English stopword behaviour must NOT widen.

        ``to`` is two ASCII characters. Accepting it would turn ``LIKE '%to%'``
        into a near-universal match, which is exactly what the length filter
        exists to prevent -- so the fix must leave this case refused.
        """
        store = self._store(tmp_path, opened=opened)
        assert store.write_episodic("User decided to use PostgreSQL for the database layer")
        assert store.search_episodic(query_text="to", limit=8) == []

    def test_a_single_character_cjk_query_stays_filtered(self, tmp_path: Path, opened) -> None:
        """One Han character is as unselective as an English stopword: still refused."""
        store = self._store(tmp_path, opened=opened)
        assert store.write_episodic("用户决定用这个模型来做推理")
        assert store.search_episodic(query_text="的", limit=8) == []


class TestPreferencesStartupCap:
    """``pref.*`` rows on the first turn are bounded like every other startup block.

    Below the cap the rendering is byte-identical to the unbounded form; above it
    the rows that speak to the request survive, the rest are deferred to
    memory_recall, and the block SAYS how many were deferred.
    """

    @staticmethod
    def _seed(store, count: int, width: int) -> None:
        for i in range(count):
            store.set_semantic(f"pref.row_{i:02d}", f"filler-{i} " * width, 0.9, "user_explicit")

    def test_under_the_cap_is_byte_identical_to_unbounded(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        self._seed(store, 5, 4)
        unbounded = store.get_preferences_context()
        assert unbounded == store.get_preferences_context(query_text="anything", cap=10_000)
        assert "[Context budget" not in unbounded
        assert unbounded.endswith("\n[End of semantic memory]\n")

    def test_over_the_cap_keeps_whole_rows_and_reports_the_rest(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        self._seed(store, 40, 20)
        # A sweep, not one cap: the room arithmetic reserves the notice at its
        # widest, and a single cap can sit exactly where an off-by-a-few error
        # still fits. Across a range it cannot.
        for cap in range(1_200, 3_000, 37):
            ctx = store.get_preferences_context(cap=cap)
            assert len(ctx) <= cap, cap
            kept = [line for line in ctx.splitlines() if line.startswith("pref.row_")]
            assert 0 < len(kept) < 40, cap
            # Whole rows only: every kept line is a complete `key: value`.
            for line in kept:
                assert line.endswith(" "), cap
            assert f"omitted {40 - len(kept)} of 40 preference facts" in ctx, cap
            assert f"{cap}-character startup budget" in ctx, cap
            assert "memory_recall" in ctx, cap
            # The notice sits between the last row and the footer, never above the rows.
            assert (
                ctx.index(kept[-1])
                < ctx.index("[Context budget")
                < ctx.index("[End of semantic memory]")
            ), cap
            assert ctx.endswith("\n[End of semantic memory]\n"), cap

    def test_rows_matching_the_request_survive_the_cut(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        self._seed(store, 40, 20)
        # Sorted last by key, so it is the first to fall to a key-ordered cut.
        store.set_semantic(
            "pref.zzz_deploy_style", "always rebase before a deploy push " * 3, 0.9, "user_explicit"
        )
        blind = store.get_preferences_context(cap=1_800)
        aimed = store.get_preferences_context(query_text="how should I deploy this", cap=1_800)
        assert "pref.zzz_deploy_style" not in blind
        assert "pref.zzz_deploy_style" in aimed

    def test_a_cap_too_small_for_one_row_still_reports_everything_omitted(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        self._seed(store, 3, 30)
        ctx = store.get_preferences_context(cap=300)
        assert "pref.row_" not in ctx
        assert "omitted 3 of 3 preference facts" in ctx
        assert ctx.startswith("[Semantic Memory")
        assert ctx.endswith("\n[End of semantic memory]\n")


class TestFaissMmrPoolRecallContract:
    """FAISS episodic tier must hand MMR the same recall-contract pool as the
    sqlite tiers.

    The sqlite tiers (per-call and resident) hand ``_mmr_rerank`` their entire
    embedded population, bounded only by ``_MMR_MAX_POOL`` — the pool comment in
    ``_mmr_rerank`` declares truncating toward ``limit`` a recall change. The
    FAISS tier must feed MMR the same full pool rather than a ``limit * 2`` slice
    (16 at the default ``limit=8``), so it does not drop the relevant-but-diverse
    tail pick that is the whole point of MMR.
    """

    dim = 16

    class _MiniFlatIP:
        """Exact IndexFlatIP semantics over a normalized row matrix.

        ``search`` computes true inner products and honors ``k`` exactly like
        FAISS: only the top-``k`` hits come back. Records every ``k`` received
        so tests can pin the pool-size request itself.
        """

        def __init__(self, mat) -> None:
            self._mat = mat
            self.ntotal = len(mat)
            self.received_k: list[int] = []

        def search(self, vec, k):
            import numpy as np

            self.received_k.append(int(k))
            sims = self._mat @ vec.reshape(-1)
            order = np.argsort(-sims)[: int(k)]
            return sims[order].reshape(1, -1), order.reshape(1, -1).astype(np.int64)

    def _build_store(self, tmp_path: Path, monkeypatch, texts_and_vecs):
        """Store with rows in sqlite (real embedding blobs) AND a MiniFlatIP index.

        Both tiers see the identical population, so FAISS-vs-sqlite parity is a
        true recall comparison, not a mock artifact.
        """
        import numpy as np

        import kiro_crew.vector_memory as vm_mod

        monkeypatch.setattr(vm_mod, "_HAS_FAISS", True)
        monkeypatch.setattr(vm_mod, "_HAS_NUMPY", True)

        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.dim)
        store.init()

        today = datetime.now(tz=timezone.utc).isoformat()
        mat = np.zeros((len(texts_and_vecs), self.dim), dtype=np.float32)
        id_map: list[str] = []
        for i, (mem_id, text, vec) in enumerate(texts_and_vecs):
            v = np.asarray(vec, dtype=np.float32)
            v /= np.linalg.norm(v)
            mat[i] = v
            id_map.append(mem_id)
            store.db.execute(
                "INSERT INTO episodic_memories (id, text, embedding, is_deleted, "
                "importance, created_at, last_accessed_at) VALUES (?, ?, ?, 0, 1.0, ?, ?)",
                (mem_id, text, v.tobytes(), today, today),
            )
        store.db.commit()

        index = self._MiniFlatIP(mat)
        store._faiss_index = index
        store._faiss_id_map = id_map
        return store, index

    def _population(self):
        """20 near-duplicate rows outranking one diverse row on pure relevance.

        Duplicates share one token set (pairwise Jaccard 1.0) with cosines
        descending from ~0.995; the diverse row sits at rank 21 with cosine 0.6
        and zero token overlap. With the old ``limit * 2`` pool (16) the diverse
        row never reaches MMR; from the full pool MMR must select it (its MMR
        value 0.6·rel ≈ 0.36 beats a second duplicate's 0.6·rel − 0.4·1.0 ≈ 0.19).
        """
        import numpy as np

        rows = []
        dup_text = "postgres database migration plan step one"
        for i in range(20):
            # e0-dominant with a tiny per-row e1 component: cosine to e0 descends
            # ~0.9950 → ~0.9046 as eps grows, keeping all 20 above the diverse row.
            eps = 0.1 + i * 0.02
            vec = np.zeros(self.dim, dtype=np.float32)
            vec[0], vec[1] = 1.0, eps
            rows.append((f"dup-{i:02d}", dup_text, vec))
        diverse = np.zeros(self.dim, dtype=np.float32)
        diverse[0], diverse[2] = 0.6, 0.8  # cosine 0.6 to e0, disjoint tokens
        rows.append(("diverse-x", "kubernetes ingress rollout canary window", diverse))
        return rows

    def _query(self):
        q = [0.0] * self.dim
        q[0] = 1.0
        return q

    def test_mmr_pool_not_truncated_to_limit_window(self, tmp_path: Path, monkeypatch) -> None:
        """ATTACK: the rank-21 diverse row must be MMR-reachable (full pool)."""
        store, index = self._build_store(tmp_path, monkeypatch, self._population())
        result = store.search_episodic(query_embedding=self._query(), limit=8, mmr=True)
        ids = [r["id"] for r in result]
        assert "diverse-x" in ids, (
            "FAISS tier truncated the MMR candidate pool below the recall "
            f"contract: rank-21 diverse row unreachable (pool k={index.received_k})"
        )
        # The pool request itself must follow the contract, not the limit window.
        assert index.received_k == [21], index.received_k

    def test_faiss_and_sqlite_tiers_return_identical_mmr_results(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """PARITY: identical population → identical MMR selections."""
        store, _ = self._build_store(tmp_path, monkeypatch, self._population())
        q = self._query()
        faiss_ids = [r["id"] for r in store.search_episodic(query_embedding=q, limit=8, mmr=True)]
        store._faiss_index = None  # falls through to the per-call sqlite tier
        sqlite_ids = [r["id"] for r in store.search_episodic(query_embedding=q, limit=8, mmr=True)]
        assert faiss_ids == sqlite_ids

    def test_non_mmr_path_keeps_limit_window(self, tmp_path: Path, monkeypatch) -> None:
        """CONTROL: mmr=False keeps the cheap limit*2 pool (top-limit slice only)."""
        store, index = self._build_store(tmp_path, monkeypatch, self._population())
        result = store.search_episodic(query_embedding=self._query(), limit=8, mmr=False)
        assert index.received_k == [16]  # limit * 2, unchanged contract
        assert len(result) == 8
        assert all(r["id"].startswith("dup-") for r in result)  # pure relevance order

    def test_tag_filtered_mmr_keeps_narrow_window(self, tmp_path: Path, monkeypatch) -> None:
        """ATTACK: a tag_filter with mmr=True must keep the narrow limit*2 window.

        ``_matches_tags`` screens candidates AFTER the FAISS window, and only
        ``_sqlite_vector_search`` masks tags over the complete active population.
        A wide MMR pool would satisfy the ``expected = min(limit, k)`` starvation
        probe with tagged hits inside the top ``_MMR_MAX_POOL`` and never fall
        through, dropping a tagged row ranked past the window. The narrow window
        keeps the probe routing tag-filtered queries to the full-population tier.
        """
        store, index = self._build_store(tmp_path, monkeypatch, self._population())
        store.search_episodic(
            query_embedding=self._query(), limit=8, mmr=True, tag_filter=["anything"]
        )
        assert index.received_k == [16], index.received_k  # limit * 2, not _MMR_MAX_POOL

    def test_small_population_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        """BENIGN: ntotal below both pool bounds → both tiers identical, no new denials."""
        rows = self._population()[:5]
        store, index = self._build_store(tmp_path, monkeypatch, rows)
        q = self._query()
        faiss_ids = [r["id"] for r in store.search_episodic(query_embedding=q, limit=8, mmr=True)]
        assert index.received_k == [5]  # min(_MMR_MAX_POOL, ntotal) = ntotal
        assert len(faiss_ids) == 5
        store._faiss_index = None
        sqlite_ids = [r["id"] for r in store.search_episodic(query_embedding=q, limit=8, mmr=True)]
        assert faiss_ids == sqlite_ids
