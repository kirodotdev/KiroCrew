"""Session search by meaning: the vector leg, and the route that decides which leg answers.

RFC ``rfc-session-semantic-search`` sections 7.2 to 7.4 and 8.3. The keyword leg is
``SessionCatalogProjection._search_parsed``; this module ranks the same window by the
cosine of the query against the chunk vectors the phase 2 fill wrote, then routes:

- A query of at least ``SEMANTIC_LITERAL_MIN_CHARS`` characters that occurs whole in
  the keyword top session is an identifier: both legs are fused, that session first.
- Otherwise the vector ranking leads, and keyword rows the vector leg did not return
  follow it. While fewer than ``SEMANTIC_MIN_COVERAGE`` of the visible sessions in the
  scan window are embedded, keyword rows lead instead, so a half-built index never
  displaces them.
- With ``session.semantic_search`` off, no store, no in-process model, numpy absent or
  no vector to score, the keyword rows are returned unchanged.

The leg only proposes keys from the window the keyword leg read, after incognito and
temporary sessions are removed, and every row it adds goes through the same
``_result_rows`` gates (the ``keys`` re-judge under the lock, and the derivation guard
for its snippet). So a session deleted or made private after the matrix loaded never
ranks: it is outside the window, whatever the matrix still holds.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.history_vectors import SessionVectorStore

_LOGGER = logging.getLogger(__name__)

#: A literal query must be at least this long (RFC 7.3, open question 5, set D).
SEMANTIC_LITERAL_MIN_CHARS = 12
#: Below this share of visible sessions embedded, keyword rows lead (RFC 8.3).
SEMANTIC_MIN_COVERAGE = 0.9
#: Sessions the vector leg contributes to one query (RFC 7.4).
SEMANTIC_MAX_ROWS = 10
#: The row (0-based) the keyword top session is held at, at the latest, when the
#: vector ranking leads a non-literal query.
_KEYWORD_TOP_ROW = 1
#: Fusion constants of the literal route, as the recall sets measured them.
_RRF_K = 60
_VECTOR_RRF_WEIGHT = 1.5
#: Query vectors kept, by text and vector space (RFC 8.4).
_QUERY_MEMO_SIZE = 64
#: The matrix is rebuilt from the store at most this often while the store changes.
_RELOAD_MIN_SECS = 60.0

TEXT, MEANING = "text", "meaning"

#: An override of ``session.semantic_search``, for the recall harness. ``None``
#: reads the config at each search; see :func:`semantic_search_ready`.
_SEMANTIC_SEARCH_OVERRIDE: bool | None = None

# Optional dependency, as in knowledge/retrieval.py: imported on first use only.
_HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class MeaningHit(NamedTuple):
    """One session the vector leg ranks, and where its best chunk sits in the transcript."""

    key: str
    score: float
    message_index: int
    char_start: int
    char_end: int


class Routed(NamedTuple):
    """One output row's session, how it matched, and its chunk for a meaning row."""

    meta: dict
    match: str
    hit: MeaningHit | None


class _Matrix:
    """The live space's vectors, normalised, one row per chunk reference."""

    def __init__(self, signature: str, fingerprint: object, rows: list, array: Any) -> None:
        self.signature = signature
        self.fingerprint = fingerprint
        self.rows = rows  # (key, message_index, char_start, char_end), aligned with array
        self.array = array
        self.loaded_at = time.monotonic()


def semantic_search_ready() -> bool:
    """Whether the leg may run at all: the flag on and numpy importable."""
    if not _HAS_NUMPY:
        return False
    if _SEMANTIC_SEARCH_OVERRIDE is not None:
        return _SEMANTIC_SEARCH_OVERRIDE
    from kiro_crew.history_vectors_index import configured_semantic_search

    return configured_semantic_search()


def local_embedder() -> Any | None:
    """The shared embedder when it runs in this process, else ``None`` (no text leaves the host)."""
    from kiro_crew import embeddings
    from kiro_crew.history_vectors_fill import embeds_on_this_host

    embedder = embeddings.get_shared_embedder()
    return embedder if embeds_on_this_host(embedder) else None


