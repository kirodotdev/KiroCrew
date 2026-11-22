"""Fill session chunk vectors in the gateway, through the one shared embedding model.

The index worker writes chunks with their text in ``pending_text``; this step
embeds a batch of that text at ``PRIORITY_BULK``, so an interactive embed is
served first, and writes each vector and deletes its text in one transaction. It
runs in the gateway because the model is a process-wide singleton the gateway
already holds: the worker loading a second copy is not acceptable.

A vector is stored with the signature of the space it was made in. A batch whose
space changed while it was embedding is discarded, and a vector from another
space is cleared and its session re-queued (``SessionVectorStore.release_stale``).

The fill never creates the store: while no file exists, the chunk pass has
written nothing and there is nothing to fill. It also runs only on the in-process
model (:func:`embeds_on_this_host`), so no transcript text leaves the host.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kiro_crew.history_vectors_index import (
    configured_semantic_search,
    session_embeddable,
    vector_store_for,
    visible_window_keys,
)

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_vectors import SessionVectorStore

_LOGGER = logging.getLogger(__name__)

#: Texts per embed call; the in-process backend splits larger batches anyway.
FILL_BATCH = 8
#: Pause after a batch that wrote vectors while more are pending.
FILL_BUSY_PAUSE_SECS = 1.0
#: Pause when pending text could not be embedded (the model is still loading).
FILL_WAIT_PAUSE_SECS = 15.0
#: Pause when there is nothing to fill, or the flag is off.
FILL_IDLE_PAUSE_SECS = 60.0
#: How often the build's progress is logged while it is running.
PROGRESS_EVERY_SECS = 600.0


def embeds_on_this_host(embedder: object) -> bool:
    """Whether *embedder* runs the model in this process, so no session text leaves the host.

    A positive check: only the in-process llama.cpp backend qualifies. The Ollama
    backend posts text to a server that ``memory.ollama_allow_remote`` lets be
    another machine, and the chunks were cut with the local model's tokenizer.
    """
    from kiro_crew.embeddings_llama import LlamaCppEmbedder

    return isinstance(embedder, LlamaCppEmbedder)


def fill_batch(
    log: "ConversationLog",
    store: "SessionVectorStore",
    *,
    limit: int = FILL_BATCH,
    release: bool = True,
) -> dict[str, int]:
    """Embed up to *limit* pending chunks. Returns ``filled`` and ``pending`` counts.

    *release* first clears vectors from another space; a caller that has already
    done so for the current space passes ``False``, since only a model change
    makes a stored vector stale.

    Just before embedding, each session holding a batch chunk has its privacy
    read again under its lock: a chunk is embedded only while some holder is
    still visible, and a holder that is gone, private or unreadable loses its
    rows here rather than at the next chunk pass.
    """
    from kiro_crew import embeddings
    from kiro_crew.knowledge.embedder import floats_to_bytes

    embedder = embeddings.get_shared_embedder()
    if not embeds_on_this_host(embedder):
        return {"filled": 0, "pending": store.pending_count()}
    # The space of the embedder this batch runs on, not of whichever one is
    # active by now: a swap in between must not label old vectors as new.
    signature = embeddings.embedding_space_signature(embedder.model_id, embedder.dim)
    store.settle()
    if release:
        store.release_stale(signature)
    batch = store.pending_batch(limit)
    if not batch:
        return {"filled": 0, "pending": 0}
    owners = store.chunk_owners(chunk_id for chunk_id, _text in batch)
    states = {key: session_embeddable(log, key) for keys in owners.values() for key in keys}
    restricted = {key for key, state in states.items() if state is False}
    if restricted:
        store.drop(restricted)
    batch = [
        (chunk_id, text)
        for chunk_id, text in batch
        if any(states[key] is True for key in owners.get(chunk_id, ()))
    ]
    if not batch:
        return {"filled": 0, "pending": store.pending_count()}
    vectors = embedder.embed_batch(
        [text for _chunk_id, text in batch], priority=embeddings.PRIORITY_BULK
    )
    if vectors is None or len(vectors) != len(batch):
        return {"filled": 0, "pending": store.pending_count()}
    if embeddings.active_embedding_space_signature() != signature:
        # The model changed while this batch was embedding: these vectors belong
        # to a space nothing will read.
        return {"filled": 0, "pending": store.pending_count()}
    rows = [
        (chunk_id, floats_to_bytes(vector)) for (chunk_id, _text), vector in zip(batch, vectors)
    ]
    filled = store.write_vectors(rows, signature)
    return {"filled": filled, "pending": store.pending_count()}


def coverage(log: "ConversationLog", store: "SessionVectorStore") -> dict[str, int]:
    """How many sessions of the visible search window have every chunk embedded.

    Counts only sessions that are neither incognito nor temporary, so neither
    number reveals how many private sessions exist.
    """
    from kiro_crew import embeddings

    window = visible_window_keys(log)
    embedded = store.embedded_keys(embeddings.active_embedding_space_signature())
    return {
        "embedded": len(window & embedded),
        "window": len(window),
        "pending_chunks": store.pending_count(),
    }


class VectorFill:
    """One gateway-side fill loop over a session directory. :meth:`step` blocks."""

    def __init__(self, log: "ConversationLog") -> None:
        self._log = log
        self._store = vector_store_for(log._dir)
        self._next_progress = time.monotonic() + PROGRESS_EVERY_SECS
        self._released_for = ""

    def step(self) -> float:
        """Fill one batch if there is work; returns how long to wait before the next."""
        if not configured_semantic_search() or not self._store.store_exists():
            self._settle()
            return FILL_IDLE_PAUSE_SECS
        from kiro_crew import embeddings

        embedder = embeddings.get_shared_embedder()
        if not embeds_on_this_host(embedder):
            self._settle()
            return FILL_IDLE_PAUSE_SECS

        signature = embeddings.embedding_space_signature(embedder.model_id, embedder.dim)
        report = fill_batch(
            self._log, self._store, limit=FILL_BATCH, release=signature != self._released_for
        )
        self._released_for = signature
        if report["filled"]:
            self._maybe_log_progress()
        if report["pending"] == 0:
            return FILL_IDLE_PAUSE_SECS
        return FILL_BUSY_PAUSE_SECS if report["filled"] else FILL_WAIT_PAUSE_SECS

    def _settle(self) -> None:
        """Retry an erasure this loop still owes, whatever the flag or provider says now."""
        if not self._store.erasure_owed:
            return
        try:
            self._store.settle()
        except Exception:  # noqa: BLE001 -- the next step retries
            _LOGGER.warning("Session vector store erasure retry failed", exc_info=True)

    def _maybe_log_progress(self) -> None:
        now = time.monotonic()
        if now < self._next_progress:
            return
        self._next_progress = now + PROGRESS_EVERY_SECS
        counts = coverage(self._log, self._store)
        _LOGGER.info(
            "Session meaning search: %d of %d sessions in the search window embedded, "
            "%d chunks pending",
            counts["embedded"],
            counts["window"],
            counts["pending_chunks"],
        )

    def close(self) -> None:
        self._store.close()
