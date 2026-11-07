"""Embed SDK — the gateway's shared embedder and its bounded worker pool, for apps.

An app that ranks or deduplicates its own records by meaning needs vectors.
Without this SDK the only ways to get them are to import ``kiro_crew.embeddings``
(a private name an installed app breaks on whenever the wheel moves) or to load a
second model of its own. A second model costs its full resident size again (the
bundled GGUF is several hundred MB) and produces vectors from a different space,
so the app's stored vectors and the host's memory would never be comparable.

**One model, one pool.** :meth:`EmbedSDK.embed_sync` asks the process-wide
embedder the host's own memory uses, so an app's stored vectors keep the host's
model id and width. :meth:`EmbedSDK.run` offloads to the same bounded embed pool
the host's memory builds use, so embed work cannot starve the shared default
executor that fast file and socket offloads ride on.

**No permission gates it.** Like :class:`~kiro_crew.apps.scrub_sdk.ScrubSDK` it
grants no reach an in-gateway app lacks: the app's Python runs in the gateway
process and could import the embedder anyway. What the seam adds is a public name
that survives a wheel upgrade, and the one-model guarantee. It is therefore always
populated on :class:`~kiro_crew.apps.context.AppContext`.

**Batch embed.** :meth:`EmbedSDK.embed_batch` returns vectors with the space
identity (model id, width, signature) and an explicit not-ready answer. Fair use
comes from the call itself, not a manifest key: it runs at the shared model's bulk
priority behind the host's own memory work and takes at most
:data:`MAX_BATCH_TEXTS` texts.

**Never raises into the app.** A missing embedding extra, a model that cannot
load, or an embed that throws all read as "no vector" (``None``), which is the
host's own contract for an embedding backend: the caller degrades to keyword
search. The first failure is logged once per process, so a broken model does not
write a traceback per stored record.

Both host modules are imported lazily, inside the call: this module stays cheap
to import and aiohttp-free, like the context that carries it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Once per process and per message, not per SDK instance: every app context
# carries its own instance, and the failure being reported is the shared model's.
# The set is bounded by the handful of fixed messages below.
_logged: set[str] = set()
_logged_lock = threading.Lock()


def _log_once(message: str, *, exc_info: bool = True) -> None:
    with _logged_lock:
        if message in _logged:
            return
        _logged.add(message)
    logger.warning(message, exc_info=exc_info)


def _backend() -> Any | None:
    """The shared embedder, constructed if needed (no model weights are loaded)."""
    try:
        from kiro_crew import embeddings_shared
    except ImportError:  # pragma: no cover - embeddings are an optional extra
        return None
    try:
        return embeddings_shared.get_shared_embedder()
    except Exception:
        _log_once("App embed SDK: the shared embedder is unavailable")
        return None


def _can_serve(backend: Any) -> bool:
    """Whether *backend* can produce a vector: loaded now, or loadable on demand.

    Loadable counts because the host unloads an idle model; gating on
    ``is_ready()`` alone would store every new record without a vector until
    something else reloaded it.
    """
    try:
        if backend.is_ready():
            return True
        can_load = getattr(backend, "can_load_on_demand", None)
        return bool(callable(can_load) and can_load())
    except Exception:
        _log_once("App embed SDK: the embedder's readiness probe failed")
        return False


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class EmbedSDK:
    """The gateway's shared embedder and its bounded pool.

    Vectors match what the host's memory uses. Carries no app identity: there is
    nothing to attribute, and a default instance needs no app name to invent.
    """

    def available(self) -> bool:
        """True when an embed can produce a vector: the model is ready, or loadable.

        May read model metadata from disk on first use, so call it off the event
        loop (inside :meth:`run` or another worker), like :meth:`embed_sync`.
        """
        backend = _backend()
        return backend is not None and _can_serve(backend)

    @property
    def model_id(self) -> str:
        """The vector space's model identifier, or ``""`` when there is no backend.

        Store it beside each vector: a later model change makes old vectors
        incomparable, and comparing this value is how an app notices.
        """
        backend = _backend()
        if backend is None:
            return ""
        try:
            model_id = backend.model_id
        except Exception:
            _log_once("App embed SDK: the embedder's model id is unreadable")
            return ""
        return model_id if isinstance(model_id, str) else ""

    def embed_sync(self, text: str) -> list[float] | None:
        """Embed *text*; ``None`` when no model can serve or the embed failed.

        BLOCKING, possibly for a model load: call it only inside :meth:`run` or
        another worker thread. Called on a running event loop it refuses (returns
        ``None`` and logs once) instead of stalling every request the gateway is
        serving.
        """
        if _on_event_loop():
            _log_once(
                "App embed SDK: embed_sync called on the event loop; use ctx.embed.run",
                exc_info=False,
            )
            return None
        backend = _backend()
        if backend is None or not _can_serve(backend):
            return None
        try:
            vector = backend.embed(text)
        except Exception:
            _log_once("App embed SDK: an embed raised")
            return None
        if vector is None:
            return None
        return [float(v) for v in vector]

    async def run(self, func: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run ``func(*args, **kwargs)`` on the host's bounded embed pool.

        The call is bound before it is handed over, so no keyword an app passes
        can reach the pool's own parameters.
        """
        from kiro_crew.executors import run_in_embed_pool

        return await run_in_embed_pool(functools.partial(func, *args, **kwargs))

    def embed_batch_sync(
        self, texts: list[str], *, instruction: str | None = None
    ) -> EmbedBatchResult:
        """Embed *texts* in one call; the result says why when it carries no vectors.

        BLOCKING like :meth:`embed_sync`: call it inside :meth:`run` or another
        worker, or use :meth:`embed_batch`.
        Runs at the shared model's bulk priority, so the host's memory recall and
        memory writes are served first. At most :data:`MAX_BATCH_TEXTS` texts.

        *instruction*, when given, is prefixed to every text in the
        ``Instruct: ...\\nQuery:`` form the instruction-tuned bundled model expects.
        Store vectors with :attr:`EmbedBatchResult.space_signature`: vectors from
        different signatures are not comparable.
        """
        if isinstance(texts, (list, tuple)) and len(texts) > MAX_BATCH_TEXTS:
            return EmbedBatchResult(reason=REASON_TOO_MANY_TEXTS)
        prepared = _prepare(texts, instruction)
        if prepared is None:
            return EmbedBatchResult(reason=REASON_INVALID_INPUT)
        if _on_event_loop():
            _log_once(
                "App embed SDK: embed_batch_sync called on the event loop; "
                "use ctx.embed.embed_batch",
                exc_info=False,
            )
            return EmbedBatchResult(reason=REASON_ON_EVENT_LOOP)
        backend = _backend()
        if backend is None or not _can_serve(backend):
            return EmbedBatchResult(reason=REASON_NOT_READY)
        from kiro_crew.embeddings_settings import PRIORITY_BULK
        from kiro_crew.embeddings_shared import embedding_space_signature

        try:
            was_ready = bool(backend.is_ready())
            model_id = backend.model_id
            raw = backend.embed_batch(prepared, priority=PRIORITY_BULK)
        except Exception:
            _log_once("App embed SDK: a batch embed raised")
            return EmbedBatchResult(reason=REASON_FAILED)
        if raw is None:
            # A cold model answers bulk work with None while the load it kicked
            # runs; a retry shortly after is served.
            return EmbedBatchResult(reason=REASON_FAILED if was_ready else REASON_NOT_READY)
        vectors = [[float(v) for v in vector] for vector in raw]
        dim = len(vectors[0]) if vectors else 0
        if (
            len(vectors) != len(prepared)
            or dim == 0
            or any(len(v) != dim for v in vectors)
            or not isinstance(model_id, str)
            or not model_id
        ):
            _log_once("App embed SDK: a batch embed returned malformed vectors", exc_info=False)
            return EmbedBatchResult(reason=REASON_FAILED)
        return EmbedBatchResult(
            ready=True,
            vectors=vectors,
            model_id=model_id,
            dim=dim,
            space_signature=embedding_space_signature(model_id, dim),
        )

    async def embed_batch(
        self, texts: list[str], *, instruction: str | None = None
    ) -> EmbedBatchResult:
        """:meth:`embed_batch_sync` on the host's bounded embed pool, off the event loop."""
        return await self.run(self.embed_batch_sync, texts, instruction=instruction)


