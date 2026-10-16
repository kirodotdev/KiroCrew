"""More coverage for ``vector_memory``'s vector-space lifecycle, lesson dedup and ranking.

The "Additional coverage" companion of ``test_vector_memory_coverage.py``, which covers
the store's other less-travelled paths and holds the first lesson-dedup, FAISS-absent
and ranking cases. This module covers FAISS-absent degradation at every entry point,
SQLite vector search ranking, the episodic cap, the lesson-dedup paths
(``TestLessonDedupPaths``), contradiction-candidate banding, the episodic backfill
sweep and the pending-embeddings probe, embedding-space reconciliation, width and
generation changes, the lazy ``embed_fn`` rebind, and the store's lifecycle.

Everything runs against a fresh SQLite file under ``tmp_path`` with a deterministic
in-process embedder -- no network, no model download, no FAISS requirement.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Callable

import pytest
from vector_memory_coverage_helpers import (
    _DIM,
    _episodic_texts,
    _fixed_embed,
    _lesson_texts,
    _raw_semantic_embedding,
    _recorder,
    _store,
)

from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore

# ── Additional coverage: vector-space lifecycle, lesson dedup, ranking ──


def _unit(index: int) -> list[float]:
    """A basis vector of width ``_DIM``, orthogonal to every other basis vector."""
    vec = [0.0] * _DIM
    vec[index % _DIM] = 1.0
    return vec


class _TableEmbedder:
    """Exact-text lookup embedder, so a test can pin two texts at a chosen cosine.

    Falls back to a deterministic, process-independent vector for any text not in
    the table (``hash()`` is salted per process and would make tests order- and
    run-dependent).
    """

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = dict(table or {})
        self.calls: list[str] = []

    def __call__(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if text in self.table:
            return list(self.table[text])
        vec = [0.0] * _DIM
        for word in text.lower().split():
            vec[sum(ord(ch) for ch in word) % _DIM] += 1.0
        if not any(vec):
            vec[0] = 1.0
        return vec


def _raw_episodic_embedding(store: VectorMemoryStore, mem_id: str) -> bytes | None:
    row = store.db.execute(
        "SELECT embedding FROM episodic_memories WHERE id = ?", (mem_id,)
    ).fetchone()
    return None if row is None else row["embedding"]


def _lesson_key(rule: str) -> str:
    import hashlib

    return f"lesson.{hashlib.md5(rule.encode(), usedforsecurity=False).hexdigest()[:12]}"


class TestFaissAbsentDegradation:
    """FAISS is an optional accelerator, so every entry point must no-op without it."""

    def test_build_save_and_load_are_noops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vm, "_HAS_FAISS", False)
        store = _store(tmp_path)
        assert store.build_faiss_index() == 0
        store.save_faiss_index()
        assert not (tmp_path / "memory.faiss").exists()
        assert store.load_faiss_index() is False

    def test_stats_report_an_empty_index(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment stored without any vector at all")
        stats = store.memory_stats()
        assert stats["episodic_active"] == 1
        assert stats["faiss_index_size"] == 0
        assert stats["embedded_count"] == 0


class TestSqliteVectorSearchRanking:
    """The stdlib cosine scan is the ranking path on a stock (FAISS-less) install."""

    _EXACT = "Notes on the deployment pipeline rollout"
    _HALF = "Notes on the kitchen renovation schedule"
    _ORTHOGONAL = "Musings on the tidal patterns offshore"

    def _seed(self, store: VectorMemoryStore) -> None:
        vectors = {
            self._EXACT: _unit(0),
            self._HALF: [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0],
            self._ORTHOGONAL: _unit(1),
        }
        for text, vec in vectors.items():
            assert store.write_episodic(text, embedding=vec)

    def test_results_are_ordered_by_similarity(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        hits = store.search_episodic(query_embedding=_unit(0), limit=5, mmr=False)
        assert [h["text"] for h in hits] == [self._EXACT, self._HALF, self._ORTHOGONAL]
        # Ordering rather than exact floats: strictly decreasing raw cosine.
        assert hits[0]["cosine_sim"] > hits[1]["cosine_sim"] > hits[2]["cosine_sim"]

    def test_limit_truncates_the_ranked_set(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        assert len(store.search_episodic(query_embedding=_unit(0), limit=1, mmr=False)) == 1

    def test_mmr_rerank_preserves_membership(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        hits = store.search_episodic(query_embedding=_unit(0), limit=5, mmr=True)
        assert {h["text"] for h in hits} == {self._EXACT, self._HALF, self._ORTHOGONAL}

    def test_relevance_gate_drops_weak_short_matches(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        kept = [
            h["text"]
            for h in store.search_episodic(
                query_embedding=_unit(0), limit=5, mmr=False, relevance_filter=True
            )
        ]
        assert kept == [self._EXACT]

    def test_relevance_gate_relaxes_for_long_text(self, tmp_path: Path) -> None:
        """The same cosine that fails the short-text gate passes the long-text one."""
        store = _store(tmp_path)
        long_text = "Long form retrospective paragraph about the rollout. " * 8
        assert len(long_text) > vm._EPISODIC_LONG_TEXT_CHARS
        assert store.write_episodic(long_text, embedding=[1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
        hits = store.search_episodic(
            query_embedding=_unit(0), limit=5, mmr=False, relevance_filter=True
        )
        assert [h["text"].strip() for h in hits] == [long_text.strip()]

    def test_a_zero_norm_query_does_not_divide(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        hits = store.search_episodic(query_embedding=[0.0] * _DIM, limit=5, mmr=False)
        assert len(hits) == 3
        assert all(h["cosine_sim"] == 0.0 for h in hits)

    def test_a_hit_records_its_access_timestamp(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        self._seed(store)
        hit = store.search_episodic(query_embedding=_unit(0), limit=1, mmr=False)[0]
        row = store.db.execute(
            "SELECT last_accessed_at FROM episodic_memories WHERE id = ?", (hit["id"],)
        ).fetchone()
        assert row["last_accessed_at"] is not None

    def test_a_second_search_is_debounced_not_rewritten(self, tmp_path: Path) -> None:
        """Proven by DB state, with no wall-clock dependency: the row is untouched."""
        store = _store(tmp_path)
        self._seed(store)
        hit = store.search_episodic(query_embedding=_unit(0), limit=1, mmr=False)[0]
        store.db.execute(
            "UPDATE episodic_memories SET last_accessed_at = 'SENTINEL' WHERE id = ?",
            (hit["id"],),
        )
        store.db.commit()
        store.search_episodic(query_embedding=_unit(0), limit=1, mmr=False)
        row = store.db.execute(
            "SELECT last_accessed_at FROM episodic_memories WHERE id = ?", (hit["id"],)
        ).fetchone()
        assert row["last_accessed_at"] == "SENTINEL"


class TestEpisodicCapEnforcement:
    def test_the_cap_tombstones_the_least_important_entry(self, tmp_path: Path) -> None:
        store = _store(tmp_path, episodic_max=2)
        assert store.write_episodic("The least important fragment of the three", importance=0.1)
        assert store.write_episodic("The middling fragment of the three here", importance=0.5)
        assert store.write_episodic("The most important fragment of the three", importance=0.9)
        active = _episodic_texts(store)
        assert "The least important fragment of the three" not in active
        assert len(active) == 2

    def test_preserve_existing_refuses_rather_than_evicting(self, tmp_path: Path) -> None:
        store = _store(tmp_path, episodic_max=1)
        assert store.write_episodic("The only fragment that fits under the cap")
        assert not store.write_episodic(
            "A second fragment the cap must refuse", preserve_existing=True
        )
        assert _episodic_texts(store) == ["The only fragment that fits under the cap"]


class TestLessonDedupPaths:
    def test_a_substring_of_an_existing_lesson_is_dropped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always pin dependency versions in the manifest")
        assert not store.write_lesson("pin dependency versions")
        assert len(store.get_lessons()) == 1

    def test_a_superstring_replaces_the_existing_lesson(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("pin dependency versions")
        assert store.write_lesson("Always pin dependency versions in the manifest")
        texts = _lesson_texts(store)
        assert len(texts) == 1
        assert "manifest" in texts[0]

    def test_topic_overlap_replaces_the_older_lesson(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Rebase feature branches before pushing them")
        assert store.write_lesson("Rebase branches before pushing, never merge upward")
        texts = _lesson_texts(store)
        assert len(texts) == 1
        assert "never merge upward" in texts[0]

    def test_a_terse_lesson_does_not_supersede_a_detailed_one(self, tmp_path: Path) -> None:
        """The overlap ratio is measured against the LARGER keyword set.

        Against the smaller one it reads "how much of the shorter rule the longer one
        covers", which is ~1.0 for any terse near-truism — so a three-word rule scored
        past the 50% threshold and DELETED eighteen words of real guidance, reporting
        success. Neither rule here is a substring of the other and the store has no
        embedder, so the topic-overlap branch is the only one that can fire.
        """
        detailed = (
            "Shell arguments must always be quoted when you interpolate them into a "
            "bash command, because unquoted globbing silently rewrites every "
            "filesystem path"
        )
        store = _store(tmp_path)
        assert store.write_lesson(detailed)
        assert store.write_lesson("Quote shell arguments")
        texts = _lesson_texts(store)
        assert detailed in texts
        assert "Quote shell arguments" in texts

    def test_a_negative_example_is_stored_as_its_own_field(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Quote shell arguments", negative="bare interpolation")
        decoded = json.loads(store.get_lessons()[0]["value_json"])
        assert decoded["negative"] == "bare interpolation"

    def test_a_non_user_source_lowers_the_confidence(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Quote every shell argument you interpolate", source="migration")
        assert store.get_lessons()[0]["confidence"] == 0.9

    def test_semantic_dedup_keeps_the_newer_rule_when_it_is_longer(self, tmp_path: Path) -> None:
        """Distinct wording, identical vectors: only the cosine path can dedup these."""
        short_rule = "Zebra crossings need beacons"
        long_rule = "Submarine hatches demand orange lanterns for visibility"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({short_rule: _unit(0), long_rule: _unit(0)})
        assert store.write_lesson(short_rule)
        assert store.write_lesson(long_rule)
        assert _lesson_texts(store) == [long_rule]

    def test_semantic_dedup_keeps_the_newer_rule_when_it_is_shorter(self, tmp_path: Path) -> None:
        """Same pair, submitted in the other order -- the newer rule still wins.

        A length tie-break on ``len(rule) > len(existing_text)`` would DROP
        the submission when it loses, so character count would decide which of two
        near-identical rules is current. Corrections land on the losing side of
        that comparison as a rule: a retraction collapses a detailed stale claim
        into a short accurate one. The stale lesson then stayed in effect, which
        misleads the agent instead of merely losing detail. The substring and
        topic-overlap branches already superseded unconditionally; this one now
        agrees with them.
        """
        long_rule = "Submarine hatches demand orange lanterns for visibility"
        short_rule = "Zebra crossings need beacons"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({short_rule: _unit(0), long_rule: _unit(0)})
        assert store.write_lesson(long_rule)
        assert store.write_lesson(short_rule)
        assert _lesson_texts(store) == [short_rule]

    def test_semantic_supersede_never_lets_an_inferred_lesson_displace_user_explicit(
        self, tmp_path: Path
    ) -> None:
        """Newest-wins must not invert source authority.

        Consolidation extracts lessons on its own schedule, so an inferred
        near-duplicate routinely arrives AFTER the user's explicit correction.
        Unconditional newest-wins would tombstone the user's own lesson and
        replace it with automated guidance -- the same inversion
        ``_write_semantic`` already refuses for same-key writes. The stored
        ``user_explicit`` row must survive, and the write must report the old
        deduped outcome rather than a supersede.
        """
        taught = "Zebra crossings need beacons"
        inferred = "Submarine hatches demand orange lanterns for visibility"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({taught: _unit(0), inferred: _unit(0)})
        assert store.write_lesson(taught)  # source defaults to user_explicit
        result = store.write_lesson(inferred, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
        assert result.superseded == ()
        assert result.winner == taught
        assert result.winner_repo_scope is None
        assert _lesson_texts(store) == [taught]

    def test_small_keyword_subset_cannot_bypass_semantic_source_authority(
        self, tmp_path: Path
    ) -> None:
        """The authority pre-pass and mutating topic branch classify the same pair."""
        taught = (
            "Shell arguments must always be quoted when you interpolate them into a "
            "bash command, because unquoted globbing silently rewrites every "
            "filesystem path"
        )
        inferred = "Quote shell arguments"
        store = _store(tmp_path)
        try:
            store.embed_fn = _TableEmbedder({taught: _unit(0), inferred: _unit(0)})
            taught_words = store._lesson_keywords(taught.lower())
            inferred_words = store._lesson_keywords(inferred.lower())
            overlap = len(taught_words & inferred_words)
            assert overlap / min(len(taught_words), len(inferred_words)) >= 0.5
            assert overlap / max(len(taught_words), len(inferred_words)) < 0.5
            assert taught.lower() not in inferred.lower() and inferred.lower() not in taught.lower()
            assert store.write_lesson(taught, source="user_explicit")
            before = store.get_lessons()
            events = store.get_events()

            result = store.write_lesson(inferred, source="consolidation")

            assert result.outcome is LessonWriteOutcome.DEDUPED
            assert result.reason == "semantic_similarity" and result.superseded == ()
            assert store.get_lessons() == before
            assert store.get_events() == events
            assert _lesson_texts(store) == [taught]
        finally:
            store.close()

    def test_semantic_supersede_lets_a_user_explicit_correction_replace_an_inferred_lesson(
        self, tmp_path: Path
    ) -> None:
        """The guard is one-directional: the user's correction still wins."""
        inferred = "Submarine hatches demand orange lanterns for visibility"
        taught = "Zebra crossings need beacons"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({taught: _unit(0), inferred: _unit(0)})
        assert store.write_lesson(inferred, source="consolidation")
        assert store.write_lesson(taught)  # source defaults to user_explicit
        assert _lesson_texts(store) == [taught]

    def test_semantic_supersede_keeps_a_higher_confidence_import_lesson(
        self, tmp_path: Path
    ) -> None:
        """Authority is source AND confidence, not the ``user_explicit`` literal.

        The onboarding import stores the user's own lessons at confidence 1.0
        under source ``import`` (see ``onboarding_import``). A consolidation
        write carries confidence 0.9, and before this PR the length tie-break
        was the only thing that could spare such a row. The guard must weigh
        stored confidence, mirroring ``_write_semantic``'s same-key refusal
        ("Existing entry has higher confidence").
        """
        imported = "Zebra crossings need beacons"
        inferred = "Submarine hatches demand orange lanterns for visibility"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({imported: _unit(0), inferred: _unit(0)})
        key = f"lesson.{hashlib.sha256(imported.encode()).hexdigest()[:16]}"
        assert (
            store.set_semantic_if_absent(
                key,
                {"rule": imported, "category": "preference", "negative": None},
                1.0,
                "import",
            )
            == "imported"
        )
        result = store.write_lesson(inferred, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
        assert _lesson_texts(store) == [imported]

    def test_a_declined_write_deletes_no_row_it_scanned_before_the_decliner(
        self, tmp_path: Path
    ) -> None:
        """Semantic supersedes are deferred until the scan settles the write.

        Rows are scanned newest-first, not authority-first. Without deferral,
        a consolidation write near-identical to TWO stored rows -- a newer
        inferred one and an older user-set one -- deleted the inferred row
        first, then hit the user's row, whose guard declined the write: the
        inferred row was gone and nothing replaced it. Declining must cost no
        stored row from this branch.
        """
        taught = "Zebra crossings need beacons"
        inferred = "Submarine hatches demand orange lanterns for visibility"
        submitted = "Quokkas patrol the western observation deck at dawn"
        # sub is >0.85 cosine to BOTH stored rows, but the stored rows sit only
        # 0.6 from each other -- so they coexist (neither write deduped the
        # other) while both match the submission in the semantic branch.
        vec_a = [1.0, 0.0] + [0.0] * (_DIM - 2)
        vec_b = [0.6, 0.8] + [0.0] * (_DIM - 2)
        vec_sub = [0.894, 0.447] + [0.0] * (_DIM - 2)
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({taught: vec_a, inferred: vec_b, submitted: vec_sub})
        assert store.write_lesson(taught)  # user_explicit, older
        assert store.write_lesson(inferred, source="consolidation")  # newer
        assert sorted(_lesson_texts(store)) == sorted([taught, inferred])
        result = store.write_lesson(submitted, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "semantic_similarity"
        # The decline cost nothing: the inferred row scanned before the
        # user-set decliner is still stored, and nothing was reported deleted.
        assert result.superseded == ()
        assert sorted(_lesson_texts(store)) == sorted([taught, inferred])

    def test_the_authority_pre_pass_skips_rows_the_lexical_branches_claim(
        self, tmp_path: Path
    ) -> None:
        """A row substring/topic-overlap would resolve keeps main's outcome.

        The pre-pass covers PURE semantic matches only. A submission that
        lexically contains a stored ``user_explicit`` rule is claimed by the
        source-blind substring branch first (per-row branch order), and main
        deletes the stored row there -- "longer wins". The pre-pass must not
        turn that into an authority decline, or it would change main-owned
        lexical dedup outcomes this change disclaims.
        """
        taught = "Zebra crossings need beacons"
        submitted = "Zebra crossings need beacons after dusk"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({taught: _unit(0), submitted: _unit(0)})
        assert store.write_lesson(taught)  # user_explicit
        result = store.write_lesson(submitted, source="consolidation")
        assert result  # stored, not declined
        assert result.outcome is LessonWriteOutcome.INSERTED
        assert len(result.superseded) == 1  # the contained row was superseded
        assert _lesson_texts(store) == [submitted]

    def test_a_substring_covered_refusal_drops_the_deferred_semantic_deletes(
        self, tmp_path: Path
    ) -> None:
        """The semantic branch's newest-wins deletes must not outrun a refusal.

        The old tie-break REFUSED where newest-wins now deletes, so an
        immediate delete could strand a row: a semantic near-duplicate deleted
        mid-scan, then a later stored superset claims the write via main's
        ``substring_covered`` refusal -- the near-duplicate is gone and the
        submission was never stored. The semantic supersedes are deferred and
        a refusal executes none of them.
        """
        superset = "Quokkas patrol the western observation deck at dawn"
        near_dup = "Zebra crossings need beacons"
        submitted = "Quokkas patrol the western observation deck"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder(
            {superset: _unit(1), near_dup: _unit(0), submitted: _unit(0)}
        )
        assert store.write_lesson(superset, source="consolidation")  # older
        assert store.write_lesson(near_dup, source="consolidation")  # newer
        result = store.write_lesson(submitted, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "substring_covered"
        # The refusal cost nothing from the semantic branch: the near-duplicate
        # scanned before the superset is still stored.
        assert result.superseded == ()
        assert sorted(_lesson_texts(store)) == sorted([superset, near_dup])

    def test_a_substring_covered_refusal_deletes_no_topic_overlap_row(self, tmp_path: Path) -> None:
        """A declined write must not cost a row the LEXICAL branches claimed.

        No embedder here, so only the substring and topic-overlap branches can
        fire. Rows are scanned newest-first: the newer row overlaps the
        submission on keywords and the older one CONTAINS it, so an eager
        delete tombstones the overlapping row and then the superset declines
        the write via ``substring_covered``. The overlapping lesson is gone,
        the submission was never stored, and the result reports a decline
        alongside the deletion -- one stored lesson lost to a call the store
        refused to honour. Assert the stored rows, not the return string: the
        old code returned a plausible ``deduped`` verdict for exactly this.
        """
        superset = "Quokkas patrol the western observation deck at dawn every single morning"
        overlapping = "Quokkas patrol the deck"
        submitted = "Quokkas patrol the western observation deck at dawn"
        store = _store(tmp_path)
        assert store.write_lesson(superset, source="consolidation")  # older
        assert store.write_lesson(overlapping, source="consolidation")  # newer
        before = sorted(_lesson_texts(store))
        result = store.write_lesson(submitted, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.DEDUPED
        assert result.reason == "substring_covered"
        assert result.superseded == ()
        assert sorted(_lesson_texts(store)) == before

    def test_a_store_refusal_after_the_scan_deletes_nothing(self, tmp_path: Path) -> None:
        """The same guarantee for a value the STORE rejects, not a dedup rule.

        ``write_lesson``'s preflight screens the values it can, so this route
        is reached by a rejection the preflight cannot predict. It must cost no
        stored row either: the queue drains after ``set_semantic`` commits, so
        a rejection there returns with the store untouched rather than naming
        rows it destroyed for a lesson it did not keep.
        """
        stored = "Quokkas patrol the deck"
        submitted = "Quokkas patrol the western observation deck at dawn"
        store = _store(tmp_path)
        assert store.write_lesson(stored, source="consolidation")
        before = sorted(_lesson_texts(store))
        store.set_semantic = (  # type: ignore[method-assign]
            lambda *a, **k: (vm.SemanticRejectCode.INJECTION, "blocked")
        )
        result = store.write_lesson(submitted, source="consolidation")
        assert not result
        assert result.outcome is LessonWriteOutcome.REFUSED
        assert result.reason == "injection_blocked"
        assert result.superseded == ()
        assert sorted(_lesson_texts(store)) == before

    def test_a_committed_write_still_executes_its_lexical_supersedes(self, tmp_path: Path) -> None:
        """Deferring must not disable dedup: a write that LANDS still replaces.

        The counterpart to the two refusals above -- same overlapping row, no
        superset to decline the write -- so the queued deletion runs and is
        reported.
        """
        overlapping = "Quokkas patrol the deck"
        submitted = "Quokkas patrol the western observation deck at dawn"
        store = _store(tmp_path)
        assert store.write_lesson(overlapping, source="consolidation")
        result = store.write_lesson(submitted, source="consolidation")
        assert result
        assert result.outcome is LessonWriteOutcome.INSERTED
        assert result.superseded == (overlapping,)
        assert _lesson_texts(store) == [submitted]

    def test_a_concurrent_write_on_a_queued_key_is_not_superseded(self, tmp_path: Path) -> None:
        """The drain deletes the version it SCANNED, never whatever is there now.

        Nothing holds the store lock across scan -> ``set_semantic`` -> drain, and
        ``_lesson_key`` keys on the rule and scope alone, so another writer can
        commit onto a queued key inside that window -- a user enriching the very
        rule a consolidation pass is retiring lands on it exactly. Deleting blind
        tombstones that write: the user is told their lesson was saved, and it is
        gone with no recovery.

        The concurrent writer is injected through ``set_semantic`` because that is
        the window: the queue is already built and the drain has not run.
        """
        retired = "Quokkas patrol the deck"
        submitted = "Quokkas patrol the western observation deck at dawn"
        store = _store(tmp_path)
        assert store.write_lesson(retired, source="consolidation")
        retired_key = store.get_lessons()[0]["key"]

        real_set_semantic = store.set_semantic

        def _set_semantic_then_a_concurrent_write(*args: object, **kwargs: object) -> object:
            err = real_set_semantic(*args, **kwargs)
            # Lands on the queued key while the drain is still pending.
            real_set_semantic(
                retired_key,
                {"rule": retired, "category": "tool", "negative": "guard the gate"},
                1.0,
                "user_explicit",
            )
            return err

        store.set_semantic = _set_semantic_then_a_concurrent_write  # type: ignore[method-assign]
        result = store.write_lesson(submitted, source="consolidation")
        store.set_semantic = real_set_semantic  # type: ignore[method-assign]

        assert result
        assert result.outcome is LessonWriteOutcome.INSERTED
        # The queued row moved under the write, so it is neither deleted nor claimed.
        assert result.superseded == ()
        stored = sorted(_lesson_texts(store))
        assert submitted in stored
        assert any(text.startswith(retired) for text in stored), stored

    def test_the_rule_vector_is_persisted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder()
        assert store.write_lesson("Quote every shell argument you interpolate")
        blob = _raw_semantic_embedding(store, store.get_lessons()[0]["key"])
        assert blob is not None and len(blob) == _DIM * 4

    def test_an_unpackable_stored_vector_does_not_stop_the_write(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Quote every shell argument you interpolate")
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?",
            (b"\x00\x01\x02\x03\x04", store.get_lessons()[0]["key"]),
        )
        store.db.commit()
        store.embed_fn = _TableEmbedder()
        assert store.write_lesson("Rotate the signing credentials every ninety days")
        assert len(store.get_lessons()) == 2

    def test_lazy_vector_backfill_is_capped_per_call(self, tmp_path: Path) -> None:
        """Legacy NULL-vector lessons are repaired inline, but only a few per write."""
        legacy = [
            "Alpha zeppelins hover",
            "Beta walruses migrate",
            "Gamma turbines whistle",
            "Delta glaciers retreat",
            "Epsilon orchids bloom",
            "Zeta harbours silt",
            "Eta volcanoes rumble",
        ]
        new_rule = "Theta lighthouses blink"
        store = _store(tmp_path)
        for rule in legacy:
            assert store.write_lesson(rule)
        assert all(e["embedding"] is None for e in store.get_lessons())

        table = {rule: _unit(i) for i, rule in enumerate(legacy)}
        table[new_rule] = _unit(7)
        store.embed_fn = _TableEmbedder(table)
        assert store.write_lesson(new_rule)

        new_key = _lesson_key(new_rule)
        backfilled = [
            e for e in store.get_lessons() if e["key"] != new_key and e["embedding"] is not None
        ]
        assert len(backfilled) == vm._MAX_BACKFILLS_PER_CALL

    def test_a_vector_from_a_previous_space_is_left_null(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder()
        generation = store.space_generation
        pre_embedded = store.embed_lesson("Quote every shell argument you interpolate")
        assert pre_embedded is not None
        store.begin_space_change()
        assert store.write_lesson(
            "Quote every shell argument you interpolate",
            rule_emb=pre_embedded,
            rule_emb_generation=generation,
        )
        assert _raw_semantic_embedding(store, store.get_lessons()[0]["key"]) is None

    def test_embed_lesson_without_an_embedder_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.embed_lesson("Quote every shell argument you interpolate") is None

    def test_lessons_context_is_empty_without_lessons(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.get_lessons_context() == ""

    def test_lessons_context_renders_imported_dict_lesson_as_prose(self, tmp_path: Path) -> None:
        """An imported lesson is injected as its rule, not as a Python dict repr.

        The onboarding import stores a mapping rather than the string
        ``learn_add`` writes, and both shapes land in the same prompt block. The
        formatter must not interpolate the decoded value directly, or an imported
        row reached the model as ``{'rule': ..., 'category': ...}`` — the
        instruction buried inside punctuation and field names.
        """
        store = _store(tmp_path)
        store.set_semantic_if_absent(
            f"lesson.{hashlib.sha256(b'Prefer dark mode').hexdigest()[:16]}",
            {"rule": "Prefer dark mode", "category": "preference", "negative": None},
            1.0,
            "import",
        )
        ctx = store.get_lessons_context()
        assert "- Prefer dark mode" in ctx
        # The repr's own punctuation is the assertion: no dict may survive into
        # the prompt, and `category`/`negative` are storage fields the model has
        # no use for.
        assert "{'rule'" not in ctx
        assert "category" not in ctx
        assert "negative" not in ctx

    def test_lessons_context_renders_an_imported_not_clause(self, tmp_path: Path) -> None:
        """A dict-form lesson's NOT-clause is joined with the same separator.

        The string form spells a clause as ``<rule> — NOT: <negative>``, so the
        mapping form has to render identically — otherwise the same lesson reads
        two different ways depending on which writer stored it.
        """
        store = _store(tmp_path)
        store.set_semantic_if_absent(
            f"lesson.{hashlib.sha256(b'Pin dependency versions').hexdigest()[:16]}",
            {
                "rule": "Pin dependency versions",
                "category": "preference",
                "negative": "never use open ranges",
            },
            1.0,
            "import",
        )
        ctx = store.get_lessons_context()
        assert "- Pin dependency versions — NOT: never use open ranges" in ctx
        assert "{'rule'" not in ctx

    def test_lessons_context_skips_an_unrenderable_row(self, tmp_path: Path) -> None:
        """A shape with no usable rule is dropped, not stringified as a guess.

        This runs while a session's prompt is being built, where a raise costs
        the whole turn, so an unexpected value has to degrade to "omit it".
        """
        store = _store(tmp_path)
        store.set_semantic_if_absent(
            "lesson.emptyrule000", {"category": "preference"}, 1.0, "import"
        )
        store.set_semantic_if_absent("lesson.listshape000", ["not", "a", "lesson"], 1.0, "import")
        ctx = store.get_lessons_context()
        assert "category" not in ctx
        assert "not a lesson" not in ctx
        assert "['not'" not in ctx


class TestContradictionCandidateBanding:
    def test_no_embedder_yields_no_candidates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Quote every shell argument you interpolate")
        assert store.find_contradiction_candidates("Never quote a shell argument") == []

    def test_only_mid_band_similarities_are_returned(self, tmp_path: Path) -> None:
        near = "Alpha zeppelins hover above harbours"
        mid = "Beta walruses migrate through channels"
        far = "Gamma turbines whistle in the valley"
        probe = "Theta lighthouses blink at dusk"
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder(
            {
                near: _unit(0),
                mid: [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                far: _unit(1),
                probe: _unit(0),
            }
        )
        for rule in (near, mid, far):
            assert store.write_lesson(rule)
        candidates = store.find_contradiction_candidates(probe)
        # `near` is above the high threshold, `far` below the low one.
        assert [c["rule"] for c in candidates] == [mid]
        assert 0.4 <= candidates[0]["similarity"] < 0.85

    def test_a_caller_supplied_vector_avoids_a_second_embed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        embedder = _TableEmbedder()
        store.embed_fn = embedder
        assert store.write_lesson("Quote every shell argument you interpolate")
        embedder.calls.clear()
        store.find_contradiction_candidates("Theta lighthouses blink", rule_emb=_unit(3))
        assert embedder.calls == []


@pytest.mark.skipif(not vm._HAS_NUMPY, reason="the episodic backfill sweep needs numpy")
class TestEpisodicBackfillSweep:
    def test_deferred_rows_are_filled_and_progress_is_reported(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder()
        for i in range(3):
            assert store.write_episodic(
                f"Deferred fragment number {i} awaiting its vector", defer_embedding=True
            )
        seen: list[tuple[int, int]] = []
        assert store.backfill_missing_embeddings(progress=_recorder(seen)) == 3
        assert seen[0] == (0, 3)
        assert seen[-1] == (3, 3)
        assert all(
            _raw_episodic_embedding(store, r["id"]) is not None for r in store.get_episodic_list()
        )

    def test_the_sweep_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder()
        assert store.write_episodic("A deferred fragment awaiting its vector", defer_embedding=True)
        assert store.backfill_missing_embeddings() == 1
        assert store.backfill_missing_embeddings() == 0

    def test_backfilled_vectors_become_searchable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder({"Deferred fragment awaiting its vector": _unit(0)})
        assert store.write_episodic("Deferred fragment awaiting its vector", defer_embedding=True)
        assert store.search_episodic(query_embedding=_unit(0), limit=5) == []
        assert store.backfill_missing_embeddings() == 1
        assert len(store.search_episodic(query_embedding=_unit(0), limit=5)) == 1


class TestPendingEmbeddingsProbe:
    """``has_pending_embeddings()`` must answer for each sub-sweep, without a model.

    The boot sweep skips the ~700MB embedding-model load when this returns False,
    so a predicate that misses a row class would strand those rows unembedded
    forever. One test per sub-sweep predicate.
    """

    def test_an_empty_store_has_nothing_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.has_pending_embeddings() is False

    def test_a_deferred_episodic_row_is_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A deferred fragment awaiting its vector", defer_embedding=True)
        assert store.has_pending_embeddings() is True

    def test_an_embedded_episodic_row_is_not_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))
        assert store.has_pending_embeddings() is False

    def test_a_soft_deleted_row_is_not_pending(self, tmp_path: Path) -> None:
        """``is_deleted = 0`` is in every sub-sweep predicate — mirror it here.

        A tombstoned row is never embedded by the sweep, so counting it as work
        would load the model on every boot for rows nothing will ever fill.
        """
        store = _store(tmp_path)
        assert store.write_episodic("A fragment that is about to be deleted", defer_embedding=True)
        mem_id = store.get_episodic_list()[0]["id"]
        assert store.delete_episodic(mem_id)
        assert store.has_pending_embeddings() is False

    def test_a_lesson_with_no_vector_is_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always name the branch explicitly when pushing", "tool")
        # write_lesson embeds at write time only when embed_fn is bound; it is
        # not here, so the row lands NULL — exactly the state after a model swap.
        assert store.has_pending_embeddings() is True

    def test_a_non_lesson_semantic_row_with_no_vector_is_pending(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.set_semantic("user.editor", "vim", 0.9, "user_explicit") is None
        assert store.has_pending_embeddings() is True

    def test_the_probe_agrees_with_the_sweep_after_it_runs(self, tmp_path: Path) -> None:
        """The probe's contract: False means the sweep would embed nothing."""
        store = _store(tmp_path)
        assert store.write_episodic("A deferred fragment awaiting its vector", defer_embedding=True)
        assert store.write_lesson("Never force-push a shared branch", "tool")
        assert store.set_semantic("user.shell", "zsh", 0.9, "user_explicit") is None
        store.embed_fn = _fixed_embed()
        assert store.has_pending_embeddings() is True
        store.backfill_missing_embeddings()
        assert store.has_pending_embeddings() is False
        assert store.backfill_missing_embeddings() == 0


class TestEmbeddingSpaceReconciliation:
    def test_the_first_call_stamps_without_clearing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))
        assert store.recorded_embedding_space() is None
        assert store.reconcile_embedding_space("sig-a") == 0
        assert store.recorded_embedding_space() == "sig-a"
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is not None

    def test_a_matching_signature_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.reconcile_embedding_space("sig-a")
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))
        assert store.reconcile_embedding_space("sig-a") == 0
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is not None

    def test_a_changed_signature_clears_episodic_and_lesson_vectors(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _TableEmbedder()
        store.reconcile_embedding_space("sig-a")
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))
        assert store.write_lesson("Quote every shell argument you interpolate")
        assert store.reconcile_embedding_space("sig-b") == 2
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is None
        assert _raw_semantic_embedding(store, store.get_lessons()[0]["key"]) is None
        assert store.recorded_embedding_space() == "sig-b"

    def test_an_unknown_space_can_be_cleared_explicitly(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))
        assert store.reconcile_embedding_space("sig-a", clear_when_unknown=True) == 1
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is None

    def test_a_surviving_stale_index_leaves_the_signature_unstamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stamping while a stale index survives would make the corruption permanent."""
        store = _store(tmp_path)
        store.reconcile_embedding_space("sig-a")
        assert store.write_episodic("A fragment that already carries a vector", embedding=_unit(0))

        def _refuse(self: Path, missing_ok: bool = False) -> None:
            raise OSError("read-only directory")

        monkeypatch.setattr(Path, "unlink", _refuse)
        assert store.reconcile_embedding_space("sig-b") == 1
        assert store.recorded_embedding_space() == "sig-a"

    def test_a_store_with_no_vectors_still_records_the_change(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.reconcile_embedding_space("sig-a")
        assert store.reconcile_embedding_space("sig-b") == 0
        assert store.recorded_embedding_space() == "sig-b"


class TestEmbeddingWidthAndGeneration:
    def test_non_positive_and_unchanged_widths_are_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.set_embedding_dim(0) is False
        assert store.set_embedding_dim(-1) is False
        assert store.set_embedding_dim(_DIM) is False

    def test_a_width_change_drops_the_in_memory_index(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.set_embedding_dim(_DIM * 2) is True
        assert store.memory_stats()["faiss_index_size"] == 0

    def test_the_generation_advances_on_a_space_change(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        before = store.space_generation
        store.begin_space_change()
        assert store.space_generation == before + 1

    def test_a_vector_produced_across_a_swap_is_discarded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        def _swap_mid_embed(_text: str) -> list[float] | None:
            store.begin_space_change()
            return _unit(0)

        store.embed_fn = _swap_mid_embed
        assert store.write_episodic("A fragment embedded across a live model swap")
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is None


class TestEmbedFnLazyRebind:
    """``embed_fn_factory`` recovers from a model that was absent at gateway boot.

    Driven through ``import_memory``, which calls ``_try_embed`` unconditionally.
    """

    def _import_one(self, store: VectorMemoryStore, text: str) -> None:
        store.import_memory({"episodic": [{"text": text}]})

    def test_a_working_candidate_is_bound(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        embedder = _TableEmbedder()
        store.embed_fn_factory = lambda: embedder
        self._import_one(store, "A fragment imported after a lazy rebind")
        assert store.embed_fn is embedder
        mem_id = store.get_episodic_list()[0]["id"]
        assert _raw_episodic_embedding(store, mem_id) is not None

    def test_a_factory_returning_none_leaves_it_unbound(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn_factory = lambda: None
        self._import_one(store, "A fragment imported while the factory yields nothing")
        assert store.embed_fn is None

    def test_a_candidate_whose_probe_returns_none_is_not_bound(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn_factory = lambda: (lambda _text: None)
        self._import_one(store, "A fragment imported while the probe returns nothing")
        assert store.embed_fn is None

    def test_a_candidate_whose_probe_returns_empty_is_not_bound(self, tmp_path: Path) -> None:
        """A zero-width probe response is a misconfiguration, not a success."""
        store = _store(tmp_path)
        store.embed_fn_factory = lambda: (lambda _text: [])
        self._import_one(store, "A fragment imported while the probe returns empty")
        assert store.embed_fn is None

    def test_a_candidate_whose_probe_raises_is_not_bound(self, tmp_path: Path) -> None:
        def _explode(_text: str) -> list[float] | None:
            raise RuntimeError("inference crashed")

        store = _store(tmp_path)
        store.embed_fn_factory = lambda: _explode
        self._import_one(store, "A fragment imported while the probe explodes")
        assert store.embed_fn is None

    def test_the_cooldown_suppresses_a_second_factory_call(self, tmp_path: Path) -> None:
        """At most one factory call per cooldown window, so a missing model is cheap."""
        calls: list[int] = []

        def _factory() -> Callable[[str], list[float] | None] | None:
            calls.append(1)
            return None

        store = _store(tmp_path)
        store.embed_fn_factory = _factory
        store.import_memory(
            {
                "episodic": [
                    {"text": "The first imported fragment triggering an attempt"},
                    {"text": "The second imported fragment inside the cooldown"},
                ]
            }
        )
        assert len(calls) == 1


class TestStoreLifecycle:
    def test_using_a_closed_store_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.close()
        with pytest.raises(RuntimeError, match="not initialized"):
            store.db.execute("SELECT 1")

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.close()
        store.close()

    def test_reopening_sees_the_prior_writes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "neovim", 0.95, "user_explicit")
        store.close()
        reopened = _store(tmp_path)
        entry = reopened.get_semantic("pref.editor")
        assert entry is not None and json.loads(entry["value_json"]) == "neovim"
        reopened.close()
