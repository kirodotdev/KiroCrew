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

import abc
import asyncio
import functools
import hashlib
import importlib.util
import json
import logging
import os
import platform
import queue
import shutil
import ssl
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple, Protocol

from kiro_crew.config.loader import config_path
from kiro_crew.config.paths import config_dir
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)


class _ReconcilableStore(Protocol):
    """Structural type for a vector store, so this module needs no import of it."""

    def recorded_embedding_space(self) -> "str | None": ...

    def reconcile_embedding_space(
        self, signature: str, *, clear_when_unknown: bool = False
    ) -> int: ...


# ── Model constants ──

_GGUF_FILENAME = "qwen3-embedding-0.6b.gguf"
# Anything smaller than this is a truncated/placeholder file, not model weights.
_GGUF_MIN_BYTES = 1_000_000
# Stable model identifier: names the vector space, feeds the knowledge
# library's embed_signature staleness detection. Changing the model (or dim)
# invalidates stored vectors → triggers a re-embed/reindex.
_MODEL_ID = "qwen3-embedding:0.6b"
# sha256 of the published Qwen3-Embedding-0.6B Q8_0 GGUF. The trust anchor for
# every download source. Bump in lockstep when the published model is updated.
_GGUF_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
_DEFAULT_DIM = 1024  # Qwen3-Embedding-0.6B output dimension
_MODELS_DIR_NAME = "models"
# Stand-in path for a REJECTED custom model. Never created, so it can never
# satisfy model_file_present() and the embedder can never load — which is the
# point: a rejected spec must not carry the real (possibly protected) path.
_REJECTED_MODEL_SENTINEL = ".rejected-custom-model.invalid"

# ── Runtime constants ──

# Qwen3-Embedding requires last-token pooling (LLAMA_POOLING_TYPE_LAST).
_POOLING_TYPE_LAST = 3
# Context window for the embedding pass. Episodic memories are capped at
# 2000 chars and knowledge chunks are bounded by the chunker (~512 tokens +
# overlap, ≈5.8k chars max), so 2048 tokens covers both. Kept deliberately
# small: KV-cache size scales linearly with n_ctx (~115KB/token for this
# model) and the embedder may load in more than one process (gateway +
# kirocrew-core MCP server — the GGUF weights themselves are mmap'd and
# physically shared, the KV buffers are not). n_ubatch must cover the whole
# input for pooled embedding models — keep all three in lockstep.
_N_CTX = 2048
# Safety truncation (chars) before inference, sized under _N_CTX at a
# conservative ~4 chars/token so a clipped input always fits the context
# window. Only pathological un-chunked blobs exceed this; mirrors the
# knowledge embedder's content-budget backstop. Inputs that still exceed
# n_ctx after clipping (dense CJK/code) fail the embed call and return None.
_MAX_EMBED_CHARS = 6_000
_LLM_LOAD_RETRY_SECS = 300.0  # re-attempt a failed model load after this long
# How long close() waits for the inference thread to finish its current job and
# exit. Bounded so an unload never wedges a shutdown; the thread is a daemon, so
# a straggler cannot hold the interpreter open either.
_INFER_STOP_TIMEOUT_SECS = 30.0

# ── Download constants ──

_DOWNLOAD_MAX_ATTEMPTS = 6  # background startup task (long backoff, may span hours)
DOWNLOAD_ATTEMPTS_INTERACTIVE = 3  # dashboard Enable/Retry click (fast feedback)
_DOWNLOAD_BACKOFF_BASE_SECS = 60.0
_DOWNLOAD_BACKOFF_CAP_SECS = 1800.0
# Escape hatch for tests/CI: never kick a 610MB model download from a test run.
_SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"
# Distribution: public CloudFront CDN in front of KiroCrew's OWN model bucket
# (kirocrew-models, reachable only through the distribution's OAC — the bucket
# itself blocks public access). Serving our own copy rather than sharing another
# project's bucket keeps the download signal attributable: a fetch here is a
# KiroCrew first-install, uncontaminated by another product's traffic.
# Plain HTTPS, no git access and no cloud SDK required. The
# sha256 pin above is the trust anchor for every source, so a tampered CDN
# object can only fail verification. Resolution order: KIROCREW_EMBED_MODEL_URL
# env, then the memory.embed_model_url config knob, then this default. The URL
# basename (qwen3-embedding-0.6b.gguf) intentionally differs from the on-disk
# _GGUF_FILENAME — the sha pin, not the name, is the integrity gate.
_MODEL_URL_ENV = "KIROCREW_EMBED_MODEL_URL"
# Custom-model escape hatch: an absolute path to a user-supplied GGUF. When set
# (env, or the memory.embed_model_path config knob) KiroCrew runs THAT model and
# never downloads or installs the bundled default — including when the default's
# pinned version changes. No sha256 pin is applied: the pin is the trust anchor
# for a NETWORK download, whereas a local file is trusted by provenance (the
# user put it there). Note a GGUF is parsed by native llama.cpp code, so this
# knob is deliberately config-file-only — it is NOT in the dashboard's
# _EDITABLE_CONFIG allowlist, so no API caller and no agent can point the
# embedder at an arbitrary file.
_MODEL_PATH_ENV = "KIROCREW_EMBED_MODEL_PATH"
_DEFAULT_MODEL_URL = (
    "https://d3j0sthz5doyui.cloudfront.net/models/qwen3-embedding-0.6b.gguf"
)
_HTTP_TIMEOUT_SECS = 1800  # 610MB at >=340KB/s; slower links retry with backoff
_HTTP_CHUNK_BYTES = 1 << 20
# Written by the HTTP downloader every ~16MB so the status endpoint can report
# byte-level progress; the dashboard renders a determinate progress bar from it.
_PROGRESS_EVERY_BYTES = 16 << 20

# ── Vendored runtime loading ──

_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"
_LIBS_DIR_NAME = "llama_cpp_libs"
# Upstream-supported override naming the directory ctypes loads the native libs
# from (see _vendor/llama_cpp/llama_cpp.py). An operator-set value wins, which
# is the escape hatch for a GPU build or a hand-assembled lib dir.
_LIB_PATH_ENV = "LLAMA_CPP_LIB_PATH"

# The native-library closure every supported platform MUST ship, keyed by the
# `llama_cpp_libs/<dir>` name. `libllama` is the entry point ctypes opens by
# base name; the `libggml*` files are its NEEDED/@rpath dependencies, so a
# missing one fails the SAME way as a missing libllama — an unusable runtime.
#
# This is the single source of truth for "is the vendored payload complete",
# consumed by `verify_vendored_libs()` and asserted per packaging lane. It is
# deliberately CODE rather than a build-time glob: a glob over whatever is on
# disk can only prove the files present are shippable, never that a file that
# should exist was silently dropped by a packaging rule — which is exactly the
# failure mode `MANIFEST.in`'s `global-exclude *.so` produced (it stripped
# precisely `libllama.so`, since every other Linux lib ends `.so.0` and the
# macOS/Windows libs are `.dylib`/`.dll`). `python -m build` builds the wheel
# FROM the sdist, so that one glob shipped a Linux wheel whose vendored
# llama_cpp could not load its own shared library, silently degrading vector
# memory to keyword search on every pip-installed Linux host.
#
# No BLAS entry on Linux is intentional, not an omission: upstream publishes no
# BLAS backend in its Linux CPU wheels (macOS gets `libggml-blas` only because
# it links the system Accelerate framework). The Linux `libggml-cpu` carries the
# optimized GEMM/repack kernels instead, so the CPU path is complete without it.
_REQUIRED_VENDORED_LIBS: dict[str, tuple[str, ...]] = {
    "linux_x86_64": (
        "libllama.so",
        "libggml.so.0",
        "libggml-base.so.0",
        "libggml-cpu.so.0",
        "libgomp-a34b3233.so.1.0.0",
    ),
    "linux_aarch64": (
        "libllama.so",
        "libggml.so.0",
        "libggml-base.so.0",
        "libggml-cpu.so.0",
        "libgomp-d22c30c5.so.1.0.0",
    ),
    "macos_arm64": (
        "libllama.dylib",
        "libggml.0.dylib",
        "libggml-base.0.dylib",
        "libggml-blas.0.dylib",
        "libggml-cpu.0.dylib",
        "libggml-metal.0.dylib",
    ),
    "macos_x86_64": (
        "libllama.dylib",
        "libggml.0.dylib",
        "libggml-base.0.dylib",
        "libggml-blas.0.dylib",
        "libggml-cpu.0.dylib",
        "libggml-metal.0.dylib",
    ),
    "win_amd64": (
        "llama.dll",
        "ggml.dll",
        "ggml-base.dll",
        "ggml-cpu.dll",
    ),
}


