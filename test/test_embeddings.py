"""Tests for the in-process embedding runtime and model download manager.

The Ollama HTTP client / OllamaManager lifecycle was replaced by an
in-process llama.cpp runtime (``LlamaCppEmbedder``) plus a background
HTTPS model download from the CDN (``ModelDownloadManager``). These tests
never load a real model and never hit the network: the vendored Llama class
is replaced with fakes and ``urllib.request.urlopen`` is monkeypatched.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import (
    _DOWNLOAD_MAX_ATTEMPTS,
    LlamaCppEmbedder,
    ModelDownloadManager,
    _platform_libs_dirname,
    default_model_path,
    get_shared_embedder,
    make_sync_embed_fn,
    model_download_manager,
    model_file_present,
    models_dir,
    reset_download_manager,
    reset_shared_embedder,
    start_background_model_download,
)

# Captured at import so the lru_cache can always be cleared even while a test
# has monkeypatched the module attribute ``_load_llama_class``.
_REAL_LOAD_LLAMA = embeddings_mod._load_llama_class

_DIM = 1024
_MODEL_BYTES = b"g" * 1_100_000  # >1MB so model_file_present() accepts it


@pytest.fixture(autouse=True)
def _reset_embedding_singletons():
    """Isolate the shared embedder / download manager singletons per test."""
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()
    yield
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()


def _write_model_file(path: Path, payload: bytes = _MODEL_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _make_fake_llama_class(dim: int = _DIM):
    """Return a fresh fake Llama class recording constructor + embed calls."""

    class _FakeLlama:
        instances: list = []
        init_attempts: int = 0
        embed_error: Exception | None = None
        response_override: dict | None = None

        def __init__(self, **kwargs):
            type(self).init_attempts += 1
            self.kwargs = kwargs
            self.embed_calls: list[list[str]] = []
            type(self).instances.append(self)

        def create_embedding(self, texts):
            error = type(self).embed_error
            if error is not None:
                raise error
            self.embed_calls.append(list(texts))
            if type(self).response_override is not None:
                return type(self).response_override
            return {"data": [{"embedding": [0.1] * dim} for _ in texts]}

    return _FakeLlama


def _make_failing_llama_class():
    """Return a fake Llama class whose constructor always raises."""

    class _FailLlama:
        init_attempts: int = 0

        def __init__(self, **kwargs):
            type(self).init_attempts += 1
            raise RuntimeError("corrupt model file")

    return _FailLlama


# ═══════════════════════════════════════════════════════════════════════════
# _platform_libs_dirname
# ═══════════════════════════════════════════════════════════════════════════


class TestPlatformLibsDirname:
    @pytest.mark.parametrize(
        ("platform_str", "machine", "expected"),
        [
            ("linux", "x86_64", "linux_x86_64"),
            ("linux", "amd64", "linux_x86_64"),
            ("linux", "aarch64", "linux_aarch64"),
            ("linux", "arm64", "linux_aarch64"),
            ("linux", "AARCH64", "linux_aarch64"),  # machine is lowercased
            ("darwin", "arm64", "macos_arm64"),
            ("darwin", "x86_64", "macos_x86_64"),  # Intel Mac (universal DMG x64 slice)
            ("win32", "amd64", "win_amd64"),
            ("win32", "x86_64", "win_amd64"),
        ],
    )
    def test_supported_platforms(
        self, monkeypatch, platform_str: str, machine: str, expected: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", platform_str)
        monkeypatch.setattr("kiro_crew.embeddings.platform.machine", lambda: machine)
        assert _platform_libs_dirname() == expected

    @pytest.mark.parametrize(
        ("platform_str", "machine"),
        [
            ("linux", "ppc64le"),
            ("darwin", "ppc"),
            ("win32", "arm64"),
            ("sunos5", "x86_64"),
            ("aix", "aarch64"),
        ],
    )
    def test_unsupported_combos_return_none(
        self, monkeypatch, platform_str: str, machine: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", platform_str)
        monkeypatch.setattr("kiro_crew.embeddings.platform.machine", lambda: machine)
        assert _platform_libs_dirname() is None


# ═══════════════════════════════════════════════════════════════════════════
# Model paths / file presence
# ═══════════════════════════════════════════════════════════════════════════


class TestModelPaths:
    def test_models_dir_under_config_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        assert models_dir() == tmp_path / "models"
        assert default_model_path() == tmp_path / "models" / "qwen3-embedding-0.6b.gguf"

    def test_model_file_absent_is_false(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        assert model_file_present() is False

    def test_small_file_is_placeholder_not_present(self, tmp_path: Path, monkeypatch) -> None:
        """A <1MB file is a truncated/placeholder file, not a real model."""
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        _write_model_file(default_model_path(), b"placeholder, not weights\n")
        assert model_file_present() is False

    def test_large_file_is_present(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        _write_model_file(default_model_path())
        assert model_file_present() is True

    def test_explicit_path_argument(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere" / "model.gguf"
        assert model_file_present(target) is False
        _write_model_file(target)
        assert model_file_present(target) is True


# ═══════════════════════════════════════════════════════════════════════════
# LlamaCppEmbedder
# ═══════════════════════════════════════════════════════════════════════════


class TestLlamaCppEmbedder:
    def _embedder(self, tmp_path: Path) -> LlamaCppEmbedder:
        model = _write_model_file(tmp_path / "model.gguf")
        return LlamaCppEmbedder(model_path=model)

    def test_embed_returns_vector(self, tmp_path: Path, monkeypatch) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        # First embed never blocks: it kicks the background load and returns None.
        assert emb.embed("hello world") is None
        assert emb.wait_ready(timeout=5)
        vec = emb.embed("hello world")
        assert vec is not None
        assert len(vec) == _DIM
        assert emb.is_ready()

    def test_embed_returns_none_when_model_file_missing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No model file → None without ever constructing the Llama class."""
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = LlamaCppEmbedder(model_path=tmp_path / "missing.gguf")
        assert emb.embed("hello") is None
        assert fake_cls.init_attempts == 0
        assert not emb.is_ready()

    def test_embed_batch_empty_and_whitespace_return_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.embed_batch([]) is None
        assert emb.embed_batch(["", "   "]) is None
        assert fake_cls.init_attempts == 0  # short-circuits before load

    def test_embed_returns_none_when_create_embedding_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_cls = _make_fake_llama_class()
        fake_cls.embed_error = RuntimeError("inference blew up")
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5)
        assert emb.embed("hello") is None

    def test_embed_returns_none_on_malformed_response(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Vector count mismatch (empty data) degrades to None, not a crash."""
        fake_cls = _make_fake_llama_class()
        fake_cls.response_override = {"data": []}
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5)
        assert emb.embed("hello") is None

    def test_embed_truncates_pathological_input(self, tmp_path: Path, monkeypatch) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5)
        assert emb.embed("x" * 20_000) is not None
        sent = fake_cls.instances[0].embed_calls[0][0]
        assert len(sent) == embeddings_mod._MAX_EMBED_CHARS

    def test_load_failure_sets_cooldown(self, tmp_path: Path, monkeypatch) -> None:
        """A failed load is not retried within the cooldown window."""
        fail_cls = _make_failing_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fail_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5) is False
        assert emb.embed("first") is None
        assert fail_cls.init_attempts == 1
        # Second attempt inside the cooldown must NOT spawn a new loader thread.
        assert emb.wait_ready(timeout=5) is False
        assert emb.embed("second") is None
        assert fail_cls.init_attempts == 1

    def test_cooldown_expiry_retries_load(self, tmp_path: Path, monkeypatch) -> None:
        fail_cls = _make_failing_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fail_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 1
        # Within the cooldown a second wait_ready must NOT retry the load.
        assert emb.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 1
        # Simulate the cooldown elapsing.
        emb._load_failed_at -= embeddings_mod._LLM_LOAD_RETRY_SECS + 1
        assert emb.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 2

    def test_load_returns_none_when_llama_class_unavailable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Unsupported platform (no Llama class) → None + cooldown, no crash."""
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: None)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5) is False
        assert emb.embed("hello") is None
        assert emb._load_failed_at > 0

    def test_close_unloads_and_resets_cooldown(self, tmp_path: Path, monkeypatch) -> None:
        fail_cls = _make_failing_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fail_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 1
        emb.close()  # resets the failure cooldown
        assert emb._load_failed_at == 0.0
        assert emb.wait_ready(timeout=5) is False
        assert fail_cls.init_attempts == 2  # retried after close()

    def test_close_unloads_loaded_model(self, tmp_path: Path, monkeypatch) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5)
        assert emb.embed("hello") is not None
        assert emb.is_ready()
        emb.close()
        assert not emb.is_ready()
        # Safe to call repeatedly; the next embed kicks a fresh background load.
        emb.close()
        assert emb.embed("hello again") is None  # not loaded yet — reload kicked
        assert emb.wait_ready(timeout=5)
        assert emb.embed("hello again") is not None
        assert len(fake_cls.instances) == 2

    def test_concurrent_embeds_are_safe(self, tmp_path: Path, monkeypatch) -> None:
        """Lock-serialized embeds from many threads all succeed."""
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        emb = self._embedder(tmp_path)
        assert emb.wait_ready(timeout=5)  # load once, then race only inference
        results: list[list[float] | None] = [None] * 8
        errors: list[BaseException] = []

        def _work(i: int) -> None:
            try:
                results[i] = emb.embed(f"text {i}")
            except BaseException as exc:  # pragma: no cover - failure diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert all(r is not None and len(r) == _DIM for r in results)
        # The model loaded exactly once despite the concurrent first calls.
        assert len(fake_cls.instances) == 1


