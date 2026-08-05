"""Custom embedding model: resolution, download suppression, and auto re-embed.

Covers the four guarantees the feature makes:

1. a local GGUF can be configured (env or ``memory.embed_model_path``);
2. changing the model regenerates stored embeddings automatically;
3. a configured custom model is never replaced by the bundled default;
4. a broken custom path fails CLOSED — it never silently reverts to the
   default, because that would swap the vector space behind the user's back.

The 610MB model is never involved: files are small stand-ins and the llama.cpp
runtime is a fake class, as in ``test_embeddings.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import (
    LlamaCppEmbedder,
    ModelDownloadManager,
    active_embedding_space_signature,
    active_model_path,
    default_embedding_backend,
    default_embedding_space_signature,
    default_model_path,
    embedding_model_is_custom,
    embedding_space_signature,
    model_file_present,
    reconcile_store_embedding_space,
    reset_download_manager,
    reset_shared_embedder,
    resolve_custom_model,
    start_background_model_download,
    store_embedding_space_is_stale,
)

_REAL_LOAD_LLAMA = embeddings_mod._load_llama_class

# >_GGUF_MIN_BYTES so the file passes the truncated-placeholder check.
_MODEL_BYTES = b"g" * 1_100_000
_MODEL_PATH_ENV = "KIROCREW_EMBED_MODEL_PATH"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    """Isolate singletons, the config file, and the model-path env var."""
    monkeypatch.delenv(_MODEL_PATH_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_EMBED_MODEL_URL", raising=False)
    # Point config_path at a file that does not exist by default, so a stray
    # real config on the dev host can never leak into these assertions.
    monkeypatch.setattr(
        "kiro_crew.embeddings.config_path", lambda: tmp_path / "config.json"
    )
    monkeypatch.setattr("kiro_crew.embeddings.config_dir", lambda: tmp_path / "home")
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()
    yield
    reset_shared_embedder()
    reset_download_manager()
    _REAL_LOAD_LLAMA.cache_clear()


def _write_config(tmp_path: Path, memory: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"memory": memory}), encoding="utf-8")


def _write_model(path: Path, payload: bytes = _MODEL_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _fake_llama_class(n_embd: int | None = None):
    """Fake llama.cpp runtime; ``n_embd`` present only when a dim is given."""

    class _Fake:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            type(self).instances.append(self)

        def create_embedding(self, texts):
            width = n_embd if n_embd is not None else 1024
            return {"data": [{"embedding": [0.1] * width} for _ in texts]}

    if n_embd is not None:

        def _n_embd(self) -> int:
            return n_embd

        _Fake.n_embd = _n_embd  # type: ignore[attr-defined]
    return _Fake


# ═══════════════════════════════════════════════════════════════════════════
# resolve_custom_model
# ═══════════════════════════════════════════════════════════════════════════


class TestResolveCustomModel:
    def test_none_when_not_configured(self) -> None:
        assert resolve_custom_model() is None
        assert embedding_model_is_custom() is False

    def test_config_path_is_resolved(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        spec = resolve_custom_model()
        assert spec is not None
        assert spec.error == ""
        assert spec.path == model
        assert spec.dim == 1024

    def test_env_wins_over_config(self, tmp_path: Path, monkeypatch) -> None:
        cfg_model = _write_model(tmp_path / "from-config.gguf")
        env_model = _write_model(tmp_path / "from-env.gguf")
        _write_config(tmp_path, {"embed_model_path": str(cfg_model)})
        monkeypatch.setenv(_MODEL_PATH_ENV, str(env_model))
        spec = resolve_custom_model()
        assert spec is not None and spec.path == env_model

    def test_configured_dim_is_used(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "small.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embedding_dim": 768})
        spec = resolve_custom_model()
        assert spec is not None and spec.dim == 768

    def test_model_id_derived_from_name_and_size(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        spec = resolve_custom_model()
        assert spec is not None
        assert spec.model_id == f"custom:mine.gguf:{len(_MODEL_BYTES)}"

    def test_explicit_model_id_wins(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(
            tmp_path, {"embed_model_path": str(model), "embed_model_id": "bge-m3:local"}
        )
        spec = resolve_custom_model()
        assert spec is not None and spec.model_id == "bge-m3:local"

    def test_different_file_changes_the_model_id(self, tmp_path: Path) -> None:
        """A swapped model file must produce a different vector-space identity."""
        first = _write_model(tmp_path / "a.gguf", b"a" * 1_100_000)
        _write_config(tmp_path, {"embed_model_path": str(first)})
        id_a = resolve_custom_model().model_id  # type: ignore[union-attr]
        second = _write_model(tmp_path / "b.gguf", b"b" * 1_200_000)
        _write_config(tmp_path, {"embed_model_path": str(second)})
        id_b = resolve_custom_model().model_id  # type: ignore[union-attr]
        assert id_a != id_b

    @pytest.mark.parametrize(
        "make_value, expected_fragment",
        [
            (lambda tmp: "relative/mine.gguf", "absolute path"),
            (lambda tmp: str(tmp / "missing.gguf"), "does not exist"),
        ],
    )
    def test_invalid_paths_report_an_error(
        self, tmp_path: Path, make_value, expected_fragment: str
    ) -> None:
        _write_config(tmp_path, {"embed_model_path": make_value(tmp_path)})
        spec = resolve_custom_model()
        assert spec is not None, "a configured-but-broken path must stay custom"
        assert expected_fragment in spec.error

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "adir"
        target.mkdir()
        _write_config(tmp_path, {"embed_model_path": str(target)})
        spec = resolve_custom_model()
        assert spec is not None and "not a regular file" in spec.error

    def test_truncated_file_is_rejected(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "stub.gguf", b"tiny")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        spec = resolve_custom_model()
        assert spec is not None and "too small" in spec.error

    def test_broken_path_never_falls_back_to_default(self, tmp_path: Path) -> None:
        """The core fail-closed guarantee (requirement 4 of the docstring).

        Falling back would embed into the DEFAULT vector space and silently
        re-embed the user's whole corpus because of a typo.
        """
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "typo.gguf")})
        assert embedding_model_is_custom() is True
        assert active_model_path() != default_model_path()


class TestSensitivePathGate:
    """A GGUF is mmap'd and parsed by native llama.cpp, so this knob is a
    file-access surface and must use the same gate as every other one
    (``security.is_sensitive_path``: credential stores + governance trust-root).
    """

    def test_credential_store_is_refused(self, monkeypatch, tmp_path: Path) -> None:
        secret = _write_model(tmp_path / "credentials")
        _write_config(tmp_path, {"embed_model_path": str(secret)})
        monkeypatch.setattr(
            "kiro_crew.embeddings.is_sensitive_path", lambda p, base_dir=None: True
        )
        spec = resolve_custom_model()
        assert spec is not None
        assert "protected location" in spec.error

    def test_refusal_precedes_existence_and_size_checks(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A protected path must be refused as protected, not as 'missing'.

        Ordering matters: reporting the wrong reason would mislead the operator,
        and the gate must not depend on the file being readable.
        """
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "nope" / "id_rsa")})
        monkeypatch.setattr(
            "kiro_crew.embeddings.is_sensitive_path", lambda p, base_dir=None: True
        )
        spec = resolve_custom_model()
        assert spec is not None and "protected location" in spec.error

    def test_ordinary_path_is_allowed(self, tmp_path: Path) -> None:
        """The gate must not reject a normal model directory."""
        model = _write_model(tmp_path / "models" / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        spec = resolve_custom_model()
        assert spec is not None and spec.error == ""

    def test_gate_failure_fails_closed(self, monkeypatch, tmp_path: Path) -> None:
        """An unavailable gate must refuse, never silently widen access."""
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})

        def _boom(_p, base_dir=None):
            raise RuntimeError("gate unavailable")

        monkeypatch.setattr("kiro_crew.embeddings.is_sensitive_path", _boom)
        spec = resolve_custom_model()
        assert spec is not None and "protected location" in spec.error

    def test_download_still_suppressed_for_a_refused_path(self, tmp_path: Path) -> None:
        """A refused path must not fall back to fetching the bundled model."""
        secret = _write_model(tmp_path / "credentials")
        _write_config(tmp_path, {"embed_model_path": str(secret)})
        assert embedding_model_is_custom() is True
        assert start_background_model_download() is None

    def test_factory_does_not_hand_over_a_refused_path(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Recording the refusal is not enough — the path must not be adopted.

        A protected file can exist AND be large enough to satisfy
        ``model_file_present()``, so a backend constructed on it would be one
        gate away from handing it to llama.cpp.
        """
        secret = _write_model(tmp_path / "credentials")
        _write_config(tmp_path, {"embed_model_path": str(secret)})
        monkeypatch.setattr(
            "kiro_crew.embeddings.is_sensitive_path", lambda p, base_dir=None: True
        )
        backend = default_embedding_backend()
        assert isinstance(backend, LlamaCppEmbedder)
        assert backend.model_path != secret
        assert not backend.model_path.exists()

    def test_protected_path_is_refused_at_the_load_boundary(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The real security boundary: an explicitly-constructed embedder.

        ``model_file_present`` is passed an explicit path here, so it does not
        re-derive the active path and cannot apply the gate — the load itself
        must refuse.
        """
        secret = _write_model(tmp_path / "credentials")
        monkeypatch.setattr(
            "kiro_crew.embeddings.is_sensitive_path", lambda p, base_dir=None: True
        )
        opened: list = []

        class _Tripwire:
            def __init__(self, **kwargs):
                opened.append(kwargs.get("model_path"))

            def create_embedding(self, texts):  # pragma: no cover - never reached
                raise AssertionError("must not run")

        monkeypatch.setattr("kiro_crew.embeddings._load_llama_class", lambda: _Tripwire)
        embedder = LlamaCppEmbedder(model_path=secret, dim=1024, model_id="x:1")
        # The file exists and is big enough, so the presence check alone passes.
        assert model_file_present(secret) is True
        embedder.wait_ready(timeout=5)
        assert embedder.is_ready() is False
        assert opened == [], "llama.cpp must never be handed a protected path"

    def test_ordinary_path_still_loads(self, monkeypatch, tmp_path: Path) -> None:
        """Guard against the new refusal blocking legitimate models."""
        model = _write_model(tmp_path / "models" / "mine.gguf")
        monkeypatch.setattr(
            "kiro_crew.embeddings._load_llama_class", lambda: _fake_llama_class(n_embd=1024)
        )
        embedder = LlamaCppEmbedder(model_path=model, dim=1024, model_id="x:1")
        embedder.wait_ready(timeout=5)
        assert embedder.is_ready() is True

    def test_persistent_error_is_logged_once(self, tmp_path: Path, caplog) -> None:
        """The resolver runs on polled paths — it must not flood the log.

        ``model_file_present`` resolves, and the dashboard polls the embedding
        status endpoint every ~2s, so an unconditional log would emit an ERROR
        every couple of seconds for as long as the path stays broken.
        """
        embeddings_mod._last_custom_model_error = ""
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "typo.gguf")})
        with caplog.at_level("ERROR", logger="kiro_crew.embeddings"):
            for _ in range(5):
                resolve_custom_model()
        assert sum("unusable" in r.message for r in caplog.records) == 1

    def test_a_different_error_still_logs(self, tmp_path: Path, caplog) -> None:
        """Dedup must not swallow a NEW misconfiguration."""
        embeddings_mod._last_custom_model_error = ""
        with caplog.at_level("ERROR", logger="kiro_crew.embeddings"):
            _write_config(tmp_path, {"embed_model_path": str(tmp_path / "typo.gguf")})
            resolve_custom_model()
            _write_config(tmp_path, {"embed_model_path": "relative/path.gguf"})
            resolve_custom_model()
        assert sum("unusable" in r.message for r in caplog.records) == 2

    def test_recovery_then_regression_logs_again(self, tmp_path: Path, caplog) -> None:
        """A fix-then-break cycle on the SAME path must log the second time."""
        embeddings_mod._last_custom_model_error = ""
        broken = {"embed_model_path": str(tmp_path / "mine.gguf")}
        with caplog.at_level("ERROR", logger="kiro_crew.embeddings"):
            _write_config(tmp_path, broken)
            resolve_custom_model()  # missing -> logs
            _write_model(tmp_path / "mine.gguf")
            resolve_custom_model()  # now valid -> clears the dedup state
            (tmp_path / "mine.gguf").unlink()
            resolve_custom_model()  # broken again -> must log again
        assert sum("unusable" in r.message for r in caplog.records) == 2


