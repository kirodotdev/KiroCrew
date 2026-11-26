"""A recall query takes the model's query instruction; a stored memory never does.

Qwen3-Embedding is trained with an instruction on the query side only, and the
instructed query lands on a lower cosine scale, so the gates read their own
floors for it. The backend here is a stand-in reached through the production
``make_sync_embed_fn``: it records the text each inference saw and answers a
fixed vector, so a cosine is a number this file sets by hand.
"""

from __future__ import annotations

import math

import pytest

from kiro_crew import embeddings, memory_v2
from kiro_crew.embeddings import PRIORITY_NORMAL
from kiro_crew.vector_memory import VectorMemoryStore
from kiro_crew.vector_memory_runtime import episodic_search

MEMORY = "Ship builds from the release branch only."
QUERY = "where do we cut versions"
INSTRUCTED_QUERY = f"Instruct: {embeddings.QWEN3_QUERY_TASK}\nQuery:{QUERY}"


class _FixedBackend(embeddings.EmbeddingBackend):
    """Answers *query_cosine* against the memory axis for the query, and the axis for the rest."""

    def __init__(self, model_id: str, query_cosine: float, seen: list[str]) -> None:
        self._model_id = model_id
        self._query = [query_cosine, math.sqrt(1 - query_cosine**2)]
        self._seen = seen

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return 2

    def is_ready(self) -> bool:
        return True

    def embed(self, text: str, *, priority: int = PRIORITY_NORMAL) -> list[float]:
        self._seen.append(text)
        return list(self._query) if QUERY in text else [1.0, 0.0]

    def embed_batch(
        self, texts: list[str], *, priority: int = PRIORITY_NORMAL
    ) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def close(self) -> None:
        """Nothing to release."""


def _store(tmp_path, monkeypatch, model_id: str, query_cosine: float, seen: list[str]):
    # Set the factory and the singleton slot through monkeypatch, as the V1 golden
    # test does, so teardown restores them without retiring another test's embedder.
    monkeypatch.setattr(
        embeddings,
        "_backend_factory",
        lambda: _FixedBackend(model_id, query_cosine, seen),
        raising=False,
    )
    monkeypatch.setattr(embeddings, "_shared_embedder", None, raising=False)
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    store.embed_fn = embeddings.make_sync_embed_fn()
    return store


@pytest.mark.parametrize(
    "model_id,applies",
    [
        ("qwen3-embedding:0.6b", True),
        ("ollama:qwen3-embedding", True),
        ("ollama:qwen3-embedding:8b", True),
        ("ollama:nomic-embed-text", False),
        ("ollama:embeddinggemma", False),
        ("custom:sha256:" + "0" * 64, False),
    ],
)
def test_only_qwen3_embedding_takes_the_query_instruction(model_id, applies):
    assert embeddings.query_instruction_applies(model_id) is applies


def test_the_instruction_is_the_model_card_query_prompt():
    assert embeddings.query_instruction_text("q") == (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query"
        "\nQuery:q"
    )


def test_the_app_sdk_shares_the_one_formatter():
    from kiro_crew.apps import embed_sdk

    assert embed_sdk._prepare(["q"], " find docs ") == [
        embeddings.query_instruction_text("q", "find docs")
    ]
    assert embed_sdk._prepare(["q"], None) == ["q"]
    assert embed_sdk._prepare(["q"], "  ") is None


def test_instructed_floors_are_their_own():
    long_text = "x" * (memory_v2.LONG_TEXT_CHARS + 1)
    assert memory_v2.cosine_floor("short") == 0.62
    assert memory_v2.cosine_floor(long_text) == 0.57
    assert memory_v2.cosine_floor("short", instructed=True) == 0.55
    assert memory_v2.cosine_floor(long_text, instructed=True) == 0.57
    evidence = memory_v2.relevance_evidence([], "short", 0.56, instructed=True)
    assert evidence["admitted"] and evidence["cosine_floor"] == 0.55
    assert not memory_v2.relevance_evidence([], "short", 0.56)["admitted"]


def test_v1_instructed_thresholds(tmp_path):
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        long_text = "x" * 301
        assert store._episodic_relevance_threshold("short") == 0.55
        assert store._episodic_relevance_threshold(long_text) == 0.42
        assert store._episodic_relevance_threshold("short", instructed=True) == 0.515
        assert store._episodic_relevance_threshold(long_text, instructed=True) == 0.375
    finally:
        store.close()