# ═══════════════════════════════════════════════════════════════════════════
# ModelDownloadManager
# ═══════════════════════════════════════════════════════════════════════════


def _fake_urlopen_factory(
    payload: bytes = _MODEL_BYTES,
    fail_rcs: list[bool] | None = None,
):
    """Build a urllib.request.urlopen replacement streaming a fake GGUF.

    ``fail_rcs`` is a per-attempt list of failure flags (True = the request
    raises URLError; False = the payload streams successfully).
    """
    state = SimpleNamespace(calls=0, urls=[])
    fails = fail_rcs if fail_rcs is not None else [False]

    class _FakeResponse:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0
            self.headers = {"Content-Length": str(len(data))}

        def read(self, n: int) -> bytes:
            chunk = self._data[self._pos:self._pos + n]
            self._pos += n
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _fake_urlopen(req, timeout=None, context=None):
        state.urls.append(getattr(req, "full_url", str(req)))
        fail = fails[min(state.calls, len(fails) - 1)]
        state.calls += 1
        if fail:
            raise urllib.error.URLError("fake network unreachable")
        return _FakeResponse(payload)

    return _fake_urlopen, state


class TestModelDownloadManager:
    """ensure_model download cycle — HTTP fully faked, no network."""

    @pytest.fixture(autouse=True)
    def _download_env(self, monkeypatch):
        """Allow the real download path (conftest sets the skip env globally)."""
        monkeypatch.delenv("KIROCREW_SKIP_MODEL_DOWNLOAD", raising=False)

        # Block real HTTP requests by default so a test that forgets to patch
        # urlopen can never touch the network.
        def _no_network(*args, **kwargs):
            raise urllib.error.URLError("blocked by test fixture")

        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", _no_network)

    def _mgr(self, tmp_path: Path) -> ModelDownloadManager:
        return ModelDownloadManager(target=tmp_path / "models" / "qwen3.gguf")

    @pytest.mark.asyncio
    async def test_successful_download_installs_model(self, tmp_path: Path, monkeypatch) -> None:
        fake_urlopen, state = _fake_urlopen_factory()
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(
            "kiro_crew.embeddings._GGUF_SHA256", hashlib.sha256(_MODEL_BYTES).hexdigest()
        )
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=1) is True
        assert mgr.target.is_file()
        assert mgr.target.stat().st_size == len(_MODEL_BYTES)
        assert mgr.status["step"] == "ready"
        assert mgr.status["error"] == ""
        assert state.calls == 1
        # No staging leftovers after the atomic os.replace install.
        assert list(mgr.target.parent.glob(".*.tmp")) == []

    @pytest.mark.asyncio
    async def test_env_url_override_wins(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_URL", "https://mirror.example/custom.gguf")
        fake_urlopen, state = _fake_urlopen_factory()
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(
            "kiro_crew.embeddings._GGUF_SHA256", hashlib.sha256(_MODEL_BYTES).hexdigest()
        )
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=1) is True
        assert state.urls == ["https://mirror.example/custom.gguf"]

    @pytest.mark.asyncio
    async def test_sha_mismatch_retries_then_fails(self, tmp_path: Path, monkeypatch) -> None:
        fake_urlopen, state = _fake_urlopen_factory()
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("kiro_crew.embeddings._GGUF_SHA256", "0" * 64)
        sleep_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.embeddings.asyncio.sleep", sleep_mock)
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=2) is False
        assert not mgr.target.exists()
        # The corrupt staging file was cleaned up, not left behind.
        assert list(mgr.target.parent.glob(".*.tmp")) == []
        assert mgr.status["step"] == "failed"
        assert "sha256 mismatch" in str(mgr.status["error"])
        assert mgr.status["attempt"] == 2
        assert state.calls == 2
        sleep_mock.assert_awaited_once()  # one backoff between the two attempts

    @pytest.mark.asyncio
    async def test_too_small_download_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A payload under _GGUF_MIN_BYTES is rejected even with a matching sha.

        Inherited upstream quirk: the too-small branch unlinks the staging
        file before formatting its error message from ``staging.stat()``, so
        the surfaced error is a generic "HTTPS download failed" rather than
        "too small" (recorded in skills/meshclaw-sync/left-out.md). The
        safety property under test — an undersized file is never installed —
        holds either way.
        """
        tiny = b"tiny placeholder"
        fake_urlopen, _state = _fake_urlopen_factory(payload=tiny)
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(
            "kiro_crew.embeddings._GGUF_SHA256", hashlib.sha256(tiny).hexdigest()
        )
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=1) is False
        assert not mgr.target.exists()
        assert list(mgr.target.parent.glob(".*.tmp")) == []
        assert mgr.status["step"] == "failed"
        assert "download failed" in str(mgr.status["error"])

    @pytest.mark.asyncio
    async def test_network_failure_reports_failed_status(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_urlopen, _state = _fake_urlopen_factory(fail_rcs=[True])
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=1) is False
        assert mgr.status["step"] == "failed"
        assert "HTTPS download failed" in str(mgr.status["error"])
        assert list(mgr.target.parent.glob(".*.tmp")) == []

    @pytest.mark.asyncio
    async def test_retry_after_transient_failure_succeeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """attempts=2: first request fails, second succeeds after backoff."""
        fake_urlopen, state = _fake_urlopen_factory(fail_rcs=[True, False])
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(
            "kiro_crew.embeddings._GGUF_SHA256", hashlib.sha256(_MODEL_BYTES).hexdigest()
        )
        sleep_mock = AsyncMock()
        monkeypatch.setattr("kiro_crew.embeddings.asyncio.sleep", sleep_mock)
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=2) is True
        assert mgr.target.is_file()
        assert mgr.status["step"] == "ready"
        assert mgr.status["attempt"] == 2
        assert state.calls == 2
        # Exponential backoff base delay before the retry.
        sleep_mock.assert_awaited_once_with(embeddings_mod._DOWNLOAD_BACKOFF_BASE_SECS)

    @pytest.mark.asyncio
    async def test_env_skip_returns_false_without_network(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
        fake_urlopen, state = _fake_urlopen_factory()
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=3) is False
        assert state.calls == 0  # no network activity whatsoever
        assert mgr.status["step"] == "idle"

    @pytest.mark.asyncio
    async def test_model_already_present_returns_true_without_network(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        fake_urlopen, state = _fake_urlopen_factory()
        monkeypatch.setattr("kiro_crew.embeddings.urllib.request.urlopen", fake_urlopen)
        mgr = self._mgr(tmp_path)
        _write_model_file(mgr.target)
        assert await mgr.ensure_model(attempts=1) is True
        assert state.calls == 0
        assert mgr.status["step"] == "ready"

    @pytest.mark.asyncio
    async def test_salvages_legacy_ollama_blob_without_network(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A byte-identical blob in the legacy Ollama store skips the download."""
        digest = hashlib.sha256(_MODEL_BYTES).hexdigest()
        monkeypatch.setattr("kiro_crew.embeddings._GGUF_SHA256", digest)
        blobs = tmp_path / "ollama" / "models" / "blobs"
        blobs.mkdir(parents=True)
        (blobs / f"sha256-{digest}").write_bytes(_MODEL_BYTES)
        monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "ollama" / "models"))
        # urlopen stays blocked (autouse fixture) — salvage must not need it.
        mgr = self._mgr(tmp_path)
        assert await mgr.ensure_model(attempts=1) is True
        assert mgr.target.is_file()
        assert mgr.target.read_bytes() == _MODEL_BYTES
        assert mgr.status["step"] == "ready"

    @pytest.mark.asyncio
    async def test_salvage_rejects_wrong_sha_blob(self, tmp_path: Path, monkeypatch) -> None:
        """A blob at the expected path with WRONG bytes is rejected (sha gate)."""
        digest = hashlib.sha256(_MODEL_BYTES).hexdigest()
        monkeypatch.setattr("kiro_crew.embeddings._GGUF_SHA256", digest)
        blobs = tmp_path / "ollama" / "models" / "blobs"
        blobs.mkdir(parents=True)
        (blobs / f"sha256-{digest}").write_bytes(b"x" * len(_MODEL_BYTES))
        monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "ollama" / "models"))
        mgr = self._mgr(tmp_path)
        # Salvage fails sha verification; the blocked urlopen then fails too.
        assert await mgr.ensure_model(attempts=1) is False
        assert not mgr.target.exists()
        assert mgr.status["step"] == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# make_sync_embed_fn
