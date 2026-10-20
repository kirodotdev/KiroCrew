"""The process-wide shared embedder and the embedding space it defines.

Which backend serves the process and how one is built, installed or retired;
the space signature a store's vectors must match and the reconcile and align
steps that bring a store to it; and the cached sync embed function vector
memory calls.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from kiro_crew.embeddings_llama import EmbeddingBackend, LlamaCppEmbedder
from kiro_crew.embeddings_model import (
    _custom_model_id,
    _model_file_stamp,
    default_model_path,
    legacy_embedding_ids,
    model_pooling_type,
    models_dir,
    resolve_custom_model,
)
from kiro_crew.embeddings_settings import (
    _DEFAULT_DIM,
    _MAX_EMBED_CHARS,
    _MODEL_ID,
    _POOLING_TYPE_LAST,
    _REJECTED_MODEL_SENTINEL,
    PRIORITY_NORMAL,
    EmbeddingWork,
    _read_memory_config,
    _work_for_priority,
    embedding_work,
)

logger = logging.getLogger("kiro_crew.embeddings")


class _ReconcilableStore(Protocol):
    """Structural type for a vector store, so this module needs no import of it.

    The read-only probe and the non-clearing reconcile need only these two.
    """

    def recorded_embedding_space(self) -> "str | None": ...

    def reconcile_embedding_space(
        self,
        signature: str,
        *,
        clear_when_unknown: bool = False,
        force: bool = False,
        rebuild_generation: str = "",
    ) -> int: ...

    def recorded_rebuild_generation(self) -> str: ...


class _AlignableStore(_ReconcilableStore, Protocol):
    """A store whose width, signature and space generation alignment may rewrite.

    Every member is required: alignment runs under the store's own lock so it
    cannot race concurrent writers, and a store that lacks one of these fails
    at the attribute access rather than aligning unlocked.
    """

    _embedding_dim: int

    @property
    def _db_lock(self) -> AbstractContextManager[Any]: ...

    def set_embedding_dim(self, dim: int) -> bool: ...

    def begin_space_change(self) -> None: ...

    def _write_meta(self, key: str, value: str) -> None: ...


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
    backend = LlamaCppEmbedder(
        model_path=default_model_path(), dim=_DEFAULT_DIM, model_id=_MODEL_ID, serving=False
    )
    backend._uses_bundled_identity = True
    return backend


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
    with _embedding_alignment_lock, _shared_embedder_lock:
        outgoing = _shared_embedder
        _shared_embedder = embedder
    if outgoing is not None and outgoing is not embedder:
        # Outside the lock: the unload joins the inference thread, and holding
        # the singleton lock across a join would stall every concurrent reader.
        _retire_backend(outgoing)


def embedding_space_signature(model_id: str, dim: int) -> str:
    """Short signature of a vector space: vectors from different sigs are incomparable.

    Single source of truth so vector memory and the knowledge library cannot
    disagree about whether stored vectors are still valid. The knowledge
    library hashes this value into its own per-item signature (which
    additionally covers its content budget); vector memory compares it against
    the signature the database was last embedded under. Because the KB's
    identity is DERIVED from this one rather than assembled beside it, any input
    added here necessarily reaches both consumers.
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


def embedding_rebuild_generation(memory: dict | None = None) -> str:
    """Return the managed request identity; absence leaves upgrades untouched."""
    value = (memory if memory is not None else _read_memory_config()).get(
        "embed_rebuild_generation", ""
    )
    return value if isinstance(value, str) else ""


def store_embedding_space_is_stale(store: "_ReconcilableStore") -> bool:
    """True when *store*'s vectors were NOT produced by the active backend.

    Read-only counterpart to :func:`reconcile_store_embedding_space`, for callers
    that must detect the condition without mutating. An unrecorded space is
    treated as the bundled model's, which is provable: nothing else could have
    written those vectors before space tracking existed.
    """
    generation = embedding_rebuild_generation()
    if generation and store.recorded_rebuild_generation() != generation:
        return True
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
        """True while an apply/re-embed is in flight; applies single-flight on it."""
        with self._lock:
            return self._step in ("applying", "running")


_reembed_progress = ReembedProgress()


def reembed_progress() -> ReembedProgress:
    """Process-wide re-embed progress singleton."""
    return _reembed_progress


def reconcile_store_embedding_space(store: "_AlignableStore") -> int:
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
    backend = get_shared_embedder()
    active = embedding_space_signature(backend.model_id, backend.dim)
    if active == default_embedding_space_signature() and not backend.is_ready():
        return store.reconcile_embedding_space(active, clear_when_unknown=False)
    return align_store_embedding_space(store)


_embedding_alignment_lock = threading.RLock()