class _LegState:
    """What one session directory's legs share: the matrix, the query memo and one timer.

    A process builds a ``ConversationLog`` per call in places (each MCP
    ``search_chat_history`` call does), so the state is held per store file, not per
    leg: every leg over one directory reuses one matrix, and one idle timer releases
    it, however many legs were built.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.matrix: _Matrix | None = None
        self.memo: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self.idle_timer: threading.Timer | None = None

    def release(self) -> None:
        """Drop the matrix and the query memo; the next meaning query reloads them."""
        with self.lock:
            self.matrix = None
            self.memo.clear()
            if self.idle_timer is not None:
                self.idle_timer.cancel()
                self.idle_timer = None


_STATES: dict[str, _LegState] = {}
_STATES_LOCK = threading.Lock()


def reset_for_tests() -> None:
    """Release every directory's matrix and timer and forget them, for a test's isolation."""
    with _STATES_LOCK:
        states = list(_STATES.values())
        _STATES.clear()
    for state in states:
        state.release()


def _state_for(store: "SessionVectorStore") -> _LegState:
    """The state every leg over *store*'s file shares, created on first use."""
    key = str(store.db_path)
    with _STATES_LOCK:
        state = _STATES.get(key)
        if state is None:
            state = _STATES[key] = _LegState()
        return state


class VectorLeg:
    """The meaning leg over one session directory's chunk store."""

    def __init__(self, store: "SessionVectorStore") -> None:
        self._store = store
        self._state = _state_for(store)

    def signature(self) -> tuple[Any, str] | None:
        """``(embedder, signature)`` when the leg can answer now, else ``None``."""
        # The store's existence first: a stat, while the flag is a config read, which
        # the MCP server takes from the file.
        if not self._store.store_exists() or not semantic_search_ready():
            return None
        embedder = local_embedder()
        if embedder is None:
            return None
        from kiro_crew import embeddings

        return embedder, embeddings.embedding_space_signature(embedder.model_id, embedder.dim)

    def rank(
        self,
        query: str,
        candidates: dict[str, float],
        embedder: Any,
        signature: str,
        accept: Callable[[str], bool] | None = None,
    ) -> list[MeaningHit] | None:
        """The best chunk per candidate session, best first, at most ``SEMANTIC_MAX_ROWS``.

        *candidates* maps each session the leg may return to its modification time, the
        tiebreak, newer first. *accept*, when given, is asked best first and the cap
        counts only the sessions it keeps, so a caller that filters the rows afterwards
        (``search_chat_history``'s workspace and dates) never loses an eligible hit to
        ones it drops. ``None`` when no query vector or no stored vector exists.
        """
        if not candidates:
            return None
        query_vector = self._query_vector(query, embedder, signature)
        if query_vector is None:
            return None
        state = self._state
        with state.lock:
            matrix = self._current_matrix(signature, embedder.dim)
            self._arm_idle_release()
        if matrix is None or not matrix.rows:
            return None
        scores = matrix.array @ query_vector
        best: dict[str, tuple[float, int]] = {}
        for position, score in enumerate(scores.tolist()):
            key = matrix.rows[position][0]
            if key in candidates and score > best.get(key, (-2.0, 0))[0]:
                best[key] = (score, position)
        ranked = sorted(
            best.items(), key=lambda item: (item[1][0], candidates[item[0]]), reverse=True
        )
        hits: list[MeaningHit] = []
        for key, (score, position) in ranked:
            if len(hits) >= SEMANTIC_MAX_ROWS:
                break
            if accept is not None and not accept(key):
                continue
            _key, message_index, char_start, char_end = matrix.rows[position]
            hits.append(MeaningHit(key, float(score), message_index, char_start, char_end))
        return hits

    def _query_vector(self, query: str, embedder: Any, signature: str) -> Any | None:
        import numpy as np

        from kiro_crew import embeddings

        state = self._state
        memo_key = (query, signature)
        with state.lock:
            cached = state.memo.get(memo_key)
            if cached is not None:
                state.memo.move_to_end(memo_key)
                return cached
        vector = embedder.embed(
            query, priority=embeddings.PRIORITY_INTERACTIVE, role=embeddings.EMBED_ROLE_QUERY
        )
        if not vector or len(vector) != embedder.dim:
            return None
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        if norm == 0.0:
            return None
        array /= norm
        with state.lock:
            state.memo[memo_key] = array
            while len(state.memo) > _QUERY_MEMO_SIZE:
                state.memo.popitem(last=False)
        return array

    def _current_matrix(self, signature: str, dim: int) -> _Matrix | None:
        """The loaded matrix, rebuilt when the space changed or the store moved on.

        Called with the shared state's lock held.
        """
        state = self._state
        matrix = state.matrix
        try:
            fingerprint = self._store.vectors_fingerprint(signature)
        except Exception:  # noqa: BLE001 -- an unreadable store answers nothing
            _LOGGER.warning("Session vector store could not be read", exc_info=True)
            return None
        if matrix is not None and matrix.signature == signature:
            if matrix.fingerprint == fingerprint:
                return matrix
            if time.monotonic() - matrix.loaded_at < _RELOAD_MIN_SECS:
                return matrix
        state.matrix = _load_matrix(self._store, signature, dim, fingerprint)
        return state.matrix

    def current_matrix(self, signature: str, dim: int) -> _Matrix | None:
        """:meth:`_current_matrix` under the shared lock, for the recall harness."""
        with self._state.lock:
            return self._current_matrix(signature, dim)

    def _arm_idle_release(self) -> None:
        """Release the matrix after the embedder's own idle time without a meaning query.

        Called with the shared state's lock held; one timer per directory.
        """
        from kiro_crew import embeddings

        state = self._state
        if state.idle_timer is not None:
            state.idle_timer.cancel()
            state.idle_timer = None
        idle_secs = embeddings.embedding_idle_evict_secs()
        if idle_secs <= 0:
            # 0 keeps the model loaded for good; the matrix stays with it.
            return
        state.idle_timer = _start_idle_timer(idle_secs, state.release)

    def release(self) -> None:
        """Drop the directory's matrix and query memo; the next meaning query reloads them."""
        self._state.release()


