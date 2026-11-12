"""The chunk pass the session index worker runs when ``session.semantic_search`` is on.

It reads the keyword index's own window (``_searchable`` and
``_facade_search_scan_window``) and quiet period (``_INDEX_QUIET_WINDOW_SECS``),
judges freshness by file identity, and drops a session that leaves the window. Each session is read under its own lock, through the
derivation guard, so an incognito or temporary session is never chunked and loses
any rows it had.

Tokens are counted with the embedding model's tokenizer loaded vocabulary-only:
no weights are read, so the worker never holds a second copy of the model.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from kiro_crew.history_chunks import CHUNK_TOKENS, chunk_messages
from kiro_crew.history_vectors import VECTORS_FILENAME, SessionVectorStore

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

_LOGGER = logging.getLogger(__name__)


#: :func:`chunk_session` outcomes. Only ``RETRY`` counts as remaining work: a
#: withheld or vanished session has had its rows dropped and nothing to redo.
INDEXED, WITHHELD, GONE, RETRY = "indexed", "withheld", "gone", "retry"


class Tokenizer(NamedTuple):
    count_tokens: Callable[[str], int]
    #: Names the model file and the bound, so a change of either re-chunks.
    signature: str


def vector_store_for(sessions_dir: Path) -> SessionVectorStore:
    return SessionVectorStore(Path(sessions_dir) / ".index" / VECTORS_FILENAME)


def configured_semantic_search() -> bool:
    """``session.semantic_search`` as of now; ``False`` when the config cannot be read."""
    from kiro_crew.config import live

    cfg = live.snapshot()
    if cfg is None:
        try:
            from kiro_crew.config.loader import KiroCrewConfig

            cfg = KiroCrewConfig.load()
        except Exception:
            return False
    return getattr(getattr(cfg, "session", None), "semantic_search", False) is True


def tokenizer_signature() -> str:
    """The signature :func:`load_tokenizer` would give now; empty while no model is usable."""
    from kiro_crew.embeddings_model import active_model_path, model_file_present

    path = active_model_path()
    if not model_file_present(path):
        return ""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{path.name}:{st.st_size}:{st.st_mtime_ns}:{CHUNK_TOKENS}"


def load_tokenizer() -> Tokenizer | None:
    """The active embedding model's tokenizer, or ``None`` while no model is usable."""
    from kiro_crew.embeddings_model import active_model_path
    from kiro_crew.embeddings_native import _load_llama_class

    signature = tokenizer_signature()
    llama_class = _load_llama_class() if signature else None
    if llama_class is None:
        return None
    try:
        llm = llama_class(model_path=str(active_model_path()), vocab_only=True, verbose=False)
    except Exception:  # noqa: BLE001 -- no tokenizer means no chunk pass, not a crash
        _LOGGER.warning("Could not load the embedding tokenizer", exc_info=True)
        return None

    def count_tokens(text: str) -> int:
        data = text.encode("utf-8", "surrogatepass")
        return len(llm.tokenize(data, add_bos=False, special=False))

    return Tokenizer(count_tokens, signature)


def chunk_session(
    log: "ConversationLog", store: SessionVectorStore, key: str, tokenizer: Tokenizer
) -> str:
    """Chunk *key* into *store*; returns one of the outcome constants above."""
    from kiro_crew.history_derivation import TranscriptBusy, TranscriptWithheld
    from kiro_crew.history_search import _history_lock_timeout

    try:
        with log._locked(key):
            try:
                st = log._path(key).stat()
            except OSError:
                store.drop([key])
                return GONE
            try:
                log._withhold_if_restricted(key)
            except TranscriptBusy:
                # Its privacy could not be read: nothing derived from it stays
                # until it can be.
                store.drop([key])
                return RETRY
            except TranscriptWithheld:
                store.drop([key])
                return WITHHELD
            try:
                texts = list(log._iter_message_texts(key))
            except OSError:
                return RETRY
            chunks = chunk_messages(texts, tokenizer.count_tokens)
            store.sync_session(key, st, tokenizer.signature, chunks)
    except _history_lock_timeout():
        return RETRY
    return INDEXED


def backfill_chunks(
    log: "ConversationLog",
    store: SessionVectorStore,
    tokenizer: Tokenizer,
    *,
    budget_secs: float = 5.0,
) -> dict[str, int]:
    """Chunk window sessions the store does not hold fresh, within *budget_secs*.

    Returns the same counts as ``backfill_index`` so the worker paces on them.
    """
    from kiro_crew.history_derivation import is_incognito_transcript
    from kiro_crew.history_search import (
        _INDEX_QUIET_WINDOW_SECS,
        _facade_search_scan_window,
        _searchable,
    )

    store.settle()
    window = _searchable(log.list_sessions())[: _facade_search_scan_window()]
    held = store.indexed_keys()
    # A private session is dropped here, before the quiet window: making a session
    # private rewrites its file, and deferring it would keep its rows meanwhile.
    private = {meta["key"] for meta in window if is_incognito_transcript(meta.get("memory_mode"))}
    private_dropped = len(private & held) if store.drop(private & held) else 0
    window = [meta for meta in window if meta["key"] not in private]
    window_keys = {meta["key"] for meta in window}
    stats = {}
    for meta in window:
        try:
            stats[meta["key"]] = log._path(meta["key"]).stat()
        except OSError:
            continue
    fresh = store.fresh_keys(stats, tokenizer.signature)
    quiet_cutoff_ns = time.time_ns() - int(_INDEX_QUIET_WINDOW_SECS * 1e9)
    pending: list[str] = []
    deferred = 0
    for meta in window:
        key = meta["key"]
        if key in fresh:
            continue
        st = stats.get(key)
        if st is not None and st.st_mtime_ns > quiet_cutoff_ns:
            deferred += 1
            continue
        pending.append(key)
    departed = held - window_keys - private
    departed_dropped = len(departed) if store.drop(departed) else 0
    deadline = time.monotonic() + budget_secs
    outcomes = {INDEXED: 0, WITHHELD: 0, GONE: 0, RETRY: 0}
    unreached = 0
    for position, key in enumerate(pending):
        if time.monotonic() > deadline:
            unreached = len(pending) - position
            break
        try:
            outcomes[chunk_session(log, store, key, tokenizer)] += 1
        except Exception:  # noqa: BLE001 -- one bad session must not stop the rest
            # Counted as deferred, not remaining: a session that fails the same
            # way every time must not hold the worker at its busy pace.
            _LOGGER.warning("Chunking session %s failed", key, exc_info=True)
            deferred += 1
    return {
        "indexed": outcomes[INDEXED],
        "dropped": departed_dropped + private_dropped,
        "remaining": outcomes[RETRY] + unreached + (1 if store.erasure_owed else 0),
        "deferred": deferred,
    }
