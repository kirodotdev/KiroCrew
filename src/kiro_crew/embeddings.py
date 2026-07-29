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
from typing import Callable

from kiro_crew.config.loader import config_path
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

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

# ── Download constants ──

_DOWNLOAD_MAX_ATTEMPTS = 6  # background startup task (long backoff, may span hours)
DOWNLOAD_ATTEMPTS_INTERACTIVE = 3  # dashboard Enable/Retry click (fast feedback)
_DOWNLOAD_BACKOFF_BASE_SECS = 60.0
_DOWNLOAD_BACKOFF_CAP_SECS = 1800.0
# Escape hatch for tests/CI: never kick a 610MB model download from a test run.
_SKIP_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"
# Distribution: public CloudFront CDN in front of the SHARED model bucket —
# the same object the upstream project serves, so both
# products fetch one canonical, sha256-verified GGUF instead of maintaining
# duplicate copies. Plain HTTPS, no git access and no cloud SDK required. The
# sha256 pin above is the trust anchor for every source, so a tampered CDN
# object can only fail verification. Resolution order: KIROCREW_EMBED_MODEL_URL
# env, then the memory.embed_model_url config knob, then this default. The URL
# basename (qwen3-embedding-0.6b.gguf) intentionally differs from the on-disk
# _GGUF_FILENAME — the sha pin, not the name, is the integrity gate.
_MODEL_URL_ENV = "KIROCREW_EMBED_MODEL_URL"
_DEFAULT_MODEL_URL = "https://d35dbuobhek1fm.cloudfront.net/qwen3-embedding-0.6b.gguf"
_HTTP_TIMEOUT_SECS = 1800  # 610MB at >=340KB/s; slower links retry with backoff
_HTTP_CHUNK_BYTES = 1 << 20
# Written by the HTTP downloader every ~16MB so the status endpoint can report
# byte-level progress; the dashboard renders a determinate progress bar from it.
_PROGRESS_EVERY_BYTES = 16 << 20

# ── Vendored runtime loading ──

_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"


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
    libs_dir = _VENDOR_DIR / "llama_cpp_libs" / libs_dirname
    if not libs_dir.is_dir():
        logger.warning("Vendored llama.cpp libs missing at %s", libs_dir)
        return None
    # setdefault so an operator-provided override (e.g. a GPU build) wins.
    os.environ.setdefault("LLAMA_CPP_LIB_PATH", str(libs_dir))
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
    return models_dir() / _GGUF_FILENAME


def model_file_present(path: Path | None = None) -> bool:
    """True when the GGUF exists and is not a truncated/placeholder file."""
    target = path or default_model_path()
    try:
        return target.is_file() and target.stat().st_size > _GGUF_MIN_BYTES
    except OSError:
        return False


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


class LlamaCppEmbedder(EmbeddingBackend):
    """Serialized in-process embedder over the vendored llama.cpp runtime.

    The underlying ``Llama`` object is NOT thread-safe, so every load and
    inference call is serialized behind ``_lock``. All failures return
    ``None`` (graceful degradation) — callers already treat ``None`` as
    "no embedding available".
    """

    def __init__(
        self,
        model_path: Path | None = None,
        dim: int = _DEFAULT_DIM,
        model_id: str = _MODEL_ID,
    ):
        self._model_path = model_path or default_model_path()
        self._dim = dim
        self._model_id = model_id
        self._llm: object | None = None
        self._load_failed_at: float = 0.0
        self._lock = threading.Lock()  # serializes inference (Llama is not thread-safe)
        self._load_lock = threading.Lock()  # guards loader-thread spawn state
        self._load_thread: threading.Thread | None = None

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
        """True when the model is loaded in memory."""
        return self._llm is not None

    def _kick_background_load(self) -> None:
        """Start loading the model on a daemon thread. Idempotent, returns fast.

        The GGUF load takes seconds — it must NEVER run on a caller's thread
        (several call paths sit on the asyncio event loop). Until the load
        lands, ``embed*`` returns ``None`` and callers degrade to keyword
        search; the existing lazy-rebind / availability-recheck machinery
        picks embeddings up on a later call.
        """
        with self._load_lock:
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
            logger.info(
                "Loaded embedding model %s in %.1fs",
                self._model_path.name,
                time.monotonic() - started,
            )
            self._llm = llm  # atomic publish (GIL)
        except Exception:
            logger.warning("Failed to load embedding model %s", self._model_path, exc_info=True)
            self._load_failed_at = time.monotonic()
            self._llm = None

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
                resp = llm.create_embedding(clipped)  # type: ignore[attr-defined]
                vectors = [item["embedding"] for item in resp["data"]]
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

    def close(self) -> None:
        """Unload the model (frees ~700MB RSS). Safe to call repeatedly."""
        with self._lock, self._load_lock:
            self._llm = None
            self._load_failed_at = 0.0
            self._load_thread = None


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


def get_shared_embedder() -> EmbeddingBackend:
    """Process-wide embedder singleton.

    Vector memory and the knowledge library share one loaded model — the
    GGUF costs ~700MB RSS, so loading it twice is never acceptable.
    """
    global _shared_embedder
    with _shared_embedder_lock:
        if _shared_embedder is None:
            _shared_embedder = (_backend_factory or LlamaCppEmbedder)()
        return _shared_embedder


def reset_shared_embedder() -> None:
    """Drop the singleton (tests, disable-embeddings, KIROCREW_HOME changes)."""
    global _shared_embedder
    with _shared_embedder_lock:
        if _shared_embedder is not None:
            _shared_embedder.close()
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
            "%s must be an https:// URL — ignoring the override and using "
            "the CDN default", _MODEL_URL_ENV,
        )
    try:
        path = config_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            cfg_url = str(data.get("memory", {}).get("embed_model_url", "") or "").strip()
            if cfg_url:
                if cfg_url.lower().startswith("https://"):
                    return cfg_url
                logger.warning(
                    "memory.embed_model_url must be an https:// URL — ignoring "
                    "the override and using the CDN default",
                )
    except Exception:
        logger.debug("Could not read embed_model_url from config", exc_info=True)
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
            self.status = {"step": "verifying", "error": "", "attempt": self.status.get("attempt", 0)}
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

    Called from gateway/server startup. Returns None when the model is
    already present (nothing to do) or downloads are skipped via env.
    Idempotent: a second call while a download is in flight returns the
    existing task instead of spawning another.
    """
    global _download_task
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