def _start_idle_timer(secs: float, callback: Any) -> threading.Timer:
    """A daemon timer calling *callback* after *secs*, already started."""
    timer = threading.Timer(secs, callback)
    timer.daemon = True
    timer.start()
    return timer


def _load_matrix(
    store: "SessionVectorStore", signature: str, dim: int, fingerprint: object
) -> _Matrix | None:
    import numpy as np

    try:
        rows = store.vector_rows(signature)
    except Exception:  # noqa: BLE001 -- an unreadable store answers nothing
        _LOGGER.warning("Session vectors could not be read", exc_info=True)
        return None
    keep: list[tuple[str, int, int, int]] = []
    vectors = []
    for key, message_index, char_start, char_end, blob in rows:
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.shape[0] != dim:
            continue
        keep.append((key, message_index, char_start, char_end))
        vectors.append(vector)
    if not vectors:
        return _Matrix(signature, fingerprint, [], np.zeros((0, dim), dtype=np.float32))
    array = np.vstack(vectors)
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return _Matrix(signature, fingerprint, keep, array / norms)


def is_literal(query: str, top_folded: str | None) -> bool:
    """RFC 7.3: long enough, and the whole query occurs in the keyword top session."""
    if top_folded is None or len(query) < SEMANTIC_LITERAL_MIN_CHARS:
        return False
    return query.casefold() in top_folded


def route(
    keyword: Sequence[dict],
    hits: Sequence[MeaningHit],
    metas: dict[str, dict],
    *,
    literal: bool,
    coverage: float,
) -> list[Routed]:
    """The routed order of *keyword* rows and the vector leg's *hits* (RFC 7.3 and 8.3)."""
    by_hit = {hit.key: hit for hit in hits}
    keyword_keys = [meta["key"] for meta in keyword]
    in_keyword = set(keyword_keys)
    if literal and keyword:
        fused = _rrf(keyword_keys, [hit.key for hit in hits])
        top = keyword_keys[0]
        order = [top] + [key for key in fused if key != top]
        return [
            Routed(
                metas[key],
                TEXT if key in in_keyword else MEANING,
                None if key in in_keyword else by_hit[key],
            )
            for key in order
        ]
    keyword_rows = [Routed(meta, TEXT, None) for meta in keyword]
    # A session the keyword leg also matched is a text row wherever the vector
    # ranking puts it: it holds the typed words, so it keeps the keyword excerpt.
    meaning_rows = [
        (
            Routed(metas[hit.key], TEXT, None)
            if hit.key in in_keyword
            else Routed(metas[hit.key], MEANING, hit)
        )
        for hit in hits
        if hit.key not in in_keyword or coverage >= SEMANTIC_MIN_COVERAGE
    ]
    if coverage < SEMANTIC_MIN_COVERAGE:
        return keyword_rows + meaning_rows
    leading = {row.meta["key"] for row in meaning_rows}
    rows = meaning_rows + [row for row in keyword_rows if row.meta["key"] not in leading]
    if keyword:
        # Row one stays the vector ranking's (RFC 7.3 measured moving the keyword top
        # there for short queries as a loss), but the keyword top is held no lower
        # than row two, so a short identifier's only text match is never pushed past
        # a caller's first rows by vector hits that do not contain it.
        position = next(i for i, row in enumerate(rows) if row.meta["key"] == keyword_keys[0])
        if position > _KEYWORD_TOP_ROW:
            rows.insert(_KEYWORD_TOP_ROW, rows.pop(position))
    return rows


