"""Tests for the session chunk pass, its deletion path, and the worker's hook.

Sessions are real transcript files read through ``ConversationLog``; the
tokenizer is a word counter, so no model is needed. Covers the privacy rule (an
incognito or temporary session is never chunked and loses rows it had), the quiet
window, the search window, resuming after a restart, ``delete_session`` failing
closed on the chunk store, and the worker pass staying off with the flag off
while it still prunes a store that an earlier flag-on pass filled.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from kiro_crew import history, history_index_worker, history_vectors
from kiro_crew import history_vectors_index as chunking
from kiro_crew.history import ConversationLog

_TOKENIZER = chunking.Tokenizer(lambda text: len(text.split()), "words-1")


def _write(base: Path, key: str, *texts: str, mode: str = "persistent", age: float = 3600) -> None:
    lines = [json.dumps({"_type": "metadata", "title": f"session {key}", "memory_mode": mode})]
    lines += [json.dumps({"role": "user", "content": text}) for text in texts]
    path = base / f"{key}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def _rows(base: Path, sql: str) -> list[tuple]:
    db = sqlite3.connect(base / ".index" / "session_vectors.db")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def _keys(base: Path) -> set[str]:
    return {row[0] for row in _rows(base, "SELECT DISTINCT session_key FROM chunk_ref")}


def _pass(base: Path) -> dict[str, int]:
    log = ConversationLog(base_dir=base)
    store = chunking.vector_store_for(base)
    try:
        return chunking.backfill_chunks(log, store, _TOKENIZER)
    finally:
        store.close()


class TestChunkPass:
    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_a_private_session_is_never_chunked(self, tmp_path: Path, mode: str) -> None:
        _write(tmp_path, "open", "plain words")
        _write(tmp_path, "private", "secret words", mode=mode)

        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"open"}
        assert report["remaining"] == 0
        assert _rows(tmp_path, "SELECT text FROM pending_text") == [("plain words",)]

    def test_a_session_turned_private_loses_its_rows(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", "words once open")
        _pass(tmp_path)
        assert _keys(tmp_path) == {"s"}

        _write(tmp_path, "s", "words once open", mode="incognito")
        _pass(tmp_path)

        assert _keys(tmp_path) == set()
        assert _rows(tmp_path, "SELECT count(*) FROM pending_text") == [(0,)]

    def test_a_session_turned_private_loses_its_rows_while_still_being_written(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "s", "words once open")
        _pass(tmp_path)

        _write(tmp_path, "s", "words once open", mode="temporary", age=0)
        _pass(tmp_path)

        assert _keys(tmp_path) == set()

    def test_unreadable_privacy_drops_the_rows_and_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.history_derivation import TranscriptBusy

        _write(tmp_path, "s", "some words")
        _pass(tmp_path)

        def busy(self: ConversationLog, key: str) -> None:
            raise TranscriptBusy(key)

        _write(tmp_path, "s", "some words changed")
        monkeypatch.setattr(ConversationLog, "_withhold_if_restricted", busy)
        report = _pass(tmp_path)

        assert _keys(tmp_path) == set()
        assert report["remaining"] == 1

    @pytest.mark.parametrize(
        "first_line", [b"{not json at all\n", b"\xff\xfe broken bytes\n"], ids=["json", "utf8"]
    )
    def test_a_damaged_first_line_drops_the_rows_without_holding_the_pace(
        self, tmp_path: Path, first_line: bytes
    ) -> None:
        """No retry reads a damaged line, so the session is not work the worker waits on."""
        _write(tmp_path, "s", "some words")
        _write(tmp_path, "ok", "other words")
        _pass(tmp_path)
        assert _keys(tmp_path) == {"s", "ok"}

        path = tmp_path / "s.jsonl"
        body = path.read_bytes().split(b"\n", 1)[1]
        path.write_bytes(first_line + body)
        stamp = time.time() - 3600
        os.utime(path, (stamp, stamp))
        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"ok"}
        assert report["remaining"] == 0
        assert _pass(tmp_path)["remaining"] == 0

    @pytest.mark.parametrize(
        "first_line", [b"{not json at all\n", b"\xff\xfe broken bytes\n"], ids=["json", "utf8"]
    )
    def test_a_damaged_first_line_is_pruned_with_no_tokenizer(
        self, tmp_path: Path, first_line: bytes
    ) -> None:
        """The listing reads the damaged line as persistent; the prune still drops it."""
        _write(tmp_path, "s", "secret words")
        _write(tmp_path, "ok", "other words")
        _pass(tmp_path)

        path = tmp_path / "s.jsonl"
        path.write_bytes(first_line + path.read_bytes().split(b"\n", 1)[1])
        log = ConversationLog(base_dir=tmp_path)
        store = chunking.vector_store_for(tmp_path)
        try:
            report = chunking.backfill_chunks(log, store, None)
        finally:
            store.close()

        assert report["dropped"] == 1
        assert _keys(tmp_path) == {"ok"}
        assert ("secret words",) not in _rows(tmp_path, "SELECT text FROM pending_text")

    def test_one_failing_session_does_not_stop_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "bad", "bad words", age=7200)
        _write(tmp_path, "good", "good words", age=3600)
        real = chunking.chunk_session

        def flaky(log, store, key, tokenizer):
            if key == "bad":
                raise RuntimeError("cannot chunk")
            return real(log, store, key, tokenizer)

        monkeypatch.setattr(chunking, "chunk_session", flaky)
        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"good"}
        assert (report["remaining"], report["deferred"]) == (0, 1)

    def test_a_lone_surrogate_in_a_transcript_is_chunked(self, tmp_path: Path) -> None:
        line = '{"role": "user", "content": "broken \\ud83d emoji"}'
        path = tmp_path / "s.jsonl"
        path.write_text('{"_type": "metadata", "title": "t"}\n' + line + "\n")
        stamp = time.time() - 3600
        os.utime(path, (stamp, stamp))

        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"s"}
        assert report["remaining"] == 0

    def test_an_owed_erasure_keeps_the_pass_busy_until_settled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "some words")
        log = ConversationLog(base_dir=tmp_path)
        store = chunking.vector_store_for(tmp_path)
        chunking.backfill_chunks(log, store, _TOKENIZER)
        _write(tmp_path, "s", "some words", mode="incognito")
        monkeypatch.setattr(history_vectors, "wal_truncated", lambda conn: False)

        assert chunking.backfill_chunks(log, store, _TOKENIZER)["remaining"] == 1

        monkeypatch.setattr(history_vectors, "wal_truncated", lambda conn: True)
        assert chunking.backfill_chunks(log, store, _TOKENIZER)["remaining"] == 0
        store.close()

    def test_a_session_still_being_written_is_deferred(self, tmp_path: Path) -> None:
        _write(tmp_path, "quiet", "settled words")
        _write(tmp_path, "busy", "words in flight", age=0)

        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"quiet"}
        assert (report["deferred"], report["remaining"]) == (1, 0)

    def test_a_session_leaving_the_window_leaves_the_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "old", "older words", age=7200)
        _write(tmp_path, "new", "newer words", age=3600)
        _pass(tmp_path)
        assert _keys(tmp_path) == {"old", "new"}

        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 1)
        report = _pass(tmp_path)

        assert _keys(tmp_path) == {"new"}
        assert report["dropped"] == 1

    def test_a_restart_resumes_and_keeps_what_was_built(self, tmp_path: Path) -> None:
        _write(tmp_path, "first", "first words")
        _write(tmp_path, "second", "second words", age=0)
        _pass(tmp_path)
        built = _rows(tmp_path, "SELECT chunk_id FROM chunk_ref WHERE session_key='first'")
        pending_before = _rows(tmp_path, "SELECT count(*) FROM pending_text")[0][0]

        _write(tmp_path, "second", "second words")
        report = _pass(tmp_path)

        assert report["indexed"] == 1
        assert _rows(tmp_path, "SELECT chunk_id FROM chunk_ref WHERE session_key='first'") == built
        assert _rows(tmp_path, "SELECT count(*) FROM pending_text")[0][0] == pending_before + 1

    def test_a_tokenizer_change_rechunks_every_session(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", "some words")
        _pass(tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        store = chunking.vector_store_for(tmp_path)

        other = chunking.Tokenizer(_TOKENIZER.count_tokens, "words-2")
        report = chunking.backfill_chunks(log, store, other)

        assert report["indexed"] == 1
        store.close()


class TestDelete:
    def test_a_host_with_the_flag_off_gets_no_store_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", "words")

        assert ConversationLog(base_dir=tmp_path).delete_session("s") is True
        assert not (tmp_path / ".index" / "session_vectors.db").exists()

    def test_delete_removes_the_session_chunks(self, tmp_path: Path) -> None:
        _write(tmp_path, "keep", "kept words")
        _write(tmp_path, "gone", "zqxjvbnm words")
        _pass(tmp_path)

        assert ConversationLog(base_dir=tmp_path).delete_session("gone") is True

        assert _keys(tmp_path) == {"keep"}
        assert ("zqxjvbnm words",) not in _rows(tmp_path, "SELECT text FROM pending_text")

    def test_delete_is_refused_when_the_store_cannot_be_opened(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", "words")
        (tmp_path / ".index" / "session_vectors.db").mkdir(parents=True)

        assert ConversationLog(base_dir=tmp_path).delete_session("s") is False
        assert (tmp_path / "s.jsonl").exists()


class TestWorkerPass:
    def test_the_flag_off_leaves_the_report_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "words")
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: False)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: _TOKENIZER.signature)
        monkeypatch.setattr(chunking, "load_tokenizer", lambda: _TOKENIZER)
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)

        assert step.run({"remaining": 2}) == {"remaining": 2}
        assert not (tmp_path / ".index" / "session_vectors.db").exists()

    def test_no_model_means_no_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write(tmp_path, "s", "words")
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: "")
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)

        assert step.run({"remaining": 0}) == {"remaining": 0}
        assert not (tmp_path / ".index" / "session_vectors.db").exists()

    @pytest.mark.parametrize(
        ("enabled", "signature"),
        [(False, _TOKENIZER.signature), (True, "")],
        ids=["flag-off", "no-model"],
    )
    def test_a_filled_store_still_drops_private_and_departed_sessions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, signature: str
    ) -> None:
        _write(tmp_path, "kept", "kept words")
        _write(tmp_path, "secret", "secret words here")
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: _TOKENIZER.signature)
        monkeypatch.setattr(chunking, "load_tokenizer", lambda: _TOKENIZER)
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)
        step.run({"remaining": 0})
        assert _keys(tmp_path) == {"kept", "secret"}

        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: enabled)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: signature)
        _write(tmp_path, "secret", "secret words here", mode="incognito")
        (tmp_path / "kept.jsonl").unlink()
        _write(tmp_path, "new", "new words")

        assert step.run({"remaining": 0}) == {"remaining": 0}
        assert _keys(tmp_path) == set()
        assert _rows(tmp_path, "SELECT text FROM pending_text") == []
        step.close()

    def test_the_flag_on_chunks_and_adds_its_remaining(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "s", "words")
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: _TOKENIZER.signature)
        monkeypatch.setattr(chunking, "load_tokenizer", lambda: _TOKENIZER)
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)

        assert step.run({"remaining": 1, "indexed": 3}) == {"remaining": 1, "indexed": 3}
        assert _keys(tmp_path) == {"s"}
        step.close()

    def test_a_failed_tokenizer_load_is_not_retried_for_the_same_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: "model-1")
        monkeypatch.setattr(chunking, "load_tokenizer", lambda: calls.append(1))
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)

        step.run({"remaining": 0})
        step.run({"remaining": 0})
        assert len(calls) == 1

        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: "model-2")
        step.run({"remaining": 0})
        assert len(calls) == 2

    def test_a_failing_pass_leaves_the_keyword_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chunking, "configured_semantic_search", lambda: True)
        monkeypatch.setattr(chunking, "tokenizer_signature", lambda: "x")

        def boom() -> None:
            raise RuntimeError("tokenizer exploded")

        monkeypatch.setattr(chunking, "load_tokenizer", boom)
        step = history_index_worker._ChunkPass(ConversationLog(base_dir=tmp_path), 5.0)

        assert step.run({"remaining": 4}) == {"remaining": 4}


class TestModelGate:
    """The tokenizer is opened by native code, so it gets the embedder's path gates."""

    @staticmethod
    def _model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec=None) -> Path:
        from kiro_crew import embeddings

        path = tmp_path / "model.gguf"
        path.write_bytes(b"x" * 64)
        monkeypatch.setattr(embeddings, "resolve_custom_model", lambda: spec)
        monkeypatch.setattr(embeddings, "default_model_path", lambda: path)
        monkeypatch.setattr(embeddings, "model_file_present", lambda p: p.exists())
        monkeypatch.setattr(embeddings, "_is_sensitive_model_path", lambda p: False)
        return path

    @staticmethod
    def _llama(monkeypatch: pytest.MonkeyPatch, opened: list[str]) -> None:
        from kiro_crew import embeddings

        class _Llama:
            def __init__(self, *, model_path: str, vocab_only: bool, verbose: bool) -> None:
                assert vocab_only is True
                opened.append(model_path)

            def tokenize(self, data: bytes, *, add_bos: bool, special: bool) -> list[int]:
                return list(range(len(data.split())))

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: _Llama)

    def test_the_bundled_model_gives_a_tokenizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = self._model(tmp_path, monkeypatch)
        opened: list[str] = []
        self._llama(monkeypatch, opened)

        tokenizer = chunking.load_tokenizer()

        assert tokenizer is not None
        assert tokenizer.signature == chunking.tokenizer_signature()
        assert tokenizer.signature.startswith("model.gguf:64:")
        assert tokenizer.count_tokens("three short words") == 3
        assert opened == [str(path)]

    def test_a_rejected_custom_path_is_never_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.embeddings_model import CustomModelSpec

        protected = tmp_path / "credentials.gguf"
        protected.write_bytes(b"x" * 64)
        spec = CustomModelSpec(protected, "custom", 1024, "protected", "model_path_protected")
        self._model(tmp_path, monkeypatch, spec)
        opened: list[str] = []
        self._llama(monkeypatch, opened)

        assert chunking.tokenizer_signature() == ""
        assert chunking.load_tokenizer() is None
        assert opened == []

    def test_a_protected_path_is_never_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import embeddings

        self._model(tmp_path, monkeypatch)
        monkeypatch.setattr(embeddings, "_is_sensitive_model_path", lambda p: True)
        opened: list[str] = []
        self._llama(monkeypatch, opened)

        assert chunking.tokenizer_signature() == ""
        assert chunking.load_tokenizer() is None
        assert opened == []

    def test_an_aliased_model_file_is_never_opened(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The embedder refuses a second name on the bundled weights; so does the tokenizer."""
        from kiro_crew import embeddings

        self._model(tmp_path, monkeypatch)
        monkeypatch.setattr(embeddings, "_model_file_stamp_unaliased", lambda path: None)
        opened: list[str] = []
        self._llama(monkeypatch, opened)

        assert chunking.load_tokenizer() is None
        assert opened == []

    def test_a_model_file_replaced_during_the_load_gives_no_tokenizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import embeddings

        path = self._model(tmp_path, monkeypatch)

        class _Swapping:
            def __init__(self, **_: object) -> None:
                path.write_bytes(b"y" * 128)

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: _Swapping)

        assert chunking.load_tokenizer() is None

    def test_a_failing_load_gives_no_tokenizer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import embeddings

        self._model(tmp_path, monkeypatch)

        def broken(**_: object) -> None:
            raise RuntimeError("bad gguf")

        monkeypatch.setattr(embeddings, "_load_llama_class", lambda: broken)

        assert chunking.load_tokenizer() is None

    def test_no_model_file_gives_no_signature(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._model(tmp_path, monkeypatch).unlink()

        assert chunking.tokenizer_signature() == ""


class TestConfiguredFlag:
    def test_the_live_config_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from kiro_crew.config import live

        cfg = SimpleNamespace(session=SimpleNamespace(semantic_search=True))
        monkeypatch.setattr(live, "snapshot", lambda: cfg)
        assert chunking.configured_semantic_search() is True

        cfg.session.semantic_search = "yes"
        assert chunking.configured_semantic_search() is False

    def test_an_unreadable_config_reads_as_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.config import live, loader

        def broken() -> None:
            raise OSError("no config")

        monkeypatch.setattr(live, "snapshot", lambda: None)
        monkeypatch.setattr(loader.KiroCrewConfig, "load", staticmethod(broken))
        assert chunking.configured_semantic_search() is False
