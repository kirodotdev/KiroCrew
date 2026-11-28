"""An idle embedding model rests: its weight pages leave the process, the model stays loaded.

A wake (a person showing up, or the next embed) maps the pages back and the
vectors are unchanged. The full unload after the idle-evict timer still
follows. The in-process model and the embedding child both rest.
"""

from __future__ import annotations

import asyncio
import io
import json
import mmap
import os
import sys
from pathlib import Path

import embed_child_fake
import embeddings_helpers
import pytest
from embed_child_fake import MappedLlama, resident_kib
from embeddings_helpers import _make_fake_llama_class, _write_model_file

import kiro_crew.embeddings as embeddings_mod
import kiro_crew.embeddings_child as child_mod
import kiro_crew.embeddings_shared as embeddings_shared
from kiro_crew.embeddings import LlamaCppEmbedder
from kiro_crew.embeddings_child import ChildLlama
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until


def rest_if_idle(emb: LlamaCppEmbedder, idle_secs: float) -> bool:
    from kiro_crew.embeddings_rest import rest_if_idle as rest

    return rest(emb, idle_secs)


_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons

_TEST_DIR = str(Path(__file__).resolve().parent)
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads /proc/self/smaps and uses madvise"
)


@linux_only
class TestWeightPages:
    def test_dropped_pages_leave_the_process_and_map_back_the_same(
        self, tmp_path: Path, seeded_rng
    ):
        from kiro_crew.embeddings_rest import drop_weight_pages, map_weight_pages

        path = tmp_path / "weights.gguf"
        data = seeded_rng.randbytes(4 << 20)
        path.write_bytes(data)
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ) as view:
            assert view[:] == data
            assert resident_kib(str(path)) >= 4096
            assert drop_weight_pages(path) is True
            assert resident_kib(str(path)) == 0
            if map_weight_pages(path):
                assert resident_kib(str(path)) >= 4096
            assert view[:] == data
            assert resident_kib(str(path)) >= 4096

    def test_a_writable_mapping_is_left_alone(self, tmp_path: Path):
        from kiro_crew.embeddings_rest import drop_weight_pages

        path = tmp_path / "rw.gguf"
        path.write_bytes(b"\0" * (1 << 20))
        with open(path, "r+b") as f, mmap.mmap(f.fileno(), 0) as view:
            view[:4] = b"edit"
            assert drop_weight_pages(path) is False
            assert view[:4] == b"edit"


def _mapped_embedder(tmp_path: Path, monkeypatch) -> LlamaCppEmbedder:
    MappedLlama.instances = []
    monkeypatch.setattr(embeddings_mod, "_load_llama_class", lambda: MappedLlama)
    emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), dim=16)
    assert emb.embed("warm") is not None
    assert emb.wait_ready(timeout=FALLBACK_TIMEOUT_SECS)
    return emb


@linux_only
class TestIdleRest:
    def test_an_idle_model_gives_back_its_pages_and_stays_loaded(self, tmp_path: Path, monkeypatch):
        emb = _mapped_embedder(tmp_path, monkeypatch)
        try:
            before = emb.embed("same text")
            path = str(emb.model_path)
            assert resident_kib(path) > 0
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is True
            assert resident_kib(path) == 0
            assert emb.is_ready() and len(MappedLlama.instances) == 1  # nothing to reload
            assert emb.embed("same text") == before
        finally:
            emb.close()

    def test_a_wake_maps_the_pages_back_before_any_embed(self, tmp_path: Path, monkeypatch):
        from kiro_crew.embeddings_rest import map_weight_pages

        emb = _mapped_embedder(tmp_path, monkeypatch)
        try:
            before = emb.embed("same text")
            path = str(emb.model_path)
            populated = map_weight_pages(path)
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is True
            emb.wake()
            assert not emb._rested
            if populated:
                wait_until(lambda: resident_kib(path) > 0, timeout=FALLBACK_TIMEOUT_SECS)
            assert emb.embed("same text") == before
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is True  # rests again once idle
        finally:
            emb.close()

    def test_a_recently_used_or_busy_model_does_not_rest(self, tmp_path: Path, monkeypatch):
        emb = _mapped_embedder(tmp_path, monkeypatch)
        try:
            assert rest_if_idle(emb, 120) is False
            emb._last_used -= 200
            with emb._lock:
                assert rest_if_idle(emb, 120) is False
            emb._inflight += 1
            assert rest_if_idle(emb, 120) is False
            emb._inflight -= 1
            assert resident_kib(str(emb.model_path)) > 0
        finally:
            emb.close()

    def test_the_evictor_rests_the_model_on_its_own(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            embeddings_mod, "_IDLE_REST_SECS", 0.1  # flake-ok: the threshold under test
        )
        emb = _mapped_embedder(tmp_path, monkeypatch)
        try:
            wait_until(lambda: emb._rested, timeout=FALLBACK_TIMEOUT_SECS)
            assert resident_kib(str(emb.model_path)) == 0
            assert emb.is_ready()
        finally:
            emb.close()

    def test_the_full_unload_still_follows_a_rest(self, tmp_path: Path, monkeypatch):
        emb = _mapped_embedder(tmp_path, monkeypatch)
        try:
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is True
            assert emb.evict_if_idle(150) is True
            assert not emb.is_ready()
            assert emb.embed("again") is not None
            assert len(MappedLlama.instances) == 2
        finally:
            emb.close()


