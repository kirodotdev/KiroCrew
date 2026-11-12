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


def _usable_model_path() -> Path | None:
    """The active model file, or ``None`` when it is rejected, protected or absent.

    The tokenizer is opened by native llama.cpp, so this applies the same gates as
    the embedder: a custom path the resolver rejected is not used, and a path the
    sensitive-path gate covers is never handed over.
    """
    from kiro_crew.embeddings_model import (
        _is_sensitive_model_path,
        default_model_path,
        model_file_present,
        resolve_custom_model,
    )

    spec = resolve_custom_model()
    if spec is not None and spec.error:
        return None
    path = spec.path if spec is not None else default_model_path()
    if _is_sensitive_model_path(path) or not model_file_present(path):
        return None
    return path


def _signature_for(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{path.name}:{st.st_size}:{st.st_mtime_ns}:{CHUNK_TOKENS}"


def tokenizer_signature() -> str:
    """The signature :func:`load_tokenizer` would give now; empty while no model is usable."""
    return _signature_for(_usable_model_path())


def load_tokenizer() -> Tokenizer | None:
    """The active embedding model's tokenizer, or ``None`` while no model is usable.

    Native code parses the GGUF header and vocabulary, so the file is judged as the
    embedder judges it: a second name on the bundled weights refuses the load, and
    the stamp taken from that judgement must still hold once the constructor returns.
    """
    from kiro_crew.embeddings_model import (
        _is_sensitive_model_path,
        _model_file_stamp,
        _model_file_stamp_unaliased,
    )
    from kiro_crew.embeddings_native import _load_llama_class

    path = _usable_model_path()
    signature = _signature_for(path)
    llama_class = _load_llama_class() if signature else None
    if path is None or llama_class is None or _is_sensitive_model_path(path):
        return None
    try:
        stamp = _model_file_stamp_unaliased(path)
        if stamp is None:
            return None
        llm = llama_class(model_path=str(path), vocab_only=True, verbose=False)
        if _model_file_stamp(path) != stamp:
            return None
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
    from kiro_crew.history_metadata_line import METADATA_LINE_CORRUPT
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
                # until it can be. A first line that is damaged (not UTF-8 or not
                # JSON) is never fixed by reading it again, so that is no work to
                # come back to; any other cause may pass.
                store.drop([key])
                if log.metadata_line_state(key) == METADATA_LINE_CORRUPT:
                    return WITHHELD
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


def session_embeddable(log: "ConversationLog", key: str) -> bool | None:
    """Whether *key*'s text may still be embedded, read under its lock now.

    ``False`` when the session is gone, private, or its privacy cannot be read,
    the cases where :func:`chunk_session` drops its rows; ``None`` when its lock
    could not be taken, so the answer is not known yet.
    """
    from kiro_crew.history_derivation import TranscriptWithheld
    from kiro_crew.history_search import _history_lock_timeout

    try:
        with log._locked(key):
            if not log._path(key).exists():
                return False
            try:
                log._withhold_if_restricted(key)
            except TranscriptWithheld:  # TranscriptBusy too: privacy that cannot be read
                return False
    except _history_lock_timeout():
        return None
    except OSError:
        return False
    return True


def search_window(log: "ConversationLog") -> list[dict]:
    """The sessions the keyword index covers: the listing, cron runs left out, cut to the window."""
    from kiro_crew.history_search import _facade_search_scan_window, _searchable

    return _searchable(log.list_sessions())[: _facade_search_scan_window()]


def visible_window_keys(log: "ConversationLog") -> set[str]:
    """Keys in :func:`search_window` that are neither incognito nor temporary."""
    from kiro_crew.history_derivation import is_incognito_transcript

    return {
        meta["key"]
        for meta in search_window(log)
        if not is_incognito_transcript(meta.get("memory_mode"))
    }


def backfill_chunks(
    log: "ConversationLog",
    store: SessionVectorStore,
    tokenizer: Tokenizer | None,
    *,
    budget_secs: float = 5.0,
) -> dict[str, int]:
    """Prune the store, then chunk window sessions it does not hold fresh.

    With no *tokenizer* (the flag is off or no model is usable) only the prune
    runs: an owed WAL truncate is retried, and private sessions, sessions whose
    first line cannot be read and sessions that left the window lose their rows,
    so the store keeps the keyword index's privacy contract whatever the flag.
    With no store file nothing runs, so a host that never turned the flag on
    never gets one. Returns the same counts as ``backfill_index`` so the worker
    paces on them.
    """
    from kiro_crew.history_derivation import is_incognito_transcript
    from kiro_crew.history_search import _INDEX_QUIET_WINDOW_SECS

    if tokenizer is None and not store.store_exists():
        return {"indexed": 0, "dropped": 0, "remaining": 0, "deferred": 0}
    store.settle()
    window = search_window(log)
    held = store.indexed_keys()
    # A private session is dropped here, before the quiet window: making a session
    # private rewrites its file, and deferring it would keep its rows meanwhile.
    private = {meta["key"] for meta in window if is_incognito_transcript(meta.get("memory_mode"))}
    private_dropped = len(private & held) if store.drop(private & held) else 0
    window = [meta for meta in window if meta["key"] not in private]
    window_keys = {meta["key"] for meta in window}
    departed = held - window_keys - private
    departed_dropped = len(departed) if store.drop(departed) else 0
    # The listing reads an unreadable first line as persistent, so neither set
    # above holds such a session. Its privacy is unknown, so its rows go, as
    # chunk_session drops them. A readable line is served from the metadata
    # cache, so this costs one stat per held session.
    unreadable = {key for key in (held & window_keys) if not log.get_metadata_status(key)[1]}
    unreadable_dropped = len(unreadable) if store.drop(unreadable) else 0
    dropped = departed_dropped + private_dropped + unreadable_dropped
    if tokenizer is None:
        return {
            "indexed": 0,
            "dropped": dropped,
            "remaining": 1 if store.erasure_owed else 0,
            "deferred": 0,
        }
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
        "dropped": dropped,
        "remaining": outcomes[RETRY] + unreached + (1 if store.erasure_owed else 0),
        "deferred": deferred,
    }
