"""Vector-space reconciliation: stored embeddings are regenerated on model change.

Before this, nothing recorded WHICH model produced a stored vector. Swapping the
embedding model therefore corrupted search silently — most dangerously when the
new model had the SAME dimensionality (any other 1024-d model), because the
existing dim guard passed and stale vectors were cosine-scored against new-model
queries, returning meaningless similarities with no warning anywhere.

``reconcile_embedding_space`` records the active vector space and clears stale
vectors to NULL, which routes them back through the existing NULL-embedding
re-embed sweep.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.embeddings import embedding_space_signature
from kiro_crew.vector_memory import VectorMemoryStore

_SIG_A = embedding_space_signature("model-a", 1024)
_SIG_B = embedding_space_signature("model-b", 1024)
_SIG_A_OTHER_DIM = embedding_space_signature("model-a", 768)


def _store(tmp_path: Path, dim: int = 8) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim)
    store.init()
    return store


def _fixed_embed(dim: int = 8):
    """One stable vector per text, orthogonal across texts.

    write_episodic rejects a write whose vector scores above the similarity
    dedupe threshold against one already indexed, so a text-independent constant
    collapses every memory seeded after the first into it and the row counts
    below can never reach 2. Basis vectors keep seeded rows distinct while
    repeating the same text still reproduces its original vector, which is what
    the re-embed assertions rely on.
    """
    slots: dict[str, int] = {}

    def embed(text: str) -> list[float]:
        slot = slots.setdefault(text, len(slots) % dim)
        return [1.0 if i == slot else 0.0 for i in range(dim)]

    return embed


def _episodic_with_vectors(store: VectorMemoryStore) -> int:
    return store.db.execute(
        "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NOT NULL"
    ).fetchone()[0]


class TestFirstRunStamping:
    def test_first_call_stamps_without_clearing(self, tmp_path: Path) -> None:
        """A plain upgrade must not force everyone to re-embed their memory.

        Existing vectors were produced by the bundled model, which is still the
        active one, so the first observation records the signature and changes
        nothing.
        """
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory worth keeping around for the test")
        assert _episodic_with_vectors(store) == 1

        assert store.reconcile_embedding_space(_SIG_A) == 0
        assert _episodic_with_vectors(store) == 1

    def test_stamp_is_persisted(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.reconcile_embedding_space(_SIG_A)
        assert store._read_meta("embedding_space_sig") == _SIG_A

    def test_unchanged_signature_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("another memory that is long enough to store")
        store.reconcile_embedding_space(_SIG_A)
        assert store.reconcile_embedding_space(_SIG_A) == 0
        assert _episodic_with_vectors(store) == 1


class TestFirstRunWithACustomModel:
    """The upgrade order that would otherwise corrupt search permanently.

    Stop the gateway, update, point ``embed_model_path`` at a model, start. The
    database has vectors but no recorded space, and the active space is custom —
    so those vectors CANNOT have come from it (custom models postdate every
    stored vector). Stamping the custom signature over them would mark foreign
    vectors as native, and ``backfill_missing_embeddings`` only revisits NULL
    rows, so they would never be re-embedded.
    """

    def test_unattributable_vectors_are_cleared_not_stamped(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory embedded by the bundled model before the swap")
        store.write_episodic("a second memory also from the bundled model here")
        assert _episodic_with_vectors(store) == 2

        invalidated = store.reconcile_embedding_space(_SIG_B, clear_when_unknown=True)

        assert invalidated == 2
        assert _episodic_with_vectors(store) == 0
        assert store._read_meta("embedding_space_sig") == _SIG_B

    def test_cleared_rows_are_re_embedded(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory embedded by the bundled model before the swap")
        store.reconcile_embedding_space(_SIG_B, clear_when_unknown=True)
        assert store.backfill_missing_embeddings() == 1
        assert _episodic_with_vectors(store) == 1

    def test_does_not_re_clear_on_every_boot(self, tmp_path: Path) -> None:
        """The critical regression guard.

        A custom-model host passes ``clear_when_unknown=True`` on EVERY boot.
        Once the signature is recorded, a matching signature must short-circuit
        before the clearing path — otherwise every restart would wipe and
        re-embed the entire corpus.
        """
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory that must survive repeated restarts here")
        store.reconcile_embedding_space(_SIG_B, clear_when_unknown=True)
        store.backfill_missing_embeddings()
        assert _episodic_with_vectors(store) == 1

        for _ in range(3):
            assert store.reconcile_embedding_space(_SIG_B, clear_when_unknown=True) == 0
        assert _episodic_with_vectors(store) == 1

    def test_fresh_install_with_a_custom_model_stamps_cleanly(self, tmp_path: Path) -> None:
        """No stored vectors: nothing to invalidate, but the space is recorded."""
        store = _store(tmp_path)
        assert store.reconcile_embedding_space(_SIG_B, clear_when_unknown=True) == 0
        assert store._read_meta("embedding_space_sig") == _SIG_B

    def test_default_model_host_still_stamps(self, tmp_path: Path) -> None:
        """Guard the other direction: no custom model -> no surprise re-embed."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory that must not be wiped by a plain upgrade")
        assert store.reconcile_embedding_space(_SIG_A, clear_when_unknown=False) == 0
        assert _episodic_with_vectors(store) == 1


