"""The Ollama server embedding backend, its settings, and how the factory selects it.

No test opens a socket: every request goes through a fake opener that records it,
and readiness probes run through an injected starter instead of a thread.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import pytest

import kiro_crew.embeddings as emb
from kiro_crew import embeddings_download, embeddings_ollama
from kiro_crew.config.memory_sections import _coerce_embedding_provider
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.embeddings_ollama import (
    OllamaHttpEmbedder,
    OllamaSettings,
    _build_opener,
    _RefuseRedirects,
    resolve_ollama_settings,
)
from kiro_crew.embeddings_settings import _MAX_EMBED_CHARS, EmbeddingWork, embedding_work

_DIM = 4
_MODEL = "nomic-embed-text"


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeOpener:
    """Answers ``/api/embed`` with one vector per input and ``/api/tags`` with *tags*."""

    def __init__(
        self,
        *,
        tags: "list[str] | None" = None,
        embed: "Callable[[dict], Any] | None" = None,
        error: "Exception | None" = None,
    ) -> None:
        self.tags = [_MODEL] if tags is None else tags
        self.embed = embed or (lambda body: {"embeddings": [[0.5] * _DIM for _ in body["input"]]})
        self.error = error
        self.requests: list[tuple[str, dict | None, float]] = []

    def open(self, request: urllib.request.Request, timeout: float) -> _FakeResponse:
        url = request.full_url
        body = json.loads(request.data) if request.data else None
        self.requests.append((url, body, timeout))
        if self.error is not None:
            raise self.error
        if url.endswith("/api/tags"):
            payload: Any = {"models": [{"name": name} for name in self.tags]}
        else:
            payload = self.embed(body or {})
        return _FakeResponse(json.dumps(payload).encode())

    @property
    def embed_requests(self) -> list[dict]:
        return [body for url, body, _ in self.requests if url.endswith("/api/embed") and body]


class _CapturedProbes:
    """A probe starter that records each probe instead of running it on a thread."""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None]) -> None:
        self.pending.append(target)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def _settings(**overrides: Any) -> OllamaSettings:
    return resolve_ollama_settings({"ollama_model": _MODEL, "embedding_dim": _DIM, **overrides})


def _embedder(
    opener: _FakeOpener, probes: "_CapturedProbes | None" = None, **overrides: Any
) -> OllamaHttpEmbedder:
    return OllamaHttpEmbedder(
        _settings(**overrides), opener=opener, start_probe=probes or _CapturedProbes()
    )


# -- settings -----------------------------------------------------------------


class TestSettings:
    def test_defaults_need_only_a_model(self) -> None:
        settings = resolve_ollama_settings({})
        assert settings.url == "http://127.0.0.1:11434"
        assert settings.error == "memory.ollama_model is empty"
        assert resolve_ollama_settings({"ollama_model": _MODEL}).error == ""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:11434",
            "http://localhost:11434/",
            "http://[::1]:8080",
            "http://127.1.2.3",
        ],
    )
    def test_a_loopback_url_needs_no_opt_in(self, url: str) -> None:
        settings = _settings(ollama_url=url)
        assert settings.error == ""
        assert not settings.url.endswith("/")

    def test_a_path_prefix_is_kept_for_a_proxy(self) -> None:
        assert _settings(ollama_url="http://127.0.0.1:9000/ollama/").url == (
            "http://127.0.0.1:9000/ollama"
        )

    def test_a_remote_host_is_refused_without_the_opt_in(self) -> None:
        assert "ollama_allow_remote" in _settings(ollama_url="https://gpu.example:11434").error

    def test_a_remote_host_must_use_https_even_with_the_opt_in(self) -> None:
        settings = _settings(ollama_url="http://10.0.0.5:11434", ollama_allow_remote=True)
        assert "https://" in settings.error

    def test_a_remote_https_host_is_accepted_with_the_opt_in(self) -> None:
        settings = _settings(ollama_url="https://gpu.example:11434", ollama_allow_remote=True)
        assert settings.error == ""

    def test_only_a_real_true_opts_in(self) -> None:
        assert _settings(ollama_url="https://gpu.example", ollama_allow_remote="true").error

    @pytest.mark.parametrize(
        ("url", "fragment"),
        [
            ("ftp://127.0.0.1:11434", "http://"),
            ("http://user:pw@127.0.0.1:11434", "credentials"),
            ("http://127.0.0.1:11434?x=1", "query"),
            ("http://127.0.0.1:notaport", "not a valid URL"),
            ("http://[::1", "not a valid URL"),
            ("http://", "no host"),
        ],
    )
    def test_an_unusable_url_is_refused(self, url: str, fragment: str) -> None:
        assert fragment in _settings(ollama_url=url).error

    def test_an_unparsable_url_builds_a_backend_that_sends_nothing(self) -> None:
        embedder = OllamaHttpEmbedder(_settings(ollama_url="http://[::1"))
        assert embedder.is_ready() is False
        assert embedder.embed("secret") is None

    @pytest.mark.parametrize("raw", [0, -1, True, "768"])
    def test_an_unreadable_width_is_the_default(self, raw: object) -> None:
        assert resolve_ollama_settings({"ollama_model": _MODEL, "embedding_dim": raw}).dim == 1024


class TestConfig:
    def test_the_loader_reads_the_ollama_keys(self) -> None:
        cfg = _build_memory_config(
            {
                "embedding_provider": "ollama_http",
                "ollama_url": "http://127.0.0.1:9000",
                "ollama_model": _MODEL,
                "ollama_allow_remote": "yes",
            }
        )
        assert cfg.embedding_provider == "ollama_http"
        assert cfg.ollama_url == "http://127.0.0.1:9000"
        assert cfg.ollama_model == _MODEL
        assert cfg.ollama_allow_remote is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ollama_http", "ollama_http"),
            # The retired value never selects the server backend.
            ("ollama", "llama_cpp"),
        ],
    )
    def test_provider_coercion(self, raw: str, expected: str) -> None:
        assert _coerce_embedding_provider(raw) == expected


# -- embedding ----------------------------------------------------------------


class TestEmbed:
    def test_one_text_is_one_request_to_api_embed(self) -> None:
        opener = _FakeOpener()
        vector = _embedder(opener).embed("hello")
        assert vector == [0.5] * _DIM
        url, body, timeout = opener.requests[0]
        assert url == "http://127.0.0.1:11434/api/embed"
        assert body == {"model": _MODEL, "input": ["hello"], "truncate": True}
        assert timeout == 30.0

    def test_a_caller_deadline_shortens_the_request(self) -> None:
        opener = _FakeOpener()
        half = embeddings_ollama._REQUEST_TIMEOUT_SECS / 2
        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(time.monotonic() + half))
        try:
            assert _embedder(opener).embed("x") == [0.5] * _DIM
        finally:
            embedding_work.set(previous)
        assert 0 < opener.requests[0][2] <= half

    def test_a_spent_deadline_sends_nothing(self) -> None:
        opener = _FakeOpener()
        previous = embedding_work.get()
        embedding_work.set(EmbeddingWork(deadline=0))
        try:
            assert _embedder(opener).embed("x") is None
        finally:
            embedding_work.set(previous)
        assert opener.requests == []

    def test_a_batch_is_split_and_kept_in_order(self) -> None:
        opener = _FakeOpener(
            embed=lambda body: {"embeddings": [[float(t[1:])] * _DIM for t in body["input"]]}
        )
        texts = [f"t{i}" for i in range(20)]
        vectors = _embedder(opener).embed_batch(texts)
        assert [len(b["input"]) for b in opener.embed_requests] == [8, 8, 4]
        assert vectors == [[float(i)] * _DIM for i in range(20)]

    def test_one_failed_batch_fails_the_call(self) -> None:
        calls = []

        def embed(body: dict) -> dict:
            calls.append(body)
            if len(calls) == 2:
                return {"error": "the input length exceeds the context length"}
            return {"embeddings": [[0.5] * _DIM for _ in body["input"]]}

        assert _embedder(_FakeOpener(embed=embed)).embed_batch(["x"] * 12) is None

    def test_long_input_is_clipped_like_the_in_process_backend(self) -> None:
        opener = _FakeOpener()
        _embedder(opener).embed("a" * (_MAX_EMBED_CHARS + 50))
        assert len(opener.embed_requests[0]["input"][0]) == _MAX_EMBED_CHARS

    @pytest.mark.parametrize("texts", [[], [""], ["  ", "\n"]])
    def test_blank_input_is_none_without_a_request(self, texts: list[str]) -> None:
        opener = _FakeOpener()
        assert _embedder(opener).embed_batch(texts) is None
        assert opener.requests == []

    @pytest.mark.parametrize(
        "payload",
        [
            {"embeddings": [[0.5] * _DIM, [0.5] * _DIM]},
            {"embeddings": [[0.5, "x", 0.5, 0.5]]},
            {"embeddings": [[0.5, True, 0.5, 0.5]]},
            {"embeddings": "nope"},
            ["not", "an", "object"],
        ],
    )
    def test_a_malformed_response_is_none(self, payload: object) -> None:
        assert _embedder(_FakeOpener(embed=lambda body: payload)).embed("x") is None

    def test_a_width_mismatch_names_the_width_and_stops_the_backend(self, caplog) -> None:
        opener = _FakeOpener(embed=lambda body: {"embeddings": [[0.5] * 768]})
        embedder = _embedder(opener)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.embeddings"):
            assert embedder.embed("x") is None
            assert embedder.embed("y") is None
        errors = [r for r in caplog.records if "memory.embedding_dim" in r.getMessage()]
        assert len(errors) == 1
        assert "set memory.embedding_dim to 768" in errors[0].getMessage()
        assert embedder.is_ready() is False
        assert len(opener.embed_requests) == 1

    def test_an_oversized_response_is_none(self, monkeypatch) -> None:
        monkeypatch.setattr(embeddings_ollama, "_MAX_RESPONSE_BYTES", 16)
        assert _embedder(_FakeOpener()).embed("x") is None

    def test_an_outage_costs_one_request_until_a_probe_sees_the_server(self, caplog) -> None:
        opener = _FakeOpener(error=urllib.error.URLError("connection refused"))
        probes = _CapturedProbes()
        embedder = _embedder(opener, probes)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.embeddings"):
            assert embedder.embed("x") is None
            assert embedder.embed("y") is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "keyword search" in warnings[0].getMessage()
        assert len(opener.requests) == 1
        opener.error = None
        embedder.prefetch()
        probes.run_all()
        assert embedder.embed("z") == [0.5] * _DIM

    def test_an_unusable_config_never_sends_text(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener, ollama_url="https://gpu.example")
        assert embedder.embed("secret") is None
        assert embedder.is_ready() is False
        embedder.prefetch()
        assert opener.requests == []

    def test_a_closed_backend_embeds_nothing(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener)
        embedder.close()
        embedder.close()
        assert embedder.embed("x") is None
        assert embedder.is_ready() is False
        assert opener.requests == []


# -- identity and readiness ----------------------------------------------------


class TestIdentityAndReadiness:
    def test_the_space_is_the_model_and_width_not_the_url(self) -> None:
        local = _embedder(_FakeOpener())
        moved = _embedder(_FakeOpener(), ollama_url="http://localhost:9999")
        assert local.model_id == moved.model_id == f"ollama:{_MODEL}"
        assert local.dim == _DIM
        assert emb.embedding_space_signature(local.model_id, local.dim) == (
            emb.embedding_space_signature(moved.model_id, moved.dim)
        )

    def test_is_ready_never_blocks_and_starts_one_probe(self) -> None:
        opener, probes = _FakeOpener(), _CapturedProbes()
        embedder = _embedder(opener, probes)
        assert embedder.is_ready() is False
        assert embedder.is_ready() is False
        assert len(probes.pending) == 1
        assert opener.requests == []
        probes.run_all()
        assert embedder.is_ready() is True
        assert opener.requests[0][0] == "http://127.0.0.1:11434/api/tags"

    @pytest.mark.parametrize(("tags", "ready"), [([f"{_MODEL}:latest"], True), (["other"], False)])
    def test_the_probe_requires_the_configured_model(self, tags: list[str], ready: bool) -> None:
        probes = _CapturedProbes()
        embedder = _embedder(_FakeOpener(tags=tags), probes)
        embedder.prefetch()
        probes.run_all()
        assert embedder.is_ready() is ready

    def test_a_successful_embed_marks_ready_without_a_probe(self) -> None:
        opener, probes = _FakeOpener(), _CapturedProbes()
        embedder = _embedder(opener, probes)
        embedder.embed("x")
        assert embedder.is_ready() is True
        assert probes.pending == []

    def test_a_failed_embed_marks_not_ready(self) -> None:
        opener = _FakeOpener()
        embedder = _embedder(opener)
        embedder.embed("x")
        opener.error = urllib.error.URLError("gone")
        embedder.embed("y")
        assert embedder.is_ready() is False

    def test_wait_ready_probes_synchronously(self) -> None:
        embedder = _embedder(_FakeOpener())
        assert embedder.wait_ready() is True


# -- transport ----------------------------------------------------------------


class TestTransport:
    def test_redirects_are_refused(self) -> None:
        req = urllib.request.Request("http://127.0.0.1:11434/api/embed", data=b"{}")
        assert (
            _RefuseRedirects().redirect_request(req, io.BytesIO(), 307, "", None, "http://x")
            is None
        )

    def test_a_loopback_url_ignores_proxy_variables(self, monkeypatch) -> None:
        monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
        loopback = _build_opener("http://127.0.0.1:11434")
        remote = _build_opener("https://gpu.example")
        # An empty ProxyHandler opens no scheme, so the opener keeps none and
        # build_opener does not add the environment-reading default.
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in loopback.handlers)
        assert any(
            h.proxies.get("http") == "http://proxy.example:3128"
            for h in remote.handlers
            if isinstance(h, urllib.request.ProxyHandler)
        )


# -- selection ----------------------------------------------------------------


class TestSelection:
    @pytest.fixture(autouse=True)
    def _no_env_model(self, _floor_monkeypatch) -> None:
        _floor_monkeypatch.delenv("KIROCREW_EMBED_MODEL_PATH", raising=False)

    def test_the_provider_selects_the_server_backend(self, monkeypatch) -> None:
        memory = {"embedding_provider": "ollama_http", "ollama_model": _MODEL, "embedding_dim": 768}
        monkeypatch.setattr(emb, "_read_memory_config", lambda: memory)
        backend = emb.default_embedding_backend()
        assert isinstance(backend, OllamaHttpEmbedder)
        assert (backend.model_id, backend.dim) == (f"ollama:{_MODEL}", 768)

    def test_the_provider_wins_over_a_custom_model_path(self, monkeypatch, tmp_path) -> None:
        memory = {
            "embedding_provider": "ollama_http",
            "ollama_model": _MODEL,
            "embed_model_path": str(tmp_path / "m.gguf"),
        }
        monkeypatch.setattr(emb, "_read_memory_config", lambda: memory)
        assert isinstance(emb.default_embedding_backend(), OllamaHttpEmbedder)

    def test_an_unusable_config_still_names_the_server_space(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(
            emb, "_read_memory_config", lambda: {"embedding_provider": "ollama_http"}
        )
        with caplog.at_level(logging.ERROR, logger="kiro_crew.embeddings"):
            backend = emb.default_embedding_backend()
        assert isinstance(backend, OllamaHttpEmbedder)
        assert backend.is_ready() is False
        assert any("memory.ollama_model is empty" in r.getMessage() for r in caplog.records)

    def test_the_bundled_model_is_not_downloaded_for_the_server_backend(self, monkeypatch) -> None:
        monkeypatch.setattr(
            emb,
            "_read_memory_config",
            lambda: {"embedding_provider": "ollama_http", "ollama_model": _MODEL},
        )

        def _must_not_run() -> None:
            raise AssertionError("the download manager was consulted")

        monkeypatch.setattr(embeddings_download, "model_download_manager", _must_not_run)
        assert embeddings_download.start_background_model_download() is None

    def test_a_pending_rebuild_request_is_acknowledged_by_the_server_backend(
        self, monkeypatch
    ) -> None:
        """A store that has not yet acknowledged a Settings model apply must still be
        cleared and re-embedded once this backend is the configured one."""
        memory = {
            "embedding_provider": "ollama_http",
            "ollama_model": _MODEL,
            "embedding_dim": _DIM,
            "embed_rebuild_generation": "apply-1",
        }
        backend = _embedder(_FakeOpener())
        backend.embed("warm")
        monkeypatch.setattr(emb, "_read_memory_config", lambda: memory)
        monkeypatch.setattr(emb, "get_shared_embedder", lambda: backend)
        calls: list[dict] = []

        class _Store:
            _db_lock = threading.RLock()

            def recorded_rebuild_generation(self) -> str:
                return ""

            def set_embedding_dim(self, dim: int) -> bool:
                return False

            def reconcile_embedding_space(self, active: str, **kwargs: Any) -> int:
                calls.append({"active": active, **kwargs})
                return 3

        assert emb.align_store_embedding_space(_Store()) == 3
        assert calls == [
            {
                "active": emb.embedding_space_signature(f"ollama:{_MODEL}", _DIM),
                "clear_when_unknown": True,
                "rebuild_generation": "apply-1",
            }
        ]
