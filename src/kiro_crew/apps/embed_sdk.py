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