class TestModelChangeInvalidates:
    def test_changed_model_clears_episodic_vectors(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("first memory that is long enough to be stored")
        store.write_episodic("second memory that is also long enough here")
        store.reconcile_embedding_space(_SIG_A)
        assert _episodic_with_vectors(store) == 2

        invalidated = store.reconcile_embedding_space(_SIG_B)

        assert invalidated == 2
        assert _episodic_with_vectors(store) == 0
        assert store._read_meta("embedding_space_sig") == _SIG_B

    def test_same_model_different_dim_also_invalidates(self, tmp_path: Path) -> None:
        """Dim is part of the vector space, not just the model name."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        assert store.reconcile_embedding_space(_SIG_A_OTHER_DIM) == 1

    def test_rows_survive_as_keyword_searchable(self, tmp_path: Path) -> None:
        """Invalidation clears VECTORS, never the memories themselves."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        store.reconcile_embedding_space(_SIG_B)
        rows = store.db.execute(
            "SELECT text FROM episodic_memories WHERE is_deleted = 0"
        ).fetchall()
        assert len(rows) == 1
        assert "keyword" not in rows[0]["text"]  # the real text is intact
        assert rows[0]["text"].startswith("a memory long enough")

    def test_semantic_vectors_are_cleared_too(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_lesson("always run the build gate before opening a PR")
        store.reconcile_embedding_space(_SIG_A)
        before = store.db.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        store.reconcile_embedding_space(_SIG_B)
        after = store.db.execute(
            "SELECT COUNT(*) FROM semantic_memory WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        assert before >= 1
        assert after == 0

    def test_stale_faiss_files_are_removed(self, tmp_path: Path) -> None:
        """A persisted index built in the old space must not be reloaded."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        ids_path = store._faiss_path.with_suffix(".ids.json")
        store._faiss_path.write_bytes(b"stale-index")
        ids_path.write_text("[]", encoding="utf-8")

        store.reconcile_embedding_space(_SIG_B)

        assert not store._faiss_path.exists()
        assert not ids_path.exists()
        assert store._faiss_id_map == []


class TestStaleIndexRemovalFailure:
    """A surviving index file must NOT be recorded as a completed reconciliation.

    ``load_faiss_index`` prefers the persisted pair and only checks
    index-vs-id-map consistency (both intact), so stamping the new signature
    while the old files survive would make the corruption permanent: the next
    start reloads old vectors and no later reconcile would run. Reachable on a
    read-only directory, and on Windows where unlink fails while another process
    holds the index mapped.
    """

    @staticmethod
    def _break_unlink(monkeypatch) -> None:
        real_unlink = Path.unlink

        def _refuse(self, *args, **kwargs):
            if self.suffix in (".faiss", ".json") or "faiss" in self.name:
                raise OSError("read-only filesystem")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _refuse)

    def test_signature_is_not_stamped(self, tmp_path: Path, monkeypatch) -> None:
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        store._faiss_path.write_bytes(b"stale-index")

        self._break_unlink(monkeypatch)
        store.reconcile_embedding_space(_SIG_B)

        assert store._read_meta("embedding_space_sig") == _SIG_A, (
            "the OLD signature must survive so the next start retries"
        )

    def test_vectors_are_still_cleared(self, tmp_path: Path, monkeypatch) -> None:
        """Clearing already committed — don't leave mixed vectors queryable."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        store._faiss_path.write_bytes(b"stale-index")

        self._break_unlink(monkeypatch)
        assert store.reconcile_embedding_space(_SIG_B) == 1
        assert _episodic_with_vectors(store) == 0

    def test_next_attempt_succeeds_and_stamps(self, tmp_path: Path, monkeypatch) -> None:
        """The retry on a later start completes the reconciliation."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        store._faiss_path.write_bytes(b"stale-index")

        self._break_unlink(monkeypatch)
        store.reconcile_embedding_space(_SIG_B)
        assert store._read_meta("embedding_space_sig") == _SIG_A

        monkeypatch.undo()  # the next boot can write again
        store.reconcile_embedding_space(_SIG_B)
        assert store._read_meta("embedding_space_sig") == _SIG_B
        assert not store._faiss_path.exists()


class TestReembedAfterInvalidation:
    def test_backfill_re_embeds_the_cleared_rows(self, tmp_path: Path) -> None:
        """The end-to-end guarantee: a model change regenerates embeddings.

        Invalidation deliberately reuses the NULL-embedding sweep rather than a
        parallel mechanism, so this is the same path a deferred first-boot write
        already takes.
        """
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("first memory that is long enough to be stored")
        store.write_episodic("second memory that is also long enough here")
        store.reconcile_embedding_space(_SIG_A)

        store.reconcile_embedding_space(_SIG_B)
        assert _episodic_with_vectors(store) == 0

        embedded = store.backfill_missing_embeddings()

        assert embedded == 2
        assert _episodic_with_vectors(store) == 2

    def test_interrupted_reembed_is_resumed(self, tmp_path: Path) -> None:
        """A sweep that could not embed leaves rows NULL for the next attempt."""
        store = _store(tmp_path)
        store.embed_fn = _fixed_embed()
        store.write_episodic("a memory long enough to survive the length check")
        store.reconcile_embedding_space(_SIG_A)
        store.reconcile_embedding_space(_SIG_B)

        store.embed_fn = lambda text: None  # model not loaded yet
        assert store.backfill_missing_embeddings() == 0
        assert _episodic_with_vectors(store) == 0

        store.embed_fn = _fixed_embed()
        assert store.backfill_missing_embeddings() == 1
        assert _episodic_with_vectors(store) == 1


def _lessons_with_vectors(store: VectorMemoryStore) -> int:
    """Count ACTIVE lessons holding a vector.

    ``is_deleted = 0`` matters: ``write_lesson`` tombstones a lesson it judges a
    duplicate, and a tombstoned row keeps its stale vector — the repair sweep
    deliberately ignores those, so counting them would assert the wrong thing.
    """
    return store.db.execute(
        "SELECT COUNT(*) FROM semantic_memory "
        "WHERE embedding IS NOT NULL AND is_deleted = 0 AND key LIKE 'lesson.%'"
    ).fetchone()[0]


def _orthogonal_embed(dim: int = 16):
    """Embed each distinct text to its own basis vector.

    A constant stub makes every lesson a perfect cosine match, so
    ``write_lesson``'s semantic dedup (threshold 0.85) tombstones all but one and
    the test measures dedup instead of the repair sweep. Assigning each text a
    distinct orthogonal direction keeps the lessons genuinely distinct.
    """
    slots: dict[str, int] = {}

    def _embed(text: str) -> list[float]:
        idx = slots.setdefault(text, len(slots))
        vec = [0.0] * dim
        vec[idx % dim] = 1.0
        return vec

    return _embed


class TestLessonVectorRepair:
    """Cleared lesson vectors must be rebuilt by the sweep, not left to chance.

    ``write_lesson`` refills NULL lesson vectors lazily but only 5 per call, and
    lesson writes are rare — so after a wholesale invalidation, dedup and
    contradiction detection would silently degrade for an unbounded time.
    """

    def test_sweep_repairs_cleared_lesson_vectors(self, tmp_path: Path) -> None:
        store = _store(tmp_path, dim=16)
        store.embed_fn = _orthogonal_embed()
        store.write_lesson("always run the build gate before opening a PR")
        store.write_lesson("never push directly to the main branch of a repo")
        store.reconcile_embedding_space(_SIG_A)
        created = _lessons_with_vectors(store)
        assert created == 2, f"both lessons should stay active, got {created}"

        store.reconcile_embedding_space(_SIG_B)
        assert _lessons_with_vectors(store) == 0

        store.backfill_missing_embeddings()
        assert _lessons_with_vectors(store) == 2

    def test_repair_exceeds_the_lazy_five_row_cap(self, tmp_path: Path) -> None:
        """The sweep is not bounded by _MAX_BACKFILLS_PER_CALL.

        The lessons need distinct VOCABULARY, not just distinct vectors:
        ``write_lesson`` also dedups on keyword overlap, so near-identical
        phrasings collapse to one lesson before embeddings matter.
        """
        store = _store(tmp_path, dim=16)
        store.embed_fn = _orthogonal_embed()
        for rule in (
            "always pin dependency versions in the manifest",
            "never store credentials inside source control",
            "prefer parameterized queries over string concatenation",
            "run the linter before committing python changes",
            "document public interfaces with typed signatures",
            "avoid global mutable singletons in request handlers",
            "cache expensive computations behind an explicit ttl",
            "validate untrusted input at the process boundary",
        ):
            store.write_lesson(rule)
        created = _lessons_with_vectors(store)
        assert created > 5, f"need >5 active lessons to prove the cap is exceeded, got {created}"
        store.reconcile_embedding_space(_SIG_A)
        store.reconcile_embedding_space(_SIG_B)
        assert _lessons_with_vectors(store) == 0

        store.backfill_missing_embeddings()
        assert _lessons_with_vectors(store) == created

    def test_failed_lesson_embed_stays_null_for_retry(self, tmp_path: Path) -> None:
        store = _store(tmp_path, dim=16)
        store.embed_fn = _orthogonal_embed()
        store.write_lesson("always run the build gate before opening a PR")
        store.reconcile_embedding_space(_SIG_A)
        store.reconcile_embedding_space(_SIG_B)

        store.embed_fn = lambda text: None
        store.backfill_missing_embeddings()
        assert _lessons_with_vectors(store) == 0

        store.embed_fn = _orthogonal_embed()
        store.backfill_missing_embeddings()
        assert _lessons_with_vectors(store) == 1

    def test_tombstoned_lessons_are_not_repaired(self, tmp_path: Path) -> None:
        """A deleted lesson is not worth inference — the sweep must skip it."""
        store = _store(tmp_path, dim=16)
        store.embed_fn = _orthogonal_embed()
        store.write_lesson("always run the build gate before opening a PR")
        key = store.db.execute(
            "SELECT key FROM semantic_memory WHERE key LIKE 'lesson.%'"
        ).fetchone()["key"]
        store.reconcile_embedding_space(_SIG_A)
        store.reconcile_embedding_space(_SIG_B)
        store.delete_semantic(key, "user_explicit")

        store.backfill_missing_embeddings()
        row = store.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key = ?", (key,)
        ).fetchone()
        assert row["embedding"] is None

    def test_non_lesson_semantic_rows_are_embedded(self, tmp_path: Path) -> None:
        """Non-lesson rows carry a vector too — written at write time and
        repaired by the backfill sweep — so get_semantic_context can rank them
        without re-embedding the table per request."""
        store = _store(tmp_path, dim=16)
        store.embed_fn = _orthogonal_embed()
        store.set_semantic("pref.os", "linux", 0.9, "user_explicit")
        row = store.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key = 'pref.os'"
        ).fetchone()
        assert row["embedding"] is not None

        # A row that missed the write-time embed (model absent) is repaired by
        # the sweep.
        store.embed_fn = None
        store.set_semantic("pref.editor", "vim", 0.9, "user_explicit")
        store.embed_fn = _orthogonal_embed()
        store.backfill_missing_embeddings()
        row = store.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key = 'pref.editor'"
        ).fetchone()
        assert row["embedding"] is not None