def test_a_query_is_embedded_with_the_instruction_and_a_memory_is_not(tmp_path, monkeypatch):
    seen: list[str] = []
    store = _store(tmp_path, monkeypatch, "qwen3-embedding:0.6b", 0.53, seen)
    try:
        assert store.write_episodic(MEMORY, importance=0.5, source="consolidation")
        assert seen == [MEMORY]
        vector = store._try_embed(QUERY, query=True)
        assert seen[-1] == INSTRUCTED_QUERY
        assert vector is not None and vector.instructed
        plain = store._try_embed(QUERY)
        assert seen[-1] == QUERY and not plain.instructed
    finally:
        store.close()


@pytest.mark.parametrize(
    "model_id,admitted",
    [("qwen3-embedding:0.6b", True), ("ollama:nomic-embed-text", False)],
)
def test_recall_gates_an_instructed_query_on_its_own_floor(
    tmp_path, monkeypatch, model_id, admitted
):
    """Cosine 0.53 sits between the instructed V1 floor (0.515) and the plain one (0.55).

    The query and the memory share no word, so only the cosine gate can admit it:
    the Qwen3 query is instructed and admitted; another model's stays plain and is not.
    """
    seen: list[str] = []
    store = _store(tmp_path, monkeypatch, model_id, 0.53, seen)
    try:
        assert store.write_episodic(MEMORY, importance=0.5, source="consolidation")
        result = store.recall(QUERY)
        texts = [row.get("text") for row in result["retrieval"]["episodes"]]
        assert (MEMORY in texts) is admitted
        assert (INSTRUCTED_QUERY in seen) is admitted
        assert MEMORY in seen and seen.count(MEMORY) == 1
    finally:
        store.close()


def test_the_v1_gate_reads_the_flag_on_the_query_vector(tmp_path, monkeypatch):
    seen: list[str] = []
    store = _store(tmp_path, monkeypatch, "qwen3-embedding:0.6b", 0.53, seen)
    try:
        assert store.write_episodic(MEMORY, importance=0.5, source="consolidation")
        instructed = store._try_embed(QUERY, query=True)
        plain_list = list(instructed)
        hit = store.search_episodic(instructed, QUERY, relevance_filter=True)
        miss = store.search_episodic(plain_list, QUERY, relevance_filter=True)
        assert [r["text"] for r in hit] == [MEMORY]
        assert miss == []
        assert episodic_search._embedding.query_is_instructed(instructed)
        assert not episodic_search._embedding.query_is_instructed(plain_list)
    finally:
        store.close()


def test_the_row_filter_gate_reads_instructed(tmp_path):
    """The FAISS and stdlib rungs gate through ``_filter_by_relevance``."""
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    try:
        rows = [{"cosine_sim": 0.53, "text": MEMORY}]
        assert store._filter_by_relevance(rows, instructed=True) == rows
        assert store._filter_by_relevance(rows) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "model_id,admitted",
    [("qwen3-embedding:0.6b", True), ("ollama:nomic-embed-text", False)],
)
def test_v2_recall_gates_an_instructed_query_on_its_own_floor(
    tmp_path, monkeypatch, model_id, admitted
):
    """Cosine 0.58 sits between the instructed V2 short floor (0.55) and the plain one (0.62)."""
    from member_memory_helpers import declare_v2_store

    from kiro_crew import memory_stores
    from kiro_crew.vector_memory import open_member_database

    seen: list[str] = []
    monkeypatch.setattr(
        embeddings,
        "_backend_factory",
        lambda: _FixedBackend(model_id, 0.58, seen),
        raising=False,
    )
    monkeypatch.setattr(embeddings, "_shared_embedder", None, raising=False)
    root = tmp_path / "memory_stores"
    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: root)
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=2
    )
    store.embed_fn = embeddings.make_sync_embed_fn()
    try:
        assert store.algorithm_version == "v2"
        # A private store records its space when the gateway binds it; do the same.
        embeddings.reconcile_store_embedding_space(store)
        assert store.write_episodic(MEMORY, importance=0.5, source="consolidation")
        assert store.set_semantic("project.branching", MEMORY, 1.0, "user_explicit") is None
        result = store.recall(QUERY)
        episodes = result["retrieval"]["episodes"]
        facts = result["retrieval"]["facts"]
        assert (MEMORY in [row.get("text") for row in episodes]) is admitted
        assert ("project.branching" in [row.get("key") for row in facts]) is admitted
        point = result["retrieval"]["operating_point"]
        assert point["short_cosine_floor"] == (0.55 if admitted else 0.62)
        if admitted:
            assert episodes[0]["retrieval"]["cosine_floor"] == 0.55
        assert (INSTRUCTED_QUERY in seen) is admitted
        assert seen.count(MEMORY) == 1
        # The relevance rule re-gates V2 facts on the flag in their evidence.
        enforced = store.get_semantic_context(QUERY, 1500, relevance_rule="enforce")
        assert ("project.branching" in enforced) is admitted
    finally:
        store.close()


