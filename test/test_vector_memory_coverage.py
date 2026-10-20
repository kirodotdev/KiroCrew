"""Coverage-focused tests for the less-travelled paths of ``vector_memory``.

The store's happy paths are already covered by ``test_vector_memory.py`` and the
topic files its docstring names. This module targets the branches that only fire
on legacy/degraded input: the legacy markdown migration, the export/import round
trip, episodic promotion, the keyword fallback, the embedding backfill sweeps,
and the many "value on disk is not what the writer promised" recovery paths.

Everything runs against a fresh SQLite file under ``tmp_path`` with a
deterministic in-process embedder — no network, no model download, no FAISS
requirement.

The module's "Additional coverage" section -- further vector-space lifecycle,
lesson-dedup and ranking cases -- is in ``test_vector_memory_coverage_vector_space.py``;
the store and embedder builders both modules use are in
``vector_memory_coverage_helpers.py``.
"""

from __future__ import annotations

import json
import struct
import time
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
from kiro_crew.vector_memory import VectorMemoryStore


class TestMigrateV2:
    """The embedding-column migration must be idempotent but not swallow real errors."""

    def test_duplicate_column_is_tolerated(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # init() already applied the migration; a second application must be a no-op.
        vm._migrate_v2(store.db)
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(semantic_memory)")}
        assert "embedding" in cols

    def test_other_operational_errors_propagate(self) -> None:
        db = vm.sqlite3.connect(":memory:")
        try:
            with pytest.raises(vm.sqlite3.OperationalError):
                vm._migrate_v2(db)
        finally:
            db.close()


class TestInitDegradesWithoutFaiss:
    def test_index_load_failure_does_not_break_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self: VectorMemoryStore) -> bool:
            raise RuntimeError("faiss-cpu not installed")

        monkeypatch.setattr(VectorMemoryStore, "load_faiss_index", _boom)
        store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=_DIM)
        store.init()
        # The store is still fully usable for semantic + keyword work.
        assert store.set_semantic("pref.os", "linux", 0.9, "user_explicit") is None
        store.close()