class TestNothingToGiveBack:
    def test_a_model_that_maps_no_file_never_rests(self, tmp_path: Path, monkeypatch):
        fake_cls = _make_fake_llama_class(dim=16)
        monkeypatch.setattr(embeddings_mod, "_load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), dim=16)
        try:
            assert emb.embed("warm") is not None
            assert emb.wait_ready(timeout=FALLBACK_TIMEOUT_SECS)
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is False
            assert emb._rest_unsupported
            assert emb.embed("again") is not None
        finally:
            emb.close()


@linux_only
class TestChildRest:
    @staticmethod
    def _lines(*msgs: object) -> io.BytesIO:
        return io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))

    def test_the_child_gives_back_its_pages_and_maps_them_on_wake(self, tmp_path: Path):
        model = _write_model_file(tmp_path / "model.gguf")
        load = {
            "op": "load",
            "kwargs": {"model_path": str(model)},
            "stamp": None,
            "factory": "embed_child_fake:load_mapped_class",
            "nice": 0,
        }
        stdin = self._lines(
            load,
            {"id": 1, "op": "embed", "texts": ["a"]},
            {"op": "rest"},
            {"id": 2, "op": "embed", "texts": ["__rss"]},
            {"op": "wake"},
            {"id": 3, "op": "embed", "texts": ["a"]},
            {"op": "exit"},
        )
        out = io.BytesIO()
        assert child_mod._serve(stdin, out) == 0
        _ready, first, rss, again = (json.loads(line) for line in out.getvalue().splitlines())
        assert rss["vectors"][0][0] == 0.0
        assert again["vectors"] == first["vectors"]

    def test_an_embedder_rests_its_child(self, tmp_path: Path, monkeypatch):
        existing = os.environ.get("PYTHONPATH")
        monkeypatch.setenv("PYTHONPATH", _TEST_DIR + (os.pathsep + existing if existing else ""))
        monkeypatch.setattr(child_mod, "_CHILD_LLAMA_FACTORY", "embed_child_fake:load_mapped_class")
        monkeypatch.setattr(embeddings_mod, "_load_llama_class", lambda: embed_child_fake.FakeLlama)
        monkeypatch.setattr(embeddings_mod, "is_vendored_llama_class", lambda cls: True)
        monkeypatch.setattr(embeddings_mod, "embedding_child_process_enabled", lambda: True)
        emb = LlamaCppEmbedder(model_path=_write_model_file(tmp_path / "model.gguf"), dim=16)
        try:
            assert emb.wait_ready(timeout=FALLBACK_TIMEOUT_SECS)
            assert isinstance(emb._llm, ChildLlama)
            before = emb.embed("same text")
            assert emb.embed("__rss")[0] > 0
            emb._last_used -= 200
            assert rest_if_idle(emb, 120) is True
            assert emb.embed("same text") == before
        finally:
            emb.retire()


class _Backend:
    def __init__(self) -> None:
        self.warmed = 0
        self.woken = 0

    def keep_warm(self) -> None:
        self.warmed += 1

    def wake(self) -> None:
        self.woken += 1

    def close(self) -> None:
        pass


class TestWakeForPerson:
    def test_a_larger_machine_wakes_a_resting_model(self, monkeypatch):
        backend = _Backend()
        monkeypatch.setattr(embeddings_shared, "_shared_embedder", backend)
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: False)
        embeddings_shared.warm_embedder_for_person()
        assert (backend.warmed, backend.woken) == (0, 1)

    def test_the_wake_runs_on_the_loop_without_waiting(self, monkeypatch):
        backend = _Backend()
        monkeypatch.setattr(embeddings_shared, "_shared_embedder", backend)
        monkeypatch.setattr(embeddings_mod, "small_memory_host", lambda: False)

        async def _send() -> None:
            embeddings_shared.warm_embedder_for_person_soon()

        asyncio.run(_send())
        assert backend.woken == 1