def _platform_libs_dirname() -> str | None:
    """Map (sys.platform, machine) to a vendored native-libs directory name."""
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux_x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux_aarch64"
    elif sys.platform == "darwin":
        if machine == "arm64":
            return "macos_arm64"
        if machine == "x86_64":
            return "macos_x86_64"
    elif sys.platform == "win32":
        if machine in ("amd64", "x86_64"):
            return "win_amd64"
    return None


def verify_vendored_libs(root: Path | None = None) -> dict[str, list[str]]:
    """Report vendored native libs that :data:`_REQUIRED_VENDORED_LIBS` expects but are absent.

    Returns a mapping of platform dir -> sorted missing filenames; an empty
    mapping means the payload is complete for EVERY platform, not just the
    running one. ``root`` defaults to the installed ``_vendor`` directory, so
    the same check runs against a source tree, an unpacked sdist, or an
    installed wheel — that cross-lane reuse is the point, since each packaging
    lane (sdist rules, wheel package_data, PyInstaller spec) selects these
    files by a different mechanism and can therefore drop them independently.

    A platform dir that is entirely absent is reported as missing all of its
    libs rather than skipped: "the directory vanished" is the more severe
    version of the bug this guards, not an exemption from it.
    """
    base = (root or _VENDOR_DIR) / _LIBS_DIR_NAME
    missing: dict[str, list[str]] = {}
    for plat, required in _REQUIRED_VENDORED_LIBS.items():
        absent = sorted(name for name in required if not (base / plat / name).is_file())
        if absent:
            missing[plat] = absent
    return missing


def _install_diskcache_stub() -> None:
    """Register a ``diskcache`` stub in ``sys.modules`` if the real one is absent.

    ``llama_cpp.llama_cache`` imports ``diskcache`` at module level, but
    KiroCrew never constructs a disk-backed LLM state cache (we only use
    embeddings). Registering a stub avoids adding the real dependency to the
    version set while never shadowing an actually-installed diskcache.
    """
    if "diskcache" in sys.modules or importlib.util.find_spec("diskcache") is not None:
        return
    stub = types.ModuleType("diskcache")

    class Cache:  # pragma: no cover - never constructed by KiroCrew
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "diskcache is stubbed out by kiro_crew.embeddings — disk-backed "
                "LLM state caching is not available"
            )

    stub.Cache = Cache  # type: ignore[attr-defined]
    stub.FanoutCache = Cache  # type: ignore[attr-defined]
    sys.modules["diskcache"] = stub