# ═══════════════════════════════════════════════════════════════════════════


class _FakeSharedEmbedder:
    """Stand-in for the shared LlamaCppEmbedder with a controllable failure."""

    # make_sync_embed_fn keys its lru_cache on (text, model_id).
    model_id = "fake-model:test"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def embed(self, text: str) -> list[float] | None:
        self.calls += 1
        if self.fail:
            return None
        return [0.5] * _DIM

    def is_ready(self) -> bool:
        return True


class TestMakeSyncEmbedFn:
    @pytest.fixture()
    def fake_embedder(self, monkeypatch) -> _FakeSharedEmbedder:
        fake = _FakeSharedEmbedder()
        monkeypatch.setattr("kiro_crew.embeddings.get_shared_embedder", lambda: fake)
        return fake

    def test_takes_no_args_and_returns_vector(self, fake_embedder) -> None:
        embed = make_sync_embed_fn()
        vec = embed("hello")
        assert isinstance(vec, list)
        assert len(vec) == _DIM

    def test_caches_successful_result(self, fake_embedder) -> None:
        embed = make_sync_embed_fn()
        first = embed("hello")
        second = embed("hello")
        assert first == second
        assert fake_embedder.calls == 1  # second call served from lru_cache
        embed("different text")
        assert fake_embedder.calls == 2

    def test_failure_returns_none_and_is_not_cached(self, fake_embedder) -> None:
        embed = make_sync_embed_fn()
        fake_embedder.fail = True
        assert embed("hello") is None
        assert fake_embedder.calls == 1
        # Model becomes available (download landed) — the same text is retried.
        fake_embedder.fail = False
        vec = embed("hello")
        assert vec is not None
        assert fake_embedder.calls == 2


