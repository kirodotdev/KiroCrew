"""The knowledge library's embedding-space primitive and its read-side refusal.

The defect these guard is reachable through the documented ``memory.embed_model_id``
knob and involves no per-store memory at all: without a signature predicate the
vector leg selects on ``embedding IS NOT NULL AND status='active'`` alone, so after
an embedding-model change an OLD-SPACE vector of the SAME WIDTH is cosine-scored
against a NEW-SPACE query and returned with a confident score -- the dimension
guard cannot see a same-width change. ``HybridRetriever._vector_search`` pins the
per-item signature, and the sig-gated rebuild is the only thing that re-stamps it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.embeddings import PRIORITY_NORMAL
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import ANY_EMBEDDING_SPACE, HybridRetriever, vector_leg
from kiro_crew.knowledge.store import KnowledgeBundleError, KnowledgeStore

_SIG_OLD = "aaaaaaaaaaaaaaaa"
_SIG_NEW = "bbbbbbbbbbbbbbbb"

# Same width in both spaces on purpose: the whole point is a change the existing
# dimension guard is blind to.
_VEC = [1.0, 0.0, 0.0, 0.0]


@pytest.fixture()
def store(tmp_path: Path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


class _SpaceDeclaringEmbedder:
    """InProcessEmbedder-shaped stub: declares an identity, loads no model."""

    model = "fake-embed"
    dim = 4
    content_budget = 2000

    def is_available(self) -> bool:
        return True

    async def is_available_async(self) -> bool:
        return True

    def embed(self, text, *, priority=PRIORITY_NORMAL):
        return list(_VEC)


def _add_embedded(store: KnowledgeStore, title: str, sig: str | None) -> str:
    """An active item carrying a stored vector, stamped with *sig*."""
    item_id = store.add_item(title, f"{title} body", "document", embedding=floats_to_bytes(_VEC))
    store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, item_id))
    store.db.commit()
    return item_id


class TestReadSideRefusal:
    """A vector is scored only against a query from its own embedding space."""

    def test_matching_signature_is_scored(self, store):
        _add_embedded(store, "Quorum design", _SIG_NEW)
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        assert len(retriever._vector_search("quorum") or []) == 1

    def test_foreign_same_width_signature_is_refused(self, store):
        """The case the dimension guard cannot catch, and the actual live bug.

        The stored vector is exactly as wide as the query's, so every length
        check passes and only the signature stands between it and a confident
        score.
        """
        _add_embedded(store, "Quorum design", _SIG_OLD)
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        assert retriever._vector_search("quorum") == []

    def test_nulled_signature_is_refused(self, store):
        """A NULLed signature is unproven provenance, not a free pass."""
        _add_embedded(store, "Quorum design", None)
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        assert retriever._vector_search("quorum") == []

    def test_refusal_composes_with_a_source_scope(self, store):
        """Both predicates bind their own placeholder.

        A scoped search appends two more ``?`` after the signature's, so the
        parameter tuple has to be extended rather than rebound — rebinding it
        binds the source id to the signature's placeholder, which either raises
        or silently matches nothing.
        """
        sid = store.add_source("src", "local_folder", "/tmp/kb-scope")
        item_id = store.add_item(
            "Scoped quorum", "body", "document", source_id=sid, embedding=floats_to_bytes(_VEC)
        )
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (_SIG_NEW, item_id))
        store.db.commit()
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        assert len(retriever._vector_search("quorum", source_id=sid) or []) == 1
        assert retriever._vector_search("quorum", source_id="no-such-source") == []

    def test_the_unfiltered_leg_must_be_asked_for_by_name(self, store):
        """The unsafe state is reachable only by naming it.

        A caller holding a bare ``callable(str) -> list[float]`` has no
        declarable identity and may opt out -- but it has to say so, because the
        predicate fails OPEN and an omitted signature is indistinguishable from a
        deliberate one at every later assertion.
        """
        _add_embedded(store, "Quorum design", _SIG_OLD)
        retriever = HybridRetriever(
            store, embedder=lambda q: list(_VEC), embed_sig=ANY_EMBEDDING_SPACE
        )
        assert len(retriever._vector_search("quorum") or []) == 1

    def test_wiring_an_embedder_without_a_signature_raises(self, store):
        """The safe state is the default: forgetting the signature is a hard error.

        A defaulted signature makes the whole fix fail OPEN: a retriever built
        without one scores every space, and no downstream assertion goes red for
        it. So the ambiguous construction is refused where the caller still holds
        the embedder the signature is read from.
        """
        with pytest.raises(ValueError, match="requires embed_sig"):
            HybridRetriever(store, embedder=lambda q: list(_VEC))
        # An empty string is the same omission spelled differently.
        with pytest.raises(ValueError, match="requires embed_sig"):
            HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig="")

    def test_no_embedder_needs_no_signature(self, store):
        """With the leg off the signature is moot, so ``None`` stands there."""
        retriever = HybridRetriever(store)
        assert retriever.embed_sig is None
        assert retriever._vector_search("quorum") is None

    def test_the_opt_out_sentinel_cannot_collide_with_a_real_signature(self):
        """Otherwise a store could stamp rows that silently disable the predicate.

        Signatures are lowercase hex digests; the sentinel is spelled with
        characters no digest contains.
        """
        assert not all(c in "0123456789abcdef" for c in ANY_EMBEDDING_SPACE)


class TestDegradedSearchStillAnswers:
    """Zero vector candidates degrades to FTS5 + graph; it does not raise or empty."""

    def test_search_returns_keyword_results_with_no_usable_vectors(self, store):
        _add_embedded(store, "Quorum replication ledger", _SIG_OLD)
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        results = retriever.search("quorum")
        assert [r["title"] for r in results] == ["Quorum replication ledger"]
        # The leg contributed nothing, so it is absent from match_type -- and the
        # empty leg is folded into RRF rather than short-circuiting the fusion.
        assert "vector" not in results[0]["match_type"]
        assert "keyword" in results[0]["match_type"]

    def test_empty_vector_leg_is_a_list_not_none(self, store):
        """``[]`` and ``None`` are NOT interchangeable to the caller.

        ``search`` records which legs a hit appeared in, and ``_rrf_fuse`` skips
        only ``None``. A refusal that returned ``None`` would read as "no embedder
        wired" instead of "no comparable vectors", which is a different fact.
        """
        _add_embedded(store, "Quorum design", _SIG_OLD)
        retriever = HybridRetriever(store, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
        assert retriever._vector_search("quorum") == []
        assert HybridRetriever(store, embedder=None)._vector_search("quorum") is None


class TestVectorLegPairing:
    """The embedder and the signature it implies are resolved in one place."""

    def test_no_embedder_yields_no_signature(self):
        assert vector_leg(None) == (None, None)

    def test_an_embedder_always_arrives_with_its_signature(self):
        from kiro_crew.knowledge.embedder import embedder_signature

        class _E:
            model = "m"
            dim = 4
            content_budget = 2000

            def embed(self, text, *, priority=PRIORITY_NORMAL):
                return list(_VEC)

        embedder = _E()
        embed_fn, sig = vector_leg(embedder)
        assert embed_fn == embedder.embed
        assert sig == embedder_signature(embedder)


class TestBundleRoundTripCarriesTheSpace:
    """``embedding_sig`` travels with the blob, or the read-side refusal eats it.

    ``export_all`` ships the signature because it serializes ``SELECT * FROM
    items``. An item INSERT that omits it lands every imported row at NULL, which
    the vector leg reads as unproven provenance -- so a bundle's vectors are
    present, comparable, and permanently unscored until a full re-embed.
    """

    def test_round_trip_keeps_imported_vectors_searchable(self, tmp_path: Path):
        source = KnowledgeStore(str(tmp_path / "export.db"))
        try:
            _add_embedded(source, "Quorum replication ledger", _SIG_NEW)
            bundle = source.export_all()
        finally:
            source.close()
        assert [i["embedding_sig"] for i in bundle["items"]] == [_SIG_NEW]

        target = KnowledgeStore(str(tmp_path / "import.db"))
        try:
            assert target.import_bundle(bundle)["items_imported"] == 1
            row = target.db.execute("SELECT embedding, embedding_sig FROM items").fetchone()
            assert row["embedding_sig"] == _SIG_NEW
            assert row["embedding"] is not None
            retriever = HybridRetriever(target, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
            assert len(retriever._vector_search("quorum") or []) == 1
        finally:
            target.close()

    def test_a_foreign_space_signature_survives_the_import_and_is_refused(self, tmp_path: Path):
        """Carrying it through is what makes the refusal deliberate.

        A bundle from a store on another embedding model must arrive stamped with
        THAT space, so the importing store refuses it on the signature rather than
        by accident on a NULL -- and re-stamps it when the rebuild re-embeds.
        """
        source = KnowledgeStore(str(tmp_path / "export-foreign.db"))
        try:
            _add_embedded(source, "Quorum replication ledger", _SIG_OLD)
            bundle = source.export_all()
        finally:
            source.close()

        target = KnowledgeStore(str(tmp_path / "import-foreign.db"))
        try:
            target.import_bundle(bundle)
            assert (
                target.db.execute("SELECT embedding_sig FROM items").fetchone()["embedding_sig"]
                == _SIG_OLD
            )
            retriever = HybridRetriever(target, embedder=lambda q: list(_VEC), embed_sig=_SIG_NEW)
            assert retriever._vector_search("quorum") == []
            # ...and the item is still answerable from FTS5.
            assert [r["title"] for r in retriever.search("quorum")] == ["Quorum replication ledger"]
        finally:
            target.close()

    def test_a_legacy_bundle_without_the_signature_imports_as_unproven(self, tmp_path: Path):
        """An absent key is NULL, not a crash: the column is nullable by design."""
        target = KnowledgeStore(str(tmp_path / "import-legacy.db"))
        try:
            target.import_bundle(
                {
                    "items": [
                        {
                            "id": "legacy-1",
                            "title": "Quorum",
                            "content": "body",
                            "item_type": "document",
                        }
                    ]
                }
            )
            assert (
                target.db.execute("SELECT embedding_sig FROM items").fetchone()["embedding_sig"]
                is None
            )
        finally:
            target.close()

    def test_a_non_string_signature_is_a_typed_rejection(self, tmp_path: Path):
        """Not a bind-time driver error escaping the typed-error contract.

        The value's grammar belongs to its producer, so the store checks only the
        shape -- but a non-string reaching the bind would raise past
        ``KnowledgeBundleError`` and lose the import handler's 400.
        """
        target = KnowledgeStore(str(tmp_path / "import-bad-sig.db"))
        try:
            with pytest.raises(KnowledgeBundleError, match="items.embedding_sig"):
                target.import_bundle(
                    {
                        "items": [
                            {
                                "id": "bad-1",
                                "title": "Quorum",
                                "content": "body",
                                "item_type": "document",
                                "embedding_sig": {"not": "a string"},
                            }
                        ]
                    }
                )
            assert target.db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0
        finally:
            target.close()


class TestEveryProductionCallSitePinsTheSpace:
    """All three retrieval entry points wire the signature, not just one.

    The constructor refuses an unpinned embedder, so a call site that forgets
    cannot serve a cross-space score -- but it would 500 instead, which is its own
    outage. These pin the value each one actually passes.
    """

    @pytest.mark.asyncio
    async def test_dashboard_items_search_pins_the_space(self, store, monkeypatch):
        """GET /api/knowledge/items?q="""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import knowledge as kh
        from kiro_crew.knowledge.embedder import embedder_signature

        seen: dict[str, object] = {}

        def _retriever(_store, embedder=None, *, embed_sig=None):
            seen["embedder"] = embedder
            seen["embed_sig"] = embed_sig
            return MagicMock(search=MagicMock(return_value=[]))

        async def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        emb = _SpaceDeclaringEmbedder()
        monkeypatch.setattr(kh, "HybridRetriever", _retriever)
        monkeypatch.setattr(kh, "run_in_embed_pool", _direct)
        monkeypatch.setattr(kh, "_store", lambda _request: store)
        request = MagicMock()
        request.query = {"q": "quorum"}
        request.app = {"knowledge_embedder": emb}
        assert (await kh.list_items(request)).status == 200
        assert seen["embedder"] == emb.embed
        assert seen["embed_sig"] == embedder_signature(emb)

    def test_the_mcp_tool_pins_the_space(self, store, monkeypatch, tmp_path):
        """The ``local_knowledge_search`` MCP tool."""
        from unittest.mock import MagicMock

        from kiro_crew import mcp_core
        from kiro_crew.knowledge.embedder import embedder_signature

        seen: dict[str, object] = {}

        def _retriever(_store, embedder=None, *, embed_sig=None):
            seen["embedder"] = embedder
            seen["embed_sig"] = embed_sig
            return MagicMock(search=MagicMock(return_value=[]))

        emb = _SpaceDeclaringEmbedder()
        db_dir = tmp_path / "workspace" / "knowledge"
        db_dir.mkdir(parents=True)
        (db_dir / "knowledge.db").touch()
        monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(mcp_core, "HybridRetriever", _retriever)
        monkeypatch.setattr(mcp_core, "_get_knowledge_search", lambda _db, _cfg: (store, emb))

        mcp_core._call_tool_inner("local_knowledge_search", {"query": "quorum"})
        assert seen["embedder"] == emb.embed
        assert seen["embed_sig"] == embedder_signature(emb)
