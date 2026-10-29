"""The embedding backend interface and its in-process llama.cpp implementation.

``EmbeddingBackend`` is what every backend provides; ``LlamaCppEmbedder`` runs
the model on one owned inference thread, loads it lazily, schedules queued
work by priority, unloads it when idle, and logs each embed's timing.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import abc
import asyncio
import heapq
import logging
import queue
import threading
import time
import weakref
from pathlib import Path

from kiro_crew.embeddings_child import ChildLlama, is_vendored_llama_class
from kiro_crew.embeddings_gguf import _GGUF_CAUSAL_SUFFIX, _model_context_policy
from kiro_crew.embeddings_model import (
    _is_sensitive_model_path,
    _model_file_stamp,
    _model_file_stamp_unaliased,
    active_model_path,
    model_file_present,
    model_pooling_type,
)
from kiro_crew.embeddings_native import _load_llama_class
from kiro_crew.embeddings_settings import (
    _COLD_LOAD_WAIT_SECS,
    _DEFAULT_DIM,
    _EMBED_WAIT_LOG_MS,
    _ENCODER_ARCHITECTURES,
    _EVICT_POLL_MAX_SECS,
    _INFER_STOP_TIMEOUT_SECS,
    _INTERACTIVE_QUEUE_RESERVE,
    _LLM_LOAD_RETRY_SECS,
    _MAX_EMBED_BATCH_TEXTS,
    _MAX_EMBED_CHARS,
    _MAX_PENDING_EMBEDS,
    _MODEL_ID,
    _N_CTX,
    _POOLING_TYPE_LAST,
    _PRIORITY_SENTINEL,
    PRIORITY_BULK,
    PRIORITY_NORMAL,
    EmbeddingWork,
    _embed_threads,
    _work_for_priority,
    bulk_embed_threads,
    bulk_pace_delay,
    embedding_child_process_enabled,
    embedding_idle_evict_secs,
    embedding_work,
)
from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger("kiro_crew.embeddings")


def _trim_native_heap() -> None:
    """Return freed heap pages to the OS after an unload. Best effort, no-op off glibc."""
    try:
        from kiro_crew.platform_compat import _malloc_trim

        _malloc_trim()
    except Exception:  # noqa: BLE001 - trimming is an optimisation only
        logger.debug("malloc_trim after the embedding unload failed", exc_info=True)


def _caller_may_wait_for_load() -> bool:
    """Whether this thread may block on a model load: never on an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


def _install_metadata_kv_clear(llm: object) -> bool:
    """Make the loaded context's KV clear reset cell metadata without zeroing.

    ``Llama.embed`` clears the KV cache before every decode and again after it,
    and the binding's ``kv_cache_clear`` passes ``data=True``, which memsets the
    whole KV buffer (224 MiB for the shipped model at 2,048 context tokens) each
    time. Embedding never reads a cell that the current decode did not write:
    clearing with ``data=False`` drops every cell's position and sequence ids,
    so the attention mask excludes all stale cells, and the stale contents are
    finite values a previous decode produced. The vectors are byte-identical;
    only the two memsets go. The buffer is zero-filled once when llama.cpp
    allocates it, so the first decode sees no uninitialised memory either.

    Installed on the context instance (the vendored binding stays upstream's),
    and only when the native symbol resolves. The context is held weakly so the
    override does not keep a freed context alive through a reference cycle, and
    a closed context (``ctx`` cleared, ``memory`` left dangling) is a no-op.
    Returns True when the override was installed.
    """
    ctx = getattr(llm, "_ctx", None)
    if ctx is None or not callable(getattr(ctx, "kv_cache_clear", None)):
        return False
    try:
        from llama_cpp import llama_cpp as native  # vendored copy, already imported
    except Exception:  # noqa: BLE001 - keep the binding's own clear
        return False
    memory_clear = getattr(native, "llama_memory_clear", None)
    if not callable(memory_clear):
        return False
    ctx_ref = weakref.ref(ctx)

    def kv_cache_clear() -> None:
        live = ctx_ref()
        if live is None or getattr(live, "ctx", None) is None:
            return
        memory = getattr(live, "memory", None)
        if memory is None:
            # Cache-less (non-causal) contexts allocate no KV memory.
            return
        memory_clear(memory, False)

    ctx.kv_cache_clear = kv_cache_clear
    return True


