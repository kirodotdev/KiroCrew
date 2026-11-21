"""A stand-in for the in-process embedding model, shared by the session search-by-meaning suites.

Its vectors put words of one topic on one axis, so a paraphrase lands near its
session without a model, and ``install_leg`` routes the chunk pass, the fill and
the vector leg through it as they run in production. Used by
``test_history_vectors_search.py`` and ``test_bench_session_recall.py``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from kiro_crew import embeddings
from kiro_crew import history_vectors_fill as fill
from kiro_crew import history_vectors_index as chunking
from kiro_crew.history import ConversationLog

_TOKENIZER = chunking.Tokenizer(lambda text: len(text.split()), "words-1")

#: Topic axes: a word votes for its topic, so texts sharing a topic share a direction.
_TOPICS = {
    word: axis
    for axis, words in enumerate(
        [
            "database sqlite postgres schema migration table",
            "deploy pipeline release rollout ship shipping",
            "lunch pizza food dinner",
            "alarm pager latency oncall",
        ]
    )
    for word in words.split()
}
_DIM = len(set(_TOPICS.values())) + 1


class _Embedder:
    """A stand-in for the in-process model: a topic vector per text."""

    model_id = "topics-1"
    dim = _DIM

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def vector(text: str) -> list[float]:
        out = [0.0] * _DIM
        for raw in text.casefold().replace("?", " ").split():
            word = raw.strip(".,")
            if word in _TOPICS:
                out[_TOPICS[word]] += 1.0
            else:
                out[_DIM - 1] += 0.05
        return out

    def embed(self, text, *, priority=embeddings.PRIORITY_NORMAL, role="document"):
        self.calls.append((text, role))
        return self.vector(text)

    def embed_batch(self, texts, *, priority=embeddings.PRIORITY_NORMAL, role="document"):
        return [self.vector(text) for text in texts]


def install_leg(monkeypatch: pytest.MonkeyPatch) -> "_Embedder":
    """Route the chunk pass, the fill and the vector leg through a fresh stand-in."""
    embedder = _Embedder()
    monkeypatch.setattr(embeddings, "get_shared_embedder", lambda: embedder)
    monkeypatch.setattr(embeddings, "embedding_space_signature", lambda model_id, dim: model_id)
    monkeypatch.setattr(embeddings, "active_embedding_space_signature", lambda: "topics-1")
    monkeypatch.setattr(fill, "embeds_on_this_host", lambda e: isinstance(e, _Embedder))
    monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
    monkeypatch.setattr(chunking, "load_tokenizer", lambda: _TOKENIZER)
    return embedder


def _write(
    base: Path,
    key: str,
    *texts: str,
    mode: str = "persistent",
    age: float = 3600,
    workspace: str | None = None,
    title: str | None = None,
) -> None:
    meta = {"_type": "metadata", "title": title or f"session {key}", "memory_mode": mode}
    if workspace is not None:
        meta["workspace"] = workspace
    lines = [json.dumps(meta)]
    lines += [json.dumps({"role": "user", "content": text}) for text in texts]
    path = base / f"{key}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def _build(base: Path) -> None:
    """Chunk and embed every window session, as the worker and the gateway fill do."""
    log = ConversationLog(base_dir=base)
    store = chunking.vector_store_for(base)
    try:
        chunking.backfill_chunks(log, store, _TOKENIZER)
        while fill.fill_batch(log, store)["filled"]:
            pass
    finally:
        store.close()


def _corpus(base: Path) -> None:
    _write(base, "db", "We moved the schema to postgres and ran the migration on the table.")
    _write(base, "ship", "The rollout went through the pipeline and the release shipped.")
    _write(base, "food", "We ordered pizza for lunch and talked about dinner.")