def align_store_embedding_space(store: "_AlignableStore") -> int:
    """Align a store against one ready backend without racing its replacement."""
    with _embedding_alignment_lock:
        return _align_store_embedding_space(store)


def _align_store_embedding_space(store: "_AlignableStore") -> int:
    """Align width and signature from ONE ready backend, including late opens.

    A gated candidate may align stores only after its identity and width are
    persisted. No model load or inference happens while holding a store lock.
    """
    backend = get_shared_embedder()
    dim = backend.dim
    if isinstance(dim, bool) or not isinstance(dim, int) or not 0 < dim <= 65_536:
        return 0
    active = embedding_space_signature(backend.model_id, dim)
    if not backend.is_ready():
        return 0
    # The store's lock is shared with readers and generation-guarded writers.
    with store._db_lock:
        memory = _read_memory_config()
        if isinstance(backend, LlamaCppEmbedder) and not backend._serving:
            configured_id = (
                memory.get("embed_model_id") if memory.get("embed_model_path") else _MODEL_ID
            )
            if backend.model_id != configured_id or dim != memory.get(
                "embedding_dim", _DEFAULT_DIM
            ):
                return 0
        generation = embedding_rebuild_generation(memory)
        if generation and store.recorded_rebuild_generation() != generation:
            # Do not let an outgoing loaded backend acknowledge the request for
            # its replacement, including a same-width change in another writer.
            configured_id = (
                memory.get("embed_model_id") if memory.get("embed_model_path") else _MODEL_ID
            )
            if backend.model_id != configured_id or dim != memory.get(
                "embedding_dim", _DEFAULT_DIM
            ):
                return 0
            store.set_embedding_dim(dim)
            return store.reconcile_embedding_space(
                active, clear_when_unknown=True, rebuild_generation=generation
            )
        legacy_ids = legacy_embedding_ids(memory.get("embed_model_legacy_ids"))
        if (
            legacy_ids
            and isinstance(backend, LlamaCppEmbedder)
            and backend.model_path == Path(str(memory.get("embed_model_path", ""))).expanduser()
            and backend.model_id == memory.get("embed_model_id")
            # The legacy vectors were made with last-token pooling, so they are
            # this space only while the model still runs with it.
            and model_pooling_type(backend.model_id) == _POOLING_TYPE_LAST
            and store._embedding_dim == dim
            and store.recorded_embedding_space()
            in {embedding_space_signature(label, dim) for label in legacy_ids}
        ):
            try:
                stamp_matches = list(_model_file_stamp(backend.model_path)) == memory.get(
                    "embed_model_stamp"
                )
            except OSError:
                stamp_matches = False
            if stamp_matches:
                store._write_meta("embedding_space_sig", active)
                return 0
        if store.set_embedding_dim(dim) or store.recorded_embedding_space() not in (None, active):
            store.begin_space_change()
        return store.reconcile_embedding_space(
            active, clear_when_unknown=active != default_embedding_space_signature()
        )


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
        backend = LlamaCppEmbedder()
        backend._uses_bundled_identity = True
        return backend
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
        if _shared_embedder is not None:
            return _shared_embedder
        if _backend_factory is not None:
            _shared_embedder = _backend_factory()
            return _shared_embedder
    # The default constructor does not load native weights. Verification may
    # read them on a worker, so it must not hold the lock needed by loop readers.
    candidate = default_embedding_backend()
    with _shared_embedder_lock:
        if _shared_embedder is None:
            if candidate.model_id == "custom:unverified":
                return candidate
            _shared_embedder = candidate
        return _shared_embedder


def peek_ready_shared_embedder() -> EmbeddingBackend | None:
    """Return an existing ready backend without constructing or warming one."""
    with _shared_embedder_lock:
        backend = _shared_embedder
    if backend is None or not backend.is_ready():
        return None
    return backend


def peek_shared_embedding_identity() -> tuple[str | None, str]:
    """Snapshot declared identity without constructing or loading a backend.

    This does not inspect model files, infer, check readiness or prove model
    equivalence. Custom/registered backends remain qualified as such even when
    they declare the bundled id and width. Metadata getters are the backend's
    existing constructor-time identity contract.
    """
    with _shared_embedder_lock:
        backend = _shared_embedder
        registered = _backend_factory is not None
    source = "custom_or_registered" if registered else "unknown"
    if backend is None:
        return None, source
    source = (
        "bundled"
        if not registered
        and type(backend) is LlamaCppEmbedder
        and getattr(backend, "_uses_bundled_identity", False)
        else "custom_or_registered"
    )
    try:
        model_id, dim = backend.model_id, backend.dim
    except Exception:
        return None, source
    if (
        not isinstance(model_id, str)
        or not model_id
        or len(model_id) > 512
        or isinstance(dim, bool)
        or not isinstance(dim, int)
        or not 0 < dim <= 65_536
    ):
        return None, source
    return embedding_space_signature(model_id, dim), source