# ═══════════════════════════════════════════════════════════════════════════
# active path + presence
# ═══════════════════════════════════════════════════════════════════════════


class TestActiveModelPath:
    def test_defaults_to_bundled_model(self) -> None:
        assert active_model_path() == default_model_path()

    def test_points_at_custom_model(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        assert active_model_path() == model

    def test_model_file_present_follows_the_custom_path(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model)})
        # The bundled model is absent, yet embeddings are available.
        assert not default_model_path().exists()
        assert model_file_present() is True

    def test_model_file_present_false_when_custom_missing(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "gone.gguf")})
        assert model_file_present() is False


# ═══════════════════════════════════════════════════════════════════════════
# backend construction
# ═══════════════════════════════════════════════════════════════════════════


class TestDefaultEmbeddingBackend:
    def test_bundled_backend_when_unconfigured(self) -> None:
        backend = default_embedding_backend()
        assert isinstance(backend, LlamaCppEmbedder)
        assert backend.model_path == default_model_path()
        assert backend.model_id == embeddings_mod._MODEL_ID

    def test_custom_backend_carries_path_id_and_dim(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(
            tmp_path,
            {"embed_model_path": str(model), "embedding_dim": 768, "embed_model_id": "m:1"},
        )
        backend = default_embedding_backend()
        assert isinstance(backend, LlamaCppEmbedder)
        assert backend.model_path == model
        assert backend.model_id == "m:1"
        assert backend.dim == 768

    def test_shared_embedder_picks_up_the_custom_model(self, tmp_path: Path) -> None:
        """Consumers get the custom model without knowing it exists."""
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embed_model_id": "m:1"})
        assert embeddings_mod.get_shared_embedder().model_id == "m:1"


class TestLoadTimeDimValidation:
    def test_mismatched_dim_refuses_to_load(self, tmp_path: Path, monkeypatch) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        monkeypatch.setattr(
            "kiro_crew.embeddings._load_llama_class", lambda: _fake_llama_class(n_embd=384)
        )
        embedder = LlamaCppEmbedder(model_path=model, dim=1024, model_id="m:1")
        embedder.wait_ready(timeout=5)
        assert embedder.is_ready() is False, "a wrong-dim model must not be published"
        assert embedder.embed("hello") is None

    def test_matching_dim_loads(self, tmp_path: Path, monkeypatch) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        monkeypatch.setattr(
            "kiro_crew.embeddings._load_llama_class", lambda: _fake_llama_class(n_embd=768)
        )
        embedder = LlamaCppEmbedder(model_path=model, dim=768, model_id="m:1")
        embedder.wait_ready(timeout=5)
        assert embedder.is_ready() is True
        assert embedder.embed("hello") == [0.1] * 768

    def test_runtime_without_n_embd_still_loads(self, tmp_path: Path, monkeypatch) -> None:
        """The probe is advisory — a runtime lacking n_embd() is not blocked."""
        model = _write_model(tmp_path / "mine.gguf")
        monkeypatch.setattr(
            "kiro_crew.embeddings._load_llama_class", lambda: _fake_llama_class(n_embd=None)
        )
        embedder = LlamaCppEmbedder(model_path=model, dim=1024, model_id="m:1")
        embedder.wait_ready(timeout=5)
        assert embedder.is_ready() is True


# ═══════════════════════════════════════════════════════════════════════════
# the default model is never installed over a custom one
# ═══════════════════════════════════════════════════════════════════════════


class TestDownloadSuppression:
    def test_background_download_is_skipped(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"embed_model_path": str(_write_model(tmp_path / "m.gguf"))})
        assert start_background_model_download() is None

    def test_background_download_skipped_even_when_custom_is_broken(
        self, tmp_path: Path
    ) -> None:
        """A typo must not cause the bundled model to be installed instead."""
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "typo.gguf")})
        assert start_background_model_download() is None

    @pytest.mark.asyncio
    async def test_interactive_download_refuses(self, tmp_path: Path) -> None:
        """The dashboard Enable/Retry click must not fetch the bundled model."""
        _write_config(tmp_path, {"embed_model_path": str(_write_model(tmp_path / "m.gguf"))})
        mgr = ModelDownloadManager(target=tmp_path / "home" / "models" / "default.gguf")
        assert await mgr.ensure_model(attempts=1) is False
        assert mgr.status["step"] == "custom_model"
        assert not (tmp_path / "home" / "models" / "default.gguf").exists()

    @pytest.mark.asyncio
    async def test_default_download_still_runs_without_a_custom_model(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Guard against the suppression leaking into the default path."""
        # The shared harness sets KIROCREW_SKIP_MODEL_DOWNLOAD=1 so no test can
        # pull the 610MB model. Clear it here: _download_once is stubbed, so the
        # only thing left to exercise is the gating logic itself.
        monkeypatch.delenv("KIROCREW_SKIP_MODEL_DOWNLOAD", raising=False)
        target = tmp_path / "home" / "models" / "default.gguf"
        mgr = ModelDownloadManager(target=target)

        def _fake_download() -> tuple[bool, str]:
            _write_model(target)
            return True, ""

        monkeypatch.setattr(mgr, "_download_once", _fake_download)
        assert await mgr.ensure_model(attempts=1) is True
        assert mgr.status["step"] == "ready"


# ═══════════════════════════════════════════════════════════════════════════
# vector-space signature
# ═══════════════════════════════════════════════════════════════════════════


class TestEmbeddingSpaceSignature:
    def test_stable_for_the_same_inputs(self) -> None:
        assert embedding_space_signature("m", 1024) == embedding_space_signature("m", 1024)

    def test_differs_by_model(self) -> None:
        assert embedding_space_signature("a", 1024) != embedding_space_signature("b", 1024)

    def test_differs_by_dim(self) -> None:
        """The same-dim case is the one that used to corrupt search silently."""
        assert embedding_space_signature("m", 1024) != embedding_space_signature("m", 768)


class TestDefaultSpaceSignature:
    """Attribution of un-versioned vectors must not depend on HOW a model was
    selected. Keying on config alone missed a programmatic
    ``register_embedding_backend``, which would stamp foreign vectors as native.
    """

    def test_matches_the_bundled_model_identity(self) -> None:
        assert default_embedding_space_signature() == embedding_space_signature(
            embeddings_mod._MODEL_ID, embeddings_mod._DEFAULT_DIM
        )

    def test_is_config_independent(self, tmp_path: Path) -> None:
        """Configuring a custom model must not move the bundled-space identity."""
        before = default_embedding_space_signature()
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embedding_dim": 768})
        reset_shared_embedder()
        assert default_embedding_space_signature() == before

    def test_default_backend_is_recognised_as_the_bundled_space(self) -> None:
        backend = default_embedding_backend()
        active = embedding_space_signature(backend.model_id, backend.dim)
        assert active == default_embedding_space_signature()

    def test_custom_backend_is_not_the_bundled_space(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embed_model_id": "m:1"})
        reset_shared_embedder()
        backend = default_embedding_backend()
        active = embedding_space_signature(backend.model_id, backend.dim)
        assert active != default_embedding_space_signature()

    def test_registered_backend_is_not_the_bundled_space(self) -> None:
        """The gap GPT found: a registered backend sets no config, so a
        config-based check called it 'default' and adopted foreign vectors.
        """
        registered = LlamaCppEmbedder(
            model_path=Path("/tmp/whatever.gguf"), dim=768, model_id="registered:1"
        )
        active = embedding_space_signature(registered.model_id, registered.dim)
        assert embedding_model_is_custom() is False, "no config knob is set"
        assert active != default_embedding_space_signature(), (
            "yet the space is foreign, so clear_when_unknown must be True"
        )


class TestReconcileChokepoint:
    """Reconciliation must not be gateway-only.

    It previously lived inline in the gateway boot sweep, so every other process
    that opens a vector store — `kirocrew run` via cli_server, the onboarding
    importer — loaded a FAISS index built under the old model and scored it
    against new-model queries. One named chokepoint is what keeps a future entry
    point from silently reintroducing that.
    """

    class _FakeStore:
        def __init__(self, recorded: "str | None" = None) -> None:
            self.calls: list = []
            self._recorded = recorded

        def recorded_embedding_space(self) -> "str | None":
            return self._recorded

        def reconcile_embedding_space(
            self, signature: str, *, clear_when_unknown: bool = False
        ) -> int:
            self.calls.append((signature, clear_when_unknown))
            return 3 if clear_when_unknown else 0

    @staticmethod
    def _ready_backend(monkeypatch, model_id: str, dim: int = 1024) -> None:
        """Pretend the active backend is loaded and serving *model_id*."""

        class _Ready:
            def __init__(self) -> None:
                self.model_id = model_id
                self.dim = dim

            def is_ready(self) -> bool:
                return True

        monkeypatch.setattr("kiro_crew.embeddings.get_shared_embedder", lambda: _Ready())

    def test_default_model_does_not_request_clearing(self) -> None:
        store = self._FakeStore()
        assert reconcile_store_embedding_space(store) == 0
        (sig, clear), = store.calls
        assert sig == default_embedding_space_signature()
        assert clear is False, "a plain upgrade must not wipe anyone's vectors"

    def test_ready_custom_model_requests_clearing(self, monkeypatch) -> None:
        self._ready_backend(monkeypatch, "m:1")
        store = self._FakeStore()
        assert reconcile_store_embedding_space(store) == 3
        (sig, clear), = store.calls
        assert sig != default_embedding_space_signature()
        assert clear is True

    def test_unready_custom_model_does_not_clear(self, tmp_path: Path) -> None:
        """The destructive case: a path typo must not cost the user every vector.

        A rejected custom path yields a backend that can never load, so clearing
        would leave nothing able to re-embed. The chokepoint must refuse.
        """
        _write_config(tmp_path, {"embed_model_path": str(tmp_path / "typo.gguf")})
        reset_shared_embedder()
        assert embeddings_mod.get_shared_embedder().is_ready() is False
        store = self._FakeStore()
        assert reconcile_store_embedding_space(store) == 0
        assert store.calls == [], "the store must not be touched at all"

    def test_active_signature_needs_no_model_load(self, tmp_path: Path) -> None:
        """Cheap enough for any startup path: identity comes from the constructor."""
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embed_model_id": "m:1"})
        reset_shared_embedder()
        assert active_embedding_space_signature() == embedding_space_signature("m:1", 1024)
        assert embeddings_mod.get_shared_embedder().is_ready() is False


class TestStaleSpaceProbe:
    """The read-only probe a one-shot CLI uses instead of a destructive clear."""

    class _Store:
        def __init__(self, recorded: "str | None") -> None:
            self._recorded = recorded

        def recorded_embedding_space(self) -> "str | None":
            return self._recorded

        def reconcile_embedding_space(
            self, signature: str, *, clear_when_unknown: bool = False
        ) -> int:  # pragma: no cover - the probe must never call this
            raise AssertionError("the probe must not mutate")

    def test_unrecorded_space_matches_the_bundled_model(self) -> None:
        assert store_embedding_space_is_stale(self._Store(None)) is False

    def test_unrecorded_space_is_stale_under_a_custom_model(self, tmp_path: Path) -> None:
        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embed_model_id": "m:1"})
        reset_shared_embedder()
        assert store_embedding_space_is_stale(self._Store(None)) is True

    def test_matching_recorded_space_is_not_stale(self) -> None:
        store = self._Store(default_embedding_space_signature())
        assert store_embedding_space_is_stale(store) is False

    def test_foreign_recorded_space_is_stale(self) -> None:
        assert store_embedding_space_is_stale(self._Store("deadbeefdeadbeef")) is True


# ═══════════════════════════════════════════════════════════════════════════
# knowledge library re-embed
# ═══════════════════════════════════════════════════════════════════════════


class TestKnowledgeReembedsOnModelSwap:
    """The knowledge library already re-embeds on a model change; lock that in.

    Its per-item ``embedding_sig`` folds in the live backend's ``model_id``, and
    the watcher's self-heal path re-embeds every item whose stored sig differs.
    Configuring a custom model must therefore change the signature — otherwise
    stale knowledge vectors would survive the swap.
    """

    def test_signature_changes_with_a_custom_model(self, tmp_path: Path) -> None:
        from kiro_crew.knowledge.embedder import InProcessEmbedder, embedder_signature

        default_sig = embedder_signature(InProcessEmbedder())

        model = _write_model(tmp_path / "mine.gguf")
        _write_config(tmp_path, {"embed_model_path": str(model), "embed_model_id": "bge:local"})
        reset_shared_embedder()

        custom = InProcessEmbedder()
        assert custom.model == "bge:local"
        assert embedder_signature(custom) != default_sig
