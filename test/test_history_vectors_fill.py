"""Tests for the gateway-side vector fill and the coverage counts.

The embedder and its space signature are stand-ins set on the ``embeddings``
facade, so no model loads. Covers a batch written with its text deleted, a batch
discarded when the model is unavailable or changes mid-embed, a chunk dropped
while it embedded, vectors from an old space re-queued through the chunk pass,
counts that do not move with the number of private sessions or count vectors from
an old space, the fill never creating a store, its pacing, and the packaged
recall fixture reaching full coverage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import embeddings
from kiro_crew import history_vectors_fill as fill
from kiro_crew import history_vectors_index as chunking
from kiro_crew.eval.bench import session_recall
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until

_TOKENIZER = chunking.Tokenizer(lambda text: len(text.split()), "words-1")


class _Embedder:
    """A fake in-process model. Its space follows the active one unless *model_id* pins it."""

    dim = 4

    def __init__(self, state: dict, model_id: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.result: str = "vectors"
        self.during = None
        self._state = state
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id or self._state["signature"]

    def embed_batch(self, texts: list[str], *, priority: int) -> list[list[float]] | None:
        assert priority == embeddings.PRIORITY_BULK
        self.calls.append(list(texts))
        if self.during is not None:
            self.during()
        if self.result == "none":
            return None
        return [[b / 255 for b in hashlib.sha256(t.encode()).digest()[:4]] for t in texts]


@pytest.fixture
def space(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"signature": "space-1"}
    state["embedder"] = _Embedder(state)
    monkeypatch.setattr(embeddings, "active_embedding_space_signature", lambda: state["signature"])
    monkeypatch.setattr(embeddings, "embedding_space_signature", lambda model_id, dim: model_id)
    monkeypatch.setattr(embeddings, "get_shared_embedder", lambda: state["embedder"])
    monkeypatch.setattr(
        fill, "embeds_on_this_host", lambda embedder: isinstance(embedder, _Embedder)
    )
    return state


def _write(base: Path, key: str, *texts: str, mode: str = "persistent") -> None:
    lines = [json.dumps({"_type": "metadata", "title": f"session {key}", "memory_mode": mode})]
    lines += [json.dumps({"role": "user", "content": text}) for text in texts]
    path = base / f"{key}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = time.time() - 3600
    os.utime(path, (stamp, stamp))


def _chunk(base: Path) -> None:
    store = chunking.vector_store_for(base)
    chunking.backfill_chunks(ConversationLog(base_dir=base), store, _TOKENIZER)
    store.close()


def _drain(base: Path) -> None:
    store = chunking.vector_store_for(base)
    while fill.fill_batch(ConversationLog(base_dir=base), store)["filled"]:
        pass
    store.close()


def _rows(base: Path, sql: str) -> list[tuple]:
    db = sqlite3.connect(base / ".index" / "session_vectors.db")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def _coverage(base: Path) -> dict[str, int]:
    store = chunking.vector_store_for(base)
    try:
        return fill.coverage(ConversationLog(base_dir=base), store)
    finally:
        store.close()


class TestFillBatch:
    def test_a_batch_writes_vectors_and_deletes_their_text(self, tmp_path: Path, space) -> None:
        _write(tmp_path, "s", "first words", "second words")
        _chunk(tmp_path)

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path), limit=1
        )

        assert report == {"filled": 1, "pending": 1}
        assert _rows(tmp_path, "SELECT count(*) FROM chunk WHERE vector IS NOT NULL") == [(1,)]
        assert _rows(tmp_path, "SELECT signature FROM chunk WHERE vector IS NOT NULL") == [
            ("space-1",)
        ]

    def test_an_unavailable_model_writes_nothing(self, tmp_path: Path, space) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        space["embedder"].result = "none"

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report == {"filled": 0, "pending": 1}
        assert _rows(tmp_path, "SELECT count(*) FROM pending_text") == [(1,)]

    def test_a_model_change_mid_batch_discards_it(self, tmp_path: Path, space) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        space["embedder"].during = lambda: space.update(signature="space-2")

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report["filled"] == 0
        assert _rows(tmp_path, "SELECT count(*) FROM chunk WHERE vector IS NOT NULL") == [(0,)]

    def test_a_session_made_private_before_its_batch_is_not_embedded(
        self, tmp_path: Path, space
    ) -> None:
        _write(tmp_path, "s", "secret words")
        _chunk(tmp_path)
        _write(tmp_path, "s", "secret words", mode="temporary")

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report == {"filled": 0, "pending": 0}
        assert space["embedder"].calls == []
        assert _rows(tmp_path, "SELECT count(*) FROM chunk") == [(0,)]

    def test_unreadable_privacy_is_not_embedded(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.history_derivation import TranscriptBusy

        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)

        def busy(self: ConversationLog, key: str) -> None:
            raise TranscriptBusy(key)

        monkeypatch.setattr(ConversationLog, "_withhold_if_restricted", busy)

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report["filled"] == 0
        assert space["embedder"].calls == []
        assert _rows(tmp_path, "SELECT count(*) FROM pending_text") == [(0,)]

    def test_a_shared_chunk_is_embedded_while_one_holder_is_visible(
        self, tmp_path: Path, space
    ) -> None:
        _write(tmp_path, "open", "shared words")
        _write(tmp_path, "secret", "shared words")
        _chunk(tmp_path)
        _write(tmp_path, "secret", "shared words", mode="incognito")

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report == {"filled": 1, "pending": 0}
        assert _rows(tmp_path, "SELECT DISTINCT session_key FROM chunk_ref") == [("open",)]

    def test_a_session_gone_before_its_batch_is_not_embedded(self, tmp_path: Path, space) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        (tmp_path / "s.jsonl").unlink()

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report == {"filled": 0, "pending": 0}
        assert space["embedder"].calls == []

    def test_a_held_lock_skips_the_chunk_without_dropping_it(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        monkeypatch.setattr(fill, "session_embeddable", lambda log, key: None)

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert report == {"filled": 0, "pending": 1}
        assert space["embedder"].calls == []

    def test_a_model_swapped_after_capture_does_not_label_old_vectors(
        self, tmp_path: Path, space
    ) -> None:
        """The batch runs on the embedder it captured; its space names the vectors."""
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        space["embedder"] = fill_embedder = _Embedder(space, model_id="space-1")
        space["signature"] = "space-2"

        report = fill.fill_batch(
            ConversationLog(base_dir=tmp_path), chunking.vector_store_for(tmp_path)
        )

        assert fill_embedder.calls == [["some words"]]
        assert report["filled"] == 0
        assert _rows(tmp_path, "SELECT count(*) FROM chunk WHERE vector IS NOT NULL") == [(0,)]

    def test_a_chunk_dropped_while_it_embedded_is_skipped(self, tmp_path: Path, space) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        store = chunking.vector_store_for(tmp_path)
        space["embedder"].during = lambda: chunking.vector_store_for(tmp_path).drop(["s"])

        report = fill.fill_batch(ConversationLog(base_dir=tmp_path), store)

        assert report == {"filled": 0, "pending": 0}
        assert _rows(tmp_path, "SELECT count(*) FROM chunk") == [(0,)]

    def test_vectors_from_another_space_are_requeued_and_refilled(
        self, tmp_path: Path, space
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        _drain(tmp_path)
        assert _coverage(tmp_path)["embedded"] == 1

        space["signature"] = "space-2"
        _drain(tmp_path)
        assert _coverage(tmp_path)["embedded"] == 0
        assert _rows(tmp_path, "SELECT chunker FROM session_state") == [("",)]

        _chunk(tmp_path)
        _drain(tmp_path)
        assert _coverage(tmp_path)["embedded"] == 1
        assert _rows(tmp_path, "SELECT signature FROM chunk") == [("space-2",)]

    def test_a_session_released_by_a_model_change_is_still_pruned(
        self, tmp_path: Path, space
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        _drain(tmp_path)
        space["signature"] = "space-2"
        _drain(tmp_path)

        _write(tmp_path, "s", "some words", mode="incognito")
        _chunk(tmp_path)

        assert _rows(tmp_path, "SELECT count(*) FROM chunk_ref") == [(0,)]
        assert _rows(tmp_path, "SELECT count(*) FROM chunk") == [(0,)]

    def test_a_session_turned_private_mid_embed_leaves_no_vector(
        self, tmp_path: Path, space
    ) -> None:
        """A dropped chunk's id is not reused, so its vector cannot land on a new chunk."""
        _write(tmp_path, "secret", "secret words")
        _chunk(tmp_path)
        store = chunking.vector_store_for(tmp_path)

        def during() -> None:
            _write(tmp_path, "secret", "secret words", mode="incognito")
            _write(tmp_path, "other", "other words")
            _chunk(tmp_path)

        space["embedder"].during = during
        fill.fill_batch(ConversationLog(base_dir=tmp_path), store)
        store.close()

        assert _rows(tmp_path, "SELECT DISTINCT session_key FROM chunk_ref") == [("other",)]
        assert _rows(tmp_path, "SELECT count(*) FROM chunk WHERE vector IS NOT NULL") == [(0,)]
        assert _rows(tmp_path, "SELECT text FROM pending_text") == [("other words",)]

    def test_a_remote_backend_fills_nothing(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.embeddings_ollama import build_ollama_backend

        remote = build_ollama_backend(
            {
                "embedding_provider": "ollama_http",
                "ollama_url": "https://ollama.example.com",
                "ollama_allow_remote": True,
                "ollama_model": "nomic-embed-text",
            }
        )
        sent: list[list[str]] = []
        monkeypatch.setattr(remote, "embed_batch", lambda texts, **_: sent.append(texts))
        space["embedder"] = remote
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: True)
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)

        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))
        assert step.step() == fill.FILL_IDLE_PAUSE_SECS
        assert fill.fill_batch(step._log, step._store) == {"filled": 0, "pending": 1}
        step.close()
        assert sent == []