def reset_shared_embedder() -> None:
    """Drop the singleton (tests, disable-embeddings, KIROCREW_HOME changes)."""
    global _shared_embedder
    with _embedding_alignment_lock, _shared_embedder_lock:
        if _shared_embedder is not None:
            _retire_backend(_shared_embedder)
        _shared_embedder = None


# ── Sync embed function (vector-memory interface) ──

# 128 entries × 1024 floats/entry × 32 bytes/float (24-byte object + 8-byte ptr) ≈ 4 MB.
_EMBED_CACHE_MAX = 128


_sync_embed_cache: "OrderedDict[tuple[str, str], tuple[float, ...]]" = OrderedDict()
_sync_embed_cache_lock = threading.Lock()
_sync_embed_cache_backend: EmbeddingBackend | None = None
# Stripes guard only in-flight bookkeeping, never inference or another waiter.
_sync_embed_stripes = tuple(threading.Lock() for _ in range(16))


@dataclass
class _EmbedFlight:
    work: EmbeddingWork
    done: threading.Event = field(default_factory=threading.Event)
    vector: list[float] | None = None


_sync_embed_flights: dict[tuple[int, str], _EmbedFlight] = {}


def _shared_sync_embed(
    text: str, *, priority: int = PRIORITY_NORMAL, backend: EmbeddingBackend | None = None
) -> list[float] | None:
    global _sync_embed_cache_backend
    text = text[:_MAX_EMBED_CHARS]
    backend = backend if backend is not None else get_shared_embedder()
    key = (backend.model_id, text)
    flight_key = (id(backend), text)
    work = _work_for_priority(priority)
    if work.expired():
        return None
    stripe = _sync_embed_stripes[hash(flight_key) % len(_sync_embed_stripes)]
    with stripe, _sync_embed_cache_lock:
        if _sync_embed_cache_backend is not backend:
            _sync_embed_cache.clear()
            _sync_embed_cache_backend = backend
        cached = _sync_embed_cache.get(key)
        if cached is not None:
            _sync_embed_cache.move_to_end(key)
            return list(cached)
        flight = _sync_embed_flights.get(flight_key)
        owner = flight is None
        if flight is None:
            if len(_sync_embed_flights) >= _EMBED_CACHE_MAX:
                return None
            flight = _EmbedFlight(work)
            _sync_embed_flights[flight_key] = flight
    if not owner:
        promote = getattr(backend, "promote_pending", None)
        if callable(promote):
            promote(flight.work, priority)
        while not flight.done.wait(0.05):
            if work.expired():
                return None
        return list(flight.vector) if flight.vector is not None and not work.expired() else None
    token = embedding_work.set(work)
    try:
        vector = backend.embed(text, priority=priority)
        if vector is None:
            return None
        # A vector the native call did return is correct whatever the owner's
        # deadline; publish and cache it so a waiter with a longer budget, and
        # the next caller, do not pay for the same inference again. Only the
        # owner's own return value honours the owner's deadline.
        flight.vector = list(vector)
        with _sync_embed_cache_lock:
            if _sync_embed_cache_backend is backend:
                _sync_embed_cache[key] = tuple(vector)
                _sync_embed_cache.move_to_end(key)
                while len(_sync_embed_cache) > _EMBED_CACHE_MAX:
                    _sync_embed_cache.popitem(last=False)
        return None if work.expired() else list(vector)
    finally:
        embedding_work.reset(token)
        with stripe, _sync_embed_cache_lock:
            _sync_embed_flights.pop(flight_key, None)
            flight.done.set()


_shared_sync_embed.accepts_priority = True  # type: ignore[attr-defined]


def embedding_in_flight(text: str) -> bool:
    """Whether the shared embedder is computing the vector for *text* right now.

    A caller that embeds *text* while this holds joins that inference, and a joined
    wait ends at the caller's own deadline even while the inference runs. Starts,
    loads and reads nothing beyond the in-flight record.
    """
    with _shared_embedder_lock:
        backend = _shared_embedder
    if backend is None:
        return False
    with _sync_embed_cache_lock:
        return (id(backend), text[:_MAX_EMBED_CHARS]) in _sync_embed_flights


def make_sync_embed_fn() -> Callable[[str], "list[float] | None"]:
    """Return a sync callable ``(str) -> list[float] | None`` over the shared embedder.

    All stores and callers share ONE bounded cache and inference backend.
    Concurrent identical texts are coalesced; failures are never cached, so a
    missing model or saturated queue can be retried. Backend replacement clears
    the cache even when the new backend advertises the same model id.
    """

    return _shared_sync_embed