def test_fact_relevance_rule_reads_instructed():
    from kiro_crew import memory_relevance_rule

    assert memory_relevance_rule.fact_admitted([], "short", 0.56, instructed=True)
    assert not memory_relevance_rule.fact_admitted([], "short", 0.56)


def test_the_prompt_build_finds_the_early_query_embed_in_flight(monkeypatch):
    """The early embed and the build's wait must agree on the instructed text."""
    backend = _FixedBackend("qwen3-embedding:0.6b", 0.53, [])
    monkeypatch.setattr(embeddings, "_shared_embedder", backend, raising=False)
    monkeypatch.setitem(embeddings._sync_embed_flights, (id(backend), INSTRUCTED_QUERY), object())
    assert embeddings.embedding_in_flight(QUERY, query=True)
    assert not embeddings.embedding_in_flight(QUERY)


# ── The recall worker processes (``memory.recall_pool_processes``) ──────────


def test_the_gateway_embeds_a_child_query_with_the_instruction(tmp_path, monkeypatch):
    from kiro_crew.consolidation_write_offload import _serve_embed

    seen: list[str] = []
    store = _store(tmp_path, monkeypatch, "qwen3-embedding:0.6b", 0.53, seen)
    try:
        serve = _serve_embed(store)
        assert serve({"text": QUERY, "query": True})["instructed"] is True
        assert seen[-1] == INSTRUCTED_QUERY
        assert serve({"text": QUERY})["instructed"] is False
        assert seen[-1] == QUERY
    finally:
        store.close()


def test_the_child_proxy_asks_for_a_query_and_keeps_the_flag():
    from unittest.mock import MagicMock

    from kiro_crew import consolidation_write_child as cwc

    rpc = MagicMock()
    rpc.next.return_value = {"vector": [1.0, 0.0], "token": ["sig", ""], "instructed": True}
    store = MagicMock()
    cwc._ChildState(rpc)._route_embeds(store)
    vector = store._try_embed(QUERY, query=True)
    assert rpc.send.call_args.args[0]["query"] is True
    assert vector.instructed
    store._try_embed(MEMORY)
    assert rpc.send.call_args.args[0]["query"] is False


def test_a_pooled_query_vector_keeps_its_flag():
    from kiro_crew import recall_pool

    rebuilt = recall_pool._query_vector([0.53, 0.85], True)
    assert episodic_search._embedding.query_is_instructed(rebuilt)
    assert recall_pool._query_vector([0.53, 0.85], None) == [0.53, 0.85]
    assert not episodic_search._embedding.query_is_instructed(
        recall_pool._query_vector([0.53, 0.85], False)
    )
    assert recall_pool._query_vector(None, True) is None


def test_a_pooled_episodic_read_sends_the_flag(tmp_path, monkeypatch):
    from kiro_crew import recall_pool

    seen: list[str] = []
    store = _store(tmp_path, monkeypatch, "qwen3-embedding:0.6b", 0.53, seen)
    sent: list[dict] = []
    monkeypatch.setattr(recall_pool, "offload", lambda _s, _k, args: sent.append(args) or "")
    try:
        store.get_episodic_context(store._try_embed(QUERY, query=True), QUERY)
        store.get_episodic_context([0.53, 0.85], QUERY)
        assert [args["query_instructed"] for args in sent] == [True, False]
    finally:
        store.close()
