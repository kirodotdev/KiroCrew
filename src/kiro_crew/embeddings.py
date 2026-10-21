"""In-process embedding runtime and model download manager.

Embeddings run in-process via the vendored llama-cpp-python runtime
(``kiro_crew/_vendor``) — no external Ollama server, no HTTP hop, no
runtime pip install. The Qwen3-Embedding-0.6B GGUF model is downloaded in
the background (sha256-verified, with retries) and installed persistently
to ``~/.kiro/crew/models/``. Sources are tried in order: a byte-identical
blob salvaged from a legacy Ollama install, then the public CloudFront CDN
(plain HTTPS — no git access, no cloud SDK). ``KIROCREW_EMBED_MODEL_URL``
(or the ``memory.embed_model_url`` config knob) overrides the CDN URL for
mirrored/airgapped deployments.

A user-supplied model can be run instead of the bundled one by pointing
``KIROCREW_EMBED_MODEL_PATH`` (or ``memory.embed_model_path``) at a local
GGUF. In that mode the default model is never downloaded or installed, so a
custom model survives a default-model version bump. Changing the model also
changes the vector space, so stored embeddings are re-generated automatically
— see :func:`embedding_space_signature`.

While the model file is absent (first boot, download in flight, or a
failed download) every embed call returns ``None`` and memory degrades
gracefully to keyword/FTS search — the existing lazy-rebind machinery in
``vector_memory._try_embed`` picks embeddings up automatically once the
model lands, no gateway restart required.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the embedding runtime's owners, and every name is reached
# through it as ``kiro_crew.embeddings.<name>``: a read is served by the owner that binds
# the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every module
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New work
# goes to the owner of its responsibility:
#
#   ``embeddings_settings`` -- Constants, scheduling priorities, the ``memory`` config knobs.
#   ``embeddings_native`` -- Loading the vendored llama.cpp runtime on this platform.
#   ``embeddings_gguf`` -- GGUF embedding metadata and the context a model loads with.
#   ``embeddings_model`` -- The active model file: bundled path, custom model, identity.
#   ``embeddings_llama`` -- ``EmbeddingBackend`` and the in-process ``LlamaCppEmbedder``.
#   ``embeddings_shared`` -- The shared embedder, its embedding space, the sync embed cache.
#   ``embeddings_download`` -- Downloading the bundled model in the background.

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.embeddings_settings",
    "kiro_crew.embeddings_native",
    "kiro_crew.embeddings_gguf",
    "kiro_crew.embeddings_model",
    "kiro_crew.embeddings_llama",
    "kiro_crew.embeddings_shared",
    "kiro_crew.embeddings_download",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.embeddings_download import (  # noqa: F401
        ModelDownloadManager,
        _download_manager,
        _download_manager_lock,
        _download_task,
        _operator_env_model_url,
        _resolve_model_url,
        _run_download_on_daemon_thread,
        logger,
        model_download_manager,
        redact_model_url,
        reset_download_manager,
        start_background_model_download,
    )
    from kiro_crew.embeddings_gguf import (  # noqa: F401
        _DECODER_CONTEXT_POLICY,
        _GGUF_CAUSAL_SUFFIX,
        _GGUF_CONTEXT_LENGTH_SUFFIX,
        _GGUF_I32,
        _GGUF_MAX_ARRAY_ITEMS,
        _GGUF_MAX_DECODED_STRING_BYTES,
        _GGUF_MAX_METADATA_ITEMS,
        _GGUF_POOLING_TYPE_SUFFIX,
        _GGUF_READ_CHUNK_BYTES,
        _GGUF_SCALAR_BYTES,
        _GGUF_TYPE_ARRAY,
        _GGUF_TYPE_BOOL,
        _GGUF_TYPE_INT32,
        _GGUF_TYPE_STRING,
        _GGUF_TYPE_UINT32,
        _GGUF_U8,
        _GGUF_U32,
        _GGUF_U64,
        _ContextPolicy,
        _GGUFEmbeddingMetadata,
        _model_context_policy,
        _parse_gguf_embedding_metadata,
        _read_gguf_embedding_metadata,
        gguf_declared_pooling,
    )
    from kiro_crew.embeddings_llama import (  # noqa: F401
        EmbeddingBackend,
        LlamaCppEmbedder,
        _caller_may_wait_for_load,
        _chars_bucket,
        _emit_embed_timing,
        _free_llama,
        _InferJob,
        _install_bounded_decode,
        _install_metadata_kv_clear,
        _trim_native_heap,
    )
    from kiro_crew.embeddings_model import (  # noqa: F401
        _CUSTOM_LABEL,
        _POOLING_SUFFIX_SEPARATOR,
        LEGACY_EMBEDDING_WARNING,
        CustomModelSpec,
        _custom_model_id,
        _is_sensitive_model_path,
        _last_custom_model_error,
        _log_custom_model_error,
        _model_content_digest,
        _model_file_stamp,
        _model_file_stamp_unaliased,
        _model_identity_lock,
        _model_verification_lock,
        _model_verification_thread,
        _ModelIdentityUnverified,
        _new_model_pooling,
        _parse_model_id,
        _ParsedModelId,
        _refuse_on_event_loop,
        _sha256_file,
        _start_model_verification,
        _verify_custom_model,
        active_model_path,
        bundled_embedding_dim,
        default_model_path,
        embedding_model_is_custom,
        legacy_embedding_ids,
        model_file_present,
        model_pooling_type,
        models_dir,
        resolve_custom_model,
        validate_custom_model_path,
    )
    from kiro_crew.embeddings_native import (  # noqa: F401
        _LIB_PATH_SEAM,
        _LINUX_CPUINFO_PATH,
        _LINUX_X86_64_REQUIRED_CPU_FLAGS,
        _VENDOR_DIR,
        _WINDOWS_MSVC_RUNTIME_DLLS,
        _harden_llama_null_streams,
        _install_diskcache_stub,
        _linux_x86_64_cpu_flags,
        _load_llama_class,
        _log_dropped_inherited_lib_path,
        _macos_x86_64_missing_cpu_flags,
        _missing_windows_msvc_runtime,
        _platform_libs_dirname,
        _publish_lib_path_for_import,
        verify_vendored_libs,
    )
    from kiro_crew.embeddings_settings import (  # noqa: F401
        _COLD_LOAD_WAIT_SECS,
        _DEFAULT_BULK_DUTY,
        _DEFAULT_BULK_THREADS,
        _DEFAULT_DIM,
        _DEFAULT_EMBED_THREADS,
        _DEFAULT_IDLE_EVICT_SECS,
        _DEFAULT_MODEL_URL,
        _DOWNLOAD_BACKOFF_BASE_SECS,
        _DOWNLOAD_BACKOFF_CAP_SECS,
        _DOWNLOAD_MAX_ATTEMPTS,
        _EMBED_WAIT_LOG_MS,
        _EMBED_WAIT_SECS,
        _ENCODER_ARCHITECTURES,
        _EVICT_POLL_MAX_SECS,
        _GGUF_FILENAME,
        _GGUF_MIN_BYTES,
        _GGUF_SHA256,
        _HTTP_CHUNK_BYTES,
        _HTTP_TIMEOUT_SECS,
        _INFER_STOP_TIMEOUT_SECS,
        _INTERACTIVE_QUEUE_RESERVE,
        _LLM_LOAD_RETRY_SECS,
        _MAX_BULK_PACE_SLEEP,
        _MAX_EMBED_BATCH_TEXTS,
        _MAX_EMBED_CHARS,
        _MAX_PENDING_EMBEDS,
        _MIN_BULK_DUTY,
        _MODEL_ID,
        _MODEL_PATH_ENV,
        _MODEL_URL_ENV,
        _MODELS_DIR_NAME,
        _N_CTX,
        _N_UBATCH,
        _POOLING_TYPE_LAST,
        _POOLING_TYPES,
        _PRIORITY_SENTINEL,
        _PROGRESS_EVERY_BYTES,
        _REJECTED_MODEL_SENTINEL,
        _SKIP_DOWNLOAD_ENV,
        DOWNLOAD_ATTEMPTS_INTERACTIVE,
        PRIORITY_BULK,
        PRIORITY_INTERACTIVE,
        PRIORITY_NORMAL,
        EmbeddingWork,
        _embed_threads,
        _read_memory_config,
        _work_for_priority,
        bulk_duty_cycle,
        bulk_embed_threads,
        bulk_pace_delay,
        embedding_idle_evict_secs,
        embedding_work,
    )
    from kiro_crew.embeddings_shared import (  # noqa: F401
        _EMBED_CACHE_MAX,
        ReembedProgress,
        _align_store_embedding_space,
        _AlignableStore,
        _backend_factory,
        _embedding_alignment_lock,
        _EmbedFlight,
        _ReconcilableStore,
        _reembed_progress,
        _retire_backend,
        _shared_embedder,
        _shared_embedder_lock,
        _shared_sync_embed,
        _sync_embed_cache,
        _sync_embed_cache_backend,
        _sync_embed_cache_lock,
        _sync_embed_flights,
        _sync_embed_stripes,
        activate_shared_embedder,
        active_embedding_space_signature,
        align_store_embedding_space,
        build_gated_bundled,
        build_gated_candidate,
        default_embedding_backend,
        default_embedding_space_signature,
        embedding_backend_serving,
        embedding_in_flight,
        embedding_rebuild_generation,
        embedding_space_signature,
        get_shared_embedder,
        install_shared_embedder,
        make_sync_embed_fn,
        peek_ready_shared_embedder,
        peek_shared_embedding_identity,
        reconcile_store_embedding_space,
        reembed_progress,
        register_embedding_backend,
        reset_shared_embedder,
        store_embedding_space_is_stale,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        asset_downloader,
    )
    from kiro_crew._llama_lib_path import (  # noqa: F401
        _LIB_PATH_ENV,
        _LIBS_DIR_NAME,
        _REQUIRED_VENDORED_LIBS,
        consume_dropped_inherited_lib_path,
        operator_lib_path_override,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        config_path,
        read_config_text,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        config_dir,
    )
    from kiro_crew.cpu_affinity import (  # noqa: F401
        affinity_cpu_count,
    )
    from kiro_crew.metrics.provider import (  # noqa: F401
        get_recorder,
    )
    from kiro_crew.security import (  # noqa: F401
        is_sensitive_path,
    )