# ═══════════════════════════════════════════════════════════════════════════
# start_background_model_download
# ═══════════════════════════════════════════════════════════════════════════


class TestStartBackgroundModelDownload:
    @pytest.mark.asyncio
    async def test_returns_none_when_model_present(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        _write_model_file(default_model_path())
        assert start_background_model_download() is None
        assert model_download_manager().status["step"] == "ready"

    @pytest.mark.asyncio
    async def test_returns_none_on_env_skip(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
        assert start_background_model_download() is None

    @pytest.mark.asyncio
    async def test_returns_task_when_download_needed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_SKIP_MODEL_DOWNLOAD", raising=False)
        mgr = model_download_manager()
        ensure_mock = AsyncMock(return_value=False)
        monkeypatch.setattr(mgr, "ensure_model", ensure_mock)
        task = start_background_model_download()
        assert isinstance(task, asyncio.Task)
        try:
            assert await task is False
        finally:
            task.cancel()
        ensure_mock.assert_awaited_once_with(attempts=_DOWNLOAD_MAX_ATTEMPTS)


# ═══════════════════════════════════════════════════════════════════════════
# Singletons
# ═══════════════════════════════════════════════════════════════════════════


class TestSingletons:
    def test_shared_embedder_is_singleton(self) -> None:
        first = get_shared_embedder()
        assert get_shared_embedder() is first

    def test_reset_shared_embedder_drops_instance(self) -> None:
        first = get_shared_embedder()
        reset_shared_embedder()
        assert get_shared_embedder() is not first

    def test_reset_shared_embedder_closes_model(self, tmp_path: Path, monkeypatch) -> None:
        fake_cls = _make_fake_llama_class()
        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: fake_cls)
        model = _write_model_file(tmp_path / "model.gguf")
        monkeypatch.setattr("kiro_crew.embeddings.default_model_path", lambda: model)
        emb = get_shared_embedder()
        assert emb.wait_ready(timeout=5)
        assert emb.embed("hello") is not None
        assert emb.is_ready()
        reset_shared_embedder()
        assert not emb.is_ready()  # close() unloaded the model

    def test_download_manager_is_singleton(self) -> None:
        first = model_download_manager()
        assert model_download_manager() is first

    def test_reset_download_manager_drops_instance(self) -> None:
        first = model_download_manager()
        reset_download_manager()
        assert model_download_manager() is not first