class TestSetSemanticIfAbsent:
    def test_imports_when_absent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.set_semantic_if_absent("pref.editor", "vim", 0.9, "import") == "imported"
        entry = store.get_semantic("pref.editor")
        assert entry is not None and entry["value_json"] == '"vim"'

    def test_does_not_replace_an_existing_value(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit")
        assert store.set_semantic_if_absent("pref.editor", "vim", 0.9, "import") == "existing"
        entry = store.get_semantic("pref.editor")
        assert entry is not None and entry["value_json"] == '"emacs"'

    def test_invalid_key_is_rejected(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.set_semantic_if_absent("Not A Key", "x", 0.9, "import") == "rejected"

    def test_tombstoned_row_reports_existing_instead_of_crashing(self, tmp_path: Path) -> None:
        """A tombstone keeps the primary key, so the INSERT hits an IntegrityError.

        The active-row probe cannot see the tombstone (it filters is_deleted=0),
        so this is the one path that reaches the IntegrityError handler. It must
        report "existing" rather than propagating a DB error to the importer.
        """
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit")
        assert store.delete_semantic("pref.editor", "user_explicit")
        assert store.set_semantic_if_absent("pref.editor", "vim", 0.9, "import") == "existing"


class TestSemanticPaginationAndFormatting:
    def test_get_all_semantic_paginates(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for i in range(5):
            store.set_semantic(f"pref.k{i}", i, 0.9, "user_explicit")
        page = store.get_all_semantic(limit=2, offset=1)
        assert [e["key"] for e in page] == ["pref.k1", "pref.k2"]

    def test_unparseable_value_json_falls_back_to_the_raw_text(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
            ("this is not json", "pref.editor"),
        )
        store.db.commit()
        assert "this is not json" in store.get_semantic_context()

    def test_complex_values_render_as_json(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.langs", ["python", "rust"], 0.9, "user_explicit")
        assert '["python", "rust"]' in store.get_semantic_context()

    def test_cap_smaller_than_the_first_line_yields_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        assert store.get_semantic_context(cap=1) == ""


class TestEventLog:
    def test_events_paginate(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for i in range(4):
            store.set_semantic(f"pref.k{i}", i, 0.9, "user_explicit")
        first = store.get_events(limit=2, offset=0)
        second = store.get_events(limit=2, offset=2)
        assert len(first) == len(second) == 2
        assert {e["id"] for e in first}.isdisjoint({e["id"] for e in second})

    def test_rotate_is_a_noop_under_the_cap(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        assert store.rotate_events(max_rows=100) == 0

    def test_rotate_trims_the_oldest_events(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for i in range(5):
            store.set_semantic(f"pref.k{i}", i, 0.9, "user_explicit")
        before = store.db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        assert before >= 5
        assert store.rotate_events(max_rows=2) == before - 2
        assert store.db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] == 2

    def test_a_broken_audit_table_does_not_fail_the_write(self, tmp_path: Path) -> None:
        """A broken audit write must never take the caller down with it."""
        store = _store(tmp_path)
        store.db.execute("DROP TABLE memory_events")
        store.db.commit()

        assert store.set_semantic("pref.editor", "vim", 0.9, "user_explicit") is None
        entry = store.get_semantic("pref.editor")
        assert entry is not None and entry["value_json"] == '"vim"'


class TestRetireStaleEpisodicTextFallback:
    def test_exact_phrase_references_are_tombstoned_without_embeddings(
        self, tmp_path: Path
    ) -> None:
        """With no embedder the retirement falls back to exact phrase matching."""
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        assert store.write_episodic("Remember that the editor: vim setting is global")
        assert store.write_episodic("Unrelated note about the deployment pipeline schedule")

        store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit")

        remaining = _episodic_texts(store)
        assert remaining == ["Unrelated note about the deployment pipeline schedule"]
        retired = _episodic_texts(store, deleted=True)
        assert retired == ["Remember that the editor: vim setting is global"]

    def test_retirement_failure_keeps_the_semantic_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")

        def _boom(key: str, old_value: str) -> None:
            raise RuntimeError("retirement exploded")

        monkeypatch.setattr(store, "_retire_stale_episodic", _boom)
        assert store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit") is None
        entry = store.get_semantic("pref.editor")
        assert entry is not None and entry["value_json"] == '"emacs"'

    def test_rephrased_references_are_retired_via_vector_similarity(
        self, tmp_path: Path
    ) -> None:
        """With an embedder the retirement also runs the vector-similarity sweep."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        assert store.write_episodic("The user has always reached for vim on this box")

        store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit")

        assert _episodic_texts(store) == []
        assert _episodic_texts(store, deleted=True) == [
            "The user has always reached for vim on this box"
        ]

    def test_an_unparseable_old_value_is_stringified(self, tmp_path: Path) -> None:
        """A legacy row whose value_json is not JSON must still drive retirement."""
        store = _store(tmp_path)
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
            ("bare-vim", "pref.editor"),
        )
        store.db.commit()
        assert store.write_episodic("Remember that the editor bare-vim is the default")

        assert store.set_semantic("pref.editor", "emacs", 0.9, "user_explicit") is None
        assert _episodic_texts(store) == []


class TestSqliteVectorSearchEdges:
    def test_rows_with_a_different_width_are_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("A memory embedded at the store's native width")
        # Corrupt one row's vector to a narrower width; it must be ignored, not crash.
        store.db.execute(
            "UPDATE episodic_memories SET embedding = ?",
            (struct.pack("3f", 1.0, 0.0, 0.0),),
        )
        store.db.commit()
        assert store.search_episodic(query_embedding=[0.5] * _DIM, limit=5) == []

    def test_tag_filter_excludes_non_matching_rows(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("A note that belongs to the ops rotation", tags=["ops"])
        assert store.write_episodic("A note that belongs to the docs backlog", tags=["docs"])

        hits = store.search_episodic(query_embedding=[0.5] * _DIM, limit=5, tag_filter=["ops"])
        assert [h["text"] for h in hits] == ["A note that belongs to the ops rotation"]


class TestEpisodicListAndDelete:
    def test_list_filters_by_tag(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("An entry filed under the ops rotation", tags=["ops"])
        assert store.write_episodic("An entry filed under the docs backlog", tags=["docs"])
        rows = store.get_episodic_list(tag_filter=["ops"])
        assert [r["text"] for r in rows] == ["An entry filed under the ops rotation"]

    def test_deleting_an_unknown_id_reports_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.delete_episodic("no-such-id") is False


class TestEpisodicContext:
    def test_query_text_is_embedded_when_no_vector_is_supplied(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("The release train ships every Thursday afternoon")
        context = store.get_episodic_context(query_text="release train schedule")
        assert "release train ships every Thursday" in context
        assert context.startswith("[Episodic Memory")

    def test_cap_truncates_the_result_list(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        for i in range(3):
            assert store.write_episodic(f"Fragment number {i} of the capped context body")
        context = store.get_episodic_context(query_embedding=[0.5] * _DIM, cap=50)
        # Only the first line fits under a 50-char cap.
        assert context.count("\n1. ") == 1
        assert "\n2. " not in context

    def test_cap_below_the_first_line_yields_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("A fragment long enough to blow a one-character cap")
        assert store.get_episodic_context(query_embedding=[0.5] * _DIM, cap=1) == ""


class TestEpisodicHelpers:
    def test_matches_tags_accepts_an_already_decoded_list(self) -> None:
        assert VectorMemoryStore._matches_tags({"tags": ["Ops"]}, ["ops"]) is True
        assert VectorMemoryStore._matches_tags({"tags": ["docs"]}, ["ops"]) is False
        assert VectorMemoryStore._matches_tags({"tags": None}, ["ops"]) is False

    def test_batch_fetch_skips_missing_and_tombstoned_ids(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("The first batch-fetched episodic fragment")
        assert store.write_episodic("The second batch-fetched episodic fragment")
        ids = [r["id"] for r in store.get_episodic_list()]
        assert store.delete_episodic(ids[0])

        assert store._get_episodic_batch([]) == {}
        fetched = store._get_episodic_batch([*ids, "ghost-id"])
        assert set(fetched) == {ids[1]}
        assert "embedding" not in fetched[ids[1]]

    def test_touch_cache_is_swept_when_it_grows_past_the_cap(self, tmp_path: Path) -> None:
        """The debounce map is process-local state, so it must stay bounded."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("A memory whose access timestamp gets debounced")
        # Seed the map past its cap with entries far older than the debounce window.
        stale = time.monotonic() - 10_000.0
        store._last_accessed_touch = {
            f"stale-{i}": stale for i in range(store._LAST_ACCESSED_CACHE_MAX + 4)
        }
        hits = store.search_episodic(query_embedding=[0.5] * _DIM, limit=5)
        assert hits
        assert len(store._last_accessed_touch) <= store._LAST_ACCESSED_CACHE_MAX
        assert not any(k.startswith("stale-") for k in store._last_accessed_touch)


class TestParsePreference:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("editor: vim", ("pref.editor", "vim")),
            ("My favorite language is python", ("pref.favorite_language", "python")),
            ("I prefer dark mode", ("pref.general", "dark mode")),
            ("something entirely freeform", None),
        ],
    )
    def test_heuristics(self, text: str, expected: tuple[str, str] | None) -> None:
        assert VectorMemoryStore._parse_preference(text) == expected


class TestDeleteLesson:
    def test_deletes_every_lesson_matching_the_substring(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always pin the release tag before publishing a wheel")
        assert store.write_lesson("Never bind a diagnostic server to a public interface")
        assert store.delete_lesson("RELEASE TAG") is True
        assert _lesson_texts(store) == ["Never bind a diagnostic server to a public interface"]

    def test_no_match_reports_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always pin the release tag before publishing a wheel")
        assert store.delete_lesson("something that is simply not there") is False


class TestContradictionCandidateBlobRecovery:
    def test_malformed_stored_vectors_are_skipped(self, tmp_path: Path) -> None:
        """Truncated / unpackable blobs must be skipped, not raise."""
        store = _store(tmp_path)
        assert store.write_lesson("Prefer allowlists over denylists for network egress")
        assert store.write_lesson("Rotate the signing key whenever a maintainer leaves")
        keys = [e["key"] for e in store.get_lessons()]

        # One blob too short to hold a single float, one that unpacks unevenly.
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?", (b"ab", keys[0])
        )
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?", (b"abcde", keys[1])
        )
        store.db.commit()

        store.embed_fn = _fixed_embed()
        assert store.find_contradiction_candidates("Some brand new rule about egress") == []


class TestTryEmbedFailureModes:
    def test_an_exploding_embedder_yields_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        def _boom(_text: str) -> list[float] | None:
            raise RuntimeError("model crashed")

        store.embed_fn = _boom
        assert store._try_embed("anything at all") is None

    def test_an_exploding_factory_yields_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store._embed_fn_rebind_cooldown_secs = 0.0

        def _boom() -> Callable[[str], list[float] | None] | None:
            raise RuntimeError("factory crashed")

        store.embed_fn_factory = _boom
        assert store._try_embed("anything at all") is None
        assert store.embed_fn is None

    def test_a_candidate_that_fails_its_probe_is_not_bound(self, tmp_path: Path) -> None:
        """The factory succeeded but the model it produced cannot embed."""
        store = _store(tmp_path)
        store._embed_fn_rebind_cooldown_secs = 0.0

        def _explodes_on_call(_text: str) -> list[float] | None:
            raise RuntimeError("model load failed lazily")

        store.embed_fn_factory = lambda: _explodes_on_call
        assert store._try_embed("anything at all") is None
        assert store.embed_fn is None


class TestBackfillSweeps:
    def test_no_embedder_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment stored while embeddings were off")
        assert store.backfill_missing_embeddings() == 0

    def test_progress_is_reported_for_rows_that_fail_to_embed(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment that the embedder will refuse to embed")
        seen: list[tuple[int, int]] = []
        store.embed_fn = lambda _text: None
        assert store.backfill_missing_embeddings(progress=_recorder(seen)) == 0
        # Denominator up front, then one report for the skipped row.
        assert seen == [(0, 1), (0, 1)]

    def test_wrong_width_vectors_are_left_null_for_a_later_sweep(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment the embedder answers at the wrong width")
        seen: list[tuple[int, int]] = []
        store.embed_fn = _fixed_embed(dim=_DIM + 3)
        assert store.backfill_missing_embeddings(progress=_recorder(seen)) == 0
        assert seen == [(0, 1), (0, 1)]
        row = store.db.execute("SELECT embedding FROM episodic_memories").fetchone()
        assert row["embedding"] is None

    def test_lesson_vectors_are_repaired_with_progress(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always squash a review branch down to one commit")
        key = store.get_lessons()[0]["key"]
        assert _raw_semantic_embedding(store, key) is None

        seen: list[tuple[int, int]] = []
        store.embed_fn = _fixed_embed()
        assert store._backfill_lesson_embeddings(progress=_recorder(seen)) == 1
        assert seen == [(0, 1), (1, 1)]
        blob = _raw_semantic_embedding(store, key)
        assert blob is not None and len(blob) == _DIM * 4

    def test_backfill_embeds_the_bare_rule_matching_the_write_path(self, tmp_path: Path) -> None:
        """write_lesson embeds only the rule, so a backfilled mapping row must
        embed the same input -- embedding rule+clause would put its vector in a
        different space than the query vectors it is compared against."""
        store = _store(tmp_path)
        assert store.write_lesson("Pin the port", "tool", "Do not autopick")
        key = store.get_lessons()[0]["key"]
        assert _raw_semantic_embedding(store, key) is None

        embedded_inputs: list[str] = []

        def _capture(text: str) -> list[float]:
            embedded_inputs.append(text)
            return [0.5] * _DIM

        store.embed_fn = _capture
        assert store._backfill_lesson_embeddings() == 1
        assert embedded_inputs == ["Pin the port"]

    def test_lessons_with_unparseable_values_are_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always squash a review branch down to one commit")
        key = store.get_lessons()[0]["key"]
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?", ("<not json>", key)
        )
        store.db.commit()

        store.embed_fn = _fixed_embed()
        assert store._backfill_lesson_embeddings() == 0
        assert _raw_semantic_embedding(store, key) is None

    def test_lessons_the_embedder_refuses_stay_null(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always squash a review branch down to one commit")
        key = store.get_lessons()[0]["key"]
        store.embed_fn = lambda _text: None
        assert store._backfill_lesson_embeddings() == 0
        assert _raw_semantic_embedding(store, key) is None

    def test_no_lesson_rows_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store._backfill_lesson_embeddings() == 0

    def test_lesson_sweep_without_an_embedder_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Always squash a review branch down to one commit")
        assert store._backfill_lesson_embeddings() == 0


class TestLessonDedup:
    def test_a_rule_already_covered_by_a_longer_lesson_is_dropped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson(
            "Always pin the release tag before publishing a wheel to the CDN"
        )
        assert store.write_lesson("pin the release tag").wrote is False
        assert len(store.get_lessons()) == 1

    def test_a_longer_rule_replaces_the_lesson_it_contains(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("pin the release tag")
        assert store.write_lesson(
            "Always pin the release tag before publishing a wheel to the CDN"
        )
        assert _lesson_texts(store) == ["Always pin the release tag before publishing a wheel to the CDN"]

    def test_an_unpackable_stored_vector_does_not_break_the_dedup_scan(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        assert store.write_lesson("Prefer allowlists over denylists for network egress")
        key = store.get_lessons()[0]["key"]
        store.db.execute(
            "UPDATE semantic_memory SET embedding = ? WHERE key = ?", (b"abcde", key)
        )
        store.db.commit()

        store.embed_fn = _fixed_embed()
        assert store.write_lesson("Rotate the signing key whenever a maintainer departs")
        assert len(store.get_lessons()) == 2

    def test_a_space_change_mid_scan_drops_the_lazy_backfills(self, tmp_path: Path) -> None:
        """Lazy backfills embedded before a model swap must not be committed.

        The dedup scan embeds legacy lessons inline, so a swap can land between
        two entries. The blob from before the swap belongs to the old vector
        space and has to be left NULL for the post-activation sweep instead of
        being written behind reconcile's back.
        """
        store = _store(tmp_path)
        assert store.write_lesson("Prefer allowlists over denylists for network egress")
        assert store.write_lesson("Rotate the signing key whenever a maintainer departs")
        keys = [e["key"] for e in store.get_lessons()]
        assert all(_raw_semantic_embedding(store, k) is None for k in keys)

        calls: list[str] = []

        def _swap_on_the_last_backfill(text: str) -> list[float] | None:
            calls.append(text)
            # 1 = the new rule, 2 = first lazy backfill, 3 = second lazy backfill.
            if len(calls) == 3:
                store.begin_space_change()
            # Distinct one-hot vectors so nothing trips the >0.85 semantic dedup.
            vec = [0.0] * _DIM
            vec[len(calls) % _DIM] = 1.0
            return vec

        store.embed_fn = _swap_on_the_last_backfill
        assert store.write_lesson("Verify the changelog headings render on mobile widths")

        assert len(calls) == 3
        # Neither legacy lesson kept a vector from the superseded space.
        assert all(_raw_semantic_embedding(store, k) is None for k in keys)


class TestFaissAbsent:
    @pytest.mark.skipif(vm._HAS_FAISS, reason="asserts the no-faiss degradation")
    def test_index_build_is_a_noop_without_faiss(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.write_episodic("A fragment stored while faiss is unavailable")
        assert store.build_faiss_index() == 0
        # Vector search still works via the stdlib cosine fallback.
        assert store.search_episodic(query_embedding=[0.5] * _DIM, limit=5)


class TestKeywordFallback:
    def test_a_query_with_no_usable_words_returns_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment about the nightly deployment pipeline")
        assert store.search_episodic(query_text="a of to") == []

    def test_keyword_search_matches_text(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic("A fragment about the nightly deployment pipeline")
        assert store.write_episodic("A fragment about the quarterly planning review")
        hits = store.search_episodic(query_text="deployment", limit=5)
        assert [h["text"] for h in hits] == ["A fragment about the nightly deployment pipeline"]

    def test_keyword_search_honours_the_tag_filter(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.write_episodic(
            "A fragment about the nightly deployment pipeline", tags=["ops"]
        )
        assert store.write_episodic(
            "Another fragment about the deployment checklist", tags=["docs"]
        )
        hits = store.search_episodic(query_text="deployment", limit=5, tag_filter=["ops"])
        assert [h["text"] for h in hits] == ["A fragment about the nightly deployment pipeline"]


class TestCosineSimDimensionGuard:
    """`_cosine_sim` must not silently truncate a
    dimension-mismatched pair via `zip` instead of rejecting it, so a row
    embedded at a different dimensionality (e.g. a leftover from a previous
    embedding-model generation) returned a plausible-looking partial-overlap
    score instead of 0.0."""

    def test_mismatched_length_scores_zero(self) -> None:
        a = [1.0, 0.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]  # one dimension short
        assert VectorMemoryStore._cosine_sim(a, b) == 0.0

    def test_matching_length_is_unaffected(self) -> None:
        a = [1.0, 0.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0, 0.0]
        assert VectorMemoryStore._cosine_sim(a, b) == pytest.approx(1.0)

    def test_zero_vector_still_scores_zero_not_a_zerodivisionerror(self) -> None:
        assert VectorMemoryStore._cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestStoredSimilarityScorer:
    """The scorer built by `_stored_similarity_scorer`
    backs the two threshold callers (semantic dedup, contradiction detection)
    as well as the two ranking callers, so it must (1) reject a mismatched
    dimension the same way `_cosine_sim` now does, (2) return the raw
    (possibly negative) cosine value rather than clamping at 0.0 -- a
    threshold caller comparing against a band that may include non-positive
    bounds needs the true value, and (3) agree with `_cosine_sim` to float64
    precision, not the ~1e-7 the numpy path's old float32 accumulation gave.
    """

    def _row(self, vec: list) -> dict:
        import struct

        return {"embedding": struct.pack(f"{len(vec)}f", *vec)}

    @pytest.mark.parametrize("has_numpy", [True, False])
    def test_mismatched_dimension_row_scores_zero(self, monkeypatch, has_numpy) -> None:
        monkeypatch.setattr(vm, "_HAS_NUMPY", has_numpy and vm._HAS_NUMPY)
        query = [1.0, 0.0, 0.0]
        scorer = VectorMemoryStore._stored_similarity_scorer(query)
        assert scorer(self._row([1.0, 0.0])) == 0.0  # one short
        assert scorer(self._row([1.0, 0.0, 0.0, 0.0])) == 0.0  # one long

    @pytest.mark.parametrize("has_numpy", [True, False])
    def test_negative_cosine_is_returned_uncapped(self, monkeypatch, has_numpy) -> None:
        monkeypatch.setattr(vm, "_HAS_NUMPY", has_numpy and vm._HAS_NUMPY)
        query = [1.0, 0.0]
        scorer = VectorMemoryStore._stored_similarity_scorer(query)
        # Exactly opposite direction -> cosine == -1.0, not clamped to 0.0.
        assert scorer(self._row([-1.0, 0.0])) == pytest.approx(-1.0)

    @pytest.mark.parametrize("has_numpy", [True, False])
    def test_agrees_with_cosine_sim_on_a_normal_pair(self, monkeypatch, has_numpy) -> None:
        monkeypatch.setattr(vm, "_HAS_NUMPY", has_numpy and vm._HAS_NUMPY)
        query = [0.37, -1.2, 0.05, 2.1, 0.0, -0.9]
        row = [1.1, 0.3, -0.4, 0.02, 0.6, -1.5]
        scorer = VectorMemoryStore._stored_similarity_scorer(query)
        got = scorer(self._row(row))
        want = VectorMemoryStore._cosine_sim(query, row)
        # float32-stored row (as every scorer input is) vs the float64-list
        # _cosine_sim compares against: agreement is bounded by the storage
        # round-trip precision, not by the scorer's own arithmetic anymore.
        assert got == pytest.approx(want, abs=1e-6)

    def test_no_query_embedding_returns_a_constant_zero_scorer(self) -> None:
        scorer = VectorMemoryStore._stored_similarity_scorer(None)
        assert scorer(self._row([1.0, 0.0])) == 0.0
        assert scorer({}) == 0.0


class TestEpisodicPromotion:
    def test_promotion_without_an_embedder_is_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.promote_episodic_patterns() == 0

    @pytest.mark.skipif(not vm._HAS_NUMPY, reason="clustering needs numpy")
    def test_a_repeated_pattern_becomes_a_semantic_fact(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        for i in range(5):
            assert store.write_episodic(
                f"Note {i}: user prefers dark mode across every dashboard surface"
            )

        assert store.promote_episodic_patterns(min_count=5) == 1
        entry = store.get_semantic("pref.general")
        assert entry is not None
        assert "dark mode" in json.loads(entry["value_json"])
        # Every clustered member is retired so it cannot be promoted twice.
        assert _episodic_texts(store) == []

    @pytest.mark.skipif(not vm._HAS_NUMPY, reason="clustering needs numpy")
    def test_a_cluster_with_no_inferrable_key_is_left_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        for i in range(5):
            assert store.write_episodic(
                f"Shard {i} of the nightly sweep finished without any warnings"
            )

        assert store.promote_episodic_patterns(min_count=5) == 0
        assert len(_episodic_texts(store)) == 5

    @pytest.mark.skipif(not vm._HAS_NUMPY, reason="clustering needs numpy")
    def test_a_cluster_below_the_threshold_is_left_alone(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        for i in range(2):
            assert store.write_episodic(
                f"Note {i}: user prefers dark mode across every dashboard surface"
            )
        assert store.promote_episodic_patterns(min_count=5) == 0
        assert store.get_semantic("pref.general") is None


class TestSemanticKeyInference:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("User prefers tabs over spaces", "pref.general"),
            ("i like short commit subjects", "pref.general"),
            ("project Redwood uses terraform for provisioning", "project.redwood.tool"),
            ("nothing here resembles a preference", None),
        ],
    )
    def test_key_inference(self, text: str, expected: str | None) -> None:
        assert VectorMemoryStore._infer_semantic_key(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("User prefers tabs over spaces", "tabs over spaces"),
            ("project Redwood uses terraform", "terraform"),
            ("already a bare value", "already a bare value"),
        ],
    )
    def test_value_extraction(self, text: str, expected: str) -> None:
        assert VectorMemoryStore._extract_value_from_text(text) == expected


class TestImportMemory:
    def test_round_trip_of_both_shapes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        counts = store.import_memory(
            {
                "semantic": [
                    {"key": "pref.editor", "value_json": '"vim"', "confidence": 0.9},
                    {"key": "pref.shell", "value": "zsh"},
                ],
                "episodic": [
                    {"text": "An imported fragment carrying JSON-encoded tags", "tags": '["ops"]'},
                    {"text": "An imported fragment carrying already-decoded tags", "tags": ["qa"]},
                ],
            }
        )
        assert counts == {"semantic": 2, "episodic": 2, "skipped": 0}
        editor = store.get_semantic("pref.editor")
        shell = store.get_semantic("pref.shell")
        assert editor is not None and json.loads(editor["value_json"]) == "vim"
        assert shell is not None and json.loads(shell["value_json"]) == "zsh"
        assert len(store.get_episodic_list(tag_filter=["ops"])) == 1
        assert len(store.get_episodic_list(tag_filter=["qa"])) == 1

    def test_missing_and_rejected_entries_are_counted_as_skipped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        counts = store.import_memory(
            {
                "semantic": [
                    {"value_json": '"orphan"'},  # no key at all
                    {"key": "Not A Key", "value": "x"},  # fails validation
                ],
                "episodic": [
                    {"tags": []},  # no text at all
                    {"text": "tiny"},  # below the minimum length
                ],
            }
        )
        assert counts == {"semantic": 0, "episodic": 0, "skipped": 4}

    def test_an_empty_payload_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.import_memory({}) == {"semantic": 0, "episodic": 0, "skipped": 0}


class TestMigrateFromMarkdown:
    """The legacy markdown/JSONL importer, driven entirely off a tmp_path home."""

    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "crew-home"
        (home / "workspace" / "memory").mkdir(parents=True)
        monkeypatch.setattr(vm, "config_dir", lambda: home)
        return home

    def test_nothing_on_disk_is_a_clean_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(tmp_path, monkeypatch)
        store = _store(tmp_path)
        assert store.migrate_from_markdown() == {"semantic": 0, "episodic": 0, "skipped": 0}

    def test_lessons_jsonl(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = self._home(tmp_path, monkeypatch)
        (home / "lessons.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"rule": "Always pin the release tag before publishing"}),
                    "",
                    json.dumps({"rule": "Never bind a debug server to a public address"}),
                    "{ not json at all",
                    json.dumps({"category": "knowledge"}),  # no rule
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        store = _store(tmp_path)
        counts = store.migrate_from_markdown()
        assert counts["semantic"] == 2
        assert counts["skipped"] == 2
        assert len(store.get_lessons()) == 2

    def test_preferences_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = self._home(tmp_path, monkeypatch)
        oversized = "y" * 5000
        (home / "workspace" / "memory" / "preferences.md").write_text(
            "\n".join(
                [
                    "# Preferences",  # not a bullet
                    "- editor: vim",  # parses into a key/value
                    "- I prefer dark mode",  # parses via the "I prefer" heuristic
                    "- ",  # empty bullet
                    "- a freeform note that has no key and no favourite phrasing",
                    f"- notes: {oversized}",  # parses, but the value is rejected as oversized
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        store = _store(tmp_path)
        counts = store.migrate_from_markdown()

        editor = store.get_semantic("pref.editor")
        general = store.get_semantic("pref.general")
        assert editor is not None and json.loads(editor["value_json"]) == "vim"
        assert general is not None and json.loads(general["value_json"]) == "dark mode"
        assert counts["semantic"] == 2
        # The freeform line lands as episodic; the oversized one is dropped entirely.
        assert counts["episodic"] == 1
        assert counts["skipped"] == 1
        assert store.get_episodic_list(tag_filter=["preference"])

    def test_projects_md(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = self._home(tmp_path, monkeypatch)
        oversized_name = "N" * 5000
        (home / "workspace" / "memory" / "projects.md").write_text(
            "\n".join(
                [
                    "Prose that is not a bullet at all",
                    "- Redwood: the ingestion rewrite",
                    "- The consolidation pass writes semantic keys nightly",
                    "- The consolidation pass writes semantic keys nightly",  # duplicate
                    "- s",  # too short for an episodic write
                    f"- {oversized_name}: rejected as an oversized value",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        store = _store(tmp_path)
        counts = store.migrate_from_markdown()

        name = store.get_semantic("project.name")
        assert name is not None and json.loads(name["value_json"]) == "Redwood"
        assert counts["semantic"] == 1
        assert counts["episodic"] == 1
        # duplicate + too-short child + oversized project name
        assert counts["skipped"] == 3
        rows = store.get_episodic_list(tag_filter=["redwood"])
        assert [r["text"] for r in rows] == [
            "The consolidation pass writes semantic keys nightly"
        ]

    def test_history_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = self._home(tmp_path, monkeypatch)
        history = home / "workspace" / "memory" / "history"
        history.mkdir()
        (history / "a.md").write_text(
            "# Daily log\n"
            "[2026-01-01] The gateway restarted cleanly after the config change.\n"
            "[9] x\n",
            encoding="utf-8",
        )
        (history / "b.md").write_text(
            "<!-- autogenerated, do not edit -->\n"
            "[2026-02-01] The nightly consolidation swept forty two fragments.\n",
            encoding="utf-8",
        )
        (history / "c.md").write_text(
            "[2026-01-01] The gateway restarted cleanly after the config change.\n",
            encoding="utf-8",
        )
        store = _store(tmp_path)
        counts = store.migrate_from_markdown()

        # Two distinct fragments imported; the duplicate in c.md is deduped away
        # and the "#", "<!--" and too-short paragraphs are filtered before that.
        assert counts["episodic"] == 2
        assert counts["skipped"] == 1
        assert len(store.get_episodic_list(tag_filter=["history"])) == 2

    def test_long_history_paragraphs_are_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = self._home(tmp_path, monkeypatch)
        history = home / "workspace" / "memory" / "history"
        history.mkdir()
        (history / "a.md").write_text(
            "[2026-01-01] " + ("w " * 2000) + "\n", encoding="utf-8"
        )
        store = _store(tmp_path)
        assert store.migrate_from_markdown()["episodic"] == 1
        stored = store.get_episodic_list()[0]["text"]
        assert len(stored) <= vm._EPISODIC_TEXT_MAX

    def test_embeddings_are_attached_when_an_embedder_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = self._home(tmp_path, monkeypatch)
        (home / "workspace" / "memory" / "preferences.md").write_text(
            "- a freeform note that has no key and no favourite phrasing\n", encoding="utf-8"
        )
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        assert store.migrate_from_markdown()["episodic"] == 1
        embedded = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        assert embedded == 1


class TestObservability:
    def test_rejection_stats_group_by_reason(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # Not on the allow-list.
        assert store.set_semantic("random.key", "x", 0.9, "user_explicit") is not None
        # Below the confidence gate.
        assert store.set_semantic("pref.editor", "vim", 0.1, "inferred") is not None
        stats = store.get_rejection_stats()
        assert stats.get("allowlist_reject") == 1
        assert stats.get("low_confidence") == 1

    def test_context_preview_reports_sizes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        assert store.write_lesson("Always squash a review branch down to one commit")
        assert store.write_episodic("A fragment worth surfacing in the preview")

        preview = store.get_context_preview(query_text="editor preferences")
        assert preview["semantic_chars"] > 0
        assert preview["episodic_chars"] > 0
        assert preview["lessons_chars"] > 0
        assert preview["total_chars"] == (
            preview["semantic_chars"] + preview["episodic_chars"] + preview["lessons_chars"]
        )
        assert preview["lessons_count"] == 1
        assert "vim" in preview["semantic_preview"]