def test_only_the_in_process_model_embeds_on_this_host() -> None:
    from kiro_crew.embeddings_llama import LlamaCppEmbedder
    from kiro_crew.embeddings_ollama import build_ollama_backend

    local = LlamaCppEmbedder.__new__(LlamaCppEmbedder)
    remote = build_ollama_backend({"embedding_provider": "ollama_http", "ollama_model": "m"})

    assert fill.embeds_on_this_host(local) is True
    assert fill.embeds_on_this_host(remote) is False
    assert fill.embeds_on_this_host(object()) is False


class TestCoverage:
    def test_private_sessions_never_move_the_counts(self, tmp_path: Path, space) -> None:
        """RFC section 12: N visible sessions plus any private ones count as N, at every stage."""
        visible = Path(tempfile.mkdtemp(dir=tmp_path))
        mixed = Path(tempfile.mkdtemp(dir=tmp_path))
        for base in (visible, mixed):
            _write(base, "a", "alpha words")
            _write(base, "b", "beta words")
        for index, mode in enumerate(["incognito", "temporary", "incognito"]):
            _write(mixed, f"p{index}", "private words", mode=mode)

        stages = []
        for step in (lambda base: None, _chunk, _drain):
            for base in (visible, mixed):
                step(base)
            stages.append((_coverage(visible), _coverage(mixed)))

        assert all(only == both for only, both in stages)
        assert stages[-1][0] == {"embedded": 2, "window": 2, "pending_chunks": 0}

    def test_vectors_from_an_old_space_do_not_count_as_embedded(
        self, tmp_path: Path, space
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        _drain(tmp_path)

        space["signature"] = "space-2"

        assert _coverage(tmp_path)["embedded"] == 0

    def test_the_recall_fixture_reaches_full_coverage(self, tmp_path: Path, space) -> None:
        golden = session_recall.SessionGoldenSet.from_json(
            session_recall.default_session_golden_set_path()
        )
        session_recall._write_sessions(tmp_path, golden.sessions, now=time.time())

        _chunk(tmp_path)
        _drain(tmp_path)

        counts = _coverage(tmp_path)
        assert counts["embedded"] == counts["window"] == len(golden.sessions)
        assert counts["pending_chunks"] == 0


class TestVectorFill:
    def test_with_no_store_the_fill_creates_none(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: True)

        assert (
            fill.VectorFill(ConversationLog(base_dir=tmp_path)).step() == fill.FILL_IDLE_PAUSE_SECS
        )
        assert not (tmp_path / ".index" / "session_vectors.db").exists()

    def test_an_owed_erasure_is_retried_even_with_the_flag_off(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))
        step._store.erasure_owed = True
        settled: list[bool] = []
        monkeypatch.setattr(step._store, "settle", lambda: settled.append(True) or True)
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: False)

        assert step.step() == fill.FILL_IDLE_PAUSE_SECS

        assert settled == [True]

    def test_an_owed_erasure_is_retried_on_a_remote_backend(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))
        step._store.erasure_owed = True
        settled: list[bool] = []
        monkeypatch.setattr(step._store, "settle", lambda: settled.append(True) or True)
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(fill, "embeds_on_this_host", lambda embedder: False)

        assert step.step() == fill.FILL_IDLE_PAUSE_SECS

        assert settled == [True]

    def test_nothing_owed_means_no_settle_call(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))
        settled: list[bool] = []
        monkeypatch.setattr(step._store, "settle", lambda: settled.append(True) or True)
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: False)

        step.step()

        assert settled == []

    def test_the_flag_off_fills_nothing(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: False)

        assert (
            fill.VectorFill(ConversationLog(base_dir=tmp_path)).step() == fill.FILL_IDLE_PAUSE_SECS
        )
        assert space["embedder"].calls == []

    def test_the_pace_follows_what_the_step_did(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(fill, "FILL_BATCH", 1)
        _write(tmp_path, "s", "one", "two", "three")
        _chunk(tmp_path)
        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))

        space["embedder"].result = "none"
        assert step.step() == fill.FILL_WAIT_PAUSE_SECS
        space["embedder"].result = "vectors"
        assert step.step() == fill.FILL_BUSY_PAUSE_SECS
        step.step()
        step.step()
        assert step.step() == fill.FILL_IDLE_PAUSE_SECS

    def test_stale_vectors_are_released_once_per_space(
        self, tmp_path: Path, space, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fill, "configured_semantic_search", lambda: True)
        _write(tmp_path, "s", "some words")
        _chunk(tmp_path)
        step = fill.VectorFill(ConversationLog(base_dir=tmp_path))
        calls: list[str] = []
        real = step._store.release_stale
        monkeypatch.setattr(
            step._store, "release_stale", lambda sig: calls.append(sig) or real(sig)
        )

        step.step()
        step.step()
        space["signature"] = "space-2"
        step.step()

        assert calls == ["space-1", "space-2"]