def _rrf(keyword_keys: Sequence[str], vector_keys: Sequence[str]) -> list[str]:
    """Both legs fused by reciprocal rank, through the knowledge retriever's own fusion."""
    from kiro_crew.knowledge.retrieval import HybridRetriever

    fused = HybridRetriever._rrf_fuse(
        [(key, rank) for rank, key in enumerate(keyword_keys, start=1)],
        [(key, rank) for rank, key in enumerate(vector_keys, start=1)],
        k=_RRF_K,
        weights=(1.0, _VECTOR_RRF_WEIGHT),
    )
    return [key for key, _score in fused]


def chunk_snippet(
    texts: Sequence[str],
    hit: MeaningHit,
    width: int,
    redact: Callable[[str], str] | None = None,
) -> str:
    """The matched chunk cut to *width* characters around its centre, whitespace folded.

    With *redact*, the whole message is redacted before anything is cut, so a
    credential whose label sits outside the excerpt is still hidden: the excerpt is
    a slice of the redacted message. The chunk's offsets are mapped into it by
    redacting the text before and after the chunk, which only places the slice.
    """
    if not 0 <= hit.message_index < len(texts):
        return ""
    text = texts[hit.message_index]
    start, end = max(0, hit.char_start), min(len(text), hit.char_end)
    if start >= end:
        return ""
    if redact is not None:
        redacted = redact(text)
        if redacted != text:
            start = min(len(redacted), len(redact(text[:start])))
            end = max(start, len(redacted) - len(redact(text[end:])))
            text = redacted
            if start >= end:
                return ""
    if end - start > width:
        centre = (start + end) // 2
        start = max(start, centre - width // 2)
        end = min(end, start + width)
    frag = " ".join(text[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + frag + suffix


def meaning_snippet(log: Any, key: str, hit: MeaningHit) -> str | None:
    """*hit*'s chunk re-read from *key*'s transcript, or ``None`` when its privacy refuses it.

    The chunk's text left the store when its vector was written, so the snippet is
    read again, under the transcript lock and through the derivation guard, from the
    message and offsets the chunk recorded (RFC 7.4).
    """
    from kiro_crew.history_derivation import TranscriptWithheld
    from kiro_crew.history_search import _history_lock_timeout

    try:
        with log._locked(key):
            log._withhold_if_restricted(key)
            texts = list(log._iter_message_texts(key))
    except TranscriptWithheld:
        return None
    except _history_lock_timeout():
        return None
    except OSError:
        return ""
    from kiro_crew.security import redact

    return chunk_snippet(texts, hit, log._SNIPPET_MAX, redact)


def meaning_route(
    projection: Any,
    query: str,
    needles: Sequence[Any],
    window: list[dict],
    scored: Sequence[tuple[Any, ...]],
    limit: int,
    accept: Callable[[dict], bool] | None = None,
) -> tuple[list[Routed], dict[str, int]] | None:
    """The routed rows of one search and their ``semantic`` counts, or ``None``.

    ``None`` keeps the keyword rows unchanged: the leg cannot answer, found nothing,
    or the query holds an excluded term or a quoted phrase, which the vector leg
    cannot honour. *window* is the keyword search's own window, *scored* its ranked
    ``(..., meta, needs_snippet)`` tuples, and *accept* the caller's own filter on a
    window row, which :meth:`VectorLeg.rank` asks before its cap.

    Coverage is counted over the window's visible sessions, the ones the chunk pass
    indexes, so a store holding more sessions than the window can still reach
    ``SEMANTIC_MIN_COVERAGE``.
    """
    from kiro_crew.history_derivation import is_incognito_transcript

    if '"' in query or any(needle.excluded for needle in needles):
        return None
    leg = projection.vector_leg
    ready = leg.signature()
    if ready is None:
        return None
    embedder, signature = ready
    candidates = {
        meta["key"]: meta.get("modified", 0.0)
        for meta in window
        if not is_incognito_transcript(meta.get("memory_mode"))
    }
    metas = {meta["key"]: meta for meta in window}
    keeps = None if accept is None else (lambda key: accept(metas[key]))
    hits = leg.rank(query, candidates, embedder, signature, keeps)
    if not hits:
        return None
    visible = set(candidates)
    try:
        indexed = len(visible & projection.vector_store.embedded_keys(signature))
    except Exception:  # noqa: BLE001 -- unknown coverage reads as none built
        indexed = 0
    coverage = indexed / len(visible) if visible else 0.0
    keyword = [entry[-2] for entry in scored]
    top_folded = projection._log._folded_content(keyword[0]["key"])[1] if keyword else None
    routed = route(keyword, hits, metas, literal=is_literal(query, top_folded), coverage=coverage)
    return routed[:limit], {"indexed": indexed, "eligible": len(visible)}