#: Most texts one :meth:`EmbedSDK.embed_batch` call accepts. A larger request is
#: refused rather than queued, so one app call cannot hold the shared model for
#: long; split the work across calls.
MAX_BATCH_TEXTS = 64
#: Longest *instruction* accepted, in characters.
MAX_INSTRUCTION_CHARS = 512

REASON_INVALID_INPUT = "invalid_input"
REASON_TOO_MANY_TEXTS = "too_many_texts"
REASON_ON_EVENT_LOOP = "on_event_loop"
REASON_NOT_READY = "not_ready"
REASON_FAILED = "failed"


@dataclass(frozen=True)
class EmbedBatchResult:
    """One batch embed's answer.

    ``ready`` is True only with one vector per input text, in input order.
    Otherwise ``vectors`` is empty and ``reason`` is one of the ``REASON_*``
    values: ``not_ready`` means the model is downloading, loading or absent, so
    fall back (keyword search) and retry later.
    """

    ready: bool = False
    vectors: list[list[float]] = field(default_factory=list)
    model_id: str = ""
    dim: int = 0
    space_signature: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for an app route to return as-is."""
        return {
            "ready": self.ready,
            "vectors": self.vectors,
            "model_id": self.model_id,
            "dim": self.dim,
            "space_signature": self.space_signature,
            "reason": self.reason,
        }


def _prepare(texts: Any, instruction: Any) -> list[str] | None:
    """*texts* with the instruction prefix applied and capped, or None when malformed."""
    from kiro_crew.embeddings_settings import _MAX_EMBED_CHARS

    if not isinstance(texts, (list, tuple)) or not texts:
        return None
    if not all(isinstance(t, str) and t.strip() for t in texts):
        return None
    prefix = ""
    if instruction is not None:
        if not isinstance(instruction, str) or not instruction.strip():
            return None
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            return None
        prefix = f"Instruct: {instruction.strip()}\nQuery:"
    return [prefix + t[:_MAX_EMBED_CHARS] for t in texts]