def _install_bounded_decode(llm: object) -> bool:
    """Cap the tokens one native decode handles at the context's physical micro-batch.

    In embedding mode llama.cpp makes every token of a batch an output and
    reserves ``n_vocab + n_embd`` floats per output -- the logits are reserved
    and computed although nothing here reads them -- in a buffer that grows to
    the largest batch ever decoded and is never shrunk: 610,772 bytes per token
    for the shipped model, 1.19 GB after one 2,048-token input, held until the
    model unloads. The vendored ``Llama.embed`` is told to decode a longer batch
    as consecutive slices of at most ``n_ubatch`` tokens (its
    ``kiro_crew_max_decode_tokens`` divergence), so the buffer never holds more
    than ``n_ubatch`` rows: 75 MiB for the shipped model's 128.

    ``n_ubatch`` and not anything smaller, because a unified-cache decode splits a
    batch into consecutive ``n_ubatch`` slices in batch order (``split_simple``):
    each bounded call is then exactly one of the micro-batches the single call
    runs -- the same tokens, positions and cache contents, in the same order -- so
    every backend runs the same kernels on the same inputs and the vectors are
    byte-identical by construction, not by a measurement on one platform.

    Set only for a context that decodes causally through a KV cache, where the
    cache carries the earlier slices; ``Llama.embed`` itself slices only under
    last-token pooling. A cache-less encoder, or a GGUF declaring
    ``<architecture>.attention.causal = false``, needs every token of an input in
    one micro-batch. ``_model_context_policy`` already gives such a file
    ``n_ubatch == n_batch``, which no batch can exceed -- but only when its own
    bounded header parser reads the file. A header it refuses and llama.cpp
    loads (over ``_GGUF_MAX_METADATA_ITEMS`` entries, an array over
    ``_GGUF_MAX_ARRAY_ITEMS``, a captured string over
    ``_GGUF_MAX_DECODED_STRING_BYTES``) gets the decoder sizes whatever the model
    is, so this reads the loaded runtime's own view instead: its ``memory``
    handle and the metadata llama.cpp parsed. A context whose shape cannot be
    read keeps the single decode. Returns True when set.
    """
    ctx = getattr(llm, "_ctx", None)
    if ctx is None or getattr(ctx, "memory", None) is None:
        return False
    metadata = getattr(llm, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    architecture = str(metadata.get("general.architecture") or "").strip().lower()
    if not architecture or architecture in _ENCODER_ARCHITECTURES:
        return False
    # Absent, the runtime defaults to causal attention (as _model_context_policy reads it).
    causal = metadata.get(architecture + _GGUF_CAUSAL_SUFFIX)
    if causal is not None and str(causal).strip().lower() != "true":
        return False
    n_ubatch = getattr(getattr(llm, "context_params", None), "n_ubatch", 0)
    if not isinstance(n_ubatch, int) or isinstance(n_ubatch, bool) or n_ubatch <= 0:
        return False
    llm.kiro_crew_max_decode_tokens = n_ubatch  # type: ignore[attr-defined]
    return True


def _free_llama(llm: object) -> None:
    """Release a constructed model the loader will not publish.

    Best effort: a model that will never be published must not keep its
    ~700MB mapped, and a failure to free it must not propagate out of the
    loader.
    """
    closer = getattr(llm, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - freeing must not propagate
            logger.debug("Freeing abandoned model failed", exc_info=True)


def _construct_llama(llama_cls: object, kwargs: dict, stamp: "tuple[int, ...] | None") -> object:
    """Build the model: in this process by default, in a child process when switched on.

    The child (``memory.embedding_child_process``, default off) takes native
    inference and its Python wrapper off this process's GIL and CPU priority; see
    :mod:`kiro_crew.embeddings_child`. Only the vendored runtime can be rebuilt in
    another interpreter, so any other class (a test fake, a future backend)
    keeps running here, as does everything when the switch is ``false``.
    """
    if embedding_child_process_enabled() and is_vendored_llama_class(llama_cls):
        return ChildLlama(kwargs, stamp=stamp)
    return llama_cls(**kwargs)  # type: ignore[operator]


def _chars_bucket(chars: int) -> str:
    """Low-cardinality size band for the inference metric.

    Bucketed rather than raw so the metric can separate a short interactive query
    from a bulk consolidation block without an unbounded attribute domain.
    """
    for bound in (128, 512, 2000, 6000):
        if chars <= bound:
            return f"<={bound}"
    return ">6000"


def _emit_embed_timing(
    t0: float,
    t_started: float,
    texts: "list[str]",
    *,
    priority: int = PRIORITY_NORMAL,
    failed: bool = False,
) -> None:
    """Report queue wait and model time separately for one embed call.

    The split is the point. ``infer_ms`` is the model's own cost. ``wait_ms`` is
    this call blocked on the queue while ANOTHER caller's embed ran, because the
    whole process is serialized onto one model on one inference thread. A short
    interactive embed reporting a large ``wait_ms`` is therefore not slow, it is
    queued behind bulk background work — and that is a different defect with a
    different fix than slow inference.
    """
    now = time.monotonic()
    # Clamped: monotonic makes a negative value impossible on the real path, but a
    # negative sample is silently REJECTED by the recorder (losing the datapoint)
    # and logs a warning, so a clock edge case must not cost us the measurement.
    wait_ms = max(0.0, (t_started - t0) * 1000.0)
    infer_ms = max(0.0, (now - t_started) * 1000.0)
    chars = sum(len(t) for t in texts)
    # BULK stays at DEBUG however long it waited. Being preempted is the DESIGNED
    # outcome for a corpus sweep, not news, and a migration preempted row-by-row
    # would otherwise emit thousands of INFO lines and bury the interactive ones
    # this log exists to surface.
    reportable = wait_ms >= _EMBED_WAIT_LOG_MS and priority != PRIORITY_BULK
    level = logging.INFO if reportable else logging.DEBUG
    logger.log(
        level,
        "Embed timing: wait=%.0fms infer=%.0fms n=%d chars=%d prio=%d%s",
        wait_ms,
        infer_ms,
        len(texts),
        chars,
        priority,
        " FAILED" if failed else "",
    )
    try:
        recorder = get_recorder()
        recorder.histogram("kirocrew.embed.queue_wait", wait_ms, unit="ms")
        recorder.histogram(
            "kirocrew.embed.inference",
            infer_ms,
            unit="ms",
            attrs={"chars_bucket": _chars_bucket(chars)},
        )
    except Exception:
        logger.debug("Embed timing metric emission failed", exc_info=True)


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
      keys off :func:`kiro_crew.knowledge.embedder.embed_signature`, which is
      built on :func:`embedding_space_signature` and so folds BOTH in; vector
      memory re-embeds via ``migrate``).
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

    def prefetch(self) -> None:
        """Start loading the model if an embed is about to need it. Never blocks.

        A hint, not a request: the default does nothing, and an implementation
        must not change what a later ``embed`` returns, only when it can start.
        """

    @abc.abstractmethod
    def embed(self, text: str, *, priority: int = PRIORITY_NORMAL) -> "list[float] | None":
        """Embed one text, or None when unavailable.

        *priority* orders competing callers on the shared model (``PRIORITY_*``).
        Implementations that do not queue may ignore it.
        """

    @abc.abstractmethod
    def embed_batch(
        self, texts: "list[str]", *, priority: int = PRIORITY_NORMAL
    ) -> "list[list[float]] | None":
        """Embed several texts, or None when unavailable. See :meth:`embed`."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources (unload model). Safe to call repeatedly."""


# ── In-process llama.cpp backend ──


class _InferJob:
    """One ``create_embedding`` call handed to the embedder's worker thread."""

    __slots__ = ("llm", "texts", "result", "error", "done", "started", "work")

    def __init__(self, llm: object, texts: "list[str]", priority: int = PRIORITY_NORMAL) -> None:
        self.work = _work_for_priority(priority)
        self.llm = llm
        self.texts = texts
        self.result: object | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()
        # Set by the worker once it actually begins inference, so the caller can
        # separate time spent QUEUED from time spent in the model.
        self.started: float = 0.0


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

    With ``memory.embedding_child_process`` on, the published model is a
    :class:`~kiro_crew.embeddings_child.ChildLlama`:
    the model itself lives in a child process and ``kc-embed-infer`` only waits
    on a pipe, so inference runs outside this process's GIL. Queueing,
    priorities, pacing and eviction are unchanged and stay here.
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
        # A pure function of the id, which is what names the vector space, so
        # the pooling can never change under vectors already stored for this id.
        self._pooling_type = model_pooling_type(model_id)
        # Only the bundled factory paths set this. A custom model may declare
        # the same id and width without containing the same weights.
        self._uses_bundled_identity = False
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
        # File stamp of the first published load. A reload after an idle unload
        # must map the same weights: model_id (the vector-space identity) was
        # fixed when this backend was built, so different weights at the same
        # path would store vectors under the wrong identity. Never cleared.
        self._published_stamp: tuple[int, ...] | None = None
        self._lock = threading.Lock()  # serializes inference (Llama is not thread-safe)
        self._load_lock = threading.Lock()  # guards loader-thread spawn state
        self._load_thread: threading.Thread | None = None
        # Single owned inference thread + its PRIORITY job queue (see the class
        # docstring). Items are ``(priority, seq, job)``. ``seq`` is a strictly
        # increasing tiebreaker, which makes the ordering stable — equal
        # priorities stay FIFO — and also means the heap never has to compare two
        # _InferJob objects, which are not orderable.
        self._jobs: "queue.PriorityQueue[tuple[int, int, _InferJob | None]]" = queue.PriorityQueue(
            maxsize=_MAX_PENDING_EMBEDS + 1  # the extra slot is reserved for shutdown
        )
        self._jobs_changed = threading.Event()
        self._bulk_ready_at = 0.0
        self._seq = 0
        self._seq_lock = threading.Lock()
        # Thread count currently programmed into the loaded context, and whether
        # this backend can reprogram it at all. Both are touched ONLY by the
        # single inference worker, under ``_lock``, so they need no lock of their
        # own. ``None`` means "not yet known" — the count llama.cpp got at load
        # time is _embed_threads(), but re-deriving that here would go stale if
        # the config changed since, so the worker programs it explicitly on the
        # first job instead of assuming.
        self._applied_threads: int | None = None
        self._thread_class_unsupported = False
        # Guards the DISPATCH state — (_infer_thread, _jobs) selection, worker
        # spawn, and the enqueue — as one atomic step. Deliberately NOT _lock:
        # the worker holds _lock across inference, so enqueueing under it would
        # block every submitter behind the in-flight embed and put at most one
        # job in the queue, which is exactly the condition that made priority
        # meaningless before. Held only for a heap push, never across inference,
        # a join, or a model load.
        self._dispatch_lock = threading.Lock()
        self._infer_thread: threading.Thread | None = None
        # Idle eviction (see _DEFAULT_IDLE_EVICT_SECS). ``_last_used`` is stamped
        # at the START of every embed and at publish, so a long job or a model
        # that just loaded is never mistaken for an idle one.
        self._last_used = time.monotonic()
        self._evict_thread: threading.Thread | None = None
        self._evict_wake = threading.Event()

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

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

    def prefetch(self) -> None:
        """Kick the background load an ``embed`` would kick, a little earlier.

        Same gates as ``embed``: nothing when closed or not serving (a candidate
        mid-swap), and ``_kick_background_load`` keeps its own (already loaded,
        loading, failed-load cooldown, file missing, protected path). So a load
        that would fail or never start behaves exactly as before; one that
        succeeds is published with ``_last_used`` stamped, which starts the idle
        clock the evictor reads, as any load does.
        """
        if self._closed or not self._serving or self._llm is not None:
            return
        self._kick_background_load()

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
            threads = _embed_threads()
            # Stamped BEFORE the header read and re-checked once the constructor
            # returns: the policy reads the GGUF header and llama.cpp then opens
            # the path a second time, so a file replaced in between would get a
            # context sized from the previous header (decoder sizes on an
            # encoder are the >512-token abort the policy exists to prevent).
            # Judged on one descriptor too: a second name on the bundled GGUF refuses
            # this load (logged, retried on the usual cooldown), never a spawn.
            stamp = _model_file_stamp_unaliased(self._model_path)
            if stamp is None:
                self._load_failed_at = time.monotonic()
                return
            if self._published_stamp is not None and stamp != self._published_stamp:
                # Reload after an idle unload, but the file is not the one this
                # backend first loaded (replaced or rewritten in place). Its
                # vectors would be stored under the old model_id, so refuse:
                # search stays keyword-only until a restart or a model change
                # re-verifies the file and reconciles stored vectors.
                logger.error(
                    "Embedding model %s changed on disk since it was first loaded; "
                    "refusing to reload it. Restart the gateway to use the new file.",
                    self._model_path.name,
                )
                self._load_failed_at = time.monotonic()
                return
            policy = _model_context_policy(self._model_path)
            if policy.n_ctx < _N_CTX:
                # Once per model load, not per embed call: the binding truncates
                # silently, so this is the only place the rule is stated.
                logger.warning(
                    "GGUF %s was trained for %d positions; its embedding context is "
                    "sized to %d tokens and a longer input is truncated to its first "
                    "%d tokens before embedding.",
                    self._model_path.name,
                    policy.n_ctx,
                    policy.n_ctx,
                    policy.n_ctx,
                )
            llama_kwargs = dict(
                model_path=str(self._model_path),
                embedding=True,
                # From the model's id, passed for every file: a model whose id
                # was recorded under last-token keeps it whatever the file says.
                pooling_type=self._pooling_type,
                # n_batch == n_ctx so the logical batch always covers the whole
                # input in one go, which last-token pooling needs. In embedding mode
                # the vendored constructor skips the ~1.24 GB per-token logits
                # buffer this would otherwise size (see the `n_score_rows`
                # divergence comment in src/kiro_crew/_vendor/llama_cpp/llama.py),
                # and _install_bounded_decode below keeps llama.cpp's own output
                # buffer at its load size, so n_batch does not trade memory
                # against input length. Both are
                # clamped to the trained position count the GGUF declares, at
                # most 2,048 (see _model_context_policy). Decoder models keep the
                # lower-RSS 128-token micro-batch, bounded by that batch; non-causal
                # models (cache-less encoder architectures, or a GGUF declaring
                # `.attention.causal = false`) need one physical micro-batch to
                # hold every token, so all three coincide. So does any model
                # running mean or CLS pooling: llama.cpp pools each micro-batch on
                # its own and keeps only the last one's result, which would store
                # the mean, or the first token, of a longer input's tail. The
                # token last-token pooling reads is always in the last micro-batch,
                # so only a last-token decoder keeps the 128.
                n_ctx=policy.n_ctx,
                n_batch=policy.n_batch,
                n_ubatch=(
                    policy.n_ubatch if self._pooling_type == _POOLING_TYPE_LAST else policy.n_batch
                ),
                # Both pools are pinned. Embedding is prompt processing, so the
                # BATCH pool is the one that actually runs, but leaving the
                # generation pool at llama.cpp's cpu//2 default would still size
                # a second oversubscribed pool on this thread.
                n_threads=threads,
                n_threads_batch=threads,
                verbose=False,
            )
            llm = _construct_llama(llama_cls, llama_kwargs, stamp)
            _install_metadata_kv_clear(llm)
            _install_bounded_decode(llm)
            try:
                unchanged = _model_file_stamp(self._model_path) == stamp
            except OSError:
                unchanged = False
            if not unchanged:
                # The mapped weights are not the file the header came from.
                # Same shape as the dim refusal below: never publish, mark the
                # load failed, and let the cooldown retry size from the header
                # that is on disk by then. (_model_content_digest guards its
                # sha256 the same way.)
                _free_llama(llm)
                logger.warning(
                    "Embedding model %s changed on disk while it was being loaded; "
                    "discarding the context sized from its previous header. The load "
                    "is retried in %.0f s.",
                    self._model_path.name,
                    _LLM_LOAD_RETRY_SECS,
                )
                self._load_failed_at = time.monotonic()
                self._llm = None
                return
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
                _free_llama(llm)
                return
            # A fresh context carries llama.cpp's load-time thread count, so the
            # count the worker last programmed does not describe it. Clear the
            # record BEFORE publishing, or the first job on the new context would
            # match the stale count and skip programming entirely — leaving a
            # bulk sweep on the interactive pool (or the reverse) with nothing to
            # show why. Written from the loader thread and read by the inference
            # worker; both are single plain-attribute accesses (GIL-atomic), and
            # the only cost of racing is one redundant set_n_threads call.
            self._applied_threads = None
            self._last_used = time.monotonic()
            if self._published_stamp is None:
                self._published_stamp = stamp
            self._llm = llm  # atomic publish (GIL)
            self._start_evictor()
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

    def _next_infer_job(
        self, jobs: "queue.PriorityQueue[tuple[int, int, _InferJob | None]]"
    ) -> "tuple[int, int, _InferJob | None]":
        """Apply one shared bulk duty cycle, interruptible by interactive work."""
        while True:
            self._jobs_changed.clear()
            delay = None
            with self._dispatch_lock:
                # Peek under PriorityQueue's mutex; only this owned worker removes
                # jobs, and dispatch/close serialize every producer with this lock.
                with jobs.mutex:
                    head = jobs.queue[0] if jobs.queue else None
                if head is not None:
                    if head[2] is not None and head[0] >= PRIORITY_BULK:
                        delay = max(0.0, self._bulk_ready_at - time.monotonic())
                    if not delay:
                        return jobs.get_nowait()
            # A later interactive call or the shutdown sentinel wakes the worker
            # immediately. Bulk loops cannot bypass the shared cooldown by each
            # sleeping independently on another store's thread.
            self._jobs_changed.wait(delay)

    def _infer_loop(self, jobs: "queue.PriorityQueue[tuple[int, int, _InferJob | None]]") -> None:
        """Worker body: run queued ``create_embedding`` calls, one at a time.

        ``jobs`` is passed in rather than read from ``self`` so this worker is
        bound for life to the queue it was started with. That is what makes a
        straggler harmless: it can only ever drain — and exit on the sentinel
        from — its own queue, never one feeding a later worker.

        Every job is completed even on failure, so a caller can never be left
        waiting on ``job.done``. ``None`` is the shutdown sentinel from
        :meth:`close`; exiting the thread is what releases llama.cpp's compute
        pool.

        This worker — NOT the caller — holds ``_lock`` across inference. That is
        what lets the queue's priority mean anything: while the caller held it,
        every other caller blocked *before* it could enqueue, so at most one job
        was ever queued and there was nothing to order. Serialization is
        unchanged, because a single owner thread running under ``_lock`` is
        strictly no more concurrent than a single caller holding it was.
        """
        while True:
            prio, _seq, job = self._next_infer_job(jobs)
            if job is None:
                # close() has already dropped the model. Anything queued behind
                # the sentinel would otherwise wait on job.done forever, since
                # no worker will serve this queue again — fail them explicitly.
                self._drain_orphans(jobs)
                return
            try:
                with self._lock:
                    if job.work.expired():
                        job.error = TimeoutError("embedding work expired before inference")
                        continue
                    self._apply_thread_class(job.llm, prio)
                    job.started = time.monotonic()
                    # A child-process model can stop waiting at the caller's
                    # deadline; an in-process native call cannot be interrupted.
                    if isinstance(job.llm, ChildLlama):
                        job.result = job.llm.create_embedding_for(job.texts, job.work)
                    else:
                        job.result = job.llm.create_embedding(job.texts)  # type: ignore[attr-defined]
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller verbatim
                job.error = exc
            finally:
                if prio >= PRIORITY_BULK and job.started:
                    self._bulk_ready_at = time.monotonic() + bulk_pace_delay(
                        time.monotonic() - job.started
                    )
                job.done.set()

    def _apply_thread_class(self, llm: object, priority: int) -> None:
        """Program llama.cpp's compute pools for this job's scheduling class.

        A BULK job may run on fewer threads than an interactive one
        (``memory.embedding_bulk_threads``), so a minutes-long corpus sweep does
        not hold most of the box while a human is typing. The count is a property
        of the CONTEXT, not of the call, so it is resolved per job — the config
        read is a small uncached parse and the ``llama_set_n_threads`` call just
        stores two ints, both negligible against the ~100–400 ms inference this
        precedes on the same thread. The native call is skipped when the class
        did not change, which is the steady state.

        Fail-soft by design: a backend without a reprogrammable context (a stub,
        a future non-llama.cpp backend) keeps whatever it loaded with, warns
        once, and never retries. Losing the dial must never lose the embedding.
        """
        if self._thread_class_unsupported:
            return
        want = bulk_embed_threads() if priority >= PRIORITY_BULK else _embed_threads()
        if want == self._applied_threads:
            return
        ctx = getattr(llm, "_ctx", None)
        setter = getattr(ctx, "set_n_threads", None)
        if not callable(setter):
            self._thread_class_unsupported = True
            logger.debug(
                "Embedding backend cannot reprogram its thread count; "
                "memory.embedding_bulk_threads has no effect"
            )
            return
        try:
            setter(want, want)
        except Exception:
            # Leave _applied_threads alone: the context is still running whatever
            # it had, and pretending otherwise would skip the next attempt.
            self._thread_class_unsupported = True
            logger.debug("Could not set embedding thread count", exc_info=True)
            return
        self._applied_threads = want

    @staticmethod
    def _drain_orphans(
        jobs: "queue.PriorityQueue[tuple[int, int, _InferJob | None]]",
    ) -> None:
        """Fail every job left on a retired queue so no caller waits forever."""
        while True:
            try:
                _prio, _seq, job = jobs.get_nowait()
            except queue.Empty:
                return
            if job is None:
                continue
            job.error = RuntimeError("embedding backend closed before this job ran")
            job.done.set()

    def _submit_infer(
        self, llm: object, texts: "list[str]", priority: int = PRIORITY_NORMAL
    ) -> "_InferJob":
        """Queue one inference for the owned thread and wait for the job to finish.

        Returns the completed job rather than its result, so the caller can read
        ``job.started`` and tell queue wait apart from model time. Errors are
        left ON the job; :meth:`_create_embedding` is the raising wrapper.

        The caller does NOT hold ``_lock`` — the worker takes it around the
        actual inference — so several callers can have work queued at once and
        *priority* decides who the single model serves next. Pending work is
        bounded; overload returns an unavailable embedding so the stored row can
        remain pending and retrieval can use its lexical path.
        """
        job = _InferJob(llm, texts, priority)
        with self._dispatch_lock:
            # Retirement check, worker selection/spawn and the enqueue are ONE
            # atomic step. Split, they lose two ways: two callers racing an
            # absent worker each spawn one and orphan the loser's thread, and a
            # close() landing between the worker check and the enqueue puts the
            # job on a queue no worker will ever serve, so job.done is never set
            # and the caller blocks forever on an unbounded wait.
            if self._closed or not self._serving or llm is not self._llm:
                # The backend was retired or swapped while this call was in
                # flight. Fail the job here rather than queueing it into a space
                # nothing will drain; embed_batch turns this into None, which is
                # the ABC's documented "no embedding available".
                job.error = RuntimeError("embedding backend retired before this job was queued")
                job.done.set()
                return job
            thread = self._infer_thread
            if thread is None or not thread.is_alive():
                # New worker, new queue. A straggler left behind by a timed-out
                # close() join keeps draining its OWN queue, so it can neither
                # consume this worker's jobs nor eat this worker's future sentinel.
                self._jobs = queue.PriorityQueue(maxsize=_MAX_PENDING_EMBEDS + 1)
                thread = threading.Thread(
                    target=self._infer_loop,
                    args=(self._jobs,),
                    name="kc-embed-infer",
                    daemon=True,
                )
                self._infer_thread = thread
                thread.start()
            priority = min(priority, job.work.priority)
            capacity = _MAX_PENDING_EMBEDS
            if priority >= PRIORITY_NORMAL:
                capacity -= _INTERACTIVE_QUEUE_RESERVE
            if self._jobs.qsize() >= capacity:
                job.error = RuntimeError("embedding queue is busy; retry deferred work later")
                job.done.set()
                return job
            if job.work.expired():
                job.error = TimeoutError("embedding work expired before admission")
                job.done.set()
                return job
            self._jobs.put((priority, self._next_seq(), job))
            self._jobs_changed.set()
        while not job.done.wait(0.05):
            if job.work.expired():
                with self._dispatch_lock, self._jobs.mutex:
                    for index, entry in enumerate(self._jobs.queue):
                        if entry[2] is job:
                            self._jobs.queue.pop(index)
                            heapq.heapify(self._jobs.queue)
                            job.error = TimeoutError("embedding work expired in queue")
                            job.done.set()
                            self._jobs_changed.set()
                            break
                # A claimed native call is not interruptible. Keep this worker
                # and its admission slot until the native owner finishes it.
        return job

    def promote_pending(self, work: EmbeddingWork, priority: int) -> None:
        """Upgrade an identical queued bulk request when a human starts waiting."""
        with self._dispatch_lock, self._jobs.mutex:
            work.priority = min(work.priority, priority)
            for index, (old, seq, job) in enumerate(self._jobs.queue):
                if job is not None and job.work is work and old > priority:
                    self._jobs.queue[index] = (priority, seq, job)
            heapq.heapify(self._jobs.queue)
            self._jobs_changed.set()

    def _create_embedding(
        self, llm: object, texts: "list[str]", priority: int = PRIORITY_NORMAL
    ) -> object:
        """Run one ``create_embedding`` on the owned thread; re-raise its error."""
        job = self._submit_infer(llm, texts, priority)
        if job.error is not None:
            raise job.error
        return job.result

    def embed(self, text: str, *, priority: int = PRIORITY_NORMAL) -> list[float] | None:
        """Embed a single text. Returns None on any failure."""
        result = self.embed_batch([text], priority=priority)
        return result[0] if result else None

    def embed_batch(
        self, texts: list[str], *, priority: int = PRIORITY_NORMAL
    ) -> list[list[float]] | None:
        """Embed multiple texts. Returns None on any failure (incl. model not loaded yet).

        When the model isn't in memory this kicks a background load. Bulk work
        and event-loop callers get ``None`` immediately; an interactive or normal
        caller on a worker thread waits for that load (bounded, see
        :meth:`_wait_for_cold_load`), so the first query after an idle unload
        still gets vectors. Inference is serialized onto one owned thread;
        *priority* decides the order that single model serves competing callers
        (see ``PRIORITY_*``).
        """
        if not texts or not any(t.strip() for t in texts):
            return None
        if len(texts) > _MAX_EMBED_BATCH_TEXTS:
            result = []
            for start in range(0, len(texts), _MAX_EMBED_BATCH_TEXTS):
                batch = self.embed_batch(
                    texts[start : start + _MAX_EMBED_BATCH_TEXTS], priority=priority
                )
                if batch is None:
                    return None
                result.extend(batch)
            return result
        if self._closed or not self._serving:
            # Closed: terminal, never reload. Not serving: a candidate mid-swap,
            # whose vectors would be in a space the store has not reconciled to.
            return None
        self._last_used = time.monotonic()
        llm = self._llm
        if llm is None:
            self._kick_background_load()
            llm = self._wait_for_cold_load(priority)
            if llm is None:
                return None
        clipped: list[str] = []
        for t in texts:
            if len(t) > _MAX_EMBED_CHARS:
                logger.debug("Truncating embed input %d -> %d chars", len(t), _MAX_EMBED_CHARS)
                t = t[:_MAX_EMBED_CHARS]
            clipped.append(t)
        _t0 = time.monotonic()
        job = self._submit_infer(llm, clipped, priority)
        # started==0 means the worker never reached inference (drained orphan).
        _started = job.started or time.monotonic()
        if job.error is not None:
            if not isinstance(job.error, Exception):
                # BaseException (KeyboardInterrupt/SystemExit) is relayed to the
                # caller verbatim, as it was when the call ran inline.
                _emit_embed_timing(_t0, _started, clipped, priority=priority, failed=True)
                raise job.error
            logger.debug("In-process embed failed", exc_info=job.error)
            _emit_embed_timing(_t0, _started, clipped, priority=priority, failed=True)
            return None
        try:
            vectors = [item["embedding"] for item in job.result["data"]]  # type: ignore[index]
        except Exception:
            logger.debug("In-process embed failed", exc_info=True)
            _emit_embed_timing(_t0, _started, clipped, priority=priority, failed=True)
            return None
        _emit_embed_timing(_t0, _started, clipped, priority=priority)
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

    def can_load_on_demand(self) -> bool:
        """True when an embed would load the model now and then produce vectors.

        Lets an availability check answer yes for a model that is merely not in
        memory (never loaded yet, or unloaded when idle), so a search uses
        semantic results and pays the load once instead of silently falling
        back to keyword search. False once a load has failed (a dim mismatch,
        a corrupt file, missing native libs, a model changed on disk), even
        after its retry cooldown: such a model would never yield vectors, so
        it must not be reported as available. ``close()`` clears the failure,
        so a later unload-and-reload starts fresh. Also False when the file is
        missing, or once the backend is closed or not serving.
        """
        if self._closed or not self._serving:
            return False
        if self._load_failed_at:
            return False
        if _is_sensitive_model_path(self._model_path):
            return False
        return model_file_present(self._model_path)

    def _wait_for_cold_load(self, priority: int) -> object | None:
        """Wait for a load this call kicked, when the caller can afford to.

        Only an interactive or normal embed off the event loop waits; bulk work
        and loop threads keep the old non-blocking answer (``None``, so the
        caller degrades to keyword search and retries later). The wait is
        bounded by ``_COLD_LOAD_WAIT_SECS`` and by the caller's own deadline.
        This is what keeps idle eviction from costing a query its semantic
        results: the first query after an unload pays the load time once.
        """
        if priority >= PRIORITY_BULK or not _caller_may_wait_for_load():
            return None
        work = embedding_work.get()
        give_up = time.monotonic() + _COLD_LOAD_WAIT_SECS
        while True:
            llm = self._llm
            if llm is not None:
                return llm
            with self._load_lock:
                thread = self._load_thread
            if thread is None or not thread.is_alive():
                # Nothing loading (no file yet, failed load in cooldown): the
                # result is decided, so do not sleep on it.
                return self._llm
            if time.monotonic() >= give_up or (work is not None and work.expired()):
                return None
            thread.join(0.05)

    def _start_evictor(self) -> None:
        """Start the idle evictor for the model just published. Idempotent.

        Guarded by ``_load_lock`` so it cannot race ``_close_retiring_worker``:
        if ``retire()`` or ``close()`` cleared ``_llm`` (or set ``_closed``)
        before this call acquires the lock, the evictor must not start -- the
        next ``_close_retiring_worker`` already saw ``_evict_thread=None`` and
        returned without a join, so a thread started here would be orphaned.
        """
        with self._load_lock:
            if self._closed or self._llm is None:
                return
            if self._evict_thread is not None and self._evict_thread.is_alive():
                return
            self._evict_wake = threading.Event()
            self._evict_thread = threading.Thread(
                target=self._evict_loop, name="kc-embed-evict", daemon=True
            )
            self._evict_thread.start()

    def _evict_loop(self) -> None:
        """Unload the model once it has gone unused for the configured time.

        Exits after an unload (the next load starts a fresh evictor) or when the
        backend is closed or unloaded by someone else.
        """
        while True:
            secs = embedding_idle_evict_secs()
            self._evict_wake.wait(
                min(_EVICT_POLL_MAX_SECS, max(1.0, secs / 4)) if secs > 0 else _EVICT_POLL_MAX_SECS
            )
            if self._closed or self._llm is None or self._evict_wake.is_set():
                return
            if secs <= 0 or time.monotonic() - self._last_used < secs:
                continue
            if self.evict_if_idle(secs):
                return

    def evict_if_idle(self, idle_secs: float) -> bool:
        """Unload the model when nothing used it for ``idle_secs``. Returns True if unloaded.

        Refuses while work is queued or running, so an eviction never fails a job;
        an embed racing the unload is refused by the dispatch check and degrades
        exactly as any embed before the first load does.
        """
        llm = self._llm
        if llm is None or self._closed:
            return False
        if time.monotonic() - self._last_used < idle_secs or self._jobs.qsize():
            return False
        if not self._lock.acquire(blocking=False):
            return False  # inference running right now
        self._lock.release()
        with self._load_lock:
            if self._load_thread is not None and self._load_thread.is_alive():
                return False
        # Decide on the model and worker close() actually retired, not a snapshot
        # taken before it: a first embed can start the worker between the two,
        # and a concurrent close/retire can take the model and its (possibly
        # still running) worker first. Freeing the context under a job is a
        # native use-after-free, so free only what THIS close retired.
        retired, thread = self._close_retiring_worker()
        if retired is llm and (thread is None or not thread.is_alive()):
            # The worker that could still touch the native context has exited,
            # so free it now rather than whenever the last reference is dropped.
            _free_llama(llm)
        # Otherwise the last reference (a job's ``llm``) frees it on drop.
        del llm, retired
        _trim_native_heap()
        logger.info(
            "Unloaded the embedding model after %.0f s idle; the next embed reloads it",
            idle_secs,
        )
        return True

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
        self._close_retiring_worker()  # result only matters to the evictor

    def _close_retiring_worker(self) -> tuple[object | None, threading.Thread | None]:
        """:meth:`close`, returning the model and inference worker it actually retired.

        Both are read and cleared in one critical section (the worker under
        ``_dispatch_lock``, the lock every dispatch takes), so the returned thread
        is the only one that can still hold a job on the returned model. A caller
        that frees the native context (the idle evictor) must decide on THIS pair,
        never on a snapshot taken before ``close()``: a first embed can start the
        worker in between, and a concurrent close/retire can take the model and
        its worker first, leaving this call nothing to free.
        """
        with self._lock, self._load_lock:
            llm, self._llm = self._llm, None
            self._load_failed_at = 0.0
            self._load_thread = None
            # Same lock the submitters use, so a retirement can never interleave
            # with a dispatch: a caller either enqueues onto a live worker before
            # this runs, or sees the retired state and fails fast.
            with self._dispatch_lock:
                thread, self._infer_thread = self._infer_thread, None
                # Retire the queue at the same time as the thread, so a late caller
                # cannot enqueue onto a queue that is shutting down.
                jobs, self._jobs = self._jobs, queue.PriorityQueue(maxsize=_MAX_PENDING_EMBEDS + 1)
                if thread is not None and thread.is_alive():
                    # The sentinel outranks queued work, so a large sweep already
                    # in the queue cannot delay shutdown. The worker fails
                    # anything still queued on its way out (_drain_orphans)
                    # rather than leaving a caller waiting on job.done.
                    jobs.put((_PRIORITY_SENTINEL, self._next_seq(), None))
                    self._jobs_changed.set()
        # Join OUTSIDE _lock. The WORKER now holds _lock around inference, so
        # joining while holding it would deadlock against a job that was dequeued
        # just before the sentinel until the timeout expired.
        if thread is not None and thread.is_alive():
            thread.join(_INFER_STOP_TIMEOUT_SECS)
        # Stop the idle evictor too: nothing is loaded for it to watch, and the
        # next load starts a fresh one. Not joined from the evictor itself, which
        # is the caller when the unload IS the eviction.
        self._evict_wake.set()
        evictor = self._evict_thread
        if evictor is not None and evictor is not threading.current_thread() and evictor.is_alive():
            evictor.join(_INFER_STOP_TIMEOUT_SECS)
        return llm, thread