@pytest.mark.asyncio
async def test_the_gateway_fills_while_it_supervises_the_index_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import history_index_worker
    from kiro_crew.dashboard import server as srv

    stepped = threading.Event()
    closed: list[bool] = []

    class _Fill:
        def __init__(self, log: ConversationLog) -> None:
            pass

        def step(self) -> float:
            stepped.set()
            return 0.0

        def close(self) -> None:
            closed.append(True)

    class _Supervisor:
        def __init__(self, sessions_dir: object) -> None:
            self.gave_up = False

        def start(self) -> bool:
            return True

        def poll(self) -> float:
            self.gave_up = stepped.wait(10)
            return 0.0

        def request_stop(self) -> None:
            pass

        def reap(self) -> None:
            pass

    monkeypatch.setattr(fill, "VectorFill", _Fill)
    monkeypatch.setattr(history_index_worker, "SessionIndexWorkerSupervisor", _Supervisor)
    monkeypatch.setattr(srv, "shutdown_event", threading.Event())
    state = SimpleNamespace(
        conversation_log=ConversationLog(base_dir=tmp_path), _background_tasks=set()
    )

    srv._kick_session_search_index(state)
    await asyncio.wait_for(asyncio.gather(*list(state._background_tasks)), 30)
    await async_wait_until(lambda: closed == [True], describe=lambda: "fill was not stopped")

    assert stepped.is_set()