@functools.lru_cache(maxsize=1)
def _load_llama_class():
    """Import the vendored llama-cpp-python runtime. Returns the Llama class or None.

    Points ``LLAMA_CPP_LIB_PATH`` at the per-platform native libs (an
    upstream-supported override — see ``_vendor/llama_cpp/llama_cpp.py``)
    and prepends ``_vendor`` to ``sys.path`` so ``import llama_cpp``
    resolves to the vendored copy. Never raises — unsupported platforms and
    import failures degrade to keyword-only memory search.
    """
    libs_dirname = _platform_libs_dirname()
    if libs_dirname is None:
        logger.warning(
            "In-process embeddings unsupported on %s/%s — memory falls back to keyword search",
            sys.platform,
            platform.machine(),
        )
        return None
    libs_dir = _VENDOR_DIR / _LIBS_DIR_NAME / libs_dirname
    if not libs_dir.is_dir():
        logger.warning("Vendored llama.cpp libs missing at %s", libs_dir)
        return None
    # Name the missing FILES before ctypes reports only the base name it could
    # not find. The upstream loader raises "Shared library with base name
    # 'llama' not found", which reads as a broken platform rather than an
    # incomplete install and sent the real-world diagnosis of this bug chasing
    # a Graviton/architecture problem instead of the packaging rule that
    # dropped the file on every architecture.
    #
    # Skipped when the operator set LLAMA_CPP_LIB_PATH: the libs then load from
    # THEIR directory, so the bundled tree's contents no longer decide whether
    # the runtime works, and refusing on it would disable the documented escape
    # hatch (a GPU build, or a hand-restored lib dir) for precisely the users an
    # incomplete wheel stranded. Their directory is the loader's to validate.
    if _LIB_PATH_ENV not in os.environ:
        absent = [
            name
            for name in _REQUIRED_VENDORED_LIBS.get(libs_dirname, ())
            if not (libs_dir / name).is_file()
        ]
        if absent:
            logger.warning(
                "Vendored llama.cpp install for %s is incomplete — missing %s in %s. "
                "This is a packaging defect, not an unsupported platform; reinstall "
                "Kiro Crew from a current release, or point %s at a complete lib "
                "directory. Memory falls back to keyword search.",
                libs_dirname,
                ", ".join(absent),
                libs_dir,
                _LIB_PATH_ENV,
            )
            return None
    # setdefault so an operator-provided override (e.g. a GPU build) wins.
    os.environ.setdefault(_LIB_PATH_ENV, str(libs_dir))
    _install_diskcache_stub()
    vendor_str = str(_VENDOR_DIR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    try:
        from llama_cpp import Llama  # noqa: F811

        return Llama
    except Exception:
        logger.warning("Vendored llama-cpp-python failed to import", exc_info=True)
        return None


# ── Model paths ──


def models_dir() -> Path:
    """Directory where downloaded embedding models live (respects KIROCREW_HOME)."""
    return config_dir() / _MODELS_DIR_NAME


def default_model_path() -> Path:
    """On-disk path of the BUNDLED model (the CDN download target)."""
    return models_dir() / _GGUF_FILENAME


def _read_memory_config() -> dict:
    """Read the raw ``memory`` section of ``config.json``. Never raises.

    Read raw rather than through the config loader so the download thread and
    the backend factory never depend on the config dataclass import graph
    (which imports large parts of the package).
    """
    try:
        path = config_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            section = data.get("memory", {})
            if isinstance(section, dict):
                return section
    except Exception:
        logger.debug("Could not read the memory config section", exc_info=True)
    return {}


class CustomModelSpec(NamedTuple):
    """A user-configured local embedding model.

    ``error`` is non-empty when the configured path is unusable. Such a spec is
    still returned (rather than falling back to the bundled model) so a typo can
    never silently swap the user's vector space back to the default and trigger a
    full re-embed behind their back: embeddings simply stay unavailable, memory
    degrades to keyword search, and the reason is logged and surfaced by
    ``kirocrew doctor``.
    """

    path: Path
    model_id: str
    dim: int
    error: str


def _custom_model_id(path: Path, configured: str) -> str:
    """Stable vector-space identifier for a custom model.

    An explicit ``memory.embed_model_id`` always wins. Otherwise it is derived
    from the file's name and byte size, which is free to compute and changes
    when a genuinely different model is dropped in. It deliberately does NOT
    hash the file: a sha256 over ~600MB on every boot buys almost nothing here.
    The tradeoff is that swapping in a different model of IDENTICAL byte size
    will not be detected — set ``memory.embed_model_id`` explicitly if you do
    that.
    """
    if configured:
        return configured
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return f"custom:{path.name}:{size}"


def _is_sensitive_model_path(path: Path) -> bool:
    """True when *path* is a credential store or the governance trust-root.

    Failing CLOSED on an evaluation error is deliberate — a gate that cannot be
    evaluated must not silently widen what this knob is allowed to open.
    """
    try:
        return is_sensitive_path(str(path))
    except Exception:
        # The path is deliberately NOT logged. These are the two sites where the
        # value is, by this gate's own determination, likely to name a credential
        # store or the governance trust-root, and a gateway log may be shipped or
        # shared. The exact path stays available on demand: `kirocrew doctor`
        # prints it, the dashboard returns it in `setup_error`, and the resolver
        # reports it through CustomModelSpec.error.
        logger.warning(
            "Could not evaluate the sensitive-path gate for the configured custom "
            "embedding model — refusing it. Run 'kirocrew doctor' for the path.",
            exc_info=True,
        )
        return True


def validate_custom_model_path(raw: str, origin: str = "embed_model_path") -> tuple[Path, str, str]:
    """Validate a candidate model path. Returns ``(resolved_path, error, code)``.

    ``error`` is empty when the path is usable; ``code`` is a stable
    lower_snake identifier for it. The prose is advisory (it carries the concrete
    path and size), the code is the contract — the dashboard renders a LOCALIZED
    message keyed on it, so returning prose alone would be untranslatable by
    construction.

    Split out of :func:`resolve_custom_model` so a request handler can validate a
    path the user just typed WITHOUT writing it to config first — the dashboard
    needs to reject a bad path before it is persisted, and the resolver only ever
    reads what is already on disk.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return path, f"{origin} must be an absolute path (got {raw!r})", "model_path_not_absolute"
    if _is_sensitive_model_path(path):
        # Same gate every other file-access surface uses (hooks, artifacts,
        # dashboard file I/O, knowledge indexing). A GGUF is handed to native
        # llama.cpp to mmap and parse, so pointing this knob at a credential
        # store or the governance trust-root would feed those bytes to native
        # code. Refuse by path rather than relying on the parse to fail.
        return path, f"{origin} points at a protected location: {path}", "model_path_protected"
    if not path.exists():
        return (
            path,
            f"{origin} points at a file that does not exist: {path}",
            "model_path_not_found",
        )
    if not path.is_file():
        return path, f"{origin} is not a regular file: {path}", "model_path_not_a_file"
    try:
        size = path.stat().st_size
        if size <= _GGUF_MIN_BYTES:
            return (
                path,
                f"{origin} is too small to be model weights ({size} bytes): {path}",
                "model_path_too_small",
            )
    except OSError as exc:
        return path, f"{origin} could not be read: {exc}", "model_path_unreadable"
    return path, "", ""


def bundled_embedding_dim() -> int:
    """Output width of the bundled model, for reverting to it.

    Exposed so the dashboard does not duplicate the literal: a future change to
    the bundled model would otherwise leave a stale 1024 in the handler and make
    every revert-to-bundled write an unloadable config.
    """
    return _DEFAULT_DIM


def build_gated_candidate(path: Path) -> LlamaCppEmbedder:
    """A GATED candidate for ``path`` that adopts the model's own width.

    Two properties make a live model change safe, and both are why this is not
    just ``LlamaCppEmbedder(path)``:

    - **Adopt-dim** (``dim=0``) lets the candidate be loaded exactly ONCE. The
      alternative — load it to read ``n_embd``, free it, let the factory load it
      again — peaks at two ~700MB models, and :func:`get_shared_embedder` states
      outright that "loading it twice is never acceptable".
    - **Gated** (``serving=False``) means it occupies the singleton slot while it
      loads but hands out no vectors. That closes both halves of the swap window:
      an empty slot would let a status poll rebuild the OUTGOING model (two
      models resident), while an ungated one would let a query be embedded in the
      new space and compared against vectors the store has not reconciled yet.

    ``model_id`` is derived here, not by the caller, so the vector-space
    signature matches what :func:`resolve_custom_model` would compute for the
    same file — otherwise a real model change could look like no change.
    """
    return LlamaCppEmbedder(
        model_path=path, dim=0, model_id=_custom_model_id(path, ""), serving=False
    )


def build_gated_bundled() -> LlamaCppEmbedder:
    """A GATED candidate for the BUNDLED model, for reverting off a custom one.

    Gated for the same reason the custom candidate is: until the store has been
    reconciled to the bundled space, a query embedded at the bundled width can
    reach a FAISS index still built at the custom width. Its width is known, so
    there is nothing to adopt — but the file can still be ABSENT (the bundled
    GGUF is a download), which is why the revert must also prove the model loads
    before its config is persisted.
    """
    # Every argument is explicit ON PURPOSE. The constructor defaults
    # model_path to active_model_path(), which resolves the STILL-CONFIGURED
    # custom path — so omitting it would load the custom model here and label it
    # with the bundled model_id, stamping custom vectors as bundled and keeping
    # them across restarts.
    return LlamaCppEmbedder(
        model_path=default_model_path(), dim=_DEFAULT_DIM, model_id=_MODEL_ID, serving=False
    )


def activate_shared_embedder() -> bool:
    """Open the serving gate on the installed backend. True if it was gated.

    Separated from installation so the caller can order it AFTER the config
    write and the store reconcile — the point of the gate is that nothing
    between those two steps can serve a vector from the new space.
    """
    with _shared_embedder_lock:
        current = _shared_embedder
    activate = getattr(current, "activate", None)
    if callable(activate):
        activate()
        return True
    return False


def _retire_backend(backend: EmbeddingBackend) -> None:
    """Terminally unload ``backend``. Falls back to close() for other runtimes."""
    retire = getattr(backend, "retire", None)
    if callable(retire):
        retire()
    else:
        backend.close()


def embedding_backend_serving() -> bool:
    """Whether the installed backend is serving vectors (not a gated candidate).

    Lets the apply path tell "I failed before activation, roll back" apart from
    "I failed after activation, the new model is already the live one" — the two
    need opposite recovery.
    """
    with _shared_embedder_lock:
        current = _shared_embedder
    if current is None:
        return False
    return bool(getattr(current, "_serving", True))


def install_shared_embedder(embedder: EmbeddingBackend) -> None:
    """Swap the singleton to ``embedder``, closing the outgoing one first.

    Closing and installing under one lock is what keeps peak residency at a
    single model. Installing BEFORE the new model has loaded is deliberate: the
    dashboard polls the status endpoint every ~2s, and that calls
    :func:`get_shared_embedder`, so leaving the slot empty would let a poll
    rebuild the OUTGOING model from config and put two models in memory after
    all. A poll that arrives mid-load instead sees the incoming embedder
    reporting not-ready, which is exactly what the UI should show.
    """
    global _shared_embedder
    with _shared_embedder_lock:
        outgoing = _shared_embedder
        _shared_embedder = embedder
    if outgoing is not None and outgoing is not embedder:
        # Outside the lock: the unload joins the inference thread, and holding
        # the singleton lock across a join would stall every concurrent reader.
        _retire_backend(outgoing)


def resolve_custom_model() -> "CustomModelSpec | None":
    """Resolve the user-configured local model, or ``None`` to use the bundled one.

    Resolution order for the path: ``KIROCREW_EMBED_MODEL_PATH`` env, then the
    ``memory.embed_model_path`` config knob. Returning ``None`` means "no custom
    model configured" — it never means "configured but broken", which is
    reported via :attr:`CustomModelSpec.error` instead.
    """
    memory_cfg = _read_memory_config()
    raw = os.environ.get(_MODEL_PATH_ENV, "").strip()
    origin = _MODEL_PATH_ENV
    if not raw:
        raw = str(memory_cfg.get("embed_model_path", "") or "").strip()
        origin = "memory.embed_model_path"
    if not raw:
        return None

    dim = _DEFAULT_DIM
    raw_dim = memory_cfg.get("embedding_dim")
    if isinstance(raw_dim, int) and not isinstance(raw_dim, bool) and raw_dim > 0:
        dim = raw_dim
    configured_id = str(memory_cfg.get("embed_model_id", "") or "").strip()

    path, error, _code = validate_custom_model_path(raw, origin)
    _log_custom_model_error(error)
    return CustomModelSpec(path, _custom_model_id(path, configured_id), dim, error)


# Last error reported by resolve_custom_model(), so a persistent misconfiguration
# is logged once instead of on every call. The resolver is invoked from hot-ish
# paths — model_file_present() runs it, and the dashboard polls the embedding
# status endpoint every ~2s — so logging unconditionally would flood the log for
# as long as the path stays broken. A CHANGED message still logs, and a resolve
# that succeeds clears the state, so a fix-then-break cycle is not swallowed.
_last_custom_model_error = ""


def _log_custom_model_error(error: str) -> None:
    global _last_custom_model_error
    if error != _last_custom_model_error:
        _last_custom_model_error = error
        if error:
            logger.error("Custom embedding model unusable — %s", error)


def embedding_model_is_custom() -> bool:
    """True when a custom model path is configured (valid or not).

    Gates the default-model download: a configured custom model must never be
    replaced by the bundled one, and a BROKEN custom path must not silently
    fall back to it either.
    """
    return resolve_custom_model() is not None


def active_model_path() -> Path:
    """Path of the model actually in use — the custom one when configured."""
    spec = resolve_custom_model()
    return spec.path if spec is not None else default_model_path()


def model_file_present(path: Path | None = None) -> bool:
    """True when the GGUF exists and is not a truncated/placeholder file.

    Defaults to :func:`active_model_path`, so callers asking "is the embedding
    model on disk?" get an answer about the model actually in use. Callers that
    specifically mean the bundled download target (the download manager) pass
    :func:`default_model_path` explicitly.
    """
    target = path or active_model_path()
    try:
        return target.is_file() and target.stat().st_size > _GGUF_MIN_BYTES
    except OSError:
        return False


def embedding_space_signature(model_id: str, dim: int) -> str:
    """Short signature of a vector space: vectors from different sigs are incomparable.

    Single source of truth so vector memory and the knowledge library cannot
    disagree about whether stored vectors are still valid. The knowledge
    library folds this model identity into its own per-item signature (which
    additionally covers its content budget); vector memory compares it against
    the signature the database was last embedded under.
    """
    return hashlib.sha256(f"{model_id}|{dim}".encode()).hexdigest()[:16]


def default_embedding_space_signature() -> str:
    """Signature of the BUNDLED model's vector space.

    Every vector stored before custom-model support existed was produced by the
    bundled model at :data:`_DEFAULT_DIM` — the embedder was always constructed
    with no arguments, so no other identity was reachable. That makes this the
    provable identity of any un-versioned vector on disk, which is what lets
    :meth:`~kiro_crew.vector_memory.VectorMemoryStore.reconcile_embedding_space`
    decide whether such vectors belong to the space now in use.

    Keyed on the bundled constants rather than on config, so it stays correct
    however the active backend was selected — config knob, env var, or a
    programmatic :func:`register_embedding_backend`.
    """
    return embedding_space_signature(_MODEL_ID, _DEFAULT_DIM)


def active_embedding_space_signature() -> str:
    """Signature of the vector space the ACTIVE backend produces.

    Available without loading the model: ``model_id`` and ``dim`` are set when
    the backend is constructed, so this is cheap enough to call on any startup
    path.
    """
    backend = get_shared_embedder()
    return embedding_space_signature(backend.model_id, backend.dim)


def store_embedding_space_is_stale(store: "_ReconcilableStore") -> bool:
    """True when *store*'s vectors were NOT produced by the active backend.

    Read-only counterpart to :func:`reconcile_store_embedding_space`, for callers
    that must detect the condition without mutating. An unrecorded space is
    treated as the bundled model's, which is provable: nothing else could have
    written those vectors before space tracking existed.
    """
    recorded = store.recorded_embedding_space() or default_embedding_space_signature()
    return recorded != active_embedding_space_signature()


class ReembedProgress:
    """Live progress of a background re-embed, for the dashboard indicator.

    An in-memory singleton mirroring :attr:`ModelDownloadManager.status`: a plain
    dict reassigned per phase and read live by the status endpoint, which the
    Memory tab already polls. Deliberately NOT a durable job row like the
    knowledge library's ``ingestion_jobs`` table — a re-embed is a single
    at-a-time operation tied to process lifetime, with no id to hand out and no
    history to query, and an interrupted run is resumed by the next boot sweep.

    ``step`` is one of ``idle`` / ``applying`` / ``running`` / ``done`` /
    ``failed``. The indicator needs the distinction: "applying" (loading the new
    model, no counts yet) reads very differently from "running 1842/3148".
    """

    def __init__(self) -> None:
        self._step = "idle"
        self._done = 0
        self._total = 0
        self._error = ""
        self._lock = threading.Lock()

    def begin_apply(self) -> None:
        with self._lock:
            self._step, self._done, self._total, self._error = "applying", 0, 0, ""

    def begin_run(self, total: int) -> None:
        with self._lock:
            self._step, self._done, self._total, self._error = "running", 0, max(0, total), ""

    def advance(self, done: int, total: int) -> None:
        """Report progress. Cheap enough to call per row (four field writes)."""
        with self._lock:
            self._step, self._done, self._total = "running", max(0, done), max(0, total)

    def finish(self, done: int) -> None:
        with self._lock:
            self._step = "done"
            self._done = max(0, done)
            self._total = max(self._total, self._done)
            self._error = ""

    def fail(self, error: str) -> None:
        with self._lock:
            self._step, self._error = "failed", error

    def snapshot(self) -> dict[str, object]:
        """The wire shape the status endpoint embeds."""
        with self._lock:
            return {
                "step": self._step,
                "done": self._done,
                "total": self._total,
                "error": self._error,
            }

    def is_active(self) -> bool:
        """True while an apply/re-embed is in flight — used to single-flight applies."""
        with self._lock:
            return self._step in ("applying", "running")


_reembed_progress = ReembedProgress()


def reembed_progress() -> ReembedProgress:
    """Process-wide re-embed progress singleton."""
    return _reembed_progress


def reconcile_store_embedding_space(store: "_ReconcilableStore") -> int:
    """Reconcile *store* against the active embedding space. Returns rows invalidated.

    THE single chokepoint for destructive reconciliation. Startup paths that
    open a vector store either reconcile through this one function (the gateway
    boot sweep, the onboarding importer) or, where clearing is unsafe, use the
    non-mutating ``store_embedding_space_is_stale`` probe instead — ``kirocrew
    run`` (``cli_server``) takes the latter path, disabling embedding for the
    run and deferring reconciliation to the gateway. Concentrating the
    destructive path here keeps a future entry point from loading a FAISS index
    built under the previous model and scoring it against new-model query
    vectors.

    Refuses to clear when the active backend is not ready and is not the bundled
    model: clearing is destructive, and a backend that cannot load — a rejected
    custom path yields exactly that — could never regenerate what was cleared, so
    a single typo in ``memory.embed_model_path`` would otherwise cost the user
    every stored vector. Callers that legitimately want the clear (the gateway
    sweep) wait for readiness first. :func:`store_embedding_space_is_stale` is the
    non-mutating probe for callers that cannot.

    Callers that can also re-embed should follow this with
    ``backfill_missing_embeddings()``; a one-shot CLI deliberately does not, so
    the affected rows stay keyword-searchable until the gateway's sweep refills
    them.
    """
    active = active_embedding_space_signature()
    if active != default_embedding_space_signature() and not get_shared_embedder().is_ready():
        logger.info(
            "Skipping embedding-space reconciliation: the active backend is not "
            "ready, so clearing stored vectors would leave nothing able to "
            "re-embed them"
        )
        return 0
    return store.reconcile_embedding_space(
        active, clear_when_unknown=active != default_embedding_space_signature()
    )


# ── Embedding backend interface ──


class EmbeddingBackend(abc.ABC):
    """Abstract embedding runtime.

    The swap seam for future runtimes (Ollama again, a remote endpoint, ONNX)
    and for user-defined models. Consumers (vector memory, knowledge library)
    depend only on this surface; everything llama.cpp-specific lives in
    :class:`LlamaCppEmbedder`. To swap runtimes: implement this ABC and return
    it from :func:`get_shared_embedder`.

    Contract:

    - ``embed``/``embed_batch`` return ``None`` on ANY failure — callers treat
      ``None`` as "no embedding available" and degrade to keyword search.
    - ``model_id`` + ``dim`` identify the vector space. Vectors produced under
      a different ``model_id`` or ``dim`` are incomparable — a swap requires
      re-embedding stored vectors (the knowledge library's sig-gated rebuild
      keys off :func:`kiro_crew.knowledge.embedder.embed_signature`, which
      folds ``model_id`` in; vector memory re-embeds via ``migrate``).
    - Implementations must be thread-safe (callers invoke from worker threads).
    """

    @property
    @abc.abstractmethod
    def model_id(self) -> str:
        """Stable identifier of the model producing vectors (feeds staleness sigs)."""

    @property
    @abc.abstractmethod
    def dim(self) -> int:
        """Output vector dimensionality."""

    @abc.abstractmethod
    def is_ready(self) -> bool:
        """True when the backend can produce vectors right now (model loaded)."""

    @abc.abstractmethod
    def embed(self, text: str) -> "list[float] | None":
        """Embed a single text. Returns None on any failure."""

    @abc.abstractmethod
    def embed_batch(self, texts: "list[str]") -> "list[list[float]] | None":
        """Embed multiple texts. Returns None on any failure."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources (unload model). Safe to call repeatedly."""


# ── In-process llama.cpp backend ──


class _InferJob:
    """One ``create_embedding`` call handed to the embedder's worker thread."""

    __slots__ = ("llm", "texts", "result", "error", "done")

    def __init__(self, llm: object, texts: "list[str]") -> None:
        self.llm = llm
        self.texts = texts
        self.result: object | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()


class LlamaCppEmbedder(EmbeddingBackend):
    """Serialized in-process embedder over the vendored llama.cpp runtime.

    The underlying ``Llama`` object is NOT thread-safe, so every load and
    inference call is serialized behind ``_lock``. All failures return
    ``None`` (graceful degradation) — callers already treat ``None`` as
    "no embedding available".

    Inference does not run on the caller's thread. llama.cpp builds a compute
    thread pool (``n_threads_batch``, which defaults to the CPU count) the
    first time a GIVEN thread runs inference, and that pool lives as long as
    its calling thread does. Embed calls arrive on long-lived executor threads
    (``mc-embed``, asyncio's default executor, cron, maintenance), so running
    inference inline would accumulate one CPU-count-sized pool per caller
    thread (a busy gateway can reach dozens of pools and well over a thousand
    threads within an hour). Every call is therefore forwarded to ONE owned
    daemon thread (``kc-embed-infer``), so the process holds exactly one
    compute pool no matter how many threads embed. ``_lock`` already serializes
    inference, so the hand-off adds no extra queueing.
    """

    def __init__(
        self,
        model_path: Path | None = None,
        dim: int = _DEFAULT_DIM,
        model_id: str = _MODEL_ID,
        serving: bool = True,
    ):
        self._model_path = model_path or active_model_path()
        self._dim = dim
        self._model_id = model_id
        self._llm: object | None = None
        # Gate: a candidate installed by a model change LOADS but serves nobody
        # until activate(). Returning None from embed* is the ABC's documented
        # "no embedding available" state, so callers degrade to keyword search
        # exactly as they do before any model has loaded.
        self._serving = serving
        # Permanent. close() is terminal, so a consumer still holding a
        # swapped-out embedder cannot resurrect its ~700MB model by calling
        # embed_batch() (which would otherwise kick a fresh background load).
        self._closed = False
        self._load_failed_at: float = 0.0
        self._lock = threading.Lock()  # serializes inference (Llama is not thread-safe)
        self._load_lock = threading.Lock()  # guards loader-thread spawn state
        self._load_thread: threading.Thread | None = None
        # Single owned inference thread + its job queue (see the class docstring).
        self._jobs: "queue.SimpleQueue[_InferJob | None]" = queue.SimpleQueue()
        self._infer_thread: threading.Thread | None = None

    @property
    def model_path(self) -> Path:
        return self._model_path

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def is_ready(self) -> bool:
        """True when the model is loaded in memory.

        Deliberately reports LOAD state, not serving state: reconciliation must
        be able to confirm the new model actually loaded before it clears the old
        vector space, and the status endpoint should show the model as present
        while the re-embed runs. The serving gate is enforced in ``embed_batch``,
        which is the path that could actually mix vector spaces.
        """
        return self._llm is not None

    def activate(self) -> None:
        """Open the serving gate. Idempotent.

        Called only after the new model's path+dim are persisted and the store's
        vector space has been reconciled, so the first vector this backend hands
        out can never be compared against un-reconciled old-model vectors.
        """
        self._serving = True

    def _kick_background_load(self) -> None:
        """Start loading the model on a daemon thread. Idempotent, returns fast.

        The GGUF load takes seconds — it must NEVER run on a caller's thread
        (several call paths sit on the asyncio event loop). Until the load
        lands, ``embed*`` returns ``None`` and callers degrade to keyword
        search; the existing lazy-rebind / availability-recheck machinery
        picks embeddings up on a later call.
        """
        with self._load_lock:
            if self._closed:
                return
            if self._llm is not None:
                return
            if self._load_thread is not None and self._load_thread.is_alive():
                return
            # A failed load (corrupt file, bad native libs) is retried only
            # after a cooldown so a broken state can't spawn a loader thread
            # per embed call.
            if (
                self._load_failed_at
                and time.monotonic() - self._load_failed_at < _LLM_LOAD_RETRY_SECS
            ):
                return
            if not model_file_present(self._model_path):
                # Not a failure — the background download may still be in flight.
                return
            if _is_sensitive_model_path(self._model_path):
                # THE security boundary: below this line the path is handed to
                # native llama.cpp to open and mmap. resolve_custom_model()
                # already reports a protected path via CustomModelSpec.error, but
                # that is a diagnostic, not enforcement — this class can be
                # constructed with an explicit path (and model_file_present() is
                # passed one here, so it does not re-derive the active path).
                # Refuse here so no construction route can feed a credential
                # store or the governance trust-root to native code.
                logger.error(
                    "Refusing to load an embedding model from a protected location. "
                    "Run 'kirocrew doctor' for the path."
                )
                self._load_failed_at = time.monotonic()
                return
            self._load_thread = threading.Thread(
                target=self._load_model, name="kc-embed-load", daemon=True
            )
            self._load_thread.start()

    def _load_model(self) -> None:
        """Loader-thread body: build the Llama object and publish it."""
        llama_cls = _load_llama_class()
        if llama_cls is None:
            self._load_failed_at = time.monotonic()
            return
        try:
            started = time.monotonic()
            llm = llama_cls(
                model_path=str(self._model_path),
                embedding=True,
                pooling_type=_POOLING_TYPE_LAST,
                n_ctx=_N_CTX,
                n_batch=_N_CTX,
                n_ubatch=_N_CTX,
                verbose=False,
            )
            # Validate the model's REAL output width against the configured dim
            # before publishing it. Without this a custom model whose dim does
            # not match memory.embedding_dim loads fine and then every embed
            # call trips embed_batch's dim guard and returns None — indefinite
            # silent keyword-only search with nothing explaining why. Fail the
            # load instead, with the number the operator has to fix.
            actual_dim = self._probe_dim(llm)
            if self._dim <= 0:
                # ADOPT mode (dim<=0): the caller does not yet know the width and
                # wants the model to report it — used by the dashboard's model
                # change, which must learn the width from the model itself
                # WITHOUT loading it a second time just to ask. Boot still uses a
                # concrete configured dim, so the loud mismatch refusal below is
                # unchanged for every non-adopt caller.
                #
                # A width we could NOT read is a failed load, never a published
                # model: dim 0 would be written nowhere (the config writer skips
                # non-positive), set_embedding_dim(0) is refused, and embed_batch
                # would then reject every vector it produced against an expected
                # width of zero — reconciliation having already cleared the old
                # ones. Fail loudly instead of adopting an unknown width.
                if actual_dim is None or actual_dim <= 0:
                    logger.error(
                        "Embedding model %s did not report an embedding width — "
                        "refusing to load (it may not be an embedding model).",
                        self._model_path.name,
                    )
                    self._load_failed_at = time.monotonic()
                    self._llm = None
                    return
                self._dim = actual_dim
            elif actual_dim is not None and actual_dim != self._dim:
                logger.error(
                    "Embedding model %s produces %d-dim vectors but memory.embedding_dim "
                    "is %d — refusing to load. Set memory.embedding_dim to %d "
                    "(changing it re-embeds stored vectors).",
                    self._model_path.name,
                    actual_dim,
                    self._dim,
                    actual_dim,
                )
                self._load_failed_at = time.monotonic()
                self._llm = None
                return
            logger.info(
                "Loaded embedding model %s in %.1fs",
                self._model_path.name,
                time.monotonic() - started,
            )
            if self._closed:
                # close() ran while this load was in flight (a timed-out or
                # rolled-back model change). Publishing now would put a ~700MB
                # model back into an embedder nobody holds a reference to except
                # a stale consumer. Drop it instead.
                closer = getattr(llm, "close", None)
                if callable(closer):
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 - freeing must not propagate
                        logger.debug("Freeing abandoned model failed", exc_info=True)
                return
            self._llm = llm  # atomic publish (GIL)
        except Exception:
            logger.warning("Failed to load embedding model %s", self._model_path, exc_info=True)
            self._load_failed_at = time.monotonic()
            self._llm = None

    @staticmethod
    def _probe_dim(llm: object) -> int | None:
        """Read a loaded model's embedding width, or None when unavailable.

        ``n_embd()`` is llama.cpp's own accessor. Returning None on anything
        unexpected keeps the check advisory — a runtime that does not expose it
        must not be blocked from loading.
        """
        try:
            n_embd = getattr(llm, "n_embd", None)
            if not callable(n_embd):
                return None
            value = int(n_embd())
            return value if value > 0 else None
        except Exception:
            logger.debug("Could not probe embedding dim", exc_info=True)
            return None

    def _infer_loop(self, jobs: "queue.SimpleQueue[_InferJob | None]") -> None:
        """Worker body: run queued ``create_embedding`` calls, one at a time.

        ``jobs`` is passed in rather than read from ``self`` so this worker is
        bound for life to the queue it was started with. That is what makes a
        straggler harmless: it can only ever drain — and exit on the sentinel
        from — its own queue, never one feeding a later worker.

        Every job is completed even on failure, so a caller can never be left
        waiting on ``job.done``. ``None`` is the shutdown sentinel from
        :meth:`close`; exiting the thread is what releases llama.cpp's compute
        pool.
        """
        while True:
            job = jobs.get()
            if job is None:
                return
            try:
                job.result = job.llm.create_embedding(job.texts)  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller verbatim
                job.error = exc
            finally:
                job.done.set()

    def _create_embedding(self, llm: object, texts: "list[str]") -> object:
        """Run one ``create_embedding`` on the owned thread; re-raise its error.

        Caller must hold ``_lock``, which keeps at most one job in flight.
        The wait is unbounded, matching the previous inline call — a wedged
        native inference blocked the caller then too.
        """
        thread = self._infer_thread
        if thread is None or not thread.is_alive():
            # New worker, new queue. A straggler left behind by a timed-out
            # close() join keeps draining its OWN queue, so it can neither
            # consume this worker's jobs nor eat this worker's future sentinel.
            self._jobs = queue.SimpleQueue()
            thread = threading.Thread(
                target=self._infer_loop, args=(self._jobs,), name="kc-embed-infer", daemon=True
            )
            self._infer_thread = thread
            thread.start()
        jobs = self._jobs
        job = _InferJob(llm, texts)
        jobs.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error
        return job.result

    def embed(self, text: str) -> list[float] | None:
        """Embed a single text. Returns None on any failure."""
        result = self.embed_batch([text])
        return result[0] if result else None

    def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """Embed multiple texts. Returns None on any failure (incl. model not loaded yet).

        Never blocks on the model load: when the model isn't in memory yet this
        kicks a background load and returns ``None`` immediately. Inference on a
        loaded model is serialized behind ``_lock`` (tens of ms per short text).
        """
        if not texts or not any(t.strip() for t in texts):
            return None
        if self._closed or not self._serving:
            # Closed: terminal, never reload. Not serving: a candidate mid-swap,
            # whose vectors would be in a space the store has not reconciled to.
            return None
        llm = self._llm
        if llm is None:
            self._kick_background_load()
            return None
        clipped: list[str] = []
        for t in texts:
            if len(t) > _MAX_EMBED_CHARS:
                logger.debug("Truncating embed input %d -> %d chars", len(t), _MAX_EMBED_CHARS)
                t = t[:_MAX_EMBED_CHARS]
            clipped.append(t)
        with self._lock:
            try:
                resp = self._create_embedding(llm, clipped)
                vectors = [item["embedding"] for item in resp["data"]]  # type: ignore[index]
            except Exception:
                logger.debug("In-process embed failed", exc_info=True)
                return None
        if len(vectors) != len(clipped) or not vectors or not vectors[0]:
            logger.warning(
                "Unexpected embedding response (got %d vectors for %d texts)",
                len(vectors),
                len(clipped),
            )
            return None
        if len(vectors[0]) != self._dim:
            # A mismatched dimension would crash or silently corrupt the
            # fixed-dim FAISS index downstream — fail per the EmbeddingBackend
            # contract (None → graceful keyword-only degradation).
            logger.warning(
                "Embedding dim mismatch: model produced %d, expected %d",
                len(vectors[0]),
                self._dim,
            )
            return None
        return vectors

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until the model is loaded (or the load fails/times out).

        For sync contexts that legitimately want to wait — tests and one-shot
        CLI flows. Kicks the background load if not already running. Returns
        ``is_ready()``. Never call from an event-loop thread.
        """
        self._kick_background_load()
        with self._load_lock:
            thread = self._load_thread
        if thread is not None:
            thread.join(timeout)
        return self.is_ready()

    def retire(self) -> None:
        """Terminal unload, for an embedder being swapped out of the singleton.

        Distinct from :meth:`close`, which is a REUSABLE unload (it clears the
        failure cooldown so a later ``embed*`` can retry the load — see
        ``test_close_unloads_and_resets_cooldown``). That reusability is a hazard
        for a swapped-out embedder specifically: a consumer that captured the old
        backend before the swap could call ``embed_batch()`` and silently bring
        its ~700MB model back, putting two models in memory and producing vectors
        in the space the store just moved away from. Retiring marks it dead first,
        so the load can never restart.
        """
        self._closed = True
        self.close()

    def close(self) -> None:
        """Unload the model (frees ~700MB RSS). Safe to call repeatedly.

        Also stops the inference thread: llama.cpp's compute pool is owned by
        that thread, so it is only released when the thread exits.
        """
        with self._lock, self._load_lock:
            self._llm = None
            self._load_failed_at = 0.0
            self._load_thread = None
            thread, self._infer_thread = self._infer_thread, None
            if thread is not None and thread.is_alive():
                # Holding _lock means no job can be queued behind this sentinel.
                # If the join times out, the straggler stays parked on THIS
                # queue, which the next worker will not share (see
                # _create_embedding), so the sentinel it eventually consumes is
                # still its own.
                self._jobs.put(None)
                thread.join(_INFER_STOP_TIMEOUT_SECS)


_shared_embedder: EmbeddingBackend | None = None
_shared_embedder_lock = threading.Lock()
_backend_factory: "Callable[[], EmbeddingBackend] | None" = None


def register_embedding_backend(factory: "Callable[[], EmbeddingBackend] | None") -> None:
    """Override the embedding backend (swap seam for other runtimes/models).

    Pass a factory returning an :class:`EmbeddingBackend`; the next
    :func:`get_shared_embedder` call constructs through it. Pass ``None`` to
    restore the default (vendored llama.cpp + bundled Qwen3 model). Call
    :func:`reset_shared_embedder` after registering so an already-built
    singleton is replaced. NOTE: a backend with a different ``model_id`` or
    ``dim`` produces incomparable vectors — stored embeddings must be
    re-embedded (knowledge: sig-gated rebuild; memory: migrate/re-embed).
    """
    global _backend_factory
    with _shared_embedder_lock:
        _backend_factory = factory


def default_embedding_backend() -> EmbeddingBackend:
    """Build the backend the config asks for: a custom local model, or the bundled one.

    This is the default factory behind :func:`get_shared_embedder`, so a custom
    model reaches every consumer (vector memory, knowledge library, the MCP
    server process) without any of them knowing it exists.
    :func:`register_embedding_backend` still overrides it for other runtimes.
    """
    spec = resolve_custom_model()
    if spec is None:
        return LlamaCppEmbedder()
    # A broken custom path is still honoured as "custom": the embedder will not
    # load, embeddings stay unavailable, and memory degrades to keyword search.
    # Falling back to the bundled model here would silently swap the vector
    # space and re-embed everything — a far worse failure than no embeddings.
    if spec.error:
        # Do not point the embedder at a rejected path. For a protected path
        # this matters beyond tidiness: the file may well exist and be large
        # enough to pass model_file_present(), so handing it over would leave
        # only LlamaCppEmbedder's own gate between it and llama.cpp. Keep the
        # custom identity (so the vector space is still recognised as
        # non-bundled) but give it a path that cannot resolve to a real file.
        logger.error(
            "Custom embedding model rejected (%s) — embeddings unavailable, "
            "memory falls back to keyword search",
            spec.error,
        )
        return LlamaCppEmbedder(
            model_path=models_dir() / _REJECTED_MODEL_SENTINEL,
            dim=spec.dim,
            model_id=spec.model_id,
        )
    logger.info(
        "Using custom embedding model %s (id=%s, dim=%d)", spec.path, spec.model_id, spec.dim
    )
    return LlamaCppEmbedder(model_path=spec.path, dim=spec.dim, model_id=spec.model_id)


def get_shared_embedder() -> EmbeddingBackend:
    """Process-wide embedder singleton.

    Vector memory and the knowledge library share one loaded model — the
    GGUF costs ~700MB RSS, so loading it twice is never acceptable.
    """
    global _shared_embedder
    with _shared_embedder_lock:
        if _shared_embedder is None:
            _shared_embedder = (_backend_factory or default_embedding_backend)()
        return _shared_embedder


def reset_shared_embedder() -> None:
    """Drop the singleton (tests, disable-embeddings, KIROCREW_HOME changes)."""
    global _shared_embedder
    with _shared_embedder_lock:
        if _shared_embedder is not None:
            _retire_backend(_shared_embedder)
        _shared_embedder = None


# ── Model download manager ──


async def _run_download_on_daemon_thread(fn: "Callable[[], tuple[bool, str]]") -> tuple[bool, str]:
    """Run the blocking download step on a DAEMON thread, awaitable from asyncio.

    Deliberately not ``run_in_executor``: both the loop's default executor and
    ``ThreadPoolExecutor`` use non-daemon threads that are joined at interpreter
    exit — an in-flight 610MB download would hang a Ctrl-C or a finished
    one-shot CLI for up to the HTTP timeout. A daemon thread is abandoned at
    exit instead (the orphaned staging file is unlinked on the next attempt).
    Cancelling the awaiting task detaches from — but does not interrupt — the
    thread; the manager's asyncio lock prevents a second concurrent download.
    """
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    result: list[tuple[bool, str]] = []

    def _worker() -> None:
        try:
            result.append(fn())
        except Exception as exc:  # pragma: no cover - fn already catches
            result.append((False, f"download thread crashed: {exc}"))
        finally:
            loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_worker, name="kc-model-download", daemon=True).start()
    await done.wait()
    return result[0]


_SSL_CA_PATHS = (
    "/etc/pki/tls/certs/ca-bundle.crt",  # AL2, RHEL, CentOS
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/ssl/cert.pem",  # macOS, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora
)


def _make_ssl_context() -> ssl.SSLContext:
    """Create an SSL context that finds system CA certs on all supported platforms.

    Bundled Python runtimes (like the PyInstaller desktop backend) may not ship
    their own CA bundle and rely on ``ssl.SSLContext.load_default_certs()`` which
    calls OpenSSL's defaults — those can miss when the compiled-in cert path
    doesn't match the host OS (common on AL2 with cross-compiled Python).
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
        # Verify the defaults work by checking the cert store has entries
        stats = ctx.cert_store_stats()
        if stats["x509_ca"] > 0:
            return ctx
    except ssl.SSLError:
        pass
    # Fallback: try well-known system CA bundle paths
    for path in _SSL_CA_PATHS:
        if os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
            return ctx
    # Last resort: honor SSL_CERT_FILE / SSL_CERT_DIR env if set
    return ctx


def _resolve_model_url() -> str:
    """Resolve the model download URL: env > config knob > CDN default.

    The ``KIROCREW_EMBED_MODEL_URL`` env var wins (mirrored/airgapped
    deployments), then a non-empty ``memory.embed_model_url`` in
    ``config.json``, then the public CDN default. The config file is read
    raw (not via the full loader) so the download thread never depends on
    the config dataclass import graph. Overrides must be ``https://`` —
    other schemes (``file://``, ``http://``) are rejected so an
    operator-controlled value can't read local files or fetch plaintext.
    Whatever the source, the download is only trusted after the streaming
    sha256 matches ``_GGUF_SHA256``.
    """
    env_url = os.environ.get(_MODEL_URL_ENV, "").strip()
    if env_url:
        if env_url.lower().startswith("https://"):
            return env_url
        logger.warning(
            "%s must be an https:// URL — ignoring the override and using " "the CDN default",
            _MODEL_URL_ENV,
        )
    cfg_url = str(_read_memory_config().get("embed_model_url", "") or "").strip()
    if cfg_url:
        if cfg_url.lower().startswith("https://"):
            return cfg_url
        logger.warning(
            "memory.embed_model_url must be an https:// URL — ignoring "
            "the override and using the CDN default",
        )
    return _DEFAULT_MODEL_URL


def redact_model_url(url: str) -> str:
    """Return *url* safe for logs/terminal: strip userinfo, query, fragment.

    A private-mirror override may carry credentials in userinfo or a signed
    query string (e.g. presigned URLs). Only scheme + host + path are ever
    logged or printed; the full URL is used exclusively for the request.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "<unparseable-url>"


class ModelDownloadManager:
    """Background download of the embedding GGUF from the CDN, with retries.

    The gateway kicks ``ensure_model()`` as a background task at startup, so
    boot is never blocked by the 610MB transfer. ``status`` is a plain dict
    readable at any time by the dashboard status endpoint. Concurrent
    ``ensure_model()`` calls (startup task + a dashboard Enable click) share
    one in-flight download via ``_lock``.
    """

    def __init__(self, target: Path | None = None):
        self._target = target or default_model_path()
        self._lock: asyncio.Lock | None = None  # created lazily inside the running loop
        self.status: dict[str, object] = {"step": "idle", "error": "", "attempt": 0}

    @property
    def target(self) -> Path:
        return self._target

    def model_ready(self) -> bool:
        return model_file_present(self._target)

    async def ensure_model(self, attempts: int = 1) -> bool:
        """Download the model if absent. Returns True when the file is in place.

        Runs up to ``attempts`` download cycles with exponential backoff between
        failures (network-unstable hosts recover automatically; a further
        retry also happens on every gateway boot and on each dashboard Retry
        click). Skipped entirely (returns False) when the
        ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` escape hatch is set — test runs
        must never trigger a 610MB model download.
        """
        if self.model_ready():
            self.status = {"step": "ready", "error": "", "attempt": 0}
            return True
        if embedding_model_is_custom():
            # Reached via the dashboard's Enable/Retry click. Downloading the
            # bundled model here would install a second model the embedder is
            # not using, and would imply the custom one had been replaced.
            msg = "a custom embedding model is configured (memory.embed_model_path)"
            logger.info("Skipping default model download — %s", msg)
            self.status = {"step": "custom_model", "error": "", "attempt": 0}
            return False
        if os.environ.get(_SKIP_DOWNLOAD_ENV) == "1":
            logger.info("%s=1 — skipping embedding model download", _SKIP_DOWNLOAD_ENV)
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # Re-check under the lock: a concurrent caller may have finished it.
            if self.model_ready():
                self.status = {"step": "ready", "error": "", "attempt": 0}
                return True
            last_error = "download failed"
            for attempt in range(1, max(1, attempts) + 1):
                self.status = {"step": "downloading", "error": "", "attempt": attempt}
                logger.info(
                    "Downloading embedding model (attempt %d/%d)...", attempt, max(1, attempts)
                )
                # Daemon thread: the blocking download must never pin
                # interpreter exit (Ctrl-C / one-shot CLI) — see helper docs.
                ok, err = await _run_download_on_daemon_thread(self._download_once)
                if ok:
                    self.status = {"step": "ready", "error": "", "attempt": attempt}
                    logger.info("Embedding model installed at %s", self._target)
                    return True
                last_error = err
                logger.warning(
                    "Embedding model download attempt %d/%d failed: %s",
                    attempt,
                    max(1, attempts),
                    err,
                )
                if attempt < attempts:
                    delay = min(
                        _DOWNLOAD_BACKOFF_BASE_SECS * (2 ** (attempt - 1)),
                        _DOWNLOAD_BACKOFF_CAP_SECS,
                    )
                    self.status = {"step": "waiting_retry", "error": err, "attempt": attempt}
                    await asyncio.sleep(delay)
            self.status = {"step": "failed", "error": last_error, "attempt": max(1, attempts)}
            return False

    # ── blocking helpers (run in executor) ──

    def _salvage_legacy_ollama_blob(self) -> bool:
        """Copy the GGUF from a legacy Ollama install instead of re-downloading.

        Users migrating from the Ollama-era embeddings already hold the exact
        model bytes on disk: Ollama stores layer blobs content-addressed as
        ``~/.ollama/models/blobs/sha256-<digest>``, and ``ollama pull`` of the
        same published GGUF stored it byte-identical. Copying locally saves
        the 610MB transfer. Best-effort: any failure falls through to the
        normal download; the copy is sha256-verified like a real download.
        """
        blobs_root = os.environ.get("OLLAMA_MODELS") or (Path.home() / ".ollama" / "models")
        blob = Path(blobs_root) / "blobs" / f"sha256-{_GGUF_SHA256}"
        try:
            if not blob.is_file() or blob.stat().st_size < _GGUF_MIN_BYTES:
                return False
            if _sha256_file(blob) != _GGUF_SHA256:
                return False
            self._install_file(blob, copy=True)
            logger.info("Reused embedding model from legacy Ollama blob store (%s)", blob)
            return True
        except OSError:
            logger.debug("Legacy Ollama blob salvage failed", exc_info=True)
            return False

    def _install_file(self, src: Path, *, copy: bool) -> None:
        """Atomically install *src* as the target model file.

        Stages into the TARGET directory (same filesystem → ``os.replace`` is
        atomic) under a per-process unique name so two concurrent processes
        (gateway + one-shot CLI) can never interleave writes into a shared
        staging file. The final replace is atomic either way — last writer
        wins with an identical, sha-verified payload.
        """
        self._target.parent.mkdir(parents=True, exist_ok=True)
        staging = self._target.parent / f".{self._target.name}.{os.getpid()}.tmp"
        try:
            if copy:
                shutil.copyfile(src, staging)
            else:
                # Cross-filesystem safe: shutil.move copies when rename fails.
                shutil.move(str(src), str(staging))
            os.replace(staging, self._target)
        finally:
            staging.unlink(missing_ok=True)

    def _download_once(self) -> tuple[bool, str]:
        """Try sources in order: Ollama salvage → HTTPS CDN."""
        # Migration fast path: users coming from the Ollama-era embeddings
        # already have the identical GGUF in Ollama's content-addressed store.
        if self._salvage_legacy_ollama_blob():
            return True, ""
        # HTTPS download from the CloudFront CDN (works for everyone, no
        # git/SSH and no cloud SDK required). Reports byte-level progress.
        return self._download_via_https()

    def _download_via_https(self) -> tuple[bool, str]:
        """Download the GGUF from the CDN via plain HTTPS with progress reporting."""
        url = _resolve_model_url()
        self._target.parent.mkdir(parents=True, exist_ok=True)
        staging = self._target.parent / f".{self._target.name}.http.{os.getpid()}.tmp"
        try:
            logger.info("Downloading embedding model from %s", redact_model_url(url))
            req = urllib.request.Request(url, method="GET")
            ctx = _make_ssl_context()
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- _resolve_model_url enforces https:// and the payload is sha256-pinned
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SECS, context=ctx) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                h = hashlib.sha256()
                with staging.open("wb") as out:
                    while True:
                        chunk = resp.read(_HTTP_CHUNK_BYTES)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        downloaded += len(chunk)
                        if downloaded % _PROGRESS_EVERY_BYTES < _HTTP_CHUNK_BYTES:
                            self.status = {
                                "step": "downloading",
                                "error": "",
                                "attempt": self.status.get("attempt", 0),
                                "bytes_downloaded": downloaded,
                                "bytes_total": total,
                            }
            # Verify inline (we computed sha256 while streaming).
            self.status = {
                "step": "verifying",
                "error": "",
                "attempt": self.status.get("attempt", 0),
            }
            digest = h.hexdigest()
            if digest != _GGUF_SHA256:
                staging.unlink(missing_ok=True)
                return False, (
                    f"sha256 mismatch: got {digest[:16]}…, "
                    f"expected {_GGUF_SHA256[:16]}… (corrupt download)"
                )
            if staging.stat().st_size < _GGUF_MIN_BYTES:
                staging.unlink(missing_ok=True)
                return False, f"downloaded file too small ({staging.stat().st_size} bytes)"
            self._install_file(staging, copy=False)
            return True, ""
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            staging.unlink(missing_ok=True)
            return False, f"HTTPS download failed: {exc}"
        except Exception as exc:
            staging.unlink(missing_ok=True)
            logger.warning("HTTPS model download failed", exc_info=True)
            return False, f"HTTPS download failed: {exc}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_download_manager: ModelDownloadManager | None = None
_download_manager_lock = threading.Lock()
# Module-level reference to the in-flight download task. asyncio only holds
# weak references to tasks — without this anchor a caller that drops the
# return value (e.g. cli_server) could see the download garbage-collected
# mid-transfer.
_download_task: "asyncio.Task[bool] | None" = None


def model_download_manager() -> ModelDownloadManager:
    """Process-wide download manager singleton (shared by gateway + dashboard)."""
    global _download_manager
    with _download_manager_lock:
        if _download_manager is None:
            _download_manager = ModelDownloadManager()
        return _download_manager


def reset_download_manager() -> None:
    """Drop the singleton (tests, KIROCREW_HOME changes)."""
    global _download_manager, _download_task
    with _download_manager_lock:
        _download_manager = None
        if _download_task is not None and not _download_task.done():
            _download_task.cancel()
        _download_task = None


def start_background_model_download() -> "asyncio.Task[bool] | None":
    """Kick the default-on background model download. Returns the task or None.

    Called from gateway/server startup. Returns None when a custom model is
    configured (the bundled model must never overwrite or compete with it —
    this is what makes a custom model survive a default-model version bump),
    when the model is already present (nothing to do), or when downloads are
    skipped via env. Idempotent: a second call while a download is in flight
    returns the existing task instead of spawning another.
    """
    global _download_task
    if embedding_model_is_custom():
        logger.info("Custom embedding model configured — skipping the default model download")
        return None
    mgr = model_download_manager()
    if mgr.model_ready():
        mgr.status = {"step": "ready", "error": "", "attempt": 0}
        return None
    if os.environ.get(_SKIP_DOWNLOAD_ENV) == "1":
        return None
    if _download_task is not None and not _download_task.done():
        return _download_task
    _download_task = asyncio.create_task(mgr.ensure_model(attempts=_DOWNLOAD_MAX_ATTEMPTS))
    return _download_task


# ── Sync embed function (vector-memory interface) ──

# 128 entries × 1024 floats/entry × 32 bytes/float (24-byte object + 8-byte ptr) ≈ 4 MB.
_EMBED_CACHE_MAX = 128


class _EmbedFailed(Exception):
    """Raised to prevent lru_cache from caching failed embedding attempts."""


def make_sync_embed_fn() -> Callable[[str], "list[float] | None"]:
    """Return a sync callable ``(str) -> list[float] | None`` over the shared embedder.

    Successful results are cached via ``functools.lru_cache`` keyed by input
    text AND the producing backend's ``model_id`` — after a backend swap
    (:func:`register_embedding_backend` + :func:`reset_shared_embedder`) the
    old model's cached vectors can never be served for the new model, which
    would silently mix incomparable vector spaces. Bounded to
    ``_EMBED_CACHE_MAX`` entries (see constant for size math). Failures
    (None) are not cached so a still-downloading model is retried. Embedding
    never blocks on the model load (kicked in the background); callers get
    ``None`` until the model is resident.
    """

    @functools.lru_cache(maxsize=_EMBED_CACHE_MAX)
    def _cached_embed(text: str, model_id: str) -> tuple[float, ...]:
        del model_id  # cache-key only — routes stale entries away after a backend swap
        info = _cached_embed.cache_info()
        if info.misses % 20 == 0:
            logger.info(
                "Embedding cache: hits=%d misses=%d size=%d/%d",
                info.hits,
                info.misses,
                info.currsize,
                info.maxsize,
            )
        vec = get_shared_embedder().embed(text)
        if vec is None:
            raise _EmbedFailed
        return tuple(vec)

    def _embed(text: str) -> list[float] | None:
        try:
            return list(_cached_embed(text, get_shared_embedder().model_id))
        except _EmbedFailed:
            return None

    return _embed
